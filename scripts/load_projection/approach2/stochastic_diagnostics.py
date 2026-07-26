"""Sanity-check diagnostics for the stochastic substation disaggregation model.

Compares the per-(month, hour_pst) substation load distributions (implied by the
10th/90th-percentile envelopes) against the within-cell distribution of hourly
CAISO load (EIA-930), to establish:

  1. Data hygiene issues in the envelope file (inverted, zero-width, negative
     cells), with anatomy of each issue written to a hygiene report.
  2. Closed-form marginal parameters per cell (normal and uniform fits).
  3. CAISO within-cell statistics, plus a decomposition of within-cell variance
     into within-year (weather) and between-year (trend/interannual) components
     over the full 2015-2025 record vs the selected window.
  4. The independence gap: std of the substation sum under independent sampling
     vs the observed within-cell std of f * CAISO.
  5. The implied scaling factor f = sum(mu) / CAISO_cell_mean, per cell/hour/month.
  6. Negative-load probability mass under the (untruncated) normal marginals.

All calculations are per (month, hour_pst) cell — 288 separate distributions per
substation — so hour-of-day and seasonal differences are already fully separated.
See docs/stochastic_model_spec.md for the model these diagnostics support.

CLI parameters:
  --year-start   first PST year of EIA-930 data for the cell stats (default 2015)
  --year-end     last PST year, inclusive (default 2025)
  --f            scaling factors to evaluate, comma-separated (default "0.70,0.775,0.85")

Outputs (always written):
  data/processed/load_projection/stochastic/diagnostic_cells.csv    per-cell table
  data/processed/load_projection/stochastic/diagnostic_hygiene.csv  problem cells
  data/processed/load_projection/stochastic/hygiene_report.md       hygiene anatomy

Usage:
  python scripts/load_projection/approach2/stochastic_diagnostics.py
  python scripts/load_projection/approach2/stochastic_diagnostics.py --year-start 2022 --year-end 2024
  python scripts/load_projection/approach2/stochastic_diagnostics.py --f 0.70,0.85
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
SUB_FILE = ROOT / "data/processed/substations/substation_load_profiles_clean.csv"
EIA_FILE = ROOT / "data/processed/eia/eia930_operations.csv"
OUT_DIR = ROOT / "data/processed/load_projection/stochastic"

Z90 = 1.2815515655446004  # Phi^-1(0.9): standard-normal 90th-percentile z-score
FULL_YEAR_RANGE = (2015, 2025)  # complete PST years available in EIA-930


def load_substation_cells() -> pd.DataFrame:
    """One row per (utility, substation, month, hour_pst) with envelope quantiles.

    Duplicate cells (PGE scraper overlap) are resolved by taking the cell mean,
    matching rank_substations.py.
    """
    df = pd.read_csv(SUB_FILE)
    df = df.groupby(["utility", "substation_name", "month", "hour_pst"], as_index=False)[
        ["min_load", "max_load"]
    ].mean()
    return df


def hygiene_checks(sub: pd.DataFrame) -> dict:
    """Characterize problem cells; returns pieces for the hygiene report."""
    inverted = sub[sub.min_load > sub.max_load].copy()
    inverted["issue"] = "min_gt_max"
    zw = sub[sub.min_load == sub.max_load].copy()
    neg_min = sub[sub.min_load < 0]
    neg_max = sub[sub.max_load < 0]
    missing = sub[sub.min_load.isna() | sub.max_load.isna()]
    dataless_subs = missing.groupby(["utility", "substation_name"]).size()
    dataless_subs = dataless_subs[dataless_subs == 288]
    half_missing = missing[missing.min_load.notna() | missing.max_load.notna()]

    zw_per_sub = zw.groupby(["utility", "substation_name"]).size().sort_values(ascending=False)
    sub_mu = sub.assign(mu=(sub.min_load + sub.max_load) / 2)
    fleet_size = sub_mu.groupby(["utility", "substation_name"])["mu"].mean()

    n_cells = len(sub)
    print("=" * 70)
    print("SECTION 1 - DATA HYGIENE")
    print("=" * 70)
    print(f"total cells: {n_cells:,}  "
          f"({sub.groupby(['utility', 'substation_name']).ngroups:,} substations)")
    print(f"\ninverted cells (min_load > max_load): {len(inverted):,} "
          f"({len(inverted) / n_cells:.2%}) across "
          f"{inverted.groupby(['utility', 'substation_name']).ngroups} substations")
    if len(inverted):
        print(inverted.head(5).to_string(index=False))

    print(f"\nzero-width cells (min == max, sigma = 0): {len(zw):,} "
          f"({len(zw) / n_cells:.2%})")
    print(f"  value exactly zero: {(zw.min_load == 0).sum():,}   "
          f"positive (all <= {zw.min_load.max():.2f} MW): {(zw.min_load > 0).sum()}")
    print(f"  by utility: {zw.groupby('utility').size().to_dict()}")
    print(f"  substations affected: {len(zw_per_sub)}   "
          f"fully flat (288/288 cells, all 0.0 MW): {(zw_per_sub == 288).sum()}")
    aff_size = fleet_size.loc[zw_per_sub.index]
    print(f"  mean load of affected substations: {aff_size.mean():.2f} MW "
          f"(max {aff_size.max():.2f}) vs fleet mean {fleet_size.mean():.1f} MW")
    print(f"  total mean load in affected substations: {aff_size.sum():.1f} MW "
          f"of fleet total {fleet_size.sum():,.0f} MW")

    print(f"\nmissing cells (min and/or max NaN): {len(missing):,}")
    print(f"  substations with NO data (288/288 NaN): {len(dataless_subs)} "
          f"({', '.join(n for _, n in dataless_subs.index)})")
    print(f"  half-missing cells (one quantile NaN): {len(half_missing)} across "
          f"{half_missing.groupby(['utility', 'substation_name']).ngroups} substations")

    print(f"\nnegative min_load cells: {len(neg_min):,} across "
          f"{neg_min.groupby(['utility', 'substation_name']).ngroups} substations "
          f"(reverse flow => net-of-BTM evidence)")
    print(f"negative max_load cells: {len(neg_max):,}")
    return {
        "missing": missing,
        "dataless_subs": dataless_subs,
        "half_missing": half_missing,
        "inverted": inverted,
        "zero_width": zw,
        "zw_per_sub": zw_per_sub,
        "zw_sub_size": aff_size,
        "fleet_size": fleet_size,
        "n_neg_min": len(neg_min),
        "n_neg_min_subs": neg_min.groupby(["utility", "substation_name"]).ngroups,
        "n_neg_max": len(neg_max),
        "n_cells": n_cells,
    }


def write_hygiene_report(h: dict) -> Path:
    """Human-readable anatomy of every hygiene issue + handling decision."""
    inv, zw, per_sub = h["inverted"], h["zero_width"], h["zw_per_sub"]
    full_flat = per_sub[per_sub == 288]
    partial = per_sub[per_sub < 288]
    lines = [
        "# Substation Envelope Hygiene Report",
        "",
        "Generated by `scripts/load_projection/approach2/stochastic_diagnostics.py`. Covers the",
        "three data-quality issues in `substation_load_profiles_clean.csv` and how the",
        "stochastic model handles each. Cell = one (substation, month, hour_pst) row.",
        "",
        "## 1. Inverted cells (min_load > max_load)",
        "",
        f"{len(inv)} cells ({len(inv) / h['n_cells']:.2%}) across "
        f"{inv.groupby(['utility', 'substation_name']).ngroups} substations. The 10th",
        "percentile exceeding the 90th is impossible; values appear transposed at the",
        "source (e.g. PGE CLAYTON, every hour of month 10). Affected cells:",
        "",
        "| utility | substation | months affected | cells |",
        "|---------|------------|-----------------|-------|",
    ]
    for (util, name), grp in inv.groupby(["utility", "substation_name"]):
        months = ",".join(str(m) for m in sorted(grp.month.unique()))
        lines.append(f"| {util} | {name} | {months} | {len(grp)} |")
    lines += [
        "",
        "**Handling: swap min and max.** Swapped values are plausible envelopes.",
        "Full row list in `diagnostic_hygiene.csv`.",
        "",
        "## 2. Zero-width cells (min_load == max_load, sigma = 0)",
        "",
        f"{len(zw):,} cells ({len(zw) / h['n_cells']:.2%}), **all SCE**, concentrated in",
        f"{len(per_sub)} substations:",
        "",
        f"- **{len(full_flat)} substations are fully flat**: all 288 cells report exactly",
        "  0.0 MW (e.g. Cima, Deep Springs, Iron Mt. (Sce), Mountain Pass A, Blythe",
        "  (Walc), Harper Lake, Edwards, George A.f.b.). Names indicate remote desert",
        "  sites, generation tie points, inter-utility interchange, and decommissioned",
        "  or military facilities - substations with no distribution load to report.",
        f"- **{len(partial)} substations are partially flat** with tiny nonzero values",
        f"  elsewhere; the {int((zw.min_load > 0).sum())} nonzero zero-width cells are all",
        f"  <= {zw.min_load.max():.2f} MW.",
        "",
        f"Affected substations average {h['zw_sub_size'].mean():.2f} MW mean load",
        f"(largest {h['zw_sub_size'].max():.2f} MW) vs a fleet mean of",
        f"{h['fleet_size'].mean():.1f} MW. Their combined mean load is",
        f"{h['zw_sub_size'].sum():.1f} MW out of a fleet total of",
        f"{h['fleet_size'].sum():,.0f} MW (~{h['zw_sub_size'].sum() / h['fleet_size'].sum():.5%}).",
        "",
        "**Handling: treat as deterministic at the reported value (usually 0 MW).**",
        "These are not data gaps - the utility reports no load, and there is no signal",
        "within the substation to interpolate from. Imputing a profile from 'similar'",
        "substations would fabricate load the utility says is absent, and the total at",
        "stake is negligible (< 0.001% of fleet load). A `zero_width` flag column marks",
        "them in the parameter table so any future imputation can target them.",
        "",
        "## 3. Missing cells (NaN quantiles)",
        "",
        f"{len(h['missing']):,} cells have min_load and/or max_load NaN:",
        "",
        f"- **{len(h['dataless_subs'])} SCE substations have no data at all** (all 288",
        f"  cells NaN): {', '.join(n for _, n in h['dataless_subs'].index)}.",
        f"- **{len(h['half_missing'])} half-missing cells** (one quantile present, the",
        "  other NaN): PGE BROWNS VALLEY (months 2-3) and PGE SOQUEL (months 3, 5, 10),",
        "  whole months at a time.",
        "- 3 further substations (PGE BOLINAS, PGE SOQUEL, SCE Bedford) each lack one",
        "  month entirely (no rows; 72 cells).",
        "",
        "**Handling: flagged `missing`, parameters NaN, excluded from all cell sums and",
        "from generation output** (the substation simply has no value in those cells).",
        "No imputation. Note a distribution cannot be fit from a single quantile, so",
        "half-missing cells cannot be salvaged without assumptions.",
        "",
        "## 4. Negative cells",
        "",
        f"{h['n_neg_min']:,} cells across {h['n_neg_min_subs']} substations have negative",
        f"min_load; {h['n_neg_max']} cells have negative max_load. These are real reverse",
        "flows (BTM export exceeding local load) under the net-of-BTM interpretation.",
        "",
        "**Handling: keep as-is; no truncation** (project decision 2026-07-16).",
        "",
        "## Affected zero-width substations",
        "",
        "| utility | substation | zero-width cells | mean load (MW) |",
        "|---------|------------|------------------|----------------|",
    ]
    for (util, name), n in per_sub.items():
        lines.append(f"| {util} | {name} | {n} | {h['zw_sub_size'].loc[(util, name)]:.3f} |")
    path = OUT_DIR / "hygiene_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def fit_marginals(sub: pd.DataFrame) -> pd.DataFrame:
    """Closed-form two-parameter fits per cell. Two quantiles exactly identify
    each family - there is no estimation freedom at the marginal level.

    Normal:  P(L <= q10) = 0.10 and P(L <= q90) = 0.90 give
             mu = (q10 + q90)/2,  sigma = (q90 - q10) / (2 * Z90)
    Uniform: q10 = a + 0.1*(b - a), q90 = a + 0.9*(b - a) give
             width = (q90 - q10)/0.8,  a = q10 - width/8,  b = q90 + width/8
    """
    sub = sub.copy()
    # inverted cells: swap so quantiles are ordered (see hygiene report section 1);
    # np.minimum/maximum propagate NaN so half-missing cells stay NaN rather than
    # collapsing to spurious zero-width cells
    lo = np.minimum(sub.min_load.values, sub.max_load.values)
    hi = np.maximum(sub.min_load.values, sub.max_load.values)
    sub["mu"] = (lo + hi) / 2
    sub["sigma"] = (hi - lo) / (2 * Z90)
    sub["unif_width"] = (hi - lo) / 0.8
    sub["unif_a"] = lo - sub["unif_width"] / 8
    sub["unif_b"] = hi + sub["unif_width"] / 8
    sub["zero_width"] = sub.sigma == 0

    print("\n" + "=" * 70)
    print("SECTION 2 - MARGINAL FITS (closed form, per cell)")
    print("=" * 70)
    print("Example: same substation, contrasting cells (distributions already")
    print("differ by month and hour - nothing is pooled across cells):")
    example = sub[(sub.utility == "pge") & (sub.substation_name == "HOLLISTER")
                  & (sub.month.isin([1, 7])) & (sub.hour_pst.isin([9, 15, 19]))]
    print(example[["month", "hour_pst", "min_load", "max_load", "mu", "sigma"]]
          .to_string(index=False))
    return sub


def load_caiso_hourly() -> pd.DataFrame:
    """CAISO hourly demand from EIA-930, converted UTC hour-ending -> PST
    hour-beginning (subtract 9h), restricted to complete PST years."""
    eia = pd.read_csv(EIA_FILE, parse_dates=["datetime_utc"])
    c = eia[eia.ba_code == "CISO"].copy()
    c["dt_pst_hb"] = c.datetime_utc - pd.Timedelta(hours=9)
    c["year"] = c.dt_pst_hb.dt.year
    c["month"] = c.dt_pst_hb.dt.month
    c["hour_pst"] = c.dt_pst_hb.dt.hour
    y0, y1 = FULL_YEAR_RANGE
    return c[(c.year >= y0) & (c.year <= y1)].dropna(subset=["demand_mwh"])


def caiso_cell_stats(c: pd.DataFrame, year_start: int, year_end: int) -> pd.DataFrame:
    w = c[(c.year >= year_start) & (c.year <= year_end)]
    cells = w.groupby(["month", "hour_pst"])["demand_mwh"].agg(
        caiso_mean="mean", caiso_std="std", caiso_n="count",
        caiso_q10=lambda s: s.quantile(0.10), caiso_q90=lambda s: s.quantile(0.90),
    )
    print("\n" + "=" * 70)
    print(f"SECTION 3 - CAISO CELLS (EIA-930 CISO, PST years {year_start}-{year_end})")
    print("=" * 70)
    print(f"hours used: {len(w):,}   obs per cell: "
          f"{cells.caiso_n.min()}-{cells.caiso_n.max()}")
    return cells


def caiso_year_decomposition(c: pd.DataFrame, year_start: int, year_end: int) -> None:
    """Within-cell variance decomposition over the full record vs the selected
    window: total = between-year (trend + interannual level) + within-year
    (day-to-day weather). Shows how sensitive the cell std - and hence rho - is
    to the choice of estimation window."""

    def decomp(df: pd.DataFrame) -> pd.DataFrame:
        total = df.groupby(["month", "hour_pst"])["demand_mwh"].std()
        year_mean = df.groupby(["month", "hour_pst", "year"])["demand_mwh"].transform("mean")
        within = (df.demand_mwh - year_mean).groupby(
            [df.month, df.hour_pst]).std()
        between = df.groupby(["month", "hour_pst", "year"])["demand_mwh"].mean().groupby(
            ["month", "hour_pst"]).std()
        return pd.DataFrame({"total_std": total, "between_year_std": between,
                             "within_year_std": within})

    y0, y1 = FULL_YEAR_RANGE
    full = decomp(c)
    window = decomp(c[(c.year >= year_start) & (c.year <= year_end)])

    print("\n" + "=" * 70)
    print("SECTION 3b - WITHIN-CELL VARIANCE DECOMPOSITION ACROSS YEARS")
    print("=" * 70)
    print("annual mean CAISO demand (MW) - net demand is nearly flat, no strong trend:")
    print(c.groupby("year")["demand_mwh"].mean().round(0).to_string())
    print(f"\nmedian across 288 cells (MW):")
    tbl = pd.DataFrame({
        f"{y0}-{y1} (full, ~{len(c) // 288} obs/cell)": full.median(),
        f"{year_start}-{year_end} (selected)": window.median(),
    })
    print(tbl.round(0).to_string())
    ratio = full.total_std / window.total_std
    print(f"\ncell-std ratio full/selected: median {ratio.median():.2f}, "
          f"range {ratio.min():.2f}-{ratio.max():.2f}")
    print("within-year (weather) std is nearly identical across windows; the windows")
    print("differ only in how much between-year variation (interannual weather +")
    print("duck-curve shape drift) is folded into the cell std.")


def build_cell_table(sub: pd.DataFrame, caiso: pd.DataFrame,
                     f_values: list[float]) -> pd.DataFrame:
    """Per-cell system aggregates joined to CAISO cell stats, plus the implied
    scaling factor, independence gap, and required common-factor share rho."""
    agg = sub.groupby(["month", "hour_pst"]).agg(
        sum_mu=("mu", "sum"),
        sum_sigma=("sigma", "sum"),
        sum_var=("sigma", lambda s: (s ** 2).sum()),
        n_subs=("mu", "size"),
    )
    agg["std_indep"] = np.sqrt(agg.sum_var)  # std of the sum if substations independent
    cells = agg.join(caiso)
    cells["implied_f"] = cells.sum_mu / cells.caiso_mean

    for f in f_values:
        tag = f"{f:.3f}".rstrip("0").rstrip(".")
        # how many times larger the observed f*CAISO spread is vs independent sampling
        cells[f"indep_gap_f{tag}"] = f * cells.caiso_std / cells.std_indep
        # common-factor share needed so Var(E[total | factor]) = Var(f*CAISO within cell):
        #   sqrt(rho) * sum_sigma = f * caiso_std   =>   rho = (f*caiso_std / sum_sigma)^2
        # rho > 1 would mean even perfectly comonotonic substations cannot swing enough.
        cells[f"rho_f{tag}"] = (f * cells.caiso_std / cells.sum_sigma) ** 2

    print("\n" + "=" * 70)
    print("SECTION 4 - INDEPENDENCE GAP AND REQUIRED COMMON-FACTOR SHARE, PER CELL")
    print("=" * 70)
    print("std of substation sum under INDEPENDENT sampling, sqrt(sum sigma^2) [MW]:")
    print(cells.std_indep.describe().round(0).to_string())
    print("\ncomonotonic ceiling, sum of sigmas [MW]:")
    print(cells.sum_sigma.describe().round(0).to_string())
    print("\nobserved CAISO within-cell std [MW]:")
    print(cells.caiso_std.describe().round(0).to_string())
    for f in f_values:
        tag = f"{f:.3f}".rstrip("0").rstrip(".")
        rho = cells[f"rho_f{tag}"]
        print(f"\nf = {f}:  indep gap (x too small): "
              f"median {cells[f'indep_gap_f{tag}'].median():.1f}, "
              f"max {cells[f'indep_gap_f{tag}'].max():.1f}")
        print(f"          required rho(m,h): median {rho.median():.3f}, "
              f"range [{rho.min():.3f}, {rho.max():.3f}], "
              f"infeasible cells (rho > 1): {(rho > 1).sum()}")
    return cells


def implied_f_report(cells: pd.DataFrame) -> None:
    print("\n" + "=" * 70)
    print("SECTION 5 - IMPLIED SCALING FACTOR f = sum(mu) / CAISO cell mean")
    print("=" * 70)
    print(cells.implied_f.describe().round(3).to_string())
    by_hour = cells.groupby("hour_pst")["implied_f"].mean()
    print("\nby hour (mean over months):")
    print(by_hour.round(3).to_string())
    by_month = cells.groupby("month")["implied_f"].mean()
    print("\nby month (mean over hours):")
    print(by_month.round(3).to_string())


def prob_negative_report(sub: pd.DataFrame) -> None:
    print("\n" + "=" * 70)
    print("SECTION 6 - NEGATIVE-LOAD MASS UNDER NORMAL MARGINALS (not truncated)")
    print("=" * 70)
    pos_sigma = sub[sub.sigma > 0]
    z0 = pos_sigma.mu / pos_sigma.sigma  # P(L < 0) = Phi(-z0)
    from scipy.stats import norm as _norm
    p_neg = _norm.cdf(-z0)
    print(f"cells with P(L < 0) > 1%:  {(p_neg > 0.01).sum():,} "
          f"({(p_neg > 0.01).mean():.1%} of nonzero-sigma cells)")
    print(f"cells with P(L < 0) > 10%: {(p_neg > 0.10).sum():,}")
    print("(negatives are physically real reverse flows under the net interpretation;")
    print(" per project decision these are NOT truncated)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--year-start", type=int, default=2015)
    ap.add_argument("--year-end", type=int, default=2025)
    ap.add_argument("--f", type=str, default="0.70,0.775,0.85")
    args = ap.parse_args()
    f_values = [float(x) for x in args.f.split(",")]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sub = load_substation_cells()
    hygiene = hygiene_checks(sub)
    sub = fit_marginals(sub)
    caiso_hourly = load_caiso_hourly()
    caiso_cells = caiso_cell_stats(caiso_hourly, args.year_start, args.year_end)
    caiso_year_decomposition(caiso_hourly, args.year_start, args.year_end)
    cells = build_cell_table(sub, caiso_cells, f_values)
    implied_f_report(cells)
    prob_negative_report(sub)

    cells_path = OUT_DIR / "diagnostic_cells.csv"
    cells.round(6).to_csv(cells_path)
    hygiene_path = OUT_DIR / "diagnostic_hygiene.csv"
    hygiene["inverted"].to_csv(hygiene_path, index=False)
    report_path = write_hygiene_report(hygiene)
    print(f"\nwrote {cells_path.relative_to(ROOT)}")
    print(f"wrote {hygiene_path.relative_to(ROOT)}")
    print(f"wrote {report_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
