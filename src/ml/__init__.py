"""LEGACY (2026-08-14) -- reusable, target-agnostic ML prediction cookbook.

NOT part of the project's method. This project measures the impact of
DISAGGREGATING a known load across a network; it does not predict load, so a
supervised prediction problem is outside the question being asked. Nothing in
the disaggregation, nodal, or GenX pipelines imports this package. It is kept
because it is self-contained, its leakage guards pass, and its negative result
(structural features recover a substation's shape but not its magnitude) is the
documented reason cold-start imputation was never wired into the projection
pipeline. See docs/ml_cookbook.md.

--------------------------------------------------------------------------


A consistent methodology for supervised prediction so every model in the paper
is built and evaluated the same way: leakage-safe splitting, declarative feature
specs, a model registry with tuning spaces, a fixed metric suite, and standard
diagnostics -- orchestrated by `run_cookbook()`.

The cookbook is deliberately generic. Its first application (cross-sectional
substation-load prediction) is a *caller* in scripts/load_projection/ml/, not
part of this package; sequential-forecasting targets (e.g. EIA-930) reuse the
same machinery via the TimeSeriesSplit re-export in `splits`.
"""
from ml.config import RunConfig  # noqa: F401
