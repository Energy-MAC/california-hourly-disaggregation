"""
compare_resolve_iepr_eia.py

Compares RESOLVE Baseline load projections against IEPR and EIA-930 demand.

RESOLVE (E3 / CPUC IRP) and IEPR both serve California IRP, but differ in
scope, load definition, and representation:

Load definition differences
---------------------------
  RESOLVE "profile_model_years" (raw shape)
      Gross load at the utility level, calibrated historical shapes for 2000-
      2022.  Before BTM solar/storage subtraction.  Higher than EIA/IEPR net.

  RESOLVE annual_energy_forecast (modeled target)
      The actual demand level RESOLVE optimizes to. Represents gross load
      (consumption + T&D losses) per utility territory.

  IEPR BASELINE_CONSUMPTION
      Total energy to serve load = gross load at the customer meter including
      T&D losses.  Comparable to RESOLVE annual_energy_forecast.

  IEPR BASELINE_NET_LOAD
      = BASELINE_CONSUMPTION - BTM_PV - BTM_STORAGE.
      Net of behind-the-meter solar and storage.  Comparable to EIA measured
      demand (which already nets out BTM generation).

  EIA-930 demand_mwh
      Measured electricity demand at balancing-authority level.  Net of BTM
      generation; includes T&D losses on the transmission side.

Geographic scope differences (CA8 vs CAISO vs RESOLVE)
-------------------------------------------------------
  RESOLVE CAISO zone: PGE + SCE + SDGE
  RESOLVE CA total:   PGE + SCE + SDGE + IID + LDWP + NCNC

  EIA CISO:    CAISO balancing authority (= PGE + SCE + SDGE territory)
               Most directly comparable to RESOLVE CAISO zone.

  EIA IID:     Imperial Irrigation District  (in RESOLVE as "IID")
  EIA LDWP:    Los Angeles Department of Water & Power  (in RESOLVE as "LDWP")
  EIA BANC:    Balancing Authority of Northern California  (NOT in RESOLVE —
               serves SMUD territory north of PGE; RESOLVE subsumes it in NCNC)
  EIA TIDC:    Turlock Irrigation District  (not in RESOLVE; tiny ~1 TWh/yr)
  EIA WALC:    Western Area Lower Colorado  (not in RESOLVE; mostly out of CA)

  EIA NEVP:    NV Energy (Nevada Power + Sierra Pacific Power).  Serves Nevada
               primarily, plus small portions of eastern CA.  ~80-85% of NEVP
               load is OUTSIDE California.  Inflates CA8 total by ~30-35 TWh.

  EIA PACW:    PacifiCorp West.  Serves OR, WA, ID, WY, UT, and a small CA
               service territory (former Pacific Power NorCal).  ~95%+ of PACW
               load is OUTSIDE California.  Inflates CA8 total by ~20 TWh.

  EIA CAL:     E IA's geographic "CAL" region boundary (available 2019+).
               Attempts to match California state boundary, excluding out-of-
               state NEVP/PACW load.  Best apples-to-apples comparison for
               total California electricity demand.

Outputs
-------
  Console: annual TWh tables, decomposition statistics
  data/figures/fig_resolve_vs_iepr_eia_annual.png
  data/figures/fig_resolve_scope_decomposition.png
  data/figures/fig_resolve_hourly_shape.png

Usage
-----
  python scripts/compare_resolve_iepr_eia.py
"""
from __future__ import annotations

import sys
from pathlib import Path

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
EIA_CAL      = PROC / "eia"      / "eia930_cal_region.csv"
IEPR_ANN     = PROC / "iepr"     / "iepr_baseline_annual.csv"
IEPR_HRLY    = PROC / "iepr"     / "iepr_hourly_forecast.csv"

CAISO_UTILS  = ["PGE", "SCE", "SDGE"]
RESOLVE_UTILS = ["PGE", "SCE", "SDGE", "IID", "LDWP", "NCNC"]

