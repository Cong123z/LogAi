# Task 42: window parser and the module query

Implementer: Sonnet 5.5 — SQL plus time math (minute ends vs starts, back-dating, clamps, partitions).
Reviewer: a fresh Sonnet 5.5 (spec + quality), then an Opus adversarial review; final review Opus.
Branch: task/42-module-query (worktree ../logai2-task-42, created by the tech lead)
PR file: /home/cong/Documents/logai2/.team/prs/task-42-module-query.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below
is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Sub-project 4, the module alert API. This task is its core: parse the caller's window
(millisecond input, 1-minute resolution rounded outward, clamped to retention) and compute,
for module M, the templates that were WARMING or ALERTING in it with M's counts. Task 43 wraps
it in a route; no web code here.

## Read first
- Spec: docs/specs/2026-10-22-module-alert-api.md §2 (request, window, range), §3 (what counts),
  §4 (fields; times stay epoch seconds here), §6 (computation).
- Plan: docs/plans/2026-10-20-module-alert-api.md, "Facts from the current code" and **Task 42**
  (interfaces, parse_window rules 1–5, module_alerts rules 1–9, tests). The plan section is the requirements.
- Code: logai2/review.py (`periods`, its callers in review.py and logai2/web/baseline_view.py),
  logai2/owner/detect.py (`step`: when WARMING/ALERTING/COOLING happen, `at` = minute end),
  logai2/timeutil.py, logai2/links.py (`ALERTING_SOURCES`), logai2/migrations/001_init.sql
  (alert_changes, alert_minutes partitioned by day, modules), logai2/db.py (`ensure_partitions`),
  tests/test_review.py, tests/test_web_baseline_view.py (how rows are seeded directly).

## Files
- Create: logai2/module_alerts.py, tests/test_module_alerts.py
- Modify: logai2/review.py
- Test: tests/test_review.py, tests/test_module_alerts.py
Touching any other file needs a QUESTION first. Note: Task 41 runs in parallel and changes the
`modules` primary key to `(service, module_code)`; do not depend on it. Seed `modules` rows with
distinct `module_code` per service where the old PK forbids two (or insert only what the old PK
allows) — test_module_in_two_services may need `ALTER TABLE modules DROP CONSTRAINT modules_pkey`
inside the test schema; say so in the PR file.

## Interfaces
Exactly as plan Task 42 "Interfaces": `review.NON_NORMAL`, `review.periods(changes, end_now,
states=NON_NORMAL)`, `module_alerts.parse_window(args, now, retention_days)`,
`module_alerts.data_until(conn)`, `module_alerts.module_alerts(conn, module_code, start, end)`.
Task 43 imports these names. For `warm_consecutive`, pick parameter-with-default or `cfg`, and state the choice in the PR.

## Requirements
Plan Task 42 parse_window rules 1–5 and module_alerts rules 1–9, exactly. The default behaviour
of `review.periods` must not change: every existing test passes untouched.

## Tests (write first, see them fail)
Plan Task 42 Step 1, all of them (test_periods_states_argument, the eleven test_module_alerts
cases, test_parse_window). Fill in the `...`/comment-only tests with real seeds and assertions.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN output in the PR file)
- [ ] `.venv/bin/pytest -q` passes (baseline on master: 311 passed), output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
