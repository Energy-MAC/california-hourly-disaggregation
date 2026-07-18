"""
compare_lcr_pge.py

Extract the PG&E Local Capacity Area Substation List (PDF) to CSV and
compare it against two reference datasets:

  A) DataBasin CA Substations 2022
       data/processed/substation_misc/ca_substations_2022.csv
       owner_std == 'pge', type == 'SUBSTATION'

  B) Our processed PGE substation attributes (clean)
       data/processed/substations/substation_attributes_clean.csv
       utility == 'pge'

PDF structure (data/raw/PotentialData/lcr/lcr-substation-list.pdf)
-------------------------------------------------------------------
  Source: PG&E Local Capacity Area Substation List, based on 12/1/2025
  32 pages; ~1,700 rows total.
  5 columns, fixed-x positions across all pages:
    SAP Substation Name  x0 <  287
    LCR Area             287 <= x0 < 386
    City served          386 <= x0 < 588
    Division             588 <= x0 < 669
    Owner                x0 >= 669
  Names use uppercase with "SUB" suffix (e.g., "ALMADEN SUB").
  The same substation name may appear on multiple rows with different
  cities (one substation serving multiple cities is expected).

Name normalisation for join
---------------------------
  LCR names → strip trailing " SUB" and whitespace → uppercase
  This maps "ALMADEN SUB" → "ALMADEN" to match our PGE column
  (substation_name = "ALMADEN") and Basin (name = "Almaden" → lowercased).
  A residual " SUB" after stripping may remain for compound suffixes
  (e.g., "AG WISHON PH SUB" → "AG WISHON PH"); the fuzzy pass handles these.

Outputs
-------
  data/raw/PotentialData/lcr/lcr_substation_list.csv
      All extracted rows (sap_substation_name, lcr_area, city_served,
      division, owner).

  data/checks/compare_lcr_pge/lcr_unique_substations.csv
      One row per unique SAP Substation Name (collapsed city lists).

  data/checks/compare_lcr_pge/lcr_basin_join.csv
      LCR unique substations joined to Basin PGE records.

  data/checks/compare_lcr_pge/lcr_our_pge_join.csv
      LCR unique substations joined to our clean PGE attributes.

  data/figures/substation_maps/lcr_comparison.png
      Two-panel coverage map.
"""
from __future__ import annotations

import re
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pdfplumber
from rapidfuzz import process as fuzz_process, fuzz

# ── Config ────────────────────────────────────────────────────────────────────

ROOT     = Path(__file__).resolve().parents[2]
PDF_IN   = ROOT / "data" / "raw" / "PotentialData" / "lcr" / "lcr-substation-list.pdf"
RAW_OUT  = ROOT / "data" / "raw" / "PotentialData" / "lcr" / "lcr_substation_list.csv"
BASIN    = ROOT / "data" / "processed" / "substation_misc" / "ca_substations_2022.csv"
PGE_CLEAN= ROOT / "data" / "processed" / "substations" / "substation_attributes_clean.csv"
FIGS_DIR = ROOT / "data" / "figures" / "substation_maps"
OUT_DIR  = ROOT / "data" / "checks" / "compare_lcr_pge"

RAW_OUT.parent.mkdir(parents=True, exist_ok=True)
FIGS_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Column x0 boundaries (derived from word positions on p.1 and p.2)
_X_LCR   = 287.0   # LCR Area starts here
_X_CITY  = 386.0   # City served starts here
_X_DIV   = 588.0   # Division starts here
_X_OWN   = 669.0   # Owner starts here

# Fuzzy match threshold (0–100, token_sort_ratio)
FUZZY_THRESHOLD = 75

_CA_LON  = (-124.5, -114.0)
_CA_LAT  = (32.5,    42.0)


# ── PDF extraction ─────────────────────────────────────────────────────────────

def _assign_col(x0: float) -> str:
    if x0 < _X_LCR:
        return "sap"
    if x0 < _X_CITY:
        return "lcr"
    if x0 < _X_DIV:
        return "city"
    if x0 < _X_OWN:
        return "div"
    return "own"


def _is_header(row_words: list[dict]) -> bool:
    texts = {w["text"].upper() for w in row_words}
    return bool(texts & {"SAP", "OWNER"})


def _is_footer(row_words: list[dict]) -> bool:
    texts = [w["text"] for w in row_words]
    joined = " ".join(texts)
    return bool(re.search(r"Page\s+\d+\s+of\s+\d+", joined))


