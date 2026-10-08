"""Guards for Approach 3 -- coordinate-free proportional disaggregation.

Run: python scripts/load_projection/approach3/test_approach3.py
Exits non-zero on the first failure. Every guard is self-contained: the fixtures
are synthesized here, so nothing depends on a built artifact.

The load-bearing one is T2. Exact seasonal energy is only achievable with the
TARGET-ENERGY-weighted block normalization; the unweighted cell mean that
`external_loads.normalized_shapes` uses is measurably wrong here, and T2 is what
stops someone "simplifying" the code by reusing it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))

from load_projection import cells as C  # noqa: E402
from load_projection import proportional as P  # noqa: E402
from genx_demand_io import round_to_printed  # noqa: E402

FAILED: list[str] = []


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def check(cond: bool, msg: str) -> None:
    if cond:
        ok(msg)
    else:
        print(f"  [FAIL] {msg}")
        FAILED.append(msg)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def synth_target(hours: int = 8760, seed: int = 0) -> tuple[pd.DataFrame, dict]:
    idx = pd.date_range("2035-01-01", periods=hours, freq="h")
    rng = np.random.default_rng(seed)
    y = np.asarray(31000 + 7000 * np.sin(2 * np.pi * (idx.hour - 18) / 24)
                   + 5000 * np.sin(2 * np.pi * idx.dayofyear / 365),
                   dtype=np.float64) + rng.normal(0, 300, hours)
    tgt = pd.DataFrame({"datetime_pst": idx, "month": idx.month,
                        "hour_pst": idx.hour, "demand_mw": y})
    T = P.target_cell_energy(tgt)
    T["_y"] = y
    T["_dt"] = idx
    return tgt, T


def foo_nodes() -> pd.DataFrame:
    """The user's case: one mixed-sign base plus an unmapped slack node."""
    return pd.DataFrame({
        "node_id": ["foo a", "foo b", "foo c", "U1", "U2"],
        "base_id": ["foo", "foo", "foo", "U1", "U2"],
        "subname": ["a", "b", "c", "", ""],
        "winter_load": [10.0, 11.0, -1.0, 45.0, 35.0],
        "summer_load": [12.0, 9.0, -1.0, 50.0, 30.0]})


def env_surface(T, amp: float = 0.3) -> np.ndarray:
    h = C.label_frame(C.MONTHHOUR).hour_pst.to_numpy()
    return (1.0 + amp * np.sin(2 * np.pi * (h - 17) / 24))[:, None]


def build(nodes, T, shape, mapped, mode="net-base", conserve="slack",
          guards=None):
    guards = guards or P.Guards()
    levels, l, ld = P.node_level_shares(nodes, mode, guards)
    S, sd = P.node_shares(l, shape, mapped, levels, T["Y"],
                          T["block_of_cell"], conserve, guards)
    real = P.realized_energy_shares(S, T["Y"], T["block_of_cell"])
    return levels, l, S, real, ld, sd


# ---------------------------------------------------------------------------
# T1 / T2 -- the identities, and why the weighting is required
# ---------------------------------------------------------------------------

def t1_exactness():
    print("T1  the construction is exact end to end")
    _, T = synth_target()
    nodes = foo_nodes()
    sh, _ = P.energy_normalize(env_surface(T), T["Y"], T["block_of_cell"])
    shape = np.ones((288, len(nodes)))
    shape[:, [0, 1, 2]] = sh
    mapped = np.array([True, True, True, False, False])
    levels, l, S, real, ld, sd = build(nodes, T, shape, mapped)
    check(sd["max_abs_cell_share_error"] < 1e-12,
          f"G9 shares sum to 1 per cell (max dev "
          f"{sd['max_abs_cell_share_error']:.2e})")
    check(np.abs(real - l).max() < 1e-12,
          f"G10 every node gets exactly its level share of block energy "
          f"(max dev {np.abs(real - l).max():.2e})")
    y, cell = T["_y"], T["cell"]
    snapped = round_to_printed(y[:, None] * S[cell, :], y, 1,
                               allow_negative=True)
    err = float(np.abs(snapped.sum(axis=1) - np.round(y, 1)).max())
    check(err < 1e-6, f"G11 hourly total exact on the printed grid "
                      f"(max {err:.2e} MW)")


