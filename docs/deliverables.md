# External deliverables — packaged disaggregations for outside users

Self-contained folders built for people **outside** this project, who get a zip and a
README rather than the repository. They are not a new method and never introduce one:
each is an existing allocation evaluated on a specified target, with a packaging layer
on top.

Packages live in `deliverables/<name>/` (plus a sibling `<name>.zip`) and are **not**
tracked in git — they are large, regenerable outputs. Builders live in
`scripts/load_projection/deliverables/`.

**To fulfil a new request of the form "I want load profiles for T using X", follow the
skill `load-profile-request`** — it carries the decision table, the source-capability
matrix, the acceptance tests and the list of mistakes already made. This document is
the per-package record it refers back to.

## Hard rules for anything in this folder

- **A deliverable is never a new `Approach N`.** If packaging a result seems to require
  a new method, that method gets its own `Approach N` section first, in the README and
  `docs/`, and the deliverable then cites it. Deliverable names must not look like
  approach names.
- **Reuse the existing allocation; import it, do not reimplement it.** A builder should
  construct a target and an args `Namespace`, then call the production functions. The
  2035 package imports `county_first_shares` / `expand_shares_to_cells` from
  `genx/rescale_genx_demand.py` so its allocation cannot drift from the pipeline's.
  Before designing anything, read `docs/genx_rescale.md` — the allocation you need
  probably already exists.
- **Every measured number quoted in a package README is script-backed** (user rule
  2026-08-14). Run the package's recompute script before editing any figure in it —
  never hand-edit a number to match a new run. Missing recompute path = a gap to close.
- **The package README is a template in the repo, not a file edited in place.** The
  builder copies `scripts/load_projection/deliverables/README_<name>.md` into the
  package on every run, and the builder deletes and rewrites the whole package
  directory — so edits made inside `deliverables/` are silently discarded.
- **Cover all of California unless there is a reason not to.** The substation fleet is
  PGE/SCE/SDGE only, so a substation-first allocation silently drops the ~20% of CA-wide
  net load in IID/LDWP/NCNC and leaves most CATS buses empty. County-first does not have
  that gap and is the default for anything bus-level. `resolve_hourly_profiles.csv`
  carries `demand_mw_net` for all six RESOLVE zones with no nulls.
- **A coverage gap that remains must be measured at the BUS level, not just the utility
  level** — how many of the target system's own loaded buses the package leaves at zero,
  and what share of that system's demand sits there. An outside user has no way to know
  this and will read an empty Los Angeles bus as a modelling result.
- **Filling a gap with an EXISTING allocation is correct; inventing one is not**
  (supersedes the narrower "disclose, do not fill" note of 2026-09-29, written before
  the county-first path was identified). County-first fills muni territory through its
  uncovered-bus pool and is an existing, documented method, so using it is reuse, not a
  new approach. What remains forbidden is writing a *new* muni-allocation method inside
  a deliverable.
- **Where no measurement reaches a bus, use the TARGET MODEL's own pattern, not a
  flat split** (user decision 2026-09-30). `uncovered_src(args) == "cats"` splits a
  county's uncovered pool by each bus's own CATS demand. This is the largest axis in
  the 2035 package (19.17% of CA-wide load vs a flat split) and follows the standing
  decision that CATS is taken as the true allocation.
- **Place load only where the target system places load** (user rule 2026-09-30).
  For CATS that means the buses CATS itself loads, with ONE exception: a bus reached
  by a direct name match from a substation, since an identity match to a specific bus
  is evidence that bus is the right place even where CATS leaves it at zero. Wired as
  `pool_src(args)` / `candidate_pool(args)` in `rescale_genx_demand.py`; the
  deliverable passes `pool="cats_loaded"`. **The GenX pipeline KEEPS the wider
  2026-08-12 pool - user decision 2026-09-30, do not narrow it.** Both this and the
  uncovered-split axis are therefore opt-in attributes with no CLI flag.
- **ReEDS contributes normalized county SHARES and never load LEVELS.** This holds in
  deliverables exactly as in the GenX rescaler. ReEDS 2035 projections exist; using them
  for the level was considered and **rejected** (user, 2026-09-30) — do not propose it
  again.

---

## `resolve_2035_cats_nodal` — CA-wide RESOLVE 2035 at CATS buses

Hourly 2035 demand at every load-carrying CATS bus, for an outside researcher.
Rebuilt CA-wide 2026-09-30 (user decision); the IOU-only first cut is under
"Superseded" below.

| | |
|---|---|
| Builder | `scripts/load_projection/deliverables/build_resolve2035_cats_package.py` (`--year` makes it any RESOLVE model year 2024-2045; the `2035` in the filename is historical) |
| Recompute | `scripts/load_projection/deliverables/deliverable_numbers.py` (sections A–H) |
| README template | `scripts/load_projection/deliverables/README_resolve_2035_cats_nodal.md` |
| Output | `deliverables/resolve_2035_cats_nodal/` + `.zip` |
| Method used | `county_first_shares` + `expand_shares_to_cells`, **imported** from `genx/rescale_genx_demand.py` — no new method, no reimplementation |
| Variants | 6 over three axes: uncovered split `{catsprop,equalsplit}`, map `{prox,voltres}`, weight `{envelope,stochmean,stochdraw0,stochdraw1}` |
| Bus pool | `pool="cats_loaded"` - 2,467 buses, every one loaded by CATS itself |
| Uncovered split | `uncovered="cats"` primary, `"equal"` shipped as the comparison |

**Division of labour between the two statewide sources, which is the point:**
RESOLVE sets the **level** (CA-wide hourly MW); ReEDS contributes **normalized county
shares only**. The choice of RESOLVE for the level is arbitrary and not the point of
the exercise; the county weights are.

### The CA-wide 2035 target

**CORRECTED 2026-10-01.** The first build used only the Baseline load component and so
understated CA-wide gross 2035 load by 118 TWh (327.35 vs 445.76). The user caught it by
checking RESOLVE directly. RESOLVE's load is **Baseline PLUS additive overlay
components**, and the two layers are constructed differently:

| Layer | Form | Treatment |
|---|---|---|
| Baseline (`{ZONE}_Baseline*`) | 8,760-h shape per WEATHER year 2000–2022, no level (201,480 rows) | scale by `E_2035 / E_2024` — RESOLVE's own `scale_by_energy` |
| Overlays (AAEE, AAFS, AATE/Baseline LDVs+MHDVs, Data_Centers, Climate_Impacts, Storage_Losses) | hourly profile per MODEL year 2024–2050, already at level (236,520 rows = 27 × 8,760) | **select the year; do NOT scale** |