def _is_title(row_words: list[dict]) -> bool:
    texts = [w["text"] for w in row_words]
    return any("Substation" in t and len(row_words) <= 6 for t in texts)


_KNOWN_LCR_AREAS = (
    "Greater Bay Area",
    "Greater Fresno",
    "Humboldt",
    "Kern",
    "North Coast/North Bay",
    "Sierra",
    "Stockton",
)


def _fix_split_greater(sap_name: str, lcr_area: str) -> tuple[str, str]:
    """
    PDF rendering artifact: for very long SAP names characters from the
    adjacent 'Greater …' LCR area token bleed left, e.g.:
      sap='...TRANSPORTATION CGr'  lcr='eater Fresno'
    pdfplumber merges the end of the SAP name with the start of 'Greater'
    into one token ('CGr').  Fix by scanning 1–5 trailing chars of sap_name
    until prepending them to lcr_area reconstructs a known LCR area string.
    """
    if not (lcr_area and lcr_area[0].islower()):
        return sap_name, lcr_area
    for n in range(1, 6):
        if len(sap_name) < n:
            break
        candidate = sap_name[-n:] + lcr_area
        for area in _KNOWN_LCR_AREAS:
            if candidate == area:
                return sap_name[:-n].strip(), area
    return sap_name, lcr_area


def extract_pdf(pdf_path: Path) -> pd.DataFrame:
    """
    Parse all 32 pages and return one row per data line.

    Groups words by rounded top-coordinate (±0.1 pt tolerance) and assigns
    them to one of five columns by x0 position.  Header, footer, title, and
    note-box rows are skipped.

    Edge cases handled:
    - Page 1 header text ('Based on 12/1/2025') spills into SAP column but
      has no LCR area; excluded by requiring a non-empty lcr_area.
    - Very long SAP names (e.g. TORNADO row, p.29) cause 'Gr' from 'Greater'
      to land in the SAP column; _fix_split_greater() reconstructs both fields.
    """
    rows: list[dict] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(keep_blank_chars=False)

            # Group words into lines by top value (round to 1 decimal place)
            lines: dict[float, list[dict]] = {}
            for w in words:
                key = round(w["top"], 1)
                lines.setdefault(key, []).append(w)

            for top_key in sorted(lines):
                line_words = sorted(lines[top_key], key=lambda w: w["x0"])
                if _is_title(line_words) or _is_header(line_words) or _is_footer(line_words):
                    continue

                # Build columns
                col_texts: dict[str, list[str]] = {
                    "sap": [], "lcr": [], "city": [], "div": [], "own": [],
                }
                for w in line_words:
                    col_texts[_assign_col(w["x0"])].append(w["text"])

                sap_name = " ".join(col_texts["sap"]).strip()
                lcr_area = " ".join(col_texts["lcr"]).strip()

                # Skip rows with no SAP name (note-box continuation on p.1)
                if not sap_name:
                    continue
                # Skip rows with no LCR area (header spillage, e.g. "Based on 12/1/2025")
                if not lcr_area:
                    continue

                sap_name, lcr_area = _fix_split_greater(sap_name, lcr_area)

                rows.append({
                    "sap_substation_name": sap_name,
                    "lcr_area":   lcr_area,
                    "city_served":" ".join(col_texts["city"]).strip(),
                    "division":   " ".join(col_texts["div"]).strip(),
                    "owner":      " ".join(col_texts["own"]).strip(),
                    "_page":      page_num,
                })

    return pd.DataFrame(rows)


# ── Name normalisation ─────────────────────────────────────────────────────────

_SUB_SUFFIX = re.compile(r"\s+SUB\s*$", re.IGNORECASE)


def normalize_lcr(name: str) -> str:
    """Strip trailing ' SUB' and whitespace, uppercase."""
    return _SUB_SUFFIX.sub("", name).strip().upper()


def normalize_basin(name: str) -> str:
    return name.strip().upper()


def normalize_pge(name: str) -> str:
    return name.strip().upper()


# ── Fuzzy join ────────────────────────────────────────────────────────────────

def fuzzy_join(
    query_names:  pd.Series,
    ref_names:    pd.Series,
    ref_index:    pd.Index,
    threshold:    int = FUZZY_THRESHOLD,
) -> tuple[np.ndarray, np.ndarray]:
    """
    For each query name, find the best-matching ref name (token_sort_ratio).
    Returns (matched_ref_indices, scores).  Score < threshold → no match (-1 index).
    """
    ref_list  = ref_names.tolist()
    match_idx = np.full(len(query_names), -1, dtype=int)
    scores    = np.zeros(len(query_names))

    for i, q in enumerate(query_names):
        result = fuzz_process.extractOne(q, ref_list, scorer=fuzz.token_sort_ratio)
        if result and result[1] >= threshold:
            scores[i]    = result[1]
            match_idx[i] = ref_list.index(result[0])

    return match_idx, scores


