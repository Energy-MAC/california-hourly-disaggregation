"""
Compare substation data sources across utilities and against the DataBasin
CA Substations (2022) reference dataset.

Sections
--------
A  Within-utility coverage
     For each utility, compare the substation name sets across loads,
     attribute sources, and (for SCE) the two competing attribute sources.

B  Name-join vs basin
     Join each utility source to the DataBasin reference on normalised
     substation name, compute haversine distances between matched coordinate
     pairs, and report coverage and distance statistics.

C  Spatial-join vs basin
     For each source that carries lat/lon, find the nearest DataBasin point
     for every substation and report name-agreement rates and distances.

Notes on sources
----------------
  PGE         loads: pge_layer25_earliest_latest_part001.csv  (subname, lat/lon)
              attrs: pge_substation_attributes.csv            (substation_name, lat/lon)

  SCE         loads: sce_combined_raw.csv                     (SUBSTATION; scrape rows have lat/lon)
              attrs: sce_substation_attributes.csv            (substation_name; NO lat/lon)
          attrs_alt: sce_ica_layer_substations_alt.csv        (SUB_NAME, lat/lon)

  SDGE        loads: sdge_substation_profiles_part001.csv     (AssetName, lat/lon)
              attrs: sdge_substation_attributes.csv           (substation_name, lat/lon)
           failures: sdge_substation_profiles_failed.csv      (substation_name)

  Pacificorp  loads: pacificorp_layer1_earliest_latest_part001.csv  (Name, lat/lon; multi-state)
              attrs: pacificorp_substation_attributes.csv            (substation_name; NO lat/lon)

  Basin (ref) data/processed/substation_misc/ca_substations_2022.csv
              4,442 CA substations; owner_std in {pge, sce, sdge, pacificorp, ...}

Outputs
-------
  Console: per-section summary tables
  data/checks/cmp_A_*.csv  - substations only in one source (Section A)
  data/checks/cmp_B_*.csv  - name-join detail tables with distances (Section B)
  data/checks/cmp_C_*.csv  - spatial-join detail tables with distances (Section C)

Usage
-----
  python scripts/compare_substations.py          # all sections
  python scripts/compare_substations.py -s A     # section A only
  python scripts/compare_substations.py -s B,C   # sections B and C
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial import KDTree

ROOT   = Path(__file__).resolve().parents[1]
RAW    = ROOT / "data" / "raw"
PROC   = ROOT / "data" / "processed"
CHECKS = ROOT / "data" / "checks"
CHECKS.mkdir(parents=True, exist_ok=True)

# ── File registry ─────────────────────────────────────────────────────────────

FILE = {
    "basin":         PROC / "substation_misc" / "ca_substations_2022.csv",
    "pge_loads":     RAW  / "pge"        / "pge_layer25_earliest_latest_part001.csv",
    "pge_attrs":     RAW  / "pge"        / "pge_substation_attributes.csv",
    "sce_loads":     RAW  / "sce"        / "sce_combined_raw.csv",
    "sce_attrs":     RAW  / "sce"        / "sce_substation_attributes.csv",
    "sce_attrs_alt": RAW  / "sce"        / "sce_ica_layer_substations_alt.csv",
    "sdge_loads":    RAW  / "sdge"       / "sdge_substation_profiles_part001.csv",
    "sdge_attrs":    RAW  / "sdge"       / "sdge_substation_attributes.csv",
    "sdge_fail":     RAW  / "sdge"       / "sdge_substation_profiles_failed.csv",
    "pac_loads":     RAW  / "pacificorp" / "pacificorp_layer1_earliest_latest_part001.csv",
    "pac_attrs":     RAW  / "pacificorp" / "pacificorp_substation_attributes.csv",
}

# Substation name column for each source
NAME_COL = {
    "basin":         "name",
    "pge_loads":     "subname",
    "pge_attrs":     "substation_name",
    "sce_loads":     "SUBSTATION",
    "sce_attrs":     "substation_name",
    "sce_attrs_alt": "SUB_NAME",
    "sdge_loads":    "AssetName",
    "sdge_attrs":    "substation_name",
    "sdge_fail":     "substation_name",
    "pac_loads":     "Name",
    "pac_attrs":     "substation_name",
}

# Sources that include lat/lon geometry
HAS_LATLON = frozenset({
    "basin", "pge_loads", "pge_attrs",
    "sce_loads",       # scrape rows only (bulk rows have NaN coords)
    "sce_attrs_alt",
    "sdge_loads", "sdge_attrs",
    "pac_loads",
})

# ── Name normalisation ────────────────────────────────────────────────────────

_PT_SUFFIX = re.compile(r"\s+p\.?\s*t\.?\s*$", re.IGNORECASE)
_SUB_WORD  = re.compile(r"\bsubstation\b",      re.IGNORECASE)
_PUNCT     = re.compile(r"[/\-,\.&\(\)_#']")
_SPACES    = re.compile(r"\s+")


def norm(s: pd.Series) -> pd.Series:
    """Normalise substation name series for fuzzy set comparisons."""
    s = s.astype(str).str.strip()
    s = s.str.replace(_PT_SUFFIX, "",  regex=True)
    s = s.str.replace(_SUB_WORD,  "",  regex=True)
    s = s.str.replace(_PUNCT,     " ", regex=True)
    s = s.str.replace(_SPACES,    " ", regex=True)
    return s.str.strip().str.lower()


# ── Geometry helpers ──────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Vectorised haversine distance in km."""
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(np.radians, [
        np.asarray(lat1, dtype=float), np.asarray(lon1, dtype=float),
        np.asarray(lat2, dtype=float), np.asarray(lon2, dtype=float),
    ])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * R * np.arcsin(np.clip(np.sqrt(a), 0.0, 1.0))


