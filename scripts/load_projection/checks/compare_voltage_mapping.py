"""Sensitivity check: how much does --voltage-mode restrict change the nodal
mapping vs proximity-only?

Reads both of map_loads_to_nodes.py's outputs -- substation_node_map.csv
(--voltage-mode off) and substation_node_map__voltrestrict.csv
(--voltage-mode restrict) -- and compares the resulting per-node load. Each
real substation's magnitude is its mean max_load across all (month, hour)
cells in substation_load_profiles_clean.csv (a load-shape-agnostic proxy for
relative size, not an annual energy total); synthetic ReEDS substations are
excluded since assign_synthetic() never uses voltage and is identical under
both modes. Magnitude x share is summed per node under each mapping, and the
two node-load vectors are compared.

A high Spearman r with a non-trivial share of load mass reassigned is the
expected, defensible result: proximity alone gets most assignments right, and
voltage correction targets a specific, quantifiable minority.

Inputs
------
  data/processed/load_projection/nodal/CATS/substation_node_map.csv
  data/processed/load_projection/nodal/CATS/substation_node_map__voltrestrict.csv
      (run map_loads_to_nodes.py --system CATS, then again with
      --voltage-mode restrict, if either is missing)
  data/processed/substations/substation_load_profiles_clean.csv

Outputs (data/checks/voltage_mapping_comparison/)
----------------------------------------------------
  node_load_comparison.csv     per node: load-proxy under each mapping
  substation_reassignment.csv  per substation: node-set under each mapping,
                               magnitude-weighted mass moved, avg distance
                               under each mapping, restrict-mode method
  summary.csv                  Spearman r/p and the support metrics below

Figure (data/figures/load_projection/voltage/)
------------------------------------------------
  voltage_mapping_comparison.png   per-node load scatter (log-log, r
                                   annotated), per-utility load-moved bar,
                                   added-distance histogram

Usage
-----
  python scripts/load_projection/checks/compare_voltage_mapping.py
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[3]
NODAL_DIR = ROOT / "data/processed/load_projection/nodal/CATS"
PROX_FILE = NODAL_DIR / "substation_node_map.csv"
VOLT_FILE = NODAL_DIR / "substation_node_map__voltrestrict.csv"
PROFILE_FILE = ROOT / "data/processed/substations/substation_load_profiles_clean.csv"
OUT_DIR = ROOT / "data/checks/voltage_mapping_comparison"
FIG_DIR = ROOT / "data/figures/load_projection/voltage"


def load_magnitudes() -> pd.Series:
    """(utility, substation_name) -> mean max_load across all cells."""
    prof = pd.read_csv(PROFILE_FILE, usecols=["utility", "substation_name", "max_load"])
    return prof.groupby(["utility", "substation_name"])["max_load"].mean()


def node_loads(mapping: pd.DataFrame, magnitude: pd.Series) -> pd.Series:
    m = mapping[~mapping.is_synthetic].copy()
    m["magnitude"] = m.set_index(["utility", "substation_name"]).index.map(magnitude)
    m = m.dropna(subset=["magnitude"])
    m["load"] = m.magnitude * m.share
    return m.groupby("node")["load"].sum()


def substation_reassignment(prox: pd.DataFrame, volt: pd.DataFrame,
                            magnitude: pd.Series) -> pd.DataFrame:
    prox_r = prox[~prox.is_synthetic]
    volt_r = volt[~volt.is_synthetic]

    rows = []
    keys = sorted(set(zip(prox_r.utility, prox_r.substation_name)) |
                  set(zip(volt_r.utility, volt_r.substation_name)))
    p_grp = {k: g for k, g in prox_r.groupby(["utility", "substation_name"])}
    v_grp = {k: g for k, g in volt_r.groupby(["utility", "substation_name"])}

    for key in keys:
        util, name = key
        mag = magnitude.get(key, np.nan)
        pg = p_grp.get(key)
        vg = v_grp.get(key)
        if pg is None or vg is None or pd.isna(mag):
            continue
        p_share = pg.set_index("node")["share"]
        v_share = vg.set_index("node")["share"]
        common_nodes = p_share.index.intersection(v_share.index)
        stayed_share = sum(min(p_share[n], v_share[n]) for n in common_nodes)
        reassigned = set(p_share.index) != set(v_share.index)
        avg_dist_prox = float((pg.dist_km * pg.share).sum())
        avg_dist_volt = float((vg.dist_km * vg.share).sum())
        rows.append({
            "utility": util, "substation_name": name, "magnitude": mag,
            "n_nodes_prox": len(p_share), "n_nodes_volt": len(v_share),
            "reassigned": reassigned,
            "mass_moved_frac": 1.0 - stayed_share,
            "mass_moved_mw": mag * (1.0 - stayed_share),
            "avg_dist_prox_km": avg_dist_prox, "avg_dist_volt_km": avg_dist_volt,
            "added_dist_km": avg_dist_volt - avg_dist_prox,
            "assignment_method_volt": vg.assignment_method.iat[0],
        })
    return pd.DataFrame(rows)


def plot_comparison(node_cmp: pd.DataFrame, sub_reassign: pd.DataFrame, r: float) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))

    ax = axes[0]
    pos = node_cmp[(node_cmp.load_proxy_prox > 0) & (node_cmp.load_proxy_volt > 0)]
    ax.scatter(pos.load_proxy_prox, pos.load_proxy_volt, s=10, alpha=0.5)
    lims = [pos[["load_proxy_prox", "load_proxy_volt"]].min().min(),
           pos[["load_proxy_prox", "load_proxy_volt"]].max().max()]
    ax.plot(lims, lims, color="grey", linestyle="--", linewidth=1)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("per-node load proxy, proximity-only [MW]")
    ax.set_ylabel("per-node load proxy, voltage-restricted [MW]")
    ax.set_title(f"per-node load, proximity vs voltage-restricted\nSpearman r = {r:.4f}")

    ax = axes[1]
    by_util = sub_reassign.groupby("utility").mass_moved_mw.sum() / sub_reassign.groupby("utility").magnitude.sum()
    by_util.plot.bar(ax=ax, color="#d95f02")
    ax.set_ylabel("fraction of load mass reassigned")
    ax.set_title("load mass reassigned, by utility")
    ax.tick_params(axis="x", rotation=0)

    ax = axes[2]
    moved = sub_reassign[sub_reassign.reassigned]
    ax.hist(moved.added_dist_km, bins=30, color="#7570b3")
    ax.axvline(0, color="grey", linestyle="--", linewidth=1)
    ax.set_xlabel("added distance under voltage-restrict [km]\n(share-weighted avg, reassigned substations only)")
    ax.set_ylabel("count")
    ax.set_title("distance cost of voltage restriction")

    fig.tight_layout()
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    fig_path = FIG_DIR / "voltage_mapping_comparison.png"
    fig.savefig(fig_path, dpi=150)
    print(f"wrote {fig_path.relative_to(ROOT)}")


def main() -> None:
    for f in (PROX_FILE, VOLT_FILE):
        if not f.exists():
            raise SystemExit(f"missing {f.relative_to(ROOT)} -- run "
                             "map_loads_to_nodes.py --system CATS "
                             f"{'--voltage-mode restrict ' if f == VOLT_FILE else ''}first")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prox = pd.read_csv(PROX_FILE)
    volt = pd.read_csv(VOLT_FILE)
    magnitude = load_magnitudes()

    load_prox = node_loads(prox, magnitude)
    load_volt = node_loads(volt, magnitude)
    all_nodes = sorted(set(load_prox.index) | set(load_volt.index))
    node_cmp = pd.DataFrame({
        "node": all_nodes,
        "load_proxy_prox": load_prox.reindex(all_nodes, fill_value=0.0).values,
        "load_proxy_volt": load_volt.reindex(all_nodes, fill_value=0.0).values,
    })
    node_cmp.to_csv(OUT_DIR / "node_load_comparison.csv", index=False)

    r, p = spearmanr(node_cmp.load_proxy_prox, node_cmp.load_proxy_volt)

    sub_reassign = substation_reassignment(prox, volt, magnitude)
    sub_reassign.to_csv(OUT_DIR / "substation_reassignment.csv", index=False)

    total_mag = sub_reassign.magnitude.sum()
    frac_reassigned = sub_reassign.reassigned.mean()
    frac_mass_moved = sub_reassign.mass_moved_mw.sum() / total_mag
    moved = sub_reassign[sub_reassign.reassigned]

    fallback = sub_reassign[sub_reassign.assignment_method_volt.isin(
        ["nearest_voltage_fallback", "nearest_novoltage"])]
    fallback_frac_count = len(fallback) / len(sub_reassign)
    fallback_frac_mass = fallback.magnitude.sum() / total_mag

    prox_match = pd.read_csv(ROOT / "data/checks/voltage_validation/substation_vs_node_kv.csv") \
        if (ROOT / "data/checks/voltage_validation/substation_vs_node_kv.csv").exists() else None
    volt_restricted = volt[(~volt.is_synthetic) & (volt.assignment_method == "nearest")]
    volt_match_rate = volt_restricted.voltage_match.astype(float).mean() if len(volt_restricted) else float("nan")

    summary = {
        "spearman_r": r, "spearman_p": p, "n_nodes": len(all_nodes),
        "frac_substations_reassigned": frac_reassigned,
        "frac_load_mass_reassigned": frac_mass_moved,
        "added_dist_km_median": moved.added_dist_km.median() if len(moved) else np.nan,
        "added_dist_km_p95": moved.added_dist_km.quantile(0.95) if len(moved) else np.nan,
        "fallback_frac_substations": fallback_frac_count,
        "fallback_frac_load_mass": fallback_frac_mass,
        "voltage_match_rate_proximity": prox_match.voltage_match.astype("boolean").mean() if prox_match is not None else np.nan,
        "voltage_match_rate_restricted_nonfallback": volt_match_rate,
    }
    pd.DataFrame([summary]).to_csv(OUT_DIR / "summary.csv", index=False)

    print("=" * 78)
    print("VOLTAGE-RESTRICTED vs PROXIMITY-ONLY MAPPING: sensitivity")
    print("=" * 78)
    print(f"per-node load Spearman r = {r:.4f}  (p = {p:.2e}, n_nodes = {len(all_nodes)})")
    print(f"substations reassigned (any node-set change): {frac_reassigned:.1%}")
    print(f"load mass reassigned to a different node: {frac_mass_moved:.1%}")
    if len(moved):
        print(f"added distance under restriction (reassigned only): "
              f"median {moved.added_dist_km.median():.2f} km, "
              f"p95 {moved.added_dist_km.quantile(0.95):.2f} km")
    print(f"fallback cases (no known voltage or no same-class node in range): "
          f"{fallback_frac_count:.1%} of substations, {fallback_frac_mass:.1%} of load mass")
    if prox_match is not None:
        print(f"voltage_match rate: proximity-only {summary['voltage_match_rate_proximity']:.1%} "
              f"-> voltage-restricted (non-fallback rows) {volt_match_rate:.1%}")

    plot_comparison(node_cmp, sub_reassign, r)

    print(f"\nWrote -> {OUT_DIR.relative_to(ROOT)}/")
    for f in ["node_load_comparison.csv", "substation_reassignment.csv", "summary.csv"]:
        print(f"    {f}")


if __name__ == "__main__":
    main()
