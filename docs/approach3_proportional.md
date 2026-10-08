# Approach 3 — Coordinate-free proportional disaggregation

Added 2026-10-08. Spec for `src/load_projection/proportional.py` and
`scripts/load_projection/approach3/`.

Every other approach in this repo routes through California geography:
substation coordinates → nearest bus → county polygon → ReEDS county share.
Approach 3 exists for the case where **no coordinates for the target node system
are available at all**. The county layer, ReEDS county weights and the nodal map
are then all structurally unreachable, so the allocation can only be
proportional to the node levels the source supplies. That makes it a distinct
method, not a configuration of county-first, which is why it carries its own
stable citation id.

**There is no unconditional validation of this method.** The identities below
(hourly conservation, exact seasonal energy, shares summing to 1) are provable
and asserted every run. Everything else is a diagnostic. The CATS back-test
described at the end exercises the machinery and measures feasibility, but it is
partly circular and covers no negative levels, so it is **not** an unbiased
check of the method's accuracy.

---

## 1. Inputs

### Node table (`--nodes`)

| column | type | required | meaning |
|---|---|---|---|
| `node_id` | string, verbatim | ✔ | arbitrary — digits, names, symbols, unicode. Stripped of surrounding whitespace; non-blank and **unique**. This is what carries load. |
| `base_id` | string, verbatim | optional | **the key the mapping joins on.** May repeat; rows sharing it are siblings. Absent ⇒ `base_id = node_id`, recovering the one-level case exactly. |
| `subname` | string | optional | provenance only; never parsed or used as a key |
| `winter_load` | float, signed, dimensionless | ✔ | → `NovApr` (months 11–4) |
| `summer_load` | float, signed, dimensionless | ✔ | → `MayOct` (months 5–10) |

The season split reuses `external_loads.SEASON_COL_BLOCK` by import, not
redefinition. `MayOct`/`NovApr` is the repo's measured choice: it explains 28.9%
of month-hour CAISO load variance against 3.2% for spring+summer, the best of
the six contiguous options.

**`base_id` is an explicit column and is never derived by splitting `node_id`.**
Node ids may contain arbitrary symbols, so no separator is safe to split on and
a parser would silently mis-group on the first id containing the delimiter. If a
source has only `node_id`, the caller adds the column — that is data prep, not a
guess this code should make.

**Refused:** a missing required column; a blank or duplicate `node_id`; a blank
`base_id` where the column is present; a non-finite level. Levels are coerced
with `errors="coerce"`, so a blank, `-`, `N/A` or a number carrying units
arrives as NaN and is refused with a per-block / blank-in-both breakdown —
*a genuine zero (no load that season) and an unknown are not interchangeable*.

**Accepted and first-class:** negative levels, zero levels, nodes with no
mapping. Coordinates, voltage and county columns are **ignored by construction**
and only carried as provenance (the run prints a notice if they are present).

**The node table's absolute scale is discarded.** Unlike the `external_loads`
weight path, where the scale cancels, here only ratios survive and the level
comes entirely from the target. Output must not be read as the source's own
numbers rescaled.

### Mapping edge list (`--mapping`) — supplied, never inferred

| column | required | notes |
|---|---|---|
| `base_id` | ✔ | **the join key**; must exist in the node table. Mapping a base maps **every** sibling under it. |
| `substation_name` | ✔ | must exist in the profiled fleet |
| `utility` | ✔ where the name is shared | without coordinates there is no tie-break |
| `weight` | optional | shape-only; inert when a base has one mapped substation |

The mapping decides **shape only**; the level always comes from the node table.
Many-to-many is permitted. An unresolved edge is **reported, never dropped**.

Two recorded TODOs, both out of scope by user decision:

- **Build the edge list from names ourselves** (2026-10-07). The collapsed
  coordinate-free cascade would be exact `(utility, name)` → unambiguous name →
  inverted `basinSourceDictionary` → unresolved, reusing
  `external_loads.resolve_names` with `cec_index={}`, `sub_coords={}` and no
  lat/lon so its spatial rules 3–4 and the proximity tie-break are structurally
  unreachable. The user has already built this mapping by hand.
