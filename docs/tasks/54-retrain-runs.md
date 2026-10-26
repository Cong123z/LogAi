# Task 54: Migration 008 `retrain_runs`; every Rebuild recorded as a run

Implementer: Sonnet 5.5 — touches the commit transaction and engine triggers.
Reviewer: a fresh Sonnet 5.5 subagent
Branch: task/54-retrain-runs (worktree ../logai2-task-54, created by the tech lead)
PR file: .team/prs/task-54-retrain-runs.md (from docs/templates/pr.md; .team/ is in the main checkout
~/Documents/logai2, write it there)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below
is unclear, stop and reply `QUESTION: ...` instead of guessing. Never kill processes you did not start
(other agents share the test Postgres).

## Why
The Retrain page (task 59) shows a history of every retrain, succeeded or failed, with steps, times, the
error and the warnings logged while it ran. Today the daily commit (`logai2/jobs/commit.py`) leaves only
`settings.last_commit`. This task adds the `retrain_runs` table and records every Rebuild (nightly, manual,
missed-at-start, first catch-up) as a run. Task 57 will add Re-read runs to the same table with the same
helpers.

## Read first
- Spec: docs/specs/2026-10-26-retrain.md §2, §5. Plan: docs/plans/2026-10-26-retrain.md task 54.
- Mockup (history + Run details panel): https://claude.ai/artifact/VdaHzwwbkVPkWYDMAofnsa boards 1 and 4.
- Code: `logai2/jobs/commit.py` (whole), `logai2/engine.py` (`start` missed-commit branch, `tick` first
  catch-up branch and `_commit_due` branch, `_commit`, `_run`), `logai2/db.py` (`migrate`,
  `drop_partitions_before`, `put_setting`), `logai2/migrations/007_template_modules.sql` (style),
  `tests/test_commit.py`, `tests/test_engine.py` (`make`, `test_recompute_command`, `test_missed_commit_runs_on_start`),
  `tests/test_db.py`.

## Files
- Create: `logai2/migrations/008_retrain_runs.sql`, `logai2/jobs/runs.py`, `tests/test_runs.py`
- Modify: `logai2/jobs/commit.py`, `logai2/engine.py`, `tests/test_commit.py`, `tests/test_engine.py`,
  `tests/test_db.py` (only if it lists migrations), `ARCHITECTURE.md` (§9, §11 schema table)
Touching any other file needs a QUESTION first.

## Interfaces
Migration 008 (exactly):
```sql
CREATE TABLE retrain_runs (id bigserial PRIMARY KEY, kind text NOT NULL, trigger text NOT NULL,
  scope jsonb NOT NULL DEFAULT '{}', state text NOT NULL, started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz, pos_ms bigint, steps jsonb NOT NULL DEFAULT '[]', result jsonb NOT NULL DEFAULT '{}',
  error text, error_type text, failed_step text, log jsonb NOT NULL DEFAULT '[]');
CREATE INDEX ON retrain_runs (started_at DESC);
```
`logai2/jobs/runs.py` (each call is its own `conn.transaction()`; the caller passes an idle connection):
- `start(conn, kind: str, trigger: str, scope: dict | None = None) -> int` — inserts `state='running'`.
- `step(conn, run_id: int, key: str, label: str, detail: dict, started_at: float, finished_at: float | None)`
  — upserts the step with this `key` in `steps` (list of `{key, label, started_at, finished_at, detail}`,
  epoch seconds), keeping order of first appearance.
- `finish(conn, run_id: int, state: str, result: dict | None = None, error: str | None = None,
  error_type: str | None = None, failed_step: str | None = None, log: list[dict] | None = None)`.
- `class RunLog(logging.Handler)`: level WARNING; keeps up to 50 records as
  `{"ts": epoch s, "level": name, "logger": name, "message": formatted}` in `.lines`; `attach()` /
  `detach()` add/remove it on the `logai2` logger.
`logai2/jobs/commit.py`:
- `commit(conn, cfg, today, rebuild: bool = True, on_step=None) -> dict`. Result keys: `days_committed`,
  `baseline_rows`, `rows_before` (rows before the rebuild; with `rebuild=False` the baseline is untouched and
  `baseline_rows == rows_before`). `on_step(key, label, detail, started_at, finished_at)` is called after
  the transaction commits, once per step, in order: `mark` ("Mark finished days", `{days}`), `rebuild`
  ("Rebuild baseline", `{rows_before, rows}`; omitted when `rebuild=False`), `housekeeping`
  ("Partitions and retention", `{partitions_dropped: [names], runs_deleted: n}`).
- On an exception inside, set `exc.step = <key of the step that was running>` and re-raise (nothing is
  committed, as today).
- Housekeeping also runs `DELETE FROM retrain_runs WHERE started_at < now() - interval '365 days'`.
Later tasks rely on: `runs.start/step/finish`, `RunLog`, `commit(..., rebuild=..., on_step=...)`.

## Requirements
1. `Engine._commit(trigger: str)`: `runs.start(self.db, "rebuild", trigger)`, attach a `RunLog`, call
   `commit(..., on_step=...)` writing each step with `runs.step`; on success `runs.finish(..., "succeeded",
   result, log=...)`; on exception `runs.finish(..., "failed", error=str(exc), error_type=type(exc).__name__,
   failed_step=getattr(exc, "step", None), log=...)` and re-raise. Detach the handler in `finally`. The
   rest of `_commit` (flush first, owner's day, reload baseline, last_commit) is unchanged.
2. Triggers: the first catch-up in `tick` → `first_start`; the due nightly commit → `schedule`; the missed
   commit in `start` → `startup`; the `recompute_baseline` command → `manual`.
3. The command result for `recompute_baseline` keeps `days_committed` and `baseline_rows` (the web reads
   them) and adds `run_id`.
4. No behaviour change to what the commit computes; all existing commit/engine tests pass unchanged except
   where a test checks the exact result dict.
5. ARCHITECTURE §9 (runs recorded, steps, retention 365 days), §11 (new table row).

## Tests (write first, see them fail)
- `test_migration_008` (test_db or test_runs): the table exists with the columns above after `migrate`.
- `test_runs_step_upserts_by_key` and `test_runs_finish_sets_state_and_log`.
- `test_run_log_keeps_warnings`: WARNING and ERROR from a `logai2.x` logger are kept, INFO is not, max 50.
- `test_rebuild_run_recorded` (engine): `recompute_baseline` command → one row, `kind='rebuild'`,
  `trigger='manual'`, `state='succeeded'`, steps keys `["mark", "rebuild", "housekeeping"]` with finished
  times, `result.baseline_rows` set; the command result has `run_id`.
- `test_failed_rebuild_records_error_and_step`: make the REBUILD statement fail (monkeypatch as in
  `test_failed_commit_rolls_back_everything`) → row `state='failed'`, `failed_step='rebuild'`, `error_type`
  set, baseline unchanged, the exception still propagates from `commit` / is reported by the command.
- `test_triggers_recorded`: first catch-up → `first_start`; missed at start → `startup`; due nightly →
  `schedule` (reuse the existing engine tests' setups).
- `test_old_runs_deleted`: a run with `started_at` 366 days ago is gone after a commit; one 364 days ago stays.
- `test_rebuild_false_keeps_baseline`: `commit(..., rebuild=False)` marks days and keeps baseline rows as they were.

## Done when
- [ ] new tests fail before the code and pass after
- [ ] `.venv/bin/pytest -q` passes, output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
