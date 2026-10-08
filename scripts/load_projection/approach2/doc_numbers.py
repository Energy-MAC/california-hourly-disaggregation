"""Recompute every number quoted in the Approach 2 generalized-envelope docs.

The generalized input layer (arbitrary percentile sets, configurable cell
granularity) introduced measured figures into CLAUDE.md, the README, the model
spec and the approach2-stochastic skill: the seasonal-partition ranking, the CV
of the envelopes at each cell granularity, rho under each coarsening rule, and
the sigma-source diagnostics for a single-percentile input. Prose goes stale
silently; this script recomputes each figure from primary sources and prints it
labeled with WHERE it is quoted, so a doc edit can be checked against a fresh run.

Mirrors scripts/load_projection/genx/doc_numbers.py, the established pattern
(user rule 2026-08-14: refresh the recompute script BEFORE editing any quoted
measured number).

  A. seasonal partitions   which contiguous 6-month split of the year explains
                           the most month-hour load variance (CAISO + envelopes)
  B. CV by granularity     envelope CV under variance-preserving vs averaged
                           coarsening, per cell spec
  C. rho feasibility       rho(c) actually produced by each cell spec and
                           coarsening rule, and whether the min(1,.) cap binds
  D. sigma sources         for a single-percentile input: rho, sigma/mu, P(L<0)
                           and the rho<=1 feasibility floor per sigma source
  E. backward compatibility the K=2 fit reproduces the historical closed form

Usage
  python scripts/load_projection/approach2/doc_numbers.py
  python scripts/load_projection/approach2/doc_numbers.py --sections A,D
"""

from __future__ import annotations

import argparse
import glob
import os
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
    load_envelope_cells,
)

OUT_FILE = ROOT / "data/checks/approach2/doc_numbers.txt"
CONTROL_GLOB = str(ROOT / "genx/scenarios_rescaled/genx__control/p*/system/Demand_data.csv")
REP_WEEK_HALFYEAR = {"p5": "NovApr", "p6": "NovApr",      # Winter rep week (Dec)
                     "p12": "MayOct", "p13": "MayOct",    # Spring (late May/Jun)
                     "p19": "MayOct", "p20": "MayOct",    # Summer (Aug)
                     "p26": "MayOct", "p27": "MayOct"}    # Fall (late Aug/Sep)

MONTH_NAMES = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
               7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}

_lines: list[str] = []


def say(msg: str = "") -> None:
    print(msg)
    _lines.append(msg)


def _partition_scores(surface: pd.Series, weights: pd.Series | None = None):
    """Share of month-hour variance explained by each contiguous 6-month split.

    `surface` is indexed by (month, hour_pst). Returns rows sorted best first.
    """
    vals = surface.to_numpy(dtype=float)
    months = surface.index.get_level_values("month").to_numpy()
    w = np.ones_like(vals) if weights is None else weights.to_numpy(dtype=float)

    def wvar(v, wt):
        m = np.average(v, weights=wt)
        return np.average((v - m) ** 2, weights=wt)

    total = wvar(vals, w)
    rows = []
    for k in range(1, 7):
        A = [((k - 1 + i) % 12) + 1 for i in range(6)]
        # the complement as a CONTIGUOUS arc, so its label reads Nov-Apr rather
        # than Jan-Dec (both blocks wrap the year boundary by construction)
        B = [((A[-1] + i) % 12) + 1 for i in range(6)]
        within = 0.0
        for blk in (A, B):
            msk = np.isin(months, blk)
            within += wvar(vals[msk], w[msk]) * w[msk].sum() / w.sum()
        means = [np.average(vals[np.isin(months, blk)], weights=w[np.isin(months, blk)])
                 for blk in (A, B)]
        rows.append({"a": f"{MONTH_NAMES[A[0]]}-{MONTH_NAMES[A[-1]]}",
                     "b": f"{MONTH_NAMES[B[0]]}-{MONTH_NAMES[B[-1]]}",
                     "explained_pct": 100 * (1 - within / total),
                     "mean_a": means[0], "mean_b": means[1]})
    return sorted(rows, key=lambda r: -r["explained_pct"]), total


