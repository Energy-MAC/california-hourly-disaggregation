"""Hyperparameter tuning bound to the leakage-safe CV.

The single rule this enforces: tuning uses the SAME group-aware splits as
evaluation, so hyperparameters are never chosen by peeking across a substation.
RandomizedSearchCV over GroupKFold; empty search space -> fit once, no search
(baselines, plain linear).
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupKFold, RandomizedSearchCV


def tune_or_fit(factory, param_dist: dict, X: pd.DataFrame, y, groups: pd.Series,
                n_splits: int, n_iter: int, scoring: str, seed: int):
    """Return a fitted estimator and the chosen params.

    - empty param_dist: fit the factory estimator directly (nothing to tune).
    - otherwise: RandomizedSearchCV with GroupKFold(n_splits), refit on all rows.
    """
    est = factory()
    n_groups = groups.nunique()
    splits = min(n_splits, n_groups)
    if not param_dist or splits < 2:
        est.fit(X, y)
        return est, {}

    cv = GroupKFold(n_splits=splits)
    search = RandomizedSearchCV(
        est, param_distributions=param_dist, n_iter=n_iter, scoring=scoring,
        cv=cv.split(X, y, groups=groups.to_numpy()), random_state=seed,
        n_jobs=-1, refit=True, error_score="raise")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        search.fit(X, y)
    return search.best_estimator_, search.best_params_
