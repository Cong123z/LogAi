# Task bk07: Tuning, sweeps, bootstrap, decision

Implementer: Sonnet 5.5 — statistics and the decision tree: subtle tie and CI rules.
Reviewer: Opus 5.5 (fresh).
Branch: task/bk07-tune-decide (worktree ../logai2-task-bk07, created by the tech lead)
PR file: .team/prs/task-bk07-tune-decide.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
bk05's sink stores, per method × kind × service, the metrics at every knob for the tune and test parts and per-day
test aggregates. This task runs after the last service (the runner's `finish` hook) and turns them into the verdicts:
it picks each method's knob on the tune part, writes the sweeps, compares every method to σ with a day-block
bootstrap, applies the gates and the decision tree per kind, and writes the verdicts.

## Read first
- Criteria: `docs/specs/2026-10-26-bakeoff-judging.md` (all; criteria 1, 4 and the decision are this task).
- Spec: `docs/specs/2026-10-27-bakeoff-laptop.md` §9, §10, §11.
- Code: `scripts/lab/run.py` (`run(..., finish=None)`, `main`), `scripts/lab/score.py` (the `lab_results.metrics`
  JSON: `knobs` → `{tune, test}` M, `days` → per-day D; read its docstrings), `scripts/lab/store.py`,
  `scripts/lab/methods/__init__.py` (`Spec.band`, `Spec.cost`, `Spec.heavy`, `default_knob`), `logai2/migrations/010_lab.sql`
  and `011_lab_knob.sql`.

## Files
- Create: `scripts/lab/tune.py`, `scripts/lab/bootstrap.py`, `scripts/lab/decide.py`, `tests/lab/test_lab_decide.py`
- Modify: `scripts/lab/run.py` (pass the finish hook by default from `main`), `scripts/lab/store.py` (writers you need)
Touching any other file needs a QUESTION first.

## Interfaces
- `finish_all(results_conn, run_id, *, seed: int, cfg) -> None` in `decide.py`: the default `finish` hook.
- Writes: `lab_sweeps` (every method × kind × knob × part, metrics summed over services); one `lab_results` row per
  method × kind with `scope = 'all'`: `knob` = the chosen knob, `status`, `metrics` = the test metrics at that knob
  + `ratio_vs_sigma`, `ci90` {ratio, mase, alert_rate}, `gates` {name: pass/fail/not_checked}, `verdict`, `strength`;
  and a per-kind decision row with `method = '_decision'` (verdict text and the leader). Per-service rows stay.

## Requirements
1. **Aggregate over services** per method × kind × knob × part: sum the additive parts (minutes, covered, winkler
   sums, abs errors, episodes, flaps, key_days) and recompute the ratios. Medians (sharpness) cannot be summed: take the
   minute-weighted mean of the per-service medians and say so.
2. **Tune (J4)** on the tune part: band methods → knob whose coverage is closest to 0.995 (tie → the wider band, i.e.
   the higher coverage); score-only methods → the most sensitive knob (lowest threshold) whose alert rate ≤ budget
   (1 per 100 key-days; M1 1 per service-week); none within budget → the least sensitive knob and gate fails. σ is
   tuned the same way.
3. **Ratio vs σ**: test interval score of the method at its knob ÷ σ's at σ's knob, per kind.
4. **Bootstrap (J6)**: resample the test days with replacement, 1,000 times, `random.Random(seed)`; per draw recompute
   the ratio (from the per-day aggregates of the method and σ on the same drawn days), MASE and alert rate; 5th–95th
   percentiles → `ci90`. Same seed → same numbers.
5. **Gates (J7)**: band present (M1–M5); cost within budget (a constant per spec `cost`; for now every method passes,
   record "pass"); coverage 0.990–0.999 on test; alert rate ≤ budget on test; flap share ≤ 0.10; detect check: the
   latest `lab_runs` row with `test = 'A'`, same `code_version`, status ok → its Test A recall (medium size) for this
   method and kind vs σ's not worse by > 0.05; no such run → `not_checked` (the method cannot be adopted).
6. **"Fits better than σ"**: ratio ≤ 0.90 and ci90 upper < 1.00.
7. **Decision per kind**: survivors of the gates ranked by ratio; methods whose ci90 overlaps the leader's are tied;
   ties → lowest `cost`, then core before heavy, then lower MASE. Service check: the leader is worse than σ (per-service
   ratio > 1.0) on > 20 % of services → `mixed`. Verdicts: `adopt candidate` (leader fits better than σ and passed every
   gate), `sigma stays` (no method fits better, or σ recalibrated wins — say which knob), `mixed`, and per method
   `not suitable` (failed a gate) / `ok`.
8. **Strength**: exploratory when < 14 days in the run window, provisional at 14–27, final at ≥ 28 with ≥ 2 test
   weekends; stability across two consecutive runs of the same site: if the previous ok run's leader for the kind differs
   (and is not tied), note `unstable` in the decision metrics.
9. `main` passes `finish=lambda conn, rid: finish_all(conn, rid, seed=args.seed, cfg=cfg)`.

## Tests (write first, see them fail)
In `tests/lab/test_lab_decide.py` (hand-made `lab_results` rows on the `conn` fixture; no real methods):
1. Tune picks the knob closest to 99.5 % coverage; tie → higher coverage; score method picks the most sensitive knob
   within budget.
2. Aggregation over two services: sums and recomputed ratios equal a hand calculation.
3. Bootstrap: same seed → same ci90; another seed differs; a method identical to σ has ci90 containing 1.0.
4. Verdicts on hand-made scoreboards: adopt candidate; sigma stays (ratio 0.95); CI crossing 1.0 → not "fits better";
   a gate failure → not suitable; tie → cheaper method leads; service check → mixed.
5. Detect gate: no Test A run → not_checked and no adoption; a Test A run with worse recall → fail.
6. Strength labels for 7, 14 and 28-day windows.
7. `run.main` end to end on a tiny recorded schema with `sigma` only: `scope='all'` rows and a `_decision` row exist.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes, output clean (report the count)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
