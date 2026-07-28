# Approach 2 — Stochastic conditional disaggregation (operational reference)

The README carries the model summary, the equations, and the headline
validation/calibration **tables**. This document holds the operational reference
(scripts, parameters, output files, figures) and the extended calibration
discussion. Theory and derivations live in
[stochastic_model_spec.md](stochastic_model_spec.md).

## Scripts and parameters

`scripts/load_projection/approach2/estimate_stochastic.py` — no arguments; writes
the three parameter tables below.

`scripts/load_projection/approach2/generate_stochastic.py`:

| Flag | Options | Default |
|------|---------|---------|
| `--target` | `eia930` or path to CSV (`dt_pst_hb`, `demand_mw`; CAISO-total series) | `eia930` |
| `--family` | `normal`, `uniform`, `both` | `both` |
| `--F` | `cal` (= F\*) or float, e.g. `0.80` | `cal` |
| `--z-mode` | `native` (standardize target in its own cells; observed trajectory shared by all draws — allocation-only uncertainty), `bootstrap` (month-matched blocks of historical z, **redrawn per draw** so weather years vary across the ensemble) | `native` |
| `--block-days` | int | `7` |
| `--calibration-window` | int N — calibrate F\*/s(c)/ρ(c) on the last N complete CAISO years (hard rectangular window); appends `__cw{N}` | all history |
| `--decay-halflife` | float H (calendar days) — recency-weight the calibration with an exponential soft kernel; composes with the window; appends `__hl{H}` | unweighted |
| `--n-draws` | int | `5` |
| `--year-start` / `--year-end` | subset target years | all |
| `--seed` | int | `0` |
| `--validate` | flag — run the three spec checks | off |
| `--save-output` | flag — hourly wide parquet per draw (~47 MB/draw-year) | off |

Other Approach 2 scripts:

- `build_resolve_target.py` — writes the CAISO-consistent RESOLVE hourly target
  (`data/processed/resolve/resolve_caiso_target.csv`, 23 weather years, PGE+SCE+SDGE
  net) for feeding to `generate_stochastic.py --target` as an out-of-sample check.
- `rolling_origin_cv.py` — rolling-origin CV that calibrates the recency of the
  shape/level (expanding vs trailing-N window vs decay-H soft kernel), EIA-930 only.
  Args `--windows`, `--halflives`. See "Calibration" below.
- `stochastic_diagnostics.py` — hygiene, implied-f, ρ feasibility, year-decomposition
  sanity checks.
- `plot_stochastic.py` — figures (see below). Args `--which`, `--substation`,
  `--n-draws`, `--year`, `--weather-year`, `--future-year`, `--month`, `--day`, `--seed`.

## Output files

Parameter tables (`data/processed/load_projection/stochastic/`, always):

```
substation_cell_params.csv   387,864 rows: q10/q90, μ/σ, unif a/b,
                             zero_width / inverted / missing flags
system_cell_params.csv       288 rows: Σμ, Σσ, ȳ, sd, implied_f, shape_s, ρ, F*
caiso_z_history.csv          92,089 rows: dt_pst_hb, demand_mw, z (2015–2025)
diagnostic_cells.csv         per-cell diagnostics (stochastic_diagnostics.py)
diagnostic_hygiene.csv       inverted-cell list
hygiene_report.md            anatomy of inverted / zero-width / missing / negative cells
```

Generation runs (`data/processed/load_projection/projections/<run_tag>/` where
`run_tag = stochastic__{target}__{family}__F{level}__{zmode}[__cw{N}][__hl{H}]`):

```
substation_annual_mwh.csv      always: (substation, year, draw) annual energy
validation_totals_cells.csv    --validate: per-cell total q10/q90 vs target
validation_marginals_subs.csv  --validate: per-substation envelope recovery
draws/draw{k}.parquet          --save-output: hourly wide matrix per draw
```

## Hygiene handling

172 inverted cells swapped; 10,110 zero-width cells (dead SCE substations)
deterministic at value; 1,848 missing/NaN cells excluded (6 SCE substations have no
data at all: Autobody, Kempster, Line Creek, Modoc, Paularino, Topanga); negatives
kept (net-of-BTM reverse flows). Anatomy in `hygiene_report.md`.

## Calibration and recency (extended discussion)

The README carries the headline tables (the rolling-origin CV strategy table, the
decay grid-search table, and the two calibration-window `--validate` tables). The
mechanics and the derivations behind them:

**The tracking bias has a closed form.** With `--F cal`, the model's expected total
per cell collapses to Σμ_s(c) — the fixed envelope sum — for *any* target, because
z(t) is standardized to mean 0 within its own cell. The `--validate` check (iii)
scores that output against the model's own reference `F·s(c)·y(t)`, and since
`s(c) = Σμ_s(c) / (F*·ȳ_train(c))`, the per-hour ratio works out to