The overlay profiles' hours sum to that model year's `annual_energy_forecast` exactly —
ratio 1.0000, asserted per component in `_overlay_hourly`, which raises if a future
source change breaks it. `CHP_Retire` is skipped (identical annual energy to
`CHP_Not_Retire`). Overlay scenario and BTM scenario are both
`2024_IEPR_Local_Reliability`; the `Planning_Scenario` alternative gives 439.10 TWh
gross, 1.5% lower.

CA-wide 2035 overlays, TWh: AAFS +34.64, Data_Centers +33.10, Baseline_LDVs +27.43,
AATE_LDVs +22.29, Baseline_MHDVs +5.61, AATE_MHDVs +4.52, Climate_Impacts +1.33,
Storage_Losses +0.27, **AAEE −10.77** → **+118.41 total**.

| Zone | IOU | Baseline 2035 | Overlays | Gross | BTM cap 2035 | net 2035 TWh |
|---|---|---|---|---|---|---|
| PGE | yes | 123.452 | 67.337 | 190.789 | 12,942 | 172.440 |
| SCE | yes | 117.851 | 39.129 | 156.980 | 9,671 | 142.538 |
| SDGE | yes | 24.459 | 7.850 | 32.309 | 3,179 | 27.505 |
| LDWP | no | 32.624 | 2.490 | 35.114 | 1,412 | 33.010 |
| NCNC | no | 24.091 | 1.522 | 25.613 | 1,073 | 24.110 |
| IID | no | 4.871 | 0.084 | 4.955 | 272 | 4.526 |

Gross 445.76 − BTM 41.63 = **net 404.13 TWh** at weather year 2005; CA-wide peak
**76,173 MW** at `hour_of_year` 5,730 (2005-08-27 17:00 PST). IOU 342.48 + non-IOU
61.65, so **non-IOU is 15.3% of CA-wide net load** (was 20.2% before the overlays, which
are concentrated in the IOUs).

Weather year 2005 is the **median CA-wide net annual peak** of the 23; spread 73,941 MW
(2021, −2.9%) to 82,698 MW (2022, +8.6%). The spread is narrower than the Baseline alone
would give because the overlays have **no weather dimension** — one profile per model
year — so weather varies only ~73% of the load. Adding the overlays also moved the
median-peak weather year from 2013 to 2005, so it must be recomputed, never carried over.
Timestamps keep the weather year's calendar (2005-01-01 is a Saturday, 2035-01-01 a
Monday), with `hour_of_year` as the year-agnostic key.

**Hour convention `# VERIFIED: sanity check` (2026-10-01).** RESOLVE is
**hour-beginning** — the value at hour *h* is load for *h*:00→*h*+1:00, matching
`hour_pst` and the processed EIA-930. Two independent tests: (i) June `Customer_PV`
weather factor peaks in hour 12, nonzero 5–19 symmetric about 12.0, centre of mass
11.59 (hour-ending would give ~12.6–13.2); (ii) RESOLVE **net** vs processed EIA-930
CISO, 2019, 8,760 common hours, best cross-correlation lag **0** (corr 0.898), both
peaking at hour 18. **Trap:** comparing RESOLVE *gross* against EIA *net* shows a
spurious 5-hour offset (13 vs 18) because BTM is added back on the gross side and peaks
at 10.4 GW in hour 11 — always compare net to net.

### The allocation (imported, not rewritten)

`county_first_shares(args, cache)` with `alpha="ratio"`, then
`expand_shares_to_cells(..., cells=all 288)`. The builder constructs only an args
`Namespace` (`map`, `system`, `alpha`, `county_year`) and a share matrix; every
allocation decision stays in `rescale_genx_demand.py`.

- `candidate_buses()` = **3,778** printed, **3,769** after the CA county
  point-in-polygon join; `candidate_pool(args)` with `pool="cats_loaded"` then
  restricts to **2,467** - the buses CATS itself loads (2,471 statewide, less 4 that
  fall outside every CA county polygon). 57 counties, none emptied (asserted;
  `candidate_pool` raises if any county loses all its buses).
- **1,028** buses substation-covered, **1,439** uncovered. `alpha = u/n`: median
  0.541, 3 counties at 1.0 (no substation bus at all), 2 at 0.0 (no uncovered bus).
- Pool split of CA-wide load: **measured pool 48.5%**, **uncovered pool 51.5%**. The
  restriction dropped **zero** covered buses - all 1,028 were already CATS-loaded - so
  it only shrank the uncovered pool, which is why the measured share ROSE from 34.5%
  to 48.5%. Under `uncovered="cats"` the uncovered 51.5% is no longer uninformed: it
  carries CATS's own spatial pattern.
- Result: **2,466 of 2,467 buses carry a share** in every variant (2,465 after 0.1 MW
  rounding). The 28 of 1,347 substations with a non-positive envelope are clipped to
  zero weight upstream - no special case added here. Covered-bus counts by variant:
  envelope/prox 1,028, envelope/voltres 998, stoch mean 1,026, draw0 1,027,
  draw1 1,026.

### County outcome — the check that it is wired up correctly

Allocated county share matches the ReEDS share to **0.000039 pp** (envelope/prox;
0.00014 pp worst over all six variants) across all 57 counties - exact by
construction and invariant to every axis, pool restriction included. The municipal counties an IOU-only allocation leaves
empty are properly loaded:

| County | TWh | % of state | buses | was (IOU-only cut) |
|---|---|---|---|---|
| Los Angeles | 85.698 | 21.206% | 413 | 56.8% of its CATS load zeroed |
| Sacramento | 12.407 | 3.070% | 231 | 98.7% zeroed |
| Stanislaus | 6.432 | 1.592% | 63 | 94.6% zeroed |
| Imperial | 4.753 | 1.176% | 28 | ~all zeroed |

**If Sacramento or Los Angeles comes out near-empty, the county allocation is not
wired up** — that is the acceptance test for any rebuild.

Against CATS's own loaded set: **0** buses carry load where CATS leaves the bus empty
(enforced; was 1,302 before the pool restriction), and **6** of the 2,471 buses CATS
loads receive nothing - 4 outside every CA county polygon (2408 and 6994 at the
Mexican border, 2759 Oregon, 7610 Arizona), 1 is Alpine's only bus and Alpine's ReEDS
share rounds to zero, 1 rounds below 0.05 MW. Together 0.2% of CATS's own demand
(1,428 / 45.6% in the IOU-only cut). By bus type: 1,859 `Substation` buses carry
222.77 TWh, 607 `AddedNode` buses carry 60.22 TWh. By class: 66 kV 1,789 buses /
205.59 TWh, 115 kV 520 / 60.98, 230 kV 149 / 15.47, 500 kV 8 / 0.94.

### Conservation

