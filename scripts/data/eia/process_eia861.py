"""
Process EIA Form 861 sales data into CA-fraction estimates by balancing authority.

Reads from one of two raw sources (auto-detected, or specified with --source):
  pudl  : data/raw/eia/pudl/core_eia861__yearly_sales_CA8.parquet
            Download with: python scripts/ingest_eia861_pudl.py
  eia   : data/raw/eia/form861/{year}/Sales_Ult_Cust_{year}.xlsx
            Download with: python scripts/scrape_eia_form861.py

Both sources produce the same intermediate format:
  year | ba_code | state | total_mwh   (one row per BA-state-year)

Output
------
  data/processed/eia/eia861_ca_fractions.csv
    year | ba_code | total_mwh | ca_mwh | ca_fraction

Analysis
--------
For each of the 8 CA balancing authorities (BANC, CISO, IID, LDWP, NEVP,
PACW, TIDC, WALC), the CA fraction answers: what share of this BA's total
retail sales goes to customers within California?

This fraction is used to partition EIA-930 hourly demand between in-CA and
out-of-CA load, explaining why:
  - EIA CA8 group overstates California demand (NEVP=0.4%, PACW=4% in CA)
  - EIA CAL region ≈ CISO + IID + LDWP + BANC + TIDC + WALC*31%

Usage
-----
  python scripts/data/eia/process_eia861.py                      # auto-detect source
  python scripts/data/eia/process_eia861.py --source pudl        # force PUDL parquet
  python scripts/data/eia/process_eia861.py --source eia --years 2022 2023 2024
  python scripts/data/eia/process_eia861.py --no-output          # print only, no CSV
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

# Force UTF-8 output on Windows consoles that default to cp1252
if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower().replace("-", "") not in ("utf8", "utf-8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src.data.eia.pudl_eia930 import CA8
from src.data.eia.form861 import find_raw_dir, parse_sales_excel

ROOT     = Path(__file__).resolve().parents[3]
PUDL_DIR = ROOT / "data" / "raw" / "eia" / "pudl"
OUT_DIR  = ROOT / "data" / "processed" / "eia"

_PUDL_FILE     = PUDL_DIR / "core_eia861__yearly_sales_CA8.parquet"
_DEFAULT_YEARS = list(range(2020, 2025))

_BA_DESC = {
    "BANC": "Balancing Authority of Northern California",
    "CISO": "California ISO (PG&E + SCE + SDG&E footprint)",
    "IID":  "Imperial Irrigation District",
    "LDWP": "Los Angeles Dept. of Water and Power",
    "TIDC": "Turlock Irrigation District",
    "WALC": "Western Area Lower Colorado (AZ/NV + southern CA)",
    "PACW": "PacifiCorp West (OR/WA/ID/UT + far-northern CA)",
    "NEVP": "NV Energy (primarily Nevada / Las Vegas)",
}


# ── Schema detection ──────────────────────────────────────────────────────────

def _pick(df: pd.DataFrame, candidates: list[str], required: bool = True) -> str | None:
    """Return the first candidate column name present in df."""
    for c in candidates:
        if c in df.columns:
            return c
    if required:
        raise ValueError(f"None of {candidates} found in columns: {list(df.columns)}")
    return None


def _to_year(series: pd.Series) -> pd.Series:
    """Convert a year-like column (datetime or integer) to integer year."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return series.dt.year
    try:
        converted = pd.to_datetime(series, errors="coerce")
        if converted.notna().mean() > 0.9:
            return converted.dt.year
    except Exception:
        pass
    return pd.to_numeric(series, errors="coerce").astype("Int64")


# ── Shared aggregation ────────────────────────────────────────────────────────

