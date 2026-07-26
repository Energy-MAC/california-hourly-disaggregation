"""
Process the CEC Substation DataPull (07/24/2026) — an updated, richer successor
to the DataBasin CA Substations 2022 reference (see process_substations_misc.py).

Source
------
    data/raw/CEC_Substation_DataPull_07242026.gdb/CEC_Substation_DataPull_07242026.gdb
    Layer: Substations_DataPull_07242026 (4,828 rows, Point Z, native CRS EPSG:3310;
    Lat/Lon attribute columns are pre-computed WGS84 degrees, used directly).
    Obtained directly from the CEC via a data request (2026-07-24) — not scraped.

This mirrors process_substations_misc.py's output schema exactly so the two
reference datasets ("basin" = 2022 DataBasin, "cec" = this 2026 CEC pull) are
directly comparable in downstream scripts (compare_cats_basin.py's CEC
counterpart, process_substations_clean.py's basin-matching stage, etc.).
CEC-only columns (status, CPUC cross-references, RESOLVE area, etc.) are
appended after the mirrored columns rather than discarded.

Owner mapping (owner_std): CEC's Owner field has many more variants than
basin's (e.g. "PG&E NGBA", "SCE Metro", "SCE Northern" all roll up to
pge/sce). "Other (X - Assumed)" rows are CEC's own best-guess attribution,
not confirmed ownership — kept as a distinct `{x}_assumed` value rather than
folded into the confirmed utility bucket, so downstream matching can choose
whether to include them.

Output
------
    data/processed/substation_misc/ca_substations_cec.csv

    Mirrored columns (same names/order as ca_substations_2022.csv): name,
    owner_raw, owner_std, type, hifld_id, max_voltage_kv, latitude,
    longitude, county, city, zip_code, source, path
    Additional CEC-only columns: cec_id, status, proposed, urban_rural,
    imagery_verified, cpuc_substation_name, cpuc_caiso_study_area,
    cpuc_caiso_local_area, cpuc_owner, cpuc_voltages, cec_resolve_area
"""
from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
GDB = ROOT / ("data/raw/CEC_Substation_DataPull_07242026.gdb/"
             "CEC_Substation_DataPull_07242026.gdb")
LAYER = "Substations_DataPull_07242026"
OUT_DIR = ROOT / "data/processed/substation_misc"


def _map_owner(raw: str) -> str:
    if pd.isna(raw):
        return "unknown"
    s = str(raw)
    if "Assumed" in s:
        if "PGE" in s:
            return "pge_assumed"
        if "SCE" in s:
            return "sce_assumed"
        if "NVE" in s:
            return "nve_assumed"
        return "other_assumed"
    if "PG&E" in s or s.strip() == "PGE":
        return "pge"
    if "SCE" in s:
        return "sce"
    if "SDG&E" in s or "SDGE" in s:
        return "sdge"
    if "SMUD" in s:
        return "smud"
    if "IID" in s:
        return "iid"
    if "PCORP" in s:
        return "pacificorp"
    if "LADWP" in s:
        return "ladwp"
    if "WAPA" in s:
        return "wapa"
    if "SVP" in s:
        return "svp"
    if "VEA" in s:
        return "vea"
    if "NVE" in s:
        return "nve"
    if "PSREC" in s:
        return "psrec"
    if "Liberty" in s:
        return "liberty"
    if s.strip() == "BV":
        return "bv"
    if "PACE" in s:
        return "pace"
    if "BPA" in s:
        return "bpa"
    return "other"


def process(out_dir: Path = OUT_DIR) -> Path:
    print(f"Reading {LAYER} from {GDB} ...")
    gdf = gpd.read_file(GDB, layer=LAYER, engine="pyogrio")
    print(f"  {len(gdf):,} rows, CRS={gdf.crs}")

    out = pd.DataFrame({
        "name":           gdf["CEC_Name_Unique"],
        "owner_raw":      gdf["CEC_Owner"],
        "owner_std":      gdf["CEC_Owner"].map(_map_owner),
        "type":           gdf["CEC_Type"].str.upper().str.strip(),
        "hifld_id":       pd.to_numeric(gdf["HIFLD_ID"], errors="coerce"),
        "max_voltage_kv": pd.to_numeric(gdf["CEC_MAX_Voltage"], errors="coerce"),
        "latitude":       pd.to_numeric(gdf["Lat"], errors="coerce"),
        "longitude":      pd.to_numeric(gdf["Lon"], errors="coerce"),
        "county":         gdf["COUNTY"],
        "city":           gdf["CITY"],
        "zip_code":       gdf["ZIP_CODE"],
        "source":         gdf["Source"],
        "path":           gdf["Transmission_Path"],
        # CEC-only extras
        "cec_id":               gdf["CEC_ID"],
        "status":               gdf["Status"],
        "proposed":             gdf["Proposed"],
        "urban_rural":          gdf["Urban_Rural"],
        "imagery_verified":     gdf["ImageryVarified"],
        "cpuc_substation_name": gdf["CPUC_Substation_Name"],
        "cpuc_caiso_study_area": gdf["CPUC_CAISO_Study_Area"],
        "cpuc_caiso_local_area": gdf["CPUC_CAISO_Local_Area"],
        "cpuc_owner":           gdf["CPUC_Owner"],
        "cpuc_voltages":        gdf["CPUC_Voltages"],
        "cec_resolve_area":     gdf["CEC_RESOLVE_AREA"],
    })

    before = len(out)
    out = out.dropna(subset=["latitude", "longitude"]).reset_index(drop=True)
    if before != len(out):
        print(f"  Dropped {before - len(out)} rows with missing coordinates.")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "ca_substations_cec.csv"
    out.to_csv(out_path, index=False)
    mb = out_path.stat().st_size / 1024 / 1024

    print(f"\nWrote {len(out):,} rows -> {out_path}  ({mb:.2f} MB)")
    print("\nOwner breakdown:")
    print(out.groupby(["owner_std", "owner_raw"], dropna=False).size()
          .rename("n").reset_index().sort_values("n", ascending=False).to_string(index=False))
    print("\nType breakdown:")
    print(out["type"].value_counts(dropna=False).to_string())
    print(f"\nVoltage range: {out['max_voltage_kv'].min():.0f} - "
          f"{out['max_voltage_kv'].max():.0f} kV ({out['max_voltage_kv'].isna().sum()} NaN)")
    return out_path


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    process()