- **Solve the shared-name tie-break** (2026-10-08, example file coming). 48
  substation names are shared across utilities, and coordinates are what resolve
  them today (47 of the 48 have candidates ≥ 65 km apart). With no coordinates
  there is no tie-break, so such an edge is **refused** unless it carries
  `utility` — "names are refused, not guessed" is the standing rule. Candidate
  non-spatial discriminators if it proves necessary: `utility` (already
  supported), voltage class, or the node system's own naming convention. Do not
  invent one speculatively.

### Target

`--target-csv PATH` (`datetime_pst` or `dt_pst_hb`, plus `demand_mw`;
hour-beginning fixed PST, validated gapless and monotone), or
`--target resolve --year Y --weather-year W`, which **imports** `build_target` /
`ca_series` / `median_peak_weather_year` from
`deliverables/build_resolve2035_cats_package.py` so RESOLVE's
Baseline-plus-overlays assembly is reused rather than reimplemented.

---

## 2. The construction

With `Y(c) = Σ y(t) over the hours in cell c`, `E(b) = Σ_{c∈b} Y(c)`, blocks
`b ∈ {MayOct, NovApr}`, and `l_n(b)` the node's level share:

```
s_n(c) = l_n(b) · shape_n(c),      shape_n Y-weighted block mean exactly 1

mapped i:    shape_i  from its base's envelope (or a per-sibling draw)
M(c)       = Σ over mapped of s_i(c)
R(c)       = 1 − M(c)
unmapped j:  s_j(c) = R(c) · L_j(b) / Σ over unmapped L(b)
```

Per-cell conservation `Σ_n s_n(c) = M(c) + R(c) = 1` is an **algebraic
identity** — exact for every cell and every sign. Substituting
`shape_U(c) = R(c)/Λ_U(b)` turns the unmapped branch into
`s_j(c) = l_j(b)·shape_U(c)`, and `shape_U`'s own Y-weighted block mean is
`(1−Λ_M)/Λ_U = 1` exactly. So the construction is uniform:

> **Every node — mapped and unmapped — receives exactly `l_n(b)·E(b)` of
> block-`b` energy.** What unmapped nodes give up is **shape, not energy**.

Two consequences worth stating rather than discovering:

- **The slack basis is the unmapped set, and that is forced by exactness, not
  chosen.** Absorbing `R(c)` on *all* nodes gives mapped nodes
  `l_n E (1+Λ_U)` and unmapped `l_n E Λ_U`, breaking exactness. Splitting by
  `|L_j|` hands a generation bus positive load. Both are rejected.
- **The promise is per block, not per year.** A node's summer:winter energy
  split is pinned by its two levels times the target's block energies; no shape
  moves energy across the MayOct/NovApr boundary. That is the only defensible
  reading of a two-number input, but a caller may not expect it.

### 2.1 Why the normalization must be target-energy-weighted

Node `i`'s block energy under `s_i(c) = l_i(b)·shape_i(c)` is
`l_i(b) · Σ_{c∈b} Y(c)·shape_i(c)`, so it equals `l_i(b)·E(b)` **iff**

```
Σ_{c∈b} Y(c)·shape_i(c) / Σ_{c∈b} Y(c) = 1      i.e.
shape_i(c) = env_i(c) · E(b) / Σ_{c'∈b} Y(c')·env_i(c')
```

**`external_loads.normalized_shapes` divides by the UNWEIGHTED cell mean and is
therefore wrong here.** It is correct in its own context, where every downstream
site renormalizes per cell and exactness was never promised. `Y(c)` varies
within a block by month day-count (28 vs 31, ~11%) *and* by the diurnal swing —
**measured, the max/min ratio of `Y(c)` inside one block is 2.153 (NovApr) and
2.238 (MayOct)**, so the two means are simply different quantities. A substation
whose shape tracks the statewide pattern therefore has a `Y`-weighted mean above
its unweighted one, overshoots its energy, and **the slack silently absorbs the
difference** — exactly the failure this design exists to prevent.

Measured (section B of `doc_numbers.py`, reference fixture): the Y-weighted
normalization is exact at **1.7e-18**; the unweighted one is off by **0.1242 pp**
at worst on the 1,003-node reference fixture, and by **0.4304 pp** on the small
fixture in `test_approach3.py` T2. T2 is what stops someone "simplifying" the
code by reusing `normalized_shapes`.

