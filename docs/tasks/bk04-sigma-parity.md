# Task bk04: σ method, episode tracker, parity with the live engine

Implementer: Sonnet 5.5 — must reproduce live detection exactly; the trust gate for every later comparison.
Reviewer: Opus 5.5 (fresh) + an independent Opus adversary (parity edges) + tech-lead review.
Branch: task/bk04-sigma-parity (worktree ../logai2-task-bk04, created by the tech lead)
PR file: .team/prs/task-bk04-sigma-parity.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Method #1 is today's σ. Every other method is ranked relative to it, and every lab number is trusted only if the lab's
σ produces the same alerts as the engine on the same minutes. This task adds the σ method, the episode tracker every
method's flags go through, and the parity test that proves the lab equals live.

## Read first
- Spec: `docs/specs/2026-10-27-bakeoff-laptop.md` §6 (all; §6.2 updated by the D4 ruling, §6.4 is this task).
- Code (live): `logai2/jobs/commit.py` (`MARK_DAYS`, `REBUILD`, `commit`), `logai2/owner/baseline.py` (whole file),
  `logai2/owner/detect.py` (`step`, `Detector.run`: doc keys, levels, `VIEW_ONLY_KEYS`, `historic`),
  `logai2/engine.py` (`_commit_due`, `_next_commit_at`, `_commit`, when the owner reloads the baseline),
  `logai2/timeutil.py` (`daytype`).
- Code (lab, merged in bk03): `scripts/lab/data.py` (`ServiceData`, `level`, `slot_of`, `minute_at`, `UNOBSERVED`),
  `scripts/lab/methods/__init__.py` (`Spec`, `DayOut`, `Method`, `apply_d4`, `floor_for`, `METHODS`, `GROUPS`),
  `scripts/lab/run.py` (the walk-forward loop, `RunCtx`, the sink contract), `scripts/lab/kinds.py`.
- Harness: `scripts/stress_pipeline.py` (`build_world`, `GenSource`, `Clock`, `pretrain`, `setup`, `Run`, `drive`,
  `rebuild`, throwaway schemas) and `tests/test_stress_pipeline.py` (how tests drive it).

## Files
- Create: `scripts/lab/methods/sigma.py`, `scripts/lab/episodes.py`, `tests/lab/test_lab_sigma.py`,
  `tests/lab/test_lab_episodes.py`, `tests/lab/test_lab_parity.py`
- Modify: `scripts/lab/methods/__init__.py` (register `sigma` in `METHODS` and `GROUPS["core"]` only)
Touching any other file needs a QUESTION first (in particular: never change `logai2/`).

## Interfaces
- Consumes: the bk03 interfaces above; live `Baseline(stats, days)`, `Stat`, `DAY_CAP`, `judge`, `Baseline.lookup`,
  `Baseline.lookup_doc`, `detect.step`, `AlertRow`, `AlertConfig`.
- Produces:
  - `scripts/lab/methods/sigma.py`: class `Sigma` with `spec = Spec(name="sigma", number="#1", label="mean ± kσ (today)",
    kinds=frozenset({"M1","M2","M3","M4","M5","M6"}), knobs=(2.0, 2.5, 3.0, 3.5, 4.0, 5.0), default_knob=3.0,
    band=True, heavy=False, cost=0, min_hour_days=1)`; `fit(sd, day, cfg) -> SigmaModel | None`; `day(...)`.
  - `rebuild_stats(sd, day) -> Baseline` (pure; also used by NB methods in bk08: the profile "as committed for `day`").
  - `scripts/lab/episodes.py`: `Episode(start, end, peak_score, alerted)` (minutes; `end` exclusive or the window end),
    class `Tracker(alert_cfg)` with `step(minute, high, score) -> Episode | None` (returns an episode when it closes)
    and `close(end_minute) -> Episode | None`; `episodes(flags, alert_cfg) -> list[Episode]` over an iterable of
    `(minute, high, score)`.

