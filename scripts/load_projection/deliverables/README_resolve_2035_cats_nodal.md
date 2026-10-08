# California 2035 hourly load at CATS transmission buses

Hourly (8,760-h) electricity demand for **2035** at every load-carrying bus of the
**CATS** synthetic California transmission network. Statewide load comes from the
**CPUC/E3 RESOLVE** 2035 forecast for all six California zones. It is spread across
buses county by county: **ReEDS county shares** set how much energy each county
gets, the utilities' **measured substation data** splits it among buses a substation
reaches, and **CATS's own demand pattern** splits it among the buses no substation
reaches.

Six variants are provided. They vary three things and **none of them changes any
county's total** — every variant allocates 100% of RESOLVE's CA-wide load.

| | |
|---|---|
| Forecast year | 2035 |
| Forecast source | RESOLVE 2024–2026 IRP cycle — Baseline (`CHP_Not_Retire`) **plus all overlay components** (EVs, building electrification, data centres, efficiency, climate, storage losses) |
| Geographic scope | **All of California** — all six RESOLVE zones (PG&E, SCE, SDG&E, IID, LADWP, NCNC) |
| Load definition | **Net of behind-the-meter PV** |
| CA-wide annual energy | **404.13 TWh** net (gross 445.76 − BTM PV 41.63) |
| CA-wide peak | **76,173 MW** (`hour_of_year` 5,730 = 2005-08-27 17:00 PST) |
| Weather year | 2005 shape (median CA-wide net annual peak of RESOLVE's 23) |
| Time convention | **Fixed PST (UTC−8), hour-beginning, no DST**, 8,760 hours |
| Buses carrying load | **2,466** — every bus CATS itself loads (2,465 after rounding) |
| Units | MW, one decimal (CATS's own grid) |

---

## 1. Quick start

Each file in `nodal_hourly/` is a gzipped CSV, 8,760 rows × 2,468 columns:

```
datetime_pst,hour_of_year,Demand_MW_z1,Demand_MW_z2,Demand_MW_z3,...
2005-01-01 00:00:00,1,18.4,7.2,11.9,...
```

- `datetime_pst` — fixed-PST hour-beginning timestamp (see §6.4 on the year label)
- `hour_of_year` — 1…8,760, the year-agnostic key
- `Demand_MW_z{N}` — demand in MW at CATS bus `bus_i = N`. The column name follows
  CATS's / GenX's own `Demand_data.csv` convention, so these drop straight into a
  GenX zone table.

```python
import pandas as pd
df = pd.read_csv("nodal_hourly/countyfirst_envelope_catsprop__prox.csv.gz",
                 index_col="datetime_pst", parse_dates=True)
buses = df.filter(like="Demand_MW_z")
buses.sum(axis=1).max()        # 76173.0 MW CA-wide peak
buses.sum().sum() / 1e6        # 404.13 TWh
```

```r
df <- data.table::fread("nodal_hourly/countyfirst_envelope_catsprop__prox.csv.gz")
```

**If you only want one file, take `countyfirst_envelope_catsprop__prox.csv.gz`.**

---

## 2. What is in the box

```
nodal_hourly/
  countyfirst_envelope_catsprop__prox.csv.gz      <- start here      (31.4 MB)
  countyfirst_envelope_catsprop__voltres.csv.gz     bus-assignment axis
  countyfirst_envelope_equalsplit__prox.csv.gz      uncovered-split axis
  countyfirst_stochmean_catsprop__prox.csv.gz       weight-source axis
  countyfirst_stochdraw0_catsprop__prox.csv.gz      one stochastic realization
  countyfirst_stochdraw1_catsprop__prox.csv.gz      another realization

summary/
  variant_summary.csv       per variant: TWh, peak, buses, conservation error
  node_annual_mwh.csv       bus x variant annual energy (MWh)
  node_peak_mw.csv          bus x variant annual peak (MW)
  county_allocation.csv     per county: ReEDS share, bus counts, alpha, the
                            uncovered/measured pool split -- the audit trail

reference/
  resolve_2035_statewide.csv   the hourly target per RESOLVE zone: baseline_mw,
                               overlay_mw, gross_mw, btm_mw, net_mw. Everything
                               sums back to this.
  resolve_2035_scaling.csv     per zone: baseline energy ratio, overlay energy,
                               gross energy, BTM capacities
  resolve_2035_components.csv  every overlay component's 2035 energy, with the
                               RESOLVE scenario each came from
  cats_node_metadata.csv       bus_i, kV, Type, Lat, Lon, county for every bus used
  node_shares_static.csv       each bus's period-average share of CA-wide load
  substation_node_map__prox.csv     substation -> bus, share, distance
  substation_node_map__voltres.csv  same for the voltage-restricted rule

code/
  build_resolve2035_cats_package.py   regenerates everything (§7)
  deliverable_numbers.py              recomputes every number quoted here
```

`cats_node_metadata.csv` carries `Lat`/`Lon` and `county_name`, so joining it to
`summary/node_annual_mwh.csv` gives a mappable table in one line.

---

## 3. The six variants

All six are the same allocation — ReEDS county shares, `alpha = u/n`, identical
county totals, 100% of CA-wide load placed. They vary three things.

**Axis 1 — how a county's *uncovered* pool is split** (the ~51% of load reaching
buses no metered substation maps to). **This is by far the largest axis.**

| Name | Rule |
|---|---|
| `catsprop` | Each uncovered bus takes a share proportional to **its own load in CATS's demand table**. CATS does not treat these buses as interchangeable, so this keeps information a flat split throws away. |
| `equalsplit` | Flat — every uncovered bus in a county gets `1/u` of that county's uncovered pool. |

**Axis 2 — substation → bus assignment:**

| Suffix | Rule |
|---|---|
| `prox` | **Nearest bus.** Each substation goes to its closest eligible CATS bus; buses within 250 m of the minimum share it equally. |
| `voltres` | **Voltage-consistent nearest bus.** The bus's CATS voltage class (66/115/230 kV) must match the substation's own high-side voltage, falling back to unrestricted nearest where no same-class bus is in range. |

**Axis 3 — which measurement weights a *covered* bus:**

| Name | Weight source |
|---|---|
| `envelope` | The substation's mean `max_load` (≈90th-percentile) envelope, per (month, hour) cell. |
| `stochmean` | The mean of the project's Monte Carlo substation model across its draws, per cell. |
| `stochdraw0/1` | Two single realizations, so the ensemble spread is visible rather than averaged away. |

### How much they actually differ

Reallocation = ½·Σ|x−y|/Σx, the share of load sitting on different buses.

| Comparison | Annual | Hourly | Pearson | Spearman |
|---|---|---|---|---|
| **uncovered split: `catsprop` vs `equalsplit`** | **19.17%** | **19.18%** | **0.7145** | 0.7337 |
| map: `prox` vs `voltres` | 6.04% | 6.06% | 0.9026 | 0.9580 |
| weights: `envelope` vs `stochmean` | 1.15% | 3.34% | 0.9987 | 0.9990 |
| weights: `envelope` vs `stochdraw0` | 1.29% | 3.61% | 0.9983 | 0.9989 |
| `stochdraw0` vs `stochdraw1` | 0.98% | 1.46% | 0.9991 | 0.9994 |
| `stochmean` vs `stochdraw0` | 0.64% | 0.95% | 0.9996 | 0.9997 |

**The uncovered split dominates everything else by a factor of three.** It governs
~51% of CA-wide load, so if you are comparing results across these files, that is
the axis to care about. The `equalsplit` file is included precisely so you can
measure its effect rather than take `catsprop` on faith.

What `catsprop` does to the municipal counties, where *every* bus is uncovered:

| County | `catsprop` per-bus GWh | `equalsplit` per-bus GWh |
|---|---|---|
| Sacramento (231 buses) | 9.8 – 481.4 (**49×** spread) | 53.7 (flat) |
| Imperial (28 buses) | 27.2 – 594.6 (**22×** spread) | 169.7 (flat) |

**Please do not read `envelope` and `stochmean` as two independent estimates.** They
correlate at 0.9987 on annual energy per bus and share the same county totals by
construction. They are the same allocation weighted two ways; averaging them buys
nothing, and a disagreement between them is a weighting sensitivity, not a
confidence interval.

### What the stochastic variants do and do not add

They add an **ensemble**: two realizations differ by 0.98% of annual energy and
1.46% hourly — a real spread the deterministic envelope cannot give you.

They do **not** add within-year shape detail. Median coefficient of variation of a
bus's hourly share of CA-wide load:

| Variant | Measured-pool buses | Uncovered-pool buses |
|---|---|---|
| `countyfirst_envelope_catsprop__prox` | **0.1543** | 0.0039 |
| `countyfirst_stochmean_catsprop__prox` | 0.1079 | 0.0039 |
| `countyfirst_stochdraw0_catsprop__prox` | 0.1226 | 0.0039 |

The stochastic weights are *smoother* across cells than the `max_load` envelope, not
sharper — the model's per-cell mean sits near the middle of each envelope while
`max_load` tracks its upper edge. For maximum within-year contrast between buses,
use the envelope variant.

---

## 4. How it was built

### 4.1 The CA-wide 2035 target

RESOLVE publishes its load as a **Baseline plus additive overlay components**, and the
two are built differently. Both are in `reference/resolve_2035_statewide.csv` as
separate columns so you can see the split.

**Baseline** — one 8,760-hour shape per weather year 2000–2022, each rescaled to the
same annual energy, so it carries shape but no level. It is scaled to 2035 by RESOLVE's
own `scale_by_energy` logic, `E_2035 / E_2024`.

**Overlays** — electrification and demand growth, each with its own hourly profile
indexed by **model year**, already at 2035 levels. These are *selected*, not projected:

| Overlay | CA-wide 2035 TWh |
|---|---|
| AAFS — building electrification | +34.64 |
| Data centres | +33.10 |
| Light-duty EVs (`Baseline_LDVs` + `AATE_LDVs`) | +49.72 |
| Medium/heavy-duty EVs (`Baseline_MHDVs` + `AATE_MHDVs`) | +10.12 |
| Climate impacts | +1.33 |
| Storage losses | +0.27 |
| AAEE — achievable energy efficiency | **−10.77** |
| **total overlays** | **+118.41** |

```
gross_2035(t) = baseline_shape(t) x (E_2035 / E_2024)  +  SUM overlay_2035(t)
btm_2035(t)   = btm_pv(t) x (cap_2035 / cap_2024)
net_2035(t)   = gross_2035(t) - btm_2035(t)
```

| Zone | Baseline 2035 | Overlays 2035 | Gross 2035 | BTM cap 2035 | **Net 2035 TWh** |
|---|---|---|---|---|---|
| PG&E | 123.45 | 67.34 | 190.79 | 12,942 MW | **172.44** |
| SCE | 117.85 | 39.13 | 156.98 | 9,671 MW | **142.54** |
| SDG&E | 24.46 | 7.85 | 32.31 | 3,179 MW | **27.51** |
| LADWP | 32.62 | 2.49 | 35.11 | 1,412 MW | **33.01** |
| NCNC (SMUD+BANC+TIDC) | 24.09 | 1.52 | 25.61 | 1,073 MW | **24.11** |
| IID | 4.87 | 0.08 | 4.96 | 272 MW | **4.53** |

`E_y` = RESOLVE's `annual_energy_forecast`; `cap_y` = its `Customer_PV`
`planned_capacity`; overlay scenario and BTM scenario are both
`2024_IEPR_Local_Reliability`. Gross 445.76 − BTM 41.63 = **net 404.13 TWh**. The three
IOUs are 342.48 TWh and the three municipal zones 61.65 TWh, i.e. **15.3% of CA-wide net
load sits outside the investor-owned utilities** — which is why this package covers all
six zones.

Note the overlays carry **no weather dimension** — one profile per model year — so the
weather year varies only the Baseline, about 73% of 2035 load.

**Net, not gross:** the substation data used for the within-county split is measured at
the substation meter, downstream of rooftop solar. All columns are in
`reference/resolve_2035_statewide.csv`.

**Hour convention:** hour-beginning. The value stamped hour *h* is the load for
*h*:00→*h*+1:00, in fixed PST with no DST.

### 4.2 County-first allocation

For county *c* with `n` candidate buses, of which `s` have a substation mapped to
them and `u = n − s` do not:

```
w_c              = R_c / SUM_j R_j            ReEDS county share of the state
alpha            = u / n
covered bus i    = (1 - alpha) * w_c * e_i / SUM e     measured weight
uncovered bus i  = alpha * w_c * g_i / SUM g           CATS's own weight  (catsprop)
                 = alpha * w_c / u                     flat             (equalsplit)
```

`R_c` is the ReEDS county load reference, used **only** as a normalized share — its
levels never enter, they cancel in `R_c / Σ R_j`. RESOLVE sets the level; ReEDS sets
only the geographic split. `e_i` is the measured weight of the substations mapped to
bus *i* (envelope or stochastic, Axis 3). `g_i` is bus *i*'s own total load in CATS's
demand table.

`alpha = u/n` gives the uncovered buses exactly the share they would receive from an
even split over all `n` buses, while the covered buses' `s/n` is re-apportioned among
them by measured weight. **This is what fills LADWP, SMUD and IID territory: an
uncovered bus needs no substation data to receive load.**

**Load is placed only where CATS itself places load.** The candidate pool is the
**2,467** buses that carry load in CATS's own demand table (of 2,471 statewide; 4
fall outside any California county polygon). The one permitted exception is a bus
reached by a *direct name match* from a substation — an identity match to a specific
CATS bus is evidence that bus is the right place even if CATS leaves it at zero — and
the two maps shipped here assign purely by distance, so they invoke that exception
zero times.