### The reference fixture

Every number quoted in this file comes from a **synthetic but regenerable**
fixture, so the project's reproducibility rule holds without shipping a node
table that is not ours to ship:

```bash
python scripts/load_projection/approach3/make_reference_fixture.py
python scripts/load_projection/approach3/build_proportional_nodal.py     --nodes   data/checks/approach3/fixture/nodes.csv     --mapping data/checks/approach3/fixture/mapping.csv     --target csv --target-csv data/checks/approach3/fixture/target.csv     --tag ref_envelope
python scripts/load_projection/approach3/doc_numbers.py --run ref_envelope
```

Seed 7 is fixed, so it reproduces bit-for-bit. It is structurally like the real
case on purpose: two-level ids carrying `|`, `/`, `,` and spaces; the mixed-sign
base (10, 11, −1); **52.7% of level mass unmapped**, so `Λ_U` is realistic
rather than degenerate; and substation names drawn from the real profiled fleet,
restricted to names only one utility uses so no shared-name tie-break is needed
(that is a recorded TODO, not part of the reference run). Scale: 1,003 nodes /
701 bases / 301 edges, 271.55 TWh target.

A general rule follows, and it is the one most likely to be broken by a later
edit:

> **Any pointwise transform of `env` is exactness-preserving provided the
> Y-weighted renormalization happens AFTER it. Clipping after normalization is
> not.**

That is why `--shape-floor` clips before `energy_normalize`, and why the GenX
`clip(lower=0)` on stochastic cell means
(`rescale_genx_demand.py:482`, `:539`) is deliberately **not** inherited: there
it guards a per-cell denominator against a negative weight, here the levels are
already signed and the slack absorbs the residual. The objects differ too — the
GenX clip applies to a *level*, ours would apply to a *shape*.

### 2.2 Feasibility is a theorem, not a bug

```
A(b) = max_{c∈b} |shape_U(c)| = max_c |R(c)| / Λ_U(b)
```

`A(b)` is the largest multiple of its own seasonal level that any unmapped node
sees in one cell; `A(b) = 1` is perfectly flat. `R(c)`'s Y-mean is pinned at
`Λ_U`, but its cell-to-cell deviation is `−Σ_i l_i(shape_i(c) − 1)`, of
magnitude ≈ `Λ_M ×` the fleet's shape dispersion. So as `Λ_U → 0`, `R(c)`
oscillates about a mean approaching zero: the ratio explodes *and* changes sign.

> Per-cell conservation forces the unmapped set to carry exactly `R(c)`.
> Exactness forces `R`'s Y-mean to be exactly `Λ_U`. The only freedom left is
> how `R(c)` is split *among* unmapped nodes, which changes neither aggregate.
> **Therefore, when `Λ_U` is small relative to the mapped fleet's shape
> dispersion, no construction satisfies per-cell conservation, exact seasonal
> energy and bounded unmapped shapes simultaneously.** The guard is a theorem.
> The escape is to relax one promise.

The subname axis makes this **more likely**, because mapping one base maps its
whole sibling mass: `Λ_M` grows by a base's every sibling, not by one node.

### 2.3 The fallback ladder

Each rung gives up strictly more. Measured on the reference fixture
(`doc_numbers.py` section C):

| rung | keeps | gives up | measured |
|---|---|---|---|
| `--negative-nodes zero` | everything | negative nodes' level mass | the real fix when cancellation is caused by generation buses |
| `--shape-common strip` | exactness, **all** cross-node shape | nothing of substance | A 1.293 → **1.018** (NovApr), 1.356 → **1.020** (MayOct); on a pathological fixture 27.7 → **3.886** |
| `--shape-source flat` | exactness | **all** shape | `M(c) ≡ Λ_M`, `shape_U ≡ 1`, `A(b) = 1.000` exactly — **provably cannot trip any guard** |
| `--conserve renorm` | shape, per-cell conservation | **exact seasonal energy** | 0.0142 pp deviation on the reference fixture |

