"""Prepare the materialized GenX cases for cluster upload: per-case Lawrencium
jobscripts plus an Excel manifest of everything being handed over.

Each case gets the cluster user's jobscript verbatim (account pc_psidml,
partition lr6, qos lr_normal, 12 threads). The job name is shared by default --
that is safe here because `submit_all.sh` cd's INTO each case directory before
calling sbatch, so every job writes test.out/test.err into its own folder and
logs cannot collide. Pass `--job-name unique` to name each job <run>__<case>
instead, which makes `squeue` readable across 200 jobs.

Also writes the submit drivers and an Excel workbook describing the upload:
one row per case (run tag, weather year, season, what the allocation is, size,
demand md5) plus a sheet describing the runs and a sheet of the SLURM settings.

CLI parameters
  --runs        comma-separated run tags (default: all materialized)
  --cases       comma-separated cases (default: all 28)
  --account     SLURM account (default fc_emac)
  --partition   SLURM partition (default savio3)
  --cpus        cpus-per-task (default 12)
  --time        wall clock limit (default 24:00:00)
  --qos         SLURM qos (default lr_normal)
  --mail-user   notification address (default: the shipped one)
  --project     Julia project path ON THE CLUSTER for the GenX install
  --grb-license Gurobi license path, written COMMENTED OUT in each jobscript
  --job-name    shared name (default) or "unique" for <run>__<case>
  --root        default genx/scenarios_rescaled
  --no-jobscripts   write only the manifest, leave jobscripts untouched

Outputs
  <root>/<run>/<case>/jobscript.sh   rewritten for the target cluster
  <root>/<run>/submit_all.sh        loops p1..p28 in THAT run and sbatch's each
  <root>/submit_everything.sh       calls every run's submit_all.sh
  <root>/genx_upload_manifest.xlsx  the sheet to hand over
  <root>/genx_upload_manifest.csv   same content, plain text

Usage
  python scripts/load_projection/genx/prep_cluster_upload.py
  python scripts/load_projection/genx/prep_cluster_upload.py --project /global/scratch/users/me/GenX.jl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
from genx_demand_io import GENX_ROOT, md5, scenario_seasons  # noqa: E402

DEFAULT_ROOT = GENX_ROOT / "scenarios_rescaled"
RESCALED = GENX_ROOT / "rescaled"

# Default case set: weather years 2011 (highest VRE availability) and 2012
# (lowest), across all four seasons -- 8 of the 28 cases. The weather year
# changes only Generators_variability.csv, so two years bracketing the
# renewable range is a replicate pair, not a loss of load-allocation signal.
DEFAULT_CASES = "p5,p6,p12,p13,p19,p20,p26,p27"


# What each family/axis is, in words -- composed per tag and carried into the
# manifest so the person running the jobs can tell the allocations apart
# without reading the code.
FAMILY_DESC = {
    "reedsco": {
        "aratio": ("ReEDS county-first, Method 1 (alpha = u/n). County gets its "
                   "ReEDS share of statewide energy; uncovered buses take the "
                   "equal share an even split would give them, substation buses "
                   "split their remainder by max-load envelope. Every candidate "
                   "bus loaded."),
        "a0": ("ReEDS county-first, Method 2 (alpha = 0). Whole county energy "
               "goes to its substation buses by max-load envelope; uncovered "
               "buses get nothing."),
    },
    "env": {
        "hold": ("ENVELOPE HOLD (minimum intervention). Re-splits only the load "
                 "the control places on buses we have substations for, among "
                 "those same buses, weighted by the substations' own max-load "
                 "envelopes; every other bus keeps its control value. Uses no "
                 "ReEDS input and no projection model."),
    },
}
MAP_DESC = {
    "prox": "",
    "voltres": (" Substation->bus map: voltage-restricted nearest node."),
    "nameprox": (" Substation->bus map: CEC-lineage IDENTITY match first "
                 "(the bus built from the substation's own CEC record), "
                 "proximity for the remainder."),
    "catch": (" Substation->bus map: transportation-LP CATCHMENTS -- every "
              "candidate bus is assigned to a substation and load returns to "
              "the catchment, so every candidate bus stays loaded."),
    "namecatch": (" Substation->bus map: identity matches forced, LP catchments "
                  "for the rest; every candidate bus stays loaded."),
}
LEVEL_DESC = {
    "static": "",
    "monthhour": (" Weights resolved per (month, hour) cell, so a bus's share "
                  "follows its own diurnal/seasonal shape through the week."),
}


def describe(run: str) -> str:
    """Human description composed from the run tag's axes."""
    if run == "genx__control":
        return ("CONTROL -- CATS demand exactly as shipped. Baseline; taken as "
                "the true allocation.")
    try:
        _, weights, mp, alloc, level = run.split("__")
    except ValueError:
        return "(unrecognized run tag)"
    if weights == "stoch":
        way, draw = alloc.split("-", 1)
        realization = ("mean over the 5 Monte Carlo draws"
                       if draw == "mean" else f"single Monte Carlo draw {draw[1:]}")
        if way.startswith("w2"):
            scope = ("Way 2 (narrow) -- only the load already on substation buses is "
                     "pooled, statewide, and re-dealt among those same buses. Every "
                     "other bus keeps its control value, so fewer buses change.")
        else:
            gate = int(way.replace("top", "")[3:]) / 100
            scope = (f"Way 1 (broad) -- counties whose substation coverage is at least "
                     f"{gate:.0%} have ALL their buses swept into the pool; elsewhere "
                     f"only substation buses are swept.")
            scope += (" Top-off ON: the swept uncovered buses stay alive on an equal "
                      "split of beta=|U|/n of the pool."
                      if way.endswith("top") else
                      " Top-off OFF: the swept uncovered buses go to zero.")
        base = (f"STOCHASTIC (Approach 2) pool-and-redistribute; {scope} Pool is "
                f"dealt to substation buses in proportion to their PER-CELL "
                f"stochastic load, {realization}. County totals EMERGE rather "
                f"than being imposed.")
    else:
        base = FAMILY_DESC.get(weights, {}).get(alloc, "(no description registered)")
    return base + MAP_DESC.get(mp, "") + LEVEL_DESC.get(level, "")

