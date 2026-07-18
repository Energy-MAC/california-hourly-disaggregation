"""
scrape_pge_feeders.py

Download PG&E distribution feeder data from the DRP Compliance ArcGIS FeatureServer.

Source
------
  https://services2.arcgis.com/mJaJSax0KPHoCNB6/ArcGIS/rest/services/DRPComplianceRelProd/FeatureServer

Layers
------
  2   FeederDetail        Polyline — feeder geometry + customer/DG attributes (3,032 records)
  23  FeederLoadProfile   Table — monthly-hourly min/max kW load per feeder (~637k records)

Outputs
-------
  data/raw/pge/feeders/pge_feeder_detail.csv
  data/raw/pge/feeders/pge_feeder_load_profiles.csv

Usage
-----
  python scripts/data/pge/scrape_pge_feeders.py detail
  python scripts/data/pge/scrape_pge_feeders.py profiles
  python scripts/data/pge/scrape_pge_feeders.py all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.data.pge.pge_scraper import (
    _FEEDERS_DIR,
    scrape_feeder_detail,
    scrape_feeder_load_profiles,
)


def cmd_detail(args: argparse.Namespace) -> None:
    out = scrape_feeder_detail(output_dir=Path(args.output_dir))
    print(f"Output: {out}  ({out.stat().st_size/1024/1024:.1f} MB)")


def cmd_profiles(args: argparse.Namespace) -> None:
    out = scrape_feeder_load_profiles(output_dir=Path(args.output_dir))
    print(f"Output: {out}  ({out.stat().st_size/1024/1024:.1f} MB)")


def cmd_all(args: argparse.Namespace) -> None:
    cmd_detail(args)
    cmd_profiles(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    _dir_kwargs = dict(
        default=str(_FEEDERS_DIR),
        metavar="PATH",
        help=f"Output directory. Default: {_FEEDERS_DIR}",
    )

    p = sub.add_parser("detail",   help="Scrape FeederDetail (layer 2) — geometry + attributes.")
    p.add_argument("--output-dir", **_dir_kwargs)
    p.set_defaults(func=cmd_detail)

    p = sub.add_parser("profiles", help="Scrape FeederLoadProfile (layer 23) — monthly-hourly kW.")
    p.add_argument("--output-dir", **_dir_kwargs)
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("all",      help="Scrape both layers.")
    p.add_argument("--output-dir", **_dir_kwargs)
    p.set_defaults(func=cmd_all)

    return parser


def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
