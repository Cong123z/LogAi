# LLM Incident Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a `(service, group)` window enters ALERTING, an LLM picks the best-fitting corpus document or suggests a fix. The result appears in the Alerts tab, where a suggestion can be saved as a document.

**Architecture:** A daemon worker thread inside the realtime engine (`IncidentClassifier`) is fed by an episode trigger in `_flush_predictions`. Candidates come from the existing doc embeddings (`top_k`). Results are written to the engine-owned `data/incident_analysis.json`, and the web reads that file through `/api/alerts`.

**Tech Stack:** Python 3, `requests` (already a dependency), Flask, vanilla JS/CSS in `index.html`, pytest via `.venv/bin/python3 -m pytest`.

**Spec:** the top half of this file. At execution start, copy it to `docs/superpowers/specs/2026-10-06-llm-incident-classification-design.md` and copy this plan to `docs/superpowers/plans/2026-10-06-llm-incident-classification.md`.

## Global Constraints
- No new dependencies. The LLM is an OpenAI-compatible `/chat/completions` endpoint called with `requests`.
- The feature is off when `config.llm.endpoint` is empty. In that case the realtime engine behaves exactly as it does today.
- An LLM call never runs on, and never raises into, the poll loop. The queue is bounded at `maxsize=100`.
- Only the engine writes `incident_analysis.json`. The web only reads it.
- `documentation_id` must be one of the candidate IDs. Anything else is treated as `null`.
- Trim limits: title 200, text 20000, error_code 100, error 500, candidate text 2000. At most 10 templates and 5 values per slot. Defaults: `max_candidates=5`, `max_tokens=800`, `max_retries=2`, `retry_backoff_seconds=1.0`, `timeout_seconds=60`, `temperature=0`.
- Metric `logai_llm_requests_total{result}`, where result is `matched|suggested|failed|dropped`.
- Run tests with `.venv/bin/python3 -m pytest -q`. The 246 existing tests stay green.

## Review Focus
1. **The LLM wraps its JSON in ```` ```json ```` fences or leading prose.** The parser must still extract the object. Test: Task 2 `test_fenced_reply_parsed`.
2. **The group has no centroid** (a fresh manual group, or deleted mid-flight). Candidates are then empty, the LLM is still called for a suggestion, and any `documentation_id` is rejected. Test: Task 2 `test_no_centroid_still_suggests`.
3. **The corpus is empty or the matcher is not ready.** `top_k` returns `[]` and does not raise. Test: Task 1 `test_top_k_empty_corpus`.
4. **Suggestion fields are oversized or the wrong type.** Oversized values are trimmed. Non-string or missing title/text means `failed`. Test: Task 2 `test_suggestion_trimmed_and_typed`.
5. **A group is deleted by a grouping change.** Its analysis entries are removed. Test: Task 2 `test_drop_group_removes_entries`.

---

### Task 1: Candidate retrieval and episode-seed helpers

**Files:**
- Modify: `logai/docmatch/doc_matcher.py` (add the method after `match_document`)
- Modify: `logai/alert/alert_state_machine.py` (add the method after `groups_not_normal`)
- Test: `tests/test_incident_classifier.py` (new; later tasks extend it)

**Interfaces:**
- Produces: `DocumentationMatcher.top_k(centroid: np.ndarray, k: int) -> list[tuple[DocEntry, float]]`, ordered by similarity descending. Returns `[]` when the matcher is not ready, has no entries, or the centroid is None or has an incompatible shape.
- Produces: `AlertStateMachine.keys_in_states(states: set[str]) -> set[tuple[str, str]]`. Parses the persisted keys with `_parse_group_id_key` and keeps tuple keys only.

- [ ] **Step 1: Write the failing tests**
```python
def test_top_k_orders_and_limits():  # matcher built like test_documentation_matcher.py, with 3 docs and a fake embedder
    hits = matcher.top_k(np.array([1.0, 0.0]), 2)
    assert [e.doc_id for e, _ in hits] == ["D1", "D2"]; assert hits[0][1] >= hits[1][1]
def test_top_k_empty_corpus():
    assert empty_matcher.top_k(np.array([1.0, 0.0]), 5) == []
    assert matcher.top_k(None, 5) == []
def test_keys_in_states():  # JSONStore with keys json.dumps(["api","GA"]) -> ALERTING, ["api","GB"] -> NORMAL, legacy "GC" -> ALERTING
    assert sm.keys_in_states({"ALERTING", "COOLING"}) == {("api", "GA")}
