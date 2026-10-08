"""Expand a compact Approach 3 package into full hourly wide CSVs.

This package ships the allocation and the statewide load separately, so a grid
of (model year x weather year) combinations costs megabytes instead of
gigabytes. One combination's hourly file is

    node_mw[t, i] = y[t] * S[cell(t), i]

where `cell` is a (month, hour-of-day) pair in fixed PST -- 12 x 24 = 288 of
them -- so the allocation is a small 288-row matrix rather than an 8,760-row one.

WHY THIS SHIPS `env` AND NOT `S`
--------------------------------
In Approach 3 the share matrix is NOT independent of the model year and weather
year, unlike some other packages in this family. The shape normalization is
target-energy weighted:

    shape_n(c) = env_n(c) * E(b) / sum over c' in b of Y(c') * env_n(c')

with `Y(c)` the statewide energy in cell `c`, so the target enters. Measured:
`shape` moves up to 8.2e-04 between weather years and 1.4e-03 between model
years. Shipping one `S` for every combination would therefore NOT be exact.

What IS target-invariant is `env` -- the raw measured envelope surface -- and the
level shares `l`. So the package ships those once, plus one statewide series per
combination, and this script recomputes the normalization and the shares from the
series itself. That is exact by construction rather than by approximation,
because it runs the SAME functions the original build ran: `proportional.py` is
shipped in this folder and imported here, not reimplemented.

Needs only numpy and pandas.

Usage
  python expand.py --list
  python expand.py --variant envelope --series y2035_wy2012
  python expand.py --all --out-dir hourly/
  python expand.py --variant envelope --series y2035_wy2012 --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
PKG = HERE.parent                      # code/expand.py -> package root
sys.path.insert(0, str(HERE))

from load_projection import proportional as P  # noqa: E402
# the SAME modules the package was built with, shipped as code/load_projection/


def round_to_printed(values: np.ndarray, targets: np.ndarray,
                     decimals: int = 1,
                     allow_negative: bool = True) -> np.ndarray:
    """Largest-remainder apportionment, so each row sums to its target exactly.

    A verbatim copy of `genx_demand_io.round_to_printed`. Naive per-cell
    rounding would leave up to n_nodes/2 * 10^-decimals per hour, breaking
    conservation at the precision the file is written in. `allow_negative`
    defaults to True here because an Approach 3 node-hour can legitimately be
    negative (an envelope shape goes negative in a measured reverse-flow cell).
    """
    scale = 10 ** decimals
    scaled = values * scale
    floor = np.floor(scaled)
    remainder = scaled - floor
    target_units = np.rint(np.asarray(targets, dtype=np.float64) * scale)

    out = floor.copy()
    n_cols = out.shape[1]
    for r in range(out.shape[0]):
        deficit = int(round(target_units[r] - out[r].sum()))
        if deficit == 0:
            continue
        if abs(deficit) > n_cols:
            raise ValueError(f"row {r}: deficit {deficit} exceeds {n_cols} cells")
        if deficit > 0:
            order = np.argsort(-remainder[r], kind="stable")[:deficit]
            out[r, order] += 1
        elif allow_negative:
            order = np.argsort(remainder[r], kind="stable")[:-deficit]
            out[r, order] -= 1
        else:
            order = np.argsort(remainder[r], kind="stable")
            reclaim = -deficit
            for j in order:
                if reclaim == 0:
                    break
                if out[r, j] >= 1:
                    out[r, j] -= 1
                    reclaim -= 1
            if reclaim:
                raise ValueError(f"row {r}: could not reclaim {reclaim} units")
    return out / scale


def manifest() -> dict:
    return json.loads((PKG / "manifest.json").read_text(encoding="utf-8"))


def variants() -> list[str]:
    return sorted(p.stem for p in (PKG / "shares").glob("*.npz"))


def series_labels() -> list[str]:
    return sorted(p.name[:-7] for p in (PKG / "statewide").glob("*.csv.gz"))


def load_allocation(variant: str):
    path = PKG / "shares" / f"{variant}.npz"
    if not path.exists():
        raise SystemExit(f"unknown variant {variant!r}; have {variants()}")
    z = np.load(path, allow_pickle=False)
    return {k: z[k] for k in z.files}


def load_series(label: str) -> pd.DataFrame:
    path = PKG / "statewide" / f"{label}.csv.gz"
    if not path.exists():
        raise SystemExit(f"unknown series {label!r}; have {series_labels()}")
    return pd.read_csv(path)


def shares_for(alloc: dict, s: pd.DataFrame) -> np.ndarray:
    """Recompute the 288 x n_nodes share matrix for one statewide series."""
    m = manifest()
    g = P.Guards(**m["guards"])
    Y = np.bincount(s.cell.to_numpy(), weights=s.y_mw.to_numpy(),
                    minlength=alloc["env"].shape[0])
    block = alloc["block_of_cell"]
    shape = np.ones_like(alloc["env"])
    mapped = alloc["mapped"].astype(bool)
    sh, _ = P.energy_normalize(alloc["env"][:, mapped], Y, block,
                               g.min_shape_net_gross)
    shape[:, mapped] = sh
    if m["axes"].get("shape_common") == "strip" and mapped.any():
        sub, _ = P.strip_common(shape[:, mapped], alloc["l"][mapped, :], Y,
                                block, g.min_shape_net_gross)
        shape[:, mapped] = sub
    S, _ = P.node_shares(alloc["l"], shape, mapped, alloc["levels"], Y, block,
                         m["axes"]["conserve"], g)
    return S


def expand(variant: str, label: str, out_path: Path, decimals: int | None = None,
           chunk: int = 1000, check: bool = False) -> dict:
    m = manifest()
    decimals = m["axes"]["decimals"] if decimals is None else decimals
    alloc = load_allocation(variant)
    s = load_series(label)
    S = shares_for(alloc, s)
    cols = [str(c) for c in alloc["columns"]]
    y = s.y_mw.to_numpy(dtype=np.float64)
    cell = s.cell.to_numpy()
    dt = s.datetime_pst.to_numpy()
    hoy = s.hour_of_year.to_numpy()

    worst = 0.0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import gzip
    opener = gzip.open if out_path.suffix == ".gz" else open
    with opener(out_path, "wt", newline="", encoding="utf-8") as fh:
        fh.write("datetime_pst,hour_of_year," + ",".join(cols) + "\n")
        fmt = "%." + str(decimals) + "f"
        for a in range(0, len(y), chunk):
            sl = slice(a, min(a + chunk, len(y)))
            blk = y[sl, None] * S[cell[sl], :]
            snapped = round_to_printed(blk, y[sl], decimals, True)
            tgt = np.round(y[sl], decimals)
            worst = max(worst, float(np.abs(snapped.sum(axis=1) - tgt).max()))
            body = pd.DataFrame(snapped).to_csv(
                header=False, index=False, float_format=fmt).rstrip("\n")
            for st, h, row in zip(dt[sl], hoy[sl], body.split("\n")):
                fh.write(f"{st},{h},{row}\n")
    res = {"rows": len(y), "nodes": len(cols),
           "max_hourly_conservation_error_mw": worst,
           "mb": round(out_path.stat().st_size / 1e6, 3)}
    if check:
        res["share_rows_sum_to_1"] = float(np.abs(S.sum(axis=1) - 1).max())
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--variant")
    ap.add_argument("--series")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--decimals", type=int, default=None)
    ap.add_argument("--check", action="store_true",
                    help="also report the per-cell share residual")
    args = ap.parse_args()

    m = manifest()
    if args.list or not (args.all or (args.variant and args.series)):
        print(f"package: {m.get('package', '?')}  "
              f"approach: {m.get('approach', '?')}")
        ax = m.get("axes", {})
        print(f"settings: " + "  ".join(f"{k}={v}" for k, v in ax.items()
                                        if v is not None))
        print(f"\nvariants ({len(variants())}):")
        for v in variants():
            print(f"  {v}")
        labels = series_labels()
        print(f"\nseries ({len(labels)}) -- y<model year>_wy<weather year>:")
        for i in range(0, len(labels), 6):
            print("  " + "  ".join(labels[i:i + 6]))
        print(f"\ntotal combinations: {len(variants()) * len(labels)}")
        print("\nexample:")
        print(f"  python expand.py --variant {variants()[0]} "
              f"--series {labels[0]}")
        return

    out_dir = Path(args.out_dir) if args.out_dir else PKG / "nodal_hourly"
    jobs = ([(v, s) for v in variants() for s in series_labels()]
            if args.all else [(args.variant, args.series)])
    if args.all:
        print(f"expanding {len(jobs)} file(s) -- check your disk first")
    for v, s in jobs:
        path = out_dir / f"{v}__{s}.csv.gz"
        r = expand(v, s, path, args.decimals, check=args.check)
        extra = (f"  share residual {r['share_rows_sum_to_1']:.2e}"
                 if "share_rows_sum_to_1" in r else "")
        print(f"  {path.name:<48} {r['rows']:,} h x {r['nodes']:,} nodes  "
              f"{r['mb']:>7.3f} MB  conservation "
              f"{r['max_hourly_conservation_error_mw']:.2e} MW{extra}")


if __name__ == "__main__":
    main()
