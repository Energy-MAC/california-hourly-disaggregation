# Comparing GenX runs across demand allocations

How to say "these two disaggregation methods differ by *x*%" defensibly. Pairs
with [`genx_rescale.md`](genx_rescale.md), which defines how the demand sets are
built; this document defines what is measured once GenX has run on them.

## The experiment

| Held fixed across runs | Varied |
|---|---|
| Network (CATS, 8,870 buses, 10,823 lines) | Which bus each MWh of demand sits on |
| Generator fleet (2,171 resources, `New_Build = 0`) | |
| Statewide demand in every hour (exactly) | |
| VRE profiles, fuel prices, policy, solver settings | |

This is a **controlled experiment with one treatment variable**. Everything that
could otherwise explain a result difference is held constant by construction, so
any divergence in GenX's output is attributable to load location alone. The
hourly-total conservation described in `genx_rescale.md` is what buys this: the
runs cannot differ because one of them simply has more load to serve.

The runs are production-cost (dispatch) models, not capacity expansion:
`New_Build = 0` for every resource and `NetworkExpansion = 0`. **There are no
investment decisions.** This single fact determines which comparisons are
available and is the reason the reference paper's headline metric does not
transfer unchanged. Dispatch-first is a deliberate scoping decision
(2026-08-14): capacity expansion may be overkill for the load-allocation
question, so it is deferred until the dispatch results show whether the
allocation differences are large enough to plausibly move investment — only
then would the CEP inputs (costs, capacity limits, annual weighting) be built.

## What we compare (and what we deliberately do not)

Both runs are optimal dispatches of the same fleet, serving the same total
energy in every hour, on the same network. Their outputs are therefore
**directly comparable element by element**, with no mapping step and no
intermediate model. So the comparison is simply: for each quantity GenX writes,
how far apart are the two runs?

The four things we report, in order of how much they should move:

| Quantity | Where it lives | What a difference means |
|---|---|---|
| **Prices** | `prices.csv`, per bus-hour | Locational marginal price. Separation between buses is congestion made visible — the most sensitive signal. |
| **Power** | `power.csv`, per resource-hour | Which plants run, and where. Aggregates to per-bus generation. |
| **Energy at each node-hour** | the demand matrices themselves | The movement fraction (below), measurable *before* any solve. |
| **Cost** | `costs.csv` | Total operating cost and its split. The headline scalar, and the least sensitive — see the envelope-theorem note below. |

Flows, curtailment, and load shed (EUE/LOLH) are written too and are worth
reporting alongside, but the four above carry the argument.

**No RMML metric, for now.** Glista et al. (2027) Table 2 reports a Reduced
Model Mapping Loss — an investment portfolio chosen on a coarse model,
re-evaluated on the true one. That construction needs a reduced *and* a full
model in different decision spaces, and an investment portfolio that survives
being moved between them. These runs have neither: one network, and
`New_Build = 0` so there are no investment variables at all. Since capacity
expansion is deferred until the dispatch results justify it (see above), the
question RMML answers is not yet on the table. Revisit only if capacity
expansion happens; a design sketch (cross-evaluating unit commitment as the
quasi-first-stage decision) is preserved in `compare_genx_results.py`'s
docstring for that eventuality.

## What GenX writes, and what each output can tell you

| File | Shape (per case) | What a difference means | Sensitivity |
|---|---|---|---|
| `costs.csv` | ~8 components | Total operating cost and its split (variable, fuel, start-up, load-shed penalty). **The headline scalar.** | Low — see below |
| `prices.csv` | 168 × zones | Locational marginal price per bus-hour. Price *separation* between buses is congestion made visible. | **Highest** |
| `flow.csv` | 168 × 10,823 | Power flow per line. Moving load between buses changes flows mechanically. | High |
| `power.csv` | 168 × 2,171 | Generation per resource per hour — which plants run, and where. Aggregates to per-bus generation. | Medium |
| `charge.csv` | 168 × 54 | Storage charge/discharge. Batteries arbitrage *local* conditions, so they track load relocation. | Medium |
| `curtail.csv` | 168 × 874 | VRE spilled. Rises when load moves away from renewable-rich buses. | Medium |
| `nse.csv` | 168 × zones | Load shed. Summed = **EUE**; hours with any shed = **LOLH** — the paper's Table 6 columns. | Low but decisive |
| `capacity.csv` | 2,171 | **Identical across runs by construction** (`New_Build = 0`). Carries no information here — unlike the paper, where capacity *is* the decision. | None |

