# Known bugs and risks

Open items to fix, newest findings included. When one is fixed, move it to
**Fixed** with the commit. Likelihood is a guess until production shows it.

## Open

### B1. A value Postgres can never store stalls the engine forever
- **Where:** `Engine._flush` (`logai2/engine.py`), `Owner.flush`.
- **What:** a failed flush retries the same pending rows until it succeeds. A permanent
  data error (not an outage) fails every retry; the engine stops reading and alerting.
  Nothing is lost (cursors did not move), but the liveness probe (TCP on the metrics
  port) still passes, so Kubernetes never restarts it.
- **Seen:** 2026-10-23, NUL byte (that cause is fixed, see F1).
- **Fix idea:** alert on `logai_engine_heartbeat_timestamp_seconds`; or tell data errors
  (`psycopg.DataError`, `UnicodeEncodeError`) apart from an outage.
- **Likelihood:** low once B2 is fixed.

### B2. Lone UTF-16 surrogate (half an emoji) in an ES field
- **Where:** `hit_to_line` (`logai2/reader/es_source.py`), `Reader.poll`.
- **What:** a Java app cutting a string mid-emoji leaves e.g. `\ud83d` in `_source`.
  Verified 2026-10-23:
  - in `service` / `moduleCode`: passes `hit_to_line`, then `UnicodeEncodeError` in the
    flush -> B1 stall.
  - in `message`: `hit_to_line` raises on `.encode("utf-8")`; `poll` only guards
    `source.page`, so the tick fails. The hits already in `seen` are skipped on retry:
    the counts of that poll (up to a page) are lost silently.
  - Stress test 2026-10-24 (`scripts/stress_pipeline.py`, I6): in `message`, 6 bad docs failed 6
    ticks and lost 20,010 of 840,006 events (the part of the failing index's page read before the bad doc, whose ids are
    already in `seen` with the position moved, plus anything already aggregated in that poll). In `service`, the tick fails
    first in `Owner._see` (`zlib.crc32(service.encode())`), but `Miners.match` has already created
    and dirtied a miner for the bad service, so its `drain3_state` row stalls every flush (B1) even
    after the bad docs stop. `moduleCode`: flush stall. NUL in any field: no effect (F1 holds).
- **Fix idea:** in `hit_to_line`, `encode("utf-8", "replace")` every field (next to the
  NUL strip), plus a test.
- **Likelihood:** medium.

### B3. One rejected document or template blocks the linker
- **Where:** `Linker._embed_documents`, `_embed_templates` (`logai2/linker.py`).
- **What:** documents are taken in `doc_id` order (templates by `last_seen`). If bge-m3
  rejects one request (more ERROR lines than its batch limit, usually 32, or too long),
  `EmbeddingError` ends the pass and the same item comes first next cycle: nothing
  after it is ever embedded.
- **Fix idea:** split a document's lines into batches of `embedding.batch`; skip and
  warn on an item that still fails instead of ending the pass.
- **Likelihood:** medium (large documents).

### B4. Docs ingested late are dropped as late counts
- **Where:** `ReaderConfig.upper_bound_seconds` (30), `OpenMinutes.add`.
- **What:** a minute closes 30 s after wall time. A doc reaching ES later (Logstash or
  Filebeat backlog, usually during an incident) counts as late and is dropped: counts
  too low, missed alerts or false `*total` drops.
- **Check:** `late_counts` on the Pipeline page / `logai_late_counts_total` should stay ~0.
- **Measured** (stress test I4, 2026-10-24): one of 10 indices 3 min late for 10 min at 200 events/s:
  90,006 events dropped as late, plus 29,994 **never read and not counted anywhere**: when the delay
  ended, newer docs became visible first, the position passed the still-hidden older ones, and the
  reader never goes back. Those docs fall in minutes already closed, so the count loss is the same
  120k either way; the defect is that `late_counts` misses them. The service raised a false `*total`
  drop alert (ALERTING after 3 minutes). The model is a whole-index delay that ends abruptly; a
  partial delay (one late shipper among several writing into one index) would let on-time docs move
  the position past hidden ones during the whole delay, so real never-read loss can be larger.
- **Fix idea:** raise `upper_bound_seconds` (e.g. 120; alerts come later by the same amount).
- **Likelihood:** likely under load.

### B6. Postgres hostPath PV has no node pinning
- **Where:** `k8s/logai-postgres.yaml` (`logai-pg-pv`).
- **What:** if the Postgres pod moves to another node it gets an empty `/u01/logai-pg`,
  initdb runs, and the engine starts over from 7 days back.
- **Fix idea:** `nodeAffinity` on the PV (or `nodeSelector` on the StatefulSet).
- **Likelihood:** only on a multi-node cluster.

