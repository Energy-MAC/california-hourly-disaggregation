"""
SCE dataset scrapers.

Available layers in the ICA Tables FeatureServer
-------------------------------------------------
  0  DRPEP_FGDB_DATE_TIME      Metadata — data extraction date/time
  1  Circuit Load Profile       Hourly circuit-level load (min/max metrics)
  2  Substation Load Profile    Substation-level load by month/year  ← primary target
  3  ICA Single Consolidated Table  Combined ICA dataset

Workflow
--------
1. Run discover_service() to confirm available layers.
2. Run discover_layer(layer_id) to see fields and record count.
3. Run scrape_layer(layer_id, ...) to pull the full dataset to chunked CSVs.

Output goes to data/raw/sce/ by default.

Output file naming convention
------------------------------
    sce_layer{N}_earliest_latest_part{chunk:03d}.csv

NOTE: The layer that you will want is layer 2. This may be updated in the future, so keeping the discover functionality

python scripts/scrape_sce.py layer --layer-id 2

"""
from __future__ import annotations

import csv as _csv
import math as _math
import re as _re
from collections import Counter as _Counter
from pathlib import Path
from typing import Optional

from src.data.arcgis_client import ArcGISClient
from src.data.sce.sce_client import SCEClient
from src.data.scraper_utils import DEFAULT_MAX_FILE_MB, inject_coords, load_progress, pages_to_csv

_SQRT3 = _math.sqrt(3)
_PF = 0.95
_KV_RE = _re.compile(r"([\d.]+)\s*kv", _re.IGNORECASE)


def _parse_kv(voltage_str: str) -> Optional[float]:
    """'12KV' -> 12.0, '4.8KV' -> 4.8, unrecognised -> None."""
    m = _KV_RE.search(voltage_str or "")
    return float(m.group(1)) if m else None

# SCE's spatial substation layer lives in a separate service from ICA_Tables.
# ICA_Layer/Substations has SUB_NAME (full names) matching the SUBSTATION field in layer 2.
_SCE_ICA_LAYER_URL = (
    "https://services5.arcgis.com/z6hI6KRjKHvhNO0r/arcgis/rest/services"
    "/ICA_Layer/FeatureServer"
)

DATA_RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "sce"


# ── Discovery helpers ─────────────────────────────────────────────────────────

def discover_service() -> None:
    """Print a summary of all layers available in the SCE ICA Tables FeatureServer."""
    client = SCEClient()
    info = client.get_service_info()

    desc = info.get("serviceDescription") or info.get("description") or "(no description)"
    print(f"\nSCE ICA Tables FeatureServer")
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
    client = SCEClient()
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
    Scrape all features from an SCE ICA Tables FeatureServer layer to chunked CSVs.

    Automatically resumes if a previous run was interrupted (progress file detected).
    Press Ctrl+C at any time to stop safely — re-run the same command to continue.

    Parameters
    ----------
    layer_id : int
        FeatureServer layer index. Use discover_service() to list IDs.
        Layer 2 = Substation Load Profile (primary target).
    where : str
        SQL WHERE clause to filter records. "1=1" fetches all.
    out_fields : str
        Comma-separated field names, or "*" for all fields.
    order_by : str
        Field to sort by for stable pagination. Default "OBJECTID".
    include_geometry : bool
        If True, point geometry is written as longitude/latitude columns.
    add_substation_coords : bool
        If True, joins longitude/latitude from the SCE ICA_Layer/Substations
        spatial service (SUB_NAME → SUBSTATION). Use with layer_id=2.
    output_dir : Path
        Directory to write CSV files. Created if absent.
    filename_prefix : str | None
        Override the auto-generated prefix (default: sce_layer{N}).
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
    prefix = filename_prefix or f"sce_layer{layer_id}"

    resume = load_progress(output_dir, prefix)
    start_offset = resume["offset"] if resume else 0

    if resume:
        print(f"\nResuming SCE layer {layer_id} from row {start_offset:,}")
    else:
        print(f"\nScraping SCE layer {layer_id}")
    print(f"  where        : {where}")
    print(f"  out_fields   : {out_fields}")
    print(f"  geometry     : {'yes' if include_geometry else 'no'}")
    print(f"  output dir   : {output_dir}")
    print(f"  max file     : {max_file_mb} MB\n")

    client = SCEClient()
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
        print("  Building coordinate lookup from ICA_Layer/Substations (SUB_NAME)...")
        coord_client = ArcGISClient(_SCE_ICA_LAYER_URL)
        lookup = coord_client.build_coordinate_lookup(0, "SUB_NAME")
        print(f"  {len(lookup)} substations with coordinates.\n")
        pager = inject_coords(pager, lookup, "SUBSTATION")

    return pages_to_csv(
        pager,
        output_dir,
        prefix,
        start=None,
        end=None,
        max_file_mb=max_file_mb,
        resume=resume,
    )


# ── Substation attributes (Table 3) ──────────────────────────────────────────

