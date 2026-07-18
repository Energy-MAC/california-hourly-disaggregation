"""
weights.py — Shared weight computation for substation-level load disaggregation.

Two disaggregation chains are supported:

  IOU chain (IEPR / RESOLVE)
      IOU load → substation
      weight[s, m, h] = max(load_col[s,m,h], 0) / Σ same for all s in IOU

  ReEDS chain (ReEDS historic / projected)
      P-region load → county → substation (two-stage)
      county_pgroup_fraction[c]  = ca_load_fraction[c] / Σ same for counties in p_region
      sub_county_weight[s, m, h] = max(load_col[s,m,h], 0) / Σ same for s in county
      chain_weight[s, m, h]      = county_pgroup_fraction × sub_county_weight

Temporal levels:
  'annual'    – one weight per substation (constant across all hours)
  'monthly'   – weight varies by month, constant within each month's 24 hours
  'hourly'    – weight varies by hour, constant across months
  'monthhour' – weight varies by (month, hour) independently

All weights are broadcast to a (n_subs, 12, 24) numpy array for vectorized application:
    cw_for_hours = matrix[:, months-1, hours]   → (n_subs, n_hours)
    sub_loads    = cw_for_hours * source_load    → (n_subs, n_hours)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


LEVELS = ("annual", "monthly", "hourly", "monthhour")
WEIGHT_COLS = ("min_load", "max_load", "avg_load")


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------

def load_profiles(profiles_csv: str | Path, weight_col: str) -> pd.DataFrame:
    """
    Load the clean substation monthhour profiles and ensure avg_load exists.
    Deduplicates any repeated (substation, month, hour) cells by mean (PGE scraper overlap).

    Returns DataFrame with columns:
        substation_name, utility, month, hour_pst, min_load, max_load, avg_load
    """
    df = pd.read_csv(
        profiles_csv,
        usecols=["substation_name", "utility", "month", "hour_pst", "min_load", "max_load"],
    )
    df = (
        df.groupby(["substation_name", "utility", "month", "hour_pst"])[["min_load", "max_load"]]
        .mean()
        .reset_index()
    )
    df["avg_load"] = (df["min_load"] + df["max_load"]) / 2
    if weight_col not in df.columns:
        raise ValueError(f"weight_col {weight_col!r} not in {list(df.columns)}")
    return df


# ---------------------------------------------------------------------------
# Temporal aggregation
# ---------------------------------------------------------------------------

def aggregate_to_level(
    profiles: pd.DataFrame,
    weight_col: str,
    level: str,
    extra_group_cols: list[str] | None = None,
) -> pd.DataFrame:
    """
    Aggregate profiles to the requested temporal granularity.

    extra_group_cols: additional columns to keep in the groupby (e.g. 'utility', 'county_name').

    Returns a DataFrame with columns:
        substation_name, [extra_group_cols...], [month,] [hour_pst,], weight_col
    """
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}, got {level!r}")

    base = ["substation_name"] + (extra_group_cols or [])

    if level == "annual":
        return profiles.groupby(base)[weight_col].mean().reset_index()
    elif level == "monthly":
        return profiles.groupby(base + ["month"])[weight_col].mean().reset_index()
    elif level == "hourly":
        return profiles.groupby(base + ["hour_pst"])[weight_col].mean().reset_index()
    else:  # monthhour
        return profiles[base + ["month", "hour_pst", weight_col]].copy()


# ---------------------------------------------------------------------------
# Normalization within group
# ---------------------------------------------------------------------------

def normalize_within(
    df: pd.DataFrame,
    group_cols: list[str],
    value_col: str,
) -> pd.DataFrame:
    """
    Add 'weight' column = max(value_col, 0) normalized to sum to 1.0 within each group.
    Falls back to equal weights when all values in a group are <= 0.
    """
    df = df.copy()
    df["_w"] = df[value_col].clip(lower=0).fillna(0)
    grp_sum = df.groupby(group_cols)["_w"].transform("sum")
    grp_n   = df.groupby(group_cols)["_w"].transform("count")
    df["weight"] = np.where(grp_sum > 0, df["_w"] / grp_sum, 1.0 / grp_n)
    return df.drop(columns=["_w"])


# ---------------------------------------------------------------------------
# Broadcast weight table → (n_subs, 12, 24) matrix
# ---------------------------------------------------------------------------

def broadcast_to_matrix(
    weights_df: pd.DataFrame,
    subs: list[str],
    level: str,
) -> np.ndarray:
    """
    Convert a per-substation weight table to a (n_subs, 12, 24) numpy array.

    weights_df must have columns: substation_name, weight, and optionally
    month (1-12) / hour_pst (0-23) depending on level.
    """
    sub_idx = {s: i for i, s in enumerate(subs)}
    mat = np.zeros((len(subs), 12, 24), dtype=np.float64)

    if level == "annual":
        for _, row in weights_df.iterrows():
            si = sub_idx.get(row["substation_name"])
            if si is not None:
                mat[si, :, :] = row["weight"]

    elif level == "monthly":
        for _, row in weights_df.iterrows():
            si = sub_idx.get(row["substation_name"])
            if si is not None:
                mat[si, int(row["month"]) - 1, :] = row["weight"]

    elif level == "hourly":
        for _, row in weights_df.iterrows():
            si = sub_idx.get(row["substation_name"])
            if si is not None:
                mat[si, :, int(row["hour_pst"])] = row["weight"]

    else:  # monthhour — use += in case any (sub, m, h) appears more than once
        for _, row in weights_df.iterrows():
            si = sub_idx.get(row["substation_name"])
            if si is not None:
                mat[si, int(row["month"]) - 1, int(row["hour_pst"])] += row["weight"]

    return mat


# ---------------------------------------------------------------------------
# IOU weight matrices (IEPR / RESOLVE)
# ---------------------------------------------------------------------------

def build_iou_weight_matrices(
    profiles: pd.DataFrame,
    level: str,
    weight_col: str,
    ious: list[str] | None = None,
) -> dict[str, tuple[list[str], np.ndarray]]:
    """
    Build (n_subs, 12, 24) weight matrices for each IOU.

    Each entry represents the substation's fraction of its IOU's total load at each
    (month, hour) cell, broadcast to the full 12×24 grid based on `level`.

    Parameters
    ----------
    profiles : cleaned monthhour profiles with 'utility' column
    level    : 'annual' | 'monthly' | 'hourly' | 'monthhour'
    weight_col : 'min_load' | 'max_load' | 'avg_load'
    ious     : IOU identifiers to process; defaults to all in profiles

    Returns
    -------
    dict: {iou → (sorted_substation_names, weight_matrix[n_subs, 12, 24])}
    """
    if ious is None:
        ious = sorted(profiles["utility"].unique())

    result: dict[str, tuple[list[str], np.ndarray]] = {}

    for iou in ious:
        p = profiles[profiles["utility"] == iou].copy()
        if p.empty:
            continue

        agg = aggregate_to_level(p, weight_col, level, extra_group_cols=["utility"])

        # Determine grouping keys based on level
        group_keys = ["utility"]
        if level in ("monthly", "monthhour"):
            group_keys.append("month")
        if level in ("hourly", "monthhour"):
            group_keys.append("hour_pst")

        normed = normalize_within(agg, group_keys, weight_col)

        subs = sorted(p["substation_name"].unique())
        mat = broadcast_to_matrix(normed, subs, level)
        result[iou] = (subs, mat)

    return result


# ---------------------------------------------------------------------------
# ReEDS two-stage chain matrices (p-region → county → substation)
# ---------------------------------------------------------------------------

def compute_county_pgroup_fractions(county_ref: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize ca_load_fraction within each p_region → pgroup_fraction sums to 1.0.
    """
    cr = county_ref[["fips_int", "county_name", "p_region", "ca_load_fraction"]].copy()
    cr["pgroup_fraction"] = cr.groupby("p_region")["ca_load_fraction"].transform(
        lambda x: x / x.sum() if x.sum() > 0 else 0.0
    )
    return cr


