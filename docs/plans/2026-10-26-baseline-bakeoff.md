# Baseline Bake-off Implementation Plan

**Superseded for the laptop build** by `docs/plans/2026-10-27-bakeoff-laptop.md` (tasks 0–17); the company deployment gets its own plan after the laptop proof.

> **Team workflow** (docs/TEAM_WORKFLOW.md): Opus = tech lead (briefs, rulings, final review, merge);
> every task implemented by a Claude **Sonnet 5.5** subagent in its own worktree; every branch reviewed by a
> **fresh Sonnet** subagent before the tech lead's final review. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Record per-minute counts on both sites, then build a lab that runs 13 alerting methods + 1 ensemble
on the recorded data and judges them with one protocol. The **company** run is Test B only: what each method
predicts and whether its band fits real traffic (equal coverage, interval score, day-block bootstrap); nothing
is injected or labelled. The **laptop** adds Tests A and C (planted incidents) as a detect gate. An **Evaluation** page where every method draws its band
over the real line, to choose and calibrate a method per monitor kind. Live alerting does not change.

**Spec:** docs/specs/2026-10-26-baseline-bakeoff.md. **Mockup:** https://claude.ai/artifact/EXqQaUkx4aAZTsEmj1A2be

**Tech Stack:** Python 3.11, Flask, psycopg 3, Postgres 16, vanilla JS + ECharts, pytest. Lab image only:
numpy, pandas, statsmodels, prophet, lightgbm, stumpy, torch (CPU), chronos-forecasting.

## Before any code (operations, 2026-10-26)

- [ ] After the retrain build is deployed in the company (this afternoon): **Retrain → Re-read, all services, the last 7 days**. This keeps hourly history back to ~20 Oct before ES drops it.
- [ ] Laptop: start `logai2-engine` (`docker compose up -d`). One **overnight 1× replay** of node97 (00:00–16:07). This gives the templates and the real per-minute shapes that the synthetic world (task 70) learns from. It needs the recorder, so start it the night task 61 is merged.

## Execution

| Id | Task | Phase | Depends on | Runs in parallel with |
|---|---|---|---|---|
| 61 | Migration 010 lab tables + `LabConfig` + **recorder** (live hours and re-read hours) | 1 | — | — |
| 62 | Lab skeleton: loader, kind assignment, method interface, runner, σ method, episodes, run storage | 2 | 61 | — |
| 63 | Core methods #2–#7, #13, E1 (pure Python) | 2 | 62 | 64, 66, 70 |
| 64 | Scoring: shared normal minutes, Test B metrics, equal-budget tuning, sweeps, bootstrap, decision tree | 2 | 62 | 63, 66, 70 |
| 65 | Test A injector + recall/delay scoring (laptop only) | 2 | 64 | 66, 68, 70 |
| 66 | Lab image + heavy methods #8–#12 (`Dockerfile.lab`, baked weights, `--check`) | 2 | 62 | 63, 64, 65 |
| 67 | *(dropped 2026-10-26: no Qwen judge or labels)* | – | – | – |
| 68 | Evaluation API (`eval_view.py`) | 3 | 64 | 65 |
| 69 | Evaluation page (nav "Evaluate", fit chart, scoreboard, calibrate) | 3 | 68 | 70, 71 |
| 70 | Synthetic world: world file from the replay, `synth.py --mode counts|engine` | 2 | 62 | 63–66 |
| 71 | Test C loader, compose lab service, k8s lab Job + read-only role, docs | 4 | 66 | 69 |
| 72 | Proof: laptop synthetic 7/14/28-day runs + replay Test C, timings, validation report | 4 | all | — |

**Tonight:** 61 (deploy as soon as it is merged), then 62. Then 63, 64, 66 and 70 in parallel.

**Per task:**

1. Tech lead writes `docs/tasks/<id>-<slug>.md` from `docs/templates/task-brief.md` just before dispatch (later
   briefs name the interfaces the merged code actually has) and commits it on master.
