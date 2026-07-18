"""
compare_cats_substations.py

Haversine nearest-neighbour join between our PGE/SCE/SDGE substation dataset
and the CATS (California Aggregate Transmission System) bus dataset.

CATS Bus types
--------------
'Substation'  -- 3,171 buses at named substations (primary comparison target)
'AddedNode'   -- 5,699 intermediate nodes on transmission segments (structural
                 model artefacts, not metered substations)

Join logic
----------
For each CATS Substation bus, find the nearest substation in our dataset.
For each substation in our dataset, find the nearest CATS Substation bus.
A pair is "matched" when the nearest-neighbour distance is <= MATCH_KM.

Outputs
-------
  data/figures/substation_maps/cats_substation_comparison.png
      Map of California showing match status for both datasets.

  data/checks/compare_cats_substations/cats_substation_join.csv
      CATS buses with join result appended (nearest substation, distance, match flag).

  data/checks/compare_cats_substations/our_substation_cats_join.csv
      Our substations with join result appended (nearest CATS bus, distance, match flag).
"""
from __future__ import annotations

import warnings
from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.neighbors import BallTree

# ── Config ────────────────────────────────────────────────────────────────────

ROOT      = Path(__file__).resolve().parents[2]
CATS_FILE = ROOT / "data" / "raw" / "PotentialData" / "CATS" / "CATS_buses.csv"
SUBS_FILE = ROOT / "data" / "processed" / "substations" / "substation_attributes_clean.csv"
FIGS_DIR  = ROOT / "data" / "figures" / "substation_maps"
OUT_DIR   = ROOT / "data" / "checks" / "compare_cats_substations"

FIGS_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

MATCH_KM  = 2.0   # distance threshold for a "match"
EARTH_KM  = 6371.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Vectorised haversine distance in km."""
    r = np.radians
    dlat = r(lat2) - r(lat1)
    dlon = r(lon2) - r(lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(r(lat1)) * np.cos(r(lat2)) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_KM * np.arcsin(np.sqrt(a))


def nearest_join(
    query_lat: np.ndarray,
    query_lon: np.ndarray,
    ref_lat:   np.ndarray,
    ref_lon:   np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """
    For each query point find the index and distance (km) of the nearest ref point.

    Uses sklearn BallTree with haversine metric — exact spherical distances.
    Returns (indices, distances_km), each shape (n_query,).
    """
    ref_rad   = np.column_stack([np.radians(ref_lat),   np.radians(ref_lon)])
    query_rad = np.column_stack([np.radians(query_lat), np.radians(query_lon)])
    tree = BallTree(ref_rad, metric="haversine")
    dist_rad, idx = tree.query(query_rad, k=1)
    return idx.flatten(), dist_rad.flatten() * EARTH_KM


# ── Load data ─────────────────────────────────────────────────────────────────

def load_cats() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (cats_sub, cats_nodes) — Substation and AddedNode rows separately."""
    df = pd.read_csv(CATS_FILE)
    df["Type"] = df["Type"].str.strip("'")  # strip surrounding quotes
    cats_sub   = df[df["Type"] == "Substation"].copy().reset_index(drop=True)
    cats_nodes = df[df["Type"] == "AddedNode"].copy().reset_index(drop=True)
    return cats_sub, cats_nodes


def load_our_subs() -> pd.DataFrame:
    """
    Load substation attributes and resolve best available coordinates.

    Priority: util_lat/util_lon (utility-provided) > basin_lat/basin_lon (DataBasin).
    Returns only rows with at least one coordinate source.
    """
    df = pd.read_csv(SUBS_FILE)
    # Best lat: util first, then basin
    df["lat"] = df["util_lat"].where(df["util_lat"].notna(), df["basin_lat"])
    df["lon"] = df["util_lon"].where(df["util_lon"].notna(), df["basin_lon"])
    df["coord_source"] = np.where(
        df["util_lat"].notna(), "utility",
        np.where(df["basin_lat"].notna(), "basin", "none")
    )
    n_before = len(df)
    df = df[df["lat"].notna() & df["lon"].notna()].copy().reset_index(drop=True)
    n_dropped = n_before - len(df)
    if n_dropped:
        print(f"  Dropped {n_dropped} substations with no coordinates")
    return df


