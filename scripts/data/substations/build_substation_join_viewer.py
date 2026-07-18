"""
build_substation_join_viewer.py

Builds an interactive HTML map for manually identifying name mismatches
between utility substations (cleaned output), DataBasin 2022, and OpenStreetMap.

The goal is to find substations that are the same physical location but named
differently across sources, so they can be added to basinSourceDictionary.csv.

OSM matching priority (applied to all 4,800+ OSM substations regardless of operator tag):
  1. Name match against IOU substations (norm comparison; closest IOU wins for shared names)
  2. Distance <1km to IOU substation
  3. Distance <1km to basin substation
  4. Distance 1-5km to nearest IOU or basin substation ("near" layer)
  5. No match within 5km

For unnamed OSM substations, a synthetic name (UNNAMED_N) is assigned; if a <1km
IOU or basin match is found that name is replaced with the matched substation name
so the point is searchable and distinguishable. The operator tag is informational only.

Layers (toggleable via layer control):
  <UTIL> — utility, no basin match        [shown]   orange circles
  <UTIL> — utility, has basin match       [hidden]  green circles
  <UTIL> — basin, not in cleaned          [shown]   red circles
  <UTIL> — basin, in cleaned              [hidden]  light-green circles
  OSM — <UTIL> — name match              [hidden]  green diamonds
  OSM — <UTIL> — IOU <1km               [hidden]  yellow diamonds
  OSM — <UTIL> — basin <1km             [hidden]  cyan diamonds
  OSM — near match 1-5km                 [hidden]  pink diamonds
  OSM — NO match                          [shown]   orange diamonds
  OSM — non-IOU operator                  [hidden]  light-blue diamonds
  OSM — unnamed no match                  [hidden]  gray diamonds

Usage
-----
  python scripts/data/substations/build_substation_join_viewer.py
  python scripts/data/substations/build_substation_join_viewer.py --out path/to/out.html
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import folium
import numpy as np
import pandas as pd

ROOT      = Path(__file__).resolve().parents[3]
PROC      = ROOT / "data" / "processed"
FIGS_MAPS = ROOT / "data" / "figures" / "substation_maps"
DEFAULT_OUT = FIGS_MAPS / "substation_join_viewer.html"
OSM_CSV   = ROOT / "data" / "raw" / "osm" / "osm_substations_ca.csv"

CA_LAT = (32.4, 42.1)
CA_LON = (-124.6, -113.9)

# ── Name normalisation (same as process_substations_clean.py) ─────────────────

_PT_RE    = re.compile(r"\s+p\.?\s*t\.?\s*$",   re.IGNORECASE)
_SUB_RE   = re.compile(r"\bsubstation\b",        re.IGNORECASE)
_PUNCT_RE = re.compile(r"[/\-,\.&\(\)_#']")
_SPC_RE   = re.compile(r"\s+")


def norm(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    for pat, rep in [(_PT_RE, ""), (_SUB_RE, ""), (_PUNCT_RE, " "), (_SPC_RE, " ")]:
        s = s.str.replace(pat, rep, regex=True)
    return s.str.strip().str.lower()


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    lat1, lon1, lat2, lon2 = (np.radians(float(x)) for x in [lat1, lon1, lat2, lon2])
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * R * np.arcsin(np.clip(np.sqrt(a), 0, 1))


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (clean_attrs, basin) with normalised name columns added."""
    clean = pd.read_csv(PROC / "substations" / "substation_attributes_clean.csv")
    basin = pd.read_csv(PROC / "substation_misc" / "ca_substations_2022.csv")

    clean["_norm"] = norm(clean["substation_name"])
    basin["_norm"] = norm(basin["name"])

    basin = basin[
        basin["owner_std"].isin(["pge", "sce", "sdge"]) &
        basin["latitude"].between(CA_LAT[0], CA_LAT[1]) &
        basin["longitude"].between(CA_LON[0], CA_LON[1]) &
        basin["latitude"].notna() & basin["longitude"].notna()
    ].copy()

    return clean, basin


def build_basin_name_lookup(clean: pd.DataFrame, basin: pd.DataFrame) -> pd.Series:
    basin_coords = basin[["latitude", "longitude", "name"]].copy().reset_index(drop=True)
    result = pd.Series(index=clean.index, dtype=object)
    has_basin = clean["basin_lat"].notna() & clean["basin_lon"].notna()
    for idx in clean.index[has_basin]:
        blat = float(clean.at[idx, "basin_lat"])
        blon = float(clean.at[idx, "basin_lon"])
        dlat = (basin_coords["latitude"] - blat).abs()
        dlon = (basin_coords["longitude"] - blon).abs()
        candidates = basin_coords[(dlat < 0.05) & (dlon < 0.05)]
        if candidates.empty:
            candidates = basin_coords
        dists = ((candidates["latitude"] - blat) ** 2 + (candidates["longitude"] - blon) ** 2)
        best_idx = dists.idxmin()
        if dists[best_idx] < 0.01 ** 2:
            result[idx] = basin_coords.at[best_idx, "name"]
    return result


