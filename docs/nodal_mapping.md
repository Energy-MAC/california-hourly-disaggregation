# Nodal mapping — projecting substation loads onto an external test system

Full detail. The README carries the summary and the headline CATS result; this
document holds the assignment rules, voltage-aware mode, coverage gaps, the hybrid
top-up prototype, the ReEDS/statewide validations, and the figures.

`scripts/load_projection/nodal/map_loads_to_nodes.py` assigns each projected
substation's load to the nearest node of an external system (CATS —
`data/raw/CATS/CATS_buses.csv` — by default; any node CSV with id/lat/lon columns
works via `--id-col/--lat-col/--lon-col`). This is the payoff for capacity-expansion
research: it turns the substation-level disaggregation into loads on the test
system's own buses.

## Assignment rules

- Each substation's load goes to its **closest candidate node**; nodes within
  `--tie-tol-km` (0.25) of the minimum distance **share it equally**; a node
  **accumulates** every substation assigned to it. Totals are conserved exactly for
  all substations with coordinates.
- Candidate nodes are filtered by default to real substations (CATS: `Type =
  'Substation'`, non-`IMPORT` — excludes 5,699 line-routing AddedNodes). Pass
  `--no-default-filters` (optionally with `--filter "col=value"`) to let AddedNodes
  receive load.
- Candidates are further restricted to nodes that **ever carry load in the target
  system's own demand table** (CATS: `Demand_data.csv`, columns `Demand_MW_z{bus_i}`)
  — a node the target model never treats as a demand bus is not a meaningful place to
  route load. For CATS this drops 1,307 of 3,168 `Type='Substation'` buses, leaving
  **1,861** real candidates. Disable with `--no-demand-filter`; override with
  `--demand-file`/`--demand-col-prefix`.
- **ReEDS synthetic substations** (Del Norte / Lassen / Modoc / Siskiyou — counties
  with no utility substations) have no real location, so each one's load is **split
  equally across every candidate node inside its county** (point-in-polygon, TIGER
  2022). Only matters for Approach 1. If a county has zero candidate nodes (Del Norte
  on CATS), that one substation falls back to the nearest node to the county centroid.
- Assignments farther than `--max-dist-km` (10) are flagged, not dropped.
- `--unmapped {drop,renormalize}` handles the 22 coordinate-less substations (0.17% of
  fleet load): `drop` (default) leaves that load unassigned; `renormalize` scales every
  mapped node up so the applied total equals the input total.
- Works with **both approaches** via `--apply`: Approach 1 outputs
  (`substation_annual_load.csv`, `substation_monthly_load.parquet`) and Approach 2
  outputs (`substation_annual_mwh.csv`, hourly `draws/draw{k}.parquet`). Long tables
  matched on (utility, substation_name) case-insensitively; wide draw parquets
  matrix-multiplied through the share matrix.

**CATS result** (post zero-demand filter, 1,861 candidate buses): 1,325 real
substations (nearest-node, 76 tie-shared) + 4 synthetic (county equal-split: Lassen
9 nodes, Modoc 8, Siskiyou 12; Del Norte falls back to its nearest node, 49.0 km) →
**1,070** distinct buses receive load. Real-substation distance: median **0.13 km**
(p95 19.8 km, max 105.7 km); 157 substations land > 10 km away. Conservation out/in:
0.99837 (stochastic) / 0.99904 (ReEDS) with `--unmapped drop`; exactly 1.0 with
`renormalize`.

Outputs (`data/processed/load_projection/nodal/{system}/`): `substation_node_map.csv`
(off-mode) / `substation_node_map__voltrestrict.csv` (restrict-mode; both coexist),
`unmapped_substations.csv`, and per `--apply` input a `nodal__{run_tag}__{stem}.csv/.parquet`.

**TODO:** 22 substations (21 SCE, 1 PGE — Visalia, Safari, Costa Mesa, Fair Oaks, …)
have no coordinates; adding them would remove the need for `--unmapped` entirely.

```bash
python scripts/load_projection/nodal/map_loads_to_nodes.py --system CATS \
    --apply data/processed/load_projection/projections/stochastic__eia930__normal__Fcal__native/substation_annual_mwh.csv
```

## Voltage-aware assignment (`--voltage-mode restrict`)

By default (`--voltage-mode off`) nearest-node assignment ignores voltage — a
distribution substation can land on a 230/500 kV bus if it happens to be closest.
`--voltage-mode restrict` requires the assigned node's CATS voltage class to match
the substation's own high-side class before nearest-node applies, falling back to
unrestricted nearest for substations with no known voltage or no same-class node
within `--voltage-max-dist-km`.