### B7. Postgres `/dev/shm` is 64 MB in a pod
- **Where:** `k8s/logai-postgres.yaml`; the nightly `REBUILD` in `logai2/jobs/commit.py`.
- **What:** a parallel query can fail with `could not resize shared memory segment`;
  the commit retries every 30 s and the baseline goes stale.
- **Fix idea:** `emptyDir: {medium: Memory, sizeLimit: 512Mi}` mounted at `/dev/shm`.
- **Likelihood:** medium as data grows.

### B8. Index pattern matching nothing: no detection until it matches
- **Where:** `Engine._status`.
- **What:** a pattern with no index reads an empty page, so it is caught up and detection is not
  blocked; the engine status carries the warning `no index matches <pattern>`.
- **Fix idea:** none needed; a typo in a pattern shows as that warning.
- **Likelihood:** low (setup mistake).

### B9. Two web tests fail at some local times of day
- **Where:** `tests/test_web_alerts_templates.py::test_alerts_grouped_by_document` (~line 38),
  `::test_templates_rate_and_band` (~line 69).
- **What:** time-dependent test data; seen failing at 00:09, 02:01 and 05:34 local, also on master.
- **Fix idea:** build the test data relative to a fixed clock, not the wall clock.
- **Likelihood:** certain at those hours; test-only.

### B10. Open UI tabs keep running old JS after a deploy
- **Where:** `logai2/web/static/js/`.
- **What:** until reloaded, a tab runs the old code against the new API (the old Pipeline
  view expects removed fields and loops on "connection lost").
- **Fix idea:** a build id in `/api/pipeline?lite=1`; the UI asks for a reload when it changes.
- **Likelihood:** every deploy that changes an API shape.

### B11. An LLM profile named `order` cannot be created or edited
- **Where:** `PUT /api/llm/profiles/<name>` in `logai2/web/app.py`.
- **What:** `PUT /api/llm/profiles/order` is the reorder route, so a profile with that name
  is unreachable.
- **Fix idea:** reject the name `order` with a 400 in the profile-name validation.
- **Likelihood:** low.

### B12. Every flush rewrites `last_seen` of every template seen in the last 2 s
- **Where:** `SQL["templates_seen"]` in `Owner.flush` (`logai2/owner/owner.py`).
- **What:** `last_seen` has minute resolution, but the UPDATE runs at every 2 s flush for each
  template seen since the last one, even when the value does not change (up to ~30 rewrites per
  template per minute, measured average ~11). Stress test 2026-10-24: ~70 % of all row writes (1.0M of 1.45M per 30
  simulated minutes at 2,000 events/s; 2.0M at 10,000/s); with the rest, ~370 MB of WAL per hour at
  2,000 events/s and ~660 MB at 10,000/s (9-16 GB a day). Mostly HOT, so tables stay small, but the
  WAL volume costs I/O and checkpoints (`max_wal_size` 1 GB on a nearly full disk here).
- **Fix idea:** `... WHERE ... AND (last_seen < %s OR level IS DISTINCT FROM %s)` (skip no-op rows),
  or only send `templates_seen` for templates whose minute closed.
- **Likelihood:** certain (cost, not correctness).

### B13. A request-id-like `moduleCode` grows `modules` and `template_modules` forever
- **Where:** `Owner._see` / `Owner.flush`; `template_modules` is never deleted (by design).
- **What:** stress test I7: one service's `moduleCode` carrying 10,000 distinct values for 5 minutes
  added 10,028 `modules` rows and 52k `template_modules` rows for good; the flush with a closed
  minute rose from ~0.27 s to 0.5 s. The Modules page and module filter then list thousands of
  junk modules.
- **Fix idea:** a cap of new modules per service per hour (warn and map the rest to `''`), or
  retention on `template_modules`/`modules` rows not seen for N days.
- **Likelihood:** low-medium (depends on the source's field hygiene).

## Fixed

### F1. NUL byte in an ES field stalled the flush
- 2026-10-23, merge `05ec099`: `hit_to_line` strips `\x00` from every stored field.

### B5. First catch-up memory when the index pattern covers several indices (fixed, task 51)
- **Where:** `Reader._seen` / `forget_before`, `OpenMinutes`.
- **What:** with a data stream, alias or daily indices, every index is read in parallel
  but minutes close at the slowest one. Faster indices' counts and every doc id stay in
  memory until then; over 7 days this can pass the 4Gi limit -> OOMKilled loop.
- **Check:** `GET _resolve/index/hdfs`; one index = not affected.
- **Fix idea:** do not read an index far ahead of the watermark (skip its page while
  `pos - watermark` > a few minutes).
- **Likelihood:** only if several indices.
- **Fix:** one cursor and one search per pattern, sorted by timestamp, so indices are no longer read in parallel.
