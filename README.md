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
| **Substation dataset** | 1,347 IOU substations (PGE 670 · SCE 578 · SDGE 99) with coordinates, high-side voltage, DER attributes, and month×hour load-percentile profiles | `data/processed/substations/` |
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
| [docs/genx_rescale.md](docs/genx_rescale.md) | GenX demand rescaling — the three allocation families (county-first, stochastic pool, envelope hold), the α/β splits, the four-way map axis, conservation, month-hour weighting |
| [docs/genx_runbook.md](docs/genx_runbook.md) | **Runbook** — the 25 allocations, every command in order, cluster handoff, and where each output lands |
| [docs/genx_comparison.md](docs/genx_comparison.md) | Comparing GenX runs across allocations — cost/price/power/energy divergence metrics, GenX output inventory |
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

**Rank stability** (Spearman r vs the annual `max_load` ranking, 1,337 ranked substations —
the 10 with an all-NaN or zero-width envelope are excluded):
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

### Validation

> **This is the table to present.** Detailed reading guide in
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

These three checks are the whole validation burden, and that is deliberate. **This model is
not a forecast.** It takes a load series that is already known — a historical record, or a
scenario someone else produced — and answers *where on the network that load sits*. So the
questions that matter are internal consistency ones: does each substation reproduce its own
measured envelope (ii), do the per-cell totals come out right (i), and does the sum track the
series being disaggregated (iii). Whether the model would predict *next year's* load is not
a question this project asks.

For the GenX experiment F\* is recalibrated on the demand actually being disaggregated
(F\* = 0.841 on the CATS control weeks, vs 0.7361 on the EIA-930 record) — the same principle:
calibrate against the load you are splitting up, take nothing from elsewhere except the
substations' own envelopes.

