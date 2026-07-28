# California Hourly Disaggregation

Substation-level hourly electricity load for California, built for capacity-expansion
planning research.

## Motivation

Capacity-expansion models of California typically run on **unrealistic network
topologies** — too many nodes, many of which are not real substations — driven by
**unrealistic loads** that were never resolved below the balancing-authority or regional
level. This project builds a reproducible routine to fix both halves of that problem:

1. **Identify real California substations** from authoritative public sources (the three
   major IOUs' own data, cross-referenced and validated against the CEC's statewide
   substation inventory), producing a defensible set of *real nodes* with coordinates,
   voltages, and hourly load profiles.
2. **Disaggregate** projected California / regional load (from EIA-930, IEPR, RESOLVE,
   ReEDS) down to those substations, and **map the result onto an external test system's
   nodes** (e.g. CATS), so capacity-expansion research can run on realistic topologies and
   realistic loads.

**Scope and honesty.** Future substation loads cannot be validated — no ground truth for a
2040 grid will exist until 2040. The aim is a **defensible, realistic** model, and
principled **stylization** is allowed. We validate what *can* be validated (historical
reconstruction, internal consistency, cross-source agreement) and are explicit throughout
about where a number is a proxy rather than a certified forecast. This target audience is
an academic open-source publication, so every assertion is reproducible from the scripts
here.

## What this produces

| Deliverable | What it is | Where |
|-------------|-----------|-------|
| **Substation dataset** | 1,341 IOU substations (PGE 664 · SCE 578 · SDGE 99) with coordinates, high-side voltage, DER attributes, and month×hour load-percentile profiles | `data/processed/substations/` |
| **Approach 1 — proportional weights** | Deterministic disaggregation that conserves the regional total at every hour | `data/processed/load_projection/projections/` |
| **Approach 2 — stochastic** | Monte Carlo per-substation draws with correct marginals + a CAISO-tied common factor | same |
| **Nodal mapping** | Substation loads assigned to an external test system's demand buses (CATS by default) | `data/processed/load_projection/nodal/` |

## Documentation

The README is the overview and carries the headline methodology and results. Detailed
reference lives in [`docs/`](docs/):

