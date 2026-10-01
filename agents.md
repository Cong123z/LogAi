# Agent Work Log

## Task: Fix training Phase 1 bottleneck + training observability

- **Date:** Thursday, 2026-10-01 (timezone +07)
- **Time:** ~01:45 – 02:03 (from file modification times; last code edit 01:48:49,
  benchmark 01:49:33, HANDOFF.md updated 01:55:39, this log written 02:03)
- **Branch:** `calibrate` (base commit `21dac5b Config index log`)
- **Status:** done, **uncommitted**
- **Scope decided by user:** fix only the bottleneck that keeps training running
  non-stop; keep `max_docs` at 200,000 with no env/CLI override; add logging to
  observe training; regex/masking work is postponed.

### Root cause

Drain3's `TemplateMiner.add_log_message()` re-serialises the **entire** template
tree (jsonpickle + zlib) and writes it to `/app/data/drain3_state.bin` every time
a cluster is created or its template changes. Each save costs O(state size), and
the number of saves grows with the number of changes, so Phase 1 is roughly
quadratic. With ~140k fragmented clusters in production, Phase 1 effectively
never finishes. This showed up as endless
`INFO drain3.template_miner: Saving state of N clusters ...` lines.

### Changes

| File | Change |
|------|--------|
| `logai/parsing/drain3_parser.py` | New `AtomicFilePersistence` (write tmp → fsync → `os.replace`). `Drain3Parser(config, autosave=True)`: with `autosave=False` the persistence handler is detached after the state is restored, so no per-change saves happen. New `save_state(reason) -> bytes`, `persistence_path`, `cluster_count()`, `message_count()`. |
| `logai/training/train_pipeline.py` | Training uses `Drain3Parser(..., autosave=False)`. Per-batch commit order: **Drain3 state → event index → ES cursor**. State goes first because event-index records reference cluster IDs; a crash after the state save only replays the batch (slightly inflated cluster sizes) and never loses or collides template IDs. Added logs: Phase 1 start (restored state size/clusters/messages, events already indexed), one line per batch, richer Phase 1 complete, job config (index, ISO time range, lookback, max_docs, batch_size, resume cursor), `TRAINING SUCCEEDED` / `TRAINING FAILED` (with retained checkpoint info). On resume, events already in the event index count towards `max_docs` (a restart no longer fetches another full 200k). |
| `logai/collector/es_collector.py` | WARNING when the historical stream stops because it hit `max_docs`, meaning the time range was truncated. |
| `scripts/run_training.py` | `drain3` logger set to WARNING (its "Saving state" line duplicates the new per-batch line). |
| `config.yaml` | `training.max_docs: 1000000` → `200000`. |
| `tests/test_drain3_deferred_save.py` | New: no write on template change when autosave is off; explicit save is atomic and restorable; default autosave still persists. |
| `HANDOFF.md` | Added a "Done 2026-10-01" section. |

Example per-batch log line:

```text
Phase 1 batch 3: fetched=2000 parsed=2000 skipped_dup=0 | total fetched=6000 parsed=6000 | last_event=2023-11-14T23:53:19+00:00 | clusters=76 drain3_messages=6000 state=0.0MB event_index=1.2MB | fetch=0.00s parse=0.03s save=0.00s | elapsed=0s rate=60636 ev/s
```

`fetch` = ES latency, `parse` = Drain3 parsing, `save` = state persistence.
Whichever of these grows shows the current bottleneck.

### Verification

- `.venv/bin/python3 -m pytest -q`: **243 passed, 6 failed**. The 6 failures are
  `tests/test_log_masking.py::TestDrain3UniversalMaskingRules` and already fail
  on the unmodified commit, because the extended regex rules are not in the
  current `config.yaml`. They belong to the postponed regex work.
- Benchmark, 6,000 distinct clusters, batches of 2,000:
  old autosave **345.2 s** vs new per-batch save **0.9 s**.
- Real log (`full_node96.log`, first 30k lines, 179 clusters): 1.1 s → 0.5 s.

### Notes / follow-ups