def compute_ca_fractions(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate a BA-state-year sales table into CA fractions.

    Input columns: year | ba_code | state | total_mwh
    Output columns: year | ba_code | total_mwh | ca_mwh | ca_fraction
    """
    total = df.groupby(["year", "ba_code"])["total_mwh"].sum()
    ca    = (
        df[df["state"] == "CA"]
        .groupby(["year", "ba_code"])["total_mwh"]
        .sum()
    )
    result = total.reset_index().rename(columns={"total_mwh": "total_mwh"})
    idx    = pd.MultiIndex.from_frame(result[["year", "ba_code"]])
    result["ca_mwh"]      = ca.reindex(idx).fillna(0).values
    result["ca_fraction"] = result["ca_mwh"] / result["total_mwh"]
    return (
        result
        .sort_values(["year", "total_mwh"], ascending=[True, False])
        .reset_index(drop=True)
    )


# ── PUDL source ───────────────────────────────────────────────────────────────

def load_from_pudl(pudl_file: Path = _PUDL_FILE) -> pd.DataFrame:
    """
    Read PUDL EIA-861 parquet and return a BA-state-year sales table.

    Returns columns: year | ba_code | state | total_mwh
    """
    print(f"Reading PUDL parquet: {pudl_file.relative_to(ROOT)} ...")
    df = pd.read_parquet(pudl_file)
    print(f"  {len(df):,} rows, columns: {list(df.columns)}")

    ba_col    = _pick(df, ["balancing_authority_code_eia", "ba_code", "balancing_authority_code"])
    state_col = _pick(df, ["state"])
    mwh_col   = _pick(df, ["sales_mwh", "mwh", "total_mwh"])
    year_col  = _pick(df, ["report_date", "year"])
    class_col = _pick(df, ["customer_class", "sector_name"], required=False)

    if class_col:
        total_vals = {"total", "all_sectors"}
        sub = df[df[class_col].str.lower().isin(total_vals)]
        if sub.empty:
            print(f"  WARNING: no 'total' rows in {class_col}; using all rows")
            sub = df
        df = sub

    result = (
        df
        .assign(
            year    = _to_year(df[year_col]),
            ba_code = df[ba_col].str.strip().str.upper(),
            state   = df[state_col].str.strip().str.upper(),
        )
        .groupby(["year", "ba_code", "state"], as_index=False)[mwh_col]
        .sum()
        .rename(columns={mwh_col: "total_mwh"})
    )
    print(f"  Loaded {result['year'].nunique()} year(s), {result['ba_code'].nunique()} BAs")
    return result


# ── Direct EIA source ─────────────────────────────────────────────────────────

def load_from_eia(years: list[int]) -> pd.DataFrame:
    """
    Parse EIA Form 861 Excel files and return a BA-state-year sales table.

    Returns columns: year | ba_code | state | total_mwh
    """
    dfs: list[pd.DataFrame] = []
    for year in sorted(years):
        print(f"\n=== Year {year} ===")
        if find_raw_dir(year) is None:
            print(f"  SKIP: no raw data. Run: python scripts/scrape_eia_form861.py --years {year}")
            continue
        try:
            dfs.append(parse_sales_excel(year))
        except FileNotFoundError as e:
            print(f"  SKIP: {e}")
    if not dfs:
        raise SystemExit("No EIA data found. Run scrape_eia_form861.py first.")
    return pd.concat(dfs, ignore_index=True)


# ── Summary printing ──────────────────────────────────────────────────────────

def print_ca8_summary(fracs: pd.DataFrame) -> None:
    years = sorted(fracs["year"].unique())

    print()
    print("=" * 80)
    print("EIA Form 861 — California retail-sales fraction for each CA8 BA")
    print("(Use fractions to partition EIA-930 hourly demand into CA vs out-of-CA)")
    print("=" * 80)

    for year in years:
        df = fracs[fracs["year"] == year].set_index("ba_code")
        avail = [ba for ba in CA8 if ba in df.index]

        print(f"\n  {year}")
        print(f"  {'BA':<6} {'Total (TWh)':>12} {'CA (TWh)':>11} {'CA %':>7}  Service territory")
        print(f"  {'-'*6} {'-'*12} {'-'*11} {'-'*7}  {'-'*40}")
        for ba in avail:
            r    = df.loc[ba]
            desc = _BA_DESC.get(ba, "")
            print(
                f"  {ba:<6} {r['total_mwh']/1e6:>12.3f} {r['ca_mwh']/1e6:>11.3f} "
                f"{r['ca_fraction']*100:>7.1f}%  {desc}"
            )

    # CA8 vs actual California implication
    print()
    print("  Implication for EIA CA8 group vs actual California demand (retail sales proxy):")
    for year in years:
        df = fracs[fracs["year"] == year].set_index("ba_code")
        avail = {ba for ba in CA8 if ba in df.index}

        ca8_total  = sum(df.loc[ba, "total_mwh"] for ba in avail)
        ca8_ca_mwh = sum(df.loc[ba, "ca_mwh"]    for ba in avail)
        out_of_ca  = ca8_total - ca8_ca_mwh

        print(f"\n  {year}:")
        print(f"    CA8 total retail sales:                {ca8_total/1e6:>7.1f} TWh")
        print(f"    Actually in California:                {ca8_ca_mwh/1e6:>7.1f} TWh")
        print(f"    Out-of-CA (overstates CA demand by):   {out_of_ca/1e6:>7.1f} TWh")
        # Break down by contributing BA
        out_rows = []
        for ba in avail:
            out = df.loc[ba, "total_mwh"] - df.loc[ba, "ca_mwh"]
            if out > 1e3:  # more than 1 GWh out of CA
                out_rows.append((ba, out))
        out_rows.sort(key=lambda x: -x[1])
        for ba, out in out_rows:
            print(f"      {ba:<6} out-of-CA: {out/1e6:.2f} TWh  "
                  f"({(1 - df.loc[ba,'ca_fraction'])*100:.1f}% of {df.loc[ba,'total_mwh']/1e6:.1f} TWh total)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source", choices=["pudl", "eia", "auto"], default="auto",
        help="Data source. 'auto' prefers PUDL if parquet exists, else falls back to EIA.",
    )
    parser.add_argument(
        "--years", nargs="+", type=int, default=_DEFAULT_YEARS, metavar="YEAR",
        help=f"Years to process from EIA source (ignored for PUDL). "
             f"Default: {_DEFAULT_YEARS[0]}-{_DEFAULT_YEARS[-1]}",
    )
    parser.add_argument(
        "--no-output", action="store_true",
        help="Skip writing the output CSV (print summary only).",
    )
    args = parser.parse_args()

    if args.source == "auto":
        source = "pudl" if _PUDL_FILE.exists() else "eia"
        label  = f"auto -> {source}"
        if source == "pudl":
            print(f"Auto-detected: {_PUDL_FILE.relative_to(ROOT)}")
        else:
            print("PUDL parquet not found; using EIA Excel source.")
    else:
        source = args.source
        label  = source

    print(f"Source: {label}\n")

    if source == "pudl":
        raw = load_from_pudl()
    else:
        raw = load_from_eia(args.years)

    fracs = compute_ca_fractions(raw)

    if not args.no_output:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / "eia861_ca_fractions.csv"
        fracs.to_csv(out_path, index=False)
        print(f"\nWrote {len(fracs)} rows -> {out_path.relative_to(ROOT)}")

    print_ca8_summary(fracs)


if __name__ == "__main__":
    main()
