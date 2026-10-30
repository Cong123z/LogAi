# Pattern On/Off switch and engine memory

Status: done, 2026-10-29, on build/pattern-switch 23ed042 (merges pm0-pm5); pm4 (web) and pm6 (proof) follow. Plan: `docs/plans/2026-10-29-pattern-switch-memory.md`.
Fixes B24, B25, B26 (new) and the memory findings below. Comes from the company incident of 2026-10-28
(vas_066: 22 h lag, flush stall on removal, OOMKilled at every start; pod at ~4,400 MiB since).

## 1. Problem

1. **Start loads every service.** `Owner.__init__` loads baseline rows, Drain3 trees and template levels of every
   service in Postgres, read or not. vas_066 alone: 135k templates, ~6M baseline rows, 3.9 GiB peak at load.
2. **A removed pattern's services are still zero-filled and judged (B26).** `Owner.services` holds every service;
   `close_before` gives each one `*total = 0` per closed minute; the detector judges it. Writes fake zeros into the
   history and raises false `*total` drop alerts.
3. **A big watermark jump closes every minute in one flush (B25).** Removing the 22 h-late pattern closed ~1,300
   minutes at once; the flush never finished.
4. **Memory grows while a pattern lags.** `Reader._seen` keeps every doc id since the boundary (only the ids of the
   position's millisecond are ever needed); `Owner._open_samples` keeps one sample line per aggregate per poll.
5. **Baseline is stored as 331 B per row** (dict of 4-tuples to NamedTuple; measured 2026-10-29, tracemalloc,
   960k synthetic rows). A packed array per (service, key) measured 20 B per row.

## 2. Decisions (user, 2026-10-29)

| # | Decision |
|---|---|
| D1 | Each selected pattern has an **On/Off switch** on Data sources. **Off is sticky**: off until a user turns it on. An Off pattern's indices stay excluded from every other pattern. **Remove** is different: the pattern is gone and a broader pattern may read its indices (and monitor its services). |
| D2 | Off = no monitoring: the engine does not read, load, zero-fill, judge or alert its services; it resets their alerts to NORMAL. **All data stays in Postgres.** |
| D3 | On again: monitoring resumes from the moment it was switched on (`enabled_at`). The gap stays a gap. |
| D4 | **Keep the current baseline.** Off hours have no rows, so they are not observed and nothing is learned from them (like an excluded accident). The nightly rebuild never deletes a service's baseline unless it produced new rows for it. |
| D5 | Off services are **hidden completely**: every page, API, picker and the linker. A direct URL answers 404. |
| D6 | The Alerts page hides them too. |
| D7 | Memory is checked with numbers: a process-memory metric and a synthetic vas_066 probe, before and after. |

## 3. Rules

- **Active services** = services with a pair in `index_services` under an On pattern, minus services with a pair
  under an Off pattern (**Off wins**), plus services first seen in a batch (not Off). Only active services are in
  memory, zero-filled, judged and exported.
- A batch count of an Off service (reached through another pattern's index) is dropped and counted in
  `skipped_off`.
- `services.monitored` (new column) = active. Every web query and the linker filter on it.
- Switching Off: services leave the active set **before** the next minutes close; their open minutes are dropped,
  their non-NORMAL alert states become NORMAL with an `alert_changes` row; memory is handed back (`malloc_trim`).
- Switching On: the engine loads the services' state (compact baseline, Drain3 blob, levels); the pattern reads
  from `enabled_at`.
- At most `flush.max_closed_minutes` (60) minutes close per flush; the engine flushes when the cap is reached.
- Pipeline warns when the watermark lags more than `reader.lag_warn_seconds` (600), naming the patterns holding it.

## 4. Non-goals

- Throttling Drain3 blob writes: a blob older than the cursors would reuse cluster ids already stored for other
  templates after a crash. If the probe shows the per-flush blob of a 135k-cluster tree is too costly, that needs its
  own design.
- Bounded read-ahead between patterns: with the two memory fixes in §1.4 a lag costs only counts.
- The lab pages (Evaluation) and lab tables: unchanged.
- Per-pattern watermarks (rejected in `docs/design/per-service-baseline.md`).