- The realtime pipeline still uses autosave (unchanged). It has the same cost
  pattern and can be fixed later by saving together with `checkpoint.commit()`.
- If the PVC still holds the old ~140k-cluster state, each batch save still
  writes that whole file (~100 saves for 200k docs). Watch `save=` in the logs.
  Reset the training artifacts once the regex work lands.
- Deploy: build a new image tag (prefer git SHA), then
  `kubectl delete job logai-training` before applying again.
- Next task: regex/masking (see "Việc tiếp theo" in `HANDOFF.md`).

---

## Task: Wording-only regex preprocessing before Drain3

- **Date:** Thursday, 2026-10-01 (timezone +07)
- **Time:** ~02:10 – 02:55 (planning, then implementation; full test run at 02:52)
- **Branch:** `calibrate` (base commit `7744ac3 Fix bottle neck in drain3`)
- **Status:** done, **uncommitted**
- **Plan:** `/home/cong/.claude/plans/i-u-ch-nh-m-t-ch-t-agile-deer.md`
- **Decisions by user:**
  - keep only the *semantic wording* of a log; numbers never need to be kept;
  - the level is not always the 3rd token: search it in the **first 5 tokens**;
  - timestamps and level are **removed** outright (not masked), because
    `@timestamp` and `RawLog.level` already hold them;
  - no separate measurement tool.

### Design (`logai/parsing/preprocessor.py`, new)

`normalize(message)` runs before Drain3 in both training and realtime (same
`Drain3Parser`). Steps:

0. Cut the message at `max_chars` (8192).
1. `find_level()` looks for a level in the first 5 tokens. Every token
   before it must be date/time/pid/`[thread]`-like, so `Connection ERROR ...`
   is not treated as a prefix. If found, the prefix up to and including the
   level, plus one `[thread]` and separator, is **deleted**.
2. HTML entities are unescaped (`&lt;soap:Envelope`), and `key=` with an
   empty value becomes `key=<*>` so the token count stays stable.
3. XML/SOAP: wrappers (Envelope/Body/Header), namespaces and attributes are
   dropped; element names and text are kept.
4. Timestamps anywhere are **deleted**: Java `Thu Aug 13 18:52:11 GMT+07:00 2020`,
   ISO, `dd/MM/yyyy HH:mm:ss.SSS`, bare `HH:mm:ss`.
5. Separators `= , ; { } ( ) [ ] " '` become spaces.
6. URL host, e-mail and IPv4[:port] become `<*>`. Hex ids with mixed
   digits and letters become `<*>`. Long ids (16+ chars with 3+ digits:
   secrets, UUIDs, transaction ids) become `<*>`. Each remaining digit run
   inside a word becomes `<*>` and the letters stay (`con-1-Sender` →
   `con-<*>-Sender`).
7. A token made only of `<*>` and punctuation becomes `<*>`, and runs of
   `<*>` collapse to one.
8. At most `max_tokens` (40) tokens are kept.

The regex cost is kept low with cheap marker checks before the costly rules
(`://`, `@`, `&`, digits) and with callbacks instead of lookaheads that run at
every position. Measured: short log ~7 µs, long transaction log ~37 µs.

### Changes

| File | Change |
|------|--------|
| `logai/parsing/preprocessor.py` | New: `find_level`, `normalize`, `LEVELS`. |
| `logai/parsing/drain3_parser.py` | `preprocess()` → `normalize()` before `add_log_message`. Removed `_strip_patterns` / `extra_delimiters`. `masking_rules` are now optional extra Drain3 rules. |
| `logai/config.py`, `config.yaml` | `Drain3Config`: `preprocess=True`, `max_chars=8192`, `max_tokens=40`, `level_search_tokens=5`. `masking_rules` defaults to `[]` (the 4 old rules are removed). |
| `logai/collector/es_collector.py` | `_extract_level_from_message` uses the shared `find_level` (first 5 tokens). Removed `_LEVEL_TOKEN_RE`, so the level list now lives in one place. |
| `tests/test_preprocessor.py` | New: level at positions 1–5, level word inside a sentence, timestamp removal, XML, ids, URL, collapse, token cap, parser integration. |
| `tests/test_log_masking.py` | Expectations updated to the new format. The HDFS audit now expects 30 templates: the two `datanode(s)` variants differ only by list length and share a template. |
| `tests/test_es_malformed_hits.py` | Level at token 2 and token 4; level word inside a sentence → INFO. |

