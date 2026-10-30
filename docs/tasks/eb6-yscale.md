# Task eb6: Evaluation y-scale follows the counts, not the widest range

Implementer: Sonnet 5.5 — one view file.
Reviewer: tech lead (Opus) final review + Chrome check
Branch: task/eb6-yscale (worktree ../logai2-task-eb6, from master)
PR file: .team/prs/task-eb6-yscale.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If unclear, stop and reply `QUESTION: ...`.

## Why
User, 2026-10-30: the charts are still unreadable, even zoomed: "a straight nearly 0 line". Cause, measured on the
live laptop DB (recharge, *total): the highest hourly rate is ~1,940/min, but the shared y max is 50,650 because
eb5 sets it from max(rate, every method's band high) and bands at k 8 on bursty data are huge. The counts use the
bottom 4 % of each chart.

## Files
- Modify: logai2/web/static/js/views/evaluation.js, logai2/web/static/index.html (#view-evaluation only),
  logai2/web/static/css/app.css (.ev-* only), docs/WEB_UI.md (Evaluation: one sentence on the scale toggle).

## Requirements
1. Default scale "counts": shared y max (all four charts) = 1.3 × the highest rate in the visible window (eb5's
   window logic), at least 1. Bands higher than that are clipped at the top of the plot (ECharts clips to the grid;
   check `clip: true` on the band series), and a chart whose band is clipped somewhere in the window shows a small
   muted note in its head: "range above the chart" (text, not colour alone).
2. A toggle in the chart panel header next to the zoom buttons: "Scale: counts | ranges" (two buttons,
   aria-pressed, 44 px). "ranges" = eb5's current behaviour (max of rate and band highs in the window).
   Default counts; not in the URL; kept while the page stays open, reset on key change is NOT needed.
3. A rate of 0 in every visible hour: y max = 1.3 × max band high in the window, else 1 (no empty 0–0 axis).
4. Zoom, slider, hour pointer, Reset zoom keep working; the hour slider / head range text unchanged.

## Tests
`node --check logai2/web/static/js/views/evaluation.js`; `.venv/bin/pytest -q tests/test_web_bands.py
tests/test_web_sources.py` still pass. List in the PR file what needs the tech lead's Chrome check.

## Done when
- [ ] checks pass; commit on the branch (explicit paths, no attribution); PR file filled; reply `DONE <sha>`
