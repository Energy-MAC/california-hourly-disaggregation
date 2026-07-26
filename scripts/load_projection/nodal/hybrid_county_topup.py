"""PROTOTYPE — hybrid ReEDS/stochastic top-up for under-covered counties.

NOT yet a formal disaggregation approach and NOT wired into map_loads_to_nodes.py
or the Approach 1/2 pipelines — this is an additive, standalone exploration
the user asked to "toy around with" before deciding whether to formalize it.
The original stochastic (Approach 2) and weights (Approach 1) outputs are
never modified; this script only produces a separate top-up allocation file.

Motivation: some counties have real IOU substations but far more CATS demand
nodes than substations (Trinity: 4 nodes, 1 substation), so most of the
county's nodes get zero load under nearest-node mapping even though the
county clearly has real demand (see validate_county_reeds.py and the README
"Nodal coverage gaps" section). This prototype tops up those counties'
*uncovered* CATS nodes (nodes with no real-substation-driven load) with the
shortfall between an independent ReEDS county reference and the Approach 2
stochastic county total — never touching the substations or the nodes they
already cover.

Design (per user decisions 2026-07-22, all deliberately conservative — the
gate is generous and meant to be refined once the qualifying-county list has
been inspected, not treated as final):
  - Gate: county's (real substations / demand-filtered candidate nodes)
    ratio < --ratio-threshold (default 0.5, "generous, refine later").
  - Applied in EVERY qualifying county where ReEDS annual > stochastic
    annual, including municipal-utility-dominated counties (Sacramento/SMUD,
    Imperial/IID, etc.) — unlike a naive top-up onto substations, this is
    correct here because the shortfall lands on the county's *CATS nodes*,
    not its IOU substations, and those uncovered CATS nodes are presumed to
    include the county's actual MOU (municipally owned utility) buses.
  - Shortfall = reeds_annual_mwh - stochastic_annual_mwh (only when positive)
    is split across the county's uncovered candidate nodes by --method:
      equal: every uncovered node gets shortfall / n_uncovered (same
        convention as ReEDS synthetic substations).
      proportional: split in proportion to each node's own CATS
        Demand_data.csv load, so nodes CATS already models as bigger demand
        points get a proportionally bigger share (introduces variation
        between nodes, vs equal's uniform share).
  - Annual resolution only (2016-2023 overlap, see validate_county_reeds.py);
    hourly/monthly top-up is future work once the annual version is vetted.

CLI parameters:
  --stochastic-run     run-tag folder under
                        data/processed/load_projection/projections/
                        (default stochastic__eia930__normal__Fcal__native)
  --system              nodal output folder to reuse (default CATS; requires
                        map_loads_to_nodes.py --system CATS to have run first)
  --nodes/--id-col/--lat-col/--lon-col/--filter/--demand-file  node system
                        selection (same conventions as map_loads_to_nodes.py)
  --ratio-threshold     substations-per-node gate (default 0.5)
  --method              equal (default) | proportional — see above

Outputs (data/processed/load_projection/validation/), one pair per --method:
  hybrid_topup_counties_{method}.csv   per (county, year): ratio,
                               reeds/stochastic totals, shortfall, n_uncovered
  hybrid_topup_nodes_{method}.csv      per (county, year, node): top-up MWh
Figure (data/figures/load_projection/validation/):
  hybrid_topup_map_{method}.png   per-county map of top-up node locations/
                               sizes + reeds vs stochastic vs +topup bars

Usage:
  python scripts/load_projection/nodal/hybrid_county_topup.py --method equal
  python scripts/load_projection/nodal/hybrid_county_topup.py --method proportional
  python scripts/load_projection/nodal/hybrid_county_topup.py --ratio-threshold 0.3
"""

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/load_projection/nodal"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/checks"))
from map_loads_to_nodes import filter_zero_demand, demand_totals  # noqa: E402
from validate_county_reeds import reeds_county_annual, stochastic_county_annual  # noqa: E402

PROCESSED = ROOT / "data/processed"
NODAL_DIR = PROCESSED / "load_projection/nodal"
COUNTY_SHP = ROOT / ("data/raw/reeds/ReEDS-2.0/inputs/shapefiles/cache/"
                     "tl_2022_us_county/tl_2022_us_county.shp")
OUT_DIR = PROCESSED / "load_projection/validation"
FIG_DIR = ROOT / "data/figures/load_projection/validation"


def load_candidate_nodes(args) -> pd.DataFrame:
    nodes = pd.read_csv(args.nodes)
    for c in nodes.columns:
        if pd.api.types.is_string_dtype(nodes[c]):
            nodes[c] = nodes[c].str.strip().str.strip("'").str.strip()
    filters = [f.split("=", 1) for f in args.filter]
    if not args.no_default_filters:
        if "Type" in nodes.columns and not any(c == "Type" for c, _ in filters):
            nodes = nodes[nodes.Type == "Substation"]
        if "Import" in nodes.columns:
            nodes = nodes[nodes.Import != "IMPORT"]
    for col, val in filters:
        nodes = nodes[nodes[col].astype(str) == val]
    nodes = filter_zero_demand(nodes, args)
    return nodes.reset_index(drop=True)


