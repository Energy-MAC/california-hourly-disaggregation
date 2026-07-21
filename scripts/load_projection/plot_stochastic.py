"""Figures for the stochastic disaggregation model (Approach 2).

Produces (a) CLT convergence demos for one high-load substation — individual
Monte Carlo draws layered one at a time until their mean converges to the
model conditional mean — as per-frame PNGs plus an animated GIF, for a day, a
month, and a year, against both historical CAISO (EIA-930) and a RESOLVE
weather-year forecast; (b) 12-subplot month panels of rho(c) and shape s(c).

CLI parameters:
  --which        clt-eia930 | clt-resolve | params | all (default all)
  --substation   "utility:NAME" (default: highest mean-load substation)
  --n-draws      draws generated for the demos (default 50)
  --year         display year for the EIA-930 demos (default 2024)
  --weather-year RESOLVE display weather year (default 2012)
  --month        display month for day/month demos (default 7)
  --day          display day-of-month for the day demo (default 15)
  --seed         RNG seed (default 0)

Outputs (data/figures/load_projection/stochastic/):
  rho_by_month_hour.png, shape_s_by_month_hour.png
  clt_{source}_{period}/frame_*.png + clt_{source}_{period}.gif
      for source in {eia930, resolve} and period in {day, month, year}
      (each GIF lives in its own subfolder with its frames)

Usage:
  python scripts/load_projection/plot_stochastic.py
  python scripts/load_projection/plot_stochastic.py --which params
  python scripts/load_projection/plot_stochastic.py --which clt-eia930 --substation "sce:Center"
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

ROOT = Path(__file__).resolve().parents[2]
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


def draw_matrix(mats, cells, target, z, n_draws, seed, sub_idx) -> np.ndarray:
    """[n_hours, n_draws] draws for one substation over the target hours."""
    out = np.empty((len(target), n_draws), dtype=np.float32)
    for d in range(n_draws):
        rng = np.random.default_rng(seed + 1000 * d)
        out[:, d] = generate(mats, cells, target, z, "normal", 1.0, rng)[:, sub_idx]
    return out


def conditional_mean(mats, cells, target, z, sub_idx) -> np.ndarray:
    k = target.cell.values
    rho = cells.rho.reindex(range(288)).values[k]
    return mats.mu[sub_idx, k] + mats.sigma[sub_idx, k] * np.sqrt(rho) * z


def envelope_band(mats, target, sub_idx) -> tuple[np.ndarray, np.ndarray]:
    k = target.cell.values
    z90 = 1.2815515655446004
    mu, sg = mats.mu[sub_idx, k], mats.sigma[sub_idx, k]
    return mu - z90 * sg, mu + z90 * sg  # the utility q10/q90 envelope


def clt_gif(folder_name: str, target: pd.DataFrame, draws: np.ndarray,
            cond_mean: np.ndarray, band: tuple, xvals, xlabel: str,
            sub_label: str, period_label: str, daily_mean: bool) -> None:
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
        ax.fill_between(xv, lo, hi, color="#bbbbbb", alpha=0.35,
                        label="utility envelope (q10–q90)")
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
            sub_idx, args) -> None:
    """Generate draws once for the display year, then cut day/month/year views."""
    target_full = standardize_z(target_full)
    disp_year = args.year if source == "eia930" else args.weather_year
    year_mask = target_full.dt_pst_hb.dt.year == disp_year
    t_year = target_full[year_mask].reset_index(drop=True)
    z_year = t_year.z.values
    draws = draw_matrix(mats, cells, t_year, z_year, args.n_draws, args.seed, sub_idx)
    cm = conditional_mean(mats, cells, t_year, z_year, sub_idx)
    band = envelope_band(mats, t_year, sub_idx)
    sub_label = f"{util.upper()} {name} ({source}, "
    sub_label += f"{disp_year})" if source == "eia930" else f"weather year {disp_year})"

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
                daily_mean=(period == "year"))


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--which", choices=["clt-eia930", "clt-resolve", "params", "all"],
                    default="all")
    ap.add_argument("--substation", default=None, help='"utility:NAME"')
    ap.add_argument("--n-draws", type=int, default=50)
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--weather-year", type=int, default=2012)
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

    if args.which in ("clt-eia930", "clt-resolve", "all"):
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


if __name__ == "__main__":
    main()
