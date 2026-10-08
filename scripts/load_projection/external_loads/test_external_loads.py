"""Guards for the external seasonal load weight source.

Checks the invariants the rest of the pipeline relies on, and the ones that make
the method honest: the published default path untouched, levels preserved, names
refused rather than guessed, coordinates beating name lookups, duplicates
surfaced, and the degenerate variant visibly degenerate.

Fast: no hourly writing, no Monte Carlo. Prints one [OK] per guard and exits
non-zero on the first failure, matching test_genx_rescale.py.

Some guards need the fixture weight folders; build them with
build_external_weights.py first (docs/external_loads.md has the commands). Those
guards say so and skip rather than fail, so this stays runnable on a fresh
checkout.

Usage
  python scripts/load_projection/external_loads/test_external_loads.py
"""

from __future__ import annotations

import sys
import tempfile
from argparse import Namespace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/data/substations"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/checks"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/nodal"))

from build_cec_name_dictionary import norm  # noqa: E402
from load_projection import cells as C  # noqa: E402
from load_projection import external_loads as ext  # noqa: E402
from load_projection.weights import load_profiles  # noqa: E402

PROFILES = ROOT / "data/processed/substations/substation_load_profiles_clean.csv"
ATTRS = ROOT / "data/processed/substations/substation_attributes_clean.csv"
BASIN_DICT = ROOT / "data/basinSourceDictionary.csv"
EXT_ROOT = ROOT / "data/processed/load_projection/external_loads"
HALF = C.get_spec("halfyear")
HALF_LABEL = C.label_frame(HALF).iloc[:, 0].to_numpy()


def _blocks(df: pd.DataFrame) -> pd.Series:
    return pd.Series(HALF_LABEL[C.encode(HALF, df.month)], index=df.index)


def _indexes():
    prof = load_profiles(PROFILES, "avg_load")
    idx, ambig, by_util = ext.build_name_index(prof, norm)
    lookup, dict_ambig = ext.invert_basin_dictionary(BASIN_DICT, idx, norm)
    return prof, idx, ambig, by_util, lookup, dict_ambig


def _sub_coords() -> dict:
    a = pd.read_csv(ATTRS, usecols=["utility", "substation_name",
                                    "util_lat", "util_lon"])
    a["utility"] = a.utility.astype(str).str.lower()
    return {(u, s): (float(la), float(lo))
            for u, s, la, lo in zip(a.utility, a.substation_name,
                                    a.util_lat, a.util_lon)
            if pd.notna(la) and pd.notna(lo)}


def guard_default_path_untouched() -> None:
    """A caller that sets no county_weights gets the published envelope split."""
    import rescale_genx_demand as RS
    base = dict(map="prox", system="CATS", alpha="ratio", county_year=2023,
                weights="reedsco", pool="cats_loaded", uncovered="cats",
                draw="mean", min_draws=3, stochastic_run="", year=None)
    s_default = RS.county_first_shares(Namespace(**base), {})[0]
    s_explicit = RS.county_first_shares(
        Namespace(**base, county_weights="envelope"), {})[0]
    m = s_default.merge(s_explicit, on="node", suffixes=("_d", "_e"))
    worst = float(np.abs(m.share_d - m.share_e).max())
    assert worst == 0.0, f"default path moved by {worst:.3e}"
    assert RS.county_weight_src(Namespace()) == "envelope"
    try:
        RS.county_weight_src(Namespace(county_weights="typo"))
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown county_weights must raise, not fall back")
    print(f"  [OK] default path identical to county_weights='envelope' "
          f"({len(m):,} buses, max diff 0.0); a typo raises")


