"""
Process CEC IEPR demand forecast files into tidy CSVs.

Source
------
data/raw/iepr/{YYYY}/   (one subdirectory per forecast vintage year)
  e.g.  data/raw/iepr/2023/
        data/raw/iepr/2024/
        data/raw/iepr/2025/

The forecast_vintage_year is taken from the subdirectory name.
Files may be downloaded directly from the CEC IEPR page without renaming.
CEC naming conventions vary across years (CED vs CEDU prefix, trailing
correction suffixes like '- Corrected' or '- correction 32025') — these
are all handled automatically.

Expected file naming (CEC convention, date-agnostic across vintages):
  Hourly forecast:   *Hourly Forecast - {TAC} - {SCENARIO}*.xlsx
  Baseline forecast: *Baseline Forecast - {UTILITY}*.xlsx

TAC (Transmission Access Charge) areas
---------------------------------------
  PGE, SCE, SDGE, VEA
  CAISO files are excluded from output: CAISO load = PGE + SCE + SDGE + VEA
  exactly, so including both would double-count when summing across utilities.

Scenarios (Hourly Forecast files)
----------------------------------
  Local_Reliability          — local area reliability planning baseline
  Local_Reliability_plusKnown — adds committed large-load interconnections
  Planning_Scenario          — statewide capacity planning scenario
  Scenario definitions and BTM/AAEE/AAFS/AATE assumptions are in Table 2 of
  the IEPR methodology document (see data/raw/iepr/ for the PDF).

Baseline forms extracted
------------------------
  Form 1.2 — Total Energy to Serve Load (GWh), includes line losses and
             self-generation; most relevant for transmission delivery planning
  Form 1.5 — Non-coincident Peak Demand (MW): historical observed peaks and
             probabilistic forecasts (1-in-2, 1-in-5, 1-in-10, 1-in-20 year)
  Total State file is skipped: it is missing Form 1.5 (peak demand).
  Per-utility Form 1.5 values are provided directly without aggregation.

Outputs
-------
  data/processed/iepr/iepr_hourly_forecast.csv
    One row per (forecast_vintage_year, utility_ba, scenario, YEAR, MONTH, DAY, HOUR).
    All 17 load component columns in MW (hour-ending PST, HOUR=1..24).
    Added 'hour' column (= HOUR - 1, range 0..23) for joins with substation data.
    Older vintages missing a column (e.g. DATA_CENTER pre-2024) carry NaN.

  data/processed/iepr/iepr_baseline_annual.csv
    One row per (forecast_vintage_year, utility_ba, Year).
    Form 1.2 columns in GWh; Form 1.5 columns in MW.
    Historical years (before last historical year) have NaN in Forecasted_*
    columns — those cells are blank in the source (no probabilistic forecast
    for observed years).

Column definitions (Hourly Forecast)
-------------------------------------
  UNADJUSTED_CONSUMPTION   modelled load including BTM PV estimate
  PUMPING                  pump load adjustment
  CLIMATE_CHANGE           climate change adder
  LIGHT_EV                 light-duty EV load
  MEDIUM_HEAVY_EV          medium/heavy-duty EV load
  DATA_CENTER              data center load (added IEPR 2024)
  OTHER_ADJUSTMENTS        all other modifiers
  BASELINE_CONSUMPTION     sum of above (unadjusted + modifiers)
  BTM_PV                   behind-the-meter solar (negative = reduces grid load)
  BTM_STORAGE_RES          residential BTM storage
  BTM_STORAGE_NONRES       non-residential BTM storage
  BASELINE_NET_LOAD        BASELINE_CONSUMPTION + BTM effects
  AAEE                     additional achievable energy efficiency
  AAFS                     additional achievable fuel substitution
  AATE_LDV                 advanced transportation – light-duty vehicles
  AATE_MDHD                advanced transportation – medium/heavy-duty
  MANAGED_NET_LOAD         BASELINE_NET_LOAD + managed program adjustments
                           (primary forecast output; most comparable to observed load)

Comparison with substation data
---------------------------------
  Substation load profiles use hour 0-23 (hour-beginning PST).
  IEPR uses HOUR 1-24 (hour-ending PST).  The 'hour' column (= HOUR - 1)
  enables direct joins.
  For matching against substation max_load:
    BASELINE_NET_LOAD — includes BTM PV/storage already embedded in meter reads
    BASELINE_CONSUMPTION — appropriate when BTM is modelled separately
    MANAGED_NET_LOAD — includes demand-response/EE programs not visible at meter
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

ROOT    = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "iepr"
OUT_DIR = ROOT / "data" / "processed" / "iepr"

_HOURLY_LOAD_COLS = [
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

_FORM_1_2_COLS = [
    "Total_Consumption",
    "Losses",
    "Gross_Generation",
    "Other_Self_Generation",
    "PV_Generation",
    "Total_Self_Generation",
    "Total_Energy_to_Serve_Load",
]

_FORM_1_5_COLS = [
    "Historical_Net_Peak",
    "Forecasted_1.in.2_Peak",
    "Forecasted_1.in.5_Peak",
    "Forecasted_1.in.10_Peak",
    "Forecasted_1.in.20_Peak",
]

_HOURLY_OUT_COLS = (
    ["forecast_vintage_year", "utility_ba", "scenario",
     "YEAR", "MONTH", "DAY", "HOUR", "hour"]
    + _HOURLY_LOAD_COLS
)

_BASELINE_OUT_COLS = (
    ["forecast_vintage_year", "utility_ba", "Year"]
    + _FORM_1_2_COLS
    + _FORM_1_5_COLS
)

_SKIP_UTILITIES = {"TOTAL STATE"}

# Utility names that changed across vintages; normalize to the 2025 spelling.
# 2023/2024 files call it "BUGL"; 2025 calls it "BUG".
_UTILITY_NORM = {
    "BUGL": "BUG",
}

# Known canonical scenario names (used to strip trailing correction suffixes).
_CANONICAL_SCENARIOS = {
    "LOCAL_RELIABILITY",
    "LOCAL_RELIABILITY_PLUSKNOWN",
    "PLANNING_SCENARIO",
}


def _vintage_from_path(path: Path) -> int | None:
    """Return vintage year from the parent directory name (e.g. .../2025/file.xlsx -> 2025).
    Falls back to parsing the filename if the directory name is not a 4-digit year."""
    dir_name = path.parent.name
    if dir_name.isdigit() and len(dir_name) == 4:
        return int(dir_name)
    # Fallback: match any of CED YYYY or CEDU YYYY in the filename
    m = re.search(r"CED[U]?\s+(\d{4})", path.stem, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_hourly_fname(path: Path) -> tuple[int, str, str] | None:
    """Return (vintage_year, utility_ba, scenario) or None if file should be skipped."""
    vintage = _vintage_from_path(path)
    if vintage is None:
        return None

    m = re.search(r"Hourly Forecast - ([A-Za-z]+) - (.+)", path.stem, re.IGNORECASE)
    if not m:
        return None

    utility = m.group(1).upper()
    if utility == "CAISO":
        return None  # CAISO = PGE + SCE + SDGE + VEA; excluded to avoid double-counting

    # Strip trailing correction suffixes: take only the first " - "-delimited token
    # after the TAC, then normalize spaces to underscores.
    # e.g. "Local_Reliability - Corrected"       -> "Local_Reliability"
    #      "Planning_Scenario - correction 32025" -> "Planning_Scenario"
    #      "Local Reliability - Corrected"        -> "Local_Reliability"
    scenario_raw = m.group(2).strip().split(" - ")[0].strip()
    scenario = scenario_raw.replace(" ", "_")

    return vintage, utility, scenario


def _parse_baseline_fname(path: Path) -> tuple[int, str] | None:
    """Return (vintage_year, utility_ba) or None if file should be skipped."""
    vintage = _vintage_from_path(path)
    if vintage is None:
        return None

    m = re.search(r"Baseline Forecast - (.+)", path.stem, re.IGNORECASE)
    if not m:
        return None

    utility = m.group(1).strip().upper()
    utility = _UTILITY_NORM.get(utility, utility)
    if utility in _SKIP_UTILITIES:
        return None
    return vintage, utility


def _read_baseline_form(
    path: Path, sheet: str, data_cols: list[str]
) -> pd.DataFrame:
    """Read a standard baseline form sheet (header row 4, data from row 5)."""
    raw = pd.read_excel(path, sheet_name=sheet, header=None)
    headers = [str(h) for h in raw.iloc[4]]
    raw.columns = headers
    data = raw.iloc[5:].copy()
    # Drop footnote rows (non-numeric Year column)
    data = data[data["Year"].astype(str).str.match(r"^\d{4}$")].copy()
    data["Year"] = data["Year"].astype(int)
    data = data.reset_index(drop=True)
    keep = ["Year"] + [c for c in data_cols if c in data.columns]
    return data[keep]


def process_hourly(out_dir: Path = OUT_DIR) -> Path:
    """Read all per-utility hourly forecast files and write combined CSV."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(RAW_DIR.glob("*/*.xlsx"))
    frames: list[pd.DataFrame] = []

    for f in files:
        meta = _parse_hourly_fname(f)
        if meta is None:
            continue
        vintage, utility, scenario = meta
        print(f"  {utility:6s}  {scenario:35s}  vintage={vintage}  ({f.name[:50]}...)")
        df = pd.read_excel(f, sheet_name="Data", header=0)
        df.insert(0, "forecast_vintage_year", vintage)
        df.insert(1, "utility_ba", utility)
        df.insert(2, "scenario", scenario)
        df["hour"] = df["HOUR"] - 1  # 0-indexed for substation joins
        frames.append(df)

    if not frames:
        raise FileNotFoundError(
            f"No hourly forecast files found in {RAW_DIR}. "
            "Download from the CEC IEPR page and rename to match: "
            "'CED YYYY Hourly Forecast - TAC - SCENARIO.xlsx'"
        )

    result = (
        pd.concat(
            [fr.reindex(columns=_HOURLY_OUT_COLS) for fr in frames],
            ignore_index=True,
        )
        .sort_values(
            ["utility_ba", "scenario", "forecast_vintage_year", "YEAR", "MONTH", "DAY", "HOUR"]
        )
        .reset_index(drop=True)
    )

    out_path = out_dir / "iepr_hourly_forecast.csv"
    result.to_csv(out_path, index=False)
    mb = out_path.stat().st_size / 1024 / 1024

    print(f"\nWrote {len(result):,} rows  ->  {out_path}  ({mb:.1f} MB)")
    print("\nRow counts by utility_ba / scenario / forecast_vintage_year:")
    print(
        result.groupby(["utility_ba", "scenario", "forecast_vintage_year"])
        .size()
        .rename("rows")
        .reset_index()
        .to_string(index=False)
    )
    return out_path


def process_baseline(out_dir: Path = OUT_DIR) -> Path:
    """Read Form 1.2 and Form 1.5 from all per-utility baseline files and write combined CSV."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(RAW_DIR.glob("*/*.xlsx"))
    frames: list[pd.DataFrame] = []

    for f in files:
        meta = _parse_baseline_fname(f)
        if meta is None:
            continue
        vintage, utility = meta
        print(f"  {utility:10s}  vintage={vintage}  ({f.name[:50]}...)")

        try:
            f12 = _read_baseline_form(f, "Form 1.2", _FORM_1_2_COLS)
        except Exception as exc:
            print(f"    WARNING: Form 1.2 read failed: {exc}")
            f12 = pd.DataFrame({"Year": pd.Series([], dtype=int)})

        try:
            f15 = _read_baseline_form(f, "Form 1.5", _FORM_1_5_COLS)
        except Exception as exc:
            print(f"    WARNING: Form 1.5 read failed: {exc}")
            f15 = pd.DataFrame({"Year": pd.Series([], dtype=int)})

        merged = (
            pd.merge(f12, f15, on="Year", how="outer")
            .sort_values("Year")
            .reset_index(drop=True)
        )
        merged.insert(0, "forecast_vintage_year", vintage)
        merged.insert(1, "utility_ba", utility)
        frames.append(merged)

    if not frames:
        raise FileNotFoundError(
            f"No baseline forecast files found in {RAW_DIR}. "
            "Download from the CEC IEPR page and rename to match: "
            "'CED YYYY Baseline Forecast - UTILITY.xlsx'"
        )

    result = (
        pd.concat(
            [fr.reindex(columns=_BASELINE_OUT_COLS) for fr in frames],
            ignore_index=True,
        )
        .sort_values(["utility_ba", "forecast_vintage_year", "Year"])
        .reset_index(drop=True)
    )

    out_path = out_dir / "iepr_baseline_annual.csv"
    result.to_csv(out_path, index=False)
    mb = out_path.stat().st_size / 1024 / 1024

    print(f"\nWrote {len(result):,} rows  ->  {out_path}  ({mb:.1f} MB)")
    print("\nYear range and peak availability by utility_ba:")
    summary = result.groupby("utility_ba").agg(
        year_min=("Year", "min"),
        year_max=("Year", "max"),
        peak_rows=("Historical_Net_Peak", "count"),
        forecast_rows=("Forecasted_1.in.2_Peak", "count"),
    )
    print(summary.to_string())
    return out_path


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    print("=== Processing hourly forecast files ===")
    print("(Reading large Excel files — this may take several minutes)")
    process_hourly()
    print()
    print("=== Processing baseline forecast files ===")
    process_baseline()
