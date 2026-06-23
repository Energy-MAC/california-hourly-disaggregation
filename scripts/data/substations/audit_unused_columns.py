"""
Reports which columns in each raw source file are not carried through
to the processed output in data/processed/substations/.
"""
from __future__ import annotations

import csv
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]

# Columns actually read and used by process_substations.py for each source.
# Any column in the raw file that is NOT in this set is dropped.
USED: dict[str, set[str]] = {
    "PGE layer25": {
        "subname",        # -> substation_name
        "monthhour",      # -> month, hour
        "high",           # -> max_load
        "low",            # -> min_load
        "longitude",
        "latitude",
    },
    "SCE layer2": {
        "YEAR",           # -> year
        "MONTH",          # -> month  (0-indexed, +1 applied)
        "HOUR",           # -> hour
        "SUBSTATION",     # -> substation_name
        "MIN_LOAD",       # -> min_load
        "MAX_LOAD",       # -> max_load
        "longitude",
        "latitude",
    },
    "SCE individual": {
        "YEAR",           # -> year
        "MONTH",          # -> month  (0-indexed, +1 applied)
        "HOUR",           # -> hour
        "SUBSTATION",     # -> substation_name
        "MIN_LOAD",       # -> min_load
        "MAX_LOAD",       # -> max_load
        # longitude/latitude not present in individual files; joined from layer2
    },
    "SDGE profiles": {
        "AssetName",      # -> substation_name
        "Month",          # -> month  (1-indexed, kept)
        "LoadDay",        # -> determines whether row contributes to max_load or min_load
        *[f"hour {i}" for i in range(1, 25)],
        "longitude",
        "latitude",
    },
    "PacifiCorp layer1": {
        "Name",           # -> substation_name
        "longitude",
        "latitude",
    },
}

# One representative file per source
SAMPLE_FILES: dict[str, str] = {
    "PGE layer25":      "data/raw/pge/pge_layer25_earliest_latest_part001.csv",
    "SCE layer2":       "data/raw/sce/sce_layer2_earliest_latest_part001.csv",
    "SCE individual":   "data/raw/sce/Acton.csv",
    "SDGE profiles":    "data/raw/sdge/sdge_substation_profiles_part001.csv",
    "PacifiCorp layer1":"data/raw/pacificorp/pacificorp_layer1_earliest_latest_part001.csv",
}

# Human-readable output column name for each used raw column
OUTPUT_NAME: dict[str, str] = {
    "subname":     "substation_name",
    "monthhour":   "month + hour",
    "high":        "max_load",
    "low":         "min_load",
    "YEAR":        "year",
    "MONTH":       "month",
    "HOUR":        "hour",
    "SUBSTATION":  "substation_name",
    "MIN_LOAD":    "min_load",
    "MAX_LOAD":    "max_load",
    "AssetName":   "substation_name",
    "Month":       "month",
    "LoadDay":     "(routing) max_load / min_load",
    "Name":        "substation_name",
    "longitude":   "longitude",
    "latitude":    "latitude",
}


def _read_columns(path: pathlib.Path) -> list[str]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh).fieldnames or [])


def main() -> None:
    for source, sample_rel in SAMPLE_FILES.items():
        sample_path = ROOT / sample_rel
        if not sample_path.exists():
            print(f"[{source}]  sample file not found: {sample_path}")
            continue

        raw_cols = _read_columns(sample_path)
        used = USED[source]

        used_cols   = [c for c in raw_cols if c in used]
        dropped_cols = [c for c in raw_cols if c not in used]

        print(f"{'='*64}")
        print(f"  {source}")
        print(f"  {sample_path.relative_to(ROOT)}")
        print(f"{'='*64}")

        if used_cols:
            print("  USED:")
            for col in used_cols:
                out = OUTPUT_NAME.get(col, col)
                if out != col:
                    print(f"    {col:<30}  ->  {out}")
                else:
                    print(f"    {col}")

        if dropped_cols:
            print("  DROPPED (not in processed output):")
            for col in dropped_cols:
                print(f"    {col}")
        else:
            print("  DROPPED: none — all columns used")

        print()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
