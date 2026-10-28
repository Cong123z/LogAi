# Known bugs and risks

Open items to fix, newest findings included. When one is fixed, move it to
**Fixed** with the commit. Likelihood is a guess until production shows it.

## Open

### B16. M6 decisions need a Test B M6 tune alert rate
- **Where:** `scripts/lab/decide.py` (`_gates`, `_detect_decision`), `scripts/lab/tune.py`.
- **What:** the M6 alert-rate gate reads the Test B M6 tune alert rate. When no template was born in the tune part there are no
  M6 normal minutes there, the rate is None, the gate fails, and M6 is always "sigma stays" on such data.
- **Fix idea:** judge the M6 alert rate on the test part, or on the base-kind minutes of the new templates.
- **Likelihood:** medium on short or quiet windows.

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

### B14. One failing index fails the whole pattern's search
- **Where:** `EsSource.page` (one search per pattern, task 51).
- **What:** any per-index search error other than an unmapped `@timestamp` (a closed or red shard, a
  mapping clash on another field) now fails the pattern's whole search; after `FAILING_AFTER` polls
  the pattern is failing and its good indices stop being read too.
- **Fix idea:** on a pattern failure, fall back to searching its resolved indices one by one.
- **Likelihood:** low.

### B15. A flush can get stuck on a dropped lab / module-hours partition
- **Where:** `Owner.handle` (`lab_cutoff`, `mod_cutoff`), `jobs/commit.py` housekeeping, `Owner.flush`.
- **What:** the cutoff day is computed in `handle()`. If the nightly commit drops the oldest
  `lab_minute_counts` (or `template_module_hours`) partition while pending rows for that day wait
  for the next flush, the flush fails on the missing partition. Pending rows are kept, so every
  retry fails until the engine restarts. Needs a catch-up over ~28-day-old (7-day for module hours)
  data across local midnight.
- **Fix idea:** re-check the cutoff in `flush()` and drop pending rows older than it.
- **Likelihood:** low.

### B17. Prophet's `uncertainty_samples` limits the 0.999 / 0.9999 band tails
- **Where:** `scripts/lab/methods/prophet_m.py`.
- **What:** Prophet's band comes from simulated samples; with the default sample count the 0.999 and 0.9999 quantiles
  rest on a handful of draws, so the noise-widening term dominates the tails and the two knobs differ little.
- **Measured (bk17, 28-day world, M2, test part):** prophet coverage at knobs 0.98 / 0.99 / 0.995 / 0.999 / 0.9999 is
  0.847 / 0.882 / 0.908 / 0.942 / 0.965 (ETS, same grid: 0.891 / 0.920 / 0.939 / 0.961 / 0.978). The band stays far
  under its nominal level at the tails and the last two knobs differ by 2 points.
- **Fix idea:** raise `uncertainty_samples` (cost grows with it) or use the analytic interval for the extreme knobs.
- **Likelihood:** certain for the extreme knobs; only matters if Prophet reaches the decision.

### B18. LightGBM cost at 3k keys is unmeasured
- **Where:** `scripts/lab/methods/lgbm.py`; spec `docs/specs/2026-10-27-bakeoff-laptop.md` §13.
- **What:** the laptop has 180 templates per service; the company has about 3k keys. Fit time and memory of the
  per-key models were not measured at that size, so the cost gate for LightGBM rests on the laptop numbers.
- **Measured (bk17):** 274 keys per service, 28-day Test B in the lab container: LightGBM fit 214 s over 87 walk-forward
  fits (2.5 s per fit), scoring 126 s for 10,752 key-days (11.7 ms per key-day), process peak 1.4 GB for all methods.
  Linear extrapolation (NOT measured) to 3,000 keys per service: about 27 s per fit and 3,000 x 28 x 11.7 ms = 16 min of
  scoring, so about 28 min per service for a 28-day Test B (fit 12 min + scoring 16 min); memory unknown (training rows grow
  to about 2 million). Still worth one real 3k-key timing before the company run.
- **Fix idea:** time one service with 3k keys (a synthetic world) before the company run.
- **Likelihood:** medium.

### B20. Test A injections on sparse floored keys cannot reach the count floor
- **Where:** `scripts/lab/inject.py` (`TYPES` sizes, `plan`).
- **What:** spike / ramp / level_shift add `(size - 1) x expected` to a key whose alert needs `count >= floor` (ERROR 5, WARN
  10 per minute, D4). On the 28-day world all 49 M4 injections (sparse ERROR/WARN templates, under 2/min) peak below the floor
  (`lab/proof/reach.py`: 0 of 49), so M4 recall is 0 for every method by construction; M3 has no eligible key (INFO
  templates have no floor, and no ERROR template has a busy mean of 2/min), so its recall is `-`. M2 is fine (100 % of spikes,
  86-92 % of ramps/level shifts reach the floor). The ramp hit window (15 min after the start of a 60 min ramp) sees at most
  a quarter of the added count, so ramp recall is 0 for every method on every world.
