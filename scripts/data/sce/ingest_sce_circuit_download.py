"""
Ingest the DRPEP "Bulk Download -> Circuit Load Profiles -> Download All"
ZIP or unzipped directory into a single consolidated CSV.

How to obtain
-------------
1. Go to https://drpep.sce.com/drpep/
2. Click "Bulk Download" in the toolbar.
3. Toggle on "Circuit Load Profiles".
4. Click "Download All".
5. Save the resulting ZIP (or unzip it) and pass the path to this script.

Schema (per-circuit CSV)
------------------------
  CIRCUIT_NAME   feeder/circuit name
  VOLTAGE        distribution voltage (e.g. 12KV, 4KV, 16KV)
  YEAR           data year
  MONTH          0-indexed (0 = January, 11 = December)
  HOUR           0-indexed hour-beginning (0-23), fixed PST
  MIN_LOAD       ~10th-percentile MW
  MAX_LOAD       ~90th-percentile MW
  SUBSTATION     parent substation name
  MONTHLABEL     display label (e.g. "JAN, 00")

Output
------
  data/raw/sce/sce_circuit_profiles.csv  — all circuits consolidated (always written)

Notes
-----
- P.T. circuits (SUBSTATION contains "P.T.") are switching nodes, not real
  load-serving circuits.  They have many duplicate rows per (MONTH, HOUR) cell.
  Use --drop-pt to exclude them (default: keep and warn).
- A small number of non-PT circuits (~11) have duplicate rows per cell.
  These are kept as-is; downstream scripts must handle them.

Processing TODOs (for downstream analysis scripts)
---------------------------------------------------
- MONTH is 0-indexed here (0 = January). Add 1 before joining to substation
  profiles or any other source that uses 1-indexed months.
- If a circuit's parent substation is dropped as a P.T. switching node (i.e.
  SUBSTATION contains "P.T."), that circuit should also be dropped — it carries
  switching-node load, not real distribution load.

Usage examples
--------------
  python scripts/data/sce/ingest_sce_circuit_download.py data/raw/sce/CIRCUIT.zip
  python scripts/data/sce/ingest_sce_circuit_download.py data/raw/sce/CIRCUIT/
  python scripts/data/sce/ingest_sce_circuit_download.py data/raw/sce/CIRCUIT.zip --drop-pt
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

RAW_SCE = ROOT / "data" / "raw" / "sce"
OUT_CSV = RAW_SCE / "sce_circuit_profiles.csv"

EXPECTED_COLS = {"CIRCUIT_NAME", "VOLTAGE", "YEAR", "MONTH", "HOUR", "MIN_LOAD", "MAX_LOAD", "SUBSTATION"}
OUT_COLS = ["CIRCUIT_NAME", "VOLTAGE", "YEAR", "MONTH", "HOUR", "MIN_LOAD", "MAX_LOAD", "SUBSTATION", "MONTHLABEL"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_pt(rows: list[dict]) -> bool:
    return any("P.T." in r.get("SUBSTATION", "") for r in rows)


def _read_csv_bytes(data: bytes, filename: str) -> list[dict]:
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
        return list(csv.DictReader(fh))


def _write_consolidated(all_rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)


# ── Ingest ────────────────────────────────────────────────────────────────────

def _collect_rows(rows: list[dict], drop_pt: bool, pt_circuits: list[str], dup_circuits: list[str]) -> list[dict]:
    """Filter P.T. circuits and track duplicates; return rows to keep."""
    if not rows:
        return []

    name = rows[0].get("CIRCUIT_NAME", "?")
    volt = rows[0].get("VOLTAGE", "?")
    label = f"{name}_{volt}"

    if _is_pt(rows):
        pt_circuits.append(label)
        if drop_pt:
            return []
        return rows

    expected = 12 * 24  # 288 cells
    if len(rows) > expected:
        dup_circuits.append(f"{label} ({len(rows)} rows)")

    return rows


def ingest_zip(src: Path, drop_pt: bool) -> list[dict]:
    all_rows: list[dict] = []
    pt_circuits: list[str] = []
    dup_circuits: list[str] = []

    with zipfile.ZipFile(src) as zf:
        csv_names = sorted(n for n in zf.namelist() if n.lower().endswith(".csv"))
        print(f"ZIP contains {len(csv_names)} CSV file(s).")
        for name in csv_names:
            rows = _read_csv_bytes(zf.read(name), name)
            all_rows.extend(_collect_rows(rows, drop_pt, pt_circuits, dup_circuits))

    _report(pt_circuits, dup_circuits, drop_pt)
    return all_rows


def ingest_dir(src: Path, drop_pt: bool) -> list[dict]:
    all_rows: list[dict] = []
    pt_circuits: list[str] = []
    dup_circuits: list[str] = []

    csv_files = sorted(src.glob("*.csv"))
    print(f"Directory contains {len(csv_files)} CSV file(s).")
    for path in csv_files:
        rows = _read_csv_file(path)
        if not rows:
            continue
        cols = set(rows[0].keys()) if rows else set()
        if not EXPECTED_COLS.issubset(cols):
            missing = EXPECTED_COLS - cols
            print(f"  WARN {path.name}: missing columns {missing} — skipped")
            continue
        all_rows.extend(_collect_rows(rows, drop_pt, pt_circuits, dup_circuits))

    _report(pt_circuits, dup_circuits, drop_pt)
    return all_rows


def _report(pt_circuits: list[str], dup_circuits: list[str], drop_pt: bool) -> None:
    if pt_circuits:
        action = "dropped" if drop_pt else "kept (use --drop-pt to exclude)"
        print(f"\nP.T. switching-node circuits ({len(pt_circuits)} {action}).")
    if dup_circuits:
        print(f"\nCircuits with duplicate (MONTH, HOUR) cells ({len(dup_circuits)}):")
        for label in dup_circuits:
            print(f"  {label}")


def ingest(src: Path, drop_pt: bool) -> list[dict]:
    if src.is_dir():
        print(f"Detected directory input: {src}")
        rows = ingest_dir(src, drop_pt)
    elif src.suffix.lower() == ".zip":
        print(f"Detected ZIP input: {src}")
        rows = ingest_zip(src, drop_pt)
    else:
        raise ValueError(f"Unrecognised input: {src}. Expected a .zip file or directory.")

    _write_consolidated(rows, OUT_CSV)
    circuits = {(r.get("CIRCUIT_NAME"), r.get("VOLTAGE")) for r in rows}
    print(f"\nConsolidated: {len(rows):,} rows, {len(circuits):,} circuits -> {OUT_CSV}")
    return rows


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "source",
        metavar="FILE_OR_DIR",
        help="Path to the CIRCUIT.zip file or the unzipped CIRCUIT/ directory.",
    )
    parser.add_argument(
        "--drop-pt",
        action="store_true",
        help="Exclude P.T. switching-node circuits (SUBSTATION contains 'P.T.').",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    src = Path(args.source)
    if not src.exists():
        print(f"Not found: {src}")
        sys.exit(1)
    ingest(src, drop_pt=args.drop_pt)


if __name__ == "__main__":
    main()
