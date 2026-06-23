"""
Download EIA Form 861 ZIP files into data/raw/eia/form861/{year}/.

Only Sales_Ult_Cust_{year}.xlsx is retained from each ZIP; all other
worksheet files are discarded to keep the raw folder small.

After downloading, run scripts/process_eia861.py to compute CA fractions.
Alternatively, use scripts/ingest_eia861_pudl.py to fetch the same data
from PUDL's public S3 bucket (no download of large ZIP files needed).

Usage
-----
  python scripts/data/eia/scrape_eia_form861.py                   # 2020-2024
  python scripts/data/eia/scrape_eia_form861.py --years 2022 2023 2024
  python scripts/data/eia/scrape_eia_form861.py --overwrite        # re-download existing
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.data.eia.form861 import RAW_DIR, download

_DEFAULT_YEARS = list(range(2020, 2025))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--years", nargs="+", type=int, default=_DEFAULT_YEARS, metavar="YEAR",
        help=f"Year(s) to download. Default: {_DEFAULT_YEARS[0]}-{_DEFAULT_YEARS[-1]}",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Re-download even if data already exists locally.",
    )
    args = parser.parse_args()

    print("=== EIA Form 861 download ===")
    print(f"Output: {RAW_DIR}\n")

    for year in sorted(args.years):
        print(f"\n=== Year {year} ===")
        try:
            path = download(year, overwrite=args.overwrite)
            print(f"  Ready: {path.relative_to(Path(__file__).resolve().parents[3])}")
        except RuntimeError as e:
            print(f"  WARNING: {e}")

    print("\nDone. Run scripts/process_eia861.py to compute CA fractions by BA.")


if __name__ == "__main__":
    main()
