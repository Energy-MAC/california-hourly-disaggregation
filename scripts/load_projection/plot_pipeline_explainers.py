"""Worked-example figures explaining how Approach 1 and the nodal mapping work.

Uses REAL numbers from existing outputs (no illustrative/fake data) for the
two weight chains, and a generic schematic (illustrative, not data-driven) for
the four nodal assignment rules:

  1. reeds_chain_example    two-stage ReEDS chain (p-region -> county ->
                            substation) for two small p9 counties, one cell
                            (July, 17:00 PST): county_pgroup_fraction,
                            sub_county_weight, and their product chain_weight,
                            with the "weights sum to 1 within a county" check
                            shown explicitly.
  2. iou_chain_example      single-stage IOU chain (IOU -> substation) for
                            PGE, same cell: top substations by sub_iou_weight,
                            multiplied by one concrete IEPR total (MW) for that
                            hour so every substation's MW load and the "weights
                            sum to 1" check are both shown numerically -
                            enough to back sub_iou_weight back out from the
                            figure alone (= load_mw / total_mw).
  3. nodal_assignment_schematic   illustrative diagram of the four
                            substation -> node assignment rules used by
                            map_loads_to_nodes.py: nearest, tie-share,
                            county equal-split, centroid fallback.

CLI parameters:
  --counties   two small p9 counties for the ReEDS example, comma-separated
               (default "Calaveras,Mariposa" - each has 2-3 real substations,
               small enough to show every substation legibly)
  --utility    IOU for the single-stage example (default PGE)
  --month/--hour   the illustrated cell (default 7, 17 - a July evening peak)

Outputs (data/figures/load_projection/pipeline/):
  reeds_chain_example.png, iou_chain_example.png, nodal_assignment_schematic.png

Usage:
  python scripts/load_projection/plot_pipeline_explainers.py
"""

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
REEDS_DIR = ROOT / "data/processed/load_projection/projections/reeds_projected__max_load__monthhour"
IEPR_DIR = ROOT / ("data/processed/load_projection/projections/"
                   "iepr__v2025__planningscenario__baselineconsumption__max_load__monthhour")
PROFILE_FILE = ROOT / "data/processed/substations/substation_load_profiles_clean.csv"
IEPR_HOURLY_FILE = ROOT / "data/processed/iepr/iepr_hourly_forecast.csv"
OUT_DIR = ROOT / "data/figures/load_projection/pipeline"


def reeds_chain_example(counties: list[str], month: int, hour: int) -> None:
    cw = pd.read_csv(REEDS_DIR / "county_pgroup_weights.csv")
    chain = pd.read_csv(REEDS_DIR / "substation_chain_weights.csv")
    prof = pd.read_csv(PROFILE_FILE)
    prof = prof.groupby(["utility", "substation_name", "month", "hour_pst"],
                        as_index=False)[["max_load"]].mean()

    cw_c = cw[cw.county_name.str.lower().isin([c.lower() for c in counties])].copy()
    cw_c["county_key"] = cw_c.county_name.str.lower()
    p_region = cw_c.p_region.iloc[0]
    cell = chain[chain.county_name.isin(counties) & (chain.month == month)
                & (chain.hour_pst == hour)].sort_values(["county_name", "substation_name"]).copy()
    cell = cell.merge(prof[["utility", "substation_name", "month", "hour_pst", "max_load"]],
                      on=["utility", "substation_name", "month", "hour_pst"])
    cell["county_key"] = cell.county_name.str.lower()
    cell = cell.merge(cw_c[["county_key", "pgroup_fraction"]], on="county_key")
    within = cell.groupby("county_name")["max_load"].transform(
        lambda s: s.clip(lower=0) / s.clip(lower=0).sum())
    cell["sub_county_weight"] = within
    cell["chain_weight_check"] = cell.pgroup_fraction * cell.sub_county_weight

    fig, (ax_t, ax_b) = plt.subplots(2, 1, figsize=(10, 7),
                                     gridspec_kw={"height_ratios": [1, 1.4]})
    ax_t.axis("off")
    stage1 = cw_c[["county_name", "ca_load_fraction", "pgroup_fraction"]].round(6)
    tbl1 = ax_t.table(cellText=stage1.values, colLabels=[
        "county", "ca_load_fraction", f"pgroup_fraction (share of {p_region})"],
        loc="center", cellLoc="center")
    tbl1.auto_set_font_size(False)
    tbl1.set_fontsize(9)
    tbl1.scale(1, 1.6)
    ax_t.set_title(f"Stage 1 - county share of p-region {p_region} "
                   f"(county_pgroup_weights.csv, constant across all hours)",
                   fontsize=10, pad=14)

    ax_b.axis("off")
    disp = cell[["county_name", "substation_name", "max_load", "sub_county_weight",
                "pgroup_fraction", "chain_weight"]].round(6)
    disp.columns = ["county", "substation", "max_load (MW)", "sub_county_weight",
                    "pgroup_fraction", "chain_weight"]
    tbl2 = ax_b.table(cellText=disp.values, colLabels=disp.columns,
                      loc="upper center", cellLoc="center")
    tbl2.auto_set_font_size(False)
    tbl2.set_fontsize(9)
    tbl2.scale(1, 1.6)
    checks = cell.groupby("county_name")["sub_county_weight"].sum().round(6)
    check_str = "  |  ".join(f"sub_county_weight sums in {c}: {v}"
                             for c, v in checks.items())
    ax_b.text(0.5, -0.05, check_str, ha="center", fontsize=9, style="italic",
             transform=ax_b.transAxes)
    ax_b.set_title(f"Stage 2 - within-county substation weight, "
                   f"July {hour}:00 PST cell (chain_weight = pgroup_fraction "
                   f"x sub_county_weight)", fontsize=10, pad=14)

    fig.suptitle("ReEDS chain worked example: p-region -> county -> substation",
                 fontsize=13, y=0.99)
    fig.tight_layout()
    out = OUT_DIR / "reeds_chain_example.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"wrote {out.relative_to(ROOT)}")


