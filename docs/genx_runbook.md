# GenX experiment runbook

Every command, in order, plus where each output lands. Methodology lives in
[`genx_rescale.md`](genx_rescale.md) (how the demand sets are built) and
[`genx_comparison.md`](genx_comparison.md) (how runs are compared). Every
measured number quoted below is recomputable with
`python scripts/load_projection/genx/doc_numbers.py`.

---

## The 25 allocations

One control plus 24 treatments in three families and a map axis. All hold
statewide demand fixed in every hour; they differ only in which bus carries it.
**25 runs × 8 cases = 200 GenX jobs** (weather years 2011 = highest VRE and
2012 = lowest, × 4 seasons: `p5,p6,p12,p13,p19,p20,p26,p27`).

### Control

| Run tag | What it is |
|---|---|
| `genx__control` | **CATS as shipped.** The baseline, taken as the true allocation. Byte-identical to the original scenario tree. |

### ReEDS county-first — county totals are *imposed*

ReEDS sets each county's share of the statewide total (normalized weights only
— levels cancel); within a county, α is the share handed to the uncovered
buses as an equal split, the rest going to substation buses by max-load
envelope.

| Run tag | What it is |
|---|---|
| `genx__reedsco__prox__aratio__static` | **Method 1 (broad).** α = u/n; every candidate bus loaded. |
| `genx__reedsco__prox__a0__static` | **Method 2 (narrow).** α = 0; substation buses only. |
| `genx__reedsco__prox__aratio__monthhour` | Method 1 with the envelope resolved **per (month, hour_pst) cell**. |
| `genx__reedsco__prox__a0__monthhour` | Method 2, same month-hour weighting. |

### Stochastic pool-and-redistribute — county totals *emerge*

A pool of load is swept off the network and dealt back out in proportion to
Approach 2's **per-cell** output (the CATS-calibrated run, F\* = 0.841;
`--level monthhour` is mandatory — a static stochastic run is a null result by
construction). Each design runs four times: mean of 5 draws + single draws
0, 1, 2.

| Run tag | What it is |
|---|---|
| `genx__stoch__prox__w1g30top-{mean,d0,d1,d2}__monthhour` | **Way 1 (broad), top-off ON.** Counties at ≥30% substation coverage (30 counties) have every bus swept; swept uncovered buses stay alive on an equal split of β = 0.580 of the pool. |
| `genx__stoch__prox__w1g30-{mean,d0,d1,d2}__monthhour` | **Way 1 (broad), top-off OFF.** Same sweep; swept uncovered buses go to zero. |
| `genx__stoch__prox__w2-{mean,d0,d1,d2}__monthhour` | **Way 2 (narrow).** No county gated; only substation buses swept and re-dealt. |

### Envelope hold — minimum intervention

| Run tag | What it is |
|---|---|
| `genx__env__prox__hold__static` | Re-splits *only* the control load on covered buses, among those buses, by the substations' own measured envelopes. No ReEDS, no projection model. Replaced the retired ReEDS substation-first hold. |
| `genx__env__prox__hold__monthhour` | Same pool, per-cell envelope split. |

### Map axis — same allocations, different substation→bus artifact

Built once by `scripts/load_projection/nodal/build_identity_catchment_maps.py`
(identity match rate 1,077/1,336 substations = 80.6%; catchment LP integral,
~1 s; see `genx_rescale.md` "The map axis").

| Run tag | What it is |
|---|---|
| `genx__reedsco__nameprox__aratio__monthhour` | County-first under the identity-first map (median assignment distance 0.065 km vs 0.133 km prox). |
| `genx__reedsco__catch__aratio__monthhour` | County-first under LP catchments — every candidate bus loaded; α nearly collapses (mean 0.013 vs 0.707 under prox; equal pool 3.8% vs 65.5% of state energy), so `aratio` ≈ `a0` here. |
| `genx__reedsco__namecatch__aratio__monthhour` | County-first, identity matches forced into the LP. |
| `genx__stoch__nameprox__w1g30top-mean__monthhour` | Stochastic Way 1 under the identity-first map. |
| `genx__stoch__catch__w2-mean__monthhour` | Stochastic under LP catchments — the gate is moot (all buses covered), so tagged w2. |
| `genx__stoch__namecatch__w2-mean__monthhour` | Stochastic, identity + catchments. |

**Why the map axis is applied to these runs and not others.** The map is
relevant to *every* family — it sets which buses count as covered and how each
substation's weight lands on buses — so this is a sampling decision, not a
statement that it does not matter elsewhere. One representative per family is
carried (county-first `aratio`; stochastic `mean`) to hold the sweep at 200
solves. Two consequences worth knowing:

- Adding `a0` under the catchment maps would be nearly redundant, since α almost
  collapses there and `aratio` ≈ `a0` (see below).
- **`env hold` currently has no map variants** — a genuine gap, not a
  structural exemption. The map would change its pool, and the cheapest way to
  close it is `--weights env --level monthhour --map namecatch`, which would
  make the hold pair map-comparable with `stoch`.

> **TODO — the full map × allocation cross is runnable but deliberately not
> run.** Every allocation accepts every `--map` value, so the complete cross is
> ~60 runs (≈480 solves). We run a 25-run slice instead, because the map axis is
> a secondary question and solver time is the binding constraint. If the
> dispatch results show the map mattering as much as the weighting method,
> expand the slice — starting with `env` under all four maps and `a0` under
> `nameprox`, which are the two cheapest gaps to close.

### What each comparison isolates

- **Any treatment vs control** — the full effect of replacing CATS's allocation.
- **`aratio` vs `a0`** — within-county bus placement *only*: county totals
  identical by construction.
- **`static` vs `monthhour`** (reedsco, env) — whether *temporal* load detail
  matters once spatial detail is held fixed.
- **ReEDS family vs stochastic family** — simple (imposed county totals) vs
  complicated (emergent) input, the headline question.
- **`stoch w2` vs `env hold`** — nearly identical pools, so this isolates the
  *weighting method* (stochastic model vs measured envelope) with scope fixed.
- **`mean` vs `d0/d1/d2`** — whether the stochastic layer's own spread is large
  enough to matter downstream.
- **`prox` vs `nameprox` vs `catch` vs `namecatch`** — whether the
  substation→bus *mapping method* matters as much as the weighting method.
  Catchment maps also dissolve the coverage problem (every candidate bus stays
  loaded), removing the β/α equal-split machinery entirely.

Candidate buses (3,778; 3,769 inside a county polygon): all 3,168
`Type='Substation'` non-IMPORT buses, plus the **610 `AddedNode` buses CATS
itself loads** (they carry 15.9% of state load). The **5,089 zero-load
AddedNodes are excluded** — pure routing points.

---

## 0. One-time setup

```bash
julia -e 'using Pkg; Pkg.add(["GenX", "Gurobi"])'
gurobi_cl --license          # confirm the license is found
```

Verified present on this machine: Julia 1.12.6, Gurobi 13.0.1 (academic license
to 2027-03-02). GenX.jl and Gurobi.jl were **not** installed as of 2026-08-12.

## 1. Prerequisite artifacts — *already done*

```bash
# CATS statewide hourly target (672 h) for recalibrating F*
python scripts/load_projection/genx/build_cats_target.py

# Approach 2, CATS-calibrated, 5 draws, WITH the per-cell table
python scripts/load_projection/approach2/generate_stochastic.py \
    --family normal --n-draws 5 \
    --target data/processed/load_projection/cats_caiso_target.csv \
    --calibrate-on target --save-cells --validate

# the three alternative substation->bus maps (identity + LP catchments)
python scripts/load_projection/nodal/build_identity_catchment_maps.py
```

## 2. Build the demand sets — *already done*

Run only to regenerate from scratch. Each writes `genx/rescaled/<tag>/`.

```bash
R=scripts/load_projection/genx/rescale_genx_demand.py
python $R --weights control
python $R --weights reedsco --alpha ratio
python $R --weights reedsco --alpha 0
python $R --weights reedsco --alpha ratio --level monthhour
python $R --weights reedsco --alpha 0     --level monthhour
python $R --weights env
python $R --weights env --level monthhour

# stochastic: 3 designs x 4 draw treatments, ALWAYS per-cell
for D in mean 0 1 2; do
  python $R --weights stoch --level monthhour --stoch-gate 0.30 --stoch-topoff equal --draw $D
  python $R --weights stoch --level monthhour --stoch-gate 0.30 --stoch-topoff none  --draw $D
  python $R --weights stoch --level monthhour --stoch-gate 2.0  --stoch-topoff none  --draw $D
done

# map axis
python $R --weights reedsco --alpha ratio --level monthhour --map nameprox
python $R --weights reedsco --alpha ratio --level monthhour --map catch
python $R --weights reedsco --alpha ratio --level monthhour --map namecatch
python $R --weights stoch --level monthhour --map nameprox  --stoch-gate 0.30 --stoch-topoff equal --draw mean
python $R --weights stoch --level monthhour --map catch     --stoch-gate 2.0 --stoch-topoff none --draw mean
python $R --weights stoch --level monthhour --map namecatch --stoch-gate 2.0 --stoch-topoff none --draw mean
```