def t2_weighting_is_required():
    print("T2  the unweighted block mean BREAKS exactness (the reason the "
          "target-energy weighting exists)")
    _, T = synth_target()
    nodes = foo_nodes()
    env = env_surface(T)
    mapped = np.array([True, True, True, False, False])

    good, _ = P.energy_normalize(env, T["Y"], T["block_of_cell"])
    shape = np.ones((288, len(nodes)))
    shape[:, [0, 1, 2]] = good
    *_, real, _, _ = build(nodes, T, shape, mapped)
    _, l, _ = P.node_level_shares(nodes, "net-base", P.Guards())
    dev_ok = float(np.abs(real - l).max())

    # the WRONG one: external_loads.normalized_shapes divides by the unweighted
    # cell mean within each block
    bad = np.ones_like(env)
    for b in range(2):
        m = T["block_of_cell"] == b
        bad[m, :] = env[m, :] / env[m, :].mean(axis=0)
    shape2 = np.ones((288, len(nodes)))
    shape2[:, [0, 1, 2]] = bad
    *_, real2, _, _ = build(nodes, T, shape2, mapped)
    dev_bad = float(np.abs(real2 - l).max())

    check(dev_ok < 1e-12, f"Y-weighted normalization: exact ({dev_ok:.2e})")
    check(dev_bad > 1e-6,
          f"unweighted normalization: energy share off by {dev_bad * 100:.4f} "
          f"pp -- NOT exact, so do not reuse normalized_shapes here")


# ---------------------------------------------------------------------------
# T3 -- the fallback ladder
# ---------------------------------------------------------------------------

def t3_ladder():
    print("T3  the feasibility ladder, each rung giving up what it claims")
    _, T = synth_target()
    # a table where the mapped fleet dominates: lambda_U tiny
    nodes = pd.DataFrame({
        "node_id": ["M1", "M2", "U1"], "base_id": ["M1", "M2", "U1"],
        "subname": "", "winter_load": [500.0, 500.0, 1.0],
        "summer_load": [500.0, 500.0, 1.0]})
    mapped = np.array([True, True, False])
    env = np.column_stack([env_surface(T, 0.5)[:, 0],
                           env_surface(T, -0.5)[:, 0],
                           np.ones(288)])
    sh, _ = P.energy_normalize(env, T["Y"], T["block_of_cell"])

    guards = P.Guards()
    try:
        build(nodes, T, sh, mapped, guards=guards)
        check(False, "a tiny lambda_U should trip G6")
    except ValueError as e:
        check("not fixable" in str(e).lower() or "lambda_U" in str(e),
              "G6 trips with the theorem message when lambda_U is small")

    # rung: flat is provably safe
    flat = np.ones((288, 3))
    _, l, S, real, _, sd = build(nodes, T, flat, mapped, guards=guards)
    check(abs(sd["A_NovApr"] - 1.0) < 1e-12 and abs(sd["A_MayOct"] - 1.0) < 1e-12,
          "rung 'flat': A(b) == 1.0 exactly, so no guard can trip")
    check(np.abs(real - l).max() < 1e-12, "rung 'flat' keeps exactness")

    # rung: strip keeps exactness and shrinks A
    stripped, _ = P.strip_common(sh[:, mapped], l[mapped, :], T["Y"],
                                 T["block_of_cell"])
    sh2 = sh.copy()
    sh2[:, mapped] = stripped
    loose = P.Guards(max_slack_amplification=1e9, min_slack_shape=-np.inf)
    _, l2, _, real2, _, sd2 = build(nodes, T, sh2, mapped, guards=loose)
    _, _, _, _, _, sd0 = build(nodes, T, sh, mapped, guards=loose)
    check(sd2["A_NovApr"] < sd0["A_NovApr"],
          f"rung 'strip': A(NovApr) {sd0['A_NovApr']:.1f} -> "
          f"{sd2['A_NovApr']:.3f}")
    check(np.abs(real2 - l2).max() < 1e-12, "rung 'strip' keeps exactness")

    # rung: renorm works without a slack basis but is not exact
    _, l3, _, real3, _, _ = build(nodes, T, sh, mapped, conserve="renorm",
                                  guards=P.Guards(min_renorm_denominator=1e-6))
    check(np.abs(real3 - l3).max() > 1e-9,
          f"rung 'renorm': energy share deviates "
          f"{np.abs(real3 - l3).max() * 100:.4f} pp, as documented")


