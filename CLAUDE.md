# CLAUDE.md — California Hourly Disaggregation

## Project Goal
Disaggregate hourly electricity load at the substation level across California.
Primary utility data sources: PG&E, SCE, SDG&E (others may be added).
Cross-validated against EIA-930, IEPR, RESOLVE, and ReEDS statewide load forecasts.

**Audience:** This codebase supports a research paper intended for open-source publication
and validation by industry experts and academic researchers.  All data assertions,
filtering decisions, and methodology choices must be written so that an independent
researcher can reproduce and audit them without access to the original authors.
Use precise citations (file paths, column names, document section numbers, URLs) rather
than general statements.  If a fact cannot be independently verified, mark it explicitly
with `# VERIFIED: sanity check` and record the reasoning here — these are the only
claims that require author trust rather than independent verification.

---

## README Maintenance Rule

**Whenever CLAUDE.md is updated, README.md must also be updated to reflect the same facts.**
CLAUDE.md is the internal evidence ledger (citations, implementation notes, override log).
README.md is the public-facing documentation. They must stay in sync on:
- What each data source's columns mean (load definition, BTM treatment)
- Timezone and DST conventions for each source
- Filtering rules and what was removed
- SCE-specific deduplication rules

When adding or changing a fact in CLAUDE.md, locate the corresponding section in README.md
and update it. If no README section covers the fact yet, add one.

---

## Core Evidence Standard

**Every factual assertion in code, comments, and the README must be backed by a specific citation.**

Acceptable citations (in order of preference):
1. A file path + column name that structurally encodes the fact (e.g., a separate `Customer_PV` column proves the load column is pre-BTM-solar)
2. A structural data observation (repeated hour in Nov = DST confirmation; exactly 8760 rows/year = no leap day / no DST)
3. A specific config or methodology file with line reference
4. An EIA/CPUC/NREL source document with section/table number
5. **Sanity check override** — when no external citation exists, the project owner (user) may assert a fact as confirmed through data exploration. Tag these with `# VERIFIED: sanity check` in code and note them explicitly in this file (see each source section below). Only the project owner can authorize this override.

When writing or reviewing code: if you cannot name the evidence, add `# CITATION NEEDED` and flag it.

---

## Data Sources

### Utility Substation Profiles (PGE / SCE / SDGE)

**Raw location:** `data/raw/{utility}/`
**Processing:** `scripts/data/substations/process_substations_clean.py`
**Output:** `data/processed/substations/substation_load_profiles_clean.csv`

#### What min_load / max_load mean
These columns do NOT represent a single observed day. They represent the **10th/90th percentile
load** at each substation, computed by the utility from a non-public historical lookback window,
aggregated to (month, hour) bins. The lookback window and exact methodology are internal to each
utility and not publicly documented.

- `max_load` = ~90th-percentile load for that (month, hour) cell
- `min_load` = ~10th-percentile load for that (month, hour) cell

**Implication for analysis:** Never treat these as individual-day observations. Summing `max_load`
across substations gives a **coincident high-percentile envelope**, not an observed peak day.

#### SCE: use most-recent vintage per (substation, month, hour)

SCE publishes year-stamped profiles (currently 2017–2026). Each year-stamp represents a new
percentile snapshot computed from that utility's non-public lookback window at that point in time.
A substation appearing in multiple year-stamps has multiple independent p10/p90 snapshots for the
same (month, hour) cell; only the most recent one should be used.

**Key observations from data:**
- 652 of 709 unique substations appear in 2+ years — different years contain different (overlapping) sets
- 2026 only covers Jan–Apr; substations in 2026 automatically fall back to 2025 for May–Dec
- Early years (2017–2018) have 21 substations each with no overlap with other years; these are retained

**Rule:** For each `(substation, month, hour)` cell, keep the row with the highest `year`.
This gives full 12-month coverage per substation using the most recent vintage available.
Implemented as `groupby(["substation_name","month","hour"])["year"].idxmax()` in
`process_substations_clean.py` (applied before writing the processed CSV) and as a defensive
guard in `load_substation_coincident()` in `compare_substation_eia_iepr.py`.