2. `git worktree add ../logai2-task-<id> -b task/<id>-<slug> master`.
3. Dispatch Sonnet (`Agent`, `model: sonnet`, background): brief path, PR file path, "reply DONE <sha> or
   QUESTION". Answers via `SendMessage`; each answer is a `Ruling:` line in `.team/ledger.md`.
4. On DONE: dispatch a **fresh** Sonnet reviewer (brief + PR file + `git diff master...task/<id>-<slug>`):
   `Spec ✅/❌`, Critical / Important / Minor, `Approved | Needs fixes`. Up to 3 fix rounds with the implementer.
5. Tech lead final review: seams (transactions, units, contracts), full suite on the branch, docs updated, no
   attribution, scope = the brief's files. **Merge into master only after the user confirms the branch.**
   Then `git merge --no-ff` with the PR summary, suite on master, remove the worktree and branch.
6. After the last merge: `graphify update .` in `~/Documents/logai2`; agents.md entry.

## Global Constraints (every brief repeats them)

- Tests: `.venv/bin/pytest -q` against the test Postgres on 127.0.0.1:5433 (`docker compose up -d postgres`).
- Tests first: see them fail, then code. Full suite passes before DONE.
- Stage by explicit path; no AI attribution in commits. Never push, merge, rebase or delete branches.
- Never touch `~/Documents/logai-engine` or its containers. The replayer is only called over HTTP, as `scripts/incident_test.py` does.
- **Live alerting never changes:** lab code never writes `alert_state`, `alert_changes`, `baseline` or the module API. It writes only `lab_*` tables.
- **Company data never leaves the company:** no export of counts, templates or log lines; only aggregate metrics may be exported, and only if the user decides so.
- App image: **no new dependencies** (`requirements.txt` unchanged). Heavy packages go only in `requirements-lab.txt` / `Dockerfile.lab`.
- The cluster has no internet: nothing is downloaded at run time (offline flags; weights baked in).
- Each task updates the docs it changes (`ARCHITECTURE.md`, `docs/WEB_UI.md`, `README.md`, `docs/BUGS.md`).
- Web never talks to ES. Code reads like the code around it.

---

## Phase 1: collect

### 61 Migration 010 + LabConfig + recorder

**Files:** `logai2/migrations/010_lab.sql`, `logai2/config.py` (`LabConfig`: `record`, `days`;
env `LOGAI2_LAB_RECORD`), `logai2/owner/owner.py`, `logai2/jobs/commit.py` (retention), `config.yaml`,
`docker-compose.yml`, `k8s/logai-engine.yaml` (env), tests `tests/test_recorder.py`, `tests/test_db.py`.

- [ ] Migration 010 creates every lab table from the spec at once:
  - `lab_minute_counts(service, key, day, hour, counts int[60])`, partitioned by day like `hourly_counts`;
  - `lab_runs`, `lab_results`, `lab_episodes`, `lab_bands`, `lab_sweeps`, `lab_choices`, `lab_incidents`.
- [ ] **Recorder:**
  - With `lab.record`, the owner keeps the open hour's per-minute counts per key in memory (from `ClosedMinute.keys`). It writes one row per key when the hour closes, **in the same flush transaction** as `hourly_counts`.
  - Keys with all-zero hours are not written.
- [ ] **Restart:** the hour in progress is rebuilt by the normal resume; the cursor is in the same transaction. The test covers a crash mid-hour: there are no duplicate or missing minutes.
- [ ] **Re-read lane:** `_flush_lane` replaces `lab_minute_counts` for each replaced hour, beside `service_hours` / `hourly_counts`, so a 7-day re-read backfills per-minute counts.
- [ ] **Retention:** the commit drops `lab_minute_counts` partitions older than `lab.days` (28).
- [ ] **Tests:**
  - the recorder writes exactly the closed hours, once;
  - nothing is written with the flag off;
  - the lane replaces whole hours;
  - the retention drop;
  - live tables are unchanged.
