"""Guards for the compact package format and its bundled expander.

The compact format ships each variant's share matrix once plus one statewide
series per combination, and `expand_template.py` (shipped as `code/expand.py`)
reconstructs any hourly file. That only holds if the expander's arithmetic stays
identical to the builder's -- and it carries its OWN copy of
`round_to_printed`, because the package must work without this repo. A copy can
drift, so it is checked here against the original on every run.

Fast: synthetic matrices, no RESOLVE assembly, no real package. The end-to-end
check (build both formats for one real combination and compare md5) is in
docs/deliverables.md's rebuild notes; this suite catches drift without it.

Usage
  python scripts/load_projection/deliverables/test_compact.py
"""

from __future__ import annotations

import gzip
import importlib.util
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))

from genx_demand_io import round_to_printed as original  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "expand_template", Path(__file__).with_name("expand_template.py"))
EX = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(EX)

N_CELLS = 288


def _synthetic(n_buses: int = 40, n_hours: int = 500, seed: int = 0):
    """A share matrix whose rows sum to 1, a statewide series, and cell labels."""
    rng = np.random.default_rng(seed)
    S = rng.random((N_CELLS, n_buses))
    S[rng.random(S.shape) < 0.15] = 0.0            # some buses empty in some cells
    S /= S.sum(axis=1, keepdims=True)
    y = 20_000 + 15_000 * rng.random(n_hours)
    cells = rng.integers(0, N_CELLS, n_hours)
    return S, y, cells


def guard_rounding_copy_matches_original() -> None:
    """The expander's copy of round_to_printed must not have drifted."""
    rng = np.random.default_rng(7)
    for trial in range(25):
        rows, cols = rng.integers(1, 40), rng.integers(2, 60)
        v = rng.random((rows, cols))
        v /= v.sum(axis=1, keepdims=True)
        tgt = 1_000 + 50_000 * rng.random(rows)
        v = v * tgt[:, None]
        for dec in (0, 1, 2):
            a = original(v.copy(), tgt.copy(), decimals=dec)
            b = EX.round_to_printed(v.copy(), tgt.copy(), decimals=dec)
            assert np.array_equal(a, b), (
                f"trial {trial}, decimals={dec}: the expander's rounding has "
                f"drifted from genx_demand_io.round_to_printed")
            # and it must actually conserve at the printed grid
            scale = 10 ** dec
            dev = np.abs(np.rint(a.sum(axis=1) * scale)
                         - np.rint(tgt * scale)).max()
            assert dev == 0, f"conservation off by {dev} units at decimals={dec}"
    print("  [OK] the expander's round_to_printed matches the original over 25 "
          "random cases x 3 precisions, and conserves exactly")


def guard_expand_matches_direct_write() -> None:
    """Expanding equals writing directly, byte for byte."""
    S, y, cells = _synthetic()
    nodes = [str(1000 + i) for i in range(S.shape[1])]
    stamps = pd.date_range("2012-01-01", periods=len(y), freq="h")

    with tempfile.TemporaryDirectory() as td:
        pkg = Path(td) / "pkg"
        (pkg / "shares").mkdir(parents=True)
        (pkg / "statewide").mkdir(parents=True)
        (pkg / "code").mkdir(parents=True)
        np.savez_compressed(pkg / "shares" / "v1.npz", shares=S,
                            nodes=np.asarray(nodes))
        pd.DataFrame({
            "datetime_pst": stamps.strftime("%Y-%m-%d %H:%M:%S"),
            "hour_of_year": np.arange(1, len(y) + 1),
            "cell": cells, "y_mw": y,
        }).to_csv(pkg / "statewide" / "s1.csv.gz", index=False,
                  float_format="%.6f")

        # the expander, pointed at this package
        EX.PKG = pkg
        out = Path(td) / "expanded.csv.gz"
        st = EX.expand("v1", "s1", out, decimals=1)

        # the same thing written the direct way the builder writes it
        direct = Path(td) / "direct.csv.gz"
        cols = [f"Demand_MW_z{n}" for n in nodes]
        # the statewide series round-trips through %.6f, so read it back rather
        # than using the in-memory y -- that is what the expander sees
        rt = pd.read_csv(pkg / "statewide" / "s1.csv.gz")
        y_rt = rt.y_mw.to_numpy(dtype=np.float64)
        with gzip.open(direct, "wt", newline="") as fh:
            fh.write(",".join(["datetime_pst", "hour_of_year", *cols]) + "\n")
            for a in range(0, len(y_rt), 2000):
                b = min(a + 2000, len(y_rt))
                blk = y_rt[a:b, None] * S[cells[a:b], :]
                snap = original(blk, y_rt[a:b], decimals=1)
                df = pd.DataFrame(snap, columns=cols)
                df.insert(0, "hour_of_year", rt.hour_of_year.to_numpy()[a:b])
                df.insert(0, "datetime_pst", rt.datetime_pst.to_numpy()[a:b])
                df.to_csv(fh, index=False, header=False, float_format="%.1f")

        a = gzip.open(out, "rb").read()
        b = gzip.open(direct, "rb").read()
        assert a == b, "expanded output differs from a direct write"
    print(f"  [OK] expanded == direct write, byte for byte "
          f"({st['hours']:,} h x {st['n_buses']} buses, conservation "
          f"{st['max_hourly_conservation_error_mw']:.3g} MW)")


