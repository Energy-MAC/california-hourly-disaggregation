# Stochastic Substation Disaggregation — Model Specification (DRAFT)

Status: **implemented as Approach 2** (2026-07-17) — see README "Approach 2 —
Stochastic conditional disaggregation" for user-facing docs. Implementation:
`src/load_projection/stochastic.py`, `scripts/load_projection/estimate_stochastic.py`,
`scripts/load_projection/generate_stochastic.py`. Supporting diagnostics:
`scripts/load_projection/stochastic_diagnostics.py`.

## Notation

| Symbol | Meaning |
|--------|---------|
| **cell** | One (month, hour_pst) pair — e.g. (July, 15:00). 12 × 24 = 288 cells. Every quantity below is defined per cell unless it carries a `(t)`. |
| `s` | A substation (1,347 after cell-dedup; PGE + SCE + SDGE). |
| `t` | A specific historical or forecast hour. Every `t` belongs to exactly one cell via (month(t), hour_pst(t)). |
| `q10_s, q90_s` | The utility envelope values (`min_load`, `max_load`) for substation `s` in a cell: 10th/90th percentile of net-of-BTM load at the substation meter. |
| `μ_s, σ_s` | Normal marginal parameters implied by the two quantiles (closed form, no estimation): `μ = (q10+q90)/2`, `σ = (q90−q10)/(2·1.28155)`. Uniform variant: `width = (q90−q10)/0.8`. |
| `y(t)` | CAISO hourly net demand (EIA-930 CISO, PST hour-beginning). |
| `ȳ_c, sd_c` | Mean and std of `y(t)` over all historical hours falling in cell `c` (2015–2025 window, ~319 obs per cell). |
| `z(t)` | CAISO's standardized within-cell deviation: `z(t) = (y(t) − ȳ_c) / sd_c`. Mean 0, variance 1 within each cell by construction. |
| `f` | IOU share of CAISO: either constant (sensitivity grid 0.70–0.85) or per-cell `f(c) = Σ_s μ_s / ȳ_c` (calibrated; range 0.58–0.88, hourly pattern reflects genuinely time-varying IOU share of CAISO). |
| `ρ(c)` | Common-factor share for cell `c`: the fraction of each substation's cell variance that moves with the systemwide driver. `√ρ(c) = f · sd_c / Σ_s σ_s`, capped at 1. |
| `ε_s(t)` | Idiosyncratic standard-normal draw, independent across substations and hours. This is the Monte Carlo randomness. |

## Model

**Layer 1 — the given (unconditional) cell distribution.** Within its cell, each
substation's load is distributed `L_s ~ Normal(μ_s, σ_s²)` (or the uniform
variant). This is taken as given from the utility envelopes and never changes.

**Layer 2 — the conditional distribution at a specific hour `t`.** Knowing where
CAISO sits at hour `t` shifts each substation's distribution:

```
m_s(t)  =  μ_s + σ_s · √ρ(c) · z(t)          # time-varying conditional mean
L_s(t)  =  m_s(t) + σ_s · √(1−ρ(c)) · ε_s(t) # Monte Carlo draw
```

Properties (each verifiable in the diagnostics):

1. **Zero MSE on deviations**: `Σ_s m_s(t) = Σ_s μ_s + f·(y(t) − ȳ_c)` — the
   expected total tracks CAISO's within-cell swings exactly. With per-cell
   `f(c)`, additionally `Σ_s μ_s = f(c)·ȳ_c`, so the expected total equals
   `f(c)·y(t)` at every hour.
2. **Marginal preservation**: `z(t)` has mean 0 / variance 1 within each cell,
   so mixing the conditional distributions over all hours of a cell reproduces
   the given `Normal(μ_s, σ_s²)` exactly (law of total variance:
   `σ_s² = ρσ_s² + (1−ρ)σ_s²`).
3. **Correlation interpretation**: `ρ(c)` is the implied pairwise correlation
   between any two substations in cell `c`, and the R² of each substation's
   load on the system factor.

## Optimization view

The model is equivalently the closed-form solution of a constrained
least-squares problem — the MSE objective originally posed for this work:

