# LLM Service Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On-demand LLM analysis of a whole service (health, summary, ranked issues linked to docs or suggestions), requested from the Alerts tab and run by the engine.

**Architecture:**
- The web writes `analysis_requests.json`. The engine picks requests up each poll loop, builds the service evidence in memory, and runs it on the existing `IncidentClassifier` worker as a second job type.
- Results go to the engine-owned `service_analysis.json`. The web serves them through `GET /api/service-analysis` and renders a panel above the Alerts table.

**Tech Stack:** Python 3, `requests`, Flask, vanilla JS/CSS, pytest via `.venv/bin/python3 -m pytest`.

**Spec:** `docs/superpowers/specs/2026-10-06-llm-service-analysis-design.md`

## Global Constraints
- No new dependencies. The LLM key and LLM calls stay in the engine only.
- File ownership:
  - the web only writes `analysis_requests.json`;
  - the engine only writes `service_analysis.json`;
  - neither process writes the other's file.
- Caps:
  - groups: 20; candidates: 15; candidate text: 1000 characters;
  - templates per group: 3; values per slot: 5; value length: 200 characters;
  - issues: 10; summary: 4000; reasoning: 4000; issue title: 200;
  - suggestion: 200 / 20000 / 100; error: 500.
- `health` is one of `healthy|degraded|critical`, and anything else becomes `unknown`.
- `max_tokens` for service jobs is `max(config.llm.max_tokens, 2000)`.
- Requests older than 24 h are pruned on every web write.
- Nothing raises into the poll loop.
- Tests that need real numpy use the `real_numpy` fixture (`tests/conftest.py`).
- Run tests with `.venv/bin/python3 -m pytest -q`; the suite must stay green.

## Review Focus
1. **No group of the service has a centroid** (all pending or new). Candidates are `[]` and the analysis still runs. Test: Task 2 `test_evidence_without_centroids`.
2. **The LLM returns `issues` that isn't a list, or items that aren't dicts.** These are skipped without failing. Test: Task 2 `test_validate_malformed_issues`.
3. **The request file is corrupt or half-edited.** It reads as `{}` and never raises. Test: Task 1 `test_load_requests_corrupt`.
4. **The service name has spaces, quotes or Unicode.** It round-trips through POST, the request file and GET, and it renders escaped in the `<select>`. Test: Task 5 `test_post_unicode_service`; Task 6 headless check with the fixture service `pay "vn" ví`.
5. **Re-analyze after a done record.** A newer request runs again. Test: Task 4 `test_new_request_after_done_runs_again`.

---

### Task 1: Request file helpers and storage config

**Files:**
- Create: `logai/incident/requests.py`
- Modify: `logai/config.py` (`StorageConfig`), `config.yaml` (`storage:`)
- Test: `tests/test_service_analysis.py` (new)

**Interfaces:**
- Produces:
  - `load_requests(path: str | Path) -> dict[str, float]`: missing, corrupt or wrong-shape file → `{}`; non-numeric values are skipped.
  - `add_request(path: str | Path, service: str, requested_at: float, *, now: float | None = None) -> None`: atomic tmp + `os.replace`, writes `{"schema_version": 1, "requests": {...}}`, and prunes entries with `requested_at < now - 86400`.
  - `StorageConfig.service_analysis_file = "service_analysis.json"`, `StorageConfig.analysis_requests_file = "analysis_requests.json"`.

- [ ] **Step 1: Write the failing tests**
```python
def test_add_and_load_requests(tmp_path):
    p = tmp_path / "analysis_requests.json"
    add_request(p, "recharge", 1000.0, now=1000.0)
    assert load_requests(p) == {"recharge": 1000.0}
def test_add_request_prunes_old(tmp_path):
    add_request(p, "old", 1.0, now=1.0); add_request(p, "new", 90_000.0, now=90_000.0)
    assert load_requests(p) == {"new": 90_000.0}
def test_load_requests_corrupt(tmp_path):
    p.write_text("{not json"); assert load_requests(p) == {}
    p.write_text('{"requests": {"a": "x", "b": 2}}'); assert load_requests(p) == {"b": 2.0}
    assert load_requests(tmp_path / "missing.json") == {}
def test_storage_defaults():
    assert StorageConfig().service_analysis_file == "service_analysis.json"
    assert StorageConfig().analysis_requests_file == "analysis_requests.json"
```
- [ ] **Step 2: Run them and confirm they fail.** `.venv/bin/python3 -m pytest tests/test_service_analysis.py -q` → FAIL (ImportError).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and confirm they pass.** Same command → PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat: analysis request file helpers"`

---

### Task 2: Service evidence and reply validation (pure functions)

**Files:**
- Create: `logai/incident/service_analysis.py`
- Modify: `logai/alert/alert_state_machine.py` (add a helper)
- Test: `tests/test_service_analysis.py`

