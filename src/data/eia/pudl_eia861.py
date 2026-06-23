"""
PUDL-based ingester for EIA-861 annual electricity sales data.

Source: Public PUDL nightly build on AWS S3 (no API key required)
  s3://pudl.catalyst.coop/nightly/

Table downloaded and filtered to CA-relevant BAs:
  core_eia861__yearly_sales.parquet
    Annual retail electricity sales by utility, state, and balancing
    authority.  PUDL normalises EIA Form 861's Sales_Ult_Cust worksheet
    into a long-format table with one row per
    (year, utility, state, ba_code, customer_class).

Output directory: data/raw/eia/pudl/
  core_eia861__yearly_sales_CA8.parquet   (filtered to CA8 BAs)

Dependencies: pip install pandas pyarrow s3fs
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.data.eia.pudl_eia930 import CA8

ROOT    = Path(__file__).resolve().parents[3]
RAW_DIR = ROOT / "data" / "raw" / "eia" / "pudl"

_BASE      = "s3://pudl.catalyst.coop/nightly"
_SALES_URL = f"{_BASE}/core_eia861__yearly_sales.parquet"
_STORAGE   = {"anon": True}
_BA_COL    = "balancing_authority_code_eia"


def ingest_sales(
    output_dir: Path = RAW_DIR,
    bas: list[str] = CA8,
) -> Path:
    """
    Download EIA-861 yearly sales from PUDL S3, filter to given BAs, and
    write a parquet file to output_dir.

    Predicate pushdown means only matching rows are transferred, keeping the
    download proportional to the BA subset rather than the full US dataset.

    Returns the path to the written parquet file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching EIA-861 yearly sales (PUDL nightly) ...")
    print(f"  source : {_SALES_URL}")
    print(f"  filter : {_BA_COL} in {bas}\n")

    filters = [(_BA_COL, "in", bas)]
    df = pd.read_parquet(
        _SALES_URL,
        dtype_backend="pyarrow",
        filters=filters,
        storage_options=_STORAGE,
    )

    print(f"  {len(df):,} rows, {len(df.columns)} columns")
    print(f"  columns: {list(df.columns)}")

    date_col = next((c for c in ("report_date", "year") if c in df.columns), None)
    if date_col:
        dates = pd.to_datetime(df[date_col])
        print(f"  period : {dates.min().year} -> {dates.max().year}")

    print("\nRows per BA:")
    for ba, n in df[_BA_COL].value_counts().sort_index().items():
        print(f"  {ba:6s}  {n:,}")

    out_path = output_dir / "core_eia861__yearly_sales_CA8.parquet"
    df.to_parquet(out_path, index=False)
    mb = out_path.stat().st_size / 1024 / 1024
    print(f"\nWrote -> {out_path.relative_to(ROOT)}  ({mb:.1f} MB)")
    return out_path
