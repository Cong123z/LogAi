# Task 52: `templates.first_seen` moves earlier; `days_back` doc

Implementer: Sonnet 5.5 — small SQL fix in the flush transaction plus a doc line.
Reviewer: a fresh Sonnet 5.5 subagent
Branch: task/52-first-seen-days-back (worktree ../logai2-task-52, created by the tech lead)
PR file: .team/prs/task-52-first-seen-days-back.md (from docs/templates/pr.md; .team/ is in the main checkout
~/Documents/logai2, write it there)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below
is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
The templates upsert never moves `first_seen` earlier: when a template's row exists and a later flush (or the
coming re-read, task 57) brings an older minute for it, the Templates page keeps showing the later time.
Separately, ARCHITECTURE §10.4 says `days_back` is 2 while `logai2/config.py` defaults it to 7.

## Read first
- Spec: docs/specs/2026-10-26-retrain.md §7 (row `templates.first_seen`), §8 items 2-3.
- Code: `logai2/owner/owner.py` `SQL["templates"]` and how `flush` builds `new_t`; `logai2/config.py`
  (`days_back`); `tests/test_owner.py` (helpers `make`, `rows`).

## Files
- Modify: `logai2/owner/owner.py`, `ARCHITECTURE.md` (§10.4 only, and any other line stating `days_back` 2)
- Test: `tests/test_owner.py`
Touching any other file needs a QUESTION first.

## Interfaces
- Consumes/produces: `SQL["templates"]` keeps its parameters and order.

## Requirements
1. In `SQL["templates"]`'s `ON CONFLICT ... DO UPDATE SET`, add
   `first_seen = LEAST(templates.first_seen, EXCLUDED.first_seen)`. Nothing else in the statement changes.
2. ARCHITECTURE: every statement of the `days_back` default says 7 (check with `grep -n days_back`).

## Tests (write first, see them fail)
- `test_template_first_seen_moves_earlier`: insert a template through a flush at minute M, then flush the same
  (service, template_id) with first minute M - 60 → `templates.first_seen` is M - 60's time; a third flush
  with a later first minute leaves it unchanged.

## Done when
- [ ] new test fails before the code and passes after
- [ ] `.venv/bin/pytest -q` passes, output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
