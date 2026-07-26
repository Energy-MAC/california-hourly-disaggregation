"""Statewide-total validation: how well do Approach 1 (weights) and Approach 2
(stochastic) match independent reference series, at both the CAISO-only and
full-California-statewide geographic scope?

By design, Approach 2's calibration target IS EIA-930 CAISO — but at F* =
0.7361 of it, not 100% (F* is the energy-weighted MEAN ratio over the whole
2015-2025 calibration window, so individual years can and do drift a little
from exactly F*; that per-year drift, not the ~-26% offset itself, is the
informative part of the Approach 2 vs CAISO comparison below). Approach 1's
chain weights redistribute
ReEDS p-region load with a construction that conserves each p-region's total
EXACTLY (chain weight sum-to-1 check, see disaggregate_reeds.py) — so
Approach 1's full statewide total (all substations, including the 3 synthetic
p8 ones) reconstructs ReEDS's OWN CA_total by construction, not independently.
What is genuinely informative here is comparing against the OTHER geographic
scope each approach was not built for:
  - Approach 2 (IOU-only, PGE/SCE/SDGE) vs statewide CA_total: expected gap =
    the municipal-utility (SMUD/IID/LADWP/MID/PacifiCorp) share of the state.
  - Approach 1 (all p-regions incl. PacifiCorp) vs the REAL observed EIA-930
    CAISO series: this is really testing whether ReEDS's own historic load
    reconstruction agrees with actual observed demand, since Approach 1
    equals ReEDS's numbers by construction — not an Approach-1-specific
    error. ReEDS's "CAISO_total" region (p9+p10+p11) is also NOT the same
    entity as the EIA-930 CISO balancing authority: it additionally includes
    BANC/IID/LDWP/TIDC territory geographically inside those counties (see
    CLAUDE.md's ReEDS geographic note), so daylight here is expected and is
    mostly definitional/geographic, not model error.

Overlap window: 2016-2023 (ReEDS historic coverage; EIA-930/stochastic runs
cover more years but are restricted to this window for a fair comparison).

CLI parameters:
  --stochastic-run   run-tag folder under
                      data/processed/load_projection/projections/
                      (default stochastic__eia930__normal__Fcal__native)
  --weights-run       run-tag folder for Approach 1
                      (default reeds_historic__max_load__monthhour)

Outputs (data/processed/load_projection/validation/):
  approach_totals_vs_reference.csv   year x {eia930_caiso, reeds_caiso_total,
                              reeds_ca_total, approach1_total, approach2_total,
                              + relative-error columns vs both references}
Figure (data/figures/load_projection/validation/):
  approach_totals_vs_reference.png   two panels (Approach 2, Approach 1),
                              each: model line vs EIA-930 CAISO vs CA_total

Usage:
  python scripts/load_projection/checks/validate_approach_totals.py
"""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
PROCESSED = ROOT / "data/processed"
EIA_FILE = PROCESSED / "eia/eia930_operations.csv"
REEDS_HIST_ANNUAL = PROCESSED / "reeds/historic_ca_load_annual.csv"
PROJ_ROOT = PROCESSED / "load_projection/projections"
OUT_DIR = PROCESSED / "load_projection/validation"
FIG_DIR = ROOT / "data/figures/load_projection/validation"

YEAR_RANGE = (2016, 2023)  # ReEDS historic coverage


def eia930_caiso_annual() -> pd.Series:
    eia = pd.read_csv(EIA_FILE, parse_dates=["datetime_utc"])
    c = eia[eia.ba_code == "CISO"].copy()
    c["year"] = (c.datetime_utc - pd.Timedelta(hours=9)).dt.year
    y0, y1 = YEAR_RANGE
    c = c[(c.year >= y0) & (c.year <= y1)]
    return c.groupby("year")["demand_mwh"].sum().rename("eia930_caiso_mwh")


def reeds_reference_annual() -> pd.DataFrame:
    a = pd.read_csv(REEDS_HIST_ANNUAL)
    piv = a.pivot(index="year", columns="region", values="annual_mwh")
    return piv.rename(columns={"CAISO_total": "reeds_caiso_total_mwh",
                               "CA_total": "reeds_ca_total_mwh"})[
        ["reeds_caiso_total_mwh", "reeds_ca_total_mwh"]]


def approach2_annual(run_tag: str) -> pd.Series:
    """Mean over MC draws, summed over all substations (PGE/SCE/SDGE only)."""
    ann = pd.read_csv(PROJ_ROOT / run_tag / "substation_annual_mwh.csv")
    per_sub_year = ann.groupby(["utility", "substation_name", "year"],
                               as_index=False)["annual_mwh"].mean()
    y0, y1 = YEAR_RANGE
    per_sub_year = per_sub_year[(per_sub_year.year >= y0) & (per_sub_year.year <= y1)]
    return per_sub_year.groupby("year")["annual_mwh"].sum().rename("approach2_mwh")


