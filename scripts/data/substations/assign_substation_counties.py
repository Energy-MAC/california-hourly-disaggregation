"""
assign_substation_counties.py

Spatially joins processed California substation data to Census county
boundaries (TIGER/Line 2022), then merges the county CA reference table to
assign each substation a ReEDS p-region, load participation factor, and BTM
distributed PV capacity.

Inputs
------
  data/processed/substations/substation_attributes_clean.csv
      Substation coordinates and attributes.
      Coordinate columns: util_lat, util_lon (primary);
                          basin_lat, basin_lon (fallback).
      Columns: utility | substation_name | util_lat | util_lon |
               basin_lat | basin_lon | dist_to_basin_km | sub_type

  data/raw/reeds/ReEDS-2.0/inputs/shapefiles/cache/tl_2022_us_county/
      Census TIGER/Line 2022 county boundary polygons (EPSG:4269 NAD83).
      Key columns: GEOID (5-digit zero-padded FIPS string), NAME, STATEFP.

  data/processed/reeds/county_ca_reference.csv
      Output of process_county_disaggregation.py.
      Key columns: fips_key | p_region | ca_load_fraction | btm_pv_{year}_mw

Coordinate source priority
--------------------------
  Each substation uses the first available coordinate pair:
    1. util_lat / util_lon   — primary utility-reported coordinates
    2. basin_lat / basin_lon — DataBasin fallback match
  Substations with no coordinates in either column are excluded from the
  spatial join and not present in the output.

Spatial join method
-------------------
  1. Build a point GeoDataFrame (EPSG:4326) from substation coordinates.
  2. Filter county shapefile to California (STATEFP == "06"); reproject
     to EPSG:4326 to match substation CRS.
  3. gpd.sjoin(predicate="within") assigns each substation to the county
     polygon that contains its point.
  4. Substations that fall outside all polygons (near coast, border, tiny
     gaps) are matched by nearest county centroid via gpd.sjoin_nearest
     using CA Albers (EPSG:3310) for accurate distance measurement.

Output
------
  data/processed/substations/substation_county_reeds_mapping.csv
      One row per substation with a valid coordinate.
      Columns: utility | substation_name | lat | lon | coord_source |
               fips_int | fips_key | county_name | p_region |
               ca_load_fraction | btm_pv_{year}_mw (2010–2050)

Usage
-----
  python scripts/data/substations/assign_substation_counties.py

Run process_county_disaggregation.py first to generate county_ca_reference.csv.
"""

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

ROOT       = Path(__file__).resolve().parents[3]
SUB_ATTR   = ROOT / "data" / "processed" / "substations" / "substation_attributes_clean.csv"
COUNTY_SHP = (ROOT / "data" / "raw" / "reeds" / "ReEDS-2.0" / "inputs"
              / "shapefiles" / "cache" / "tl_2022_us_county" / "tl_2022_us_county.shp")
COUNTY_REF = ROOT / "data" / "processed" / "reeds" / "county_ca_reference.csv"
OUT        = ROOT / "data" / "processed" / "substations"


def _load_substations() -> gpd.GeoDataFrame:
    attrs = pd.read_csv(SUB_ATTR)
    # Build coordinate columns using util first, then basin fallback
    lat = attrs["util_lat"].where(attrs["util_lat"].notna(), attrs["basin_lat"])
    lon = attrs["util_lon"].where(attrs["util_lon"].notna(), attrs["basin_lon"])
    coord_source = np.where(
        attrs["util_lat"].notna(),
        "util",
        np.where(attrs["basin_lat"].notna(), "basin", "none"),
    )

    valid = lat.notna() & lon.notna()
    n_missing = (~valid).sum()
    if n_missing:
        print(f"  {n_missing} substations have no coordinates — excluded")

    gdf = gpd.GeoDataFrame(
        attrs[valid].assign(
            lat=lat[valid],
            lon=lon[valid],
            coord_source=coord_source[valid],
        ),
        geometry=[Point(lo, la) for lo, la in zip(lon[valid], lat[valid])],
        crs="EPSG:4326",
    )
    print(f"Loaded {len(gdf)} substations with coordinates "
          f"(util={int((coord_source[valid] == 'util').sum())}, "
          f"basin={int((coord_source[valid] == 'basin').sum())})")
    return gdf


def _load_ca_counties() -> gpd.GeoDataFrame:
    counties = gpd.read_file(COUNTY_SHP)
    ca = counties[counties["STATEFP"] == "06"].to_crs("EPSG:4326").copy()
    print(f"Loaded {len(ca)} California county polygons (TIGER/Line 2022)")
    return ca


def _spatial_join(subs: gpd.GeoDataFrame, ca_cnty: gpd.GeoDataFrame) -> pd.DataFrame:
    joined = gpd.sjoin(
        subs,
        ca_cnty[["GEOID", "NAME", "geometry"]],
        how="left",
        predicate="within",
    )

    # Nearest-centroid fallback for substations that missed all polygons
    missed = joined["GEOID"].isna()
    n_missed = missed.sum()
    if n_missed:
        print(f"  {n_missed} substations outside all polygons — nearest-centroid fallback")
        ca_alb   = ca_cnty.to_crs("EPSG:3310")
        subs_alb = subs[missed].to_crs("EPSG:3310")
        nearest  = gpd.sjoin_nearest(
            subs_alb,
            ca_alb[["GEOID", "NAME", "geometry"]],
            how="left",
            distance_col="_dist_m",
        )
        joined.loc[missed, "GEOID"] = nearest["GEOID"].values
        joined.loc[missed, "NAME"]  = nearest["NAME"].values

    n_assigned = joined["GEOID"].notna().sum()
    print(f"  {n_assigned} / {len(subs)} substations assigned to a county")
    return pd.DataFrame(joined.drop(columns=["geometry", "index_right"], errors="ignore"))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    subs   = _load_substations()
    ca_cty = _load_ca_counties()
    ref    = pd.read_csv(COUNTY_REF)

    result = _spatial_join(subs, ca_cty)

    # GEOID is a 5-digit string like "06037"; fips_key = "p06037"
    result["fips_key"] = "p" + result["GEOID"].fillna("")
    result["fips_int"] = result["GEOID"].fillna("0").astype(int)

    # Merge county reference (p_region, load fraction, BTM PV capacities)
    btm_cols = [c for c in ref.columns if c.startswith("btm_pv_")]
    keep_ref = ["fips_key", "p_region", "ca_load_fraction"] + btm_cols
    result = result.merge(ref[keep_ref], on="fips_key", how="left")

    out_cols = (
        ["utility", "substation_name", "lat", "lon", "coord_source",
         "fips_int", "fips_key", "NAME", "p_region", "ca_load_fraction"]
        + btm_cols
    )
    out_cols = [c for c in out_cols if c in result.columns]
    result = result[out_cols].rename(columns={"NAME": "county_name"})

    out_path = OUT / "substation_county_reeds_mapping.csv"
    result.to_csv(out_path, index=False)
    print(f"\nWrote {out_path.relative_to(ROOT)}  ({len(result)} rows × {len(result.columns)} cols)")

    # Summary
    print(f"\nSubstation counts by ReEDS p-region:")
    by_region = (result.groupby("p_region", dropna=False)
                 .size()
                 .reset_index(name="n_substations")
                 .sort_values("p_region"))
    print(by_region.to_string(index=False))

    n_unmatched = result["p_region"].isna().sum()
    if n_unmatched:
        print(f"  WARNING: {n_unmatched} substations unmatched to a county reference row")


if __name__ == "__main__":
    main()