def build_utility_name_lookup(clean: pd.DataFrame, basin: pd.DataFrame) -> pd.Series:
    util_by_norm: dict[str, str] = {}
    for _, row in clean.iterrows():
        util_by_norm[row["_norm"]] = f"{row['substation_name']} ({row['utility'].upper()})"
    return basin["_norm"].map(util_by_norm)


# ── Popup HTML ────────────────────────────────────────────────────────────────

def _fmt(v, fmt=None, default="—") -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return default
    if fmt:
        return fmt.format(v)
    return str(v)


def utility_popup(row: pd.Series, basin_name: str | None) -> str:
    status = (
        f"<span style='color:#2ca02c'>&#10003; Basin match: <b>{basin_name}</b> "
        f"({_fmt(row.get('dist_to_basin_km'), '{:.2f}')} km)</span>"
        if basin_name
        else "<span style='color:#d62728'>&#10007; No basin match</span>"
    )
    coords = (f"{_fmt(row.get('util_lat'), '{:.5f}')}, "
              f"{_fmt(row.get('util_lon'), '{:.5f}')}")
    extra = ""
    if _fmt(row.get("division")) != "—":
        extra += f"<br>Division: {row['division']}"
    if _fmt(row.get("voltage_kv")) != "—":
        extra += f"<br>Voltage: {_fmt(row.get('voltage_kv'), '{:.1f}')} kV"
    return (
        f"<div style='font-family:sans-serif;min-width:200px'>"
        f"<b style='font-size:13px'>{row['substation_name']}</b>"
        f"<br><span style='color:#555'>Utility: {row['utility'].upper()}</span>"
        f"<br>Coords: {coords}"
        f"{extra}"
        f"<br>{status}"
        f"</div>"
    )


def basin_popup(row: pd.Series, util_name: str | None) -> str:
    status = (
        f"<span style='color:#2ca02c'>&#10003; In cleaned: <b>{util_name}</b></span>"
        if util_name
        else "<span style='color:#d62728'>&#10007; Not in cleaned output</span>"
    )
    city   = _fmt(row.get("city"))
    county = _fmt(row.get("county"))
    loc = ", ".join(x for x in [city, county] if x != "—")
    coords = f"{_fmt(row.get('latitude'), '{:.5f}')}, {_fmt(row.get('longitude'), '{:.5f}')}"
    return (
        f"<div style='font-family:sans-serif;min-width:200px'>"
        f"<b style='font-size:13px'>{row['name']}</b>"
        f"<br><span style='color:#555'>Owner: {row.get('owner_std','').upper()} (basin)</span>"
        f"<br>Coords: {coords}"
        + (f"<br>Location: {loc}" if loc else "")
        + f"<br>{status}"
        f"</div>"
    )


# ── Layer builders ────────────────────────────────────────────────────────────

_CAT_STYLE = {
    "util_unmatched": dict(radius=8, fill=True, fill_opacity=0.85, weight=1.5, color="#7f3f00"),
    "util_matched":   dict(radius=6, fill=True, fill_opacity=0.6,  weight=1,   color="#155724"),
    "basin_unmatched":dict(radius=8, fill=True, fill_opacity=0.85, weight=1.5, color="#7f0000"),
    "basin_matched":  dict(radius=5, fill=True, fill_opacity=0.5,  weight=1,   color="#0a3d0a"),
}
_CAT_FILL = {
    "util_unmatched":  "#ff7f0e",
    "util_matched":    "#2ca02c",
    "basin_unmatched": "#d62728",
    "basin_matched":   "#98df8a",
}


def add_utility_layer(m, util, util_df, basin_names, show, matched):
    cat   = "util_matched" if matched else "util_unmatched"
    label = f"{util.upper()} — utility, {'has basin match' if matched else 'NO basin match'}"
    style = {**_CAT_STYLE[cat], "fill_color": _CAT_FILL[cat]}
    fg = folium.FeatureGroup(name=label, show=show)
    for idx, row in util_df.iterrows():
        lat, lon = row.get("util_lat"), row.get("util_lon")
        if pd.isna(lat) or pd.isna(lon):
            continue
        folium.CircleMarker(
            location=[lat, lon],
            tooltip=folium.Tooltip(row["substation_name"], sticky=False),
            popup=folium.Popup(utility_popup(row, basin_names.get(idx)), max_width=320),
            **style,
        ).add_to(fg)
    fg.add_to(m)


