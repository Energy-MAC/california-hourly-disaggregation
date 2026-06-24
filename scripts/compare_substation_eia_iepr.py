"""
compare_substation_eia_iepr.py

Compares substation ICA load profiles (coincident sum) to:
  - EIA-930 CISO realized demand (month-hour mean + inter-annual range)
  - IEPR BASELINE_NET_LOAD (PGE+SCE+SDGE, by vintage)
  - RESOLVE net load (PGE+SCE+SDGE, 23 weather years 2000-2022)

Substation profiles represent the high-load-day (max_load) and low-load-day
(min_load) at each substation for each month-hour.  Summing these across all
substations gives the COINCIDENT load bounds as measured at distribution level.

PGE and SDGE have no year stamp -- their profiles are fixed month-hour overlays.
SCE has years 2017-2026 with overlapping substation coverage across years.  For
each (substation, month, hour) cell the most recent vintage is used; 2026 only
covers Jan-Apr so May-Dec fall back to 2025 for those substations.  The processed
CSV already encodes this deduplication; the loader applies it defensively as well.

RESOLVE: gross → net load derivation
--------------------------------------
`resolve_hourly_profiles.csv` stores RESOLVE's historical load shapes as
`demand_mw_2024scaled` (MW), which is GROSS demand -- BTM solar has been removed
from the demand side because RESOLVE models it as a supply resource (Customer_PV).

To compare RESOLVE against EIA-930 and IEPR (both net-of-BTM), this script
subtracts RESOLVE's own native Customer_PV profiles:

    resolve_net_mw = demand_mw_2024scaled − weather_factor × planned_capacity_2024

where:
  weather_factor   -- hourly solar capacity factor ("Weather Factor" column, values 0-1)
                      from RESOLVE_RAW/data/profiles/pmax/2025/{UTIL}_Customer_PV.csv
                      (23 weather years 2000-2022; these are RESOLVE's native BTM PV
                      generation profiles — their provenance within RESOLVE's modelling
                      workflow is not independently documented in publicly available files,
                      but the column name and 0-1 range are consistent with a capacity
                      factor profile).
                      varies day to day based on cloud cover / irradiance)
  planned_capacity -- installed BTM PV capacity (MW) from
                      RESOLVE_RAW/data/interim/resources/{UTIL}_Customer_PV.csv
                      attribute=planned_capacity, year=2024, scenario=2024_IEPR_Local_Reliability.
                      This scenario is the CPUC 2024-2026 IRP local reliability planning case.
                      Values (2024): PGE=9,669 MW, SCE=6,553 MW, SDGE=2,463 MW (from file rows).
                      (PGE: 9,669 MW; SCE: 6,553 MW; SDGE: 2,463 MW)

This uses RESOLVE's own internal BTM model, NOT the IEPR BTM_PV fixed monthly
template.  The two approaches give nearly identical means (~13 GW peak at July noon)
but RESOLVE's weather-year profiles add realistic day-to-day solar variability that
the fixed IEPR template cannot capture.

Figures produced:
  Fig 1  -- 12-panel monthly load profiles (hour vs MW)
  Fig 2  -- Coverage ratio heatmap: substation_max / EIA_mean by (month, hour)
  Fig 3  -- Utility-level breakdown: PGE / SCE / SDGE vs IEPR
  Fig 4  -- Monthly peak demand: substation vs EIA vs IEPR
"""

from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd
from scipy import stats as _stats

# ---------------------------------------------------------------------------
# Timezone helper
# ---------------------------------------------------------------------------

def _utc_to_pst(ts: pd.Series) -> pd.Series:
    """Convert EIA-930 UTC hour-ending timestamps to fixed PST (UTC-8) hour-beginning labels.

    EIA-930 uses hour-ENDING UTC convention per filing instructions (confirmed empirically:
    EIA API period T06:00Z maps to "hour ending 1:00 AM EST" in EIA documentation; PUDL
    preserves the same timestamps — they agree to <0.001% at identical UTC timestamps).

    Subtracting 9 hours = 8h UTC-to-PST offset + 1h hour-ending-to-beginning conversion.
    This aligns EIA hours with IEPR (HOUR−1, hour-beginning 0–23) and substation
    hour_pst (fixed PST, hour-beginning) for correct shape and peak-hour comparison.

    Without this correction the hourly peak analysis would show a systematic 1-hour bias
    (EIA peaks appearing 1 hour later than the equivalent IEPR/substation hour label).
    Annual and monthly totals are unaffected by this convention.
    """
    if ts.dt.tz is not None:
        ts = ts.dt.tz_localize(None)
    return ts - pd.Timedelta(hours=9)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
FIGS = ROOT / "data" / "figures"
FIGS.mkdir(parents=True, exist_ok=True)

SUBS_FILE  = PROC / "substations" / "substation_load_profiles_clean.csv"
EIA_FILE   = PROC / "eia" / "eia930_operations.csv"
CAL_FILE   = PROC / "eia" / "eia930_cal_region_PUDL.csv"
IEPR_FILE  = PROC / "iepr" / "iepr_hourly_forecast.csv"
REEDS_FILE = PROC / "reeds" / "reeds_ca_load_hourly.parquet"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EIA_BA        = "CISO"
IEPR_UTILS    = ["PGE", "SCE", "SDGE"]
IEPR_SCENARIO = "Local_Reliability"
IEPR_COL      = "BASELINE_NET_LOAD"

VINTAGE_COLORS = {2023: "#1f77b4", 2024: "#ff7f0e", 2025: "#2ca02c"}
UTIL_COLORS    = {"pge": "#e41a1c", "sce": "#377eb8", "sdge": "#4daf4a"}
CAL_COLOR      = "#9467bd"   # purple — EIA CAL region
RESOLVE_COLOR  = "#8c564b"   # brown  — RESOLVE weather-year ensemble
REEDS_COLOR    = "#7f7f7f"   # gray   — ReEDS IRA_low
RESOLVE_FILE   = PROC / "resolve" / "resolve_hourly_profiles.csv"
RESOLVE_UTILS  = ["PGE", "SCE", "SDGE"]   # match IEPR scope
RESOLVE_RAW    = (ROOT / "data" / "raw" /
                  "RESOLVE Code Base and Inputs" /
                  "RESOLVE Code Base and Inputs")

FIGS_UTILITY = FIGS / "utility_breakdown"
FIGS_UTILITY.mkdir(parents=True, exist_ok=True)

FIGS_SHIFT = FIGS / "peak_hour_shift"
FIGS_SHIFT.mkdir(parents=True, exist_ok=True)

MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

# For IEPR: use year 2024 from vintages 2023/2024; year 2025 from vintage 2025
IEPR_REPR_YEAR = {2023: 2024, 2024: 2024, 2025: 2025}


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_reeds_month_hour(target_year: int = 2025) -> pd.DataFrame:
    """
    Load ReEDS CA total (p8+p9+p10+p11) mean hourly load by (month, hour),
    averaged across all 7 weather years for the given target_year.

    Returns DataFrame with columns: month, hour, mean_mw, min_mw, max_mw.
    Time convention: hour 0-23, fixed PST (same as RESOLVE/IEPR).
    """
    if not REEDS_FILE.exists():
        return pd.DataFrame(columns=["month", "hour", "mean_mw", "min_mw", "max_mw"])
    df = pd.read_parquet(REEDS_FILE,
                         filters=[("year", "=", target_year)],
                         columns=["time_index", "weather_year", "region",
                                  "load_mw", "month", "day", "hour"])
    # Step 1: sum across 4 CA regions for each (weather_year, time_index)
    hourly_ca = (df.groupby(["weather_year", "time_index", "month", "day", "hour"])
                   ["load_mw"].sum().reset_index())
    # Step 2: average across days within each (weather_year, month, hour)
    mh_by_wy = (hourly_ca.groupby(["weather_year", "month", "hour"])["load_mw"]
                          .mean().reset_index())
    # Step 3: aggregate across weather years
    agg = (mh_by_wy.groupby(["month", "hour"])["load_mw"]
                   .agg(mean_mw="mean", min_mw="min", max_mw="max")
                   .reset_index())
    # Cast int8 columns (written as int8 in parquet for storage efficiency) to int64
    # so that arithmetic like `hour + offset` doesn't overflow when offset > 127.
    agg["month"] = agg["month"].astype("int64")
    agg["hour"]  = agg["hour"].astype("int64")
    return agg


def load_reeds_daily_peak_hour(target_year: int = 2025) -> pd.DataFrame:
    """
    Extract daily peak hour (argmax of CA total load) per (weather_year, month, day)
    from the ReEDS CA hourly parquet for the given target year.

    Returns DataFrame with columns: weather_year, month, day, peak_hour.
    Falls back to nearest available target year if exact year is missing.
    """
    if not REEDS_FILE.exists():
        return pd.DataFrame(columns=["weather_year", "month", "day", "peak_hour"])
    df = pd.read_parquet(REEDS_FILE, filters=[("year", "=", target_year)],
                         columns=["weather_year", "region", "load_mw",
                                  "month", "day", "hour"])
    if df.empty:
        all_df = pd.read_parquet(REEDS_FILE, columns=["year"]).drop_duplicates()
        available = sorted(all_df["year"].unique())
        nearest = min(available, key=lambda y: abs(y - target_year))
        df = pd.read_parquet(REEDS_FILE, filters=[("year", "=", nearest)],
                             columns=["weather_year", "region", "load_mw",
                                      "month", "day", "hour"])
    # Sum across 4 CA regions per (weather_year, month, day, hour)
    ca = (df.groupby(["weather_year", "month", "day", "hour"])["load_mw"]
            .sum().reset_index())
    # Peak hour = argmax per (weather_year, month, day)
    idx = ca.groupby(["weather_year", "month", "day"])["load_mw"].idxmax()
    peak = (ca.loc[idx, ["weather_year", "month", "day", "hour"]]
              .rename(columns={"hour": "peak_hour"})
              .reset_index(drop=True))
    # Cast int8 columns from parquet to int64 to prevent overflow in downstream arithmetic.
    for col in ["month", "day", "peak_hour"]:
        peak[col] = peak[col].astype("int64")
    return peak


def load_substation_coincident() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns:
        total_coin  -- coincident sum across all utilities by (month, hour)
        util_coin   -- coincident sum by (utility, month, hour)
    hour column is fixed PST (UTC-8, no DST) from hour_pst in the processed CSV.

    SCE profiles are year-stamped (2017–2026). Each year is a distinct 10th/90th
    percentile snapshot from a non-public utility lookback window. Per-cell
    deduplication keeps the most recent vintage for each (substation, month, hour)
    — 2026 only covers Jan-Apr so May-Dec fall back to 2025 for those substations.
    PGE and SDGE have no year stamp (year=NaN) and are used as-is.
    See CLAUDE.md: "Utility Substation Profiles — SCE: use only the most recent year."
    """
    df = pd.read_csv(SUBS_FILE)

    # Defensive dedup: processed CSV should already be deduplicated per
    # (substation, month, hour) keeping max year, but guard against stale files.
    sce_mask = df["utility"] == "sce"
    if sce_mask.any() and df.loc[sce_mask, "year"].nunique() > 1:
        sce_df = df[sce_mask].copy()
        idx_keep = (sce_df
                    .groupby(["substation_name", "month", "hour_pst"])["year"]
                    .idxmax())
        df = pd.concat([df[~sce_mask], sce_df.loc[idx_keep]], ignore_index=True)
        yrs = sorted(df.loc[df["utility"] == "sce", "year"].unique())
        print(f"  Substation SCE: deduped to most-recent vintage per (sub,month,hour); "
              f"vintages present: {[int(y) for y in yrs]}")

    total_coin = (
        df.groupby(["month", "hour_pst"])[["max_load", "min_load"]]
        .sum()
        .reset_index()
        .rename(columns={"hour_pst": "hour", "max_load": "coin_max_mw", "min_load": "coin_min_mw"})
    )
    util_coin = (
        df.groupby(["utility", "month", "hour_pst"])[["max_load", "min_load"]]
        .sum()
        .reset_index()
        .rename(columns={"hour_pst": "hour", "max_load": "coin_max_mw", "min_load": "coin_min_mw"})
    )
    return total_coin, util_coin


def load_eia_ciso() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns:
        mh_stats  -- (month, hour) -> mean/p10/p90/min/max demand across complete years
        yr_mh     -- (year, month, hour) -> mean demand for individual-year overlays
    """
    df = pd.read_csv(EIA_FILE, parse_dates=["datetime_utc"])
    df = df[df["ba_code"] == EIA_BA].dropna(subset=["demand_mwh"]).copy()

    df["dt_pst"] = _utc_to_pst(df["datetime_utc"])
    df["year"]   = df["dt_pst"].dt.year
    df["month"]  = df["dt_pst"].dt.month
    df["hour"]   = df["dt_pst"].dt.hour

    # Keep only complete years (>= 8 500 hours to allow minor gaps)
    yr_counts = df.groupby("year")["demand_mwh"].count()
    complete_years = yr_counts[yr_counts >= 8500].index.tolist()
    df = df[df["year"].isin(complete_years)].copy()
    print(f"EIA CISO complete years used: {complete_years}")

    yr_mh = df.groupby(["year", "month", "hour"])["demand_mwh"].mean().reset_index()

    # Inter-annual statistics at each (month, hour)
    mh_stats = (
        yr_mh.groupby(["month", "hour"])["demand_mwh"]
        .agg(
            eia_mean="mean",
            eia_p10=lambda x: x.quantile(0.10),
            eia_p90=lambda x: x.quantile(0.90),
            eia_min="min",
            eia_max="max",
        )
        .reset_index()
    )
    return mh_stats, yr_mh


def load_cal_region() -> pd.DataFrame:
    """
    Load EIA CAL region demand and aggregate to (month, hour) statistics.
    CAL region is the state-boundary aggregate (no NEVP/PACW) from 2019 onward.
    Returns month-hour mean and inter-annual band, same structure as load_eia_ciso().
    """
    if not CAL_FILE.exists():
        print(f"  WARNING: {CAL_FILE.name} not found -- skipping CAL region.")
        return pd.DataFrame(), pd.DataFrame()

    df = pd.read_csv(CAL_FILE, parse_dates=["datetime_utc"])
    df = df.dropna(subset=["demand_mwh"]).copy()

    df["dt_pst"] = _utc_to_pst(df["datetime_utc"])
    df["year"]   = df["dt_pst"].dt.year
    df["month"]  = df["dt_pst"].dt.month
    df["hour"]   = df["dt_pst"].dt.hour

    yr_counts  = df.groupby("year")["demand_mwh"].count()
    full_years = yr_counts[yr_counts >= 8500].index.tolist()
    df = df[df["year"].isin(full_years)].copy()
    print(f"  EIA CAL region complete years: {full_years}")

    yr_mh = df.groupby(["year", "month", "hour"])["demand_mwh"].mean().reset_index()

    mh_stats = (
        yr_mh.groupby(["month", "hour"])["demand_mwh"]
        .agg(
            cal_mean="mean",
            cal_p10=lambda x: x.quantile(0.10),
            cal_p90=lambda x: x.quantile(0.90),
        )
        .reset_index()
    )
    return mh_stats, yr_mh