### Verification

- `.venv/bin/python3 -m pytest -q`: **265 passed, 1 failed**. The failure is
  `test_realtime_crash_load_and_perf.py::test_high_load_throughput_and_stress`
  (asserts ≥4,900 logs/s). It also fails on the committed HEAD before this
  change (4,100–4,600 logs/s on this machine), so it depends on machine speed
  and is not caused by this change.
- Real log `full_node96.log`, first 300k lines (119,432 events after joining
  continuation lines):
  - old 4 rules: 177 templates, 12 singletons;
  - new preprocessor: 211 templates, 10 singletons.
  - Both curves flatten after ~40% of the data.
  - The old count was lower only because it **over-merged** unrelated logs.
    One old template `<*><*> <*>:<*>:<*>.<*> INFO [worker-<*>] <*> <*> <*> <*>`
    swallowed `RequestHandler: have password`, `ContentValidator: Name valid`,
    `TopupUtil: Start verify`, … The new templates keep them apart and have
    no date, level or thread noise.
  - The new preprocessor also merged transaction logs that the old rules split
    because of empty values.

### Deploy notes

Template text and ids change, so stop realtime, back up and delete
`drain3_state.bin`, `training_checkpoint.json`, `training_event_index.jsonl`,
`template_registry.json`, `template_embeddings.pkl`, `group_registry.json`,
`group_centroids.pkl` and `models/global_v3.pkl`. Then build a new image tag,
`kubectl delete job logai-training`, apply, and run realtime on the same image.

---

## Check: speed / "job never finishes" risk on `full_node97.log`

- **Date:** Thursday, 2026-10-01 (+07), ~02:55 – 03:00
- **Method:**
  - Ran the real training Phase 1 (`TrainingPipeline._stream_parse`) and
    Phase 2 on the **whole file** (590 MB). The path is preprocess → Drain3
    (autosave off) → one state save per batch → event index → checkpoint.
  - Continuation lines were joined into one event, as Filebeat multiline would do.
  - Batches of 2,000, no `max_docs` limit (9× the production 200k).
  - Elasticsearch was not involved, so `fetch` time here is only the file read.
- **Result (new preprocessor):**
  - 1,816,742 events in **95.6 s** for Phase 1, plus 3.9 s for Phase 2.
  - **349 templates**, Drain3 state **20 KB**, peak RSS 370 MB.
  - Speed stays flat: 20.5k ev/s at 200k events, 19.0k ev/s at the end.
    Average parse per batch: 0.079 s in the first 100 batches, 0.084 s in
    the last 100.
  - Worst save per batch 0.04 s. Worst parse per batch 0.16 s.
  - Cluster count flattens: 225 at 200k, 292 at 1M, 349 at 1.8M.
- **Same data, old 4 rules, first 400k:** 188 clusters, 21.3k ev/s. The new
  preprocessor costs about 10% throughput and gives cleaner, correctly
  separated templates.
- **Conclusion:**
  - On this data Phase 1 for `max_docs=200000` takes ~10 s of CPU. The rest
    is ES fetch latency for ~100 requests.
  - Neither template growth nor save cost increases in a way that could keep
    the job running forever.
  - The ~140k clusters seen in production did not come from these node logs.
    They most likely come from the Drain3 state accumulated on the PVC, or
    from other services in the index. So the PVC reset before retraining is
    required.

---

## Task: Training phase logs + Drain3 placeholder fix + config review

- **Date:** Thursday, 2026-10-01 (+07), ~03:05 – 03:35
- **Status:** done, **uncommitted**. `pytest`: 267 passed.

### Phase logs (`logai/training/train_pipeline.py`)

