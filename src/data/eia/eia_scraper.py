"""
EIA dataset scrapers.

Each public scrape_* function in this module:
  - Accepts start/end dates and dataset-specific filter parameters
  - Pages through the EIA API and writes results to chunked CSVs
  - Returns a list of Paths to the written files

Output goes to data/raw/eia/ by default.

Output file naming convention
------------------------------
    {prefix}_{start}_{end}_part{chunk:03d}.csv

Examples:
    eia_rto-region-data_CAL_earliest_latest_part001.csv
    eia_rto-interchange-data_from-CALI_20200101_20241231_part001.csv
    eia_rto-interchange-data_ALL_earliest_latest_part001.csv
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.data.eia.eia_client import EIAClient
from src.data.scraper_utils import DEFAULT_MAX_FILE_MB, load_progress, pages_to_csv

DATA_RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "eia"


# ── Dataset-specific scrapers ─────────────────────────────────────────────────

def scrape_rto_region_data(
    start: Optional[str] = None,
    end: Optional[str] = None,
    respondents: Optional[list[str]] = None,
    output_dir: Path = DATA_RAW_DIR,
    filename_prefix: Optional[str] = None,
    max_file_mb: float = DEFAULT_MAX_FILE_MB,
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
    start : str | None
        Start date, YYYY-MM-DD (inclusive). None = earliest available.
    end : str | None
        End date, YYYY-MM-DD (inclusive). None = most recent available.
    respondents : list[str] | None
        EIA balancing authority / region codes. None fetches all regions.
        CA region aggregate: CAL
        Individual CA BAs: CALI, PACW, PACE, NEVP, WALC, LDWP, BANC, IID, TID
    output_dir : Path
        Directory to write CSV files. Created if absent.
    filename_prefix : str | None
        Override the auto-generated prefix.
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
    output_dir = Path(output_dir)

    resume = load_progress(output_dir, prefix)
    start_offset = resume["offset"] if resume else 0

    params: dict = {
        "frequency": "hourly",
        "data[0]": "value",
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
    }
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    for i, code in enumerate(respondents):
        params[f"facets[respondent][{i}]"] = code

    if resume:
        print(f"\nResuming EIA rto-region-data from row {start_offset:,}")
    else:
        print(f"\nScraping EIA rto-region-data")
    print(f"  respondents : {facet_tag}")
    print(f"  date range  : {start or 'earliest'} → {end or 'latest'}")
    print(f"  output dir  : {output_dir}")
    print(f"  max file    : {max_file_mb} MB\n")

    client = EIAClient(api_key=api_key)
    pager = client.paginate("electricity/rto/region-data/data/", params, page_size, start_offset=start_offset)
    return pages_to_csv(pager, output_dir, prefix, start, end, max_file_mb, resume=resume)


def scrape_rto_interchange_data(
    start: Optional[str] = None,
    end: Optional[str] = None,
    from_bas: Optional[list[str]] = None,
    to_bas: Optional[list[str]] = None,
    output_dir: Path = DATA_RAW_DIR,
    filename_prefix: Optional[str] = None,
    max_file_mb: float = DEFAULT_MAX_FILE_MB,
    api_key: Optional[str] = None,
    page_size: int = 5000,
) -> list[Path]:
    """
    Scrape EIA hourly RTO interchange data (flows between balancing authorities).

    EIA endpoint: /electricity/rto/interchange-data/data/

    Returned columns include: period, fromba, fromba-name, toba, toba-name,
    value (MW), value-units.

    Parameters
    ----------
    start : str | None
        Start date, YYYY-MM-DD (inclusive). None = earliest available.
    end : str | None
        End date, YYYY-MM-DD (inclusive). None = most recent available.
    from_bas : list[str] | None
        Filter by originating balancing authority codes (e.g. ["CALI"]).
        None fetches all.
    to_bas : list[str] | None
        Filter by destination balancing authority codes. None fetches all.
    output_dir : Path
        Directory to write CSV files. Created if absent.
    filename_prefix : str | None
        Override the auto-generated prefix.
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
    from_bas = from_bas or []
    to_bas = to_bas or []

    parts = []
    if from_bas:
        parts.append("from-" + "-".join(from_bas))
    if to_bas:
        parts.append("to-" + "-".join(to_bas))
    facet_tag = "_".join(parts) if parts else "ALL"
    prefix = filename_prefix or f"eia_rto-interchange-data_{facet_tag}"
    output_dir = Path(output_dir)

    resume = load_progress(output_dir, prefix)
    start_offset = resume["offset"] if resume else 0

    params: dict = {
        "frequency": "hourly",
        "data[0]": "value",
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
    }
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    for i, code in enumerate(from_bas):
        params[f"facets[fromba][{i}]"] = code
    for i, code in enumerate(to_bas):
        params[f"facets[toba][{i}]"] = code

    if resume:
        print(f"\nResuming EIA rto-interchange-data from row {start_offset:,}")
    else:
        print(f"\nScraping EIA rto-interchange-data")
    print(f"  from_bas    : {from_bas or 'ALL'}")
    print(f"  to_bas      : {to_bas or 'ALL'}")
    print(f"  date range  : {start or 'earliest'} → {end or 'latest'}")
    print(f"  output dir  : {output_dir}")
    print(f"  max file    : {max_file_mb} MB\n")

    client = EIAClient(api_key=api_key)
    pager = client.paginate("electricity/rto/interchange-data/data/", params, page_size, start_offset=start_offset)
    return pages_to_csv(pager, output_dir, prefix, start, end, max_file_mb, resume=resume)
