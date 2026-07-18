"""
Compare EIA-930 data sources: direct EIA API scrape vs PUDL nightly.

For each of the 8 California BAs, verifies that PUDL does not omit any
hourly time steps present in the EIA API scrape, and that reported values
agree within reasonable tolerances in the period covered by both sources.

PUDL starts earlier (EIA-930 history from 2015) and may end slightly before
the EIA live API (PUDL is a nightly batch, not real-time). Hours missing near
the current date are expected; missing hours deep in the historical record are
not — that would indicate PUDL dropped data.

Sections
--------
A  Source summary
     Row counts, BA coverage, and date ranges for each source.

B  Hourly time coverage
     Per BA, within the window covered by both sources, counts hours present
     in one source but not the other. Hours in EIA-not-PUDL are the concern;
     hours in PUDL-not-EIA within the overlap are also flagged.

C  Value agreement
     For each metric (demand, demand_forecast, net_generation, total_interchange)
     and each BA, reports correlation, mean absolute error, and share of hours
     with |diff| > 50 MWh across all paired observations.

D  NaN analysis (PUDL only)
     For each value metric and each BA, reports how many rows are NaN in the
     PUDL processed output, when the NaN values occur, and whether they are
     concentrated at the start/end of the record or scattered throughout.
     Runs independently of sections A/B/C (does not need the EIA file).

Outputs
-------
  Console: section summaries
  data/checks/eia_cmp_B_missing_in_pudl.csv  — hours in EIA but absent from PUDL
  data/checks/eia_cmp_B_missing_in_eia.csv   — hours in PUDL but absent from EIA
  data/checks/eia_cmp_C_large_diffs.csv      — hours with |diff| > threshold
  data/checks/eia_cmp_D_pudl_nans.csv        — every (ba, metric, hour) NaN triple
  data/checks/eia_source_gaps.txt            — narrative summary (B/C)

Usage
-----
  python scripts/data/compare_eia_sources.py            # all sections
  python scripts/data/compare_eia_sources.py -s D       # NaN audit only (no EIA file needed)
  python scripts/data/compare_eia_sources.py -s A       # summary only
  python scripts/data/compare_eia_sources.py -s B,C     # coverage + values
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

ROOT      = Path(__file__).resolve().parents[2]
PROC      = ROOT / "data" / "processed" / "eia"
IEPR_PROC = ROOT / "data" / "processed" / "iepr"
FIGS      = ROOT / "data" / "figures"
CHECKS    = ROOT / "data" / "checks" / "compare_eia_sources"
CHECKS.mkdir(parents=True, exist_ok=True)
FIGS.mkdir(parents=True, exist_ok=True)

CA8 = ["BANC", "CISO", "IID", "LDWP", "PACW", "NEVP", "TIDC", "WALC"]

EIA_FILE      = PROC / "eia_region.csv"
PUDL_FILE     = PROC / "eia930_operations.csv"
CAL_FILE      = PROC / "eia930_cal_region_EIA.csv"
PUDL_CAL_FILE = PROC / "eia930_cal_region_PUDL.csv"
IEPR_FILE     = IEPR_PROC / "iepr_baseline_annual.csv"

CAL_COLOR      = "#9467bd"   # purple — PUDL CA5 sum (preferred)
CAL_EIA_COLOR  = "#d62728"   # red — EIA API CAL (for annual plot comparison)
CA8_COLOR      = "#222222"
IEPR_COLORS = {2023: "#1f77b4", 2024: "#ff7f0e", 2025: "#2ca02c"}

# Metrics to compare: canonical key -> (EIA col, PUDL col, display label)
_METRICS: list[tuple[str, str, str, str]] = [
    ("demand",  "demand",           "demand_mwh",            "Demand"),
    ("fcst",    "demand_forecast",  "demand_forecast_mwh",   "Demand forecast"),
    ("netgen",  "net_gen",          "net_generation_mwh",    "Net generation"),
    ("ti",      "total_interchange","total_interchange_mwh", "Total interchange"),
]

_LARGE_DIFF_MWH = 50  # flag threshold


# ── Formatting ────────────────────────────────────────────────────────────────

def _hdr(s: str) -> None:
    print(f"\n{'=' * 72}\n  {s}\n{'=' * 72}")


def _subhdr(s: str) -> None:
    print(f"\n  {'-' * 60}\n  {s}\n  {'-' * 60}")


def _save(df: pd.DataFrame, name: str) -> None:
    if not df.empty:
        df.to_csv(CHECKS / name, index=False)


# ── Loading ───────────────────────────────────────────────────────────────────

def _localize_utc(ts: pd.Series) -> pd.Series:
    """Ensure a datetime series is UTC-aware."""
    if ts.dt.tz is None:
        return ts.dt.tz_localize("UTC")
    return ts.dt.tz_convert("UTC")


def _load_eia() -> pd.DataFrame:
    """Load EIA API scrape and normalise to canonical columns."""
    df = pd.read_csv(EIA_FILE, low_memory=False)
    # EIA period format: "2019-01-01T00"  (hour-beginning UTC)
    df["ts"] = _localize_utc(pd.to_datetime(df["period"], format="%Y-%m-%dT%H"))
    return df.rename(columns={"respondent": "ba"})[
        ["ts", "ba", "demand", "demand_forecast", "net_gen", "total_interchange"]
    ].copy()


def _load_pudl() -> pd.DataFrame:
    """Load PUDL operations CSV and normalise to canonical columns."""
    df = pd.read_csv(PUDL_FILE, low_memory=False)
    df["ts"] = _localize_utc(pd.to_datetime(df["datetime_utc"]))
    return df.rename(columns={
        "ba_code":               "ba",
        "demand_mwh":            "demand",
        "demand_forecast_mwh":   "demand_forecast",
        "net_generation_mwh":    "net_gen",
        "total_interchange_mwh": "total_interchange",
    })[["ts", "ba", "demand", "demand_forecast", "net_gen", "total_interchange"]].copy()


# ── Section A ─────────────────────────────────────────────────────────────────

def section_a(eia: pd.DataFrame, pudl: pd.DataFrame) -> None:
    _hdr("SECTION A — Source summary")

    for label, df in [("EIA API scrape", eia), ("PUDL nightly", pudl)]:
        print(f"\n  {label}")
        print(f"    rows       : {len(df):,}")
        print(f"    date range : {df['ts'].min()} -> {df['ts'].max()}")
        print(f"    BAs present: {sorted(df['ba'].unique())}")
        print()
        print(f"    {'BA':6s}  {'rows':>9}  {'start':>22}  {'end':>22}")
        print(f"    {'------':6s}  {'-'*9}  {'-'*22}  {'-'*22}")
        for ba in sorted(df["ba"].unique()):
            sub = df[df["ba"] == ba]
            print(f"    {ba:6s}  {len(sub):>9,}  {str(sub['ts'].min()):>22}  {str(sub['ts'].max()):>22}")

    eia_only = set(eia["ba"].unique()) - set(pudl["ba"].unique())
    pudl_only = set(pudl["ba"].unique()) - set(eia["ba"].unique())
    if eia_only:
        print(f"\n  BAs in EIA but not PUDL : {sorted(eia_only)}")
    if pudl_only:
        print(f"  BAs in PUDL but not EIA : {sorted(pudl_only)}")
    if not eia_only and not pudl_only:
        print(f"\n  Both sources cover the same BA set ✓")


# ── Section B ─────────────────────────────────────────────────────────────────

def section_b(
    eia: pd.DataFrame, pudl: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Within the overlap window (max of starts -> min of ends), compare the set
    of hourly timestamps for each BA.

    Hours in EIA but absent from PUDL are the key concern.
    Hours in PUDL but absent from EIA within the overlap are also noted.
    """
    _hdr("SECTION B — Hourly time coverage per BA")

    overlap_start = max(eia["ts"].min(), pudl["ts"].min())
    overlap_end   = min(eia["ts"].max(), pudl["ts"].max())

    print(f"\n  PUDL extends further back : {pudl['ts'].min()} -> {overlap_start}")
    print(f"  Overlap window             : {overlap_start} -> {overlap_end}")
    expected_hours = int((overlap_end - overlap_start).total_seconds() / 3600) + 1
    print(f"  Expected hours in overlap  : {expected_hours:,}")

    eia_ov  = eia[ (eia["ts"]  >= overlap_start) & (eia["ts"]  <= overlap_end)]
    pudl_ov = pudl[(pudl["ts"] >= overlap_start) & (pudl["ts"] <= overlap_end)]

    all_bas = sorted(set(eia_ov["ba"].unique()) | set(pudl_ov["ba"].unique()))

    print()
    print(f"  {'BA':6s}  {'EIA hrs':>9}  {'PUDL hrs':>9}  {'Missing in PUDL':>16}  "
          f"{'Extra in PUDL':>14}  {'Coverage':>9}")
    print(f"  {'------':6s}  {'-'*9}  {'-'*9}  {'-'*16}  {'-'*14}  {'-'*9}")

    rows_miss_pudl: list[dict] = []
    rows_miss_eia:  list[dict] = []

    for ba in all_bas:
        eia_ts  = set(eia_ov.loc[eia_ov["ba"]  == ba, "ts"])
        pudl_ts = set(pudl_ov.loc[pudl_ov["ba"] == ba, "ts"])

        miss_pudl = eia_ts  - pudl_ts   # BAD: EIA has it, PUDL does not
        miss_eia  = pudl_ts - eia_ts    # minor: PUDL has extra within overlap

        coverage = (len(eia_ts & pudl_ts) / len(eia_ts) * 100) if eia_ts else float("nan")

        flag = "  ← GAPS" if miss_pudl else ""
        print(f"  {ba:6s}  {len(eia_ts):>9,}  {len(pudl_ts):>9,}  "
              f"{len(miss_pudl):>16,}  {len(miss_eia):>14,}  {coverage:>8.2f}%{flag}")

        for ts in sorted(miss_pudl):
            rows_miss_pudl.append({"ba": ba, "datetime_utc": ts})
        for ts in sorted(miss_eia):
            rows_miss_eia.append({"ba": ba, "datetime_utc": ts})

    miss_pudl_df = pd.DataFrame(rows_miss_pudl)
    miss_eia_df  = pd.DataFrame(rows_miss_eia)

    print()
    if miss_pudl_df.empty:
        print("  PUDL covers every EIA hour in the overlap window ✓")
    else:
        print(f"  HOURS IN EIA BUT NOT PUDL: {len(miss_pudl_df):,} total")
        by_ba = miss_pudl_df.groupby("ba").size()
        for ba, n in by_ba.items():
            print(f"    {ba}: {n:,} missing hours")
        _save(miss_pudl_df, "eia_cmp_B_missing_in_pudl.csv")
        print("  Saved: eia_cmp_B_missing_in_pudl.csv")

    if not miss_eia_df.empty:
        _save(miss_eia_df, "eia_cmp_B_missing_in_eia.csv")
        print(f"  Extra PUDL hours within overlap: {len(miss_eia_df):,}  (see eia_cmp_B_missing_in_eia.csv)")

    return miss_pudl_df, miss_eia_df