def fetch_iou_total(utility: str, month: int, hour: int, year: int, day: int) -> float:
    """One concrete IEPR BASELINE_CONSUMPTION value (Planning_Scenario, v2025)
    for the illustrated cell, so the worked example has a real total to
    multiply weights against. IEPR is hour-ending; hour_pst (0-23,
    hour-beginning) -> HOUR = hour_pst + 1."""
    h = pd.read_csv(IEPR_HOURLY_FILE)
    row = h[(h.forecast_vintage_year == 2025) & (h.utility_ba == utility)
           & (h.scenario == "Planning_Scenario") & (h.YEAR == year)
           & (h.MONTH == month) & (h.DAY == day) & (h.HOUR == hour + 1)]
    return float(row.BASELINE_CONSUMPTION.iloc[0])


def iou_chain_example(utility: str, month: int, hour: int, year: int, day: int,
                      top_n: int = 8) -> None:
    w = pd.read_csv(IEPR_DIR / "substation_iou_weights.csv")
    cell = w[(w.utility == utility) & (w.month == month) & (w.hour_pst == hour)]
    total_mw = fetch_iou_total(utility, month, hour, year, day)
    top = cell.nlargest(top_n, "sub_iou_weight").copy()
    top["load_mw"] = top.sub_iou_weight * total_mw

    fig, (ax_bar, ax_t) = plt.subplots(1, 2, figsize=(12, 5),
                                       gridspec_kw={"width_ratios": [1, 1.2]})
    ax_bar.barh(top.substation_name[::-1], top.load_mw[::-1], color="#3182bd")
    ax_bar.set_xlabel("substation load (MW) = sub_iou_weight x total")
    ax_bar.set_title(f"Top {top_n} of {len(cell):,} {utility} substations\n"
                     f"{year}-{month:02d}-{day:02d}, hour_pst {hour}:00 PST",
                     fontsize=10)
    ax_bar.grid(alpha=0.3, lw=0.5, axis="x")

    ax_t.axis("off")
    disp = top[["substation_name", "sub_iou_weight", "load_mw"]].round(4)
    disp.columns = ["substation", "sub_iou_weight", "load (MW)"]
    tbl = ax_t.table(cellText=disp.values, colLabels=disp.columns,
                     loc="upper center", cellLoc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1, 1.6)
    ax_t.text(0.5, 0.06,
             f"{utility} total load this hour (IEPR Planning_Scenario, "
             f"BASELINE_CONSUMPTION): {total_mw:,.0f} MW\n"
             f"all {len(cell):,} {utility} substation weights sum to "
             f"{cell.sub_iou_weight.sum():.6f}  =>  all {len(cell):,} substation "
             f"loads sum to {cell.sub_iou_weight.sum() * total_mw:,.0f} MW\n"
             f"formula: substation_load(t) = sub_iou_weight x {utility}_IOU_load(t) "
             "-- back out sub_iou_weight as load_mw / total_mw",
             ha="center", fontsize=8.5, style="italic", transform=ax_t.transAxes)

    fig.suptitle("IOU chain worked example (IEPR/RESOLVE): single-stage "
                 "IOU -> substation", fontsize=13)
    fig.tight_layout()
    out = OUT_DIR / "iou_chain_example.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"wrote {out.relative_to(ROOT)}")


def _node(ax, xy, label=""):
    ax.add_patch(mpatches.Rectangle((xy[0] - 0.05, xy[1] - 0.05), 0.1, 0.1,
                                    facecolor="#4575b4", edgecolor="black", zorder=3))
    if label:
        ax.text(xy[0], xy[1] - 0.14, label, ha="center", fontsize=8)


def _sub(ax, xy, label="", synthetic=False):
    marker = "^" if synthetic else "o"
    ax.plot(*xy, marker=marker, color="#d73027", markersize=11,
           markeredgecolor="black", zorder=3)
    if label:
        ax.text(xy[0], xy[1] + 0.12, label, ha="center", fontsize=8)