**Interfaces:**
- Consumes:
  - `summarize_parameters`, `PLACEHOLDER` value rules, and the suggestion rules from `logai/incident/classifier.py`. Extract the existing suggestion check from `IncidentClassifier._validate` into a module-level `clean_suggestion(raw: Any) -> dict | None`. Both `_validate` and the new code call it; it returns None when the suggestion is invalid.
  - `DocumentationMatcher.top_k`, `GroupRegistry`, `TemplateRegistry`, `LEVEL_RANK`/`DEFAULT_LEVEL`.
- Produces:
  - `AlertStateMachine.states_for_service(service: str) -> dict[str, tuple[str, float]]`: group_id → `(alert_state, anomaly_score)` for persisted `(service, g)` keys.
  - `build_service_evidence(service: str, groups: GroupRegistry, templates: TemplateRegistry, alert_states: dict[str, tuple[str, float]], recent_params: dict[str, list[tuple[str, list[str]]]], matcher: DocumentationMatcher) -> tuple[dict, dict[str, str], set[str]]`. Returns `(evidence, candidates: id→title, sent_groups)`. The evidence shape and ranking come from spec §2; the alert rank is ALERTING 3 > COOLING 2 > WARMING 1 > NORMAL/none 0.
  - `validate_service_reply(reply: dict, candidates: dict[str, str], sent_groups: set[str]) -> dict`. Returns `{"health", "summary", "issues": [{"title", "group_ids", "documentation_id", "document_title", "reasoning", "suggestion"}]}`. Raises `ValueError` when the summary is missing.
  - `SERVICE_SYSTEM_PROMPT: str`, with the spec §3 text plus the JSON shape.

- [ ] **Step 1: Write the failing tests.** Fixtures:
  - reuse `_matcher`, `CLASSIFIER_DOCS`, `FakeSession` and `_reply` from `tests/test_incident_classifier.py` via import (`tests/` is on `sys.path`);
  - registries in `tmp_path`, with service `recharge`;
  - groups `G1` (ERROR, 50 events, centroid `[1,0]`), `G2` (INFO, 900 events), and `G3`, which has only an `api` template plus a persisted `("recharge","G3")` ALERTING window.
```python
def test_evidence_groups_and_ranking():   # sent order: G3 (ALERTING) , G1 (ERROR), G2 (INFO); G3 included via window only
def test_evidence_per_service_counts():   # G1 has templates from recharge(50) and api(7): event_count == 50, templates only recharge's
def test_evidence_caps():                 # 25 groups -> len(groups)==20; candidates <=15 and deduped by id keeping max similarity
def test_evidence_without_centroids():    # no centroids -> evidence["candidates"] == [] and groups still present
def test_evidence_parameters_limited():   # params for a template not in the top-3 are excluded; values cut to 200
def test_states_for_service(tmp_path):    # store keys ["recharge","G3"] ALERTING 0.9, ["api","G1"] -> {"G3": ("ALERTING", 0.9)}
def test_validate_health_and_summary():   # health "meh" -> "unknown"; summary "" -> ValueError
def test_validate_issue_rules():           # unknown group ids filtered; "DOC-999"+suggestion -> doc None, suggestion kept; neither -> dropped; 12 issues -> 10; error_code None accepted; document_title filled
def test_validate_malformed_issues():      # issues "x" -> []; issues [1, None, {...valid}] -> only the valid one
```
- [ ] **Step 2: Run them and confirm they fail.** `.venv/bin/python3 -m pytest tests/test_service_analysis.py -q` → FAIL (ImportError).
- [ ] **Step 3: Implement.** Refactor `_validate` to use `clean_suggestion`. The existing classifier tests must stay green.
- [ ] **Step 4: Run the tests and confirm they pass.** `.venv/bin/python3 -m pytest tests/test_service_analysis.py tests/test_incident_classifier.py -q` → PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat: service analysis evidence and validation"`

---

### Task 3: Classifier: service job type

**Files:**
- Modify: `logai/incident/classifier.py`
- Test: `tests/test_service_analysis.py`

**Interfaces:**
- Consumes: Task 2 `SERVICE_SYSTEM_PROMPT` and `validate_service_reply`.
- Produces:
  - Constructor kwarg `service_store: JSONStore | None = None`. On init, the service store's `pending` records become `failed` with the error `"Interrupted by engine restart before analysis finished"`; `requested_at` is kept.
  - `_call(evidence: dict, system_prompt: str = SYSTEM_PROMPT, max_tokens: int | None = None) -> str`.
  - `submit_service(service: str, requested_at: float, evidence: dict, candidates: dict[str, str], sent_groups: set[str]) -> bool`:
    - returns False when `service_store` is None or `("service", service)` is already active;
    - writes `{"status": "pending", "service", "requested_at", "queued_at"}`;
    - enqueues the job. Queue items become tagged tuples: `("window", key, alert, params)` / `("service", service, requested_at, evidence, candidates, sent_groups)`.
    - A full queue writes a `failed` record and calls `on_result("dropped")`.
  - `process_service(service, requested_at, evidence, candidates, sent_groups) -> dict`:
    - never raises;
    - writes `{"status": "done", "service", "requested_at", "analyzed_at", "model", "health", "summary", "issues", "error": None}`, or a failed record that keeps `requested_at`;
    - calls `on_result("service_done"|"service_failed")`.

