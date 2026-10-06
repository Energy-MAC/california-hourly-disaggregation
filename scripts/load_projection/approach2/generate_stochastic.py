"""Monte Carlo generation of substation hourly loads (Approach 2 - stochastic).

Disaggregates a CAISO-total hourly series into per-substation stochastic draws
per docs/stochastic_model_spec.md: expected total = F * s(c) * y(t), substation
marginals preserved, common factor from the target's z-trajectory, one
idiosyncratic draw per substation-day. See README for methodology.

CLI parameters:
  --target      "eia930" (historical CAISO, default) or path to a CSV with
                columns dt_pst_hb, demand_mw holding a CAISO-total series
  --family      normal | uniform | both (default both)
  --F           level: "cal" (calibrated F*, default) or a float e.g. 0.80
  --z-mode      native (standardize target within its own cells; observed
                trajectory shared by all draws, default) |
                bootstrap (month-matched 7-day blocks of historical z,
                redrawn independently per draw so weather varies across
                the ensemble)
  --block-days  bootstrap block length in days (default 7)
  --calibration-window  calibrate F*/s(c)/rho(c) on only the last N complete
                CAISO years instead of all history (default: all). Reduces the
                level-drift bias when the target's period differs from the full
                record (see README + docs/stochastic_model_spec.md); appends
                __cw{N} to the run tag so windowed runs never overwrite the
                all-history ones.
  --decay-halflife  recency-weight the calibration with an exponential kernel of
                this half-life in calendar days (smooth alternative to the hard
                window; composes with it). Captures BTM-driven shape drift;
                appends __hl{N} to the run tag.
  --calibrate-on  history (default, EIA-930) | target (calibrate F*/s(c)/rho(c)
                on the --target series itself); appends __calibtgt to the run tag
  --calib-target  with --calibrate-on target: calibrate on THIS series rather than
                --target, so a longer record can sharpen F*/s(c)/rho(c) while only
                the weeks of interest are disaggregated
  --n-draws     Monte Carlo draws (default 5)
  --year-start/--year-end   subset of target years (default all)
  --seed        RNG seed (default 0)
  --validate    run the three spec validation checks (totals, marginals, tracking)
  --save-output write hourly wide parquet per draw (~47 MB/draw-year, off by default)
  --save-cells  write per-(substation, draw, cell) mean MW over the target
                period -- the per-cell weight table the GenX rescaler's
                stochastic month-hour allocation consumes (off by default)
  --cells       cell definition: monthhour (default, 288 cells -- the published
                behavior, bit-for-bit), monthdayhour, month, season3, halfyear,
                or custom:<path>. Appends __cells{name} to the run tag when it is
                not the default, so existing run tags never change.
  --coarsen     how the (month, hour_pst) utility envelopes aggregate onto a
                coarser --cells: variance (default; law of total variance, keeps
                the diurnal swing) or average (drops it, inflates rho ~4x toward
                the cap -- comparison only)
  --envelope    a tidy long envelope to disaggregate onto INSTEAD of the CA
                substation profiles: columns unit_id, cell_label, percentile,
                load_mw. Lets a set of nodes carrying one value per season enter
                the model. Needs a --cells spec with one key column. Check a file
                first with validate_long_envelope.py
  --sigma-source  with a single-percentile --envelope, where sigma comes from:
                input-crosssec (default) = sd across units of the input loads in
                that cell, identical for every unit | pinned-rho = the sigma that
                makes rho(c) hit --rho-target by construction | scalar = a flat
                --sigma-mw | proportional-cv = --sigma-cv * mu, so sigma scales
                with unit size. rho(c) and P(L<0) are printed either way
  --sigma-mw / --sigma-cv / --rho-target   parameters of the above

Outputs (data/processed/load_projection/projections/<run_tag>/ where run_tag =
stochastic__{target}__{family}__F{level}__{z-mode}[__cw{N}]):
  substation_annual_mwh.csv       always: per (substation, year, draw) energy
  substation_cell_mw.csv          with --save-cells: per (substation, draw, cell)
                                  mean MW + n_hours over the whole target period;
                                  the cell is spelled with --cells' own key
                                  columns (month, hour_pst by default)
  validation_totals_cells.csv     with --validate: per-cell total q10/q90 check
  validation_marginals_subs.csv   with --validate: per-substation envelope recovery
  draws/draw{k}.parquet           with --save-output: hourly wide matrix per draw

Only --family normal supports a generalized envelope; the uniform family is
frozen at the legacy settings (two percentiles on monthhour cells) and the run
aborts otherwise -- generalizing it is a TODO (see src/load_projection/envelopes.py).

Usage:
  python scripts/load_projection/approach2/generate_stochastic.py --validate
  python scripts/load_projection/approach2/generate_stochastic.py --family normal --F 0.80 --n-draws 20
  python scripts/load_projection/approach2/generate_stochastic.py --target forecast.csv --z-mode bootstrap --save-output
  python scripts/load_projection/approach2/generate_stochastic.py --family normal       --cells halfyear --envelope nodes.csv --sigma-source pinned-rho --save-cells
"""

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from load_projection import cells as cellspecs  # noqa: E402
from load_projection import envelopes as envlib  # noqa: E402
from scipy.stats import norm as _snorm  # noqa: E402

