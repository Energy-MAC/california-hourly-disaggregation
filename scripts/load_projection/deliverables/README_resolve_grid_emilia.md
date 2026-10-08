# CA-wide hourly nodal load at CATS buses — RESOLVE grid

Hourly electricity demand at every load-carrying CATS substation bus, for five
RESOLVE model years × eight weather years.

**160 datasets:** 4 variants × 40 (model year, weather year) combinations, each
8,760 hours × 1,859 buses. The allocation is shipped once and the hourly files are
reconstructed on demand, so the package is **15 MB** rather than the ~2.5–2.9 GB the
same grid costs once expanded.

---

## Quick start

```bash
python code/expand.py --list                                   # what is available
python code/expand.py --variant <variant> --series <series>     # write one hourly file
```

Example:

```bash
python code/expand.py \
    --variant countyfirst_cec2022_shaped_v2_equalsplit__prox \
    --series y2035_wy2012
```

That writes `nodal_hourly/<variant>__<series>.csv.gz` — 15.7–17.9 MB, about 25
seconds (measured). `--out-dir` puts it elsewhere; `--all` writes all 160, which is
**~2.5–2.9 GB and over an hour — check your disk first**. Only numpy and pandas are
needed; nothing else, and no access to the originating repository.

Output is a wide CSV: `datetime_pst, hour_of_year, Demand_MW_z<bus_i>…`, one row per
hour, MW at one decimal place. `Demand_MW_z1065` is CATS bus 1065.

**`datetime_pst` carries the *weather* year's calendar**, because that is where the
8,760-hour shape comes from; the model year sets the level. `hour_of_year` (1–8760)
is the stable key across combinations. All times are **fixed PST, hour-beginning** —
no daylight saving.

## What this is

For each combination of a **model year** (the demand level RESOLVE forecasts) and a
**weather year** (the hourly shape), 100% of California's net electricity demand is
allocated across CATS buses.

- **RESOLVE sets the level.** CA-wide hourly MW, net of behind-the-meter PV, for all
  six California zones (PG&E, SCE, SDG&E, IID, LADWP, NCNC) — not just the
  investor-owned utilities. The load is RESOLVE's Baseline **plus** its additive
  overlay components (building electrification, EVs, data centres, climate impacts,
  storage losses, minus achievable efficiency). Baseline alone would understate 2035
  CA-wide load by 118 TWh.
- **County shares come from ReEDS**, normalized. ReEDS never contributes a load level.
- **Within each county**, the split across buses follows a measurement — see
  "Variants".

| Model year | CA-wide energy across the 8 weather years |
|---|---|
| 2026 | 276.9 – 279.1 TWh |
| 2030 | 334.9 – 337.4 TWh |
| 2035 | 401.4 – 404.1 TWh |
| 2040 | 443.5 – 446.3 TWh |
| 2045 | 467.9 – 470.9 TWh |

Weather years: **2007–2014**. Model years stop at 2045 because RESOLVE's Baseline —
its only weather-varying component — stops there; a later year could only be
extrapolated, which this package does not do.

## Files

```
shares/<variant>.npz              the allocation: 288 cells x 1,859 buses, one per variant
statewide/<series>.csv.gz         CA-wide hourly MW, one per (model year, weather year)
code/expand.py                    reconstructs any hourly file from the two above
nodal_hourly/                     empty; expand.py's default output directory
summary/                          per-variant and per-bus energy and peak, county table
reference/                        RESOLVE statewide series and scaling, bus metadata, maps
joinedWinterSummerWecc.csv        the seasonal substation-load input the weights came from
```

### Why it is shipped this way

The share matrices do not depend on the model year or the weather year — only on the
allocation settings. So the allocation is stored once and each combination costs one
8,760-row statewide series. `expand.py` reproduces any hourly file **byte for byte**
identical to one written directly; this is exact arithmetic, not an approximation.

A "cell" is a (month, hour-of-day) pair in fixed PST — 12 × 24 = 288. All load in a
cell is allocated by one share vector, which is why the matrix is small.

## Variants

Each variant is the same county-first allocation with a different **within-county
weight** — the measurement deciding how a county's energy splits across its buses.
**ReEDS county totals are identical across all four**, and all four have the same
CA-wide hourly total. They differ only in where load sits *inside* a county.

| variant | within-county weight |
|---|---|
| `countyfirst_cec2022_shaped_v2_equalsplit__prox` | external seasonal substation loads, with each substation's own measured diurnal shape within a season |
| `countyfirst_cec2022_flat_v2_equalsplit__prox` | the same seasonal loads, flat within a season — what a seasonal-only input literally contains |
| `countyfirst_cec2022_shaped_vrestrict_v2_equalsplit__prox` | as `shaped`, but coordinate-placed rows must match the bus's voltage class |
| `countyfirst_stochmean_equalsplit__prox` | the repo's stochastic disaggregation output, as a comparison not derived from the external file |

