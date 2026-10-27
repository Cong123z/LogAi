# Task bk15: Evaluation page

Implementer: Sonnet 5.5. Reviewer: Sonnet 5.5 (fresh). The tech lead checks every board in Chrome before merge.
Branch: task/bk15-eval-page (worktree ../logai2-task-bk15, created by the tech lead)
PR file: .team/prs/task-bk15-eval-page.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
The user chooses and calibrates a method per monitor kind by looking at it: the decision per kind, the scoreboard, and
every method's band drawn over the real counts, with the knob sweep to calibrate. This page shows a lab run.

## Read first
- Mockup (2 boards: Evaluation, Calibrate): https://claude.ai/artifact/EXqQaUkx4aAZTsEmj1A2be — build it as shown,
  with the spec's changes: verdict strength, 90 % CI, site, laptop detect/Test A columns; no Episodes board, no Qwen,
  no labels.
- Spec `docs/specs/2026-10-27-bakeoff-laptop.md` §12; `docs/WEB_UI.md` (the `/api/eval/*` section: exact JSON shapes,
  `series` needs `kind`).
- Code: `logai2/web/static/index.html` (nav entries, view sections, script tags), `logai2/web/static/js/main.js`
  (views, titles, polling, routing), `logai2/web/static/js/views/baseline.js` and `retrain.js` (view style, ECharts use,
  DOM helpers), `logai2/web/static/js/charts.js`, `keypicker.js`, `core.js`, `ui.js`, `logai2/web/static/css/app.css`
  (tokens, components; reuse, do not restyle the app).

## Files
- Create: `logai2/web/static/js/views/evaluation.js`
- Modify: `logai2/web/static/index.html` (nav "Evaluate" + view section + script tag), `logai2/web/static/js/main.js`
  (register the view, no polling), `logai2/web/static/css/app.css` (only what the page needs, with existing tokens),
  `docs/WEB_UI.md` (the page section)
Touching any other file needs a QUESTION first. No new JS library: ECharts is already loaded.

## Requirements
1. **Run bar**: run picker (newest first: id, site, test, window, status), strength badge, kind counts.
2. **Decision per kind** (M1–M7 cards): verdict (adopt candidate / sigma stays / mixed / pending / no method), leader,
   strength, note, stability; Test A runs show recall instead.
3. **Scoreboard** (per kind tab): one row per method — knob, ratio vs σ with its 90 % CI as a small bar, coverage,
   interval score, sharpness, MASE, alert rate, flap share, detect, and pass/fail cells per gate (colour + text, not
   colour alone); σ first; Test A runs: recall small/medium/large and delay median/p90.
4. **Fit chart**: key picker (from `/api/eval/keys` for the kind, reusing `keypicker.js` the way Baseline does), method
   toggles, date window (≤ 7 days); small multiples (one panel per method) with a shared zoom, plus an overlay mode;
   count line, expected line, band area; flagged minutes and episodes marked; step-5 and step-1 data both draw correctly.
5. **Calibrate board**: for a kind, the sweep chart (coverage and alert rate vs knob per method, chosen knob marked);
   knob pills for the selected method redraw its band in the fit chart at that knob; "Choose" → `POST /api/eval/choices`
   with a note, then shows the saved choice. Errors from the API are shown as the message, not swallowed.
6. Empty states: no runs yet ("Run `python -m scripts.lab.run ...` first" with the command), a run with failed status
   (its error), a kind with no keys.
7. Accessible basics: buttons are buttons, focus visible, colour never the only signal.

## Tests
No JS test framework exists: add a Python test only if you add a route (you should not). In the PR file, give a manual
check list with what you verified in a browser against a seeded run. To seed one: build a tiny scenario
(`python -m scripts.lab.synth --mode counts --scenario lab/scenarios/tiny.yaml --schema lab_s_tiny --dsn postgresql://logai:logai@127.0.0.1:5433/logai_test`
— use the **test** DB `logai_test`, never the live DB) and run `python -m scripts.lab.run --site laptop --test B
--methods core --days 3 --schema lab_s_tiny --results-schema lab_s_tiny --dsn <same>`; serve the web app against the
test DB on a spare port (e.g. `LOGAI2_DATABASE_DSN=... LOGAI2_WEB_PORT=5599 .venv/bin/python scripts/run_web.py` with
`search_path` set to that schema if needed) and open it. Do not start/stop the Docker containers.

## Done when
- [ ] every board renders against the seeded run; the manual check list is in the PR file
- [ ] `.venv/bin/pytest -q` passes (nothing should change)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in (how to reproduce the seeded run and open the page)
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
