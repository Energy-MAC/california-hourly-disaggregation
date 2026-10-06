"""External seasonal substation loads -> per-cell weight tables.

An outside source may give one load value per substation per season (summer and
winter) rather than the utilities' (month, hour_pst) percentile envelopes. This
module turns such a file into the weight tables the county-first allocation's
within-county split consumes, so the external measurement decides where load
sits inside each county.

This is a WEIGHT SOURCE, not a new disaggregation approach. The allocation stays
county-first: ReEDS sets how much energy each county gets and these weights set
only how it splits among that county's buses. Every consumption site normalizes,
so the absolute MW cancel -- what survives is the cross-substation pattern and
the summer:winter ratio. Nothing here needs, or produces, a sigma: the weight is
a level, and `rescale_genx_demand.envelope_cell_weights` (the function these
tables stand in for) reads `max_load` alone.

Why the input file cannot simply be relabelled as an envelope: synthesizing
q10/q90 from a chosen coefficient of variation would write a modelling
assumption into a file shaped like measured data, and Approach 2's claim is that
its marginals are *identified* by the envelopes rather than estimated. So the
tables here carry a single `weight` per cell and no distributional parameters.
The column is deliberately NOT called `load_mw`: the input may be in arbitrary
units (`--units relative`), and a column named for megawatts invites exactly
the misreading that cannot be undone later.
If the stochastic weight source is also wanted on this data, sigma is supplied at
run time by `generate_stochastic.py --sigma-source`, where it is visible in the
run tag.

Name resolution is a strict cascade -- each input row takes the FIRST rule that
fires (see `resolve_names`):

  1. the name is a profiled utility substation                  -> route 1
  2. the name is a BasinName in basinSourceDictionary whose
     SourceName is a profiled utility substation                -> route 1
  3. the name is a same-owner record in ca_substations_2022.csv  -> route 2
  4. otherwise                                                   -> unresolved

Rule 1 precedes rule 2 because the dictionary is an EXCEPTIONS list, not a
complete mapping. Route 1 rows ride the nodal map and therefore honour the
`--map` axis and its tie shares; route 2 rows are placed spatially and are
map-independent, which is a real limitation recorded per row rather than hidden.

Per CLAUDE.md, name matching uses `cecSourceDictionary.csv` or
`basinSourceDictionary.csv` ONLY. This module uses the basin one, because the
external names are keyed to `ca_substations_2022.csv`.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import cells as _cells

REQUIRED_INPUT_COLS = ("name", "summer_load", "winter_load")

#: the half-year block each input column describes. Spring lumps with winter and
#: September with summer; see `cells` for the measured justification.
SEASON_COL_BLOCK = {"summer_load": "MayOct", "winter_load": "NovApr"}

SHAPE_MODES = ("utility", "flat", "file")

#: how a repeated `name` in the input is handled -- see `read_input`
DUPLICATE_NAME_MODES = ("error", "keep", "sum")

#: `avg_load` = (min+max)/2 is the envelope midpoint, i.e. the central-tendency
#: load in the cell. The external level is itself a central value (a median), so
#: pairing it with the central-tendency shape keeps the two consistent.
#: `max_load` is offered for sensitivity because it is what the envelope weight
#: source itself uses.
SHAPE_COLS = ("avg_load", "max_load")

ROUTE_UTILITY_DIRECT = "utility_direct"
ROUTE_UTILITY_DICT = "utility_dict"
ROUTE_COORD_SPATIAL = "coord_spatial"
ROUTE_CEC_SPATIAL = "cec_spatial"
ROUTE_UNRESOLVED = "unresolved"


class AmbiguousNameError(ValueError):
    """A name resolves to more than one substation, so no choice is defensible."""


# ---------------------------------------------------------------------------
# Reference tables
# ---------------------------------------------------------------------------

def read_input(path: str | Path, on_duplicate_name: str = "error",
               colocate_km: float = 0.5) -> pd.DataFrame:
    """Read and check the external seasonal load file.

    Expected columns: `name`, `summer_load`, `winter_load` -- one row per
    substation, the load in MW. `name` may be a utility substation name or a
    `ca_substations_2022.csv` name, mixed freely in the same column.

    Optional columns, all of which widen what can be resolved:

    `lat`, `lon`   the row's own coordinates. These are the valuable ones: they
                   let a row be placed on a bus without its name having to match
                   the reference table at all, and they disambiguate every name
                   that several utilities share. Measured: of the 48 shared
                   names, 47 have candidates at least 65 km apart (median 558),
                   so proximity settles them with a wide margin.
    `voltage_kv`   enables the same voltage-restricted placement the `voltres`
                   map variant uses, via `band_to_cats_class`.
    `utility`      pge / sce / sdge. A cross-check and a route hint. With
                   coordinates supplied it is NOT needed to disambiguate; it is
                   required only for a shared name whose candidates have no
                   coordinates, and for the per-utility shape file.

    Any other column is carried through untouched for provenance.

    A `name` may repeat. The row's identity is `row_id`, not its name, because
    two rows can legitimately describe one site (two banks at one substation) or
    two different sites that happen to share a name -- and across utilities,
    shared names sit 65 km apart or more. `on_duplicate_name` decides:

      error  (default) refuse, listing the repeats
      keep   every row stays its own unit. Rows that resolve to the same
             substation or bus are summed downstream anyway, because weights are
             summed per node -- so co-located duplicates need no special case
      sum    additionally COLLAPSE same-name rows that sit within `colocate_km`
             of each other into one unit, adding their loads. Same-name rows at
             different locations stay separate, exactly as under `keep`

    `keep` and `sum` give identical final weights whenever the duplicates are
    co-located; `sum` differs only in making the merge explicit up front and in
    what it reports.
    """
    df = pd.read_csv(path)
    missing = [c for c in REQUIRED_INPUT_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing column(s) {missing}; "
                         f"expected {list(REQUIRED_INPUT_COLS)}, got {list(df.columns)}")
    if on_duplicate_name not in DUPLICATE_NAME_MODES:
        raise ValueError(f"on_duplicate_name must be one of "
                         f"{DUPLICATE_NAME_MODES}; got {on_duplicate_name!r}")
    df["name"] = df.name.astype(str)
    for c in ("summer_load", "winter_load"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["utility"] = (df.utility.astype(str).str.strip().str.lower()
                     if "utility" in df.columns else None)
    for src, dst in (("lat", "lat"), ("long", "lon"), ("lon", "lon"),
                     ("longitude", "lon"), ("latitude", "lat"),
                     ("voltage", "voltage_kv"), ("voltage_kv", "voltage_kv"),
                     ("kv", "voltage_kv")):
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]
    for c in ("lat", "lon", "voltage_kv"):
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df.columns else np.nan
    bad = df[(df.lat.notna() ^ df.lon.notna())]
    if len(bad):
        raise ValueError(f"{path}: {len(bad)} row(s) have only one of lat/lon, "
                         f"e.g. {bad.name.head(3).tolist()}")
    off = df[(df.lat.abs() > 90) | (df.lon.abs() > 180)]
    if len(off):
        raise ValueError(f"{path}: {len(off)} row(s) have out-of-range coordinates, "
                         f"e.g. {off.name.head(3).tolist()}")

    dup = sorted(df.name[df.name.duplicated()].unique())
    if dup and on_duplicate_name == "error":
        raise ValueError(
            f"{path}: {len(dup)} name(s) appear more than once, e.g. {dup[:5]}. "
            f"Pass --on-duplicate-name keep to treat every row as its own unit "
            f"(co-located rows still merge at their shared bus), or "
            f"--on-duplicate-name sum to add up same-name rows that sit within "
            f"--colocate-km of each other.")
    if dup and on_duplicate_name == "sum":
        df = _collapse_colocated(df, colocate_km)
    df = df.reset_index(drop=True)
    df["row_id"] = np.arange(len(df), dtype=np.int64)
    return df


def _collapse_colocated(df: pd.DataFrame, colocate_km: float) -> pd.DataFrame:
    """Add up same-name rows that sit within `colocate_km`; keep the rest apart.

    Two rows naming one substation at one location are one unit and their loads
    add. Two rows sharing a name at materially different locations are different
    substations, and merging them would move load to the wrong place -- so they
    are left as separate units, to be told apart by their own coordinates.

    Rows in a duplicate group with no coordinates cannot be clustered, so they
    are summed together and `collapse_report` records it.
    """
    out, report = [], []
    for name, g in df.groupby("name", sort=False):
        if len(g) == 1:
            out.append(g)
            continue
        with_xy = g[g.lat.notna() & g.lon.notna()]
        without = g[~(g.lat.notna() & g.lon.notna())]
        clusters: list[pd.DataFrame] = []
        remaining = with_xy
        while len(remaining):
            seed = remaining.iloc[0]
            d = _haversine_km(seed.lat, seed.lon,
                              remaining.lat.to_numpy(), remaining.lon.to_numpy())
            near = remaining[d <= colocate_km]
            clusters.append(near)
            remaining = remaining[d > colocate_km]
        if len(without):
            clusters.append(without)
        for cl in clusters:
            row = cl.iloc[[0]].copy()
            for c in SEASON_COL_BLOCK:
                row[c] = cl[c].sum()
            if cl.lat.notna().any():
                w = cl[list(SEASON_COL_BLOCK)[0]].to_numpy(float)
                w = w if np.isfinite(w).all() and w.sum() > 0 else None
                row["lat"] = np.average(cl.lat.to_numpy(float), weights=w)
                row["lon"] = np.average(cl.lon.to_numpy(float), weights=w)
            if "voltage_kv" in cl.columns and cl.voltage_kv.notna().any():
                row["voltage_kv"] = cl.voltage_kv.max()
            out.append(row)
        spread = 0.0
        if len(with_xy) > 1:
            spread = float(max(
                _haversine_km(r.lat, r.lon, with_xy.lat.to_numpy(),
                              with_xy.lon.to_numpy()).max()
                for _, r in with_xy.iterrows()))
        report.append({"name": name, "rows": len(g), "units": len(clusters),
                       "max_km_within_name": spread,
                       "rows_without_coordinates": len(without)})
    res = pd.concat(out, ignore_index=True)[list(df.columns)]
    res.attrs["collapse_report"] = pd.DataFrame(report)
    return res


def build_name_index(profiles: pd.DataFrame, norm
                     ) -> tuple[dict, dict, dict]:
    """`norm(substation_name) -> (utility, verbatim substation_name)`.

    The verbatim name is what the nodal map joins on (`how="inner"`, no case
    normalization anywhere), so a resolved row must carry it back exactly.

    Returns (unambiguous lookup, ambiguous -> its candidate pairs,
    `(utility, norm) -> pair` for rows that supply a utility). A name several
    utilities use is kept OUT of the first lookup: resolving it by coincidence,
    or letting it fall through to a coordinate match, would both be wrong.
    """
    subs = (profiles[["utility", "substation_name"]].drop_duplicates()
            .assign(_n=lambda d: d.substation_name.map(norm)))
    subs["utility"] = subs.utility.astype(str).str.lower()
    lookup, ambiguous = {}, {}
    for n, g in subs.groupby("_n"):
        if not n:
            continue
        pairs = sorted(zip(g.utility, g.substation_name))
        if len(pairs) > 1:
            ambiguous[n] = pairs
            continue
        lookup[n] = pairs[0]
    by_utility = {(u, n): (u, s)
                  for n, g in subs.groupby("_n") if n
                  for u, s in zip(g.utility, g.substation_name)}
    return lookup, ambiguous, by_utility


def invert_basin_dictionary(dict_path: str | Path, name_index: dict, norm
                            ) -> tuple[dict, dict]:
    """`norm(BasinName) -> (utility, verbatim substation_name)`, inverted.

    The file maps utility name -> basin name; resolving an external basin name
    needs the reverse. That direction is MANY-TO-ONE in the real data (PGE
    `drum 1` and `drum 2` both point at `DRUM`; SCE `cal city` is claimed by two
    substations), so an inverted key with more than one profiled target is
    recorded as ambiguous and refused at resolution time rather than guessed.
    `SourceName`s absent from the profiled fleet are dropped.
    """
    bd = pd.read_csv(dict_path, encoding="utf-8-sig")
    for col in ("SourceName", "BasinName", "Utility"):
        if col not in bd.columns:
            raise ValueError(f"{dict_path}: expected column {col!r}, "
                             f"got {list(bd.columns)}")
    targets: dict[str, set] = {}
    for src, basin in zip(bd.SourceName, bd.BasinName):
        key = norm(basin)
        hit = name_index.get(norm(src))
        if not key or hit is None:
            continue           # SourceName not in the profiled fleet -> drop
        targets.setdefault(key, set()).add(hit)
    lookup = {k: next(iter(v)) for k, v in targets.items() if len(v) == 1}
    ambiguous = {k: sorted(v) for k, v in targets.items() if len(v) > 1}
    return lookup, ambiguous


def build_cec_index(cec_path: str | Path, norm) -> tuple[dict, list[str]]:
    """`norm(name) -> (verbatim name, lat, lon)` for the reference table.

    The coordinates come back with the name so a row matched here can be placed
    the same way a row with its OWN coordinates is -- through the production
    nearest-node function, restricted to buses the allocation can actually use.
    The alternative, a precomputed nearest-bus lookup, was computed against every
    CATS bus with no pool filter, so it could land a row on a bus the deliverable
    drops (e.g. one outside every California county polygon) and lose its load
    silently.

    Within-owner normalized collisions exist in this table; a collision that
    cannot be resolved to one record is reported rather than silently
    first-won, because the caller may legitimately mean either.
    """
    ref = pd.read_csv(cec_path)
    if "name" not in ref.columns:
        raise ValueError(f"{cec_path}: expected a 'name' column, got {list(ref.columns)}")
    # placeholder records, dropped here for the same reason
    # process_substations_clean.py drops them: the name identifies nothing
    ref = ref[ref.name.astype(str).str.strip().str.lower() != "unknown"]
    ref = ref.assign(_n=lambda d: d.name.map(norm))
    has_xy = {"latitude", "longitude"} <= set(ref.columns)
    lookup, ambiguous = {}, []
    for n, g in ref[ref._n != ""].groupby("_n"):
        names = sorted(set(g.name.astype(str)))
        if len(names) > 1:
            ambiguous.append(n)
            continue
        lat = lon = np.nan
        if has_xy:
            xy = g[g.latitude.notna() & g.longitude.notna()]
            if len(xy):
                lat = float(xy.latitude.iat[0])
                lon = float(xy.longitude.iat[0])
        lookup[n] = (names[0], lat, lon)
    return lookup, sorted(ambiguous)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km. Vectorised over the second pair."""
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(np.asarray(lat2, dtype=float))
    dp = p2 - p1
    dl = np.radians(np.asarray(lon2, dtype=float) - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def resolve_names(inp: pd.DataFrame, name_index: dict, name_ambiguous: dict,
                  name_by_utility: dict, dict_lookup: dict,
                  dict_ambiguous: dict, cec_index: dict,
                  norm, sub_coords: dict | None = None,
                  name_coord_tol_km: float = 25.0,
                  max_name_dist_km: float | None = None) -> pd.DataFrame:
    """Apply the cascade to every input row. One output row per input row.

    Columns: name, route, utility, substation_name, node, dist_km, name_dist_km,
    status. `status` is "ok" for a resolving route and a reason otherwise.
    Nothing is dropped: an unresolved row is reported, never omitted.

    First rule to fire wins:

      1. an exact match to a profiled utility substation -- by `(utility, name)`
         when the row supplies a utility, else by name alone when unambiguous,
         else by PROXIMITY among the candidates when the row supplies coordinates
      2. the inverted basinSourceDictionary -> a profiled substation
      3. the row's own coordinates (node assigned by the caller, which owns the
         production nearest-node function)
      4. a reference-table name -> that record's own coordinates
      5. unresolved

    Rules 1 and 2 ride the nodal map and carry the substation's own envelope
    shape; rules 3 and 4 are spatial and map-independent.

    A rule-4 row carries the reference record's `ref_lat`/`ref_lon` for the caller
    to place; it is NOT given a node here, for the same reason a rule-3 row is not.

    `sub_coords` maps `(utility, substation_name) -> (lat, lon)` and powers both
    the proximity tie-break and `name_dist_km`, the distance between a row's own
    coordinates and the substation its NAME matched. A large `name_dist_km` means
    the name agreed but the location did not; `max_name_dist_km` (None = off)
    demotes such a row to rule 3 rather than trusting the name.
    """
    sub_coords = sub_coords or {}
    has_util = ("utility" in inp.columns and inp.utility.notna().any())
    utilities = inp.utility.tolist() if has_util else [None] * len(inp)
    lats = inp.lat.tolist() if "lat" in inp.columns else [np.nan] * len(inp)
    lons = inp.lon.tolist() if "lon" in inp.columns else [np.nan] * len(inp)

    def coords_of(pair):
        c = sub_coords.get(pair)
        return c if c and np.isfinite(c[0]) and np.isfinite(c[1]) else None

    rids = (inp.row_id.tolist() if "row_id" in inp.columns
            else list(range(len(inp))))
    rows = []
    for rid, name, util, lat, lon in zip(rids, inp.name, utilities, lats, lons):
        n = norm(name)
        has_xy = bool(np.isfinite(lat) and np.isfinite(lon))
        rec = {"row_id": rid, "name": name, "route": ROUTE_UNRESOLVED,
               "utility": None, "input_utility": util or None,
               "substation_name": None, "node": None, "dist_km": np.nan,
               "name_dist_km": np.nan, "ref_lat": np.nan, "ref_lon": np.nan,
               "status": ""}

        def finish(route, hit=None, node=None, dist=np.nan, status="ok", note=""):
            rec.update(route=route, status=(status + ("; " + note if note else "")))
            if hit is not None:
                rec["utility"], rec["substation_name"] = hit
                if has_xy:
                    c = coords_of(hit)
                    if c:
                        rec["name_dist_km"] = float(_haversine_km(lat, lon, c[0], c[1]))
            if node is not None:
                rec["node"], rec["dist_km"] = str(node), dist
            rows.append(rec)

        if not n:
            rec["status"] = "blank name after normalization"
            rows.append(rec)
            continue

        # --- rule 1: a profiled utility substation ---------------------------
        hit, note = None, ""
        if util and (util, n) in name_by_utility:
            hit = name_by_utility[(util, n)]
        elif n in name_ambiguous:
            cands = name_ambiguous[n]
            if has_xy:
                scored = [(float(_haversine_km(lat, lon, *coords_of(c))), c)
                          for c in cands if coords_of(c)]
                scored.sort()
                if len(scored) == 1 and scored[0][0] <= name_coord_tol_km:
                    hit, note = scored[0][1], f"only candidate with coordinates, {scored[0][0]:.1f} km"
                elif (len(scored) > 1 and scored[0][0] <= name_coord_tol_km
                        and scored[1][0] >= 2 * scored[0][0]):
                    hit = scored[0][1]
                    note = (f"disambiguated by proximity: {scored[0][0]:.1f} km vs "
                            f"{scored[1][0]:.1f} km for the runner-up")
            if hit is None:
                cs = ", ".join(f"{u}/{sn}" for u, sn in cands)
                rec["status"] = (f"name used by more than one utility ({cs}); supply a "
                                 f"'utility' column, or coordinates within "
                                 f"{name_coord_tol_km:g} km of one candidate")
                rows.append(rec)
                continue
        else:
            hit = name_index.get(n)

        if hit is not None:
            c = coords_of(hit)
            if (max_name_dist_km is not None and has_xy and c
                    and float(_haversine_km(lat, lon, c[0], c[1])) > max_name_dist_km):
                d = float(_haversine_km(lat, lon, c[0], c[1]))
                rec["name_dist_km"] = d
                finish(ROUTE_COORD_SPATIAL, status="ok",
                       note=f"name matched {hit[0]}/{hit[1]} but {d:.1f} km away "
                            f"(> {max_name_dist_km:g}); placed by coordinates instead")
                continue
            finish(ROUTE_UTILITY_DIRECT, hit=hit, note=note)
            continue

        # --- rule 2: the inverted dictionary ---------------------------------
        if n in dict_ambiguous:
            raise AmbiguousNameError(
                f"{name!r} normalizes to {n!r}, which basinSourceDictionary maps "
                f"back to {dict_ambiguous[n]} -- more than one profiled "
                f"substation. Resolve it in the dictionary; this module will not "
                f"pick one.")
        hit = dict_lookup.get(n)
        if hit is not None:
            finish(ROUTE_UTILITY_DICT, hit=hit)
            continue

        # --- rule 3: the row's own coordinates -------------------------------
        # the node is assigned by the caller, which owns the production
        # nearest-node function (map_loads_to_nodes.build_mapping)
        if has_xy:
            finish(ROUTE_COORD_SPATIAL, status="ok")
            continue

        # --- rule 4: a reference-table name, placed by ITS coordinates -------
        # the caller places these exactly as rule 3, so the same pool restriction
        # applies and a row cannot land on a bus the allocation drops
        hit = cec_index.get(n)
        if hit is not None:
            _, rlat, rlon = hit
            if np.isfinite(rlat) and np.isfinite(rlon):
                rec["ref_lat"], rec["ref_lon"] = rlat, rlon
                finish(ROUTE_CEC_SPATIAL, status="ok",
                       note="placed by the reference record's own coordinates")
            else:
                rec["status"] = ("matched the reference table but that record has "
                                 "no coordinates, and the row supplied none")
                rows.append(rec)
            continue

        rec["status"] = ("no match in the profiled fleet, the dictionary, or the "
                         "reference table, and no coordinates to place it by")
        rows.append(rec)

    out = pd.DataFrame(rows)
    # node ids are strings everywhere downstream -- they must match the
    # `Demand_MW_z{id}` column suffix exactly, so never let pandas infer a float
    out["node"] = [None if v is None or pd.isna(v) else str(v) for v in out.node]
    return out


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def normalized_shapes(profiles: pd.DataFrame, shape_col: str = "avg_load"
                      ) -> pd.DataFrame:
    """Per-(substation, month, hour_pst) shape, mean 1 within each half-year block.

    Multiplying a block's external level by this shape reproduces that level as
    the block mean, so the seasonal number is preserved exactly while the
    diurnal and within-block seasonal pattern comes from the utility envelope.
    Substations whose block mean is non-positive get a flat shape: there is no
    pattern to borrow from a dead or net-export site.
    """
    if shape_col not in SHAPE_COLS:
        raise ValueError(f"shape_col must be one of {SHAPE_COLS}; got {shape_col!r}")
    half = _cells.get_spec("halfyear")
    p = profiles.copy()
    p["utility"] = p.utility.astype(str).str.lower()
    p["block"] = _cells.label_frame(half).iloc[:, 0].to_numpy()[
        _cells.encode(half, p.month)]
    denom = p.groupby(["utility", "substation_name", "block"])[shape_col].transform("mean")
    p["shape"] = np.where(denom > 0, p[shape_col] / denom, 1.0)
    return p[["utility", "substation_name", "month", "hour_pst", "block", "shape"]]


def load_shape_file(path: str | Path) -> pd.DataFrame:
    """Read an optional per-utility hourly shape: `utility, season, hour_pst, shape`.

    `season` is `summer`/`winter` (mapped to the MayOct/NovApr blocks) and is
    renormalized to mean 1 within each (utility, block) on load, so supplying a
    curve cannot move the between-utility energy split -- the seasonal level keeps
    its block-mean meaning either way.

    MEASURED CAVEAT, so nobody reaches for this expecting more than it gives: a
    shape shared by every substation of a utility multiplies them all by the same
    hourly factor, so the ranking WITHIN a utility never changes. Spearman(hour
    10, hour 18) within PGE = 1.000000 exactly, and statewide per-bus share CV is
    0.104 against 0.187 for each substation's own envelope. It buys
    between-utility reordering only. Indexing by (utility, size-bin) would
    restore within-utility reordering (PGE Spearman 0.888; normalized midday
    shape rises 0.724 -> 0.910 from the smallest to the largest quartile) and is
    the costed upgrade if this path is ever taken.
    """
    df = pd.read_csv(path)
    need = {"utility", "season", "hour_pst", "shape"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing column(s) {sorted(missing)}; "
                         f"expected {sorted(need)}, got {list(df.columns)}")
    season_block = {"summer": "MayOct", "winter": "NovApr"}
    df["utility"] = df.utility.astype(str).str.strip().str.lower()
    key = df.season.astype(str).str.strip().str.lower()
    unknown = sorted(set(key) - set(season_block))
    if unknown:
        raise ValueError(f"{path}: unknown season value(s) {unknown}; "
                         f"expected {sorted(season_block)}")
    df["block"] = key.map(season_block)
    df["hour_pst"] = pd.to_numeric(df.hour_pst, errors="coerce").astype("Int64")
    df["shape"] = pd.to_numeric(df["shape"], errors="coerce")
    if df["shape"].isna().any() or df.hour_pst.isna().any():
        raise ValueError(f"{path}: non-numeric hour_pst or shape")
    n = df.groupby(["utility", "block"]).hour_pst.nunique()
    if not (n == 24).all():
        raise ValueError(f"{path}: every (utility, season) needs all 24 hours; "
                         f"got {sorted(n.unique())}")
    m = df.groupby(["utility", "block"])["shape"].transform("mean")
    if (m <= 0).any():
        raise ValueError(f"{path}: a (utility, season) curve has a non-positive mean")
    df["shape"] = df["shape"] / m
    return df[["utility", "block", "hour_pst", "shape"]].astype({"hour_pst": int})


def fallback_shapes(shapes: pd.DataFrame, sub_county: pd.DataFrame
                    ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """County-mean and statewide fleet-mean shapes, for levels with no envelope.

    A substation the external file names but the utilities do not profile has a
    level and no pattern. The county mean is the closest available stand-in --
    climate zone drives most of the diurnal difference -- and the fleet mean
    covers counties with no profiled substation at all.

    Returns (county_shape[county_name, month, hour_pst, shape],
             fleet_shape[month, hour_pst, shape]), each renormalized to mean 1
    within its block so the level-preserving property survives averaging.
    """
    j = shapes.merge(sub_county, on=["utility", "substation_name"], how="inner")
    county = (j.groupby(["county_name", "block", "month", "hour_pst"], as_index=False)
              .shape.mean())
    fleet = (shapes.groupby(["block", "month", "hour_pst"], as_index=False)
             .shape.mean())
    for df, keys in ((county, ["county_name", "block"]), (fleet, ["block"])):
        m = df.groupby(keys)["shape"].transform("mean")
        df["shape"] = np.where(m > 0, df["shape"] / m, 1.0)
    return county, fleet


# ---------------------------------------------------------------------------
# Weight tables
# ---------------------------------------------------------------------------

def _levels_long(inp: pd.DataFrame, report: pd.DataFrame) -> pd.DataFrame:
    """One row per (resolved input row, half-year block) with its level."""
    # gate on ROUTE, not status: a resolved row may carry "ok; <note>" when it
    # was disambiguated by proximity or demoted by distance
    ok = report[report.route != ROUTE_UNRESOLVED]
    # take ONLY the levels from the input: the report's `utility` is the resolved
    # one and must win over any the caller supplied for disambiguation
    # merge on row_id, not name: a name may repeat, and two rows naming one
    # substation must each contribute their own level
    m = ok.merge(inp[["row_id", *SEASON_COL_BLOCK]], on="row_id", how="left")
    if "input_utility" not in m.columns:
        m["input_utility"] = None
    out = []
    for col, block in SEASON_COL_BLOCK.items():
        part = m.copy()
        part["block"] = block
        part["level"] = part[col]
        out.append(part.drop(columns=list(SEASON_COL_BLOCK)))
    long = pd.concat(out, ignore_index=True)
    bad = long[~np.isfinite(long.level)]
    if len(bad):
        raise ValueError(f"{len(bad)} (name, block) pair(s) have a non-finite load, "
                         f"e.g. {bad.name.head(3).tolist()}")
    return long


def build_weight_tables(inp: pd.DataFrame, report: pd.DataFrame,
                        shapes: pd.DataFrame, county_shape: pd.DataFrame,
                        fleet_shape: pd.DataFrame, sub_county: pd.DataFrame,
                        node_county: pd.DataFrame, shape_mode: str = "utility",
                        shape_file: pd.DataFrame | None = None,
                        spatial_assignment: pd.DataFrame | None = None
                        ) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Expand resolved levels over all 288 cells.

    Returns (substation_weights, node_weights, counts):
      substation_weights  utility, substation_name, month, hour_pst, weight
      node_weights        node, month, hour_pst, weight

    All 288 cells are emitted so the artifact serves both the 120-cell
    representative-week runs and the 288-cell deliverable; each consumer
    restricts as it needs.

    Shape preference, in order, with the first that applies winning:

      shape_mode="utility" (default)  own envelope -> shape_file -> county -> fleet
      shape_mode="file"               shape_file -> county -> fleet
      shape_mode="flat"               1 everywhere

    `shape_file` (optional, from `load_shape_file`) is keyed by utility, so it
    reaches a spatial row only when the input supplied a utility for it. Under
    "flat" the shape is 1 everywhere and every cell in a block is identical --
    the degenerate variant, kept because it is what a seasonal-only input
    literally contains.

    `spatial_assignment` (`name, node, share`) lets a coordinate-placed row split
    across tied buses exactly as a named row splits through the nodal map's tie
    shares. A row the assignment does not cover keeps the single node its report
    row names, at share 1. Shares must sum to 1 per name, which is asserted --
    silently dropping part of a row's load would be worse than failing.
    """
    if shape_mode not in SHAPE_MODES:
        raise ValueError(f"shape_mode must be one of {SHAPE_MODES}; got {shape_mode!r}")
    half = _cells.get_spec("halfyear")
    grid = _cells.label_frame(_cells.MONTHHOUR).reset_index(drop=True)
    grid["block"] = _cells.label_frame(half).iloc[:, 0].to_numpy()[
        _cells.encode(half, grid.month)]

    long = _levels_long(inp, report)
    counts = {"n_rows_resolved": int((report.route != ROUTE_UNRESOLVED).sum()),
              "n_rows_unresolved": int((report.route == ROUTE_UNRESOLVED).sum())}
    counts.update({f"n_rows_{r}": int((report.route == r).sum())
                   for r in sorted(report.route.unique())})

    def expand(keys: pd.DataFrame, own_shape: bool,
               unit_keys: tuple[str, ...] = ()) -> pd.DataFrame:
        """(row, cell) for every cell in the row's own block, with a shape attached.

        Merging on `block` rather than cross-joining gives each row exactly the
        144 cells of its half-year, so a block's level is spread over that block
        and nowhere else.
        """
        base = keys.merge(grid, on="block", how="inner")
        if shape_mode == "flat":
            base["shape"] = 1.0
            return base
        if own_shape and shape_mode == "utility":
            base = base.merge(shapes, how="left",
                              on=["utility", "substation_name",
                                  "month", "hour_pst", "block"])
        else:
            base["shape"] = np.nan
        if shape_file is not None and "shape_utility" in base.columns:
            sf = shape_file.rename(columns={"utility": "shape_utility",
                                            "shape": "_file"})
            base = base.merge(sf, on=["shape_utility", "block", "hour_pst"],
                              how="left")
            base["shape"] = base["shape"].fillna(base["_file"])
            base = base.drop(columns=["_file"])
        if "county_name" in base.columns:
            base = base.merge(county_shape.rename(columns={"shape": "_county"}),
                              on=["county_name", "block", "month", "hour_pst"],
                              how="left")
            base["shape"] = base["shape"].fillna(base["_county"])
            base = base.drop(columns=["_county"])
        base = base.merge(fleet_shape.rename(columns={"shape": "_fleet"}),
                          on=["block", "month", "hour_pst"], how="left")
        base["shape"] = base["shape"].fillna(base["_fleet"]).fillna(1.0)
        base = base.drop(columns=["_fleet"])
        # Renormalize the FINAL shape to mean 1 within each (unit, block). A unit
        # whose profile is missing cells gets its own shape on most of them and a
        # borrowed one on the rest, so the combined mean is not exactly 1 and the
        # level would not come back as the block mean. This makes that invariant
        # unconditional without touching the hour-to-hour pattern.
        if unit_keys:
            g = list(unit_keys) + ["block"]
            m = base.groupby(g)["shape"].transform("mean")
            base["shape"] = np.where(m > 0, base["shape"] / m, 1.0)
        return base

    # --- route 1: keyed on (utility, substation_name), rides the nodal map ----
    r1 = long[long.route.isin({ROUTE_UTILITY_DIRECT, ROUTE_UTILITY_DICT})]
    sub_w = pd.DataFrame(columns=["utility", "substation_name", "month",
                                  "hour_pst", "weight"])
    if len(r1):
        keys = (r1[["utility", "substation_name", "block", "level"]]
                .merge(sub_county, on=["utility", "substation_name"], how="left"))
        keys["shape_utility"] = keys.utility
        exp = expand(keys, own_shape=True,
                     unit_keys=("utility", "substation_name"))
        exp["weight"] = exp.level * exp["shape"]
        sub_w = exp.groupby(["utility", "substation_name", "month", "hour_pst"],
                            as_index=False)["weight"].sum()
        counts["n_substations_named"] = int(
            r1.groupby(["utility", "substation_name"]).ngroups)
        counts["n_route1_cells_shape_from_fallback"] = int(
            exp.merge(shapes, how="left", on=["utility", "substation_name",
                                              "month", "hour_pst", "block"],
                      suffixes=("", "_own")).shape_own.isna().sum()
        ) if shape_mode == "utility" else 0

    # --- route 2: already a bus, map-independent -----------------------------
    # both spatial routes are already on a bus: coordinate-placed rows (node
    # assigned by the caller) and reference-name rows
    r2 = long[long.route.isin({ROUTE_COORD_SPATIAL, ROUTE_CEC_SPATIAL})]
    node_w = pd.DataFrame(columns=["node", "month", "hour_pst", "weight"])
    if len(r2):
        if spatial_assignment is not None and len(spatial_assignment):
            sa = spatial_assignment.copy()
            sa["node"] = sa.node.astype(str)
            dev = sa.groupby("row_id")["share"].sum().sub(1.0).abs()
            if (dev > 1e-9).any():
                off = dev[dev > 1e-9]
                raise ValueError(f"spatial tie shares must sum to 1 per name; "
                                 f"{len(off)} do not, e.g. {off.index[:3].tolist()}")
            r2 = r2.rename(columns={"node": "_node_report"}).merge(
                sa[["row_id", "node", "share"]], on="row_id", how="left")
            r2["node"] = r2.node.fillna(r2._node_report)
            r2["share"] = r2.share.fillna(1.0)
            r2 = r2.drop(columns=["_node_report"])
        else:
            r2 = r2.assign(share=1.0)
        keys = r2[["node", "share", "block", "level", "input_utility"]].merge(
            node_county, on="node", how="left")
        keys = keys.rename(columns={"input_utility": "shape_utility"})
        exp = expand(keys, own_shape=False, unit_keys=("node",))
        exp["weight"] = exp.level * exp.share * exp["shape"]
        node_w = exp.groupby(["node", "month", "hour_pst"],
                             as_index=False)["weight"].sum()
        counts["n_nodes_spatial"] = int(r2.node.nunique())
        missing = r2[r2.node.isna()]
        if len(missing):
            raise ValueError(
                f"{len(missing)} spatial row(s) still have no node -- assign "
                f"coordinate-placed rows before building the weight tables, "
                f"e.g. {missing.name.head(3).tolist()}")

    return sub_w, node_w, counts
