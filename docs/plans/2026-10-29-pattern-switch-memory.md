# Pattern On/Off switch and engine memory: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Off patterns cost no engine memory and are hidden everywhere while their history stays; no flush or start
can be blown up by one huge service or one big watermark jump.

**Architecture:** The owner keeps state only for *active* services (On patterns, Off wins) and loads / unloads them
when the selection changes. The baseline moves to one packed array per (service, key), streamed from Postgres.
Minutes close in bounded bites. `services.monitored` is the one flag the web and the linker filter on.

**Tech Stack:** Python 3, psycopg 3, Postgres (partitioned tables), Flask, vanilla JS, Drain3, prometheus_client.

**Spec:** `docs/specs/2026-10-29-pattern-switch-memory.md` (decisions D1-D7, rules §3).

## Global Constraints

- Tests: `.venv/bin/pytest -q <files>` against the test Postgres on 127.0.0.1:5433 (`docker compose up -d postgres`).
- **Test budget (user, 2026-10-29: "run test less"):** implementers and reviewers run only the task's
  **focused set** (listed per task below), never the full suite. The tech lead runs the full suite **twice in the
  build**: on `build/pattern-switch` after wave 2 (pm1 + pm5 merged) and once before the final merge into master.
  A failure there goes back to the task that owns the file.
- One branch + worktree per task (`task/pm<N>-<slug>`); merge into `master` with `--no-ff` only after the user
  confirms that branch. Never push. Stage by explicit path; no AI attribution in commits.
- New config values, exactly: `flush.max_closed_minutes: 60`, `reader.lag_warn_seconds: 600`.
- New migration: `logai2/migrations/012_services_monitored.sql`.
- Off is sticky and Off wins over any On pair (spec D1, §3). Data is never deleted by an Off switch.
- Off services: web 404 on any per-service route, absent from every list (spec D5, D6).
- Match the surrounding code: short comments, `ponytail:` notes for known ceilings, no new dependencies.
- Each task adds its entry to `agents.md` and updates docs it makes wrong (ARCHITECTURE.md, docs/WEB_UI.md,
  docs/BUGS.md: move fixed items to **Fixed** with the commit).

## Review Focus

1. **Off while the pattern is the slowest one** (the incident): the watermark jumps hours; expect no zeros written
   for the Off services, minutes closed 60 per flush, counts of the other patterns exact. Test: pm3 Step 1,
   `test_disable_slowest_pattern_jump_writes_no_zeros`.
2. **A service in an On and an Off pattern:** expect it Off (not loaded, not shown), its docs reaching the engine
   through the On pattern dropped and counted in `skipped_off`. Test: pm3 `test_off_wins_over_on_pair`.
3. **Restart with a pattern Off:** expect its services not loaded at start and its pattern not read, the cursor
   row gone, and On again reading from `enabled_at`, not from the old cursor. Test: pm2
   `test_reenabled_pattern_reads_from_enabled_at`.
4. **Old saved selection without `enabled`:** expect every entry On (upgrade). Test: pm2
   `test_selection_without_enabled_is_on`.
5. **Rebuild after 90 days Off** (service_hours dropped by retention): expect the old baseline rows kept. Test: pm3
   `test_rebuild_keeps_unmonitored_and_dataless_services`.

---

## Agent roles

Team workflow (`docs/TEAM_WORKFLOW.md`). Only Opus and Sonnet (user ruling 2026-10-27: no Gemini / `agy`).
Nothing is dispatched until the user says start.

| Role | Who | Does | Never does |
|---|---|---|---|
| Tech lead | Claude Opus 5.5 (this session) | Writes each brief `docs/tasks/pm<N>-<slug>.md` just before dispatch (with the interfaces merged code really has); creates the branch + worktree; answers `QUESTION:`; assigns reviewers; final review (seams, focused set, docs); runs the two full-suite checkpoints; adds the probe rows to `docs/perf/2026-10-29-memory.md`; merges each approved task into `build/pattern-switch`; master gets **one merge at the end, after the user confirms** (user, 2026-10-29); keeps `.team/ledger.md` | Feature code (only tiny review fixes, and says so) |
| Implementer | Claude Sonnet 5.5 subagent (`model: sonnet`), one fresh agent per task | Tests first (seen failing), code, the focused set, probe run when the task says so, PR file `.team/prs/task-pm<N>-<slug>.md`; stops with `QUESTION:` when unsure | Merges; reviews its own branch; touches files outside its brief |
| Reviewer O | Claude Opus 5.5 subagent (`model: opus`), fresh | Reviews the tasks where a mistake silently loses or doubles counts, or wrongly silences alerts (pm1, pm3, pm5): another model than the implementer, so a shared blind spot is less likely | Rewrites the feature |
| Reviewer S | Claude Sonnet 5.5 subagent, fresh | Reviewed pm0 and pm2 (before the 2026-10-29 ruling); not used after | Rewrites the feature |
| User | - | Confirms each branch before merge; checks the Data sources switch and the hidden service in Chrome (pm2, pm4); does the company rollout | - |