def section_a(ctx: dict) -> None:
    say("=== [A] Seasonal 6-month partitions (cells.py docstring; "
        "docs/stochastic_model_spec.md; CLAUDE.md) ===")
    caiso = ctx["caiso"]
    c = caiso[caiso.dt_pst_hb.dt.year >= 2016]
    g = c.groupby(["month", "hour_pst"]).demand_mw.agg(["mean", "count"])
    rows_c, _ = _partition_scores(g["mean"] / c.demand_mw.mean(), g["count"])

    env = ctx["env_mh"]
    e = env.groupby(["month", "hour_pst"]).mu.sum()
    rows_e, _ = _partition_scores(e / e.mean())

    by_e = {(r["a"], r["b"]): r for r in rows_e}
    say(f"  {'partition':>22s} {'CAISO 2016+':>12s} {'envelopes':>11s}   block means (CAISO)")
    for r in rows_c:
        key = (r["a"], r["b"])
        say(f"  {r['a'] + ' vs ' + r['b']:>22s} {r['explained_pct']:11.1f}% "
            f"{by_e[key]['explained_pct']:10.1f}%   "
            f"{r['mean_a']:.3f} / {r['mean_b']:.3f}")
    best = rows_c[0]
    say(f"  -> best contiguous split: {best['a']} vs {best['b']} "
        f"(spring lumps with winter)")
    say("  monthly load index (CAISO 2016+, mean = 1):")
    mi = c.groupby("month").demand_mw.mean()
    say("    " + "  ".join(f"{MONTH_NAMES[m]} {v / mi.mean():.3f}" for m, v in mi.items()))
    say()


def _spec_table(ctx: dict, specs=("monthhour", "month", "season3", "halfyear")):
    """Per cell spec and coarsening mode: envelope CV, rho, F*."""
    out = []
    for name in specs:
        spec = cellspecs.get_spec(name)
        caiso = load_caiso_history(spec)
        modes = ["variance"] if name == "monthhour" else ["variance", "average"]
        for mode in modes:
            env = load_envelope_cells(spec, coarsen_mode=mode)
            cl, f_star = build_system_cells(env, caiso, spec=spec)
            out.append({"spec": name, "mode": mode, "n_cells": spec.n_cells,
                        "f_star": f_star, "cv": (cl.sum_sigma / cl.sum_mu),
                        "rho": cl.rho})
    return out


def section_b(ctx: dict) -> None:
    say("=== [B] Envelope CV by cell granularity and coarsening rule "
        "(envelopes.coarsen docstring; docs/stochastic_model_spec.md) ===")
    say("  CV = sum(sigma) / sum(mu) over the covered substations in each cell")
    say(f"  {'cell spec':>12s} {'mode':>9s} {'n_cells':>8s} "
        f"{'CV median':>10s} {'CV min':>8s} {'CV max':>8s}")
    for r in ctx["spec_rows"]:
        say(f"  {r['spec']:>12s} {r['mode']:>9s} {r['n_cells']:8d} "
            f"{r['cv'].median():10.4f} {r['cv'].min():8.4f} {r['cv'].max():8.4f}")
    say("  -> averaging sigma across the fine cells discards the diurnal swing, "
        "so it understates CV")
    say()