```
- [ ] **Step 2: Run them and confirm they fail.** `.venv/bin/python3 -m pytest tests/test_incident_classifier.py -q` → FAIL (AttributeError).
- [ ] **Step 3: Implement both methods.** `top_k` takes `self._lock` and uses `np.argsort(-(self.embeddings @ c))[:k]`.
- [ ] **Step 4: Run the tests and confirm they pass.** Same command → PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat: top_k doc candidates and alert keys_in_states"`

---

### Task 2: LLMConfig and IncidentClassifier

**Files:**
- Modify: `logai/config.py`: `LLMConfig` dataclass, `AppConfig.llm`, `StorageConfig.incident_analysis_file = "incident_analysis.json"`, environment overrides `LOGAI_LLM_ENDPOINT/API_KEY/MODEL` in `load_config`
- Modify: `config.yaml`: an `llm:` section with the spec §6 defaults
- Create: `logai/incident/__init__.py` (empty), `logai/incident/classifier.py`
- Test: `tests/test_incident_classifier.py` (copy the `FakeResponse`/`FakeSession` pattern from `tests/test_remote_embedder.py`)

**Interfaces:**
- Consumes: `top_k` (Task 1), `group_id_key` from `logai/alert/alert_state_machine.py`, `JSONStore`, `GroupRegistry.get`/`get_centroid`, `TemplateRegistry.get`.
- Produces:
  - `LLMConfig(endpoint="", api_key=None, model="", timeout_seconds=60.0, max_retries=2, retry_backoff_seconds=1.0, max_candidates=5, max_tokens=800)`
  - `summarize_parameters(entries: Iterable[tuple[str, list[str]]], top_n: int = 5) -> list[dict]`. Each item is `{"template_id", "slot", "top_values": [[value, count], ...]}`, where slot is the index in the params list. `"<*>"` values are dropped. Items are sorted by `(template_id, slot)`, values by count descending then value.
  - `IncidentClassifier(config: LLMConfig, matcher, groups, templates, store: JSONStore, session=None, on_result: Callable[[str], None] | None = None)`
  - `.submit(window_key: tuple[str, str], alert: dict, recent_params: list[tuple[str, list[str]]]) -> bool`. It writes the `pending` record and enqueues the job. It returns False when the queue is full (`on_result("dropped")`) or when the key is already queued or in flight.
  - `.process(window_key, alert, parameters: list[dict]) -> dict`. This runs synchronously: evidence, then the LLM, then validation, then `store.set(group_id_key(key), record)`. It returns the record and calls `on_result("matched"|"suggested"|"failed")`. It never raises.
  - `.start()`, `.stop(timeout=2.0)`. The worker loops over `queue.get` and calls `process`, then removes the key from the in-flight set.
  - `.drop_group(group_id: str) -> int`. Deletes every store key whose parsed identity has `[1] == group_id`.

- [ ] **Step 1: Write the failing tests.** Fixtures:
  - a 2-dimensional fake matcher with docs `DOC-001` and `DOC-002`
  - group `GA` (service `api`) with a centroid and template `T1`
  - an LLM reply given as `{"choices": [{"message": {"content": <json str>}}]}`
```python
def test_valid_candidate_matched():   rec = clf.process(("api","GA"), alert, []); assert rec["status"]=="done" and rec["documentation_id"]=="DOC-001" and rec["document_title"]
def test_null_with_suggestion():      assert rec["documentation_id"] is None and rec["suggestion"]["title"]
def test_hallucinated_id_downgraded(): # "DOC-999" + suggestion -> suggestion kept, id None; "DOC-999" w/o suggestion -> status "failed"
def test_malformed_json_failed():     assert rec["status"]=="failed" and rec["error"]
def test_http_500_retried_then_failed(): # 3x FakeResponse(500): len(session.calls)==3, status "failed"; retry_backoff_seconds=0
def test_fenced_reply_parsed():       # content "Here:\n```json\n{...}\n```" -> status "done"
def test_no_centroid_still_suggests(): # group without centroid: payload candidates == [], LLM called once, suggestion stored
def test_suggestion_trimmed_and_typed(): # title "x"*500 -> len 200; title 123 -> "failed"
def test_drop_group_removes_entries(): assert clf.drop_group("GA")==1 and store.get(json.dumps(["api","GA"])) is None
def test_summarize_parameters():
    assert summarize_parameters([("T1",["<*>","MOMO"]),("T1",["<*>","MOMO"]),("T1",["<*>","VTP"])]) == \
        [{"template_id":"T1","slot":1,"top_values":[["MOMO",2],["VTP",1]]}]
def test_submit_writes_pending_and_dedupes(): assert clf.submit(k,a,[]) is True and store.get(key)["status"]=="pending"; assert clf.submit(k,a,[]) is False
def test_request_body(): # session.calls[0]["json"]: model, temperature==0, max_tokens==800, messages[1].content parses to evidence with keys service, group_id, alert, group, templates, parameters, candidates; Authorization header present only when api_key set
def test_llm_env_overrides(): # patch.dict LOGAI_LLM_ENDPOINT/MODEL -> load_config().llm fields
```
- [ ] **Step 2: Run them and confirm they fail.** `.venv/bin/python3 -m pytest tests/test_incident_classifier.py -q` → FAIL (ImportError).
- [ ] **Step 3: Implement config and the classifier.** The retry loop copies the shape of `TemplateEmbedder._request`: retry on 408/429/5xx, sleep `retry_backoff_seconds * 2**attempt`. The parser takes the substring from the first `{` to the last `}` and then runs `json.loads`, which covers fences and prose. The system prompt text is fixed by spec §4. Template selection follows spec §4.
- [ ] **Step 4: Run the tests and confirm they pass.** Same command → PASS. Then the full suite `.venv/bin/python3 -m pytest -q` → all pass.
- [ ] **Step 5: Commit.** `git commit -m "feat: LLM incident classifier"`