# ---------------------------------------------------------------------------
# T4 -- round_to_printed
# ---------------------------------------------------------------------------

def t4_rounding():
    print("T4  round_to_printed: default bit-for-bit, allow_negative correct")
    rng = np.random.default_rng(0)
    V = rng.random((300, 50)) * 40
    Tg = V.sum(axis=1)
    out = round_to_printed(V, Tg, 1)
    check(np.abs(out.sum(axis=1) - np.round(Tg, 1)).max() < 1e-9
          and (out >= 0).all(),
          "default path conserves and stays non-negative on non-negative input")
    check(np.allclose(out, round_to_printed(V, Tg, 1, allow_negative=False)),
          "allow_negative=False is the default")

    S = rng.normal(0, 10, (300, 50))
    S[:, :5] -= 60.0
    Ts = S.sum(axis=1)
    o2 = round_to_printed(S, Ts, 1, allow_negative=True)
    check(np.abs(o2.sum(axis=1) - np.round(Ts, 1)).max() < 1e-9,
          "allow_negative conserves on signed values")
    o3 = round_to_printed(-np.abs(S), -np.abs(S).sum(axis=1), 1,
                          allow_negative=True)
    check(np.abs(o3.sum(axis=1) + np.round(np.abs(S).sum(axis=1), 1)).max() < 1e-9,
          "allow_negative conserves with negative row targets")

    # the real defect: an all-negative row that must give a unit back has no
    # cell the old floor would allow it to take from
    bad = np.array([[-10.0, -10.0, -10.0]])
    tgt = np.array([-30.1])
    try:
        round_to_printed(bad, tgt, 1)
        check(False, "the old non-negativity floor should fail here")
    except ValueError as e:
        check("reclaim" in str(e),
              "default path raises on an all-negative row needing a reclaim "
              "(the defect allow_negative fixes)")
    o4 = round_to_printed(bad, tgt, 1, allow_negative=True)
    check(abs(o4.sum() - (-30.1)) < 1e-9,
          "allow_negative handles that row exactly")


# ---------------------------------------------------------------------------
# T5 -- node id sanitization
# ---------------------------------------------------------------------------

def t5_sanitization():
    print("T5  arbitrary node ids survive as sanitized columns")
    ids = ['a,b', 'quote"here', "new\nline", "BASE|7/x", "100", "  pad  ".strip(),
           "éà中文", "z0001", "same", "same ", "x" * 300]
    cols, index = P.sanitize_node_ids(ids)
    check(len(set(cols)) == len(cols), "columns are unique")
    check(list(index.node_id) == list(ids), "index round-trips every id exactly")
    check(all(c.isascii() and c[0].isalpha() and c.replace("_", "").isalnum()
              for c in cols), "every column is a CSV-safe identifier")
    # a comma/quote/newline id must survive a real CSV round trip
    import io as _io
    buf = _io.StringIO()
    index.to_csv(buf, index=False)
    buf.seek(0)
    back = pd.read_csv(buf, dtype=str, keep_default_na=False)
    check(list(back.node_id) == list(ids),
          "ids with commas, quotes and newlines survive a CSV round trip")


# ---------------------------------------------------------------------------
# T6 -- mapping composition
# ---------------------------------------------------------------------------

