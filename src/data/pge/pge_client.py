"""PG&E DRP Compliance ArcGIS FeatureServer client (no authentication required)."""
from __future__ import annotations

from src.data.arcgis_client import ArcGISClient

_FEATURESERVER_URL = (
    "https://services2.arcgis.com/mJaJSax0KPHoCNB6/arcgis/rest/services"
    "/DRPComplianceRelProd/FeatureServer"
)


class PGEClient(ArcGISClient):
    def __init__(self) -> None:
        super().__init__(_FEATURESERVER_URL)
