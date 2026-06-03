"""PacifiCorp Transmission and Distribution ArcGIS FeatureServer client (public, no auth)."""
from __future__ import annotations

from src.data.arcgis_client import ArcGISClient

_FEATURESERVER_URL = (
    "https://services1.arcgis.com/ePo6UhbBpZFy1wO2/arcgis/rest/services"
    "/Transmission_and_Distribution_Public/FeatureServer"
)


class PacifiCorpClient(ArcGISClient):
    def __init__(self) -> None:
        super().__init__(_FEATURESERVER_URL)
