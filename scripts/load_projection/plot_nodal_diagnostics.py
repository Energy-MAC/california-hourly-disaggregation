"""Diagnostic figures for the substation -> node mapping (map_loads_to_nodes.py).

Three figures from substation_node_map.csv plus a from-scratch Voronoi
partition of the candidate nodes:

  1. dist_hist    histogram of real-substation assignment distance (km)
  2. tie_hist     distribution of how many substations tie-share a node
                  (n_tied per real substation)
  3. voronoi      Voronoi cells of every candidate node, clipped to California,
                  with substations overlaid — the geometric partition that
                  nearest-node assignment is implicitly performing (nearest
                  node <=> same Voronoi cell). Applies to any approach, since
                  the node mapping is shared infrastructure.

Requires substation_node_map.csv to already exist for --system (run
map_loads_to_nodes.py first).

CLI parameters:
  --system    folder name under data/processed/load_projection/nodal/ (default CATS)
  --nodes     node CSV, for the Voronoi generators (default data/raw/CATS/CATS_buses.csv)
  --id-col/--lat-col/--lon-col   node columns (default bus_i/Lat/Lon)
  --filter / --no-default-filters   candidate-node filter (see map_loads_to_nodes.py)

Outputs (data/figures/load_projection/nodal/{system}/):
  dist_hist.png, tie_hist.png, voronoi.png

Usage:
  python scripts/load_projection/plot_nodal_diagnostics.py --system CATS
"""

import argparse
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from shapely import voronoi_polygons
from shapely.geometry import MultiPoint

ROOT = Path(__file__).resolve().parents[2]
NODAL_DIR = ROOT / "data/processed/load_projection/nodal"
COUNTY_SHP = ROOT / ("data/raw/reeds/ReEDS-2.0/inputs/shapefiles/cache/"
                     "tl_2022_us_county/tl_2022_us_county.shp")
FIG_DIR = ROOT / "data/figures/load_projection/nodal"


def dist_hist(m: pd.DataFrame, out_dir: Path) -> None:
    real = m[~m.is_synthetic]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.hist(real.dist_km, bins=np.arange(0, real.dist_km.max() + 1, 0.5),
           color="#3182bd", edgecolor="white", linewidth=0.3)
    ax.axvline(real.dist_km.median(), color="#c0392b", ls="--", lw=1.3,
              label=f"median {real.dist_km.median():.2f} km")
    ax.set_xlim(0, min(30, real.dist_km.max()))
    ax.set_yscale("log")
    ax.set_xlabel("distance to assigned node (km)")
    ax.set_ylabel("count (substation-node assignment rows, log scale)")
    ax.set_title(f"Substation -> node assignment distance "
                f"(n={real.groupby(['utility', 'substation_name']).ngroups:,} "
                f"real substations)")
    ax.legend()
    ax.grid(alpha=0.3, lw=0.5)
    fig.tight_layout()
    fig.savefig(out_dir / "dist_hist.png", dpi=150)
    plt.close(fig)
    print(f"wrote {(out_dir / 'dist_hist.png').relative_to(ROOT)}")


def tie_hist(m: pd.DataFrame, out_dir: Path) -> None:
    real = m[~m.is_synthetic]
    per_sub = real.groupby(["utility", "substation_name"])["n_tied"].first()
    counts = per_sub.value_counts().sort_index()
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.bar(counts.index.astype(str), counts.values, color="#3182bd")
    for x, v in enumerate(counts.values):
        ax.text(x, v, f"{v:,}", ha="center", va="bottom", fontsize=9)
    ax.set_xlabel("nodes tied within --tie-tol-km of the nearest")
    ax.set_ylabel("number of substations")
    ax.set_title(f"Tie-sharing: {(per_sub > 1).sum()} of {len(per_sub):,} "
                f"substations ({(per_sub > 1).mean():.1%}) split across 2+ nodes")
    ax.grid(alpha=0.3, lw=0.5, axis="y")
    fig.tight_layout()
    fig.savefig(out_dir / "tie_hist.png", dpi=150)
    plt.close(fig)
    print(f"wrote {(out_dir / 'tie_hist.png').relative_to(ROOT)}")


def load_nodes(args) -> pd.DataFrame:
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
    return nodes.reset_index(drop=True)


def voronoi_figure(nodes: pd.DataFrame, subs: pd.DataFrame, args, out_dir: Path) -> None:
    """Voronoi cells of the candidate nodes, clipped to the CA state boundary
    (dissolved county polygons). Nearest-node assignment <=> substation falls
    in that node's cell, so this is a direct visualization of the geometric
    partition map_loads_to_nodes.py performs implicitly via distance search."""
    ca = gpd.read_file(COUNTY_SHP)
    ca = ca[ca.STATEFP == "06"].to_crs(3310)  # CA Albers for area-honest clipping
    boundary = ca.union_all()

    pts = gpd.GeoSeries(gpd.points_from_xy(nodes[args.lon_col], nodes[args.lat_col]),
                        crs=4326).to_crs(3310)
    cells = voronoi_polygons(MultiPoint(pts.values), extend_to=boundary)
    cells = gpd.GeoSeries(list(cells.geoms), crs=3310).clip(boundary)

    fig, ax = plt.subplots(figsize=(9, 10))
    cells.boundary.plot(ax=ax, color="#999999", linewidth=0.25)
    gpd.GeoSeries([boundary]).boundary.plot(ax=ax, color="black", linewidth=1.0)
    sub_pts = gpd.GeoSeries(gpd.points_from_xy(subs.util_lon, subs.util_lat),
                            crs=4326).to_crs(3310)
    colors = {"pge": "#1b9e77", "sce": "#d95f02", "sdge": "#7570b3"}
    for util, color in colors.items():
        mask = subs.utility.str.lower() == util
        if mask.any():
            sub_pts[mask.values].plot(ax=ax, color=color, markersize=4,
                                      label=util.upper(), zorder=3)
    ax.set_title(f"Voronoi partition of {len(nodes):,} candidate CATS nodes\n"
                f"(nearest-node assignment = which cell a substation falls in)")
    ax.legend(markerscale=2, loc="lower left")
    ax.set_axis_off()
    fig.tight_layout()
    fig.savefig(out_dir / "voronoi.png", dpi=150)
    plt.close(fig)
    print(f"wrote {(out_dir / 'voronoi.png').relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--system", default="CATS")
    ap.add_argument("--nodes", default=str(ROOT / "data/raw/CATS/CATS_buses.csv"))
    ap.add_argument("--id-col", default="bus_i")
    ap.add_argument("--lat-col", default="Lat")
    ap.add_argument("--lon-col", default="Lon")
    ap.add_argument("--filter", action="append", default=[])
    ap.add_argument("--no-default-filters", action="store_true")
    args = ap.parse_args()

    map_path = NODAL_DIR / args.system / "substation_node_map.csv"
    m = pd.read_csv(map_path)
    out_dir = FIG_DIR / args.system
    out_dir.mkdir(parents=True, exist_ok=True)

    dist_hist(m, out_dir)
    tie_hist(m, out_dir)

    nodes = load_nodes(args)
    attrs = pd.read_csv(ROOT / "data/processed/substations/substation_attributes_clean.csv",
                        usecols=["utility", "substation_name", "util_lat", "util_lon"])
    subs = attrs.dropna(subset=["util_lat", "util_lon"])
    print(f"nodes for Voronoi: {len(nodes):,}   substations overlaid: {len(subs):,}")
    voronoi_figure(nodes, subs, args, out_dir)


if __name__ == "__main__":
    main()
