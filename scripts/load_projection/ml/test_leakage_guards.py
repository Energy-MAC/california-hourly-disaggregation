"""Leakage guards for the substation-load cookbook run.

Two properties the methodology depends on, checked as hard assertions so a
regression can't silently pass:

  1. NO GROUP LEAKAGE -- no substation may appear in both train and test of the
     final hold-out, for either the group or the spatial scheme. (This is also
     asserted inside run_cookbook on every real run; here we test both schemes
     directly on the assembled frame.)

  2. IMPUTABLE FEASIBILITY -- every feature in the imputable config must be
     constructible for substations we have NO profile for. We verify the raw
     inputs each imputable feature derives from are present in the CEC unscraped
     inventory (cec_unscraped_*.csv from the coverage audit), so a model trained
     in the imputable config could actually be applied there.

Run:
  python scripts/load_projection/ml/test_leakage_guards.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/ml"))
from ml.splits import group_holdout, spatial_blocks  # noqa: E402
from predict_substation_load import assemble  # noqa: E402

# raw CEC-unscraped columns each imputable feature FAMILY can be derived from
CEC_UNSCRAPED = ROOT / "data/checks/substation_coverage_audit/cec_unscraped_pge.csv"


def test_no_group_leakage(df: pd.DataFrame) -> None:
    groups = df.substation_id
    # group scheme
    mask = group_holdout(groups, test_frac=0.2, seed=1)
    _assert_disjoint(groups, mask, "group")
    # spatial scheme (whole blocks -> whole substations on one side)
    blocks = spatial_blocks(df[["lat", "lon"]], n_blocks=10, seed=1)
    valid = [b for b in blocks.unique() if b >= 0]
    test_blocks = set(np.random.default_rng(1).choice(valid, size=2, replace=False).tolist())
    smask = blocks.isin(test_blocks).to_numpy()
    _assert_disjoint(groups, smask, "spatial")
    print("  [OK] no substation appears in both train and test (group & spatial)")


def _assert_disjoint(groups, test_mask, name) -> None:
    tr = set(groups[~test_mask]); te = set(groups[test_mask])
    overlap = tr & te
    assert not overlap, f"{name}: {len(overlap)} substations leak across the split"


def test_imputable_feasible(spec) -> None:
    cec = pd.read_csv(CEC_UNSCRAPED)
    have = set(cec.columns)
    # each imputable feature -> the raw CEC-unscraped column(s) it derives from
    derivable = {
        "lat": "latitude", "lon": "longitude",
        "highside_kv": "max_voltage_kv", "sub_kv_class": "max_voltage_kv",
        "county_population": "county", "ca_load_fraction": "county",
        "btm_pv_2024_mw": "county",
        "month": None, "hour": None,            # calendar: free
    }
    for feat in spec.imputable:
        if feat.startswith(("month_", "hour_", "util_", "preg_")):
            continue  # engineered from calendar / owner_std / county -> all derivable
        src = derivable.get(feat, "MISSING")
        assert src is None or src in have, \
            f"imputable feature {feat!r} derives from {src!r}, absent in cec_unscraped"
    print(f"  [OK] all {len(spec.imputable)} imputable features are constructible "
          "for unscraped CEC substations")


def main() -> None:
    df, spec, _ = assemble("max_load")
    print(f"assembled {len(df):,} rows / {df.substation_id.nunique()} substations")
    test_no_group_leakage(df)
    test_imputable_feasible(spec)
    print("ALL GUARDS PASSED")


if __name__ == "__main__":
    main()
