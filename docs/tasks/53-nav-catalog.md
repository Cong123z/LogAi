# Task 53: Nav — Baseline and Review move from Settings to Catalog

Implementer: Sonnet 5.5 — markup-only change with a browser check.
Reviewer: a fresh Sonnet 5.5 subagent
Branch: task/53-nav-catalog (worktree ../logai2-task-53, created by the tech lead)
PR file: .team/prs/task-53-nav-catalog.md (from docs/templates/pr.md; .team/ is in the main checkout
~/Documents/logai2, write it there)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below
is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Baseline and Review are pages operators read and act on, not settings. Settings will hold Data sources, LLM
profiles and (task 59) Retrain. Approved mockup: https://claude.ai/artifact/VdaHzwwbkVPkWYDMAofnsa (sidebar).

## Read first
- Spec: docs/specs/2026-10-26-retrain.md §1 (last line), §8 item 4.
- Code: `logai2/web/static/index.html` sidebar (`<nav class="sidebar-nav" id="sidebar-nav">`, ~L37-52);
  `logai2/web/static/js/main.js` (how `data-view` and the active link work — read only);
  `docs/WEB_UI.md` page list near the top.

## Files
- Modify: `logai2/web/static/index.html`, `docs/WEB_UI.md`
Touching any other file needs a QUESTION first.

## Requirements
1. Sidebar order: Monitor (Alerting, Pipeline); Catalog (Templates, Documentation, Baseline, Review);
   Settings (Data sources, LLM profiles). Move the two `<a>` elements as they are (href, `data-view`, title,
   icon, label unchanged).
2. No route, view or JS change.
3. `docs/WEB_UI.md`: wherever the page list or nav groups are described, Baseline and Review are under Catalog.

## Tests
- No Python test is needed (markup only). Run the full suite anyway.
- Browser check: start the web from the worktree on a free port against the test database, e.g.
  `LOGAI2_DATABASE_DSN=postgresql://logai:logai@localhost:5433/logai_test .venv/bin/python -m logai2.web --port 5599`
  (check README/`logai2/web` for the real entry point and flags; if it needs anything else, ask). Open
  `/#baseline` and `/#review`: the link is highlighted under Catalog, the page loads; collapse the sidebar:
  icons still in the new order. Record what you checked in the PR file. If you cannot run a browser, say so in
  the PR file and describe the static check you did instead.

## Done when
- [ ] `.venv/bin/pytest -q` passes, output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
