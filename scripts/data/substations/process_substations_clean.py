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
      capacity numbers for certain substations, but load profiles ARE still
      published for those sites.  Confirmed by structural observation: the 48
      redacted substations (note_sub="Yes") have NaN in capacity columns but
      non-NaN values in the layer 25 load profile data.
      All 664 metered substations are retained.
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
    - Basin lat/lon (DataBasin CA Substations 2022). Candidates come from:
        1. Exact normalised-name match against the basin dataset.
        2. Fallback dictionary lookup via data/basinSourceDictionary.csv for
           substations whose names differ between the utility source and basin
           (e.g. "CRESTA PH" -> "Cresta", "DRUM" -> "Drum 1" / "Drum 2").
      The candidate pool is this utility's basin rows PLUS rows basin labels
      owner `other`/`unknown`, which it does for many IOU-owned stations.
      Candidates are resolved by _pick_basin_row(): own-owner rows first and
      never distance-gated, nearest wins among duplicates (basin has two PGE
      `Live Oak` 481 km apart), and a relaxed row is admitted only within
      BASIN_RELAXED_MAX_KM. dist_to_basin_km is computed from the chosen row.
      This whole step runs AFTER the coordinate overrides, because it keys on
      util_lat/util_lon.

  High-side voltage (highside_kv)
    - SCE/SDGE: first token of substation_voltage (e.g. "115/33 kV" -> 115),
      a pure transform, no join.
    - PGE (and fallback for all): CEC max_voltage_kv attached via .map() on
      normalized substation name (norm() from build_cec_name_dictionary.py),
      using cecSourceDictionary.csv for names that differ from CEC's. .map()
      is used deliberately instead of merge -- an unmatched name yields NaN,
      it can never drop or duplicate a row. main() asserts substation counts
      are unchanged before/after this step.

Output columns
--------------
  substation_attributes_clean.csv
      utility, substation_name,
      util_lat, util_lon,           -- from utility source
      coord_source,                 -- 'utility', the override file's source, or ''
      basin_lat, basin_lon,         -- DataBasin 2022 (NaN if no match)
      dist_to_basin_km,             -- NaN when either coordinate is missing
      sub_type,                     -- SCE: D/A/S/T; NaN otherwise
      substation_voltage,           -- ratio string (SCE and SDGE)
      voltage_kv,                   -- numeric secondary kV
      highside_kv,                  -- transmission voltage: substation_voltage's
                                     -- first token (SCE/SDGE) else CEC max_voltage_kv
                                     -- (all utilities, only source for PGE)
      highside_kv_source,           -- "utility" | "cec" | "none"
      cec_max_voltage_kv,           -- raw CEC value (NaN/-99 sentinel dropped)
      sys_name, division, subst_id,
      existing_gen, queued_gen, total_gen,
      projected_load, der_penetration, max_remain_cap,
      circuit_count,
      res_pct, com_pct, agr_pct, ind_pct, other_pct,
      res_total, com_total, agr_total, ind_total, other_total,
      note_sub                      -- PGE: "Yes" if redacted attrs; SCE: deliverability text

  substation_load_profiles_clean.csv
      utility, substation_name, year, month, hour, hour_pst, min_load, max_load
      year is NaN for PGE and SDGE (typical month-hour profiles, no year stamp).

Usage
-----
  python scripts/data/substations/process_substations_clean.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from build_cec_name_dictionary import norm as cec_norm  # noqa: E402

ROOT     = Path(__file__).resolve().parents[3]
RAW      = ROOT / "data" / "raw"
PROC     = ROOT / "data" / "processed"
OUT_DIR  = PROC / "substations"
DICT_PATH = ROOT / "data" / "basinSourceDictionary.csv"
CEC_FILE = PROC / "substation_misc" / "ca_substations_cec.csv"
CEC_DICT_PATH = ROOT / "data" / "cecSourceDictionary.csv"
# Hand-curated coordinates for substations no automatic source can place (see
# apply_coordinate_overrides). Same hand-maintained tier as the two name dicts.
COORD_OVERRIDE_PATH = ROOT / "data" / "substationCoordinateOverrides.csv"

