"""Generic, leakage-aware feature transforms.

Kept target-agnostic on purpose: application-specific joins (substation ->
county population, load fraction, etc.) belong in the caller, while the reusable
primitives -- cyclical calendar encoding, within-group "diurnal-neighbor" lags,
and the explanatory-vs-imputable feature mask -- live here.

Leakage note on lags: `add_cyclic_neighbors` builds features from OTHER cells of
the same group (e.g. neighboring hours of the same substation). These are only
valid in the EXPLANATORY setting ("given part of a substation, predict the
rest") -- never in cold-start imputation, where a group has no observed cells.
The caller must exclude them from the imputable `FeatureSpec`; `FeatureSpec`
makes that split explicit.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def add_cyclic(df: pd.DataFrame, col: str, period: int) -> list[str]:
    """Encode a periodic integer column as (sin, cos) so the model sees hour 23
    and hour 0 as adjacent. Mutates df; returns the new column names."""
    s = df[col].astype(float)
    df[f"{col}_sin"] = np.sin(2 * np.pi * s / period)
    df[f"{col}_cos"] = np.cos(2 * np.pi * s / period)
    return [f"{col}_sin", f"{col}_cos"]


def add_cyclic_neighbors(df: pd.DataFrame, group_col: str, order_col: str,
                         value_col: str, period: int, offsets=(1,)) -> list[str]:
    """Diurnal-neighbor lags: within each group, the value at order_col +/- k
    (wrapping mod `period`). EXPLANATORY-ONLY (see module docstring). Mutates df
    in place via a (group, order) lookup; returns the new column names."""
    lookup = df.set_index([group_col, order_col])[value_col]
    if lookup.index.has_duplicates:
        raise ValueError(f"({group_col}, {order_col}) is not unique; cannot build "
                         "neighbor lags without ambiguity")
    new_cols = []
    for k in offsets:
        for sign, tag in ((k, f"p{k}"), (-k, f"m{k}")):
            shifted_order = (df[order_col] + sign) % period
            keys = pd.MultiIndex.from_arrays([df[group_col], shifted_order])
            name = f"{value_col}_nbr_{tag}"
            df[name] = lookup.reindex(keys).to_numpy()
            new_cols.append(name)
    return new_cols


@dataclass
class FeatureSpec:
    """Declares which engineered columns each configuration may use.

    `explanatory` is the full set; `imputable` is the subset that also exists for
    substations we have no profile for (location, voltage, county proxies,
    calendar) -- the only columns a cold-start imputation model may touch. Keeping
    both here, next to each other, is what prevents the imputable model from
    silently depending on a scraped-only or lag feature.
    """
    explanatory: list[str]
    imputable: list[str]

    def cols(self, config: str) -> list[str]:
        if config == "explanatory":
            return list(self.explanatory)
        if config == "imputable":
            return list(self.imputable)
        raise ValueError(f"unknown feature config {config!r}")

    def assert_imputable_available(self, available: set[str]) -> None:
        """Guard: every imputable feature must exist for the imputation targets."""
        missing = [c for c in self.imputable if c not in available]
        if missing:
            raise AssertionError(
                f"imputable feature(s) absent from imputation-target columns: {missing}")
