"""
Process EIA 930 data into standardized CSVs.

Outputs
-------
    data/processed/eia/eia_interchange.csv
        Standardized BA-pair interchange (see process_interchange docstring).

    data/processed/eia/eia_region.csv
        Per-BA and CAL-region hourly demand, net generation, total interchange,
        and day-ahead demand forecast, pivoted wide (one row per period+respondent).
        Columns: period, respondent, respondent-name,
                 demand, demand_forecast, net_gen, total_interchange
        Sources combined:
          eia_rto-region-data_BANC-CISO-*  (per-BA; may be a partial scrape)
          eia_rto-region-data_CAL_*        (CAL aggregate)

Interchange transformation rules
----------------------------------
    FROM records                  : kept as-is (fromba in CA8 already)
    TO records where fromba in CA8: duplicate of FROM — dropped
    TO records where fromba not in CA8: inverted so CA BA becomes fromba:
        new fromba  = old toba     (the CA BA)
        new toba    = old fromba   (the outside BA)
        new value   = -old value   (sign flipped: outside-BA perspective -> CA-BA)

Both FROM and TO files are trimmed to the earlier endpoint before merging so that
every period has symmetric coverage in both directions.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "eia"
OUT_DIR = Path(__file__).resolve().parents[3] / "data" / "processed" / "eia"

CA8 = frozenset({"BANC", "CISO", "IID", "LDWP", "NEVP", "PACW", "TIDC", "WALC"})

_KEEP = ["period", "fromba", "fromba-name", "toba", "toba-name", "value", "value-units"]


def _load_parts(glob: str) -> pd.DataFrame:
    paths = sorted(RAW_DIR.glob(glob))
    if not paths:
        raise FileNotFoundError(f"No files matched {RAW_DIR / glob}")
    print(f"  {len(paths)} file(s): {glob}")
    return pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)


def process_interchange(out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Loading FROM files ...")
    from_df = _load_parts("*_from-*")
    print(f"  {len(from_df):,} rows")

    print("Loading TO files ...")
    to_df = _load_parts("*_to-*")
    print(f"  {len(to_df):,} rows")

    # Align time ranges: trim to the last period covered by the shorter file so
    # we never have rows from one direction without the corresponding other direction.
    from_last = from_df["period"].max()
    to_last   = to_df["period"].max()
    cutoff    = min(from_last, to_last)
    if from_last != to_last:
        shorter = "FROM" if from_last < to_last else "TO"
        print(f"\nDate range mismatch: FROM ends {from_last}, TO ends {to_last}.")
        print(f"  Trimming to {shorter} cutoff: {cutoff}")
        from_df = from_df[from_df["period"] <= cutoff]
        to_df   = to_df[to_df["period"] <= cutoff]
        print(f"  FROM after trim: {len(from_df):,} rows")
        print(f"  TO   after trim: {len(to_df):,} rows")

    # FROM records are already in canonical form (fromba in CA8)
    from_std = from_df[_KEEP].copy()

    # TO records where fromba ∉ 8: invert so the CA BA becomes fromba
    outside_mask = ~to_df["fromba"].isin(CA8)
    to_outside = to_df[outside_mask].copy()
    print(f"\nInverting {len(to_outside):,} TO rows where fromba not in CA-8 ...")

    to_inv = pd.DataFrame({
        "period":      to_outside["period"].values,
        "fromba":      to_outside["toba"].values,        # CA BA → fromba
        "fromba-name": to_outside["toba-name"].values,
        "toba":        to_outside["fromba"].values,      # outside BA → toba
        "toba-name":   to_outside["fromba-name"].values,
        "value":       -to_outside["value"].values,      # sign flip
        "value-units": to_outside["value-units"].values,
    })

    result = pd.concat([from_std, to_inv], ignore_index=True)

    # Drop any duplicates that sneak in (shouldn't happen, but guard)
    before = len(result)
    result.drop_duplicates(subset=["period", "fromba", "toba"], keep="first", inplace=True)
    dropped = before - len(result)
    if dropped:
        print(f"  Dropped {dropped:,} duplicate (period, fromba, toba) rows.")

    out_path = out_dir / "eia_interchange.csv"
    result.to_csv(out_path, index=False)
    mb = out_path.stat().st_size / 1024 / 1024
    print(f"\nWrote {len(result):,} rows -> {out_path}  ({mb:.1f} MB)")
    return out_path


# ── EIA region (demand / NG / TI) ────────────────────────────────────────────

# Maps raw EIA type codes to readable column names.
_TYPE_MAP = {
    "D":  "demand",
    "DF": "demand_forecast",
    "NG": "net_gen",
    "TI": "total_interchange",
}


def process_region(out_dir: Path = OUT_DIR) -> Path:
    """
    Pivot EIA 930 region data (demand, net gen, TI, day-ahead forecast) from long
    to wide format and write a single CSV covering all respondents.

    Sources
    -------
    eia_rto-region-data_BANC-CISO-*  — hourly D/DF/NG/TI for each of the 8 CA BAs
    eia_rto-region-data_CAL_*        — same series for the aggregate CAL region

    If the per-BA file is a partial scrape (the scraper was stopped early), only
    the downloaded date range is included.  Re-run the EIA scraper to extend it:
        python scripts/scrape_eia.py rto-region

    Output columns
    --------------
    period, respondent, respondent-name,
    demand, demand_forecast, net_gen, total_interchange
    All values in megawatthours.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    frames = []
    for glob in ["eia_rto-region-data_BANC-CISO-*"]:#, "*region-data_CAL_*"
        paths = sorted(RAW_DIR.glob(glob))
        if not paths:
            print(f"  (no files matched {glob} — skipping)")
            continue
        print(f"  {len(paths)} file(s): {glob}")
        frames.append(pd.concat([pd.read_csv(p) for p in paths], ignore_index=True))

    if not frames:
        raise FileNotFoundError(f"No region data files found in {RAW_DIR}")

    raw = pd.concat(frames, ignore_index=True)
    print(f"  {len(raw):,} rows loaded  [{raw['period'].min()} - {raw['period'].max()}]")

    # Keep only known type codes; drop rows with null value
    raw = raw[raw["type"].isin(_TYPE_MAP)].dropna(subset=["value"]).copy()
    raw["type_label"] = raw["type"].map(_TYPE_MAP)

    wide = raw.pivot_table(
        index=["period", "respondent", "respondent-name"],
        columns="type_label",
        values="value",
        aggfunc="first",
    ).reset_index()
    wide.columns.name = None

    # Ensure all four columns present even if a type is absent in a source
    out_cols = ["period", "respondent", "respondent-name"] + list(_TYPE_MAP.values())
    for col in _TYPE_MAP.values():
        if col not in wide.columns:
            wide[col] = pd.NA
    wide = wide[out_cols].sort_values(["respondent", "period"]).reset_index(drop=True)

    out_path = out_dir / "eia_region.csv"
    wide.to_csv(out_path, index=False)
    mb = out_path.stat().st_size / 1024 / 1024

    print(f"\nWrote {len(wide):,} rows -> {out_path}  ({mb:.1f} MB)")
    print(f"  Respondents : {sorted(wide['respondent'].unique())}")
    print(f"  Period range: {wide['period'].min()} - {wide['period'].max()}")
    return out_path


if __name__ == "__main__":
    process_interchange()
    print()
    print("Processing region data ...")
    process_region()
