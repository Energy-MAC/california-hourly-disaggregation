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

E  SCE bulk download vs scrape load values
     For substations and time-points present in BOTH the bulk download and
     the web-scraped data (inner join on SUBSTATION + YEAR + MONTH + HOUR),
     compare MIN_LOAD and MAX_LOAD (MW).  Reports per-substation disagreement
     statistics and saves a row-level detail file for manual inspection.

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

Dictionary augmentation
-----------------------
  data/basinSourceDictionary.csv maps utility source names that could not be
  matched by normalised name to their corresponding basin names (e.g.
  "CRESTA PH" -> "Cresta", "DRUM" -> "Drum 1" / "Drum 2").  This dictionary
  was built by manual inspection of the Section D remainders and automated
  candidate suggestions from scripts/find_basin_name_candidates.py.

  In Sections B and D, after the standard normalised-name join, any source
  substations still unmatched are looked up in the dictionary.  A substation
  counts as "matched via dictionary" when its norm(SourceName) appears in the
  dictionary AND the corresponding norm(BasinName) exists in the basin dataset.
  Remainders reported and exported always reflect this two-step matching.

Outputs
-------
  Console: per-section summary tables
  data/checks/cmp_A_*.csv  - substations only in one source (Section A)
  data/checks/cmp_B_*.csv  - name-join detail tables; *_only_in_source.csv
                             files show substations unmatched by name OR dict
  data/checks/cmp_C_*.csv  - spatial-join detail tables with distances (Section C)
  data/checks/cmp_D_*.csv  - ID-join details and remainders after name + ID +
                             dict matching
  data/checks/cmp_E_sce_bulk_vs_scrape_detail.csv
                           - all inner-joined rows with bulk/scrape MIN/MAX and diffs
  data/checks/cmp_E_sce_bulk_vs_scrape_by_sub.csv
                           - per-substation summary: n_rows, n_diff, pct_diff, mean/max
                             absolute differences, zero-load counts

Usage
-----
  python scripts/data/substations/compare_substations.py          # all sections
  python scripts/data/substations/compare_substations.py -s A     # section A only
  python scripts/data/substations/compare_substations.py -s B,C   # sections B and C
  python scripts/data/substations/compare_substations.py -s E     # SCE load value check
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd
from scipy.spatial import KDTree

ROOT      = Path(__file__).resolve().parents[3]
RAW       = ROOT / "data" / "raw"
PROC      = ROOT / "data" / "processed"
CHECKS    = ROOT / "data" / "checks" / "compare_substations"
FIGS_SCE  = ROOT / "data" / "figures" / "sce_vintage_analysis"
DICT_PATH = ROOT / "data" / "basinSourceDictionary.csv"
CHECKS.mkdir(parents=True, exist_ok=True)
FIGS_SCE.mkdir(parents=True, exist_ok=True)

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


def _norm1(s: str) -> str:
    """Scalar wrapper around norm() for single-string use."""
    return norm(pd.Series([s]))[0]


def _load_dict_all() -> dict[str, dict[str, list[str]]]:
    """
    Load data/basinSourceDictionary.csv and return a nested mapping:
      { utility_lower: { norm(SourceName): [norm(BasinName), ...] } }

    Returns an empty dict if the file does not exist.  One SourceName can map
    to multiple BasinNames (e.g. DRUM -> ["drum 1", "drum 2"]).
    """
    if not DICT_PATH.exists():
        return {}
    d = pd.read_csv(DICT_PATH)
    result: dict[str, dict[str, list[str]]] = {}
    for _, row in d.iterrows():
        util   = str(row["Utility"]).strip().lower()
        src_n  = _norm1(str(row["SourceName"]))
        bas_n  = _norm1(str(row["BasinName"]))
        result.setdefault(util, {}).setdefault(src_n, []).append(bas_n)
    return result


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
        try:
            df.to_csv(CHECKS / filename, index=False)
        except PermissionError:
            print(f"    WARNING: {filename} is locked (open elsewhere); skipping write.")


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
    dict_map: dict[str, list[str]] | None = None,
) -> None:
    """
    Name-join one utility source against the basin subset.

    dict_map, if provided, is used as a fallback for source substations that
    did not match by normalised name.  A source substation counts as
    "matched via dictionary" when its norm(SourceName) appears in dict_map
    AND at least one of the mapped norm(BasinName) values is present in the
    basin dataset.  The *_only_in_source.csv output excludes both name-matched
    and dict-matched substations.
    """
    util_names  = _name_set(df, key)
    basin_names = set(norm(basin_sub["name"].dropna()))
    both        = util_names & basin_names
    only_util   = util_names - basin_names
    only_basin  = basin_names - util_names
    pct_u = len(both) / len(util_names)  * 100 if util_names  else 0
    pct_b = len(both) / len(basin_names) * 100 if basin_names else 0

    # Dictionary augmentation: how many of the source-only names can be matched
    # via the basin source dictionary?
    dict_matched: set[str] = set()
    if dict_map:
        for src_n in only_util:
            bas_norms = dict_map.get(src_n, [])
            if any(bn in basin_names for bn in bas_norms):
                dict_matched.add(src_n)

    total_matched = len(both) + len(dict_matched)
    only_util_after_dict = only_util - dict_matched
    pct_u_total = total_matched / len(util_names) * 100 if util_names else 0

    print(f"\n  {label}")
    print(f"    Basin:  {len(basin_names):,}  |  Source: {len(util_names):,}  |  "
          f"Matched by name: {len(both):,} ({pct_u:.0f}% of source, {pct_b:.0f}% of basin)")
    if dict_matched:
        print(f"    Additionally matched via dictionary: {len(dict_matched)} "
              f"-> total {total_matched:,} ({pct_u_total:.0f}% of source)")
    print(f"    Only in source (after dict): {len(only_util_after_dict):,}   |   "
          f"Only in basin: {len(only_basin):,}")

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

    # Save only-in-source list (after both name and dictionary matching)
    only_u_rows = (df[df[NAME_COL[key]].pipe(norm).isin(only_util_after_dict)]
                   .drop_duplicates(NAME_COL[key]))
    _save(only_u_rows, f"cmp_B_{label}_only_in_source.csv")


