"""
Combine the two SCE load-profile raw data sources into a single file.

Sources
-------
scrape (MW-converted)
    data/raw/sce/sce_layer2_mw_part001.csv
    Produced by: python scripts/scrape_sce.py convert-to-mw
    (which reads the raw sce_layer2_*_part*.csv Amp files and converts to MW
    using per-substation voltage from sce_substation_attributes.csv)

    Columns: YEAR, MONTH, HOUR, SUBSTATION,
             MIN_LOAD_A, MAX_LOAD_A,   (original Amp values)
             MIN_LOAD, MAX_LOAD,       (converted MW values)
             MONTHLABEL, OBJECTID, longitude, latitude

bulk
    data/raw/sce/sce_bulk_download_all.csv
    Produced by: python scripts/ingest_sce_bulk_download.py <path/to/download.zip>
    Columns: YEAR, MONTH, HOUR, SUBSTATION, MIN_LOAD, MAX_LOAD, MONTHLABEL
    MIN_LOAD and MAX_LOAD are in MW (as published by DRPEP).

Both sources use MW for MIN_LOAD / MAX_LOAD after the convert-to-mw step.
Scrape rows additionally carry MIN_LOAD_A / MAX_LOAD_A (Amps); bulk rows
have NaN for those columns.

Output
------
    data/raw/sce/sce_combined_raw.csv

    All original columns from both sources are kept.  Columns present in one
    source but not the other are filled with NaN.  Duplicates are NOT removed.

    Final column order:
        source, YEAR, MONTH, HOUR, SUBSTATION,
        MIN_LOAD_A, MAX_LOAD_A,   (Amps — scrape rows only; NaN for bulk)
        MIN_LOAD, MAX_LOAD,       (MW   — both sources)
        MONTHLABEL, OBJECTID, longitude, latitude
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT    = Path(__file__).resolve().parents[3]
RAW_SCE = ROOT / "data" / "raw" / "sce"
OUT     = RAW_SCE / "sce_combined_raw.csv"

SCRAPE_FILE  = RAW_SCE / "sce_layer2_mw_part001.csv"
BULK_FILE    = RAW_SCE / "sce_bulk_download_all.csv"

OUT_COLS = [
    "source",
    "YEAR", "MONTH", "HOUR", "SUBSTATION",
    "MIN_LOAD_A", "MAX_LOAD_A",
    "MIN_LOAD", "MAX_LOAD",
    "MONTHLABEL", "OBJECTID", "longitude", "latitude",
]


def load_scrape() -> pd.DataFrame | None:
    if not SCRAPE_FILE.exists():
        print(f"  ({SCRAPE_FILE.name} not found — scrape source skipped)")
        print("  Run: python scripts/scrape_sce.py convert-to-mw")
        return None
    print(f"  scrape file: {SCRAPE_FILE.name}")
    df = pd.read_csv(SCRAPE_FILE, low_memory=False)
    df.insert(0, "source", "scrape")
    print(f"  scrape: {len(df):,} rows  "
          f"({df['SUBSTATION'].nunique()} substations, "
          f"YEAR {int(df['YEAR'].min())}-{int(df['YEAR'].max())})")
    n_converted = df["MIN_LOAD"].notna().sum()
    print(f"  {n_converted:,}/{len(df):,} rows have MW values "
          f"({df['MIN_LOAD'].isna().sum():,} rows missing voltage — left empty)")
    return df


def load_bulk() -> pd.DataFrame | None:
    if not BULK_FILE.exists():
        print(f"  ({BULK_FILE.name} not found — bulk source skipped)")
        return None
    df = pd.read_csv(BULK_FILE, low_memory=False)
    df.insert(0, "source", "bulk")
    print(f"  bulk  : {len(df):,} rows  "
          f"({df['SUBSTATION'].nunique()} substations, "
          f"YEAR {int(df['YEAR'].min())}-{int(df['YEAR'].max())})")
    print("  NOTE: bulk MIN_LOAD/MAX_LOAD are in MW")
    return df


def combine() -> Path:
    print("Loading SCE scrape data ...")
    scrape = load_scrape()
    print("Loading SCE bulk download data ...")
    bulk   = load_bulk()

    frames = [f for f in (scrape, bulk) if f is not None]
    if not frames:
        raise FileNotFoundError(
            "Neither scrape files nor bulk download found in "
            f"{RAW_SCE}.\n"
            "Run at least one of:\n"
            "  python scripts/scrape_sce.py layer --layer-id 2\n"
            "  python scripts/ingest_sce_bulk_download.py <path>"
        )

    combined = pd.concat(frames, ignore_index=True)

    # Ensure all output columns exist (fill missing with NaN)
    for col in OUT_COLS:
        if col not in combined.columns:
            combined[col] = pd.NA

    combined = combined[OUT_COLS]

    RAW_SCE.mkdir(parents=True, exist_ok=True)
    combined.to_csv(OUT, index=False)
    mb = OUT.stat().st_size / 1024 / 1024

    print(f"\nWrote {len(combined):,} rows -> {OUT}  ({mb:.1f} MB)")
    print()
    print("Row counts by source:")
    print(combined["source"].value_counts().to_string())
    print()
    print("Substations by source:")
    print(
        combined.groupby("source")["SUBSTATION"]
        .nunique()
        .rename("unique_substations")
        .to_string()
    )
    print()
    print("Substations in both sources:",
          combined.groupby("source")["SUBSTATION"]
          .apply(set)
          .pipe(lambda s: len(s.iloc[0] & s.iloc[1]) if len(s) == 2 else "N/A"))

    return OUT


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    combine()