**Expect cost to understate the difference.** Total cost is a *minimized*
objective, so by the envelope theorem it is first-order insensitive to
perturbations around the optimum, while the decision variables it optimizes over
(dispatch, flows, prices) move first-order. Two load maps can therefore produce
nearly identical system cost while operating the network quite differently. Lead
with the physical divergence metrics and report cost alongside; a small cost
delta with a large redispatch is a real and reportable finding, not a null
result. It says the fleet has enough slack to absorb the relocation — which is
itself the answer to "does nodal load detail matter *for cost*?"

## Metrics

### Where this is implemented

| Concept | Function | File |
|---|---|---|
| Movement fraction (½·L1) | `relocated_pct()` | `genx/compare_genx_demand.py` |
| All per-season input metrics (nRMSE, Spearman, Jaccard) | `season_metrics()` | same |
| Bus → county for the county-level metric | `node_counties()` → `candidate_buses()` | same → `genx/rescale_genx_demand.py` |
| Output-side parsing and metrics | `compare_genx_results.py` (`--inspect` first) | `genx/` |
| Every number quoted in this doc | `doc_numbers.py` sections **H** (divergence) and **I** (pool coverage) | `genx/` |

The same two metrics are used on inputs and outputs so divergence can be traced
from one to the other.

**Movement fraction** — for quantity $y$ over cells $i$ (bus-hours, resource-hours, line-hours):

$$M = \frac{1}{2}\frac{\sum_i |y_i^{A} - y_i^{B}|}{\sum_i |y_i^{A}|} \times 100\%$$

The $\tfrac12$ makes it a *movement* share rather than a difference count: every
unit that moves appears twice, once as a loss at its origin and once as a gain
at its destination. Read as "x% of the generation (or load, or flow) is
somewhere else." Bounded, unit-free, and directly comparable across quantities.

**Normalized RMSE** — $\text{RMSE}(y^A, y^B) / \overline{|y^A|} \times 100\%$.
Weights large deviations more heavily, so it separates "many small shifts" from
"a few big ones." Note the denominator: on the demand side it is the mean over
**buses the control actually loads**, because normalizing by the whole matrix
would divide by ~72% structural zeros and inflate every percentage
meaninglessly.

**Bus-level vs county-level movement.** Both are reported. The gap between them
is the substantive result: movement that survives county aggregation means the
methods disagree about *regional* load; movement that vanishes under aggregation
means they agree regionally and disagree only about which bus inside the region
carries the load. Only the second kind is a question a nodal model can answer
and a zonal model cannot — which is precisely the axis Glista et al. study from
the other direction.

## Input-side results (measured, no GenX run needed)

`compare_genx_demand.py`, each allocation vs the CATS-native control, mean over
the four seasonal weeks (refreshed 2026-08-17 after the coordinate overrides; stochastic runs are per-cell
on the CATS-calibrated Approach 2; recompute via `doc_numbers.py` section H).
Single-draw variants are collapsed into the "draw spread" finding below.

