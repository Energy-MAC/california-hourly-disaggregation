"""
Download and process EIA Form 861 annual data.

EIA Form 861 (Annual Electric Power Industry Report) provides:
  - Retail electricity sales by utility, customer class, and state
  - Balancing authority assignments for each utility/state combination

This script downloads Form 861 ZIP files for specified years and extracts
the Sales_Ult_Cust workbook to compute California-specific retail sales
broken out by balancing authority.  Two primary uses:

  1. Verify CA fractions of NEVP and PACW (mostly serve NV and OR/WA).
     Confirmed 2024: NEVP ~0.4% in CA (0.18 TWh), PACW ~4% in CA (0.85 TWh).

  2. Quantify the non-CISO California load that explains why EIA CAL region
     exceeds EIA CISO BA demand:
       IID 3.7 + LDWP 23.4 + BANC 15.8 + TIDC 2.3 + WALC-CA 3.8 ≈ 49 TWh (2024)
     This is consistent with EIA CAL region exceeding EIA CISO by ~40-50 TWh.

Note on Form 861 vs EIA-930
---------------------------
Form 861 = RETAIL SALES (MWh billed to end-use customers after net metering).
EIA-930  = SYSTEM DEMAND (total BA load measured at grid boundary).
These differ because:
  - Net-metered rooftop solar reduces retail sales billed to customers
    but EIA-930 demand already nets out BTM generation at the BA boundary
  - T&D losses appear in EIA-930 demand but not in retail sales
The Form 861 is used here ONLY to compute state-by-state splits for each BA
(which fraction of NEVP / PACW / WALC load is actually in California).

Outputs
-------
  data/processed/eia/form861_ca_sales_by_ba.csv
    Columns: year, ba_code, total_mwh, ca_mwh, ca_fraction

Downloads
---------
  data/raw/eia/form861/{year}/    (one folder per year)

  Legacy paths from manually downloaded data are also checked:
    data/raw/eiaForm861/          (2024)
    data/raw/eiaForm8612023/      (2023)

Usage
-----
  python scripts/scrape_eia_form861.py                   # download + process 2020-2024
  python scripts/scrape_eia_form861.py --years 2023 2024
  python scripts/scrape_eia_form861.py --process-only    # skip download; process existing files
  python scripts/scrape_eia_form861.py --overwrite       # re-download even if already present
"""
from __future__ import annotations

import argparse
import io
import sys
import zipfile

