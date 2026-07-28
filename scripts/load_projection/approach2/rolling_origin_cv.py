"""
Rolling-origin cross-validation of the Approach 2 CALIBRATION, using EIA-930
CAISO history only (no RESOLVE, no forecast) -- an honest, historical-data-only
estimate of how much the model's per-cell tracking drifts when its calibration
window and the target come from different periods.

Why this exists
---------------
Against a RESOLVE target the model's hourly tracking (check iii in
generate_stochastic.py --validate) shows a +5.56% bias. That bias has a closed
form -- with --F cal the model's expected total per cell is Sum_mu(c) (fixed,
target-independent), while the reference is F*.s(c).y(t) = implied_f(c).y(t)
with implied_f(c) = Sum_mu(c)/ybar_train(c). Their ratio per hour is therefore

    bias(t) = ybar_train(c(t)) / y_target(t) - 1

i.e. purely the mismatch between the calibration window's per-cell mean demand
and the target's. So the SAME bias must appear within CAISO whenever we
calibrate on one period and score another -- which is exactly what a forecast
does. This script measures it directly, with no RESOLVE involved, and compares
two calibration strategies:

  * expanding  -- train on all complete CAISO years <= origin T (the current
                  behavior: build_system_cells over all history).
  * trailing-N -- train only on the last N complete years (T-N, T]. Tracks
                  recent level/shape at the cost of a noisier (fewer-year)
                  estimate. This is the "rolling look-back that can run into the
                  future" idea -- re-estimate each year from the trailing window.

Each (strategy, origin) is scored one-year-ahead and at longer horizons on the
held-out later year(s). Scoring is the semi-analytic draw-mean of check (iii)
(reproduces --validate's bias/relRMSE to <0.03 pp; the omitted copula/MC noise
is ~0.4% and swamped by the drift measured here), so the whole sweep runs in
seconds instead of the multi-minute full Monte Carlo.

This is CROSS-VALIDATION of the calibration, NOT a hyperparameter search: the
model has no regularization knob to tune (mu_s/sigma_s are closed-form quantile
inversions; s(c)/rho(c)/F* are method-of-moments). The one genuine design
choice it evaluates is the calibration window itself.

Output
------
  data/checks/stochastic/rolling_origin_cv.csv
    strategy, origin_year, test_year, horizon, n_train_years, n_train_hours,
    bias_pct, relrmse_pct
  data/figures/load_projection/stochastic/rolling_origin_cv.png
    (a) one-year-ahead tracking bias by origin year, per strategy
    (b) bias vs forecast horizon, pooled per strategy (mean +/- 1 sd across origins)

Usage
-----
  python scripts/load_projection/approach2/rolling_origin_cv.py
  python scripts/load_projection/approach2/rolling_origin_cv.py --windows 3,5,7
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from load_projection.stochastic import (  # noqa: E402
    build_system_cells,
    load_caiso_history,
    load_envelope_cells,
    standardize_z,
)

CHECK_DIR = ROOT / "data" / "checks" / "stochastic"
FIG_DIR = ROOT / "data" / "figures" / "load_projection" / "stochastic"
MIN_FULL_HOURS = 8000  # a year with fewer observed hours is treated as incomplete


def score(cells_train: pd.DataFrame, test: pd.DataFrame) -> tuple[float, float]:
    """Semi-analytic check (iii): draw-mean tracking bias & relRMSE of the
    model calibrated on `cells_train` against a held-out target `test`
    (columns dt_pst_hb, demand_mw, cell). Mirrors validate_totals() without
    the Monte Carlo (see module docstring)."""
    t = standardize_z(test.copy())
    k = t.cell.values
    sum_mu = cells_train.sum_mu.reindex(range(288)).values[k]
    sum_sig = cells_train.sum_sigma.reindex(range(288)).values[k]
    rho = cells_train.rho.reindex(range(288)).values[k]
    impf = cells_train.implied_f.reindex(range(288)).values[k]
    draw_mean = sum_mu + np.sqrt(rho) * t.z.values * sum_sig  # E[total] per hour
    fy = impf * t.demand_mw.values                            # F*.s(c).y reference
    e = (draw_mean - fy) / fy
    e = e[np.isfinite(e)]
    return float(e.mean() * 100), float(np.sqrt((e ** 2).mean()) * 100)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--windows", default="3,5",
                    help="comma-separated trailing-window sizes in years (default 3,5)")
    args = ap.parse_args()
    windows = [int(w) for w in args.windows.split(",")]

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)

    env = load_envelope_cells()
    caiso = load_caiso_history()
    caiso["year"] = caiso.dt_pst_hb.dt.year

    hrs = caiso.groupby("year").size()
    full_years = sorted(hrs.index[hrs >= MIN_FULL_HOURS])
    print(f"complete CAISO years used: {full_years[0]}-{full_years[-1]} "
          f"({len(full_years)} years; {sorted(set(hrs.index) - set(full_years))} dropped as partial)")

    by_year = {y: caiso[caiso.year == y].reset_index(drop=True) for y in full_years}

    def train_cells(train_years: list[int]) -> tuple[pd.DataFrame, int]:
        tr = pd.concat([by_year[y] for y in train_years], ignore_index=True)
        cells, _ = build_system_cells(env, tr)
        return cells, len(tr)

    rows = []
    origins = full_years[:-1]  # need at least one later year to test
    for T in origins:
        test_years = [y for y in full_years if y > T]
        strategies = {"expanding": [y for y in full_years if y <= T]}
        for N in windows:
            win = [y for y in full_years if T - N < y <= T]
            if len(win) == N:  # only when the full window is available
                strategies[f"trailing{N}"] = win
        for strat, train_years in strategies.items():
            cells_train, n_hours = train_cells(train_years)
            for ty in test_years:
                bias, rrmse = score(cells_train, by_year[ty])
                rows.append({
                    "strategy": strat, "origin_year": T, "test_year": ty,
                    "horizon": ty - T, "n_train_years": len(train_years),
                    "n_train_hours": n_hours, "bias_pct": round(bias, 3),
                    "relrmse_pct": round(rrmse, 3),
                })

    df = pd.DataFrame(rows)
    out_csv = CHECK_DIR / "rolling_origin_cv.csv"
    df.to_csv(out_csv, index=False)
    print(f"wrote {out_csv.relative_to(ROOT)} ({len(df)} rows)\n")

    # ---- console summary ----------------------------------------------------
    strat_order = ["expanding"] + [f"trailing{N}" for N in windows]
    strat_order = [s for s in strat_order if s in df.strategy.unique()]

    h1 = df[df.horizon == 1]
    print("One-year-ahead (horizon = 1), across origins:")
    print(f"  {'strategy':<12}{'mean bias':>11}{'mean |bias|':>13}{'bias sd':>10}"
          f"{'mean relRMSE':>14}")
    for s in strat_order:
        g = h1[h1.strategy == s]
        print(f"  {s:<12}{g.bias_pct.mean():>+10.2f}%{g.bias_pct.abs().mean():>12.2f}%"
              f"{g.bias_pct.std():>9.2f}{g.relrmse_pct.mean():>13.2f}%")

    print("\nAll horizons pooled (how drift grows as the target moves away):")
    print(f"  {'strategy':<12}{'mean |bias|':>13}{'mean relRMSE':>14}")
    for s in strat_order:
        g = df[df.strategy == s]
        print(f"  {s:<12}{g.bias_pct.abs().mean():>12.2f}%{g.relrmse_pct.mean():>13.2f}%")

    # shape-estimation stability: how much implied_f(c) wobbles between the
    # trailing windows vs the (smoother) expanding ones -- the variance cost of
    # a short look-back. Measured as the mean over cells of the across-origin
    # std of implied_f, using each strategy's per-origin train window.
    print("\nShape-estimation noise (mean over cells of across-origin sd of implied_f):")
    for s in strat_order:
        per_origin = []
        for T in origins:
            g = df[(df.strategy == s) & (df.origin_year == T)]
            if g.empty:
                continue
            train_years = ([y for y in full_years if y <= T] if s == "expanding"
                           else [y for y in full_years if T - int(s[8:]) < y <= T])
            cells_train, _ = train_cells(train_years)
            per_origin.append(cells_train.implied_f.reindex(range(288)))
        M = pd.concat(per_origin, axis=1)
        print(f"  {s:<12}{M.std(axis=1).mean():.4f}")

    # ---- figure -------------------------------------------------------------
    colors = {"expanding": "#c0392b"}
    palette = ["#1f6f8b", "#2e8b57", "#8e44ad", "#e08e00"]
    for i, N in enumerate(windows):
        colors[f"trailing{N}"] = palette[i % len(palette)]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 5.6))

    for s in strat_order:
        g = h1[h1.strategy == s].sort_values("origin_year")
        ax1.plot(g.origin_year, g.bias_pct, marker="o", color=colors[s], label=s)
    ax1.axhline(0, color="black", lw=0.8, ls="--", alpha=0.6)
    ax1.set_xlabel("calibration origin year (test = origin + 1)")
    ax1.set_ylabel("one-year-ahead tracking bias (%)")
    ax1.set_title("(a) One-year-ahead calibration bias by origin")
    ax1.legend(fontsize=9)
    ax1.grid(alpha=0.3)

    for s in strat_order:
        g = df[df.strategy == s].groupby("horizon").bias_pct
        m, sd = g.mean(), g.std()
        ax2.plot(m.index, m.values, marker="o", color=colors[s], label=s)
        ax2.fill_between(m.index, m.values - sd.values, m.values + sd.values,
                         color=colors[s], alpha=0.15)
    ax2.axhline(0, color="black", lw=0.8, ls="--", alpha=0.6)
    ax2.set_xlabel("forecast horizon (test_year - origin_year)")
    ax2.set_ylabel("tracking bias (%)")
    ax2.set_title("(b) Bias vs horizon (mean ± 1 sd across origins)")
    ax2.legend(fontsize=9)
    ax2.grid(alpha=0.3)

    fig.suptitle("Rolling-origin CV of Approach 2 calibration (EIA-930 CAISO only, "
                 "semi-analytic check iii)", fontsize=12)
    fig.tight_layout()
    out_png = FIG_DIR / "rolling_origin_cv.png"
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    print(f"\nwrote {out_png.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