def add_basin_layer(m, util, basin_df, util_names, show, matched):
    cat   = "basin_matched" if matched else "basin_unmatched"
    label = f"{util.upper()} — basin, {'in cleaned' if matched else 'NOT in cleaned'}"
    style = {**_CAT_STYLE[cat], "fill_color": _CAT_FILL[cat]}
    fg = folium.FeatureGroup(name=label, show=show)
    for idx, row in basin_df.iterrows():
        lat, lon = row.get("latitude"), row.get("longitude")
        if pd.isna(lat) or pd.isna(lon):
            continue
        folium.CircleMarker(
            location=[lat, lon],
            tooltip=folium.Tooltip(row["name"], sticky=False),
            popup=folium.Popup(basin_popup(row, util_names.get(idx)), max_width=320),
            **style,
        ).add_to(fg)
    fg.add_to(m)


# ── Search bar (custom JS) ────────────────────────────────────────────────────

_SEARCH_JS = """
<style>
  #sub-search-box {
    position: fixed; top: 12px; left: 55px; z-index: 10000;
    background: white; border: 2px solid #aaa; border-radius: 6px;
    padding: 5px 8px; font-family: sans-serif; font-size: 13px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.25);
    display: flex; align-items: center; gap: 6px;
  }
  #sub-search-input { border: none; outline: none; width: 200px; font-size: 13px; }
  #sub-search-clear { cursor: pointer; color: #999; font-size: 14px; }
  #sub-search-status { color: #555; font-size: 11px; min-width: 60px; }
</style>
<div id="sub-search-box">
  <span>&#128269;</span>
  <input id="sub-search-input" type="text" placeholder="Search substation name…" />
  <span id="sub-search-clear" title="Clear">&#x2715;</span>
  <span id="sub-search-status"></span>
</div>
<script>
(function() {
  var input  = document.getElementById('sub-search-input');
  var clear  = document.getElementById('sub-search-clear');
  var status = document.getElementById('sub-search-status');
  var found  = [];
  var foundIdx = 0;

  function getAllLayers(group) {
    var out = [];
    if (!group) return out;
    if (typeof group.eachLayer === 'function') {
      group.eachLayer(function(l) { out = out.concat(getAllLayers(l)); });
    } else if (group._latlng) {
      out.push(group);
    }
    return out;
  }

  function doSearch(q) {
    q = q.trim().toLowerCase();
    found = []; foundIdx = 0;
    if (!q) { status.textContent = ''; return; }
    var map = window._leaflet_map;
    if (!map) return;
    map.eachLayer(function(layer) {
      if (layer instanceof L.FeatureGroup || layer instanceof L.LayerGroup) {
        getAllLayers(layer).forEach(function(m) {
          var tooltip = m.getTooltip ? m.getTooltip() : null;
          var text = tooltip ? (tooltip._content || '').toLowerCase() : '';
          if (text && text.includes(q)) found.push(m);
        });
      }
    });
    if (found.length === 0) { status.textContent = 'not found'; return; }
    status.textContent = '1/' + found.length;
    jumpTo(0);
  }

  function jumpTo(idx) {
    var m = found[idx];
    if (!m) return;
    window._leaflet_map.setView(m.getLatLng(), 14, {animate: true});
    if (m.openPopup) m.openPopup();
    status.textContent = (idx + 1) + '/' + found.length;
  }

  input.addEventListener('input', function() { doSearch(input.value); });
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && found.length > 1) {
      foundIdx = (foundIdx + 1) % found.length;
      jumpTo(foundIdx);
    }
  });
  clear.addEventListener('click', function() {
    input.value = ''; found = []; status.textContent = '';
  });

  var _init = setInterval(function() {
    var keys = Object.keys(window).filter(function(k) {
      try { return k.startsWith('map_') && window[k] instanceof L.Map; } catch(e) { return false; }
    });
    if (keys.length) { window._leaflet_map = window[keys[0]]; clearInterval(_init); }
  }, 200);
})();
</script>
"""


# ── Legend ────────────────────────────────────────────────────────────────────

