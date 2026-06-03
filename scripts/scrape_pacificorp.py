"""
CLI to scrape PacifiCorp electricity data into data/raw/pacificorp/.

Source: PacifiCorp Transmission and Distribution Public ArcGIS FeatureServer
  https://services1.arcgis.com/ePo6UhbBpZFy1wO2/arcgis/rest/services/Transmission_and_Distribution_Public/FeatureServer
  No authentication required.

Available layers
----------------
  0  Poles
  1  Substations     ← Name, Sub_Type, longitude, latitude
  3  Transmission Lines
  4  Distribution Lines
  5  Service Territory

Subcommands
-----------
discover
    List all layers in the PacifiCorp FeatureServer.
    --layer-id N    Show field definitions and record count for a layer.

layer
    Scrape all features from a layer to chunked CSVs.
    Coordinates (longitude/latitude) are included automatically.
    Press Ctrl+C to stop safely; re-run the same command to resume.

Usage examples
--------------
# Inspect the Substations layer (layer 1)
python scripts/scrape_pacificorp.py discover --layer-id 1

# Scrape all substation names and coordinates
python scripts/scrape_pacificorp.py layer --layer-id 1

Output naming convention
------------------------
    pacificorp_layer{N}_earliest_latest_part{NNN}.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.pacificorp.pacificorp_scraper import (
    DATA_RAW_DIR,
    discover_layer,
    discover_service,
    scrape_layer,
    scrape_substation_attributes,
)


# ── Subcommand handlers ───────────────────────────────────────────────────────

def cmd_discover(args: argparse.Namespace) -> None:
    if args.layer_id is not None:
        discover_layer(args.layer_id)
    else:
        discover_service()


def cmd_attributes(args: argparse.Namespace) -> None:
    out = scrape_substation_attributes(output_dir=Path(args.output_dir))
    mb = out.stat().st_size / 1024 / 1024
    print(f"Output: {out}  ({mb:.1f} MB)")


def cmd_layer(args: argparse.Namespace) -> None:
    files = scrape_layer(
        layer_id=args.layer_id,
        where=args.where,
        out_fields=args.out_fields,
        order_by=args.order_by,
        include_geometry=not args.no_geometry,
        output_dir=Path(args.output_dir),
        filename_prefix=args.filename_prefix,
        max_file_mb=args.max_file_mb,
        page_size=args.page_size,
    )
    print("\nFiles written:")
    for f in files:
        mb = f.stat().st_size / 1024 / 1024
        print(f"  {f}  ({mb:.1f} MB)")


# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── discover ──────────────────────────────────────────────────────────────
    p = sub.add_parser(
        "discover",
        help="List layers (and optionally fields) in the PacifiCorp FeatureServer.",
    )
    p.add_argument("--layer-id", type=int, default=None, metavar="N",
                   help="Show field metadata and record count for this layer ID.")
    p.set_defaults(func=cmd_discover)

    # ── layer ─────────────────────────────────────────────────────────────────
    p2 = sub.add_parser(
        "layer",
        help="Scrape all features from a FeatureServer layer to chunked CSVs.",
    )
    p2.add_argument("--layer-id", type=int, required=True, metavar="N",
                    help="Layer ID to scrape. Layer 1 = Substations.")
    p2.add_argument("--where", default="1=1", metavar="SQL",
                    help="SQL WHERE filter. Default: 1=1 (all records).")
    p2.add_argument("--out-fields", default="*", metavar="FIELDS",
                    help="Comma-separated field names, or * for all. Default: *")
    p2.add_argument("--order-by", default="OBJECTID", metavar="FIELD",
                    help="Sort field for stable pagination. Default: OBJECTID")
    p2.add_argument("--no-geometry", action="store_true",
                    help="Omit longitude/latitude columns from the output CSV.")
    p2.add_argument("--output-dir", default=str(DATA_RAW_DIR), metavar="PATH",
                    help=f"Output directory. Default: {DATA_RAW_DIR}")
    p2.add_argument("--filename-prefix", default=None, metavar="PREFIX",
                    help="Override the auto-generated filename prefix.")
    p2.add_argument("--max-file-mb", type=float, default=100.0, metavar="MB",
                    help="Rotate to a new chunk file at this size (MB). Default: 100")
    p2.add_argument("--page-size", type=int, default=2000, metavar="N",
                    help="Records per API request. PacifiCorp max is 2000. Default: 2000")
    p2.set_defaults(func=cmd_layer)

    # ── attributes ───────────────────────────────────────────────────────────
    pa = sub.add_parser(
        "attributes",
        help="Scrape DER and load attributes from the DG Readiness FeatureServer (circuit-level, aggregated to substation).",
    )
    pa.add_argument("--output-dir", default=str(DATA_RAW_DIR), metavar="PATH",
                    help=f"Output directory. Default: {DATA_RAW_DIR}")
    pa.set_defaults(func=cmd_attributes)

    return parser


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
