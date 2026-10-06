# AI Insights: on-demand LLM analysis, template triage, document assignment

Agreed with the user on 2026-10-06. The UI direction follows UI UX Pro Max: a dense, minimal dashboard in the app's existing dark tokens; every status has text, not just color; deletes ask for confirmation; actions confirm success briefly; empty states offer the next action; motion is minimal.

## Decisions

1. **The LLM is never called automatically.** Alert-episode auto-submit is removed. Every analysis is requested by a user from the AI Insights page.
2. **AI Insights page** (new sidebar view `#insights`):
   - three tabs: **Alerts** (one row per `(service, group)` window), **Services**, **Unknown templates** (pending templates);
   - a list on the left and a detail panel on the right; on narrow screens they stack.
   - The Alerts table keeps a compact AI column whose link opens the matching window in Insights.
   - The old service-analysis panel and AI modal on the Alerts tab are removed.
3. **Actions in the detail panel:**
   - **Analyze / Re-analyze**;
   - **Save as document**: creates the document **and assigns it to the group(s)**, which fixes the bug;
   - **Apply suggested group** (templates only), through the existing `PUT /api/templates/<id>/group`;
   - **Delete analysis**, with a confirmation dialog;
   - **Open document**, which jumps to the Documentation tab.
4. **Unknown-template triage.** The result has:
   - a verdict: `suspicious | benign | unsure`;
   - a confidence and the reasoning;
   - a suggested group: one of the up to 5 nearest groups by embedding, or `"new"`, or null.
   "Triage all unknown" queues up to 50 pending templates.
5. **Request file v2** (`analysis_requests.json`, web-owned): `{"schema_version": 2, "requests": {"<kind>:<id>": {"action": "analyze"|"delete", "at": ts}}}`.
   - `kind` is `window` (id = `json.dumps([service, group])`), `service` (id = name) or `template` (id = template_id).
   - v1 entries (`{service: ts}`) are read as `service:<name>` analyze requests.
6. **Engine processing.** Requests are handled on the 2s control thread, not the ES poll loop.
   - Analyze is restart-safe through the record's `requested_at`.
   - Delete removes the record (idempotent).
   - Results go to `incident_analysis.json` (window), `service_analysis.json` and `template_triage.json`, all engine-owned.
7. **`POST /api/documentation`** accepts `assign_group_ids: [..]`. After creating the document it sets a manual override for each existing group. If documentation mutations are blocked (grouping pending), the document is still created and the response says the assignment was skipped and why.

## Tasks (TDD, Native execution, then one fresh review)

1. `logai/incident/requests.py` v2: `add_request(path, kind, target_id, action="analyze", *, now=None)`, `load_requests(path) -> dict[str, tuple[str, float]]`, `request_key(kind, id)`, `parse_key(key) -> (kind, id)`; v1 compatibility; 24h prune.
2. `logai/incident/template_triage.py`: `build_template_evidence(template_id, templates, groups) -> (evidence, candidate_group_ids: dict[id, rep_template])` and `validate_template_reply(reply, candidates) -> dict`; `TEMPLATE_SYSTEM_PROMPT`.
3. Classifier:
   - a generic queued job runner;
   - `submit(..., requested_at=None)` and `process(..., requested_at=None)` keep `requested_at` in the window record;
   - `submit_template` / `process_template` with `template_store`;
   - `delete_record(kind, target_id) -> bool`;
   - the restart pending→failed conversion covers all three stores and keeps `requested_at`.
4. Pipeline:
   - remove the alert auto-trigger;
   - `_process_analysis_requests` dispatches by kind and action and runs from `_control_tick`;
   - a lock guards `_recent_params`;
   - unknown targets get a failed record (analyze) or are ignored (delete).
5. Web:
   - `GET /api/insights`;
   - `POST /api/insights/analyze {kind, id}` (`kind="templates_all"` queues all pending, capped at 50);
   - `POST /api/insights/delete {kind, id}`;
   - `POST /api/documentation` with `assign_group_ids`;
   - remove `/api/service-analysis`;
   - `/api/alerts` keeps `analysis` and `llm`.
6. UI: the Insights view, the Alerts AI column linking to Insights, removal of the old panel and modal; checked in headless Chrome.
7. Docs (ARCHITECTURE §7.6–7.8 updated), full suite, a live LLM check of template triage, then the final review.