_LEGEND_HTML = """
<div style="
  position: fixed; bottom: 30px; left: 10px; z-index: 9999;
  background: white; border: 1px solid #aaa; border-radius: 6px;
  padding: 10px 14px; font-family: sans-serif; font-size: 12px;
  box-shadow: 0 2px 6px rgba(0,0,0,0.2); line-height: 1.8;
">
  <b style="font-size:13px">Legend</b><br>
  <span style="color:#ff7f0e">&#9679;</span> Utility — no basin match<br>
  <span style="color:#2ca02c">&#9679;</span> Utility — has basin match<br>
  <span style="color:#d62728">&#9679;</span> Basin — not in cleaned output<br>
  <span style="color:#98df8a">&#9679;</span> Basin — in cleaned output<br>
  <b style="font-size:11px">OSM circles (dashed border):</b><br>
  <span style="color:#155724">&#9711;</span> IOU name match (hidden)<br>
  <span style="color:#8a6d00">&#9711;</span> IOU &lt;1km (hidden)<br>
  <span style="color:#0e6b77">&#9711;</span> Basin &lt;1km (hidden)<br>
  <span style="color:#c5004a">&#9711;</span> Near 1-5km (hidden)<br>
  <span style="color:#7f3f00">&#9711;</span> NO match &gt;5km (shown)<br>
  <span style="color:#1f77b4">&#9711;</span> Non-IOU operator (hidden)<br>
  <span style="color:#666">&#9711;</span> Unnamed no match (hidden)<br>
  <span style="color:#888;font-size:11px">Click any point for details.<br>
  Search by name, press Enter to cycle.</span>
</div>
"""


# ── OSM helpers ───────────────────────────────────────────────────────────────

_NON_IOU_SUBSTRINGS = [
    "los angeles department", "ladwp",
    "sacramento municipal", "smud",
    "western area power", "wapa",
    "imperial irrigation", "iid",
    "turlock irrigation", "modesto irrigation",
    "northern california power",
    "burbank water", "glendale water", "pasadena water",
    "riverside public", "silicon valley power",
    "pacificorp", "pacific power",
    "anaheim public", "azusa light",
    "city of lodi", "city of palo alto", "city of roseville",
]

# match_type -> (fill_color, border_color, fill_opacity, radius)
_MATCH_STYLE = {
    "name":           ("#2ca02c", "#155724", 0.75, 6),
    "distance_iou":   ("#ffdd57", "#8a6d00", 0.85, 7),
    "distance_basin": ("#17becf", "#0e6b77", 0.75, 7),
    "near":           ("#f7b6d2", "#c5004a", 0.65, 6),
    "none":           ("#ff7f0e", "#7f3f00", 0.85, 8),
    "non_iou":        ("#aec7e8", "#1f77b4", 0.55, 5),
    "unnamed":        ("#cccccc", "#666666", 0.55, 5),
}


def _is_non_iou(operator: str) -> bool:
    op = operator.strip().lower()
    return bool(op) and any(p in op for p in _NON_IOU_SUBSTRINGS)


def load_osm(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str).fillna("")
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df = df.dropna(subset=["lat", "lon"]).copy()
    df["_had_name"] = df["name"].str.strip() != ""
    # Synthetic names so unnamed subs have tooltip text; replaced by matched name if <1km match found
    unnamed_idx = df.index[~df["_had_name"]]
    df.loc[unnamed_idx, "name"] = [f"UNNAMED_{i+1}" for i in range(len(unnamed_idx))]
    df["_norm"] = norm(df["name"])
    df["_display_name"] = df["name"]   # updated during matching for unnamed <1km matches
    return df


def _vec_haversine_km(lat1: float, lon1: float, lats2: np.ndarray, lons2: np.ndarray) -> np.ndarray:
    R = 6371.0
    la1, lo1 = np.radians(lat1), np.radians(lon1)
    la2, lo2 = np.radians(lats2), np.radians(lons2)
    a = np.sin((la2 - la1) / 2) ** 2 + np.cos(la1) * np.cos(la2) * np.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * np.arcsin(np.clip(np.sqrt(a), 0, 1))


