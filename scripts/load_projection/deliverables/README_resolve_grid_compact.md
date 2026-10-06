# CA-wide hourly nodal load at CATS buses — RESOLVE grid

Hourly electricity demand at every load-carrying CATS substation bus, for several
RESOLVE model years and several weather years, with the allocation shipped once and
the hourly files reconstructed on demand.

> **Numbers marked `[recompute]` must be filled from
> `code/deliverable_numbers.py --sections I,J` before this package is sent to
> anyone.** The build prints them; nothing here should be typed by hand.

## What this is

For each combination of a **model year** (the load level RESOLVE forecasts) and a
**weather year** (the 8,760-hour shape), 100% of California's net electricity
demand is allocated across CATS buses.

- **RESOLVE sets the level.** CA-wide hourly MW, net of behind-the-meter PV, for all
  six California zones (PG&E, SCE, SDG&E, IID, LADWP, NCNC) — not just the
  investor-owned utilities. The load is RESOLVE's Baseline **plus** its additive
  overlay components (building electrification, EVs, data centres, climate impacts,
  storage losses, minus achievable efficiency). Baseline alone would understate
  2035 CA-wide gross load by 118 TWh.
- **County shares come from ReEDS**, normalized. ReEDS never contributes a load
  level.
- **Within each county**, the split across buses follows a measurement — see
  "Variants" below.

Model years available: **2024–2045**. That ceiling is RESOLVE's: its Baseline — the
only weather-varying component — stops at 2045, so a later year could only be
produced by extrapolating, which this package does not do.

Weather years available: **2000–2022**.

## Files

```
shares/<variant>.npz              the allocation: 288 cells x N buses, one per variant
statewide/<series>.csv.gz         CA-wide hourly MW, one per (model year, weather year)
code/expand.py                    reconstructs any hourly file from the two above
summary/                          per-variant and per-bus energy and peak, county table
reference/                        RESOLVE statewide series and scaling, bus metadata, maps
```

### Why it is shipped this way

The share matrices do not depend on the model year or the weather year — only on the
allocation settings. So the allocation is stored once and each combination costs one
8,760-row statewide series. A grid that would be **[recompute] GB** as hourly CSVs is
**[recompute] MB** here, and `expand.py` reproduces any hourly file **byte for byte**
identical to one written directly.

A "cell" is a (month, hour-of-day) pair in fixed PST — 12 × 24 = 288. Load within a
cell is allocated by one share vector, which is why the matrix is small.

### Getting an hourly file

```bash
python code/expand.py --list
python code/expand.py --variant <variant> --series <series>
python code/expand.py --all --out-dir hourly/     # check your disk first
```

Output is a wide CSV, `datetime_pst, hour_of_year, Demand_MW_z<bus_i>…`, one row per
hour, values in MW at one decimal place. Column `Demand_MW_z1065` is CATS bus 1065.
Only numpy and pandas are needed.

**`datetime_pst` carries the weather year's calendar**, because that is where the
shape comes from; the model year sets the level. `hour_of_year` (1–8760) is the
position within the year and is the stable key across combinations.

## Variants

Each variant is the same county-first allocation with a different **within-county
weight** — the measurement deciding how a county's energy splits across its buses.
The ReEDS county totals are identical across variants.

| variant | within-county weight |
|---|---|
| `[recompute]` | see `summary/variant_summary.csv` |

Two settings apply to every variant in this package:

- **Substation buses only.** Every CATS `AddedNode` is unloaded, including those
  CATS itself loads. The pool is **[recompute]** buses rather than 2,467. County
  totals are unaffected: an excluded bus's load is re-split within its own county.
- **Uncovered pool split evenly.** A county's load that no measurement reaches is
  divided equally among its remaining buses, rather than in proportion to CATS's own
  bus loads. This is deliberate: proportional-to-CATS would make roughly half the
  allocation a function of CATS itself, so this package could not be compared
  against CATS independently. The trade-off is that CATS's own variation within a
  county's uncovered buses is not reproduced.

**100% of load is allocated in every variant.** A separate figure,
`envelope_governed_share` in `summary/`, is the share of load whose *within-county
split* is measurement-driven: **[recompute]**. Those are different quantities and
the distinction matters.

## Caveats

1. **Weather years move only part of the load.** RESOLVE's overlay components carry
   no weather dimension — only the Baseline does, about three-quarters of the load.
   So the spread across weather years is narrower than a Baseline-only view would
   suggest. Measured spread: **[recompute]**.
2. **Bus-level placement is a model, not a measurement.** A substation's load is
   assigned to its nearest CATS bus (`reference/substation_node_map__*.csv` records
   which). Buses are not metering points.
3. **Load is net of behind-the-meter PV**, matching where the substation
   measurements are taken. Gross load would place the BTM offset on net-of-meter
   weights.
4. **Reference substations with no utility profile are placed spatially**, so they
   do not follow the nodal-map variants, and they carry a borrowed diurnal shape
   rather than a measured one. Counts are in `summary/`.
5. **Conservation.** The CA-wide total is preserved exactly in every hour, in
   floating point and at the printed precision: the per-hour residual is at most
   half of one print unit. Reported as
   `max_hourly_conservation_error_mw` — **[recompute]**.

## Provenance

| | |
|---|---|
| Source of the level | RESOLVE, scenario in `reference/resolve_scaling_y*.csv` |
| Source of county shares | ReEDS, county year in `summary/county_allocation.csv` |
| Network | CATS (California Test System) bus list |
| Built by | `code/build_resolve2035_cats_package.py` |
| Numbers verified by | `code/deliverable_numbers.py` |
