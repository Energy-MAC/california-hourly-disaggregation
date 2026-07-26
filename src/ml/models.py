"""Model registry -- every candidate as (estimator factory, tuning space).

One place defines what "the models" are so every run compares the same set the
same way. Each entry returns a fresh sklearn-compatible estimator (Pipelines
handle scaling/imputation where the model needs it) plus a param-distribution
dict for RandomizedSearchCV (empty {} = no tuning, e.g. baselines).

Baselines are real estimators so they flow through the identical eval path:
- `cell_mean`   -- predicts the mean target of each (calendar) cell, ignoring the
                   substation; the "no structural information" reference.
- `global_mean` -- predicts one constant; the floor any model must clear.

Tree models (HistGB/XGB/LGBM) get raw features (native NaN handling, scale-
invariant). Linear/SVR get median-imputed + standardized features. `arx_ols` is
a statsmodels OLS wrapper exposing coefficients/p-values for interpretation.
"""
from __future__ import annotations

import numpy as np
from scipy.stats import loguniform, randint, uniform
from sklearn.base import BaseEstimator, RegressorMixin
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import ElasticNet, Lasso, LinearRegression, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVR

try:
    from xgboost import XGBRegressor
    _HAS_XGB = True
except ImportError:
    _HAS_XGB = False
try:
    from lightgbm import LGBMRegressor
    _HAS_LGBM = True
except ImportError:
    _HAS_LGBM = False
try:
    import statsmodels.api as sm
    _HAS_SM = True
except ImportError:
    _HAS_SM = False


# ── baselines ────────────────────────────────────────────────────────────────

class CellMeanRegressor(BaseEstimator, RegressorMixin):
    """Predict the mean target within each cell defined by `cell_cols` (e.g.
    month, hour), falling back to the global mean for unseen cells. Ignores every
    structural feature -- the reference a real model must beat."""

    def __init__(self, cell_cols=("month", "hour")):
        self.cell_cols = list(cell_cols)

    def fit(self, X, y):
        df = X[self.cell_cols].copy()
        df["_y"] = np.asarray(y)
        self.table_ = df.groupby(self.cell_cols)["_y"].mean()
        self.global_ = float(np.mean(y))
        return self

    def predict(self, X):
        keys = X[self.cell_cols]
        idx = list(map(tuple, keys.to_numpy())) if len(self.cell_cols) > 1 \
            else keys.iloc[:, 0].to_numpy()
        return self.table_.reindex(idx).fillna(self.global_).to_numpy()


class GlobalMeanRegressor(BaseEstimator, RegressorMixin):
    """Predict a single constant (the training mean). The absolute floor."""

    def fit(self, X, y):
        self.mean_ = float(np.mean(y))
        return self

    def predict(self, X):
        return np.full(len(X), self.mean_)


class ARXOLSRegressor(BaseEstimator, RegressorMixin):
    """statsmodels OLS on the (already-engineered, numeric) feature matrix, with
    a constant. Behaves like sklearn but retains `.summary()` for the paper's
    interpretability table. Median-imputes NaNs to stay robust."""

    def fit(self, X, y):
        Xn = np.asarray(X, dtype=float)
        self.medians_ = np.nanmedian(Xn, axis=0)
        Xn = self._impute(Xn)
        self.model_ = sm.OLS(np.asarray(y, dtype=float), sm.add_constant(Xn, has_constant="add")).fit()
        return self

    def _impute(self, Xn):
        inds = np.where(np.isnan(Xn))
        Xn[inds] = np.take(self.medians_, inds[1])
        return Xn

    def predict(self, X):
        Xn = self._impute(np.asarray(X, dtype=float))
        return self.model_.predict(sm.add_constant(Xn, has_constant="add"))


# ── pipeline helpers ─────────────────────────────────────────────────────────

def _scaled(estimator):
    """median-impute -> standardize -> estimator (for linear/SVR)."""
    return Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("model", estimator),
    ])


# ── registry ─────────────────────────────────────────────────────────────────
# name -> (factory() -> estimator, param_distributions for RandomizedSearchCV)
# param keys are prefixed "model__" when the estimator is wrapped in _scaled().

def build_registry(cell_cols=("month", "hour")) -> dict:
    reg: dict[str, tuple] = {
        "global_mean": (lambda: GlobalMeanRegressor(), {}),
        "cell_mean": (lambda: CellMeanRegressor(cell_cols=cell_cols), {}),
        "linear": (lambda: _scaled(LinearRegression()), {}),
        "ridge": (lambda: _scaled(Ridge()),
                  {"model__alpha": loguniform(1e-2, 1e3)}),
        "lasso": (lambda: _scaled(Lasso(max_iter=5000)),
                  {"model__alpha": loguniform(1e-3, 1e1)}),
        "elasticnet": (lambda: _scaled(ElasticNet(max_iter=5000)),
                       {"model__alpha": loguniform(1e-3, 1e1),
                        "model__l1_ratio": uniform(0.05, 0.9)}),
        "svr": (lambda: _scaled(SVR(kernel="rbf")),
                {"model__C": loguniform(1e-1, 1e3),
                 "model__gamma": loguniform(1e-4, 1e0),
                 "model__epsilon": loguniform(1e-3, 1e0)}),
        "hist_gbm": (lambda: HistGradientBoostingRegressor(random_state=0),
                     {"learning_rate": loguniform(1e-2, 3e-1),
                      "max_leaf_nodes": randint(15, 64),
                      "max_depth": randint(3, 12),
                      "min_samples_leaf": randint(20, 200),
                      "l2_regularization": loguniform(1e-3, 1e1)}),
    }
    if _HAS_SM:
        reg["arx_ols"] = (lambda: _scaled(ARXOLSRegressor()), {})
    if _HAS_XGB:
        reg["xgboost"] = (
            lambda: XGBRegressor(random_state=0, n_estimators=400, tree_method="hist",
                                 n_jobs=-1, verbosity=0),
            {"learning_rate": loguniform(1e-2, 3e-1),
             "max_depth": randint(3, 10),
             "subsample": uniform(0.6, 0.4),
             "colsample_bytree": uniform(0.6, 0.4),
             "min_child_weight": randint(1, 10),
             "reg_lambda": loguniform(1e-2, 1e1)})
    if _HAS_LGBM:
        reg["lightgbm"] = (
            lambda: LGBMRegressor(random_state=0, n_estimators=400, n_jobs=-1, verbose=-1),
            {"learning_rate": loguniform(1e-2, 3e-1),
             "num_leaves": randint(15, 128),
             "max_depth": randint(3, 12),
             "subsample": uniform(0.6, 0.4),
             "colsample_bytree": uniform(0.6, 0.4),
             "min_child_samples": randint(10, 100),
             "reg_lambda": loguniform(1e-2, 1e1)})
    return reg


# Kernel SVR is O(n^2-n^3); it cannot fit on hundreds of thousands of rows.
# Models listed here are trained on a seeded row subsample when the training set
# exceeds the cap (CV/groups preserved on the subsample). Documented, not hidden.
MAX_TRAIN_ROWS = {"svr": 15000}


def available_models() -> list[str]:
    return list(build_registry().keys())
