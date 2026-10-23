# Whole pipeline: stress test, normal operation and incidents (2026-10-24)

Script: `scripts/stress_pipeline.py`; `tests/test_stress_pipeline.py` runs it at `--scale tiny`
(N1, I1, I5, one I6 case). Each scenario was run on its own:
`python scripts/stress_pipeline.py --dsn postgresql://logai:logai@localhost:5433/logai_test --scenarios <name>`.

## How it works

- **Real engine code:** `Engine` itself (`start`, `tick`, `_flush`, the `run_forever` error handler copied
  into the driver loop), so `Reader`, `KnownLines`, `Owner.handle` with real Drain3 miners, detection,
  `Owner.flush` and the status are the production paths. Only the clock, `Engine.sleep` and the source are
  injected.
- **Source:** a generator with the `EsSource` interface, one index per service (`stress-svc00`, ...). Hits are
  made lazily per `page(index, from_ms, upper_ms)` call, never stored: event `k` of a stream is at
  `a_ms + k*60000 // per_minute`, its template, module and variables come from `splitmix64(seed ^ k)`, so any
  range can be regenerated and the ground truth of any interval is a formula. Lines look like the recharge
  logs: `2026-10-21 10:00:01,123 ERROR [pool-3-thread-7] <4-8 words> msisdn=849... transId=<hex> amount=...`;
  30 % of templates carry `channel=<USSD|SMS|...>` (unmasked words: several known-line cache entries per
  template). Per service 300 templates: 10 % ERROR (1 % of events), 15 % WARN (4 %), rest INFO, Zipf within a
  class; each template has a module of 5 per service (10 % of its events in a second one, 5 % no module).
- **Setup per scenario** (throwaway schema `stress_pipe_<hex>`, dropped in `finally`, then `CHECKPOINT`):
  Drain3 is pretrained on every template and channel variant (state saved in `drain3_state` as on a running
  engine); `templates`, `services`, `modules` filled; five past weekdays of `hourly_counts`/`service_hours`
  written from the generator's rates (mean = variance = rate per minute, Poisson); `commit()` builds a full
  baseline; `detection_enabled` set as after the first catch-up; `bootstrap` set so the engine reads from the
  scenario start. The busiest ERROR template of `svc00` is manually linked to document `stress`.
- **Time:** simulated start Wednesday 2026-10-21 10:00 local. The clock advances by the engine's own wall time
  per tick (generator time excluded), a modelled ES latency per page and every engine sleep. ES latency model
  from E1: **2.5 ms per page + 10 us per returned hit** (a 10k page 104 ms). The engine never sleeps while
  events arrive (as in production), so live ticks are small.
- **Disk guard:** rows x 150 B x 2 (WAL) per scenario against half the free space on `/` (or `--max-bytes`);
  refused above (exit 2). A scenario also stops if `/` drops below 600 MB free. Free space stayed at
  2,122-2,151 MB through all runs.

## Machine

AMD Ryzen 7 8845H (16 threads), 12 GB RAM (~3 GB available, swap full), NVMe, Linux 7.0. PostgreSQL 16.15
(`pgvector/pgvector:pg16` in Docker, defaults: `shared_buffers` 128 MB, `max_wal_size` 1 GB,
`checkpoint_timeout` 5 min). Elasticsearch 8.12.2 (single node, Docker). Python 3.14, psycopg 3. The live
logai2 stack (engine reading `recharge-logs`, web, linker) and the recharge replayer kept running on the same
Postgres and ES: numbers include that contention. Background WAL from the other databases was 0.6-235 KB/s
during the runs (corrected below where noted).

## Parameters

10 services, 300 templates each (3,000), 2,000 events/s total (200/s per service) unless stated; flush every
2 s or 50k events; `upper_bound_seconds` 30; floors ERROR 5, WARN 10; 3 sigma (full baseline); warm 2,
alert 3, cool 3 minutes.

## Results

Times are wall seconds of the engine's own work (generator excluded). p50 / p95 / max.

### N1 steady state

