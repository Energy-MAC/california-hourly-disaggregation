"""Interactive coverage map: IOU substations vs. CATS nodes per CA county.

Answers "where does our substation data density differ from the test
system's node density?" — a Folium choropleth of the 58 CA counties (TIGER
2022), colored by nodes-per-substation ratio, with a hover tooltip giving the
raw counts. Substation counts come from substation_attributes_clean.csv
(spatially joined to counties, same method as the ReEDS county assignment)
and count ONLY real substations with a real coordinate — ReEDS synthetic
substations (Del Norte/Lassen/Modoc/Siskiyou, placed at county centroids for
the nodal mapping) are NOT in that file and are NOT counted here. Those 4
counties are marked on the map with a dashed red border and a
"synthetic substation only" tooltip note so they are never mistaken for a
real substation's county. CATS node counts use the same candidate-node
filters as map_loads_to_nodes.py: Type='Substation', non-IMPORT, AND nonzero
load in Demand_data.csv (imports filter_zero_demand from that module, so the
two scripts can never drift apart).

Generalizable to any node system via --nodes/--id-col/--lat-col/--lon-col/
--filter/--demand-file (same conventions as map_loads_to_nodes.py).

CLI parameters:
  --nodes    node CSV (default data/raw/CATS/CATS_buses.csv)
  --system   label used in the output filename (default: nodes file stem)
  --id-col/--lat-col/--lon-col   node columns (default bus_i/Lat/Lon)
  --filter   repeatable "col=value" candidate filter (see map_loads_to_nodes.py)
  --no-default-filters
  --demand-file/--demand-col-prefix/--no-demand-filter   zero-demand candidate
                  filter (see map_loads_to_nodes.py)

Outputs (data/figures/load_projection/coverage/):
  coverage_map_{system}.html   interactive county choropleth
  coverage_by_county_{system}.csv   the underlying per-county table

Usage:
  python scripts/load_projection/nodal/plot_coverage_map.py
  python scripts/load_projection/nodal/plot_coverage_map.py --system CATS
"""

import argparse
import sys
from pathlib import Path

import branca.colormap as cm
import folium
import geopandas as gpd
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/load_projection/nodal"))
from map_loads_to_nodes import SYNTHETIC_COUNTIES, filter_zero_demand  # noqa: E402

ATTR_FILE = ROOT / "data/processed/substations/substation_attributes_clean.csv"
COUNTY_SHP = ROOT / ("data/raw/reeds/ReEDS-2.0/inputs/shapefiles/cache/"
                     "tl_2022_us_county/tl_2022_us_county.shp")
OUT_DIR = ROOT / "data/figures/load_projection/coverage"


def load_counties() -> gpd.GeoDataFrame:
    g = gpd.read_file(COUNTY_SHP)
    return g[g.STATEFP == "06"][["NAME", "geometry"]].to_crs(4326).rename(
        columns={"NAME": "county_name"})


def load_nodes(args) -> gpd.GeoDataFrame:
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
    return gpd.GeoDataFrame(
        nodes, geometry=gpd.points_from_xy(nodes[args.lon_col], nodes[args.lat_col]),
        crs=4326)


def load_substations() -> gpd.GeoDataFrame:
    a = pd.read_csv(ATTR_FILE, usecols=["utility", "substation_name",
                                        "util_lat", "util_lon"]).dropna(
        subset=["util_lat", "util_lon"])
    return gpd.GeoDataFrame(
        a, geometry=gpd.points_from_xy(a.util_lon, a.util_lat), crs=4326)


def build_table(counties, subs, nodes) -> pd.DataFrame:
    s_j = gpd.sjoin(subs, counties, predicate="within")
    n_j = gpd.sjoin(nodes, counties, predicate="within")
    tbl = counties[["county_name"]].copy()
    tbl["n_substations"] = tbl.county_name.map(
        s_j.groupby("county_name").size()).fillna(0).astype(int)
    tbl["n_nodes"] = tbl.county_name.map(
        n_j.groupby("county_name").size()).fillna(0).astype(int)
    tbl["nodes_per_sub"] = tbl.n_nodes / tbl.n_substations.replace(0, pd.NA)
    synthetic_counties = set(SYNTHETIC_COUNTIES.values())
    tbl["synthetic_only"] = tbl.county_name.isin(synthetic_counties)
    tbl["note"] = tbl.synthetic_only.map(
        {True: "synthetic substation only (no real IOU substation)", False: ""})
    return tbl


