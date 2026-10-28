# Task bk17: Laptop proof

Implementer: Sonnet 5.5. Reviewer: the tech lead.
Branch: task/bk17-laptop-proof (worktree ../logai2-task-bk17, created by the tech lead)
PR file: .team/prs/task-bk17-laptop-proof.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Every piece is merged. This task runs the whole lab end to end on the laptop and writes down what it shows, before
anything goes to the company: does each method run, in what time and memory, does the decision logic produce sane
verdicts, do the forecasters learn the planted patterns, and is each planted incident caught by the method meant for it.

## Read first
Plan `docs/plans/2026-10-27-bakeoff-laptop.md` Task 17; spec `docs/specs/2026-10-27-bakeoff-laptop.md` §2, §6.4, §8,
§13 (cost estimates); README "Lab" commands; `lab/scenarios/default.yaml`, `tiny.yaml`; ARCHITECTURE §18.

## Environment rules (strict)
- DB: the live Postgres DB `logai` on 127.0.0.1:5433 **only for new `lab_s_*` schemas** (`--schema` and
  `--results-schema` always a `lab_s_*` name; never write into `public`). Unit tests use `logai_test` as usual.
- Memory: about 2 GB free. Before any run with heavy methods (ets, prophet, lgbm, chronos, mprofile) you may
  `docker stop elasticsearch logai2-web`; start them again (`docker start elasticsearch logai2-web`) when done, and
  before you reply. Never stop/restart `logai2-engine` or `logai2-postgres-1`; never start linker/bge-m3.
- Heavy methods run only in the lab container: `docker compose --profile lab run --rm logai2-lab <args>` (add
  `--dsn`/schemas pointing at `postgres:5432` inside the network). Core methods may run in the venv.
- One run at a time. Measure wall time and peak memory per run (`/usr/bin/time -v` in the venv;
  `docker stats --no-stream` sampling or the container's cgroup `memory.peak` for the lab container).
- Real data (Test B/C on recorded counts) is **out of scope here**: recording only started today. Do not run
  `incident_test.py`.

## Steps
1. **Worlds**: build synthetic counts worlds of 7, 14 and 28 days from `default.yaml` (copies with `days` changed,
   kept under `lab/scenarios/`), each in its own `lab_s_*` schema.
2. **Runs** per world: Test A, then Test B, `--methods all` (core in the venv is fine for a quick first pass; the
   recorded numbers must come from the full roster in the lab container). Time + peak memory per run; per-method
   time from the run's own logs/`lab_results` if available. Compare with the spec §13 estimates.
3. **Engine mode**: one 14-day `--mode engine` world (`tiny.yaml` scaled to 14 days is fine); Test B core; confirm the
   parity rule of task 4 (σ lab bands vs engine bands) holds on it — report mismatches.
4. **Checks** (on the 28-day world), each with a number:
   - weekly/ets/prophet beat σ's interval score on the holiday days and on weekends (the planted pattern);
   - each planted incident type is caught (Test A recall at medium size, chosen knob) by at least the method designed
     for it (spike → nb/mad, drop → σ/nb, ramp/level_shift → cusum/ets, burst → M6 rule, mix_swap → pca);
   - the decision rows per kind are sane (no NaN, a verdict per kind M1–M7, M6/M7 not pending after Test A);
   - the Evaluation page API (`/api/eval/scoreboard`, `keys`, `series`, `sweep`) answers for the 28-day run (serve
     `scripts/run_web.py` on port 5598 with the DSN `search_path` set to the run's schema; stop it when done).
5. **Known items to investigate** and report: (a) an earlier Test A run on a seeded `mid` world wrote no results —
   reproduce on the 7-day world and find why; (b) mprofile scores 0 when a key has fewer than 2 prior days
   (`model.dist` built from `hist[1440:]`, review note) — say what it does in the 7-day run; (c) lgbm time at the
   key count of the 28-day world, extrapolated to 3k keys; (d) prophet tail coverage (BUGS B-entry).
6. **Fixes**: a bug that blocks a run → smallest fix with a failing test first, on this branch; anything else →
   a `docs/BUGS.md` entry. Code fixes beyond a few lines need a `QUESTION` first.
7. **Report**: `docs/validation/2026-10-28-bakeoff-laptop-proof.md`: setup, a table per world (method × test: status,
   wall time, peak memory), the checks with their numbers, the decisions per kind, investigations (a)–(d), bugs found,
   what is not covered (real data, company), and a short "ready for the company plan?" verdict.

## Done when
- [ ] report written with real measured numbers (no estimates presented as measurements)
- [ ] `.venv/bin/pytest -q` passes if any code changed (report the count)
- [ ] elasticsearch and logai2-web running again; no web server left on 5598; your `lab_s_*` schemas listed in the
      PR file (keep them, the user may look at the page)
- [ ] commits on the branch, explicit-path staging, no attribution lines; PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus deviations
