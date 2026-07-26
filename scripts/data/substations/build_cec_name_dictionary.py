"""
build_cec_name_dictionary.py

Builds a utility-source-name -> CEC-name mapping dictionary
(data/cecSourceDictionary.csv), the CEC analogue of the hand-curated
data/basinSourceDictionary.csv, so process_substations_clean.py-style
name matching can recover CEC coordinates for utility substations whose
raw name differs from the CEC name.

Why this is mostly a reuse, not a rebuild
-----------------------------------------
CEC is a descendant of the same dataset as DataBasin 2022 ("basin"), and it
inherited basin's substation naming: 70 of the 79 basinSourceDictionary.csv
`BasinName` targets exist verbatim as a CEC name for the same utility. So the
existing hand-curated `SourceName -> BasinName` pairs transfer almost
one-to-one into `SourceName -> CECName`. This script:

  TIER 1 (seed, auto-verified): for every basinSourceDictionary.csv entry
    whose norm(BasinName) matches a norm(CEC name) of the same owner, emit
    (SourceName, CECName=the matched CEC name, Utility, source="basin_reuse").
    These are effectively already human-reviewed (they were curated for basin
    and the target name still exists in CEC).

  TIER 2 (new candidates): for utility substations that are STILL unmatched to
    CEC after (a) exact normalized-name join and (b) the Tier-1 seed, search
    CONFIRMED-owner CEC records for candidates using the same rule-based +
    spatial logic as find_basin_name_candidates.py (rules on the name, plus
    nearest CEC record within 2 km of the utility coordinate). Auto-accepted
    matches (name match after suffix-stripping, or very close spatially) are
    added to the dictionary; the rest go to a candidates file for manual
    review.

  TIER 3 (assumed-owner rescue): CEC tags some records "Other (PGE - Assumed)"
    etc when it isn't fully sure of ownership (owner_std suffix "_assumed").
    These are EXCLUDED from Tiers 1-2 to keep the confirmed-owner match
    honest, but a substation still unresolved after Tier 2 is re-searched
    against its utility's *_assumed pool with the same rules. Found by
    inspection: every one of PGE's post-Tier-2 leftovers (RUSSELL, BERKELEY T,
    LODI, LOCKHEED NO 1/2) turned out to have a near-exact, <100m assumed-owner
    CEC match that Tier 2 never saw because of the owner filter — this tier
    exists so that doesn't require a human to notice it by hand every time.
    Only auto-accepted on an exact suffix-stripped name match (no
    spatial-only acceptance — ownership is already unconfirmed, so we don't
    also relax the name bar), tagged source="name_auto_assumed" with
    Notes flagging the unconfirmed CEC ownership for visibility.

Inputs
------
  data/basinSourceDictionary.csv                         (existing curated dict)
  data/processed/substation_misc/ca_substations_cec.csv  (process_substations_cec.py)
  data/processed/substations/substation_attributes_clean.csv  (utility subs + coords)

Outputs
-------
  data/cecSourceDictionary.csv
      SourceName, CECName, Utility, source, Notes
      Tier-1 seed rows (source="basin_reuse"), Tier-2 auto-accepts
      (source="name_auto" or "spatial_auto"), and Tier-3 assumed-owner
      rescues (source="name_auto_assumed").
  data/checks/find_cec_name_candidates/cec_candidates_{pge,sce,sdge}.csv
      SourceName, CECName, match_rule, source_lat, source_lon,
      cec_lat, cec_lon, dist_km    (for manual review)
  data/checks/find_cec_name_candidates/basin_dict_not_in_cec.csv
      the 9 basin-dict entries whose BasinName is NOT in CEC (need attention)

Usage:
  python scripts/data/substations/build_cec_name_dictionary.py
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
BASIN_DICT = ROOT / "data" / "basinSourceDictionary.csv"
CEC_FILE = ROOT / "data" / "processed" / "substation_misc" / "ca_substations_cec.csv"
ATTR_FILE = ROOT / "data" / "processed" / "substations" / "substation_attributes_clean.csv"
OUT_DICT = ROOT / "data" / "cecSourceDictionary.csv"
CHECKS = ROOT / "data" / "checks" / "find_cec_name_candidates"

SPATIAL_THRESHOLD_KM = 2.0   # candidate ceiling (matches basin workflow)
AUTO_ACCEPT_KM = 0.25        # spatial matches this close are auto-added to the dict

_UTIL_MAP = {"PGE": "pge", "SCE": "sce", "SDGE": "sdge"}

# CEC systematically appends an owner suffix to many names, e.g.
# "Acton - PG&E", "Alhambra - (PG&E)", "Salt Creek - SDG&E", "Santee - (SDG&E)".
# Stripping it lets a source name match the CEC base name regardless of
# coordinate distance — important for SDGE, whose coords are polygon centroids
# (offset up to ~1.8 km) so the spatial gate alone misses true name matches.
_OWNER_SUFFIX_RE = re.compile(
    r"\s*-\s*\(?\s*(pg&e|pge|sce|sdg&e|sdge|smud|iid|ladwp|pcorp|wapa|svp|vea|nve)\b.*$",
    re.IGNORECASE)


# -- normalisation (mirrors process_substations_clean.py / find_basin_name_candidates.py) --

def norm(name) -> str:
    if pd.isna(name):
        return ""
    s = str(name).strip()
    s = re.sub(r"\bP\.?\s*T\.?\b", "", s, flags=re.I)
    s = re.sub(r"\bsubstation\b", "", s, flags=re.I)
    s = re.sub(r"[/\\\-,\.&\(\)_#']", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def norm_base(name) -> str:
    """norm() with a trailing CEC owner suffix removed first."""
    if pd.isna(name):
        return ""
    return norm(_OWNER_SUFFIX_RE.sub("", str(name).strip()))


def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _apply_rules(src_name: str):
    """Same rule set as find_basin_name_candidates.py."""
    out = []
    t = re.sub(r"\bNO\s+(\d+)\b", r"\1", src_name, flags=re.I)
    if t != src_name:
        out.append(("no_n_rule", t))
    for suf, tag in ((r"\s+PH$", "ph_drop"), (r"\s+PP$", "pp_drop")):
        t = re.sub(suf, "", src_name, flags=re.I).strip()
        if t != src_name:
            out.append((tag, t))
    if re.search(r"\bMOUNTAIN\b", src_name, re.I):
        out.append(("mountain_mt", re.sub(r"\bMOUNTAIN\b", "Mt.", src_name, flags=re.I)))
    m = re.match(r"^(.+?)\s+([A-Z])$", src_name.strip())
    if m:
        out.append(("station_insert", m.group(1) + " Station " + m.group(2)))
        out.append(("letter_drop", m.group(1).strip()))
    sf = re.match(r"^SF\s+[A-Z]\s*\(([^)]+)\)$", src_name.strip(), re.I)
    if sf:
        out.append(("sf_paren", sf.group(1).strip()))
    t = re.sub(r"\s*\([^)]*\)", "", src_name).strip()
    if t and t != src_name:
        out.append(("paren_strip", t))
    if not re.search(r"\d", src_name):
        for n in range(1, 13):
            out.append((f"numbered_{n}", f"{src_name} {n}"))
    if re.search(r"\bAND\b", src_name, re.I):
        out.append(("and_ampersand", re.sub(r"\bAND\b", "&", src_name, flags=re.I)))
    return out


def main() -> None:
    CHECKS.mkdir(parents=True, exist_ok=True)
    cec = pd.read_csv(CEC_FILE)
    cec["name_norm"] = cec["name"].map(norm)
    attrs = pd.read_csv(ATTR_FILE)
    attrs["name_norm"] = attrs.substation_name.map(norm)
    bdict = pd.read_csv(BASIN_DICT)

    # CEC lookups per confirmed owner (norm -> first (name, lat, lon))
    cec_by_owner = {}
    cec_by_owner_assumed = {}
    for owner in _UTIL_MAP.values():
        c = cec[(cec.owner_std == owner)
                & cec.latitude.notna() & cec.longitude.notna()].drop_duplicates("name_norm")
        cec_by_owner[owner] = c
        ca = cec[(cec.owner_std == f"{owner}_assumed")
                & cec.latitude.notna() & cec.longitude.notna()].drop_duplicates("name_norm")
        cec_by_owner_assumed[owner] = ca

    seed_rows = []
    dict_gap_rows = []

    # ---- TIER 1: reuse basin dict where BasinName exists verbatim in CEC ----
    for _, r in bdict.iterrows():
        U = str(r.Utility).upper().strip()
        owner = _UTIL_MAP.get(U)
        if owner is None:
            continue
        bn = norm(r.BasinName)
        c = cec_by_owner[owner]
        hit = c[c.name_norm == bn]
        if len(hit):
            seed_rows.append({
                "SourceName": r.SourceName,
                "CECName": hit.iloc[0]["name"],
                "Utility": U,
                "source": "basin_reuse",
                "Notes": r.get("Notes", ""),
            })
        else:
            dict_gap_rows.append({
                "SourceName": r.SourceName, "BasinName": r.BasinName,
                "Utility": U, "Notes": r.get("Notes", ""),
            })

    print(f"TIER 1 (basin reuse): {len(seed_rows)}/{len(bdict)} basin-dict entries "
          f"transfer to CEC; {len(dict_gap_rows)} need attention.")
    pd.DataFrame(dict_gap_rows).to_csv(CHECKS / "basin_dict_not_in_cec.csv", index=False)

    # normalized source names already covered (direct match OR seed)
    seed_src_norm = {norm(s["SourceName"]) for s in seed_rows}

    # ---- TIER 2: candidate search for utility subs still unmatched to CEC ----
    auto_rows = []
    for U, owner in _UTIL_MAP.items():
        c = cec_by_owner[owner]
        cec_norms = set(c.name_norm)
        cand_records = []

        g = attrs[attrs.utility == owner].copy()
        # still-unmatched = no direct CEC name match AND not covered by the seed dict
        unmatched = g[~g.name_norm.isin(cec_norms) & ~g.name_norm.isin(seed_src_norm)]
        unmatched = unmatched[unmatched.util_lat.notna() & unmatched.util_lon.notna()]

        for _, s in unmatched.iterrows():
            src_name = s.substation_name
            src_base = norm_base(src_name)
            slat, slon = s.util_lat, s.util_lon
            seen = set()
            # rule-based
            for rule, transformed in _apply_rules(src_name):
                tn = norm(transformed)
                for _, brow in c[c.name_norm == tn].iterrows():
                    d = haversine(slat, slon, brow.latitude, brow.longitude)
                    if d > SPATIAL_THRESHOLD_KM:
                        continue
                    key = brow["name"]
                    if key in seen:
                        continue
                    seen.add(key)
                    cand_records.append((src_name, key, rule, slat, slon,
                                         brow.latitude, brow.longitude, round(d, 3),
                                         norm_base(key) == src_base))
            # spatial fallback: nearest CEC of same owner within threshold
            c2 = c.copy()
            c2["d"] = c2.apply(lambda b: haversine(slat, slon, b.latitude, b.longitude), axis=1)
            near = c2[c2.d <= SPATIAL_THRESHOLD_KM].sort_values("d")
            for _, brow in near.iterrows():
                if brow["name"] in seen:
                    continue
                seen.add(brow["name"])
                cand_records.append((src_name, brow["name"], "spatial", slat, slon,
                                     brow.latitude, brow.longitude, round(brow.d, 3),
                                     norm_base(brow["name"]) == src_base))

        cdf = pd.DataFrame(cand_records, columns=[
            "SourceName", "CECName", "match_rule", "source_lat", "source_lon",
            "cec_lat", "cec_lon", "dist_km", "name_agrees"]).sort_values(
            ["SourceName", "dist_km"])

        # auto-accept per source name, preferring a within-2km candidate whose
        # suffix-stripped CEC name agrees with the source (name is the reliable
        # signal — critical for SDGE centroids), else the closest within
        # AUTO_ACCEPT_KM.
        utility_auto_rows = []
        for src, grp in cdf.groupby("SourceName"):
            name_hit = grp[grp.name_agrees]
            if len(name_hit):
                best = name_hit.iloc[0]
                utility_auto_rows.append({
                    "SourceName": src, "CECName": best.CECName, "Utility": U,
                    "source": "name_auto",
                    "Notes": f"auto: name match, {best.dist_km}km",
                })
            else:
                best = grp.iloc[0]
                if best.dist_km <= AUTO_ACCEPT_KM:
                    utility_auto_rows.append({
                        "SourceName": src, "CECName": best.CECName, "Utility": U,
                        "source": "spatial_auto",
                        "Notes": f"auto: {best.match_rule} {best.dist_km}km",
                    })

        # ---- TIER 3: assumed-owner rescue for whatever Tier 2 left unresolved ----
        # CEC's "_assumed" records are excluded from Tiers 1-2 to keep the
        # confirmed-owner match honest, but a substation still unresolved
        # after Tier 2 is worth re-checking against them: if the name matches
        # exactly (suffix-stripped) it's almost certainly the same facility,
        # CEC just wasn't sure who owns it.
        resolved_src_t2 = seed_src_norm | {norm(r["SourceName"]) for r in utility_auto_rows}
        ca = cec_by_owner_assumed[owner]
        still_open = unmatched[~unmatched.name_norm.isin(resolved_src_t2)]
        for _, s in still_open.iterrows():
            src_name = s.substation_name
            slat, slon = s.util_lat, s.util_lon
            hit = None
            # exact norm() match against ca.name_norm first (ca.name_norm was
            # built with norm(), so stay consistent — no separate norm_base
            # re-check needed, the lookup itself IS the match criterion),
            # then fall back to the same rule transforms used elsewhere.
            direct = ca[ca.name_norm == norm(src_name)]
            if len(direct):
                hit = direct.iloc[0]
            else:
                for rule, transformed in _apply_rules(src_name):
                    cand = ca[ca.name_norm == norm(transformed)]
                    if len(cand):
                        hit = cand.iloc[0]
                        break
            if hit is None:
                continue
            d = haversine(slat, slon, hit.latitude, hit.longitude)
            if d > SPATIAL_THRESHOLD_KM:
                continue
            utility_auto_rows.append({
                "SourceName": src_name, "CECName": hit["name"], "Utility": U,
                "source": "name_auto_assumed",
                "Notes": f"auto: name match to CEC-unconfirmed owner ({hit.owner_raw}), {d:.3f}km",
            })

        # Flag rows whose SourceName was already resolved (Tier 1 seed or the
        # Tier 2/3 auto-accepts above) so the saved CSV distinguishes "already
        # in the dictionary" from "still needs your judgment" — the raw
        # candidate search finds every SourceName with >=1 candidate, most of
        # which get auto-resolved, so without this flag the file looks
        # unreviewed even when it mostly isn't.
        resolved_src = seed_src_norm | {norm(r["SourceName"]) for r in utility_auto_rows}
        cdf["already_in_dict"] = cdf["SourceName"].map(norm).isin(resolved_src)
        cdf.to_csv(CHECKS / f"cec_candidates_{owner}.csv", index=False)

        n_review = cdf[~cdf.already_in_dict].SourceName.nunique()
        print(f"  {U}: {len(unmatched)} utility subs still unmatched -> "
              f"{len(cdf)} candidate rows ({cdf.SourceName.nunique()} subs with >=1 candidate, "
              f"{n_review} still need manual review)")

        auto_rows.extend(utility_auto_rows)

    print(f"TIER 2+3 auto-accepted (<= {AUTO_ACCEPT_KM} km, plus assumed-owner name rescues): "
          f"{len(auto_rows)} entries")

    out = pd.DataFrame(seed_rows + auto_rows,
                       columns=["SourceName", "CECName", "Utility", "source", "Notes"])

    # Preserve manually-added rows from a prior run — this script overwrites
    # OUT_DICT wholesale, but hand-reviewed entries (any source value this
    # script doesn't itself generate) aren't reproducible by rerunning it.
    _AUTO_SOURCES = {"basin_reuse", "name_auto", "spatial_auto", "name_auto_assumed"}
    if OUT_DICT.exists():
        prev = pd.read_csv(OUT_DICT)
        manual = prev[~prev.source.isin(_AUTO_SOURCES)]
        if len(manual):
            print(f"Preserving {len(manual)} manually-added row(s) from the existing "
                  f"{OUT_DICT.name}: {list(manual.SourceName)}")
            out = pd.concat([out, manual], ignore_index=True)

    out = out.drop_duplicates(["SourceName", "Utility"]).sort_values(["Utility", "SourceName"])
    out.to_csv(OUT_DICT, index=False)
    print(f"\nWrote {len(out)} dictionary entries -> {OUT_DICT.relative_to(ROOT)}")
    print(out.source.value_counts().to_string())
    print(f"Candidates for manual review -> {CHECKS.relative_to(ROOT)}/cec_candidates_*.csv")


if __name__ == "__main__":
    main()