# ── Join ──────────────────────────────────────────────────────────────────────

def join_datasets(
    cats_sub: pd.DataFrame,
    our_subs: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Bidirectional nearest-neighbour join.

    Returns
    -------
    cats_joined : cats_sub with columns appended:
        nearest_our_sub   -- substation_name of closest match
        nearest_utility   -- utility of closest match
        dist_km           -- haversine distance to closest match
        matched           -- True if dist_km <= MATCH_KM
    our_joined  : our_subs with columns appended:
        nearest_cats_id   -- bus_i of closest CATS Substation
        nearest_cats_kv   -- kV of closest CATS Substation
        cats_dist_km      -- haversine distance
        cats_matched      -- True if cats_dist_km <= MATCH_KM
    """
    # CATS -> our substations
    idx_o, dist_o = nearest_join(
        cats_sub["Lat"].values, cats_sub["Lon"].values,
        our_subs["lat"].values, our_subs["lon"].values,
    )
    cats_joined = cats_sub.copy()
    cats_joined["nearest_our_sub"] = our_subs["substation_name"].iloc[idx_o].values
    cats_joined["nearest_utility"] = our_subs["utility"].iloc[idx_o].values
    cats_joined["nearest_our_lat"] = our_subs["lat"].iloc[idx_o].values
    cats_joined["nearest_our_lon"] = our_subs["lon"].iloc[idx_o].values
    cats_joined["dist_km"]         = dist_o
    cats_joined["matched"]         = dist_o <= MATCH_KM

    # Our substations -> CATS
    idx_c, dist_c = nearest_join(
        our_subs["lat"].values, our_subs["lon"].values,
        cats_sub["Lat"].values, cats_sub["Lon"].values,
    )
    our_joined = our_subs.copy()
    our_joined["nearest_cats_id"]  = cats_sub["bus_i"].iloc[idx_c].values
    our_joined["nearest_cats_kv"]  = cats_sub["kV"].iloc[idx_c].values
    our_joined["nearest_cats_lat"] = cats_sub["Lat"].iloc[idx_c].values
    our_joined["nearest_cats_lon"] = cats_sub["Lon"].iloc[idx_c].values
    our_joined["cats_dist_km"]     = dist_c
    our_joined["cats_matched"]     = dist_c <= MATCH_KM

    return cats_joined, our_joined


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(cats_joined: pd.DataFrame, our_joined: pd.DataFrame) -> None:
    n_cats      = len(cats_joined)
    n_cats_mat  = cats_joined["matched"].sum()
    n_cats_miss = n_cats - n_cats_mat

    n_our       = len(our_joined)
    n_our_mat   = our_joined["cats_matched"].sum()
    n_our_miss  = n_our - n_our_mat

    print(f"\n{'='*55}")
    print(f"CATS vs Our Substations — Distance Join (threshold {MATCH_KM} km)")
    print(f"{'='*55}")
    print(f"\nCATS 'Substation' buses ({n_cats:,} total):")
    print(f"  Matched to our data     : {n_cats_mat:,}  ({100*n_cats_mat/n_cats:.1f}%)")
    print(f"  Not in our data         : {n_cats_miss:,}  ({100*n_cats_miss/n_cats:.1f}%)")

    print(f"\nOur substations ({n_our:,} total, PGE+SCE+SDGE):")
    print(f"  Matched to CATS         : {n_our_mat:,}  ({100*n_our_mat/n_our:.1f}%)")
    print(f"  Not in CATS             : {n_our_miss:,}  ({100*n_our_miss/n_our:.1f}%)")

    print(f"\nDistance distribution (CATS -> nearest our sub, km):")
    print(cats_joined["dist_km"].describe(percentiles=[.25,.5,.75,.9,.95,.99]).round(2).to_string())

    print(f"\nUnmatched CATS buses by kV:")
    miss_cats = cats_joined[~cats_joined["matched"]]
    print(miss_cats["kV"].value_counts().sort_index().to_string())

    print(f"\nUnmatched our substations by utility:")
    miss_our = our_joined[~our_joined["cats_matched"]]
    print(miss_our["utility"].value_counts().to_string())


# ── Map ───────────────────────────────────────────────────────────────────────

# Approximate California bounding box
_CA_LON = (-124.5, -114.0)
_CA_LAT  = (32.5,   42.0)


def fig_cats_comparison(
    cats_joined:  pd.DataFrame,
    our_joined:   pd.DataFrame,
    cats_nodes:   pd.DataFrame,
) -> None:
    """
    Three-panel map:
      Left   -- CATS Substation buses coloured by match status
      Centre -- Our substations coloured by match status
      Right  -- Overlay: all four groups on one axes
    """
    fig, axes = plt.subplots(1, 3, figsize=(21, 10))

    cats_match  = cats_joined[cats_joined["matched"]]
    cats_miss   = cats_joined[~cats_joined["matched"]]
    our_match   = our_joined[our_joined["cats_matched"]]
    our_miss    = our_joined[~our_joined["cats_matched"]]

    def _setup(ax, title):
        ax.set_xlim(*_CA_LON)
        ax.set_ylim(*_CA_LAT)
        ax.set_aspect("equal")
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.grid(True, alpha=0.25, linewidth=0.5)
        # AddedNodes as faint background
        ax.scatter(cats_nodes["Lon"], cats_nodes["Lat"],
                   s=2, color="#cccccc", alpha=0.3, zorder=1,
                   label=f"CATS AddedNode ({len(cats_nodes):,})")

    # ── Panel A: CATS substations ──────────────────────────────────────────────
    ax = axes[0]
    _setup(ax, f"CATS 'Substation' buses\n(threshold {MATCH_KM} km)")
    ax.scatter(cats_miss["Lon"],  cats_miss["Lat"],
               s=14, color="#d62728", alpha=0.7, zorder=3,
               label=f"CATS only — not in our data ({len(cats_miss):,})")
    ax.scatter(cats_match["Lon"], cats_match["Lat"],
               s=14, color="#2ca02c", alpha=0.7, zorder=3,
               label=f"CATS matched to our data ({len(cats_match):,})")
    ax.legend(fontsize=8, loc="lower right", markerscale=1.5)

    # ── Panel B: our substations ───────────────────────────────────────────────
    ax = axes[1]
    _setup(ax, f"Our substations (PGE+SCE+SDGE)\n(threshold {MATCH_KM} km)")
    ax.scatter(our_miss["lon"],  our_miss["lat"],
               s=18, color="#ff7f0e", marker="x", linewidths=1.2, zorder=3,
               label=f"Our data only — not in CATS ({len(our_miss):,})")
    ax.scatter(our_match["lon"], our_match["lat"],
               s=14, color="#1f77b4", alpha=0.7, zorder=3,
               label=f"Our data matched to CATS ({len(our_match):,})")
    ax.legend(fontsize=8, loc="lower right", markerscale=1.5)

    # ── Panel C: combined overlay ──────────────────────────────────────────────
    ax = axes[2]
    _setup(ax, "Combined overlay\n(all four groups)")
    ax.scatter(cats_miss["Lon"],  cats_miss["Lat"],
               s=10, color="#d62728", alpha=0.6, zorder=3,
               label=f"CATS only ({len(cats_miss):,})")
    ax.scatter(cats_match["Lon"], cats_match["Lat"],
               s=10, color="#2ca02c", alpha=0.5, zorder=4,
               label=f"CATS matched ({len(cats_match):,})")
    ax.scatter(our_miss["lon"],  our_miss["lat"],
               s=22, color="#ff7f0e", marker="x", linewidths=1.2, zorder=5,
               label=f"Ours only ({len(our_miss):,})")
    ax.scatter(our_match["lon"], our_match["lat"],
               s=10, color="#1f77b4", alpha=0.6, zorder=4,
               label=f"Ours matched ({len(our_match):,})")
    ax.legend(fontsize=8, loc="lower right", markerscale=1.5)

    fig.suptitle(
        f"CATS Bus vs PGE/SCE/SDGE Substation Coverage  |  match threshold = {MATCH_KM} km\n"
        f"Grey dots = CATS AddedNodes (intermediate line nodes, not substations)\n"
        f"CATS: {len(cats_joined):,} Substation buses  |  Ours: {len(our_joined):,} substations  |  "
        f"Matched: {our_joined['cats_matched'].sum():,}",
        fontsize=10, y=1.01,
    )
    plt.tight_layout()
    out = FIGS_DIR / "cats_substation_comparison.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\nSaved: {out.relative_to(ROOT)}")


def fig_distance_distribution(cats_joined: pd.DataFrame) -> None:
    """
    Histogram of nearest-neighbour distances (CATS -> our data) with threshold line.
    Helps the user pick an appropriate match threshold.
    """
    dists = cats_joined["dist_km"]
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))

    # Full range
    ax = axes[0]
    ax.hist(dists, bins=80, color="#1f77b4", edgecolor="white", linewidth=0.3)
    ax.axvline(MATCH_KM, color="#d62728", lw=2, linestyle="--",
               label=f"Match threshold ({MATCH_KM} km)")
    ax.set_xlabel("Distance to nearest our-substation (km)")
    ax.set_ylabel("CATS Substation bus count")
    ax.set_title("CATS -> our data: nearest-neighbour distance\n(full range)", fontsize=10)
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    # Zoomed to ≤ 20 km
    ax = axes[1]
    close = dists[dists <= 20]
    ax.hist(close, bins=60, color="#1f77b4", edgecolor="white", linewidth=0.3)
    ax.axvline(MATCH_KM, color="#d62728", lw=2, linestyle="--",
               label=f"Match threshold ({MATCH_KM} km)")
    pct = 100 * (dists <= MATCH_KM).sum() / len(dists)
    ax.set_xlabel("Distance to nearest our-substation (km)")
    ax.set_ylabel("CATS Substation bus count")
    ax.set_title(
        f"Zoomed: distance <= 20 km\n"
        f"{pct:.1f}% of CATS buses within {MATCH_KM} km of our data",
        fontsize=10,
    )
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out = FIGS_DIR / "cats_distance_distribution.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Saved: {out.relative_to(ROOT)}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Loading CATS buses ...")
    cats_sub, cats_nodes = load_cats()
    print(f"  CATS Substation buses : {len(cats_sub):,}")
    print(f"  CATS AddedNode buses  : {len(cats_nodes):,}")

    print("\nLoading our substation attributes ...")
    our_subs = load_our_subs()
    print(f"  Our substations (with coords) : {len(our_subs):,}")
    print(f"  Coord source breakdown: {our_subs['coord_source'].value_counts().to_dict()}")

    print("\nRunning haversine nearest-neighbour join ...")
    cats_joined, our_joined = join_datasets(cats_sub, our_subs)

    print_summary(cats_joined, our_joined)

    # Save join outputs
    cats_out = OUT_DIR / "cats_substation_join.csv"
    our_out  = OUT_DIR / "our_substation_cats_join.csv"
    cats_joined.to_csv(cats_out, index=False)
    our_joined.to_csv(our_out,   index=False)
    print(f"\nJoin CSVs saved:")
    print(f"  {cats_out.relative_to(ROOT)}")
    print(f"  {our_out.relative_to(ROOT)}")

    print("\nGenerating figures ...")
    fig_cats_comparison(cats_joined, our_joined, cats_nodes)
    fig_distance_distribution(cats_joined)

    print("\nDone.")


if __name__ == "__main__":
    main()
