# Task pm4: Hide Off services in the web and the linker

Implementer: Sonnet 5.5 — many small, similar query edits behind one flag.
Reviewer: none (user ruling 2026-10-29); the tech lead's final review (Opus) is the gate; the user clicks through in Chrome.
Branch: task/pm4-hide-off-services (worktree ../logai2-task-pm4, created by the tech lead from build/pattern-switch)
PR file: .team/prs/task-pm4-hide-off-services.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
A service whose pattern is Off (or whose pattern was removed and no other pattern reads it) is no longer monitored
(pm3 sets `services.monitored = false`). The user decided it is hidden completely: every page, API and picker, the
Alerts page, and the linker (no embeddings / LLM work for it). Its data stays in Postgres for when it is turned On.

## Read first
- Spec `docs/specs/2026-10-29-pattern-switch-memory.md` §2 D5, D6; §3.
- Plan `docs/plans/2026-10-29-pattern-switch-memory.md`: "Global Constraints", "Task pm4".
- Code: `logai2/web/app.py` (every route listed below, `known_service`, `known_template`, `document_fields`),
  `logai2/web/pipeline_view.py`, `logai2/web/baseline_view.py` (entry points only), `logai2/web/modules_view.py`,
  `logai2/module_alerts.py`, `logai2/linker.py`, `logai2/resolver.py`, `logai2/links.py`,
  `logai2/migrations/012_services_monitored.sql`, `tests/test_web_*.py` (fixtures and helpers), `tests/test_linker.py`.

## Files
- Modify: `logai2/web/app.py`, `logai2/web/pipeline_view.py`, `logai2/module_alerts.py`, `logai2/linker.py`,
  `logai2/resolver.py`, `docs/WEB_UI.md`
- Create: `tests/test_web_monitored.py`
- Modify (tests): `tests/test_linker.py`
Touching any other file needs a QUESTION first (the JS needs no change: it renders what the API returns).

## Interfaces
- Consumes: `services.monitored boolean NOT NULL DEFAULT true` (migration 012).
- Produces: `MONITORED = "{} IN (SELECT service FROM services WHERE monitored)"` in `logai2/web/app.py`, used as
  `MONITORED.format("<service column>")`; `known_service` / `known_template` abort 404 `"Unknown service"` for an
  unmonitored service.

## Requirements
1. Exactly the plan's pm4 steps.
2. Per-service routes 404 for an unmonitored service: everything that goes through `known_service` /
   `known_template`, plus `/api/modules`, `/api/baseline/groups` (route them through `known_service`), the
   `/api/templates/<service>/...` routes, and the documents `service` field check (400 as today, "must be a known
   service": unmonitored is not known).
3. Lists exclude unmonitored services: `/api/services`, `/api/alerts`, `/api/module-alerts`, `/api/templates`
   (with and without `service=`), `/api/template-levels`, `/api/documents` (`linked_templates` count and
   `template_level`), `/api/retrain` (`services`), `/api/pipeline` (`services` rows and the per-service counts).
4. Linker: the template SELECT and the pending count in `_embed_templates` / `cycle`, and the resolver's candidate
   SELECT, skip unmonitored services. `links.py` stays as it is (layer 1-2 SQL over already embedded templates; say
   in the PR file why that is fine or raise a QUESTION if you find it is not).
5. Grep every `FROM services`, `FROM templates`, `FROM alert_state`, `FROM alert_changes`, `FROM modules`,
   `template_modules` and `template_doc_links` in `logai2/web/`, `logai2/module_alerts.py`, `logai2/linker.py`,
   `logai2/resolver.py`: list each one in the PR file as "filtered" or "not needed: <reason>". The Evaluation (lab)
   routes are out of scope.
6. `docs/WEB_UI.md`: one short section "Off services" (what is hidden, 404, data kept).
7. Tests: `/home/cong/Documents/logai2/.venv/bin/pytest` from inside the worktree (check `logai2.__file__`). No full
   suite: only the focused set of the plan (pm4 Step 4), once at the end.

## Tests (write first, see them fail)
The plan's pm4 Step 1 tests (`test_off_service_absent_from_lists` parametrized over every list route in requirement
3, `test_off_service_routes_404` over the per-service routes in requirement 2, `test_template_levels_and_module_alerts_skip_off`,
`test_linker_skips_off_templates`), plus `test_documents_service_field_rejects_off_service` and
`test_resolver_skips_off_templates`. Seed one monitored service `sa` and one unmonitored `sb`, both with templates,
an ALERTING alert_state row, modules, a doc link and hourly rows, and assert `sb` appears nowhere in the JSON.

## Done when
- [ ] new tests fail before the code and pass after
- [ ] the focused set passes, output clean
- [ ] the grep list (requirement 5) in the PR file
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
