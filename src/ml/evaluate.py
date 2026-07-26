"""Metric suite -- one fixed definition of "how good", used for every model.

Deliberately NO MAPE: this project's loads go negative (BTM reverse flow) and
near-zero, so MAPE explodes/undefines. WAPE (sum|err| / sum|actual|) is the
robust percentage analogue. Every model is also reported as a SKILL score vs a
baseline (1 - RMSE/RMSE_baseline > 0 means it beats the baseline), because raw
RMSE alone lets a model look good while beating only a trivial reference.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def rmse(y, yhat) -> float:
    return float(np.sqrt(np.mean((np.asarray(y) - np.asarray(yhat)) ** 2)))


def mae(y, yhat) -> float:
    return float(np.mean(np.abs(np.asarray(y) - np.asarray(yhat))))


def r2(y, yhat) -> float:
    y = np.asarray(y, dtype=float)
    ss_res = np.sum((y - np.asarray(yhat)) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    return float(1 - ss_res / ss_tot) if ss_tot > 0 else float("nan")


def wape(y, yhat) -> float:
    """Weighted absolute percentage error: sum|err| / sum|actual|. MAPE-robust."""
    denom = np.sum(np.abs(np.asarray(y)))
    return float(np.sum(np.abs(np.asarray(y) - np.asarray(yhat))) / denom) if denom else float("nan")


def peak_cell_mae(df: pd.DataFrame, group_col: str, y_col: str, pred_col: str) -> float:
    """Mean abs error on each group's PEAK cell (the row with the largest actual
    target) -- grid planning cares most about getting the peak right."""
    idx = df.groupby(group_col)[y_col].idxmax()
    peaks = df.loc[idx]
    return mae(peaks[y_col], peaks[pred_col])


def core_metrics(y, yhat) -> dict:
    return {"rmse": rmse(y, yhat), "mae": mae(y, yhat),
            "r2": r2(y, yhat), "wape": wape(y, yhat)}


def skill(model_rmse: float, baseline_rmse: float) -> float:
    """1 - RMSE_model / RMSE_baseline. >0 beats baseline, 0 ties, <0 worse."""
    return float(1 - model_rmse / baseline_rmse) if baseline_rmse else float("nan")


def segmented_errors(df: pd.DataFrame, y_col: str, pred_col: str,
                     by: list[str]) -> pd.DataFrame:
    """Per-segment RMSE/MAE/WAPE (e.g. by hour, month, utility, voltage class),
    to expose where a model is weak rather than only a pooled number."""
    rows = []
    for seg_col in by:
        for val, g in df.groupby(seg_col):
            rows.append({"segment": seg_col, "value": val, "n": len(g),
                         **core_metrics(g[y_col], g[pred_col])})
    return pd.DataFrame(rows)