from load_projection.stochastic import (  # noqa: E402
    _cell_moments,
    EnvelopeMatrices,
    bootstrap_z,
    build_system_cells,
    cell_index,
    decay_weights,
    generate,
    load_caiso_history,
    load_envelope_cells,
    standardize_z,
    trailing_window,
)

PROJ_DIR = ROOT / "data/processed/load_projection/projections"


def _label_series(t: pd.DataFrame, spec) -> pd.DataFrame:
    """Derive month/day/hour_pst and the cell index for a target-like frame."""
    t["month"] = t.dt_pst_hb.dt.month
    t["day"] = t.dt_pst_hb.dt.day
    t["hour_pst"] = t.dt_pst_hb.dt.hour
    t["cell"] = cellspecs.encode(spec, t.month, t.hour_pst, t.day)
    return t


def load_target(args, caiso: pd.DataFrame, spec) -> pd.DataFrame:
    if args.target == "eia930":
        t = caiso.copy()
    else:
        t = _label_series(pd.read_csv(args.target, parse_dates=["dt_pst_hb"]), spec)
    if args.year_start:
        t = t[t.dt_pst_hb.dt.year >= args.year_start]
    if args.year_end:
        t = t[t.dt_pst_hb.dt.year <= args.year_end]
    return t.sort_values("dt_pst_hb").reset_index(drop=True)