def build_map(counties: gpd.GeoDataFrame, tbl: pd.DataFrame) -> folium.Map:
    gdf = counties.merge(tbl, on="county_name")
    # counties with 0 substations get their own bucket (ratio undefined, not "low")
    has_subs = gdf.nodes_per_sub.notna()
    vmax = gdf.loc[has_subs, "nodes_per_sub"].quantile(0.95)
    colormap = cm.LinearColormap(
        ["#4575b4", "#ffffbf", "#d73027"], vmin=0, vmax=vmax,
        caption="CATS nodes per IOU substation (capped at 95th pct)")

    m = folium.Map(location=[37.2, -119.5], zoom_start=6, tiles="cartodbpositron")

    def style(feature):
        v = feature["properties"]["nodes_per_sub"]
        base = {"fillColor": "#999999" if v is None else colormap(v),
                "color": "#333333", "weight": 0.8,
                "fillOpacity": 0.75 if v is not None else 0.35}
        if feature["properties"]["synthetic_only"]:
            base.update(color="#e31a1c", weight=2.5, dashArray="6,4")
        return base

    folium.GeoJson(
        gdf.to_json(), style_function=style,
        tooltip=folium.GeoJsonTooltip(
            fields=["county_name", "n_substations", "n_nodes", "nodes_per_sub", "note"],
            aliases=["County:", "IOU substations:", "CATS nodes:", "Nodes/sub:", ""],
            localize=True),
    ).add_to(m)
    colormap.add_to(m)
    legend_note = folium.Element(
        '<div style="position:fixed;bottom:20px;left:20px;z-index:9999;'
        'background:white;padding:6px 10px;border:1px solid #999;'
        'font-size:12px;">Grey = no IOU substations in this county<br>'
        'Dashed red border = synthetic substation only '
        '(no real IOU substation; load placed at county centroid for the '
        'nodal mapping, not counted as a substation here)</div>')
    m.get_root().html.add_child(legend_note)
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--nodes", default=str(ROOT / "data/raw/CATS/CATS_buses.csv"))
    ap.add_argument("--system", default=None)
    ap.add_argument("--id-col", default="bus_i")
    ap.add_argument("--lat-col", default="Lat")
    ap.add_argument("--lon-col", default="Lon")
    ap.add_argument("--filter", action="append", default=[])
    ap.add_argument("--no-default-filters", action="store_true")
    ap.add_argument("--demand-file", default=str(ROOT / "data/raw/CATS/Demand_data.csv"))
    ap.add_argument("--demand-col-prefix", default="Demand_MW_z")
    ap.add_argument("--no-demand-filter", action="store_true")
    args = ap.parse_args()
    system = args.system or Path(args.nodes).stem

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    counties = load_counties()
    subs = load_substations()
    nodes = load_nodes(args)
    print(f"counties: {len(counties)}   substations (with coords): {len(subs):,}   "
          f"candidate nodes: {len(nodes):,}")

    tbl = build_table(counties, subs, nodes)
    csv_path = OUT_DIR / f"coverage_by_county_{system}.csv"
    tbl.sort_values("n_substations", ascending=False).round(3).to_csv(csv_path, index=False)
    print(f"counties with substations but 0 nodes: "
          f"{((tbl.n_substations > 0) & (tbl.n_nodes == 0)).sum()}")
    print(f"counties with 0 substations: {(tbl.n_substations == 0).sum()}")

    m = build_map(counties, tbl)
    html_path = OUT_DIR / f"coverage_map_{system}.html"
    m.save(str(html_path))
    print(f"wrote {csv_path.relative_to(ROOT)}")
    print(f"wrote {html_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
