# Task pm3: Owner keeps only active services (B24, B26); Off resets alerts; rebuild keeps baselines

Implementer: Sonnet 5.5 — the core of the build: owner state, selection changes, flush, nightly rebuild.
Reviewer: Opus 5.5 (fresh).
Branch: task/pm3-active-services (worktree ../logai2-task-pm3, created by the tech lead from build/pattern-switch)
PR file: .team/prs/task-pm3-active-services.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
Company 2026-10-28: the engine loaded every service's state at start, also a removed 135k-template service, and was
OOMKilled at each start (B24). A removed pattern's services are still zero-filled and judged: fake zeros in their
history and false `*total` drop alerts (B26). pm2 added the On/Off switch (reading only); this task makes Off really
mean "not monitored": not in memory, not zero-filled, not judged, alerts reset, flagged unmonitored for the web.

## Read first
- Spec `docs/specs/2026-10-29-pattern-switch-memory.md` (all; §2 D2, D4 and §3 are this task).
- Plan `docs/plans/2026-10-29-pattern-switch-memory.md`: "Global Constraints", "Review Focus" (items 1, 2, 5 are
  this task's tests), "Task pm3" (requirements, interfaces, tests).
- Code on this branch: `logai2/owner/owner.py` (all; the module docstring is the exactly-once contract),
  `logai2/owner/baseline.py` (`load(services=)`, `update`, `drop`, from pm1), `logai2/owner/miners.py`,
  `logai2/owner/counts.py`, `logai2/owner/detect.py`, `logai2/engine.py` (`start`, `_select_indices` with the pm3
  comment, `tick`, `_flush`, `_lane_tick`, `_status`), `logai2/jobs/commit.py`, `logai2/metrics.py` (`rss_mib`,
  `trim`), `logai2/migrations/009_index_services.sql`, `tests/test_owner.py` (helpers), `tests/test_commit.py`.
- `.team/prs/task-pm1-compact-baseline.md` and `.team/prs/task-pm5-flush-bites.md` (the Opus reviews: notes for you).

## Files
- Create: `logai2/migrations/012_services_monitored.sql`, `tests/test_owner_selection.py`
- Modify: `logai2/owner/owner.py`, `logai2/owner/miners.py`, `logai2/owner/counts.py`, `logai2/engine.py`,
  `logai2/jobs/commit.py`, `logai2/config.py` (requirement 9 only), `scripts/mem_probe.py` (`--off`), `ARCHITECTURE.md`
- Test: `tests/test_owner_selection.py`, `tests/test_commit.py`, `tests/test_engine.py`
Touching any other file needs a QUESTION first. Do not edit `docs/perf/2026-10-29-memory.md` (probe JSON in the PR file).

## Interfaces
- Consumes (exact, on this branch):
  - `Baseline.load(conn, keys=None, services: list[str] | None = None)` — with `keys=None` it uses a server-side
    cursor: **must run inside a transaction** (the owner already calls it inside `conn.transaction()`).
  - `Baseline.update(other)` replaces keys present in `other` but **does not remove keys missing from it**: to
    reload a service, `drop(service)` first, then `update(...)`. `Baseline.drop(service)`. `Baseline.has(service, key)`.
  - `Engine.enabled`, `Engine.disabled` (sets of pattern strings) set by `_select_indices` before the owner exists.
  - `Owner.close_full`, `Owner._closed` (pm5); `rss_mib()`, `trim()` in `logai2/metrics.py`.
- Produces: exactly the plan's pm3 Interfaces block (`Owner(..., enabled=None, disabled=frozenset())`,
  `Owner.set_selection`, `Owner.active`, `Owner.skipped_off`, `Miners.load`, `Miners.drop`,
  `OpenMinutes.drop_service`).

## Requirements
1. Exactly the plan's pm3 steps and rules (spec §3: Off wins; lazy load of a new service not Off; counts of an Off
   service dropped into `skipped_off`).
2. Order on a switch: `_select_indices` calls `self.owner.set_selection(self.enabled, self.disabled)` at its end
   when the owner exists, so leaving services are unloaded **before the next `poll`/`handle`** closes the jump's
   minutes. In `start()` the owner is built with `enabled=self.enabled, disabled=self.disabled`.
3. `_unload`: also drop the services' pending zero-fill: their entries in `Owner.services` (the first-minute map),
   `minutes.hour_keys` entries and open-minute keys (`OpenMinutes.drop_service`). Pending rows of minutes already
   closed (not yet flushed) are kept: they are real.
4. Alert reset on Off: every state of a leaving service that is not NORMAL or has a streak becomes NORMAL, streak 0,
   with a `Change(service, key, now, from_state, "NORMAL", count, band_low, band_high)` and `touched` so the next
   flush writes `alert_state` and `alert_changes`. Prometheus `export_rows` in `Engine._flush` exports only
   `r.service in self.owner.active`.
5. `services.monitored`: written in the flush transaction when the active set changed (`UPDATE services SET monitored
   = (service = ANY(%s))`); new services inserted with the default true. An Off service's row stays (history).
6. `commit.py` REBUILD per service, exactly as in the plan: monitored services only; result into `tmp_rebuild`; delete
   and insert only the services it produced rows for; `rows` = rows inserted. Off services and services whose hours
   were dropped by retention keep their baseline rows (spec D4).
7. Memory: after `_unload`, call `trim()` and log `"state loaded: %d services, rss %.0f MiB"` (also once in `start()`
   after building the owner).
8. pm5 review follow-ups (engine.py):
   a. `tick` returns true (no poll sleep) while `owner.minutes.next_to_close < self._watermark_ms // 60_000`, so a long
      jump is closed bite after bite without sleeping between bites.
   b. `_lane_tick` pauses also while the owner is still closing bites (same condition as `owner_done` in `tick`).
   c. Status: add `"owner_lag_seconds": max(0.0, now - owner.boundary_ms / 1000)` next to `lag_seconds` (do not
      change `lag_seconds`).
9. `config.py`: `flush.max_closed_minutes` must be >= 1 (raise the config error the file uses for bad values).
10. `ARCHITECTURE.md`: active services and Off rules, alert reset, `services.monitored`, migration 012, the rebuild
    rule; and the pm5 review gaps in §6 tick steps (flush on `close_full`, first catch-up waits for the owner, samples
    for error templates only).
11. Probe: add `--off` to `scripts/mem_probe.py` (the big service's pattern Off at start, then switched On, then Off)
    and report start / on / off RSS; run `--templates 40000 --off` if `free -m` shows >= 4000 MB available (kill above
    4000 MiB), else 20000.
12. Tests run with `/home/cong/Documents/logai2/.venv/bin/pytest` from inside the worktree; first check
    `logai2.__file__` is in the worktree. **No full suite**: only the focused set (plan pm3 Step 4), once at the end.

## Tests (write first, see them fail)
The plan's pm3 Step 1 tests, plus:
- `test_unload_keeps_closed_pending_rows` (requirement 3),
- `test_switch_order_unloads_before_jump` in `tests/test_engine.py` (requirement 2: Off a pattern 3 h behind, tick
  until the owner caught up: no `hourly_counts` / `service_hours` rows for its services after the switch time),
- `test_export_only_active_services` (requirement 4),
- `test_tick_no_sleep_while_owner_behind` and `test_lane_waits_for_owner_bites` (requirement 8).

## Done when
- [ ] new tests fail before the code and pass after
- [ ] the focused set passes, output clean
- [ ] probe `--off` JSON in the PR file
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
