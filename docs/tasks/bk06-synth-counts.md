# Task bk06: Synthetic world, `counts` mode

Implementer: Sonnet 5.5 — derived keys must equal what the engine writes; every laptop result is computed on this data.
Reviewer: Opus 5.5 (fresh).
Branch: task/bk06-synth-counts (worktree ../logai2-task-bk06, created by the tech lead)
PR file: .team/prs/task-bk06-synth-counts.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
The laptop has two days of one service. Test B needs 14–28 days with a weekly shape, weekends, a holiday, trend, new
templates and known incidents. This task builds a synthetic world from the laptop's own templates and hourly shapes and
writes it, exactly as the engine would have, into its own Postgres schema, so the lab can run on it.

## Read first
- Spec: `docs/specs/2026-10-27-bakeoff-laptop.md` §4.1–4.2 (what the recorder stores), §7 (all), §10 (`lab_incidents`).
- Plan: `docs/plans/2026-10-27-bakeoff-laptop.md` "Task 6" (scenario YAML example).
- Code: `logai2/owner/counts.py` (`OpenMinutes.add`/`close_before`: how `*total`, `*error`, `*warn`, `*info` are
  derived; `level_class` in `logai2/links.py`), `logai2/owner/owner.py` (`hourly_counts`, `service_hours` rows),
  `logai2/jobs/commit.py` (`MARK_DAYS`: `day_status`), `logai2/db.py` (`migrate`, `ensure_partitions`),
  `logai2/migrations/*.sql` (columns of `templates`, `template_doc_links`, `documents`, `lab_minute_counts`,
  `lab_incidents`), `scripts/stress_pipeline.py` (`setup`: how a throwaway schema is migrated and seeded, including the
  `documents` row a link needs), `scripts/lab/load.py` (what the lab reads back), `tests/conftest.py`.

## Files
- Create: `scripts/lab/world.py`, `scripts/lab/synth.py`, `lab/scenarios/default.yaml`, `lab/scenarios/tiny.yaml`,
  `tests/lab/test_lab_synth.py`
- Modify: `.gitignore` (add `lab/world.json`), `README.md` (a short "Synthetic world" section with single-line commands)
Touching any other file needs a QUESTION first. Never write to `public` tables.

## Interfaces
- Produces:
  - `python -m scripts.lab.world [--dsn DSN] [--schema public] [--out lab/world.json]` → world file (below).
  - `python -m scripts.lab.synth --mode counts --scenario lab/scenarios/default.yaml [--world lab/world.json]
    [--schema lab_s_default] [--dsn DSN] [--end YYYY-MM-DD]` → the scenario schema (dropped and recreated).
  - `build_world(conn) -> dict` and `generate(world: dict, scenario: dict, end: date) -> Generated` (pure, no DB) and
    `write(conn, schema, gen: Generated) -> None`, so tests run without subprocesses. `Generated` holds per service:
    templates, links, per (key, day, hour) 60-int arrays, incidents.
  - `--mode engine` is **not** in this task (bk12): reject it with a clear message.

## Requirements
1. **World file** (`world.py`, from the source schema, read-only): per service, per template: `template_id`, `text`,
   `level`, `doc_id` (alerting sources only), per local hour 0–23: `mean` per minute = Σsum / Σn_minutes and `var` =
   Σsumsq / Σn_minutes − mean² (from `hourly_counts` + `service_hours`, all days available). Hours with no observed
   minute are **mirrored**: hour h takes hour 23 − h, flagged `mirrored: true` (the replay covers 00:00–16:07). If both
   are missing, the template's mean over observed hours. A template with mean 0 everywhere is dropped.
