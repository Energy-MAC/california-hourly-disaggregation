"""Turn an external seasonal substation load file into within-county weight tables.

Takes a file of one summer and one winter load value per substation, keyed by
name -- the utility's own substation name where known, otherwise a
`ca_substations_2022.csv` name -- and writes the weight tables the county-first
allocation's within-county split consumes. The allocation itself is unchanged:
this is a new value of the `county_weights` axis, not a new `Approach N`, so no
run tag and no citation id moves.

Input contract (one row per substation; only the first three are required):

    name,utility,summer_load,winter_load,lat,lon,voltage_kv
    HOLLISTER,pge,56.63,41.20,36.8525,-121.4016,115
    Jenney,,12.19,9.87,37.7724,-122.2429,115

`lat`/`lon` are the valuable optional columns: they place a row on a bus without
its name needing to match the reference table, and they disambiguate every name
several utilities share -- measured, 47 of the 48 shared names have candidates at
least 65 km apart (median 558), so proximity settles them with a wide margin.
That makes `utility` optional; it is still a useful cross-check, and is required
only for a shared name whose candidates have no coordinates. `voltage_kv` enables
the same voltage-restricted placement the `voltres` map variant uses.

Name resolution is a strict cascade; each row takes the FIRST rule that fires:
a profiled utility substation (by `(utility, name)`, else by name when
unambiguous, else by proximity among the candidates), then the inverted
`basinSourceDictionary`, then the row's own coordinates, then a
`ca_substations_2022.csv` record on its precomputed bus, then unresolved.
Ambiguous dictionary inversions raise rather than guess. See
`src/load_projection/external_loads.py` for the reasoning.

Coordinate placement reuses `map_loads_to_nodes.build_mapping()`, the production
nearest-node function, so tie shares and voltage restriction behave exactly as
they do for the nodal maps -- there is no second nearest-node search here.

`--shape utility` multiplies each substation's seasonal level by its own utility
month-hour envelope, normalized to mean 1 within the half-year block, so the
level is preserved as the block mean while the diurnal pattern is borrowed.
`--shape flat` leaves the shape at 1, which is what a seasonal-only input
literally contains -- kept because it is the honest degenerate baseline, and
because feeding it to the stochastic weight source is a documented null result
(the per-cell weights come back ~equal to the input levels).

Substations named with a level but no utility envelope take their county's mean
normalized shape, falling back to the statewide fleet mean. Counted in the
manifest, never silent.

The load values may be in arbitrary units: every consumption site normalizes, so
only the cross-substation pattern and the summer:winter ratio survive. `--units`
records which it is and the emitted column is called `weight`, never `load_mw`.

CLI parameters:
  --input        the external seasonal load CSV (required)
  --units        relative (default) | mw -- provenance only; recorded in the
                 manifest so these numbers are never later read as megawatts
  --shape-file   optional per-utility hourly curve (utility, season, hour_pst,
                 shape). CAVEAT, measured: a shape shared by all of a utility's
                 substations leaves the ranking WITHIN that utility unchanged
                 (Spearman(h10,h18) within PGE = 1.000000) and gives statewide
                 per-bus share CV 0.104 vs 0.187 for each substation's own
                 envelope. Useful mainly as a better fallback than the county
                 mean for rows with no envelope of their own.
  --name-coord-tol-km   a shared name is resolved to the nearest candidate only
                 within this distance, and only if the runner-up is at least
                 twice as far (default 25 km)
  --max-name-dist-km    demote a name match whose location is further than this
                 from the row's own coordinates to coordinate placement
                 (default off -- the name is trusted)
  --tie-tol-km / --voltage-mode / --voltage-max-dist-km
                 passed through to build_mapping for coordinate placement
  --shape        utility | flat (default utility)
  --shape-col    avg_load (default; the envelope midpoint, matching the external
                 level's central-tendency meaning) | max_load (sensitivity; what
                 the envelope weight source itself uses)
  --map          nodal map for route-1 substations: prox | voltres | nameprox |
                 catch | namecatch (default prox). Route-2 rows are placed
                 spatially and are map-independent.
  --tag          output folder name (default derived from --shape and --map)
  --out          parent directory (default data/processed/load_projection/external_loads)

Outputs (<out>/<tag>/):
  external_substation_weights.csv  utility, substation_name, month, hour_pst, weight
  external_node_weights.csv        node, month, hour_pst, weight  (spatial rows)
  resolution_report.csv            every input row: route, keys, node, distances,
                                   status -- nothing is dropped silently
  manifest.json                    provenance: shape mode, map, reference tables,
                                   per-route counts, input md5

Usage:
  python scripts/load_projection/external_loads/build_external_weights.py \\
      --input my_seasonal_loads.csv --shape utility
  python scripts/load_projection/external_loads/build_external_weights.py \\
      --input my_seasonal_loads.csv --shape flat --tag external_flat
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from argparse import Namespace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/data/substations"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/nodal"))

import numpy as np  # noqa: E402

from build_cec_name_dictionary import norm  # noqa: E402
from map_loads_to_nodes import band_to_cats_class, build_mapping, load_nodes  # noqa: E402
from load_projection import external_loads as ext  # noqa: E402
from load_projection.weights import load_profiles  # noqa: E402

PROCESSED = ROOT / "data/processed"
PROFILES = PROCESSED / "substations/substation_load_profiles_clean.csv"
BASIN_DICT = ROOT / "data/basinSourceDictionary.csv"
CEC_2022 = PROCESSED / "substation_misc/ca_substations_2022.csv"
ATTRS = PROCESSED / "substations/substation_attributes_clean.csv"
CATS_BUSES = ROOT / "data/raw/CATS/CATS_buses.csv"
CATS_DEMAND = ROOT / "data/raw/CATS/Demand_data.csv"
NODAL_DIR = PROCESSED / "load_projection/nodal/CATS"
OUT_PARENT = PROCESSED / "load_projection/external_loads"

MAP_FILES = {
    "prox": "substation_node_map.csv",
    "voltres": "substation_node_map__voltrestrict.csv",
    "nameprox": "substation_node_map__nameprox.csv",
    "catch": "substation_node_map__catchment.csv",
    "namecatch": "substation_node_map__namecatchment.csv",
}


def md5(path: Path) -> str:
    h = hashlib.md5()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


def rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def node_county_table() -> pd.DataFrame:
    """`node -> county_name` from the production point-in-polygon join.

    Imported from the rescaler rather than re-derived: `candidate_buses()` is the
    one definition of which buses exist and which county each sits in.
    """
    import rescale_genx_demand as RS
    pool = RS.candidate_buses()
    return pool[["node", "county_name"]].drop_duplicates()


def substation_county_table(map_path: Path, node_county: pd.DataFrame) -> pd.DataFrame:
    """`(utility, substation_name) -> county_name`, via the substation's own bus.

    A substation tied across several buses takes the county of its largest-share
    bus, which is also the bus carrying most of its load.
    """
    m = pd.read_csv(map_path, dtype={"node": str})
    m["utility"] = m.utility.astype(str).str.lower()
    m = m.sort_values("share", ascending=False).drop_duplicates(
        ["utility", "substation_name"], keep="first")
    j = m.merge(node_county, on="node", how="left")
    return j[["utility", "substation_name", "county_name"]]


def substation_coords() -> dict:
    """`(utility, substation_name) -> (lat, lon)` for the profiled fleet.

    Powers the proximity tie-break for shared names and `name_dist_km`, the
    distance between a row's own coordinates and the substation its NAME matched.
    """
    a = pd.read_csv(ATTRS, usecols=["utility", "substation_name",
                                    "util_lat", "util_lon"])
    a["utility"] = a.utility.astype(str).str.lower()
    return {(u, s): (float(la), float(lo))
            for u, s, la, lo in zip(a.utility, a.substation_name,
                                    a.util_lat, a.util_lon)
            if pd.notna(la) and pd.notna(lo)}


def place_by_coordinates(rows: pd.DataFrame, args) -> pd.DataFrame:
    """Assign coordinate-supplied rows to buses with the PRODUCTION function.

    Calls `map_loads_to_nodes.load_nodes` + `build_mapping` on the rows' own
    coordinates, with that module's own defaults, so these rows land on the same
    candidate bus set -- and split across ties by the same rule -- as the nodal
    maps themselves. Returns `name, node, share, dist_km, n_tied,
    assignment_method`.
    """
    nargs = Namespace(
        nodes=str(CATS_BUSES), id_col="bus_i", lat_col="Lat", lon_col="Lon",
        filter=[], no_default_filters=False,
        demand_file=str(CATS_DEMAND), demand_col_prefix="Demand_MW_z",
        no_demand_filter=False, tie_tol_km=args.tie_tol_km,
        voltage_mode=args.voltage_mode, voltage_col="kV",
        max_dist_km=10.0,
        voltage_max_dist_km=(args.voltage_max_dist_km
                             if args.voltage_max_dist_km is not None else 10.0))
    nodes = load_nodes(nargs)

    # Restrict to the buses the DELIVERABLE can allocate over. The two pools are
    # built differently -- `load_nodes` filters on CATS_buses plus the (deprecated)
    # raw Demand_data, while the deliverable additionally requires a county
    # polygon and may exclude AddedNodes -- so a row placed outside the
    # deliverable's pool would have its weight silently dropped at allocation
    # time. Bus 2408, on the Arizona border near Winterhaven, is exactly that
    # case: CATS loads it, but it falls outside every California county polygon.
    import rescale_genx_demand as RS
    pool = set(RS.candidate_pool(
        Namespace(map=args.map, system=args.system, pool=args.pool,
                  bus_types=args.bus_types)).node.astype(str))
    before = len(nodes)
    nodes = nodes[nodes[nargs.id_col].astype(str).isin(pool)].reset_index(drop=True)
    if len(nodes) != before:
        print(f"  placement pool: {len(nodes):,} of {before:,} CATS candidates are "
              f"allocatable (pool={args.pool}, bus-types={args.bus_types}); "
              f"{before - len(nodes):,} excluded, so nothing can be placed there")
    if nodes.empty:
        raise SystemExit("no allocatable bus left to place coordinate rows on")

    subs = pd.DataFrame({
        "utility": "external",
        # the REAL name, because build_mapping prints this column in its own
        # diagnostics and a row_id there is unreadable. Names may repeat, so
        # row_id is recovered from the output grouping below, not by joining.
        "substation_name": rows.name.to_numpy(),
        "_row_id": rows.row_id.to_numpy(),
        "util_lat": rows.lat.to_numpy(float),
        "util_lon": rows.lon.to_numpy(float),
    })
    if args.voltage_mode == "restrict":
        kv = pd.to_numeric(rows.get("voltage_kv"), errors="coerce")
        subs["highside_kv"] = kv.to_numpy(float)
        subs["sub_kv_class"] = [band_to_cats_class(v) for v in subs.highside_kv]
    m = build_mapping(subs, nodes, nargs)
    # build_mapping always appends the four SYNTHETIC ReEDS substations
    # (Del Norte / Lassen / Modoc / Siskiyou) because its own callers want them.
    # They are nothing to do with an external input, so drop them here -- leaving
    # them in would silently invent four weighted substations.
    m = m[~m.is_synthetic.astype(bool)].reset_index(drop=True)
    # build_mapping emits the real substations in input order, exactly `n_tied`
    # consecutive rows per input row (one per tied bus), so walking it in groups
    # recovers which input row each belongs to without needing a unique name.
    rids, i, j = [], 0, 0
    while i < len(m):
        k = int(m.n_tied.iat[i])
        rids.extend([int(subs._row_id.iat[j])] * k)
        i += k
        j += 1
    if j != len(subs) or len(rids) != len(m):
        raise RuntimeError(
            f"could not match build_mapping's {len(m)} output row(s) back to "
            f"{len(subs)} input row(s) (matched {j}); its row order or n_tied "
            f"semantics must have changed")
    m = m.assign(row_id=rids)
    return m[["row_id", "substation_name", "node", "share", "dist_km", "n_tied",
              "assignment_method"]]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", required=True, help="external seasonal load CSV")
    ap.add_argument("--shape", choices=list(ext.SHAPE_MODES), default="utility")
    ap.add_argument("--shape-col", choices=list(ext.SHAPE_COLS), default="avg_load")
    ap.add_argument("--map", choices=list(MAP_FILES), default="prox")
    ap.add_argument("--system", default="CATS",
                    help="nodal artifact namespace to read the map from, i.e. "
                         "data/processed/load_projection/nodal/<system>/. MUST "
                         "match the --system the deliverable will run with, or "
                         "the placement pool is computed against a different "
                         "mapping vintage than the allocation (default CATS)")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--out", default=str(OUT_PARENT))
    ap.add_argument("--units", choices=["relative", "mw"], default="relative",
                    help="provenance only -- the scale cancels at every "
                         "consumption site. Recorded in the manifest so these "
                         "numbers are never later read as megawatts")
    ap.add_argument("--shape-file", default=None,
                    help="optional per-utility hourly curve (utility, season, "
                         "hour_pst, shape). MEASURED CAVEAT: a shape shared by "
                         "all of a utility's substations leaves the ranking "
                         "WITHIN that utility unchanged (Spearman 1.000000) and "
                         "gives share CV 0.104 vs 0.187 for each substation's "
                         "own envelope -- it buys between-utility reordering "
                         "only. Most useful as a better fallback than the "
                         "county mean for rows with no envelope")
    ap.add_argument("--name-coord-tol-km", type=float, default=25.0,
                    help="a shared name resolves to its nearest candidate only "
                         "within this distance, and only if the runner-up is at "
                         "least twice as far (candidates are >=65 km apart in "
                         "every measured case)")
    ap.add_argument("--max-name-dist-km", type=float, default=None,
                    help="demote a name match further than this from the row's "
                         "own coordinates to coordinate placement "
                         "(default: trust the name)")
    ap.add_argument("--tie-tol-km", type=float, default=0.25,
                    help="coordinate placement: buses within this of the nearest "
                         "share the row equally (build_mapping's own default)")
    ap.add_argument("--voltage-mode", choices=["off", "restrict"], default="off",
                    help="coordinate placement: 'restrict' requires a matching "
                         "CATS voltage class, needing a voltage_kv column")
    ap.add_argument("--voltage-max-dist-km", type=float, default=None)
    ap.add_argument("--on-duplicate", choices=["sum", "error"], default="sum",
                    help="when two input rows RESOLVE to the same substation or "
                         "bus: 'sum' adds them (two measurements at one site) "
                         "and always reports; 'error' refuses. Distinct from "
                         "--on-duplicate-name, which is about the input file")
    ap.add_argument("--on-duplicate-name", choices=list(ext.DUPLICATE_NAME_MODES),
                    default="error",
                    help="when a NAME repeats in the input: 'error' (default) "
                         "refuses; 'keep' makes every row its own unit, which is "
                         "safe because rows landing on one bus are summed there "
                         "anyway; 'sum' additionally collapses same-name rows "
                         "within --colocate-km into one unit, adding their loads, "
                         "while leaving same-name rows at DIFFERENT locations "
                         "apart -- shared names across utilities sit 65 km or "
                         "more apart, so merging them blindly would move load")
    ap.add_argument("--pool", choices=["cats_loaded", "all"], default="cats_loaded",
                    help="which buses a coordinate-placed row may land on. Must "
                         "match the pool the deliverable will allocate over, or "
                         "its weight is dropped there (see --bus-types)")
    ap.add_argument("--bus-types", choices=["all", "substation"], default="all",
                    help="'substation' excludes every CATS AddedNode from the "
                         "placement pool. Match the deliverable's own "
                         "--bus-types, otherwise a row placed on an excluded bus "
                         "contributes nothing")
    ap.add_argument("--colocate-km", type=float, default=0.5,
                    help="with --on-duplicate-name sum: how close two same-name "
                         "rows must be to count as one site (default 0.5 km)")
    args = ap.parse_args()
    if args.shape == "file" and not args.shape_file:
        ap.error("--shape file needs --shape-file")

    tag = args.tag or f"external_{args.shape}__{args.map}"
    out_dir = Path(args.out) / tag
    map_path = NODAL_DIR / MAP_FILES[args.map]

    inp = ext.read_input(args.input, on_duplicate_name=args.on_duplicate_name,
                         colocate_km=args.colocate_km)
    rep_collapse = inp.attrs.get("collapse_report")
    if rep_collapse is not None and len(rep_collapse):
        merged = int((rep_collapse.rows - rep_collapse.units).sum())
        split = int((rep_collapse.units > 1).sum())
        print(f"  --on-duplicate-name sum: {len(rep_collapse):,} repeated name(s); "
              f"{merged:,} row(s) merged into co-located units, "
              f"{split:,} name(s) kept split across locations "
              f"(max spread {rep_collapse.max_km_within_name.max():,.1f} km)")
        nox = int(rep_collapse.rows_without_coordinates.sum())
        if nox:
            print(f"    {nox:,} duplicate row(s) had no coordinates and were "
                  f"summed together within their name")
    elif args.on_duplicate_name == "keep":
        ndup = int(inp.name.duplicated().sum())
        if ndup:
            print(f"  --on-duplicate-name keep: {ndup:,} repeated-name row(s) kept "
                  f"as separate units; any that land on one bus are summed there")
    if args.voltage_mode == "restrict" and not inp.get("voltage_kv", pd.Series()).notna().any():
        ap.error("--voltage-mode restrict needs a voltage_kv column with values")
    prof = load_profiles(PROFILES, args.shape_col)

    name_index, name_ambig, name_by_utility = ext.build_name_index(prof, norm)
    dict_lookup, dict_ambig = ext.invert_basin_dictionary(BASIN_DICT, name_index, norm)
    cec_index, cec_ambig = ext.build_cec_index(CEC_2022, norm)

    sub_coords = substation_coords()
    report = ext.resolve_names(inp, name_index, name_ambig, name_by_utility,
                              dict_lookup, dict_ambig, cec_index, norm,
                              sub_coords=sub_coords,
                              name_coord_tol_km=args.name_coord_tol_km,
                              max_name_dist_km=args.max_name_dist_km)

    # coordinate-placed rows get their bus from the production nearest-node
    # function, with tie shares, rather than a second search written here
    # both spatial routes go through the SAME production placement, so both
    # respect the allocatable-bus pool: rule 3 uses the row's own coordinates,
    # rule 4 the matched reference record's
    coord_rows = report[report.route.isin(
        {ext.ROUTE_COORD_SPATIAL, ext.ROUTE_CEC_SPATIAL})]
    spatial_assignment = None
    if len(coord_rows):
        src = inp.set_index("row_id").loc[coord_rows.row_id]
        own_lat = src.lat.to_numpy(float)
        own_lon = src.lon.to_numpy(float)
        ref_lat = coord_rows.ref_lat.to_numpy(float)
        ref_lon = coord_rows.ref_lon.to_numpy(float)
        rows = pd.DataFrame({
            "row_id": coord_rows.row_id.to_numpy(),
            "name": coord_rows.name.to_numpy(),
            "lat": np.where(np.isfinite(own_lat), own_lat, ref_lat),
            "lon": np.where(np.isfinite(own_lon), own_lon, ref_lon),
        })
        if "voltage_kv" in inp.columns:
            rows["voltage_kv"] = src.voltage_kv.to_numpy(float)
        bad = rows[~(np.isfinite(rows.lat) & np.isfinite(rows.lon))]
        if len(bad):
            raise SystemExit(
                f"{len(bad)} spatial row(s) have no usable coordinates, e.g. "
                f"{bad.name.head(3).tolist()} -- this is a bug in resolution, "
                f"which should not have routed them here")
        spatial_assignment = place_by_coordinates(rows, args)
        primary = (spatial_assignment.sort_values("share", ascending=False)
                   .drop_duplicates("row_id", keep="first")
                   .set_index("row_id"))
        report = report.set_index("row_id")
        report.loc[primary.index, "node"] = primary.node.astype(str)
        report.loc[primary.index, "dist_km"] = primary.dist_km.to_numpy()
        report = report.reset_index()

    # two input rows can normalize onto the SAME substation ("DRUM" and "Drum 1"
    # both reach DRUM through the dictionary; a trailing space does it too). The
    # weight tables sum them, which is right when they really are two
    # measurements at one site and wrong when it is a near-duplicate name -- so
    # it is always reported, and --on-duplicate error refuses outright.
    res = report[report.route != ext.ROUTE_UNRESOLVED]
    key = ["utility", "substation_name"]
    named = res[res.substation_name.notna()]
    dup_named = named.groupby(key).name.agg(list)  # names of the rows involved
    dup_named = dup_named[dup_named.map(len) > 1]
    dup_node = res[res.substation_name.isna()].groupby("node").name.agg(list)
    dup_node = dup_node[dup_node.map(len) > 1]
    duplicates = ([{"target": f"{u}/{sn}", "rows": v} for (u, sn), v in dup_named.items()]
                  + [{"target": f"node {n}", "rows": v} for n, v in dup_node.items()])
    if duplicates:
        print(f"  NOTE {len(duplicates)} resolved target(s) receive more than one "
              f"input row; their weights are SUMMED:")
        for d in duplicates[:5]:
            print(f"    {d['target']:28s} <- {d['rows']}")
        if args.on_duplicate == "error":
            raise SystemExit(
                f"{len(duplicates)} target(s) receive multiple input rows and "
                f"--on-duplicate error was given; dedupe the input or pass "
                f"--on-duplicate sum to add them together")

    node_county = node_county_table()
    sub_county = substation_county_table(map_path, node_county)
    shapes = ext.normalized_shapes(prof, args.shape_col)
    county_shape, fleet_shape = ext.fallback_shapes(shapes, sub_county)
    shape_file = ext.load_shape_file(args.shape_file) if args.shape_file else None

    sub_w, node_w, counts = ext.build_weight_tables(
        inp, report, shapes, county_shape, fleet_shape, sub_county, node_county,
        shape_mode=args.shape, shape_file=shape_file,
        spatial_assignment=spatial_assignment)

    out_dir.mkdir(parents=True, exist_ok=True)
    sub_w.round(6).to_csv(out_dir / "external_substation_weights.csv", index=False)
    node_w.round(6).to_csv(out_dir / "external_node_weights.csv", index=False)
    report.to_csv(out_dir / "resolution_report.csv", index=False)

    by_route = report.route.value_counts().to_dict()
    manifest = {
        "tag": tag,
        "shape_mode": args.shape,
        "shape_col": args.shape_col,
        "shape_file": (rel(Path(args.shape_file)) if args.shape_file else None),
        "units": args.units,
        "weight_column": "weight",
        "name_coord_tol_km": args.name_coord_tol_km,
        "max_name_dist_km": args.max_name_dist_km,
        "tie_tol_km": args.tie_tol_km,
        "voltage_mode": args.voltage_mode,
        "input_columns": list(inp.columns),
        "on_duplicate": args.on_duplicate,
        "duplicate_targets": duplicates,
        "n_rows_with_coordinates": int(inp.lat.notna().sum())
        if "lat" in inp.columns else 0,
        "on_duplicate_name": args.on_duplicate_name,
        "colocate_km": args.colocate_km,
        "duplicate_names": (int(len(rep_collapse))
                            if rep_collapse is not None else
                            int(inp.name.duplicated().sum())),
        "map": args.map,
        "system": args.system,
        "pool": args.pool,
        "bus_types": args.bus_types,
        "map_file": rel(map_path), "map_md5": md5(map_path),
        "input_file": str(args.input), "input_md5": md5(Path(args.input)),
        "n_input_rows": int(len(inp)),
        "routes": {k: int(v) for k, v in by_route.items()},
        "counts": counts,
        "reference_tables": {
            "profiles": rel(PROFILES),
            "name_dictionary": rel(BASIN_DICT),
            "reference_substations": rel(CEC_2022),
        },
        "ambiguous_refused": {
            "profiled_names_ambiguous_across_utilities": sorted(name_ambig),
            "dictionary_inversions_ambiguous": sorted(dict_ambig),
            "reference_names_ambiguous": cec_ambig[:50],
        },
        "name_match_distance_km": (
            {k: (None if pd.isna(v) else round(float(v), 3)) for k, v in
             report.name_dist_km.describe(
                 percentiles=[0.5, 0.9, 0.99]).items()}
            if report.name_dist_km.notna().any() else None),
        "notes": [
            f"Weights are LEVELS in {args.units} units; every consumption site "
            "normalizes, so the scale cancels. No sigma and no q10/q90 are implied.",
            "Spatial rows (coord_spatial, cec_spatial) do not honour the --map axis.",
            "name_dist_km is the distance from a row's own coordinates to the "
            "substation its NAME matched -- large values mean the name agreed "
            "but the location did not.",
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"input: {len(inp):,} rows from {args.input}")
    for route, n in sorted(by_route.items()):
        print(f"  {route:16s} {n:6,d}")
    unres = report[report.route == ext.ROUTE_UNRESOLVED]
    if len(unres):
        print(f"  UNRESOLVED       {len(unres):6,d}  "
              f"(named in resolution_report.csv, e.g. {unres.name.head(3).tolist()})")
    noted = report[(report.route != ext.ROUTE_UNRESOLVED)
                   & report.status.str.contains(";", na=False)]
    if len(noted):
        print(f"  resolved with a note: {len(noted):,} "
              f"(proximity disambiguation or distance demotion -- see the report)")
    print(f"shape: {args.shape} ({args.shape_col})"
          f"{' + ' + Path(args.shape_file).name if args.shape_file else ''}"
          f"   map: {args.map}   units: {args.units}")
    nd = report.name_dist_km.dropna()
    if len(nd):
        print(f"name-vs-coordinate distance over {len(nd):,} checked rows: "
              f"median {nd.median():.2f} km, p90 {nd.quantile(0.9):.2f} km, "
              f"max {nd.max():.2f} km")
    print(f"substation weights: {len(sub_w):,} rows "
          f"({sub_w.groupby(['utility', 'substation_name']).ngroups if len(sub_w) else 0} substations)")
    print(f"node weights:       {len(node_w):,} rows "
          f"({node_w.node.nunique() if len(node_w) else 0} buses, route 2)")
    print(f"wrote 4 files to {rel(out_dir)}")


if __name__ == "__main__":
    main()