- [ ] **Step 1: Write the failing tests.**
```python
def test_process_service_done(tmp_path):    # FakeSession reply {"health":"degraded","summary":"s","issues":[{"title":"t","group_ids":["G1"],"documentation_id":"DOC-001","reasoning":"r"}]}
    # record status done, health degraded, issues[0]["document_title"]; session body max_tokens == 2000 and system prompt == SERVICE_SYSTEM_PROMPT
def test_process_service_failed_keeps_requested_at(tmp_path):  # reply "garbage" -> status failed, requested_at == 5.0, results == ["service_failed"]
def test_submit_service_pending_and_dedupe(tmp_path):  # True + pending record; second False; no service_store -> False
def test_service_restart_pending_to_failed(tmp_path):  # pre-seed service_analysis.json pending with requested_at 7.0 -> failed, requested_at 7.0
def test_worker_runs_both_job_types(tmp_path): # start(); submit window + submit_service; poll stores up to 3s -> both non-pending; stop()
```
- [ ] **Step 2: Run them and confirm they fail.** Same file → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and confirm they pass.** Then run `.venv/bin/python3 -m pytest -q` → all pass.
- [ ] **Step 5: Commit.** `git commit -m "feat: service analysis job in incident classifier"`

---

### Task 4: Engine pickup and heartbeat

**Files:**
- Modify: `logai/realtime/realtime_pipeline.py`
- Test: `tests/test_service_analysis.py`. Build the pipeline with the `_pipeline` helper imported from `tests/test_incident_classifier.py`, then set `p.incident_classifier.submit_service = MagicMock(return_value=True)` and point `service_store` at the real store.

**Interfaces:**
- Consumes: Task 1 `load_requests`; Task 2 `build_service_evidence` and `states_for_service`; Task 3 `submit_service` and `service_store`.
- Produces:
  - The classifier is built with `service_store=JSONStore(base/service_analysis_file)`.
  - Attributes `_analysis_requests: dict[str, float]`, `_analysis_requests_mtime: int | None`, `_handled_requests: dict[str, float]`, `_analysis_requests_path: Path`.
  - Method `_process_analysis_requests(self) -> None`, called right after each `_refresh_grouping_if_needed()` in `run_forever`. It wraps everything in try/except that logs at WARNING, and it does nothing when the classifier is None (use `getattr`).
  - The recent-parameter input for the service is `{g: list(dq) for (s, g), dq in self._recent_params.items() if s == service}`.
  - A service is unknown when `template_registry.count_by_service(service) == 0 and not alert_sm.states_for_service(service)`.
  - `_heartbeat_grouping`'s runtime dict gains `"llm_enabled": getattr(self, "incident_classifier", None) is not None`.

- [ ] **Step 1: Write the failing tests.**
```python
def test_request_submitted_once(tmp_path):        # template for "recharge" exists; add_request(...,"recharge",10.0); two calls -> submit_service called once with requested_at 10.0
def test_handled_across_restart(tmp_path):        # service_analysis.json has {"recharge": {"status":"done","requested_at":10.0}} -> new pipeline, request 10.0 -> not submitted
def test_new_request_after_done_runs_again(tmp_path):  # record requested_at 10.0, request 20.0 -> submitted once
def test_unknown_service_marked_handled(tmp_path):     # request "ghost" -> never submitted; second call also not
def test_active_service_retried(tmp_path):        # submit_service returns False then True -> called twice across two loops, then stops
def test_heartbeat_has_llm_enabled(tmp_path):     # p._last_grouping_heartbeat = 0; p._heartbeat_grouping(); load_status()["runtime"]["llm_enabled"] is True; endpoint "" -> False
def test_pickup_never_raises(tmp_path):           # build_service_evidence patched to raise -> _process_analysis_requests() returns None
```
- [ ] **Step 2: Run them and confirm they fail.** Same file → FAIL.
- [ ] **Step 3: Implement.** The mtime check uses `os.stat(path).st_mtime_ns`; a missing file means no requests.
- [ ] **Step 4: Run the tests and confirm they pass.** Then run `.venv/bin/python3 -m pytest -q` → all pass.
- [ ] **Step 5: Commit.** `git commit -m "feat: engine picks up service analysis requests"`

---

### Task 5: Web API

