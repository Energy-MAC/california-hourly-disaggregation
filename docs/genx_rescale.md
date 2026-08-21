# GenX demand rescaling — spatial redistribution under fixed statewide load

Full detail. The README carries the summary; this document holds the control
description, the three allocation families, the substation→bus map axis, the
share-vector construction, the conservation guarantee, and the caveats that
matter for interpreting a downstream GenX comparison.

**Every measured number quoted here is recomputable**: run
`python scripts/load_projection/genx/doc_numbers.py`, whose sections are
labeled with the doc passages they back. Refresh a number there before editing
it here. `doc_numbers.py` *imports* the production functions
(`candidate_buses`, `load_profiles`, `build_system_cells`, `read_demand`)
rather than reimplementing them, so its numbers cannot drift from what the
pipeline actually does.

### Where this is implemented

Everything below lives in `scripts/load_projection/genx/rescale_genx_demand.py`
unless noted. Search these names to check any claim in this document.

| Concept in this doc | Function |
|---|---|
| Candidate bus pool (3,778) | `candidate_buses()` |
| Which nodal map a run uses | `map_path_for()` |
| County-first allocation, the α split | `county_first_shares()` |
| County-first per-cell expansion | `expand_shares_to_cells()` |
| Envelope weights (static / per-cell) | `envelope_node_weights()` / `envelope_cell_weights()` |
| Stochastic pool + gate + β | `stoch_pool_shares()` |
| Stochastic per-cell weights and expansion | `stoch_cell_weights()` / `expand_stoch_shares_to_cells()` |
| Envelope hold | `env_hold_shares()` / `expand_env_shares_to_cells()` |
| Full vs hold redistribution | `redistribution_mode()` |
| The actual hourly rewrite + conservation asserts | `rescale_season()` |
| Largest-remainder rounding | `round_to_printed()` — `genx_demand_io.py` |
| Byte-safe GenX CSV read/write | `read_demand()` / `write_demand()` — `genx_demand_io.py` |
| Rep-week calendar → (month, hour_pst) | `load_rep_week_calendar()` — `genx_demand_io.py` |
| Season detection / md5 verification | `scenario_seasons()` — `genx_demand_io.py` |
| ReEDS **county weights** (the only ReEDS input) | `reeds_county_annual()` — `checks/validate_county_reeds.py` |
| Bus → county point-in-polygon | `nodes_by_county()` — `nodal/hybrid_county_topup.py` |
| Guards (conservation, partition, by-cell) | `test_genx_rescale.py` |

## Purpose

The `genx/` scenario tree is a set of GenX capacity-expansion cases whose demand
inputs come from an external allocation of California load across CATS buses.
Those cases are treated here as **controls**. This stage produces alternative
demand inputs in which the *spatial* distribution of load across buses follows
this project's disaggregation and nodal-assignment methods, while the statewide
load in every hour is held **exactly** at the control's value.

Fixing the hourly statewide total is what makes the downstream comparison
interpretable: any difference in GenX's dispatch, capacity, prices, or
congestion is attributable to *where* load sits on the network, not to how much
of it there is. Load magnitude and load location are separated by construction.

## The control set

| Property | Value |
|---|---|
| Cases | 28 (`p1`–`p28`), indexed by `genx/scenarios_2019.csv` |
| Grid | 7 renewable weather years (2007–2013) × 4 seasons |
| Demand file | `p{N}/system/Demand_data.csv` |
| Timesteps | 168 (one representative week; `Rep_Periods=1`, `Sub_Weights=168`) |
| Zones | 8,870 `Demand_MW_z{i}` columns, zone `z{i}` ≡ CATS `bus_i` |
| Nonzero zones | 2,471 |
| Distinct demand files | **4** — one per season, shared across the 7 weather years |
| Units / precision | MW, written to one decimal place |

Because demand varies only with season, a rescale run computes **4 files, not
28**. `materialize_genx_cases.py` expands them into runnable case folders.

The zone-to-bus identity is what makes this possible at all: `Demand_MW_z{i}`
indexes the same CATS `bus_i` that `map_loads_to_nodes.py` assigns substations
to, so a nodal share vector drops straight into the demand file.

## Three families of allocation

Every method answers the same question — *what share $s_i$ of statewide load
does bus $i$ carry?* — but they build that share from different ends.

**County-first (`--weights reedsco`).** Start from ReEDS county *weights*, hand
each county exactly its share of the statewide total, then decide how that
county's energy splits across its own buses. County totals are correct *by
construction*, so no shortfall can arise and no top-off exists.

