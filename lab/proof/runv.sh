#!/bin/bash
# usage: runv.sh tag test methods days schema   core methods in the venv
cd /home/cong/Documents/logai2-task-bk17
OUT=/home/cong/Documents/logai2/.team/bk17-out
PYTHONPATH=$PWD:$PWD/lab/timing /usr/bin/time -v /home/cong/Documents/logai2/.venv/bin/python -m scripts.lab.run --site laptop --test $2 --methods $3 --days $4 --schema $5 --results-schema $5 --dsn postgresql://logai:logai@127.0.0.1:5433/logai > $OUT/$1.out 2> $OUT/$1.err
echo "rc=$?" >> $OUT/$1.err; echo finished >> $OUT/$1.err
