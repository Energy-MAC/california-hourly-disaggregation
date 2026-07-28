"""Shared substation structural-feature assembly for the ML drivers.

Single source of truth for per-substation features so `predict_substation_load.py`
(cell-level cross-sectional model) and `impute_unscraped_load.py` (magnitude x
shape imputation) can never drift on what a feature means. Provides the same
structural feature frame for two populations:

  scraped_structural()      -- the 1,347 substations we HAVE profiles for
                               (training), keyed (utility, substation_name).
  unscraped_structural(u)   -- the load-eligible unscraped CEC substations we
                               want to impute onto, from cec_unscraped_{u}.csv.

Both expose the identical IMPUTABLE columns (location, voltage class, county
population / load-fraction / BTM-PV, utility & p_region one-hots). The rich
SCE-only attributes (RICH_ATTRS) are attached to scraped substations only --
they exist for essentially no unscraped target (2%), so they can be used for a
ceiling study but never to impute.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/load_projection/nodal"))
from map_loads_to_nodes import band_to_cats_class  # noqa: E402

ATTR_FILE = ROOT / "data/processed/substations/substation_attributes_clean.csv"
COUNTY_MAP = ROOT / "data/processed/substations/substation_county_reeds_mapping.csv"
COUNTY_REF = ROOT / "data/processed/reeds/county_ca_reference.csv"
POP_FILE = ROOT / "data/raw/reeds/ReEDS-2.0/inputs/disaggregation/county_population.csv"
AUDIT_DIR = ROOT / "data/checks/substation_coverage_audit"

# county-level structural features (available for ANY substation via its county)
COUNTY_FEATURES = ["county_population", "ca_load_fraction", "btm_pv_2024_mw"]
# SCE-only rich attributes (ceiling study only; absent for imputation targets)
RICH_ATTRS = ["voltage_kv", "circuit_count", "existing_gen", "queued_gen", "total_gen",
              "projected_load", "der_penetration", "max_remain_cap",
              "res_pct", "com_pct", "agr_pct", "ind_pct", "other_pct"]

_STRUCT_NUMERIC = ["lat", "lon", "highside_kv", "sub_kv_class", *COUNTY_FEATURES]
_ONEHOT = ["util_pge", "util_sce", "util_sdge", "preg_p9", "preg_p10", "preg_p11"]
# per-substation imputable feature set (no calendar -- magnitude is calendar-agnostic)
IMPUTABLE_STRUCT = _STRUCT_NUMERIC + _ONEHOT


def feature_tiers() -> dict[str, list[str]]:
    """Ceiling-study feature tiers (see plan): imputable-only, rich without the
    near-circular projected_load, and full rich."""
    rich = IMPUTABLE_STRUCT + RICH_ATTRS
    return {
        "imputable": list(IMPUTABLE_STRUCT),
        "rich_no_projected": [c for c in rich if c != "projected_load"],
        "rich": rich,
    }


def _add_onehots(df: pd.DataFrame) -> pd.DataFrame:
    for u in ["pge", "sce", "sdge"]:
        df[f"util_{u}"] = (df.utility == u).astype(float)
    for p in ["p9", "p10", "p11"]:
        df[f"preg_{p}"] = (df.p_region == p).astype(float)
    return df


def _county_features_by_name(county_raw: pd.Series) -> pd.DataFrame:
    """Map raw CEC county strings ('Los Angeles County') -> CA county structural
    features (ca_load_fraction, BTM-PV, population). Out-of-CA border counties
    (Clark/Nye/La Paz) find no match and get NaN (trees tolerate it)."""
    ref = pd.read_csv(COUNTY_REF)
    pop = pd.read_csv(POP_FILE).rename(columns={"FIPS": "fips_key", "value": "county_population"})
    ref = ref.merge(pop, on="fips_key", how="left")
    ref["key"] = ref.county_name.str.lower().str.strip()
    lut = ref.set_index("key")[["ca_load_fraction", "btm_pv_2024_mw", "county_population"]]
    key = county_raw.str.lower().str.replace(r"\s*county\s*$", "", regex=True).str.strip()
    return lut.reindex(key.values).reset_index(drop=True)


def scraped_structural(utility: str | None = None) -> pd.DataFrame:
    """Per-substation structural features for substations we have profiles for.
    Keyed (utility, substation_name). Includes RICH_ATTRS (SCE-populated)."""
    cmap = pd.read_csv(COUNTY_MAP)
    cmap["utility"] = cmap.utility.str.lower()
    pop = pd.read_csv(POP_FILE).rename(columns={"FIPS": "fips_key", "value": "county_population"})
    cmap = cmap.merge(pop, on="fips_key", how="left")
    cols = ["utility", "substation_name", "lat", "lon", "p_region",
            "ca_load_fraction", "btm_pv_2024_mw", "county_population"]
    df = cmap[cols].copy()

    attrs = pd.read_csv(ATTR_FILE)
    attrs["utility"] = attrs.utility.str.lower()
    attrs["sub_kv_class"] = attrs.highside_kv.map(band_to_cats_class)
    df = df.merge(attrs[["utility", "substation_name", "highside_kv", "sub_kv_class", *RICH_ATTRS]],
                  on=["utility", "substation_name"], how="left")
    df = _add_onehots(df)
    if utility:
        df = df[df.utility == utility].reset_index(drop=True)
    return df


def unscraped_structural(utility: str) -> pd.DataFrame:
    """Per-substation structural features for the load-eligible unscraped CEC
    substations of one utility (imputation targets). Same IMPUTABLE_STRUCT
    columns as scraped_structural; RICH_ATTRS are absent (left as NaN)."""
    u = pd.read_csv(AUDIT_DIR / f"cec_unscraped_{utility}.csv")
    u = u[(u.category == "substation") & (~u.matched_to_scrape)
          & (u.max_voltage_kv.fillna(0) < 500)].copy()
    df = pd.DataFrame({
        "utility": utility,
        "substation_name": u["name"].values,
        "lat": u.latitude.values, "lon": u.longitude.values,
        "highside_kv": u.max_voltage_kv.values,
        "sub_kv_class": u.max_voltage_kv.map(band_to_cats_class).values,
        "county_raw": u.county.values,
        "p_region": pd.NA,  # not directly known; p_region one-hots default 0
    })
    cf = _county_features_by_name(u.county)
    for c in COUNTY_FEATURES:
        df[c] = cf[c].values
    df = _add_onehots(df)
    for c in RICH_ATTRS:
        df[c] = float("nan")
    return df.reset_index(drop=True)