A review returns `Spec ✅/❌`, Critical / Important / Minor, `Approved / Needs fixes`. Up to 3 fix rounds by the same
implementer, then the tech lead rules. Re-review only after a Critical or Important fix. Minors go to the ledger.

### Per task

**Review ruling (user, 2026-10-29: "less review"):** one fresh Opus reviewer only on the important tasks (pm1, pm3,
pm5); no adversaries; no separate review on the bounded tasks (pm4, pm6): the tech lead's final review (Opus) is the
only gate there. pm0 and pm2 were already Sonnet-reviewed before this ruling.

| Task | Implementer | Reviewer | Why | Tech lead checks before merge |
|---|---|---|---|---|
| pm0 probe + metric | Sonnet | Sonnet (done) | Script and one metric line | Done |
| pm1 compact baseline | Sonnet | **Opus** | A wrong slot or NaN rule gives wrong bands everywhere, silently | Old dict vs new layout give the same `lookup` for every row of the laptop's live baseline |
| pm5 bites, forget, samples | Sonnet | **Opus** | Exactly-once path: the cap, the cursor boundary and forgetting ids can double or lose counts | Kill the engine between two bites and restart: counts exact (run myself) |
| pm2 On/Off switch | Sonnet | Sonnet (done) | Bounded contract | Done |
| pm3 active services | Sonnet | **Opus** | The core: Off wins, alert reset, open minutes dropped, rebuild delete rule, lazy load | Replay the incident (Off the slowest pattern 3 h behind) on the laptop: no zeros, alerts reset, RSS drops |
| pm4 hide everywhere | Sonnet | Tech lead only | Many small, similar query edits | Grep every `FROM services` / `FROM templates` / `FROM alert_state` in web and linker: each filtered or justified; the user clicks through in Chrome |
| pm6 proof + docs | Sonnet | Tech lead only | Measurements and docs | Re-run one number of each probe row |

### Waves (parallel only when the file lists are disjoint; at most 3 agents at once)

| Wave | Tasks | Why together |
|---|---|---|
| 1 | pm0, pm2 | Disjoint files (metrics + script / web + UI + `_select_indices`) |
| 2 | pm1, pm5 | Disjoint files (baseline + baseline_view / owner + reader + engine + config); both need pm0's probe |
| 3 | pm3 | Needs pm1, pm2, pm5 merged; touches owner, engine, commit |
| 4 | pm4 | Needs `services.monitored` from pm3 |
| 5 | pm6 | Everything merged |