Of the pool, **1,028 buses are substation-covered and 1,439 are not**:

| | Share of CA-wide load | Split by |
|---|---|---|
| Measured pool (substation-covered) | **48.5%** | utility substation data |
| Uncovered pool | **51.5%** | CATS's own pattern (`catsprop`) or flat (`equalsplit`) |

County weights use the ReEDS county table for **2023**, the latest available. Median
`alpha` is 0.541; 3 counties have no substation-covered bus at all (`alpha = 1`) and
2 have no uncovered bus (`alpha = 0`). No county's uncovered buses are entirely
unloaded in CATS, so `catsprop` never falls back to a flat split.

### 4.3 What the counties get

County totals are exact by construction in **every** variant — allocated share
matches the ReEDS share to within **0.00014 percentage points** across all 57
counties, in all six files.

| County | TWh | % of state | Buses |
|---|---|---|---|
| Los Angeles | 85.70 | 21.21% | 413 |
| San Diego | 51.70 | 12.79% | 126 |
| San Bernardino | 37.52 | 9.29% | 142 |
| Orange | 31.48 | 7.79% | 107 |
| Santa Clara | 21.00 | 5.20% | 66 |
| Riverside | 16.14 | 4.00% | 127 |
| Contra Costa | 14.62 | 3.62% | 83 |
| Kern | 12.63 | 3.13% | 122 |

