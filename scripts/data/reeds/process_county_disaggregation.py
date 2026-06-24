"""
process_county_disaggregation.py

Builds a California county reference table combining three ReEDS input files:

  1. county2zone.csv       — county FIPS (int) → ReEDS p-region (p8–p11) + county name
  2. county_state_lpf.csv  — county share of California state load (load participation factor)
  3. distpvcap             — county distributed PV capacity by year (MW, every 2 years 2010–2050)

Source: data/raw/reeds/ReEDS-2.0/inputs/

FIPS format note
----------------
  county2zone.csv uses integer FIPS without leading zero on the state code:
    e.g., 6001 (Alameda County, CA) — state 06 encoded without leading zero.
  county_state_lpf.csv and distpvcap use a string format with 'p' prefix and
  zero-padded 5 digits:
    e.g., 'p06001' (Alameda County, CA).
  Conversion applied here: f"p{fips:05d}"
    6001  → 'p06001'
    6037  → 'p06037'
    6115  → 'p06115'
  CA FIPS integers span 6001–6115 (58 counties).

Outputs
-------
  data/processed/reeds/county_ca_reference.csv
    One row per California county (58 rows).
    Columns:
      fips_int          int    integer county FIPS (e.g., 6037)
      fips_key          str    p-format FIPS (e.g., 'p06037')
      county_name       str    county name from county2zone
      state             str    state abbreviation ('CA')
      p_region          str    ReEDS p-region ('p8', 'p9', 'p10', or 'p11')
      ca_load_fraction  float  fraction of CA state load attributed to this county
      btm_pv_{year}_mw  float  distributed PV capacity (MW) for years 2010–2050

Usage
-----
  python scripts/data/reeds/process_county_disaggregation.py

Run process_reeds.py first to ensure the processed/reeds/ directory exists,
or this script will create it automatically.
"""

from pathlib import Path

import pandas as pd

ROOT  = Path(__file__).resolve().parents[3]
REEDS = ROOT / "data" / "raw" / "reeds" / "ReEDS-2.0" / "inputs"
OUT   = ROOT / "data" / "processed" / "reeds"

COUNTY2ZONE = REEDS / "county2zone.csv"
COUNTY_LPF  = REEDS / "disaggregation" / "county_state_lpf.csv"
DISTPVCAP   = (REEDS / "dgen_model_inputs" / "stscen2023_mid_case"
               / "distpvcap_stscen2023_mid_case.csv")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # ── 1. county2zone: integer FIPS → p-region ──────────────────────────────
    # California FIPS integers are in range 6001–6115 (state code 06, no leading zero)
    c2z = pd.read_csv(COUNTY2ZONE)
    ca = c2z[c2z["FIPS"].between(6001, 6115)].copy()
    ca["fips_int"] = ca["FIPS"].astype(int)
    ca["fips_key"] = ca["fips_int"].apply(lambda x: f"p{x:05d}")
    ca = ca[["fips_int", "fips_key", "county_name", "state", "ba"]].rename(
        columns={"ba": "p_region"}
    )
    print(f"county2zone: {len(ca)} CA counties")
    print(f"  p-regions: {sorted(ca['p_region'].unique())}")

    # ── 2. county_state_lpf: CA county share of CA state load ────────────────
    # Values sum to ~1.0 across all 58 CA counties (fraction of statewide CA load).
    # Source: ReEDS disaggregation inputs; LPF = load participation factor.
    lpf = pd.read_csv(COUNTY_LPF)
    lpf_ca = (lpf[lpf["FIPS"].str.startswith("p06")]
              .rename(columns={"FIPS": "fips_key", "value": "ca_load_fraction"})
              .copy())
    print(f"county_state_lpf: {len(lpf_ca)} CA rows, sum={lpf_ca['ca_load_fraction'].sum():.6f}")

    merged = ca.merge(lpf_ca, on="fips_key", how="left")
    n_missing = merged["ca_load_fraction"].isna().sum()
    if n_missing:
        print(f"  WARNING: {n_missing} counties missing from LPF — filling 0")
        merged["ca_load_fraction"] = merged["ca_load_fraction"].fillna(0.0)

    # ── 3. distpvcap: county BTM distributed PV by year (MW) ─────────────────
    # Source: ReEDS dGen model inputs, mid-case scenario.
    # Column 'r' uses same p-format FIPS as county_state_lpf.
    dpv = pd.read_csv(DISTPVCAP)
    dpv_ca = dpv[dpv["r"].str.startswith("p06")].copy()
    year_cols = [c for c in dpv.columns if c.isdigit()]
    dpv_ca = (dpv_ca[["r"] + year_cols]
              .rename(columns={"r": "fips_key"})
              .rename(columns={y: f"btm_pv_{y}_mw" for y in year_cols}))
    print(f"distpvcap: {len(dpv_ca)} CA rows, years {year_cols[0]}–{year_cols[-1]}")

    merged = merged.merge(dpv_ca, on="fips_key", how="left")
    btm_cols = [f"btm_pv_{y}_mw" for y in year_cols]
    n_missing_pv = merged[btm_cols].isna().any(axis=1).sum()
    if n_missing_pv:
        print(f"  WARNING: {n_missing_pv} counties missing from distpvcap")

    # ── 4. Write ──────────────────────────────────────────────────────────────
    merged = merged.sort_values("fips_int").reset_index(drop=True)
    out_path = OUT / "county_ca_reference.csv"
    merged.to_csv(out_path, index=False)
    print(f"\nWrote {out_path.relative_to(ROOT)}  ({len(merged)} rows × {len(merged.columns)} cols)")

    # Summary by p-region
    summary = (merged.groupby("p_region")
               .agg(
                   n_counties=("county_name", "count"),
                   load_share=("ca_load_fraction", "sum"),
                   btm_pv_2024_mw=("btm_pv_2024_mw", "sum"),
               )
               .reset_index())
    print(f"\n  Summary by ReEDS p-region:")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