def build_reeds_chain_matrices(
    profiles: pd.DataFrame,
    sub_county: pd.DataFrame,
    county_ref: pd.DataFrame,
    level: str,
    weight_col: str,
    p_regions: list[str] | None = None,
    add_synthetic: bool = True,
) -> dict[str, tuple[list[str], np.ndarray, pd.DataFrame]]:
    """
    Build (n_subs, 12, 24) chain weight matrices for each ReEDS p-region.

    Chain weight = county_pgroup_fraction × sub_county_weight
    Synthetic substations are added for counties with no real substations.

    Parameters
    ----------
    profiles    : monthhour profiles (substation_name, utility, month, hour_pst, weight_col)
    sub_county  : substation → county/p_region mapping
    county_ref  : county → p_region → ca_load_fraction
    level       : temporal aggregation level
    weight_col  : load column for weighting
    p_regions   : p-regions to process; defaults to all in county_ref
    add_synthetic : create placeholder substations for counties with no real substations

    Returns
    -------
    dict: {p_region → (sorted_substation_names, chain_matrix[n_subs, 12, 24], metadata_df)}
    metadata_df has: substation_name, utility, county_name, fips_int, p_region, is_synthetic
    """
    county_weights = compute_county_pgroup_fractions(county_ref)

    # Some substation names (e.g. ALPINE, BARRETT) are genuinely distinct substations
    # owned by different utilities in different counties — join on (substation_name,
    # utility), not substation_name alone, so each is mapped to its own county.
    sc_cols = ["substation_name", "utility", "fips_int", "county_name", "p_region"]
    sc = sub_county[sc_cols].drop_duplicates(["utility", "substation_name"])

    merged = profiles.merge(sc, on=["substation_name", "utility"], how="inner")
    merged = merged.merge(
        county_weights[["fips_int", "pgroup_fraction"]], on="fips_int", how="left"
    )

    if p_regions is None:
        p_regions = sorted(county_weights["p_region"].unique())

    result: dict[str, tuple[list[str], np.ndarray, pd.DataFrame]] = {}

    for p in p_regions:
        p_merged = merged[merged["p_region"] == p].copy()

        # Build sub_county_weight by aggregating to the requested level
        # Group by county (fips_int) only — utility is substation metadata, not a grouping key here
        agg = aggregate_to_level(
            p_merged, weight_col, level,
            extra_group_cols=["fips_int", "county_name", "p_region"]
        )

        group_keys = ["fips_int"]
        if level in ("monthly", "monthhour"):
            group_keys.append("month")
        if level in ("hourly", "monthhour"):
            group_keys.append("hour_pst")

        normed = normalize_within(agg, group_keys, weight_col)
        normed = normed.rename(columns={"weight": "sub_county_weight"})

        # pgroup_fraction was merged into p_merged; re-attach via fips_int
        normed = normed.merge(
            county_weights[["fips_int", "pgroup_fraction"]], on="fips_int", how="left"
        )
        normed["chain_weight"] = normed["pgroup_fraction"] * normed["sub_county_weight"]

        # Attach utility back for metadata (join from original merged table)
        utility_map = (
            p_merged[["substation_name", "utility"]]
            .drop_duplicates("substation_name")
            .set_index("substation_name")["utility"]
        )
        normed["utility"] = normed["substation_name"].map(utility_map).fillna("unknown")
        normed["is_synthetic"] = False

        # Add synthetic substations for counties with no real substations
        if add_synthetic:
            p_counties = county_weights[county_weights["p_region"] == p]
            covered_fips = set(normed["fips_int"].unique())
            missing = p_counties[~p_counties["fips_int"].isin(covered_fips)]
            if not missing.empty:
                normed = _add_synthetic_rows(normed, missing, level)

        # Build ordered substation list and weight matrix
        subs = sorted(normed["substation_name"].unique())
        normed_for_matrix = normed.rename(columns={"chain_weight": "weight"})
        mat = broadcast_to_matrix(normed_for_matrix, subs, level)

        # Metadata (one row per substation)
        meta = (
            normed[["substation_name", "utility", "county_name", "fips_int", "p_region", "is_synthetic"]]
            .drop_duplicates("substation_name")
            .reset_index(drop=True)
        )

        result[p] = (subs, mat, meta)

    return result


