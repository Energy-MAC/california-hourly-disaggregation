# California Hourly Disaggregation

Compile publicly available data to support substation-level hourly load disaggregation
for California transmission studies.  The pipeline collects hourly load profiles and
physical attributes for substations served by the four major California investor-owned
utilities (IOUs), inter-BA interchange data from EIA 930, and statewide demand forecasts
from the California Energy Commission (CEC IEPR).

---

## Repository Structure

```
california-hourly-disaggregation/
├── data/
│   ├── raw/                   # Downloaded source data (gitignored)
│   │   ├── eia/               # EIA 930 interchange and region files
│   │   ├── iepr/              # CEC IEPR forecast workbooks (manual download)
│   │   ├── pge/               # PG&E ArcGIS feeder and substation files
│   │   ├── sce/               # SCE DRPEP bulk download and ArcGIS files
│   │   ├── sdge/              # SDG&E load profiles and substation attributes
│   │   ├── pacificorp/        # PacifiCorp substation and DER readiness files
│   │   ├── calpeco/           # CalPeco (Liberty Utilities) — no data yet
│   │   └── bves/              # BVES — no data yet
│   └── processed/
│       ├── substations/
│       │   ├── substation_locations.csv      # All utilities, one row per substation
│       │   └── substation_load_profiles.csv  # Hourly min/max load by substation
│       └── eia/
│           └── eia_interchange.csv           # Standardized BA interchange
├── notebooks/
│   ├── 01_eia_from_to_consistency.ipynb      # FROM vs TO cross-file consistency
│   └── 02_eia_region_vs_interchange.ipynb    # Region TI vs sum-of-BA interchange
├── scripts/
│   ├── data/                  # Scraping and processing — organised by source
│   │   ├── eia/               # EIA-930 scrape, PUDL ingest, and processing
│   │   ├── iepr/              # CEC IEPR forecast processing
│   │   ├── resolve/           # RESOLVE load-input processing
│   │   ├── pge/               # PG&E scraper
│   │   ├── sce/               # SCE scraper, ingest, and validation
│   │   ├── sdge/              # SDG&E scraper
│   │   ├── bves/              # BVES scraper (placeholder)
│   │   ├── calpeco/           # CalPeco scraper (placeholder)
│   │   ├── pacificorp/        # PacifiCorp scraper
│   │   └── substations/       # Substation processing, audit, and comparison
│   ├── compare_cal_region_sources.py   # EIA API CAL vs PUDL CA5 sum
│   ├── compare_eia_sources.py          # EIA API scrape vs PUDL nightly
│   ├── compare_iepr_eia.py             # IEPR projections vs EIA realized demand
│   ├── compare_resolve_iepr_eia.py     # RESOLVE vs IEPR vs EIA
│   └── compare_substation_eia_iepr.py  # Substation profiles vs EIA and IEPR
├── src/data/                  # Scraper and processing library modules
├── requirements.txt
└── README.md
```

---

## Setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

**EIA API key** — required only for `scripts/data/eia/scrape_eia.py`.
Get a free key at <https://www.eia.gov/opendata/> and add it to a `.env` file at the
repo root:

```
EIA_API_KEY=your_key_here
```

---

## Data Sources

### EIA 930 (Hourly Balancing Authority Operations)

EIA collects hourly self-reports from every U.S. balancing authority via Form EIA-930.
This project pulls two datasets for the eight California-adjacent BAs:

| BA code | Full name |
|---------|-----------|
| BANC | Balancing Authority of Northern California |
| CISO | California Independent System Operator |
| IID  | Imperial Irrigation District |
| LDWP | Los Angeles Dept. of Water and Power |
| NEVP | NV Energy |
| PACW | PacifiCorp West |
| TIDC | Turlock Irrigation District |
| WALC | Western Area Lower Colorado |

**Interchange** (`rto-interchange`) — hourly MWh flows between every tracked BA pair.
Scraped in two passes: flows *from* each of the eight BAs (all counterparts), and flows
*to* each of the eight BAs (all counterparts).

**Region** (`rto-region`) — hourly demand, net generation, total interchange, and
day-ahead forecast for the aggregate California (`CAL`) region.

API endpoint: `https://api.eia.gov/v2/electricity/rto/`

### Utility IOUs — Load Profiles

Hourly substation-level min/max load profiles (MW) for three utilities:

| Utility | Source | Access |
|---------|--------|--------|
| PG&E | DRP Compliance ArcGIS FeatureServer, layer 25 | Public ArcGIS REST API |
| SCE | DRPEP "Historical Substation Load Profiles" bulk download | Manual ZIP download from drpep.sce.com |
| SDG&E | Interactive map download API (ZIP per substation) | Public HTTP API |

PacifiCorp, CalPeco, BVEA, and MOUs do not publish comparable hourly load profiles.

### Utility IOUs — Substation Attributes

