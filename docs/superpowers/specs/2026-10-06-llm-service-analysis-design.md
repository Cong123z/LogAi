# Spec: On-demand LLM analysis of a whole service

Status: design approved section by section in chat (2026-10-06). Extends `2026-10-06-llm-incident-classification-design.md`, which covers the automatic per-window analysis on ALERTING. That behavior is unchanged.

## 1. Intent

**Problem.** LLM analysis runs only when a single `(service, group)` window enters ALERTING. Operators have no way to ask "how is this service doing overall?" at any other time.

**Outcome.** In the Alerts tab, an operator picks a service and clicks **Analyze service**. The LLM looks at all of that service's groups together and returns:
- an overall health verdict;
- a summary;
- a ranked list of issues. Each issue names the groups involved and either links a matching corpus document or gives a suggested fix that can be saved as a document.

**What the user decided:**
- The analysis covers the **whole service**, not individual groups.
- Output: a health summary plus an issue list.
- It runs on demand only.
- Approach: the web writes a request and the engine runs it (the LLM key and LLM calls stay in the engine).

**Success criteria:**
- Clicking Analyze service produces a `done` or `failed` record within one engine poll cycle plus one LLM round-trip, at any alert state, without a restart.
- Each request runs at most once, including across engine restarts.
- Document IDs and group IDs in the result are always ones that were sent to the LLM.

## 2. Evidence (engine side, `logai/incident/service_analysis.py`)

`build_service_evidence(service, groups, templates, alert_states, recent_params, matcher) -> tuple[dict, dict[str, str], set[str]]` returns the evidence, a map of candidate document ID → title, and the set of group IDs that were sent.

**Groups considered:**
- every registry group with at least one member template whose `service == S`;
- plus every group `g` that has a persisted alert window `(S, g)`, if `g` is still in the registry.

**Ranking:** by the alert state of `(S, g)` (ALERTING > COOLING > WARMING > NORMAL/none), then by the maximum level of S's templates in the group (`LEVEL_RANK`), then by S's event count, all descending. **The top 20 are kept.**

**Per-group evidence:**
- `group_id`;
- `representative_template`;
- `event_count`: the sum over S's templates in the group;
- `level`: the max over S's templates;
- `alert`: `{state, score}` from `alert_sm`, or `{"state": "NORMAL", "score": 0.0}` when there is none;
- `templates`: the top 3 of S's templates, as `{id, text, level, count}`;
- `parameters`: `summarize_parameters(recent_params[(S, g)])`, limited to those 3 templates, each value cut to 200 characters.

**Candidates:** `matcher.top_k(centroid, 3)` for each kept group. Deduplicated by ID, keeping the highest similarity, then sorted by similarity and **cut to 15**. Each text is cut to 1000 characters.

**Evidence shape:** `{"service", "groups": [...], "candidates": [{id, title, text, error_code}]}`.

## 3. LLM contract

**System prompt** (`SERVICE_SYSTEM_PROMPT`): "You are an SRE reviewing the overall health of one service from its log groups. Assess its health, summarize it, and list the most important issues (at most 10), most severe first. For each issue name the related group_ids, choose the single candidate document that explains and resolves it or none, and if none fits propose a concise remediation. Reply with JSON only: {...}".

**Reply:**
```json
{"health": "healthy|degraded|critical", "summary": "...",
 "issues": [{"title": "...", "group_ids": ["G0001"], "documentation_id": "DOC-x" | null,
             "reasoning": "...", "suggestion": {"title", "text", "error_code"} | null}]}
```

