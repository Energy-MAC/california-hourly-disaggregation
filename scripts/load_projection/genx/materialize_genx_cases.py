"""Expand a rescale run's seasonal demand files into runnable GenX case folders.

rescale_genx_demand.py writes only the 4 distinct seasonal `Demand_data.csv`
files, since the 28 control cases share one demand file per season.  GenX needs
a complete case directory per run, so this script copies each control case and
swaps in the run's demand file for that case's season.  Everything else --
`Run.jl`, `settings/`, `resources/`, `policies/`, `Generators_variability.csv`
(the axis the scenario grid actually sweeps) -- is copied unchanged, so a
materialized case differs from its control in exactly one file.

Copies are plain file copies rather than symlinks: Julia reads them directly and
Windows symlinks need elevated privileges.  Budget roughly 12 MB per case
(~350 MB for all 28), which is why materializing is a separate opt-in step.

The control cases ship with most result writers disabled, which would leave the
downstream comparison with almost nothing to compare (no load shedding, no
curtailment, no commitment).  `--enable-outputs` patches `settings/
output_settings.yml` to turn on the writers the comparison needs.  Apply it to
EVERY run being compared, control included, or the runs stop being
like-for-like.

CLI parameters
  --run-tag     rescale run folder under --rescaled-root (required)
  --cases       comma-separated subset, e.g. p1,p8 (default: all 28)
  --force       re-copy cases that already exist (default: skip them)
  --enable-outputs  turn on the comparison-relevant GenX result writers
                    (NSE, curtailment, commitment, capacity factor); the
                    zone x hour writers that would blow up on 8,870 zones
                    (power balance) stay off -- add them explicitly if wanted
  --rescaled-root  default genx/rescaled
  --out-root       default genx/scenarios_rescaled

Outputs ({out-root}/{run_tag}/)
  p1 ... p28/                 full runnable GenX cases
  scenarios_2019.csv          copy of the scenario index, for self-containment
  materialize_manifest.json   source paths + per-case demand md5s

Usage
  python scripts/load_projection/genx/materialize_genx_cases.py --run-tag genx__stoch__prox__topprop__static
  python scripts/load_projection/genx/materialize_genx_cases.py --run-tag genx__env__prox__hold__static --cases p1
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
from genx_demand_io import (  # noqa: E402
    DEMAND_RELPATH, GENX_ROOT, SCENARIO_INDEX, md5, scenario_seasons)

DEFAULT_RESCALED_ROOT = GENX_ROOT / "rescaled"
DEFAULT_OUT_ROOT = GENX_ROOT / "scenarios_rescaled"

# Result writers --enable-outputs turns on, and why each is needed to compare
# two demand allocations on a fixed-capacity dispatch model:
#   NSE            load shed per zone-hour -- the reliability cost of moving load
#   Curtailment    VRE spilled -- shows load moving away from renewable-rich buses
#   Commit         thermal commitment -- the only quasi-first-stage decision a PCM
#                  has, so it is what a cross-evaluation would carry between runs
#   CapacityFactor per-resource utilization; cheap
# Deliberately NOT enabled: WritePowerBalance (zone x hour x component over 8,870
# zones is hundreds of MB per case).
ENABLE_OUTPUTS = {
    "WriteNSE": "true",
    "WriteCurtailment": "true",
    "WriteCommit": "true",
    "WriteCapacityFactor": "true",
}


def patch_output_settings(path: Path, updates: dict[str, str]) -> list[str]:
    """Set the named keys in a GenX output_settings.yml, preserving everything else.

    Edited line-wise rather than via a YAML round-trip so comments, ordering, and
    any key this script does not know about survive untouched.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    changed, seen = [], set()
    for i, line in enumerate(lines):
        key = line.split(":", 1)[0].strip()
        if key in updates:
            seen.add(key)
            new = f"{key}: {updates[key]}"
            if line.strip() != new:
                lines[i] = new
                changed.append(key)
    for key in updates:
        if key not in seen:  # writer absent from this GenX version's template
            lines.append(f"{key}: {updates[key]}")
            changed.append(key)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-tag", required=True)
    ap.add_argument("--cases", default=None,
                    help="comma-separated case subset (default: all)")
    ap.add_argument("--force", action="store_true",
                    help="re-copy cases that already exist")
    ap.add_argument("--enable-outputs", action="store_true",
                    help="turn on the GenX result writers the comparison needs; "
                         "apply to every run being compared, control included")
    ap.add_argument("--rescaled-root", default=str(DEFAULT_RESCALED_ROOT))
    ap.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    args = ap.parse_args()

    run_dir = Path(args.rescaled_root) / args.run_tag
    if not run_dir.exists():
        raise FileNotFoundError(
            f"rescale run not found: {run_dir}\n"
            f"Run rescale_genx_demand.py for this tag first.")

    tree = scenario_seasons()
    cases = args.cases.split(",") if args.cases else tree.cases
    unknown = [c for c in cases if c not in tree.case_season]
    if unknown:
        raise ValueError(f"unknown case(s) {unknown}; known: {tree.cases}")

    season_demand = {}
    for season in tree.seasons:
        path = run_dir / f"Demand_data__{season}.csv"
        if not path.exists():
            raise FileNotFoundError(f"run {args.run_tag} is missing {path.name}")
        season_demand[season] = path

    out_root = Path(args.out_root) / args.run_tag
    out_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_tag": args.run_tag,
        "rescale_run": str(run_dir.relative_to(ROOT)),
        "control_tree": str(tree.root.relative_to(ROOT)),
        "manifest_md5": md5(run_dir / "manifest.json") if (run_dir / "manifest.json").exists() else None,
        "enable_outputs": ENABLE_OUTPUTS if args.enable_outputs else None,
        "cases": {},
    }

    n_copied = n_skipped = 0
    for case in cases:
        season = tree.case_season[case]
        dest = out_root / case
        if dest.exists() and not args.force:
            print(f"skip {case} (exists; --force to replace)")
            n_skipped += 1
        else:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(tree.case_dir(case), dest)
            shutil.copyfile(season_demand[season], dest / DEMAND_RELPATH)
            note = ""
            if args.enable_outputs:
                changed = patch_output_settings(
                    dest / "settings/output_settings.yml", ENABLE_OUTPUTS)
                note = f"  [outputs enabled: {', '.join(changed)}]" if changed else ""
            print(f"{case:4s} <- control {case} + {season} demand{note}")
            n_copied += 1
        manifest["cases"][case] = {
            "season": season,
            "demand_md5": md5(dest / DEMAND_RELPATH),
            "output_settings_md5": md5(dest / "settings/output_settings.yml"),
        }

    shutil.copyfile(SCENARIO_INDEX, out_root / SCENARIO_INDEX.name)
    (out_root / "materialize_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\n{n_copied} case(s) materialized, {n_skipped} skipped")
    print(f"wrote {out_root.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
