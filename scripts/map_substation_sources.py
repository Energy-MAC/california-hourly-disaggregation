"""
map_substation_sources.py

Maps California substations color-coded by which data sources contain them.

Per-IOU plots (two-panel figures):
  Left panel:  all substations, colored by source overlap; black ring = in cleaned output.
  Right panel: only substations NOT in the cleaned output (same colors, no ring).

  PGE:  loads (pge_layer25), attrs (pge_substation_attributes), basin
  SCE:  scrape loads, bulk loads, attrs_alt (ICA_Layer), attrs_t3 (Table 3), basin
        Note: bulk and T3 have no lat/lon; shown only when also in a coord source.
        SCE substations present only in bulk/T3 (no coord source) are counted but
        cannot be mapped -- the console reports how many are dropped.
  SDGE: loads, attrs, basin

  All per-IOU plots auto-zoom to the data extent.

Additional plots:
  map_cleaned_vs_basin.png   -- cleaned substations colored by distance to basin match
  map_basin_coverage.png     -- basin substations colored by whether they appear in cleaned;
                                cleaned substations with no basin match also shown

Outputs
-------
  data/figures/map_pge_source_overlap.png
  data/figures/map_sce_source_overlap.png
  data/figures/map_sdge_source_overlap.png
  data/figures/map_cleaned_vs_basin.png
  data/figures/map_basin_coverage.png

Usage
-----
  python scripts/map_substation_sources.py
  python scripts/map_substation_sources.py --util pge
  python scripts/map_substation_sources.py --util cleaned
  python scripts/map_substation_sources.py --util basin
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT  = Path(__file__).resolve().parents[1]
RAW   = ROOT / "data" / "raw"
PROC  = ROOT / "data" / "processed"
FIGS  = ROOT / "data" / "figures"
FIGS.mkdir(parents=True, exist_ok=True)

CA_LAT = (32.4, 42.1)
CA_LON = (-124.6, -113.9)

# ── California state outline ──────────────────────────────────────────────────
# Fetched once from the ArcGIS FeatureServer and cached locally.

_CA_GDF: gpd.GeoDataFrame | None = None
_CA_OUTLINE_URL = (
    "https://services1.arcgis.com/jUJYIo9tSA7EHvfZ/arcgis/rest/services"
    "/California_Outline/FeatureServer/0"
    "/query?where=1%3D1&outFields=*&f=geojson"
)
_CA_OUTLINE_CACHE = PROC / "substation_misc" / "ca_outline.geojson"


def _load_ca_outline() -> gpd.GeoDataFrame | None:
    global _CA_GDF
    if _CA_GDF is not None:
        return _CA_GDF
    try:
        if _CA_OUTLINE_CACHE.exists():
            _CA_GDF = gpd.read_file(_CA_OUTLINE_CACHE)
        else:
            print("  Fetching CA outline from ArcGIS ...")
            _CA_GDF = gpd.read_file(_CA_OUTLINE_URL)
            _CA_GDF.to_file(_CA_OUTLINE_CACHE, driver="GeoJSON")
            print(f"  Cached to {_CA_OUTLINE_CACHE.relative_to(ROOT)}")
    except Exception as e:
        print(f"  WARNING: could not load CA outline ({e}); skipping.")
        return None
    return _CA_GDF


def _add_ca_outline(ax: plt.Axes, color: str = "#555555", lw: float = 0.8) -> None:
    """
    Draw the CA state boundary on ax.  Saves and restores the axis limits so
    geopandas.plot() does not override the auto-zoom already in place.
    """
    ca = _load_ca_outline()
    if ca is None:
        return
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    ca.boundary.plot(ax=ax, color=color, linewidth=lw, zorder=2)
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)

# ── Name normalisation ────────────────────────────────────────────────────────

_PT_RE    = re.compile(r"\s+p\.?\s*t\.?\s*$",   re.IGNORECASE)
_SUB_RE   = re.compile(r"\bsubstation\b",        re.IGNORECASE)
_PUNCT_RE = re.compile(r"[/\-,\.&\(\)_#']")
_SPC_RE   = re.compile(r"\s+")


def norm(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    s = s.str.replace(_PT_RE,    "",  regex=True)
    s = s.str.replace(_SUB_RE,   "",  regex=True)
    s = s.str.replace(_PUNCT_RE, " ", regex=True)
    s = s.str.replace(_SPC_RE,   " ", regex=True)
    return s.str.strip().str.lower()


def is_pt(s: pd.Series) -> pd.Series:
    return s.str.contains(r"p\.?\s*t\.?\s*$", case=False, regex=True, na=False)


# ── Color palette ─────────────────────────────────────────────────────────────

_PALETTE = [
    "#2ca02c",  # dark green  — all sources
    "#1f77b4",  # blue
    "#ff7f0e",  # orange
    "#9467bd",  # purple
    "#d62728",  # red
    "#8c564b",  # brown
    "#e377c2",  # pink
    "#7f7f7f",  # gray
    "#bcbd22",  # olive
    "#17becf",  # teal
    "#aec7e8",  # light blue
    "#ffbb78",  # light orange
    "#98df8a",  # light green
    "#ff9896",  # light red
    "#c5b0d5",  # light purple
]


def _assign_colors(categories: list[str]) -> dict[str, str]:
    """Most-sources-in-common category gets the first (green) color."""
    by_count = sorted(categories, key=lambda s: -s.count("&"))
    return {cat: _PALETTE[i % len(_PALETTE)] for i, cat in enumerate(by_count)}


# ── Generic overlap builder ───────────────────────────────────────────────────

def _build_overlap_table(
    sources: dict[str, tuple[pd.DataFrame, str, bool]],
    coord_priority: list[tuple[str, str, str]],
    cleaned_names: set[str] | None = None,
) -> tuple[pd.DataFrame, int]:
    """
    Build one row per unique normalised substation name.
    Returns (table, n_unmapped) where n_unmapped is the count of substations
    that had no coordinate source and were dropped.

    Columns: name_norm, lat, lon, category, in_cleaned, n_sources
    """
    all_norms: dict[str, dict[str, bool]] = {}
    for label, (df, nc, _) in sources.items():
        if df.empty:
            continue
        for n in norm(df[nc].dropna()).unique():
            all_norms.setdefault(n, {})[label] = True

    coord_lookups: dict[str, dict[str, tuple[float, float]]] = {}
    for label, lat_col, lon_col in coord_priority:
        df, nc, _ = sources.get(label, (pd.DataFrame(), "", False))
        if df.empty or lat_col not in df.columns:
            continue
        tmp = df[[nc, lat_col, lon_col]].copy()
        tmp["_n"] = norm(tmp[nc])
        tmp[lat_col] = pd.to_numeric(tmp[lat_col], errors="coerce")
        tmp[lon_col] = pd.to_numeric(tmp[lon_col], errors="coerce")
        tmp = tmp.dropna(subset=[lat_col, lon_col]).drop_duplicates("_n")
        coord_lookups[label] = dict(zip(tmp["_n"], zip(tmp[lat_col], tmp[lon_col])))

    rows = []
    for n, memberships in all_norms.items():
        lat = lon = float("nan")
        for label, _, _ in coord_priority:
            if n in coord_lookups.get(label, {}):
                lat, lon = coord_lookups[label][n]
                break
        cat_parts = [lbl for lbl in sources.keys() if memberships.get(lbl)]
        rows.append({
            "name_norm":  n,
            "lat":        lat,
            "lon":        lon,
            "category":   " & ".join(cat_parts),
            "in_cleaned": (n in cleaned_names) if cleaned_names else False,
            "n_sources":  len(cat_parts),
        })

    df_out = pd.DataFrame(rows)
    n_unmapped = int(df_out["lat"].isna().sum())
    df_out = df_out.dropna(subset=["lat", "lon"]).reset_index(drop=True)
    df_out = df_out[
        df_out["lat"].between(CA_LAT[0], CA_LAT[1]) &
        df_out["lon"].between(CA_LON[0], CA_LON[1])
    ].reset_index(drop=True)
    return df_out, n_unmapped


# ── Plotting helpers ──────────────────────────────────────────────────────────

def _auto_bounds(lats: pd.Series, lons: pd.Series, pad_frac: float = 0.08,
                 min_pad: float = 0.15) -> tuple[tuple, tuple]:
    """Return (lon_bounds, lat_bounds) with fractional padding."""
    lat_min, lat_max = lats.min(), lats.max()
    lon_min, lon_max = lons.min(), lons.max()
    pad_lat = max(min_pad, (lat_max - lat_min) * pad_frac)
    pad_lon = max(min_pad, (lon_max - lon_min) * pad_frac)
    return (lon_min - pad_lon, lon_max + pad_lon), (lat_min - pad_lat, lat_max + pad_lat)


def _scatter_map(
    ax: plt.Axes,
    df: pd.DataFrame,
    color_map: dict[str, str],
    lon_bounds: tuple | None = None,
    lat_bounds: tuple | None = None,
    alpha: float = 0.65,
    s: float = 16,
    highlight_cleaned: bool = True,
) -> None:
    df_sorted = df.sort_values("n_sources")
    for cat, grp in df_sorted.groupby("category"):
        color = color_map.get(cat, "#aaaaaa")
        ax.scatter(grp["lon"], grp["lat"], s=s, color=color, alpha=alpha,
                   linewidths=0, zorder=3, label=f"{cat}  (n={len(grp):,})")

    if highlight_cleaned and "in_cleaned" in df.columns:
        cln = df[df["in_cleaned"]]
        if not cln.empty:
            ax.scatter(cln["lon"], cln["lat"], s=s * 3.5, facecolors="none",
                       edgecolors="black", linewidths=0.5, zorder=4,
                       label=f"in cleaned output  (n={len(cln):,})")

    if lon_bounds:
        ax.set_xlim(lon_bounds)
    if lat_bounds:
        ax.set_ylim(lat_bounds)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, lw=0.3, alpha=0.4)
    _add_ca_outline(ax)


def _make_iou_fig(
    tbl: pd.DataFrame,
    color_map: dict[str, str],
    title_left: str,
    title_right: str,
    out_path: Path,
    figsize: tuple = (16, 9),
    legend_loc: str = "lower left",
    legend_ncol: int = 1,
    legend_fontsize: int = 7,
) -> None:
    """
    Two-panel figure:
      Left:  all substations (colored by category, cleaned rings)
      Right: non-cleaned substations only (same colors, no rings)
    Both panels auto-zoom to the data extent.
    """
    lon_bounds, lat_bounds = _auto_bounds(tbl["lat"], tbl["lon"])

    fig, (ax_all, ax_excl) = plt.subplots(1, 2, figsize=figsize)

    # Left: all substations
    _scatter_map(ax_all, tbl, color_map,
                 lon_bounds=lon_bounds, lat_bounds=lat_bounds,
                 highlight_cleaned=True)
    ax_all.set_title(title_left, fontsize=9)
    ax_all.legend(fontsize=legend_fontsize, loc=legend_loc, framealpha=0.85,
                  title="Source combination", title_fontsize=legend_fontsize,
                  ncol=legend_ncol)

    # Right: non-cleaned only
    excl = tbl[~tbl["in_cleaned"]].copy()
    lon_b2, lat_b2 = _auto_bounds(excl["lat"], excl["lon"]) if not excl.empty else (lon_bounds, lat_bounds)
    _scatter_map(ax_excl, excl, color_map,
                 lon_bounds=lon_b2, lat_bounds=lat_b2,
                 highlight_cleaned=False)
    ax_excl.set_title(title_right, fontsize=9)
    ax_excl.legend(fontsize=legend_fontsize, loc=legend_loc, framealpha=0.85,
                   title="Source combination", title_fontsize=legend_fontsize,
                   ncol=legend_ncol)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"Saved {out_path.relative_to(ROOT)}")


# ── Console summary ───────────────────────────────────────────────────────────

def _print_summary(tbl: pd.DataFrame, label: str, n_unmapped: int = 0) -> None:
    print(f"\n{label} source overlap summary ({len(tbl):,} mapped, {n_unmapped:,} unmapped/no-coord):")
    summary = (tbl.groupby("category")
                   .agg(count=("name_norm", "size"), cleaned=("in_cleaned", "sum"))
                   .sort_values("count", ascending=False)
                   .reset_index())
    for _, row in summary.iterrows():
        print(f"  {row['category']:<55}  n={row['count']:>4}  cleaned={int(row['cleaned']):>4}")


# ── PGE ──────────────────────────────────────────────────────────────────────

def map_pge() -> None:
    pge_loads_df = pd.read_csv(RAW / "pge" / "pge_layer25_earliest_latest_part001.csv",
                               low_memory=False)
    pge_attrs_df = pd.read_csv(RAW / "pge" / "pge_substation_attributes.csv",
                               low_memory=False)
    basin_df     = pd.read_csv(PROC / "substation_misc" / "ca_substations_2022.csv")
    basin_pge    = basin_df[basin_df["owner_std"] == "pge"].copy()
    clean_df     = pd.read_csv(PROC / "substations" / "substation_attributes_clean.csv")
    cleaned_pge  = set(norm(clean_df[clean_df["utility"] == "pge"]["substation_name"]))

    pge_loads_df = pge_loads_df[~is_pt(pge_loads_df["subname"])].copy()

    sources = {
        "pge_loads": (pge_loads_df, "subname",         True),
        "pge_attrs": (pge_attrs_df, "substation_name", True),
        "basin":     (basin_pge,    "name",             True),
    }
    coord_priority = [
        ("pge_loads", "latitude",  "longitude"),
        ("pge_attrs", "latitude",  "longitude"),
        ("basin",     "latitude",  "longitude"),
    ]

    tbl, n_unmapped = _build_overlap_table(sources, coord_priority, cleaned_pge)
    color_map = _assign_colors(tbl["category"].unique().tolist())

    n_cleaned = tbl["in_cleaned"].sum()
    n_excl    = (~tbl["in_cleaned"]).sum()
    _make_iou_fig(
        tbl, color_map,
        title_left=(
            f"PGE — all substations by source overlap\n"
            f"Black ring = in cleaned output  |  total: {len(tbl):,}"
        ),
        title_right=(
            f"PGE — NOT in cleaned output (n={n_excl:,})\n"
            f"(cleaned={n_cleaned:,} of {len(tbl):,} total)"
        ),
        out_path=FIGS / "map_pge_source_overlap.png",
    )
    _print_summary(tbl, "PGE", n_unmapped)


# ── SCE ──────────────────────────────────────────────────────────────────────

def map_sce() -> None:
    sce_combined = pd.read_csv(RAW / "sce" / "sce_combined_raw.csv", low_memory=False)
    sce_scrape   = sce_combined[sce_combined["source"] == "scrape"].copy()
    sce_bulk     = sce_combined[sce_combined["source"] == "bulk"].copy()
    sce_alt      = pd.read_csv(RAW / "sce" / "sce_ica_layer_substations_alt.csv",
                               low_memory=False)
    sce_t3       = pd.read_csv(RAW / "sce" / "sce_substation_attributes.csv",
                               low_memory=False)
    basin_df     = pd.read_csv(PROC / "substation_misc" / "ca_substations_2022.csv")
    basin_sce    = basin_df[basin_df["owner_std"] == "sce"].copy()
    clean_df     = pd.read_csv(PROC / "substations" / "substation_attributes_clean.csv")
    cleaned_sce  = set(norm(clean_df[clean_df["utility"] == "sce"]["substation_name"]))

    sce_scrape = sce_scrape[~is_pt(sce_scrape["SUBSTATION"])].copy()
    sce_bulk   = sce_bulk[~is_pt(sce_bulk["SUBSTATION"])].copy()

    sources = {
        "scrape":    (sce_scrape, "SUBSTATION",       True),
        "bulk":      (sce_bulk,   "SUBSTATION",       False),
        "attrs_alt": (sce_alt,    "SUB_NAME",         True),
        "attrs_t3":  (sce_t3,     "substation_name",  False),
        "basin":     (basin_sce,  "name",             True),
    }
    # bulk and attrs_t3 have no lat/lon; coords come from scrape, attrs_alt, or basin only.
    coord_priority = [
        ("scrape",    "latitude", "longitude"),
        ("attrs_alt", "latitude", "longitude"),
        ("basin",     "latitude", "longitude"),
    ]

    tbl, n_unmapped = _build_overlap_table(sources, coord_priority, cleaned_sce)
    color_map = _assign_colors(tbl["category"].unique().tolist())

    n_cleaned = tbl["in_cleaned"].sum()
    n_excl    = (~tbl["in_cleaned"]).sum()

    print(f"  SCE: {n_unmapped:,} substations have no coord source (bulk/T3 only) and cannot be mapped.")

    _make_iou_fig(
        tbl, color_map,
        title_left=(
            f"SCE — all substations by source overlap\n"
            f"Black ring = in cleaned output  |  total mapped: {len(tbl):,}  "
            f"(+{n_unmapped:,} unmapped: bulk/T3 only)\n"
            f"bulk & T3 have no lat/lon; plotted only when also in scrape/alt/basin"
        ),
        title_right=(
            f"SCE — NOT in cleaned output (n={n_excl:,})\n"
            f"(cleaned={n_cleaned:,} of {len(tbl):,} mapped)"
        ),
        out_path=FIGS / "map_sce_source_overlap.png",
        legend_fontsize=6,
    )
    _print_summary(tbl, "SCE", n_unmapped)


# ── SDGE ─────────────────────────────────────────────────────────────────────

def map_sdge() -> None:
    failures    = set(
        pd.read_csv(RAW / "sdge" / "sdge_substation_profiles_failed.csv")
        ["substation_name"].str.upper().str.strip()
    )
    sdge_loads  = pd.read_csv(RAW / "sdge" / "sdge_substation_profiles_part001.csv",
                              dtype=str, low_memory=False)
    sdge_attrs  = pd.read_csv(RAW / "sdge" / "sdge_substation_attributes.csv",
                              low_memory=False)
    basin_df    = pd.read_csv(PROC / "substation_misc" / "ca_substations_2022.csv")
    basin_sdge  = basin_df[basin_df["owner_std"] == "sdge"].copy()
    clean_df    = pd.read_csv(PROC / "substations" / "substation_attributes_clean.csv")
    cleaned_sdge = set(norm(clean_df[clean_df["utility"] == "sdge"]["substation_name"]))

    sdge_loads = sdge_loads[~sdge_loads["AssetName"].str.upper().str.strip().isin(failures)]
    sdge_loads = sdge_loads[~is_pt(sdge_loads["AssetName"])].copy()
    sdge_attrs = sdge_attrs[~sdge_attrs["substation_name"].str.upper().str.strip().isin(failures)]
    sdge_attrs = sdge_attrs[~is_pt(sdge_attrs["substation_name"])].copy()

    sources = {
        "sdge_loads": (sdge_loads, "AssetName",       True),
        "sdge_attrs": (sdge_attrs, "substation_name", True),
        "basin":      (basin_sdge, "name",            True),
    }
    coord_priority = [
        ("sdge_loads", "latitude", "longitude"),
        ("sdge_attrs", "latitude", "longitude"),
        ("basin",      "latitude", "longitude"),
    ]

    tbl, n_unmapped = _build_overlap_table(sources, coord_priority, cleaned_sdge)
    color_map = _assign_colors(tbl["category"].unique().tolist())

    n_cleaned = tbl["in_cleaned"].sum()
    n_excl    = (~tbl["in_cleaned"]).sum()
    _make_iou_fig(
        tbl, color_map,
        title_left=(
            f"SDGE — all substations by source overlap\n"
            f"Black ring = in cleaned output  |  total: {len(tbl):,}"
        ),
        title_right=(
            f"SDGE — NOT in cleaned output (n={n_excl:,})\n"
            f"(cleaned={n_cleaned:,} of {len(tbl):,} total)"
        ),
        out_path=FIGS / "map_sdge_source_overlap.png",
        figsize=(14, 7),
    )
    _print_summary(tbl, "SDGE", n_unmapped)


# ── Cleaned vs Basin ─────────────────────────────────────────────────────────

def map_cleaned_vs_basin() -> None:
    clean = pd.read_csv(PROC / "substations" / "substation_attributes_clean.csv")

    has_util  = clean["util_lat"].notna() & clean["util_lon"].notna()
    has_basin = clean["basin_lat"].notna() & clean["basin_lon"].notna()

    dist = clean["dist_to_basin_km"]
    cats = pd.Series("util only (no basin match)", index=clean.index)
    cats[has_basin & (dist <= 1)]                   = "basin match <=1 km"
    cats[has_basin & (dist >  1) & (dist <=  5)]   = "basin match 1-5 km"
    cats[has_basin & (dist >  5) & (dist <= 20)]   = "basin match 5-20 km"
    cats[has_basin & (dist > 20)]                   = "basin match >20 km"
    clean["cat"] = cats

    cat_colors = {
        "basin match <=1 km":         "#2ca02c",
        "basin match 1-5 km":         "#1f77b4",
        "basin match 5-20 km":        "#ff7f0e",
        "basin match >20 km":         "#d62728",
        "util only (no basin match)":  "#7f7f7f",
    }

    util_labels = ["pge", "sce", "sdge"]
    titles = ["PGE", "SCE", "SDGE"]

    fig, axes = plt.subplots(1, 3, figsize=(18, 9))
    for ax, util, title in zip(axes, util_labels, titles):
        sub = clean[(clean["utility"] == util) & has_util].copy()
        sub_ca = sub[
            sub["util_lat"].between(CA_LAT[0], CA_LAT[1]) &
            sub["util_lon"].between(CA_LON[0], CA_LON[1])
        ]
        for cat, grp in sub_ca.groupby("cat"):
            color = cat_colors.get(cat, "#aaaaaa")
            ax.scatter(grp["util_lon"], grp["util_lat"], s=16,
                       color=color, alpha=0.7, linewidths=0, zorder=3,
                       label=f"{cat}  (n={len(grp):,})")

        lon_bounds, lat_bounds = _auto_bounds(sub_ca["util_lat"], sub_ca["util_lon"])
        ax.set_xlim(lon_bounds)
        ax.set_ylim(lat_bounds)
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.set_aspect("equal", adjustable="box")
        ax.grid(True, lw=0.3, alpha=0.4)
        _add_ca_outline(ax)
        ax.set_title(f"{title}\n(n={len(sub_ca):,} with util coords)", fontsize=9)
        ax.legend(fontsize=6, loc="lower left", framealpha=0.85, title_fontsize=6)

    fig.suptitle(
        "Cleaned substation attributes vs DataBasin 2022 reference\n"
        "Colored by name-matched basin coordinate distance",
        fontsize=11,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    out = FIGS / "map_cleaned_vs_basin.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Saved {out.relative_to(ROOT)}")

    print("\nCleaned vs Basin summary:")
    for util in util_labels:
        sub = clean[clean["utility"] == util]
        print(f"\n  {util.upper()}:")
        for cat, cnt in sub.groupby("cat").size().sort_values(ascending=False).items():
            print(f"    {cat:<35}: {cnt:>4}")


# ── Basin coverage (basin-centric view) ──────────────────────────────────────

def map_basin_coverage() -> None:
    """
    Show all basin substations for each IOU, colored by whether a cleaned
    substation with a matching normalised name exists.  Cleaned substations
    that have NO basin name match are plotted as a third category.
    """
    basin_df = pd.read_csv(PROC / "substation_misc" / "ca_substations_2022.csv")
    clean_df = pd.read_csv(PROC / "substations" / "substation_attributes_clean.csv")

    util_map = {"pge": "pge", "sce": "sce", "sdge": "sdge"}
    titles   = {"pge": "PGE", "sce": "SCE", "sdge": "SDGE"}

    # Colors
    COL_MATCHED   = "#2ca02c"   # basin point that matches a cleaned substation
    COL_UNMATCHED = "#d62728"   # basin point NOT in cleaned
    COL_NO_BASIN  = "#7f7f7f"   # cleaned substation with no basin match

    fig, axes = plt.subplots(1, 3, figsize=(18, 9))

    for ax, util in zip(axes, ["pge", "sce", "sdge"]):
        owner_std  = util_map[util]
        basin_sub  = basin_df[basin_df["owner_std"] == owner_std].copy()
        clean_sub  = clean_df[clean_df["utility"] == util].copy()

        basin_sub["_norm"] = norm(basin_sub["name"])
        clean_sub["_norm"] = norm(clean_sub["substation_name"])

        cleaned_set = set(clean_sub["_norm"])

        basin_sub_ca = basin_sub[
            basin_sub["latitude"].between(CA_LAT[0], CA_LAT[1]) &
            basin_sub["longitude"].between(CA_LON[0], CA_LON[1])
        ]

        in_cleaned   = basin_sub_ca[basin_sub_ca["_norm"].isin(cleaned_set)]
        not_in_clean = basin_sub_ca[~basin_sub_ca["_norm"].isin(cleaned_set)]

        # Basin points matched to cleaned
        if not in_cleaned.empty:
            ax.scatter(in_cleaned["longitude"], in_cleaned["latitude"],
                       s=18, color=COL_MATCHED, alpha=0.7, linewidths=0, zorder=3,
                       label=f"basin, in cleaned  (n={len(in_cleaned):,})")

        # Basin points NOT in cleaned
        if not not_in_clean.empty:
            ax.scatter(not_in_clean["longitude"], not_in_clean["latitude"],
                       s=18, color=COL_UNMATCHED, alpha=0.6, linewidths=0, zorder=3,
                       label=f"basin, NOT in cleaned  (n={len(not_in_clean):,})")

        # Cleaned substations with no basin name match (use util coords)
        no_basin_match = clean_sub[
            ~clean_sub["_norm"].isin(set(basin_sub["_norm"])) &
            clean_sub["util_lat"].notna() & clean_sub["util_lon"].notna() &
            clean_sub["util_lat"].between(CA_LAT[0], CA_LAT[1]) &
            clean_sub["util_lon"].between(CA_LON[0], CA_LON[1])
        ]
        if not no_basin_match.empty:
            ax.scatter(no_basin_match["util_lon"], no_basin_match["util_lat"],
                       s=30, color=COL_NO_BASIN, alpha=0.7, marker="^",
                       linewidths=0, zorder=4,
                       label=f"cleaned, no basin match  (n={len(no_basin_match):,})")

        # Auto-zoom to basin extent for this utility
        all_lats = pd.concat([basin_sub_ca["latitude"],
                               no_basin_match["util_lat"]]).dropna()
        all_lons = pd.concat([basin_sub_ca["longitude"],
                               no_basin_match["util_lon"]]).dropna()
        if not all_lats.empty:
            lon_bounds, lat_bounds = _auto_bounds(all_lats, all_lons)
            ax.set_xlim(lon_bounds)
            ax.set_ylim(lat_bounds)

        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.set_aspect("equal", adjustable="box")
        ax.grid(True, lw=0.3, alpha=0.4)
        _add_ca_outline(ax)
        ax.set_title(
            f"{titles[util]}\n"
            f"basin total: {len(basin_sub_ca):,}  |  cleaned: {len(clean_sub):,}",
            fontsize=9,
        )
        ax.legend(fontsize=7, loc="lower left", framealpha=0.85, title_fontsize=7)

        print(f"\n  {titles[util]} basin coverage:")
        print(f"    basin total (CA bbox):  {len(basin_sub_ca):,}")
        print(f"    basin matched cleaned:  {len(in_cleaned):,}")
        print(f"    basin NOT in cleaned:   {len(not_in_clean):,}")
        print(f"    cleaned, no basin match: {len(no_basin_match):,}")

    fig.suptitle(
        "DataBasin 2022 substations vs cleaned output\n"
        "Green = basin point has name match in cleaned;  "
        "Red = basin point absent from cleaned;  "
        "Gray triangle = cleaned substation with no basin name match",
        fontsize=10,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    out = FIGS / "map_basin_coverage.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"\nSaved {out.relative_to(ROOT)}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--util", default="all",
                        help="pge | sce | sdge | cleaned | basin | all (default: all)")
    args = parser.parse_args()
    which = args.util.lower()

    if which in ("pge", "all"):
        print("Mapping PGE ...")
        map_pge()

    if which in ("sce", "all"):
        print("Mapping SCE ...")
        map_sce()

    if which in ("sdge", "all"):
        print("Mapping SDGE ...")
        map_sdge()

    if which in ("cleaned", "all"):
        print("Mapping cleaned vs basin ...")
        map_cleaned_vs_basin()

    if which in ("basin", "all"):
        print("Mapping basin coverage ...")
        map_basin_coverage()

    print("\nDone.")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