**Substation high-side voltage** (`highside_kv` in `substation_attributes_clean.csv`):
SCE/SDGE publish a transformer-ratio string (`substation_voltage`, e.g. `"115/33 kV"`)
whose first token is the high side — used directly. PGE publishes no such field, so
its `highside_kv` comes entirely from CEC's `max_voltage_kv`, attached by
normalized-name `.map()` (never a row-dropping merge). Both this and the CATS node's
`kV` are snapped onto CATS's four classes {66, 115, 230, 500 kV} by
`band_to_cats_class()` (boundaries at the geometric means 87.1, 162.6, 339.1 kV).

**Validation** (`checks/validate_voltage.py` → `data/checks/voltage_validation/`):
coverage PGE 611/670 (from CEC), SCE 557/578 (utility + 5 CEC rescue), SDGE 99/99
(utility). Utility-vs-CEC class agreement is only ~87% for SCE/SDGE — cross-checked
against a third SCE signal (`sys_name`, which names the transmission *area* a
substation is fed from) it emerged that **CEC's `max_voltage_kv` tends to record a
site's broader transmission-area voltage, not the substation's own load-attachment
voltage**. This doesn't affect SCE/SDGE (utility value used directly) but is a caveat
for **PGE, whose `highside_kv` is ~100% CEC-derived**. Under proximity-only mapping,
substation class matches the assigned node's class only 65.8% (PGE) vs 90.5% (SCE) /
79.8% (SDGE) — the gap `restrict` closes.

**Sensitivity vs proximity-only** (`checks/compare_voltage_mapping.py` →
`data/checks/voltage_mapping_comparison/`): per-node load Spearman **r = 0.740**;
**8.0%** of substations get a different node-set, moving **10.9%** of load mass (PGE
~19%, SDGE ~13%, SCE ~4%); reassigned substations travel a median **3.5 km** farther;
**23.9%** hit the no-same-class fallback; voltage-match rate 77.5% → 100% among
non-fallback rows. Net: proximity gets most assignments right; voltage-restriction is
a targeted, quantifiable correction to a specific minority.

```bash
python scripts/load_projection/nodal/map_loads_to_nodes.py --system CATS --voltage-mode restrict
python scripts/load_projection/checks/validate_voltage.py
python scripts/load_projection/checks/compare_voltage_mapping.py
```

## Figures

- **`plot_coverage_map.py`** — interactive Folium choropleth of CA counties colored by
  CATS-nodes-per-IOU-substation ratio. Counts only real substations with a real
  coordinate; the 4 synthetic counties are marked with a dashed red border. →
  `data/figures/load_projection/coverage/coverage_map_{system}.html` +
  `coverage_by_county_{system}.csv`.
- **`plot_nodal_diagnostics.py`** — `dist_hist.png` (assignment distance, median
  0.07 km), `tie_hist.png` (219 of 1,325 substations split across 2+ nodes),
  `voronoi.png` (Voronoi partition of candidate nodes clipped to CA). →
  `data/figures/load_projection/nodal/{system}/`.
- **`plot_pipeline_explainers.py`** — worked examples with real numbers:
  `reeds_chain_example.png` (two-stage chain), `iou_chain_example.png` (single-stage
  IOU chain), `nodal_assignment_schematic.png` (the four assignment rules). →
  `data/figures/load_projection/pipeline/`.

```bash
python scripts/load_projection/nodal/plot_coverage_map.py --system CATS
python scripts/load_projection/nodal/plot_nodal_diagnostics.py --system CATS
python scripts/load_projection/shared/plot_pipeline_explainers.py
```

## Validation against ReEDS