The municipal-utility counties are properly loaded — **Sacramento 12.41 TWh across
231 buses, Imperial 4.75 TWh across 28, Stanislaus 6.43 TWh across 63.** A
substation-only allocation leaves these almost empty, because PG&E/SCE/SDG&E never
had metered substations there to scrape.

---

## 5. What is guaranteed

**100% of RESOLVE's CA-wide load is allocated, exactly, in every hour and every
variant.** The buses sum to the CA-wide net 2035 series in floating point and at the
0.1 MW precision the files are written in. Per-cell share vectors sum to exactly
1.000000000000, and largest-remainder apportionment is used so that rounding 2,466
values per row cannot leave a residual. Worst deviation across all six files:
**0.0501 MW**, exactly half the print grid, ≈0.66 ppm of the peak hour.

**No bus-hour is ever negative** (0 cells in all six variants).

**County totals are invariant** across all three axes, so swapping variants changes
only the within-county split.

---

## 6. Caveats — please read before using

**1. Half the allocation is CATS's own spatial pattern, not ours.** 48.5% of load is
split by measured utility substation data; the other 51.5% reaches buses no metered
substation maps to, and under `catsprop` it follows CATS's own demand distribution.
Two consequences. First, **if you intend to compare this against CATS, half of it is
not independent of CATS** — use `countyfirst_envelope_equalsplit__prox` for that
comparison instead. Second, where a county's buses are all uncovered (Sacramento,
Imperial), the *within-county* pattern is entirely CATS's, and the only thing this
package contributes there is the county total from ReEDS and the 2035 level from
RESOLVE.