- Log order now follows the real execution order: 1 Parse, 2 Template registry,
  3 Embedding, 4 HDBSCAN (now also reports clusters vs noise singletons),
  **5 Build groups** (overrides applied/unresolved, singleton groups, templates
  without a group), 6 Event windows, 7 Isolation Forest, **8 Publish**
  (registry, groups, centroids), 9 Documentation, 10 Cleanup (+ `Phase 10 complete`).
- Removed dead code that nothing called: `_build_group_registry`,
  `_compute_centroids`, `_parse_all`, `_build_template_registry`. Groups and
  centroids are built by `GroupAssignmentManager.build_groups`, which runs
  HDBSCAN output plus manual overrides.
- The job start now logs the Drain3, preprocessor and clustering settings.

### Bug fix: placeholder-heavy messages created a new cluster every time

`Drain.get_seq_distance` skips template tokens equal to Drain3's wildcard `<*>`.
A preprocessed message where at least half the tokens were `<*>` therefore
never reached `sim_threshold` against its own cluster, so every occurrence
created a new cluster. This was a latent template explosion.

Fix in `logai/parsing/drain3_parser.py`: preprocessor placeholders go into
Drain3 as the literal `<#>`, which Drain3 counts as a match, and
`display_template()` turns them back into `<*>` everywhere templates are
exposed. A regression test is in `tests/test_preprocessor.py`.

### `sim_threshold` retuned: 0.5 → 0.6 (config.yaml and dataclass default)

With placeholders now counted as matches, 0.5 merged distinct errors
("stream is closed" with "connection reset by peer").

| sim_th | node96 200k | node97 400k | HDFS 31 variants |
|---|---|---|---|
| 0.5 | 217 | 212 | 29 (merged 2 errors) |
| **0.6** | **223** | **216** | **30 (correct)** |
| 0.7 | 234 | 226 | 30 |

### node97 traffic profile (full file, 00:00–16:07, 1.82M events, 316 templates)

- All events: median 1,854/min, p95 2,454/min, max 4,215/min.
- Per template: no template has a median active-minute rate ≥150; only 12
  ever peak ≥150/min. The busiest templates run ~100/min (median) and
  ~220/min (peak).
- Consequence: `alert.min_events_1m=150` blocks almost every template or
  small group from ever alerting (see the config review in the conversation).
- `max_docs=200000` covers only ~108 minutes of one node at this rate.

---

## Evaluation: template quality on full production logs (node96 + node97)

- **Date:** Thursday, 2026-10-01 (+07), ~03:40 – 03:55
- **Scope:** templates only. Groups need bge-m3 embeddings, which are not
  available on this machine, so the user chose to skip them.
- **Setup:** current code (preprocessor + `<#>` literal placeholder, sim_th 0.6);
  continuation lines joined per event. Script:
  `scratchpad/tpl_eval.py`.

| | node96 | node97 |
|---|---|---|
| Events (full day, 00:00–16:07) | 1,859,660 | 1,816,742 |
| Templates | 342 | 316 |
| Templates covering 99% of events | 88 | 80 |
| Singletons / ≤10 events | 25 / 109 | 27 / 111 |
| Throughput | 19.9k ev/s (94 s) | 20.6k ev/s (88 s) |
| Level found in the first 5 tokens | 100% | 100% |
| Templates with a date, digit, level or thread left | 0 | 0 |
| Split candidates (pairs differing in 1 token) | 0 | 2 (7 and 2 events) |
| Median / max tokens; templates hitting the 40-token cap | 9 / 40; 44 | 9 / 40; 39 |

- **Saturation:** templates still grow ~10–25 per 10% of the day. These are
  rare message types, not noise.
- **Cross-node:** learning on node96, only 25 node97 events (0.001%, 15
  templates) hit an unseen template.
- **Production-like `max_docs=200k` on node97:**
  - first 200k (current behaviour): 0.03% of the rest of the day unseen,
    **107** unseen templates;
  - 200k sampled evenly across the day: 0.02% unseen, **74** unseen templates.
  - The unseen ones are rare business or error messages (LixiHandler,
    TransferHandler, ExchNode…). In realtime they become PENDING and are not
    scored.
