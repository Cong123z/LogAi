# Task pm1: Compact baseline, streamed load

Implementer: Sonnet 5.5 — a data-structure swap under the detection math; must give identical bands.
Reviewer: Opus 5.5 (fresh) — a wrong slot or NaN rule gives wrong bands everywhere, silently.
Branch: task/pm1-compact-baseline (worktree ../logai2-task-pm1, created by the tech lead from build/pattern-switch)
PR file: .team/prs/task-pm1-compact-baseline.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
`Baseline` keeps one dict entry of a 4-tuple key and a `Stat` NamedTuple per row: 331 B per row (measured). The company's
biggest service has ~6M rows: GBs at every start (B24). One packed `array("d")` of 96 values per (service, key)
measured 20 B per row. pm3 also needs per-service load / update / drop.

## Read first
- Spec `docs/specs/2026-10-29-pattern-switch-memory.md` §1.5.
- Plan `docs/plans/2026-10-29-pattern-switch-memory.md`: "Global Constraints", "Task pm1" (layout, interfaces, tests).
- Code: `logai2/owner/baseline.py` (all), `logai2/owner/detect.py` (uses `lookup`, `lookup_doc`),
  `logai2/web/baseline_view.py` (uses `Baseline.load(conn, keys)`, `bl.stats` at ~319-321), `logai2/web/app.py`
  (two `Baseline.load(c, keys)` calls), `tests/test_baseline.py`, `tests/test_detect.py`.
- `docs/perf/2026-10-29-memory.md` and `scripts/mem_probe.py` (from pm0, on build/pattern-switch).

## Files
- Modify: `logai2/owner/baseline.py`, `logai2/web/baseline_view.py`
- Test: `tests/test_baseline.py`
Touching any other file needs a QUESTION first. Do not edit `docs/perf/2026-10-29-memory.md`: put the probe JSON in the
PR file; the tech lead adds the row.

## Interfaces
Exactly the plan's pm1 Interfaces block. `stats` attribute removed; `lookup`, `lookup_doc`, `days`, `judge`,
`full_after`, `DAY_CAP`, `FLOOR_LEVEL`, `KEY_LEVEL` unchanged.

## Requirements
1. Exactly the plan's pm1 steps.
2. Slot = `(daytype == "weekend") * 48 + hour * 2` (mean), slot + 1 (std); NaN = no stat. A stored std of 0.0 or a mean
   of 0.0 is a real stat, not "missing".
3. The server-side cursor is used only when `keys is None` (the owner's path); its name must be unique per call or the
   cursor closed before returning, so two loads in one transaction work.
4. `update(other)`: other's keys replace this one's for the same (service, key); other's `days` entries replace too.
   `drop(service)`: removes its keys and its `days` entries.
5. Add a parity test on a seeded database: rows written to `baseline` and loaded give for every row
   `stat(...) == Stat(mean, std)` and `lookup` equal to what the old dict form gave (build the old form in the test
   from the same rows).
6. Tests: `/home/cong/Documents/logai2/.venv/bin/pytest` from inside the worktree; first check `logai2.__file__` is in
   the worktree.

## Tests (write first, see them fail)
The plan's pm1 tests, plus `test_load_parity_with_dict_form` (requirement 5) and
`test_two_streamed_loads_in_one_transaction` (requirement 3).

## Done when
- [ ] new tests fail before the code and pass after
- [ ] `.venv/bin/pytest -q` (full suite) passes, output clean
- [ ] probe run (`--templates 135000`) JSON in the PR file, next to the "before" row
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
