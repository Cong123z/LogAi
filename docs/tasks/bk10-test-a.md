# Task bk10: Test A (in-memory injections), recall and delay, M6/M7 decisions

Implementer: Sonnet 5.5. Reviewer: Sonnet 5.5 (fresh).
Branch: task/bk10-test-a (worktree ../logai2-task-bk10, created by the tech lead)
PR file: .team/prs/task-bk10-test-a.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Test B measures fit; nothing in it says whether a method catches incidents. Test A plants incidents into an in-memory
copy of the counts (laptop only), measures recall and delay per method at the alert budget, and feeds the detect gate
of the Test B decision (bk07 reads it) and the M6/M7 decisions, which bk07 left `pending`.

## Read first
- Criteria `docs/specs/2026-10-26-bakeoff-judging.md` criterion 3 and the decision; spec `docs/specs/2026-10-27-bakeoff-laptop.md` §8.
- Code: `scripts/lab/run.py` (`run`, `main`, the sink contract, the Test-B-only finish), `scripts/lab/score.py`
  (`ScoreSink`, shared normal minutes, incidents, episodes), `scripts/lab/decide.py` (`finish_all`, how the detect gate
  reads `metrics["recall"]["medium"]` from the latest ok Test A run's `scope='all'` rows of the same `code_version`;
  the M6/M7 `pending` rows), `scripts/lab/tune.py`, `scripts/lab/data.py` (`ServiceData`, `Incident`, `MIX`),
  `scripts/lab/kinds.py`, `scripts/lab/store.py`.

## Files
- Create: `scripts/lab/inject.py`, `scripts/lab/test_a.py` (Test A finish), `tests/lab/test_lab_inject.py`
- Modify: `scripts/lab/run.py` (Test A flow), `scripts/lab/score.py` (recall/delay accumulation), `scripts/lab/decide.py`
  (M6/M7 decisions from the latest Test A run), `tests/lab/test_lab_run.py`, `tests/lab/test_lab_decide.py`
Touching any other file needs a QUESTION first.

## Requirements
1. **Run flow for `--test A`** (`--site company` is already refused): per service, load the clean `ServiceData`;
   build 5 injection passes (seeds `seed+0..4`); for every method and day: `fit(clean_sd, d)` **once** (fits are reused
   across passes and never see injected data), then for each pass `day(model, prev, injected_sd_p, key, d, ...)` →
   the pass's sink. Memory: an injected copy shares unchanged arrays with the clean data (copy only the touched
   key-day arrays).
2. **Injections** (`inject.py`, J5), placed only in the test part, never within ± 15 min of a planted (`synthetic`) or
   `test-c` incident of the service, ≥ 3 h apart per key, seeded:
   | type | target kinds | sizes (small, medium, large) | duration |
   |---|---|---|---|
   | spike | M2–M5 templates/doc keys | ×2, ×3, ×5 of the expected level | 10 min |
   | drop | M1 (`*total`) | ×0.5, ×0.3, ×0.1 | 30 min |
   | ramp | M1–M4 | to ×1.5, ×2, ×3 linearly | 60 min |
   | level_shift | M1–M4 | ×1.3, ×1.5, ×2 | 240 min |
   | burst | new template (M6) | 0 → 5, 20, 50 per min | 15 min |
   | mix_swap | M7 | two templates' counts exchanged (size: their rate ratio ≥ 2, 4, 8) | 60 min |
   "Expected level" = σ's profile mean for that minute (`rebuild_stats` of the clean data); a target whose expected
   count over the window is < 10 (spike/ramp/level_shift) or < 50 (drop) is skipped. A burst adds a new key with
   `first_seen` = the burst start (M6). Per pass about 3 injections per type per service.
3. **Consistency**: an injection into a template changes `*total`, its level-class key and every `doc:` key containing
   it by the same delta on the same minutes (and `*mix`, which shares `*total`'s arrays); `*total` = Σ templates still
   holds after injection (test). Counts never go below 0 (drop: round down, floor 0).
4. **Incidents**: every injection becomes an `Incident(key, start, end, 'test-a')` in the injected copy's `incidents`
   (so the sink excludes it from shared normal minutes) and is written to `lab_incidents` (source `test-a`, `run_id`).
5. **Recall and delay** (in the sink when `ctx` marks Test A, or a separate sink — your choice, documented): per method
   × kind × type × size × knob: hit = an episode of that key (for drop: `*total`; for mix_swap: `*mix`) starting in
   `[start, start + 15)`; recall = hits ÷ injections; delay = episode start − injection start over hits (median, p90).
   Also the alert rate on shared normal minutes of the tune part per knob (for the budget).
6. **Test A finish** (`test_a.py`, passed by `main` only for `--test A`): per method × kind, knob = the most sensitive knob
   whose tune-part alert rate ≤ budget (1 per 100 key-days; M1 1 per service-week); write one `scope='all'`
   `lab_results` row with `knob` and `metrics = {"recall": {"small", "medium", "large"}, "by_type": {type: {size:
   recall}}, "delay": {"median", "p90"}, "alert_rate"}` (medium = the headline). σ included.
7. **M6/M7 decisions** (`decide.py`, in the Test B `finish_all`): when an ok Test A run of the same `code_version`
   exists, replace the `pending` rows: M6 → leader = highest medium burst recall among methods passing the alert-rate
   and flap gates; `adopt candidate` if its recall ≥ σ's + 0.10, else `sigma stays`. M7 → σ does not judge the mix:
   `adopt candidate` if the leader's medium mix_swap recall ≥ 0.5 and its gates pass, else `no method`. Clear the
   ranking fields bk07 left on pending rows. No Test A run → `pending` as now.

## Tests (write first, see them fail) — `tests/lab/test_lab_inject.py` (+ the run/decide test files)
1. One spike injected on a quiet key → exactly one hit with the expected delay for a fake method that flags it.
2. Consistency: `*total` = Σ templates and doc/level keys move together after every injection type; counts ≥ 0.
3. Placement: test part only, never within ± 15 min of a planted incident, ≥ 3 h apart per key; same seed → same set.
4. Fits are computed once and never see injected counts (a recording fake method).
5. Burst creates an M6 key; mix_swap leaves `*total` unchanged.
6. Test A finish writes the `recall` shape bk07 reads; then a Test B `finish_all` with that run present fills the
   detect gate and decides M6/M7 (adopt candidate / sigma stays / no method) on hand-made rows.
7. `--site company --test A` still exits 2.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes, output clean (report the count)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
