"""Check a tidy long envelope file before handing it to the model.

The long contract is how a non-utility load input enters Approach 2: one row per
(unit, cell, percentile), so a set of nodes carrying a single seasonal load value
is expressible without changing any model equation.

    unit_id,cell_label,percentile,load_mw
    1015,MayOct,0.50,56.63
    1015,NovApr,0.50,41.20
    2033,MayOct,0.50,12.19

`envelopes.from_long` raises on the first problem it meets, which is the right
behavior mid-run but a poor way to fix a file. This script reports EVERY problem
at once, then previews what the model would make of the input: the fitted mu, the
sigma each --sigma-source would supply, and whether rho(c) stays feasible.

--template writes a skeleton for the chosen --cells spec, with one row per
(unit, cell) at p=0.50, so a new file starts out well formed.

Usage
  python scripts/load_projection/approach2/validate_long_envelope.py path.csv
  python scripts/load_projection/approach2/validate_long_envelope.py path.csv --cells season3
  python scripts/load_projection/approach2/validate_long_envelope.py out.csv --template --units 1,2,3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from load_projection import cells as cellspecs  # noqa: E402
from load_projection import envelopes as envlib  # noqa: E402
from load_projection.stochastic import (  # noqa: E402
    _cell_moments,
    build_system_cells,
    load_caiso_history,
)

REQUIRED = ["unit_id", "cell_label", "percentile", "load_mw"]


def write_template(path: Path, spec, units: list[str], percentile: float) -> None:
    labels = [str(v) for v in spec.labels.iloc[:, 0]]
    rows = [(u, c, percentile, "") for u in units for c in labels]
    pd.DataFrame(rows, columns=REQUIRED).to_csv(path, index=False)
    print(f"wrote template {path}: {len(units)} units x {len(labels)} cells "
          f"({', '.join(labels)}) at p={percentile}")
    print("fill load_mw, then re-run this script without --template")


def check(df: pd.DataFrame, spec) -> list[str]:
    """Every problem, not just the first."""
    problems: list[str] = []
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        # the commonest mistake is pointing this at the WIDE seasonal file, which
        # a different tool reads -- say so instead of just listing what is absent
        wide = {"summer_load", "winter_load"}
        if wide & set(df.columns):
            problems.append(
                "this looks like the WIDE seasonal format (it has "
                f"{sorted(wide & set(df.columns))}), which this script does not "
                "read. Build its weight tables with "
                "scripts/load_projection/external_loads/build_external_weights.py "
                "-- that validates the file itself before doing any work. This "
                "script is only for the long contract "
                f"({', '.join(REQUIRED)}), used to feed Approach 2 directly.")
        else:
            problems.append(
                f"missing required columns: {missing} (have {list(df.columns)})")
        return problems

    if len(spec.key_cols) != 1:
        problems.append(
            f"--cells {spec.name} has key columns {spec.key_cols}; the long "
            f"contract addresses cells by one cell_label. Use month, season3, "
            f"halfyear or custom:<path>")
        return problems

    valid = [str(v) for v in spec.labels.iloc[:, 0]]
    unknown = sorted(set(df.cell_label.astype(str)) - set(valid))
    if unknown:
        problems.append(f"cell_label values not in spec {spec.name}: {unknown} "
                        f"(valid: {valid})")

    bad_p = df[(df.percentile <= 0) | (df.percentile >= 1)]
    if len(bad_p):
        problems.append(f"{len(bad_p)} rows have a percentile outside (0, 1), "
                        f"e.g. {sorted(bad_p.percentile.unique())[:5]}")

    nan_load = int(df.load_mw.isna().sum())
    if nan_load:
        problems.append(f"{nan_load} rows have a blank/NaN load_mw")

    counts = df.groupby(["unit_id", "cell_label"]).percentile.agg(["count", "nunique"])
    if counts["count"].nunique() > 1:
        hist = counts["count"].value_counts().sort_index().to_dict()
        problems.append(f"ragged percentile sets: every (unit_id, cell_label) needs "
                        f"the same count; found {hist}")
    dup = counts[counts["count"] != counts["nunique"]]
    if len(dup):
        problems.append(f"{len(dup)} (unit_id, cell_label) groups repeat a percentile, "
                        f"e.g. {dup.index[0]}")

    per_unit = df.groupby("unit_id").cell_label.nunique()
    short = per_unit[per_unit < len(valid)]
    if len(short):
        problems.append(f"{len(short)} units do not cover all {len(valid)} cells, "
                        f"e.g. {list(short.index[:5])}")

    neg = int((df.load_mw < 0).sum())
    if neg:
        problems.append(f"NOTE (not fatal): {neg} rows carry a negative load_mw; "
                        f"these are kept as-is, matching the net-of-BTM convention")
    return problems


def preview(df: pd.DataFrame, spec) -> None:
    """What the model would make of this input."""
    caiso = load_caiso_history(spec)
    cy = _cell_moments(caiso, None, spec.n_cells)
    sd_c = cy.sd.reindex(range(spec.n_cells)).to_numpy()
    ybar_c = cy.ybar.reindex(range(spec.n_cells)).to_numpy()
    k = int(df.groupby(["unit_id", "cell_label"]).percentile.count().iloc[0])

    print(f"\ninput: {df.unit_id.nunique():,} units x "
          f"{df.cell_label.nunique()} cells x {k} percentile(s) "
          f"= {len(df):,} rows")
    if k > 1:
        env = envlib.from_long(df, spec)
        cl, f_star = build_system_cells(env, caiso, spec=spec)
        print(f"  {k} percentiles identify sigma directly; no prior needed")
        print(f"  F* = {f_star:.4f}   rho median {cl.rho.median():.4f} "
              f"(capped cells {int((cl.rho >= 1.0).sum())})")
        return

    print(f"  one percentile (p={df.percentile.iloc[0]}) does NOT identify sigma; "
          f"--sigma-source supplies it")
    from scipy.stats import norm
    print(f"  {'sigma source':>22s} {'rho median':>11s} {'sigma/mu':>9s} "
          f"{'P(L<0) med':>11s} {'P>10%':>7s} {'capped':>7s}")
    for kind, val in [("input-crosssec", None), ("pinned-rho", None),
                      ("scalar", 2.5), ("proportional-cv", 0.32)]:
        try:
            src = envlib.SigmaSource(kind=kind, value=val)
            env = envlib.from_long(df, spec, sigma_source=src,
                                   sd_c=sd_c, ybar_c=ybar_c)
            cl, _ = build_system_cells(env, caiso, spec=spec)
            ok = (env.sigma > 0) & np.isfinite(env.mu) & np.isfinite(env.sigma)
            pneg = norm.cdf(-(env.mu[ok].to_numpy() / env.sigma[ok].to_numpy()))
            ratio = (env.sigma[ok] / env.mu[ok]).replace([np.inf, -np.inf], np.nan)
            tag = kind + (f" {val}" if val is not None else "")
            print(f"  {tag:>22s} {cl.rho.median():11.4f} {ratio.median():9.3f} "
                  f"{np.median(pneg) * 100:10.1f}% {np.mean(pneg > 0.10) * 100:6.1f}% "
                  f"{int((cl.rho >= 1.0).sum()):7d}")
        except Exception as exc:  # a source may be infeasible for this input
            print(f"  {kind:>22s} unavailable: {exc}")
    n_per_cell = df.groupby("cell_label").size().to_numpy()
    floor = envlib.sigma_floor_for_rho(sd_c, n_per_cell)
    print(f"  sigma floor for rho <= 1 (sd_c/N): "
          + ", ".join(f"{v:.3f} MW" for v in floor))
    print("  a sigma taken from the cross-sectional spread of the loads measures "
          "inter-unit\n  inequality, which mu already carries -- see "
          "src/load_projection/envelopes.py")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", help="the long envelope CSV (or the file to write "
                                 "with --template)")
    ap.add_argument("--cells", default="halfyear",
                    help="cell spec the cell_label values belong to (default halfyear)")
    ap.add_argument("--template", action="store_true",
                    help="write a skeleton instead of checking an existing file")
    ap.add_argument("--units", default="1,2,3",
                    help="with --template: comma-separated unit_id values")
    ap.add_argument("--percentile", type=float, default=0.50,
                    help="with --template: the percentile to pre-fill (default 0.50)")
    args = ap.parse_args()

    spec = cellspecs.get_spec(args.cells)
    path = Path(args.path)
    if args.template:
        write_template(path, spec, args.units.split(","), args.percentile)
        return

    df = pd.read_csv(path)
    problems = check(df, spec)
    fatal = [p for p in problems if not p.startswith("NOTE")]
    for p in problems:
        print(("  NOTE " if p.startswith("NOTE") else "  PROBLEM ") +
              p.removeprefix("NOTE (not fatal): "))
    if fatal:
        print(f"\n{len(fatal)} problem(s) must be fixed before this file can be used")
        sys.exit(1)
    print(f"{path.name}: contract OK")
    preview(df, spec)


if __name__ == "__main__":
    main()
