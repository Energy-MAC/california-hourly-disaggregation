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

### Where this is implemented

| Concept in this doc | Function | File |
|---|---|---|
| Nearest-node assignment, ties, synthetic split | `build_mapping()`, `assign_synthetic()` | `nodal/map_loads_to_nodes.py` |
| Candidate-node filtering (zero-demand rule) | `filter_zero_demand()`, `demand_totals()` | same |
| Voltage-aware restriction | `band_to_cats_class()` (used inside `build_mapping`) | same |
| Applying a projection onto nodes | `apply_projection()`, `apply_long()`, `apply_wide()` | same |
| Identity (CEC-lineage) matching | `identity_pairs()` | `nodal/build_identity_catchment_maps.py` |
| Identity-first map assembly | `build_nameprox()` | same |
| Catchment transportation LP | `solve_catchment()` | same |
| Substation name normalisation | `norm()` | `data/substations/build_cec_name_dictionary.py` |
| Coordinate overrides | `apply_coordinate_overrides()` | `data/substations/process_substations_clean.py` |
| Override sanity check (duplicate / isolated) | `main()` | `data/substations/check_coordinate_overrides.py` |
| Substation → county | `assign_substation_counties.py` | `data/substations/` |

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
- `--unmapped {drop,renormalize}` handles the coordinate-less substations (**1** as of
  2026-08-17, down from 12, and it carries no load — see the worklist below): `drop` (default) leaves that load
  unassigned; `renormalize` scales every mapped node up so the applied total equals the
  input total.
- Works with **both approaches** via `--apply`: Approach 1 outputs
  (`substation_annual_load.csv`, `substation_monthly_load.parquet`) and Approach 2
  outputs (`substation_annual_mwh.csv`, hourly `draws/draw{k}.parquet`). Long tables
  matched on (utility, substation_name) case-insensitively; wide draw parquets
  matrix-multiplied through the share matrix.

**CATS result** (post zero-demand filter, 1,861 candidate buses; refreshed 2026-08-17
after the coordinate overrides): **1,336** real substations (nearest-node, 77
tie-shared) + 4 synthetic (county equal-split: Lassen 9 nodes, Modoc 8, Siskiyou 12;
Del Norte falls back to its nearest node, 49.0 km) → **1,071** distinct buses receive
load. Real-substation distance: median **0.133 km** (p95 19.85 km, max 105.7 km);
159 substations land > 10 km away. Conservation out/in approaches 1.0 with
`--unmapped drop` as the unplaced set shrinks; exactly 1.0 with `renormalize`.

The eleven substations placed by the override file mostly land far from a bus — they are
remote SCE sites (Mountain Pass, Camino, Bishop Creek, Poole) — which is why the >10 km
count rose from 157 to **159** even though median distance barely moved. That is honest
coverage, not degraded matching: previously their load was simply dropped.

Outputs (`data/processed/load_projection/nodal/{system}/`): `substation_node_map.csv`
(off-mode) / `substation_node_map__voltrestrict.csv` (restrict-mode; both coexist),
`unmapped_substations.csv`, and per `--apply` input a `nodal__{run_tag}__{stem}.csv/.parquet`.

```bash
python scripts/load_projection/nodal/map_loads_to_nodes.py --system CATS \
    --apply data/processed/load_projection/projections/stochastic__eia930__normal__Fcal__native/substation_annual_mwh.csv
```

### Substations with no coordinate — the worklist and how to fix one

The count that matters is **12, not 22**: 22 substations lack a *utility-published*
coordinate, but 10 of those are placed by a DataBasin name match, leaving 12 (all
SCE) with no coordinate from any automatic source. All 12 carry real load profiles,
so each one was load we could not place on the network.

**Eleven of the twelve are now placed** (2026-08-17), leaving exactly one:
`Autobody` — which is one of the six SCE sites with **no load data at all** (288
cells, every one NaN). So the remaining coordinate gap carries no load, and the
fleet stands at **1,346 of 1,347 substations with a coordinate**. This is the
practical end of the coordinate work: nothing further can be gained, because the
only unplaced substation has nothing to place.

