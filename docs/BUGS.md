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
- **Fix idea:** raise `upper_bound_seconds` (e.g. 120; alerts come later by the same amount).
- **Likelihood:** likely under load.

### B5. First catch-up memory when the index pattern covers several indices
- **Where:** `Reader._seen` / `forget_before`, `OpenMinutes`.
- **What:** with a data stream, alias or daily indices, every index is read in parallel
  but minutes close at the slowest one. Faster indices' counts and every doc id stay in
  memory until then; over 7 days this can pass the 4Gi limit -> OOMKilled loop.
- **Check:** `GET _resolve/index/hdfs`; one index = not affected.
- **Fix idea:** do not read an index far ahead of the watermark (skip its page while
  `pos - watermark` > a few minutes).
- **Likelihood:** only if several indices.

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

### B8. Index pattern matching nothing: detection never turns on
- **Where:** `Engine._caught_up`.
- **What:** with no resolved index, `_caught_up` is always false: no detection, no daily
  commit; the only sign is an empty indices list on the Pipeline page.
- **Fix idea:** a status warning when the selection resolves to no index.
- **Likelihood:** low (setup mistake).

## Fixed

### F1. NUL byte in an ES field stalled the flush
- 2026-10-23, merge `05ec099`: `hit_to_line` strips `\x00` from every stored field.