CA-wide load is exact in every hour: per-cell share vectors sum to 1.000000000000
(asserted in `share_matrix`, the run aborts otherwise), and `round_to_printed`
largest-remainder apportionment holds the 0.1 MW grid. Worst deviation across both
files **0.0501 MW** = half the grid ≈ 0.79 ppm of peak. **Zero negative bus-hours** —
all shares non-negative and the CA-wide net series is positive throughout, so the
Approach-2 negative-tail caveat does not apply to this package.

### Map axis

`prox` vs `voltres`: **3.14%** annual / **3.18%** hourly reallocation, Spearman
**0.9691**. Much smaller than the 11.05% it moved in the substation-only cut, because
the map can only reshuffle the 34.5% envelope pool — county totals and the equal pool
are invariant to it. Bus-share variability, median CV of a bus's hourly share: envelope
pool **0.1554** (n=1,027) vs equal pool **0.0047** (n=2,741) — a single median over all
buses reads ~0 and is misleading, so `deliverable_numbers.py` section F splits it.

### The within-county weight source axis (added 2026-09-30)

The weight enters county-first at exactly one point, so it is swappable:
`county_first_shares` -> `county_node_weights`, and `expand_shares_to_cells` ->
`county_cell_weights`. Both dispatch on `county_weight_src(args)`, i.e.
`getattr(args, "county_weights", "envelope")`:

| value | per-node weight | per-cell weight |
|---|---|---|
| `envelope` (default) | `envelope_node_weights` — mean `max_load` | `envelope_cell_weights` |
| `stoch` | `substation_annual` + `substation_to_node` | `stoch_cell_weights` |

**This is county-first with a different weight, NOT a new family and NOT a new
`Approach N`.** Everything downstream is weight-source agnostic: ReEDS county
totals, `alpha = u/n`, the equal pool, the renormalisation. Verified: the county
`county_share`, `alpha`, `equal_pool_share` and `envelope_pool_share` are identical
to 1.1e-16 between the two, and the acceptance test passes for every variant
(max deviation from the ReEDS share 0.00105 pp over 57 counties).

Rule-clean for the same reason `envelope_node_weights` documents: Approach 1's
disaggregated MWh was rejected as a weight because it already descends from ReEDS,
which would apply the same regional signal twice. Approach 2 descends from the
utility envelopes plus the target series, never from ReEDS.

**Deliberately no CLI flag on `rescale_genx_demand.py`.** Exposing it would add an
axis to `run_tag()`, and those tags are stable citation ids. Default-path
equivalence was checked against the pre-change build before anything else.
`substation_annual`'s `args.weights != "stoch"` guard was widened to admit
`county_weight_src(args) == "stoch"`; nothing else in that module changed.

Variants shipped: `countyfirst_envelope__{prox,voltres}`,
`countyfirst_stochastic_mean__prox`, `countyfirst_stochastic_draw{0,1}__prox`.
Measured (section F):

| Comparison | Annual | Hourly | Pearson | Spearman |
|---|---|---|---|---|
| **uncovered split catsprop vs equalsplit** | **19.17%** | **19.18%** | **0.7145** | 0.7337 |
| map prox vs voltres | 6.04% | 6.06% | 0.9026 | 0.9580 |
| envelope vs stochmean | 1.15% | 3.34% | 0.9987 | 0.9990 |
| envelope vs stochdraw0 | 1.29% | 3.61% | 0.9983 | 0.9989 |
| stochdraw0 vs stochdraw1 | 0.98% | 1.46% | 0.9991 | 0.9994 |
| stochmean vs stochdraw0 | 0.64% | 0.95% | 0.9996 | 0.9997 |

So the weight source moves very little ANNUAL energy; its contribution is the
draw-to-draw ensemble spread. **It does NOT add within-year shape** — median CV of a
covered bus's hourly share actually FALLS, 0.1543 (envelope) -> 0.1079 (stoch mean)
/ 0.1226 (draw0), because Approach 2's per-cell mean sits mid-envelope while
`max_load` tracks the upper edge. The README says this explicitly so the two are not
read as independent estimates.

### Why the stochastic POOL family is NOT shipped (and the failure is gate-dependent)

`stoch_pool_shares` + `expand_stoch_shares_to_cells` is structurally a **hold**
method: it sweeps a pool and copies every bus outside it through from a GenX
control. A fresh 2035 projection has no control, so non-swept buses get nothing.

**Correction to an earlier note here:** the coverage failure is **gate-dependent**,
not unconditional. Section H sweeps it (`--stoch-topoff equal` throughout):

| gate | counties gated | buses loaded | beta | LA | Sacramento | Imperial | max dev vs ReEDS |
|---|---|---|---|---|---|---|---|
| ReEDS reference | | | | 21.206% | 3.070% | 1.176% | |
| 2.0 | 0 | 1,026 | 0.0000 | 16.545% | 0.226% | 0.000% | 4.66 pp |
| 0.30 | 30 | 2,442 | 0.5799 | 20.997% | 0.095% | 0.000% | 4.41 pp |
| 0.05 | 50 | 3,192 | 0.6786 | 16.064% | 0.072% | 0.000% | 6.38 pp |
| 0.0 | 57 | **3,769** | 0.7278 | 13.604% | 7.676% | 2.600% | 7.60 pp |

Gate 0.0 **does** load the whole pool. The reason it is still not shipped is the
share formula, not coverage: `beta / n_unc` in `stoch_pool_shares` is a single
**statewide** constant with no county grouping, so 72.8% of load is distributed by
**bus count** — Sacramento's 289 buses are 7.7% of the pool and it overshoots its
3.07% ReEDS share 2.5x while LA comes in 7.6 pp light. County-first's
`alpha * w_c / u` is the per-county analogue and is why the weight-source route
works where the pool route does not (user decision 2026-09-30: do not ship gate 0.0;
a 7.6 pp LA disagreement with nothing to arbitrate it is worse than one method).

`stoch_variant()` is retained in the builder for the section-H diagnostic only and
is never written to the package. Note `substation_annual()` selects one year of the
Approach 2 run as the WEIGHT basis; the CATS-calibrated run covers only the rep-week
year (2019), so `stoch_weight_year()` picks the latest year present. It is a relative
quantity and carries no level into the deliverable.

### The bus-pool restriction (2026-09-30) and its tension with the GenX rule

`pool_src(args)` = `getattr(args, "pool", "all")`, consumed by `candidate_pool(args)`,
which `county_first_shares` now calls in place of `candidate_buses()`:

| value | pool |
|---|---|
| `all` (default) | the 2026-08-12 rule - every Type=Substation non-IMPORT bus plus the AddedNodes CATS loads. **3,769** after the county join. |
| `cats_loaded` | buses CATS itself loads, plus `name_assigned_nodes(args)` - buses the chosen map reaches by direct name match. **2,467**. |

`candidate_pool` raises if the restriction empties any county, so silently losing a
county's ReEDS share is impossible. `cats_loaded_buses()` reads the loaded set from the
GenX control tree (the authoritative current copy), memoized; `candidate_buses()` was
refactored to reuse it with no behaviour change.

