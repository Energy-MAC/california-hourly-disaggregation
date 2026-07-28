"""Figures for the stochastic disaggregation model (Approach 2).

Produces (a) CLT convergence demos for one high-load substation — individual
Monte Carlo draws layered one at a time until their mean converges to the
model conditional mean — as per-frame PNGs plus an animated GIF, for a day, a
month, and a year, against historical CAISO (EIA-930), a RESOLVE weather-year
forecast, and an out-of-sample RESOLVE future-year projection; (b) 12-subplot
month panels of rho(c) and shape s(c).

`clt-resolve-future` is genuinely out-of-sample: today's substation envelopes
(mu, sigma) never change, but RESOLVE's own annual energy forecast growth
(net of BTM PV, using that vintage's planned-capacity projection) scales the
model's output level via the existing F/F* `scale` knob in `generate()` — the
same mechanism used for F sensitivity, applied here as a what-if "this
substation grows at the statewide average rate" rather than a re-estimate.

CLI parameters:
  --which        clt-eia930 | clt-resolve | clt-resolve-future | params | all (default all)
  --substation   "utility:NAME" (default: highest mean-load substation)
  --n-draws      draws generated for the demos (default 50)
  --year         display year for the EIA-930 demos (default 2024)
  --weather-year RESOLVE display weather year, also the shape donor for
                 clt-resolve-future (default 2012)
  --future-year  RESOLVE annual-forecast year for clt-resolve-future,
                 2025-2045 (default 2042)
  --month        display month for day/month demos (default 7)
  --day          display day-of-month for the day demo (default 15)
  --seed         RNG seed (default 0)

Outputs (data/figures/load_projection/stochastic/):
  rho_by_month_hour.png, shape_s_by_month_hour.png
  clt_{source}_{period}/frame_*.png + clt_{source}_{period}.gif
      for source in {eia930, resolve, resolve{future_year}} and period in
      {day, month, year} (each GIF lives in its own subfolder with its
      frames; clt-resolve-future writes clt_resolve{future_year}_* folders,
      never touching the existing clt_resolve_* ones)

Usage:
  python scripts/load_projection/approach2/plot_stochastic.py
  python scripts/load_projection/approach2/plot_stochastic.py --which params
  python scripts/load_projection/approach2/plot_stochastic.py --which clt-eia930 --substation "sce:Center"
  python scripts/load_projection/approach2/plot_stochastic.py --which clt-resolve-future --future-year 2042
"""

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from load_projection.stochastic import (  # noqa: E402
    EnvelopeMatrices,
    build_system_cells,
    cell_index,
    generate,
    load_caiso_history,
    load_envelope_cells,
    standardize_z,
)

FIG_DIR = ROOT / "data/figures/load_projection/stochastic"
RESOLVE_FILE = ROOT / "data/processed/resolve/resolve_hourly_profiles.csv"
RESOLVE_ANNUAL_FILE = ROOT / "data/processed/resolve/resolve_annual_forecast.csv"
RESOLVE_RAW = (ROOT / "data" / "raw" / "RESOLVE Code Base and Inputs"
               / "RESOLVE Code Base and Inputs")
RESOLVE_PMAX_DIR = RESOLVE_RAW / "data" / "profiles" / "pmax" / "2025"
RESOLVE_RSRC_DIR = RESOLVE_RAW / "data" / "interim" / "resources"
RESOLVE_BTM_SCENARIO = "2024_IEPR_Local_Reliability"  # matches process_resolve.py
FUTURE_IOUS = ["PGE", "SCE", "SDGE"]
FRAME_KS = [1, 2, 3, 5, 8, 12, 20, 30, 50]  # draws shown per GIF frame


# ---------------------------------------------------------------------------
# Parameter panels (figures 3 and 4)
# ---------------------------------------------------------------------------

def month_hour_panel(cells: pd.DataFrame, col: str, ylabel: str, title: str,
                     out_path: Path) -> None:
    fig, axes = plt.subplots(3, 4, figsize=(13, 8), sharex=True, sharey=True)
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for m, ax in enumerate(axes.flat, start=1):
        sub = cells[cells.month == m].sort_values("hour_pst")
        ax.plot(sub.hour_pst, sub[col], color="#1f6f8b", lw=1.8)
        ax.fill_between(sub.hour_pst, sub[col], color="#1f6f8b", alpha=0.15)
        ax.set_title(months[m - 1], fontsize=10)
        ax.set_xticks([0, 6, 12, 18, 23])
        ax.grid(alpha=0.3, lw=0.5)
    for ax in axes[-1]:
        ax.set_xlabel("hour (PST)")
    for ax in axes[:, 0]:
        ax.set_ylabel(ylabel)
    fig.suptitle(title, fontsize=13)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"wrote {out_path.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