**2. This is a plausible allocation, not a measurement.** Nothing here observes load
at a CATS bus. County totals come from a national capacity-expansion model's county
shares, the covered-bus split from data published by three of California's utilities,
and the uncovered-bus split from a synthetic network model. Treat per-bus values as a
defensible spatial prior.

**3. County weights are historic, not projected.** The ReEDS county reference has
observed years only (2016–2023); 2023 is used. There is no 2035 county-share
projection in it, so the allocation assumes county shares hold to 2035. The
sensitivity is small: using 2019 instead moves 0.73% of state load, 2016 moves 0.95%.

**4. Weather year 2005 is one of 23, and `datetime_pst` carries its calendar.**
Annual energy is identical across RESOLVE's weather years; the peak is not. CA-wide net
2035 annual peak runs from 73,941 MW (weather year 2021, −2.9%) to **82,698 MW (2022,
+8.6%)**; 2005 is the median at 76,173 MW, and the distribution is right-skewed. The
spread is narrower than the Baseline alone would give, because the overlays (27% of
load) have no weather dimension. **If your question is about peak or reliability rather
than energy, this file understates the risk** — regenerate the high years (§7).
Timestamps stay on the weather year because relabelling to 2035 would misalign weekdays
and weekends (1 Jan 2005 is a Saturday; 1 Jan 2035 a Monday); use `hour_of_year` as a
year-agnostic key.

