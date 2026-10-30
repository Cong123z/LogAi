# Task eb2: tune — walk-forward fit test, CLI, estimate check

Implementer: Sonnet 5.5 — the coverage maths and the estimate check need judgment.
Reviewer: Opus 5.5
Branch: task/eb2-tune (worktree ../logai2-task-eb2, from build/eval-bands after the previous task merged)
PR file: .team/prs/task-eb2-tune.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything is unclear, stop and reply `QUESTION: ...`.

## Read first
- Spec: docs/specs/2026-10-30-evaluation-bands.md sections 5 (all), 2.
- Plan: docs/plans/2026-10-30-evaluation-bands.md "Global Constraints", "Review Focus" and "Task eb2" — its
  Interfaces, tests and steps are this brief's requirements, verbatim.
- Code: logai2/bands.py (eb1), logai2/db.py (get_setting, put_setting), logai2/config.py (floors, load_config), lab_s_w28 schema (hourly tables + lab_minute_counts.counts int[60] per hour).

## Files
- Modify: logai2/bands.py. Create: tests/test_bands_tune.py, scripts/bands_estimate_check.py, docs/validation/2026-10-30-bands-tune.md
Touching any other file needs a QUESTION first.

## Done when
- [ ] new tests fail before the code and pass after
- [ ] focused set passes: `.venv/bin/pytest -q tests/test_bands.py tests/test_bands_tune.py` (no full suite)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)

## Rulings from the eb1 review (tech lead, 2026-10-30)
1. Skip services with `services.monitored = false` (`history` and `in_scope` do not filter them).
2. Lookback: per service, start `history` at the earliest day `training()` can need for the first test day: the
   oldest of (the 5th weekday / 4th weekend committed day before it, first test day − 28). Do not use a fixed
   35 days (a service with commit gaps would get fewer days than REBUILD).
3. A key whose training rows all have s = 0 has no `baseline` row in REBUILD (no live band). Change `band()` in
   `logai2/bands.py` to return None when Σs = 0 over the rows, and add `test_all_zero_rows_no_band`. Tune skips
   such hours like any missing band.
4. Report the timing of `tail_above` / `tail_below` on busy NB keys in the validation doc.
