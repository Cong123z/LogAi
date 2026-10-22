# Task 43: GET /api/module-alerts, optional token, docs, deploy config

Implementer: Sonnet 5.5 — the public contract and the security check of the module alert API.
Reviewer: a fresh Sonnet 5.5 (spec + quality); final review Opus.
Branch: task/43-module-route (worktree ../logai2-task-43, created by the tech lead). The branch already
contains tasks 41 and 42 (merge commits eef347f, bc12fb2); master does not yet. Do not rebase.
PR file: /home/cong/Documents/logai2/.team/prs/task-43-module-route.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below
is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Sub-project 4. Tasks 41-42 built the data and the query (`logai2/module_alerts.py`). This task
exposes it to other services as HTTP, with an optional shared bearer token, and documents it.

## Read first
- Spec: docs/specs/2026-10-22-module-alert-api.md §2 (request), §4 (response, field meanings),
  §5 (errors), §8 (config and deploy).
- Plan: docs/plans/2026-10-20-module-alert-api.md **Task 43** (requirements 1-4, tests).
- Code: logai2/module_alerts.py (`parse_window(args, now, retention_days)` returns
  `(module_code, from_s, to_s, start, end)`; `data_until(conn)`; `module_alerts(conn, module_code,
  start, end, warm_consecutive=2)` — pass `cfg.alert.warm_consecutive`; times are epoch seconds,
  items as in the plan Task 42 rule 9; `series` items have `minute` or `hour`), logai2/web/app.py
  (`create_app`, `conn()`, the `BadRequest` handler returning `{"error": str}`), logai2/config.py
  (`WebConfig`, `load_config` env block), tests/test_web_alerts_templates.py (app/client fixtures),
  tests/test_module_alerts.py (how rows are seeded), docker-compose.yml, k8s/logai-web.yaml.

## Files
- Modify: logai2/config.py, logai2/web/app.py, docs/WEB_UI.md, README.md, ARCHITECTURE.md,
  docker-compose.yml, k8s/logai-web.yaml
- Test: tests/test_web_module_alerts.py (new), tests/test_config.py
Touching any other file needs a QUESTION first.

## Requirements
Plan Task 43 requirements 1-4, plus:
1. Response JSON exactly as spec §4: `module_code`, `from`, `to`, `effective_from`, `effective_to`,
   `resolution: "minute"`, `data_until`, `unit: "events_per_minute"`, `templates`. Every time via one
   helper `iso(ts)`: UTC with `Z`; milliseconds printed only when non-zero (`2026-09-30T07:00:30.500Z`,
   `2026-09-30T07:00:00Z`); `None` → `null`. Inside items: `periods[].start/end`, `series[].minute` or
   `series[].hour` formatted the same way.
2. 400 for every `ValueError` of `parse_window` (body `{"error": <message>}` via the existing handler).
   401 `{"error": "unauthorized"}` when a token is configured and the header is missing or wrong;
   compare with `hmac.compare_digest`. The token check runs before parsing.
3. Compose: the token reaches **logai2-web only** (`LOGAI2_API_TOKEN: ${LOGAI2_API_TOKEN:-}`; if the shared
   env anchor makes that awkward, use a `<<: *anchor` merge for the web service). k8s: env
   `LOGAI2_API_TOKEN` from Secret `logai-env` key `LOGAI_API_TOKEN`, `optional: true`. No token value in any file.
4. Docs:
   - WEB_UI.md: an HTTP API section with the request, the response, the errors, outward rounding with
     examples, the WARMING/ALERTING rule and `peak_state`, `end: null` = open at the window's end,
     `data_until`, the hourly series for windows > 1 day (hours in the deployment zone, +07), the
     token, and that DEBUG/INFO templates never alert.
   - README: the env table and a short "Module alert API" section with a `curl` example.
   - ARCHITECTURE: §1 sub-project 4 built, §3 env table, §14 "one shared token, no per-caller keys".

## Tests (write first, see them fail)
Plan Task 43 Step 1 (test_api_token_from_env, test_shape, test_unknown_module_empty_200,
test_bad_input_400 with all 7 cases, test_module_code_unicode_and_slash_round_trip,
test_data_until_null_without_engine_row, test_token). In test_shape also assert one `periods` item's
`start` string, one `series` item's `minute` string and `data_until` when a worker_status row exists.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN output in the PR file)
- [ ] `.venv/bin/pytest -q` passes (branch baseline with 41+42: 360 passed), output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