**Stochastic pool-and-redistribute (`--weights stoch`).** Sweep a pool of load
off the network and let Approach 2's per-cell stochastic output deal it back
out. County totals **emerge** rather than being imposed — the substantive
contrast with the county-first family, and the reason this counts as the
"complicated" input. **Always per-cell** (`--level monthhour` is mandatory;
the rescaler refuses static): every Approach 2 parameter (μ, σ, ρ, s) is
estimated per (month, hour_pst) cell, and a static stochastic run collapses
the model to a rescaled envelope midpoint — a null result by construction
(at annual level the stochastic mean correlates 0.9998 with the envelope
midpoint; the model's content lives entirely in the per-cell shape).

**Envelope hold (`--weights env`).** The minimum-intervention contrast:
re-split only the load the control already places on covered buses, among
those same buses, weighted by the substations' own measured `max_load`
envelopes. Uses no ReEDS input and no projection model. (This replaced the
former Approach 1 substation-first hold on 2026-08-13: that variant carried
ReEDS load *levels* through Approach 1's disaggregated MWh, violating the
standing rule that only normalized ReEDS county *weights* may enter.)

An earlier substation-first family with gated county top-off modes
(`topeq`/`topprop`) sized shortfalls against ReEDS load levels and was
**deleted outright** for the same reason.

Run tag: **`genx__{weights}__{map}__{alloc}__{level}`**

| Axis | Values | Meaning |
|---|---|---|
| `weights` | `reedsco` | **county-first**: ReEDS county weights + within-county envelope split |
| | `stoch` | pool-and-redistribute by Approach 2 per-cell output |
| | `env` | envelope-weighted hold (minimum intervention) |
| | `control` | no-op passthrough; reproduces the control byte-for-byte |
| `map` | `prox` | nearest node (`substation_node_map.csv`) |
| | `voltres` | voltage-restricted nearest node |
| | `nameprox` | CEC-lineage identity match first, proximity for the rest |
| | `catch` | transportation-LP catchments — every candidate bus assigned and loaded |
| | `namecatch` | identity matches forced into the LP, catchments for the rest |
| `alloc` | `aratio`, `a0`, `a<x>` | county-first: the α split (see below) |
| | `w1g30top-…`, `w1g30-…`, `w2-…` | stochastic: gate/top-off design + draw |
| | `hold` | envelope hold |
| `level` | `static` | one share per bus, fixed across the week (forbidden for `stoch`) |
| | `monthhour` | shares vary by (month, hour_pst) cell — envelope cells for `reedsco`/`env`, Approach 2 per-cell output for `stoch` |

## County-first allocation (`--weights reedsco`)

Each county $c$ receives $E_c = w_c \times$ (statewide total), where

$$w_c = \frac{R_c}{\sum_{j} R_j}$$

and $R_c$ is the ReEDS county annual load. **Only the normalized weight survives
— the ReEDS load *level* cancels in the ratio.** The statewide total is CATS's
own, hour by hour, so ReEDS never sets a magnitude anywhere in the allocation;
it supplies one number per county, the county's *share*. (The deleted
`topeq`/`topprop` top-off path compared ReEDS MWh levels against method MWh to
size a shortfall — precisely why it was removed rather than kept as an option.)

Inside the county, with $u$ **uncovered** buses (no substation maps to them) and
$s$ **substation buses**:

$$
\text{uncovered bus} \;\to\; \frac{\alpha \, E_c}{u}
\qquad\qquad
\text{substation bus } i \;\to\; (1-\alpha)\, E_c \frac{w_i}{\sum_{j \in c} w_j}
$$