def section_b(dfs: dict, dict_all: dict | None = None) -> None:
    _hdr("SECTION B - Name-join vs DataBasin reference")
    print("  Joins each source to the basin on normalised substation name.")
    print("  A fallback dictionary match is applied for remaining non-matches.")
    print("  For sources with lat/lon: reports haversine distances between matched pairs.")
    basin    = dfs["basin"]
    dict_all = dict_all or {}

    # ── PGE ──────────────────────────────────────────────────────────────────
    _subhdr("PGE")
    basin_pge = basin[basin["owner_std"] == "pge"]
    dict_pge  = dict_all.get("pge", {})
    _name_join_single(dfs["pge_attrs"], "pge_attrs", basin_pge, "pge_attrs", dict_pge)
    _name_join_single(dfs["pge_loads"], "pge_loads", basin_pge, "pge_loads", dict_pge)

    # ── SCE ──────────────────────────────────────────────────────────────────
    _subhdr("SCE")
    basin_sce = basin[basin["owner_std"] == "sce"]
    dict_sce  = dict_all.get("sce", {})
    _name_join_single(dfs["sce_attrs"],     "sce_attrs",     basin_sce, "sce_attrs_t3",  dict_sce)
    _name_join_single(dfs["sce_attrs_alt"], "sce_attrs_alt", basin_sce, "sce_attrs_alt", dict_sce)

    sce_all    = dfs["sce_loads"]
    sce_scrape = sce_all[sce_all["source"] == "scrape"]
    sce_bulk   = sce_all[sce_all["source"] == "bulk"]

    # Scrape vs basin (has lat/lon)
    print(f"\n  sce_loads_scrape  ({sce_scrape['SUBSTATION'].nunique()} unique substations, lat/lon available)")
    _name_join_single(sce_scrape, "sce_loads", basin_sce, "sce_loads_scrape", dict_sce)

    # Bulk vs basin (no lat/lon)
    print(f"\n  sce_loads_bulk  ({sce_bulk['SUBSTATION'].nunique()} unique substations, no lat/lon)")
    _name_join_single(sce_bulk, "sce_loads", basin_sce, "sce_loads_bulk", dict_sce)

    # Combined loads vs basin (kept for reference)
    print(f"\n  sce_loads_combined  ({sce_all['SUBSTATION'].nunique()} unique substations across both sources)")
    _name_join_single(sce_all, "sce_loads", basin_sce, "sce_loads_combined", dict_sce)

    # ── SDGE ─────────────────────────────────────────────────────────────────
    _subhdr("SDGE")
    basin_sdge = basin[basin["owner_std"] == "sdge"]
    dict_sdge  = dict_all.get("sdge", {})
    _name_join_single(dfs["sdge_attrs"], "sdge_attrs", basin_sdge, "sdge_attrs", dict_sdge)
    _name_join_single(dfs["sdge_loads"], "sdge_loads", basin_sdge, "sdge_loads", dict_sdge)

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


# ── Section D ─────────────────────────────────────────────────────────────────

