"""Map projected substation loads onto the nodes of an external test system.

Builds a (substation -> node) assignment by nearest-node matching: each
projected substation's load goes to its closest candidate node; nodes within
--tie-tol-km of the minimum distance share it equally; a node accumulates all
substations assigned to it (many-to-one). Totals are conserved for every
substation with coordinates. Optionally applies the mapping to projection
outputs (Approach 1 weights-based or Approach 2 stochastic) to produce nodal
loads. Generalizable to any node dataset with id + lat/lon columns (CATS is
the default).

ReEDS synthetic substations (SYNTHETIC_DEL_NORTE/LASSEN/MODOC/SISKIYOU —
counties with no utility substations) have no real location, so nearest-node
assignment does not apply to them: "closest" only makes sense for a load with
an actual coordinate. Instead each synthetic substation's load is split
EQUALLY across every candidate node whose location falls inside that county's
polygon (point-in-polygon test, TIGER 2022 shapefile) — e.g. Siskiyou's 37
in-county CATS buses each get 1/37 of SYNTHETIC_SISKIYOU's load. If a county
contains zero candidate nodes (Del Norte, on CATS: 0 in-county buses), there
is nothing to distribute across, so the load falls back to the single nearest
node to the county centroid (printed as a warning — this is the one place
distance-based assignment still applies to a synthetic substation).
This only matters for the weights-based approach (Approach 1); the
stochastic approach (Approach 2) covers only the PGE/SCE/SDGE portion of
CAISO and has no synthetic substations.

Candidate nodes and CATS AddedNodes: by default only Type='Substation',
non-IMPORT buses receive load (CATS's 5,699 'AddedNode' line-routing points
and 30 import interfaces are excluded). To also allow AddedNodes, pass
--no-default-filters (add e.g. --filter "Type=AddedNode" to allow ONLY them);
any "col=value" combination on the node file's columns works.

Applying weights-based (Approach 1) projections: any long CSV/parquet with
utility + substation_name columns works, e.g.
  --apply data/processed/load_projection/projections/reeds_projected__max_load__monthhour/substation_annual_load.csv
  --apply data/processed/load_projection/projections/iepr__*__monthhour/substation_annual_load.csv
  --apply data/processed/load_projection/projections/reeds_projected__max_load__monthhour/substation_monthly_load.parquet
Load columns are auto-detected by *_mw/*_mwh suffix (override --value-cols);
all other columns become group keys. Approach 2 hourly draw parquets (wide,
'utility|name' columns) are also supported.

TODO (known limitations):
- Assignment ignores node voltage — a distribution substation can land on a
  230/500 kV bus if it is nearest. Possible refinement: prefer the
  lowest-kV candidate within the tie tolerance.
- 22 substations (21 SCE, 1 PGE — see unmapped_substations.csv) have no
  coordinates in substation_attributes_clean.csv and are excluded from every
  mapping (0.17% of fleet load). Names include Visalia, Safari, Costa Mesa,
  Fair Oaks — real, presumably locatable sites. Finding and adding their
  coordinates should be straightforward and would let --unmapped drop go to
  zero substations rather than being needed at all.

CLI parameters:
  --nodes         node CSV (default data/raw/CATS/CATS_buses.csv)
  --system        output folder name (default: nodes file stem)
  --id-col/--lat-col/--lon-col   node columns (default bus_i/Lat/Lon)
  --filter        repeatable "col=value" candidate filter (see above)
  --no-default-filters           disable the Type/Import defaults
  --tie-tol-km    nodes within this of the min distance share equally (0.25)
  --max-dist-km   flag (not drop) assignments farther than this (10)
  --unmapped      drop (default) | renormalize — what to do with the load of
                  the 22 substations that have no coordinates (0.17% of
                  fleet load; TODO below):
                    drop: that load is not assigned anywhere; the printed
                      out/in ratio (e.g. 0.9984) reports the resulting shortfall
                    renormalize: every mapped node's load is scaled up by a
                      single global factor (out-of-input total, computed once
                      across the whole --apply file, not per year/draw) so the
                      applied total exactly equals the input total. This
                      redistributes the missing 0.17% uniformly across every
                      node rather than placing it near the substations it
                      actually came from
  --value-cols    comma-separated load columns for --apply (default: auto)
  --apply         repeatable path to a projection output (CSV or parquet)

Outputs (data/processed/load_projection/nodal/{system}/):
  substation_node_map.csv    utility, substation_name, node, share, dist_km,
                             n_tied, is_synthetic
  unmapped_substations.csv   substations without coordinates
  nodal__{input stem}.csv/.parquet   per --apply input

Usage:
  python scripts/load_projection/map_loads_to_nodes.py --system CATS
  python scripts/load_projection/map_loads_to_nodes.py --system CATS \\
      --apply data/processed/load_projection/projections/stochastic__eia930__normal__Fcal__native/substation_annual_mwh.csv \\
      --apply data/processed/load_projection/projections/reeds_projected__max_load__monthhour/substation_annual_load.csv
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
ATTR_FILE = ROOT / "data/processed/substations/substation_attributes_clean.csv"
PROFILE_FILE = ROOT / "data/processed/substations/substation_load_profiles_clean.csv"
COUNTY_SHP = ROOT / ("data/raw/reeds/ReEDS-2.0/inputs/shapefiles/cache/"
                     "tl_2022_us_county/tl_2022_us_county.shp")
OUT_ROOT = ROOT / "data/processed/load_projection/nodal"

SYNTHETIC_COUNTIES = {  # substation_name -> TIGER county name (CA)
    "SYNTHETIC_DEL_NORTE": "Del Norte",
    "SYNTHETIC_LASSEN": "Lassen",
    "SYNTHETIC_MODOC": "Modoc",
    "SYNTHETIC_SISKIYOU": "Siskiyou",
}


def haversine_km(lat1, lon1, lat2, lon2):
    """Vectorized great-circle distance; broadcasts (n,1) vs (m,) to (n,m)."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))