2. **Scenario YAML** (exactly these keys; defaults in `default.yaml`):
   `seed`, `days` (ending `--end`, default yesterday), `services` {`count`, `scale` [per service], `subset` (share of
   world templates each service keeps, seeded), `phase_minutes` [per service, shifts the daily curve]},
   `day_factors` {`weekday`, `jitter`, `sat`, `sun`}, `holiday` {`start_day` (index into the days), `length`,
   `factor`}, `trend` {`per_week`}, `dispersion` (`world` = use the world's var, `poisson`), `new_templates`
   {`per_service_day`, `error_share`, `rate`}, `gaps` (list of {`day`, `hour_from`, `hour_to`} with no observed minutes),
   `incidents` (list of {`type`: spike|drop|ramp|level_shift|mix_swap, `size`, `minutes`, `every_days`, optional `key`}).
   `tiny.yaml`: 1 service, 5 templates, 3 days, one spike.
3. **Rate model** per service s, template t, minute m (local day d, hour h, slot):
   λ = mean_t[h'] × scale_s × day_factor(d) × holiday(d) × (1 + per_week × weeks_since_start) × incident(t, m),
   with h' the hour after applying `phase_minutes`; `day_factor` = weekday ×(1 ± jitter, seeded per day) or sat / sun;
   holiday days use `factor`. Count ~ Poisson(λ) for `poisson`; Negative binomial with the world's var when var > mean
   (gamma–Poisson: `rng.gammavariate(shape, λ/shape)` then Poisson, shape = mean² / (var − mean) scaled with λ); one
   `random.Random(seed)` stream, so the same seed gives the same world.
4. **Incidents** (written to `lab_incidents`, source `synthetic`, `data_schema` = the scenario schema): placed every
   `every_days` at a seeded minute, never overlapping another incident on the same service; target = a seeded
   template (M3-like busy one for spike/ramp/level_shift, any for mix_swap) unless `key` is given; `drop` on `*total`
   multiplies every template of the service by `size`; `mix_swap` swaps the rates of two templates for `minutes`;
   spike ×size flat; ramp linear 1 → size; level_shift ×size flat. Row: service, key (the template id, `*total` for
   drop, NULL for mix_swap with both ids in `meta`), type, size, start_minute, end_minute.
5. **New templates**: per service per day, `per_service_day` new templates born at a seeded minute, id
   `<service>:N<day index><n>`, text = a world template's text with one word changed, level ERROR with probability
   `error_share` else INFO, rate `rate` per minute flat × the day factors; `first_seen` = birth minute.
6. **Derived keys, exactly as `OpenMinutes`**: per minute `*total` = Σ templates; `*error` / `*warn` / `*info` by
   `level_class(level)`; `*minutes` = 1 on observed minutes (0 inside `gaps`, where every count is 0 and nothing is
   written). Rows: `lab_minute_counts` (all-zero rows skipped, like the recorder), `hourly_counts` (sum, sumsq of the
   observed minutes; only keys with sum > 0), `service_hours` (n_minutes = observed minutes; hours with 0 skipped),
   `day_status` (committed for every day but the last, `daytype` as `MARK_DAYS`), `services`, `templates`
   (first_seen = window start − 30 days for world templates, birth for new ones; last_seen = window end),
   `documents` + `template_doc_links` (source `manual`) for linked templates.
7. **Schema**: `write` drops `lab_s_<name>` if it exists, creates it, runs `migrate` with `search_path` = that schema,
   `public` (pgvector), `ensure_partitions` for the days, inserts with COPY. It never writes outside the schema (test).
8. **Speed**: `default.yaml` (3 services × the laptop's ~180 templates × 28 days) finishes in < 15 min on the laptop;
   print elapsed time and row counts at the end.

## Tests (write first, see them fail)
In `tests/lab/test_lab_synth.py` (use a small hand-made world dict, not the laptop DB):
1. Same seed → identical `Generated` (hash of all arrays and incidents); another seed differs.
2. Invariants on every minute: `*total` = Σ templates; `*error`+`*warn`+`*info` = `*total`; `*minutes` 1 on observed,
   0 in gaps; for every key-hour `sum(counts)` = `hourly_counts.sum` and Σc² = `sumsq` (after `write` on the `conn`
   fixture's schema mechanics — create a scenario schema and drop it in teardown).
3. Day factors: mean daily `*total` on Sat / weekday ≈ `sat` within 5 % on a 28-day Poisson world; holiday days ≈ `factor`.
4. Incidents raise (spike) or lower (drop) the counts inside their windows by the expected factor (within noise) and
   are written to `lab_incidents`; no two overlap on a service.
5. New templates appear at their birth minute, never before; `first_seen` matches.
6. `write` touches only the scenario schema: row counts of `public` tables unchanged.
7. `load_service` (bk03) on the written schema returns arrays equal to `Generated`'s for a few keys.
8. `world.py` on a hand-made source schema: mean/var per hour, mirroring of missing hours, alerting-source links only.
9. `tiny.yaml` end to end (world dict → generate → write) in < 10 s.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes, output clean (report the count)
- [ ] `default.yaml` run once against a world built from the laptop DB: report time and row counts in the PR file
      (`python -m scripts.lab.world --dsn postgresql://logai:logai@127.0.0.1:5433/logai` then `python -m scripts.lab.synth ... --dsn <same>`; the config default DSN is the container hostname, so always pass `--dsn` from the host)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
