"""
scrape_osm_substations.py

Fetches all power=substation features in California from the OpenStreetMap
Overpass API (the same data shown on openinframap.org).

Source
------
  Overpass API: https://overpass-api.de/api/interpreter
  OSM tag: power=substation
  Bounding box: California (32.4,-124.6,42.1,-113.9)

Coverage
--------
  ~4,800 features (344 nodes + 4,450 ways + 44 relations as of 2026-07).
  Ways and relations use their centroid (Overpass `out center`).

Output
------
  data/raw/osm/osm_substations_ca.csv

Columns
-------
  osm_type      node | way | relation
  osm_id        OSM element ID
  lat, lon      centroid coordinates
  name          OSM name tag (empty if not tagged)
  operator      utility operator (e.g. "Pacific Gas and Electric")
  voltage       voltage tag (kV string, e.g. "115000" = 115 kV)
  substation    OSM substation type (transmission, distribution, etc.)
  ref           reference/ID number

Usage
-----
  python scripts/data/substations/scrape_osm_substations.py
  python scripts/data/substations/scrape_osm_substations.py --out data/raw/osm/osm_substations_ca.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = ROOT / "data" / "raw" / "osm" / "osm_substations_ca.csv"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
CA_BBOX = "32.4,-124.6,42.1,-113.9"
USER_AGENT = "CaliforniaHourlyDisaggregation/1.0 (research; contact: github.com/Energy-MAC)"

KEEP_TAGS = ["name", "operator", "voltage", "substation", "ref"]

OUT_COLS = ["osm_type", "osm_id", "lat", "lon"] + KEEP_TAGS


# ── Overpass fetch ─────────────────────────────────────────────────────────────

def _overpass_query(query: str, retries: int = 3, backoff: float = 30.0) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(
        OVERPASS_URL, data=data,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 504) and attempt < retries - 1:
                wait = backoff * (attempt + 1)
                print(f"  HTTP {e.code} — waiting {wait:.0f}s before retry {attempt + 2}/{retries} ...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Overpass fetch failed after all retries")


def fetch_all(bbox: str) -> list[dict]:
    """Single query for all power=substation features; uses `out center` for ways/relations."""
    query = (
        f"[out:json][timeout:90];"
        f"("
        f"node[\"power\"=\"substation\"]({bbox});"
        f"way[\"power\"=\"substation\"]({bbox});"
        f"relation[\"power\"=\"substation\"]({bbox});"
        f");"
        f"out center;"
    )
    print(f"Querying Overpass API for power=substation in bbox {bbox} ...")
    resp = _overpass_query(query)
    return resp.get("elements", [])


# ── Parse ──────────────────────────────────────────────────────────────────────

def parse_elements(elements: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for el in elements:
        osm_type = el.get("type", "")
        osm_id   = el.get("id", "")
        tags     = el.get("tags", {})

        # Coordinates: nodes have lat/lon directly; ways/relations have a center dict
        if osm_type == "node":
            lat = el.get("lat")
            lon = el.get("lon")
        else:
            center = el.get("center", {})
            lat = center.get("lat")
            lon = center.get("lon")

        if lat is None or lon is None:
            continue

        row = {
            "osm_type": osm_type,
            "osm_id":   osm_id,
            "lat":      lat,
            "lon":      lon,
        }
        for tag in KEEP_TAGS:
            row[tag] = tags.get(tag, "")

        rows.append(row)
    return rows


# ── Write ──────────────────────────────────────────────────────────────────────

def write_csv(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_COLS)
        writer.writeheader()
        writer.writerows(rows)


# ── CLI ────────────────────────────────────────────────────────────────────────

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

    elements = fetch_all(CA_BBOX)
    print(f"Received {len(elements):,} elements.")

    rows = parse_elements(elements)
    print(f"Parsed {len(rows):,} substations with coordinates.")

    # Summary by type
    from collections import Counter
    type_counts = Counter(r["osm_type"] for r in rows)
    for t, n in sorted(type_counts.items()):
        print(f"  {t}: {n:,}")

    # Tag coverage
    for tag in KEEP_TAGS:
        n = sum(1 for r in rows if r.get(tag))
        print(f"  has '{tag}': {n:,} / {len(rows):,}")

    write_csv(rows, out)
    print(f"\nWrote {out}  ({out.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