`--shape-common strip` divides every mapped shape by the fleet common mode
`g(c) = M(c)/Λ_M` and renormalizes per column. Before the renormalization
`shape_U ≡ 1` exactly; after it the oscillation is only second order. The
rationale: the amplification is driven by the common mode — the part of every
mapped shape describing how much the *whole fleet* moves in cell `c` — but the
target `y(t)` already carries the aggregate time pattern. The envelopes' job
here is only to say *which nodes are big in cell c*. Making the slack fight the
fleet common mode is the pathology.

**`--shape-source fleet` is NOT a safe rung.** With every mapped node on one
shape, `shape_U(c) = (1 − Λ_M·fleet(c))/Λ_U` has the same pathology; measured
A = 1.220 / 1.314, still above 1. Only `flat` is provably safe.

**`--conserve renorm`** uses `s_n(c) = l_n·shape_n(c)/D(c)` with
`D(c) = Σ_m l_m·shape_m(c)` and unmapped shape ≡ 1. It keeps per-cell
conservation and needs no slack basis, but the seasonal energy share is no
longer exact (deviation first-order in `−cov_Y(shape_n, 1/D)`). **It is not
unconditionally safer:** it trades the `Λ_U` guard for one on `min_c |D(c)|`,
and with signed levels `D(c)` can cross zero, which flips the sign of *every*
node at once. Under `renorm`, G10 is downgraded from an assertion to a measured
table and the manifest records
`conservation: per-cell renormalization; seasonal energy share NOT exact`.

### 2.4 Within-cell resolution

The utility envelopes are published only at `(month, hour_pst)` and
`substation_cell_mw.csv` is also month-hour, so **288 cells is the resolution
ceiling**. Within a cell every node's share is constant, so all intra-cell
hour-to-hour variation in the output comes from `y(t)`.

---

## 3. Negative levels — one axis, four modes

**Every mode is nothing more than a different definition of `l_n(b)`.** The
share construction, the slack absorption, every guard and the exactness proof
are strictly downstream and completely mode-agnostic — one function,
`node_level_shares(node_table, mode)`. That is what makes "this base should sum
to its net" a one-line swap rather than a redesign.

Worked on the case that prompted it — base `foo` with siblings `a`=10, `b`=11,
`c`=−1 (measured, `test_approach3.py` T8):

| mode | base `l` | `a` | `b` | `c` | **`foo` sums to** |
|---|---|---|---|---|---|
| **`net-base`** (default) | `net = 20` over `T_L` | `·10/21` | `·11/21` | `0.0` | **20.00** |
| `zero` | `Σ positive = 21` | `·10/21` | `·11/21` | `0.0` | 21.00 |
| `participate` | `net = 20` | `·10/20` | `·11/20` | `·−1/20` | 20.00 |
| `refuse` | — | — | — | — | aborts, listing the negative nodes |

`net-base` is the default because it is the most faithful reading of the source:
if the table is a power-flow case, a base's **net** is what that physical
location actually draws, and a net-of-BTM statewide target should be allocated
on net draw. It also keeps the statewide normalization consistently net. It
differs from `zero` **only where a base mixes signs** — a standalone negative
node (its own `base_id`) nets negative and is zeroed under both.

In every mode `T_L(b) = Σ` over **participating bases**, so a base excluded from
the allocation is excluded from the denominator too. A non-participating node is
written as literal `0.0` and **its original level survives in
`node_index.csv`** — nothing is dropped.

`net-base` guards:

- **base net < 0** → the whole base is zeroed (there is no positive sibling to
  carry a negative total, and putting it on one would invert the sign of a load
  node). Counted, reported per base, and surfaced in `summary.csv` because this
  is the one place `net-base` discards level mass. `--on-negative-base refuse`
  aborts instead.
- **base net == 0** → base allocated 0; all siblings 0. Degenerate but defined.
- **base net > 0** → at least one sibling is positive by arithmetic, so the
  `Σ positive` denominator is never zero. Asserted anyway so a refactor cannot
  break it silently.

**Exactness is asserted and holds identically in all four modes** (T8).

---

## 4. Mapping composition and the subname axis

The level always comes from the node table, so the mapping decides shape only.

