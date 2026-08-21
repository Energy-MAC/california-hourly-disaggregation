# Data Pipeline

Full scrape → process → validate reference, plus the time-zone/DST conventions, data
quality notes, and notebooks. The README carries a "pipeline at a glance"; this document
holds the per-source commands and the processed-output column dictionaries. Source
descriptions are in [data_sources.md](data_sources.md).

All scrapers support **safe stop/resume**: press `Ctrl+C` and re-run to continue.

---

## Where this is implemented

| Stage | Entry point | Notes |
|---|---|---|
| Scrape (per source) | `scripts/data/{pge,sce,sdge,eia,iepr,resolve,reeds}/` | each has its own CLI; see Step 1 |
| Unified substation build | `data/substations/process_substations_clean.py` | writes both `substation_attributes_clean.csv` and `substation_load_profiles_clean.csv` |
| Substation → county | `data/substations/assign_substation_counties.py` | point-in-polygon, TIGER 2022 |
| Coordinate overrides + their check | `apply_coordinate_overrides()`, `check_coordinate_overrides.py` | `data/substations/` |
| Timezone conversion to fixed PST | the `pdt_mask` block in `process_substations_clean.main()` | majority-month rule, see "Time zone" below |
| Rankings | `shared/rank_substations.py` | prerequisite for both approaches |

## Step 1 — Scrape raw data

### EIA 930

```bash
# Primary (PUDL — recommended; no API key)
python scripts/data/eia/ingest_eia_pudl.py   # → data/raw/eia/pudl/ (operations + interchange parquets)

# Secondary (EIA API direct — CAL region + validation only; needs EIA_API_KEY)
python scripts/data/eia/scrape_eia.py rto-interchange --from-bas BANC CISO IID LDWP PACW NEVP TIDC WALC
python scripts/data/eia/scrape_eia.py rto-interchange --to-bas   BANC CISO IID LDWP PACW NEVP TIDC WALC
python scripts/data/eia/scrape_eia.py rto-region              # CAL aggregate, 2019–present
```

### PG&E

```bash
python scripts/data/pge/scrape_pge.py layer --layer-id 25     # feeder load profiles (primary load source)
python scripts/data/pge/scrape_pge.py attributes              # substation physical attributes (layer 0)
```

### SCE

Load profiles come from the DRPEP bulk download (manual):
1. <https://drpep.sce.com/drpep/> → **Bulk Download → Historical Substation Load Profiles → Download All**
2. `python scripts/data/sce/ingest_sce_bulk_download.py path/to/SUBSTATION.zip`

ArcGIS (coordinates + attributes) is scraped:
```bash
python scripts/data/sce/scrape_sce.py layer --layer-id 2      # coordinates only (values are Amps — not used for load)
python scripts/data/sce/scrape_sce.py attributes              # ICA Table 3
```
> SCE ArcGIS layer 2 returns **Amps**, not MW. The DRPEP bulk download (MW) is the
> authoritative load source; the ArcGIS layer is kept only for coordinates.

### SDG&E

```bash
python scripts/data/sdge/scrape_sdge.py substation-profiles   # hourly profiles (ZIP per substation)
python scripts/data/sdge/scrape_sdge.py attributes
```

### PacifiCorp

```bash
python scripts/data/pacificorp/scrape_pacificorp.py layer --layer-id 1   # names + coordinates
python scripts/data/pacificorp/scrape_pacificorp.py attributes           # DG Readiness (circuit → substation)
```

### EIA Form 861 (annual retail sales)

```bash
python scripts/data/eia/ingest_eia861_pudl.py                       # Option A (PUDL, recommended)
python scripts/data/eia/scrape_eia_form861.py --years 2022 2023 2024 # Option B (direct EIA ZIP)
```

### CalPeco / BVES

No public source identified. Placeholder scripts (`scrape_calpeco.py`, `scrape_bves.py`)
and empty raw directories are in place for future work.

---

## Step 2 — Process into unified outputs

### Substation tables

```bash
python scripts/data/substations/process_substations.py         # raw merge → substation_attributes.csv (2,614), substation_load_profiles.csv (455,568)
python scripts/data/substations/process_substations_clean.py   # cleaned, analysis-ready
```

`process_substations_clean.py` applies filtering, deduplication, coordinate enrichment,
and DST correction. Filtering: P.T. (pass-through switching) substations removed (170
SCE, 8 SDGE); PacifiCorp excluded (no metered load profiles); SCE deduplication (bulk
download preferred; most-recent year-vintage per cell); SDGE kW→MW.

**`substation_attributes_clean.csv`** — 1,347 substations (PGE 670 · SCE 578 · SDGE 99):

