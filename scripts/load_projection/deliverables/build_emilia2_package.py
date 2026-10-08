"""Build the `emilia2` deliverable: hourly load at sub-nodes, coordinate-free.

ONE COMMAND. Takes the raw wide bus/sub-node table (as in
`data/example.csv`), ingests it into Approach 3's contract, runs the
disaggregation, and writes a self-contained package an outsider can use with
only numpy and pandas and no access to this repository.

    python scripts/load_projection/deliverables/build_emilia2_package.py \\
        --input data/example.csv --year 2035 --weather-year 2012

Everything else has a working default. `--target-csv PATH` substitutes your own
hourly statewide series for the RESOLVE one.

Why Approach 3 and not the CATS county-first package
----------------------------------------------------
This target node system is a bus list we have **no coordinate-based nodal
map for**, so the county layer and the ReEDS county shares are unreachable and
`county_first_shares` cannot be used. Approach 3 is the method for exactly that
case; see `docs/approach3_proportional.md`. Per the amended deliverable rule
(CLAUDE.md, 2026-10-08) this deliverable **cites** Approach 3 rather than
inventing anything: all allocation logic is imported from
`src/load_projection/proportional.py`.

What the builder does
---------------------
1. `ingest_node_table.py` -> `nodes.csv` (sub-node levels) + `mapping.csv`
   (bus -> substation edges). `base_id` is the bus, `subname` is the `'ID'`
   column, so the sub-node axis becomes Approach 3's sibling axis.
2. `build_proportional_nodal.py` for each requested shape source.
3. Copies the result into `deliverables/emilia2/` with the README, the inputs
   that produced it, and the code to re-run it.

`--conserve auto` (the default) picks `slack` when some buses are unmapped and
`renorm` when every bus is mapped, because `slack` needs an unmapped set to
absorb the per-cell residual and refuses without one. It prints which it chose
and why.

**The README is a template at `README_emilia2.md` next to this script.** The
builder deletes and rewrites the whole package directory every run, so an edit
inside `deliverables/emilia2/` is silently discarded. Measured numbers in the
template come from `approach3/doc_numbers.py` -- never hand-edit one.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/approach3"))

A3 = ROOT / "scripts/load_projection/approach3"
INGEST = A3 / "ingest_node_table.py"
BUILD = A3 / "build_proportional_nodal.py"
GRID = A3 / "build_proportional_grid.py"
RUNS = ROOT / "data/processed/load_projection/approach3"
README_SRC = Path(__file__).with_name("README_emilia2.md")
DEFAULT_OUT = ROOT / "deliverables/emilia2"
PKG = "emilia2"


def run(cmd: list[str], label: str) -> str:
    print(f"\n--- {label}")
    print("    " + " ".join(str(c) for c in cmd[1:]))
    p = subprocess.run([str(c) for c in cmd], cwd=ROOT, capture_output=True,
                       text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stdout + p.stderr)
        raise SystemExit(f"{label} failed (exit {p.returncode})")
    print("\n".join("    " + ln for ln in p.stdout.strip().splitlines()))
    return p.stdout


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=str(ROOT / "data/example.csv"),
                    help="the NODE UNIVERSE: every node you want load on. Rows "
                         "the mapping does not cover leave `name` and "
                         "`lat`/`long` blank.")
    ap.add_argument("--mapping-input", default=None,
                    help="OPTIONAL separate file carrying match info for a "
                         "SUBSET of buses, joined onto the universe by bus "
                         "number. Use this when your mapping is its own file -- "
                         "passing it as --input would drop every unmapped bus "
                         "from the allocation.")
    ap.add_argument("--expect-buses", type=int, default=None,
                    help="refuse unless the universe has exactly this many "
                         "buses (guards against handing in the mapping file)")
    ap.add_argument("--expect-nodes", type=int, default=None,
                    help="refuse unless the universe has exactly this many "
                         "sub-nodes")
    ap.add_argument("--target", choices=["resolve", "csv"], default="resolve")
    ap.add_argument("--target-csv", default=None)
    ap.add_argument("--year", type=int, default=None,
                    help="shorthand for --period <year>")
    ap.add_argument("--weather-year", type=int, default=None,
                    help="shorthand for --weather-years <year>")
    ap.add_argument("--load-basis", choices=["net", "gross"], default="net")
    ap.add_argument("--match", default="name",
                    choices=["coords", "name", "coords-then-name"],
                    help="how the bus->substation edges are built. Default "
                         "`name`: in a real bus list `name` is the CURATED "
                         "match and is blank where the bus was not matched, so "
                         "`coords` would override that by mapping blank-name "
                         "rows on proximity anyway.")
    ap.add_argument("--shape-sources", default="envelope",
                    help="comma-separated: envelope,stoch,fleet,flat")
    ap.add_argument("--conserve", default="auto",
                    choices=["auto", "slack", "renorm"])
    ap.add_argument("--negative-nodes", default="net-base",
                    choices=["net-base", "zero", "participate", "refuse"])
    ap.add_argument("--sibling-draws", default="off",
                    choices=["off", "independent"])
    ap.add_argument("--decimals", type=int, default=1)
    ap.add_argument("--period", default="2026,2030,2035,2040,2045",
                    help="comma list of RESOLVE model years (snapshots), "
                         "crossed with --weather-years")
    ap.add_argument("--weather-years", default="2007-2014",
                    help="comma list and/or ranges, e.g. 2007-2014")
    ap.add_argument("--format", choices=["compact", "hourly"],
                    default="compact",
                    help="compact ships the allocation once plus one statewide "
                         "series per combination, with code/expand.py to "
                         "rebuild any hourly file exactly (~30x smaller)")
    ap.add_argument("--zero-sources", default="",
                    help="comma list matched against the input `source` column; "
                         "matching nodes carry ZERO load")
    ap.add_argument("--zero-unmapped", action="store_true",
                    help="zero every bus with no substation match")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--readme", default=str(README_SRC))
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()

    py = sys.executable
    inp = Path(args.input)
    if not inp.exists():
        raise SystemExit(f"--input {inp} not found")

    # ---- 1. ingest -------------------------------------------------------
    stage = ROOT / "data/checks/approach3" / PKG
    if stage.exists():
        shutil.rmtree(stage)
    ing = [py, INGEST, "--input", inp]
    if args.mapping_input:
        mi = Path(args.mapping_input)
        if not mi.exists():
            raise SystemExit(f"--mapping-input {mi} not found")
        ing += ["--mapping-input", mi]
    for flag, val in (("--expect-buses", args.expect_buses),
                      ("--expect-nodes", args.expect_nodes)):
        if val is not None:
            ing += [flag, str(val)]
    audit = run(ing + ["--audit"], "column audit")
    run(ing + ["--match", args.match, "--out", stage],
        "ingest -> Approach 3 contract")

    nodes = pd.read_csv(stage / "nodes.csv", dtype={"base_id": str})
    edges = pd.read_csv(stage / "mapping.csv", dtype={"base_id": str})
    n_bus = nodes.base_id.nunique()
    n_mapped = edges.base_id.nunique()
    unmapped_bases = n_bus - n_mapped

    conserve = args.conserve
    if conserve == "auto":
        conserve = "renorm" if unmapped_bases == 0 else "slack"
        why = ("every bus is mapped, so there is no slack basis to absorb the "
               "per-cell residual" if unmapped_bases == 0 else
               f"{unmapped_bases} of {n_bus} buses are unmapped and can carry "
               f"the residual, which keeps seasonal energy EXACT")
        print(f"\n--- conserve: auto -> {conserve}\n    because {why}")

    # ---- 2. run Approach 3 over the grid ---------------------------------
    # A single combination is just a one-job grid, so there is ONE code path
    # and the compact format is available either way.
    if args.target != "resolve":
        raise SystemExit(
            "--target csv is not supported here: the grid is defined over "
            "RESOLVE model years. For a custom hourly series use the "
            "single-run builder:\n"
            "  python scripts/load_projection/approach3/"
            "build_proportional_nodal.py --target csv --target-csv PATH ...")
    sources = [s.strip() for s in args.shape_sources.split(",") if s.strip()]
    period = str(args.year) if args.year else args.period
    wys = str(args.weather_year) if args.weather_year else args.weather_years
    tag = (f"{PKG}__{'-'.join(sources)}__{conserve}__{args.negative_nodes}")
    cmd = [py, GRID, "--nodes", stage / "nodes.csv",
           "--mapping", stage / "mapping.csv",
           "--period", period, "--weather-years", wys,
           "--shape-sources", args.shape_sources,
           "--conserve", conserve,
           "--negative-nodes", args.negative_nodes,
           "--load-basis", args.load_basis, "--format", args.format,
           "--decimals", str(args.decimals), "--tag", tag]
    if args.zero_sources:
        cmd += ["--zero-sources", args.zero_sources]
    if args.zero_unmapped:
        cmd += ["--zero-unmapped"]
    run(cmd, f"Approach 3 grid: ({period}) x ({wys})")
    grid_dir = RUNS / tag

    # ---- 3. package ------------------------------------------------------
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    for sub in ("summary", "reference", "code"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    # the grid run already laid out shares/ statewide/ code/ (+ nodal_hourly/
    # when --format hourly); copy that tree through unchanged so expand.py
    # finds exactly the layout it expects
    for sub in ("shares", "statewide", "code", "nodal_hourly"):
        if (grid_dir / sub).exists():
            shutil.copytree(grid_dir / sub, out / sub, dirs_exist_ok=True)
    for f, dest in (("summary.csv", "summary"),
                    ("node_energy_check.csv.gz", "summary"),
                    ("node_index.csv", "reference"),
                    ("mapping_report.csv", "reference")):
        if (grid_dir / f).exists():
            shutil.copy(grid_dir / f, out / dest / f)

    shutil.copy(stage / "nodes.csv", out / "reference" / "nodes.csv")
    shutil.copy(stage / "mapping.csv", out / "reference" / "mapping.csv")
    shutil.copy(inp, out / "reference" / f"source__{inp.name}")
    (out / "reference" / "column_audit.txt").write_text(audit, encoding="utf-8")
    shutil.copy(INGEST, out / "code" / INGEST.name)

    summary = pd.read_csv(grid_dir / "summary.csv")
    gman = json.loads((grid_dir / "manifest.json").read_text())

    (out / "manifest.json").write_text(json.dumps({
        "package": PKG,
        "approach": "Approach 3 -- coordinate-free proportional disaggregation",
        "built_from": str(inp),
        "match": args.match, "conserve": conserve,
        "negative_nodes": args.negative_nodes,
        "sibling_draws": args.sibling_draws,
        "shape_sources": sources, "format": args.format,
        "zero_sources": args.zero_sources or None,
        "zero_unmapped": bool(args.zero_unmapped),
        "n_buses": int(n_bus), "n_sub_nodes": int(len(nodes)),
        "n_buses_mapped": int(n_mapped),
        "n_buses_unmapped": int(unmapped_bases),
        # expand.py reads `axes` and `guards` from THIS file, so they must be
        # the grid run's own values, never a hand-copied set that could drift
        "axes": gman["axes"], "guards": gman["guards"], "grid": gman["grid"],
        "inputs": gman["inputs"], "realized": gman["realized"],
        "notes": gman["notes"],
    }, indent=2, default=str), encoding="utf-8")

    shutil.copy(Path(args.readme), out / "README.md")

    if not args.no_zip:
        shutil.make_archive(str(out), "zip", root_dir=out.parent,
                            base_dir=out.name)

    print(f"\n=== package {PKG} ===")
    print(f"  {out}")
    for p in sorted(out.rglob("*")):
        if p.is_file():
            print(f"    {p.relative_to(out).as_posix():<52} "
                  f"{p.stat().st_size / 1e6:>8.3f} MB")
    z = out.with_suffix(".zip")
    if z.exists():
        print(f"  zip  {z}  {z.stat().st_size / 1e6:.2f} MB")
    print(f"\n  buses {n_bus:,} ({n_mapped:,} mapped, {unmapped_bases:,} "
          f"unmapped) -> {len(nodes):,} sub-nodes")
    print(f"  conserve={conserve}  negative-nodes={args.negative_nodes}  "
          f"match={args.match}  format={args.format}")
    print(f"  {len(summary):,} dataset(s): "
          f"{summary.model_year.nunique()} model year(s) x "
          f"{summary.weather_year.nunique()} weather year(s) x "
          f"{summary.shape_source.nunique()} variant(s)")
    print(f"  worst energy-share deviation "
          f"{summary.max_abs_energy_share_dev_pp.max():.2e} pp")
    print(f"  worst hourly conservation    "
          f"{summary.max_hourly_conservation_error_mw.max():.2e} MW")
    if args.format == "compact":
        print(f"  rebuild any hourly file:  python code/expand.py --list")
    if unmapped_bases == 0:
        print("\n  NOTE every bus is mapped, so there is no slack basis and the")
        print("       run used per-cell renormalization. With a single mapped")
        print("       bus the shape cancels entirely and the result is the")
        print("       statewide series times each sub-node's level share --")
        print("       correct, but it cannot demonstrate the shape layer.")
        print("       A second bus with a different substation is needed.")


if __name__ == "__main__":
    main()
