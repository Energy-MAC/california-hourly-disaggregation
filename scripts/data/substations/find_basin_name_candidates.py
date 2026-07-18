"""
find_basin_name_candidates.py

Generates candidate source-to-basin substation name mappings using rule-based
transformations and spatial proximity, for human review.

For each source-remainder substation the following rules are tried (all applied;
results are combined and deduplicated by (source_name, basin_name) pair):

  1. no_n_rule      -- "NO {N}" -> "{N}"
                       e.g., COALINGA NO 1 -> Coalinga 1
  2. ph_drop        -- strip trailing " PH"
                       e.g., CRESTA PH -> Cresta
  3. pp_drop        -- strip trailing " PP"
                       e.g., HUMBOLDT BAY PP -> Humboldt Bay
  4. mountain_mt    -- MOUNTAIN -> Mt.
                       e.g., POSO MOUNTAIN -> Poso Mt.
  5. station_insert -- insert "Station" before trailing single letter
                       e.g., PETALUMA A -> Petaluma Station A
  6. letter_drop    -- drop trailing single letter
                       e.g., FORT BRAGG A -> Fort Bragg
  7. sf_paren       -- extract parenthetical for "SF X (...)" names
                       e.g., SF H (MARTIN) -> Martin
  8. paren_strip    -- strip parenthetical entirely
                       e.g., BEACH (Q) -> BEACH
  9. numbered_N     -- append " {N}" for N=1..12 (only if source has no digits)
                       e.g., DRUM -> Drum 1, Drum 2
  10. and_ampersand -- " AND " -> " & " then normalize
                       e.g., ROUGH AND READY -> Rough & Ready  (norm strips &)
  11. spatial       -- nearest basin substation within 2 km regardless of name

Exclusions:
  - Pairs already confirmed in basinSourceDictionary.csv (specific source-basin pairs)
  - Basin names already claimed on the basin side of the dictionary (any BasinName
    entry in basinSourceDictionary.csv) -- these are already matched to some source
  - Basin entries with type == "RISER" or type == "TAP" are excluded from the
    candidate pool (these are line taps/risers, not substations)

Outputs:
  data/checks/basin_candidates_pge.csv
  data/checks/basin_candidates_sce.csv
"""

import math
import re
from pathlib import Path

import pandas as pd

ROOT   = Path(__file__).resolve().parents[3]
CHECKS_compare_substations = ROOT / "data" / "checks" / "compare_substations"
CHECKS = ROOT / "data" / "checks" / "find_basin_name_candidates"
CHECKS.mkdir(parents=True, exist_ok=True)
SPATIAL_THRESHOLD_KM = 2.0

# ── Normalisation (mirrors process_substations_clean.py) ─────────────────────

