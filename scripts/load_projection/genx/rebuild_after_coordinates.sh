#!/usr/bin/env bash
# Full rebuild after editing data/substationCoordinateOverrides.csv.
#
# Coordinates flow: overrides -> substation_attributes_clean.csv -> the nodal
# maps -> the GenX demand sets -> materialized cases -> cluster manifest.
# Every step below is on that chain, in dependency order.
#
#   bash scripts/load_projection/genx/rebuild_after_coordinates.sh
#
# Runs from any shell: it resolves the project's .venv interpreter itself, so
# the venv does NOT need to be activated first (PowerShell activates it
# automatically, an interactive Git Bash session does not).
#
# Budget ~1.5-2 h (step 5 dominates: 25 runs x ~2 min of point-in-polygon).
# Safe to re-run; every step overwrites its own outputs.
set -euo pipefail
cd "$(dirname "$0")/../../.."          # repo root

# PY is an ARRAY so the "py -3" fallback (two words) survives quoting.
if   [ -x ".venv/Scripts/python.exe" ];  then PY=(./.venv/Scripts/python.exe)  # Windows venv
elif [ -x ".venv/bin/python" ];          then PY=(./.venv/bin/python)          # POSIX venv
elif command -v python  >/dev/null 2>&1; then PY=(python)
elif command -v python3 >/dev/null 2>&1; then PY=(python3)
elif command -v py      >/dev/null 2>&1; then PY=(py -3)
else
  echo "No Python interpreter found. Expected .venv/Scripts/python.exe in the" >&2
  echo "repo root, or python/python3/py on PATH." >&2
  exit 1
fi
echo "interpreter: ${PY[*]}  ($("${PY[@]}" --version 2>&1))"

R=scripts/load_projection/genx/rescale_genx_demand.py
CASES=p5,p6,p12,p13,p19,p20,p26,p27

echo "############ 1/9  substation attributes (applies the coordinate overrides)"
"${PY[@]}" scripts/data/substations/process_substations_clean.py

echo "############ 2/9  substation -> county (point-in-polygon; feeds Approach 1 + checks)"
"${PY[@]}" scripts/data/substations/assign_substation_counties.py

echo "############ 3/9  nodal maps"
"${PY[@]}" scripts/load_projection/nodal/map_loads_to_nodes.py --system CATS
"${PY[@]}" scripts/load_projection/nodal/map_loads_to_nodes.py --system CATS --voltage-mode restrict
"${PY[@]}" scripts/load_projection/nodal/build_identity_catchment_maps.py

echo "############ 4/9  Approach 2 (regenerating so nothing is stale; ~10 min)"
"${PY[@]}" scripts/load_projection/genx/build_cats_target.py
"${PY[@]}" scripts/load_projection/approach2/generate_stochastic.py \
    --family normal --n-draws 5 \
    --target data/processed/load_projection/cats_caiso_target.csv \
    --calibrate-on target --save-cells --validate

echo "############ 5/9  the 25 GenX demand sets"
"${PY[@]}" $R --weights control
"${PY[@]}" $R --weights reedsco --alpha ratio
"${PY[@]}" $R --weights reedsco --alpha 0
"${PY[@]}" $R --weights reedsco --alpha ratio --level monthhour
"${PY[@]}" $R --weights reedsco --alpha 0     --level monthhour
"${PY[@]}" $R --weights env
"${PY[@]}" $R --weights env --level monthhour
for D in mean 0 1 2; do
  "${PY[@]}" $R --weights stoch --level monthhour --stoch-gate 0.30 --stoch-topoff equal --draw $D
  "${PY[@]}" $R --weights stoch --level monthhour --stoch-gate 0.30 --stoch-topoff none  --draw $D
  "${PY[@]}" $R --weights stoch --level monthhour --stoch-gate 2.0  --stoch-topoff none  --draw $D
done
"${PY[@]}" $R --weights reedsco --alpha ratio --level monthhour --map nameprox
"${PY[@]}" $R --weights reedsco --alpha ratio --level monthhour --map catch
"${PY[@]}" $R --weights reedsco --alpha ratio --level monthhour --map namecatch
"${PY[@]}" $R --weights stoch --level monthhour --map nameprox  --stoch-gate 0.30 --stoch-topoff equal --draw mean
"${PY[@]}" $R --weights stoch --level monthhour --map catch     --stoch-gate 2.0  --stoch-topoff none  --draw mean
"${PY[@]}" $R --weights stoch --level monthhour --map namecatch --stoch-gate 2.0  --stoch-topoff none  --draw mean

echo "############ 6/9  guards (fail loudly before spending materialize time)"
"${PY[@]}" scripts/load_projection/genx/test_genx_rescale.py

echo "############ 7/9  materialize the 8-case set for every run"
for TAG in $(ls genx/rescaled); do
  "${PY[@]}" scripts/load_projection/genx/materialize_genx_cases.py \
      --run-tag "$TAG" --cases $CASES --enable-outputs --force
done

echo "############ 8/9  cluster handoff (unique jobscripts + Excel manifest)"
"${PY[@]}" scripts/load_projection/genx/prep_cluster_upload.py

echo "############ 9/9  refresh measured numbers"
"${PY[@]}" scripts/load_projection/genx/compare_genx_demand.py --no-figures
"${PY[@]}" scripts/load_projection/genx/doc_numbers.py | tee data/checks/genx_rescale/doc_numbers.txt

echo
echo "DONE. doc_numbers output saved to data/checks/genx_rescale/doc_numbers.txt"
echo "It PRINTS numbers; it does not edit the docs. Hand that file over to have"
echo "the quoted figures updated in README/docs/CLAUDE.md."
