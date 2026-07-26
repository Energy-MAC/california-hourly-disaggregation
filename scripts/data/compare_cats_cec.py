"""
compare_cats_cec.py

Haversine nearest-neighbour join between the CEC Substation DataPull
(07/24/2026) and the CATS (California Aggregate Transmission System) bus
dataset. Exact mirror of compare_cats_basin.py's methodology, run against the
updated CEC reference instead of the 2022 DataBasin reference — see that
script's docstring for the original. The basin outputs are left untouched;
this writes to separate paths so both results can be compared side by side.

CEC dataset
-----------
  data/processed/substation_misc/ca_substations_cec.csv (via process_substations_cec.py)
  4,828 rows; type values: SUBSTATION (3,702), TAP (685), SUBSTATION ASSUMED (248),
  RISER (103), NOT AVAILABLE (75), DEAD END (15). All rows have coordinates.

CATS Bus types
--------------
  'Substation'  — 3,171 buses at named substations (primary comparison target)
  'AddedNode'   — 5,699 intermediate nodes on transmission segments

Join logic (identical to compare_cats_basin.py)
-------------------------------------------------
For each CATS Substation bus, find the nearest CEC record.
For each CEC record, find the nearest CATS Substation bus.
A pair is "matched" when the nearest-neighbour distance is <= MATCH_KM (2.0).

Note: the original compare_cats_basin.py/compare_cats_substations.py/
compare_cats_unfiltered.py hardcode CATS_FILE at the nonexistent
data/raw/PotentialData/CATS/CATS_buses.csv; this script uses the real path
(data/raw/CATS/CATS_buses.csv) instead.

Outputs
-------
  data/figures/substation_maps/cats_cec_comparison.png
  data/figures/substation_maps/cats_cec_distance_distribution.png
  data/checks/compare_cats_cec/cats_cec_join.csv
  data/checks/compare_cats_cec/cec_cats_join.csv
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.neighbors import BallTree

# -- Config -------------------------------------------------------------------

ROOT     = Path(__file__).resolve().parents[2]
CATS_FILE = ROOT / "data" / "raw" / "CATS" / "CATS_buses.csv"
CEC_FILE  = ROOT / "data" / "processed" / "substation_misc" / "ca_substations_cec.csv"
FIGS_DIR  = ROOT / "data" / "figures" / "substation_maps"
OUT_DIR   = ROOT / "data" / "checks" / "compare_cats_cec"

FIGS_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

MATCH_KM = 2.0
EARTH_KM = 6371.0

_CA_LON = (-124.5, -114.0)
_CA_LAT = (32.5, 42.0)


# -- Helpers --------------------------------------------------------------------

def nearest_join(query_lat, query_lon, ref_lat, ref_lon):
    ref_rad = np.column_stack([np.radians(ref_lat), np.radians(ref_lon)])
    query_rad = np.column_stack([np.radians(query_lat), np.radians(query_lon)])
    tree = BallTree(ref_rad, metric="haversine")
    dist_rad, idx = tree.query(query_rad, k=1)
    return idx.flatten(), dist_rad.flatten() * EARTH_KM


# -- Load data ------------------------------------------------------------------

def load_cats():
    df = pd.read_csv(CATS_FILE)
    df["Type"] = df["Type"].str.strip("'")
    cats_sub = df[df["Type"] == "Substation"].copy().reset_index(drop=True)
    cats_nodes = df[df["Type"] == "AddedNode"].copy().reset_index(drop=True)
    return cats_sub, cats_nodes


def load_cec():
    df = pd.read_csv(CEC_FILE)
    n_before = len(df)
    df = df[df["latitude"].notna() & df["longitude"].notna()].copy().reset_index(drop=True)
    if len(df) < n_before:
        print(f"  Dropped {n_before - len(df)} CEC rows with missing coordinates")
    return df


# -- Join ------------------------------------------------------------------------

def join_datasets(cats_sub: pd.DataFrame, cec: pd.DataFrame):
    idx_c, dist_c = nearest_join(
        cats_sub["Lat"].values, cats_sub["Lon"].values,
        cec["latitude"].values, cec["longitude"].values,
    )
    cats_joined = cats_sub.copy()
    cats_joined["nearest_cec_name"] = cec["name"].iloc[idx_c].values
    cats_joined["nearest_cec_owner"] = cec["owner_std"].iloc[idx_c].values
    cats_joined["nearest_cec_type"] = cec["type"].iloc[idx_c].values
    cats_joined["nearest_cec_lat"] = cec["latitude"].iloc[idx_c].values
    cats_joined["nearest_cec_lon"] = cec["longitude"].iloc[idx_c].values
    cats_joined["dist_km"] = dist_c
    cats_joined["matched"] = dist_c <= MATCH_KM

    idx_k, dist_k = nearest_join(
        cec["latitude"].values, cec["longitude"].values,
        cats_sub["Lat"].values, cats_sub["Lon"].values,
    )
    cec_joined = cec.copy()
    cec_joined["nearest_cats_id"] = cats_sub["bus_i"].iloc[idx_k].values
    cec_joined["nearest_cats_kv"] = cats_sub["kV"].iloc[idx_k].values
    cec_joined["nearest_cats_lat"] = cats_sub["Lat"].iloc[idx_k].values
    cec_joined["nearest_cats_lon"] = cats_sub["Lon"].iloc[idx_k].values
    cec_joined["cats_dist_km"] = dist_k
    cec_joined["cats_matched"] = dist_k <= MATCH_KM

    return cats_joined, cec_joined


# -- Summary -----------------------------------------------------------------------

def print_summary(cats_joined: pd.DataFrame, cec_joined: pd.DataFrame) -> None:
    n_cats = len(cats_joined)
    n_c_mat = cats_joined["matched"].sum()
    n_c_miss = n_cats - n_c_mat

    n_cec = len(cec_joined)
    n_b_mat = cec_joined["cats_matched"].sum()
    n_b_miss = n_cec - n_b_mat

    print(f"\n{'=' * 60}")
    print(f"CATS vs CEC 2026 — Distance Join (threshold {MATCH_KM} km)")
    print(f"{'=' * 60}")
    print(f"\nCATS 'Substation' buses ({n_cats:,} total):")
    print(f"  Matched to CEC          : {n_c_mat:,}  ({100 * n_c_mat / n_cats:.1f}%)")
    print(f"  Not in CEC              : {n_c_miss:,}  ({100 * n_c_miss / n_cats:.1f}%)")

    print(f"\nCEC records ({n_cec:,} total, all types):")
    print(f"  Matched to CATS         : {n_b_mat:,}  ({100 * n_b_mat / n_cec:.1f}%)")
    print(f"  Not in CATS             : {n_b_miss:,}  ({100 * n_b_miss / n_cec:.1f}%)")

    print(f"\nDistance distribution (CATS -> nearest CEC, km):")
    print(cats_joined["dist_km"].describe(
        percentiles=[.25, .5, .75, .9, .95, .99]).round(2).to_string())

    print(f"\nUnmatched CATS buses by kV:")
    miss_cats = cats_joined[~cats_joined["matched"]]
    print(miss_cats["kV"].value_counts().sort_index().to_string())

    print(f"\nMatched CATS buses by nearest CEC type:")
    print(cats_joined[cats_joined["matched"]]["nearest_cec_type"].value_counts().to_string())

    print(f"\nUnmatched CEC records by type:")
    miss_cec = cec_joined[~cec_joined["cats_matched"]]
    print(miss_cec["type"].value_counts().to_string())

    print(f"\nUnmatched CEC records by owner_std:")
    print(miss_cec["owner_std"].value_counts().head(15).to_string())

    sub_only = cec_joined[cec_joined["type"] == "SUBSTATION"]
    n_s_mat = sub_only["cats_matched"].sum()
    print(f"\nCEC SUBSTATION type only ({len(sub_only):,} records):")
    print(f"  Matched to CATS         : {n_s_mat:,}  ({100 * n_s_mat / len(sub_only):.1f}%)")
    print(f"  Not in CATS             : {len(sub_only) - n_s_mat:,}  "
          f"({100 * (len(sub_only) - n_s_mat) / len(sub_only):.1f}%)")


# -- Figures --------------------------------------------------------------------------

def fig_comparison(cats_joined, cec_joined, cats_nodes) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(21, 10))

    cats_match = cats_joined[cats_joined["matched"]]
    cats_miss = cats_joined[~cats_joined["matched"]]
    cec_match = cec_joined[cec_joined["cats_matched"]]
    cec_miss = cec_joined[~cec_joined["cats_matched"]]

    def _setup(ax, title):
        ax.set_xlim(*_CA_LON)
        ax.set_ylim(*_CA_LAT)
        ax.set_aspect("equal")
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.grid(True, alpha=0.25, linewidth=0.5)
        ax.scatter(cats_nodes["Lon"], cats_nodes["Lat"],
                  s=2, color="#cccccc", alpha=0.3, zorder=1,
                  label=f"CATS AddedNode ({len(cats_nodes):,})")

    ax = axes[0]
    _setup(ax, f"CATS 'Substation' buses\n(threshold {MATCH_KM} km)")
    ax.scatter(cats_miss["Lon"], cats_miss["Lat"],
              s=12, color="#d62728", alpha=0.65, zorder=3,
              label=f"CATS only — not in CEC ({len(cats_miss):,})")
    ax.scatter(cats_match["Lon"], cats_match["Lat"],
              s=12, color="#2ca02c", alpha=0.65, zorder=3,
              label=f"CATS matched to CEC ({len(cats_match):,})")
    ax.legend(fontsize=8, loc="lower right", markerscale=1.5)

    ax = axes[1]
    _setup(ax, f"CEC 2026 (all types)\n(threshold {MATCH_KM} km)")
    ax.scatter(cec_miss["longitude"], cec_miss["latitude"],
              s=12, color="#ff7f0e", marker="x", linewidths=1.0, zorder=4,
              label=f"CEC only — not in CATS ({len(cec_miss):,})")
    ax.scatter(cec_match["longitude"], cec_match["latitude"],
              s=10, color="#1f77b4", alpha=0.5, zorder=3,
              label=f"CEC matched to CATS ({len(cec_match):,})")
    ax.legend(fontsize=8, loc="lower right", markerscale=1.5)

    ax = axes[2]
    _setup(ax, "Combined overlay")
    ax.scatter(cats_miss["Lon"], cats_miss["Lat"],
              s=8, color="#d62728", alpha=0.55, zorder=3,
              label=f"CATS only ({len(cats_miss):,})")
    ax.scatter(cats_match["Lon"], cats_match["Lat"],
              s=8, color="#2ca02c", alpha=0.45, zorder=4,
              label=f"CATS matched ({len(cats_match):,})")
    ax.scatter(cec_miss["longitude"], cec_miss["latitude"],
              s=16, color="#ff7f0e", marker="x", linewidths=1.0, zorder=5,
              label=f"CEC only ({len(cec_miss):,})")
    ax.scatter(cec_match["longitude"], cec_match["latitude"],
              s=8, color="#1f77b4", alpha=0.45, zorder=4,
              label=f"CEC matched ({len(cec_match):,})")
    ax.legend(fontsize=8, loc="lower right", markerscale=1.5)

    fig.suptitle(
        f"CATS Bus vs CEC Substation DataPull 2026  |  match threshold = {MATCH_KM} km\n"
        f"Grey dots = CATS AddedNodes (intermediate nodes, not substations)\n"
        f"CATS: {len(cats_joined):,} Substation buses  |  CEC: {len(cec_joined):,} records  |  "
        f"Matched: {cec_joined['cats_matched'].sum():,}",
        fontsize=10, y=1.01,
    )
    plt.tight_layout()
    out = FIGS_DIR / "cats_cec_comparison.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\nSaved: {out.relative_to(ROOT)}")


def fig_distance_distribution(cats_joined: pd.DataFrame) -> None:
    dists = cats_joined["dist_km"]
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))

    ax = axes[0]
    ax.hist(dists, bins=80, color="#2ca02c", edgecolor="white", linewidth=0.3)
    ax.axvline(MATCH_KM, color="#d62728", lw=2, linestyle="--",
              label=f"Match threshold ({MATCH_KM} km)")
    ax.set_xlabel("Distance to nearest CEC record (km)")
    ax.set_ylabel("CATS Substation bus count")
    ax.set_title("CATS -> CEC 2026: nearest-neighbour distance\n(full range)", fontsize=10)
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    close = dists[dists <= 20]
    ax.hist(close, bins=60, color="#2ca02c", edgecolor="white", linewidth=0.3)
    ax.axvline(MATCH_KM, color="#d62728", lw=2, linestyle="--",
              label=f"Match threshold ({MATCH_KM} km)")
    pct = 100 * (dists <= MATCH_KM).sum() / len(dists)
    ax.set_xlabel("Distance to nearest CEC record (km)")
    ax.set_ylabel("CATS Substation bus count")
    ax.set_title(
        f"Zoomed: distance <= 20 km\n{pct:.1f}% of CATS buses within {MATCH_KM} km of CEC",
        fontsize=10,
    )
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out = FIGS_DIR / "cats_cec_distance_distribution.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved: {out.relative_to(ROOT)}")


# -- Main -------------------------------------------------------------------------------

def main() -> None:
    print("Loading CATS buses ...")
    cats_sub, cats_nodes = load_cats()
    print(f"  CATS Substation buses : {len(cats_sub):,}")
    print(f"  CATS AddedNode buses  : {len(cats_nodes):,}")

    print("\nLoading CEC 2026 substations ...")
    cec = load_cec()
    print(f"  CEC records            : {len(cec):,}")
    print(f"  Type breakdown         : {cec['type'].value_counts().to_dict()}")

    print("\nRunning haversine nearest-neighbour join ...")
    cats_joined, cec_joined = join_datasets(cats_sub, cec)

    print_summary(cats_joined, cec_joined)

    cats_out = OUT_DIR / "cats_cec_join.csv"
    cec_out = OUT_DIR / "cec_cats_join.csv"
    cats_joined.to_csv(cats_out, index=False)
    cec_joined.to_csv(cec_out, index=False)
    print(f"\nJoin CSVs saved:")
    print(f"  {cats_out.relative_to(ROOT)}")
    print(f"  {cec_out.relative_to(ROOT)}")

    print("\nGenerating figures ...")
    fig_comparison(cats_joined, cec_joined, cats_nodes)
    fig_distance_distribution(cats_joined)

    print("\nDone.")


if __name__ == "__main__":
    main()
