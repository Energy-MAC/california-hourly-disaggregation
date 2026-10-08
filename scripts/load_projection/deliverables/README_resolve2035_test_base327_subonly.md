# California 2035 hourly load at CATS substation buses — Baseline-only test set

Hourly (8,760-h) demand for **2035** at **physical CATS substation buses only**, built
from RESOLVE's **Baseline load component alone** and allocated on a **gross** basis —
behind-the-meter PV is *not* netted out. Six variants, all allocating 100% of the
target: **327.35 TWh**.

> ### Read this before interpreting anything
>
> **This is not RESOLVE's 2035 forecast.** It uses only RESOLVE's `Baseline` load
> component and omits every additive overlay — electric vehicles, building
> electrification, data centres, climate impacts, storage losses, and the offsetting
> efficiency savings. That is **118 TWh of gross load, 27% of the total**, left out on
> purpose.
>
> It exists to exercise an algorithm against a specific known series. **Do not cite it
> as a California load projection, and do not compare it with the full package** —
> `deliverables/resolve_2035_cats_nodal/`, which carries RESOLVE's actual 2035 load of
> 445.76 TWh gross / 404.13 TWh net.
>
> It also differs from that package in three further ways, all deliberate: the load is
> **gross**, every `AddedNode` bus is unloaded, and the weather year is 2012 rather than
> the median-peak year.
>
> On the gross basis specifically: the substation weights behind this allocation are
> measured *at the substation meter*, downstream of rooftop solar. Allocating gross load
> with them puts the 42.93 TWh BTM offset onto those substations rather than removing
> it. That is the requested behaviour here, and it is why `net` is the default
> everywhere else.

| | This test set | Full package |
|---|---|---|
| Load components | **Baseline only** | Baseline + all overlays |
| Load basis allocated | **gross** (BTM not removed) | net of BTM |
| **Energy allocated** | **327.35 TWh** | 404.13 TWh (net) |
| CA-wide peak | **73,260 MW** | 76,173 MW |
| Weather year | **2012** (requested) | 2005 (median peak) |
| Buses | **1,859** — `Type='Substation'` only | 2,466 incl. 607 `AddedNode` |
| Measured pool | **62.0%** of load | 48.5% |

For reference, the same build on a net basis allocates 284.41 TWh; the 42.93 TWh
difference is the BTM PV this set deliberately keeps.

Everything else is identical to the full package: the county-first allocation, ReEDS
county shares, `alpha = u/n`, the three variant axes, and the conservation guarantee.
For the method, see that package's README §4 — only the three settings above differ.

---

## What is in the box

```
nodal_hourly/                                                     (~23.5 MB each)
  countyfirst_envelope_catsprop__prox.csv.gz        <- start here
  countyfirst_envelope_catsprop__voltres.csv.gz       bus-assignment axis
  countyfirst_envelope_equalsplit__prox.csv.gz        uncovered-split axis
  countyfirst_stochmean_catsprop__prox.csv.gz         weight-source axis
  countyfirst_stochdraw0_catsprop__prox.csv.gz        one stochastic realization
  countyfirst_stochdraw1_catsprop__prox.csv.gz        another realization

summary/     variant_summary, node_annual_mwh, node_peak_mw, county_allocation
reference/   resolve_2035_statewide (baseline_mw / overlay_mw / gross / btm / net),
             resolve_2035_scaling, resolve_2035_components (empty here by design),
             cats_node_metadata, node_shares_static, substation_node_map__{prox,voltres}
code/        build_resolve2035_cats_package.py, deliverable_numbers.py
```

Each hourly file is 8,760 rows × 1,861 columns: `datetime_pst`, `hour_of_year`, then
`Demand_MW_z{bus_i}` in MW — the CATS/GenX zone convention. Timestamps are fixed PST,
hour-beginning: the value at hour *h* is the load for *h*:00→*h*+1:00.
`summary/variant_summary.csv` records `load_basis`, `overlays` and `bus_types`, so a
file can always be traced back to how it was built.

```python
import pandas as pd
df = pd.read_csv("nodal_hourly/countyfirst_envelope_catsprop__prox.csv.gz",
                 index_col="datetime_pst", parse_dates=True)
buses = df.filter(like="Demand_MW_z")
buses.sum(axis=1).max()        # 73260.0 MW
buses.sum().sum() / 1e6        # 327.35 TWh
```

`reference/resolve_2035_components.csv` is present but empty — that is the expected
signature of `--overlays none`, and a quick way to confirm which build you have.

---

## What is guaranteed

- **100% of the target is allocated, exactly, in every hour.** Worst deviation across
  all six files is **0.0501 MW**, half the 0.1 MW print grid.
- **No bus-hour is negative** (0 cells in all six variants).
- **Zero `AddedNode` buses carry load**, and no bus carries load that CATS itself
  leaves unloaded — both verified column by column.
- **County totals match the ReEDS county shares** to **0.000031 percentage points**
  across all 57 counties, in every variant. Los Angeles 21.206% (69.42 TWh), Sacramento
  3.070% (10.05 TWh), Imperial 1.176% (3.85 TWh), Stanislaus 1.592% (5.21 TWh) — the
  same shares as the full package, because excluding AddedNodes re-splits their load
  *within their own county* rather than moving it.

Dropping the AddedNodes raises the measured pool from 48.5% to **62.0%** of load: the
607 excluded buses were all in the uncovered pool, so removing them shrinks the part of
the allocation that carries no substation measurement. Bus counts fall accordingly —
Los Angeles 413 → 335, Sacramento 231 → 190, Imperial 28 → 22, Stanislaus 63 → 39.

---

## Regenerating

```bash
python build_resolve2035_cats_package.py \
    --load-basis gross --overlays none --bus-types substation --weather-year 2012 \
    --out deliverables/resolve2035_test_base327_subonly_wy2012 \
    --readme scripts/load_projection/deliverables/README_resolve2035_test_base327_subonly.md
```

Each flag is independent and defaults to the full-package behaviour:

| Flag | This build | Default |
|---|---|---|
| `--load-basis` | `gross` | `net` (BTM removed) |
| `--overlays` | `none` (Baseline only) | `all` |
| `--bus-types` | `substation` | `all` (includes loaded AddedNodes) |
| `--weather-year` | `2012` | median CA-wide annual peak |

`--bus-types all` with everything else unchanged gives the same allocation with the
607 loaded AddedNode buses included — that is the variant to use for research that
wants load disaggregated onto every bus CATS loads.

Generated 2026-10-02.
