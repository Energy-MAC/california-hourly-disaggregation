"""Estimate the stochastic disaggregation model (Approach 2) from history.

Builds the three parameter tables defined in docs/stochastic_model_spec.md from
the substation envelopes and EIA-930 CAISO 2015-2025: per-substation-cell
marginal parameters, the per-cell system table (shape s(c), rho(c), F*), and
the historical standardized z(t) series used by the bootstrap z-mode.

No CLI parameters (the estimation window and F-invariant rho are fixed by the
spec; see stochastic_diagnostics.py for window sensitivities).

Outputs (data/processed/load_projection/stochastic/):
  substation_cell_params.csv   387,864 rows: q10/q90, mu/sigma, unif_a/b, flags
  system_cell_params.csv       288 rows: sum_mu/sum_sigma, ybar/sd, implied_f,
                               shape_s, rho, f_star
  caiso_z_history.csv          ~96k rows: dt_pst_hb, demand_mw, z

Usage:
  python scripts/load_projection/estimate_stochastic.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from load_projection.stochastic import (  # noqa: E402
    build_system_cells,
    load_caiso_history,
    load_envelope_cells,
    standardize_z,
)

OUT_DIR = ROOT / "data/processed/load_projection/stochastic"


def main() -> None:
    env = load_envelope_cells()
    caiso = load_caiso_history()
    cells, f_star = build_system_cells(env, caiso)
    zhist = standardize_z(caiso)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    env_out = env[["utility", "substation_name", "month", "hour_pst",
                   "q10", "q90", "mu", "sigma", "unif_a", "unif_b",
                   "zero_width", "inverted", "missing"]]
    env_out.to_csv(OUT_DIR / "substation_cell_params.csv", index=False)

    cells_out = cells[["month", "hour_pst", "sum_mu", "sum_sigma", "ybar", "sd",
                       "n_obs", "implied_f", "shape_s", "rho"]].copy()
    cells_out["f_star"] = f_star
    cells_out.round(6).to_csv(OUT_DIR / "system_cell_params.csv", index=False)

    zout = zhist[["dt_pst_hb", "demand_mw", "z"]].copy()
    zout[["demand_mw", "z"]] = zout[["demand_mw", "z"]].round(6)
    zout.to_csv(OUT_DIR / "caiso_z_history.csv", index=False)

    n_subs = env.groupby(["utility", "substation_name"]).ngroups
    n_dataless = (env.groupby(["utility", "substation_name"])["missing"].all()).sum()
    print(f"substations: {n_subs:,} ({n_dataless} with no data at all)   "
          f"cells: {len(env):,} (zero-width {env.zero_width.sum():,}, "
          f"inverted-swapped {env.inverted.sum()}, missing {env.missing.sum():,})")
    print(f"calibrated level F* (annual IOU energy share of CAISO): {f_star:.4f}")
    print(f"shape s(c): range {cells.shape_s.min():.3f}-{cells.shape_s.max():.3f}; by hour:")
    print(cells.groupby("hour_pst")["shape_s"].mean().round(3).to_string())
    print(f"rho(c): median {cells.rho.median():.3f}, "
          f"range {cells.rho.min():.3f}-{cells.rho.max():.3f}, capped cells: "
          f"{(cells.rho >= 1.0).sum()}")
    print(f"z history: {len(zhist):,} hours "
          f"({zhist.dt_pst_hb.dt.year.min()}-{zhist.dt_pst_hb.dt.year.max()})")
    print(f"\nwrote 3 tables to {OUT_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
