# Data Sources

Full detail on each raw source. The README carries a compact source table; this
document holds the per-source specifics, the substation coverage summary, and the CEC
reference/audit work that underpins **realistic substation identification**. Statewide
forecast sources (RESOLVE, ReEDS, IEPR framework comparison, BTM treatment) are in
[statewide_forecast_sources.md](statewide_forecast_sources.md). Scrape/process commands
and column dictionaries are in [data_pipeline.md](data_pipeline.md).

## Where this is implemented

| Concept in this doc | Function / script | File |
|---|---|---|
| PG&E attribute + feeder scrape | `scrape_pge.py attributes` / `layer --layer-id 25` | `scripts/data/pge/` |
| SCE bulk-download ingest | `ingest_sce_bulk_download.py` | `scripts/data/sce/` |
| SDG&E attribute + profile scrape | `scrape_sdge.py attributes` / `substation-profiles` | `scripts/data/sdge/` |
| Unified clean build (all three utilities) | `process_pge()`, `process_sce()`, `process_sdge()` | `data/substations/process_substations_clean.py` |
| SCE year-stamp rule (highest year per cell) | the `groupby(...)["year"].idxmax()` block in `process_sce()` | same |
| PG&E legacy recovery of deleted substations | the `LEGACY_PGE_LOADS` / `LEGACY_PGE_ATTRS` block in `main()` | same |
| Basin coordinate join + name dictionary | `add_basin_coords()`, `_build_dict_map()` | same |
| High-side voltage enrichment | `attach_highside_voltage()` | same |
| Hand-researched coordinates | `apply_coordinate_overrides()` | same |
| Name normalisation (the match definition) | `norm()` | `data/substations/build_cec_name_dictionary.py` |
| CEC reference build + name dictionary | `process_substations_cec.py`, `build_cec_name_dictionary.py` | `data/substations/` |
| CEC coverage audit / reverse gap | `audit_substation_coverage.py` | same |
| EIA-930 ingest | `scrape_eia.py`, `process_eia.py` | `scripts/data/eia/` |

## EIA 930 (Hourly Balancing Authority Operations)

Eight California-adjacent BAs: BANC, CISO, IID, LDWP, NEVP, PACW, TIDC, WALC.

