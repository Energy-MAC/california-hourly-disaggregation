"""Approach 3 over a GRID of (model year x weather year), in the compact format.

A snapshot list of model years crossed with a list of weather years. Writes the
allocation ONCE plus one statewide series per combination, with `expand.py` to
reconstruct any hourly file exactly -- megabytes instead of gigabytes.

    python scripts/load_projection/approach3/build_proportional_grid.py \\
        --nodes nodes.csv --mapping mapping.csv \\
        --period 2026,2030,2035,2040,2045 --weather-years 2007-2014

WHAT MAKES THIS FAST
--------------------
Only the cheap part of the pipeline repeats per combination:

  once overall   read the node table, resolve levels, read the mapping, build
                 the raw envelope surface, composite it per base  (the
                 expensive reads)
  once per MODEL YEAR   `build_target` -- the RESOLVE assembly, which is the
                 slowest single step, so it runs 5 times, not 40
  per combination  `energy_normalize` + `node_shares` on a 288 x n_nodes array,
                 and writing one 8,760-row statewide series

No hourly node file is written at build time at all. `--format hourly` is
available but costs ~30x the disk.

WHY `env` IS SHIPPED AND NOT `S`
--------------------------------
Approach 3's share matrix is NOT invariant to the model year or the weather
year, unlike the CATS county-first package's. The shape normalization is
target-energy weighted:

    shape_n(c) = env_n(c) * E(b) / sum_{c' in b} Y(c') * env_n(c')

`# VERIFIED` 2026-10-08: `shape` moves up to **8.2e-04** between weather years
(same model year) and **1.4e-03** between model years (same weather year). So
shipping one `S` for the whole grid would NOT be exact, and the CATS package's
invariance claim must NOT be carried over.

What IS target-invariant is `env` (the measured envelope surface) and `l` (the
level shares). The package ships those once; `expand.py` recomputes the
normalization and the shares from each statewide series by calling the SAME
`proportional.py` functions, so expansion is exact by construction.

A pure RESCALING of the target would cancel in the normalizer, which is why only
the cell-to-cell SHAPE of `Y(c)` matters -- that is what the weather year and the
overlay mix change.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/deliverables"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from load_projection import cells as C  # noqa: E402
from load_projection import proportional as P  # noqa: E402
from load_projection.periods import build_plan  # noqa: E402
from genx_demand_io import round_to_printed  # noqa: E402

import build_proportional_nodal as BN  # noqa: E402

EXPAND_SRC = Path(__file__).with_name("expand_a3_template.py")
BLOCKS = C.HALFYEAR_LABELS

#: docstring for the shipped package's __init__, so `expand.py` can say where
#: its modules came from without the reader having to guess.
INIT_DOC = (
    '"""Allocation modules copied verbatim from the repo that built this\n'
    'package, so expand.py runs the SAME code the build ran."""\n'
)