# CLT convergence demos (figures 1 and 2)
# ---------------------------------------------------------------------------

def pick_substation(env: pd.DataFrame, arg: str | None) -> tuple[str, str]:
    """Default pick: among the top 20 substations by mean load, the one with
    the smoothest hourly profile (several top-load substations have erratic
    hour-to-hour envelopes that make poor representative examples)."""
    if arg:
        util, name = arg.split(":", 1)
        return util, name
    mean_mu = env.groupby(["utility", "substation_name"])["mu"].mean()
    best, best_rough = None, np.inf
    for util, name in mean_mu.nlargest(20).index:
        s = env[(env.utility == util) & (env.substation_name == name)].sort_values(
            ["month", "hour_pst"])
        rough = s.groupby("month")["mu"].apply(
            lambda x: np.abs(np.diff(x)).mean()).mean() / mean_mu[(util, name)]
        if rough < best_rough:
            best, best_rough = (util, name), rough
    return best


def draw_matrix(mats, cells, target, z, n_draws, seed, sub_idx, scale=1.0) -> np.ndarray:
    """[n_hours, n_draws] draws for one substation over the target hours.

    `scale` = F/F*, the model's built-in level knob (1.0 = today's
    calibration); an out-of-sample future run passes a growth ratio here.
    """
    out = np.empty((len(target), n_draws), dtype=np.float32)
    for d in range(n_draws):
        rng = np.random.default_rng(seed + 1000 * d)
        out[:, d] = generate(mats, cells, target, z, "normal", scale, rng)[:, sub_idx]
    return out


def conditional_mean(mats, cells, target, z, sub_idx, scale=1.0) -> np.ndarray:
    k = target.cell.values
    rho = cells.rho.reindex(range(288)).values[k]
    return scale * (mats.mu[sub_idx, k] + mats.sigma[sub_idx, k] * np.sqrt(rho) * z)


def envelope_band(mats, target, sub_idx, scale=1.0) -> tuple[np.ndarray, np.ndarray]:
    """`scale`=1.0 returns the actual utility q10/q90 envelope; scale != 1.0
    returns that envelope proportionally scaled (an implied future envelope,
    not an observed one)."""
    k = target.cell.values
    z90 = 1.2815515655446004
    mu, sg = mats.mu[sub_idx, k], mats.sigma[sub_idx, k]
    return scale * (mu - z90 * sg), scale * (mu + z90 * sg)


def clt_gif(folder_name: str, target: pd.DataFrame, draws: np.ndarray,
            cond_mean: np.ndarray, band: tuple, xvals, xlabel: str,
            sub_label: str, period_label: str, daily_mean: bool,
            band_label: str = "utility envelope (q10–q90)") -> None:
    out_dir = FIG_DIR / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.png"):
        old.unlink()

    if daily_mean:  # year view: aggregate hours to daily means for legibility
        day = pd.to_datetime(target.dt_pst_hb.dt.date)
        grp = pd.DataFrame(draws, index=day).groupby(level=0).mean()
        draws_p, xv = grp.values, grp.index
        cm = pd.Series(cond_mean, index=day).groupby(level=0).mean().values
        lo = pd.Series(band[0], index=day).groupby(level=0).mean().values
        hi = pd.Series(band[1], index=day).groupby(level=0).mean().values
    else:
        draws_p, xv, cm, lo, hi = draws, xvals, cond_mean, band[0], band[1]

    frames = []
    for i, kd in enumerate(FRAME_KS):
        kd = min(kd, draws_p.shape[1])
        fig, ax = plt.subplots(figsize=(11, 5.2))
        ax.fill_between(xv, lo, hi, color="#bbbbbb", alpha=0.35, label=band_label)
        ax.plot(xv, draws_p[:, :kd], color="#7fb2d9", lw=0.7,
                alpha=max(0.12, 0.75 / kd ** 0.5))
        ax.plot(xv, draws_p[:, :kd].mean(axis=1), color="#c0392b", lw=1.8,
                label=f"mean of {kd} draw{'s' if kd > 1 else ''}")
        ax.plot(xv, cm, color="black", lw=1.2, ls="--",
                label="model conditional mean")
        ax.plot([], [], color="#7fb2d9", lw=0.9, label="individual draws")
        ax.set_xlabel(xlabel)
        ax.set_ylabel("load (MW)" + (" — daily mean" if daily_mean else ""))
        ax.set_title(f"{sub_label} — {period_label} — {kd} draw"
                     f"{'s' if kd > 1 else ''}")
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(alpha=0.3, lw=0.5)
        fig.tight_layout()
        frame_path = out_dir / f"frame_{i + 1:02d}_k{kd:03d}.png"
        fig.savefig(frame_path, dpi=120)
        plt.close(fig)
        frames.append(frame_path)

    imgs = [Image.open(p) for p in frames]
    gif_path = out_dir / f"{folder_name}.gif"
    imgs[0].save(gif_path, save_all=True, append_images=imgs[1:],
                 duration=900, loop=0)
    print(f"wrote {gif_path.relative_to(ROOT)} ({len(frames)} frames)")