# ── Section C ─────────────────────────────────────────────────────────────────

def section_c(eia: pd.DataFrame, pudl: pd.DataFrame) -> pd.DataFrame:
    """
    Inner-join on (ts, ba) and compare each value column per BA.
    PUDL applies corrections and gap-filling, so small differences are expected.
    Large differences suggest a systematic problem (e.g. sign flip, unit error).
    """
    _hdr("SECTION C — Value agreement on overlapping hours")

    # Check for systematic 1-hour offset (hour-ending vs hour-beginning)
    inner = pd.merge(eia[["ts", "ba"]], pudl[["ts", "ba"]], on=["ts", "ba"], how="inner")
    if inner.empty:
        print("\n  WARNING: inner join produced zero rows. Possible 1-hour UTC offset.")
        print("  Trying with PUDL shifted +1 hour ...")
        pudl_shifted = pudl.copy()
        pudl_shifted["ts"] = pudl_shifted["ts"] + pd.Timedelta(hours=1)
        inner2 = pd.merge(eia[["ts", "ba"]], pudl_shifted[["ts", "ba"]], on=["ts", "ba"])
        if not inner2.empty:
            print(f"  Shifted join found {len(inner2):,} rows — PUDL timestamps are 1 hour behind EIA.")
            print("  Using shifted PUDL for Section C. Investigate time convention in source data.")
            pudl = pudl_shifted
        else:
            print("  Shifted join also empty. Cannot compare values. Check timestamp formats.")
            return pd.DataFrame()

    # Both DataFrames are already normalised to canonical column names by _load_eia/_load_pudl.
    # _METRICS[1] is the canonical col name for EIA (and PUDL, after normalisation).
    merged = pd.merge(
        eia.rename(columns={c: f"{k}_eia"  for k, c, _, _ in _METRICS}),
        pudl.rename(columns={c: f"{k}_pudl" for k, c, _, _ in _METRICS}),
        on=["ts", "ba"],
        how="inner",
    )
    print(f"\n  Inner-join rows: {len(merged):,}")
    if merged.empty:
        print("  No overlapping (ts, ba) pairs — cannot compare values.")
        return pd.DataFrame()

    large_rows: list[pd.DataFrame] = []

    for ba in sorted(merged["ba"].unique()):
        _subhdr(ba)
        sub = merged[merged["ba"] == ba].copy()
        print(f"  {'Metric':<22}  {'n_pairs':>8}  {'corr':>7}  "
              f"{'MAE':>8}  {'MedAE':>8}  {f'|diff|>{_LARGE_DIFF_MWH}':>15}")
        print(f"  {'-'*22}  {'-'*8}  {'-'*7}  {'-'*8}  {'-'*8}  {'-'*15}")

        for key, _, _, label in _METRICS:
            col_e = f"{key}_eia"
            col_p = f"{key}_pudl"
            # keep ts so we can emit it in the large-diff output
            valid = sub[["ts", col_e, col_p]].dropna(subset=[col_e, col_p])
            n = len(valid)

            if n < 2:
                note = "(no EIA data)" if sub[col_e].isna().all() else \
                       "(no PUDL data)" if sub[col_p].isna().all() else \
                       "(< 2 pairs)"
                print(f"  {label:<22}  {n:>8,}  {'n/a':>7}  {'n/a':>8}  {'n/a':>8}  {'n/a':>15}  {note}")
                continue

            diff    = (valid[col_e] - valid[col_p]).abs()
            corr    = valid[col_e].corr(valid[col_p])
            mae     = diff.mean()
            med     = diff.median()
            n_large = int((diff > _LARGE_DIFF_MWH).sum())
            pct     = n_large / n * 100

            flag = "  ← CHECK" if n_large / n > 0.05 else ""
            print(f"  {label:<22}  {n:>8,}  {corr:>7.4f}  "
                  f"{mae:>8.1f}  {med:>8.1f}  {n_large:>6,} ({pct:>4.1f}%){flag}")

            if n_large > 0:
                bad = valid[diff > _LARGE_DIFF_MWH].copy()
                bad["ba"]     = ba
                bad["metric"] = label
                bad["diff"]   = bad[col_e] - bad[col_p]
                large_rows.append(bad[["ba", "metric", "ts", col_e, col_p, "diff"]])

    large_df = pd.concat(large_rows, ignore_index=True) if large_rows else pd.DataFrame()
    print()
    if large_df.empty:
        print(f"  All values agree within ±{_LARGE_DIFF_MWH} MWh ✓")
    else:
        print(f"  Hours with |diff| > {_LARGE_DIFF_MWH} MWh: {len(large_df):,}")
        _save(large_df, "eia_cmp_C_large_diffs.csv")
        print("  Saved: eia_cmp_C_large_diffs.csv")

    return large_df


