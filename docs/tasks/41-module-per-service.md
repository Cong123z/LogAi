# Task 41: engine — a module per service, and the streak's first minute

Implementer: Sonnet 5.5 — touches the owner's exactly-once flush path and a schema migration.
Reviewer: a fresh Sonnet 5.5 (spec + quality); final review Opus.
Branch: task/41-module-per-service (worktree ../logai2-task-41, created by the tech lead)
PR file: /home/cong/Documents/logai2/.team/prs/task-41-module-per-service.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below
is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Sub-project 4, the module alert API (other services ask which templates of module M alerted
in a window). The API needs two engine facts that are missing today: a module seen in two
services must be recorded for both, and the first high minute of a streak (state NORMAL,
streak 1) must have `alert_minutes` rows so an incident is returned from its first minute.

## Read first
- Spec: docs/specs/2026-10-22-module-alert-api.md §3 (what counts) and §7 (engine changes).
- Plan: docs/plans/2026-10-20-module-alert-api.md, "Facts from the current code" and **Task 41**
  (requirements, tests, SQL). The plan section is the requirements; this brief does not repeat it.
- Code: logai2/owner/owner.py (`Owner.__init__` modules load, `_see`, `handle` alert_minutes loop,
  `flush`), logai2/owner/detect.py (`step`, `Detector.run`), logai2/migrations/001_init.sql (modules),
  logai2/db.py (`migrate`), tests/test_owner.py helpers (`make`, `run_minutes`, `lines`, `rows`),
  tests/test_detect.py (how a committed baseline is set up for a high minute).

## Files
- Create: logai2/migrations/006_modules_per_service.sql
- Modify: logai2/owner/owner.py, ARCHITECTURE.md (§7.2 or §8.5: the alert_minutes rule now includes
  NORMAL with streak > 0; §11: modules key `(service, module_code)` and migration 006 in the lists;
  §16 migration list; README.md "Repo layout" migrations line)
- Test: tests/test_db.py, tests/test_owner.py
Touching any other file needs a QUESTION first.

## Interfaces
- Consumes: `Owner`, `Detector.states` (`AlertRow.state`, `.streak`), `SQL["modules"]`.
- Produces: `modules` PK `(service, module_code)` + index `modules_code (module_code)`;
  `alert_minutes` rows for template keys whose row after the step is non-NORMAL **or** NORMAL with
  `streak > 0` (historic minutes still write none). Task 42 relies on both.

## Requirements
Plan Task 41 requirements 1–3, exactly. Detection, alert states, alert_changes, metrics and the
flush order must not change.

## Tests (write first, see them fail)
Plan Task 41 Step 1: test_module_can_belong_to_two_services, test_module_rows_per_service,
test_module_set_survives_restart, test_first_high_minute_in_alert_minutes,
test_single_high_minute_writes_one_row_and_no_change.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN output in the PR file)
- [ ] `.venv/bin/pytest -q` passes (baseline on master: 311 passed), output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