def load_substations() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(real substations with coords, substations without coords).

    ReEDS synthetic substations are handled separately (assign_synthetic) —
    they have no real coordinate, so they never enter the nearest-node path.
    """
    prof = pd.read_csv(PROFILE_FILE, usecols=["utility", "substation_name"])
    subs = prof.drop_duplicates().reset_index(drop=True)
    attrs = pd.read_csv(ATTR_FILE, usecols=["utility", "substation_name",
                                            "util_lat", "util_lon"])
    attrs["utility"] = attrs.utility.str.lower()
    m = subs.merge(attrs, on=["utility", "substation_name"], how="left")
    return m[m.util_lat.notna()].copy(), m[m.util_lat.isna()].copy()


def assign_synthetic(nodes: pd.DataFrame, args) -> pd.DataFrame:
    """ReEDS synthetic substations (no real coordinate): split each one's
    load equally across every candidate node located inside its county
    (point-in-polygon). Falls back to the single node nearest the county
    centroid only if the county contains zero candidate nodes."""
    import geopandas as gpd
    node_gdf = gpd.GeoDataFrame(
        nodes, geometry=gpd.points_from_xy(nodes[args.lon_col], nodes[args.lat_col]),
        crs=4326)
    g = gpd.read_file(COUNTY_SHP)
    g = g[(g.STATEFP == "06") & g.NAME.isin(SYNTHETIC_COUNTIES.values())].to_crs(4326)
    within = gpd.sjoin(node_gdf, g[["NAME", "geometry"]], predicate="within")

    rows = []
    for sub_name, county in SYNTHETIC_COUNTIES.items():
        in_county = within[within.NAME == county]
        if len(in_county):
            for node_id in in_county[args.id_col]:
                rows.append(("synthetic", sub_name, node_id, 1.0 / len(in_county),
                            np.nan, len(in_county), True, "county_equal_split"))
        else:
            cent = g.loc[g.NAME == county, "geometry"].to_crs(3310).centroid.to_crs(4326).iloc[0]
            d = haversine_km(cent.y, cent.x, nodes[args.lat_col].values,
                             nodes[args.lon_col].values)
            j = int(np.argmin(d))
            print(f"WARNING: {county} county has zero candidate nodes; "
                  f"{sub_name} falls back to nearest node to county centroid "
                  f"({nodes[args.id_col].iat[j]}, {d[j]:.1f} km)")
            rows.append(("synthetic", sub_name, nodes[args.id_col].iat[j], 1.0,
                        d[j], 1, True, "centroid_fallback"))
    return pd.DataFrame(rows, columns=["utility", "substation_name", "node", "share",
                                       "dist_km", "n_tied", "is_synthetic",
                                       "assignment_method"])


def load_nodes(args) -> pd.DataFrame:
    nodes = pd.read_csv(args.nodes)
    for c in nodes.columns:  # strip the quote-and-space artifacts CATS carries
        if pd.api.types.is_string_dtype(nodes[c]):
            nodes[c] = nodes[c].str.strip().str.strip("'").str.strip()
    n_all = len(nodes)
    filters = [f.split("=", 1) for f in args.filter]
    if not args.no_default_filters:
        if "Type" in nodes.columns and not any(c == "Type" for c, _ in filters):
            nodes = nodes[nodes.Type == "Substation"]
        if "Import" in nodes.columns:
            nodes = nodes[nodes.Import != "IMPORT"]
    for col, val in filters:
        nodes = nodes[nodes[col].astype(str) == val]
    print(f"nodes: {len(nodes):,} candidates of {n_all:,} in {Path(args.nodes).name}")
    return nodes.reset_index(drop=True)


def build_mapping(subs: pd.DataFrame, nodes: pd.DataFrame, args) -> pd.DataFrame:
    """Real substations: nearest-node with tie-sharing (has a real location,
    so distance is meaningful). Synthetic substations: handled separately by
    assign_synthetic — equal split across nodes located inside their county,
    never a distance-based "closest" call (see module docstring)."""
    d = haversine_km(subs.util_lat.values[:, None], subs.util_lon.values[:, None],
                     nodes[args.lat_col].values, nodes[args.lon_col].values)
    dmin = d.min(axis=1)
    rows = []
    node_ids = nodes[args.id_col].values
    for i in range(len(subs)):
        tied = np.flatnonzero(d[i] <= dmin[i] + args.tie_tol_km)
        for j in tied:
            rows.append((subs.utility.iat[i], subs.substation_name.iat[i],
                         node_ids[j], 1.0 / len(tied), d[i, j], len(tied),
                         False, "nearest"))
    real = pd.DataFrame(rows, columns=["utility", "substation_name", "node", "share",
                                       "dist_km", "n_tied", "is_synthetic",
                                       "assignment_method"])
    synth = assign_synthetic(nodes, args)
    m = pd.concat([real, synth], ignore_index=True)

    print(f"mapping: {real.groupby(['utility', 'substation_name']).ngroups:,} real "
          f"substations (nearest-node) + {synth.groupby('substation_name').ngroups} "
          f"synthetic (county equal-split) -> {m.node.nunique():,} nodes; "
          f"ties: {(real.n_tied > 1).sum()} rows across "
          f"{real[real.n_tied > 1].groupby(['utility', 'substation_name']).ngroups} "
          f"real substations")
    print(f"distance to assigned node [km] (real only; synthetic county "
          f"equal-split has no single distance): median {real.dist_km.median():.2f}, "
          f"p95 {real.dist_km.quantile(0.95):.2f}, max {real.dist_km.max():.2f}")
    far = real[real.dist_km > args.max_dist_km]
    if len(far):
        print(f"WARNING: {far.groupby(['utility', 'substation_name']).ngroups} real "
              f"substations assigned farther than {args.max_dist_km} km (still included):")
        print(far.nlargest(5, "dist_km")[["utility", "substation_name", "node",
                                          "dist_km"]].to_string(index=False))
    return m


def detect_value_cols(df: pd.DataFrame, args) -> list[str]:
    if args.value_cols:
        return args.value_cols.split(",")
    byname = [c for c in df.columns
              if c.lower().endswith(("_mw", "_mwh")) or c.lower() in ("mw", "mwh")]
    if byname:
        return byname
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def apply_long(mapping, df, value_cols, renormalize):
    """Long table with utility + substation_name -> grouped nodal table."""
    df = df.copy()
    df["utility"] = df.utility.astype(str).str.lower()
    tot_all = df[value_cols].sum()
    j = df.merge(mapping[["utility", "substation_name", "node", "share"]],
                 on=["utility", "substation_name"], how="inner")
    group_cols = ["node"] + [c for c in df.columns
                             if c not in value_cols + ["utility", "substation_name"]]
    for c in value_cols:
        j[c] = j[c] * j["share"]
    out = j.groupby(group_cols, as_index=False)[value_cols].sum()
    if renormalize:
        for c in value_cols:
            if out[c].sum() > 0:
                out[c] *= tot_all[c] / out[c].sum()
    return out, (out[value_cols].sum() / tot_all).round(6).to_dict()


def apply_wide(mapping, df, renormalize):
    """Wide hourly matrix (columns 'utility|name') -> wide nodal matrix."""
    key = mapping.assign(col=mapping.utility + "|" + mapping.substation_name)
    key = key[key.col.isin(df.columns)]
    share = key.pivot_table(index="col", columns="node", values="share",
                            fill_value=0.0)
    out = pd.DataFrame(df[share.index].fillna(0.0).values @ share.values,
                       index=df.index, columns=share.columns.astype(str))
    tot_all = np.nansum(df.values)
    if renormalize and out.values.sum() > 0:
        out *= tot_all / out.values.sum()
    return out, round(out.values.sum() / tot_all, 6)


def apply_projection(mapping, path: Path, out_dir: Path, args) -> None:
    df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    renorm = args.unmapped == "renormalize"
    # prefix with the run-tag folder so same-named files from different runs
    # (e.g. every Approach 1 substation_annual_load.csv) cannot collide
    stem = f"{path.parent.name}__{path.stem}"
    if "substation_name" in df.columns:  # long format (CSV or parquet)
        value_cols = detect_value_cols(df, args)
        out, cons = apply_long(mapping, df, value_cols, renorm)
        out_path = out_dir / f"nodal__{stem}{path.suffix if path.suffix == '.parquet' else '.csv'}"
        if out_path.suffix == ".parquet":
            out.to_parquet(out_path)
        else:
            out.to_csv(out_path, index=False)
        print(f"applied {path.name}: {len(out):,} nodal rows, "
              f"{out.node.nunique():,} nodes; total out/in per column: {cons}")
    else:  # wide hourly matrix (Approach 2 draws)
        out, cons = apply_wide(mapping, df, renorm)
        out_path = out_dir / f"nodal__{stem}.parquet"
        out.to_parquet(out_path)
        print(f"applied {path.name}: {out.shape[0]:,} hours x {out.shape[1]:,} nodes; "
              f"total out/in: {cons}")
    print(f"wrote {out_path.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--nodes", default=str(ROOT / "data/raw/CATS/CATS_buses.csv"))
    ap.add_argument("--system", default=None)
    ap.add_argument("--id-col", default="bus_i")
    ap.add_argument("--lat-col", default="Lat")
    ap.add_argument("--lon-col", default="Lon")
    ap.add_argument("--filter", action="append", default=[],
                    help='repeatable "col=value" candidate-node filter')
    ap.add_argument("--no-default-filters", action="store_true")
    ap.add_argument("--tie-tol-km", type=float, default=0.25)
    ap.add_argument("--max-dist-km", type=float, default=10.0)
    ap.add_argument("--unmapped", choices=["drop", "renormalize"], default="drop")
    ap.add_argument("--value-cols", default=None)
    ap.add_argument("--apply", action="append", default=[])
    args = ap.parse_args()

    system = args.system or Path(args.nodes).stem
    out_dir = OUT_ROOT / system
    out_dir.mkdir(parents=True, exist_ok=True)

    subs, unmapped = load_substations()
    nodes = load_nodes(args)
    print(f"substations: {len(subs):,} real with coordinates, "
          f"{len(SYNTHETIC_COUNTIES)} synthetic (county equal-split), "
          f"{len(unmapped)} without coordinates (policy: {args.unmapped})")
    mapping = build_mapping(subs, nodes, args)

    mapping.round(6).to_csv(out_dir / "substation_node_map.csv", index=False)
    unmapped[["utility", "substation_name"]].to_csv(
        out_dir / "unmapped_substations.csv", index=False)
    print(f"wrote {(out_dir / 'substation_node_map.csv').relative_to(ROOT)}")

    for p in args.apply:
        apply_projection(mapping, Path(p), out_dir, args)


if __name__ == "__main__":
    main()