# ── Load reference datasets ───────────────────────────────────────────────────

def load_basin_pge() -> pd.DataFrame:
    df = pd.read_csv(BASIN)
    df = df[(df["owner_std"] == "pge") & (df["type"] == "SUBSTATION")].copy()
    df = df.reset_index(drop=True)
    df["norm_name"] = df["name"].apply(normalize_basin)
    return df


def load_our_pge() -> pd.DataFrame:
    df = pd.read_csv(PGE_CLEAN)
    df = df[df["utility"] == "pge"].copy().reset_index(drop=True)
    df["norm_name"] = df["substation_name"].apply(normalize_pge)
    # Prefer util_lat/lon; fall back to basin_lat/lon
    df["lat"] = df["util_lat"].fillna(df["basin_lat"])
    df["lon"] = df["util_lon"].fillna(df["basin_lon"])
    return df


# ── Comparison logic ──────────────────────────────────────────────────────────

def compare_lcr_basin(
    lcr_unique: pd.DataFrame,
    basin_pge:  pd.DataFrame,
) -> pd.DataFrame:
    """
    Two-stage join: exact normalised name match, then fuzzy for remainders.
    """
    lcr_unique = lcr_unique.copy()
    lcr_unique["norm_name"] = lcr_unique["sap_substation_name"].apply(normalize_lcr)

    # Exact match
    exact_map = dict(zip(basin_pge["norm_name"], basin_pge.index))
    lcr_unique["basin_idx"]   = lcr_unique["norm_name"].map(exact_map).fillna(-1).astype(int)
    lcr_unique["match_type"]  = np.where(lcr_unique["basin_idx"] >= 0, "exact", "none")
    lcr_unique["match_score"] = np.where(lcr_unique["basin_idx"] >= 0, 100, 0).astype(float)

    # Fuzzy for unmatched
    unmatched = lcr_unique["match_type"] == "none"
    if unmatched.sum():
        idx, scores = fuzzy_join(
            lcr_unique.loc[unmatched, "norm_name"],
            basin_pge["norm_name"],
            basin_pge.index,
        )
        lcr_unique.loc[unmatched, "basin_idx"]   = idx
        lcr_unique.loc[unmatched, "match_score"] = scores
        has_fuzzy = unmatched & (lcr_unique["basin_idx"] >= 0)
        lcr_unique.loc[has_fuzzy, "match_type"]  = "fuzzy"
        lcr_unique.loc[unmatched & ~has_fuzzy, "match_type"] = "unmatched"
        lcr_unique.loc[unmatched & ~has_fuzzy, "basin_idx"]  = -1

    # Attach Basin columns
    def _get_basin(col: str) -> pd.Series:
        return lcr_unique["basin_idx"].apply(
            lambda i: basin_pge[col].iloc[i] if i >= 0 else np.nan
        )

    lcr_unique["basin_name"]      = _get_basin("name")
    lcr_unique["basin_norm_name"] = _get_basin("norm_name")
    lcr_unique["basin_lat"]       = _get_basin("latitude")
    lcr_unique["basin_lon"]       = _get_basin("longitude")

    return lcr_unique.drop(columns=["basin_idx", "norm_name"])


