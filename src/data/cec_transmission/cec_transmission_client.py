"""CEC California Electric Transmission Lines ArcGIS FeatureServer client."""
from __future__ import annotations

from src.data.arcgis_client import ArcGISClient

# Source: California Energy Commission via ArcGIS Online
# Service description: "California Electric Transmission Lines"
# Discovered 2026-06-25 by running discover_service() against the endpoint.
_FEATURESERVER_URL = (
    "https://services3.arcgis.com/bWPjFyq029ChCGur/arcgis/rest/services"
    "/Transmission_Line/FeatureServer"
)

LAYER_ID = 2  # TransmissionLine_CEC — the only layer in this service


class CECTransmissionClient(ArcGISClient):
    def __init__(self) -> None:
        super().__init__(_FEATURESERVER_URL)