def t6_composition():
    print("T6  composition: many->one SUMS levels, one->many COPIES the pattern")
    _, T = synth_target()
    a = env_surface(T, 0.5)[:, 0] * 300.0      # big substation
    b = env_surface(T, -0.5)[:, 0] * 5.0       # small one, opposite shape
    arr = np.column_stack([a, b])
    units = [("pge", "BIG"), ("pge", "SMALL")]
    edges = pd.DataFrame({"base_id": ["X", "X"],
                          "substation_name": ["BIG", "SMALL"],
                          "utility": ["pge", "pge"], "weight": [1.0, 1.0],
                          "status": ["ok", "ok"]})
    comp, bases, _ = P.base_envelope(arr, units, edges)
    check(np.allclose(comp[:, 0], a + b),
          "many->one composite is the SUM of raw levels")
    sum_shape, _ = P.energy_normalize(comp, T["Y"], T["block_of_cell"])
    mean_of_shapes = P.energy_normalize(arr, T["Y"], T["block_of_cell"])[0].mean(axis=1)
    check(not np.allclose(sum_shape[:, 0], mean_of_shapes, atol=1e-3),
          "and is NOT the mean of the normalized shapes (which would weight a "
          "5 MW substation like a 300 MW one)")

    base_of_node = np.array([0, 0, 0])
    bs = P.broadcast_to_siblings(sum_shape, base_of_node)
    check(np.allclose(bs[:, 0], bs[:, 1]) and np.allclose(bs[:, 1], bs[:, 2]),
          "one base -> siblings all get the IDENTICAL shape (nothing is split; "
          "this is NOT the tie-share rule)")


# ---------------------------------------------------------------------------
# T7 / T8 -- nothing dropped, every mode
# ---------------------------------------------------------------------------

def t7_nothing_dropped():
    print("T7  every node appears exactly once, including zero/negative/unmapped")
    _, T = synth_target()
    nodes = foo_nodes()
    nodes.loc[len(nodes)] = ["Z0", "Z0", "", 0.0, 0.0]
    shape = np.ones((288, len(nodes)))
    mapped = np.array([True, True, True, False, False, False])
    _, l, S, _, ld, _ = build(nodes, T, shape, mapped)
    check(S.shape[1] == len(nodes), f"all {len(nodes)} nodes in the share matrix")
    cols, index = P.sanitize_node_ids(nodes.node_id)
    check(len(index) == len(nodes), "all nodes in the index")
    zeroed = ~(l != 0).any(axis=1)
    check(zeroed.sum() == 2 and set(nodes.node_id[zeroed]) == {"foo c", "Z0"},
          "the negative and the zero node are zeroed, not dropped")


def t8_modes():
    print("T8  the four negative-level modes are just definitions of l_n(b)")
    _, T = synth_target()
    nodes = foo_nodes()
    nodes["summer_load"] = nodes["winter_load"]     # keep the arithmetic simple
    shape = np.ones((288, len(nodes)))
    mapped = np.array([True, True, True, False, False])
    want = {"net-base": 20.0, "zero": 21.0, "participate": 20.0}
    for mode, expect in want.items():
        levels, l, S, real, ld, _ = build(nodes, T, shape, mapped, mode=mode)
        TL = ld["total_level_NovApr"]
        got = l[:3, 0].sum() * TL
        check(abs(got - expect) < 1e-9,
              f"--negative-nodes {mode:<12} base 'foo' sums to {got:.2f} "
              f"(expected {expect})")
        check(np.abs(real - l).max() < 1e-12,
              f"  and G10 still holds exactly under {mode}")
    neg = l[2, 0]
    check(neg < 0, f"participate gives foo c a negative share ({neg:+.5f})")
    try:
        P.node_level_shares(nodes, "refuse", P.Guards())
        check(False, "refuse should abort")
    except ValueError:
        ok("--negative-nodes refuse aborts and names the negative nodes")

    # a base netting below zero is zeroed and counted
    n2 = nodes.copy()
    n2.loc[:, "winter_load"] = [1.0, 1.0, -5.0, 45.0, 35.0]
    n2.loc[:, "summer_load"] = n2.winter_load
    _, l2, d2 = P.node_level_shares(n2, "net-base", P.Guards())
    check(d2["n_bases_net_negative_NovApr"] == 1
          and abs(d2["negative_base_mass_NovApr"] - 3.0) < 1e-9
          and not l2[:3, 0].any(),
          "a base netting below zero is zeroed, its discarded mass counted")
    try:
        P.node_level_shares(n2, "net-base", P.Guards(on_negative_base="refuse"))
        check(False, "--on-negative-base refuse should abort")
    except ValueError:
        ok("--on-negative-base refuse aborts instead")


# ---------------------------------------------------------------------------
# T9 -- the subname axis
# ---------------------------------------------------------------------------

