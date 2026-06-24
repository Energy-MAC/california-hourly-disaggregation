"""
Exploratory script for the two ReEDS/historic load files in data/raw/reeds/.

Files examined:
  - historic_post2015_load_hourly.h5   (HDF5, ~69 MB)
  - reeds_load_transformed.parquet     (Parquet, ~814 MB)

Run:
    python scripts/explore_potential_data.py

Optional flags:
    --h5-only       skip the parquet (it's large; reading takes a moment)
    --parquet-only  skip the HDF5
    --sample N      number of sample rows to display (default 5)
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

H5_PATH      = ROOT / "data" / "raw" / "reeds" / "historic_post2015_load_hourly.h5"
PARQUET_PATH = ROOT / "data" / "raw" / "reeds" / "reeds_load_transformed.parquet"

SEP = "=" * 72


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _section(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def _dtype_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in df.columns:
        s = df[col]
        row = {"column": col, "dtype": str(s.dtype), "n_null": int(s.isna().sum())}
        if pd.api.types.is_numeric_dtype(s):
            row.update({"min": s.min(), "max": s.max(),
                        "mean": round(s.mean(), 3), "std": round(s.std(), 3)})
        elif pd.api.types.is_datetime64_any_dtype(s):
            row.update({"min": s.min(), "max": s.max(), "mean": "—", "std": "—"})
        else:
            top = s.value_counts().head(3).index.tolist()
            row.update({"min": "—", "max": "—",
                        "mean": f"top3: {top}", "std": "—"})
        rows.append(row)
    return pd.DataFrame(rows).set_index("column")


def _time_col_info(df: pd.DataFrame) -> None:
    """Print date-range info for any column that looks like a timestamp."""
    for col in df.columns:
        s = df[col]
        if pd.api.types.is_datetime64_any_dtype(s):
            print(f"  [{col}] range: {s.min()} -> {s.max()}"
                  f"  |  n_unique={s.nunique()}")
        elif s.dtype == object:
            # try parsing a sample
            sample = s.dropna().head(200)
            try:
                parsed = pd.to_datetime(sample, infer_datetime_format=True, errors="coerce")
                if parsed.notna().sum() > 150:
                    print(f"  [{col}] looks like datetime strings; "
                          f"parsed range: {parsed.min()} -> {parsed.max()}")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# HDF5
# ---------------------------------------------------------------------------

def explore_h5(n_sample: int) -> None:
    _section(f"HDF5: {H5_PATH.name}  ({H5_PATH.stat().st_size / 1e6:.1f} MB)")

    try:
        import h5py
    except ImportError:
        print("  h5py not installed — falling back to pandas.HDFStore")
        h5py = None

    if h5py is None:
        print("  h5py required to read this file — install it with: pip install h5py")
        return

    print("\n--- Raw HDF5 structure (h5py) ---")
    def _walk(name, obj):
        kind = "group" if isinstance(obj, h5py.Group) else "dataset"
        shape = getattr(obj, "shape", "—")
        dtype = getattr(obj, "dtype", "—")
        print(f"  {kind:8s}  {name:<45s}  shape={shape}  dtype={dtype}")
        if isinstance(obj, h5py.Dataset) and obj.attrs:
            for k, v in obj.attrs.items():
                print(f"            attrs[{k}] = {v}")

    with h5py.File(H5_PATH, "r") as f:
        print(f"  Top-level keys: {list(f.keys())}")
        f.visititems(_walk)

        # Reconstruct as DataFrame from raw datasets
        print("\n--- Reconstructing DataFrame from raw datasets ---")
        cols = [c.decode() if isinstance(c, bytes) else c for c in f["columns"][:]]
        idx  = [i.decode().strip() if isinstance(i, bytes) else i for i in f["index_0"][:]]
        idx_name_raw = f["index_names"][0]
        idx_name = idx_name_raw.decode().strip() if isinstance(idx_name_raw, bytes) else str(idx_name_raw)
        data = f["data"][:]  # shape (n_rows, n_cols)

    df = pd.DataFrame(data, columns=cols)
    df.index = pd.Index(idx, name=idx_name)

    # Try to parse index as datetime
    try:
        df.index = pd.to_datetime(df.index)
        print(f"  Index parsed as datetime: {df.index.min()} -> {df.index.max()}")
    except Exception:
        print(f"  Index (raw): {df.index[:3].tolist()} … {df.index[-3:].tolist()}")

    print(f"\n  Shape:   {df.shape}  ({df.shape[0]:,} rows × {df.shape[1]} columns)")
    print(f"  Columns ({len(cols)}): {sorted(cols)}")

    # Summary stats
    print(f"\n--- Numeric describe() ---")
    desc = df.describe().T
    with pd.option_context("display.float_format", "{:.2f}".format,
                           "display.max_rows", 200):
        print(desc.to_string())

    # Check for nulls / zeros
    null_counts = df.isna().sum()
    zero_counts = (df == 0).sum()
    print(f"\n--- Null / zero counts ---")
    print(f"  Total nulls:  {null_counts.sum()}")
    print(f"  Total zeros:  {zero_counts.sum()}")
    if null_counts.any():
        print("  Columns with nulls:")
        print(null_counts[null_counts > 0].to_string())

    print(f"\n--- Sample ({n_sample} rows) ---")
    sample_df = df.sample(min(n_sample, len(df)), random_state=42)
    print(sample_df.iloc[:, :10].to_string())  # first 10 cols to keep output readable
    if len(cols) > 10:
        print(f"  ... ({len(cols) - 10} more columns not shown)")

    print(f"\n--- Head (5 rows, first 10 cols) ---")
    print(df.iloc[:5, :10].to_string())


# ---------------------------------------------------------------------------
# Parquet
# ---------------------------------------------------------------------------

def explore_parquet(n_sample: int) -> None:
    _section(f"Parquet: {PARQUET_PATH.name}  ({PARQUET_PATH.stat().st_size / 1e6:.1f} MB)")

    try:
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(PARQUET_PATH)
        meta = pf.metadata
        schema = pf.schema_arrow

        print(f"\n--- File metadata ---")
        print(f"  Row groups:  {meta.num_row_groups}")
        print(f"  Total rows:  {meta.num_rows:,}")
        print(f"  Columns:     {meta.num_columns}")
        print(f"  Created by:  {meta.created_by}")

        print(f"\n--- Arrow schema ---")
        for i, field in enumerate(schema):
            print(f"  [{i:2d}]  {field.name:<35s}  {field.type}")

        # Row-group stats (min/max per column, first group only)
        print(f"\n--- Row-group 0 column stats (min/max from parquet metadata) ---")
        rg = meta.row_group(0)
        for i in range(rg.num_columns):
            col_meta = rg.column(i)
            stats = col_meta.statistics
            if stats and stats.has_min_max:
                print(f"  {col_meta.path_in_schema:<35s}  "
                      f"min={stats.min}  max={stats.max}")

    except ImportError:
        print("  pyarrow not installed — install with: pip install pyarrow")
        return

    # Sample via row groups — never load the full 254M rows (12+ GB)
    print(f"\n--- Sampling row groups to build column stats ---")
    try:
        # Read first 5 and last 2 row groups as a representative sample
        n_rg = meta.num_row_groups
        sample_rg_ids = list(range(min(5, n_rg))) + list(range(max(0, n_rg - 2), n_rg))
        sample_rg_ids = sorted(set(sample_rg_ids))
        print(f"  Reading row groups: {sample_rg_ids} (of {n_rg} total)")
        batch = pf.read_row_groups(sample_rg_ids).to_pandas()
        print(f"  Sample shape: {batch.shape}")

        # Key dimension columns — unique values
        for col in ["scenario", "year", "weather_year", "region"]:
            if col in batch.columns:
                vals = sorted(batch[col].unique().tolist())
                print(f"\n  unique {col} (in sample): {vals}")

        # For the full unique counts, scan the full file metadata per row group
        print(f"\n--- Full metadata scan: unique values per row group ---")
        all_scenarios = set()
        all_years     = set()
        all_wyr       = set()
        all_regions   = set()
        load_min, load_max = float("inf"), float("-inf")
        for i in range(n_rg):
            rg = meta.row_group(i)
            for j in range(rg.num_columns):
                cm = rg.column(j)
                s = cm.statistics
                if s is None or not s.has_min_max:
                    continue
                name = cm.path_in_schema
                if name == "scenario":
                    all_scenarios.update([s.min, s.max])
                elif name == "year":
                    all_years.update([s.min, s.max])
                elif name == "weather_year":
                    all_wyr.update([s.min, s.max])
                elif name == "region":
                    all_regions.update([s.min, s.max])
                elif name == "load_mw":
                    load_min = min(load_min, s.min)
                    load_max = max(load_max, s.max)

        print(f"  scenario (bounds only): {sorted(all_scenarios)}")
        print(f"  year     (bounds only): {sorted(all_years)}")
        print(f"  weather_year (bounds): {sorted(all_wyr)}")
        print(f"  region   (bounds only): {sorted(all_regions)}")
        print(f"  load_mw  range: {load_min:.1f} to {load_max:.1f} MW")

        # Numeric stats from the sample
        print(f"\n--- load_mw stats (from sample rows) ---")
        print(batch["load_mw"].describe().to_string())

        # Row count check: does it divide cleanly?
        total_rows = meta.num_rows
        n_hours = 8760  # ReEDS standard (no Feb 29)
        n_regions = len(sorted(set(batch["region"].unique())))  # from sample
        print(f"\n--- Row count arithmetic ---")
        print(f"  Total rows: {total_rows:,}")
        print(f"  / 8760 h   = {total_rows / 8760:,.0f}  (region x year x weather_year combos?)")
        print(f"  Unique regions in sample: {sorted(batch['region'].unique().tolist())}")
        print(f"  Unique years in sample:   {sorted(batch['year'].unique().tolist())}")
        print(f"  Unique weather_years:     {sorted(batch['weather_year'].unique().tolist())}")
        print(f"  Unique scenarios:         {sorted(batch['scenario'].unique().tolist())}")

        print(f"\n--- Head (5 rows) ---")
        print(batch.head(5).to_string())

        print(f"\n--- Sample ({n_sample} rows) ---")
        print(batch.sample(min(n_sample, len(batch)), random_state=42).to_string())

    except Exception as exc:
        print(f"  [Parquet row-group read failed: {exc}]")
        print(f"  Metadata-only view complete above — fix the error above and re-run.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--h5-only",      action="store_true")
    parser.add_argument("--parquet-only", action="store_true")
    parser.add_argument("--sample", type=int, default=5,
                        help="number of sample rows to display (default: 5)")
    args = parser.parse_args()

    pd.set_option("display.max_columns", 40)
    pd.set_option("display.width", 120)
    pd.set_option("display.max_colwidth", 40)

    if not args.parquet_only:
        if H5_PATH.exists():
            explore_h5(args.sample)
        else:
            print(f"[skip] HDF5 not found: {H5_PATH}")

    if not args.h5_only:
        if PARQUET_PATH.exists():
            explore_parquet(args.sample)
        else:
            print(f"[skip] Parquet not found: {PARQUET_PATH}")


if __name__ == "__main__":
    main()
