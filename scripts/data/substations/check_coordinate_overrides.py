"""Sanity-check hand-researched coordinates in substationCoordinateOverrides.csv.

Run this BEFORE rebuilding the pipeline. A hand-researched coordinate has two
characteristic failure modes, and both look fine in the CSV:

  1. DUPLICATE  -- the coordinate lands on top of an existing substation, which
     usually means the "missing" substation is really the same facility under a
     different name (or a second bank at the same site). Placing load there
     double-counts a location.
  2. IMPLAUSIBLE -- the coordinate is nowhere near any known substation, which
     usually means the wrong facility was found (a same-named site elsewhere in
     the state, or a transposed sign).

Neither is decidable automatically -- a genuinely new remote substation looks
exactly like case 2 -- so this prints the evidence and flags, and a human calls
it. Distances are great-circle km.

Reference set: every substation in substation_attributes_clean.csv that already
has a coordinate (utility or basin), plus optionally the CEC statewide inventory
(--cec), which contains many substations we never scraped and is the better test
of "does anything real sit here".

CLI parameters
  --near-km      flag a duplicate when a same-utility substation is closer than
                 this (default 0.5)
  --far-km       flag as isolated when the nearest same-utility substation is
                 farther than this (default 25)
  --k            neighbours to list per override (default 3)
  --cec          also report the nearest CEC inventory record
  --all          check every filled row, not just ones absent from the
                 processed attributes (default: only rows not yet applied)

Usage
  python scripts/data/substations/check_coordinate_overrides.py --cec
  python scripts/data/substations/check_coordinate_overrides.py --all --cec
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
OVERRIDES = ROOT / "data" / "substationCoordinateOverrides.csv"
ATTRS = ROOT / "data" / "processed" / "substations" / "substation_attributes_clean.csv"
CEC = ROOT / "data" / "processed" / "substation_misc" / "ca_substations_cec.csv"


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = np.radians(lat2 - lat1), np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--near-km", type=float, default=0.5)
    ap.add_argument("--far-km", type=float, default=25.0)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--cec", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    ov = pd.read_csv(OVERRIDES)
    ov = ov[ov.lat.notna() & ov.lon.notna()].reset_index(drop=True)
    if ov.empty:
        print("No filled override rows.")
        return

    attrs = pd.read_csv(ATTRS)
    attrs["lat"] = attrs.util_lat.fillna(attrs.basin_lat)
    attrs["lon"] = attrs.util_lon.fillna(attrs.basin_lon)
    ref = attrs[attrs.lat.notna()].copy()

    # An override already applied to the processed file would otherwise report
    # itself as its own nearest neighbour at 0 km.
    applied = set(zip(attrs.utility.str.lower(), attrs.substation_name))
    ov["already_applied"] = [
        (str(u).lower(), n) in applied
        and pd.notna(attrs.loc[(attrs.utility.str.lower() == str(u).lower())
                               & (attrs.substation_name == n), "lat"]).any()
        for u, n in zip(ov.utility, ov.substation_name)]

    cec = None
    if args.cec:
        cec = pd.read_csv(CEC)
        cec = cec[cec.latitude.notna()]

    print(f"{len(ov)} filled override(s); reference set {len(ref):,} placed "
          f"substations" + (f" + {len(cec):,} CEC records" if cec is not None else ""))
    print(f"flags: DUPLICATE < {args.near_km} km (same utility), "
          f"ISOLATED > {args.far_km} km\n")

    verdicts = []
    for r in ov.itertuples():
        util = str(r.utility).lower()
        # exclude the row's own record so it cannot match itself
        others = ref[~((ref.utility.str.lower() == util)
                       & (ref.substation_name == r.substation_name))].copy()
        others["km"] = haversine_km(r.lat, r.lon, others.lat.values, others.lon.values)
        same = others[others.utility.str.lower() == util].nsmallest(args.k, "km")
        nearest_same = float(same.km.iloc[0])
        any_near = others.nsmallest(1, "km")

        flag = "ok"
        if nearest_same < args.near_km:
            flag = "DUPLICATE?"
        elif nearest_same > args.far_km:
            flag = "ISOLATED?"

        tag = " [already in processed attrs]" if r.already_applied else " [NEW]"
        print(f"--- {r.utility}/{r.substation_name}  ({r.lat:.5f}, {r.lon:.5f})"
              f"  {flag}{tag}")
        for s in same.itertuples():
            print(f"      {s.km:8.3f} km  {s.utility}/{s.substation_name}")
        if any_near.utility.iloc[0].lower() != util:
            a = any_near.iloc[0]
            print(f"      {a.km:8.3f} km  {a.utility}/{a.substation_name}  (other utility)")
        if cec is not None:
            c = cec.copy()
            c["km"] = haversine_km(r.lat, r.lon, c.latitude.values, c.longitude.values)
            c = c.nsmallest(1, "km").iloc[0]
            print(f"      {c.km:8.3f} km  CEC: {c['name']} "
                  f"[{c.owner_std}, {c.county}, {c.max_voltage_kv} kV]")
        verdicts.append({"utility": r.utility, "substation_name": r.substation_name,
                         "nearest_same_utility_km": round(nearest_same, 3),
                         "flag": flag, "new": not r.already_applied})

    v = pd.DataFrame(verdicts)
    print("\n=== summary ===")
    print(v.to_string(index=False))
    n_flag = int((v.flag != "ok").sum())
    print(f"\n{n_flag} row(s) flagged for review; {len(v) - n_flag} look clean.")
    if n_flag:
        print("A flag is a prompt to look, not a verdict: a genuinely remote new "
              "substation reads as ISOLATED, and two banks at one site read as "
              "DUPLICATE.")


if __name__ == "__main__":
    main()
