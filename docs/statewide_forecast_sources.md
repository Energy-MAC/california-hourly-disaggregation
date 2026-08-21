# Statewide load forecast sources (RESOLVE, ReEDS, IEPR)

Full detail on the statewide demand sources the substation work is disaggregated *from*
and validated *against*, how each handles behind-the-meter (BTM) solar, why RESOLVE and
IEPR differ, and the peak-hour reconciliation. The README carries the compact source
table and the essential net-vs-gross rule.

| Source | Scope | Load definition | Horizon | Used for |
|--------|-------|-----------------|---------|----------|
| EIA-930 | CISO BA | Net of BTM solar (measured) | Historical (2015–) | Ground truth |
| IEPR | PGE+SCE+SDGE | `BASELINE_NET_LOAD` (net) / `BASELINE_CONSUMPTION` (gross) | 2024–2050 | Policy forecast |
| RESOLVE | PGE+SCE+SDGE+IID+LDWP+NCNC | Gross (BTM solar on supply side) | 2024–2045 | IRP optimization target |
| ReEDS projected | p8–p11 (CA total); p9–p11 = WECC_CA | Net, IRA_low scenario | 2020–2050 | Long-run US capacity planning |
| ReEDS historic | p9–p11 (WECC_CA ≈ BANC+CISO+IID+LDWP+TIDC) | Net, actual observed | 2016–2023 | Ground truth at WECC_CA scale |
| Substations | PGE+SCE+SDGE distribution | Net-of-BTM at the substation meter | Historical monthly | Sub-BA spatial resolution |

## Where this is implemented

