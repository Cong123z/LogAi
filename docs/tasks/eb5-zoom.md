# Task eb5: zoom on the Evaluation charts

Implementer: Sonnet 5.5 — one view file, ECharts options and event sync.
Reviewer: tech lead (Opus) final review + Chrome check
Branch: task/eb5-zoom (worktree ../logai2-task-eb5, from master)
PR file: .team/prs/task-eb5-zoom.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything is unclear, stop and reply `QUESTION: ...`.

## Why
User, 2026-10-30: the Evaluation charts are too hard to read. On real data a few burst hours (e.g. 50,000/min on
`*total`) set the shared y-scale, so every other hour and every band sit flat on the floor, and 7 days × 24 h are
squeezed into one row.

## Read first
- logai2/web/static/js/views/evaluation.js (charts built around line 230-290: one ECharts instance per method,
  shared y max, `evCursor(i)` drives the dashed pointer of all four by `setOption`; there is deliberately NO
  `echarts.connect`, it hid pointers), logai2/web/static/js/charts.js, logai2/web/static/index.html
  (#view-evaluation), logai2/web/static/css/app.css (.ev-*), views/pipeline.js or retrain.js for any existing
  dataZoom style if present.

## Files
- Modify: logai2/web/static/js/views/evaluation.js, logai2/web/static/index.html (#view-evaluation only),
  logai2/web/static/css/app.css (.ev-* only), docs/WEB_UI.md (Evaluation section: one paragraph on zoom).
Touching any other file needs a QUESTION first.

## Requirements
1. Time zoom on all four charts, kept in sync: mouse wheel / trackpad pinch zooms around the cursor, drag pans
   (ECharts `dataZoom` type `inside` on the x axis of each chart), plus ONE `slider` dataZoom under the last chart
   (or a single slider in the panel) showing the 7 days, draggable handles. Zooming or panning any chart, or the
   slider, applies the same start/end to all four (sync by listening to `datazoom` and dispatching
   `{type:'dataZoom', start, end}` to the others, with a guard against loops). No `echarts.connect`.
2. The y-scale follows the visible window: after every zoom/pan, the shared y max = max over the visible hours of
   the rate and of every method's band high (not the whole 7 days), same for all four charts, with a little
   headroom; at full range it equals today's behaviour.
3. A "Reset zoom" button in the chart panel header (44 px, `aria-label`), disabled at full range; keyboard: `+`/`-`
   buttons next to it zoom in/out around the centre (each step halves/doubles the visible span, minimum 6 hours).
4. The day ticks / x labels adapt: at ≤ 2 days visible show hours (e.g. "Thu 29 14:00"), otherwise day names.
5. The hour slider and the dashed pointer keep working; if the cursor hour leaves the visible window, the pointer is
   simply not visible (no auto-pan). Changing key or days resets the zoom.
6. The 1 day / 7 days buttons stay; zoom state is not kept in the URL.
7. No new library; reduced motion: zoom changes without animation when `REDUCED_MOTION`.

## Tests
JS has no unit tests here: `node --check logai2/web/static/js/views/evaluation.js`, and the focused set
`.venv/bin/pytest -q tests/test_web_bands.py tests/test_web_sources.py` must still pass. Describe in the PR file
exactly what you could not check without a browser; the tech lead checks it in Chrome.

## Done when
- [ ] node --check and the focused set pass
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
