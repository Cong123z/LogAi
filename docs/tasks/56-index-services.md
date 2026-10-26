# Task 56: `index_services` (pattern → services) and the oldest doc in ES per pattern

Implementer: Sonnet 5.5 — reader/owner flush path.
Reviewer: a fresh Sonnet 5.5 subagent
Branch: task/56-index-services (worktree ../logai2-task-56, created by the tech lead)
PR file: .team/prs/task-56-index-services.md (from docs/templates/pr.md; .team/ is in the main checkout
~/Documents/logai2, write it there)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below
is unclear, stop and reply `QUESTION: ...` instead of guessing. Never kill processes you did not start
(other agents share the test Postgres).

## Why
Task 57 (Re-read) must know which selected patterns hold a chosen service (to read every source of that
service) and how far back ES still holds each pattern's docs (hours older than that are never re-read:
writing them would store zeros). The live reader already sees both facts; this task records them.

## Read first
- Spec: docs/specs/2026-10-26-retrain.md §3. Plan: docs/plans/2026-10-26-retrain.md task 56.
- Code (after task 51, merged on master): `logai2/reader/reader.py` (`poll`, `MinuteBatch`, `_targets`),
  `logai2/reader/es_source.py` (`page`, `_search`, `resolve`), `logai2/owner/owner.py` (`__init__` loads,
  `handle`, `_clear`, `flush`, `SQL`), `logai2/engine.py` (`_refresh`, `_status`, `AVAILABLE_REFRESH_SECONDS`),
  `tests/fakes.py`, `tests/test_reader.py`, `tests/test_owner.py`, `tests/test_engine.py`, `tests/test_es_source.py`.

## Files
- Create: `logai2/migrations/009_index_services.sql`
- Modify: `logai2/reader/reader.py`, `logai2/reader/es_source.py`, `logai2/owner/owner.py`, `logai2/engine.py`,
  `tests/fakes.py`, `tests/test_reader.py`, `tests/test_owner.py`, `tests/test_engine.py`,
  `tests/test_es_source.py`, `ARCHITECTURE.md` (§5 contracts, §11 schema, status fields table)
Touching any other file needs a QUESTION first. (Task 54 adds migration 008 in parallel; do not create 008.)

## Interfaces
Migration 009 (exactly):
```sql
CREATE TABLE index_services (pattern text NOT NULL, service text NOT NULL, PRIMARY KEY (pattern, service));
```
- `MinuteBatch` gains `sources: list[tuple[str, str]]` — distinct `(pattern, service)` pairs of the lines
  read in this poll (the pattern is the reader key, not the concrete index).
- Owner: loads all `index_services` rows into an in-memory set at `__init__`; `handle(batch)` queues pairs not
  yet in the set; `flush` inserts them in the same transaction (`INSERT ... ON CONFLICT DO NOTHING`) and adds
  them to the set only after the commit (a failed flush keeps them pending, like other pending rows).
- `EsSource.oldest_ms(target: str) -> int | None`: one search, `size: 0`, `aggs: {"oldest": {"min":
  {"field": "@timestamp"}}}`, same `ignore_unavailable` / `allow_no_indices`; returns epoch ms or None when
  no doc.
- Engine: every `ES_OLDEST_REFRESH_SECONDS = 600` (and at the first `_refresh`) computes
  `self.es_oldest = {pattern: source.oldest_ms(reader target of pattern)}`; a failure logs a warning and keeps
  the previous values. Status gains `"es_oldest": {pattern: ms | None}`.
Later tasks rely on: table `index_services`, status `es_oldest`, `EsSource.oldest_ms`.

## Requirements
1. Exactly one insert per new pair over the engine's life (in-memory set); restart reloads the set.
2. Pairs come only from lines that became events (malformed hits do not count).
3. A pattern dropped from the selection keeps its rows (history); no delete.
4. `oldest_ms` uses the same target string the reader uses for that pattern (exclusions included).
5. Docs: ARCHITECTURE §5 (`MinuteBatch.sources`), §11 (table), status fields (`es_oldest`).

## Tests (write first, see them fail)
- `test_batch_sources_pairs` (reader): two services under one pattern → both pairs, once each.
- `test_index_services_recorded_once` (owner): the same pair in two flushes → one row; a failed flush keeps
  it pending and the next flush writes it once.
- `test_index_services_loaded_on_start` (owner): existing rows are not re-inserted (count queries or a spy).
- `test_oldest_ms_query_shape` (es_source): size 0, min aggregation, target passed through; None when the
  aggregation value is null.
- `test_status_es_oldest` (engine): status has `es_oldest` per pattern; refreshed only after 600 s; a raising
  `oldest_ms` keeps old values and logs a warning. Extend `FakeSource` with `oldest_ms`.

## Done when
- [ ] new tests fail before the code and pass after
- [ ] `.venv/bin/pytest -q` passes, output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
