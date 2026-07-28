"""Shared logic for the stochastic substation disaggregation model (Approach 2).

Implements docs/stochastic_model_spec.md: closed-form marginals per
(substation, month, hour_pst) cell, the calibrated IOU-share shape s(c) and
level F*, the per-cell common-factor share rho(c), z-trajectory construction
(native standardization or month-matched block bootstrap), and the conditional
Monte Carlo generator (normal marginals directly; uniform marginals via a
Gaussian copula). See the spec for derivations; scripts in
scripts/load_projection/ are the CLI entry points.
"""

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm as _norm

ROOT = Path(__file__).resolve().parents[2]
SUB_FILE = ROOT / "data/processed/substations/substation_load_profiles_clean.csv"
EIA_FILE = ROOT / "data/processed/eia/eia930_operations.csv"

Z90 = 1.2815515655446004  # Phi^-1(0.9)
N_CELLS = 288  # 12 months x 24 hours
YEAR_RANGE = (2015, 2025)  # complete PST years of EIA-930 used for estimation
FULL_YEAR_MIN_HOURS = 8000  # a year with fewer observed hours is "incomplete"


def cell_index(month, hour) -> np.ndarray:
    """Map (month 1-12, hour_pst 0-23) to a flat cell index 0-287."""
    return (np.asarray(month) - 1) * 24 + np.asarray(hour)


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------

def load_envelope_cells() -> pd.DataFrame:
    """Per-(utility, substation, month, hour_pst) envelope quantiles with
    closed-form marginal parameters and hygiene flags.

    Duplicate cells are resolved by the cell mean (matches rank_substations.py).
    Inverted cells (min > max) are swapped; zero-width cells flagged; cells with
    either quantile NaN are flagged `missing` (params NaN, excluded everywhere).
    """
    df = pd.read_csv(SUB_FILE)
    df = df.groupby(["utility", "substation_name", "month", "hour_pst"], as_index=False)[
        ["min_load", "max_load"]
    ].mean()
    df["missing"] = df.min_load.isna() | df.max_load.isna()
    df["inverted"] = df.min_load > df.max_load
    # np.minimum/maximum propagate NaN (unlike DataFrame.min(axis=1), which
    # would silently turn half-missing cells into zero-width ones)
    lo = np.minimum(df.min_load.values, df.max_load.values)
    hi = np.maximum(df.min_load.values, df.max_load.values)
    df["q10"], df["q90"] = lo, hi
    df["mu"] = (lo + hi) / 2
    df["sigma"] = (hi - lo) / (2 * Z90)
    width = (hi - lo) / 0.8
    df["unif_a"] = lo - width / 8
    df["unif_b"] = hi + width / 8
    df["zero_width"] = df.sigma == 0
    df["cell"] = cell_index(df.month, df.hour_pst)
    return df


def load_caiso_history() -> pd.DataFrame:
    """CAISO hourly net demand (EIA-930 CISO), UTC hour-ending -> PST
    hour-beginning, complete PST years 2015-2025, with cell labels."""
    eia = pd.read_csv(EIA_FILE, parse_dates=["datetime_utc"])
    c = eia[eia.ba_code == "CISO"].copy()
    c["dt_pst_hb"] = c.datetime_utc - pd.Timedelta(hours=9)
    y0, y1 = YEAR_RANGE
    c = c[(c.dt_pst_hb.dt.year >= y0) & (c.dt_pst_hb.dt.year <= y1)]
    c = c.dropna(subset=["demand_mwh"]).sort_values("dt_pst_hb")
    out = pd.DataFrame({
        "dt_pst_hb": c.dt_pst_hb.values,
        "demand_mw": c.demand_mwh.values,
    })
    out["month"] = out.dt_pst_hb.dt.month
    out["hour_pst"] = out.dt_pst_hb.dt.hour
    out["cell"] = cell_index(out.month, out.hour_pst)
    return out.reset_index(drop=True)


def complete_years(caiso: pd.DataFrame, min_hours: int = FULL_YEAR_MIN_HOURS) -> list[int]:
    """Calendar years in `caiso` with at least `min_hours` observed hours
    (i.e. not a partial stub; EIA-930's 2015 has only 4,417)."""
    hrs = caiso.groupby(caiso.dt_pst_hb.dt.year).size()
    return sorted(int(y) for y in hrs.index[hrs >= min_hours])


def trailing_window(caiso: pd.DataFrame, window: int | None,
                    min_hours: int = FULL_YEAR_MIN_HOURS) -> pd.DataFrame:
    """Restrict a CAISO history to its last `window` COMPLETE calendar years,
    for a recency-limited calibration (see docs/stochastic_model_spec.md,
    "Rolling-window calibration"). `window=None` returns `caiso` unchanged, so
    the default all-history behavior is byte-for-byte preserved. `window`
    larger than the number of complete years keeps all of them."""
    if not window:
        return caiso
    keep = complete_years(caiso, min_hours)[-window:]
    return caiso[caiso.dt_pst_hb.dt.year.isin(keep)].reset_index(drop=True)


