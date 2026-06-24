# California Hourly Disaggregation

Compile publicly available data to support substation-level hourly load disaggregation
for California transmission studies.  The pipeline collects hourly load profiles and
physical attributes for substations served by the four major California investor-owned
utilities (IOUs), inter-BA interchange data from EIA 930, and statewide demand forecasts
from the California Energy Commission (CEC IEPR).

---

## Table of Contents

- [Repository Structure](#repository-structure)
- [Setup](#setup)
- [Data Sources](#data-sources)
  - [EIA 930](#eia-930-hourly-balancing-authority-operations)
  - [Utility IOUs — Load Profiles](#utility-ious--load-profiles)
  - [Utility IOUs — Substation Attributes](#utility-ious--substation-attributes)
  - [Substation Coverage Summary](#substation-coverage-summary)
  - [ReEDS Projected Load](#reeds-projected-load-nrel)
  - [ReEDS Historic Load](#reeds-historic-load-nrel-2016-2023)
  - [CEC IEPR Forecasts](#cec-iepr-forecasts)
- [Data Pipeline](#data-pipeline)
  - [Step 1 — Scrape raw data](#step-1--scrape-raw-data)
    - [EIA 930](#eia-930)
    - [PG&E](#pge)
    - [SCE](#sce)
    - [SDG&E](#sdge)
    - [PacifiCorp](#pacificorp)
    - [EIA Form 861](#eia-form-861-annual-retail-sales)
    - [CalPeco / BVES](#calpeco--bves)
  - [Step 2 — Process into unified outputs](#step-2--process-into-unified-outputs)
    - [Substation tables (raw)](#substation-tables-raw)
    - [Substation tables (cleaned)](#substation-tables-cleaned)
    - [Substation-to-county and ReEDS-region mapping](#substation-to-county-and-reeds-region-mapping)
    - [EIA interchange](#eia-interchange)
    - [RESOLVE load inputs](#resolve-load-inputs)
    - [ReEDS California load](#reeds-california-load)
    - [ReEDS historic California load](#reeds-historic-california-load)
    - [ReEDS county disaggregation reference table](#reeds-county-disaggregation-reference-table)
    - [Substation to county spatial join](#substation-to-county-spatial-join)
    - [EIA Form 861 — CA fractions by BA](#eia-form-861--ca-fractions-by-ba)
  - [Step 3 — Validate and audit](#step-3--validate-and-audit)
- [Notebooks](#notebooks)
- [Time Zone and DST Conventions](#time-zone-and-daylight-saving-time-conventions)
  - [Substation DST treatment](#substation-dst-treatment-majority-month-rule)
  - [Converting between conventions](#converting-between-conventions)
- [Notes on Data Quality](#notes-on-data-quality)
- [RESOLVE and Statewide Load Forecast Sources](#resolve-and-statewide-load-forecast-sources)
  - [RESOLVE](#resolve)
  - [ReEDS](#reeds-nrel--ira_low-scenario-and-historic-2016-2023)
  - [BTM Solar Treatment by Source](#btm-solar-treatment-by-source)
  - [RESOLVE vs IEPR: Modeling Framework Differences](#resolve-vs-iepr-modeling-framework-differences)
  - [RESOLVE Baseline + Overlays = IEPR](#resolve-baseline--overlays--iepr-mathematical-verification)
  - [EIA CA8 Group: CA Fractions by BA](#eia-ca8-group-california-fractions-by-balancing-authority)
- [Peak Hour Alignment](#peak-hour-alignment-reconciling-three-measures-of-iepr-vs-eia)
  - [Why fig4 and daily distributions appear to contradict](#why-fig4-and-the-daily-distributions-appear-to-contradict-each-other)
  - [RESOLVE as a reference](#resolve-as-a-reference)

---

## Repository Structure

```
california-hourly-disaggregation/
├── data/
│   ├── raw/                        # Downloaded source data (gitignored)
│   │   ├── eia/                    # EIA 930 interchange and region files
│   │   ├── iepr/                   # CEC IEPR forecast workbooks (manual download)
│   │   ├── reeds/                  # NREL ReEDS datasets
│   │   │   ├── reeds_load_transformed.parquet       # ReEDS IRA_low projected load (2020–2050)
│   │   │   ├── historic_post2015_load_hourly.h5     # ReEDS historic actual load (2016–2023)
│   │   │   └── ReEDS-2.0/          # Full ReEDS model inputs (inputs/, hourlize/, shapefiles/)
│   │   ├── resolve/                # RESOLVE Code Base and Inputs (E3/CPUC IRP)
│   │   │   └── RESOLVE Code Base and Inputs/
│   │   │       ├── data/profiles/loads/2024/        # Full 8760h load profiles (PGE, SCE, SDGE, …)
│   │   │       └── data/interim/loads/              # Annual energy targets for profile scaling
│   │   ├── pge/                    # PG&E ArcGIS feeder and substation files
│   │   ├── sce/                    # SCE DRPEP bulk download and ArcGIS files
│   │   ├── sdge/                   # SDG&E load profiles and substation attributes
│   │   ├── pacificorp/             # PacifiCorp substation and DER readiness files
│   │   ├── calpeco/                # CalPeco (Liberty Utilities) — no data yet
│   │   └── bves/                   # BVES — no data yet
│   └── processed/
│       ├── substations/
│       │   ├── substation_attributes_clean.csv      # One row per substation with coords and attributes
│       │   ├── substation_load_profiles_clean.csv   # Deduplicated hourly min/max load by substation
│       │   └── substation_county_reeds_mapping.csv  # Substation → county → ReEDS p-region + LPF
│       ├── reeds/
│       │   ├── reeds_ca_load_hourly.parquet         # CA-filtered ReEDS projected hourly load
│       │   ├── reeds_ca_load_annual.csv             # ReEDS projected annual totals by p-region
│       │   ├── historic_ca_load_hourly.parquet      # CA-filtered ReEDS historic hourly load
│       │   ├── historic_ca_load_annual.csv          # ReEDS historic annual totals by p-region
│       │   └── county_ca_reference.csv              # CA county → p-region + LPF + BTM PV (2010–2050)
│       └── eia/
│           └── eia_interchange.csv                  # Standardized BA interchange
├── notebooks/
│   ├── 01_eia_from_to_consistency.ipynb      # FROM vs TO cross-file consistency
│   └── 02_eia_region_vs_interchange.ipynb    # Region TI vs sum-of-BA interchange
├── scripts/
│   ├── data/                       # Scraping and processing — organised by source
│   │   ├── eia/                    # EIA-930 scrape, PUDL ingest, and processing
│   │   ├── iepr/                   # CEC IEPR forecast processing
│   │   ├── resolve/                # RESOLVE load-input processing
│   │   ├── reeds/                  # ReEDS processing pipeline
│   │   │   ├── process_reeds.py               # Projected load → reeds_ca_load_*.{parquet,csv}
│   │   │   ├── process_historic_load.py       # Historic HDF5 → historic_ca_load_*.{parquet,csv}
│   │   │   └── process_county_disaggregation.py  # County → p-region + LPF + BTM PV reference table
│   │   ├── pge/                    # PG&E scraper
│   │   ├── sce/                    # SCE scraper, ingest, and validation
│   │   ├── sdge/                   # SDG&E scraper
│   │   ├── bves/                   # BVES scraper (placeholder)
│   │   ├── calpeco/                # CalPeco scraper (placeholder)
│   │   ├── pacificorp/             # PacifiCorp scraper
│   │   └── substations/            # Substation processing, audit, and spatial join
│   │       ├── process_substations_clean.py   # Clean and deduplicate substation profiles
│   │       └── assign_substation_counties.py  # Spatial join: substations → county → p-region
│   ├── compare_cal_region_sources.py    # EIA API CAL vs PUDL CA5 sum
│   ├── compare_eia_sources.py           # EIA API scrape vs PUDL nightly
│   ├── compare_iepr_eia.py              # IEPR projections vs EIA realized demand
│   ├── compare_resolve_iepr_eia.py      # RESOLVE vs IEPR vs EIA (with ReEDS overlay)
│   └── compare_substation_eia_iepr.py   # Substation profiles vs EIA and IEPR
├── src/data/                       # Scraper and processing library modules
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
This project covers eight California-adjacent BAs:

| BA code | Full name |
|---------|-----------|
| BANC | Balancing Authority of Northern California |
| CISO | California Independent System Operator |
| IID  | Imperial Irrigation District |
| LDWP | Los Angeles Dept. of Water and Power |
| NEVP | NV Energy (Nevada) |
| PACW | PacifiCorp West |
| TIDC | Turlock Irrigation District |
| WALC | Western Area Lower Colorado |

**Primary source: PUDL nightly parquet.**  [PUDL](https://catalyst.coop/pudl/) (Public
Utility Data Liberation) mirrors EIA-930 daily with cleaned, imputed, and gap-filled
values, with history from 2015 onward.  We download two filtered parquets restricted to
the eight CA BAs via `scripts/data/eia/ingest_eia_pudl.py`, then process them into
`data/processed/eia/eia930_operations.csv` and `eia930_interchange.csv`.  All timestamps
are UTC (`datetime_utc`, **hour-ending**).  PUDL's gap-filling methodology is documented at
https://docs.catalyst.coop/pudl/en/latest/methodology/timeseries_imputation.html.

> **EIA-930 hour convention:** EIA instructs balancing authorities to report in
> **hour-ending UTC** — a timestamp of `T06:00:00Z` represents the period ending at
> 06:00 UTC, i.e., the integrated MWh for 05:00–06:00 UTC.  Example from EIA instructions:
> "hour ending 1:00 AM EST → 2017-03-01T06:00:00.000Z."  PUDL preserves this convention
> unchanged — EIA API and PUDL values agree to < 0.001% at identical UTC timestamps.
> To convert to fixed PST hour-beginning labels (as used by IEPR, RESOLVE, and substations):
> subtract **9 hours** = 8h UTC-to-PST offset + 1h hour-ending-to-beginning.  This is
> applied in `_utc_to_pst()` in `compare_substation_eia_iepr.py`.  For annual and monthly
> totals the distinction is negligible (< 0.01% of annual load).

**EIA demand definition:** EIA defines demand as total metered net electricity generation
within the BA minus total metered net electricity interchange with neighboring BAs
([EIA Grid Monitor methodology](https://www.eia.gov/electricity/gridmonitor/about)).
Because behind-the-meter (BTM) generation is not visible to BA-boundary meters, this is
a **net-of-BTM** measure — rooftop solar that never crosses a BA meter reduces the
apparent demand but is not explicitly subtracted.

**Secondary source: EIA API direct scrape.**  `scripts/data/eia/scrape_eia.py` queries the
EIA v2 API (`https://api.eia.gov/v2/electricity/rto/`) for two endpoints:
- **`rto-interchange`** — hourly MWh flows between every tracked BA pair (scraped in two
  passes: flows *from* each BA and flows *to* each BA)
- **`rto-region`** — hourly demand, net generation, total interchange, and day-ahead
  forecast for the aggregate California (`CAL`) region

This scrape produces the `data/raw/eia/` CSV files used only to validate PUDL (see
`scripts/compare_eia_sources.py`) and to provide the EIA API CAL region series alongside
the PUDL-derived CA5 sum in `scripts/compare_cal_region_sources.py`.  For all analysis
scripts, `eia930_operations.csv` (PUDL) is the authoritative operations source.

**Source validation: `scripts/compare_eia_sources.py`** cross-checks PUDL against the
EIA API scrape across four sections:

| Section | What it checks |
|---------|----------------|
| **A** — Source summary | Row counts, BA coverage, and date ranges for both sources |
| **B** — Hourly coverage | Per BA, within the overlap window: hours present in EIA but absent from PUDL (gaps are a concern); hours present in PUDL but absent from EIA (minor) |
| **C** — Value agreement | For demand, demand forecast, net generation, and total interchange: Pearson correlation, MAE, and share of hours with \|diff\| > 50 MWh across all paired observations |
| **D** — NaN audit | For each PUDL metric and BA, count of NaN values, when they occur (early record / recent / scattered), and maximum consecutive NaN run length |
| **E** — Scope comparison | Annual TWh: EIA CA8 sum vs PUDL CA5 sum vs EIA API CAL region vs IEPR; quantifies the NEVP+PACW excess (~60 TWh) in the CA8 sum.  The "PUDL CA5 sum" is defined as BANC+CISO+IID+LDWP+TIDC — the five BAs that serve only California load.  EIA defines the CAL region as exactly this sum; verified in `scripts/compare_cal_region_sources.py` where the EIA API CAL series and the PUDL CA5 sum track each other within imputation differences. |

Outputs are written to `data/checks/` (CSV files) and `data/figures/fig_e_cal_vs_ca8_vs_iepr.png`.
Run with `-s D` to audit NaN values only (no EIA scrape file required).

> **PUDL preferred over EIA scrape for analysis** because PUDL applies gap-filling and
> imputation that the raw EIA API data does not, starts earlier (2015 vs ~2019 for the
> API CAL region), and is updated nightly with corrections.  The EIA scrape is retained
> for independent validation via `compare_eia_sources.py`.

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
| Load profile rows (processed)        | 191,184 | 166,440 | 28,512 | 386,136   |

¹ SDG&E: 99 substations with data + 8 failed scrapes = 107 attempted.

The **name dictionary** (`data/basinSourceDictionary.csv`, 79 entries) maps utility
source names that differ from the DataBasin reference (e.g. "CRESTA PH" → "Cresta",
"DRUM" → "Drum 1 / Drum 2") to recover additional geolocation matches beyond the
normalised-name join.  PG&E and SDG&E publish monthly aggregates without a year column.

**SCE year-stamp deduplication:** SCE publishes year-stamped profiles (2017–2026), where
each year is an independent 10th/90th percentile snapshot from a non-public utility
lookback window.  652 of 709 unique SCE substations appear in multiple years with
overlapping coverage.  The 2026 vintage only covers January–April.  The processed output
applies per-cell deduplication: for each `(substation, month, hour)` the row with the
highest year is retained, so May–December data for substations in the 2026 batch falls
back to 2025 automatically.  This gives full 12-month coverage per substation using the
most recent available percentile snapshot.  See `process_substations_clean.py`.

### ReEDS Projected Load (NREL)

The Regional Energy Deployment System (ReEDS) is NREL's long-term US capacity-planning
model.  This project uses a pre-transformed parquet produced by a ReEDS run under the
**IRA_low** (Inflation Reduction Act, low-demand growth) scenario.

**File:** `data/raw/reeds/reeds_load_transformed.parquet`

ReEDS divides California into four planning regions (`p`-regions) from
`inputs/hierarchy.csv` in the ReEDS 2.0 repository:

| Region | ReEDS NERC region | Description |
|--------|-------------------|-------------|
| p8  | WECC_NW | PacifiCorp West — California slice only (~0.8 TWh/yr) |
| p9  | WECC_CA | California sub-region (see scope note below) |
| p10 | WECC_CA | California sub-region (see scope note below) |
| p11 | WECC_CA | California sub-region (see scope note below) |

**Important scope note — WECC_CA ≠ EIA CISO BA:** The `hierarchy.csv` labels p9–p11
as `WECC_CA`.  Empirically, the annual load of p9+p10+p11 (~252–268 TWh, 2016–2023
actual) tracks the PUDL CA5 sum (BANC+CISO+IID+LDWP+TIDC), not EIA CISO alone
(~218–224 TWh).  The ~40 TWh gap between p9–p11 and EIA CISO equals approximately
IID + LDWP + BANC + TIDC combined.  This confirms that **WECC_CA in ReEDS = all
California BAs except PacifiCorp West** — it is not limited to the CAISO BA boundary.
Do not compare ReEDS p9–p11 directly to EIA CISO; compare to PUDL CA5 or EIA CAL.

The raw parquet is long-format: one row per (`time_index`, `weather_year`, `region`,
`year`).  `time_index` runs 1–8,760 (no Feb 29).  ReEDS uses CST (UTC−6, no DST)
as its output timezone (`config_base.json` line 7: `"output_timezone": "Etc/GMT+6"`),
so `time_index` 1 = Jan 1 00:00 CST = **Dec 31 22:00 PST**.  `process_reeds.py`
converts to fixed PST before writing processed outputs.
`weather_year` is one of 7 historical patterns (2007–2013) used to generate hourly
shapes.  `year` is the planning target year (2020–2050).

### ReEDS Historic Load (NREL, 2016–2023)

**File:** `data/raw/reeds/historic_post2015_load_hourly.h5`

The same 134-region structure as the ReEDS projected data, but covering **actual
observed load** for 2016–2023 (8 years × 8,760 h = 70,080 rows; leap days excluded,
consistent with ReEDS hourlize convention).  Timestamps are in CST (UTC−6), same
timezone as the projected data.

Load definition: sourced by the ReEDS hourlize tool from BA-level meter data
(EIA-930 / FERC Form 714), which report demand **net of BTM generation** — same
convention as EIA CISO.  CITATION NEEDED: specific hourlize input mapping not
confirmed in publicly available files; validation against EIA sources is provided
empirically in `compare_resolve_iepr_eia.py`.

**Processed by:** `scripts/data/reeds/process_historic_load.py`
**Outputs:**
- `data/processed/reeds/historic_ca_load_annual.csv` — annual TWh by region
- `data/processed/reeds/historic_ca_load_hourly.parquet` — hourly CA data

Annual totals (WECC_CA = p9+p10+p11; CA total = p8+p9+p10+p11):

| Year | WECC_CA p9-p11 (TWh) | CA total p8-p11 (TWh) |
|------|---------------------|-----------------------|
| 2016 | 268.3 | 269.1 |
| 2017 | 269.2 | 270.1 |
| 2018 | 267.3 | 268.1 |
| 2019 | 262.5 | 263.3 |
| 2020 | 261.9 | 262.7 |
| 2021 | 259.5 | 260.3 |
| 2022 | 264.1 | 264.9 |
| 2023 | 251.8 | 252.5 |

The ~0.8 TWh annual difference between WECC_CA and CA total is the PacifiCorp West
California slice (p8), which is negligible at this scale.

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

**Primary (PUDL — recommended):**

```bash
python scripts/data/eia/ingest_eia_pudl.py   # downloads parquets → data/raw/eia/pudl/
```

Downloads two filtered PUDL parquets for the eight CA BAs:
- `out_eia930__hourly_operations_CA8.parquet` — per-BA hourly demand, net gen, interchange
- `core_eia930__hourly_interchange_CA8.parquet` — BA-pair interchange flows (optional)

**Secondary (EIA API direct — for CAL region and validation only):**

```bash
# Interchange: flows FROM each of the 8 BAs (all counterparts)
python scripts/data/eia/scrape_eia.py rto-interchange \
    --from-bas BANC CISO IID LDWP PACW NEVP TIDC WALC

# Interchange: flows TO each of the 8 BAs (all counterparts)
python scripts/data/eia/scrape_eia.py rto-interchange \
    --to-bas BANC CISO IID LDWP PACW NEVP TIDC WALC

# CAL region aggregate (demand, net gen, TI, day-ahead forecast) — 2019 to present
python scripts/data/eia/scrape_eia.py rto-region
```

Output: `data/raw/eia/eia_rto-interchange-data_from-*.csv` (×4 chunks),
`eia_rto-interchange-data_to-*.csv` (×4 chunks), `eia_rto-region-data_CAL_*.csv`

> An `EIA_API_KEY` environment variable is required for the EIA scraper.
> The PUDL download does not need an API key.

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

#### Substation tables (raw)

```bash
python scripts/data/substations/process_substations.py
```

Reads all raw utility files and writes two CSVs to `data/processed/substations/`:

**`substation_attributes.csv`** — one row per substation (2,614 total across all utilities)

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
| existing_gen | Existing DER/generation capacity (MW): PGE/SCE/SDGE from ICA data; PacifiCorp from DG Readiness `Existing_DER` field (same column, different source) |
| queued_gen | Queued interconnection capacity (MW) — PGE, SCE, SDGE only |
| total_gen | Existing + queued (MW) — PGE, SCE, SDGE only |
| projected_load | Projected peak load (MW) — SCE, SDGE only |
| der_penetration | DER as % of projected load — SCE, SDGE only |
| max_remain_cap | Maximum remaining hosting capacity (MW) — SCE only |
| circuit_count | Number of distribution circuits (PG&E = transformer banks; PacifiCorp from DG Readiness) |
| res/com/agr/ind/other_pct | Customer-class share of circuits (%) — SCE only |
| res/com/agr/ind/other_total | Customer-class circuit count — SCE only |
| note_sub | Data quality flag (PG&E `REDACTED` field; SCE: interconnection notes) |
| net_min_daytime_load_mw | PacifiCorp only: net minimum daytime load aggregated across circuits (MW) |

**`substation_load_profiles.csv`** — hourly min/max load by substation (455,568 rows)

| Column | Description |
|--------|-------------|
| utility | Source utility |
| substation_name | Matches `substation_attributes.csv` |
| latitude, longitude | Coordinates |
| year | Calendar year (NaN for PG&E, which publishes monthly aggregates without year) |
| month | 1–12 |
| hour | 0–23, **wall-clock Pacific time** (PDT in summer, PST in winter — see DST section) |
| min_load | Minimum load observed in that month/hour slot (MW) |
| max_load | Maximum load observed in that month/hour slot (MW) |

#### Substation tables (cleaned)

```bash
python scripts/data/substations/process_substations_clean.py
```

Applies filtering, deduplication, coordinate enrichment, and DST correction to produce
the analysis-ready versions used by all comparison scripts:

**`substation_attributes_clean.csv`** — 1,341 substations (PGE 664 · SCE 578 · SDGE 99)

Filtering applied: P.T. (pass-through switching) substations removed (170 SCE, 8 SDGE); PacifiCorp excluded (no metered load profiles); SCE deduplication — bulk download preferred over scraped data on matching keys.

| Column | Description |
|--------|-------------|
| utility | `pge`, `sce`, or `sdge` |
| substation_name | Name as reported by the utility |
| util_lat, util_lon | Coordinates from the utility data source (primary) |
| basin_lat, basin_lon | Coordinates from DataBasin CA Substations 2022 (fallback) |
| dist_to_basin_km | Haversine distance (km) between util and Basin coordinate matches |
| sub_type | Substation type (e.g. "Distribution", "Transmission") |
| substation_voltage, voltage_kv | Primary bus voltage label and numeric kV |
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
| res/com/agr/ind/other_pct | SCE: customer-class share of circuits (%) |
| res/com/agr/ind/other_total | SCE: customer-class circuit count |
| note_sub | Data quality flag (PG&E `REDACTED` field) |

**`substation_load_profiles_clean.csv`** — 386,136 rows

Filtering applied: P.T. substations removed; SCE loads deduplicated (most-recent year-vintage per `(substation, month, hour)` cell); SDGE kW→MW conversion applied; `hour_pst` column added.

| Column | Description |
|--------|-------------|
| utility | `pge`, `sce`, or `sdge` |
| substation_name | Matches `substation_attributes_clean.csv` |
| year | SCE vintage year (2017–2026); NaN for PGE and SDGE (no year stamp published) |
| month | 1–12 |
| hour | 0–23, original wall-clock Pacific time (PDT in summer, PST in winter) |
| hour_pst | 0–23, **fixed PST (UTC−8, no DST)** — use this column for all comparisons |
| min_load | ~10th-percentile load for that (month, hour) cell (MW) |
| max_load | ~90th-percentile load for that (month, hour) cell (MW) |

#### Substation-to-county and ReEDS-region mapping

```bash
python scripts/data/reeds/process_county_disaggregation.py   # county reference table
python scripts/data/substations/assign_substation_counties.py # spatial join
```

**`data/processed/reeds/county_ca_reference.csv`** — 58 rows, one per California county

Built from `county2zone.csv` (county→p-region), `county_state_lpf.csv` (county load participation factors), and `distpvcap_stscen2023_mid_case.csv` (county BTM PV capacity by year) from `data/raw/reeds/ReEDS-2.0/inputs/`.

| Column | Description |
|--------|-------------|
| fips_int | FIPS county code as integer (e.g. 6037 for Los Angeles) |
| fips_key | FIPS in ReEDS p-format string (e.g. `p06037`) |
| county_name | County name |
| state | State abbreviation (CA for all rows) |
| p_region | ReEDS planning region: p8, p9, p10, or p11 |
| ca_load_fraction | County's fraction of California state load (sums to 1.0 across all 58 counties); source: `county_state_lpf.csv` |
| btm_pv_{year}_mw | Distributed PV capacity (MW) for that county in the given year (2010–2050 in 2-year steps); source: `distpvcap_stscen2023_mid_case.csv` |

Distribution across p-regions: p9 = 44 counties (37.4% of CA load), p10 = 10 counties (55.2%), p11 = 1 county (7.1%), p8 = 3 counties (0.3%; PacifiCorp CA slice — no PGE/SCE/SDGE substations fall here).

**`data/processed/substations/substation_county_reeds_mapping.csv`** — 1,329 rows

Spatial join of `substation_attributes_clean.csv` coordinates against the Census TIGER/Line 2022 county shapefile (`tl_2022_us_county.shp`), then joined to `county_ca_reference.csv`. 12 substations are excluded for missing coordinates (neither utility nor Basin source provides lat/lon). All 1,329 assigned substations fall in p9/p10/p11; none fall in p8 (PacifiCorp CA slice is geographically separate).

| Column | Description |
|--------|-------------|
| utility | `pge`, `sce`, or `sdge` |
| substation_name | Matches `substation_attributes_clean.csv` |
| lat, lon | Best available coordinates (utility source preferred, Basin fallback) |
| coord_source | `util` (1,320 substations) or `basin` (9 substations) |
| fips_int | FIPS county code as integer |
| fips_key | FIPS in ReEDS p-format string (e.g. `p06037`) |
| county_name | County containing the substation |
| p_region | ReEDS p-region for that county (p9, p10, or p11) |
| ca_load_fraction | County's fraction of California state load |
| btm_pv_{year}_mw | County distributed PV capacity (MW) for each 2-year step 2010–2050 |

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
`data/raw/RESOLVE Code Base and Inputs/RESOLVE Code Base and Inputs/data/profiles/loads/2024/`
(full 8,760-hour profiles — no model run needed) and annual scaling targets from
`data/interim/loads/`, then writes two CSVs to `data/processed/resolve/`.

> **Why no model run is needed:** The RESOLVE Outputs directory contains only 36 representative
> dispatch windows used by the optimizer internally.  The full 8,760-hour Baseline load profiles
> already exist as inputs (`{UTIL}_Baseline.csv`, 23 weather years × 8,760 h = 201,480 rows
> each).  `process_resolve.py` reads these directly and applies the annual scaling described
> below.  Running the full RESOLVE optimization requires the HiGHS or Gurobi solver plus a
> `dispatch_windows_map.csv` cluster file not included in the local copy.

**How the annual scaling works:**

Each `{UTIL}_Baseline_CHP_Not_Retire.csv` in `data/interim/loads/` is a long-format file with
`attribute` / `timestamp` / `value` / `scenario` columns.  The relevant rows are:

| attribute | what it means |
|-----------|---------------|
| `profile` | path to the shape file (e.g. `profiles/loads/2024/PGE_Baseline.csv`) |
| `scale_by_energy` | `True` — instructs RESOLVE to scale by energy, not by peak capacity |
| `annual_energy_forecast` | one row per model year (2024–2045), value in MWh |
| `td_losses_adjustment` | transmission/distribution loss multiplier (1.0 in this dataset) |

RESOLVE (and our script) applies this formula **per weather year**:

```
scale_factor = annual_energy_forecast_MWh[target_year] / sum(profile_MW × 1h) for all 8760h
scaled_MW[hour] = profile_MW[hour] × scale_factor
```

Every hour in that weather year is multiplied by the same scalar.  The *shape* of demand comes
from historical weather-year patterns; the *magnitude* is anchored to the IEPR-derived forecast.
Our script fixes the target year at 2024 for all weather years to produce comparable absolute
levels (`demand_mw_2024scaled`).

**What "CHP Not Retire" means:** CHP = Combined Heat and Power (industrial/commercial cogeneration).
"Not Retire" means these plants remain online and continue to self-supply their host facilities,
keeping BA-meter demand lower.  The alternative scenario (`CHP_Retire.csv`) assumes these plants
are decommissioned, shifting their load back onto the grid.  The two scenarios differ by a few
percent at most.

**How BTM solar and storage are handled:**

RESOLVE models demand as a sum of multiple additive load components, each with its own profile
and annual target.  We process only the Baseline component.  The full component list for PGE
(same structure exists for SCE, SDGE, LDWP, NCNC) includes:

| File | Sign | What it represents |
|------|------|--------------------|
| `PGE_Baseline_CHP_Not_Retire.csv` | + | Core grid load (all end uses) |
| `PGE_AAEE.csv` | − | Advanced Action Energy Efficiency (demand reductions) |
| `PGE_AAFS.csv` | + | Advanced Action Fuel Substitution (electrification: EVs, heat pumps) |
| `PGE_Storage_Losses.csv` | + | Round-trip losses from grid-scale battery storage |
| `PGE_Baseline_LDVs.csv` | + | Light-duty vehicle EV charging load |
| `PGE_Baseline_MHDVs.csv` | + | Medium/heavy-duty vehicle EV charging load |
| `PGE_Climate_Impacts.csv` | + | Additional cooling/heating demand from climate change |
| `PGE_Data_Centers.csv` | + | Data center load growth |

**BTM solar** (`Customer_PV`) is NOT a load component — it is modeled as a **supply-side resource**
in `data/profiles/pmax/2025/{UTIL}_Customer_PV.csv` (column `Weather Factor`, hourly capacity
factor 0–1) with installed capacity set in `data/interim/resources/{UTIL}_Customer_PV.csv`.
RESOLVE dispatches this resource to offset grid demand during optimization:
`net_load = Baseline_demand − Customer_PV_generation`.  The `demand_mw_2024scaled` column
in our processed output is therefore **before** BTM solar subtraction.  `compare_substation_eia_iepr.py`
subtracts the native Customer_PV offset to produce comparable net-load figures.

**BTM storage** is handled analogously as a supply-side resource; round-trip losses of
grid-scale (non-BTM) batteries appear in the `Storage_Losses` load component above.

**`resolve_hourly_profiles.csv`** — hourly load shapes for six California BA zones (PGE, SCE, SDGE, IID, LDWP, NCNC — where NCNC = Northern California Non-CAISO, covering TIDC + BANC territory), covering 23 historical weather years (2000–2022) at 8,760 h/year (no Feb 29)

| Column | Description |
|--------|-------------|
| datetime_pst | Hourly timestamp (fixed PST, UTC−8, no DST) |
| utility | BA zone label: PGE, SCE, SDGE, IID, LDWP, or NCNC |
| demand_mw_raw | Raw shape value (MW) from `profiles/loads/2024/{UTIL}_Baseline.csv`; reflects historical weather-year load magnitudes |
| demand_mw_2024scaled | `demand_mw_raw` scaled so each weather year integrates to the PGE/SCE/SDGE 2024 annual energy forecast (MWh); all 23 weather years brought to the same absolute level for direct comparison |

**`resolve_annual_forecast.csv`** — annual energy forecast targets (MWh and TWh) by utility and year (2024–2045), extracted from the `annual_energy_forecast` rows of each `{UTIL}_Baseline_CHP_Not_Retire.csv`.

#### ReEDS California load

```bash
python scripts/data/reeds/process_reeds.py
```

Filters the raw ReEDS parquet to the four California p-regions, adds `month`/`day`/`hour`
columns from the `time_index`, and writes two outputs to `data/processed/reeds/`:

**`reeds_ca_load_hourly.parquet`** — CA-filtered hourly rows (7.6 M rows):

| Column | Description |
|--------|-------------|
| time_index | 1–8,760 (Jan 1 h0 → Dec 31 h23, no Feb 29) |
| weather_year | 2007–2013 — which historical weather pattern drives the shape |
| region | p8, p9, p10, p11 |
| region_label | PacifiCorp_West_CA, CAISO_North, CAISO_Central, CAISO_South |
| load_mw | Projected hourly load (MW) |
| year | Planning target year (2020–2050) |
| scenario | IRA_low |
| month, day, hour | Derived from time_index; hour 0–23 fixed PST (no DST) |

**`reeds_ca_load_annual.csv`** — annual energy totals by (year, weather_year, region) plus a `CA_total` row summing all four regions.  IRA_low CA total grows from ~291 TWh (2020) to ~525 TWh (2050) — higher than other California sources because ReEDS covers all of California (CAISO + PacifiCorp West CA slice), models electrification growth explicitly, and reports gross load.

#### ReEDS historic California load

```bash
python scripts/data/reeds/process_historic_load.py
```

Reads `data/raw/reeds/historic_post2015_load_hourly.h5` and writes two outputs to `data/processed/reeds/`:

- **`historic_ca_load_hourly.parquet`** — 70,080 rows (8 years × 8,760 h) for p8–p11 plus derived WECC_CA and CA total columns.
- **`historic_ca_load_annual.csv`** — annual TWh by region (p8, p9, p10, p11, CAISO_total, CA_total).

#### ReEDS county disaggregation reference table

```bash
python scripts/data/reeds/process_county_disaggregation.py
```

Joins three ReEDS input files (all from `data/raw/reeds/ReEDS-2.0/inputs/`) to produce a California county reference table:

- **`county_ca_reference.csv`** — 58 rows (one per California county).

| Column | Description |
|--------|-------------|
| fips_int | Integer county FIPS (e.g., 6037) |
| fips_key | p-format FIPS (e.g., `p06037`) — matches ReEDS disaggregation files |
| county_name | County name |
| p_region | ReEDS p-region (`p8`, `p9`, `p10`, or `p11`) |
| ca_load_fraction | County share of California state load (sums to 1.0 across 58 counties) |
| btm_pv_{year}_mw | County distributed PV capacity (MW) for 2010–2050 in 2-year steps |

Source files: `county2zone.csv` (FIPS → p-region), `disaggregation/county_state_lpf.csv` (load participation factors), `dgen_model_inputs/stscen2023_mid_case/distpvcap_stscen2023_mid_case.csv` (BTM PV by year).

#### Substation to county spatial join

```bash
python scripts/data/substations/assign_substation_counties.py
```

Spatially joins each substation (from `substation_attributes_clean.csv`) to a California county polygon using the Census TIGER/Line 2022 county shapefile (`data/raw/reeds/ReEDS-2.0/inputs/shapefiles/cache/tl_2022_us_county/`), then merges the county reference table to assign a ReEDS p-region, load fraction, and BTM PV capacity to every substation.

- **`substation_county_reeds_mapping.csv`** — 1,329 substations with valid coordinates (12 excluded for missing lat/lon).

| Column | Description |
|--------|-------------|
| utility | PGE, SCE, or SDGE |
| substation_name | Utility substation name |
| lat, lon | Coordinates used (util or basin fallback) |
| coord_source | `util` (primary utility) or `basin` (DataBasin fallback) |
| fips_int, fips_key | County FIPS in integer and p-format |
| county_name | County name |
| p_region | ReEDS p-region (p9, p10, or p11 — no PGE/SCE/SDGE substations fall in p8) |
| ca_load_fraction | County share of statewide CA load |
| btm_pv_{year}_mw | County-level distributed PV capacity (MW) for 2010–2050 |

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
# Cross-validate PUDL EIA-930 against the EIA API scrape (all sections)
python scripts/compare_eia_sources.py

# NaN audit only — does not require the EIA scrape file
python scripts/compare_eia_sources.py -s D

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

## Time Zone and Daylight Saving Time Conventions

Every processed file in this project uses a specific time zone and hour-labeling convention.
The table below documents them so comparisons across files are unambiguous.

**Canonical comparison format** (used by all compare scripts): **fixed PST (UTC−8), hour-beginning, hours 0–23**.

| File | Time zone | Raw hour label | Column name | Conversion to canonical PST hour-beg 0–23 | DST? |
|------|-----------|---------------|-------------|-------------------------------------------|------|
| `eia930_operations.csv` (PUDL) | UTC | **hour-ending** (`datetime_utc`) | `datetime_utc` | Subtract **9h** (8h offset + 1h ending→beginning) via `_utc_to_pst()` | n/a |
| `eia930_cal_region_PUDL.csv` | UTC | **hour-ending** (same PUDL source) | `datetime_utc` | Same `_utc_to_pst()` (−9h) | n/a |
| `eia_region.csv` (EIA API scrape) | UTC | **hour-ending**, `YYYY-MM-DDTHH` | `period` | Subtract **9h** | n/a |
| `iepr_hourly_forecast.csv` | Fixed PST (UTC−8) | **hour-ending**, 1–24 | `HOUR` | `hour0 = HOUR − 1` (→ 0–23) | No |
| `resolve_hourly_profiles.csv` | Fixed PST (UTC−8) | **hour-beginning**, 0–23 | `datetime_pst` | None | No |
| `substation_load_profiles_clean.csv` | Fixed PST (UTC−8) | **hour-beginning**, 0–23 | `hour_pst` | None | No |
| `reeds_ca_load_hourly.parquet` (projected) | Fixed PST (UTC−8) | **hour-beginning**, 0–23 | `hour` (int8) | None (cast int8→int64 before arithmetic) | No |
| `historic_ca_load_hourly.parquet` (ReEDS) | Fixed PST (UTC−8) | **hour-beginning**, 0–23 | `hour` (int8) | None (cast int8→int64 before arithmetic) | No |
| `substation_load_profiles.csv` (raw) | Wall-clock Pacific | **hour-beginning**, 0–23 | `hour` | Majority-month rule → `hour_pst` (see below) | Yes |

**Why EIA needs −9h, not −8h:**  EIA-930 filing instructions say "report by hour ending time" — the UTC timestamp marks the *end* of the integration period, not the beginning.  Example: `T06:00:00Z` = period 05:00–06:00 UTC = hour ending 1:00 AM EST.  Subtracting only 8h gives 22:00 PST of the *previous* calendar day — one hour too late.  Subtracting 9h gives 21:00 PST, which is the correct **start** label for the 21:00–22:00 PST period.  Annual and monthly totals are unaffected; only hourly peak analysis is sensitive to this.  Evidence: EIA API and PUDL demand values agree to < 0.001% at identical UTC timestamps, confirming PUDL preserves the EIA convention unchanged.

**IEPR column naming:** The processed IEPR file uses the column name `hour0` (= HOUR − 1) while all other sources use `hour`.  Scripts that join IEPR against another source must use `left_on=["month","hour0"], right_on=["month","hour"]` — see `compare_substation_eia_iepr.py` `print_summary()` as the reference pattern.

**ReEDS int8 columns:** The `month`, `day`, and `hour` columns in the ReEDS parquet files are stored as int8 for space efficiency.  Adding a month offset (e.g., `hour + 144` for the annual-profile plot) would overflow int8 (max 127).  The compare script casts these to int64 immediately after reading: `agg["hour"] = agg["hour"].astype("int64")`.

### Substation DST treatment (majority-month rule)

The raw utility substation scrapes report hours in local Pacific wall-clock time — PDT
(UTC−7) from March through October and PST (UTC−8) the rest of the year.  To align with
the IEPR and RESOLVE files (which both use fixed PST), the clean file converts using a
**majority-month rule**: if more than half the days in a calendar month fall in a PDT
period (months 3–10), all hours in that month are shifted back 1 hour; months 1, 2, 11,
and 12 are left unchanged.

```python
# From scripts/data/substations/process_substations_clean.py
pdt_mask = loads_all["month"].isin(range(3, 11))   # months 3–10 are majority-PDT
loads_all["hour_pst"] = loads_all["hour"].where(~pdt_mask, (loads_all["hour"] - 1) % 24)
```

**Methodological note:** This is a deliberate approximation, not a data-verifiable fact.
The min/max load profiles are 10th/90th percentile envelopes computed over a non-public
lookback window — they do not correspond to any specific observed day, so it is impossible
to look up the DST status of individual timestamps.  The majority-month assignment
(per US federal DST rules, 15 USC 260a: second Sunday in March to first Sunday in
November) introduces at most a 1-hour systematic error in the two transition months
(March and November), but avoids the need for exact DST changeover dates.

### Converting between conventions

| From → To | Operation |
|-----------|-----------|
| EIA UTC hour-ending → PST hour-beginning | `ts − 9h` (8h offset + 1h ending→beginning) |
| IEPR hour-ending PST (HOUR 1–24) → hour-beginning PST (0–23) | `hour0 = HOUR − 1` |
| ReEDS CST (UTC−6) → PST (UTC−8) | `ts − 2h` (done in `process_reeds.py` before writing processed outputs) |
| PST hour-beginning → UTC | `ts + 8h` (then treat result as hour-beginning UTC, not hour-ending) |

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

This project compares substation-level profiles against five statewide demand sources.
The sections below document how each source handles behind-the-meter (BTM) solar,
why RESOLVE and IEPR differ numerically, and which values are raw vs derived.

| Source | Scope | Load definition | Horizon | Used for |
|--------|-------|-----------------|---------|----------|
| EIA-930 | CISO BA (CAISO territory) | Net of BTM solar (measured) | Historical (2015–) | Ground truth |
| IEPR | PGE+SCE+SDGE utilities | BASELINE_NET_LOAD (net) or BASELINE_CONSUMPTION (gross) | 2024–2050 | Policy forecast |
| RESOLVE | PGE+SCE+SDGE+IID+LDWP+NCNC | Gross (BTM solar on supply side) | 2024–2045 | IRP optimization target |
| ReEDS projected | p8–p11 (CA total); p9–p11 = WECC_CA ≈ all CA except PACW | Net load projected under IRA_low scenario | 2020–2050 | Long-run US capacity planning |
| ReEDS historic | p9–p11 (WECC_CA ≈ BANC+CISO+IID+LDWP+TIDC) | Net load actual observed | 2016–2023 | Ground truth at WECC_CA scale |
| Substations | PGE+SCE+SDGE distribution | Gross (metered substation peak) | Historical monthly | Sub-BA spatial resolution |

### RESOLVE

RESOLVE (the E3/CPUC Integrated Resource Planning model) is the statewide optimization
model used by CPUC for the 2024-2026 IRP.  Its raw load inputs sit in
`data/raw/RESOLVE Code Base and Inputs/`.  Processed outputs are in
`data/processed/resolve/`.

RESOLVE covers six California BA zones: **PGE**, **SCE**, **SDGE**, **IID**, **LDWP**,
**NCNC**.  It does *not* model NEVP or PACW as California zones (see EIA CA8 note below).
NCNC (Northern California Non-CAISO) covers BANC, TIDC, SMUD, and other small municipal
utilities in northern California outside the CAISO footprint.  See CEC Demand Modelling
Form 1.1c at https://www.energy.ca.gov/data-reports/california-energy-planning-library/forecasts-and-system-planning/demand-side-3

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

### ReEDS (NREL — IRA_low scenario and Historic 2016–2023)

ReEDS (Regional Energy Deployment System) is NREL's flagship US capacity-planning model.
Unlike RESOLVE (which is a California-specific IRP tool) and IEPR (which are CEC policy
forecasts), ReEDS produces long-run US-wide projections through 2050, modelling technology
costs, renewable build-out, and load growth from electrification under specific policy
scenarios.

This project uses two ReEDS datasets:
- **IRA_low projected** (2020–2050): `data/raw/reeds/reeds_load_transformed.parquet`
- **Historic actual** (2016–2023): `data/raw/reeds/historic_post2015_load_hourly.h5`
- **ReEDS-2.0 model inputs**: `data/raw/reeds/ReEDS-2.0/`

Both use the same four California p-regions:

- **p8** — PacifiCorp West California slice (WECC_NW; ~0.8 TWh/yr)
- **p9, p10, p11** — WECC_CA sub-regions (all CA BAs except PacifiCorp West)

**WECC_CA scope:** Despite being labeled as "CAISO sub-regions" in some ReEDS
documentation, p9–p11 empirically correspond to all California BAs except PacifiCorp
West (PACW) — approximately BANC+CISO+IID+LDWP+TIDC.  This was confirmed by comparing
historic p9–p11 annual load (~252–268 TWh) to EIA sources: it tracks the PUDL CA5 sum,
not EIA CISO alone (~218–224 TWh).  Because ReEDS aggregates California into these four
p-regions, IID and LDWP are folded into the WECC_CA regions rather than appearing
separately (as they do in RESOLVE and EIA).

**Projected CA total (IRA_low, all 4 p-regions):**

| Year | Mean TWh (across 7 weather years) |
|------|----------------------------------|
| 2020 | 291 |
| 2025 | 288 |
| 2030 | 336 |
| 2035 | 394 |
| 2040 | 449 |
| 2050 | 525 |

The near-zero standard deviation across weather years (~0–0.6 TWh) confirms that weather
only affects the hourly *shape*, not the annual total — the annual energy level is fixed
by the demand model for each target year.

ReEDS values are higher than RESOLVE/IEPR because ReEDS:
(a) covers all of California (CAISO + PacifiCorp CA), not just CAISO utilities, and
(b) projects strong load growth from EVs and building electrification through 2050.

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
| **ReEDS IRA_low projected** (`reeds_ca_load_annual.csv`) | **Projected net load** — ReEDS models BTM solar as a generation resource that reduces system demand in the optimization | Long-run projected system load (net of BTM solar); WECC_CA (p9–p11) ≈ all CA except PACW; CA total (p8–p11) adds ~0.8 TWh/yr | Raw from ReEDS run; CA filtered in `process_reeds.py` | ~288 TWh (2025, CA total p8–p11) growing to ~525 TWh (2050) |
| **ReEDS historic actual** (`historic_ca_load_annual.csv`) | **Net load** — sourced from BA-level meter data (EIA-930 / FERC Form 714) by the ReEDS hourlize tool | Observed 2016–2023 load; WECC_CA (p9–p11) tracks PUDL CA5 sum, not EIA CISO alone | HDF5 processed by `process_historic_load.py` | ~252–268 TWh (WECC_CA p9–p11, 2016–2023) |

**Key implication for comparisons:** A direct TWh comparison between RESOLVE Baseline
and EIA-930 CISO will show an apparent ~17–20 TWh gap in 2024.  The true sources of
that gap are:

1. **BTM PV (~30 TWh statewide in 2025)** — RESOLVE adds it back; EIA/IEPR subtract it.
   PGE+SCE+SDGE share of statewide BTM PV is roughly ~17–18 TWh, explaining most of the gap.
2. **Geographic scope** — RESOLVE covers PGE+SCE+SDGE+IID+LDWP+NCNC; EIA CISO covers
   only the CAISO footprint (PGE+SCE+SDGE, plus some BANC/TIDC slivers).
3. **T&D losses** — RESOLVE loads include distribution losses (demand at the generator
   busbar rather than the customer meter); EIA-930 measures at the BA boundary.  The
   T&D loss adjustment factor is stored per-utility in RESOLVE's interim loads files
   (`data/interim/loads/{UTIL}_*.csv`, attribute `td_losses_adjustment`); CAISO
   utilities (PGE, SCE, SDGE) use a value of 1.0 in the 2024-2026 IRP inputs,
   meaning no explicit loss grossup is applied in the RESOLVE load files for this cycle.

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
| Non-CAISO California | IID, LDWP, NCNC | Imperial ID, LADWP, TIDC + BANC territory (NCNC = Northern California Non-CAISO) |
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