| Run | Energy relocated (bus) | (county) | Spearman | Buses loaded | Support overlap |
|---|---|---|---|---|---|
| **ReEDS county-first** (county totals imposed) | | | | | |
| `reedsco__prox__aratio__static` (Method 1) | 49.7% | 14.41% | 0.403 | 3,762 | 0.652 |
| `reedsco__prox__aratio__monthhour` | 49.7% | 14.41% | 0.403 | 3,763 | 0.652 |
| `reedsco__prox__a0__static` (Method 2) | 58.6% | 14.41% | 0.458 | 1,177 | 0.418 |
| `reedsco__prox__a0__monthhour` | 58.7% | 14.41% | 0.457 | 1,177 | 0.418 |
| **Stochastic pool, per-cell** (county totals emerge) | | | | | |
| `stoch__prox__w1g30top-mean__monthhour` (Way 1, top-off) | 43.8% | 14.20% | 0.469 | 3,107 | 0.794 |
| `stoch__prox__w1g30-mean__monthhour` (Way 1, no top-off) | 47.8% | 17.48% | 0.668 | 1,691 | 0.684 |
| `stoch__prox__w2-mean__monthhour` (Way 2, narrow) | 20.5% | 7.74% | 0.758 | 2,468 | 0.999 |
| **Envelope hold** (minimum intervention) | | | | | |
| `env__prox__hold__static` | 20.3% | 7.68% | 0.760 | 2,467 | 0.998 |
| `env__prox__hold__monthhour` | 20.7% | 8.29% | 0.756 | 2,468 | 0.999 |
| **Map axis** (same allocations, different substation→bus map) | | | | | |
| `reedsco__nameprox__aratio__monthhour` | 48.1% | 14.41% | 0.405 | 3,758 | 0.653 |
| `reedsco__catch__aratio__monthhour` | 50.4% | 14.41% | 0.391 | 3,693 | 0.651 |
| `reedsco__namecatch__aratio__monthhour` | 50.7% | 14.41% | 0.387 | 3,691 | 0.652 |
| `stoch__nameprox__w1g30top-mean__monthhour` | 45.6% | 17.21% | 0.403 | 3,427 | 0.720 |
| `stoch__catch__w2-mean__monthhour` | 49.8% | 17.85% | 0.447 | 3,608 | 0.669 |
| `stoch__namecatch__w2-mean__monthhour` | 49.6% | 17.70% | 0.448 | 3,607 | 0.671 |

The two families bracket the question from opposite directions, which is the
design's point. The **ReEDS** methods impose county totals, so their
county-level relocation is pinned at 14.41% regardless of the within-county
rule *and regardless of the map* — six different bus-level allocations, one
county figure. The **stochastic** methods let county totals emerge, and their
county relocation therefore varies with the design and the map (7.7% to
17.8%). A stochastic run's county figure is a *result*, a ReEDS run's is an
*input*.

**`env hold` vs `stoch w2` is the clean weighting-method pair.** They re-split
nearly the same pool (support overlap ≈0.999, 2,467–2,468 buses) and land
within 0.2 pp of each other on bus relocation (20.3% vs 20.5%) — so whatever
separates their GenX results is the *weighting method* (measured envelope vs
per-cell stochastic model), with the intervention's scope held fixed.

**The map matters as much as the within-county rule for the stochastic
family.** Under the catchment maps the narrow stochastic design relocates
49.8% at bus level and 17.8% at county level (vs 20.5% / 7.7% under prox):
with every candidate bus in a catchment, the sweep touches the whole network
and the emergent county pattern moves further from CATS. For the county-first
family the map changes bus relocation only within 48.1–50.7% — regional
structure is pinned, so the map only re-routes within counties.

**Draw-to-draw spread is negligible at this aggregation.** Bus-level
relocation moves ≤ 0.06 percentage points between the mean and any single draw
(Way 1 top-off: 43.76 / 43.83 / 43.77 / 43.80), county-level ≤ 0.03 pp; the
underlying substation shares vary by a median 3.1% (p90 6.3%) across the five
draws. Three single-draw runs are carried alongside each mean run so this can
be checked downstream rather than assumed: if GenX results differ between
draws by more than they differ between methods, the method comparison is not
resolvable at this sample size.