def guard_input_contract() -> None:
    """Missing columns, duplicate names and broken coordinates are refused."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad.csv"

        def check(df, fragment):
            df.to_csv(p, index=False)
            try:
                ext.read_input(p)
            except ValueError as e:
                assert fragment in str(e), f"expected {fragment!r} in {e}"
            else:
                raise AssertionError(f"should have raised for {fragment!r}")

        check(pd.DataFrame({"name": ["A"], "summer_load": [1.0]}), "winter_load")
        check(pd.DataFrame({"name": ["A", "A"], "summer_load": [1.0, 2.0],
                            "winter_load": [1.0, 2.0]}), "more than once")
        check(pd.DataFrame({"name": ["A"], "summer_load": [1.0],
                            "winter_load": [1.0], "lat": [37.0]}),
              "only one of lat/lon")
        check(pd.DataFrame({"name": ["A"], "summer_load": [1.0],
                            "winter_load": [1.0], "lat": [137.0], "lon": [-120.0]}),
              "out-of-range")
        # `long`/`latitude`/`voltage` are accepted as aliases
        pd.DataFrame({"name": ["A"], "summer_load": [1.0], "winter_load": [1.0],
                      "latitude": [37.0], "long": [-120.0], "voltage": [115]}
                     ).to_csv(p, index=False)
        got = ext.read_input(p)
        assert {"lat", "lon", "voltage_kv"} <= set(got.columns)
    print("  [OK] input contract enforced; lat/long/voltage aliases accepted")


def guard_cascade_order() -> None:
    """Rule 1 beats rule 2; coordinates beat the reference-name lookup."""
    prof, idx, ambig, by_util, lookup, dict_ambig = _indexes()
    sample = next(iter(idx))
    rep = ext.resolve_names(pd.DataFrame({"name": [sample]}), idx, ambig, by_util,
                            lookup, dict_ambig, {}, norm)
    assert rep.route.iat[0] == ext.ROUTE_UTILITY_DIRECT, rep.route.iat[0]

    # a name that ALSO exists in the reference table, but with coordinates:
    # rule 3 must win over rule 4
    rep = ext.resolve_names(
        pd.DataFrame({"name": ["NOT A SUBSTATION"], "lat": [37.5], "lon": [-122.0]}),
        idx, ambig, by_util, lookup, dict_ambig,
        {norm("NOT A SUBSTATION"): ("NOT A SUBSTATION", 36.0, -120.0)}, norm)
    assert rep.route.iat[0] == ext.ROUTE_COORD_SPATIAL, (
        f"own coordinates must beat the reference-name lookup; got {rep.route.iat[0]}")

    # without coordinates the same row falls to rule 4
    rep = ext.resolve_names(
        pd.DataFrame({"name": ["NOT A SUBSTATION"]}), idx, ambig, by_util,
        lookup, dict_ambig, {norm("NOT A SUBSTATION"): ("NOT A SUBSTATION", 36.0, -120.0)}, norm)
    assert rep.route.iat[0] == ext.ROUTE_CEC_SPATIAL, rep.route.iat[0]
    # rule 4 hands back the reference record's coordinates for the caller to
    # place, exactly like rule 3 -- it does NOT pick a bus itself, so both
    # spatial routes go through one pool-restricted placement
    assert rep.node.iat[0] is None, "rule 4 must not assign a bus"
    assert (rep.ref_lat.iat[0], rep.ref_lon.iat[0]) == (36.0, -120.0)
    print("  [OK] cascade order: rule 1 > rule 2, own coordinates > reference "
          "name; both spatial routes yield coordinates, not a bus")


def guard_shared_names() -> None:
    """A shared name is refused without evidence and resolved with it."""
    prof, idx, ambig, by_util, lookup, dict_ambig = _indexes()
    coords = _sub_coords()
    assert ambig, "expected names shared across utilities"
    # pick a shared name whose candidates all have coordinates
    pick = next((n for n, c in ambig.items() if all(k in coords for k in c)), None)
    assert pick, "expected a shared name with coordinates for every candidate"
    util, raw = ambig[pick][0]

    # no utility, no coordinates -> refused, with the candidates named
    rep = ext.resolve_names(pd.DataFrame({"name": [raw]}), idx, ambig, by_util,
                            lookup, dict_ambig, {}, norm, sub_coords=coords)
    assert rep.route.iat[0] == ext.ROUTE_UNRESOLVED
    assert "more than one utility" in rep.status.iat[0]

    # a utility resolves it
    rep = ext.resolve_names(pd.DataFrame({"name": [raw], "utility": [util]}),
                            idx, ambig, by_util, lookup, dict_ambig, {}, norm,
                            sub_coords=coords)
    assert rep.route.iat[0] == ext.ROUTE_UTILITY_DIRECT, rep.status.iat[0]

    # so do coordinates on top of the right candidate
    lat, lon = coords[(util, raw)]
    rep = ext.resolve_names(
        pd.DataFrame({"name": [raw], "lat": [lat], "lon": [lon]}),
        idx, ambig, by_util, lookup, dict_ambig, {}, norm, sub_coords=coords)
    assert rep.route.iat[0] == ext.ROUTE_UTILITY_DIRECT, rep.status.iat[0]
    assert rep.substation_name.iat[0] == raw
    assert "proximity" in rep.status.iat[0]

    # coordinates in the middle of nowhere do NOT resolve it
    rep = ext.resolve_names(
        pd.DataFrame({"name": [raw], "lat": [41.9], "lon": [-120.1]}),
        idx, ambig, by_util, lookup, dict_ambig, {}, norm, sub_coords=coords)
    assert rep.route.iat[0] == ext.ROUTE_UNRESOLVED, (
        "a far-away coordinate must not resolve a shared name")
    print(f"  [OK] shared names ({len(ambig)}): refused bare, resolved by a "
          f"utility or by nearby coordinates, still refused by far ones")


def guard_distance_demotion() -> None:
    """A name match whose location disagrees can be demoted to coordinates."""
    prof, idx, ambig, by_util, lookup, dict_ambig = _indexes()
    coords = _sub_coords()
    pick = next(k for k in coords if k in set(idx.values()))
    name = pick[1]
    far = pd.DataFrame({"name": [name], "lat": [32.7], "lon": [-117.2]})
    rep = ext.resolve_names(far, idx, ambig, by_util, lookup, dict_ambig, {},
                            norm, sub_coords=coords, max_name_dist_km=None)
    assert rep.route.iat[0] == ext.ROUTE_UTILITY_DIRECT, "default trusts the name"
    assert rep.name_dist_km.iat[0] > 0, "the distance must still be reported"
    rep = ext.resolve_names(far, idx, ambig, by_util, lookup, dict_ambig, {},
                            norm, sub_coords=coords, max_name_dist_km=25.0)
    assert rep.route.iat[0] == ext.ROUTE_COORD_SPATIAL, (
        "beyond the threshold the row must be placed by its coordinates")
    assert "km away" in rep.status.iat[0]
    print(f"  [OK] name/coordinate disagreement reported always "
          f"({rep.name_dist_km.iat[0]:.0f} km here), demoted when asked")


def guard_dictionary_inversion_refuses() -> None:
    """A many-to-one dictionary inversion is recorded ambiguous and then raises."""
    prof, idx, ambig, by_util, lookup, dict_ambig = _indexes()
    assert dict_ambig, ("expected a many-to-one inversion "
                        "(PGE 'drum 1'/'drum 2' -> DRUM)")
    key = next(iter(dict_ambig))
    try:
        ext.resolve_names(pd.DataFrame({"name": [key]}), idx, ambig, by_util,
                          lookup, dict_ambig, {}, norm)
    except ext.AmbiguousNameError:
        pass
    else:
        raise AssertionError("an ambiguous dictionary inversion must raise")
    print(f"  [OK] {len(dict_ambig)} ambiguous dictionary inversion(s) raise "
          f"rather than pick one")


def guard_shape_is_level_preserving() -> None:
    """The normalized shape averages exactly 1 within each half-year block."""
    prof = load_profiles(PROFILES, "avg_load")
    sh = ext.normalized_shapes(prof, "avg_load")
    m = sh.groupby(["utility", "substation_name", "block"])["shape"].mean()
    assert np.allclose(m.to_numpy(), 1.0), (
        f"shape must average 1 within a block; got {m.min():.6f}-{m.max():.6f}")
    print(f"  [OK] normalized shape averages exactly 1 within each block "
          f"({len(m):,} (substation, block) groups)")


def guard_shape_file() -> None:
    """A supplied curve is validated and renormalized; a broken one is refused."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "shape.csv"
        rows = [{"utility": u, "season": s, "hour_pst": h,
                 "shape": 5.0 + 3.0 * np.sin(h / 24 * 2 * np.pi)}
                for u in ("pge", "sce") for s in ("summer", "winter")
                for h in range(24)]
        pd.DataFrame(rows).to_csv(p, index=False)
        sf = ext.load_shape_file(p)
        m = sf.groupby(["utility", "block"])["shape"].mean()
        assert np.allclose(m.to_numpy(), 1.0), f"not renormalized: {m.to_dict()}"
        assert set(sf.block) == {"MayOct", "NovApr"}

        pd.DataFrame(rows[:20]).to_csv(p, index=False)   # missing hours
        try:
            ext.load_shape_file(p)
        except ValueError as e:
            assert "24 hours" in str(e)
        else:
            raise AssertionError("an incomplete curve must raise")

        bad = pd.DataFrame(rows).assign(season="autumn")
        bad.to_csv(p, index=False)
        try:
            ext.load_shape_file(p)
        except ValueError as e:
            assert "season" in str(e)
        else:
            raise AssertionError("an unknown season must raise")
    print("  [OK] shape file renormalized to mean 1 per block; incomplete or "
          "unknown-season files refused")


