"""run_cookbook -- the one consistent flow every prediction task reuses.

    prep -> leakage-safe split -> baselines + tuned models -> evaluate on the
    untouched test set -> comparison table + diagnostics.

The caller supplies a fully-assembled frame (target, group key, engineered
feature columns, calendar/segment columns, coordinates) and a RunConfig. This
module owns nothing task-specific -- it just guarantees the methodology.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ml import evaluate as ev
from ml import report as rp
from ml.config import RunConfig
from ml.models import MAX_TRAIN_ROWS, build_registry
from ml.splits import assert_no_group_leakage, group_holdout, spatial_blocks
from ml.tuning import tune_or_fit

PRED_COL = "_pred"


def _test_mask(df: pd.DataFrame, cfg: RunConfig) -> np.ndarray:
    """Choose the untouched test rows by holding out whole groups (or whole
    spatial blocks for the imputation setting) -- never individual rows."""
    if cfg.cv_scheme == "spatial":
        blocks = spatial_blocks(df[list(cfg.coord_cols)], cfg.n_spatial_blocks, cfg.seed)
        rng = np.random.default_rng(cfg.seed)
        valid = [b for b in blocks.unique() if b >= 0]
        n_test = max(1, int(round(len(valid) * cfg.test_frac)))
        test_blocks = set(rng.choice(valid, size=n_test, replace=False).tolist())
        return blocks.isin(test_blocks).to_numpy()
    return group_holdout(df[cfg.group_col], cfg.test_frac, cfg.seed)


def run_cookbook(df: pd.DataFrame, cfg: RunConfig, cell_cols=("month", "hour"),
                 segment_cols=None) -> dict:
    """Fit every model in cfg.models (or all) and return results.

    Returns {comparison, segmented, predictions, estimators, params, test_mask}.
    """
    segment_cols = segment_cols or list(cell_cols)
    registry = build_registry(cell_cols=cell_cols)
    model_names = cfg.models or list(registry.keys())

    test_mask = _test_mask(df, cfg)
    train_idx = np.flatnonzero(~test_mask)
    test_idx = np.flatnonzero(test_mask)
    assert_no_group_leakage(df[cfg.group_col], train_idx, test_idx)

    X = df[cfg.feature_cols]
    y = df[cfg.target].to_numpy()
    groups = df[cfg.group_col]
    X_tr, y_tr, g_tr = X.iloc[train_idx], y[train_idx], groups.iloc[train_idx]
    df_te = df.iloc[test_idx].copy()

    print(f"  [{cfg.feature_config}] {len(train_idx):,} train rows / {len(test_idx):,} test rows "
          f"({g_tr.nunique()} train groups, {groups.iloc[test_idx].nunique()} test groups); "
          f"{len(cfg.feature_cols)} features; models: {', '.join(model_names)}")

    rows, seg_frames, estimators, params, preds = [], [], {}, {}, {}
    for name in model_names:
        factory, space = registry[name]
        Xf, yf, gf = X_tr, y_tr, g_tr
        cap = MAX_TRAIN_ROWS.get(name)
        if cap and len(X_tr) > cap:
            sub = np.random.default_rng(cfg.seed).choice(len(X_tr), size=cap, replace=False)
            Xf, yf, gf = X_tr.iloc[sub], y_tr[sub], g_tr.iloc[sub]
            print(f"    ({name}: subsampled {cap:,} of {len(X_tr):,} train rows -- kernel cost)")
        est, best = tune_or_fit(factory, space, Xf, yf, gf,
                                cfg.n_splits, cfg.n_iter, cfg.scoring, cfg.seed)
        yhat = est.predict(df_te[cfg.feature_cols])
        df_te[PRED_COL] = yhat
        m = ev.core_metrics(df_te[cfg.target], yhat)
        m["peak_cell_mae"] = ev.peak_cell_mae(df_te, cfg.group_col, cfg.target, PRED_COL)
        rows.append({"model": name, "config": cfg.feature_config, **m})
        seg = ev.segmented_errors(df_te, cfg.target, PRED_COL, segment_cols)
        seg.insert(0, "model", name)
        seg_frames.append(seg)
        estimators[name], params[name], preds[name] = est, best, yhat
        print(f"    {name:12s} rmse={m['rmse']:.3f}  mae={m['mae']:.3f}  "
              f"r2={m['r2']:.3f}  wape={m['wape']:.3f}")

    comparison = pd.DataFrame(rows)
    base_rmse = comparison.loc[comparison.model == cfg.baseline_model, "rmse"]
    base_rmse = float(base_rmse.iloc[0]) if len(base_rmse) else float("nan")
    comparison["skill_vs_baseline"] = comparison["rmse"].map(lambda r: ev.skill(r, base_rmse))
    comparison = comparison.sort_values("rmse").reset_index(drop=True)
    comparison.attrs["config"] = cfg.feature_config

    return {"comparison": comparison,
            "segmented": pd.concat(seg_frames, ignore_index=True),
            "predictions": preds, "estimators": estimators, "params": params,
            "test_df": df_te, "test_mask": test_mask, "cell_cols": cell_cols}


def write_outputs(result: dict, cfg: RunConfig, feature_cols: list[str]) -> None:
    """Persist the comparison/segmented tables, tuned params, and standard
    figures for the best non-baseline model."""
    comp, seg = result["comparison"], result["segmented"]
    if cfg.out_checks:
        comp.to_csv(cfg.out_checks / f"comparison_{cfg.feature_config}.csv", index=False)
        seg.to_csv(cfg.out_checks / f"segmented_errors_{cfg.feature_config}.csv", index=False)
        pd.DataFrame([{"model": k, **v} for k, v in result["params"].items()]).to_csv(
            cfg.out_checks / f"tuned_params_{cfg.feature_config}.csv", index=False)

    if cfg.out_figures:
        rp.comparison_bar(comp, cfg.out_figures)
        baselines = {"global_mean", "cell_mean"}
        best = next((m for m in comp.model if m not in baselines), comp.model.iloc[0])
        df_te = result["test_df"].copy()
        df_te["_pred"] = result["predictions"][best]
        rp.diagnostics(df_te, cfg.target, "_pred", f"{best}_{cfg.feature_config}",
                       cfg.out_figures, cell_cols=result["cell_cols"])
        rp.permutation_importance_plot(
            result["estimators"][best], df_te[feature_cols], df_te[cfg.target].to_numpy(),
            feature_cols, f"{best}_{cfg.feature_config}", cfg.out_figures, cfg.seed)

    if cfg.save_predictions and cfg.out_processed:
        out = result["test_df"][[cfg.group_col, *result["cell_cols"], cfg.target]].copy()
        for name, yhat in result["predictions"].items():
            out[f"pred_{name}"] = yhat
        out.to_csv(cfg.out_processed / f"predictions_{cfg.feature_config}.csv", index=False)
