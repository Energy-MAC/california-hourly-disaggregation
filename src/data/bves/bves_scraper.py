"""
BVES (Bear Valley Electric Service) dataset scrapers.

TODO: No data source identified yet. Implement once a source is found,
      following the pattern in src/data/pacificorp/pacificorp_scraper.py.

Template outline:
-----------------
    from src.data.scraper_utils import DEFAULT_MAX_FILE_MB, load_progress, pages_to_csv

    DATA_RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "bves"

    def scrape_<dataset>(...) -> list[Path]:
        ...
"""
from __future__ import annotations

from pathlib import Path

DATA_RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "bves"