def section_d(dfs: dict, dict_all: dict | None = None) -> None:
    """
    ID-based name disambiguation joins, followed by dictionary augmentation.

    Basin uses HIFLD IDs (300001+); utility sources use internal IDs that do not
    overlap with basin, so there is no direct utility-ID -> basin-ID join.

    Join strategy (three steps, applied in order):
      Step 1 — exact normalised-name join (same as Section B).
      Step 2 — two-hop ID join:
        PGE:  loads.subid == attrs.substation_id  (664/664 overlap)
              -> try norm(attrs_name) in basin when norm(loads_name) missed
        SCE:  T3.subst_id == alt.SUBST_ID  (675/735 overlap)
              -> try norm(T3_name) in basin when norm(alt_name) missed
        SDGE: no ID columns; skipped.
      Step 3 — dictionary lookup via data/basinSourceDictionary.csv:
        Source names still unmatched after steps 1-2 are looked up in the
        dictionary.  A match is counted when the mapped basin name exists in
        the basin dataset.  Basin remainders also exclude entries that are
        targeted by dictionary entries for in-data source names.

    Outputs
    -------
      cmp_D_pge_id_name_pairs.csv        all loads<->attrs ID pairs (with basin flags)
      cmp_D_pge_new_via_id.csv           loads substations newly linked to basin via attrs name
      cmp_D_pge_loads_remainder.csv      loads substations unmatched after name + ID + dict
      cmp_D_sce_t3_alt_id_pairs.csv      all T3<->alt ID pairs (with basin flags)
      cmp_D_sce_new_via_t3.csv           alt substations newly linked via T3 name
      cmp_D_sce_alt_remainder.csv        alt substations unmatched after name + ID + dict
      cmp_D_sdge_attrs_remainder.csv     SDGE attrs unmatched after name + dict
      cmp_D_sdge_loads_remainder.csv     SDGE loads unmatched after name + dict
      cmp_D_basin_{pge,sce,sdge}_remainder.csv  basin substations not matched by any
                                                utility source (name or dict)
    """
    _hdr("SECTION D - Substation ID joins + dictionary augmentation")
    print("  Step 1: normalised-name join.  Step 2: two-hop ID join.")
    print("  Step 3: dictionary fallback via data/basinSourceDictionary.csv.")

    basin    = dfs["basin"]
    dict_all = dict_all or {}

    # ── PGE ──────────────────────────────────────────────────────────────────
    _subhdr("PGE: loads.subid <-> attrs.substation_id")

    pge_l = dfs["pge_loads"].copy()
    pge_a = dfs["pge_attrs"].copy()
    basin_pge   = basin[basin["owner_std"] == "pge"]
    basin_norms = set(norm(basin_pge["name"].dropna()))

    # One row per unique substation
    pge_l_subs = (pge_l[["subname", "subid", "latitude", "longitude"]]
                  .drop_duplicates("subid").copy())
    pge_l_subs["loads_norm"] = norm(pge_l_subs["subname"])

    pge_a_subs = (pge_a[["substation_name", "substation_id", "latitude", "longitude"]]
                  .drop_duplicates("substation_id").copy())
    pge_a_subs["attrs_norm"] = norm(pge_a_subs["substation_name"])

    # Inner join by ID
    id_pairs = pd.merge(
        pge_l_subs.rename(columns={"subid": "sub_id", "subname": "loads_name",
                                    "latitude": "loads_lat", "longitude": "loads_lon"}),
        pge_a_subs.rename(columns={"substation_id": "sub_id",
                                    "substation_name": "attrs_name",
                                    "latitude": "attrs_lat", "longitude": "attrs_lon"}),
        on="sub_id",
    )
    id_pairs["loads_in_basin"] = id_pairs["loads_norm"].isin(basin_norms)
    id_pairs["attrs_in_basin"] = id_pairs["attrs_norm"].isin(basin_norms)
    id_pairs["names_agree"]    = id_pairs["loads_norm"] == id_pairs["attrs_norm"]

    mismatches     = id_pairs[~id_pairs["names_agree"]]
    new_via_attrs  = id_pairs[~id_pairs["loads_in_basin"] & id_pairs["attrs_in_basin"]]
    conflicts      = mismatches[mismatches["loads_in_basin"] & mismatches["attrs_in_basin"]]
    # coords distance for loads<->attrs (same physical station)
    from_lat = id_pairs["loads_lat"].values.astype(float)
    from_lon = id_pairs["loads_lon"].values.astype(float)
    to_lat   = id_pairs["attrs_lat"].values.astype(float)
    to_lon   = id_pairs["attrs_lon"].values.astype(float)
    id_pairs["loads_attrs_dist_km"] = haversine_km(from_lat, from_lon, to_lat, to_lon)

    # Also: for new_via_attrs, compute distance to basin
    basin_pge_locs = (basin_pge[["name", "latitude", "longitude"]].copy()
                      .assign(name_norm=lambda d: norm(d["name"]))
                      .drop_duplicates("name_norm")
                      .set_index("name_norm"))
    new_via_attrs  = new_via_attrs.copy()
    new_via_attrs["basin_lat"] = new_via_attrs["attrs_norm"].map(basin_pge_locs["latitude"])
    new_via_attrs["basin_lon"] = new_via_attrs["attrs_norm"].map(basin_pge_locs["longitude"])
    new_via_attrs["dist_to_basin_km"] = haversine_km(
        new_via_attrs["attrs_lat"].astype(float),
        new_via_attrs["attrs_lon"].astype(float),
        new_via_attrs["basin_lat"].astype(float),
        new_via_attrs["basin_lon"].astype(float),
    )

    # Remainder: loads not matched by name OR by ID disambiguation
    loads_in_basin_name  = set(id_pairs[id_pairs["loads_in_basin"]]["loads_norm"])
    loads_in_basin_id    = set(new_via_attrs["loads_norm"])
    all_loads_norms      = set(id_pairs["loads_norm"])
    remainder_norms_pge  = all_loads_norms - loads_in_basin_name - loads_in_basin_id

    # Step 3: dictionary augmentation for PGE
    dict_pge = dict_all.get("pge", {})
    loads_in_basin_dict_pge = {
        n for n in remainder_norms_pge
        if any(bn in basin_norms for bn in dict_pge.get(n, []))
    }
    remainder_norms = remainder_norms_pge - loads_in_basin_dict_pge

    remainder_df = (id_pairs[id_pairs["loads_norm"].isin(remainder_norms)]
                    [["loads_name", "loads_norm", "attrs_name", "attrs_norm",
                      "sub_id", "loads_lat", "loads_lon", "attrs_lat", "attrs_lon",
                      "loads_attrs_dist_km"]]
                    .sort_values("loads_name"))

    print(f"    All ID pairs (loads <-> attrs): {len(id_pairs)}")
    print(f"    Same norm name (ID + name agree): {id_pairs['names_agree'].sum()}")
    print(f"    Name mismatches (same ID, different norm name): {len(mismatches)}")
    print(f"      Loads matched basin by name:  {id_pairs['loads_in_basin'].sum()}")
    print(f"      Attrs matched basin by name:  {id_pairs['attrs_in_basin'].sum()}")
    print(f"      New via attrs name (loads miss, attrs hit):  {len(new_via_attrs)}")
    print(f"      Conflicts (both match basin, different names): {len(conflicts)}")
    if len(new_via_attrs) > 0:
        d = new_via_attrs["dist_to_basin_km"].dropna()
        print(f"      New-via-ID dist to basin: median={d.median():.1f} km  "
              f"p90={d.quantile(0.9):.1f} km  max={d.max():.1f} km")
    print(f"    Additionally matched via dictionary: {len(loads_in_basin_dict_pge)}")
    print(f"    Remainder (unmatched after name + ID + dict): {len(remainder_df)}")
    d2 = id_pairs["loads_attrs_dist_km"].dropna()
    print(f"    Loads<->attrs coord distance: median={d2.median():.2f} km  "
          f"p90={d2.quantile(0.9):.1f} km  max={d2.max():.1f} km")

    if not conflicts.empty:
        print("\n    CONFLICTS (same ID, both names match basin but to different records):")
        for _, row in conflicts.iterrows():
            print(f"      subid={row['sub_id']}  loads='{row['loads_name']}' "
                  f"-> basin '{row['loads_norm']}'  |  "
                  f"attrs='{row['attrs_name']}' -> basin '{row['attrs_norm']}'")

    _save(id_pairs,     "cmp_D_pge_id_name_pairs.csv")
    _save(new_via_attrs, "cmp_D_pge_new_via_id.csv")
    _save(remainder_df,  "cmp_D_pge_loads_remainder.csv")

    # ── SCE ──────────────────────────────────────────────────────────────────
    _subhdr("SCE: T3.subst_id <-> alt.SUBST_ID")
    print("  (SCE loads uses OBJECTID, not subst_id; loads<->attrs ID join not possible)")

    sce_t3  = dfs["sce_attrs"].copy()
    sce_alt = dfs["sce_attrs_alt"].copy()
    basin_sce   = basin[basin["owner_std"] == "sce"]
    basin_norms_sce = set(norm(basin_sce["name"].dropna()))

    t3_subs = (sce_t3[["substation_name", "subst_id"]]
               .drop_duplicates("subst_id").copy())
    t3_subs["t3_norm"] = norm(t3_subs["substation_name"])

    alt_subs = (sce_alt[["SUB_NAME", "SUBST_ID", "latitude", "longitude"]]
                .drop_duplicates("SUBST_ID").copy())
    alt_subs["alt_norm"] = norm(alt_subs["SUB_NAME"])

    id_pairs_sce = pd.merge(
        t3_subs.rename(columns={"subst_id": "sub_id", "substation_name": "t3_name"}),
        alt_subs.rename(columns={"SUBST_ID": "sub_id", "SUB_NAME": "alt_name",
                                  "latitude": "alt_lat", "longitude": "alt_lon"}),
        on="sub_id",
    )
    id_pairs_sce["t3_in_basin"]  = id_pairs_sce["t3_norm"].isin(basin_norms_sce)
    id_pairs_sce["alt_in_basin"] = id_pairs_sce["alt_norm"].isin(basin_norms_sce)
    id_pairs_sce["names_agree"]  = id_pairs_sce["t3_norm"] == id_pairs_sce["alt_norm"]

    mismatches_sce   = id_pairs_sce[~id_pairs_sce["names_agree"]]
    # New connections: alt not in basin by name, but T3 name (same ID) is
    new_via_t3 = id_pairs_sce[~id_pairs_sce["alt_in_basin"] & id_pairs_sce["t3_in_basin"]].copy()
    # Compute distance alt->basin using T3 name for the lookup
    basin_sce_locs = (basin_sce[["name", "latitude", "longitude"]].copy()
                      .assign(name_norm=lambda d: norm(d["name"]))
                      .drop_duplicates("name_norm")
                      .set_index("name_norm"))
    new_via_t3["basin_lat"] = new_via_t3["t3_norm"].map(basin_sce_locs["latitude"])
    new_via_t3["basin_lon"] = new_via_t3["t3_norm"].map(basin_sce_locs["longitude"])
    new_via_t3["dist_to_basin_km"] = haversine_km(
        new_via_t3["alt_lat"].astype(float),
        new_via_t3["alt_lon"].astype(float),
        new_via_t3["basin_lat"].astype(float),
        new_via_t3["basin_lon"].astype(float),
    )

    conflicts_sce = mismatches_sce[
        mismatches_sce["t3_in_basin"] & mismatches_sce["alt_in_basin"]
    ]

    # Remainder for alt: not matched by name, not helped by T3 ID.
    # Use the complete alt set to determine basin membership — id_pairs_sce only
    # covers 675 of 734 alt subs (the 59 without a T3 ID still match basin directly).
    all_alt_norms       = set(norm(dfs["sce_attrs_alt"]["SUB_NAME"].dropna()))
    alt_in_basin_name   = all_alt_norms & basin_norms_sce
    alt_in_basin_id     = set(new_via_t3["alt_norm"])
    alt_remainder_norms = all_alt_norms - alt_in_basin_name - alt_in_basin_id

    # Step 3: dictionary augmentation for SCE
    dict_sce = dict_all.get("sce", {})
    alt_in_basin_dict_sce = {
        n for n in alt_remainder_norms
        if any(bn in basin_norms_sce for bn in dict_sce.get(n, []))
    }
    alt_remainder_norms = alt_remainder_norms - alt_in_basin_dict_sce

    alt_remainder_df = (sce_alt.assign(_norm=norm(sce_alt["SUB_NAME"]))
                        [lambda d: d["_norm"].isin(alt_remainder_norms)]
                        .drop_duplicates("SUBST_ID")
                        [["SUB_NAME", "SUBST_ID", "SYS_NAME", "latitude", "longitude"]]
                        .sort_values("SUB_NAME"))

    print(f"    All ID pairs (T3 <-> alt): {len(id_pairs_sce)}")
    print(f"    Same norm name (ID + name agree): {id_pairs_sce['names_agree'].sum()}")
    print(f"    Name mismatches (same ID, different norm name): {len(mismatches_sce)}")
    print(f"      T3  matched basin by name:  {id_pairs_sce['t3_in_basin'].sum()}")
    print(f"      Alt matched basin by name:  {id_pairs_sce['alt_in_basin'].sum()}")
    print(f"      New via T3 name (alt miss, T3 hit): {len(new_via_t3)}")
    print(f"      Conflicts (both match basin, different names): {len(conflicts_sce)}")
    if len(new_via_t3) > 0:
        d = new_via_t3["dist_to_basin_km"].dropna()
        print(f"      New-via-ID dist to basin: median={d.median():.1f} km  "
              f"p90={d.quantile(0.9):.1f} km  max={d.max():.1f} km")
    print(f"    Additionally matched via dictionary: {len(alt_in_basin_dict_sce)}")
    print(f"    Alt remainder (unmatched after name + ID + dict): {len(alt_remainder_df)}")

    if not conflicts_sce.empty:
        print("\n    CONFLICTS:")
        for _, row in conflicts_sce.iterrows():
            print(f"      sub_id={row['sub_id']}  T3='{row['t3_name']}' "
                  f"-> basin '{row['t3_norm']}'  |  "
                  f"alt='{row['alt_name']}' -> basin '{row['alt_norm']}'")

    _save(id_pairs_sce,   "cmp_D_sce_t3_alt_id_pairs.csv")
    _save(new_via_t3,     "cmp_D_sce_new_via_t3.csv")
    _save(alt_remainder_df, "cmp_D_sce_alt_remainder.csv")

    # ── SDGE ─────────────────────────────────────────────────────────────────
    _subhdr("SDGE: no ID columns available — remainder from name join + dict")

    basin_sdge       = basin[basin["owner_std"] == "sdge"]
    basin_norms_sdge = set(norm(basin_sdge["name"].dropna()))
    dict_sdge        = dict_all.get("sdge", {})
    failures_sdge = set(
        pd.read_csv(FILE["sdge_fail"])["substation_name"].str.upper().str.strip()
    )

    sdge_a = dfs["sdge_attrs"].copy()
    sdge_a = sdge_a[~sdge_a["substation_name"].str.upper().str.strip().isin(failures_sdge)]
    sdge_a["_norm"] = norm(sdge_a["substation_name"])
    sdge_a_unmatched_name = ~sdge_a["_norm"].isin(basin_norms_sdge)
    sdge_a_dict_matched = sdge_a["_norm"].apply(
        lambda n: any(bn in basin_norms_sdge for bn in dict_sdge.get(n, []))
    )
    sdge_a_rem = (sdge_a[sdge_a_unmatched_name & ~sdge_a_dict_matched]
                  .drop_duplicates("substation_name")
                  [["substation_name", "latitude", "longitude"]]
                  .sort_values("substation_name"))

    sdge_l = dfs["sdge_loads"].copy()
    sdge_l = sdge_l[~sdge_l["AssetName"].str.upper().str.strip().isin(failures_sdge)]
    sdge_l["_norm"] = norm(sdge_l["AssetName"])
    sdge_l_unmatched_name = ~sdge_l["_norm"].isin(basin_norms_sdge)
    sdge_l_dict_matched = sdge_l["_norm"].apply(
        lambda n: any(bn in basin_norms_sdge for bn in dict_sdge.get(n, []))
    )
    sdge_l_rem = (sdge_l[sdge_l_unmatched_name & ~sdge_l_dict_matched]
                  .drop_duplicates("AssetName")
                  [["AssetName", "latitude", "longitude"]]
                  .sort_values("AssetName"))

    n_sdge_a_dict = int(sdge_a[sdge_a_unmatched_name & sdge_a_dict_matched]["substation_name"].nunique())
    n_sdge_l_dict = int(sdge_l[sdge_l_unmatched_name & sdge_l_dict_matched]["AssetName"].nunique())
    print(f"    SDGE attrs remainder: {len(sdge_a_rem)} "
          f"(+{n_sdge_a_dict} matched via dict)")
    print(f"    SDGE loads remainder: {len(sdge_l_rem)} "
          f"(+{n_sdge_l_dict} matched via dict)")
    for _, r in sdge_a_rem.iterrows():
        print(f"      attrs: {r['substation_name']}")
    for _, r in sdge_l_rem.iterrows():
        print(f"      loads: {r['AssetName']}")

    _save(sdge_a_rem, "cmp_D_sdge_attrs_remainder.csv")
    _save(sdge_l_rem, "cmp_D_sdge_loads_remainder.csv")

    # ── Basin remainders ──────────────────────────────────────────────────────
    _subhdr("Basin remainders — basin substations not matched by any utility source or dict")

    # Union of all normalised names from every source for each utility.
    # SCE sce_loads contains both scrape and bulk rows.
    util_source_norms = {
        "pge": (
            set(norm(dfs["pge_loads"]["subname"].dropna())) |
            set(norm(dfs["pge_attrs"]["substation_name"].dropna()))
        ),
        "sce": (
            set(norm(dfs["sce_loads"]["SUBSTATION"].dropna())) |
            set(norm(dfs["sce_attrs"]["substation_name"].dropna())) |
            set(norm(dfs["sce_attrs_alt"]["SUB_NAME"].dropna()))
        ),
        "sdge": (
            set(norm(dfs["sdge_loads"]["AssetName"].dropna())) |
            set(norm(dfs["sdge_attrs"]["substation_name"].dropna()))
        ),
    }

    for owner_std, util_norms in util_source_norms.items():
        # Dictionary augmentation: basin entries targeted by dict mappings from
        # in-data source names are considered "matched" and excluded from remainder.
        dict_util = dict_all.get(owner_std, {})
        dict_claimed_basin_norms: set[str] = set()
        for src_n, bas_norms in dict_util.items():
            if src_n in util_norms:
                dict_claimed_basin_norms.update(bas_norms)

        covered = util_norms | dict_claimed_basin_norms

        basin_sub = basin[basin["owner_std"] == owner_std].copy()
        basin_sub["_norm"] = norm(basin_sub["name"])
        remainder = (basin_sub[~basin_sub["_norm"].isin(covered)]
                     .drop(columns=["_norm"])
                     .sort_values("name")
                     .reset_index(drop=True))
        n_by_name = int(basin_sub["_norm"].isin(util_norms).sum())
        n_by_dict = int(basin_sub["_norm"].isin(dict_claimed_basin_norms - util_norms).sum())
        matched   = n_by_name + n_by_dict
        print(f"  {owner_std.upper()}: {len(basin_sub)} basin total  |  "
              f"{matched} matched (name: {n_by_name}, dict: {n_by_dict})  |  "
              f"{len(remainder)} unmatched")
        _save(remainder, f"cmp_D_basin_{owner_std}_remainder.csv")


