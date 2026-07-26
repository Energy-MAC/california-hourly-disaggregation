"""
audit_substation_coverage.py

Reproducible accounting of substation coverage: what we scraped, what has a
coordinate, what cross-references to the CEC inventory, and — crucially — how
many CEC substations exist that we have NO load data for, split by whether the
CEC record looks like a real load-carrying substation or a line structure
(tap / riser / dead-end) that almost certainly carries no load.

Two distinct questions this settles, which are easy to conflate:

  1. COORDINATES. Every scraped substation carries the utility's own published
     coordinate (`util_lat/lon`); basin/CEC are only fallbacks. So "do we have
     a coordinate" is nearly always yes (only 12 SCE subs lack any). This is
     NOT the same as the CEC name-match rate.

  2. CEC CROSS-REFERENCE. Of our scraped substations, how many tie to a CEC
     record (direct normalized-name match OR via cecSourceDictionary.csv). This
     is the "666/670" style number — a validation/enrichment rate, not a
     coordinate-availability rate.

  3. REVERSE GAP. CEC is a location inventory of *every* substation; our scrape
     is a load inventory of only the substations a utility publishes profiles
     for. CEC therefore lists far more IOU substations than we have load data
     for. This script tags each unscraped CEC record by `type` so line
     structures (TAP / RISER / DEAD END) — which don't carry load and are not
     projection targets — can be separated from genuine load-eligible
     substations we simply have no profile for.

"Matched to scrape" for a CEC record := its normalized name equals the
normalized name of a scraped substation of the same utility, OR equals the
normalized CECName of a cecSourceDictionary.csv entry. Uses norm() imported
from build_cec_name_dictionary.py so the definition can never drift from the
dictionary build.

Inputs
------
  data/processed/substations/substation_attributes_clean.csv
  data/processed/substation_misc/ca_substations_cec.csv
  data/cecSourceDictionary.csv

Outputs (data/checks/substation_coverage_audit/)
------------------------------------------------
  coverage_summary.csv          per-utility scraped / coord / CEC-matched counts
  cec_inventory_by_type.csv     per-utility CEC record counts by type (load-
                                eligible vs line structure vs unknown)
  cec_unscraped_{util}.csv      every CEC record for that utility NOT matched to
                                a scraped substation, with type/category, coord,
                                county, voltage, status — the expansion candidate
                                list; sort puts load-eligible substations first

Usage
-----
  python scripts/data/substations/audit_substation_coverage.py
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from build_cec_name_dictionary import norm

ROOT = Path(__file__).resolve().parents[3]
ATTR_FILE = ROOT / "data" / "processed" / "substations" / "substation_attributes_clean.csv"
CEC_FILE = ROOT / "data" / "processed" / "substation_misc" / "ca_substations_cec.csv"
DICT_FILE = ROOT / "data" / "cecSourceDictionary.csv"
OUT_DIR = ROOT / "data" / "checks" / "substation_coverage_audit"

_UTIL_MAP = {"pge": "PGE", "sce": "SCE", "sdge": "SDGE"}

# CEC `type` values that are line structures, not load-carrying substations.
_STRUCTURE_TYPES = {"TAP", "RISER", "DEAD END"}


def _category(cec_type) -> str:
    """Load-carrying substation vs line structure vs unknown."""
    if pd.isna(cec_type):
        return "unknown"
    t = str(cec_type).strip().upper()
    if t in _STRUCTURE_TYPES:
        return "line_structure"
    if t == "NOT AVAILABLE":
        return "unknown"
    return "substation"  # SUBSTATION / SUBSTATION ASSUMED


def _owner_util(owner_std) -> str | None:
    """Map confirmed OR assumed owner_std to the IOU code (pge/sce/sdge)."""
    if pd.isna(owner_std):
        return None
    o = str(owner_std).replace("_assumed", "")
    return o if o in _UTIL_MAP else None


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    attrs = pd.read_csv(ATTR_FILE)
    cec = pd.read_csv(CEC_FILE)
    dic = pd.read_csv(DICT_FILE)

    cec = cec.copy()
    cec["util"] = cec.owner_std.map(_owner_util)
    cec["category"] = cec["type"].map(_category)
    cec["name_norm"] = cec["name"].map(norm)

    dic = dic.copy()
    dic["cecname_norm"] = dic.CECName.map(norm)
    dic["util_lc"] = dic.Utility.str.lower()

    # ---- 1. coverage summary (scraped / coordinate / CEC-matched) ----
    cov_rows = []
    # per-utility set of normalized names our scrape "covers" a CEC record with:
    #   scraped substation names + dictionary CECName targets
    covered_by_util = {}
    for owner, U in _UTIL_MAP.items():
        s = attrs[attrs.utility == owner]
        scraped_norms = set(s.substation_name.map(norm))
        dict_norms = set(dic[dic.util_lc == owner].cecname_norm)
        covered = scraped_norms | dict_norms
        covered_by_util[owner] = covered

        cec_o = cec[cec.util == owner]
        cec_confirmed_names = set(cec_o[cec_o.owner_std == owner].name_norm)
        # a scraped sub is "CEC-matched" if its norm name is a confirmed CEC name
        # or it appears as a SourceName in the dictionary
        dict_sources = set(dic[dic.util_lc == owner].SourceName.map(norm))
        matched = s.substation_name.map(norm).isin(cec_confirmed_names | dict_sources)

        cov_rows.append({
            "utility": U,
            "scraped": len(s),
            "has_util_coord": int(s.util_lat.notna().sum()),
            "has_basin_coord": int(s.basin_lat.notna().sum()),
            "has_any_coord": int((s.util_lat.notna() | s.basin_lat.notna()).sum()),
            "no_coord_at_all": int((s.util_lat.isna() & s.basin_lat.isna()).sum()),
            "cec_name_matched": int(matched.sum()),
        })
    cov = pd.DataFrame(cov_rows)
    cov.loc["total"] = cov.sum(numeric_only=True)
    cov.loc["total", "utility"] = "TOTAL"
    cov.to_csv(OUT_DIR / "coverage_summary.csv", index=False)

    # ---- 2. CEC inventory by type (per utility) ----
    inv = (cec[cec.util.notna()]
           .groupby([cec.util.map(_UTIL_MAP), "type", "category"])
           .size().reset_index(name="cec_records")
           .rename(columns={"util": "utility"}))
    inv.to_csv(OUT_DIR / "cec_inventory_by_type.csv", index=False)

    # ---- 3. reverse gap: CEC records NOT matched to any scraped substation ----
    gap_summary = []
    for owner, U in _UTIL_MAP.items():
        cec_o = cec[cec.util == owner].copy()
        covered = covered_by_util[owner]
        cec_o["matched_to_scrape"] = cec_o.name_norm.isin(covered)

        cols = ["name", "owner_raw", "owner_std", "type", "category",
                "matched_to_scrape", "max_voltage_kv", "latitude", "longitude",
                "county", "city", "status", "cec_resolve_area", "hifld_id"]
        out = cec_o[cols].copy()
        # unmatched load-eligible substations first, then by voltage desc
        out["_sort"] = out.matched_to_scrape.astype(int) * 10 + (out.category != "substation").astype(int)
        out = out.sort_values(["_sort", "max_voltage_kv"],
                              ascending=[True, False]).drop(columns="_sort")
        out.to_csv(OUT_DIR / f"cec_unscraped_{owner}.csv", index=False)

        unmatched = cec_o[~cec_o.matched_to_scrape]
        gap_summary.append({
            "utility": U,
            "cec_total": len(cec_o),
            "cec_substations": int((cec_o.category == "substation").sum()),
            "cec_line_structures": int((cec_o.category == "line_structure").sum()),
            "cec_unknown_type": int((cec_o.category == "unknown").sum()),
            "unmatched_total": len(unmatched),
            "unmatched_substations": int((unmatched.category == "substation").sum()),
            "unmatched_line_structures": int((unmatched.category == "line_structure").sum()),
        })
    gap = pd.DataFrame(gap_summary)
    gap.loc["total"] = gap.sum(numeric_only=True)
    gap.loc["total", "utility"] = "TOTAL"

    # ---- console report ----
    pd.set_option("display.width", 140)
    print("=" * 78)
    print("1. COVERAGE - scraped substations, coordinates, CEC cross-reference")
    print("=" * 78)
    print(cov.to_string(index=False))
    print("\n  has_any_coord = utility coord OR basin fallback. no_coord_at_all is")
    print("  the only genuine coordinate gap. cec_name_matched is a cross-reference")
    print("  rate (direct name or dictionary), NOT a coordinate-availability rate.")

    print("\n" + "=" * 78)
    print("2. CEC INVENTORY BY TYPE (per utility; confirmed + assumed owners)")
    print("=" * 78)
    piv = inv.pivot_table(index="utility", columns="type", values="cec_records",
                          aggfunc="sum", fill_value=0, margins=True)
    print(piv.to_string())
    print("\n  TAP / RISER / DEAD END = line structures (no load). SUBSTATION[/ ASSUMED]")
    print("  = load-eligible. NOT AVAILABLE = unknown type.")

    print("\n" + "=" * 78)
    print("3. REVERSE GAP - CEC substations we have NO load data for")
    print("=" * 78)
    print(gap.to_string(index=False))
    print("\n  unmatched_substations = load-eligible CEC records with no scraped")
    print("  profile (real expansion candidates). unmatched_line_structures are")
    print("  taps/risers/dead-ends - filter these OUT of any projection target set.")

    print(f"\nWrote -> {OUT_DIR.relative_to(ROOT)}/")
    for f in ["coverage_summary.csv", "cec_inventory_by_type.csv",
              "cec_unscraped_pge.csv", "cec_unscraped_sce.csv", "cec_unscraped_sdge.csv"]:
        print(f"    {f}")


if __name__ == "__main__":
    main()