def run_clt(source: str, target_full: pd.DataFrame, mats, cells, util, name,
            sub_idx, args, disp_year: int | None = None, scale: float = 1.0,
            sub_label: str | None = None) -> None:
    """Generate draws once for the display year, then cut day/month/year views."""
    target_full = standardize_z(target_full)
    if disp_year is None:
        disp_year = args.year if source == "eia930" else args.weather_year
    year_mask = target_full.dt_pst_hb.dt.year == disp_year
    t_year = target_full[year_mask].reset_index(drop=True)
    z_year = t_year.z.values
    draws = draw_matrix(mats, cells, t_year, z_year, args.n_draws, args.seed, sub_idx, scale)
    cm = conditional_mean(mats, cells, t_year, z_year, sub_idx, scale)
    band = envelope_band(mats, t_year, sub_idx, scale)
    if sub_label is None:
        sub_label = f"{util.upper()} {name} ({source}, "
        sub_label += f"{disp_year})" if source == "eia930" else f"weather year {disp_year})"
    band_label = ("utility envelope (q10–q90)" if scale == 1.0
                  else f"implied envelope (today's shape x{scale:.2f})")

    views = {
        "day": (t_year.dt_pst_hb.dt.month == args.month)
               & (t_year.dt_pst_hb.dt.day == args.day),
        "month": t_year.dt_pst_hb.dt.month == args.month,
        "year": np.ones(len(t_year), bool),
    }
    month_name = pd.Timestamp(2000, args.month, 1).strftime("%B")
    labels = {"day": f"{month_name} {args.day}", "month": month_name,
              "year": "full year"}
    for period, mask in views.items():
        mask = np.asarray(mask)
        t = t_year[mask]
        xv = t.dt_pst_hb
        xlabel = {"day": "hour (PST)", "month": "day", "year": "date"}[period]
        clt_gif(f"clt_{source}_{period}", t, draws[mask],
                cm[mask], (band[0][mask], band[1][mask]),
                xv, xlabel, sub_label, labels[period],
                daily_mean=(period == "year"), band_label=band_label)


def load_resolve_target() -> pd.DataFrame:
    """CAISO-consistent RESOLVE series: PGE+SCE+SDGE net-of-BTM hourly sum
    across all 23 weather years (net-to-net with the model calibration)."""
    r = pd.read_csv(RESOLVE_FILE, parse_dates=["datetime_pst"])
    r = r[r.utility.isin(["PGE", "SCE", "SDGE"])]
    y = r.groupby("datetime_pst")["demand_mw_net"].sum().reset_index()
    y.columns = ["dt_pst_hb", "demand_mw"]
    y["month"] = y.dt_pst_hb.dt.month
    y["hour_pst"] = y.dt_pst_hb.dt.hour
    y["cell"] = cell_index(y.month, y.hour_pst)
    return y


