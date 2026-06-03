"""
SDG&E ArcGIS FeatureServer client (no authentication required for public services).

SDG&E hosts ~26 separate FeatureServer services under one ArcGIS organization.
Use list_services() to enumerate them, then instantiate SDGEClient(service_name)
to query a specific service.
"""
from __future__ import annotations

import requests

from src.data.arcgis_client import ArcGISClient

_ORG_ID = "S0EUI1eVapjRPS5e"
_SERVICES_BASE = f"https://services.arcgis.com/{_ORG_ID}/arcgis/rest/services"
_FEATURESERVER_TMPL = f"{_SERVICES_BASE}/{{name}}/FeatureServer"


def list_services() -> list[dict]:
    """
    Return all services available in the SDG&E ArcGIS organization.

    Each entry has at minimum: 'name', 'type', 'url'.
    """
    r = requests.get(_SERVICES_BASE, params={"f": "json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(
            f"ArcGIS error {data['error'].get('code', '?')}: "
            f"{data['error'].get('message', data['error'])}"
        )
    return data.get("services", [])


class SDGEClient(ArcGISClient):
    """
    ArcGIS client pointed at a specific SDG&E FeatureServer service.

    Parameters
    ----------
    service_name : str
        Name of the FeatureServer service (from list_services()).
        e.g. "ICA_MAP_PROD_Substations_VW"
    """

    def __init__(self, service_name: str) -> None:
        super().__init__(_FEATURESERVER_TMPL.format(name=service_name))
        self.service_name = service_name