def expand_year_spec(spec: str) -> str:
    """`2007-2014` -> `2007,2008,...,2014`. `periods` takes only lists."""
    out = []
    for tok in str(spec).split(","):
        tok = tok.strip()
        if "-" in tok and not tok.startswith("-"):
            a, b = tok.split("-", 1)
            if a.strip().isdigit() and b.strip().isdigit():
                lo, hi = int(a), int(b)
                if hi < lo:
                    raise SystemExit(f"--weather-years {tok!r} runs backwards")
                out += [str(v) for v in range(lo, hi + 1)]
                continue
        out.append(tok)
    return ",".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nodes", required=True)
    ap.add_argument("--mapping", required=True)
    ap.add_argument("--period", default="2035",
                    help="comma list of model years (snapshots, crossed with "
                         "the weather years)")
    ap.add_argument("--weather-years", default="2012",
                    help="comma list and/or ranges, e.g. 2007-2014")
    ap.add_argument("--load-basis", choices=["net", "gross"], default="net")
    ap.add_argument("--overlays", choices=["all", "none"], default="all")
    ap.add_argument("--shape-sources", default="envelope")
    ap.add_argument("--shape-col", default="avg_load")
    ap.add_argument("--shape-common", choices=["keep", "strip"], default="keep")
    ap.add_argument("--conserve", default="auto",
                    choices=["auto", "slack", "renorm"])
    ap.add_argument("--negative-nodes", default="net-base",
                    choices=list(P.NEGATIVE_NODE_MODES))
    ap.add_argument("--on-negative-base", choices=["zero", "refuse"],
                    default="zero")
    ap.add_argument("--zero-sources", default="",
                    help="comma list matched case-insensitively against the "
                         "node's `src_source` column; matching nodes carry ZERO "
                         "load (levels kept in node_index.csv). This is how "
                         "'no added nodes should have load' is expressed when "
                         "the bus list marks them in `source`.")
    ap.add_argument("--zero-unmapped", action="store_true",
                    help="give ZERO load to every bus with no substation match. "
                         "Removes the slack basis, so --conserve auto then "
                         "picks renorm and seasonal energy is no longer exact.")
    ap.add_argument("--zero-nodes", default="",
                    help="comma list of node_id values to force to zero load "
                         "(they keep their level in node_index.csv)")
    ap.add_argument("--draw", default="mean")
    ap.add_argument("--min-draws", type=int, default=3)
    ap.add_argument("--stochastic-run", default=None)
    ap.add_argument("--decimals", type=int, default=1)
    ap.add_argument("--format", choices=["compact", "hourly"], default="compact")
    ap.add_argument("--min-shape-net-gross", type=float, default=0.20)
    ap.add_argument("--max-slack-amplification", type=float, default=5.0)
    ap.add_argument("--min-slack-shape", type=float, default=0.0)
    ap.add_argument("--min-renorm-denominator", type=float, default=0.5)
    ap.add_argument("--min-net-gross-total", type=float, default=0.10)
    ap.add_argument("--min-net-gross-unmapped", type=float, default=0.05)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--out", default=str(BN.OUT_PARENT))
    args = ap.parse_args()

    import build_resolve2035_cats_package as B

    guards = P.Guards(args.min_net_gross_total, args.min_net_gross_unmapped,
                      args.max_slack_amplification, args.min_slack_shape,
                      args.min_shape_net_gross, args.min_renorm_denominator,
                      args.on_negative_base)

    # ---- plan -----------------------------------------------------------
    wy = expand_year_spec(args.weather_years)
    plan = build_plan(args.period, wy, B.available_model_years(),
                      B.available_weather_years())
    print(f"plan: {len(plan.model_years)} model year(s) x "
          f"{len(plan.weather_years)} weather year(s) = {len(plan.jobs)} "
          f"combination(s)")
    for n in plan.notes:
        print(f"  note: {n}")

    # ---- target-invariant work, done ONCE -------------------------------
    nodes = P.read_node_table(args.nodes)
    levels, l, ldiag = P.node_level_shares(nodes, args.negative_nodes, guards)
    if args.zero_nodes:
        want = {v.strip() for v in args.zero_nodes.split(",") if v.strip()}
        hit = nodes.node_id.isin(want).to_numpy()
        missing = want - set(nodes.node_id)
        if missing:
            raise SystemExit(f"--zero-nodes names unknown node_id(s) "
                             f"{sorted(missing)[:5]}")
        l[hit, :] = 0.0
        print(f"  --zero-nodes: forced {int(hit.sum())} node(s) to zero load; "
              f"levels preserved in node_index.csv")

    if args.zero_sources:
        if "src_source" not in nodes.columns:
            raise SystemExit(
                "--zero-sources needs a `src_source` column (the ingest "
                "carries the input's `source`). Re-run ingest_node_table.py, "
                "or use --zero-nodes with explicit node_ids.")
        pats = [v.strip().lower() for v in args.zero_sources.split(",")
                if v.strip()]
        src_l = nodes.src_source.astype(str).str.lower()
        hit = np.zeros(len(nodes), dtype=bool)
        for pat in pats:
            hit |= src_l.str.contains(pat, regex=False).to_numpy()
        print("  source inventory: "
              + ", ".join(f"{v}={k}" for v, k
                          in nodes.src_source.value_counts().items()))
        if not hit.any():
            print(f"  WARNING --zero-sources {pats} matched NO node. Nothing "
                  f"was zeroed -- check the source values printed above.")
        else:
            l[hit, :] = 0.0
            print(f"  --zero-sources {pats}: zeroed {int(hit.sum())} node(s) "
                  f"in {nodes.base_id[hit].nunique()} bus(es)")
    elif "src_source" in nodes.columns:
        print("  source inventory: "
              + ", ".join(f"{v}={k}" for v, k
                          in nodes.src_source.value_counts().items())
              + "   (nothing zeroed; pass --zero-sources to exclude some)")

    prof = pd.read_csv(BN.PROFILES, usecols=["utility", "substation_name"])
    prof["utility"] = prof.utility.str.lower()
    edges = P.read_mapping(args.mapping, nodes, prof)
    mapped_bases = set(edges.base_id[edges.status == "ok"])
    # `mapped` is purely "is this node's base in the edge list". A node with
    # l == 0 (zeroed by --negative-nodes, or by --zero-nodes) contributes
    # nothing either way, so it must NOT be folded in here: doing so pushes a
    # zeroed node into the unmapped set and makes the slack basis look like it
    # carries zero level share.
    mapped = nodes.base_id.isin(mapped_bases).to_numpy()
    participating = (l != 0).any(axis=1)
    n_unmapped_bases = nodes.base_id.nunique() - len(mapped_bases)
    print(f"nodes: {len(nodes):,} in {nodes.base_id.nunique():,} base(s); "
          f"{int(mapped.sum()):,} mapped, "
          f"{int((~mapped).sum()):,} unmapped, "
          f"{int(participating.sum()):,} participating")

    if args.zero_unmapped:
        l[~mapped, :] = 0.0
        print(f"  --zero-unmapped: zeroed {int((~mapped).sum())} node(s) with "
              f"no substation match")

    # renormalize once, after every zeroing rule has been applied
    for b in range(len(BLOCKS)):
        tot = l[:, b].sum()
        if not tot > 0:
            raise SystemExit(
                f"block {BLOCKS[b]}: every node was zeroed, so there is "
                f"nothing to allocate to. Relax --zero-sources / "
                f"--zero-unmapped / --negative-nodes.")
        l[:, b] /= tot
    participating = (l != 0).any(axis=1)

    conserve = args.conserve
    if conserve == "auto":
        # the slack basis is the PARTICIPATING unmapped nodes
        has_slack = bool((~mapped & participating).any())
        conserve = "slack" if has_slack else "renorm"
        why = ("some unmapped nodes carry level and can absorb the per-cell "
               "residual, which keeps seasonal energy EXACT" if has_slack else
               "no unmapped node carries level, so there is no slack basis")
        print(f"conserve: auto -> {conserve}  ({why})")

    sources = [s.strip() for s in args.shape_sources.split(",") if s.strip()]
    raw: dict = {}
    for src in sources:
        if src == "flat":
            raw[src] = np.ones((C.MONTHHOUR.n_cells, len(nodes)))
            continue
        a = argparse.Namespace(shape_source=src, shape_col=args.shape_col,
                               draw=args.draw, min_draws=args.min_draws,
                               stochastic_run=args.stochastic_run)
        if src == "stoch":
            run = args.stochastic_run or BN.DEFAULT_STOCH_RUN
            arr, units, _ = P.stoch_surface(
                BN.PROJECTIONS / run / "substation_cell_mw.csv",
                args.draw, args.min_draws)
        else:
            arr, units, _ = P.envelope_surface(BN.PROFILES, args.shape_col)
        if src == "fleet":
            base = P.fleet_surface(arr, np.zeros(C.MONTHHOUR.n_cells, dtype=int))
            env = np.repeat(base[:, None], len(nodes), axis=1)
        else:
            braw, bases, _ = P.base_envelope(arr, units, edges)
            col = {b: i for i, b in enumerate(bases)}
            env = np.ones((C.MONTHHOUR.n_cells, len(nodes)))
            for i, b in enumerate(nodes.base_id):
                if mapped[i] and b in col:
                    env[:, i] = braw[:, col[b]]
        raw[src] = env
        print(f"  shape surface '{src}': built once "
              f"({env.shape[0]}x{env.shape[1]})")

    out = Path(args.out) / (args.tag or
                            f"approach3grid__{'-'.join(sources)}__{conserve}"
                            f"__{args.negative_nodes}")
    if out.exists():
        shutil.rmtree(out)
    (out / "shares").mkdir(parents=True, exist_ok=True)
    (out / "statewide").mkdir(parents=True, exist_ok=True)
    (out / "code").mkdir(parents=True, exist_ok=True)
    if args.format == "hourly":
        (out / "nodal_hourly").mkdir(parents=True, exist_ok=True)

    cols, index = P.sanitize_node_ids(nodes.node_id)
    half = C.get_spec("halfyear")
    block_of_cell = C.encode(half, C.label_frame(C.MONTHHOUR).month.to_numpy())

    for src, env in raw.items():
        np.savez_compressed(
            out / "shares" / f"{src}.npz", env=env.astype(np.float64),
            l=l.astype(np.float64), levels=levels.astype(np.float64),
            mapped=mapped, block_of_cell=block_of_cell,
            columns=np.array(cols, dtype=object).astype(str),
            node_ids=np.array(nodes.node_id.tolist(), dtype=object).astype(str))

    # ---- loop: one build_target per MODEL YEAR ---------------------------
    rows, checks = [], []
    for my in plan.model_years:
        target, scaling = B.build_target(my, B.OVERLAY_SCENARIO, args.overlays)
        for job in [j for j in plan.jobs if j.model_year == my]:
            idx, y = B.ca_series(target, job.weather_year, args.load_basis)
            t = pd.DataFrame({"month": idx.month, "hour_pst": idx.hour_pst,
                              "demand_mw": y})
            T = P.target_cell_energy(t)
            label = job.label
            pd.DataFrame({
                "datetime_pst": idx.datetime_pst.dt.strftime("%Y-%m-%d %H:%M"),
                "hour_of_year": idx.hour_of_year, "cell": T["cell"],
                "y_mw": y}).to_csv(out / "statewide" / f"{label}.csv.gz",
                                   index=False, float_format="%.17g",
                                   compression="gzip")
            # %.17g round-trips a float64 exactly. This matters: largest-
            # remainder apportionment breaks ties on the discarded fractions,
            # so a y(t) that differs in the 15th decimal can hand the leftover
            # 0.1 MW unit to a different node. Both results conserve exactly,
            # but only an exact round-trip makes expand.py byte-for-byte
            # identical to a direct write.
            for src, env in raw.items():
                shape = np.ones_like(env)
                sh, _ = P.energy_normalize(env[:, mapped], T["Y"],
                                           T["block_of_cell"],
                                           guards.min_shape_net_gross)
                shape[:, mapped] = sh
                if args.shape_common == "strip" and mapped.any():
                    sub, _ = P.strip_common(shape[:, mapped], l[mapped, :],
                                            T["Y"], T["block_of_cell"],
                                            guards.min_shape_net_gross)
                    shape[:, mapped] = sub
                S, sd = P.node_shares(l, shape, mapped, levels, T["Y"],
                                      T["block_of_cell"], conserve, guards)
                real = P.realized_energy_shares(S, T["Y"], T["block_of_cell"])
                dev = float(np.abs(real - l).max())
                if conserve == "slack" and dev > 1e-11:
                    raise AssertionError(
                        f"{label}/{src}: seasonal energy share off by {dev:.2e}")
                pre = P.node_annual_and_peak(S, T["Y"], T["ymax"], T["ymin"])
                worst = 0.0
                if args.format == "hourly":
                    st = BN.write_hourly(
                        out / "nodal_hourly" / f"{src}__{label}.csv.gz",
                        pd.DatetimeIndex(idx.datetime_pst), cols, S, y,
                        T["cell"], args.decimals)
                    worst = st["max_hourly_conservation_error_mw"]
                else:
                    # verify conservation on a sample without writing 8,760 rows
                    k = min(500, len(y))
                    sel = np.linspace(0, len(y) - 1, k).astype(int)
                    blk = y[sel, None] * S[T["cell"][sel], :]
                    snap = round_to_printed(blk, y[sel], args.decimals, True)
                    worst = float(np.abs(snap.sum(axis=1)
                                         - np.round(y[sel], args.decimals)).max())
                rows.append({
                    "series": label, "shape_source": src,
                    "model_year": job.model_year,
                    "weather_year": job.weather_year,
                    "twh": float(y.sum() / 1e6), "peak_mw": float(y.max()),
                    "max_abs_energy_share_dev_pp": dev * 100,
                    "max_hourly_conservation_error_mw": worst,
                    "max_abs_cell_share_error": sd["max_abs_cell_share_error"],
                    "A_NovApr": sd.get("A_NovApr"),
                    "A_MayOct": sd.get("A_MayOct"),
                    "node_peak_mw_max": float(pre["peak_mw"].max()),
                })
                c = pd.DataFrame({"node_id": nodes.node_id, "series": label,
                                  "shape_source": src})
                for bi, bn in enumerate(BLOCKS):
                    c[f"target_share_{bn}"] = l[:, bi]
                    c[f"realized_share_{bn}"] = real[:, bi]
                checks.append(c)
            print(f"  {label:<18} {y.sum() / 1e6:8.2f} TWh  "
                  f"peak {y.max():9,.0f} MW  energy dev "
                  f"{dev * 100:.2e} pp  conservation {worst:.2e} MW")

    summary = pd.DataFrame(rows)
    summary.to_csv(out / "summary.csv", index=False)
    pd.concat(checks, ignore_index=True).to_csv(
        out / "node_energy_check.csv.gz", index=False, compression="gzip")

    n_sub = edges[edges.status == "ok"].groupby("base_id").size().to_dict()
    index.assign(base_id=nodes.base_id.to_numpy(),
                 subname=nodes.subname.to_numpy(),
                 winter_load=nodes.winter_load.to_numpy(),
                 summer_load=nodes.summer_load.to_numpy(),
                 participating=(l != 0).any(axis=1), mapped=mapped,
                 n_substations_mapped=[n_sub.get(b, 0) for b in nodes.base_id]
                 ).to_csv(out / "node_index.csv", index=False)
    edges.to_csv(out / "mapping_report.csv", index=False)

    # Ship the allocation modules as a PACKAGE, not flat files: they use
    # relative imports (`from . import cells`), so a flat copy cannot import.
    # Shipping the package keeps the code byte-identical to the repo, which is
    # what makes expansion exact by construction, not by a reimplementation.
    shutil.copy(EXPAND_SRC, out / "code" / "expand.py")
    lp = out / "code" / "load_projection"
    lp.mkdir(parents=True, exist_ok=True)
    (lp / "__init__.py").write_text(
        INIT_DOC, encoding="utf-8")
    for mod in ("proportional.py", "cells.py", "external_loads.py"):
        shutil.copy(ROOT / "src/load_projection" / mod, lp / mod)

    (out / "manifest.json").write_text(json.dumps({
        "package": out.name, "git_rev": BN.git_rev(),
        "approach": "Approach 3 -- coordinate-free proportional disaggregation",
        "format": args.format,
        "command": "python " + " ".join(sys.argv),
        "grid": {"model_years": plan.model_years,
                 "weather_years": plan.weather_years,
                 "n_combinations": len(plan.jobs), "notes": plan.notes},
        "axes": {"negative_nodes": args.negative_nodes, "conserve": conserve,
                 "shape_sources": sources, "shape_col": args.shape_col,
                 "shape_common": args.shape_common, "draw": args.draw,
                 "decimals": args.decimals, "load_basis": args.load_basis,
                 "overlays": args.overlays,
                 "zero_nodes": args.zero_nodes or None,
                 "zero_sources": args.zero_sources or None,
                 "zero_unmapped": bool(args.zero_unmapped)},
        "guards": guards.as_dict(),
        "inputs": {"nodes": str(args.nodes), "nodes_md5": BN.md5(Path(args.nodes)),
                   "mapping": str(args.mapping),
                   "mapping_md5": BN.md5(Path(args.mapping)),
                   "profiles_md5": BN.md5(BN.PROFILES)},
        "realized": {k: v for k, v in ldiag.items() if not isinstance(v, dict)}
        | {"n_nodes": len(nodes), "n_bases": int(nodes.base_id.nunique()),
           "n_mapped_nodes": int(mapped.sum()),
           "n_unmapped_bases": int(n_unmapped_bases),
           "worst_energy_share_dev_pp": float(summary.max_abs_energy_share_dev_pp.max()),
           "worst_conservation_mw": float(summary.max_hourly_conservation_error_mw.max())},
        "notes": [
            "COMPACT FORMAT: `shares/<src>.npz` carries the TARGET-INVARIANT "
            "envelope surface `env` and the level shares `l`; `statewide/` "
            "carries one series per combination. expand.py recomputes the "
            "normalization and the shares from the series by calling the same "
            "proportional.py functions, so expansion is exact by construction.",
            "The share matrix is NOT invariant to the model year or weather "
            "year in Approach 3 (shape moves 8.2e-04 across weather years, "
            "1.4e-03 across model years), which is why `env` is shipped rather "
            "than `S`. Do not carry the CATS package's invariance claim over.",
            "The node table's ABSOLUTE SCALE IS DISCARDED; only ratios survive.",
        ],
    }, indent=2, default=str), encoding="utf-8")

    print(f"\nwrote {out}")
    tot = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"  {len(plan.jobs)} combination(s) x {len(sources)} variant(s) = "
          f"{len(rows)} dataset(s)")
    print(f"  package size {tot / 1e6:.2f} MB  (format={args.format})")
    print(f"  worst energy-share deviation "
          f"{summary.max_abs_energy_share_dev_pp.max():.2e} pp")
    print(f"  worst hourly conservation    "
          f"{summary.max_hourly_conservation_error_mw.max():.2e} MW")
    if args.format == "compact":
        est = len(rows) * len(y) * len(nodes) * 6 / 1e6
        print(f"  expanding all {len(rows)} would cost roughly {est:,.0f} MB")
        print(f"  python code/expand.py --list")


if __name__ == "__main__":
    main()
