"""
LEGACY (retired 2026-08-14) -- NOT part of the Approach 2 method.

This script tunes calibration recency by held-out one-year-ahead error, i.e. it
treats the model as a predictor. Approach 2 does not predict: it takes a load
series that is already known and decides where on the network that load sits, so
optimizing predictive generalization answers a question the project does not
ask, and cannot improve a disaggregation of a series that is given. The method
now calibrates on the series being disaggregated
(generate_stochastic.py --calibrate-on target).

Kept, not deleted: the closed-form bias result below explains why --validate's
check (iii) moves when the target changes, and the knobs it calibrated
(--calibration-window, --decay-halflife) still exist and still default off. See
docs/approach2_stochastic.md -> "LEGACY" for the measured tables and the
reasoning that retired this line of work.

--------------------------------------------------------------------------

Rolling-origin cross-validation of the Approach 2 CALIBRATION, using EIA-930
CAISO history only (no RESOLVE, no forecast) -- an honest, historical-data-only
estimate of how much the model's per-cell tracking drifts when its calibration
window and the target come from different periods.

Why this existed
----------------
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
three calibration strategies:

  * expanding  -- train on all complete CAISO years <= origin T (the current
                  behavior: build_system_cells over all history).
  * trailing-N -- train only on the last N complete years (T-N, T]. Tracks
                  recent level/shape at the cost of a noisier (fewer-year)
                  estimate. This is the "rolling look-back that can run into the
                  future" idea -- re-estimate each year from the trailing window.
  * decay-H    -- train on all complete years up to T but exponentially recency-weight them
                  (half-life H calendar days, aged from the end of year T). The
                  smooth version of a trailing window: per cell it down-weights
                  older years' occurrences of that (month, hour) rather than
                  hard-cutting them, so a half-life is a continuous knob to
                  CALIBRATE (this is the tunable hyperparameter the fixed-form
                  estimators otherwise lack -- see below).

Each (strategy, origin) is scored one-year-ahead and at longer horizons on the
held-out later year(s). Scoring is the semi-analytic draw-mean of check (iii)
(reproduces --validate's bias/relRMSE to <0.03 pp; the omitted copula/MC noise
is ~0.4% and swamped by the drift measured here), so the whole sweep runs in
seconds instead of the multi-minute full Monte Carlo.

The decay half-life IS a genuine tunable hyperparameter (the closed-form
mu_s/sigma_s and method-of-moments s(c)/rho(c)/F* have none), and this rolling
origin is exactly the CV that selects it: the strategy with the lowest
one-year-ahead relRMSE is flagged as the recency default. Caveat that this
optimizes for the one-year-ahead proxy; a specific projection whose level
differs structurally (e.g. RESOLVE's 2024-BTM net) is not exchangeable with any
held-out historical year, so the CV picks a sensible shape-recency default, not
a window guaranteed optimal for that projection's level (set that via --F).

Output
------
  data/checks/stochastic/rolling_origin_cv.csv
    strategy, origin_year, test_year, horizon, bias_pct, relrmse_pct
  data/figures/load_projection/stochastic/rolling_origin_cv.png
    (a) one-year-ahead tracking bias by origin year, per strategy
    (b) bias vs forecast horizon, pooled per strategy (mean +/- 1 sd across origins)
  data/checks/stochastic/calibration_search.csv
    the recency grid search: knob (decay/window/all-history), value, n_origins,
    bias_pct, abs_bias_pct, relrmse_pct
  data/figures/load_projection/stochastic/calibration_search.png
    (a) one-year-ahead error vs decay half-life (1 day .. 7 yr, log x)
    (b) one-year-ahead error vs hard look-back window (1 .. 7 complete years),
    both against the all-history baseline, optimum starred

Usage
-----
  python scripts/load_projection/approach2/rolling_origin_cv.py
  python scripts/load_projection/approach2/rolling_origin_cv.py --windows 3,5,7 --halflives 90,180,365,730
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
    decay_weights,
    load_caiso_history,
    load_envelope_cells,
    standardize_z,
)

CHECK_DIR = ROOT / "data" / "checks" / "stochastic"
FIG_DIR = ROOT / "data" / "figures" / "load_projection" / "stochastic"
MIN_FULL_HOURS = 8000  # a year with fewer observed hours is treated as incomplete

# Grid-search axes (calibration_search figure). Decay is the continuous knob and
# works at every scale; a hard window only spans integer years (a sub-year
# contiguous window would leave off-season cells empty). Labeled points cover
# the user's 1 day / 1 week / 1 month / 1 year .. 7 years request.
DECAY_GRID_DAYS = [1, 3, 7, 14, 30, 60, 91, 182, 365, 548, 730, 1095, 1460, 1825, 2190, 2555]
DECAY_LABELS = {1: "1d", 7: "1wk", 30: "1mo", 91: "3mo", 365: "1yr",
                730: "2yr", 1095: "3yr", 1825: "5yr", 2555: "7yr"}
WINDOW_GRID_YRS = [1, 2, 3, 4, 5, 6, 7]


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


def _sweep(env, by_year, full_years, origins, cells_fn):
    """One-year-ahead (test = origin+1) mean bias / mean |bias| / mean relRMSE
    across `origins`, for a calibration built by `cells_fn(T) -> cells`."""
    b, r = [], []
    for T in origins:
        if (T + 1) not in by_year:
            continue
        cells = cells_fn(T)
        bias, rrmse = score(cells, by_year[T + 1])
        b.append(bias)
        r.append(rrmse)
    return (float(np.mean(b)), float(np.mean(np.abs(b))),
            float(np.mean(r)), len(r))


def calibration_search(env, by_year, full_years) -> pd.DataFrame:
    """Grid-search the two recency knobs by rolling-origin one-year-ahead CV and
    write calibration_search.{png,csv}. Decay half-lives share the full up-to-T
    history, so every half-life shares ONE origin set -> clean absolute curve.
    A hard window is only definable back N years, so windows can't share a large
    common origin set; each is instead compared to all-history on its OWN maximal
    origins (a matched delta), which is fair and uses maximal data. Returns the
    tidy grid."""
    le = {T: pd.concat([by_year[y] for y in full_years if y <= T], ignore_index=True)
          for T in full_years}
    all_origins = full_years[:-1]

    def expanding_cells(T):
        return build_system_cells(env, le[T])[0]

    def decay_cells(T, H):
        tr = le[T]
        return build_system_cells(env, tr, decay_weights(tr, H, as_of=tr.dt_pst_hb.max()))[0]

    def window_cells(T, N):
        yrs = [y for y in full_years if T - N < y <= T]
        return build_system_cells(env, pd.concat([by_year[y] for y in yrs], ignore_index=True))[0]

    base_bias, base_abias, base_rrmse, base_n = _sweep(
        env, by_year, full_years, all_origins, expanding_cells)

    rows = [{"knob": "all-history", "value_days": np.nan, "value_years": np.nan,
             "n_origins": base_n, "bias_pct": round(base_bias, 3),
             "abs_bias_pct": round(base_abias, 3), "relrmse_pct": round(base_rrmse, 3),
             "delta_relrmse_pp": 0.0}]
    for H in DECAY_GRID_DAYS:
        bias, abias, rrmse, n = _sweep(env, by_year, full_years, all_origins,
                                       lambda T, H=H: decay_cells(T, H))
        rows.append({"knob": "decay", "value_days": H, "value_years": round(H / 365.25, 2),
                     "n_origins": n, "bias_pct": round(bias, 3),
                     "abs_bias_pct": round(abias, 3), "relrmse_pct": round(rrmse, 3),
                     "delta_relrmse_pp": round(rrmse - base_rrmse, 3)})
    for N in WINDOW_GRID_YRS:
        origins_N = [T for T in all_origins
                     if sum(T - N < y <= T for y in full_years) == N]
        if not origins_N:
            continue
        _, _, w_rrmse, n = _sweep(env, by_year, full_years, origins_N,
                                  lambda T, N=N: window_cells(T, N))
        w_bias, w_abias, _, _ = _sweep(env, by_year, full_years, origins_N,
                                       lambda T, N=N: window_cells(T, N))
        _, _, base_matched, _ = _sweep(env, by_year, full_years, origins_N, expanding_cells)
        rows.append({"knob": "window", "value_days": N * 365, "value_years": N,
                     "n_origins": n, "bias_pct": round(w_bias, 3),
                     "abs_bias_pct": round(w_abias, 3), "relrmse_pct": round(w_rrmse, 3),
                     "delta_relrmse_pp": round(w_rrmse - base_matched, 3)})
    grid = pd.DataFrame(rows)
    grid.to_csv(CHECK_DIR / "calibration_search.csv", index=False)

    dec = grid[grid.knob == "decay"].sort_values("value_days")
    win = grid[grid.knob == "window"].sort_values("value_years")
    dec_best = dec.loc[dec.relrmse_pct.idxmin()]
    win_best = win.loc[win.delta_relrmse_pp.idxmin()]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 5.6))

    # (a) decay half-life sweep -- absolute relRMSE / |bias|, one shared origin
    # set, so the all-history baseline is a single horizontal line.
    ax1.axhline(base_rrmse, color="#c0392b", ls=":", lw=1.3,
                label=f"all-history relRMSE ({base_rrmse:.2f}%)")
    ax1.axhline(base_abias, color="#1f6f8b", ls=":", lw=1.3,
                label=f"all-history |bias| ({base_abias:.2f}%)")
    ax1.plot(dec.value_days, dec.relrmse_pct, "-o", color="#c0392b", label="decay relRMSE")
    ax1.plot(dec.value_days, dec.abs_bias_pct, "--s", color="#1f6f8b", ms=4, label="decay |bias|")
    ax1.plot(dec_best.value_days, dec_best.relrmse_pct, "*", color="black", ms=16, zorder=5)
    ax1.annotate(f"best {int(dec_best.value_days)} d\n{dec_best.relrmse_pct:.2f}%",
                 (dec_best.value_days, dec_best.relrmse_pct),
                 textcoords="offset points", xytext=(6, 10), fontsize=9)
    ax1.set_xscale("log")
    ax1.set_xticks(list(DECAY_LABELS))
    ax1.set_xticklabels([DECAY_LABELS[d] for d in DECAY_LABELS])
    ax1.set_xlabel("decay half-life (log scale)")
    ax1.set_ylabel("one-year-ahead error (%)")
    ax1.set_title(f"(a) Soft kernel: decay half-life  (n={base_n} origins)")
    ax1.legend(fontsize=8)
    ax1.grid(alpha=0.3, which="both")

    # (b) hard window sweep -- a window is only definable back N years, so each
    # is scored against all-history on its OWN origins (a matched delta); below 0
    # means the window beats all history there. n annotated (shrinks with N).
    ax2.axhline(0, color="black", lw=1.0, ls="--", alpha=0.7, label="all-history (matched)")
    ax2.plot(win.value_years, win.delta_relrmse_pp, "-o", color="#8e44ad",
             label="window relRMSE - all-history")
    ax2.plot(win_best.value_years, win_best.delta_relrmse_pp, "*", color="black", ms=16, zorder=5)
    ax2.annotate(f"best {int(win_best.value_years)} yr\n{win_best.delta_relrmse_pp:+.2f} pp",
                 (win_best.value_years, win_best.delta_relrmse_pp),
                 textcoords="offset points", xytext=(6, -4), fontsize=9)
    for _, rr in win.iterrows():
        ax2.annotate(f"n={int(rr.n_origins)}", (rr.value_years, rr.delta_relrmse_pp),
                     textcoords="offset points", xytext=(0, 9), fontsize=7,
                     ha="center", color="#555")
    ax2.set_xlabel("hard look-back window (complete years)")
    ax2.set_ylabel("relRMSE vs matched all-history (pp; <0 = better)")
    ax2.set_title("(b) Hard window: matched delta (window is origin-limited)")
    ax2.legend(fontsize=8)
    ax2.grid(alpha=0.3)

    fig.suptitle("Calibration recency search - rolling-origin one-year-ahead CV "
                 "(EIA-930 CAISO only, semi-analytic check iii)", fontsize=12)
    fig.tight_layout()
    out = FIG_DIR / "calibration_search.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"\nGrid search: decay best = {int(dec_best.value_days)} d "
          f"(relRMSE {dec_best.relrmse_pct:.2f}% vs all-history {base_rrmse:.2f}%); "
          f"window best = {int(win_best.value_years)} yr "
          f"({win_best.delta_relrmse_pp:+.2f} pp vs matched all-history)")
    print(f"wrote {out.relative_to(ROOT)} and calibration_search.csv")
    return grid


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--windows", default="3,5",
                    help="comma-separated trailing-window sizes in years (default 3,5)")
    ap.add_argument("--halflives", default="180,365,730",
                    help="comma-separated exponential-decay half-lives in DAYS "
                         "(default 180,365,730); '' disables the decay strategies")
    args = ap.parse_args()
    windows = [int(w) for w in args.windows.split(",") if w.strip()]
    halflives = [int(h) for h in args.halflives.split(",") if h.strip()]

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

    def build_strategies(T: int) -> dict[str, tuple[pd.DataFrame, np.ndarray | None]]:
        """{name: (train_df, weights_or_None)} for calibration origin year T.
        Trailing windows are rectangular kernels; decay strategies weight the
        full up-to-T history by an exponential kernel aged from the end of year T."""
        le_T = pd.concat([by_year[y] for y in full_years if y <= T], ignore_index=True)
        as_of = le_T.dt_pst_hb.max()
        strat: dict[str, tuple] = {"expanding": (le_T, None)}
        for N in windows:
            win = [y for y in full_years if T - N < y <= T]
            if len(win) == N:
                strat[f"trailing{N}"] = (
                    pd.concat([by_year[y] for y in win], ignore_index=True), None)
        for H in halflives:
            strat[f"decay{H}"] = (le_T, decay_weights(le_T, H, as_of=as_of))
        return strat

    rows, implied_f = [], {}  # implied_f[(strategy, T)] = per-cell f, for shape noise
    origins = full_years[:-1]  # need at least one later year to test
    for T in origins:
        test_years = [y for y in full_years if y > T]
        for strat, (train_df, wts) in build_strategies(T).items():
            cells_train, _ = build_system_cells(env, train_df, wts)
            implied_f[(strat, T)] = cells_train.implied_f.reindex(range(288))
            for ty in test_years:
                bias, rrmse = score(cells_train, by_year[ty])
                rows.append({
                    "strategy": strat, "origin_year": T, "test_year": ty,
                    "horizon": ty - T, "bias_pct": round(bias, 3),
                    "relrmse_pct": round(rrmse, 3),
                })

    df = pd.DataFrame(rows)
    out_csv = CHECK_DIR / "rolling_origin_cv.csv"
    df.to_csv(out_csv, index=False)
    print(f"wrote {out_csv.relative_to(ROOT)} ({len(df)} rows)\n")

    # ---- console summary ----------------------------------------------------
    strat_order = (["expanding"] + [f"trailing{N}" for N in windows]
                   + [f"decay{H}" for H in halflives])
    strat_order = [s for s in strat_order if s in df.strategy.unique()]

    h1 = df[df.horizon == 1]
    print("One-year-ahead (horizon = 1), across origins:")
    print(f"  {'strategy':<12}{'mean bias':>11}{'mean |bias|':>13}{'bias sd':>10}"
          f"{'mean relRMSE':>14}")
    best = min(strat_order, key=lambda s: h1[h1.strategy == s].relrmse_pct.mean())
    for s in strat_order:
        g = h1[h1.strategy == s]
        star = "  <- best relRMSE" if s == best else ""
        print(f"  {s:<12}{g.bias_pct.mean():>+10.2f}%{g.bias_pct.abs().mean():>12.2f}%"
              f"{g.bias_pct.std():>9.2f}{g.relrmse_pct.mean():>13.2f}%{star}")

    print("\nAll horizons pooled (how drift grows as the target moves away):")
    print(f"  {'strategy':<12}{'mean |bias|':>13}{'mean relRMSE':>14}")
    for s in strat_order:
        g = df[df.strategy == s]
        print(f"  {s:<12}{g.bias_pct.abs().mean():>12.2f}%{g.relrmse_pct.mean():>13.2f}%")

    # shape-estimation stability: mean over cells of the across-origin sd of
    # implied_f(c) -- the variance cost of a short look-back / short half-life.
    print("\nShape-estimation noise (mean over cells of across-origin sd of implied_f):")
    for s in strat_order:
        cols = [implied_f[(s, T)] for T in origins if (s, T) in implied_f]
        print(f"  {s:<12}{pd.concat(cols, axis=1).std(axis=1).mean():.4f}")

    # ---- figure -------------------------------------------------------------
    colors = {"expanding": "#c0392b"}
    palette = ["#1f6f8b", "#2e8b57", "#8e44ad", "#e08e00"]
    for i, N in enumerate(windows):
        colors[f"trailing{N}"] = palette[i % len(palette)]
    decay_palette = ["#d98880", "#a04000", "#7d3c98", "#117864"]
    for i, H in enumerate(halflives):
        colors[f"decay{H}"] = decay_palette[i % len(decay_palette)]

    ls = lambda s: "--" if s.startswith("decay") else "-"  # noqa: E731

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 5.6))

    for s in strat_order:
        g = h1[h1.strategy == s].sort_values("origin_year")
        ax1.plot(g.origin_year, g.bias_pct, marker="o", color=colors[s], ls=ls(s), label=s)
    ax1.axhline(0, color="black", lw=0.8, ls="--", alpha=0.6)
    ax1.set_xlabel("calibration origin year (test = origin + 1)")
    ax1.set_ylabel("one-year-ahead tracking bias (%)")
    ax1.set_title("(a) One-year-ahead calibration bias by origin")
    ax1.legend(fontsize=9)
    ax1.grid(alpha=0.3)

    for s in strat_order:
        g = df[df.strategy == s].groupby("horizon").bias_pct
        m, sd = g.mean(), g.std()
        ax2.plot(m.index, m.values, marker="o", color=colors[s], ls=ls(s), label=s)
        ax2.fill_between(m.index, m.values - sd.values, m.values + sd.values,
                         color=colors[s], alpha=0.12)
    ax2.axhline(0, color="black", lw=0.8, ls="--", alpha=0.6)
    ax2.set_xlabel("forecast horizon (test_year - origin_year)")
    ax2.set_ylabel("tracking bias (%)")
    ax2.set_title("(b) Bias vs horizon (mean +/- 1 sd across origins)")
    ax2.legend(fontsize=9)
    ax2.grid(alpha=0.3)

    fig.suptitle("Rolling-origin CV of Approach 2 calibration (EIA-930 CAISO only, "
                 "semi-analytic check iii)", fontsize=12)
    fig.tight_layout()
    out_png = FIG_DIR / "rolling_origin_cv.png"
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    print(f"\nwrote {out_png.relative_to(ROOT)}")

    # ---- dedicated recency grid-search figure + table -----------------------
    grid = calibration_search(env, by_year, full_years)
    show = grid[grid.knob.isin(["all-history", "decay"])]
    show = show[show.value_days.isin(list(DECAY_LABELS)) | show.value_days.isna()]
    print("\nDecay grid (labeled points):")
    print(f"  {'half-life':>10}{'relRMSE':>10}{'bias':>9}{'|bias|':>9}")
    for _, r in show.iterrows():
        lbl = "all-history" if r.knob == "all-history" else DECAY_LABELS[int(r.value_days)]
        print(f"  {lbl:>10}{r.relrmse_pct:>9.2f}%{r.bias_pct:>+8.2f}%{r.abs_bias_pct:>8.2f}%")


if __name__ == "__main__":
    main()