| Column | Description |
|--------|-------------|
| utility | `pge`, `sce`, or `sdge` |
| substation_name | Name as reported by the utility |
| util_lat, util_lon | Utility-source coordinates (primary) |
| basin_lat, basin_lon | DataBasin 2022 coordinates (fallback) |
| dist_to_basin_km | Haversine distance between util and basin coordinate |
| sub_type | Substation type (Distribution / Transmission) |
| substation_voltage, voltage_kv | Transformer-ratio string (SCE/SDGE) + numeric low-side kV — NOT a transmission rating |
| highside_kv | High-side (transmission) voltage: `substation_voltage`'s first token (SCE/SDGE) else CEC `max_voltage_kv` (PGE's only source) |
| highside_kv_source | `utility` \| `cec` \| `none` |
| cec_max_voltage_kv | Raw CEC value backing `highside_kv` when CEC-sourced |
| sys_name | SCE system/circuit area name |
| division | PG&E service division |
| subst_id | Internal substation ID (PG&E, SCE) |
| existing_gen / queued_gen / total_gen | DER/gen capacity (MW) |
| projected_load / der_penetration / max_remain_cap | SCE/SDGE hosting-capacity fields |
| circuit_count | Distribution circuits (PG&E = transformer banks) |
| res/com/agr/ind/other_pct, _total | SCE customer-class shares/counts |
| note_sub | Data quality flag |

**`substation_load_profiles_clean.csv`** — 387,864 rows:

| Column | Description |
|--------|-------------|
| utility | `pge`, `sce`, or `sdge` |
| substation_name | Matches attributes_clean |
| year | SCE vintage year (2017–2026); NaN for PGE/SDGE (no year stamp) |
| month | 1–12 |
| hour | 0–23, original wall-clock Pacific (PDT summer / PST winter) |
| hour_pst | 0–23, **fixed PST (UTC−8, no DST)** — use this for all comparisons |
| min_load | ~10th-percentile load for that (month, hour) cell (MW) |
| max_load | ~90th-percentile load for that (month, hour) cell (MW) |

### Substation → county → ReEDS-region mapping

```bash
python scripts/data/reeds/process_county_disaggregation.py     # county_ca_reference.csv (58 rows)
python scripts/data/substations/assign_substation_counties.py  # substation_county_reeds_mapping.csv (1,329 rows)
```

`county_ca_reference.csv` (58 CA counties) from `county2zone.csv` (county→p-region),
`county_state_lpf.csv` (load participation factors), `distpvcap_stscen2023_mid_case.csv`
(county BTM PV by year 2010–2050). Columns: fips_int, fips_key, county_name, p_region,
`ca_load_fraction` (sums to 1.0 over 58 counties), `btm_pv_{year}_mw`. Distribution: p9 =
44 counties (37.4% of CA load), p10 = 10 (55.2%), p11 = 1 (7.1%), p8 = 3 (0.3%, PacifiCorp).

`substation_county_reeds_mapping.csv` (1,329 substations; 12 excluded for missing coords):
spatial join against TIGER 2022 county shapefile, merged to the county reference. All
1,329 fall in p9/p10/p11; none in p8. Columns: utility, substation_name, lat, lon,
coord_source (`util` 1,320 / `basin` 9), fips_int/fips_key, county_name, p_region,
ca_load_fraction, btm_pv_{year}_mw.

### EIA interchange

```bash
python scripts/data/eia/process_eia_interchange.py             # eia_interchange.csv (~3.1M rows)
```

Standardizes FROM/TO files so the CA BA is always `fromba`: FROM records (fromba ∈ CA-8)
kept; TO records where fromba ∉ CA-8 sign-flipped and swapped; TO where fromba ∈ CA-8
(duplicate of FROM) dropped; both files trimmed to the earlier endpoint. Columns: period,
fromba, fromba-name, toba, toba-name, value (MWh net export, +export/−import), value-units.

### RESOLVE load inputs

```bash
python scripts/data/resolve/process_resolve.py
```

Reads RESOLVE's full 8,760-hour Baseline load profiles (no model run needed — the Outputs
directory holds only 36 representative dispatch windows; the full profiles exist as
inputs) and applies annual scaling. Per weather year:
`scale_factor = annual_energy_forecast_MWh[2024] / Σ(profile_MW × 1h)`; every hour ×
scale_factor. Shape from historical weather years; magnitude anchored to the IEPR-derived
forecast. Full BTM/overlay treatment is in
[statewide_forecast_sources.md](statewide_forecast_sources.md).

**`resolve_hourly_profiles.csv`** — six BA zones (PGE, SCE, SDGE, IID, LDWP, NCNC), 23
weather years (2000–2022) × 8,760 h. Columns: datetime_pst (fixed PST), utility,
demand_mw_raw, demand_mw_2024scaled (gross), btm_pv_mw, demand_mw_net (net-of-BTM-PV).
**`resolve_annual_forecast.csv`** — annual energy targets (MWh/TWh) by utility and year
(2024–2045).

### ReEDS California load

```bash
python scripts/data/reeds/process_reeds.py            # reeds_ca_load_hourly.parquet (7.6M rows), reeds_ca_load_annual.csv
python scripts/data/reeds/process_historic_load.py    # historic_ca_load_hourly.parquet (70,080 rows), historic_ca_load_annual.csv
```

`reeds_ca_load_annual.csv` includes a `CA_total` row; IRA_low CA total grows ~291 TWh
(2020) → ~525 TWh (2050) — higher than other CA sources because ReEDS covers all of CA,
models electrification explicitly, and reports gross load.