### Reading guide — why "buses loaded" varies so much

Two structural switches explain every row above. Neither is about the weights.

First, the arithmetic that makes the rest legible — **the control loads 2,471
buses but the candidate pool is 3,778**, and the gap is the whole story:

```
Type='Substation', non-IMPORT      3,168 buses ─┬─ CATS loads      1,861
                                                └─ CATS leaves EMPTY 1,307
AddedNode buses CATS loads           610 buses (15.9% of energy)
                                   ─────────────────────────────
CATS control loads                 2,471 = 1,861 + 610
candidate pool                     3,778 = 3,168 + 610   (3,769 inside a county)
pool buses CATS leaves empty       1,307
```

Those **1,307 empty candidate buses** are why `reedsco aratio` ends up loading
*more* buses than the control (3,763 vs 2,471) while every hold ends up with
slightly *fewer* (2,468).

**Switch 1 — full vs hold redistribution.** County-first runs redistribute the
*entire* statewide total over their share vector, so they can put load on buses
CATS leaves dark — 1,302 of the 1,307 light up — and a bus the control loads
but the vector omits is **zeroed**. The stochastic and envelope-hold runs
redistribute only the load already inside their own pool and **copy every other
column through verbatim**: they can move load between pool buses but can never
create a newly loaded bus, because there is no load there to redistribute.

That is also why "zeroing" is a big effect only for `a0`. Under `aratio` the
share vector reaches 3,768 of the 3,769 in-county candidates, so just **5**
control-loaded buses are zeroed (4 sitting outside every county polygon —
`2408, 2759, 6994, 7610`, together **0.109%** of control energy — plus bus
`2318`, the one candidate that receives zero share, **0.078%**, because its only
substation has a zero envelope and its county has no uncovered bus to fall back
on). Under `a0` the vector is substation-buses-only, so **1,394** control-loaded
buses are zeroed by design.

**Switch 2 — do uncovered buses inside the pool get a share?** Only relevant
when a pool contains buses no substation maps to (Way 1, and the county-first
α pool). With top-off on they stay alive; with it off they are swept and then
given nothing, so they go to zero.

Decomposing the Summer week makes it concrete (control loads 2,471 buses):

| Run | Pool | Control-loaded buses outside the pool | Pool buses ending at 0 | Newly loaded | Total loaded |
|---|---|---|---|---|---|
| `env hold` | 1,028 | 1,443 **kept** | 3 | 0 | 2,468 |
| `stoch w2` | 1,026 | 1,445 **kept** | 3 | 0 | 2,468 |
| `stoch w1g30top` | 2,442 | 668 **kept** | 3 | 639 | 3,107 |
| `stoch w1g30` | 2,442 | 668 **kept** | 1,419 | 0 | 1,691 |
| `reedsco aratio` | 3,768 | 5 **zeroed** | 5 | 1,302 | 3,763 |
| `reedsco a0` | 1,180 | 1,392 **zeroed** | 3 | 101 | 1,177 |

So the intuition that a hold "leaves everything loaded" is exactly right, and
the table confirms it: `env hold` and `stoch w2` load 2,468 of the control's
2,471 buses. The variability lives in the other rows — `reedsco a0` zeroes
1,395 buses because full redistribution omits every non-substation bus, while
`stoch w1g30` zeroes 780 because it sweeps 2,442 buses into a pool and then
hands all of it to the 1,026 substation buses.

**How much load a hold actually moves: 53.8%.** Of the control's total energy,
`env hold` re-allocates **53.84%** and leaves **46.16%** exactly as CATS had it;
`stoch w2` is 53.83% / 46.17%. That untouched 46% is load on buses no IOU
substation maps to — municipal territory (LADWP, SMUD, IID), the 610 loaded
`AddedNode`s, and CATS substations with no IOU counterpart. It is the ceiling on
what any hold-type method can influence, and the single clearest statement of
the coverage gap this project works around. (Recompute:
`doc_numbers.py --sections I`.)

