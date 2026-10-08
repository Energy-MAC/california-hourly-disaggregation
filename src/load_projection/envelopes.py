"""Percentile envelope -> normal marginal parameters (Approach 2 input layer).

The model conditions on a per-cell normal marginal `Normal(mu, sigma^2)` for each
unit (substation, or any other load-carrying unit). Historically those two
parameters came from exactly two fixed quantiles -- the utilities' 10th/90th
percentile envelopes -- via the closed form `mu = (q10 + q90) / 2`,
`sigma = (q90 - q10) / (2 * Z90)`. That closed form is not a separate assumption:
it is the exact solution of

    L_k = mu + sigma * Phi^-1(p_k)

at K = 2, p = {0.10, 0.90}. This module solves that relation for an ARBITRARY set
of percentiles, so an input carrying one value per unit per cell (say a median) or
five values is expressible without changing anything downstream.

Backward compatibility is exact, not approximate. At K = 2 the fit uses the
two-point difference form `sigma = (L_1 - L_0) / (z_1 - z_0)`, and because
`Phi^-1(0.9) - Phi^-1(0.1) == 2 * Z90` holds exactly in float64, the result is
bit-for-bit the historical `(q90 - q10) / (2 * Z90)`. The general OLS normal
equations differ in the last one or two bits (~4e-16 relative), which is why K = 2
dispatches to the difference form rather than falling through to the general path.

ONE percentile does not identify sigma, so a prior supplies it -- see
`SigmaSource`. At p = 0.5, `Phi^-1(0.5) = 0` and therefore `mu == L` exactly,
whatever the prior.

The UNIFORM family is deliberately NOT handled here. It stays frozen on its
original code path in `stochastic.py` and is only reachable at the legacy
settings (2 percentiles, `monthhour` cells); generalizing it means fitting
`L_k ~ a + width * p_k` (OLS on p, not on Phi^-1(p)) and is a TODO.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import norm as _norm

Z90 = 1.2815515655446004  # Phi^-1(0.9); the historical constant, kept verbatim

LEGACY_PERCENTILES = (0.10, 0.90)

SIGMA_SOURCES = ("input-crosssec", "pinned-rho", "scalar", "proportional-cv")


@dataclass(frozen=True)
class SigmaSource:
    """How sigma is supplied when the input carries a single percentile.

    input-crosssec  sigma(c) = sd across units of the input loads in cell c.
                    One sigma per cell, IDENTICAL for every unit. This is the
                    default. NOTE it measures inter-unit INEQUALITY, which mu
                    already carries; used as sigma it becomes hour-to-hour noise
                    and drives rho toward 0 (measured 0.0157 on the 2,471 loaded
                    CATS buses, vs 0.2305 for the CA month-hour system), while
                    sigma/mu reaches 2.45 so 82.3% of units spend >10% of their
                    time negative. The run reports rho and P(L<0) so this is
                    visible, not silent.
    pinned-rho      sigma(c) = f * sd_c / (N * sqrt(rho_target)), the value that
                    makes rho(c) come out at `rho_target` by construction. Also
                    identical across units.
    scalar          sigma(c) = `value`, a flat MW figure for every unit and cell.
    proportional-cv sigma_s(c) = cv * mu_s(c), so sigma scales with unit size and
                    sigma/mu is constant. `cv` may be a scalar or a per-cell
                    Series.

    Feasibility: rho(c) = (implied_f * sd_c / sum_sigma)^2 is capped at 1, so a
    sigma below `f * sd_c / N` makes the cap bind (1.19 MW in MayOct, 1.80 MW in
    NovApr for the CATS buses). `sigma_floor_for_rho` computes that floor.

    Measured on those buses (doc_numbers.py section D): input-crosssec gives rho
    0.0157 and sigma/mu 2.45; pinned-rho 0.2310 and 0.59; scalar 2.5 MW 0.3710
    and 0.49; proportional-cv 0.32 gives 0.2555 and 0.32, the only variant that
    leaves no unit mostly-negative.
    """

    kind: str = "input-crosssec"
    value: float | pd.Series | None = None
    rho_target: float = 0.231

    def __post_init__(self) -> None:
        if self.kind not in SIGMA_SOURCES:
            raise ValueError(f"unknown sigma source {self.kind!r}; "
                             f"expected one of {SIGMA_SOURCES}")
        if self.kind in ("scalar", "proportional-cv") and self.value is None:
            raise ValueError(f"sigma source {self.kind!r} needs an explicit value "
                             f"(--sigma-mw or --sigma-cv)")


def fit_normal(loads: np.ndarray, percentiles, sigma_prior: np.ndarray | None = None
               ) -> tuple[np.ndarray, np.ndarray]:
    """Solve `L_k = mu + sigma * Phi^-1(p_k)` for mu and sigma.

    loads       [K, n] the K quantile values for each of n (unit, cell) rows
    percentiles [K] the percentiles those values are, in (0, 1)
    sigma_prior [n] required when K == 1, ignored otherwise

    Returns (mu, sigma), each [n]. NaN in `loads` propagates, which is how a
    half-missing envelope stays missing rather than becoming spurious.

    K == 2 uses the two-point difference form and is bit-for-bit the historical
    closed form at p = {0.10, 0.90}. K > 2 is ordinary least squares of L on
    [1, Phi^-1(p)] -- the Q-Q / probability-plot estimator. It ships lightly
    tested; weighting the fit by each quantile's precision (tighter near the
    median) is a TODO.
    """
    L = np.asarray(loads, dtype=float)
    if L.ndim == 1:
        L = L[None, :]
    p = np.asarray(percentiles, dtype=float)
    if p.ndim == 0:
        p = p[None]
    if len(p) != L.shape[0]:
        raise ValueError(f"{len(p)} percentiles for {L.shape[0]} load rows")
    if np.any((p <= 0) | (p >= 1)):
        raise ValueError(f"percentiles must lie in (0, 1); got {p}")
    z = _norm.ppf(p)

    if L.shape[0] == 1:
        if sigma_prior is None:
            raise ValueError(
                "a single percentile does not identify sigma; supply a sigma "
                "prior (see SigmaSource) -- at p=0.5 mu equals the input exactly")
        sigma = np.asarray(sigma_prior, dtype=float)
        mu = L[0] - sigma * z[0]
        return mu, sigma

    if L.shape[0] == 2:
        # exact two-point solution; bit-identical to (hi - lo) / (2 * Z90) and
        # (lo + hi) / 2 when p == (0.10, 0.90) because z[1] - z[0] == 2 * Z90
        sigma = (L[1] - L[0]) / (z[1] - z[0])
        mu = (L[0] + L[1]) / 2 if _is_symmetric(p) else L.mean(0) - sigma * z.mean()
        return mu, sigma

    zc = z - z.mean()
    sigma = (L * zc[:, None]).sum(0) / (zc ** 2).sum()
    mu = L.mean(0) - sigma * z.mean()
    return mu, sigma


def _is_symmetric(p: np.ndarray) -> bool:
    """True when the two percentiles straddle the median symmetrically, so
    `z.mean() == 0` and `mu` is the plain midpoint. Written as an explicit
    branch so the default path performs literally `(lo + hi) / 2`."""
    return len(p) == 2 and p[0] + p[1] == 1.0


def resolve_sigma(src: SigmaSource, mu: np.ndarray, cell: np.ndarray,
                  n_cells: int, sd_c: np.ndarray | None = None,
                  f: float = 1.0) -> np.ndarray:
    """Per-row sigma from a `SigmaSource`, given each row's mu and cell.

    `sd_c` (per-cell sd of the target series) and `f` are needed only by
    `pinned-rho`. Returns [n].
    """
    mu = np.asarray(mu, dtype=float)
    cell = np.asarray(cell, dtype=np.int64)

    if src.kind == "input-crosssec":
        per_cell = _cell_sd(mu, cell, n_cells)
        return per_cell[cell]

    if src.kind == "pinned-rho":
        if sd_c is None:
            raise ValueError("sigma source 'pinned-rho' needs the target's "
                             "per-cell sd (sd_c)")
        n_units = np.bincount(cell, minlength=n_cells).astype(float)
        fv = np.asarray(f, dtype=float)
        with np.errstate(invalid="ignore", divide="ignore"):
            per_cell = fv * np.asarray(sd_c, dtype=float) / (
                n_units * np.sqrt(src.rho_target))
        return per_cell[cell]

    if src.kind == "scalar":
        return np.full(mu.shape, float(src.value))

    # proportional-cv
    cv = src.value
    cv_row = (np.asarray(cv, dtype=float)[cell] if np.ndim(cv) else float(cv))
    return np.abs(cv_row * mu)


def _cell_sd(values: np.ndarray, cell: np.ndarray, n_cells: int) -> np.ndarray:
    """Sample sd of `values` within each cell, NaN-skipping, ddof=1."""
    ok = np.isfinite(values)
    c, v = cell[ok], values[ok]
    n = np.bincount(c, minlength=n_cells).astype(float)
    s1 = np.bincount(c, v, minlength=n_cells)
    s2 = np.bincount(c, v * v, minlength=n_cells)
    with np.errstate(invalid="ignore", divide="ignore"):
        var = (s2 - s1 ** 2 / n) / (n - 1)
    return np.sqrt(np.clip(var, 0, None))


def sigma_floor_for_rho(sd_c: np.ndarray, n_units: np.ndarray, f: float = 1.0
                        ) -> np.ndarray:
    """The smallest per-unit sigma that keeps rho(c) <= 1: `f * sd_c / N`.

    Below this the `min(1, .)` cap binds, which the spec reads as envelope/target
    inconsistency: even perfectly synchronized units could not reproduce the
    target's within-cell variability.
    """
    with np.errstate(invalid="ignore", divide="ignore"):
        return f * np.asarray(sd_c, dtype=float) / np.asarray(n_units, dtype=float)


def coarsen(env: pd.DataFrame, fine_to_coarse: np.ndarray,
            mode: str = "variance") -> pd.DataFrame:
    """Aggregate per-(unit, fine cell) mu/sigma onto coarser cells.

    mode 'variance' (default) applies the law of total variance,
    `sigma^2 = mean(sigma_fine^2) + var(mu_fine)`, so the coarse marginal keeps
    the full within-coarse-cell variability of the underlying load -- including
    the diurnal and seasonal swing that averaging would discard.

    mode 'average' takes `mean(sigma_fine)`. It is offered for comparison only
    and is NOT sound: on the California envelopes it drops the median half-year
    CV from 0.382 to 0.189, which inflates rho from a median 0.167 (max 0.209)
    to a median 0.688 (max 0.890) -- 4.2x, most of the way to the `min(1, .)`
    cap, though it does not actually bind on this data. `variance` keeps rho in
    the same range as the month-hour median of 0.2305. Measured by
    scripts/load_projection/approach2/doc_numbers.py sections B and C.

    `mu` is the plain mean either way: the fit is linear in the quantile values,
    so averaging mu over fine cells equals fitting the averaged envelope.
    """
    if mode not in ("variance", "average"):
        raise ValueError(f"unknown coarsen mode {mode!r}; "
                         f"expected 'variance' or 'average'")
    out = env.copy()
    out["cell"] = np.asarray(fine_to_coarse)[env["cell"].to_numpy()]
    g = out.groupby(["utility", "substation_name", "cell"], as_index=False, sort=True)
    agg = g.agg(mu=("mu", "mean"), sigma_sq=("sigma", lambda s: np.mean(np.square(s))),
                mu_var=("mu", lambda s: np.var(s, ddof=0)),
                sigma_avg=("sigma", "mean"),
                missing=("missing", "any"), inverted=("inverted", "any"))
    if mode == "variance":
        agg["sigma"] = np.sqrt(agg.sigma_sq + agg.mu_var)
    else:
        agg["sigma"] = agg.sigma_avg
    agg["zero_width"] = agg.sigma == 0
    return agg.drop(columns=["sigma_sq", "mu_var", "sigma_avg"])


def from_long(df: pd.DataFrame, spec, sigma_source: SigmaSource | None = None,
              sd_c: np.ndarray | None = None, ybar_c: np.ndarray | None = None,
              f: float = 1.0) -> pd.DataFrame:
    """Build the per-(unit, cell) parameter table from the tidy long contract.

    Expected columns: `unit_id`, `cell_label`, `percentile`, `load_mw`. Every
    (unit_id, cell_label) group must carry the same set of percentiles.

    Returns the same shape `stochastic.load_envelope_cells()` returns -- columns
    utility, substation_name, cell, mu, sigma, missing, inverted, zero_width --
    so the generator cannot tell the two input paths apart. `utility` is set to
    the literal "unit" and `substation_name` to `unit_id`, keeping the existing
    two-level key without inventing a utility attribution.
    """
    need = {"unit_id", "cell_label", "percentile", "load_mw"}
    if not need.issubset(df.columns):
        raise ValueError(f"long envelope needs columns {sorted(need)}; "
                         f"got {list(df.columns)}")

    labels = spec.labels
    if len(labels.columns) != 1:
        raise ValueError(f"cell spec {spec.name!r} has key columns "
                         f"{spec.key_cols}; the long contract addresses cells by "
                         f"a single cell_label, so use a spec with one key column "
                         f"(month, season3, halfyear or custom:<path>)")
    lut = pd.Series(labels.index.to_numpy(),
                    index=labels.iloc[:, 0].astype(str).to_numpy())
    unknown = sorted(set(df.cell_label.astype(str)) - set(lut.index))
    if unknown:
        raise ValueError(f"cell_label values {unknown} are not cells of spec "
                         f"{spec.name!r}; expected {list(lut.index)}")

    d = df.copy()
    d["cell"] = lut.reindex(d.cell_label.astype(str)).to_numpy()
    d = d.sort_values(["unit_id", "cell", "percentile"], kind="stable")

    counts = d.groupby(["unit_id", "cell"]).percentile.agg(["count", "nunique"])
    if counts["count"].nunique() != 1:
        raise ValueError("every (unit_id, cell_label) needs the same number of "
                         f"percentiles; found {sorted(counts['count'].unique())}")
    if (counts["count"] != counts["nunique"]).any():
        raise ValueError("duplicate percentiles within a (unit_id, cell_label)")
    k = int(counts["count"].iloc[0])

    keys = d[["unit_id", "cell"]].drop_duplicates().reset_index(drop=True)
    L = d.load_mw.to_numpy().reshape(len(keys), k).T   # [K, n]
    p = d.percentile.to_numpy().reshape(len(keys), k)[0]

    prior = None
    if k == 1:
        src = sigma_source or SigmaSource()
        cellv = keys.cell.to_numpy()
        # pinned-rho must hit rho_target exactly, and rho carries implied_f(c)
        # = sum(mu)/ybar as a factor. mu == load_mw at p=0.5, so implied_f is
        # known before sigma is; away from the median mu depends on sigma, so
        # this is one Newton step rather than an identity (documented, not exact)
        if src.kind == "pinned-rho" and ybar_c is not None:
            sum_mu = np.bincount(cellv, np.nan_to_num(L[0]), spec.n_cells)
            with np.errstate(invalid="ignore", divide="ignore"):
                f = sum_mu / np.asarray(ybar_c, dtype=float)
        # mu == load_mw at p=0.5; elsewhere mu depends on sigma, so seed sigma
        # from the raw loads and let proportional-cv solve its fixed point
        prior = resolve_sigma(src, L[0], cellv, spec.n_cells, sd_c=sd_c, f=f)
        if src.kind == "proportional-cv" and p[0] != 0.5:
            cv = np.abs(prior / np.where(L[0] == 0, np.nan, L[0]))
            denom = 1.0 + cv * _norm.ppf(p[0])
            if np.any(denom <= 0):
                raise ValueError("proportional-cv is infeasible at percentile "
                                 f"{p[0]}: 1 + cv * z <= 0 for some units")
            prior = np.abs(cv * (L[0] / denom))

    mu, sigma = fit_normal(L, p, sigma_prior=prior)

    out = pd.DataFrame({
        "utility": "unit",
        "substation_name": keys.unit_id.astype(str).to_numpy(),
        "cell": keys.cell.to_numpy(),
        "mu": mu,
        "sigma": np.abs(sigma),
    })
    out["missing"] = ~np.isfinite(out.mu) | ~np.isfinite(out.sigma)
    out["inverted"] = sigma < 0
    out["zero_width"] = out.sigma == 0
    return out