def t9_siblings():
    print("T9  the subname axis: shared shape, own level, never reorders")
    _, T = synth_target()
    nodes = foo_nodes()
    nodes["summer_load"] = nodes["winter_load"]
    sh, _ = P.energy_normalize(env_surface(T), T["Y"], T["block_of_cell"])
    shape = np.ones((288, len(nodes)))
    shape[:, [0, 1, 2]] = sh
    mapped = np.array([True, True, True, False, False])
    levels, l, S, real, ld, _ = build(nodes, T, shape, mapped)
    check(np.allclose(shape[:, 0], shape[:, 1]),
          "siblings carry the identical shape")
    ratio = l[0, 0] / l[1, 0]
    check(abs(ratio - 10 / 11) < 1e-12,
          f"their energies are in the ratio of their levels ({ratio:.6f} "
          f"= 10/11)")
    check(l[2, 0] == 0.0 and levels[2, 0] == -1.0,
          "the negative sibling is 0.0 with its -1 preserved")
    for b in range(2):
        m = T["block_of_cell"] == b
        r = S[m, 0] / S[m, 1]
        check(float(np.abs(r / r.mean() - 1).max()) < 1e-12,
              f"within block {C.HALFYEAR_LABELS[b]} the sibling share ratio is "
              f"constant across cells -- siblings never reorder")


def t10_one_level():
    print("T10 a node table with no base_id reproduces the one-level case")
    _, T = synth_target()
    two = pd.DataFrame({"node_id": ["A", "B", "C"], "base_id": ["A", "B", "C"],
                        "subname": "", "winter_load": [10.0, 20.0, 30.0],
                        "summer_load": [10.0, 20.0, 30.0]})
    one = two.drop(columns=["base_id"])
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "n.csv"
        one.to_csv(p, index=False)
        got = P.read_node_table(p)
    check(list(got.base_id) == list(got.node_id),
          "base_id defaults to node_id when the column is absent")
    g = P.Guards()
    _, l1, _ = P.node_level_shares(got, "net-base", g)
    _, l2, _ = P.node_level_shares(two, "net-base", g)
    check(np.allclose(l1, l2), "and the level shares are identical")


# ---------------------------------------------------------------------------
# T13 / T14 / T15 -- per-sibling draws
# ---------------------------------------------------------------------------

class _SharedEps:
    """An rng whose eps column is identical for every sibling."""

    def __init__(self, seed):
        self.r = np.random.default_rng(seed)

    def standard_normal(self, shape):
        n_days, n = shape
        return np.tile(self.r.standard_normal((n_days, 1)), (1, n))


def t14_kn_cancellation():
    print("T14 per-sibling draws: k_n CANCELS under 'proportional'")
    n = 288
    mu = np.full((n, 1), 100.0)
    sg = np.full((n, 1), 20.0)
    rho = np.full(n, 0.3)
    cell = np.tile(np.arange(n), 4)
    z = np.random.default_rng(1).standard_normal(len(cell))
    day = np.arange(len(cell)) // 24
    k = np.array([0.9, 0.1])
    bon = np.array([0, 0])

    for mode, same in (("proportional", True), ("quadrature", False)):
        out, _ = P.sibling_draws(mu, sg, rho, z, cell, day, k, bon, mode,
                                 _SharedEps(42))
        s = out / out.mean(axis=0)
        got = bool(np.allclose(s[:, 0], s[:, 1], atol=1e-12))
        check(got == same,
              f"{mode}: with eps forced equal, two siblings with k=0.9 and "
              f"k=0.1 {'DO' if same else 'do NOT'} get identical shapes "
              f"(max diff {np.abs(s[:, 0] - s[:, 1]).max():.2e})")

    # cv independent of k under proportional, ~1/sqrt(k) under quadrature
    for mode, expect in (("proportional", 1.0), ("quadrature", 3.0)):
        acc = []
        for s_ in range(40):
            out, _ = P.sibling_draws(mu, sg, rho, z, cell, day, k, bon, mode,
                                     np.random.default_rng(s_))
            sh = out / out.mean(axis=0)
            acc.append(sh.std(axis=0) / sh.mean(axis=0))
        a = np.mean(acc, axis=0)
        ratio = a[1] / a[0]
        check(abs(ratio - expect) < 0.15,
              f"{mode}: cv(k=0.1)/cv(k=0.9) = {ratio:.3f} (expected "
              f"{expect:.2f}) -- so {'the level scaling is inert' if expect == 1 else 'a smaller sibling IS relatively noisier'}")