- **Several substations → one base:** the composite is the **sum of raw envelope
  levels**, cell by cell, then one Y-normalization. Summing is correct because
  the shape of a bus fed by two substations is the shape of their *summed* load,
  which weights each by its own magnitude automatically. A mean of the
  *normalized* shapes would weight a 5 MW substation equally with a 300 MW one
  (asserted in T6). Cells are gap-filled per substation from its own mean
  **before** summing, so a substation absent in one cell cannot drop out and
  distort the composite.
- **One base → its siblings:** the composite shape is resolved **once per
  `base_id`** and broadcast to every `node_id` under it; each sibling keeps its
  own level, so sibling `n` gets `l_n(b)·shape_base(c)`. **Nothing is split,
  because nothing is being divided** — a substation contributes a *pattern*, and
  a pattern is copyable. This is the sharp contrast with `map_loads_to_nodes`'
  tie-share rule, where a substation's *load* is split across tied buses, and it
  is precisely the thing a reader will assume is a bug.
- **One substation → several bases:** each takes that substation's shape
  independently.
- The optional per-edge `weight` affects the shape only when several substations
  share a base, and is a **no-op** when a base has exactly one (normalization
  kills a scalar), which is the common case.

### Siblings never reorder under a shared shape

All siblings of a base share one shape, so their relative split is constant
across all 288 cells of a block and Spearman(h10, h18) within a base is exactly
1.0. **The subname axis adds *level* resolution, never *shape* resolution.**
Measured: the within-base, within-block CV of the sibling share ratio is
**2.0e-10** (float noise) with `--sibling-draws off`. This is the same measured
caveat already recorded for the shared-curve path in `external_loads.py:546-554`.

### Per-sibling stochastic draws (the fix)

`--sibling-draws independent`, with `σ_n(c) = σ_base(c)·L_n/Σ_siblings L`
(user-approved 2026-10-08). Follows `stochastic.generate` exactly:

```
L_n(t) = mu_n(c) + sigma_n(c)·[sqrt(rho(c))·z(t) + sqrt(1-rho(c))·eps_n(t)]
```

with `eps` one draw per unit-**day** reused across that day's 24 hours — the
generator's `eps_mode="daily"` convention. `z` and `ρ(c)` are shared; only
`eps_n` is per sibling, and that is what decorrelates them. The realized
per-cell means are then Y-normalized, so **the draw supplies a shape, never a
level**, and exactness survives (T15: 1.1e-16).

Measured: the within-block sibling share-ratio CV rises from 2.0e-10 to
**4.8e-02** (`proportional`) / **7.1e-02** (`quadrature`) — siblings genuinely
reorder.

**A property of the approved formula worth knowing.** Scaling `μ` and `σ` by the
same `k_n = L_n/ΣL` makes `k_n` **cancel exactly** in the shape:

```
raw_n   = k_n·g_n,   g_n = mu_base + sigma_base·(sqrt(rho)·z + sqrt(1-rho)·eps_n)
shape_n = k_n·g_n / (k_n·Ymean(g_n)) = g_n / Ymean(g_n)
```

