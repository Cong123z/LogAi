# Spec: LLM incident classification for alerts

Status: design approved section by section in chat (2026-10-06). This file is the spec. Because plan mode only allows this file, it will be copied to `docs/superpowers/specs/2026-10-06-llm-incident-classification-design.md` and committed once plan mode ends.

## 1. Intent

**Problem.** An alert only says that a `(service, group)` window is ALERTING. To find the matching runbook, on-call operators have to search the documentation corpus by hand. Group documentation today is a static cosine match per group and does not reason about the specific incident.

**Outcome.** When a window enters ALERTING, an LLM:
- receives the incident evidence and the closest candidate documents;
- picks the document that fits, or, if none fits, writes a suggested solution.

Operators see the result in the Alerts tab and can save a suggestion as a new corpus document with one click. The next incident like it can then match that document.

**What the user decided:**
- The LLM is an OpenAI-compatible `/chat/completions` endpoint.
- Analysis runs automatically when a window enters ALERTING.
- A suggestion can be saved as a document.
- The evidence is templates, group info, alert state and the extracted template parameters.
- The UI follows the UI UX Pro Max guidance.

**Assumptions (not contradicted by the user):**
- One analysis per alert episode.
- The result is advice only and never changes a group's documentation fields.
- An LLM outage must never slow or break log processing.

**Success criteria:**
- Within one LLM round-trip of a window entering ALERTING, `/api/alerts` shows either a valid corpus document ID or a suggestion for that window.
- The engine's poll and checkpoint loop is never blocked by the LLM, and an LLM failure never raises into it.
- The LLM can never point at a document that isn't in the corpus.

## 2. Approach (chosen: A)

**A. Background worker thread inside the realtime engine (chosen).** The engine already knows the exact moment a window enters ALERTING, and it already holds the parameters in memory. The worker uses the same thread pattern as `DocumentationRefreshWorker`.

Rejected:
- **B. A separate sidecar process.** It could not see parameters (they exist only in engine memory), and it would add a third writer process.
- **C. Web on-demand.** The user chose automatic analysis.

## 3. Architecture and data flow

1. **Parameter buffer** (`RealtimePipeline`):
   - Keep `_recent_params: dict[(service, group_id), deque(maxlen=200)]` holding `(template_id, params)`.
   - Append in `_process_one` when `grouped` is not None and the event has a parameter that isn't `<*>`.
   - Fact checked on real-shaped data: the preprocessor already masks numbers and IDs before Drain3, so the surviving parameters are word values such as `reason=Unauthorized` or `gateway=MOMO`. Sensitive numbers never reach the LLM.
   - The buffer is memory only. After a restart it refills from new traffic.
2. **Episode trigger** (`_flush_predictions`, after `transition_batch`): loop over the returned `states` in order.
   - ALERTING and the key is not in `_alert_episodes`: add the key and call `classifier.submit(key, evidence)`.
   - NORMAL: discard the key.
   - COOLING→ALERTING is the same episode and does not trigger again.
   - At startup, seed `_alert_episodes` from the persisted ALERTING and COOLING keys, using a new helper `AlertStateMachine.keys_in_states(states)` built on `_parse_group_id_key`. A restart therefore never re-triggers.
3. **`IncidentClassifier`** (new file `logai/incident/classifier.py`):
   - `submit()` builds the evidence snapshot on the caller thread, which is cheap and in memory. It then writes the `pending` record (one small write per episode) and puts the job on a bounded `queue.Queue(maxsize=100)` without blocking. If the queue is full, it logs a warning and drops the job. A key already queued or in flight is ignored.
   - The daemon worker thread gets the candidates, calls the LLM, validates the reply and persists it.
