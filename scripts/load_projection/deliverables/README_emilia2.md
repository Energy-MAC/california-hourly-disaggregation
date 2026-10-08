# Hourly load at sub-nodes — coordinate-free allocation

Hourly electricity demand at each **bus-list bus sub-node** in the supplied bus list,
for one RESOLVE model year × weather year. 100% of California's net electricity
demand is allocated across the sub-nodes, and the statewide total is reproduced
**exactly every hour**.

Only `numpy` and `pandas` are needed. No access to the originating repository is
required to use the data; `code/` is included so you can re-run it.

---

## Quick start

This package ships **40 datasets** — 5 model years × 8 weather years — as the
allocation once plus one statewide series per combination, with `code/expand.py`
to rebuild any hourly file. That is **5.9 MB** instead of the ~7 GB the same grid
costs fully expanded.

```bash
python code/expand.py --list                                 # what is available
python code/expand.py --variant envelope --series y2035_wy2012
python code/expand.py --all --out-dir hourly/                # CHECK YOUR DISK
```

That writes `nodal_hourly/<variant>__<series>.csv.gz`. Series labels are
`y<model year>_wy<weather year>`; model years are **2026, 2030, 2035, 2040,
2045** and weather years **2007–2014**.

Then:

```python
import pandas as pd

df = pd.read_csv("nodal_hourly/envelope__y2035_wy2012.csv.gz")
nodes = pd.read_csv("reference/node_index.csv")
df = df.rename(columns=dict(zip(nodes.column, nodes.node_id)))
```

`--check` also reports the per-cell share residual. `summary/summary.csv` is the
one-row-per-dataset headline table (TWh, peak, conservation error).

Only `numpy` and `pandas` are needed, and `code/` is self-contained — it imports
nothing from the repository that produced the package.

### Why it ships `env` and not the share matrix

Expansion is **exact, not approximate**: a rebuilt file is byte-for-byte
identical to one written directly (verified).

Some packages in this family can ship one share matrix for a whole grid because
their allocation does not depend on the year. **Approach 3's does**, because the
shape normalization is weighted by the statewide energy in each month-hour cell:

```
shape_n(c) = env_n(c) × E(b) / Σ_{c' in b} Y(c') × env_n(c')
```

Measured, the shape moves up to **8.2e-04** between weather years and **1.4e-03**
between model years. So instead the package ships the parts that genuinely do not
depend on the year — the measured envelope surface `env` and the level shares —
and `expand.py` recomputes the normalization from each statewide series by
calling the **same** functions the build called (`code/load_projection/` is a
verbatim copy). Exactness is by construction rather than by assumption.

## What this is

Two separate things combine:

1. **The level** — each sub-node's share of statewide load, taken directly from
   the `summer_load` / `winter_load` columns of the supplied bus list. These are
   treated as *dimensionless weights*: only their ratios are used, and the
   absolute scale is discarded. The MW level comes entirely from the statewide
   forecast.
2. **The shape** — the hour-to-hour pattern. Where a bus could be matched
   to a metered California utility substation, that substation's own measured
   month-by-hour load envelope supplies the pattern. Where it could not, the bus
   carries the statewide pattern instead.

Writing `Y(c)` for the statewide energy in month-hour cell `c`, and splitting the
year into two half-year blocks (**MayOct** = months 5–10, **NovApr** = 11–4):

```
sub-node load(t) = statewide(t) × share(cell of t)
share(c)         = level_share × shape(c),   shape normalized per block
```

### Two guarantees

- **The statewide total is reproduced exactly every hour**, in floating point
  and at the printed one-decimal precision. Measured worst-case deviation in
  this package: **1.5e-11 MW**. `reference/statewide_total.csv.gz` is the hourly
  sum of every column, so you can verify this yourself.
- **Each sub-node receives its stated share of each half-year's energy.** Worst
  deviation across all 40 datasets: **2.2e-14 percentage points**. The per-node,
  per-dataset evidence is in `summary/node_energy_check.csv.gz`, which lists the
  target share and the realized share side by side.

A node's **summer:winter energy split is therefore pinned** by the ratio of its
two input levels times the statewide energy in each block. No hourly shape moves
energy across the MayOct/NovApr boundary — that is the only defensible reading of
a two-number-per-node input, but it may not be what you expect.