def trajectory_pass(mats, cells, target, z_draws, family, scale, n_draws, seed,
                    out_dir, save_output, save_cells=False):
    """Per-draw generation chunked by year. Returns (annual_df, totals [H, D],
    cell_df) where cell_df is the per-(substation, draw, cell) mean-MW table
    (None unless save_cells).

    z_draws is one z array per draw: identical in native mode (observed
    trajectory shared, draws differ only in allocation), independently
    bootstrapped per draw in bootstrap mode (weather varies across ensemble).
    """
    years = target.dt_pst_hb.dt.year.values
    n_subs = len(mats.subs)
    totals = np.empty((len(target), n_draws), dtype=np.float64)
    annual_rows, cell_rows = [], []
    if save_output:
        (out_dir / "draws").mkdir(parents=True, exist_ok=True)
    for d in range(n_draws):
        rng = np.random.default_rng(seed + 1000 * d)
        draw_chunks = []
        # per-cell accumulators over the WHOLE target period (not per year):
        # the cell mean is the weight the GenX rescaler wants, and a (month,
        # hour) cell is a within-year concept, so years pool
        cellsum = np.zeros((mats.spec.n_cells, n_subs))
        cellcnt = np.zeros((mats.spec.n_cells, n_subs), dtype=np.int64)
        for yr in np.unique(years):
            mask = years == yr
            chunk = target[mask]
            L = generate(mats, cells, chunk, z_draws[d][mask], family, scale, rng)
            totals[mask, d] = np.nansum(L, axis=1)
            annual = pd.DataFrame({
                "utility": mats.subs.utility, "substation_name": mats.subs.substation_name,
                "year": yr, "draw": d, "annual_mwh": np.nansum(L, axis=0),
            })
            annual_rows.append(annual)
            if save_cells:
                k = chunk.cell.values
                np.add.at(cellsum, k, np.nan_to_num(L, nan=0.0))
                np.add.at(cellcnt, k, (~np.isnan(L)).astype(np.int64))
            if save_output:
                draw_chunks.append(pd.DataFrame(
                    L, index=chunk.dt_pst_hb,
                    columns=[f"{u}|{n}" for u, n in mats.subs.itertuples(index=False)]))
        if save_cells:
            has = cellcnt > 0
            cell_i, sub_i = np.nonzero(has)
            block = pd.DataFrame({
                "utility": mats.subs.utility.values[sub_i],
                "substation_name": mats.subs.substation_name.values[sub_i],
                "draw": d,
            })
            lbl = cellspecs.label_frame(mats.spec)
            for col in lbl.columns:
                block[col] = lbl[col].to_numpy()[cell_i]
            block["mean_mw"] = cellsum[has] / cellcnt[has]
            block["n_hours"] = cellcnt[has]
            cell_rows.append(block)
        if save_output:
            pd.concat(draw_chunks).to_parquet(out_dir / "draws" / f"draw{d}.parquet")
    cell_df = pd.concat(cell_rows, ignore_index=True) if cell_rows else None
    return pd.concat(annual_rows, ignore_index=True), totals, cell_df


def validate_totals(cells, target, totals, family, F_level, n_cells):
    """Check (i)+(iii): per-cell q10/q90 of simulated totals vs the target
    F*s(c)*y distribution, and hourly tracking error of the draw-mean total."""
    fy = F_level * cells.shape_s.reindex(range(n_cells)).values[target.cell.values] \
        * target.demand_mw.values
    df = pd.DataFrame({"cell": target.cell.values, "fy": fy})
    tgt = df.groupby("cell")["fy"].agg(tgt_q10=lambda s: s.quantile(0.1),
                                       tgt_q90=lambda s: s.quantile(0.9))
    sim = pd.DataFrame({"cell": np.repeat(target.cell.values, totals.shape[1]),
                        "tot": totals.ravel()})
    simq = sim.groupby("cell")["tot"].agg(sim_q10=lambda s: s.quantile(0.1),
                                          sim_q90=lambda s: s.quantile(0.9))
    out = tgt.join(simq)
    out["err_q10_pct"] = 100 * (out.sim_q10 - out.tgt_q10) / out.tgt_q10
    out["err_q90_pct"] = 100 * (out.sim_q90 - out.tgt_q90) / out.tgt_q90
    track_rel = (totals.mean(axis=1) - fy) / fy
    print(f"\n[{family}] check (i) totals per cell: |q10 err| median "
          f"{out.err_q10_pct.abs().median():.2f}% max {out.err_q10_pct.abs().max():.2f}%; "
          f"|q90 err| median {out.err_q90_pct.abs().median():.2f}% "
          f"max {out.err_q90_pct.abs().max():.2f}%")
    print(f"[{family}] check (iii) hourly tracking (mean of draws vs F*s(c)*y): "
          f"relRMSE {np.sqrt((track_rel ** 2).mean()) * 100:.2f}%, "
          f"bias {track_rel.mean() * 100:+.3f}%")
    out["family"] = family
    return out.reset_index()


