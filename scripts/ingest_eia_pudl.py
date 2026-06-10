"""
Download EIA-930 data from the PUDL public S3 bucket into data/raw/eia/pudl/.

Two parquet files are written (both filtered to CA-relevant BAs):
  core_eia930__hourly_operations_CA8.parquet
  core_eia930__hourly_interchange_CA8.parquet  (skipped if not available on PUDL)

No API key or AWS credentials required.
Dependencies: pip install pandas pyarrow s3fs

Usage
-----
python scripts/ingest_eia_pudl.py
python scripts/ingest_eia_pudl.py --ops-only
python scripts/ingest_eia_pudl.py --bas BANC CISO LDWP
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.eia.pudl_eia930 import CA8, RAW_DIR, ingest_interchange, ingest_operations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--bas",
        nargs="+",
        default=CA8,
        metavar="CODE",
        help=f"Balancing authority codes to keep. Default: {' '.join(CA8)}",
    )
    parser.add_argument(
        "--output-dir",
        default=str(RAW_DIR),
        metavar="PATH",
        help=f"Output directory. Default: {RAW_DIR}",
    )
    parser.add_argument(
        "--ops-only",
        action="store_true",
        help="Download only the operations table (skip interchange).",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    out = Path(args.output_dir)

    print("=== EIA-930 PUDL ingest ===")
    print(f"BAs   : {args.bas}")
    print(f"Output: {out}\n")

    ingest_operations(output_dir=out, bas=args.bas)

    if not args.ops_only:
        print()
        ingest_interchange(output_dir=out, bas=args.bas)


if __name__ == "__main__":
    main()