JOBSCRIPT = """#!/bin/bash
# Job name:
#SBATCH --job-name={jobname}
#
# Account:
#SBATCH --account={account}
# Partition:
#SBATCH --partition={partition}
#SBATCH --qos={qos}
#SBATCH --nodes=1
#SBATCH --cpus-per-task={cpus}
# Wall clock limit:
#SBATCH --time={time}
# Output:
#SBATCH --output="test.out"
#SBATCH --error="test.err"
# Mail:
#SBATCH --mail-type=FAIL          # notifications for job done & fail
#SBATCH --mail-type=end          # notifications for job done & fail
#SBATCH --mail-user={mail} # send-to address

# Commands to run
module load julia
module load gurobi
#export GRB_LICENSE_FILE="{grb_license}"

julia --project="{project}" --threads={cpus} Run.jl
date
"""

SUBMIT_ALL = """#!/bin/bash

# Loop through directories p1 to p28
for i in {1..28}
do
    folder="p$i"
    if [ -d "$folder" ]; then
        echo "Submitting job in $folder..."
        cd "$folder"
        sbatch jobscript.sh  # Change to qsub if using PBS
        cd ..
    else
        echo "Directory $folder not found, skipping."
    fi
done
"""

SUBMIT_EVERYTHING = """#!/bin/bash
# Submit EVERY case in EVERY run folder by calling each run's own submit_all.sh.
#   bash submit_everything.sh                # all runs
#   bash submit_everything.sh genx__control  # only runs whose name matches
set -u
FILTER="${1:-}"
cd "$(dirname "$0")"
for d in */; do
    d="${d%/}"
    [ -f "$d/submit_all.sh" ] || continue
    if [ -n "$FILTER" ] && [[ "$d" != *"$FILTER"* ]]; then continue; fi
    echo "=== $d"
    ( cd "$d" && bash submit_all.sh )
done
"""