4. **Candidates:** new `DocumentationMatcher.top_k(centroid, k) -> list[(DocEntry, float)]`. It reuses the corpus embeddings already loaded (`self.embeddings @ candidate`, then `argsort`).
5. **Persistence:**
   - File: `data/incident_analysis.json`, a `JSONStore` keyed by `group_id_key((service, group_id))`. Only the engine writes it.
   - When a grouping change deletes a group, its entries are removed in `_refresh_grouping_if_needed`, following the `alert_sm.drop_group` pattern.
6. **Web:** `/api/alerts` reads the file with `_load_json` and attaches `item["analysis"]`. The identity key is decoded the same way `identity()` already does it. The web only reads this file.

## 4. Prompt and response contract

**System message:** "You are an SRE incident classifier. Choose the single candidate document that explains and resolves this incident, or none. If none fits, propose a concise remediation. Reply with JSON only."

**User message:** JSON evidence.
```json
{"service": "...", "group_id": "...",
 "alert": {"state": "ALERTING", "score": 0.81, "count_1m": 420},
 "group": {"representative_template": "...", "event_count": 123},
 "templates": [{"id": "T00012", "text": "...", "level": "ERROR", "count": 900}],
 "parameters": [{"template_id": "T00012", "slot": 2, "top_values": [["Unauthorized", 37]]}],
 "candidates": [{"id": "DOC-003", "title": "...", "text": "...", "error_code": "...", "similarity": 0.71}]}
```

**Limits:**
- templates: at most 10 group members whose `service` equals the window's service (all members if none match), ordered by `event_count` descending
- parameters: top 5 values per slot
- candidates: `llm.max_candidates` (default 5), each candidate's text cut to 2000 characters
- request: `temperature: 0`, `max_tokens: llm.max_tokens` (default 800)

**Expected reply:**
```json
{"documentation_id": "DOC-003" | null, "confidence": 0.0-1.0, "reasoning": "...",
 "suggestion": {"title": "...", "text": "...", "error_code": "..."} | null}
```

**Validation:**
- Strip ```` ``` ```` fences, then `json.loads`.
- A `documentation_id` that isn't among the candidates is treated as `null` (the hallucination guard).
- No valid document and no suggestion counts as a failure.
- Suggestion fields are trimmed to the corpus limits: title 200, text 20000, error_code 100.

**Stored record:**
```json
{"status": "done|failed", "service", "group_id", "analyzed_at", "model",
 "documentation_id", "document_title", "confidence", "reasoning", "suggestion", "error"}
