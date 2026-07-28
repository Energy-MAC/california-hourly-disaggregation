"""
Scrape the CAISO Points-of-Interconnection (POI) capacity heatmap.

Source: https://www.caiso.com/poi-heatmap/
  Same PowerGEM React/Leaflet app as the SMUD heatmap
  (scripts/data/smud/scrape_smud_heatmap.py) -- identical static-JSON
  endpoint name and bus schema, but CAISO-wide instead of one municipal
  utility:
    https://www.caiso.com/poi-heatmap/WClusterTrLimSumJson.json

Unlike SMUD's four operating-condition scenarios (Heavy Summer, Light
Spring, ...), CAISO's three "scenarios" are independent interconnection
queue cluster studies (C15_Reassessment, 2025_TPD, Cluster15) -- different
bus universes (~90%+ overlap by busnum) rather than the same buses under
different conditions. Pivoted the same way as SMUD: one row per busnum,
one trlim column per scenario (NaN where a bus is absent from that study).

Coordinates
-----------
  The JSON `lat`/`lon` are PSS/E model coordinates in the SAME frame as
  SMUD's heatmap (confirmed: SMUD's Sacramento-only 8-anchor affine, tested
  against a San Francisco CAISO bus, is off by ~35 km -- a locally-fit
  affine does NOT generalize statewide; the underlying projection has real
  curvature over California's ~1000 km extent). This script instead builds
  anchors across the WHOLE state: CAISO busnames are plain substation names
  ("BAKERSFIELD 230 kV") once the voltage suffix is stripped, so they can be
  matched directly against the CEC substation reference
  (ca_substations_cec.csv) by normalized name + utility (PGAE/SCE/SDGE),
  keeping only unambiguous 1:1 matches (391 anchors). A first-pass fit is
  dominated by a handful of far-flung outliers (north coast anchors up to
  ~30 km off); anchors past `_TRIM_RESID_KM` (20 km) are dropped and the
  affine is refit once on the remainder (384 anchors) -- median residual
  ~4.4 km, p90 ~8.7 km, max ~19.8 km (vs SMUD's single-region 0.03 km).
  Buses whose name didn't match any CEC anchor (extrapolated, not
  interpolated) can be noticeably worse -- e.g. the oddly-named "SAN FRAN A
  (POTRERO PP)" bus lands ~30 km off. coord_resid_km (the final fit's max)
  is printed each run and stored per-row as a blanket caveat flag.

Output
------
  data/raw/caiso/caiso_heatmap_substations.csv
    busname, busvolt_kv, busnum, pto, region, latitude, longitude,
    trlim_c15_reassessment_mw, trlim_2025_tpd_mw, trlim_cluster15_mw,
    coord_source (= "caiso_affine"), coord_resid_km (anchor fit max residual)

Usage
-----
  python scripts/data/caiso/scrape_caiso_heatmap.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = ROOT / "data" / "raw" / "caiso"
CEC_FILE = ROOT / "data" / "processed" / "substation_misc" / "ca_substations_cec.csv"

sys.path.insert(0, str(ROOT / "scripts" / "data" / "substations"))
from build_cec_name_dictionary import norm, norm_base  # noqa: E402

BASE = "https://www.caiso.com/poi-heatmap/"
DATA_URL = BASE + "WClusterTrLimSumJson.json"

# Scenario title (as it appears in the JSON) -> output column suffix.
_SCENARIO_COL = {
    "C15_Reassessment": "trlim_c15_reassessment_mw",
    "2025_TPD": "trlim_2025_tpd_mw",
    "Cluster15": "trlim_cluster15_mw",
}

# CAISO "pto" (participating transmission owner) -> CEC owner_std, for the
# three IOUs this project scopes to. Other POI ptos (APS, DCRT, GWT, LSPC,
# MWD, VEA) are kept in the output but not used as anchor candidates.
_PTO_TO_CEC_OWNER = {"PGAE": "pge", "SCE": "sce", "SDGE": "sdge"}

_VOLT_SUFFIX_RE = re.compile(r"\s*\d+(\.\d+)?\s*k[Vv]\s*$")


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
    """Return one row per unique CAISO bus with model coords + per-scenario trlim."""
    data = session.get(DATA_URL, timeout=60).json()["wcResults"]
    rows: dict[int, dict] = {}
    for scenario in data:
        col = _SCENARIO_COL.get(scenario["title"])
        for b in scenario["buses"]:
            r = rows.setdefault(b["busnum"], {
                "busname": b["busname"].strip(),
                "busvolt_kv": b["busvolt"],
                "busnum": b["busnum"],
                "pto": (b.get("pto") or "").strip(),
                "region": (b.get("region") or "").strip(),
                "xp": b["lon"], "yp": b["lat"],  # PSS/E model coords
            })
            if col:
                r[col] = b["trlim"]
    return pd.DataFrame(rows.values())


def build_anchors(df: pd.DataFrame) -> pd.DataFrame:
    """CAISO busname (voltage suffix stripped) <-> CEC substation name,
    matched within the same utility, unambiguous 1:1 normalized-name matches
    only (both sides deduplicated by (name, owner) before intersecting) --
    conservative on purpose, since a bad anchor corrupts the whole statewide
    fit rather than just one substation."""
    cec = pd.read_csv(CEC_FILE, usecols=["name", "owner_std", "latitude", "longitude"])
    cec = cec.dropna(subset=["latitude", "longitude"]).copy()
    cec["norm_name"] = cec["name"].map(norm_base)

    cand = df.copy()
    cand["base_name"] = cand["busname"].apply(lambda s: _VOLT_SUFFIX_RE.sub("", s).strip())
    cand["norm_name"] = cand["base_name"].map(norm)
    cand["cec_owner"] = cand["pto"].map(_PTO_TO_CEC_OWNER)
    cand = cand.dropna(subset=["cec_owner"])
    cand = cand[cand["norm_name"] != ""]

    cec_counts = cec.groupby(["norm_name", "owner_std"]).size()
    unambig_cec = set(cec_counts[cec_counts == 1].index)
    bus_counts = cand.groupby(["norm_name", "cec_owner"]).size()
    unambig_bus = set(bus_counts[bus_counts == 1].index)
    keys = unambig_cec & unambig_bus

    cec_lookup = cec.set_index(["norm_name", "owner_std"])[["latitude", "longitude"]].sort_index()
    bus_lookup = cand.set_index(["norm_name", "cec_owner"]).sort_index()

    anchors = []
    for name, owner in keys:
        c = cec_lookup.loc[(name, owner)]
        c = c.iloc[0] if isinstance(c, pd.DataFrame) else c
        b = bus_lookup.loc[(name, owner)]
        b = b.iloc[0] if isinstance(b, pd.DataFrame) else b
        anchors.append({"busname": b["busname"], "xp": b["xp"], "yp": b["yp"],
                        "alat": c["latitude"], "alon": c["longitude"]})
    out = pd.DataFrame(anchors)
    return out.astype({"xp": "float64", "yp": "float64", "alat": "float64", "alon": "float64"})


_TRIM_RESID_KM = 20.0  # drop anchors past this before the final fit (see docstring)


def _fit_affine(anchors: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    M = np.column_stack([anchors.xp, anchors.yp, np.ones(len(anchors))])
    clon = np.linalg.lstsq(M, anchors.alon, rcond=None)[0]
    clat = np.linalg.lstsq(M, anchors.alat, rcond=None)[0]
    resid = _haversine_km(anchors.alat, anchors.alon, M @ clat, M @ clon)
    return clon, clat, resid


def georeference(df: pd.DataFrame, anchors: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Fit one statewide affine map (model coords -> WGS84) from the
    matched anchors, apply to every bus. A first pass over the full
    statewide anchor set is dominated by a handful of far-flung outliers
    (e.g. the north coast, ~150 km+ from the anchor centroid) that skew
    the least-squares fit for everyone else; anchors past
    `_TRIM_RESID_KM` are dropped and the affine is refit once on the
    remainder. Returns (df with lat/lon, final max anchor residual km)."""
    _, _, resid0 = _fit_affine(anchors)
    keep = resid0 <= _TRIM_RESID_KM
    n_dropped = int((~keep).sum())
    trimmed = anchors[keep]
    clon, clat, resid = _fit_affine(trimmed)

    max_resid = float(resid.max())
    print(f"  Georeferencing: {len(anchors)} anchors ({n_dropped} dropped as outliers "
          f"> {_TRIM_RESID_KM:.0f} km), {len(trimmed)} used for the final fit")
    print(f"    residual median {np.median(resid):.3f} km, "
          f"p90 {np.percentile(resid, 90):.3f} km, max {max_resid:.3f} km")

    P = np.column_stack([df.xp, df.yp, np.ones(len(df))])
    df = df.copy()
    df["latitude"] = P @ clat
    df["longitude"] = P @ clon
    df["coord_source"] = "caiso_affine"
    df["coord_resid_km"] = round(max_resid, 3)
    return df, max_resid


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    session = _session()

    print(f"Fetching CAISO POI heatmap data from {DATA_URL} ...")
    df = fetch_buses(session)
    print(f"  {len(df)} unique CAISO buses, pto = {sorted(df.pto.unique())}")

    anchors = build_anchors(df)
    print(f"  {len(anchors)} unambiguous CEC name matches used as georeferencing anchors")
    df, _ = georeference(df, anchors)

    cols = ["busname", "busvolt_kv", "busnum", "pto", "region", "latitude", "longitude",
            *(_SCENARIO_COL.values()), "coord_source", "coord_resid_km"]
    for c in cols:
        if c not in df:
            df[c] = np.nan
    df = df[cols].sort_values(["pto", "busvolt_kv", "busname"], ascending=[True, False, True])

    out = OUT_DIR / "caiso_heatmap_substations.csv"
    df.to_csv(out, index=False)
    print(f"\nWrote {len(df)} substations -> {out.relative_to(ROOT)}")
    print(df[["busname", "pto", "busvolt_kv", "latitude", "longitude"]].head(15)
          .round(4).to_string(index=False))


if __name__ == "__main__":
    main()
