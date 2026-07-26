"""Coarse-resolution check: ReEDS p-region (balancing area) boundaries vs IOU
service-territory footprints.

Companion to validate_county_reeds.py's county-level MSE check, but at BA
resolution instead of county resolution, and geographic (are p-regions
utility-pure?) rather than load-magnitude (does the geographic county split
reproduce our load?). No official IOU service-territory shapefile is
available, so utility footprint is approximated by the real substation point
cloud (substation_county_reeds_mapping.csv), colored by utility, overlaid on
county polygons dissolved into their p-region (TIGER 2022 counties x
county_ca_reference.csv's county->p_region assignment).

Substation utility-purity by p-region (printed and saved):
  p9  = PGE (96.8%), p10 = SCE (90.4%, 8.6% PGE bleed-through -
  eastern Sierra/high-desert counties like Mono/Inyo where PGE and SCE
  territory interleave), p11 = SDGE (100%). p8 has zero PGE/SCE/SDGE
  substations (confirmed PacifiCorp-only territory, see CLAUDE.md).

Outputs:
  data/processed/load_projection/validation/ba_iou_purity.csv
  data/figures/load_projection/validation/ba_iou_map.png

Usage:
  python scripts/load_projection/checks/plot_ba_iou_comparison.py
"""

from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
SUB_COUNTY = ROOT / "data/processed/substations/substation_county_reeds_mapping.csv"
COUNTY_REF = ROOT / "data/processed/reeds/county_ca_reference.csv"
COUNTY_SHP = ROOT / ("data/raw/reeds/ReEDS-2.0/inputs/shapefiles/cache/"
                     "tl_2022_us_county/tl_2022_us_county.shp")
OUT_DIR = ROOT / "data/processed/load_projection/validation"
FIG_DIR = ROOT / "data/figures/load_projection/validation"

UTIL_COLORS = {"PGE": "#1b9e77", "SCE": "#d95f02", "SDGE": "#7570b3"}
PREGION_COLORS = {"p8": "#f0f0f0", "p9": "#a1d99b", "p10": "#fdae6b", "p11": "#9ecae1"}


def purity_table(sc: pd.DataFrame) -> pd.DataFrame:
    ct = pd.crosstab(sc.p_region, sc.utility)
    ct["total"] = ct.sum(axis=1)
    for u in UTIL_COLORS:
        if u in ct.columns:
            ct[f"{u}_pct"] = (100 * ct[u] / ct.total).round(1)
    return ct.reset_index()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)

    sc = pd.read_csv(SUB_COUNTY)
    sc["utility"] = sc.utility.str.upper()

    tbl = purity_table(sc)
    tbl.to_csv(OUT_DIR / "ba_iou_purity.csv", index=False)
    print("substation utility purity by p-region (ReEDS balancing area):")
    print(tbl.to_string(index=False))

    county_ref = pd.read_csv(COUNTY_REF)[["fips_int", "p_region"]]
    county = gpd.read_file(COUNTY_SHP)
    county = county[county.STATEFP == "06"].to_crs(4326)
    county["fips_int"] = county.STATEFP.astype(int) * 1000 + county.COUNTYFP.astype(int)
    county = county.merge(county_ref, on="fips_int", how="left")
    ba = county.dissolve(by="p_region").reset_index()

    subs = sc.dropna(subset=["lat", "lon"])
    gdf = gpd.GeoDataFrame(subs, geometry=gpd.points_from_xy(subs.lon, subs.lat), crs=4326)

    fig, ax = plt.subplots(figsize=(8, 10))
    ba.plot(ax=ax, color=ba.p_region.map(PREGION_COLORS), edgecolor="black",
           linewidth=1.2)
    for u, color in UTIL_COLORS.items():
        sub = gdf[gdf.utility == u]
        ax.scatter(sub.geometry.x, sub.geometry.y, s=4, color=color, label=u, alpha=0.8)
    ax.legend(title="substation utility", loc="lower left", fontsize=9)
    ax.set_title("ReEDS p-region (balancing area) boundaries vs IOU substation footprint")
    ax.set_axis_off()
    fig.tight_layout()
    fig_path = FIG_DIR / "ba_iou_map.png"
    fig.savefig(fig_path, dpi=150)
    print(f"\nwrote {(OUT_DIR / 'ba_iou_purity.csv').relative_to(ROOT)}")
    print(f"wrote {fig_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
