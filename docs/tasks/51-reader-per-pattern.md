# Task 51: Live reader reads one cursor per pattern

Implementer: Sonnet 5.5 — reader/engine exactly-once path, needs judgment.
Reviewer: a fresh Sonnet 5.5 subagent
Branch: task/51-reader-per-pattern (worktree ../logai2-task-51, created by the tech lead)
PR file: .team/prs/task-51-reader-per-pattern.md (from docs/templates/pr.md; .team/ is in the main checkout
~/Documents/logai2, write it there)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below
is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Production indices are one per service per UTC day (`udcntt-<service>-YYYY.MM.dd`, a new one at 07:00 local).
Today the reader resolves patterns every 60 s and reads each concrete index with its own cursor. Two bugs follow:
(1) a new daily index can be picked up after its first minute already closed (minutes close 30 s after wall
time), so its first docs are dropped as late; (2) one search per concrete index per poll grows with services x
retention days (measured 0.3 s per poll for 300 indices vs 51 ms for one pattern search), and every index is
read in parallel on catch-up, holding memory (BUGS B5). Reading one cursor per **pattern** (one search over
all its indices, sorted by timestamp) fixes both.

## Read first
- Spec: docs/specs/2026-10-26-retrain.md §8 item 1. Plan: docs/plans/2026-10-26-retrain.md task 51.
- ARCHITECTURE.md §4.2, §4.3 (index selection, floors, cursors), §14.
- Code: `logai2/reader/reader.py` (whole), `logai2/reader/es_source.py` (`_SORT`, `_FILTER_PATH`, `_search`,
  `page`, `resolve`), `logai2/engine.py` (`start`, `_wait_for_indices`, `_select_indices`, `_refresh`,
  `_caught_up`, `_flush`, `_status`), `logai2/owner/owner.py` (`SQL["es_cursors"]`, the two es_cursors lines
  in `flush`), `logai2/web/pipeline_view.py` (`stage_states`), `logai2/web/static/js/views/sources.js`
  (selected patterns table), `logai2/web/static/js/views/pipeline.js` (~L63), `tests/fakes.py`,
  `tests/test_reader.py`, `tests/test_engine.py`, `tests/test_es_source.py`.

## Files
- Modify: `logai2/reader/reader.py`, `logai2/reader/es_source.py`, `logai2/engine.py`,
  `logai2/web/pipeline_view.py` (only if a key or text must change), `logai2/web/static/js/views/sources.js`,
  `logai2/web/static/js/views/pipeline.js` (only if needed), `tests/fakes.py`, `tests/test_reader.py`,
  `tests/test_engine.py`, `tests/test_es_source.py`, `tests/test_web_pipeline_view.py` (only if needed),
  `ARCHITECTURE.md`, `docs/WEB_UI.md`, `docs/BUGS.md`.
Touching any other file needs a QUESTION first. No migration.

## Interfaces
- `EsSource.page(target: str, from_ms: int, upper_ms: int) -> list[dict]`: `target` is a search expression
  (a pattern, optionally followed by `,-<pattern>` exclusions). Same sorting, stuck-millisecond and
  `ignore_unavailable` behaviour as today; also pass `allow_no_indices=True`. `_FILTER_PATH` gains
  `hits.hits._index`.
- `Reader.set_sources(floors: dict[str, int], saved: dict[str, int], targets: dict[str, str] | None = None)`
  replaces `set_indices`: keys are **patterns**; `targets[pattern]` is the search expression (default the
  pattern). Positions, `_caught_up`, `_errors`, `_seen` keyed by pattern. `_seen[pattern]` keys are
  `(_index, _id)`. `floors` property keyed by pattern.
- `Reader.poll()` batch: `positions` keyed by pattern, same fields. `Reader.cursors(boundary_ms)` returns
  `{pattern: (max(boundary_ms, floor), floor)}` (stored in `es_cursors.index`, column unchanged).