PGE and SDGE publish no year stamp (`year = NaN`) — they are used as-is.

#### SCE data source preference

Two SCE data pipelines exist:
- **Bulk download** (`scripts/data/sce/ingest_sce_combined.py`) — official published data,
  extends through 2025–2026. **Preferred.**
- **Scraper** (`scripts/data/sce/scrape_sce.py`) — web scrape of per-substation pages;
  SCE is actively improving their website, so this scraper may break or lag. Use as fallback only.

Deduplication (in `process_substations_clean.py`) prefers bulk over scrape on matching
`(SUBSTATION, YEAR, MONTH, HOUR)` keys.

#### Timezone

| Assertion | Evidence |
|-----------|----------|
| Raw data = wall-clock Pacific (DST-aware) | Observed: November hours are repeated (2:00 AM appears twice); March hours are skipped (spring-forward gap) in raw SCE data |
| Cleaned output = Fixed PST (UTC-8, no DST) | Majority-month UTC offset rule applied in `process_substations_clean.py` line ~572: Oct–Mar → UTC-8 (PST), Apr–Sep → UTC-7 (PDT), then re-aligned to produce 8760 unique hours/year |

#### Filtering rules and citations

| Filter | Evidence |
|--------|----------|
| P.T. (pass-through) substations removed | P.T. flag in utility attribute files; confirmed by zero load-profile presence in T3 substation data for P.T. entries — they are switching nodes, not metered |
| SCE: P.T. removes 170 of 748 unique substations | Observed count from `process_substations_clean.py` output |
| PGE: "Redacted" substations retained (664 total) | PGE documentation states redaction applies to DG capacity columns only, not load profiles; 48 redacted substations have valid load data |
| SCE: T3 entries with null voltage_kv excluded | These are ICA deliverability-only nodes — confirmed by absence of any metered load profiles for these entries in the load data |
| SDGE: 8 failed scrapes excluded | Observed: 8 substation pages returned errors during scrape; no load data recoverable |
| SDGE: kW → MW conversion applied | `# VERIFIED: sanity check` — raw SDGE load values are ~1000× larger than expected MW range given SDGE system size (~5 GW peak). No public SDGE documentation explicitly states kW units, but the magnitude mismatch is unambiguous. Division by 1000 brings values in line with EIA CISO SDGE territory |
| Pacificorp excluded | No metered load profiles exist in the Pacificorp data obtained |

#### Coordinate sources (lat/lon)

- **SCE:** `sce_ica_layer_substations_alt.csv` first (735 substations, verified coords); fallback to scrape-row coords
- **Basin:** DataBasin CA Substations 2022; joined via exact name match, then `data/basinSourceDictionary.csv` dictionary, then nearest haversine distance for multi-match cases

---

### EIA-930 (via PUDL)

**Raw location:** `data/raw/eia/pudl/out_eia930__hourly_operations_CA8.parquet`
**Processing:** `scripts/data/eia/process_eia_pudl.py`

| Assertion | Evidence |
|-----------|----------|
| Timestamp = UTC, hour-beginning | PUDL column named `datetime_utc`; EIA-930 API documentation states UTC hour-beginning convention |
| `demand_mwh` = net of BTM solar | EIA-930 Form instructions: "demand" = load measured at balancing authority boundary meter; BTM generation is not visible to grid meters and is not subtracted by the utility before reporting |
| NEVP ≈ 0.4% California load | EIA Form 861 (2024 actuals), `scripts/data/eia/process_eia861.py` |
| PACW ≈ 4% California load | EIA Form 861 (2024 actuals), same source |
| EIA CAL region available 2019+ only | Observed: no CAL rows in PUDL data before 2019 |
| CA8 BAs = CISO, IID, LDWP, BANC, TIDC, WALC, NEVP, PACW | `src/data/eia/pudl_eia930.py`, constant `CA8` |