def decay_weights(caiso: pd.DataFrame, halflife_days: float,
                  as_of: pd.Timestamp | None = None) -> np.ndarray:
    """Exponential recency weights `0.5**(age_days / halflife_days)` for each
    CAISO row, aged in CALENDAR time from `as_of` (default: the latest hour in
    `caiso`). Calendar age is the right staleness measure for a BTM-driven
    non-stationarity: an observation's weight reflects how much BTM has grown
    since it was recorded. Fed to `build_system_cells(..., weights=)`; per cell
    this down-weights older years' occurrences of that (month, hour) -- the
    smooth generalization of `trailing_window` (a rectangular kernel).
    halflife_days -> inf recovers the unweighted all-history calibration; a very
    short halflife collapses each cell onto its most recent same-month day."""
    if as_of is None:
        as_of = caiso["dt_pst_hb"].max()
    age_days = (as_of - caiso["dt_pst_hb"]).dt.total_seconds().to_numpy() / 86400.0
    return 0.5 ** (age_days / float(halflife_days))


def _cell_moments(caiso: pd.DataFrame, weights: np.ndarray | None) -> pd.DataFrame:
    """Per-cell ybar, sd, n_obs. `weights=None` is the plain unweighted groupby
    (byte-for-byte the original path); otherwise weighted moments with an
    effective sample size n_eff = (sum w)^2 / sum(w^2) and its Bessel correction."""
    if weights is None:
        return caiso.groupby("cell")["demand_mw"].agg(ybar="mean", sd="std", n_obs="count")
    c = caiso["cell"].to_numpy()
    y = caiso["demand_mw"].to_numpy()
    w = np.asarray(weights, dtype=float)
    sw = np.bincount(c, w, N_CELLS)
    swy = np.bincount(c, w * y, N_CELLS)
    swy2 = np.bincount(c, w * y * y, N_CELLS)
    sw2 = np.bincount(c, w * w, N_CELLS)
    with np.errstate(invalid="ignore", divide="ignore"):
        ybar = swy / sw
        var_pop = swy2 / sw - ybar ** 2
        neff = sw ** 2 / sw2
        sd = np.sqrt(np.clip(var_pop, 0, None) * np.where(neff > 1, neff / (neff - 1), np.nan))
    return pd.DataFrame({"ybar": ybar, "sd": sd, "n_obs": neff},
                        index=pd.RangeIndex(N_CELLS, name="cell"))


def build_system_cells(env: pd.DataFrame, caiso: pd.DataFrame,
                       weights: np.ndarray | None = None) -> tuple[pd.DataFrame, float]:
    """Per-cell system table and the calibrated level F*.

    Columns: sum_mu, sum_sigma (envelope aggregates); ybar, sd, n_obs (CAISO
    within-cell stats); implied_f = sum_mu/ybar; shape_s = implied_f/F*;
    rho = min(1, (implied_f * sd / sum_sigma)^2)  [F-invariant, see spec].

    F* is the energy-weighted mean of implied_f: total envelope-implied energy
    over total CAISO energy, so F* = the calibrated annual IOU share.

    `weights` (optional, one per CAISO row -- e.g. from `decay_weights`) makes
    ybar, sd, n_obs, and hence f(c)/s(c)/rho(c) recency-weighted; `None` is the
    unweighted all-history default (see docs/stochastic_model_spec.md,
    "Rolling-window calibration").
    """
    agg = env.groupby("cell").agg(sum_mu=("mu", "sum"), sum_sigma=("sigma", "sum"))
    cy = _cell_moments(caiso, weights)
    cells = agg.join(cy)
    cells["implied_f"] = cells.sum_mu / cells.ybar
    f_star = (cells.sum_mu * cells.n_obs).sum() / (cells.ybar * cells.n_obs).sum()
    cells["shape_s"] = cells.implied_f / f_star
    # min(1,.) projects the unbounded moment ratio onto rho's [0,1] parameter
    # space; a binding cap would signal envelope/CAISO inconsistency (see spec)
    cells["rho"] = np.minimum(1.0, (cells.implied_f * cells.sd / cells.sum_sigma) ** 2)
    cells["month"] = cells.index // 24 + 1
    cells["hour_pst"] = cells.index % 24
    return cells, f_star


