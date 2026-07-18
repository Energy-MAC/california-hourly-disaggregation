"""
compare_cats_unfiltered.py

CATS distance join against the unfiltered substation attributes
(data/processed/substations/substation_attributes.csv), which retains
pass-through (P.T.) substations and Pacificorp entries removed during
cleaning.

Purpose
-------
Determine whether the P.T. substations filtered out of our clean dataset
are represented in the CATS transmission model, which would imply that
CATS models them as grid nodes even though we exclude them from metered
load analysis.

Filtering groups in the unfiltered file
----------------------------------------
  is_pt = True    -- name ends with "P.T." (pass-through switching node;
                     excluded from load analysis because they have no
                     individual meters)
  in_clean = True -- appears by (utility, substation_name) in
                     substation_attributes_clean.csv (our final kept set)
  pacificorp      -- utility == 'pacificorp' (excluded from load analysis
                     entirely; no metered load profiles exist)

Coordinate source
-----------------
The unfiltered file uses utility-provided lat/lon only (no Basin enrichment).
3 of 165 PT rows have null coordinates and are excluded from the join.

Outputs
-------
  data/checks/compare_cats_unfiltered/cats_unfiltered_join.csv
      All joinable unfiltered rows with CATS match result and group labels.

  data/figures/substation_maps/cats_unfiltered_comparison.png
      Map coloured by group × match status.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.neighbors import BallTree

# ── Config ────────────────────────────────────────────────────────────────────

ROOT          = Path(__file__).resolve().parents[2]
CATS_FILE     = ROOT / "data" / "raw" / "PotentialData" / "CATS" / "CATS_buses.csv"
UNFILTERED    = ROOT / "data" / "processed" / "substations" / "substation_attributes.csv"
CLEAN         = ROOT / "data" / "processed" / "substations" / "substation_attributes_clean.csv"
FIGS_DIR      = ROOT / "data" / "figures" / "substation_maps"
OUT_DIR       = ROOT / "data" / "checks" / "compare_cats_unfiltered"

FIGS_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

MATCH_KM      = 2.0
EARTH_KM      = 6371.0
_CA_LON       = (-124.5, -114.0)
_CA_LAT       = (32.5,    42.0)


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


def is_pt(names: pd.Series) -> pd.Series:
    return names.str.contains(r"p\.?\s*t\.?\s*$", case=False, regex=True, na=False)


# ── Load data ─────────────────────────────────────────────────────────────────

def load_cats() -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_csv(CATS_FILE)
    df["Type"] = df["Type"].str.strip("'")
    return (
        df[df["Type"] == "Substation"].copy().reset_index(drop=True),
        df[df["Type"] == "AddedNode"].copy().reset_index(drop=True),
    )


def load_unfiltered() -> pd.DataFrame:
    df       = pd.read_csv(UNFILTERED)
    clean    = pd.read_csv(CLEAN)
    clean_keys = set(zip(clean["utility"].str.lower(), clean["substation_name"].str.lower()))

    df["is_pt"]     = is_pt(df["substation_name"])
    df["in_clean"]  = [
        (u.lower(), n.lower()) in clean_keys
        for u, n in zip(df["utility"], df["substation_name"])
    ]

    # Drop rows with no coordinates
    n_before = len(df)
    df = df[df["latitude"].notna() & df["longitude"].notna()].copy().reset_index(drop=True)
    if len(df) < n_before:
        print(f"  Dropped {n_before - len(df)} rows with null coordinates")

    return df


# ── Join ──────────────────────────────────────────────────────────────────────

def join_cats(df: pd.DataFrame, cats_sub: pd.DataFrame) -> pd.DataFrame:
    idx, dist = nearest_join(
        df["latitude"].values, df["longitude"].values,
        cats_sub["Lat"].values, cats_sub["Lon"].values,
    )
    out = df.copy()
    out["cats_bus_i"]  = cats_sub["bus_i"].iloc[idx].values
    out["cats_kv"]     = cats_sub["kV"].iloc[idx].values
    out["cats_lat"]    = cats_sub["Lat"].iloc[idx].values
    out["cats_lon"]    = cats_sub["Lon"].iloc[idx].values
    out["cats_dist_km"]= dist
    out["cats_matched"]= dist <= MATCH_KM
    return out


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(joined: pd.DataFrame) -> None:
    print(f"\n{'='*65}")
    print(f"CATS vs Unfiltered Substations — match threshold {MATCH_KM} km")
    print(f"{'='*65}")

    groups = {
        "PT substations (filtered out)":
            joined[joined["is_pt"] & ~joined["in_clean"] & (joined["utility"] != "pacificorp")],
        "Non-PT, in clean dataset":
            joined[~joined["is_pt"] & joined["in_clean"]],
        "Non-PT, filtered for other reasons":
            joined[~joined["is_pt"] & ~joined["in_clean"] & (joined["utility"] != "pacificorp")],
        "Pacificorp (all excluded)":
            joined[joined["utility"] == "pacificorp"],
    }

    for label, grp in groups.items():
        if len(grp) == 0:
            continue
        n_mat = grp["cats_matched"].sum()
        print(f"\n{label}  (n={len(grp):,})")
        print(f"  Matched to CATS : {n_mat:,}  ({100*n_mat/len(grp):.1f}%)")
        print(f"  Not in CATS     : {len(grp)-n_mat:,}  ({100*(len(grp)-n_mat)/len(grp):.1f}%)")
        print(f"  Median dist km  : {grp['cats_dist_km'].median():.2f}")
        if len(grp) >= 5:
            print(f"  Distance p90 km : {grp['cats_dist_km'].quantile(0.90):.2f}")

    # PT by utility breakdown
    print(f"\nPT substations breakdown by utility:")
    pt = joined[joined["is_pt"]]
    for util, grp in pt.groupby("utility"):
        n_m = grp["cats_matched"].sum()
        print(f"  {util:12s}  n={len(grp):3d}  matched={n_m:3d} ({100*n_m/len(grp):.0f}%)")

    # Unmatched PT names (to identify geography)
    pt_miss = joined[joined["is_pt"] & ~joined["cats_matched"]]
    if len(pt_miss):
        print(f"\nSample unmatched PT substations ({min(20, len(pt_miss))} of {len(pt_miss)}):")
        print(pt_miss[["utility","substation_name","latitude","longitude","cats_dist_km"]]
              .head(20).to_string(index=False))


# ── Map ───────────────────────────────────────────────────────────────────────

def fig_unfiltered_comparison(
    joined:     pd.DataFrame,
    cats_nodes: pd.DataFrame,
) -> None:
    """
    Two-panel map.
    Left  — all substations coloured by group, sized by match status.
    Right — PT substations only, zoomed to show their locations.
    """
    fig, axes = plt.subplots(1, 2, figsize=(18, 10))

    # Group masks
    in_clean_mat  = joined[~joined["is_pt"] &  joined["in_clean"] &  joined["cats_matched"]]
    in_clean_miss = joined[~joined["is_pt"] &  joined["in_clean"] & ~joined["cats_matched"]]
    pt_mat        = joined[ joined["is_pt"] &  joined["cats_matched"]]
    pt_miss       = joined[ joined["is_pt"] & ~joined["cats_matched"]]
    pac           = joined[ joined["utility"] == "pacificorp"]
    other_miss    = joined[~joined["is_pt"] & ~joined["in_clean"] & (joined["utility"] != "pacificorp")]

    def _setup(ax, title):
        ax.set_xlim(*_CA_LON)
        ax.set_ylim(*_CA_LAT)
        ax.set_aspect("equal")
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.grid(True, alpha=0.25, linewidth=0.5)
        ax.scatter(cats_nodes["Lon"], cats_nodes["Lat"],
                   s=2, color="#cccccc", alpha=0.25, zorder=1,
                   label=f"CATS AddedNode ({len(cats_nodes):,})")

    # Panel A: all groups
    ax = axes[0]
    _setup(ax, f"All unfiltered substations by group\n(threshold {MATCH_KM} km)")
    ax.scatter(pac["longitude"], pac["latitude"],
               s=6, color="#aec7e8", alpha=0.4, zorder=2,
               label=f"Pacificorp ({len(pac):,})")
    ax.scatter(other_miss["longitude"], other_miss["latitude"],
               s=10, color="#98df8a", marker="s", alpha=0.6, zorder=3,
               label=f"Non-PT filtered-other, unmatched ({len(other_miss):,})")
    ax.scatter(in_clean_miss["longitude"], in_clean_miss["latitude"],
               s=14, color="#ff7f0e", marker="x", linewidths=1.2, zorder=4,
               label=f"In clean, not in CATS ({len(in_clean_miss):,})")
    ax.scatter(in_clean_mat["longitude"], in_clean_mat["latitude"],
               s=10, color="#1f77b4", alpha=0.5, zorder=4,
               label=f"In clean, matched CATS ({len(in_clean_mat):,})")
    ax.scatter(pt_miss["longitude"], pt_miss["latitude"],
               s=40, color="#d62728", marker="^", alpha=0.85, zorder=5,
               label=f"PT, not in CATS ({len(pt_miss):,})")
    ax.scatter(pt_mat["longitude"], pt_mat["latitude"],
               s=40, color="#2ca02c", marker="^", alpha=0.85, zorder=5,
               label=f"PT, matched CATS ({len(pt_mat):,})")
    ax.legend(fontsize=7.5, loc="lower right", markerscale=1.3)

    # Panel B: PT substations only (zoomed to their bounding box)
    ax = axes[1]
    all_pt = joined[joined["is_pt"]]
    pad_lon = 1.0
    pad_lat = 0.5
    lon_min = max(_CA_LON[0], all_pt["longitude"].min() - pad_lon)
    lon_max = min(_CA_LON[1], all_pt["longitude"].max() + pad_lon)
    lat_min = max(_CA_LAT[0], all_pt["latitude"].min()  - pad_lat)
    lat_max = min(_CA_LAT[1], all_pt["latitude"].max()  + pad_lat)
    ax.set_xlim(lon_min, lon_max)
    ax.set_ylim(lat_min, lat_max)
    ax.set_aspect("equal")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title(
        f"P.T. substations only\n({len(all_pt):,} total, {len(pt_mat):,} matched, {len(pt_miss):,} unmatched)",
        fontsize=11, fontweight="bold",
    )
    ax.grid(True, alpha=0.25, linewidth=0.5)
    ax.scatter(cats_nodes["Lon"], cats_nodes["Lat"],
               s=3, color="#cccccc", alpha=0.3, zorder=1)

    # also show our clean (non-PT) substations as context
    ax.scatter(in_clean_mat["longitude"], in_clean_mat["latitude"],
               s=6, color="#aec7e8", alpha=0.35, zorder=2,
               label=f"Our clean substations ({len(in_clean_mat)+len(in_clean_miss):,})")
    ax.scatter(pt_miss["longitude"], pt_miss["latitude"],
               s=60, color="#d62728", marker="^", alpha=0.9, zorder=5,
               label=f"PT, not in CATS ({len(pt_miss):,})")
    ax.scatter(pt_mat["longitude"], pt_mat["latitude"],
               s=60, color="#2ca02c", marker="^", alpha=0.9, zorder=5,
               label=f"PT, matched CATS ({len(pt_mat):,})")

    # annotate a few unmatched PT names
    for _, row in pt_miss.head(8).iterrows():
        ax.annotate(
            row["substation_name"], (row["longitude"], row["latitude"]),
            fontsize=5.5, xytext=(3, 3), textcoords="offset points",
            color="#d62728", alpha=0.8,
        )

    ax.legend(fontsize=8, loc="lower right", markerscale=1.3)

    fig.suptitle(
        f"CATS vs Unfiltered Substation Attributes  |  match threshold = {MATCH_KM} km\n"
        f"Total rows: {len(joined):,}  |  "
        f"PT substations: {len(all_pt):,}  |  "
        f"PT matched to CATS: {len(pt_mat):,} ({100*len(pt_mat)/max(1,len(all_pt)):.0f}%)",
        fontsize=10, y=1.01,
    )
    plt.tight_layout()
    out = FIGS_DIR / "cats_unfiltered_comparison.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\nSaved: {out.relative_to(ROOT)}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Loading CATS buses ...")
    cats_sub, cats_nodes = load_cats()
    print(f"  CATS Substation buses : {len(cats_sub):,}")

    print("\nLoading unfiltered substation attributes ...")
    df = load_unfiltered()
    print(f"  Total rows with coords: {len(df):,}")
    print(f"  PT rows               : {df['is_pt'].sum():,}")
    print(f"  In-clean rows         : {df['in_clean'].sum():,}")
    print(f"  Pacificorp rows       : {(df['utility']=='pacificorp').sum():,}")

    print("\nRunning haversine nearest-neighbour join ...")
    joined = join_cats(df, cats_sub)

    print_summary(joined)

    out = OUT_DIR / "cats_unfiltered_join.csv"
    joined.to_csv(out, index=False)
    print(f"\nJoin CSV saved: {out.relative_to(ROOT)}")

    print("\nGenerating figure ...")
    fig_unfiltered_comparison(joined, cats_nodes)
    print("\nDone.")


if __name__ == "__main__":
    main()