Name exception, measured: `prox` and `voltres` have **0** name-assigned nodes, so for
the shipped variants the pool is exactly CATS's loaded set. `nameprox` has 1,242
name-matched buses of which **422** are CATS-unloaded - the only buses the exception
would ever admit.

**This NARROWS the standing 2026-08-12 GenX pool rule**, whose reasoning was the
opposite ("a real substation the model happens to leave unloaded is still somewhere our
methods may legitimately place load").

**DECIDED (user, 2026-09-30): do NOT narrow the GenX pool. `all` stays the GenX
default and the 2026-08-12 rule stands — in the allocation experiment, the latitude
to place load on any real substation IS the treatment.** Restricting it would remove
the very degree of freedom the experiment is measuring. `cats_loaded` stays the
DELIVERABLE default, where the goal is a plausible demand table rather than a
treatment contrast.

The restriction is therefore permanently opt-in: `rescale_genx_demand.py` gets no
`--pool` CLI flag, the default stays `all`, default-path equivalence was verified at
1.1e-16 against the previous build, every `genx__*` run tag keeps its meaning, and no
GenX allocation needs regenerating.

Effect on this package: the restriction dropped **zero** substation-covered buses (all
1,028 were already CATS-loaded), so it removed only unmeasured equal-pool buses - the
measured share of CA-wide load rose 34.5% -> **48.5%**, and the map axis grew from
3.14% to 4.58% because it now governs more load.

### The uncovered-pool split axis (2026-09-30)

`uncovered_src(args)` = `getattr(args, "uncovered", "equal")`, consumed inside
`county_first_shares` at the uncovered-rows branch and in `expand_shares_to_cells`'s
dead-cell fallback:

| value | a county's uncovered pool is split |
|---|---|
| `equal` (default) | flat, `alpha * w_c / u` per bus - the published behaviour |
| `cats` | proportional to each bus's own total MWh in CATS's control demand (`cats_bus_demand()`), falling back to flat for a county whose uncovered buses carry no CATS load (**0 counties here**) |

The pool SIZE (`alpha * w_c`), `alpha`, and every county total are untouched - verified
identical to 1e-15 between the two. Only the split inside the uncovered pool moves, and
because that pool carries 51.5% of CA-wide load this is **the largest axis in the
package: 19.17% of CA-wide load reallocated, Pearson 0.7145**, against 6.04% for the map
axis and 1.12% for the weight source.

Why it matters: CATS does **not** treat uncovered buses as interchangeable - its own MWh
across a county's uncovered buses has median CV **0.79** (p90 1.34, max 1.76). A flat
split discards that. Concretely, Sacramento's 231 uncovered buses go from a flat
~53.7 GWh each to 9.8-481.4 GWh (49x spread); Imperial's 28 from a flat 169.7 GWh to
27.2-594.6 GWh (22x). Precedent: `hybrid_county_topup.py` already documents exactly this
as its `proportional` method, and the standing decision that CATS is the true allocation
sanctions it.

`cats_bus_demand()` and `cats_loaded_buses()` now share one memoized pass over the
control tree (`_scan_control_demand`); the loaded-set semantics are unchanged (positive
in ANY season).

**Interpretation caveat that must stay in the package README:** under `cats` roughly
half the allocation is CATS's own pattern, so a comparison of this package against CATS
is **not independent** for that half. `countyfirst_envelope_equalsplit__prox` is shipped
precisely so that comparison can be made cleanly.

### Two run-only toggles: `--overlays` and `--bus-types` (2026-10-02)

Both default to the full-package behaviour, so nothing above changes. Both exist
because a specific consumer asked for a specific series; **neither belongs in the
`load-profile-request` skill's defaults** (user, 2026-10-02 — the skill keeps
overlays-on as the only correct way to read RESOLVE).

| Flag | Default | Other value | Effect |
|---|---|---|---|
| `--overlays` | `all` | `none` | `none` builds from RESOLVE's **Baseline component alone**: CA-wide 2035 gross 327.35 TWh instead of 445.76, dropping all EV / building-electrification / data-centre growth and the offsetting efficiency. **Not RESOLVE's forecast**; the builder prints a `!!` warning. |
| `--bus-types` | `all` | `substation` | `substation` unloads **every** `AddedNode`, including the 607 CATS itself loads. Pool 2,467 → 1,860. Implemented as `bus_types_src(args)` in `candidate_pool`, composing with `pool_src`. |

`--bus-types substation` leaves **county totals unchanged** (0.000069 pp max deviation
from the ReEDS shares) because the excluded AddedNodes' load is re-split *within their
own county*. All 57 counties survive the exclusion; `candidate_pool` raises if any
county were emptied. Side effect worth knowing: the 607 excluded buses were all in the
uncovered pool, so the measured share of load rises 48.5% → **62.0%**. Bus counts fall
— LA 413 → 335, Sacramento 231 → 190, Imperial 28 → 22, Stanislaus 63 → 39.

`--readme` was added alongside, so a non-default run ships an accurate README instead of
the default template's numbers. Non-default runs must use it.

### Sibling package: `resolve2035_test_base327_subonly_wy2012`

Built 2026-10-02 to exercise an algorithm against the pre-correction 327 TWh series, NOT
as a forecast. Baseline-only, substation buses only, weather year 2012.

| | Test set | Full package |
|---|---|---|
| Gross / net | **327.35 / 284.41 TWh** | 445.76 / 404.13 TWh |
| CA-wide peak | **64,326 MW** | 76,173 MW |
| Weather year | **2012** | 2005 |
| Buses | **1,859** (`Substation` only) | 2,466 |
| Measured pool | **62.0%** | 48.5% |
| Zip | 138.7 MB | 182.4 MB |

README template: `scripts/load_projection/deliverables/README_resolve2035_test_base327_subonly.md`,
which leads with the warning that 118 TWh is missing by design. `resolve_2035_components.csv`
ships empty — the signature of `--overlays none`, and a quick way to tell the builds apart.
Verified: 8,760 h, zero AddedNode columns, no bus loaded that CATS leaves unloaded,
conservation 0.0501 MW, no negative bus-hours, county shares matching ReEDS.

Rebuild:

```bash
python scripts/load_projection/deliverables/build_resolve2035_cats_package.py \
    --overlays none --bus-types substation --weather-year 2012 \
    --out deliverables/resolve2035_test_base327_subonly_wy2012 \
    --readme scripts/load_projection/deliverables/README_resolve2035_test_base327_subonly.md
```

`--bus-types all` with the rest unchanged gives the every-loaded-bus variant for
research use.

### ReEDS county-year