# ── Section D ─────────────────────────────────────────────────────────────────

def section_d(pudl: pd.DataFrame) -> None:
    """
    NaN analysis in the PUDL processed output (eia930_operations.csv).

    For each value metric, shows which BAs are missing values, how many,
    and when — distinguishing structural gaps (BA never reports a series),
    early-history gaps (before reporting requirements applied), and scattered
    individual hours.
    """
    _hdr("SECTION D — NaN values in PUDL processed output")

    metrics = ["demand", "demand_forecast", "net_gen", "total_interchange"]
    labels  = {
        "demand":            "Demand",
        "demand_forecast":   "Demand fcst",
        "net_gen":           "Net gen",
        "total_interchange": "Total interchg",
    }

    all_bas = sorted(pudl["ba"].unique())

    # ── Overview table ────────────────────────────────────────────────────────
    print(f"\n  NaN counts per BA per metric (n and % of that BA's rows):")
    header  = f"  {'BA':6s}  {'rows':>7}"
    divider = f"  {'------':6s}  {'-------':>7}"
    for m in metrics:
        header  += f"  {labels[m]:>18}"
        divider += f"  {'------------------':>18}"
    print(header)
    print(divider)

    total_nan: dict[str, int] = {m: 0 for m in metrics}

    for ba in all_bas:
        sub = pudl[pudl["ba"] == ba]
        n   = len(sub)
        row = f"  {ba:6s}  {n:>7,}"
        for m in metrics:
            n_nan = int(sub[m].isna().sum())
            total_nan[m] += n_nan
            cell = "-" if n_nan == 0 else f"{n_nan:,} ({n_nan/n*100:.1f}%)"
            row += f"  {cell:>18}"
        print(row)

    total_row = f"  {'TOTAL':6s}  {len(pudl):>7,}"
    for m in metrics:
        n = total_nan[m]
        pct = n / len(pudl) * 100
        cell = "-" if n == 0 else f"{n:,} ({pct:.1f}%)"
        total_row += f"  {cell:>18}"
    print(divider)
    print(total_row)

    # ── Per-BA-metric detail for anything that has NaN ────────────────────────
    any_nan = any(v > 0 for v in total_nan.values())
    if not any_nan:
        print("\n  No NaN values found in any BA or metric. ✓")
        return

    _subhdr("Temporal detail for BAs/metrics with NaN values")

    nan_rows: list[dict] = []

    for ba in all_bas:
        sub = pudl[pudl["ba"] == ba].sort_values("ts").reset_index(drop=True)
        ba_start = sub["ts"].iloc[0]
        ba_end   = sub["ts"].iloc[-1]
        ba_span_h = max((ba_end - ba_start).total_seconds() / 3600, 1)

        ba_header_printed = False

        for m in metrics:
            nan_mask = sub[m].isna()
            if not nan_mask.any():
                continue

            if not ba_header_printed:
                print(f"\n  {ba}  [{ba_start.date()} -> {ba_end.date()}, {len(sub):,} hrs]")
                ba_header_printed = True

            nan_sub = sub[nan_mask]
            first_nan = nan_sub["ts"].iloc[0]
            last_nan  = nan_sub["ts"].iloc[-1]
            n_nan     = len(nan_sub)

            # Max consecutive NaN run using groupby on change-point IDs
            run_ids  = nan_mask.ne(nan_mask.shift()).cumsum()
            max_run  = int(nan_mask.groupby(run_ids).sum().max())

            # Where in the BA's time series do the NaN values sit?
            first_pct = (first_nan - ba_start).total_seconds() / 3600 / ba_span_h * 100
            last_pct  = (last_nan  - ba_start).total_seconds() / 3600 / ba_span_h * 100

            if last_pct < 25:
                location = "early record only"
            elif first_pct > 75:
                location = "recent record only"
            elif max_run >= 24 * 30:
                months = max_run // (24 * 30)
                location = f"long streak (~{months}+ month(s) consecutive)"
            elif n_nan / len(sub) > 0.5:
                location = "majority of record missing"
            else:
                location = "scattered throughout record"

            print(f"    {labels[m]:<14}  {n_nan:>6,}/{len(sub):,} NaN  "
                  f"({n_nan/len(sub)*100:.1f}%)  "
                  f"first={first_nan.date()}  last={last_nan.date()}  "
                  f"max_run={max_run}h  [{location}]")

            for row_ts in nan_sub["ts"]:
                nan_rows.append({"ba": ba, "metric": m, "datetime_utc": row_ts})

    nan_df = pd.DataFrame(nan_rows)
    _save(nan_df, "eia_cmp_D_pudl_nans.csv")
    print(f"\n  Total NaN (ba, metric, hour) triples: {len(nan_rows):,}")
    print("  Saved: eia_cmp_D_pudl_nans.csv")