def marginal_pass(mats, env, cells, target, z_draws, family, scale, n_draws, seed):
    """Check (ii): per-cell envelope recovery. Generated cell-by-cell so exact
    empirical q10/q90 over (hours-in-cell x draws) samples fit in memory.
    Within one cell each sample is a distinct day, so daily idiosyncratic
    draws are equivalent to i.i.d. draws here."""
    rng = np.random.default_rng(seed + 777)
    rows = []
    kvec = target.cell.values
    for c in range(mats.spec.n_cells):
        rho_c = cells.rho.get(c, np.nan)
        zc = np.concatenate([zd[kvec == c] for zd in z_draws])
        if len(zc) == 0 or np.isnan(rho_c):
            continue
        w = (np.sqrt(rho_c) * zc[:, None]
             + np.sqrt(1 - rho_c) * rng.standard_normal((len(zc), len(mats.subs))))
        if family == "normal":
            L = mats.mu[:, c] + mats.sigma[:, c] * w
        else:
            from scipy.stats import norm as _norm
            L = mats.unif_a[:, c] + (mats.unif_b[:, c] - mats.unif_a[:, c]) * _norm.cdf(w)
        L *= scale
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN dataless subs
            q10, q90 = np.nanquantile(L, [0.1, 0.9], axis=0)
        rows.append(pd.DataFrame({
            "utility": mats.subs.utility, "substation_name": mats.subs.substation_name,
            "cell": c, "sim_q10": q10, "sim_q90": q90}))
    sim = pd.concat(rows, ignore_index=True)
    if "q10" not in env.columns:
        # check (ii) scores recovered q10/q90 against the INPUT quantiles; a
        # generalized envelope (coarsened, or a single percentile) has none to
        # compare against, so the check is undefined rather than failing
        print(f"[{family}] check (ii) skipped: the envelope carries no q10/q90 "
              f"to recover (generalized input)")
        return None
    chk = env.merge(sim, on=["utility", "substation_name", "cell"], how="inner")
    chk = chk[~chk.missing & ~chk.zero_width]
    width = (chk.q90 - chk.q10) * scale
    e10 = (chk.sim_q10 - chk.q10 * scale) / width
    e90 = (chk.sim_q90 - chk.q90 * scale) / width
    print(f"[{family}] check (ii) envelope recovery over {len(chk):,} sub-cells "
          f"(width-normalized): |q10 err| median {e10.abs().median() * 100:.2f}% "
          f"p95 {e10.abs().quantile(0.95) * 100:.2f}%; "
          f"|q90 err| median {e90.abs().median() * 100:.2f}% "
          f"p95 {e90.abs().quantile(0.95) * 100:.2f}%")
    per_sub = (pd.DataFrame({"utility": chk.utility, "substation_name": chk.substation_name,
                             "abs_err10": e10.abs(), "abs_err90": e90.abs()})
               .groupby(["utility", "substation_name"]).median().reset_index())
    per_sub["family"] = family
    return per_sub


def annualized_mean_twh(annual: pd.DataFrame, target: pd.DataFrame) -> float:
    """Mean simulated total (TWh/yr) across draws, annualized over COMPLETE
    calendar years only. Dividing summed energy by the raw count of distinct
    years understates the rate when the target has a partial year (EIA-930
    starts mid-2015 with 4,417 hours; that stub is also summer-skewed, so
    hours-normalizing it in would instead over-state the rate). Years with
    >= 8000 observed hours are treated as complete; if none are, fall back to
    hours-normalization so the number is still a sane annual rate."""
    hrs_by_year = target.groupby(target.dt_pst_hb.dt.year).size()
    full_years = hrs_by_year.index[hrs_by_year >= 8000]
    per_draw = annual.groupby("draw").annual_mwh.sum()  # fallback: full period
    if len(full_years):
        per_draw = annual[annual.year.isin(full_years)].groupby("draw").annual_mwh.sum()
        n_year_equiv = len(full_years)
    else:
        n_year_equiv = len(target) / 8760
    return per_draw.mean() / 1e6 / n_year_equiv


