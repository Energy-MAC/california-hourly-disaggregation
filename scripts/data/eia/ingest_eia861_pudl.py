"""
Download EIA-861 yearly sales data from the PUDL public S3 bucket.

Output: data/raw/eia/pudl/core_eia861__yearly_sales_CA8.parquet
  Filtered to the 8 CA-relevant BAs.  No API key or AWS credentials required.
  Predicate pushdown ensures only CA8 rows are transferred (not the full US
  dataset), keeping the download fast and the file small.

After downloading, run scripts/process_eia861.py to compute CA fractions.

Dependencies: pip install pandas pyarrow s3fs

Usage
-----
  python scripts/data/eia/ingest_eia861_pudl.py
  python scripts/data/eia/ingest_eia861_pudl.py --bas BANC CISO IID LDWP TIDC WALC NEVP PACW
  python scripts/data/eia/ingest_eia861_pudl.py --output-dir data/raw/eia/pudl
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.data.eia.pudl_eia861 import CA8, RAW_DIR, ingest_sales


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--bas", nargs="+", default=CA8, metavar="CODE",
        help=f"BA codes to keep. Default: {' '.join(CA8)}",
    )
    parser.add_argument(
        "--output-dir", default=str(RAW_DIR), metavar="PATH",
        help=f"Output directory. Default: {RAW_DIR}",
    )
    args = parser.parse_args()

    print("=== EIA-861 PUDL ingest ===")
    print(f"BAs   : {args.bas}")
    print(f"Output: {args.output_dir}\n")

    ingest_sales(output_dir=Path(args.output_dir), bas=args.bas)


if __name__ == "__main__":
    main()