So **the level scaling is inert, and what the feature buys is the independent
`eps_n` — not the `σ` scaling.** Every sibling ends up with the same coefficient
of variation `σ_base/μ_base`, differing only by its noise realization. That is a
coherent model ("each sibling is a scaled replica of the parent, with
independent noise") and it is the shipped default, but it must not be documented
as giving small siblings different variability, because it does not.

Measured (T14), with `eps` forced identical across two siblings at `k`=0.9 and
0.1:

| mode | identical shapes | cv(k=0.1)/cv(k=0.9) |
|---|---|---|
| `proportional` | **yes**, 8.9e-16 | **0.979** (≈ 1 — `k_n` cancels) |
| `quadrature` | no, 4.99e-01 | **3.012** (≈ √(0.9/0.1) = 3.00) |

Two consequences:

- **Do not scale `σ` without also scaling `μ`.** That gives
  `cv_n = k_n·σ_base/μ_base`, making a *smaller* sibling relatively *smoother* —
  backwards, since less aggregation means more relative noise. Not offered.
- **`--sibling-sigma quadrature`** is the physically-motivated alternative:
  `μ_n = k_n·μ_base` but `σ_n = √k_n·σ_base`, the standard result for splitting a
  sum into independent components. Then `cv_n = cv_base/√k_n`, a smaller sibling
  *is* relatively noisier, and `k_n` no longer cancels. Default stays
  `proportional`.

**Composite σ for a multi-substation base** is `σ_base(c) = Σ_s σ_s(c)` (perfect
correlation), not `√Σσ_s²`: the substations feeding one base are co-located by
construction and `ρ(c)` already carries the common-factor structure.
`--base-sigma quadrature` is the alternative. Both are inert for a
single-substation base.

**Standing-rule note.** The external-loads rules record that synthesizing an
envelope from a chosen cv was *considered and rejected*, because "it would write
a modelling assumption into a file shaped like measured data." This is
**compatible**: here the assumption is an explicit model option named in the
manifest (`sibling_draws`, `sibling_sigma`, `base_sigma`) and is never written
into a file shaped like measured data. The external-loads weight path is
unchanged and still synthesizes nothing.

Scope: this is Approach 2's own generator applied at finer granularity — **not**
a new `Approach N`.

---

## 5. Shape sources

`--shape-source {envelope, stoch, fleet, flat}`:

- **`envelope`** (default) — the clean substation profiles via
  `weights.load_profiles`, which already dedupes the PGE scraper overlap by cell
  mean and synthesizes `avg_load = (min+max)/2`. `--shape-col {avg_load,
  max_load}`; `avg_load` is the default because the node level is itself a
  central value, so pairing it with the central-tendency shape keeps the two
  consistent.
- **`stoch`** — Approach 2's per-cell output `substation_cell_mw.csv`. Mirrors
  the GenX reader's contract (`--draw mean` averages, an integer keeps one
  realization, `--min-draws` guards a 1-draw run, per-substation mean gap-fill)
  but **omits its `clip(lower=0)`** — see §2.1.
- **`fleet`** — the statewide fleet mean; also the per-base fallback when a
  mapped base's substations have no usable envelope. **Not a safe rung.**
- **`flat`** — the provably-feasible baseline, and literally what a
  seasonal-only node table contains.

### The common-mode trap with `stoch`

Consuming Approach 2 as renormalized shares cancels `F*` and `s(c)` exactly,
leaving `ρ(c)` as the only surviving calibrated parameter — **but that depends on
per-cell renormalization.** Approach 3 normalizes **per block**, which is
strictly weaker: a global scalar like `F/F*` still cancels, but a **cell-common**
factor `g(c)` does **not**. It survives identically in every mapped node's
shape, shifting `M(c)` and hence `R(c)` — reallocating energy within the block
between the mapped fleet and the slack. So Approach 3 is *not* immune to the
calibrated per-cell parameters the way the CATS deliverable is.

`doc_numbers.py` section E reports `max_c |M(c)/Λ_M − 1|` per shape source as
the **common-mode survival number**, and `--shape-common strip` is precisely the
surgical removal of that surviving component — the recommended pairing with
`--shape-source stoch`.

Approach 2 itself should run `--calibrate-on target --calib-target PATH` per the
SCOPE RULE; consistently, `Y(c)` here comes from the series being disaggregated.

---

## 6. Guards