def match_osm_to_utilities(
    osm: pd.DataFrame,
    clean: pd.DataFrame,
    basin: pd.DataFrame,
    dist_close_km: float = 1.0,
    dist_near_km: float = 5.0,
) -> pd.DataFrame:
    """
    Match all OSM substations against IOU substations (priority) then basin substations.

    Priority:
      1. Name match against IOU (norm comparison; when name shared across IOUs, closest wins)
      2. Distance < dist_close_km to nearest IOU substation
      3. Distance < dist_close_km to nearest basin substation
      4. Distance < dist_near_km to nearest IOU or basin substation ('near')
      5. No match

    For unnamed OSM substations that get a <dist_close_km match, _display_name is set
    to the matched IOU or basin name so the point is searchable on the map.

    Adds columns: match_type, match_util, match_util_name, match_dist_km,
                  match_nearest, match_nearest_util, _display_name (updated for unnamed).
    """
    osm = osm.copy()

    # IOU norm lookup: norm -> list of (util, substation_name, lat|None, lon|None)
    norm_to_subs: dict[str, list[tuple]] = {}
    for _, row in clean.iterrows():
        n = row["_norm"]
        lat = row.get("util_lat")
        lon = row.get("util_lon")
        lat = None if (lat is None or (isinstance(lat, float) and np.isnan(lat))) else float(lat)
        lon = None if (lon is None or (isinstance(lon, float) and np.isnan(lon))) else float(lon)
        norm_to_subs.setdefault(n, []).append((row["utility"], row["substation_name"], lat, lon))

    # IOU coords for vectorised distance search
    iou_coords = (
        clean[["utility", "substation_name", "util_lat", "util_lon"]]
        .dropna(subset=["util_lat", "util_lon"])
        .reset_index(drop=True)
    )
    i_lats = iou_coords["util_lat"].to_numpy(dtype=float)
    i_lons = iou_coords["util_lon"].to_numpy(dtype=float)

    # Basin coords for vectorised distance search
    basin_coords = (
        basin[["owner_std", "name", "latitude", "longitude"]]
        .dropna(subset=["latitude", "longitude"])
        .reset_index(drop=True)
    )
    b_lats = basin_coords["latitude"].to_numpy(dtype=float)
    b_lons = basin_coords["longitude"].to_numpy(dtype=float)

    match_types, match_utils, match_names, match_dists = [], [], [], []
    match_nearests, match_nearest_utils = [], []
    display_names = list(osm["_display_name"])

    for i, (_, row) in enumerate(osm.iterrows()):
        osm_norm = row["_norm"]
        had_name = bool(row.get("_had_name", True))
        olat, olon = float(row["lat"]), float(row["lon"])

        # ── nearest IOU ────────────────────────────────────────────────────────
        if len(i_lats) > 0:
            d_iou_all = _vec_haversine_km(olat, olon, i_lats, i_lons)
            bi_iou = int(np.argmin(d_iou_all))
            d_iou  = float(d_iou_all[bi_iou])
            nn_iou_name = iou_coords.at[bi_iou, "substation_name"]
            nn_iou_util = iou_coords.at[bi_iou, "utility"]
        else:
            d_iou, nn_iou_name, nn_iou_util = np.inf, "", ""

        # ── nearest basin ──────────────────────────────────────────────────────
        if len(b_lats) > 0:
            d_basin_all = _vec_haversine_km(olat, olon, b_lats, b_lons)
            bi_basin = int(np.argmin(d_basin_all))
            d_basin  = float(d_basin_all[bi_basin])
            nn_basin_name = basin_coords.at[bi_basin, "name"]
            nn_basin_util = basin_coords.at[bi_basin, "owner_std"]
        else:
            d_basin, nn_basin_name, nn_basin_util = np.inf, "", ""

        # ── 1. IOU name match ──────────────────────────────────────────────────
        if osm_norm in norm_to_subs:
            candidates = norm_to_subs[osm_norm]
            if len(candidates) == 1:
                winner, win_d = candidates[0], np.nan
            else:
                winner, win_d = None, np.inf
                for util, sub_name, clat, clon in candidates:
                    if clat is None or clon is None:
                        continue
                    d = float(_vec_haversine_km(olat, olon, np.array([clat]), np.array([clon]))[0])
                    if d < win_d:
                        win_d, winner = d, (util, sub_name, clat, clon)
                if winner is None:
                    winner = candidates[0]
                    win_d = np.nan
            match_types.append("name")
            match_utils.append(winner[0])
            match_names.append(winner[1])
            match_dists.append(win_d if not np.isinf(win_d) else np.nan)
            match_nearests.append(winner[1])
            match_nearest_utils.append(winner[0])
            continue

        # ── 2. IOU distance <1km ───────────────────────────────────────────────
        if d_iou <= dist_close_km:
            if not had_name:
                display_names[i] = f"{nn_iou_name} ({nn_iou_util.upper()})"
            match_types.append("distance_iou")
            match_utils.append(nn_iou_util)
            match_names.append(nn_iou_name)
            match_dists.append(d_iou)
            match_nearests.append(nn_iou_name)
            match_nearest_utils.append(nn_iou_util)
            continue

        # ── 3. Basin distance <1km ─────────────────────────────────────────────
        if d_basin <= dist_close_km:
            if not had_name:
                display_names[i] = f"{nn_basin_name} (basin)"
            match_types.append("distance_basin")
            match_utils.append(nn_basin_util)
            match_names.append(nn_basin_name)
            match_dists.append(d_basin)
            match_nearests.append(nn_basin_name)
            match_nearest_utils.append(nn_basin_util)
            continue

        # ── 4. Near match 1-5km (IOU or basin, whichever is closer) ───────────
        d_near = min(d_iou, d_basin)
        if d_near <= dist_near_km:
            if d_iou <= d_basin:
                near_name, near_util, near_dist = nn_iou_name, nn_iou_util, d_iou
            else:
                near_name, near_util, near_dist = nn_basin_name, nn_basin_util, d_basin
            match_types.append("near")
            match_utils.append(near_util)
            match_names.append(near_name)
            match_dists.append(near_dist)
            match_nearests.append(near_name)
            match_nearest_utils.append(near_util)
            continue

        # ── 5. No match ────────────────────────────────────────────────────────
        match_types.append("none" if had_name else "unnamed")
        match_utils.append("")
        match_names.append("")
        nn_d = min(d_iou, d_basin)
        match_dists.append(nn_d if not np.isinf(nn_d) else np.nan)
        if d_iou <= d_basin:
            match_nearests.append(nn_iou_name)
            match_nearest_utils.append(nn_iou_util)
        else:
            match_nearests.append(nn_basin_name)
            match_nearest_utils.append(nn_basin_util)

    osm["match_type"]         = match_types
    osm["match_util"]         = match_utils
    osm["match_util_name"]    = match_names
    osm["match_dist_km"]      = match_dists
    osm["match_nearest"]      = match_nearests
    osm["match_nearest_util"] = match_nearest_utils
    osm["_display_name"]      = display_names
    return osm