# ── Section E: CAL region vs CA8 BA sum vs IEPR ──────────────────────────────

def _load_ca8_annual(pudl: pd.DataFrame) -> pd.DataFrame:
    """Sum PUDL CA8 BA demand by year; keep only complete years (>=95% hours/BA)."""
    pudl_copy = pudl.copy()
    pudl_copy["year"] = pudl_copy["ts"].dt.year
    counts = pudl_copy.groupby(["ba", "year"]).size().reset_index(name="n")
    full   = counts[counts["n"] >= int(8760 * 0.95)]
    n_bas  = pudl_copy["ba"].nunique()
    full_years = (
        full.groupby("year").size()
        .pipe(lambda s: s[s >= n_bas - 1])
        .index
    )
    annual = (
        pudl_copy[pudl_copy["year"].isin(full_years)]
        .groupby("year")["demand"]
        .sum()
        .reset_index()
    )
    annual["twh"] = annual["demand"] / 1_000_000
    return annual[["year", "twh"]].rename(columns={"twh": "ca8_twh"})


def _cal_annual_from(path: Path, col_name: str = "cal_twh") -> pd.DataFrame:
    """Load CAL demand by year from any cal_region CSV; keep only complete years."""
    if not path.exists():
        return pd.DataFrame(columns=["year", col_name])
    df = pd.read_csv(path, parse_dates=["datetime_utc"])
    if df["datetime_utc"].dt.tz is not None:
        df["datetime_utc"] = df["datetime_utc"].dt.tz_localize(None)
    df["year"] = df["datetime_utc"].dt.year
    counts     = df.groupby("year")["demand_mwh"].count()
    full_years = counts[counts >= int(8760 * 0.95)].index
    annual = (
        df[df["year"].isin(full_years)]
        .groupby("year")["demand_mwh"]
        .sum()
        .reset_index()
    )
    annual[col_name] = annual["demand_mwh"] / 1_000_000
    return annual[["year", col_name]]


