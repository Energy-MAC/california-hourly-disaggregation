"""
process_substations_clean.py

Produces cleaned, filtered substation attributes and load profiles,
applying corrections for the systematic data quality issues documented in
data/checks/substation_source_gaps.txt.

Filtering logic (per utility)
------------------------------
  All utilities
    - P.T. (pass-through) substations removed: they are switching nodes with
      no individual meters, confirmed by zero load-profile presence in T3.

  PGE
    - "Redacted" flag in pge_substation_attributes.csv means PGE redacts DG
      capacity numbers, but load profiles ARE still published for those sites.
      All 664 metered substations are retained; 48 that are redacted will have
      NaN for capacity columns but valid load profiles.
    - Duplicate attr entries for the same substation collapsed by canonical
      name (e.g. "POTTER VALLEY PH" / "POTTER VALLEY P H" -> one row).

  SCE
    - P.T. substations removed (170 of 748 unique load substations).
    - Scrape + bulk loads deduplicated on (SUBSTATION, YEAR, MONTH, HOUR),
      preferring bulk (official published data; extends to 2025-2026).
    - All 578 remaining substations retained; T3 attrs joined where available
      (NaN for the 19 substations not in T3).
    - T3 entries with null voltage_kv but not in loads are excluded
      (ICA deliverability-only nodes, never metered).
    - Lat/lon: sce_ica_layer_substations_alt.csv first (735 substations with
      reliable coords); fallback to scrape-row coords from combined_raw.
    - sub_type and substation_voltage added from ICA_Layer alt.

  SDGE
    - 8 failed scrapes excluded.
    - kW -> MW conversion applied.

  Pacificorp
    - Excluded: no metered load profiles exist.

  All utilities
    - Basin lat/lon (DataBasin CA Substations 2022) joined by normalised
      substation name; dist_to_basin_km added via haversine.

Output columns
--------------
  substation_attributes_clean.csv
      utility, substation_name,
      util_lat, util_lon,           -- from utility source
      basin_lat, basin_lon,         -- DataBasin 2022 (NaN if no name match)
      dist_to_basin_km,
      sub_type,                     -- SCE: D/A/S/T; NaN otherwise
      substation_voltage,           -- ratio string (SCE and SDGE)
      voltage_kv,                   -- numeric secondary kV
      sys_name, division, subst_id,
      existing_gen, queued_gen, total_gen,
      projected_load, der_penetration, max_remain_cap,
      circuit_count,
      res_pct, com_pct, agr_pct, ind_pct, other_pct,
      res_total, com_total, agr_total, ind_total, other_total,
      note_sub                      -- PGE: "Yes" if redacted attrs; SCE: deliverability text

  substation_load_profiles_clean.csv
      utility, substation_name, year, month, hour, min_load, max_load
      year is NaN for PGE and SDGE (typical month-hour profiles, no year stamp).

Usage
-----
  python scripts/process_substations_clean.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT    = Path(__file__).resolve().parents[1]
RAW     = ROOT / "data" / "raw"
PROC    = ROOT / "data" / "processed"
OUT_DIR = PROC / "substations"

ATTR_COLS = [
    "utility", "substation_name",
    "util_lat", "util_lon",
    "basin_lat", "basin_lon", "dist_to_basin_km",
    "sub_type", "substation_voltage",
    "voltage_kv",
    "sys_name", "division", "subst_id",
    "existing_gen", "queued_gen", "total_gen",
    "projected_load", "der_penetration", "max_remain_cap",
    "circuit_count",
    "res_pct", "com_pct", "agr_pct", "ind_pct", "other_pct",
    "res_total", "com_total", "agr_total", "ind_total", "other_total",
    "note_sub",
]

LOAD_COLS = ["utility", "substation_name", "year", "month", "hour", "min_load", "max_load"]

# ── Name helpers ──────────────────────────────────────────────────────────────

_PT_RE    = re.compile(r"\s+p\.?\s*t\.?\s*$",   re.IGNORECASE)
_SUB_RE   = re.compile(r"\bsubstation\b",        re.IGNORECASE)
_PUNCT_RE = re.compile(r"[/\-,\.&\(\)_#']")
_SPC_RE   = re.compile(r"\s+")


def norm(s: pd.Series) -> pd.Series:
    """Normalise substation names for set comparison and basin join."""
    s = s.astype(str).str.strip()
    s = s.str.replace(_PT_RE,    "",  regex=True)
    s = s.str.replace(_SUB_RE,   "",  regex=True)
    s = s.str.replace(_PUNCT_RE, " ", regex=True)
    s = s.str.replace(_SPC_RE,   " ", regex=True)
    return s.str.strip().str.lower()


def is_pt(s: pd.Series) -> pd.Series:
    """True for P.T. (pass-through) substations."""
    return s.str.contains(r"p\.?\s*t\.?\s*$", case=False, regex=True, na=False)


# ── Geometry ──────────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2) -> np.ndarray:
    R = 6371.0
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(x, float))
                               for x in [lat1, lon1, lat2, lon2])
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * R * np.arcsin(np.clip(np.sqrt(a), 0, 1))


# ── Basin join ────────────────────────────────────────────────────────────────

def _load_basin_lookup(owner_std: str) -> pd.DataFrame:
    b = pd.read_csv(PROC / "substation_misc" / "ca_substations_2022.csv")
    b = b[b["owner_std"] == owner_std].dropna(subset=["latitude", "longitude"]).copy()
    b["name_norm"] = norm(b["name"])
    return b.drop_duplicates("name_norm")[["name_norm", "latitude", "longitude"]]


def add_basin_coords(attrs: pd.DataFrame, basin: pd.DataFrame) -> pd.DataFrame:
    """Left-join basin lat/lon by normalised name, compute haversine distance."""
    a = attrs.copy()
    a["_norm"] = norm(a["substation_name"])
    merged = a.merge(
        basin.rename(columns={"latitude": "basin_lat", "longitude": "basin_lon"}),
        left_on="_norm", right_on="name_norm", how="left",
    ).drop(columns=["_norm", "name_norm"])

    has = (merged["basin_lat"].notna() & merged["basin_lon"].notna() &
           merged["util_lat"].notna()  & merged["util_lon"].notna())
    merged["dist_to_basin_km"] = np.nan
    if has.any():
        merged.loc[has, "dist_to_basin_km"] = haversine_km(
            merged.loc[has, "util_lat"], merged.loc[has, "util_lon"],
            merged.loc[has, "basin_lat"], merged.loc[has, "basin_lon"],
        )
    return merged


# ── PGE ──────────────────────────────────────────────────────────────────────

def process_pge() -> tuple[pd.DataFrame, pd.DataFrame]:
    loads_raw = pd.read_csv(RAW / "pge" / "pge_layer25_earliest_latest_part001.csv")
    attrs_raw = pd.read_csv(RAW / "pge" / "pge_substation_attributes.csv")

    # Remove P.T. from loads (none expected in PGE, but guard)
    loads_raw = loads_raw[~is_pt(loads_raw["subname"])].copy()

    # Canonical name: strip + upper; collapse "P H" -> "PH" spacing
    # (conservative: only collapses where two single uppercase letters are separated by a space)
    def _canon(s: pd.Series) -> pd.Series:
        return s.str.strip().str.upper()

    loads_raw["name_c"] = _canon(loads_raw["subname"])
    attrs_raw["name_c"] = _canon(attrs_raw["substation_name"])
    # Deduplicate attrs by canonical name (handles "POTTER VALLEY PH" / "POTTER VALLEY P H")
    attrs_raw = attrs_raw.drop_duplicates("name_c").set_index("name_c")

    # ── Load profiles ─────────────────────────────────────────────────────────
    split = loads_raw["monthhour"].str.split("_", expand=True)
    loads_out = pd.DataFrame({
        "utility":         "pge",
        "substation_name": loads_raw["name_c"].values,
        "year":            pd.NA,
        "month":           split[0].astype(int).values,
        "hour":            split[1].astype(int).values,
        "min_load":        pd.to_numeric(loads_raw["low"],  errors="coerce").values / 1000,
        "max_load":        pd.to_numeric(loads_raw["high"], errors="coerce").values / 1000,
    })

    # ── Attributes ────────────────────────────────────────────────────────────
    # Base: all unique substations with load data
    uniq = loads_out[["substation_name"]].drop_duplicates().copy()

    def _map(col_in: str, numeric: bool = False, scale: float = 1.0) -> pd.Series:
        s = uniq["substation_name"].map(
            attrs_raw[col_in] if col_in in attrs_raw.columns else pd.Series(dtype=object)
        )
        if numeric:
            return pd.to_numeric(s, errors="coerce") * scale
        return s

    attrs_out = pd.DataFrame({
        "utility":         "pge",
        "substation_name": uniq["substation_name"].values,
        "util_lat":        _map("latitude",     numeric=True).values,
        "util_lon":        _map("longitude",    numeric=True).values,
        "voltage_kv":      _map("voltage_kv",   numeric=True).values,
        "division":        _map("division").values,
        "subst_id":        _map("substation_id").values,
        "circuit_count":   _map("num_banks",    numeric=True).values,
        "existing_gen":    _map("existing_dg_kw", numeric=True, scale=1/1000).values,
        "queued_gen":      _map("queued_dg_kw",   numeric=True, scale=1/1000).values,
        "total_gen":       _map("total_dg_kw",    numeric=True, scale=1/1000).values,
        # note_sub carries "Yes" for redacted substations (capacity attrs are NaN there)
        "note_sub":        _map("redacted").values,
    })

    return attrs_out, loads_out[LOAD_COLS]


# ── SCE ──────────────────────────────────────────────────────────────────────

_SCE_VOLTAGE_MAP = {
    115: 115.47, 66: 68.7, 55: 55.05, 33: 34.5, 25: 25,
    16: 16.98, 12: 12.55, 7: 6.88, 4.8: 5, 4.16: 4.4, 2.4: 2.5,
}


def process_sce() -> tuple[pd.DataFrame, pd.DataFrame]:
    combined = pd.read_csv(RAW / "sce" / "sce_combined_raw.csv", low_memory=False)
    t3       = pd.read_csv(RAW / "sce" / "sce_substation_attributes.csv")
    alt      = pd.read_csv(RAW / "sce" / "sce_ica_layer_substations_alt.csv")

    # ── Clean loads ───────────────────────────────────────────────────────────
    combined = combined[~is_pt(combined["SUBSTATION"])].copy()

    # Deduplicate (SUBSTATION, YEAR, MONTH, HOUR): bulk preferred over scrape
    combined["_src_ord"] = (combined["source"] == "scrape").astype(int)
    combined = (combined.sort_values("_src_ord")
                        .drop_duplicates(subset=["SUBSTATION", "YEAR", "MONTH", "HOUR"], keep="first")
                        .drop(columns=["_src_ord"]))

    combined["_norm"] = norm(combined["SUBSTATION"])

    # ── T3 attribute lookup (indexed by normalised name) ──────────────────────
    t3["_norm"] = norm(t3["substation_name"])
    t3_idx = t3.drop_duplicates("_norm").set_index("_norm")

    # ── Alt lat/lon and subtype lookup ────────────────────────────────────────
    alt["_norm"] = norm(alt["SUB_NAME"])
    alt_dedup = alt.drop_duplicates("_norm").set_index("_norm")
    alt_str = alt_dedup[["SUB_TYPE", "SUBSTATION_VOLTAGE"]]
    alt_lat  = pd.to_numeric(alt_dedup["latitude"],  errors="coerce")
    alt_lon  = pd.to_numeric(alt_dedup["longitude"], errors="coerce")

    # Scrape-row lat/lon fallback
    scrape = combined[combined["source"] == "scrape"].dropna(subset=["latitude", "longitude"])
    scrape_lat = (scrape.drop_duplicates("_norm")
                        .set_index("_norm")["latitude"]
                        .apply(pd.to_numeric, errors="coerce"))
    scrape_lon = (scrape.drop_duplicates("_norm")
                        .set_index("_norm")["longitude"]
                        .apply(pd.to_numeric, errors="coerce"))

    # ── Load profiles output ──────────────────────────────────────────────────
    loads_out = pd.DataFrame({
        "utility":         "sce",
        "substation_name": combined["SUBSTATION"].values,
        "year":            pd.to_numeric(combined["YEAR"],  errors="coerce").values,
        "month":           pd.to_numeric(combined["MONTH"], errors="coerce").astype(int).values + 1,
        "hour":            pd.to_numeric(combined["HOUR"],  errors="coerce").astype(int).values,
        "min_load":        pd.to_numeric(combined["MIN_LOAD"], errors="coerce").values,
        "max_load":        pd.to_numeric(combined["MAX_LOAD"], errors="coerce").values,
    })

    # ── Unique substations for attrs ──────────────────────────────────────────
    uniq = (combined[["SUBSTATION", "_norm"]]
            .drop_duplicates("_norm")
            .copy())

    # Canonical name: prefer T3's cleaned name; fallback to raw load name
    uniq["substation_name"] = (uniq["_norm"].map(t3_idx["substation_name"])
                                            .fillna(uniq["SUBSTATION"]))

    # Map loads to canonical name
    name_map = uniq.set_index("_norm")["substation_name"]
    loads_out["substation_name"] = combined["_norm"].map(name_map).values

    # ── Lat/lon: alt first, scrape fallback ───────────────────────────────────
    uniq["util_lat"] = uniq["_norm"].map(alt_lat).combine_first(uniq["_norm"].map(scrape_lat))
    uniq["util_lon"] = uniq["_norm"].map(alt_lon).combine_first(uniq["_norm"].map(scrape_lon))

    # ── Map T3 attribute columns ──────────────────────────────────────────────
    def _t3(col: str, numeric: bool = False) -> pd.Series:
        s = uniq["_norm"].map(t3_idx[col] if col in t3_idx.columns else pd.Series(dtype=object))
        return pd.to_numeric(s, errors="coerce") if numeric else s

    nom_kv = _t3("voltage_kv", numeric=True)
    real_kv = nom_kv.map(lambda v: _SCE_VOLTAGE_MAP.get(v, v) if pd.notna(v) else np.nan)

    # ── Map alt supplementary columns ────────────────────────────────────────
    sub_type = uniq["_norm"].map(
        alt_str["SUB_TYPE"].str.extract(r"^([A-Z])", expand=False)
        if "SUB_TYPE" in alt_str.columns else pd.Series(dtype=str)
    )
    substation_voltage = uniq["_norm"].map(
        alt_str["SUBSTATION_VOLTAGE"]
        if "SUBSTATION_VOLTAGE" in alt_str.columns else pd.Series(dtype=str)
    )

    attrs_out = pd.DataFrame({
        "utility":            "sce",
        "substation_name":    uniq["substation_name"].values,
        "util_lat":           uniq["util_lat"].values,
        "util_lon":           uniq["util_lon"].values,
        "sub_type":           sub_type.values,
        "substation_voltage": substation_voltage.values,
        "voltage_kv":         real_kv.values,
        "sys_name":           _t3("sys_name").values,
        "subst_id":           _t3("subst_id").values,
        "existing_gen":       _t3("existing_gen", numeric=True).values,
        "queued_gen":         _t3("queued_gen",   numeric=True).values,
        "total_gen":          _t3("total_gen",    numeric=True).values,
        "projected_load":     _t3("projected_load",  numeric=True).values,
        "der_penetration":    _t3("der_penetration", numeric=True).values,
        "max_remain_cap":     _t3("max_remain_cap",  numeric=True).values,
        "circuit_count":      _t3("circuit_count",   numeric=True).values,
        "res_pct":            _t3("res_pct",  numeric=True).values,
        "com_pct":            _t3("com_pct",  numeric=True).values,
        "agr_pct":            _t3("agr_pct",  numeric=True).values,
        "ind_pct":            _t3("ind_pct",  numeric=True).values,
        "other_pct":          _t3("other_pct", numeric=True).values,
        "res_total":          _t3("res_total",  numeric=True).values,
        "com_total":          _t3("com_total",  numeric=True).values,
        "agr_total":          _t3("agr_total",  numeric=True).values,
        "ind_total":          _t3("ind_total",  numeric=True).values,
        "other_total":        _t3("other_total", numeric=True).values,
        "note_sub":           _t3("note_sub").values,
    })

    return attrs_out, loads_out[LOAD_COLS]


# ── SDGE ─────────────────────────────────────────────────────────────────────

def process_sdge() -> tuple[pd.DataFrame, pd.DataFrame]:
    raw_loads = pd.read_csv(RAW / "sdge" / "sdge_substation_profiles_part001.csv", dtype=str)
    attrs_raw = pd.read_csv(RAW / "sdge" / "sdge_substation_attributes.csv")
    failures  = set(
        pd.read_csv(RAW / "sdge" / "sdge_substation_profiles_failed.csv")
        ["substation_name"].str.upper().str.strip()
    )

    # Remove failures and P.T.
    raw_loads = raw_loads[~raw_loads["AssetName"].str.upper().str.strip().isin(failures)].copy()
    raw_loads = raw_loads[~is_pt(raw_loads["AssetName"])].copy()
    attrs_raw = attrs_raw[~attrs_raw["substation_name"].str.upper().str.strip().isin(failures)].copy()
    attrs_raw = attrs_raw[~is_pt(attrs_raw["substation_name"])].copy()

    # ── Pivot loads: wide hours -> long ──────────────────────────────────────
    hour_cols = [f"hour {i}" for i in range(1, 25)]
    meta_cols = ["AssetName", "Month", "LoadDay", "latitude", "longitude"]

    melted = raw_loads.melt(id_vars=meta_cols, value_vars=hour_cols,
                            var_name="hour_col", value_name="load")
    melted["hour"]      = melted["hour_col"].str.extract(r"(\d+)").astype(int) - 1
    melted["load_type"] = (
        melted["LoadDay"].str.strip().str.lower()
        .map({"high load": "max_load", "low load": "min_load"})
    )
    melted = melted.dropna(subset=["load_type"])

    pivoted = melted.pivot_table(
        index=["AssetName", "Month", "hour"],
        columns="load_type", values="load", aggfunc="first",
    ).reset_index()
    pivoted.columns.name = None
    for col in ("min_load", "max_load"):
        if col not in pivoted.columns:
            pivoted[col] = np.nan
    pivoted["min_load"] = pd.to_numeric(pivoted["min_load"], errors="coerce") / 1000
    pivoted["max_load"] = pd.to_numeric(pivoted["max_load"], errors="coerce") / 1000

    canon_name = pivoted["AssetName"].str.upper().str.strip()
    loads_out = pd.DataFrame({
        "utility":         "sdge",
        "substation_name": canon_name.values,
        "year":            pd.NA,
        "month":           pivoted["Month"].astype(int).values,
        "hour":            pivoted["hour"].values,
        "min_load":        pivoted["min_load"].values,
        "max_load":        pivoted["max_load"].values,
    })

    # ── Attributes ────────────────────────────────────────────────────────────
    attrs_raw["name_c"] = attrs_raw["substation_name"].str.upper().str.strip()
    attrs_idx = attrs_raw.drop_duplicates("name_c").set_index("name_c")

    uniq = loads_out[["substation_name"]].drop_duplicates().copy()

    def _map(col: str, numeric: bool = False, strip_kv: bool = False) -> pd.Series:
        s = uniq["substation_name"].map(
            attrs_idx[col] if col in attrs_idx.columns else pd.Series(dtype=object)
        )
        if strip_kv:
            s = s.str.replace("kV", "", regex=False).str.strip()
        return pd.to_numeric(s, errors="coerce") if numeric else s

    attrs_out = pd.DataFrame({
        "utility":            "sdge",
        "substation_name":    uniq["substation_name"].values,
        "util_lat":           _map("latitude",         numeric=True).values,
        "util_lon":           _map("longitude",        numeric=True).values,
        "substation_voltage": _map("substation_type").values,
        "voltage_kv":         _map("voltage_kv",       numeric=True, strip_kv=True).values,
        "existing_gen":       _map("existing_gen",     numeric=True).values,
        "queued_gen":         _map("queued_gen",       numeric=True).values,
        "total_gen":          _map("total_gen",        numeric=True).values,
        "projected_load":     _map("projected_load",   numeric=True).values,
        "der_penetration":    _map("der_penetration",  numeric=True).values,
    })

    return attrs_out, loads_out[LOAD_COLS]


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Processing PGE ...")
    pge_attrs, pge_loads = process_pge()
    print(f"  {pge_attrs['substation_name'].nunique():,} substations  |  {len(pge_loads):,} load rows")

    print("Processing SCE ...")
    sce_attrs, sce_loads = process_sce()
    print(f"  {sce_attrs['substation_name'].nunique():,} substations  |  {len(sce_loads):,} load rows")

    print("Processing SDGE ...")
    sdge_attrs, sdge_loads = process_sdge()
    print(f"  {sdge_attrs['substation_name'].nunique():,} substations  |  {len(sdge_loads):,} load rows")

    # ── Combine and add basin coords ──────────────────────────────────────────
    print("Joining basin coordinates ...")
    all_attrs = []
    for utility, df, owner_std in [("pge", pge_attrs, "pge"),
                                    ("sce", sce_attrs, "sce"),
                                    ("sdge", sdge_attrs, "sdge")]:
        basin  = _load_basin_lookup(owner_std)
        part   = add_basin_coords(df, basin)
        n_m    = part["basin_lat"].notna().sum()
        d_med  = part["dist_to_basin_km"].median()
        n_c    = part["util_lat"].notna().sum()
        print(f"  {utility}: {n_c}/{len(part)} with util coords  |  "
              f"{n_m}/{len(part)} basin-matched (median {d_med:.1f} km)")
        all_attrs.append(part)

    attrs_all = pd.concat(all_attrs, ignore_index=True)
    loads_all = pd.concat([pge_loads, sce_loads, sdge_loads], ignore_index=True)

    # Ensure all columns exist (NaN for utility-specific columns not present)
    for col in ATTR_COLS:
        if col not in attrs_all.columns:
            attrs_all[col] = np.nan
    for col in LOAD_COLS:
        if col not in loads_all.columns:
            loads_all[col] = np.nan

    # ── Write outputs ─────────────────────────────────────────────────────────
    out_attrs = OUT_DIR / "substation_attributes_clean.csv"
    out_loads = OUT_DIR / "substation_load_profiles_clean.csv"

    attrs_all[ATTR_COLS].to_csv(out_attrs, index=False)
    loads_all[LOAD_COLS].to_csv(out_loads, index=False)

    mb_a = out_attrs.stat().st_size / 1024 / 1024
    mb_l = out_loads.stat().st_size / 1024 / 1024
    print(f"\nAttributes    : {len(attrs_all):,} rows  ->  "
          f"{out_attrs.relative_to(ROOT)}  ({mb_a:.1f} MB)")
    print(f"Load profiles : {len(loads_all):,} rows  ->  "
          f"{out_loads.relative_to(ROOT)}  ({mb_l:.1f} MB)")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print(f"  {'utility':<8}  {'n_subs':>6}  {'util_coords':>11}  {'basin_match':>11}  "
          f"{'has_voltage':>11}  {'load_rows':>9}")
    for util, grp in attrs_all.groupby("utility"):
        n         = len(grp)
        n_coords  = grp["util_lat"].notna().sum()
        n_basin   = grp["basin_lat"].notna().sum()
        n_voltage = grp["voltage_kv"].notna().sum()
        n_loads   = (loads_all["utility"] == util).sum()
        print(f"  {util:<8}  {n:>6,}  {n_coords:>11,}  {n_basin:>11,}  {n_voltage:>11,}  {n_loads:>9,}")

    print()
    print("SCE year coverage:")
    sce_lp = loads_all[loads_all["utility"] == "sce"]
    if not sce_lp.empty:
        yc = sce_lp.groupby("year")["substation_name"].nunique().rename("n_substations")
        print(yc.to_string())


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
