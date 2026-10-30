# Task pm6: Memory proof and docs

Implementer: Sonnet 5.5 — measurements and docs.
Reviewer: none (user ruling 2026-10-29); the tech lead's final review (Opus) re-runs one number of each probe row.
Branch: task/pm6-memory-proof (worktree ../logai2-task-pm6, created by the tech lead from build/pattern-switch)
PR file: .team/prs/task-pm6-memory-proof.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Close the build with numbers and docs: what the memory is now versus before, which bugs are fixed, what is still
open, and how to upgrade the company (which runs v15, migrations 001-009).

## Read first
- Spec and plan (`docs/specs/2026-10-29-pattern-switch-memory.md`, `docs/plans/2026-10-29-pattern-switch-memory.md`).
- `docs/perf/2026-10-29-memory.md`, `scripts/mem_probe.py`.
- PR files with probe numbers and review notes: `/home/cong/Documents/logai2/.team/prs/task-pm0-memory-probe.md`,
  `task-pm1-compact-baseline.md`, `task-pm5-flush-bites.md`, `task-pm3-active-services.md`.
- `/home/cong/Documents/logai2/.team/ledger.md` lines from "Pattern switch + memory" on (rulings and deferred minors).
- **The main checkout's working copy of the bugs list**: `/home/cong/Documents/logai2/docs/BUGS.md` (it has the
  user's uncommitted entries B24, B25 and the lead's B26, which this branch does not have). Read only; do not edit
  anything in `/home/cong/Documents/logai2/`.
- `README.md` (upgrade sections), `ARCHITECTURE.md` (already updated by pm3/pm5: only fix what is wrong).

## Files
- Modify: `docs/BUGS.md`, `docs/perf/2026-10-29-memory.md`, `README.md`, `ARCHITECTURE.md` (corrections only),
  `docs/specs/2026-10-29-pattern-switch-memory.md` and `docs/plans/2026-10-29-pattern-switch-memory.md` (status
  lines only), `scripts/mem_probe.py` (requirement 2 only), `tests/test_mem_probe.py` (requirement 2 only)
- NOT `docs/WEB_UI.md` (task pm4 edits it in parallel).
Touching any other file needs a QUESTION first.

## Requirements
1. Probe, one size at a time in the foreground, `free -m` before each (skip a size if available < 3000 MB), kill
   above 4000 MiB RSS. Another agent (pm4) runs tests in parallel; that is fine.
   - `--templates 20000 --minutes 120`, `--templates 40000 --minutes 120`, `--templates 80000 --minutes 1`,
     and `--templates 40000 --off`.
   Write into `docs/perf/2026-10-29-memory.md` an "after (build/pattern-switch <sha>)" section next to the "before"
   rows, with the pm1/pm5/pm3 rows from their PR files, a before/after table per size, and the 135k extrapolation
   redone from the after numbers (labeled "extrapolated, not measured"). Say plainly what each number includes
   (`flush_peak_mib` is the whole-run peak; holding M open minutes is not reduced by the bites).
2. pm0 review minors: docstring/perf wording ("peak through flush", steady is after `trim()`); `test_mem_probe`
   asserts `flush_peak_mib >= load_peak_mib`.
3. `docs/BUGS.md` on this branch: start from the main checkout's working copy (so B24, B25, B26 are present), then:
   - move B24, B25, B26 to **Fixed** with the merge commits on `build/pattern-switch` (`git log --merges`):
     B24 -> pm1 + pm3, B25 -> pm5, B26 -> pm3; one short "Fix:" line each;
   - add the open findings of this build (each with Where / What / Fix idea / Likelihood, short):
     a Drain3 `save_state` serialises the whole tree on every flush that learns a template (2.3 s at 80k, est. 4-5 s
       at 135k; a fix must never let a saved tree lag the cursors: cluster ids would be reused after a crash);
     b a restart within the lag after a switch On can zero-fill from the boundary up to `enabled_at`
       (rejected fix: leaving the first minute unset at start would stop zero-filling silent services after every
       restart);
     c while the owner is behind (bites), open minutes still hold every count of the not-yet-closed minutes
       (bounded only by the lag); the Pipeline `lag_seconds` is the reader's (`owner_lag_seconds` is in the status);
     d deferred minors from the ledger worth keeping (levels of left pending templates never removed; Off lines
       never learned by the reader cache; open samples decided by the level at arrival). One entry for d is enough.
4. `README.md`: an upgrade note: from v15 the engine applies migrations 010, 011, 012 at start; every saved pattern
   stays On; how to switch a pattern Off (Data sources) and what it does; watch `process_resident_memory_bytes`.
   Single short lines for any command (no JSON blocks).
5. Spec and plan: status line "done" with the build branch sha.
6. Tests: only `tests/test_mem_probe.py` (from inside the worktree, `logai2.__file__` check first).

## Done when
- [ ] probe rows and before/after table in the perf doc
- [ ] BUGS, README, status lines updated
- [ ] `tests/test_mem_probe.py` passes
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
