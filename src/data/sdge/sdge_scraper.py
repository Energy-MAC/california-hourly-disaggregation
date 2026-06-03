"""
SDG&E dataset scrapers.

SDG&E exposes ~26 separate ArcGIS FeatureServer services under one org.
The typical workflow is two-level discovery before scraping:

  1. List all available services:
         discover_services()

  2. List layers inside a specific service:
         discover_service("ICA_MAP_PROD_Substations_VW")

  3. Inspect fields and record count for a layer:
         discover_layer("ICA_MAP_PROD_Substations_VW", 0)

  4. Scrape all records from a layer:
         scrape_layer("ICA_MAP_PROD_Substations_VW", 0)

Known public services of interest
-----------------------------------
  ICA_MAP_PROD_Substations_VW            Substation polygons + ICA capacity
  ICA_MAP_PROD_LoadCapacityGrids_VW      Circuit-level load capacity
  ICA_MAP_PROD_GenerationCapacityGrids_VW  Circuit-level generation capacity
  ICA_MAP_PROD_GNAGrids_VW               Grid Needs Assessment areas
  SDGE_District_Boundary                 Administrative district boundaries

Output goes to data/raw/sdge/ by default.

Output file naming convention
------------------------------
    sdge_{service_name}_layer{N}_earliest_latest_part{chunk:03d}.csv
"""
from __future__ import annotations

import csv
import io
import json
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.data.sdge.sdge_client import SDGEClient, list_services
from src.data.scraper_utils import DEFAULT_MAX_FILE_MB, load_progress, pages_to_csv

_DOWNLOAD_BASE = "https://interconnectionmapsdge.extweb.sempra.com/Electric/Download"
_DOWNLOAD_DELAY = 0.5  # seconds between ZIP requests

DATA_RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "sdge"


# ── Discovery helpers ─────────────────────────────────────────────────────────

def discover_services() -> None:
    """Print all FeatureServer services available in the SDG&E ArcGIS organization."""
    services = list_services()
    if not services:
        print("No services found.")
        return

    fs_services = [s for s in services if s.get("type") == "FeatureServer"]
    other = [s for s in services if s.get("type") != "FeatureServer"]

    print(f"\nSDG&E ArcGIS Organization  ({len(services)} total services)\n")

    if fs_services:
        print(f"  FeatureServer services ({len(fs_services)}):")
        for s in sorted(fs_services, key=lambda x: x["name"]):
            print(f"    {s['name']}")

    if other:
        print(f"\n  Other services ({len(other)}):")
        for s in sorted(other, key=lambda x: x["name"]):
            print(f"    [{s.get('type', '?')}]  {s['name']}")

    print(f"\nNext step: run 'discover --service SERVICE_NAME' to see layers.")


def discover_service(service_name: str) -> None:
    """Print layers available in a specific SDG&E FeatureServer service."""
    client = SDGEClient(service_name)
    info = client.get_service_info()

    desc = info.get("serviceDescription") or info.get("description") or "(no description)"
    print(f"\nSDG&E FeatureServer: {service_name}")
    print(f"  {client.base_url}")
    print(f"  {desc}\n")

    layers = info.get("layers", []) + info.get("tables", [])
    if not layers:
        print("  No layers found.")
        return

    print(f"  {'ID':>4}  {'Type':<16}  Name")
    print("  " + "-" * 60)
    for layer in layers:
        ltype = "Table" if layer.get("type") == "Table" else "Feature Layer"
        print(f"  {layer['id']:>4}  {ltype:<16}  {layer['name']}")

    print(f"\nNext step: run 'discover --service {service_name} --layer-id N' to see fields.")


def discover_layer(service_name: str, layer_id: int) -> None:
    """Print field metadata and record count for a specific layer."""
    client = SDGEClient(service_name)
    info = client.get_layer_info(layer_id)

    print(f"\nService: {service_name}  |  Layer {layer_id}: {info.get('name', '?')}")
    print(f"  Type          : {info.get('type', '?')}")
    print(f"  Geometry type : {info.get('geometryType', 'none')}")

    try:
        count = client.get_record_count(layer_id)
        print(f"  Record count  : {count:,}")
    except Exception as exc:
        print(f"  Record count  : (unavailable — {exc})")

    fields = info.get("fields", [])
    if not fields:
        print("  No fields found.")
        return

    print(f"\n  {'Name':<40}  {'Type':<25}  Alias")
    print("  " + "-" * 80)
    for f in fields:
        print(f"  {f['name']:<40}  {f['type']:<25}  {f.get('alias', '')}")

    print(f"\nNext step: run 'layer --service {service_name} --layer-id {layer_id}' to scrape.")


# ── Layer scraper ─────────────────────────────────────────────────────────────

