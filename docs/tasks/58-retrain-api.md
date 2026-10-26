# Task 58: Retrain HTTP API

Implementer: Sonnet 5.5 — Flask routes over tables and engine commands that exist after tasks 54-57.
Reviewer: a fresh Sonnet 5.5 subagent
Branch: task/58-retrain-api (worktree ../logai2-task-58, created by the tech lead)
PR file: .team/prs/task-58-retrain-api.md (from docs/templates/pr.md; .team/ is in the main checkout
~/Documents/logai2, write it there)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below is unclear, stop and reply
`QUESTION: ...` instead of guessing. Never kill processes you did not start.

## Why
The Retrain page (task 59) needs one read endpoint for everything it shows, a schedule save, and actions that
queue engine commands. The web never talks to ES or to the engine directly: it reads `retrain_runs`,
`settings`, `worker_status` and queues `commands`.

## Read first
- Spec: docs/specs/2026-10-26-retrain.md §1, §4-§6. Plan: task 58. Mockup boards 1-4:
  https://claude.ai/artifact/VdaHzwwbkVPkWYDMAofnsa
- Code: `logai2/web/app.py` (`setting`, `save`, `engine_status`, `body`, error handlers, `recompute`, `command`,
  `int_arg`), `logai2/commands.py` (KINDS incl. `retrain_run`, `retrain_cancel`, `retrain_estimate`),
  `logai2/engine.py` (`_reread_run`, `_estimate`, `_run`, status keys `schedule`, `next_commit_at`,
  `es_oldest`, `heartbeat`), `logai2/jobs/reread.py` (`Scope.from_json` — the payload shape),
  `logai2/migrations/008_retrain_runs.sql`, `tests/test_web_sources.py` (client fixture), `docs/WEB_UI.md`.

## Files
- Modify: `logai2/web/app.py`, `docs/WEB_UI.md` (HTTP API section)
- Create: `tests/test_web_retrain.py`
Touching any other file needs a QUESTION first.

## Interfaces (all JSON)
- `GET /api/retrain` →
  `{"schedule": {enabled, time, weekdays}, "revision": str|null, "saved": bool, "next_run_at": epoch|null,
    "timezone": "Asia/Ho_Chi_Minh", "current": run|null, "runs": [run, ...] (last 50, newest first, without `log`),
    "es_oldest": {pattern: ms|null}, "services": [names], "llm_active": bool, "engine_stale": bool,
    "default_rate": int}`.
  `schedule` = the saved setting merged over defaults (`enabled` true, `cfg.baseline.commit_time`, all 7 days);
  `next_run_at` from engine status `next_commit_at`; `current` = the `state='running'` run, else null;
  `services` from the `services` table; `llm_active` = any active LLM profile (same query the LLM page uses);
  `engine_stale` same rule as `/api/pipeline` (`STALE_HEARTBEAT_SECONDS`).
  A `run` = `{id, kind, trigger, scope, state, started_at, finished_at, steps, result, error, error_type,
  failed_step}` with times as epoch seconds.
- `GET /api/retrain/runs/<id>` → run + `log`; 404 unknown.
- `PUT /api/retrain/schedule` body `{"schedule": {...}, "revision": str|null}` → 200 with the GET payload;
  409 on a stale revision (existing handler); 400 when `enabled` not bool, `time` not `HH:MM` (00-23, 00-59),
  `weekdays` not a non-empty list of distinct ints 0-6 (an empty list is refused: "choose at least one day").
  Stored normalised (`weekdays` sorted). No command needed (the engine re-reads every 60 s).
- `POST /api/retrain/runs` body `{"kind": "rebuild"}` → enqueue `recompute_baseline`, 202 `{"command_id"}`;
  body `{"kind": "reread", "services": [..]|null, "days": ["YYYY-MM-DD", ...], "hour_from": int, "hour_to": int}`
  → 400 unless: days non-empty, distinct, each between today-7 and today (local); `0 <= hour_from < hour_to <= 24`;
  services null or a non-empty list of known services. 409 `{"error": "a retrain is running"}` when a
  `kind='reread'` run is `running`. Else enqueue `retrain_run` with the `Scope.to_json()` shape, 202
  `{"command_id"}`. (The engine's command result holds `run_id` or `error`; the client polls
  `GET /api/commands/<id>`.)
- `POST /api/retrain/estimate` same reread body (same validation, no 409) → enqueue `retrain_estimate`, 202
  `{"command_id"}`.
- `POST /api/retrain/runs/<id>/cancel` → 404 unknown; 409 unless it is a running `reread`; else enqueue
  `retrain_cancel` `{"run_id": id}`, 202 `{"command_id"}`.
- `POST /api/baseline/recompute` unchanged.

## Requirements
1. Every route above with the exact status codes and shapes; all writes commit with their command in one
   transaction (as `save` does).
2. Local "today" = `timeutil` local date of now.
3. `docs/WEB_UI.md` HTTP API: each route with request/response examples.

## Tests (write first, see them fail)
`test_get_retrain_defaults`, `test_get_retrain_current_and_runs_order_without_log`, `test_run_detail_has_log`,
`test_schedule_save_and_409`, `test_schedule_validation_400` (time, weekdays empty/duplicate/out of range,
enabled), `test_run_rebuild_enqueues_recompute`, `test_run_reread_enqueues_scope`, `test_run_reread_validation_400`
(days empty/future/too old/bad format, hours, unknown service), `test_run_reread_409_while_running`,
`test_estimate_enqueues`, `test_cancel_404_409_202`, `test_llm_active_flag`, `test_engine_stale_flag`.

## Done when
- [ ] new tests fail before the code and pass after
- [ ] `.venv/bin/pytest -q` passes, output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
