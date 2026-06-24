"""
process_reeds.py

Processes the ReEDS projected load parquet into California-filtered outputs for
comparison against IEPR, RESOLVE, and EIA-930.

ReEDS (Regional Energy Deployment System) is NREL's US capacity-planning model.
This script uses the pre-transformed parquet produced by a ReEDS run under the
IRA_low (Inflation Reduction Act, low-demand) scenario.

Source file
-----------
  data/raw/reeds/reeds_load_transformed.parquet
      Long-format table of projected hourly load for all 134 US p-regions,
      every target year (2020-2050), and 7 historical weather years (2007-2013).
      Weather year range and single scenario verified by scripts/explore_potential_data.py
      (parquet metadata scan: unique weather_year bounds and unique scenario values).

      Columns:
        time_index   int  1-8760  hour of the year (no Feb 29; 1 = Jan 1 00:00 CST)
        weather_year int  2007-2013  which historical weather year drives the shape
        region       str  p1-p134    ReEDS planning region
        load_mw      float  projected hourly load (MW)
        year         int  2020-2050  planning/target year
        scenario     str  "IRA_low"  (only scenario in this file)

California regions (from ReEDS inputs/hierarchy.csv)
------------------------------------------------------
  p8   PacifiCorp West California slice  (WECC_NW / NorthernGrid, non-CAISO)
  p9   CAISO sub-region (WECC_CA)
  p10  CAISO sub-region (WECC_CA)
  p11  CAISO sub-region (WECC_CA)

Time convention
---------------
  ReEDS output timezone is Etc/GMT+6 (CST, UTC-6, no DST), hour-beginning.
  Confirmed in data/raw/reeds/ReEDS-2.0/hourlize/inputs/configs/config_base.json
  ("output_timezone": "Etc/GMT+6") and hourlize/load.py (fixed-offset tz_localize,
  no DST transitions, first 8760 hours taken per weather year).

  Conversion to Fixed PST (UTC-8):
    time_index 1 = Jan 1 00:00 CST = Dec 31 22:00 PST
  The _REF_DATES reference therefore starts at "2000-12-31 22:00" so that
  month/day/hour columns reflect the correct PST wall-clock hour, matching the
  no-DST 8760-h/year convention used by IEPR and RESOLVE.

Outputs
-------
  data/processed/reeds/reeds_ca_load_hourly.parquet
      CA-filtered hourly data with datetime columns added.
      Columns: time_index | weather_year | region | region_label | load_mw |
               year | scenario | month | day | hour

  data/processed/reeds/reeds_ca_load_annual.csv
      Annual energy totals by (year, weather_year, region) + CA total.
      Columns: year | scenario | weather_year | region | annual_mwh | annual_twh
      Plus a CA_total row per (year, weather_year) summing all four regions.

Usage
-----
  python scripts/data/reeds/process_reeds.py
"""

from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[3]
RAW  = ROOT / "data" / "raw"  / "reeds" / "reeds_load_transformed.parquet"
OUT  = ROOT / "data" / "processed" / "reeds"

# CA region confirmation source (filter st==CA returns exactly p8, p9, p10, p11)
HIERARCHY_CSV = ROOT / "data" / "raw" / "reeds" / "ReEDS-2.0" / "inputs" / "hierarchy.csv"

CA_REGIONS = ["p8", "p9", "p10", "p11"]

REGION_LABELS = {
    "p8":  "PacifiCorp_West_CA",
    "p9":  "CAISO_North",
    "p10": "CAISO_Central",
    "p11": "CAISO_South",
}

# Reference date range for time_index -> (month, day, hour) in Fixed PST.
# ReEDS uses Etc/GMT+6 (CST, UTC-6); time_index 1 = Jan 1 00:00 CST = Dec 31 22:00 PST.
# Starting 2 hours earlier converts CST indices to correct PST wall-clock hours.
_REF_DATES = pd.date_range("2000-12-31 22:00", periods=8760, freq="h")
_TI_TO_MONTH = (_REF_DATES.month).astype("int8")
_TI_TO_DAY   = (_REF_DATES.day).astype("int8")
_TI_TO_HOUR  = (_REF_DATES.hour).astype("int8")


def _read_ca_parquet() -> pd.DataFrame:
    """Read only CA rows from the large parquet using pyarrow predicate pushdown."""
    print(f"Reading CA regions {CA_REGIONS} from {RAW.name} ...")
    df = pd.read_parquet(RAW, filters=[("region", "in", CA_REGIONS)])
    print(f"  Loaded {len(df):,} rows x {df.shape[1]} columns")
    return df


