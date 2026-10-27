# Task bk14: Evaluation API

Implementer: Sonnet 5.5. Reviewer: Sonnet 5.5 (fresh).
Branch: task/bk14-eval-api (worktree ../logai2-task-bk14, created by the tech lead)
PR file: .team/prs/task-bk14-eval-api.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
The Evaluation page (bk15) shows each lab run: the decision per kind, the scoreboard, every method's band over the real
counts, episodes, and the knob sweeps for calibration. This task serves that data as JSON from the `lab_*` tables only.

## Read first
- Spec `docs/specs/2026-10-27-bakeoff-laptop.md` §10 (tables), §12 (API and page); mockup
  https://claude.ai/artifact/EXqQaUkx4aAZTsEmj1A2be (what the page shows).
- Code: `logai2/web/app.py` (route style, `conn()`, `BadRequest`, `iso`, error handlers; UI routes have no token —
  only `/api/module-alerts` checks `cfg.web.api_token`), `logai2/web/baseline_view.py` (a view module's style),
  `tests/test_web_baseline_view.py` (the `client` fixture), the lab tables as written by `scripts/lab/store.py`,
  `scripts/lab/score.py` (`lab_results.metrics` per-service shape; `lab_bands` per-day arrays with `step` 1 or 5;
  `lab_episodes` with `knob`), `scripts/lab/decide.py` (`scope='all'` rows, `_decision` rows), `scripts/lab/test_a.py`
  if merged when you start (Test A recall rows), `logai2/migrations/010_lab.sql`, `011_lab_knob.sql`.

## Files
- Create: `logai2/web/eval_view.py`, `tests/test_web_eval_view.py`
- Modify: `logai2/web/app.py` (routes), `docs/WEB_UI.md` (the API section for these routes)
Touching any other file needs a QUESTION first.

## Endpoints (all read `lab_*` only, unqualified names; JSON; times as ISO via `iso`)
| Route | Returns |
|---|---|
| `GET /api/eval/runs` | the newest 20 runs: id, site, test, status, started/finished, window_from/to, tune_to, data_schema, code_version, strength (from any `_decision` row), kinds counts |
| `GET /api/eval/scoreboard?run=` | per kind: the `_decision` row (verdict, leader, note, strength, stability) and per method the `scope='all'` row (knob, status, verdict, ratio_vs_sigma, ci90, gates, coverage, interval_score, sharpness, mase, alert_rate, flap_share); for a Test A run: recall/delay per method and kind instead; methods in a stable order (σ first) |
| `GET /api/eval/keys?run=&kind=` | drawn keys with bands for the run: service, key, kind, methods that have bands |
| `GET /api/eval/series?run=&service=&key=&methods=&knob=&from=&to=` | per method: the band rows for the days in [from, to] at the requested knob (default: the method's chosen knob from its `scope='all'` row, else its default), expanded to `{t, count, expected, low, high, flag}` points with `t` = minute ISO; step-5 days give 288 points, step-1 days 1440; plus the method's episodes in the window at that knob |
| `GET /api/eval/sweep?run=&kind=` | `lab_sweeps` rows for the kind: per method, per knob, tune and test metrics; the chosen knob marked |
| `POST /api/eval/choices` | body `{kind, method, knob, run_id, note}` → upsert `lab_choices`; validation: kind in M1–M7, method known in that run, knob in that method's grid (from `lab_runs.params.knobs`) |

- Bad or unknown `run` → 404 `{"error": ...}`; a missing parameter → 400 (`BadRequest`).
- No route touches live tables; no route reads count tables (the band rows carry the counts).
- `series` caps the window at 7 days per request (400 beyond) to bound the payload.

## Tests (write first, see them fail) — `tests/test_web_eval_view.py`, hand-made `lab_*` rows on the `conn` fixture
1. `runs` lists runs newest first with strength.
2. `scoreboard` for a Test B run: decision per kind and per-method fields; for a Test A run: recall shape, no Test B fields.
3. `series`: step-5 and step-1 days expanded to the right points and times; NaN/None values pass as `null`; default knob
   = chosen knob; episodes included; window > 7 days → 400.
4. `sweep`: knobs ordered, chosen knob marked.
5. `choices`: valid POST upserts; bad kind/method/knob → 400; a POST changes no live table (row counts of
   `alert_state`, `baseline`, `hourly_counts` unchanged).
6. Unknown run → 404.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes, output clean (report the count)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
