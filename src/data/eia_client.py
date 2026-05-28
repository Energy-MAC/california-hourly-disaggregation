"""
Low-level EIA Open Data API v2 client.

Handles authentication, pagination, retries, and rate limiting.
All higher-level dataset logic lives in eia_scraper.py.
"""
from __future__ import annotations

import os
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

_BASE_URL = "https://api.eia.gov/v2"
_DEFAULT_PAGE_SIZE = 5000
_REQUEST_DELAY = 0.2  # seconds between paginated requests to stay well under rate limits


class EIAClient:
    """Thin wrapper around the EIA Open Data API v2."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("EIA_API_KEY")
        if not self.api_key:
            raise ValueError(
                "No EIA API key found. "
                "Set EIA_API_KEY in your .env file or pass api_key= to EIAClient()."
            )
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=5,
            backoff_factor=1.5,
            status_forcelist={429, 500, 502, 503, 504},
            allowed_methods={"GET"},
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    def _fetch_page(self, endpoint: str, params: dict, offset: int, length: int) -> dict:
        url = f"{_BASE_URL}/{endpoint.lstrip('/')}"
        response = self.session.get(
            url,
            params={"api_key": self.api_key, "offset": offset, "length": length, **params},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def paginate(
        self,
        endpoint: str,
        params: dict,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ):
        """
        Yield (rows, total_count) for each API page until all records are fetched.

        Parameters
        ----------
        endpoint : str
            EIA API v2 path, e.g. "electricity/rto/region-data/data/"
        params : dict
            Query parameters (excluding api_key, offset, length).
        page_size : int
            Rows per request. EIA maximum is 5000.

        Yields
        ------
        tuple[list[dict], int]
            (page_rows, total_record_count)
        """
        offset = 0
        total: int | None = None
        while True:
            payload = self._fetch_page(endpoint, params, offset, page_size)
            rows: list[dict] = payload["response"]["data"]
            if total is None:
                total = int(payload["response"]["total"])
            yield rows, total
            offset += len(rows)
            if not rows or offset >= total:
                break
            time.sleep(_REQUEST_DELAY)
