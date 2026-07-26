"""
disaggregate_iou.py  —  Approach 1 (participation weights), IEPR and RESOLVE sources

Disaggregate IEPR or RESOLVE hourly IOU-level load (PGE / SCE / SDGE) to
individual substations using a single-stage participation-weight chain.

Single-stage chain
------------------
    sub_iou_weight[s, m, h] = max(weight_col[s,m,h], 0) / Σ same for all s in IOU(s)
    substation_load[s, t]   = IOU_load[IOU(s), t] × sub_iou_weight[s, month(t), hour_pst(t)]

Weight is derived from the utility substation profiles (90th-pct max_load by default).
Weights sum to 1.0 per IOU at every (month, hour) cell; equal weights are used as a
fallback when all substations in an IOU have weight_col ≤ 0 at a cell.

IOU scope
---------
Only PGE (664 subs), SCE (578 subs), and SDGE (99 subs) are disaggregated — these
are the only utilities for which we have substation load profiles.
  IEPR: VEA load is excluded and reported.
  RESOLVE: IID, LDWP, and NCNC load are excluded and reported.

Gross vs net load convention
-----------------------------
Substation profiles are gross load (BTM solar not subtracted).  For a gross-to-gross
comparison, use:
  IEPR:    --load-col BASELINE_CONSUMPTION  (gross, default)
  RESOLVE: --load-col demand_mw_2024scaled  (gross, default)
Do NOT pair gross substation weights with BASELINE_NET_LOAD or demand_mw_net — the
~30 TWh statewide BTM offset would be lost in the scaled output.

Parameters
----------
  --source        iepr | resolve  (required)

  --vintage       2023 | 2024 | 2025  (IEPR only; default 2025)
                  IEPR forecast vintage year to filter from iepr_hourly_forecast.csv.

  --scenario      Planning_Scenario | Local_Reliability | Local_Reliability_plusKnown
                  (IEPR only; default Planning_Scenario)

  --load-col      Load column to disaggregate.
                  IEPR options:    BASELINE_CONSUMPTION (default) | BASELINE_NET_LOAD
                                   | MANAGED_NET_LOAD
                  RESOLVE options: demand_mw_2024scaled (default) | demand_mw_net

  --weight-col    min_load | max_load | avg_load  (default: max_load)
                  Which percentile envelope from the substation profiles to use as weight.

  --weight-level  annual | monthly | hourly | monthhour  (default: monthhour)
                  Temporal resolution at which weights vary:
                    monthhour — full diurnal + seasonal shape (288 weight cells)
                    monthly   — seasonal variation, constant within each month's hours
                    hourly    — daily shape variation, constant across months
                    annual    — constant weights for all hours

  --save-output   Flag (no argument).  Default: off.
                  When set, writes the full hourly per-substation parquet in addition
                  to the weights CSV and annual CSV that are always written.
                  IEPR full-hourly parquet: ~2.4 GB.
                  RESOLVE full-hourly parquet: ~2.2 GB (23 weather years × all subs).

Run-tag folder naming
---------------------
  IEPR:    iepr__v{vintage}__{scenario_lower}__{load_col_lower}__{weight_col}__{weight_level}/
  RESOLVE: resolve__{load_col_lower}__{weight_col}__{weight_level}/

  Example defaults:
    iepr__v2025__planningscenario__baselineconsumption__max_load__monthhour/
    resolve__demandmw2024scaled__max_load__monthhour/

Outputs (under data/processed/load_projection/projections/<run_tag>/)
-------
  Always written:
    substation_iou_weights.csv   — PGE/SCE/SDGE weight per (substation, month, hour)
    substation_annual_load.csv   — annual MWh per (year or weather_year, substation, utility)

  Requires --save-output:
    substation_disaggregated_load.parquet  — full hourly loads for all 1,341 substations

Usage
-----
  # IEPR defaults: vintage 2025, Planning_Scenario, BASELINE_CONSUMPTION, max_load, monthhour
  python scripts/load_projection/approach1/disaggregate_iou.py --source iepr

  # IEPR: different vintage
  python scripts/load_projection/approach1/disaggregate_iou.py --source iepr --vintage 2024

  # IEPR: alternative scenario and also save the large parquet
  python scripts/load_projection/approach1/disaggregate_iou.py --source iepr \\
      --scenario Local_Reliability --save-output

  # IEPR: net load column (pair with net-load statewide forecast)
  python scripts/load_projection/approach1/disaggregate_iou.py --source iepr \\
      --load-col BASELINE_NET_LOAD --weight-col max_load

  # RESOLVE defaults: demand_mw_2024scaled, max_load, monthhour (23 weather years)
  python scripts/load_projection/approach1/disaggregate_iou.py --source resolve

  # RESOLVE: also write the full hourly parquet (~2.2 GB)
  python scripts/load_projection/approach1/disaggregate_iou.py --source resolve --save-output

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

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from load_projection.weights import (
    load_profiles,
    build_iou_weight_matrices,
    LEVELS,
    WEIGHT_COLS,
)

PROCESSED = ROOT / "data" / "processed"
PROFILES  = PROCESSED / "substations" / "substation_load_profiles_clean.csv"
IEPR_FILE = PROCESSED / "iepr" / "iepr_hourly_forecast.csv"
RESOLVE_FILE = PROCESSED / "resolve" / "resolve_hourly_profiles.csv"

IOUS = ["PGE", "SCE", "SDGE"]

IEPR_SCENARIOS = ["Planning_Scenario", "Local_Reliability", "Local_Reliability_plusKnown"]
IEPR_LOAD_COLS = ["BASELINE_CONSUMPTION", "BASELINE_NET_LOAD", "MANAGED_NET_LOAD"]
RESOLVE_LOAD_COLS = ["demand_mw_2024scaled", "demand_mw_net"]

# Map our internal utility labels (lowercase) to external source labels (uppercase)
IOU_MAP = {"pge": "PGE", "sce": "SCE", "sdge": "SDGE"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Disaggregate IEPR or RESOLVE load to substations")
    p.add_argument("--source", choices=["iepr", "resolve"], required=True)
    p.add_argument("--vintage", type=int, choices=[2023, 2024, 2025], default=2025,
                   help="IEPR forecast vintage year (IEPR only)")
    p.add_argument("--scenario", choices=IEPR_SCENARIOS, default="Planning_Scenario",
                   help="IEPR scenario (IEPR only)")
    p.add_argument("--load-col", default=None,
                   help="Load column: IEPR BASELINE_CONSUMPTION (default) or RESOLVE demand_mw_2024scaled (default)")
    p.add_argument("--weight-col", choices=list(WEIGHT_COLS), default="max_load")
    p.add_argument("--weight-level", choices=list(LEVELS), default="monthhour")
    p.add_argument("--save-output", action="store_true",
                    help="Write the full hourly disaggregated parquet (default: off)")
    return p.parse_args()


def resolve_load_col(args: argparse.Namespace) -> str:
    if args.load_col is not None:
        return args.load_col
    return "BASELINE_CONSUMPTION" if args.source == "iepr" else "demand_mw_2024scaled"


def run_tag(args: argparse.Namespace, load_col: str) -> str:
    lc = load_col.lower().replace("_", "")
    if args.source == "iepr":
        sc = args.scenario.lower().replace("_", "")
        return f"iepr__v{args.vintage}__{sc}__{lc}__{args.weight_col}__{args.weight_level}"
    else:
        return f"resolve__{lc}__{args.weight_col}__{args.weight_level}"


def output_dir(tag: str) -> Path:
    d = PROCESSED / "load_projection" / "projections" / tag
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Weight table to CSV (long format for inspection)
# ---------------------------------------------------------------------------

def iou_weights_to_df(
    profiles: pd.DataFrame,
    weight_matrices: dict,
    weight_col: str,
    weight_level: str,
) -> pd.DataFrame:
    """Flatten IOU weight matrices to a long CSV for inspection."""
    rows = []
    meta = (
        profiles[["substation_name", "utility"]]
        .drop_duplicates("substation_name")
        .set_index("substation_name")
    )
    for iou, (subs, mat) in weight_matrices.items():
        for si, sub in enumerate(subs):
            for m in range(12):
                for h in range(24):
                    rows.append({
                        "substation_name": sub,
                        "utility":         iou,
                        "month":           m + 1,
                        "hour_pst":        h,
                        "sub_iou_weight":  mat[si, m, h],
                        "weight_col":      weight_col,
                        "weight_level":    weight_level,
                    })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Core vectorized disaggregation
# ---------------------------------------------------------------------------

def disaggregate_block(
    iou: str,
    subs: list[str],
    mat: np.ndarray,
    source_load: np.ndarray,  # (n_hours,)
    months_0idx: np.ndarray,  # (n_hours,) 0-indexed
    hours_0idx: np.ndarray,   # (n_hours,) 0-indexed
    time_keys: dict,          # extra int32 columns for the batch (year or weather_year)
    build_batch: bool = True,
) -> tuple[pa.Table | None, pd.Series]:
    """
    Vectorized disaggregation of one IOU time block.
    Returns (hourly_batch: pa.Table | None, annual_mwh: pd.Series indexed by substation_name).
    build_batch=False skips constructing the hourly PyArrow table when only the
    annual summary is needed.
    """
    n_subs  = len(subs)
    n_hours = len(source_load)

    cw_for_hours = mat[:, months_0idx, hours_0idx]      # (n_subs, n_hours)
    sub_loads    = cw_for_hours * source_load[None, :]   # (n_subs, n_hours)

    batch = None
    if build_batch:
        fields: dict[str, pa.array] = {
            "substation_name": pa.array(np.repeat(subs, n_hours), type=pa.string()),
            "utility":         pa.array(np.repeat([iou] * n_subs, n_hours), type=pa.string()),
            "month":           pa.array(np.tile(months_0idx + 1, n_subs), type=pa.int32()),
            "hour":            pa.array(np.tile(hours_0idx, n_subs), type=pa.int32()),
            "load_mw":         pa.array(sub_loads.ravel()),
        }
        for k, v in time_keys.items():
            fields[k] = pa.array(np.full(n_subs * n_hours, v, dtype=np.int32))
        batch = pa.table(fields)

    annual_mwh = pd.Series(sub_loads.sum(axis=1), index=subs, name="annual_mwh")
    return batch, annual_mwh


# ---------------------------------------------------------------------------
# IEPR mode
# ---------------------------------------------------------------------------

def run_iepr(
    weight_matrices: dict,
    vintage: int,
    scenario: str,
    load_col: str,
    out: Path,
    save_output: bool,
) -> None:
    print(f"\n--- Source: IEPR  vintage={vintage}  scenario={scenario}  load_col={load_col} ---")
    if not save_output:
        print("  (--save-output not set: skipping full hourly parquet, computing annual summary only)")

    cols_needed = ["forecast_vintage_year", "utility_ba", "scenario", "YEAR", "MONTH",
                   "hour", load_col]
    iepr = pd.read_csv(IEPR_FILE, usecols=cols_needed)
    iepr = iepr[
        (iepr["forecast_vintage_year"] == vintage) &
        (iepr["scenario"] == scenario)
    ].copy()

    excl = iepr[~iepr["utility_ba"].isin(IOUS)]["utility_ba"].unique().tolist()
    if excl:
        print(f"  Excluded utilities (no substations): {excl}")
    iepr = iepr[iepr["utility_ba"].isin(IOUS)]

    years = sorted(iepr["YEAR"].unique())
    print(f"  Forecast years: {years[0]}–{years[-1]}  ({len(years)} years)")

    annual_records: list[pd.DataFrame] = []
    writer = None

    for year in years:
        print(f"  {year}", end="", flush=True)
        yr_data = iepr[iepr["YEAR"] == year].copy()

        for iou, (subs, mat) in weight_matrices.items():
            iou_data = yr_data[yr_data["utility_ba"] == iou].sort_values(["MONTH", "hour"])
            if iou_data.empty:
                continue

            source_load  = iou_data[load_col].values
            months_0idx  = (iou_data["MONTH"].values.astype(int) - 1)
            hours_0idx   = iou_data["hour"].values.astype(int)

            batch, ann_mwh = disaggregate_block(
                iou, subs, mat, source_load, months_0idx, hours_0idx,
                {"year": year}, build_batch=save_output,
            )

            if save_output:
                if writer is None:
                    writer = pq.ParquetWriter(
                        str(out / "substation_disaggregated_load.parquet"), batch.schema, compression="snappy"
                    )
                writer.write_table(batch)

            ann = pd.DataFrame({
                "year":            year,
                "substation_name": subs,
                "utility":         iou,
                "annual_mwh":      ann_mwh.values,
            })
            annual_records.append(ann)

        print(" done", flush=True)

    if writer:
        writer.close()
        print(f"  Saved: {(out / 'substation_disaggregated_load.parquet').relative_to(ROOT)}")

    ann_df = pd.concat(annual_records, ignore_index=True)
    ann_df.sort_values(["year", "utility", "substation_name"]).to_csv(
        out / "substation_annual_load.csv", index=False
    )
    print(f"  Saved: {(out / 'substation_annual_load.csv').relative_to(ROOT)}")

    print("\n  Total disaggregated IOU load by year (TWh):")
    for yr_val, twh in (ann_df.groupby("year")["annual_mwh"].sum() / 1e6).items():
        print(f"    {yr_val}: {twh:.2f}")


# ---------------------------------------------------------------------------
# RESOLVE mode
# ---------------------------------------------------------------------------

def run_resolve(
    weight_matrices: dict,
    load_col: str,
    out: Path,
    save_output: bool,
) -> None:
    print(f"\n--- Source: RESOLVE  load_col={load_col} ---")
    if not save_output:
        print("  (--save-output not set: skipping full hourly parquet, computing annual summary only)")

    cols = ["datetime_pst", "utility", load_col]
    resolve = pd.read_csv(RESOLVE_FILE, usecols=cols)
    resolve["datetime_pst"] = pd.to_datetime(resolve["datetime_pst"])
    resolve["month"]  = resolve["datetime_pst"].dt.month
    resolve["hour"]   = resolve["datetime_pst"].dt.hour
    resolve["year"]   = resolve["datetime_pst"].dt.year  # weather year

    # Map RESOLVE utility labels to IOUS
    resolve = resolve[resolve["utility"].isin(IOUS)]

    excl_utils = set(pd.read_csv(RESOLVE_FILE, usecols=["utility"])["utility"].unique()) - set(IOUS)
    if excl_utils:
        print(f"  Excluded utilities (no substations): {sorted(excl_utils)}")

    weather_years = sorted(resolve["year"].unique())
    print(f"  Weather years: {weather_years[0]}–{weather_years[-1]}  ({len(weather_years)} years)")

    annual_records: list[pd.DataFrame] = []
    writer = None

    for wy, wy_data in resolve.groupby("year"):
        print(f"  weather_year={wy}", end="", flush=True)

        for iou, (subs, mat) in weight_matrices.items():
            iou_data = wy_data[wy_data["utility"] == iou].sort_values(["month", "hour"])
            if iou_data.empty:
                continue

            source_load = iou_data[load_col].values
            months_0idx = (iou_data["month"].values.astype(int) - 1)
            hours_0idx  = iou_data["hour"].values.astype(int)

            batch, ann_mwh = disaggregate_block(
                iou, subs, mat, source_load, months_0idx, hours_0idx,
                {"weather_year": wy}, build_batch=save_output,
            )

            if save_output:
                if writer is None:
                    writer = pq.ParquetWriter(
                        str(out / "substation_disaggregated_load.parquet"), batch.schema, compression="snappy"
                    )
                writer.write_table(batch)

            ann = pd.DataFrame({
                "weather_year":    wy,
                "substation_name": subs,
                "utility":         iou,
                "annual_mwh":      ann_mwh.values,
            })
            annual_records.append(ann)

        print(" done", flush=True)

    if writer:
        writer.close()
        print(f"  Saved: {(out / 'substation_disaggregated_load.parquet').relative_to(ROOT)}")

    ann_df = pd.concat(annual_records, ignore_index=True)
    ann_df.sort_values(["weather_year", "utility", "substation_name"]).to_csv(
        out / "substation_annual_load.csv", index=False
    )
    print(f"  Saved: {(out / 'substation_annual_load.csv').relative_to(ROOT)}")

    print("\n  Total disaggregated IOU load by weather year (TWh):")
    for wy_val, twh in (ann_df.groupby("weather_year")["annual_mwh"].sum() / 1e6).items():
        print(f"    {wy_val}: {twh:.2f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args     = parse_args()
    load_col = resolve_load_col(args)
    wt       = args.weight_col
    wl       = args.weight_level
    tag      = run_tag(args, load_col)
    out      = output_dir(tag)
    save_output = args.save_output

    print(f"IOU disaggregation: source={args.source}  weight_col={wt}  weight_level={wl}  save_output={save_output}")
    print(f"Output directory: {out.relative_to(ROOT)}")

    # Load and build IOU weight matrices
    print("\nLoading substation profiles...")
    profiles = load_profiles(PROFILES, wt)
    profiles["utility"] = profiles["utility"].str.upper()  # normalise to PGE/SCE/SDGE

    print(f"Building IOU weight matrices ({wl} level, {wt})...")
    weight_matrices = build_iou_weight_matrices(profiles, level=wl, weight_col=wt, ious=IOUS)

    for iou, (subs, mat) in weight_matrices.items():
        col_sums = mat.sum(axis=0)  # (12, 24)
        max_err = abs(col_sums - 1.0).max()
        print(f"  {iou}: {len(subs)} substations  weight sum check = {max_err:.2e}")

    # Save weights CSV
    wt_df = iou_weights_to_df(profiles, weight_matrices, wt, wl)
    wt_df.to_csv(out / "substation_iou_weights.csv", index=False)
    print(f"  Saved: {(out / 'substation_iou_weights.csv').relative_to(ROOT)}")
    print(f"  Rows: {len(wt_df):,}  (substations: {wt_df['substation_name'].nunique():,})")

    # Run source-specific disaggregation
    if args.source == "iepr":
        run_iepr(weight_matrices, args.vintage, args.scenario, load_col, out, save_output)
    else:
        run_resolve(weight_matrices, load_col, out, save_output)

    print("\nDone.")


if __name__ == "__main__":
    main()
