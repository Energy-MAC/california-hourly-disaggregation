"""
PUDL-based ingester for EIA-930 hourly balancing authority data.

Source: Public PUDL nightly build on AWS S3 (no API key required)
  s3://pudl.catalyst.coop/nightly/

Tables downloaded and filtered to CA-relevant BAs:
  core_eia930__hourly_operations.parquet  — per-BA demand/gen/interchange
  core_eia930__hourly_interchange.parquet — BA-pair interchange flows

Output directory: data/raw/eia/pudl/

Dependencies: pandas, pyarrow, s3fs  (pip install pandas pyarrow s3fs)
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = ROOT / "data" / "raw" / "eia" / "pudl"

CA8 = ["BANC", "CISO", "IID", "LDWP", "PACW", "NEVP", "TIDC", "WALC"]

_BASE = "s3://pudl.catalyst.coop/nightly"
_OPS_URL = f"{_BASE}/core_eia930__hourly_operations.parquet"
_IXC_URL = f"{_BASE}/core_eia930__hourly_interchange.parquet"

# Public PUDL bucket — anonymous S3 access
_STORAGE = {"anon": True}

# Column used to identify balancing authorities in PUDL EIA-930 tables
_BA_COL = "balancing_authority_code_eia"


def ingest_operations(
    output_dir: Path = RAW_DIR,
    bas: list[str] = CA8,
) -> Path:
    """
    Download EIA-930 hourly BA operations from PUDL S3, filter to given BAs,
    and write a parquet file to output_dir.

    Predicate pushdown via pyarrow means only matching rows are transferred,
    keeping download size proportional to the BA subset (not the full US dataset).

    Returns
    -------
    Path
        Path to the written parquet file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching EIA-930 operations (PUDL nightly) ...")
    print(f"  source : {_OPS_URL}")
    print(f"  filter : {_BA_COL} in {bas}\n")

    filters = [(_BA_COL, "in", bas)]
    df = pd.read_parquet(
        _OPS_URL,
        dtype_backend="pyarrow",
        filters=filters,
        storage_options=_STORAGE,
    )

    print(f"  {len(df):,} rows, {len(df.columns)} columns")
    print(f"  columns: {list(df.columns)}\n")

    ts_col = _detect_ts_col(df)
    if ts_col:
        print(f"  period : {df[ts_col].min()} -> {df[ts_col].max()}")

    print("\nRows per BA:")
    for ba, n in df[_BA_COL].value_counts().sort_index().items():
        print(f"  {ba:6s}  {n:,}")

    out_path = output_dir / "core_eia930__hourly_operations_CA8.parquet"
    df.to_parquet(out_path, index=False)
    mb = out_path.stat().st_size / 1024 / 1024
    print(f"\nWrote -> {out_path}  ({mb:.1f} MB)")
    return out_path


def ingest_interchange(
    output_dir: Path = RAW_DIR,
    bas: list[str] = CA8,
) -> Path | None:
    """
    Download EIA-930 hourly BA-pair interchange from PUDL S3, filter to rows
    where the reporting BA is in the given set, and write a parquet file.

    Returns None if the interchange table is not available on PUDL.

    Returns
    -------
    Path | None
        Path to the written parquet file, or None if unavailable.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching EIA-930 interchange (PUDL nightly) ...")
    print(f"  source : {_IXC_URL}")
    print(f"  filter : {_BA_COL} in {bas}\n")

    filters = [(_BA_COL, "in", bas)]
    try:
        df = pd.read_parquet(
            _IXC_URL,
            dtype_backend="pyarrow",
            filters=filters,
            storage_options=_STORAGE,
        )
    except Exception as exc:
        print(f"  WARNING: interchange table not available: {exc}")
        return None

    print(f"  {len(df):,} rows, {len(df.columns)} columns")
    print(f"  columns: {list(df.columns)}\n")

    ts_col = _detect_ts_col(df)
    if ts_col:
        print(f"  period : {df[ts_col].min()} -> {df[ts_col].max()}")

    print("\nRows per reporting BA:")
    for ba, n in df[_BA_COL].value_counts().sort_index().items():
        print(f"  {ba:6s}  {n:,}")

    out_path = output_dir / "core_eia930__hourly_interchange_CA8.parquet"
    df.to_parquet(out_path, index=False)
    mb = out_path.stat().st_size / 1024 / 1024
    print(f"\nWrote -> {out_path}  ({mb:.1f} MB)")
    return out_path


def _detect_ts_col(df: pd.DataFrame) -> str | None:
    """Return the first datetime-like column, to report date range."""
    for col in ("datetime_utc", "datetime", "period", "report_date"):
        if col in df.columns:
            return col
    return None