# EIA BAs present in California (inside CA or overlapping)
# NEVP and PACW are listed but extend substantially outside CA
EIA_CA8_BAS  = ["CISO", "IID", "LDWP", "BANC", "TIDC", "WALC", "NEVP", "PACW"]
EIA_INCA_BAS = ["CISO", "IID", "LDWP", "BANC", "TIDC", "WALC"]  # excludes NEVP, PACW
NEVP_PACW    = ["NEVP", "PACW"]


# ── Loaders ───────────────────────────────────────────────────────────────────

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


def _eia_cal_annual() -> pd.DataFrame:
    """EIA CAL region annual TWh (geographic CA boundary, available 2019+)."""
    if not EIA_CAL.exists():
        return pd.DataFrame(columns=["year", "twh"])
    df = pd.read_csv(EIA_CAL, usecols=["datetime_utc", "demand_mwh"],
                     parse_dates=["datetime_utc"])
    df["year"] = df["datetime_utc"].dt.year
    counts = df.groupby("year")["demand_mwh"].count()
    full   = counts[counts >= int(8760 * 0.95)].index
    ann    = (df[df["year"].isin(full)].groupby("year")["demand_mwh"].sum() / 1e6
              ).reset_index(name="twh")
    return ann


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
    return pd.read_csv(RESOLVE_HRLY, parse_dates=["datetime"])


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
}


def fig1_annual_comparison(
    resolve: pd.DataFrame,
    iepr_net: pd.DataFrame,
    iepr_gross: pd.DataFrame,
    eia_piv: pd.DataFrame,
    cal_ann: pd.DataFrame,
) -> None:
    fig, ax = plt.subplots(figsize=(12, 6))

    # RESOLVE Baseline (CAISO: PGE+SCE+SDGE)
    res_caiso = (resolve[resolve["utility"].isin(CAISO_UTILS)]
                 .groupby("year")["energy_twh"].sum().reset_index())
    ax.plot(res_caiso["year"], res_caiso["energy_twh"],
            color=COLORS["resolve"], lw=2.5, marker="D", ms=5,
            label="RESOLVE Baseline PGE+SCE+SDGE (gross, 2024 IRP)")

    # IEPR BASELINE_NET_LOAD — each vintage as solid line
    vintage_colors = {2023: "#1f77b4", 2024: "#ff7f0e", 2025: "#2ca02c"}
    last_hist = _iepr_last_hist()
    for vintage, grp in iepr_net.groupby("vintage"):
        last_h = last_hist.get(vintage, 9999)
        col    = vintage_colors.get(vintage, "gray")
        proj   = grp[grp["year"] > last_h]
        hist   = grp[grp["year"] <= last_h]
        ax.plot(hist["year"], hist["twh"], color=col, lw=1.2, ls="--", alpha=0.4)
        ax.plot(proj["year"], proj["twh"], color=col, lw=2,
                label=f"IEPR v{vintage} BASELINE_NET_LOAD PGE+SCE+SDGE")
        bnd = grp[grp["year"] == last_h]
        if not bnd.empty:
            ax.plot(bnd["year"].iloc[0], bnd["twh"].iloc[0], "o", color=col, ms=5)

    # IEPR Total_Consumption (gross)
    latest_vintage = iepr_gross["vintage"].max()
    ig = iepr_gross[iepr_gross["vintage"] == latest_vintage]
    ax.plot(ig["year"], ig["twh_gross"],
            color=COLORS["iepr_gross"], lw=1.5, ls=":",
            label=f"IEPR v{latest_vintage} Total_Consumption PGE+SCE+SDGE (gross)")

    # EIA CISO
    ciso = eia_piv[eia_piv["CISO"].notna()][["year", "CISO"]]
    ax.plot(ciso["year"], ciso["CISO"],
            color=COLORS["eia_ciso"], lw=2.5, marker="o", ms=5,
            label="EIA CISO BA (measured, net of BTM)")

    # EIA CAL region
    if not cal_ann.empty:
        ax.plot(cal_ann["year"], cal_ann["twh"],
                color=COLORS["eia_cal"], lw=1.8, marker="^", ms=4, ls="--",
                label="EIA CAL region (geographic CA boundary, 2019+)")

    # EIA CA8 total (all 8 BAs incl. NEVP/PACW)
    ca8 = eia_piv[eia_piv["CA8"].notna()][["year", "CA8"]]
    ax.plot(ca8["year"], ca8["CA8"],
            color=COLORS["eia_ca8"], lw=1.2, marker="s", ms=3, ls=":",
            label="EIA CA8 sum (8 BAs incl. NEVP+PACW ~50 TWh out-of-CA)")

    ax.set_xlabel("Year")
    ax.set_ylabel("Annual demand (TWh)")
    ax.set_xlim(2015, 2045)
    ax.set_title(
        "California electricity demand: RESOLVE vs IEPR vs EIA-930\n"
        "RESOLVE and IEPR Total_Consumption are gross (pre-BTM-solar); "
        "IEPR NET_LOAD and EIA are net of BTM solar"
    )
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    out = FIGS / "fig_resolve_vs_iepr_eia_annual.png"
    fig.savefig(out, dpi=150)
    print(f"Saved {out}")
    plt.close(fig)


