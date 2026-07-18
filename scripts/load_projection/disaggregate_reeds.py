"""
disaggregate_reeds.py  —  Approach 1 (participation weights), ReEDS source

Disaggregate ReEDS p-region hourly load (historic 2016–2023 and/or projected
2020–2050) to the substation level using a two-stage participation-weight chain.

Two-stage chain
---------------
  Stage 1 — P-region → county  (geographic fractions, fixed across all hours)
      county_pgroup_fraction[c] = ca_load_fraction[c] / Σ same for counties in p_region
      Source: data/processed/reeds/county_ca_reference.csv (ReEDS county_state_lpf.csv)

  Stage 2 — County → substation  (varies by month × hour)
      sub_county_weight[s, m, h] = max(weight_col[s,m,h], 0) / Σ same for s in county(s)
      Source: data/processed/substations/substation_load_profiles_clean.csv
      Fallback: equal weights when all substations in a county have weight_col ≤ 0

  Combined chain weight (applied directly to any p-region hourly series):
      chain_weight[s, m, h] = county_pgroup_fraction[county(s)] × sub_county_weight[s, m, h]
      substation_load[s, t] = p_region_load[p_region(s), t] × chain_weight[s, month(t), hour(t)]

  Validation: chain weights sum to 1.0 per p-region at every (month, hour) cell.
  Max observed deviation: 1.89e-15 (floating-point only).

Substation coverage
-------------------
  1,329 real substations assigned to counties via substation_county_reeds_mapping.csv.
  + 4 synthetic substations for counties with no utility data (all PacifiCorp territory):
      SYNTHETIC_DEL_NORTE (p9), SYNTHETIC_LASSEN / _MODOC / _SISKIYOU (p8)
  = 1,333 total substations in the chain weights table.
  p8 is all PacifiCorp CA territory — no PGE/SCE/SDGE substations fall there.
  10 substation names appear under both PGE and SDGE (e.g. ALPINE, BARRETT); they
  are genuinely distinct substations in different counties and are kept separate.

ReEDS data sources
------------------
  Historic (2016–2023): data/processed/reeds/historic_ca_load_hourly.parquet
      8 years × 8,760 h per p-region; net load (BTM excluded), CST→PST converted.
  Projected (2020–2050): data/processed/reeds/reeds_ca_load_hourly.parquet
      7 weather years × 31 forecast years × 8,760 h, IRA_low scenario.
      Full hourly output (2.65B rows) is impractical; monthly + annual summaries
      are written instead.  Apply the saved chain weights for custom reconstruction.

Parameters
----------
  --mode          historic | projected | both  (default: both)
                  Each mode writes its own run-tag folder.

  --weight-col    min_load | max_load | avg_load  (default: max_load)
                  Which percentile envelope from the substation profiles to use.
                  max_load (~90th pct) is recommended for gross-load consistency
                  with IEPR BASELINE_CONSUMPTION and RESOLVE demand_mw_2024scaled.

  --weight-level  annual | monthly | hourly | monthhour  (default: monthhour)
                  Temporal resolution at which weights vary.
                  monthhour: weights differ at every (month, hour) cell — captures
                    full diurnal + seasonal load shape variation.
                  monthly: weights vary by month only, constant across 24 hours.
                  annual: constant weights — all hours scaled identically.

  --save-output   Flag (no argument).  Default: off.
                  When set, writes the large per-substation parquet file in addition
                  to the weight tables and annual CSV that are always written.
                  Historic full-hourly parquet: ~750 MB.
                  Projected monthly parquet: ~33 MB.

Run-tag folder naming
---------------------
  reeds_historic__{weight_col}__{weight_level}/
  reeds_projected__{weight_col}__{weight_level}/

Outputs (under data/processed/load_projection/projections/<run_tag>/)
-------
  Always written:
    county_pgroup_weights.csv         — 58-row county → p-region fractions
    substation_chain_weights.csv      — 1,333 subs × 288 (month, hour) cells
    substation_annual_load_by_year.csv  (historic) or substation_annual_load.csv (projected)

  Requires --save-output:
    substation_disaggregated_load.parquet  — historic full hourly, ~93M rows / ~750 MB
    substation_monthly_load.parquet        — projected monthly MWh by (weather_year, year)

Usage
-----
  # Default: both historic and projected, max_load weights at month-hour resolution
  python scripts/load_projection/disaggregate_reeds.py

  # Historic only
  python scripts/load_projection/disaggregate_reeds.py --mode historic

  # Projected only, also write the monthly parquet
  python scripts/load_projection/disaggregate_reeds.py --mode projected --save-output

  # Custom weight column and level
  python scripts/load_projection/disaggregate_reeds.py --weight-col min_load --weight-level monthly

  # Full run with large output saved
  python scripts/load_projection/disaggregate_reeds.py --mode both --weight-col max_load --weight-level monthhour --save-output

Working directory: california-hourly-disaggregation/ (project root)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from load_projection.weights import (
    load_profiles,
    compute_county_pgroup_fractions,
    build_reeds_chain_matrices,
    LEVELS,
    WEIGHT_COLS,
)

PROCESSED  = ROOT / "data" / "processed"
PROFILES   = PROCESSED / "substations" / "substation_load_profiles_clean.csv"
COUNTY_REF = PROCESSED / "reeds" / "county_ca_reference.csv"
SUB_COUNTY = PROCESSED / "substations" / "substation_county_reeds_mapping.csv"
RANKINGS   = PROCESSED / "load_projection" / "rankings"
REEDS_HIST = PROCESSED / "reeds" / "historic_ca_load_hourly.parquet"
REEDS_PROJ = PROCESSED / "reeds" / "reeds_ca_load_hourly.parquet"

P_REGIONS = ["p8", "p9", "p10", "p11"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Disaggregate ReEDS load to substation level")
    p.add_argument("--mode", choices=["historic", "projected", "both"], default="both")
    p.add_argument("--weight-col", choices=list(WEIGHT_COLS), default="max_load")
    p.add_argument("--weight-level", choices=list(LEVELS), default="monthhour")
    p.add_argument("--save-output", action="store_true",
                    help="Write the large per-substation hourly/monthly parquet (default: off)")
    return p.parse_args()


def run_tag(mode: str, weight_col: str, weight_level: str) -> str:
    return f"reeds_{mode}__{weight_col}__{weight_level}"


def output_dir(mode: str, weight_col: str, weight_level: str) -> Path:
    d = PROCESSED / "load_projection" / "projections" / run_tag(mode, weight_col, weight_level)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Chain weights CSV (long format for inspection)
# ---------------------------------------------------------------------------

def chain_weights_to_df(
    matrices: dict,
    weight_col: str,
    weight_level: str,
) -> pd.DataFrame:
    """
    Flatten chain weight matrices back to a long CSV for documentation/inspection.
    """
    rows = []
    for p_region, (subs, mat, meta) in matrices.items():
        meta_idx = meta.set_index("substation_name")
        for si, sub in enumerate(subs):
            r = meta_idx.loc[sub]
            for m in range(12):
                for h in range(24):
                    rows.append({
                        "substation_name":   sub,
                        "utility":           r["utility"],
                        "county_name":       r["county_name"],
                        "fips_int":          r["fips_int"],
                        "p_region":          p_region,
                        "month":             m + 1,
                        "hour_pst":          h,
                        "chain_weight":      mat[si, m, h],
                        "is_synthetic":      r["is_synthetic"],
                        "weight_col":        weight_col,
                        "weight_level":      weight_level,
                    })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Vectorized application helper
# ---------------------------------------------------------------------------

def _apply_matrices_to_hours(
    p_region: str,
    subs: list[str],
    mat: np.ndarray,
    meta: pd.DataFrame,
    p_load: np.ndarray,
    months: np.ndarray,
    hours: np.ndarray,
    year: int,
    weather_year: int | None = None,
    build_batch: bool = True,
) -> tuple[pa.Table | None, pd.DataFrame]:
    """
    Apply chain weight matrix for one p_region to one year's hourly load array.

    Returns (hourly_batch: pa.Table | None, annual_summary: pd.DataFrame).
    build_batch=False skips constructing the (often large) hourly PyArrow table
    when the caller only needs the annual summary.
    """
    n_subs  = len(subs)
    n_hours = len(p_load)

    cw_for_hours = mat[:, months, hours]          # (n_subs, n_hours)
    sub_loads    = cw_for_hours * p_load[None, :] # (n_subs, n_hours)

    meta_idx = meta.set_index("substation_name")

    batch = None
    if build_batch:
        # Hourly batch (PyArrow)
        batch = pa.table({
            "year":            pa.array(np.full(n_subs * n_hours, year, dtype=np.int32)),
            "month":           pa.array(np.tile(months + 1, n_subs), type=pa.int32()),
            "hour":            pa.array(np.tile(hours, n_subs), type=pa.int32()),
            "substation_name": pa.array(np.repeat(subs, n_hours), type=pa.string()),
            "utility":         pa.array(np.repeat(meta_idx.loc[subs, "utility"].values, n_hours), type=pa.string()),
            "county_name":     pa.array(np.repeat(meta_idx.loc[subs, "county_name"].values, n_hours), type=pa.string()),
            "fips_int":        pa.array(np.repeat(meta_idx.loc[subs, "fips_int"].values.astype(np.int64), n_hours)),
            "p_region":        pa.array(np.repeat([p_region] * n_subs, n_hours), type=pa.string()),
            "load_mw":         pa.array(sub_loads.ravel()),
            "is_synthetic":    pa.array(np.repeat(meta_idx.loc[subs, "is_synthetic"].values, n_hours)),
        })
        if weather_year is not None:
            batch = batch.append_column(
                "weather_year",
                pa.array(np.full(n_subs * n_hours, weather_year, dtype=np.int32)),
            )

    # Annual summary
    ann = pd.DataFrame({
        "substation_name": subs,
        "annual_mwh":      sub_loads.sum(axis=1),
    })
    ann["year"] = year
    if weather_year is not None:
        ann["weather_year"] = weather_year
    ann = ann.join(meta_idx[["utility", "county_name", "fips_int", "p_region", "is_synthetic"]],
                   on="substation_name")

    return batch, ann


# ---------------------------------------------------------------------------
# Historic mode
# ---------------------------------------------------------------------------

def run_historic(matrices: dict, out: Path, save_output: bool) -> None:
    print("\n--- Mode: historic (2016-2023) ---")
    if not save_output:
        print("  (--save-output not set: skipping full hourly parquet, computing annual summary only)")
    reeds = pd.read_parquet(REEDS_HIST)

    annual_records = []
    writer = None

    for year, yr in reeds.groupby("year"):
        print(f"  {year}", end="", flush=True)
        yr   = yr.reset_index(drop=True)
        m0   = yr["month"].values.astype(int) - 1   # 0-indexed
        h    = yr["hour"].values.astype(int)

        for p_region, (subs, mat, meta) in matrices.items():
            p_load = yr[f"{p_region}_mw"].values
            batch, ann = _apply_matrices_to_hours(
                p_region, subs, mat, meta, p_load, m0, h, year,
                build_batch=save_output,
            )
            if save_output:
                if writer is None:
                    writer = pq.ParquetWriter(str(out / "substation_disaggregated_load.parquet"),
                                              batch.schema, compression="snappy")
                writer.write_table(batch)
            annual_records.append(ann)

        print(" done", flush=True)

    if writer:
        writer.close()
        print(f"  Saved: {(out / 'substation_disaggregated_load.parquet').relative_to(ROOT)}")

    ann_df = pd.concat(annual_records, ignore_index=True)
    ann_df[[
        "year", "substation_name", "utility", "county_name", "fips_int",
        "p_region", "annual_mwh", "is_synthetic"
    ]].sort_values(["year", "p_region", "county_name", "substation_name"]
     ).to_csv(out / "substation_annual_load_by_year.csv", index=False)
    print(f"  Saved: {(out / 'substation_annual_load_by_year.csv').relative_to(ROOT)}")

    print("\n  CA totals by year (TWh):")
    for yr_val, twh in (ann_df.groupby("year")["annual_mwh"].sum() / 1e6).items():
        print(f"    {yr_val}: {twh:.3f}")


# ---------------------------------------------------------------------------
# Projected mode
# ---------------------------------------------------------------------------

def run_projected(matrices: dict, out: Path, save_output: bool) -> None:
    print("\n--- Mode: projected (2020-2050, 7 weather years) ---")
    print("  (Hourly output omitted for projected — 2.65B rows. Using monthly + annual summaries.)")
    if not save_output:
        print("  (--save-output not set: skipping monthly parquet, computing annual summary only)")
    reeds = pd.read_parquet(REEDS_PROJ)

    annual_records  = []
    monthly_records = []

    groups = list(reeds.groupby(["weather_year", "year", "region"]))
    total  = len(groups)

    for idx, ((wy, yr, region), grp) in enumerate(groups):
        if region not in matrices:
            continue
        if idx % 40 == 0:
            print(f"  ({idx}/{total}) weather_year={wy} year={yr} region={region}", flush=True)

        grp    = grp.reset_index(drop=True)
        m0     = grp["month"].values.astype(int) - 1
        h      = grp["hour"].values.astype(int)
        p_load = grp["load_mw"].values
        subs, mat, meta = matrices[region]

        # Annual
        cw_for_hours = mat[:, m0, h]
        sub_loads    = cw_for_hours * p_load[None, :]  # (n_subs, n_hours)

        ann = pd.DataFrame({
            "weather_year":    wy,
            "year":            yr,
            "substation_name": subs,
            "annual_mwh":      sub_loads.sum(axis=1),
        })
        meta_idx = meta.set_index("substation_name")
        ann = ann.join(meta_idx[["utility","county_name","fips_int","p_region","is_synthetic"]],
                       on="substation_name")
        annual_records.append(ann)

        # Monthly aggregation (only needed when writing the monthly parquet)
        if save_output:
            months_1based = grp["month"].values.astype(int)
            for m_val in range(1, 13):
                mask = months_1based == m_val
                if not mask.any():
                    continue
                monthly_mwh = sub_loads[:, mask].sum(axis=1)
                mon = pd.DataFrame({
                    "weather_year":    wy,
                    "year":            yr,
                    "month":           m_val,
                    "substation_name": subs,
                    "monthly_mwh":     monthly_mwh,
                })
                mon = mon.join(meta_idx[["utility","county_name","fips_int","p_region","is_synthetic"]],
                               on="substation_name")
                monthly_records.append(mon)

    ann_df = pd.concat(annual_records, ignore_index=True)
    ann_df[[
        "weather_year", "year", "substation_name", "utility", "county_name",
        "fips_int", "p_region", "annual_mwh", "is_synthetic"
    ]].sort_values(["weather_year","year","p_region","county_name","substation_name"]
     ).to_csv(out / "substation_annual_load.csv", index=False)
    print(f"  Saved: {(out / 'substation_annual_load.csv').relative_to(ROOT)}")

    if save_output:
        mon_df = pd.concat(monthly_records, ignore_index=True)
        mon_df[[
            "weather_year", "year", "month", "substation_name", "utility", "county_name",
            "fips_int", "p_region", "monthly_mwh", "is_synthetic"
        ]].sort_values(["weather_year","year","month","p_region","county_name","substation_name"]
         ).to_parquet(out / "substation_monthly_load.parquet", index=False)
        print(f"  Saved: {(out / 'substation_monthly_load.parquet').relative_to(ROOT)}")

    print("\n  CA totals by year (mean across weather years, TWh):")
    totals = ann_df.groupby(["weather_year","year"])["annual_mwh"].sum().unstack("weather_year") / 1e6
    for yr_val, row in totals.iterrows():
        print(f"    {yr_val}: mean={row.mean():.2f} TWh  min={row.min():.2f}  max={row.max():.2f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    wt   = args.weight_col
    wl   = args.weight_level
    mode = args.mode
    save_output = args.save_output

    print(f"ReEDS disaggregation: mode={mode}  weight_col={wt}  weight_level={wl}  save_output={save_output}")

    # Load inputs
    print("\nLoading inputs...")
    profiles   = load_profiles(PROFILES, wt)
    sub_county = pd.read_csv(SUB_COUNTY)
    county_ref = pd.read_csv(COUNTY_REF)

    # Build chain weight matrices
    print(f"Building chain weight matrices ({wl} level, {wt})...")
    matrices = build_reeds_chain_matrices(
        profiles, sub_county, county_ref,
        level=wl, weight_col=wt,
        p_regions=P_REGIONS, add_synthetic=True,
    )

    n_subs_total = sum(len(subs) for subs, _, _ in matrices.values())
    for p, (subs, mat, meta) in matrices.items():
        n_syn = meta["is_synthetic"].sum()
        print(f"  {p}: {len(subs)} substations ({n_syn} synthetic)")

    # Chain weight validation
    # sum of chain_weights over subs in p_region at each (month, hour) must equal 1.0
    max_err = 0.0
    for p, (subs, mat, _) in matrices.items():
        col_sums = mat.sum(axis=0)  # (12, 24)
        max_err = max(max_err, abs(col_sums - 1.0).max())
    print(f"  Chain weight sum check: max deviation from 1.0 = {max_err:.2e}")

    # Save county fractions (geographic, not weight-level-dependent)
    county_weights = compute_county_pgroup_fractions(county_ref)

    # Run in either/both modes, each gets its own output folder
    if mode in ("historic", "both"):
        out_hist = output_dir("historic", wt, wl)
        county_weights[["fips_int","county_name","p_region","ca_load_fraction","pgroup_fraction"]
                       ].to_csv(out_hist / "county_pgroup_weights.csv", index=False)
        chain_df = chain_weights_to_df(matrices, wt, wl)
        chain_df.to_csv(out_hist / "substation_chain_weights.csv", index=False)
        print(f"  Saved chain weights: {(out_hist / 'substation_chain_weights.csv').relative_to(ROOT)}")
        run_historic(matrices, out_hist, save_output)

    if mode in ("projected", "both"):
        out_proj = output_dir("projected", wt, wl)
        county_weights[["fips_int","county_name","p_region","ca_load_fraction","pgroup_fraction"]
                       ].to_csv(out_proj / "county_pgroup_weights.csv", index=False)
        chain_df = chain_weights_to_df(matrices, wt, wl)
        chain_df.to_csv(out_proj / "substation_chain_weights.csv", index=False)
        print(f"  Saved chain weights: {(out_proj / 'substation_chain_weights.csv').relative_to(ROOT)}")
        run_projected(matrices, out_proj, save_output)

    print("\nDone.")


if __name__ == "__main__":
    main()