def load_resolve_hourly() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load RESOLVE hourly profiles and return inter-annual (month, hour) statistics.

    Uses demand_mw_net from resolve_hourly_profiles.csv (pre-computed by
    process_resolve.py as demand_mw_2024scaled − btm_pv_mw, where btm_pv_mw =
    Customer_PV weather_factor × planned_capacity_2024_Local_Reliability).

    Returns:
        mh_stats  -- (month, hour) -> resolve_mean/p10/p90 across 23 weather years
        yr_mh     -- (year, month, hour) -> mean net demand per weather year
    """
    if not RESOLVE_FILE.exists():
        print(f"  WARNING: {RESOLVE_FILE.name} not found -- skipping RESOLVE.")
        return pd.DataFrame(), pd.DataFrame()

    df = pd.read_csv(RESOLVE_FILE, parse_dates=["datetime_pst"])
    df = df[df["utility"].isin(RESOLVE_UTILS)].copy()

    # Sum net demand across utilities for each timestamp
    hourly = (
        df.groupby("datetime_pst")["demand_mw_net"]
        .sum()
        .reset_index()
        .rename(columns={"demand_mw_net": "resolve_net_mw"})
    )
    hourly["year"]  = hourly["datetime_pst"].dt.year
    hourly["month"] = hourly["datetime_pst"].dt.month
    hourly["day"]   = hourly["datetime_pst"].dt.day
    hourly["hour"]  = hourly["datetime_pst"].dt.hour

    # Per weather-year: mean across days at each (month, hour)
    yr_mh = (
        hourly.groupby(["year", "month", "hour"])["resolve_net_mw"]
        .mean()
        .reset_index()
        .rename(columns={"resolve_net_mw": "resolve_mw"})
    )

    # Inter-annual statistics at each (month, hour)
    mh_stats = (
        yr_mh.groupby(["month", "hour"])["resolve_mw"]
        .agg(
            resolve_mean="mean",
            resolve_p10=lambda x: x.quantile(0.10),
            resolve_p90=lambda x: x.quantile(0.90),
        )
        .reset_index()
    )
    print(
        f"  RESOLVE PGE+SCE+SDGE net: {yr_mh['year'].nunique()} weather years, "
        f"peak mean = {mh_stats['resolve_mean'].max():,.0f} MW"
    )
    return mh_stats, yr_mh


def load_iepr_hourly() -> pd.DataFrame:
    """
    Aggregate IEPR hourly data to (vintage, month, hour) by:
      1. Filtering to Local_Reliability, CAISO utilities (PGE+SCE+SDGE)
      2. Selecting a representative year per vintage (IEPR_REPR_YEAR)
      3. Averaging BASELINE_NET_LOAD across days within each (utility, month, hour)
      4. Summing across utilities

    Returns DataFrame with columns: vintage, month, hour0, iepr_total_mw
    Also returns per-utility DataFrame: vintage, utility, month, hour0, iepr_mw
    """
    df = pd.read_csv(IEPR_FILE)
    df = df[
        (df["utility_ba"].isin(IEPR_UTILS)) &
        (df["scenario"] == IEPR_SCENARIO)
    ].copy()

    # IEPR HOUR is 1-24 (hour-ending); convert to hour-beginning 0-23
    df["hour0"] = df["HOUR"] - 1

    results_total = []
    results_util  = []

    for vintage, repr_year in IEPR_REPR_YEAR.items():
        sub = df[(df["forecast_vintage_year"] == vintage) & (df["YEAR"] == repr_year)]
        if sub.empty:
            print(f"  IEPR vintage {vintage}: no data for year {repr_year}, skipping")
            continue

        # Average across days for each (utility, month, hour)
        util_mh = (
            sub.groupby(["utility_ba", "MONTH", "hour0"])[IEPR_COL]
            .mean()
            .reset_index()
            .rename(columns={"MONTH": "month", IEPR_COL: "iepr_mw"})
        )
        util_mh["vintage"] = vintage
        results_util.append(util_mh)

        # Sum across utilities
        total_mh = (
            util_mh.groupby(["month", "hour0"])["iepr_mw"]
            .sum()
            .reset_index()
            .rename(columns={"iepr_mw": "iepr_total_mw"})
        )
        total_mh["vintage"] = vintage
        results_total.append(total_mh)
        print(
            f"  IEPR vintage {vintage} (year {repr_year}): "
            f"peak total = {total_mh['iepr_total_mw'].max():,.0f} MW"
        )

    iepr_total = pd.concat(results_total, ignore_index=True)
    iepr_util  = pd.concat(results_util,  ignore_index=True)
    return iepr_total, iepr_util


# ---------------------------------------------------------------------------
# Figure 1: Monthly 24-hour profiles (3x4 grid)
# ---------------------------------------------------------------------------

def fig_monthly_profiles(
    total_coin:    pd.DataFrame,
    mh_stats:      pd.DataFrame,
    iepr_total:    pd.DataFrame,
    cal_stats:     pd.DataFrame,
    resolve_stats: pd.DataFrame | None = None,
    reeds_mh:      pd.DataFrame | None = None,
    sharey:        bool = False,
    out_suffix:    str = "",
) -> None:
    fig, axes = plt.subplots(3, 4, figsize=(18, 12), sharey=sharey)
    axes = axes.flatten()

    hours = np.arange(24)

    for m in range(1, 13):
        ax = axes[m - 1]

        # Substation coincident band
        s = total_coin[total_coin["month"] == m].sort_values("hour")
        ax.fill_between(
            s["hour"], s["coin_min_mw"], s["coin_max_mw"],
            alpha=0.3, color="grey", label="Substation (min-max)" if m == 1 else "_",
        )
        ax.plot(s["hour"], s["coin_max_mw"], color="grey", lw=1.2)
        ax.plot(s["hour"], s["coin_min_mw"], color="grey", lw=1.2, linestyle="--")

        # EIA CISO inter-annual band + mean
        e = mh_stats[mh_stats["month"] == m].sort_values("hour")
        ax.fill_between(
            e["hour"], e["eia_p10"], e["eia_p90"],
            alpha=0.25, color="#1f77b4", label="EIA CISO (p10-p90)" if m == 1 else "_",
        )
        ax.plot(
            e["hour"], e["eia_mean"],
            color="#1f77b4", lw=2, label="EIA CISO (mean)" if m == 1 else "_",
        )

        # IEPR by vintage
        for vintage, color in VINTAGE_COLORS.items():
            iv = iepr_total[(iepr_total["vintage"] == vintage) & (iepr_total["month"] == m)]
            if iv.empty:
                continue
            iv = iv.sort_values("hour0")
            ax.plot(
                iv["hour0"], iv["iepr_total_mw"],
                color=color, lw=1.8, linestyle=":",
                label=f"IEPR v{vintage}" if m == 1 else "_",
            )

        # EIA CAL region
        if not cal_stats.empty:
            c = cal_stats[cal_stats["month"] == m].sort_values("hour")
            ax.fill_between(
                c["hour"], c["cal_p10"], c["cal_p90"],
                alpha=0.20, color=CAL_COLOR,
                label="EIA CAL region (p10-p90)" if m == 1 else "_",
            )
            ax.plot(
                c["hour"], c["cal_mean"],
                color=CAL_COLOR, lw=2, linestyle="-.",
                label="EIA CAL region (mean)" if m == 1 else "_",
            )

        # RESOLVE inter-annual band + mean (23 weather years at 2024 demand scale)
        if resolve_stats is not None and not resolve_stats.empty:
            rv = resolve_stats[resolve_stats["month"] == m].sort_values("hour")
            ax.fill_between(
                rv["hour"], rv["resolve_p10"], rv["resolve_p90"],
                alpha=0.20, color=RESOLVE_COLOR,
                label="RESOLVE net (p10–p90, 23 wx yrs)" if m == 1 else "_",
            )
            ax.plot(
                rv["hour"], rv["resolve_mean"],
                color=RESOLVE_COLOR, lw=2,
                label="RESOLVE net (mean, BTM_PV subtracted)" if m == 1 else "_",
            )

        # ReEDS IRA_low: mean + min/max band across 7 weather years
        if reeds_mh is not None and not reeds_mh.empty:
            rd = reeds_mh[reeds_mh["month"] == m].sort_values("hour")
            ax.fill_between(
                rd["hour"], rd["min_mw"], rd["max_mw"],
                alpha=0.15, color=REEDS_COLOR,
                label="ReEDS IRA_low CA total (min-max, 7 wx yrs)" if m == 1 else "_",
            )
            ax.plot(
                rd["hour"], rd["mean_mw"],
                color=REEDS_COLOR, lw=1.8, ls="--",
                label="ReEDS IRA_low CA total (mean, p8+p9+p10+p11)" if m == 1 else "_",
            )

        ax.set_title(MONTH_NAMES[m - 1], fontsize=11)
        ax.set_xlim(0, 23)
        ax.set_xlabel("Hour (Pacific)" if m >= 9 else "")
        ax.set_ylabel("MW" if m % 4 == 1 else "")
        ax.tick_params(labelsize=8)
        ax.grid(True, alpha=0.3)

    # Single legend at bottom
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles, labels,
        loc="lower center", ncol=6, fontsize=9,
        bbox_to_anchor=(0.5, -0.02),
    )
    shared_note = "  [shared y-axis]" if sharey else ""
    fig.suptitle(
        f"Monthly 24-Hour Load Profiles: Substation vs EIA CISO vs IEPR vs RESOLVE vs ReEDS{shared_note}\n"
        "Substation = PGE+SCE+SDGE distribution substations; "
        "EIA = CISO BA realized demand; IEPR = BASELINE_NET_LOAD (Local_Reliability); "
        "RESOLVE = PGE+SCE+SDGE weather-year ensemble (2024 scale); "
        "ReEDS = IRA_low CA total (p8+p9+p10+p11, 2025 target year, 7 weather years)",
        fontsize=10, y=1.01,
    )
    plt.tight_layout()
    out = FIGS / f"substation_vs_eia_iepr_monthly_profiles{out_suffix}.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved: {out}")


# ---------------------------------------------------------------------------
# Figure 2: Coverage ratio heatmap (substation max / EIA mean)
# ---------------------------------------------------------------------------

def fig_coverage_heatmap(
    total_coin: pd.DataFrame,
    mh_stats:   pd.DataFrame,
    cal_stats:  pd.DataFrame,
) -> None:
    has_cal = not cal_stats.empty

    def _make_heatmap(ax, piv, title, vmin, vmax):
        im = ax.imshow(piv.values, aspect="auto", origin="upper",
                       cmap="RdYlGn", vmin=vmin, vmax=vmax)
        ax.set_xticks(range(24))
        ax.set_xticklabels(range(24), fontsize=7)
        ax.set_yticks(range(12))
        ax.set_yticklabels(MONTH_NAMES, fontsize=8)
        ax.set_xlabel("Hour (Pacific)")
        ax.set_title(title, fontsize=9)
        plt.colorbar(im, ax=ax, label="Ratio")
        for r in range(12):
            for c in range(24):
                val = piv.values[r, c]
                if not np.isnan(val):
                    ax.text(c, r, f"{val:.2f}", ha="center", va="center",
                            fontsize=5, color="black" if 0.6 < val < 1.8 else "white")

    n_cols = 4 if has_cal else 2
    fig, axes = plt.subplots(1, n_cols, figsize=(8 * n_cols, 5))
    if n_cols == 2:
        axes = list(axes)

    merged_ciso = total_coin.merge(mh_stats, on=["month", "hour"])
    merged_ciso["cov_max"] = merged_ciso["coin_max_mw"] / merged_ciso["eia_mean"]
    merged_ciso["cov_min"] = merged_ciso["coin_min_mw"] / merged_ciso["eia_mean"]

    _make_heatmap(axes[0],
                  merged_ciso.pivot(index="month", columns="hour", values="cov_max"),
                  "High-Load Day / EIA CISO Mean", 0.8, 2.2)
    _make_heatmap(axes[1],
                  merged_ciso.pivot(index="month", columns="hour", values="cov_min"),
                  "Low-Load Day / EIA CISO Mean", 0.3, 1.2)

    if has_cal:
        merged_cal = total_coin.merge(cal_stats, on=["month", "hour"])
        merged_cal["cov_max"] = merged_cal["coin_max_mw"] / merged_cal["cal_mean"]
        merged_cal["cov_min"] = merged_cal["coin_min_mw"] / merged_cal["cal_mean"]
        _make_heatmap(axes[2],
                      merged_cal.pivot(index="month", columns="hour", values="cov_max"),
                      "High-Load Day / EIA CAL Region Mean", 0.8, 2.2)
        _make_heatmap(axes[3],
                      merged_cal.pivot(index="month", columns="hour", values="cov_min"),
                      "Low-Load Day / EIA CAL Region Mean", 0.3, 1.2)

    fig.suptitle(
        "Substation Coincident Sum / EIA Reference -- Coverage Ratio\n"
        "Left pair vs CISO BA; right pair vs CAL state-boundary region (2019+)\n"
        "Systematic ratio pattern confirms gap is from missing substations",
        fontsize=10, y=1.04,
    )
    plt.tight_layout()
    out = FIGS / "substation_vs_eia_coverage_heatmap.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved: {out}")


# ---------------------------------------------------------------------------
# Figure 3: Utility-level breakdown vs IEPR
# ---------------------------------------------------------------------------

def fig_utility_breakdown(
    util_coin:  pd.DataFrame,
    iepr_util:  pd.DataFrame,
    month:      int = 8,
) -> None:
    """Summer month profile (default August) per utility."""
    util_map_display = {"pge": "PGE", "sce": "SCE", "sdge": "SDGE"}
    fig, axes = plt.subplots(1, 3, figsize=(16, 5), sharey=False)

    for ax, (util_lower, util_upper) in zip(axes, util_map_display.items()):
        # Substation
        s = util_coin[
            (util_coin["utility"] == util_lower) & (util_coin["month"] == month)
        ].sort_values("hour")
        ax.fill_between(
            s["hour"], s["coin_min_mw"], s["coin_max_mw"],
            alpha=0.3, color="grey", label="Substation (min-max)",
        )
        ax.plot(s["hour"], s["coin_max_mw"], color="grey", lw=1.5)
        ax.plot(s["hour"], s["coin_min_mw"], color="grey", lw=1.5, linestyle="--")

        # IEPR by vintage
        for vintage, color in VINTAGE_COLORS.items():
            iv = iepr_util[
                (iepr_util["utility_ba"] == util_upper) &
                (iepr_util["vintage"] == vintage) &
                (iepr_util["month"] == month)
            ].sort_values("hour0")
            if iv.empty:
                continue
            ax.plot(
                iv["hour0"], iv["iepr_mw"],
                color=color, lw=2, linestyle=":",
                label=f"IEPR v{vintage} ({IEPR_REPR_YEAR[vintage]})",
            )

        ax.set_title(f"{util_upper} -- {MONTH_NAMES[month-1]}", fontsize=11)
        ax.set_xlabel("Hour (Pacific)")
        ax.set_ylabel("MW")
        ax.set_xlim(0, 23)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)

    fig.suptitle(
        f"Utility-Level Substation Coincident Sum vs IEPR BASELINE_NET_LOAD -- {MONTH_NAMES[month-1]}\n"
        "Grey band = substation min/max profile; dashed colored lines = IEPR projections\n"
        "Gap shows load not captured in substation data (redacted, non-IOU, etc.)",
        fontsize=10,
    )
    plt.tight_layout()
    out = FIGS_UTILITY / f"substation_vs_iepr_utility_{MONTH_NAMES[month-1].lower()}.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved: {out}")


# ---------------------------------------------------------------------------
# Figure 4: Monthly peak demand comparison
# ---------------------------------------------------------------------------

def fig_monthly_peaks(
    total_coin: pd.DataFrame,
    mh_stats:   pd.DataFrame,
    iepr_total: pd.DataFrame,
    cal_stats:  pd.DataFrame,
) -> None:
    months = range(1, 13)
    has_cal = not cal_stats.empty

    sub_peaks = [total_coin[total_coin["month"] == m]["coin_max_mw"].max() for m in months]
    sub_off   = [total_coin[total_coin["month"] == m]["coin_min_mw"].max() for m in months]
    eia_mean  = [mh_stats[mh_stats["month"] == m]["eia_mean"].max() for m in months]
    eia_p90   = [mh_stats[mh_stats["month"] == m]["eia_p90"].max() for m in months]
    cal_mean  = (
        [cal_stats[cal_stats["month"] == m]["cal_mean"].max() for m in months]
        if has_cal else []
    )
    cal_p90   = (
        [cal_stats[cal_stats["month"] == m]["cal_p90"].max() for m in months]
        if has_cal else []
    )

    fig, ax = plt.subplots(figsize=(14, 5))
    x = np.arange(12)
    n_bars = 4 if has_cal else 3
    width  = 0.16 if has_cal else 0.18
    offset = np.linspace(-(n_bars - 1) / 2 * width, (n_bars - 1) / 2 * width, n_bars)

    ax.bar(x + offset[0], sub_peaks, width, label="Substation coincident max",
           color="grey",     alpha=0.8)
    ax.bar(x + offset[1], sub_off,   width, label="Substation coincident min",
           color="lightgrey", alpha=0.8)
    ax.bar(x + offset[2], eia_mean,  width, label="EIA CISO mean peak",
           color="#1f77b4",  alpha=0.8)
    eia_err = [p90 - mn for p90, mn in zip(eia_p90, eia_mean)]
    ax.errorbar(x + offset[2], eia_mean, yerr=[np.zeros(12), eia_err],
                fmt="none", color="#1f77b4", capsize=4, lw=1.5, label="EIA CISO p90")

    if has_cal:
        ax.bar(x + offset[3], cal_mean, width, label="EIA CAL region mean peak",
               color=CAL_COLOR, alpha=0.8)
        cal_err = [p90 - mn for p90, mn in zip(cal_p90, cal_mean)]
        ax.errorbar(x + offset[3], cal_mean, yerr=[np.zeros(12), cal_err],
                    fmt="none", color=CAL_COLOR, capsize=4, lw=1.5, label="EIA CAL p90")

    for vintage, color in VINTAGE_COLORS.items():
        peaks = []
        for m in months:
            iv = iepr_total[(iepr_total["vintage"] == vintage) & (iepr_total["month"] == m)]
            peaks.append(iv["iepr_total_mw"].max() if not iv.empty else np.nan)
        ax.plot(x, peaks, color=color, marker="o", lw=2,
                label=f"IEPR v{vintage} peak ({IEPR_REPR_YEAR[vintage]})")

    ax.set_xticks(x)
    ax.set_xticklabels(MONTH_NAMES)
    ax.set_ylabel("MW")
    ax.set_title(
        "Monthly Peak Demand: Substation Coincident Sum vs EIA CISO vs EIA CAL vs IEPR\n"
        "CAL region (state boundary) vs CISO (BA boundary, incl. some non-CA load); "
        "IEPR dotted lines compare to EIA p90",
        fontsize=10,
    )
    ax.legend(fontsize=8, ncol=4)
    ax.grid(True, alpha=0.3, axis="y")
    plt.tight_layout()
    out = FIGS / "substation_vs_eia_iepr_monthly_peaks.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved: {out}")


# ---------------------------------------------------------------------------
# Figure 5 (shared y) — thin wrapper
# ---------------------------------------------------------------------------

def fig_monthly_profiles_shared_y(
    total_coin:    pd.DataFrame,
    mh_stats:      pd.DataFrame,
    iepr_total:    pd.DataFrame,
    cal_stats:     pd.DataFrame,
    resolve_stats: pd.DataFrame | None = None,
    reeds_mh:      pd.DataFrame | None = None,
) -> None:
    """Same as fig_monthly_profiles but all panels share the same y-axis scale."""
    fig_monthly_profiles(
        total_coin, mh_stats, iepr_total, cal_stats,
        resolve_stats=resolve_stats, reeds_mh=reeds_mh,
        sharey=True, out_suffix="_shared_y",
    )


# ---------------------------------------------------------------------------
# Figure 6: Annual profile — all 12 months on one x-axis
# ---------------------------------------------------------------------------

def fig_annual_profile(
    total_coin:    pd.DataFrame,
    mh_stats:      pd.DataFrame,
    iepr_total:    pd.DataFrame,
    cal_stats:     pd.DataFrame,
    resolve_stats: pd.DataFrame | None = None,
    reeds_mh:      pd.DataFrame | None = None,
) -> None:
    """Single figure: all 12 months concatenated on one x-axis, shared y-scale."""
    fig, ax = plt.subplots(figsize=(28, 6))

    # Track legend entries so each series appears once
    _seen: set[str] = set()

    def _lab(key: str, text: str) -> str:
        if key in _seen:
            return "_"
        _seen.add(key)
        return text

    for m in range(1, 13):
        offset = (m - 1) * 24

        # Substation coincident band
        s  = total_coin[total_coin["month"] == m].sort_values("hour")
        xh = s["hour"].values + offset
        ax.fill_between(xh, s["coin_min_mw"], s["coin_max_mw"],
                        alpha=0.20, color="grey",
                        label=_lab("sub_band", "Substation (min–max)"))
        ax.plot(xh, s["coin_max_mw"], color="grey", lw=1.0)
        ax.plot(xh, s["coin_min_mw"], color="grey", lw=1.0, linestyle="--")

        # EIA CISO inter-annual band + mean
        e  = mh_stats[mh_stats["month"] == m].sort_values("hour")
        xh = e["hour"].values + offset
        ax.fill_between(xh, e["eia_p10"], e["eia_p90"],
                        alpha=0.20, color="#1f77b4",
                        label=_lab("eia_band", "EIA CISO (p10–p90)"))
        ax.plot(xh, e["eia_mean"], color="#1f77b4", lw=1.8,
                label=_lab("eia_mean", "EIA CISO (mean)"))

        # IEPR by vintage
        for vintage, color in VINTAGE_COLORS.items():
            iv = iepr_total[
                (iepr_total["vintage"] == vintage) & (iepr_total["month"] == m)
            ]
            if iv.empty:
                continue
            iv = iv.sort_values("hour0")
            ax.plot(iv["hour0"].values + offset, iv["iepr_total_mw"],
                    color=color, lw=1.6, linestyle=":",
                    label=_lab(f"iepr{vintage}", f"IEPR v{vintage}"))

        # EIA CAL region
        if not cal_stats.empty:
            c  = cal_stats[cal_stats["month"] == m].sort_values("hour")
            xh = c["hour"].values + offset
            ax.fill_between(xh, c["cal_p10"], c["cal_p90"],
                            alpha=0.15, color=CAL_COLOR,
                            label=_lab("cal_band", "EIA CAL (p10–p90)"))
            ax.plot(xh, c["cal_mean"], color=CAL_COLOR, lw=1.8, linestyle="-.",
                    label=_lab("cal_mean", "EIA CAL (mean)"))

        # RESOLVE inter-annual band + mean
        if resolve_stats is not None and not resolve_stats.empty:
            rv = resolve_stats[resolve_stats["month"] == m].sort_values("hour")
            xh = rv["hour"].values + offset
            ax.fill_between(xh, rv["resolve_p10"], rv["resolve_p90"],
                            alpha=0.15, color=RESOLVE_COLOR,
                            label=_lab("res_band", "RESOLVE net (p10–p90)"))
            ax.plot(xh, rv["resolve_mean"], color=RESOLVE_COLOR, lw=1.8,
                    label=_lab("res_mean", "RESOLVE net (mean)"))

        # ReEDS IRA_low CA total: mean line + weather-year min/max band
        if reeds_mh is not None and not reeds_mh.empty:
            rd = reeds_mh[reeds_mh["month"] == m].sort_values("hour")
            xh = rd["hour"].values + offset
            ax.fill_between(xh, rd["min_mw"], rd["max_mw"],
                            alpha=0.12, color=REEDS_COLOR,
                            label=_lab("reeds_band", "ReEDS IRA_low CA (wx-yr range)"))
            ax.plot(xh, rd["mean_mw"], color=REEDS_COLOR, lw=1.8, ls="--",
                    label=_lab("reeds_mean", "ReEDS IRA_low CA (mean)"))

        # Vertical separator between months
        if m < 12:
            ax.axvline(offset + 23.5, color="k", lw=0.8, linestyle="--", alpha=0.4)

        # Light hour markers at 6, 12, 18 within each month block
        for _h in [6, 12, 18]:
            ax.axvline(offset + _h, color="gray", lw=0.5, ls=":", alpha=0.35, zorder=0)

    # Two-tier x-axis: minor ticks = hour markers; major ticks = month names below
    from matplotlib.ticker import FixedLocator, FixedFormatter

    # Minor: hours 6, 12, 18, 24 within each month (24 placed at position 23 — last data point)
    minor_pos  = [(m - 1) * 24 + h for m in range(1, 13) for h in [6, 12, 18, 23]]
    minor_labs = ["6", "12", "18", "24"] * 12
    ax.xaxis.set_minor_locator(FixedLocator(minor_pos))
    ax.xaxis.set_minor_formatter(FixedFormatter(minor_labs))
    ax.tick_params(axis="x", which="minor", length=4, labelsize=6,
                   color="gray", labelcolor="gray")

    # Major: month names at block midpoints, padded below the hour labels
    ax.xaxis.set_major_locator(FixedLocator([(m - 1) * 24 + 11.5 for m in range(1, 13)]))
    ax.xaxis.set_major_formatter(FixedFormatter(MONTH_NAMES))
    ax.tick_params(axis="x", which="major", length=0, labelsize=10, pad=16)

    ax.set_xlim(-0.5, 287.5)
    ax.set_ylabel("MW")
    ax.set_title(
        "Annual 24-Hour Load Profile: Jan–Dec on One Axis (shared y-scale)\n"
        "Substation coincident sum | EIA CISO | EIA CAL | IEPR | RESOLVE (PGE+SCE+SDGE) | ReEDS IRA_low CA",
        fontsize=11,
    )
    ax.grid(True, alpha=0.2, axis="y")
    ax.legend(fontsize=8, ncol=5, loc="upper right")
    plt.tight_layout()
    out = FIGS / "substation_vs_eia_iepr_annual_profile.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved: {out}")


# ---------------------------------------------------------------------------
# Peak-hour shift analysis: IEPR vs EIA (Figure 7)
# ---------------------------------------------------------------------------

def analyze_peak_shift(
    yr_mh_eia:  pd.DataFrame,
    iepr_total: pd.DataFrame,
) -> pd.DataFrame:
    """
    For each (month, eia_year, iepr_vintage) triple, compute:
        shift = IEPR_peak_hour - EIA_peak_hour

    Negative shift = IEPR peaks earlier than EIA ("IEPR leads").

    Returns shift_df with columns:
        month | eia_year | vintage | eia_peak_hour | iepr_peak_hour | shift
    """
    # EIA: per (year, month) find the hour with maximum mean demand
    eia_idx = yr_mh_eia.groupby(["year", "month"])["demand_mwh"].idxmax()
    eia_peaks = (
        yr_mh_eia.loc[eia_idx, ["year", "month", "hour"]]
        .rename(columns={"year": "eia_year", "hour": "eia_peak_hour"})
        .reset_index(drop=True)
    )

    # IEPR: per (vintage, month) find the hour with maximum total demand
    iepr_idx = iepr_total.groupby(["vintage", "month"])["iepr_total_mw"].idxmax()
    iepr_peaks = (
        iepr_total.loc[iepr_idx, ["vintage", "month", "hour0"]]
        .rename(columns={"hour0": "iepr_peak_hour"})
        .reset_index(drop=True)
    )

    # Cross-join on month to get all (eia_year × vintage) combinations per month
    shift_df = eia_peaks.merge(iepr_peaks, on="month")
    shift_df["shift"] = shift_df["iepr_peak_hour"] - shift_df["eia_peak_hour"]
    return shift_df


def print_shift_summary(shift_df: pd.DataFrame) -> None:
    print("\n--- IEPR vs EIA-CISO Peak-Hour Shift Summary ---")
    print("  Shift = IEPR_peak_hour - EIA_peak_hour  (negative = IEPR peaks earlier)")

    o = shift_df["shift"]
    print(
        f"\n  Overall:  mean={o.mean():+.2f}h  median={o.median():+.2f}h  "
        f"std={o.std():.2f}h  range=[{int(o.min()):+d}, {int(o.max()):+d}]h"
    )

    print("\n  By month:")
    for m in range(1, 13):
        sub = shift_df[shift_df["month"] == m]["shift"]
        if sub.empty:
            continue
        print(
            f"    {MONTH_NAMES[m-1]:>3}:  mean={sub.mean():+.1f}h  "
            f"std={sub.std():.1f}h  range=[{int(sub.min()):+d}, {int(sub.max()):+d}]h"
        )

    print("\n  By IEPR vintage:")
    for v in sorted(shift_df["vintage"].unique()):
        sub = shift_df[shift_df["vintage"] == v]["shift"]
        print(
            f"    v{v}:  mean={sub.mean():+.1f}h  std={sub.std():.1f}h  "
            f"range=[{int(sub.min()):+d}, {int(sub.max()):+d}]h"
        )

    print("\n  By EIA year:")
    for y in sorted(shift_df["eia_year"].unique()):
        sub = shift_df[shift_df["eia_year"] == y]["shift"]
        print(
            f"    {y}:  mean={sub.mean():+.1f}h  std={sub.std():.1f}h  "
            f"range=[{int(sub.min()):+d}, {int(sub.max()):+d}]h"
        )


def fig_peak_shift_distributions(shift_df: pd.DataFrame) -> None:
    """
    Figure 7: Distribution of IEPR vs EIA peak-hour shift — three panels.

    A) By month   — shift distribution across (vintage × EIA year)
    B) By vintage — shift distribution across (month × EIA year)
    C) By EIA year — shift distribution across (month × vintage)
    """
    vintage_list = sorted(shift_df["vintage"].unique())
    eia_years    = sorted(shift_df["eia_year"].unique())

    fig, axes = plt.subplots(1, 3, figsize=(18, 6))

    def _violin(ax, groups, positions, labels, title, xlabel):
        data = [g.values for g in groups]
        if any(len(d) > 1 for d in data):
            vp = ax.violinplot(data, positions=positions, showmedians=True, showextrema=True)
            for body in vp["bodies"]:
                body.set_facecolor("#4878cf")
                body.set_alpha(0.45)
            vp["cmedians"].set_color("black")
        # Overlay scatter jitter for visibility
        rng = np.random.default_rng(42)
        for pos, d in zip(positions, data):
            jitter = rng.uniform(-0.15, 0.15, size=len(d))
            ax.scatter(np.full(len(d), pos) + jitter, d,
                       s=12, color="steelblue", alpha=0.5, zorder=3)
        ax.axhline(0, color="black", lw=0.9, linestyle="--", alpha=0.6)
        ax.set_xticks(positions)
        ax.set_xticklabels(labels, fontsize=8)
        ax.set_ylabel("Peak-hour shift (hours)")
        ax.set_title(title, fontsize=10)
        ax.set_xlabel(xlabel)
        ax.grid(True, alpha=0.3, axis="y")

        mean_all = np.concatenate(data).mean() if data else np.nan
        ax.text(
            0.03, 0.97, f"mean={mean_all:+.1f}h",
            transform=ax.transAxes, va="top", fontsize=8,
            bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.8),
        )

    # Panel A: by month
    month_groups = [shift_df[shift_df["month"] == m]["shift"] for m in range(1, 13)]
    _violin(
        axes[0], month_groups, list(range(1, 13)), MONTH_NAMES,
        "A) By Month\n(across vintage × EIA year)", "Month",
    )

    # Panel B: by IEPR vintage
    vint_groups = [shift_df[shift_df["vintage"] == v]["shift"] for v in vintage_list]
    _violin(
        axes[1], vint_groups, list(range(len(vintage_list))),
        [f"v{v}" for v in vintage_list],
        "B) By IEPR Vintage\n(across month × EIA year)", "IEPR Vintage",
    )

    # Panel C: by EIA year
    year_groups = [shift_df[shift_df["eia_year"] == y]["shift"] for y in eia_years]
    _violin(
        axes[2], year_groups, list(range(len(eia_years))),
        [str(y) for y in eia_years],
        "C) By EIA Year\n(across month × vintage)", "EIA Year",
    )
    axes[2].tick_params(axis="x", rotation=45)

    repr_yr_note = ", ".join(f"v{v}→{y}" for v, y in IEPR_REPR_YEAR.items())
    fig.suptitle(
        "IEPR vs EIA-CISO Peak-Hour Shift  (shift = IEPR peak hour - EIA peak hour; "
        "negative = IEPR peaks earlier)\n"
        "METHOD: argmax of mean monthly profile (not individual days).  "
        f"IEPR representative years: {repr_yr_note}.  "
        "EIA realized years: 2016–2025.\n"
        "Each point = one (month, EIA year, IEPR vintage) combination",
        fontsize=10,
    )
    plt.tight_layout()
    out = FIGS_SHIFT / "iepr_vs_eia_peak_shift_distributions.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved: {out}")


# ---------------------------------------------------------------------------
# Significance table (monthly t-tests)
# ---------------------------------------------------------------------------

# Last historical year per IEPR vintage; projected years start at vintage+1
IEPR_LAST_HIST = {2023: 2023, 2024: 2024, 2025: 2025}


def compute_shift_significance_table(shift_df: pd.DataFrame) -> pd.DataFrame:
    """
    One-sample t-test (H0: mean shift = 0) for each month and overall.

    The shift is defined as: peak hour of IEPR mean monthly profile
    minus peak hour of EIA mean monthly profile (argmax of per-(year,month)
    average hourly demand).  n per month = #vintages × #EIA years.

    Returns a DataFrame with one row per month (plus overall) and columns:
      Month | n | Mean (h) | Median (h) | Std (h) | t-stat | p-value | Sig.
    Prints a formatted table and saves a CSV to data/tables/.
    """
    rows = []
    entries = [("All", shift_df)] + [
        (MONTH_NAMES[m - 1], shift_df[shift_df["month"] == m])
        for m in range(1, 13)
    ]
    for label, sub in entries:
        s = sub["shift"].dropna()
        if len(s) < 2:
            continue
        t, p = _stats.ttest_1samp(s, popmean=0)
        sig = ("***" if p < 0.001 else
               "**"  if p < 0.01  else
               "*"   if p < 0.05  else "ns")
        rows.append({
            "Month":      label,
            "n":          int(len(s)),
            "Mean (h)":   float(s.mean()),
            "Median (h)": float(s.median()),
            "Std (h)":    float(s.std()),
            "t-stat":     float(t),
            "p-value":    float(p),
            "Sig.":       sig,
        })

    tbl = pd.DataFrame(rows)
    n_eia = shift_df["eia_year"].nunique()
    n_vnt = shift_df["vintage"].nunique()

    print("\n--- Peak-Hour Shift Significance Table ---")
    print(f"  Shift = peak of IEPR mean monthly profile - peak of EIA mean monthly profile")
    print(f"  IEPR representative years: {', '.join(f'v{v}->{y}' for v,y in IEPR_REPR_YEAR.items())}")
    print(f"  EIA realized years: {sorted(shift_df['eia_year'].unique())[0]}-"
          f"{sorted(shift_df['eia_year'].unique())[-1]}")
    print(f"  n per month = {n_vnt} vintages x {n_eia} EIA years = {n_vnt*n_eia}")
    print()
    w = 7
    hdr = f"  {'Month':<6} {'n':>4} {'Mean':>{w}} {'Median':>{w}} {'Std':>{w}} "
    hdr += f"{'t-stat':>{w}} {'p-value':>{w+1}} Sig."
    sep = "  " + "-" * (len(hdr) - 2)
    print(hdr)
    print(sep)
    for _, r in tbl.iterrows():
        p_str = f"{r['p-value']:.4f}" if r["p-value"] >= 0.001 else "<0.001"
        if r["Month"] == "All":
            print(sep)
        print(
            f"  {r['Month']:<6} {r['n']:>4} {r['Mean (h)']:>+{w}.2f} "
            f"{r['Median (h)']:>+{w}.2f} {r['Std (h)']:>{w}.2f} "
            f"{r['t-stat']:>+{w}.2f} {p_str:>{w+1}}  {r['Sig.']}"
        )

    out_csv = ROOT / "data" / "tables" / "peak_shift_significance.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    tbl.round(4).to_csv(out_csv, index=False)
    print(f"\n  Saved: data/tables/peak_shift_significance.csv")

    return tbl


def fig_shift_significance_table(sig_df: pd.DataFrame) -> None:
    """Render the significance table as a matplotlib figure."""
    SIG_BG = {"***": "#d4edda", "**": "#cce5ff", "*": "#fff3cd", "ns": "#f8d7da"}
    HDR_BG  = "#343a40"

    col_labels = ["Month", "n", "Mean (h)", "Median (h)", "Std (h)", "t-stat", "p-value", "Sig."]

    cell_text   = []
    cell_colors = []
    for _, r in sig_df.iterrows():
        p_str = f"{r['p-value']:.4f}" if r["p-value"] >= 0.001 else "<0.001"
        bg    = SIG_BG.get(r["Sig."], "white")
        cell_text.append([
            r["Month"],
            str(int(r["n"])),
            f"{r['Mean (h)']:+.2f}",
            f"{r['Median (h)']:+.2f}",
            f"{r['Std (h)']:.2f}",
            f"{r['t-stat']:+.2f}",
            p_str,
            r["Sig."],
        ])
        cell_colors.append([bg] * len(col_labels))

    fig, ax = plt.subplots(figsize=(13, 5.5))
    ax.axis("off")
    tbl = ax.table(
        cellText=cell_text,
        colLabels=col_labels,
        cellColours=cell_colors,
        cellLoc="center",
        loc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10)
    tbl.scale(1.0, 1.55)

    for j in range(len(col_labels)):
        tbl[(0, j)].set_facecolor(HDR_BG)
        tbl[(0, j)].get_text().set_color("white")
        tbl[(0, j)].get_text().set_fontweight("bold")
    # Bold "All" row (row index 1 = first data row)
    for j in range(len(col_labels)):
        tbl[(1, j)].get_text().set_fontweight("bold")

    repr_note = ", ".join(f"v{v}→{y}" for v, y in IEPR_REPR_YEAR.items())
    ax.text(
        0.5, 0.0,
        f"Sig.: *** p<0.001  ** p<0.01  * p<0.05  ns not significant  |  "
        f"IEPR representative years: {repr_note}  |  EIA realized years: 2016–2025",
        transform=ax.transAxes, ha="center", va="bottom", fontsize=8.5, color="#555",
    )
    ax.set_title(
        "IEPR vs EIA-CISO: Peak-Hour Shift Significance by Month\n"
        "Shift = argmax(IEPR mean monthly profile) - argmax(EIA mean monthly profile).  "
        "Negative = IEPR peaks earlier.  One-sample t-test H₀: mean = 0.",
        fontsize=10, pad=14,
    )
    fig.tight_layout()
    out = FIGS_SHIFT / "peak_shift_significance_table.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close(fig)


# ---------------------------------------------------------------------------
# IEPR daily peak hours across all projected years (for evolution analysis)
# ---------------------------------------------------------------------------

def load_eia_ciso_daily_peaks() -> pd.DataFrame:
    """
    Daily peak hour (0-23, US/Pacific) and peak MW from EIA CISO for all
    complete years.  Returns: year | month | date | peak_hour | peak_mw
    """
    df = pd.read_csv(EIA_FILE, usecols=["datetime_utc", "ba_code", "demand_mwh"],
                     parse_dates=["datetime_utc"])
    ciso = df[df["ba_code"] == EIA_BA].copy()
    ciso["dt_pst"] = _utc_to_pst(ciso["datetime_utc"])
    ciso["date"]   = ciso["dt_pst"].dt.normalize()
    ciso["month"]  = ciso["dt_pst"].dt.month
    ciso["year"]   = ciso["dt_pst"].dt.year
    ciso["hour"]   = ciso["dt_pst"].dt.hour
    ciso = ciso.dropna(subset=["demand_mwh"])

    idx   = ciso.groupby("date")["demand_mwh"].idxmax()
    peaks = ciso.loc[idx, ["year", "month", "date", "hour", "demand_mwh"]].copy()
    peaks.columns = ["year", "month", "date", "peak_hour", "peak_mw"]

    yr_counts     = ciso.groupby("year")["demand_mwh"].count()
    complete_yrs  = yr_counts[yr_counts >= 8500].index
    return peaks[peaks["year"].isin(complete_yrs)].reset_index(drop=True)


def load_iepr_all_projected_years() -> pd.DataFrame:
    """
    Load IEPR daily peak hours for ALL projected years and all vintages.

    Sums PGE+SCE+SDGE at each (vintage, year, month, day, hour), finds the
    argmax hour per day, and filters to projected years only
    (year > IEPR_LAST_HIST[vintage]).

    Returns: vintage | year | month | day | peak_hour | peak_mw
    """
    print("  Loading IEPR all projected years (this may take a moment)...")
    df = pd.read_csv(
        IEPR_FILE,
        usecols=["forecast_vintage_year", "utility_ba", "scenario",
                 "YEAR", "MONTH", "DAY", "HOUR", "BASELINE_NET_LOAD"],
    )
    df = df[
        (df["utility_ba"].isin(IEPR_UTILS)) &
        (df["scenario"] == IEPR_SCENARIO)
    ].copy()
    df["hour0"] = df["HOUR"] - 1  # 1-24 → 0-23

    # Sum across utilities → one row per (vintage, year, month, day, hour)
    total = (
        df.groupby(["forecast_vintage_year", "YEAR", "MONTH", "DAY", "hour0"],
                   sort=False)["BASELINE_NET_LOAD"]
        .sum()
        .reset_index()
        .rename(columns={
            "forecast_vintage_year": "vintage",
            "YEAR": "year", "MONTH": "month", "DAY": "day",
            "hour0": "hour",
        })
    )

    # Daily peak: argmax per (vintage, year, month, day)
    idx   = total.groupby(["vintage", "year", "month", "day"])["BASELINE_NET_LOAD"].idxmax()
    peaks = total.loc[idx, ["vintage", "year", "month", "day",
                             "hour", "BASELINE_NET_LOAD"]].copy()
    peaks.columns = ["vintage", "year", "month", "day", "peak_hour", "peak_mw"]

    # Keep only projected years
    pieces = [
        peaks[(peaks["vintage"] == v) & (peaks["year"] > last_h)]
        for v, last_h in IEPR_LAST_HIST.items()
        if v in peaks["vintage"].values
    ]
    result = pd.concat(pieces, ignore_index=True)
    print(f"  IEPR projected daily peaks: {len(result):,} days across "
          f"{result['vintage'].nunique()} vintages, "
          f"years {result['year'].min()}-{result['year'].max()}")
    return result


def fig_iepr_peak_evolution(iepr_daily: pd.DataFrame) -> None:
    """
    Fig: 12 panels (one per month) showing how IEPR's predicted daily peak
    hour evolves across the forecast horizon for each vintage.

    Line  = mean daily peak hour per projected year.
    Shade = ±1 std across days of that month.
    """
    fig, axes = plt.subplots(3, 4, figsize=(18, 12), sharey=True)
    axes = axes.flatten()

    vintages = sorted(iepr_daily["vintage"].unique())

    for m_idx, m in enumerate(range(1, 13)):
        ax  = axes[m_idx]
        sub = iepr_daily[iepr_daily["month"] == m]

        for v in vintages:
            vs = sub[sub["vintage"] == v]
            if vs.empty:
                continue
            yr_grp = vs.groupby("year")["peak_hour"]
            ym = yr_grp.mean().reset_index()
            ys = yr_grp.std().reset_index()
            color = VINTAGE_COLORS.get(v, "gray")
            ax.plot(ym["year"], ym["peak_hour"], color=color, lw=2,
                    marker="o", ms=3,
                    label=f"IEPR v{v}" if m_idx == 0 else "_")
            ax.fill_between(ym["year"],
                            ym["peak_hour"] - ys["peak_hour"],
                            ym["peak_hour"] + ys["peak_hour"],
                            color=color, alpha=0.12)

        ax.set_title(MONTH_NAMES[m - 1], fontsize=10, fontweight="bold")
        if m_idx % 4 == 0:
            ax.set_ylabel("Mean daily peak hour")
        if m_idx >= 8:
            ax.set_xlabel("Projected year")
        ax.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda h, _: f"{int(h):02d}:00" if 0 <= h <= 23 else "")
        )
        ax.set_xlim(
            iepr_daily["year"].min() - 0.5,
            iepr_daily["year"].max() + 0.5,
        )
        ax.grid(alpha=0.25)

    axes[0].legend(fontsize=9, loc="upper left")
    fig.suptitle(
        "IEPR projected daily peak-hour distribution by month across forecast horizon\n"
        "Line = mean daily peak hour per projected year;  shade = ±1 std across days in month\n"
        "Interpretation: does IEPR expect peak demand to shift later over time "
        "as BTM solar grows?",
        fontsize=10,
    )
    fig.tight_layout()
    out = FIGS_SHIFT / "iepr_peak_hour_evolution.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close(fig)


def fig_monthly_peak_distributions(
    iepr_daily: pd.DataFrame,
    eia_daily: pd.DataFrame,
    sig_df: pd.DataFrame,
) -> None:
    """
    Fig: 12 panels (one per month) showing violin distributions of daily peak
    hours — IEPR all projected years (by vintage) vs EIA realized years.

    Panel titles show the mean shift (from sig_df) and significance stars,
    making the table values visually concrete.
    """
    vintages  = sorted(iepr_daily["vintage"].unique())
    eia_color = "#333333"
    rng       = np.random.default_rng(42)

    fig, axes = plt.subplots(3, 4, figsize=(18, 12), sharey=True)
    axes = axes.flatten()

    for m_idx, m in enumerate(range(1, 13)):
        ax = axes[m_idx]

        # Build groups: EIA then each IEPR vintage
        groups = [("EIA\nrealized", eia_daily[eia_daily["month"] == m]["peak_hour"], eia_color)]
        for v in vintages:
            data = iepr_daily[(iepr_daily["vintage"] == v) & (iepr_daily["month"] == m)]["peak_hour"]
            groups.append((f"IEPR\nv{v}", data, VINTAGE_COLORS.get(v, "gray")))

        positions = np.arange(len(groups))
        for pos, (lbl, data, color) in enumerate(groups):
            if data.empty or len(data) < 2:
                continue
            vp = ax.violinplot(data.values, positions=[pos],
                               showmedians=True, showextrema=True)
            for body in vp["bodies"]:
                body.set_facecolor(color)
                body.set_alpha(0.5)
            for part in ["cmedians", "cmaxes", "cmins", "cbars"]:
                vp[part].set_color(color)
            jitter = rng.uniform(-0.1, 0.1, size=min(len(data), 500))
            samp   = data.sample(n=min(len(data), 500), random_state=42)
            ax.scatter(pos + jitter[:len(samp)], samp.values,
                       s=2, alpha=0.25, color=color, zorder=3)

        ax.set_xticks(positions)
        ax.set_xticklabels([g[0] for g in groups], fontsize=7.5)
        ax.set_ylim(-0.5, 23.5)
        ax.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda h, _: f"{int(h):02d}:00" if 0 <= h <= 23 else "")
        )
        ax.grid(alpha=0.2, axis="y")
        if m_idx % 4 == 0:
            ax.set_ylabel("Daily peak hour")

        # Title with shift and significance from the table
        row = sig_df[sig_df["Month"] == MONTH_NAMES[m - 1]]
        if not row.empty:
            mean_s = row["Mean (h)"].iloc[0]
            sig    = row["Sig."].iloc[0]
            ax.set_title(f"{MONTH_NAMES[m-1]}  shift = {mean_s:+.1f} h  {sig}",
                         fontsize=9, fontweight="bold")
        else:
            ax.set_title(MONTH_NAMES[m - 1], fontsize=9, fontweight="bold")

    fig.suptitle(
        "Distribution of daily peak hours by month: IEPR projected years vs EIA-CISO realized\n"
        "IEPR: all projected years (year > last historical year) pooled per vintage\n"
        "EIA: all complete realized years (2016–2025)  |  "
        "Titles show mean shift (IEPR mean-profile peak - EIA mean-profile peak) + significance",
        fontsize=10,
    )
    fig.tight_layout()
    out = FIGS_SHIFT / "iepr_vs_eia_monthly_peak_distributions.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Daily peak-hour comparison: IEPR + RESOLVE + EIA (individual days, large n)
# ---------------------------------------------------------------------------

def load_resolve_daily_peaks() -> pd.DataFrame:
    """
    Daily peak hour from RESOLVE net load (demand_mw_net from resolve_hourly_profiles.csv).
    Returns: year (weather year 2000-2022) | month | day | peak_hour | peak_mw
    """
    if not RESOLVE_FILE.exists():
        return pd.DataFrame()

    df = pd.read_csv(RESOLVE_FILE, parse_dates=["datetime_pst"])
    df = df[df["utility"].isin(RESOLVE_UTILS)].copy()

    hourly = df.groupby("datetime_pst")["demand_mw_net"].sum().reset_index()
    hourly["year"]  = hourly["datetime_pst"].dt.year
    hourly["month"] = hourly["datetime_pst"].dt.month
    hourly["day"]   = hourly["datetime_pst"].dt.day
    hourly["hour"]  = hourly["datetime_pst"].dt.hour

    idx   = hourly.groupby(["year", "month", "day"])["demand_mw_net"].idxmax()
    peaks = hourly.loc[idx, ["year", "month", "day", "hour", "demand_mw_net"]].copy()
    peaks.columns = ["year", "month", "day", "peak_hour", "peak_mw"]
    print(f"  RESOLVE daily peaks (net): {len(peaks):,} days, "
          f"{peaks['year'].nunique()} weather years")
    return peaks.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Per-utility RESOLVE and IEPR daily peaks (for utility-level shift analysis)
# ---------------------------------------------------------------------------

def load_resolve_daily_peaks_by_utility() -> pd.DataFrame:
    """
    Daily peak hour from RESOLVE net load per utility (PGE, SCE, SDGE).
    Uses demand_mw_net from resolve_hourly_profiles.csv.
    Returns: utility | year | month | day | peak_hour | peak_mw
    """
    if not RESOLVE_FILE.exists():
        return pd.DataFrame()

    df = pd.read_csv(RESOLVE_FILE, parse_dates=["datetime_pst"])
    df = df[df["utility"].isin(RESOLVE_UTILS)].copy()
    df["year"]  = df["datetime_pst"].dt.year
    df["month"] = df["datetime_pst"].dt.month
    df["day"]   = df["datetime_pst"].dt.day
    df["hour"]  = df["datetime_pst"].dt.hour

    idx   = df.groupby(["utility", "year", "month", "day"])["demand_mw_net"].idxmax()
    peaks = df.loc[idx, ["utility", "year", "month", "day", "hour", "demand_mw_net"]].copy()
    peaks.columns = ["utility", "year", "month", "day", "peak_hour", "peak_mw"]
    print(f"  RESOLVE per-utility daily peaks: {len(peaks):,} day-utility combos, "
          f"{peaks['year'].nunique()} weather years")
    return peaks.reset_index(drop=True)


def load_iepr_daily_peaks_by_utility() -> pd.DataFrame:
    """
    Daily peak hour per utility (PGE, SCE, SDGE) from IEPR all projected years.
    Peak is found independently within each utility (not the coincident CAISO peak).
    Returns: vintage | utility | year | month | day | peak_hour | peak_mw
    """
    print("  Loading IEPR per-utility daily peaks (all projected years)...")
    df = pd.read_csv(
        IEPR_FILE,
        usecols=["forecast_vintage_year", "utility_ba", "scenario",
                 "YEAR", "MONTH", "DAY", "HOUR", "BASELINE_NET_LOAD"],
    )
    df = df[
        (df["utility_ba"].isin(IEPR_UTILS)) &
        (df["scenario"] == IEPR_SCENARIO)
    ].copy()
    df["hour0"] = df["HOUR"] - 1
    df = df.rename(columns={
        "forecast_vintage_year": "vintage",
        "utility_ba": "utility",
        "YEAR": "year", "MONTH": "month", "DAY": "day",
    })

    idx   = df.groupby(["vintage", "utility", "year", "month", "day"])["BASELINE_NET_LOAD"].idxmax()
    peaks = df.loc[idx, ["vintage", "utility", "year", "month", "day",
                          "hour0", "BASELINE_NET_LOAD"]].copy()
    peaks.columns = ["vintage", "utility", "year", "month", "day", "peak_hour", "peak_mw"]

    pieces = [
        peaks[(peaks["vintage"] == v) & (peaks["year"] > last_h)]
        for v, last_h in IEPR_LAST_HIST.items()
        if v in peaks["vintage"].values
    ]
    result = pd.concat(pieces, ignore_index=True)
    print(f"  IEPR per-utility daily peaks: {len(result):,} day-utility combos across "
          f"{result['vintage'].nunique()} vintages, "
          f"years {result['year'].min()}-{result['year'].max()}")
    return result


def compute_daily_shift_significance_table(
    iepr_daily: pd.DataFrame,
    eia_daily: pd.DataFrame,
    resolve_daily: pd.DataFrame,
) -> pd.DataFrame:
    """
    For each month (and overall), compare daily peak-hour distributions using:
      - Two-sample t-test (tests difference in means)
      - Mann-Whitney U test (non-parametric; tests stochastic ordering)

    Comparisons: IEPR vs EIA  and  RESOLVE vs EIA.
    n is far larger than the mean-profile approach because each day contributes.
    """
    def _sig(p: float) -> str:
        return ("***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "ns")

    # Near-term IEPR (2024-2025 only) mirrors the date range used in fig4 of
    # compare_iepr_eia.py, which inner-joins on the same calendar date.  If the
    # large overall shift is driven by long-range projections, restricting to these
    # near-term years should collapse shifts toward zero, matching fig4's result.
    iepr_nearterm = iepr_daily[iepr_daily["year"].isin([2024, 2025])].copy()
    eia_nearterm  = eia_daily[eia_daily["year"].isin([2024, 2025])].copy()
    print(f"  IEPR near-term (2024-25): {len(iepr_nearterm):,} days from "
          f"vintages {sorted(iepr_nearterm['vintage'].unique())}")
    print(f"  EIA  near-term (2024-25): {len(eia_nearterm):,} days")

    rows = []
    # (label, forecast_df, reference_eia_df)
    # "IEPR 2024-25" uses matched EIA years to replicate fig4's near-term date range
    comparisons = [
        ("IEPR", iepr_daily, eia_daily),
        ("IEPR 2024-25", iepr_nearterm, eia_nearterm),
        ("RESOLVE", resolve_daily, eia_daily),
    ]

    for comp_label, comp_df, ref_eia in comparisons:
        entries = [("All", slice(None))] + [(MONTH_NAMES[m - 1], m) for m in range(1, 13)]
        for month_label, m_sel in entries:
            if m_sel == slice(None):
                eia_s  = ref_eia["peak_hour"].dropna()
                comp_s = comp_df["peak_hour"].dropna()
            else:
                eia_s  = ref_eia[ref_eia["month"] == m_sel]["peak_hour"].dropna()
                comp_s = comp_df[comp_df["month"] == m_sel]["peak_hour"].dropna()
            if len(comp_s) < 2 or len(eia_s) < 2:
                continue
            t,  tp = _stats.ttest_ind(comp_s, eia_s)
            u,  up = _stats.mannwhitneyu(comp_s, eia_s, alternative="two-sided")
            rows.append({
                "Comparison": f"{comp_label} vs EIA",
                "Month":      month_label,
                "n_forecast": int(len(comp_s)),
                "n_eia":      int(len(eia_s)),
                "Mean_fcst":  float(comp_s.mean()),
                "Mean_EIA":   float(eia_s.mean()),
                "Mean_diff":  float(comp_s.mean() - eia_s.mean()),
                "Std_fcst":   float(comp_s.std()),
                "Std_EIA":    float(eia_s.std()),
                "t_stat":     float(t),
                "t_p":        float(tp),
                "t_sig":      _sig(tp),
                "MWU_p":      float(up),
                "MWU_sig":    _sig(up),
            })

    tbl = pd.DataFrame(rows)

    # Console summary
    print("\n--- Daily Peak-Hour Shift: Two-Sample Tests (individual days) ---")
    print("  Each row = distribution of all daily peak hours in that month")
    for comp_label, _, _ in comparisons:
        sub = tbl[tbl["Comparison"] == f"{comp_label} vs EIA"]
        print(f"\n  {comp_label} vs EIA:")
        hdr = f"  {'Month':<6} {'n_fcst':>7} {'n_EIA':>6} {'Mean_f':>7} {'Mean_E':>7} "
        hdr += f"{'Diff':>6} {'t-sig':<5} {'MWU':>4}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for _, r in sub.iterrows():
            if r["Month"] == "All":
                print("  " + "-" * (len(hdr) - 2))
            print(
                f"  {r['Month']:<6} {r['n_forecast']:>7,} {r['n_eia']:>6,} "
                f"{r['Mean_fcst']:>+7.2f} {r['Mean_EIA']:>+7.2f} "
                f"{r['Mean_diff']:>+6.2f} {r['t_sig']:<5} {r['MWU_sig']:>4}"
            )

    out = ROOT / "data" / "tables" / "daily_peak_shift_significance.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    tbl.round(4).to_csv(out, index=False)
    print(f"\n  Saved: data/tables/daily_peak_shift_significance.csv")
    return tbl


def fig_daily_peak_distributions(
    iepr_daily: pd.DataFrame,
    eia_daily: pd.DataFrame,
    resolve_daily: pd.DataFrame,
    daily_sig_df: pd.DataFrame,
) -> None:
    """
    12-panel violin figure: distribution of daily peak hours per month.
    Three groups per panel: EIA realized | IEPR projected | RESOLVE net.
    Panel titles show mean differences (vs EIA) and Mann-Whitney significance.
    """
    EIA_C     = "#333333"
    IEPR_C    = "#1f77b4"
    RESOLVE_C = "#8c564b"
    rng = np.random.default_rng(42)

    fig, axes = plt.subplots(3, 4, figsize=(18, 12), sharey=True)
    axes = axes.flatten()

    for m_idx, m in enumerate(range(1, 13)):
        ax = axes[m_idx]
        mn = MONTH_NAMES[m - 1]

        eia_m     = eia_daily[eia_daily["month"] == m]["peak_hour"].dropna()
        iepr_m    = iepr_daily[iepr_daily["month"] == m]["peak_hour"].dropna()
        resolve_m = (resolve_daily[resolve_daily["month"] == m]["peak_hour"].dropna()
                     if not resolve_daily.empty else pd.Series([], dtype=float))

        groups = [
            ("EIA\nrealized", eia_m, EIA_C),
            ("IEPR\nprojected", iepr_m, IEPR_C),
            ("RESOLVE\nnet", resolve_m, RESOLVE_C),
        ]
        positions = np.arange(len(groups))

        for pos, (lbl, data, color) in enumerate(groups):
            if len(data) < 2:
                continue
            vp = ax.violinplot(data.values, positions=[pos],
                               showmedians=True, showextrema=True)
            for body in vp["bodies"]:
                body.set_facecolor(color)
                body.set_alpha(0.45)
            for part in ["cmedians", "cmaxes", "cmins", "cbars"]:
                vp[part].set_color(color)
            samp   = data.sample(n=min(len(data), 500), random_state=42)
            jitter = rng.uniform(-0.1, 0.1, size=len(samp))
            ax.scatter(pos + jitter, samp.values, s=2, alpha=0.2, color=color, zorder=3)

        ax.set_xticks(positions)
        ax.set_xticklabels([g[0] for g in groups], fontsize=7.5)
        ax.set_ylim(-0.5, 23.5)
        ax.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda h, _: f"{int(h):02d}:00" if 0 <= h <= 23 else "")
        )
        ax.grid(alpha=0.2, axis="y")
        if m_idx % 4 == 0:
            ax.set_ylabel("Daily peak hour")

        # Title: mean diff vs EIA + MWU sig for IEPR and RESOLVE
        title_parts = [mn]
        for comp_label in ["IEPR", "RESOLVE"]:
            row = daily_sig_df[
                (daily_sig_df["Comparison"] == f"{comp_label} vs EIA")
                & (daily_sig_df["Month"] == mn)
            ]
            if not row.empty:
                diff = row["Mean_diff"].iloc[0]
                sig  = row["MWU_sig"].iloc[0]
                title_parts.append(f"{comp_label}: {diff:+.1f}h {sig}")
        ax.set_title("  ".join(title_parts), fontsize=8.5, fontweight="bold")

    fig.suptitle(
        "Daily peak-hour distributions: IEPR projected / RESOLVE net / EIA-CISO realized\n"
        "IEPR: all projected years 2024-2050 pooled;  "
        "RESOLVE: 23 weather years net load (BTM_PV subtracted, 2024 scale)\n"
        "Titles: mean diff from EIA (IEPR or RESOLVE minus EIA) + Mann-Whitney U significance",
        fontsize=10,
    )
    fig.tight_layout()
    out = FIGS_SHIFT / "daily_peak_distributions_iepr_resolve_eia.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close(fig)


def fig_daily_shift_significance_table(daily_sig_df: pd.DataFrame) -> None:
    """
    Matplotlib three-panel significance table.

    Left   -- IEPR all projected years (2024-2050) vs EIA.
    Center -- IEPR near-term (2024-2025 only) vs EIA.  This mirrors the year
              range used by fig4 in compare_iepr_eia.py.  If the large shifts
              seen in the left panel are driven by long-range projections, this
              verification panel should show near-zero shifts — confirming that
              IEPR and EIA agree for years where realized data exists, and that
              the divergence emerges in the out-of-sample future scenarios.
    Right  -- RESOLVE vs EIA.
    """
    SIG_BG = {"***": "#d4edda", "**": "#cce5ff", "*": "#fff3cd", "ns": "#f8d7da"}
    HDR_BG = "#343a40"

    panels = [
        (
            "IEPR",
            "IEPR vs EIA-CISO\nAll projected years 2024-2050 (majority are 2026-2050)",
        ),
        (
            "IEPR 2024-25",
            "IEPR vs EIA-CISO  |  VERIFICATION\n"
            "Near-term 2024-2025 only — same years as fig4; near-zero shift expected",
        ),
        (
            "RESOLVE",
            "RESOLVE vs EIA-CISO\n23 weather years (2000-2022), BTM_PV corrected to 2024",
        ),
    ]

    fig, axes_arr = plt.subplots(1, 3, figsize=(27, 5.5))

    for ax, (comp_key, panel_title) in zip(axes_arr, panels):
        comp_lookup = f"{comp_key} vs EIA"
        sub = daily_sig_df[daily_sig_df["Comparison"] == comp_lookup].copy()
        if sub.empty:
            ax.axis("off")
            ax.set_title(panel_title, fontsize=9, fontweight="bold", pad=8)
            continue

        col_labels = ["Month", "n forecast", "n EIA",
                      "Mean fcst (h)", "Mean EIA (h)", "Diff (h)",
                      "t-sig", "MWU sig"]
        cell_text   = []
        cell_colors = []
        for _, r in sub.iterrows():
            bg = SIG_BG.get(r["MWU_sig"], "white")
            cell_text.append([
                r["Month"],
                f"{int(r['n_forecast']):,}",
                f"{int(r['n_eia']):,}",
                f"{r['Mean_fcst']:.1f}",
                f"{r['Mean_EIA']:.1f}",
                f"{r['Mean_diff']:+.2f}",
                r["t_sig"],
                r["MWU_sig"],
            ])
            cell_colors.append([bg] * len(col_labels))

        ax.axis("off")
        tbl = ax.table(
            cellText=cell_text,
            colLabels=col_labels,
            cellColours=cell_colors,
            cellLoc="center",
            loc="center",
        )
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(9)
        tbl.scale(1.0, 1.5)
        for j in range(len(col_labels)):
            tbl[(0, j)].set_facecolor(HDR_BG)
            tbl[(0, j)].get_text().set_color("white")
            tbl[(0, j)].get_text().set_fontweight("bold")
        for j in range(len(col_labels)):
            tbl[(1, j)].get_text().set_fontweight("bold")

        ax.set_title(panel_title, fontsize=9, fontweight="bold", pad=8)

    fig.suptitle(
        "Daily peak-hour shift significance: IEPR / RESOLVE vs EIA-CISO\n"
        "Two-sample t-test and Mann-Whitney U test on distributions of daily peak hours.  "
        "Background color = MWU significance.  Sig.: *** p<0.001  ** p<0.01  * p<0.05  ns not significant\n"
        "Context: fig4 (compare_iepr_eia.py) shows near-zero shift because it compares only 2024-2025 near-term "
        "projections matched to the same realized EIA dates.  The center panel replicates that year range "
        "as a distributional comparison — near-zero shifts here confirm fig4 is consistent; large shifts "
        "in the left panel are driven by the 2026-2050 long-range scenarios where IEPR projects different "
        "within-day load shapes (especially winter) due to increasing BTM solar and electrification.",
        fontsize=9,
    )
    fig.tight_layout()
    out = FIGS_SHIFT / "daily_peak_shift_significance_table.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close(fig)


# ---------------------------------------------------------------------------
# IEPR vs RESOLVE vs Substation: direct shift comparison
# ---------------------------------------------------------------------------

def fig_iepr_resolve_substation_shift(
    total_coin:          pd.DataFrame,
    iepr_caiso_daily:    pd.DataFrame,
    resolve_caiso_daily: pd.DataFrame,
    reeds_daily:         pd.DataFrame | None = None,
) -> None:
    """
    12-panel violin (3×4): IEPR projected vs RESOLVE weather-year daily peak-hour
    distributions at CAISO level, per month.  ReEDS IRA_low CA daily peaks added
    as a third violin when reeds_daily is provided.

    Grey dashed horizontal line = argmax of the substation coincident max-load-day
    profile, giving the historical reference point for each month.

    Panel title = month, IEPR−RESOLVE mean diff, Mann-Whitney U significance.
    """
    IEPR_C    = "#1f77b4"
    RESOLVE_C = "#8c564b"
    REEDS_C   = REEDS_COLOR
    rng = np.random.default_rng(42)

    fig, axes = plt.subplots(3, 4, figsize=(18, 12), sharey=True)
    axes = axes.flatten()

    for m_idx, m in enumerate(range(1, 13)):
        ax = axes[m_idx]
        mn = MONTH_NAMES[m - 1]

        iepr_m = iepr_caiso_daily[iepr_caiso_daily["month"] == m]["peak_hour"].dropna()
        res_m  = (resolve_caiso_daily[resolve_caiso_daily["month"] == m]["peak_hour"].dropna()
                  if not resolve_caiso_daily.empty else pd.Series(dtype=float))

        # Substation argmax for this month
        sub_m = total_coin[total_coin["month"] == m]
        sub_peak_h = (int(sub_m.loc[sub_m["coin_max_mw"].idxmax(), "hour"])
                      if not sub_m.empty else None)

        reeds_m = (reeds_daily[reeds_daily["month"] == m]["peak_hour"].dropna()
                   if reeds_daily is not None and not reeds_daily.empty
                   else pd.Series(dtype=float))
        groups    = [("IEPR\nprojected", iepr_m, IEPR_C),
                     ("RESOLVE\nnet",    res_m,  RESOLVE_C),
                     ("ReEDS\nIRA_low",  reeds_m, REEDS_C)]
        positions = np.arange(len(groups))
        for pos, (lbl, data, color) in zip(positions, groups):
            if len(data) < 2:
                continue
            vp = ax.violinplot(data.values, positions=[pos],
                               showmedians=True, showextrema=True)
            for body in vp["bodies"]:
                body.set_facecolor(color)
                body.set_alpha(0.45)
            for part in ["cmedians", "cmaxes", "cmins", "cbars"]:
                vp[part].set_color(color)
            samp   = data.sample(n=min(len(data), 500), random_state=42)
            jitter = rng.uniform(-0.1, 0.1, size=len(samp))
            ax.scatter(pos + jitter, samp.values, s=2, alpha=0.2, color=color, zorder=3)

        # Substation reference line
        if sub_peak_h is not None:
            ax.axhline(sub_peak_h, color="grey", lw=2, linestyle="--",
                       alpha=0.85, zorder=4,
                       label="Substation max-profile" if m_idx == 0 else "_")
            ax.text(-0.45, sub_peak_h + 0.3, f"Sub {sub_peak_h:02d}:00",
                    color="grey", fontsize=6.5, va="bottom")

        # Panel title with IEPR vs RESOLVE shift and significance
        if len(iepr_m) >= 2 and len(res_m) >= 2:
            diff = iepr_m.mean() - res_m.mean()
            _, p = _stats.mannwhitneyu(iepr_m, res_m, alternative="two-sided")
            sig  = ("***" if p < 0.001 else "**" if p < 0.01 else
                    "*"   if p < 0.05  else "ns")
            title_str = f"{mn}  IEPR−RESOLVE: {diff:+.1f}h  {sig}"
        else:
            title_str = mn

        ax.set_title(title_str, fontsize=8.5, fontweight="bold")
        ax.set_xticks(positions)
        ax.set_xticklabels([g[0] for g in groups], fontsize=7.5)
        ax.set_ylim(-0.5, 23.5)
        ax.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda h, _: f"{int(h):02d}:00" if 0 <= h <= 23 else "")
        )
        ax.grid(alpha=0.2, axis="y")
        if m_idx % 4 == 0:
            ax.set_ylabel("Daily peak hour")

    axes[0].legend(fontsize=8, loc="upper left")
    fig.suptitle(
        "CAISO daily peak-hour distributions: IEPR projected vs RESOLVE weather-year net load vs ReEDS IRA_low\n"
        "IEPR: all projected years 2024–2050 pooled  |  "
        "RESOLVE: 23 weather years (2000–2022, net of BTM_PV, 2024 scale)  |  "
        "ReEDS: 7 weather years (2007–2013) × 365 days\n"
        "Grey dashed line = substation coincident max-load-day profile argmax.  "
        "Title: mean diff (IEPR − RESOLVE) + Mann-Whitney U significance.",
        fontsize=10,
    )
    fig.tight_layout()
    out = FIGS_SHIFT / "iepr_vs_resolve_substation_shift.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close(fig)


def fig_peak_hour_monthly_by_utility(
    util_coin:           pd.DataFrame,
    total_coin:          pd.DataFrame,
    iepr_util_daily:     pd.DataFrame,
    iepr_caiso_daily:    pd.DataFrame,
    resolve_util_daily:  pd.DataFrame,
    resolve_caiso_daily: pd.DataFrame,
) -> None:
    """
    4-panel figure: monthly mean daily peak hour by utility and CAISO total.

    X-axis = month, Y-axis = mean daily peak hour.
    Sources:
      • Substation: argmax of coincident max-load-day profile per month (one fixed
        point per month — no statistical uncertainty, since there are no individual days)
      • IEPR: mean ± 1 std across all projected years (2024–2050) by vintage
      • RESOLVE: mean ± 1 std across 23 weather years (net of BTM_PV, 2024 scale)

    PGE and SDGE substation profiles have no year stamp (fixed monthly shape);
    SCE has years 2017–2026.  This limits temporal comparisons for PGE/SDGE.
    """
    months = np.arange(1, 13)
    # (panel_title, util_upper key for IEPR/RESOLVE, util_lower key for coin)
    panels = [
        ("PGE",                         "PGE",  "pge"),
        ("SCE",                         "SCE",  "sce"),
        ("SDGE",                        "SDGE", "sdge"),
        ("CAISO total\n(PGE+SCE+SDGE)", None,   None),
    ]

    fig, axes = plt.subplots(1, 4, figsize=(22, 6), sharey=True)

    for ax, (panel_title, util_upper, util_lower) in zip(axes, panels):
        is_total = util_upper is None

        # ── Substation argmax per month ───────────────────────────────────────
        coin_df  = total_coin if is_total else util_coin[util_coin["utility"] == util_lower]
        sub_peak = []
        for m in months:
            sub_m = coin_df[coin_df["month"] == m]
            if sub_m.empty or sub_m["coin_max_mw"].max() == 0:
                sub_peak.append(np.nan)
            else:
                sub_peak.append(int(sub_m.loc[sub_m["coin_max_mw"].idxmax(), "hour"]))
        ax.plot(months, sub_peak, color="grey", lw=2.5, linestyle="--",
                marker="s", ms=6, zorder=5,
                label="Substation (max-profile argmax)")

        # ── IEPR: mean ± std across all projected years by vintage ────────────
        iepr_df = (iepr_caiso_daily if is_total else
                   iepr_util_daily[iepr_util_daily["utility"] == util_upper]
                   if not iepr_util_daily.empty else pd.DataFrame())
        for vintage, color in VINTAGE_COLORS.items():
            v_df = (iepr_df[iepr_df["vintage"] == vintage]
                    if not iepr_df.empty else pd.DataFrame())
            if v_df.empty:
                continue
            mu = np.array([v_df[v_df["month"] == m]["peak_hour"].mean() for m in months])
            sd = np.array([v_df[v_df["month"] == m]["peak_hour"].std()  for m in months])
            ax.plot(months, mu, color=color, lw=1.8, linestyle=":",
                    marker="o", ms=4, label=f"IEPR v{vintage} (proj. yrs)")
            ax.fill_between(months, mu - sd, mu + sd, color=color, alpha=0.12)

        # ── RESOLVE: mean ± std across 23 weather years ───────────────────────
        res_df = (resolve_caiso_daily if is_total else
                  resolve_util_daily[resolve_util_daily["utility"] == util_upper]
                  if not resolve_util_daily.empty else pd.DataFrame())
        if not res_df.empty:
            mu_r = np.array([res_df[res_df["month"] == m]["peak_hour"].mean() for m in months])
            sd_r = np.array([res_df[res_df["month"] == m]["peak_hour"].std()  for m in months])
            ax.plot(months, mu_r, color=RESOLVE_COLOR, lw=2,
                    marker="^", ms=5, label="RESOLVE (wx yrs)")
            ax.fill_between(months, mu_r - sd_r, mu_r + sd_r,
                            color=RESOLVE_COLOR, alpha=0.12)

        ax.set_title(panel_title, fontsize=11, fontweight="bold")
        ax.set_xlabel("Month")
        ax.set_xticks(months)
        ax.set_xticklabels(MONTH_NAMES, fontsize=8, rotation=45)
        ax.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda h, _: f"{int(h):02d}:00" if 0 <= h <= 23 else "")
        )
        ax.set_ylim(6, 23)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=7.5, loc="lower right")

    axes[0].set_ylabel("Mean daily peak hour")
    fig.suptitle(
        "Monthly mean daily peak hour: Substation / IEPR / RESOLVE by utility and CAISO total\n"
        "Substation = argmax of coincident max-load-day profile (one point per month)  |  "
        "IEPR = mean ± 1 std across all projected years (2024–2050) by vintage  |  "
        "RESOLVE = mean ± 1 std across 23 weather years (net of BTM_PV, 2024 scale)\n"
        "PGE and SDGE substations have no year dimension (fixed monthly shape);  "
        "SCE has years 2017–2026.  IEPR and RESOLVE are projections, substation is historical.",
        fontsize=10,
    )
    plt.tight_layout()
    out = FIGS_SHIFT / "peak_hour_monthly_by_utility.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved: {out.name}")


# ---------------------------------------------------------------------------
# BTM_PV visualizations
# ---------------------------------------------------------------------------

def load_btm_pv_all_years() -> pd.DataFrame:
    """
    Load IEPR BTM_PV + BTM_STORAGE across all vintages and all projected years
    (PGE+SCE+SDGE summed, Local_Reliability).

    Returns DataFrame with columns:
      vintage | year | month | day | hour | btm_pv | storage_res | storage_nonres | btm_combined
    All in MW (negative = reduces grid load).
    btm_combined = btm_pv + storage_res + storage_nonres  (= BASELINE_NET_LOAD - BASELINE_CONSUMPTION).
    """
    df = pd.read_csv(
        IEPR_FILE,
        usecols=["forecast_vintage_year", "utility_ba", "scenario",
                 "YEAR", "MONTH", "DAY", "HOUR",
                 "BTM_PV", "BTM_STORAGE_RES", "BTM_STORAGE_NONRES"],
    )
    df = df[
        (df["utility_ba"].isin(IEPR_UTILS)) &
        (df["scenario"] == IEPR_SCENARIO)
    ].copy()
    df["hour"] = df["HOUR"] - 1  # 1-24 hour-ending → 0-23 hour-beginning

    total = (
        df.groupby(["forecast_vintage_year", "YEAR", "MONTH", "DAY", "hour"],
                   sort=False)[["BTM_PV", "BTM_STORAGE_RES", "BTM_STORAGE_NONRES"]]
        .sum()
        .reset_index()
        .rename(columns={
            "forecast_vintage_year": "vintage",
            "YEAR": "year", "MONTH": "month", "DAY": "day",
            "BTM_PV": "btm_pv",
            "BTM_STORAGE_RES": "storage_res",
            "BTM_STORAGE_NONRES": "storage_nonres",
        })
    )
    total["btm_combined"] = total["btm_pv"] + total["storage_res"] + total["storage_nonres"]
    print(f"  BTM loaded: {len(total):,} rows across "
          f"{total['vintage'].nunique()} vintages, "
          f"years {total['year'].min()}–{total['year'].max()}")
    return total


def fig_btm_pv_shape_invariance(btm_all: pd.DataFrame) -> None:
    """
    12-panel figure (one per month): every individual day's hourly BTM_PV profile
    overlaid for the reference vintage and year (2024/2024), with the combined
    BTM (PV + storage) mean shown alongside.

    If IEPR uses a fixed daily template per (month, hour), all day traces will
    coincide → proves BTM_PV is a lookup table, not a weather-sensitive simulation.
    The combined mean (dashed) shows where storage modifies the BTM_PV profile:
    storage charges at midday (slightly less offset) and discharges at evening
    (additional offset beyond solar).
    """
    ref_vintage, ref_year = 2024, 2024
    ref = btm_all[
        (btm_all["vintage"] == ref_vintage) &
        (btm_all["year"] == ref_year)
    ].copy()
    if ref.empty:
        print("  WARNING: no BTM_PV data for vintage 2024 / year 2024 — skipping shape figure.")
        return

    ref["btm_abs"]  = -ref["btm_pv"]
    ref["comb_abs"] = -ref["btm_combined"]

    fig, axes = plt.subplots(3, 4, figsize=(18, 12), sharey=True, sharex=True)
    axes = axes.flatten()

    for m_idx, m in enumerate(range(1, 13)):
        ax = axes[m_idx]
        sub = ref[ref["month"] == m]
        days = sorted(sub["day"].unique())

        for d in days:
            day_data = sub[sub["day"] == d].sort_values("hour")
            ax.plot(day_data["hour"], day_data["btm_abs"],
                    color="#aaaaaa", lw=0.9, alpha=0.4, zorder=2)

        mean_pv   = sub.groupby("hour")["btm_abs"].mean()
        mean_comb = sub.groupby("hour")["comb_abs"].mean()

        ax.plot(mean_pv.index, mean_pv.values,
                color="#d62728", lw=2.5, zorder=5,
                label="Mean BTM_PV" if m_idx == 0 else "_")
        ax.plot(mean_comb.index, mean_comb.values,
                color="#e07b00", lw=2.0, ls="dashed", zorder=6,
                label="Mean PV+Storage" if m_idx == 0 else "_")

        ax.set_title(f"{MONTH_NAMES[m - 1]}  ({len(days)} days)",
                     fontsize=10, fontweight="bold")
        ax.set_xticks([0, 6, 12, 18, 23])
        ax.set_xticklabels(["00", "06", "12", "18", "23"], fontsize=7)
        if m_idx % 4 == 0:
            ax.set_ylabel("BTM offset (MW, positive = grid load reduction)")
        if m_idx >= 8:
            ax.set_xlabel("Hour of day (PST, 0-23)")
        ax.grid(True, alpha=0.25)

    axes[0].legend(fontsize=9, loc="upper left")
    fig.suptitle(
        f"IEPR BTM shape invariance — every day's BTM_PV profile overlaid per month\n"
        f"Reference: vintage {ref_vintage}, year {ref_year},  PGE+SCE+SDGE summed,  "
        f"Local_Reliability\n"
        "Grey = individual BTM_PV days;  red solid = mean BTM_PV;  orange dashed = mean PV+Storage.\n"
        "Storage adds a small evening tail (dashed above solid at hours 17–22); "
        "at midday dashed is slightly below solid (storage charging partially offsets solar).",
        fontsize=9.5,
    )
    fig.tight_layout()
    out = FIGS_SHIFT / "btm_pv_shape_invariance.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close(fig)


def fig_btm_pv_annual_growth(btm_all: pd.DataFrame) -> None:
    """
    12-panel figure (one per month): BTM magnitude growth across projected years,
    comparing BTM_PV (midday solar peak) and combined PV+Storage (evening discharge peak).

    For each (vintage, year, month):
      - Solid:  mean daily-peak absolute BTM_PV (dominates at midday)
      - Dashed: mean peak combined BTM discharge at evening shoulder (hours 16–22)
                i.e. max of -btm_combined restricted to hours 16-22 per day, averaged

    Shows that solar grows monotonically (solid) while evening storage discharge
    grows FASTER (dashed), especially in later projected years.
    """
    df = btm_all.copy()
    df["btm_abs"]  = -df["btm_pv"]
    df["comb_abs"] = -df["btm_combined"]

    # Midday solar peak: max btm_abs per (vintage, year, month, day)
    mid_peak = (
        df.groupby(["vintage", "year", "month", "day"])["btm_abs"]
        .max()
        .reset_index(name="mid_peak_mw")
    )
    mid_annual = mid_peak.groupby(["vintage", "year", "month"])["mid_peak_mw"].mean().reset_index()

    # Evening combined discharge peak: max comb_abs during hours 16-22
    eve = df[df["hour"].between(16, 22)].copy()
    eve_peak = (
        eve.groupby(["vintage", "year", "month", "day"])["comb_abs"]
        .max()
        .reset_index(name="eve_peak_mw")
    )
    eve_annual = eve_peak.groupby(["vintage", "year", "month"])["eve_peak_mw"].mean().reset_index()

    vintages = sorted(df["vintage"].unique())
    year_min  = df["year"].min() - 0.5
    year_max  = df["year"].max() + 0.5

    fig, axes = plt.subplots(3, 4, figsize=(18, 12), sharex=True)
    axes = axes.flatten()

    for m_idx, m in enumerate(range(1, 13)):
        ax = axes[m_idx]
        mid_m = mid_annual[mid_annual["month"] == m]
        eve_m = eve_annual[eve_annual["month"] == m]

        for v in vintages:
            color = VINTAGE_COLORS.get(v, "gray")
            vm = mid_m[mid_m["vintage"] == v].sort_values("year")
            ve = eve_m[eve_m["vintage"] == v].sort_values("year")
            if not vm.empty:
                ax.plot(vm["year"], vm["mid_peak_mw"],
                        color=color, lw=2, marker="o", ms=2.5,
                        label=f"v{v} midday solar" if m_idx == 0 else "_")
            if not ve.empty:
                ax.plot(ve["year"], ve["eve_peak_mw"],
                        color=color, lw=1.5, ls="dashed", marker=".", ms=2,
                        label=f"v{v} evening PV+Stor." if m_idx == 0 else "_")

        ax.set_title(MONTH_NAMES[m - 1], fontsize=10, fontweight="bold")
        if m_idx % 4 == 0:
            ax.set_ylabel("Peak BTM offset (MW)")
        if m_idx >= 8:
            ax.set_xlabel("Projected year")
        ax.set_xlim(year_min, year_max)
        ax.grid(True, alpha=0.25)

    axes[0].legend(fontsize=8, loc="upper left", ncol=2)
    fig.suptitle(
        "IEPR BTM growth across forecast horizon — solar vs evening storage discharge\n"
        "Solid = daily-peak BTM_PV magnitude (midday solar peak, PGE+SCE+SDGE, Local_Reliability)\n"
        "Dashed = peak combined BTM_PV+Storage during hours 16–22 (evening discharge)\n"
        "Evening storage grows FASTER than midday solar in later projected years.",
        fontsize=10,
    )
    fig.tight_layout()
    out = FIGS_SHIFT / "btm_pv_annual_growth.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close(fig)


def load_iepr_net_load_profiles() -> pd.DataFrame:
    """
    Load IEPR BASELINE_NET_LOAD and BTM_STORAGE components for all vintages,
    all projected years, PGE+SCE+SDGE summed, Local_Reliability.

    Returns (vintage, year, month, day, hour) with columns:
      net_load          — BASELINE_NET_LOAD (includes BTM_PV + storage)
      storage_res       — BTM_STORAGE_RES
      storage_nonres    — BTM_STORAGE_NONRES
      net_no_storage    — BASELINE_NET_LOAD minus storage (BTM_PV only correction)
    """
    df = pd.read_csv(
        IEPR_FILE,
        usecols=["forecast_vintage_year", "utility_ba", "scenario",
                 "YEAR", "MONTH", "DAY", "HOUR",
                 "BASELINE_NET_LOAD", "BTM_STORAGE_RES", "BTM_STORAGE_NONRES"],
    )
    df = df[
        (df["utility_ba"].isin(IEPR_UTILS)) &
        (df["scenario"] == IEPR_SCENARIO)
    ].copy()
    df["hour"] = df["HOUR"] - 1

    total = (
        df.groupby(["forecast_vintage_year", "YEAR", "MONTH", "DAY", "hour"],
                   sort=False)[
            ["BASELINE_NET_LOAD", "BTM_STORAGE_RES", "BTM_STORAGE_NONRES"]
        ]
        .sum()
        .reset_index()
        .rename(columns={
            "forecast_vintage_year": "vintage",
            "YEAR": "year", "MONTH": "month", "DAY": "day",
            "BASELINE_NET_LOAD": "net_load",
            "BTM_STORAGE_RES": "storage_res",
            "BTM_STORAGE_NONRES": "storage_nonres",
        })
    )
    total["net_no_storage"] = total["net_load"] - total["storage_res"] - total["storage_nonres"]
    print(f"  Net load profiles loaded: {len(total):,} rows across "
          f"{total['vintage'].nunique()} vintages, "
          f"years {total['year'].min()}–{total['year'].max()}")
    return total


def fig_btm_peak_hour_by_year(net_df: pd.DataFrame) -> None:
    """
    12-panel figure: IEPR BASELINE_NET_LOAD peak hour vs projected year per month,
    comparing with vs without BTM storage.

    For each (vintage, year, month): mean daily profile → argmax peak hour.
    Two series:
      - Solid:  BASELINE_NET_LOAD peak hour (BTM_PV + storage correction)
      - Dashed: BASELINE_NET_LOAD minus storage peak hour (BTM_PV only)
    Gap = storage's effect on the predicted peak demand hour.

    Key finding: for 2024–2035, storage has essentially no effect on peak hour
    (curves overlap).  In far-future winter months (2040 for vintage 2024), storage
    becomes large enough to depress the normal 6pm evening peak, shifting the argmax
    to an unrealistically early hour — meaning IEPR storage projections may eventually
    saturate or invert the evening peak in winter.
    """
    # Filter to projected years only
    pieces = []
    for v, last_h in IEPR_LAST_HIST.items():
        sub = net_df[(net_df["vintage"] == v) & (net_df["year"] > last_h)]
        if not sub.empty:
            pieces.append(sub)
    if not pieces:
        print("  WARNING: no projected-year data in net_df — skipping peak hour figure.")
        return
    df = pd.concat(pieces, ignore_index=True)

    # Mean profile per (vintage, year, month, hour)
    mean_prof = (
        df.groupby(["vintage", "year", "month", "hour"])[
            ["net_load", "net_no_storage"]
        ]
        .mean()
        .reset_index()
    )

    # Peak hour per (vintage, year, month) for each series
    idx_full   = mean_prof.groupby(["vintage", "year", "month"])["net_load"].idxmax()
    idx_nostor = mean_prof.groupby(["vintage", "year", "month"])["net_no_storage"].idxmax()

    peak_full = (
        mean_prof.loc[idx_full, ["vintage", "year", "month", "hour"]]
        .rename(columns={"hour": "ph_full"})
        .reset_index(drop=True)
    )
    peak_nostor = (
        mean_prof.loc[idx_nostor, ["vintage", "year", "month", "hour"]]
        .rename(columns={"hour": "ph_nostor"})
        .reset_index(drop=True)
    )
    peaks = peak_full.merge(peak_nostor, on=["vintage", "year", "month"])

    vintages = sorted(peaks["vintage"].unique())
    year_min  = peaks["year"].min() - 0.5
    year_max  = peaks["year"].max() + 0.5

    fig, axes = plt.subplots(3, 4, figsize=(20, 14), sharex=True)
    axes = axes.flatten()

    for m_idx, m in enumerate(range(1, 13)):
        ax = axes[m_idx]
        sub = peaks[peaks["month"] == m]

        for v in vintages:
            vs = sub[sub["vintage"] == v].sort_values("year")
            if vs.empty:
                continue
            color = VINTAGE_COLORS.get(v, "gray")
            ax.plot(vs["year"], vs["ph_nostor"],
                    color=color, lw=1.4, ls="dashed", marker=".", ms=2,
                    alpha=0.75,
                    label=f"v{v} BTM_PV only" if m_idx == 0 else "_")
            ax.plot(vs["year"], vs["ph_full"],
                    color=color, lw=2.0, ls="solid", marker=".", ms=2,
                    label=f"v{v} PV+Storage" if m_idx == 0 else "_")
            ax.fill_between(
                vs["year"], vs["ph_nostor"], vs["ph_full"],
                color=color, alpha=0.12,
            )

        ax.set_title(MONTH_NAMES[m - 1], fontsize=10, fontweight="bold")
        ax.set_xlim(year_min, year_max)
        ax.set_ylim(-0.5, 23.5)
        ax.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda h, _: f"{int(h):02d}:00" if 0 <= h <= 23 else "")
        )
        if m_idx % 4 == 0:
            ax.set_ylabel("Peak hour (PST, mean daily profile)")
        if m_idx >= 8:
            ax.set_xlabel("Projected year")
        ax.grid(True, alpha=0.25)

    axes[0].legend(fontsize=8, loc="upper left")
    fig.suptitle(
        "IEPR BASELINE_NET_LOAD peak hour by projected year and month\n"
        "Solid = with BTM storage (PV+Storage combined);  "
        "dashed = BTM_PV only (storage removed from net load)\n"
        "Gap = storage's effect on predicted peak demand hour.  "
        "PGE+SCE+SDGE summed, Local_Reliability, argmax of mean daily profile.\n"
        "Near-term (2024–2035): storage has negligible effect.  "
        "Far-future: storage depresses the 6pm winter peak, shifting argmax to an extreme hour.",
        fontsize=9.5,
    )
    fig.tight_layout()
    out = FIGS_SHIFT / "btm_peak_hour_by_year.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close(fig)


def fig_btm_combined_vs_pv(btm_all: pd.DataFrame) -> None:
    """
    12-panel figure (one per month): hourly BTM_PV vs combined BTM (solar + storage)
    for three projected years from vintage 2024 (2024, 2032, 2040).

    Answers whether storage matters for understanding the peak-hour shift:
    - Midday: storage charges (positive contribution) → tiny offset from BTM_PV
    - Evening: storage discharges (adds to the BTM offset) → grows fast across years,
      creating an additional evening demand reduction that can shift the apparent peak
    - Storage share of combined BTM grows from ~1% at midday to ~50%+ at evening by 2040

    Two sub-axes per panel: top = absolute MW (BTM_PV and combined); bottom = storage alone.
    """
    ref_vintage = 2024
    ref_years   = [2024, 2032, 2040]
    # Keep only years available in this vintage
    avail = sorted(btm_all[btm_all["vintage"] == ref_vintage]["year"].unique())
    ref_years = [y for y in ref_years if y in avail]

    year_colors = {2024: "#1f77b4", 2032: "#ff7f0e", 2040: "#2ca02c"}
    year_ls_pv  = "solid"
    year_ls_comb = "dashed"

    df = btm_all[btm_all["vintage"] == ref_vintage].copy()
    df["btm_abs"]      = -df["btm_pv"]
    df["comb_abs"]     = -df["btm_combined"]
    df["storage_total"] = df["storage_res"] + df["storage_nonres"]

    fig, axes = plt.subplots(3, 4, figsize=(20, 14))
    axes = axes.flatten()

    for m_idx, m in enumerate(range(1, 13)):
        ax = axes[m_idx]

        for yr in ref_years:
            sub = df[(df["month"] == m) & (df["year"] == yr)]
            if sub.empty:
                continue
            # Mean across all days in month
            prof = sub.groupby("hour")[["btm_abs", "comb_abs", "storage_total"]].mean()
            color = year_colors.get(yr, "gray")

            ax.fill_between(prof.index, prof["btm_abs"],
                            alpha=0.12, color=color)
            ax.plot(prof.index, prof["btm_abs"],
                    color=color, lw=1.5, linestyle="solid",
                    label=f"{yr} BTM_PV" if m_idx == 0 else "_")
            ax.plot(prof.index, prof["comb_abs"],
                    color=color, lw=2.2, linestyle="dashed",
                    label=f"{yr} PV+Storage" if m_idx == 0 else "_")

            # Shade the storage contribution (difference between combined and PV)
            ax.fill_between(prof.index, prof["btm_abs"], prof["comb_abs"],
                            alpha=0.22, color=color,
                            where=(prof["comb_abs"] >= prof["btm_abs"]))

        ax.set_title(MONTH_NAMES[m - 1], fontsize=10, fontweight="bold")
        ax.set_xticks([0, 6, 12, 18, 23])
        ax.set_xticklabels(["00", "06", "12", "18", "23"], fontsize=7)
        if m_idx % 4 == 0:
            ax.set_ylabel("MW offset (positive = grid load reduction)")
        if m_idx >= 8:
            ax.set_xlabel("Hour (PST, 0-23)")
        ax.axhline(0, color="black", lw=0.6, alpha=0.4)
        ax.grid(True, alpha=0.2)

    # Legend
    from matplotlib.lines import Line2D
    handles = []
    for yr in ref_years:
        c = year_colors.get(yr, "gray")
        handles.append(Line2D([0], [0], color=c, lw=1.5, ls="solid",  label=f"{yr} BTM_PV only"))
        handles.append(Line2D([0], [0], color=c, lw=2.2, ls="dashed", label=f"{yr} BTM_PV + Storage"))
    axes[0].legend(handles=handles, fontsize=7.5, loc="upper left")

    fig.suptitle(
        f"IEPR BTM total grid offset: BTM_PV vs BTM_PV + Storage  (vintage {ref_vintage}, "
        f"PGE+SCE+SDGE summed, Local_Reliability)\n"
        "Solid = BTM_PV only;  dashed = PV + BTM_STORAGE_RES + BTM_STORAGE_NONRES  "
        "(= actual BASELINE_NET_LOAD offset from BASELINE_CONSUMPTION)\n"
        "Shaded band = storage contribution.  "
        "Storage is negligible at midday but grows to match BTM_PV at the evening shoulder by 2040 → "
        "pushes the combined offset deeper into the evening, amplifying the late-peak effect.",
        fontsize=9.5,
    )
    fig.tight_layout()
    out = FIGS_SHIFT / "btm_combined_pv_plus_storage.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved: {out.name}")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def print_summary(
    total_coin: pd.DataFrame,
    mh_stats:   pd.DataFrame,
    iepr_total: pd.DataFrame,
    cal_stats:  pd.DataFrame,
) -> None:
    def _cov_report(label, merged, ratio_col):
        r = merged[ratio_col]
        cv = r.std() / r.mean()
        sys_flag = " (highly systematic)" if cv < 0.10 else ""
        print(f"  {label}: mean={r.mean():.3f}  std={r.std():.3f}  "
              f"range [{r.min():.2f},{r.max():.2f}]  CoV={cv:.3f}{sys_flag}")

    # --- CISO comparison ---
    m_ciso = total_coin.merge(mh_stats, on=["month", "hour"])
    m_ciso["ratio_max"] = m_ciso["coin_max_mw"] / m_ciso["eia_mean"]
    m_ciso["ratio_min"] = m_ciso["coin_min_mw"] / m_ciso["eia_mean"]
    print("\n--- Coverage: Substation / EIA CISO Mean ---")
    _cov_report("High-load-day", m_ciso, "ratio_max")
    _cov_report("Low-load-day ", m_ciso, "ratio_min")

    # --- CAL comparison ---
    if not cal_stats.empty:
        m_cal = total_coin.merge(cal_stats, on=["month", "hour"])
        m_cal["ratio_max"] = m_cal["coin_max_mw"] / m_cal["cal_mean"]
        m_cal["ratio_min"] = m_cal["coin_min_mw"] / m_cal["cal_mean"]
        print("\n--- Coverage: Substation / EIA CAL Region Mean ---")
        _cov_report("High-load-day", m_cal, "ratio_max")
        _cov_report("Low-load-day ", m_cal, "ratio_min")

    print("\n--- IEPR vs EIA CISO Mean (288 month-hours, summed PGE+SCE+SDGE) ---")
    for vintage in sorted(iepr_total["vintage"].unique()):
        iv = iepr_total[iepr_total["vintage"] == vintage].merge(
            mh_stats, left_on=["month", "hour0"], right_on=["month", "hour"]
        )
        if iv.empty:
            continue
        bias = (iv["iepr_total_mw"] - iv["eia_mean"]).mean()
        mae  = (iv["iepr_total_mw"] - iv["eia_mean"]).abs().mean()
        r    = np.corrcoef(iv["iepr_total_mw"], iv["eia_mean"])[0, 1]
        repr_yr = IEPR_REPR_YEAR[vintage]
        print(f"  v{vintage} (year {repr_yr}): bias={bias:+.0f} MW  MAE={mae:.0f} MW  r={r:.4f}")

    if not cal_stats.empty:
        print("\n--- IEPR vs EIA CAL Region Mean (288 month-hours, summed PGE+SCE+SDGE) ---")
        for vintage in sorted(iepr_total["vintage"].unique()):
            iv = iepr_total[iepr_total["vintage"] == vintage].merge(
                cal_stats, left_on=["month", "hour0"], right_on=["month", "hour"]
            )
            if iv.empty:
                continue
            bias = (iv["iepr_total_mw"] - iv["cal_mean"]).mean()
            mae  = (iv["iepr_total_mw"] - iv["cal_mean"]).abs().mean()
            r    = np.corrcoef(iv["iepr_total_mw"], iv["cal_mean"])[0, 1]
            repr_yr = IEPR_REPR_YEAR[vintage]
            print(f"  v{vintage} (year {repr_yr}): bias={bias:+.0f} MW  MAE={mae:.0f} MW  r={r:.4f}")

    print("\n--- Substation Coverage vs IEPR (PGE+SCE+SDGE total) ---")
    for vintage in sorted(iepr_total["vintage"].unique()):
        iv = iepr_total[iepr_total["vintage"] == vintage].merge(
            total_coin, left_on=["month", "hour0"], right_on=["month", "hour"]
        )
        if iv.empty:
            continue
        ratio_max = iv["coin_max_mw"] / iv["iepr_total_mw"]
        ratio_min = iv["coin_min_mw"] / iv["iepr_total_mw"]
        print(f"  v{vintage}: high-load ratio = {ratio_max.mean():.3f} "
              f"(CoV={ratio_max.std()/ratio_max.mean():.3f}), "
              f"low-load ratio = {ratio_min.mean():.3f} "
              f"(CoV={ratio_min.std()/ratio_min.mean():.3f})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Loading substation coincident profiles...")
    total_coin, util_coin = load_substation_coincident()
    print(f"  Total substations sum: peak max={total_coin['coin_max_mw'].max():,.0f} MW, "
          f"peak min={total_coin['coin_min_mw'].max():,.0f} MW")

    print("\nLoading EIA CISO...")
    mh_stats, yr_mh = load_eia_ciso()
    print(f"  EIA CISO mean demand range (month-hour range across all years): {mh_stats['eia_mean'].min():,.0f} - "
          f"{mh_stats['eia_mean'].max():,.0f} MW")

    print("\nLoading EIA CAL region...")
    cal_stats, cal_yr_mh = load_cal_region()
    if not cal_stats.empty:
        print(f"  EIA CAL mean demand range: {cal_stats['cal_mean'].min():,.0f} - "
              f"{cal_stats['cal_mean'].max():,.0f} MW")

    print("\nLoading IEPR hourly (Local_Reliability, BASELINE_NET_LOAD)...")
    iepr_total, iepr_util = load_iepr_hourly()

    print("\nLoading RESOLVE hourly (PGE+SCE+SDGE, 2000-2022, 2024 scale)...")
    resolve_stats, resolve_yr_mh = load_resolve_hourly()

    print("\nLoading ReEDS IRA_low CA hourly (p8+p9+p10+p11, 2025 target year)...")
    reeds_mh = load_reeds_month_hour(target_year=2025)
    if not reeds_mh.empty:
        print(f"  ReEDS CA mean load: {reeds_mh['mean_mw'].mean():,.0f} MW  "
              f"peak: {reeds_mh['mean_mw'].max():,.0f} MW")

    print("\nGenerating figures...")
    # Fig 1: monthly profiles (per-panel y-axes)
    fig_monthly_profiles(total_coin, mh_stats, iepr_total, cal_stats,
                         resolve_stats, reeds_mh=reeds_mh)
    # Fig 5: same with shared y-axis across all panels
    fig_monthly_profiles_shared_y(total_coin, mh_stats, iepr_total, cal_stats,
                                  resolve_stats, reeds_mh=reeds_mh)
    # Fig 6: all months on a single x-axis
    fig_annual_profile(total_coin, mh_stats, iepr_total, cal_stats, resolve_stats,
                       reeds_mh=reeds_mh)
    # Original supporting figures
    fig_coverage_heatmap(total_coin, mh_stats, cal_stats)
    for _month in range(1, 13):
        fig_utility_breakdown(util_coin, iepr_util, month=_month)
    fig_monthly_peaks(total_coin, mh_stats, iepr_total, cal_stats)

    # Fig 7: IEPR vs EIA peak-hour shift distributions (mean-profile approach)
    print("\nAnalyzing IEPR vs EIA-CISO peak-hour shift (mean monthly profiles)...")
    shift_df = analyze_peak_shift(yr_mh, iepr_total)
    fig_peak_shift_distributions(shift_df)
    print_shift_summary(shift_df)

    # Significance table (monthly t-tests)
    sig_df = compute_shift_significance_table(shift_df)
    fig_shift_significance_table(sig_df)

    # IEPR predicted peak-hour evolution across all projected years
    print("\nLoading IEPR daily peaks (all projected years, for evolution analysis)...")
    iepr_daily = load_iepr_all_projected_years()

    print("\nLoading EIA CISO daily peaks (realized)...")
    eia_daily  = load_eia_ciso_daily_peaks()
    print(f"  EIA daily peaks: {len(eia_daily):,} days, "
          f"years {eia_daily['year'].min()}-{eia_daily['year'].max()}")

    # Fig 8: how IEPR's predicted peak hour changes over the forecast horizon
    fig_iepr_peak_evolution(iepr_daily)

    # Fig 9: monthly peak-hour distributions (IEPR projected vs EIA realized)
    fig_monthly_peak_distributions(iepr_daily, eia_daily, sig_df)

    # Daily-level analysis: individual days, large n, includes RESOLVE
    print("\nLoading RESOLVE daily peaks (net load, for daily comparison)...")
    resolve_daily = load_resolve_daily_peaks()

    print("\nRunning daily peak-shift significance tests (IEPR + RESOLVE vs EIA)...")
    daily_sig_df = compute_daily_shift_significance_table(iepr_daily, eia_daily, resolve_daily)
    fig_daily_peak_distributions(iepr_daily, eia_daily, resolve_daily, daily_sig_df)
    fig_daily_shift_significance_table(daily_sig_df)

    # Per-utility IEPR and RESOLVE daily peaks for utility-level shift analysis
    print("\nLoading IEPR per-utility daily peaks (all projected years)...")
    iepr_util_daily = load_iepr_daily_peaks_by_utility()

    print("\nLoading RESOLVE per-utility daily peaks...")
    resolve_util_daily = load_resolve_daily_peaks_by_utility()

    # IEPR vs RESOLVE vs substation shift figures
    print("\nGenerating IEPR vs RESOLVE vs substation shift figures...")
    print("\nLoading ReEDS daily peaks (IRA_low, 2025 target year)...")
    reeds_daily = load_reeds_daily_peak_hour(target_year=2025)
    if not reeds_daily.empty:
        print(f"  ReEDS daily peaks: {len(reeds_daily):,} days across "
              f"{reeds_daily['weather_year'].nunique()} weather years")
    fig_iepr_resolve_substation_shift(total_coin, iepr_daily, resolve_daily,
                                      reeds_daily=reeds_daily)
    fig_peak_hour_monthly_by_utility(
        util_coin, total_coin,
        iepr_util_daily, iepr_daily,
        resolve_util_daily, resolve_daily,
    )

    # BTM_PV shape and growth figures
    print("\nLoading IEPR BTM_PV across all vintages and years...")
    btm_all = load_btm_pv_all_years()
    print("\nGenerating BTM_PV shape invariance figure...")
    fig_btm_pv_shape_invariance(btm_all)
    print("Generating BTM_PV annual growth figure...")
    fig_btm_pv_annual_growth(btm_all)
    print("Generating BTM PV + storage combined figure...")
    fig_btm_combined_vs_pv(btm_all)
    print("\nLoading IEPR net load profiles (for peak hour by year analysis)...")
    net_df = load_iepr_net_load_profiles()
    print("Generating BTM peak hour by projected year figure...")
    fig_btm_peak_hour_by_year(net_df)

    print_summary(total_coin, mh_stats, iepr_total, cal_stats)
    print("\nDone.")


if __name__ == "__main__":
    main()