def _load_cal_annual() -> pd.DataFrame:
    """EIA API CAL region demand by year (for annual plot alongside PUDL CA5 sum)."""
    return _cal_annual_from(CAL_FILE, col_name="cal_twh")


def _load_pudl_cal_annual() -> pd.DataFrame:
    """PUDL CA5 sum demand by year (preferred for analysis; cleaner data quality)."""
    return _cal_annual_from(PUDL_CAL_FILE, col_name="pudl_cal_twh")


def _load_iepr_annual() -> pd.DataFrame:
    """
    Annual IEPR BASELINE_NET_LOAD (net of BTM PV and storage) for PGE+SCE+SDGE
    (CAISO-territory utilities), Local_Reliability scenario, per (vintage, year).
    Returns iepr_twh in TWh and last_historical_year from the annual CSV.
    """
    IEPR_HRLY = IEPR_PROC / "iepr_hourly_forecast.csv"
    if not IEPR_FILE.exists() or not IEPR_HRLY.exists():
        return pd.DataFrame(columns=["vintage", "year", "iepr_twh", "last_historical_year"])

    # last_historical_year only in the annual file
    ann = pd.read_csv(IEPR_FILE)
    last_hist = (
        ann[ann["Historical_Net_Peak"].notna()]
        .groupby("forecast_vintage_year")["Year"]
        .max()
        .rename("last_historical_year")
    )

    raw = pd.read_csv(
        IEPR_HRLY,
        usecols=["forecast_vintage_year", "utility_ba", "scenario",
                 "YEAR", "BASELINE_NET_LOAD"],
    )
    CAISO = ["PGE", "SCE", "SDGE"]
    raw = raw[
        (raw["utility_ba"].isin(CAISO)) &
        (raw["scenario"] == "Local_Reliability")
    ]
    total = (
        raw.groupby(["forecast_vintage_year", "YEAR"])["BASELINE_NET_LOAD"]
        .sum()
        .reset_index()
        .rename(columns={
            "forecast_vintage_year": "vintage",
            "YEAR": "year",
            "BASELINE_NET_LOAD": "iepr_twh",
        })
    )
    total["iepr_twh"] /= 1_000_000  # MW·h -> TWh
    total = total.merge(last_hist, left_on="vintage", right_index=True)
    return total


def _load_cal_monthly() -> pd.DataFrame:
    """CAL region monthly mean hourly demand in GW; drops incomplete months."""
    if not PUDL_CAL_FILE.exists():
        return pd.DataFrame(columns=["period_ts", "cal_gw"])
    df = pd.read_csv(PUDL_CAL_FILE, parse_dates=["datetime_utc"])
    df["ts"]     = _localize_utc(df["datetime_utc"])
    df["period"] = df["ts"].dt.to_period("M")
    monthly = (
        df.groupby("period")["demand_mwh"]
        .agg(mean="mean", count="count")
        .reset_index()
    )
    days_per_month = monthly["period"].dt.days_in_month
    monthly  = monthly[monthly["count"] >= days_per_month * 24 * 0.95].copy()
    monthly["period_ts"] = monthly["period"].dt.to_timestamp()
    monthly["cal_gw"]    = monthly["mean"] / 1_000
    return monthly[["period_ts", "cal_gw"]]


