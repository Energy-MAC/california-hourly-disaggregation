IEPR (Integrated Energy Policy Report) Demand Forecast Data
============================================================

Source
------
California Energy Commission (CEC) IEPR page:
  https://www.energy.ca.gov/data-reports/reports/integrated-energy-policy-report-iepr/

Files are published annually (CEC designation: CED = California Energy Demand).
Download the relevant year's "Hourly Demand Forecast Files" and
"Baseline Demand Forecast Files" and place them in a year-named subdirectory:

  data/raw/iepr/2023/   <- 2023 IEPR files
  data/raw/iepr/2024/   <- 2024 IEPR files
  data/raw/iepr/2025/   <- 2025 IEPR files
  ...

The forecast_vintage_year is taken from the subdirectory name, not the filename.
Do not rename the files themselves; the processing script handles CEC naming
variations across years (CED vs CEDU prefix, correction suffixes, etc.).

File naming patterns the script recognises:
  *Hourly Forecast - {TAC} - {SCENARIO}*.xlsx
  *Baseline Forecast - {UTILITY}*.xlsx


File inventory (CED 2025, downloaded January 2026)
---------------------------------------------------

Hourly Demand Forecast Files — per TAC area, per scenario:
  TACs:      PGE, SCE, SDGE, VEA, CAISO
  Scenarios: Local_Reliability, Local_Reliability_plusKnown, Planning_Scenario
  Sheet:     "Data"  —  227,760 rows (8,760 h/yr x 26 yrs, 2025-2050), 21 columns
             "Notes" —  column definitions (informational, not parsed)

  CAISO = PGE + SCE + SDGE + VEA exactly.  The processing script excludes CAISO
  rows to prevent double-counting; include CAISO explicitly if system-level totals
  are needed separately.

Baseline Demand Forecast Files — per utility/BA:
  BUG, IID, LADWP, NCNC, PGE, SCE, SDGE, SMUD
  Plus a "Total State" summary (excluded from processing: missing Form 1.5).

  Relevant forms extracted by the processing script:
    Form 1.2 — Total Energy to Serve Load (GWh, annual), 2000-2045
               Includes line losses and distributed self-generation;
               the most transmission-relevant energy metric.
    Form 1.5 — Non-coincident Peak Demand (MW, annual), 2000-2045
               Historical observed peaks (2000 to last historical year)
               and probabilistic forecasts: 1-in-2, 1-in-5, 1-in-10, 1-in-20.
               Missing from the Total State file; available only per-utility.

  Forms NOT extracted (available in raw files):
    Form 1.1  — GWh consumption by sector (Residential, Commercial, etc.)
    Form 1.1b — GWh sales by sector
    Form 2.2  — Economic and demographic assumptions
    Form 2.3  — Electricity rates by sector (cent/kWh)

  CED 2025 Baseline Natural Gas Forecast: skipped (gas-specific, not electricity).


Scenario definitions (from IEPR 2025 methodology)
--------------------------------------------------
  Local_Reliability         Historical BTM PV growth; conservative AAEE/AAFS/AATE.
                            Used for local area resource adequacy and T&D planning.
  Local_Reliability_plusKnown
                            Same as Local_Reliability plus load from committed large
                            load interconnection requests (data centers, industrial).
                            Represents near-term buildout of known load additions.
  Planning_Scenario         Accelerated BTM PV growth; more aggressive AAEE/AAFS/AATE.
                            Used for statewide capacity and long-term resource planning.

  Key differences between scenarios per IEPR Table 2:
    BTM_PV             Planning > Local_Reliability (more rooftop solar penetration)
    DATA_CENTER        plusKnown > Local_Reliability (adds committed interconnections)
    AAEE/AAFS/AATE     Planning scenario applies higher efficiency/fuel-switching targets


Column definitions (Hourly Forecast outputs, all values in MW)
--------------------------------------------------------------
  UNADJUSTED_CONSUMPTION   modelled load including BTM PV estimate
  PUMPING                  pump load adjustment (agriculture, water conveyance)
  CLIMATE_CHANGE           climate change temperature adder
  LIGHT_EV                 light-duty EV charging load
  MEDIUM_HEAVY_EV          medium/heavy-duty EV charging load
  DATA_CENTER              incremental data center load
  OTHER_ADJUSTMENTS        all remaining modifiers
  BASELINE_CONSUMPTION     sum of unadjusted + all modifiers
  BTM_PV                   behind-the-meter solar (negative: reduces grid load)
  BTM_STORAGE_RES          residential BTM battery storage
  BTM_STORAGE_NONRES       non-residential BTM battery storage
  BASELINE_NET_LOAD        BASELINE_CONSUMPTION + BTM effects (what the grid sees)
  AAEE                     additional achievable energy efficiency reduction
  AAFS                     additional achievable fuel substitution
  AATE_LDV                 advanced transportation – light-duty vehicle managed charging
  AATE_MDHD                advanced transportation – medium/heavy-duty managed charging
  MANAGED_NET_LOAD         BASELINE_NET_LOAD + managed program adjustments
                           Primary output; most comparable to observed system load.

  HOUR is 1-24 (hour-ending PST).  The processed CSV adds 'hour' (= HOUR - 1,
  range 0-23, hour-beginning) for direct joins with substation load profiles.


Outputs (produced by scripts/process_iepr.py)
---------------------------------------------
  data/processed/iepr/iepr_hourly_forecast.csv
    ~2.7M rows (4 TACs x 3 scenarios x 227,760 hourly rows per file).
    Columns: forecast_vintage_year, utility_ba, scenario,
             YEAR, MONTH, DAY, HOUR, hour, <17 load columns>

  data/processed/iepr/iepr_baseline_annual.csv
    ~1,840 rows (8 utilities x 46 years x 5 forms merged per year).
    Columns: forecast_vintage_year, utility_ba, Year,
             <Form 1.2 GWh columns>, <Form 1.5 MW columns>


Multi-vintage usage (historical forecasts back to 2003)
-------------------------------------------------------
  Download earlier IEPR files from the CEC archive, place each vintage year in
  its own subdirectory: data/raw/iepr/2022/, data/raw/iepr/2021/, etc.
  The processing script reads forecast_vintage_year from the directory name and
  handles structural differences across vintages automatically:
    - DATA_CENTER column absent pre-2024 -> filled NaN
    - Local_Reliability_plusKnown scenario absent pre-2025 -> simply not present
    - VEA planning area absent pre-2025 -> simply not present
    - BUGL utility name (2023/2024) normalised to BUG (2025 spelling)
    - Trailing filename suffixes (Corrected, correction 32025, etc.) stripped

  To compare forecast vs realized demand:
    - Filter iepr_hourly_forecast.csv by forecast_vintage_year (e.g. 2020)
    - Compare MANAGED_NET_LOAD for a given YEAR (e.g. 2023) against observed
      substation data in data/processed/substations/substation_load_profiles_clean.csv
    - Use BASELINE_NET_LOAD when BTM solar/storage is tracked separately