def guard_analytic_annual_and_peak() -> None:
    """The builder's share-only annual/peak equal the expanded ones pre-rounding.

    This is what lets a compact package report per-bus energy and peak without
    expanding: the share vector is constant within a cell, so annual is
    `S.T @ cell_sum` and per-bus peak is `max_c cell_max[c] * S[c, i]`.
    """
    S, y, cells = _synthetic(n_buses=25, n_hours=900, seed=3)
    cell_sum = np.bincount(cells, weights=y, minlength=N_CELLS)
    cell_max = np.zeros(N_CELLS)
    np.maximum.at(cell_max, cells, y)

    analytic_annual = S.T @ cell_sum
    analytic_peak = (S * cell_max[:, None]).max(axis=0)

    full = y[:, None] * S[cells, :]
    assert np.allclose(analytic_annual, full.sum(axis=0)), "annual mismatch"
    assert np.allclose(analytic_peak, full.max(axis=0)), "per-bus peak mismatch"
    print(f"  [OK] analytic annual and per-bus peak match a full expansion "
          f"({len(y)} h x {S.shape[1]} buses) without expanding")


def guard_expander_refusals() -> None:
    """A corrupt package is refused, not silently expanded."""
    S, y, cells = _synthetic(n_buses=10, n_hours=50, seed=5)
    nodes = [str(i) for i in range(S.shape[1])]
    with tempfile.TemporaryDirectory() as td:
        pkg = Path(td) / "pkg"
        (pkg / "shares").mkdir(parents=True)
        (pkg / "statewide").mkdir(parents=True)
        EX.PKG = pkg

        # rows that do not sum to 1
        np.savez_compressed(pkg / "shares" / "bad.npz", shares=S * 0.5,
                            nodes=np.asarray(nodes))
        pd.DataFrame({"datetime_pst": ["2012-01-01 00:00:00"] * len(y),
                      "hour_of_year": np.arange(1, len(y) + 1),
                      "cell": cells, "y_mw": y}
                     ).to_csv(pkg / "statewide" / "s.csv.gz", index=False)
        try:
            EX.expand("bad", "s", Path(td) / "x.csv.gz")
        except SystemExit as e:
            assert "deviate from 1" in str(e), e
        else:
            raise AssertionError("unnormalized shares must be refused")

        # a cell index out of range
        np.savez_compressed(pkg / "shares" / "ok.npz", shares=S,
                            nodes=np.asarray(nodes))
        pd.DataFrame({"datetime_pst": ["2012-01-01 00:00:00"] * 3,
                      "hour_of_year": [1, 2, 3], "cell": [0, 1, 9999],
                      "y_mw": [1.0, 2.0, 3.0]}
                     ).to_csv(pkg / "statewide" / "badcell.csv.gz", index=False)
        try:
            EX.expand("ok", "badcell", Path(td) / "y.csv.gz")
        except SystemExit as e:
            assert "out of range" in str(e), e
        else:
            raise AssertionError("an out-of-range cell must be refused")

        # unknown names list what is available
        for v, sr, frag in (("nope", "s", "no such variant"),
                            ("ok", "nope", "no such series")):
            try:
                EX.expand(v, sr, Path(td) / "z.csv.gz")
            except SystemExit as e:
                assert frag in str(e), e
            else:
                raise AssertionError(f"{v}/{sr} should have been refused")
    print("  [OK] corrupt or unknown inputs refused: unnormalized shares, "
          "out-of-range cell, unknown variant or series")


def main() -> None:
    print("Compact package format guards")
    guard_rounding_copy_matches_original()
    guard_expand_matches_direct_write()
    guard_analytic_annual_and_peak()
    guard_expander_refusals()
    print("all guards passed")


if __name__ == "__main__":
    main()
