"""
CLI to scrape CalPeco electricity data into data/raw/calpeco/.

TODO: No public data source identified yet for CalPeco (Liberty Utilities).
      See src/data/calpeco/calpeco_scraper.py for notes on finding a source.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def main() -> None:
    print("CalPeco scraper not yet implemented — no public data source identified.")
    print("See src/data/calpeco/calpeco_scraper.py for details.")
    sys.exit(1)


if __name__ == "__main__":
    main()
