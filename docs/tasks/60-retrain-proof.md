# Task 60: Re-read proof — truth and stress scenarios

Implementer: Sonnet 5.5 — extends the existing whole-pipeline stress script; needs care with ground truth.
Reviewer: a fresh Sonnet 5.5 subagent
Branch: task/60-retrain-proof (worktree ../logai2-task-60, created by the tech lead)
PR file: .team/prs/task-60-retrain-proof.md (from docs/templates/pr.md; .team/ is in the main checkout
~/Documents/logai2, write it there)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below is unclear, stop and reply
`QUESTION: ...` instead of guessing. Never kill processes you did not start. Disk is tight (~2 GB free on /):
respect the script's disk guard; never write to the live Elasticsearch.

## Why
Task 57's unit tests prove the re-read lane on small data. This task proves it on the real engine at volume,
with the same harness as docs/perf/2026-10-24-pipeline-stress.md: re-read counts must equal what live reading
stores, live must keep up while a re-read runs, and crashes and a deleted index must not corrupt counts.

## Read first
- Spec: docs/specs/2026-10-26-retrain.md §3, §7. Plan: task 60.
- docs/perf/2026-10-24-pipeline-stress.md (whole), `scripts/stress_pipeline.py` (whole: `GenSource`, `Stream`,
  `setup`, `Run`, `Scenario`, `run_scenario`, `scenarios`, `run`), `tests/test_stress_pipeline.py`.
- `logai2/jobs/reread.py`, `logai2/engine.py` (`_reread_run`, `_lane_tick`, `_reread_resume`, `_estimate`,
  `reread`), `logai2/owner/owner.py` (`add_lane`, `_flush_lane`), `tests/test_reread.py` (how the unit tests
  drive the lane and compare tables).

## Files
- Modify: `scripts/stress_pipeline.py`, `tests/test_stress_pipeline.py`, `docs/BUGS.md` (new findings only)
- Create: `docs/perf/2026-10-26-retrain.md`
Touching any other file needs a QUESTION first. If the harness needs a source feature the engine relies on
(`oldest_ms`, `count`, `page(..., timeout)`, deleting an index mid-run), add it to `GenSource`.

## Requirements: four scenarios, selectable with `--scenarios R1,R2,R3,R4`
1. **R1 truth:** two schemas, same generated world (10 services x 300 templates at default scale, 2 days of
   data at the default eps; one service fed by two patterns). Schema A: live reading of both days. Schema B:
   live reading of day 2 only, then a re-read of day 1 (all services, 00-24). Compare `service_hours`,
   `hourly_counts`, `template_module_hours` for day 1 row by row (A vs B) and `baseline` after a rebuild in
   both: report mismatching rows (expected 0). Template ids may differ between schemas (Drain3 numbers by
   first appearance): compare by template **text**, not id, and say so in the report.
2. **R2 under load:** live at 10k events/s while a re-read of 6 hours runs. Report live lag p50/p95/max
   (target: max < `retrain.max_live_lag_seconds` + 30 s), late counts (target 0), lane rate (docs/s), the
   re-read's wall time, how often the lane paused.
3. **R3 crashes:** kill the engine (stop it, build a new `Engine` on the same schema, as the I5 scenario does
   for Postgres) at about 30 % and 70 % of the re-read; final day-1 rows equal R1's truth; the run ends
   `succeeded` and its `pos_ms` never went backwards.
4. **R4 retention:** delete one pattern's oldest index at 50 %; the run ends with a WARNING in its log; no hour
   of that pattern's services after the deleted range is zero where truth is non-zero; hours of other services
   are written.
5. `--scale tiny` runs all four in under ~3 minutes; `tests/test_stress_pipeline.py` gains one test running
   `R1,R3,R4` tiny and asserting zero mismatches, `succeeded`, and the R4 warning.
6. `docs/perf/2026-10-26-retrain.md`: how it works (one paragraph, link the 2026-10-24 doc), machine, the
   command per scenario, the results table, and findings. Any defect found goes to docs/BUGS.md as a new
   B-number, and to the PR file as a concern (do not fix engine code in this task; report it).

## Done when
- [ ] the new test fails before the scenarios exist and passes after
- [ ] each scenario run once at default scale (or the largest the disk guard allows; say which) with results
      in the perf doc
- [ ] `.venv/bin/pytest -q` passes, output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
