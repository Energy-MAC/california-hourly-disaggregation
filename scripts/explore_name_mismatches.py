"""
explore_name_mismatches.py

Scratch script: loads DataFrames of substations that did NOT match the basin
name join, for manual inspection and dictionary building.

Run compare_substations.py -s B,D first to regenerate the CSVs.

Section B outputs (substations in utility source, absent from basin by name):
  pge_loads_only    -- 114 PGE load substations not in basin
  pge_attrs_only    -- 130 PGE attr substations not in basin
  sce_alt_only      -- 208 SCE ICA-Layer substations not in basin
  sce_scrape_only   -- unique scrape-load substations not in basin
  sdge_attrs_only   -- SDGE attr substations not in basin
  sdge_loads_only   -- SDGE load substations not in basin

Section D outputs (remainders after name + ID join):
  pge_remainder     -- PGE loads still unmatched after ID disambiguation
  sce_alt_remainder -- SCE alt still unmatched after T3-ID disambiguation
  sdge_attrs_rem    -- SDGE attrs remainder (no IDs, same as B)
  sdge_loads_rem    -- SDGE loads remainder
"""
from pathlib import Path
import pandas as pd

ROOT   = Path(__file__).resolve().parents[1]
CHECKS = ROOT / "data" / "checks"

# ── Section B: raw name-join misses ──────────────────────────────────────────

pge_loads_only = pd.read_csv(
    CHECKS / "cmp_B_pge_loads_only_in_source.csv",
    usecols=["subname", "subid", "latitude", "longitude"],
).rename(columns={"subname": "name"}).sort_values("name").reset_index(drop=True)

pge_attrs_only = pd.read_csv(
    CHECKS / "cmp_B_pge_attrs_only_in_source.csv",
    usecols=["substation_name", "substation_id", "latitude", "longitude"],
).rename(columns={"substation_name": "name", "substation_id": "subid"}).sort_values("name").reset_index(drop=True)

sce_alt_only = pd.read_csv(
    CHECKS / "cmp_B_sce_attrs_alt_only_in_source.csv",
    usecols=["SUB_NAME", "SUBST_ID", "SYS_NAME", "latitude", "longitude"],
).rename(columns={"SUB_NAME": "name", "SUBST_ID": "subst_id", "SYS_NAME": "sys_name"}).sort_values("name").reset_index(drop=True)

_sce_scrape_raw = pd.read_csv(CHECKS / "cmp_B_sce_loads_scrape_only_in_source.csv", low_memory=False)
sce_scrape_only = (
    _sce_scrape_raw[["SUBSTATION", "longitude", "latitude"]]
    .drop_duplicates("SUBSTATION")
    .rename(columns={"SUBSTATION": "name"})
    .sort_values("name")
    .reset_index(drop=True)
)

sdge_attrs_only = pd.read_csv(
    CHECKS / "cmp_B_sdge_attrs_only_in_source.csv",
    usecols=["substation_name", "latitude", "longitude"],
).rename(columns={"substation_name": "name"}).sort_values("name").reset_index(drop=True)

sdge_loads_only = pd.read_csv(
    CHECKS / "cmp_B_sdge_loads_only_in_source.csv",
    usecols=["AssetName", "latitude", "longitude"],
).drop_duplicates("AssetName").rename(columns={"AssetName": "name"}).sort_values("name").reset_index(drop=True)

# ── Section D: remainders after ID disambiguation ─────────────────────────────

pge_remainder = pd.read_csv(CHECKS / "cmp_D_pge_loads_remainder.csv") if (CHECKS / "cmp_D_pge_loads_remainder.csv").exists() else None
sce_alt_remainder = pd.read_csv(CHECKS / "cmp_D_sce_alt_remainder.csv") if (CHECKS / "cmp_D_sce_alt_remainder.csv").exists() else None
sdge_attrs_rem = pd.read_csv(CHECKS / "cmp_D_sdge_attrs_remainder.csv") if (CHECKS / "cmp_D_sdge_attrs_remainder.csv").exists() else None
sdge_loads_rem = pd.read_csv(CHECKS / "cmp_D_sdge_loads_remainder.csv") if (CHECKS / "cmp_D_sdge_loads_remainder.csv").exists() else None

# ── Quick summary on import ───────────────────────────────────────────────────

if __name__ == "__main__":
    print("Section B — raw name-join misses:")
    print(f"  pge_loads_only   : {len(pge_loads_only):>4} substations")
    print(f"  pge_attrs_only   : {len(pge_attrs_only):>4} substations")
    print(f"  sce_alt_only     : {len(sce_alt_only):>4} substations")
    print(f"  sce_scrape_only  : {len(sce_scrape_only):>4} unique substations")
    print(f"  sdge_attrs_only  : {len(sdge_attrs_only):>4} substations")
    print(f"  sdge_loads_only  : {len(sdge_loads_only):>4} substations")
    print()
    print("Section D — remainders after ID disambiguation:")
    for label, df in [("pge_remainder", pge_remainder), ("sce_alt_remainder", sce_alt_remainder),
                      ("sdge_attrs_rem", sdge_attrs_rem), ("sdge_loads_rem", sdge_loads_rem)]:
        n = len(df) if df is not None else "(run compare_substations.py -s D first)"
        print(f"  {label:<20}: {n}")