def _arrow(ax, a, b, label=""):
    ax.annotate("", xy=b, xytext=a,
               arrowprops=dict(arrowstyle="->", color="#555555", lw=1.3))
    if label:
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        ax.text(mx, my + 0.04, label, ha="center", fontsize=8, color="#333333")


def nodal_assignment_schematic() -> None:
    fig, axes = plt.subplots(2, 2, figsize=(10, 9))

    ax = axes[0, 0]
    _node(ax, (0.3, 0.5), "node A")
    _node(ax, (0.85, 0.8), "node B")
    _sub(ax, (0.35, 0.6), "substation")
    _arrow(ax, (0.35, 0.6), (0.3, 0.5), "share = 1.0")
    ax.set_title("1. Nearest node\n(real substation, one clear closest node)")

    ax = axes[0, 1]
    _node(ax, (0.25, 0.5), "node A")
    _node(ax, (0.55, 0.52), "node B")
    _sub(ax, (0.4, 0.7), "substation")
    _arrow(ax, (0.4, 0.7), (0.25, 0.5), "share = 0.5")
    _arrow(ax, (0.4, 0.7), (0.55, 0.52), "share = 0.5")
    ax.set_title("2. Tie-share\n(nodes within --tie-tol-km of the minimum)")

    ax = axes[1, 0]
    county = mpatches.Polygon([(0.15, 0.1), (0.85, 0.15), (0.8, 0.72), (0.2, 0.68)],
                              closed=True, facecolor="#eeeeee", edgecolor="#999999")
    ax.add_patch(county)
    node_xy = [(0.3, 0.25), (0.6, 0.28), (0.45, 0.45), (0.7, 0.5)]
    for i, xy in enumerate(node_xy):
        _node(ax, xy, f"n{i + 1}")
    sub_xy = (0.5, 0.62)
    ax.plot(*sub_xy, marker="^", color="#d73027", markersize=11,
           markeredgecolor="black", zorder=3)
    ax.text(sub_xy[0] + 0.13, sub_xy[1], "SYNTHETIC_X", va="center", fontsize=8)
    for xy in node_xy:
        _arrow(ax, sub_xy, xy)
    ax.text(0.05, 0.62, "share = 1/4\neach", fontsize=8, color="#333333")
    ax.text(0.5, 0.03, "county polygon (point-in-polygon)", ha="center", fontsize=8,
           color="#666666")
    ax.set_title("3. County equal-split\n(synthetic substation, no real location)")

    ax = axes[1, 1]
    county = mpatches.Polygon([(0.15, 0.55), (0.55, 0.5), (0.5, 0.85), (0.15, 0.85)],
                              closed=True, facecolor="#eeeeee", edgecolor="#999999")
    ax.add_patch(county)
    ax.text(0.45, 0.62, "0 nodes\ninside county", ha="center", fontsize=8, color="#666666")
    sub_xy = (0.28, 0.72)
    ax.plot(*sub_xy, marker="^", color="#d73027", markersize=11,
           markeredgecolor="black", zorder=3)
    ax.text(sub_xy[0], sub_xy[1] + 0.14, "SYNTHETIC_Y", ha="center", fontsize=8)
    node_xy = (0.8, 0.35)
    _node(ax, node_xy)
    ax.text(node_xy[0] + 0.09, node_xy[1], "nearest node\n(outside county)",
           ha="left", va="center", fontsize=8)
    _arrow(ax, sub_xy, node_xy, "share = 1.0 (fallback)")
    ax.text(0.5, -0.1,
           "county has ZERO candidate nodes -> all load goes to the single\n"
           "node nearest the county centroid, wherever it is (may be outside\n"
           "the county) -- the only case a synthetic substation uses distance",
           ha="center", fontsize=7.5, color="#333333", style="italic",
           transform=ax.transAxes)
    ax.set_title("4. Centroid fallback\n(synthetic substation, county has 0 nodes)")

    for ax in axes.flat:
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_axis_off()
    fig.suptitle("Substation -> node assignment rules (map_loads_to_nodes.py)\n"
                "illustrative schematic, not real coordinates", fontsize=13)
    fig.tight_layout()
    fig.subplots_adjust(hspace=0.5)  # room for panel 4's caption below its axes
    out = OUT_DIR / "nodal_assignment_schematic.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"wrote {out.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--counties", default="Calaveras,Mariposa")
    ap.add_argument("--utility", default="PGE")
    ap.add_argument("--month", type=int, default=7)
    ap.add_argument("--hour", type=int, default=17)
    ap.add_argument("--iepr-year", type=int, default=2025,
                    help="forecast YEAR for the IOU-chain example total (default 2025)")
    ap.add_argument("--iepr-day", type=int, default=15,
                    help="day-of-month for the IOU-chain example total (default 15)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    reeds_chain_example(args.counties.split(","), args.month, args.hour)
    iou_chain_example(args.utility, args.month, args.hour, args.iepr_year, args.iepr_day)
    nodal_assignment_schematic()


if __name__ == "__main__":
    main()
