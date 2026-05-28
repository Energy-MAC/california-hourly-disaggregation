"""
EIA dataset scrapers.

Each public scrape_* function in this module:
  - Accepts start/end dates and dataset-specific filter parameters
  - Pages through the EIA API and writes results to chunked CSVs
  - Returns a list of Paths to the written files

Output file naming convention
------------------------------
    {prefix}_{start}_{end}_part{chunk:03d}.csv

Where `prefix` encodes source, dataset, and key facets:

    eia_{dataset-slug}_{facet-tag}

Examples:
    eia_rto-region-data_CALI_20200101_20241231_part001.csv
    eia_rto-region-data_CALI-PACE_20200101_20241231_part001.csv
    eia_rto-region-data_ALL_20200101_20241231_part001.csv

The prefix can be overridden with the `filename_prefix` parameter on any
scrape_* function so you can add run-specific labels without changing the
date/chunk suffix.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Generator, Optional

from src.data.eia_client import EIAClient

# Canonical location for raw source data within the repo
DATA_RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"

_DEFAULT_MAX_FILE_MB = 100.0


# ── Filename helpers ──────────────────────────────────────────────────────────

def build_filename(prefix: str, start: str, end: str, chunk: int) -> str:
    """
    Build a CSV filename from a descriptive prefix, date range, and chunk index.

    Convention:
        {prefix}_{YYYYMMDD_start}_{YYYYMMDD_end}_part{chunk:03d}.csv

    Parameters
    ----------
    prefix : str
        Encodes source + dataset + key facets, e.g. "eia_rto-region-data_CALI".
    start, end : str
        Date strings in YYYY-MM-DD format. Hyphens are stripped in the filename.
    chunk : int
        1-based chunk index. Zero-padded to 3 digits for natural sort order.

    Returns
    -------
    str
        e.g. "eia_rto-region-data_CALI_20200101_20241231_part001.csv"
    """
    s = start.replace("-", "")
    e = end.replace("-", "")
    return f"{prefix}_{s}_{e}_part{chunk:03d}.csv"


# ── Generic CSV writer ────────────────────────────────────────────────────────

def _pages_to_csv(
    page_iter: Generator,
    output_dir: Path,
    prefix: str,
    start: str,
    end: str,
    max_file_mb: float,
) -> list[Path]:
    """
    Consume a paginated iterator of (rows, total) tuples and write to chunked CSVs.

    Rotates to a new file whenever the current CSV reaches max_file_mb.
    Each chunk gets its own header row so every file is independently readable.

    Parameters
    ----------
    page_iter : Generator
        Yields (list[dict], total_int) — typically from EIAClient.paginate().
    output_dir : Path
        Directory to write files. Created if absent.
    prefix, start, end : str
        Passed to build_filename().
    max_file_mb : float
        File size ceiling in megabytes.

    Returns
    -------
    list[Path]
        Paths to all written CSV files, in order.
    """
    max_bytes = max_file_mb * 1024 * 1024
    output_dir.mkdir(parents=True, exist_ok=True)

    written_files: list[Path] = []
    fieldnames: Optional[list[str]] = None

    chunk = 1
    current_path: Optional[Path] = None
    current_file = None
    current_writer: Optional[csv.DictWriter] = None
    rows_in_chunk = 0
    total_rows = 0

    def _open_chunk() -> None:
        nonlocal current_path, current_file, current_writer, rows_in_chunk, chunk
        if current_file and not current_file.closed:
            current_file.close()
            print(f"\n  Closed: {current_path.name}  ({rows_in_chunk:,} rows)")
        fname = build_filename(prefix, start, end, chunk)
        current_path = output_dir / fname
        current_file = open(current_path, "w", newline="", encoding="utf-8")
        written_files.append(current_path)
        current_writer = None   # recreated once we have fieldnames for this chunk
        rows_in_chunk = 0
        chunk += 1

    _open_chunk()

    try:
        for page_rows, total in page_iter:
            if not page_rows:
                break

            # Capture column names from the first row we ever see
            if fieldnames is None:
                fieldnames = list(page_rows[0].keys())

            # Open (or re-open after a chunk rotation) the DictWriter
            if current_writer is None:
                current_writer = csv.DictWriter(current_file, fieldnames=fieldnames)
                current_writer.writeheader()

            current_writer.writerows(page_rows)
            rows_in_chunk += len(page_rows)
            total_rows += len(page_rows)
            current_file.flush()

            pct = total_rows / total * 100 if total else 0.0
            size_mb = current_path.stat().st_size / 1024 / 1024
            print(
                f"  {total_rows:,}/{total:,} rows  ({pct:.1f}%)  |  "
                f"chunk {chunk - 1}: {size_mb:.1f} / {max_file_mb} MB",
                end="\r",
            )

            if current_path.stat().st_size >= max_bytes:
                _open_chunk()

    finally:
        if current_file and not current_file.closed:
            current_file.close()
            print(f"\n  Closed: {current_path.name}  ({rows_in_chunk:,} rows)")

    print(f"Done.  {total_rows:,} total rows across {len(written_files)} file(s).")
    return written_files


# ── Dataset-specific scrapers ─────────────────────────────────────────────────

def scrape_rto_region_data(
    start: str,
    end: str,
    respondents: Optional[list[str]] = None,
    output_dir: Path = DATA_RAW_DIR,
    filename_prefix: Optional[str] = None,
    max_file_mb: float = _DEFAULT_MAX_FILE_MB,
    api_key: Optional[str] = None,
    page_size: int = 5000,
) -> list[Path]:
    """
    Scrape EIA hourly RTO region-level electricity data.

    EIA endpoint: /electricity/rto/region-data/data/

    Returned columns include: period, respondent, respondent-name,
    type (D=Demand / NG=Net Generation / TI=Total Interchange / DF=Demand Forecast),
    value, value-units.

    Parameters
    ----------
    start : str
        Start date, YYYY-MM-DD (inclusive).
    end : str
        End date, YYYY-MM-DD (inclusive).
    respondents : list[str] | None
        EIA balancing authority codes. None fetches all regions.
        Common CA-region codes: CALI, PACW, PACE, NEVP, WALC
    output_dir : Path
        Directory to write CSV files. Created if absent.
    filename_prefix : str | None
        Override the auto-generated prefix.
        Auto format: eia_rto-region-data_{respondent_tag}
        Final filename: {prefix}_{start}_{end}_part001.csv
    max_file_mb : float
        Rotate to a new file when the current CSV exceeds this size (MB).
    api_key : str | None
        EIA API key. Falls back to EIA_API_KEY env var / .env file.
    page_size : int
        Rows per API request. EIA maximum is 5000.

    Returns
    -------
    list[Path]
        Paths to every written CSV file.
    """
    respondents = respondents or []
    facet_tag = "-".join(respondents) if respondents else "ALL"
    prefix = filename_prefix or f"eia_rto-region-data_{facet_tag}"

    params: dict = {
        "frequency": "hourly",
        "data[0]": "value",
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
        "start": start,
        "end": end,
    }
    for i, code in enumerate(respondents):
        params[f"facets[respondent][{i}]"] = code

    print(f"\nScraping EIA rto-region-data")
    print(f"  respondents : {facet_tag}")
    print(f"  date range  : {start} → {end}")
    print(f"  output dir  : {output_dir}")
    print(f"  max file    : {max_file_mb} MB\n")

    client = EIAClient(api_key=api_key)
    pages = client.paginate("electricity/rto/region-data/data/", params, page_size)
    return _pages_to_csv(pages, Path(output_dir), prefix, start, end, max_file_mb)