def _latlon_to_xyz(lat, lon) -> np.ndarray:
    """Unit-sphere 3D coordinates for lat/lon arrays (degrees)."""
    lat_r = np.radians(np.asarray(lat, dtype=float))
    lon_r = np.radians(np.asarray(lon, dtype=float))
    return np.column_stack([
        np.cos(lat_r) * np.cos(lon_r),
        np.cos(lat_r) * np.sin(lon_r),
        np.sin(lat_r),
    ])


def _chord_to_km(chord: np.ndarray, R: float = 6371.0) -> np.ndarray:
    """Convert Euclidean chord distance to great-circle km."""
    return 2 * R * np.arcsin(np.clip(chord / 2.0, 0.0, 1.0))


# ── DataFrame helpers ─────────────────────────────────────────────────────────

def _load(key: str) -> pd.DataFrame:
    path = FILE[key]
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


def _name_set(df: pd.DataFrame, key: str) -> set:
    if df.empty:
        return set()
    return set(norm(df[NAME_COL[key]].dropna()))


def _unique_locs(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """
    One row per substation: normalised name + lat/lon.
    Drops rows with NaN coordinates, then deduplicates by normalised name
    (keeps first occurrence).
    """
    name_col = NAME_COL[key]
    sub = df[[name_col, "latitude", "longitude"]].copy()
    sub.columns = ["name_raw", "lat", "lon"]
    sub["name_norm"] = norm(sub["name_raw"])
    sub = sub.dropna(subset=["lat", "lon"])
    return sub.groupby("name_norm", as_index=False).first()


def _dist_stats(dist_km: pd.Series | np.ndarray) -> str:
    d = pd.Series(dist_km).dropna()
    if d.empty:
        return "n=0"
    q = d.quantile([0.50, 0.90, 0.99])
    return (
        f"n={len(d):,}  mean={d.mean():.1f} km  "
        f"median={q[0.50]:.1f}  p90={q[0.90]:.1f}  p99={q[0.99]:.1f}  max={d.max():.1f}"
    )


# ── Formatting ────────────────────────────────────────────────────────────────

def _hdr(s: str) -> None:
    width = 72
    print(f"\n{'=' * width}")
    print(f"  {s}")
    print("=" * width)


def _subhdr(s: str) -> None:
    print(f"\n  {'-' * 60}")
    print(f"  {s}")
    print(f"  {'-' * 60}")


def _set_compare(label_a: str, set_a: set, label_b: str, set_b: set) -> dict:
    both   = set_a & set_b
    only_a = set_a - set_b
    only_b = set_b - set_a
    pct_a  = len(both) / len(set_a) * 100 if set_a else 0
    pct_b  = len(both) / len(set_b) * 100 if set_b else 0
    print(
        f"    {label_a:<20} {len(set_a):>5,}  covered by {label_b}: {len(both):,} ({pct_a:.0f}%)"
    )
    print(
        f"    {label_b:<20} {len(set_b):>5,}  covered by {label_a}: {len(both):,} ({pct_b:.0f}%)"
    )
    print(
        f"    only in {label_a}: {len(only_a):,}   |   only in {label_b}: {len(only_b):,}"
    )
    return {"both": both, "only_a": only_a, "only_b": only_b}


def _save(df: pd.DataFrame, filename: str) -> None:
    if not df.empty:
        df.to_csv(CHECKS / filename, index=False)


# ── Section A ─────────────────────────────────────────────────────────────────

def section_a(dfs: dict) -> None:
    _hdr("SECTION A - Within-utility source coverage")
    print("  Compares substation name sets within each utility's own sources.")

    # ── PGE ──────────────────────────────────────────────────────────────────
    _subhdr("PGE")
    pge_loads_s = _name_set(dfs["pge_loads"], "pge_loads")
    pge_attrs_s = _name_set(dfs["pge_attrs"], "pge_attrs")
    r = _set_compare("loads", pge_loads_s, "attrs", pge_attrs_s)

    # Export mismatches
    _save(
        dfs["pge_loads"][dfs["pge_loads"]["subname"].pipe(norm).isin(r["only_a"])]
            .drop_duplicates("subname"),
        "cmp_A_pge_only_in_loads.csv",
    )
    _save(
        dfs["pge_attrs"][dfs["pge_attrs"]["substation_name"].pipe(norm).isin(r["only_b"])],
        "cmp_A_pge_only_in_attrs.csv",
    )

    # ── SCE ──────────────────────────────────────────────────────────────────
    _subhdr("SCE")
    sce_all   = dfs["sce_loads"]
    sce_scrape_df = sce_all[sce_all["source"] == "scrape"]
    sce_bulk_df   = sce_all[sce_all["source"] == "bulk"]

    sce_loads_s     = _name_set(sce_all,       "sce_loads")
    sce_scrape_s    = _name_set(sce_scrape_df, "sce_loads")
    sce_bulk_s      = _name_set(sce_bulk_df,   "sce_loads")
    sce_attrs_s     = _name_set(dfs["sce_attrs"],     "sce_attrs")
    sce_attrs_alt_s = _name_set(dfs["sce_attrs_alt"], "sce_attrs_alt")

    # Count summary
    print(f"    {'source':<25} {'n':>5}  notes")
    print(f"    {'loads (combined)':<25} {len(sce_loads_s):>5,}")
    print(f"    {'loads (scrape)':<25} {len(sce_scrape_s):>5,}  lat/lon available")
    print(f"    {'loads (bulk download)':<25} {len(sce_bulk_s):>5,}  no lat/lon")
    print(f"    {'attrs (Table 3)':<25} {len(sce_attrs_s):>5,}  circuit/customer mix, no lat/lon")
    print(f"    {'attrs_alt (ICA_Layer)':<25} {len(sce_attrs_alt_s):>5,}  SUB_TYPE, SUBSTATION_VOLTAGE, lat/lon")
    print()

    # Load source comparison
    print("    Scrape vs Bulk  [which substations each load source covers]:")
    r_sb = _set_compare("scrape", sce_scrape_s, "bulk", sce_bulk_s)
    print()

    # Each load source vs attributes
    print("    Scrape vs Table 3:")
    r_slt = _set_compare("scrape", sce_scrape_s, "T3", sce_attrs_s)
    print()
    print("    Bulk vs Table 3:")
    r_blt = _set_compare("bulk", sce_bulk_s, "T3", sce_attrs_s)
    print()
    print("    Scrape vs Alt (ICA_Layer):")
    r_sla = _set_compare("scrape", sce_scrape_s, "alt", sce_attrs_alt_s)
    print()
    print("    Bulk vs Alt (ICA_Layer):")
    r_bla = _set_compare("bulk", sce_bulk_s, "alt", sce_attrs_alt_s)
    print()

    # Combined loads vs attributes (kept for reference)
    print("    Combined loads vs Table 3:")
    r_lt = _set_compare("loads", sce_loads_s, "T3", sce_attrs_s)
    print()
    print("    Combined loads vs Alt (ICA_Layer):")
    r_la = _set_compare("loads", sce_loads_s, "alt", sce_attrs_alt_s)
    print()
    print("    Table 3 vs Alt (ICA_Layer)  [attribute source comparison]:")
    r_ta = _set_compare("T3", sce_attrs_s, "alt", sce_attrs_alt_s)

    # Export mismatches
    def _sce_export(df, col, names, filename):
        _save(df[df[col].pipe(norm).isin(names)].drop_duplicates(col), filename)

    # Scrape vs bulk
    _sce_export(sce_scrape_df, "SUBSTATION", r_sb["only_a"], "cmp_A_sce_only_in_scrape_vs_bulk.csv")
    _sce_export(sce_bulk_df,   "SUBSTATION", r_sb["only_b"], "cmp_A_sce_only_in_bulk_vs_scrape.csv")

    # Combined loads vs attributes
    _sce_export(sce_all,            "SUBSTATION",     r_lt["only_a"], "cmp_A_sce_only_in_loads_vs_t3.csv")
    _sce_export(dfs["sce_attrs"],   "substation_name",r_lt["only_b"], "cmp_A_sce_only_in_t3_vs_loads.csv")
    _sce_export(sce_all,            "SUBSTATION",     r_la["only_a"], "cmp_A_sce_only_in_loads_vs_alt.csv")
    _sce_export(dfs["sce_attrs_alt"],"SUB_NAME",      r_la["only_b"], "cmp_A_sce_only_in_alt_vs_loads.csv")
    _sce_export(dfs["sce_attrs"],   "substation_name",r_ta["only_a"], "cmp_A_sce_only_in_t3_vs_alt.csv")
    _sce_export(dfs["sce_attrs_alt"],"SUB_NAME",      r_ta["only_b"], "cmp_A_sce_only_in_alt_vs_t3.csv")

    # Intersection summary
    triple = sce_loads_s & sce_attrs_s & sce_attrs_alt_s
    scrape_both_attrs = sce_scrape_s & (sce_attrs_s | sce_attrs_alt_s)
    bulk_both_attrs   = sce_bulk_s   & (sce_attrs_s | sce_attrs_alt_s)
    print(f"\n    In all three sources (combined loads & T3 & alt): {len(triple):,}")
    print(f"    Scrape in at least one attr source: {len(scrape_both_attrs):,}")
    print(f"    Bulk   in at least one attr source: {len(bulk_both_attrs):,}")

    # ── SDGE ─────────────────────────────────────────────────────────────────
    _subhdr("SDGE")
    sdge_loads_s = _name_set(dfs["sdge_loads"], "sdge_loads")
    sdge_attrs_s = _name_set(dfs["sdge_attrs"], "sdge_attrs")
    sdge_fail_s  = _name_set(dfs["sdge_fail"],  "sdge_fail")

    print(f"    Failures (scrape errors, excluded from gap counts): {len(sdge_fail_s):,}")
    sdge_loads_ex = sdge_loads_s - sdge_fail_s
    sdge_attrs_ex = sdge_attrs_s - sdge_fail_s
    r_sdge = _set_compare("loads-ex-fail", sdge_loads_ex, "attrs-ex-fail", sdge_attrs_ex)

    _save(
        dfs["sdge_loads"][dfs["sdge_loads"]["AssetName"].pipe(norm).isin(r_sdge["only_a"])]
            .drop_duplicates("AssetName"),
        "cmp_A_sdge_only_in_loads.csv",
    )
    _save(
        dfs["sdge_attrs"][dfs["sdge_attrs"]["substation_name"].pipe(norm).isin(r_sdge["only_b"])],
        "cmp_A_sdge_only_in_attrs.csv",
    )

    # ── Pacificorp ────────────────────────────────────────────────────────────
    _subhdr("Pacificorp  (multi-state; basin only covers CA)")
    pac_loads_s = _name_set(dfs["pac_loads"], "pac_loads")
    pac_attrs_s = _name_set(dfs["pac_attrs"], "pac_attrs")
    _set_compare("loads", pac_loads_s, "attrs", pac_attrs_s)

    sub_types = dfs["pac_loads"]["Sub_Type"].value_counts()
    print(f"\n    Sub_Type breakdown in loads: {sub_types.to_dict()}")


# ── Section B ─────────────────────────────────────────────────────────────────

def _name_join_single(
    df: pd.DataFrame, key: str,
    basin_sub: pd.DataFrame,
    label: str,
) -> None:
    """Name-join one utility source against the basin subset."""
    util_names  = _name_set(df, key)
    basin_names = set(norm(basin_sub["name"].dropna()))
    both        = util_names & basin_names
    only_util   = util_names - basin_names
    only_basin  = basin_names - util_names
    pct_u = len(both) / len(util_names)  * 100 if util_names  else 0
    pct_b = len(both) / len(basin_names) * 100 if basin_names else 0

    print(f"\n  {label}")
    print(f"    Basin:  {len(basin_names):,}  |  Source: {len(util_names):,}  |  "
          f"Matched: {len(both):,} ({pct_u:.0f}% of source, {pct_b:.0f}% of basin)")
    print(f"    Only in source: {len(only_util):,}   |   Only in basin: {len(only_basin):,}")

    # Distance stats if this source carries lat/lon
    if key in HAS_LATLON and both:
        util_locs  = _unique_locs(df, key)
        basin_locs = _unique_locs(basin_sub, "basin")

        if util_locs.empty:
            print("    (lat/lon columns present but all NaN in this subset)")
        else:
            merged = pd.merge(
                basin_locs[["name_norm", "lat", "lon", "name_raw"]],
                util_locs[ ["name_norm", "lat", "lon", "name_raw"]],
                on="name_norm", suffixes=("_basin", "_util"), how="inner",
            )
            merged["dist_km"] = haversine_km(
                merged["lat_basin"], merged["lon_basin"],
                merged["lat_util"],  merged["lon_util"],
            )
            print(f"    Distance (name-matched pairs): {_dist_stats(merged['dist_km'])}")
            pct_1km = (merged["dist_km"] < 1).mean() * 100
            pct_5km = (merged["dist_km"] < 5).mean() * 100
            print(f"    Within 1 km: {pct_1km:.1f}%   Within 5 km: {pct_5km:.1f}%")
            _save(merged, f"cmp_B_{label}_name_join.csv")
    elif key not in HAS_LATLON:
        print("    (No lat/lon in this source - distance stats not available)")

    # Save only-in-source list for inspection
    only_u_rows = df[df[NAME_COL[key]].pipe(norm).isin(only_util)].drop_duplicates(NAME_COL[key])
    _save(only_u_rows, f"cmp_B_{label}_only_in_source.csv")


def section_b(dfs: dict) -> None:
    _hdr("SECTION B - Name-join vs DataBasin reference")
    print("  Joins each source to the basin on normalised substation name.")
    print("  For sources with lat/lon: reports haversine distances between matched pairs.")
    basin = dfs["basin"]

    # ── PGE ──────────────────────────────────────────────────────────────────
    _subhdr("PGE")
    basin_pge = basin[basin["owner_std"] == "pge"]
    _name_join_single(dfs["pge_attrs"], "pge_attrs", basin_pge, "pge_attrs")
    _name_join_single(dfs["pge_loads"], "pge_loads", basin_pge, "pge_loads")

    # ── SCE ──────────────────────────────────────────────────────────────────
    _subhdr("SCE")
    basin_sce = basin[basin["owner_std"] == "sce"]
    _name_join_single(dfs["sce_attrs"],     "sce_attrs",     basin_sce, "sce_attrs_t3")
    _name_join_single(dfs["sce_attrs_alt"], "sce_attrs_alt", basin_sce, "sce_attrs_alt")

    sce_all    = dfs["sce_loads"]
    sce_scrape = sce_all[sce_all["source"] == "scrape"]
    sce_bulk   = sce_all[sce_all["source"] == "bulk"]

    # Scrape vs basin (has lat/lon)
    print(f"\n  sce_loads_scrape  ({sce_scrape['SUBSTATION'].nunique()} unique substations, lat/lon available)")
    _name_join_single(sce_scrape, "sce_loads", basin_sce, "sce_loads_scrape")

    # Bulk vs basin (no lat/lon)
    print(f"\n  sce_loads_bulk  ({sce_bulk['SUBSTATION'].nunique()} unique substations, no lat/lon)")
    _name_join_single(sce_bulk, "sce_loads", basin_sce, "sce_loads_bulk")

    # Combined loads vs basin (kept for reference)
    print(f"\n  sce_loads_combined  ({sce_all['SUBSTATION'].nunique()} unique substations across both sources)")
    _name_join_single(sce_all, "sce_loads", basin_sce, "sce_loads_combined")

    # ── SDGE ─────────────────────────────────────────────────────────────────
    _subhdr("SDGE")
    basin_sdge = basin[basin["owner_std"] == "sdge"]
    _name_join_single(dfs["sdge_attrs"], "sdge_attrs", basin_sdge, "sdge_attrs")
    _name_join_single(dfs["sdge_loads"], "sdge_loads", basin_sdge, "sdge_loads")

    # ── Pacificorp ────────────────────────────────────────────────────────────
    _subhdr("Pacificorp  (basin is CA-only; most pac substations are out of CA)")
    basin_pac = basin[basin["owner_std"] == "pacificorp"]
    print(f"    Basin (CA, pacificorp): {len(basin_pac):,} substations")
    _name_join_single(dfs["pac_attrs"], "pac_attrs", basin_pac, "pac_attrs")
    _name_join_single(dfs["pac_loads"], "pac_loads", basin_pac, "pac_loads")


# ── Section C ─────────────────────────────────────────────────────────────────

def _spatial_join_single(
    df: pd.DataFrame, key: str,
    basin_sub: pd.DataFrame,
    label: str,
    max_dist_km: float = 50.0,
) -> None:
    """Nearest-neighbour join from utility source to basin subset."""
    util_locs  = _unique_locs(df, key)
    basin_locs = _unique_locs(basin_sub, "basin")

    print(f"\n  {label}")
    if util_locs.empty or basin_locs.empty:
        print("    Insufficient data for spatial join.")
        return

    # Build KDTree on 3D unit sphere from basin points
    basin_xyz = _latlon_to_xyz(basin_locs["lat"].values, basin_locs["lon"].values)
    util_xyz  = _latlon_to_xyz(util_locs["lat"].values,  util_locs["lon"].values)
    tree = KDTree(basin_xyz)
    chord, idx = tree.query(util_xyz, k=1)
    dist_km = _chord_to_km(chord)

    result = pd.DataFrame({
        "util_name":   util_locs["name_raw"].values,
        "util_norm":   util_locs["name_norm"].values,
        "util_lat":    util_locs["lat"].values,
        "util_lon":    util_locs["lon"].values,
        "basin_name":  basin_locs["name_raw"].iloc[idx].values,
        "basin_norm":  basin_locs["name_norm"].iloc[idx].values,
        "basin_lat":   basin_locs["lat"].iloc[idx].values,
        "basin_lon":   basin_locs["lon"].iloc[idx].values,
        "dist_km":     dist_km,
    })
    result["name_match"] = result["util_norm"] == result["basin_norm"]

    within = result[result["dist_km"] <= max_dist_km]
    n_total  = len(result)
    n_within = len(within)
    n_agree  = int(within["name_match"].sum()) if n_within > 0 else 0

    print(f"    {n_total:,} utility points matched to nearest basin point")
    print(f"    Within {max_dist_km:.0f} km: {n_within:,} pairs")
    if n_within > 0:
        print(f"    Name agreement (within {max_dist_km:.0f} km): "
              f"{n_agree}/{n_within} = {n_agree/n_within*100:.1f}%")
    print(f"    Distance to nearest basin point: {_dist_stats(result['dist_km'])}")

    # Agreement by distance band
    print("    Agreement by distance band:")
    bands = [(0, 1), (1, 5), (5, 20), (20, max_dist_km)]
    for lo, hi in bands:
        band = within[(within["dist_km"] >= lo) & (within["dist_km"] < hi)]
        if len(band) == 0:
            continue
        ag = int(band["name_match"].sum())
        print(f"      {lo:>4}-{hi:<4} km : {len(band):>4,} pairs  "
              f"name-agree: {ag}/{len(band)} ({ag/len(band)*100:.0f}%)")

    _save(result.sort_values("dist_km"), f"cmp_C_{label}_spatial_join.csv")


def section_c(dfs: dict) -> None:
    _hdr("SECTION C - Spatial nearest-neighbour join vs DataBasin reference")
    print("  For each source with lat/lon, finds the nearest basin point per substation.")
    print("  Reports how often the name of the nearest basin point matches the source name.")
    basin = dfs["basin"]

    # ── PGE ──────────────────────────────────────────────────────────────────
    _subhdr("PGE")
    basin_pge = basin[basin["owner_std"] == "pge"]
    _spatial_join_single(dfs["pge_attrs"], "pge_attrs", basin_pge, "pge_attrs")
    _spatial_join_single(dfs["pge_loads"], "pge_loads", basin_pge, "pge_loads")

    # ── SCE ──────────────────────────────────────────────────────────────────
    _subhdr("SCE")
    basin_sce = basin[basin["owner_std"] == "sce"]
    _spatial_join_single(dfs["sce_attrs_alt"], "sce_attrs_alt", basin_sce, "sce_attrs_alt")

    # SCE loads scrape (has lat/lon); bulk has no lat/lon so spatial join is not applicable
    sce_scrape = dfs["sce_loads"][dfs["sce_loads"]["source"] == "scrape"]
    n_scrape = sce_scrape["SUBSTATION"].nunique()
    n_bulk   = (dfs["sce_loads"]["source"] == "bulk").sum()
    print(f"\n  sce_loads_scrape  ({n_scrape} unique substations with lat/lon)")
    print(f"  [bulk rows ({n_bulk:,} rows) omitted - no lat/lon; see Section B for bulk name coverage]")
    _spatial_join_single(sce_scrape, "sce_loads", basin_sce, "sce_loads_scrape")

    # ── SDGE ─────────────────────────────────────────────────────────────────
    _subhdr("SDGE")
    basin_sdge = basin[basin["owner_std"] == "sdge"]
    _spatial_join_single(dfs["sdge_attrs"], "sdge_attrs", basin_sdge, "sdge_attrs")
    _spatial_join_single(dfs["sdge_loads"], "sdge_loads", basin_sdge, "sdge_loads")

    # ── Pacificorp ────────────────────────────────────────────────────────────
    _subhdr("Pacificorp  (basin is CA-only; most pac substations are outside CA)")
    basin_pac = basin[basin["owner_std"] == "pacificorp"]
    print(f"    Basin (CA, pacificorp): {len(basin_pac):,} substations")
    print("    NOTE: many pacificorp loads substations are in Utah/Oregon; "
          "large distances expected.")
    _spatial_join_single(dfs["pac_loads"], "pac_loads", basin_pac, "pac_loads")


# ── Data loading ──────────────────────────────────────────────────────────────

def load_all() -> dict[str, pd.DataFrame]:
    print("Loading data sources ...")
    dfs: dict[str, pd.DataFrame] = {}
    for key, path in FILE.items():
        if path.exists():
            dfs[key] = pd.read_csv(path, low_memory=False)
            ncol = NAME_COL.get(key)
            n = dfs[key][ncol].nunique() if ncol and ncol in dfs[key].columns else "?"
            print(f"  {key:<20} {len(dfs[key]):>8,} rows  {n:>5} unique names")
        else:
            dfs[key] = pd.DataFrame()
            print(f"  {key:<20}   [FILE NOT FOUND: {path.relative_to(ROOT)}]")
    return dfs


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-s", "--section",
        default="A,B,C",
        metavar="SECTIONS",
        help="Comma-separated sections to run: A, B, C (default: A,B,C)",
    )
    args = parser.parse_args()
    sections = {s.strip().upper() for s in args.section.split(",")}

    dfs = load_all()

    if "A" in sections:
        section_a(dfs)
    if "B" in sections:
        section_b(dfs)
    if "C" in sections:
        section_c(dfs)

    print(f"\n{'=' * 72}")
    print(f"  Done. Exported CSVs -> {CHECKS.relative_to(ROOT)}/")
    print("=" * 72)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