def _add_datetime_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Map time_index (1-8760) to month / day / hour (fixed PST, no DST)."""
    ti = df["time_index"].values - 1   # 0-based index into _REF_DATES
    df = df.copy()
    df["month"] = _TI_TO_MONTH[ti]
    df["day"]   = _TI_TO_DAY[ti]
    df["hour"]  = _TI_TO_HOUR[ti]
    df["region_label"] = df["region"].map(REGION_LABELS)
    return df


def _write_hourly(df: pd.DataFrame) -> None:
    out = OUT / "reeds_ca_load_hourly.parquet"
    col_order = ["time_index", "weather_year", "region", "region_label",
                 "load_mw", "year", "scenario", "month", "day", "hour"]
    df[col_order].to_parquet(out, index=False, engine="pyarrow", compression="snappy")
    print(f"Wrote {out.relative_to(ROOT)}  ({len(df):,} rows)")


def _write_annual(df: pd.DataFrame) -> None:
    """Aggregate to annual MWh by (year, scenario, weather_year, region)."""
    ann = (df.groupby(["year", "scenario", "weather_year", "region"])["load_mw"]
             .sum()
             .reset_index()
             .rename(columns={"load_mw": "annual_mwh"}))
    ann["annual_twh"] = ann["annual_mwh"] / 1e6

    # WECC_CA total row: p9+p10+p11 only (excludes p8 PacifiCorp CA slice)
    # hierarchy.csv labels p9-p11 as WECC_CA.  Empirically, WECC_CA ≈ all CA BAs
    # except PacifiCorp West — it tracks PUDL CA5 sum (~BANC+CISO+IID+LDWP+TIDC),
    # not EIA CISO alone.  Stored as "CAISO_total" for backward compatibility, but
    # the correct label is WECC_CA.  Do NOT compare this to EIA CISO.
    caiso_mask = ann["region"].isin(["p9", "p10", "p11"])
    caiso_tot = (ann[caiso_mask]
                 .groupby(["year", "scenario", "weather_year"])
                 .agg(annual_mwh=("annual_mwh", "sum"),
                      annual_twh=("annual_twh", "sum"))
                 .reset_index()
                 .assign(region="CAISO_total"))

    # CA total row: sum across all 4 CA regions (p8+p9+p10+p11)
    # Use CA_total when comparing against PUDL CA5 sum or EIA CAL geographic region.
    ca_tot = (ann.groupby(["year", "scenario", "weather_year"])
                 .agg(annual_mwh=("annual_mwh", "sum"),
                      annual_twh=("annual_twh", "sum"))
                 .reset_index()
                 .assign(region="CA_total"))
    ann = pd.concat([ann, caiso_tot, ca_tot], ignore_index=True)
    ann = ann.sort_values(["year", "weather_year", "region"]).reset_index(drop=True)

    out = OUT / "reeds_ca_load_annual.csv"
    ann.to_csv(out, index=False)
    print(f"Wrote {out.relative_to(ROOT)}  ({len(ann):,} rows)")

    # Print summary
    ca = ann[ann["region"] == "CA_total"]
    mean_by_year = ca.groupby("year")["annual_twh"].mean()
    std_by_year  = ca.groupby("year")["annual_twh"].std()
    print("\n  ReEDS IRA_low CA total p8+p9+p10+p11 (mean ± std across 7 weather years, TWh):")
    for yr in [2020, 2025, 2030, 2035, 2040, 2045, 2050]:
        if yr in mean_by_year.index:
            m, s = mean_by_year[yr], std_by_year[yr]
            print(f"    {yr}: {m:.1f} ± {s:.1f} TWh")

    caiso = ann[ann["region"] == "CAISO_total"]
    caiso_mean = caiso.groupby("year")["annual_twh"].mean()
    caiso_std  = caiso.groupby("year")["annual_twh"].std()
    print("\n  ReEDS IRA_low CAISO p9+p10+p11 (mean ± std across 7 weather years, TWh):")
    for yr in [2020, 2025, 2030, 2035, 2040, 2045, 2050]:
        if yr in caiso_mean.index:
            m, s = caiso_mean[yr], caiso_std[yr]
            print(f"    {yr}: {m:.1f} ± {s:.1f} TWh")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    df = _read_ca_parquet()
    df = _add_datetime_cols(df)
    _write_hourly(df)
    _write_annual(df)

    print("\nDone.")


if __name__ == "__main__":
    main()