```
min_{b, v, f}   Σ_t [ f(c(t))·y(t) − Σ_s m_s(t) ]²

s.t.  m_s(t) = μ_s + b_s(c)·z(t)              (linear conditional mean — all that
                                               two quantiles per cell can identify)
      L_s(t) = m_s(t) + v_s(c)·ε_s(t)
      b_s(c)² + v_s(c)² = σ_s²                (marginal variance preserved)
      E[m_s(t) | c] = μ_s                     (marginal mean preserved)
      b_s(c) = σ_s·λ(c)                       (equal correlation within cell)
      0 ≤ λ(c) ≤ 1                            (correlation parameter space)
```

First-order conditions solve analytically — no numerical optimizer runs
(analogous to OLS solved via normal equations):

- level term  → `f(c) = Σμ_s/ȳ_c` (the calibrated implied f; the level is then
  deliberately frozen as `F·s(c)` so F remains a scenario parameter — s(c)
  carries the optimal shape and F = F* recovers the exact optimum);
- deviation term → zero MSE requires `Σ_s b_s = f(c)·sd_c`; the equal-correlation
  constraint resolves the allocation: `λ(c) = √ρ(c) = f(c)·sd_c/Σσ_s`;
- marginal variance → `v_s = σ_s·√(1−ρ)` with no remaining freedom.

The achieved objective is exactly zero in expectation wherever the cap does
not bind (it never binds on our data) — verified empirically as check (iii)'s
≈0 bias. Estimation therefore = solving the minimization in closed form;
Monte Carlo = sampling from the resulting joint distribution, not solving
anything. A numerical optimizer would only be needed if the closed-form
assumptions were relaxed (per-substation loadings β_s, richer copulas, or
cross-cell smoothness penalties).

## Estimation

Only two objects are estimated, both by moment matching per cell:

| Object | Estimator | Data |
|--------|-----------|------|
| `f(c)` (calibrated case) | `Σ_s μ_s / ȳ_c` | envelopes + EIA-930 |
| `ρ(c)` | `(f · sd_c / Σ_s σ_s)²` | envelopes + EIA-930 |

Feasibility requires `ρ(c) ≤ 1`, i.e. even perfectly synchronized substations
can swing as much as `f·CAISO`. Verified on the 2015–2025 window: zero
infeasible cells at any f ∈ [0.70, 0.85] (median ρ = 0.20 / 0.25 / 0.30 at
f = 0.70 / 0.775 / 0.85; range 0.11–0.58 across cells).

**Why the estimator carries a `min(1, ·)` cap:** ρ is a variance share (and a
pairwise correlation), so its parameter space is [0, 1] by definition. The
moment-ratio estimator `(f·sd_c/Σσ_s)²`, however, is just a ratio of two
quantities measured from different datasets (CAISO history vs utility
envelopes) and is not intrinsically bounded — nothing prevents noisy or
inconsistent inputs from producing a value above 1. The cap is therefore part
of the estimator's definition: it projects the moment estimate onto the
feasible parameter space. A binding cap would mean the observed CAISO swings
in that cell exceed what the envelopes can produce even under perfect
synchronization — a data-inconsistency signal, which is why the estimator
reports the number of capped cells (zero on our data; largest ρ̂ = 0.48, so
the cap is currently dormant and no estimate is distorted by it).

## Decided

- Substation envelopes are **net-of-BTM** (evidence in CLAUDE.md consistency
  note); EIA-930 net CAISO is the consistent target.
- **No truncation** of negative loads (real reverse flows).
- Inverted cells (172): **swap** min/max. Zero-width cells (10,110, all SCE,
  ~0 MW dead substations): **deterministic at reported value**, flagged
  `zero_width`; no imputation (see hygiene_report.md).
- `ρ` is **uniform within a cell** (varies across cells). Per-substation factor
  loadings (β_s) are unidentifiable without substation time series, which are
  confidential and will never be available; customer-mix metrics (residential
  vs non-residential shares) could in principle inform β_s but a realistic
  mapping is out of reach. Decision (2026-07-17): **constant β within cell**,
  stated as a model assumption.
- **CAISO estimation window: all years, 2015–2025** (~319 obs/cell). Decision
  2026-07-17. CAISO net demand is trend-free over this period (annual means
  24.5–28.0 GW), so pooling is safe. `--year-start/--year-end` remain available
  for window sensitivity.
