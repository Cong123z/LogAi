# Task bk05: Scoring sink (Test B metrics, episodes, drawn bands)

Implementer: Sonnet 5.5 — the ranking metric: one wrong formula changes every verdict.
Reviewer: Opus 5.5 (fresh).
Branch: task/bk05-scoring (worktree ../logai2-task-bk05, created by the tech lead)
PR file: .team/prs/task-bk05-scoring.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
The runner streams each method's per-minute outputs (band, score, high flag) to a sink. This task writes the sink that
turns them into the judging numbers (coverage, interval score, sharpness, MASE, alert rate, flap share) per kind ×
knob × part, plus per-day aggregates for the bootstrap (bk07), the episodes, and the drawn bands for the Evaluation page.
Tuning, the σ-relative ratio, bootstrap and verdicts are bk07.

## Read first
- Criteria: `docs/specs/2026-10-26-bakeoff-judging.md` (ground rules, criterion 1–2).
- Spec: `docs/specs/2026-10-27-bakeoff-laptop.md` §5.3 (kinds), §6.2–6.3 (D4, D5), §8 (tests), §9, §10.
- Code: `scripts/lab/run.py` (`run` docstring: the sink contract; `RunCtx`; `make_part`; `DefaultSink`),
  `scripts/lab/data.py`, `scripts/lab/kinds.py` (`is_new`, `has_new`), `scripts/lab/load.py`, `scripts/lab/store.py`,
  `scripts/lab/methods/__init__.py` (`DayOut`), `scripts/lab/episodes.py` (`Tracker`, `Episode`; merged in bk04),
  `scripts/lab/methods/sigma.py` (a real method to test with), `logai2/migrations/010_lab.sql`.

## Files
- Create: `scripts/lab/score.py`, `logai2/migrations/011_lab_knob.sql`, `tests/lab/test_lab_score.py`
- Modify: `scripts/lab/data.py` (`ServiceData.incidents`), `scripts/lab/load.py` (load incidents),
  `scripts/lab/store.py` (episode/band writers for the new columns), `scripts/lab/run.py` (default `collect` =
  the scoring sink; keep `DefaultSink` for tests or remove it if unused), `tests/lab/test_lab_load.py`,
  `tests/lab/test_lab_run.py`, `tests/test_db.py` (migration 011), `ARCHITECTURE.md` (§11 lab tables)
Touching any other file needs a QUESTION first.

## Interfaces
- Produces:
  - Migration 011 (lab tables only, they hold no data yet): `lab_episodes` gets `knob double precision NOT NULL` and
    PK `(run_id, method, knob, service, key, start_minute)`; `lab_bands` is recreated as one row per day:
    `(run_id, method, knob, service, key, day, step smallint, counts int[], expected real[], low real[], high real[],
    flags boolean[])`, PK `(run_id, method, knob, service, key, day)`, cascade from `lab_runs`; `step` 1 (1440
    values) or 5 (288 values).
  - `ServiceData.incidents: list[Incident]` (default empty), `Incident(key: str | None, start: int, end: int, source: str)`
    (minutes); `load_service` fills it from `lab_incidents` of the data schema for the service and window.
  - `scripts/lab/score.py`: `ALPHA = 0.005`; `class ScoreSink` (the sink); `score_collect(results_conn, run_id, service,
    method, spec, kinds, ctx) -> ScoreSink`; `metrics_from(acc) -> dict` (pure); helpers `winkler(y, low, high, alpha)`,
    `median_from_hist(hist)`. `run.run` uses `score_collect` by default.
  - `lab_results` row per (method, kind, scope = service), `knob` NULL, `status` ok / skipped, `metrics` JSON:
    ```
    {"n_keys": int, "no_band_share": float,
     "knobs": {"<knob>": {"tune": M, "test": M}},
     "days":  {"<knob>": {"<YYYY-MM-DD>": D}}}          # test part only, for the day-block bootstrap
    M = {"n_minutes", "coverage", "interval_score", "sharpness", "mase", "mase_naive": "7d"|"1d"|null,
         "alert_rate", "flap_share", "episodes", "key_days"}
    D = {"n", "covered", "winkler", "abs_err", "abs_naive", "episodes", "flaps", "key_days"}
    ```
    Numbers are null when undefined (no band, no minutes). bk07 recomputes ratios from `days`.

