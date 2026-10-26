# Throughput: profile, faster normalize, parallel parsing, pattern-sharded scale-out

Status: **plan for review** (2026-10-26). No task is dispatched until the user says start.
Task ids 73-77 (61-72 are taken by the baseline bake-off plan).

## Why

Production writes about 900k logs/s (design doc, `udcntt*`). One logai2 engine handles about 35k/s live
(docs/perf/2026-10-24-pipeline-stress.md: 10k/s = 27 % of one core; real-ES reader 37.4k docs/s; catch-up
55.4k/s engine-only, 35.3k/s with ES time). Earlier benchmark (agents.md 2026-10-22): ES read ~83k docs/s,
parse ~37k lines/s (normalize-bound), ~25k docs/s end to end in one thread.

So per engine the limit is **parsing in one Python thread** (`normalize` + known-line cache on every line,
even known ones), then the owner (~55k/s). Scale-out is needed anyway: even at 100k/s per engine,
production needs ~10 engines.

## Targets

| Step | Target (to verify by measurement) |
|---|---|
| After 74 (faster normalize) | 1.5-2x parse rate per core |
| After 75 (parallel parse) | 60-100k logs/s per engine on 4 cores, lag <= 60 s |
| After 76-77 (sharding) | N engines x per-engine rate; 900k/s with ~10-15 engines |

New templates (I3 storm: -22 % for minutes) and the LLM/linker (separate process, not in the ingest path) are
measured, not optimised, in this plan; 73 adds the missing linker/LLM throughput measurement.

## Tasks

| Id | Task | Depends on | Implementer / reviewer | Output |
|---|---|---|---|---|
| 73 | **Profile and measure**: cProfile + per-stage timers (ES fetch, `hit_to_line`, `normalize`, cache, owner `handle`, flush) for one engine at 10k, 25k, 35k ev/s in the stress harness; cache hit ratio; plus a linker scenario (X new templates, fake embedder/LLM of fixed latency: time to embedded, time to linked, LLM calls/h) | Retrain build merged | Sonnet / fresh Sonnet | `docs/perf/<date>-throughput-profile.md`, harness scenarios `P1-P3`, `L1` |
| 74 | **Faster normalize**: from 73's profile, cut the per-line cost without changing any output (fast paths for lines with no digits/XML/URL, merged regexes, fewer passes; a faster regex library only if baked into the image, no pip in the cluster) | 73 | Sonnet / fresh Sonnet + Opus check | golden test: normalize output byte-identical on a large corpus (synthetic + recharge sample templates), benchmark before/after |
| 75 | **Parallel parsing in one engine**: N reader worker processes (per pattern or hash of pattern), each doing ES read + `hit_to_line` + `normalize` + cache lookup, sending compact counts (and unknown lines) to the single owner process; the owner keeps Drain3, counting, detection, flush; positions and watermark stay exact (exactly-once unchanged) | 74 | Sonnet / fresh Sonnet + Opus adversarial (exactly-once path) | `cfg.reader.workers`; stress P-scenarios with 1/2/4 workers; crash and restart tests |
| 76 | **Sharding design** (spec only): each engine instance owns a set of selection patterns (a service lives in one pattern); per-instance selection in settings; one leader for the nightly rebuild, retention and retrain runs (advisory lock); web unchanged (same DB); per-instance status and cursors; how a pattern moves between instances; Postgres sizing for ~10 writers | 75 measured | Opus writes, user reviews | `docs/specs/<date>-sharded-engines.md` + mockup of Data sources with instance assignment |
| 77 | **Sharding build** (tasks split after 76 is approved) | 76 approved | Sonnet tasks, team workflow | k8s manifests: N engine Deployments or a StatefulSet |

Parallel: none in 73-75 (each needs the previous measurement). 76 can be drafted while 75 is in review.

## Roles (team workflow, docs/TEAM_WORKFLOW.md)

- **Opus (tech lead):** briefs, rulings, the 76 spec, final review and merge of each branch after review.
- **Sonnet implementers:** one per task, own worktree, tests first.
- **Fresh Sonnet reviewers:** every branch; Opus adds an adversarial review on 75 (exactly-once path) and a
  check of 74's golden test (no template text may change: a change would split or merge templates in
  production).

## Risks

| Risk | Handling |
|---|---|
| 74 changes normalized text: templates split or merge in production | byte-identical golden test over a large corpus; any difference fails |
| 75 breaks exactly-once (positions vs counts across processes) | owner stays the only writer; workers send counts tagged with pattern + position; owner advances cursors only after their counts are in its minutes; crash tests as in R3 |
| 75 memory per worker (cache per process) | shared nothing: each worker keeps the cache of its own patterns; sized by `reader.cache_size / workers` |
| Laptop numbers differ from company nodes | report cores/CPU in every perf doc; re-run P-scenarios in the cluster with synthetic data only (no real data leaves company servers) |
| New dependency for faster regex | only if baked into the image (cluster pulls Docker Hub only) |

## Not in scope

Kafka; sampling or ES-side aggregation (counts must be exact; mining needs the text); changing Drain3.
