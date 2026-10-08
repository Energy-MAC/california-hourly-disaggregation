# External seasonal substation loads as a within-county weight source

An outside measurement may give **one load value per substation per season** —
summer and winter — rather than the utilities' (month, hour_pst) percentile
envelopes. This document covers how such a file enters the pipeline, how its
names are resolved, and what it changes.

> **Every measured number here comes from a SYNTHETIC fixture** built in-repo to
> exercise the machinery, not from a real external file. They demonstrate the
> mechanism and its magnitude; recompute them from the real input before quoting
> any of them as a result. The recompute lives in
> `scripts/load_projection/deliverables/deliverable_numbers.py`, section I.

## What this is, and what it is not

It is a new value of the existing **`county_weights`** axis. The allocation stays
county-first: ReEDS sets how much energy each county gets, and this measurement
sets only how that energy splits among the county's buses. It is therefore **not
a new `Approach N`**, it adds no output-folder prefix, and it changes no
`run_tag()` — the 25 existing GenX allocations and their citation ids are
untouched.

Because every consumption site normalizes, the weights are **relative only**. The
absolute MW in the input file cancel; what survives is the cross-substation
pattern and the summer:winter ratio.

### Where it is implemented

| Concept | Function | File |
|---|---|---|
| Input contract, name cascade, shape | `read_input`, `resolve_names`, `normalized_shapes`, `build_weight_tables` | `src/load_projection/external_loads.py` |
| The CLI that writes the tables | — | `scripts/load_projection/external_loads/build_external_weights.py` |
| Per-cell weight (the within-county split) | `external_cell_weights()` | `scripts/load_projection/genx/rescale_genx_demand.py` |
| Static weight (sets coverage, and so α) | `external_node_weights()` | same |
| Which source a run uses | `county_weight_src()` → `COUNTY_WEIGHT_SRCS` | same |
| Deliverable variants | `county_first_variant(..., external=)`, `--external-loads` | `scripts/load_projection/deliverables/build_resolve2035_cats_package.py` |
| Guards | — | `scripts/load_projection/external_loads/test_external_loads.py` |

## Input contract

One row per substation. Only the first three columns are required:

```
name,utility,summer_load,winter_load,lat,lon,voltage_kv
HOLLISTER,pge,56.63,41.20,36.8525,-121.4016,115
Jenney,,12.19,9.87,37.7724,-122.2429,115
```

| column | required | what it does |
|---|---|---|
| `name` | yes | the resolution cascade: utility names first, then the reference table |
| `summer_load`, `winter_load` | yes | the block levels |
| `lat`, `lon` | recommended | **the valuable additions.** Place a row on a bus without its name having to match the reference table, disambiguate every shared name, and distance-check the name matches |
| `voltage_kv` | useful | with coordinates, enables the same voltage-restricted placement the `voltres` map variant uses, via `band_to_cats_class` |
| `utility` | optional | a cross-check and a route hint. **Not** needed to disambiguate when coordinates are supplied — see below |

`long`, `latitude` and `voltage` are accepted as aliases. Any other column is
carried into the manifest for provenance and otherwise ignored. Min/max profiles
are not used: the weight is a level.

`summer_load` is the `MayOct` half-year block (months 5–10) and `winter_load` is
`NovApr` (11–4): **spring lumps with winter**. That split explains 28.9% of the
month-hour CAISO load variance against 3.2% for spring+summer, the worst of the
six contiguous options — see `src/load_projection/cells.py`.

### Units

The loads may be in **arbitrary units**. Every consumption site normalizes, so the
scale cancels; what survives is the cross-substation pattern and the
summer:winter ratio. `--units {relative,mw}` (default `relative`) records which
it is, and the emitted value column is called **`weight`**, never `load_mw` — a
column named for megawatts invites a misreading that cannot be undone later.

### Coordinates make `utility` optional

Measured: of the **48** normalized names more than one utility uses, **47** have
coordinates for two or more candidates, and in every one of those the candidates
are at least **65 km** apart (median **558 km**; closest pair `pico` at 65.1 km).
Only **`soquel`** cannot be separated this way. So with `lat`/`lon` supplied,
proximity settles the collisions with a wide margin and `--name-coord-tol-km`
defaults to 25 km, four times tighter than the floor.

