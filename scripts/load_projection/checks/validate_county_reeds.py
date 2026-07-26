"""Validate Approach 2 (stochastic) county totals against ReEDS county loads.

ReEDS itself only reports load at p-region resolution (p8/p9/p10/p11), but
disaggregate_reeds.py's Stage 1 already disaggregates that down to counties
using a purely geographic weight (`county_pgroup_fraction`, from
`ca_load_fraction` in county_ca_reference.csv) that has no dependence on
substation load shape. That geographic county load is treated here as an
independent reference: for each county with >=1 IOU substation, its
implied annual load (p_region annual load x county_pgroup_fraction) is
compared against the bottom-up total obtained by summing that county's
substations' Approach 2 stochastic annual output. Overlap window is the 8
years common to both sources' historic coverage (2016-2023).

CLI parameters:
  --stochastic-run   run-tag folder under
                      data/processed/load_projection/projections/
                      (default stochastic__eia930__normal__Fcal__native)

Outputs (data/processed/load_projection/validation/):
  county_reeds_stochastic_annual_long.csv    county x year: both totals, error
  county_reeds_stochastic_annual_summary.csv per-county RMSE/relative RMSE/bias
Figure (data/figures/load_projection/validation/):
  county_reeds_relrmse_bar.png   per-county relative RMSE, sorted

Usage:
  python scripts/load_projection/checks/validate_county_reeds.py
  python scripts/load_projection/checks/validate_county_reeds.py --stochastic-run stochastic__eia930__uniform__Fcal__native
"""

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from load_projection.weights import compute_county_pgroup_fractions  # noqa: E402

PROCESSED = ROOT / "data/processed"
SUB_COUNTY = PROCESSED / "substations/substation_county_reeds_mapping.csv"
COUNTY_REF = PROCESSED / "reeds/county_ca_reference.csv"
REEDS_HIST = PROCESSED / "reeds/historic_ca_load_hourly.parquet"
PROJ_ROOT = PROCESSED / "load_projection/projections"
OUT_DIR = PROCESSED / "load_projection/validation"
FIG_DIR = ROOT / "data/figures/load_projection/validation"

P_REGIONS = ["p8", "p9", "p10", "p11"]


def reeds_county_annual() -> pd.DataFrame:
    """(county_name, fips_int, p_region, year) -> reeds_mwh via geographic
    p_region -> county disaggregation (Stage 1 of disaggregate_reeds.py)."""
    hist = pd.read_parquet(REEDS_HIST)
    p_annual = hist.groupby("year")[[f"{p}_mw" for p in P_REGIONS]].sum()
    p_annual = p_annual.rename(columns=lambda c: c.replace("_mw", "")).stack()
    p_annual.index.names = ["year", "p_region"]
    p_annual = p_annual.rename("p_region_mwh").reset_index()

    county_ref = pd.read_csv(COUNTY_REF)
    cw = compute_county_pgroup_fractions(county_ref)
    m = cw.merge(p_annual, on="p_region")
    m["reeds_mwh"] = m.pgroup_fraction * m.p_region_mwh
    return m[["fips_int", "county_name", "p_region", "year", "reeds_mwh"]]


def stochastic_county_annual(run_tag: str) -> pd.DataFrame:
    """(county_name, fips_int, year) -> stochastic_mwh, mean over MC draws
    then summed over substations in the county."""
    run_dir = PROJ_ROOT / run_tag
    ann = pd.read_csv(run_dir / "substation_annual_mwh.csv")
    ann["utility"] = ann.utility.str.lower()
    per_sub_year = ann.groupby(["utility", "substation_name", "year"],
                               as_index=False)["annual_mwh"].mean()

    sc = pd.read_csv(SUB_COUNTY, usecols=["utility", "substation_name",
                                          "fips_int", "county_name"])
    sc["utility"] = sc.utility.str.lower()
    j = per_sub_year.merge(sc, on=["utility", "substation_name"], how="inner")
    dropped = len(per_sub_year) - len(j)
    if dropped:
        print(f"  note: {dropped} substation-year rows had no county match "
              f"(likely synthetic substations, excluded from this check)")
    return j.groupby(["fips_int", "county_name", "year"],
                     as_index=False)["annual_mwh"].sum().rename(
        columns={"annual_mwh": "stochastic_mwh"})