- **Semantic loss from Drain generalisation:** 70–79 templates had a word
  position generalised. Most of them are free-text user input (`params
  khang`, `Dcu m …`). One is a real loss: `ussd rsp … content: <*>` merges the
  result codes `SYNTAX_ERR` / `NAME_CORRECT` / `PREPAID_NOPASS`. Also
  `con-<*>-Sender: send <*>` merges `send message` / `send login`.

---

## Check + hardening: real ES doc (`udcntt-vtn_cntt_vas_066`, gossip log)

- **Date:** Thursday, 2026-10-01 (+07)
- **Status:** done, **uncommitted**. `pytest`: 278 passed.
- The real doc parses correctly:
  - service `vtn_cntt_vas_066` comes from `service_code2`;
  - level `DEBUG`;
  - metadata keeps only `groupModule`/`moduleCode`;
  - the Drain3 input has no date, level or thread, the IP becomes `<*>`, and
    `WARNING` inside `NewGossipRouterWARNING_...` is not taken as the level.
- Fixes so that an odd field falls back instead of dropping the hit:
  - `message` that is not a string becomes `str()`, and `null` becomes `""`;
  - `@timestamp` that is a dict, list or garbage falls back to now; epoch
    millis (> 1e11) are divided by 1000;
  - `log.level`/`level` are stripped and upper-cased.
- Preprocessor: a leading timestamp + `[thread]` with **no** level
  (`30/09/2026 14:00:31 [main] x`) is now removed (`_strip_leading_timestamp`).
  On node96 (first 300k lines) this case never occurs, so templates are unchanged.
- Tests: `TestProductionGossipHit`, `TestOddFieldsAreIgnored`, `TestNormalizeMissingParts`.

---

## Check: wipe-and-retrain readiness with remote BGE-M3

- **Date:** Friday, 2026-10-02 (+07), ~01:00 – 01:20
- **Branch:** `calibrate` (base commit `9bdca62 Changing config`)
- **Status:** done, no engine code change.
- **Result:** the code path works from an empty `data/` dir.
  - `pytest`: 287 passed.
  - A fresh E2E run (empty data dir → training → realtime) passed against a fake
    OpenAI server and against the real `bge-m3` TEI container (both `openai`
    `/v1/embeddings` and `tei` `/embed`).
  - 200 texts in 4 batches, including a 50k-char text, also passed on the real TEI.
  - TEI runs with `max_client_batch_size=64` (same as LogAI `batch_size`) and
    `auto_truncate=true`; CPU speed is ~0.1 s per text.
- **Deploy blockers found:**
  - Elasticsearch was stopped.
  - `.env` was an empty **directory**, so compose fell back to its defaults:
    `LOGAI_EMBEDDING_ENDPOINT=""` (training aborts) and
    `LOGAI_ES_INDEX=app-logs-*`, which overrides `config.yaml`. With the wrong
    index, training reports SUCCEEDED with 0 templates and no model.
  - The running containers were built from an image 3 days old.

---

## Task: Real-log demo — `recharge-log-replayer` (node96 train, node97 realtime)

- **Date:** Friday, 2026-10-02 (+07)
- **Time:** ~01:25 – 02:50 (plan approved ~01:40, replayer tests green ~02:00,
  local E2E ~02:00 – 02:10, Docker demo run by the user from ~02:15)
- **Branch:** `calibrate` (base commit `9bdca62 Changing config`)
- **Status:** done, **uncommitted** (engine config only; the replayer is a new
  project outside this repo).
- **Plan:** `/home/cong/.claude/plans/jiggly-watching-bentley.md`
- **Decisions by user:**
  - one service `recharge`; `host=node96|node97` is metadata only;
  - node96 00:00–04:00 is the training source; node97 from 04:00 is replayed;
  - faithful replay only, no injected incidents;
  - CLI + FastAPI control panel.

