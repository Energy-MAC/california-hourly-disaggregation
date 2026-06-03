"""
Validates consistency and coverage of SCE data in data/raw/sce/.

Two sources exist:
  1. sce_layer2_*.csv  — scraped programmatically; includes coordinates; years 2023-2024
  2. Individual *.csv  — manually downloaded per-substation; no coordinates; years 2025-2026

Checks
------
  1. Schema: all individual files share the same columns
  2. Row count: each individual file should have 288 rows (12 months × 24 hours)
  3. Year coverage: reports years per source and flags overlap
  4. Substation coverage: names present in one source but not the other
"""
from __future__ import annotations

import csv
import pathlib
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCE_DIR = ROOT / "data" / "raw" / "sce"
EXPECTED_ROW_COUNT = 288  # 12 months × 24 hours


def _read_col_names(path: pathlib.Path) -> tuple[str, ...]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return tuple(csv.DictReader(fh).fieldnames or ())


def _count_rows(path: pathlib.Path) -> int:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return sum(1 for _ in csv.DictReader(fh))


def _year_set(path: pathlib.Path, year_field: str = "YEAR") -> set[str]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return {row[year_field] for row in csv.DictReader(fh)}


def section(title: str) -> None:
    print()
    print("=" * 64)
    print(title)
    print("=" * 64)


def main() -> None:
    all_csvs = sorted(SCE_DIR.glob("*.csv"))
    layer2_files = [f for f in all_csvs if "layer2" in f.name]
    individual_files = [f for f in all_csvs if "layer2" not in f.name]

    print(f"SCE directory : {SCE_DIR}")
    print(f"Individual files : {len(individual_files)}")
    print(f"Layer2 file(s)   : {len(layer2_files)}")

    # ── 1. Schema ─────────────────────────────────────────────────────────────
    section("1. SCHEMA  (individual files)")

    col_variants: Counter[tuple[str, ...]] = Counter()
    for f in individual_files:
        col_variants[_read_col_names(f)] += 1

    if len(col_variants) == 1:
        only_cols = next(iter(col_variants))
        print(f"OK   All {len(individual_files)} files share columns:")
        print(f"     {list(only_cols)}")
    else:
        print(f"WARN {len(col_variants)} distinct column layouts found:")
        for cols, n in col_variants.most_common():
            print(f"  {n:4d} × {list(cols)}")

    if layer2_files:
        l2_cols = _read_col_names(layer2_files[0])
        indiv_cols = next(iter(col_variants))
        extra = [c for c in l2_cols if c not in indiv_cols]
        print(f"Layer2 extra columns vs individual: {extra}")

    # ── 2. Row count / duplicate hours ───────────────────────────────────────
    section("2. ROWS PER HOUR  (each (year, month) group should have 24 rows)")

    from collections import defaultdict

    multiplier_groups: dict[int, list[str]] = defaultdict(list)  # {mult: [filenames]}
    # For files with mult > 1: True = all duplicate rows share identical load values
    dup_values_identical: dict[str, bool] = {}

    for f in individual_files:
        with open(f, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))

        ym_counts: Counter[tuple[str, str]] = Counter(
            (r["YEAR"], r["MONTH"]) for r in rows
        )
        max_per_ym = max(ym_counts.values()) if ym_counts else 0
        mult = max_per_ym // 24
        multiplier_groups[mult].append(f.name)

        if mult > 1:
            ymh_vals: dict[tuple[str, str, str], set[tuple[str, str]]] = defaultdict(set)
            for r in rows:
                key = (r["YEAR"], r["MONTH"], r["HOUR"])
                ymh_vals[key].add((r["MIN_LOAD"], r["MAX_LOAD"]))
            dup_values_identical[f.name] = all(
                len(v) == 1 for v in ymh_vals.values() if len(v) > 1
            )

    for mult in sorted(multiplier_groups):
        names = multiplier_groups[mult]
        label = "OK  " if mult == 1 else "WARN"
        print(f"{label} {len(names):3d} files: {mult}x rows per (year, month) — "
              f"{'normal' if mult == 1 else f'{mult} records per hour'}")
        if mult > 1:
            shown = min(len(names), 5)
            for name in names[:shown]:
                print(f"     {name}")
            if len(names) > shown:
                print(f"     ... and {len(names) - shown} more")

    if dup_values_identical:
        n_identical = sum(1 for v in dup_values_identical.values() if v)
        n_different = sum(1 for v in dup_values_identical.values() if not v)
        print()
        if n_identical:
            print(f"     {n_identical} of those files: duplicate rows have IDENTICAL min/max values (true duplicates)")
        if n_different:
            print(f"     {n_different} of those files: duplicate rows have DIFFERENT min/max values (distinct data)")

    # ── 3. Year coverage ──────────────────────────────────────────────────────
    section("3. YEAR COVERAGE")

    indiv_years: set[str] = set()
    for f in individual_files:
        indiv_years |= _year_set(f)
    print(f"Individual files : years = {sorted(indiv_years)}")

    if layer2_files:
        l2_years = _year_set(layer2_files[0])
        print(f"Layer2 scrape    : years = {sorted(l2_years)}")
        overlap = indiv_years & l2_years
        if overlap:
            print(f"WARN Overlapping years: {sorted(overlap)}")
            print("     Rows with the same (substation, year, month, hour) appear in both sources.")
        else:
            print("OK   Sources cover distinct time periods — safe to combine without deduplication.")

    # ── 4. Substation coverage ────────────────────────────────────────────────
    section("4. SUBSTATION COVERAGE")

    indiv_names = {f.stem for f in individual_files}

    if layer2_files:
        with open(layer2_files[0], newline="", encoding="utf-8-sig") as fh:
            l2_names = {row["SUBSTATION"] for row in csv.DictReader(fh)}

        print(f"Individual files : {len(indiv_names)} unique substations")
        print(f"Layer2 scrape    : {len(l2_names)} unique substations")

        only_indiv = sorted(indiv_names - l2_names)
        only_l2 = sorted(l2_names - indiv_names)

        if only_indiv:
            print(f"\nWARN {len(only_indiv)} substation(s) only in individual files (no coordinates from layer2):")
            for name in only_indiv:
                print(f"  {name}")
        else:
            print("OK   Every individually-downloaded substation appears in layer2.")

        if only_l2:
            n = len(only_l2)
            shown = min(n, 30)
            print(f"\n     {n} substation(s) only in layer2 (no individual download):")
            for name in only_l2[:shown]:
                print(f"  {name}")
            if n > shown:
                print(f"  ... and {n - shown} more")

    print()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
