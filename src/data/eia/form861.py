"""
Direct EIA Form 861 ZIP download and Sales_Ult_Cust worksheet parsing.

EIA Form 861 (Annual Electric Power Industry Report) publishes retail
electricity sales by utility, customer class, state, and balancing
authority.  The relevant worksheet is Sales_Ult_Cust_{year}.xlsx.

ZIP download URLs (tried in order):
  primary : https://www.eia.gov/electricity/data/eia861/zip/f861{year}.zip
  archive : https://www.eia.gov/electricity/data/eia861/archive/zip/f861{year}.zip

Only Sales_Ult_Cust_{year}.xlsx is extracted from the ZIP; all other
worksheet files are discarded to keep the raw folder small.

Output directory: data/raw/eia/form861/{year}/

Note on Form 861 vs EIA-930
---------------------------
Form 861 = RETAIL SALES (MWh billed to end-use customers, net of net-metering).
EIA-930  = SYSTEM DEMAND (total BA load measured at the grid boundary).
These differ because net-metered rooftop solar reduces retail sales billed
to customers; T&D losses appear in EIA-930 demand but not in retail sales.
Use Form 861 only to compute state-by-state fractions for each BA.
"""
from __future__ import annotations

import urllib.error
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

import pandas as pd

ROOT    = Path(__file__).resolve().parents[3]
RAW_DIR = ROOT / "data" / "raw" / "eia" / "form861"

_URL_PRIMARY = "https://www.eia.gov/electricity/data/eia861/zip/f861{year}.zip"
_URL_ARCHIVE = "https://www.eia.gov/electricity/data/eia861/archive/zip/f861{year}.zip"

# Positional column names for Sales_Ult_Cust_{year}.xlsx (skip 2 header rows)
_SALES_COLS = [
    "year", "utility_number", "utility_name", "part", "service_type", "data_type",
    "state", "ownership", "ba_code",
    "res_rev", "res_mwh", "res_cust",
    "com_rev", "com_mwh", "com_cust",
    "ind_rev", "ind_mwh", "ind_cust",
    "trn_rev", "trn_mwh", "trn_cust",
    "tot_rev", "tot_mwh", "tot_cust",
]

# Legacy paths for data downloaded before this module existed
# _LEGACY: dict[int, Path] = {
#     2024: ROOT / "data" / "raw" / "eiaForm861",
#     2023: ROOT / "data" / "raw" / "eiaForm8612023",
# }


def find_raw_dir(year: int) -> Path | None:
    """Return the directory containing Sales_Ult_Cust_{year}.xlsx, or None."""
    for p in [RAW_DIR / str(year)]:#, _LEGACY.get(year, Path("/nonexistent"))
        if p.exists() and list(p.glob(f"Sales_Ult_Cust_{year}.xlsx")):
            return p
    return None


def download(year: int, overwrite: bool = False) -> Path:
    """
    Download and extract the EIA Form 861 ZIP for a given year.

    Only Sales_Ult_Cust_{year}.xlsx is retained from the ZIP.
    Skips download if data already exists locally (unless overwrite=True).

    Returns the directory containing the Sales_Ult_Cust file.
    """
    dest = RAW_DIR / str(year)
    existing = find_raw_dir(year)
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
            print(f"    HTTP {e.code} — trying archive URL ...")
        except Exception as e:
            print(f"    Error ({e}) — trying archive URL ...")

    if data is None:
        raise RuntimeError(
            f"Could not download Form 861 for {year}.\n"
            f"Please download manually from https://www.eia.gov/electricity/data/eia861/\n"
            f"and extract Sales_Ult_Cust_{year}.xlsx to {dest}"
        )

    with zipfile.ZipFile(BytesIO(data)) as zf:
        sales_members = [m for m in zf.namelist() if "Sales_Ult_Cust" in m]
        targets = sales_members if sales_members else zf.namelist()
        for member in targets:
            zf.extract(member, dest)
        tag = "Sales_Ult_Cust only" if sales_members else "all files (Sales_Ult_Cust not found by name)"
        print(f"  Extracted {len(targets)} file(s) to {dest.relative_to(ROOT)} ({tag})")

    return dest


def parse_sales_excel(year: int, raw_dir: Path | None = None) -> pd.DataFrame:
    """
    Parse Sales_Ult_Cust_{year}.xlsx and return retail sales by BA and state.

    Returns a DataFrame with columns:
      year | ba_code | state | total_mwh
    One row per (BA, state) combination, summed across all utilities.
    """
    if raw_dir is None:
        raw_dir = find_raw_dir(year)
    if raw_dir is None:
        raise FileNotFoundError(
            f"No Form 861 data found for {year}. "
            f"Run download({year}) first or place files in {RAW_DIR / year}"
        )

    sales_file = raw_dir / f"Sales_Ult_Cust_{year}.xlsx"
    if not sales_file.exists():
        matches = list(raw_dir.glob("Sales_Ult_Cust_*.xlsx"))
        if not matches:
            raise FileNotFoundError(f"Sales_Ult_Cust_{year}.xlsx not found in {raw_dir}")
        sales_file = matches[0]

    print(f"  Parsing {sales_file.name} ...")
    df = pd.read_excel(sales_file, skiprows=2, header=None, dtype=str)

    n_exp = len(_SALES_COLS)
    if len(df.columns) > n_exp:
        df = df.iloc[:, :n_exp]
    df.columns = _SALES_COLS[: len(df.columns)]

    df["tot_mwh"] = pd.to_numeric(df["tot_mwh"], errors="coerce")
    df["ba_code"] = df["ba_code"].str.strip().str.upper()
    df["state"]   = df["state"].str.strip().str.upper()
    df = df[df["tot_mwh"].notna() & df["ba_code"].ne("NAN") & df["ba_code"].ne("")].copy()

    result = (
        df.groupby(["ba_code", "state"], as_index=False)["tot_mwh"]
        .sum()
        .rename(columns={"tot_mwh": "total_mwh"})
    )
    result.insert(0, "year", year)
    return result
