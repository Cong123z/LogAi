# Task bk12: Synthetic world, `engine` mode

Implementer: Sonnet 5.5 — drives the real Engine on an injected clock; proves the counts-mode shortcut matches the pipeline.
Reviewer: Opus 5.5 (fresh).
Branch: task/bk12-synth-engine (worktree ../logai2-task-bk12, created by the tech lead)
PR file: .team/prs/task-bk12-synth-engine.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
`--mode counts` (bk06) writes the tables directly, as the engine would have. `--mode engine` produces the same scenario
by sending log lines through the real pipeline (reader → Drain3 → counts → recorder → σ detection) on a simulated clock.
It proves the shortcut is faithful and gives realistic live `alert_changes` for parity checks.

## Read first
- Spec: `docs/specs/2026-10-27-bakeoff-laptop.md` §7.5, §6.4 (parity rule and known differences).
- Code: `scripts/lab/synth.py` (`generate`, `Generated`, `Svc`, `write`, `main`, the rate model and incidents),
  `scripts/lab/world.py`, `scripts/stress_pipeline.py` (`Stream`, `GenSource`, `Clock`, `pretrain`, `setup`, `Run`,
  `drive`, how a throwaway schema is made and the Engine built), `tests/lab/test_lab_parity.py` (bk04: a working
  engine-driven world on the harness; reuse its approach), `logai2/engine.py`, `logai2/reader/es_source.py`
  (`hit_to_line`: the hit shape), `logai2/db.py` (`migrate` now uses `current_schema()`).

## Files
- Modify: `scripts/lab/synth.py`, `tests/lab/test_lab_synth.py`, `README.md` (one line for `--mode engine`)
Touching any other file needs a QUESTION first (in particular `scripts/stress_pipeline.py` and `logai2/`; import from
them, do not change them).

## Interfaces
- `python -m scripts.lab.synth --mode engine --scenario <yaml> [--world lab/world.json] [--schema lab_s_<name>_engine]
  [--dsn DSN] [--end YYYY-MM-DD] [--scale 1.0]`.
- `run_engine(world, scenario, end, schema, dsn, scale=1.0) -> dict` (summary: minutes, events, seconds, alert changes).

## Requirements
1. The same `generate(world, scenario, end)` result drives both modes: engine mode turns each template's per-minute
   count into that many log lines with timestamps spread inside the minute (seeded), the template's text with its `<*>`
   wildcards filled by seeded tokens, its level, service and a module code; `*total`/level keys are not emitted (the
   engine derives them).
2. The engine runs in the scenario schema (fresh, migrated), with templates **pretrained** in Drain3 like
   `stress_pipeline.pretrain` so ids are stable, `lab.record = True`, detection enabled after the first catch-up as the
   engine does, its own 02:00 commits, on an injected clock (`Clock`) driven as `test_lab_parity.py` drives it.
   Before the first day, seed `hourly_counts`/`service_hours`/`day_status` history for the prior days from the
   counts-mode generator (so σ has a baseline), exactly as `write` does for those days.
3. `--scale` multiplies every λ before drawing (to cap events/s on long runs); it is recorded in the summary.
4. Gaps in the scenario: no events and the engine's minutes are still closed (observed, count 0) — engine mode cannot
   produce unobserved minutes; document this difference (counts mode marks gaps unobserved).
5. Incidents are written to `lab_incidents` of the schema exactly as counts mode does.
6. Print the summary at the end (minutes simulated, events, wall time, alert changes).

## Tests (write first, see them fail)
1. `test_engine_mode_matches_counts_mode`: `tiny.yaml`-like world (1 service, 5 templates, 1 day after 2 seeded history
   days), seed fixed: run counts mode into schema A and engine mode into schema B; map template ids by text; for
   every (key, day, hour) compare `lab_minute_counts`: ≥ 99.9 % of (key, minute) cells equal, and `*total` equal on
   every minute. Report the mismatch count in the PR. Must finish in < 3 min.
2. `test_engine_mode_writes_incidents_and_alerts`: a planted spike produces `alert_changes` rows in schema B and its
   `lab_incidents` row.
3. Both schemas are dropped in teardown; `public` untouched.

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes, output clean (report the count)
- [ ] one `--mode engine --scenario lab/scenarios/tiny.yaml` run against the live DB into `lab_s_tiny_engine` (pass
      `--dsn postgresql://logai:logai@127.0.0.1:5433/logai`): report its summary in the PR file; never write to `public`
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
