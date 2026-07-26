"""
map_review_candidates.py

Quick interactive map for manually reviewing the small set of utility
substations that build_cec_name_dictionary.py could not auto-resolve
(cec_candidates_{util}.csv rows where already_in_dict == False).

Unlike the candidates CSV — which only lists names that survived the rule
set (no_n_rule, ph_drop, spatial, ...) — this map shows EVERY CEC and basin
record within RADIUS_KM of the utility coordinate, regardless of name, so a
true match that the rules missed is still visible.

Usage
-----
  python scripts/data/substations/map_review_candidates.py
      (defaults to every still-unresolved SourceName across all 3 utilities)
  python scripts/data/substations/map_review_candidates.py --names LODI,"BERKELEY T",RUSSELL

Output
------
  data/figures/substation_maps/review_candidates.html
"""
from __future__ import annotations

import argparse
from pathlib import Path

import folium
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
ATTR_FILE = ROOT / "data/processed/substations/substation_attributes_clean.csv"
CEC_FILE = ROOT / "data/processed/substation_misc/ca_substations_cec.csv"
BASIN_FILE = ROOT / "data/processed/substation_misc/ca_substations_2022.csv"
CHECKS = ROOT / "data/checks/find_cec_name_candidates"
OUT = ROOT / "data/figures/substation_maps/review_candidates.html"

RADIUS_KM = 3.0
_UTIL_MAP = {"pge": "PGE", "sce": "SCE", "sdge": "SDGE"}


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))


def still_unresolved() -> pd.DataFrame:
    """All (SourceName, utility) pairs across the 3 candidate files where
    already_in_dict is False."""
    rows = []
    for owner, U in _UTIL_MAP.items():
        f = CHECKS / f"cec_candidates_{owner}.csv"
        if not f.exists():
            continue
        c = pd.read_csv(f)
        need = c[~c.already_in_dict][["SourceName"]].drop_duplicates()
        need["utility"] = owner
        rows.append(need)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["SourceName", "utility"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", default=None,
                    help="Comma-separated SourceName list to restrict to (default: all unresolved)")
    ap.add_argument("--radius-km", type=float, default=RADIUS_KM)
    args = ap.parse_args()

    attrs = pd.read_csv(ATTR_FILE)
    cec = pd.read_csv(CEC_FILE)
    basin = pd.read_csv(BASIN_FILE)

    targets = still_unresolved()
    if args.names:
        wanted = {n.strip() for n in args.names.split(",")}
        targets = targets[targets.SourceName.isin(wanted)]

    if targets.empty:
        print("No unresolved SourceNames to map (nothing left, or --names matched nothing).")
        return

    m = folium.Map(location=[37.5, -120.0], zoom_start=6, tiles="CartoDB positron")

    for _, t in targets.iterrows():
        src_name, owner = t.SourceName, t.utility
        srow = attrs[(attrs.utility == owner) & (attrs.substation_name == src_name)]
        if srow.empty or srow.iloc[0].util_lat != srow.iloc[0].util_lat:  # NaN check
            print(f"  skip {src_name} ({owner}): no utility coordinate")
            continue
        slat, slon = srow.iloc[0].util_lat, srow.iloc[0].util_lon

        fg = folium.FeatureGroup(name=f"{_UTIL_MAP[owner]} — {src_name}", show=True)

        folium.Marker(
            [slat, slon],
            tooltip=f"SOURCE: {src_name} ({_UTIL_MAP[owner]})",
            icon=folium.Icon(color="red", icon="bolt", prefix="fa"),
        ).add_to(fg)

        c = cec.copy()
        c["dist_km"] = haversine_km(slat, slon, c.latitude, c.longitude)
        near_cec = c[c.dist_km <= args.radius_km].sort_values("dist_km")
        for _, r in near_cec.iterrows():
            folium.CircleMarker(
                [r.latitude, r.longitude], radius=6, color="#1f77b4", fill=True,
                fill_opacity=0.8,
                tooltip=f"CEC: {r['name']} ({r.owner_std}, {r.type}) — {r.dist_km:.2f} km",
                popup=folium.Popup(
                    f"<b>{r['name']}</b><br>owner: {r.owner_raw}<br>type: {r.type}<br>"
                    f"status: {r.get('status','')}<br>dist: {r.dist_km:.2f} km", max_width=250),
            ).add_to(fg)

        b = basin.copy()
        b["dist_km"] = haversine_km(slat, slon, b.latitude, b.longitude)
        near_basin = b[b.dist_km <= args.radius_km].sort_values("dist_km")
        for _, r in near_basin.iterrows():
            folium.CircleMarker(
                [r.latitude, r.longitude], radius=4, color="#2ca02c", fill=True,
                fill_opacity=0.6,
                tooltip=f"basin: {r['name']} ({r.owner_std}, {r.type}) — {r.dist_km:.2f} km",
            ).add_to(fg)

        fg.add_to(m)
        print(f"  {src_name} ({_UTIL_MAP[owner]}): {len(near_cec)} CEC + {len(near_basin)} "
              f"basin records within {args.radius_km} km")

    folium.LayerControl(collapsed=False).add_to(m)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(OUT))
    print(f"\nSaved -> {OUT.relative_to(ROOT)}")
    print(f"Legend: RED marker = utility source point | BLUE dot = CEC record | GREEN dot = basin record")


if __name__ == "__main__":
    main()