# PGE removed some substations from their published ArcGIS layer between scrapes.
# The older non-clean processed file is the only remaining source for those profiles.
# Load values there are already in MW (converted during earlier processing run).
LEGACY_PGE_LOADS = OUT_DIR / "substation_load_profiles.csv"
LEGACY_PGE_ATTRS = OUT_DIR / "substation_attributes.csv"

ATTR_COLS = [
    "utility", "substation_name",
    "util_lat", "util_lon", "coord_source",
    "basin_lat", "basin_lon", "dist_to_basin_km",
    "sub_type", "substation_voltage",
    "voltage_kv",
    "highside_kv", "highside_kv_source", "cec_max_voltage_kv",
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


def _build_dict_map(utility_upper: str) -> dict[str, list[str]]:
    """
    Load data/basinSourceDictionary.csv and return a mapping
      norm(SourceName) -> [norm(BasinName), ...]
    filtered to the given utility label ("PGE", "SCE", or "SDGE").

    Returns an empty dict if the file does not exist or has no entries for
    this utility.  One SourceName can map to multiple BasinNames (e.g. DRUM
    maps to both Drum 1 and Drum 2 in the basin dataset).
    """
    if not DICT_PATH.exists():
        return {}
    d = pd.read_csv(DICT_PATH)
    d = d[d["Utility"].str.strip().str.upper() == utility_upper].copy()
    if d.empty:
        return {}
    d["src_norm"] = norm(d["SourceName"])
    d["bas_norm"] = norm(d["BasinName"])
    mapping: dict[str, list[str]] = {}
    for _, row in d.iterrows():
        mapping.setdefault(row["src_norm"], []).append(row["bas_norm"])
    return mapping


# ── Geometry ──────────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2) -> np.ndarray:
    R = 6371.0  # mean Earth radius (km); IAU/GRS80 value, see https://en.wikipedia.org/wiki/Earth_radius
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(x, float))
                               for x in [lat1, lon1, lat2, lon2])
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * R * np.arcsin(np.clip(np.sqrt(a), 0, 1))


# ── Basin join ────────────────────────────────────────────────────────────────

# Max separation at which a basin row NOT owned by this utility may be accepted.
# Calibrated on rows whose names already agree exactly, where median separation is
# 36 m (PGE), 57 m (SCE) and 1,340 m (SDGE).  All 23 matches this admits land at
# <= 244 m and the next candidate is at 1.0 km, so the threshold sits in a wide
# empty band.  Own-owner candidates are deliberately NOT distance-gated: SDGE's
# published coordinates are genuinely imprecise and gating them would discard
# correct matches.  SDGE gains nothing from the relaxed pool at any threshold
# (its unmatched substations' nearest non-SDGE rows are 2.7 km+ away), so this
# single global value costs it nothing.
BASIN_RELAXED_MAX_KM = 0.25


def _load_basin_lookup(owner_std: str) -> pd.DataFrame:
    """Candidate basin rows for one utility: its own, plus `other`/`unknown`.

    Basin mislabels many IOU-owned stations as owner `Other` or `Unknown`, so an
    `owner_std == utility` filter hides correct rows -- 23 profiled substations
    have an exact-name basin row that the filter made invisible, and it also made
    three reviewed basinSourceDictionary entries inert (DAVIS, IONE,
    Blythe (Walc)).  The relaxed rows are marked so the caller can require them
    to agree on coordinates (BASIN_RELAXED_MAX_KM) while leaving own-owner rows
    ungated.

    Rows literally named "Unknown" stay excluded.  Duplicate `name_norm` values
    are NOT collapsed -- picking the first silently took the wrong row (PGE's two
    `Live Oak` rows are 481 km apart), so the caller resolves by distance.
    """
    b = pd.read_csv(PROC / "substation_misc" / "ca_substations_2022.csv")
    b = b[b["owner_std"].isin([owner_std, "other", "unknown"])].copy()
    b = b.dropna(subset=["latitude", "longitude"])
    b = b[b["name"].str.strip().str.lower() != "unknown"].copy()
    b["name_norm"] = norm(b["name"])
    b["relaxed"] = b["owner_std"] != owner_std
    return b[["name_norm", "latitude", "longitude", "relaxed"]].reset_index(drop=True)


