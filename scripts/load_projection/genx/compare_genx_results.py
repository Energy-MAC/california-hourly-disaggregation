"""Compare GenX results across demand-allocation runs (the output-side half).

Pairs with compare_genx_demand.py: that one measures how differently the runs
place LOAD, this one measures how differently GenX then OPERATES the system.
Because every run holds statewide hourly demand exactly fixed and the fleet is
fixed too (New_Build = 0 for all 2,171 resources, NetworkExpansion = 0), any
difference reported here is caused by load location alone.

What "x% different" can mean here, in increasing order of how much it commits
to (see docs/genx_comparison.md for the full argument):

  1. COST     total operating cost of the optimal dispatch, run vs control.
              The headline scalar, and the closest available analogue of the
              RMML error metric in Glista et al. (2027) Table 2.
  2. PHYSICAL how differently the system is operated: generation redispatch,
              storage charge/discharge, flows, prices.  Reported as a movement
              fraction (half the L1 distance over total) and a normalized RMSE,
              the same pair of metrics used on the demand side so input and
              output divergence are directly comparable.
  3. RELIABILITY  load shed and curtailment -- the analogue of the EUE / LOLH
              columns in that paper's Table 6.

Note on Table 2: its error column is NOT reproducible here, and the reason is
structural rather than a missing input.  RMML measures the penalty for carrying
an INVESTMENT portfolio from an approximate model into the true one; these
cases are production-cost runs with no investment variables, and both runs live
on the same 8,870-bus network rather than on a reduced one, so there is no
mapping step to perform.  The cost comparison in (1) is the honest analogue.
A true cross-evaluation is possible via unit commitment (--cross-eval explains
the design) but needs a second GenX solve, not just result parsing.

Because GenX's result layout varies across versions, run --inspect FIRST on a
real result folder: it prints each file's shape and header block so the parsing
below can be confirmed (or corrected) against actual output rather than assumed.

CLI parameters
  --runs        comma-separated materialized run tags (default: all present)
  --baseline    run tag to compare against (default genx__control)
  --cases       comma-separated case subset, e.g. p1,p8 (default: all found)
  --results-dir results subfolder name inside a case (default: auto-detect)
  --root        default genx/scenarios_rescaled
  --inspect     print result-file structure and exit (no comparison)
  --no-figures  metrics only

Outputs (data/checks/genx_rescale/)
  results_comparison_summary.csv   per (run, case) headline metrics
  results_cost_breakdown.csv       per (run, case) cost component deltas
Figures (data/figures/genx/)
  results_cost_delta.png           cost difference vs control, by case
  results_divergence.png           redispatch / price / shed divergence by case

Usage
  python scripts/load_projection/genx/compare_genx_results.py --inspect
  python scripts/load_projection/genx/compare_genx_results.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
from genx_demand_io import GENX_ROOT, scenario_seasons  # noqa: E402

DEFAULT_ROOT = GENX_ROOT / "scenarios_rescaled"
OUT_DIR = ROOT / "data/checks/genx_rescale"
FIG_DIR = ROOT / "data/figures/genx"
BASELINE_DEFAULT = "genx__control"

SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
C_POS, C_NEG = "#2a78d6", "#e34948"
INK, INK_2, MUTED, GRID = "#0b0b0b", "#52514e", "#898781", "#e1e0d9"

# GenX writes results into one of these; auto-detected per case
RESULTS_DIRNAMES = ("results", "Results")
# time-series result files, by the quantity they carry
TS_FILES = {
    "power": "power.csv",
    "charge": "charge.csv",
    "prices": "prices.csv",
    "flow": "flow.csv",
    "curtail": "curtail.csv",
    "nse": "nse.csv",
}


def find_results(case_dir: Path, override: str | None) -> Path | None:
    if override:
        p = case_dir / override
        return p if p.exists() else None
    for name in RESULTS_DIRNAMES:
        p = case_dir / name
        if p.exists():
            return p
    return None


def read_timeseries(path: Path) -> pd.DataFrame | None:
    """Extract the t1..tT block from a GenX result file.

    GenX prefixes its time-series tables with a few label rows (Resource, Zone,
    AnnualSum) and labels each timestep row `t1`, `t2`, ....  Rather than
    hard-coding a row count -- which differs by file and by GenX version -- this
    keeps only rows whose first column matches `t<digits>` and coerces the rest
    to float.  Files that carry no such rows return None and are skipped.
    """
    if not path.exists():
        return None
    raw = pd.read_csv(path, dtype=str, keep_default_na=False, low_memory=False)
    label = raw.columns[0]
    mask = raw[label].str.fullmatch(r"t\d+")
    if not mask.any():
        return None
    block = raw.loc[mask].set_index(label)
    block.index = block.index.str[1:].astype(int)
    return block.apply(pd.to_numeric, errors="coerce").fillna(0.0).sort_index()


def read_costs(path: Path) -> pd.Series | None:
    """costs.csv -> {cost component: total}."""
    if not path.exists():
        return None
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    label, value = raw.columns[0], raw.columns[1]
    s = pd.Series(pd.to_numeric(raw[value], errors="coerce").values,
                  index=raw[label].values).dropna()
    return s


def movement_pct(a: np.ndarray, b: np.ndarray) -> float:
    """Half the L1 distance as a share of total magnitude -- see the demand script.

    Uses absolute totals so it stays meaningful for signed quantities (flows,
    storage charge) where a plain sum could cancel to near zero.
    """
    denom = np.abs(a).sum()
    return float(100 * 0.5 * np.abs(a - b).sum() / denom) if denom > 0 else np.nan


def nrmse_pct(a: np.ndarray, b: np.ndarray) -> float:
    scale = np.abs(a).mean()
    return float(100 * np.sqrt(((a - b) ** 2).mean()) / scale) if scale > 0 else np.nan


def compare_case(base_res: Path, run_res: Path) -> dict:
    m: dict = {}

    cb, cr = read_costs(base_res / "costs.csv"), read_costs(run_res / "costs.csv")
    if cb is not None and cr is not None:
        for comp in sorted(set(cb.index) & set(cr.index)):
            base_v = cb[comp]
            m[f"cost_{comp}_base"] = float(base_v)
            m[f"cost_{comp}_run"] = float(cr[comp])
            m[f"cost_{comp}_delta_pct"] = (
                float(100 * (cr[comp] - base_v) / base_v) if base_v else np.nan)

    for key, fname in TS_FILES.items():
        a = read_timeseries(base_res / fname)
        b = read_timeseries(run_res / fname)
        if a is None or b is None:
            continue
        cols = [c for c in a.columns if c in b.columns]
        if not cols:
            continue
        av, bv = a[cols].to_numpy(float), b[cols].to_numpy(float)
        if av.shape != bv.shape:
            continue
        m[f"{key}_movement_pct"] = movement_pct(av, bv)
        m[f"{key}_nrmse_pct"] = nrmse_pct(av, bv)
        m[f"{key}_total_base"] = float(av.sum())
        m[f"{key}_total_run"] = float(bv.sum())
        if key == "prices":  # price levels matter in their own units
            m["price_mean_abs_diff"] = float(np.abs(av - bv).mean())
            m["price_mean_base"] = float(av.mean())
        if key == "nse":  # the paper's EUE / LOLH analogues
            m["eue_base_mwh"] = float(av.sum())
            m["eue_run_mwh"] = float(bv.sum())
            m["lolh_base"] = int((av.sum(axis=1) > 1e-6).sum())
            m["lolh_run"] = int((bv.sum(axis=1) > 1e-6).sum())
    return m


def inspect(root: Path, runs: list[str], results_dir: str | None) -> None:
    print("Inspecting GenX result layout (run this on real output before trusting "
          "the parsing in this script).\n")
    found_any = False
    for run in runs:
        for case_dir in sorted((root / run).glob("p*")):
            res = find_results(case_dir, results_dir)
            if res is None:
                continue
            found_any = True
            print(f"=== {run}/{case_dir.name}/{res.name} ===")
            for f in sorted(res.glob("*.csv")):
                raw = pd.read_csv(f, dtype=str, keep_default_na=False,
                                  nrows=6, low_memory=False)
                print(f"  {f.name:28s} cols={raw.shape[1]:>6}  "
                      f"first col head={list(raw[raw.columns[0]])[:5]}")
            print()
            break  # one case per run is enough to show the layout
    if not found_any:
        print("No result folders found. GenX has not been run on these cases yet.\n"
              "Expected a 'results/' (or 'Results/') folder inside a case, e.g.\n"
              f"  {root}/<run_tag>/p1/results/")


def fig_cost(summary: pd.DataFrame) -> None:
    col = next((c for c in summary.columns if c.endswith("_delta_pct")
                and "cTotal" in c), None)
    if col is None or summary[col].isna().all():
        return
    fig, ax = plt.subplots(figsize=(9.5, 4.6))
    runs = summary.run.unique()
    width = 0.8 / len(runs)
    cases = sorted(summary.case.unique(), key=lambda c: int(c[1:]))
    x = np.arange(len(cases))
    for i, run in enumerate(runs):
        d = summary[summary.run == run].set_index("case").reindex(cases)
        ax.bar(x + i * width - 0.4 + width / 2, d[col], width * 0.9,
               label=run, color=SERIES[i % len(SERIES)])
    ax.axhline(0, color=MUTED, lw=1.0)
    ax.set_xticks(x); ax.set_xticklabels(cases, fontsize=7)
    ax.set_ylabel("total operating cost vs control (%)", color=INK_2, fontsize=9)
    ax.set_xlabel("GenX case", color=INK_2, fontsize=9)
    ax.set_title("Cost of operating the same fleet under a different load allocation\n"
                 "positive = the rescaled allocation is more expensive to serve",
                 fontsize=11, color=INK)
    leg = ax.legend(fontsize=8, frameon=False)
    for t in leg.get_texts():
        t.set_color(INK_2)
    ax.grid(alpha=0.3, color=GRID, lw=0.6, axis="y")
    ax.tick_params(colors=MUTED, labelsize=8)
    for s in ax.spines.values():
        s.set_color(GRID)
    fig.tight_layout()
    out = FIG_DIR / "results_cost_delta.png"
    fig.savefig(out, dpi=160, facecolor="white")
    plt.close(fig)
    print(f"wrote {out.relative_to(ROOT)}")


def fig_divergence(summary: pd.DataFrame) -> None:
    metrics = [c for c in ("power_movement_pct", "charge_movement_pct",
                           "flow_movement_pct", "prices_nrmse_pct")
               if c in summary.columns and not summary[c].isna().all()]
    if not metrics:
        return
    fig, ax = plt.subplots(figsize=(8.5, 4.6))
    agg = summary.groupby("run")[metrics].mean()
    x = np.arange(len(metrics))
    width = 0.8 / len(agg)
    for i, (run, row) in enumerate(agg.iterrows()):
        ax.bar(x + i * width - 0.4 + width / 2, row.values, width * 0.9,
               label=run, color=SERIES[i % len(SERIES)])
    ax.set_xticks(x)
    ax.set_xticklabels([m.replace("_", "\n") for m in metrics], fontsize=8)
    ax.set_ylabel("divergence vs control (%)", color=INK_2, fontsize=9)
    ax.set_title("How differently the system is operated\n"
                 "mean over cases; movement = share of the quantity that shifts",
                 fontsize=11, color=INK)
    leg = ax.legend(fontsize=8, frameon=False)
    for t in leg.get_texts():
        t.set_color(INK_2)
    ax.grid(alpha=0.3, color=GRID, lw=0.6, axis="y")
    ax.tick_params(colors=MUTED, labelsize=8)
    for s in ax.spines.values():
        s.set_color(GRID)
    fig.tight_layout()
    out = FIG_DIR / "results_divergence.png"
    fig.savefig(out, dpi=160, facecolor="white")
    plt.close(fig)
    print(f"wrote {out.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", default=None)
    ap.add_argument("--baseline", default=BASELINE_DEFAULT)
    ap.add_argument("--cases", default=None)
    ap.add_argument("--results-dir", default=None)
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--no-figures", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        raise FileNotFoundError(
            f"{root} not found. Materialize runs first:\n"
            f"  python scripts/load_projection/genx/materialize_genx_cases.py "
            f"--run-tag <tag> --enable-outputs")
    available = sorted(p.name for p in root.iterdir()
                       if p.is_dir() and (p / "p1").is_dir())
    runs = args.runs.split(",") if args.runs else [r for r in available
                                                   if r != args.baseline]

    if args.inspect:
        inspect(root, [args.baseline] + runs, args.results_dir)
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    tree = scenario_seasons()
    cases = args.cases.split(",") if args.cases else tree.cases

    rows, skipped = [], []
    for run in runs:
        for case in cases:
            base_res = find_results(root / args.baseline / case, args.results_dir)
            run_res = find_results(root / run / case, args.results_dir)
            if base_res is None or run_res is None:
                skipped.append(f"{run}/{case}")
                continue
            m = compare_case(base_res, run_res)
            if not m:
                skipped.append(f"{run}/{case} (no comparable files)")
                continue
            rows.append({"run": run, "baseline": args.baseline, "case": case,
                         "season": tree.case_season[case], **m})

    if not rows:
        print(f"No comparable results found under {root}.")
        print(f"  {len(skipped)} run/case pair(s) had no results directory.")
        print("\nGenX has not been run yet. Once it has, start with:\n"
              "  python scripts/load_projection/genx/compare_genx_results.py --inspect")
        return

    summary = pd.DataFrame(rows)
    summary.round(6).to_csv(OUT_DIR / "results_comparison_summary.csv", index=False)
    cost_cols = ["run", "case", "season"] + [c for c in summary.columns
                                             if c.startswith("cost_")]
    summary[cost_cols].round(4).to_csv(
        OUT_DIR / "results_cost_breakdown.csv", index=False)

    head = [c for c in ("cost_cTotal_delta_pct", "power_movement_pct",
                        "charge_movement_pct", "prices_nrmse_pct",
                        "eue_run_mwh", "lolh_run") if c in summary.columns]
    print("\n=== mean over cases ===")
    print(summary.groupby("run")[head].mean().round(3).to_string())
    if skipped:
        print(f"\nskipped {len(skipped)} run/case pair(s) with no results")

    if not args.no_figures:
        fig_cost(summary)
        fig_divergence(summary)
    print(f"\nwrote {(OUT_DIR / 'results_comparison_summary.csv').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