def scrape_layer(
    service_name: str,
    layer_id: int,
    where: str = "1=1",
    out_fields: str = "*",
    order_by: str = "OBJECTID",
    include_geometry: bool = True,
    output_dir: Path = DATA_RAW_DIR,
    filename_prefix: Optional[str] = None,
    max_file_mb: float = DEFAULT_MAX_FILE_MB,
    page_size: int = 1000,
) -> list[Path]:
    """
    Scrape all features from an SDG&E FeatureServer layer to chunked CSVs.

    Automatically resumes if a previous run was interrupted.
    Press Ctrl+C at any time to stop safely — re-run the same command to continue.

    Parameters
    ----------
    service_name : str
        FeatureServer service name (from discover_services()).
    layer_id : int
        Layer index within the service (from discover_service()).
    where : str
        SQL WHERE clause. "1=1" fetches all records.
    out_fields : str
        Comma-separated field names, or "*" for all.
    order_by : str
        Field for stable pagination. Default "OBJECTID".
    include_geometry : bool
        If True, point geometry is written as geometry_x / geometry_y columns.
    output_dir : Path
        Directory to write CSV files. Created if absent.
    filename_prefix : str | None
        Override the auto-generated prefix.
    max_file_mb : float
        Rotate to a new chunk file when the CSV reaches this size (MB).
    page_size : int
        Records per API request. Most servers cap at 1000–2000.

    Returns
    -------
    list[Path]
        Paths to every CSV file written (or appended to) in this session.
    """
    output_dir = Path(output_dir)
    prefix = filename_prefix or f"sdge_{service_name}_layer{layer_id}"

    resume = load_progress(output_dir, prefix)
    start_offset = resume["offset"] if resume else 0

    if resume:
        print(f"\nResuming SDG&E {service_name} layer {layer_id} from row {start_offset:,}")
    else:
        print(f"\nScraping SDG&E {service_name} layer {layer_id}")
    print(f"  where        : {where}")
    print(f"  out_fields   : {out_fields}")
    print(f"  geometry     : {'yes' if include_geometry else 'no'}")
    print(f"  output dir   : {output_dir}")
    print(f"  max file     : {max_file_mb} MB\n")

    client = SDGEClient(service_name)
    pager = client.paginate_layer(
        layer_id,
        where=where,
        out_fields=out_fields,
        order_by=order_by,
        page_size=page_size,
        start_offset=start_offset,
        include_geometry=include_geometry,
    )
    return pages_to_csv(
        pager,
        output_dir,
        prefix,
        start=None,
        end=None,
        max_file_mb=max_file_mb,
        resume=resume,
    )


# ── Substation load-profile download scraper ─────────────────────────────────

def _build_download_session() -> requests.Session:
    session = requests.Session()
    # Only retry on genuine transient errors (502/503/504). A 500 from this
    # endpoint consistently means the substation has no published data —
    # retrying wastes time and hits the retry limit with no benefit.
    retry = Retry(total=2, backoff_factor=1.0, status_forcelist={502, 503, 504})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({
        "Referer": "https://interconnectionmapsdge.extweb.sempra.com/",
        "User-Agent": "Mozilla/5.0",
    })
    return session


def _download_substation_rows(
    session: requests.Session, name: str
) -> tuple[Optional[list[dict]], Optional[str]]:
    """
    Download and parse one substation's load profile ZIP.

    Returns
    -------
    (rows, error)
        rows : list[dict] on success, None on any failure.
        error : None on success, short error string on failure.
    """
    encoded = urllib.parse.quote(name, safe="")
    url = f"{_DOWNLOAD_BASE}/SUBSTATION~{encoded}.zip"
    try:
        r = session.get(url, timeout=60)
    except Exception as exc:
        return None, str(exc)

    if r.status_code in (404, 500):
        # 404 = not found; 500 = no data published for this substation
        return None, f"HTTP {r.status_code}"
    try:
        r.raise_for_status()
    except Exception as exc:
        return None, str(exc)

    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                return [], None
            with zf.open(csv_names[0]) as f:
                text = f.read().decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text))), None
    except Exception as exc:
        return None, f"parse error: {exc}"


def get_substation_names(service_name: str = "ICA_MAP_PROD_Substations_VW") -> list[str]:
    """
    Query the SDG&E FeatureServer and return all unique substation NAME values.

    Parameters
    ----------
    service_name : str
        FeatureServer service to pull names from.
        Use the PROD service for the authoritative list.
    """
    client = SDGEClient(service_name)
    names: set[str] = set()
    for rows, _ in client.paginate_layer(0, out_fields="NAME", include_geometry=False):
        for row in rows:
            n = row.get("NAME")
            if n:
                names.add(n)
    return sorted(names)