**Files:**
- Modify: `logai/web/app.py` (`create_app` kwargs `service_analysis_path=None`, `analysis_requests_path=None`, defaulting to `base / "<name>"`), `scripts/run_web.py` (pass both through `_storage_path(web_base, config.storage.<file>)`)
- Test: `tests/test_web_grouping_api.py`. Reuse `_client()`, and write a fresh heartbeat with `GroupingOverrideStore(...).update_heartbeat(runtime={"llm_enabled": True})`, following the existing health test.

**Interfaces:**
- Consumes: Task 1 helpers.
- Produces:
  - `POST /api/service-analysis` and `GET /api/service-analysis` per spec §4.4–4.5.
  - Known services are the `service` values in `template_registry.json` plus the first element of the JSON-list keys in `anomaly_state.json`.
  - "Pending" means the record status is `pending`, or the request is newer than the record's `requested_at` (or there is no record and a request exists).

- [ ] **Step 1: Write the failing tests.**
```python
def test_post_service_analysis_202():      # fresh heartbeat llm_enabled True, service "api" -> 202, load_requests(...)["api"] > 0
def test_post_service_analysis_errors():   # "ghost" -> 404; stale heartbeat -> 503; llm_enabled False -> 409 llm_disabled; second POST -> 409 analysis_pending
def test_post_unicode_service():           # template service 'pay "vn" ví' -> 202 and GET lists it exactly
def test_get_service_analysis():           # services sorted; record done returned; request newer than record -> status "requested"
```
- [ ] **Step 2: Run them and confirm they fail.** `.venv/bin/python3 -m pytest tests/test_web_grouping_api.py -q` → FAIL (404 route).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and confirm they pass.** Same command → PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat: service analysis web API"`

---

### Task 6: Service analysis panel (UI UX Pro Max guidance)

**Files:**
- Modify: `logai/web/static/index.html`: panel markup between the Alerts filter bar and `.table-wrap`, a few CSS classes using the existing tokens, and JS next to the AI analysis code.

**Interfaces:**
- Consumes: Task 5 endpoints; the existing `openDocumentEditor()`, `escapeHtml`, `formatFullTs`, `apiMutation`, and the alert refresh loop.
- Produces: the IDs `service-analysis-select`, `service-analysis-run`, `service-analysis-status` (`aria-live="polite"`) and `service-analysis-card`. The spec §5 labels are exact: `Analyze service`, `Analyzing…`, `No analysis yet for this service.`, `Healthy`/`Degraded`/`Critical`/`Unknown`, `Save as document`; errors map to `Engine unavailable` / `LLM analysis is disabled` / `Analysis already running`.

- [ ] **Step 1: Implement.**
  - Fetch `GET /api/service-analysis` in the same place `/api/alerts` is loaded.
  - Keep the selected service across refreshes, falling back to the first service.
  - Health badge colors: green, yellow, red, gray.
  - Issues are an `<ol>`, with group chips using `.tmpl-id`.
  - Suggestions reuse the AI modal's prefill code, extracted into `prefillDocumentEditor(suggestion)` and shared with it.
- [ ] **Step 2: Check it in headless Chrome.** Use the throwaway-page method from the previous feature: copy the page to `_uitest.html`, run assertions with `--dump-dom`, then delete the copy. The fixture data has services `recharge` (done, 2 issues: one doc and one suggestion) and `pay "vn" ví` (no record). Expected `data-uitest`, all true:
  - the select has both options, with the quote rendered escaped;
  - the card shows `Degraded`, the 2 issues, the doc badge and `Save as document`;
  - switching to the other service shows `No analysis yet for this service.`;
  - a stubbed `fetch` POST returning 202 shows `Analyzing…`;
  - Save prefills the editor.
  Also take a screenshot and look at it.
- [ ] **Step 3: Run the full suite.** `.venv/bin/python3 -m pytest -q` → all pass.
- [ ] **Step 4: Commit.** `git commit -m "feat: service analysis panel in Alerts tab"`

---

### Task 7: Docs and live check

**Files:**
- Modify: `ARCHITECTURE.md` (§7.7: request flow, file ownership, evidence caps, the `llm_enabled` heartbeat), `config.yaml` comments if needed.

- [ ] **Step 1: Write the docs.**
- [ ] **Step 2: Run the live check.** Use a scratch script that runs `IncidentClassifier.process_service` against the configured endpoint, with `LOGAI_LLM_*` passed as environment variables on the command line only (never written to files), on synthetic `recharge` evidence: 3 groups, 2 candidates. Expected: `status == "done"`, a valid `health`, and at least 1 issue.
- [ ] **Step 3: Run the full suite and update the graph.** `.venv/bin/python3 -m pytest -q` → all pass; then `graphify update .`.
- [ ] **Step 4: Commit.** `git commit -m "docs: service analysis architecture"`
