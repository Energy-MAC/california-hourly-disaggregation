"""
Process the California Electric Substations (2022) dataset from DataBasin / CEC.

Source
------
    data/raw/substation_misc/California Electric Substations (2022).zip
    Dataset: https://databasin.org/datasets/cb9ff78949db409f83d4d6ca38f707bf/
    Credit: California Energy Commission (CEC), 2022.
            HIFLD-sourced records are from Homeland Infrastructure Foundation-Level Data.

The ZIP contains an Esri Layer Package (.lpk), which is a 7-zip archive holding a
File Geodatabase (GDB) with one point layer: CA_Substations_Final.

Raw schema (4,442 rows)
-----------------------
    Name          — substation name
    Owner         — utility abbreviation (PG&E, SCE, SDG&E, SMUD, IID, PCORP, LADWP,
                    WAPA, SVP, Other, or NaN for HIFLD-only records)
    Type          — feature type: SUBSTATION, TAP, RISER, DEAD END, NOT AVAILABLE, NaN
    HIFLD_ID      — Homeland Infrastructure Foundation-Level Data record ID (may be NaN)
    Max_Voltage   — maximum voltage in kV (may be NaN)
    Source        — 'CEC' (CEC-curated) or 'HIFLD' (HIFLD-only, often lacking owner)
    Path          — WECC path or planning region label; mostly NaN
    COUNTY        — California county name
    CITY          — nearest city or 'Unincorporated'
    ZIP_CODE      — 5-digit ZIP code
    STATE         — always 'CA'
    Lat / Lon     — WGS84 decimal degrees (pre-computed attributes, not from geometry)
    geometry      — EPSG:3857 Web Mercator point (redundant with Lat/Lon)

Owner abbreviation map (used in output owner_std column)
---------------------------------------------------------
    PG&E   → pge          SDG&E  → sdge         IID    → iid
    SCE    → sce          SMUD   → smud          WAPA   → wapa
    PCORP  → pacificorp   LADWP  → ladwp         SVP    → svp
    Other  → other        NaN    → unknown

Output
------
    data/processed/substation_misc/ca_substations_2022.csv

    Columns: name, owner_raw, owner_std, type, hifld_id, max_voltage_kv,
             latitude, longitude, county, city, zip_code, source, path

Extraction note
---------------
    The ZIP is extracted to data/raw/substation_misc/_extract/ and the LPK
    (which is a 7-zip archive) to data/raw/substation_misc/_lpk_contents/ on
    first run.  These directories are kept as-is for subsequent runs.
    Requires: geopandas, pyogrio, py7zr
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

import pandas as pd

ROOT    = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "substation_misc"
OUT_DIR = ROOT / "data" / "processed" / "substation_misc"

_ZIP_NAME = "California Electric Substations (2022).zip"
_LPK_NAME = "California_Substations.lpk"
_GDB_NAME = "8668d35d-22e2-48d3-86e7-c2182cd622c8.gdb"
_LAYER    = "CA_Substations_Final"

_EXTRACT_DIR  = RAW_DIR / "_extract"
_LPK_CONTENTS = RAW_DIR / "_lpk_contents"

_OWNER_MAP = {
    "PG&E":  "pge",
    "SCE":   "sce",
    "SDG&E": "sdge",
    "SMUD":  "smud",
    "IID":   "iid",
    "PCORP": "pacificorp",
    "LADWP": "ladwp",
    "WAPA":  "wapa",
    "SVP":   "svp",
    "Other": "other",
}


def _ensure_extracted() -> Path:
    """Return the path to the extracted GDB, extracting if necessary."""
    # Find GDB in either v101 or v10 subfolder
    for subdir in ("v101", "v10"):
        gdb = _LPK_CONTENTS / subdir / _GDB_NAME
        if gdb.exists():
            return gdb

    zip_path = RAW_DIR / _ZIP_NAME
    if not zip_path.exists():
        raise FileNotFoundError(
            f"Source ZIP not found: {zip_path}\n"
            "Download from https://databasin.org/datasets/cb9ff78949db409f83d4d6ca38f707bf/ "
            "and place in data/raw/substation_misc/"
        )

    try:
        import py7zr
    except ImportError as exc:
        raise ImportError("pip install py7zr  (needed to extract the .lpk layer package)") from exc

    # Step 1: unzip outer archive
    print(f"Extracting {_ZIP_NAME} ...")
    _EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(_EXTRACT_DIR)

    # Step 2: extract LPK (7-zip) to _lpk_contents/
    lpk_path = _EXTRACT_DIR / _LPK_NAME
    if not lpk_path.exists():
        raise FileNotFoundError(f"Expected {lpk_path} after unzipping {_ZIP_NAME}")

    print(f"Extracting {_LPK_NAME} (7-zip layer package) ...")
    _LPK_CONTENTS.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(lpk_path, mode="r") as z:
        z.extractall(path=_LPK_CONTENTS)

    # Return the GDB path
    for subdir in ("v101", "v10"):
        gdb = _LPK_CONTENTS / subdir / _GDB_NAME
        if gdb.exists():
            return gdb

    raise FileNotFoundError(f"GDB not found after extraction in {_LPK_CONTENTS}")


def process(out_dir: Path = OUT_DIR) -> Path:
    """
    Read the CA_Substations_Final GDB layer, clean columns, and write a CSV.

    Returns the path to the output CSV.
    """
    try:
        import geopandas as gpd
    except ImportError as exc:
        raise ImportError("pip install geopandas pyogrio") from exc

    gdb = _ensure_extracted()
    print(f"Reading {_LAYER} from {gdb} ...")
    gdf = gpd.read_file(gdb, layer=_LAYER, engine="pyogrio")
    print(f"  {len(gdf):,} rows, CRS={gdf.crs}")

    # Build output DataFrame from existing attribute columns (Lat/Lon already WGS84)
    out = pd.DataFrame({
        "name":           gdf["Name"],
        "owner_raw":      gdf["Owner"],
        "owner_std":      gdf["Owner"].map(_OWNER_MAP).fillna("unknown"),
        "type":           gdf["Type"].str.upper().str.strip(),
        "hifld_id":       gdf["HIFLD_ID"],
        "max_voltage_kv": pd.to_numeric(gdf["Max_Voltage"], errors="coerce"),
        "latitude":       pd.to_numeric(gdf["Lat"],         errors="coerce"),
        "longitude":      pd.to_numeric(gdf["Lon"],         errors="coerce"),
        "county":         gdf["COUNTY"],
        "city":           gdf["CITY"],
        "zip_code":       gdf["ZIP_CODE"],
        "source":         gdf["Source"],
        "path":           gdf["Path"],
    })

    # Drop rows with no coordinates (shouldn't happen but guard)
    before = len(out)
    out = out.dropna(subset=["latitude", "longitude"]).reset_index(drop=True)
    if before != len(out):
        print(f"  Dropped {before - len(out)} rows with missing coordinates.")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "ca_substations_2022.csv"
    out.to_csv(out_path, index=False)
    mb = out_path.stat().st_size / 1024 / 1024

    print(f"\nWrote {len(out):,} rows -> {out_path}  ({mb:.2f} MB)")
    print()
    print("Owner breakdown:")
    print(
        out.groupby(["owner_std", "owner_raw"], dropna=False)
        .size().rename("n")
        .reset_index()
        .sort_values("n", ascending=False)
        .to_string(index=False)
    )
    print()
    print("Type breakdown (top 6):")
    print(out["type"].value_counts(dropna=False).head(6).to_string())
    print()
    print(f"Voltage range: {out['max_voltage_kv'].min():.0f} - {out['max_voltage_kv'].max():.0f} kV  "
          f"({out['max_voltage_kv'].isna().sum()} NaN)")
    return out_path


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    process()
