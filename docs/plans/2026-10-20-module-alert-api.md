# logai2 Module Alert API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: **approved, not built** (sub-project 4). Rewritten 2026-10-22 against `master` `6c600f3`.

**Goal:** other services call `GET /api/module-alerts?module_code=M&from=…&to=…` and get the
templates that were WARMING or ALERTING in that window and were logged by module M, with M's
per-minute count next to the template's total.

**Architecture:** no second detector, no new table. A read view over what the engine already
writes: `modules`, `alert_changes`, `alert_minutes`. Two small engine changes (module per
service; the streak's first minute in `alert_minutes`). `review.periods` gains a `states`
argument; a new `logai2/module_alerts.py` holds the window parser and the query; one route.

**Tech stack:** unchanged (Python 3.11, psycopg 3, Flask 3, pytest, Postgres 16). No new dependency.

**Spec (the contract, approved 2026-10-22):** [`docs/specs/2026-10-22-module-alert-api.md`](../specs/2026-10-22-module-alert-api.md).
Every value below is copied from it; when they disagree, the spec wins.

---

## Part A — Execution with Opus and Sonnet

Team workflow per [`docs/TEAM_WORKFLOW.md`](../TEAM_WORKFLOW.md), with **Claude models only**
(user, 2026-10-22): no Gemini on this build.

### Roles

| Role | Model | Does |
|---|---|---|
| Tech lead | Opus 5.5 (main session) | writes the 3 briefs, creates worktrees, dispatches, answers `QUESTION:`, final review of every task, merges `--no-ff` **only after the user confirms that merge**, keeps `.team/ledger.md`, deploys after the user says so |
| Implementer | Sonnet 5.5 subagent (`model: sonnet`), one per task | tests first, code, full suite, commits, PR file `.team/prs/task-<id>-<slug>.md` |
| Task reviewer | a **fresh** Sonnet 5.5 subagent (never the implementer's conversation) | spec + quality review of the branch against its brief: `Spec ✅/❌`, Critical / Important / Minor, `Approved / Needs fixes` |
| Adversarial reviewer (Task 2 only) | Opus 5.5 subagent (`model: opus`) | Task 2 holds the time math (minute ends vs starts, back-dating, clamps, partitions); one extra review that writes edge-case tests against it |

Why Sonnet implements all three: each task touches SQL or the owner's flush path, which
TEAM_WORKFLOW gives to the judgment model; the tasks are small enough that one Sonnet run each fits.

### Tasks, branches, order

| Id | Task | Branch / worktree | Depends on | Files (disjoint between 1 and 2) |
|---|---|---|---|---|
| 41 | Engine: module per service, first minute | `task/41-module-per-service`, `../logai2-task-41` | — | migrations/006, owner/owner.py, tests/test_db.py, tests/test_owner.py, ARCHITECTURE.md §7.2/§8.5/§11 |
| 42 | Window parser + module query | `task/42-module-query`, `../logai2-task-42` | — | review.py, module_alerts.py (new), tests/test_review.py, tests/test_module_alerts.py (new) |
| 43 | Route, token, docs, deploy config | `task/43-module-route`, `../logai2-task-43` | 41 and 42 merged | config.py, web/app.py, tests/test_web_module_alerts.py (new), tests/test_config.py, docs/WEB_UI.md, README.md, ARCHITECTURE.md §1/§3/§14, docker-compose.yml, k8s/logai-web.yaml |

```
 t0 ──┬─ 41 Sonnet impl ─ Sonnet review ─ Opus final ─ merge ──┐
      └─ 42 Sonnet impl ─ Sonnet review ─ Opus adversarial ─ Opus final ─ merge ──┤
                                                                                  └─ 43 Sonnet impl ─ Sonnet review ─ Opus final ─ merge
                                                                                        └─ graphify update, agents.md, deploy (user's go)
```

41 and 42 run **in parallel** (no shared file; 42 seeds its rows directly and does not need 41's
code). 43 branches off `master` after both merges. Every "merge" in this diagram waits for the
user's confirmation of that branch.

### Per-task loop (tech lead)

1. Write the brief `docs/tasks/<id>-<slug>.md` from `docs/templates/task-brief.md`: it points to
   this plan's task section and the spec sections, and copies the file list and done-when.
2. `git worktree add ../logai2-task-<id> -b task/<id>-<slug> master`; ledger line `dispatch`.
3. Dispatch the Sonnet implementer (`Agent`, `model: sonnet`, run in background): "Work in
   `../logai2-task-<id>`. Brief: `docs/tasks/<id>-<slug>.md`. PR file: `.team/prs/task-<id>-<slug>.md`.
   Reply with Status / commits / test output / `QUESTION:` or concerns."
4. `QUESTION:` → answer by `SendMessage` to the same agent; ledger line `Ruling:` with its cost if wrong.
5. `DONE <sha>` → check `git -C ../logai2-task-<id> status`, diff stays inside the brief's files,
   commit messages without attribution; then dispatch the fresh Sonnet reviewer (and for 42 the
   Opus adversarial reviewer) on brief + PR file + `git diff master...task/<id>-<slug>`.
6. Needs fixes → `SendMessage` the findings to the implementer; at most 3 rounds, then the tech lead rules.
7. Opus final review: seams (units: minute **ends** in `alert_changes` vs minute **starts** in
   the API; transactions; the token; partitions), full suite on the branch, docs updated.
8. **Ask the user to confirm the merge** (show the review verdict, the diff stat and the test
   output). Nothing reaches `master` before the user says yes for that branch (user rule 2026-10-22).
9. On the user's yes: `git merge --no-ff task/<id>-<slug>` with the PR summary as message; full suite
   on `master`; `git worktree remove`, `git branch -d`; ledger line `merged <sha> (user confirmed)`.

### Finish

- After 43: `graphify update .` in `~/Documents/logai2`; `agents.md` entry; spec and plan Status → built.
- Deploy **only when the user says so**: `docker compose build logai2-engine logai2-web`, restart
  `logai2-engine` first (migration 006), then `logai2-web`; linker untouched. k8s: the user pushes
  the image and adds `LOGAI_API_TOKEN` to Secret `logai-env` (optional key).
- Live check on the test stack: the route answers 400 without `from`/`to`, 200 with `templates: []`
  for an unknown module, and `data_until` close to now. (The test stack's `recharge-logs` has no
  `moduleCode`, so a non-empty answer needs `udcntt*`.)

---

## Part B — Tasks

### Facts from the current code the tasks rely on

- `alert_changes.at` = **end** of the closed minute, `(minute + 1) * 60` (`owner/detect.py`).
  Do **not** change it: Review, Baseline and `alert_rows.since` read it as is. The API converts
  to minute starts (Task 42).
- Hysteresis `detect.step`, defaults `warm 2 / alert 3 / cool 3`: high minute 1 → NORMAL streak 1;
  high minute 2 → WARMING; high minute 3 → ALERTING; low after ALERTING → COOLING.
- `alert_minutes` rows are appended in `Owner.handle` after `detector.run(cm)`, from the state
  after the step, for template keys with `state != "NORMAL"` (`owner.py` ~line 189). The streak's
  first minute (NORMAL, streak 1) has no row today.
- `modules (module_code PK, service)`; the owner keeps a set of codes and `_modules[module] = service`,
  so a module seen in a second service is never recorded for it.
- `drop_partitions_before(conn, table, cutoff)` drops days `< today − retention.counts_days`; the
  oldest kept local day is `today − counts_days`.
- `worker_status['engine'].status` has `heartbeat` and `lag_seconds` (`engine._status`).
- `app.py` already has a `parse_range` (Baseline); the new parser is `parse_window`.
- Test helpers: `tests/test_owner.py` `make`, `run_minutes`, `lines`, `rows`; web tests build
  `create_app(cfg)` in an `app` fixture over the test schema (`tests/test_web_alerts_templates.py`).

### Task 41: engine — a module per service, and the streak's first minute

**Files:** create `logai2/migrations/006_modules_per_service.sql`; modify `logai2/owner/owner.py`,
`ARCHITECTURE.md` (§7.2 / §8.5 `alert_minutes` rule, §11 `modules` key, migration list in §11 and §16);
tests `tests/test_db.py`, `tests/test_owner.py`.

**Requirements**
1. `modules` primary key `(service, module_code)`, plus index `modules_code (module_code)`.
2. The owner's known set is `{(service, module_code)}`, loaded with `SELECT service, module_code FROM modules`;
   a new pair (module ≠ `""`) is queued and inserted as `(module_code, service)` with `ON CONFLICT DO NOTHING`.
3. `alert_minutes` rows are also written for a template key whose row after the step is NORMAL
   with `streak > 0`: condition `row is not None and (row.state != "NORMAL" or row.streak)`.
   Historic minutes still write none.

- [ ] **Step 1: failing tests**

```python
# tests/test_db.py
def test_module_can_belong_to_two_services(conn):
    conn.execute("INSERT INTO services VALUES ('a', 1), ('b', 2)")
    conn.execute("INSERT INTO modules (module_code, service) VALUES ('M1', 'a'), ('M1', 'b')")
    assert conn.execute("SELECT count(*) AS n FROM modules WHERE module_code = 'M1'").fetchone()["n"] == 2

# tests/test_owner.py
def test_module_rows_per_service(conn, cfg):
    # one minute: service a module M1, service b module M1, service a no module
    ...
    assert rows(conn, "SELECT service, module_code FROM modules ORDER BY 1") == [("a", "M1"), ("b", "M1")]

def test_module_set_survives_restart(conn, cfg):
    # run with (a, M1); new Owner on the same schema; then (b, M1) → both rows, no error

def test_first_high_minute_in_alert_minutes(conn, cfg):
    # committed baseline s:T1 (ERROR) mean 1 std 0.5, detection on, not historic;
    # minutes M..M+3 at 40/min from module M1
    # → alert_minutes for s:T1/M1 holds minutes M (NORMAL streak 1), M+1 (WARMING), M+2, M+3 (ALERTING)

def test_single_high_minute_writes_one_row_and_no_change(conn, cfg):
    # one high minute then low → one alert_minutes row, no alert_changes row
```

- [ ] **Step 2:** `.venv/bin/pytest tests/test_db.py tests/test_owner.py -q` → new tests fail.
- [ ] **Step 3:** implement.

```sql
-- 006_modules_per_service.sql
ALTER TABLE modules DROP CONSTRAINT modules_pkey;
ALTER TABLE modules ADD PRIMARY KEY (service, module_code);
CREATE INDEX modules_code ON modules (module_code);
```
- [ ] **Step 4:** `.venv/bin/pytest -q` all green. ARCHITECTURE updated.
- [ ] **Step 5:** commit `Engine: modules keyed by (service, module_code); alert_minutes from a streak's first minute`.

### Task 42: window parser and the module query

**Files:** modify `logai2/review.py`; create `logai2/module_alerts.py`;
tests `tests/test_review.py`, new `tests/test_module_alerts.py`.

**Interfaces (Task 43 relies on these exact names)**

```python
# review.py
NON_NORMAL = frozenset({"WARMING", "ALERTING", "COOLING"})
def periods(changes, end_now, states=NON_NORMAL) -> list[tuple[str, float, float]]
    # a change whose to_state is in `states` opens a period if none is open; any other change closes it.
    # Default unchanged: every Review/Baseline test passes untouched.

# module_alerts.py
def parse_window(args: Mapping[str, str], now: float, retention_days: int) -> tuple[str, float, float, float, float]
    # -> (module_code, from_s, to_s, start, end): from_s/to_s as sent (epoch s, ms kept as fraction),
    #    start/end the effective window (rounded outward, clamped). ValueError(message) for each 400 case.
def data_until(conn) -> float | None
    # floor((heartbeat - lag_seconds) / 60) * 60 from worker_status['engine']; None when no row.
def module_alerts(conn, module_code: str, start: float, end: float) -> list[dict]
```

**`parse_window` rules** (spec §2)
1. `module_code` stripped; missing or empty → ValueError. `from` and `to` both required → ValueError.
2. A value parses as a number first: `> 1e11` = epoch ms, else epoch s. Otherwise ISO-8601 via
   `datetime.fromisoformat(v.replace("Z", "+00:00"))`, naive = UTC. Neither → ValueError.
3. `from > to` → ValueError (checked on the values as sent).
4. Clamp: `oldest` = 00:00 local of `(local date of now) − retention_days` (`timeutil.TZ`);
   `from = max(from, oldest)`, `to = min(to, now)`; if clamping makes `from > to`, `to = from`.
5. `start = floor(from / 60) * 60`; `end = ceil(to / 60) * 60`, and `end = start + 60` when equal.

**`module_alerts` rules** (spec §3, §6)
1. `SELECT service FROM modules WHERE module_code = %s`; none → `[]`.
2. Changes of those services on template keys (`key NOT LIKE '*%%' AND key NOT LIKE 'doc:%%'`):
   latest row per `(service, key)` with `at < start + 60` (`DISTINCT ON (service, key) … ORDER BY service, key, at DESC`)
   plus every row with `start + 60 <= at < end + 60`, ordered by `at`. (`+ 60`: `at` is a minute end, so
   the change that ends the window's first minute belongs to the window.)
3. Per service: `p = periods(rows, end + 60, {"WARMING", "ALERTING"})` and
   `a = periods(rows, end + 60, {"ALERTING"})`. Convert every bound to a minute start (`− 60`);
   a WARMING/ALERTING period whose opening change is `NORMAL → WARMING` moves its start back
   `(cfg.alert.warm_consecutive − 1) * 60` more (take the value as a parameter with default 2, or
   pass `cfg`; one choice, stated in the PR). An end equal to `end` (open at the window's end)
   is reported as `None` when no closing change exists.
4. Keep periods overlapping `[start, end)`. Minute `m` counts when `p.start <= m < p.end` and `start <= m < end`.
   `peak_state` = `ALERTING` when some `a` period overlaps `[start, end)`, else `WARMING`.
5. `alert_minutes` rows of those keys with `minute >= start AND minute < end AND day BETWEEN
   day_of(start) AND day_of(end − 1)` (local days via `timeutil`). Minutes failing rule 4 are dropped.
   All modules → `template`; `module_code = M` → `module`.
6. Drop a template whose module total is 0. `alerting_minutes` = counted minutes of the periods
   inside the window. `avg = round(total / alerting_minutes, 2)`, `peak` = max per minute.
   `series`: per minute (`{"minute", "count"}`, stored minutes only) when `end − start <= 86400`,
   else per local hour start (`{"hour", "count"}`).
7. `text`, `level` from `templates` (missing row → `""`, `"INFO"`); `doc_id`/`doc_title` from
   `template_doc_links` (`doc_id IS NOT NULL AND source = ANY(links.ALERTING_SOURCES)`) joined to `documents`.
8. Sort by `module.total` desc, then `service`, `template_id`. Times stay epoch seconds here; Task 43 formats.
9. Item shape:
   ```
   {"service", "template_id", "text", "level", "doc_id", "doc_title", "peak_state",
    "periods": [{"start": float, "end": float | None}],
    "alerting_minutes": int,
    "module":   {"total", "avg", "peak", "series": [...]},
    "template": {"total", "avg", "peak"}}
   ```

- [ ] **Step 1: failing tests** (seed `modules`, `templates`, `alert_changes`, `alert_minutes`
  directly; `t(h, m)` = epoch of 2026-10-14 h:m local; `at` values are minute **ends**)

```python
# tests/test_review.py
def test_periods_states_argument():
    ch = [c("s:T1", t(8, 1), "NORMAL", "WARMING"), c("s:T1", t(8, 3), "WARMING", "ALERTING"),
          c("s:T1", t(8, 5), "ALERTING", "COOLING"), c("s:T1", t(8, 6), "COOLING", "ALERTING")]
    assert periods(ch, t(9, 0), {"WARMING", "ALERTING"}) == [("s:T1", t(8, 1), t(8, 5)), ("s:T1", t(8, 6), t(9, 0))]
    assert periods(ch, t(9, 0), {"ALERTING"}) == [("s:T1", t(8, 3), t(8, 5)), ("s:T1", t(8, 6), t(9, 0))]

# tests/test_module_alerts.py
def test_counts_from_first_high_minute_to_cooling(conn):
    # minute 08:00 streak 1, 08:01 WARMING (change at 08:02), 08:02..08:05 ALERTING (change at 08:03),
    # COOLING from 08:06 (change at 08:07); alert_minutes M1=10, M2=5 on 08:00..08:08
    it = module_alerts(conn, "M1", t(8, 0), t(8, 15))[0]
    assert it["periods"] == [{"start": t(8, 0), "end": t(8, 6)}] and it["peak_state"] == "ALERTING"
    assert it["alerting_minutes"] == 6
    assert it["module"]["total"] == 60 and it["module"]["peak"] == 10 and it["module"]["avg"] == 10.0
    assert it["template"]["total"] == 90

def test_warming_that_falls_back(conn):
    # 08:00 streak 1, 08:01 WARMING (at 08:02), 08:02 low → NORMAL (at 08:03)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 15))[0]
    assert it["peak_state"] == "WARMING" and it["alerting_minutes"] == 2

def test_started_before_window_and_still_open(conn):
    # only change before the window: WARMING → ALERTING at 07:50 (end of minute 07:49)
    it = module_alerts(conn, "M1", t(8, 0), t(8, 10))[0]
    assert it["periods"] == [{"start": t(7, 49), "end": None}] and it["alerting_minutes"] == 10

def test_window_of_one_minute_inside_an_alert(conn):
    # ALERTING 08:00..08:09; window [08:03, 08:04) → 1 minute, that minute's counts only

def test_cooling_before_window_and_other_module_left_out(conn):
    # s:T2 ALERTING→COOLING at 07:59; s:T3 ALERTING with only M2 rows
    assert [i["template_id"] for i in module_alerts(conn, "M1", t(8, 0), t(8, 15))] == ["s:T1"]

def test_module_in_two_services(conn):
    assert {i["service"] for i in module_alerts(conn, "M1", t(8, 0), t(8, 15))} == {"a", "b"}

def test_window_across_local_midnight(conn):
    # ALERTING minutes 23:58 (Oct 14) .. 00:02 (Oct 15), M1 = 7 per minute, both partitions
    assert module_alerts(conn, "M1", t(23, 55), t(23, 55) + 600)[0]["module"]["total"] == 5 * 7

def test_series_per_hour_over_one_day(conn):
    # window of 2 days → series items have "hour", not "minute"

def test_unknown_module_and_missing_template_row(conn):
    assert module_alerts(conn, "NOPE", t(8, 0), t(8, 15)) == []
    # alerting key with no templates row → text "" and level "INFO", no error

def test_doc_and_service_keys_never_returned(conn):
    # doc:DOC-1 and *error ALERTING with M1 rows → []

def test_data_until(conn):
    # worker_status engine {heartbeat: 1000.0, lag_seconds: 75.0} → 900.0; no row → None

def test_parse_window():
    now = 1792461600.0                                  # 2026-10-20T02:00:00Z
    pw = lambda **a: parse_window(a, now, 90)
    assert pw(module_code=" M1 ", **{"from": "1792458000", "to": "1792458300"})[0] == "M1"
    assert pw(module_code="M", **{"from": "2026-10-20T01:00:00Z", "to": "1792461600"})[3:] == (1792458000.0, 1792461600.0)
    assert pw(module_code="M", **{"from": "1792458030500", "to": "1792458031200"})[3:] == (1792458000.0, 1792458060.0)
    assert pw(module_code="M", **{"from": "2026-10-20T01:00:59.999Z", "to": "2026-10-20T01:01:00.001Z"})[3:] \
        == (1792458000.0, 1792458120.0)
    assert pw(module_code="M", **{"from": "1792458000", "to": "1792458300"})[3:] == (1792458000.0, 1792458300.0)
    assert pw(module_code="M", **{"from": "1792458012", "to": "1792458012"})[3:] == (1792458000.0, 1792458060.0)
    assert pw(module_code="M", **{"from": "2026-10-20T08:00:00+07:00", "to": "1792458060"})[3:] \
        == (1792458000.0, 1792458060.0)                 # offset honoured
    # clamps: from older than retention → 00:00 local of today − 90; to in the future → now
    oldest = datetime(2026, 7, 22, tzinfo=TZ).timestamp()       # local date of now is 2026-10-20
    assert pw(module_code="M", **{"from": "0", "to": "1792458000"})[3] == oldest
    assert pw(module_code="M", **{"from": "1792458000", "to": str(now + 3600)})[4] == now
    for bad in ({}, {"module_code": ""}, {"module_code": "M"}, {"module_code": "M", "from": "0"},
                {"module_code": "M", "to": "0"}, {"module_code": "M", "from": "abc", "to": "0"},
                {"module_code": "M", "from": "200", "to": "100"}):
        with pytest.raises(ValueError):
            parse_window(bad, now, 90)
```

- [ ] **Step 2:** run → fail. **Step 3:** implement. **Step 4:** `.venv/bin/pytest -q` all green
  (Review/Baseline tests unchanged).
- [ ] **Step 5:** commit `Module alerts: window parser and WARMING/ALERTING query per module`.

### Task 43: route, token, docs, deploy config

**Files:** modify `logai2/config.py`, `logai2/web/app.py`, `docs/WEB_UI.md`, `README.md`,
`ARCHITECTURE.md` (§1 sub-project 4 built, §3 env table, §14 limits), `docker-compose.yml`,
`k8s/logai-web.yaml`; tests new `tests/test_web_module_alerts.py`, `tests/test_config.py`.

**Requirements**
1. `WebConfig.api_token: str = ""`; `LOGAI2_API_TOKEN` sets it (empty ignored, like the other vars).
2. `GET /api/module-alerts`:
   - token set and `not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token)`
     → 401 `{"error": "unauthorized"}`. Only this route checks it.
   - `parse_window(request.args, time.time(), cfg.retention.counts_days)`; `ValueError` → `BadRequest(str(exc))`.
   - 200 body (spec §4):
     ```json
     {"module_code", "from", "to", "effective_from", "effective_to",
      "resolution": "minute", "data_until", "unit": "events_per_minute", "templates": [...]}
     ```
     every time through one helper `iso(ts) -> str | None` (UTC, `Z`, milliseconds kept when non-zero:
     `from`/`to` echo `…T07:00:30.500Z`; minute bounds print `…T07:00:00Z`). Items as Task 42 with
     `periods`, `series` minutes/hours formatted.
3. Compose: `LOGAI2_API_TOKEN: ${LOGAI2_API_TOKEN:-}` on `logai2-web`. k8s `logai-web.yaml`:
   `LOGAI2_API_TOKEN` from Secret `logai-env` key `LOGAI_API_TOKEN` with `optional: true`. No token value in any file.
4. Docs: WEB_UI.md HTTP API section (request, response, errors, rounding, WARMING/ALERTING and
   `peak_state`, `data_until`, token, DEBUG/INFO never alert); README env table + "Module alert API"
   with a `curl` example; ARCHITECTURE §1, §3, §14 ("one shared token, no per-caller keys").

- [ ] **Step 1: failing tests**

```python
# tests/test_config.py
def test_api_token_from_env(monkeypatch): ...   # set → cfg.web.api_token; "" → ""

# tests/test_web_module_alerts.py  (rows seeded as Task 42's first test)
def test_shape(client, conn):
    body = client.get("/api/module-alerts?module_code=M1&from=<08:00:30.5 local ISO>&to=<08:14:10.2>").get_json()
    assert body["effective_from"].endswith("T01:00:00Z") and body["from"].endswith(".500Z")
    assert body["resolution"] == "minute" and body["unit"] == "events_per_minute"
    assert body["templates"][0]["module"]["total"] == 60 and body["templates"][0]["peak_state"] == "ALERTING"

def test_unknown_module_empty_200(client): ...   # with from/to → 200, templates []

@pytest.mark.parametrize("qs", ["", "module_code=&from=0&to=60", "module_code=M", "module_code=M&from=0",
                                "module_code=M&to=60", "module_code=M&from=abc&to=60",
                                "module_code=M&from=200&to=100"])
def test_bad_input_400(client, qs): ...

def test_module_code_unicode_and_slash_round_trip(client, conn):   # "VTN/066 é", urlencoded

def test_data_until_null_without_engine_row(client): ...

def test_token(conn, cfg, schema):   # cfg.web.api_token = "s3cret"
    # no header → 401; "Bearer wrong" → 401; "Bearer s3cret" → 200; /api/alerts still 200 without a header
```

- [ ] **Step 2–4:** fail, implement, `.venv/bin/pytest -q` all green.
- [ ] **Step 5:** commit `Web: GET /api/module-alerts with optional bearer token`.

---

## Review focus (task reviewers and the final review)

1. Minute ends (`alert_changes.at`) vs minute starts (API): the first counted minute, the last
   one before COOLING, and a window of one minute inside an alert.
2. Back-dating applies only to a period opened by `NORMAL → WARMING`.
3. A period open since before `from`; a WARMING that falls back (`peak_state: WARMING`).
4. One module in two services; a window across local midnight; a 2-day window (hourly series).
5. Clamps: `from` older than retention, `to` in the future; `from == to`; ms and `+07:00` input.
6. Token: 401 cases, constant-time compare, other routes unaffected, no token value committed.
7. Engine: `modules` migration on a database that already has rows; the owner restart path.

## Out of scope (spec §9)

Document and service alerts, query by `groupModule`, COOLING minutes, per-second unit,
per-caller keys, pagination, a web page, the normal rate per template.