def scrape_substation_load_profiles(
    service_name: str = "ICA_MAP_PROD_Substations_VW",
    substation_names: Optional[list[str]] = None,
    add_substation_coords: bool = True,
    output_dir: Path = DATA_RAW_DIR,
    filename_prefix: str = "sdge_substation_profiles",
    max_file_mb: float = DEFAULT_MAX_FILE_MB,
) -> list[Path]:
    """
    Download hourly load profiles for every SDG&E substation.

    Each substation's data arrives as a ZIP containing one CSV with rows:
        AssetName, AssetType, DERForecasted, Month, LoadDay, Units,
        hour 1 … hour 24
    (12 months × 2 load-day types = 24 rows = 576 hourly values per substation)

    All substations are concatenated into chunked output CSVs.
    A progress file tracks which substations are done so an interrupted run
    can be resumed by re-running the same command.  Press Ctrl+C to stop safely.

    Parameters
    ----------
    service_name : str
        FeatureServer service used to discover substation names when
        substation_names is None.
    substation_names : list[str] | None
        Explicit list of substation names to download.  None = fetch from
        the FeatureServer automatically.
    output_dir : Path
        Directory to write CSV files.  Created if absent.
    filename_prefix : str
        Prefix for output filenames.
    max_file_mb : float
        Rotate to a new chunk file when the current CSV reaches this size (MB).

    Returns
    -------
    list[Path]
        Paths to every CSV file written in this session.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Progress / resume ─────────────────────────────────────────────────────
    prog_path = output_dir / f"{filename_prefix}_progress.json"
    if prog_path.exists():
        state = json.loads(prog_path.read_text(encoding="utf-8"))
        completed: set[str] = set(state.get("completed", []))
        failed: dict[str, str] = dict(state.get("failed", {}))
        print(f"\nResuming: {len(completed)} substations already complete.")
    else:
        completed = set()
        failed = {}

    # ── Substation name list ──────────────────────────────────────────────────
    if substation_names is None:
        print(f"Fetching substation names from {service_name}...")
        substation_names = get_substation_names(service_name)
        print(f"  Found {len(substation_names)} substations.")

    todo = [n for n in substation_names if n not in completed]
    total = len(substation_names)

    # ── Coordinate lookup (polygon centroids from FeatureServer) ──────────────
    coord_lookup: dict = {}
    if add_substation_coords:
        print(f"  Building coordinate lookup from {service_name} (polygon centroids)...")
        coord_client = SDGEClient(service_name)
        raw = coord_client.build_coordinate_lookup(0, "NAME", use_centroid=True)
        # Index case-insensitively — ZIP AssetName may differ in casing from NAME
        coord_lookup = {k.upper(): v for k, v in raw.items()}
        print(f"  {len(coord_lookup)} substations with coordinates.")

    print(f"\nScraping SDG&E substation load profiles")
    print(f"  substations  : {len(todo)} remaining / {total} total")
    print(f"  coordinates  : {'yes' if add_substation_coords else 'no'}")
    print(f"  output dir   : {output_dir}")
    print(f"  max file     : {max_file_mb} MB\n")

    # ── CSV file state ────────────────────────────────────────────────────────
    max_bytes = max_file_mb * 1024 * 1024
    written_files: list[Path] = []
    fieldnames: Optional[list[str]] = None
    chunk = 1
    current_path: Optional[Path] = None
    current_file = None
    current_writer: Optional[csv.DictWriter] = None

    def _open_chunk() -> None:
        nonlocal current_path, current_file, current_writer, chunk
        if current_file and not current_file.closed:
            current_file.close()
            print(f"\n  Closed: {current_path.name}")
        current_path = output_dir / f"{filename_prefix}_part{chunk:03d}.csv"
        current_file = open(current_path, "w", newline="", encoding="utf-8")
        written_files.append(current_path)
        current_writer = None
        chunk += 1

    _open_chunk()

    session = _build_download_session()
    done_count = len(completed)
    interrupted = False

    try:
        for name in todo:
            rows, error = _download_substation_rows(session, name)

            if error:
                failed[name] = error
                print(f"\n  [skip] {name}: {error}")
            elif not rows:
                failed[name] = "empty"
                print(f"\n  [skip] {name}: empty ZIP")
            else:
                if coord_lookup:
                    for row in rows:
                        key = str(row.get("AssetName") or "").strip().upper()
                        coords = coord_lookup.get(key)
                        row["longitude"] = coords[0] if coords else ""
                        row["latitude"] = coords[1] if coords else ""
                if fieldnames is None:
                    fieldnames = list(rows[0].keys())
                if current_writer is None:
                    current_writer = csv.DictWriter(current_file, fieldnames=fieldnames)
                    current_writer.writeheader()
                current_writer.writerows(rows)
                current_file.flush()
                completed.add(name)
                done_count += 1

                size_mb = current_path.stat().st_size / 1024 / 1024
                pct = done_count / total * 100
                print(
                    f"  {done_count}/{total} ({pct:.1f}%)  {name:<35}"
                    f"  [{size_mb:.1f}/{max_file_mb} MB]",
                    end="\r",
                )

                if current_path.stat().st_size >= max_bytes:
                    _open_chunk()

            prog_path.write_text(
                json.dumps(
                    {"completed": sorted(completed), "failed": failed, "total": total},
                    indent=2,
                ),
                encoding="utf-8",
            )

            time.sleep(_DOWNLOAD_DELAY)

    except KeyboardInterrupt:
        interrupted = True

    finally:
        if current_file and not current_file.closed:
            current_file.close()
            if current_path:
                print(f"\n  Closed: {current_path.name}")

    # Always write a failures CSV so the user can inspect what was skipped
    failures_path = output_dir / f"{filename_prefix}_failed.csv"
    if failed:
        with open(failures_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["substation_name", "error"])
            w.writeheader()
            for sname, err in sorted(failed.items()):
                w.writerow({"substation_name": sname, "error": err})
        print(f"\n  Failures logged: {failures_path.name}  ({len(failed)} substations)")
    elif failures_path.exists():
        failures_path.unlink()  # clean up stale file from a prior run

    if interrupted:
        print(f"\nStopped at {done_count}/{total} substations.")
        print("Re-run the same command to resume.")
    else:
        if prog_path.exists():
            prog_path.unlink()
        print(f"\nDone.  {done_count}/{total} substations downloaded, {len(failed)} skipped.")

    return [f for f in written_files if f.exists() and f.stat().st_size > 0]


# ── Substation attribute scraper ──────────────────────────────────────────────

#: Fields from ICA_MAP_PROD_Substations_VW layer 0 that describe the physical
#: and DER characteristics of each substation.
_ATTR_FIELDS = "NAME,SUBSTATIONTYPE,IMAP_VOLTAGE,EXIST_GEN,QUE_GEN,TOT_GEN,PROJ_LOAD,PENETRATION"


def scrape_substation_attributes(
    service_name: str = "ICA_MAP_QA_Substations_VW",
    output_dir: Path = DATA_RAW_DIR,
) -> Path:
    """
    Scrape physical and DER attributes for every SDG&E substation.

    Queries ICA_MAP_PROD_Substations_VW layer 0 (polygon features) and extracts:
      substation_name   NAME field
      substation_type   SUBSTATIONTYPE  e.g. "69/12 kV"
      voltage_kv        IMAP_VOLTAGE    e.g. "12kV"
      existing_gen      EXIST_GEN       existing DER generation (MW)
      queued_gen        QUE_GEN         queued DER generation (MW)
      total_gen         TOT_GEN         total generation (MW)
      projected_load    PROJ_LOAD       projected load (MW)
      der_penetration   PENETRATION     DER penetration (%)
      longitude         polygon centroid longitude (WGS84)
      latitude          polygon centroid latitude  (WGS84)

    Output: data/raw/sdge/sdge_substation_attributes.csv
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching substation attributes from {service_name} layer 0 ...")
    client = SDGEClient(service_name)
    total_count = client.get_record_count(0)
    print(f"  {total_count} substations expected.")

    rows_out: list[dict] = []
    for rows, _ in client.paginate_layer(
        0,
        out_fields=_ATTR_FIELDS,
        include_geometry=False,
        return_centroid=True,
        page_size=1000,
    ):
        for row in rows:
            rows_out.append({
                "substation_name": row.get("NAME", ""),
                "substation_type": row.get("SUBSTATIONTYPE", ""),
                "voltage_kv":      row.get("IMAP_VOLTAGE", ""),
                "existing_gen":    row.get("EXIST_GEN", ""),
                "queued_gen":      row.get("QUE_GEN", ""),
                "total_gen":       row.get("TOT_GEN", ""),
                "projected_load":  row.get("PROJ_LOAD", ""),
                "der_penetration": row.get("PENETRATION", ""),
                "longitude":       row.get("longitude", ""),
                "latitude":        row.get("latitude", ""),
            })
        print(f"  {len(rows_out)}/{total_count}", end="\r")

    out_path = output_dir / "sdge_substation_attributes.csv"
    _ATTR_COLS = [
        "substation_name", "substation_type", "voltage_kv",
        "existing_gen", "queued_gen", "total_gen", "projected_load", "der_penetration",
        "longitude", "latitude",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_ATTR_COLS)
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"\n  {len(rows_out)} substations -> {out_path}")
    return out_path