# Force UTF-8 output on Windows consoles that default to cp1252
if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower().replace("-", "") not in ("utf8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from io import BytesIO
from pathlib import Path
import urllib.request
import urllib.error

import pandas as pd

ROOT    = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "eia" / "form861"
OUT_DIR = ROOT / "data" / "processed" / "eia"

# EIA Form 861 ZIP download URLs (try primary first, fall back to archive)
_URL_PRIMARY = "https://www.eia.gov/electricity/data/eia861/zip/f861{year}.zip"
_URL_ARCHIVE = "https://www.eia.gov/electricity/data/eia861/archive/zip/f861{year}.zip"

# Positional column names for Sales_Ult_Cust_{year}.xlsx
# (skip 2 header rows; columns are fixed-position in the EIA format)
_SALES_COLS = [
    "year", "utility_number", "utility_name", "part", "service_type", "data_type",
    "state", "ownership", "ba_code",
    "res_rev", "res_mwh", "res_cust",
    "com_rev", "com_mwh", "com_cust",
    "ind_rev", "ind_mwh", "ind_cust",
    "trn_rev", "trn_mwh", "trn_cust",
    "tot_rev", "tot_mwh", "tot_cust",
]

# BAs to highlight in the CA scope summary
_CA8_BAS    = ["CISO", "IID", "LDWP", "BANC", "TIDC", "WALC", "NEVP", "PACW"]
_INSTATE    = ["CISO", "IID", "LDWP", "BANC", "TIDC"]   # fully within California
_PARTIAL    = {"WALC": 0.312, "NEVP": 0.004, "PACW": 0.040}  # CA fraction from Form 861

# Legacy paths the user manually downloaded before this scraper existed
_LEGACY: dict[int, Path] = {
    2024: ROOT / "data" / "raw" / "eiaForm861",
    2023: ROOT / "data" / "raw" / "eiaForm8612023",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_raw_dir(year: int) -> Path | None:
    """Return path to extracted Form 861 data for a year (new or legacy location)."""
    for p in [RAW_DIR / str(year), _LEGACY.get(year, Path("/nonexistent"))]:
        if p.exists() and list(p.glob(f"Sales_Ult_Cust_{year}.xlsx")):
            return p
    return None


# ── Download ──────────────────────────────────────────────────────────────────

def download_form861(year: int, overwrite: bool = False) -> Path:
    """
    Download and extract EIA Form 861 ZIP for a given year.

    Returns the directory containing the extracted files.
    Skips download if data already exists locally (unless overwrite=True).
    """
    dest = RAW_DIR / str(year)
    existing = _find_raw_dir(year)
    if existing and not overwrite:
        print(f"  {year}: found existing data at {existing.relative_to(ROOT)} — skipping download")
        return existing

    dest.mkdir(parents=True, exist_ok=True)

    data: bytes | None = None
    for url_template in (_URL_PRIMARY, _URL_ARCHIVE):
        url = url_template.format(year=year)
        print(f"  Downloading {url} ...")
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (compatible; EIA861-scraper)"}
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            print(f"  Downloaded {len(data)/1024/1024:.1f} MB")
            break
        except urllib.error.HTTPError as e:
            print(f"    HTTP {e.code} from {url} — trying archive URL ...")
        except Exception as e:
            print(f"    Error ({e}) — trying archive URL ...")

    if data is None:
        raise RuntimeError(
            f"Could not download Form 861 for {year}.\n"
            f"Please download manually from https://www.eia.gov/electricity/data/eia861/\n"
            f"and extract to {dest}"
        )

    with zipfile.ZipFile(BytesIO(data)) as zf:
        zf.extractall(dest)
    n = len(list(dest.iterdir()))
    print(f"  Extracted {n} files to {dest.relative_to(ROOT)}")
    return dest


# ── Process ───────────────────────────────────────────────────────────────────

def process_ca_sales(year: int, raw_dir: Path | None = None) -> pd.DataFrame:
    """
    Parse Sales_Ult_Cust_{year}.xlsx and return CA-specific retail sales by BA.

    Returns DataFrame with columns:
      year | ba_code | total_mwh | ca_mwh | ca_fraction
    Sorted by total_mwh descending.
    """
    if raw_dir is None:
        raw_dir = _find_raw_dir(year)
    if raw_dir is None:
        raise FileNotFoundError(
            f"No Form 861 data found for {year}. "
            f"Run without --process-only to download, or place files in {RAW_DIR / str(year)}"
        )

    sales_file = raw_dir / f"Sales_Ult_Cust_{year}.xlsx"
    if not sales_file.exists():
        matches = list(raw_dir.glob("Sales_Ult_Cust_*.xlsx"))
        if not matches:
            raise FileNotFoundError(f"Sales_Ult_Cust_{year}.xlsx not found in {raw_dir}")
        sales_file = matches[0]

    print(f"  Parsing {sales_file.name} ...")
    df = pd.read_excel(sales_file, skiprows=2, header=None, dtype=str)

    # Trim to expected column count (some years have extra trailing columns)
    n_exp = len(_SALES_COLS)
    if len(df.columns) > n_exp:
        df = df.iloc[:, :n_exp]
    df.columns = _SALES_COLS[: len(df.columns)]

    df["tot_mwh"] = pd.to_numeric(df["tot_mwh"], errors="coerce")
    df["ba_code"] = df["ba_code"].str.strip().str.upper()
    df["state"]   = df["state"].str.strip().str.upper()
    df = df[df["tot_mwh"].notna() & df["ba_code"].ne("NAN") & df["ba_code"].ne("")].copy()

    total = df.groupby("ba_code")["tot_mwh"].sum()
    ca    = df[df["state"] == "CA"].groupby("ba_code")["tot_mwh"].sum()

    result = pd.DataFrame({
        "year":      year,
        "ba_code":   total.index,
        "total_mwh": total.values,
        "ca_mwh":    ca.reindex(total.index).fillna(0).values,
    })
    result["ca_fraction"] = result["ca_mwh"] / result["total_mwh"]
    return result.sort_values("total_mwh", ascending=False).reset_index(drop=True)


# ── Summary output ────────────────────────────────────────────────────────────

def print_ca_summary(dfs: list[pd.DataFrame]) -> None:
    """Print CAL vs CISO scope analysis and NEVP/PACW CA fractions across years."""
    combined = pd.concat(dfs, ignore_index=True)

    print()
    print("=" * 70)
    print("EIA Form 861 — California retail sales analysis")
    print("(Note: retail sales != EIA-930 demand; use fractions to partition demand)")
    print("=" * 70)

    for year in sorted(combined["year"].unique()):
        df   = combined[combined["year"] == year].set_index("ba_code")
        avail = [ba for ba in _CA8_BAS if ba in df.index]

        print(f"\n  Year {year}:")
        print(f"  {'BA':<8} {'Total (TWh)':>13} {'CA (TWh)':>12} {'CA %':>8}  Notes")
        print(f"  {'─'*8} {'─'*13} {'─'*12} {'─'*8}  {'─'*30}")

        for ba in avail:
            r    = df.loc[ba]
            note = ""
            if ba in _PARTIAL:
                note = f"<-- only ~{_PARTIAL[ba]*100:.0f}% in CA (Form 861 verified)"
            elif r["ca_fraction"] > 0.99:
                note = "(fully in CA)"
            print(
                f"  {ba:<8} {r['total_mwh']/1e6:>13.3f} "
                f"{r['ca_mwh']/1e6:>12.3f} "
                f"{r['ca_fraction']*100:>8.1f}%  {note}"
            )

        # Non-CISO CA contributions
        non_ciso_ca = sum(df.loc[ba, "ca_mwh"] for ba in _INSTATE[1:] if ba in df.index)
        walc_ca     = df.loc["WALC", "ca_mwh"] if "WALC" in df.index else 0.0
        ciso_ca     = df.loc["CISO", "ca_mwh"] if "CISO" in df.index else 0.0

        print()
        print(f"  CAL vs CISO (why EIA CAL region > EIA CISO BA), {year}:")
        print(f"    CISO retail sales (CA only):         {ciso_ca/1e6:>7.3f} TWh")
        print(f"    IID + LDWP + BANC + TIDC (CA):       {non_ciso_ca/1e6:>7.3f} TWh")
        print(f"    WALC CA-attributed (~31% of total):  {walc_ca/1e6:>7.3f} TWh")
        non_ciso_total = non_ciso_ca + walc_ca
        print(f"    Non-CISO CA total (retail sales):    {non_ciso_total/1e6:>7.3f} TWh")
        print(f"    => EIA CAL region ≈ CISO + ~{non_ciso_total/1e6:.0f} TWh more California load")
        print(f"    => This shows up in EIA-930 as CAL > CISO BA by similar margin")
        print(f"       (EIA-930 CAL demand gap may differ slightly from sales due to BTM solar)")

        # NEVP / PACW summary
        nevp_ca = df.loc["NEVP", "ca_mwh"] / 1e6 if "NEVP" in df.index else 0
        pacw_ca = df.loc["PACW", "ca_mwh"] / 1e6 if "PACW" in df.index else 0
        nevp_tot = df.loc["NEVP", "total_mwh"] / 1e6 if "NEVP" in df.index else 0
        pacw_tot = df.loc["PACW", "total_mwh"] / 1e6 if "PACW" in df.index else 0
        print()
        print(f"  NEVP/PACW CA correction ({year}):")
        print(f"    NEVP total: {nevp_tot:.2f} TWh  →  CA portion: {nevp_ca:.3f} TWh")
        print(f"    PACW total: {pacw_tot:.2f} TWh  →  CA portion: {pacw_ca:.3f} TWh")
        print(f"    Combined out-of-CA load in EIA CA8: "
              f"{(nevp_tot - nevp_ca) + (pacw_tot - pacw_ca):.1f} TWh")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--years", nargs="+", type=int,
        default=list(range(2020, 2025)),
        metavar="YEAR",
        help="Year(s) to download and process. Default: 2020-2024",
    )
    parser.add_argument(
        "--process-only", action="store_true",
        help="Skip download; only process existing local files",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Re-download even if data already exists",
    )
    parser.add_argument(
        "--no-output", action="store_true",
        help="Do not write the output CSV (print summary only)",
    )
    args = parser.parse_args()

    dfs: list[pd.DataFrame] = []

    for year in sorted(args.years):
        print(f"\n=== Year {year} ===")

        if not args.process_only:
            try:
                download_form861(year, overwrite=args.overwrite)
            except RuntimeError as e:
                print(f"  WARNING: {e}")

        try:
            df = process_ca_sales(year)
            dfs.append(df)
            print(f"  Processed {len(df)} BA entries ({df['ba_code'].nunique()} unique BAs)")
        except FileNotFoundError as e:
            print(f"  SKIP: {e}")

    if not dfs:
        print("\nNo data processed.")
        sys.exit(1)

    if not args.no_output:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / "form861_ca_sales_by_ba.csv"
        combined = pd.concat(dfs, ignore_index=True)
        combined.to_csv(out_path, index=False)
        print(f"\nWrote {len(combined)} rows -> {out_path.relative_to(ROOT)}")

    print_ca_summary(dfs)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