### EIA Form 861 — CA fractions by BA

```bash
python scripts/data/eia/process_eia861.py             # auto-detects PUDL parquet or EIA Excel
```

→ `eia861_ca_fractions.csv`: year, ba_code, total_mwh, ca_mwh, `ca_fraction = ca_mwh /
total_mwh`. Used to partition EIA-930 hourly BA demand into in-CA vs out-of-CA portions.

---

## Step 3 — Validate and audit

```bash
python scripts/data/compare_eia_sources.py            # cross-validate PUDL vs EIA scrape (all sections)
python scripts/data/compare_eia_sources.py -s D       # NaN audit only (no EIA scrape file needed)
python scripts/data/sce/validate_sce.py               # SCE schema / completeness / duplicate hours
python scripts/data/substations/audit_unused_columns.py  # which raw columns are unused downstream
```

---

## Notebooks

| Notebook | Purpose |
|----------|---------|
| `01_eia_from_to_consistency.ipynb` | Cross-file misreporting check: does `A→B` in FROM agree with `B→A` in TO? |
| `02_eia_region_vs_interchange.ipynb` | EIA CAL region total interchange (type=TI) vs sum of per-BA flows. |

---

## Time zone and DST conventions

**Canonical comparison format** (all compare scripts): **fixed PST (UTC−8),
hour-beginning, hours 0–23**.

| File | Time zone | Raw hour label | Conversion to canonical | DST? |
|------|-----------|---------------|--------------------------|------|
| `eia930_operations.csv` (PUDL) | UTC | hour-ending (`datetime_utc`) | subtract **9h** (`_utc_to_pst()`) | n/a |
| `eia_region.csv` (EIA API) | UTC | hour-ending, `YYYY-MM-DDTHH` | subtract 9h | n/a |
| `iepr_hourly_forecast.csv` | Fixed PST | hour-ending, 1–24 (`HOUR`) | `hour0 = HOUR − 1` | No |
| `resolve_hourly_profiles.csv` | Fixed PST | hour-beginning 0–23 (`datetime_pst`) | none | No |
| `substation_load_profiles_clean.csv` | Fixed PST | hour-beginning 0–23 (`hour_pst`) | none | No |
| `reeds_ca_load_hourly.parquet` | Fixed PST | hour-beginning 0–23 (`hour`, int8) | none (cast int8→int64 first) | No |
| `substation_load_profiles.csv` (raw) | Wall-clock Pacific | hour-beginning 0–23 | majority-month rule → `hour_pst` | Yes |

**Why EIA needs −9h, not −8h:** the UTC timestamp marks the *end* of the integration
period. `T06:00:00Z` = 05:00–06:00 UTC = hour ending 1 AM EST. Subtracting 9h gives 21:00
PST, the correct **start** label. Annual/monthly totals unaffected; only hourly peak
analysis is sensitive. EIA API and PUDL agree to < 0.001% at identical UTC timestamps.

**IEPR column naming:** the processed IEPR file uses `hour0` (= HOUR − 1); joins against
other sources use `left_on=["month","hour0"], right_on=["month","hour"]`.

**Substation DST (majority-month rule):** raw scrapes report wall-clock Pacific (PDT
Mar–Oct, PST otherwise). The clean file shifts months 3–10 back 1 hour, leaves 1/2/11/12
unchanged. This is a deliberate approximation — the min/max profiles are percentile
envelopes over a non-public window, so individual timestamps have no verifiable DST
status; the rule introduces at most a 1-hour error in the two transition months.

```python
pdt_mask = loads_all["month"].isin(range(3, 11))
loads_all["hour_pst"] = loads_all["hour"].where(~pdt_mask, (loads_all["hour"] - 1) % 24)
```

**Converting between conventions:**

| From → To | Operation |
|-----------|-----------|
| EIA UTC hour-ending → PST hour-beginning | `ts − 9h` |
| IEPR hour-ending PST (1–24) → hour-beginning (0–23) | `hour0 = HOUR − 1` |
| ReEDS CST (UTC−6) → PST (UTC−8) | `ts − 2h` (in `process_reeds.py`) |
| PST hour-beginning → UTC | `ts + 8h` (result is hour-beginning UTC, not hour-ending) |

---

## Notes on data quality

- **EIA FROM vs TO**: FROM and TO files hold identical values for the same `(period,
  fromba, toba)` — EIA does not measure each endpoint independently. Reporting
  discrepancies are visible only by comparing `A→B` in FROM with `B→A` in TO (~13% of
  inter-CA-8 pairs differ by > 1 MWh).
- **PacifiCorp coverage**: DG Readiness covers only Pacific Power's distribution
  territory. Only ~168 of 1,142 scraped substations have DER attributes; the rest have
  location + name only.
- **SCE unit discrepancy**: ArcGIS layer 2 returns Amps; DRPEP bulk download returns MW.
  Amps→MW via nominal voltage was tested and found inaccurate. All SCE load comes from the
  bulk download.
- **PG&E years**: PG&E feeder profiles are monthly aggregates without a year column; the
  `year` field is `NaN` for all PG&E rows.