def fig2_scope_decomposition(eia_piv: pd.DataFrame, resolve: pd.DataFrame,
                              cal_ann: pd.DataFrame) -> None:
    """
    Bar chart decomposing 2022 demand across all sources to illustrate scope
    and definition differences.  2022 is the last year where most sources
    have complete data.
    """
    yr = 2022
    row = eia_piv[eia_piv["year"] == yr]
    if row.empty:
        print(f"  WARNING: no EIA data for {yr}, skipping scope decomposition figure.")
        return

    row = row.iloc[0]

    # Build component bars
    components = []
    for ba in ["CISO", "IID", "LDWP", "BANC", "TIDC", "WALC"]:
        if ba in row and pd.notna(row[ba]):
            components.append((f"EIA {ba}", float(row[ba]), "#1f77b4"))
    for ba in ["NEVP", "PACW"]:
        if ba in row and pd.notna(row[ba]):
            components.append((f"EIA {ba}\n(mostly outside CA)", float(row[ba]), "#e74c3c"))

    if not cal_ann.empty:
        cal_row = cal_ann[cal_ann["year"] == yr]
        if not cal_row.empty:
            components.append(
                (f"EIA CAL\n(geographic CA)", float(cal_row["twh"].iloc[0]), "#9467bd")
            )

    # RESOLVE by utility
    res_row = resolve[resolve["year"] == yr + 2]  # closest RESOLVE year is 2024
    if res_row.empty:
        res_row = resolve[resolve["year"] == resolve["year"].min()]
    for util in RESOLVE_UTILS:
        val = res_row[res_row["utility"] == util]["energy_twh"]
        if not val.empty:
            color = "#2ca02c" if util in CAISO_UTILS else "#98df8a"
            components.append((f"RESOLVE\n{util}", float(val.iloc[0]), color))

    labels  = [c[0] for c in components]
    values  = [c[1] for c in components]
    colors  = [c[2] for c in components]

    fig, ax = plt.subplots(figsize=(14, 5))
    bars = ax.bar(labels, values, color=colors, edgecolor="white", linewidth=0.5)
    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                f"{val:.1f}", ha="center", va="bottom", fontsize=8)

    # Legend patches
    patches = [
        mpatches.Patch(color="#1f77b4", label="EIA BA (in-CA or overlapping)"),
        mpatches.Patch(color="#e74c3c", label="EIA BA (mostly outside CA — inflates CA8 total)"),
        mpatches.Patch(color="#9467bd", label="EIA CAL (geographic CA boundary)"),
        mpatches.Patch(color="#2ca02c", label="RESOLVE CAISO utils (PGE+SCE+SDGE, gross, ~2024 target)"),
        mpatches.Patch(color="#98df8a", label="RESOLVE non-CAISO CA utils (gross, ~2024 target)"),
    ]
    ax.legend(handles=patches, fontsize=8, loc="upper right")
    ax.set_ylabel("Annual demand (TWh)")
    ax.set_title(
        f"Demand scope decomposition: EIA-930 ({yr}) vs RESOLVE Baseline (~2024 forecast)\n"
        "Illustrates why EIA CA8 total exceeds RESOLVE and IEPR: "
        "NEVP (~80% Nevada) and PACW (~95% Oregon/other) add ~50 TWh of out-of-CA load"
    )
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    fig.tight_layout()
    out = FIGS / "fig_resolve_scope_decomposition.png"
    fig.savefig(out, dpi=150)
    print(f"Saved {out}")
    plt.close(fig)


