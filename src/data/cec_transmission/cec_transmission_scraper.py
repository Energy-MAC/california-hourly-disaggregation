"""
CEC California Electric Transmission Lines scraper.

Source
------
California Energy Commission ArcGIS FeatureServer, layer 2 (TransmissionLine_CEC).
Service description: "California Electric Transmission Lines".
URL: https://services3.arcgis.com/bWPjFyq029ChCGur/arcgis/rest/services/Transmission_Line/FeatureServer/2

Layer schema (discovered 2026-06-25, 6,839 records, esriGeometryPolyline)
--------------------------------------------------------------------------
OBJECTID         — ArcGIS internal row ID
Name             — Line name (e.g. "AMP 115kV")
kV               — Voltage class string (e.g. "115", "500", "230")
kV_Sort          — Voltage class as number (for sorting)
Owner            — Utility / owner abbreviation (e.g. "PGE", "SCE", "LADWP")
Status           — "Operational", "Proposed", "Under Construction", etc.
Circuit          — "Single" or "Double"
Type             — "OH" (overhead) or "UG" (underground)
Legend           — Voltage-class grouping label used in CEC map legend
Length_Mile      — Segment length in miles
Length_Feet      — Segment length in feet (string)
TLine_Name       — Transmission line name (often blank; distinct from Name)
Source           — Data source attribution
Comments         — Free-text notes
Creator          — ArcGIS editor username
Creator_Date     — Creation timestamp (epoch ms)
Last_Editor      — Last editor username
Last_Editor_Date — Last edit timestamp (epoch ms)
GlobalID         — ArcGIS GlobalID (UUID)
Shape__Length    — Projected length in map units (not miles)

Geometry: esriGeometryPolyline — each record is one segment of a transmission
line, represented as one or more ordered coordinate paths.  The endpoints
of each segment correspond to substation or junction locations.

Output columns added by the scraper beyond the raw attributes
-------------------------------------------------------------
lon_start, lat_start — first vertex of the first path (one endpoint)
lon_end,   lat_end   — last vertex of the last path (other endpoint)
geometry_json        — full polyline geometry serialised as JSON, preserving
                       all intermediate vertices for GIS use

These endpoint columns make it straightforward to use this dataset as a
substation-location reference: transmission lines terminate at substations,
so the start/end coordinates identify grid node positions independent of
utility ICA data or DataBasin.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

from src.data.cec_transmission.cec_transmission_client import CECTransmissionClient, LAYER_ID
from src.data.scraper_utils import DEFAULT_MAX_FILE_MB, load_progress, pages_to_csv

DATA_RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "cec_transmission"

_ATTR_FIELDS = (
    "OBJECTID,Name,kV,kV_Sort,Owner,Status,Circuit,Type,Legend,"
    "Length_Mile,Length_Feet,TLine_Name,Source,Comments,"
    "Creator_Date,Last_Editor_Date,GlobalID"
)

_OUT_COLS = [
    "OBJECTID", "Name", "kV", "kV_Sort", "Owner", "Status",
    "Circuit", "Type", "Legend", "Length_Mile", "Length_Feet",
    "TLine_Name", "Source", "Comments",
    "Creator_Date", "Last_Editor_Date", "GlobalID",
    "lon_start", "lat_start", "lon_end", "lat_end",
    "geometry_json",
]


def _extract_endpoints(geom_json: str) -> tuple[Optional[float], Optional[float],
                                                  Optional[float], Optional[float]]:
    """
    Parse a polyline geometry JSON string and return (lon_start, lat_start, lon_end, lat_end).

    ArcGIS polylines are {"paths": [[[x, y], [x, y], ...], ...]}.
    Start = first vertex of first path; end = last vertex of last path.
    Returns (None, None, None, None) if the geometry cannot be parsed.
    """
    try:
        geom = json.loads(geom_json)
        paths = geom.get("paths", [])
        if not paths:
            return None, None, None, None
        first_pt = paths[0][0]
        last_pt  = paths[-1][-1]
        return float(first_pt[0]), float(first_pt[1]), float(last_pt[0]), float(last_pt[1])
    except Exception:
        return None, None, None, None


# ── Discovery ─────────────────────────────────────────────────────────────────

def discover_service() -> None:
    """Print all layers in the CEC Transmission Line FeatureServer."""
    client = CECTransmissionClient()
    info   = client.get_service_info()
    desc   = info.get("serviceDescription") or info.get("description") or "(no description)"
    layers = info.get("layers", []) + info.get("tables", [])

    print(f"\nCEC Transmission Line FeatureServer")
    print(f"  {client.base_url}")
    print(f"  {desc}\n")
    print(f"  {'ID':>4}  {'Type':<20}  Name")
    print("  " + "-" * 55)
    for lyr in layers:
        ltype = "Table" if lyr.get("type") == "Table" else "Feature Layer"
        print(f"  {lyr['id']:>4}  {ltype:<20}  {lyr['name']}")
    print(f"\nUse 'discover --layer-id N' to inspect field metadata.")


def discover_layer(layer_id: int = LAYER_ID) -> None:
    """Print field metadata and record count for a layer."""
    client = CECTransmissionClient()
    info   = client.get_layer_info(layer_id)

    print(f"\nLayer {layer_id}: {info.get('name', '?')}")
    print(f"  Type          : {info.get('type', '?')}")
    print(f"  Geometry type : {info.get('geometryType', 'none')}")
    try:
        count = client.get_record_count(layer_id)
        print(f"  Record count  : {count:,}")
    except Exception as exc:
        print(f"  Record count  : (unavailable — {exc})")

    fields = info.get("fields", [])
    print(f"\n  {'Name':<35}  {'Type':<30}  Alias")
    print("  " + "-" * 80)
    for f in fields:
        print(f"  {f['name']:<35}  {f['type']:<30}  {f.get('alias', '')}")

    print(f"\nUse 'scrape' to download all {count:,} records.")


# ── Scraper ───────────────────────────────────────────────────────────────────

def scrape_transmission_lines(
    output_dir: Path = DATA_RAW_DIR,
    page_size:  int  = 1000,
) -> Path:
    """
    Download all CEC transmission line records to a single CSV.

    Attributes, plus extracted start/end endpoint coordinates and the full
    polyline geometry as JSON, are written to cec_transmission_lines.csv.

    Endpoint coordinates (lon_start/lat_start, lon_end/lat_end) identify the
    terminal nodes of each segment — in the CEC dataset these correspond to
    substation or junction locations and can be used as an independent
    substation coordinate source.

    Returns
    -------
    Path
        Path to the written CSV file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    client = CECTransmissionClient()
    total  = client.get_record_count(LAYER_ID)
    print(f"\nScraping CEC TransmissionLine_CEC (layer {LAYER_ID})")
    print(f"  Records   : {total:,}")
    print(f"  Output    : {output_dir}\n")

    rows_out: list[dict] = []

    for rows, _ in client.paginate_layer(
        LAYER_ID,
        out_fields=_ATTR_FIELDS,
        include_geometry=True,
        page_size=page_size,
    ):
        for row in rows:
            geom_json = row.pop("geometry", None) or ""
            lon_s, lat_s, lon_e, lat_e = _extract_endpoints(geom_json)
            rows_out.append({
                **{k: row.get(k, "") for k in _OUT_COLS
                   if k not in ("lon_start", "lat_start", "lon_end", "lat_end", "geometry_json")},
                "lon_start":    lon_s,
                "lat_start":    lat_s,
                "lon_end":      lon_e,
                "lat_end":      lat_e,
                "geometry_json": geom_json,
            })
        print(f"  {len(rows_out):,}/{total:,}", end="\r")

    out_path = output_dir / "cec_transmission_lines.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_OUT_COLS)
        writer.writeheader()
        writer.writerows(rows_out)

    mb = out_path.stat().st_size / 1024 / 1024
    print(f"\n  {len(rows_out):,} records -> {out_path}  ({mb:.1f} MB)")
    return out_path
