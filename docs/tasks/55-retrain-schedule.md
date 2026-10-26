# Task 55: Retrain schedule setting (on/off, time, week days)

Implementer: Sonnet 5.5 — engine scheduling around the daily commit.
Reviewer: a fresh Sonnet 5.5 subagent
Branch: task/55-retrain-schedule (worktree ../logai2-task-55, created by the tech lead)
PR file: .team/prs/task-55-retrain-schedule.md (from docs/templates/pr.md; .team/ is in the main checkout
~/Documents/logai2, write it there)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below
is unclear, stop and reply `QUESTION: ...` instead of guessing. Never kill processes you did not start
(other agents share the test Postgres).

## Why
The Retrain page has a one-line schedule: nightly rebuild on/off, time, week days (mockup board 1,
https://claude.ai/artifact/VdaHzwwbkVPkWYDMAofnsa). Today the time is config-only (`baseline.commit_time`) and
the commit runs every day. The daily commit also does housekeeping (marks finished days, rolls and drops
partitions, retention), which must keep running every night even when the rebuild is off.

## Read first
- Spec: docs/specs/2026-10-26-retrain.md §4. Plan: docs/plans/2026-10-26-retrain.md task 55.
- Code (after task 54, on master): `logai2/engine.py` (`__init__` `commit_at`, `start` missed-commit branch,
  `tick` first catch-up and `_commit_due` branches, `_refresh`, `_commit_due`, `_commit(trigger)`, `_run`),
  `logai2/jobs/commit.py` (`commit(conn, cfg, today, rebuild=True, on_step=None)`), `logai2/db.py`
  (`get_setting`, `put_setting`), `tests/test_engine.py` (`make`, `Clock`, `test_missed_commit_runs_on_start`,
  `test_no_commit_before_commit_time`, `test_daily_commit_uses_owner_day_not_wall_day`).

## Files
- Modify: `logai2/engine.py`, `tests/test_engine.py`, `ARCHITECTURE.md` (§9, §10 engine loop, §11 settings table)
Touching any other file needs a QUESTION first.

## Interfaces
- Setting `retrain_schedule` (written later by the web, task 58):
  `{"enabled": bool, "time": "HH:MM", "weekdays": [int]}` — weekdays 0 = Monday … 6 = Sunday.
  Missing setting, or any missing / invalid field → that field's default: `enabled` true, `time`
  `cfg.baseline.commit_time`, `weekdays` `[0, 1, 2, 3, 4, 5, 6]`. An invalid value logs one warning.
- `Engine.schedule` (dict, the effective schedule) — read at `start()` and again in `_refresh` every
  `index_refresh_seconds` (no restart needed; up to 60 s stale is fine). Status gains `"schedule": {...}` and
  `"next_commit_at": epoch s` (the next time `_commit_due` becomes true; also when the rebuild will be skipped
  that day). Task 58/59 read these.
- `_commit(trigger, rebuild=True)`: with `rebuild=False` it calls `commit(..., rebuild=False)`, writes **no**
  `retrain_runs` row, and still updates `last_commit` and reloads nothing (baseline unchanged).

## Requirements
1. The nightly commit becomes due at `schedule.time` on the owner's day (replace `commit_at` from config).
2. If `enabled` is false or the owner's day's weekday is not in `weekdays`: at that time run
   `_commit("schedule", rebuild=False)` (housekeeping only, `last_commit` advances, so it runs once per day).
   Otherwise `_commit("schedule")` as today.
3. Missed run at start (`start()`): same rule, trigger `startup`.
4. The first catch-up commit (`first_start`) and the `recompute_baseline` command (`manual`) always rebuild,
   whatever the schedule.
5. Docs: ARCHITECTURE §9/§10 (schedule rules, housekeeping always runs), §11 settings table (`retrain_schedule`),
   status fields (`schedule`, `next_commit_at`). Note that `baseline.commit_time` is now only the default.

## Tests (write first, see them fail)
- `test_schedule_time_from_setting`: setting time "03:30" → not due at 02:00, due at 03:30 (owner's day).
- `test_disabled_schedule_still_housekeeps`: enabled false → at the time, `last_commit` advances, `day_status`
  gains the finished day, baseline rows unchanged, no `retrain_runs` row; next tick not due again.
- `test_weekday_off_skips_rebuild`: weekdays without the owner's day's weekday → housekeeping only; with it →
  a `schedule` run row.
- `test_schedule_change_picked_up_without_restart`: change the setting, advance the clock past
  `index_refresh_seconds`, the new time applies.
- `test_invalid_schedule_falls_back_to_defaults`: `{"time": "25:99", "weekdays": "x"}` → defaults, one warning.
- `test_manual_and_first_start_always_rebuild`: schedule disabled → `recompute_baseline` still writes a
  `manual` rebuild run.
- `test_status_next_commit_at`.

## Done when
- [ ] new tests fail before the code and pass after
- [ ] `.venv/bin/pytest -q` passes, output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
