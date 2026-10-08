"""Expand a compact nodal-load package into a full hourly wide CSV.

This package ships the allocation and the statewide load separately, because the
share matrices do not depend on the model year or the weather year at all -- only
on the allocation settings. One combination's hourly file is just

    bus_mw[t, i] = y[t] * S[cell(t), i]

so the package carries each variant's 288-cell share matrix ONCE and one
statewide series per (model year, weather year). That is tens of megabytes
instead of several gigabytes, and this script reconstructs any hourly file you
want, byte for byte identical to one written directly.

A cell is a (month, hour-of-day) pair in fixed PST -- 12 x 24 = 288 of them. Load
within a cell is allocated by one share vector, which is why the matrix is small.

Needs only numpy and pandas. It does not import anything from the project that
produced the package.

Usage
  python expand.py --list
  python expand.py --variant countyfirst_cec2022_shaped_equalsplit__prox \\
                   --series y2035_wy2012
  python expand.py --all --out-dir hourly/
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
PKG = HERE.parent                      # code/expand.py -> package root


def round_to_printed(values: np.ndarray, targets: np.ndarray,
                     decimals: int = 1) -> np.ndarray:
    """Round each row to `decimals` places so its sum hits `targets` exactly.

    Largest-remainder apportionment in integer units of 10^-decimals. Naive
    per-cell rounding would leave a residual of up to n_buses/2 units per hour,
    which breaks hourly conservation at the precision the file is written in.

    Reproduced from the generating pipeline so an expanded file matches one
    written directly; do not "simplify" it to np.round.
    """
    scale = 10 ** decimals
    scaled = values * scale
    floor = np.floor(scaled)
    remainder = scaled - floor
    target_units = np.rint(np.asarray(targets, dtype=np.float64) * scale)

    out = floor.copy()
    for r in range(out.shape[0]):
        deficit = int(round(target_units[r] - out[r].sum()))
        if deficit == 0:
            continue
        if deficit > 0:
            order = np.argsort(-remainder[r], kind="stable")[:deficit]
            out[r, order] += 1
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


def variants() -> list[str]:
    return sorted(p.stem for p in (PKG / "shares").glob("*.npz"))


def series_labels() -> list[str]:
    return sorted(p.name[: -len(".csv.gz")] if p.name.endswith(".csv.gz") else p.stem
                  for p in (PKG / "statewide").glob("*.csv*"))


def load_shares(variant: str) -> tuple[np.ndarray, list[str]]:
    path = PKG / "shares" / f"{variant}.npz"
    if not path.exists():
        raise SystemExit(f"no such variant: {variant}\navailable: {variants()}")
    z = np.load(path, allow_pickle=False)
    S = z["shares"]                                   # [288, n_buses] float64
    nodes = [str(n) for n in z["nodes"]]
    dev = np.abs(S.sum(axis=1) - 1.0).max()
    if dev > 1e-9:
        raise SystemExit(f"{variant}: share rows deviate from 1 by {dev:.3e}")
    return S, nodes


def load_series(label: str) -> pd.DataFrame:
    for name in (f"{label}.csv.gz", f"{label}.csv"):
        path = PKG / "statewide" / name
        if path.exists():
            d = pd.read_csv(path)
            need = {"datetime_pst", "hour_of_year", "cell", "y_mw"}
            missing = need - set(d.columns)
            if missing:
                raise SystemExit(f"{path.name}: missing {sorted(missing)}")
            return d
    raise SystemExit(f"no such series: {label}\navailable: {series_labels()}")


def expand(variant: str, label: str, out_path: Path, decimals: int = 1,
           chunk: int = 2000) -> dict:
    """Write one hourly wide CSV and report what it contains."""
    S, nodes = load_shares(variant)
    d = load_series(label)
    y = d.y_mw.to_numpy(dtype=np.float64)
    cells = d.cell.to_numpy(dtype=np.int64)
    if cells.min() < 0 or cells.max() >= S.shape[0]:
        raise SystemExit(f"{label}: cell out of range for a {S.shape[0]}-cell matrix")
    stamps = d.datetime_pst.to_numpy()
    hoy = d.hour_of_year.to_numpy()
    cols = [f"Demand_MW_z{n}" for n in nodes]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    annual = np.zeros(len(nodes))
    peak = np.zeros(len(nodes))      # per-bus peak
    sys_peak = 0.0                   # statewide peak -- a different quantity
    worst = 0.0
    n_neg = 0
    with __import__("gzip").open(out_path, "wt", newline="") as fh:
        fh.write(",".join(["datetime_pst", "hour_of_year", *cols]) + "\n")
        for a in range(0, len(y), chunk):
            b = min(a + chunk, len(y))
            blk = y[a:b, None] * S[cells[a:b], :]
            snapped = round_to_printed(blk, y[a:b], decimals=decimals)
            worst = max(worst, float(np.abs(snapped.sum(axis=1) - y[a:b]).max()))
            annual += snapped.sum(axis=0)
            peak = np.maximum(peak, snapped.max(axis=0))
            sys_peak = max(sys_peak, float(snapped.sum(axis=1).max()))
            n_neg += int((snapped < 0).sum())
            df = pd.DataFrame(snapped, columns=cols)
            df.insert(0, "hour_of_year", hoy[a:b])
            df.insert(0, "datetime_pst", stamps[a:b])
            df.to_csv(fh, index=False, header=False, float_format=f"%.{decimals}f")
    return {"variant": variant, "series": label, "hours": len(y),
            "n_buses": len(nodes), "annual_twh": annual.sum() / 1e6,
            "system_peak_mw": sys_peak, "max_bus_peak_mw": float(peak.max()),
            "n_negative_cells": n_neg,
            "max_hourly_conservation_error_mw": worst,
            "file_mb": out_path.stat().st_size / 1e6}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--variant")
    ap.add_argument("--series")
    ap.add_argument("--all", action="store_true",
                    help="every variant x every series (check the disk first)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--out-dir", default=str(PKG / "nodal_hourly"))
    ap.add_argument("--decimals", type=int, default=1)
    args = ap.parse_args()

    if args.list or not (args.all or (args.variant and args.series)):
        print(f"package: {PKG}")
        print(f"\nvariants ({len(variants())}):")
        for v in variants():
            print(f"  {v}")
        print(f"\nseries ({len(series_labels())}):")
        for s in series_labels():
            print(f"  {s}")
        print("\nexpand one:  python expand.py --variant <v> --series <s>")
        print("expand all:  python expand.py --all --out-dir hourly/")
        if not args.list:
            raise SystemExit("give --variant and --series, or --all")
        return

    jobs = ([(v, s) for v in variants() for s in series_labels()] if args.all
            else [(args.variant, args.series)])
    out_dir = Path(args.out_dir)
    rows = []
    for v, s in jobs:
        st = expand(v, s, out_dir / f"{v}__{s}.csv.gz", args.decimals)
        rows.append(st)
        print(f"  {v}__{s}: {st['hours']:,} h, {st['n_buses']:,} buses, "
              f"{st['annual_twh']:.2f} TWh, system peak "
              f"{st['system_peak_mw']:,.0f} MW (largest single bus "
              f"{st['max_bus_peak_mw']:,.0f}), "
              f"conservation {st['max_hourly_conservation_error_mw']:.3g} MW, "
              f"{st['file_mb']:.1f} MB")
    if len(rows) > 1:
        pd.DataFrame(rows).to_csv(out_dir / "expanded_summary.csv", index=False)
        print(f"\n{len(rows)} file(s) -> {out_dir}")


if __name__ == "__main__":
    main()