def _pick_basin_row(cands: pd.DataFrame, u_lat, u_lon):
    """Choose one basin row for a substation; returns (latitude, longitude) or None.

    Two rules, in this order:

    1. OWN-OWNER PRIORITY.  Rows this utility owns are preferred and are never
       distance-gated.  Among several (basin has duplicate names -- PGE has two
       `Live Oak` 481 km apart, SDGE two `Eastgate`), the one nearest the
       substation's own coordinate wins.
    2. RELAXED FALL-THROUGH.  A row basin labels `other`/`unknown` is accepted
       only within BASIN_RELAXED_MAX_KM, and only when there is no own-owner
       candidate, or the best own-owner candidate is itself beyond that distance
       while the relaxed one is inside it.  So a relaxed row can rescue a
       substation with no match and can replace a clearly-wrong match, but never
       displaces an own-owner row that already agrees.

    With no coordinate to compare against, the first own-owner row is taken and
    relaxed rows are unusable, having nothing to verify them with.  Since the
    caller runs after apply_coordinate_overrides(), only a substation unplaced by
    every source reaches that branch (currently just SCE `Autobody`).
    """
    if cands.empty:
        return None
    own = cands[~cands["relaxed"]]
    rel = cands[cands["relaxed"]]

    if pd.isna(u_lat) or pd.isna(u_lon):
        if own.empty:
            return None
        r = own.iloc[0]
        return float(r["latitude"]), float(r["longitude"])

    def nearest(df):
        if df.empty:
            return None, np.inf
        d = haversine_km(np.full(len(df), float(u_lat)), np.full(len(df), float(u_lon)),
                         df["latitude"].to_numpy(float), df["longitude"].to_numpy(float))
        i = int(np.argmin(d))
        return df.iloc[i], float(d[i])

    own_row, own_d = nearest(own)
    rel_row, rel_d = nearest(rel)

    use = None
    if own_row is not None:
        use = own_row
        if rel_row is not None and own_d > BASIN_RELAXED_MAX_KM >= rel_d:
            use = rel_row
    elif rel_row is not None and rel_d <= BASIN_RELAXED_MAX_KM:
        use = rel_row

    if use is None:
        return None
    return float(use["latitude"]), float(use["longitude"])


