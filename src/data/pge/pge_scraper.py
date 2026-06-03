"""
PG&E dataset scrapers.

Workflow
--------
1. Run discover_service() to see all available FeatureServer layers.
2. Run discover_layer(layer_id) to see fields and record count for a layer.
3. Run scrape_layer(layer_id, ...) to pull the full dataset to chunked CSVs.

Output goes to data/raw/pge/ by default.

Output file naming convention
------------------------------
    pge_layer{N}_part{chunk:03d}.csv

The prefix can be overridden with filename_prefix on scrape_layer().


NOTE: The layer that you will want is layer 25. This may be updated in the future, so keeping the discover functionality
python scripts/scrape_pge.py layer --layer-id 25

"""
from __future__ import annotations

import csv as _csv
from pathlib import Path
from typing import Optional

from src.data.pge.pge_client import PGEClient
from src.data.scraper_utils import DEFAULT_MAX_FILE_MB, inject_coords, load_progress, pages_to_csv

DATA_RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "pge"


# ── Discovery helpers ─────────────────────────────────────────────────────────

def discover_service() -> None:
    """Print a summary of all layers available in the PG&E FeatureServer."""
    client = PGEClient()
    info = client.get_service_info()

    desc = info.get("serviceDescription") or info.get("description") or "(no description)"
    print(f"\nPG&E FeatureServer")
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

    print(f"\nNext step: run with 'discover --layer-id N' to see fields for a layer.")


def discover_layer(layer_id: int) -> None:
    """Print field metadata and record count for a specific layer."""
    client = PGEClient()
    info = client.get_layer_info(layer_id)

    print(f"\nLayer {layer_id}: {info.get('name', '?')}")
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

    print(f"\nNext step: run 'layer --layer-id {layer_id}' to scrape all records.")


# ── Layer scraper ─────────────────────────────────────────────────────────────

def scrape_layer(
    layer_id: int,
    where: str = "1=1",
    out_fields: str = "*",
    order_by: str = "OBJECTID",
    include_geometry: bool = True,
    add_substation_coords: bool = False,
    output_dir: Path = DATA_RAW_DIR,
    filename_prefix: Optional[str] = None,
    max_file_mb: float = DEFAULT_MAX_FILE_MB,
    page_size: int = 1000,
) -> list[Path]:
    """
    Scrape all features from a PG&E FeatureServer layer to chunked CSVs.

    Automatically resumes if a previous run was interrupted (progress file detected).
    Press Ctrl+C at any time to stop safely — re-run the same command to continue.

    Parameters
    ----------
    layer_id : int
        FeatureServer layer index. Use discover_service() to list available IDs.
    where : str
        SQL WHERE clause to filter records. "1=1" fetches all.
    out_fields : str
        Comma-separated field names, or "*" for all fields.
    order_by : str
        Field to sort by for stable pagination. Default "OBJECTID".
    include_geometry : bool
        If True, point geometry is written as geometry_x / geometry_y columns.
    output_dir : Path
        Directory to write CSV files. Created if absent.
    filename_prefix : str | None
        Override the auto-generated prefix (default: pge_layer{N}).
    max_file_mb : float
        Rotate to a new chunk file when the current CSV reaches this size (MB).
    page_size : int
        Records per API request. Most ArcGIS servers cap at 1000–2000.

    Returns
    -------
    list[Path]
        Paths to every CSV file written (or appended to) in this session.
    """
    output_dir = Path(output_dir)
    prefix = filename_prefix or f"pge_layer{layer_id}"

    resume = load_progress(output_dir, prefix)
    start_offset = resume["offset"] if resume else 0

    if resume:
        print(f"\nResuming PG&E layer {layer_id} from row {start_offset:,}")
    else:
        print(f"\nScraping PG&E layer {layer_id}")
    print(f"  where        : {where}")
    print(f"  out_fields   : {out_fields}")
    print(f"  geometry     : {'yes' if include_geometry else 'no'}")
    print(f"  output dir   : {output_dir}")
    print(f"  max file     : {max_file_mb} MB\n")

    client = PGEClient()
    pager = client.paginate_layer(
        layer_id,
        where=where,
        out_fields=out_fields,
        order_by=order_by,
        page_size=page_size,
        start_offset=start_offset,
        include_geometry=include_geometry,
    )

    if add_substation_coords:
        print("  Building coordinate lookup from layer 0 (EDSubstations)...")
        lookup = client.build_coordinate_lookup(0, "SubstationName")
        print(f"  {len(lookup)} substations with coordinates.\n")
        pager = inject_coords(pager, lookup, "subname")

    return pages_to_csv(
        pager,
        output_dir,
        prefix,
        start=None,
        end=None,
        max_file_mb=max_file_mb,
        resume=resume,
    )