**The 3 buses a hold does drop are real, not a bug.** They are SCE sites whose
measured envelope is effectively zero — Cottonwood (mean `max_load` 0.0003 MW)
and Converse Flats (0.014 MW, tie-split across buses 2395/2397). Their share is
so small that the load rounds below the file's 0.1 MW precision, so the ~864
MWh/week the control puts on them is re-dealt to their neighbours. That is the
method working as intended: the utility data says these substations carry no
load.

**Why `env hold` and `stoch w2` are near-identical in scope.** Their pools are
1,028 vs 1,026 buses. A bus enters the envelope-hold pool if some substation
mapping to it has positive mean `max_load`; it enters the stochastic Way 2 pool
if some substation mapping to it has positive simulated load. Since the
stochastic model's per-cell mean is generated *from* those same envelopes
(μ = (q10+q90)/2), the two positivity tests almost coincide — they can only
differ where a substation's envelope midpoint and its envelope mean disagree in
sign. Same pool, same hold mechanics, different weights inside: that is exactly
what makes the pair a controlled read on the weighting method.

**Support overlap is a set measure, not a ranking.** It is the Jaccard index of
the *sets of buses carrying nonzero energy* — |A ∩ B| / |A ∪ B| — so it answers
"do the two runs light up the same buses at all," and it is near 1 for the hold
runs simply because they switch almost nothing on or off. Rank agreement is the
separate **Spearman** column, computed over the buses both runs load, which
answers "do they agree about which buses are the big ones." The two say
different things: `stoch w1g30` has middling overlap (0.684) but the highest
Spearman (0.668), because it drops many buses yet ranks the survivors much like
the control.

Month-hour weighting moves these week-total statistics barely at all (49.67 vs
49.74% for Method 1), which is expected: the metric aggregates each bus over
the whole week, and the static weight is that week-average by construction. Its
effect is on the *within-week* profile — envelope-pool buses swing by a median
68% of their own mean share across cells — so it will show up in dispatch,
congestion and price timing rather than in relocated energy. Comparing `static`
against `monthhour` (reedsco, env) is therefore a clean test of whether
**temporal** load detail matters once spatial detail is held fixed. The
stochastic family has no static counterpart: static stochastic weights collapse
the model to a rescaled envelope midpoint, which is why the rescaler refuses
them.

Two more findings are load-bearing for the write-up.

**The two county-first methods relocate the same 14.41% at county level.** Not
approximately — identically, because both take county energy from the same ReEDS
weights and differ *only* in how a county splits it internally. The pair is
therefore a controlled experiment inside the controlled experiment: any GenX
difference between `aratio` and `a0` is attributable to within-county bus
placement alone, with regional load held fixed.

**Bus-level movement is 3–4× county-level movement in the county-first
methods.** The methods and the control largely agree about which *county* load
is in and disagree about which *bus* inside it carries the load. That is
precisely the disagreement a nodal model can resolve and a zonal one cannot —
the same axis Glista et al. study from the opposite direction, and the reason
this experiment is worth running on a nodal model at all.

Per-season values are identical to two decimals because **the control's spatial
allocation is itself season-invariant**: per-bus shares of statewide energy
agree across the four seasonal weeks to within 8×10⁻⁷ (0.014% relocated), and
within a week each bus's share of statewide load varies by only ~11% of its own
mean. The control is, to a good approximation, one statewide profile scaled by a
fixed per-bus share — the same structural form as a `static` share vector. The
`monthhour` runs therefore carry bus-varying temporal shape that the control
does not have — a difference in kind, not just degree.