**Primary source: PUDL nightly parquet.** [PUDL](https://catalyst.coop/pudl/) mirrors
EIA-930 daily with cleaned, imputed, gap-filled values from 2015 onward. Two filtered
parquets (eight CA BAs) → `data/processed/eia/eia930_operations.csv` and
`eia930_interchange.csv`. All timestamps UTC (`datetime_utc`, **hour-ending**).

> **EIA-930 hour convention:** hour-ending UTC — `T06:00:00Z` is the MWh for 05:00–06:00
> UTC. To convert to fixed PST hour-beginning (as used by IEPR, RESOLVE, substations):
> **subtract 9 hours** (8h UTC→PST + 1h ending→beginning). For annual/monthly totals the
> distinction is < 0.01%.

**EIA demand definition:** metered net generation within the BA minus net interchange
with neighbors. Because BTM generation is invisible to BA-boundary meters, this is a
**net-of-BTM** measure.

**Secondary source: EIA API direct scrape** (`scrape_eia.py`) — `rto-interchange`
(BA-pair flows) and `rto-region` (CAL region demand/gen/TI/day-ahead). Used only to
validate PUDL (`compare_eia_sources.py`) and provide the CAL series. For all analysis,
`eia930_operations.csv` (PUDL) is authoritative — it gap-fills, starts earlier (2015 vs
~2019 for the API CAL region), and updates nightly.

`compare_eia_sources.py` cross-checks PUDL against the EIA scrape across five sections
(source summary; hourly coverage; value agreement; NaN audit; scope comparison — annual
TWh EIA CA8 vs PUDL CA5 vs EIA CAL vs IEPR, quantifying the ~60 TWh NEVP+PACW excess in
CA8). "PUDL CA5" = BANC+CISO+IID+LDWP+TIDC, the five BAs serving only CA load. → CSVs in
`data/checks/`, `data/figures/fig_e_cal_vs_ca8_vs_iepr.png`. Run `-s D` for NaN audit only.

## Utility IOU load profiles

Hourly substation-level min/max load profiles (MW) for three utilities:

| Utility | Source | Access |
|---------|--------|--------|
| PG&E | DRP Compliance ArcGIS FeatureServer, layer 25 | Public ArcGIS REST API |
| SCE | DRPEP "Historical Substation Load Profiles" bulk download | Manual ZIP from drpep.sce.com |
| SDG&E | Interactive map download API (ZIP per substation) | Public HTTP API |

PacifiCorp, CalPeco, BVEA, and MOUs do not publish comparable hourly load profiles.

## Utility IOU substation attributes

Physical + DER attributes from each utility's public ArcGIS FeatureServer:

| Utility | Source layer | Key fields |
|---------|-------------|------------|
| PG&E | EDSubstations (layer 0) | voltage_kv, num_banks, existing/queued/total DG (kW→MW) |
| SCE | ICA Tables layer 3 | existing/queued/total gen, projected load, DER penetration, circuit voltage, customer mix |
| SDG&E | ICA_MAP_PROD_Substations_VW (layer 0) | substation type, voltage, gen, projected load, DER penetration |
| PacifiCorp | DG Readiness with Net Minimum (layer 0) | existing DER (MW), net minimum daytime load (MW) |

## Substation coverage summary

After cleaning (removing pass-through switching nodes and failed scrapes) and joining to
the DataBasin CA Substations 2022 reference for coordinates:

|                                      | PG&E    | SCE     | SDG&E  | Total     |
|--------------------------------------|---------|---------|--------|-----------|
| Raw substations published            | 704²    | 748     | 107¹   | 1,559     |
| Removed (P.T. nodes / no load profile)| 34      | 170     | 8      | 212       |
| **Cleaned (in processed output)**    | **670** | **578** | **99** | **1,347** |
| **Basin-matched total**              | **605** | **527** | **96** | **1,228** |
| Not matched to basin                 | 65      | 51      | 3      | 119       |
| Basin substations not in any source  | 346     | 160     | 42     | 548       |
| With a utility/override coordinate   | 669     | 568     | 99     | 1,336     |
| **With ANY coordinate**              | **670** | **577** | **99** | **1,346** |
| Load profile rows (processed)        | 192,912 | 166,440 | 28,512 | 387,864   |

¹ SDG&E: 99 substations with load profiles + 8 published with attributes but **no**
profile (all 69/12 kV distribution sites) = 107 in the attribute layer.

² PG&E publishes 704 named substations in the ArcGIS attribute layer, but only 670 of
them carry feeder load data; the other 34 have attributes and coordinates but no
profile, so they cannot be used. (The layer also returns 72 fully name-redacted rows —
id, coordinates and DER capacity but no name — which cannot be joined to load and are
dropped.)

The **name dictionary** (`data/basinSourceDictionary.csv`, 79 entries) maps utility
source names that differ from the DataBasin reference (e.g. "CRESTA PH" → "Cresta") to
recover geolocation matches beyond the normalised-name join.

### Coverage completeness check (re-scrape, 2026-08-17)

All three utility sources were re-pulled and diffed against the frozen snapshot to
confirm the substation set is complete. **No utility has added or removed a substation**,
so the dataset covers everything the three IOUs currently publish load profiles for.
Comparison artifacts are in `data/checks/rescrape_2026_08_17/`.

| Utility | Method | Result |
|---|---|---|
| PG&E | `scrape_pge.py attributes --output-dir <tmp>` | **704 named substations in both** — none added, none removed. The live layer now returns 776 rows, but the extra 72 are fully **name-redacted** (id + coordinates + DER capacity, no name), so they cannot be joined to load and are not usable. Of the 704, still only 670 have feeder load. |
| SCE | Fresh DRPEP "Download All" ZIP, diffed member-by-member | **709 members in both, identical name set.** Load *values* refresh monthly (the 2026 vintage has since extended from April to July), but no substation appears or disappears. |
| SDG&E | `scrape_sdge.py attributes --output-dir <tmp>` | **107 in both, identical names.** 99 have load profiles; the 8 without are 69/12 kV distribution sites (Basilone, Division, Kyocera, Loveland, Mira Sorrento, Miramar, Pendleton, Warren Canyon) that SDG&E lists but publishes no profile for. |

#### Can the 72 name-redacted PG&E rows be recovered? No — there is no load.

Worth documenting because the idea is reasonable and the answer is definitive.
The redacted rows *do* carry usable identity information: `substation_id`,
longitude/latitude, and DER capacity. Matching them to the CEC inventory by
coordinate works well — **34 of 72 fall within 100 m of a CEC record, 48 within
500 m, 66 within 2 km**, and the matches are plainly real PG&E substations
(Alta Ph, Skaggs Island, San Fran E, Randolph, Signetics, Toadtown Ph…). So we
could name and place them.

**But there is no load to attach.** Querying PG&E's live feeder layer (layer 25)
directly for all 69 distinct redacted `subid`s returns **0 rows** — and the same
is true of the frozen snapshot. The censoring is total: attributes, load, and
even the DER values are placeholders (every one of the 72 reports exactly
`existing_dg_kw = 1000`, `total_dg_kw = 2000`). These are almost certainly
few-customer substations withheld for customer privacy.

Since the disaggregation needs a **load profile** per substation — a coordinate
and a name alone contribute nothing to a share vector — these 72 cannot enter the
dataset. Imputing their load was considered and rejected: the cold-start study
(now legacy, `ml_cookbook.md`) showed structural features recover profile *shape*
but not *magnitude*, so imputed substations would carry invented load.

#### Newly published PG&E feeder load (checked 2026-08-17, not ingested)

The live feeder layer has grown to **639 distinct substations (from 628)** and
184,008 rows (from 180,816). Cross-referencing:

- **21 substations gained feeder load** since the snapshot — but **19 of them are
  already in our build**, recovered through the PG&E legacy path. PG&E has simply
  re-published what it had earlier deleted, which is direct confirmation that the
  legacy-recovery mechanism was reconstructing real substations rather than
  stale artifacts.
- **2 are genuinely new: `RAINBOW` and `TEVIS`** — they previously had attributes
  but no profile.
- **10 substations dropped out** of the live layer (Lakewood, Bridgeville, Suisun,
  Cuyama, Divide, Spring Gap, San Benito, Soquel, Sobrante, Haas). **All 10 remain
  in our build** via the frozen snapshot, so the churn costs us nothing.

**Deliberately not ingested.** Adding the 2 new substations (+0.15% of the fleet)
would require re-scraping layer 25, which also restates every other PG&E
substation's load values and would drop the 10 above unless legacy recovery
happened to catch them. Perturbing a verified pipeline for a 0.15% coverage gain
is a bad trade; the frozen snapshot is the published basis. Recorded here so the
choice is visible rather than implicit.

Two schema notes for anyone re-running the scrapers:

- PG&E changed the `redacted` flag's casing from `Yes`/`No` to `YES`/`NO`, and moved 34
  substations from redacted to non-redacted. This is **cosmetic for this pipeline**: the
  field is only carried through as the `note_sub` passthrough column and is never
  compared to a literal, so no code depends on the casing.
- The load profiles are a **frozen snapshot by design.** SCE restates values and advances
  year-stamps monthly, so re-ingesting mid-analysis would silently change every
  downstream number. The published results correspond to the snapshot in
  `data/raw/`, not to a live pull.

**SCE year-stamp deduplication:** SCE publishes year-stamped profiles (2017–2026), each
an independent 10th/90th-percentile snapshot from a non-public lookback window. 652 of
709 unique SCE substations appear in multiple years. The 2026 vintage covered January–April at the time of the frozen
snapshot (it has since extended to July — see the re-scrape note below). The processed output keeps, per `(substation, month, hour)` cell, the row
with the highest year, so May–December for 2026-batch substations falls back to 2025 —
full 12-month coverage from the most recent snapshot. See `process_substations_clean.py`.

## CEC Substation DataPull (2026) — the authoritative substation reference

The **CEC Substation DataPull (07/24/2026)** is a direct data request from the CEC — a
newer, richer version of the same underlying dataset DataBasin 2022 ("basin") was built
from. It supersedes basin as the authoritative statewide substation reference, and
because **CATS is itself derived from basin**, this pull is the intended basis for
eventually replacing CATS node coordinates. This is central to the project's
"realistic nodes" goal: CEC gives an authoritative, verifiable substation inventory.

**Raw:** `data/raw/CEC_Substation_DataPull_07242026.gdb/` (Esri FGDB, 4,828 rows,
EPSG:3310; `Lat`/`Lon` are WGS84). **Processed:**
`data/processed/substation_misc/ca_substations_cec.csv` (`process_substations_cec.py`)
— mirrors basin's `ca_substations_2022.csv` schema exactly plus CEC-only columns
(status, CPUC cross-references, `cec_resolve_area`, urban/rural, imagery-verified).

**Basin results are kept and reported separately** (parallel scripts and output folders;
`compare_cats_cec.py`, `compare_substations_cec.py`).

**Key validation findings:**

1. **CEC ≈ basin at the record level** — 4,247 shared HIFLD IDs, median coordinate shift
   **1.3 m**, only 2 moved > 1 km. CEC is an *extension* of basin (386 more rows), not a
   re-survey.
2. **CATS is fully contained in CEC** — all **3,171 of 3,171** CATS substation buses match
   a CEC record within 2 km (median ≈0 m). CEC can replace basin as CATS's coordinate
   reference with zero coverage loss, adding 915 CEC substations CATS does not model.
3. **CEC recovers 2 of the 12 previously un-locatable substations** (SCE Topanga, Paularino).
4. **Utility-vs-CEC coordinate agreement** — PGE/SCE near-perfect (median 34 m / 58 m).
   **SDGE is the outlier** (median 1.33 km, 41/65 pairs > 1 km, up to ~9.7 km) — root
   cause is that `sdge_scraper.py` uses **polygon centroids** (`use_centroid=True`),
   while SDGE substations are area polygons, so the centroid sits away from CEC/HIFLD's
   point. PGE/SCE publish point coords.

**CEC name dictionary** (`build_cec_name_dictionary.py` → `data/cecSourceDictionary.csv`):
the CEC analogue of the basin dict. Because CEC inherited basin's naming, 70 of the basin
dict's 79 targets exist verbatim in CEC. Four tiers: *basin_reuse* (transferable entries),
*name_auto* (strips CEC's systematic " - (OWNER)" suffix via `norm_base()` — the reliable
signal for SDGE centroids), *spatial_auto* (≤0.25 km), *name_auto_assumed* (rescues exact
name matches whose only CEC hit has an unconfirmed "Other (PGE - Assumed)" owner tag).
With the dictionary, the **CEC cross-reference rate** is **PGE 666/670, SCE 559/578, SDGE
90/99** (vs basin's 605/527/96); aggregate **1,315 vs basin's 1,228 (+87)**.

> **This is a cross-reference/enrichment rate, NOT coordinate availability — do not read
> "666/670" as "4 PGE substations lack a location."** Every scraped substation already
> carries the utility's own coordinate; basin/CEC are fallbacks + cross-checks. Of 1,347
> scraped substations, **1,346 have a coordinate and only 1 (SCE `Autobody`, which has no load
> data) lacks any** after the 2026-08-17 hand-researched overrides — CEC
> recovers 2. The match's value is validating the utility coordinate and pulling CEC
> attributes.

CEC **is** wired in for voltage (`highside_kv`, PGE's only source) — see
[nodal_mapping.md](nodal_mapping.md) → "Voltage-aware assignment". Remaining unresolved
names → `data/checks/find_cec_name_candidates/cec_candidates_{util}.csv` (with an
`already_in_dict` flag); `map_review_candidates.py` renders an interactive review map.

## CEC coverage audit — what we scraped vs what CEC lists

`audit_substation_coverage.py` (→ `data/checks/substation_coverage_audit/`) separates
three easily-conflated questions:

1. **Coordinates** — 1,346 / 1,347 scraped substations have one; only SCE `Autobody` lacks one
   (and it has no load data). See `data/substationCoordinateOverrides.csv` and
   [nodal_mapping.md](nodal_mapping.md) for the hand-researched set and its provenance.
2. **CEC cross-reference** — the 666 / 559 / 90 name-match counts.
3. **Reverse gap** (`cec_unscraped_{util}.csv`) — CEC is a *location* inventory of every
   substation; our scrape is a *load* inventory of only those a utility publishes
   profiles for, so CEC lists far more IOU substations:

| Utility | CEC records | load-eligible (`SUBSTATION`) | line structures (`TAP`/`RISER`/`DEAD END`) | unmatched to our scrape |
|---------|-------------|------------------------------|---------------------------------------------|--------------------------|
| PGE | 2,246 | 1,728 | 476 | 1,579 (1,061 substations · 476 structures) |
| SCE | 1,598 | 1,269 | 301 | 1,038 (709 substations · 301 structures) |
| SDGE | 366 | 337 | 25 | 276 (247 substations · 25 structures) |

`type` is the primary "carries load" filter — **`TAP`/`RISER`/`DEAD END` are line
structures with no load** and must be excluded from any projection-target set. Among
`SUBSTATION`-type records, `max_voltage_kv` is a secondary filter (500 kV = bulk
transmission, no retail load). The files sort load-eligible unmatched records first — the
genuine expansion candidates for widening substation coverage.

```bash
python scripts/data/substations/process_substations_cec.py
python scripts/data/compare_cats_cec.py
python scripts/data/substations/compare_substations_cec.py
python scripts/data/substations/build_cec_name_dictionary.py
python scripts/data/substations/audit_substation_coverage.py
```

## SMUD substations (POI capacity heatmap)

SMUD is a municipal utility (a known Sacramento coverage gap).
`scrape_smud_heatmap.py` pulls the SMUD POI capacity heatmap (a PowerGEM React/Leaflet
app backed by static JSON) into `data/raw/smud/smud_heatmap_substations.csv`. This is a
**transmission-level** POI map: **13** major SMUD substations (11 × 230 kV, 2 × 115 kV),
not the full distribution network, each carrying per-scenario interconnection capacity
(MW) for four study cases. The JSON `lat`/`lon` are PSS/E model coordinates; the scraper
re-derives a 2-D affine to WGS84 from 8 unambiguous 230 kV CEC anchors (residual median
~0.03 km) and applies it to all 13.

A statewide analogue — the **CAISO POI heatmap** (`scripts/data/caiso/scrape_caiso_heatmap.py`
→ `data/raw/caiso/caiso_heatmap_substations.csv`, 829 buses) — uses the same app but fits
its own statewide affine from 384 CEC-matched anchors (median residual ~4.4 km).

```bash
python scripts/data/smud/scrape_smud_heatmap.py
python scripts/data/caiso/scrape_caiso_heatmap.py
```

## ReEDS projected & historic load (NREL)

ReEDS is NREL's long-term US capacity-planning model. This project uses a pre-transformed
parquet from a run under the **IRA_low** scenario.

- **Projected:** `data/raw/reeds/reeds_load_transformed.parquet` (2020–2050).
- **Historic:** `data/raw/reeds/historic_post2015_load_hourly.h5` (2016–2023 actual;
  8 yr × 8,760 h, leap days excluded).

ReEDS divides California into four p-regions from `inputs/hierarchy.csv`:

| Region | ReEDS NERC | Description |
|--------|------------|-------------|
| p8  | WECC_NW | PacifiCorp West — CA slice only (~0.8 TWh/yr) |
| p9, p10, p11 | WECC_CA | California sub-regions |

> **Scope note — WECC_CA ≠ EIA CISO BA.** p9+p10+p11 annual load (~252–268 TWh actual)
> tracks the PUDL CA5 sum (BANC+CISO+IID+LDWP+TIDC), not EIA CISO alone (~218–224 TWh).
> **WECC_CA = all California BAs except PacifiCorp West.** Compare ReEDS p9–p11 to PUDL
> CA5 or EIA CAL, never to EIA CISO.

Raw parquet is long-format (`time_index` 1–8,760, `weather_year` one of 2007–2013, `year`
2020–2050). ReEDS uses CST (UTC−6, no DST); `process_reeds.py` converts to fixed PST.
Historic annual totals (WECC_CA p9–p11): 2016 268.3, 2017 269.2, 2018 267.3, 2019 262.5,
2020 261.9, 2021 259.5, 2022 264.1, 2023 251.8 TWh (CA total p8–p11 adds ~0.8 TWh).

## CEC IEPR forecasts

CEC publishes two Excel workbooks as part of the Integrated Energy Policy Report:
Baseline Demand Forecast (annual statewide) and Peak Demand Forecast (hourly peak by
planning area). **Manually downloaded** from
<https://www.energy.ca.gov/data-reports/reports/integrated-energy-policy-report-iepr/>
into `data/raw/iepr/`. No scraper; two files cover all years.