- **Fix idea:** size the addition so the peak minute reaches the floor (or skip the injection and say so), and measure ramp
  hits over the whole incident.
- **Likelihood:** certain; M3/M4/ramp recall and the "detect" gate for M3/M4 are not usable until fixed.

### B21. pca (M7) cannot see a mix swap on the synthetic worlds
- **Where:** `scripts/lab/methods/pca.py`, `scripts/lab/inject.py` (`mix_swap`).
- **What:** recall is 0 on all three worlds. Not an injection bug: a swap moves Q by about 2-5 while the noise Q of a normal
  minute is 30-38 and the 0.99 / 0.9999 thresholds are 51-67 (even swapping the busiest template with one 8 times smaller:
  Q mean 28.8 against a threshold of 52.9). Poisson noise on 100 low-count templates drowns a swap of two of them.
- **Fix idea:** a mix_swap that moves many templates, or PCA on hourly shares; first check on real data.
- **Likelihood:** certain on these worlds.

### B22. M5 (doc keys) is never exercised by the synthetic worlds
- **Where:** `scripts/lab/synth.py`.
- **What:** `template_doc_links` is empty in every counts world, so there are no `doc:` keys and every M5 row is `skipped`.
- **Fix idea:** link a few templates to documents in the scenario.
- **Likelihood:** certain.

### B23. A window needs recorded history before it
- **Where:** `scripts/lab/load.py`, `scripts/lab/run.py` (tune/test split).
- **What:** weekly, ETS, Prophet and LightGBM need 14 days of hourly history; mad, mprofile and pca need 7 days of minutes
  (now loaded, `MINUTE_HISTORY`). A world that starts at the window start leaves the tune half without a fitted day and the
  scoreboard row `skipped` with empty metrics. The synthetic worlds are therefore built with N + 14 days; real data needs
  a recording of at least window + 14 days before a Test B can rank these methods.
- **Fix idea:** none needed beyond the rule; the runner could warn when a method has no fitted tune day.
- **Likelihood:** certain for short recordings.

### B19. The lab judges some minutes differently from the live engine
- **Where:** `scripts/lab/methods/sigma.py`, `scripts/lab/load.py`.
- **What:** six known differences (late-closed minutes, retrain schedule, re-read counts, services with no committed
  day, template levels and doc links at load time, idle templates); none occurs in the synthetic worlds. The list is in
  spec `docs/specs/2026-10-27-bakeoff-laptop.md` §6.4.
- **Fix idea:** none planned; check the list against company data before trusting a "σ stays" verdict there.
- **Likelihood:** low on laptop worlds; unknown on company data.

## Fixed

### F7. Test A never committed its recall rows or the run status (fixed in bk17, `b5118d6`)
- **What:** `test_a.finish` committed, then read, then wrote inside the read's transaction (a savepoint), so closing the
  connection rolled the `scope = 'all'` rows and `finish_run` back: the run stayed `running` with per-service rows only.
- **Fix:** commit after the read; test `test_a_main_commits_finish_and_run_status`.

### F8. Test A injected where nothing can alert, and bursts as INFO (fixed in bk17, `9cde33a`)
- **What:** upward injections went on any template (INFO templates have no floor, D4 never flags them high) and the burst
  template was INFO, so recall was 0 by construction.
- **Fix:** spike / ramp / level_shift only on keys with a floor (`floor_for`), bursts as ERROR templates.

### F9. Test A chose the least sensitive tail probability (fixed in bk17, `58ea334`)
- **What:** with no alerts at any knob, `tune` took the numerically lowest knob; for nb / nb_clip / ratio / lgbm / chronos
  (grid 0.01 .. 1e-5) that is the least sensitive, so their Test A recall was near 0 and the detect gate failed them.
- **Fix:** `tune` follows the grid order (most sensitive first); `finish` passes the knobs in that order.

### F10. mad, mprofile and pca never fitted on the first 7 days of a window (fixed in bk17, `4cf66fb`)
- **What:** minute arrays were loaded only for the window, so the 7-prior-day fits failed and the tune half had no result.
- **Fix:** `load_service` keeps the minute arrays of the 7 recorded days before the window (`sd.days` is still the window).

### F11. A tie at recall 0 named a leader for M6 / M7 (fixed in bk17, `84fa91f`)
- **Fix:** zero best recall gives no leader, note "no method detects".

### F2. mprofile looked ahead inside the scored day (fixed in bk16, `14bc644`)
- **What:** a minute of day D was scored against the whole day's matrix profile, so it could match later windows of D;
  a key with an observed prior day but no array for it raised `KeyError`.
- **Fix:** each minute is scored with the left matrix profile (distance to the nearest earlier window), the history
  thresholds use the left profile too (day 1 of the history excluded), and a missing array is skipped.

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