## Requirements
1. **Minute attribution** (per key and minute): unobserved → ignored entirely (not stepped, not scored). Kind = "M6"
   when `is_new(sd, key, minute)`, else `kinds[key]` unless that is "M6" (then the minute is not scored). Only kinds in
   `spec.kinds` are scored. Part = `ctx.part(key, day)`.
2. **Shared normal minutes**: observed, not in `sd.excluded` hours, not within ± 15 min of any `sd.incidents` window of
   the service (an incident with `key` NULL or `*total` covers every key of the service; otherwise only that key and
   the service's `*total`, its level-class key and any `doc:` key containing it). One set, the same for every method.
3. **Band metrics** (band methods; minutes with NaN band count in `no_band_share` and are skipped):
   - coverage = share of shared normal minutes with `low ≤ y ≤ high`;
   - Winkler at α = 0.005: `(high − low) + (2/α)·max(low − y, 0) + (2/α)·max(y − high, 0)`, divided by the key's
     mean count per observed minute over the whole window (from `ctx.sd`, computed once per key; mean < 1 → 1);
     `interval_score` = mean over minutes of the normalized value (minutes weigh equally; keys with more minutes
     weigh more — document it);
   - sharpness = median of `(high − low) / max(expected, 1e-9)` from a log-spaced histogram (e.g. 600 bins over
     1e-4 … 1e4, plus under/overflow): approximate median, documented;
   - MASE = Σ|expected − y| ÷ Σ|naive − y| over minutes where the naive value exists; naive = the same minute 7 days
     earlier if observed in the window for every scored minute of this part, else 1 day earlier; `mase_naive` says
     which (per part; if neither exists, null).
4. **Episodes**: one `Tracker` per (key, knob), stepped on observed minutes in ascending order across days (the
   runner adds days in order per key; assert it). An episode counts for the part and kind of its start minute and only
   when its start minute is a shared normal minute. Alert rate = episodes ÷ key_days × 100, with key_days = shared
   normal minutes ÷ 1440 (M1: per service-week = episodes ÷ (minutes ÷ 10080)). Flap = episode shorter than 5 min or
   starting < 30 min after the previous episode's end on the same key and knob; flap_share = flaps ÷ episodes.
   Open episodes are closed at the window end.
5. **Writes, all in `end()`, one transaction**: the `lab_results` rows; `lab_episodes` for every knob (all episodes,
   normal or not; `peak_score`, `alerted`); `lab_bands` for the **drawn keys** at every knob: per kind the 3 walked keys
   with the highest mean count, plus every key named by an incident of the service (max 5); per day, `step` 1 when the
   day has any high flag at that knob, else `step` 5 (5-minute means of expected/low/high, count sum ÷ 5 rounded,
   flag any). Memory: the sink keeps only accumulators, trackers and the drawn keys' day rows (bounded); document the
   bound.
6. Score methods (`spec.band` False): only alert rate, flap share and episodes; band metrics null.
7. `--test` value does not change scoring in this task (A and C reuse it later).
8. ARCHITECTURE §11: the lab tables after 011.

## Tests (write first, see them fail)
In `tests/lab/test_lab_score.py` (hand-made `ServiceData` + fake methods returning chosen arrays; no DB unless stated):
1. A band covering exactly 995 of 1000 shared normal minutes → coverage 0.995.
2. One miss of size d on a key of mean 1 → interval score = mean width + (2/0.005)·d / n.
3. Normalization: the same relative band on a key of mean 10 and mean 1 gives the same interval score.
4. MASE: expected = y − 1 everywhere, naive error 2 → 0.5; 7d vs 1d choice.
5. Episodes and flaps: a flag pattern gives the expected episodes, one flap (short) and one flap (close to the previous),
   alert rate per 100 key-days, M1 per service-week.
6. Shared normal minutes: incident ± 15 min removed for the incident's key, `*total` and the doc key; excluded hours
   removed; unobserved ignored; identical set for two different methods.
7. M6 attribution: a template's first 1440 minutes are scored as M6, later ones as its base kind; an M6-only key's later
   minutes are not scored.
8. `days` aggregates sum to the test-part totals.
9. DB (fixture `conn`): migration 011 applies; a run of `Sigma` on a small recorded schema through `run.run` writes
   `lab_results` with the JSON shape above, `lab_episodes` with knob, `lab_bands` step 1 on a day with a flag and step 5
   otherwise; the whole sink write is one transaction (a forced failure leaves no partial rows and the runner's failed
   rows appear).

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes, output clean (report the count)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