def section_e(pudl: pd.DataFrame) -> None:
    """
    Compare CAL region demand to the sum of the 8 CA BAs (PUDL) and IEPR statewide.

    CAL is the EIA region boundary for California (2019-present).
    CA8 is the sum of eight BAs that include the CA grid but also Nevada (NEVP)
    and Pacific Northwest (PACW) load outside California.
    IEPR covers California utility territories (PGE, SCE, SDGE, LADWP, SMUD, etc.)
    and should be the closest match to CAL.
    """
    _hdr("SECTION E -- CAL region vs CA8 BA sum vs IEPR statewide")

    ca8_ann      = _load_ca8_annual(pudl)
    cal_ann      = _load_cal_annual()        # EIA API (for annual plot)
    pudl_cal_ann = _load_pudl_cal_annual()   # PUDL CA5 sum (preferred)
    iepr_ann     = _load_iepr_annual()

    # ── Annual summary ────────────────────────────────────────────────────────
    if not cal_ann.empty:
        merged = ca8_ann.merge(cal_ann, on="year", how="inner")
        merged["delta_twh"]   = merged["ca8_twh"] - merged["cal_twh"]
        merged["delta_pct"]   = merged["delta_twh"] / merged["cal_twh"] * 100

        print(f"\n  Annual comparison (complete years in both CA8 PUDL and CAL region):")
        print(f"  {'Year':>6}  {'CA8 TWh':>10}  {'CAL TWh':>10}  "
              f"{'Delta TWh':>10}  {'Delta %':>8}  Note")
        print(f"  {'------':>6}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*8}  ----")
        for _, row in merged.iterrows():
            note = ""
            if row["delta_pct"] > 10:
                note = "<- NEVP+PACW likely"
            print(f"  {int(row['year']):>6}  {row['ca8_twh']:>10,.1f}  "
                  f"{row['cal_twh']:>10,.1f}  {row['delta_twh']:>+10,.1f}  "
                  f"{row['delta_pct']:>+8.1f}%  {note}")

        avg_delta = merged["delta_twh"].mean()
        avg_pct   = merged["delta_pct"].mean()
        print(f"\n  Mean CA8 excess over CAL: {avg_delta:+.1f} TWh ({avg_pct:+.1f}%)")
        print(f"  (Expected: NEVP ~38.5 TWh + PACW ~21 TWh = ~60 TWh excess)")
    else:
        print(f"\n  CAL region file not found ({CAL_FILE.name}). Run process_eia_pudl.py first.")
        merged = pd.DataFrame()

    # ── IEPR vs CAL ───────────────────────────────────────────────────────────
    if not iepr_ann.empty and not cal_ann.empty:
        print(f"\n  IEPR BASELINE_NET_LOAD (PGE+SCE+SDGE) vs CAL region (projected years only):")
        print(f"  Note: IEPR covers CAISO territory only; CAL includes LDWP, BANC, IID etc.")
        print(f"  {'Vintage':>8}  {'Year':>6}  {'IEPR TWh':>10}  "
              f"{'CAL TWh':>10}  {'Delta TWh':>10}  {'Delta %':>8}")
        print(f"  {'-'*8}  {'------':>6}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*8}")
        for vintage, grp in iepr_ann.groupby("vintage"):
            last_h = grp["last_historical_year"].iloc[0]
            proj   = grp[grp["year"] > last_h].merge(cal_ann, on="year", how="inner")
            for _, row in proj.iterrows():
                d   = row["iepr_twh"] - row["cal_twh"]
                pct = d / row["cal_twh"] * 100
                print(f"  {int(vintage):>8}  {int(row['year']):>6}  "
                      f"{row['iepr_twh']:>10,.1f}  {row['cal_twh']:>10,.1f}  "
                      f"{d:>+10,.1f}  {pct:>+8.1f}%")

    # ── Figure ────────────────────────────────────────────────────────────────
    _section_e_figure(ca8_ann, cal_ann, iepr_ann, pudl_cal_ann=pudl_cal_ann)


