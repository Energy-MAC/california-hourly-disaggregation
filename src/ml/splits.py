"""Leakage-safe splitting -- the core of doing ML on non-IID data correctly.

Random k-fold is WRONG for this project's data: it would train on some cells of
a substation and test on other cells of the SAME substation, so the model just
memorizes each substation's level and the score is meaningless for the real
task (predicting a substation we have no data for). Every splitter here keeps a
whole group (substation) entirely on one side of the split.

- `group_holdout` / `GroupKFoldCV` -- hold out whole substations at random.
- `spatial_block_split` -- hold out whole spatial BLOCKS of substations (KMeans
  on coordinates) so test substations are geographically separated from train,
  the honest cold-start-imputation setting.
- `TimeSeriesSplit` is re-exported unchanged for future sequential targets
  (e.g. EIA-930 forecasting); the same cookbook then applies with no changes.

`assert_no_group_leakage` is the guard the plan requires: call it on any split
to prove train/test groups are disjoint.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.model_selection import GroupKFold, TimeSeriesSplit  # noqa: F401  (re-export)

__all__ = ["group_holdout", "spatial_blocks", "group_kfold_indices",
           "assert_no_group_leakage", "TimeSeriesSplit"]


def group_holdout(groups: pd.Series, test_frac: float, seed: int) -> np.ndarray:
    """Boolean mask (len == len(groups)) marking rows whose GROUP is held out as
    the final untouched test set. Whole groups go to test, never split rows."""
    uniq = pd.Index(groups.unique())
    rng = np.random.default_rng(seed)
    n_test = max(1, int(round(len(uniq) * test_frac)))
    test_groups = set(rng.choice(uniq, size=n_test, replace=False).tolist())
    return groups.isin(test_groups).to_numpy()


def spatial_blocks(coords: pd.DataFrame, n_blocks: int, seed: int) -> pd.Series:
    """Assign each row to a spatial block via KMeans on (lat, lon). Rows sharing
    a substation share coordinates and so land in the same block automatically.
    Returns an integer block label per row (NaN coords -> block -1)."""
    xy = coords.to_numpy(dtype=float)
    ok = ~np.isnan(xy).any(axis=1)
    labels = np.full(len(coords), -1, dtype=int)
    if ok.sum() >= n_blocks:
        km = KMeans(n_clusters=n_blocks, random_state=seed, n_init=10)
        labels[ok] = km.fit_predict(xy[ok])
    return pd.Series(labels, index=coords.index, name="spatial_block")


def group_kfold_indices(groups: pd.Series, n_splits: int):
    """Yield (train_idx, val_idx) positional arrays with disjoint groups.
    Thin wrapper over sklearn GroupKFold; X/y are unused by the split itself."""
    gkf = GroupKFold(n_splits=n_splits)
    dummy = np.zeros((len(groups), 1))
    yield from gkf.split(dummy, groups=groups.to_numpy())


def assert_no_group_leakage(groups: pd.Series, train_idx, test_idx) -> None:
    """Hard guard: raise if any group appears on both sides of a split."""
    g = groups.to_numpy()
    overlap = set(g[train_idx]) & set(g[test_idx])
    if overlap:
        raise AssertionError(
            f"group leakage: {len(overlap)} group(s) in both train and test, "
            f"e.g. {list(overlap)[:3]}")