$\alpha$ is the share of the county's energy handed to the **uncovered** pool as
an equal split; the remainder goes to substation buses in proportion to their
**max-load envelope** ($w_i$ = the substation's mean `max_load`, carried onto
buses through the map's tie shares). Defining α against the *uncovered* pool
rather than against all buses is what makes both named methods exact special
cases:

| `--alpha` | Behaviour | Buses loaded |
|---|---|---|
| `ratio` | $\alpha = u/n$ per county. Uncovered buses collectively receive $\tfrac{u}{n}E_c$ — precisely the equal share an even split over all $n$ buses would give them — while the substation buses' $\tfrac{s}{n}E_c$ is re-apportioned among them by envelope weight. **Every bus is loaded.** | 3,768 |
| `0` | The county's entire energy goes to its substation buses by weight; uncovered buses get nothing. | 1,178 |
| any float | Fixed α everywhere; sweepable as a sensitivity axis. | varies |

Two degenerate cases are forced, not configured: a county with no weighted
substation bus takes $\alpha = 1$ (equal split over all its buses — there is
nothing to weight), and a county with no uncovered bus takes $\alpha = 0$ (the
equal pool has nowhere to go). Three counties fall in the first case — Imperial,
Modoc, Siskiyou, together 1.5% of state load — which is exactly the
"counties without IOU substations" branch of the design.

**ReEDS enters at exactly one point: the county total.** Nothing else in the
allocation descends from it. The within-county weight $w_i$ is the utilities'
own measured `max_load` envelope, taken straight from
`substation_load_profiles_clean.csv`. (Approach 1's `substation_annual_load.csv`
was *not* used, precisely because that number is already ReEDS county load ×
substation share — feeding it back in would let ReEDS set the county total a
second time, through the back door.) The 28 of 1,347 substations with a
non-positive mean envelope — dead or net-export sites — are clipped to zero
weight.

Because both α settings share the same $w_c$, **their county totals are
identical** — they differ only in the within-county split. That makes the pair a
clean isolation of the within-county question.

### The region is a parameter, not a commitment to counties

Nothing in the allocation above is specific to counties or to ReEDS. The
algorithm consumes exactly two things: a **partition of candidate buses into
regions**, and **one relative weight per region**. `fips_int` is used purely as
an opaque group label, `w_c` purely as a relative magnitude — which is why the
ReEDS load level cancels. The α split, the envelope weighting, and the per-cell
expansion never inspect what a region *is*.

So a different regionalization — ZIP codes, utility planning areas, CAISO local
capacity areas, climate zones — needs only two substitutions:

| Component | Today | To generalize |
|---|---|---|
| bus → region | `candidate_buses()`, point-in-polygon on TIGER counties via `nodes_by_county()` | any polygon layer with a stable id column |
| region → weight | `reeds_county_annual()`, normalized to shares | any table of `(region_id, relative_weight)` |

Two requirements the code enforces and a new layer must satisfy: the regions
must **partition** the candidate buses (each bus in exactly one), and **every
region containing a candidate bus must carry a weight** — the run aborts with
the offending region names otherwise, rather than silently dropping their load.
Finer regions make the ReEDS-vs-envelope division of labour more favourable
(the external source pins more of the spatial pattern, the envelopes less), so
this is a real experimental axis rather than a refactor for its own sake.

### Static vs month-hour weighting (`--level`)

`--level static` collapses each substation's envelope to one number (its mean
`max_load` over the 288 cells), so a bus's share is fixed for the whole week.
`--level monthhour` keeps the envelope cell by cell: hour $t$ of a
representative week is mapped to its $(\text{month}, \text{hour\_pst})$ cell
through `genx/rep_week_calendar.csv`, and the within-county split uses that
cell's weights, so the bus split follows the diurnal and seasonal shape the
utilities actually measured.

Only the split *within* a county's substation buses varies by cell. Which buses
are covered — and therefore α and the size of each pool — are structural facts
about where substations exist, not hour-dependent, so the equal pool is carried
through unchanged. County shares still hit their ReEDS target in **every cell**
(verified to 5×10⁻⁹ across all 120 cells the four weeks touch).

**How often does the missing-cell fallback actually fire?** Almost never. Of the
387,936 possible (substation × cell) slots, **72 are missing — 0.02%**. Within
the four representative weeks the picture is even cleaner:

| Week | Cells | Slots | Missing | Share |
|---|---|---|---|---|
| Winter | 24 | 32,328 | 0 | 0.00% |
| Spring | 48 | 64,656 | 0 | 0.00% |
| Summer | 24 | 32,328 | 0 | 0.00% |
| Fall | 25 | 33,675 | 24 | 0.07% |
| **Aggregate (all 288 cells)** | 288 | 387,936 | **72** | **0.02%** |

So the fallback is a correctness guard, not a material modelling assumption: it
touches 24 slots in one of the four weeks and nothing in the other three.

The effect is concentrated exactly where it should be: **equal-pool buses show
zero share variation across cells** (by construction), while **envelope-pool
buses swing by a median 68% of their own mean share** (p90 152%). A substation
missing a cell falls back to its own mean over the cells it has, so it keeps its
place in every split rather than handing its load to its neighbours.

### Which buses are eligible to carry load

The candidate pool is **3,778 buses**:

| Included | Count | Why |
|---|---|---|
| `Type = 'Substation'`, non-`IMPORT` | 3,168 | Real substations. Kept whether or not CATS itself loads them — a substation the model leaves unloaded is still somewhere our methods may legitimately place load. |
| `Type = 'AddedNode'` **that CATS loads** | 610 | AddedNodes are topology helpers, but CATS places **15.9% of state load** on these. Excluding them would force that load somewhere CATS never intended. |
| `Type = 'AddedNode'` with zero CATS load | **0 — excluded** | 5,089 pure routing points. Nothing should ever land there. |
| `IMPORT` buses | 0 — excluded | Import proxies, not load. |

"CATS loads it" is read from the GenX control demand files being rescaled —
the current, authoritative copy (1,861 `Substation` + 610 `AddedNode` = the
control's 2,471 loaded zones). It is deliberately *not* read from
`data/raw/CATS/Demand_data.csv`, which is an earlier GenX run's output: the
two agree on the loaded-bus support set but differ in magnitudes. Of the
3,778-bus pool, 3,769 fall inside a county polygon; the remainder sit outside
every county and are not eligible for county-based allocation.

**The pool is larger than the loaded set, and that matters.** CATS loads 2,471
buses (1,861 + 610) while the pool is 3,778, so **1,307 `Type='Substation'`
buses sit at zero in the control**. Keeping them eligible is a deliberate
choice: a real substation the model happens to leave unloaded is still a
legitimate place for our methods to put load. It is also the mechanical reason
the families differ so visibly in "buses loaded" — full redistribution
(county-first) can light those 1,307 up, while a hold never can, because a hold
only re-splits load that already exists on its pool buses.

Energy shares worth quoting (recompute with `doc_numbers.py --sections I`):

| Quantity | Value |
|---|---|
| Control energy on the 610 loaded `AddedNode` buses | **15.9%** |
| Control energy on buses outside the candidate pool | **0.109%** (4 buses: 2408, 2759, 6994, 7610 — outside every county polygon) |
| Control energy a hold re-allocates / leaves untouched | **53.8% / 46.2%** |
| Candidate buses receiving zero share under `aratio` | **1** (bus 2318, 0.078% of control energy — its only substation has a zero envelope and its county has no uncovered bus) |

The 46.2% a hold leaves untouched is the coverage gap in one number: load on
buses no IOU substation maps to (municipal territory — LADWP, SMUD, IID — the
loaded `AddedNode`s, and CATS substations with no IOU counterpart). It is the
ceiling on what any hold-type method can influence, and the reason the
county-first family exists at all.

## Stochastic pool-and-redistribute (`--weights stoch`)

Where the county-first method **imposes** county totals from ReEDS, this one
**sweeps a pool of load off the network and lets Approach 2's own spatial
structure deal it back out**, so county totals *emerge* rather than being set.
That difference is the point: it is what makes the stochastic input the "more
complicated" one, and it is what the comparison against the ReEDS family is
testing.

### F\* is recalibrated on the CATS demand itself

Approach 2's level parameter $F^\ast$, its shape $s(c)$ and correlation
$\rho(c)$ are estimated against whatever target is being disaggregated —
nothing is inherited from another dataset except the substations' own
envelopes. The default stochastic run is therefore the **CATS-calibrated**
one (`stochastic__cats_caiso_target__normal__Fcal__native__calibtgt`):
`build_cats_target.py` writes the statewide hourly total of the four control
weeks (672 hours, 120 of 288 cells, 3–8 observations per cell, mean
24,628 MW), and `generate_stochastic.py --calibrate-on target` estimates on
it. **$F^\ast = 0.841$ on CATS vs 0.7361 on EIA-930 history** — the IOU
substation fleet accounts for a larger share of the CATS control demand than
of historical CAISO metered demand. If a longer CATS/GenX demand record
becomes available, `--calib-target` calibrates on all of it while only the
weeks of interest are disaggregated.

Validation on this target (5 draws, native z): hourly tracking relRMSE 0.49%
with −0.02% bias; per-cell total q10/q90 errors median ≈1%. Envelope recovery
is noisier than on EIA-930 (median ≈8% of width vs ≈1%) — a small-sample
effect of having only ~7 target hours per cell, not model error.

### Per-cell weights, per-cell sweep

The pool is dealt out per (month, hour_pst) cell using Approach 2's own
per-cell output (`substation_cell_mw.csv`, written with `--save-cells`): in
cell $c$, substation buses split their portion of the pool in proportion to
the model's mean load $E[L_s(c)]$ (or a single draw's cell mean under
`--draw 0|1|2`). The swept set and β are structural — which counties gate and
which buses map to substations does not change hour to hour — so only the
within-pool split varies by cell. Negative cell means (net-export midday
cells) are clipped to zero weight — 1,337 of 160,920 substation-cell slots
(0.83%) on the mean draw — and a substation missing a cell falls back to its
own mean over the cells it has (48 slots, 0.03%).