def norm(name):
    if pd.isna(name):
        return ""
    s = str(name).strip()
    s = re.sub(r"\bP\.?\s*T\.?\b", "", s, flags=re.I)
    s = re.sub(r"\bsubstation\b",  "", s, flags=re.I)
    s = re.sub(r"[/\\\-,\.&\(\)_#']", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


# ── Haversine ─────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ── Rule-based transforms ─────────────────────────────────────────────────────

def _apply_rules(src_name):
    """
    Return list of (rule_name, transformed_name) for all rules that fire.
    Multiple (rule, transform) pairs can be returned from a single call.
    """
    results = []

    # Rule 1: NO N -> N
    t = re.sub(r"\bNO\s+(\d+)\b", r"\1", src_name, flags=re.I)
    if t != src_name:
        results.append(("no_n_rule", t))

    # Rule 2: drop trailing PH
    t = re.sub(r"\s+PH$", "", src_name, flags=re.I).strip()
    if t != src_name:
        results.append(("ph_drop", t))

    # Rule 3: drop trailing PP
    t = re.sub(r"\s+PP$", "", src_name, flags=re.I).strip()
    if t != src_name:
        results.append(("pp_drop", t))

    # Rule 4: MOUNTAIN -> Mt.
    if re.search(r"\bMOUNTAIN\b", src_name, re.I):
        t = re.sub(r"\bMOUNTAIN\b", "Mt.", src_name, flags=re.I)
        results.append(("mountain_mt", t))

    # Rules 5 & 6: trailing single letter
    m = re.match(r"^(.+?)\s+([A-Z])$", src_name.strip())
    if m:
        results.append(("station_insert", m.group(1) + " Station " + m.group(2)))
        results.append(("letter_drop",    m.group(1).strip()))

    # Rule 7: SF X (NAME) -> NAME
    sf_m = re.match(r"^SF\s+[A-Z]\s*\(([^)]+)\)$", src_name.strip(), re.I)
    if sf_m:
        results.append(("sf_paren", sf_m.group(1).strip()))

    # Rule 8: strip parenthetical entirely
    t = re.sub(r"\s*\([^)]*\)", "", src_name).strip()
    if t and t != src_name:
        results.append(("paren_strip", t))

    # Rule 9: numbered variants (only if no digits in source name)
    if not re.search(r"\d", src_name):
        for n in range(1, 13):
            results.append((f"numbered_{n}", f"{src_name} {n}"))

    # Rule 10: AND -> &  (norm then strips & to space, same effect for matching)
    if re.search(r"\bAND\b", src_name, re.I):
        t = re.sub(r"\bAND\b", "&", src_name, flags=re.I)
        results.append(("and_ampersand", t))

    return results


# ── Main matching function ────────────────────────────────────────────────────

def find_candidates(
    source_df,
    basin_df,
    src_name_col,
    src_lat_col,
    src_lon_col,
    existing_pairs,        # set of (norm_src, norm_basin) already in dict
    claimed_basin_norms,   # set of norm(BasinName) for all dict entries -- skip these
):
    """
    Apply rules + spatial search.  Returns a DataFrame of candidates.

    existing_pairs: set of (norm(source_name), norm(basin_name)) tuples to skip.
    claimed_basin_norms: set of norm(basin_name) values already assigned in the dict;
                         any basin name in this set is skipped entirely.
    """
    # Build basin index: norm -> list of row-dicts
    basin_by_norm = {}
    basin_rows = []
    for _, row in basin_df.iterrows():
        n = norm(row["name"])
        basin_by_norm.setdefault(n, []).append(row)
        basin_rows.append(row)

    records = []

    for _, src in source_df.iterrows():
        src_name = src[src_name_col]
        src_lat  = src[src_lat_col]
        src_lon  = src[src_lon_col]

        if pd.isna(src_lat) or pd.isna(src_lon):
            continue

        src_n = norm(src_name)
        seen_basin_norms = set()  # track basin matches to avoid duplicate rows

        # ── Rule-based ──────────────────────────────────────────────────────
        for rule, transformed in _apply_rules(src_name):
            t_norm = norm(transformed)
            matching_rows = basin_by_norm.get(t_norm, [])
            for brow in matching_rows:
                b_norm = norm(brow["name"])
                if b_norm in claimed_basin_norms:
                    continue
                if (src_n, b_norm) in existing_pairs:
                    continue
                dist = haversine(src_lat, src_lon, brow["latitude"], brow["longitude"])
                if dist > SPATIAL_THRESHOLD_KM:
                    continue
                if (src_n, b_norm) not in seen_basin_norms:
                    seen_basin_norms.add((src_n, b_norm))
                    records.append({
                        "SourceName":  src_name,
                        "BasinName":   brow["name"],
                        "match_rule":  rule,
                        "source_lat":  round(src_lat, 6),
                        "source_lon":  round(src_lon, 6),
                        "basin_lat":   round(brow["latitude"],  6),
                        "basin_lon":   round(brow["longitude"], 6),
                        "dist_km":     round(dist, 3),
                    })

        # ── Spatial fallback ─────────────────────────────────────────────────
        for brow in basin_rows:
            b_lat = brow["latitude"]
            b_lon = brow["longitude"]
            if pd.isna(b_lat) or pd.isna(b_lon):
                continue
            dist = haversine(src_lat, src_lon, b_lat, b_lon)
            if dist > SPATIAL_THRESHOLD_KM:
                continue
            b_norm = norm(brow["name"])
            if b_norm in claimed_basin_norms:
                continue
            if (src_n, b_norm) in existing_pairs:
                continue
            if (src_n, b_norm) in seen_basin_norms:
                continue  # already reported by a rule
            seen_basin_norms.add((src_n, b_norm))
            records.append({
                "SourceName":  src_name,
                "BasinName":   brow["name"],
                "match_rule":  "spatial",
                "source_lat":  round(src_lat, 6),
                "source_lon":  round(src_lon, 6),
                "basin_lat":   round(b_lat, 6),
                "basin_lon":   round(b_lon, 6),
                "dist_km":     round(dist, 3),
            })

    if not records:
        return pd.DataFrame(columns=["SourceName","BasinName","match_rule",
                                     "source_lat","source_lon","basin_lat","basin_lon","dist_km"])

    df = pd.DataFrame(records)
    return df.sort_values(["SourceName", "dist_km"]).reset_index(drop=True)


# ── Load data and run ─────────────────────────────────────────────────────────

def _load_existing_pairs():
    """
    Returns:
        existing_pairs: set of (norm_src, norm_basin) tuples already in the dict
        claimed_basin_norms: set of norm(BasinName) values across ALL dict entries
                             -- basin names already assigned to some source
    """
    path = ROOT / "data" / "basinSourceDictionary.csv"
    if not path.exists():
        return set(), set()
    d = pd.read_csv(path)
    pairs = set()
    claimed_basin_norms = set()
    for _, row in d.iterrows():
        src_n = norm(row["SourceName"])
        bas_n = norm(row["BasinName"])
        pairs.add((src_n, bas_n))
        claimed_basin_norms.add(bas_n)
    return pairs, claimed_basin_norms


def _filter_basin(basin, label):
    """Remove RISER, TAP, and 'Unknown' name entries; report counts."""
    mask_rt = basin["type"].isin(["RISER", "TAP"])
    mask_unk = basin["name"].str.strip().str.lower() == "unknown"

    n_rt  = mask_rt.sum()
    n_unk = mask_unk.sum()

    basin_filtered = basin[~mask_rt & ~mask_unk].copy()

    if n_rt > 0:
        by_type = basin[mask_rt]["type"].value_counts()
        parts = ", ".join(f"{t}: {c}" for t, c in by_type.items())
        print(f"  Filtered RISER/TAP from basin pool: {n_rt} removed ({parts})")
    else:
        print(f"  Filtered RISER/TAP from basin pool: 0 removed")
    print(f"  Filtered 'Unknown' names from basin pool: {n_unk} removed")
    print(f"  Basin pool after filter: {len(basin_filtered)} substations")
    return basin_filtered


def run_pge(existing_pairs, claimed_basin_norms):
    print("-- PGE -----------------------------------------------------------------")
    src   = pd.read_csv(CHECKS_compare_substations / "cmp_D_pge_loads_remainder.csv")
    basin = pd.read_csv(CHECKS_compare_substations/ "cmp_D_basin_pge_remainder.csv")
    print(f"  Source remainder : {len(src):>4} substations")
    print(f"  Basin  remainder : {len(basin):>4} substations (before RISER/TAP filter)")
    basin = _filter_basin(basin, "PGE")

    df = find_candidates(
        source_df          = src,
        basin_df           = basin,
        src_name_col       = "loads_name",
        src_lat_col        = "loads_lat",
        src_lon_col        = "loads_lon",
        existing_pairs     = existing_pairs,
        claimed_basin_norms= claimed_basin_norms,
    )
    out = CHECKS / "basin_candidates_pge.csv"
    df.to_csv(out, index=False)
    print(f"  Candidates found : {len(df):>4}  -> {out.name}")

    if not df.empty:
        print("  Rule breakdown:")
        for rule, grp in df.groupby("match_rule"):
            print(f"    {rule:<20} {len(grp):>4} rows")
    print()
    return df


def run_sce(existing_pairs, claimed_basin_norms):
    print("-- SCE -----------------------------------------------------------------")
    src   = pd.read_csv(CHECKS_compare_substations / "cmp_D_sce_alt_remainder.csv")
    basin = pd.read_csv(CHECKS_compare_substations/ "cmp_D_basin_sce_remainder.csv")
    print(f"  Source remainder : {len(src):>4} substations")
    print(f"    of which P.T.  : {src['SUB_NAME'].str.contains('P\\.T\\.', na=False).sum():>4}")
    print(f"  Basin  remainder : {len(basin):>4} substations (before RISER/TAP filter)")
    basin = _filter_basin(basin, "SCE")

    df = find_candidates(
        source_df          = src,
        basin_df           = basin,
        src_name_col       = "SUB_NAME",
        src_lat_col        = "latitude",
        src_lon_col        = "longitude",
        existing_pairs     = existing_pairs,
        claimed_basin_norms= claimed_basin_norms,
    )
    out = CHECKS / "basin_candidates_sce.csv"
    df.to_csv(out, index=False)
    print(f"  Candidates found : {len(df):>4}  -> {out.name}")

    if not df.empty:
        print("  Rule breakdown:")
        for rule, grp in df.groupby("match_rule"):
            print(f"    {rule:<20} {len(grp):>4} rows")
    print()
    return df


def main():
    print("Rules tried:")
    rules = [
        "1. no_n_rule      : 'NO {N}' -> '{N}'  (e.g., COALINGA NO 1 -> Coalinga 1)",
        "2. ph_drop        : strip trailing ' PH'  (e.g., CRESTA PH -> Cresta)",
        "3. pp_drop        : strip trailing ' PP'  (e.g., HUMBOLDT BAY PP -> Humboldt Bay)",
        "4. mountain_mt    : MOUNTAIN -> Mt.  (e.g., POSO MOUNTAIN -> Poso Mt.)",
        "5. station_insert : insert 'Station' before trailing letter  (e.g., PETALUMA A -> Petaluma Station A)",
        "6. letter_drop    : drop trailing single letter  (e.g., FORT BRAGG A -> Fort Bragg)",
        "7. sf_paren       : extract parenthetical from 'SF X (...)' names  (e.g., SF H (MARTIN) -> Martin)",
        "8. paren_strip    : strip parenthetical entirely  (e.g., BEACH (Q) -> BEACH)",
        "9. numbered_N     : append ' N' for N=1..12 if source has no digits  (e.g., DRUM -> Drum 1)",
        "10. and_ampersand : ' AND ' -> ' & '  (norm then strips & giving same token)",
        "11. spatial       : nearest basin substation within 2 km regardless of name",
    ]
    for r in rules:
        print(f"  {r}")
    print()
    print(f"Pairs in basinSourceDictionary.csv are excluded from output.")
    print(f"Spatial threshold : {SPATIAL_THRESHOLD_KM} km")
    print()

    existing_pairs, claimed_basin_norms = _load_existing_pairs()
    print(f"Loaded {len(existing_pairs)} existing (source, basin) pairs from dict.")
    print(f"Claimed basin names (excluded from candidates): {len(claimed_basin_norms)}\n")

    run_pge(existing_pairs, claimed_basin_norms)
    run_sce(existing_pairs, claimed_basin_norms)


if __name__ == "__main__":
    main()