**The worklist is `data/substationCoordinateOverrides.csv`** — the coordinate
analogue of `basinSourceDictionary.csv` / `cecSourceDictionary.csv`, and the one
place a hand-researched coordinate should be written:

| Column | Meaning |
|---|---|
| `utility`, `substation_name` | must match the cleaned name exactly (normalised on both sides, so punctuation/case are forgiving — `Palm Springs 'A'` is fine) |
| `lat`, `lon` | WGS84 decimal degrees. **Leave blank while unresolved** — blank rows are skipped, so the file doubles as the to-do list |
| `source` | provenance, written through to the `coord_source` column (e.g. `cec_exact_name_match`, `google_earth`, `sce_map_pdf`) |
| `notes` | free text; the shipped placeholders record what has already been ruled out |

Resolved: **Topanga** and **Paularino** from exact CEC name+owner matches;
**Safari**, **Mountain Pass**, **Camino**, **Bishop Creek Plant 2**, **Santa Ana
River 1**, **Poole** and **Lunar** from utility fact-sheets, Google Earth/Maps, a
radio-site registry and a BESS interconnection filing; **Palm Springs 'A'** and
**'B'** from DataBasin (both resolve to the same site, so they land on one bus).
Still open: **Autobody** only — and it has no load data.

Note `coord_source` is populated only where a *utility or override* coordinate
exists; the ~10 substations placed by a DataBasin name match have `basin_lat`
instead and a blank `coord_source`, which is why that column shows more blanks than
the genuinely unplaced site.

#### Provenance and caveats for the eleven researched coordinates

Written out because these are hand-made decisions and a reader of the paper is
entitled to see them. Verify any of it with
`python scripts/data/substations/check_coordinate_overrides.py --cec`, which
reports each override's nearest same-utility substation and nearest CEC record.

| Substation | Corroboration | Note |
|---|---|---|
| Topanga, Paularino | CEC exact name+owner match, 0–1 m | Unambiguous |
| Poole | CEC `Poole Ph` (SCE, Mono Co., 115 kV) at **75 m**; nearest SCE substation 8.7 km | Only "Poole" record in the CEC inventory statewide; clean |
| Safari | CEC record at 658 m; nearest SCE substation 4.1 km | SCE's own Safari substation map PDF |
| Mountain Pass | CEC `Mt. Pass` at 74 m; **our own `Mountain Pass A` at 169 m** | Same physical site as `Mountain Pass A` (both `Kramer 220/115 System`); CEC lists **one** facility there. Two banks at one site — they share a CATS bus and their loads sum. Not a duplication error. |
| Camino | CEC `Camino - (Other)` at 133 m, 230 kV, San Bernardino Co. | Isolated (65 km to the nearest SCE substation) because it is in SCE's Needles-area territory — 22 SCE substations lie east of −116°. CEC's `owner_raw` is `Other (SCE - Assumed)`, an *unconfirmed-owner* tag, not a different owner; SCE's own `sys_name` for it is `Camino 220/16 System`, i.e. SCE names a transmission system after it. Confirmed. |
| Bishop Creek Plant 2 | CEC `Bishop Creek 2` at 130 m | SCE Bishop Creek hydro chain, Inyo Co. |
| Santa Ana River 1 | CEC `Santa Ana 3` at 97 m | SCE Santa Ana River hydro chain; zero envelope (see below) |
| Palm Springs 'A' / 'B' | CEC `Palm Springs` at 1 m | **Deliberately the same coordinate** — two banks at one station. They map to one bus and their loads sum (1.35 + 0.70 MW). |
| **Lunar** | **No CEC record named "Lunar" exists anywhere in the state** | ⚠️ **See the caveat below.** |

