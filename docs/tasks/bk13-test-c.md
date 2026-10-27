# Task bk13: Test C (real incidents through ES), A vs C gap, drawn keys' kinds

Implementer: Sonnet 5.5. Reviewer: Sonnet 5.5 (fresh).
Branch: task/bk13-test-c (worktree ../logai2-task-bk13, created by the tech lead)
PR file: .team/prs/task-bk13-test-c.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Test C is the only test whose incidents pass through the whole real path (replayer → ES → reader → Drain3 → counts →
recorder). `scripts/incident_test.py` already injects bursts through the replayer and logs them; this task turns such a
run into `lab_incidents` rows, scores recall/delay on them like Test A, and flags methods whose Test A and Test C recall
differ by > 10 points (the injector would then be mis-modelling something). It also closes the bk14 gap: the Evaluation
API guesses each drawn key's kind; the sink now stores it.

## Read first
- Preflight `docs/validation/2026-10-27-bakeoff-preflight.md` ("incident_test.py run directory" — the exact format and
  the template-id re-resolution rule), spec `docs/specs/2026-10-27-bakeoff-laptop.md` §8.
- Code: `scripts/incident_test.py` (`SCENARIO`, `Runner.run`, `meta.json`, `phases.jsonl`), `scripts/lab/run.py`
  (`--test C` flow; how Test A builds passes and its finish), `scripts/lab/test_a.py` (recall/delay finish),
  `scripts/lab/score.py` (`InjectSink`, recall accumulation, drawn keys in `end()`), `scripts/lab/load.py`
  (incidents: `test-c` rows are loaded into `sd.incidents`), `logai2/web/eval_view.py` (`keys`: the kind heuristic).

## Files
- Create: `scripts/lab/load_incidents.py`, `tests/lab/test_lab_test_c.py`
- Modify: `scripts/lab/run.py` (`--test C`), `scripts/lab/test_a.py` (reuse the finish for C, plus the A vs C gap),
  `scripts/lab/score.py` (recall on stored incidents; drawn keys' kinds), `logai2/web/eval_view.py` (read the stored
  kinds), `tests/test_web_eval_view.py`, `docs/WEB_UI.md`, `README.md` (Test C commands, single lines)
Touching any other file needs a QUESTION first.

## Requirements
1. `python -m scripts.lab.load_incidents <run_dir> [--dsn] [--schema public]`: reads `meta.json` and `phases.jsonl`;
   every phase of kind `burst` or `spread` with targets → one row per target in `lab_incidents` (source `test-c`,
   `data_schema`, service, key = the template id **re-resolved** against the current `templates` of that service by
   exact text (`meta.targets[label].text`), else by the inject `pattern` regex; ambiguous or none → key NULL and a
   warning), `start_minute` = phase start // 60, `end_minute` = phase end // 60 (exclusive), size = `per_minute` of the
   target's inject event, `meta` = run dir name, label, phase. Inject events with status ≠ 200 → skipped with a
   warning. Re-loading the same run dir replaces its rows (idempotent, keyed on `meta.run_dir`).
2. `--test C` (laptop only; company refused as today): Test B data (no in-memory injection); recall/delay scored on the
   window's `test-c` incidents with the same hit rule as Test A (episode start in `[start, start + 15)` on the key; a
   doc key's incident also counts hits on the doc key); knob per method × kind = the most sensitive within budget, as
   Test A; finish writes the same `scope='all'` recall shape. No Test C incidents in the window → the run ends ok with
   a note and no recall rows.
3. **A vs C gap**: the Test C finish compares, per method × kind, its medium-size recall with the latest ok Test A run
   of the same `code_version` (by type `spike` vs the burst/spread incidents); a gap > 0.10 → `metrics.injector_flag =
   true` with both numbers.
4. **Drawn keys' kinds**: the sink stores `{key: kind}` for its drawn keys in the per-service `lab_results.metrics`
   (`"drawn"`); `/api/eval/keys` reads it (no heuristic); WEB_UI.md updated. Old runs without it fall back to the
   heuristic.

## Tests (write first, see them fail)
- A fixture run directory (built in the test from the format above) loads into the expected rows; exact-text and regex
  re-resolution; ambiguous pattern → NULL key + warning; non-200 inject skipped; reload replaces.
- `--test C` on a small recorded schema with two `test-c` incidents: a fake method that flags one of them → recall 0.5
  and the expected delay; no incidents → ok run, no recall rows.
- A vs C gap flag with hand-made Test A rows.
- `keys` returns stored kinds (M6 included) for a new run and the heuristic for an old one.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes, output clean (report the count)
- [ ] the old run dir `reports/incident-20261020-2351` loads into the **test** DB without error (paste the warnings —
      its template ids predate the DB rebuild, so re-resolution is exercised)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