def osm_popup(row: pd.Series) -> str:
    had_name    = bool(row.get("_had_name", True))
    raw_name    = row.get("name", "").strip()
    display     = row.get("_display_name", raw_name)
    name_html   = display if had_name else f"<em>{display} (no name tag)</em>"
    operator    = row.get("operator", "").strip() or "—"
    voltage     = row.get("voltage", "").strip()
    sub_type    = row.get("substation", "").strip()
    osm_id      = row.get("osm_id", "")
    osm_type    = row.get("osm_type", "")
    osm_url     = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"

    volt_str = ""
    if voltage:
        try:
            volt_str = f"<br>Voltage: {int(voltage)//1000:.0f} kV"
        except (ValueError, TypeError):
            volt_str = f"<br>Voltage: {voltage}"

    match_type = row.get("match_type", "")
    match_name = row.get("match_util_name", "") or row.get("match_nearest", "")
    match_util = (row.get("match_util") or row.get("match_nearest_util", "")).upper()
    dist_km    = row.get("match_dist_km")
    dist_str   = (f"{dist_km:.2f} km"
                  if dist_km is not None and not (isinstance(dist_km, float) and np.isnan(dist_km))
                  else "?")

    if match_type == "name":
        mstatus = (f"<span style='color:#2ca02c'>&#10003; IOU name match: "
                   f"<b>{match_name}</b> ({match_util})</span>")
    elif match_type == "distance_iou":
        mstatus = (f"<span style='color:#8a6d00'>&#8771; IOU &lt;1km: "
                   f"<b>{match_name}</b> ({match_util}, {dist_str})</span>")
    elif match_type == "distance_basin":
        mstatus = (f"<span style='color:#0e6b77'>&#8771; Basin &lt;1km: "
                   f"<b>{match_name}</b> ({match_util.upper()}, {dist_str})</span>")
    elif match_type == "near":
        mstatus = (f"<span style='color:#c5004a'>&#8771; Near 1-5km: "
                   f"<b>{match_name}</b> ({match_util}, {dist_str})</span>")
    elif match_type in ("none", "unnamed"):
        near_str = (f"<br><span style='color:#555'>Nearest: {match_name} "
                    f"({match_util}, {dist_str})</span>") if match_name else ""
        mstatus = f"<span style='color:#d62728'>&#10007; No match within 5km</span>{near_str}"
    else:
        mstatus = ""

    return (
        f"<div style='font-family:sans-serif;min-width:220px'>"
        f"<b style='font-size:13px'>{name_html}</b>"
        f"<br><span style='color:#555'>OSM ({osm_type})</span>"
        f"<br>Operator: {operator}"
        + volt_str
        + (f"<br>Type: {sub_type}" if sub_type else "")
        + f"<br>Coords: {row['lat']:.5f}, {row['lon']:.5f}"
        + (f"<br>{mstatus}" if mstatus else "")
        + f"<br><a href='{osm_url}' target='_blank'>OSM {osm_id}</a>"
        + f"</div>"
    )


def _add_osm_marker(fg, row, fill_color, border_color, fill_opacity, radius):
    display = row.get("_display_name", row.get("name", "")).strip()
    tooltip_text = display if display else f"OSM {row.get('osm_id','')}"
    # CircleMarker (not RegularPolygonMarker) — avoids leaflet-dvf dependency and
    # canvas/SVG conflicts that cause all markers to disappear on zoom.
    # Visual distinction from utility/basin: lower fill_opacity + dashed border.
    folium.CircleMarker(
        location=[row["lat"], row["lon"]],
        radius=radius,
        color=border_color,
        weight=2,
        dash_array="4 3",
        fill=True,
        fill_color=fill_color,
        fill_opacity=fill_opacity * 0.6,   # dimmer fill so OSM reads as "secondary" layer
        tooltip=folium.Tooltip(tooltip_text, sticky=False),
        popup=folium.Popup(osm_popup(row), max_width=320),
    ).add_to(fg)


