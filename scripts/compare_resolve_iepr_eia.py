"""
compare_resolve_iepr_eia.py

Compares RESOLVE Baseline load projections against IEPR and EIA-930 demand.

RESOLVE (E3 / CPUC IRP) and IEPR both serve California IRP, but differ in
scope, load definition, and representation:

Load definition hierarchy (gross -> net)
-----------------------------------------
  RESOLVE Baseline Consumption   (= IEPR BASELINE_CONSUMPTION)
      Gross load before BTM solar subtraction.  Includes EV charging, data
      centers, climate impacts already embedded in historical shapes.  This
      is the demand RESOLVE optimises against, with BTM PV modelled as a
      supply-side resource.  ~241 TWh PGE+SCE+SDGE (2024 targets).

  IEPR BASELINE_CONSUMPTION  (hourly column in iepr_hourly_forecast.csv)
      Identical concept to RESOLVE Baseline.  Gross load at the grid busbar
      including T&D losses, before BTM_PV and BTM_STORAGE subtraction.

  IEPR BASELINE_NET_LOAD  (= BASELINE_CONSUMPTION - BTM_PV - BTM_STORAGE)
      Net-of-BTM-solar.  Comparable to EIA-930 measured demand.

  IEPR MANAGED_NET_LOAD  (= BASELINE_NET_LOAD + AAEE + AAFS + AATE overlays)
      Final scenario load after all demand-side programme overlays applied.
      This is "IEPR Total CAISO Load" as referenced in RESOLVE I&A Table 2.
      ~217 TWh PGE+SCE+SDGE (2025, Local_Reliability scenario).

  EIA-930 demand_mwh
      Measured demand at balancing-authority level.  Net of BTM generation.

Reconstruction identity (from RESOLVE I&A Table 2)
---------------------------------------------------
  RESOLVE Baseline + AATE_LDVs + AATE_MHDVs + AAFS + Data_Centers
    + Climate_Impacts + Storage_Losses - AAEE - BTM_PV
    ≈ IEPR MANAGED_NET_LOAD

Geographic scope differences (CA8 vs CAISO vs RESOLVE vs ReEDS)
-----------------------------------------------------------------
  RESOLVE CAISO zone: PGE + SCE + SDGE
  RESOLVE CA total:   PGE + SCE + SDGE + IID + LDWP + NCNC

  EIA CISO:    CAISO balancing authority (= PGE + SCE + SDGE territory)
  EIA IID:     Imperial Irrigation District (in RESOLVE as "IID")
  EIA LDWP:    Los Angeles Dept. of Water & Power (in RESOLVE as "LDWP")
  EIA BANC:    Balancing Authority of Northern California (NOT in RESOLVE)
  EIA TIDC:    Turlock Irrigation District (not in RESOLVE; ~1 TWh/yr)

  EIA WALC:    Western Area Lower Colorado (not in RESOLVE; mostly out of CA); Not included in CAL region

  EIA NEVP:    NV Energy (Nevada Power + Sierra Pacific Power).  Serves
               Nevada entirely; ~0.4% of NEVP load is in California
               (verified via EIA Form 861).  2024 CA load ≈ 0.18 TWh.

  EIA PACW:    PacifiCorp West.  Serves OR, WA, ID, WY, UT, and a small
               slice of far-northern CA (Del Norte, Siskiyou, Modoc).
               ~4% of PACW load is in California.  2024 CA load ≈ 0.85 TWh.

  EIA CAL:     EIA's geographic "CAL" region (available 2019+).  Excludes
               out-of-state NEVP/PACW load.  Best apples-to-apples
               comparison for total California electricity demand.

  ReEDS p9-p11 (WECC_CA): Empirically found to track PUDL CA5 sum
               (~BANC+CISO+IID+LDWP+TIDC, ~265-274 TWh/yr), NOT EIA CISO
               alone (~224 TWh/yr).  The ~40 TWh gap between p9-p11 and CISO
               is approximately equal to IID+LDWP+BANC+TIDC combined, confirming
               that WECC_CA in ReEDS = all California BAs except PacifiCorp West.
               Do NOT compare ReEDS p9-p11 directly to EIA CISO; compare to
               PUDL CA5 or EIA CAL instead.
  ReEDS p8 (WECC_NW CA slice): PacifiCorp West California territory only,
               ~0.8 TWh/yr.  So CA total (p8+p9+p10+p11) ≈ WECC_CA + ~0.8 TWh.

Outputs
-------
  Console: annual TWh tables, decomposition statistics, reconstruction check
  data/figures/fig_resolve_vs_iepr_eia_annual.png
  data/figures/fig_resolve_scope_decomposition.png
  data/figures/fig_resolve_hourly_shape.png

Usage
-----
  python scripts/compare_resolve_iepr_eia.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

# Force UTF-8 output on Windows consoles that default to cp1252
if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower().replace("-", "") not in ("utf8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from scipy import stats

ROOT  = Path(__file__).resolve().parents[1]
PROC  = ROOT / "data" / "processed"
FIGS  = ROOT / "data" / "figures"
FIGS.mkdir(parents=True, exist_ok=True)

RESOLVE_ANN  = PROC / "resolve"  / "resolve_annual_forecast.csv"
RESOLVE_HRLY = PROC / "resolve"  / "resolve_hourly_profiles.csv"
EIA_OPS      = PROC / "eia"      / "eia930_operations.csv"
EIA_CAL      = PROC / "eia"      / "eia930_cal_region_EIA.csv"
PUDL_CAL     = PROC / "eia"      / "eia930_cal_region_PUDL.csv"
IEPR_ANN     = PROC / "iepr"     / "iepr_baseline_annual.csv"
IEPR_HRLY    = PROC / "iepr"     / "iepr_hourly_forecast.csv"
REEDS_ANN    = PROC / "reeds"    / "reeds_ca_load_annual.csv"
REEDS_HRLY   = PROC / "reeds"    / "reeds_ca_load_hourly.parquet"
HIST_LOAD_ANN  = PROC / "reeds"  / "historic_ca_load_annual.csv"

# RESOLVE optimization outputs (auto-detect latest timestamped run folder)
def _find_resolve_outputs() -> Path | None:
    raw_dir = ROOT / "data" / "raw" / "Raw RESOLVE Outputs"
    if not raw_dir.exists():
        return None
    candidates = sorted([p for p in raw_dir.iterdir()
                         if p.is_dir() and (p / "summary").exists()])
    return candidates[-1] if candidates else None

RESOLVE_OUTPUTS = _find_resolve_outputs()

CAISO_UTILS  = ["PGE", "SCE", "SDGE"]
RESOLVE_UTILS = ["PGE", "SCE", "SDGE", "IID", "LDWP", "NCNC"]

# EIA BAs present in California (inside CA or overlapping)
# NEVP and PACW are listed but extend substantially outside CA
EIA_CA8_BAS  = ["CISO", "IID", "LDWP", "BANC", "TIDC", "WALC", "NEVP", "PACW"]
EIA_INCA_BAS = ["CISO", "IID", "LDWP", "BANC", "TIDC"]  # excludes NEVP, PACW, "WALC"
NEVP_PACW    = ["NEVP", "PACW"]


# ── Loaders ───────────────────────────────────────────────────────────────────

def _reeds_hourly_ca(target_year: int = 2022) -> pd.DataFrame:
    """
    ReEDS CA total (p8-p11) mean hourly load across 7 weather years for a target year.

    Uses nearest available target year if the exact year is not in the data.
    Returns DataFrame with columns: time_index, month, hour, load_mw_mean.
    """
    if not REEDS_HRLY.exists():
        return pd.DataFrame(columns=["time_index", "month", "hour", "load_mw_mean"])
    df = pd.read_parquet(REEDS_HRLY, filters=[("year", "=", target_year)])
    if df.empty:
        all_df = pd.read_parquet(REEDS_HRLY, columns=["year"]).drop_duplicates()
        available = sorted(all_df["year"].unique())
        nearest = min(available, key=lambda y: abs(y - target_year))
        df = pd.read_parquet(REEDS_HRLY, filters=[("year", "=", nearest)])
    # Step 1: sum 4 CA regions per (weather_year, time_index)
    ca = (df.groupby(["weather_year", "time_index", "month", "hour"])["load_mw"]
            .sum().reset_index())
    # Step 2: mean across 7 weather years per time_index
    mean_h = (ca.groupby(["time_index", "month", "hour"])["load_mw"]
                .mean().reset_index()
                .rename(columns={"load_mw": "load_mw_mean"}))
    return mean_h


def _reeds_annual() -> pd.DataFrame:
    """
    ReEDS IRA_low CA total annual energy (TWh) by (year, weather_year).

    Returns CA_total rows (p8+p9+p10+p11 summed).
    Use for comparisons against PUDL CA5 sum or EIA CAL geographic region.
    Columns: year, weather_year, annual_twh.
    """
    if not REEDS_ANN.exists():
        return pd.DataFrame(columns=["year", "weather_year", "annual_twh"])
    df = pd.read_csv(REEDS_ANN)
    return df[df["region"] == "CA_total"][["year", "weather_year", "annual_twh"]].copy()


def _reeds_annual_caiso() -> pd.DataFrame:
    """
    ReEDS IRA_low CAISO annual energy (TWh) by (year, weather_year).

    Returns CAISO_total rows (p9+p10+p11 only; excludes p8 PacifiCorp CA slice).
    Use for comparisons against EIA CISO or other CAISO-territory sources.
    Columns: year, weather_year, annual_twh.
    """
    if not REEDS_ANN.exists():
        return pd.DataFrame(columns=["year", "weather_year", "annual_twh"])
    df = pd.read_csv(REEDS_ANN)
    if "CAISO_total" not in df["region"].values:
        # Fallback: compute on the fly from individual regions (pre-CAISO_total output)
        caiso = (df[df["region"].isin(["p9", "p10", "p11"])]
                 .groupby(["year", "scenario", "weather_year"])["annual_twh"]
                 .sum().reset_index())
        return caiso[["year", "weather_year", "annual_twh"]].copy()
    return df[df["region"] == "CAISO_total"][["year", "weather_year", "annual_twh"]].copy()


def _historic_annual() -> pd.DataFrame:
    """
    Historic CA load annual energy (TWh) by year and region.

    Source: process_historic_load.py from
    data/raw/PotentialData/historic_post2015_load_hourly.h5
    Covers 2016-2023.  Timestamps in CST (UTC-6); annual totals use CST
    calendar year grouping (~0.02% annual shift vs PST year — negligible).

    Returns tidy DataFrame:
      Columns: year | region | annual_twh
      Region values: p8, p9, p10, p11, CAISO_total, CA_total
    """
    if not HIST_LOAD_ANN.exists():
        return pd.DataFrame(columns=["year", "region", "annual_twh"])
    return pd.read_csv(HIST_LOAD_ANN)[["year", "region", "annual_twh"]].copy()


def _eia_annual_by_ba() -> pd.DataFrame:
    """Annual TWh per BA, filtered to full years (>= 95% of expected hours)."""
    df = pd.read_csv(EIA_OPS, usecols=["datetime_utc", "ba_code", "demand_mwh"],
                     parse_dates=["datetime_utc"])
    df["year"] = df["datetime_utc"].dt.year
    counts = df.groupby(["ba_code", "year"])["demand_mwh"].count().reset_index(name="n")
    full   = counts[counts["n"] >= int(8760 * 0.95)][["ba_code", "year"]]
    df     = df.merge(full, on=["ba_code", "year"])
    ann    = (df.groupby(["ba_code", "year"])["demand_mwh"].sum() / 1e6).reset_index()
    ann.columns = ["ba_code", "year", "twh"]
    return ann


def _cal_annual_from(path: Path) -> pd.DataFrame:
    """Annual CAL demand TWh from any cal_region CSV; drops years < 95% complete."""
    if not path.exists():
        return pd.DataFrame(columns=["year", "twh"])
    df = pd.read_csv(path, usecols=["datetime_utc", "demand_mwh"],
                     parse_dates=["datetime_utc"])
    if df["datetime_utc"].dt.tz is not None:
        df["datetime_utc"] = df["datetime_utc"].dt.tz_localize(None)
    df["year"] = df["datetime_utc"].dt.year
    counts = df.groupby("year")["demand_mwh"].count()
    full   = counts[counts >= int(8760 * 0.95)].index
    ann    = (df[df["year"].isin(full)].groupby("year")["demand_mwh"].sum() / 1e6
              ).reset_index(name="twh")
    return ann


def _eia_cal_annual() -> pd.DataFrame:
    """EIA API CAL region annual TWh (geographic CA boundary, available 2019+)."""
    return _cal_annual_from(EIA_CAL)


def _pudl_cal_annual() -> pd.DataFrame:
    """PUDL CA5 sum annual TWh (BANC+CISO+IID+LDWP+TIDC; preferred for analysis)."""
    return _cal_annual_from(PUDL_CAL)


def _iepr_net_annual() -> pd.DataFrame:
    """IEPR BASELINE_NET_LOAD (PGE+SCE+SDGE, Local_Reliability) in TWh."""
    raw = pd.read_csv(IEPR_HRLY,
                      usecols=["forecast_vintage_year", "utility_ba", "scenario",
                               "YEAR", "BASELINE_NET_LOAD"])
    raw = raw[(raw["utility_ba"].isin(CAISO_UTILS)) &
              (raw["scenario"] == "Local_Reliability")]
    ann = (raw.groupby(["forecast_vintage_year", "YEAR"])["BASELINE_NET_LOAD"]
              .sum().reset_index())
    ann.columns = ["vintage", "year", "twh"]
    ann["twh"] /= 1e6
    return ann


def _iepr_consumption_annual() -> pd.DataFrame:
    """IEPR Total_Consumption (all IEPR utilities, GWh -> TWh) from annual file."""
    df = pd.read_csv(IEPR_ANN)
    # Filter to CAISO scope utilities matching RESOLVE
    caiso_iepr = ["PGE", "SCE", "SDGE"]
    df = df[df["utility_ba"].isin(caiso_iepr)] if "utility_ba" in df.columns else df
    ann = (df.groupby(["forecast_vintage_year", "Year"])["Total_Consumption"]
             .sum().reset_index())
    ann.columns = ["vintage", "year", "twh_gross"]
    ann["twh_gross"] /= 1_000  # GWh -> TWh
    return ann


def _iepr_last_hist() -> dict[int, int]:
    """last_historical_year per IEPR vintage."""
    ann = pd.read_csv(IEPR_ANN)
    if "Historical_Net_Peak" not in ann.columns:
        return {}
    last = (ann[ann["Historical_Net_Peak"].notna()]
              .groupby("forecast_vintage_year")["Year"].max()
              .to_dict())
    return last


def _resolve_annual() -> pd.DataFrame:
    """RESOLVE Baseline annual energy forecasts in TWh."""
    return pd.read_csv(RESOLVE_ANN)


def _resolve_hourly() -> pd.DataFrame:
    """RESOLVE hourly profiles, raw (MW)."""
    return pd.read_csv(RESOLVE_HRLY, parse_dates=["datetime_pst"])


def _iepr_baseline_consumption_annual() -> pd.DataFrame:
    """
    IEPR BASELINE_CONSUMPTION annual TWh — gross load before BTM solar subtraction.

    BASELINE_CONSUMPTION = UNADJUSTED + PUMPING + CLIMATE_CHANGE + LIGHT_EV
                           + MEDIUM_HEAVY_EV + DATA_CENTER + OTHER_ADJUSTMENTS.
    This is the gross load concept comparable to RESOLVE Baseline Consumption
    (both are pre-BTM-solar).  Summed over PGE+SCE+SDGE, Local_Reliability.
    """
    cols = ["forecast_vintage_year", "utility_ba", "scenario", "YEAR",
            "BASELINE_CONSUMPTION"]
    raw = pd.read_csv(IEPR_HRLY, usecols=cols)
    raw = raw[(raw["utility_ba"].isin(CAISO_UTILS)) &
              (raw["scenario"] == "Local_Reliability")]
    ann = (raw.groupby(["forecast_vintage_year", "YEAR"])["BASELINE_CONSUMPTION"]
              .sum().reset_index())
    ann.columns = ["vintage", "year", "twh"]
    ann["twh"] /= 1e6
    return ann


def _iepr_managed_annual() -> pd.DataFrame:
    """
    IEPR MANAGED_NET_LOAD annual TWh — final net load after all overlays.

    MANAGED_NET_LOAD = BASELINE_NET_LOAD + AATE_LDV + AATE_MDHD + AAFS - AAEE.
    This is 'IEPR Total CAISO Load' as referenced in RESOLVE I&A Table 2 and
    the target that RESOLVE Baseline + overlays - BTM_PV reconstructs.
    Summed over PGE+SCE+SDGE, Local_Reliability.
    """
    cols = ["forecast_vintage_year", "utility_ba", "scenario", "YEAR",
            "MANAGED_NET_LOAD"]
    raw = pd.read_csv(IEPR_HRLY, usecols=cols)
    raw = raw[(raw["utility_ba"].isin(CAISO_UTILS)) &
              (raw["scenario"] == "Local_Reliability")]
    ann = (raw.groupby(["forecast_vintage_year", "YEAR"])["MANAGED_NET_LOAD"]
              .sum().reset_index())
    ann.columns = ["vintage", "year", "twh"]
    ann["twh"] /= 1e6
    return ann


def _iepr_btm_pv_annual() -> pd.DataFrame:
    """
    IEPR BTM_PV annual TWh — the BTM solar generation subtracted from gross load
    to produce BASELINE_NET_LOAD.  Used in the Baseline + overlays reconstruction.
    Summed over PGE+SCE+SDGE, Local_Reliability.
    """
    cols = ["forecast_vintage_year", "utility_ba", "scenario", "YEAR", "BTM_PV"]
    raw = pd.read_csv(IEPR_HRLY, usecols=cols)
    raw = raw[(raw["utility_ba"].isin(CAISO_UTILS)) &
              (raw["scenario"] == "Local_Reliability")]
    ann = (raw.groupby(["forecast_vintage_year", "YEAR"])["BTM_PV"]
              .sum().reset_index())
    ann.columns = ["vintage", "year", "twh"]
    ann["twh"] /= 1e6
    return ann


# Overlay components that should be ADDED to RESOLVE Baseline (positive = load increase)
# AAEE is negative (demand reduction), others are positive
_OVERLAY_COMPONENTS = [
    "AATE_LDVs",       # light-duty EV incremental charging
    "AATE_MHDVs",      # medium/heavy-duty EV incremental charging
    "AAFS",            # additional fuel substitution (building electrification)
    "AAEE",            # additional energy efficiency (negative — demand reduction)
    "Data_Centers",    # data center growth overlay
    "Climate_Impacts", # climate warming demand increase
    "Storage_Losses",  # BTM storage net charging losses
]


def _resolve_outputs_overlays() -> pd.DataFrame | None:
    """
    RESOLVE optimization output: annual energy by load overlay component.

    Returns tidy DataFrame with columns:
      utility | component | year | twh_avg
    where twh_avg is 'Average Annual Energy' (interpolated between modeled years).
    Only CAISO utilities (PGE, SCE, SDGE) and overlay components are included.
    AAEE values are negative (demand reduction).
    Returns None if RESOLVE_OUTPUTS path is not found.
    """
    if RESOLVE_OUTPUTS is None:
        return None
    summary = RESOLVE_OUTPUTS / "summary" / "Load_annual_results_summary.csv"
    if not summary.exists():
        return None

    df = pd.read_csv(summary)
    df["year"] = pd.to_datetime(df["Modeled Year"]).dt.year
    df["twh"]  = pd.to_numeric(df["Average Annual Energy (MWh)"], errors="coerce") / 1e6

    # Parse "PGE_AATE_LDVs" -> utility="PGE", component="AATE_LDVs"
    df["utility"]   = df["Component Name"].str.split("_", n=1).str[0]
    df["component"] = df["Component Name"].str.split("_", n=1).str[1]

    mask = (df["utility"].isin(CAISO_UTILS) &
            df["component"].isin(_OVERLAY_COMPONENTS))
    out = df[mask][["utility", "component", "year", "twh"]].copy()
    return out.reset_index(drop=True)


# ── Derived aggregates ────────────────────────────────────────────────────────

def _eia_pivot(ann: pd.DataFrame) -> pd.DataFrame:
    """Wide table: year | CISO | IID | LDWP | BANC | ... | CA8 | INCA | NEVP_PACW"""
    piv = ann.pivot_table(index="year", columns="ba_code", values="twh")
    piv["CA8"]       = piv[[c for c in EIA_CA8_BAS  if c in piv.columns]].sum(axis=1)
    piv["INCA"]      = piv[[c for c in EIA_INCA_BAS if c in piv.columns]].sum(axis=1)
    piv["NEVP_PACW"] = piv[[c for c in NEVP_PACW    if c in piv.columns]].sum(axis=1)
    return piv.reset_index()


# ── Statistics ────────────────────────────────────────────────────────────────

def _compare_stats(a: pd.Series, b: pd.Series, label_a: str, label_b: str) -> dict:
    """Bias, MAE, MAPE, Pearson r between two aligned series."""
    diff = a - b
    return {
        "n":        len(a),
        "mean_a":   float(a.mean()),
        "mean_b":   float(b.mean()),
        "bias":     float(diff.mean()),
        "bias_pct": float(diff.mean() / b.mean() * 100),
        "mae":      float(diff.abs().mean()),
        "mape":     float((diff.abs() / b.abs() * 100).mean()),
        "r":        float(stats.pearsonr(a, b)[0]) if len(a) >= 3 else float("nan"),
        "label_a":  label_a,
        "label_b":  label_b,
    }


def _print_stats(s: dict) -> None:
    print(f"    {s['label_a']} mean: {s['mean_a']:.1f}  |  "
          f"{s['label_b']} mean: {s['mean_b']:.1f}  |  "
          f"bias: {s['bias']:+.1f} ({s['bias_pct']:+.1f}%)  |  "
          f"MAE: {s['mae']:.1f}  |  r: {s['r']:.4f}")


# ── Figures ───────────────────────────────────────────────────────────────────

COLORS = {
    "resolve":        "#2ca02c",
    "iepr_net":       "#1f77b4",
    "iepr_gross":     "#aec7e8",
    "eia_ciso":       "#222222",
    "eia_cal":        "#9467bd",
    "eia_ca8":        "#bcbd22",
    "eia_inca":       "#8c564b",
    "nevp_pacw":      "#e74c3c",
    "reeds":          "#7f7f7f",     # projected CA total (p8-p11)
    "reeds_caiso":    "#b5b5b5",     # projected CAISO (p9-p11)
    "hist_caiso":     "#d62728",     # historic CAISO actual (p9-p11)
    "hist_ca":        "#ff7f0e",     # historic CA total actual (p8-p11)
}


def fig1_annual_comparison(
    resolve: pd.DataFrame,
    iepr_net: pd.DataFrame,
    iepr_gross: pd.DataFrame,
    eia_piv: pd.DataFrame,
    cal_ann: pd.DataFrame,
    iepr_cons: pd.DataFrame | None = None,
    iepr_mgd: pd.DataFrame | None = None,
    cal_ann_eia: pd.DataFrame | None = None,
    reeds: pd.DataFrame | None = None,
    reeds_caiso: pd.DataFrame | None = None,
    hist: pd.DataFrame | None = None,
) -> None:
    fig, ax = plt.subplots(figsize=(13, 7))

    # RESOLVE Baseline (CAISO: PGE+SCE+SDGE) — gross, pre-BTM-solar
    res_caiso = (resolve[resolve["utility"].isin(CAISO_UTILS)]
                 .groupby("year")["energy_twh"].sum().reset_index())
    ax.plot(res_caiso["year"], res_caiso["energy_twh"],
            color=COLORS["resolve"], lw=2.5, marker="D", ms=5, zorder=5,
            label="RESOLVE Baseline PGE+SCE+SDGE (gross, pre-BTM-solar, 2024 IRP targets)")

    # IEPR BASELINE_CONSUMPTION (hourly gross) — latest vintage only
    if iepr_cons is not None and not iepr_cons.empty:
        latest_v = iepr_cons["vintage"].max()
        ic = iepr_cons[iepr_cons["vintage"] == latest_v]
        ax.plot(ic["year"], ic["twh"],
                color="#17becf", lw=2, ls="-.", marker="v", ms=4, zorder=4,
                label=f"IEPR v{latest_v} BASELINE_CONSUMPTION PGE+SCE+SDGE (gross, pre-BTM-solar)")

    # IEPR MANAGED_NET_LOAD (final net load, all overlays applied) — latest vintage
    if iepr_mgd is not None and not iepr_mgd.empty:
        latest_v = iepr_mgd["vintage"].max()
        im = iepr_mgd[iepr_mgd["vintage"] == latest_v]
        ax.plot(im["year"], im["twh"],
                color="#e377c2", lw=1.8, ls="--", marker="P", ms=4, zorder=4,
                label=f"IEPR v{latest_v} MANAGED_NET_LOAD PGE+SCE+SDGE (net, all overlays)")

    # IEPR BASELINE_NET_LOAD — each vintage as coloured solid line
    vintage_colors = {2023: "#1f77b4", 2024: "#ff7f0e", 2025: "#2ca02c"}
    last_hist = _iepr_last_hist()
    for vintage, grp in iepr_net.groupby("vintage"):
        last_h   = last_hist.get(vintage, 9999)
        col      = vintage_colors.get(vintage, "gray")
        proj     = grp[grp["year"] > last_h]
        iepr_his = grp[grp["year"] <= last_h]
        ax.plot(iepr_his["year"], iepr_his["twh"], color=col, lw=1.2, ls="--", alpha=0.4)
        ax.plot(proj["year"], proj["twh"], color=col, lw=2,
                label=f"IEPR v{vintage} BASELINE_NET_LOAD PGE+SCE+SDGE (net of BTM solar)")
        bnd = grp[grp["year"] == last_h]
        if not bnd.empty:
            ax.plot(bnd["year"].iloc[0], bnd["twh"].iloc[0], "o", color=col, ms=5)

    # IEPR Total_Consumption (annual workbook gross, for reference)
    if not iepr_gross.empty:
        latest_vintage = iepr_gross["vintage"].max()
        ig = iepr_gross[iepr_gross["vintage"] == latest_vintage]
        ax.plot(ig["year"], ig["twh_gross"],
                color=COLORS["iepr_gross"], lw=1.2, ls=":",
                label=f"IEPR v{latest_vintage} Total_Consumption annual workbook (gross, ref only)")

    # EIA CISO
    ciso = eia_piv[eia_piv["CISO"].notna()][["year", "CISO"]]
    ax.plot(ciso["year"], ciso["CISO"],
            color=COLORS["eia_ciso"], lw=2.5, marker="o", ms=5, zorder=5,
            label="EIA-930 CISO BA (measured, net of BTM solar)")

    # CAL region: PUDL CA5 sum (solid) and EIA API (dashed, for comparison)
    if not cal_ann.empty:
        ax.plot(cal_ann["year"], cal_ann["twh"],
                color=COLORS["eia_cal"], lw=1.8, marker="^", ms=4,
                label="PUDL CA5 sum (BANC+CISO+IID+LDWP+TIDC, net of BTM solar)")
    if cal_ann_eia is not None and not cal_ann_eia.empty:
        ax.plot(cal_ann_eia["year"], cal_ann_eia["twh"],
                color="#d62728", lw=1.2, marker="v", ms=3, ls="--", alpha=0.7,
                label="EIA API CAL region (data quality issues in some years)")

    # EIA CA8 total (all 8 BAs incl. NEVP/PACW)
    ca8 = eia_piv[eia_piv["CA8"].notna()][["year", "CA8"]]
    ax.plot(ca8["year"], ca8["CA8"],
            color=COLORS["eia_ca8"], lw=1.2, marker="s", ms=3, ls=":",
            label="EIA CA8 sum (8 BAs incl. NEVP+PACW — ~1 TWh actual CA, rest out-of-state)")

    # ReEDS IRA_low CA total (p8+p9+p10+p11): mean across 7 weather years, no band
    # WECC_CA (p9-p11) + WECC_NW CA slice (p8, ~0.8 TWh/yr).
    # Use for comparison against PUDL CA5 sum / EIA CAL geographic CA boundary.
    if reeds is not None and not reeds.empty:
        rd_mean = reeds.groupby("year")["annual_twh"].mean()
        ax.plot(rd_mean.index, rd_mean.values,
                color=COLORS["reeds"], lw=1.8, ls="--",
                marker="x", ms=4, zorder=3,
                label="ReEDS IRA_low CA total p8-p11 (WECC_CA + PACW CA slice, net projected)")

    # ReEDS IRA_low WECC_CA (p9+p10+p11): mean across 7 weather years, no band
    # Empirically tracks PUDL CA5 sum, not EIA CISO — see scope note in docstring.
    # p8 (PacifiCorp CA slice) adds only ~0.8 TWh/yr; difference vs CA total is invisible at this scale.
    if reeds_caiso is not None and not reeds_caiso.empty:
        rc_mean = reeds_caiso.groupby("year")["annual_twh"].mean()
        ax.plot(rc_mean.index, rc_mean.values,
                color=COLORS["reeds_caiso"], lw=1.5, ls=":",
                marker="+", ms=4, zorder=3,
                label="ReEDS IRA_low WECC_CA p9-p11 (all CA excl. PACW, net projected)")

    # ReEDS historic WECC_CA (p9+p10+p11), 2016-2023 actual observed load
    # Source: historic_post2015_load_hourly.h5 via process_historic_load.py
    # NOTE: p9-p11 tracks PUDL CA5 (~BANC+CISO+IID+LDWP+TIDC), not EIA CISO alone.
    #   EIA CISO gap is ~40 TWh/yr; CA total (p8-p11) differs from p9-p11 by only ~0.8 TWh.
    if hist is not None and not hist.empty:
        h_wca = hist[hist["region"] == "CAISO_total"]   # column named CAISO_total = p9+p10+p11
        if not h_wca.empty:
            ax.plot(h_wca["year"], h_wca["annual_twh"],
                    color=COLORS["hist_caiso"], lw=2, marker="o", ms=5, zorder=6,
                    label="ReEDS historic WECC_CA p9-p11 (2016-2023 actual, all CA excl. PACW)")

    ax.set_xlabel("Year")
    ax.set_ylabel("Annual demand (TWh)")
    ax.set_xlim(2015, 2045)
    ax.set_title(
        "California electricity demand: RESOLVE vs IEPR vs EIA-930\n"
        "Gross sources (RESOLVE Baseline, IEPR BASELINE_CONSUMPTION) are pre-BTM-solar; "
        "net sources (IEPR NET_LOAD, MANAGED, EIA) have BTM solar subtracted"
    )
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.legend(fontsize=7.5, loc="upper left", bbox_to_anchor=(1.01, 1), borderaxespad=0)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    out = FIGS / "fig_resolve_vs_iepr_eia_annual.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved {out}")
    plt.close(fig)


def fig2_scope_decomposition(eia_piv: pd.DataFrame, resolve: pd.DataFrame,
                              cal_ann: pd.DataFrame,
                              cal_ann_eia: pd.DataFrame | None = None,
                              reeds: pd.DataFrame | None = None) -> None:
    """
    Paired bar chart: for each geographic scope, EIA (left) and RESOLVE (right) side-by-side.
    RESOLVE CAISO bars are stacked by utility (PGE / SCE / SDGE).
    Right section aggregates: in-CA sum and EIA CAL vs RESOLVE All CA.
    """
    yr_eia = 2022
    row = eia_piv[eia_piv["year"] == yr_eia]
    if row.empty:
        print(f"  WARNING: no EIA data for {yr_eia}, skipping scope decomposition figure.")
        return
    row = row.iloc[0]

    # RESOLVE values from closest forecast year (2024 if available)
    yr_res = yr_eia + 2
    res_row = resolve[resolve["year"] == yr_res]
    if res_row.empty:
        res_row = resolve[resolve["year"] == resolve["year"].min()]
    yr_res_lbl = int(res_row["year"].iloc[0]) if not res_row.empty else yr_res

    def rv(util: str) -> float:
        v = res_row[res_row["utility"] == util]["energy_twh"]
        return float(v.iloc[0]) if not v.empty else 0.0

    def ev(ba: str) -> float:
        val = row.get(ba, float("nan"))
        return float(val) if pd.notna(val) else 0.0

    pge, sce, sdge = rv("PGE"), rv("SCE"), rv("SDGE")
    iid_r, ldwp_r, ncnc_r = rv("IID"), rv("LDWP"), rv("NCNC")

    cal_v: float | None = None
    if not cal_ann.empty:
        c = cal_ann[cal_ann["year"] == yr_eia]
        if not c.empty:
            cal_v = float(c["twh"].iloc[0])

    cal_v_eia: float | None = None
    if cal_ann_eia is not None and not cal_ann_eia.empty:
        c = cal_ann_eia[cal_ann_eia["year"] == yr_eia]
        if not c.empty:
            cal_v_eia = float(c["twh"].iloc[0])

    # Nearest ReEDS target year to yr_eia for comparison
    reeds_mean_twh: float | None = None
    reeds_min_twh:  float | None = None
    reeds_max_twh:  float | None = None
    reeds_yr_lbl:   int          = yr_eia
    if reeds is not None and not reeds.empty:
        available_yrs = sorted(reeds["year"].unique())
        nearest_yr = min(available_yrs, key=lambda y: abs(y - yr_eia))
        r = reeds[reeds["year"] == nearest_yr]["annual_twh"]
        reeds_mean_twh = float(r.mean())
        reeds_min_twh  = float(r.min())
        reeds_max_twh  = float(r.max())
        reeds_yr_lbl   = nearest_yr

    # ── Colors ────────────────────────────────────────────────────────────────
    C_EIA_IN  = "#1f77b4"   # EIA in-CA / mostly-CA BAs
    C_EIA_OUT = "#e74c3c"   # EIA mostly-out-of-state BAs (NEVP, PACW)
    C_EIA_CAL = "#9467bd"   # EIA geographic CA region
    C_REEDS   = COLORS["reeds"]
    # RESOLVE utility colors — green ramp for CAISO, purple ramp for non-CAISO
    C_PGE  = "#1a7a2e"
    C_SCE  = "#2ca02c"
    C_SDGE = "#74c476"
    C_IID  = "#807dba"
    C_LDWP = "#6a51a3"
    C_NCNC = "#54278f"

    # ── Layout constants ──────────────────────────────────────────────────────
    BAR_W  = 0.33      # bar width
    PAIR_D = 0.40      # center-to-center between EIA and RESOLVE bars in a group
    IND_D  = 1.05      # center-to-center between individual BA groups
    SUM_D  = 1.15      # center-to-center between summary groups

    fig, ax = plt.subplots(figsize=(21, 6))

    # ── Helper: draw a stacked RESOLVE bar and return its top y ───────────────
    def _stacked_bar(cx: float, comps: list, label_min_twh: float = 7.0) -> float:
        bottom = 0.0
        for cl, cv, cc in comps:
            ax.bar(cx, cv, BAR_W, bottom=bottom, color=cc, alpha=0.9,
                   edgecolor="white", linewidth=0.5)
            if cv >= label_min_twh:
                ax.text(cx, bottom + cv / 2, cl, ha="center", va="center",
                        fontsize=5.5, color="white", fontweight="bold")
            bottom += cv
        ax.text(cx, bottom + 0.8, f"{bottom:.0f}",
                ha="center", va="bottom", fontsize=6.5)
        return bottom

    # ── Individual BA groups ─────────────────────────────────────────────────
    # Each group: left bar = EIA, right bar = RESOLVE (stacked if multiple utilities)
    ind_groups: list[tuple] = [
        ("CAISO / CISO", "CISO", C_EIA_IN,
         [("PGE", pge, C_PGE), ("SCE", sce, C_SCE), ("SDGE", sdge, C_SDGE)]),
        ("IID", "IID", C_EIA_IN,
         [("IID", iid_r, C_IID)]),
        ("LDWP", "LDWP", C_EIA_IN,
         [("LDWP", ldwp_r, C_LDWP)]),
        ("BANC", "BANC", C_EIA_IN, []),
        ("TIDC", "TIDC", C_EIA_IN, []),
        ("WALC\n(~31% in CA)", "WALC", C_EIA_IN, []),
        ("NEVP\n(mostly Nevada)", "NEVP", C_EIA_OUT, []),
        ("PACW\n(mostly OR/WA)", "PACW", C_EIA_OUT, []),
    ]

    tick_positions: list[float] = []
    tick_labels:    list[str]   = []
    x = 0.0

    for lbl, ba, eia_color, res_comps in ind_groups:
        eia_val = ev(ba)
        eia_x   = x - PAIR_D / 2
        res_x   = x + PAIR_D / 2

        # EIA bar
        ax.bar(eia_x, eia_val, BAR_W, color=eia_color, alpha=0.85,
               edgecolor="white", linewidth=0.5)
        if eia_val > 0.3:
            ax.text(eia_x, eia_val + 0.8, f"{eia_val:.0f}",
                    ha="center", va="bottom", fontsize=6.5)

        # RESOLVE stacked bar (only if this scope exists in RESOLVE)
        if res_comps:
            _stacked_bar(res_x, res_comps)

        tick_positions.append(x)
        tick_labels.append(lbl)
        x += IND_D

    # ── Separator ─────────────────────────────────────────────────────────────
    sep_x = x - IND_D / 2 + 0.35
    ax.axvline(sep_x, color="gray", lw=1.5, ls="--", alpha=0.5)
    x = sep_x + 0.6

    # ── Summary groups: CA aggregates ─────────────────────────────────────────
    # RESOLVE All CA components (reused in each summary pair)
    res_all_comps = [
        ("PGE",  pge,    C_PGE),  ("SCE",  sce,    C_SCE),
        ("SDGE", sdge,   C_SDGE), ("IID",  iid_r,  C_IID),
        ("LDWP", ldwp_r, C_LDWP), ("NCNC", ncnc_r, C_NCNC),
    ]

    # Group A: EIA in-CA sum (excl. NEVP/PACW) vs RESOLVE All CA
    inca_v = ev("INCA")    # CISO + IID + LDWP + BANC + TIDC
    eia_x  = x - PAIR_D / 2
    res_x  = x + PAIR_D / 2
    ax.bar(eia_x, inca_v, BAR_W, color=C_EIA_IN, alpha=0.85, edgecolor="white", lw=0.5)
    ax.text(eia_x, inca_v + 0.8, f"{inca_v:.0f}", ha="center", va="bottom", fontsize=6.5)
    _stacked_bar(res_x, res_all_comps, label_min_twh=12.0)
    tick_positions.append(x)
    tick_labels.append("In-CA BAs\n(excl. NEVP/PACW)")
    x += SUM_D

    # Group B: CAL region — PUDL CA5 sum and (optionally) EIA API vs RESOLVE All CA
    if cal_v is not None:
        if cal_v_eia is not None:
            # Three-bar group: PUDL (left), EIA API (center-left), RESOLVE (right)
            pudl_x = x - PAIR_D * 0.7
            eia_x  = x
            res_x  = x + PAIR_D * 0.7
            ax.bar(pudl_x, cal_v, BAR_W, color=C_EIA_CAL, alpha=0.85,
                   edgecolor="white", lw=0.5)
            ax.text(pudl_x, cal_v + 0.8, f"{cal_v:.0f}",
                    ha="center", va="bottom", fontsize=6.5)
            ax.bar(eia_x, cal_v_eia, BAR_W, color="#d62728", alpha=0.7,
                   edgecolor="white", lw=0.5)
            ax.text(eia_x, cal_v_eia + 0.8, f"{cal_v_eia:.0f}",
                    ha="center", va="bottom", fontsize=6.5)
            _stacked_bar(res_x, res_all_comps, label_min_twh=12.0)
            tick_positions.append(x)
            tick_labels.append("CAL region\n(PUDL / EIA / RESOLVE)")
        else:
            eia_x = x - PAIR_D / 2
            res_x = x + PAIR_D / 2
            ax.bar(eia_x, cal_v, BAR_W, color=C_EIA_CAL, alpha=0.85,
                   edgecolor="white", lw=0.5)
            ax.text(eia_x, cal_v + 0.8, f"{cal_v:.0f}",
                    ha="center", va="bottom", fontsize=6.5)
            _stacked_bar(res_x, res_all_comps, label_min_twh=12.0)
            tick_positions.append(x)
            tick_labels.append("PUDL CA5 sum\n(geographic CA)")
        x += SUM_D

    # Group C: ReEDS IRA_low CA total (nearest target year to yr_eia)
    if reeds_mean_twh is not None:
        ax.bar(x, reeds_mean_twh, BAR_W, color=C_REEDS, alpha=0.85,
               edgecolor="white", lw=0.5)
        ax.errorbar(x, reeds_mean_twh,
                    yerr=[[reeds_mean_twh - reeds_min_twh],
                          [reeds_max_twh  - reeds_mean_twh]],
                    color=C_REEDS, capsize=4, lw=1.5, zorder=5)
        ax.text(x, reeds_max_twh + 1.0, f"{reeds_mean_twh:.0f}",
                ha="center", va="bottom", fontsize=6.5)
        tick_positions.append(x)
        tick_labels.append(f"ReEDS IRA_low\nCA total (~{reeds_yr_lbl})")
        x += SUM_D

    # ── Axes formatting ───────────────────────────────────────────────────────
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, rotation=20, ha="right", fontsize=7.5)
    ax.set_xlim(-0.7, x - SUM_D + 0.8)
    ax.set_ylabel("Annual demand (TWh)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.set_title(
        f"Demand scope: EIA-930 ({yr_eia}) vs RESOLVE Baseline (~{yr_res_lbl} forecast targets)\n"
        "Right of dashed line: CA aggregates — PUDL CA5 sum ≈ EIA API CAL ≈ in-CA BAs ≈ RESOLVE CA total."
    )

    # ── Legend ────────────────────────────────────────────────────────────────
    patches = [
        mpatches.Patch(color=C_EIA_IN,  alpha=0.85,
                       label=f"EIA {yr_eia}: in-CA BAs (CISO, IID, LDWP, BANC, TIDC)"),
        mpatches.Patch(color=C_EIA_OUT, alpha=0.85,
                       label=f"EIA {yr_eia}: mostly out-of-state (NEVP≈Nevada, PACW≈OR/WA)"),
        mpatches.Patch(color=C_EIA_CAL, alpha=0.85,
                       label=f"PUDL {yr_eia}: CA5 sum (BANC+CISO+IID+LDWP+TIDC)"),
        mpatches.Patch(color="#d62728", alpha=0.7,
                       label=f"EIA API {yr_eia}: CAL geographic region"),
        mpatches.Patch(color=C_PGE,  alpha=0.9, label=f"RESOLVE PGE  (~{yr_res_lbl})"),
        mpatches.Patch(color=C_SCE,  alpha=0.9, label=f"RESOLVE SCE  (~{yr_res_lbl})"),
        mpatches.Patch(color=C_SDGE, alpha=0.9, label=f"RESOLVE SDGE (~{yr_res_lbl})"),
        mpatches.Patch(color=C_IID,  alpha=0.9, label=f"RESOLVE IID  (~{yr_res_lbl})"),
        mpatches.Patch(color=C_LDWP, alpha=0.9, label=f"RESOLVE LDWP (~{yr_res_lbl})"),
        mpatches.Patch(color=C_NCNC, alpha=0.9, label=f"RESOLVE NCNC (~{yr_res_lbl})"),
    ]
    if reeds_mean_twh is not None:
        patches.append(mpatches.Patch(color=C_REEDS, alpha=0.85,
                                      label=f"ReEDS IRA_low p8+p9+p10+p11 (~{reeds_yr_lbl})"
                                            " — error bars = weather-year range"))
    ax.legend(handles=patches, fontsize=7, ncol=3, loc="upper center")

    fig.tight_layout()
    out = FIGS / "fig_resolve_scope_decomposition.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved {out}")
    plt.close(fig)


def fig3_hourly_shape(resolve_hrly: pd.DataFrame, eia_ann_by_ba: pd.DataFrame,
                      reeds_hourly: pd.DataFrame | None = None) -> None:
    """
    Compare RESOLVE 2022 hourly shape vs EIA CISO 2022 hourly, with ReEDS overlay.

    Uses demand_mw_2024scaled (GROSS demand, before BTM solar subtraction) directly —
    no Customer_PV correction is applied here.  This is intentional: the level gap
    between RESOLVE (gross) and EIA-930 (net-of-BTM) illustrates the ~20-25 TWh BTM
    solar offset.  For net-load comparisons, see compare_substation_eia_iepr.py which
    subtracts RESOLVE's native Customer_PV profiles from demand_mw_2024scaled.

    reeds_hourly: output of _reeds_hourly_ca(), columns (time_index, month, hour,
    load_mw_mean).  Nearest available target year is used (see _reeds_hourly_ca).
    """
    yr = 2022

    # RESOLVE 2022 CAISO sum (scaled to 2024 targets for absolute comparison)
    res = resolve_hrly[resolve_hrly["utility"].isin(CAISO_UTILS)].copy()
    res = res[res["datetime_pst"].dt.year == yr]
    if res.empty:
        print("  WARNING: No RESOLVE 2022 data found, skipping hourly shape figure.")
        return
    res_sum = res.groupby("datetime_pst")["demand_mw_2024scaled"].sum().reset_index()
    res_sum.columns = ["datetime_pst", "demand_mw"]

    # EIA CISO 2022 hourly — convert UTC to fixed PST (UTC-8) to match RESOLVE timezone
    eia = pd.read_csv(EIA_OPS, usecols=["datetime_utc", "ba_code", "demand_mwh"],
                      parse_dates=["datetime_utc"])
    ciso22 = eia[(eia["ba_code"] == "CISO") & (eia["datetime_utc"].dt.year == yr)].copy()
    dt_pst = ciso22["datetime_utc"]
    if dt_pst.dt.tz is not None:
        dt_pst = dt_pst.dt.tz_localize(None)
    ciso22["datetime_pst"] = dt_pst - pd.Timedelta(hours=8)
    ciso22 = ciso22.rename(columns={"demand_mwh": "demand_mw"})

    res_vals  = res_sum["demand_mw"].dropna().sort_values().values
    ciso_vals = ciso22["demand_mw"].dropna().sort_values().values
    min_len   = min(len(res_vals), len(ciso_vals))
    res_vals  = res_vals[:min_len]
    ciso_vals = ciso_vals[:min_len]

    r_corr, _ = stats.pearsonr(res_vals, ciso_vals)
    diff_pct  = (res_vals.mean() - ciso_vals.mean()) / ciso_vals.mean() * 100

    # ReEDS sorted hourly values and monthly means (if available)
    reeds_ldc:  np.ndarray | None = None
    reeds_mon:  pd.Series | None  = None
    reeds_lbl   = ""
    if reeds_hourly is not None and not reeds_hourly.empty:
        rv = reeds_hourly["load_mw_mean"].dropna().sort_values().values
        reeds_ldc = rv
        rm = (reeds_hourly.groupby("month")["load_mw_mean"].mean() / 1000)
        reeds_mon = rm
        # Identify the target year from the data (stored via filter in _reeds_hourly_ca)
        reeds_lbl = "ReEDS IRA_low CA total (p8-p11, mean across 7 wx-yrs)"

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # Load duration curves
    ax = axes[0]
    ax.plot(np.arange(min_len) / min_len * 100, res_vals[::-1] / 1000,
            color=COLORS["resolve"], lw=2, label=f"RESOLVE {yr} PGE+SCE+SDGE (scaled to 2024 target)")
    ax.plot(np.arange(min_len) / min_len * 100, ciso_vals[::-1] / 1000,
            color=COLORS["eia_ciso"], lw=2, label=f"EIA CISO {yr} (measured)")
    if reeds_ldc is not None:
        n_r = len(reeds_ldc)
        ax.plot(np.arange(n_r) / n_r * 100, reeds_ldc[::-1] / 1000,
                color=COLORS["reeds"], lw=1.8, ls="--", label=reeds_lbl)
    ax.set_xlabel("% of hours (load duration curve)")
    ax.set_ylabel("Demand (GW)")
    ax.set_title(f"Load duration curves: RESOLVE vs EIA CISO ({yr})")
    ax.legend(fontsize=8.5)
    ax.grid(alpha=0.3)
    stats_txt = (f"RESOLVE mean: {res_vals.mean()/1000:.1f} GW\n"
                 f"EIA CISO mean: {ciso_vals.mean()/1000:.1f} GW\n"
                 f"Level difference: {diff_pct:+.1f}%\n"
                 f"Shape correlation (sorted ranks): r={r_corr:.4f}")
    if reeds_ldc is not None:
        stats_txt += f"\nReEDS mean: {reeds_ldc.mean()/1000:.1f} GW"
    ax.text(0.98, 0.02, stats_txt,
            transform=ax.transAxes, fontsize=8, ha="right", va="bottom",
            bbox=dict(boxstyle="round", fc="white", alpha=0.8))

    # Monthly average
    ax = axes[1]
    res_mon  = res_sum.copy()
    res_mon["month"] = res_mon["datetime_pst"].dt.month
    res_mon  = res_mon.groupby("month")["demand_mw"].mean() / 1000

    ciso22["month"] = ciso22["datetime_pst"].dt.month
    ciso_mon = ciso22.groupby("month")["demand_mw"].mean() / 1000

    mon_labels = ["Jan","Feb","Mar","Apr","May","Jun",
                  "Jul","Aug","Sep","Oct","Nov","Dec"]
    x = np.arange(1, 13)
    if reeds_mon is not None:
        w = 0.26
        ax.bar(x - w, res_mon.reindex(x).values,  width=w, label="RESOLVE (scaled to 2024)",
               color=COLORS["resolve"], alpha=0.8)
        ax.bar(x,     ciso_mon.reindex(x).values, width=w, label="EIA CISO",
               color=COLORS["eia_ciso"], alpha=0.8)
        ax.bar(x + w, reeds_mon.reindex(x).values, width=w, label=reeds_lbl,
               color=COLORS["reeds"], alpha=0.75)
    else:
        w = 0.38
        ax.bar(x - w/2, res_mon.reindex(x).values,  width=w, label="RESOLVE (scaled to 2024)",
               color=COLORS["resolve"], alpha=0.8)
        ax.bar(x + w/2, ciso_mon.reindex(x).values, width=w, label="EIA CISO",
               color=COLORS["eia_ciso"], alpha=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(mon_labels)
    ax.set_ylabel("Mean hourly demand (GW)")
    ax.set_title(f"Monthly mean demand: RESOLVE vs EIA CISO ({yr})")
    ax.legend(fontsize=8.5)
    ax.grid(alpha=0.3, axis="y")

    fig.suptitle(
        "RESOLVE hourly shape comparison vs EIA-930 CISO (+ ReEDS IRA_low CA total)\n"
        "RESOLVE 2024scaled = raw shape × (2024 annual target / shape-year annual sum)\n"
        "Level gap reflects gross load (RESOLVE) vs net-of-BTM-solar (EIA); "
        "ReEDS is projected total consumption",
        fontsize=9
    )
    fig.tight_layout()
    out = FIGS / "fig_resolve_hourly_shape.png"
    fig.savefig(out, dpi=150)
    print(f"Saved {out}")
    plt.close(fig)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Loading data ...")
    resolve      = _resolve_annual()
    iepr_net     = _iepr_net_annual()
    iepr_gross   = _iepr_consumption_annual()
    iepr_cons    = _iepr_baseline_consumption_annual()   # gross (hourly BASELINE_CONSUMPTION)
    iepr_mgd     = _iepr_managed_annual()                # net + overlays (MANAGED_NET_LOAD)
    iepr_btmpv   = _iepr_btm_pv_annual()
    eia_ba       = _eia_annual_by_ba()
    eia_piv      = _eia_pivot(eia_ba)
    cal_ann      = _pudl_cal_annual()
    cal_ann_eia  = _eia_cal_annual()
    resolve_hrly = _resolve_hourly()
    overlays     = _resolve_outputs_overlays()
    last_hist    = _iepr_last_hist()
    reeds_ann    = _reeds_annual()          # CA total (p8+p9+p10+p11)
    reeds_caiso  = _reeds_annual_caiso()    # CAISO only (p9+p10+p11)
    hist_ann     = _historic_annual()       # historic 2016-2023 actual (all regions)

    # ── Section 1: Annual level comparison ───────────────────────────────────
    print()
    print("=" * 70)
    print("SECTION 1 — Annual demand: RESOLVE vs IEPR vs EIA")
    print("=" * 70)

    # RESOLVE CAISO total
    res_caiso = (resolve[resolve["utility"].isin(CAISO_UTILS)]
                 .groupby("year")["energy_twh"].sum())
    res_all   = resolve.groupby("year")["energy_twh"].sum()

    print()
    print("  RESOLVE Baseline PGE+SCE+SDGE (gross annual forecast):")
    for yr in [2024, 2026, 2030, 2035, 2040, 2045]:
        print(f"    {yr}: {res_caiso.get(yr, float('nan')):.1f} TWh")

    print()
    print("  RESOLVE full CA scope (+ IID + LDWP + NCNC):")
    for yr in [2024, 2030, 2045]:
        print(f"    {yr}: {res_all.get(yr, float('nan')):.1f} TWh")

    print()
    print("  EIA annual TWh — selected BAs:")
    for yr in sorted(eia_piv["year"].unique()):
        if yr < 2019: continue
        row = eia_piv[eia_piv["year"] == yr].iloc[0]
        ciso = row.get("CISO", float("nan"))
        iid  = row.get("IID",  float("nan"))
        ldwp = row.get("LDWP", float("nan"))
        nevp = row.get("NEVP", float("nan"))
        pacw = row.get("PACW", float("nan"))
        ca8  = row.get("CA8",  float("nan"))
        inCA = row.get("INCA", float("nan"))
        print(f"    {yr}: CISO={ciso:.1f}  IID={iid:.1f}  LDWP={ldwp:.1f}  "
              f"NEVP={nevp:.1f}  PACW={pacw:.1f}  CA8={ca8:.1f}  in-CA={inCA:.1f}")

    if not cal_ann.empty:
        print()
        print("  PUDL CA5 sum (BANC+CISO+IID+LDWP+TIDC, geographic CA boundary):")
        for _, r in cal_ann.iterrows():
            print(f"    {int(r['year'])}: {r['twh']:.1f} TWh")

    # ── Section 2: Systematic difference decomposition ───────────────────────
    print()
    print("=" * 70)
    print("SECTION 2 — Systematic scope and definition differences")
    print("=" * 70)

    # Compare RESOLVE CAISO to EIA CISO for 2024 (EIA) / 2024 (RESOLVE)
    eia_2023 = eia_piv[eia_piv["year"] == 2023]
    eia_2024 = eia_piv[eia_piv["year"] == 2024]
    res_2024 = float(res_caiso.get(2024, float("nan")))
    eia_ciso_2024 = float(eia_2024["CISO"].iloc[0]) if not eia_2024.empty else float("nan")
    eia_ca8_2024  = float(eia_2024["CA8"].iloc[0])  if not eia_2024.empty else float("nan")
    eia_inCA_2024 = float(eia_2024["INCA"].iloc[0]) if not eia_2024.empty else float("nan")
    nevp_pacw_2024 = float(eia_2024["NEVP_PACW"].iloc[0]) if not eia_2024.empty else float("nan")

    print()
    print("  RESOLVE vs EIA CISO (same geographic scope, 2024):")
    print(f"    RESOLVE PGE+SCE+SDGE (gross):  {res_2024:.1f} TWh")
    print(f"    EIA CISO (net of BTM solar):   {eia_ciso_2024:.1f} TWh")
    if not np.isnan(res_2024) and not np.isnan(eia_ciso_2024):
        diff = res_2024 - eia_ciso_2024
        print(f"    Difference:                   {diff:+.1f} TWh ({diff/eia_ciso_2024*100:+.1f}%)")
        print(f"    Likely drivers:")
        print(f"      ~ BTM solar in CAISO territory removes ~20-25 TWh from EIA measurement")
        print(f"      ~ RESOLVE gross load includes losses captured differently from EIA")

    print()
    print("  EIA CA8 inflation from NEVP and PACW (mostly outside CA, 2024):")
    print(f"    NEVP + PACW total:   {nevp_pacw_2024:.1f} TWh (almost all is NOT California load)")
    print(f"    EIA CA8 total:       {eia_ca8_2024:.1f} TWh")
    print(f"    EIA in-CA BAs only:  {eia_inCA_2024:.1f} TWh  (CISO+IID+LDWP+BANC+TIDC)")
    if not np.isnan(eia_ca8_2024) and not np.isnan(eia_inCA_2024):
        inflation = eia_ca8_2024 - eia_inCA_2024
        print(f"    NEVP+PACW inflation: {inflation:.1f} TWh ({inflation/eia_inCA_2024*100:.1f}% of in-CA total)")
        print(f"    NOTE: NEVP (NV Energy) serves Clark County (Las Vegas) + N. Nevada.")
        print(f"          Only ~0.4% of NEVP load is in California (EIA Form 861, 2024).")
        print(f"          PACW serves OR/WA/ID/WY/UT + tiny far-N. CA slice.")
        print(f"          Only ~4% of PACW load is in California (EIA Form 861, 2024).")
        # Verified CA portions from EIA Form 861 (2024 actuals)
        nevp_ca_frac = 0.004   # 0.4% verified
        pacw_ca_frac = 0.04    # 4.0% verified
        nevp_ca = eia_2024["NEVP"].iloc[0] * nevp_ca_frac if not eia_2024.empty else 0
        pacw_ca = eia_2024["PACW"].iloc[0] * pacw_ca_frac if not eia_2024.empty else 0
        print(f"    Verified CA portions: NEVP×0.4% + PACW×4% = "
              f"{nevp_ca:.2f} + {pacw_ca:.2f} = {nevp_ca+pacw_ca:.2f} TWh actual CA load")
        print(f"    (~0.18 TWh NEVP + 0.85 TWh PACW = 1.03 TWh total)")
        true_ca_inflation = nevp_pacw_2024 - (nevp_ca + pacw_ca)
        print(f"    True out-of-CA inflation in CA8: {true_ca_inflation:.1f} TWh")

    if not cal_ann.empty:
        cal_latest = cal_ann[cal_ann["year"] == cal_ann["year"].max()]
        print()
        print(f"  EIA CAL region ({int(cal_latest['year'].iloc[0])}, geographic CA boundary):")
        print(f"    CAL total: {float(cal_latest['twh'].iloc[0]):.1f} TWh")
        print(f"    CAL vs RESOLVE CAISO gross: difference reflects BTM solar + "
              f"IID/LDWP/BANC/TIDC scope")

    # ── Section 3: IEPR vs RESOLVE forecast comparison ────────────────────────
    print()
    print("=" * 70)
    print("SECTION 3 — RESOLVE vs IEPR annual forecast comparison (2025-2035)")
    print("=" * 70)

    latest_v = iepr_net["vintage"].max()
    last_h   = last_hist.get(latest_v, 9999)
    iepr_proj = (iepr_net[(iepr_net["vintage"] == latest_v) & (iepr_net["year"] > last_h)]
                 .set_index("year")["twh"])
    iepr_gross_proj = (iepr_gross[(iepr_gross["vintage"] == latest_v) & (iepr_gross["year"] > last_h)]
                       .set_index("year")["twh_gross"])

    print(f"\n  IEPR v{latest_v} vs RESOLVE Baseline (PGE+SCE+SDGE):")
    print(f"  {'Year':<6} {'RESOLVE':>10} {'IEPR NET':>10} {'IEPR GROSS':>12} "
          f"{'R-IE_NET':>10} {'R-IE_GRS':>10}")
    for yr in [2025, 2026, 2028, 2030, 2035, 2040, 2045]:
        res = res_caiso.get(yr, float("nan"))
        ie_net  = iepr_proj.get(yr, float("nan"))
        ie_grs  = iepr_gross_proj.get(yr, float("nan"))
        d_net   = res - ie_net  if not np.isnan(res) and not np.isnan(ie_net)  else float("nan")
        d_grs   = res - ie_grs if not np.isnan(res) and not np.isnan(ie_grs) else float("nan")
        print(f"  {yr:<6} {res:>10.1f} {ie_net:>10.1f} {ie_grs:>12.1f} "
              f"{d_net:>+10.1f} {d_grs:>+10.1f}")

    print()
    print("  Key: R-IE_NET = RESOLVE minus IEPR net  (expected +20-30 TWh: BTM solar)")
    print("       R-IE_GRS = RESOLVE minus IEPR gross (expected small; both are gross load)")
    print("       If R-IE_GRS is large, RESOLVE and IEPR use different gross load baselines.")

    # ── Section 4: Hourly shape summary ──────────────────────────────────────
    print()
    print("=" * 70)
    print("SECTION 4 — Hourly shape summary (RESOLVE 2022 vs EIA CISO 2022)")
    print("=" * 70)

    yr = 2022
    res_yr = resolve_hrly[
        (resolve_hrly["utility"].isin(CAISO_UTILS)) &
        (resolve_hrly["datetime_pst"].dt.year == yr)
    ]
    if not res_yr.empty:
        res_sum = res_yr.groupby("datetime_pst")["demand_mw_2024scaled"].sum()
        print(f"  RESOLVE {yr} (scaled to 2024 targets): "
              f"mean={res_sum.mean()/1000:.1f} GW  "
              f"peak={res_sum.max()/1000:.1f} GW  "
              f"min={res_sum.min()/1000:.1f} GW")

    eia = pd.read_csv(EIA_OPS, usecols=["datetime_utc","ba_code","demand_mwh"],
                      parse_dates=["datetime_utc"])
    ciso22 = eia[(eia["ba_code"]=="CISO") & (eia["datetime_utc"].dt.year==yr)]["demand_mwh"]
    if not ciso22.empty:
        print(f"  EIA CISO {yr}: "
              f"mean={ciso22.mean()/1000:.1f} GW  "
              f"peak={ciso22.max()/1000:.1f} GW  "
              f"min={ciso22.min()/1000:.1f} GW")
    if not res_yr.empty and not ciso22.empty:
        ratio = res_sum.mean() / ciso22.mean()
        print(f"  RESOLVE/EIA level ratio: {ratio:.3f}  "
              f"(expected >1 due to gross vs net-of-BTM-solar)")

    # ── Section 5: RESOLVE Baseline + Overlays = IEPR reconstruction ─────────
    print()
    print("=" * 70)
    print("SECTION 5 — RESOLVE Baseline + Overlays ≈ IEPR (reconstruction check)")
    print("=" * 70)
    latest_v = iepr_cons["vintage"].max() if not iepr_cons.empty else None

    if latest_v is None:
        print("  WARNING: IEPR hourly data not found — skipping Section 5.")
    else:
        print(f"  Using IEPR vintage {latest_v}, PGE+SCE+SDGE, Local_Reliability")
        if RESOLVE_OUTPUTS is not None:
            print(f"  Using RESOLVE Outputs: {RESOLVE_OUTPUTS.name}")
        else:
            print("  WARNING: RESOLVE Outputs folder not found — overlay reconstruction skipped.")

        # --- 5a: RESOLVE Baseline vs IEPR BASELINE_CONSUMPTION ---
        print()
        print("  (a) RESOLVE Baseline vs IEPR BASELINE_CONSUMPTION (both should be gross):")
        print(f"  {'Year':<6} {'RESOLVE':>10} {'IEPR_CONS':>11} {'Diff':>8} {'Diff%':>7}")
        cons_v = iepr_cons[iepr_cons["vintage"] == latest_v].set_index("year")["twh"]
        for yr in [2025, 2026, 2028, 2030, 2035, 2040, 2045]:
            res = res_caiso.get(yr, float("nan"))
            con = cons_v.get(yr, float("nan"))
            d   = res - con if not (np.isnan(res) or np.isnan(con)) else float("nan")
            dp  = d / con * 100 if not np.isnan(d) and con != 0 else float("nan")
            print(f"  {yr:<6} {res:>10.1f} {con:>11.1f} {d:>+8.1f} {dp:>+7.1f}%")
        print("  Expected: small residuals only (RESOLVE target = IEPR gross - overlays + BTM_PV)")
        print("  If consistently ~+30 TWh, RESOLVE Baseline includes BTM_PV that IEPR_CONS does not.")

        # --- 5b: IEPR BASELINE_CONSUMPTION - BTM_PV vs IEPR BASELINE_NET_LOAD ---
        print()
        print("  (b) Cross-check: IEPR BASELINE_CONSUMPTION - BTM_PV ≈ IEPR BASELINE_NET_LOAD:")
        btmpv_v = iepr_btmpv[iepr_btmpv["vintage"] == latest_v].set_index("year")["twh"]
        net_v   = iepr_net[iepr_net["vintage"] == latest_v].set_index("year")["twh"]
        print(f"  {'Year':<6} {'CONS':>8} {'BTM_PV':>8} {'CONS-PV':>9} {'NET_LOAD':>10} {'Residual':>10}")
        for yr in [2025, 2026, 2030, 2035, 2040]:
            con  = cons_v.get(yr, float("nan"))
            pv   = btmpv_v.get(yr, float("nan"))
            net  = net_v.get(yr, float("nan"))
            cpv  = con - pv if not (np.isnan(con) or np.isnan(pv)) else float("nan")
            resid= cpv - net if not (np.isnan(cpv) or np.isnan(net)) else float("nan")
            print(f"  {yr:<6} {con:>8.1f} {pv:>8.1f} {cpv:>9.1f} {net:>10.1f} {resid:>+10.2f}")
        print("  Expected residual ≈ 0 (BTM_STORAGE terms; typically < 0.5 TWh)")

        # --- 5c: RESOLVE Baseline + overlays - BTM_PV vs IEPR MANAGED_NET_LOAD ---
        print()
        if overlays is not None and not overlays.empty:
            mgd_v = iepr_mgd[iepr_mgd["vintage"] == latest_v].set_index("year")["twh"]

            print("  (c) RESOLVE Baseline + overlays - BTM_PV ≈ IEPR MANAGED_NET_LOAD:")
            print()
            print("  Overlay annual totals from RESOLVE Outputs (PGE+SCE+SDGE, TWh):")
            print(f"  {'Component':<20} " + "  ".join(f"{y:>6}" for y in [2026,2028,2030,2035,2040,2045]))
            ov_sum = overlays.groupby(["component", "year"])["twh"].sum()
            for comp in _OVERLAY_COMPONENTS:
                row_vals = [ov_sum.get((comp, yr), float("nan")) for yr in [2026,2028,2030,2035,2040,2045]]
                row_str  = "  ".join(f"{v:>+6.1f}" if not np.isnan(v) else f"{'N/A':>6}" for v in row_vals)
                print(f"  {comp:<20} {row_str}")

            print()
            print("  Reconstruction: RESOLVE Baseline + sum(overlays) - IEPR BTM_PV vs IEPR MANAGED_NET_LOAD:")
            print(f"  {'Year':<6} {'RES_BASE':>9} {'Overlays':>9} {'BTM_PV':>8} "
                  f"{'Reconstr':>10} {'MANAGED':>9} {'Residual':>10}")
            for yr in [2026, 2028, 2030, 2035, 2040, 2045]:
                res     = res_caiso.get(yr, float("nan"))
                ov_tot  = float(ov_sum.xs(yr, level="year").sum()) if yr in ov_sum.index.get_level_values("year") else float("nan")
                pv      = btmpv_v.get(yr, float("nan"))
                mgd     = mgd_v.get(yr, float("nan"))
                reconst = res + ov_tot - pv if not any(np.isnan(x) for x in [res, ov_tot, pv]) else float("nan")
                resid   = reconst - mgd if not (np.isnan(reconst) or np.isnan(mgd)) else float("nan")
                print(f"  {yr:<6} {res:>9.1f} {ov_tot:>+9.1f} {pv:>8.1f} "
                      f"{reconst:>10.1f} {mgd:>9.1f} {resid:>+10.2f}")
            print()
            print("  Residual interpretation:")
            print("    ≈ 0        : RESOLVE Baseline + overlays reconstructs IEPR exactly (identity holds)")
            print("    Systematic : Scope mismatch (IID/LDWP/NCNC vs CAISO) or scenario difference")
            print("    Growing    : Divergence in RESOLVE vs IEPR demand growth assumptions")
        else:
            print("  (c) RESOLVE Outputs overlay data not available — skipping reconstruction.")
            print("      Place the unzipped RESOLVE run in data/raw/Raw RESOLVE Outputs/")

    # ── Figures ───────────────────────────────────────────────────────────────
    print()
    print("Generating figures ...")
    fig1_annual_comparison(resolve, iepr_net, iepr_gross, eia_piv, cal_ann,
                           iepr_cons, iepr_mgd, cal_ann_eia=cal_ann_eia,
                           reeds=reeds_ann, reeds_caiso=reeds_caiso,
                           hist=hist_ann)
    reeds_hourly = _reeds_hourly_ca(target_year=2022)
    fig2_scope_decomposition(eia_piv, resolve, cal_ann, cal_ann_eia=cal_ann_eia,
                             reeds=reeds_ann)
    fig3_hourly_shape(resolve_hrly, eia_ba, reeds_hourly=reeds_hourly)
    print(f"\nDone. Figures saved to {FIGS.relative_to(ROOT)}/")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