def guard_duplicate_names() -> None:
    """A repeated name is a decision, and co-location is what decides it.

    Two rows naming one substation at one spot are one site and their loads add.
    Two rows sharing a name 200 km apart are different substations, and merging
    them would move load -- so they stay separate. `keep` and `sum` must give the
    SAME final weights for co-located rows, because rows landing on one bus are
    summed there anyway; `sum` only makes the merge explicit.
    """
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "d.csv"
        base = dict(summer_load=[10.0, 30.0], winter_load=[8.0, 24.0])
        # co-located: ~0.09 km apart
        pd.DataFrame({"name": ["A", "A"], "lat": [37.0, 37.0008],
                      "lon": [-122.0, -122.0], **base}).to_csv(p, index=False)
        got = ext.read_input(p, on_duplicate_name="sum", colocate_km=0.5)
        assert len(got) == 1, f"co-located rows must collapse; got {len(got)}"
        assert got.summer_load.iat[0] == 40.0, got.summer_load.iat[0]
        assert got.winter_load.iat[0] == 32.0
        r = got.attrs["collapse_report"]
        assert int(r.units.iat[0]) == 1 and r.max_km_within_name.iat[0] < 0.5

        # far apart: ~200 km
        pd.DataFrame({"name": ["A", "A"], "lat": [37.0, 38.5],
                      "lon": [-122.0, -120.5], **base}).to_csv(p, index=False)
        got = ext.read_input(p, on_duplicate_name="sum", colocate_km=0.5)
        assert len(got) == 2, "rows 200 km apart must NOT be merged"
        assert sorted(got.summer_load) == [10.0, 30.0], "loads must be untouched"
        assert int(got.attrs["collapse_report"].units.iat[0]) == 2

        # error refuses, and says what to do instead
        try:
            ext.read_input(p, on_duplicate_name="error")
        except ValueError as e:
            assert "--on-duplicate-name keep" in str(e)
            assert "--on-duplicate-name sum" in str(e)
        else:
            raise AssertionError("the default must refuse a repeated name")

        # keep leaves every row its own unit, with a unique row_id
        got = ext.read_input(p, on_duplicate_name="keep")
        assert len(got) == 2 and got.row_id.is_unique
        assert "collapse_report" not in got.attrs

        # an unknown mode is refused
        try:
            ext.read_input(p, on_duplicate_name="nope")
        except ValueError as e:
            assert "on_duplicate_name must be one of" in str(e)
        else:
            raise AssertionError("an unknown mode must raise")
    print("  [OK] duplicate names: co-located rows summed, 200 km-apart rows kept "
          "separate, default refuses with guidance, row_id stays unique")