**What goes into the pool** is set by one knob, `--stoch-gate`, a county coverage
threshold (substation buses ÷ candidate buses):

| County | Swept into the pool |
|---|---|
| coverage ≥ gate | **every** bus, including those no substation maps to |
| coverage < gate | only its substation buses; every other bus keeps its control value |

So a single parameter separates the two designs (numbers for the
CATS-calibrated run under the `prox` map):

- **Way 1 (broad), `--stoch-gate 0.30`** — the 30 counties at ≥ 30% coverage
  are fully re-dealt, so their uncovered buses change too. Swept pool: 2,442
  buses = 1,026 substation + 1,416 uncovered.
- **Way 2 (narrow), `--stoch-gate 2.0`** — no county qualifies, so only
  substation buses anywhere are re-dealt and everything else is untouched.

**Recipients** are always every substation bus carrying stochastic load; the pool
is dealt in proportion to it. With `--stoch-topoff equal` the swept uncovered
buses are kept alive rather than zeroed: they take $\beta = |U|/n$ of the pool
as an equal split — the count ratio over the swept set, exactly the role
$\alpha = u/n$ plays in the county-first family — and the substation buses
split the remaining $1-\beta$. Under the prox map $\beta = 0.580$ (1,416 of
2,442 swept buses are uncovered). Top-off is meaningless without a gate, since
then nothing uncovered is ever swept, so it is offered on Way 1 only.

Conservation is immediate: the pool is redistributed within itself and every bus
outside it is copied through unchanged, so the statewide hourly total is
untouched.

**Draws.** `--draw mean` averages Approach 2's 5 Monte Carlo draws into one
stable weight; `--draw 0|1|2` carries a single realization so the model's own
spread reaches GenX. Both are run: a mean variant plus three single draws per
design. Draw-to-draw variation is small at this aggregation — a substation's
share of the statewide total varies by a median 3.1% across the five draws
(p90 6.3%, CATS-calibrated run) — which is itself a reportable result about
how much of the stochastic layer's spread survives aggregation to bus weights.

## Envelope hold (`--weights env`)

The **minimal-intervention** allocation, and the only one that never asserts
anything about buses our substation data does not reach. In one sentence: *it
takes the load the control already places on the buses we do have substations
for, and re-splits only that load among only those buses — in proportion to the
substations' own measured `max_load` envelopes — leaving every other bus
exactly as the control had it.* No ReEDS input of any kind, no projection
model; the weights are the utilities' measurements, full stop.

At `--level monthhour` the covered pool is fixed and its internal split follows
each cell's own envelope weights, so the hold variant participates in the
static-vs-monthhour contrast on equal terms.

Precisely — let $C$ be the covered set (buses at least one substation maps to
with positive envelope weight; 1,028 buses under the `prox` map). For each
hour $t$:

$$
d'_{t,i} = \begin{cases}
P_t \cdot \dfrac{m_i}{\sum_{j \in C} m_j} & i \in C \\[2ex]
d_{t,i} & i \notin C
\end{cases}
\qquad \text{where } P_t = \sum_{j \in C} d_{t,j}
$$

$d_{t,i}$ is the control's load, $m_i$ the substation envelope weight carried
onto bus $i$, and $P_t$ is the **covered pool** — the total load the control
happens to put on covered buses in that hour. The pool is emptied and re-dealt
among the same buses using the envelope proportions; buses outside $C$ are
copied through untouched. Statewide conservation is then immediate:
$\sum_i d'_{t,i} = P_t + \sum_{i \notin C} d_{t,i} = \sum_i d_{t,i}$.

Consequences worth stating in a write-up:

- **Only about half of state load is ever re-allocated** (the covered-pool
  share; see `coverage_share` in the manifest). The rest keeps CATS's own
  allocation, because we have no substation evidence about those buses.
- **The bus support set is essentially preserved**: no bus is switched on or
  off except covered buses the envelope weights at zero.
- **County totals are not corrected.** The hold uses no county reference at
  all, so a county's total ends up wherever the control plus a partial
  re-split leaves it. This is the main structural difference from the
  county-first family, and the reason the hold is a *contrast* rather than a
  competing estimate.