def add_basin_coords(
    attrs: pd.DataFrame,
    basin: pd.DataFrame,
    dict_map: dict[str, list[str]] | None = None,
) -> pd.DataFrame:
    """
    Attach basin lat/lon to attrs, then compute haversine distance.

    Candidates for each substation come from, in order (the dictionary is an
    EXCEPTIONS list, so exact matching always goes first):

      1. exact normalised-name match against the basin lookup;
      2. the basinSourceDictionary mappings in dict_map, for rows where step 1
         produced no ACCEPTED row -- either it had no candidate, or every
         candidate failed the guard.

    Whichever step supplies them, candidates are resolved by `_pick_basin_row`
    (own-owner priority, nearest-wins, distance guard on relaxed rows).  Row
    count and order are preserved -- this is a per-row assignment, not a merge,
    because the basin lookup now carries duplicate `name_norm` values on purpose.

    Call this AFTER apply_coordinate_overrides(): every candidate decision and
    dist_to_basin_km depend on util_lat/util_lon, and 11 substations only get a
    coordinate from the override table.
    """
    merged = attrs.copy()
    # not "_norm": itertuples() renames leading-underscore columns positionally
    merged["norm_key"] = norm(merged["substation_name"])
    by_name = {n: g for n, g in basin.groupby("name_norm", sort=False)}

    lats: list[float] = []
    lons: list[float] = []
    empty = basin.iloc[0:0]
    for row in merged.itertuples(index=False):
        n = row.norm_key
        hit = _pick_basin_row(by_name.get(n, empty), row.util_lat, row.util_lon)
        # Dictionary second, and only if the exact name FAILED TO RESOLVE -- not
        # merely if it had no candidate. The relaxed pool can surface a
        # same-named row that the guard then rejects (PGE BUCKS CREEK has an
        # `unknown`-owner `Bucks Creek` 9.1 km away, while the dictionary points
        # at `Grizzly` 95 m away), and that must not shadow the dictionary.
        if hit is None and dict_map:
            parts = [by_name[bn] for bn in dict_map.get(n, ()) if bn in by_name]
            if parts:
                hit = _pick_basin_row(pd.concat(parts, ignore_index=True),
                                      row.util_lat, row.util_lon)
        lats.append(hit[0] if hit else np.nan)
        lons.append(hit[1] if hit else np.nan)

    merged["basin_lat"] = lats
    merged["basin_lon"] = lons
    merged = merged.drop(columns=["norm_key"])

    has = (merged["basin_lat"].notna() & merged["basin_lon"].notna() &
           merged["util_lat"].notna()  & merged["util_lon"].notna())
    merged["dist_to_basin_km"] = np.nan
    if has.any():
        merged.loc[has, "dist_to_basin_km"] = haversine_km(
            merged.loc[has, "util_lat"], merged.loc[has, "util_lon"],
            merged.loc[has, "basin_lat"], merged.loc[has, "basin_lon"],
        )
    return merged


# ── High-side (transmission) voltage ────────────────────────────────────────
#
# SCE/SDGE publish a transformer-ratio string (substation_voltage, e.g.
# "115/33 kV") whose first token is the high side -- a pure transform of a
# column already in the frame, no join involved.  PGE publishes no high-side
# field at all, so its only source is CEC's max_voltage_kv, attached via
# normalized-name .map() (never .merge()) so an unmatched name yields NaN
# and can never drop or duplicate a substation row -- critical because PGE's
# legacy-recovered substations (see LEGACY_PGE_* below) exist nowhere else.

def _load_cec_voltage_lookup() -> dict[tuple[str, str], float]:
    """(utility, norm(substation_name)) -> CEC max_voltage_kv.

    Built from a direct normalized-name match against CEC records of the same
    (or "_assumed") owner, plus a cecSourceDictionary.csv fallback for names
    that differ between the utility source and CEC.  Uses norm() imported from
    build_cec_name_dictionary.py -- the same function the dictionary itself
    was built with -- so dictionary lookups stay consistent.  The -99 sentinel
    (CEC's "unknown voltage" marker, SDGE-only) and NaN are dropped.
    """
    if not CEC_FILE.exists():
        print("  WARNING: CEC substation file not found "
              f"({CEC_FILE.relative_to(ROOT)}); highside_kv will use utility data only.")
        return {}

    cec = pd.read_csv(CEC_FILE)
    cec = cec[cec.max_voltage_kv.notna() & (cec.max_voltage_kv != -99)].copy()
    cec["owner_base"] = cec.owner_std.astype(str).str.replace("_assumed", "", regex=False)
    cec["name_norm"] = cec.name.map(cec_norm)
    cec_by_name = (cec.drop_duplicates(["owner_base", "name_norm"])
                      .set_index(["owner_base", "name_norm"])["max_voltage_kv"])

    lookup: dict[tuple[str, str], float] = dict(cec_by_name.items())

    if CEC_DICT_PATH.exists():
        dic = pd.read_csv(CEC_DICT_PATH)
        dic["util_lc"] = dic.Utility.astype(str).str.strip().str.lower()
        dic["source_norm"] = dic.SourceName.map(cec_norm)
        dic["cecname_norm"] = dic.CECName.map(cec_norm)
        for row in dic.itertuples(index=False):
            key_cec = (row.util_lc, row.cecname_norm)
            if key_cec in cec_by_name.index:
                lookup[(row.util_lc, row.source_norm)] = cec_by_name.loc[key_cec]
    else:
        print(f"  WARNING: {CEC_DICT_PATH.relative_to(ROOT)} not found; "
              "skipping dictionary-fallback CEC voltage matches.")

    return lookup