**5. The bus set matches CATS's loaded set, 6 buses short.** No bus here carries load
where CATS leaves the bus empty — enforced by construction (§4.2). In the other
direction, **6** of the 2,471 buses CATS loads receive nothing, holding 0.2% of
CATS's own demand, each for a specific reason: **4** sit outside every California
county polygon (buses 2408 and 6994 just over the Mexican border, 2759 in Oregon,
7610 in Arizona), so a county-first allocation has no county share to give them;
**1** is bus 2318, Alpine County's only bus, and Alpine's ReEDS share rounds to zero;
**1** receives a share too small to survive rounding to 0.1 MW in any hour.

**6. BTM PV growth is applied at the zone level.** The 2035 capacities in §4.1 are
per RESOLVE zone; the *within-zone* spatial pattern of net load comes from historical
measurements, so 2035 rooftop solar is implicitly distributed as today's.

**7. The substation data are percentile envelopes, not observed days.** The `max_load`
values behind `e_i` are ~90th-percentile MW per (month, hour) cell computed by each
utility over an undisclosed lookback window — not metered peak days.

**8. Per-bus peaks are non-coincident.** They sum to 84,320 MW against a 76,173 MW
CA-wide peak (ratio 1.11). Do not size anything off a sum of per-bus maxima. Per-bus
annual energy: median 84.5 GWh, p90 391.0 GWh, max 2,302.7 GWh.

**9. No transmission feasibility check.** These are demand injections at buses.
Nothing here verifies the CATS network can serve the resulting pattern.

---

## 7. Regenerating

`code/build_resolve2035_cats_package.py` builds this package and
`code/deliverable_numbers.py` recomputes every number quoted above. Both need the
parent research repository (RESOLVE raw inputs, processed substation profiles, CATS
files, the ReEDS county reference and the prebuilt nodal maps), so they will not run
standalone from this folder — they are included so the method is auditable and the
parameter choices visible.

```bash
python build_resolve2035_cats_package.py                       # exactly this package
python build_resolve2035_cats_package.py --weather-year 2022    # the high-peak year
python build_resolve2035_cats_package.py --uncovered equal      # flat as the primary
python build_resolve2035_cats_package.py --county-year 2019     # older county shares
python build_resolve2035_cats_package.py --draws 0,1,2,3,4      # more realizations
python deliverable_numbers.py                                  # verify every figure
```

The builder's module docstring is the parameter reference. The stochastic model is
seeded upstream, so all six variants are exactly reproducible.

## 8. Provenance

| Input | Source | Role |
|---|---|---|
| Statewide load level | CPUC/E3 RESOLVE, 2024–2026 IRP cycle, Baseline `CHP_Not_Retire`, all six CA zones | Sets CA-wide hourly MW |
| County split | ReEDS (NREL) county load reference, 2023, **normalized shares only** | Sets each county's share |
| Covered-bus split | PG&E, SCE and SDG&E published substation load profiles (month × hour percentile envelopes), directly or through the project's stochastic substation model | Weights the 48.5% reaching metered substations |
| Uncovered-bus split | CATS's own `Demand_data.csv` per-bus load | Weights the 51.5% reaching other buses (`catsprop`) |
| Transmission network | CATS — California Test System (`CATS_buses.csv`, `Demand_data.csv`) | Defines the buses |
| Allocation code | `rescale_genx_demand.county_first_shares` from the *California hourly load disaggregation* project | The allocation |

Generated 2026-10-01. Questions about method or caveats should go back through
whoever sent you this folder.