- It is a **lower bound** on how much load allocation can matter: it is the
  smallest intervention that still applies our data everywhere it has
  something to say.
- Paired with the stochastic Way 2 run (nearly the same pool; support overlap
  ≈1), it isolates the *weighting method* — measured envelope vs stochastic
  model — with scope held fixed.

### Can a hold use stochastic weights instead of envelope weights?

Yes, and it already does: **`stoch` at `--stoch-gate 2.0` (Way 2) *is* the
stochastic hold.** With no county gated, the sweep collects exactly the
substation buses and every other bus is copied through — mechanically the same
hold, with Approach 2's per-cell output supplying the weights in place of the
raw envelope. `--weights env` is not a different coverage rule, only a
different weight source, which is why the two land on 1,028 vs 1,026 pool
buses. Read them as one design run twice:

| Run | Pool definition | Weight inside the pool |
|---|---|---|
| `env … hold` | substations with positive mean envelope | mean `max_load` (per cell at `monthhour`) |
| `stoch … w2` | substations with positive simulated load | Approach 2 per-cell mean MW |

The county-first family is the one that genuinely cannot be expressed this way,
because it holds nothing back: it re-allocates the whole state outward from the
ReEDS county weights, so there is no "pool" to re-weight.

## The map axis — four ways from a substation to a bus

The `map` slot picks the substation→bus artifact; all are built once and
cached, none depends on any run axis. `prox`/`voltres` come from
`map_loads_to_nodes.py`; the other three from
`build_identity_catchment_maps.py`.

**`nameprox` — identity first.** CATS bus coordinates descend from the
CEC/HIFLD lineage: all 3,171 `Type='Substation'` buses sit within 8 m (p99) of
a CEC record, so a bus effectively *is* its CEC record. Our substations link to
CEC records by name (`norm()` + `cecSourceDictionary.csv` — the same match
definition as `audit_substation_coverage.py`). Chaining the two gives an
identity assignment: **1,077 of 1,336 substations (80.6%) match 1,242 buses**
(PGE 530 subs at median 39 m, SCE 466 at 60 m, SDGE 81 at 1.28 km — the known
SDGE polygon-centroid offset), each matched substation splitting equally over
its identity buses (a station's several voltage-level buses). The remaining
259 substations keep their proximity rows. A 30 km sanity gate rejects name
collisions between distant same-name sites (0 rejections in practice). Result:
median assignment distance falls from 0.133 km (prox) to 0.065 km, buses >1 km
from their substation from 40.4% to 22.4%, >10 km from 11.9% to 4.4%.

**`catch` — transportation-LP catchments.** Solves

$$\min \sum_{ij} x_{ij} d_{ij} \quad \text{s.t.} \quad \sum_j x_{ij} = 1 \;\forall i \in L, \qquad \sum_i x_{ij} \ge 1 \;\forall j \in N, \qquad x_{ij} \in [0,1]$$

where $L$ is the candidate bus pool (3,769 buses with a county), $N$ the 1,336
substations, and $d_{ij}$ great-circle distance. The constraint matrix is a
bipartite transportation structure, hence totally unimodular: the LP vertex
optimum is integral (verified — 0 fractional variables), so no MIP is needed.
Arcs are sparsified to each bus's 20 nearest substations plus each
substation's nearest buses as a feasibility floor. Solves in ~1 s; total
assignment distance 28,192 km (mean 7.5 km per bus). Each substation's load
then **returns to its catchment** as an equal split — so **every candidate bus
stays loaded** (the answer to the coverage problem: no uncovered buses exist
under this map). Largest catchment: 99 buses (a fringe substation catching a
sparse region). Median bus→substation distance 1.98 km, p95 33.5 km.

**`namecatch` — identity + catchments.** The 1,242 identity matches enter the
LP as forced assignments ($x_{ij} = 1$ fixed); the LP places the remaining
2,527 buses. When forcing removes many buses from the free pool, nearby
unmatched substations can collide on the same few feasibility arcs (Hall's
condition fails locally); the builder widens the per-substation feasibility
arcs and re-solves until feasible.

Under the catchment maps the county-first α machinery **nearly** degenerates,
which is worth stating precisely because the obvious guess is slightly wrong.
Every candidate bus is assigned to some substation, so almost none is
"uncovered" and α collapses: mean α falls from **0.707 (prox) to 0.013
(catch)**, and only **8 of 57 counties** still contain an uncovered bus versus
56 of 57 under proximity. The equal-split pool therefore shrinks from **65.5%
of statewide energy to 3.8%**. It does not reach exactly zero because a bus
assigned to one of the 28 substations with a non-positive envelope still has
zero weight, and so still counts as uncovered. `aratio` and `a0` consequently
come very close together under catchment maps but are not identical. The
stochastic gate is likewise near-moot (essentially every county fully covered),
so catchment stochastic runs are tagged `w2`.

## Share-vector construction

**Step A — substation weights.** For `stoch`, read the Approach 2 run at
`--year` (default 2019, the scenario year): the per-cell table for the
month-hour split, the annual table for the structural sweep. For
`reedsco`/`env`, the substations' own `max_load` envelopes (per cell or their
mean). `--draw` selects the mean over the 5 Monte Carlo draws or one
realization.