| Concept in this doc | Function / script | File |
|---|---|---|
| RESOLVE ingest | `process_resolve.py` | `scripts/data/resolve/` |
| ReEDS projected + historic ingest | `process_reeds.py`, `process_historic_load.py` | `scripts/data/reeds/` |
| ReEDS county reference (**weights only**) | `reeds_county_annual()` | `scripts/load_projection/checks/validate_county_reeds.py` |
| p-region → county split | `compute_county_pgroup_fractions()` | `src/load_projection/weights.py` |
| IEPR ingest | `process_iepr.py` | `scripts/data/iepr/` |
| Cross-source comparison (this doc's tables) | `compare_resolve_iepr_eia.py` | `scripts/data/` |
| CAISO history for Approach 2 | `load_caiso_history()` | `src/load_projection/stochastic.py` |

## RESOLVE

RESOLVE (E3/CPUC Integrated Resource Planning model) is the statewide optimization model
used by CPUC for the 2024–2026 IRP. Raw inputs in `data/raw/RESOLVE Code Base and
Inputs/`; processed in `data/processed/resolve/`. It covers six CA BA zones — PGE, SCE,
SDGE, IID, LDWP, NCNC (Northern California Non-CAISO = BANC + TIDC + SMUD + small northern
munis). It does **not** model NEVP or PACW as California zones.

### RESOLVE net load: gross → net derivation

`resolve_hourly_profiles.csv` stores **gross demand** (`demand_mw_2024scaled`) — RESOLVE
models rooftop PV as a supply-side resource `Customer_PV`, removed from the demand side.
To compare against EIA-930 / IEPR (net-of-BTM), subtract it:

```
resolve_net_mw = demand_mw_2024scaled − (weather_factor × planned_capacity_2024)
```

`compare_substation_eia_iepr.py` applies this via `_load_resolve_customer_pv_native()`,
loading `data/profiles/pmax/2025/{UTIL}_Customer_PV.csv` (hourly `Weather Factor` 0–1
across 23 weather years — real SAM outputs with day-to-day cloud variability) and
`data/interim/resources/{UTIL}_Customer_PV.csv` (planned capacity; 2024:
PGE 9,669 MW · SCE 6,553 MW · SDGE 2,463 MW). The native RESOLVE profiles add realistic
day-to-day solar variability the IEPR fixed monthly template cannot.

## ReEDS (IRA_low + Historic)

ReEDS is NREL's US capacity-planning model producing long-run US-wide projections through
2050. Four California p-regions: p8 (PacifiCorp West CA slice, WECC_NW, ~0.8 TWh/yr) and
p9/p10/p11 (WECC_CA). **WECC_CA = all CA BAs except PacifiCorp West** (≈ BANC+CISO+IID+
LDWP+TIDC) — confirmed empirically (p9–p11 ~252–268 TWh tracks PUDL CA5, not EIA CISO
~218–224 TWh). IID and LDWP are folded into WECC_CA rather than appearing separately.

Projected CA total (IRA_low, all 4 p-regions, mean across 7 weather years): 2020 291,
2025 288, 2030 336, 2035 394, 2040 449, 2050 525 TWh. The near-zero sd across weather
years confirms weather affects only the hourly *shape*, not the annual total. ReEDS runs
higher than RESOLVE/IEPR because it covers all of CA (CAISO + PacifiCorp CA) and projects
strong EV/electrification growth.

## BTM Solar Treatment by Source

The most important difference between sources is how they handle rooftop solar (BTM PV) —
a source that subtracts it always reads lower.

| Source | BTM Treatment | Load metric | Raw vs derived | ~2024 CA annual |
|--------|---------------|-------------|----------------|-----------------|
| **EIA-930 (CISO)** | Net-of-BTM | Net demand at CAISO boundary | Raw | ~224 TWh |
| **EIA-930 (CA8 group)** | Net-of-BTM | Sum of 8 BAs incl. NEVP + PACW | Raw, inflated ~55–60 TWh by non-CA load | ~285 TWh (overestimates) |
| **EIA CAL region** | Net-of-BTM | Geographic CA; NEVP/PACW excluded | Raw | ~270–273 TWh |
| **IEPR `BASELINE_CONSUMPTION`** | Gross (BTM not yet subtracted) | Gross at grid busbar | Raw (CEC workbooks) | ~247–250 TWh |
| **IEPR `BASELINE_NET_LOAD`** | Net-of-BTM (− BTM_PV − BTM_STORAGE) | Net system load | Raw | ~217–220 TWh |
| **IEPR `MANAGED_NET_LOAD`** | Net + all scenario overlays | Final scenario net load | Raw | ~217–220 TWh |
| **RESOLVE Baseline** (`demand_mw_2024scaled`) | Gross (BTM as supply) | Gross before BTM subtraction; incl. T&D losses | Derived from IEPR MANAGED_NET_LOAD | ~241 TWh (PGE+SCE+SDGE) |
| **RESOLVE Net Load** (derived) | Net-of-BTM (own weather-year Customer_PV subtracted) | Net for peak-hour comparison | `demand_mw_2024scaled − weather_factor × planned_capacity_2024` | ~221 TWh mean (23 weather years) |
| **ReEDS IRA_low projected** | Projected net (BTM as generation resource) | Long-run net; CA total p8–p11 | Raw | ~288 TWh (2025) → ~525 (2050) |
| **ReEDS historic** | Net (BA-meter data via hourlize) | Observed 2016–2023 | HDF5 processed | ~252–268 TWh (WECC_CA) |
| **Substations** (PGE/SCE/SDGE clean) | **Net-of-BTM at the substation meter** | Net at the distribution substation | Raw (utility scrapes) | n/a (percentile envelope) |

**Substation net-of-BTM evidence** (`# VERIFIED: sanity check`, from
`stochastic_diagnostics.py`): 13,318 cells across 368 substations have negative `min_load`
(reverse flow — only possible when BTM export exceeds local load), and the implied scaling
`Σμ_s / CAISO_cell_mean` dips midday (0.66 at h10–11 vs 0.72–0.74 overnight) — the
signature of the same BTM offset being netted from both sides. Practical rule: **pair
substation weights with net targets** (EIA-930, IEPR `BASELINE_NET_LOAD`, RESOLVE derived
net), not gross (`BASELINE_CONSUMPTION`, `demand_mw_2024scaled`). Approach 1 runs using the
gross variants predate the 2026-07-16 net revision.

**Key implication:** a direct RESOLVE-Baseline-vs-EIA-CISO comparison shows an apparent
~17–20 TWh gap in 2024, from (1) BTM PV (~30 TWh statewide; PGE+SCE+SDGE ~17–18 TWh),
(2) geographic scope, and (3) T&D losses (RESOLVE at the busbar; `td_losses_adjustment` =
1.0 for CAISO utilities in this cycle).

## RESOLVE vs IEPR: modeling framework differences

RESOLVE is not an independent demand forecast — it uses IEPR as its load input and
transforms it for a resource optimization.

| Dimension | IEPR | RESOLVE |
|-----------|------|---------|
| What is reported | "Total CAISO Load" = demand + T&D losses, net of BTM | "Baseline Consumption" = IEPR with overlays stripped, BTM PV added back |
| BTM PV | Subtracted from demand | Supply-side resource (ELCC-weighted) |
| BTM storage | Demand reduction | Modeled explicitly; net losses added to demand |
| EV / AAFS / AAEE / Data Centers / Climate | Embedded in scenario totals | Modeled as additive overlays |

RESOLVE uses **Perfect Capacity (PCAP)** PRM (every resource at its ELCC); IEPR does not
model resource adequacy. PRM targets (2024–2026 IRP): 2026 15.6%, 2030 14.5%, 2035 14.9%,
2040 14.1%. ELCC from a 3-D surface (solar × 4-hr × 8-hr battery) across 23 weather years
compressed to 36 representative days via affinity-propagation clustering.

Geographic zones: California CAISO (PGE, SCE, SDGE); Non-CAISO California (IID, LDWP, NCNC);
PNW out-of-state (NW: BPAT, PACW, PortlandGE); Desert SW out-of-state (SW: AZPS, NEVP, SRP,
WALC). **Neither NEVP nor PACW is a California zone in RESOLVE.**

### RESOLVE Baseline + overlays = IEPR (mathematical verification)

From Table 2 of the CPUC 2024–2026 IRP I&A (Feb 2026), 2025 values (GWh):

```
IEPR Total CAISO Load                  217,688
  − Light-Duty Vehicle EVs             −  3,024
  − Med/Heavy Duty Vehicle EVs         −    717
  − AAFS (Building Electrification)    −    391
  + AAEE (Energy Efficiency)           +  3,110   ← demand reduction, add back
  − Data Centers                       −  2,149
  − Climate Impacts                    −    213
  + Behind-the-Meter PV               + 30,154   ← subtracted from IEPR, add back
  − BTM Storage Net Losses             −     72
  = Baseline Consumption               244,386
```

The identity holds by construction (RESOLVE's Baseline is derived *from* IEPR; running
RESOLVE to equilibrium reconstructs IEPR net demand). **Note on `demand_mw_net`:** the
processed output subtracts only the BTM PV overlay, so it is closer to IEPR Total CAISO
Load than `demand_mw_2024scaled` but not equal — the remaining non-BTM overlays (~26
TWh/yr for CAISO in 2025) are still embedded.

## EIA CA8 group: California fractions by BA

EIA-930 defines a "CA8" group of 8 BAs; three serve significant out-of-state territory,
inflating the CA8 total. Retail-sales fractions from EIA Form 861:

| BA | Territory | 2024 CA % | 2024 CA / total load |
|----|-----------|-----------|----------------------|
| BANC | N. California co-ops/munis | 100% | 15.8 / 15.8 TWh |
| CISO | PGE + SCE + SDGE | ~100% | 285.3 / 285.3 TWh |
| IID | Imperial Irrigation District | 100% | 3.7 / 3.7 TWh |
| LDWP | LADWP | 100% | 23.4 / 23.4 TWh |
| TIDC | Turlock Irrigation District | 100% | 2.3 / 2.3 TWh |
| WALC | Western Area Lower Colorado | 31% | 3.8 / 12.2 TWh |
| PACW | PacifiCorp West | 4% | 0.85 / 21.2 TWh |
| NEVP | NV Energy | 0.4% | 0.18 / 47.2 TWh |

CA8 group total ~411 TWh retail sales; actually in CA ~335 TWh; out-of-CA inflation ~76
TWh (NEVP 47, PACW 20, WALC 8). **Practical implication:** for annual totals use EIA CISO
(~224 TWh) or EIA CAL (~270–273 TWh), not EIA CA8 (~285 TWh demand / ~411 TWh sales).
RESOLVE's PGE+SCE+SDGE total (~241 TWh gross, ~211 TWh net) is the most directly comparable
forecast for the CAISO footprint.

## Peak hour alignment: reconciling three measures of IEPR vs EIA

Three analyses measure IEPR-vs-EIA peak-hour alignment and report different numbers
because they measure different things:

| Measure | Script | Result | Meaning |
|---------|--------|--------|---------|
| **fig4 daily offset** | `compare_iepr_eia.py` (projected 2024–2025, date-matched) | ~0h | Near-term IEPR agrees with realized EIA day-by-day |
| **Mean-profile argmax** | `compare_substation_eia_iepr.py` | −1.91h | EIA's mean profile peaks slightly later (BTM duck curve) |
| **Daily argmax distributions** | `compare_substation_eia_iepr.py` (all 2024–2050 pooled) | −2.16h; **−5 to −6h winter** | IEPR's long-range winter peaks cluster near noon; EIA realized at 6–7 PM |

They don't contradict: fig4 restricts to near-term years (2024–2025) where IEPR still
matches the current system; the daily distributions pool 2026–2050 where IEPR projects a
fundamentally different winter shape (as BTM solar grows, morning peaks compete with
evening, and the mean of a bimodal distribution lands near noon).

**RESOLVE as a reference:** its 23 historical weather years (2000–2022) produce winter
daily peaks at ~17–18h, within ~1h of EIA's ~18–19h — a much closer match than IEPR's
long-range projections, because RESOLVE is built from actual historical CA load shapes.
For summer duck-curve months RESOLVE runs 1–2h earlier than EIA (the 2000–2022 shapes
carry an earlier evening ramp than the current grid — the duck has deepened since).

Key outputs (`data/figures/peak_hour_shift/`, `data/tables/`):
`daily_peak_shift_significance_table.png` (three-panel: IEPR all years | near-term
verification | RESOLVE), `daily_peak_distributions_iepr_resolve_eia.png` (violin plots),
`iepr_peak_hour_evolution.png`, `daily_peak_shift_significance.csv`. The
`btm_*` shape-invariance figures in the same folder are produced by
`compare_substation_eia_iepr.py`.
