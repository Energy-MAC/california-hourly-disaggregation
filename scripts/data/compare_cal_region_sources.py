"""
Compare the two EIA CAL region demand series:

  EIA API  : eia930_cal_region_EIA.csv  — direct scrape from EIA open-data API
             (geographic CA boundary, available 2019+)
  PUDL CA5 : eia930_cal_region_PUDL.csv — sum of BANC+CISO+IID+LDWP+TIDC
             from the PUDL nightly parquet build

Background
----------
EIA defines the CAL region as exactly the sum of the five purely-California BAs
(BANC, CISO, IID, LDWP, TIDC).  In principle the two series should be identical.
In practice the EIA API scrape has known data-quality issues: some hours show
dramatic spikes or drops not reflected in the individual BA data that PUDL uses.
This script quantifies how large and how frequent those discrepancies are.

Outputs (console)
-----------------
  - Matched-hour count and coverage
  - Distribution of hourly differences (|diff| buckets)
  - Annual TWh totals for both series
  - Per-year worst deviations (table of >= 10 largest |diff| hours per year)

Figures saved to data/figures/
  fig_cal_sources_diff_ts.png     — hourly difference time series, coloured by year
  fig_cal_sources_diff_hist.png   — histogram of (EIA - PUDL) differences
  fig_cal_sources_annual.png      — annual TWh: EIA API vs PUDL CA5 sum

Usage
-----
  python scripts/data/compare_cal_region_sources.py
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PROC = ROOT / "data" / "processed" / "eia"
FIGS = ROOT / "data" / "figures"
FIGS.mkdir(parents=True, exist_ok=True)

EIA_FILE  = PROC / "eia930_cal_region_EIA.csv"
PUDL_FILE = PROC / "eia930_cal_region_PUDL.csv"

C_EIA  = "#e74c3c"   # red — EIA API scrape
C_PUDL = "#1f77b4"   # blue — PUDL CA5 sum


# ── Loaders ───────────────────────────────────────────────────────────────────

def _load_eia() -> pd.DataFrame:
    df = pd.read_csv(EIA_FILE, usecols=["datetime_utc", "demand_mwh"],
                     parse_dates=["datetime_utc"])
    if df["datetime_utc"].dt.tz is not None:
        df["datetime_utc"] = df["datetime_utc"].dt.tz_localize(None)
    df = df.dropna(subset=["demand_mwh"]).rename(columns={"demand_mwh": "eia_mwh"})
    return df.sort_values("datetime_utc").reset_index(drop=True)


def _load_pudl() -> pd.DataFrame:
    df = pd.read_csv(PUDL_FILE, usecols=["datetime_utc", "demand_mwh"],
                     parse_dates=["datetime_utc"])
    if df["datetime_utc"].dt.tz is not None:
        df["datetime_utc"] = df["datetime_utc"].dt.tz_localize(None)
    df = df.dropna(subset=["demand_mwh"]).rename(columns={"demand_mwh": "pudl_mwh"})
    return df.sort_values("datetime_utc").reset_index(drop=True)


# ── Analysis ──────────────────────────────────────────────────────────────────

def _diff_stats(m: pd.DataFrame) -> None:
    diff = m["diff"]
    print(f"\n  Matched hours     : {len(m):,}")
    print(f"  Mean diff (EIA-PUDL): {diff.mean():+.2f} MWh")
    print(f"  Std dev           : {diff.std():.2f} MWh")
    print(f"  Median |diff|     : {diff.abs().median():.2f} MWh")
    print()
    thresholds = [1, 10, 100, 500, 1000, 5000]
    print(f"  {'|diff| threshold':>20}  {'hours above':>12}  {'%':>7}")
    print(f"  {'--------------------':>20}  {'----------':>12}  {'-------':>7}")
    for t in thresholds:
        n   = int((diff.abs() > t).sum())
        pct = n / len(m) * 100
        print(f"  {f'> {t:,} MWh':>20}  {n:>12,}  {pct:>7.2f}%")


def _annual_totals(m: pd.DataFrame) -> pd.DataFrame:
    m = m.copy()
    m["year"] = m["datetime_utc"].dt.year
    counts = m.groupby("year").size().rename("n_hours")
    sums = m.groupby("year")[["eia_mwh", "pudl_mwh"]].sum()
    ann = sums.join(counts).reset_index()
    ann["eia_twh"]  = ann["eia_mwh"]  / 1_000_000
    ann["pudl_twh"] = ann["pudl_mwh"] / 1_000_000
    ann["delta_twh"] = ann["eia_twh"] - ann["pudl_twh"]
    # Only include years with >= 95% of expected hours
    ann = ann[ann["n_hours"] >= int(8760 * 0.95)].copy()
    return ann[["year", "eia_twh", "pudl_twh", "delta_twh", "n_hours"]]


def _worst_per_year(m: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    m = m.copy()
    m["year"] = m["datetime_utc"].dt.year
    m["abs_diff"] = m["diff"].abs()
    rows = []
    for yr, grp in m.groupby("year"):
        worst = grp.nlargest(top_n, "abs_diff")[
            ["datetime_utc", "eia_mwh", "pudl_mwh", "diff"]
        ]
        worst.insert(0, "year", yr)
        rows.append(worst)
    return pd.concat(rows, ignore_index=True)


# ── Figures ───────────────────────────────────────────────────────────────────

def _fig_diff_ts(m: pd.DataFrame) -> None:
    """Hourly |diff| time series coloured by year."""
    m = m.copy()
    m["year"] = m["datetime_utc"].dt.year
    years = sorted(m["year"].unique())
    cmap  = plt.cm.get_cmap("tab10", len(years))
    yr_color = {yr: cmap(i) for i, yr in enumerate(years)}

    fig, ax = plt.subplots(figsize=(14, 4))
    for yr, grp in m.groupby("year"):
        ax.scatter(grp["datetime_utc"], grp["diff"].abs(),
                   s=0.3, alpha=0.4, color=yr_color[yr], rasterized=True)

    handles = [mpatches.Patch(color=yr_color[yr], label=str(yr)) for yr in years]
    ax.legend(handles=handles, fontsize=7, ncol=len(years), loc="upper left",
              title="Year", title_fontsize=7)
    ax.set_xlabel("UTC datetime")
    ax.set_ylabel("|EIA - PUDL| (MWh)")
    ax.set_title("CAL region demand: |EIA API - PUDL CA5 sum| per hour\n"
                 "Near-zero = data quality agreement; large spikes = EIA API anomalies")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.set_ylim(bottom=-1000)
    fig.tight_layout()
    out = FIGS / "fig_cal_sources_diff_ts.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {out.relative_to(ROOT)}")


def _fig_diff_hist(m: pd.DataFrame) -> None:
    """Histogram of (EIA - PUDL) differences, clipped for readability."""
    diff = m["diff"]
    clip_val = 5_000

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4))

    # Full range (log scale)
    ax1.hist(diff, bins=200, color=C_EIA, alpha=0.7, edgecolor="none")
    ax1.set_yscale("log")
    ax1.set_xlabel("EIA - PUDL (MWh)")
    ax1.set_ylabel("Hours (log scale)")
    ax1.set_title("Full distribution of hourly differences")
    ax1.axvline(0, color="black", lw=0.8)

    # Zoomed: |diff| < clip_val
    zoomed = diff[diff.abs() < clip_val]
    ax2.hist(zoomed, bins=200, color=C_PUDL, alpha=0.7, edgecolor="none")
    ax2.set_xlabel("EIA - PUDL (MWh)")
    ax2.set_ylabel("Hours")
    ax2.set_title(f"Zoomed: |diff| < {clip_val:,} MWh  ({len(zoomed)/len(diff)*100:.1f}% of hours)")
    # ax2.axvline(0, color="black", lw=0.8)

    fig.suptitle("CAL region demand: EIA API vs PUDL CA5 sum — hourly differences",
                 fontsize=11)
    fig.tight_layout()
    out = FIGS / "fig_cal_sources_diff_hist.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {out.relative_to(ROOT)}")


def _fig_annual(ann: pd.DataFrame) -> None:
    """Annual TWh: EIA API vs PUDL CA5 sum."""
    fig, ax = plt.subplots(figsize=(9, 4))

    ax.plot(ann["year"], ann["eia_twh"], color=C_EIA,
            lw=2, marker="o", ms=5, label="EIA API CAL region")
    ax.plot(ann["year"], ann["pudl_twh"], color=C_PUDL,
            lw=2, marker="s", ms=5, label="PUDL CA5 sum (BANC+CISO+IID+LDWP+TIDC)")

    for _, row in ann.iterrows():
        if abs(row["delta_twh"]) > 0.5:
            ax.annotate(f"{row['delta_twh']:+.1f}",
                        xy=(row["year"], max(row["eia_twh"], row["pudl_twh"])),
                        xytext=(0, 6), textcoords="offset points",
                        ha="center", fontsize=7, color="gray")

    ax.set_xlabel("Year")
    ax.set_ylabel("Annual demand (TWh)")
    ax.set_title("CAL region annual demand: EIA API vs PUDL CA5 sum\n"
                 "Annotation = EIA-PUDL (TWh); non-zero values indicate EIA API data quality issues")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out = FIGS / "fig_cal_sources_annual.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {out.relative_to(ROOT)}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Loading EIA API CAL region ...")
    eia = _load_eia()
    print(f"  {len(eia):,} rows  {eia['datetime_utc'].min()} -> {eia['datetime_utc'].max()}")

    print("Loading PUDL CA5 sum ...")
    pudl = _load_pudl()
    print(f"  {len(pudl):,} rows  {pudl['datetime_utc'].min()} -> {pudl['datetime_utc'].max()}")

    # Match on UTC timestamp
    m = eia.merge(pudl, on="datetime_utc", how="inner")
    m["diff"] = m["eia_mwh"] - m["pudl_mwh"]
    print(f"\n  Matched on UTC timestamp: {len(m):,} hours")

    # ── Hourly difference statistics ─────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  HOURLY DIFFERENCE STATISTICS (EIA - PUDL)")
    print("=" * 60)
    _diff_stats(m)

    # ── Annual totals ─────────────────────────────────────────────────────────
    ann = _annual_totals(m)
    print("\n" + "=" * 60)
    print("  ANNUAL TOTALS (complete years only, >= 95% hours)")
    print("=" * 60)
    print(f"\n  {'Year':>6}  {'EIA TWh':>10}  {'PUDL TWh':>10}  "
          f"{'Delta TWh':>10}  {'Delta %':>8}  {'Hours':>8}")
    print(f"  {'------':>6}  {'----------':>10}  {'----------':>10}  "
          f"{'----------':>10}  {'--------':>8}  {'--------':>8}")
    for _, row in ann.iterrows():
        pct = row["delta_twh"] / row["pudl_twh"] * 100
        print(f"  {int(row['year']):>6}  {row['eia_twh']:>10,.1f}  {row['pudl_twh']:>10,.1f}  "
              f"  {row['delta_twh']:>+9.2f}  {pct:>+7.3f}%  {int(row['n_hours']):>8,}")

    # ── Worst deviations per year ─────────────────────────────────────────────
    worst = _worst_per_year(m, top_n=10)
    print("\n" + "=" * 60)
    print("  TOP 10 WORST DEVIATIONS PER YEAR (|EIA - PUDL|)")
    print("=" * 60)
    print(f"\n  {'Year':>6}  {'UTC datetime':>22}  {'EIA MWh':>10}  "
          f"{'PUDL MWh':>10}  {'Diff MWh':>10}")
    print(f"  {'------':>6}  {'----------------------':>22}  {'----------':>10}  "
          f"{'----------':>10}  {'----------':>10}")
    for yr, grp in worst.groupby("year"):
        for _, row in grp.iterrows():
            print(f"  {int(yr):>6}  {str(row['datetime_utc']):>22}  "
                  f"{row['eia_mwh']:>10,.0f}  {row['pudl_mwh']:>10,.0f}  "
                  f"{row['diff']:>+10,.0f}")
        print()

    # ── Figures ───────────────────────────────────────────────────────────────
    print("Saving figures ...")
    _fig_diff_ts(m)
    _fig_diff_hist(m)
    _fig_annual(ann)

    print("\nDone.")


if __name__ == "__main__":
    main()