def summarize(long: pd.DataFrame) -> pd.DataFrame:
    def per_county(g):
        err = g.stochastic_mwh - g.reeds_mwh
        rmse = np.sqrt((err ** 2).mean())
        mean_reeds = g.reeds_mwh.mean()
        return pd.Series({
            "n_years": len(g),
            "mean_reeds_mwh": mean_reeds,
            "mean_stochastic_mwh": g.stochastic_mwh.mean(),
            "rmse_mwh": rmse,
            "rel_rmse_pct": 100 * rmse / mean_reeds if mean_reeds > 0 else np.nan,
            "mean_bias_pct": 100 * err.mean() / mean_reeds if mean_reeds > 0 else np.nan,
        })
    return (long.groupby(["fips_int", "county_name"])
            .apply(per_county, include_groups=False)
            .reset_index().sort_values("rel_rmse_pct", ascending=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stochastic-run", default="stochastic__eia930__normal__Fcal__native")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)

    print(f"loading ReEDS historic county-implied annual load...")
    reeds = reeds_county_annual()
    print(f"loading stochastic run {args.stochastic_run!r}...")
    stoch = stochastic_county_annual(args.stochastic_run)

    long = stoch.merge(reeds[["fips_int", "year", "reeds_mwh"]],
                       on=["fips_int", "year"], how="inner")
    long["error_mwh"] = long.stochastic_mwh - long.reeds_mwh
    long["error_pct"] = 100 * long.error_mwh / long.reeds_mwh
    print(f"{long.county_name.nunique()} counties x up to {long.year.nunique()} "
          f"years ({sorted(long.year.unique())}) = {len(long)} county-year rows")

    long.sort_values(["county_name", "year"]).round(2).to_csv(
        OUT_DIR / "county_reeds_stochastic_annual_long.csv", index=False)

    summary = summarize(long)
    summary.round(3).to_csv(
        OUT_DIR / "county_reeds_stochastic_annual_summary.csv", index=False)

    pooled_rmse = np.sqrt((long.error_mwh ** 2).mean())
    pooled_rel = 100 * pooled_rmse / long.reeds_mwh.mean()
    print(f"\npooled RMSE: {pooled_rmse:,.0f} MWh  "
          f"({pooled_rel:.2f}% of mean county-year load)")
    print("\nworst 10 counties by relative RMSE:")
    print(summary.head(10)[["county_name", "n_years", "mean_reeds_mwh",
                            "rel_rmse_pct", "mean_bias_pct"]].to_string(index=False))
    print("\nbest 10 counties by relative RMSE:")
    print(summary.tail(10)[["county_name", "n_years", "mean_reeds_mwh",
                            "rel_rmse_pct", "mean_bias_pct"]].to_string(index=False))

    fig, ax = plt.subplots(figsize=(8, max(6, 0.22 * len(summary))))
    s = summary.sort_values("rel_rmse_pct")
    ax.barh(s.county_name, s.rel_rmse_pct, color="#4575b4")
    ax.set_xlabel("Relative RMSE (%) vs ReEDS county-implied annual load")
    ax.set_title(f"Stochastic (Approach 2) vs ReEDS county load\n"
                f"{args.stochastic_run}, {long.year.min()}-{long.year.max()}")
    fig.tight_layout()
    fig_path = FIG_DIR / "county_reeds_relrmse_bar.png"
    fig.savefig(fig_path, dpi=150)
    print(f"\nwrote {(OUT_DIR / 'county_reeds_stochastic_annual_long.csv').relative_to(ROOT)}")
    print(f"wrote {(OUT_DIR / 'county_reeds_stochastic_annual_summary.csv').relative_to(ROOT)}")
    print(f"wrote {fig_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