- [ ] Docs: ARCHITECTURE (the recorder), README (flag, size ~0.3–0.6 GB / 28 d at 3k keys).

## Phase 2: lab core

### 62 Lab skeleton

**Files:** `scripts/lab/__init__.py`, `scripts/lab/load.py`, `scripts/lab/kinds.py`, `scripts/lab/methods/__init__.py`,
`scripts/lab/methods/sigma.py`, `scripts/lab/run.py`, `scripts/lab/store.py`, tests `tests/lab/test_parity.py`,
`tests/lab/test_run.py`.

- [ ] **`load.py`:** read-only reads of `lab_minute_counts`, `hourly_counts`, `service_hours`, `day_status`, `review_exclusions`, templates (levels, first_seen), doc links and `lab_incidents`, for a window. It works with a read-only DB role.
- [ ] **`kinds.py`:** J1 kind assignment (M1–M7) from the tune window. M6 is dynamic by template age.
- [ ] **Method interface:** `fit(history, knob) -> model` and `score(model, key, minute) -> Verdict(high, score, band_low, expected, band_high)`.
  - It also declares: knob grid, kinds served, needs (hourly / per-minute days), `band: bool`, `heavy: bool`.
  - A method lacking history returns "not enough history" (skipped, not failed).
- [ ] **Method #1 σ:** wraps `Baseline` + `judge` (`logai2/owner/baseline.py`), with history rebuilt walk-forward from the loaded hourly counts, as `jobs/commit.py` REBUILD does.
- [ ] **Episodes:** every method's minute verdicts go through `detect.step` (`logai2/owner/detect.py`) with the live `AlertConfig`.
- [ ] **`run.py`:**
  - `--test A|B|C`, `--methods`, `--days`, `--site laptop|company`, `--scenario`, `--check`, `--seed`;
  - `--site company` with `--test A` or `C` exits with an error and writes nothing (test in `tests/lab/test_run.py`);
  - it writes `lab_runs` (code version, image tag, window, seed, knob grids, kinds) and per-method `lab_results`, `lab_episodes`, `lab_bands` (5-min for the drawn keys, 1-min ± 2 h around episodes);
  - it keeps the last 5 runs.
- [ ] **Parity test:** method #1 on recorded minutes reproduces the engine's own `alert_changes` on the same minutes, on a small world made by the stress generator.

### 63 Core methods

**Files:** `scripts/lab/methods/{nb.py,mad.py,ratio.py,cusum.py,weekly.py,pca.py,vote.py}`, `scripts/lab/holidays.py`,
`config.yaml` (VN holiday dates), tests `tests/lab/test_methods.py`.

