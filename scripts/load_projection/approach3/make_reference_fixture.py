"""Generate the Approach 3 REFERENCE FIXTURE that the doc numbers are quoted from.

The project rule is that every assertion must be independently reproducible. The
numbers in `docs/approach3_proportional.md` and the `approach3-proportional`
skill are measured on a synthetic fixture, so that fixture has to be
regenerable from this repo alone -- hence this script rather than a one-off.

It is synthetic ON PURPOSE. The real target node system has no coordinates and
is supplied by the user; a committed sample of it would be neither ours to ship
nor representative. What the fixture has to be is *structurally* like the real
case:

  * two-level node ids carrying symbols (`|`, `/`, `,`, spaces), so the
    sanitizer is exercised on something that would corrupt a CSV
  * a mapped base with several siblings, including the mixed-sign base from the
    case that prompted the design (10, 11, -1)
  * roughly half the level mass UNMAPPED, so the slack path is genuinely
    exercised at a realistic Lambda_U rather than degenerately
  * substation names drawn from the real profiled fleet, restricted to names
    only one utility uses, so no shared-name tie-break is needed (that is a
    recorded TODO, not part of the reference run)

Run:
  python scripts/load_projection/approach3/make_reference_fixture.py
  python scripts/load_projection/approach3/build_proportional_nodal.py \\
      --nodes data/checks/approach3/fixture/nodes.csv \\
      --mapping data/checks/approach3/fixture/mapping.csv \\
      --target csv --target-csv data/checks/approach3/fixture/target.csv \\
      --tag ref_envelope
  python scripts/load_projection/approach3/doc_numbers.py --run ref_envelope

Deterministic: seed 7 is fixed, so the fixture and therefore every quoted number
reproduces bit-for-bit.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
PROFILES = ROOT / "data/processed/substations/substation_load_profiles_clean.csv"
OUT = ROOT / "data/checks/approach3/fixture"

SEED = 7
N_MAPPED_BASES = 300
N_UNMAPPED = 400


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)

    prof = pd.read_csv(PROFILES, usecols=["utility", "substation_name"]).drop_duplicates()
    prof["utility"] = prof.utility.str.lower()
    # names only one utility uses: the shared-name tie-break is a recorded TODO
    # and must not be part of the reference run
    n_util = prof.groupby("substation_name").utility.nunique()
    uniq = prof[prof.substation_name.isin(n_util[n_util == 1].index)]
    pick = uniq.sample(N_MAPPED_BASES, random_state=args.seed).reset_index(drop=True)

    rows, edges = [], []
    for i, r in pick.iterrows():
        # symbols on purpose: these would break a raw-id column header
        b = f"BASE|{i:03d}/{r.substation_name[:6]}"
        w = float(rng.uniform(5, 300))
        rows.append({"node_id": f"{b} a", "base_id": b, "subname": "a",
                     "winter_load": w,
                     "summer_load": w * float(rng.uniform(0.8, 1.3))})
        rows.append({"node_id": f"{b} b", "base_id": b, "subname": "b",
                     "winter_load": w * 0.6, "summer_load": w * 0.7})
        edges.append({"base_id": b, "substation_name": r.substation_name,
                      "utility": r.utility})

    # the case that prompted the two-level design: a mixed-sign base
    foo = "BASE|foo"
    for sub, w in (("a", 10.0), ("b", 11.0), ("c", -1.0)):
        rows.append({"node_id": f"foo {sub}", "base_id": foo, "subname": sub,
                     "winter_load": w, "summer_load": w})
    edges.append({"base_id": foo, "substation_name": pick.substation_name.iloc[0],
                  "utility": pick.utility.iloc[0]})

    # unmapped nodes, ~half the level mass, so the slack path is real
    for i in range(N_UNMAPPED):
        nid = f"UNM-{i:03d},x"
        rows.append({"node_id": nid, "base_id": nid, "subname": "",
                     "winter_load": float(rng.uniform(10, 400)),
                     "summer_load": float(rng.uniform(10, 400))})

    nodes = pd.DataFrame(rows)
    nodes.to_csv(out / "nodes.csv", index=False)
    pd.DataFrame(edges).to_csv(out / "mapping.csv", index=False)

    idx = pd.date_range("2035-01-01", periods=8760, freq="h")
    y = np.asarray(31000
                   + 7000 * np.sin(2 * np.pi * (idx.hour - 18) / 24)
                   + 5000 * np.sin(2 * np.pi * idx.dayofyear / 365),
                   dtype=np.float64) + rng.normal(0, 400, 8760)
    pd.DataFrame({"datetime_pst": idx, "demand_mw": y}).to_csv(
        out / "target.csv", index=False)

    unm = nodes.base_id.str.startswith("UNM")
    print(f"wrote {out}")
    print(f"  nodes            {len(nodes):,} in {nodes.base_id.nunique():,} bases")
    print(f"  edges            {len(edges):,}")
    print(f"  unmapped share   "
          f"{nodes.loc[unm, 'winter_load'].sum() / nodes.winter_load.sum() * 100:.1f}% "
          f"of winter level mass")
    print(f"  target           {y.sum() / 1e6:.2f} TWh, peak {y.max():,.0f} MW")
    print(f"  seed             {args.seed} (fixed, so the quoted numbers reproduce)")


if __name__ == "__main__":
    main()