def compare_lcr_our_pge(
    lcr_unique: pd.DataFrame,
    our_pge:    pd.DataFrame,
) -> pd.DataFrame:
    """
    Two-stage join: exact normalised name match, then fuzzy for remainders.
    """
    lcr_unique = lcr_unique.copy()
    lcr_unique["norm_name"] = lcr_unique["sap_substation_name"].apply(normalize_lcr)

    exact_map = dict(zip(our_pge["norm_name"], our_pge.index))
    lcr_unique["pge_idx"]    = lcr_unique["norm_name"].map(exact_map).fillna(-1).astype(int)
    lcr_unique["match_type"] = np.where(lcr_unique["pge_idx"] >= 0, "exact", "none")
    lcr_unique["match_score"]= np.where(lcr_unique["pge_idx"] >= 0, 100, 0).astype(float)

    unmatched = lcr_unique["match_type"] == "none"
    if unmatched.sum():
        idx, scores = fuzzy_join(
            lcr_unique.loc[unmatched, "norm_name"],
            our_pge["norm_name"],
            our_pge.index,
        )
        lcr_unique.loc[unmatched, "pge_idx"]    = idx
        lcr_unique.loc[unmatched, "match_score"] = scores
        has_fuzzy = unmatched & (lcr_unique["pge_idx"] >= 0)
        lcr_unique.loc[has_fuzzy, "match_type"] = "fuzzy"
        lcr_unique.loc[unmatched & ~has_fuzzy, "match_type"] = "unmatched"
        lcr_unique.loc[unmatched & ~has_fuzzy, "pge_idx"]    = -1

    def _get_pge(col: str) -> pd.Series:
        return lcr_unique["pge_idx"].apply(
            lambda i: our_pge[col].iloc[i] if i >= 0 else np.nan
        )

    lcr_unique["pge_name"]       = _get_pge("substation_name")
    lcr_unique["pge_norm_name"]  = _get_pge("norm_name")
    lcr_unique["pge_voltage_kv"] = _get_pge("voltage_kv")
    lcr_unique["pge_lat"]        = _get_pge("lat")
    lcr_unique["pge_lon"]        = _get_pge("lon")

    return lcr_unique.drop(columns=["pge_idx", "norm_name"])


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(
    df_raw:     pd.DataFrame,
    lcr_unique: pd.DataFrame,
    basin_join: pd.DataFrame,
    our_join:   pd.DataFrame,
) -> None:
    print(f"\n{'='*65}")
    print("LCR Substation List — Extraction + Comparison Summary")
    print(f"{'='*65}")

    print(f"\nPDF extraction:")
    print(f"  Total rows extracted    : {len(df_raw):,}")
    print(f"  Unique SAP names        : {df_raw['sap_substation_name'].nunique():,}")
    print(f"  Unique LCR areas        : {sorted(df_raw['lcr_area'].unique())}")
    print(f"  Owner breakdown         :\n{df_raw['owner'].value_counts().to_string()}")

    print(f"\nLCR unique substations   : {len(lcr_unique):,}")

    print(f"\n--- A) LCR vs Basin PGE substations ---")
    for mt, grp in basin_join.groupby("match_type"):
        print(f"  {mt:<12}: {len(grp):>4}  ({100*len(grp)/len(basin_join):.1f}%)")
    if "fuzzy" in basin_join["match_type"].values:
        fuzzy = basin_join[basin_join["match_type"] == "fuzzy"]
        print(f"  Fuzzy score distribution:")
        print(f"    {fuzzy['match_score'].describe(percentiles=[.25,.5,.75,.9]).round(1).to_string()}")

    print(f"\n--- B) LCR vs Our PGE substations ---")
    for mt, grp in our_join.groupby("match_type"):
        print(f"  {mt:<12}: {len(grp):>4}  ({100*len(grp)/len(our_join):.1f}%)")
    if "fuzzy" in our_join["match_type"].values:
        fuzzy = our_join[our_join["match_type"] == "fuzzy"]
        print(f"  Fuzzy score distribution:")
        print(f"    {fuzzy['match_score'].describe(percentiles=[.25,.5,.75,.9]).round(1).to_string()}")

    unmatched_our = our_join[our_join["match_type"] == "unmatched"]
    if len(unmatched_our):
        print(f"\n  LCR substations not found in our PGE data ({len(unmatched_our)}):")
        for _, row in unmatched_our.head(30).iterrows():
            print(f"    {row['sap_substation_name']:<40}  owner={row['owner']}  lcr={row['lcr_area']}")
        if len(unmatched_our) > 30:
            print(f"    ... and {len(unmatched_our)-30} more")

    unmatched_basin = basin_join[basin_join["match_type"] == "unmatched"]
    if len(unmatched_basin):
        print(f"\n  LCR substations not found in Basin ({len(unmatched_basin)}):")
        for _, row in unmatched_basin.head(20).iterrows():
            print(f"    {row['sap_substation_name']}")


# ── Figure ─────────────────────────────────────────────────────────────────────