## 3. Check the inputs before spending solver time — *already done*

```bash
python scripts/load_projection/genx/test_genx_rescale.py     # guards
python scripts/load_projection/genx/compare_genx_demand.py --no-figures
python scripts/load_projection/genx/doc_numbers.py           # audit every quoted number
```

## 4. Expand into runnable cases

`--enable-outputs` turns on the result writers the comparison needs (NSE,
curtailment, commitment, capacity factor). **Apply it to every run including the
control**, or the runs stop being like-for-like.

```bash
for TAG in $(ls genx/rescaled); do
  python scripts/load_projection/genx/materialize_genx_cases.py \
      --run-tag "$TAG" --cases p5,p6,p12,p13,p19,p20,p26,p27 --enable-outputs --force
done
```

## 5a. Solve locally

200 solves = 25 allocations × 8 cases, strictly serial. Start with one case to
calibrate runtime and memory before committing.

```bash
python scripts/load_projection/genx/run_genx_local.py --dry-run
python scripts/load_projection/genx/run_genx_local.py --runs genx__control --cases p5
python scripts/load_projection/genx/run_genx_local.py
```

Useful flags: `--project <julia env>` if GenX is not in the default environment,
`--threads N`, `--timeout <seconds>` (default 6 h), `--force` to redo cases that
already have results. Interrupting is safe: cases with a `results/` folder are
skipped on the next invocation.

## 5b. Cluster handoff

> **⚠️ Check this first — it is the one thing that can waste every job.** All 200
> `jobscript.sh` files carry `julia --project="/global/scratch/users/manocha/GenX.jl"`.
> If GenX.jl lives anywhere else on the cluster account actually running them,
> **all 200 jobs fail instantly**. Confirm the path with whoever runs them and, if
> it differs, regenerate with
> `prep_cluster_upload.py --project <their GenX.jl path>` (seconds — it only
> rewrites the jobscripts and the manifest, not the demand data).

**Pre-submission verification** (all confirmed 2026-08-17):

| Check | Status |
|---|---|
| Cases materialized | 200 = 25 runs × 8 cases, 1.90 GB |
| Required files present in every case (`Run.jl`, `system/*`, `settings/*`, `jobscript.sh`) | ✅ none missing |
| Unique SLURM job names | ✅ 200 / 200 |
| Manifest `demand_md5` vs the file on disk | ✅ 0 mismatches |
| Materialized demand vs its rescale-run source | ✅ 0 md5 drift |
| Diagnostics on in **every** case incl. control (`WriteNSE`/`WriteCurtailment`/`WriteCommit`/`WriteCapacityFactor`) | ✅ 200 / 200 `true` |
| Prices written (`WriteShadowPrices: 1`, in `genx_settings.yml`) | ✅ 200 / 200 |
| Solver settings uniform (`UCommit: 2`, `DC_OPF: 1`, `NetworkExpansion: 0`) | ✅ 200 / 200 |
| Control demand byte-identical to the shipped tree | ✅ all 8 cases |
| Hourly statewide conservation | ✅ 0.0 MW printed deviation, 25 runs × 4 seasons |

Note `WriteShadowPrices` lives in `genx_settings.yml`, **not** `output_settings.yml`
— it will not appear if you grep only the latter.