`--county-year` defaults to **2023**, the latest in `reeds_county_annual()` (observed
years 2016–2023 only — there is no 2035 county projection, so the allocation assumes
county shares hold). Sensitivity on normalized shares: 2019 vs 2023 moves 0.73% of
state load, 2016 moves 0.95%.

### Rebuild

```bash
python scripts/load_projection/deliverables/build_resolve2035_cats_package.py
python scripts/load_projection/deliverables/deliverable_numbers.py   # verify figures
```

`--weather-year`, `--county-year`, `--uncovered`, `--pool`, `--maps`, `--draws`,
`--stochastic-run`, `--decimals`, `--out`, `--no-zip`; the builder's docstring is
the parameter
reference. The allocation is deterministic, so the package is exactly reproducible.
`deliverable_numbers.py` is copied into the package's `code/` folder so the recompute
path travels with the zip.

### Superseded — the IOU-only first cut (2026-09-29)

The first build disaggregated only PGE/SCE/SDGE to their 1,347 metered substations
(Approach 1 month-hour weights and Approach 2 stochastic shares, each renormalized per
(IOU, hour)) and mapped substations to their nearest bus. Two defects, both measured,
both fixed by county-first:

- it discarded the 20.2% of CA-wide net load in IID/LDWP/NCNC;
- nearest-node mapping is many-to-one, so only 1,043 of the 2,471 buses CATS loads
  received anything; the 1,428 zeroed buses held 45.6% of CATS's own demand, and the
  610 loaded `AddedNode` buses were outside the nodal pipeline's narrower pool
  entirely.

Retained facts from that cut worth not relearning: renormalizing Approach 2 output to
shares cancels `F*` and `s(c)` exactly, leaving `rho(c)` as the only surviving
calibrated parameter — which is the only way Approach 2's envelope-driven,
target-independent level (`sum mu_s` ~ 164 TWh under `--F cal`) can be used against a
forecast at all. A scalar `--F` cannot conserve, because the residual `s(c)` is
per-cell (0.851–1.297 on the 2035 net series, so −15% to +32% by cell).

---

## `emilia2` — hourly load at sub-nodes, coordinate-free (2026-10-08)

Builder `scripts/load_projection/deliverables/build_emilia2_package.py`; README
template `README_emilia2.md` beside it; output `deliverables/emilia2/` (+ `.zip`).
**The first deliverable built on `Approach 3`**, and the case that forced the
deliverable-rule amendment: the target is an arbitrary bus list with no
coordinate-based nodal map, so `county_first_shares` is unreachable and there is
no existing allocation to import. Per the amended rule the deliverable **cites**
Approach 3 rather than inventing anything — all allocation logic is imported from
`src/load_projection/proportional.py`.

### Input shape

One row per (bus-list bus, sub-node), as in `data/example.csv`:
`bus_id, bus_label, name, lat, long, source, utility, base_kv,
bus_number_x, summer_load, 'ID', bus_number_y, winter_load, Count`.

`ingest_node_table.py` maps this onto Approach 3's contract: `base_id` is the
bus-list bus, `subname` is the `'ID'` column, `node_id` is `{bus}|{ID}`. The
sub-node axis therefore becomes Approach 3's sibling axis and all of its sibling
machinery applies unchanged.

### Which input columns are actually needed (`--audit`, measured)

| class | columns |
|---|---|
| REQUIRED | `bus_id`, `'ID'`, `summer_load`, `winter_load`, and ONE join key |
| JOIN KEY | `lat`+`long` **or** `name` (+`utility`). **Not equivalent** — see below |
| OPTIONAL | `utility` — needed only for a NAME join on a shared name |
| IGNORED | `base_kv` — Approach 3 has no voltage axis |
| REDUNDANT | `bus_number_x`, `bus_number_y` (== the bus), `bus_label` (== `name`), `Count` (== rows per bus), `source` (constant) |

Verified by dropping each and re-running: coords-only works, name+`utility`
works, and **name without `utility` or coordinates is REFUSED** because the
example name is shared across utilities.

### `# VERIFIED` — the bus NAME is not a reliable join key

On `data/example.csv` the row is named `LIVE OAK`, but its coordinates sit **4 m**
from PG&E's profiled `EL CERRITO G`, while the nearest substation actually called
Live Oak is **161 km** away in Sutter County (four Live Oaks exist statewide: pge
Kern, pge Sutter, sce LA, smud Sacramento; none near these coordinates). The two
keys resolve to different substations with different load:

| candidate | reached by | mean `avg_load` | peak `max_load` |
|---|---|---|---|
| `pge / EL CERRITO G` | coordinates, 4 m | 34.89 MW | 53.93 MW |
| `pge / LIVE OAK` | name | 5.14 MW | 15.15 MW |

So `bus_label`/`name` is the bus **label** and need not equal a utility
substation name. **`--match coords` is the default**, the name is kept as a
cross-check, and any bus whose two keys disagree by more than
`--max-name-dist-km` (25 km) is reported in `reference/column_audit.txt` rather
than silently resolved. Do not switch the default to `name` without re-running
`--audit` on the real file.

A second, subtler finding: ambiguity must be decided on `norm(name)`, not the
verbatim string. Our fleet carries pge `"LIVE OAK"` and sce `"Live Oak"`, which
differ only in CASE, so a verbatim index treats them as two names and would
resolve on that coincidence alone. `ingest_bus_nodes.match_by_name` uses the
project's single `norm` definition.

### `load_station` is the join key (2026-10-08)

A substation NAME is not a key in California: `Mission` is three distinct
stations, `Potrero` three, `Newhall` and `Antelope` two each, and by bare name
the nearest same-name candidates sit 6-1,035 km apart (`ANTELOPE` 188 km,
`BELMONT` 501 km, `LINCOLN` 143 km). Matching on name both misses real matches
and produces confident-sounding nonsense.

The input therefore carries **`load_station`** -- the `substation_name` to join
`substation_load_profiles_clean.csv` on, together with `utility`. It is produced
upstream by coordinate IDENTITY (an exact key match, not a nearest-neighbour
search) or by a name lookup confined to the single table where the name IS the
key; it never matches names across tables. A BLANK value is a definite "no load
profile for this station", not an unattempted lookup, because the profiles table
holds exactly the stations of the attributes table.

Match order in `ingest_node_table.py`:

0. `load_station` + `utility` -- supersedes everything where present
1. `station_name` -- exact/normalized fleet match, then the inverted
   `basinSourceDictionary`. The fallback for blank `load_station`; it recovers
   e.g. `Balch 1` -> `pge/BALCH NO 1`, a known 1.3 km name/coordinate ambiguity
2. coordinates -- available but NOT recommended

Where both routes answer a bus they are compared and any disagreement printed;
there are currently **zero**, so one appearing later is a regression signal.
Measured on the `emilia2` run: mapping 732 -> 741 rows, 0 changed assignments,
`Potrero` now resolving to `pge/SF A (POTRERO)` and `Mission` to
`pge/SF X (MISSION)`.

