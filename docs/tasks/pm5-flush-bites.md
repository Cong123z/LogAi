# Task pm5: Close minutes in bites, stop hoarding while lagging, lag warning

Implementer: Sonnet 5.5 — the exactly-once path (owner, reader, engine flush).
Reviewer: Opus 5.5 (fresh) + an independent Opus adversary — the cap, the cursor boundary and forgetting ids can
double or lose counts.
Branch: task/pm5-flush-bites (worktree ../logai2-task-pm5, created by the tech lead from build/pattern-switch)
PR file: .team/prs/task-pm5-flush-bites.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Company 2026-10-28: removing a pattern 22 h behind moved the watermark 22 h at once; ~1,300 minutes closed in one
flush that never finished (B25). While a pattern lags, `Reader._seen` keeps every doc id since the boundary and
`Owner._open_samples` keeps one sample line per aggregate per poll: memory grows with the lag.

## Read first
- Spec `docs/specs/2026-10-29-pattern-switch-memory.md` §1.3, §1.4, §3 (last two bullets).
- Plan `docs/plans/2026-10-29-pattern-switch-memory.md`: "Global Constraints", "Review Focus", "Task pm5".
- Code: `logai2/owner/owner.py` (module docstring: the exactly-once contract; `handle`, `_clear`, `flush`,
  `boundary_ms`), `logai2/owner/counts.py` (`close_before`), `logai2/reader/reader.py` (all; the `forget_before`
  comment and `poll`'s stall guard), `logai2/engine.py` (`tick`, `_flush`, `_status`, `_caught_up`, `_lane_tick`,
  `_commit_due`), `logai2/config.py`, `tests/test_owner.py`, `tests/test_reader.py`, `tests/test_engine.py`.
- `ARCHITECTURE.md` sections on the reader, the owner and the flush.

## Files
- Modify: `logai2/config.py`, `logai2/owner/owner.py`, `logai2/reader/reader.py`, `logai2/engine.py`, `ARCHITECTURE.md`
- Test: `tests/test_owner.py`, `tests/test_reader.py`, `tests/test_engine.py`; update the `forget_before` callers in
  `tests/test_reread.py`, `tests/test_recorder_adversarial.py`
Touching any other file needs a QUESTION first. Do not edit `docs/perf/2026-10-29-memory.md` (probe JSON goes in the PR file).

## Interfaces
Exactly the plan's pm5 Interfaces block: `Owner.close_full`, `Reader.forget_read()`, config
`flush.max_closed_minutes: 60`, `reader.lag_warn_seconds: 600`, the warning text.

## Requirements
1. Exactly the plan's pm5 steps.
2. Crash safety must hold: the saved cursor is still `owner.boundary_ms` (the start of the oldest open minute), so a
   restart between two bites re-reads every not-yet-closed minute once. Add
   `test_crash_between_bites_counts_exact`: close 60 of 300 minutes, flush, rebuild Reader/Owner from the database (as
   `make()` does), run to the end: every minute's `*total` equals its docs, no minute twice.
3. A failed flush between bites keeps its pending rows (today's contract); the next handle must not close more
   minutes while `close_full` is true. Add `test_failed_flush_keeps_cap`.
4. Detection: the minutes closed per bite are judged as today (`historic` decides alerts); the first-start commit and
   `_commit_due` use the owner's day, which now may lag the reader by some bites: check that nothing commits an open
   day (say in the PR file how you checked).
5. `forget_read`: if you find any path where a doc with `ts < position` can be read again (a rewind, a re-read of an
   older range, the lane), stop with a QUESTION and name it.
6. Tests: `/home/cong/Documents/logai2/.venv/bin/pytest` from inside the worktree; first check `logai2.__file__` is in
   the worktree.

## Tests (write first, see them fail)
The plan's pm5 tests plus `test_crash_between_bites_counts_exact` and `test_failed_flush_keeps_cap`.

## Done when
- [ ] new tests fail before the code and pass after
- [ ] `.venv/bin/pytest -q` (full suite) passes, output clean
- [ ] probe run (`--templates 135000 --minutes 1300`) JSON in the PR file
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