def load_resolve_target_future(weather_year: int, future_year: int) -> tuple[pd.DataFrame, float]:
    """Out-of-sample RESOLVE series for `future_year`: one weather year's
    shape (`weather_year`, 2000-2022), rescaled from its 2024 annual-energy
    basis to RESOLVE's own `future_year` forecast, per utility, then netted
    against that weather year's solar CF times `future_year`'s planned BTM
    PV capacity (both from the raw RESOLVE inputs -- same source/scenario
    process_resolve.py uses for the 2024 BTM offset, just at a different
    year). No substation data informs this scale; it is purely a RESOLVE
    growth projection. Returns (CAISO-consistent PGE+SCE+SDGE net hourly
    target, net growth ratio vs the same weather year's demand_mw_net) --
    the ratio is what should be passed as `scale` to generate()/
    conditional_mean()/envelope_band(), since F*=1.0 is calibrated at
    today's net level.
    """
    r = pd.read_csv(RESOLVE_FILE, parse_dates=["datetime_pst"],
                     usecols=["datetime_pst", "utility", "demand_mw_2024scaled", "demand_mw_net"])
    r = r[(r.utility.isin(FUTURE_IOUS)) & (r.datetime_pst.dt.year == weather_year)]
    annual = pd.read_csv(RESOLVE_ANNUAL_FILE)
    annual = annual[annual.utility.isin(FUTURE_IOUS)]

    pieces = []
    for util, g in r.groupby("utility"):
        g = g.copy()
        target_2024 = annual.loc[(annual.utility == util) & (annual.year == 2024),
                                  "energy_mwh"].iloc[0]
        target_future = annual.loc[(annual.utility == util) & (annual.year == future_year),
                                    "energy_mwh"].iloc[0]
        g["demand_mw_future_gross"] = g["demand_mw_2024scaled"] * (target_future / target_2024)

        pmax = pd.read_csv(RESOLVE_PMAX_DIR / f"{util}_Customer_PV.csv", parse_dates=["datetime"])
        pmax = pmax[pmax.datetime.dt.year == weather_year].rename(
            columns={"datetime": "datetime_pst", "Weather Factor": "weather_factor"})
        rsrc = pd.read_csv(RESOLVE_RSRC_DIR / f"{util}_Customer_PV.csv")
        cap_row = rsrc[(rsrc.attribute == "planned_capacity")
                       & (rsrc.scenario == RESOLVE_BTM_SCENARIO)
                       & (pd.to_datetime(rsrc.timestamp).dt.year == future_year)]
        capacity_future = float(cap_row["value"].iloc[0])

        g = g.merge(pmax[["datetime_pst", "weather_factor"]], on="datetime_pst", how="left")
        g["btm_pv_mw_future"] = g["weather_factor"].fillna(0.0) * capacity_future
        g["demand_mw_net_future"] = g["demand_mw_future_gross"] - g["btm_pv_mw_future"]
        pieces.append(g[["datetime_pst", "demand_mw_net", "demand_mw_net_future"]])

    combined = pd.concat(pieces, ignore_index=True)
    totals = combined.groupby("datetime_pst")[["demand_mw_net", "demand_mw_net_future"]].sum()
    growth_ratio = totals["demand_mw_net_future"].sum() / totals["demand_mw_net"].sum()

    y = totals["demand_mw_net_future"].reset_index()
    y.columns = ["dt_pst_hb", "demand_mw"]
    y["month"] = y.dt_pst_hb.dt.month
    y["hour_pst"] = y.dt_pst_hb.dt.hour
    y["cell"] = cell_index(y.month, y.hour_pst)
    return y, float(growth_ratio)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--which",
                    choices=["clt-eia930", "clt-resolve", "clt-resolve-future", "params", "all"],
                    default="all")
    ap.add_argument("--substation", default=None, help='"utility:NAME"')
    ap.add_argument("--n-draws", type=int, default=50)
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--weather-year", type=int, default=2012)
    ap.add_argument("--future-year", type=int, default=2042,
                    help="RESOLVE annual-forecast year for clt-resolve-future (2025-2045)")
    ap.add_argument("--month", type=int, default=7)
    ap.add_argument("--day", type=int, default=15)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    FIG_DIR.mkdir(parents=True, exist_ok=True)
    env = load_envelope_cells()
    caiso = load_caiso_history()
    cells, f_star = build_system_cells(env, caiso)

    if args.which in ("params", "all"):
        month_hour_panel(cells, "rho", "rho",
                         "Common-factor share rho(c) by month and hour "
                         f"(median {cells.rho.median():.2f})",
                         FIG_DIR / "rho_by_month_hour.png")
        month_hour_panel(cells, "shape_s", "s(c)",
                         "IOU-share shape s(c) by month and hour "
                         f"(F* = {f_star:.4f}, mean 1 by construction)",
                         FIG_DIR / "shape_s_by_month_hour.png")

    if args.which in ("clt-eia930", "clt-resolve", "clt-resolve-future", "all"):
        mats = EnvelopeMatrices(env)
        util, name = pick_substation(env, args.substation)
        sub_idx = mats.subs.index[(mats.subs.utility == util)
                                  & (mats.subs.substation_name == name)][0]
        mean_mu = env[(env.utility == util) & (env.substation_name == name)].mu.mean()
        print(f"substation: {util.upper()} {name} (mean load {mean_mu:.1f} MW)")
        if args.which in ("clt-eia930", "all"):
            run_clt("eia930", caiso, mats, cells, util, name, sub_idx, args)
        if args.which in ("clt-resolve", "all"):
            run_clt("resolve", load_resolve_target(), mats, cells, util, name,
                    sub_idx, args)
        if args.which == "clt-resolve-future":
            target, growth_ratio = load_resolve_target_future(args.weather_year, args.future_year)
            print(f"RESOLVE {args.future_year} net-of-BTM growth vs weather year "
                  f"{args.weather_year}'s 2024 basis: x{growth_ratio:.3f}")
            sub_label = (f"{util.upper()} {name} (RESOLVE {args.future_year} forecast, "
                        f"weather year {args.weather_year} shape, growth x{growth_ratio:.2f})")
            run_clt(f"resolve{args.future_year}", target, mats, cells, util, name, sub_idx,
                    args, disp_year=args.weather_year, scale=growth_ratio, sub_label=sub_label)


if __name__ == "__main__":
    main()
