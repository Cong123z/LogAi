# Error evidence for the LLM (`logai/incident/error_samples.py`)

**Goal right now:** make this module produce the best possible evidence, so
that every AI analysis (window, service, unknown template) names the real
failure, such as an exception, an error code or a host, instead of guessing from
masked templates and rates.

Related: ARCHITECTURE.md §7.6 (what the LLM is sent), `agents.md` (work log).

## 1. Why the module exists

Templates are wording only. The preprocessor (`parsing/preprocessor.py`) masks
IDs, numbers, IPs and hosts, and Drain3 templates are capped at 40 tokens. As a
result, the `parameters` the LLM used to get came from already masked text and
were almost always empty. The root cause of an incident is written in the
error lines themselves: the exception class, the `Caused by` chain, error codes
and the failing host. This module keeps those lines raw and turns them into
the first block of LLM evidence.

The design is **error-first, not error-only**. Incidents without errors (for
example latency warnings, success logs disappearing or traffic drops) are still
covered by the existing rate/group/alert evidence.

## 2. How it works

| Step | Where | What |
|---|---|---|
| Host capture | `collector/es_collector.py::_extract_host` | `host.name` → `host.hostname` → a string `host` → `service_code` (a string or `{"host_ip"}`), stored as `RawLog.metadata["host_name"]` |
| Record | `RealtimePipeline._process_one` → `ErrorSamples.record(raw, template_id)` | Only level ≥ ERROR (ERROR, FATAL, CRITICAL). Stores `(ts, redacted text ≤400 chars, signature, host, ≤8 scalar metadata fields)` in a `deque(maxlen=50)` per `(service, template)`, plus the template's first-seen sample. This covers grouped and pending templates. |
| Signature | `error_signature(message)` | Regex over the first 4000 chars: the first `…Exception/Error/Fault` class, ≤3 `Caused by:` classes, the first `at pkg.Class.method(File:line)` frame, and ≤3 codes (`ERR_*`, `code=`, `error_code=`, `status=`, `http_status=`). Returns None when there is neither an exception nor a cause; the template is then the signature. |
| Redact | `redact(text)` | Masks `password|passwd|pwd|secret|token|api_key|access_key|authorization` `[=:] value` and `Bearer/Basic <value>`. IDs and codes are kept on purpose. |
| Block | `ErrorSamples.block(service, template_ids, counts, now, first_seen, activity)` | Built on the control thread in `RealtimePipeline._errors_block`. Covers the last 30 min. Returns None when quiet. |
| Placement | `classifier._evidence`, `service_analysis.build_service_evidence`, `template_triage.build_template_evidence` | `errors` is the **first** key; `correlated_alerts` (`RealtimePipeline._correlated_alerts`, ≤10 other non-NORMAL windows) is last. The text of every system prompt ends with `ERRORS_PROMPT`. |

Scope per analysis kind:
- **window:** the group's templates of that service;
- **service:** every error template of the service;
- **template triage:** the template itself.

### The `errors` block

```json
{
  "summary": {"error_templates": 2, "signatures": 2, "error_events_15m": 940,
              "error_events_30m": 1850, "new_error_templates_1h": 1},
  "signatures": [
    {"exception": "com.acme.pay.GatewayException",
     "caused_by": ["java.net.SocketTimeoutException"],
     "frame": "com.acme.pay.PaymentClient.call(PaymentClient.java:88)",
     "codes": ["code=PAY_504"], "templates": ["T00042"],
     "count_15m": 40, "count_30m": 50, "rate_per_min": 2.7, "baseline_per_min": 0.1,
     "ratio": 27.0, "chronic": false, "first_seen_minutes_ago": 10,
     "samples": ["<first-seen line>", "<latest distinct line>"]}
  ],
  "top_values": {"host": [["pay-03", 140]], "moduleCode": [["M1", 120]]},
  "timeline_per_min": [0, 0, 1, "...", 140]
}
```

- **Counts, rate and ratio** come from `TemplateActivity.counts()`. When a template's events are spread over several signatures, they are split by sample share.
- **Chronic** means `ratio < 1.5` with a known baseline, which needs at least 3 closed hours with engine traffic. Signatures are sorted elevated before chronic, then by `count_30m`; at most 5 are kept.
- **top_values** holds the top 3 values per key, host first, at most 6 keys. Keys whose values are all distinct (IDs) are skipped.
- **timeline_per_min** comes from `TemplateActivity.series()`. It counts every event of the error templates, at every level, oldest first.

