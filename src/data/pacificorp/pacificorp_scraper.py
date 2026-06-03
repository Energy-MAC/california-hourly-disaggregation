"""
PacifiCorp dataset scrapers.

Available layers in the Transmission_and_Distribution_Public FeatureServer
---------------------------------------------------------------------------
  0  Poles
  1  Substations          ← primary target: Name, Sub_Type, longitude, latitude
  3  Transmission Lines
  4  Distribution Lines
  5  Service Territory

Geometry is embedded directly in each layer (no separate coordinate join needed).
Coordinates are returned as WGS84 longitude/latitude by default.

Output goes to data/raw/pacificorp/ by default.

Output file naming convention
------------------------------
    pacificorp_layer{N}_earliest_latest_part{chunk:03d}.csv
"""
from __future__ import annotations

import csv as _csv
from collections import defaultdict as _defaultdict
from pathlib import Path
from typing import Optional

from src.data.arcgis_client import ArcGISClient
from src.data.pacificorp.pacificorp_client import PacifiCorpClient
from src.data.scraper_utils import DEFAULT_MAX_FILE_MB, load_progress, pages_to_csv

_DG_READINESS_URL = (
    "https://services1.arcgis.com/ePo6UhbBpZFy1wO2/ArcGIS/rest/services"
    "/DG%20Readiness%20with%20Net%20Minimum/FeatureServer"
)

DATA_RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "pacificorp"


# ── Discovery helpers ─────────────────────────────────────────────────────────

def discover_service() -> None:
    """Print all layers available in the PacifiCorp FeatureServer."""
    client = PacifiCorpClient()
    info = client.get_service_info()

    desc = info.get("serviceDescription") or info.get("description") or "(no description)"
    print(f"\nPacifiCorp FeatureServer")
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

    print(f"\nNext step: run 'discover --layer-id N' to see fields for a layer.")


def discover_layer(layer_id: int) -> None:
    """Print field metadata and record count for a specific layer."""
    client = PacifiCorpClient()
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
    output_dir: Path = DATA_RAW_DIR,
    filename_prefix: Optional[str] = None,
    max_file_mb: float = DEFAULT_MAX_FILE_MB,
    page_size: int = 2000,
) -> list[Path]:
    """
    Scrape all features from a PacifiCorp FeatureServer layer to chunked CSVs.

    Geometry is embedded in the layer itself — longitude and latitude columns
    are included automatically (no separate coordinate join needed).

    Automatically resumes if a previous run was interrupted.
    Press Ctrl+C to stop safely — re-run the same command to continue.

    Parameters
    ----------
    layer_id : int
        Layer index. Layer 1 = Substations (Name, Sub_Type, longitude, latitude).
    where : str
        SQL WHERE clause. "1=1" fetches all records.
    out_fields : str
        Comma-separated field names, or "*" for all.
    order_by : str
        Sort field for stable pagination. Default "OBJECTID".
    include_geometry : bool
        If True, longitude/latitude columns are added from the layer geometry.
    output_dir : Path
        Directory to write CSV files. Created if absent.
    filename_prefix : str | None
        Override the auto-generated prefix (default: pacificorp_layer{N}).
    max_file_mb : float
        Rotate to a new chunk file when the CSV reaches this size (MB).
    page_size : int
        Records per API request. PacifiCorp server max is 2000.

    Returns
    -------
    list[Path]
        Paths to every CSV file written (or appended to) in this session.
    """
    output_dir = Path(output_dir)
    prefix = filename_prefix or f"pacificorp_layer{layer_id}"

    resume = load_progress(output_dir, prefix)
    start_offset = resume["offset"] if resume else 0

    if resume:
        print(f"\nResuming PacifiCorp layer {layer_id} from row {start_offset:,}")
    else:
        print(f"\nScraping PacifiCorp layer {layer_id}")
    print(f"  where        : {where}")
    print(f"  out_fields   : {out_fields}")
    print(f"  geometry     : {'yes' if include_geometry else 'no'}")
    print(f"  output dir   : {output_dir}")
    print(f"  max file     : {max_file_mb} MB\n")

    client = PacifiCorpClient()
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


# ── DG Readiness attributes ───────────────────────────────────────────────────

def scrape_substation_attributes(
    output_dir: Path = DATA_RAW_DIR,
) -> Path:
    """
    Scrape DER and load attributes from the PacifiCorp DG Readiness service.

    Source: DG Readiness with Net Minimum / FeatureServer / layer 0 (outputLayer)
      https://services1.arcgis.com/ePo6UhbBpZFy1wO2/ArcGIS/rest/services/
      DG%20Readiness%20with%20Net%20Minimum/FeatureServer

    The layer is circuit-level (one row per feeder). Rows are aggregated to
    substation level by summing across all circuits under the same Substation name.

    Output columns
    --------------
    substation_name         raw Substation value (original casing from the source)
    circuit_count           number of circuits under this substation in the layer
    existing_der            sum of Existing_DER across circuits (MW)
    net_min_daytime_load_mw sum of Net_Minimum_Daytime_Load__MW_ across circuits (MW)

    Note: substation names in this layer use title case with occasional trailing
    spaces. process_substations.py joins via strip().upper() to match the all-caps
    names in pacificorp_layer1_*.csv. ~185/202 substations match; the remaining 17
    use alternate or more specific names not in layer 1.

    Output: pacificorp_substation_attributes.csv
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    client = ArcGISClient(_DG_READINESS_URL)
    total_count = client.get_record_count(0)
    print(f"Fetching PacifiCorp DG Readiness circuit rows (layer 0) — {total_count} expected ...")

    # Accumulate per substation (key = strip().upper() for dedup, value = original name)
    sub_name_map: dict[str, str] = {}           # normalised_key → original_name
    sub_der:      dict[str, float] = _defaultdict(float)
    sub_load:     dict[str, float] = _defaultdict(float)
    sub_ckt:      dict[str, int]   = _defaultdict(int)

    rows_seen = 0
    for rows, _ in client.paginate_layer(
        0,
        out_fields="Substation,Existing_DER,Net_Minimum_Daytime_Load__MW_",
        include_geometry=False,
        page_size=1000,
    ):
        for row in rows:
            raw_name = str(row.get("Substation") or "").rstrip()  # strip trailing spaces
            key = raw_name.upper()
            if key not in sub_name_map:
                sub_name_map[key] = raw_name
            try:
                sub_der[key] += float(row.get("Existing_DER") or 0)
            except (TypeError, ValueError):
                pass
            try:
                sub_load[key] += float(row.get("Net_Minimum_Daytime_Load__MW_") or 0)
            except (TypeError, ValueError):
                pass
            sub_ckt[key] += 1
        rows_seen += len(rows)
        print(f"  {rows_seen}/{total_count}", end="\r")

    print(f"\n  {len(sub_name_map)} substations aggregated from {rows_seen} circuit rows.")

    _COLS = ["substation_name", "circuit_count", "existing_der", "net_min_daytime_load_mw"]
    out_rows = [
        {
            "substation_name":         sub_name_map[key],
            "circuit_count":           sub_ckt[key],
            "existing_der":            round(sub_der[key], 6),
            "net_min_daytime_load_mw": round(sub_load[key], 6),
        }
        for key in sorted(sub_name_map)
    ]

    out_path = output_dir / "pacificorp_substation_attributes.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = _csv.DictWriter(fh, fieldnames=_COLS)
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"  {len(out_rows)} substations -> {out_path}")
    return out_path