The environment and solver are fixed too: Julia + Gurobi (`Run.jl` calls
`run_genx_case!(…, Gurobi.Optimizer)`). Solve time and MIP gap are therefore
comparable across runs and are themselves reportable results — a load map that
induces more congestion can be measurably harder to solve. Runs are driven
locally by `run_genx_local.py`; the shipped `jobscript.sh` targets SLURM on
Berkeley Savio and hard-codes a cluster project path, so it is not used.

**Scope.** 25 allocations × 8 cases = 200 solves, run serially. The 8 cases are
weather years 2011 (highest VRE availability) and 2012 (lowest) × 4 seasons;
weather year changes only `Generators_variability.csv`, so the pair brackets
the renewable range without diluting the load-placement signal.

## Suggested reporting table

Mirroring Table 2's shape, one row per run (per case or averaged over cases):

| Run | Solve time | MIP gap | Energy relocated (input) | Δ cost | Redispatch | Price nRMSE | EUE | LOLH |
|---|---|---|---|---|---|---|---|---|
| control (assumed true) | | | — | ref | ref | ref | | |
| reedsco aratio (static / monthhour) | | | 49.7% | | | | | |
| reedsco a0 (static / monthhour) | | | 58.6% / 58.7% | | | | | |
| stoch w1g30top-mean monthhour | | | 43.8% | | | | | |
| stoch w1g30-mean monthhour | | | 47.8% | | | | | |
| stoch w2-mean monthhour | | | 20.5% | | | | | |
| env hold (static / monthhour) | | | 20.3% / 20.7% | | | | | |
| map-axis variants (6 runs) | | | 45.6–50.7% | | | | | |

**CATS is taken as the true allocation** (user decision, 2026-08-12), which makes
every "Δ vs control" column readable as a deviation from truth rather than a
symmetric difference. Note this fixes the *direction* of comparison; it does not
by itself license calling the result an error metric, since dispatch is
re-optimized from scratch under each allocation rather than one allocation's
decisions being evaluated under another's conditions.

The research question these columns serve is **the relative importance of load
allocation at the input versus network topology and other constraints**. This
experiment fixes topology and varies allocation; the natural companion
experiment fixes allocation and varies topology (the KITTENS-style reduction of
Glista et al.), and the two magnitudes are then directly comparable on the same
test system. Worth stating as the framing even if only the first half is run.

Input columns are already measured; output columns are filled by
`compare_genx_results.py` once GenX has run.

## Running it

```bash
# input side -- available now
python scripts/load_projection/genx/compare_genx_demand.py

# after GenX runs: confirm the result-file layout, then compare
python scripts/load_projection/genx/compare_genx_results.py --inspect
python scripts/load_projection/genx/compare_genx_results.py
```

`--inspect` prints each result file's shape and label column. **Run it before
trusting the comparison**: GenX's result layout varies by version, and the
time-series parsing here (keep rows whose first column matches `t\d+`) was
written against the documented convention, not against output from this
particular build.

## Caveats

- **Diagnostics must be enabled before running.** The control cases ship with
  `WriteNSE`, `WriteCurtailment`, and `WriteCommit` set to `false`, which would
  leave no reliability or commitment data to compare. `materialize_genx_cases.py
  --enable-outputs` turns them on; apply it to **every** run including the
  control, or the runs stop being like-for-like. `WritePowerBalance` is
  deliberately left off — zone × hour × component over 8,870 zones is hundreds of
  MB per case.
- **The families are not competing estimates of one quantity.** County-first,
  the stochastic pool, and the envelope hold encode different claims about
  buses we have no substation for (impose regional totals / sweep and re-deal /
  leave alone), and the catchment maps dissolve the distinction by covering
  every bus. Report them as designs, not as noisy versions of each other.
- **The 8 cases are 2 weather years × 4 seasons.** Weather year varies only
  `Generators_variability.csv`, so it is a replicate dimension for the load
  question; 2011/2012 bracket the VRE-availability range.
- **Solve time is a result too.** Glista et al. report it as a first-class
  column. A load map that induces more congestion can be measurably harder to
  solve.
