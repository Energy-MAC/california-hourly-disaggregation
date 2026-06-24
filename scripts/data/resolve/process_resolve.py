"""
process_resolve.py

Processes RESOLVE (E3 / CPUC IRP) load input data into clean files for
comparison against IEPR and EIA-930, and for use in the substation
disaggregation prediction pipeline.

RESOLVE is an energy system planning model used by CPUC for California Integrated Resource Planning.
Its load inputs come in two layers:

  1. profiles/loads/2024/{utility}_Baseline.csv
       Hourly (8760-row) shape profiles for each utility covering calendar
       years 2000-2022.  Column "profile_model_years" is in MW (gross load,
       before BTM solar subtraction).
       STRUCTURAL PROOF that this is gross load: the file contains ONLY two
       columns ("datetime", "profile_model_years") — there is no BTM_PV or
       Customer_PV column in this file.  BTM solar is in a completely separate
       directory (data/profiles/pmax/2025/{UTIL}_Customer_PV.csv) with its own
       "Weather Factor" column (hourly solar capacity factor, 0-1).  The physical
       separation of load and BTM profiles proves they are independent quantities.

  2. data/interim/loads/{utility}_Baseline_CHP_Not_Retire.csv
       Annual energy forecast targets (MWh) for model years 2024-2045.
       RESOLVE scales each shape profile by (annual_target / shape_sum)
       before running the optimization (see new_modeling_toolkit/system/electric/
       load_component.py, scale_multiplier logic).

BTM solar subtraction
---------------------
RESOLVE models rooftop PV (Customer_PV) as a supply-side resource, not a
demand-side reduction.  demand_mw_2024scaled is therefore GROSS demand.  To
produce net load for use in the prediction pipeline:

    btm_pv_mw     = weather_factor × planned_capacity_2024
    demand_mw_net = demand_mw_2024scaled − btm_pv_mw

where:
  weather_factor      from data/profiles/pmax/2025/{UTIL}_Customer_PV.csv
                      column "Weather Factor" (0–1 solar capacity factor)
  planned_capacity    from data/interim/resources/{UTIL}_Customer_PV.csv
                      attribute=planned_capacity, scenario=2024_IEPR_Local_Reliability,
                      year=2024 (PGE: 9,669 MW; SCE: 6,553 MW; SDGE: 2,463 MW;
                                 IID: 185 MW; LDWP: 730 MW; NCNC: 754 MW)

Both btm_pv_mw and demand_mw_net are written to resolve_hourly_profiles.csv
so downstream scripts do not need to load the pmax/resource files themselves.

Geographic scope of RESOLVE utilities modeled here:
  PGE    Pacific Gas & Electric (CAISO territory; PGE, SCE, SDGE map to CISO BA)
  SCE    Southern California Edison (CAISO territory)
  SDGE   San Diego Gas & Electric (CAISO territory)
  IID    Imperial Irrigation District
  LDWP   Los Angeles Department of Water & Power
  NCNC   Northern California Non-CAISO — includes BANC, TIDC, SMUD, and other small
         municipal utilities in northern CA.  See CEC Demand Modelling Form 1.1c at
         https://www.energy.ca.gov/data-reports/california-energy-planning-library/
         forecasts-and-system-planning/demand-side-3

RESOLVE does NOT include NEVP, PACW, WALC — these appear in
EIA-930 CA8 but not in RESOLVE's California scope.  BANC and TIDC are included in NCNC.

Outputs
-------
  data/processed/resolve/resolve_hourly_profiles.csv
      datetime | utility | demand_mw_raw | demand_mw_2024scaled
      Raw shape values from 2024/ profile folder, plus a version scaled to
      each utility's 2024 annual energy forecast for absolute comparison.
      Covers 2000-2022 for PGE, SCE, SDGE, IID, LDWP, NCNC.

  data/processed/resolve/resolve_annual_forecast.csv
      utility | year | energy_mwh | energy_twh
      Annual energy forecast targets from interim/loads Baseline files.
      CHP_Not_Retire scenario for PGE/SCE/SDGE/LDWP/NCNC; CHP_Retire
      version nearly identical (same 2024 values).
      Covers 2024-2045.

Usage
-----
  python scripts/data/resolve/process_resolve.py
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT    = Path(__file__).resolve().parents[3]
PROC    = ROOT / "data" / "processed" / "resolve"
PROC.mkdir(parents=True, exist_ok=True)

RESOLVE = (ROOT / "data" / "raw" /
           "RESOLVE Code Base and Inputs" /
           "RESOLVE Code Base and Inputs")

PROFILES_DIR = RESOLVE / "data" / "profiles" / "loads" / "2024"
INTERIM_DIR  = RESOLVE / "data" / "interim"  / "loads"
PMAX_DIR     = RESOLVE / "data" / "profiles" / "pmax"  / "2025"
RSRC_DIR     = RESOLVE / "data" / "interim"  / "resources"

BTM_SCENARIO = "2024_IEPR_Local_Reliability"
BTM_YEAR     = 2024

# Which profile files to load and their canonical utility label
PROFILE_FILES = {
    "PGE":  "PGE_Baseline.csv",
    "SCE":  "SCE_Baseline.csv",
    "SDGE": "SDGE_Baseline.csv",
    "IID":  "IID_Baseline.csv",
    "LDWP": "LDWP_Baseline.csv",
    "NCNC": "NCNC_Baseline.csv",
}

# Interim baseline files (annual energy forecasts)
INTERIM_FILES = {
    "PGE":  "PGE_Baseline_CHP_Not_Retire.csv",
    "SCE":  "SCE_Baseline_CHP_Not_Retire.csv",
    "SDGE": "SDGE_Baseline_CHP_Not_Retire.csv",
    "IID":  "IID_Baseline.csv",
    "LDWP": "LDWP_Baseline_CHP_Not_Retire.csv",
    "NCNC": "NCNC_Baseline_CHP_Not_Retire.csv",
}


# ── BTM solar offset ─────────────────────────────────────────────────────────

def _load_customer_pv_offset(util: str) -> pd.DataFrame | None:
    """
    Load RESOLVE's native Customer_PV BTM offset for one utility.

    Returns DataFrame with columns: datetime_pst, btm_pv_mw
    where btm_pv_mw = Weather_Factor × planned_capacity_2024 (MW, positive).
    Returns None if either file is missing (btm_pv_mw will be set to 0).
    """
    pmax_path = PMAX_DIR / f"{util}_Customer_PV.csv"
    rsrc_path = RSRC_DIR / f"{util}_Customer_PV.csv"
    if not pmax_path.exists() or not rsrc_path.exists():
        print(f"  {util}: no Customer_PV files found — btm_pv_mw = 0")
        return None

    rsrc = pd.read_csv(rsrc_path)
    cap_rows = rsrc[
        (rsrc["attribute"] == "planned_capacity") &
        (rsrc["scenario"] == BTM_SCENARIO) &
        (pd.to_datetime(rsrc["timestamp"]).dt.year == BTM_YEAR)
    ]
    if cap_rows.empty:
        print(f"  {util}: planned_capacity row missing for {BTM_YEAR}/{BTM_SCENARIO} — btm_pv_mw = 0")
        return None
    capacity_mw = float(cap_rows["value"].iloc[0])

    pmax = pd.read_csv(pmax_path, parse_dates=["datetime"])
    pmax = pmax.rename(columns={"datetime": "datetime_pst", "Weather Factor": "weather_factor"})
    pmax["btm_pv_mw"] = pmax["weather_factor"] * capacity_mw
    print(f"  {util}: {capacity_mw:.0f} MW × CF (max {pmax['weather_factor'].max():.3f}) "
          f"= peak BTM {pmax['btm_pv_mw'].max():.0f} MW")
    return pmax[["datetime_pst", "btm_pv_mw"]]


# ── Hourly profiles ───────────────────────────────────────────────────────────

def _load_one_profile(util: str, path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["datetime"])
    df = df.rename(columns={"datetime": "datetime_pst", "profile_model_years": "demand_mw_raw"})
    df["utility"] = util
    df["year"]    = df["datetime_pst"].dt.year
    return df[["datetime_pst", "year", "utility", "demand_mw_raw"]]


def _load_annual_target(path: Path) -> pd.Series:
    """Return pd.Series year -> energy_mwh from an interim loads file."""
    df = pd.read_csv(path)
    rows = df[df["attribute"] == "annual_energy_forecast"].copy()
    rows["year"] = pd.to_datetime(rows["timestamp"]).dt.year
    rows["energy_mwh"] = pd.to_numeric(rows["value"], errors="coerce")
    return rows.set_index("year")["energy_mwh"]


def process_hourly_profiles() -> pd.DataFrame:
    pieces = []
    for util, fname in PROFILE_FILES.items():
        fpath = PROFILES_DIR / fname
        if not fpath.exists():
            print(f"  WARNING: {fpath.name} not found — skipping {util}")
            continue
        df = _load_one_profile(util, fpath)
        print(f"  {util}: {df['year'].min()}-{df['year'].max()}  "
              f"({len(df):,} rows)  "
              f"raw mean {df['demand_mw_raw'].mean():.0f} MW")

        # Compute per-year scale to 2024 annual forecast (for absolute comparison)
        interim_path = INTERIM_DIR / INTERIM_FILES[util]
        if interim_path.exists():
            targets = _load_annual_target(interim_path)
            target_2024 = float(targets.get(2024, float("nan")))
            annual_raw = df.groupby("year")["demand_mw_raw"].sum()
            scale_map = {
                yr: (target_2024 / s) if s and not pd.isna(s) and not pd.isna(target_2024) else float("nan")
                for yr, s in annual_raw.items()
            }
            df["demand_mw_2024scaled"] = df["demand_mw_raw"] * df["year"].map(scale_map)
            print(f"    2024 target: {target_2024/1e6:.2f} TWh  "
                  f"  scaled mean: {df['demand_mw_2024scaled'].mean():.0f} MW")
        else:
            df["demand_mw_2024scaled"] = float("nan")
            print(f"    (no interim file — 2024scaled will be NaN)")

        # BTM solar: weather_factor × planned_capacity_2024
        cpv = _load_customer_pv_offset(util)
        if cpv is not None:
            df = df.merge(cpv, on="datetime_pst", how="left")
            df["btm_pv_mw"] = df["btm_pv_mw"].fillna(0.0)
        else:
            df["btm_pv_mw"] = 0.0
        df["demand_mw_net"] = df["demand_mw_2024scaled"] - df["btm_pv_mw"]

        pieces.append(df)

    combined = pd.concat(pieces, ignore_index=True)
    return combined.drop(columns=["year"])


# ── Annual forecasts ──────────────────────────────────────────────────────────

def process_annual_forecasts() -> pd.DataFrame:
    pieces = []
    for util, fname in INTERIM_FILES.items():
        fpath = INTERIM_DIR / fname
        if not fpath.exists():
            print(f"  WARNING: {fpath.name} not found — skipping {util}")
            continue
        targets = _load_annual_target(fpath)
        df = pd.DataFrame({
            "utility":    util,
            "year":       targets.index,
            "energy_mwh": targets.values,
        })
        df["energy_twh"] = df["energy_mwh"] / 1e6
        pieces.append(df)
        y_min, y_max = int(df["year"].min()), int(df["year"].max())
        print(f"  {util}: {y_min}-{y_max}  "
              f"2024={df.loc[df['year']==2024,'energy_twh'].squeeze():.2f} TWh  "
              f"2030={df.loc[df['year']==2030,'energy_twh'].squeeze():.2f} TWh  "
              f"2045={df.loc[df['year']==2045,'energy_twh'].squeeze():.2f} TWh")

    return pd.concat(pieces, ignore_index=True).sort_values(["utility", "year"])


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Processing RESOLVE hourly shape profiles ...")
    profiles = process_hourly_profiles()
    out1 = PROC / "resolve_hourly_profiles.csv"
    profiles[["datetime_pst", "utility", "demand_mw_raw", "demand_mw_2024scaled",
              "btm_pv_mw", "demand_mw_net"]].to_csv(
        out1, index=False
    )
    print(f"  -> {out1.relative_to(ROOT)}  ({len(profiles):,} rows)\n")

    print("Processing RESOLVE annual energy forecasts ...")
    annual = process_annual_forecasts()
    out2 = PROC / "resolve_annual_forecast.csv"
    annual.to_csv(out2, index=False)
    print(f"  -> {out2.relative_to(ROOT)}  ({len(annual):,} rows)\n")

    # Summary
    print("Summary by utility (annual forecast, TWh):")
    piv = annual.pivot_table(index="utility", columns="year", values="energy_twh")
    cols = [c for c in [2024, 2026, 2030, 2035, 2040, 2045] if c in piv.columns]
    print(piv[cols].to_string(float_format="{:.1f}".format))
    print()

    caiso_utils = ["PGE", "SCE", "SDGE"]
    caiso_totals = annual[annual["utility"].isin(caiso_utils)].groupby("year")["energy_twh"].sum()
    print("CAISO zone total (PGE+SCE+SDGE) annual forecast:")
    for yr in [2024, 2026, 2030, 2035, 2040, 2045]:
        if yr in caiso_totals:
            print(f"  {yr}: {caiso_totals[yr]:.1f} TWh")


if __name__ == "__main__":
    main()