**PUDL column coalescing:** imputed > adjusted > reported (see `_OPS_COL_CANDIDATES` in `process_eia_pudl.py`). PUDL documentation: imputed columns fill gaps via linear interpolation.

---

### IEPR (CEC Integrated Energy Policy Report)

**Raw location:** `data/raw/iepr/{vintage_year}/`
**Processing:** `scripts/data/iepr/process_iepr.py`

| Assertion | Evidence |
|-----------|----------|
| Hour-ending PST (HOUR = 1..24) | CEC IEPR methodology document, Table 2; `HOUR` column max = 24 in all source files (hour-ending convention) |
| Fixed PST, no DST | `# VERIFIED: sanity check` — every utility-year in the hourly files contains exactly 8760 rows. DST-aware data would have 8759 rows in spring-forward years and 8761 in fall-back years. The uniformity of 8760 is dispositive |
| `BASELINE_CONSUMPTION` = gross load (pre-BTM-solar) | Structural: separate `BTM_PV` column exists in the same file with negative values. If BTM were already subtracted from BASELINE_CONSUMPTION, the BTM_PV column would be redundant. Column definition: UNADJUSTED + PUMPING + CLIMATE_CHANGE + EV loads + DATA_CENTER + OTHER |
| `BASELINE_NET_LOAD` = net of BTM | = BASELINE_CONSUMPTION + BTM_PV + BTM_STORAGE (BTM_PV is negative) |
| `MANAGED_NET_LOAD` = primary IEPR output | IEPR I&A Table 2 labels this "IEPR Total CAISO Load" |
| CAISO = PGE + SCE + SDGE + VEA exactly | CEC confirms identity; CAISO-level file excluded from processing to avoid double-counting |
| `hour` column = HOUR − 1 | Explicit derivation in `process_iepr.py` to align with substation hour-beginning 0–23 convention |

---

### RESOLVE (E3 / CPUC IRP)

**Raw location:** `data/raw/RESOLVE Code Base and Inputs/RESOLVE Code Base and Inputs/`
**Processing:** `scripts/data/resolve/process_resolve.py`

| Assertion | Evidence |
|-----------|----------|
| `profile_model_years` column = gross MW, pre-BTM solar | Structural: source file `data/profiles/loads/2024/PGE_Baseline.csv` contains both `profile_model_years` (load) AND `Customer_PV` (BTM solar, negative values). If BTM solar were already subtracted from the load column, a separate BTM column would serve no purpose. The presence of both columns proves they are independent |
| Fixed PST, hour-beginning, 8760 rows/year | Observed: each utility profile file has exactly 8760 rows per calendar year across all 23 years (2000–2022); no 8759/8761 anomalies; datetime column has no sub-hourly values |
| Annual targets in `interim/loads/` are in MWh | Column attribute = `annual_energy_forecast`; order of magnitude (~240 TWh for PGE+SCE+SDGE combined) consistent with known California demand |
| RESOLVE scales profiles to annual targets | RESOLVE source code and CPUC IRP documentation; identity: output_mw = profile_mw × (target_mwh / profile_annual_sum) |
| CA scope = PGE, SCE, SDGE, IID, LDWP, NCNC | Exactly these utility profile files exist under `data/profiles/loads/2024/`; no BANC, TIDC, NEVP, PACW, or WALC files exist |
| NCNC = Northern California Non-CAISO (TIDC + BANC) | RESOLVE I&A documentation; NCNC profile covers the non-CAISO northern CA footprint |

**BTM subtraction for net-load comparison** (in `compare_substation_eia_iepr.py`):
```
resolve_net_mw = demand_mw_2024scaled − weather_factor × planned_capacity_2024
```
- `weather_factor` from `data/profiles/pmax/2025/{UTIL}_Customer_PV.csv` (SAM simulation, 23 weather years 2000–2022)
- `planned_capacity_2024` from `data/interim/resources/{UTIL}_Customer_PV.csv` (PGE: 9,669 MW; SCE: 6,553 MW; SDGE: 2,463 MW)

---