def _section_e_figure(
    ca8_ann:  pd.DataFrame,
    cal_ann:  pd.DataFrame,
    iepr_ann: pd.DataFrame,
    pudl_cal_ann: pd.DataFrame | None = None,
) -> None:
    """Two-panel figure: annual totals + monthly mean demand."""
    cal_monthly = _load_cal_monthly()  # PUDL CA5 sum monthly

    # Monthly CA8 sum from PUDL
    pudl_df = pd.read_csv(PUDL_FILE, usecols=["datetime_utc", "demand_mwh"],
                          parse_dates=["datetime_utc"])
    pudl_df["ts"]     = _localize_utc(pudl_df["datetime_utc"])
    pudl_df["period"] = pudl_df["ts"].dt.to_period("M")
    ca8_monthly_raw = pudl_df.groupby("period")["demand_mwh"].agg(mean="mean", count="count").reset_index()
    days_per_month = ca8_monthly_raw["period"].dt.days_in_month
    ca8_monthly = ca8_monthly_raw[ca8_monthly_raw["count"] >= days_per_month * 24 * 8 * 0.90].copy()
    ca8_monthly["period_ts"] = ca8_monthly["period"].dt.to_timestamp()
    ca8_monthly["ca8_gw"]    = ca8_monthly["mean"] / 1_000

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 9))

    # ── Panel 1: Annual totals ────────────────────────────────────────────────
    if not ca8_ann.empty:
        ax1.plot(ca8_ann["year"], ca8_ann["ca8_twh"], color=CA8_COLOR,
                 lw=2.5, marker="o", ms=5, label="EIA CA8 sum (PUDL, 8 BAs)")
    # PUDL CA5 sum (solid) — preferred
    if pudl_cal_ann is not None and not pudl_cal_ann.empty:
        ax1.plot(pudl_cal_ann["year"], pudl_cal_ann["pudl_cal_twh"], color=CAL_COLOR,
                 lw=2.5, marker="s", ms=5, label="PUDL CA5 sum (BANC+CISO+IID+LDWP+TIDC)")
    # EIA API CAL (dashed) — shown alongside PUDL for comparison
    if not cal_ann.empty:
        ax1.plot(cal_ann["year"], cal_ann["cal_twh"], color=CAL_EIA_COLOR,
                 lw=1.5, marker="v", ms=4, ls="--", alpha=0.7,
                 label="EIA API CAL region (data quality issues in some years)")

    # Projected BASELINE_NET_LOAD net load (from hourly file — projected years only)
    if not iepr_ann.empty:
        for vintage, grp in iepr_ann.groupby("vintage"):
            color  = IEPR_COLORS.get(vintage, "gray")
            last_h = grp["last_historical_year"].iloc[0]
            proj   = grp[grp["year"] > last_h]
            ax1.plot(proj["year"], proj["iepr_twh"], color=color,
                     lw=2, label=f"IEPR v{vintage} projected net (PGE+SCE+SDGE)")

    # Historical IEPR from annual CSV (Total_Consumption, gross, all utilities).
    # The hourly forecast file only covers projected years; the annual CSV has both
    # historical and projected years with Total_Consumption for all utilities.
    if IEPR_FILE.exists():
        ann_df = pd.read_csv(IEPR_FILE)
        last_hist_by_v = (
            ann_df[ann_df["Historical_Net_Peak"].notna()]
            .groupby("forecast_vintage_year")["Year"]
            .max()
        )
        gross = (
            ann_df.groupby(["forecast_vintage_year", "Year"])["Total_Consumption"]
            .sum()
            .reset_index()
        )
        gross["twh"] = gross["Total_Consumption"] / 1_000  # GWh -> TWh
        for vintage, grp in gross.groupby("forecast_vintage_year"):
            if vintage not in IEPR_COLORS:
                continue
            color  = IEPR_COLORS[vintage]
            last_h = last_hist_by_v.get(vintage, None)
            if last_h is None:
                continue
            hist_rows = grp[grp["Year"] <= last_h]
            if not hist_rows.empty:
                ax1.plot(hist_rows["Year"], hist_rows["twh"], color=color,
                         lw=1.8, ls="--", alpha=0.75,
                         label=f"IEPR v{vintage} historical (gross, all utilities)")

    ref_cal = pudl_cal_ann if (pudl_cal_ann is not None and not pudl_cal_ann.empty) else None
    if not ca8_ann.empty and ref_cal is not None:
        m = ca8_ann.merge(ref_cal.rename(columns={"pudl_cal_twh": "cal_twh"}), on="year")
        ax1.fill_between(m["year"], m["cal_twh"], m["ca8_twh"],
                         alpha=0.12, color=CA8_COLOR,
                         label="CA8 excess over CA5 (NEVP + PACW territory)")

    ax1.set_xlabel("Year")
    ax1.set_ylabel("Annual demand (TWh)")
    ax1.set_title(
        "Annual demand: EIA CA8 sum vs PUDL CA5 sum vs EIA API CAL region vs IEPR by vintage\n"
        "Solid IEPR = projected net load (PGE+SCE+SDGE, BASELINE_NET_LOAD);  "
        "dashed IEPR = historical Total_Consumption (gross, all utilities)\n"
        "CA8 excess over CAL = NEVP+PACW;  IEPR net below CAL = non-CAISO utilities (LDWP, BANC, etc.)",
        fontsize=8,
    )
    ax1.set_xlim(2015, 2035)
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax1.legend(fontsize=7, ncol=2)
    ax1.grid(True, alpha=0.3)

    # ── Panel 2: Monthly mean demand ─────────────────────────────────────────
    if not ca8_monthly.empty:
        ax2.plot(ca8_monthly["period_ts"], ca8_monthly["ca8_gw"],
                 color=CA8_COLOR, lw=1.5, alpha=0.85, label="EIA CA8 sum (PUDL)")
    if not cal_monthly.empty:
        ax2.plot(cal_monthly["period_ts"], cal_monthly["cal_gw"],
                 color=CAL_COLOR, lw=1.5, alpha=0.85, label="PUDL CA5 sum (BANC+CISO+IID+LDWP+TIDC)")

    if not ca8_monthly.empty and not cal_monthly.empty:
        merged_m = ca8_monthly[["period_ts", "ca8_gw"]].merge(
            cal_monthly[["period_ts", "cal_gw"]], on="period_ts"
        )
        delta_mean = (merged_m["ca8_gw"] - merged_m["cal_gw"]).mean()
        ax2.fill_between(
            merged_m["period_ts"], merged_m["cal_gw"], merged_m["ca8_gw"],
            alpha=0.12, color=CA8_COLOR,
            label=f"CA8 excess over CA5 (mean {delta_mean:+.1f} GW)",
        )

    ax2.set_xlabel("Month")
    ax2.set_ylabel("Mean hourly demand (GW)")
    ax2.set_title("Monthly mean demand: EIA CA8 sum vs PUDL CA5 sum")
    ax2.legend(fontsize=9)
    ax2.grid(True, alpha=0.3)

    fig.tight_layout(rect=[0, 0, 1, 0.97])
    out = FIGS / "fig_e_cal_vs_ca8_vs_iepr.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  Figure saved: {out.relative_to(ROOT)}")