## Requirements
1. `rebuild_stats(sd, D)` reproduces what `commit()` would put in `baseline` when it runs on owner day D:
   - committed days as of D = days `< D` that have `service_hours` rows (the commit marks every such day);
   - per (service, daytype): the last 5 weekdays / 4 weekend days of those, by day descending;
   - hours: `n_minutes > 0`, minus Review `exclude` hours (`sd.excluded`; known limit: today's exclusions apply
     backwards — a comment says so);
   - keys: every key with an `hourly_counts` row in a picked hour; the `born` rule for `*warn`/`*info` (earlier days
     than their first day as of D are not counted);
   - mean = Σsum / Σn_minutes, std = sqrt(max(Σsumsq / Σn_minutes − mean², 0)), over **all** picked hours of the
     service for that (daytype, hour) — a key absent in a picked hour counts 0 (exactly the SQL's LEFT JOIN);
   - `days` = `{(service, daytype): min(number of committed days of that daytype, DAY_CAP[daytype])}` as `Baseline.load`.
   It returns a real `logai2.owner.baseline.Baseline`.
2. `fit(sd, D, cfg)` = `rebuild_stats(sd, D)`; `None` when no committed day exists before D.
3. `day(model, prev_model, sd, key, D, knobs, cfg)`:
   - for each slot, the minute uses `model` when the local time is ≥ `cfg.baseline.commit_time`, else `prev_model`
     (or `model` when `prev_model` is None) — the live commit for D has not run before 02:00;
   - per knob k: a config copy with `sigma_multiplier = k` and `young_sigma_multiplier = k × (live young / live k)`
     (4/3 with the defaults); `dataclasses.replace`, never mutate `cfg`;
   - `lookup` (or `lookup_doc` with `sd.docs[key]` for `doc:` keys) + `judge(count, key, level, lk, cfg_k)` with
     `level = sd.level(key)`; `high_flag` = `judge(...).high` (σ uses live `judge` directly; it does not call `apply_d4`);
   - `low`/`high` = the verdict's `band_low`/`band_high`, `expected` = `lk.this.mean` (doc keys: the summed mean),
     `score` = verdict score; NaN in expected/low/high where there is no band; unobserved minutes: NaN band, score 0,
     flag 0;
   - `*warn`, `*info` (view-only in live) are never flagged.
4. `Tracker`: steps `detect.step` from a fresh `AlertRow`; unobserved minutes are not passed in (callers skip them);
   an episode is a run that reaches ALERTING (D5): start = the first high minute of the run that left NORMAL,
   end = the minute the state returns to NORMAL; WARMING → NORMAL without ALERTING returns nothing; `peak_score` =
   max score in the episode, `alerted` = minutes in ALERTING or COOLING.
5. Register `sigma` in `METHODS` and `GROUPS["core"]`.

## Tests (write first, see them fail)
- `test_lab_sigma.py`:
  - `rebuild_stats` equals the SQL `REBUILD`: build a DB on the `conn` fixture with ≥ 8 days of `hourly_counts` /
    `service_hours` (two services, weekdays and weekends, one key absent in some hours, one excluded hour, `*warn`
    born mid-way), run `commit(conn, cfg, D)`, read `baseline`, compare every (service, key, daytype, hour) mean and
    std to 1e-9 and the `days` map, for at least three different D;
  - weekend with < 2 committed weekend days borrows the weekday profile (`upper_only`), young vs full k, knob scaling
    (k = 2 gives a narrower band than k = 3), doc-key band = sum of linked templates, commit-time switch (a minute at
    01:59 uses `prev_model`, 02:00 uses `model`).
- `test_lab_episodes.py`: the D5 cases (2 highs → WARMING then NORMAL: no episode; 5 highs then 3 lows: one episode
  with the right start/end/alerted; COOLING interrupted by a high; episode still open at `close`).
- `test_lab_parity.py` (mark `@pytest.mark.slow` only if the repo already uses such a marker; otherwise keep it
  under ~5 min): drive the real Engine with the stress harness in a throwaway schema, recorder on (`cfg.lab.record`),
  1 service × ~20 templates + one doc link, a low event rate, at least **3 local days** that include a weekend day,
  the engine's own 02:00 commits, one planted burst per day on a template and one on the doc's template.
  Then load the schema with `load_service`, run `Sigma` at k = 3 through `Tracker` over days 2…N, and compare the set
  of transitions `(key, minute, to_state)` with `alert_changes` (convert `at` to the minute it closed: `at/60 − 1`).
  **Exact match required**, except transitions within 10 minutes after each engine commit instant (from
  `retrain_runs` rows of kind `rebuild` or the `last_commit` setting history you can capture); count them, assert 0
  mismatches outside those windows and ≤ 10 inside each. If exact parity fails, do not loosen the test: find the
  cause and report it (QUESTION) — that is the point of this task.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes, output clean (report the count and the parity test's runtime)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in, including how many transitions were compared and how many fell in commit windows
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