| Measure | 2,000 events/s | 10,000 events/s |
|---|---|---|
| Simulated minutes / events | 30 / 3.54M | 30 / 17.7M |
| Engine CPU for 30 min | 188 s (10 % of a core) | 481 s (27 %) |
| Engine throughput (events / engine second) | 18,800/s | 36,800/s |
| Poll (reader, excl. ES) ms | 1.0 / 1.3 / 74 | 5.9 / 8.3 / 128 |
| Handle (owner, Drain3 on misses) ms | 0.1 / 0.2 / 140 (cold cache) | 0.6 / 1.0 / 258 |
| Flush, all (894) ms | 31 / 42 / 316 | 47 / 60 / 338 |
| Flush with a closed minute ms | 194 / 271 / 316 | 280 / 330 / 338 |
| Simulated lag s | 30.0 / 30.1 / 31.0 | 30.0 / 30.2 / 31.0 |
| Process RSS peak | 143 MB | 277 MB |
| Row writes (30 min) | 1.45M (templates UPDATE 1.02M) | 2.64M (templates 2.03M) |
| WAL per simulated hour | ~370 MB | ~660 MB |
| Table growth (30 min) | 14 MB | 15 MB |
| Counted vs ground truth | 3,480,000 = 3,480,000 | 17,400,000 = 17,400,000 |
| Alerts raised (false) | 0 | 0 |

The live per-tick cost is mostly fixed (commands poll, status, small batches): at 2,000/s the engine
processes ~100 events per tick. At 10,000/s it uses ~27 % of one core, so live capacity is roughly
35-40k events/s before lag grows past the 30 s floor; catch-up reaches 55k/s (N2).

### N2 catch-up (2 hours of backlog at 2,000/s)

| Measure | Value |
|---|---|
| Events | 15.19M (14.4M backlog + new ones) |
| Time to caught up (lag < 60 s) | 7.2 simulated minutes (engine 274 s + modelled ES ~150 s) |
| Engine throughput | 55,400 events/s (35,300/s with modelled ES time) |
| Poll (10 indices x 10k page, excl. ES) ms | 1,565 / 1,611 / 1,671 |
| Handle ms | 28 / 58 / 78 |
| Flush ms (153 flushes) | 186 / 239 / 426 |
| RSS peak | 134 MB |
| WAL | 341 MB (2.1M row writes) |
| Counted vs ground truth | 15,120,000 = 15,120,000 |

10 equal-rate indices advance together, so B5 (memory when indices run far apart) was not provoked.

### I1 error burst (svc00's busiest ERROR template, 34/min, x20 for 10 min)

| Key | WARMING | ALERTING | NORMAL after the burst |
|---|---|---|---|
| template `svc00:T00001` | 2 min | 3 min | 3 min |
| doc `doc:stress` (linked) | 2 min | 3 min | 3 min |
| `*error` | 2 min | 3 min | 3 min |

Minutes are counted to the end of the judged minute; the alert is visible ~30 s later (`upper_bound_seconds`)
plus at most one flush (2 s). 12 `alert_changes` rows, 144 `alert_minutes` rows, no false alert on other
services, counts exact.

### I2 volume drop (svc01 at 10 % for 15 min)

`*total` WARMING after 2 min, ALERTING after 3 min, NORMAL 3 min after recovery. No other alert. Counts exact.

### I3 template storm (5,000 new stack-trace-like templates in 5 min on svc04, ~6 events each)

| Measure | Value |
|---|---|
| New templates | 4,987 (13 storm lines merged by Drain3) |
| Handle ms | 0.1 / 1.6 / 170 |
| Flush ms | 33 / 287 / 841 (with a closed minute: 208 / 841 / 841) |
| Engine throughput | 14,600 events/s (18,800 in N1) |
| `error_samples` rows | 35,716 |
| Drain3 blob of svc04 | 354 KB |
| Unparsed events | 0 |
| RSS peak | 171 MB (+28 MB) |
| Alerts | `svc04 *error` (expected), one merged storm cluster over the ERROR floor |

### I4 late data (svc02's index 3 min late for 10 min)

| Measure | Value |
|---|---|
| Ground truth / counted | 2,880,000 / 2,760,000 |
| Dropped as late (`late_counts`) | 90,006 |
| Never read, not counted anywhere | 29,994 |
| Detection | false `*total` drop on svc02: ALERTING after 3 min |

The silent loss: when the delay ended, newer docs became visible first; the reader's position passed the
older ones still hidden, and it never reads behind its position. See docs/BUGS.md B4.

### I5 Postgres outage (both connections closed, reconnect refused for 2 simulated minutes)

| Measure | Value |
|---|---|
| Tick failures / flush failures | 5 / 1 (`OperationalError: the connection is closed`, then `Owner.flush needs an idle connection` on the dead owner connection, reconnected) |
| Lag max | 180 s (2 min outage + 30 s backoff after each failed tick) |
| Lag back under 60 s | 0.66 min after the database came back |
| Ground truth / counted | 1,680,000 / 1,680,000: no loss, no double count |
| Template keys sum = `*total` | yes |
| Stall | no |