def fig_comparison(
    basin_join: pd.DataFrame,
    our_join:   pd.DataFrame,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(18, 10))

    _COLOR = {"exact": "#2ca02c", "fuzzy": "#ff7f0e", "unmatched": "#d62728"}

    def _setup(ax, title):
        ax.set_xlim(*_CA_LON)
        ax.set_ylim(*_CA_LAT)
        ax.set_aspect("equal")
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.grid(True, alpha=0.25, linewidth=0.5)

    # Panel A: LCR vs Basin — use basin coords for matched, no coords for unmatched
    ax = axes[0]
    _setup(ax, "LCR vs Basin PGE Substations\n(matched = Basin coords)")
    for mt in ["exact", "fuzzy", "unmatched"]:
        grp = basin_join[basin_join["match_type"] == mt].dropna(subset=["basin_lat", "basin_lon"])
        if len(grp):
            ax.scatter(grp["basin_lon"], grp["basin_lat"],
                       s=14, color=_COLOR[mt], alpha=0.7, zorder=3,
                       label=f"{mt} ({len(basin_join[basin_join['match_type']==mt]):,})")
    ax.legend(fontsize=9, loc="lower right")

    # Panel B: LCR vs Our PGE — use pge coords for matched
    ax = axes[1]
    _setup(ax, "LCR vs Our PGE Substations\n(matched = our PGE coords)")
    for mt in ["exact", "fuzzy", "unmatched"]:
        grp = our_join[our_join["match_type"] == mt].dropna(subset=["pge_lat", "pge_lon"])
        if len(grp):
            ax.scatter(grp["pge_lon"], grp["pge_lat"],
                       s=14, color=_COLOR[mt], alpha=0.7, zorder=3,
                       label=f"{mt} ({len(our_join[our_join['match_type']==mt]):,})")
    ax.legend(fontsize=9, loc="lower right")

    n_unique = len(basin_join)
    fig.suptitle(
        f"PG&E LCR Substation List (12/1/2025)  |  {n_unique:,} unique SAP names\n"
        f"Green = exact name match  |  Orange = fuzzy match  |  Red = unmatched",
        fontsize=10, y=1.01,
    )
    plt.tight_layout()
    out = FIGS_DIR / "lcr_comparison.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\nSaved figure: {out.relative_to(ROOT)}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # ── 1. Extract PDF ────────────────────────────────────────────────────────
    print(f"Extracting PDF: {PDF_IN.name} ...")
    df_raw = extract_pdf(PDF_IN)
    print(f"  {len(df_raw):,} rows, {df_raw['sap_substation_name'].nunique():,} unique SAP names")

    df_raw.to_csv(RAW_OUT, index=False)
    print(f"  Saved raw CSV: {RAW_OUT.relative_to(ROOT)}")

    # ── 2. Unique substations (collapse duplicate city rows) ──────────────────
    lcr_unique = (
        df_raw
        .groupby("sap_substation_name", sort=True)
        .agg(
            lcr_area     = ("lcr_area",    lambda x: "; ".join(sorted(x.unique()))),
            city_served  = ("city_served", lambda x: "; ".join(sorted(x.unique()))),
            division     = ("division",    "first"),
            owner        = ("owner",       "first"),
            n_city_rows  = ("city_served", "count"),
        )
        .reset_index()
    )
    print(f"  Unique substations: {len(lcr_unique):,}")

    out_uniq = OUT_DIR / "lcr_unique_substations.csv"
    lcr_unique.to_csv(out_uniq, index=False)
    print(f"  Saved: {out_uniq.relative_to(ROOT)}")

    # ── 3. Load reference datasets ────────────────────────────────────────────
    print("\nLoading reference datasets ...")
    basin_pge = load_basin_pge()
    print(f"  Basin PGE SUBSTATION records : {len(basin_pge):,}")
    our_pge = load_our_pge()
    print(f"  Our PGE clean substations    : {len(our_pge):,}")

    # ── 4. Compare ────────────────────────────────────────────────────────────
    print("\nRunning comparisons ...")
    basin_join = compare_lcr_basin(lcr_unique, basin_pge)
    our_join   = compare_lcr_our_pge(lcr_unique, our_pge)

    basin_out = OUT_DIR / "lcr_basin_join.csv"
    our_out   = OUT_DIR / "lcr_our_pge_join.csv"
    basin_join.to_csv(basin_out, index=False)
    our_join.to_csv(our_out,   index=False)
    print(f"  Saved: {basin_out.relative_to(ROOT)}")
    print(f"  Saved: {our_out.relative_to(ROOT)}")

    # ── 5. Summary + figure ───────────────────────────────────────────────────
    print_summary(df_raw, lcr_unique, basin_join, our_join)

    print("\nGenerating figure ...")
    fig_comparison(basin_join, our_join)

    print("\nDone.")


if __name__ == "__main__":
    main()
