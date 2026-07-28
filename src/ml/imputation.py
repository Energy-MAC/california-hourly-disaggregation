"""Magnitude x shape decomposition for cold-start load-profile imputation.

A substation's 288-cell envelope is split into a scalar SIZE and normalized
SHAPE templates, estimated by separate procedures so each can be validated on
its own (both are only weakly predictable, so a single conflated number would
hide where the error lives):

    magnitude M   = mean_c(max_load)                     -- the substation "size"
    max_shape[c]  = max_load[c] / M                      -- normalized envelope
    min_shape[c]  = min_load[c] / M   (may be negative -- BTM reverse flow, kept)
    imputed[c]    = M_hat * shape_template[c]

Magnitude is predicted by the ml cookbook (structural regression) in the caller;
this module owns the SHAPE side (k-NN donor and group-average templates) plus
the decomposition, profile assembly, and shape/per-cell scorers built on
`ml.evaluate`. Nothing here is SCE-specific.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler

from ml import evaluate as ev

CELL = ["month", "hour"]


def decompose(prof: pd.DataFrame, id_col: str = "substation_id",
              max_col: str = "max_load", min_col: str = "min_load") -> tuple[pd.Series, pd.DataFrame]:
    """Return (magnitude per substation, long shape frame with max_shape/min_shape)."""
    mag = prof.groupby(id_col)[max_col].mean().rename("magnitude")
    p = prof.merge(mag, left_on=id_col, right_index=True)
    p["max_shape"] = p[max_col] / p["magnitude"]
    if min_col in p.columns:
        p["min_shape"] = p[min_col] / p["magnitude"]
    return mag, p


def _shape_wide(shape_long: pd.DataFrame, id_col: str, value: str) -> pd.DataFrame:
    """(substation x 288-cell) matrix of a normalized shape column."""
    w = shape_long.pivot_table(index=id_col, columns=CELL, values=value, aggfunc="mean")
    return w.sort_index(axis=1)


def knn_donor_templates(target_feat: pd.DataFrame, donor_feat: pd.DataFrame,
                        donor_shape_long: pd.DataFrame, feature_cols: list[str],
                        id_col: str, k: int, values=("max_shape", "min_shape")
                        ) -> tuple[dict[str, pd.DataFrame], pd.Series]:
    """For each target substation, template = mean normalized shape of its k
    nearest donor substations (standardized-feature distance, median-imputed).

    Returns ({value -> (target x 288-cell) template matrix}, donor-spread Series
    per target -- mean cross-donor std of max_shape, an uncertainty proxy)."""
    imp = SimpleImputer(strategy="median")
    sc = StandardScaler()
    Xd = sc.fit_transform(imp.fit_transform(donor_feat[feature_cols]))
    Xt = sc.transform(imp.transform(target_feat[feature_cols]))
    k = min(k, len(donor_feat))
    nn = NearestNeighbors(n_neighbors=k).fit(Xd)
    _, idx = nn.kneighbors(Xt)  # (n_targets, k) donor row positions

    donor_ids = donor_feat[id_col].to_numpy()
    target_ids = target_feat[id_col].to_numpy()
    out: dict[str, pd.DataFrame] = {}
    spread = None
    for value in values:
        if value not in donor_shape_long.columns:
            continue
        wide = _shape_wide(donor_shape_long, id_col, value).reindex(donor_ids)
        arr = wide.to_numpy()  # (n_donors, 288)
        neigh = arr[idx]       # (n_targets, k, 288)
        out[value] = pd.DataFrame(np.nanmean(neigh, axis=1), index=target_ids, columns=wide.columns)
        if value == "max_shape":
            spread = pd.Series(np.nanmean(np.nanstd(neigh, axis=1), axis=1),
                               index=target_ids, name="donor_spread")
    return out, spread


def group_templates(donor_shape_long: pd.DataFrame, group_map: pd.Series,
                    target_groups: pd.Series, id_col: str,
                    values=("max_shape", "min_shape"), min_donors: int = 3
                    ) -> dict[str, pd.DataFrame]:
    """Group-average normalized shape template (fallback/comparator to k-NN).
    `group_map`: donor id -> group key; `target_groups`: target id -> group key.

    Robustness: a group with fewer than `min_donors` distinct donors (or missing
    cells) falls back to the global mean template, so every target gets a
    complete 288-cell template (no single-donor noise, no NaN cells)."""
    glong = donor_shape_long.copy()
    glong["_g"] = glong[id_col].map(group_map)
    n_donors = glong.groupby("_g")[id_col].nunique()
    ok_groups = set(n_donors[n_donors >= min_donors].index)
    out: dict[str, pd.DataFrame] = {}
    for value in values:
        if value not in glong.columns:
            continue
        globalmean = glong.groupby(CELL)[value].mean().sort_index()
        cell_index = globalmean.index
        gmean = glong[glong._g.isin(ok_groups)].groupby(["_g", *CELL])[value].mean()
        rows = {}
        for tid, g in target_groups.items():
            if g in ok_groups:
                tmpl = gmean.loc[g].reindex(cell_index).fillna(globalmean)
            else:
                tmpl = globalmean
            rows[tid] = tmpl
        out[value] = pd.DataFrame(rows).T
    return out


def assemble_profiles(magnitude: pd.Series, templates: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """profile = magnitude * shape_template, long form with imputed min/max_load."""
    frames = []
    name = {"max_shape": "max_load", "min_shape": "min_load"}
    for value, wide in templates.items():
        long = wide.stack(CELL, future_stack=True).rename(name[value]).reset_index()
        long = long.rename(columns={"level_0": "substation_id"})
        frames.append(long.set_index(["substation_id", *CELL]))
    prof = pd.concat(frames, axis=1).reset_index()
    prof = prof.merge(magnitude.rename("magnitude"), left_on="substation_id", right_index=True)
    for value, col in name.items():
        if col in prof.columns:
            prof[col] = prof[col] * prof["magnitude"]
    return prof


# ── scorers (separated, per the plan) ────────────────────────────────────────

def shape_scores(pred_wide: pd.DataFrame, true_wide: pd.DataFrame) -> pd.DataFrame:
    """Per-substation Pearson correlation and normalized RMSE between predicted
    and actual normalized shape (only substations present in both)."""
    ids = pred_wide.index.intersection(true_wide.index)
    rows = []
    for sid in ids:
        p = pred_wide.loc[sid].to_numpy(float)
        t = true_wide.loc[sid].to_numpy(float)
        ok = ~(np.isnan(p) | np.isnan(t))
        if ok.sum() < 3 or np.nanstd(t[ok]) == 0:
            continue
        corr = np.corrcoef(p[ok], t[ok])[0, 1]
        nrmse = np.sqrt(np.mean((p[ok] - t[ok]) ** 2)) / (np.mean(np.abs(t[ok])) or np.nan)
        rows.append({"substation_id": sid, "shape_corr": corr, "shape_nrmse": nrmse})
    return pd.DataFrame(rows)


def percell_scores(pred_long: pd.DataFrame, true_long: pd.DataFrame, value: str) -> dict:
    """Pooled per-cell RMSE/MAE/WAPE of imputed vs actual load on matched cells."""
    m = true_long.merge(pred_long, on=["substation_id", *CELL], suffixes=("_true", "_pred"))
    return ev.core_metrics(m[f"{value}_true"], m[f"{value}_pred"])