---

## Read this before using the numbers

### 0. Which nodes carry a measured shape, and which do not

Two different things combine, and the distinction decides how to read the file:

- The **node universe** is every node load is allocated to. It sets each node's
  share of statewide load, from its `summer_load` / `winter_load`.
- The **mapping** is the subset of buses matched to a metered California utility
  substation. Those buses carry that substation's own measured month-by-hour
  pattern. **Every other node carries the statewide pattern**, scaled by its own
  level share.

`reference/node_index.csv` has a `mapped` column, so you can tell them apart
row by row, and `reference/mapping_report.csv` says why each unmapped bus is
unmapped -- distinguishing "the mapping does not cover this bus" from "a match
was attempted and failed".

**100% of load is allocated either way.** What the mapping changes is only the
shape. A mapping covering a small share of buses is not a coverage gap in the
load; it is a smaller share of load whose *hourly shape* is measurement-driven,
and `reference/column_audit.txt` states that share explicitly.

### 1. The bus name and the bus coordinates disagree in the supplied file

The example bus is named `LIVE OAK`, but its coordinates sit **4 m** from PG&E's
metered substation `EL CERRITO G`. The nearest substation actually *called* Live
Oak is **161 km** away in Sutter County. (There are four Live Oaks in
California — PG&E Kern, PG&E Sutter, SCE Los Angeles, SMUD Sacramento — and none
is near these coordinates.)

The two candidate substations are not interchangeable:

| candidate | how it was reached | mean load | peak |
|---|---|---|---|
| `pge / EL CERRITO G` | coordinates, 4 m | 34.89 MW | 53.93 MW |
| `pge / LIVE OAK` | name | 5.14 MW | 15.15 MW |

**This package used the coordinates**, on the reasoning that a bus *name* is
a bus label and need not equal a utility substation name, whereas the coordinate
is a measured location. `reference/column_audit.txt` lists every bus where the
two keys disagree. **Please check that list** — if a name is in fact the
authoritative key for your bus list, the package must be rebuilt with
`--match name`, and the shapes will change.

### 2. Negative sub-node loads

Sub-node `EE` carries a negative load (summer −7.74, winter −1.85), which reads
as net generation rather than demand. The default handling is `net-base`:

- the **bus** is allocated its signed sibling **net** (winter 96.40, not the
  positive-only 98.25), and
- that net is split among the bus's **positive** sub-nodes only, so the negative
  sub-node is written as literal `0.0`.

Its original level is preserved in `node_index.csv` (`winter_load`,
`summer_load`), so nothing is lost — but **a negative sub-node carries no load in
this dataset.** If you want those buses to carry negative demand instead, rebuild
with `--negative-nodes participate`.

### 3. With a single bus the hourly shape has no effect

This is a property of the method, not a bug, and it matters for interpreting the
example package. All sub-nodes of one bus share one shape. When *every* bus in
the file is mapped and there is only one of them, the shape divides out exactly
and the result is simply the statewide series × each sub-node's level share. The
measured within-block variation of each sub-node's share is **0.0**.

So the example package demonstrates the plumbing, the conservation guarantee and
the negative-load handling — but **not** the shape layer. A bus list with several
buses matched to *different* substations is needed for that, and is the case the
method is built for.

### 4. No coordinates were used in the allocation itself

The allocation is proportional to the supplied levels. It does **not** use a
county layer, ReEDS county shares, or a coordinate-based nodal map, because none
of those is available for an arbitrary bus list. Coordinates were used for
one thing only: deciding which metered substation lends each bus its hourly
shape. Consequently there is **no county-level or regional validation** of this
dataset — the guarantees are the two identities above, not an accuracy claim.

### 5. Say it precisely

100% of load is always allocated. What the substation matching changes is the
**share of load whose hourly shape is measurement-driven**. Those are two
different statements and the difference is easy to misread.

---

## Files

