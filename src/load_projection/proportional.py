"""Approach 3 -- coordinate-free proportional disaggregation.

Disaggregate a statewide hourly load series onto an arbitrary external node
system when NO coordinates are available for it.  Every other approach in this
repo routes through California geography (substation coordinates -> nearest bus
-> county polygon -> ReEDS county share); none of that is reachable here, so the
allocation can only be proportional to the node levels the source supplies.
That makes it a distinct method, not a configuration of county-first, hence its
own `Approach N`.

Inputs
------
1. A node table: `node_id` (arbitrary string), optional `base_id` (the join key)
   and `subname`, plus `winter_load` / `summer_load` -- two dimensionless,
   possibly NEGATIVE levels per node.
2. A user-supplied mapping edge list `base_id, substation_name[, utility]`,
   which lets a base borrow an hourly shape from our utility envelopes.  The
   edge list is supplied, never inferred from names here.
3. A statewide hourly target series (hour-beginning, fixed PST).

The construction
----------------
With `Y(c) = sum of y(t) over the hours in cell c` and blocks MayOct / NovApr::

    s_n(c) = l_n(b) * shape_n(c),   shape_n Y-weighted block mean exactly 1

    mapped i:    shape_i  from its base's envelope (or a per-sibling draw)
    M(c)       = sum over mapped of s_i(c)
    R(c)       = 1 - M(c)
    unmapped j:  s_j(c) = R(c) * L_j(b) / sum over unmapped L(b)

Per-cell conservation `sum_n s_n(c) = M(c) + R(c) = 1` is an algebraic identity,
exact for every cell and every sign.  Writing `shape_U(c) = R(c)/Lambda_U(b)`
turns the unmapped branch into `s_j(c) = l_j(b)*shape_U(c)` with `shape_U`'s own
Y-weighted block mean exactly 1, so the construction is uniform:

    EVERY node -- mapped and unmapped -- receives exactly l_n(b)*E(b) of
    block-b energy.  What unmapped nodes give up is SHAPE, not energy.

Rules that are easy to get wrong
--------------------------------
* The shape normalization is **target-energy-weighted**, never the unweighted
  cell mean.  `external_loads.normalized_shapes` divides by the unweighted mean
  and is therefore WRONG here -- it is right in its own context, where every
  downstream site renormalizes per cell and exactness was never promised.  Y(c)
  varies within a block by month day-count (28 vs 31) and by the diurnal swing,
  so a substation whose shape tracks the statewide pattern would overshoot its
  energy and the slack would silently absorb it.
* **Any pointwise transform of `env` is exactness-preserving provided the
  Y-weighted renormalization happens AFTER it.  Clipping after normalization is
  not.**  This is why `--shape-floor` clips before `energy_normalize`, and why
  the GenX `clip(lower=0)` on stochastic cell means is deliberately NOT
  inherited: there it guards a per-cell denominator, here the levels are already
  signed and the slack absorbs the residual.
* The slack basis is the UNMAPPED set and that is **forced by exactness, not
  chosen**.  Absorbing R(c) on all nodes gives mapped `l_n E (1+Lambda_U)`;
  splitting by `|L_j|` hands a generation bus positive load.  Both are wrong.
* Feasibility is a theorem, not a bug.  Per-cell conservation forces the
  unmapped set to carry exactly R(c); exactness forces R's Y-mean to be exactly
  Lambda_U; the only freedom left is the split AMONG unmapped nodes, which
  changes neither aggregate.  When Lambda_U is small relative to the mapped
  fleet's shape dispersion, NO construction satisfies conservation + exact
  energy + bounded unmapped shapes at once.  Hence the ladder:
  `negative-nodes zero` -> `shape-common strip` -> `shape flat` ->
  `conserve renorm`.  `fleet` is NOT a safe rung.
* The node table's ABSOLUTE SCALE IS DISCARDED (unlike `external_loads`, where
  it cancels).  Only ratios survive; the level comes entirely from the target.
* 288 (month, hour_pst) cells is the resolution ceiling, because that is what
  the utility envelopes are published at.  Within a cell every node's share is
  constant, so all intra-cell variation in the output comes from y(t).
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from . import cells as _cells
from .external_loads import SEASON_COL_BLOCK

__all__ = [
    "REQUIRED_NODE_COLS", "NEGATIVE_NODE_MODES", "CONSERVE_MODES",
    "SHAPE_SOURCES", "SIBLING_DRAW_MODES", "SIGMA_MODES", "SHAPE_COMMON_MODES",
    "Guards", "net_to_gross", "read_node_table", "node_level_shares",
    "read_mapping", "target_cell_energy", "envelope_surface", "stoch_surface",
    "fleet_surface", "base_envelope", "energy_normalize", "strip_common",
    "sibling_draws", "broadcast_to_siblings", "node_shares",
    "realized_energy_shares", "node_annual_and_peak", "sanitize_node_ids",
]

#: the node table's own required columns.  `base_id` and `subname` are optional
#: and `base_id` defaults to `node_id`, which recovers the one-level case.
REQUIRED_NODE_COLS = ("node_id", "winter_load", "summer_load")

#: how a negative level is treated.  EVERY mode is nothing more than a different
#: definition of l_n(b) -- the share construction, the slack absorption, every
#: guard and the exactness proof are strictly downstream and mode-agnostic.
#:   net-base     a base is allocated its signed sibling NET, then split among
#:                its POSITIVE siblings only.  Default: if the table is a
#:                power-flow case, a base's net is what that location draws, and
#:                a net-of-BTM target should be allocated on net draw.
#:   zero         negative nodes excluded outright; a base gets its positive sum
#:   participate  signed shares; a negative node carries negative demand
#:   refuse       abort, listing them
NEGATIVE_NODE_MODES = ("net-base", "zero", "participate", "refuse")

CONSERVE_MODES = ("slack", "renorm")
SHAPE_SOURCES = ("envelope", "stoch", "fleet", "flat")
SHAPE_COMMON_MODES = ("keep", "strip")
SIBLING_DRAW_MODES = ("off", "independent")

#: how a sibling's sigma is derived from its base's.
#:   proportional  sigma_n = k_n * sigma_base with mu_n = k_n * mu_base.  The
#:                 user's approved formula.  NOTE k_n CANCELS exactly in the
#:                 shape, so what this buys is the independent eps, not the
#:                 sigma scaling; every sibling ends up with the same cv.
#:   quadrature    mu_n = k_n * mu_base but sigma_n = sqrt(k_n) * sigma_base --
#:                 the standard result for splitting a sum into independent
#:                 components, so a smaller sibling IS relatively noisier and
#:                 k_n no longer cancels.
#: Scaling sigma WITHOUT scaling mu is deliberately not offered: it would make a
#: smaller sibling relatively SMOOTHER, which is backwards.
SIGMA_MODES = ("proportional", "quadrature")

#: envelope column used as the raw shape surface.  avg_load = (min+max)/2 is the
#: envelope midpoint; the node level is itself a central value, so pairing it
#: with the central-tendency shape keeps the two consistent.  max_load is the
#: sensitivity option (it is what the GenX envelope weight source uses).
SHAPE_COLS = ("avg_load", "max_load")

_BLOCKS = _cells.HALFYEAR_LABELS  # ("NovApr", "MayOct")


class Guards:
    """Guard thresholds, so a run records what it was checked against."""

    def __init__(self, min_net_gross_total: float = 0.10,
                 min_net_gross_unmapped: float = 0.05,
                 max_slack_amplification: float = 5.0,
                 min_slack_shape: float = 0.0,
                 min_shape_net_gross: float = 0.20,
                 min_renorm_denominator: float = 0.5,
                 on_negative_base: str = "zero"):
        self.min_net_gross_total = float(min_net_gross_total)
        self.min_net_gross_unmapped = float(min_net_gross_unmapped)
        self.max_slack_amplification = float(max_slack_amplification)
        self.min_slack_shape = float(min_slack_shape)
        self.min_shape_net_gross = float(min_shape_net_gross)
        self.min_renorm_denominator = float(min_renorm_denominator)
        if on_negative_base not in ("zero", "refuse"):
            raise ValueError("on_negative_base must be 'zero' or 'refuse'; "
                             f"got {on_negative_base!r}")
        self.on_negative_base = on_negative_base

    def as_dict(self) -> dict:
        return dict(vars(self))


# ---------------------------------------------------------------------------
# Cancellation diagnostic
# ---------------------------------------------------------------------------

def net_to_gross(x) -> float:
    """`|sum(x)| / sum(|x|)` in [0, 1]; 1 = no cancellation, 0 = total.

    The one diagnostic used at three levels (whole table, unmapped set, a single
    unit's envelope over a block).  A small value means a ratio of two
    near-cancelling sums, where a one-unit change at one node moves every share.
    """
    a = np.asarray(x, dtype=np.float64)
    gross = np.abs(a).sum()
    if gross == 0:
        return 0.0
    return float(abs(a.sum()) / gross)


# ---------------------------------------------------------------------------
# Node table
# ---------------------------------------------------------------------------

def read_node_table(path: str | Path) -> pd.DataFrame:
    """Read and validate the node table.  No coordinate logic anywhere.

    Returns `node_id, base_id, subname, winter_load, summer_load` plus every
    other input column, in INPUT ORDER (which is also the output column order --
    see `sanitize_node_ids`).

    `base_id` is an explicit column and is NEVER derived by splitting `node_id`:
    node ids may contain arbitrary symbols, so there is no separator that is
    safe to split on, and a parser would silently mis-group on the first id
    containing the delimiter.  Absent, it defaults to `node_id`.
    """
    path = Path(path)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = [c for c in REQUIRED_NODE_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"{path}: node table missing column(s) {missing}; required "
            f"{list(REQUIRED_NODE_COLS)}, got {list(df.columns)}")

    df["node_id"] = df.node_id.astype(str).str.strip()
    blank = df.node_id == ""
    if blank.any():
        raise ValueError(f"{path}: {int(blank.sum())} row(s) have a blank "
                         f"node_id; it is the identity that carries load")
    dup = df.node_id[df.node_id.duplicated()].unique()
    if len(dup):
        raise ValueError(
            f"{path}: {len(dup)} duplicate node_id(s), e.g. "
            f"{list(dup[:5])}. A duplicate would double-count that node's "
            f"load; collapse or rename them in the input.")

    if "base_id" in df.columns:
        df["base_id"] = df.base_id.astype(str).str.strip()
        blank_b = df.base_id == ""
        if blank_b.any():
            raise ValueError(
                f"{path}: {int(blank_b.sum())} row(s) have a blank base_id "
                f"while the column is present; a blank cannot be joined. Fill "
                f"it (use the node_id itself for a node with no siblings) or "
                f"drop the column entirely for a one-level table.")
    else:
        df["base_id"] = df.node_id
    if "subname" not in df.columns:
        df["subname"] = ""

    for col in SEASON_COL_BLOCK:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    bad = df[~np.isfinite(df.winter_load) | ~np.isfinite(df.summer_load)]
    if len(bad):
        per_col = {c: int((~np.isfinite(bad[c])).sum()) for c in SEASON_COL_BLOCK}
        n_both = int((~np.isfinite(bad.winter_load)
                      & ~np.isfinite(bad.summer_load)).sum())
        raise ValueError(
            f"{path}: {len(bad)} row(s) have a non-finite level, so their "
            f"weight is undefined.\n"
            f"  by season column: "
            + ", ".join(f"{c}: {n}" for c, n in per_col.items()) + "\n"
            f"  rows blank in BOTH seasons: {n_both}\n"
            f"  e.g. {bad.node_id.head(5).tolist()}\n"
            f"These are blank or non-numeric cells: levels are coerced with "
            f"errors='coerce', so a blank, '-', 'N/A' or a value carrying "
            f"units/separators becomes NaN here rather than failing earlier.\n"
            f"Decide what a missing seasonal value MEANS for your source "
            f"before proceeding -- a genuine zero (no load that season) and an "
            f"unknown are not interchangeable, and this code refuses rather "
            f"than guess.")

    spatial = [c for c in ("lat", "lon", "latitude", "longitude", "county",
                           "voltage_kv", "kv") if c in df.columns]
    if spatial:
        print(f"  note: {path.name} carries {spatial}; Approach 3 IGNORES "
              f"these by construction and keeps them as provenance only")
    return df.reset_index(drop=True)


def node_level_shares(nodes: pd.DataFrame, mode: str, guards: Guards
                      ) -> tuple[np.ndarray, np.ndarray, dict]:
    """`l_n(b)` under one of `NEGATIVE_NODE_MODES`.  THE whole negative axis.

    Returns `(levels[n_nodes, 2], l[n_nodes, 2], diag)` where column order is
    `_BLOCKS` = (NovApr, MayOct) and `l` sums to 1 over nodes within each block.
    A non-participating node gets `l = 0` exactly and is written as literal 0.0;
    its original level survives in the node index.

    Everything downstream is mode-agnostic, which is what makes "this base
    should sum to its net" a one-function swap rather than a redesign.
    """
    if mode not in NEGATIVE_NODE_MODES:
        raise ValueError(f"unknown negative-nodes mode {mode!r}; "
                         f"expected one of {NEGATIVE_NODE_MODES}")
    col_for = {b: c for c, b in SEASON_COL_BLOCK.items()}
    levels = np.column_stack([nodes[col_for[b]].to_numpy(dtype=np.float64)
                              for b in _BLOCKS])
    n_nodes = len(nodes)
    l = np.zeros((n_nodes, 2), dtype=np.float64)
    diag: dict = {"mode": mode, "blocks": list(_BLOCKS)}

    if mode == "refuse":
        neg = nodes.node_id[(levels < 0).any(axis=1)].tolist()
        if neg:
            raise ValueError(
                f"{len(neg)} node(s) have a negative level, e.g. {neg[:5]}. "
                f"--negative-nodes refuse was requested. Use 'net-base' "
                f"(default) to allocate a base its signed net, 'zero' to drop "
                f"them, or 'participate' for signed shares.")

    base_codes, base_ids = pd.factorize(nodes.base_id, sort=False)
    n_bases = len(base_ids)

    for bi, bname in enumerate(_BLOCKS):
        L = levels[:, bi]
        if mode in ("zero", "refuse"):
            part = L > 0
            eff = np.where(part, L, 0.0)
        elif mode == "participate":
            part = np.ones(n_nodes, dtype=bool)
            eff = L.copy()
        else:  # net-base
            base_net = np.bincount(base_codes, weights=L, minlength=n_bases)
            base_pos = np.bincount(base_codes, weights=np.maximum(L, 0.0),
                                   minlength=n_bases)
            neg_base = base_net < 0
            if neg_base.any():
                names = [str(base_ids[k]) for k in np.flatnonzero(neg_base)[:5]]
                mass = float(np.abs(base_net[neg_base]).sum())
                gross = float(np.abs(L).sum())
                pct = 100.0 * mass / gross if gross > 0 else 0.0
                msg = (f"block {bname}: {int(neg_base.sum())} base(s) net "
                       f"below zero (|net| total {mass:.4g} = {pct:.3f}% of "
                       f"this block's gross level mass {gross:.4g}), e.g. "
                       f"{names}. Every sibling of such a base is at or below "
                       f"zero, so there is no positive sibling to carry the "
                       f"negative total and putting it on one would invert the "
                       f"sign of a load node. These buses get ZERO load and "
                       f"their level mass is dropped from the denominator; use "
                       f"--negative-nodes participate to let them carry "
                       f"negative load instead.")
                if guards.on_negative_base == "refuse":
                    raise ValueError(msg + " --on-negative-base refuse.")
                print(f"  WARNING {msg} Zeroed; level mass discarded.")
            # a base with net > 0 has a positive sibling by arithmetic
            ok = base_net > 0
            assert (base_pos[ok] > 0).all(), "net>0 base with no positive sibling"
            share = np.where(base_pos[base_codes] > 0,
                             np.maximum(L, 0.0) / np.where(
                                 base_pos[base_codes] > 0,
                                 base_pos[base_codes], 1.0),
                             0.0)
            eff = np.where(ok[base_codes], base_net[base_codes] * share, 0.0)
            part = eff != 0
            diag[f"n_bases_net_negative_{bname}"] = int(neg_base.sum())
            diag[f"negative_base_mass_{bname}"] = float(
                np.abs(base_net[neg_base]).sum())
            diag[f"base_total_diff_vs_zero_{bname}"] = float(
                base_net[ok].sum() - base_pos[ok].sum())

        T = float(eff.sum())
        if not T > 0:
            raise ValueError(
                f"block {bname}: the node table's participating levels sum to "
                f"{T:.6g}. A share of a positive statewide load is undefined "
                f"when the table nets to zero or negative. "
                f"--negative-nodes zero excludes generation buses.")
        kappa = net_to_gross(eff)
        if kappa < guards.min_net_gross_total:
            raise ValueError(
                f"block {bname}: the node table's levels cancel -- net "
                f"{T:.4g} is {kappa:.2%} of gross {np.abs(eff).sum():.4g}, so "
                f"every l_n is a ratio of near-cancelling sums and a one-unit "
                f"change at one node moves every share. Refusing. "
                f"(--min-net-gross-total {guards.min_net_gross_total})")
        l[:, bi] = eff / T
        diag[f"total_level_{bname}"] = T
        diag[f"net_to_gross_{bname}"] = kappa
        diag[f"n_participating_{bname}"] = int(part.sum())

    diag["n_nodes"] = n_nodes
    diag["n_bases"] = n_bases
    diag["n_negative_nodes"] = int((levels < 0).any(axis=1).sum())
    return levels, l, diag


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

def read_mapping(path: str | Path, nodes: pd.DataFrame,
                 profiles: pd.DataFrame) -> pd.DataFrame:
    """Validate the user-supplied edge list.  The node is SUPPLIED, not inferred.

    Expected columns `base_id, substation_name` plus optional `utility` and
    `weight`.  Returns one row per input edge with a `status` column; nothing is
    dropped -- an unresolved edge is reported, never omitted.

    The mapping decides SHAPE ONLY; the level always comes from the node table.

    TODO(out of scope, user 2026-10-07): build this edge list ourselves from a
    name column on the node table, via the collapsed coordinate-free cascade --
    exact (utility, name) -> unambiguous name -> inverted basinSourceDictionary
    -> unresolved, reusing `external_loads.resolve_names` with `cec_index={}`,
    `sub_coords={}` and no lat/lon so its spatial rules 3-4 and the proximity
    tie-break are structurally unreachable.  The user has already built this
    mapping by hand.

    TODO(user 2026-10-08, example file coming): solve the shared-name tie-break.
    48 substation names are shared across utilities and coordinates are what
    resolve them today (47 of the 48 have candidates >= 65 km apart).  With no
    coordinates there is no tie-break, so such an edge is REFUSED here unless it
    carries `utility` -- "names are refused, not guessed" is the standing rule.
    Candidate non-spatial discriminators if it turns out to be needed: the
    `utility` column (already supported), voltage class, or the node system's
    own naming convention.  Do not invent one speculatively.
    """
    path = Path(path)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in ("base_id", "substation_name"):
        if col not in df.columns:
            raise ValueError(f"{path}: mapping missing column {col!r}; "
                             f"got {list(df.columns)}")
    df["base_id"] = df.base_id.astype(str).str.strip()
    df["substation_name"] = df.substation_name.astype(str).str.strip()
    if "utility" in df.columns:
        df["utility"] = df.utility.astype(str).str.strip().str.lower()
    else:
        df["utility"] = ""
    df["weight"] = (pd.to_numeric(df.weight, errors="coerce").fillna(1.0)
                    if "weight" in df.columns else 1.0)
    df["status"] = "ok"

    known_bases = set(nodes.base_id)
    bad_base = ~df.base_id.isin(known_bases)
    if bad_base.any():
        names = df.base_id[bad_base].unique()[:5].tolist()
        one_level = (nodes.base_id == nodes.node_id).all()
        raise ValueError(
            f"{path}: {int(bad_base.sum())} edge(s) name a base_id absent from "
            f"the node table, e.g. {names}. The node table is authoritative "
            f"about existence -- a mapped base with no sibling cannot be given "
            f"energy.\nThe node table is "
            + ("ONE-LEVEL (base_id == node_id), so the edge list must key on "
               "node_id." if one_level else
               "TWO-LEVEL, so the edge list must key on base_id, not node_id."))

    fleet = profiles[["utility", "substation_name"]].drop_duplicates()
    by_name = fleet.groupby("substation_name").utility.apply(set).to_dict()
    pairs = set(map(tuple, fleet.values))
    for i, r in enumerate(df.itertuples()):
        cands = by_name.get(r.substation_name)
        if cands is None:
            df.at[df.index[i], "status"] = (
                f"substation_name not in the profiled fleet")
        elif r.utility:
            if (r.utility, r.substation_name) not in pairs:
                df.at[df.index[i], "status"] = (
                    f"utility {r.utility!r} does not operate a substation named "
                    f"{r.substation_name!r} (known: {sorted(cands)})")
        elif len(cands) > 1:
            df.at[df.index[i], "status"] = (
                f"name used by more than one utility ({sorted(cands)}); supply "
                f"a 'utility' column -- with no coordinates there is no "
                f"tie-break, and this code refuses rather than guess")
        else:
            df.at[df.index[i], "utility"] = next(iter(cands))
    return df


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------

def target_cell_energy(target: pd.DataFrame) -> dict:
    """Cell-level aggregates of the target series.

    `target` needs `month`, `hour_pst`, `demand_mw`.  Returns a dict with
    `cell` (per hour), `Y` (per-cell energy), `block_of_cell`, `E` (per block),
    `ymax`/`ymin` (per cell, for the exact pre-rounding peak).
    """
    spec = _cells.MONTHHOUR
    cell = _cells.encode(spec, target.month, target.hour_pst)
    y = target.demand_mw.to_numpy(dtype=np.float64)
    n = spec.n_cells
    Y = np.bincount(cell, weights=y, minlength=n)
    cnt = np.bincount(cell, minlength=n)
    if (cnt == 0).any():
        print(f"  note: {int((cnt == 0).sum())} of {n} cells are not visited by "
              f"this target; their shares are still emitted but carry no energy")

    half = _cells.get_spec("halfyear")
    months = _cells.label_frame(spec).month.to_numpy()
    block_of_cell = _cells.encode(half, months)
    E = np.array([Y[block_of_cell == b].sum() for b in range(len(_BLOCKS))])
    if not (E > 0).all():
        raise ValueError(
            f"target has non-positive energy in a half-year block: "
            f"{dict(zip(_BLOCKS, E))}. Approach 3 normalizes shapes per block, "
            f"so both blocks must carry energy.")

    ymax = np.full(n, np.nan)
    ymin = np.full(n, np.nan)
    order = np.argsort(cell, kind="stable")
    cs, ys = cell[order], y[order]
    starts = np.searchsorted(cs, np.arange(n), side="left")
    ends = np.searchsorted(cs, np.arange(n), side="right")
    for c in range(n):
        if ends[c] > starts[c]:
            seg = ys[starts[c]:ends[c]]
            ymax[c], ymin[c] = seg.max(), seg.min()
    return {"cell": cell, "Y": Y, "n_hours_per_cell": cnt,
            "block_of_cell": block_of_cell, "E": E,
            "ymax": ymax, "ymin": ymin, "spec": spec}


# ---------------------------------------------------------------------------
# Raw shape surfaces, keyed (utility, substation_name) x cell
# ---------------------------------------------------------------------------

def _surface_from_long(df: pd.DataFrame, value: str, spec) -> tuple[np.ndarray, list, dict]:
    """Pivot a long (utility, substation_name, month, hour_pst, value) frame.

    Missing cells are filled from the unit's own mean over the cells it HAS,
    per block, so a unit absent in one cell does not silently drop out of a
    composite and distort its shape.  This mirrors the GenX reader's
    per-substation fallback and is the reason the final per-unit renormalization
    that `external_loads.build_weight_tables` needs is unnecessary here.
    """
    d = df.copy()
    d["utility"] = d.utility.astype(str).str.lower()
    d["cell"] = _cells.encode(spec, d.month, d.hour_pst)
    units = (d[["utility", "substation_name"]].drop_duplicates()
             .sort_values(["utility", "substation_name"]).reset_index(drop=True))
    key = {t: i for i, t in enumerate(map(tuple, units.values))}
    arr = np.full((spec.n_cells, len(units)), np.nan)
    ui = np.array([key[(u, s)] for u, s in zip(d.utility, d.substation_name)])
    arr[d.cell.to_numpy(), ui] = d[value].to_numpy(dtype=np.float64)

    have = ~np.isnan(arr)
    n_missing = int((~have).sum())
    # per-unit mean over the cells it HAS.  A unit with no data at all (the 6
    # dataless SCE substations) becomes flat 0 and is caught downstream by the
    # net-to-gross shape guard; np.nanmean is avoided so an all-NaN column does
    # not warn.
    cnt = have.sum(axis=0)
    tot = np.where(have, arr, 0.0).sum(axis=0)
    unit_mean = np.where(cnt > 0, tot / np.maximum(cnt, 1), 0.0)
    filled = np.where(have, arr, unit_mean[None, :])
    return filled, list(map(tuple, units.values)), {
        "n_cells_filled": n_missing,
        "n_unit_cells": int(arr.size),
        "n_units_no_data": int((cnt == 0).sum()),
    }


def envelope_surface(profiles_csv: str | Path, shape_col: str = "avg_load",
                     spec=None) -> tuple[np.ndarray, list, dict]:
    """Raw envelope surface from the clean substation profiles.

    Reuses `weights.load_profiles`, which already dedupes the PGE scraper
    overlap by cell mean and synthesizes `avg_load = (min+max)/2`.
    """
    from .weights import load_profiles
    if shape_col not in SHAPE_COLS:
        raise ValueError(f"shape_col must be one of {SHAPE_COLS}; "
                         f"got {shape_col!r}")
    spec = spec or _cells.MONTHHOUR
    prof = load_profiles(profiles_csv, shape_col)
    arr, units, meta = _surface_from_long(prof, shape_col, spec)
    meta["source"] = f"envelope:{shape_col}"
    return arr, units, meta


def stoch_surface(cell_mw_csv: str | Path, draw="mean", min_draws: int = 3,
                  spec=None) -> tuple[np.ndarray, list, dict]:
    """Raw surface from Approach 2's per-cell output (`substation_cell_mw.csv`).

    Mirrors the GenX reader's contract (`--draw mean` averages the draws, an
    integer keeps one realization, `--min-draws` guards against consuming a
    1-draw run) but DELIBERATELY OMITS its `clip(lower=0)`.  Approach 2's
    generator is not truncated at zero, so a negative cell mean is measured
    reverse flow, not noise; here the levels are already signed and the slack
    absorbs the residual, so there is no per-cell denominator for a negative
    value to destroy.  They are counted, never silently dropped.
    """
    spec = spec or _cells.MONTHHOUR
    d = pd.read_csv(cell_mw_csv)
    n_draws = d.draw.nunique()
    if n_draws < min_draws:
        raise ValueError(
            f"{cell_mw_csv} has {n_draws} draw(s), below --min-draws "
            f"{min_draws}. A 1-draw run is almost always an accident; pass "
            f"--min-draws 1 deliberately if it is not.")
    if draw == "mean":
        d = (d.groupby(["utility", "substation_name", "month", "hour_pst"],
                       as_index=False).mean_mw.mean())
    else:
        want = int(draw)
        d = d[d.draw == want]
        if d.empty:
            raise ValueError(f"{cell_mw_csv} has no draw {want}")
    arr, units, meta = _surface_from_long(d, "mean_mw", spec)
    meta["source"] = f"stoch:{draw}"
    meta["n_draws_available"] = int(n_draws)
    meta["n_cells_negative"] = int((arr < 0).sum())
    return arr, units, meta


def fleet_surface(arr: np.ndarray, block_of_cell: np.ndarray) -> np.ndarray:
    """Statewide fleet-mean surface: `[n_cells]`, one shared pattern.

    The coordinate-free fallback.  `external_loads.fallback_shapes` returns a
    county mean ahead of this one, but the county branch needs coordinates and
    is structurally unavailable here, so the fleet mean is the only stand-in.

    NOTE this is NOT a safe rung of the feasibility ladder: with every mapped
    node on one shape, `shape_U(c) = (1 - Lambda_M*fleet(c))/Lambda_U` has the
    same amplification pathology.  Only `flat` is provably safe.
    """
    # arr is already gap-filled, so a plain mean is correct and cannot warn
    return arr.mean(axis=1)


def base_envelope(arr: np.ndarray, units: list, edges: pd.DataFrame
                  ) -> tuple[np.ndarray, list, dict]:
    """Composite raw surface per `base_id`: the SUM of its substations' levels.

    Summing is correct because the shape of a bus fed by two substations is the
    shape of their SUMMED load, which weights each by its own magnitude
    automatically.  A mean of the NORMALIZED shapes would weight a 5 MW
    substation equally with a 300 MW one.  Cells are already gap-filled per
    substation, so a substation absent in one cell cannot drop out here.

    An optional per-edge `weight` scales a substation's contribution.  It is
    inert when a base has exactly one mapped substation (normalization kills a
    scalar), which is the common case.
    """
    idx = {t: i for i, t in enumerate(units)}
    ok = edges[edges.status == "ok"]
    bases = sorted(ok.base_id.unique())
    out = np.zeros((arr.shape[0], len(bases)))
    bpos = {b: i for i, b in enumerate(bases)}
    n_sub = {}
    for r in ok.itertuples():
        j = idx.get((r.utility, r.substation_name))
        if j is None:
            continue
        out[:, bpos[r.base_id]] += float(r.weight) * arr[:, j]
        n_sub[r.base_id] = n_sub.get(r.base_id, 0) + 1
    return out, bases, {"n_bases_mapped": len(bases),
                        "n_substations_used": int(ok.shape[0]),
                        "substations_per_base": n_sub}


# ---------------------------------------------------------------------------
# Normalization -- the exactness-critical step
# ---------------------------------------------------------------------------

def energy_normalize(env: np.ndarray, Y: np.ndarray,
                     block_of_cell: np.ndarray,
                     min_net_gross: float = 0.20
                     ) -> tuple[np.ndarray, dict]:
    """Scale each column so its TARGET-ENERGY-weighted block mean is exactly 1.

        shape(c) = env(c) * sum_{c' in b} Y(c') / sum_{c' in b} Y(c')*env(c')

    This, and NOT the unweighted cell mean, is what makes a node's seasonal
    energy share exactly `l_n(b)`.  See the module docstring.

    A column whose envelope NETS OUT within a block (net-to-gross below
    `min_net_gross`) falls back to flat 1.0 for that block and is counted: no
    pattern can be borrowed from an envelope that cancels, and flat trivially
    has a Y-weighted mean of 1, so exactness survives the fallback.
    """
    env = np.asarray(env, dtype=np.float64)
    if env.ndim == 1:
        env = env[:, None]
    shape = np.ones_like(env)
    n_flat = 0
    for b in range(len(_BLOCKS)):
        m = block_of_cell == b
        w = Y[m]
        den = w.sum()
        num = w @ env[m, :]                      # sum_c Y(c) env(c)
        gross = np.abs(w) @ np.abs(env[m, :])
        ymean = num / den
        with np.errstate(divide="ignore", invalid="ignore"):
            kappa = np.where(gross > 0, np.abs(num) / gross, 0.0)
        good = (kappa >= min_net_gross) & (ymean != 0) & np.isfinite(ymean)
        n_flat += int((~good).sum())
        shape[np.ix_(m, good)] = env[np.ix_(m, good)] / ymean[good]
        # ~good columns keep the initialized 1.0
    return shape, {"n_block_columns_flat": n_flat,
                   "n_columns": int(env.shape[1])}


def _assert_unit_ymean(shape: np.ndarray, Y: np.ndarray,
                       block_of_cell: np.ndarray, what: str,
                       atol: float = 1e-12) -> float:
    worst = 0.0
    for b in range(len(_BLOCKS)):
        m = block_of_cell == b
        got = (Y[m] @ shape[m, :]) / Y[m].sum()
        worst = max(worst, float(np.abs(got - 1.0).max()))
    if worst > atol:
        raise AssertionError(
            f"{what}: Y-weighted block mean of shape deviates from 1 by "
            f"{worst:.3e} (atol {atol:.0e}). Exact seasonal energy depends on "
            f"this being 1 for every column.")
    return worst


def strip_common(shape: np.ndarray, l_mapped: np.ndarray, Y: np.ndarray,
                 block_of_cell: np.ndarray, min_net_gross: float = 0.20
                 ) -> tuple[np.ndarray, dict]:
    """Divide out the mapped fleet's COMMON MODE, then renormalize per column.

        g(c) = M(c)/Lambda_M(b),   shape~ = Y-renormalize(shape(c)/g(c))

    The slack amplification is driven by the common mode -- the part of every
    mapped shape that says how much the WHOLE fleet moves in cell c.  But the
    target y(t) already carries the aggregate time pattern; the envelopes' job
    here is only to say which nodes are big in cell c.  Making the slack fight
    the fleet common mode is the pathology.

    Before the per-column renormalization `M~(c) == Lambda_M` exactly and
    `shape_U == 1`; the renormalization reintroduces only a SECOND-ORDER
    oscillation.  So this keeps exact energy and all cross-node shape
    information while shrinking A(b) substantially.
    """
    g = np.zeros(shape.shape[0])
    for b in range(len(_BLOCKS)):
        m = block_of_cell == b
        lam = l_mapped[:, b].sum()
        if lam == 0:
            g[m] = 1.0
            continue
        g[m] = (shape[m, :] @ l_mapped[:, b]) / lam
    if not np.all(np.isfinite(g)) or np.any(g == 0):
        raise ValueError("the mapped fleet's common mode g(c) hits zero or is "
                         "non-finite; --shape-common strip is not usable here")
    out, meta = energy_normalize(shape / g[:, None], Y, block_of_cell,
                                 min_net_gross)
    meta["common_mode_range"] = (float(g.min()), float(g.max()))
    return out, meta


# ---------------------------------------------------------------------------
# Per-sibling stochastic draws
# ---------------------------------------------------------------------------

def sibling_draws(mu_base: np.ndarray, sigma_base: np.ndarray,
                  rho: np.ndarray, z: np.ndarray, cell: np.ndarray,
                  day_id: np.ndarray, k: np.ndarray, base_of_node: np.ndarray,
                  sigma_mode: str, rng: np.random.Generator
                  ) -> tuple[np.ndarray, dict]:
    """Per-sibling realized per-cell means, each sibling drawing its own eps.

    Follows `stochastic.generate` exactly::

        L_n(t) = mu_n(c) + sigma_n(c)*[sqrt(rho(c))*z(t)
                                       + sqrt(1-rho(c))*eps_n(t)]

    with `eps` ONE draw per unit-DAY, reused across that day's 24 hours, which
    is `generate`'s `eps_mode="daily"` convention.  `z` and `rho(c)` are shared;
    only `eps_n` is per sibling, and that is what decorrelates them.

    sigma_mode (see SIGMA_MODES):
      proportional  mu_n = k_n*mu_base, sigma_n = k_n*sigma_base.  The approved
                    formula.  k_n CANCELS in the shape once the realized means
                    are Y-normalized, so this buys the independent eps and
                    nothing else; every sibling gets the same cv.
      quadrature    mu_n = k_n*mu_base, sigma_n = sqrt(k_n)*sigma_base, so
                    cv_n = cv_base/sqrt(k_n) and a smaller sibling IS
                    relatively noisier.  k_n does not cancel.

    Returns `(cell_mean[n_cells, n_nodes], meta)`.  The caller must still
    `energy_normalize` the result -- the draw supplies a shape, never a level.
    """
    if sigma_mode not in SIGMA_MODES:
        raise ValueError(f"sigma_mode must be one of {SIGMA_MODES}; "
                         f"got {sigma_mode!r}")
    n_nodes = len(k)
    n_cells = mu_base.shape[0]
    kk = np.asarray(k, dtype=np.float64)
    mu_n = mu_base[:, base_of_node] * kk[None, :]
    s_fac = kk if sigma_mode == "proportional" else np.sqrt(np.abs(kk))
    sig_n = sigma_base[:, base_of_node] * s_fac[None, :]

    rc = rho[cell]
    w_common = (np.sqrt(rc) * z)[:, None]
    coef = np.sqrt(np.maximum(0.0, 1.0 - rc))[:, None]
    n_days = int(day_id.max()) + 1
    eps = rng.standard_normal((n_days, n_nodes))[day_id]
    w = w_common + coef * eps

    acc = np.zeros((n_cells, n_nodes))
    cnt = np.zeros(n_cells)
    # chunk the hourly pass so a multi-year target does not materialize
    # [n_hours, n_nodes] all at once
    step = max(1, int(2_000_000 // max(1, n_nodes)))
    for a in range(0, len(cell), step):
        sl = slice(a, min(a + step, len(cell)))
        c = cell[sl]
        vals = mu_n[c, :] + sig_n[c, :] * w[sl, :]
        np.add.at(acc, c, vals)
        np.add.at(cnt, c, 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(cnt[:, None] > 0, acc / np.maximum(cnt, 1)[:, None],
                       np.nan)
    # a cell the target never visits has no realized mean; fall back to the
    # analytic mu so the column is still fully defined
    out = np.where(np.isnan(out), mu_n, out)
    return out, {"sigma_mode": sigma_mode, "n_days": n_days,
                 "n_cells_unvisited": int((cnt == 0).sum())}


def broadcast_to_siblings(base_shape: np.ndarray, base_of_node: np.ndarray
                          ) -> np.ndarray:
    """One shape per base -> every `node_id` under it.

    Nothing is split, because nothing is being divided: a substation contributes
    a PATTERN and a pattern is copyable.  This is the sharp contrast with
    `map_loads_to_nodes`' tie-share rule, where a substation's LOAD is split
    across tied buses -- precisely the thing a reader will assume is a bug here.

    Consequence to state, not bury: siblings of a base then share one shape, so
    their relative split is constant across all cells and Spearman(h10, h18)
    within a base is exactly 1.0.  The subname axis adds LEVEL resolution, never
    SHAPE resolution; `sibling_draws` is the fix.
    """
    return base_shape[:, base_of_node]


# ---------------------------------------------------------------------------
# Shares
# ---------------------------------------------------------------------------

def node_shares(l: np.ndarray, shape: np.ndarray, mapped: np.ndarray,
                levels: np.ndarray, Y: np.ndarray, block_of_cell: np.ndarray,
                conserve: str, guards: Guards) -> tuple[np.ndarray, dict]:
    """`S[n_cells, n_nodes]`, summing to exactly 1 in every cell.

    slack  -- mapped nodes carry their own shape; the unmapped set absorbs the
              per-cell residual, split by its own levels.  Every node then gets
              exactly `l_n(b)*E(b)` of block energy.
    renorm -- divide by `D(c) = sum_n l_n*shape_n(c)` with unmapped shape == 1.
              Keeps per-cell conservation and needs no slack basis, but the
              seasonal energy share is NO LONGER EXACT.  It is not
              unconditionally safer: it trades the Lambda_U guard for one on
              `min_c |D(c)|`, and with signed levels D(c) can cross zero, which
              flips the sign of every node at once.
    """
    if conserve not in CONSERVE_MODES:
        raise ValueError(f"unknown conserve mode {conserve!r}; "
                         f"expected one of {CONSERVE_MODES}")
    n_cells, n_nodes = shape.shape
    S = np.zeros((n_cells, n_nodes))
    unmapped = ~mapped
    diag: dict = {"conserve": conserve,
                  "n_mapped_nodes": int(mapped.sum()),
                  "n_unmapped_nodes": int(unmapped.sum())}

    if conserve == "renorm":
        sh = shape.copy()
        sh[:, unmapped] = 1.0
        D = sh @ l[:, 0]  # placeholder, recomputed per block below
        for b in range(len(_BLOCKS)):
            m = block_of_cell == b
            Db = sh[m, :] @ l[:, b]
            worst = float(np.abs(Db).min())
            if worst < guards.min_renorm_denominator:
                c = int(np.flatnonzero(m)[np.argmin(np.abs(Db))])
                raise ValueError(
                    f"block {_BLOCKS[b]}: the renormalization denominator D(c) "
                    f"falls to {worst:.4g} at cell {c} (--min-renorm-"
                    f"denominator {guards.min_renorm_denominator}). D(c) "
                    f"crossing zero flips the sign of EVERY node at once.")
            S[m, :] = l[None, :, b] * sh[m, :] / Db[:, None]
            diag[f"min_abs_D_{_BLOCKS[b]}"] = worst
        del D
    else:
        if not unmapped.any():
            raise ValueError(
                "every node is mapped, so there is no slack basis to absorb "
                "the per-cell residual. Use --conserve renorm.")
        for b in range(len(_BLOCKS)):
            m = block_of_cell == b
            lam_u = float(l[unmapped, b].sum())
            lam_m = float(l[mapped, b].sum())
            kappa_u = net_to_gross(levels[unmapped, b])
            if kappa_u < guards.min_net_gross_unmapped:
                raise ValueError(
                    f"block {_BLOCKS[b]}: the unmapped levels cancel -- net is "
                    f"{kappa_u:.2%} of gross, so the residual split is "
                    f"numerically meaningless. Ladder: --shape-common strip, "
                    f"--shape-source flat, --conserve renorm, "
                    f"--negative-nodes zero.")
            if lam_u == 0:
                raise ValueError(
                    f"block {_BLOCKS[b]}: the unmapped nodes carry zero level "
                    f"share, so they cannot absorb the residual. "
                    f"Use --conserve renorm.")
            M = shape[m, :][:, mapped] @ l[mapped, b]
            R = 1.0 - M
            shape_U = R / lam_u
            A = float(np.abs(shape_U).max())
            if A > guards.max_slack_amplification:
                w = int(np.flatnonzero(m)[np.argmax(np.abs(shape_U))])
                lbl = _cells.label_frame(_cells.MONTHHOUR).loc[w]
                raise ValueError(
                    f"block {_BLOCKS[b]}: unmapped nodes would see up to "
                    f"{A:.2f}x their own level in cell "
                    f"(month={int(lbl.month)}, hour={int(lbl.hour_pst)}), "
                    f"because the mapped fleet carries Lambda_M={lam_m:.4f} of "
                    f"the levels and only Lambda_U={lam_u:.4f} is left to "
                    f"absorb its shape.\nThis is NOT fixable while keeping both "
                    f"per-cell conservation and exact seasonal energy -- see "
                    f"docs/approach3_proportional.md 'the theorem'.\nLadder: "
                    f"--shape-common strip (free), --shape-source flat "
                    f"(provably safe), --conserve renorm, --negative-nodes "
                    f"zero. (--max-slack-amplification "
                    f"{guards.max_slack_amplification})")
            if shape_U.min() < guards.min_slack_shape:
                nneg = int((shape_U < guards.min_slack_shape).sum())
                raise ValueError(
                    f"block {_BLOCKS[b]}: R(c) changes sign relative to "
                    f"Lambda_U in {nneg} cell(s) (worst shape_U "
                    f"{shape_U.min():.3f}), so every unmapped LOAD bus would "
                    f"become a net exporter in those hours. Pass "
                    f"--min-slack-shape -inf to permit it deliberately.")
            S[np.ix_(m, mapped)] = (l[mapped, b][None, :]
                                    * shape[m, :][:, mapped])
            S[np.ix_(m, unmapped)] = (l[unmapped, b][None, :]
                                      * shape_U[:, None])
            diag[f"lambda_M_{_BLOCKS[b]}"] = lam_m
            diag[f"lambda_U_{_BLOCKS[b]}"] = lam_u
            diag[f"net_to_gross_unmapped_{_BLOCKS[b]}"] = kappa_u
            diag[f"A_{_BLOCKS[b]}"] = A
            diag[f"min_shape_U_{_BLOCKS[b]}"] = float(shape_U.min())

    row = S.sum(axis=1)
    worst = float(np.abs(row - 1.0).max())
    if worst > 1e-9:
        raise AssertionError(
            f"per-cell shares deviate from 1 by {worst:.3e} (atol 1e-9); "
            f"hourly conservation depends on this identity")
    diag["max_abs_cell_share_error"] = worst
    return S, diag


def realized_energy_shares(S: np.ndarray, Y: np.ndarray,
                           block_of_cell: np.ndarray) -> np.ndarray:
    """`[n_nodes, 2]` realized share of each block's energy."""
    out = np.zeros((S.shape[1], len(_BLOCKS)))
    for b in range(len(_BLOCKS)):
        m = block_of_cell == b
        out[:, b] = (Y[m] @ S[m, :]) / Y[m].sum()
    return out


def node_annual_and_peak(S: np.ndarray, Y: np.ndarray, ymax: np.ndarray,
                         ymin: np.ndarray) -> dict:
    """Exact pre-rounding annual energy and peak MW per node.

    The peak needs a SIGN BRANCH: for a negative share the largest magnitude
    comes from the cell's MINIMUM target hour, not its maximum.  The
    non-negative formula in the compact deliverable writer would be wrong here.
    """
    annual = Y @ S
    hi = np.where(np.isnan(ymax), 0.0, ymax)[:, None] * S
    lo = np.where(np.isnan(ymin), 0.0, ymin)[:, None] * S
    cand = np.where(S >= 0, hi, lo)
    peak_cell = np.argmax(cand, axis=0)
    peak = cand[peak_cell, np.arange(S.shape[1])]
    return {"annual_mwh": annual, "peak_mw": peak, "peak_cell": peak_cell}


# ---------------------------------------------------------------------------
# Output column names
# ---------------------------------------------------------------------------

_SAFE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def sanitize_node_ids(node_ids, prefix: str = "z", width: int = 4
                      ) -> tuple[list, pd.DataFrame]:
    """Positional safe column names + the index frame that maps them back.

    POSITIONAL, in node-table order -- deliberately not sorted, because
    `sorted()` on arbitrary strings is locale-unstable and would silently
    reorder the caller's own ordering.  Positional naming is collision-free by
    construction, so no symbol, unicode codepoint, comma, quote, newline or
    leading digit can break the CSV.

    Contrast the CATS convention `Demand_MW_z{bus_i}`, which embeds the real id
    and is parsed back with `int(...)`: that only works for integer ids, which
    is exactly why arbitrary ids need an index file instead.
    """
    ids = list(node_ids)
    w = max(width, len(str(len(ids))))
    cols = [f"{prefix}{i + 1:0{w}d}" for i in range(len(ids))]
    for c in cols[:1]:
        if not _SAFE.match(c):
            raise AssertionError(f"generated column {c!r} is not CSV-safe")
    if len(set(cols)) != len(cols):
        raise AssertionError("sanitized column names are not unique")
    return cols, pd.DataFrame({"column": cols, "node_id": ids})