| # | guard | default | what it catches |
|---|---|---|---|
| G1 | `node_id` non-blank and unique; levels finite | hard | input damage; a duplicate would double-count load |
| G2 | `T_L(b) > 0` | hard | a share of a positive load is undefined when the table nets to ≤ 0 |
| G3 | `net_to_gross(participating L(b)) ≥ 0.10` | 0.10 | levels cancelling, so every `l_n` is a ratio of near-cancelling sums |
| G4 | unmapped set non-empty | hard | no slack basis; use `--conserve renorm` |
| G5 | `net_to_gross(unmapped L(b)) ≥ 0.05` | 0.05 | the residual split being numerically meaningless |
| G6 | `A(b) ≤ 5.0` | 5.0 | **the theorem**; names `Λ_M`, `Λ_U`, the worst cell and the ladder |
| G7 | `min_c shape_U(c) ≥ 0` | 0.0 | `R(c)` flipping sign so an unmapped load bus becomes a net exporter; `-inf` permits it deliberately |
| G8 | per column, `net_to_gross(env over block) ≥ 0.20` → else flat 1.0 | 0.20 | not an error, a counted fallback: no pattern can be borrowed from an envelope that nets out. Flat trivially has Y-mean 1, so exactness survives |
| G9 | `Σ_n s_n(c) = 1` | 1e-9 | the conservation identity |
| G10 | per node per block, relative dev ≤ 1e-12 | hard | **the test** — satisfiable only with the Y-weighted normalization |
| G11 | `Σ_n snapped_n(t) == y(t)` on the `10^-d` grid | 1e-6 | printed-precision conservation, against the **grid-snapped** target |
| G12 | printed-vs-float per-node annual drift | reported | measured 11.5 MWh against a 438 MWh bound at d=1, 8,760 h |
| G13 | sanitization is a bijection; the index round-trips | hard | an id that would corrupt the CSV |
| G14 | every edge's `base_id` exists in the node table | hard | a mapped base with no sibling; says whether the table is one- or two-level |
| G16 | every `node_id` resolves to exactly one `base_id` | hard | mis-grouped siblings |
| G17 | `net-base`: a base netting < 0 is zeroed, counted, surfaced | counted | the one place `net-base` discards level mass |
| G18 | `net-base`: a base with net > 0 has `Σ positive > 0` | assert | true by arithmetic; asserted against refactors |
| G15 | `renorm`: `min_c |D(c)| ≥ 0.5` | 0.5 | `D(c)` crossing zero flips the sign of every node at once |

**A mapped node's `shape_i(c) < 0` in individual cells, with a healthy block
mean, is admissible and not clipped.** It is measured reverse flow — CLAUDE.md
records 13,318 cells across 368 substations with negative `min_load`, kept
as-is. Per-cell conservation is unaffected (the slack absorbs whatever `M(c)`
is) and exactness depends only on the weighted mean. Counted and reported;
`--shape-floor` (default off) clips before normalization for a caller who wants
it.

---

## 7. Outputs

`data/processed/load_projection/approach3/<tag>/`, tag
`approach3__{shape}__{conserve}__{negative}[__sib{sigma}][__my{Y}_wy{W}]`.

| file | columns |
|---|---|
| `nodal_hourly.csv.gz` | `datetime_pst, hour_of_year, z0001 … zNNNN` (MW at `--decimals`) |
| `node_index.csv` | `column, node_id, base_id, subname, winter_load, summer_load, participating, mapped, n_substations_mapped, shape_source` — in column order. The **only** link from `z0001` back to reality, and the only record of a zeroed node's original level |
| `node_energy_check.csv` | `node_id, block, level, level_share_target, energy_share_realized, abs_dev` — the G10 evidence, and the substitute report under `renorm` |
| `node_shape.csv.gz` | `node_id, month, hour_pst, shape, share` — lets anyone recompute the hourly file as `y(t)·share(cell(t))` |
| `slack_diagnostics.csv` | `cell, month, hour_pst, block, target_energy_mwh, n_hours, M, R, lambda_U, shape_U` — where every guard number comes from |
| `mapping_report.csv` | every edge with `status`, plus `n_siblings, n_siblings_participating, zeroed_level_mass` per base |
| `node_annual_peak.csv` | `node_id, annual_mwh_{float,printed}, peak_mw_{float,printed}, peak_month, peak_hour_pst, min_mw, n_hours_negative` |
| `manifest.json` | input md5s, every axis, **every guard threshold AND its realized value**, per-block `Λ_M/Λ_U/A(b)/min shape_U`, target provenance, the run command, and the interpretation notes |
| `summary.csv` | one row of headline numbers |

### Column naming

Columns are **positional** safe names in node-table order — deliberately not
sorted, because `sorted()` on arbitrary strings is locale-unstable and would
silently reorder the caller's own ordering. Positional naming is collision-free
by construction, so no symbol, unicode codepoint, comma, quote, newline or
leading digit can break the CSV (T5 exercises all of them).

Contrast the CATS convention `Demand_MW_z{bus_i}`, which embeds the real id and
is parsed back with `int(...)` at `deliverable_numbers.py:307`: that works only
for integer ids, which is exactly why arbitrary ids need an index file instead.
**Do not reuse `deliverable_numbers.py`'s parsing for Approach 3.**