def attach_highside_voltage(attrs_all: pd.DataFrame) -> pd.DataFrame:
    """Add highside_kv, highside_kv_source, cec_max_voltage_kv.

    highside_kv is the utility-published value (substation_voltage first
    token, SCE/SDGE) where available, else CEC max_voltage_kv (all utilities,
    but the only source for PGE).  Index-aligned .map()/Series construction
    throughout -- row count and order are guaranteed unchanged; this is
    verified by an explicit assertion in main().
    """
    a = attrs_all.copy()

    first_tok = a["substation_voltage"].astype(str).str.extract(r"(\d+\.?\d*)")[0]
    kv_util = pd.to_numeric(first_tok, errors="coerce")
    kv_util = kv_util.where(a["substation_voltage"].notna())

    cec_lookup = _load_cec_voltage_lookup()
    keys = list(zip(a["utility"], a["substation_name"].map(cec_norm)))
    kv_cec = pd.Series([cec_lookup.get(k, np.nan) for k in keys], index=a.index)

    a["cec_max_voltage_kv"] = kv_cec
    a["highside_kv"] = kv_util.combine_first(kv_cec)
    a["highside_kv_source"] = np.select(
        [kv_util.notna(), kv_cec.notna()],
        ["utility", "cec"],
        default="none",
    )
    return a


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
        # VERIFIED: sanity check — raw values are ~1000x larger than expected MW range for
        # PGE (system peak ~28 GW); division by 1000 yields plausible MW magnitudes.
        # No PGE documentation explicitly states kW units.
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

    # ── Legacy recovery: substations removed from PGE's published ArcGIS layer ─
    # PGE periodically removes substations from pge_layer25_earliest_latest_part001.csv.
    # The older non-clean processed file (LEGACY_PGE_LOADS / LEGACY_PGE_ATTRS) preserves
    # their load profiles (already in MW) and attributes.  We union them back in here so
    # the clean output does not silently lose historic coverage.
    if LEGACY_PGE_LOADS.exists() and LEGACY_PGE_ATTRS.exists():
        leg_loads = pd.read_csv(LEGACY_PGE_LOADS)
        leg_loads = leg_loads[leg_loads["utility"] == "pge"].copy()
        leg_loads["substation_name"] = leg_loads["substation_name"].str.upper().str.strip()

        current_names = set(loads_out["substation_name"])
        leg_loads = leg_loads[~leg_loads["substation_name"].isin(current_names)]

        if not leg_loads.empty:
            n_leg = leg_loads["substation_name"].nunique()
            print(f"  PGE legacy recovery: adding {n_leg} substations "
                  f"no longer in raw ArcGIS data ({leg_loads['substation_name'].nunique()} subs, "
                  f"{len(leg_loads):,} rows)")
            leg_loads_out = pd.DataFrame({
                "utility":         "pge",
                "substation_name": leg_loads["substation_name"].values,
                "year":            pd.NA,
                "month":           leg_loads["month"].astype(int).values,
                "hour":            leg_loads["hour"].astype(int).values,
                "min_load":        pd.to_numeric(leg_loads["min_load"], errors="coerce").values,
                "max_load":        pd.to_numeric(leg_loads["max_load"], errors="coerce").values,
            })
            loads_out = pd.concat([loads_out, leg_loads_out], ignore_index=True)

            # Attributes for recovered substations
            leg_attrs = pd.read_csv(LEGACY_PGE_ATTRS)
            leg_attrs = leg_attrs[leg_attrs["utility"] == "pge"].copy()
            leg_attrs["substation_name"] = leg_attrs["substation_name"].str.upper().str.strip()
            leg_attrs = leg_attrs[leg_attrs["substation_name"].isin(set(leg_loads_out["substation_name"]))]
            leg_attrs = leg_attrs.drop_duplicates("substation_name")

            leg_attrs_out = pd.DataFrame({
                "utility":         "pge",
                "substation_name": leg_attrs["substation_name"].values,
                "util_lat":        pd.to_numeric(leg_attrs.get("latitude"),  errors="coerce").values,
                "util_lon":        pd.to_numeric(leg_attrs.get("longitude"), errors="coerce").values,
                "voltage_kv":      pd.to_numeric(leg_attrs.get("voltage_kv"), errors="coerce").values,
                "division":        leg_attrs.get("division", pd.Series(dtype=str)).values,
                "subst_id":        leg_attrs.get("subst_id", pd.Series(dtype=str)).values,
                "circuit_count":   pd.to_numeric(leg_attrs.get("circuit_count"), errors="coerce").values,
                "existing_gen":    pd.to_numeric(leg_attrs.get("existing_gen"), errors="coerce").values,
                "queued_gen":      pd.to_numeric(leg_attrs.get("queued_gen"),   errors="coerce").values,
                "total_gen":       pd.to_numeric(leg_attrs.get("total_gen"),    errors="coerce").values,
                "note_sub":        leg_attrs.get("note_sub", pd.Series(dtype=str)).values,
            })
            attrs_out = pd.concat([attrs_out, leg_attrs_out], ignore_index=True)

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

    # Deduplicate SCE to most-recent vintage per (substation, month, hour).
    # Each year-stamp is an independent p10/p90 snapshot from SCE's non-public
    # lookback window; including older rows for the same (sub, month, hour)
    # would double-count that cell's envelope.
    # We keep per-cell rather than a single global year because 2026 only covers
    # Jan-Apr — substations in 2026 fall back to 2025 for months May-Dec.
    n_before = len(loads_out)
    idx_keep = (loads_out
                .groupby(["substation_name", "month", "hour"])["year"]
                .idxmax())
    loads_out = loads_out.loc[idx_keep].copy()
    n_years_used = loads_out["year"].nunique()
    yrs_used = sorted(loads_out["year"].unique())
    print(f"  SCE: kept most-recent vintage per (substation, month, hour): "
          f"{len(loads_out):,} of {n_before:,} rows; "
          f"vintages used: {yrs_used}")

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
    # VERIFIED: sanity check — raw SDGE values are ~1000x larger than expected MW range
    # given SDGE system size (~5 GW peak); division by 1000 brings values in line with
    # EIA-930 CISO SDGE-territory demand.  No public SDGE documentation states kW units.
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

