# Approach 2 — Stochastic conditional disaggregation (operational reference)

The README carries the model summary, the equations, and the headline
validation/calibration **tables**. This document holds the operational reference
(scripts, parameters, output files, figures) and the extended calibration
discussion. Theory and derivations live in
[stochastic_model_spec.md](stochastic_model_spec.md).

## Where this is implemented

Model logic lives in `src/load_projection/stochastic.py`; the driver is
`scripts/load_projection/approach2/generate_stochastic.py`.

| Concept | Function | File |
|---|---|---|
| Envelope → per-cell μ, σ, uniform bounds; hygiene flags | `load_envelope_cells()` | `src/load_projection/stochastic.py` |
| (month, hour) → cell index 0–287 | `cell_index()` | same |
| Per-cell system table: `implied_f`, **F\***, `shape_s`, `rho` | `build_system_cells()` | same |
| One Monte Carlo draw of all substations | `generate()` | same |
| z(t): native standardisation / block bootstrap | `standardize_z()` / `bootstrap_z()` | same |
| *(legacy)* hard window / decay kernel | `trailing_window()` / `decay_weights()` | same |
| Per-draw generation, annual + **per-cell** tables | `trajectory_pass(..., save_cells)` | `generate_stochastic.py` |
| Which series F\* is calibrated on | `--calibrate-on` / `--calib-target` branch in `main()` | same |
| Validation checks (i)/(iii) and (ii) | `validate_totals()` / `marginal_pass()` | same |
| Complete-year annualisation | `annualized_mean_twh()` | same |
| CATS statewide target (672 h) | `build_cats_target.py` | `genx/` |

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

## LEGACY — calibration recency and out-of-sample behaviour

> **Not part of the method.** Everything in this section treats the stochastic
> model as if it had to *predict* load, and asks how much recent history should
> count when calibrating s(c)/ρ(c). Approach 2 does not predict: it takes an
> already-known load series and decides where on the network that load sits, so
> tuning recency by held-out error optimizes a question the project never asks
> and cannot improve a disaggregation of a series that is given.
>
> Retained because the findings are real and were expensive to obtain, because
> the closed-form bias result below explains *why* the `--validate` numbers move
> when the target changes, and because the knobs still work
> (`generate_stochastic.py --calibration-window N`, `--decay-halflife H`; both
> default off, so the shipped behaviour is the unweighted all-history
> calibration). Supporting scripts: `rolling_origin_cv.py` and
> `build_resolve_target.py`, both marked legacy in their own docstrings.
>
> **What replaced it:** calibrate F\*, s(c) and ρ(c) on the very series being
> disaggregated (`--calibrate-on target`), and take nothing from any other
> dataset except the substations' own envelopes.

### Out-of-sample — RESOLVE weather-year target (never a validation)

The three `--validate` checks with a RESOLVE-derived CAISO target (PGE+SCE+SDGE
net, 23 weather years) swapped in. ρ(c)/s(c)/F\* never touch RESOLVE, and RESOLVE
is not ground truth for the substations, so this only ever showed how the fitted
model behaves outside its training data.

| Check | normal | uniform |
|-------|--------|---------|
| (i) per-cell total q10 / q90 error | median 3.68% / 4.85% | 3.80% / 4.66% |
| (ii) envelope recovery q10 / q90 (width-normalized) | median 0.81% / 0.76% | 2.42% / 2.47% |
| (iii) hourly tracking relRMSE / bias | 9.43% / **+5.56%** | 9.52% / +5.57% |

The mean total is unchanged (~164 TWh — the level is set by the fixed envelopes
Σμ_s, not the target). Check (ii), against the real utility q10/q90, barely
moves; checks (i)/(iii) degrade because they score against `F·s(c)·y` built from
the target's own cells.

### Calibration-recency search

`rolling_origin_cv.py`; figure `calibration_search.png`. A rolling-origin CV — a
chronological train/test split within EIA-930 — selecting the recency weight by
**held-out one-year-ahead error**. The two tables are the two panels of the
figure; starred optima in the plot are bolded here.

*Panel (a) — soft decay kernel* (all half-lives share the same 9 origins →
absolute one-year-ahead relRMSE):

