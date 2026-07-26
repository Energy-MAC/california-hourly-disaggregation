"""
compare_substations_cec.py

Name-normalized join of our cleaned utility substations against the CEC
Substation DataPull (07/24/2026), mirroring process_substations_clean.py's
basin-matching stage (Step 1: exact normalized-name join; same norm()
regex) so the "matched by name" counts are directly comparable to the
existing DataBasin 2022 numbers in the README's Substation Coverage Summary
table (550/518/87 matched by name for PGE/SCE/SDGE, before the hand-curated
79-entry basinSourceDictionary.csv dictionary fallback adds 50/9/9 more).

No CEC-specific name dictionary exists yet (the original was hand-curated
over time via find_basin_name_candidates.py) — this script reports the
name-only match rate honestly and lists the unmatched remainder so a
dictionary could be built the same way if useful. It does NOT modify
process_substations_clean.py, substation_attributes_clean.csv, or any basin
output — this is a read-only comparison against the existing clean file.

Three checks, in order:
  1. Name-join match rate per utility (CEC vs basin, side by side).
  2. For substations matched to BOTH a utility coordinate AND a CEC
     coordinate: distance between them (utility-claimed vs CEC-claimed
     location for the same substation — an accuracy cross-check basin
     alone cannot offer, since basin is only ever used when the utility
     coordinate is missing).
  3. The 12 substations with NO coordinate at all (util AND basin both
     missing) — does CEC's name join recover any of them?

Outputs (data/checks/compare_substations_cec/):
  name_join_matched_{pge,sce,sdge}.csv     matched pairs with both coords
  name_join_unmatched_{pge,sce,sdge}.csv   utility substations with no CEC name match
  cec_unmatched_{pge,sce,sdge}.csv         CEC substations with no utility name match
  no_coord_recovery.csv                    the 12 no-coordinate substations x CEC match result

Usage:
  python scripts/data/substations/compare_substations_cec.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
ATTR_FILE = ROOT / "data/processed/substations/substation_attributes_clean.csv"
CEC_FILE = ROOT / "data/processed/substation_misc/ca_substations_cec.csv"
BASIN_FILE = ROOT / "data/processed/substation_misc/ca_substations_2022.csv"
OUT_DIR = ROOT / "data/checks/compare_substations_cec"

# identical to process_substations_clean.py's norm() — do not diverge
_PT_RE = re.compile(r"\s+p\.?\s*t\.?\s*$", re.IGNORECASE)
_SUB_RE = re.compile(r"\bsubstation\b", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[/\-,\.&\(\)_#']")
_SPC_RE = re.compile(r"\s+")


def norm(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    s = s.str.replace(_PT_RE, "", regex=True)
    s = s.str.replace(_SUB_RE, "", regex=True)
    s = s.str.replace(_PUNCT_RE, " ", regex=True)
    s = s.str.replace(_SPC_RE, " ", regex=True)
    return s.str.strip().str.lower()


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))


def load_cec_lookup(owner_std: str) -> pd.DataFrame:
    """CONFIRMED owners only (excludes *_assumed) — mirrors basin's exclusion
    of ambiguous/'unknown' rows so the comparison is apples to apples."""
    c = pd.read_csv(CEC_FILE)
    c = c[c["owner_std"] == owner_std].dropna(subset=["latitude", "longitude"]).copy()
    c["name_norm"] = norm(c["name"])
    return c.drop_duplicates("name_norm")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    attrs = pd.read_csv(ATTR_FILE)
    basin = pd.read_csv(BASIN_FILE)

    print(f"{'utility':6s} {'n_subs':>7s} {'basin_name':>11s} {'cec_name':>9s} "
          f"{'basin_dict':>11s} {'cec_gap':>8s}")

    summary_rows = []
    for owner_std, utility_label in [("pge", "PGE"), ("sce", "SCE"), ("sdge", "SDGE")]:
        df = attrs[attrs.utility == owner_std].copy()
        df["name_norm"] = norm(df.substation_name)

        cec = load_cec_lookup(owner_std)
        cec_norms = set(cec.name_norm)
        basin_matched = df.basin_lat.notna().sum()  # already computed by process_substations_clean.py

        m = df.merge(cec[["name_norm", "latitude", "longitude", "type", "status"]],
                    on="name_norm", how="left")
        matched = m[m.latitude.notna()].copy()
        unmatched = m[m.latitude.isna()].copy()

        both = matched[matched.util_lat.notna()].copy()
        if len(both):
            both["dist_util_cec_km"] = haversine_km(
                both.util_lat, both.util_lon, both.latitude, both.longitude)

        summary_rows.append({
            "utility": utility_label, "n_subs": len(df),
            "basin_name_matched": basin_matched,
            "cec_name_matched": len(matched),
            "cec_unmatched": len(unmatched),
            "median_dist_util_vs_cec_km": both["dist_util_cec_km"].median() if len(both) else np.nan,
            "p95_dist_util_vs_cec_km": both["dist_util_cec_km"].quantile(0.95) if len(both) else np.nan,
            "n_util_cec_over_1km": int((both["dist_util_cec_km"] > 1).sum()) if len(both) else 0,
        })
        print(f"{utility_label:6s} {len(df):7d} {basin_matched:11d} {len(matched):9d} "
              f"{'-':>11s} {len(unmatched):8d}")

        matched[["utility", "substation_name", "util_lat", "util_lon",
                "latitude", "longitude", "type", "status"]].rename(
            columns={"latitude": "cec_lat", "longitude": "cec_lon"}).to_csv(
            OUT_DIR / f"name_join_matched_{owner_std}.csv", index=False)
        unmatched[["utility", "substation_name", "util_lat", "util_lon",
                  "basin_lat", "basin_lon"]].to_csv(
            OUT_DIR / f"name_join_unmatched_{owner_std}.csv", index=False)

        cec_remainder = cec[~cec.name_norm.isin(set(df.name_norm))]
        cec_remainder[["name", "owner_raw", "type", "status", "latitude", "longitude",
                      "county", "city"]].to_csv(
            OUT_DIR / f"cec_unmatched_{owner_std}.csv", index=False)

    summary = pd.DataFrame(summary_rows)
    print("\n=== Name-join summary (basin vs CEC, side by side) ===")
    print(summary.round(3).to_string(index=False))
    summary.round(3).to_csv(OUT_DIR / "name_join_summary.csv", index=False)

    print("\n=== Utility-vs-CEC coordinate distance (both sources have a location) ===")
    for row in summary_rows:
        print(f"  {row['utility']}: median {row['median_dist_util_vs_cec_km']:.3f} km, "
              f"p95 {row['p95_dist_util_vs_cec_km']:.3f} km, "
              f"{row['n_util_cec_over_1km']} pairs disagree by >1 km")

    # --- No-coordinate-at-all recovery check -------------------------------
    none_coord = attrs[attrs.util_lat.isna() & attrs.basin_lat.isna()].copy()
    none_coord["name_norm"] = norm(none_coord.substation_name)
    rows = []
    for _, r in none_coord.iterrows():
        cec = load_cec_lookup(r.utility)
        hit = cec[cec.name_norm == r.name_norm]
        rows.append({
            "utility": r.utility, "substation_name": r.substation_name,
            "cec_recovered": len(hit) > 0,
            "cec_lat": hit.latitude.iloc[0] if len(hit) else np.nan,
            "cec_lon": hit.longitude.iloc[0] if len(hit) else np.nan,
            "cec_type": hit.type.iloc[0] if len(hit) else None,
        })
    recovery = pd.DataFrame(rows)
    recovery.to_csv(OUT_DIR / "no_coord_recovery.csv", index=False)
    print(f"\n=== No-coordinate-at-all substations (util AND basin both missing): "
          f"{len(none_coord)} ===")
    print(f"Recovered by CEC name match: {recovery.cec_recovered.sum()} of {len(recovery)}")
    print(recovery.to_string(index=False))

    print(f"\nwrote outputs to {OUT_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