`prep_cluster_upload.py` rewrites each case's `jobscript.sh` with a **unique**
`--job-name` and log names (the shipped copies all share `testing_with_monitor`
and would overwrite each other's logs), keeps the CPU/memory monitor loop, and
writes the Excel manifest describing the upload.

```bash
python scripts/load_projection/genx/prep_cluster_upload.py \
    --project /global/scratch/users/<her>/GenX.jl \
    --account <acct> --partition <part> --mail-user <her email>
```

On the cluster, from the directory holding the run folders:

```bash
bash submit_all.sh                 # all 200
bash submit_all.sh genx__control   # or filter to one run
```

A job-array form is also available from `run_genx_local.py --emit-slurm`; its
paths are absolute, so regenerate it on the cluster.

## 6. Compare the results

```bash
python scripts/load_projection/genx/compare_genx_results.py --inspect   # do this FIRST
python scripts/load_projection/genx/compare_genx_results.py
```

`--inspect` prints each result file's shape and label column. The time-series
parsing follows GenX's documented convention but has never been checked against
output from this build, so confirm it before trusting the numbers.

---

## Where everything lands

| Path | Contents |
|---|---|
| `genx/scenarios_rescaled/genx_upload_manifest.xlsx` | **The sheet for the cluster handoff** — sheets: `runs`, `cases`, `slurm_settings` |
| `genx/scenarios_rescaled/submit_all.sh` | Submits every prepared case; takes an optional run-tag filter |
| `genx/rescaled/<tag>/Demand_data__{season}.csv` | The rescaled demand, in the control's exact layout |
| `genx/rescaled/<tag>/node_shares.csv` | Per-bus share and its provenance (per bus-cell for `monthhour`) |
| `genx/rescaled/<tag>/county_allocation.csv` | Per-county share, α, bus counts (county-first runs) |
| `genx/rescaled/<tag>/pool_counties.csv` | Per-county coverage/sweep detail (stochastic runs) |
| `genx/rescaled/<tag>/manifest.json` | Axes, source md5s, conservation + coverage checks |
| `genx/scenarios_rescaled/<tag>/p*/` | Runnable GenX cases |
| `genx/scenarios_rescaled/<tag>/p*/results/` | **GenX output** — costs, power, prices, flow, charge, nse, curtail, commit |
| `data/processed/load_projection/nodal/CATS/substation_node_map__{nameprox,catchment,namecatchment}.csv` | The cached map artifacts |
| `data/checks/build_identity_catchment_maps/` | Identity pairs, map summary, LP stats |
| `data/checks/genx_rescale/demand_comparison_summary.csv` | Input-side divergence per run × season |
| `data/checks/genx_rescale/demand_{bus,county}_deltas.csv` | Per-bus / per-county load change vs control |
| `data/checks/genx_rescale/local_run_log.csv` | Per case: status and wall seconds |
| `data/checks/genx_rescale/results_comparison_summary.csv` | Output-side metrics (after step 6) |
| `data/figures/genx/` | Δload maps, per-bus scatters, county bars, statewide profile checks |

## Sanity numbers to expect

Recompute any of these with `doc_numbers.py` (section letters in parentheses).

| Check | Value |
|---|---|
| Hourly statewide conservation (G) | exact — 0.0 MW printed deviation, every run × season |
| Control passthrough (A/G) | byte-identical to the shipped scenario tree |
| Candidate buses (B) | 3,778 = 3,168 substations + 610 loaded AddedNodes; 3,769 inside a county |
| Zero-load AddedNodes excluded (B) | 5,089 |
| Control-loaded buses (I) | 2,471 = 1,861 Substation + 610 AddedNode; **1,307 pool buses sit at zero in the control** |
| Energy on loaded AddedNodes (I) | 15.9% |
| Energy a hold re-allocates / leaves untouched (I) | 53.8% / **46.2%** (the coverage gap) |
| Energy outside the candidate pool (I) | 0.109% (4 buses, outside every county polygon) |
| F\* on the CATS target (E) | 0.841 (vs 0.7361 on EIA-930 history) |
| Stochastic per-cell fallback / clipping (E) | 48 / 160,920 slots filled (0.03%); 1,337 negative cell means clipped (0.83%) |
| Envelope month-hour fallback (D) | 72 / 387,936 slots (0.02%); only Fall touched (24 slots, 0.07%) |
| Identity map match rate (F) | 1,077 / 1,336 substations (80.6%) → 1,242 buses |
| Catchment LP (F) | 3,769 buses assigned, 0 fractional, ~1 s, Σdist 28,192 km |
| County shares vs ReEDS target | within 5×10⁻⁹, in every month-hour cell |
| Stochastic sweep (prox, gate 0.30) (G) | 30 counties gated; 2,442 buses swept (1,026 substation + 1,416 uncovered); β = 0.580 |
| Draw-to-draw spread (E) | substation share cv median 3.1% (p90 6.3%); bus relocation moves ≤0.06 pp between draws |
| Energy relocated, bus level (H) | reedsco `aratio` 49.7% / `a0` 58.6–58.7%; stoch `w1g30top` 43.8% / `w1g30` 47.8% / `w2` 20.5%; env hold 20.3–20.7%; map variants 45.6–50.7% |
| Energy relocated, county level (H) | **14.41% pinned for every county-first run (all maps)**; stochastic emerges 7.7–17.8%; env hold 7.7–8.3% |
| Jobs prepared for cluster | 200 = 25 runs × 8 cases, 1.90 GB, unique SLURM job names |