#: Output columns for sce_substation_attributes.csv
_ATTR_CSV_COLS = [
    "substation_name", "subst_id", "sys_name",
    "existing_gen", "queued_gen", "total_gen", "projected_load",
    "der_penetration", "max_remain_cap",
    "voltage_kv", "circuit_count",
    "res_pct", "com_pct", "agr_pct", "ind_pct", "other_pct",
    "res_total", "com_total", "agr_total", "ind_total", "other_total",
    "note_sub",
]


def scrape_substation_attributes(
    output_dir: Path = DATA_RAW_DIR,
) -> Path:
    """
    Scrape per-substation physical and DER attributes from ICA Tables Table 3
    (ICA Single Consolidated Table).

    Table 3 contains two row types distinguished by layer_identifier:
      ESRI_DRP_SUBSTATION_T_DRP  — one substation-level aggregate row per substation
      ESRI_CIRCUIT_3_T_DRP       — one row per circuit (multiple per substation)

    From substation-level rows:
      substation_name, subst_id, sys_name, note_sub,
      existing_gen, queued_gen, total_gen, projected_load,
      der_penetration (penetration_level), max_remain_cap

    Aggregated from circuit-level rows per substation:
      voltage_kv      dominant circuit_voltage (most common, kV as float)
      circuit_count   number of circuits
      res/com/agr/ind/other _pct   customer-type percentages (recalculated from totals)
      res/com/agr/ind/other _total customer-type totals (summed across circuits)

    Output: sce_substation_attributes.csv
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    client = SCEClient()

    # ── 1. Substation-level rows ──────────────────────────────────────────────
    print("Fetching substation-level rows from ICA Tables Table 3 ...")
    sub_rows: dict[str, dict] = {}
    for rows, total in client.paginate_layer(
        3,
        where="layer_identifier='ESRI_DRP_SUBSTATION_T_DRP'",
        out_fields=(
            "sub_name,subst_id,sys_name,note_sub,"
            "existing_gen,queued_gen,total_gen,projected_load,"
            "penetration_level,max_remain_cap"
        ),
        include_geometry=False,
        page_size=1000,
    ):
        for row in rows:
            name = row.get("sub_name") or ""
            sub_rows[name] = {
                "substation_name": name,
                "subst_id":        row.get("subst_id", ""),
                "sys_name":        row.get("sys_name", ""),
                "note_sub":        row.get("note_sub", ""),
                "existing_gen":    row.get("existing_gen", ""),
                "queued_gen":      row.get("queued_gen", ""),
                "total_gen":       row.get("total_gen", ""),
                "projected_load":  row.get("projected_load", ""),
                "der_penetration": row.get("penetration_level", ""),
                "max_remain_cap":  row.get("max_remain_cap", ""),
            }
        print(f"  {len(sub_rows)}/{total} substation rows", end="\r")
    print(f"\n  {len(sub_rows)} substations.")

    # ── 2. Circuit-level rows (voltage + customer mix) ────────────────────────
    print("Fetching circuit-level rows from ICA Tables Table 3 ...")
    _CUST_TOTAL_FIELDS = ("res_total", "com_total", "agr_total", "ind_total", "other_total")
    # Accumulate per substation
    volt_counter: dict[str, _Counter[str]] = {}
    ckt_count:    dict[str, int]           = {}
    cust_totals:  dict[str, dict[str, float]] = {}

    ckt_rows_seen = 0
    for rows, total in client.paginate_layer(
        3,
        where="layer_identifier='ESRI_CIRCUIT_3_T_DRP'",
        out_fields="sub_name,circuit_voltage," + ",".join(_CUST_TOTAL_FIELDS),
        include_geometry=False,
        page_size=1000,
    ):
        for row in rows:
            sub = row.get("sub_name") or ""
            volt_counter.setdefault(sub, _Counter())[str(row.get("circuit_voltage") or "")] += 1
            ckt_count[sub] = ckt_count.get(sub, 0) + 1
            if sub not in cust_totals:
                cust_totals[sub] = {f: 0.0 for f in _CUST_TOTAL_FIELDS}
            for f in _CUST_TOTAL_FIELDS:
                try:
                    cust_totals[sub][f] += float(row.get(f) or 0)
                except (TypeError, ValueError):
                    pass
        ckt_rows_seen += len(rows)
        print(f"  {ckt_rows_seen}/{total} circuit rows", end="\r")
    print(f"\n  {ckt_rows_seen} circuit rows covering {len(ckt_count)} substations.")

    # ── 3. Merge and write ────────────────────────────────────────────────────
    all_sub_names = sorted(set(sub_rows) | set(ckt_count))
    out_rows: list[dict] = []

    for name in all_sub_names:
        row = sub_rows.get(name, {"substation_name": name})
        row = dict(row)  # copy

        # Voltage from circuit rows.  Table 3 circuit_voltage is a bare number
        # ("12") not "12KV", so _parse_kv falls back to a direct float conversion.
        if name in volt_counter and volt_counter[name]:
            dominant_volt_str = volt_counter[name].most_common(1)[0][0]
            if dominant_volt_str:
                kv = _parse_kv(dominant_volt_str)
                if kv is None:
                    try:
                        kv = float(dominant_volt_str)
                    except (ValueError, TypeError):
                        kv = None
                row["voltage_kv"] = "" if kv is None else kv
            else:
                row["voltage_kv"] = ""
        else:
            row["voltage_kv"] = ""

        row["circuit_count"] = ckt_count.get(name, "")

        # Customer totals and derived percentages
        totals = cust_totals.get(name, {})
        grand = sum(totals.values())
        for f in _CUST_TOTAL_FIELDS:
            row[f] = totals.get(f, "")
        pct_map = {
            "res_total": "res_pct", "com_total": "com_pct", "agr_total": "agr_pct",
            "ind_total": "ind_pct", "other_total": "other_pct",
        }
        for tot_f, pct_f in pct_map.items():
            if grand and tot_f in totals:
                row[pct_f] = round(totals.get(tot_f, 0) / grand * 100, 4)
            else:
                row[pct_f] = ""

        # Ensure all columns present
        for col in _ATTR_CSV_COLS:
            row.setdefault(col, "")

        out_rows.append({col: row[col] for col in _ATTR_CSV_COLS})

    out_path = output_dir / "sce_substation_attributes.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = _csv.DictWriter(fh, fieldnames=_ATTR_CSV_COLS)
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"  {len(out_rows)} substations -> {out_path}")
    return out_path


# ── Amps -> MW conversion (Table 2) ──────────────────────────────────────────

def convert_layer2_to_mw(
    voltage_csv: Optional[Path] = None,
    output_dir: Path = DATA_RAW_DIR,
) -> Path:
    """
    [DEPRECATED] Convert sce_layer2_*.csv (Amps) to MW via per-substation voltage.

    The conversion produced inaccurate results. Use the DRPEP bulk download
    (scripts/ingest_sce_bulk_download.py) for MW data instead.
    Output files are in data/raw/sce/deprecated/.

    Original docstring: Convert existing sce_layer2_*.csv (MIN_LOAD / MAX_LOAD in Amps) to MW and
    write sce_layer2_mw_part001.csv with the same schema.

    Loads per-substation voltage from sce_substation_voltages.csv (runs
    scrape_substation_voltages() first if the file is absent).

    Conversion: MW = Amps x sqrt(3) x 0.95 x V_kV / 1000

    Substations with no voltage entry are written unconverted and listed at the end.
    """
    output_dir = Path(output_dir)
    if voltage_csv is None:
        voltage_csv = output_dir / "sce_substation_voltages.csv"
    if not voltage_csv.exists():
        raise FileNotFoundError(
            f"{voltage_csv} not found. This function is deprecated; "
            "see sce_substation_attributes.csv for voltage data."
        )

    voltage_lookup: dict[str, Optional[float]] = {}
    with open(voltage_csv, newline="", encoding="utf-8") as fh:
        for row in _csv.DictReader(fh):
            kv_str = row.get("dominant_voltage_kv", "")
            voltage_lookup[row["substation_name"]] = float(kv_str) if kv_str else None
    print(f"Loaded voltage for {len(voltage_lookup)} substations.")

    layer2_files = sorted(output_dir.glob("sce_layer2_earliest_latest_*.csv"))
    if not layer2_files:
        raise FileNotFoundError(f"No sce_layer2_earliest_latest_*.csv in {output_dir}")

    out_path = output_dir / "sce_layer2_mw_part001.csv"
    no_voltage: set[str] = set()
    rows_written = 0

    with open(out_path, "w", newline="", encoding="utf-8") as fout:
        writer: Optional[_csv.DictWriter] = None
        for src in layer2_files:
            with open(src, newline="", encoding="utf-8-sig") as fin:
                reader = _csv.DictReader(fin)
                if writer is None:
                    writer = _csv.DictWriter(fout, fieldnames=reader.fieldnames)
                    writer.writeheader()
                for row in reader:
                    sub = row["SUBSTATION"]
                    v_kv = voltage_lookup.get(sub)
                    if v_kv is not None:
                        try:
                            row["MIN_LOAD"] = round(
                                float(row["MIN_LOAD"]) * _SQRT3 * _PF * v_kv / 1000, 6
                            )
                            row["MAX_LOAD"] = round(
                                float(row["MAX_LOAD"]) * _SQRT3 * _PF * v_kv / 1000, 6
                            )
                        except (ValueError, TypeError):
                            pass
                    else:
                        no_voltage.add(sub)
                    writer.writerow(row)
                    rows_written += 1

    print(f"Wrote {rows_written:,} rows -> {out_path}")
    if no_voltage:
        print(f"  {len(no_voltage)} substations had no voltage (left unconverted):")
        for s in sorted(no_voltage):
            print(f"    {s}")
    return out_path