def standardize_z(target: pd.DataFrame) -> pd.DataFrame:
    """Native z-mode: standardize a target hourly series within its own cells.

    target needs columns dt_pst_hb, demand_mw, cell. Returns a copy with `z`.
    """
    t = target.copy()
    g = t.groupby("cell")["demand_mw"]
    t["z"] = (t.demand_mw - g.transform("mean")) / g.transform("std")
    return t


def bootstrap_z(z_hist: pd.DataFrame, target: pd.DataFrame, block_days: int,
                rng: np.random.Generator) -> np.ndarray:
    """Bootstrap z-mode: month-matched block resampling of historical z.

    Historical z (columns dt_pst_hb, z) is reshaped into complete days; the
    target hours are covered in consecutive blocks of `block_days`, each filled
    with a historical block whose start day falls in the same calendar month.
    Preserves diurnal alignment and multi-day (heat wave) persistence.
    """
    h = z_hist.copy()
    h["date"] = h.dt_pst_hb.dt.date
    complete = h.groupby("date")["z"].transform("count") == 24
    h = h[complete].sort_values("dt_pst_hb")
    day_mat = h.z.values.reshape(-1, 24)  # [n_days, 24]
    day_dates = pd.to_datetime(h.dt_pst_hb.dt.date.unique())
    # valid block starts by month: block must fit inside the historical record
    n_days = len(day_dates)
    starts_by_month = {
        m: np.flatnonzero((day_dates.month == m) & (np.arange(n_days) <= n_days - block_days))
        for m in range(1, 13)
    }

    t = target.sort_values("dt_pst_hb").reset_index(drop=True)
    t_dates = pd.to_datetime(t.dt_pst_hb.dt.date)
    unique_days, day_of_target = np.unique(t_dates, return_inverse=True)
    unique_days = pd.to_datetime(unique_days)
    n_target_days = len(unique_days)

    z_by_day = np.empty((n_target_days, 24))
    d = 0
    while d < n_target_days:
        m = unique_days[d].month
        start = rng.choice(starts_by_month[m])
        span = min(block_days, n_target_days - d)
        z_by_day[d:d + span] = day_mat[start:start + span]
        d += span
    return z_by_day[day_of_target, t.dt_pst_hb.dt.hour.values]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

class EnvelopeMatrices:
    """Envelope parameters pivoted to [n_subs, 288] arrays (NaN = missing cell)."""

    def __init__(self, env: pd.DataFrame):
        subs = env[["utility", "substation_name"]].drop_duplicates().sort_values(
            ["utility", "substation_name"]).reset_index(drop=True)
        self.subs = subs
        idx = pd.MultiIndex.from_frame(subs)

        def pivot(col: str) -> np.ndarray:
            wide = env.pivot_table(index=["utility", "substation_name"],
                                   columns="cell", values=col, dropna=False)
            wide = wide.reindex(index=idx, columns=range(N_CELLS))
            return wide.values

        self.mu = pivot("mu")
        self.sigma = pivot("sigma")
        self.unif_a = pivot("unif_a")
        self.unif_b = pivot("unif_b")


def generate(mats: EnvelopeMatrices, cells: pd.DataFrame, target: pd.DataFrame,
             z: np.ndarray, family: str, scale: float,
             rng: np.random.Generator, eps_mode: str = "daily") -> np.ndarray:
    """One Monte Carlo draw of all substations over the target hours.

    Returns [n_hours, n_subs] float32 (NaN where a substation has no cell).
    `scale` = F / F* multiplies the output (level sensitivity; rho unchanged).
    eps_mode 'daily' = one idiosyncratic draw per substation-day (spec default);
    'hourly' = i.i.d. per hour (sensitivity case).
    """
    k = target.cell.values
    n_hours, n_subs = len(k), len(mats.subs)
    rho = cells.rho.reindex(range(N_CELLS)).values[k]        # [H]
    w_common = (np.sqrt(rho) * z)[:, None]                    # [H,1]
    w_idio_coef = np.sqrt(1.0 - rho)[:, None]                 # [H,1]

    if eps_mode == "daily":
        day_ids = pd.factorize(target.dt_pst_hb.dt.date)[0]
        eps_day = rng.standard_normal((day_ids.max() + 1, n_subs))
        eps = eps_day[day_ids]                                # [H,S]
    elif eps_mode == "hourly":
        eps = rng.standard_normal((n_hours, n_subs))
    else:
        raise ValueError(f"unknown eps_mode {eps_mode!r}")

    w = w_common + w_idio_coef * eps                          # [H,S] std normal
    if family == "normal":
        out = mats.mu[:, k].T + mats.sigma[:, k].T * w
    elif family == "uniform":
        u = _norm.cdf(w)
        a, b = mats.unif_a[:, k].T, mats.unif_b[:, k].T
        out = a + (b - a) * u
    else:
        raise ValueError(f"unknown family {family!r}")
    return (scale * out).astype(np.float32)