def add_osm_layers(m: folium.Map, osm: pd.DataFrame) -> None:
    """Add OSM layers by match type. Per-IOU for name/distance; combined for near/no-match."""

    # Per-IOU: name match and IOU <1km and basin <1km (all hidden)
    for util_key, util_label in [("pge", "PGE"), ("sce", "SCE"), ("sdge", "SDG&E")]:
        for mtype, type_label in [
            ("name",           "name match"),
            ("distance_iou",   "IOU <1km"),
            ("distance_basin", "basin <1km"),
        ]:
            sub = osm[(osm["match_type"] == mtype) & (osm["match_util"] == util_key)]
            if sub.empty:
                continue
            fc, bc, fo, r = _MATCH_STYLE[mtype]
            fg = folium.FeatureGroup(
                name=f"OSM — {util_label} — {type_label} ({len(sub):,})", show=False
            )
            for _, row in sub.iterrows():
                _add_osm_marker(fg, row, fc, bc, fo, r)
            fg.add_to(m)
            print(f"  OSM {util_label} {type_label}: {len(sub):,}")

    # Near 1-5km — combined, hidden
    near = osm[osm["match_type"] == "near"]
    if not near.empty:
        fc, bc, fo, r = _MATCH_STYLE["near"]
        fg = folium.FeatureGroup(name=f"OSM — near match 1-5km ({len(near):,})", show=False)
        for _, row in near.iterrows():
            _add_osm_marker(fg, row, fc, bc, fo, r)
        fg.add_to(m)
        print(f"  OSM near match 1-5km: {len(near):,}")

    # NO match — split by known non-IOU operator vs. needs review
    no_match     = osm[osm["match_type"] == "none"]
    review       = no_match[~no_match["operator"].apply(_is_non_iou)]
    non_iou_ops  = no_match[no_match["operator"].apply(_is_non_iou)]

    if not review.empty:
        fc, bc, fo, r = _MATCH_STYLE["none"]
        fg = folium.FeatureGroup(name=f"OSM — NO match ({len(review):,})", show=True)
        for _, row in review.iterrows():
            _add_osm_marker(fg, row, fc, bc, fo, r)
        fg.add_to(m)
        print(f"  OSM NO match (needs review): {len(review):,}")

    if not non_iou_ops.empty:
        fc, bc, fo, r = _MATCH_STYLE["non_iou"]
        fg = folium.FeatureGroup(
            name=f"OSM — non-IOU operator ({len(non_iou_ops):,})", show=False
        )
        for _, row in non_iou_ops.iterrows():
            _add_osm_marker(fg, row, fc, bc, fo, r)
        fg.add_to(m)
        print(f"  OSM non-IOU operator: {len(non_iou_ops):,}")

    # Unnamed — no distance match within 5km
    unnamed = osm[osm["match_type"] == "unnamed"]
    if not unnamed.empty:
        fc, bc, fo, r = _MATCH_STYLE["unnamed"]
        fg = folium.FeatureGroup(name=f"OSM — unnamed no match ({len(unnamed):,})", show=False)
        for _, row in unnamed.iterrows():
            _add_osm_marker(fg, row, fc, bc, fo, r)
        fg.add_to(m)
        print(f"  OSM unnamed no match: {len(unnamed):,}")


# ── Name map export ───────────────────────────────────────────────────────────