def approach1_annual(run_tag: str) -> pd.Series:
    """All substations incl. synthetic p8 (full statewide coverage by construction)."""
    ann = pd.read_csv(PROJ_ROOT / run_tag / "substation_annual_load_by_year.csv")
    return ann.groupby("year")["annual_mwh"].sum().rename("approach1_mwh")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stochastic-run", default="stochastic__eia930__normal__Fcal__native")
    ap.add_argument("--weights-run", default="reeds_historic__max_load__monthhour")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)

    print("loading EIA-930 CAISO actual, ReEDS historic references, "
          "Approach 1 + 2 totals...")
    tbl = pd.concat([
        eia930_caiso_annual(),
        reeds_reference_annual(),
        approach2_annual(args.stochastic_run),
        approach1_annual(args.weights_run),
    ], axis=1).sort_index()
    tbl.index.name = "year"

    tbl["approach2_vs_eia930_pct"] = 100 * (tbl.approach2_mwh / tbl.eia930_caiso_mwh - 1)
    tbl["approach2_vs_ca_total_pct"] = 100 * (tbl.approach2_mwh / tbl.reeds_ca_total_mwh - 1)
    tbl["approach1_vs_eia930_pct"] = 100 * (tbl.approach1_mwh / tbl.eia930_caiso_mwh - 1)
    tbl["approach1_vs_ca_total_pct"] = 100 * (tbl.approach1_mwh / tbl.reeds_ca_total_mwh - 1)
    tbl["reeds_caiso_vs_eia930_pct"] = 100 * (tbl.reeds_caiso_total_mwh / tbl.eia930_caiso_mwh - 1)

    out = tbl.reset_index()
    out.round(2).to_csv(OUT_DIR / "approach_totals_vs_reference.csv", index=False)
    print(out.round(2).to_string(index=False))

    print(f"\nApproach 2 (stochastic, IOU-only) vs EIA-930 CAISO: "
          f"mean {tbl.approach2_vs_eia930_pct.mean():+.2f}% "
          f"(NOT a 0% target — Approach 2 is calibrated to F*={1 + tbl.approach2_vs_eia930_pct.mean() / 100:.4f} "
          f"of CAISO, not 100% of it, since even within CAISO's own footprint "
          f"only the metered PGE/SCE/SDGE substation portion is captured; the "
          f"informative check is that this ratio is STABLE across years, "
          f"range {tbl.approach2_vs_eia930_pct.min():+.2f}% to "
          f"{tbl.approach2_vs_eia930_pct.max():+.2f}%)")
    print(f"Approach 2 (stochastic, IOU-only) vs ReEDS CA_total (statewide): "
          f"mean {tbl.approach2_vs_ca_total_pct.mean():+.2f}% "
          f"(gap = municipal-utility + PacifiCorp share of the state)")
    print(f"Approach 1 (weights, all p-regions) vs EIA-930 CAISO: "
          f"mean {tbl.approach1_vs_eia930_pct.mean():+.2f}% "
          f"(NOT an Approach-1 error — reflects ReEDS's own CAISO_total vs "
          f"actual EIA-930 gap, {tbl.reeds_caiso_vs_eia930_pct.mean():+.2f}%, "
          f"plus the small PacifiCorp/p8 addition)")
    print(f"Approach 1 (weights, all p-regions) vs ReEDS CA_total (statewide): "
          f"mean {tbl.approach1_vs_ca_total_pct.mean():+.4f}% "
          f"(expected ~0 — Approach 1 reconstructs its own ReEDS input exactly "
          f"by construction; this row is a conservation sanity check, not new "
          f"information)")

    fig, axes = plt.subplots(1, 2, figsize=(13, 5), sharey=False)

    ax = axes[0]
    ax.plot(tbl.index, tbl.approach2_mwh / 1e6, "o-", color="#d73027",
           label="Approach 2 (stochastic, IOU-only)")
    ax.plot(tbl.index, tbl.eia930_caiso_mwh / 1e6, "s--", color="#4575b4",
           label="EIA-930 CAISO actual")
    ax.plot(tbl.index, tbl.reeds_ca_total_mwh / 1e6, "^:", color="#1a9850",
           label="ReEDS CA_total (statewide)")
    ax.set_title("Approach 2 (stochastic) vs reference")
    ax.set_ylabel("annual load (TWh)")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)

    ax = axes[1]
    ax.plot(tbl.index, tbl.approach1_mwh / 1e6, "o-", color="#d73027",
           label="Approach 1 (weights, all p-regions)")
    ax.plot(tbl.index, tbl.eia930_caiso_mwh / 1e6, "s--", color="#4575b4",
           label="EIA-930 CAISO actual")
    ax.plot(tbl.index, tbl.reeds_ca_total_mwh / 1e6, "^:", color="#1a9850",
           label="ReEDS CA_total (statewide, = Approach 1 by construction)")
    ax.set_title("Approach 1 (weights) vs reference")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)

    fig.suptitle("Statewide annual total: model output vs independent references (2016-2023)")
    fig.tight_layout()
    fig_path = FIG_DIR / "approach_totals_vs_reference.png"
    fig.savefig(fig_path, dpi=150)
    print(f"\nwrote {(OUT_DIR / 'approach_totals_vs_reference.csv').relative_to(ROOT)}")
    print(f"wrote {fig_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