**Validation:** `validate_service_reply(reply, candidates, sent_groups) -> dict`.
- `health`: not one of the three values becomes `"unknown"`.
- `summary`: a missing or empty string raises an error, which makes the analysis failed. It is trimmed to 4000 characters.
- `issues`: anything that is not a list counts as `[]`. Only the first 10 are kept. For each issue:
  - `group_ids` is filtered to `sent_groups`;
  - `documentation_id` must be a string among the candidates, otherwise null;
  - the suggestion goes through the existing rules (non-empty string title and text; `error_code` string or null; trimmed to 200/20000/100);
  - an issue with neither a valid document nor a valid suggestion is **dropped**;
  - `title` is cut to 200 characters and `reasoning` to 4000 (a missing title falls back to the suggestion's title or the document's title);
  - `document_title` is filled in from the candidates.

The HTTP call and the tolerant JSON parse reuse `IncidentClassifier._call` and `_parse`; `_call` gains a `system_prompt` parameter. Request settings are unchanged: `temperature 0`, and `max_tokens = max(config.max_tokens, 2000)` for this job type.

## 4. Storage and request flow

1. **`data/analysis_requests.json`:**
   - Written only by the web (atomic tmp + `os.replace`). Read by the engine.
   - Shape: `{"schema_version": 1, "requests": {<service>: <requested_at>}}`. On every write, the web drops entries older than 24 h.
   - Helpers in `logai/incident/requests.py`: `load_requests(path) -> dict[str, float]` (missing or corrupt file → `{}`) and `add_request(path, service, requested_at) -> None`.
2. **`data/service_analysis.json`:**
   - Written only by the engine (a `JSONStore`), keyed by service.
   - Record: `{status: "pending"|"done"|"failed", service, requested_at, queued_at|analyzed_at, model, health, summary, issues, error}`.
   - On engine start, pending records become `failed` ("Interrupted by engine restart…"), keeping `requested_at`, the same as the incident store.
3. **Heartbeat:** `_heartbeat_grouping` adds `"llm_enabled": incident_classifier is not None` to `runtime`.
4. **`POST /api/service-analysis`** with body `{"service"}`:
   - **404** `not_found` when no template in `template_registry.json` and no `anomaly_state.json` window has that service;
   - **503** `engine_unavailable` when the heartbeat is stale;
   - **409** `llm_disabled` when `runtime.llm_enabled` is not true;
   - **409** `analysis_pending` when the record is `pending` or a request is newer than the record's `requested_at`;
   - otherwise **202** `{"state": "requested", "requested_at"}`.
5. **`GET /api/service-analysis`** returns `{"services": sorted known services (templates ∪ alert windows), "analyses": {service: record}}`. When a request is newer than the record, that service's entry is returned as `{"status": "requested", "service", "requested_at"}`.
6. **Engine pickup:** `RealtimePipeline._process_analysis_requests()` runs every loop next to `_refresh_grouping_if_needed()`. It does nothing when the classifier is None, and it never raises (exceptions are logged at WARNING).
   - It reloads requests when the file's mtime changes and caches them in memory.
   - For each `(service, requested_at)` that is newer than both `_handled_requests[service]` (in memory) and the stored record's `requested_at`:
     - **Unknown service** (no templates and no windows): mark it handled and log at INFO.
     - **Otherwise**: call `classifier.submit_service(service, requested_at, evidence_inputs)`. If it returns True, mark the request handled. If it returns False because the service is already active, leave it for the next loop.
   - The evidence is built on the poll thread, which is cheap and in memory. It uses snapshot copies of `_recent_params` for the service and `alert_sm` states.
7. **Classifier** (`logai/incident/classifier.py`):
   - `submit_service(service: str, requested_at: float, evidence: dict, candidates: dict[str, str], sent_groups: set[str]) -> bool` writes the pending record to the service store and enqueues the job.
   - The single worker runs both job types; active keys are `("service", name)` so they can never collide with window tuples.
   - `process_service(...) -> dict` follows the same never-raise contract as `process`.
   - The constructor takes an optional `service_store: JSONStore | None`.
   - `on_result` reports `service_done`/`service_failed`, which become `logai_llm_requests_total{result}`.

## 5. UI (`index.html`, Alerts tab; following the UI UX Pro Max guidance)

A **"Service analysis"** panel above the Alerts table, using the existing `.btn`, `.badge`, `.filter-select` and colour tokens:

