"""Rescale GenX scenario demand so its spatial distribution follows this
project's disaggregation + nodal-assignment methods, holding statewide load fixed.

The `genx/` scenario tree is treated as a CONTROL.  Each case's
`system/Demand_data.csv` is a 168-hour representative week over 8,870
`Demand_MW_z{i}` zones, where zone `z{i}` is CATS `bus_i` -- the same node id
the nodal mapping assigns substations to.  This script rewrites how that load
is split across buses while preserving the statewide total in every hour, so a
downstream GenX comparison isolates spatial allocation from load magnitude.

Only 4 distinct demand files exist (one per season, shared across the 7
renewable weather years), so a run computes 4 files, not 28; use
materialize_genx_cases.py to expand them into runnable case folders.

Method axes (run tag `genx__{weights}__{map}__{alloc}__{level}`)
  --weights  reedsco COUNTY-FIRST (primary): each county gets its ReEDS share of
                     the statewide total, then splits it internally via --alpha.
                     County totals are exact, so no top-off can arise.
             stoch   pool-and-redistribute by Approach 2 stochastic output
                     (--stoch-gate / --stoch-topoff / --draw); county totals
                     EMERGE. MUST run at --level monthhour: every Approach 2
                     parameter is estimated per (month, hour_pst) cell, and a
                     static run collapses the model to a rescaled envelope
                     midpoint (a null result by construction).
             env     envelope-weighted hold: re-split ONLY the control load on
                     covered buses, among those buses, in proportion to the
                     substations' own max_load envelope; every other bus is
                     copied through untouched (minimum-intervention contrast).
                     No ReEDS anything.
             control no-op passthrough; reproduces the control byte-for-byte
  --map      prox      substation_node_map.csv (nearest node)
             voltres   substation_node_map__voltrestrict.csv (voltage-restricted)
             nameprox  substation_node_map__nameprox.csv (CEC-lineage identity
                       match first, proximity for the remainder)
             catch     substation_node_map__catchment.csv (transportation-LP
                       catchments: every candidate bus assigned to a substation,
                       load returns to the catchment; every candidate bus loaded)
             namecatch substation_node_map__namecatchment.csv (identity matches
                       forced into the LP, catchments for the rest)
             (nameprox/catch/namecatch are built once by
             scripts/load_projection/nodal/build_identity_catchment_maps.py)
  --alpha    county-first only; share of a county's energy given to its UNCOVERED
             buses as an equal split, the rest going to its substation buses in
             proportion to their mean max_load envelope.
             ratio  alpha = u/n per county -> every bus loaded
             0      substation buses only -> fewer buses loaded
             <float> fixed alpha everywhere (sensitivity sweeps)
  --level    static  one share per bus (default; forbidden for --weights stoch)
             monthhour  bus shares vary by (month, hour_pst) cell -- envelope
                     cells for reedsco/env, Approach 2 per-cell output for stoch;
                     needs genx/rep_week_calendar.csv

Other parameters
  --year N            projection year for the stochastic weights (default 2019)
  --county-year N     ReEDS county table year for the NORMALIZED county weights
                      (default: --year); only shares are used, never levels
  --stoch-gate F      county coverage threshold for the stochastic sweep
  --stoch-topoff      none | equal (keep swept uncovered buses on beta=|U|/|S|)
  --draw              mean | 0..N-1 (stochastic realization choice)
  --min-draws N       fail if the stochastic run has fewer MC draws (default 1)
  --stochastic-run    run-tag folder for --weights stoch (default: the
                      CATS-calibrated run, F* recomputed on the CATS target)
  --float-format      one_decimal (default, matches control) | full
  --out-root          default genx/rescaled

Outputs (genx/rescaled/{run_tag}/)
  Demand_data__{season}.csv   one per season, control layout preserved
  node_shares.csv             per-bus (or bus-cell) share provenance
  county_allocation.csv       per-county share, alpha, bus counts (county-first)
  pool_counties.csv           per-county coverage/sweep detail (stochastic)
  manifest.json               axes, sources + md5s, conservation + coverage checks

Usage
  python scripts/load_projection/genx/rescale_genx_demand.py --weights reedsco --alpha ratio
  python scripts/load_projection/genx/rescale_genx_demand.py --weights reedsco --alpha 0 --level monthhour
  python scripts/load_projection/genx/rescale_genx_demand.py --weights stoch --level monthhour --stoch-gate 0.30 --stoch-topoff equal
  python scripts/load_projection/genx/rescale_genx_demand.py --weights env --level monthhour
  python scripts/load_projection/genx/rescale_genx_demand.py --weights control   # no-op check
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/nodal"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/checks"))
from genx_demand_io import (  # noqa: E402
    GENX_ROOT, ZONE_PREFIX, GenXDemand, load_rep_week_calendar, md5, read_demand,
    round_to_printed, scenario_seasons, write_demand)
from hybrid_county_topup import nodes_by_county  # noqa: E402
from validate_county_reeds import reeds_county_annual  # noqa: E402

PROCESSED = ROOT / "data/processed"
PROJ_ROOT = PROCESSED / "load_projection/projections"
EXTERNAL_ROOT = PROCESSED / "load_projection/external_loads"

#: within-county weight sources. 'envelope' is the published default, so callers
#: that never set `args.county_weights` are byte-for-byte unchanged.
COUNTY_WEIGHT_SRCS = ("envelope", "stoch", "external")
NODAL_DIR = PROCESSED / "load_projection/nodal"
CATS_BUSES = ROOT / "data/raw/CATS/CATS_buses.csv"
DEFAULT_OUT_ROOT = GENX_ROOT / "rescaled"

MAP_FILES = {
    "prox": "substation_node_map.csv",
    "voltres": "substation_node_map__voltrestrict.csv",
    "nameprox": "substation_node_map__nameprox.csv",
    "catch": "substation_node_map__catchment.csv",
    "namecatch": "substation_node_map__namecatchment.csv",
}


def run_tag(args) -> str:
    # the control passthrough ignores the other axes, so they are left out of its
    # tag rather than minting identical folders under different names
    if args.weights == "control":
        return "genx__control"
    # the third slot is the allocation rule: the alpha split for the county-first
    # family, the sweep design for the stochastic one, 'hold' for the envelope hold
    if args.weights == "reedsco":
        alloc = f"a{str(args.alpha).replace('.', 'p')}"
    elif args.weights == "stoch":
        # gate + top-off name the design; the draw names the realization
        way = "w2" if args.stoch_gate > 1 else f"w1g{int(round(args.stoch_gate*100))}"
        if args.stoch_topoff == "equal":
            way += "top"
        alloc = f"{way}-{args.draw if args.draw == 'mean' else 'd' + str(args.draw)}"
    else:
        alloc = "hold"
    return f"genx__{args.weights}__{args.map}__{alloc}__{args.level}"


# --------------------------------------------------------------------------
# Step A -- substation-level annual MWh from a projection run
# --------------------------------------------------------------------------

def substation_annual(args) -> tuple[pd.DataFrame, dict]:
    """(utility, substation_name, annual_mwh) from the Approach 2 stochastic run.

    The Approach 1 (ReEDS substation-first) source was REMOVED 2026-08-13: it
    carried ReEDS load LEVELS through Approach 1's disaggregated MWh, and the
    standing rule is that only ReEDS county WEIGHTS (normalized shares) may ever
    enter -- levels must cancel.  The minimum-intervention contrast is now the
    envelope-weighted hold (--weights env), which uses no ReEDS at all.
    """
    if args.weights != "stoch" and county_weight_src(args) != "stoch":
        raise ValueError(f"no substation table for weights={args.weights!r}")
    path = PROJ_ROOT / args.stochastic_run / "substation_annual_mwh.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"stochastic run not found: {path}\n"
            f"Run scripts/load_projection/approach2/generate_stochastic.py first.")
    df = pd.read_csv(path)
    n_draws = df["draw"].nunique()
    if n_draws < args.min_draws:
        raise ValueError(
            f"{path} has {n_draws} Monte Carlo draw(s), --min-draws is "
            f"{args.min_draws}. A share vector built from too few draws is "
            f"not a stable mean; re-run generate_stochastic.py with "
            f"--n-draws >= {args.min_draws} (or lower --min-draws to accept it).")
    df = df[df.year == args.year]
    if df.empty:
        raise ValueError(f"{path} has no rows for year {args.year}")
    draw = getattr(args, "draw", "mean")
    if draw != "mean":
        # a single realization, so the run carries the model's own spread
        # rather than averaging it away
        if int(draw) not in set(df.draw):
            raise ValueError(f"{path} has no draw {draw}; available "
                             f"{sorted(set(df.draw))}")
        df = df[df.draw == int(draw)]
    sub = df.groupby(["utility", "substation_name"], as_index=False)["annual_mwh"].mean()
    meta = {"source_file": rel(path), "source_md5": md5(path),
            "n_draws_available": int(n_draws), "draw_used": draw,
            "year": args.year}

    sub["utility"] = sub.utility.astype(str).str.lower()
    meta["n_substations"] = int(len(sub))
    meta["total_mwh"] = float(sub.annual_mwh.sum())
    return sub, meta


# --------------------------------------------------------------------------
# Step B -- substation annual MWh -> bus annual MWh
# --------------------------------------------------------------------------

def substation_to_node(sub: pd.DataFrame, map_path: Path) -> tuple[pd.Series, dict]:
    """Move each substation's annual MWh onto the bus the nodal map assigns it.

    A substation tied between equidistant buses carries a `share` < 1 per bus,
    so the load is split accordingly; summing per bus gives the method's bus
    total.  Node ids stay strings end to end because they have to match the
    `Demand_MW_z{id}` column suffix exactly.
    """
    mapping = pd.read_csv(map_path, dtype={"node": str})
    mapping["utility"] = mapping.utility.astype(str).str.lower()
    j = sub.merge(mapping[["utility", "substation_name", "node", "share"]],
                  on=["utility", "substation_name"], how="inner")
    matched = j.substation_name.nunique()
    j["node_mwh"] = j.annual_mwh * j.share
    per_node = j.groupby("node")["node_mwh"].sum()
    meta = {
        "map_file": rel(map_path), "map_md5": md5(map_path),
        "n_substations_mapped": int(matched),
        "n_substations_unmapped": int(sub.substation_name.nunique() - matched),
        "n_nodes_covered": int((per_node > 0).sum()),
        "n_nodes_covered_zero_weight": int((per_node <= 0).sum()),
        "mapped_mwh": float(per_node.sum()),
    }
    return per_node, meta


# --------------------------------------------------------------------------
# Step C -- gated county top-off
# --------------------------------------------------------------------------

_CANDIDATE_CACHE: pd.DataFrame | None = None
_LOADED_CACHE: set[str] | None = None
_CATS_DEMAND_CACHE: pd.Series | None = None


def _scan_control_demand() -> tuple[set[str], pd.Series]:
    """One pass over the GenX control demand: (loaded bus ids, per-bus total MWh).

    Read from the control being rescaled -- the authoritative, current copy -- NOT
    from data/raw/CATS/Demand_data.csv, which is an earlier GenX run's output.
    (Their loaded-bus sets happen to be identical, 2,471 buses.)  Memoized.
    """
    global _LOADED_CACHE, _CATS_DEMAND_CACHE
    if _LOADED_CACHE is None or _CATS_DEMAND_CACHE is None:
        tree = scenario_seasons()
        loaded: set[str] = set()
        totals: dict[str, float] = {}
        for season, case in tree.canonical.items():
            d = read_demand(tree.demand_path(case))
            col = d.values.sum(axis=0)
            # the loaded set keeps its original semantics: positive in ANY season
            loaded |= {z for z, tot in zip(d.zones, col) if tot > 0}
            for z, tot in zip(d.zones, col):
                totals[z] = totals.get(z, 0.0) + float(tot)
        _LOADED_CACHE, _CATS_DEMAND_CACHE = loaded, pd.Series(totals, dtype=float)
    return _LOADED_CACHE, _CATS_DEMAND_CACHE


def cats_loaded_buses() -> set[str]:
    """Bus ids CATS itself puts load on (see `_scan_control_demand`)."""
    return _scan_control_demand()[0]


def cats_bus_demand() -> pd.Series:
    """Per-bus total MWh in CATS's own control demand, summed over the 4 seasons.

    Used as the within-county weight for UNCOVERED buses under
    `uncovered_src(args) == "cats"`: where no metered substation reaches a bus, the
    target model's own spatial pattern is better information than a flat split.
    Consistent with the standing decision that CATS is taken as the true allocation.
    """
    return _scan_control_demand()[1]


def name_assigned_nodes(args) -> set[str]:
    """Buses the chosen nodal map reaches by DIRECT NAME MATCH, not by proximity.

    Only the identity-lineage maps (nameprox / namecatch, built from
    cecSourceDictionary.csv / basinSourceDictionary.csv) carry
    `assignment_method == 'name'`; prox and voltres have none, so this is empty
    for them.
    """
    m = pd.read_csv(map_path_for(args), dtype={"node": str})
    if "assignment_method" not in m.columns:
        return set()
    hit = m.assignment_method.astype(str).str.lower().str.contains("name")
    return set(m.loc[hit, "node"])


def pool_src(args) -> str:
    """Which candidate-bus pool the allocation may place load on.

    'all' (default) is the 2026-08-12 rule: every Type='Substation' non-IMPORT bus
    plus the AddedNodes CATS loads. Callers that never set `args.pool` are
    unchanged, so every existing `genx__*` run tag keeps its meaning.

    'cats_loaded' restricts to buses CATS ITSELF puts load on, plus any bus the
    chosen map reaches by direct name match -- an identity match to a specific
    CATS bus is evidence that bus is the right place even where CATS leaves it at
    zero. Rationale: placing load on a bus the target model never loads puts it
    somewhere CATS never intended, and for the uncovered (equal-split) pool there
    is no measurement justifying it either.
    """
    return getattr(args, "pool", "all")


def uncovered_src(args) -> str:
    """How a county's UNCOVERED pool is split among its uncovered buses.

    'equal' (default) is the published behaviour -- a flat `alpha * w_c / u` per
    bus.  Callers that never set `args.uncovered` are unchanged, so every existing
    `genx__*` run keeps its meaning.

    'cats' splits that same pool in proportion to each uncovered bus's own load in
    CATS's control demand (`cats_bus_demand`).  The pool SIZE (`alpha * w_c`) and
    every county total are untouched -- only the split inside the uncovered pool
    changes.  Motivation: CATS does not treat those buses as interchangeable (its
    own MWh across a county's uncovered buses has a median CV of 0.79), so a flat
    split discards real information for roughly half the state's load.  A county
    whose uncovered buses carry no CATS load at all falls back to the equal split.
    """
    return getattr(args, "uncovered", "equal")


def bus_types_src(args) -> str:
    """Which CATS bus TYPES may carry load.

    'all' (default) keeps the `candidate_buses()` pool as built: every
    Type='Substation' non-IMPORT bus plus the `AddedNode` buses CATS itself loads.
    Callers that never set `args.bus_types` are unchanged.

    'substation' drops EVERY `AddedNode`, including the 607 CATS does load. Those
    are topology helpers rather than real substations, so a consumer who wants
    load only at physical substations asks for this; their load is then
    redistributed within their own county by the normal county-first split, and
    the county total is unchanged.

    Independent of `pool_src`: the two compose, and this one is applied second.
    """
    return getattr(args, "bus_types", "all")


def candidate_pool(args) -> pd.DataFrame:
    """`candidate_buses()`, optionally restricted per `pool_src` / `bus_types_src`."""
    nodes = candidate_buses()
    out = nodes
    if pool_src(args) != "all":
        keep = cats_loaded_buses() | name_assigned_nodes(args)
        out = out[out.node.isin(keep)]
        print(f"  pool restricted to CATS-loaded (+name matches): "
              f"{len(nodes):,} -> {len(out):,} buses")
    if bus_types_src(args) == "substation":
        types = pd.read_csv(CATS_BUSES)
        for c in types.columns:
            if pd.api.types.is_string_dtype(types[c]):
                types[c] = types[c].str.strip().str.strip("'").str.strip()
        sub = set(types.loc[types.Type == "Substation", "bus_i"].astype(str))
        before = len(out)
        out = out[out.node.isin(sub)]
        print(f"  AddedNode buses excluded (bus_types=substation): "
              f"{before:,} -> {len(out):,} buses")
    out = out.reset_index(drop=True)
    dead = set(nodes.fips_int) - set(out.fips_int)
    if dead:
        raise ValueError(
            f"pool={pool_src(args)!r} / bus_types={bus_types_src(args)!r} leaves "
            f"{len(dead)} county/counties with no candidate bus, so their share "
            f"cannot be placed: {sorted(dead)}")
    return out


def candidate_buses() -> pd.DataFrame:
    """CATS buses eligible to carry load, with their county (memoized).

    Eligible, per the rule set 2026-08-12:
      - every `Type == 'Substation'` bus, whether or not CATS itself loads it: a
        real substation the model happens to leave unloaded is still somewhere
        our methods may legitimately place load;
      - `Type == 'AddedNode'` buses ONLY where CATS already puts load on them.
        AddedNodes are topology helpers, but CATS does place 15.9% of state load
        on 610 of them, so excluding those would force that load somewhere CATS
        never intended.  The 5,089 AddedNodes CATS leaves at zero are dropped --
        they are pure routing points and nothing should ever land there.
      - IMPORT buses are excluded throughout; they are import proxies, not load.

    "CATS already loads it" is read from the GenX control demand files being
    rescaled -- the current, authoritative copy -- NOT from
    data/raw/CATS/Demand_data.csv, which is an earlier GenX run's output.

    The county assignment is a point-in-polygon join, so the result is cached:
    it does not depend on any method axis.
    """
    global _CANDIDATE_CACHE
    if _CANDIDATE_CACHE is None:
        nodes = pd.read_csv(CATS_BUSES)
        for c in nodes.columns:
            if pd.api.types.is_string_dtype(nodes[c]):
                nodes[c] = nodes[c].str.strip().str.strip("'").str.strip()
        # "CATS loads it" is read from the GenX control demand being rescaled --
        # the authoritative, current copy -- not data/raw/CATS/Demand_data.csv,
        # which came from an earlier GenX run. (Their loaded-bus sets happen to
        # be identical, 2,471 buses, but the control's magnitudes are current.)
        loaded = cats_loaded_buses()
        ids = nodes.bus_i.astype(str)
        keep = (nodes.Import != "IMPORT") & (
            (nodes.Type == "Substation") | ids.isin(loaded))
        n_added = int(((nodes.Type == "AddedNode") & keep).sum())
        n_dropped = int(((nodes.Type == "AddedNode") & ~keep).sum())
        nodes = nodes[keep].reset_index(drop=True)
        print(f"  candidate buses: {len(nodes):,} "
              f"({len(nodes) - n_added:,} substations + {n_added:,} loaded AddedNodes; "
              f"{n_dropped:,} zero-load AddedNodes dropped)")

        shim = Namespace(id_col="bus_i", lat_col="Lat", lon_col="Lon")
        county = nodes_by_county(nodes, shim).rename(columns={"bus_i": "node"})
        county["node"] = county.node.astype(str)
        _CANDIDATE_CACHE = county.reset_index(drop=True)
    return _CANDIDATE_CACHE


# --------------------------------------------------------------------------
# Step D -- share vector
# --------------------------------------------------------------------------

def map_path_for(args) -> Path:
    """Path of the chosen nodal-map artifact, with an actionable missing-file hint."""
    map_path = NODAL_DIR / args.system / MAP_FILES[args.map]
    if not map_path.exists():
        builder = ("scripts/load_projection/nodal/build_identity_catchment_maps.py"
                   if args.map in ("nameprox", "catch", "namecatch")
                   else "scripts/load_projection/nodal/map_loads_to_nodes.py"
                   + (" --voltage-mode restrict" if args.map == "voltres" else ""))
        raise FileNotFoundError(f"nodal map not found: {map_path}\nRun {builder} first.")
    return map_path


def stoch_cell_weights(args, cache: dict, cells: set) -> tuple[pd.DataFrame, dict]:
    """Per-bus weight in each (month, hour_pst) cell from Approach 2's own
    per-cell output (`substation_cell_mw.csv`, written by generate_stochastic.py
    --save-cells).

    This is the stochastic analogue of envelope_cell_weights, and it is the
    whole point of the stochastic family: every Approach 2 parameter (mu, sigma,
    rho, s) is estimated per cell, so the weights MUST vary per cell -- a static
    stochastic weight collapses the model to a rescaled envelope midpoint.

    `--draw mean` averages the Monte Carlo draws per cell; an integer keeps one
    realization.  Negative cell means (net-export hours) are clipped to zero
    weight; a substation missing a cell falls back to its own mean over the
    cells it has, mirroring the envelope fallback.
    """
    key = (args.map, args.draw, tuple(sorted(cells)))
    cache = cache.setdefault("stoch_cells", {})
    if key not in cache:
        path = PROJ_ROOT / args.stochastic_run / "substation_cell_mw.csv"
        if not path.exists():
            raise FileNotFoundError(
                f"per-cell stochastic table not found: {path}\n"
                f"Re-run generate_stochastic.py with --save-cells "
                f"(plus --calibrate-on target for the CATS-calibrated run).")
        df = pd.read_csv(path)
        n_draws = df.draw.nunique()
        if n_draws < args.min_draws:
            raise ValueError(f"{path} has {n_draws} draw(s), --min-draws is "
                             f"{args.min_draws}")
        if args.draw == "mean":
            df = df.groupby(["utility", "substation_name", "month", "hour_pst"],
                            as_index=False)["mean_mw"].mean()
        else:
            if int(args.draw) not in set(df.draw):
                raise ValueError(f"{path} has no draw {args.draw}")
            df = df[df.draw == int(args.draw)]
        df["utility"] = df.utility.astype(str).str.lower()
        df = df[[(m, h) in cells for m, h in zip(df.month, df.hour_pst)]]

        subs = df[["utility", "substation_name"]].drop_duplicates()
        grid = subs.merge(pd.DataFrame(sorted(cells), columns=["month", "hour_pst"]),
                          how="cross")
        full = grid.merge(df, on=["utility", "substation_name", "month", "hour_pst"],
                          how="left")
        fallback = df.groupby(["utility", "substation_name"])["mean_mw"].mean()
        idx = pd.MultiIndex.from_frame(full[["utility", "substation_name"]])
        n_filled = int(full.mean_mw.isna().sum())
        full["mean_mw"] = full.mean_mw.fillna(pd.Series(fallback.reindex(idx).values))
        n_negative = int((full.mean_mw < 0).sum())
        full["weight"] = full.mean_mw.fillna(0.0).clip(lower=0.0)

        map_path = map_path_for(args)
        mapping = pd.read_csv(map_path, dtype={"node": str})
        mapping["utility"] = mapping.utility.astype(str).str.lower()
        j = full.merge(mapping[["utility", "substation_name", "node", "share"]],
                       on=["utility", "substation_name"], how="inner")
        j["weight"] = j.weight * j.share
        per_cell = j.groupby(["node", "month", "hour_pst"], as_index=False)["weight"].sum()
        meta = {
            "weight_definition": "Approach 2 per-cell mean MW "
                                 f"(draw={args.draw}, {n_draws} draws available)",
            "source_file": rel(path), "source_md5": md5(path),
            "map_file": rel(map_path), "map_md5": md5(map_path),
            "n_cells": len(cells),
            "n_cells_filled_from_substation_mean": n_filled,
            "n_cells_negative_clipped": n_negative,
            "n_nodes_weighted": int(per_cell[per_cell.weight > 0].node.nunique()),
        }
        cache[key] = (per_cell, meta)
    return cache[key]


def envelope_cell_weights(args, cache: dict, cells: set) -> tuple[pd.DataFrame, dict]:
    """Per-bus weight in each (month, hour_pst) cell, from the max-load envelope.

    The month-hour analogue of envelope_node_weights: instead of collapsing each
    substation's envelope to one number, keep `max_load` cell by cell so the
    within-county split follows the diurnal and seasonal shape the utilities
    actually measured.  `cells` restricts the work to the (month, hour) pairs the
    representative weeks touch.

    A substation missing a cell falls back to its own mean over the cells it does
    have, so it keeps its place in every split rather than silently handing its
    load to its neighbours in the cells it lacks.
    """
    key = (args.map, tuple(sorted(cells)))
    cache = cache.setdefault("envelope_cells", {})
    if key not in cache:
        sys.path.insert(0, str(ROOT / "src"))
        from load_projection.weights import load_profiles
        prof = load_profiles(PROCESSED / "substations/substation_load_profiles_clean.csv",
                             "max_load")
        prof["utility"] = prof.utility.astype(str).str.lower()
        prof = prof.groupby(["utility", "substation_name", "month", "hour_pst"],
                            as_index=False)["max_load"].mean()   # dedupe PGE overlap
        prof = prof[[(m, h) in cells for m, h in zip(prof.month, prof.hour_pst)]]

        subs = prof[["utility", "substation_name"]].drop_duplicates()
        grid = subs.merge(pd.DataFrame(sorted(cells), columns=["month", "hour_pst"]),
                          how="cross")
        full = grid.merge(prof, on=["utility", "substation_name", "month", "hour_pst"],
                          how="left")
        fallback = prof.groupby(["utility", "substation_name"])["max_load"].mean()
        idx = pd.MultiIndex.from_frame(full[["utility", "substation_name"]])
        n_filled = int(full.max_load.isna().sum())
        full["max_load"] = full.max_load.fillna(pd.Series(fallback.reindex(idx).values))
        full["weight"] = full.max_load.fillna(0.0).clip(lower=0.0)

        map_path = map_path_for(args)
        mapping = pd.read_csv(map_path, dtype={"node": str})
        mapping["utility"] = mapping.utility.astype(str).str.lower()
        j = full.merge(mapping[["utility", "substation_name", "node", "share"]],
                       on=["utility", "substation_name"], how="inner")
        j["weight"] = j.weight * j.share
        per_cell = j.groupby(["node", "month", "hour_pst"], as_index=False)["weight"].sum()
        meta = {
            "weight_definition": "max_load envelope per (month, hour_pst) cell",
            "map_file": rel(map_path), "map_md5": md5(map_path),
            "n_cells": len(cells), "n_cells_filled_from_substation_mean": n_filled,
            "n_nodes_weighted": int(per_cell[per_cell.weight > 0].node.nunique()),
        }
        cache[key] = (per_cell, meta)
    return cache[key]


def county_weight_src(args) -> str:
    """Which within-county weight source the county-first family uses.

    'envelope' (default) is the published behaviour -- callers that never set
    `args.county_weights` are byte-for-byte unchanged, so every existing
    `genx__reedsco__*` run tag keeps its meaning.

    'stoch' substitutes Approach 2's own per-substation output for the max-load
    envelope at the ONE place the weight enters (`county_first_shares` ->
    `df["weight"]`, and `expand_shares_to_cells` -> per-cell weights).
    Everything downstream -- the ReEDS county totals, alpha = u/n, the equal
    pool, the renormalisation -- is weight-source agnostic, so this is
    county-first with a different weight, NOT a new allocation family and NOT a
    new `Approach N`.

    'external' substitutes a measurement from outside this repo, built by
    `scripts/load_projection/external_loads/build_external_weights.py` into the
    folder named by `args.external_loads` (which also records whether the
    seasonal level was given a borrowed diurnal shape or left flat). It pairs
    with that attribute the same way 'stoch' pairs with `args.stochastic_run`.
    It is the one weight source that can reach buses the utilities do not
    profile: reference substations with no utility load profile are placed
    spatially, so part of what county-first otherwise fills with an UNCOVERED
    county split becomes measurement-driven instead. Still county-first, still
    not a new `Approach N`.

    Rule-clean: the envelope was preferred over Approach 1's disaggregated MWh
    because that MWh already descends from ReEDS, which would apply the same
    regional signal twice. Approach 2 descends from the utility envelopes plus
    the target series, never from ReEDS, so it carries no ReEDS signal to
    double-count.

    There is deliberately no CLI flag: exposing it would add an axis to
    `run_tag()`, and those tags are stable citation ids. The deliverable builder
    sets the attribute on its own args namespace.
    """
    src = getattr(args, "county_weights", "envelope")
    if src not in COUNTY_WEIGHT_SRCS:
        raise ValueError(f"unknown county_weights {src!r}; "
                         f"expected one of {COUNTY_WEIGHT_SRCS}")
    return src


def county_node_weights(args, cache: dict) -> tuple[pd.Series, dict]:
    """Per-node within-county weight for county-first -- dispatches on county_weight_src."""
    src = county_weight_src(args)
    if src == "external":
        return external_node_weights(args, cache)
    if src == "stoch":
        sub, meta_w = substation_annual(args)
        per_node, meta_m = substation_to_node(sub, map_path_for(args))
        return per_node, {"weight_definition": "Approach 2 per-substation annual MWh "
                                               f"(draw={getattr(args, 'draw', 'mean')})",
                          "source": meta_w, "mapping": meta_m}
    return envelope_node_weights(args, cache)


def county_cell_weights(args, cache: dict, cells: set) -> tuple[pd.DataFrame, dict]:
    """Per-(node, cell) within-county weight for county-first -- same dispatch."""
    src = county_weight_src(args)
    if src == "external":
        return external_cell_weights(args, cache, cells)
    if src == "stoch":
        return stoch_cell_weights(args, cache, cells)
    return envelope_cell_weights(args, cache, cells)


def envelope_node_weights(args, cache: dict) -> tuple[pd.Series, dict]:
    """Per-bus weight from the substations' own max-load envelopes.

    Weight is each substation's mean `max_load` over its (month, hour) cells --
    the "max load envelope" magnitude -- carried onto buses through the nodal
    map's tie shares.  Deliberately NOT Approach 1's disaggregated annual MWh:
    the county-first allocation already takes its county totals from ReEDS, so
    reusing a substation number that also descends from ReEDS would apply the
    same regional signal twice.  Here ReEDS sets how much energy a county gets
    and the envelopes set only how it splits inside that county.

    Substations whose mean envelope is <= 0 (28 of 1,347 -- dead or net-export
    sites) are clipped to zero: they carry no weight but do not subtract.
    """
    cache = cache.setdefault("envelope", {})
    if args.map not in cache:
        sys.path.insert(0, str(ROOT / "src"))
        from load_projection.weights import load_profiles
        prof = load_profiles(PROCESSED / "substations/substation_load_profiles_clean.csv",
                             "max_load")
        env = prof.groupby(["utility", "substation_name"], as_index=False)["max_load"].mean()
        env["utility"] = env.utility.astype(str).str.lower()
        n_nonpos = int((env.max_load <= 0).sum())
        env["weight"] = env.max_load.clip(lower=0.0)

        map_path = map_path_for(args)
        mapping = pd.read_csv(map_path, dtype={"node": str})
        mapping["utility"] = mapping.utility.astype(str).str.lower()
        j = env.merge(mapping[["utility", "substation_name", "node", "share"]],
                      on=["utility", "substation_name"], how="inner")
        per_node = (j.weight * j.share).groupby(j.node).sum()
        meta = {
            "weight_definition": "mean max_load envelope per substation",
            "map_file": rel(map_path), "map_md5": md5(map_path),
            "n_substations": int(len(env)),
            "n_substations_nonpositive_envelope": n_nonpos,
            "n_substations_mapped": int(j.substation_name.nunique()),
            "n_nodes_weighted": int((per_node > 0).sum()),
        }
        cache[args.map] = (per_node, meta)
    return cache[args.map]


def _external_tables(args) -> tuple[Path, pd.DataFrame, pd.DataFrame]:
    """The two weight tables written by build_external_weights.py.

    `args.external_loads` names a folder under
    data/processed/load_projection/external_loads/. Opt-in like the other
    weight-source attributes: no CLI flag, so no run tag moves.
    """
    tag = getattr(args, "external_loads", None)
    if not tag:
        raise ValueError(
            "county_weights='external' needs args.external_loads set to a folder "
            "under data/processed/load_projection/external_loads/ (see "
            "scripts/load_projection/external_loads/build_external_weights.py)")
    d = EXTERNAL_ROOT / tag
    sub_path = d / "external_substation_weights.csv"
    node_path = d / "external_node_weights.csv"
    if not sub_path.exists():
        raise FileNotFoundError(
            "external weight table not found: " + str(sub_path)
            + "\nRun build_external_weights.py --input <file> first.")
    sub = pd.read_csv(sub_path)
    sub["utility"] = sub.utility.astype(str).str.lower()
    node = (pd.read_csv(node_path, dtype={"node": str}) if node_path.exists()
            else pd.DataFrame(columns=["node", "month", "hour_pst", "weight"]))
    return d, sub, node


def external_cell_weights(args, cache: dict, cells: set) -> tuple[pd.DataFrame, dict]:
    """Per-bus weight in each (month, hour_pst) cell, from an EXTERNAL load source.

    The external analogue of envelope_cell_weights. Two inputs combine: a
    substation-keyed table, which rides the nodal map and so honours the --map
    axis and its tie shares, and a node-keyed table for reference substations the
    utilities do not profile, which were placed spatially and are therefore
    map-independent. The second is what lets this source reach municipal
    territory the utility envelopes cannot.

    Weights are LEVELS, like every other weight source here; the absolute scale
    cancels at each normalisation site. A substation missing a cell falls back to
    its own mean over the cells it has, mirroring the envelope fallback.
    """
    key = (args.map, getattr(args, "external_loads", None), tuple(sorted(cells)))
    cache = cache.setdefault("external_cells", {})
    if key not in cache:
        d, sub, node = _external_tables(args)
        cell_df = pd.DataFrame(sorted(cells), columns=["month", "hour_pst"])

        sub = sub[[(m, h) in cells for m, h in zip(sub.month, sub.hour_pst)]]
        subs = sub[["utility", "substation_name"]].drop_duplicates()
        full = subs.merge(cell_df, how="cross").merge(
            sub, on=["utility", "substation_name", "month", "hour_pst"], how="left")
        fallback = sub.groupby(["utility", "substation_name"])["weight"].mean()
        idx = pd.MultiIndex.from_frame(full[["utility", "substation_name"]])
        n_filled = int(full.weight.isna().sum())
        full["weight"] = full.weight.fillna(pd.Series(fallback.reindex(idx).values))
        full["weight"] = full.weight.fillna(0.0).clip(lower=0.0)

        map_path = map_path_for(args)
        mapping = pd.read_csv(map_path, dtype={"node": str})
        mapping["utility"] = mapping.utility.astype(str).str.lower()
        mapped_keys = mapping[["utility", "substation_name"]].drop_duplicates()
        n_unmapped = int(subs.merge(mapped_keys, on=["utility", "substation_name"],
                                    how="left", indicator=True)
                         ._merge.eq("left_only").sum())
        j = full.merge(mapping[["utility", "substation_name", "node", "share"]],
                       on=["utility", "substation_name"], how="inner")
        j["weight"] = j.weight * j.share
        parts = [j[["node", "month", "hour_pst", "weight"]]]

        node = node[[(m, h) in cells for m, h in zip(node.month, node.hour_pst)]]
        n_node_tbl = int(node.node.nunique()) if len(node) else 0
        if len(node):
            nd = node.copy()
            nd["weight"] = nd.weight.fillna(0.0).clip(lower=0.0)
            parts.append(nd[["node", "month", "hour_pst", "weight"]])

        per_cell = (pd.concat(parts, ignore_index=True)
                    .groupby(["node", "month", "hour_pst"], as_index=False)["weight"].sum())
        manifest = d / "manifest.json"
        meta = {
            "weight_definition": "external seasonal load x shape, per (month, hour_pst) cell",
            "external_dir": rel(d),
            "external_manifest_md5": md5(manifest) if manifest.exists() else None,
            "map_file": rel(map_path), "map_md5": md5(map_path),
            "n_cells": len(cells),
            "n_cells_filled_from_substation_mean": n_filled,
            "n_substations_not_in_nodal_map": n_unmapped,
            "n_nodes_from_substation_table": int(j.node.nunique()),
            "n_nodes_from_node_table": n_node_tbl,
            "n_nodes_weighted": int(per_cell[per_cell.weight > 0].node.nunique()),
        }
        cache[key] = (per_cell, meta)
    return cache[key]


def external_node_weights(args, cache: dict) -> tuple[pd.Series, dict]:
    """Per-bus weight from an external load source, collapsed over cells.

    The static read, which is what decides COVERAGE (`weight > 0`) and therefore
    alpha. Derived from the same tables as external_cell_weights, so the two
    cannot disagree about which buses the source reaches.
    """
    cache_k = (args.map, getattr(args, "external_loads", None))
    cache = cache.setdefault("external", {})
    if cache_k not in cache:
        d, sub, node = _external_tables(args)
        env = sub.groupby(["utility", "substation_name"], as_index=False)["weight"].mean()
        n_nonpos = int((env.weight <= 0).sum())
        env["weight"] = env.weight.clip(lower=0.0)

        map_path = map_path_for(args)
        mapping = pd.read_csv(map_path, dtype={"node": str})
        mapping["utility"] = mapping.utility.astype(str).str.lower()
        j = env.merge(mapping[["utility", "substation_name", "node", "share"]],
                      on=["utility", "substation_name"], how="inner")
        per_node = (j.weight * j.share).groupby(j.node).sum()

        n_node_tbl = int(node.node.nunique()) if len(node) else 0
        if len(node):
            nd = node.groupby("node")["weight"].mean().clip(lower=0.0)
            per_node = per_node.add(nd, fill_value=0.0)

        manifest = d / "manifest.json"
        meta = {
            "weight_definition": "mean external seasonal load x shape per substation",
            "external_dir": rel(d),
            "external_manifest_md5": md5(manifest) if manifest.exists() else None,
            "map_file": rel(map_path), "map_md5": md5(map_path),
            "n_substations": int(len(env)),
            "n_substations_nonpositive": n_nonpos,
            "n_substations_mapped": int(j.substation_name.nunique()),
            "n_nodes_from_node_table": n_node_tbl,
            "n_nodes_weighted": int((per_node > 0).sum()),
        }
        cache[cache_k] = (per_node, meta)
    return cache[cache_k]


def county_first_shares(args, cache: dict) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """County-first allocation: ReEDS sets county energy, alpha splits it inside.

    Each county receives its ReEDS share of the statewide total, so county
    energy is correct BY CONSTRUCTION and no top-off is needed -- the shortfall
    that gated top-off exists to patch cannot arise.

    Inside a county with `u` uncovered buses and `s` substation-mapped buses:

        uncovered bus    ->  alpha * E_c / u                     (equal split)
        substation bus i ->  (1 - alpha) * E_c * w_i / sum_c w   (envelope weight)

    alpha is the share handed to the uncovered pool, so the two named methods
    are exact special cases:
      --alpha ratio : alpha = u/n per county. The uncovered buses collectively
                      receive u/n * E_c, i.e. exactly the equal share they would
                      get from an even split over all n buses, while the
                      substation buses' s/n * E_c is re-apportioned among them by
                      envelope weight.  Every bus is loaded.
      --alpha 0     : the county's whole energy goes to its substation buses by
                      weight; uncovered buses get nothing.  Fewer buses loaded.
    A county with no weighted substation bus is forced to alpha = 1 (equal split
    over all its buses) -- there is nothing to weight.  A county with no
    uncovered bus is forced to alpha = 0, since the equal pool has nowhere to go.

    The within-county weight `w_i` comes from `county_node_weights`: the max-load
    envelope by default, Approach 2's per-substation output when
    `args.county_weights == "stoch"` (see `county_weight_src`).  Nothing else here
    depends on which -- the ReEDS county totals, alpha, the equal pool and the
    renormalisation are all weight-source agnostic.
    """
    per_node, meta_w = county_node_weights(args, cache)
    node_county = candidate_pool(args)

    reeds = reeds_county_annual()
    reeds = reeds[reeds.year == args.county_year][["fips_int", "reeds_mwh"]]
    if reeds.empty:
        raise ValueError(f"ReEDS county reference has no year {args.county_year}")

    df = node_county.copy()
    df["weight"] = df.node.map(per_node).fillna(0.0)
    counties = df.merge(reeds, on="fips_int", how="left")
    missing = counties[counties.reeds_mwh.isna()].county_name.unique()
    if len(missing):
        raise ValueError(f"counties with CATS buses but no ReEDS weight: {list(missing)}")

    total_ref = counties.groupby("fips_int").reeds_mwh.first().sum()
    rows, detail = [], []
    n_unc_fallback = 0   # counties whose uncovered buses carry no CATS load
    for fips, g in counties.groupby("fips_int"):
        e_c = float(g.reeds_mwh.iloc[0]) / total_ref      # county share of the state
        cov = g[g.weight > 0]
        unc = g[g.weight <= 0]
        n, s, u = len(g), len(cov), len(unc)

        if s == 0:
            alpha = 1.0
        elif u == 0:
            alpha = 0.0
        elif args.alpha == "ratio":
            alpha = u / n
        else:
            alpha = float(args.alpha)

        if u:
            # pool label stays "equal" -- it names the UNCOVERED pool, which
            # expand_shares_to_cells carries through unchanged; its internal split
            # is equal or CATS-proportional per uncovered_src(args)
            w = None
            if uncovered_src(args) == "cats":
                w = unc.node.map(cats_bus_demand()).fillna(0.0).clip(lower=0.0)
                if float(w.sum()) <= 0:
                    w, n_unc_fallback = None, n_unc_fallback + 1
            if w is None:
                rows += [{"node": nd, "share": alpha * e_c / u, "pool": "equal"}
                         for nd in unc.node]
            else:
                wsum_u = float(w.sum())
                rows += [{"node": nd, "share": alpha * e_c * wi / wsum_u,
                          "pool": "equal"}
                         for nd, wi in zip(unc.node, w)]
        if s:
            wsum = cov.weight.sum()
            rows += [{"node": nd, "share": (1 - alpha) * e_c * w / wsum,
                      "pool": "envelope"}
                     for nd, w in zip(cov.node, cov.weight)]
        detail.append({
            "fips_int": fips, "county_name": g.county_name.iloc[0],
            "county_share": e_c, "n_nodes": n, "n_substation_nodes": s,
            "n_uncovered_nodes": u, "alpha": alpha,
            "equal_pool_share": alpha * e_c, "envelope_pool_share": (1 - alpha) * e_c,
        })

    shares = pd.DataFrame(rows).groupby("node", as_index=False).agg(
        share=("share", "sum"), pool=("pool", "first"))
    shares = shares.merge(node_county, on="node", how="left")
    shares["method_mwh"] = shares.node.map(per_node).fillna(0.0)
    shares["topup_mwh"] = 0.0
    shares["allocated_mwh"] = shares.share
    shares["covered"] = shares.method_mwh > 0
    shares = shares[shares.share > 0].reset_index(drop=True)
    shares["share"] = shares.share / shares.share.sum()

    county_detail = pd.DataFrame(detail)
    meta = {
        "allocation": "county_first", "alpha": args.alpha,
        "county_weights": county_weight_src(args),
        "pool": pool_src(args),
        "bus_types": bus_types_src(args),
        "uncovered_split": uncovered_src(args),
        "n_counties_uncovered_fallback_to_equal": n_unc_fallback,
        "county_reference_year": args.county_year,
        "weights": meta_w,
        "n_counties": int(len(county_detail)),
        "n_counties_no_substation": int((county_detail.n_substation_nodes == 0).sum()),
        "n_nodes_with_share": int(len(shares)),
        "envelope_governed_share": float(
            county_detail.envelope_pool_share.sum() / county_detail.county_share.sum()),
        "topoff_fraction": 0.0,   # structurally zero: county totals are exact
    }
    return shares, county_detail, meta


def expand_shares_to_cells(shares: pd.DataFrame, county_detail: pd.DataFrame,
                           args, cache: dict, cells: set) -> tuple[pd.DataFrame, dict]:
    """Turn the static county-first share vector into one share vector per cell.

    Only the split *within* a county's substation buses varies by (month, hour):
    which buses are covered, and therefore alpha and the size of each pool, are
    structural facts about where substations exist and do not change hour to
    hour.  So the equal pool is carried through unchanged and only the envelope
    pool is re-split cell by cell.
    """
    cell_w, meta = county_cell_weights(args, cache, cells)
    cell_df = pd.DataFrame(sorted(cells), columns=["month", "hour_pst"])
    det = county_detail.set_index("fips_int")

    eq = shares[shares.pool == "equal"][["node", "fips_int", "share"]]
    eq = eq.merge(cell_df, how="cross") if len(eq) else eq.assign(month=[], hour_pst=[])

    cov = shares[shares.pool == "envelope"][["node", "fips_int"]]
    cw = cov.merge(cell_w, on="node", how="left")
    cw["weight"] = cw.weight.fillna(0.0)
    wsum = cw.groupby(["fips_int", "month", "hour_pst"])["weight"].transform("sum")
    pool = cw.fips_int.map(det.envelope_pool_share)
    cw["share"] = np.where(wsum > 0, pool * cw.weight / wsum.where(wsum > 0), 0.0)

    # a county whose covered buses all measure zero in some cell has no way to
    # split its envelope pool there; hand that cell's pool to its uncovered buses
    dead = cw.loc[wsum <= 0, ["fips_int", "month", "hour_pst"]].drop_duplicates()
    n_dead = len(dead)
    if n_dead and len(eq):
        add = eq.merge(dead, on=["fips_int", "month", "hour_pst"], how="inner")
        if uncovered_src(args) == "cats":
            # hand the dead cell's envelope pool out on the SAME basis the
            # uncovered pool already uses, not a flat split
            w = add.node.map(cats_bus_demand()).fillna(0.0).clip(lower=0.0)
            tot = w.groupby(add.fips_int).transform("sum")
            frac = np.where(tot > 0, w / tot.where(tot > 0),
                            1.0 / add.fips_int.map(det.n_uncovered_nodes))
        else:
            frac = 1.0 / add.fips_int.map(det.n_uncovered_nodes)
        add["share"] = add.fips_int.map(det.envelope_pool_share) * frac
        eq = pd.concat([eq, add[eq.columns]], ignore_index=True)

    out = pd.concat([eq[["node", "month", "hour_pst", "share"]],
                     cw[["node", "month", "hour_pst", "share"]]], ignore_index=True)
    out = out.groupby(["node", "month", "hour_pst"], as_index=False)["share"].sum()
    out = out[out.share > 0]
    tot = out.groupby(["month", "hour_pst"])["share"].transform("sum")
    out["share"] = out.share / tot          # exactly 1 per cell
    meta["n_cells_with_no_covered_weight"] = int(n_dead)
    meta["n_nodes_any_cell"] = int(out.node.nunique())
    return out, meta


def stoch_pool_shares(args, cache: dict) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Pool-and-redistribute allocation driven by Approach 2 (stochastic).

    Unlike the county-first family, this does NOT impose county totals from an
    external reference.  It sweeps a pool of load off the network and lets the
    stochastic model's own spatial structure deal it back out, so county totals
    EMERGE rather than being set -- which is the substantive contrast with the
    ReEDS method, and the reason Approach 2 counts as the "more complicated"
    input here.

    What goes into the pool is controlled by `--stoch-gate`, a county coverage
    threshold (substation buses / candidate buses):

      county coverage >= gate   ALL of that county's buses are swept, including
                                the ones no substation maps to
      county coverage <  gate   only its substation buses are swept; every other
                                bus keeps its control value untouched

    So the gate is the single knob separating the two designs:
      --stoch-gate 0.30  "broad"  -- high-coverage counties are fully re-dealt,
                          so their uncovered buses change too (more buses moved)
      --stoch-gate 2.0   "narrow" -- no county qualifies, so only substation
                          buses anywhere are re-dealt (fewer buses moved)

    Recipients are always every substation bus with stochastic load -- the pool
    is dealt in proportion to it.  With `--stoch-topoff equal` the uncovered
    buses that were swept in are kept alive rather than zeroed: they take
    beta = |U|/|S| of the pool as an equal split (the count ratio, exactly the
    role alpha = u/n plays in the county-first family) and the substation buses
    split the remaining 1 - beta.  Top-off is meaningless without a gate, since
    then nothing uncovered is ever swept.

    Statewide hourly load is conserved because the pool is redistributed within
    itself and every bus outside it is copied through unchanged.
    """
    sub, meta_w = substation_annual(args)
    per_node, meta_m = substation_to_node(sub, map_path_for(args))

    node_county = candidate_buses().copy()
    node_county["weight"] = node_county.node.map(per_node).fillna(0.0)
    node_county["covered"] = node_county.weight > 0

    cov = node_county.groupby(["fips_int", "county_name"], as_index=False).agg(
        n_nodes=("node", "size"), n_covered=("covered", "sum"))
    cov["coverage"] = cov.n_covered / cov.n_nodes
    cov["gated"] = cov.coverage >= args.stoch_gate
    gated = set(cov.loc[cov.gated, "fips_int"])

    # the swept set: everything in a gated county, plus substation buses elsewhere
    node_county["swept"] = node_county.fips_int.isin(gated) | node_county.covered
    swept = node_county[node_county.swept].copy()
    n_unc = int((~swept.covered).sum())
    beta = (n_unc / len(swept)) if (args.stoch_topoff == "equal" and n_unc) else 0.0

    wsum = swept.loc[swept.covered, "weight"].sum()
    if wsum <= 0:
        raise ValueError("no substation bus carries stochastic load")
    swept["share"] = np.where(
        swept.covered, (1 - beta) * swept.weight / wsum,
        (beta / n_unc) if n_unc else 0.0)
    swept["share"] = swept.share / swept.share.sum()

    shares = swept[["node", "county_name", "fips_int", "weight", "covered", "share"]].copy()
    shares = shares.rename(columns={"weight": "method_mwh"})
    shares["topup_mwh"] = np.where(shares.covered, 0.0, shares.share)
    shares["allocated_mwh"] = shares.share
    # every swept bus stays in the frame even at share 0, so the redistribution
    # actually zeroes the ones the model gives nothing to
    shares["covered"] = True          # 'covered' marks pool membership downstream
    shares["is_substation_bus"] = swept.covered.to_numpy()

    meta = {
        "allocation": "stochastic_pool", "stoch_gate": args.stoch_gate,
        "stoch_topoff": args.stoch_topoff, "draw": args.draw,
        "weights": meta_w, "mapping": meta_m,
        "n_counties_gated": int(len(gated)),
        "n_buses_swept": int(len(swept)),
        "n_buses_substation": int(swept.covered.sum()),
        "n_buses_uncovered_swept": n_unc,
        "beta_equal_pool": float(beta),
        "topoff_fraction": float(beta),
    }
    cov["in_pool_nodes"] = cov.fips_int.map(
        swept.groupby("fips_int").size()).fillna(0).astype(int)
    return shares, cov, meta


def expand_stoch_shares_to_cells(shares: pd.DataFrame, args, cache: dict,
                                 cells: set, beta: float
                                 ) -> tuple[pd.DataFrame, dict]:
    """Per-cell share vectors for the stochastic pool.

    The SWEPT SET and beta are structural -- which counties gate and which buses
    map to substations does not change hour to hour -- so they stay fixed.  Only
    the split of the substation buses' (1 - beta) varies cell by cell, following
    Approach 2's own per-cell output; the uncovered swept buses' equal
    beta-split is constant.  Zero-share rows are kept so the redistribution
    still zeroes swept buses the model gives nothing to.
    """
    cell_w, meta = stoch_cell_weights(args, cache, cells)
    cell_df = pd.DataFrame(sorted(cells), columns=["month", "hour_pst"])

    unc = shares.loc[~shares.is_substation_bus, ["node", "share"]]
    unc = (unc.merge(cell_df, how="cross") if len(unc)
           else unc.assign(month=pd.Series(dtype=int), hour_pst=pd.Series(dtype=int)))

    sub = shares.loc[shares.is_substation_bus, ["node"]]
    cw = sub.merge(cell_df, how="cross").merge(
        cell_w, on=["node", "month", "hour_pst"], how="left")
    cw["weight"] = cw.weight.fillna(0.0)
    wsum = cw.groupby(["month", "hour_pst"])["weight"].transform("sum")
    if (wsum <= 0).any():
        bad = cw.loc[wsum <= 0, ["month", "hour_pst"]].drop_duplicates()
        raise ValueError(
            f"stochastic pool has zero total weight in {len(bad)} cell(s), e.g. "
            f"{bad.values.tolist()[:5]} -- cannot split the pool there")
    cw["share"] = (1 - beta) * cw.weight / wsum

    out = pd.concat([unc[["node", "month", "hour_pst", "share"]],
                     cw[["node", "month", "hour_pst", "share"]]], ignore_index=True)
    tot = out.groupby(["month", "hour_pst"])["share"].transform("sum")
    out["share"] = out.share / tot          # exactly 1 per cell
    meta["beta_equal_pool"] = float(beta)
    meta["n_nodes_any_cell"] = int(out.node.nunique())
    return out, meta


def env_hold_shares(args, cache: dict) -> tuple[pd.DataFrame, None, dict]:
    """Envelope-weighted hold: the minimum-intervention contrast.

    Re-splits ONLY the load the control places on covered buses (buses some
    substation maps to), among those same buses, in proportion to the
    substations' own mean max_load envelope; every other bus is copied through
    untouched.  Uses no ReEDS input of any kind and no projection model -- the
    weights are the utilities' measured envelopes, full stop.  Replaced the
    former Approach 1 (ReEDS substation-first) hold 2026-08-13, which carried
    ReEDS load levels in violation of the weights-only rule.
    """
    per_node, meta_w = envelope_node_weights(args, cache)
    node_county = candidate_buses()
    shares = pd.DataFrame({"node": per_node.index.astype(str),
                           "method_mwh": per_node.values})
    shares = shares[shares.method_mwh > 0].reset_index(drop=True)
    shares = shares.merge(node_county[["node", "county_name", "fips_int"]],
                          on="node", how="left")
    shares["topup_mwh"] = 0.0
    shares["allocated_mwh"] = shares.method_mwh
    shares["covered"] = True
    shares["share"] = shares.method_mwh / shares.method_mwh.sum()
    meta = {"allocation": "envelope_hold", "weights": meta_w,
            "n_nodes_with_share": int(len(shares)), "topoff_fraction": 0.0}
    return shares, None, meta


def expand_env_shares_to_cells(shares: pd.DataFrame, args, cache: dict,
                               cells: set) -> tuple[pd.DataFrame, dict]:
    """Per-cell share vectors for the envelope hold: the covered pool is fixed,
    its internal split follows each cell's own envelope weights."""
    cell_w, meta = envelope_cell_weights(args, cache, cells)
    cell_df = pd.DataFrame(sorted(cells), columns=["month", "hour_pst"])
    cw = shares[["node"]].merge(cell_df, how="cross").merge(
        cell_w, on=["node", "month", "hour_pst"], how="left")
    cw["weight"] = cw.weight.fillna(0.0)
    wsum = cw.groupby(["month", "hour_pst"])["weight"].transform("sum")
    if (wsum <= 0).any():
        bad = cw.loc[wsum <= 0, ["month", "hour_pst"]].drop_duplicates()
        raise ValueError(f"envelope pool has zero total weight in {len(bad)} cell(s)")
    cw["share"] = cw.weight / wsum
    meta["n_nodes_any_cell"] = int(cw.node.nunique())
    return cw[["node", "month", "hour_pst", "share"]], meta


def build_shares(args, cache: dict) -> tuple[pd.DataFrame, pd.DataFrame | None, dict]:
    """Per-bus share of statewide load, the county detail table, and provenance."""
    if args.weights == "reedsco":
        return county_first_shares(args, cache)
    if args.weights == "stoch":
        return stoch_pool_shares(args, cache)
    if args.weights == "env":
        return env_hold_shares(args, cache)
    raise ValueError(f"unknown weights source {args.weights!r}")


# --------------------------------------------------------------------------
# Rescale one season
# --------------------------------------------------------------------------

def redistribution_mode(args) -> str:
    """'full' (share vector is the whole allocation) or 'hold' (partial).

    A county-first run's share vector already covers every county's entire
    energy, so it always redistributes fully.  The stochastic pool and the
    envelope hold both move only the load inside their pool -- every bus
    outside it is copied through untouched -- so they take the hold path.
    """
    return "full" if args.weights == "reedsco" else "hold"


def rescale_season(demand: GenXDemand, shares: pd.DataFrame, mode: str,
                   hours: list[tuple[int, int]] | None = None
                   ) -> tuple[np.ndarray, dict]:
    """Rewrite the bus split of one seasonal demand file, hour totals fixed.

    Two coverage modes, both conserving `sum_i d[t,i]` exactly:
      hold -- only the load the control puts on covered buses is redistributed
              (over those same buses); every other column is copied verbatim.
      full -- the whole statewide total is redistributed over the share vector,
              so buses outside it go to zero.

    `shares` is either one share per bus, or -- when `hours` gives this season's
    (month, hour_pst) per timestep -- one share per bus per cell, in which case
    each hour uses its own cell's vector and the bus split varies through the
    week.  Rounding back to the control's 0.1 MW grid uses largest-remainder
    apportionment against the control's own printed hourly totals, so
    conservation survives serialization rather than only holding in float.
    """
    zidx = demand.zone_index()
    missing = [n for n in shares.node.unique() if n not in zidx]
    if missing:
        raise ValueError(
            f"{len(missing)} bus id(s) in the share vector have no "
            f"{ZONE_PREFIX}* column (e.g. {missing[:5]}); node ids and GenX zone "
            f"ids must be the same identifier space")
    control = demand.values
    by_cell = hours is not None and "month" in shares.columns

    if by_cell:
        # per-cell shares work in BOTH coverage modes: 'full' redistributes each
        # hour's statewide total over that hour's cell vector; 'hold' does the
        # same but only with the load the control puts on the pool's buses,
        # copying every other column through verbatim
        nodes = sorted(shares.node.unique())
        pos = {n: k for k, n in enumerate(nodes)}
        cols = np.array([zidx[n] for n in nodes])
        cell_vec: dict[tuple[int, int], np.ndarray] = {}
        for (m, h), g in shares.groupby(["month", "hour_pst"]):
            v = np.zeros(len(nodes))
            v[[pos[n] for n in g.node]] = g.share.to_numpy(float)
            if v.sum() <= 0:
                raise ValueError(f"cell ({m},{h}) has an all-zero share vector")
            cell_vec[(m, h)] = v / v.sum()
        unknown = [c for c in set(hours) if c not in cell_vec]
        if unknown:
            raise ValueError(f"no share vector for cell(s) {sorted(unknown)[:5]}")
        if mode == "hold":
            block_target = control[:, cols].sum(axis=1)  # pool load, per hour
            new = control.copy()
        else:
            block_target = control.sum(axis=1)           # statewide, per hour
            new = np.zeros_like(control)
        block = np.vstack([block_target[t] * cell_vec[c] for t, c in enumerate(hours)])
    elif mode == "hold":
        cols = np.array([zidx[n] for n in shares.node])
        s = shares.share.to_numpy(dtype=np.float64)
        keep = shares.covered.to_numpy()
        cols, s = cols[keep], s[keep]
        if s.sum() <= 0:
            raise ValueError("hold mode has no covered bus with load")
        s = s / s.sum()
        block_target = control[:, cols].sum(axis=1)  # covered pool, per hour
        new = control.copy()
        block = np.outer(block_target, s)
    else:
        cols = np.array([zidx[n] for n in shares.node])
        s = shares.share.to_numpy(dtype=np.float64)
        s = s / s.sum()
        block_target = control.sum(axis=1)           # statewide, per hour
        new = np.zeros_like(control)
        block = np.outer(block_target, s)
    dev_preround = float(np.abs(block.sum(axis=1) - block_target).max())
    new[:, cols] = round_to_printed(block, block_target, decimals=1)

    printed_dev = float(np.abs(np.rint(new.sum(axis=1) * 10)
                               - np.rint(control.sum(axis=1) * 10)).max()) / 10
    if dev_preround > 1e-6 or printed_dev > 0:
        raise AssertionError(
            f"hourly conservation failed: pre-round {dev_preround:.3e} MW, "
            f"printed {printed_dev:.3f} MW")

    delta = new.sum(axis=0) - control.sum(axis=0)
    checks = {
        "n_hours": int(demand.n_hours),
        "max_abs_hourly_dev_preround_mw": dev_preround,
        "max_abs_hourly_dev_printed_mw": printed_dev,
        "control_total_mwh": float(control.sum()),
        "rescaled_total_mwh": float(new.sum()),
        "coverage_share": float(control[:, cols].sum() / control.sum()),
        "n_zones_gaining": int((delta > 1e-9).sum()),
        "n_zones_losing": int((delta < -1e-9).sum()),
        "n_zones_unchanged": int((np.abs(delta) <= 1e-9).sum()),
        "n_zones_newly_nonzero": int(((control.sum(axis=0) == 0) & (new.sum(axis=0) > 0)).sum()),
        "n_zones_zeroed": int(((control.sum(axis=0) > 0) & (new.sum(axis=0) == 0)).sum()),
        "peak_zone_mw_control": float(control.max()),
        "peak_zone_mw_rescaled": float(new.max()),
    }
    return new, checks


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def rel(path: Path) -> str:
    """Repo-relative path for display/manifest, tolerating outside-repo roots."""
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def git_rev() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def run_one(args, tree, cache: dict) -> Path:
    tag = run_tag(args)
    out_dir = Path(args.out_root) / tag
    print(f"\n=== {tag} ===")

    manifest = {
        "run_tag": tag,
        "axes": {"weights": args.weights, "map": args.map, "alpha": args.alpha,
                 "stoch_gate": args.stoch_gate, "stoch_topoff": args.stoch_topoff,
                 "draw": args.draw, "level": args.level},
        "float_format": args.float_format,
        "git_rev": git_rev(),
        "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
        "control_tree": rel(tree.root),
        "control_md5": tree.season_md5,
        "seasons": {},
    }

    if args.weights == "control":
        shares = None
        print("  control passthrough: writing the control demand unchanged")
    else:
        # built before the output dir exists so a failure here (missing run,
        # too few draws) leaves no half-empty run folder behind
        shares, county_detail, meta = build_shares(args, cache)
        if args.level == "monthhour":
            cal = cache.setdefault("calendar", load_rep_week_calendar())
            cells = {c for season in tree.seasons for c in cal[season]}
            if args.weights == "reedsco":
                shares, meta_c = expand_shares_to_cells(
                    shares, county_detail, args, cache, cells)
            elif args.weights == "stoch":
                shares, meta_c = expand_stoch_shares_to_cells(
                    shares, args, cache, cells, meta["beta_equal_pool"])
            else:   # env
                shares, meta_c = expand_env_shares_to_cells(shares, args, cache, cells)
            meta["cells"] = meta_c
            print(f"  month-hour shares over {len(cells)} cells "
                  f"({meta_c['n_nodes_any_cell']:,} buses)")
        manifest["provenance"] = meta
        out_dir.mkdir(parents=True, exist_ok=True)
        shares.round(12).to_csv(out_dir / "node_shares.csv", index=False)
        if county_detail is not None:
            # shares carry real information at small magnitudes, so they are not
            # rounded to the 2 dp that suits the MWh columns
            name = {"reedsco": "county_allocation.csv",
                    "stoch": "pool_counties.csv"}.get(args.weights, "county_detail.csv")
            county_detail.round(8).to_csv(out_dir / name, index=False)
        n_bus = shares.node.nunique()
        print(f"  {n_bus:,} buses carry share"
              + (f" across {len(shares):,} bus-cell rows" if args.level == "monthhour" else "")
              + f"; top-off fraction {meta.get('topoff_fraction', 0.0):.4f}")

    out_dir.mkdir(parents=True, exist_ok=True)
    for season, case in tree.canonical.items():
        # cached across matrix runs: the 4 control files are ~6 MB each and
        # every run of the matrix reads the same ones
        demand = cache.setdefault("demand", {}).setdefault(
            season, read_demand(tree.demand_path(case)))
        if shares is None:
            values, checks = demand.values, {
                "n_hours": int(demand.n_hours),
                "max_abs_hourly_dev_preround_mw": 0.0,
                "max_abs_hourly_dev_printed_mw": 0.0,
                "control_total_mwh": float(demand.values.sum()),
                "rescaled_total_mwh": float(demand.values.sum()),
            }
        else:
            hrs = cache["calendar"][season] if args.level == "monthhour" else None
            values, checks = rescale_season(
                demand, shares, redistribution_mode(args), hours=hrs)
        out_path = out_dir / f"Demand_data__{season}.csv"
        write_demand(out_path, demand, values, float_format=args.float_format)
        checks["source_case"] = case
        checks["output_md5"] = md5(out_path)
        manifest["seasons"][season] = checks
        print(f"  {season:7s} <- {case:4s}  total {checks['control_total_mwh']:,.1f} MWh"
              f" -> {checks['rescaled_total_mwh']:,.1f} MWh"
              f"  (hourly dev {checks['max_abs_hourly_dev_printed_mw']:.1f} MW)")

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"  wrote {rel(out_dir)}")
    return out_dir


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--weights", choices=["reedsco", "stoch", "env", "control"],
                    default="reedsco",
                    help="reedsco: county-first (ReEDS county WEIGHTS + envelope "
                         "split inside, see --alpha); stoch: Approach 2 pool-and-"
                         "redistribute (requires --level monthhour); env: "
                         "envelope-weighted hold (minimum intervention, no ReEDS)")
    ap.add_argument("--map", choices=list(MAP_FILES), default="prox")
    ap.add_argument("--county-year", type=int, default=None,
                    help="year of the ReEDS county table whose NORMALIZED shares "
                         "set w_c (default: --year); only the shares are used, "
                         "never the levels")
    ap.add_argument("--stoch-gate", type=float, default=0.30,
                    help="stochastic only: county coverage threshold. Counties at "
                         "or above it have ALL their buses swept into the pool "
                         "(Way 1); use a value >1 so none qualify and only "
                         "substation buses are swept (Way 2)")
    ap.add_argument("--stoch-topoff", choices=["none", "equal"], default="none",
                    help="stochastic only: 'equal' keeps the swept uncovered buses "
                         "alive with beta=|U|/|S| of the pool split evenly; "
                         "meaningless without a gate")
    ap.add_argument("--draw", default="mean",
                    help="stochastic only: 'mean' (average the MC draws) or a draw "
                         "index, e.g. 0, to carry one realization")
    ap.add_argument("--alpha", default="ratio",
                    help="county-first only: share of a county's energy given to its "
                         "UNCOVERED buses as an equal split; the rest goes to its "
                         "substation buses by max-load envelope weight. "
                         "'ratio' = u/n per county (every bus loaded); "
                         "'0' = substation buses only (fewer buses loaded); "
                         "any float in [0,1] also accepted")
    ap.add_argument("--level", choices=["static", "monthhour"], default="static")
    ap.add_argument("--year", type=int, default=2019)
    ap.add_argument("--min-draws", type=int, default=3,
                    help="fail if the stochastic run has fewer MC draws than this "
                         "(guards against accidentally shipping a 1-draw 'mean')")
    ap.add_argument("--stochastic-run",
                    default="stochastic__cats_caiso_target__normal__Fcal__native__calibtgt",
                    help="Approach 2 run folder; default is the CATS-calibrated run "
                         "(F* recomputed on the CATS control demand, not EIA-930)")
    ap.add_argument("--system", default="CATS")
    ap.add_argument("--float-format", choices=["one_decimal", "full"], default="one_decimal")
    ap.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    args = ap.parse_args()
    if args.county_year is None:
        args.county_year = args.year

    if args.weights == "stoch" and args.level != "monthhour":
        sys.exit(
            "--weights stoch MUST run at --level monthhour. Every Approach 2 "
            "parameter (mu, sigma, rho, s) is estimated per (month, hour_pst) "
            "cell; a static stochastic run collapses the model to a rescaled "
            "envelope midpoint and is a null result by construction.")

    if args.level == "monthhour":
        try:
            load_rep_week_calendar()   # fail here, not after minutes of work
        except FileNotFoundError as e:
            sys.exit(f"--level monthhour needs the rep-week calendar.\n\n{e}")

    tree = scenario_seasons()
    print(f"control tree: {len(tree.cases)} cases, {len(tree.seasons)} seasons "
          f"({', '.join(tree.seasons)})")

    run_one(args, tree, cache={})


if __name__ == "__main__":
    main()