Merge order inside a wave: the first to pass review; the second rebases onto master in its worktree (by its
implementer, on the tech lead's request) and reruns its focused set before its final review.

### Cost

7 Sonnet implementers, 2 Sonnet reviews (done), 3 Opus reviews (pm1, pm3, pm5), the tech lead's final review on
every task, fix rounds only after a Critical or Important finding.

---

## Order and dependencies

| Task | Branch | Needs | Fixes |
|---|---|---|---|
| pm0 | `task/pm0-memory-probe` | - | measurement (D7) |
| pm1 | `task/pm1-compact-baseline` | pm0 | §1.5, start peak |
| pm5 | `task/pm5-flush-bites` | pm0 | B25, §1.4 |
| pm2 | `task/pm2-pattern-switch` | - | D1, D3 (selection + reading) |
| pm3 | `task/pm3-active-services` | pm1, pm2, pm5 | B24, B26, D2, D4 |
| pm4 | `task/pm4-hide-off-services` | pm3 | D5, D6 |
| pm6 | `task/pm6-memory-proof` | all | proof + docs |

pm2 does not need pm0 and runs beside it (wave 1); pm1 and pm5 run together after pm0 (wave 2); pm3 touches
`owner.py` after pm5 and starts from master with pm1, pm2 and pm5 merged. See "Agent roles" for the waves.

---

### Task pm0: Process-memory metric and the synthetic vas_066 probe

**Files:**
- Modify: `logai2/metrics.py` (register `ProcessCollector`; add `rss_mib()`, `trim()`)
- Create: `scripts/mem_probe.py`
- Create: `docs/perf/2026-10-29-memory.md` (the "before" numbers)
- Test: `tests/test_metrics.py`, `tests/test_mem_probe.py`

**Interfaces:**
- Produces: `rss_mib() -> float` (VmRSS from `/proc/self/status`, MiB; 0.0 when unreadable);
  `trim() -> None` (`ctypes.CDLL("libc.so.6").malloc_trim(0)`, ignores `OSError`/`AttributeError`);
  `scripts/mem_probe.py --dsn DSN --templates N [--minutes M]` printing one JSON line with keys
  `templates, baseline_rows, blob_bytes, load_peak_mib, load_steady_mib, save_state_ms, flush_peak_mib`.

- [ ] **Step 1: Write the failing tests**

```python
def test_process_memory_exported():
    m = Metrics(None)
    assert b"process_resident_memory_bytes" in generate_latest(m.registry)

def test_rss_mib_positive():
    assert rss_mib() > 0

def test_mem_probe_small(schema):   # tests/test_mem_probe.py
    out = subprocess.run([sys.executable, "scripts/mem_probe.py", "--dsn", f"{TEST_DSN}?options=-csearch_path%3D{schema},public",
                          "--templates", "500", "--minutes", "90"], capture_output=True, text=True, check=True)
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["templates"] == 500 and r["baseline_rows"] == 500 * 48 and r["blob_bytes"] > 0
    assert r["load_peak_mib"] >= r["load_steady_mib"] > 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_metrics.py tests/test_mem_probe.py`
Expected: FAIL (`rss_mib` not defined; script missing).

- [ ] **Step 3: Implement**

`metrics.py`: `ProcessCollector(registry=r)` in `Metrics.__init__`; `rss_mib`, `trim` as above.
`mem_probe.py`: create the schema (`migrate`, `ensure_partitions`), seed one service `vas` with N templates
(`vas:T%05d`, distinct texts), N x 48 baseline rows, a Drain3 blob from a `Miners` fed N distinct lines, 5 committed
weekdays in `day_status`; then in a **child process** (fresh RSS) build `Owner` and report peak
(`resource.getrusage(RUSAGE_SELF).ru_maxrss / 1024`) and steady (`rss_mib()`); time `Miners.dirty()` after one new
cluster (`save_state_ms`); feed M minutes of one count per template per minute through `Owner.handle` with the
watermark held, then one batch moving it past all M and flush (`flush_peak_mib`). Drop the schema at the end unless
`--keep`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q tests/test_metrics.py tests/test_mem_probe.py`
Expected: PASS.

- [ ] **Step 5: Record the "before" numbers**

Run: `.venv/bin/python scripts/mem_probe.py --dsn postgresql://logai:logai@localhost:5433/logai_test --templates 135000 --minutes 1300`
Write the JSON and a short table into `docs/perf/2026-10-29-memory.md` ("before", master commit hash).
If the 1,300-minute flush OOMs the laptop, record that and the largest `--minutes` that finished.

- [ ] **Step 6: Commit**

```bash
git add logai2/metrics.py scripts/mem_probe.py tests/test_metrics.py tests/test_mem_probe.py docs/perf/2026-10-29-memory.md
git commit -m "Memory metric and synthetic big-service probe"
```

---

### Task pm1: Compact baseline, streamed load

**Files:**
- Modify: `logai2/owner/baseline.py` (`Baseline` internals, `load`)
- Modify: `logai2/web/baseline_view.py:319-321` (the two `bl.stats` uses)
- Test: `tests/test_baseline.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (pm3 relies on these):
  - `Baseline(stats: dict[tuple[str, str, str, int], Stat] | None = None, days: dict[tuple[str, str], int] | None = None)`
    (same call form as today; converted to the packed layout).
  - `Baseline.load(conn, keys: list[tuple[str, str]] | None = None, services: list[str] | None = None) -> Baseline`
  - `Baseline.stat(service, key, daytype, hour) -> Stat | None`
  - `Baseline.has(service, key) -> bool`
  - `Baseline.update(other: Baseline) -> None` (adds/replaces other's keys and days)
  - `Baseline.drop(service: str) -> None`
  - `lookup`, `lookup_doc`, `days` unchanged. The `stats` attribute is removed.

Layout: `self._rows: dict[tuple[str, str], array]`, `array("d")` of 96 values, NaN = no stat;
slot = `(daytype == "weekend") * 48 + hour * 2` holds mean, slot + 1 holds std. `sys.intern` the service name.

- [ ] **Step 1: Write the failing tests**

```python
def test_compact_lookup_matches_dict_form():
    stats = {("s", "k", "weekday", 10): Stat(5, 1), ("s", "k", "weekend", 23): Stat(2, 0.5)}
    b = Baseline(stats, {("s", "weekday"): 5})
    assert b.stat("s", "k", "weekday", 10) == Stat(5, 1)
    assert b.stat("s", "k", "weekend", 23) == Stat(2, 0.5)
    assert b.stat("s", "k", "weekday", 11) is None and b.stat("s", "x", "weekday", 10) is None
    assert b.has("s", "k") and not b.has("s", "x")

def test_compact_memory_per_row():
    stats = {("s", f"s:T{t:05d}", dt, h): Stat(t, 1) for t in range(2000) for dt in ("weekday", "weekend") for h in range(24)}
    tracemalloc.start(); b = Baseline(stats, {}); cur, _ = tracemalloc.get_traced_memory(); tracemalloc.stop()
    assert cur / len(stats) < 40   # was 331 B/row in the dict form

def test_load_services_filter(conn):
    # seed baseline rows for services a and b
    b = Baseline.load(conn, services=["a"])
    assert b.has("a", "a:T1") and not b.has("b", "b:T1")

def test_update_and_drop():
    b = Baseline({("a", "k", "weekday", 1): Stat(1, 1)}, {("a", "weekday"): 5})
    b.update(Baseline({("b", "k", "weekday", 1): Stat(2, 1)}, {("b", "weekday"): 3}))
    assert b.stat("b", "k", "weekday", 1) == Stat(2, 1) and b.days[("b", "weekday")] == 3
    b.drop("a")
    assert not b.has("a", "k") and ("a", "weekday") not in b.days
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest -q tests/test_baseline.py`
Expected: FAIL (`stat` / `has` / `update` not defined).

- [ ] **Step 3: Implement**

`load` with `keys is None`: a server-side cursor (`conn.cursor(name="baseline_load")`, `itersize = 50_000`), filled
row by row into the arrays; the caller is already inside a transaction (owner). With `keys` given (web pages, small)
keep the plain `conn.execute`. `services` adds `WHERE service = ANY(%s)` to both the baseline and the `day_status`
query. `baseline_view.py:319` uses `bl.stat(service, ids[0], dt, h)`; `:321` uses `any(bl.has(service, t) for t in ids)`.

- [ ] **Step 4: Run the baseline, detect, web baseline tests**

Run: `.venv/bin/pytest -q tests/test_baseline.py tests/test_detect.py tests/test_web_baseline_view.py tests/test_web_docs_baseline.py tests/test_web_alerts_templates.py tests/test_owner.py`
(this is pm1's focused set)
Expected: PASS.

- [ ] **Step 5: Probe and record**

Run the pm0 probe at `--templates 135000`; put its JSON line in the PR file (the tech lead adds the "after pm1" row
to `docs/perf/2026-10-29-memory.md` at merge).
Expected: `load_steady_mib` and `load_peak_mib` far below the "before" row (6.5M rows x 20 B ~ 125 MiB for the baseline).

- [ ] **Step 6: Commit**

```bash
git add logai2/owner/baseline.py logai2/web/baseline_view.py tests/test_baseline.py
git commit -m "Baseline: one packed array per key, streamed load"
```

---

### Task pm5: Close minutes in bites, stop hoarding while lagging, lag warning

**Files:**
- Modify: `logai2/config.py` (`FlushConfig.max_closed_minutes: int = 60`, `ReaderConfig.lag_warn_seconds: int = 600`)
- Modify: `logai2/owner/owner.py` (`handle`, `_clear`, `_open_samples`, new `close_full`)
- Modify: `logai2/reader/reader.py` (`forget_before` -> `forget_read`)
- Modify: `logai2/engine.py` (`tick` flush condition, `_flush` calls `forget_read`, `_status` warning)
- Modify callers in tests: `tests/test_owner.py:42`, `tests/test_reread.py:138`, `tests/test_recorder_adversarial.py:255`, `tests/test_reader.py:158`
- Test: `tests/test_owner.py`, `tests/test_reader.py`, `tests/test_engine.py`

**Interfaces:**
- Produces: `Owner.close_full -> bool` (property: minutes closed since the last flush reached
  `cfg.flush.max_closed_minutes`); `Reader.forget_read() -> None` (per pattern, drops dedup ids with ts below its
  position); status warning text exactly `f"{pattern} is {minutes} min behind: alerts of every pattern wait for it"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_close_at_most_cap_per_flush(conn, cfg):        # tests/test_owner.py
    hits = [h for m in range(300) for h in lines(m, 1)]
    reader, owner = make(conn, cfg, FakeSource({"a": hits}), (T0 + 301 * 60_000) / 1000 + 60)
    batch = reader.poll(); owner.handle(batch)
    assert owner.minutes.next_to_close == M + 60 and owner.close_full
    for _ in range(4):
        owner.flush(reader.cursors(owner.boundary_ms), {}); owner.handle({**batch, "counts": []})
    owner.flush(reader.cursors(owner.boundary_ms), {})
    assert owner.minutes.next_to_close == M + 300
    assert rows(conn, "SELECT sum(sum) AS n FROM hourly_counts WHERE key = '*total'")[0]["n"] == 300

def test_open_samples_capped(conn, cfg):
    _, owner = make(conn, cfg, FakeSource({"a": []}), T0 / 1000 + 600)
    c = {"service": "s", "template_id": None, "text": "boom <*>", "level": "ERROR", "module": "", "minute": M, "n": 1, "sample": "boom 1"}
    for _ in range(100):
        owner.handle({"counts": [c], "watermark_ms": T0, "events": 1, "positions": {}, "sources": []})
    assert sum(len(v) for v in owner._open_samples[M].values()) <= SAMPLES_KEPT

def test_forget_read_drops_ids_before_position(cfg):    # tests/test_reader.py (replaces the forget_before test)
    r = Reader(FakeSource({"a": [hit("x", T0), hit("y", T0 + 5_000)]}), cfg, KnownLines(10), now=lambda: T0 / 1000 + 600)
    r.set_sources({"a": T0}, {})
    r.poll(); r.forget_read()
    assert set(r._seen["a"]) == {("a", "y")}              # only ids at the position's millisecond
    assert r.poll()["events"] == 0                         # no double count after forgetting

def test_lag_warning_names_the_slow_pattern(...):       # tests/test_engine.py
    # two patterns, detection on, "slow" position 20 min behind, "fast" caught up
    assert "slow is 20 min behind: alerts of every pattern wait for it" in engine._status()["warnings"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest -q tests/test_owner.py tests/test_reader.py tests/test_engine.py -k "cap or samples or forget_read or lag_warning"`
Expected: FAIL.

- [ ] **Step 3: Implement**

- `handle`: `close_before(min(batch["watermark_ms"] // 60_000, self.minutes.next_to_close + room), ...)` with
  `room = cfg.flush.max_closed_minutes - self._closed`; `self._closed += len(closed)`; `_clear` resets `_closed = 0`.
- `_open_samples: dict[int, dict[tuple[str, str], list[str]]]`; append only while the list is shorter than `SAMPLES_KEPT`.
- `Reader.forget_read`: per pattern, `cut = self._pos[index]`, delete ids with `ts < cut` (ids at or after the
  position are kept: the next page re-reads that millisecond). Update the four callers.
- `Engine.tick`: flush also when `self.owner.close_full`.
- `Engine._status`: when `self.owner.detection_enabled` and `lag > cfg.reader.lag_warn_seconds`, add the warning for
  each healthy pattern whose `pos_ms // 60_000 <= self._watermark_ms // 60_000` and is not caught up;
  minutes = `int((now - pos_ms / 1000) // 60)`.

- [ ] **Step 4: Run the focused set**

Run: `.venv/bin/pytest -q tests/test_owner.py tests/test_reader.py tests/test_engine.py tests/test_reread.py tests/test_recorder.py tests/test_recorder_adversarial.py tests/test_counts.py`
Expected: all pass.

- [ ] **Step 5: Probe and record**

Run the pm0 probe with `--minutes 1300`; put its JSON line in the PR file (tech lead adds the "after pm5" row).

- [ ] **Step 6: Commit**

```bash
git add logai2/config.py logai2/owner/owner.py logai2/reader/reader.py logai2/engine.py tests/test_owner.py tests/test_reader.py tests/test_engine.py tests/test_reread.py tests/test_recorder_adversarial.py
git commit -m "Close at most 60 minutes per flush; forget read ids; cap open samples; lag warning"
```

---

### Task pm2: On/Off switch per pattern (selection, reading, UI)

**Files:**
- Modify: `logai2/web/app.py` (`selection_entries`, `put_selection`)
- Modify: `logai2/web/static/js/views/sources.js` (switch per row, draft state, save payload)
- Modify: `logai2/engine.py` (`_select_indices`: floors, targets, `self.enabled` / `self.disabled`)
- Test: `tests/test_web_sources.py`, `tests/test_engine.py`

**Interfaces:**
- Produces:
  - Selection entry: `{"pattern": str, "added_at": float, "enabled": bool, "enabled_at": float | None}`; a saved
    entry without `enabled` reads as `enabled: True, enabled_at: None`.
  - `PUT /api/sources/selection` body: `{"patterns": [...], "disabled": [...], "revision": ...}`; `disabled` optional,
    must be a subset of `patterns` (else 400 `"disabled must name selected patterns"`).
  - `Engine.enabled: set[str]`, `Engine.disabled: set[str]` (patterns), set by `_select_indices`; pm3 passes them to
    the owner.

- [ ] **Step 1: Write the failing tests**

```python
def test_selection_disable_and_reenable(client):        # tests/test_web_sources.py
    put(client, ["a", "b"], disabled=["b"])
    e = entries(client)
    assert e["b"]["enabled"] is False and e["a"]["enabled"] is True and e["b"]["added_at"] == e_before["b"]["added_at"]
    put(client, ["a", "b"], disabled=[])
    assert entries(client)["b"]["enabled"] is True and entries(client)["b"]["enabled_at"] >= t_switch

def test_selection_disabled_must_be_selected(client):
    assert put(client, ["a"], disabled=["zzz"]).status_code == 400

def test_selection_without_enabled_is_on(client, conn):
    put_setting(conn, "index_selection", {"entries": [{"pattern": "a", "added_at": 0}]}); conn.commit()
    assert entries(client)["a"] == {"pattern": "a", "added_at": 0, "enabled": True, "enabled_at": None}

def test_disabled_pattern_not_read_and_excluded(...):   # tests/test_engine.py
    # selection: "logs-*" On, "logs-vas" Off; FakeSource has logs-app and logs-vas
    engine.tick()
    assert "logs-vas" not in engine.reader.floors
    assert engine.reader._targets["logs-*"] == "logs-*,-logs-vas"
    assert all(not c.startswith("logs-vas") for c in src.calls)

def test_reenabled_pattern_reads_from_enabled_at(...):
    # Off, flush (cursor row gone), On with enabled_at = T; a doc before T and one after
    assert engine.reader._pos["logs-vas"] >= T * 1000 and only the doc after T is counted
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest -q tests/test_web_sources.py tests/test_engine.py -k "disable or enabled"`
Expected: FAIL.

- [ ] **Step 3: Implement**

- `put_selection`: per pattern, `enabled = p not in disabled`; `enabled_at = now` when the previous entry was Off and
  this one is On, else the previous `enabled_at` (None for new entries); `added_at` kept as today.
- `_select_indices`: only On entries get a floor; `floor = max(bootstrap-or-added floor, enabled_at or 0)`;
  `targets[p] = p + exclusions of earlier On patterns + "-<q>" for every Off pattern q`. Set `self.enabled`,
  `self.disabled`. (An Off pattern has no cursor; `Owner.flush` already deletes rows of patterns not in `cursors`.)
- `sources.js`: a checkbox switch column "Monitoring" (`data-src-toggle`), kept in `src.draft.disabled`; Save sends
  `disabled`; an Off row shows badge `Off: not read, hidden everywhere, history kept`; the dirty check includes the
  switch. Remove keeps its button.

- [ ] **Step 4: Run web + engine tests**

Run: `.venv/bin/pytest -q tests/test_web_sources.py tests/test_engine.py`
Expected: PASS. Then open the Data sources page (`run` skill or the local stack) and switch a pattern Off and On.

- [ ] **Step 5: Commit**

```bash
git add logai2/web/app.py logai2/web/static/js/views/sources.js logai2/engine.py tests/test_web_sources.py tests/test_engine.py
git commit -m "Data sources: On/Off switch per pattern; Off patterns are not read and stay excluded"
```

---

### Task pm3: Owner keeps only active services (B24, B26), rebuild keeps baselines

**Files:**
- Create: `logai2/migrations/012_services_monitored.sql`
  (`ALTER TABLE services ADD COLUMN monitored boolean NOT NULL DEFAULT true;`)
- Modify: `logai2/owner/owner.py` (`__init__`, new `set_selection`, `_load`, `_unload`, `handle`, `flush`, `reload_baseline`)
- Modify: `logai2/owner/miners.py` (`load`, `drop`)
- Modify: `logai2/owner/counts.py` (`OpenMinutes.drop_service`)
- Modify: `logai2/engine.py` (`start` passes the selection; `_select_indices` calls `owner.set_selection`; `_flush` exports only active services)
- Modify: `logai2/jobs/commit.py` (`REBUILD` per service)
- Test: create `tests/test_owner_selection.py`; modify `tests/test_commit.py`

**Interfaces:**
- Consumes: pm1 `Baseline.load(services=)`, `update`, `drop`; pm2 `Engine.enabled` / `Engine.disabled`; pm0 `trim()`;
  pm5 bites.
- Produces:
  - `Owner(conn, cfg, start_minute, now=time.time, enabled: set[str] | None = None, disabled: set[str] = frozenset())`;
    `enabled=None` means every pattern in `index_services` is On (today's behaviour, keeps existing tests).
  - `Owner.set_selection(enabled: set[str], disabled: set[str]) -> None`
  - `Owner.active: set[str]`, `Owner.skipped_off: int`
  - `Miners.load(service: str, blob: bytes | None) -> None`, `Miners.drop(service: str) -> None`
  - `OpenMinutes.drop_service(service: str) -> None` (removes its keys from open minutes and `hour_keys`)

- [ ] **Step 1: Write the failing tests** (`tests/test_owner_selection.py`; seed `index_services` pairs
  (`a`, `sa`) and (`b`, `sb`), templates, baseline rows, a blob and alert states for both)

```python
def test_off_service_not_loaded(conn, cfg):
    o = Owner(conn, cfg, M, enabled={"a"}, disabled={"b"})
    assert o.active == {"sa"} and "sb" not in o.services
    assert not o.detector.baseline.has("sb", "sb:T00001") and ("sb", "sb:T00001") not in o.levels

def test_disable_slowest_pattern_jump_writes_no_zeros(conn, cfg):
    # sa counted live, b holds the watermark 3 h back; switch b Off, then move the watermark 3 h
    o.set_selection({"a"}, {"b"}); handle and flush until caught up
    assert rows(conn, "SELECT count(*) AS n FROM hourly_counts WHERE service = 'sb'")[0]["n"] == n_before
    assert rows(conn, "SELECT count(*) AS n FROM service_hours WHERE service = 'sb'")[0]["n"] == hours_before
    assert sa's *total sum == docs of sa                    # other pattern exact

def test_disable_resets_alerts(conn, cfg):
    # sb:T00001 ALERTING in alert_state
    o.set_selection({"a"}, {"b"}); o.flush({}, {})
    assert state_of("sb", "sb:T00001") == "NORMAL"
    assert last_change("sb", "sb:T00001") == ("ALERTING", "NORMAL")

def test_disable_drops_open_minutes(conn, cfg):
    # an open minute with sb counts
    o.set_selection({"a"}, {"b"})
    assert all(k[0] != "sb" for cm in o.minutes._open.values() for k in cm.keys)

def test_off_wins_over_on_pair(conn, cfg):
    # extra pair ("a", "sb"); a count for sb arrives through pattern a
    o = Owner(conn, cfg, M, enabled={"a"}, disabled={"b"})
    o.handle(batch_with(sb_count))
    assert "sb" not in o.active and o.skipped_off == sb_count["n"]

def test_reenable_loads_state_and_keeps_template_ids(conn, cfg):
    o = Owner(conn, cfg, M, enabled={"a"}, disabled={"b"})
    o.set_selection({"a", "b"}, set())
    assert o.detector.baseline.has("sb", "sb:T00001")
    assert o.miners.match("sb", sb_known_text)[0] == "sb:T00001"

def test_new_service_in_on_pattern_activates(conn, cfg):
    o.handle(batch_with(count_for("sc")))
    assert "sc" in o.active

def test_monitored_column_follows_selection(conn, cfg):
    o.set_selection({"a"}, {"b"}); o.flush({}, {})
    assert monitored(conn) == {"sa": True, "sb": False}

def test_rebuild_keeps_unmonitored_and_dataless_services(conn, cfg):   # tests/test_commit.py
    # baseline rows for sa, sb (monitored false) and sd (monitored, its service_hours dropped)
    commit(conn, cfg, today)
    assert baseline rows of sb and sd unchanged and sa rebuilt from its hours
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest -q tests/test_owner_selection.py tests/test_commit.py`
Expected: FAIL.

- [ ] **Step 3: Implement**

- `__init__`: compute `self.active` from `_source_pairs` and the selection (Off wins, spec §3); every state query
  (`drain3_state`, `templates` levels, `services`/first minute, `modules`, `alert_state`, `Baseline.load`) gets
  `WHERE service = ANY(%s)` with the active list.
- `set_selection`: new active set; `_unload(leaving)` then `_load(coming)`; mark `self._monitored_dirty`.
- `_unload(services)`: drop from `self.services`, `self.levels`, `self.modules`, `miners.drop`, `baseline.drop`,
  `minutes.drop_service`, `_open_samples`; for each of their states not NORMAL or with a streak: set NORMAL, streak 0,
  append a `Change(..., from_state, "NORMAL", ...)` at `now()`, add to `detector.touched`. Then `trim()`.
- `_load(services)`: same queries as `__init__` for those services inside `with conn.transaction()`;
  `miners.load`, `baseline.update`, merge levels/modules/states/first minutes.
- `handle`: a count whose service is Off-by-pair is skipped (`skipped_off += n`); a service not active and not Off is
  loaded with `_load({service})` before it is counted.
- `flush`: when `_monitored_dirty`, `UPDATE services SET monitored = (service = ANY(%s))` in the same transaction;
  new services insert with the default (true).
- `reload_baseline`: `Baseline.load(conn, services=sorted(self.active))`.
- `engine.py`: `Owner(..., enabled=self.enabled, disabled=self.disabled)`; `_select_indices` calls
  `self.owner.set_selection(...)` when the owner exists (before the next `poll`, so leaving services never get the
  jump's minutes); `export_rows` only for `r.service in self.owner.active`; status gains `"skipped_off"`;
  `start()` logs `logger.info("state loaded: %d services, rss %.0f MiB", len(self.owner.active), rss_mib())` after
  building the owner, and `_unload` logs the same line after `trim()`.
- `commit.py`: REBUILD's `hours` CTE also requires `sh.service IN (SELECT service FROM services WHERE monitored)`;
  insert the result into `tmp_rebuild (LIKE baseline)`, then `DELETE FROM baseline WHERE service IN (SELECT DISTINCT
  service FROM tmp_rebuild)`, then `INSERT INTO baseline SELECT * FROM tmp_rebuild`; `rows` = rows inserted.

- [ ] **Step 4: Run the focused set**

Run: `.venv/bin/pytest -q tests/test_owner_selection.py tests/test_owner.py tests/test_engine.py tests/test_commit.py tests/test_detect.py tests/test_miners.py tests/test_counts.py tests/test_reread.py tests/test_recorder.py`
Expected: all pass.

- [ ] **Step 5: Probe and record**

Extend the probe with `--off` (the big service's pattern Off at start, then switched On, then Off); put start / on /
off RSS in the PR file (tech lead adds the "after pm3" row).

- [ ] **Step 6: Commit**

```bash
git add logai2/migrations/012_services_monitored.sql logai2/owner/owner.py logai2/owner/miners.py logai2/owner/counts.py logai2/engine.py logai2/jobs/commit.py tests/test_owner_selection.py tests/test_commit.py scripts/mem_probe.py
git commit -m "Owner keeps only active services; Off resets alerts; rebuild keeps baselines it did not rebuild"
```

---

### Task pm4: Hide Off services in the web and the linker

**Files:**
- Modify: `logai2/web/app.py` (`known_service`, `known_template`, `document_fields` service check, `/api/services`,
  `/api/alerts`, `/api/templates`, `/api/template-levels`, `/api/documents`, `/api/modules`, `/api/baseline/groups`,
  `/api/retrain` services)
- Modify: `logai2/web/pipeline_view.py` (`services`)
- Modify: `logai2/module_alerts.py:63`
- Modify: `logai2/linker.py` (`_embed_templates`, pending count), `logai2/resolver.py:131`
- Test: create `tests/test_web_monitored.py`; modify `tests/test_linker.py`

**Interfaces:**
- Consumes: pm3 `services.monitored`.
- Produces: `MONITORED = "{} IN (SELECT service FROM services WHERE monitored)"` in `logai2/web/app.py`, used as
  `MONITORED.format("t.service")`.

- [ ] **Step 1: Write the failing tests** (`sa` monitored, `sb` not; both with templates, alerts, modules)

```python
LISTS = ["/api/services", "/api/alerts", "/api/templates", "/api/pipeline", "/api/retrain", "/api/documents"]

@pytest.mark.parametrize("url", LISTS)
def test_off_service_absent_from_lists(client, url):
    assert "sb" not in client.get(url).get_data(as_text=True)

@pytest.mark.parametrize("url", ["/api/baseline?service=sb", "/api/baseline/groups?service=sb",
                                 "/api/modules?service=sb", "/api/templates/sb/sb:T00001/similar"])
def test_off_service_routes_404(client, url):
    assert client.get(url).status_code == 404

def test_template_levels_and_module_alerts_skip_off(client): ...

def test_linker_skips_off_templates(conn, cfg):          # tests/test_linker.py
    Linker(cfg, conn, embed_fn=fake).cycle()
    assert embedded(conn, "sb") == 0 and embedded(conn, "sa") > 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest -q tests/test_web_monitored.py tests/test_linker.py`
Expected: FAIL.

- [ ] **Step 3: Implement**

`known_service` / `known_template`: add `AND monitored` (via the `services` row) and `abort(404, "Unknown service")`;
`/api/modules` and `/api/baseline/groups` go through `known_service`. Every list query listed in **Files** adds
`AND MONITORED.format(<service column>)`; the documents' `linked_templates` count adds it to its FILTER. The linker's
template SELECT, its pending count, and the resolver's candidate SELECT join `services s ON s.service = t.service AND s.monitored`.

- [ ] **Step 4: Run the focused set and click through**

Run: `.venv/bin/pytest -q tests/test_web_*.py tests/test_linker.py tests/test_resolver.py tests/test_module_alerts.py tests/test_module_alerts_adversarial.py`
Expected: all pass. Then with the local stack: switch a pattern Off and check Templates, Baseline, Review, Pipeline,
Alerts, Modules: the service is gone; a bookmarked `?service=` URL shows the "unknown service" state.

- [ ] **Step 5: Commit**

```bash
git add logai2/web/app.py logai2/web/pipeline_view.py logai2/module_alerts.py logai2/linker.py logai2/resolver.py tests/test_web_monitored.py tests/test_linker.py
git commit -m "Off services are hidden from every page, API and the linker"
```

---

### Task pm6: Memory proof and docs

**Files:**
- Modify: `docs/perf/2026-10-29-memory.md`, `docs/BUGS.md`, `ARCHITECTURE.md`, `docs/WEB_UI.md`, `README.md`
- Modify: `docs/specs/2026-10-29-pattern-switch-memory.md` (status: done)

- [ ] **Step 1: Final probe on master with all tasks merged**

Run: `.venv/bin/python scripts/mem_probe.py --dsn postgresql://logai:logai@localhost:5433/logai_test --templates 135000 --minutes 1300 --off`
Expected, recorded in the perf doc beside "before": start with the service Off adds under 50 MiB over an empty
start; switching it On adds under 400 MiB; the 1,300-minute catch-up finishes with `flush_peak_mib` under 2x
`load_steady_mib`. A miss is reported as a finding, not tuned away.

- [ ] **Step 2: Docs**

BUGS: B24, B25 and B26 (add B26 first, then move) to **Fixed** with their merge commits; add the memory findings
(dedup ids, open samples, one-flush close) as fixed under pm5. ARCHITECTURE: active services, Off rules, bites,
compact baseline, migration 012. WEB_UI: the switch, `disabled` in the selection API, 404 for Off services.
README: upgrade note (migration 012 runs at start; every saved pattern stays On).

- [ ] **Step 3: Commit, then `graphify update .` in `~/Documents/logai2` after the user confirms the merge**

```bash
git add docs/perf/2026-10-29-memory.md docs/BUGS.md ARCHITECTURE.md docs/WEB_UI.md README.md docs/specs/2026-10-29-pattern-switch-memory.md
git commit -m "Pattern switch and memory: proof numbers and docs"
```

## Company rollout (after pm6, by the user)

1. Deploy; migration 012 runs at start (every service monitored, every pattern On).
2. On Data sources, switch vas_066's pattern **Off**, Save. Expect within one index refresh: its services gone from
   the pages, the engine log line `state loaded` / RSS drop (or after one restart), no `*total` alerts for it.
3. Watch `process_resident_memory_bytes`; lower the pod limit only after a day of numbers.