def section_c(ctx: dict) -> None:
    say("=== [C] rho(c) feasibility by cell spec and coarsening rule "
        "(envelopes.coarsen docstring; docs/stochastic_model_spec.md) ===")
    say("  rho(c) = min(1, (implied_f * sd_c / sum_sigma)^2) = (CV_target / CV_unit)^2")
    say(f"  {'cell spec':>12s} {'mode':>9s} {'F*':>7s} {'rho median':>11s} "
        f"{'rho min':>8s} {'rho max':>8s} {'capped':>7s}")
    for r in ctx["spec_rows"]:
        say(f"  {r['spec']:>12s} {r['mode']:>9s} {r['f_star']:7.4f} "
            f"{r['rho'].median():11.4f} {r['rho'].min():8.4f} {r['rho'].max():8.4f} "
            f"{int((r['rho'] >= 1.0).sum()):7d}")
    var = {r["spec"]: r for r in ctx["spec_rows"] if r["mode"] == "variance"}
    avg = {r["spec"]: r for r in ctx["spec_rows"] if r["mode"] == "average"}
    for k in avg:
        say(f"  {k}: averaging inflates max rho {var[k]['rho'].max():.3f} -> "
            f"{avg[k]['rho'].max():.3f} "
            f"({avg[k]['rho'].max() / var[k]['rho'].max():.1f}x toward the cap)")
    say()


def _cats_single_percentile() -> pd.DataFrame | None:
    """A one-value-per-node-per-half-year envelope built from the GenX control
    demand: the median load of each loaded CATS bus within each half-year block.
    Stands in for a real single-percentile nodal input so section D is
    self-contained and reproducible."""
    files = sorted(glob.glob(CONTROL_GLOB))
    if not files:
        return None
    acc: dict[str, list[np.ndarray]] = {}
    zones: list[str] = []
    for f in files:
        case = os.path.basename(os.path.dirname(os.path.dirname(f)))
        blk = REP_WEEK_HALFYEAR.get(case)
        if blk is None:
            continue
        d = pd.read_csv(f)
        z = [c for c in d.columns if c.startswith("Demand_MW_z")]
        zones = [c.replace("Demand_MW_z", "") for c in z]
        acc.setdefault(blk, []).append(d[z].to_numpy(float))
    rows = []
    for blk, mats in acc.items():
        med = np.median(np.vstack(mats), axis=0)
        for b, v in zip(zones, med):
            if v > 0:
                rows.append((b, blk, 0.50, float(v)))
    return pd.DataFrame(rows, columns=["unit_id", "cell_label", "percentile", "load_mw"])


def section_d(ctx: dict) -> None:
    say("=== [D] sigma sources for a single-percentile input "
        "(envelopes.SigmaSource docstring; CLAUDE.md; README) ===")
    long_env = _cats_single_percentile()
    if long_env is None:
        say("  SKIPPED: no GenX control demand tree found")
        say()
        return
    spec = cellspecs.get_spec("halfyear")
    caiso = load_caiso_history(spec)
    cy = _cell_moments(caiso, None, spec.n_cells)
    sd_c = cy.sd.reindex(range(spec.n_cells)).to_numpy()
    ybar_c = cy.ybar.reindex(range(spec.n_cells)).to_numpy()

    n_units = long_env.unit_id.nunique()
    say(f"  input: {n_units:,} loaded CATS buses x {spec.n_cells} half-year cells, "
        f"one p50 value each")
    mu_in = long_env.groupby("cell_label").load_mw
    say(f"  cross-sectional sd of the input loads: "
        + ", ".join(f"{k} {v:.3f} MW" for k, v in mu_in.std().items()))
    say(f"  median nodal load: "
        + ", ".join(f"{k} {v:.3f} MW" for k, v in mu_in.median().items()))

    say(f"  {'sigma source':>22s} {'rho median':>11s} {'sigma/mu':>9s} "
        f"{'P(L<0) med':>11s} {'P>10%':>7s} {'capped':>7s}")
    variants = [("input-crosssec", None), ("pinned-rho", None),
                ("scalar", 2.5), ("proportional-cv", 0.32)]
    for kind, val in variants:
        src = envlib.SigmaSource(kind=kind, value=val, rho_target=0.231)
        env = envlib.from_long(long_env, spec, sigma_source=src,
                               sd_c=sd_c, ybar_c=ybar_c)
        cl, _ = build_system_cells(env, caiso, spec=spec)
        ok = (env.sigma > 0) & np.isfinite(env.mu) & np.isfinite(env.sigma)
        from scipy.stats import norm
        pneg = norm.cdf(-(env.mu[ok].to_numpy() / env.sigma[ok].to_numpy()))
        ratio = (env.sigma[ok] / env.mu[ok]).replace([np.inf, -np.inf], np.nan)
        tag = kind + (f" {val}" if val is not None else "")
        say(f"  {tag:>22s} {cl.rho.median():11.4f} {ratio.median():9.3f} "
            f"{np.median(pneg) * 100:10.1f}% {np.mean(pneg > 0.10) * 100:6.1f}% "
            f"{int((cl.rho >= 1.0).sum()):7d}")
    n_per_cell = long_env.groupby("cell_label").size().to_numpy()
    cl_ref, _ = build_system_cells(
        envlib.from_long(long_env, spec,
                         sigma_source=envlib.SigmaSource("pinned-rho"),
                         sd_c=sd_c, ybar_c=ybar_c), caiso, spec=spec)
    floor = envlib.sigma_floor_for_rho(sd_c, n_per_cell,
                                       f=cl_ref.implied_f.to_numpy())
    say(f"  sigma floor for rho <= 1 (f*sd_c/N): "
        + ", ".join(f"{v:.3f} MW" for v in floor))
    say("  -> the cross-sectional sd measures inter-unit INEQUALITY, which mu "
        "already carries;")
    say("     used as sigma it becomes hour-to-hour noise and collapses rho.")
    say()