### I6 bad data (one doc every 20 s for 2 min in svc00's index)

| Case | Tick failures | Flush failures | Result |
|---|---|---|---|
| NUL in message / service / module | 0 | 0 | stripped by `hit_to_line` (F1), counts exact |
| Lone surrogate in message | 6 | 0 | `UnicodeEncodeError` in `hit_to_line`; 20,010 of 840,006 events lost (rest of each poll), lag max 211 s |
| Lone surrogate in service | 5 | 19 | tick fails in `Owner._see` (`crc32(service.encode())`); Drain3 already dirtied the bad service, its `drain3_state` row fails every flush: **stalled** (B1) |
| Lone surrogate in moduleCode | 0 | 24 | flush fails in the psycopg dumper: **stalled** (B1) |

"Stalled" = 600 simulated seconds without a successful flush; the engine kept retrying and stopped reading.
B2 is still open, as expected; details added there.

### I7 module cardinality spike (svc03's moduleCode request-id-like, 10,000 values, 5 min)

| Measure | Value |
|---|---|
| `modules` rows | 10,028 (50 before; ~9,980 of the 10,000 values seen) |
| `template_modules` rows | 57,836 (from ~6,000) |
| `template_module_hours` rows | 57,836 |
| Flush with a closed minute ms | 238 / 499 / 499 |
| Counts | exact |

The rows stay forever (`template_modules` is never deleted): docs/BUGS.md B13.

### E1 real Elasticsearch (temporary index `stress-pipeline-<hex>`, deleted after)

| Measure | Value |
|---|---|
| Docs indexed (bulk) | 250,000 in 7.6 s, 31.6 MB |
| Reader catch-up (real `EsSource` + `Reader`) | 37,400 docs/s |
| Page of 10k hits | 104 ms |
| Small page (< 100 hits) | 2.4 ms |
| Live (500 docs/s bulk-indexed with `@timestamp` = now) | newest doc read 0.7 / 1.1 / 1.1 s after its timestamp |

End-to-end, a doc is in ES ~1 s after its timestamp (refresh), its minute closes `upper_bound_seconds` (30 s)
after the minute ends, and the next flush (<= 2 s) writes it and its alert.

## Findings

1. Throughput is not a limit at the planned rates: 2,000/s costs 10 % of a core, 10,000/s 27 %; catch-up runs
   at ~55k events/s (engine) / ~35k/s with ES time; 2 h of backlog clears in ~7 minutes. RSS 130-280 MB.
2. Detection timing is as designed: 3 minutes to ALERTING for a burst and a drop, 3 minutes back to NORMAL,
   plus ~30 s for minute closing. No false alerts in 30-minute normal runs at both rates.
3. Exactness: every run without injected bad data matched the generator's ground truth to the event, including
   the outage (I5).
4. **Write amplification (B12):** ~70 % of row writes are `templates_seen` UPDATEs rewriting the same
   minute-resolution `last_seen` on every 2 s flush: ~370 MB WAL per hour at 2,000/s, ~660 MB at 10,000/s.
5. **Late data (B4):** beyond the counted late docs, out-of-order late arrival loses docs silently; detection
   raises a false `*total` drop.
6. **Bad data (B2/B1):** lone surrogates still fail ticks (message) or stall the engine (service, module).
7. Flush with a closed minute stays under 0.35 s in normal runs; incidents push it to 0.5 s (module spike)
   and 0.84 s (template storm), well under the 2 s cadence.

## Not run, or scaled down

- N2's live tail after the backlog was capped at 30 simulated minutes (it stops when caught up, at 7 min); the
  first N2 attempt (60 min tail) was refused by the disk guard (estimate 2.3 GB), which then charged the
  backlog with a 2 s flush cadence; the estimate now uses the 50k-event flush of a catch-up.
- E1 indexed 250k docs (31.6 MB), not 100 MB: enough for steady 10k pages and the latency model.
- Clock model: ES latency is modelled from E1, not measured per page in N/I scenarios; real ES under load
  (or a remote cluster) is slower. One index per service; B5's uneven indices were not exercised.
- Generator rates are flat over the day; real diurnal shape and over-dispersed (bursty) traffic were not
  modelled, so the false-alert result holds for Poisson-like noise only.
