# Task eb1: `logai2/bands.py` — data, the 4 methods, tails, scope

Implementer: Sonnet 5.5 — detection math and SQL that must match REBUILD exactly.
Reviewer: Opus 5.5
Branch: task/eb1-bands (worktree ../logai2-task-eb1, from build/eval-bands)
PR file: .team/prs/task-eb1-bands.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything is unclear, stop and reply `QUESTION: ...`.

## Why
First of 3 code tasks for the new Evaluation page: the pure module every later task (tune, API) builds on.

## Read first
- Spec: docs/specs/2026-10-30-evaluation-bands.md sections 2, 3, 4, 5.2 (tails).
- Plan: docs/plans/2026-10-30-evaluation-bands.md "Global Constraints", "Review Focus" and "Task eb1" — the
  Interfaces, tests and steps there are this brief's requirements, verbatim.
- Code: logai2/jobs/commit.py (MARK_DAYS, REBUILD), logai2/owner/baseline.py (DAY_CAP, FLOOR_LEVEL, KEY_LEVEL),
  logai2/web/baseline_view.py (resolve_key, picked_days), scripts/lab/methods/nb.py (_quantiles),
  tests/test_web_baseline_view.py (fixture helpers).

## Files
- Create: logai2/bands.py, tests/test_bands.py
Touching any other file needs a QUESTION first.

## Done when
- [ ] new tests fail before the code and pass after
- [ ] focused set passes: `.venv/bin/pytest -q tests/test_bands.py tests/test_commit.py` (no full suite)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
