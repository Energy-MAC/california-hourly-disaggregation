"""
compare_substation_eia_iepr.py

Compares substation ICA load profiles (coincident sum) to:
  - EIA-930 CISO realized demand (month-hour mean + inter-annual range)
  - IEPR BASELINE_NET_LOAD (PGE+SCE+SDGE, by vintage)

Substation profiles represent the high-load-day (max_load) and low-load-day
(min_load) at each substation for each month-hour.  Summing these across all
substations gives the COINCIDENT load bounds as measured at distribution level.

PGE and SDGE have no year stamp -- their profiles are fixed month-hour overlays.
SCE has years 2017-2026 and is included in the aggregate.

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

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
FIGS = ROOT / "data" / "figures"
FIGS.mkdir(parents=True, exist_ok=True)

SUBS_FILE = PROC / "substations" / "substation_load_profiles_clean.csv"
EIA_FILE  = PROC / "eia" / "eia930_operations.csv"
CAL_FILE  = PROC / "eia" / "eia930_cal_region.csv"
IEPR_FILE = PROC / "iepr" / "iepr_hourly_forecast.csv"

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

MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

# For IEPR: use year 2024 from vintages 2023/2024; year 2025 from vintage 2025
IEPR_REPR_YEAR = {2023: 2024, 2024: 2024, 2025: 2025}


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_substation_coincident() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns:
        total_coin  -- coincident sum across all utilities by (month, hour)
        util_coin   -- coincident sum by (utility, month, hour)
    """
    df = pd.read_csv(SUBS_FILE)
    total_coin = (
        df.groupby(["month", "hour"])[["max_load", "min_load"]]
        .sum()
        .reset_index()
        .rename(columns={"max_load": "coin_max_mw", "min_load": "coin_min_mw"})
    )
    util_coin = (
        df.groupby(["utility", "month", "hour"])[["max_load", "min_load"]]
        .sum()
        .reset_index()
        .rename(columns={"max_load": "coin_max_mw", "min_load": "coin_min_mw"})
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

    df["dt_pt"] = (
        df["datetime_utc"]
        .dt.tz_localize("UTC")
        .dt.tz_convert("US/Pacific")
    )
    df["year"]  = df["dt_pt"].dt.year
    df["month"] = df["dt_pt"].dt.month
    df["hour"]  = df["dt_pt"].dt.hour

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

    ts = df["datetime_utc"]
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("UTC")
    df["dt_pt"] = ts.dt.tz_convert("US/Pacific")
    df["year"]  = df["dt_pt"].dt.year
    df["month"] = df["dt_pt"].dt.month
    df["hour"]  = df["dt_pt"].dt.hour

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
    total_coin: pd.DataFrame,
    mh_stats:   pd.DataFrame,
    iepr_total: pd.DataFrame,
    cal_stats:  pd.DataFrame,
) -> None:
    fig, axes = plt.subplots(3, 4, figsize=(18, 12), sharey=False)
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
    fig.suptitle(
        "Monthly 24-Hour Load Profiles: Substation Coincident Sum vs EIA CISO vs IEPR\n"
        "Substation = PGE+SCE+SDGE distribution substations; "
        "EIA = CISO BA realized demand; IEPR = BASELINE_NET_LOAD (Local_Reliability)",
        fontsize=11, y=1.01,
    )
    plt.tight_layout()
    out = FIGS / "substation_vs_eia_iepr_monthly_profiles.png"
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
    out = FIGS / f"substation_vs_iepr_utility_{MONTH_NAMES[month-1].lower()}.png"
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
    print(f"  EIA CISO mean demand range: {mh_stats['eia_mean'].min():,.0f} - "
          f"{mh_stats['eia_mean'].max():,.0f} MW")

    print("\nLoading EIA CAL region...")
    cal_stats, cal_yr_mh = load_cal_region()
    if not cal_stats.empty:
        print(f"  EIA CAL mean demand range: {cal_stats['cal_mean'].min():,.0f} - "
              f"{cal_stats['cal_mean'].max():,.0f} MW")

    print("\nLoading IEPR hourly (Local_Reliability, BASELINE_NET_LOAD)...")
    iepr_total, iepr_util = load_iepr_hourly()

    print("\nGenerating figures...")
    fig_monthly_profiles(total_coin, mh_stats, iepr_total, cal_stats)
    fig_coverage_heatmap(total_coin, mh_stats, cal_stats)
    fig_utility_breakdown(util_coin, iepr_util, month=8)   # August (summer peak)
    fig_utility_breakdown(util_coin, iepr_util, month=1)   # January (winter)
    fig_monthly_peaks(total_coin, mh_stats, iepr_total, cal_stats)

    print_summary(total_coin, mh_stats, iepr_total, cal_stats)
    print("\nDone.")


if __name__ == "__main__":
    main()
