"""
Process CEC IEPR peak forecast workbook into tidy CSVs.

Source
------
    data/raw/iepr/TN262286_20250321T133815_CED 2024 Peak Forecast - correction 32025.xlsx
    Sheet: monthly_peak_days

Sheet structure
---------------
    One row per (TAC, SCENARIO, COINCIDENT, YEAR, MONTH, HOUR).
    HOUR is 1-24, hour-ending Pacific Standard Time.

    TAC values
    ----------
    CAISO  — full CAISO system (always COINCIDENT=True)
    PGE    — PG&E transmission access charge area
    SCE    — SCE transmission access charge area
    SDGE   — SDG&E transmission access charge area

    COINCIDENT flag
    ---------------
    True   — row represents the TAC load on the *CAISO system peak day* for that month
    False  — row represents the TAC load on its own *TAC-local peak day* for that month
    (CAISO rows are always COINCIDENT=True; CAISO is both the system and its own TAC.)

    Scenarios
    ---------
    Local_Reliability  — used for local area reliability planning
    Planning_Scenario  — statewide capacity planning scenario

    Load columns (all in MW at the CAISO system level, including line losses)
    -------------------------------------------------------------------------
    UNADJUSTED_CONSUMPTION   — modelled load including behind-the-meter PV estimate
    PUMPING                  — pump load adjustment
    CLIMATE_CHANGE           — climate change adder
    LIGHT_EV                 — light-duty EV load
    MEDIUM_HEAVY_EV          — medium/heavy-duty EV load
    DATA_CENTER              — data center load (new for IEPR 2024)
    OTHER_ADJUSTMENTS        — all other modifiers
    BASELINE_CONSUMPTION     — sum of above (= UNADJUSTED + all modifiers)
    BTM_PV                   — behind-the-meter solar (negative = reduces grid load)
    BTM_STORAGE_RES          — residential BTM storage
    BTM_STORAGE_NONRES       — non-residential BTM storage
    BASELINE_NET_LOAD        — BASELINE_CONSUMPTION + BTM effects
    AAEE                     — additional achievable energy efficiency
    AAFS                     — additional achievable fuel substitution
    AATE_LDV                 — advanced and alternative transportation (light-duty)
    AATE_MDHD                — advanced and alternative transportation (medium/heavy)
    MANAGED_NET_LOAD         — BASELINE_NET_LOAD + managed program adjustments
                               (primary forecast output; most comparable to observed load)

Comparison guidance
-------------------
    Substation load profiles use hour 0-23 (hour-beginning convention).
    IEPR uses hour 1-24 (hour-ending).  This script adds a `hour` column
    (= HOUR - 1, range 0-23) for direct joins with substation data.

    For comparing against substation max_load:
      BASELINE_NET_LOAD is the closest match — it includes BTM solar/storage
      effects, which are already embedded in metered substation loads.
      BASELINE_CONSUMPTION is appropriate when BTM is treated separately.
      MANAGED_NET_LOAD includes demand-response and EE program adjustments
      that are not visible at the substation meter.

Output
------
    data/processed/iepr/iepr_monthly_peak_days.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT    = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "iepr"
OUT_DIR = ROOT / "data" / "processed" / "iepr"

_WORKBOOK = "TN262286_20250321T133815_CED 2024 Peak Forecast - correction 32025.xlsx"
_SHEET    = "monthly_peak_days"

# All load component and summary columns from the sheet
_LOAD_COLS = [
    "UNADJUSTED_CONSUMPTION",
    "PUMPING",
    "CLIMATE_CHANGE",
    "LIGHT_EV",
    "MEDIUM_HEAVY_EV",
    "DATA_CENTER",
    "OTHER_ADJUSTMENTS",
    "BASELINE_CONSUMPTION",
    "BTM_PV",
    "BTM_STORAGE_RES",
    "BTM_STORAGE_NONRES",
    "BASELINE_NET_LOAD",
    "AAEE",
    "AAFS",
    "AATE_LDV",
    "AATE_MDHD",
    "MANAGED_NET_LOAD",
]


def process_monthly_peak_days(out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    workbook = RAW_DIR / _WORKBOOK
    if not workbook.exists():
        raise FileNotFoundError(
            f"Workbook not found: {workbook}\n"
            "Download from https://www.energy.ca.gov/data-reports/reports/"
            "integrated-energy-policy-report-iepr/ and place in data/raw/iepr/"
        )

    print(f"Reading {_SHEET} from {workbook.name} ...")
    df = pd.read_excel(workbook, sheet_name=_SHEET, header=0)
    print(f"  {len(df):,} rows loaded")
    print(f"  TACs      : {sorted(df['TAC'].unique())}")
    print(f"  Scenarios : {sorted(df['SCENARIO'].unique())}")
    print(f"  Years     : {int(df['YEAR'].min())} - {int(df['YEAR'].max())}")

    # Add 0-indexed hour column to match substation load profile convention (0-23)
    # IEPR HOUR is 1-24 (hour-ending PST); hour 1 = 00:00-01:00 = substation hour 0
    df["hour"] = df["HOUR"] - 1

    # Canonical column order: identifiers, then both hour conventions, then loads
    id_cols  = ["TAC", "SCENARIO", "COINCIDENT", "YEAR", "MONTH", "DAY", "HOUR", "hour"]
    out_cols = id_cols + _LOAD_COLS

    result = df[out_cols].sort_values(
        ["SCENARIO", "TAC", "COINCIDENT", "YEAR", "MONTH", "HOUR"]
    ).reset_index(drop=True)

    out_path = out_dir / "iepr_monthly_peak_days.csv"
    result.to_csv(out_path, index=False)
    mb = out_path.stat().st_size / 1024 / 1024

    print(f"\nWrote {len(result):,} rows -> {out_path}  ({mb:.1f} MB)")
    print()
    print("Row counts by TAC and COINCIDENT:")
    print(
        result.groupby(["TAC", "COINCIDENT"])
        .size()
        .rename("rows")
        .reset_index()
        .to_string(index=False)
    )
    return out_path


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    process_monthly_peak_days()