### ReEDS (NREL — IRA_low scenario + Historic 2016–2023)

**Projected:** `data/raw/reeds/reeds_load_transformed.parquet` — `scripts/data/reeds/process_reeds.py`
**Historic:** `data/raw/PotentialData/historic_post2015_load_hourly.h5/historic_post2015_load_hourly.h5` — `scripts/data/reeds/process_historic_load.py`

| Assertion | Evidence |
|-----------|----------|
| California = p8 + p9 + p10 + p11 | `data/raw/PotentialData/ReEDS-2.0/inputs/hierarchy.csv` filtered on `st == "CA"` returns exactly {p8, p9, p10, p11} |
| p8 = WECC_NW (PacifiCorp West CA slice, ~0.8 TWh/yr) | hierarchy.csv `wecc_reg` column |
| p9/p10/p11 = WECC_CA (all CA except PACW) | hierarchy.csv `wecc_reg == WECC_CA`; empirically confirmed — annual load of p9-p11 tracks PUDL CA5 sum (~BANC+CISO+IID+LDWP+TIDC), not EIA CISO alone. Gap vs CISO ~40 TWh ≈ IID+LDWP+BANC+TIDC combined |
| Timezone = Etc/GMT+6 (CST, UTC-6), no DST | `config_base.json` line 7: `"output_timezone": "Etc/GMT+6"` (both projected and historic files use same timezone) |
| Hour-beginning convention | `hourlize/load.py` lines 312–324: fixed-offset tz_localize, no DST |
| No leap day (8760 h/year) | `hourlize/load.py` lines 334–341: first 8760 hours per weather year; confirmed for historic: 70080 rows / 8 years = 8760 exactly |
| time_index 1 = Dec 31 22:00 PST | CST (UTC-6) is 2h ahead of PST; `_REF_DATES` starts at `"2000-12-31 22:00"` |
| Scenario = IRA_low only | Parquet metadata scan (`scripts/explore_potential_data.py`) |
| Weather years = 2007–2013 | Parquet metadata |
| Historic date range = 2016–2023 | HDF5 index_0 first/last values: 2016-01-01T00:00-06:00 to 2023-12-31T23:00-06:00 |

---

## Time Zone Quick Reference

| Source | Timezone | DST | Convention | Primary Evidence |
|--------|----------|-----|------------|-----------------|
| EIA-930 | UTC | n/a | Hour-beginning | PUDL column `datetime_utc`; EIA API docs |
| IEPR | Fixed PST (UTC-8) | None | Hour-ending (HOUR 1–24) | 8760 rows/yr *(sanity check)* |
| RESOLVE | Fixed PST (UTC-8) | None | Hour-beginning | 8760 rows/yr; no repeated timestamps observed |
| Substation raw | Wall-clock Pacific | Yes | Hour-beginning | Repeated Nov 2:00 AM; missing Mar 2:00 AM observed in SCE raw |
| Substation clean | Fixed PST (UTC-8) | None | Hour-beginning | Majority-month rule in `process_substations_clean.py` line ~572 |
| ReEDS | CST (UTC-6, Etc/GMT+6) | None | Hour-beginning | `config_base.json` line 7; `hourlize/load.py` lines 312–324 |

All processed outputs used in comparison (IEPR, RESOLVE, substation clean, ReEDS after conversion) use **Fixed PST, hour-beginning, hours 0–23** as the canonical time representation.

---

## Adding a New Data Source

Document the following in the processing script docstring AND in this file:

1. **Load column meaning** — cite column structure (companion columns prove independence) or source documentation (section/table)
2. **Timezone** — cite config file, API docs, or observed artifact (repeated/skipped hours; row count per year)
3. **DST handling** — cite observed evidence (8760 vs 8759/8761 row count per year; timestamp duplicates)
4. **Filtering rationale** — cite what was observed or documented to justify each filter
5. **Geographic scope** — cite the file or table that identifies which regions belong to California
6. If no external citation exists: document as `# VERIFIED: sanity check` and record the reasoning here