### New project `/home/cong/recharge-log-replayer`

| File | Role |
|------|------|
| `replayer/logfile.py` | Streams a log file and joins continuation lines (SOAP/XML, `key: value`) into the entry of the header above them (Filebeat-multiline style). Parses time in `Asia/Ho_Chi_Minh`. Caps a message at 16,384 chars (`truncated=true`). Window by clock time `--from/--to`, tolerating ~2 s of thread reordering. |
| `replayer/es_sink.py` | Index `recharge-logs` with **1 shard, 0 replicas** plus mapping. Doc = full raw entry as `message`, plus `service/host/level/thread/logger/orig_timestamp/source_line/truncated`. `_id = node-line`. |
| `replayer/train_loader.py` | `load-train`: shifts the window so it ends at `now - 60s`, then bulk-loads it. |
| `replayer/realtime.py` | `Replayer`: emits at the original pace / speed with `@timestamp` = emit time. Speed can change live (re-anchored), stop is graceful, status is exposed. `_id` gets a per-run suffix, because LogAI dedups by `_id`. |
| `replayer/docs_seed.py` | `seed-docs`: 10 recharge runbook entries pushed through `POST /api/documentation`. Idempotent by title, threads the corpus revision. |
| `replayer/cli.py`, `replayer/app.py`, `templates/index.html` | CLI `load-train` / `replay` / `seed-docs` (SIGTERM = graceful stop). Panel on :8001 for train-load, start/stop/speed and status. |
| `Dockerfile`, `docker-compose.yml` | Joins `aiops-net`; logs mounted read-only at `/logs`. |
| `tests/` | 18 tests: multiline, truncation, timezone, window, time shift, pacing (10x), out-of-order, live speed change, stop, unique timestamps, seed-docs, panel smoke. |

### Engine-side changes (`logai-engine`)

| File | Change |
|------|--------|
| `config.yaml` | `elasticsearch.index: recharge-logs`; `training.max_docs: 200000` → `500000` (the 4 h window is 434,636 entries). |
| `.env` (gitignored) | Empty directory replaced by a file from `.env.example` with `LOGAI_ES_INDEX=recharge-logs` and `LOGAI_EMBEDDING_ENDPOINT=http://bge-m3:8080/v1/embeddings`. |

### Bug found: collector can skip docs that share a millisecond

`ElasticsearchCollector` pages with `search_after` on (`@timestamp`, `_doc`).
`_doc` is not stable across Lucene segment merges, so docs that share a
millisecond at a page boundary can be skipped or re-read.

- **Observed:** 4 of 35,289 replayed docs were never processed (all at
  `19:03:43.089`), and ~1,370 docs were re-read (dedup caught them).
- **Workaround in the replayer:** every doc gets a unique millisecond (strictly
  increasing in replay, nudged apart in train-load).
- **After the workaround:** 0 of 30,214 missing, re-reads down to ~200.
- **Still open in the engine:** production logs have many same-ms ties. Fix by
  polling `@timestamp >= last_ts - overlap` and relying on dedup, or by sorting
  on a stable unique field.

### Verification

- **Splitting is lossless on both full files:**
  - Re-joining all entries reproduces every line, in order (identical).
  - node96: 4,661,638 lines → 1,859,660 entries. node97: 4,554,819 lines →
    1,816,742 entries.
  - 0 lines before the first header, 0 date-prefixed lines missed as headers,
    0 invalid UTF-8 lines.
  - The demo windows match `grep`: node96 00:00–04:00 = 434,636 entries;
    node97 04:00–end = 1,390,901 entries.
- **Local E2E** (scratch data dir, real ES + bge-m3, engine on :9118, web on :5556):
  - load-train: 434,636 docs in 20 s.
  - Training: **67 s**, peak RSS 660 MB, 247 templates, 122 groups.
  - Realtime: 0.08 ms/event, 0 failures, empty DLQ.
  - seed-docs: 10 created, 8 groups documented (cosine 0.76–0.84).
