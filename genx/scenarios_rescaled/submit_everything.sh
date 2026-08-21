#!/bin/bash
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
