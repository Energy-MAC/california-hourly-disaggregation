"""
process_historic_load.py

Processes the NREL ReEDS historic hourly load data (2016-2023) into
California-filtered outputs for comparison against EIA-930, IEPR, RESOLVE,
and the ReEDS projected load.

Source file
-----------
  data/raw/reeds/historic_post2015_load_hourly.h5
      HDF5-serialised pandas DataFrame with 134 p-regions (same as ReEDS
      projected data), 70080 rows = 8 years x 8760 h (leap days excluded),
      timestamps in CST (UTC-6, Etc/GMT+6), hour-beginning.

Load definition
---------------
  Net load measured at balancing-authority level (same convention as EIA-930
  demand_mwh).  The ReEDS hourlize tool sources historical load from BA-level
  meter data (EIA-930 / FERC Form 714), which report demand net of BTM
  generation.  CITATION NEEDED: specific hourlize source mapping not confirmed
  in publicly available files; magnitude comparison with EIA CISO in
  compare_resolve_iepr_eia.py provides empirical validation.

California regions (same citation as process_reeds.py)
-------------------------------------------------------
  Confirmed via data/raw/reeds/ReEDS-2.0/inputs/hierarchy.csv:
  filtering st == "CA" returns exactly p8, p9, p10, p11.
    p8   PacifiCorp West California slice  (WECC_NW; ~100-160 MW, ~0.8 TWh/yr)
    p9   WECC_CA sub-region
    p10  WECC_CA sub-region
    p11  WECC_CA sub-region
  hierarchy.csv labels p9-p11 as "WECC_CA", which in ReEDS terminology covers
  all California BAs except PacifiCorp West.  Empirically confirmed by comparing
  annual totals against EIA sources: p9-p11 annual load (~252-268 TWh, 2016-2023)
  tracks PUDL CA5 sum (BANC+CISO+IID+LDWP+TIDC), not EIA CISO alone (~218-224 TWh).
  The ~40 TWh gap between p9-p11 and CISO equals approximately IID+LDWP+BANC+TIDC
  combined, confirming WECC_CA ≈ all California BAs except PACW.
  WECC_CA (p9-p11) = p9 + p10 + p11 only.
  CA total          = p8 + p9 + p10 + p11 (WECC_CA + PACW CA slice).

Time convention
---------------
  Source timestamps: CST (UTC-6), hour-beginning, ISO 8601 with "-06:00" offset.
  Confirmed by inspecting index_0 in the HDF5 file: first value is
  "2016-01-01T00:00:00-06:00".  Same timezone as ReEDS projected data
  (config_base.json "output_timezone": "Etc/GMT+6").

  Conversion to Fixed PST (UTC-8): subtract 2 hours.
    CST Jan 1 00:00 = PST Dec 31 22:00 (previous calendar year).
  Annual totals below use CST calendar year for grouping; the first 2 CST
  hours of each year fall in the prior PST year, shifting ~2 h of load between
  years.  This ~0.02% annual effect is negligible for the comparison figures.

  Leap years: 70080 rows / 8 years = 8760 rows/year exactly, confirming leap
  days are excluded (consistent with ReEDS hourlize convention).

Outputs
-------
  data/processed/reeds/historic_ca_load_annual.csv
      Annual energy totals (CST calendar year) by region.
      Columns: year | region | annual_mwh | annual_twh
      Region values: p8, p9, p10, p11, CAISO_total, CA_total

  data/processed/reeds/historic_ca_load_hourly.parquet
      CA-filtered hourly data with PST datetime columns added.
      Columns: timestamp_cst | datetime_pst | year | month | day | hour |
               p8_mw | p9_mw | p10_mw | p11_mw | CAISO_mw | CA_total_mw

Usage
-----
  python scripts/data/reeds/process_historic_load.py
"""
from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
RAW  = ROOT / "data" / "raw" / "reeds" / "historic_post2015_load_hourly.h5"
OUT  = ROOT / "data" / "processed" / "reeds"

CA_COLS    = ["p8", "p9", "p10", "p11"]
CAISO_COLS = ["p9", "p10", "p11"]


def _read_h5() -> pd.DataFrame:
    """Read HDF5 file and return CA-filtered DataFrame with parsed timestamps."""
    print(f"Reading {RAW.name} ...")
    with h5py.File(RAW, "r") as f:
        all_cols = [c.decode() for c in f["columns"][:]]
        raw_idx  = [i.decode() for i in f["index_0"][:]]
        data     = f["data"][:]

    # Filter to CA columns only
    ca_idx = [all_cols.index(c) for c in CA_COLS]
    df = pd.DataFrame(
        data[:, ca_idx],
        columns=CA_COLS,
        index=raw_idx,
    )
    df.index.name = "timestamp_cst"
    print(f"  Loaded {len(df):,} rows for CA regions {CA_COLS}")
    print(f"  Date range (CST): {df.index[0]}  to  {df.index[-1]}")
    return df