The name cascade still earns its place: a named match is what lets a row ride the
`--map` axis and carry its **own** envelope shape, the highest-variability option.
Coordinates disambiguate and verify; they do not replace the name match.

## Name resolution — a strict cascade

Each row takes the **first** rule that fires. Matching uses the canonical
`norm()` from `scripts/data/substations/build_cec_name_dictionary.py`, and per
CLAUDE.md only `basinSourceDictionary.csv` or `cecSourceDictionary.csv` may serve
as a name reference. This path uses the **basin** one, because the external names
are keyed to `ca_substations_2022.csv`.

| # | Rule | Route | Rides the `--map` axis? |
|---|---|---|---|
| 1 | a profiled utility substation — by `(utility, name)`, else by name when unambiguous, else by **proximity** among the candidates | `utility_direct` | yes |
| 2 | `norm(name)` is a `BasinName` in the inverted `basinSourceDictionary` whose `SourceName` is profiled | `utility_dict` | yes |
| 3 | the row's **own `lat`/`lon`** | `coord_spatial` | **no** |
| 4 | `norm(name)` is a record in `ca_substations_2022.csv` with a CATS bus within the join threshold | `cec_spatial` | **no** |
| 5 | otherwise | `unresolved` | — |

Rule 1 precedes rule 2 because **the dictionary is an exceptions list, not a
complete mapping**. Rule 3 precedes rule 4 because a row's own coordinates beat a
name lookup into a reference table. Rules 3 and 4 are what let this source reach
substations the utilities do not profile.

### Coordinate placement reuses the production function

Rule-3 rows are assigned by `map_loads_to_nodes.build_mapping()` — the same
nearest-node function the nodal maps themselves are built with — called with that
module's own defaults, so these rows land on the same candidate bus set and split
across ties by the same rule. There is no second nearest-node search.

Two things to know about that reuse: `build_mapping` always appends the four
**synthetic ReEDS substations** (Del Norte / Lassen / Modoc / Siskiyou) because
its own callers want them, and they are dropped here — leaving them in would
invent four weighted substations out of nothing. And a tied row genuinely splits
across buses, so spatial rows carry tie shares exactly as named rows do; the
shares are asserted to sum to 1 per row.

### Two checks the coordinates make possible

- **`name_dist_km`** — the distance from a row's own coordinates to the substation
  its *name* matched. A large value means the name agreed but the location did
  not. Always reported (in the report, the manifest and the run log);
  `--max-name-dist-km` (default off) demotes such a row to rule 3 rather than
  trusting the name.
- **Duplicate targets.** Two input rows can normalize onto the same substation
  (`DRUM` and `Drum 1` both reach `DRUM` through the dictionary; a trailing space
  does it too), or two spatial rows onto one bus. Their weights are **summed**,
  which is right when they really are two measurements at one site and wrong when
  it is a near-duplicate name — so it is always listed in the manifest and the run
  log, and `--on-duplicate error` refuses outright.

`resolution_report.csv` carries **every** input row with its route and status.
Nothing is dropped silently.

### What is refused rather than guessed

- **A name several utilities share** (48 of them: `alhambra`, `lucerne`,
  `cottonwood`, …) is reported unresolved with its candidates named. It must not
  fall through to rule 3: a utility substation placed by coordinates is a
  different assignment, and letting that happen quietly would misattribute load.
  Supply a `utility` column instead.
- **A many-to-one dictionary inversion** raises. The file maps utility → basin,
  and reversing it is ambiguous in real data (PGE `drum 1` and `drum 2` both point
  at `DRUM`; SCE `cal city` is claimed by two substations). One such key exists
  today, and it must be fixed in the dictionary rather than guessed here.
- `SourceName`s absent from the profiled fleet (`sce/inyo sce`, `sce/outplaw`) are
  dropped on inversion.
- `name == "Unknown"` reference records are dropped, as
  `process_substations_clean.py` already drops them.

### Route 2 is map-independent — a real limitation

