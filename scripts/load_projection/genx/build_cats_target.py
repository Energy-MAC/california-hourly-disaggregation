"""Write the CATS/GenX control demand as a CAISO-style hourly target series.

Approach 2 calibrates F*, s(c) and rho(c) against whatever target it is given.
Its default target is the historical EIA-930 CISO series, which makes F* = 0.7361
a statement about how the substation fleet relates to *history*.  That is the
wrong reference here: the methodology is to take the load we care about as given
and disaggregate it, so F* must be recomputed against the CATS control demand
that the GenX cases actually carry.  Nothing should be inherited from another
dataset except the substations' own min/max load profiles.

This script emits that target: the statewide hourly total of the four seasonal
control weeks, timestamped through `genx/rep_week_calendar.csv` so every hour
lands in its correct (month, hour_pst) cell.  Feed it to

    generate_stochastic.py --target <this file>

which then reports an F* calibrated on CATS rather than on EIA-930.

Coverage caveat, stated plainly: the four weeks are 672 hours touching 120 of
the 288 month-hour cells, with 7 observations per cell.  F* is a single ratio
and is well determined by that; the per-cell s(c) and rho(c) are not, and cells
outside the four weeks have no CATS observation at all.  The run tag records
which target was used so the two calibrations never get mixed up.

CLI parameters
  --out     output CSV (default data/processed/load_projection/cats_caiso_target.csv)
  --runs    rescale run tags whose demand to sum instead of the control
            (default: the control tree itself)

Output columns
  dt_pst_hb, demand_mw, season, time_index, month, hour_pst

Usage
  python scripts/load_projection/genx/build_cats_target.py
  python scripts/load_projection/approach2/generate_stochastic.py \
      --family normal --n-draws 5 \
      --target data/processed/load_projection/cats_caiso_target.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
from genx_demand_io import (  # noqa: E402
    load_rep_week_calendar, read_demand, scenario_seasons)

DEFAULT_OUT = ROOT / "data/processed/load_projection/cats_caiso_target.csv"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    tree = scenario_seasons()
    cal = load_rep_week_calendar()

    rows = []
    for season, case in tree.canonical.items():
        demand = read_demand(tree.demand_path(case))
        total = demand.values.sum(axis=1)          # statewide MW, per timestep
        cells = cal[season]
        for t, ((month, hour), mw) in enumerate(zip(cells, total), start=1):
            rows.append({"season": season, "time_index": t,
                         "month": month, "hour_pst": hour, "demand_mw": float(mw)})

    df = pd.DataFrame(rows)
    # rebuild a real timestamp per season from the calendar start so hours are
    # contiguous and unique across the four weeks
    cal_raw = pd.read_csv(ROOT / "genx/rep_week_calendar.csv")
    starts = dict(zip(cal_raw.season, pd.to_datetime(cal_raw.start_datetime)))
    stamps = []
    for season, g in df.groupby("season", sort=False):
        base = starts[season]
        stamps.append(pd.Series(base + pd.to_timedelta(g.time_index - 1, unit="h"),
                                index=g.index))
    df["dt_pst_hb"] = pd.concat(stamps).sort_index()
    df = df[["dt_pst_hb", "demand_mw", "season", "time_index", "month", "hour_pst"]]
    df = df.sort_values("dt_pst_hb").reset_index(drop=True)

    dup = df.dt_pst_hb.duplicated().sum()
    if dup:
        raise ValueError(f"{dup} duplicate timestamps across the four weeks")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)

    cells = df.groupby(["month", "hour_pst"]).size()
    print(f"{len(df)} hours across {df.season.nunique()} seasonal weeks")
    print(f"  demand: min {df.demand_mw.min():,.0f} MW  mean {df.demand_mw.mean():,.0f} "
          f"MW  max {df.demand_mw.max():,.0f} MW")
    print(f"  covers {len(cells)} of 288 month-hour cells, "
          f"{cells.min()}-{cells.max()} observations each")
    print(f"\nwrote {out.relative_to(ROOT)}")
    print("\nNow recalibrate Approach 2 on it:")
    print(f"  python scripts/load_projection/approach2/generate_stochastic.py "
          f"--family normal --n-draws 5 --target {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