def section_e(ctx: dict) -> None:
    say("=== [E] Backward compatibility of the generalized fit "
        "(envelopes.py docstring; docs/stochastic_model_spec.md) ===")
    rng = np.random.default_rng(1)
    lo = rng.uniform(-8, 60, 300_000)
    hi = lo + rng.uniform(0, 45, 300_000)
    lo[:50] = np.nan
    hi[50:100] = np.nan
    hi[100:150] = lo[100:150]
    mu_o = (lo + hi) / 2
    sd_o = (hi - lo) / (2 * envlib.Z90)
    mu_g, sd_g = envlib.fit_normal(np.stack([lo, hi]), envlib.LEGACY_PERCENTILES)
    from scipy.stats import norm
    z = norm.ppf(np.asarray(envlib.LEGACY_PERCENTILES))
    say(f"  Phi^-1(0.9) - Phi^-1(0.1) == 2 * Z90 exactly: "
        f"{bool((z[1] - z[0]) == 2 * envlib.Z90)}")
    say(f"  mu    bit-identical over 300,000 random envelopes: "
        f"{bool(np.array_equal(mu_g, mu_o, equal_nan=True))}")
    say(f"  sigma bit-identical over 300,000 random envelopes: "
        f"{bool(np.array_equal(sd_g, sd_o, equal_nan=True))}")
    mh = cellspecs.MONTHHOUR
    m = rng.integers(1, 13, 200_000)
    h = rng.integers(0, 24, 200_000)
    say(f"  monthhour cell index == (month-1)*24 + hour, same dtype: "
        f"{bool(np.array_equal(cellspecs.encode(mh, m, h), (m - 1) * 24 + h))}")
    say(f"  default cell count: {mh.n_cells}")
    say()


SECTIONS = {"A": section_a, "B": section_b, "C": section_c,
            "D": section_d, "E": section_e}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sections", default="ABCDE",
                    help="comma-free letters, e.g. AD (default: all)")
    args = ap.parse_args()
    want = [s for s in SECTIONS if s in args.sections.upper().replace(",", "")]

    ctx: dict = {}
    if any(s in want for s in "ABC"):
        ctx["caiso"] = load_caiso_history()
        ctx["env_mh"] = load_envelope_cells()
    if any(s in want for s in "BC"):
        ctx["spec_rows"] = _spec_table(ctx)

    for s in want:
        SECTIONS[s](ctx)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text("\n".join(_lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
