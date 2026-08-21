#!/bin/bash
# Patch an already-uploaded GenX case tree with the Lawrencium jobscript and
# per-run submit_all.sh. Run this IN PLACE on the cluster -- nothing needs to be
# re-uploaded.
#
#   bash patch_cluster_jobscripts.sh --dry-run     # show what would change
#   bash patch_cluster_jobscripts.sh               # do it
#   bash patch_cluster_jobscripts.sh --unique-names  # also give each job a
#                                                    # distinct --job-name so
#                                                    # squeue is readable
#
# Expects to sit in the directory that holds the run folders:
#   <here>/genx__control/p5/ ... , <here>/genx__reedsco__.../p5/ ...
#
# What it writes:
#   <run>/<case>/jobscript.sh   overwritten in every case directory
#   <run>/submit_all.sh         loops p1..p28 and sbatches each case present
#   <here>/submit_everything.sh convenience: runs every <run>/submit_all.sh
set -euo pipefail
cd "$(dirname "$0")"

DRY=0; UNIQ=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --unique-names) UNIQ=1 ;;
    *) echo "unknown option: $a" >&2; exit 1 ;;
  esac
done

write_jobscript() {   # $1 = destination path, $2 = job name
  cat > "$1" <<EOF
#!/bin/bash
# Job name:
#SBATCH --job-name=$2
#
# Account:
#SBATCH --account=pc_psidml
# Partition:
#SBATCH --partition=lr6
#SBATCH --qos=lr_normal
#SBATCH --nodes=1
#SBATCH --cpus-per-task=12
# Wall clock limit:
#SBATCH --time=24:00:00
# Output:
#SBATCH --output="test.out"
#SBATCH --error="test.err"
# Mail:
#SBATCH --mail-type=FAIL          # notifications for job done & fail
#SBATCH --mail-type=end          # notifications for job done & fail
#SBATCH --mail-user=emiliac@berkeley.edu # send-to address

# Commands to run
module load julia
module load gurobi
#export GRB_LICENSE_FILE="/global/home/users/enc/.gurobi/gurobi.lic"

julia --project="/global/scratch/users/enc/GenX" --threads=12 Run.jl
date
EOF
}

write_submit_all() {  # $1 = destination path
  cat > "$1" <<'EOF'
#!/bin/bash

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
EOF
}

n_runs=0; n_cases=0
for run in */; do
  run="${run%/}"
  # a run folder is one that actually holds p<N> case directories
  shopt -s nullglob
  cases=("$run"/p[0-9]*)
  shopt -u nullglob
  [ ${#cases[@]} -eq 0 ] && continue
  n_runs=$((n_runs+1))

  for c in "${cases[@]}"; do
    [ -d "$c" ] || continue
    case_name="$(basename "$c")"
    if [ $UNIQ -eq 1 ]; then jn="${run}__${case_name}"; else jn="cem_1wk_dispatch"; fi
    if [ $DRY -eq 1 ]; then
      echo "would write $c/jobscript.sh   (job-name=$jn)"
    else
      write_jobscript "$c/jobscript.sh" "$jn"
    fi
    n_cases=$((n_cases+1))
  done

  if [ $DRY -eq 1 ]; then
    echo "would write $run/submit_all.sh"
  else
    write_submit_all "$run/submit_all.sh"
    chmod +x "$run/submit_all.sh" 2>/dev/null || true
  fi
done

# top-level convenience: submit every run's cases in one go
if [ $DRY -eq 0 ]; then
  cat > submit_everything.sh <<'EOF'
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
EOF
  chmod +x submit_everything.sh 2>/dev/null || true
fi

echo
if [ $DRY -eq 1 ]; then
  echo "DRY RUN: $n_cases jobscript(s) across $n_runs run folder(s) would be rewritten."
else
  echo "Wrote $n_cases jobscript.sh + $n_runs submit_all.sh + submit_everything.sh"
  echo
  echo "TEST ONE FIRST:"
  echo "  cd <a run folder>/p5 && sbatch jobscript.sh"
  echo "  # check it lands, then:  cd .. && bash submit_all.sh"
  echo "  # when happy with everything:  bash submit_everything.sh"
fi