- **`validate_county_reeds.py`** — for every county with ≥1 real IOU substation,
  compares the county's Approach 2 stochastic total (mean over draws) against an
  independent ReEDS-implied county load (`p_region annual load × county_pgroup_fraction`
  — the purely-geographic Stage-1 weight, no dependence on substation shape), 2016–2023.
  Pooled relative RMSE **105%**, but this single number is misleading: error is almost
  entirely municipal-utility coverage gaps, not model error. IOU-pure counties match
  closely (Fresno 8.1%, Alameda 6.7%, Ventura 3.1%, Humboldt 1.2%); muni-heavy counties
  show huge negative bias (Sacramento/SMUD −98.9%, Imperial/IID −99.8%, Stanislaus
  −90.0%, LA −51.9%). **Trinity (−94.8%) is the one genuine undersampling case** (real
  PGE, 1 scraped substation). Mono is **+301%** (small, high-tourism-load county where
  ReEDS' population-based fraction understates metered demand). Output:
  `data/processed/load_projection/validation/county_reeds_stochastic_annual_*.csv`,
  `data/figures/load_projection/validation/county_reeds_relrmse_bar.png`.
- **`plot_ba_iou_comparison.py`** — are ReEDS p-regions utility-pure? (No official IOU
  shapefile exists, so footprint is approximated by the substation point cloud.) p9 =
  96.8% PGE, p11 = 100% SDGE, p10 = 90.4% SCE with 8.6% PGE bleed-through (eastern
  Sierra/high-desert). p8 has zero PGE/SCE/SDGE substations (PacifiCorp-only). Output:
  `ba_iou_purity.csv`, `ba_iou_map.png`.

```bash
python scripts/load_projection/checks/validate_county_reeds.py
python scripts/load_projection/checks/plot_ba_iou_comparison.py
```

## Nodal coverage gaps

Filtering candidate nodes to only those the target ever loads surfaces a coverage
problem distinct from mapping *accuracy*: several counties have real IOU substations
but far more CATS demand buses than substations, so most buses get zero load. Two
flavors:

- **Sparse-but-real** (Trinity: 4 buses, 1 substation) — genuinely undersampled utility
  territory; more scraped substations would help.
- **Municipal-utility gaps** (Sacramento/SMUD: 190 buses, 1 substation; Imperial/IID: 22
  buses, 1 substation) — PGE/SCE/SDGE never had substations there to scrape; a
  data-source gap, not sampling error.

`coverage_by_county_{system}.csv` has the full per-county breakdown.

## Hybrid county top-up (prototype)

`hybrid_county_topup.py` — **PROTOTYPE, not wired into the main pipeline.** For every
county whose (real substations / demand-filtered candidate nodes) ratio is below
`--ratio-threshold` (default 0.5) AND whose ReEDS county reference exceeds its Approach
2 stochastic total, the shortfall is distributed across that county's *uncovered* CATS
nodes (never its substations — uncovered nodes stand in for the county's actual MOU
buses). Two methods: `equal` (uniform share) and `proportional` (weighted by each
node's own CATS `Demand_data.csv` load). At the default threshold, **5 counties
qualify**: Sacramento (+7.9 TWh/yr, 188 nodes), Stanislaus (+3.7, 35), Imperial (+3.1,
22), Trinity (+77 GWh, 3), Inyo (+25 GWh, 5). LA does **not** qualify despite its LADWP
gap (enough SCE substations keep its ratio above 0.5). Outputs:
`hybrid_topup_{counties,nodes}_{method}.csv`, `hybrid_topup_map_{method}.png`.

```bash
python scripts/load_projection/nodal/hybrid_county_topup.py --method equal
python scripts/load_projection/nodal/hybrid_county_topup.py --method proportional
```

## Statewide total validation

`validate_approach_totals.py` compares each approach's statewide annual total against
EIA-930 CAISO and ReEDS `CA_total`, 2016–2023. Two comparisons are informative, two
tautological:

- **Approach 2 vs EIA-930 CAISO**: mean **−26.4%**, stable within 0.3 pp across all 8
  years. NOT a 0% target — Approach 2 targets F\*=0.7361 of CAISO by design; the
  *stability* is the confirmatory result.
- **Approach 2 vs ReEDS CA_total**: mean **−37.9%** (adds the muni/PacifiCorp share).
- **Approach 1 vs EIA-930 CAISO**: mean **+18.6%** — NOT an Approach-1 error. Approach 1
  reconstructs its ReEDS p-region input exactly, so this reflects ReEDS's own historic
  reconstruction vs actual CAISO (+18.2%) plus ReEDS's "CAISO_total" region (p9+p10+p11)
  being geographically broader than the CISO BA.
- **Approach 1 vs ReEDS CA_total**: mean **≈0.0000%** — tautological conservation check.

Output: `approach_totals_vs_reference.csv`, `approach_totals_vs_reference.png`.

```bash
python scripts/load_projection/checks/validate_approach_totals.py
```

## Known limitations (future work)

- The stochastic approach only covers the PGE/SCE/SDGE fraction of CAISO; non-IOU
  regions in the weights approach have no within-region variance.
- The hybrid top-up is a prototype with no formal "Approach N" section.
