"""
rank_substations.py

Rank all substations by their 10th-percentile (min_load), 90th-percentile
(max_load), and average load at four temporal aggregation levels, then measure
how much substation ordering changes across those levels.

This is a shared prerequisite for all load projection approaches — the rankings
are used to construct participation weights in Approach 1.  Run this script
once (or whenever the substation profiles are updated) before running any
disaggregation script.

Aggregation levels
------------------
  Annual      (1 ranking per percentile)    mean across all 288 (month, hour_pst) cells
  Monthly     (12 rankings per percentile)  mean across 24 hours within each month
  Hourly      (24 rankings per percentile)  mean across 12 months for each hour
  Month-hour  (288 rankings per percentile) raw value at each (month, hour_pst) cell

For each level the same procedure is applied to min_load, max_load, and avg_load.
Rankings are descending (rank 1 = highest load).

Comparison
----------
Spearman rank correlation of every monthly / hourly / month-hour ordering vs.
the annual max_load ordering.  This quantifies how much the choice of temporal
aggregation changes which substations appear "most important".

Key results (first run, 1,341 unique substation names):
  Monthly vs annual max_load:    r = 0.987–0.994  (very stable)
  Hourly vs annual max_load:     r = 0.991–0.997
  Month-hour vs annual max_load: r = 0.934–0.985

Data note
---------
This script deduplicates by substation_name only (not by utility).  Ten
substation names (e.g. ALPINE, BARRETT) appear in both PGE and SDGE data and
represent genuinely distinct substations in different counties — here their
profiles are averaged together, which is an acceptable approximation for
exploratory rank stability analysis.  The disaggregation scripts treat them
correctly as separate substations via (substation_name, utility) joins.

10 PGE substations also have duplicate (substation_name, month, hour_pst) rows
from scraper overlap (no year stamp).  These are resolved by taking the cell
mean before ranking.

Input
-----
  data/processed/substations/substation_load_profiles_clean.csv

Outputs (all in data/processed/load_projection/rankings/)
-------
  substation_annual_ranks.csv
      One row per unique substation name; annual mean load and rank for each
      of min_load, max_load, and avg_load.

  substation_monthly_ranks.csv
      Long format: (substation_name, month) × load + rank for each percentile.

  substation_hourly_ranks.csv
      Long format: (substation_name, hour_pst) × load + rank.

  substation_monthhour_ranks.csv
      Long format: (substation_name, month, hour_pst) × load + rank — 288 rows
      per substation.

  rank_correlations.csv
      Spearman r vs the annual max_load ranking, for every temporal period and
      every percentile (min_load, max_load, avg_load).

  data/figures/load_projection/rank_correlation_heatmap.png
      Four-panel figure: monthly line, hourly line, and two 12×24 heatmaps
      (min_load and max_load) showing Spearman r at each month-hour cell.

Usage
-----
  python scripts/load_projection/rank_substations.py
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT     = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "data" / "processed" / "substations" / "substation_load_profiles_clean.csv"
OUT_DIR  = ROOT / "data" / "processed" / "load_projection" / "rankings"
FIG_DIR  = ROOT / "data" / "figures" / "load_projection"
OUT_DIR.mkdir(parents=True, exist_ok=True)
FIG_DIR.mkdir(parents=True, exist_ok=True)

LOAD_COLS = ["min_load", "max_load", "avg_load"]
LABELS    = {
    "min_load": "10th pct (min)",
    "max_load": "90th pct (max)",
    "avg_load": "avg (min+max)/2",
}


# ── Data loading ──────────────────────────────────────────────────────────────

def load_profiles() -> pd.DataFrame:
    """
    Load clean profiles and resolve the 10 PGE substations with duplicate
    (substation_name, month, hour_pst) rows by taking the cell mean.
    """
    df = pd.read_csv(PROFILES, usecols=["substation_name", "month", "hour_pst",
                                         "min_load", "max_load"])
    df = (
        df.groupby(["substation_name", "month", "hour_pst"], sort=False)
        [["min_load", "max_load"]].mean()
        .reset_index()
    )
    df["avg_load"] = (df["min_load"] + df["max_load"]) / 2
    return df


# ── Ranking helpers ───────────────────────────────────────────────────────────

def _rank_desc(series: pd.Series) -> pd.Series:
    """Rank descending: rank 1 = highest value. NaN values are left unranked (NaN)."""
    return series.rank(ascending=False, method="min", na_option="keep")


# ── Annual ─────────────────────────────────────────────────────────────────────

def annual_ranks(df: pd.DataFrame) -> pd.DataFrame:
    """Mean across all 288 (month, hour_pst) cells → single rank per substation."""
    agg = df.groupby("substation_name")[LOAD_COLS].mean()
    out = agg.copy()
    for col in LOAD_COLS:
        out[f"rank_{col}"] = _rank_desc(agg[col])
    return out.reset_index().rename(columns={c: f"mean_{c}" for c in LOAD_COLS})


# ── Monthly ────────────────────────────────────────────────────────────────────

def monthly_ranks(df: pd.DataFrame) -> pd.DataFrame:
    """
    Mean across 24 hours within each month → 12 rankings.
    Returns long-format DataFrame: (substation_name, month, mean_min_load,
    mean_max_load, rank_min_load, rank_max_load).
    """
    agg = df.groupby(["substation_name", "month"])[LOAD_COLS].mean().reset_index()
    rows = []
    for month, grp in agg.groupby("month"):
        grp = grp.copy()
        for col in LOAD_COLS:
            grp[f"rank_{col}"] = _rank_desc(grp[col])
        rows.append(grp)
    out = pd.concat(rows, ignore_index=True)
    return out.rename(columns={c: f"mean_{c}" for c in LOAD_COLS})


# ── Hourly ────────────────────────────────────────────────────────────────────

def hourly_ranks(df: pd.DataFrame) -> pd.DataFrame:
    """
    Mean across 12 months for each hour → 24 rankings.
    This is the "average monthly profile" comparison: does knowing the
    typical load shape across hours change which substations rank highest?
    """
    agg = df.groupby(["substation_name", "hour_pst"])[LOAD_COLS].mean().reset_index()
    rows = []
    for hour, grp in agg.groupby("hour_pst"):
        grp = grp.copy()
        for col in LOAD_COLS:
            grp[f"rank_{col}"] = _rank_desc(grp[col])
        rows.append(grp)
    out = pd.concat(rows, ignore_index=True)
    return out.rename(columns={c: f"mean_{c}" for c in LOAD_COLS})


# ── Month-hour ─────────────────────────────────────────────────────────────────

def monthhour_ranks(df: pd.DataFrame) -> pd.DataFrame:
    """
    Raw value at each (month, hour_pst) cell → 288 rankings.
    Returns long-format DataFrame with rank columns.
    """
    rows = []
    for (month, hour), grp in df.groupby(["month", "hour_pst"]):
        grp = grp.copy()
        for col in LOAD_COLS:
            grp[f"rank_{col}"] = _rank_desc(grp[col])
        rows.append(grp)
    out = pd.concat(rows, ignore_index=True)
    return out


# ── Rank correlations ─────────────────────────────────────────────────────────

def compute_correlations(
    annual: pd.DataFrame,
    monthly: pd.DataFrame,
    hourly: pd.DataFrame,
    monthhour: pd.DataFrame,
) -> pd.DataFrame:
    """
    Spearman rank correlation of each monthly / hourly / month-hour ordering
    vs. the max_load ANNUAL ordering (single reference for all comparisons).

    Includes min_load, max_load, and avg_load rankings at all temporal levels
    (annual, monthly, hourly, month-hour) so the effect of both percentile choice
    and temporal resolution can be read off a common scale.

    Returns a DataFrame with columns:
      level, period_label, month, hour_pst, percentile, spearman_r
    """
    # Single fixed reference: 90th-pct (max_load) annual ranking
    ann_idx = annual.set_index("substation_name")
    ref_annual = ann_idx["rank_max_load"]

    records = []

    for col in LOAD_COLS:
        rank_col = f"rank_{col}"

        # Annual: how similar is this percentile's annual rank to max_load annual?
        r, _ = spearmanr(
            ref_annual.values,
            ann_idx[rank_col].reindex(ref_annual.index).values,
            nan_policy="omit",
        )
        records.append({
            "level": "annual", "period_label": "annual",
            "month": np.nan, "hour_pst": np.nan, "percentile": col, "spearman_r": r,
        })

        # Monthly
        for month, grp in monthly.groupby("month"):
            ref = ref_annual.reindex(grp["substation_name"].values)
            r, _ = spearmanr(ref.values, grp[rank_col].values, nan_policy="omit")
            records.append({
                "level": "monthly", "period_label": f"month_{month:02d}",
                "month": month, "hour_pst": np.nan, "percentile": col, "spearman_r": r,
            })

        # Hourly
        for hour, grp in hourly.groupby("hour_pst"):
            ref = ref_annual.reindex(grp["substation_name"].values)
            r, _ = spearmanr(ref.values, grp[rank_col].values, nan_policy="omit")
            records.append({
                "level": "hourly", "period_label": f"hour_{hour:02d}",
                "month": np.nan, "hour_pst": hour, "percentile": col, "spearman_r": r,
            })

        # Month-hour
        for (month, hour), grp in monthhour.groupby(["month", "hour_pst"]):
            ref = ref_annual.reindex(grp["substation_name"].values)
            r, _ = spearmanr(ref.values, grp[rank_col].values, nan_policy="omit")
            records.append({
                "level": "month_hour", "period_label": f"m{month:02d}_h{hour:02d}",
                "month": month, "hour_pst": hour, "percentile": col, "spearman_r": r,
            })

    return pd.DataFrame(records)


# ── Figure ────────────────────────────────────────────────────────────────────

def plot_correlations(corr: pd.DataFrame, out_path: Path) -> None:
    """
    Four-panel figure showing Spearman r vs max_load annual ranking.
    Line panels (monthly, hourly) include min, max, and avg.
    Heatmap panels (month-hour) show min and max only (avg omitted per design).
    """
    fig = plt.figure(figsize=(18, 12))
    gs  = fig.add_gridspec(2, 2, hspace=0.45, wspace=0.35)
    ax_monthly = fig.add_subplot(gs[0, 0])
    ax_hourly  = fig.add_subplot(gs[0, 1])
    ax_mh_min  = fig.add_subplot(gs[1, 0])
    ax_mh_max  = fig.add_subplot(gs[1, 1])

    colors    = {"min_load": "#1f77b4", "max_load": "#d62728", "avg_load": "#2ca02c"}
    markers   = {"min_load": "o",       "max_load": "s",       "avg_load": "^"}
    ref_label = "90th pct annual (max_load)"

    # ── Monthly panel ──────────────────────────────────────────────────────────
    monthly = corr[corr["level"] == "monthly"]
    for col in LOAD_COLS:
        sub = monthly[monthly["percentile"] == col].sort_values("month")
        ax_monthly.plot(sub["month"], sub["spearman_r"],
                        marker=markers[col], label=LABELS[col], color=colors[col])
    ax_monthly.set_xlabel("Month")
    ax_monthly.set_ylabel(f"Spearman r vs {ref_label}")
    ax_monthly.set_title("Monthly ordering vs max_load annual")
    ax_monthly.set_xticks(range(1, 13))
    ax_monthly.set_ylim(0, 1.05)
    ax_monthly.legend(fontsize=9)
    ax_monthly.grid(True, alpha=0.3)
    ax_monthly.axhline(1.0, color="gray", linewidth=0.5, linestyle="--")

    # ── Hourly panel ───────────────────────────────────────────────────────────
    hourly = corr[corr["level"] == "hourly"]
    for col in LOAD_COLS:
        sub = hourly[hourly["percentile"] == col].sort_values("hour_pst")
        ax_hourly.plot(sub["hour_pst"], sub["spearman_r"],
                       marker=markers[col], label=LABELS[col], color=colors[col])
    ax_hourly.set_xlabel("Hour (PST, 0=midnight)")
    ax_hourly.set_ylabel(f"Spearman r vs {ref_label}")
    ax_hourly.set_title("Hourly (month-avg) ordering vs max_load annual")
    ax_hourly.set_xticks(range(0, 24, 3))
    ax_hourly.set_ylim(0, 1.05)
    ax_hourly.legend(fontsize=9)
    ax_hourly.grid(True, alpha=0.3)
    ax_hourly.axhline(1.0, color="gray", linewidth=0.5, linestyle="--")

    # ── Month-hour heatmaps (min and max only; avg omitted) ───────────────────
    mh = corr[corr["level"] == "month_hour"]
    vmin = mh["spearman_r"].min()
    vmin = max(0.0, np.floor(vmin * 20) / 20)   # round down to nearest 0.05
    for ax, col, title in [
        (ax_mh_min, "min_load", "Month-hour vs max_load annual\n(10th pct / min_load)"),
        (ax_mh_max, "max_load", "Month-hour vs max_load annual\n(90th pct / max_load)"),
    ]:
        sub  = mh[mh["percentile"] == col].copy()
        grid = sub.pivot(index="month", columns="hour_pst", values="spearman_r")
        im   = ax.imshow(grid.values, origin="upper", aspect="auto",
                         vmin=vmin, vmax=1.0, cmap="RdYlGn")
        ax.set_xlabel("Hour (PST)")
        ax.set_ylabel("Month")
        ax.set_xticks(range(0, 24, 3)); ax.set_xticklabels(range(0, 24, 3))
        ax.set_yticks(range(12));       ax.set_yticklabels(range(1, 13))
        ax.set_title(title)
        plt.colorbar(im, ax=ax, label="Spearman r")

    fig.suptitle(
        "Substation rank stability — all panels vs max_load annual (90th pct) ranking\n"
        "r=1: identical ordering to annual max_load;  r<1: ordering differs",
        fontsize=11, y=1.01,
    )
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {out_path.relative_to(ROOT)}")


# ── Summary print ─────────────────────────────────────────────────────────────

def print_summary(corr: pd.DataFrame) -> None:
    print("\n" + "=" * 70)
    print("Substation Rank Stability - all Spearman r vs max_load ANNUAL ranking")
    print("=" * 70)

    # Annual cross-percentile comparison (single r per col)
    print("\nANNUAL (single value per percentile):")
    for col in LOAD_COLS:
        r = corr[(corr["level"] == "annual") & (corr["percentile"] == col)]["spearman_r"].iloc[0]
        print(f"  {LABELS[col]:25s}  r={r:.4f}")

    for level in ["monthly", "hourly", "month_hour"]:
        sub = corr[corr["level"] == level]
        n_periods = sub[sub["percentile"] == "max_load"]["period_label"].nunique()
        print(f"\n{level.upper()} (range across {n_periods} periods):")
        for col in LOAD_COLS:
            r = sub[sub["percentile"] == col]["spearman_r"]
            print(f"  {LABELS[col]:25s}  "
                  f"min r={r.min():.4f}  mean r={r.mean():.4f}  max r={r.max():.4f}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Loading substation load profiles ...")
    df = load_profiles()
    n_subs = df["substation_name"].nunique()
    print(f"  {n_subs:,} substations, {len(df):,} unique (substation, month, hour_pst) cells")

    print("\nComputing annual rankings ...")
    annual = annual_ranks(df)
    annual.to_csv(OUT_DIR / "substation_annual_ranks.csv", index=False)
    print(f"  Saved: data/processed/load_projection/rankings/substation_annual_ranks.csv")

    print("\nComputing monthly rankings ...")
    monthly = monthly_ranks(df)
    monthly.to_csv(OUT_DIR / "substation_monthly_ranks.csv", index=False)
    print(f"  Saved: data/processed/load_projection/rankings/substation_monthly_ranks.csv")

    print("\nComputing hourly rankings ...")
    hourly = hourly_ranks(df)
    hourly.to_csv(OUT_DIR / "substation_hourly_ranks.csv", index=False)
    print(f"  Saved: data/processed/load_projection/rankings/substation_hourly_ranks.csv")

    print("\nComputing month-hour rankings (288 orderings) ...")
    mh = monthhour_ranks(df)
    mh.to_csv(OUT_DIR / "substation_monthhour_ranks.csv", index=False)
    print(f"  Saved: data/processed/load_projection/rankings/substation_monthhour_ranks.csv")

    print("\nComputing rank correlations vs annual ...")
    corr = compute_correlations(annual, monthly, hourly, mh)
    corr.to_csv(OUT_DIR / "rank_correlations.csv", index=False)
    print(f"  Saved: data/processed/load_projection/rankings/rank_correlations.csv")

    print_summary(corr)

    print("\nGenerating figure ...")
    plot_correlations(corr, FIG_DIR / "rank_correlation_heatmap.png")

    print("\nDone.")


if __name__ == "__main__":
    main()
