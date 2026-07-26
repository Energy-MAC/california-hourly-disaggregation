"""Voltage-source validation for the nodal mapping's --voltage-mode restrict.

Answers three questions before trusting substation high-side voltage as a
mapping input: (1) do independent voltage sources agree with each other
(utility-published vs CEC, plus SCE's sys_name as a third signal)? (2) how
often does the CURRENT proximity-only mapping already land a substation on a
wrong-voltage-class CATS node -- the number that motivates the mapping change?
(3) how much of the fleet can the voltage rule act on at all, per utility
(highside_kv_source coverage)? All three read fields already computed by
process_substations_clean.py (highside_kv, highside_kv_source,
cec_max_voltage_kv) -- no voltage is re-derived here.

CEC caveat (found running this check, SCE fleet): utility-vs-CEC class
agreement is only ~87%, which first looked like a data-quality problem.
Cross-checking against a THIRD source (SCE's own sys_name, e.g. substation
"Zanja" belongs to sys_name "El Casco 220/115 System") resolved it: sys_name
names the transmission SYSTEM/AREA a substation is fed from, and its LOW leg
(115) agrees with the utility's own high-side rating 84.1% of the time, while
its HIGH leg (220) agrees with CEC max_voltage_kv only 8% of the time. That is
the opposite pairing from what a data-entry error would produce, so the
straightforward reading is that CEC max_voltage_kv tends to record the site's/
area's broader transmission backbone voltage rather than THIS substation's own
load-attachment voltage. This does not affect SCE/SDGE (utility-published
value used directly), but it is a real caveat for PGE, whose highside_kv is
100% CEC-derived (no utility signal exists) -- PGE's voltage classes may skew
toward the areas's bulk voltage rather than the actual attachment point. See
"3. MOTIVATION" below for the per-utility mismatch rate this produces.

Inputs
------
  data/processed/substations/substation_attributes_clean.csv
  data/processed/load_projection/nodal/CATS/substation_node_map.csv
      (the --voltage-mode off / proximity-only mapping; run
      map_loads_to_nodes.py --system CATS first if missing)
  data/raw/CATS/CATS_buses.csv

Outputs (data/checks/voltage_validation/)
------------------------------------------
  agreement_utility_vs_cec.csv   per substation (SCE/SDGE only): utility vs
                                 CEC vs (SCE only) sys_name high-side kV,
                                 exact-match and same-CATS-class flags
  substation_vs_node_kv.csv      per proximity-map row: substation class vs
                                 assigned node's CATS class, match flag
  coverage_summary.csv           per utility: highside_kv_source counts

Figure (data/figures/load_projection/voltage/)
------------------------------------------------
  voltage_agreement.png   utility-vs-CEC and substation-vs-node confusion
                          matrices (CATS class x CATS class)

Usage
-----
  python scripts/load_projection/checks/validate_voltage.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/load_projection/nodal"))
from map_loads_to_nodes import band_to_cats_class  # noqa: E402

ATTR_FILE = ROOT / "data/processed/substations/substation_attributes_clean.csv"
MAP_FILE = ROOT / "data/processed/load_projection/nodal/CATS/substation_node_map.csv"
BUSES_FILE = ROOT / "data/raw/CATS/CATS_buses.csv"
OUT_DIR = ROOT / "data/checks/voltage_validation"
FIG_DIR = ROOT / "data/figures/load_projection/voltage"

_CATS_CLASSES = [66, 115, 230, 500]


def _sys_name_low_leg(sys_name: pd.Series) -> pd.Series:
    """SCE sys_name names the transmission SYSTEM/AREA a substation is fed
    from, not the substation's own voltage -- e.g. substation "Zanja" belongs
    to sys_name "El Casco 220/115 System" (a corridor between a 220 kV bulk
    leg and a 115 kV sub-transmission leg feeding the area's substations).
    The correct per-substation cross-check is therefore this LOW leg (115)
    against the substation's OWN high-side rating (substation_voltage's first
    token) -- not the system's high leg (220), which describes the area's
    backbone, not this substation's attachment point. (Verified empirically:
    sys_name's high leg agrees with CEC max_voltage_kv only 8% of the time,
    while sys_name's low leg agrees with the utility high side 84.1% of the
    time -- see module docstring "CEC caveat".)"""
    tok = sys_name.astype(str).str.extract(r"\d+(?:\.\d+)?\s*/\s*(\d+(?:\.\d+)?)\s+System",
                                           expand=False)
    return pd.to_numeric(tok, errors="coerce")


def build_agreement_table(attrs: pd.DataFrame) -> pd.DataFrame:
    """SCE/SDGE only -- substations with both a utility-published high side
    and a CEC high side to compare. PGE has no utility signal (highside_kv
    is CEC-only for PGE by construction) so it cannot appear here."""
    a = attrs[attrs.highside_kv_source == "utility"].copy()
    a = a[a.cec_max_voltage_kv.notna()].copy()
    a["kv_util"] = a["highside_kv"]
    a["kv_cec"] = a["cec_max_voltage_kv"]
    a["kv_sys_name_low_leg"] = np.where(a.utility == "sce", _sys_name_low_leg(a.sys_name), np.nan)
    a["class_util"] = a.kv_util.map(band_to_cats_class)
    a["class_cec"] = a.kv_cec.map(band_to_cats_class)
    a["exact_match"] = a.kv_util == a.kv_cec
    a["class_match"] = a.class_util == a.class_cec
    return a[["utility", "substation_name", "kv_util", "kv_cec", "kv_sys_name_low_leg",
             "class_util", "class_cec", "exact_match", "class_match"]]


def build_substation_vs_node_table(attrs: pd.DataFrame) -> pd.DataFrame:
    if not MAP_FILE.exists():
        print(f"  WARNING: {MAP_FILE.relative_to(ROOT)} not found -- run "
              "map_loads_to_nodes.py --system CATS first. Skipping.")
        return pd.DataFrame()
    prox = pd.read_csv(MAP_FILE)
    prox = prox[~prox.is_synthetic].copy()  # synthetic subs have no voltage to compare

    buses = pd.read_csv(BUSES_FILE)[["bus_i", "kV"]].rename(
        columns={"bus_i": "node", "kV": "node_kv"})
    buses["node_kv_class"] = buses.node_kv.map(band_to_cats_class)

    a = attrs[["utility", "substation_name", "highside_kv"]].copy()
    a["sub_kv_class"] = a.highside_kv.map(band_to_cats_class)

    m = prox.merge(a, on=["utility", "substation_name"], how="left")
    m = m.merge(buses, on="node", how="left")
    m["voltage_match"] = np.where(
        m.sub_kv_class.notna() & m.node_kv_class.notna(),
        m.sub_kv_class == m.node_kv_class, np.nan)
    return m[["utility", "substation_name", "node", "share", "dist_km",
             "highside_kv", "sub_kv_class", "node_kv", "node_kv_class", "voltage_match"]]


def build_coverage_summary(attrs: pd.DataFrame) -> pd.DataFrame:
    # crosstab's column categories include the literal value "utility" (one of
    # highside_kv_source's own values), which collides with the index name on
    # reset_index() -- rename the index first, then relabel it back.
    ct = pd.crosstab(attrs.utility.rename("iou"), attrs.highside_kv_source)
    ct["total"] = ct.sum(axis=1)
    return ct.reset_index().rename(columns={"iou": "utility"})


def plot_confusion(agreement: pd.DataFrame, sub_vs_node: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.5))

    def _heat(ax, x, y, title):
        ct = pd.crosstab(x, y).reindex(index=_CATS_CLASSES, columns=_CATS_CLASSES, fill_value=0)
        im = ax.imshow(ct.values, cmap="Blues")
        ax.set_xticks(range(len(_CATS_CLASSES)), [f"{c}" for c in _CATS_CLASSES])
        ax.set_yticks(range(len(_CATS_CLASSES)), [f"{c}" for c in _CATS_CLASSES])
        for i in range(len(_CATS_CLASSES)):
            for j in range(len(_CATS_CLASSES)):
                v = ct.values[i, j]
                ax.text(j, i, f"{v:,}", ha="center", va="center",
                       color="white" if v > ct.values.max() / 2 else "black", fontsize=9)
        ax.set_title(title)
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)

    if len(agreement):
        _heat(axes[0], agreement.class_util, agreement.class_cec,
             "utility class (rows) vs CEC class (cols)\nSCE/SDGE only")
    else:
        axes[0].set_visible(False)

    if len(sub_vs_node):
        s = sub_vs_node.dropna(subset=["sub_kv_class", "node_kv_class"])
        _heat(axes[1], s.sub_kv_class, s.node_kv_class,
             "substation class (rows) vs\nproximity-assigned node class (cols)")
    else:
        axes[1].set_visible(False)

    fig.tight_layout()
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    fig_path = FIG_DIR / "voltage_agreement.png"
    fig.savefig(fig_path, dpi=150)
    print(f"wrote {fig_path.relative_to(ROOT)}")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    attrs = pd.read_csv(ATTR_FILE)

    print("=" * 78)
    print("1. COVERAGE - highside_kv source, per utility")
    print("=" * 78)
    coverage = build_coverage_summary(attrs)
    print(coverage.to_string(index=False))
    print("  NOTE: PGE's highside_kv is ~100% CEC-derived (no utility signal exists) -- "
          "see module docstring 'CEC caveat': CEC max_voltage_kv likely skews toward "
          "each site's broader transmission-area voltage rather than the substation's "
          "own load-attachment voltage, based on SCE cross-validation below.")
    coverage.to_csv(OUT_DIR / "coverage_summary.csv", index=False)

    print("\n" + "=" * 78)
    print("2. AGREEMENT - utility-published vs CEC high side (SCE/SDGE only;")
    print("   PGE has no utility signal to compare)")
    print("=" * 78)
    agreement = build_agreement_table(attrs)
    agreement.to_csv(OUT_DIR / "agreement_utility_vs_cec.csv", index=False)
    if len(agreement):
        for util, grp in agreement.groupby("utility"):
            print(f"  {util}: n={len(grp)}  exact-match={grp.exact_match.mean():.1%}  "
                  f"same-CATS-class={grp.class_match.mean():.1%}  (utility vs CEC)")
        sce_sys = agreement[agreement.utility == "sce"].dropna(subset=["kv_sys_name_low_leg"])
        if len(sce_sys):
            sys_class_match = (sce_sys.kv_sys_name_low_leg.map(band_to_cats_class)
                               == sce_sys.class_util).mean()
            print(f"  sce: n={len(sce_sys)} with a parseable sys_name -- its LOW leg "
                  f"(the sub-transmission voltage feeding this substation's area) "
                  f"matches the utility high side {sys_class_match:.1%} of the time "
                  "(third independent signal, corroborates the utility source over CEC "
                  "-- see module docstring 'CEC caveat')")
        mismatches = agreement[~agreement.class_match]
        if len(mismatches):
            print(f"\n  {len(mismatches)} class-level disagreements:")
            print(mismatches[["utility", "substation_name", "kv_util", "kv_cec"]]
                  .to_string(index=False))
    else:
        print("  no substations with both a utility and a CEC high side found.")

    print("\n" + "=" * 78)
    print("3. MOTIVATION - proximity-only mapping: substation class vs assigned")
    print("   node's CATS class")
    print("=" * 78)
    sub_vs_node = build_substation_vs_node_table(attrs)
    if len(sub_vs_node):
        sub_vs_node.to_csv(OUT_DIR / "substation_vs_node_kv.csv", index=False)
        known = sub_vs_node.dropna(subset=["voltage_match"])
        for util, grp in known.groupby("utility"):
            print(f"  {util}: n={len(grp)}  proximity lands on same-class node "
                  f"{grp.voltage_match.mean():.1%} of the time "
                  f"({(~grp.voltage_match.astype(bool)).sum()} mismatches)")
        print(f"  overall: {known.voltage_match.mean():.1%} same-class "
              f"({len(known)} rows with both a substation and node class known)")

    plot_confusion(agreement, sub_vs_node)

    print(f"\nWrote -> {OUT_DIR.relative_to(ROOT)}/")
    for f in ["coverage_summary.csv", "agreement_utility_vs_cec.csv", "substation_vs_node_kv.csv"]:
        print(f"    {f}")


if __name__ == "__main__":
    main()
