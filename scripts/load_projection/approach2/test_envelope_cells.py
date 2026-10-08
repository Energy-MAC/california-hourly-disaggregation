"""Guards for the generalized envelope input layer (Approach 2).

The generalization of mu/sigma (arbitrary percentile sets) and of the cell
definition (configurable granularity) must leave the published model bit-for-bit
unchanged. These guards check the invariants that make that true, plus the
properties the new paths rely on. Fast: no Monte Carlo, no full pipeline run.

The end-to-end byte-for-byte check is separate and lives in the run itself --
re-run estimate_stochastic.py and generate_stochastic.py and hash-compare the
outputs (see docs/approach2_stochastic.md).

Mirrors scripts/load_projection/genx/test_genx_rescale.py in style: prints one
[OK] per guard, exits non-zero on the first failure.

Usage
  python scripts/load_projection/approach2/test_envelope_cells.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from load_projection import cells as C  # noqa: E402
from load_projection import envelopes as E  # noqa: E402


def guard_cell_index_identity() -> None:
    """The default cell encoding is literally (month-1)*24 + hour, same dtype."""
    rng = np.random.default_rng(0)
    m = rng.integers(1, 13, 200_000)
    h = rng.integers(0, 24, 200_000)
    want = (m - 1) * 24 + h
    got = C.encode(C.MONTHHOUR, m, h)
    assert np.array_equal(got, want), "monthhour cell index changed"
    assert got.dtype == want.dtype, f"dtype drift: {got.dtype} vs {want.dtype}"
    assert C.MONTHHOUR.n_cells == 288
    lbl = C.label_frame(C.MONTHHOUR)
    assert np.array_equal(lbl.month.to_numpy(), lbl.index.to_numpy() // 24 + 1)
    assert np.array_equal(lbl.hour_pst.to_numpy(), lbl.index.to_numpy() % 24)
    print("  [OK] monthhour cell index and decode bit-identical to the original")


def guard_two_point_fit_identity() -> None:
    """K=2 at p={0.10,0.90} reproduces the historical closed form exactly."""
    rng = np.random.default_rng(1)
    lo = rng.uniform(-8, 60, 300_000)
    hi = lo + rng.uniform(0, 45, 300_000)
    lo[:50] = np.nan          # half-missing
    hi[50:100] = np.nan
    hi[100:150] = lo[100:150]  # zero-width
    mu_o = (lo + hi) / 2
    sd_o = (hi - lo) / (2 * E.Z90)
    mu_g, sd_g = E.fit_normal(np.stack([lo, hi]), E.LEGACY_PERCENTILES)
    assert np.array_equal(mu_g, mu_o, equal_nan=True), "mu drifted"
    assert np.array_equal(sd_g, sd_o, equal_nan=True), "sigma drifted"
    from scipy.stats import norm
    z = norm.ppf(np.asarray(E.LEGACY_PERCENTILES))
    assert (z[1] - z[0]) == 2 * E.Z90, "the identity the bit-identity rests on broke"
    print("  [OK] two-point fit bit-identical over 300,000 envelopes "
          "(incl. NaN, zero-width, negative)")


def guard_single_percentile() -> None:
    """One percentile needs a sigma prior; at p=0.5 mu is the input exactly."""
    L = np.array([[10.0, 20.0, -3.0]])
    prior = np.array([2.0, 3.0, 4.0])
    mu, sg = E.fit_normal(L, [0.5], sigma_prior=prior)
    assert np.array_equal(mu, L[0]), "mu != input at p=0.5"
    assert np.array_equal(sg, prior)
    try:
        E.fit_normal(L, [0.5])
    except ValueError:
        pass
    else:
        raise AssertionError("a single percentile without a prior must raise")
    # away from the median mu shifts by sigma * z(p)
    mu2, _ = E.fit_normal(L, [0.9], sigma_prior=prior)
    assert np.allclose(mu2, L[0] - prior * E.Z90)
    print("  [OK] single-percentile fit: mu == input at p=0.5, prior required")


def guard_ols_fit() -> None:
    """K>2 recovers mu and sigma from a clean normal quantile ladder."""
    from scipy.stats import norm
    p = np.array([0.05, 0.25, 0.5, 0.75, 0.95])
    mu_t, sd_t = 37.5, 6.25
    L = (mu_t + sd_t * norm.ppf(p))[:, None]
    mu, sd = E.fit_normal(L, p)
    assert np.allclose(mu, mu_t) and np.allclose(sd, sd_t), (mu, sd)
    print("  [OK] K>2 OLS fit recovers mu and sigma from a 5-point ladder")


def guard_coarsening() -> None:
    """Coarsening obeys the law of total variance and dominates averaging."""
    mdh, mh = C.get_spec("monthdayhour"), C.MONTHHOUR
    rng = np.random.default_rng(7)
    lbl = C.label_frame(mdh)
    rows = []
    for si in range(15):
        base = rng.uniform(5, 80)
        mu = base * (1 + 0.25 * np.sin(lbl.hour_pst / 24 * 2 * np.pi)
                     + 0.1 * rng.standard_normal(len(lbl)))
        rows.append(pd.DataFrame({"utility": "u", "substation_name": f"S{si}",
                                  "cell": lbl.index.to_numpy(), "mu": mu,
                                  "sigma": np.abs(0.15 * mu),
                                  "missing": False, "inverted": False}))
    fine = pd.concat(rows, ignore_index=True)
    cm = C.coarsen_map(mdh, mh)
    var = E.coarsen(fine, cm, mode="variance").sort_values(
        ["substation_name", "cell"]).reset_index(drop=True)
    avg = E.coarsen(fine, cm, mode="average").sort_values(
        ["substation_name", "cell"]).reset_index(drop=True)

    chk = fine.copy()
    chk["t"] = cm[chk.cell.to_numpy()]
    ref = (chk.groupby(["utility", "substation_name", "t"])
           .apply(lambda x: pd.Series({
               "mu": x.mu.mean(),
               "sigma": np.sqrt(np.mean(x.sigma ** 2) + np.var(x.mu, ddof=0))}),
               include_groups=False)
           .reset_index().sort_values(["substation_name", "t"]).reset_index(drop=True))
    assert np.allclose(var.mu, ref.mu), "coarsened mu is not the plain mean"
    assert np.allclose(var.sigma, ref.sigma), "coarsened sigma breaks total variance"
    assert (var.sigma >= avg.sigma - 1e-12).all(), "averaging exceeded variance-preserving"
    print(f"  [OK] coarsening {len(fine):,} -> {len(var):,} sub-cells obeys the law "
          f"of total variance and dominates averaging")


def guard_coarsen_refusals() -> None:
    """A refinement, or a target needing an axis the source lacks, must refuse."""
    for fine, coarse in [("monthhour", "monthdayhour"), ("month", "monthhour"),
                         ("halfyear", "season3")]:
        try:
            C.coarsen_map(C.get_spec(fine), C.get_spec(coarse))
        except ValueError:
            continue
        raise AssertionError(f"coarsening {fine} -> {coarse} should have refused")
    print("  [OK] refinements and straddling aggregations are refused")


def guard_specs_partition_the_year() -> None:
    """Every built-in spec assigns each real calendar hour to exactly one cell."""
    for name in C.BUILTIN_NAMES:
        spec = C.get_spec(name)
        live = spec.codes[spec.codes >= 0]
        assert len(live) == 8784, f"{name} covers {len(live)} hours, expected 8784"
        assert set(np.unique(live)) == set(range(spec.n_cells)), f"{name} has empty cells"
    hy = C.get_spec("halfyear")
    assert C.encode(hy, [5, 10])[0] == C.encode(hy, [5, 10])[1] == 1, "MayOct"
    assert C.encode(hy, [11, 4])[0] == C.encode(hy, [11, 4])[1] == 0, "NovApr"
    assert C.encode(C.get_spec("season3"), [12])[0] == 0, "Dec must be DJF"
    print(f"  [OK] all {len(C.BUILTIN_NAMES)} built-in specs partition the 8,784 "
          f"leap-year hours exactly")


def guard_custom_spec() -> None:
    """A user-authored mapping is honoured, and a partial one is refused."""
    with tempfile.TemporaryDirectory() as td:
        full = Path(td) / "full.csv"
        pd.DataFrame({"month": range(1, 13),
                      "cell_label": ["cool"] * 4 + ["warm"] * 6 + ["cool"] * 2}
                     ).to_csv(full, index=False)
        spec = C.get_spec(f"custom:{full}")
        assert spec.n_cells == 2 and spec.key_cols == ("cell_label",)
        assert list(spec.labels.cell_label.astype(str)) == ["cool", "warm"]
        assert C.encode(spec, [7])[0] == 1 and C.encode(spec, [1])[0] == 0

        partial = Path(td) / "partial.csv"
        pd.DataFrame({"month": [1, 2], "cell_label": ["a", "b"]}).to_csv(partial, index=False)
        try:
            C.get_spec(f"custom:{partial}")
        except ValueError:
            pass
        else:
            raise AssertionError("a partial custom calendar must be refused")
    print("  [OK] custom cell maps honour author ordering; partial maps refused")


def guard_uniform_frozen() -> None:
    """The uniform family is unreachable for a generalized envelope."""
    import load_projection.stochastic as S
    env = pd.DataFrame({"utility": ["u"], "substation_name": ["S"], "cell": [0],
                        "mu": [10.0], "sigma": [1.0]})
    mats = S.EnvelopeMatrices(env, C.get_spec("halfyear"))
    assert mats.unif_a is None and mats.unif_b is None, "uniform bounds leaked"
    cells = pd.DataFrame({"rho": [0.2, 0.2]}, index=pd.RangeIndex(2, name="cell"))
    tgt = pd.DataFrame({"dt_pst_hb": pd.to_datetime(["2020-01-01 00:00"]), "cell": [0]})
    try:
        S.generate(mats, cells, tgt, np.zeros(1), "uniform", 1.0,
                   np.random.default_rng(0))
    except ValueError as exc:
        assert "uniform" in str(exc).lower()
    else:
        raise AssertionError("the uniform family must refuse a generalized envelope")
    print("  [OK] uniform family refuses generalized envelopes (frozen, TODO)")


def main() -> None:
    print("Approach 2 generalized envelope guards")
    for fn in (guard_cell_index_identity, guard_two_point_fit_identity,
               guard_single_percentile, guard_ols_fit, guard_coarsening,
               guard_coarsen_refusals, guard_specs_partition_the_year,
               guard_custom_spec, guard_uniform_frozen):
        fn()
    print("all guards passed")


if __name__ == "__main__":
    main()
