# Stochastic Substation Disaggregation — Model Specification (DRAFT)

Status: **implemented as Approach 2** (2026-07-17) — see README "Approach 2 —
Stochastic conditional disaggregation" for user-facing docs. Implementation:
`src/load_projection/stochastic.py`, `scripts/load_projection/approach2/estimate_stochastic.py`,
`scripts/load_projection/approach2/generate_stochastic.py`. Supporting diagnostics:
`scripts/load_projection/approach2/stochastic_diagnostics.py`.

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
| `f(c)` | The **calibrated** per-cell IOU share of CAISO: `f(c) = Σ_s μ_s / ȳ_c`, computed directly from the envelopes and CAISO history — one number per cell, range 0.58–0.88. Not a free parameter; it is measured, not chosen. |
| `F*` | The **calibrated level**: the single energy-weighted average of `f(c)` across all 288 cells (weighted by `ȳ_c` × hours-per-cell, so `F*` is exactly the IOU's share of *annual* CAISO energy). One number: `F* = 0.7361`. `F*` is what you get if you don't touch any dial — the model reproduces history exactly at `F = F*`. |
| `s(c)` | The **calibrated shape**: `s(c) = f(c) / F*`, i.e. `f(c)` renormalized to have an energy-weighted mean of 1. Captures *when* (which hour/month) the IOUs take a bigger or smaller slice of CAISO, independent of the overall level. Duck-curve-like: <1 midday, >1 evening. Computed once from history, never re-fit. |
| `F` | The **free scenario parameter** you actually choose per run: the sensitivity grid `{0.70, 0.75, 0.80, 0.85}` (or `--F cal` to use the calibrated `F*`). The model applies `f(c) = F · s(c)` — same hourly/monthly shape as history, but scaled to whatever overall IOU share you want to test. Every substation's output scales by the single factor `F/F*` (see "Optimization view" below for why `F` alone moves the level without touching `ρ`). |
| `ρ(c)` | Common-factor share for cell `c`: the fraction of each substation's cell variance that moves with the systemwide driver. `√ρ(c) = f(c) · sd_c / Σ_s σ_s`, capped at 1 — computed from the *calibrated* `f(c)`, not from whichever `F` you're sweeping, which is why `ρ` is unaffected by the `--F` choice. |
| `ε_s(t)` | Idiosyncratic standard-normal draw for substation `s`. **How it's drawn:** `numpy.random.default_rng(seed).standard_normal(...)`, one independent draw per (substation, calendar day) — held constant across all 24 hours of that day, then reused at every hour in the day (see "Decided" below: no data exists to estimate hourly autocorrelation, so daily persistence is the modeling choice). Independent across substations and across days. This is the only source of Monte Carlo randomness in native `z`-mode; bootstrap `z`-mode adds a second random ingredient (which historical weather block gets resampled). |
| `L_s(t)` | **The model's output**: substation `s`'s disaggregated load at hour `t` — one Monte Carlo draw. This is literally what `generate_stochastic.py` writes out (per-substation columns, per-hour rows). Averaging `L_s(t)` over infinitely many draws recovers the conditional mean `m_s(t)`; histogramming it over all hours in one cell recovers exactly the given `Normal(μ_s, σ_s²)` (or uniform) envelope. |

## Model

**Layer 1 — the given (unconditional) cell distribution.** Within its cell, each
substation's load is distributed `L_s ~ Normal(μ_s, σ_s²)` (or the uniform
variant). This is taken as given from the utility envelopes and never changes.

**Layer 2 — the conditional distribution at a specific hour `t`.** Knowing where
CAISO sits at hour `t` shifts each substation's distribution, and the chosen
scenario level `F` rescales the whole thing:

```
m_s(t)  =  μ_s + σ_s · √ρ(c) · z(t)                       # conditional mean, calibrated level
L_s(t)  =  (F/F*) · [ m_s(t) + σ_s · √(1−ρ(c)) · ε_s(t) ]  # Monte Carlo draw = model OUTPUT
```

`ρ(c)` is always computed from the calibrated `f(c)` (never from the swept
`F`), which is why sweeping `--F` changes every substation's level uniformly
without touching the correlation structure — see "Optimization view" for why.

Properties (each verifiable in the diagnostics):

1. **Zero MSE on deviations**: at the calibrated level (`F = F*`),
   `Σ_s m_s(t) = Σ_s μ_s + f(c)·(y(t) − ȳ_c) = f(c)·y(t)` at every hour — the
   expected total tracks CAISO exactly, both its level and its within-cell
   swings. At any other `F`, the same holds with `f(c)` replaced by `F·s(c)`.
2. **Marginal preservation**: `z(t)` has mean 0 / variance 1 within each cell,
   so mixing the conditional distributions over all hours of a cell reproduces
   the given `Normal(μ_s, σ_s²)` exactly (law of total variance:
   `σ_s² = ρσ_s² + (1−ρ)σ_s²`).
3. **Correlation interpretation**: `ρ(c)` is the implied pairwise correlation
   between any two substations in cell `c`, and the R² of each substation's
   load on the system factor.

