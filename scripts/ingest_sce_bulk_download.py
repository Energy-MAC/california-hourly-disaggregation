"""
Ingest the DRPEP "Bulk Download -> Historical Substation Load Profiles -> Download All"
file and (optionally) compare it against the voltage-converted layer2 data.

How to obtain the bulk download
--------------------------------
1. Go to https://drpep.sce.com/drpep/
2. Click "Bulk Download" in the toolbar.
3. Toggle on "Historical Substation Load Profiles".
4. Click "Download All".
5. Save the resulting file (ZIP or CSV) somewhere and pass its path to this script.

Supported input formats
-----------------------
  ZIP of individual CSVs  — one CSV per substation, same schema as the manually
                             downloaded files (YEAR, MONTH, HOUR, SUBSTATION,
                             MIN_LOAD, MAX_LOAD, MONTHLABEL).  Values are in MW.
  Single consolidated CSV — all substations in one file, same columns as above.

Output
------
  data/raw/sce/bulk_download/   individual CSVs extracted from ZIP (if ZIP input)
  data/raw/sce/sce_bulk_download_all.csv   consolidated CSV (written in both cases)

Comparison
----------
  Pass --compare to diff sce_bulk_download_all.csv against sce_layer2_mw_part001.csv.
  Both files must exist.  Comparison is keyed on (SUBSTATION, YEAR, MONTH, HOUR) and
  reports per-substation mean absolute % error for MIN_LOAD and MAX_LOAD.

Usage examples
--------------
  python scripts/ingest_sce_bulk_download.py path/to/download.zip
  python scripts/ingest_sce_bulk_download.py path/to/download.csv
  python scripts/ingest_sce_bulk_download.py path/to/download.zip --compare
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RAW_SCE = ROOT / "data" / "raw" / "sce"
BULK_DIR = RAW_SCE / "bulk_download"
BULK_ALL = RAW_SCE / "sce_bulk_download_all.csv"
LAYER2_MW = RAW_SCE / "sce_layer2_mw_part001.csv"

EXPECTED_COLS = {"YEAR", "MONTH", "HOUR", "SUBSTATION", "MIN_LOAD", "MAX_LOAD"}
OUT_COLS = ["YEAR", "MONTH", "HOUR", "SUBSTATION", "MIN_LOAD", "MAX_LOAD", "MONTHLABEL"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_csv_bytes(data: bytes, filename: str) -> list[dict]:
    """Parse CSV bytes (handles UTF-8 BOM)."""
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    cols = set(reader.fieldnames or [])
    if not EXPECTED_COLS.issubset(cols):
        missing = EXPECTED_COLS - cols
        print(f"  WARN {filename}: missing columns {missing} — skipped")
        return []
    return rows


def _read_csv_file(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    return rows


def _write_consolidated(all_rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)


# ── Ingest ────────────────────────────────────────────────────────────────────

def ingest_zip(src: Path) -> list[dict]:
    BULK_DIR.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict] = []
    with zipfile.ZipFile(src) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        print(f"ZIP contains {len(csv_names)} CSV file(s).")
        for name in sorted(csv_names):
            data = zf.read(name)
            rows = _read_csv_bytes(data, name)
            if not rows:
                continue
            # Write individual file to bulk_download/
            out_file = BULK_DIR / Path(name).name
            out_file.write_bytes(data)
            all_rows.extend(rows)
    return all_rows


def ingest_csv(src: Path) -> list[dict]:
    rows = _read_csv_file(src)
    if not rows:
        raise ValueError(f"No valid rows found in {src}")
    subs = {r.get("SUBSTATION") for r in rows}
    print(f"Single CSV: {len(rows):,} rows, {len(subs)} unique substations.")
    return rows


def ingest(src: Path) -> list[dict]:
    if src.suffix.lower() == ".zip":
        print(f"Detected ZIP input: {src}")
        rows = ingest_zip(src)
    elif src.suffix.lower() in (".csv", ".txt"):
        print(f"Detected CSV input: {src}")
        rows = ingest_csv(src)
    else:
        raise ValueError(f"Unrecognised file type: {src.suffix}. Expected .zip or .csv")

    _write_consolidated(rows, BULK_ALL)
    subs = {r.get("SUBSTATION") for r in rows}
    print(f"\nConsolidated: {len(rows):,} rows, {len(subs)} substations -> {BULK_ALL}")
    return rows


# ── Comparison ────────────────────────────────────────────────────────────────

def compare(bulk_path: Path = BULK_ALL, layer2_mw_path: Path = LAYER2_MW) -> None:
    if not bulk_path.exists():
        print(f"Bulk download file not found: {bulk_path}")
        return
    if not layer2_mw_path.exists():
        print(f"Layer2 MW file not found: {layer2_mw_path}")
        print("Run: python scripts/scrape_sce.py convert-to-mw")
        return

    # Load both into dicts keyed by (SUBSTATION, YEAR, MONTH, HOUR)
    def _load(path: Path) -> dict[tuple, dict]:
        index: dict[tuple, dict] = {}
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                key = (row["SUBSTATION"], row["YEAR"], row["MONTH"], row["HOUR"])
                index[key] = row
        return index

    print(f"\nLoading bulk download ...  {bulk_path}")
    bulk = _load(bulk_path)
    print(f"Loading layer2 MW ...      {layer2_mw_path}")
    layer2 = _load(layer2_mw_path)

    common_keys = set(bulk) & set(layer2)
    print(f"\nMatched keys (substation, year, month, hour): {len(common_keys):,}")
    print(f"  Only in bulk download : {len(bulk) - len(common_keys):,}")
    print(f"  Only in layer2 MW     : {len(layer2) - len(common_keys):,}")

    if not common_keys:
        print("No overlapping rows to compare.")
        return

    # Per-substation absolute % error
    from collections import defaultdict
    sub_errors: dict[str, list[float]] = defaultdict(list)

    for key in common_keys:
        sub = key[0]
        for field in ("MIN_LOAD", "MAX_LOAD"):
            try:
                b = float(bulk[key][field])
                l = float(layer2[key][field])
                if b != 0:
                    sub_errors[sub].append(abs(b - l) / abs(b) * 100)
            except (ValueError, TypeError, KeyError):
                pass

    if not sub_errors:
        print("No numeric pairs to compare.")
        return

    all_errors = [e for errs in sub_errors.values() for e in errs]
    print(f"\nAbs % error  (bulk MW as reference):")
    print(f"  Mean  : {sum(all_errors)/len(all_errors):.2f}%")
    print(f"  Median: {sorted(all_errors)[len(all_errors)//2]:.2f}%")
    print(f"  Max   : {max(all_errors):.2f}%")

    # Worst 10 substations
    sub_mean = {s: sum(v)/len(v) for s, v in sub_errors.items()}
    worst = sorted(sub_mean.items(), key=lambda x: x[1], reverse=True)[:10]
    print(f"\n  Worst 10 substations by mean abs % error:")
    print(f"  {'Substation':<35}  Mean err%")
    for sub, err in worst:
        print(f"  {sub:<35}  {err:.2f}%")

    # Best summary
    within_5pct = sum(1 for e in all_errors if e <= 5)
    print(f"\n  {within_5pct/len(all_errors)*100:.1f}% of load values within 5% of bulk download")


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "source",
        nargs="?",
        metavar="FILE",
        help="Path to the bulk download file (.zip or .csv). "
             "Omit to skip ingestion and only run --compare.",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="After ingestion, compare bulk MW values against sce_layer2_mw_part001.csv.",
    )
    parser.add_argument(
        "--compare-only",
        action="store_true",
        help="Skip ingestion; compare existing sce_bulk_download_all.csv against layer2 MW.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.compare_only:
        compare()
        return

    if not args.source:
        build_parser().print_help()
        sys.exit(1)

    src = Path(args.source)
    if not src.exists():
        print(f"File not found: {src}")
        sys.exit(1)

    ingest(src)

    if args.compare:
        compare()


if __name__ == "__main__":
    main()
