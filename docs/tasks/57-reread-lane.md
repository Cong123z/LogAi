# Task 57: Re-read lane — replace chosen services' hours from Elasticsearch, then rebuild

Implementer: Sonnet 5.5 — the exactly-once write path; the riskiest task of the build.
Reviewer: a fresh Sonnet 5.5 subagent, then an Opus adversarial review (tech lead).
Branch: task/57-reread-lane (worktree ../logai2-task-57, created by the tech lead)
PR file: .team/prs/task-57-reread-lane.md (from docs/templates/pr.md; .team/ is in the main checkout
~/Documents/logai2, write it there)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below is unclear, stop and reply
`QUESTION: ...` instead of guessing. Never kill processes you did not start (other agents share the test
Postgres). This is a large task: commit in steps (tests first, then each part), and ask early rather than late.

## Why
An operator picks services, days and an hour range on the Retrain page and starts a **Re-read**: the engine
reads those hours again from Elasticsearch, replaces their stored counts one whole hour at a time, then
rebuilds the baseline. It fills a new pattern's past days, hours the engine was down, and hours that lost late
docs. ES keeps 7 days. Live reading and detection never stop during a re-read.

## Read first
- Spec: docs/specs/2026-10-26-retrain.md §2, §3, §5, §7 (every risk row). Plan: task 57.
- Mockup: https://claude.ai/artifact/VdaHzwwbkVPkWYDMAofnsa boards 2 (running) and 4 (failed): the step lines
  are built from the `steps` details defined below.
- ARCHITECTURE.md §6-§8 (counts, owner flush, zero-fill), §9 (commit, runs).
- Code: `logai2/reader/reader.py` (`poll`, `_add`, dedup, `MinuteBatch`), `logai2/reader/es_source.py`
  (`page`, `_search`, `oldest_ms`, `hit_to_line`), `logai2/reader/normalize.py` (`normalize`),
  `logai2/reader/cache.py`, `logai2/owner/counts.py` (`OpenMinutes`, `close_before`), `logai2/owner/owner.py`
  (`handle`, `_see`, `flush`, `SQL`, pending-row lifecycle, `mod_cutoff`), `logai2/owner/miners.py`,
  `logai2/jobs/commit.py` (`commit(..., rebuild, on_step)`), `logai2/jobs/runs.py` (`start/step/finish`,
  `RunLog`), `logai2/engine.py` (`tick`, `_caught_up`, `_commit`, `_record`, `_run`, `es_oldest`, `_targets`,
  `index_services` use), `logai2/commands.py`, `logai2/db.py` (`ensure_partitions`), `logai2/timeutil.py`,
  `logai2/config.py`, `tests/fakes.py`, `tests/test_engine.py`, `tests/test_owner.py`.

## Files
- Create: `logai2/jobs/reread.py`, `tests/test_reread.py`
- Modify: `logai2/owner/owner.py`, `logai2/engine.py`, `logai2/reader/es_source.py` (count + timeout only),
  `logai2/commands.py` (KINDS), `logai2/config.py` (`retrain` section), `tests/fakes.py`, `tests/test_owner.py`,
  `tests/test_engine.py`, `tests/test_es_source.py`, `README.md` (config), `ARCHITECTURE.md`
  (§5 contracts, §9 re-read, §10 engine loop, §14 limits), `docs/BUGS.md` (only if you find a new limit)
Touching any other file needs a QUESTION first.

## Interfaces
Config: `RetrainConfig(tick_seconds: float = 1.0, es_timeout_seconds: int = 30, max_live_lag_seconds: int = 60,
default_rate: int = 25000)` as `cfg.retrain`.

`logai2/jobs/reread.py` (no DB access except where stated; pure parts are unit-tested alone):
- `Scope(services: list[str] | None, days: list[date], hour_from: int, hour_to: int)` — local dates,
  `0 <= hour_from < hour_to <= 24`. `Scope.from_json(d)` / `to_json()`.
- `plan(scope, patterns: dict[str, list[str]], es_oldest: dict[str, int | None], owner_hour_ms: int)
  -> Plan` where `patterns` = selected pattern → services it holds (from `index_services`, only selected
  patterns), `es_oldest` = pattern → oldest doc ms. `Plan` has:
  `windows: list[Window]` (time-ordered local hour windows `(start_ms, end_ms)`, each with the patterns to
  read and the services to replace in it), `services`, `patterns`, `hours_chosen` (services × hours),
  `hours_not_in_es`, `hours_live` (at/after the owner hour), `hours_to_read` (service-hours in windows).
  Rules: services None = every service of the selected patterns; a service's patterns = every selected
  pattern holding it; a service-hour is **not in ES** when its window start < max(oldest of its patterns) or
  any of its patterns has oldest None; a window is dropped when `end_ms > owner_hour_ms` (live hour and later).