def _add_synthetic_rows(
    normed: pd.DataFrame,
    missing_counties: pd.DataFrame,
    level: str,
) -> pd.DataFrame:
    """Add one SYNTHETIC_<COUNTY> row (or rows per temporal cell) for each missing county."""
    mh_grid = pd.MultiIndex.from_product(
        [range(1, 13), range(0, 24)], names=["month", "hour_pst"]
    ).to_frame(index=False)

    rows = []
    for _, cr in missing_counties.iterrows():
        synth = "SYNTHETIC_" + cr["county_name"].upper().replace(" ", "_")

        if level == "annual":
            base = {"substation_name": synth, "utility": "SYNTHETIC",
                    "fips_int": cr["fips_int"], "county_name": cr["county_name"],
                    "p_region": cr["p_region"], "pgroup_fraction": cr["pgroup_fraction"],
                    "sub_county_weight": 1.0, "chain_weight": cr["pgroup_fraction"],
                    "is_synthetic": True}
            rows.append(pd.DataFrame([base]))

        elif level == "monthly":
            for m in range(1, 13):
                rows.append(pd.DataFrame([{
                    "substation_name": synth, "utility": "SYNTHETIC",
                    "fips_int": cr["fips_int"], "county_name": cr["county_name"],
                    "p_region": cr["p_region"], "month": m,
                    "pgroup_fraction": cr["pgroup_fraction"],
                    "sub_county_weight": 1.0, "chain_weight": cr["pgroup_fraction"],
                    "is_synthetic": True,
                }]))

        elif level == "hourly":
            for h in range(24):
                rows.append(pd.DataFrame([{
                    "substation_name": synth, "utility": "SYNTHETIC",
                    "fips_int": cr["fips_int"], "county_name": cr["county_name"],
                    "p_region": cr["p_region"], "hour_pst": h,
                    "pgroup_fraction": cr["pgroup_fraction"],
                    "sub_county_weight": 1.0, "chain_weight": cr["pgroup_fraction"],
                    "is_synthetic": True,
                }]))

        else:  # monthhour
            synth_rows = mh_grid.copy()
            synth_rows["substation_name"] = synth
            synth_rows["utility"] = "SYNTHETIC"
            synth_rows["fips_int"] = int(cr["fips_int"])
            synth_rows["county_name"] = cr["county_name"]
            synth_rows["p_region"] = cr["p_region"]
            synth_rows["pgroup_fraction"] = cr["pgroup_fraction"]
            synth_rows["sub_county_weight"] = 1.0
            synth_rows["chain_weight"] = cr["pgroup_fraction"]
            synth_rows["is_synthetic"] = True
            rows.append(synth_rows)

    if rows:
        normed = normed.copy()
        normed["is_synthetic"] = normed.get("is_synthetic", False)
        normed = pd.concat([normed, *rows], ignore_index=True)

    return normed


# ---------------------------------------------------------------------------
# Vectorized application to a source time series
# ---------------------------------------------------------------------------

def apply_weights_to_series(
    source_df: pd.DataFrame,
    weight_matrices: dict,
    source_col: str,
    month_col: str = "month",
    hour_col: str = "hour",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Apply weight matrices to a source time series and return (annual_df, hourly_batches).

    Parameters
    ----------
    source_df : time series with columns [month_col, hour_col, source_col] plus group identifiers
    weight_matrices : {group → (subs, matrix[n_subs,12,24])} — output of build_iou_weight_matrices
                      or {p_region → (subs, matrix, meta_df)} from build_reeds_chain_matrices
    source_col : load column in source_df
    month_col, hour_col : column names for month (1-12) and hour (0-23)

    Returns (annual_df, list_of_pyarrow_tables)
    """
    # Handled in the calling script for flexibility.
    raise NotImplementedError("Use apply_weights_vectorized in the calling script.")
