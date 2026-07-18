"""
Scrape the CEC California Electric Transmission Lines dataset.

Source: California Energy Commission ArcGIS FeatureServer
  https://services3.arcgis.com/bWPjFyq029ChCGur/arcgis/rest/services/Transmission_Line/FeatureServer/2

This dataset contains 6,839 transmission line segments (polylines) covering
California.  Each segment carries voltage class, owner, status, circuit type,
and length.  The scraper also extracts endpoint coordinates (lon_start/lat_start,
lon_end/lat_end) from each polyline — these identify substation and junction
node positions and can be used as a coordinate source independent of utility
ICA data or DataBasin.

Subcommands
-----------
scrape
    Download all 6,839 records to data/raw/cec_transmission/cec_transmission_lines.csv.
    Includes attributes + endpoint coords + full geometry JSON.

discover
    List all layers in the FeatureServer (currently only layer 2).

discover --layer-id 2
    Show field metadata and record count for layer 2.

Usage
-----
  python scripts/data/cec_transmission/scrape_cec_transmission.py scrape
  python scripts/data/cec_transmission/scrape_cec_transmission.py discover
  python scripts/data/cec_transmission/scrape_cec_transmission.py discover --layer-id 2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.data.cec_transmission.cec_transmission_scraper import (
    DATA_RAW_DIR,
    LAYER_ID,
    discover_layer,
    discover_service,
    scrape_transmission_lines,
)


def cmd_discover(args: argparse.Namespace) -> None:
    if args.layer_id is not None:
        discover_layer(args.layer_id)
    else:
        discover_service()


def cmd_scrape(args: argparse.Namespace) -> None:
    out = scrape_transmission_lines(
        output_dir=Path(args.output_dir),
        page_size=args.page_size,
    )
    mb = out.stat().st_size / 1024 / 1024
    print(f"\nOutput: {out}  ({mb:.1f} MB)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── discover ──────────────────────────────────────────────────────────────
    p = sub.add_parser("discover", help="List layers or show field metadata.")
    p.add_argument(
        "--layer-id", type=int, default=None, metavar="N",
        help=f"Show field metadata for this layer (default: service summary). "
             f"The only layer is {LAYER_ID}.",
    )
    p.set_defaults(func=cmd_discover)

    # ── scrape ────────────────────────────────────────────────────────────────
    p2 = sub.add_parser(
        "scrape",
        help="Download all transmission line records to CSV.",
    )
    p2.add_argument(
        "--output-dir", default=str(DATA_RAW_DIR), metavar="PATH",
        help=f"Output directory. Default: {DATA_RAW_DIR}",
    )
    p2.add_argument(
        "--page-size", type=int, default=1000, metavar="N",
        help="Records per API request. Default: 1000",
    )
    p2.set_defaults(func=cmd_scrape)

    return parser


def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
