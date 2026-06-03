"""
Generic ArcGIS FeatureServer REST client.

Handles session management, retries, feature flattening, and pagination.
Not utility-specific — instantiate with any public FeatureServer URL.
Used by PGEClient and SCEClient (and any future utility ArcGIS scrapers).
"""
from __future__ import annotations

import json
import time
from typing import Generator, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_REQUEST_DELAY = 0.3
_DEFAULT_PAGE_SIZE = 1000


def _flatten_feature(feature: dict) -> dict:
    """
    Flatten an ArcGIS feature into a plain dict suitable for CSV.

    Point geometry        → longitude / latitude columns.
    Polygon centroid      → longitude / latitude (from returnCentroid=true).
    Other geometry types  → serialized JSON string in a 'geometry' column.
    """
    row = dict(feature.get("attributes") or {})

    # Polygon centroid takes priority (returned when returnCentroid=true)
    centroid = feature.get("centroid")
    if centroid and "x" in centroid and "y" in centroid:
        row["longitude"] = centroid["x"]
        row["latitude"] = centroid["y"]
        return row

    geom = feature.get("geometry")
    if geom:
        if "x" in geom and "y" in geom:
            row["longitude"] = geom["x"]
            row["latitude"] = geom["y"]
        else:
            row["geometry"] = json.dumps(geom)
    return row


class ArcGISClient:
    """
    Thin wrapper around an ArcGIS FeatureServer REST endpoint.

    Parameters
    ----------
    featureserver_url : str
        Base URL of the FeatureServer, e.g.
        "https://services5.arcgis.com/.../arcgis/rest/services/ICA_Tables/FeatureServer"
    """

    def __init__(self, featureserver_url: str) -> None:
        self.base_url = featureserver_url.rstrip("/")
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

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self.base_url}{path}"
        r = self.session.get(url, params={"f": "json", **params}, timeout=60)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(
                f"ArcGIS error {data['error'].get('code', '?')}: "
                f"{data['error'].get('message', data['error'])}"
            )
        return data

    def get_service_info(self) -> dict:
        """Return FeatureServer metadata, including the full layers list."""
        return self._get("", {})

    def get_layer_info(self, layer_id: int) -> dict:
        """Return layer metadata, including field definitions."""
        return self._get(f"/{layer_id}", {})

    def get_record_count(self, layer_id: int, where: str = "1=1") -> int:
        """Return the total number of features matching the WHERE clause."""
        data = self._get(f"/{layer_id}/query", {
            "where": where,
            "returnCountOnly": "true",
        })
        return int(data["count"])

    def build_coordinate_lookup(
        self,
        layer_id: int,
        name_field: str,
        use_centroid: bool = False,
        where: str = "1=1",
    ) -> dict:
        """
        Build a {name: (longitude, latitude)} lookup from a spatial layer.

        Parameters
        ----------
        layer_id : int
            Layer with point or polygon geometry.
        name_field : str
            Field used as the lookup key (e.g. "SubstationName").
        use_centroid : bool
            True for polygon layers — requests centroid via returnCentroid=true
            instead of the full polygon geometry.
        where : str
            Optional filter clause.

        Returns
        -------
        dict
            {name_value: (longitude, latitude)}
        """
        lookup: dict = {}
        for rows, _ in self.paginate_layer(
            layer_id,
            out_fields=name_field,
            include_geometry=not use_centroid,
            return_centroid=use_centroid,
            where=where,
        ):
            for row in rows:
                name = row.get(name_field)
                lon = row.get("longitude")
                lat = row.get("latitude")
                if name and lon is not None and lat is not None:
                    lookup[name] = (float(lon), float(lat))
        return lookup

    def paginate_layer(
        self,
        layer_id: int,
        where: str = "1=1",
        out_fields: str = "*",
        order_by: str = "OBJECTID",
        page_size: int = _DEFAULT_PAGE_SIZE,
        start_offset: int = 0,
        include_geometry: bool = True,
        out_sr: int = 4326,
        return_centroid: bool = False,
    ) -> Generator:
        """
        Yield (rows, total_count) for each page of features from a layer.

        Rows are flat dicts — attributes plus optional flattened geometry columns.

        Parameters
        ----------
        layer_id : int
            FeatureServer layer index.
        where : str
            SQL WHERE clause. "1=1" = all records.
        out_fields : str
            Comma-separated field names, or "*" for all.
        order_by : str
            Field used for deterministic pagination across pages.
        page_size : int
            Features per request. Many servers cap at 1000–2000.
        start_offset : int
            Record offset to begin from (non-zero when resuming).
        include_geometry : bool
            Whether to flatten geometry into longitude/latitude columns.
        out_sr : int
            Output spatial reference WKID. Default 4326 (WGS84) gives
            standard longitude/latitude values directly.
        return_centroid : bool
            Return polygon centroid instead of full geometry. Useful for
            polygon layers when only the centre-point is needed.

        Yields
        ------
        tuple[list[dict], int]
            (page_rows, total_record_count)
        """
        offset = start_offset
        total: Optional[int] = None

        while True:
            params: dict = {
                "where": where,
                "outFields": out_fields,
                "resultOffset": offset,
                "resultRecordCount": page_size,
                "orderByFields": order_by,
                "returnGeometry": "true" if include_geometry else "false",
                "returnCentroid": "true" if return_centroid else "false",
            }
            if include_geometry or return_centroid:
                params["outSR"] = out_sr
            data = self._get(f"/{layer_id}/query", params)

            features = data.get("features", [])
            rows = [_flatten_feature(f) for f in features]

            if total is None:
                total = self.get_record_count(layer_id, where)

            yield rows, total

            offset += len(rows)
            if not features or offset >= total:
                break

            time.sleep(_REQUEST_DELAY)