def _add_datetime_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Parse CST timestamps and derive PST datetime columns."""
    # Parse ISO strings; tzinfo is UTC-6 from the raw strings
    dt_cst = pd.to_datetime(df.index, utc=True).tz_convert("Etc/GMT+6")
    # Convert to PST (UTC-8) = subtract 2 hours
    dt_pst = dt_cst - pd.Timedelta(hours=2)

    df = df.copy()
    df["timestamp_cst"] = df.index
    df["datetime_pst"]  = dt_pst.tz_localize(None)   # strip tz for output
    df["year"]          = dt_cst.year                 # CST calendar year for grouping
    df["month"]         = dt_pst.month
    df["day"]           = dt_pst.day
    df["hour"]          = dt_pst.hour

    # Derived aggregates
    df["CAISO_mw"]    = df[CAISO_COLS].sum(axis=1)
    df["CA_total_mw"] = df[CA_COLS].sum(axis=1)
    return df.reset_index(drop=True)


def _write_hourly(df: pd.DataFrame) -> None:
    col_order = [
        "timestamp_cst", "datetime_pst", "year", "month", "day", "hour",
        "p8_mw", "p9_mw", "p10_mw", "p11_mw", "CAISO_mw", "CA_total_mw",
    ]
    out_df = df.rename(columns={c: f"{c}_mw" for c in CA_COLS})[col_order]
    out = OUT / "historic_ca_load_hourly.parquet"
    out_df.to_parquet(out, index=False, engine="pyarrow", compression="snappy")
    print(f"Wrote {out.relative_to(ROOT)}  ({len(out_df):,} rows)")


def _write_annual(df: pd.DataFrame) -> None:
    """Annual MWh/TWh by CST calendar year, per region and aggregated totals."""
    rows = []

    # Per-region rows
    for col in CA_COLS:
        ann = (df.groupby("year")[col].sum()
                 .reset_index()
                 .rename(columns={col: "annual_mwh"}))
        ann["region"]     = col
        ann["annual_twh"] = ann["annual_mwh"] / 1e6
        rows.append(ann)

    # CAISO total (p9+p10+p11)
    caiso = (df.groupby("year")["CAISO_mw"].sum()
               .reset_index()
               .rename(columns={"CAISO_mw": "annual_mwh"}))
    caiso["region"]     = "CAISO_total"
    caiso["annual_twh"] = caiso["annual_mwh"] / 1e6
    rows.append(caiso)

    # CA total (p8+p9+p10+p11)
    ca = (df.groupby("year")["CA_total_mw"].sum()
            .reset_index()
            .rename(columns={"CA_total_mw": "annual_mwh"}))
    ca["region"]     = "CA_total"
    ca["annual_twh"] = ca["annual_mwh"] / 1e6
    rows.append(ca)

    ann_all = (pd.concat(rows, ignore_index=True)
                 [["year", "region", "annual_mwh", "annual_twh"]]
                 .sort_values(["year", "region"])
                 .reset_index(drop=True))

    out = OUT / "historic_ca_load_annual.csv"
    ann_all.to_csv(out, index=False)
    print(f"Wrote {out.relative_to(ROOT)}  ({len(ann_all):,} rows)")

    # Summary
    caiso_ann = ann_all[ann_all["region"] == "CAISO_total"]
    ca_ann    = ann_all[ann_all["region"] == "CA_total"]
    print("\n  Historic CA load annual totals (TWh):")
    print(f"  {'Year':<6} {'CAISO (p9-p11)':>15} {'CA total (p8-p11)':>18} {'p8 only':>10}")
    for yr in sorted(caiso_ann["year"].unique()):
        caiso_v = caiso_ann[caiso_ann["year"] == yr]["annual_twh"].iloc[0]
        ca_v    = ca_ann[ca_ann["year"] == yr]["annual_twh"].iloc[0]
        p8_v    = ca_v - caiso_v
        print(f"  {yr:<6} {caiso_v:>15.1f} {ca_v:>18.1f} {p8_v:>10.2f}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    df = _read_h5()
    df = _add_datetime_cols(df)
    _write_hourly(df)
    _write_annual(df)

    print("\nDone.")


if __name__ == "__main__":
    main()
