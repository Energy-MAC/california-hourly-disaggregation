"""
Compare IEPR load projections against EIA-930 realized demand.

Four figures saved to data/figures/:
  fig1_iepr_vs_eia_annual.png     -- IEPR annual projections vs EIA actual
  fig2_eia_forecast_vs_actual.png -- EIA day-ahead forecast vs realized demand
  fig3_iepr_vintages_overlay.png  -- IEPR vintage projections overlaid
  fig4_daily_peak_alignment.png   -- Daily peak-hour distribution: IEPR vs EIA

Correlation and error statistics are printed to the console.

Geographic note: IEPR uses utility-territory boundaries; EIA uses balancing-authority
boundaries.  CISO (CAISO) ~= PGE + SCE + SDGE.  NEVP and PACW extend substantially
beyond California, inflating the EIA CA8 total relative to IEPR statewide.

Usage
-----
python scripts/data/compare_iepr_eia.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
PROC = ROOT / "data" / "processed"
FIGS = ROOT / "data" / "figures"
FIGS.mkdir(parents=True, exist_ok=True)

EIA_OPS       = PROC / "eia" / "eia930_operations.csv"
CAL_FILE_EIA  = PROC / "eia" / "eia930_cal_region_EIA.csv"
CAL_FILE_PUDL = PROC / "eia" / "eia930_cal_region_PUDL.csv"
IEPR_ANN      = PROC / "iepr" / "iepr_baseline_annual.csv"
IEPR_HRLY     = PROC / "iepr" / "iepr_hourly_forecast.csv"
REEDS_ANN     = PROC / "reeds" / "reeds_ca_load_annual.csv"

VINTAGE_COLORS = {2023: "#1f77b4", 2024: "#ff7f0e", 2025: "#2ca02c"}
EIA_COLOR      = "#222222"
CAL_COLOR      = "#9467bd"   # purple — PUDL CA5 sum (preferred for analysis)
CAL_EIA_COLOR  = "#d62728"   # red — EIA API CAL (shown in annual plots alongside PUDL)
REEDS_COLOR    = "#7f7f7f"   # gray — ReEDS IRA_low scenario

# IEPR utilities that map to CAISO (CISO) territory
CAISO_UTILS = ["PGE", "SCE", "SDGE"]


def _utc_to_pst(ts: pd.Series) -> pd.Series:
    """Convert a UTC datetime series (tz-aware or tz-naive) to fixed PST (UTC-8, no DST)."""
    if ts.dt.tz is not None:
        ts = ts.dt.tz_localize(None)
    return ts - pd.Timedelta(hours=8)


# ── Loaders ───────────────────────────────────────────────────────────────────

def _cal_annual_from(path: Path) -> pd.DataFrame:
    """Annual CAL demand TWh from any cal_region CSV; drops years < 95% complete."""
    if not path.exists():
        print(f"  WARNING: {path.name} not found -- skipping.")
        return pd.DataFrame(columns=["year", "twh"])
    df = pd.read_csv(path, usecols=["datetime_utc", "demand_mwh"],
                     parse_dates=["datetime_utc"])
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
    annual["twh"] = annual["demand_mwh"] / 1_000_000
    return annual[["year", "twh"]]


def load_cal_annual() -> pd.DataFrame:
    """
    Annual PUDL CA5 sum demand in TWh.  Drops years with < 95% of expected hours.
    CAL = BANC+CISO+IID+LDWP+TIDC from PUDL (preferred for analysis; EIA API CAL
    has data-quality issues in ~3.9% of hours).
    """
    return _cal_annual_from(CAL_FILE_PUDL)


def load_cal_annual_eia() -> pd.DataFrame:
    """Annual EIA API CAL region demand in TWh (for display alongside PUDL in annual plots)."""
    return _cal_annual_from(CAL_FILE_EIA)


def load_cal_monthly() -> pd.DataFrame:
    """
    CAL region monthly mean hourly demand in GW.  Drops partial months.
    Also returns day-ahead forecast when available.
    Uses PUDL CA5 sum (preferred over EIA API for data quality).
    """
    if not CAL_FILE_PUDL.exists():
        return pd.DataFrame(columns=["period_ts", "actual_gw", "forecast_gw"])
    df = pd.read_csv(
        CAL_FILE_PUDL,
        usecols=["datetime_utc", "demand_mwh", "demand_forecast_mwh"],
        parse_dates=["datetime_utc"],
    )
    df["period"] = df["datetime_utc"].dt.to_period("M")
    monthly = (
        df.groupby("period")[["demand_mwh", "demand_forecast_mwh"]]
        .agg(["mean", "count"])
        .reset_index()
    )
    monthly.columns = ["period", "actual_mean", "actual_n", "forecast_mean", "forecast_n"]
    expected = monthly["period"].dt.days_in_month * 24
    complete = monthly["actual_n"] >= expected * 0.95
    monthly  = monthly[complete].copy()
    monthly["period_ts"]   = monthly["period"].dt.to_timestamp()
    monthly["actual_gw"]   = monthly["actual_mean"]   / 1_000
    monthly["forecast_gw"] = monthly["forecast_mean"] / 1_000
    return monthly[["period_ts", "actual_gw", "forecast_gw"]]


def load_cal_daily_peaks() -> pd.DataFrame:
    """Daily peak hour (fixed PST, 0-23) and peak MW from the PUDL CA5 sum."""
    if not CAL_FILE_PUDL.exists():
        return pd.DataFrame(columns=["date", "peak_hour_cal", "peak_mw_cal"])
    df = pd.read_csv(CAL_FILE_PUDL, usecols=["datetime_utc", "demand_mwh"],
                     parse_dates=["datetime_utc"])
    df = df.dropna(subset=["demand_mwh"])
    df["dt_pst"] = _utc_to_pst(df["datetime_utc"])
    df["date"]   = df["dt_pst"].dt.normalize()
    df["hour"]   = df["dt_pst"].dt.hour
    idx      = df.groupby("date")["demand_mwh"].idxmax()
    peak_hrs = df.loc[idx, ["date", "hour", "demand_mwh"]]
    return peak_hrs.rename(columns={"hour": "peak_hour_cal",
                                    "demand_mwh": "peak_mw_cal"}).reset_index(drop=True)


def load_reeds_ca_annual() -> pd.DataFrame:
    """
    ReEDS IRA_low CA total annual energy (TWh) by (year, weather_year).

    Returns a DataFrame with columns: year, weather_year, annual_twh.
    Only the CA_total aggregate row is returned (p8+p9+p10+p11 summed).
    Source: data/processed/reeds/reeds_ca_load_annual.csv
    """
    if not REEDS_ANN.exists():
        return pd.DataFrame(columns=["year", "weather_year", "annual_twh"])
    df = pd.read_csv(REEDS_ANN)
    return df[df["region"] == "CA_total"][["year", "weather_year", "annual_twh"]].copy()


def load_iepr_statewide() -> pd.DataFrame:
    """
    Annual IEPR BASELINE_NET_LOAD (net of BTM PV and storage) for CAISO-territory
    utilities (PGE + SCE + SDGE), Local_Reliability scenario, per (vintage, year).

    Source: iepr_hourly_forecast.csv — each hourly row is in MW; summing all
    hours in a year gives MWh; dividing by 1e6 gives TWh.

    last_historical_year is still read from iepr_baseline_annual.csv using the
    Historical_Net_Peak sentinel (the only place that field appears).

    This replaces the former Total_Consumption (gross) comparison with a net-load
    comparison that is directly comparable to EIA measured demand.
    """
    # last_historical_year is only in the annual file
    ann = pd.read_csv(IEPR_ANN)
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
    raw = raw[
        (raw["utility_ba"].isin(CAISO_UTILS)) &
        (raw["scenario"] == "Local_Reliability")
    ]

    total = (
        raw.groupby(["forecast_vintage_year", "YEAR"])["BASELINE_NET_LOAD"]
        .sum()
        .reset_index()
        .rename(columns={
            "forecast_vintage_year": "vintage",
            "YEAR":                  "year",
            "BASELINE_NET_LOAD":     "twh",
        })
    )
    total["twh"] /= 1_000_000  # MW·h -> TWh
    total = total.merge(last_hist, left_on="vintage", right_index=True)
    return total


def load_iepr_gross_annual() -> pd.DataFrame:
    """
    Annual IEPR Total_Consumption (gross load, all utilities) from iepr_baseline_annual.csv.
    Sums across all utilities per (vintage, year); converts GWh to TWh.
    Includes last_historical_year so callers can split historical vs projected.
    """
    ann = pd.read_csv(IEPR_ANN)
    last_hist = (
        ann[ann["Historical_Net_Peak"].notna()]
        .groupby("forecast_vintage_year")["Year"]
        .max()
        .rename("last_historical_year")
    )
    total = (
        ann.groupby(["forecast_vintage_year", "Year"])["Total_Consumption"]
        .sum()
        .reset_index()
        .rename(columns={"forecast_vintage_year": "vintage", "Year": "year",
                         "Total_Consumption": "twh_gross"})
    )
    total["twh_gross"] /= 1_000  # GWh -> TWh
    total = total.merge(last_hist, left_on="vintage", right_index=True)
    return total


def load_eia_annual() -> pd.DataFrame:
    """
    Sum EIA demand across all CA8 BAs per calendar year. Returns TWh.
    Used as a reference line in Fig 1; includes NEVP/PACW load outside CA.
    Drops partial years (< 95% of expected hourly rows per BA).
    """
    df = pd.read_csv(
        EIA_OPS,
        usecols=["datetime_utc", "ba_code", "demand_mwh"],
        parse_dates=["datetime_utc"],
    )
    df["year"] = df["datetime_utc"].dt.year
    counts     = df.groupby(["ba_code", "year"]).size().reset_index(name="n_hours")
    full_ba_yrs = counts[counts["n_hours"] >= int(8760 * 0.95)]
    n_bas       = df["ba_code"].nunique()
    full_years  = (
        full_ba_yrs.groupby("year").size()
        .pipe(lambda s: s[s >= n_bas - 1])
        .index
    )
    annual = (
        df[df["year"].isin(full_years)]
        .groupby("year")["demand_mwh"]
        .sum()
        .reset_index()
    )
    annual["twh"] = annual["demand_mwh"] / 1_000_000
    return annual[["year", "twh"]]


def load_eia_ciso_annual() -> pd.DataFrame:
    """
    Annual EIA CISO BA demand in TWh.
    CISO is the CAISO balancing authority, covering PGE+SCE+SDGE service territory
    — the correct geographic match for IEPR BASELINE_NET_LOAD (PGE+SCE+SDGE).
    Drops years with < 95% of 8760 expected hours.
    """
    df = pd.read_csv(
        EIA_OPS,
        usecols=["datetime_utc", "ba_code", "demand_mwh"],
        parse_dates=["datetime_utc"],
    )
    df = df[df["ba_code"] == "CISO"].copy()
    df["year"] = df["datetime_utc"].dt.year
    counts     = df.groupby("year")["demand_mwh"].count()
    full_years = counts[counts >= int(8760 * 0.95)].index
    annual = (
        df[df["year"].isin(full_years)]
        .groupby("year")["demand_mwh"]
        .sum()
        .reset_index()
    )
    annual["twh"] = annual["demand_mwh"] / 1_000_000
    return annual[["year", "twh"]]


def load_eia_monthly() -> pd.DataFrame:
    """
    Sum demand across all BAs per hour, then compute monthly mean hourly demand.
    Drops partial months (< 95% of expected hours) to avoid misleading endpoints.
    Returns period_ts (month start), actual_gw, forecast_gw.
    """
    df = pd.read_csv(
        EIA_OPS,
        usecols=["datetime_utc", "demand_mwh", "demand_forecast_mwh"],
        parse_dates=["datetime_utc"],
    )
    hourly = (
        df.groupby("datetime_utc")[["demand_mwh", "demand_forecast_mwh"]]
        .sum()
        .reset_index()
    )
    hourly["period"] = hourly["datetime_utc"].dt.to_period("M")
    monthly = (
        hourly.groupby("period")[["demand_mwh", "demand_forecast_mwh"]]
        .agg(["mean", "count"])
        .reset_index()
    )
    monthly.columns = ["period", "actual_mean", "actual_n",
                       "forecast_mean", "forecast_n"]

    # Drop months where either series has fewer than 95% of expected hours
    expected = (monthly["period"].dt.days_in_month * 24)
    complete = (monthly["actual_n"] >= expected * 0.95) & (monthly["forecast_n"] >= expected * 0.95)
    monthly  = monthly[complete].copy()

    monthly["period_ts"]   = monthly["period"].dt.to_timestamp()
    monthly["actual_gw"]   = monthly["actual_mean"]   / 1_000
    monthly["forecast_gw"] = monthly["forecast_mean"] / 1_000
    return monthly[["period_ts", "actual_gw", "forecast_gw"]]


def load_iepr_daily_peaks(
    vintage_years: list[tuple[int, list[int]]],
    scenario: str = "Local_Reliability",
) -> pd.DataFrame:
    """
    Return daily peak hours from IEPR hourly, stacked across (vintage, years) pairs.

    vintage_years: list of (vintage, [projected_years]) tuples.
      Each pair produces rows labelled with that vintage and the lead in years.
      Pass only projected years (year > last_historical_year for that vintage).

    IEPR HOUR is 1-24 (hour-ending); subtracting 1 gives hour-beginning 0-23,
    matching EIA's UTC-to-Pacific hour convention.
    """
    # Load once, filter in one pass
    all_vintages = [v for v, _ in vintage_years]
    all_years    = sorted({y for _, ys in vintage_years for y in ys})
    raw = pd.read_csv(
        IEPR_HRLY,
        usecols=["forecast_vintage_year", "utility_ba", "scenario",
                 "YEAR", "MONTH", "DAY", "HOUR", "BASELINE_NET_LOAD"],
    )
    raw = raw[
        (raw["forecast_vintage_year"].isin(all_vintages))
        & (raw["scenario"] == scenario)
        & (raw["utility_ba"].isin(CAISO_UTILS))
        & (raw["YEAR"].isin(all_years))
    ]

    pieces: list[pd.DataFrame] = []
    for vintage, years in vintage_years:
        sub = raw[(raw["forecast_vintage_year"] == vintage) & (raw["YEAR"].isin(years))]
        hourly = (
            sub.groupby(["YEAR", "MONTH", "DAY", "HOUR"])["BASELINE_NET_LOAD"]
            .sum()
            .reset_index()
        )
        hourly["hour0"] = hourly["HOUR"] - 1  # hour-ending 1-24 -> hour-beginning 0-23
        idx      = hourly.groupby(["YEAR", "MONTH", "DAY"])["BASELINE_NET_LOAD"].idxmax()
        peak_hrs = hourly.loc[idx, ["YEAR", "MONTH", "DAY", "hour0", "BASELINE_NET_LOAD"]].copy()
        peak_hrs = peak_hrs.rename(columns={"hour0": "peak_hour_iepr",
                                            "BASELINE_NET_LOAD": "peak_mw_iepr"})
        peak_hrs["date"] = pd.to_datetime(
            peak_hrs[["YEAR", "MONTH", "DAY"]].rename(
                columns={"YEAR": "year", "MONTH": "month", "DAY": "day"}
            )
        )
        peak_hrs["vintage"]    = vintage
        peak_hrs["lead_years"] = peak_hrs["YEAR"] - vintage
        pieces.append(peak_hrs[["date", "vintage", "lead_years",
                                 "peak_hour_iepr", "peak_mw_iepr"]])
    return pd.concat(pieces, ignore_index=True)


def load_eia_daily_peaks_ciso() -> pd.DataFrame:
    """
    Return the daily peak hour (0-23, US/Pacific) and peak MW from EIA-930 CISO.
    """
    df = pd.read_csv(
        EIA_OPS,
        usecols=["datetime_utc", "ba_code", "demand_mwh"],
        parse_dates=["datetime_utc"],
    )
    ciso = df[df["ba_code"] == "CISO"].copy()
    ciso["dt_pst"] = _utc_to_pst(ciso["datetime_utc"])
    ciso["date"]   = ciso["dt_pst"].dt.normalize()
    ciso["hour"]   = ciso["dt_pst"].dt.hour
    ciso = ciso.dropna(subset=["demand_mwh"])

    idx      = ciso.groupby("date")["demand_mwh"].idxmax()
    peak_hrs = ciso.loc[idx, ["date", "hour", "demand_mwh"]]
    peak_hrs = peak_hrs.rename(columns={"hour": "peak_hour_eia",
                                        "demand_mwh": "peak_mw_eia"})
    return peak_hrs.reset_index(drop=True)


# ── Statistics helpers ────────────────────────────────────────────────────────

def _annual_stats(iepr: pd.DataFrame, eia: pd.DataFrame) -> dict[int, dict]:
    """Pearson r, R2, MAE, MAPE between IEPR projected TWh and EIA actual TWh."""
    result: dict[int, dict] = {}
    for vintage, grp in iepr.groupby("vintage"):
        last_h  = grp["last_historical_year"].iloc[0]
        proj    = grp[grp["year"] > last_h]
        merged  = proj.merge(eia, on="year", suffixes=("_iepr", "_eia"))
        if len(merged) < 2:
            result[vintage] = None
            continue
        r, pval   = stats.pearsonr(merged["twh_iepr"], merged["twh_eia"])
        mae        = (merged["twh_iepr"] - merged["twh_eia"]).abs().mean()
        mape       = ((merged["twh_iepr"] - merged["twh_eia"]).abs()
                      / merged["twh_eia"] * 100).mean()
        bias       = (merged["twh_iepr"] - merged["twh_eia"]).mean()
        result[vintage] = {
            "n": len(merged), "r": r, "r2": r**2,
            "mae_twh": mae, "mape_pct": mape, "bias_twh": bias, "p": pval,
            "years": sorted(merged["year"].tolist()),
        }
    return result


def _monthly_corr_stats(monthly: pd.DataFrame) -> dict:
    """Pearson r, RMSE, MAE, MAPE, bias for EIA day-ahead forecast vs actual."""
    valid      = monthly.dropna(subset=["actual_gw", "forecast_gw"])
    r, pval    = stats.pearsonr(valid["actual_gw"], valid["forecast_gw"])
    err        = valid["forecast_gw"] - valid["actual_gw"]
    return {
        "n":        len(valid),
        "r":        r,
        "r2":       r**2,
        "rmse_gw":  float(np.sqrt((err**2).mean())),
        "mae_gw":   float(err.abs().mean()),
        "mape_pct": float((err.abs() / valid["actual_gw"] * 100).mean()),
        "bias_gw":  float(err.mean()),
        "p":        pval,
    }


def _peak_alignment_stats(peaks: pd.DataFrame) -> dict:
    """t-test, Pearson r, and offset distribution for daily peak hours."""
    offset = peaks["peak_hour_iepr"] - peaks["peak_hour_eia"]
    t_stat, t_p = stats.ttest_1samp(offset.dropna(), popmean=0)
    r, r_p      = stats.pearsonr(
        peaks["peak_hour_iepr"].dropna(),
        peaks["peak_hour_eia"].dropna(),
    )
    return {
        "n":          len(peaks),
        "mean_offset": float(offset.mean()),
        "std_offset":  float(offset.std()),
        "t_stat":      float(t_stat),
        "t_p":         float(t_p),
        "r":           float(r),
        "r_p":         float(r_p),
        "pct_exact":   float((offset == 0).mean() * 100),
        "pct_within1": float((offset.abs() <= 1).mean() * 100),
    }


# ── Plot helpers ──────────────────────────────────────────────────────────────

def _plot_iepr_vintage(ax, grp: pd.DataFrame, vintage: int, color: str,
                       label: str | None = None) -> None:
    """Draw one IEPR vintage: dashed for historical portion, solid for projected."""
    last_h   = grp["last_historical_year"].iloc[0]
    hist     = grp[grp["year"] <= last_h]
    proj     = grp[grp["year"] >  last_h]
    ax.plot(hist["year"], hist["twh"], color=color, lw=1.2, ls="--", alpha=0.45)
    ax.plot(proj["year"], proj["twh"], color=color, lw=2,
            label=label or f"IEPR {vintage}")
    boundary = grp[grp["year"] == last_h]
    if not boundary.empty:
        ax.plot(boundary["year"].values[0], boundary["twh"].values[0],
                "o", color=color, ms=5, zorder=5)


# ── Figures ───────────────────────────────────────────────────────────────────

def fig1_iepr_vs_eia(
    iepr: pd.DataFrame,
    eia_ca8: pd.DataFrame,
    cal_pudl: pd.DataFrame,
    eia_ciso: pd.DataFrame,
    ann_stats: dict,
    cal_eia: pd.DataFrame | None = None,
    reeds: pd.DataFrame | None = None,
) -> None:
    fig, ax = plt.subplots(figsize=(11, 5))

    for vintage, grp in iepr.groupby("vintage"):
        color = VINTAGE_COLORS.get(vintage, "gray")
        s     = ann_stats.get(vintage)
        lbl   = f"IEPR v{vintage} (PGE+SCE+SDGE net)"
        if s:
            lbl += f"  (MAE={s['mae_twh']:.0f} TWh vs CISO, n={s['n']})"
        _plot_iepr_vintage(ax, grp, vintage, color, label=lbl)

    # EIA CISO — primary comparison for IEPR (same geographic scope)
    ax.plot(eia_ciso["year"], eia_ciso["twh"], color=EIA_COLOR, lw=2.5,
            marker="o", ms=5, label="EIA CISO (CAISO BA — same scope as IEPR)")

    # CAL region: PUDL CA5 sum (solid) and EIA API (dashed, for comparison)
    if not cal_pudl.empty:
        ax.plot(cal_pudl["year"], cal_pudl["twh"], color=CAL_COLOR, lw=1.8,
                marker="^", ms=4,
                label="PUDL CA5 sum (BANC+CISO+IID+LDWP+TIDC)")
    if cal_eia is not None and not cal_eia.empty:
        ax.plot(cal_eia["year"], cal_eia["twh"], color=CAL_EIA_COLOR, lw=1.2,
                marker="v", ms=3, linestyle="--", alpha=0.7,
                label="EIA API CAL region (data quality issues in some years)")

    ax.plot(eia_ca8["year"], eia_ca8["twh"], color=EIA_COLOR, lw=1.2,
            marker="s", ms=3, linestyle=":", alpha=0.6,
            label="EIA CA8 sum (8 BAs incl. NEVP/PACW)")

    # ReEDS IRA_low CA total (p8+p9+p10+p11): mean + min/max across weather years
    if reeds is not None and not reeds.empty:
        rd_mean = reeds.groupby("year")["annual_twh"].mean()
        rd_min  = reeds.groupby("year")["annual_twh"].min()
        rd_max  = reeds.groupby("year")["annual_twh"].max()
        ax.plot(rd_mean.index, rd_mean.values, color=REEDS_COLOR, lw=1.8,
                ls="--", marker="x", ms=4,
                label="ReEDS IRA_low CA total (p8+p9+p10+p11, all CA — mean across 7 weather years)")
        ax.fill_between(rd_mean.index, rd_min.values, rd_max.values,
                        color=REEDS_COLOR, alpha=0.12, label="_nolegend_")

    ax.set_xlabel("Year")
    ax.set_ylabel("Annual demand (TWh)")
    ax.set_title(
        "California load: IEPR BASELINE_NET_LOAD vs. EIA-930 realized demand\n"
        "IEPR (PGE+SCE+SDGE net) vs CISO = apples-to-apples; "
        "CAL (PUDL and EIA API) and CA8 shown as broader state/regional references"
    )
    ax.set_xlim(2015, 2035)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.legend(fontsize=8)
    fig.tight_layout()
    out = FIGS / "fig1_iepr_vs_eia_annual.png"
    fig.savefig(out, dpi=150)
    print(f"Saved {out}")
    plt.close(fig)


def fig2_eia_fcst_vs_actual(
    monthly: pd.DataFrame,
    cal_monthly: pd.DataFrame,
    fcast_stats: dict,
) -> None:
    fig, ax = plt.subplots(figsize=(12, 4.5))
    ax.plot(monthly["period_ts"], monthly["actual_gw"],
            color=EIA_COLOR, lw=1.5, label="CA8 actual (8 BAs)")
    ax.plot(monthly["period_ts"], monthly["forecast_gw"],
            color="#e74c3c", lw=1, alpha=0.85, label="CA8 day-ahead forecast")
    if not cal_monthly.empty:
        ax.plot(cal_monthly["period_ts"], cal_monthly["actual_gw"],
                color=CAL_COLOR, lw=1.5, alpha=0.85, label="CAL region actual")

    # Stats in title to avoid overlap with legend
    stats_line = (
        f"CA8 forecast vs actual:  r={fcast_stats['r']:.4f}  "
        f"MAE={fcast_stats['mae_gw']:.2f} GW  "
        f"bias={fcast_stats['bias_gw']:+.2f} GW  "
        f"MAPE={fcast_stats['mape_pct']:.1f}%"
    )
    ax.set_xlabel("Month")
    ax.set_ylabel("Mean hourly demand (GW)")
    ax.set_title(
        "EIA-930: day-ahead demand forecast vs. realized demand (monthly mean)\n"
        "CA8 sum (8 BAs, incl. NEVP/PACW) vs CAL region (CA boundary only)\n"
        + stats_line,
        fontsize=10,
    )
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.legend(fontsize=9, loc="lower left")
    fig.tight_layout()
    out = FIGS / "fig2_eia_forecast_vs_actual.png"
    fig.savefig(out, dpi=150)
    print(f"Saved {out}")
    plt.close(fig)


def fig3_iepr_vintages(
    iepr: pd.DataFrame,
    iepr_gross: pd.DataFrame | None = None,
) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))

    # Gross (all utilities, Total_Consumption) as dotted reference lines — plot first so
    # they sit behind the net lines
    if iepr_gross is not None and not iepr_gross.empty:
        for vintage, grp in iepr_gross.groupby("vintage"):
            color = VINTAGE_COLORS.get(vintage, "gray")
            ax.plot(grp["year"], grp["twh_gross"], color=color,
                    lw=1.5, ls=":", alpha=0.55,
                    label=f"IEPR v{vintage} Total_Consumption (gross, all utilities)")

    # Net load (PGE+SCE+SDGE BASELINE_NET_LOAD) — solid for projected, dashed for historical
    for vintage, grp in iepr.groupby("vintage"):
        color  = VINTAGE_COLORS.get(vintage, "gray")
        last_h = grp["last_historical_year"].iloc[0]
        _plot_iepr_vintage(ax, grp, vintage, color)
        ax.axvline(last_h, color=color, lw=0.7, ls=":", alpha=0.45)

    ax.set_xlabel("Year")
    ax.set_ylabel("Annual demand (TWh)")
    ax.set_title(
        "IEPR load by forecast vintage -- PGE+SCE+SDGE (Local_Reliability)\n"
        "Solid = BASELINE_NET_LOAD (net, PGE+SCE+SDGE);  "
        "dotted = Total_Consumption (gross, all utilities)\n"
        "Dashed = historical portion;  vertical line = vintage boundary year",
        fontsize=9,
    )
    ax.set_xlim(2010, 2050)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.legend(fontsize=8, ncol=2)
    fig.tight_layout(rect=[0, 0, 1, 0.91])
    out = FIGS / "fig3_iepr_vintages_overlay.png"
    fig.savefig(out, dpi=150)
    print(f"Saved {out}")
    plt.close(fig)


def fig4_peak_alignment(
    peaks: pd.DataFrame,
    pk_stats: dict,
    cal_peaks: pd.DataFrame,
) -> None:
    """
    Two-panel figure:
      Left  -- histogram of (IEPR peak hour - EIA CISO peak hour) offset;
               also shows CAL region peak-hour distribution
      Right -- mean daily peak hour by calendar month for IEPR, EIA CISO, and CAL
    """
    offset = peaks["peak_hour_iepr"] - peaks["peak_hour_eia"]
    peaks  = peaks.copy()
    peaks["month"] = peaks["date"].dt.month

    mon_labels = ["Jan","Feb","Mar","Apr","May","Jun",
                  "Jul","Aug","Sep","Oct","Nov","Dec"]

    fig, (ax_hist, ax_mon) = plt.subplots(1, 2, figsize=(14, 5))

    # -- Histogram of IEPR-CISO offset --------------------------------------
    bins = np.arange(offset.min() - 0.5, offset.max() + 1.5, 1)
    ax_hist.hist(offset, bins=bins, edgecolor="white", color="#5b9bd5",
                 linewidth=0.5, alpha=0.8, label="IEPR - EIA CISO offset")
    ax_hist.axvline(0, color="black", lw=1.5, ls="--", label="No offset")
    ax_hist.axvline(pk_stats["mean_offset"], color="#e74c3c", lw=1.5,
                    label=f"Mean = {pk_stats['mean_offset']:+.2f} h")
    ax_hist.set_xlabel("Peak hour offset  (IEPR - EIA CISO, hours)")
    ax_hist.set_ylabel("Number of days")
    ann = (f"n={pk_stats['n']}  mean={pk_stats['mean_offset']:+.2f}h  "
           f"std={pk_stats['std_offset']:.2f}h\n"
           f"t-test p={pk_stats['t_p']:.4f}  "
           f"within +/-1 h: {pk_stats['pct_within1']:.0f}%")
    ax_hist.text(0.97, 0.97, ann, transform=ax_hist.transAxes, fontsize=8,
                 ha="right", va="top", color="#333333")
    ax_hist.legend(fontsize=8)
    ax_hist.set_title("Daily peak-hour offset: IEPR vs. EIA-930 CISO")

    # -- Mean peak hour by month --------------------------------------------
    x = np.arange(1, 13)
    has_cal = not cal_peaks.empty
    n_bars  = 3 if has_cal else 2
    w       = 0.26 if has_cal else 0.35

    grp_iepr = peaks.groupby("month")["peak_hour_iepr"].mean()
    grp_eia  = peaks.groupby("month")["peak_hour_eia"].mean()

    ax_mon.bar(x - w, grp_eia.reindex(x), width=w,
               label="EIA-930 CISO", color=EIA_COLOR, alpha=0.8)
    ax_mon.bar(x, grp_iepr.reindex(x), width=w,
               label=f"IEPR {CAISO_UTILS[0]}+{CAISO_UTILS[1]}+{CAISO_UTILS[2]}",
               color="#5b9bd5", alpha=0.8)

    if has_cal:
        cal_peaks_m = cal_peaks.copy()
        cal_peaks_m["month"] = cal_peaks_m["date"].dt.month
        grp_cal = cal_peaks_m.groupby("month")["peak_hour_cal"].mean()
        ax_mon.bar(x + w, grp_cal.reindex(x), width=w,
                   label="EIA CAL region", color=CAL_COLOR, alpha=0.8)

    ax_mon.set_xticks(x)
    ax_mon.set_xticklabels(mon_labels)
    ax_mon.set_ylabel("Mean daily peak hour (0 = midnight, 17 = 5 PM)")
    ax_mon.set_title("Mean daily peak hour by month")
    ax_mon.legend(fontsize=8)
    ax_mon.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda h, _: f"{int(h):02d}:00")
    )

    fig.suptitle(
        "Daily peak-hour alignment: IEPR projected years vs. EIA-930 CISO vs. EIA CAL region\n"
        "IEPR BASELINE_NET_LOAD (PGE+SCE+SDGE, Local_Reliability) vs. EIA CISO vs. CAL, "
        "Pacific time; stacked across all available (vintage, projected-year) pairs",
        fontsize=8,
    )
    fig.tight_layout()
    out = FIGS / "fig4_daily_peak_alignment.png"
    fig.savefig(out, dpi=150)
    print(f"Saved {out}")
    plt.close(fig)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # ── Load data ────────────────────────────────────────────────────────────
    print("Loading IEPR annual (net load) ...")
    iepr = load_iepr_statewide()

    print("Loading IEPR gross annual (Total_Consumption, all utilities) ...")
    iepr_gross = load_iepr_gross_annual()

    print("Loading EIA annual (CA8 sum) ...")
    eia_ann = load_eia_annual()
    print(f"  full years: {sorted(eia_ann['year'].tolist())}")

    print("Loading EIA CISO annual (CAISO BA, matches IEPR scope) ...")
    eia_ciso_ann = load_eia_ciso_annual()
    print(f"  full years: {sorted(eia_ciso_ann['year'].tolist())}")

    print("Loading CAL region annual (PUDL CA5 sum) ...")
    cal_ann = load_cal_annual()
    if not cal_ann.empty:
        print(f"  full years: {sorted(cal_ann['year'].tolist())}")

    print("Loading CAL region annual (EIA API — for annual plot comparison) ...")
    cal_ann_eia = load_cal_annual_eia()
    if not cal_ann_eia.empty:
        print(f"  full years: {sorted(cal_ann_eia['year'].tolist())}")

    print("Loading EIA monthly ...")
    monthly = load_eia_monthly()

    print("Loading EIA CAL region monthly ...")
    cal_monthly = load_cal_monthly()

    # Build (vintage, projected_years) pairs — only years that are:
    #   (a) projected (year > last_historical_year for that vintage), AND
    #   (b) fully covered by EIA actual data
    eia_full_years = set(eia_ann["year"].tolist())
    last_hist_by_v = (
        iepr.groupby("vintage")["last_historical_year"].first().to_dict()
    )
    vintage_years: list[tuple[int, list[int]]] = []
    for vintage, last_h in sorted(last_hist_by_v.items()):
        proj_and_realized = sorted(
            eia_full_years & {y for y in range(last_h + 1, last_h + 20)}
        )
        if proj_and_realized:
            vintage_years.append((vintage, proj_and_realized))
            print(f"  Vintage {vintage}: projected years with EIA data = {proj_and_realized}")

    all_peak_years = sorted({y for _, ys in vintage_years for y in ys})
    print(f"Loading IEPR hourly peaks (stacked across {len(vintage_years)} vintage(s)) ...")
    iepr_peaks = load_iepr_daily_peaks(vintage_years)

    print("Loading EIA CISO daily peaks ...")
    eia_peaks = load_eia_daily_peaks_ciso()
    eia_peaks = eia_peaks[eia_peaks["date"].dt.year.isin(all_peak_years)]

    print("Loading EIA CAL region daily peaks ...")
    cal_peaks = load_cal_daily_peaks()
    if not cal_peaks.empty:
        cal_peaks = cal_peaks[cal_peaks["date"].dt.year.isin(all_peak_years)]

    peaks = iepr_peaks.merge(eia_peaks, on="date")
    print(f"  {len(peaks)} matched (date, vintage) rows across {len(all_peak_years)} calendar years\n")

    # ── Compute statistics ────────────────────────────────────────────────────
    ann_stats   = _annual_stats(iepr, eia_ciso_ann)  # IEPR vs CISO (same scope)
    fcast_stats = _monthly_corr_stats(monthly)
    pk_stats    = _peak_alignment_stats(peaks)

    # ── Print statistics ──────────────────────────────────────────────────────
    print("=" * 60)
    print("ANNUAL IEPR BASELINE_NET_LOAD vs EIA CISO  (projected years only)")
    print("IEPR = PGE+SCE+SDGE net load; EIA = CISO BA (same CAISO territory)")
    print("=" * 60)
    for vintage, s in ann_stats.items():
        if s is None:
            print(f"  Vintage {vintage}: insufficient overlap")
            continue
        print(f"  Vintage {vintage}  years={s['years']}")
        print(f"    r={s['r']:.4f}  R2={s['r2']:.4f}  p={s['p']:.4f}")
        print(f"    MAE={s['mae_twh']:.1f} TWh  MAPE={s['mape_pct']:.1f}%  "
              f"bias={s['bias_twh']:+.1f} TWh  (positive = IEPR over-projects)")

    print()
    print("=" * 60)
    print("EIA DAY-AHEAD FORECAST vs ACTUAL  (monthly means, all CA8)")
    print("=" * 60)
    s = fcast_stats
    print(f"  n={s['n']} months")
    print(f"  r={s['r']:.4f}  R2={s['r2']:.4f}  p={s['p']:.2e}")
    print(f"  MAE={s['mae_gw']:.3f} GW  RMSE={s['rmse_gw']:.3f} GW  "
          f"bias={s['bias_gw']:+.3f} GW  MAPE={s['mape_pct']:.2f}%")

    print()
    print("=" * 60)
    print("DAILY PEAK-HOUR ALIGNMENT  (IEPR projected vs EIA CISO)")
    print("=" * 60)
    for vt, ys in vintage_years:
        print(f"  Vintage {vt}: compared against years {ys}")
    s = pk_stats
    print(f"  n={s['n']} (date, vintage) rows  calendar years={all_peak_years}")
    print(f"  Mean offset (IEPR - EIA): {s['mean_offset']:+.3f} h  "
          f"std={s['std_offset']:.3f} h")
    print(f"  One-sample t-test (H0: mean=0):  t={s['t_stat']:+.3f}  p={s['t_p']:.4f}")
    if s['t_p'] < 0.05:
        direction = "later" if s['mean_offset'] > 0 else "earlier"
        print(f"  ** IEPR peak is systematically {direction} by "
              f"~{abs(s['mean_offset']):.2f} h (p < 0.05) **")
    else:
        print("  No statistically significant systematic offset detected (p >= 0.05)")
    print(f"  Pearson r (peak hours): {s['r']:.4f}  p={s['r_p']:.4f}")
    print(f"  Exact hour match:       {s['pct_exact']:.1f}% of days")
    print(f"  Within +/-1 hour:       {s['pct_within1']:.1f}% of days")

    # ── Recommendations ───────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("RECOMMENDED ADDITIONAL STATISTICAL TESTS")
    print("=" * 60)
    recs = [
        ("Load duration curve comparison (KS test)",
         "Sort hourly demand descending for both IEPR and EIA to build load duration "
         "curves (LDCs). A two-sample Kolmogorov-Smirnov test on the distributions "
         "tests whether IEPR and EIA imply the same overall load shape, not just the "
         "same mean. Wasserstein (Earth Mover) distance quantifies how far apart the "
         "distributions are in energy terms."),
        ("Seasonal bias decomposition",
         "Compute mean (IEPR - EIA) by season (DJF, MAM, JJA, SON). IEPR may "
         "over-project summer peaks (heat events, AC) and under-project winter peaks "
         "(building electrification). An ANOVA on seasonal residuals tests whether "
         "forecast bias is season-dependent."),
        ("Annual peak day and peak magnitude accuracy",
         "For each year, identify the single highest-demand day in EIA and in IEPR. "
         "Compare (a) whether it falls in the same month, (b) the difference in peak "
         "magnitude. A signed t-test on peak-magnitude errors over multiple years "
         "tests for systematic over/under-prediction of extreme events."),
        ("Year-over-year growth rate comparison",
         "Compute annual YoY demand growth for both IEPR projections and EIA actuals. "
         "Theil-Sen slope estimation gives a robust trend line for each. A two-sample "
         "t-test on growth rates checks whether IEPR's assumed trend matches reality."),
        ("Duck curve depth (net load ramp) comparison",
         "For each day, compute the afternoon ramp rate (demand increase from the "
         "solar-trough minimum around 13:00-15:00 to the evening peak around 19:00-21:00). "
         "Pearson r between IEPR and EIA ramp magnitudes, plus a t-test on ramp-rate "
         "differences, reveals whether IEPR correctly models how BTM solar is shaping "
         "net load. This is closely related to your daily peak-hour question."),
        ("Peak magnitude scatter (IEPR vs EIA, per-day)",
         "Scatter plot of IEPR daily peak MW vs EIA CISO daily peak MW with an OLS "
         "regression line. Slope != 1 indicates a systematic scale bias; intercept != 0 "
         "indicates a level shift. Include R2 and 95% prediction intervals."),
    ]
    for i, (title, desc) in enumerate(recs, 1):
        print(f"\n  {i}. {title}")
        # Wrap description at 70 chars
        words = desc.split()
        line, lines = "", []
        for w in words:
            if len(line) + len(w) + 1 > 70:
                lines.append(line)
                line = w
            else:
                line = (line + " " + w).strip()
        if line:
            lines.append(line)
        for ln in lines:
            print(f"     {ln}")

    print("Loading ReEDS IRA_low CA annual projections ...")
    reeds_ann = load_reeds_ca_annual()
    if not reeds_ann.empty:
        print(f"  years: {sorted(reeds_ann['year'].unique().tolist())}")

    # ── Figures ───────────────────────────────────────────────────────────────
    print()
    print("Generating figures ...")
    fig1_iepr_vs_eia(iepr, eia_ann, cal_ann, eia_ciso_ann, ann_stats,
                     cal_eia=cal_ann_eia, reeds=reeds_ann)
    fig2_eia_fcst_vs_actual(monthly, cal_monthly, fcast_stats)
    fig3_iepr_vintages(iepr, iepr_gross)
    fig4_peak_alignment(peaks, pk_stats, cal_peaks)
    print(f"\nDone. Figures saved to {FIGS}")


if __name__ == "__main__":
    main()