Rule-3 rows are placed on their nearest CATS bus via the existing
`data/checks/compare_cats_basin/basin_cats_join.csv` (2 km threshold), not through
one of the five nodal maps. So map-sensitivity comparisons do not apply to them.
Recorded per row as its own route, and in the manifest, rather than hidden.

## Shape

Each substation's seasonal level is spread over its block's 144 cells. The shape
is its own utility envelope, normalized to mean 1 within the block:

```
load_mw(s, m, h) = level(s, block(m)) * env(s, m, h) / mean of env(s, .) over that block
```

so the seasonal level comes back **exactly as the block mean** — verified to
5.6e-08. `--shape-col` picks the envelope column: `avg_load` (default, the
envelope midpoint, matching the external level's central-tendency meaning) or
`max_load` (sensitivity; what the envelope weight source itself uses).

The **final** shape is renormalized to mean 1 within each (unit, block) after the
whole fallback chain has run. Without that step a substation whose profile is
missing cells (72 of 387,936 slots are) carries its own shape on most of them and
a borrowed one on the rest, so the combined mean is not 1 and the level does not
come back as the block mean. Renormalizing makes the invariant unconditional and
does not touch the hour-to-hour pattern.

| `--shape` | within-block CV of `load_mw` | meaning |
|---|---|---|
| `utility` | **0.221133** (median) | borrows the measured diurnal pattern |
| `flat` | **0.000000** | what a seasonal-only input literally contains |

A substation with a level but no envelope — every rule-3 and rule-4 row, plus any
named-route gap — takes its **county's mean** normalized shape, falling back to
the statewide fleet mean where a county has no profiled substation. Counted in the
manifest.

### Optional shape hook (`--shape-file`)

Accepts `utility,season,hour_pst,shape` (season `summer`/`winter`), renormalized
to mean 1 within each (utility, block) on load so supplying a curve cannot move
the between-utility energy split. With `--shape file` it becomes the whole shape
source; otherwise it replaces the **county-mean fallback** for rows with no
envelope of their own, which is where it genuinely helps.

**Measured caveat, so nobody reaches for it expecting more than it gives.** A
shape shared by every substation of a utility multiplies them all by the same
hourly factor, so the ranking *within* that utility never changes:
Spearman(hour 10, hour 18) within PGE = **1.000000** exactly, and statewide
per-bus share CV is **0.104** against **0.187** for each substation's own
envelope. It buys **between-utility reordering only**. The utility curves are
genuinely different (MayOct noon: PGE 0.666 of its own mean vs SCE 1.035, a 1.56×
spread), so it is not nothing — it is just not within-utility variability.

The costed upgrade, if that path is ever taken: index the shape by
**(utility, season, size-bin, hour)**. Normalized shape rises monotonically with
substation size (MayOct midday Q1 small 0.724 → Q4 big 0.910; PGE alone 0.485 →
0.795, a 1.64× spread), which restores within-utility reordering — PGE
Spearman(h10, h19) falls to **0.888**. Still weaker than each substation's own
envelope (**0.613**), which is why the own-envelope shape remains the default.

### Why `flat` exists, and why it is not the stochastic story

Feeding a seasonal-only input to Approach 2 as a weight source is a **null
result**, not a stochastic result. `stoch_cell_weights` consumes the per-cell
*mean* of the draws, and `E[L_s] = μ_s` because `z` and `ε` are both mean-zero
within a cell. Measured: the per-cell stochastic weights correlate **0.999915**
with the raw input levels, total absolute deviation **1.3%** as normalized shares
— with the noisiest σ option at 3 draws. One value per season has no per-cell
shape for the model to contribute.

This is the same null the repo already guards with its `--weights stoch` +
`--level static` refusal. `--shape flat` is kept as the honest baseline that makes
the contrast visible; `--shape utility` is what restores per-cell shape.

## What it changes: coverage, not bus count

Only the **1,347** utility-profiled substations carry a measurement today, so
municipal territory is filled by county-first's **uncovered** split. Giving
reference substations a load moves part of that territory into the
measurement-driven covered set.

State this precisely: **100% of load is always allocated.** What rises is the
share of load whose *within-county split is measurement-driven*
(`envelope_governed_share` in the run meta). Those two ideas are not the same and
the confusion is easy to cause.

Measured on the synthetic fixture (map `prox`, `pool=cats_loaded`,
`uncovered=cats`, `alpha ratio`):

| Weight source | covered buses | measurement-driven | uncovered |
|---|---|---|---|
| `envelope` (published default) | 1,028 | **48.53%** | 51.47% |
| `external`, utility names only | 1,018 | 47.66% | 52.34% |
| `external`, utility + reference names | **1,741** | **73.40%** | 26.60% |

The utility-only row lands near the envelope baseline, as it must — same
substation set, different measurement. The gain of **+24.87 pp** comes entirely
from rule-3 rows.

Where it lands matters as much as the total. Counties gaining most:

| County | covered buses | α (envelope → external) |
|---|---|---|
| Sacramento | 2 → **177** | 0.9913 → 0.2338 |
| Los Angeles | 181 → 302 | 0.5617 → 0.2688 |
| Santa Clara | 34 → 59 | 0.4848 → 0.1061 |
| San Bernardino | 77 → 105 | 0.4577 → 0.2606 |

Sacramento is the `load-profile-request` skill's own canary ("Sacramento and Los
Angeles are not near-empty"), and it is exactly the muni-coverage gap
`validation-checks` blames for the large county errors.

## Runbook

```bash
B=scripts/load_projection/external_loads/build_external_weights.py

# the shaped variant, and the degenerate baseline to contrast it against
python $B --input my_seasonal_loads.csv --shape utility
python $B --input my_seasonal_loads.csv --shape flat

# guards
python scripts/load_projection/external_loads/test_external_loads.py

# into the deliverable (one variant per folder named)
python scripts/load_projection/deliverables/build_resolve2035_cats_package.py \
    --year 2040 --external-loads external_utility__prox,external_flat__prox
python scripts/load_projection/deliverables/deliverable_numbers.py
```

Outputs land in `data/processed/load_projection/external_loads/<tag>/`:

| File | Contents |
|---|---|
| `external_substation_weights.csv` | `utility, substation_name, month, hour_pst, load_mw` — rule 1 and 2 rows |
| `external_node_weights.csv` | `node, month, hour_pst, load_mw` — rule 3 rows, already on a bus |
| `resolution_report.csv` | every input row: `name, route, utility, substation_name, node, dist_km, status` |
| `manifest.json` | shape mode, map, reference tables and their md5s, per-route counts, refused ambiguities |

All **288** cells are emitted so one artifact serves both the 120-cell
representative-week runs and the 288-cell deliverable; each consumer restricts as
it needs.

## Invariants a reader relies on

Checked by `test_external_loads.py`:

1. A caller that sets no `county_weights` gets the published envelope split, bit
   for bit (max share diff **0.0** across 2,466 buses). An unknown value raises
   rather than silently falling back.
2. Weights are non-negative and finite; `node` is a plain string matching the
   `Demand_MW_z{id}` suffix, never a float.
3. 144 cells per (substation, block); the normalized shape averages exactly 1
   within each block.
4. Per-cell share vectors sum to 1 (max deviation **0.0** over 288 cells) and no
   share is negative.
5. The measurement-driven share rises when reference substations are added, and
   stays at the envelope baseline when they are not.

## Open items

- **No σ is implied anywhere on this path.** `envelope_cell_weights` and its
  external analogue read a level only. Synthesizing q10/q90 from a chosen
  coefficient of variation was considered and rejected: it would write a
  modelling assumption into a file shaped like measured data, and Approach 2's
  claim is that its marginals are *identified* by the envelopes. If the
  stochastic weight source is wanted on this input, σ comes from
  `generate_stochastic.py --sigma-source proportional-cv` at run time, where it
  is visible in the run tag.
- **GenX comparison runs** driven by this source through
  `rescale_genx_demand.py`'s CLI would need a real flag and a `run_tag()` slot,
  which touches stable citation ids. Deliberately not done; the deliverable path
  needs neither.
- **`--shape utility` for rule-3 rows** uses a borrowed county shape, so those
  buses carry a pattern no one measured at that location. The count is in the
  manifest; weigh it when interpreting diurnal results for municipal territory.