- `class Lane`: built from a `Plan`, the run id, `pos_ms` (resume point: windows with `start_ms < pos_ms`
  are done), the `EsSource`, the owner's `Miners`, the reader's `KnownLines` cache, `first: dict[str, int]`
  (service → first minute known from the DB), cfg.
  - `step(budget_s: float, now) -> list[LaneHour]`: reads pages for the current window until the budget is
    used; returns the windows completed in this call. Per pattern per window it pages with
    `source.page(target, from_ms, end_ms - 1, timeout=cfg.retrain.es_timeout_seconds)` from the window start,
    advancing like the live reader (position = max sort ms; re-read docs at the position ms are dropped by
    `(_index, _id)`; keep only the ids at the current position ms — never one id set per hour).
    Each hit → `hit_to_line` → `normalize` → cache, else `miners.match` (same as live; a normalize or match
    failure counts the line as `<service>:unparsed` like the owner does). Lines of services not in the
    window's service set are dropped.
  - Counts go into the lane's **own** `OpenMinutes(window start minute)`; when a window is fully read, it closes
    the window's 60 minutes with `close_before(end_minute, first_map)` where `first_map[s] = max(window start
    minute, first[s] if known else the minute the lane first saw s)` for the window's services — a service
    never seen (no DB first, no doc) is not written.
  - `LaneHour`: `service, day, hour, n_minutes, sums {key: (sum, sumsq)}, module_hours {(template_id, module):
    n}, pairs {(template_id, module): (first_min, last_min)}, samples [(template_id, minute, line)] (ERROR+
    only, ≤ 20 per template), templates {template_id: {text, level, first, last}}`.
  - `progress()` → dict for the `read` step detail (below). `done` when all windows are completed.
  - `lane.note_es_gone(window)`: see requirement 6.
- `EsSource.count(target, from_ms, to_ms) -> int` (`_count` API with the same range filter,
  `ignore_unavailable`, `allow_no_indices`); `page(..., timeout: int | None = None)` passes `request_timeout`.

Owner (`logai2/owner/owner.py`):
- `Owner.add_lane(run_id: int, hours: list[LaneHour], run_update: dict)` — queues hours and the run's
  `pos_ms` / `steps` update; `flush` writes them in **the same transaction** as everything else:
  for each LaneHour, `had = EXISTS service_hours row`; `DELETE` that (service, day, hour) from `service_hours`,
  `hourly_counts`, `template_module_hours`; insert the lane's rows (`template_module_hours` only when
  `day >= module cutoff`, as live); upsert `template_modules` pairs; upsert `templates` (LEAST first_seen,
  GREATEST last_seen, level by severity) and `services`; `error_samples` only when not `had`;
  `UPDATE retrain_runs SET pos_ms, steps WHERE id`. Cleared only after the commit (a failed flush keeps them).
- `SQL["templates_seen"]` also sets `first_seen = LEAST(first_seen, %s)` (the lane's cache hits carry no text).
- Lane templates go through the owner's existing pending-template path (levels, `_templates`), not a new one.
  The lane must **not** change `Owner.services` (live zero-fill map) or the live `OpenMinutes`.

Engine:
- Commands `retrain_run` (payload `Scope.to_json()`), `retrain_cancel` (`{run_id}`), `retrain_estimate`
  (payload scope) added to `commands.KINDS` and `_run`.
- `retrain_run`: refuse with `{"error": "a retrain is running"}` if a lane is active; compute `oldest_ms` fresh
  for the needed patterns (do not trust `es_oldest` alone: it can be 600 s stale); `plan()`; if
  `hours_to_read == 0` → `{"error": "nothing to re-read", ...plan counts}` and no run row; else
  `ensure_partitions(first day, today)`, `runs.start("reread", "manual", scope)`, steps `scope` (finished,
  detail: services, patterns, hours_chosen, hours_not_in_es, hours_live, hours_to_read) and `partitions`
  (finished), then the lane starts; result `{"run_id": id}`. Docs target for progress = sum of `count()` over
  the windows (computed at start; if it fails, target None).
- Each `tick`, after the live poll/handle: if a lane is active and the engine is caught up and
  `lag_seconds <= cfg.retrain.max_live_lag_seconds` → `lane.step(cfg.retrain.tick_seconds)`; completed hours →
  `owner.add_lane(...)` with the run's updated `read` step. Live always first; the lane never runs during the
  first catch-up.
- `read` step detail (used by the page): `docs_read, docs_target, hours_done, hours_total, new_templates,
  rate (docs/s over the last 60 s), position_ms, live_lag_s, paused (times the lane waited for live), pct,
  eta_s`.
- End: when `lane.done` and its last hours are flushed → `commit(self.db, cfg, owner_day, rebuild=True,
  on_step=...)` recording `rebuild` (and `mark`, `housekeeping`) as steps of **this** run, reload the baseline,
  `runs.finish("succeeded", result={docs_read, hours_replaced, hours_not_in_es, new_templates, baseline_rows})`.
- Cancel: lane dropped at once (its partial window discarded); hours already flushed stay; no rebuild;
  `finish("cancelled")`.
- Failure: a window's `page()` failing 3 times in a row (across ticks) → `finish("failed", error,
  error_type, failed_step="read")`, read step marked failed; no rebuild; hours already flushed stay.
- Restart: at `start()`, a `retrain_runs` row with `kind='reread'` and `state='running'` resumes: re-plan from its
  scope (fresh `oldest_ms`), `pos_ms` from the row; a step note `resumed_at` in the read detail.
- A `RunLog` is attached while the lane is active; its lines are written with each run update.
- `retrain_estimate`: `plan()` + `count()` → `{services, patterns, hours_to_read, hours_not_in_es,
  hours_live, docs, eta_s (docs / last lane rate or cfg.retrain.default_rate), indices}`; no run row.
Later tasks rely on: commands `retrain_run/retrain_cancel/retrain_estimate` and their results, the `read` step
detail keys, `retrain_runs.kind='reread'` rows.

## Requirements (each has a test)
1. A re-read hour equals what live would have stored for it: same `service_hours.n_minutes`,
   `hourly_counts` sum/sumsq per key (`*total`, `*error`, `*warn`, `*info`, templates), `template_module_hours`.
2. Never writes an hour before the oldest ES doc of the service's patterns, nor the owner's current hour or later.
3. Whole-hour replace: old rows of a replaced hour are gone; nothing doubled; other services in the same hour
   untouched; an hour not replaced keeps its old rows.
4. Crash between flushes: restart resumes from `pos_ms`; final rows equal a run without a crash.
5. Live is never starved: the lane waits while live lags more than `max_live_lag_seconds` or is not caught up.
6. An index deleted by ES retention mid-run: when a pattern returns no hit for a window, re-check
   `oldest_ms`; if the window start is now before it, that window is skipped for that pattern's services
   (counted as not in ES, a WARNING logged so it lands in the run log), not written as zeros.
7. One re-read at a time; a nightly or manual Rebuild may still run during it (it is a separate run row).
8. Error samples are never duplicated for an hour that already had rows.

## Tests (write first, see them fail) — `tests/test_reread.py` unless noted
`test_plan_clamps_not_in_es_and_live_hours`, `test_plan_service_with_two_patterns_reads_both`,
`test_reread_equals_live_counts` (feed the same synthetic docs live into one schema and through the lane into
another; compare the three tables), `test_reread_never_writes_hours_not_in_es`,
`test_reread_stops_before_owner_hour`, `test_reread_replaces_whole_hour`, `test_reread_other_service_untouched`,
`test_reread_crash_resumes_exact` (flush fails mid-run, engine rebuilt from DB), `test_reread_cancel_keeps_done_hours_no_rebuild`,
`test_reread_es_error_fails_with_step`, `test_reread_index_deleted_midway_warns`, `test_lane_waits_while_live_lags`,
`test_second_run_rejected`, `test_samples_only_for_new_hours`, `test_lane_dedup_bounded` (ids kept only at the
current position ms), `test_estimate_counts_docs` and, in test_owner, `test_templates_seen_moves_first_seen_earlier`.
`tests/fakes.py`: `FakeSource.count`, `page(..., timeout=None)`, and a way to delete an index mid-test.

## Done when
- [ ] new tests fail before the code and pass after
- [ ] `.venv/bin/pytest -q` passes, output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in (with any interface you had to change and why)
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