---

### Task 3: Wire the classifier into the realtime pipeline

**Files:**
- Modify: `logai/realtime/realtime_pipeline.py` (`__init__`, `run_forever`, `_process_one`, `_flush_predictions`, `_refresh_grouping_if_needed`)
- Modify: `logai/metrics/prometheus_exporter.py`: `logai_llm_requests_total = Counter(..., ["result"])`
- Test: `tests/test_incident_classifier.py` (build the pipeline the way `tests/test_stuck_alert_tick.py` `setUp` does, with `cfg.llm.endpoint = "http://llm"`, and then set `pipeline.incident_classifier = MagicMock()`)

**Interfaces:**
- Consumes: Task 2 `IncidentClassifier`; Task 1 `keys_in_states`.
- Produces:
  - Attributes `self.incident_classifier: Optional[IncidentClassifier]` (None when the endpoint is empty), `self._recent_params: dict[tuple[str, str], deque]` (`maxlen=200`), `self._alert_episodes: set[tuple[str, str]]` (seeded with `alert_sm.keys_in_states({"ALERTING", "COOLING"})`).
  - Method `_track_alert_episodes(self, scored: list[AnomalyResult], states: list[AnomalyState]) -> None`. `transition_batch` returns its states 1:1 with `scored`, so zip the two. On ALERTING with a key that isn't in the set: add the key and call `submit(key, {"state", "score": result.anomaly_score, "count_1m": result.count_1m}, list(self._recent_params.get(key, ())))`. On NORMAL: discard the key. It does nothing when the classifier is None. Call it at the end of `_flush_predictions`.
  - In `_process_one`: when `grouped` is set and the classifier exists and `any(p != "<*>" for p in parsed.parameters)`, append `(parsed.template_id, parsed.parameters)`.
  - In `_refresh_grouping_if_needed`: inside the `deleted_groups` loop, add `incident_classifier.drop_group(gid)` and pop `_recent_params` keys for that group.
  - In `run_forever`: `incident_classifier.start()` next to `documentation_worker.start()`.
  - `on_result` is wired to `self.metrics.logai_llm_requests_total.labels(result=r).inc()`.

- [ ] **Step 1: Write the failing tests.**
```python
def test_episode_triggers_once_per_episode():
    seq = ["NORMAL","WARMING","ALERTING","COOLING","ALERTING","COOLING","NORMAL","WARMING","ALERTING"]
    for s in seq: p._track_alert_episodes([_result(K)], [_state(K, s)])
    assert p.incident_classifier.submit.call_count == 2
def test_restart_seeded_episode_not_retriggered():  # write anomaly_state.json {json.dumps(list(K)): {... "alert_state":"ALERTING"}} before constructing
    p._track_alert_episodes([_result(K)], [_state(K,"ALERTING")]); p.incident_classifier.submit.assert_not_called()
def test_params_buffered_without_placeholders():  # _process_one on a known-template event whose params are ["<*>","MOMO"] -> p._recent_params[("auth","G_AUTH")] == deque([("T…",["<*>","MOMO"])])
def test_disabled_when_no_endpoint(): # cfg.llm.endpoint = "" -> p.incident_classifier is None and _track_alert_episodes does not raise
```
- [ ] **Step 2: Run them and confirm they fail.** `.venv/bin/python3 -m pytest tests/test_incident_classifier.py -q` → FAIL.
- [ ] **Step 3: Implement the wiring as described in Interfaces.**
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python3 -m pytest -q` → all pass, including `test_stuck_alert_tick.py` and `test_predict_batch_and_flush.py`.
- [ ] **Step 5: Commit.** `git commit -m "feat: trigger LLM incident analysis on ALERTING episodes"`

---

### Task 4: Web API and Alerts UI

**Files:**
- Modify: `logai/web/app.py` (`list_alerts`)
- Modify: `logai/web/static/index.html` (Alerts table header and row template around lines 719 and 1100, new modal next to `#document-modal`, three CSS classes near `.alert-state-*`)
- Test: `tests/test_web_grouping_api.py` (add a test next to `test_alert_api_filters_orphan_group_state`)

