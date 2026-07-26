"""Standard diagnostics -- the same figures/tables for every run.

Consistency is the point: whichever model wins, the paper shows the same
actual-vs-predicted, residual, error-by-cell, and model-comparison views so runs
are comparable at a glance.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.inspection import permutation_importance


def comparison_bar(comparison: pd.DataFrame, fig_dir: Path, metric: str = "skill_vs_baseline") -> Path:
    fig, ax = plt.subplots(figsize=(9, 5))
    d = comparison.sort_values(metric)
    colors = ["#d73027" if v < 0 else "#1a9850" for v in d[metric]]
    ax.barh(d["model"], d[metric], color=colors)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.set_xlabel(metric)
    ax.set_title(f"model comparison ({comparison.attrs.get('config', '')})")
    fig.tight_layout()
    p = fig_dir / f"model_comparison_{comparison.attrs.get('config', 'run')}.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def diagnostics(df_test: pd.DataFrame, y_col: str, pred_col: str, model_name: str,
                fig_dir: Path, cell_cols=("month", "hour")) -> Path:
    """3-panel: actual-vs-pred, residual-vs-pred, and error heatmap over the two
    calendar cell dims."""
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    y, yhat = df_test[y_col].to_numpy(), df_test[pred_col].to_numpy()

    ax = axes[0]
    ax.scatter(y, yhat, s=6, alpha=0.3)
    lim = [min(y.min(), yhat.min()), max(y.max(), yhat.max())]
    ax.plot(lim, lim, "--", color="grey", linewidth=1)
    ax.set_xlabel(f"actual {y_col}")
    ax.set_ylabel("predicted")
    ax.set_title(f"{model_name}: actual vs predicted")

    ax = axes[1]
    ax.scatter(yhat, y - yhat, s=6, alpha=0.3)
    ax.axhline(0, color="grey", linestyle="--", linewidth=1)
    ax.set_xlabel("predicted")
    ax.set_ylabel("residual (actual - pred)")
    ax.set_title("residuals")

    ax = axes[2]
    c0, c1 = cell_cols
    piv = (df_test.assign(_ae=np.abs(y - yhat))
           .pivot_table(index=c0, columns=c1, values="_ae", aggfunc="mean"))
    im = ax.imshow(piv.values, aspect="auto", cmap="magma", origin="lower")
    ax.set_xlabel(c1)
    ax.set_ylabel(c0)
    ax.set_title("mean abs error by cell")
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)

    fig.tight_layout()
    p = fig_dir / f"diagnostics_{model_name}.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def permutation_importance_plot(estimator, X_test, y_test, feature_cols, model_name,
                                fig_dir: Path, seed: int, n_repeats: int = 8) -> Path | None:
    try:
        r = permutation_importance(estimator, X_test, y_test, n_repeats=n_repeats,
                                   random_state=seed, scoring="neg_root_mean_squared_error")
    except Exception as e:  # some estimators/pipelines may not support it cleanly
        print(f"  (permutation importance skipped for {model_name}: {e})")
        return None
    order = np.argsort(r.importances_mean)
    fig, ax = plt.subplots(figsize=(8, max(3, 0.3 * len(feature_cols))))
    ax.barh(np.array(feature_cols)[order], r.importances_mean[order],
            xerr=r.importances_std[order], color="#4575b4")
    ax.set_xlabel("increase in RMSE when shuffled")
    ax.set_title(f"{model_name}: permutation importance")
    fig.tight_layout()
    p = fig_dir / f"importance_{model_name}.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p
