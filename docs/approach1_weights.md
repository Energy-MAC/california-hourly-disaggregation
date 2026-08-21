# Approach 1 — Proportional participation weights

Full detail for Approach 1. The README carries the one-paragraph summary and the
core formula; this document holds the chains, parameters, output files, validation
numbers, and run commands.

Each substation is assigned a **participation weight** equal to its share of its
regional total load at a chosen percentile and temporal resolution. Projected
regional load is then multiplied by that weight to produce a substation-level
hourly forecast. This is a proportional scaling approach: it preserves the
regional total at every hour. It does **not** account for substation-specific
growth trajectories or changing load shapes (that is Approach 2's role).

## Where this is implemented

| Concept in this doc | Function | File |
|---|---|---|
| Load the percentile envelopes | `load_profiles()` | `src/load_projection/weights.py` |
| Collapse to annual / monthly / hourly / month-hour | `aggregate_to_level()` | same |
| The weight itself (share within a region) | `normalize_within()` | same |
| Expand weights to an hourly matrix | `broadcast_to_matrix()` | same |
| IOU chain (IEPR / RESOLVE → substation) | `build_iou_weight_matrices()` | same |
| ReEDS chain (p-region → county → substation) | `build_reeds_chain_matrices()` | same |
| p-region → county geographic split | `compute_county_pgroup_fractions()` | same |
| Synthetic substations for county gaps | `_add_synthetic_rows()` | same |
| Apply weights to a projected series | `apply_weights_to_series()` | same |
| Drivers | `disaggregate_reeds.py`, `disaggregate_iou.py` | `scripts/load_projection/approach1/` |

## Method

For each (month, hour) cell:

```
weight[s, m, h] = max(load_col[s, m, h], 0) / Σ_{j∈Region} max(load_col[j, m, h], 0)  for all Regions
```

Negative or missing values are clipped to zero; equal weights are used as a
fallback when all substations in a region are zero at a cell. Applied vectorized:

```
substation_load[s, t] = regional_load[region(s), t] × weight[s, month(t), hour(t)]
```

Two disaggregation chains apply this weighting depending on forecast source.

### ReEDS chain (p-region → county → substation) — `disaggregate_reeds.py`

Stage 1 uses county-level load participation fractions from ReEDS (geographic,
constant across all hours). Stage 2 distributes county load to substations using
the substation profiles at the chosen temporal resolution.

```
county_pgroup_fraction[c] = ca_load_fraction[c] / Σ ca_load_fraction[c]  for counties in p-region
sub_county_weight[s, m, h] = max(weight_col[s,m,h], 0) / Σ max(weight_col[s,m,h], 0)  for s in county(s)
chain_weight[s, m, h]      = county_pgroup_fraction[county(s)] × sub_county_weight[s, m, h]
substation_load[s, t]      = p_region_load[p_region(s), t] × chain_weight[s, month(t), hour(t)]
```

Chain weights sum to 1.0 per p-region at every cell (max deviation ≤ 2×10⁻¹⁵).
1,333 substations total: 1,329 real + 4 synthetic for counties with no utility data
(`SYNTHETIC_DEL_NORTE` in p9; `SYNTHETIC_LASSEN`, `_MODOC`, `_SISKIYOU` in p8).
p8 is entirely PacifiCorp territory — no PGE/SCE/SDGE substations fall there.

### IOU chain (IOU → substation) — `disaggregate_iou.py` (IEPR and RESOLVE)

Single-stage: distributes each IOU's hourly load among its substations.

```
sub_iou_weight[s, m, h] = max(weight_col[s,m,h], 0) / Σ max(weight_col[s,m,h], 0)  for s in IOU(s)
substation_load[s, t]   = IOU_load[IOU(s), t] × sub_iou_weight[s, month(t), hour(t)]
```

PGE (670 subs), SCE (578 subs), SDGE (99 subs) only. IEPR VEA load and RESOLVE
IID/LDWP/NCNC load are excluded (no substation data for those utilities).

Substation profiles are **net-of-BTM load** — pair them with net statewide targets
(EIA-930, IEPR `BASELINE_NET_LOAD`, RESOLVE derived net load), not gross ones. See
[statewide_forecast_sources.md](statewide_forecast_sources.md) → "BTM Solar
Treatment by Source" for the evidence and the source-by-source gross/net breakdown.

## Parameters

| Flag | Options | Default |
|------|---------|---------|
| `--weight-col` | `min_load`, `max_load`, `avg_load` | `max_load` |
| `--weight-level` | `annual`, `monthly`, `hourly`, `monthhour` | `monthhour` |
| `--save-output` | flag | off — weight tables and annual CSVs always written |

`disaggregate_reeds.py` also takes `--mode {historic,projected,both}`.
`disaggregate_iou.py` also takes `--source {iepr,resolve}`, `--vintage {2023,2024,2025}`,
`--scenario`, and `--load-col` (see the script header for full details and defaults).

## Output files

All outputs: `data/processed/load_projection/projections/<run_tag>/`.
Large parquets only written with `--save-output`; CSV files always written.

```
projections/reeds_historic__max_load__monthhour/
  county_pgroup_weights.csv                  # 58-row county → p-region fractions
  substation_chain_weights.csv               # 1,333 subs × 288 (month, hour) cells
  substation_annual_load_by_year.csv         # annual MWh 2016–2023
  [substation_disaggregated_load.parquet]    # ~93M rows / ~750 MB  (--save-output)

projections/reeds_projected__max_load__monthhour/
  county_pgroup_weights.csv
  substation_chain_weights.csv               # apply to any projected p-region series
  substation_annual_load.csv                 # annual MWh by (weather_year, year, substation)
  [substation_monthly_load.parquet]          # ~3.4M rows / ~33 MB  (--save-output)

projections/iepr__v2025__planningscenario__baselineconsumption__max_load__monthhour/
  substation_iou_weights.csv                 # 1,347 subs × 288 cells
  substation_annual_load.csv                 # annual MWh 2025–2050
  [substation_disaggregated_load.parquet]    # ~2.4 GB  (--save-output)

projections/resolve__demandmw2024scaled__max_load__monthhour/
  substation_iou_weights.csv
  substation_annual_load.csv                 # annual MWh by weather year (2000–2022)
  [substation_disaggregated_load.parquet]    # ~2.2 GB  (--save-output)
```

**Validation:** ReEDS historic CA totals 252–270 TWh/yr (2016–2023); IEPR 2025
PGE+SCE+SDGE 242–384 TWh (2025–2050); RESOLVE PGE+SCE+SDGE 241 TWh (2024-scaled).
The statewide-total cross-checks against EIA-930 and ReEDS are in
[nodal_mapping.md](nodal_mapping.md) → "Statewide total validation".

## Run commands

```bash
# ReEDS — both historic and projected, default params (max_load, monthhour)
python scripts/load_projection/approach1/disaggregate_reeds.py

# ReEDS — historic only; also write full hourly parquet
python scripts/load_projection/approach1/disaggregate_reeds.py --mode historic --save-output

# ReEDS — projected, alternate weight column
python scripts/load_projection/approach1/disaggregate_reeds.py --mode projected --weight-col min_load

# IEPR — defaults: 2025 vintage, Planning_Scenario, BASELINE_CONSUMPTION
python scripts/load_projection/approach1/disaggregate_iou.py --source iepr

# IEPR — different vintage or scenario
python scripts/load_projection/approach1/disaggregate_iou.py --source iepr --vintage 2024
python scripts/load_projection/approach1/disaggregate_iou.py --source iepr --scenario Local_Reliability

# RESOLVE — all 23 weather years, defaults
python scripts/load_projection/approach1/disaggregate_iou.py --source resolve

# Either source — also write the full hourly parquet
python scripts/load_projection/approach1/disaggregate_iou.py --source iepr --save-output
python scripts/load_projection/approach1/disaggregate_iou.py --source resolve --save-output
```