Physical and DER (distributed energy resource) attributes scraped from each utility's
public ArcGIS FeatureServer:

| Utility | Source layer | Key fields |
|---------|-------------|------------|
| PG&E | EDSubstations (layer 0) | voltage_kv, num_banks, existing/queued/total DG (kW→MW) |
| SCE | ICA Tables layer 3 (Table 3) | existing/queued/total gen, projected load, DER penetration, circuit voltage, customer mix |
| SDG&E | ICA_MAP_PROD_Substations_VW (layer 0) | substation type, voltage, existing/queued/total gen, projected load, DER penetration |
| PacifiCorp | DG Readiness with Net Minimum (layer 0) | existing DER (MW), net minimum daytime load (MW) — aggregated from circuit level |

### Substation Coverage Summary

After cleaning (removing pass-through switching nodes and failed scrapes) and joining
to the DataBasin CA Substations 2022 reference for geographic coordinates:

|                                      | PG&E    | SCE     | SDG&E  | Total     |
|--------------------------------------|---------|---------|--------|-----------|
| Raw substations scraped              | 664     | 748     | 107¹   | 1,519     |
| Removed (P.T. nodes / failed scrapes)| —       | 170     | 8      | 178       |
| **Cleaned (in processed output)**    | **664** | **578** | **99** | **1,341** |
| Matched to basin by name             | 550     | 518     | 87     | 1,155     |
| Added via name dictionary            | 50      | 9       | 9      | 68        |
| **Basin-matched total**              | **600** | **527** | **96** | **1,223** |
| Not matched to basin                 | 64      | 51      | 3      | 118       |
| Basin substations not in any source  | 346     | 160     | 42     | 548       |
| Load profile rows                    | 191,184 | 313,608 | 28,512 | 533,304   |

¹ SDG&E: 99 substations with data + 8 failed scrapes = 107 attempted.

The **name dictionary** (`data/basinSourceDictionary.csv`, 79 entries) maps utility
source names that differ from the DataBasin reference (e.g. "CRESTA PH" → "Cresta",
"DRUM" → "Drum 1 / Drum 2") to recover additional geolocation matches beyond the
normalised-name join.  SCE carries year-stamped profiles (2021–2026); PG&E and SDG&E
publish monthly aggregates without a year column.

### CEC IEPR Forecasts

The California Energy Commission publishes two Excel workbooks as part of the
Integrated Energy Policy Report (IEPR):

- **Baseline Demand Forecast** (`CED* Baseline Forecast - Total State.xlsx`) —
  annual statewide demand projections
- **Peak Demand Forecast** (`CED* Peak Forecast*.xlsx`) —
  hourly peak demand by planning area

These are **manually downloaded** from:
<https://www.energy.ca.gov/data-reports/reports/integrated-energy-policy-report-iepr/>

Place downloaded files in `data/raw/iepr/`.  No scraper is needed; two files cover
all years.

---

## Data Pipeline

### Step 1 — Scrape raw data

Each `scripts/data/<source>/scrape_*.py` command writes chunked CSVs to the corresponding
`data/raw/<utility>/` folder.  All scrapers support **safe stop/resume**: press
`Ctrl+C` at any time; re-run the same command to continue from where it left off.

#### EIA 930

```bash
# Interchange: flows FROM each of the 8 BAs (all counterparts)
python scripts/data/eia/scrape_eia.py rto-interchange \
    --from-bas BANC CISO IID LDWP PACW NEVP TIDC WALC

# Interchange: flows TO each of the 8 BAs (all counterparts)
python scripts/data/eia/scrape_eia.py rto-interchange \
    --to-bas BANC CISO IID LDWP PACW NEVP TIDC WALC

# CAL region aggregate (demand, net gen, TI, day-ahead forecast)
python scripts/data/eia/scrape_eia.py rto-region
```

Output: `data/raw/eia/eia_rto-interchange-data_from-*.csv` (×4 chunks),
`eia_rto-interchange-data_to-*.csv` (×4 chunks), `eia_rto-region-data_CAL_*.csv`

#### PG&E

```bash
# Feeder load profiles (layer 25) — primary load source
python scripts/data/pge/scrape_pge.py layer --layer-id 25

# Substation physical attributes (layer 0)
python scripts/data/pge/scrape_pge.py attributes
```

Output: `data/raw/pge/pge_layer25_*.csv`, `pge_substation_attributes.csv`

#### SCE

SCE load profiles are obtained via the DRPEP bulk download (not scraped automatically):

1. Go to <https://drpep.sce.com/drpep/>
2. Click **Bulk Download → Historical Substation Load Profiles → Download All**
3. Save the ZIP file, then run:

```bash
python scripts/data/sce/ingest_sce_bulk_download.py path/to/SUBSTATION.zip
```

ArcGIS data (coordinates + substation attributes) is scraped programmatically:

