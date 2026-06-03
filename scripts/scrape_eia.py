"""
CLI to scrape EIA electricity data into data/raw/.

Subcommands
-----------
rto-region
    Hourly demand, net generation, interchange, and demand forecast
    by balancing authority. Most relevant for California disaggregation.

rto-interchange
    Hourly electricity flows between pairs of balancing authorities.
    Columns: period, fromba, fromba-name, toba, toba-name, value (MW).

Usage examples
--------------
# CAISO data for all of 2023 (default respondent)
python scripts/scrape_eia.py rto-region --start 2023-01-01 --end 2023-12-31

# All interchange flows, full history
python scripts/scrape_eia.py rto-interchange --start 2020-01-01 --end 2024-12-31

# Only flows originating from CAISO
python scripts/scrape_eia.py rto-interchange \\
    --start 2020-01-01 --end 2024-12-31 \\
    --from-bas CALI

# Flows between CALI and PACE in both directions
python scripts/scrape_eia.py rto-interchange \\
    --start 2020-01-01 --end 2024-12-31 \\
    --from-bas CALI PACE --to-bas PACE CALI

EIA API key
-----------
Store your key in a .env file at the repo root:
    EIA_API_KEY=your_key_here

Or pass it explicitly with --api-key. Free keys at https://www.eia.gov/opendata/

Output naming convention
------------------------
    {prefix}_{YYYYMMDD_start}_{YYYYMMDD_end}_part{NNN}.csv

e.g.  eia_rto-region-data_CALI_20230101_20231231_part001.csv
      eia_rto-interchange-data_ALL_20230101_20231231_part001.csv
      eia_rto-interchange-data_from-CALI_20230101_20231231_part001.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python scripts/scrape_eia.py` from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.eia.eia_scraper import DATA_RAW_DIR, scrape_rto_interchange_data, scrape_rto_region_data


# ── Subcommand handlers ───────────────────────────────────────────────────────

def cmd_rto_region(args: argparse.Namespace) -> None:
    respondents = None if args.respondents == ["ALL"] else args.respondents

    files = scrape_rto_region_data(
        start=args.start,
        end=args.end,
        respondents=respondents,
        output_dir=Path(args.output_dir),
        filename_prefix=args.filename_prefix,
        max_file_mb=args.max_file_mb,
        api_key=args.api_key,
        page_size=args.page_size,
    )

    print("\nFiles written:")
    for f in files:
        mb = f.stat().st_size / 1024 / 1024
        print(f"  {f}  ({mb:.1f} MB)")


def cmd_rto_interchange(args: argparse.Namespace) -> None:
    from_bas = None if args.from_bas == ["ALL"] else args.from_bas
    to_bas = None if args.to_bas == ["ALL"] else args.to_bas

    files = scrape_rto_interchange_data(
        start=args.start,
        end=args.end,
        from_bas=from_bas,
        to_bas=to_bas,
        output_dir=Path(args.output_dir),
        filename_prefix=args.filename_prefix,
        max_file_mb=args.max_file_mb,
        api_key=args.api_key,
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

    # ── rto-region ────────────────────────────────────────────────────────────
    p = sub.add_parser(
        "rto-region",
        help="Hourly RTO region data: demand, net generation, interchange, demand forecast.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Scrape EIA /electricity/rto/region-data/data/ and write to chunked CSVs.\n\n"
            "Common CA-region balancing authority codes:\n"
            "  CALI  California ISO (CAISO)\n"
            "  PACW  Pacific Gas & Electric / NV Energy (West)\n"
            "  PACE  PacifiCorp East\n"
            "  NEVP  Nevada Power\n"
            "  WALC  Western Area Power Administration - Lower Colorado"
        ),
    )
    p.add_argument(
        "--start",
        default=None,
        metavar="YYYY-MM-DD",
        help="Start date, inclusive. Omit to fetch from the earliest available record.",
    )
    p.add_argument(
        "--end",
        default=None,
        metavar="YYYY-MM-DD",
        help="End date, inclusive. Omit to fetch through the most recent available record.",
    )
    p.add_argument(
        "--respondents",
        nargs="+",
        default=["CALI"],
        metavar="CODE",
        help=(
            "Balancing authority code(s). Default: CALI. "
            "Pass ALL to fetch every region with no filter."
        ),
    )
    p.add_argument(
        "--output-dir",
        default=str(DATA_RAW_DIR),
        metavar="PATH",
        help=f"Output directory. Default: {DATA_RAW_DIR}",
    )
    p.add_argument(
        "--filename-prefix",
        default=None,
        metavar="PREFIX",
        help=(
            "Override the auto-generated filename prefix. "
            "Auto format: eia_rto-region-data_<respondents>. "
            "Full filename: <prefix>_<start>_<end>_part001.csv"
        ),
    )
    p.add_argument(
        "--max-file-mb",
        type=float,
        default=100.0,
        metavar="MB",
        help="Rotate to a new chunk file when current CSV reaches this size. Default: 100",
    )
    p.add_argument(
        "--api-key",
        default=None,
        metavar="KEY",
        help="EIA API key. Falls back to EIA_API_KEY in .env",
    )
    p.add_argument(
        "--page-size",
        type=int,
        default=5000,
        metavar="N",
        help="Rows per API request. EIA max is 5000. Default: 5000",
    )
    p.set_defaults(func=cmd_rto_region)

    # ── rto-interchange ───────────────────────────────────────────────────────
    p2 = sub.add_parser(
        "rto-interchange",
        help="Hourly electricity flows between pairs of balancing authorities.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Scrape EIA /electricity/rto/interchange-data/data/ and write to chunked CSVs.\n\n"
            "Columns: period, fromba, fromba-name, toba, toba-name, value (MW), value-units.\n\n"
            "Common balancing authority codes:\n"
            "  CALI  California ISO (CAISO)\n"
            "  PACW  PacifiCorp West\n"
            "  PACE  PacifiCorp East\n"
            "  NEVP  Nevada Power\n"
            "  WALC  Western Area Power Administration - Lower Colorado\n"
            "  BPAT  Bonneville Power Administration\n"
            "  AZPS  Arizona Public Service"
        ),
    )
    p2.add_argument(
        "--start",
        default=None,
        metavar="YYYY-MM-DD",
        help="Start date, inclusive. Omit to fetch from the earliest available record.",
    )
    p2.add_argument(
        "--end",
        default=None,
        metavar="YYYY-MM-DD",
        help="End date, inclusive. Omit to fetch through the most recent available record.",
    )
    p2.add_argument(
        "--from-bas",
        nargs="+",
        default=["ALL"],
        metavar="CODE",
        help="Originating balancing authority code(s). Default: ALL (no filter).",
    )
    p2.add_argument(
        "--to-bas",
        nargs="+",
        default=["ALL"],
        metavar="CODE",
        help="Destination balancing authority code(s). Default: ALL (no filter).",
    )
    p2.add_argument(
        "--output-dir",
        default=str(DATA_RAW_DIR),
        metavar="PATH",
        help=f"Output directory. Default: {DATA_RAW_DIR}",
    )
    p2.add_argument(
        "--filename-prefix",
        default=None,
        metavar="PREFIX",
        help="Override the auto-generated filename prefix.",
    )
    p2.add_argument(
        "--max-file-mb",
        type=float,
        default=100.0,
        metavar="MB",
        help="Rotate to a new chunk file when current CSV reaches this size. Default: 100",
    )
    p2.add_argument("--api-key", default=None, metavar="KEY", help="EIA API key. Falls back to EIA_API_KEY in .env")
    p2.add_argument(
        "--page-size",
        type=int,
        default=5000,
        metavar="N",
        help="Rows per API request. EIA max is 5000. Default: 5000",
    )
    p2.set_defaults(func=cmd_rto_interchange)

    return parser


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