> **⚠️ Lunar — an unresolved placement, recorded for the write-up.**
> The coordinate used (34.68577, −118.30349) sits in the Antelope Valley, 333 m
> from CEC's `Antelope - (SCE)` 500 kV station, and was inferred from the Luna
> BESS project, which interconnects via Big Sky to Antelope. Nothing
> independently confirms it: CEC has **zero** records containing "Lunar".
>
> **Counter-evidence:** SCE's own attribute for this substation is
> `sys_name = "Big Creek 220/220 System"`, and the four other substations
> carrying that `sys_name` (Timberwine, Pitman, Big Creek 2, Camp 10) all sit in
> the Big Creek hydro complex in Fresno County, **286–294 km away**. `sys_name`
> denotes the transmission area a substation is fed from, so this points at the
> Sierra complex rather than the Antelope Valley. (The Big Creek 220 kV corridor
> does run south toward Vincent/Antelope, which is why the Antelope reading is
> not absurd — but the other four members cluster at Big Creek itself.)
>
> **Why it does not affect any result:** Lunar's mean `max_load` envelope is
> **exactly 0.000 MW across all 288 cells.** It is one of the 28 substations with
> a non-positive envelope, so it is clipped to zero weight in every
> envelope-weighted and stochastic allocation and contributes nothing to any
> share vector. Under the proximity map it lands on bus 8341 (0.333 km) — the
> Antelope bus — carrying no load. The placement is therefore recorded as
> uncertain and left as-is; resolving it would change no number in this paper.

**Propagation is automatic.** `process_substations_clean.py` applies the file
(`apply_coordinate_overrides()`) when it builds
`substation_attributes_clean.csv`, filling `util_lat`/`util_lon`. Since every
downstream consumer — nodal mapping, the identity/catchment map builder, GenX
county assignment, the ML feature assembly — reads its coordinates from that
one file, a filled row reaches all of them with no further edits. So the loop is:

```bash
# 1. edit data/substationCoordinateOverrides.csv (fill lat/lon/source)
python scripts/data/substations/process_substations_clean.py     # 2. rebuild attributes
python scripts/load_projection/nodal/map_loads_to_nodes.py --system CATS   # 3. remap
python scripts/load_projection/nodal/build_identity_catchment_maps.py      # 4. rebuild GenX maps
```

Guard rails: an override is **last resort only** — a substation that already has
a utility coordinate is never overwritten (those rows are counted and reported as
skipped) — and the step asserts that the substation count is unchanged, so the
file can add a location but never a substation.

### What of this feeds the GenX rescaling

Only a narrow slice of this pipeline is load-bearing for the GenX experiment:

| Used by GenX | How |
|---|---|
| `substation_node_map.csv` | the `--map prox` artifact, used directly |
| `substation_node_map__voltrestrict.csv` | the `--map voltres` artifact |
| the substation set + coordinates behind them | `build_identity_catchment_maps.py` takes its substation list from the prox map and its coordinates from `substation_attributes_clean.csv`, then builds the `nameprox` / `catch` / `namecatch` artifacts |
| `nodes_by_county()` (from `hybrid_county_topup.py`) | the point-in-polygon bus→county join behind the county-first allocation |

**Not used by GenX:** the `--apply` outputs (`nodal__*.csv/parquet`) — the
rescaler always recomputes bus loads from the substation level, because those
frozen files are tied to one map mode; `--unmapped` renormalisation, since the
GenX share vectors are normalised per allocation anyway; the coverage/diagnostic
plots; and `hybrid_county_topup.py` itself, which is a prototype and supplies
only its county-join helper. Note also that GenX's **candidate-bus pool is
deliberately different** from this page's: it keeps all 3,168 `Type='Substation'`
buses plus the 610 loaded `AddedNode`s (3,778), rather than applying the
zero-demand filter that yields 1,861 here — see
[genx_rescale.md](genx_rescale.md).

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
  0.07 km), `tie_hist.png` (substations split across 2+ nodes),
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
