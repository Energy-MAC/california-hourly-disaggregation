"""
CLI to scrape SDG&E electricity data into data/raw/sdge/.

Source: SDG&E ArcGIS Organization (no authentication required for public services)
  https://icm-api-explorer.sdge.com/

Subcommands
-----------
substation-profiles
    Download hourly load profiles for every SDG&E substation from the interactive
    map download API (ZIP per substation, ~576 data points each).
    Output: sdge_substation_profiles_part*.csv, sdge_substation_profiles_failed.csv
    Press Ctrl+C to stop safely; re-run to resume.

attributes
    Scrape physical and DER attributes from the ICA_MAP_PROD_Substations_VW
    FeatureServer (layer 0): substation type, voltage, existing/queued/total
    generation, projected load, DER penetration.
    Output: sdge_substation_attributes.csv

discover
    Explore available FeatureServer services and their layers.
    Useful for finding additional data; not needed for the standard pipeline.

layer
    Scrape arbitrary features from any SDG&E FeatureServer service/layer.
    Useful for ad-hoc exploration.

Standard pipeline commands
--------------------------
# Scrape load profiles (can take a while; Ctrl+C to pause, re-run to resume)
python scripts/scrape_sdge.py substation-profiles

# Scrape substation attributes (voltage, generation capacity, etc.)
python scripts/scrape_sdge.py attributes

Discovery commands (exploration only)
--------------------------------------
python scripts/scrape_sdge.py discover
python scripts/scrape_sdge.py discover --service ICA_MAP_PROD_Substations_VW
python scripts/scrape_sdge.py discover --service ICA_MAP_PROD_Substations_VW --layer-id 0

Output naming convention
------------------------
    sdge_substation_profiles_part{NNN}.csv
    sdge_{SERVICE_NAME}_layer{N}_earliest_latest_part{NNN}.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.sdge.sdge_scraper import (
    DATA_RAW_DIR,
    discover_layer,
    discover_service,
    discover_services,
    get_substation_names,
    scrape_layer,
    scrape_substation_attributes,
    scrape_substation_load_profiles,
)


# ── Subcommand handlers ───────────────────────────────────────────────────────

def cmd_discover(args: argparse.Namespace) -> None:
    if args.service and args.layer_id is not None:
        discover_layer(args.service, args.layer_id)
    elif args.service:
        discover_service(args.service)
    else:
        discover_services()


def cmd_attributes(args: argparse.Namespace) -> None:
    out = scrape_substation_attributes(
        service_name=args.service,
        output_dir=Path(args.output_dir),
    )
    mb = out.stat().st_size / 1024 / 1024
    print(f"Output: {out}  ({mb:.1f} MB)")


def cmd_substation_profiles(args: argparse.Namespace) -> None:
    names = args.substation if args.substation else None
    files = scrape_substation_load_profiles(
        service_name=args.service,
        substation_names=names,
        add_substation_coords=not args.no_coords,
        output_dir=Path(args.output_dir),
        filename_prefix=args.filename_prefix,
        max_file_mb=args.max_file_mb,
    )
    print("\nFiles written:")
    for f in files:
        mb = f.stat().st_size / 1024 / 1024
        print(f"  {f}  ({mb:.1f} MB)")


def cmd_layer(args: argparse.Namespace) -> None:
    files = scrape_layer(
        service_name=args.service,
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
        help="List services, layers, or fields in the SDG&E ArcGIS org.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Without flags: list all available FeatureServer services.\n"
            "With --service: list layers inside that service.\n"
            "With --service and --layer-id: show field metadata for that layer."
        ),
    )
    p.add_argument("--service", default=None, metavar="NAME",
                   help="FeatureServer service name to inspect.")
    p.add_argument("--layer-id", type=int, default=None, metavar="N",
                   help="Layer ID within the service to inspect fields for.")
    p.set_defaults(func=cmd_discover)

    # ── layer ─────────────────────────────────────────────────────────────────
    p2 = sub.add_parser(
        "layer",
        help="Scrape all features from a service/layer to chunked CSVs.",
    )
    p2.add_argument("--service", required=True, metavar="NAME",
                    help="FeatureServer service name (from 'discover' output).")
    p2.add_argument("--layer-id", type=int, required=True, metavar="N",
                    help="Layer ID within the service.")
    p2.add_argument("--where", default="1=1", metavar="SQL",
                    help="SQL WHERE filter. Default: 1=1 (all records).")
    p2.add_argument("--out-fields", default="*", metavar="FIELDS",
                    help="Comma-separated field names, or * for all. Default: *")
    p2.add_argument("--order-by", default="OBJECTID", metavar="FIELD",
                    help="Sort field for stable pagination. Default: OBJECTID")
    p2.add_argument("--no-geometry", action="store_true",
                    help="Omit geometry columns from the output CSV.")
    p2.add_argument("--output-dir", default=str(DATA_RAW_DIR), metavar="PATH",
                    help=f"Output directory. Default: {DATA_RAW_DIR}")
    p2.add_argument("--filename-prefix", default=None, metavar="PREFIX",
                    help="Override the auto-generated filename prefix.")
    p2.add_argument("--max-file-mb", type=float, default=100.0, metavar="MB",
                    help="Rotate to a new chunk file at this size (MB). Default: 100")
    p2.add_argument("--page-size", type=int, default=1000, metavar="N",
                    help="Records per API request. Default: 1000")
    p2.set_defaults(func=cmd_layer)

    # ── attributes ───────────────────────────────────────────────────────────
    pa = sub.add_parser(
        "attributes",
        help="Scrape physical/DER attributes for all SDG&E substations (voltage, gen, load, penetration).",
    )
    pa.add_argument(
        "--service",
        default="ICA_MAP_PROD_Substations_VW",
        metavar="NAME",
        help="FeatureServer service to query. Default: ICA_MAP_PROD_Substations_VW",
    )
    pa.add_argument(
        "--output-dir",
        default=str(DATA_RAW_DIR),
        metavar="PATH",
        help=f"Output directory. Default: {DATA_RAW_DIR}",
    )
    pa.set_defaults(func=cmd_attributes)

    # ── substation-profiles ───────────────────────────────────────────────────
    p3 = sub.add_parser(
        "substation-profiles",
        help="Download hourly load profiles for all SDG&E substations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Downloads a ZIP per substation from the SDG&E interactive map API,\n"
            "extracts the CSV inside, and concatenates everything into chunked output files.\n\n"
            "Each substation has 24 rows (12 months × High/Low Load) × 24 hourly columns\n"
            "= 576 data points.  Substation names are fetched from the FeatureServer\n"
            "automatically unless --substation is provided.\n\n"
            "Press Ctrl+C to stop safely; re-run the same command to resume."
        ),
    )
    p3.add_argument(
        "--service",
        default="ICA_MAP_PROD_Substations_VW",
        metavar="NAME",
        help="FeatureServer service to fetch substation names from. Default: ICA_MAP_PROD_Substations_VW",
    )
    p3.add_argument(
        "--substation",
        nargs="+",
        default=None,
        metavar="NAME",
        help="Specific substation name(s) to download. Omit to download all.",
    )
    p3.add_argument(
        "--output-dir",
        default=str(DATA_RAW_DIR),
        metavar="PATH",
        help=f"Output directory. Default: {DATA_RAW_DIR}",
    )
    p3.add_argument(
        "--filename-prefix",
        default="sdge_substation_profiles",
        metavar="PREFIX",
        help="Output filename prefix. Default: sdge_substation_profiles",
    )
    p3.add_argument(
        "--max-file-mb",
        type=float,
        default=100.0,
        metavar="MB",
        help="Rotate to a new chunk file at this size (MB). Default: 100",
    )
    p3.add_argument(
        "--no-coords",
        action="store_true",
        help="Skip the coordinate lookup (no longitude/latitude columns in output).",
    )
    p3.set_defaults(func=cmd_substation_profiles)

    return parser


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