def t15_draws_keep_exactness():
    print("T15 per-sibling draws decorrelate siblings without breaking G10")
    _, T = synth_target()
    nodes = foo_nodes()
    nodes["summer_load"] = nodes["winter_load"]
    n = 288
    mu = np.full((n, 1), 100.0)
    sg = np.full((n, 1), 25.0)
    rho = np.full(n, 0.3)
    day = pd.factorize(pd.DatetimeIndex(T["_dt"]).normalize())[0]
    zt = pd.DataFrame({"demand_mw": T["_y"], "cell": T["cell"]})
    import load_projection.stochastic as ST
    z = ST.standardize_z(zt).z.to_numpy()
    z = np.where(np.isfinite(z), z, 0.0)
    k = np.array([10, 11, 0, 0, 0], dtype=float)
    k = k / 21.0
    bon = np.zeros(len(nodes), dtype=int)
    realized, _ = P.sibling_draws(mu, sg, rho, z, T["cell"], day, k, bon,
                                  "proportional", np.random.default_rng(3))
    mapped = np.array([True, True, True, False, False])
    shape = np.ones((n, len(nodes)))
    norm, _ = P.energy_normalize(realized[:, mapped], T["Y"],
                                 T["block_of_cell"])
    shape[:, mapped] = norm
    _, l, S, real, _, _ = build(nodes, T, shape, mapped)
    check(np.abs(real - l).max() < 1e-12,
          f"G10 still exact with draws on ({np.abs(real - l).max():.2e})")
    devs = []
    for b in range(2):
        m = T["block_of_cell"] == b
        r = S[m, 0] / S[m, 1]
        devs.append(float(np.abs(r / r.mean() - 1).max()))
    check(max(devs) > 1e-6,
          f"and the sibling share ratio now VARIES across cells "
          f"(max {max(devs):.3e}), so siblings genuinely reorder")


def t16_expander_copy():
    print("T16 the shipped expander's round_to_printed matches the original")
    import importlib.util
    tmpl = ROOT / "scripts/load_projection/approach3/expand_a3_template.py"
    src = tmpl.read_text(encoding="utf-8")
    # import just the function, without running the package-relative imports
    ns: dict = {}
    body = src[src.index("def round_to_printed"):src.index("def manifest(")]
    exec("import numpy as np\n" + body, ns)
    theirs = ns["round_to_printed"]
    rng = np.random.default_rng(11)
    worst_pos = worst_neg = 0.0
    for d in (0, 1, 2):
        for _ in range(12):
            V = rng.random((40, 30)) * 100
            T = V.sum(axis=1)
            worst_pos = max(worst_pos, float(np.abs(
                theirs(V, T, d, False) - round_to_printed(V, T, d, False)).max()))
            S = rng.normal(0, 20, (40, 30))
            S[:, :3] -= 120.0
            Ts = S.sum(axis=1)
            worst_neg = max(worst_neg, float(np.abs(
                theirs(S, Ts, d, True) - round_to_printed(S, Ts, d, True)).max()))
    check(worst_pos == 0.0 and worst_neg == 0.0,
          f"identical over 72 random cases x 3 precisions, signed and unsigned "
          f"(max diff {max(worst_pos, worst_neg):.1e})")
    check("allow_negative: bool = True" in src,
          "the expander defaults allow_negative=True, because an Approach 3 "
          "node-hour can legitimately be negative")


def main() -> None:
    print("Approach 3 guards\n")
    for fn in (t1_exactness, t2_weighting_is_required, t3_ladder, t4_rounding,
               t5_sanitization, t6_composition, t7_nothing_dropped, t8_modes,
               t9_siblings, t10_one_level, t14_kn_cancellation,
               t15_draws_keep_exactness, t16_expander_copy):
        fn()
        print()
    if FAILED:
        print(f"{len(FAILED)} guard(s) failed")
        raise SystemExit(1)
    print("all guards passed")


if __name__ == "__main__":
    main()