| Document | Contents |
|----------|----------|
| [docs/stochastic_model_spec.md](docs/stochastic_model_spec.md) | Approach 2 model derivations, optimization view, estimation, rolling-window calibration theory |
| [docs/approach1_weights.md](docs/approach1_weights.md) | Approach 1 chains, parameters, output files, run commands |
| [docs/approach2_stochastic.md](docs/approach2_stochastic.md) | Approach 2 scripts/params/outputs/figures + extended calibration discussion |
| [docs/nodal_mapping.md](docs/nodal_mapping.md) | Nodal assignment rules, voltage-aware mode, coverage gaps, hybrid top-up, ReEDS/statewide validation |
| [docs/ml_cookbook.md](docs/ml_cookbook.md) | Reusable ML methodology + cold-start substation imputation |
| [docs/data_sources.md](docs/data_sources.md) | Per-source detail, substation coverage, CEC reference & audit, SMUD/CAISO POI |
| [docs/data_pipeline.md](docs/data_pipeline.md) | Scrape → process → validate commands, column dictionaries, timezone/DST conventions, data-quality notes |
| [docs/statewide_forecast_sources.md](docs/statewide_forecast_sources.md) | RESOLVE, ReEDS, IEPR; BTM treatment by source; RESOLVE-vs-IEPR framework; EIA CA8; peak-hour alignment |

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
```

An `EIA_API_KEY` (free at <https://www.eia.gov/opendata/>, in a repo-root `.env`) is
needed only for the direct EIA API scraper; the recommended PUDL path needs no key.

## Pipeline at a glance

```
scrape raw sources  →  process into unified substation + forecast tables  →
rank substations  →  disaggregate (Approach 1 or 2)  →  map onto external nodes
```

Full commands and column dictionaries: [docs/data_pipeline.md](docs/data_pipeline.md).
Data-source descriptions: [docs/data_sources.md](docs/data_sources.md).

---

# Load Projection Methodology

Disaggregates projected California statewide/regional load into substation-level hourly
forecasts. Two approaches, plus a shared substation ranking and a downstream nodal mapping.

## Substation rankings (shared prerequisite)

`scripts/load_projection/shared/rank_substations.py` ranks all substations at four temporal
levels (annual, monthly, hourly, month-hour) and three percentiles (`min_load` ~10th,
`max_load` ~90th, `avg_load`). Run once before any disaggregation.

**Rank stability** (Spearman r vs the annual `max_load` ranking, 1,341 substations):
max-load orderings are very stable (monthly r 0.987–0.994, hourly 0.991–0.997, month-hour
0.934–0.985); min-load varies more at off-peak month-hours (hourly r ≥ 0.832, month-hour
≥ 0.651). Outputs: `data/processed/load_projection/rankings/` + a rank-correlation heatmap.

```bash
python scripts/load_projection/shared/rank_substations.py
```

## Approach 1 — Proportional participation weights

Each substation gets a **participation weight** = its share of its region's total load at a
chosen percentile and temporal resolution; projected regional load × that weight gives the
substation forecast. It **conserves the regional total at every hour** but does not model
substation-specific growth or changing shapes.

```
weight[s, m, h] = max(load_col[s, m, h], 0) / Σ_{j∈Region} max(load_col[j, m, h], 0)
substation_load[s, t] = regional_load[region(s), t] × weight[s, month(t), hour(t)]
```

Two chains: **ReEDS** (p-region → county → substation, two-stage) and **IOU** (IEPR/RESOLVE
→ substation, single-stage). Chain weights sum to 1.0 per region at every cell.
Full parameters, output files, and validation: [docs/approach1_weights.md](docs/approach1_weights.md).

```bash
python scripts/load_projection/approach1/disaggregate_reeds.py
python scripts/load_projection/approach1/disaggregate_iou.py --source iepr
python scripts/load_projection/approach1/disaggregate_iou.py --source resolve
```

## Approach 2 — Stochastic conditional disaggregation

Disaggregates a CAISO-total hourly series into per-substation **Monte Carlo draws** rather
than deterministic weights. Each substation's load within a (month, hour_pst) cell is a
random variable whose distribution (normal or uniform) is exactly identified by the utility
10th/90th-percentile envelopes; a per-cell common factor tied to CAISO's standardized
within-cell deviation `z(t)` makes substations move together, so the simulated total tracks
`F · s(c) · y(t)` while every substation retains its full envelope variability. It does not
model substation-specific factor loadings (uniform correlation within a cell — substation
time series are confidential) and does not truncate negative loads (real BTM reverse flows).

```
z(t)    = (y(t) − ȳ_c) / sd_c                          # CAISO standardized within its cell
m_s(t)  = μ_s + σ_s · √ρ(c) · z(t)                      # conditional mean
L_s(t)  = (F/F*) · [ m_s(t) + σ_s · √(1−ρ(c)) · ε_s ]   # normal-family Monte Carlo draw
uniform family: same W = √ρ·z + √(1−ρ)·ε through a Gaussian copula

