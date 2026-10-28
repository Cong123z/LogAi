#!/bin/bash
# usage: runc.sh tag test days schema [results_schema]   all methods in the lab container, worktree scripts mounted read-only
cd /home/cong/Documents/logai2-task-bk17
OUT=/home/cong/Documents/logai2/.team/bk17-out
START=$(date +%s)
docker compose --profile lab run --rm -v $PWD/scripts:/app/scripts:ro -e PYTHONPATH=/app:/app/lab/timing logai2-lab --site laptop --test $2 --methods all --days $3 --schema $4 --results-schema ${5:-$4} > $OUT/$1.out 2> $OUT/$1.err
echo "rc=$? wall_s=$(( $(date +%s) - START ))" >> $OUT/$1.err; echo finished >> $OUT/$1.err