def write_sh(path: Path, text: str) -> None:
    r"""Write a shell script with LF endings, ALWAYS.

    Path.write_text() on Windows turns \n into \r\n. A shell script with
    CRLF fails on Linux: the shebang becomes "/bin/bash\r" -> "bad interpreter",
    and every "#SBATCH --time=24:00:00\r" carries a stray CR into the value, so
    SLURM either rejects the job or misparses it. These files are written on
    Windows and consumed on a Linux cluster, so the newline must be pinned.
    """
    path.write_text(text, encoding="utf-8", newline="\n")


def dir_size_mb(p: Path) -> float:
    return round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6, 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", default=None)
    ap.add_argument("--cases", default=None)
    ap.add_argument("--account", default="pc_psidml")
    ap.add_argument("--partition", default="lr6")
    ap.add_argument("--qos", default="lr_normal")
    ap.add_argument("--cpus", type=int, default=12)
    ap.add_argument("--time", default="24:00:00")
    ap.add_argument("--mail-user", default="emiliac@berkeley.edu")
    ap.add_argument("--project", default="/global/scratch/users/enc/GenX")
    ap.add_argument("--grb-license",
                    default="/global/home/users/enc/.gurobi/gurobi.lic",
                    help="written as a COMMENTED-OUT export in each jobscript")
    ap.add_argument("--job-name", default="cem_1wk_dispatch",
                    help="SLURM job name. Default is the single shared name the "
                         "cluster user asked for; pass 'unique' to name each job "
                         "<run>__<case> instead, which makes squeue readable")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--no-jobscripts", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        sys.exit(f"{root} not found -- materialize runs first.")
    # a run folder is one holding at least one case directory (p<N>); checking
    # for p1 specifically broke silently when the case set no longer included it
    available = sorted(p.name for p in root.iterdir()
                       if p.is_dir() and any(c.is_dir() and c.name.startswith("p")
                                             and c.name[1:].isdigit()
                                             for c in p.iterdir()))
    runs = args.runs.split(",") if args.runs else available
    unknown = [r for r in runs if r not in available]
    if unknown:
        sys.exit(f"unknown run tag(s) {unknown}; available: {available}")

    tree = scenario_seasons()
    idx = pd.read_csv(GENX_ROOT / "scenarios_2019.csv")
    weather = {f"p{int(n)}": int(g) for n, g in zip(idx["Number"], idx["Gen_Var"])}
    cases = args.cases.split(",") if args.cases else DEFAULT_CASES.split(",")

    rows, case_paths, n_written = [], [], 0
    for run in runs:
        desc = describe(run)
        for case in cases:
            case_dir = root / run / case
            if not case_dir.exists():
                continue
            jobname = (f"{run}__{case}" if args.job_name == "unique"
                       else args.job_name)
            if not args.no_jobscripts:
                write_sh(case_dir / "jobscript.sh", JOBSCRIPT.format(
                    jobname=jobname, account=args.account, partition=args.partition,
                    qos=args.qos, cpus=args.cpus, time=args.time,
                    mail=args.mail_user, project=args.project,
                    grb_license=args.grb_license))
                n_written += 1
            rel = f"{run}/{case}"
            case_paths.append(rel)
            rows.append({
                "run_tag": run,
                "case": case,
                "case_number": int(case[1:]),
                "weather_year": weather.get(case),
                "season": tree.case_season[case],
                "allocation": desc,
                "slurm_job_name": jobname,
                "relative_path": rel,
                "demand_md5": md5(case_dir / "system/Demand_data.csv"),
                "size_mb": dir_size_mb(case_dir),
            })

    if not rows:
        sys.exit("no cases found to prepare")
    manifest = pd.DataFrame(rows).sort_values(["run_tag", "case_number"])

    write_sh(root / "genx_upload_cases.txt", "\n".join(case_paths) + "\n")
    # submit_all.sh lives NEXT TO the p<N> folders (one per run), because that is
    # where its `cd "p$i"` loop resolves; submit_everything.sh drives them all.
    for run in runs:
        write_sh(root / run / "submit_all.sh", SUBMIT_ALL)
    write_sh(root / "submit_everything.sh", SUBMIT_EVERYTHING)

    # one row per RUN, for the summary sheet
    per_run = (manifest.groupby("run_tag")
               .agg(n_cases=("case", "size"), total_size_mb=("size_mb", "sum"),
                    n_distinct_demand=("demand_md5", "nunique"),
                    allocation=("allocation", "first"))
               .reset_index())
    per_run["demand_note"] = per_run.n_distinct_demand.map(
        lambda n: f"{n} distinct demand file(s) -- one per season, shared across "
                  f"the 7 weather years")

    settings = pd.DataFrame([
        {"setting": "account", "value": args.account},
        {"setting": "partition", "value": args.partition},
        {"setting": "cpus-per-task", "value": args.cpus},
        {"setting": "time", "value": args.time},
        {"setting": "mail-user", "value": args.mail_user},
        {"setting": "julia --project", "value": args.project},
        {"setting": "cases per run", "value": len(cases)},
        {"setting": "runs", "value": len(runs)},
        {"setting": "total jobs", "value": len(manifest)},
        {"setting": "total upload size (GB)",
         "value": round(manifest.size_mb.sum() / 1000, 2)},
        {"setting": "qos", "value": args.qos},
        {"setting": "job name", "value": jobname},
        {"setting": "submit command",
         "value": "bash submit_everything.sh   (all runs)  |  cd <run> && bash "
                  "submit_all.sh   (one run)  |  cd <run>/<case> && sbatch jobscript.sh"},
        {"setting": "results land in", "value": "<run>/<case>/results/"},
    ])

    xlsx = root / "genx_upload_manifest.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xl:
        per_run.to_excel(xl, sheet_name="runs", index=False)
        manifest.to_excel(xl, sheet_name="cases", index=False)
        settings.to_excel(xl, sheet_name="slurm_settings", index=False)
        for sheet, df in (("runs", per_run), ("cases", manifest),
                          ("slurm_settings", settings)):
            ws = xl.sheets[sheet]
            for i, col in enumerate(df.columns, start=1):
                width = min(60, max(12, int(df[col].astype(str).str.len().max()) + 2,
                                    len(col) + 2))
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    manifest.to_csv(root / "genx_upload_manifest.csv", index=False,
                encoding="utf-8")

    print(f"{len(manifest)} cases across {len(runs)} runs")
    if not args.no_jobscripts:
        print(f"  rewrote {n_written} jobscript(s)  (job-name={jobname!r}; "
              f"each writes test.out/test.err into its OWN case directory, so "
              f"logs cannot collide even with a shared name)")
    print(f"  total upload {manifest.size_mb.sum()/1000:.2f} GB")
    print(f"\nwrote {xlsx.relative_to(ROOT)}")
    print(f"wrote {(root / 'genx_upload_manifest.csv').relative_to(ROOT)}")
    print(f"wrote {len(runs)} x <run>/submit_all.sh  +  "
          f"{(root / 'submit_everything.sh').relative_to(ROOT)}")
    print(f"wrote {(root / 'genx_upload_cases.txt').relative_to(ROOT)}")
    print(f"\nNOTE: --project is the GenX.jl environment path ON THE CLUSTER "
          f"(currently {args.project!r}); re-run with --project if it differs.")


if __name__ == "__main__":
    main()