The first three come from `joinedWinterSummerWecc.csv`: **1,284 rows** carrying a
summer (May–Oct) and winter (Nov–Apr) load, which collapse to **1,111 weighted
units** once same-name rows sitting within 500 m of each other are added together
(two banks at one substation). **`shaped` and `flat` differ only in the
within-season hourly pattern** — their seasonal totals per bus are identical.

Two settings apply to every variant:

- **Substation buses only.** Every CATS `AddedNode` is unloaded, including those CATS
  itself loads: the pool is **1,860** buses rather than 2,467. County totals are
  unaffected — an excluded bus's load is re-split within its own county.
- **Uncovered pool split evenly.** A county's load that no measurement reaches is
  divided equally among its remaining buses, rather than in proportion to CATS's own
  bus loads. This is deliberate: proportional-to-CATS would make much of the
  allocation a function of CATS itself, so the package could not be compared against
  CATS independently. The trade-off is that CATS's own variation within a county's
  uncovered buses is not reproduced.

**100% of load is allocated in every variant.** A separate and much smaller figure is
the share of load whose *within-county split* is measurement-driven:

| variant | measurement-driven share | buses covered |
|---|---|---|
| `cec2022_shaped_v2`, `cec2022_flat_v2` | 43.81% | 830 |
| `cec2022_shaped_vrestrict_v2` | 43.45% | 827 |

The remaining ~56% is the evenly-split uncovered pool. **These two ideas are
different and the distinction matters**: no load is missing, but for most of it the
within-county placement is an even split rather than a measurement.

## Caveats

1. **Weather years move only part of the load.** RESOLVE's overlay components carry
   no weather dimension — only the Baseline does. So **energy barely moves while peak
   moves substantially**:

   | Model year | peak range | spread | energy spread |
   |---|---|---|---|
   | 2026 | 57,968 – 62,252 MW | +7.4% | +0.79% |
   | 2030 | 65,997 – 70,087 MW | +6.2% | +0.74% |
   | 2035 | 74,470 – 80,463 MW | +8.0% | +0.68% |
   | 2040 | 81,133 – 87,828 MW | +8.3% | +0.64% |
   | 2045 | 84,740 – 92,334 MW | +9.0% | +0.63% |

   If your question is about energy, the weather year barely matters. If it is about
   peak or reliability, it matters a great deal and a single weather year will
   mislead. 2010 is the high-peak year for 2026 and 2030; 2007 for 2035–2045.

2. **1,859 of the 1,860 pool buses carry load.** The exception is CATS bus 2318 in
   **Alpine County**, whose ReEDS county share is exactly zero, so there is no load to
   give it. No load is lost.

3. **Bus-level placement is a model, not a measurement.** A substation's load is
   assigned to its nearest CATS bus (`reference/substation_node_map__prox.csv` records
   which, with tie shares). **Buses are not metering points**, and a bus is not a
   substation.

4. **Load is net of behind-the-meter PV**, matching where the substation measurements
   are taken. Some buses may therefore show low or unusual midday load.

5. **How the 1,111 units were resolved**, since it bounds what the measurement can
   claim:

   | route | rows | meaning |
   |---|---|---|
   | direct name match | 649 | matched a profiled utility substation by name |
   | name dictionary | 24 | matched via a curated name-exceptions list |
   | placed by coordinates | 434 | no usable name match; placed at the nearest eligible bus |
   | unresolved | 4 | contributed no weight |

   The 434 coordinate-placed rows do not follow the nodal-map variants, and in the
   `shaped` variants **72 cells carry a borrowed diurnal shape** rather than a
   measured one. **66 targets received more than one input row** and their weights
   were summed, which is correct where several listed substations sit behind one bus.
   The 4 unresolved rows (`Perry`, `Lucerne`, `Belmont`, `Mariposa`) carry names that
   two utilities both use, with coordinates too far from either candidate to settle
   it; together they are **0.10% of summer and 0.09% of winter** input weight.

6. **Conservation.** The CA-wide total is preserved in every hour, in floating point
   and at printed precision: the largest per-hour residual measured over a full
   expanded year is **0.049995 MW**, under half of one 0.1 MW print unit, with **zero
   hours exceeding it** and **zero negative bus-hours**.

## Provenance

| | |
|---|---|
| Level | RESOLVE; scenario and scaling in `reference/resolve_scaling_y*.csv`, components in `reference/resolve_components_y*.csv` |
| County shares | ReEDS, county year 2023 (`summary/county_allocation.csv`, 57 counties, shares sum to 1.0000000000) |
| Network | CATS (California Test System) bus list |
| Within-county weights | `joinedWinterSummerWecc.csv`, md5 `63ad4b45891362e81b9cc47ba43a501c`, 1,284 rows -> 1,111 units |
| Nodal mapping vintage | `CATS_updated_mapping_20261006` (recorded per row in `summary/variant_summary.csv`) |
| Built by | `code/build_resolve2035_cats_package.py` |
| Numbers verified by | `code/deliverable_numbers.py --sections I,J` |

Every measured figure above comes from that recompute script. If you change anything
in the package, re-run it rather than editing a number here.