**Interfaces:**
- Consumes: the record schema from spec §4 and the key format `group_id_key` (`json.dumps([service, group_id])`).
- Produces: each `/api/alerts` item gets `"analysis": dict | None`, including the synthetic NORMAL items.

- [ ] **Step 1: Write the failing test.**
```python
def test_alert_api_attaches_incident_analysis():
    # anomaly_state.json key ["api","GA"] ALERTING; incident_analysis.json same key {"status":"done","documentation_id":"DOC-001",...}
    item = next(i for i in listing["items"] if i["group_id"]=="GA")
    assert item["analysis"]["documentation_id"] == "DOC-001"
    assert next(i for i in listing["items"] if i["group_id"]=="GB")["analysis"] is None
```
- [ ] **Step 2: Run it and confirm it fails.** `.venv/bin/python3 -m pytest tests/test_web_grouping_api.py -q` → FAIL (KeyError).
- [ ] **Step 3: Implement the API.** `analyses = _load_json("incident_analysis.json")`, then `analyses.get(group_id_key((service, group_id)))`.
- [ ] **Step 4: Run the tests and confirm they pass.** Same command → PASS.
- [ ] **Step 5: Implement the UI as described in spec §7.**
  - Add an "AI" `<th>` and raise the empty-state `colspan` from 9 to 10.
  - Write `aiCell(item)`, which returns the button or `—` using the labels in the §7 table, with the confidence shown as a whole-number percent.
  - Add an `#ai-analysis-modal` that uses the existing `role`/`aria` pattern from `#delete-confirm-modal`.
  - Rows are re-rendered with `innerHTML`, so open the modal through a click handler delegated on `$alertsTbody` (`data-ai-key`).
  - Esc and `.modal-close` close the modal, and focus returns to the button that opened it.
  - "Save as document" closes this modal and calls `openDocumentEditor()`, then fills `#document-title`, `#document-text` and `#document-error-code` from the suggestion.
  - Escape all LLM text with `escapeHtml`.
- [ ] **Step 6: Check the UI by hand.**
  - Write fixture `data/` files: one done+doc, one suggestion, one pending, one failed.
  - Run `.venv/bin/python3 scripts/run_web.py --data-dir <tmp>` and open `:5555`, Alerts tab.
  - Confirm the four labels, that Tab reaches each AI button, and that Esc closes the modal and returns focus.
  - Confirm that "Save as document" opens the prefilled editor and that saving creates `DOC-xxx`.
- [ ] **Step 7: Commit.** `git commit -m "feat: show LLM incident analysis in Alerts tab"`

---

### Task 5: Deployment config, docs, and final verification

**Files:**
- Modify: `.env.example`, `k8s/.env.example`, `docker-compose.yml` (logai-engine `environment`: `LOGAI_LLM_ENDPOINT`, `LOGAI_LLM_API_KEY`, `LOGAI_LLM_MODEL`, each with an empty default)
- Modify: `ARCHITECTURE.md`: a short realtime subsection covering the trigger rule, the file ownership, the hallucination guard, and the disabled mode, plus the three variables in the environment-override table
- Create: the spec and plan copies under `docs/superpowers/` (see the header)

- [ ] **Step 1: Make the edits listed above.**
- [ ] **Step 2: Run the full suite.** `.venv/bin/python3 -m pytest -q` → every test passes (246 plus the new ones).
- [ ] **Step 3: Run the manual end-to-end check from spec §8**, against a local OpenAI-compatible server.
- [ ] **Step 4: Update the graph.** Run `graphify update .`.
- [ ] **Step 5: Commit.** `git commit -m "docs: LLM incident classification config and architecture"`
