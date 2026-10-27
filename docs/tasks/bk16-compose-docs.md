# Task bk16: Compose lab service, run commands, docs, deferred fixes

Implementer: Sonnet 5.5. Reviewer: Sonnet 5.5 (fresh).
Branch: task/bk16-compose-docs (worktree ../logai2-task-bk16, created by the tech lead)
PR file: .team/prs/task-bk16-compose-docs.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Make the lab runnable on the laptop with single-line commands, document it, and close the small items the reviews
deferred, before the laptop proof (bk17).

## Read first
- Spec `docs/specs/2026-10-27-bakeoff-laptop.md` §2 (container modes), §11, §13; `README.md`; `ARCHITECTURE.md`;
  `docs/BUGS.md`; `docker-compose.yml`; `Dockerfile.lab`; `scripts/lab/` (all); `.team/ledger.md` lines mentioning
  "Deferred" / "deferred" (the items below are the list).

## Files
- Modify: `docker-compose.yml`, `README.md`, `ARCHITECTURE.md`, `docs/BUGS.md`, `docs/specs/2026-10-27-bakeoff-laptop.md`
  (status lines only), `scripts/lab/methods/mprofile.py`, `scripts/lab/score.py`, `tests/lab/test_lab_heavy.py`,
  `tests/lab/test_lab_score.py`, `scripts/stress_pipeline.py` (the leftover-schema check only),
  `tests/test_stress_pipeline.py`
Touching any other file needs a QUESTION first.

## Requirements
1. **Compose**: a `logai2-lab` service, `profiles: [lab]`, image `logai2-lab:latest` (build: `Dockerfile.lab`), same
   network and DB env as the engine, no ES env, `mem_limit: 5g`, no restart policy, default command `--check`.
2. **README** (single short lines each): build the lab image; the container modes (spec §2) as a short table; build the
   world; synth counts / engine; run Test B / A / C (core in the venv, all in the lab container); load a Test C run dir;
   open `/#evaluation`; stop linker/bge-m3 before heavy runs.
3. **ARCHITECTURE**: a "Lab (bake-off)" section: data flow, the sink contract, tables, Test A/B/C, decision, known
   lab-vs-live differences (link spec §6.4); keep it short and accurate to the code.
4. **Deferred fixes**:
   - mprofile in-day look-ahead: score each minute with the **left** matrix profile (distance to the nearest earlier
     window only), so day-D windows never match later windows of D; fix the `per[d]` KeyError for a day without the
     key's array; make `test_no_look_ahead` assert mprofile's history distances do not change when day-D data changes.
   - score.py: keep drawn band rows as `array('f')` (NaN → None only at write time); an incident on a non-template key
     (`*error`, `doc:`) applies the level-class masking only when the key is a template.
   - stress_pipeline: the leftover-schema check only considers schemas created by this run's prefix (parallel runs of
     other suites must not fail it); test.
5. **BUGS.md** open entries (one each): mprofile left profile (now fixed → put under Fixed with the commit);
   Prophet's `uncertainty_samples` limits the 0.999/0.9999 tails (widening dominates); lgbm cost at 3k keys unmeasured;
   bk04's known lab-vs-live differences (one entry pointing to spec §6.4).
6. Spec status: mark the laptop spec "implemented (bk00–bk16), proof pending (bk17)".

## Tests
The tests named in 4 (write first, see them fail). Then `.venv/bin/pytest -q` once.
Check `docker compose --profile lab config` parses; do not start the service.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes (report the count); the lab image rebuilt (`docker build -f Dockerfile.lab
      --build-arg TAG=bk11 -t logai2-lab:latest .` — the base tag exists) and the heavy tests pass in it once
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations

## Added after bk15 (page follow-ups)
- `/api/eval/runs` returns `error` for failed runs; the page shows it (eval_view.py, evaluation.js, test).
- `GET /api/eval/choices` (current `lab_choices`); the page shows saved choices and updates the chosen star after Choose.
- evaluation.js minors: disable Choose until a knob is picked; on a failed keys/sweep fetch after a kind switch, keep
  the old kind (or clear the fit panel); neutral status class for "No method selected"; escape window dates.
Files added to this task: `logai2/web/eval_view.py`, `logai2/web/app.py`, `tests/test_web_eval_view.py`,
`logai2/web/static/js/views/evaluation.js`, `docs/WEB_UI.md`.