### The one change outside the new module

`round_to_printed` (`genx/genx_demand_io.py`) gained
`allow_negative` (**default `False` = bit-for-bit the historical behaviour**) and
an `abs(deficit) <= n_cols` assertion. Its floor/remainder arithmetic was already
sign-agnostic, but the reclaim branch refused to take a unit below zero
(`if out[r, j] >= 1`), so an **all-negative row** needing a reclaim raises
`could not reclaim`, and even when it succeeds it biases every downward
adjustment onto the positive columns. Negative node-hours are reachable in
Approach 3 even with negative *levels* zeroed, because envelope shapes go
negative in measured reverse-flow cells.

The default must stay bit-for-bit: `read_demand` rejects negative input anyway,
the GenX outputs are published, and `expand_template.py` carries a copy of this
function that `deliverables/test_compact.py` checks against the original **every
run** (verified passing after the change).

---

## 8. Run commands

```bash
# generic target
python scripts/load_projection/approach3/build_proportional_nodal.py \
    --nodes nodes.csv --mapping mapping.csv \
    --target csv --target-csv target.csv

# RESOLVE 2035, weather year 2012
python scripts/load_projection/approach3/build_proportional_nodal.py \
    --nodes nodes.csv --mapping mapping.csv \
    --target resolve --year 2035 --weather-year 2012

# guards + the k_n cancellation property
python scripts/load_projection/approach3/test_approach3.py

# every quoted number, sectioned A-G
python scripts/load_projection/approach3/doc_numbers.py --run <tag>
# which combinations are feasible -- CHOOSE THE DEFAULT FROM THIS
python scripts/load_projection/approach3/doc_numbers.py --run <tag> --sections C
```

**Refresh `doc_numbers.py` before editing any measured number in this file.**

---

## 9. CATS back-test — scope and caveats

Not yet built; specified here so it is cheap and so its numbers cannot travel
without their caveats. It rests on artifacts that already exist:

- **Edge list, free:** `data/checks/build_identity_catchment_maps/CATS/`
  `identity_pairs.csv` is exactly the exact-match subset — **1,242 rows**,
  `utility, substation_name, node, dist_km`. Rename `node` → `base_id`, drop
  `dist_km`.
- **Node table, one small script:** from the GenX control tree's **Winter** and
  **Summer** `Demand_data.csv`, two siblings per bus at local noon and 15:00,
  `base_id = bus_i`, levels = the mean demand at that local hour over the week's
  7 days. Resolve local hour → `Time_Index` with `load_rep_week_calendar()`,
  **not** by assuming `Time_Index 1` is midnight PST: Winter is PST but Summer
  is PDT, so local noon sits at a different PST hour in the two weeks. Scale:
  ~2,467 loaded bases × 2 siblings ≈ 4,934 nodes, 1,242 mapped bases.

**What it legitimately measures:** `Λ_M`, `Λ_U` and `A(b)` at real scale;
G9/G10/G11 at ~4,934 nodes rather than on a fixture; the subname axis end to
end; and, holding the level fixed, how much the shape layer moves load.

**What it does not measure — print these above any number:**

1. **Not an unbiased "cost of coordinate-freeness."** Three things change at
   once versus county-first: no coordinates/counties/ReEDS, levels from CATS's
   own demand instead of envelopes + ReEDS county shares, and a synthetic
   subname split.
2. **Partly circular.** The levels come from CATS and CATS is the natural
   baseline, so Approach 3 scores well for reasons unrelated to the method. This
   is the trap already flagged for `catsprop` — "comparing the package against
   CATS is not independent for that half" — only stronger, because the levels
   are *entirely* CATS's. Compare against
   **`countyfirst_envelope_equalsplit__prox`**, shipped precisely for
   independent comparison.
3. **No coverage of the negative-level path.** CATS demand is non-negative
   (`read_demand` rejects otherwise), so `net-base`, `participate` and G17 get
   none. They stay on the synthetic fixture (T8, T12).
4. **The two siblings are near-duplicates.** Noon and 15:00 are two points on
   one diurnal curve; real subnames will not be.