def apply_coordinate_overrides(attrs: pd.DataFrame) -> pd.DataFrame:
    """Fill `util_lat`/`util_lon` from the hand-curated override table.

    A handful of substations have no coordinate from any automatic source: the
    utility publishes none, no basin name matches, and no CEC record matches by
    name.  They still carry real load, so leaving them unplaced drops them from
    every downstream spatial step (nodal mapping, GenX rescaling, county
    assignment).  `data/substationCoordinateOverrides.csv` is where a
    hand-researched coordinate goes; it is the coordinate analogue of
    basinSourceDictionary.csv / cecSourceDictionary.csv.

    Schema: utility, substation_name, lat, lon, source, notes.  Rows with a
    blank lat/lon are placeholders for still-unresolved sites and are skipped,
    so the file doubles as the worklist of what remains.

    Overrides are LAST-RESORT ONLY: a row whose `util_lat` is already populated
    is never touched, so this can never silently contradict a utility-published
    coordinate.  `coord_source` records the provenance ('utility', the override
    file's own `source` value, or '' when still unplaced).

    Never adds or removes substations -- asserted by the caller.
    """
    attrs = attrs.copy()
    attrs["coord_source"] = np.where(attrs["util_lat"].notna(), "utility", "")
    if not COORD_OVERRIDE_PATH.exists():
        print("  no substationCoordinateOverrides.csv; skipping coordinate overrides")
        return attrs

    ov = pd.read_csv(COORD_OVERRIDE_PATH)
    ov = ov[ov["lat"].notna() & ov["lon"].notna()]
    if ov.empty:
        print(f"  {COORD_OVERRIDE_PATH.name}: no filled rows yet "
              f"(all placeholders) -- nothing to apply")
        return attrs

    # `norm` here is Series-based, so both sides are normalised in one call --
    # that keeps the override key identical to the basin-join key
    attr_key = list(zip(attrs.utility.astype(str).str.lower(),
                        norm(attrs.substation_name)))
    ov_key = list(zip(ov.utility.astype(str).str.lower(), norm(ov.substation_name)))
    lut = {k: (float(la), float(lo), str(src) if pd.notna(src) else "override")
           for k, la, lo, src in zip(ov_key, ov.lat, ov.lon,
                                     ov.get("source", pd.Series([None] * len(ov))))}

    c_lat, c_lon, c_src = (attrs.columns.get_loc(c)
                           for c in ("util_lat", "util_lon", "coord_source"))
    n_applied = n_skipped = 0
    for i, k in enumerate(attr_key):
        hit = lut.pop(k, None)
        if hit is None:
            continue
        if pd.notna(attrs.iat[i, c_lat]):
            n_skipped += 1          # already placed; an override must not win
            continue
        attrs.iat[i, c_lat], attrs.iat[i, c_lon], attrs.iat[i, c_src] = hit
        n_applied += 1
    n_unmatched = len(lut)
    print(f"  coordinate overrides: {n_applied} applied"
          + (f", {n_skipped} skipped (already had a utility coordinate)" if n_skipped else "")
          + (f", {n_unmatched} override row(s) matched no substation" if n_unmatched else ""))
    if n_unmatched:
        print(f"    unmatched: {sorted(f'{u}/{n}' for u, n in lut)}")
    return attrs


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

    # ── Combine ───────────────────────────────────────────────────
    attrs_all = pd.concat([pge_attrs, sce_attrs, sdge_attrs], ignore_index=True)
    loads_all = pd.concat([pge_loads, sce_loads, sdge_loads], ignore_index=True)

    # ── High-side voltage (highside_kv / highside_kv_source / cec_max_voltage_kv) ──
    # Guard: this enrichment must never change which substations exist -- PGE's
    # legacy-recovered rows (added above) have no other source. See
    # attach_highside_voltage()/_load_cec_voltage_lookup() docstrings.
    n_before = attrs_all.groupby("utility").size().sort_index()
    attrs_all = attach_highside_voltage(attrs_all)
    n_after = attrs_all.groupby("utility").size().sort_index()
    assert n_before.equals(n_after), (
        "substation counts changed after voltage enrichment! "
        f"before={n_before.to_dict()} after={n_after.to_dict()}"
    )
    print(f"\nVoltage enrichment guard passed: substation counts unchanged "
          f"{n_after.to_dict()}")

    # ── Hand-curated coordinate overrides (last resort; never overwrite a
    # utility-published coordinate). Same no-row-change guard as voltage.
    print("Applying coordinate overrides ...")
    attrs_all = apply_coordinate_overrides(attrs_all)
    assert n_after.equals(attrs_all.groupby("utility").size().sort_index()), (
        "substation counts changed after coordinate overrides!")
    n_placed = attrs_all["util_lat"].notna().sum()
    print(f"  {n_placed}/{len(attrs_all)} with a utility/override coordinate")
    for util, grp in attrs_all.groupby("utility"):
        src_counts = grp["highside_kv_source"].value_counts().to_dict()
        print(f"  {util}: highside_kv source counts {src_counts}")

    # ── Basin coordinates ───────────────────────────────────
    # AFTER the overrides on purpose: candidate selection and dist_to_basin_km
    # both key on util_lat/util_lon, and 11 substations are placed only by the
    # override table. Joining first left those rows unable to resolve an
    # ambiguous name and left 16 basin-matched rows with a NaN distance.
    print("Joining basin coordinates ...")
    if DICT_PATH.exists():
        dict_entry_count = len(pd.read_csv(DICT_PATH))
        print(f"  Dictionary: {DICT_PATH.relative_to(ROOT)} ({dict_entry_count} entries)")
        dict_maps = {
            "pge":  _build_dict_map("PGE"),
            "sce":  _build_dict_map("SCE"),
            "sdge": _build_dict_map("SDGE"),
        }
    else:
        print("  WARNING: basinSourceDictionary.csv not found; skipping dict augmentation.")
        dict_maps = {"pge": {}, "sce": {}, "sdge": {}}

    n_basin_before = attrs_all.groupby("utility").size().sort_index()
    all_attrs = []
    for utility in ("pge", "sce", "sdge"):
        df    = attrs_all[attrs_all["utility"] == utility]
        basin = _load_basin_lookup(utility)
        own   = basin[~basin["relaxed"]]
        # own-owner exact-name matches, for reporting only
        n_name_own = len(set(norm(df["substation_name"])) & set(own["name_norm"]))

        part    = add_basin_coords(df, basin, dict_maps[utility])
        n_m     = part["basin_lat"].notna().sum()
        n_c     = part["util_lat"].notna().sum()
        d_med   = part["dist_to_basin_km"].median()
        n_extra = n_m - n_name_own
        print(f"  {utility}: {n_c}/{len(part)} with util coords  |  "
              f"{n_m}/{len(part)} basin-matched "
              f"(own-owner name: {n_name_own}, relaxed+dict: {n_extra}, "
              f"median dist: {d_med:.1f} km)")
        all_attrs.append(part)

    attrs_all = pd.concat(all_attrs, ignore_index=True)
    # Per-row assignment, but the lookup now carries duplicate name_norm values
    # on purpose -- guard the row count the way voltage and overrides are guarded.
    assert n_basin_before.equals(attrs_all.groupby("utility").size().sort_index()), (
        "substation counts changed after the basin join!")
    n_any = (attrs_all["util_lat"].notna() | attrs_all["basin_lat"].notna()).sum()
    print(f"  {len(attrs_all) - n_any} substations unplaced by any source")

    # Convert wall-clock Pacific hours to fixed PST (UTC-8, no DST) using majority-month rule.
    # METHODOLOGICAL ASSUMPTION: min/max load profiles represent percentile envelopes over a
    # non-public lookback window and do not correspond to any single observed day, so we cannot
    # look up the DST status of individual timestamps.  Instead we assign PDT to all hours in
    # months where the majority of days fall in PDT (Mar-Oct = months 3-10 in California, per
    # US federal DST rules, 15 USC 260a: "second Sunday in March" to "first Sunday in November").
    # This introduces at most a 1-hour systematic error in the two transition months (Mar, Nov).
    # PST months (Jan, Feb, Nov, Dec): already PST.
    pdt_mask = loads_all["month"].isin(range(3, 11))
    loads_all["hour_pst"] = loads_all["hour"].where(~pdt_mask, (loads_all["hour"] - 1) % 24)
    out_cols = LOAD_COLS[:5] + ["hour_pst"] + LOAD_COLS[5:]  # insert hour_pst after hour

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
    loads_all[out_cols].to_csv(out_loads, index=False)

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