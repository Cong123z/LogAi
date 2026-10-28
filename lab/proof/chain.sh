#!/bin/bash
# resumable: a step whose .err already says rc=0 is skipped (the laptop reboots now and then)
OUT=/home/cong/Documents/logai2/.team/bk17-out
cd /home/cong/Documents/logai2-task-bk17/lab/proof
step() { grep -q "rc=0" $OUT/$1.err 2>/dev/null || ./runc.sh "$@"; }
for n in 7 14 28; do
  step f${n}A A $n lab_s_w$n
  step f${n}B B $n lab_s_w$n
done
echo done > $OUT/chain.done
