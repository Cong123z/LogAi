# Task bk01: Migration 010, LabConfig, live recorder

Implementer: Sonnet 5.5 — exactly-once flush path; one mistake loses or doubles data.
Reviewer: Opus 5.5 (fresh) + an independent Opus adversary (crash and flush-failure timing).
Branch: task/bk01-recorder (worktree ../logai2-task-bk01, created by the tech lead)
PR file: .team/prs/task-bk01-recorder.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
The baseline bake-off compares alerting methods on **per-minute** counts. Postgres keeps only hourly sums today.
This task adds the lab tables and a recorder in the engine's owner that stores each key's 60 per-minute counts per
local hour, written exactly once, in the same flush transaction as `hourly_counts`. Live alerting must not change.

## Read first
- Spec: `docs/specs/2026-10-27-bakeoff-laptop.md` §1 (invariants), §4 (recorder, all of it), §10 (storage).
- Plan: `docs/plans/2026-10-27-bakeoff-laptop.md` "Task 1" (schema SQL is there verbatim).
- Code: `logai2/owner/owner.py` (whole file: `SQL`, `copy_upsert`, `Owner.__init__`, `_clear`, `handle`, `flush`),
  `logai2/owner/counts.py` (`ClosedMinute`, `OpenMinutes.close_before`), `logai2/db.py` (`PARTITIONED`,
  `ensure_partitions`, `drop_partitions_before`, `migrate`), `logai2/jobs/commit.py` (housekeeping),
  `logai2/config.py` (dataclasses, `load_config` env handling), `logai2/timeutil.py`,
  `logai2/migrations/007_template_modules.sql` and `008_retrain_runs.sql` (style),
  `tests/test_owner.py` (helpers `rows`, `make`, `tick`, `run_minutes`, `lines`; crash tests), `tests/test_db.py`,
  `tests/test_commit.py`, `tests/test_config.py`.

## Files
- Create: `logai2/migrations/010_lab.sql`, `tests/test_recorder.py`
- Modify: `logai2/db.py`, `logai2/config.py`, `config.yaml`, `logai2/owner/owner.py`, `logai2/jobs/commit.py`,
  `docker-compose.yml`, `tests/test_db.py`, `tests/test_commit.py`, `tests/test_config.py`, `ARCHITECTURE.md`, `README.md`
Touching any other file needs a QUESTION first. Do not touch `logai2/jobs/reread.py` or `Owner._flush_lane`
(the re-read lane is task bk02).

## Interfaces
- Consumes: `ClosedMinute(minute, day, hour, keys, modules, levels)`; `OpenMinutes.close_before(...)`;
  `copy_upsert(cur, table, rows)` + `SQL[table]`; `ensure_partitions(conn, first, last)`;
  `drop_partitions_before(conn, table, day)`; `timeutil.local(minute)`, `timeutil.day_hour(minute)`.
- Produces (later tasks rely on these exact names):
  - tables `lab_minute_counts`, `lab_runs`, `lab_results`, `lab_episodes`, `lab_bands`, `lab_sweeps`, `lab_choices`,
    `lab_incidents` exactly as in the plan's Task 1 §1.1 SQL;
  - `"lab_minute_counts"` in `db.PARTITIONED`;
  - `LabConfig(record: bool = False, days: int = 28, holidays: list = [])` as `AppConfig.lab`;
  - env `LOGAI2_LAB_RECORD` (true/false, parsed like `LOGAI2_ES_VERIFY_CERTS`);
  - key `*minutes` in `lab_minute_counts` (1 per observed minute of a service).

## Requirements
1. Migration `010_lab.sql` creates the eight tables with the plan's SQL (column names, types, keys, CHECKs,
   `ON DELETE CASCADE`). It creates no partitions. `schema_migrations` reaches 10.
