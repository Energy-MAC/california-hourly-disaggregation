"""
compare_feeder_substations.py

Compare the set of substations found in PG&E's feeder data (FeederDetail layer 2)
against three reference datasets to identify substations present in feeder data
but missing from our processed PGE substation set.

Feeder data
-----------
  data/raw/pge/feeders/pge_feeder_detail.csv
  3,032 feeders; 701 unique substation names.
  Coordinates are polyline endpoints (lon_start/lat_start from first vertex of
  first path).  Per-substation coordinates are estimated as the median of all
  feeder start points belonging to that substation.

Reference datasets
------------------
  A) Our clean PGE substations
       data/processed/substations/substation_attributes_clean.csv (utility == 'pge')
       664 substations; name-matched first, then distance fallback.

  B) DataBasin CA Substations 2022
       data/processed/substation_misc/ca_substations_2022.csv (owner_std == 'pge')
       956 PGE records; distance join (threshold 2 km).

  C) CATS buses
       data/raw/PotentialData/CATS/CATS_buses.csv (Type == 'Substation')
       3,171 buses; distance join (threshold 2 km).

Join strategy
-------------
For each of the 701 feeder-derived substations:
  1. Exact name match against our clean PGE list (uppercase, stripped).
  2. If unmatched, haversine nearest-neighbour against the same list.
  3. Independently: haversine join to Basin PGE records.
  4. Independently: haversine join to CATS buses.

Outputs
-------
  data/checks/compare_feeder_substations/feeder_substations.csv
      701 rows — one per unique substation in feeder data, with all join results.

  data/checks/compare_feeder_substations/missing_from_our_pge.csv
      Substations in feeder data not found in our clean PGE set (exact or near match).

  data/figures/substation_maps/feeder_substation_comparison.png
      Map coloured by match status across all three references.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.neighbors import BallTree

# ── Config ────────────────────────────────────────────────────────────────────

ROOT          = Path(__file__).resolve().parents[2]
FEEDER_DETAIL = ROOT / "data" / "raw" / "pge" / "feeders" / "pge_feeder_detail.csv"
PGE_CLEAN     = ROOT / "data" / "processed" / "substations" / "substation_attributes_clean.csv"
BASIN         = ROOT / "data" / "processed" / "substation_misc" / "ca_substations_2022.csv"
CATS_FILE     = ROOT / "data" / "raw" / "PotentialData" / "CATS" / "CATS_buses.csv"
FIGS_DIR      = ROOT / "data" / "figures" / "substation_maps"
OUT_DIR       = ROOT / "data" / "checks" / "compare_feeder_substations"

FIGS_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

MATCH_KM  = 2.0
EARTH_KM  = 6371.0
_CA_LON   = (-124.5, -114.0)
_CA_LAT   = (32.5,    42.0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def nearest_join(
    query_lat: np.ndarray, query_lon: np.ndarray,
    ref_lat:   np.ndarray, ref_lon:   np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    ref_rad   = np.column_stack([np.radians(ref_lat),   np.radians(ref_lon)])
    query_rad = np.column_stack([np.radians(query_lat), np.radians(query_lon)])
    tree = BallTree(ref_rad, metric="haversine")
    dist_rad, idx = tree.query(query_rad, k=1)
    return idx.flatten(), dist_rad.flatten() * EARTH_KM


def norm(s: pd.Series) -> pd.Series:
    return s.str.strip().str.upper()


# ── Load data ─────────────────────────────────────────────────────────────────

def load_feeder_substations() -> pd.DataFrame:
    """
    Aggregate feeder detail to one row per unique substation.
    Geometry was not fetched (polyline responses exceed server limits), so
    coordinates are filled later from Basin name-matching.
    """
    df = pd.read_csv(FEEDER_DETAIL)
    df["substation"] = norm(df["substation"].fillna(""))

    agg = (
        df.groupby("substation", sort=True)
        .agg(
            n_feeders      = ("feeder_id", "count"),
            division       = ("division",  "first"),
            voltages       = ("nominal_voltage_kv", lambda x: ";".join(sorted(x.dropna().astype(str).unique()))),
            total_res_cust = ("res_cust",  "sum"),
            total_com_cust = ("com_cust",  "sum"),
            redacted_any   = ("load_profile_redaction",
                              lambda x: "Yes" if (x == "Yes").any() else "No"),
        )
        .reset_index()
    )
    # Coordinates filled by Basin join below; initialise as NaN
    agg["lat"] = np.nan
    agg["lon"] = np.nan
    return agg


def load_our_pge() -> pd.DataFrame:
    df = pd.read_csv(PGE_CLEAN)
    df = df[df["utility"] == "pge"].copy().reset_index(drop=True)
    df["norm_name"] = norm(df["substation_name"])
    df["lat"] = df["util_lat"].fillna(df["basin_lat"])
    df["lon"] = df["util_lon"].fillna(df["basin_lon"])
    return df


def load_basin_pge() -> pd.DataFrame:
    df = pd.read_csv(BASIN)
    df = df[(df["owner_std"] == "pge") & (df["type"] == "SUBSTATION")].copy()
    return df.reset_index(drop=True)


def load_cats() -> pd.DataFrame:
    df = pd.read_csv(CATS_FILE)
    df["Type"] = df["Type"].str.strip("'")
    return df[df["Type"] == "Substation"].copy().reset_index(drop=True)


# ── Join logic ─────────────────────────────────────────────────────────────────

def join_our_pge(subs: pd.DataFrame, our_pge: pd.DataFrame) -> pd.DataFrame:
    """Exact name match first, then distance fallback for remainders."""
    subs = subs.copy()
    exact_map = dict(zip(our_pge["norm_name"], our_pge.index))
    subs["our_pge_idx"]   = subs["substation"].map(exact_map).fillna(-1).astype(int)
    subs["our_pge_match"] = np.where(subs["our_pge_idx"] >= 0, "exact", "none")

    # Distance fallback for unmatched that have coordinates
    unmatched = (subs["our_pge_match"] == "none")
    has_coord = subs["lat"].notna() & subs["lon"].notna()
    ref_has   = our_pge["lat"].notna() & our_pge["lon"].notna()
    do_dist   = unmatched & has_coord

    if do_dist.sum() and ref_has.sum():
        idx, dist = nearest_join(
            subs.loc[do_dist, "lat"].values,
            subs.loc[do_dist, "lon"].values,
            our_pge.loc[ref_has, "lat"].values,
            our_pge.loc[ref_has, "lon"].values,
        )
        # Map back to original our_pge index
        ref_idx = our_pge.index[ref_has].to_numpy()
        mapped_idx = ref_idx[idx]

        subs.loc[do_dist, "our_pge_dist_km"] = dist
        within = dist <= MATCH_KM
        subs.loc[do_dist & subs.index.isin(subs.index[do_dist][within]),
                 "our_pge_idx"]   = mapped_idx[within]
        subs.loc[do_dist, "our_pge_match"] = np.where(within, "distance", "unmatched")

    # Patch any remaining "none" to "unmatched"
    subs["our_pge_match"] = subs["our_pge_match"].replace("none", "unmatched")

    def _get(col: str) -> pd.Series:
        return subs["our_pge_idx"].apply(
            lambda i: our_pge[col].iloc[i] if i >= 0 else np.nan
        )

    subs["our_pge_name"]       = _get("substation_name")
    subs["our_pge_voltage_kv"] = _get("voltage_kv")

    return subs


def join_basin(subs: pd.DataFrame, basin: pd.DataFrame) -> pd.DataFrame:
    """
    Name-match feeder substations against Basin PGE records.
    Basin coordinates are copied onto matched rows so downstream distance
    joins (e.g., vs CATS) can use them.
    """
    subs = subs.copy()
    basin_norm = norm(basin["name"])
    exact_map  = dict(zip(basin_norm, basin.index))

    subs["basin_idx"]     = subs["substation"].map(exact_map).fillna(-1).astype(int)
    subs["basin_matched"] = subs["basin_idx"] >= 0

    def _get_basin(col: str) -> pd.Series:
        return subs["basin_idx"].apply(
            lambda i: basin[col].iloc[i] if i >= 0 else np.nan
        )

    subs["basin_name"] = _get_basin("name")
    # Use Basin coordinates as the substation location for matched rows
    subs.loc[subs["basin_matched"], "lat"] = _get_basin("latitude")[subs["basin_matched"]]
    subs.loc[subs["basin_matched"], "lon"] = _get_basin("longitude")[subs["basin_matched"]]

    return subs.drop(columns=["basin_idx"])


def join_cats(subs: pd.DataFrame, cats: pd.DataFrame) -> pd.DataFrame:
    """
    Distance join against CATS using Basin-derived coordinates.
    Only rows that matched Basin (and thus have coordinates) can be checked.
    """
    subs = subs.copy()
    has_coord = subs["lat"].notna() & subs["lon"].notna()

    subs["cats_dist_km"] = np.nan
    subs["cats_matched"] = False
    subs["cats_kv"]      = np.nan

    if has_coord.sum():
        idx, dist = nearest_join(
            subs.loc[has_coord, "lat"].values,
            subs.loc[has_coord, "lon"].values,
            cats["Lat"].values,
            cats["Lon"].values,
        )
        subs.loc[has_coord, "cats_dist_km"] = dist
        subs.loc[has_coord, "cats_matched"] = dist <= MATCH_KM
        subs.loc[has_coord, "cats_kv"]      = cats["kV"].iloc[idx].values

    return subs


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(subs: pd.DataFrame) -> None:
    n = len(subs)
    print(f"\n{'='*65}")
    print(f"PG&E Feeder Substations vs Reference Datasets")
    print(f"{'='*65}")
    print(f"\nTotal unique substations in feeder data : {n:,}")
    print(f"Total feeders                           : {subs['n_feeders'].sum():,}")

    print(f"\n--- vs Our clean PGE substations (664) ---")
    for mt, grp in subs.groupby("our_pge_match"):
        print(f"  {mt:<12}: {len(grp):>4}  ({100*len(grp)/n:.1f}%)")

    print(f"\n--- vs Basin PGE substations (distance <= {MATCH_KM} km) ---")
    n_bm = subs["basin_matched"].sum()
    print(f"  matched   : {n_bm:>4}  ({100*n_bm/n:.1f}%)")
    print(f"  unmatched : {n-n_bm:>4}  ({100*(n-n_bm)/n:.1f}%)")

    print(f"\n--- vs CATS buses (distance <= {MATCH_KM} km) ---")
    n_cm = subs["cats_matched"].sum()
    print(f"  matched   : {n_cm:>4}  ({100*n_cm/n:.1f}%)")
    print(f"  unmatched : {n-n_cm:>4}  ({100*(n-n_cm)/n:.1f}%)")

    missing = subs[subs["our_pge_match"] == "unmatched"].sort_values("substation")
    print(f"\nSubstations in feeder data but NOT in our PGE data ({len(missing)}):")
    cols = ["substation", "division", "voltages", "n_feeders",
            "basin_matched", "cats_matched", "basin_name"]
    print(missing[cols].to_string(index=False))


# ── Figure ─────────────────────────────────────────────────────────────────────

def fig_comparison(subs: pd.DataFrame) -> None:
    has_coord = subs["lat"].notna() & subs["lon"].notna()
    df = subs[has_coord].copy()

    exact    = df[df["our_pge_match"] == "exact"]
    distance = df[df["our_pge_match"] == "distance"]
    missing  = df[df["our_pge_match"] == "unmatched"]

    fig, axes = plt.subplots(1, 2, figsize=(18, 9))

    def _setup(ax, title):
        ax.set_xlim(*_CA_LON); ax.set_ylim(*_CA_LAT)
        ax.set_aspect("equal")
        ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.grid(True, alpha=0.25, linewidth=0.5)

    # Panel A: match status vs our PGE data
    ax = axes[0]
    _setup(ax, "Feeder substations vs Our PGE Clean Data")
    ax.scatter(exact["lon"],    exact["lat"],    s=14, color="#2ca02c", alpha=0.7, zorder=3,
               label=f"Exact name match ({len(exact):,})")
    ax.scatter(distance["lon"], distance["lat"], s=14, color="#ff7f0e", alpha=0.7, zorder=3,
               label=f"Distance match <={MATCH_KM}km ({len(distance):,})")
    ax.scatter(missing["lon"],  missing["lat"],  s=30, color="#d62728", marker="^", alpha=0.85, zorder=4,
               label=f"Not in our PGE data ({len(missing):,})")
    ax.legend(fontsize=9, loc="lower right")

    # Panel B: missing vs Basin + CATS
    ax = axes[1]
    _setup(ax, f"Missing substations: Basin & CATS coverage")
    miss_basin = missing[missing["basin_matched"]]
    miss_cats  = missing[missing["cats_matched"]]
    miss_none  = missing[~missing["basin_matched"] & ~missing["cats_matched"]]
    miss_both  = missing[missing["basin_matched"]  &  missing["cats_matched"]]

    ax.scatter(miss_none["lon"],  miss_none["lat"],  s=30, color="#d62728", marker="^", alpha=0.9, zorder=4,
               label=f"Not in Basin or CATS ({len(miss_none):,})")
    ax.scatter(miss_basin["lon"], miss_basin["lat"], s=30, color="#9467bd", marker="^", alpha=0.85, zorder=4,
               label=f"In Basin only ({len(miss_basin)-len(miss_both):,})")
    ax.scatter(miss_cats["lon"],  miss_cats["lat"],  s=30, color="#1f77b4", marker="^", alpha=0.85, zorder=4,
               label=f"In CATS only ({len(miss_cats)-len(miss_both):,})")
    ax.scatter(miss_both["lon"],  miss_both["lat"],  s=40, color="#e377c2", marker="^", alpha=0.9, zorder=5,
               label=f"In both Basin & CATS ({len(miss_both):,})")
    ax.legend(fontsize=9, loc="lower right")

    fig.suptitle(
        f"PG&E Feeder Substations (701 unique)  |  match threshold = {MATCH_KM} km\n"
        f"Triangles = substations in feeder data not found in our clean PGE set",
        fontsize=10, y=1.01,
    )
    plt.tight_layout()
    out = FIGS_DIR / "feeder_substation_comparison.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\nSaved: {out.relative_to(ROOT)}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Loading feeder detail ...")
    subs = load_feeder_substations()
    print(f"  {len(subs):,} unique substations, {subs['n_feeders'].sum():,} total feeders")
    print(f"  {subs['lat'].notna().sum():,} have coordinates")

    print("\nLoading reference datasets ...")
    our_pge   = load_our_pge()
    basin_pge = load_basin_pge()
    cats      = load_cats()
    print(f"  Our PGE clean : {len(our_pge):,}")
    print(f"  Basin PGE     : {len(basin_pge):,}")
    print(f"  CATS buses    : {len(cats):,}")

    print("\nRunning joins ...")
    subs = join_our_pge(subs, our_pge)
    subs = join_basin(subs, basin_pge)
    subs = join_cats(subs, cats)

    print_summary(subs)

    full_out   = OUT_DIR / "feeder_substations.csv"
    miss_out   = OUT_DIR / "missing_from_our_pge.csv"
    subs.to_csv(full_out, index=False)
    subs[subs["our_pge_match"] == "unmatched"].to_csv(miss_out, index=False)
    print(f"\nSaved: {full_out.relative_to(ROOT)}")
    print(f"Saved: {miss_out.relative_to(ROOT)}")

    print("\nGenerating figure ...")
    fig_comparison(subs)
    print("\nDone.")


if __name__ == "__main__":
    main()
