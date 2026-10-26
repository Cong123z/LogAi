# Task 59: Retrain page

Implementer: Sonnet 5.5 — vanilla JS view over the task 58 API, following the approved mockup.
Reviewer: a fresh Sonnet 5.5 subagent
Branch: task/59-retrain-page (worktree ../logai2-task-59, created by the tech lead)
PR file: .team/prs/task-59-retrain-page.md (from docs/templates/pr.md; .team/ is in the main checkout
~/Documents/logai2, write it there)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below is unclear, stop and reply
`QUESTION: ...` instead of guessing. Never kill processes you did not start.

## Why
The operator's page for retraining: what runs now and how far it is, what ran before and why it failed, when
the nightly rebuild runs, and one button to start a Rebuild or a Re-read. The user approved the mockup:
https://claude.ai/artifact/VdaHzwwbkVPkWYDMAofnsa — boards 1 (idle), 2 (re-read running), 3 (Run retrain
dialog), 4 (failed run opened). Build it in the **console's existing look** (`css/tokens.css`, `css/app.css`
classes: cards, tables, badges, buttons, `.modal-overlay`, `.sync-status`), not the mockup's fonts and
colours; the mockup fixes the **layout, content and wording**.

## Read first
- Spec: docs/specs/2026-10-26-retrain.md §1-§6. Plan: task 59. docs/WEB_UI.md (HTTP API `/api/retrain*`).
- Code: `logai2/web/static/index.html` (sidebar, a `<section class="view">`, a modal, script tags),
  `js/main.js` (`viewTitles`, `POLL_MS`, `setView`, `loadData`), `js/core.js` / `js/ui.js` (helpers:
  `getJson`, `escapeHtml`, `formatCount`, time formatting — reuse them), `js/views/sources.js` and
  `js/views/llm.js` (form + revision + 409 handling patterns), `js/views/review.js` (Recompute button and
  command polling ~L582), `css/app.css` (`#rt-status` rules already exist at ~L811; reuse), `logai2/web/app.py`
  routes `/api/retrain*` and `/api/commands/<id>`.
- The engine's run row shape: `retrain_runs` steps `[{key, label, started_at, finished_at, detail}]`;
  read detail keys `docs_read, docs_target, hours_done, hours_total, new_templates, rate, position_ms,
  live_lag_s, paused, pct, eta_s, resumed_at?, error?`; scope detail keys `services, patterns, hours_chosen,
  hours_not_in_es, hours_live, hours_to_read`; rebuild steps `mark {days}`, `rebuild {rows_before, rows}`,
  `housekeeping {partitions_dropped, runs_deleted}`.

## Files
- Create: `logai2/web/static/js/views/retrain.js`
- Modify: `logai2/web/static/index.html`, `logai2/web/static/js/main.js`, `logai2/web/static/css/app.css`,
  `logai2/web/static/js/views/review.js`, `docs/WEB_UI.md`
Touching any other file needs a QUESTION first.

## Requirements
1. Sidebar: Settings → **Retrain** after LLM profiles (icon: circular arrow, as the mockup); route `#retrain`;
   `#retrain/<run id>` opens that run's details. `viewTitles`, `loadData`; poll every 2 s while a run is
   `running`, else every 15 s (adjust the interval on state change).
2. **One action button**, "Run retrain", in the header; disabled with a title while a re-read runs or the
   engine is stale.
3. **Status line** (`#rt-status` style: one sentence, coloured by state): running ("Re-reading (manual) for
   12m 40s · now: Read from Elasticsearch… Live detection keeps running." / "Rebuilding…"), last succeeded
   ("Last retrain succeeded <time> (<kind>, <duration>). Next retrain: <next_run_at> (Asia/Ho_Chi_Minh)."),
   last failed (red: "Last retrain failed at <step label>. … Next retrain: …"), engine stale (red), schedule off
   ("Automatic retrain is off. …").
4. **Run details panel** (`<section>` above the history; hidden when nothing to show): shows the current run;
   else the history row picked; else the last run if it failed, until Close. Header: kind · scope (services,
   day range, hour range), run id, patterns / live lag line for a re-read; **Cancel** while a re-read runs
   (POST cancel, then poll); **Close** otherwise. Error box (failed: `error_type`, `error`). **Step list**:
   mark (✓ done, ● current, ✕ failed, ○ waiting), label, time (duration), one detail line built from the
   detail keys exactly as the mockup words it; the current read step shows a `<progress>` with "45% · about
   15m left" (eta from `eta_s`). Waiting steps: show "Rebuild baseline — waiting" while a re-read reads.
   **Warnings log**: `<details>` "Warnings and errors from the engine log (N)", WARNING/ERROR lines with
   time, level, logger; open by default for a failed run.
5. **Schedule row**: switch "Nightly rebuild at", `<input type="time">`, 7 day toggles (Mon..Sun,
   `aria-pressed`), "Asia/Ho_Chi_Minh", Save (disabled until changed) / Discard; refuse saving with no day
   ticked (inline message); 409 keeps the draft and says "changed elsewhere" (as sources.js). One meta line:
   "A run at 02:00 belongs to the new day: it rebuilds from the days before it." (reviewer note from task 55).
6. **History table**: Started, Kind, Scope, Trigger, Duration, Result (badge; failed shows "at <step label>").
   Clicking a row (or Enter on it) opens its details (`GET /api/retrain/runs/<id>`), highlights the row.
   Times in local format via the existing helpers.
7. **Run retrain dialog** (`.modal-overlay`): Kind (Rebuild / Re-read, then rebuild); for Re-read: services
   (multi-select chips with an "all services" default; list from `services`), days (7 checkbox tiles for the
   last 7 local days; the oldest tile says "from HH:MM" when `es_oldest` says ES starts mid-day; today says
   "to HH:00"), hour From / To selects (0-23 / 1-24), an **estimate** line (POST estimate, poll the command:
   "About 41.2M docs, ~27 min. Stored counts of these hours are replaced.") refreshed on change (debounced),
   an LLM warning line when `llm_active`, Cancel / Start. Start: POST runs; on 202 poll the command; show
   `error` from the command result in the dialog ("nothing to re-read", "a retrain is running"); on success
   close and open the new run's details.
8. Review page: its "Recompute baseline" button becomes a link "Retrain…" to `#retrain` (remove its command
   polling code if nothing else uses it).
9. Accessibility: real buttons/inputs/labels, `aria-live="polite"` on the status line, focus moves into the
   dialog and back, Esc closes it, keyboard row selection; contrast via existing tokens; works at phone width
   (tables scroll in `.table-wrap`).
10. All text through `escapeHtml`; no inline event handlers.
11. docs/WEB_UI.md: a "Retrain" page section (what each part shows, polling) and the nav list.

## Checks (no JS test framework in this repo)
- Run the full Python suite (it must stay green).
- Start the web from the worktree against the test DB on a free port (see how task 53 did it in its PR file:
  `scripts/run_web.py` with `LOGAI2_WEB_PORT`), seed `retrain_runs` rows (one running reread with a read
  step, one failed reread, rebuilds) and settings with a small script in your scratch dir (not committed),
  fetch the page and every API call it makes, and if a browser is available check each mockup board's state.
  Record exactly what you checked in the PR file; stop the server afterwards.

## Done when
- [ ] `.venv/bin/pytest -q` passes, output clean
- [ ] the checks above done and written in the PR file
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