**Step B — substation → bus.** Join to the chosen map and multiply each
substation's weight by its `share` (tie-split or catchment fraction), then sum
per bus. Write $m_i$ for the resulting weight on bus $i$; the **covered set**
is $C = \{i : m_i > 0\}$.

**Step C — shares.** Each family turns $m_i$ into its share vector as
described in its own section: county-first normalizes within counties under
the ReEDS county weights and the α split; the stochastic family normalizes
within its swept pool with the β top-off; the envelope hold normalizes within
$C$. No step anywhere compares ReEDS load *levels* against method load —
only normalized ReEDS county weights ever enter, and only in `reedsco`.

## Conservation

Let $d_{t,i}$ be the control load on bus $i$ in hour $t$ and $T_t = \sum_i
d_{t,i}$ the control's statewide total in that hour.

**Full redistribution** (county-first) rewrites everything:
$\;d'_{t,i} = T_t \, s_i$ (per cell, $s_i$ is that hour's cell vector).

**Hold redistribution** (stochastic pool, envelope hold) rewrites only its
pool. With $P_t = \sum_{i \in C} d_{t,i}$ and shares renormalized within $C$:

$$
d'_{t,i} = \begin{cases} P_t \, s_i & i \in C \\ d_{t,i} & i \notin C \end{cases}
\qquad\Rightarrow\qquad \sum_i d'_{t,i} = P_t + \sum_{i \notin C} d_{t,i} = T_t
$$

Both hold to float precision. They also hold **at the precision the file is
written in**, which does not follow automatically: rounding 8,870 values to one
decimal leaves a residual of up to ~440 MW per hour. Output is therefore
apportioned by **largest remainder** in units of 0.1 MW — floor every value,
then hand the leftover units to the cells with the largest discarded fractions
(reclaiming from the smallest when the floor sum overshoots). Every hour's
printed statewide total equals the control's exactly.

Both properties are asserted on every run (`max_abs_hourly_dev_preround_mw`
< 1e-6, `max_abs_hourly_dev_printed_mw` = 0) and the run aborts otherwise.

## Byte-safety of the GenX file format

A GenX demand file's first 9 columns are metadata (`Time_Index`, `Voll`,
`Demand_Segment`, ...) and are *ragged* — only the first few rows carry values.
They are read as strings and written back verbatim, never parsed, so blank
cells and integer formatting (`Voll=200000`) survive untouched. Only the zone
columns are converted to float. Reading and rewriting an untouched control is
**byte-identical** (md5), asserted for all four seasonal controls by
`test_genx_rescale.py`; that identity is what licenses treating the metadata as
opaque.

Season detection verifies by md5 that all cases of a season really do share one
demand file, rather than assuming it — a silent mismatch would mean rescaling
the wrong week.

## Outputs

```
genx/rescaled/{run_tag}/
    Demand_data__{Summer,Winter,Spring,Fall}.csv   control layout preserved
    node_shares.csv          per-bus share provenance (per bus-cell for monthhour)
    county_allocation.csv    per-county share, alpha, bus counts (county-first)
    pool_counties.csv        per-county coverage / sweep detail (stochastic)
    manifest.json            axes, provenance, conservation + coverage checks
genx/scenarios_rescaled/{run_tag}/
    p{N}/ ...                full runnable GenX cases (the materialized subset)
    materialize_manifest.json
```

Manifest fields worth reading before trusting a run:

