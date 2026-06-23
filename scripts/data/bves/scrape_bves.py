"""
CLI to scrape BVES electricity data into data/raw/bves/.

TODO: No data source identified yet.
      See src/data/bves/bves_scraper.py for the template.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def main() -> None:
    print("BVES scraper not yet implemented.")
    print("See src/data/bves/bves_scraper.py for the template.")
    sys.exit(1)


if __name__ == "__main__":
    main()
