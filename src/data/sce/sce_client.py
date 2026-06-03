"""SCE ICA Tables ArcGIS FeatureServer client (no authentication required)."""
from __future__ import annotations

from src.data.arcgis_client import ArcGISClient

_FEATURESERVER_URL = (
    "https://services5.arcgis.com/z6hI6KRjKHvhNO0r/arcgis/rest/services"
    "/ICA_Tables/FeatureServer"
)


class SCEClient(ArcGISClient):
    def __init__(self) -> None:
        super().__init__(_FEATURESERVER_URL)
