# Spec: On-demand LLM analysis (any alert state)

Status: design approved section by section in chat (2026-10-06). Extends `2026-10-06-llm-incident-classification-design.md`.

## 1. Intent

**Problem.** LLM analysis runs only when a `(service, group)` window enters ALERTING. Operators who want to understand a group while it is NORMAL, WARMING or COOLING have no way to ask.

**Outcome.** An **Analyze** button on every row of the Alerts tab, which lists every group × service including NORMAL ones. One click runs the existing classifier on that window: it matches the window to a corpus document, or suggests what the logs indicate. The result appears in the same AI cell and modal.

**What the user decided:**
- On-demand button only. Not chosen: automatic analysis of new ERROR templates, WARMING early warning, a periodic digest.
- Approach A: the web writes a request and the engine runs it.

**Assumptions:** one record per window, and the latest analysis wins. Alert-triggered analysis stays exactly as it is.

**Success criteria:**
- Clicking Analyze on a NORMAL row produces a `done` or `failed` analysis within one engine poll cycle plus one LLM round-trip, without restarting anything.
- The LLM key and LLM calls stay in the engine only.
- A request is processed at most once, including across engine restarts.

## 2. Data flow

1. **Request file** `data/analysis_requests.json`:
   - Written only by the web (atomic tmp + `os.replace`). Read by the engine.
   - Shape: `{"schema_version": 1, "requests": {<group_id_key((service, group_id))>: <requested_at epoch float>}}`.
   - On every write, the web drops entries older than 24 h.
   - Read and write helpers live in `logai/incident/requests.py`: `load_requests(path) -> dict[str, float]` (missing or corrupt file → `{}`) and `add_request(path, key, requested_at) -> None`.
2. **`POST /api/analysis`** with body `{"service", "group_id"}`:
   - The group must exist in `group_registry.json`. Otherwise **404** `not_found`.
   - The engine heartbeat must be fresh (`grouping.synchronization_status(...)["state"] != "engine_unavailable"`). Otherwise **503** `engine_unavailable`.
   - The heartbeat `runtime.llm_enabled` must be `true` (missing counts as false). Otherwise **409** `llm_disabled`.
   - The window must not already have a pending analysis (record status `pending`, or a request newer than the record's `requested_at`, or than its `analyzed_at`/`queued_at` when it has no `requested_at`). Otherwise **409** `analysis_pending`.
   - On success it calls `add_request(...)` and returns **202** `{"state": "requested", "requested_at": ...}`.
3. **`/api/alerts`**: when a window has a request newer than its record, the response returns `analysis = {"status": "requested", "service", "group_id", "requested_at"}` in place of the older record. This is computed by the web without writing anything, so a page reload still shows Analyzing….
4. **Engine heartbeat**: `RealtimePipeline._heartbeat_grouping` adds `"llm_enabled": incident_classifier is not None` to its `runtime` dict.
5. **Engine pickup**: `RealtimePipeline._process_analysis_requests()` runs each loop iteration next to `_refresh_grouping_if_needed()`. It does nothing when the classifier is None.
   - It reloads the requests dict only when the file mtime changes, and keeps it in memory.
   - For each `(key, requested_at)` it checks two things:
     - `requested_at` is newer than `_handled_requests[key]`, an in-memory dict;
     - it is also newer than the stored record's `requested_at` (0 if missing). This check is what makes it restart-safe.
   - When both hold:
     - **Group missing** from the registry, or the key can't be parsed into `(service, group_id)`: mark the request handled and log at INFO.
     - **Otherwise**: call `submit(window_key, alert, recent_params, trigger="manual", requested_at=requested_at)`. The alert part is built from `alert_sm._load(window_key)` as `{"state": state.alert_state, "score": state.anomaly_score, "count_1m": None}`. If `submit` returns True, mark the request handled. If it returns False because the key is already active, leave it for the next loop.
6. **Classifier changes** (`logai/incident/classifier.py`):
   - `submit(window_key, alert, recent_params, trigger: str = "alert", requested_at: float | None = None) -> bool`.
   - `trigger` and `requested_at` are carried into the pending, done and failed records. The alert path passes `trigger="alert"`.
   - The SYSTEM_PROMPT gains one sentence: "If the alert state is not ALERTING, an operator requested this analysis: explain what these logs indicate and whether any candidate document applies."
   - The restart conversion (pending → failed) keeps `trigger` and `requested_at`.
7. **One record per window:** the latest analysis wins. An ALERTING episode still triggers automatically and overwrites a manual result.

## 3. UI (`index.html`, Alerts tab)

- **AI cell:**
  - Analysis is `null`: an **Analyze** `.btn` (`aria-label="Analyze <group> on <service>"`) that POSTs `/api/analysis`.
  - Status `requested` or `pending`: `Analyzing…` with `aria-busy="true"`, as it does today.
  - Every other state stays as it is.
- **Modal:**
  - The Window row gains the trigger, shown as "Alert" or "Manual request".
  - A **Re-analyze** `.btn` appears when the status is `done` or `failed`. It POSTs and closes the modal; it is disabled while the request is in flight.
- **Errors:** a short message in an `aria-live="polite"` status line above the Alerts table: "Engine unavailable", "LLM analysis is disabled", or "Analysis already running". The message is never shown only as a tooltip.
- After a successful POST, the UI reloads `/api/alerts` once immediately. The regular refresh then picks up the result.

## 4. Errors

- LLM, network or validation failures end as `status: "failed"`, as for alerts.
- A corrupt or missing requests file is read as `{}`.
- Unparseable keys are ignored.
- `_process_analysis_requests` never raises into the poll loop: any exception is logged at WARNING.

## 5. Testing (TDD)

- **Web:**
  - 202 with the request written;
  - 404 for an unknown group;
  - 503 for a stale heartbeat;
  - 409 `llm_disabled`;
  - 409 `analysis_pending`;
  - `/api/alerts` shows `status: "requested"` for an unhandled request.
- **requests.py:** load from a missing or corrupt file returns `{}`; entries older than 24 h are pruned on add.
- **Engine:**
  - the request is submitted once with `trigger="manual"`, `requested_at` and NORMAL alert evidence;
  - it is not resubmitted on the next loop;
  - it is not resubmitted by a new pipeline when the record's `requested_at` is greater than or equal to the request;
  - a missing group is marked handled and never submitted;
  - an active key is retried on the next loop.
- **Classifier:** records carry `trigger` and `requested_at`; restart conversion keeps them.
- **Heartbeat:** runtime includes `llm_enabled`.
- **Headless Chrome:** Analyze appears for a NORMAL row with no analysis; a POST turns the cell into Analyzing…; Re-analyze is visible for a done record.
- **Full suite** green.

## 6. Out of scope (YAGNI)

Analyze buttons on the Groups and Templates tabs, rate limiting beyond the pending check, keeping history of past analyses per window, automatic analysis of new templates, WARMING triggers, and digests.

## 7. Files

- `logai/incident/requests.py` (new)
- `logai/incident/classifier.py`
- `logai/realtime/realtime_pipeline.py`
- `logai/web/app.py`
- `logai/web/static/index.html`
- `config.yaml` and `logai/config.py` (`StorageConfig.analysis_requests_file = "analysis_requests.json"`)
- `scripts/run_web.py` (passes the requests path)
- `tests/test_incident_classifier.py`, `tests/test_web_grouping_api.py`
- `ARCHITECTURE.md` (§7.6 addendum)
