"""
Process PUDL EIA-930 parquets into standardized CSVs.

Inputs (from scripts/ingest_eia_pudl.py)
-----------------------------------------
  data/raw/eia/pudl/core_eia930__hourly_operations_CA8.parquet
  data/raw/eia/pudl/core_eia930__hourly_interchange_CA8.parquet  (optional)

Outputs
-------
  data/processed/eia/eia930_operations.csv
    One row per (datetime_utc, balancing_authority_code).
    Columns: datetime_utc, ba_code, ba_name,
             demand_mwh, demand_forecast_mwh, net_generation_mwh,
             total_interchange_mwh
    All values in MWh.

  data/processed/eia/eia930_interchange.csv
    One row per (datetime_utc, from_ba, to_ba).
    Positive value = net export from from_ba to to_ba (MW/MWh — EIA-930 convention).
    Columns: datetime_utc, from_ba, to_ba, interchange_mwh

Notes
-----
PUDL column names are mapped via constants at the top of this file.
Run once to see the printed schema, then update the mappings below if needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.eia.pudl_eia930 import CA8

RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw" / "eia" / "pudl"
OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "processed" / "eia"

_OPS_FILE = RAW_DIR / "core_eia930__hourly_operations_CA8.parquet"
_IXC_FILE = RAW_DIR / "core_eia930__hourly_interchange_CA8.parquet"

# ── Column name mappings ──────────────────────────────────────────────────────
# PUDL column → canonical output column name.
# Adjust if PUDL renames a column in a future nightly build.
#
# Datetime: PUDL uses 'datetime_utc' (UTC-stamped, hour-beginning).
_TS_COL = "datetime_utc"

# BA identifier columns
_BA_COL  = "balancing_authority_code_eia"   # e.g. "CISO"
_BA_NAME = "balancing_authority_name_eia"    # e.g. "California ISO"

# Operations: map PUDL column → output column name.
# PUDL may provide both "reported" and "imputed" variants; we prefer imputed
# where available (PUDL has filled gaps), falling back to reported.
_OPS_COL_CANDIDATES: dict[str, list[str]] = {
    "demand_mwh":            ["demand_imputed_eia_mwh", "demand_reported_mwh","demand_adjusted_mwh"],
    "demand_forecast_mwh":   ["demand_forecast_mwh"],
    "net_generation_mwh":    ["net_generation_reported_mwh","net_generation_imputed_eia_mwh","net_generation_adjusted_mwh"],
    "total_interchange_mwh": ["interchange_reported_mwh","interchange_imputed_eia_mwh","interchange_adjusted_mwh"],
}

# Interchange: PUDL BA-pair interchange columns
_IXC_ADJ_COL = "adjacent_balancing_authority_code_eia"  # the other BA in the pair
_IXC_VAL_COL = "interchange_mwh"  # positive = export from reporting BA


def _resolve_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """Return the first candidate column that exists in df, or None."""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def process_operations(out_dir: Path = OUT_DIR) -> Path:
    """
    Read PUDL operations parquet and write a lean wide-format CSV.

    Missing series (e.g. a BA that only reports demand, not generation) will
    appear as NaN in the output rather than cause the row to be dropped.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    if not _OPS_FILE.exists():
        raise FileNotFoundError(
            f"{_OPS_FILE} not found. Run scripts/ingest_eia_pudl.py first."
        )

    print(f"Loading {_OPS_FILE.name} ...")
    df = pd.read_parquet(_OPS_FILE)
    print(f"  {len(df):,} rows, {len(df.columns)} columns")
    print(f"  columns: {list(df.columns)}")

    # Resolve column names
    resolved: dict[str, str] = {}
    for out_col, candidates in _OPS_COL_CANDIDATES.items():
        src = _resolve_col(df, candidates)
        if src:
            resolved[out_col] = src
        else:
            print(f"  WARNING: none of {candidates} found in data; {out_col} will be NaN")

    # Build output frame
    ba_name_col = _BA_NAME if _BA_NAME in df.columns else None
    out = pd.DataFrame()
    out["datetime_utc"]  = df[_TS_COL]
    out["ba_code"]       = df[_BA_COL]
    out["ba_name"]       = df[ba_name_col] if ba_name_col else pd.NA

    for out_col, src_col in resolved.items():
        out[out_col] = df[src_col]
    for out_col in _OPS_COL_CANDIDATES:
        if out_col not in resolved:
            out[out_col] = pd.NA

    out = (
        out.sort_values(["ba_code", "datetime_utc"])
        .reset_index(drop=True)
    )

    out_path = out_dir / "eia930_operations.csv"
    out.to_csv(out_path, index=False)
    mb = out_path.stat().st_size / 1024 / 1024

    print(f"\nWrote {len(out):,} rows -> {out_path}  ({mb:.1f} MB)")
    print(f"  BA codes  : {sorted(out['ba_code'].dropna().unique())}")
    print(f"  Period    : {out['datetime_utc'].min()} -> {out['datetime_utc'].max()}")
    return out_path


def process_interchange(out_dir: Path = OUT_DIR) -> Path | None:
    """
    Read PUDL interchange parquet and write a canonical from/to CSV.

    Sign convention (EIA-930): positive value = net export from from_ba to to_ba.
    Only rows where from_ba is in CA8 are included; this is a complete picture
    because PUDL records interchange from the reporting BA's perspective.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    if not _IXC_FILE.exists():
        print(f"  Interchange file not found ({_IXC_FILE.name}); skipping.")
        return None

    print(f"Loading {_IXC_FILE.name} ...")
    df = pd.read_parquet(_IXC_FILE)
    print(f"  {len(df):,} rows, {len(df.columns)} columns")
    print(f"  columns: {list(df.columns)}")

    adj_col = _resolve_col(df, [_IXC_ADJ_COL, "balancing_authority_code_adjacent_eia",
                                 "toba", "to_ba"])
    val_col = _resolve_col(df, [_IXC_VAL_COL, "interchange_reported_mwh", "value"])

    if not adj_col or not val_col:
        print(f"  WARNING: cannot identify adjacent-BA or value columns; skipping.")
        return None

    out = pd.DataFrame()
    out["datetime_utc"]    = df[_TS_COL]
    out["from_ba"]         = df[_BA_COL]
    out["to_ba"]           = df[adj_col]
    out["interchange_mwh"] = df[val_col]

    out = (
        out.dropna(subset=["from_ba", "to_ba", "interchange_mwh"])
        .sort_values(["from_ba", "to_ba", "datetime_utc"])
        .reset_index(drop=True)
    )

    out_path = out_dir / "eia930_interchange.csv"
    out.to_csv(out_path, index=False)
    mb = out_path.stat().st_size / 1024 / 1024

    print(f"\nWrote {len(out):,} rows -> {out_path}  ({mb:.1f} MB)")
    print(f"  Reporting BAs : {sorted(out['from_ba'].unique())}")
    print(f"  Period        : {out['datetime_utc'].min()} -> {out['datetime_utc'].max()}")
    return out_path


if __name__ == "__main__":
    print("=== Processing EIA-930 operations ===")
    process_operations()
    print()
    print("=== Processing EIA-930 interchange ===")
    process_interchange()