**Do NOT promote `--match coords` as the fix.** It takes the nearest profiled
substation within 5 km, and in dense areas that is routinely the wrong station:
Moss Landing -> `DOLAN ROAD` at 898 m, Larkin -> `SF X (MISSION)` at 721 m,
Alamitos -> `Stadium` at 794 m, Antelope -> `Lunar` at 332 m -- all different
stations. Proximity with no name or identity agreement is not evidence at this
scale.

**Correction (2026-10-08):** an earlier note reported four buses as "0.0 km from
<station>, label looks wrong, recoverable". Those distances came from a
synthetic fixture whose coordinates were set equal to the candidate's, and were
wrongly presented as describing the real input. The real distances are 319.5,
511.8, 188.2 and 733.7 km -- all genuinely different stations, correctly left
unmapped. The haversine step itself is sound (independently re-verified).

### The input contract is source-agnostic

Nothing in the reader is specific to any source system. Canonical column names
are `bus_id`, `sub_id`, `summer_load`, `winter_load`, plus `load_station` /
`station_name` / `utility` / `lat` / `lon` / `base_kv` / `source` / `bus_label`.
`COLUMN_ALIASES` accepts the common export spellings (`'ID'`, `name`, `long`,
`bus_number`, `kv`, ...) so most exports read unchanged.

### Curating on `name`, and the usable-envelope rule (2026-10-08)

The real bus list differs from `data/example.csv` in a way that changes the
default. There, `bus_label == name` for every row. In the real file they are
**different columns with different jobs**:

| column | role |
|---|---|
| `bus_label` | the bus's own label, ALWAYS present (`GATE 42A`). Carried as provenance; **never a join key**, because a bus label need not be a substation name |
| `name` | the CURATED match to a utility substation, **blank when the bus was not matched** (`Mesquite`, or blank) |

So **`--match name` is the default** (user, 2026-10-08). `--match coords` would
override the caller's own decision: a row with blank `name` but populated
coordinates would be mapped on proximity anyway, which is exactly the judgement
the blank was expressing. Both the substation match AND the usable-envelope
check key on `name` only.

**A match is only useful if the substation has a USABLE LOAD ENVELOPE** (user,
2026-10-08). Being in `substation_load_profiles_clean.csv` is not enough. If the
envelope is degenerate, Approach 3's shape guard (G8) replaces it with a flat
1.0 — so the bus would be counted as "mapped" while carrying the statewide shape
anyway, making the coverage figure a lie. `usable_envelopes()` therefore
restricts the match index to substations that are non-degenerate, and
`unusable_envelopes()` exists purely so a dropped match can be named correctly
("in the fleet but no usable envelope") rather than as "not in the fleet".

Measured on the current fleet: **1,308 of 1,347 substations are usable.** The 39
that are not break down as 6 with no data at all (all 288 cells NaN, e.g.
`sce/Autobody`), plus substations whose envelope nets to zero or below
(`sce/Alola` at exactly 0.000, `pge/HENRIETTA` at −5,975 MW of net reverse flow,
`pge/CABRILLO` at −426 MW) or whose net-to-gross falls under 0.20
(`pge/PIT NO 5` at 0.024, `pge/SALT SPRINGS` at 0.021).

**Buses outside PG&E / SCE / SDG&E can never match**, because those are the only
utilities we hold envelopes for. A row with `utility = iid` (or `ladwp`, `smud`)
is reported with that reason explicitly and carries the statewide shape. This is
not a defect: 100% of its load is still allocated.

Every unmapped bus is therefore attributed to one of four distinguishable
reasons, counted in the ingest's COVERAGE block:

1. `no name supplied` — the mapping does not cover this bus (the normal case)
2. `utility '<x>' is outside ['pge','sce','sdge']` — no envelopes exist for it
3. `matched [...] but that substation has NO USABLE LOAD ENVELOPE`
4. `name not in the profiled fleet` — a genuine match failure worth checking

### Basin-sourced names go through `basinSourceDictionary` (2026-10-08)

A bus list that sources some rows from the basin dataset carries the **basin
spelling** in `name`, and **86 of the 90 dictionary rows have
`BasinName != SourceName`** -- `Artesian` vs `Artesian Ranch`, `Capistrano` vs
`San Juan Capistrano`, `Balch 1` vs `BALCH NO 1`, `Chollas` vs `Chollas West`.
A direct fleet match fails on every one of those even though the substation is
right there.

So `ingest_bus_nodes.match_by_name` has a rule 2, exactly as
`external_loads.resolve_names` does: exact/normalized fleet match first, then
the inverted `basinSourceDictionary`. It reuses the production
`invert_basin_dictionary`, so the blank-`BasinName` VETO and the many-to-one
refusal behave identically. The index is built from the USABLE-envelope fleet
only, so the dictionary route cannot smuggle in a substation with no shape to
lend. A row resolved this way is reported as `ok; matched via
basinSourceDictionary`.

**The coordinate is irrelevant to this.** Under the default `--match name` the
match keys on `name` alone, so it does not matter whether a row's `lat`/`long`
came from the utility source or the basin source -- they are carried as
provenance and used only for the disagreement report.

**Trap, and it bit once:** a resolved row may carry `ok; <note>`, so edge
selection must gate on `status.startswith("ok")`, NOT `== "ok"`. Gating on
equality silently dropped every basin-matched bus from the edge list (measured:
2 edges instead of 7 on the basin fixture) while still *reporting* them as
resolved. `external_loads.py` documents the same trap as "gate on ROUTE, not
status".

### THE NODE UNIVERSE AND THE MAPPING ARE TWO DIFFERENT INPUTS

The easiest way to get a badly wrong answer, so it is called out in the ingest's
module docstring, the builder's `--input` help and the package README.

* The **node universe** (`--input`) is every node load is allocated to. It is
  the allocation denominator: a node absent from it receives NOTHING.
* The **mapping** is the subset of buses that can borrow a measured hourly shape
  from a profiled utility substation. It is normally far smaller, and that is
  fine -- a mapping covering 1 of 2,000 buses gives that bus its substation's
  measured pattern while the other 1,999 carry the STATEWIDE pattern, each
  scaled by its own level. **100% of load is allocated either way; what the
  mapping changes is only the shape.**

If the mapping file is passed as `--input`, the other 1,999 buses are simply not
in the universe and all of California's load lands on the one bus. Measured on a
2,000-bus fixture: that mistake yields `1 bus / 6 sub-nodes` instead of
`2,000 / 4,004`.

Two supported ways to express the split, giving identical levels and edges
(verified on the fixture):