## Optimization view

The model is equivalently the closed-form solution of a constrained
least-squares problem — the MSE objective originally posed for this work.
Before solving, these are treated as **unknowns to be found** — separate
symbols from the model's `σ_s` and `ρ(c)` even though the solution ends up
expressing them in terms of those (that's the point: solving *proves*
`b_s = σ_s√ρ(c)`, it isn't assumed going in):

- `b_s(c)` — substation `s`'s (unknown) loading on the common factor `z(t)`:
  how many MW its conditional mean moves per unit of `z`. Turns out to equal
  `σ_s(c)·λ(c)` (constraint below), and after solving, `σ_s(c)·√ρ(c)`.
- `v_s(c)` — the (unknown) scale of substation `s`'s idiosyncratic noise in
  cell `c`, in MW. After solving, equals `σ_s(c)·√(1−ρ(c))`.
- `λ(c)` — the (unknown) common standardized loading shared by every
  substation in cell `c` (the equal-correlation assumption forces `b_s/σ_s`
  to be the same value `λ(c)` for all `s`). After solving, `λ(c) = √ρ(c)` —
  i.e. `ρ(c)` is *defined* as `λ(c)²` once the optimization is solved, not
  assumed beforehand.

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

### Derivations

Assumptions used throughout: within cell `c`, `z(t)` has mean 0 and variance 1
(true by construction — `z(t) = (y(t) − ȳ_c)/sd_c`, and `ȳ_c, sd_c` are
defined as the mean/std of `y(t)` over that cell's hours, so `Σ_t z(t) = 0`
exactly). `ε_s(t) ~ N(0,1)`, independent of `z(t)`, independent across `s`.

**1. Marginal mean preserved.** By linearity of expectation:
```
E[L_s(t)] = E[μ_s + b_s(c)·z(t) + v_s(c)·ε_s(t)]
          = μ_s + b_s(c)·E[z(t)] + v_s(c)·E[ε_s(t)]
          = μ_s + b_s(c)·0       + v_s(c)·0
          = μ_s
```
Holds for any `b_s(c), v_s(c)` — both random pieces have mean zero.

**2. Marginal variance preserved → `b_s(c)² + v_s(c)² = σ_s²`.** `L_s(t)` is
the sum of `μ_s + b_s(c)·z(t)` (a deterministic function of `z(t)`) and
`v_s(c)·ε_s(t)`; since `ε_s(t) ⊥ z(t)`, `Var(X+Y) = Var(X) + Var(Y)`:
```
Var(L_s(t)) = Var(μ_s + b_s(c)·z(t))  +  Var(v_s(c)·ε_s(t))
            = b_s(c)²·Var(z(t))       +  v_s(c)²·Var(ε_s(t))
            = b_s(c)²·1               +  v_s(c)²·1
            = b_s(c)² + v_s(c)²
```
Requiring this to equal the envelope-given `σ_s²` gives the constraint. This
is the law of total variance (`Var(L) = Var(E[L|z]) + E[Var(L|z)]`): the
common-factor piece plus the idiosyncratic piece must sum to the total the
envelope demands. It is also why `ε_s(t)` must be mean-0/variance-1
*regardless of substation* — all substation-specific scale has to live in
`v_s(c)`, or this bookkeeping breaks.

**3. Equal correlation within cell → `b_s(c) = σ_s·λ(c)` implies
`Corr(L_s, L_s') = λ(c)²` for every pair.** For two substations `s ≠ s'` in
the same cell, using the same independence assumptions:
```
Cov(L_s(t), L_s'(t)) = Cov(b_s(c)·z(t), b_s'(c)·z(t))   (ε cross-terms vanish)
                     = b_s(c)·b_s'(c)·Var(z(t)) = b_s(c)·b_s'(c)

Corr(L_s, L_s') = Cov(L_s, L_s') / (σ_s·σ_s') = b_s(c)·b_s'(c) / (σ_s·σ_s')
```
Substituting `b_s(c) = σ_s·λ(c)` (and the same for `s'`), the `σ_s, σ_s'`
cancel completely: `Corr(L_s, L_s') = λ(c)²` — the same value for *any* pair,
which is why `ρ(c) := λ(c)²` is the pairwise correlation between any two
substations in the cell, not just an average.

**4. Solving for `b_s(c) = σ_s(c)·√ρ(c)`.** This one uses the objective
itself, not just the model equations. Substitute `y(t) = ȳ_c + sd_c·z(t)` and
`Σ_s m_s(t) = Σ_s μ_s + B(c)·z(t)` (writing `B(c) := Σ_s b_s(c)`) into the
objective:
```
Σ_t [f(c)·y(t) − Σ_s m_s(t)]²
  = Σ_t [ (f(c)·ȳ_c − Σμ_s) + (f(c)·sd_c − B(c))·z(t) ]²
  = Σ_t [ C0 + C1·z(t) ]²                    where C0, C1 constants (not in t)
  = n·C0² + 2·C0·C1·Σ_t z(t) + C1²·Σ_t z(t)²
  = n·C0²  +  (const > 0)·C1²                since Σ_t z(t) = 0
```
The objective separates into an independent level term (`C0`) and deviation
term (`C1`) — minimize each separately:
- Level: `f(c) = Σμ_s/ȳ_c` makes `C0 = f(c)·ȳ_c − Σμ_s = 0` exactly.
- Deviation: with `C0 = 0`, the objective is `(const)·C1²`, minimized only at
  `C1 = 0`, i.e. `Σ_s b_s(c) = f(c)·sd_c`.

This pins the *sum* `Σ_s b_s(c)`, not the individual `b_s` — one equation,
~1,347 unknowns, so the objective is already at its zero minimum for any
allocation that sums correctly. Substituting the equal-correlation ansatz
`b_s(c) = σ_s·λ(c)` resolves the indeterminacy:
```
Σ_s b_s(c) = Σ_s [σ_s·λ(c)] = λ(c)·Σ_s σ_s = f(c)·sd_c
          => λ(c) = f(c)·sd_c / Σ_s σ_s
```
and since `λ(c) = √ρ(c)` (definition 3 above), substituting back into
`b_s(c) = σ_s·λ(c)` gives `b_s(c) = σ_s(c)·√ρ(c)`.

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

## Rolling-window calibration (extension, 2026-07-27)

The estimators above pool a fixed calibration window `W` of CAISO history (the
original decision: all complete years 2015–2025). Making `W` an explicit knob
does not change the model — only which slice of history the CAISO-side moments
come from. Everything estimated from CAISO becomes window-conditional:

```
ȳ_c^W , sd_c^W          within-cell mean / sd over the hours of W
f_W(c)  = Σ_s μ_s / ȳ_c^W
F*_W    = energy-weighted mean of f_W(c)          s_W(c) = f_W(c)/F*_W
ρ_W(c)  = min(1, (f_W(c)·sd_c^W / Σ_s σ_s)²)
```

The envelope-side quantities `μ_s, σ_s, Σ_s μ_s, Σ_s σ_s` are **window-invariant**
(they come from the utility envelopes, not CAISO). Two consequences:

1. **The output level is window-invariant.** At the calibrated level, the
   expected total per cell is `E[Σ_s m_s(t) | c] = Σ_s μ_s` (because `z^W` has
   within-cell mean 0 by construction), so the expected annual total is
   `Σ_c Σ_s μ_s(c) · (hours in that cell)` **for any `W`**. Narrowing the window
   never moves the level — it re-estimates the *shape* `s_W(c)`, the
   *correlation* `ρ_W(c)`, and the *tracking reference* `f_W(c)·y(t)`.

2. **The window is the lever on the tracking bias.** For a target series `y(·)`
   scored under calibration `W`, the per-hour bias of the expected total against
   the reference is, exactly,

   ```
   bias(t) = Σ_s μ_s(c) / (f_W(c)·y(t)) − 1 = ȳ_c^W / y(t) − 1
   ```

   i.e. the ratio of the calibration window's cell mean to the target's actual
   demand. When `W` and the target come from the same period this is ≈ 0 (the
   in-sample case); the RESOLVE +5.56% and the rolling-origin CV drift are both
   this term with `ȳ_c^W` from an older/broader period than the target.

**Bias–variance tradeoff.** Observations per cell fall roughly linearly with
`|W|` (all history ≈ 319 obs/cell; a 5-year window ≈ 145), so `ρ_W(c)` and
`s_W(c)` grow noisier as the window shrinks. The rolling-origin CV
(`rolling_origin_cv.py`) shows the turn: a 5-year trailing window minimizes
one-year-ahead |bias| and is the most stable, while a 3-year window lowers the
*mean* bias further but has the highest cross-origin variance.

**Decision (2026-07-27):** expose `W` as `--calibration-window N` (last `N`
complete CAISO years); **default remains all-history**, byte-for-byte unchanged.
Use all history to describe the historical record (in-sample optimal); use a
trailing window (≈5–7 yr on the current record) when projecting a target whose
level and shape reflect recent conditions. Because the window removes only the
*temporal-drift* component of a level gap, pair it with an explicit `--F` when
the target's absolute level is known and differs structurally (e.g. RESOLVE
nets out 2024 BTM-PV, a scope difference no CAISO window can absorb). This
revises the earlier "trend-free, pooling is safe" note: pooling is safe for
*describing 2015–2025*, but leaves a recency gap against a forward projection.

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
- **CAISO estimation window: all years, 2015–2025** (~319 obs/cell) by default.
  Decision 2026-07-17. CAISO net demand is roughly trend-free over this period
  (annual means 24.5–28.0 GW), so pooling is safe *for describing the record
  itself*. Revisited 2026-07-27 (see "Rolling-window calibration"): the mild
  recency gap does bias tracking against a forward-looking target, so
  `--calibration-window N` now exposes a trailing window; the all-history
  default is unchanged. `--year-start/--year-end` remain for window sensitivity.
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