def guard_artifacts(tag: str, expect_flat: bool) -> bool:
    """Emitted tables: 144 cells per block, level preserved, ids and names clean."""
    d = EXT_ROOT / tag
    if not (d / "external_substation_weights.csv").exists():
        print(f"  [--] skipped {tag}: not built (see docs/external_loads.md)")
        return False
    sub = pd.read_csv(d / "external_substation_weights.csv")
    assert "weight" in sub.columns and not [c for c in sub.columns if c.endswith("_mw")], (
        f"{tag}: the value column must be `weight`, never named for megawatts")
    sub["block"] = _blocks(sub)
    n = sub.groupby(["utility", "substation_name", "block"]).size()
    assert set(n.unique()) == {144}, f"{tag}: cells per block {sorted(n.unique())}"
    cv = sub.groupby(["utility", "substation_name", "block"]).weight.agg(
        lambda x: 0.0 if x.mean() == 0 else x.std() / x.mean())
    if expect_flat:
        assert float(cv.max()) < 1e-12, (
            f"{tag}: flat shape must not vary within a block (max CV {cv.max():.2e})")
    else:
        assert float(cv.median()) > 0.01, (
            f"{tag}: utility shape must vary within a block (median {cv.median():.4f})")
    nw = d / "external_node_weights.csv"
    if nw.exists():
        raw = pd.read_csv(nw, dtype={"node": str})
        if len(raw):
            assert not raw.node.astype(str).str.contains(r"\.").any(), (
                f"{tag}: node ids must be plain strings, not floats")
    print(f"  [OK] {tag}: 144 cells/block, within-block CV median "
          f"{cv.median():.6f} ({'flat' if expect_flat else 'shaped'}), "
          f"column `weight`, node ids clean")
    return True