- **Docker demo (run by the user):**
  - Index has 434,636 docs; training produced 247 templates.
  - Replay at 1x, then 10x: processed count equals the ES count, 0 failures,
    0.14 ms/event.
  - New templates appeared exactly as the offline simulation predicted: first
    at orig 04:17:49, first pending at 04:19:23 (T00249 `getAmountLixi`, cosine
    0.862), next pending at 05:33:45.

### Evaluation of templates and groups (training artifacts from the volume)

- **Templates — good.**
  - 247 templates; the top 20 cover 86% of events; 43 templates cover 99%.
  - Logger "impurity" is only thread or connection numbers in the logger name.
  - Only 1 template mixes levels (2 events).
  - node97 04:00–08:00 matched against the trained tree: **99.988%**.
- **Groups — fair.**
  - 17 HDBSCAN clusters (142 templates, 83% of events) + 105 singletons.
  - Silhouette (cosine) 0.33.
  - Problems:
    - `connection down. Try reconnect` (WARN) sits in G0000 with 96k normal
      send/receive events.
    - G0007 mixes 40k success logs with `Transaction timeout` / `Error when
      log request his`, and the whole group is labelled DOC-005 (timeout).
    - G0006 mixes `have password` with `don't have password`.
  - Cause: embeddings are dominated by the `TransactionInfo{…}` field list,
    and negation barely moves the vector.
- **Realtime simulation over all of node97 04:00–end** (1,390,901 events):
  - 99.98% hit trained templates.
  - 75 new templates: 31 auto-assigned (all sensible), 44 pending, including 2
    ERROR templates that are therefore not scored.
  - About 20 of the 75 are free text typed by users (`Wish valid: …`,
    `Name valid: …`, `param:…`).
  - 115 of 122 groups receive traffic, with the same event share as in training.

### Alert analysis (user saw INFO groups alerting)

- **Cause:** the replay speed changed from 1x to 10x at 19:38 UTC, so traffic
  went from ~1.2–2.2k to 15–19k docs/min.
- **Reconstructed for G_SINGLE_0000** (`ConvertUtil`, INFO), with the same
  `FeatureEngine` and model:
  - at 1x: 42–75 events/min, score 0.36–0.47;
  - at 10x: 330–660 events/min, `z_1m` = 10 (clipped), score 0.70–0.76.
- **Why INFO groups alert:**
  - the model is rate-only and level-agnostic;
  - the UI "Level" column is just the most severe template in the group;
  - the hysteresis counts **events**, not time, so 10 + 30 high scores take
    ~4 s at 600 events/min.
- **Recovery:** the baseline (30 min median) adapts ~15–20 min after the speed
  change, if the speed is left unchanged.

### Notes / follow-ups (prioritised)

1. Fix the collector `_doc` tie paging (see above).
2. Split HDBSCAN clusters by severity (ERROR/WARN vs INFO/DEBUG) and embed only
   the leading wording, not the `TransactionInfo{…}` dump.
3. Make `alert.min_events_1m=150` relative to the group's own baseline, and add
   a separate rule for ERROR/WARN groups. Small error groups can never alert today.
4. Give new ERROR/WARN templates a provisional singleton group instead of
   PENDING. Consider an assignment threshold of ~0.85: the near-threshold
   cases (0.86–0.879) all had the right nearest group.
5. Mask free text after `Wish valid:` / `Name valid:` / `param:` in the preprocessor.
6. Train on at least a full 24 h day. RAM grows with events (660 MB per 435k),
   so feature vectors need sampling or streaming.
7. Count hysteresis in time (e.g. consecutive 10 s windows), and correlate
   simultaneous alerts into one incident.
8. Fail training when 0 events or templates are found.
9. Persist feature windows (`window_state.json`).
10. Mask PII (msisdn, imsi, `encryptedPass`, BCCS credentials) at ingest before
    production.
- **Demo rule:** pick the replay speed before Start and do not change it while
  running.
- Scratch scripts (`fresh_e2e.py`, `verify_split.py`, `eval_tg.py`,
  `sim_realtime.py`) live in the session scratchpad and are not part of either repo.
