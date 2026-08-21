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