# ── Section E ─────────────────────────────────────────────────────────────────

def section_e(dfs: dict) -> None:
    _hdr("SECTION E - SCE bulk download vs scrape: MIN/MAX load values")
    print("  Inner-joins bulk and scrape rows on (SUBSTATION, YEAR, MONTH, HOUR).")
    print("  Only substations present in both sources are compared.")

    sce_all = dfs["sce_loads"]
    bulk   = sce_all[sce_all["source"] == "bulk"].copy()
    scrape = sce_all[sce_all["source"] == "scrape"].copy()

    bulk["sub_key"]   = norm(bulk["SUBSTATION"])
    scrape["sub_key"] = norm(scrape["SUBSTATION"])

    intersection = set(bulk["sub_key"].unique()) & set(scrape["sub_key"].unique())
    print(f"\n  Substations: {bulk['sub_key'].nunique()} bulk  |  "
          f"{scrape['sub_key'].nunique()} scrape  |  {len(intersection)} intersection")

    bulk2   = bulk[bulk["sub_key"].isin(intersection)]
    scrape2 = scrape[scrape["sub_key"].isin(intersection)]

    merged = pd.merge(
        bulk2[["sub_key", "SUBSTATION", "YEAR", "MONTH", "HOUR",
               "MIN_LOAD", "MAX_LOAD"]],
        scrape2[["sub_key", "YEAR", "MONTH", "HOUR", "MIN_LOAD", "MAX_LOAD"]],
        on=["sub_key", "YEAR", "MONTH", "HOUR"],
        suffixes=("_bulk", "_scrape"),
        how="inner",
    )

    merged["min_diff"] = merged["MIN_LOAD_bulk"] - merged["MIN_LOAD_scrape"]
    merged["max_diff"] = merged["MAX_LOAD_bulk"] - merged["MAX_LOAD_scrape"]
    merged["any_diff"] = (merged["min_diff"].abs() > 0.01) | (merged["max_diff"].abs() > 0.01)

    print(f"  Inner-merged rows: {len(merged):,}")
    print(f"  Rows where MIN_LOAD differs (>0.01 MW): {(merged['min_diff'].abs() > 0.01).sum():,}")
    print(f"  Rows where MAX_LOAD differs (>0.01 MW): {(merged['max_diff'].abs() > 0.01).sum():,}")

    # ── Per-substation summary ────────────────────────────────────────────────
    grp = merged.groupby("SUBSTATION").agg(
        n_rows           =("any_diff",        "count"),
        n_diff           =("any_diff",        "sum"),
        mean_abs_min_diff=("min_diff",        lambda x: x.abs().mean()),
        mean_abs_max_diff=("max_diff",        lambda x: x.abs().mean()),
        max_abs_min_diff =("min_diff",        lambda x: x.abs().max()),
        max_abs_max_diff =("max_diff",        lambda x: x.abs().max()),
        bulk_zeros       =("MIN_LOAD_bulk",   lambda x: (x == 0).sum()),
        scrape_zeros     =("MIN_LOAD_scrape", lambda x: (x == 0).sum()),
    ).reset_index()
    grp["pct_diff"]   = grp["n_diff"] / grp["n_rows"] * 100
    grp["max_any_diff"] = grp[["max_abs_min_diff", "max_abs_max_diff"]].max(axis=1)
    grp = grp.sort_values("max_any_diff", ascending=False).reset_index(drop=True)

    disagreeing = grp[grp["n_diff"] > 0]
    print(f"\n  Substations with any disagreement: {len(disagreeing)} of {len(grp)}")
    print(f"  Substations with >50% rows disagreeing: {(grp['pct_diff'] > 50).sum()}")
    print(f"  Substations where bulk has zeros, scrape does not: "
          f"{((grp['bulk_zeros'] > 0) & (grp['scrape_zeros'] == 0)).sum()}")

    if not disagreeing.empty:
        _subhdr("Per-substation summary (worst first)")
        hdr = (f"  {'SUBSTATION':<22} {'rows':>6} {'diff':>6} {'%':>6} "
               f"{'mean|min|':>10} {'mean|max|':>10} "
               f"{'max|min|':>9} {'max|max|':>9} {'blk0':>5} {'scr0':>5}")
        print(hdr)
        print(f"  {'-'*22} {'-'*6} {'-'*6} {'-'*6} {'-'*10} {'-'*10} {'-'*9} {'-'*9} {'-'*5} {'-'*5}")
        for _, row in disagreeing.iterrows():
            print(f"  {row['SUBSTATION']:<22} {int(row['n_rows']):>6,} "
                  f"{int(row['n_diff']):>6,} {row['pct_diff']:>6.1f} "
                  f"{row['mean_abs_min_diff']:>10.3f} {row['mean_abs_max_diff']:>10.3f} "
                  f"{row['max_abs_min_diff']:>9.3f} {row['max_abs_max_diff']:>9.3f} "
                  f"{int(row['bulk_zeros']):>5} {int(row['scrape_zeros']):>5}")

    # ── Exports ───────────────────────────────────────────────────────────────
    detail = merged.sort_values(["SUBSTATION", "YEAR", "MONTH", "HOUR"])
    _save(detail, "cmp_E_sce_bulk_vs_scrape_detail.csv")
    _save(grp,    "cmp_E_sce_bulk_vs_scrape_by_sub.csv")
    print(f"\n  cmp_E_sce_bulk_vs_scrape_detail.csv  ({len(detail):,} rows)")
    print(f"  cmp_E_sce_bulk_vs_scrape_by_sub.csv  ({len(grp):,} substations)")