1. **One file** -- the universe with `name` and `lat`/`long` left BLANK on rows
   the mapping does not cover. A blank key means "deliberately unmapped" and is
   reported as `no name supplied` / `no coordinates supplied`, never as a failed
   match. (A blank read back from CSV is NaN and `str(NaN)` is `"nan"`, so the
   ingest normalizes blanks explicitly rather than letting `"nan"` be matched as
   a substation name.)
2. **Two files** -- `--input` the universe (levels alone suffice: just
   `bus_id`, `'ID'`, `summer_load`, `winter_load`) plus
   `--mapping-input` the smaller match file, joined by bus number. A bus in the
   mapping absent from the universe is **refused**, since that is the signature
   of the mistake above.

Guards: `--expect-buses N` / `--expect-nodes N` refuse unless the universe has
exactly that size, and the ingest always prints a COVERAGE block giving buses
mapped, sub-nodes mapped, and the share of level mass whose shape is
measurement-driven versus statewide.

Measured on the 2,000-bus / 1-mapped fixture: `Lambda_M` = 0.000583,
`Lambda_U` = 0.999417, `A(b)` = **1.0002**, and an unmapped node's shape is
1.0 +- 0.0002 -- i.e. flat, which IS the statewide profile. So a sparse mapping
is the BEST-conditioned regime for the slack path, not the worst. Energy exact
to 4.9e-17 pp and conservation 1.46e-11 MW across all 40 combinations.

### The grid and the compact format (2026-10-08)

Default package: model years **2026, 2030, 2035, 2040, 2045** crossed with
weather years **2007-2014** = **40 datasets**. Built by
`approach3/build_proportional_grid.py`.

Speed comes from doing each step at the right level: the reads and the envelope
surface once overall, `build_target` once per MODEL year (5x, not 40x), and only
a 288 x n_nodes normalize + share step per combination. No hourly file is written
at build time. Measured: 40 combinations x 4,004 sub-nodes in **75 s**.

**It ships `env`, NOT the share matrix `S`.** `# VERIFIED` 2026-10-08:
Approach 3's shares are NOT invariant to the model year or weather year, because
the shape normalization is target-energy weighted -- shape moves **8.2e-04**
between weather years and **1.4e-03** between model years. **The CATS compact
format's "share matrices do not depend on the model year or the weather year at
all" claim must NOT be carried over.** What IS target-invariant is `env` and the
level shares `l`, so the package ships those once plus one statewide series per
combination, and `expand.py` recomputes the normalization by importing the
SHIPPED `code/load_projection/` (a verbatim copy of the building repo's
modules). Expansion is therefore exact by construction, verified byte-for-byte
against a direct hourly write.

The statewide series is written at `%.17g`, not `%.4f`: largest-remainder
apportionment breaks ties on the discarded fractions, so a y(t) differing in the
15th decimal hands a 0.1 MW unit to a different node. Both results conserve
exactly, but only an exact round-trip is byte-identical. `test_approach3.py` T16
guards the expander's copy of `round_to_printed` against drift, the same way
`test_compact.py` guards the CATS one.

Measured sizes at 4,004 sub-nodes: compact package **10.85 MB**; one expanded
hourly file 45.6 MB, so all 40 would be ~1.8 GB (~170x).

### Degeneracy of the one-bus example — read before interpreting it

`data/example.csv` has **one** bus with 6 sub-nodes, all mapped. Then:

- `Λ_U = 0`, so `--conserve slack` correctly refuses (no slack basis).
  `--conserve auto` falls back to `renorm` and says why.
- With a single mapped base and one shared shape, `D(c) = shape(c)` and
  `S = l_n·shape/shape = l_n` **exactly**: the shape cancels completely and the
  output is the statewide series × each sub-node's level share. Measured
  within-block share CV **0.0** for every sub-node. Energy share is exact anyway
  (5.6e-15 pp), as a by-product.

So the example package demonstrates the plumbing, the conservation guarantee and
the negative-level handling, but **not** the shape layer.

A synthetic 4-bus check (3 mapped
to different substations + 1 unmapped) confirms the shape layer does work on this
input format: `--conserve auto` chose `slack`, seasonal energy stayed exact
(1.1e-16), `Λ_M/Λ_U` = 0.416/0.584 (NovApr), `A(b)` = 1.190, and the three mapped
buses' per-cell share vectors correlate **0.40, 0.54 and −0.17** with each other
— a negative pair is only possible if each bus carries its own measured pattern.

### Negative sub-nodes

The example's `'EE'` sub-node is negative (summer −7.74, winter −1.85). Under the
default `net-base` the bus is allocated its signed **net** (winter 96.40, not the
positive-only 98.25) and that net is split among the bus's **positive** siblings,
so `EE` is written literal `0.0` with its level preserved in `node_index.csv`.
Measured: `z0006` is zero at all 8,760 hours.

### Measured package (RESOLVE 2035, weather year 2012, net of BTM)

| quantity | value |
|---|---|
| target | **402.83 TWh**, peak **76,624 MW** |
| buses / sub-nodes | 1 / 6 (1 mapped, 0 unmapped) |
| max hourly conservation error, printed grid | **1.455e-11 MW** |
| target grid-snap offset (not an error) | 0.04999 MW |
| max seasonal energy share deviation | 5.6e-15 pp |
| max per-node printed-vs-float annual | 3.62 MWh |
| package / zip | 0.52 MB / **0.32 MB** |

### Rebuild

```bash
python scripts/load_projection/deliverables/build_emilia2_package.py     --input data/example.csv --year 2035 --weather-year 2012
```

`--conserve auto` (default) picks `slack` when some buses are unmapped and
`renorm` when all are, printing which and why. Other axes: `--match`,
`--negative-nodes`, `--shape-sources`, `--sibling-draws`, `--target-csv`.
**The README is a template under `scripts/`** — the builder deletes and rewrites
the package directory every run, so an edit inside `deliverables/emilia2/` is
discarded.

## External seasonal load weight source (2026-10-01)

A third value of the `county_weights` axis, beside `envelope` and `stoch`:
`external`, paired with `args.external_loads` naming an artifact folder under
`data/processed/load_projection/external_loads/`. Built by
`scripts/load_projection/external_loads/build_external_weights.py` from a file of
one summer and one winter load per substation. Full spec: `docs/external_loads.md`.

Reached from the builder with `--external-loads <tag>[,<tag>...]`; each folder adds
one county-first variant, tagged `countyfirst_<tag>_<uncovered>__<map>`.

**Why it is not a new `Approach N`.** The allocation is unchanged — ReEDS county
totals, α = u/n, the equal pool and the renormalisation are all weight-source
agnostic. Only the within-county split moves. This follows the rule that a
deliverable never introduces a new approach.

**What it changes that the other two sources cannot.** `envelope` and `stoch` both
weight the same 1,347 utility-profiled substations, so they re-split the same
measured pool. `external` can give a load to reference substations the utilities do
not profile, so it moves buses OUT of the uncovered pool and raises
`envelope_governed_share`.

