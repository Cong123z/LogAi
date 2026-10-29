# Task pm2: On/Off switch per pattern (selection API, reading, Data sources UI)

Implementer: Sonnet 5.5 — selection contract, the targets string and a UI toggle; bounded but touches the engine's reading.
Reviewer: Sonnet 5.5 (fresh).
Branch: task/pm2-pattern-switch (worktree ../logai2-task-pm2, created by the tech lead)
PR file: .team/prs/task-pm2-pattern-switch.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Users must be able to stop monitoring a pattern (e.g. a huge backfill service) without losing its history, and turn it
on again later. This task adds the switch and makes the engine stop reading an Off pattern. Unloading its services'
state, hiding them in the web and resetting their alerts are later tasks (pm3, pm4): do not do them here.

## Read first
- Spec `docs/specs/2026-10-29-pattern-switch-memory.md`: §2 decisions D1, D3; §3 rules.
- Plan `docs/plans/2026-10-29-pattern-switch-memory.md`: "Global Constraints", "Review Focus" items 3-4, "Task pm2".
- Code: `logai2/web/app.py` (`selection_entries`, `put_selection`, `/api/sources`), `logai2/engine.py`
  (`_select_indices`, `_refresh`, `start`), `logai2/reader/reader.py` (`set_sources`, `cursors`),
  `logai2/owner/owner.py` (`flush`: the `DELETE FROM es_cursors WHERE index <> ALL` line),
  `logai2/web/static/js/views/sources.js`, `tests/test_web_sources.py`, `tests/test_engine.py`, `tests/fakes.py`
  (`FakeSource._match` understands `,-pattern` exclusions).
- `docs/WEB_UI.md` (Data sources section): update it for the switch and the `disabled` field.

## Files
- Modify: `logai2/web/app.py`, `logai2/engine.py`, `logai2/web/static/js/views/sources.js`, `docs/WEB_UI.md`
- Test: `tests/test_web_sources.py`, `tests/test_engine.py`
Touching any other file (CSS included) needs a QUESTION first.

## Interfaces
As in the plan's pm2 Interfaces block (entry shape, `PUT` body with `disabled`, the 400 text
`"disabled must name selected patterns"`, `Engine.enabled` / `Engine.disabled` as sets of pattern strings).
pm3 will call `self.owner.set_selection(self.enabled, self.disabled)` from `_select_indices`; leave a clear place for
it but do not add the call.

## Requirements
1. Exactly the plan's pm2 steps.
2. Targets: an On pattern excludes the earlier On patterns (as today) **and every Off pattern, whatever its position**.
   An Off pattern gets no floor, no position, no cursor. A removed pattern excludes nothing.
3. Floor of an On pattern: today's rule (`bootstrap.since` if `added_at <= bootstrap.until`, else `added_at`), then
   `max(that, enabled_at)` when `enabled_at` is set. Times in the setting are epoch seconds; floors are ms.
4. A saved selection without `enabled` / `enabled_at` reads as On / None (upgrade; no migration of the setting).
5. Reloading: the switch takes effect at the next `reload_sources` command (the web enqueues it on save) or index
   refresh; nothing else.
6. UI: a switch column "Monitoring" per selected row; Off rows show the badge text
   `Off: not read, hidden everywhere, history kept`; the switch is part of the draft (Save / Discard, dirty check);
   Remove keeps working. Keep the page's existing style and escaping (`escapeHtml`).
7. Tests run with `/home/cong/Documents/logai2/.venv/bin/pytest` from inside the worktree (no venv there). First check
   `…/.venv/bin/python -c "import logai2; print(logai2.__file__)"` from the worktree prints a worktree path; if not,
   QUESTION.
8. The tests in the plan are sketches: write them in the style of the files they go in (their helpers, fixtures).

## Tests (write first, see them fail)
The plan's pm2 Step 1 tests: `test_selection_disable_and_reenable`, `test_selection_disabled_must_be_selected`,
`test_selection_without_enabled_is_on`, `test_disabled_pattern_not_read_and_excluded`,
`test_reenabled_pattern_reads_from_enabled_at`. Add one: an Off pattern listed **after** an On pattern that matches
the same index is still excluded from it (`test_off_pattern_excluded_whatever_its_order`).

## Done when
- [ ] new tests fail before the code and pass after
- [ ] `.venv/bin/pytest -q` (full suite) passes, output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in (say how you checked the UI: you cannot use a browser; describe the rendered rows from the JS)
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