# ── Section F ─────────────────────────────────────────────────────────────────

def section_f(dfs: dict) -> None:
    """
    SCE load profile evolution across data vintages (years).

    Each year in the SCE bulk download is a distinct snapshot of the utility's
    internally-computed 10th/90th percentile profiles from a non-public lookback
    window.  This section shows how those snapshots have changed from 2017–2026.

    Analysis uses bulk-source rows only (preferred over scrape; see CLAUDE.md).

    Coverage varies by year (21 substations in 2017, 602 in 2025, 520 in 2026).
    652 of 709 unique substations appear in 2+ years with overlapping coverage.
    2026 only covers Jan-Apr; May-Dec fall back to 2025 in the processed output.
    Cross-year comparison uses mean load per substation (normalised) for F1/F3;
    F4 uses only substations present in both adjacent years for apples-to-apples.

    Figures saved to data/figures/sce_vintage_analysis/:
      F1  Per-substation mean MAX profile (normalised) — 12 monthly panels
      F2  Raw coincident total + substation count on twin axes — coverage growth
      F3  Peak hour shift — argmax of normalised hourly profile per (month, year)
      F4  Change heatmap — common substations across best adjacent-year pair
    """
    _hdr("SECTION F - SCE load profile evolution across data vintages")

    sce = dfs["sce_loads"]
    if sce.empty:
        print("  SCE loads not found — skipping section F.")
        return

    bulk = sce[sce["source"] == "bulk"].copy()
    if bulk.empty:
        print("  No bulk rows in SCE combined — skipping section F.")
        return

    # Numeric conversions; MONTH in raw CSV is 0-indexed, convert to 1-indexed
    bulk["YEAR"]     = pd.to_numeric(bulk["YEAR"],     errors="coerce")
    bulk["MONTH"]    = pd.to_numeric(bulk["MONTH"],    errors="coerce").add(1).astype("Int64")
    bulk["HOUR"]     = pd.to_numeric(bulk["HOUR"],     errors="coerce")
    bulk["MAX_LOAD"] = pd.to_numeric(bulk["MAX_LOAD"], errors="coerce")
    bulk["MIN_LOAD"] = pd.to_numeric(bulk["MIN_LOAD"], errors="coerce")
    bulk = bulk.dropna(subset=["YEAR", "MONTH", "HOUR", "MAX_LOAD"]).copy()
    bulk["YEAR"] = bulk["YEAR"].astype(int)

    years = sorted(bulk["YEAR"].unique())
    n_subs_by_year = {y: bulk[bulk["YEAR"] == y]["SUBSTATION"].nunique() for y in years}
    print(f"  Available years: {years}")
    for y in years:
        print(f"    {y}: {n_subs_by_year[y]:,} substations")

    # ── How many substations appear in each pair of adjacent years? ───────────
    sub_by_year = {y: set(bulk[bulk["YEAR"] == y]["SUBSTATION"].unique()) for y in years}
    print()
    for i in range(len(years) - 1):
        ya, yb = years[i], years[i + 1]
        overlap = len(sub_by_year[ya] & sub_by_year[yb])
        print(f"    {ya}<->{yb} common substations: {overlap}")

    # ── Substations present in 2+ years for vintage-delta analysis ────────────
    sub_year_sets = bulk.groupby("SUBSTATION")["YEAR"].apply(set)
    multi_year_subs = set(sub_year_sets[sub_year_sets.apply(len) > 1].index)
    print(f"\n  Substations appearing in 2+ years: {len(multi_year_subs)} "
          f"(used for per-substation vintage delta in F4)")

    # ── Raw coincident totals (sum across all substations in each year) ───────
    coin_raw = (bulk
                .groupby(["YEAR", "MONTH", "HOUR"])[["MAX_LOAD", "MIN_LOAD"]]
                .sum()
                .reset_index())

    # ── Normalised: mean load per substation per (year, month, hour) ─────────
    coin_norm = coin_raw.copy()
    for y in years:
        mask = coin_norm["YEAR"] == y
        coin_norm.loc[mask, "MAX_LOAD"] = (coin_norm.loc[mask, "MAX_LOAD"]
                                           / n_subs_by_year[y])
        coin_norm.loc[mask, "MIN_LOAD"] = (coin_norm.loc[mask, "MIN_LOAD"]
                                           / n_subs_by_year[y])

    # ── Figure color map: one color per year ─────────────────────────────────
    cmap   = plt.cm.plasma
    norm_c = mcolors.Normalize(vmin=min(years), vmax=max(years))
    def yr_color(y): return cmap(norm_c(y))

    MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun",
                   "Jul","Aug","Sep","Oct","Nov","Dec"]

    # ── Fig F1: Normalised per-substation MAX profile — 12 monthly panels ────
    fig, axes = plt.subplots(3, 4, figsize=(22, 13), sharey=False)
    axes_flat = axes.flatten()
    for m_idx, m in enumerate(range(1, 13)):
        ax = axes_flat[m_idx]
        for yr in years:
            sub_max = (coin_norm[(coin_norm["YEAR"] == yr) & (coin_norm["MONTH"] == m)]
                       .sort_values("HOUR"))
            sub_min = (coin_norm[(coin_norm["YEAR"] == yr) & (coin_norm["MONTH"] == m)]
                       .sort_values("HOUR"))
            if sub_max.empty:
                continue
            ax.plot(sub_max["HOUR"], sub_max["MAX_LOAD"],
                    color=yr_color(yr), lw=1.8, label=str(yr))
            ax.fill_between(sub_max["HOUR"], sub_min["MIN_LOAD"], sub_max["MAX_LOAD"],
                            color=yr_color(yr), alpha=0.08)
        ax.set_title(MONTH_NAMES[m - 1], fontsize=11, fontweight="bold")
        ax.set_xlabel("Hour (PST)", fontsize=8)
        ax.set_ylabel("Mean load per substation (MW)", fontsize=8)
        ax.set_xlim(-0.5, 23.5)
        ax.set_xticks([0, 6, 12, 18, 23])
        ax.grid(alpha=0.25)

    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm_c)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=axes_flat, orientation="vertical", fraction=0.015, pad=0.01)
    cbar.set_label("Year", fontsize=10)
    cbar.set_ticks(years)
    cbar.set_ticklabels([str(y) for y in years])

    fig.suptitle(
        "SCE load profile shape evolution across bulk-download vintages "
        "(normalised: mean MW per substation)\n"
        "Shading = p10–p90 band per year.  "
        "Substation coverage varies by year — see F2 for counts.  "
        "Bulk source only (preferred; see CLAUDE.md).",
        fontsize=10,
    )
    fig.tight_layout()
    out = FIGS_SCE / "sce_vintage_F1_normalised_profiles.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"  Saved: {out.relative_to(ROOT)}")
    plt.close(fig)

    # ── Fig F2: Raw total coincident + substation count on twin axes ──────────
    # One panel per month (small multiples), showing how total load and coverage grew
    fig, axes = plt.subplots(3, 4, figsize=(22, 13), sharey=False)
    axes_flat = axes.flatten()
    for m_idx, m in enumerate(range(1, 13)):
        ax = axes_flat[m_idx]
        ax2 = ax.twinx()
        peak_raw  = [coin_raw[(coin_raw["YEAR"] == yr) &
                              (coin_raw["MONTH"] == m)]["MAX_LOAD"].max()
                     for yr in years]
        sub_cnts  = [n_subs_by_year[yr] for yr in years]
        ax.bar(years, [v / 1000 if pd.notna(v) else 0 for v in peak_raw],
               width=0.6, color="steelblue", alpha=0.7, label="Peak raw (GW)")
        ax2.plot(years, sub_cnts, "o--", color="firebrick", lw=1.5,
                 ms=5, label="Substations")
        ax.set_title(MONTH_NAMES[m - 1], fontsize=11, fontweight="bold")
        ax.set_xlabel("Year", fontsize=7)
        ax.set_ylabel("Peak coincident MAX (GW)", fontsize=7, color="steelblue")
        ax2.set_ylabel("# substations", fontsize=7, color="firebrick")
        ax.tick_params(axis="x", labelrotation=45, labelsize=7)
        ax.tick_params(axis="y", labelcolor="steelblue", labelsize=7)
        ax2.tick_params(axis="y", labelcolor="firebrick", labelsize=7)
        ax.grid(alpha=0.2)
    fig.suptitle(
        "SCE bulk download coverage expansion (raw coincident total vs. substation count)\n"
        "Blue bars = sum of MAX_LOAD across ALL substations in each year's download.  "
        "Red line = number of substations.  "
        "Levels NOT comparable across years — coverage is non-overlapping.",
        fontsize=9,
    )
    fig.tight_layout()
    out = FIGS_SCE / "sce_vintage_F2_coverage_expansion.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"  Saved: {out.relative_to(ROOT)}")
    plt.close(fig)

    # ── Fig F3: Peak hour shift in normalised profile per (month, year) ───────
    fig, ax = plt.subplots(figsize=(13, 6))
    for m in range(1, 13):
        peak_hrs = []
        for yr in years:
            sub = (coin_norm[(coin_norm["YEAR"] == yr) & (coin_norm["MONTH"] == m)]
                   .sort_values("HOUR"))
            if sub.empty or sub["MAX_LOAD"].isna().all():
                peak_hrs.append(np.nan)
            else:
                peak_hrs.append(int(sub.loc[sub["MAX_LOAD"].idxmax(), "HOUR"]))
        ax.plot(years, peak_hrs, marker="o", ms=5, lw=1.8, label=MONTH_NAMES[m - 1])

    ax.set_xlabel("Year")
    ax.set_ylabel("Hour of peak mean MAX_LOAD (PST, 0–23)")
    ax.set_title(
        "SCE peak hour shift across vintages (normalised per-substation profile, bulk source)\n"
        "Downward trend = solar duck-curve pushing peak later in the day",
        fontsize=11,
    )
    ax.set_xticks(years)
    ax.set_xticklabels([str(y) for y in years], rotation=45)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(
        lambda h, _: f"{int(h):02d}:00" if 0 <= h <= 23 else ""
    ))
    ax.set_ylim(-0.5, 23.5)
    ax.legend(fontsize=8, ncol=3, loc="upper right")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    out = FIGS_SCE / "sce_vintage_F3_peak_hour_shift.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"  Saved: {out.relative_to(ROOT)}")
    plt.close(fig)

    # ── Fig F4: Change heatmap — common substations across adjacent year pairs ─
    # Find the adjacent year pair with the most common substations for the most
    # informative apples-to-apples comparison.
    best_overlap, yr_a, yr_b = 0, years[-2], years[-1]
    for i in range(len(years) - 1):
        ya, yb = years[i], years[i + 1]
        ov = len(sub_by_year[ya] & sub_by_year[yb])
        if ov > best_overlap:
            best_overlap, yr_a, yr_b = ov, ya, yb

    common_ab = sub_by_year[yr_a] & sub_by_year[yr_b]
    print(f"\n  F4 using {yr_a} vs {yr_b}: {len(common_ab)} common substations")

    bulk_a = bulk[(bulk["YEAR"] == yr_a) & bulk["SUBSTATION"].isin(common_ab)]
    bulk_b = bulk[(bulk["YEAR"] == yr_b) & bulk["SUBSTATION"].isin(common_ab)]
    coin_a = bulk_a.groupby(["MONTH", "HOUR"])["MAX_LOAD"].mean().reset_index()
    coin_b = bulk_b.groupby(["MONTH", "HOUR"])["MAX_LOAD"].mean().reset_index()
    merged = coin_a.merge(coin_b, on=["MONTH", "HOUR"], suffixes=("_a", "_b"))
    merged["delta_mw"]   = merged["MAX_LOAD_b"] - merged["MAX_LOAD_a"]
    merged["pct_change"] = (merged["delta_mw"] / merged["MAX_LOAD_a"].replace(0, np.nan)) * 100

    fig, axes = plt.subplots(1, 2, figsize=(18, 6))
    for ax, col, label, cmap_name in [
        (axes[0], "delta_mw",
         f"Change in mean MAX per substation (MW)\n{yr_a} → {yr_b}", "RdBu_r"),
        (axes[1], "pct_change",
         f"% change in mean MAX per substation\n{yr_a} → {yr_b}", "RdBu_r"),
    ]:
        pivot = merged.pivot(index="MONTH", columns="HOUR", values=col)
        vals  = pivot.values.astype(float)
        finite = vals[np.isfinite(vals)]
        if finite.size == 0:
            ax.set_title(f"{label}\n(no data)")
            continue
        vmax = max(np.nanpercentile(np.abs(finite), 97), 1e-6)
        im   = ax.imshow(vals, aspect="auto", cmap=cmap_name,
                         vmin=-vmax, vmax=vmax, origin="upper")
        ax.set_xticks(range(24))
        ax.set_xticklabels([str(h) for h in range(24)], fontsize=7)
        months_present = sorted(pivot.index)
        ax.set_yticks(range(len(months_present)))
        ax.set_yticklabels([MONTH_NAMES[m - 1] for m in months_present], fontsize=9)
        ax.set_xlabel("Hour (PST)")
        ax.set_title(label, fontsize=10)
        fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)

    fig.suptitle(
        f"SCE profile change {yr_a}→{yr_b}: {len(common_ab)} substations present in BOTH years "
        f"(bulk source)\n"
        "Red = load increased; Blue = load decreased.  "
        "Mean MAX per common substation — apples-to-apples vintage comparison.",
        fontsize=10,
    )
    fig.tight_layout()
    out = FIGS_SCE / "sce_vintage_F4_change_heatmap.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"  Saved: {out.relative_to(ROOT)}")
    plt.close(fig)

    # ── Console summary ───────────────────────────────────────────────────────
    print(f"\n  Summary (normalised, bulk, July peak hour — NaN if year lacks July data):")
    for yr in years:
        sub = (coin_norm[(coin_norm["YEAR"] == yr) & (coin_norm["MONTH"] == 7)]
               .sort_values("HOUR"))
        n = n_subs_by_year[yr]
        if sub.empty:
            print(f"    {yr}:  July data not available ({n} substations in this vintage)")
            continue
        peak_row = sub.loc[sub["MAX_LOAD"].idxmax()]
        print(f"    {yr}:  July mean MAX = {peak_row['MAX_LOAD']:.1f} MW/sub  "
              f"at hour {int(peak_row['HOUR']):02d}:00 PST  "
              f"({n} substations)")


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

    # Drop basin entries named 'Unknown' — they have no useful identity and
    # clutter spatial joins and remainder outputs.
    if not dfs["basin"].empty:
        mask_unk = dfs["basin"]["name"].str.strip().str.lower() == "unknown"
        n_unk = mask_unk.sum()
        if n_unk:
            dfs["basin"] = dfs["basin"][~mask_unk].reset_index(drop=True)
            print(f"  Dropped {n_unk} 'Unknown' entries from basin dataset.")

    return dfs


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-s", "--section",
        default="A,B,C,D,E,F",
        metavar="SECTIONS",
        help="Comma-separated sections to run: A, B, C, D, E, F (default: A,B,C,D,E,F)",
    )
    args = parser.parse_args()
    sections = {s.strip().upper() for s in args.section.split(",")}

    dfs = load_all()

    # Load basin-source name dictionary for augmenting name joins in B and D.
    dict_all = _load_dict_all()
    if dict_all:
        total_entries = sum(len(v) for util in dict_all.values() for v in util.values())
        print(f"\nDictionary loaded: {DICT_PATH.relative_to(ROOT)} "
              f"({total_entries} source->basin pairs across "
              f"{', '.join(f'{k.upper()}:{len(v)}' for k, v in dict_all.items())})")
    else:
        print(f"\nWARNING: {DICT_PATH.relative_to(ROOT)} not found; "
              "skipping dictionary augmentation in sections B and D.")

    if "A" in sections:
        section_a(dfs)
    if "B" in sections:
        section_b(dfs, dict_all)
    if "C" in sections:
        section_c(dfs)
    if "D" in sections:
        section_d(dfs, dict_all)
    if "E" in sections:
        section_e(dfs)
    if "F" in sections:
        section_f(dfs)

    print(f"\n{'=' * 72}")
    print(f"  Done. Exported CSVs -> {CHECKS.relative_to(ROOT)}/")
    print("=" * 72)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