> **Legacy.** An earlier line of work treated calibration recency as a tunable
> hyperparameter — rolling-origin cross-validation, a decay-half-life grid search, and
> out-of-sample scoring against a RESOLVE-derived target. That work is preserved but is
> **not part of the method**, because it answers a predictive question this project does not
> pose. Tables, findings, and the still-functional `--calibration-window` / `--decay-halflife`
> knobs (both default off) are in
> [docs/approach2_stochastic.md → Legacy](docs/approach2_stochastic.md#legacy--calibration-recency-and-out-of-sample-behaviour).

## Nodal mapping — projecting substation loads onto an external test system

`scripts/load_projection/nodal/map_loads_to_nodes.py` assigns each projected substation's
load to the nearest **demand-eligible** node of an external system (CATS by default). This
is the capacity-expansion payoff: real substation loads land on the test model's own buses.
Rules in brief — nearest candidate node, ties within `--tie-tol-km` shared equally,
candidates filtered to buses the target model actually loads, ReEDS synthetic substations
split across their county, voltage-aware matching available via `--voltage-mode restrict`.

**CATS result:** 1,336 real substations + 4 synthetic → **1,071** distinct buses receive
load; real-substation distance median **0.13 km** (p95 19.8 km); totals conserved. Full
rules, voltage validation, coverage gaps, hybrid top-up, and the ReEDS/statewide validation
numbers: [docs/nodal_mapping.md](docs/nodal_mapping.md).

```bash
python scripts/load_projection/nodal/map_loads_to_nodes.py --system CATS \
    --apply data/processed/load_projection/projections/stochastic__eia930__normal__Fcal__native/substation_annual_mwh.csv
```

## GenX demand rescaling — spatial redistribution at fixed statewide load

The `genx/` scenario tree (28 GenX cases: 7 renewable weather years × 4 seasons, each a
168-hour representative week over 8,870 CATS buses) is treated as a **control**.
`scripts/load_projection/genx/rescale_genx_demand.py` rewrites how that demand is split
across buses using this project's disaggregation + nodal-assignment methods, while holding
the statewide total in **every hour exactly fixed** — so a downstream GenX comparison
isolates *where* load sits from *how much* there is.

Run tag `genx__{weights}__{map}__{alloc}__{level}`. Three allocation families:

- **County-first** (`--weights reedsco`, primary): each county receives its ReEDS
  *share* of the statewide total (normalized weights — ReEDS load levels cancel), then
  splits it internally — a fraction α to its *uncovered* buses as an equal split, the
  rest to its substation buses in proportion to their **max-load envelope**. County
  totals are exact by construction. `--alpha ratio` (α = u/n) loads every bus;
  `--alpha 0` loads substation buses only.
- **Stochastic pool** (`--weights stoch`): sweeps a pool of load off the network and
  deals it back in proportion to Approach 2's **per-cell** output, so county totals
  *emerge*. Always per-cell (`--level monthhour` mandatory — every Approach 2 parameter
  is estimated per (month, hour) cell; a static stochastic run collapses to a rescaled
  envelope midpoint). F\* is recalibrated on the CATS demand itself: **0.841** vs
  0.7361 on EIA-930 history.
- **Envelope hold** (`--weights env`): re-splits only the load the control places on
  buses we have substations for, by the substations' own measured envelopes — the
  minimum-intervention lower bound. No ReEDS, no projection model.

The `map` axis varies how substations attach to CATS buses: nearest node (`prox`),
CEC-lineage **identity matching** (`nameprox`: 80.6% of substations matched to the very
bus built from their CEC record; median assignment distance 0.065 km vs 0.133 km),
**transportation-LP catchments** (`catch`: every candidate bus assigned to a substation
and every bus stays loaded; totally unimodular, solved to integrality in ~1 s), and the
combination (`namecatch`).

**Status:** conservation is exact — every run-season of all 25 runs shows zero deviation
in printed hourly statewide totals, and `--weights control` reproduces the controls
byte-for-byte. Full methodology: [docs/genx_rescale.md](docs/genx_rescale.md); every
quoted number is recomputable via `scripts/load_projection/genx/doc_numbers.py`.

**Measured input-side divergence** (vs the CATS-native control, mean over the four
seasonal weeks; full 24-row table in [docs/genx_comparison.md](docs/genx_comparison.md)):

| Allocation (prox map) | Energy relocated (bus) | (county) | Buses loaded |
|---|---|---|---|
| county-first, α = u/n (static / monthhour) | 49.7% | **14.41%** | 3,762 |
| county-first, α = 0 (static / monthhour) | 58.6% | **14.41%** | 1,177 |
| stochastic Way 1 top-off, per-cell | 43.8% | 14.20% | 3,107 |
| stochastic Way 1, per-cell | 47.8% | 17.48% | 1,691 |
| stochastic Way 2 (narrow), per-cell | 20.5% | 7.74% | 2,468 |
| envelope hold (static / monthhour) | 20.3% / 20.7% | 7.7% / 8.3% | 2,467 |

County-level relocation is **pinned at 14.41% for every county-first run** — including
all map variants — while the stochastic county figure *emerges* (7.7–17.8%, rising
under catchment maps where the sweep reaches the whole network). `env hold` vs
`stoch w2` (20.3% vs 20.5%, support overlap 0.999) isolates the weighting method with
scope held fixed. Draw-to-draw spread is ≤0.06 pp at bus level. Bus-level movement runs
3–4× the county figure: the methods agree on *where in California* load is and disagree
on *which bus* carries it — exactly the disagreement a nodal model can resolve and a
zonal one cannot. Candidate buses are the 3,168 real substations plus the **610
`AddedNode` buses CATS itself loads**; the 5,089 zero-load AddedNodes are excluded.
CATS loads only 2,471 of those 3,778, so **1,307 pool buses sit at zero in the
control** — which is why full redistribution can raise the loaded-bus count while a
hold never can. A hold re-allocates **53.8%** of state energy and leaves **46.2%** on
buses no IOU substation reaches (LADWP/SMUD/IID and the loaded AddedNodes).
Metric definitions and the GenX output inventory:
[docs/genx_comparison.md](docs/genx_comparison.md).

```bash
python scripts/load_projection/genx/rescale_genx_demand.py --weights reedsco --alpha ratio --level monthhour
python scripts/load_projection/genx/rescale_genx_demand.py --weights stoch --level monthhour --stoch-gate 0.30 --stoch-topoff equal
python scripts/load_projection/genx/materialize_genx_cases.py \
    --run-tag genx__stoch__prox__w1g30top-mean__monthhour --cases p5,p6,p12,p13,p19,p20,p26,p27 --enable-outputs
python scripts/load_projection/genx/compare_genx_demand.py
```

## Machine-learning prediction cookbook (legacy)

> **Legacy — not part of the method.** This project measures the impact of
> *disaggregating a known load*, not of predicting an unknown one, so a supervised
> prediction problem sits outside the question being asked. The code and findings are
> kept (they are self-contained and the negative result is informative), but nothing in
> the disaggregation, nodal, or GenX pipelines depends on them.

A reusable, leakage-safe ML methodology (`src/ml/`) — **not** a disaggregation approach —
whose first application predicts per-cell substation load, and whose imputable configuration
motivates cold-start profile imputation for unscraped substations. The honest headline:
structural features recover a substation's *shape* but not its *magnitude*
(explanatory skill ≈0.37 → imputable ≈0.06) — which is itself the reason imputed profiles
were never wired into the projection pipeline. Full detail:
[docs/ml_cookbook.md](docs/ml_cookbook.md).

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

**Substation coverage:** 1,347 cleaned IOU substations (PGE 670 · SCE 578 · SDGE 99); **1,346 of 1,347 have a coordinate** —
only `Autobody` lacks one, and it has no load data at all (11 formerly-unplaced SCE sites were
hand-researched into `data/substationCoordinateOverrides.csv`; provenance and the one uncertain
placement are documented in [docs/nodal_mapping.md](docs/nodal_mapping.md)). The CEC 2026 DataPull cross-references 1,315 of them and confirms CATS is
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