### Tunables (module constants today)

`SAMPLES_PER_TEMPLATE=50`, `SAMPLE_CHARS=400`, `SCAN_CHARS=4000`,
`WINDOW_SECONDS=1800`, `MAX_SIGNATURES=5`, `CHRONIC_RATIO=1.5`,
`MAX_TOP_KEYS=6`, `TOP_VALUES=3`, `MAX_META_KEYS=8`.

### Tests

- `tests/test_error_samples.py`: signatures, redaction, level filter, chronic vs elevated, top values, timeline.
- `tests/test_ai_insights.py::test_error_lines_and_correlated_alerts_reach_window_and_service_evidence`: wiring from `_process_one` to the window and service evidence.

## 3. Known weaknesses (as of 2026-10-09)

1. **No real-data check yet.** It is unknown how many real recharge error lines produce a signature, and whether stack traces arrive as one ES document or as one document per line. If they arrive one per line, the `Caused by` and `at` lines have no level, fall back to INFO, and are dropped.
2. **What the LLM saw is not stored.** Analysis records keep the answer but not the evidence, so a bad answer cannot be traced back to bad evidence. This blocks all tuning.
3. **Personal data is not masked.** Recharge logs carry `msisdn` and phone numbers, which are sent to the LLM endpoint as they are.
4. **Signature patterns are narrow.** They target Java and Python. Missing: Oracle `ORA-xxxxx`, `SQLSTATE`, Go `panic:`, "returned 503" or "HTTP 503" style status codes, and Vietnamese error text.
5. **Samples have little variety.** 50 lines per template cover only seconds on a busy template, and only the first-seen and latest lines are shown. A rarer second variant of the same template can be missed.
6. **First-seen is per process.** After a restart the "first-seen" sample is the first one seen *since the restart*. All samples are in memory only and are lost on restart.
7. **Memory is unbounded per key.** Keys are never pruned (marked with `# ponytail:` in the code).
8. **Tunables are constants.** They are not in `config.py`, so they cannot be changed without a release.
9. **The token cost has not been measured.** The expected cost is about 1–2k extra prompt tokens.

## 4. Roadmap to "perform best" (in order)

| # | Work | Why first | Done when |
|---|---|---|---|
| 1 | **Measure on real data.** Run a read-only script over a recharge index sample: share of ERROR lines with a signature, multiline or not, top unmatched shapes, phone-number frequency. | Every later choice depends on it | A short table of findings added to this doc |
| 2 | **Store the evidence.** Keep the sent `errors` block, or a compact digest, on each analysis record and in `analysis_history.jsonl`, and show it in AI Insights ("What the AI saw"). | Makes every later change measurable and debuggable | A record shows its signatures and samples in the UI |
| 3 | **Mask personal data.** Add configurable masks (phone/msisdn, e-mail, card-like numbers) to `redact`, on by default for msisdn. | Privacy before wider use | A test covers msisdn masking |
| 4 | **Broaden signatures.** Based on #1: add `ORA-`, `SQLSTATE`, HTTP status phrases, Go `panic:`, and the deployment's own `ERR_`/`code` formats. Join multiline traces if #1 shows they are split. | Raises the share of lines with a real cause | Signature coverage on the sample is above an agreed target, e.g. 80% |
| 5 | **Sample variety.** Keep a small set of distinct samples per signature (newest per distinct masked variant) instead of first plus latest. | Rare variants reach the LLM | A test shows two variants of one template both sampled |
| 6 | **Quality loop.** Replay known incidents (node97 replay, `incident_cases.json`), then compare the AI's cited exception and document with the confirmed root cause, along with prompt tokens. | Proves "better", not only "more" | A repeatable script that prints accuracy and tokens per run |
| 7 | **Housekeeping.** Move tunables to `LLMConfig` (or a new `error_evidence:` section), prune idle keys after 24 h, and optionally persist first-seen samples with `template_activity.json`. | Operability | Config documented, memory bounded |

Each step must keep the existing guarantees:
- nothing in this module raises into the poll loop;
- evidence keys are optional, so old records and tests still load;
- the LLM's hallucination guards are unchanged.