s(c) = implied_f(c) / F*     # empirical 288-value IOU-share shape (mean 1); duck-curve-like
ρ(c) = min(1, (implied_f(c)·sd_c / Σσ_s)²)   # F-invariant common-factor share
ε_s  = one draw per substation-day (persistent within day)
```

Estimated from EIA-930 CISO 2015–2025: **F\* = 0.7361** (annual IOU energy share of CAISO),
s(c) ∈ [0.78, 1.20], ρ(c) median 0.231. Full model theory:
[docs/stochastic_model_spec.md](docs/stochastic_model_spec.md); scripts/params/outputs/figures
and the extended calibration discussion: [docs/approach2_stochastic.md](docs/approach2_stochastic.md).

```bash
python scripts/load_projection/approach2/estimate_stochastic.py
python scripts/load_projection/approach2/generate_stochastic.py --validate
```

### Validation and calibration

> **These are the tables to present.** The first two are the core validation; the last group
> is the calibration-recency search. Detailed reading guides are in
> [docs/approach2_stochastic.md](docs/approach2_stochastic.md).

**Validation — EIA-930 2015–2025 (native z, F = cal, 5 draws).** Three checks: (i) per-cell
total q10/q90 error, (ii) per-substation envelope recovery, (iii) hourly tracking.

| Check | normal | uniform |
|-------|--------|---------|
| (i) per-cell total q10/q90 error | median 0.16% / max 0.75% | median 0.22% / max 1.26% |
| (ii) envelope recovery (width-normalized, 375,906 sub-cells) | median 1.1%, p95 3.3% | median 2.4%, p95 4.0% |
| (iii) hourly tracking relRMSE / bias | 0.40% / +0.002% | 0.78% / +0.013% |

Mean simulated total **164.3 TWh/yr = F\* × CAISO mean (~223 TWh/yr)**, annualized over the
ten complete calendar years (2016–2025; 2015 is a partial summer-skewed stub).

**Out-of-sample — RESOLVE weather-year target (not a validation).** The same checks with a
RESOLVE-derived CAISO target (PGE+SCE+SDGE net, 23 weather years) swapped in. ρ(c)/s(c)/F\*
never touch RESOLVE, and RESOLVE is not ground truth for the substations, so this only shows
how the fitted model behaves outside its training data.

| Check | normal | uniform |
|-------|--------|---------|
| (i) per-cell total q10 / q90 error | median 3.68% / 4.85% | 3.80% / 4.66% |
| (ii) envelope recovery q10 / q90 (width-normalized) | median 0.81% / 0.76% | 2.42% / 2.47% |
| (iii) hourly tracking relRMSE / bias | 9.43% / **+5.56%** | 9.52% / +5.57% |

The mean total is unchanged (~164 TWh — the level is set by the fixed envelopes Σμ_s, not
the target). Check (ii), against the real utility q10/q90, barely moves; checks (i)/(iii)
degrade because they score against `F·s(c)·y` built from the target's own cells. The bias
has a closed form, `ȳ_train(c)/y_target(t) − 1`: CAISO's 2015–2025 mean sits ~4.7% above
RESOLVE's 2024-BTM-net cells. It is **exactly F-invariant** — `--F` sets the output level,
not this self-referential metric.

**Calibration-recency search** (`rolling_origin_cv.py`; figure `calibration_search.png`).
The one tunable hyperparameter is *how much recent CAISO years count more* when calibrating
s(c)/ρ(c). A rolling-origin CV — a genuine chronological train/test split within EIA-930 —
selects it by **held-out one-year-ahead error**. The two tables below are the two panels of
`calibration_search.png`; the starred optima in the plot are the bolded cells here.

*Panel (a) — soft decay kernel* (all half-lives share the same 9 origins → absolute
one-year-ahead relRMSE):

| Decay half-life | 1 d | 1 wk | 1 mo | 3 mo | **1 yr** | 2 yr | 3 yr | 5 yr | 7 yr | all-history |
|-----------------|-----|------|------|------|----------|------|------|------|------|-------------|
| relRMSE | 8.97% | 6.76% | 6.37% | 6.25% | **5.97%** | 6.12% | 6.23% | 6.35% | 6.41% | 6.57% |

*Panel (b) — hard look-back window* (a window is only definable back N years, so each is
scored against all-history on *its own* origins — a matched Δ; below 0 = beats all-history):

| Hard window (yr) | 1 | **2** | 3 | 4 | 5 | 6 | 7 |
|------------------|---|-------|---|---|---|---|---|
| relRMSE vs matched all-history (pp) | −0.18 | **−0.58** | −0.58 | −0.37 | −0.26 | −0.09 | +0.00 |
| n origins | 9 | 8 | 7 | 6 | 5 | 4 | 3 |

(2 yr and 3 yr are effectively tied at −0.58 pp; the plot stars **2 yr** as the marginal
minimum.) The optimum is a **~1-year decay half-life** (relRMSE 5.97% vs all-history's
6.57%) or, equivalently, a **~2-year hard window** (−0.58 pp) — the soft decay is preferred
because it keeps all the data (smoother s(c)/ρ(c)) and isn't data-limited the way the window
is (its n shrinks with N). Too short a decay (1 day, ~3 obs/cell) is degenerate.

**But those one-year-ahead optima are *not* the best calibration for either real job.** This
is the key, and it is why the plot and the tables below look different — they answer
different questions. Re-scoring a *fixed* calibration through the full `--validate` checks on
each **complete** target (both tables use the same five calibrations, so the two
one-year-ahead CV optima — trailing-2 and decay-365 — appear in both even where they lose;
bold = best on that target):

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

**The rule is: match the calibration period to the target period** — which is exactly why no
single "optimal" row exists across the plot and both tables. The one-year-ahead search picks
recency (≈1-yr decay / ≈2-yr window) because its target is *next year* — and note both of
those optima (trailing-2, decay-365) are *worse* than all-history on both full targets.
All-history wins for *describing the whole historical record* (it is in-sample there). For
RESOLVE, only trailing-7 beats all-history — and only by luck (2019–25 is the lowest-demand
window, nearest RESOLVE's solar-heavy level); it still leaves +4.12%, a structural level gap
that belongs to `--F`, not to recency. The recency knobs are `generate_stochastic.py
--decay-halflife H` (soft) and `--calibration-window N` (hard); both default off.

```bash
python scripts/load_projection/approach2/rolling_origin_cv.py
python scripts/load_projection/approach2/build_resolve_target.py
python scripts/load_projection/approach2/generate_stochastic.py \
    --target data/processed/resolve/resolve_caiso_target.csv --validate