# ── Narrative summary ─────────────────────────────────────────────────────────

def _write_narrative(
    eia: pd.DataFrame,
    pudl: pd.DataFrame,
    miss_pudl: pd.DataFrame,
    large_diffs: pd.DataFrame,
) -> None:
    overlap_start = max(eia["ts"].min(), pudl["ts"].min())
    overlap_end   = min(eia["ts"].max(), pudl["ts"].max())

    lines = [
        "EIA-930 Source Comparison: EIA API vs PUDL",
        f"Generated: {pd.Timestamp.now().strftime('%Y-%m-%d')}",
        "=" * 60,
        "",
        "Date ranges:",
        f"  EIA API : {eia['ts'].min()} -> {eia['ts'].max()}",
        f"  PUDL    : {pudl['ts'].min()} -> {pudl['ts'].max()}",
        f"  Overlap : {overlap_start} -> {overlap_end}",
        "",
        "BAs covered by both sources:",
        f"  {sorted(set(eia['ba'].unique()) & set(pudl['ba'].unique()))}",
        "",
        "─" * 60,
        "Time coverage (B)",
        "─" * 60,
    ]

    if miss_pudl.empty:
        lines += [
            "  PUDL has every hourly timestamp present in the EIA scrape.",
            "  No historical gaps detected.",
        ]
    else:
        n_total = len(miss_pudl)
        by_ba   = miss_pudl.groupby("ba").size()
        lines += [
            f"  {n_total:,} EIA hours are absent from PUDL within the overlap period:",
        ]
        for ba, n in by_ba.items():
            lines.append(f"    {ba}: {n:,} missing hours")
        lines.append("  -> Saved to eia_cmp_B_missing_in_pudl.csv")

    lines += [
        "",
        "─" * 60,
        "Value agreement (C)",
        "─" * 60,
    ]

    if large_diffs.empty:
        lines += [
            f"  All paired values agree within ±{_LARGE_DIFF_MWH} MWh.",
            "  Note: small systematic differences are expected where PUDL",
            "  applies imputation or outlier corrections to the raw EIA data.",
        ]
    else:
        by_m = large_diffs.groupby("metric").size()
        lines += [
            f"  {len(large_diffs):,} (BA, hour, metric) triplets exceed ±{_LARGE_DIFF_MWH} MWh:",
        ]
        for m, n in by_m.items():
            lines.append(f"    {m}: {n:,} hours")
        lines.append("  -> Saved to eia_cmp_C_large_diffs.csv")

    txt = "\n".join(lines)
    out = CHECKS / "eia_source_gaps.txt"
    out.write_text(txt, encoding="utf-8")
    print(f"\n  Narrative summary -> {out.relative_to(ROOT)}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-s", "--section",
        default="A,B,C,D,E",
        metavar="SECTIONS",
        help="Sections to run: A, B, C, D, E (default: A,B,C,D,E)",
    )
    args = parser.parse_args()
    sections = {s.strip().upper() for s in args.section.split(",")}

    print("Loading PUDL operations ...")
    if not PUDL_FILE.exists():
        sys.exit(f"  File not found: {PUDL_FILE}\n  Run scripts/data/eia/process_eia_pudl.py first.")
    pudl = _load_pudl()
    print(f"  {len(pudl):,} rows  [{pudl['ts'].min()} -> {pudl['ts'].max()}]")

    eia = pd.DataFrame()
    if sections & {"A", "B", "C"}:
        print("Loading EIA API scrape ...")
        if not EIA_FILE.exists():
            print(f"  WARNING: {EIA_FILE} not found — skipping sections A/B/C.")
            sections -= {"A", "B", "C"}
        else:
            eia = _load_eia()
            print(f"  {len(eia):,} rows  [{eia['ts'].min()} -> {eia['ts'].max()}]")

    miss_pudl  = pd.DataFrame()
    large_diff = pd.DataFrame()

    if "A" in sections:
        section_a(eia, pudl)
    if "B" in sections:
        miss_pudl, _ = section_b(eia, pudl)
    if "C" in sections:
        large_diff = section_c(eia, pudl)
    if "D" in sections:
        section_d(pudl)
    if "E" in sections:
        section_e(pudl)

    if {"B", "C"} & sections:
        _write_narrative(eia, pudl, miss_pudl, large_diff)

    print(f"\n{'=' * 72}")
    print(f"  Done. Outputs -> {CHECKS.relative_to(ROOT)}/")
    print("=" * 72)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
