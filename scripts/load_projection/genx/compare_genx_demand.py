"""Compare rescaled GenX demand sets against the control, before running GenX.

This is the INPUT-side half of the comparison. Because every rescale run holds
the statewide hourly total exactly fixed, the demand files differ only in where
load sits -- so the metrics here measure spatial divergence and nothing else.
Running it before the cluster is the cheap way to know how much signal a GenX
comparison could possibly carry: if two demand sets relocate only 2% of the
state's energy, no dispatch difference downstream can be large.

Headline metric: **energy relocated**, 0.5 * sum_i |A_i - B_i| / sum_i A_i.
The half is what makes it a *movement* fraction rather than a difference count --
every MWh moved shows up twice, once as a loss at its origin bus and once as a
gain at its destination. Read it as "x% of the state's energy sits on a
different bus than the control puts it on."

The same quantity is computed at bus level and at county level, and the gap
between them is the interesting part: bus-level movement that vanishes under
county aggregation means the two methods agree about regional load and disagree
only about which bus inside the region carries it -- exactly the disagreement a
nodal network model can resolve and a zonal one cannot.

CLI parameters
  --runs        comma-separated run tags under genx/rescaled
                (default: every run present)
  --baseline    run tag to compare against (default genx__control)
  --season      restrict to one season (default: all four)
  --no-figures  metrics only

Outputs (data/checks/genx_rescale/)
  demand_comparison_summary.csv   per (run, season) metrics
  demand_bus_deltas.csv           per (run, season, bus) control/method/delta
  demand_county_deltas.csv        per (run, season, county) control/method/delta
Figures (data/figures/genx/)
  demand_delta_map__{run}.png       bus-level delta, diverging, geographic
  demand_scatter__{run}.png         control vs method per-bus season energy
  demand_county_delta__{run}.png    county-level delta, sorted
  demand_statewide_profile.png      hourly statewide totals (conservation check)

Usage
  python scripts/load_projection/genx/compare_genx_demand.py
  python scripts/load_projection/genx/compare_genx_demand.py --runs genx__stoch__prox__w2-mean__monthhour
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
from genx_demand_io import GENX_ROOT, read_demand, scenario_seasons  # noqa: E402

RESCALED = GENX_ROOT / "rescaled"
CATS_BUSES = ROOT / "data/raw/CATS/CATS_buses.csv"
OUT_DIR = ROOT / "data/checks/genx_rescale"
FIG_DIR = ROOT / "data/figures/genx"
BASELINE_DEFAULT = "genx__control"

# validated palette (dataviz reference instance); diverging blue<->red with a
# neutral gray midpoint, categorical slots 1-3, recessive chrome
C_POS, C_NEG, C_MID = "#2a78d6", "#e34948", "#f0efec"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
INK, INK_2, MUTED, GRID = "#0b0b0b", "#52514e", "#898781", "#e1e0d9"


def bus_coords() -> pd.DataFrame:
    b = pd.read_csv(CATS_BUSES)
    for c in b.columns:
        if pd.api.types.is_string_dtype(b[c]):
            b[c] = b[c].str.strip().str.strip("'").str.strip()
    b["node"] = b.bus_i.astype(str)
    return b[["node", "Lat", "Lon", "kV", "Type"]]


def node_counties() -> pd.DataFrame:
    """Bus -> county, reusing the rescaler's own memoized point-in-polygon join."""
    sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
    from rescale_genx_demand import candidate_buses
    return candidate_buses()[["node", "county_name", "fips_int"]]


def relocated_pct(a: np.ndarray, b: np.ndarray) -> float:
    """Half the L1 distance as a share of total -- the movement fraction."""
    total = a.sum()
    return float(100 * 0.5 * np.abs(a - b).sum() / total) if total > 0 else np.nan