2. `db.PARTITIONED` includes `"lab_minute_counts"`.
3. `LabConfig` + `AppConfig.lab`; YAML `lab:` section in `config.yaml` with `record: false`, `days: 28` and
   `holidays:` a plain list of ISO dates, exactly these (core days only; a comment says that official bridge days
   are added by the user each year): for each year 2025, 2026, 2027: `YYYY-01-01`, `YYYY-04-30`, `YYYY-05-01`,
   `YYYY-09-02`; Tết (lunar New Year's Eve + days 1–3): `2025-01-28..2025-01-31`, `2026-02-16..2026-02-19`,
   `2027-02-05..2027-02-08` (each day listed); Hùng Kings: `2025-04-07`, `2026-04-26`, `2027-04-16`.
4. `docker-compose.yml`: engine env `LOGAI2_LAB_RECORD: ${LOGAI2_LAB_RECORD:-true}`.
5. Recorder in `Owner` (only when `cfg.lab.record`):
   - pending dict reset in `_clear()`: `(service, key, day, hour) -> list[int] of 60` (minutes closed since the last
     successful flush);
   - in `handle()`, for every closed minute (before the `detection_enabled` check, so recording does not depend on
     detection), with `slot = local(cm.minute).minute`:
     - for each `(service, key), n` in `cm.keys` with `n > 0`: add `n` at `slot`;
     - for each service that has a `*total` key in `cm.keys`: set `*minutes` at `slot` to 1;
     - skip rows whose `day < today - timedelta(days=cfg.lab.days)` (`today` as `handle` already computes it);
   - in `flush()`, inside the same transaction, after `hourly_counts`: `copy_upsert(cur, "lab_minute_counts", rows)`
     with `SQL["lab_minute_counts"]` = the element-wise add upsert from spec §4.3 (with `WITH ORDINALITY ... ORDER BY i`);
   - the pending dict is cleared only by `_clear()` after a successful commit (a failed flush keeps it);
   - nothing is recorded for `doc:` keys (they do not exist in `cm.keys`); `:unparsed` keys are recorded.
6. Retention: `commit()` housekeeping drops `lab_minute_counts` partitions older than `today - cfg.lab.days`.
7. With `lab.record` false, the owner does no recorder work and writes no lab rows.
8. Live output is identical with the flag on or off (see test 8).
9. Docs: ARCHITECTURE (configuration table, §7.3 flush transaction lists the recorder write, §11 schema lists the
   lab tables with one line each, migrations list 010); README (the `lab.record` flag, size ≈ 0.3–0.6 GB / 28 days
   at 3k keys, ≈ 30 MB on the laptop; upgrade note: migration 010, engine first).

## Tests (write first, see them fail)
In `tests/test_recorder.py`, reuse the helpers of `tests/test_owner.py` (import them; do not copy) and set
`cfg.lab.record = True` unless stated:
1. `test_flag_off_writes_nothing`: flag off, close one hour → `lab_minute_counts` empty.
2. `test_hour_across_three_flushes`: feed minutes 0–19, 20–39, 40–59 of one hour in three separate flushes → one row
   per key equal to the per-minute counts fed; for every key `sum(counts) == hourly_counts.sum`; `*minutes` row sums
   to `service_hours.n_minutes`.
3. `test_crash_mid_hour_exact`: flush minutes 0–29; feed 30–34 without flushing; build a new Reader/Owner with
   `make()` (as after a restart) and feed 30–59 again → rows equal a run that never crashed (no doubled, no missing minute).
4. `test_failed_flush_keeps_pending`: make one flush raise inside its transaction (as `test_flush_failure_keeps_pending`
   does) → nothing written; the next flush writes the minutes once.
5. `test_silent_minutes_and_minutes_key`: a template seen at minute 3 only → its row has the count at slot 3 and zeros
   elsewhere; `*minutes` has 1 at every closed slot.
6. `test_old_day_not_written`: a closed minute older than `today - lab.days` is not written and nothing raises.
7. `tests/test_commit.py::test_commit_drops_old_lab_partitions`: partitions older than `lab.days` dropped, newer kept.
8. `test_live_tables_unchanged`: the same feed with flag on and flag off (two fresh DBs / truncations) → identical
   `hourly_counts`, `service_hours`, `alert_changes`, `alert_state`.
9. `tests/test_db.py`: migration 010 applies on an empty DB and on one already at 009; the eight tables exist;
   `lab_minute_counts` is partitioned and `ensure_partitions` creates its daily partition.
10. `tests/test_config.py`: `LOGAI2_LAB_RECORD=true` sets `cfg.lab.record`; YAML `lab.days` is read.
11. `test_int_array_copy`: `copy_upsert` of `lab_minute_counts` rows round-trips 60 ints (covers psycopg COPY of `int[]`).

## Done when
- [ ] new tests fail before the code and pass after (record RED and GREEN output in the PR file)
- [ ] `.venv/bin/pytest -q` passes, output clean (report the count)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
