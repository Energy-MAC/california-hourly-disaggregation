"""
Check whether CATS (the external test-system model used by
scripts/load_projection/nodal/) assigns one common statewide load shape to
every demand zone, just scaled per zone.

data/raw/CATS/Demand_data.csv is a single 168-hour REPRESENTATIVE PERIOD
(Rep_Periods=1, Timesteps_per_Rep_Period=168 -- a GenX-style representative
week, not a full year; no month/day/year labels), across 8,870
Demand_MW_z{bus_i} zone columns. This rules out literally reproducing
btm_combined_pv_plus_storage.png's 12-panel-by-month, multi-year layout --
there is no month/year axis here. Instead this is the CATS analogue of
data/figures/peak_hour_shift/btm_pv_shape_invariance.png's day-overlay test:
every zone's 168-hour profile is normalized to its own mean and overlaid to
see whether the normalized curves coincide (one template shape, scaled per
zone) or genuinely differ zone to zone.

Zones with zero load throughout (6,399 of 8,870 -- CATS buses this project's
own nodal-coverage work already treats as non-demand, see
filter_zero_demand()/demand_totals() in map_loads_to_nodes.py, reused here)
are excluded; normalizing a zero-mean series is undefined.

Output
------
  data/figures/peak_hour_shift/cats_load_shape_invariance.png
    Grey = each nonzero zone's 168h profile / its own mean.
    Red  = mean of the normalized zones (the "template" shape if invariant).
    Shaded band = +/-1 std across zones at each hour -- every zone
    contributes one full week, so this band's width IS the invariance
    answer, not sampling noise.

Usage
-----
  python scripts/data/cats_load_shape_invariance.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DEMAND_FILE = ROOT / "data" / "raw" / "CATS" / "Demand_data.csv"
FIG_DIR = ROOT / "data" / "figures" / "peak_hour_shift"

sys.path.insert(0, str(ROOT / "scripts" / "load_projection" / "nodal"))
from map_loads_to_nodes import demand_totals  # noqa: E402


def main() -> None:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(DEMAND_FILE)
    zcols = [c for c in df.columns if c.startswith("Demand_MW_z")]
    print(f"Loaded {DEMAND_FILE.relative_to(ROOT)}: {len(df)} hours x {len(zcols)} zones")

    totals = demand_totals(DEMAND_FILE, "Demand_MW_z")
    nonzero_cols = [f"Demand_MW_z{zid}" for zid, tot in totals.items() if tot > 0]
    nonzero_cols = [c for c in nonzero_cols if c in zcols]
    print(f"  {len(nonzero_cols)} of {len(zcols)} zones carry nonzero load "
          f"({len(zcols) - len(nonzero_cols)} excluded)")

    load = df[nonzero_cols]
    shapes = load / load.mean(axis=0)  # each zone normalized to its own week-mean

    hours = np.arange(len(df))
    mean_shape = shapes.mean(axis=1)
    std_shape = shapes.std(axis=1)

    # per-zone correlation to the mean shape -- quantifies "identical" beyond eyeballing
    corr = shapes.corrwith(mean_shape)
    print(f"  zone-vs-mean-shape correlation: median {corr.median():.3f}, "
          f"p10 {corr.quantile(0.10):.3f}, min {corr.min():.3f}")

    fig, ax = plt.subplots(figsize=(13, 6))
    ax.plot(hours, shapes.values, color="#888888", lw=0.4, alpha=0.05)
    ax.fill_between(hours, mean_shape - std_shape, mean_shape + std_shape,
                    color="#1f6f8b", alpha=0.2, label=r"$\pm$1 std across zones")
    ax.plot(hours, mean_shape, color="#c0392b", lw=2.0, label="mean normalized shape")
    ax.set_xlabel("hour of representative week (0-167)")
    ax.set_ylabel("load / zone's own week-mean")
    ax.set_xticks(range(0, 168, 24))
    ax.set_title(
        f"CATS Demand_data.csv shape invariance -- {len(nonzero_cols)} nonzero zones, "
        f"each normalized to its own mean\n"
        f"Grey = individual zone shapes;  red = mean;  band = "
        r"$\pm$1 std across zones.  "
        f"Median zone-vs-mean correlation: {corr.median():.3f}"
    )
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    out = FIG_DIR / "cats_load_shape_invariance.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Saved: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