```

## Nodal mapping — projecting substation loads onto an external test system

`scripts/load_projection/nodal/map_loads_to_nodes.py` assigns each projected substation's
load to the nearest **demand-eligible** node of an external system (CATS by default). This
is the capacity-expansion payoff: real substation loads land on the test model's own buses.
Rules in brief — nearest candidate node, ties within `--tie-tol-km` shared equally,
candidates filtered to buses the target model actually loads, ReEDS synthetic substations
split across their county, voltage-aware matching available via `--voltage-mode restrict`.

**CATS result:** 1,325 real substations + 4 synthetic → **1,070** distinct buses receive
load; real-substation distance median **0.13 km** (p95 19.8 km); totals conserved. Full
rules, voltage validation, coverage gaps, hybrid top-up, and the ReEDS/statewide validation
numbers: [docs/nodal_mapping.md](docs/nodal_mapping.md).

```bash
python scripts/load_projection/nodal/map_loads_to_nodes.py --system CATS \
    --apply data/processed/load_projection/projections/stochastic__eia930__normal__Fcal__native/substation_annual_mwh.csv
```

## Machine-learning prediction cookbook

A reusable, leakage-safe ML methodology (`src/ml/`) — **not** a disaggregation approach —
whose first application predicts per-cell substation load, and whose imputable configuration
motivates cold-start profile imputation for unscraped substations. The honest headline:
structural features recover a substation's *shape* but not its *magnitude*
(explanatory skill ≈0.37 → imputable ≈0.06). Full detail: [docs/ml_cookbook.md](docs/ml_cookbook.md).

---

# Data Sources

Compact summary; full per-source detail in [docs/data_sources.md](docs/data_sources.md)
(historical/substation) and [docs/statewide_forecast_sources.md](docs/statewide_forecast_sources.md)
(forecasts).

| Source | Scope | Load definition | Horizon | Role |
|--------|-------|-----------------|---------|------|
| **EIA-930** (PUDL) | 8 CA BAs | Net-of-BTM (metered) | 2015– | Historical ground truth |
| **Utility IOUs** (PGE/SCE/SDGE) | Distribution substations | Net-of-BTM at the substation meter | Historical monthly | Substation profiles + attributes |
| **CEC Substation DataPull 2026** | Statewide | n/a (location inventory) | — | Authoritative substation reference (basin/CATS successor) |
| **IEPR** | PGE+SCE+SDGE+VEA | `BASELINE_NET_LOAD` (net) / `BASELINE_CONSUMPTION` (gross) | 2024–2050 | Policy forecast |
| **RESOLVE** | PGE+SCE+SDGE+IID+LDWP+NCNC | Gross (BTM on supply side) | 2024–2045 | IRP optimization target |
| **ReEDS** | CA counties → p8–p11 | Net (IRA_low projected; actual historic) | 2020–2050 / 2016–2023 | Long-run capacity planning |

**Substation profiles are net-of-BTM at the meter**, so pair them with net statewide
targets (EIA-930, IEPR `BASELINE_NET_LOAD`, RESOLVE derived net), not gross. The evidence
and the full source-by-source gross/net breakdown are in
[docs/statewide_forecast_sources.md](docs/statewide_forecast_sources.md) → "BTM Solar
Treatment by Source".

**Substation coverage:** 1,341 cleaned IOU substations; 1,335 have a coordinate (only 12
SCE lack any). The CEC 2026 DataPull cross-references 1,315 of them and confirms CATS is
fully contained in the CEC lineage (all 3,171 CATS buses match within 2 km), so CEC can
replace basin as CATS's coordinate source — the basis for realistic nodes. Details, the
coverage table, and the reverse-gap audit (CEC lists ~2,000+ load-eligible IOU substations
we have not scraped) are in [docs/data_sources.md](docs/data_sources.md).

---

# Repository Structure

```
california-hourly-disaggregation/
├── data/
│   ├── raw/                     # Downloaded source data (gitignored)
│   └── processed/               # Unified substation, forecast, and projection outputs
├── docs/                        # Detailed methodology & reference (see Documentation above)
├── notebooks/                   # EIA interchange consistency checks
├── scripts/
│   ├── data/                    # Ingestion, processing, validation — organised by source
│   └── load_projection/         # shared / approach1 / approach2 / nodal / checks / ml
├── src/
│   ├── data/                    # Scraper & processing library modules
│   └── load_projection/         # Projection library (weights.py, stochastic.py) + src/ml/
├── requirements.txt
└── README.md
```

Conventions in one line: all processed outputs use **fixed PST (UTC−8), hour-beginning,
hours 0–23**. The full timezone/DST table, per-file conversions, and data-quality notes are
in [docs/data_pipeline.md](docs/data_pipeline.md).