```bash
# Substation load profile layer (layer 2) — coordinates only, values are in Amps
python scripts/data/sce/scrape_sce.py layer --layer-id 2

# Substation physical attributes (ICA Table 3)
python scripts/data/sce/scrape_sce.py attributes
```

Output: `data/raw/sce/sce_bulk_download_all.csv`, `sce_layer2_*.csv`,
`sce_substation_attributes.csv`

> **Note on SCE units**: The ArcGIS layer 2 returns load values in **Amps**, not MW.
> The DRPEP bulk download returns MW directly and is the authoritative load source.
> The ArcGIS layer is retained only for substation coordinates.

#### SDG&E

```bash
# Hourly load profiles (ZIP download per substation)
python scripts/data/sdge/scrape_sdge.py substation-profiles

# Substation physical attributes
python scripts/data/sdge/scrape_sdge.py attributes
```

Output: `data/raw/sdge/sdge_substation_profiles_part*.csv`,
`sdge_substation_attributes.csv`, `sdge_substation_profiles_failed.csv`
(substations with no published data receive a graceful failure entry)

#### PacifiCorp

```bash
# Substation names and coordinates (layer 1)
python scripts/data/pacificorp/scrape_pacificorp.py layer --layer-id 1

# DER attributes from DG Readiness service (circuit-level, aggregated to substation)
python scripts/data/pacificorp/scrape_pacificorp.py attributes
```

Output: `data/raw/pacificorp/pacificorp_layer1_*.csv`,
`pacificorp_substation_attributes.csv`

#### EIA Form 861 (Annual Retail Sales)

Two options for downloading EIA Form 861 data (choose one):

**Option A — PUDL (recommended):** Downloads only the sales table for the CA8 BAs as
a compact parquet file.  No large ZIP files; fastest way to get the data.

```bash
python scripts/data/eia/ingest_eia861_pudl.py          # → data/raw/eia/pudl/core_eia861__yearly_sales_CA8.parquet
```

**Option B — Direct from EIA:** Downloads the full Form 861 ZIP and extracts only
`Sales_Ult_Cust_{year}.xlsx` (all other worksheets discarded).

```bash
python scripts/data/eia/scrape_eia_form861.py --years 2022 2023 2024   # → data/raw/eia/form861/{year}/
```

#### CalPeco / BVES

No public data source has been identified for either utility.  Placeholder scripts
(`scrape_calpeco.py`, `scrape_bves.py`) and empty raw data directories are in place
for future work.

---

### Step 2 — Process into unified outputs

#### Substation tables

```bash
python scripts/data/substations/process_substations.py
```

Reads all raw utility files and writes two CSVs to `data/processed/substations/`:

**`substation_locations.csv`** — one row per substation (2,614 total across all utilities)

| Column | Description |
|--------|-------------|
| utility | `pge`, `sce`, `sdge`, or `pacificorp` |
| substation_name | Name as reported by the utility |
| latitude, longitude | WGS84 coordinates |
| voltage_kv | Primary bus voltage (kV) |
| substation_type | E.g. "Distribution", "Transmission" (SDG&E/SCE) |
| sys_name | SCE system/circuit area name |
| division | PG&E service division (e.g. "Kern", "Bay") |
| subst_id | Internal substation ID (PG&E, SCE) |
| existing_gen | Existing DER/generation capacity (MW) |
| queued_gen | Queued interconnection capacity (MW) |
| total_gen | Existing + queued (MW) |
| projected_load | Projected peak load (MW) |
| der_penetration | DER as % of projected load |
| max_remain_cap | Maximum remaining hosting capacity (MW) |
| circuit_count | Number of distribution circuits (PG&E = transformer banks) |
| res/com/agr/ind/other_pct | Customer-class share of circuits (SCE) |
| res/com/agr/ind/other_total | Customer-class circuit count (SCE) |
| note_sub | Data quality flag (PG&E `REDACTED` field) |
| existing_der | PacifiCorp: existing DER across circuits (MW) |
| net_min_daytime_load_mw | PacifiCorp: net minimum daytime load (MW) |

**`substation_load_profiles.csv`** — hourly min/max load by substation (455,568 rows)

| Column | Description |
|--------|-------------|
| utility | Source utility |
| substation_name | Matches `substation_locations.csv` |
| latitude, longitude | Coordinates |
| year | Calendar year (NaN for PG&E, which publishes monthly aggregates without year) |
| month | 1–12 |
| hour | 0–23 |
| min_load | Minimum load observed in that month/hour slot (MW) |
| max_load | Maximum load observed in that month/hour slot (MW) |

#### EIA interchange

```bash
python scripts/data/eia/process_eia_interchange.py
```

Standardizes all FROM and TO interchange files into a single canonical table
(`data/processed/eia/eia_interchange.csv`, ~3.1M rows):

