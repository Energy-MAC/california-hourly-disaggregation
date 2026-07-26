"""RunConfig -- the single object that makes a cookbook run reproducible.

Everything that affects results (target column, feature mask, which models,
CV scheme, tuning budget, seed, output paths) lives here so a run is fully
described by one serializable object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RunConfig:
    # --- what to predict ---
    target: str                       # target column, e.g. "max_load"
    group_col: str                    # leakage grouping key, e.g. "substation_id"

    # --- features ---
    feature_cols: list[str]           # columns to feed the models (post-engineering)
    feature_config: str = "explanatory"   # "explanatory" | "imputable" -- for labeling/report

    # --- validation ---
    cv_scheme: str = "group"          # "group" | "spatial" | "time"
    n_splits: int = 5
    test_frac: float = 0.2            # fraction of GROUPS held out as the untouched test set
    coord_cols: tuple[str, str] = ("lat", "lon")   # for cv_scheme="spatial"
    n_spatial_blocks: int = 10

    # --- models / tuning ---
    models: list[str] = field(default_factory=list)   # names from ml.models.REGISTRY ([] = all)
    n_iter: int = 25                  # RandomizedSearchCV iterations per model
    scoring: str = "neg_root_mean_squared_error"
    baseline_model: str = "cell_mean"   # skill scores are computed relative to this

    # --- bookkeeping ---
    seed: int = 20260726
    label: str = "run"                # run tag used in output paths
    out_processed: Path | None = None
    out_checks: Path | None = None
    out_figures: Path | None = None
    save_predictions: bool = False    # write per-row predictions (large)

    def __post_init__(self) -> None:
        for p in (self.out_processed, self.out_checks, self.out_figures):
            if p is not None:
                Path(p).mkdir(parents=True, exist_ok=True)
