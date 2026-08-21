"""Guards for the GenX demand rescaler.

The rescaler's whole claim is that it changes WHERE load sits without changing
HOW MUCH there is, and that it rewrites nothing in the GenX file except the
zone columns.  Both are easy to break silently -- a rounding residual or a
reordered header would still produce a file GenX happily reads -- so they are
asserted here rather than eyeballed:

  1. IO ROUND TRIP -- reading and rewriting an untouched control demand file
     reproduces it byte for byte (md5), for all four seasonal controls.  This
     is what licenses treating the metadata columns as opaque strings.

  2. HOURLY CONSERVATION -- after rescaling, every hour's statewide total
     matches the control both in float and at the 0.1 MW precision the file is
     written in.  Checked against the real controls with a synthetic share
     vector, so it does not depend on any projection run existing.

  3. HOLD-MODE PARTITION -- in hold redistribution (env hold, stochastic pool),
     buses outside the pool keep their control values exactly.

  4. LARGEST-REMAINDER ROUNDING -- row sums land on target, values stay
     non-negative, and no cell moves by more than one rounding unit.

  5. SHARE ACCOUNTING -- shares are non-negative and sum to 1, and top-off
     lands only on buses the method left empty.

  6. PER-CELL (MONTH-HOUR) MODES -- by-cell shares conserve every hour in both
     full and hold redistribution, buses outside the pool stay untouched in
     hold, a pool bus given zero share in every cell is actually zeroed, and
     the bus split genuinely varies hour to hour.

Run:
  python scripts/load_projection/genx/test_genx_rescale.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
from genx_demand_io import (  # noqa: E402
    md5, read_demand, round_to_printed, scenario_seasons, write_demand)
from rescale_genx_demand import rescale_season  # noqa: E402


def test_io_round_trip(tree) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for season, case in tree.canonical.items():
            src = tree.demand_path(case)
            demand = read_demand(src)
            out = Path(tmp) / f"{season}.csv"
            write_demand(out, demand)
            assert md5(src) == md5(out), (
                f"{season} ({case}): round trip is not byte-identical -- the "
                f"control's float formatting differs from '%.1f'")
    print(f"  [OK] all {len(tree.canonical)} seasonal controls round-trip byte-identically")


def test_rounding_unit() -> None:
    rng = np.random.default_rng(0)
    values = rng.random((20, 500)) * 37.0
    targets = values.sum(axis=1)
    out = round_to_printed(values, targets, decimals=1)

    assert (out >= 0).all(), "rounding produced negative load"
    assert np.abs(np.rint(out * 10) - out * 10).max() < 1e-6, "output is off the 0.1 grid"
    assert np.abs(out.sum(axis=1) - np.rint(targets * 10) / 10).max() < 1e-9, \
        "row sums do not hit their targets"
    assert np.abs(out - values).max() <= 0.1 + 1e-9, \
        "a cell moved by more than one rounding unit"
    print("  [OK] largest-remainder rounding hits row targets on the 0.1 MW grid")


def _synthetic_shares(demand, n_covered=400, seed=0) -> pd.DataFrame:
    """A plausible share vector over real zone ids, independent of any run.

    Half the covered buses are drawn from zones the control already loads and
    half from zones it leaves empty, so the test exercises both the
    redistribute-onto-empty and zero-out-a-loaded-bus paths.
    """
    rng = np.random.default_rng(seed)
    zones = np.array(demand.zones)
    loaded = demand.values.sum(axis=0) > 0
    on = rng.choice(zones[loaded], size=n_covered // 2, replace=False)
    off = rng.choice(zones[~loaded], size=n_covered // 2, replace=False)
    node = np.concatenate([on, off])

    method = rng.random(len(node)) * 1000
    method[-50:] = 0.0                       # buses the method leaves empty
    topup = np.zeros(len(node))
    topup[-50:] = rng.random(50) * 200       # ... which is where top-off lands
    alloc = method + topup
    return pd.DataFrame({
        "node": node, "method_mwh": method, "topup_mwh": topup,
        "allocated_mwh": alloc, "covered": method > 0, "share": alloc / alloc.sum(),
    })


def test_conservation_and_partition(tree) -> None:
    case = tree.canonical[tree.seasons[0]]
    demand = read_demand(tree.demand_path(case))
    shares = _synthetic_shares(demand)
    control = demand.values
    zidx = demand.zone_index()

    for mode in ("full", "hold"):
        new, checks = rescale_season(demand, shares, mode)

        assert checks["max_abs_hourly_dev_preround_mw"] < 1e-6, \
            f"{mode}: float conservation broken"
        assert checks["max_abs_hourly_dev_printed_mw"] == 0.0, \
            f"{mode}: printed conservation broken"
        assert np.abs(np.rint(new.sum(axis=1) * 10)
                      - np.rint(control.sum(axis=1) * 10)).max() == 0, \
            f"{mode}: an hour's statewide total moved"
        assert (new >= 0).all(), f"{mode}: produced negative load"

        if mode == "hold":
            covered = {zidx[n] for n in shares.node[shares.covered]}
            others = [i for i in range(control.shape[1]) if i not in covered]
            assert np.array_equal(new[:, others], control[:, others]), \
                "hold: a bus outside the covered set changed"
            print(f"  [OK] hold: {len(others):,} uncovered buses bit-identical to control, "
                  f"hourly totals preserved")
        else:
            in_share = {zidx[n] for n in shares.node}
            outside = [i for i in range(control.shape[1]) if i not in in_share]
            assert new[:, outside].sum() == 0, \
                "full: load landed on a bus outside the share vector"
            print(f"  [OK] full: all load on the {len(in_share):,}-bus share vector, "
                  f"hourly totals preserved")


def test_share_vector_sanity(demand=None) -> None:
    tree = scenario_seasons()
    demand = read_demand(tree.demand_path(tree.canonical[tree.seasons[0]]))
    shares = _synthetic_shares(demand)

    assert (shares.share >= 0).all(), "negative share"
    assert abs(shares.share.sum() - 1.0) < 1e-12, "shares do not sum to 1"
    topped = shares[shares.topup_mwh > 0]
    assert (topped.method_mwh == 0).all(), \
        "top-off landed on a bus the method already covers"
    print(f"  [OK] shares sum to 1; top-off confined to the "
          f"{len(topped)} method-empty buses")


def test_by_cell_modes(tree) -> None:
    """Guard 6: the month-hour paths (used by every stoch run and the monthhour
    variants of reedsco/env) conserve hourly totals, respect the hold
    partition, zero a pool bus whose share is zero in every cell, and actually
    vary the split across hours."""
    case = tree.canonical[tree.seasons[0]]
    demand = read_demand(tree.demand_path(case))
    base = _synthetic_shares(demand)
    control = demand.values
    zidx = demand.zone_index()

    cells = [(1, h) for h in range(24)]
    hours = cells * (demand.n_hours // 24)
    rng = np.random.default_rng(1)
    per_cell = base[["node"]].merge(
        pd.DataFrame(cells, columns=["month", "hour_pst"]), how="cross")
    per_cell["share"] = rng.random(len(per_cell))
    # one bus the pool sweeps but the method gives nothing: must end at zero
    zeroed = base.node[base.covered].iloc[0]
    per_cell.loc[per_cell.node == zeroed, "share"] = 0.0
    per_cell["share"] /= per_cell.groupby(["month", "hour_pst"]).share.transform("sum")

    for mode in ("full", "hold"):
        new, checks = rescale_season(demand, per_cell, mode, hours=hours)
        assert checks["max_abs_hourly_dev_preround_mw"] < 1e-6, \
            f"by-cell {mode}: float conservation broken"
        assert checks["max_abs_hourly_dev_printed_mw"] == 0.0, \
            f"by-cell {mode}: printed conservation broken"
        pool = {zidx[n] for n in per_cell.node.unique()}
        outside = [i for i in range(control.shape[1]) if i not in pool]
        if mode == "hold":
            assert np.array_equal(new[:, outside], control[:, outside]), \
                "by-cell hold: a bus outside the pool changed"
        else:
            assert new[:, outside].sum() == 0, \
                "by-cell full: load landed outside the share vector"
        assert new[:, zidx[zeroed]].sum() == 0, \
            f"by-cell {mode}: a zero-share pool bus kept load"
        # the split must differ between two hours of different cells
        col = zidx[base.node[base.covered].iloc[1]]
        tot = new.sum(axis=1) if mode == "full" else new[:, sorted(pool)].sum(axis=1)
        frac = np.divide(new[:, col], tot, out=np.zeros(demand.n_hours), where=tot > 0)
        assert frac.std() > 1e-6, f"by-cell {mode}: bus share is constant across hours"
    print("  [OK] by-cell full+hold: conservation, partition, zeroing, and "
          "hour-to-hour share variation all hold")


def main() -> None:
    print("GenX rescaler guards")
    tree = scenario_seasons()
    print(f"  control tree: {len(tree.cases)} cases, {len(tree.seasons)} seasons")
    test_io_round_trip(tree)
    test_rounding_unit()
    test_conservation_and_partition(tree)
    test_share_vector_sanity()
    test_by_cell_modes(tree)
    print("all guards passed")


if __name__ == "__main__":
    main()