- Engine status `indices`: `{pattern: {pos_ms, caught_up, failing, floor}}`. `resolution` unchanged
  (`{pattern: [index, ...]}`, still refreshed every `index_refresh_seconds`, for Data sources only).
- Later tasks rely on: reader state keyed by pattern; each hit's `_index` available inside `poll()`.

## Requirements
1. One `page()` call per selected pattern per poll, never per concrete index. A new index matching a pattern
   is read from its first doc without any refresh.
2. Overlap (Ruling R1): selection entries keep their saved order. Pattern number k searches
   `"<P_k>,-<P_1>,...,-<P_{k-1}>"` (only the earlier patterns), so an index matched by two patterns is read
   once, by the earlier one. Duplicate patterns: keep the first.
3. Floor per pattern (unchanged rule): `bootstrap.since` if the entry's `added_at <= bootstrap.until`, else
   `added_at` (ms).
4. Upgrade (Ruling R2): `saved` comes from `es_cursors`. A pattern without a saved row starts at
   `max(floor, min(since_ms over all saved rows))` when any saved row exists, else at its floor. (Old rows are
   keyed by concrete index and all hold the minute boundary; they get deleted by the next flush, as rows not in
   `cursors()` are today.) Engine `start()` computes `start_ms` from these starting positions.
5. Dedup by `(_index, _id)`; `forget_before` unchanged in meaning.
6. A pattern that matches no index: its page is empty, so it is caught up; the engine adds the status warning
   `no index matches <pattern>` (it reads `resolution`). Update BUGS B8 accordingly.
7. Watermark, failing (`FAILING_AFTER`), stall guard, malformed counting: same semantics, per pattern.
8. Data sources page (`sources.js`): the "Reading" cell shows the pattern's own progress (`at <time>`,
   caught up, failing) once, then its resolved indices as a plain list. Pipeline page keeps working with
   pattern keys (labels say "pattern" where they said "index").
9. Docs: ARCHITECTURE §4.2/§4.3 (one cursor per pattern, overlap rule, upgrade rule), §14 (remove/adjust the
   per-index limits), WEB_UI Pipeline + Data sources, BUGS (B5 → Fixed, B8 updated).

## Tests (write first, see them fail)
- `tests/fakes.py`: `FakeSource.page(target, ...)` resolves `target` with fnmatch over its index names,
  honouring `-<pattern>` exclusions, merges hits sorted by `sort`, and sets `_index` on each hit; it records
  each call in `self.calls`. Keep `fail(target)`.
- `test_new_daily_index_first_minute_counted`: a pattern `app-*` with index `app-1`; poll; then add index
  `app-2` with a doc in the next minute (no `set_sources` call); poll → the doc is counted, not late.
- `test_one_search_per_pattern_per_poll`: 3 indices under one pattern → exactly one `page()` call per poll.
- `test_dedup_same_id_two_indices`: same `_id` in `app-1` and `app-2` at the same ms → counted twice.
- `test_overlapping_patterns_read_once`: patterns `app-*` then `app-pay-*`, index `app-pay-1` with 3 docs →
  3 events total; the second pattern's target is `"app-pay-*,-app-*"`.
- `test_upgrade_from_index_cursors` (engine): `es_cursors` holds rows for concrete indices at boundary B;
  start with one pattern added before bootstrap → reader position = B (not `bootstrap.since`).
- `test_empty_pattern_caught_up_with_warning` (engine): pattern with no index → caught up; status warnings
  contain `no index matches <pattern>`.
- Port every existing test in `tests/test_reader.py` and the affected ones in `tests/test_engine.py` to the
  pattern API; their assertions keep their meaning.
- `tests/test_es_source.py`: `page()` passes the target and `allow_no_indices=True`; `_FILTER_PATH` has `_index`.

## Done when
- [ ] new tests fail before the code and pass after
- [ ] `.venv/bin/pytest -q` passes, output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
