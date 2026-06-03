"""
CalPeco (Liberty Utilities - California Pacific Electric) dataset scrapers.

TODO: No public ArcGIS or REST API data source has been identified for CalPeco.
      The ArcGIS Experience Builder app at:
        https://experience.arcgis.com/experience/6f422654851e4d858852fa00d1905170
      contains only PacifiCorp data — CalPeco is a separate utility (Liberty Utilities).

      Possible next steps:
      - Contact Liberty Utilities / CalPeco directly for GIS data
      - Check CPUC regulatory filings for service territory / substation data
      - Look for a Liberty Utilities open data portal

Template outline (once a data source is found):
------------------------------------------------
    from src.data.scraper_utils import DEFAULT_MAX_FILE_MB, load_progress, pages_to_csv

    DATA_RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "calpeco"

    def scrape_<dataset>(...) -> list[Path]:
        ...
"""
from __future__ import annotations

from pathlib import Path

DATA_RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "calpeco"