def guard_coverage_rises() -> None:
    """The point of the method: reference substations raise the measured share."""
    import rescale_genx_demand as RS
    tags = ["external_full_utilonly", "external_full_both"]
    if not all((EXT_ROOT / t / "external_substation_weights.csv").exists() for t in tags):
        print("  [--] skipped coverage comparison: fixtures not built")
        return

    def gov(src, ext_tag=None):
        a = Namespace(map="prox", system="CATS", alpha="ratio", county_year=2023,
                      county_weights=src, weights="reedsco", pool="cats_loaded",
                      uncovered="cats", external_loads=ext_tag, draw="mean",
                      min_draws=3, stochastic_run="", year=None)
        shares, detail, meta = RS.county_first_shares(a, {})
        return meta["envelope_governed_share"], int(detail.n_substation_nodes.sum())

    g_env, n_env = gov("envelope")
    g_u, _ = gov("external", tags[0])
    g_b, n_b = gov("external", tags[1])
    assert n_b > n_env, f"reference substations must cover MORE buses; {n_b} vs {n_env}"
    assert g_b > g_env, f"measured share must rise; {g_b:.4f} vs {g_env:.4f}"
    assert abs(g_u - g_env) < 0.05, (
        f"utility-only external should land near the envelope baseline; "
        f"{g_u:.4f} vs {g_env:.4f}")
    print(f"  [OK] measured share rises {g_env:.4f} -> {g_b:.4f} "
          f"(+{(g_b - g_env) * 100:.2f} pp), covered buses {n_env:,} -> {n_b:,}; "
          f"utility-only stays at {g_u:.4f}")


def guard_conservation() -> None:
    """Per-cell share vectors sum to 1 for an external variant."""
    import rescale_genx_demand as RS
    tag = "external_full_both"
    if not (EXT_ROOT / tag / "external_substation_weights.csv").exists():
        print("  [--] skipped conservation: fixture not built")
        return
    lf = C.label_frame(C.MONTHHOUR)
    all_cells = {(int(m), int(h)) for m, h in zip(lf.month, lf.hour_pst)}
    a = Namespace(map="prox", system="CATS", alpha="ratio", county_year=2023,
                  county_weights="external", weights="reedsco", pool="cats_loaded",
                  uncovered="cats", external_loads=tag, draw="mean", min_draws=3,
                  stochastic_run="", year=None)
    cache: dict = {}
    shares, detail, _ = RS.county_first_shares(a, cache)
    per_cell, _ = RS.expand_shares_to_cells(shares, detail, a, cache, all_cells)
    tot = per_cell.groupby(["month", "hour_pst"]).share.sum()
    worst = float(np.abs(tot - 1.0).max())
    assert len(tot) == 288, f"expected 288 cells, got {len(tot)}"
    assert worst < 1e-12, f"per-cell shares deviate from 1 by {worst:.3e}"
    assert (per_cell.share >= 0).all(), "negative share"
    print(f"  [OK] external variant: 288 cells, shares sum to 1 "
          f"(max dev {worst:.1e}), none negative")


def main() -> None:
    print("External seasonal load weight-source guards")
    guard_default_path_untouched()
    guard_input_contract()
    guard_duplicate_names()
    guard_cascade_order()
    guard_shared_names()
    guard_distance_demotion()
    guard_dictionary_inversion_refuses()
    guard_shape_is_level_preserving()
    guard_shape_file()
    guard_artifacts("external_utility__prox", expect_flat=False)
    guard_artifacts("external_flat__prox", expect_flat=True)
    guard_coverage_rises()
    guard_conservation()
    print("all guards passed")


if __name__ == "__main__":
    main()