| Decay half-life | 1 d | 1 wk | 1 mo | 3 mo | **1 yr** | 2 yr | 3 yr | 5 yr | 7 yr | all-history |
|-----------------|-----|------|------|------|----------|------|------|------|------|-------------|
| relRMSE | 8.97% | 6.76% | 6.37% | 6.25% | **5.97%** | 6.12% | 6.23% | 6.35% | 6.41% | 6.57% |

*Panel (b) — hard look-back window* (a window is only definable back N years, so
each is scored against all-history on *its own* origins — a matched Δ; below 0 =
beats all-history):

| Hard window (yr) | 1 | **2** | 3 | 4 | 5 | 6 | 7 |
|------------------|---|-------|---|---|---|---|---|
| relRMSE vs matched all-history (pp) | −0.18 | **−0.58** | −0.58 | −0.37 | −0.26 | −0.09 | +0.00 |
| n origins | 9 | 8 | 7 | 6 | 5 | 4 | 3 |

(2 yr and 3 yr are effectively tied at −0.58 pp; the plot stars **2 yr** as the
marginal minimum.) The optimum was a **~1-year decay half-life** (5.97% vs
all-history 6.57%) or equivalently a **~2-year hard window**. Too short a decay
(1 day, ~3 obs/cell) is degenerate.

**Those one-year-ahead optima are not the best calibration for either real
target** — which is the finding that ultimately retired the whole line of work.
Re-scoring a *fixed* calibration through the full `--validate` checks on each
complete target (same five calibrations in both tables; bold = best on that
target):

*Historical target — EIA-930, scored on the whole 2015–2025 record:*

| Calibration | F\* | (i) total err | (ii) recovery med/p95 | (iii) relRMSE / bias |
|-------------|-----|---------------|-----------------------|----------------------|
| **all-history** (best here) | 0.7361 | **0.15%** | 1.1% / 3.3% | **0.40% / +0.00%** |
| trailing-2 yr *(CV window opt)* | 0.7340 | 3.17% | 1.14% / 3.35% | 4.49% / +0.61% |
| trailing-5 yr | 0.7407 | 1.84% | 1.13% / 3.29% | 2.68% / −0.46% |
| trailing-7 yr | 0.7462 | 1.25% | 1.13% / 3.28% | 2.09% / −1.30% |
| decay-365 d *(CV decay opt)* | 0.7370 | 2.61% | 1.12% / 3.25% | 3.68% / +0.18% |

*Projected target — RESOLVE (2024-BTM-net weather years):*

| Calibration | F\* | (i) total err | (ii) recovery med/p95 | (iii) relRMSE / bias |
|-------------|-----|---------------|-----------------------|----------------------|
| all-history | 0.7361 | 4.15% | 0.81% / 2.44% | 9.43% / +5.56% |
| trailing-2 yr *(CV window opt)* | 0.7340 | 7.23% | 0.79% / 2.42% | 11.87% / +6.26% |
| trailing-5 yr | 0.7407 | 5.51% | 0.80% / 2.39% | 9.11% / +5.04% |
| **trailing-7 yr** (best here) | 0.7462 | **3.89%** | 0.81% / 2.47% | **8.08% / +4.12%** |
| decay-365 d *(CV decay opt)* | 0.7370 | 6.86% | 0.79% / 2.36% | 11.01% / +5.81% |

The rule that emerged — **match the calibration period to the target period** —
is the same instinct that the current method implements directly and more
simply, by calibrating on the target series itself. Both one-year-ahead optima
(trailing-2, decay-365) are *worse* than all-history on both full targets.

Legacy run commands:

```bash
python scripts/load_projection/approach2/rolling_origin_cv.py
python scripts/load_projection/approach2/build_resolve_target.py
python scripts/load_projection/approach2/generate_stochastic.py \
    --target data/processed/resolve/resolve_caiso_target.csv --validate
```

### Mechanics behind the tables

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

**The calibration was a genuine train/test split; the disaggregation is not one
at all.** The rolling-origin CV (`rolling_origin_cv.py`) is a textbook
chronological split within EIA-930: calibrate on years ≤ T, score the held-out
year T+1, slide T forward. It honestly measures *near-term* generalization. It
does not certify anything about a projection in a different regime (2040 BTM,
different scope) with no ground truth — and, more fundamentally, generalization
is not the property a disaggregation of a known series needs.

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

Full derivation: [stochastic_model_spec.md](stochastic_model_spec.md) →
"Rolling-window calibration" (also legacy).

*(End of legacy section.)*

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