| Column | Description |
|--------|-------------|
| period | Hourly timestamp, e.g. `2023-06-15T14` |
| fromba | One of the 8 CA balancing authorities |
| fromba-name | Full BA name |
| toba | Counterpart BA (any, including external) |
| toba-name | Full counterpart name |
| value | MWh net exported from `fromba` to `toba` (positive = export, negative = import) |
| value-units | `megawatthours` |

**Transformation rules applied:**
- *FROM records* (fromba ∈ CA-8): kept as-is
- *TO records where fromba ∉ CA-8*: sign-flipped and fromba/toba swapped so the
  CA BA is always `fromba`
- *TO records where fromba ∈ CA-8*: duplicate of FROM record — dropped
- Both files are trimmed to the earlier endpoint before processing to avoid
  asymmetric coverage

#### RESOLVE load inputs

```bash
python scripts/data/resolve/process_resolve.py
```

Reads RESOLVE's hourly load shape profiles and annual energy forecasts from
`data/raw/RESOLVE Code Base and Inputs/` and writes two CSVs to `data/processed/resolve/`:

**`resolve_hourly_profiles.csv`** — hourly load shapes for six California BA zones (PGE, SCE, SDGE, IID, LDWP, NCNC), covering 23 historical weather years (2000–2022) at 8,760 h/year (no Feb 29)

| Column | Description |
|--------|-------------|
| datetime_pst | Hourly timestamp (fixed PST, UTC−8, no DST) |
| utility | BA zone label: PGE, SCE, SDGE, IID, LDWP, or NCNC |
| demand_mw_raw | Raw shape profile value (MW) from `profiles/loads/2024/{UTIL}_Baseline.csv` |
| demand_mw_2024scaled | `demand_mw_raw` scaled so each weather year integrates to the 2024 IEPR annual energy forecast target (MWh) for that utility |

> **Important:** `demand_mw_2024scaled` is **gross demand** — BTM solar has been moved to
> the supply side in RESOLVE's model (see "RESOLVE Net Load" section below).  Use
> `demand_mw_raw` / `demand_mw_2024scaled` only for gross-load comparisons.  For
> net-load comparisons, `compare_substation_eia_iepr.py` applies the native
> Customer_PV correction automatically.

**`resolve_annual_forecast.csv`** — annual energy forecast targets (MWh and TWh) by utility and year (2024–2045), from IEPR interim load files used as RESOLVE scaling targets.

#### EIA Form 861 — CA fractions by BA

```bash
python scripts/data/eia/process_eia861.py          # auto-detects PUDL parquet or EIA Excel
python scripts/data/eia/process_eia861.py --source pudl
python scripts/data/eia/process_eia861.py --source eia --years 2022 2023 2024
```

Reads from either the PUDL parquet (`data/raw/eia/pudl/core_eia861__yearly_sales_CA8.parquet`)
or the direct EIA Excel files (`data/raw/eia/form861/{year}/Sales_Ult_Cust_{year}.xlsx`),
then writes `data/processed/eia/eia861_ca_fractions.csv`:

| Column | Description |
|--------|-------------|
| year | Report year |
| ba_code | Balancing authority code (e.g. `NEVP`, `WALC`) |
| total_mwh | Total retail sales across all states served by this BA |
| ca_mwh | Retail sales to California customers only |
| ca_fraction | `ca_mwh / total_mwh` — fraction of this BA's load in CA |

---

### Step 3 — Validate and audit

```bash
# Check SCE data for schema consistency, row-count completeness, and duplicate hours
python scripts/data/sce/validate_sce.py

# Report which raw columns are unused in the processed output
python scripts/data/substations/audit_unused_columns.py
```

---

## Notebooks

| Notebook | Purpose |
|----------|---------|
| `01_eia_from_to_consistency.ipynb` | Cross-file misreporting check: for each flow `A→B` in FROM, does the paired `B→A` in TO agree? Identifies BA pairs and time periods with the largest discrepancies. |
| `02_eia_region_vs_interchange.ipynb` | Compares the EIA CAL region total interchange (type=TI) against the sum computed from individual BA interchange flows.  Quantifies the systematic gap and identifies its largest contributors. |

---

## Notes on Data Quality

- **EIA FROM vs TO**: The FROM and TO files contain identical values for the same
  `(period, fromba, toba)` row — EIA does not collect measurements from each endpoint
  independently.  True reporting discrepancies are visible only by comparing `A→B` in
  FROM with `B→A` in TO (~13% of inter-CA-8 pairs differ by >1 MWh).

- **PacifiCorp coverage**: The DG Readiness service covers only Pacific Power's
  distribution territory.  Only ~168 of 1,142 scraped substations have DER attribute
  data; the rest have latitude/longitude and substation name only.

- **SCE unit discrepancy**: SCE's ArcGIS layer 2 returns Amps; the DRPEP bulk download
  returns MW.  A direct Amps→MW conversion using nominal voltage was tested and found
  inaccurate.  All SCE load values in the processed output come exclusively from the
  DRPEP bulk download.