def export_osm_name_map(osm: pd.DataFrame, out_path: Path) -> None:
    """
    Write a CSV mapping each matched OSM substation to its IOU or basin name.

    Includes all match types so the user can filter. Columns:
      osm_display_name   name shown on map (synthetic UNNAMED_N replaced by match name if <1km)
      osm_original_name  original OSM name tag (empty if no name tag)
      osm_operator       OSM operator tag (informational)
      match_type         name / distance_iou / distance_basin / near / none / unnamed
      match_source       IOU or basin (blank for name matches: source is implied)
      match_util         pge / sce / sdge
      matched_name       IOU or basin substation name
      dist_km            distance to matched substation (blank for norm-based name matches)
      osm_lat            OSM latitude
      osm_lon            OSM longitude
      osm_id             OSM element id
      osm_type           node / way / relation

    Rows are sorted by: match_type priority, then util, then matched_name.
    """
    _TYPE_ORDER = {
        "name": 0, "distance_iou": 1, "distance_basin": 2,
        "near": 3, "none": 4, "unnamed": 5,
    }
    _SOURCE = {
        "name":           "IOU",
        "distance_iou":   "IOU",
        "distance_basin": "basin",
        "near":           "",    # could be either; popup has detail
        "none":           "",
        "unnamed":        "",
    }

    rows = []
    for _, r in osm.iterrows():
        mtype = r.get("match_type", "")
        dist  = r.get("match_dist_km")
        rows.append({
            "osm_display_name":  r.get("_display_name", r.get("name", "")),
            "osm_original_name": r.get("name", "") if r.get("_had_name") else "",
            "osm_operator":      r.get("operator", ""),
            "match_type":        mtype,
            "match_source":      _SOURCE.get(mtype, ""),
            "match_util":        r.get("match_util", ""),
            "matched_name":      r.get("match_util_name", "") or r.get("match_nearest", ""),
            "dist_km":           "" if (dist is None or (isinstance(dist, float) and np.isnan(dist))) else f"{dist:.3f}",
            "osm_lat":           r["lat"],
            "osm_lon":           r["lon"],
            "osm_id":            r.get("osm_id", ""),
            "osm_type":          r.get("osm_type", ""),
        })

    df = pd.DataFrame(rows)
    df["_sort"] = df["match_type"].map(_TYPE_ORDER).fillna(99).astype(int)
    df = df.sort_values(["_sort", "match_util", "matched_name", "osm_display_name"]).drop(columns="_sort")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    counts = osm["match_type"].value_counts().to_dict()
    print(f"  name={counts.get('name',0)}, "
          f"distance_iou={counts.get('distance_iou',0)}, "
          f"distance_basin={counts.get('distance_basin',0)}, "
          f"near={counts.get('near',0)}, "
          f"none={counts.get('none',0)}, "
          f"unnamed={counts.get('unnamed',0)}")
    print(f"  Wrote {len(df):,} rows -> {out_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def build_map(out_path: Path) -> None:
    print("Loading data ...")
    clean, basin = load_data()
    if OSM_CSV.exists():
        osm = load_osm(OSM_CSV)
        print(f"  OSM: {len(osm):,} substations loaded from {OSM_CSV.relative_to(ROOT)}")
    else:
        osm = None
        print(f"  OSM: {OSM_CSV.relative_to(ROOT)} not found — run scrape_osm_substations.py first")

    print("Building basin-name lookup for utility substations ...")
    basin_name_for_util = build_basin_name_lookup(clean, basin)

    print("Building utility-name lookup for basin substations ...")
    util_name_for_basin = build_utility_name_lookup(clean, basin)

    print("Building map ...")
    m = folium.Map(
        location=[37.5, -119.5],
        zoom_start=6,
        tiles="CartoDB positron",
    )

    for util in ["pge", "sce", "sdge"]:
        util_df  = clean[clean["utility"] == util].copy()
        basin_df = basin[basin["owner_std"] == util].copy()

        util_norms  = set(util_df["_norm"])
        basin_norms = basin_df["_norm"]

        util_matched   = util_df[util_df["basin_lat"].notna()]
        util_unmatched = util_df[util_df["basin_lat"].isna()]
        b_matched      = basin_df[basin_norms.isin(util_norms)]
        b_unmatched    = basin_df[~basin_norms.isin(util_norms)]

        add_utility_layer(m, util, util_unmatched, basin_name_for_util, show=True,  matched=False)
        add_utility_layer(m, util, util_matched,   basin_name_for_util, show=False, matched=True)
        add_basin_layer(m, util, b_unmatched, util_name_for_basin, show=True,  matched=False)
        add_basin_layer(m, util, b_matched,   util_name_for_basin, show=False, matched=True)

        print(f"  {util.upper()}: util {len(util_unmatched)} unmatched + {len(util_matched)} matched | "
              f"basin {len(b_unmatched)} unmatched + {len(b_matched)} matched")

    if osm is not None:
        print("Matching all OSM substations against IOU and basin substations ...")
        osm = match_osm_to_utilities(osm, clean, basin)
        map_path = out_path.parent / "osm_name_map.csv"
        print(f"Exporting OSM name map -> {map_path.name} ...")
        export_osm_name_map(osm, map_path)
        print("Adding OSM layers ...")
        add_osm_layers(m, osm)

    folium.LayerControl(collapsed=False, position="topright").add_to(m)
    m.get_root().html.add_child(folium.Element(_SEARCH_JS))
    m.get_root().html.add_child(folium.Element(_LEGEND_HTML))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(out_path))
    kb = out_path.stat().st_size / 1024
    print(f"\nSaved {out_path.relative_to(ROOT)}  ({kb:.0f} KB)")
    print("Open in a browser to use the interactive viewer.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        metavar="PATH",
        help=f"Output HTML path. Default: {DEFAULT_OUT}",
    )
    args = parser.parse_args()
    build_map(Path(args.out))


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