| path | what it is |
|---|---|
| `shares/<variant>.npz` | the allocation: the target-invariant envelope surface `env`, the level shares, which nodes are mapped, and the column names |
| `statewide/y<my>_wy<wy>.csv.gz` | one statewide series per combination — `datetime_pst, hour_of_year, cell, y_mw`. Also the conservation target |
| `code/expand.py` | rebuilds any hourly file; `--list`, `--all`, `--check` |
| `code/load_projection/` | verbatim copy of the allocation modules `expand.py` calls |
| `code/ingest_node_table.py` | the wide-bus-list → two-file converter, with `--audit` |
| `nodal_hourly/` | hourly files, once you expand them (empty on arrival) |
| `reference/node_index.csv` | **column → node_id**, plus `base_id` (the bus), `subname` (the `'ID'` value), the original levels, and whether each node participates and is mapped |
| `reference/nodes.csv` | the ingested sub-node table the allocation used |
| `reference/mapping.csv` | the bus → substation edges actually used |
| `reference/column_audit.txt` | which input columns were needed, and **every bus where name and coordinates disagree** |
| `reference/mapping_report.csv` | every edge with its resolution status |
| `reference/source__*.csv` | the input bus list, unmodified |
| `summary/summary.csv` | one row per dataset: TWh, peak, node counts, worst conservation and energy-share error |
| `summary/node_energy_check.csv.gz` | per node per block per dataset: target vs realized energy share |
| `manifest.json` | every input checksum, every setting, the grid definition, and the guard thresholds `expand.py` reads |

Variant tags read `emilia2__<shape source>__<conservation>__<negative handling>`.

---

## Rebuilding

```bash
# one file: the universe, with name/lat/long blank where unmapped
python build_emilia2_package.py --input <universe>.csv --expect-buses <N>

# two files: the universe (levels alone are enough) plus a smaller match file
python build_emilia2_package.py --input <universe>.csv \
    --mapping-input <mapping>.csv --expect-buses <N>
```

Either form builds the default 5 × 8 grid. Narrow it with
`--period 2030,2040 --weather-years 2010-2012`, or take one combination with
`--year 2035 --weather-year 2012`.

**`--input` must be the full node universe, not the mapping.** A node absent
from it receives no load at all, so passing a mapping that covers 1 of 2,000
buses would put all of California's load on that one bus. `--expect-buses N`
refuses unless the universe has exactly N buses, and a bus present in the
mapping but absent from the universe is refused outright.

Options worth knowing:

| flag | default | effect |
|---|---|---|
| `--mapping-input` | | a separate, smaller match file joined on by bus number |
| `--expect-buses` | | refuse unless the universe has exactly this many buses |
| `--match` | `coords` | `name` joins on the bus name instead (see caveat 1) |
| `--negative-nodes` | `net-base` | `participate` lets a negative sub-node carry negative load |
| `--conserve` | `auto` | `slack` keeps seasonal energy exact but needs some unmapped buses; `renorm` works without them |
| `--shape-sources` | `envelope` | `stoch` uses the stochastic model's pattern; `flat` uses no pattern at all |
| `--period` | `2026,2030,2035,2040,2045` | RESOLVE model years (crossed with the weather years) |
| `--weather-years` | `2007-2014` | ranges and comma lists both work |
| `--format` | `compact` | `hourly` writes every file at build time (~30× the disk) |
| `--zero-sources` | — | comma list matched against the input `source` column; matching nodes carry zero load |
| `--zero-unmapped` | off | zero every bus with no substation match |

---

## Provenance

| layer | source |
|---|---|
| Statewide level and hourly shape | RESOLVE, model year 2035, weather year 2012, Baseline plus all additive overlays, net of behind-the-meter PV |
| Sub-node level shares | the supplied bus list's `summer_load` / `winter_load` |
| Sub-node hourly shape | metered PG&E / SCE / SDG&E substation month-by-hour load envelopes |
| Bus → substation match | the bus list's own coordinates |
| Method | Approach 3 — coordinate-free proportional disaggregation |

**Measured targets** (CA-wide, net of BTM), the range across the grid:

| model year | TWh | peak MW |
|---|---|---|
| 2026 | 276.9 – 279.1 | 57,968 – 62,252 |
| 2030 | 334.9 – 337.4 | 65,997 – 70,087 |
| 2035 | 401.4 – 404.1 | 74,470 – 80,463 |
| 2040 | 443.5 – 446.3 | 81,133 – 87,828 |
| 2045 | 467.9 – 470.9 | 84,740 – 92,334 |

Per-combination figures are in `summary/summary.csv`.
