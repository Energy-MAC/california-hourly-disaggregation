"""
compare_cats_basin.py

Haversine nearest-neighbour join between the DataBasin California Substations 2022
dataset and the CATS (California Aggregate Transmission System) bus dataset.

Basin dataset
-------------
  data/processed/substation_misc/ca_substations_2022.csv
  4,442 rows; type values: SUBSTATION (3,371), TAP (692), RISER (103), etc.
  All rows have latitude / longitude coordinates.
  Covers all California utilities (PGE, SCE, SDGE, SMUD, IID, LADWP, Pacificorp, …)

CATS Bus types
--------------
  'Substation'  — 3,171 buses at named substations (primary comparison target)
  'AddedNode'   — 5,699 intermediate nodes on transmission segments

Join logic
----------
For each CATS Substation bus, find the nearest Basin record.
For each Basin record, find the nearest CATS Substation bus.
A pair is "matched" when the nearest-neighbour distance is <= MATCH_KM.

Outputs
-------
  data/figures/substation_maps/cats_basin_comparison.png
  data/figures/substation_maps/cats_basin_distance_distribution.png
  data/checks/compare_cats_basin/cats_basin_join.csv
  data/checks/compare_cats_basin/basin_cats_join.csv
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.neighbors import BallTree

# ── Config ────────────────────────────────────────────────────────────────────

ROOT       = Path(__file__).resolve().parents[2]
CATS_FILE  = ROOT / "data" / "raw" / "PotentialData" / "CATS" / "CATS_buses.csv"
BASIN_FILE = ROOT / "data" / "processed" / "substation_misc" / "ca_substations_2022.csv"
FIGS_DIR   = ROOT / "data" / "figures" / "substation_maps"
OUT_DIR    = ROOT / "data" / "checks" / "compare_cats_basin"

FIGS_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

MATCH_KM   = 2.0
EARTH_KM   = 6371.0

_CA_LON    = (-124.5, -114.0)
_CA_LAT    = (32.5,    42.0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def nearest_join(
    query_lat: np.ndarray,
    query_lon: np.ndarray,
    ref_lat:   np.ndarray,
    ref_lon:   np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    ref_rad   = np.column_stack([np.radians(ref_lat),   np.radians(ref_lon)])
    query_rad = np.column_stack([np.radians(query_lat), np.radians(query_lon)])
    tree = BallTree(ref_rad, metric="haversine")
    dist_rad, idx = tree.query(query_rad, k=1)
    return idx.flatten(), dist_rad.flatten() * EARTH_KM


# ── Load data ─────────────────────────────────────────────────────────────────

def load_cats() -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_csv(CATS_FILE)
    df["Type"] = df["Type"].str.strip("'")
    cats_sub   = df[df["Type"] == "Substation"].copy().reset_index(drop=True)
    cats_nodes = df[df["Type"] == "AddedNode"].copy().reset_index(drop=True)
    return cats_sub, cats_nodes


def load_basin() -> pd.DataFrame:
    df = pd.read_csv(BASIN_FILE)
    # Drop the tiny fraction with invalid coordinates (none in this file, but be safe)
    n_before = len(df)
    df = df[df["latitude"].notna() & df["longitude"].notna()].copy().reset_index(drop=True)
    if len(df) < n_before:
        print(f"  Dropped {n_before - len(df)} Basin rows with missing coordinates")
    return df


# ── Join ──────────────────────────────────────────────────────────────────────

def join_datasets(
    cats_sub: pd.DataFrame,
    basin:    pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    # CATS -> Basin
    idx_b, dist_b = nearest_join(
        cats_sub["Lat"].values, cats_sub["Lon"].values,
        basin["latitude"].values, basin["longitude"].values,
    )
    cats_joined = cats_sub.copy()
    cats_joined["nearest_basin_name"]  = basin["name"].iloc[idx_b].values
    cats_joined["nearest_basin_owner"] = basin["owner_std"].iloc[idx_b].values
    cats_joined["nearest_basin_type"]  = basin["type"].iloc[idx_b].values
    cats_joined["nearest_basin_lat"]   = basin["latitude"].iloc[idx_b].values
    cats_joined["nearest_basin_lon"]   = basin["longitude"].iloc[idx_b].values
    cats_joined["dist_km"]             = dist_b
    cats_joined["matched"]             = dist_b <= MATCH_KM

    # Basin -> CATS
    idx_c, dist_c = nearest_join(
        basin["latitude"].values, basin["longitude"].values,
        cats_sub["Lat"].values, cats_sub["Lon"].values,
    )
    basin_joined = basin.copy()
    basin_joined["nearest_cats_id"]  = cats_sub["bus_i"].iloc[idx_c].values
    basin_joined["nearest_cats_kv"]  = cats_sub["kV"].iloc[idx_c].values
    basin_joined["nearest_cats_lat"] = cats_sub["Lat"].iloc[idx_c].values
    basin_joined["nearest_cats_lon"] = cats_sub["Lon"].iloc[idx_c].values
    basin_joined["cats_dist_km"]     = dist_c
    basin_joined["cats_matched"]     = dist_c <= MATCH_KM

    return cats_joined, basin_joined


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(cats_joined: pd.DataFrame, basin_joined: pd.DataFrame) -> None:
    n_cats     = len(cats_joined)
    n_c_mat    = cats_joined["matched"].sum()
    n_c_miss   = n_cats - n_c_mat

    n_basin    = len(basin_joined)
    n_b_mat    = basin_joined["cats_matched"].sum()
    n_b_miss   = n_basin - n_b_mat

    print(f"\n{'='*60}")
    print(f"CATS vs Basin 2022 — Distance Join (threshold {MATCH_KM} km)")
    print(f"{'='*60}")
    print(f"\nCATS 'Substation' buses ({n_cats:,} total):")
    print(f"  Matched to Basin        : {n_c_mat:,}  ({100*n_c_mat/n_cats:.1f}%)")
    print(f"  Not in Basin            : {n_c_miss:,}  ({100*n_c_miss/n_cats:.1f}%)")

    print(f"\nBasin records ({n_basin:,} total, all types):")
    print(f"  Matched to CATS         : {n_b_mat:,}  ({100*n_b_mat/n_basin:.1f}%)")
    print(f"  Not in CATS             : {n_b_miss:,}  ({100*n_b_miss/n_basin:.1f}%)")

    print(f"\nDistance distribution (CATS -> nearest Basin, km):")
    print(cats_joined["dist_km"].describe(
        percentiles=[.25, .5, .75, .9, .95, .99]).round(2).to_string())

    print(f"\nUnmatched CATS buses by kV:")
    miss_cats = cats_joined[~cats_joined["matched"]]
    print(miss_cats["kV"].value_counts().sort_index().to_string())

    print(f"\nMatched CATS buses by nearest Basin type:")
    print(cats_joined[cats_joined["matched"]]["nearest_basin_type"].value_counts().to_string())

    print(f"\nUnmatched Basin records by type:")
    miss_basin = basin_joined[~basin_joined["cats_matched"]]
    print(miss_basin["type"].value_counts().to_string())

    print(f"\nUnmatched Basin records by owner_std:")
    print(miss_basin["owner_std"].value_counts().head(15).to_string())

    # Basin SUBSTATION type specifically
    sub_only = basin_joined[basin_joined["type"] == "SUBSTATION"]
    n_s_mat  = sub_only["cats_matched"].sum()
    print(f"\nBasin SUBSTATION type only ({len(sub_only):,} records):")
    print(f"  Matched to CATS         : {n_s_mat:,}  ({100*n_s_mat/len(sub_only):.1f}%)")
    print(f"  Not in CATS             : {len(sub_only)-n_s_mat:,}  ({100*(len(sub_only)-n_s_mat)/len(sub_only):.1f}%)")


# ── Figures ───────────────────────────────────────────────────────────────────

def fig_comparison(
    cats_joined:  pd.DataFrame,
    basin_joined: pd.DataFrame,
    cats_nodes:   pd.DataFrame,
) -> None:
    """Three-panel coverage map."""
    fig, axes = plt.subplots(1, 3, figsize=(21, 10))

    cats_match  = cats_joined[cats_joined["matched"]]
    cats_miss   = cats_joined[~cats_joined["matched"]]
    bas_match   = basin_joined[basin_joined["cats_matched"]]
    bas_miss    = basin_joined[~basin_joined["cats_matched"]]

    # Colour Basin records by type for panel B
    _TYPE_COLORS = {
        "SUBSTATION":    "#1f77b4",
        "TAP":           "#9467bd",
        "RISER":         "#8c564b",
        "DEAD END":      "#e377c2",
        "NOT AVAILABLE": "#7f7f7f",
    }

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

    # Panel A: CATS substations
    ax = axes[0]
    _setup(ax, f"CATS 'Substation' buses\n(threshold {MATCH_KM} km)")
    ax.scatter(cats_miss["Lon"],  cats_miss["Lat"],
               s=12, color="#d62728", alpha=0.65, zorder=3,
               label=f"CATS only — not in Basin ({len(cats_miss):,})")
    ax.scatter(cats_match["Lon"], cats_match["Lat"],
               s=12, color="#2ca02c", alpha=0.65, zorder=3,
               label=f"CATS matched to Basin ({len(cats_match):,})")
    ax.legend(fontsize=8, loc="lower right", markerscale=1.5)

    # Panel B: Basin records coloured by type + match status
    ax = axes[1]
    _setup(ax, f"Basin 2022 (all types)\n(threshold {MATCH_KM} km)")
    ax.scatter(bas_miss["longitude"],  bas_miss["latitude"],
               s=12, color="#ff7f0e", marker="x", linewidths=1.0, zorder=4,
               label=f"Basin only — not in CATS ({len(bas_miss):,})")
    ax.scatter(bas_match["longitude"], bas_match["latitude"],
               s=10, color="#1f77b4", alpha=0.5, zorder=3,
               label=f"Basin matched to CATS ({len(bas_match):,})")
    ax.legend(fontsize=8, loc="lower right", markerscale=1.5)

    # Panel C: overlay
    ax = axes[2]
    _setup(ax, "Combined overlay")
    ax.scatter(cats_miss["Lon"],  cats_miss["Lat"],
               s=8,  color="#d62728", alpha=0.55, zorder=3,
               label=f"CATS only ({len(cats_miss):,})")
    ax.scatter(cats_match["Lon"], cats_match["Lat"],
               s=8,  color="#2ca02c", alpha=0.45, zorder=4,
               label=f"CATS matched ({len(cats_match):,})")
    ax.scatter(bas_miss["longitude"],  bas_miss["latitude"],
               s=16, color="#ff7f0e", marker="x", linewidths=1.0, zorder=5,
               label=f"Basin only ({len(bas_miss):,})")
    ax.scatter(bas_match["longitude"], bas_match["latitude"],
               s=8,  color="#1f77b4", alpha=0.45, zorder=4,
               label=f"Basin matched ({len(bas_match):,})")
    ax.legend(fontsize=8, loc="lower right", markerscale=1.5)

    fig.suptitle(
        f"CATS Bus vs DataBasin CA Substations 2022  |  match threshold = {MATCH_KM} km\n"
        f"Grey dots = CATS AddedNodes (intermediate nodes, not substations)\n"
        f"CATS: {len(cats_joined):,} Substation buses  |  Basin: {len(basin_joined):,} records  |  "
        f"Matched: {basin_joined['cats_matched'].sum():,}",
        fontsize=10, y=1.01,
    )
    plt.tight_layout()
    out = FIGS_DIR / "cats_basin_comparison.png"
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
    ax.set_xlabel("Distance to nearest Basin record (km)")
    ax.set_ylabel("CATS Substation bus count")
    ax.set_title("CATS -> Basin 2022: nearest-neighbour distance\n(full range)", fontsize=10)
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    close = dists[dists <= 20]
    ax.hist(close, bins=60, color="#2ca02c", edgecolor="white", linewidth=0.3)
    ax.axvline(MATCH_KM, color="#d62728", lw=2, linestyle="--",
               label=f"Match threshold ({MATCH_KM} km)")
    pct = 100 * (dists <= MATCH_KM).sum() / len(dists)
    ax.set_xlabel("Distance to nearest Basin record (km)")
    ax.set_ylabel("CATS Substation bus count")
    ax.set_title(
        f"Zoomed: distance <= 20 km\n{pct:.1f}% of CATS buses within {MATCH_KM} km of Basin",
        fontsize=10,
    )
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out = FIGS_DIR / "cats_basin_distance_distribution.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved: {out.relative_to(ROOT)}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Loading CATS buses ...")
    cats_sub, cats_nodes = load_cats()
    print(f"  CATS Substation buses : {len(cats_sub):,}")
    print(f"  CATS AddedNode buses  : {len(cats_nodes):,}")

    print("\nLoading Basin 2022 substations ...")
    basin = load_basin()
    print(f"  Basin records         : {len(basin):,}")
    print(f"  Type breakdown        : {basin['type'].value_counts().to_dict()}")

    print("\nRunning haversine nearest-neighbour join ...")
    cats_joined, basin_joined = join_datasets(cats_sub, basin)

    print_summary(cats_joined, basin_joined)

    cats_out  = OUT_DIR / "cats_basin_join.csv"
    basin_out = OUT_DIR / "basin_cats_join.csv"
    cats_joined.to_csv(cats_out,   index=False)
    basin_joined.to_csv(basin_out, index=False)
    print(f"\nJoin CSVs saved:")
    print(f"  {cats_out.relative_to(ROOT)}")
    print(f"  {basin_out.relative_to(ROOT)}")

    print("\nGenerating figures ...")
    fig_comparison(cats_joined, basin_joined, cats_nodes)
    fig_distance_distribution(cats_joined)

    print("\nDone.")


if __name__ == "__main__":
    main()
