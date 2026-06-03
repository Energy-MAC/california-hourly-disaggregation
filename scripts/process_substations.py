"""
Normalizes raw substation data from all utilities into two tidy CSVs.

Output files
------------
  data/processed/substations/substation_locations.csv
      utility, substation_name, latitude, longitude

  data/processed/substations/substation_load_profiles.csv
      utility, substation_name, latitude, longitude, year, month, hour, min_load, max_load

Source schema notes
-------------------
  PGE (pge_layer25_*.csv)
      subname, monthhour='MM_HH', high=max_load, low=min_load
      months 1-12, hours 0-23; no year column

  SCE (data/raw/sce/sce_bulk_download_all.csv)
      YEAR, MONTH=0-indexed, HOUR, SUBSTATION, MIN_LOAD, MAX_LOAD  — values in MW from DRPEP
      Consolidated from 709-substation bulk download (individual CSVs in bulk_download/).
      Coordinates joined from sce_layer2_*.csv (coordinate reference only).
      Falls back to data/raw/sce/bulk_download/*.csv if the consolidated file is absent.

  SCE layer2 (sce_layer2_*.csv) — COORDINATE REFERENCE ONLY, NOT LOADED AS LOAD DATA
      Load values are in Amps (not MW); excluded from the processed output.
      Used only by _sce_coord_lookup() to attach lat/lon to individual substation files.

  SDGE (sdge_substation_profiles_part*.csv)
      AssetName, Month=1-indexed, LoadDay='High Load'|'Low Load', hour 1..hour 24
      wide format — melted to long; hours converted to 0-indexed; no year column

  PacifiCorp (pacificorp_layer1_*.csv)
      Name, longitude, latitude; no load data
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "processed" / "substations"

LOC_COLS = ["utility", "substation_name", "latitude", "longitude"]
ATTR_COLS = [
    # Physical / administrative
    "voltage_kv",       # SCE: real system kV (mapped from nominal); SDGE: IMAP_VOLTAGE; PGE: Voltage_kV
    "substation_type",  # SDGE: voltage transformation ratio (e.g. "69/12 kV")
    "sys_name",         # SCE: transmission system name (e.g. "Rio Hondo 220/66 System")
    "division",         # PGE: service division (e.g. "Kern")
    "subst_id",         # SCE: subst_id; PGE: SubstationID
    # DER generation / load — all in MW in this file
    # (PGE raw is in kW; divided by 1000 during enrichment below)
    "existing_gen",     # SCE, SDGE, PGE (MW)
    "queued_gen",       # SCE, SDGE, PGE (MW)
    "total_gen",        # SCE, SDGE, PGE (MW)
    "projected_load",   # SCE, SDGE (MW)
    "der_penetration",  # SCE, SDGE (%)
    "max_remain_cap",   # SCE: remaining ICA capacity (MW)
    # Circuit / bank count
    "circuit_count",    # SCE: circuit count; PGE: NUMBANKS (transformer banks)
    # Customer mix percentages (SCE only, derived from circuit totals)
    "res_pct", "com_pct", "agr_pct", "ind_pct", "other_pct",
    # Customer totals (SCE only, summed across circuits)
    "res_total", "com_total", "agr_total", "ind_total", "other_total",
    # Notes / flags
    "note_sub",         # SCE: interconnection notes; PGE: REDACTED data flag
    # PacifiCorp DG Readiness — distinct from existing_gen in other utilities
    "existing_der",             # PacifiCorp: sum of Existing_DER across circuits (MW)
    "net_min_daytime_load_mw",  # PacifiCorp: sum of Net Minimum Daytime Load across circuits (MW)
]
PROFILE_COLS = [
    "utility", "substation_name", "latitude", "longitude",
    "year", "month", "hour", "min_load", "max_load",
]


# ── PGE ──────────────────────────────────────────────────────────────────────

def load_pge() -> pd.DataFrame:
    frames = []
    for f in sorted(ROOT.glob("data/raw/pge/pge_layer25_*.csv")):
        df = pd.read_csv(f, dtype=str)
        split = df["monthhour"].str.split("_", expand=True)
        df["month"] = split[0].astype(int)
        df["hour"] = split[1].astype(int)
        df = df.rename(columns={"subname": "substation_name", "high": "max_load", "low": "min_load"})
        df["utility"] = "pge"
        df["year"] = ""
        frames.append(df[PROFILE_COLS])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=PROFILE_COLS)


# ── SCE ──────────────────────────────────────────────────────────────────────

def _sce_coord_lookup() -> dict[str, tuple[str, str]]:
    """Return {substation_name: (latitude, longitude)} from layer2 files."""
    lookup: dict[str, tuple[str, str]] = {}
    for f in sorted(ROOT.glob("data/raw/sce/sce_layer2_*.csv")):
        df = pd.read_csv(f, dtype=str, usecols=["SUBSTATION", "latitude", "longitude"])
        for _, row in df.drop_duplicates("SUBSTATION").iterrows():
            if row["SUBSTATION"] not in lookup and row["latitude"] and row["longitude"]:
                lookup[row["SUBSTATION"]] = (row["latitude"], row["longitude"])
    return lookup


def _normalize_sce_df(df: pd.DataFrame, lat: str = "", lon: str = "") -> pd.DataFrame:
    """Shared normalization for SCE rows (month 0->1 indexed, rename columns)."""
    df = df.copy()
    df["month"] = df["MONTH"].astype(int) + 1  # 0-indexed -> 1-indexed
    df["hour"] = df["HOUR"].astype(int)
    df = df.rename(columns={
        "SUBSTATION": "substation_name",
        "MIN_LOAD": "min_load",
        "MAX_LOAD": "max_load",
        "YEAR": "year",
    })
    df["utility"] = "sce"
    if lat:
        df["latitude"] = lat
        df["longitude"] = lon
    elif "latitude" not in df.columns:
        df["latitude"] = ""
        df["longitude"] = ""
    return df[PROFILE_COLS]


def load_sce() -> pd.DataFrame:
    # sce_layer2_*.csv is Amps (not MW) — used only for coordinates via _sce_coord_lookup().
    coord_lookup = _sce_coord_lookup()

    bulk_all = ROOT / "data/raw/sce/sce_bulk_download_all.csv"
    if bulk_all.exists():
        # Primary path: consolidated DRPEP bulk download (MW, all substations in one file).
        df = pd.read_csv(bulk_all, dtype=str)
        df["latitude"]  = df["SUBSTATION"].map({k: v[0] for k, v in coord_lookup.items()}).fillna("")
        df["longitude"] = df["SUBSTATION"].map({k: v[1] for k, v in coord_lookup.items()}).fillna("")
        return _normalize_sce_df(df)

    # Fallback: individual CSVs extracted into bulk_download/ subdirectory.
    frames = []
    for f in sorted((ROOT / "data/raw/sce/bulk_download").glob("*.csv")):
        df = pd.read_csv(f, dtype=str)
        sub_name = df["SUBSTATION"].iloc[0] if not df.empty else f.stem
        lat, lon = coord_lookup.get(sub_name, ("", ""))
        frames.append(_normalize_sce_df(df, lat=lat, lon=lon))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=PROFILE_COLS)


# ── SDGE ─────────────────────────────────────────────────────────────────────

def load_sdge() -> pd.DataFrame:
    hour_cols = [f"hour {i}" for i in range(1, 25)]
    meta_cols = ["AssetName", "Month", "LoadDay", "latitude", "longitude"]
    frames = []

    for f in sorted(ROOT.glob("data/raw/sdge/sdge_substation_profiles_part*.csv")):
        df = pd.read_csv(f, dtype=str)

        melted = df.melt(id_vars=meta_cols, value_vars=hour_cols,
                         var_name="hour_col", value_name="load")
        melted["hour"] = melted["hour_col"].str.extract(r"(\d+)").astype(int) - 1
        melted["load_type"] = (
            melted["LoadDay"].str.strip().str.lower()
            .map({"high load": "max_load", "low load": "min_load"})
        )
        melted = melted.dropna(subset=["load_type"])

        pivoted = melted.pivot_table(
            index=["AssetName", "latitude", "longitude", "Month", "hour"],
            columns="load_type",
            values="load",
            aggfunc="first",
        ).reset_index()
        pivoted.columns.name = None

        pivoted = pivoted.rename(columns={"AssetName": "substation_name", "Month": "month"})
        pivoted["utility"] = "sdge"
        pivoted["year"] = ""
        pivoted["month"] = pivoted["month"].astype(int)

        for col in ("min_load", "max_load"):
            if col not in pivoted.columns:
                pivoted[col] = ""

        frames.append(pivoted[PROFILE_COLS])

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=PROFILE_COLS)


# ── PacifiCorp ────────────────────────────────────────────────────────────────

def load_pacificorp() -> pd.DataFrame:
    frames = []
    for f in sorted(ROOT.glob("data/raw/pacificorp/pacificorp_layer1_*.csv")):
        df = pd.read_csv(f, dtype=str)
        df = df.rename(columns={"Name": "substation_name"})
        df["utility"] = "pacificorp"
        frames.append(df[LOC_COLS])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=LOC_COLS)


# ── Attribute enrichment ─────────────────────────────────────────────────────

def _enrich_locations(locs: pd.DataFrame) -> pd.DataFrame:
    """
    Join available physical attributes onto the locations DataFrame.

    Sources (each is optional — silently skipped if file absent):
      SDGE: data/raw/sdge/sdge_substation_attributes.csv
            Fields: voltage_kv, substation_type, existing_gen, queued_gen,
                    total_gen, projected_load, der_penetration
            Scrape with: python scripts/scrape_sdge.py attributes

      SCE:  data/raw/sce/sce_substation_attributes.csv
            Full attribute set from ICA Tables Table 3 (voltage, gen, load, customer mix).
            Scrape with: python scripts/scrape_sce.py attributes

      PGE:  data/raw/pge/pge_substation_attributes.csv
            Fields from EDSubstations (layer 0): voltage, division, num_banks,
            existing/queued/total DG (kW in raw, converted to MW here).
            Scrape with: python scripts/scrape_pge.py attributes

      PacifiCorp: data/raw/pacificorp/pacificorp_substation_attributes.csv
            Fields from DG Readiness FeatureServer (circuit-level, aggregated):
            existing_der, net_min_daytime_load_mw, circuit_count.
            Scrape with: python scripts/scrape_pacificorp.py attributes
    """
    locs = locs.copy()
    for col in ATTR_COLS:
        locs[col] = ""

    # SDGE — FeatureServer names are uppercase; load-profile names are title-case.
    # Join case-insensitively by normalising both sides to uppercase.
    sdge_attrs_path = ROOT / "data/raw/sdge/sdge_substation_attributes.csv"
    if sdge_attrs_path.exists():
        attrs = pd.read_csv(sdge_attrs_path, dtype=str).drop_duplicates("substation_name")
        attrs_idx = attrs.set_index(attrs["substation_name"].str.upper())
        sdge_mask = locs["utility"] == "sdge"
        lookup_keys = locs.loc[sdge_mask, "substation_name"].str.upper()
        for col in ATTR_COLS:
            if col in attrs_idx.columns:
                locs.loc[sdge_mask, col] = lookup_keys.map(attrs_idx[col]).fillna("")

    # SCE — Table 3 sub_name uses mixed case; bulk download SUBSTATION uses different
    # casing for "P.T." abbreviations. Join case-insensitively via uppercase normalisation.
    # Nominal-to-real voltage map: SCE reports nominal kV; actual system voltage differs.
    _SCE_VOLTAGE_MAP = {
        115: 115.47, 66: 68.7, 55: 55.05, 33: 34.5, 25: 25,
        16: 16.98, 12: 12.55, 7: 6.88, 4.8: 5, 4.16: 4.4, 2.4: 2.5,
    }
    sce_attrs_path = ROOT / "data/raw/sce/sce_substation_attributes.csv"
    if sce_attrs_path.exists():
        sce_attrs = pd.read_csv(sce_attrs_path, dtype=str).drop_duplicates("substation_name")
        sce_attrs_idx = sce_attrs.set_index(sce_attrs["substation_name"].str.upper())
        sce_mask = locs["utility"] == "sce"
        lookup_keys = locs.loc[sce_mask, "substation_name"].str.upper()
        for col in ATTR_COLS:
            if col in sce_attrs_idx.columns:
                locs.loc[sce_mask, col] = lookup_keys.map(sce_attrs_idx[col]).fillna("")
        # Convert nominal voltage to real voltage using SCE's system voltage map.
        nominal = pd.to_numeric(locs.loc[sce_mask, "voltage_kv"], errors="coerce")
        real_kv = nominal.map(_SCE_VOLTAGE_MAP)
        locs.loc[sce_mask, "voltage_kv"] = (
            real_kv.where(real_kv.notna(), "").astype(str).replace("nan", "")
        )

    # PGE — SubstationName is uppercase in both layer 0 and layer 25, so names
    # match exactly. DG fields are in kW in the raw file; divide by 1000 for MW.
    pge_attrs_path = ROOT / "data/raw/pge/pge_substation_attributes.csv"
    if pge_attrs_path.exists():
        pge_attrs = (
            pd.read_csv(pge_attrs_path, dtype=str)
            .drop_duplicates("substation_name")
            .set_index("substation_name")
        )
        pge_mask = locs["utility"] == "pge"

        # Direct string mappings
        _pge_direct = {
            "voltage_kv":   "voltage_kv",
            "division":     "division",
            "subst_id":     "substation_id",
            "circuit_count":"num_banks",
            "note_sub":     "redacted",
        }
        for out_col, raw_col in _pge_direct.items():
            if raw_col in pge_attrs.columns:
                locs.loc[pge_mask, out_col] = (
                    locs.loc[pge_mask, "substation_name"].map(pge_attrs[raw_col]).fillna("")
                )

        # kW → MW conversion
        _pge_kw_cols = {
            "existing_gen": "existing_dg_kw",
            "queued_gen":   "queued_dg_kw",
            "total_gen":    "total_dg_kw",
        }
        for out_col, raw_col in _pge_kw_cols.items():
            if raw_col in pge_attrs.columns:
                mw_vals = (
                    pd.to_numeric(
                        locs.loc[pge_mask, "substation_name"].map(pge_attrs[raw_col]),
                        errors="coerce",
                    ) / 1000
                ).round(6)
                locs.loc[pge_mask, out_col] = mw_vals.where(mw_vals.notna(), "").astype(str).replace("nan", "")

    # PacifiCorp — DG Readiness names use title case with possible trailing spaces;
    # layer 1 (locations source) uses all-caps.  Join via strip().upper() on both sides.
    pac_attrs_path = ROOT / "data/raw/pacificorp/pacificorp_substation_attributes.csv"
    if pac_attrs_path.exists():
        pac_attrs = pd.read_csv(pac_attrs_path, dtype=str).drop_duplicates("substation_name")
        # Build index keyed on strip().upper() of the raw name
        pac_attrs_idx = pac_attrs.set_index(pac_attrs["substation_name"].str.strip().str.upper())
        pac_mask = locs["utility"] == "pacificorp"
        lookup_keys = locs.loc[pac_mask, "substation_name"].str.strip().str.upper()
        for col in ("existing_der", "net_min_daytime_load_mw", "circuit_count"):
            if col in pac_attrs_idx.columns:
                locs.loc[pac_mask, col] = lookup_keys.map(pac_attrs_idx[col]).fillna("")

    # Normalise attr columns: numeric coercion from .loc assignment can turn
    # our initial "" sentinels into NaN — convert everything back to plain strings.
    for col in ATTR_COLS:
        locs[col] = locs[col].fillna("").astype(str).replace("nan", "")

    return locs


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading PGE ...")
    pge = load_pge()
    print(f"  {len(pge):>8,} rows")

    print("Loading SCE ...")
    sce = load_sce()
    print(f"  {len(sce):>8,} rows")

    print("Loading SDGE ...")
    sdge = load_sdge()
    print(f"  {len(sdge):>8,} rows")

    print("Loading PacifiCorp ...")
    pac = load_pacificorp()
    print(f"  {len(pac):>8,} rows")

    # ── Load profiles ─────────────────────────────────────────────────────────
    profiles = pd.concat([pge, sce, sdge], ignore_index=True)
    out_profiles = OUT_DIR / "substation_load_profiles.csv"
    profiles[PROFILE_COLS].to_csv(out_profiles, index=False)

    mb = out_profiles.stat().st_size / 1024 / 1024
    print(f"\nLoad profiles : {len(profiles):,} rows -> {out_profiles.relative_to(ROOT)}  ({mb:.1f} MB)")

    # ── Locations (deduplicated, then enriched) ────────────────────────────────
    profile_locs = profiles[LOC_COLS].drop_duplicates(["utility", "substation_name"])
    locations = pd.concat([profile_locs, pac[LOC_COLS]], ignore_index=True)
    locations = locations.drop_duplicates(["utility", "substation_name"])
    locations = _enrich_locations(locations)

    out_locs = OUT_DIR / "substation_locations.csv"
    locations[LOC_COLS + ATTR_COLS].to_csv(out_locs, index=False)

    print(f"Locations     : {len(locations):,} rows -> {out_locs.relative_to(ROOT)}")

    # ── Summary by utility ────────────────────────────────────────────────────
    print()
    print("Substations per utility:")
    for util, grp in locations.groupby("utility"):
        n_coords = grp[["latitude", "longitude"]].replace("", pd.NA).dropna().shape[0]
        n_voltage = (grp["voltage_kv"].replace("", pd.NA).dropna().shape[0])
        print(f"  {util:<12}  {len(grp):>5} substations  "
              f"({n_coords} with coordinates, {n_voltage} with voltage)")

    print()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