- **Controls:**
  - a visible `<label>` and a `<select id="service-analysis-select">` filled from `GET /api/service-analysis`;
  - an **Analyze service** `.btn-primary`, disabled while its POST is in flight;
  - an `aria-live="polite"` status line for errors: "Engine unavailable", "LLM analysis is disabled", "Analysis already running".
- **Result card** for the selected service:
  - **Header:** a health badge with text and color. Healthy uses green, Degraded yellow, Critical red, Unknown gray. Next to it, "Analyzed <time> · <model>".
  - **Summary** paragraph.
  - **Issues** as an ordered list. Each issue shows:
    - its title;
    - group-ID chips (monospace `.tmpl-id` style);
    - the reasoning;
    - either a doc badge `DOC-xxx — title` (blue), or the suggested fix (title, error code, `pre-wrap` text) with a **Save as document** button that reuses the existing prefilled `openDocumentEditor()` flow.
  - **States:** `requested`/`pending` shows "Analyzing…" with `aria-busy="true"`; `failed` shows the error text; no record shows "No analysis yet for this service."
- The card re-renders from the same periodic refresh as the alerts table, and immediately after a successful POST.
- All LLM text is escaped with `escapeHtml`.

## 6. Errors

- LLM, network or validation failures end as `status: "failed"` with the error cut to 500 characters.
- A corrupt or missing requests file reads as `{}`.
- A queue-full submit writes `failed` ("Analysis queue is full"), the same as incidents.
- Pending-write failures never raise into the poll loop.

## 7. Testing (TDD)

- **`service_analysis.py`:**
  - group selection includes windows-only groups;
  - ranking order and the cap of 20;
  - per-service event counts and levels;
  - candidates deduplicated and capped at 15;
  - parameters limited to the listed templates.
- **`validate_service_reply`:**
  - invalid health becomes `"unknown"`;
  - a missing summary raises;
  - unknown group_ids are filtered;
  - a hallucinated documentation ID is downgraded to the suggestion, and an issue with neither is dropped;
  - only 10 issues are kept;
  - a null `error_code` is accepted.
- **Classifier:**
  - `process_service` against a FakeSession: done record with `health`/`issues`, failed on malformed JSON;
  - `submit_service` writes pending and dedupes;
  - restart conversion of pending service records.
- **Pipeline:**
  - a request is submitted once;
  - it is not resubmitted on the next loop or by a new pipeline when the record has its `requested_at`;
  - an unknown service is marked handled;
  - an active service is retried next loop;
  - the heartbeat includes `llm_enabled`.
- **Web:**
  - POST: 202 and file written, 404, 503, 409 `llm_disabled`, 409 `analysis_pending`;
  - GET lists services and shows `requested` for unhandled requests.
- **Headless Chrome:**
  - the select is populated;
  - Analyze shows "Analyzing…";
  - a done fixture renders the health badge text, issues, doc badge and suggestion;
  - Save as document prefills the editor.
- **Live check:** one real call to the configured LLM with synthetic evidence.
- **Full suite** green.

## 8. Out of scope (YAGNI)

- per-group on-demand analysis;
- analysis history (only the latest record per service is kept);
- scheduled or automatic service digests;
- comparisons across services;
- rate limiting beyond the pending check.

## 9. Files

- New: `logai/incident/service_analysis.py`, `logai/incident/requests.py`
- Modified:
  - `logai/incident/classifier.py`
  - `logai/realtime/realtime_pipeline.py`
  - `logai/config.py`, `config.yaml` (`StorageConfig.service_analysis_file = "service_analysis.json"`, `analysis_requests_file = "analysis_requests.json"`)
  - `logai/web/app.py`, `scripts/run_web.py`
  - `logai/web/static/index.html`
  - `ARCHITECTURE.md` (§7.7)
- Tests: `tests/test_service_analysis.py` (new), `tests/test_web_grouping_api.py`