- **PG&E years**: PG&E's published feeder profiles are monthly aggregates without a
  year column.  The `year` field is `NaN` for all PG&E rows in `substation_load_profiles.csv`.

---

## RESOLVE and Statewide Load Forecast Sources

This project compares substation-level profiles against four statewide demand sources.
The sections below document how each source handles behind-the-meter (BTM) solar,
why RESOLVE and IEPR differ numerically, and which values are raw vs derived.

### RESOLVE

RESOLVE (the E3/CPUC Integrated Resource Planning model) is the statewide optimization
model used by CPUC for the 2024-2026 IRP.  Its raw load inputs sit in
`data/raw/RESOLVE Code Base and Inputs/`.  Processed outputs are in
`data/processed/resolve/`.

RESOLVE covers six California BA zones: **PGE**, **SCE**, **SDGE**, **IID**, **LDWP**,
**NCNC**.  It does *not* model NEVP or PACW as California zones (see EIA CA8 note below).

#### RESOLVE Net Load: gross → net derivation

`resolve_hourly_profiles.csv` stores **gross demand** (`demand_mw_2024scaled`) — the
demand side from which BTM solar has been removed because RESOLVE models rooftop PV as
a supply-side resource named `Customer_PV`.  To compare RESOLVE against EIA-930 or IEPR
(both of which measure or forecast **net-of-BTM** load), the Customer_PV output must be
subtracted:

```
resolve_net_mw = demand_mw_2024scaled − (weather_factor × planned_capacity_2024)
```

`compare_substation_eia_iepr.py` applies this correction automatically via the
`_load_resolve_customer_pv_native()` helper, which loads:

| File | Contents |
|------|----------|
| `RESOLVE_RAW/data/profiles/pmax/2025/{UTIL}_Customer_PV.csv` | Hourly solar capacity factor (`Weather Factor`, 0–1) for each BA zone across 23 actual weather years (2000–2022). These are real SAM simulation outputs; the profile varies day to day with cloud cover and irradiance. 201,480 rows per utility (23 × 8,760 h, no Feb 29). |
| `RESOLVE_RAW/data/interim/resources/{UTIL}_Customer_PV.csv` | Planned BTM PV installed capacity (MW) by year and scenario. For the 2024 comparison, `2024_IEPR_Local_Reliability` values are used: PGE 9,669 MW · SCE 6,553 MW · SDGE 2,463 MW. |

The two approaches (native RESOLVE vs IEPR fixed template) produce nearly the same
**mean** correction (~13 GW at July noon for PGE+SCE+SDGE), but the native RESOLVE
profiles add realistic day-to-day solar variability — cloud-cover days reduce BTM output
below the monthly average, rainy winter days near zero — that the IEPR fixed monthly
template cannot capture.  This widens the inter-annual p10–p90 band in the monthly
profile figures compared to using the IEPR template.

---

### BTM Solar Treatment by Source

The most important difference between sources is how they handle rooftop solar (BTM PV).
BTM generation reduces the net demand visible to the grid meter, so a source that
subtracts it will always read lower than one that does not.

| Source | BTM Solar Treatment | Load Metric | Raw vs Derived | ~2024 CA Annual |
|--------|---------------------|-------------|----------------|-----------------|
| **EIA-930 (CISO)** | **Net-of-BTM** — rooftop generation reduces the metered demand the grid sees | Net demand at CAISO system boundary | Raw (hourly self-reports from BA) | ~224 TWh |
| **EIA-930 (CA8 group)** | Net-of-BTM | Sum of 8 BAs: CISO + BANC + IID + LDWP + TIDC + WALC + **NEVP + PACW** | Raw, but inflated by ~55–60 TWh of non-CA load (only ~1 TWh of NEVP+PACW is actually in CA) | ~285 TWh (overestimates CA) |
| **EIA CAL region** | Net-of-BTM | Geographic California boundary; NEVP/PACW excluded | Raw | ~270–273 TWh |
| **IEPR `BASELINE_CONSUMPTION`** | **Gross (BTM PV not yet subtracted)** — includes EV charging, data centers, climate already embedded | Gross load at grid busbar; comparable to RESOLVE Baseline | Raw from CEC hourly workbooks (`iepr_hourly_forecast.csv`) | ~247–250 TWh |
| **IEPR `BASELINE_NET_LOAD`** | **Net-of-BTM** — `BASELINE_CONSUMPTION` − BTM\_PV − BTM\_STORAGE | Net system load, same concept as EIA-930 | Raw from CEC hourly workbooks | ~217–220 TWh |
| **IEPR `MANAGED_NET_LOAD`** | **Net-of-BTM + all scenario overlays applied** — AAEE, AAFS, AATE adjustments | Final scenario net load ("IEPR Total CAISO Load" in RESOLVE I&A) | Raw from CEC hourly workbooks | ~217–220 TWh |
| **RESOLVE Baseline Consumption** (`demand_mw_2024scaled` in `resolve_hourly_profiles.csv`) | **Gross (BTM PV removed from demand side, modeled as supply)** | Gross demand before BTM PV subtraction; includes T&D losses | **Derived** from IEPR MANAGED_NET_LOAD — see formula below | ~241 TWh (PGE+SCE+SDGE only) |
| **RESOLVE Net Load** (derived in `compare_substation_eia_iepr.py`) | **Net-of-BTM** — RESOLVE's own weather-year `Customer_PV` profiles subtracted from Baseline; see "RESOLVE Net Load" section above | Net system load for peak-hour comparisons against EIA/IEPR; 23-year ensemble captures real day-to-day solar variability | `demand_mw_2024scaled − weather_factor × planned_capacity_2024` using native RESOLVE pmax profiles | ~221 TWh mean across 23 weather years (PGE+SCE+SDGE) |