Measured on a **synthetic in-repo fixture** (map `prox`, `pool=cats_loaded`,
`uncovered=cats`, `alpha ratio`) — recompute with
`deliverable_numbers.py --sections I` before quoting against a real input:

| Weight source | covered buses | measurement-driven | uncovered |
|---|---|---|---|
| `envelope` (published default) | 1,028 | 48.53% | 51.47% |
| `external`, utility names only | 1,018 | 47.66% | 52.34% |
| `external`, utility + reference names | 1,741 | **73.40%** | 26.60% |

Sacramento moves from 2 covered buses (α 0.9913) to 177 (α 0.2338) and Los Angeles
from 181 to 302 (α 0.5617 → 0.2688) — the two counties this package's acceptance
tests use as the canary.

**Caveats to carry into any package README.**

- 100% of load is still allocated; the rising figure is the measurement-driven
  *within-county split*, not the allocated fraction.
- Reference substations with no utility profile are placed **spatially**, so those
  rows do not honour the `--map` axis.
- Under `--shape utility` those same rows carry a **borrowed county-mean** diurnal
  shape — a pattern nobody measured at that location. The count is in the artifact
  manifest.
- `--shape flat` is the honest baseline for a seasonal-only input. Driving the
  stochastic weight source from such an input is a null result: per-cell weights
  correlate 0.999915 with the input levels.

---

## Period grammar and the compact format (2026-10-05)

Two additions to `build_resolve2035_cats_package.py`. Both default to the previous
behaviour: a bare `--year 2035` run reproduces every one of the six shipped hourly
files **bit-for-bit** (verified by md5 against `resolve_2035_cats_nodal.zip`).

### `--period` / `--weather-years`

`--year` and `--weather-year` are kept as aliases. The grammar lives in
`src/load_projection/periods.py` (pure, guarded by
`scripts/load_projection/deliverables/test_periods.py`):

| `--period` | meaning |
|---|---|
| `2035` | one snapshot |
| `2026,2030,2035` | three independent snapshots |
| `2030-2035` | ONE continuous 52,560-hour series |
| `2030-01-01:2031-06-30` | ONE continuous 1.5-year series |

Weather years differ by mode, which is why a list and a range mean different things:

- **snapshots** take the **cross product** — every model year x every weather year;
- **continuous** pairs **one weather year per calendar year**, in order, and a single
  value is repeated.

A continuous period over a fractional number of years needs one weather year per
calendar year it *touches*, and the last is truncated at the period end:
`2030-01-01:2031-06-30` takes two, the second contributing 4,344 of 8,760 hours.
A sub-year window is one calendar year, so exactly one weather year, and is expressed
internally as a range of `hour_of_year` -- RESOLVE is an 8,760-hour year, so a
requested Feb 29 is refused rather than silently shifting the window.

**Model years are validated against the source, 2024-2045**, read from the Baseline's
own `annual_energy_forecast` via `available_model_years()` rather than hardcoded. The
overlays reach 2050 but the Baseline -- the only weather-varying component -- does
not, so a later year could only be projected. `--period 2050` fails naming the range.
Weather years are validated against 2000-2022 the same way.

With neither `--period` nor any weather-year flag, the median-peak default still
applies to the single `--year`; a multi-year period requires `--weather-years`
explicitly, because "the median" is not defined across model years.

### `--format compact`

`hourly` (default) is unchanged. `compact` exploits the fact that **the share
matrices do not depend on the model year or the weather year** -- only on
`county_year`, `pool`, `bus_types`, `uncovered`, `map` and `county_weights`:

| artifact | contents |
|---|---|
| `shares/<variant>.npz` | 288 cells x N buses, float64, lossless |
| `statewide/<series>.csv.gz` | `datetime_pst, hour_of_year, cell, y_mw` |
| `code/expand.py` | reconstructs any hourly file; numpy + pandas only |

Measured: a 4-combination grid is **1.51 MB** (1.03 MB of shares + 0.48 MB of series)
against ~0.1 GB as hourly CSVs; a 40-combination, 4-variant grid lands near 15 MB
against ~4.0 GB. The precedent's own hourly files are 25.1 MB (`equalsplit`) to
31.4 MB (`catsprop`) each.

`code/expand.py` carries its own copy of `round_to_printed` because the package must
work without this repo. **That copy is checked against the original on every test
run** (`test_compact.py`), and an expanded file is **byte-for-byte identical** to one
written directly -- verified on a real combination (md5
`3550bb9da753800ef22168845e251a67` both ways) and on synthetic matrices in the guard.

Per-bus annual energy and peak are reported without expanding anything, because the
share vector is constant within a cell:

    annual_i = S.T @ cell_sum        peak_i = max_c cell_max[c] * S[c, i]

Both verified against a full expansion. They are **pre-rounding**, so they differ
from the expanded files by at most half a print unit per hour; the expanded file is
the authority.

### Two other changes

- **`--weights {envelope,stoch,external}`** selects which weight families ship.
  Default `envelope,stoch` -- the published set. A package need not contain
  `envelope`, so `node_shares_static.csv` now keys off the first variant actually
  built rather than a hardcoded envelope tag.
- **`--no-uncovered-comparison`** suppresses the alternate-uncovered comparison file
  for one run. Per-run only: `--uncovered` keeps its default and the comparison stays
  on everywhere else.

### Weather-year spread, measured

Worth stating in any package README, because it is easy to over-read a grid of eight
weather years. RESOLVE's overlays have no weather dimension, so only the Baseline
(~73% of load) varies. Measured on a 2 x 2 grid, substation buses, stoch weight,
equal split:

| model year | peak | energy |
|---|---|---|
| 2035 | 76,624-80,463 MW (**+5.0%**) | 401.83-402.83 TWh (+0.25%) |
| 2040 | 82,929-87,828 MW (**+5.9%**) | 443.93-444.98 TWh (+0.23%) |

Peak moves ~5-6% across weather years; **annual energy barely moves at all**
(~0.25%). A weather-year grid is therefore a peak/reliability instrument, not an
energy one. `deliverable_numbers.py --sections J` prints this for any compact grid.

### Rebuild

```bash
python scripts/load_projection/deliverables/build_resolve2035_cats_package.py \
    --period 2026,2030,2035,2040,2045 \
    --weather-years 2007,2008,2009,2010,2011,2012,2013,2014 \
    --format compact --weights stoch,external --maps prox --draws "" \
    --uncovered equal --no-uncovered-comparison --bus-types substation \
    --external-loads <tag>,<tag> \
    --readme scripts/load_projection/deliverables/README_resolve_grid_compact.md \
    --out deliverables/<name>
python scripts/load_projection/deliverables/deliverable_numbers.py \
    --package deliverables/<name> --sections I,J
```