def load_envelope(args, spec, calib, weights):
    """The per-(unit, cell) marginal table, from either input path.

    Default: the CA substation percentile envelopes, fitted at (month, hour_pst)
    and aggregated onto `spec`. With --envelope: the tidy long contract, whose
    sigma comes from --sigma-source when it carries a single percentile.
    """
    if not args.envelope:
        return load_envelope_cells(spec, coarsen_mode=args.coarsen)
    value = (args.sigma_mw if args.sigma_source == "scalar"
             else args.sigma_cv if args.sigma_source == "proportional-cv" else None)
    src = envlib.SigmaSource(kind=args.sigma_source, value=value,
                             rho_target=args.rho_target)
    cy = _cell_moments(calib, weights, spec.n_cells)
    sd_c = cy.sd.reindex(range(spec.n_cells)).to_numpy()
    ybar_c = cy.ybar.reindex(range(spec.n_cells)).to_numpy()
    df = pd.read_csv(args.envelope)
    env = envlib.from_long(df, spec, sigma_source=src, sd_c=sd_c, ybar_c=ybar_c)
    print(f"envelope: {args.envelope} -> {env.substation_name.nunique():,} units "
          f"x {env.cell.nunique()} cells, sigma from {args.sigma_source}")
    return env


def report_marginal_diagnostics(env, cells, spec) -> None:
    """Print the numbers that reveal a degenerate sigma choice.

    A sigma taken from the CROSS-SECTIONAL spread of the input loads measures
    inter-unit inequality, which mu already carries; used as sigma it becomes
    hour-to-hour noise, so rho collapses and small units spend much of their time
    negative. Both effects are reported here rather than left silent. The floor
    is the smallest sigma that keeps rho <= 1.
    """
    capped = int((cells.rho >= 1.0).sum())
    print(f"cells: {spec.n_cells} ({spec.name})   rho median "
          f"{cells.rho.median():.4f} range {cells.rho.min():.4f}-"
          f"{cells.rho.max():.4f}, capped {capped}")
    ok = (env.sigma > 0) & np.isfinite(env.mu) & np.isfinite(env.sigma)
    if ok.any():
        ratio = (env.sigma[ok] / env.mu[ok]).replace([np.inf, -np.inf], np.nan)
        pneg = _snorm.cdf(-(env.mu[ok].to_numpy() / env.sigma[ok].to_numpy()))
        print(f"marginals: sigma/mu median {ratio.median():.3f}   P(L<0) median "
              f"{np.median(pneg) * 100:.1f}%, units with P(L<0)>10%: "
              f"{np.mean(pneg > 0.10) * 100:.1f}%")
    n_units = env.groupby("cell").mu.size().reindex(range(spec.n_cells)).to_numpy()
    floor = envlib.sigma_floor_for_rho(cells.sd.to_numpy(), n_units,
                                      f=cells.implied_f.to_numpy())
    print(f"sigma floor for rho<=1 (f*sd_c/N): median "
          f"{np.nanmedian(floor):.3f} MW")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", default="eia930")
    ap.add_argument("--family", choices=["normal", "uniform", "both"], default="both")
    ap.add_argument("--F", default="cal")
    ap.add_argument("--z-mode", choices=["native", "bootstrap"], default="native")
    ap.add_argument("--block-days", type=int, default=7)
    ap.add_argument("--calibration-window", type=int, default=None,
                    help="calibrate F*/s(c)/rho(c) on only the last N complete "
                         "CAISO years (default: all history). See README "
                         "'Rolling-origin calibration CV' and the model spec.")
    ap.add_argument("--decay-halflife", type=float, default=None,
                    help="recency-weight the calibration with an exponential "
                         "kernel of this half-life in CALENDAR DAYS (smooth "
                         "alternative to --calibration-window; composes with it "
                         "if both given). Default: unweighted. Appends __hl{N}.")
    ap.add_argument("--calib-target", default=None,
                    help="with --calibrate-on target: calibrate F*/s(c)/rho(c) on THIS "
                         "series instead of --target, so a longer record can inform the "
                         "calibration while only the weeks of interest are disaggregated")
    ap.add_argument("--calibrate-on", choices=["history", "target"], default="history",
                    help="which series F*, s(c) and rho(c) are estimated on. "
                         "'history' = EIA-930 CAISO (default, preserves published "
                         "F*=0.7361); 'target' = the --target series itself, so the "
                         "calibration belongs to the load actually being disaggregated")
    ap.add_argument("--n-draws", type=int, default=5)
    ap.add_argument("--year-start", type=int, default=None)
    ap.add_argument("--year-end", type=int, default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--save-output", action="store_true")
    ap.add_argument("--save-cells", action="store_true",
                    help="write substation_cell_mw.csv: per (substation, draw, "
                         "cell) mean MW over the target period, for the GenX "
                         "rescaler's per-cell stochastic weights")
    ap.add_argument("--cells", default="monthhour",
                    help="cell definition: monthhour (default, 288 cells, the "
                         "published behavior), monthdayhour, month, season3, "
                         "halfyear, or custom:<path>. See src/load_projection/cells.py")
    ap.add_argument("--coarsen", choices=["variance", "average"], default="variance",
                    help="how the (month, hour_pst) utility envelopes aggregate "
                         "onto a coarser --cells: 'variance' applies the law of "
                         "total variance and keeps the diurnal swing (default); "
                         "'average' drops it and inflates rho (comparison only)")
    ap.add_argument("--envelope", default=None,
                    help="path to a tidy long envelope (unit_id, cell_label, "
                         "percentile, load_mw) to disaggregate onto instead of "
                         "the CA substation profiles. Requires a --cells spec "
                         "with a single key column (month/season3/halfyear/custom)")
    ap.add_argument("--sigma-source", choices=list(envlib.SIGMA_SOURCES),
                    default="input-crosssec",
                    help="with a single-percentile --envelope, where sigma comes "
                         "from. input-crosssec (default) = sd across units of the "
                         "input loads in that cell, identical for every unit")
    ap.add_argument("--sigma-mw", type=float, default=None,
                    help="with --sigma-source scalar: the flat per-unit sigma in MW")
    ap.add_argument("--sigma-cv", type=float, default=None,
                    help="with --sigma-source proportional-cv: sigma = cv * mu")
    ap.add_argument("--rho-target", type=float, default=0.231,
                    help="with --sigma-source pinned-rho: the rho(c) to hit by "
                         "construction (default 0.231, the CA month-hour median)")
    args = ap.parse_args()
    spec = cellspecs.get_spec(args.cells)
    legacy_cells = spec.name == cellspecs.MONTHHOUR.name
    if args.family in ("uniform", "both") and not (legacy_cells and not args.envelope):
        ap.error("the uniform family is not implemented for generalized "
                 "envelopes (TODO): it requires --cells monthhour and the "
                 "two-percentile CA substation envelopes. Use --family normal.")
    if args.envelope and len(spec.key_cols) != 1:
        ap.error(f"--envelope addresses cells by a single cell_label, but "
                 f"--cells {args.cells} has key columns {spec.key_cols}; "
                 f"use month, season3, halfyear or custom:<path>")
    if args.calib_target and args.calibrate_on != "target":
        ap.error("--calib-target only makes sense with --calibrate-on target; "
                 "without it the calibration series would silently stay EIA-930")

    caiso = load_caiso_history(spec)
    target_pre = load_target(args, caiso, spec)
    # F*, s(c) and rho(c) are calibrated on `calib`. Default is the EIA-930
    # history, which makes F* a statement about how the fleet relates to the
    # PAST. --calibrate-on target instead calibrates against the series being
    # disaggregated, so nothing is inherited from another dataset except the
    # substations' own envelopes (see docs/genx_rescale.md, "Recalibrating F*").
    if args.calibrate_on == "target":
        # the calibration series may be LONGER than the series being
        # disaggregated: F*, s(c) and rho(c) want as many observations per cell
        # as exist, while the CEP analysis only ever looks at the chosen weeks.
        # --calib-target supplies that longer series; it defaults to --target.
        if args.calib_target:
            calib_src = _label_series(
                pd.read_csv(args.calib_target, parse_dates=["dt_pst_hb"]), spec)
        else:
            calib_src = target_pre
    else:
        calib_src = caiso
    calib = trailing_window(calib_src, args.calibration_window)
    weights = decay_weights(calib, args.decay_halflife) if args.decay_halflife else None
    # the envelope is built AFTER calib because the pinned-rho sigma prior needs
    # the calibration series' per-cell sd; nothing else depends on the order
    env = load_envelope(args, spec, calib, weights)
    cells, f_star = build_system_cells(env, calib, weights, spec)
    mats = EnvelopeMatrices(env, spec)
    target = target_pre
    if args.envelope or not legacy_cells:
        report_marginal_diagnostics(env, cells, spec)

    F_level = f_star if args.F == "cal" else float(args.F)
    scale = F_level / f_star
    if args.z_mode == "native":
        target = standardize_z(target)
        z_draws = [target.z.values] * args.n_draws  # shared observed trajectory
    else:
        # independent weather trajectory per draw: the ensemble varies weather
        zhist = standardize_z(caiso)[["dt_pst_hb", "z"]]
        z_draws = [bootstrap_z(zhist, target, args.block_days,
                               np.random.default_rng(args.seed + 555 + d))
                   for d in range(args.n_draws)]

    tname = "eia930" if args.target == "eia930" else Path(args.target).stem
    f_tag = "cal" if args.F == "cal" else f"{F_level:.2f}"
    cw_tag = f"__cw{args.calibration_window}" if args.calibration_window else ""
    hl_tag = f"__hl{int(args.decay_halflife)}" if args.decay_halflife else ""
    ct_tag = "__calibtgt" if args.calibrate_on == "target" else ""
    # only a non-default cell definition touches the run tag, so every existing
    # run tag is unchanged
    cell_tag = "" if legacy_cells else f"__cells{spec.name.replace(':', '-')}"
    families = ["normal", "uniform"] if args.family == "both" else [args.family]

    calib_desc = (f"last {args.calibration_window} complete yrs "
                  f"({calib.dt_pst_hb.dt.year.min()}-{calib.dt_pst_hb.dt.year.max()})"
                  if args.calibration_window else "all history")
    if args.decay_halflife:
        calib_desc += f", decay half-life {args.decay_halflife:g} d (median n_eff {cells.n_obs.median():.0f})"
    print(f"target: {tname} {target.dt_pst_hb.dt.year.min()}-"
          f"{target.dt_pst_hb.dt.year.max()} ({len(target):,} hours)   "
          f"F = {F_level:.4f} (scale {scale:.3f})   z-mode: {args.z_mode}   "
          f"draws: {args.n_draws}   calibration: {calib_desc}")

    for family in families:
        run_tag = (f"stochastic__{tname}__{family}__F{f_tag}__{args.z_mode}"
                   f"{cw_tag}{hl_tag}{ct_tag}{cell_tag}")
        out_dir = PROJ_DIR / run_tag
        out_dir.mkdir(parents=True, exist_ok=True)
        annual, totals, cell_df = trajectory_pass(
            mats, cells, target, z_draws, family, scale, args.n_draws,
            args.seed, out_dir, args.save_output, args.save_cells)
        annual.to_csv(out_dir / "substation_annual_mwh.csv", index=False)
        if cell_df is not None:
            cell_df.round(4).to_csv(out_dir / "substation_cell_mw.csv", index=False)
            print(f"[{family}] wrote substation_cell_mw.csv: {len(cell_df):,} rows, "
                  f"{cell_df.groupby(list(spec.key_cols)).ngroups} cells, "
                  f"{cell_df.draw.nunique()} draws")
        mean_twh = annualized_mean_twh(annual, target)
        print(f"\n[{family}] -> {run_tag}: mean {mean_twh:.1f} TWh/yr across draws")
        if args.validate:
            vt = validate_totals(cells, target, totals, family, F_level,
                                 spec.n_cells)
            vt.round(4).to_csv(out_dir / "validation_totals_cells.csv", index=False)
            vm = marginal_pass(mats, env, cells, target, z_draws, family, scale,
                               args.n_draws, args.seed)
            if vm is not None:
                vm.round(5).to_csv(out_dir / "validation_marginals_subs.csv",
                                   index=False)


if __name__ == "__main__":
    main()