**Key implication for comparisons:** A direct TWh comparison between RESOLVE Baseline
and EIA-930 CISO will show an apparent ~17–20 TWh gap in 2024.  The true sources of
that gap are:

1. **BTM PV (~30 TWh statewide in 2025)** — RESOLVE adds it back; EIA/IEPR subtract it.
   PGE+SCE+SDGE share of statewide BTM PV is roughly ~17–18 TWh, explaining most of the gap.
2. **Geographic scope** — RESOLVE covers PGE+SCE+SDGE+IID+LDWP+NCNC; EIA CISO covers
   only the CAISO footprint (PGE+SCE+SDGE, plus some BANC/TIDC slivers).
3. **T&D losses** — RESOLVE and IEPR express demand at the generator busbar using a 7.97%
   gross-up; EIA-930 measures at the BA boundary.

---

### RESOLVE vs IEPR: Modeling Framework Differences

RESOLVE is not an independent demand forecast — it uses IEPR as its load input and
transforms it for use in a resource optimization.  The key differences are:

#### Load definition

| Dimension | IEPR | RESOLVE |
|-----------|------|---------|
| What is reported | "Total CAISO Load" = customer demand + T&D losses, net of BTM generation | "Baseline Consumption" = IEPR with overlays stripped out and BTM PV added back |
| BTM PV | Subtracted from demand (reduces visible load) | Modeled as a supply-side resource (ELCC-weighted); removed from demand side |
| BTM Storage | Treated as demand reduction | Modeled explicitly; net losses added to demand |
| EV load | Included in scenario totals | Modeled as an additive overlay (AATE_LDVs, AATE_MHDVs) |
| Building electrification (AAFS) | Included in scenario totals | Modeled as AAFS overlay |
| Energy efficiency (AAEE) | Included in scenario totals | Modeled as AAEE overlay (demand reduction) |
| Data Centers | Included in scenario totals | Modeled as a separate overlay |
| Climate impacts | Included in scenario totals | Modeled as a Climate Impacts overlay |

#### Resource adequacy and planning reserve

RESOLVE uses **Perfect Capacity (PCAP)** planning reserve margins where every resource
is counted at its Effective Load Carrying Capability (ELCC) — not its nameplate capacity.
IEPR does not model resource adequacy.

PRM targets used in the 2024-2026 IRP (from I&A, Section 3):

| Year | PCAP PRM |
|------|----------|
| 2026 | 15.6% |
| 2030 | 14.5% |
| 2035 | 14.9% |
| 2040 | 14.1% |

#### ELCC treatment

RESOLVE computes ELCC from a 3-D surface (solar × 4-hr battery × 8-hr battery penetration)
across **23 weather years (2000–2022)** compressed to **36 representative days** via affinity
propagation clustering.  Wind resources use separate 1-D penetration curves (in-state,
out-of-state, offshore).

#### Geographic zones

| Zone | RESOLVE label | IOU/BA covered |
|------|--------------|----------------|
| California CAISO | PGE, SCE, SDGE | PG&E, SCE, SDG&E |
| Non-CAISO California | IID, LDWP, NCNC | Imperial ID, LADWP, northern co-ops |
| Pacific Northwest (out-of-state) | NW | BPAT, PACW, PortlandGE |
| Desert Southwest (out-of-state) | SW | AZPS, NEVP, SRP, WALC |

Neither NEVP nor PACW is treated as a California zone in RESOLVE.

#### Summary of 2024-2026 IRP cycle changes (vs prior IRP)

The February 2026 Inputs & Assumptions document highlights several changes from the prior
IRP cycle:

- **Updated 23-year weather record** (2000–2022) used for load shapes and ELCC.
- **New Data Center overlay** added explicitly — data center growth is now a separate
  demand modifier rather than embedded in baseline.
