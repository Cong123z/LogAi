# Task pm0: Process-memory metric and the synthetic big-service probe

Implementer: Sonnet 5.5 — a script and one metric; needs care with Postgres seeding and a child process for clean RSS.
Reviewer: Sonnet 5.5 (fresh).
Branch: task/pm0-memory-probe (worktree ../logai2-task-pm0, created by the tech lead)
PR file: .team/prs/task-pm0-memory-probe.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
The company engine pod sits at ~4,400 MiB and was OOMKilled at every start (B24). Before fixing memory we need
numbers: a process-memory metric for production and a probe that rebuilds a vas_066-sized service on the laptop and
measures start, flush and Drain3 save costs. Every later task (pm1, pm3, pm5, pm6) reruns this probe.

## Read first
- Spec `docs/specs/2026-10-29-pattern-switch-memory.md` (all, it is short).
- Plan `docs/plans/2026-10-29-pattern-switch-memory.md`: "Global Constraints" and "Task pm0" (the requirements,
  interfaces and tests are there; this brief only adds what the plan does not say).
- Code: `logai2/metrics.py`, `logai2/owner/owner.py` (`Owner.__init__`, `handle`, `flush`), `logai2/owner/miners.py`,
  `logai2/owner/baseline.py`, `logai2/db.py` (`migrate`, `ensure_partitions`), `logai2/migrations/001_init.sql`
  (tables you seed), `tests/conftest.py`, `tests/test_owner.py` (how an Owner is driven).

## Files
- Modify: `logai2/metrics.py`
- Create: `scripts/mem_probe.py`, `tests/test_mem_probe.py`, `docs/perf/2026-10-29-memory.md`
- Modify: `tests/test_metrics.py`
Touching any other file needs a QUESTION first. (`logai2/engine.py` is NOT in this task: the RSS log line moved to pm3.)

## Interfaces
As in the plan's pm0 Interfaces block: `rss_mib() -> float`, `trim() -> None` in `logai2/metrics.py`;
`scripts/mem_probe.py --dsn DSN --templates N [--minutes M] [--keep]` printing one JSON line (last line of stdout)
with keys `templates, baseline_rows, blob_bytes, load_peak_mib, load_steady_mib, save_state_ms, flush_peak_mib`.

## Requirements
1. Exactly the plan's pm0 steps 1-5.
2. The probe creates and uses its own schema (`probe_<8 hex>`) on the given DSN, `public` second on the search_path
   (pgvector), and drops it at the end unless `--keep`. It never touches any other schema.
3. Seed with COPY or executemany in chunks; 135k templates x 48 baseline rows (6.48M) must seed in minutes, not hours.
4. Measure the Owner load in a child process (`multiprocessing` spawn or `subprocess`), so the parent's seeding memory
   does not count. `load_peak_mib` from `resource.getrusage(RUSAGE_SELF).ru_maxrss / 1024` in the child.
5. The flush measurement holds the watermark (positions not caught up) for M minutes of counts, then one batch moves it
   past all M, then one `flush`. Report the child's peak RSS after it (`flush_peak_mib`). This is the B25 shape; on
   master it is expected to be large.
6. Tests run with `/home/cong/Documents/logai2/.venv/bin/pytest` from inside the worktree (there is no venv in the
   worktree). First check `…/.venv/bin/python -c "import logai2; print(logai2.__file__)"` run from the worktree prints
   a path inside the worktree; if it does not, stop with a QUESTION.
7. Step 5 (the 135k run): if it needs more than ~8 GB RAM or 30 min, stop it, record the largest size that finished, and
   say so in the PR file. Do not start or stop containers.

## Tests (write first, see them fail)
The plan's pm0 Step 1 tests: `test_process_memory_exported`, `test_rss_mib_positive`, `test_mem_probe_small`.

## Done when
- [ ] new tests fail before the code and pass after
- [ ] `.venv/bin/pytest -q` (full suite) passes, output clean
- [ ] "before" numbers in `docs/perf/2026-10-29-memory.md` with the master commit hash
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in (with the probe JSON lines)
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