```
A job that is queued but not yet finished is written as `{"status": "pending", "service", "group_id", "queued_at"}`.

## 5. Error handling

- **HTTP:** 408, 429 and 5xx are retried with exponential backoff (`llm.max_retries`, default 2, base `retry_backoff_seconds`). This is the same loop shape as `TemplateEmbedder._request`. Other status codes fail right away.
- **Any failure** (HTTP, timeout, invalid JSON, validation) is stored as `status: "failed"` with the error cut to 500 characters, logged at WARNING, and counted. It never raises into the engine.
- **Metric:** `logai_llm_requests_total{result="matched|suggested|failed|dropped"}` in `MetricsExporter`.
- **Disabled mode:** when `LOGAI_LLM_ENDPOINT` is empty, no classifier is created and the realtime engine behaves exactly as it does today.

## 6. Configuration

`LLMConfig` in `logai/config.py`, with a matching `llm:` section in `config.yaml`:
- `endpoint=""`
- `api_key=None`
- `model=""`
- `timeout_seconds=60`
- `max_retries=2`
- `retry_backoff_seconds=1.0`
- `max_candidates=5`
- `max_tokens=800`

Environment overrides: `LOGAI_LLM_ENDPOINT`, `LOGAI_LLM_API_KEY`, `LOGAI_LLM_MODEL`. `StorageConfig.incident_analysis_file = "incident_analysis.json"`. Add the variables to `.env.example`, `k8s/.env.example`, and the logai-engine service in `docker-compose.yml`.

## 7. UI (`logai/web/static/index.html`, following the UI UX Pro Max guidance)

**AI column.** A new last-but-one column in the Alerts table. Every row's cell is **one `.btn` button** with a text label, so the state is never shown by color alone:

| Status | Button label | Style |
|---|---|---|
| done + document | `DOC-012 · 82%` | `.badge` style `--blue-bg`/`--blue` |
| done + suggestion | `Suggestion` | `--yellow-bg`/`--yellow` |
| pending | `Analyzing…` | dim text, `aria-busy="true"` |
| failed | `Failed` | `--red-bg`/`--red` |
| none | `—` | plain text (no button) |

- Each button has `aria-label="AI analysis for <group> on <service>"`.
- Reasoning is never available only through a hover tooltip.

**"AI analysis" modal.** A single new `.modal-overlay` with `role="dialog" aria-modal="true" aria-labelledby`, reusing the existing `.modal`, `.modal-header` and `.modal-close` (`aria-label="Close"`). It closes with Esc, and focus returns to the button that opened it.
- **Header:** service and group_id, the model, and when it was analyzed.
- **Matched document:** the document ID and title, confidence, and reasoning.
- **Suggestion:** reasoning, plus the suggested title, error_code and text (the text in a scrollable `pre-wrap` block), and a **"Save as document"** `.btn-primary`. That button calls the existing `openDocumentEditor()` with the title, text and error_code prefilled, so saving goes through the existing `POST /api/documentation` with its revision and 409 handling. The button disables itself while saving, following the double-submit guidance.
- **Failed:** the error text.

**No new styles** beyond about three small classes (`.ai-cell`, `.ai-suggestion`, `.ai-failed`) that use the existing tokens. Existing focus rings are kept.

## 8. Testing (TDD)

New `tests/test_incident_classifier.py`, using a fake `requests.Session` like `test_remote_embedder.py`:
1. A valid candidate ID is stored as `done` with that document.
2. `null` plus a suggestion is stored as a suggestion.
3. A hallucinated ID is downgraded: with a suggestion it is stored as a suggestion, without one it is `failed`.
4. Malformed JSON is stored as `failed`. HTTP 500 → 500 → 500 is retried and then stored as `failed`.
5. `top_k` orders candidates by similarity and respects k.
6. The parameter summary drops `<*>` and counts the top values per slot.
7. Episode rule: the sequence NORMAL→WARMING→ALERTING→COOLING→ALERTING→COOLING→NORMAL→WARMING→ALERTING calls `submit` exactly twice. Startup seeding prevents a re-trigger.
8. `/api/alerts` attaches `analysis` for a key that has one.

All 246 existing tests must still pass: `.venv/bin/python3 -m pytest -q`.

**Manual end-to-end check:**
- Use a local OpenAI-compatible server, for example Ollama `http://localhost:11434/v1/chat/completions`.
- Inject a burst into the replay index.
- Confirm that the AI cell moves from `Analyzing…` to `DOC-…` or `Suggestion`, and that "Save as document" creates a new `DOC-xxx`.

## 9. Out of scope (YAGNI)

- **Manual re-analyze:** it needs a web→engine command channel.
- **Auto-assigning the chosen document to the group:** the existing override does this in one click.
- **Configurable prompt or language.**
- **Sending raw log lines.**
- **Analyzing WARMING windows.**

## 10. Files

- `logai/config.py`, `config.yaml`
- `logai/docmatch/doc_matcher.py` (`top_k`)
- `logai/incident/__init__.py` and `logai/incident/classifier.py` (new)
- `logai/alert/alert_state_machine.py` (`keys_in_states`)
- `logai/realtime/realtime_pipeline.py`
- `logai/metrics/prometheus_exporter.py`
- `logai/web/app.py`, `logai/web/static/index.html`
- `tests/test_incident_classifier.py`
- `.env.example`, `k8s/.env.example`, `docker-compose.yml`, `ARCHITECTURE.md`

---
