# Task bk09: Core methods B (#5 error ratio, #6 Poisson CUSUM, #13 PCA on the template mix, E1 vote) and M7 walking

Implementer: Sonnet 5.5. Reviewer: Sonnet 5.5 (fresh).
Branch: task/bk09-core-methods-b (worktree ../logai2-task-bk09, created by the tech lead)
PR file: .team/prs/task-bk09-core-methods-b.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
The last four pure-Python methods: #5 separates traffic from failures, #6 catches small persistent rises, #13 is the only
method that judges the service's template mix (M7), E1 combines three views. M7 also needs the runner and loader to
walk the per-service mix pseudo-key `*mix`, which bk03 deferred to this task.

## Read first
- Methods doc `docs/specs/2026-10-26-bakeoff-methods.md` (#5, #6, #13, E1); spec `docs/specs/2026-10-27-bakeoff-laptop.md` §5.3, §6.
- Code: `scripts/lab/methods/__init__.py` (`apply_d4`, `floor_for`, registry), `scripts/lab/methods/nb.py` (`run_day`,
  `nb_quantiles`, how bands and drop guards are passed), `scripts/lab/methods/sigma.py` (`rebuild_stats`),
  `scripts/lab/methods/weekly.py`, `scripts/lab/data.py` (`MIX`), `scripts/lab/kinds.py` (`assign`),
  `scripts/lab/load.py`, `scripts/lab/run.py` (how keys are walked), `scripts/lab/score.py` (how a key's counts and
  incidents are read; mix_swap incidents have `key` NULL so they cover every key of the service).

## Files
- Create: `scripts/lab/methods/ratio.py`, `scripts/lab/methods/cusum.py`, `scripts/lab/methods/pca.py`,
  `scripts/lab/methods/vote.py`, `tests/lab/test_lab_methods_b.py`
- Modify: `scripts/lab/methods/__init__.py` (register; the M7 rule in `apply_d4`), `scripts/lab/kinds.py`
  (`assign` gives `MIX` → "M7"), `scripts/lab/load.py` (`minutes[MIX]` = the `*total` arrays, same objects),
  `tests/lab/test_lab_kinds.py`, `tests/lab/test_lab_load.py`
Touching any other file needs a QUESTION first (the runner change for Test A is bk10's; `run.py` should need no change
because MIX becomes an ordinary key with kind M7 — if it does, ask).

## Requirements (contract as bk08: fit only on days before D; every knob in `day()`; NaN band where none; unobserved
minutes skipped; `*warn`/`*info` never flagged)
1. **M7 walking**: `assign` adds `MIX: "M7"`; `load_service` sets `minutes[MIX]` to the `*total` day arrays (so the sink's
   shared-minute and count logic works unchanged); `apply_d4` with `key == MIX`: high when `score > high` (no floor —
   a mix alert is not a count); `ServiceData.level(MIX)` stays INFO. Only methods with M7 in `spec.kinds` walk it.
2. **#5 `ratio`** — kinds M2, M5, knobs tail p (1e-2, 3e-3, 1e-3, 3e-4, 1e-4, 1e-5), default 1e-3, cost 1, band.
   Share p̂ per (daytype, hour) = key mean ÷ `*total` mean from σ's profile (`rebuild_stats`), with σ's drop guards
   irrelevant (no drop rule for M2/M5). Per minute with total n = the `*total` count: high = smallest c with
   P(Binomial(n, p̂) > c) ≤ p, low = largest c with P(X < c) ≤ p; expected = n·p̂. n = 0 or no share → NaN band.
   Exact binomial tails in pure Python (log-pmf recursion, cached per (n, p̂ rounded 1e-6, p)).
3. **#6 `cusum`** — kinds M1–M5, score method, knobs h (2, 3, 4, 5, 7, 10), default 4, cost 1. λ0 = σ's profile mean
   for the minute's (daytype, hour) (`lookup`; no stats → no score); λ1 = 1.5·λ0; per knob S = max(0, S + x·ln(λ1/λ0)
   − (λ1 − λ0)); score = S; high via `apply_d4(score=S, high=h)`; after S > h, S resets to 0 (one alarm per crossing).
   S starts at 0 each day (document: state does not carry across days).
4. **#13 `pca`** — kinds M7 only, score method, knobs = Q-threshold quantile (0.99, 0.995, 0.999, 0.9995, 0.9999),
   default 0.999, cost 2, `min_minute_days` 7. Fit: the service's templates with any count in the last 7 days before D
   (at most the 100 busiest), vectors = log1p of per-minute counts on observed minutes, every 5th minute (subsample for
   speed); center; top components by power iteration until 90 % variance (max 10); threshold per knob = that quantile of
   the fit vectors' Q (squared residual). Refit at most every 7 days (reuse the model when the last fit is < 7 days old;
   still only data before D). Per minute: score = Q of that minute's vector; `metrics`-friendly: the model exposes
   the top 3 contributing templates for a minute (`top_templates(model, sd, minute)`), not stored by the sink yet.
5. **E1 `vote`** — kinds M1–M5, score method, one knob (2 = votes needed), cost 3. Internally instantiates `nb`, `cusum`
   and `weekly` (heavy forecasters are not used in this task; document), fits each, and flags a minute when ≥ 2 of their
   default-knob flags are set; score = number of votes. A voter with no model does not vote (needs 2 of the remaining).
6. Register `ratio`, `cusum`, `pca`, `vote` in `METHODS` and `GROUPS["core"]`.

## Tests (write first, see them fail) — `tests/lab/test_lab_methods_b.py`, hand-made `ServiceData`, no DB
- ratio: traffic ×2 with errors ×2 → not flagged; errors ×2 alone (enough volume) → flagged; binomial tails equal a
  brute-force sum for 4 cases; n = 0 → NaN band.
- cusum: +20 % for 60 min on a key of mean 20 is flagged while `nb` at its default misses it; clean Poisson → ≤ 1 alarm
  per day at the default; reset after an alarm.
- pca: a mix swap (two templates' rates exchanged, total unchanged) is scored above the default threshold on ≥ 50 % of
  its minutes; clean data rarely; refit cadence (fit reused within 7 days, no data from D or later used).
- vote: 2 of 3 rule; a missing voter.
- M7 walking: `assign` gives `*mix` M7; `load_service` exposes `minutes["*mix"]` equal to `*total`; a run through
  `run.run` with `pca` writes `lab_results` for kind M7 (DB, `conn` fixture).
- `apply_d4` for `*mix`: no floor.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes, output clean (report the count)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