- **Revised BTM PV trajectory** — higher near-term adoption driven by updated CEC
  Behind-the-Meter solar forecast.
- **ELCC surface recalibration** — updated to 2024 grid conditions (higher solar and
  battery penetration shift the marginal ELCC curves).
- **Climate Impacts overlay added** — a new modifier for incremental demand growth due
  to warming temperatures, separate from IEPR baseline.
- **MHD Vehicle EV overlay added** — medium- and heavy-duty EV charging is now tracked
  separately from light-duty.

---

### RESOLVE Baseline + Overlays = IEPR (Mathematical Verification)

The RESOLVE "Baseline Consumption" is derived from the IEPR Total CAISO Load by removing
all demand-side modifiers that RESOLVE models explicitly, and adding BTM PV back so it
can be treated as a supply resource.  Reversing the transformation reconstructs IEPR.

From Table 2 of the CPUC 2024-2026 IRP Inputs & Assumptions (February 2026), using 2025
projected values (GWh):

```
IEPR Total CAISO Load                  217,688
  − Light-Duty Vehicle EVs             −  3,024
  − Med/Heavy Duty Vehicle EVs         −    717
  − AAFS (Building Electrification)    −    391
  + AAEE (Energy Efficiency)           +  3,110   ← demand *reduction* so we add it back
  − Data Centers                       −  2,149
  − Climate Impacts                    −    213
  + Behind-the-Meter PV               + 30,154   ← subtracted from IEPR, so add back
  − BTM Storage Net Losses             −     72
  ────────────────────────────────────────────
  = Baseline Consumption               244,386
```

Rearranged, **RESOLVE Baseline + all overlays ≈ IEPR Total CAISO Load**:

```
IEPR Total CAISO Load  =  Baseline Consumption
                          + LDV EVs  + MHD EVs  + AAFS  + Data Centers  + Climate
                          − AAEE
                          − BTM PV   + BTM Storage Losses
```

This identity holds by construction — RESOLVE's Baseline is derived *from* IEPR, and
running RESOLVE to equilibrium (optimizing overlays against Baseline) reconstructs IEPR
net demand.  The relationship is definitional, not empirical.

**Note on BTM PV sign convention:** BTM PV *reduces* IEPR Total CAISO Load (customers
generate their own electricity, reducing grid demand).  RESOLVE adds it back to Baseline
because it models rooftop solar as a supply-side resource — the demand side "sees" gross
consumption, and BTM PV generation is subtracted on the supply side by assigning it a
capacity credit via ELCC.

---

### EIA CA8 Group: California Fractions by Balancing Authority

EIA-930 defines a "California" (CA8) group of 8 balancing authorities for regional
reporting, inherited from WECC planning conventions.  Three of these BAs serve
significant out-of-state territory, inflating the CA8 total relative to actual
California demand.  The table below shows retail sales fractions from EIA Form 861
(Annual Electric Power Industry Report), which provides state-level sales by BA.

| BA | Primary service territory | 2024 CA % | 2024 CA load | 2024 total load |
|----|--------------------------|-----------|--------------|-----------------|
| BANC | Northern California co-ops and munis | **100%** | 15.8 TWh | 15.8 TWh |
| CISO | PG&E + SCE + SDG&E footprint | **~100%** | 285.3 TWh | 285.3 TWh |
| IID | Imperial Irrigation District (SE California) | **100%** | 3.7 TWh | 3.7 TWh |
| LDWP | Los Angeles Dept. of Water and Power | **100%** | 23.4 TWh | 23.4 TWh |
| TIDC | Turlock Irrigation District (San Joaquin Valley) | **100%** | 2.3 TWh | 2.3 TWh |
| WALC | Western Area Lower Colorado (AZ/NV + southern CA) | **31%** | 3.8 TWh | 12.2 TWh |
| PACW | PacifiCorp West (OR/WA/ID/UT + far-northern CA) | **4%** | 0.85 TWh | 21.2 TWh |
| NEVP | NV Energy (primarily Nevada / Las Vegas) | **0.4%** | 0.18 TWh | 47.2 TWh |

Note: Form 861 measures retail sales (MWh billed to customers), not EIA-930 system
demand.  The fractions are used to partition EIA-930 hourly BA demand into in-CA vs
out-of-CA portions; the total CA8 retail-sales overstatement (~76 TWh in 2024) will
differ slightly from the EIA-930 demand overstatement.

**CA8 vs actual California (2024 retail sales):**
- CA8 group total: **411 TWh** across all 8 BAs
- Actually in California: **335 TWh**
- Out-of-CA inflation: **~76 TWh** — driven by NEVP (47 TWh), PACW (20 TWh), WALC (8 TWh)

**Source:** EIA Form 861 state-level retail sales, processed with
`scripts/data/eia/process_eia861.py` (PUDL or direct EIA source; see pipeline below).

