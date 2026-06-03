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
├── scripts/                   # Runnable pipeline commands (see below)
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

**EIA API key** — required only for `scripts/scrape_eia.py`.
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

PacifiCorp does not publish comparable hourly load profiles.

### Utility IOUs — Substation Attributes

Physical and DER (distributed energy resource) attributes scraped from each utility's
public ArcGIS FeatureServer:

| Utility | Source layer | Key fields |
|---------|-------------|------------|
| PG&E | EDSubstations (layer 0) | voltage_kv, num_banks, existing/queued/total DG (kW→MW) |
| SCE | ICA Tables layer 3 (Table 3) | existing/queued/total gen, projected load, DER penetration, circuit voltage, customer mix |
| SDG&E | ICA_MAP_PROD_Substations_VW (layer 0) | substation type, voltage, existing/queued/total gen, projected load, DER penetration |
| PacifiCorp | DG Readiness with Net Minimum (layer 0) | existing DER (MW), net minimum daytime load (MW) — aggregated from circuit level |

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

Each `scripts/scrape_*.py` command writes chunked CSVs to the corresponding
`data/raw/<utility>/` folder.  All scrapers support **safe stop/resume**: press
`Ctrl+C` at any time; re-run the same command to continue from where it left off.

#### EIA 930

```bash
# Interchange: flows FROM each of the 8 BAs (all counterparts)
python scripts/scrape_eia.py rto-interchange \
    --from-bas BANC CISO IID LDWP PACW NEVP TIDC WALC

# Interchange: flows TO each of the 8 BAs (all counterparts)
python scripts/scrape_eia.py rto-interchange \
    --to-bas BANC CISO IID LDWP PACW NEVP TIDC WALC

# CAL region aggregate (demand, net gen, TI, day-ahead forecast)
python scripts/scrape_eia.py rto-region
```

Output: `data/raw/eia/eia_rto-interchange-data_from-*.csv` (×4 chunks),
`eia_rto-interchange-data_to-*.csv` (×4 chunks), `eia_rto-region-data_CAL_*.csv`

#### PG&E

```bash
# Feeder load profiles (layer 25) — primary load source
python scripts/scrape_pge.py layer --layer-id 25

# Substation physical attributes (layer 0)
python scripts/scrape_pge.py attributes
```

Output: `data/raw/pge/pge_layer25_*.csv`, `pge_substation_attributes.csv`

#### SCE

SCE load profiles are obtained via the DRPEP bulk download (not scraped automatically):

1. Go to <https://drpep.sce.com/drpep/>
2. Click **Bulk Download → Historical Substation Load Profiles → Download All**
3. Save the ZIP file, then run:

```bash
python scripts/ingest_sce_bulk_download.py path/to/SUBSTATION.zip
```

ArcGIS data (coordinates + substation attributes) is scraped programmatically:

```bash
# Substation load profile layer (layer 2) — coordinates only, values are in Amps
python scripts/scrape_sce.py layer --layer-id 2

# Substation physical attributes (ICA Table 3)
python scripts/scrape_sce.py attributes
```

Output: `data/raw/sce/sce_bulk_download_all.csv`, `sce_layer2_*.csv`,
`sce_substation_attributes.csv`

> **Note on SCE units**: The ArcGIS layer 2 returns load values in **Amps**, not MW.
> The DRPEP bulk download returns MW directly and is the authoritative load source.
> The ArcGIS layer is retained only for substation coordinates.

#### SDG&E

```bash
# Hourly load profiles (ZIP download per substation)
python scripts/scrape_sdge.py substation-profiles

# Substation physical attributes
python scripts/scrape_sdge.py attributes
```

Output: `data/raw/sdge/sdge_substation_profiles_part*.csv`,
`sdge_substation_attributes.csv`, `sdge_substation_profiles_failed.csv`
(substations with no published data receive a graceful failure entry)

#### PacifiCorp

```bash
# Substation names and coordinates (layer 1)
python scripts/scrape_pacificorp.py layer --layer-id 1

# DER attributes from DG Readiness service (circuit-level, aggregated to substation)
python scripts/scrape_pacificorp.py attributes
```

Output: `data/raw/pacificorp/pacificorp_layer1_*.csv`,
`pacificorp_substation_attributes.csv`

#### CalPeco / BVES

No public data source has been identified for either utility.  Placeholder scripts
(`scrape_calpeco.py`, `scrape_bves.py`) and empty raw data directories are in place
for future work.

---

### Step 2 — Process into unified outputs

#### Substation tables

```bash
python scripts/process_substations.py
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
python scripts/process_eia_interchange.py
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

---

### Step 3 — Validate and audit

```bash
# Check SCE data for schema consistency, row-count completeness, and duplicate hours
python scripts/validate_sce.py

# Report which raw columns are unused in the processed output
python scripts/audit_unused_columns.py
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