- Marginal families to compare: **normal** and **uniform** (same `z(t)` and
  `ρ`, different marginal shape).

- **f treatment: hybrid shape × level** (decision 2026-07-17). Factor the
  calibrated per-cell factor as `f(c) = F · s(c)`:
  - `s(c)` is an **empirical 288-value shape** — one number per (month, hour)
    cell, computed **once** from history as `f(c) / ⟨f⟩`, where `⟨f⟩` is the
    energy-weighted mean of `f(c)` (weighted by `ȳ_c ×` hours per cell), so
    that `F` is interpretable as the annual-energy IOU share of CAISO. The
    shape is not Gaussian or parametric — by hour it is duck-curve-like
    (≈0.87 at 11:00, ≈1.09 at 18:00 relative to the mean). It is independent
    of `F` by construction and is **not** re-fit per grid point.
  - `F` is swept over **{0.70, 0.75, 0.80, 0.85}** (grid can be refined later).
- **Uniform-variant conditioning: Gaussian copula** (decision 2026-07-17).
  Draw `W_s = √ρ·z(t) + √(1−ρ)·ε_s`, set `L_s = a + (b−a)·Φ(W_s)`. Marginals
  stay exactly uniform; tracking error of the expected total vs `f·CAISO` to
  be quantified in validation. The uniform family is a shape-comparison case,
  not theory-driven.
- **`z(t)` for forecast hours** (decision 2026-07-17): standardize the
  forecast's own trajectory within its cells for **RESOLVE** (23 native
  weather years); **block-bootstrap** historical 2015–2025 `z(t)` in 7-day
  blocks overlaid on forecast cell means for **IEPR**. Both used in
  validation. i.i.d. hourly `z` is rejected (destroys the temporal
  autocorrelation that peak/duration statistics depend on).
- **Bootstrap z is redrawn independently for every Monte Carlo draw**
  (decision 2026-07-20): a forecast ensemble should vary the weather year
  across draws, so each draw carries its own bootstrapped trajectory plus its
  own idiosyncratic noise ("100 plausible 2035s"). Native mode shares the one
  observed trajectory across draws — there the uncertainty is allocation-only
  ("how was the observed CAISO load split across substations").
- **Idiosyncratic noise `ε_s`: one draw per substation-day** (decision
  2026-07-17), held constant across the 24 hours of each day. No substation
  time series exists to estimate hourly autocorrelation, so daily persistence
  is the simplest defensible structure; i.i.d.-hourly kept as a cheap
  sensitivity. Downstream use is full 8760 hourly profiles fed to capacity
  expansion models, so substation-level peak persistence matters.

## Status

Implemented and validated 2026-07-17. Estimated parameters (EIA-930 2015–2025):
F* = 0.7361, s(c) ∈ [0.78, 1.20], ρ(c) median 0.231 range 0.10–0.48 (no capped
cells). Validation (native z, F = cal, 5 draws): (i) per-cell total q10/q90
errors ≤ 1.3%; (ii) envelope recovery median ~1.1% (normal) / ~2.4% (uniform)
of envelope width, p95 ≤ 4%; (iii) tracking relRMSE 0.40% (normal, pure MC
noise) / 0.78% (uniform, includes copula nonlinearity), bias ≈ 0. Residual
marginal error stems from empirical z(t) being skewed rather than exactly
standard normal.

One implementation detail beyond the original spec: cells with NaN quantiles
are flagged `missing` and excluded (1,848 cells: 6 SCE substations with no
data at all — Autobody, Kempster, Line Creek, Modoc, Paularino, Topanga —
plus half-missing months at PGE BROWNS VALLEY / SOQUEL and 72 absent rows).
A distribution cannot be fit from one quantile, so half-missing cells are
treated as missing, not imputed.

The F sensitivity enters as a pure output scaling: since the marginals are
given, `f(c) = F·s(c)` is achieved by scaling all substation loads by `F/F*`;
ρ is invariant to F under the shape normalization (√ρ = F*·s(c)·sd_c/Σσ after
the σ's scale too, so the F's cancel).