def fig3_hourly_shape(resolve_hrly: pd.DataFrame, eia_ann_by_ba: pd.DataFrame) -> None:
    """
    Compare RESOLVE 2022 hourly shape vs EIA CISO 2022 hourly.
    Uses the RESOLVE demand_mw_2024scaled column so both are in comparable MW units.
    """
    yr = 2022

    # RESOLVE 2022 CAISO sum (scaled to 2024 targets for absolute comparison)
    res = resolve_hrly[resolve_hrly["utility"].isin(CAISO_UTILS)].copy()
    res = res[res["datetime"].dt.year == yr]
    if res.empty:
        print("  WARNING: No RESOLVE 2022 data found, skipping hourly shape figure.")
        return
    res_sum = res.groupby("datetime")["demand_mw_2024scaled"].sum().reset_index()
    res_sum.columns = ["datetime", "demand_mw"]

    # EIA CISO 2022 hourly
    eia = pd.read_csv(EIA_OPS, usecols=["datetime_utc", "ba_code", "demand_mwh"],
                      parse_dates=["datetime_utc"])
    ciso22 = eia[(eia["ba_code"] == "CISO") & (eia["datetime_utc"].dt.year == yr)].copy()
    ciso22 = ciso22.rename(columns={"datetime_utc": "datetime", "demand_mwh": "demand_mw"})

    # Align to common datetime (RESOLVE is naive, EIA is UTC — both hourly; we compare
    # by rank/distribution since timezone alignment is approximate)
    res_vals  = res_sum["demand_mw"].dropna().sort_values().values
    ciso_vals = ciso22["demand_mw"].dropna().sort_values().values
    min_len   = min(len(res_vals), len(ciso_vals))
    res_vals  = res_vals[:min_len]
    ciso_vals = ciso_vals[:min_len]

    r_corr, _ = stats.pearsonr(res_vals, ciso_vals)
    diff_pct  = (res_vals.mean() - ciso_vals.mean()) / ciso_vals.mean() * 100

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))

    # Load duration curves
    ax = axes[0]
    ax.plot(np.arange(min_len) / min_len * 100, res_vals[::-1] / 1000,
            color=COLORS["resolve"], lw=2, label=f"RESOLVE {yr} PGE+SCE+SDGE (scaled to 2024 target)")
    ax.plot(np.arange(min_len) / min_len * 100, ciso_vals[::-1] / 1000,
            color=COLORS["eia_ciso"], lw=2, label=f"EIA CISO {yr} (measured)")
    ax.set_xlabel("% of hours (load duration curve)")
    ax.set_ylabel("Demand (GW)")
    ax.set_title(f"Load duration curves: RESOLVE vs EIA CISO ({yr})")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)
    ax.text(0.98, 0.02,
            f"RESOLVE mean: {res_vals.mean()/1000:.1f} GW\n"
            f"EIA CISO mean: {ciso_vals.mean()/1000:.1f} GW\n"
            f"Level difference: {diff_pct:+.1f}%\n"
            f"Shape correlation (sorted ranks): r={r_corr:.4f}",
            transform=ax.transAxes, fontsize=8, ha="right", va="bottom",
            bbox=dict(boxstyle="round", fc="white", alpha=0.8))

    # Monthly average
    ax = axes[1]
    res_mon  = res_sum.copy()
    res_mon["month"] = res_mon["datetime"].dt.month
    res_mon  = res_mon.groupby("month")["demand_mw"].mean() / 1000

    ciso22["month"] = ciso22["datetime"].dt.month
    ciso_mon = ciso22.groupby("month")["demand_mw"].mean() / 1000

    mon_labels = ["Jan","Feb","Mar","Apr","May","Jun",
                  "Jul","Aug","Sep","Oct","Nov","Dec"]
    x = np.arange(1, 13)
    w = 0.38
    ax.bar(x - w/2, res_mon.reindex(x).values,  width=w, label="RESOLVE (scaled to 2024)",
           color=COLORS["resolve"], alpha=0.8)
    ax.bar(x + w/2, ciso_mon.reindex(x).values, width=w, label="EIA CISO",
           color=COLORS["eia_ciso"], alpha=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(mon_labels)
    ax.set_ylabel("Mean hourly demand (GW)")
    ax.set_title(f"Monthly mean demand: RESOLVE vs EIA CISO ({yr})")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3, axis="y")

    fig.suptitle(
        "RESOLVE hourly shape comparison vs EIA-930 CISO\n"
        "RESOLVE 2024scaled = raw shape × (2024 annual target / shape-year annual sum)\n"
        "Level gap reflects gross load (RESOLVE) vs net-of-BTM-solar (EIA)",
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
    resolve  = _resolve_annual()
    iepr_net = _iepr_net_annual()
    iepr_gross = _iepr_consumption_annual()
    eia_ba   = _eia_annual_by_ba()
    eia_piv  = _eia_pivot(eia_ba)
    cal_ann  = _eia_cal_annual()
    resolve_hrly = _resolve_hourly()
    last_hist    = _iepr_last_hist()

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
        print("  EIA CAL region (geographic CA boundary):")
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
    print(f"    NEVP + PACW total:   {nevp_pacw_2024:.1f} TWh (most is NOT California load)")
    print(f"    EIA CA8 total:       {eia_ca8_2024:.1f} TWh")
    print(f"    EIA in-CA BAs only:  {eia_inCA_2024:.1f} TWh  (CISO+IID+LDWP+BANC+TIDC+WALC)")
    if not np.isnan(eia_ca8_2024) and not np.isnan(eia_inCA_2024):
        inflation = eia_ca8_2024 - eia_inCA_2024
        print(f"    NEVP+PACW inflation: {inflation:.1f} TWh ({inflation/eia_inCA_2024*100:.1f}% of in-CA total)")
        print(f"    NOTE: NEVP serves Nevada (~80-85% of NEVP load is outside CA)")
        print(f"          PACW serves OR/WA/ID/WY/UT/CA (~95%+ of PACW load is outside CA)")
        # Estimated CA portions: NEVP ~15% CA, PACW ~5% CA
        nevp_ca = eia_2024["NEVP"].iloc[0] * 0.15 if not eia_2024.empty else 0
        pacw_ca = eia_2024["PACW"].iloc[0] * 0.05 if not eia_2024.empty else 0
        print(f"    Est. CA portions:    NEVP~15% + PACW~5% = {nevp_ca:.1f} + {pacw_ca:.1f} = "
              f"{nevp_ca+pacw_ca:.1f} TWh actual CA load in NEVP+PACW")

    if not cal_ann.empty:
        cal_latest = cal_ann[cal_ann["year"] == cal_ann["year"].max()]
        print()
        print(f"  EIA CAL region ({int(cal_latest['year'].iloc[0])}, geographic CA boundary):")
        print(f"    CAL total: {float(cal_latest['twh'].iloc[0]):.1f} TWh")
        print(f"    CAL vs RESOLVE CAISO gross: difference reflects BTM solar + "
              f"IID/LDWP/BANC/TIDC/WALC scope")

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
        (resolve_hrly["datetime"].dt.year == yr)
    ]
    if not res_yr.empty:
        res_sum = res_yr.groupby("datetime")["demand_mw_2024scaled"].sum()
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

    # ── Figures ───────────────────────────────────────────────────────────────
    print()
    print("Generating figures ...")
    fig1_annual_comparison(resolve, iepr_net, iepr_gross, eia_piv, cal_ann)
    fig2_scope_decomposition(eia_piv, resolve, cal_ann)
    fig3_hourly_shape(resolve_hrly, eia_ba)
    print(f"\nDone. Figures saved to {FIGS.relative_to(ROOT)}/")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