```
bias(t) = ȳ_train(c) / y_target(t) − 1
```

i.e. purely the mismatch between the calibration window's per-cell mean demand and
the target's. Against RESOLVE, energy-weighted ȳ_CAISO / ȳ_RESOLVE = **1.047**
(CAISO 2015–2025 mean ~4.7% above RESOLVE's 2024-BTM-net cells).

**This bias is exactly F-invariant** (verified: identical at F = cal / 0.60 / 0.85):
`--F` scales output and reference together, so it cancels in the self-referential
metric. `--F` still sets the projection's *absolute output level* — it just doesn't
move this metric. The lever the metric *does* respond to is the calibration window
(which moves `ȳ_train`, hence the reference `f(c)`).

**The calibration is a genuine train/test split; the projection is not.** The
rolling-origin CV (`rolling_origin_cv.py`) is a textbook chronological split within
EIA-930: calibrate on years ≤ T, score the held-out year T+1, slide T forward. It
honestly measures *near-term* generalization and selects the recency default. It
does **not** certify a decades-out RESOLVE projection, because that projection is a
different regime (2040 BTM, different scope) with no ground truth — no historical
fold is drawn from its distribution. So recency is a near-term default; the
projection's level comes from `--F`.

**Recency helps one-year-ahead, but not the full targets.** On held-out
one-year-ahead relRMSE the soft-kernel decay at H ≈ 365 d wins (5.97% vs trailing-5
6.27% vs all-history 6.57%) because it keeps all the data (smoother s(c)/ρ(c)) while
emphasizing recency. But on the *full* EIA-930 record all-history is best (in-sample),
and on RESOLVE recency does not help (decay-365 lands slightly above all-history —
RESOLVE sits below even recent CAISO). This is the concrete demonstration that a
recency default tuned on a one-year-ahead proxy does not transfer to an
out-of-distribution projection.

**Decay mechanics (cell-dependent).** Each (month, hour) cell reaches back to *its
own* recent same-month occurrences, and ȳ_c is a weight *ratio*, so no cell ever
empties — median effective obs/cell is ~3 at a 1-day half-life, ~30 at 1 month, ~92
at 1 year. A *hard contiguous sub-year window* is the construction that fails (as-of
December it holds no June data), which is why the decay spans day→years while the
hard `--calibration-window` is integer-years only. In the grid search, relRMSE is
degenerate-high below ~1 week (~3 obs/cell collapses ρ/s(c)), minimized at ~1 year,
and creeps back to the all-history value as H → ∞.

Full derivation and the "match the calibration period to the target period" rule:
[stochastic_model_spec.md](stochastic_model_spec.md) → "Rolling-window calibration".

## Figures

`plot_stochastic.py` → `data/figures/load_projection/stochastic/`:

- `rho_by_month_hour.png`, `shape_s_by_month_hour.png` — 12-subplot month panels of
  ρ(c) and s(c) vs hour.
- `clt_{eia930,resolve,resolve<year>}_{day,month,year}/` — CLT convergence demos for
  one representative high-load substation: Monte Carlo draws layered 1 → 50 until
  their mean converges to the model conditional mean, against historical CAISO
  (2024), a RESOLVE weather-year (2012), and an out-of-sample RESOLVE future-year
  projection (`clt-resolve-future`, scales today's envelopes by RESOLVE's own
  net-of-BTM annual growth via the F/F* knob). Each subfolder holds per-frame PNGs +
  an assembled GIF; year views plot daily means.

`rolling_origin_cv.py` → same folder:

- `rolling_origin_cv.png` — mechanism: one-year-ahead bias by origin, and bias vs
  horizon.
- `calibration_search.png` — the recency grid search: relRMSE vs decay half-life
  (1 day → 7 yr, log x) and vs hard window (1–7 yr), optimum starred. Plus
  `data/checks/stochastic/{rolling_origin_cv,calibration_search}.csv`.

## Run commands

```bash
python scripts/load_projection/approach2/estimate_stochastic.py
python scripts/load_projection/approach2/generate_stochastic.py --validate
python scripts/load_projection/approach2/generate_stochastic.py --family normal --F 0.80 --n-draws 20
python scripts/load_projection/approach2/generate_stochastic.py --target forecast.csv --z-mode bootstrap --save-output

# recency calibration
python scripts/load_projection/approach2/rolling_origin_cv.py --windows 3,5,7 --halflives 90,180,365,730
python scripts/load_projection/approach2/generate_stochastic.py --decay-halflife 365 --validate

# out-of-sample RESOLVE target
python scripts/load_projection/approach2/build_resolve_target.py
python scripts/load_projection/approach2/generate_stochastic.py \
    --target data/processed/resolve/resolve_caiso_target.csv --validate
```
