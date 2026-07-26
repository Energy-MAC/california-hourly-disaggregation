"""
Scrape the SMUD Points-of-Interconnection (POI) capacity heatmap.

Source: https://www.smud.org/heatmap/index.html
  A PowerGEM React/Leaflet app. Its substation data is a static JSON:
    https://www.smud.org/heatmap/WClusterTrLimSumJson.json
  (column definitions in ./configuration.json).

SMUD is a municipal utility (not one of the scraped IOUs), so its substations
are a known coverage gap in this project — Sacramento County shows many CATS
demand buses but no scraped substation. This heatmap is the SMUD-specific
substation list we were missing. NOTE: it is a TRANSMISSION POI heatmap — only
13 major SMUD substations (115/230 kV), not the full SMUD distribution network.

What each substation carries
----------------------------
  busname, busvolt (kV), busnum (PSS/E bus number), busarea (=SMUD), and
  per-scenario `trlim` = available interconnection capacity (MW) for the four
  study scenarios: Heavy Summer, Light Spring, Partial Peak Discharging,
  Charging.

Coordinates
-----------
  The JSON `lat`/`lon` are PSS/E model coordinates (a rotated/scaled frame,
  ~-4.09e6, -5.6e5), NOT WGS84. They are, however, cleanly affine-related to
  real geography: fitting a 2-D affine map from the model coords to the WGS84
  coordinates of 8 unambiguous 230 kV SMUD substations (from the CEC 2026
  reference, ca_substations_cec.csv) reproduces those anchors to a median of
  ~0.03 km (max ~0.1 km). We derive that transform at runtime and apply it to
  all 13 buses, so even substations without a clean CEC name (e.g. "STA. E")
  get an accurate coordinate. The per-run residual print is a built-in check —
  if it ever grows, the anchor names or the source projection changed.

Output
------
  data/raw/smud/smud_heatmap_substations.csv
    busname, busvolt_kv, busnum, busarea, latitude, longitude,
    cap_heavy_summer_mw, cap_light_spring_mw,
    cap_partial_peak_discharging_mw, cap_charging_mw,
    coord_source (= "smud_affine"), coord_resid_km (anchor fit residual, or "")

Usage
-----
  python scripts/data/smud/scrape_smud_heatmap.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = ROOT / "data" / "raw" / "smud"
CEC_FILE = ROOT / "data" / "processed" / "substation_misc" / "ca_substations_cec.csv"

BASE = "https://www.smud.org/heatmap/"
DATA_URL = BASE + "WClusterTrLimSumJson.json"

# Scenario title (as it appears in the JSON) -> output column suffix.
_SCENARIOS = {
    "Heavy Summer": "cap_heavy_summer_mw",
    "Light Spring": "cap_light_spring_mw",
    "Partial Peak Discharging": "cap_partial_peak_discharging_mw",
    "Charging (Enter negative MW values)": "cap_charging_mw",
}

# Georeferencing anchors: SMUD busname -> WGS84 (lat, lon) of the corresponding
# 230 kV substation in the CEC 2026 reference. Only unambiguous facilities are
# used (each is the sole 230 kV SMUD substation of that name in CEC).
_ANCHORS = {
    "CARMICAL": (38.643640, -121.329078),  # Carmichael
    "ELKGROVE": (38.372719, -121.353021),  # Elk Grove
    "FOOTHILL": (38.693503, -121.347318),  # Foothill - (SMUD)
    "HEDGE":    (38.508243, -121.355353),  # Hedge
    "LAKE":     (38.657123, -121.132404),  # Lake - (SMUD)
    "ORANGEVL": (38.684980, -121.266360),  # Orangevale
    "POCKET":   (38.489639, -121.470023),  # Pocket
    "RNCHSECO": (38.344368, -121.123556),  # Rancho Seco
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": BASE + "index.html"})
    return s


def _haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))


def fetch_buses(session: requests.Session) -> pd.DataFrame:
    """Return one row per unique SMUD bus with model coords + per-scenario capacity."""
    data = session.get(DATA_URL, timeout=60).json()["wcResults"]
    rows: dict[int, dict] = {}
    for scenario in data:
        col = _SCENARIOS.get(scenario["title"])
        for b in scenario["buses"]:
            r = rows.setdefault(b["busnum"], {
                "busname": b["busname"].strip(),
                "busvolt_kv": b["busvolt"],
                "busnum": b["busnum"],
                "busarea": b["busarea"].strip(),
                "xp": b["lon"], "yp": b["lat"],  # PSS/E model coords
            })
            if col:
                r[col] = b["trlim"]
    return pd.DataFrame(rows.values())


def georeference(df: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Fit an affine map (model coords -> WGS84) from the anchors, apply to all.

    Returns (df with latitude/longitude, max anchor residual km).
    """
    A = df[df.busname.isin(_ANCHORS) & (df.busvolt_kv == 230)].drop_duplicates("busname").copy()
    A["alat"] = A.busname.map(lambda n: _ANCHORS[n][0])
    A["alon"] = A.busname.map(lambda n: _ANCHORS[n][1])

    M = np.column_stack([A.xp, A.yp, np.ones(len(A))])
    clon = np.linalg.lstsq(M, A.alon, rcond=None)[0]
    clat = np.linalg.lstsq(M, A.alat, rcond=None)[0]

    resid = _haversine_km(A.alat, A.alon, M @ clat, M @ clon)
    max_resid = float(resid.max())
    print(f"  Georeferencing: {len(A)} anchors, residual median "
          f"{np.median(resid):.3f} km, max {max_resid:.3f} km")

    P = np.column_stack([df.xp, df.yp, np.ones(len(df))])
    df = df.copy()
    df["latitude"] = P @ clat
    df["longitude"] = P @ clon
    df["coord_source"] = "smud_affine"
    df["coord_resid_km"] = round(max_resid, 3)
    return df, max_resid


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    session = _session()

    print(f"Fetching SMUD POI heatmap data from {DATA_URL} ...")
    df = fetch_buses(session)
    print(f"  {len(df)} unique SMUD substations "
          f"({(df.busvolt_kv == 230).sum()} @230kV, {(df.busvolt_kv == 115).sum()} @115kV)")

    df, _ = georeference(df)

    cols = ["busname", "busvolt_kv", "busnum", "busarea", "latitude", "longitude",
            *(_SCENARIOS.values()), "coord_source", "coord_resid_km"]
    for c in cols:
        if c not in df:
            df[c] = np.nan
    df = df[cols].sort_values(["busvolt_kv", "busname"], ascending=[False, True])

    out = OUT_DIR / "smud_heatmap_substations.csv"
    df.to_csv(out, index=False)
    print(f"\nWrote {len(df)} substations -> {out.relative_to(ROOT)}")
    print(df[["busname", "busvolt_kv", "latitude", "longitude",
              "cap_heavy_summer_mw"]].round(4).to_string(index=False))


if __name__ == "__main__":
    main()
