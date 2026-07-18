"""
Scrape SCE circuit-level attributes from the ICA FeatureServer (Layer 4: RAM - Circuits).

Source
------
  https://services5.arcgis.com/z6hI6KRjKHvhNO0r/ArcGIS/rest/services/ICA_Layer/FeatureServer/4

Coverage
--------
  1,998 distribution circuits with ICA/RAM analysis data (~47% of DRPEP circuits by name).
  Joins to sce_circuit_profiles.csv on (CIRCUIT_NAME, CIRCUIT_VOLTAGE).
  Note: CIRCUIT_VOLTAGE here is an integer (e.g. 12); DRPEP uses "12KV" strings.
  Circuits not present in this layer either lack ICA analysis (Layer 1) or are not
  yet analyzed.  Layer 2 (ICA Circuit Segments) has segment-level data for ~3,079
  DRPEP circuits but requires per-segment aggregation — see README for details.

Output
------
  data/raw/sce/sce_circuit_attributes.csv

Columns
-------
  CIRCUIT_ID          SCE internal circuit identifier
  CIRCUIT_NAME        circuit name (matches DRPEP CIRCUIT_NAME)
  CIRCUIT_VOLTAGE     distribution voltage in kV as integer (e.g. 12, 4, 16)
  SUB_NAME            parent substation name and voltage (e.g. "Greening 69/12 kV")
  SUBSTATION_VOLTAGE  substation HV/LV designation (e.g. "69/12 kV")
  SYS_NAME            transmission system area name
  EXISTING_GEN        existing DER generation (MW)
  QUEUED_GEN          queued interconnection requests (MW)
  TOTAL_GEN           existing + queued (MW)
  PROJECTED_LOAD      projected peak load (MW)
  PENETRATION_LEVEL   total gen / projected load (%)
  MAX_REMAIN_CAP      remaining hosting capacity (MW)
  PERCENT_15_CAP      fraction of load at 15% penetration threshold (%)
  NOTE                SCE notes on deliverability or constraints

Usage
-----
  python scripts/data/sce/scrape_sce_circuit_attrs.py
  python scripts/data/sce/scrape_sce_circuit_attrs.py --out data/raw/sce/sce_circuit_attributes.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

BASE_URL = (
    "https://services5.arcgis.com/z6hI6KRjKHvhNO0r"
    "/ArcGIS/rest/services/ICA_Layer/FeatureServer/4"
)
DEFAULT_OUT = ROOT / "data" / "raw" / "sce" / "sce_circuit_attributes.csv"

OUT_FIELDS = [
    "CIRCUIT_ID",
    "CIRCUIT_NAME",
    "CIRCUIT_VOLTAGE",
    "SUB_NAME",
    "SUBSTATION_VOLTAGE",
    "SYS_NAME",
    "EXISTING_GEN",
    "QUEUED_GEN",
    "TOTAL_GEN",
    "PROJECTED_LOAD",
    "PENETRATION_LEVEL",
    "MAX_REMAIN_CAP",
    "PERCENT_15_CAP",
    "NOTE",
]


# ── Fetch ─────────────────────────────────────────────────────────────────────

def _query(params: dict, timeout: int = 60) -> dict:
    params.setdefault("f", "json")
    url = f"{BASE_URL}/query?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def fetch_all(batch: int = 2000) -> list[dict]:
    """Page through all features and return list of attribute dicts."""
    all_rows: list[dict] = []
    offset = 0

    while True:
        data = _query({
            "where": "1=1",
            "outFields": ",".join(OUT_FIELDS),
            "returnGeometry": "false",
            "resultOffset": offset,
            "resultRecordCount": batch,
        })

        if "error" in data:
            raise RuntimeError(f"ArcGIS error: {data['error']}")

        features = data.get("features", [])
        if not features:
            break

        for feat in features:
            all_rows.append(feat["attributes"])

        offset += len(features)

        if not data.get("exceededTransferLimit"):
            break

        print(f"  ... fetched {offset} records so far")

    return all_rows


# ── Write ─────────────────────────────────────────────────────────────────────

def write_csv(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        metavar="PATH",
        help=f"Output CSV path. Default: {DEFAULT_OUT}",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    out = Path(args.out)

    print(f"Fetching ICA Layer 4 (RAM - Circuits) from ArcGIS FeatureServer ...")
    rows = fetch_all()
    print(f"Fetched {len(rows):,} records.")

    write_csv(rows, out)
    print(f"Wrote {out}  ({out.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
