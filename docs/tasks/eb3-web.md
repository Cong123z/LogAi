# Task eb3: API + Evaluation page; delete the lab-run page

Implementer: Sonnet 5.5 — routes, a web thread and a full page rewrite across many files.
Reviewer: Sonnet 5.5 (a separate reviewer agent)
Branch: task/eb3-web (worktree ../logai2-task-eb3, from build/eval-bands after the previous task merged)
PR file: .team/prs/task-eb3-web.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything is unclear, stop and reply `QUESTION: ...`.

## Read first
- Spec: docs/specs/2026-10-30-evaluation-bands.md sections 6, 7, 8.
- Plan: docs/plans/2026-10-30-evaluation-bands.md "Global Constraints", "Review Focus" and "Task eb3" — its
  Interfaces, tests and steps are this brief's requirements, verbatim.
- Code: logai2/bands.py (eb1+eb2), logai2/web/app.py (known_service, baseline_series route, the eval routes to delete), logai2/web/baseline_view.py (series, UnknownKey), logai2/web/static/js/charts.js, views/baseline.js (chart style), views/evaluation.js (to rewrite), index.html #view-evaluation, main.js; the mockup source /tmp/claude-1000/-home-cong-Documents-logai2/e840dcce-66e0-4930-ad3f-7566dff8153f/scratchpad/artifact-files/a4357a13-0248-4b62-94ee-10d5309e66fb/project/Main.dc.html (read-only; layout, copy and states to match; colours are the console tokens).

## Files
- As the plan's Task eb3 Files list (create, modify, delete, docs)
Touching any other file needs a QUESTION first.

## Done when
- [ ] new tests fail before the code and pass after
- [ ] focused set passes: `.venv/bin/pytest -q tests/test_web_bands.py tests/test_web_baseline_view.py tests/test_web_monitored.py` (no full suite)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