- [ ] **Methods** (pure Python, stdlib `math` only, so they run in the app image):
  - #2 NB tail (Poisson when var ≤ mean) and #3 NB with outlier clipping at the walk-forward rebuild;
  - #4 median ± k·MAD;
  - #5 error ratio (binomial test vs `*total`);
  - #6 Poisson CUSUM (score + threshold, no band);
  - #7 168-hour weekly profile + holiday calendar;
  - #13 PCA by power iteration on the per-service vector, with top contributing templates;
  - E1 2-of-3 vote (#2, #6, best forecaster when present, else #7).
- [ ] **Per method, one test:** a synthetic spike, drop or ramp is caught, and clean Poisson data stays quiet at the default knob.

### 64 Scoring

**Files:** `scripts/lab/score.py`, `scripts/lab/tune.py`, `scripts/lab/bootstrap.py`, `scripts/lab/decide.py`,
tests `tests/lab/test_score.py`.

- [ ] **J2/J3:**
  - shared normal minutes (one set for all methods; minus Review `exclude` hours, and on the laptop minus planted windows);
  - coverage, Winkler interval score (relative to σ), sharpness, MASE (7-day, or 1-day seasonal naive);
  - alert rate and flap share.
- [ ] **J4:** tune/test split (keys split on a < 14-day window); band methods: the knob whose tune-window coverage is closest to 99.5 %; score-only methods: the most sensitive knob within the alert budget; σ tuned the same way; `lab_sweeps` for every knob value.
- [ ] **J6:**
  - day-block bootstrap (1,000) with 90 % intervals;
  - "fits better than σ" = interval-score ratio ≤ 0.90 and the upper end of its 90 % interval < 1.00;
  - ties → simplest (core before heavy, then cost);
  - verdict strength exploratory / provisional / final.
- [ ] **J7:** decision tree per kind (gates incl. the laptop detect check, rank by interval score, service check) → `lab_results` verdicts. The detect check reads the latest laptop run's Test A recall from `lab_results` when the same database holds it; on the company it is entered from the laptop's metrics-only export (or shown "not checked").
- [ ] **Tests:** hand-made series with known coverage, interval score, flaps and verdicts; the bootstrap is reproducible with a seed.

### 65 Test A injector

**Files:** `scripts/lab/inject.py`, `scripts/lab/score.py` (recall/delay), tests `tests/lab/test_inject_score.py`.

- [ ] **J5 injections:** spike, drop, ramp, level shift, burst (M6), mix swap (M7) at 3 relative sizes.
  - Laptop only. Placed at random times in the test window, never in planted incident windows, and ≥ 3 h apart per key.
  - 5 passes with different seeds; fits are cached between passes.
- [ ] **Recall and delay:** recall (hit = episode start within 15 min) and delay per type × size.
- [ ] **Tests:** an injected window produces exactly the expected hits, false alarms and delay; no overlap with real windows.

### 66 Lab image + heavy methods

**Files:** `requirements-lab.txt`, `Dockerfile.lab`, `scripts/lab/methods/{ets.py,prophet_m.py,lgbm.py,chronos_m.py,mprofile.py}`,
`scripts/lab/check.py`, tests in `tests/lab/test_methods.py` (skipped without the packages).

- [ ] **`Dockerfile.lab`:** `FROM congdock123/logai2:<tag>`, plus `requirements-lab.txt`.
  - Chronos-Bolt-small weights are downloaded **at build time on the laptop** and copied in.
  - `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`.
- [ ] **Methods:**
  - #8 ETS and #9 Prophet (with holidays) on hourly history, walk-forward daily refit;
  - #10 LightGBM global quantile (one model over all keys; features hour, weekday, holiday, lags, key level);
  - #11 Chronos-Bolt quantiles → band;
  - #12 Matrix Profile (score + threshold), limited to M1 + top-200 M3.
- [ ] **`run.py --check`:** loads every package and the weights. A failing method is marked unavailable and the others run.
- [ ] **Image check:** `docker run --network none congdock123/logai2-lab:<tag> python scripts/lab/run.py --check`.

### 70 Synthetic world

**Files:** `scripts/lab/world.py`, `scripts/lab/synth.py`, `lab/scenarios/default.yaml`, tests `tests/lab/test_synth.py`.
It reuses `scripts/stress_pipeline.py` (`GenSource`, `Clock`, `pretrain`, throwaway schema).

- [ ] **`world.py`:** builds `lab/world.json` from the laptop's recorded replay. It holds templates (text, level, doc link), the hourly shape per template (16:07–24:00 mirrored and flagged) and real residual blocks.
- [ ] **`synth.py --mode counts`:** writes `lab_minute_counts`, `hourly_counts`, `service_hours`, `day_status` and `lab_incidents` (source `synthetic`) into a scenario schema. The scenario YAML sets services, scales, subset, phase, day factors, holiday block, trend, residual bootstrap, new templates and the incident catalogue.
- [ ] **`synth.py --mode engine`:** the real Engine on the injected clock against a generated source, with `scale` capping the volume. The recorder and σ detection run as live.
- [ ] **Tests:**
  - the same seed gives the same world;
  - planted incidents show in the counts;
  - day factors and the holiday show in the hourly totals;
  - `engine` mode on a 1-day tiny world gives the same counts as `counts` mode (≥ 99.9 % template match).

## Phase 3: the page

*(Task 67, the Qwen judge, was dropped on 2026-10-26: the company run injects and labels nothing.)*

### 68 Evaluation API

**Files:** `logai2/web/eval_view.py`, `logai2/web/app.py` (routes), tests `tests/test_web_eval_view.py`, `docs/WEB_UI.md`.

- [ ] **Endpoints:**
  - `GET /api/eval/runs`
  - `GET /api/eval/scoreboard?run=`
  - `GET /api/eval/series?run=&service=&key=&from=&to=&methods=&knob=`
  - `GET /api/eval/sweep?run=&kind=`
  - `POST /api/eval/choices`
  - `POST /api/eval/detect`: import a laptop run's metrics-only summary (Test A recall per method × kind) for the detect gate.
- [ ] **Behaviour:**
  - reads only `lab_*` (plus counts for the real line);
  - a company run's scoreboard has no Test A columns;
  - the API token is honoured as on the other routes.

### 69 Evaluation page

**Files:** `logai2/web/static/js/views/evaluation.js`, `logai2/web/static/index.html` (nav section "Evaluate"),
`logai2/web/static/js/main.js`, `logai2/web/static/css/app.css`, `docs/WEB_UI.md`.

- [ ] **Build it as the mockup:** run bar (with site), decision per kind, scoreboard with pass/fail cells, the fit chart as small multiples with a shared zoom (ECharts via `charts.js`), overlay mode, score panels, swimlanes, and Calibrate (sweeps, knob pills redraw the band, Choose).
- [ ] **Add the mockup changes** from the spec: verdict strength, 90 % interval, site, laptop detect column; drop Episodes, Qwen summary and the label columns.
- [ ] **Key picker:** reuse `keypicker.js` (as Baseline does).

## Phase 4: deploy and prove

### 71 Test C, compose, k8s, docs

**Files:** `scripts/lab/load_incidents.py`, `docker-compose.yml` (`logai2-lab`, `profiles: [lab]`), `k8s/logai-lab-job.yaml`,
`k8s/README` or `README.md` (read-only role SQL, Job schedule), `ARCHITECTURE.md`, tests `tests/lab/test_load_incidents.py`.

- [ ] **`load_incidents.py`:** turns a `scripts/incident_test.py` run directory into `lab_incidents` (source `test-c`).
- [ ] **Test C check:** recall difference Test A vs Test C per method (> 10 points → flag the injector).
- [ ] **Compose:** the lab service; `LOGAI2_LAB_RECORD=true` for the engine.
- [ ] **k8s:** a CronJob at 03:00 (after the 02:00 commit) running `run.py --site company --test B`, 4 CPU / 8 Gi, read-only DSN for loading and a lab-writer DSN for `lab_*`.
- [ ] **Docs:** README (both sites, run commands as single short lines), ARCHITECTURE (lab section).

### 72 Proof

**Files:** `docs/validation/2026-11-xx-bakeoff-proof.md`, `agents.md`.

- [ ] **Laptop runs:** synthetic world 7 / 14 / 28 days (`counts`), and one 14-day `engine` run. Full roster, Tests A/B/C.
- [ ] **Record:** run times per method vs the spec's estimates, memory, and the page drawing every method.
- [ ] **Checks:**
  - forecasters learn the planted weekly pattern and holiday;
  - parity holds;
  - the Test A vs Test C gap.
- [ ] **Company smoke:** `run.py --test B --methods 1,2,6 --days 3` in the cluster, and the page draws all three.
- [ ] **Docs:** BUGS.md entries for anything found.

## Dates (company)

| When | What |
|---|---|
| 26 Oct afternoon | retrain deploy + 7-day re-read |
| ~30 Oct | task 61 deployed, 7-day per-minute backfill |
| ~30 Oct | exploratory run |
| ~6 Nov | provisional ranking (14 days) |
| ~20 Nov | final decision run (28 days); the same leader as on ~6 Nov is required for "final" |
