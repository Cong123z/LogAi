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