def season_metrics(ctrl, meth, counties: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    """Per-season divergence between two demand matrices on the same zones."""
    if ctrl.zones != meth.zones:
        raise ValueError("demand files do not share a zone ordering")
    a, b = ctrl.values, meth.values
    a_bus, b_bus = a.sum(axis=0), b.sum(axis=0)   # season energy per bus

    loaded_a, loaded_b = a_bus > 0, b_bus > 0
    both = loaded_a & loaded_b
    # normalize errors by the mean load of a bus the CONTROL actually loads.
    # Normalizing by the whole matrix's mean would divide by ~72% structural
    # zeros and report percentages that look enormous but mean nothing.
    scale_a = float(a[:, loaded_a].mean()) if loaded_a.any() else np.nan

    bus = pd.DataFrame({
        "node": ctrl.zones,
        "control_mwh": a_bus,
        "method_mwh": b_bus,
        "delta_mwh": b_bus - a_bus,
    })
    cty = bus.merge(counties, on="node", how="left")
    cty_agg = cty.groupby("county_name", as_index=False)[
        ["control_mwh", "method_mwh"]].sum()

    # rank agreement only over buses both sets load; zeros would dominate otherwise
    if both.sum() > 2:
        rho = float(spearmanr(a_bus[both], b_bus[both]).statistic)
        pear = float(np.corrcoef(a_bus[both], b_bus[both])[0, 1])
    else:
        rho = pear = np.nan

    m = {
        "energy_relocated_pct": relocated_pct(a_bus, b_bus),
        "county_energy_relocated_pct": relocated_pct(
            cty_agg.control_mwh.to_numpy(), cty_agg.method_mwh.to_numpy()),
        "bus_hour_rmse_mw": float(np.sqrt(((a - b) ** 2).mean())),
        "bus_hour_nrmse_pct": float(100 * np.sqrt(((a - b) ** 2).mean()) / scale_a),
        "mean_loaded_bus_mw_control": scale_a,
        "spearman_bus_energy": rho,
        "pearson_bus_energy": pear,
        "n_buses_control": int(loaded_a.sum()),
        "n_buses_method": int(loaded_b.sum()),
        "n_buses_both": int(both.sum()),
        "jaccard_support": float(both.sum() / (loaded_a | loaded_b).sum()),
        "max_bus_gain_mwh": float(bus.delta_mwh.max()),
        "max_bus_loss_mwh": float(bus.delta_mwh.min()),
        "peak_bus_mw_control": float(a.max()),
        "peak_bus_mw_method": float(b.max()),
        "hourly_total_max_dev_mw": float(np.abs(a.sum(axis=1) - b.sum(axis=1)).max()),
    }
    return m, bus


def fig_delta_map(bus: pd.DataFrame, coords: pd.DataFrame, run: str, season: str) -> None:
    d = bus.merge(coords, on="node", how="inner")
    d = d[d.delta_mwh.abs() > 1e-9]
    if d.empty:
        return
    lim = float(np.nanpercentile(d.delta_mwh.abs(), 99)) or 1.0

    fig, ax = plt.subplots(figsize=(7.2, 8.4))
    base = coords[coords.Type == "Substation"]
    ax.scatter(base.Lon, base.Lat, s=1.5, c=GRID, linewidths=0, zorder=1)
    order = d.delta_mwh.abs().sort_values().index          # big deltas drawn last
    d = d.loc[order]
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list(
        "delta", [C_NEG, C_MID, C_POS])
    sc = ax.scatter(d.Lon, d.Lat, c=d.delta_mwh.clip(-lim, lim), cmap=cmap,
                    vmin=-lim, vmax=lim, s=6 + 40 * (d.delta_mwh.abs() / lim).clip(0, 1),
                    linewidths=0.2, edgecolors="white", zorder=2)
    cb = fig.colorbar(sc, ax=ax, fraction=0.036, pad=0.02)
    cb.set_label("change in week energy at bus (MWh)", color=INK_2, fontsize=9)
    cb.ax.tick_params(colors=MUTED, labelsize=8)
    cb.outline.set_visible(False)

    ax.set_title(f"Where the load moves — {run}\n{season} week, vs control "
                 f"(blue = bus gains load, red = bus loses load)",
                 fontsize=11, color=INK)
    ax.set_xlabel("longitude", color=INK_2, fontsize=9)
    ax.set_ylabel("latitude", color=INK_2, fontsize=9)
    ax.tick_params(colors=MUTED, labelsize=8)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.set_aspect(1 / np.cos(np.radians(37)))
    fig.tight_layout()
    out = FIG_DIR / f"demand_delta_map__{run}__{season}.png"
    fig.savefig(out, dpi=160, facecolor="white")
    plt.close(fig)
    print(f"  wrote {out.relative_to(ROOT)}")


def fig_scatter(bus: pd.DataFrame, run: str, season: str) -> None:
    d = bus[(bus.control_mwh > 0) | (bus.method_mwh > 0)].copy()
    fig, ax = plt.subplots(figsize=(6.4, 6.2))
    floor = 1e-1
    x = d.control_mwh.clip(lower=floor)
    y = d.method_mwh.clip(lower=floor)
    hi = max(x.max(), y.max())
    ax.plot([floor, hi], [floor, hi], color=MUTED, lw=1.0, ls="--", zorder=1)
    ax.scatter(x, y, s=9, color=SERIES[0], alpha=0.45, linewidths=0, zorder=2)
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("control bus energy (MWh/week)", color=INK_2, fontsize=9)
    ax.set_ylabel("rescaled bus energy (MWh/week)", color=INK_2, fontsize=9)

    n_zeroed = int(((d.control_mwh > 0) & (d.method_mwh <= 0)).sum())
    n_new = int(((d.control_mwh <= 0) & (d.method_mwh > 0)).sum())
    ax.set_title(f"Per-bus energy, rescaled vs control — {run}\n{season} week · "
                 f"{n_zeroed:,} buses zeroed, {n_new:,} newly loaded "
                 f"(points on the axis floor)", fontsize=10, color=INK)
    ax.grid(alpha=0.35, color=GRID, lw=0.6)
    ax.tick_params(colors=MUTED, labelsize=8)
    for s in ax.spines.values():
        s.set_color(GRID)
    fig.tight_layout()
    out = FIG_DIR / f"demand_scatter__{run}__{season}.png"
    fig.savefig(out, dpi=160, facecolor="white")
    plt.close(fig)
    print(f"  wrote {out.relative_to(ROOT)}")


def fig_county(bus: pd.DataFrame, counties: pd.DataFrame, run: str, season: str) -> None:
    d = bus.merge(counties, on="node", how="inner")
    agg = d.groupby("county_name", as_index=False)[["control_mwh", "method_mwh"]].sum()
    agg["delta_mwh"] = agg.method_mwh - agg.control_mwh
    agg = agg[agg.delta_mwh.abs() > 0].sort_values("delta_mwh")
    if agg.empty:
        return
    fig, ax = plt.subplots(figsize=(7.6, max(5, 0.22 * len(agg))))
    colors = [C_POS if v > 0 else C_NEG for v in agg.delta_mwh]
    ax.barh(agg.county_name, agg.delta_mwh / 1e3, color=colors, height=0.72)
    ax.axvline(0, color=MUTED, lw=1.0)
    ax.set_xlabel("change in county week energy (GWh)", color=INK_2, fontsize=9)
    ax.set_title(f"County-level load reallocation — {run}\n{season} week, vs control",
                 fontsize=11, color=INK)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(alpha=0.3, color=GRID, lw=0.6, axis="x")
    for s in ax.spines.values():
        s.set_color(GRID)
    fig.tight_layout()
    out = FIG_DIR / f"demand_county_delta__{run}__{season}.png"
    fig.savefig(out, dpi=160, facecolor="white")
    plt.close(fig)
    print(f"  wrote {out.relative_to(ROOT)}")


def fig_profiles(profiles: dict[str, np.ndarray], season: str) -> None:
    """Statewide hourly totals for every run -- they must lie exactly on top."""
    fig, ax = plt.subplots(figsize=(9.5, 4.2))
    for i, (run, tot) in enumerate(profiles.items()):
        ax.plot(np.arange(len(tot)), tot / 1e3, lw=2.0 if i == 0 else 1.2,
                ls="-" if i == 0 else (0, (4, 3)),
                color=SERIES[i % len(SERIES)], label=run, zorder=3 - i)
    ax.set_xlabel(f"hour of {season} representative week", color=INK_2, fontsize=9)
    ax.set_ylabel("statewide demand (GW)", color=INK_2, fontsize=9)
    ax.set_xticks(range(0, 169, 24))
    ax.set_title(f"Statewide hourly demand is identical across runs — {season}\n"
                 "conservation check: the curves coincide exactly by construction",
                 fontsize=11, color=INK)
    leg = ax.legend(fontsize=8, frameon=False, loc="upper right")
    for t in leg.get_texts():
        t.set_color(INK_2)
    ax.grid(alpha=0.3, color=GRID, lw=0.6)
    ax.tick_params(colors=MUTED, labelsize=8)
    for s in ax.spines.values():
        s.set_color(GRID)
    fig.tight_layout()
    out = FIG_DIR / f"demand_statewide_profile__{season}.png"
    fig.savefig(out, dpi=160, facecolor="white")
    plt.close(fig)
    print(f"  wrote {out.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", default=None)
    ap.add_argument("--baseline", default=BASELINE_DEFAULT)
    ap.add_argument("--season", default=None)
    ap.add_argument("--no-figures", action="store_true")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)

    available = sorted(p.name for p in RESCALED.iterdir() if p.is_dir())
    runs = args.runs.split(",") if args.runs else [r for r in available
                                                   if r != args.baseline]
    missing = [r for r in [args.baseline] + runs if r not in available]
    if missing:
        raise FileNotFoundError(
            f"run(s) not found under {RESCALED}: {missing}\navailable: {available}")

    tree = scenario_seasons()
    seasons = [args.season] if args.season else tree.seasons
    counties = node_counties()
    coords = bus_coords()

    rows, bus_rows, county_rows = [], [], []
    for season in seasons:
        ctrl = read_demand(RESCALED / args.baseline / f"Demand_data__{season}.csv")
        profiles = {args.baseline: ctrl.values.sum(axis=1)}
        for run in runs:
            meth = read_demand(RESCALED / run / f"Demand_data__{season}.csv")
            profiles[run] = meth.values.sum(axis=1)
            m, bus = season_metrics(ctrl, meth, counties)
            rows.append({"run": run, "baseline": args.baseline, "season": season, **m})
            print(f"{run} / {season}: {m['energy_relocated_pct']:.2f}% of energy "
                  f"relocated at bus level, {m['county_energy_relocated_pct']:.2f}% "
                  f"at county level (Spearman {m['spearman_bus_energy']:.3f})")

            keep = bus[bus.delta_mwh.abs() > 1e-9].copy()
            keep.insert(0, "season", season); keep.insert(0, "run", run)
            bus_rows.append(keep)

            cd = bus.merge(counties, on="node", how="inner").groupby(
                ["fips_int", "county_name"], as_index=False)[
                ["control_mwh", "method_mwh"]].sum()
            cd["delta_mwh"] = cd.method_mwh - cd.control_mwh
            cd.insert(0, "season", season); cd.insert(0, "run", run)
            county_rows.append(cd)

            if not args.no_figures:
                fig_delta_map(bus, coords, run, season)
                fig_scatter(bus, run, season)
                fig_county(bus, counties, run, season)
        if not args.no_figures:
            fig_profiles(profiles, season)

    summary = pd.DataFrame(rows)
    summary.round(4).to_csv(OUT_DIR / "demand_comparison_summary.csv", index=False)
    pd.concat(bus_rows).round(3).to_csv(OUT_DIR / "demand_bus_deltas.csv", index=False)
    pd.concat(county_rows).round(2).to_csv(OUT_DIR / "demand_county_deltas.csv", index=False)

    print("\n=== summary (mean over seasons) ===")
    cols = ["energy_relocated_pct", "county_energy_relocated_pct",
            "bus_hour_nrmse_pct", "spearman_bus_energy", "n_buses_method",
            "jaccard_support"]
    print(summary.groupby("run")[cols].mean().round(3).to_string())
    print(f"\nwrote {(OUT_DIR / 'demand_comparison_summary.csv').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