**NEVP and PACW in RESOLVE:** CPUC 2024-2026 IRP Inputs & Assumptions (February 2026),
Table 99 confirms NEVP is in RESOLVE's SW zone and PACW is in RESOLVE's NW zone —
neither is classified as California.  Only ~1.03 TWh combined of their total retail
sales (~68 TWh) is actually in California.

**Practical implication:** When comparing annual totals across sources, use EIA CISO
(~224 TWh for 2024–2025) or EIA CAL (~270–273 TWh) rather than EIA CA8 (~285 TWh demand /
~411 TWh retail sales).  RESOLVE's PGE+SCE+SDGE total (~241 TWh gross, ~211 TWh net
of BTM PV) is the most directly comparable forecast for the CAISO footprint.

---

## Peak Hour Alignment: Reconciling Three Measures of IEPR vs EIA

Three different analyses in this repository measure the alignment between IEPR
projected peak hours and EIA-930 realized peak hours.  They report different
numbers because they measure fundamentally different things.

| Measure | Script | IEPR data used | EIA data used | Result | What this means |
|---------|--------|----------------|---------------|--------|-----------------|
| **fig4 daily offset** | `compare_iepr_eia.py` | Projected years 2024–2025 only, inner-joined to the **same realized calendar date** | Realized 2024–2025, matched by date | ~0h overall | Near-term IEPR projections agree with realized EIA day-by-day. Both datasets reflect the current (2024–2025) electricity system and peak in the same evening hours. |
| **Mean-profile argmax** | `compare_substation_eia_iepr.py` | Representative year per vintage (2024 or 2025); **argmax of mean monthly profile** | Realized 2016–2025; argmax of mean profile | −1.91h overall | Comparing the peak of the average load shape, not individual days. EIA's mean profile peaks slightly later due to the growing BTM solar duck curve shifting the evening ramp. |
| **Daily argmax distributions** | `compare_substation_eia_iepr.py` | **All** projected years 2024–2050 pooled; argmax per individual day | Realized 2016–2025; argmax per individual day | −2.16h overall; **−5 to −6h in winter** | Reveals that the aggregate shift masks a stark seasonal pattern: IEPR's long-range scenarios (2026–2050) project winter daily peaks clustering around noon, while EIA's realized winter peaks are at 6–7 PM. RESOLVE (23 weather years) is a much closer match to EIA in winter. |

### Why fig4 and the daily distributions appear to contradict each other

They do not contradict each other — they compare different populations:

- **fig4** restricts to *projected years where EIA realized data exists* (2024 and 2025).
  For those near-term years, IEPR's calibrated load shapes still closely match the current
  system.  Both IEPR and EIA peak in the evening, so the offset is near zero.

- **Daily distributions (left panel of `daily_peak_shift_significance_table.png`)** pool
  *all* projected years 2024–2050.  The majority of IEPR data is from 2026–2050 long-range
  scenarios where IEPR projects a fundamentally different winter daily load shape: as BTM
  solar grows and electrification increases, morning peaks (before solar generation) compete
  with evening peaks, and a growing fraction of IEPR winter days have their daily maximum
  in the late morning rather than the evening.  The mean of a bimodal morning/evening
  distribution lands near noon, pulling the monthly average far from EIA's consistently
  evening-peaked realized data.

- **Verification (center panel of `daily_peak_shift_significance_table.png`)** replicates
  the year range from fig4 as a distributional comparison (no date-matching).  If
  near-zero shifts appear here, it confirms that fig4 and the daily-distributions analysis
  are consistent, and that the large shifts in the left panel are specifically driven by
  2026–2050 long-range projections rather than a data problem.

### RESOLVE as a reference

RESOLVE's 23 historical weather years (2000–2022) produce winter daily peaks at
~17–18h, within ~1h of EIA's ~18–19h — a much closer match than IEPR's long-range
projections.  Because RESOLVE is built from actual historical California load shapes,
it correctly captures the evening-dominant winter demand pattern.  For the summer
duck-curve months, RESOLVE runs 1–2h earlier than EIA, likely because the 2000–2022
historical load shapes carry an earlier evening ramp than the current (2024–2025) grid
— the duck curve has deepened over time as BTM solar installed capacity has grown well
beyond the 2024 levels used to scale the profiles.

**Key outputs** (in `data/figures/` and `data/tables/`):

| File | Description |
|------|-------------|
| `daily_peak_shift_significance_table.png` | Three-panel table: IEPR all years \| IEPR near-term verification \| RESOLVE |
| `daily_peak_distributions_iepr_resolve_eia.png` | Violin plots of daily peak-hour distributions per month |
| `iepr_peak_hour_evolution.png` | How IEPR's predicted daily peak hour changes across the 2024–2050 forecast horizon |
| `data/tables/daily_peak_shift_significance.csv` | Full statistical results (t-test + Mann-Whitney U) |
