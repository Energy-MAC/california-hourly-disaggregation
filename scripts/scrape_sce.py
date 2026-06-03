"""
CLI to scrape SCE electricity data into data/raw/sce/.

Source: SCE ICA Tables ArcGIS FeatureServer (no authentication required)
  https://drpep-sce2.opendata.arcgis.com/datasets/SCE2::ica-tables-1/about

Available layers
----------------
  0  DRPEP_FGDB_DATE_TIME          Metadata — data extraction date/time
  1  Circuit Load Profile           Hourly circuit-level load (min/max metrics)
  2  Substation Load Profile        Substation-level load by month/year
  3  ICA Single Consolidated Table  Combined ICA dataset

Subcommands
-----------
discover
    List all layers in the SCE FeatureServer.
    Add --layer-id N to show field definitions and record count for a layer.

layer
    Scrape all features from a specific layer to chunked CSVs.
    Press Ctrl+C to stop safely; re-run the same command to resume.

Usage examples
--------------
# List all available layers
python scripts/scrape_sce.py discover

# Inspect fields for the Substation Load Profile (layer 2)
python scripts/scrape_sce.py discover --layer-id 2

# Scrape all substation load records
python scripts/scrape_sce.py layer --layer-id 2

# Scrape all records, 50 MB file cap, no geometry columns
python scripts/scrape_sce.py layer --layer-id 2 --max-file-mb 50 --no-geometry

Output naming convention
------------------------
    sce_layer{N}_earliest_latest_part{NNN}.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.sce.sce_scraper import (
    DATA_RAW_DIR,
    convert_layer2_to_mw,
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
    print(f"\nOutput: {out}  ({mb:.1f} MB)")


def cmd_convert_to_mw(args: argparse.Namespace) -> None:
    voltage_csv = Path(args.voltage_csv) if args.voltage_csv else None
    out = convert_layer2_to_mw(voltage_csv=voltage_csv, output_dir=Path(args.output_dir))
    mb = out.stat().st_size / 1024 / 1024
    print(f"\nOutput: {out}  ({mb:.1f} MB)")


def cmd_layer(args: argparse.Namespace) -> None:
    files = scrape_layer(
        layer_id=args.layer_id,
        where=args.where,
        out_fields=args.out_fields,
        order_by=args.order_by,
        include_geometry=not args.no_geometry,
        add_substation_coords=args.add_coords,
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

    # ── attributes ────────────────────────────────────────────────────────────
    pv = sub.add_parser(
        "attributes",
        help="Scrape per-substation physical/DER attributes from ICA_Tables Table 3 (voltage, gen, load, customer mix).",
    )
    pv.add_argument("--output-dir", default=str(DATA_RAW_DIR), metavar="PATH",
                    help=f"Output directory. Default: {DATA_RAW_DIR}")
    pv.set_defaults(func=cmd_attributes)

    # ── convert-to-mw [DEPRECATED] ───────────────────────────────────────────
    pm = sub.add_parser(
        "convert-to-mw",
        help="[DEPRECATED] Convert layer2 Amps to MW. Produced inaccurate results; use DRPEP bulk download instead.",
    )
    pm.add_argument("--voltage-csv", default=None, metavar="PATH",
                    help="Path to sce_substation_voltages.csv. Auto-built if absent.")
    pm.add_argument("--output-dir", default=str(DATA_RAW_DIR), metavar="PATH",
                    help=f"Directory containing layer2 CSVs and voltage CSV. Default: {DATA_RAW_DIR}")
    pm.set_defaults(func=cmd_convert_to_mw)

    # ── discover ──────────────────────────────────────────────────────────────
    p = sub.add_parser(
        "discover",
        help="List layers (and optionally fields) in the SCE FeatureServer.",
    )
    p.add_argument(
        "--layer-id",
        type=int,
        default=None,
        metavar="N",
        help="Show field metadata and record count for this layer ID.",
    )
    p.set_defaults(func=cmd_discover)

    # ── layer ─────────────────────────────────────────────────────────────────
    p2 = sub.add_parser(
        "layer",
        help="Scrape all features from a FeatureServer layer to chunked CSVs.",
    )
    p2.add_argument("--layer-id", type=int, required=True, metavar="N",
                    help="Layer ID to scrape (from 'discover' output).")
    p2.add_argument("--where", default="1=1", metavar="SQL",
                    help="SQL WHERE filter. Default: 1=1 (all records).")
    p2.add_argument("--out-fields", default="*", metavar="FIELDS",
                    help="Comma-separated field names, or * for all. Default: *")
    p2.add_argument("--order-by", default="OBJECTID", metavar="FIELD",
                    help="Sort field for stable pagination. Default: OBJECTID")
    p2.add_argument("--no-geometry", action="store_true",
                    help="Omit geometry columns from the output CSV.")
    p2.add_argument("--add-coords", action="store_true",
                    help="Join longitude/latitude from ICA_Layer/Substations. Use with --layer-id 2.")
    p2.add_argument("--output-dir", default=str(DATA_RAW_DIR), metavar="PATH",
                    help=f"Output directory. Default: {DATA_RAW_DIR}")
    p2.add_argument("--filename-prefix", default=None, metavar="PREFIX",
                    help="Override the auto-generated filename prefix.")
    p2.add_argument("--max-file-mb", type=float, default=100.0, metavar="MB",
                    help="Rotate to a new chunk file at this size (MB). Default: 100")
    p2.add_argument("--page-size", type=int, default=1000, metavar="N",
                    help="Records per API request. Default: 1000")
    p2.set_defaults(func=cmd_layer)

    return parser


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
