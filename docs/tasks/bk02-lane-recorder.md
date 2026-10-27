# Task bk02: Recorder in the re-read lane

Implementer: Sonnet 5.5 — lane replace semantics and resume, same risk class as task 57.
Reviewer: Opus 5.5 (fresh).
Branch: task/bk02-lane-recorder (worktree ../logai2-task-bk02, created by the tech lead)
PR file: .team/prs/task-bk02-lane-recorder.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
A Retrain re-read replaces whole service-hours from Elasticsearch. With the recorder on (task bk01), a re-read must
also replace those hours' per-minute rows, so a 7-day re-read backfills `lab_minute_counts` before ES drops the data.

## Read first
- Spec: `docs/specs/2026-10-27-bakeoff-laptop.md` §4 (recorder; §4.5 is this task).
- Code: `logai2/jobs/reread.py` (`LaneHour`, `Lane._close`), `logai2/owner/owner.py` (`SQL["lane_delete"]`,
  `_flush_lane`, the bk01 recorder: `SQL["lab_minute_counts"]`, the pending dict and its rules),
  `tests/test_reread.py` (helpers and the live-vs-lane equality tests), `tests/test_recorder.py` (bk01).

## Files
- Modify: `logai2/jobs/reread.py`, `logai2/owner/owner.py`, `tests/test_reread.py`, `ARCHITECTURE.md`
Touching any other file needs a QUESTION first.

## Interfaces
- Consumes: bk01's `lab_minute_counts`, `cfg.lab.record`, `cfg.lab.days`, the `*minutes` key rule.
- Produces: `LaneHour.minutes: dict[str, list[int]]` (key → 60 counts, slot = local minute), filled for every hour.

## Requirements
1. `Lane._close` fills `h.minutes[key][slot]` for every `(service, key), n` with `n > 0` of a kept service, and
   `h.minutes["*minutes"][slot] = 1` for every closed minute where it counts `n_minutes` (same condition:
   `key == "*total"`). `slot = local(cm.minute).minute`. Filled whatever the flag (the lane does not know the config;
   it is cheap).
2. `_flush_lane`: `lab_minute_counts` is deleted with the hour's other replaced tables (add it to `lane_delete`, so the
   delete always runs, flag on or off). Then, when `cfg.lab.record` and `h.day >= today - lab.days`, the hour's rows
   (all-zero rows skipped) are inserted with `copy_upsert(cur, "lab_minute_counts", rows)` (the upsert adds to nothing
   because the hour was just deleted).
3. Live pending recorder rows for the same hour in the same flush: the live part of `flush()` runs before
   `_flush_lane`, so the lane's delete-then-insert wins. That is the existing rule for `hourly_counts`; keep it and
   say so in a comment.
4. ARCHITECTURE: the re-read section lists `lab_minute_counts` among the replaced tables.

## Tests (write first, see them fail)
In `tests/test_reread.py`, reusing its helpers:
1. `test_lane_replaces_lab_rows`: an hour with recorded rows (including a key that does not occur in the re-read) is
   re-read → exactly the lane's rows remain; the stale key is gone.
2. `test_lane_lab_rows_equal_live`: the same synthetic docs read live (recorder on) and through the lane → identical
   `lab_minute_counts` rows (all keys, all slots), and `*minutes` sums equal `service_hours.n_minutes`.
3. `test_lane_gone_service_writes_nothing`: a skipped service (its pattern lost the window) writes no lab rows for that hour.
4. `test_lane_flag_off_deletes_only`: flag off → the hour's existing lab rows are deleted and none are written.
5. `test_lane_old_day_not_written`: an hour older than `today - lab.days` writes no lab rows and does not raise.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes, output clean (report the count)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