# ── Substation attributes (layer 0) ──────────────────────────────────────────

#: All fields fetched from EDSubstations (layer 0).
_ATTR_FIELDS = (
    "SubstationName,SubstationID,Division,Voltage_kV,"
    "NUMBANKS,UNGROUNDEDBANKS,Existing_DG,Queued_DG,Total_DG,REDACTED"
)

#: Output columns for pge_substation_attributes.csv (raw, units unchanged).
_ATTR_CSV_COLS = [
    "substation_name", "substation_id", "division", "voltage_kv",
    "num_banks", "ungrounded_banks",
    "existing_dg_kw", "queued_dg_kw", "total_dg_kw",
    "redacted",
    "longitude", "latitude",
]


def scrape_substation_attributes(
    output_dir: Path = DATA_RAW_DIR,
) -> Path:
    """
    Scrape physical and DER attributes for every PG&E substation from
    EDSubstations (layer 0).

    Raw field → output column mapping
    ----------------------------------
    SubstationName   → substation_name
    SubstationID     → substation_id
    Division         → division          (service division, e.g. "Kern")
    Voltage_kV       → voltage_kv        (minimum bus voltage as a string, e.g. "12")
    NUMBANKS         → num_banks         (number of transformer banks)
    UNGROUNDEDBANKS  → ungrounded_banks
    Existing_DG      → existing_dg_kw    (kW — divide by 1000 for MW in processing)
    Queued_DG        → queued_dg_kw      (kW)
    Total_DG         → total_dg_kw       (kW)
    REDACTED         → redacted          ("Yes"/"No" data-quality flag)
    geometry (point) → longitude, latitude

    Note on units: DG fields are in kW here. process_substations.py divides
    by 1000 to match SCE and SDGE, which store generation in MW.

    Output: pge_substation_attributes.csv
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    client = PGEClient()
    total_count = client.get_record_count(0)
    print(f"Fetching PG&E EDSubstations attributes (layer 0) — {total_count} records ...")

    rows_out: list[dict] = []
    for rows, _ in client.paginate_layer(
        0,
        out_fields=_ATTR_FIELDS,
        include_geometry=True,   # point layer — gives longitude / latitude
        page_size=1000,
    ):
        for row in rows:
            rows_out.append({
                "substation_name":  row.get("SubstationName", ""),
                "substation_id":    row.get("SubstationID", ""),
                "division":         row.get("Division", ""),
                "voltage_kv":       row.get("Voltage_kV", ""),
                "num_banks":        row.get("NUMBANKS", ""),
                "ungrounded_banks": row.get("UNGROUNDEDBANKS", ""),
                "existing_dg_kw":   row.get("Existing_DG", ""),
                "queued_dg_kw":     row.get("Queued_DG", ""),
                "total_dg_kw":      row.get("Total_DG", ""),
                "redacted":         row.get("REDACTED", ""),
                "longitude":        row.get("longitude", ""),
                "latitude":         row.get("latitude", ""),
            })
        print(f"  {len(rows_out)}/{total_count}", end="\r")

    out_path = output_dir / "pge_substation_attributes.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = _csv.DictWriter(fh, fieldnames=_ATTR_CSV_COLS)
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"\n  {len(rows_out)} substations -> {out_path}")
    return out_path
