"""First application of the ML cookbook (src/ml): cross-sectional prediction of
substation per-cell load.

The substation "profiles" are month x hour percentile envelopes (max_load /
min_load per (substation, month, hour) cell), NOT time series -- so this is
cross-sectional regression, validated by holding out WHOLE substations (never
random cells; see src/ml/splits.py). Two feature configurations of the same
cookbook:

  explanatory -- every available feature incl. SCE-only attributes (customer
    mix, DER, projected load) and diurnal-neighbor lags. Best fit / driver
    analysis; answers "given part of a substation, predict the rest".
  imputable   -- only features that also exist for substations we have NO profile
    for (location, voltage, county population / load fraction / BTM-PV, calendar;
    no lags). Validated by SPATIAL block hold-out. This is the configuration that
    can be applied to the unscraped CEC substations (see coverage audit).

Feature joins reuse existing artifacts (never recomputed here):
  substation_county_reeds_mapping.csv  -- substation -> lat/lon, county,
                                          ca_load_fraction, BTM-PV by year
  ReEDS county_population.csv           -- county -> population
  substation_attributes_clean.csv      -- highside_kv + SCE-only rich attributes

CLI parameters:
  --target {max_load,min_load}   cell target (default max_load)
  --config {explanatory,imputable,both}   which configuration(s) (default both)
  --models CSV                   subset of models (default: all in the registry)
  --n-iter N                     RandomizedSearchCV iterations per model (default 25)
  --max-rows N                   subsample this many rows total (dev speed; default all)
  --seed N                       (default 20260726)
  --save-output                  also write per-row predictions

Outputs:
  data/checks/ml/substation_load/comparison_{config}.csv, segmented_errors_*, tuned_params_*
  data/figures/ml/substation_load/model_comparison_*, diagnostics_*, importance_*

Usage:
  python scripts/load_projection/ml/predict_substation_load.py
  python scripts/load_projection/ml/predict_substation_load.py --config imputable --target min_load
  python scripts/load_projection/ml/predict_substation_load.py --models cell_mean,arx_ols,hist_gbm --max-rows 40000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/nodal"))
from ml.config import RunConfig  # noqa: E402
from ml.features import FeatureSpec, add_cyclic, add_cyclic_neighbors  # noqa: E402
from ml.pipeline import run_cookbook, write_outputs  # noqa: E402
from substation_features import (COUNTY_FEATURES, IMPUTABLE_STRUCT, RICH_ATTRS,  # noqa: E402
                                 scraped_structural)

PROF_FILE = ROOT / "data/processed/substations/substation_load_profiles_clean.csv"

OUT_CHECKS = ROOT / "data/checks/ml/substation_load"
OUT_FIG = ROOT / "data/figures/ml/substation_load"
OUT_PROC = ROOT / "data/processed/ml/substation_load"


def assemble(target: str) -> tuple[pd.DataFrame, FeatureSpec, list[str]]:
    """Build the (substation, month, hour) modeling frame + the FeatureSpec.
    Structural features come from the shared substation_features module (one
    source of truth); calendar + diurnal lags are engineered here at cell level."""
    prof = pd.read_csv(PROF_FILE, usecols=["utility", "substation_name", "month",
                                           "hour_pst", target]).rename(
        columns={"hour_pst": "hour"})
    prof = prof.dropna(subset=[target]).copy()
    prof["substation_id"] = prof.utility + "|" + prof.substation_name

    struct = scraped_structural()  # per-substation structural features + one-hots
    df = prof.merge(struct, on=["utility", "substation_name"], how="left")

    # --- engineered calendar features ---
    cal_cols = add_cyclic(df, "month", 12) + add_cyclic(df, "hour", 24)

    # --- diurnal-neighbor lags of the target (EXPLANATORY ONLY) ---
    df["_submonth"] = df.substation_id + "|" + df.month.astype(str)
    lag_cols = add_cyclic_neighbors(df, "_submonth", "hour", target, period=24, offsets=(1, 2))

    # imputable = shared structural set (location, voltage, county, one-hots)
    #             + calendar (raw + cyclic); explanatory adds SCE-only + lags
    imputable = IMPUTABLE_STRUCT + ["month", "hour", *cal_cols]
    explanatory = imputable + RICH_ATTRS + lag_cols

    spec = FeatureSpec(explanatory=explanatory, imputable=imputable)
    spec.assert_imputable_available(set(df.columns))
    return df, spec, ["month", "hour"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", choices=["max_load", "min_load"], default="max_load")
    ap.add_argument("--config", choices=["explanatory", "imputable", "both"], default="both")
    ap.add_argument("--models", default=None, help="comma-separated subset of models")
    ap.add_argument("--n-iter", type=int, default=25)
    ap.add_argument("--max-rows", type=int, default=None,
                    help="subsample this many rows total (dev speed)")
    ap.add_argument("--seed", type=int, default=20260726)
    ap.add_argument("--save-output", action="store_true")
    args = ap.parse_args()

    df, spec, cell_cols = assemble(args.target)
    if args.max_rows and len(df) > args.max_rows:
        # subsample WHOLE substations so groups stay intact
        rng = np.random.default_rng(args.seed)
        subs = df.substation_id.unique()
        n = max(2, int(round(len(subs) * args.max_rows / len(df))))
        keep = set(rng.choice(subs, size=min(n, len(subs)), replace=False))
        df = df[df.substation_id.isin(keep)].reset_index(drop=True)
    print(f"assembled {len(df):,} rows across {df.substation_id.nunique()} substations "
          f"(target={args.target})")

    models = args.models.split(",") if args.models else []
    configs = ["explanatory", "imputable"] if args.config == "both" else [args.config]
    schemes = {"explanatory": "group", "imputable": "spatial"}

    for feat_config in configs:
        print(f"\n=== config: {feat_config} ({schemes[feat_config]} hold-out) ===")
        cfg = RunConfig(
            target=args.target, group_col="substation_id",
            feature_cols=spec.cols(feat_config), feature_config=feat_config,
            cv_scheme=schemes[feat_config], coord_cols=("lat", "lon"),
            models=models, n_iter=args.n_iter, seed=args.seed,
            label=f"substation_{args.target}",
            out_checks=OUT_CHECKS, out_figures=OUT_FIG, out_processed=OUT_PROC,
            save_predictions=args.save_output)
        result = run_cookbook(df, cfg, cell_cols=cell_cols,
                              segment_cols=["month", "hour", "utility", "sub_kv_class"])
        write_outputs(result, cfg, spec.cols(feat_config))
        print(result["comparison"].to_string(index=False))

    print(f"\nwrote -> {OUT_CHECKS.relative_to(ROOT)}/ and {OUT_FIG.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