def nodes_by_county(nodes: pd.DataFrame, args) -> pd.DataFrame:
    """Candidate nodes -> county_name, fips_int (point-in-polygon, TIGER 2022)."""
    gdf = gpd.GeoDataFrame(
        nodes, geometry=gpd.points_from_xy(nodes[args.lon_col], nodes[args.lat_col]),
        crs=4326)
    county = gpd.read_file(COUNTY_SHP)
    county = county[county.STATEFP == "06"].to_crs(4326)
    county["fips_int"] = county.STATEFP.astype(int) * 1000 + county.COUNTYFP.astype(int)
    j = gpd.sjoin(gdf, county[["NAME", "fips_int", "geometry"]], predicate="within")
    return j[[args.id_col, "NAME", "fips_int"]].rename(columns={"NAME": "county_name"})


def plot_topup(county_out: pd.DataFrame, node_out: pd.DataFrame,
              nodes: pd.DataFrame, args) -> None:
    """Two-panel figure: (1) small-multiples map per qualifying county showing
    which uncovered nodes receive the top-up and how much; (2) bar chart of
    reeds vs stochastic vs stochastic+topup totals per county (mean over
    years) so the size of the closed gap is visible at a glance."""
    counties = sorted(county_out.county_name.unique())
    node_mean = node_out.groupby(["county_name", "node"], as_index=False)["topup_mwh"].mean()
    node_coords = nodes[[args.id_col, args.lat_col, args.lon_col]].rename(
        columns={args.id_col: "node"})
    node_coords["node"] = node_coords.node.astype(str)

    county_geo = gpd.read_file(COUNTY_SHP)
    county_geo = county_geo[(county_geo.STATEFP == "06") & county_geo.NAME.isin(counties)].to_crs(4326)

    fig = plt.figure(figsize=(4 * len(counties), 8))
    for i, county in enumerate(counties):
        ax = fig.add_subplot(2, len(counties), i + 1)
        geom = county_geo[county_geo.NAME == county]
        geom.boundary.plot(ax=ax, color="black", linewidth=1.0)
        pts = node_mean[node_mean.county_name == county].merge(node_coords, on="node")
        sizes = 20 + 300 * pts.topup_mwh / node_mean.topup_mwh.max()
        ax.scatter(pts[args.lon_col], pts[args.lat_col], s=sizes, c="#d73027",
                  alpha=0.7, edgecolor="white", linewidth=0.4)
        ax.set_title(f"{county}\n({len(pts)} uncovered nodes)", fontsize=10)
        ax.set_axis_off()

    ax2 = fig.add_subplot(2, 1, 2)
    summary = county_out.groupby("county_name").agg(
        reeds_mwh=("reeds_mwh", "mean"), stochastic_mwh=("stochastic_mwh", "mean"),
        shortfall_mwh=("shortfall_mwh", "mean")).loc[counties]
    x = np.arange(len(counties))
    w = 0.35
    ax2.bar(x - w / 2, summary.reeds_mwh / 1e6, w, label="ReEDS county reference", color="#4575b4")
    ax2.bar(x + w / 2, (summary.stochastic_mwh + summary.shortfall_mwh) / 1e6, w,
           label="stochastic + top-up", color="#fc8d59")
    ax2.bar(x + w / 2, summary.stochastic_mwh / 1e6, w,
           label="stochastic (pre-top-up)", color="#91bfdb")
    ax2.set_xticks(x)
    ax2.set_xticklabels(counties)
    ax2.set_ylabel("mean annual load (TWh/yr)")
    ax2.set_title(f"ReEDS reference vs stochastic total, before/after top-up "
                 f"(method={args.method}, mean over 2016-2023)")
    ax2.legend()
    ax2.grid(alpha=0.3, axis="y")

    fig.tight_layout()
    fig_path = FIG_DIR / f"hybrid_topup_map_{args.method}.png"
    fig.savefig(fig_path, dpi=150)
    print(f"wrote {fig_path.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stochastic-run", default="stochastic__eia930__normal__Fcal__native")
    ap.add_argument("--nodes", default=str(ROOT / "data/raw/CATS/CATS_buses.csv"))
    ap.add_argument("--system", default="CATS")
    ap.add_argument("--id-col", default="bus_i")
    ap.add_argument("--lat-col", default="Lat")
    ap.add_argument("--lon-col", default="Lon")
    ap.add_argument("--filter", action="append", default=[])
    ap.add_argument("--no-default-filters", action="store_true")
    ap.add_argument("--demand-file", default=str(ROOT / "data/raw/CATS/Demand_data.csv"))
    ap.add_argument("--demand-col-prefix", default="Demand_MW_z")
    ap.add_argument("--no-demand-filter", action="store_true")
    ap.add_argument("--ratio-threshold", type=float, default=0.5)
    ap.add_argument("--method", choices=["equal", "proportional"], default="equal",
                    help="equal: shortfall split evenly across uncovered nodes. "
                         "proportional: split in proportion to each node's own "
                         "CATS Demand_data.csv load (falls back to equal for a "
                         "county where every uncovered node has zero CATS load)")
    args = ap.parse_args()
    system = args.system

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("loading candidate nodes, county assignment, ReEDS + stochastic county totals...")
    nodes = load_candidate_nodes(args)
    node_county = nodes_by_county(nodes, args)
    n_nodes_by_county = node_county.groupby(["fips_int", "county_name"]).size().rename("n_nodes")

    map_path = NODAL_DIR / system / "substation_node_map.csv"
    smap = pd.read_csv(map_path)
    real = smap[~smap.is_synthetic]
    covered_nodes = set(real.node.astype(str))

    sc = pd.read_csv(PROCESSED / "substations/substation_county_reeds_mapping.csv",
                     usecols=["utility", "substation_name", "fips_int", "county_name"])
    sc["utility"] = sc.utility.str.lower()
    n_subs_by_county = sc.groupby(["fips_int", "county_name"]).size().rename("n_substations")

    cov = pd.DataFrame(n_nodes_by_county).join(n_subs_by_county, how="left").fillna(0).reset_index()
    cov["n_substations"] = cov.n_substations.astype(int)
    cov["ratio"] = cov.n_substations / cov.n_nodes
    qualifying = cov[(cov.n_substations > 0) & (cov.ratio < args.ratio_threshold)]
    print(f"{len(qualifying)} counties qualify at ratio < {args.ratio_threshold} "
          f"(of {len(cov)} counties with >=1 candidate node)")

    reeds = reeds_county_annual()
    stoch = stochastic_county_annual(args.stochastic_run)
    long = stoch.merge(reeds[["fips_int", "year", "reeds_mwh"]],
                       on=["fips_int", "year"], how="inner")
    long = long.merge(qualifying[["fips_int"]], on="fips_int", how="inner")
    long["shortfall_mwh"] = (long.reeds_mwh - long.stochastic_mwh).clip(lower=0)
    long = long.merge(cov[["fips_int", "n_nodes", "n_substations", "ratio"]],
                      on="fips_int", how="left")

    node_weight = {}
    if args.method == "proportional":
        node_weight = demand_totals(Path(args.demand_file), args.demand_col_prefix)

    node_rows = []
    county_rows = []
    for fips, g in long.groupby("fips_int"):
        county_nodes = node_county.loc[node_county.fips_int == fips, args.id_col].astype(str)
        uncovered = [n for n in county_nodes if n not in covered_nodes]
        if args.method == "proportional":
            weights = np.array([node_weight.get(n, 0.0) for n in uncovered])
            if weights.sum() <= 0:  # no CATS-load signal for any uncovered node: fall back to equal
                weights = np.ones(len(uncovered))
            weights = weights / weights.sum() if len(weights) else weights
        else:
            weights = np.full(len(uncovered), 1.0 / len(uncovered)) if uncovered else np.array([])
        for _, row in g.iterrows():
            county_rows.append({**row.to_dict(), "n_uncovered_nodes": len(uncovered)})
            if row.shortfall_mwh > 0 and uncovered:
                for node_id, w in zip(uncovered, weights):
                    node_rows.append({"fips_int": fips, "county_name": row.county_name,
                                      "year": row.year, "node": node_id,
                                      "topup_mwh": row.shortfall_mwh * w})

    county_out = pd.DataFrame(county_rows)
    node_out = pd.DataFrame(node_rows)
    county_out.round(2).to_csv(OUT_DIR / f"hybrid_topup_counties_{args.method}.csv", index=False)
    node_out.round(3).to_csv(OUT_DIR / f"hybrid_topup_nodes_{args.method}.csv", index=False)

    n_zero_uncovered = (county_out.groupby("county_name")["n_uncovered_nodes"].first() == 0).sum()
    print(f"\nqualifying counties: {sorted(qualifying.county_name.unique())}")
    print(f"counties with a shortfall but ZERO uncovered nodes (cannot top up): "
          f"{n_zero_uncovered}")
    print(f"\ntotal top-up by county (mean MWh/yr over overlap years):")
    summary = county_out.groupby("county_name").agg(
        ratio=("ratio", "first"), n_uncovered_nodes=("n_uncovered_nodes", "first"),
        mean_shortfall_mwh=("shortfall_mwh", "mean"),
        mean_reeds_mwh=("reeds_mwh", "mean"),
        mean_stochastic_mwh=("stochastic_mwh", "mean"),
    ).sort_values("mean_shortfall_mwh", ascending=False)
    summary["ratio"] = summary.ratio.round(3)
    for c in ["mean_shortfall_mwh", "mean_reeds_mwh", "mean_stochastic_mwh"]:
        summary[c] = summary[c].round(0)
    print(summary.to_string())
    print(f"\nwrote {(OUT_DIR / f'hybrid_topup_counties_{args.method}.csv').relative_to(ROOT)}")
    print(f"wrote {(OUT_DIR / f'hybrid_topup_nodes_{args.method}.csv').relative_to(ROOT)}")

    if len(node_out):
        FIG_DIR.mkdir(parents=True, exist_ok=True)
        plot_topup(county_out, node_out, nodes, args)


if __name__ == "__main__":
    main()