| Field | Meaning |
|---|---|
| `max_abs_hourly_dev_preround_mw` / `_printed_mw` | conservation checks; must be ~0 and exactly 0 |
| `coverage_share` | fraction of control load sitting on the buses being redistributed |
| `topoff_fraction` | county-first: 0 structurally; stochastic: β, the equal-pool share |
| `n_zones_zeroed` / `n_zones_newly_nonzero` | how much the bus support set changed |
| `n_counties_gated` / `n_buses_swept` | stochastic sweep structure |
| `n_cells_filled_from_substation_mean` / `n_cells_negative_clipped` | month-hour fallback/clipping counts |
| `n_substations_unmapped` | substations the map does not carry |

## Caveats

- **The two families place very different fractions of load.** County-first
  rewrites the whole state; the envelope hold moves only the covered pool
  (~half of state load); the stochastic designs sit in between depending on
  the gate. Compare `coverage_share` and the relocation metrics before
  attributing a GenX result difference to the weighting method rather than to
  the intervention's sheer size.
- **Full redistribution zeroes buses the control loads** (`n_zones_zeroed`).
  This is a large spatial intervention; the envelope hold exists precisely as
  the low-intervention contrast.
- **The stochastic β top-off is large under the prox map** (β = 0.580): most
  swept buses in Way 1 are uncovered, so more than half the pool is dealt out
  as an equal split rather than by the model. The catchment maps dissolve this
  (no uncovered buses exist), which is one reason the map axis is worth
  running.
- **Approach 2's envelope-recovery validation is small-sample on the CATS
  target** (~7 hours per cell): median recovery error ≈8% of envelope width vs
  ≈1% on the full EIA-930 record. Sampling noise, not model failure — but
  don't quote the CATS-target recovery figure as the model's accuracy.
- **One projection year.** Shares are drawn from a single year (default 2019,
  matching the scenario year).

## Rep-week calendar

The control files carry only `Time_Index` 1–168 with no date axis, so month-hour
weighting needs the calendar period of each week supplied separately, in
`genx/rep_week_calendar.csv` (`season,start_datetime,timezone,notes`).
`start_datetime` is the wall-clock time of `Time_Index == 1`; hours advance by
one and are converted to the repo's canonical fixed PST (UTC-8, hour-beginning,
no DST) so they align with `hour_pst` in the substation profiles.

The values come from `settings_2019.yml` (`time_slicing.sample_weeks`, the
highest-peak-load week per season in the 2019 CAISO series):

| Season | Week start | Clock | Peak |
|---|---|---|---|
| Winter | 2019-12-09 | PST | 30.5 GW |
| Spring | 2019-05-29 | **PDT** (spans May→June) | 33.8 GW |
| Summer | 2019-08-08 | **PDT** | 44.0 GW (annual peak) |
| Fall | 2019-09-01 | **PDT** | 43.8 GW |

**Three of the four weeks are in daylight saving time**, and none contains a DST
transition, so all four are exactly 168 hours. CATS hours are hour-beginning
(`00:00` covers midnight→1 am) and, since `apply_time_shift: True` puts the VRE
profiles on local Pacific while `demand_hour_shift: 0` says demand needs no
further shift to match, the demand is already local wall clock — hence
`America/Los_Angeles` in the calendar. In fixed-PST terms the three PDT weeks
are therefore offset by one hour: Summer's `Time_Index 1` is cell
(month 8, hour_pst 23), not (8, 0). This resolves the `TODO(verify)` in
`settings_2019.yml`.

## Reproducing

```bash
# no-op check: reproduces the controls byte-for-byte
python scripts/load_projection/genx/rescale_genx_demand.py --weights control

# one run per family
python scripts/load_projection/genx/rescale_genx_demand.py --weights reedsco --alpha ratio --level monthhour
python scripts/load_projection/genx/rescale_genx_demand.py --weights stoch --level monthhour \
    --stoch-gate 0.30 --stoch-topoff equal --draw mean
python scripts/load_projection/genx/rescale_genx_demand.py --weights env --level monthhour

# a map-axis variant
python scripts/load_projection/genx/rescale_genx_demand.py --weights reedsco --alpha ratio \
    --level monthhour --map namecatch

# expand a run into runnable GenX cases (the 8-case set)
python scripts/load_projection/genx/materialize_genx_cases.py \
    --run-tag genx__stoch__prox__w1g30top-mean__monthhour \
    --cases p5,p6,p12,p13,p19,p20,p26,p27 --enable-outputs

# guards + number audit
python scripts/load_projection/genx/test_genx_rescale.py
python scripts/load_projection/genx/doc_numbers.py
```

The full ordered command list lives in [`genx_runbook.md`](genx_runbook.md).
Materializing is a separate opt-in step because each case is a full copy of the
control tree differing in exactly one file.
