# logai2 Module Alert API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `GET /api/module-alerts?module_code=M&from=…&to=…` returns the
templates that were ALERTING in that range and logged from module M, with
module M's per-minute rate next to the template's total rate.

**Architecture:** No second detector and no new storage. The web serves a
read view over tables the core engine already writes: `modules` (module →
service), `alert_changes` (ALERTING periods per template key) and
`alert_minutes` (per-module counts for minutes a key was not NORMAL). A pure
function turns state changes into ALERTING periods; one query module joins
periods, minutes, template texts and documents.

**Tech Stack:** as core engine v1 (Python ≥ 3.11, psycopg 3, Flask 3, pytest, Postgres 16). No new dependency.

**Spec:** [`docs/design/per-service-baseline.md`](../design/per-service-baseline.md)
section 7 "Module alert API", and [`docs/specs/2026-10-20-core-engine-design.md`](../specs/2026-10-20-core-engine-design.md)
(sub-project 4 in its Scope table). Builds on
[`2026-10-20-core-engine-v1.md`](2026-10-20-core-engine-v1.md): its Task 1 (schema),
Task 8 (`Change`, `alerts.py`), Task 9 (owner flush), Task 12 (web routes), Task 13 (`review.suggest`),
Task 14 (`incident_test.py`). Replaces logai-engine's
`docs/superpowers/plans/2026-10-19-module-alert-api.md` (groups, file store).

## Global Constraints

- Start after `core-engine-v1` is merged: branch `module-alert-api` off `master`. Never add `Co-Authored-By` or any Claude attribution to commits.
- "Alerting" means state `ALERTING` only. WARMING and COOLING minutes are not counted (decided 2026-10-19).
- Template keys only (`service:T…`). `*total`, `*error` and `doc:` keys are not returned (see "Out of scope").
- Module key = ES field `reader.module_field` (default `moduleCode`), already collected; `""` means no module and cannot be queried.
- Rate unit: events **per minute** (the unit of the stats). Open question 5 in the design; change the unit only in Task 3's response if the callers ask for it.
- Input times: ISO-8601 (`2026-10-20T08:00:00Z`, offsets allowed) or epoch seconds. Output: ISO-8601 UTC with `Z`.
- Defaults: `to` = now, `from` = `to − 15 min`. Longest range 24 h.
- Errors are `{"error": code, "message": text}`. 400 `invalid_request` for a missing/empty `module_code`, a bad time, `from >= to`, or a range over 24 h. An unknown module returns 200 with `templates: []`.
- No authentication (same as the rest of the web; revisit before exposing outside the trusted network).
- Queries on `alert_minutes` always carry a `day` range so Postgres prunes partitions (`day` = local day through `timeutil`).

## Review Focus

1. **Template already ALERTING before `from`** (alert started yesterday, still open): it must be returned with its period start before `from`. The latest change before `from` decides the state at `from`. Tested in Task 2.
2. **One module code logged by two services:** the design assumes a module belongs to one service. Core v1's `modules (module_code PK)` would keep only the first service and hide the second service's alerts (the same bug class as logai-engine's `TemplateState.service`). Task 1 makes the key `(service, module_code)`. Tested in Tasks 1 and 2.
3. **The first ALERTING minute must be counted:** `alert_minutes` has to be written from the state *after* the minute's step, and `alert_changes.at` has to be that minute's start. Otherwise the series misses the minute that tripped the alert, or the period is off by one. Pinned in Task 1.
4. **`module_code` with spaces, slashes or Unicode** (`VTN_CNTT/066 é`) goes in the query string, so it must round-trip unchanged. Tested in Task 3.
5. **A range that crosses local midnight** touches two `alert_minutes` partitions. Both days must be read. Tested in Task 2.

---

## File map

```
logai2/migrations/002_modules_per_service.sql   new: modules PK (service, module_code)
logai2/owner/owner.py                           modify: new-module key (service, module)
logai2/alerts.py                                modify: add periods()
logai2/review.py                                modify: suggest() uses periods()
logai2/module_alerts.py                         new: parse_range(), module_alerts()
logai2/web/app.py                               modify: GET /api/module-alerts
scripts/incident_test.py                        modify: module API check
README.md                                       modify: API section
tests/test_owner.py, tests/test_alerts.py, tests/test_module_alerts.py, tests/test_web_module_alerts.py
```

---

### Task 1: Module per service and alert-minute timing in the engine

**Files:**
- Create: `logai2/migrations/002_modules_per_service.sql`
- Modify: `logai2/owner/owner.py` (the known-modules set and the pending `modules` rows)
- Test: `tests/test_owner.py` (append), `tests/test_db.py` (append)

**Interfaces:**
- Consumes: `Owner`, `run_minutes` helper and FakeSource from core v1 Task 9.
- Produces: `modules` primary key `(service, module_code)` plus index `modules_code (module_code)`. The owner's known-module set is keyed `(service, module)`, so a module that shows up under a second service gets a second row. Rows are written only for `module != ""`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_db.py
def test_module_can_belong_to_two_services(conn):
    conn.execute("INSERT INTO services VALUES ('a', 1), ('b', 2)")
    conn.execute("INSERT INTO modules (module_code, service) VALUES ('M1', 'a'), ('M1', 'b')")
    assert conn.execute("SELECT count(*) AS n FROM modules WHERE module_code = 'M1'").fetchone()["n"] == 2

# tests/test_owner.py
def test_module_rows_per_service(conn, cfg):
    # same moduleCode "M1" on service a and service b, plus lines with no module
    run_minutes(conn, cfg, hits, ...)
    rows = conn.execute("SELECT service, module_code FROM modules ORDER BY 1").fetchall()
    assert [(r["service"], r["module_code"]) for r in rows] == [("a", "M1"), ("b", "M1")]

def test_first_alerting_minute_is_in_alert_minutes(conn, cfg):
    # committed baseline mean 1 std 0.5 for s:T1 (ERROR); minutes M..M+4 at 40/min from module M1,
    # detection_enabled, not historic
    run_minutes(conn, cfg, hits, ...)
    ch = conn.execute("SELECT at, to_state FROM alert_changes WHERE key = 's:T1' ORDER BY at").fetchall()
    alerting_at = next(r["at"] for r in ch if r["to_state"] == "ALERTING")
    assert alerting_at == minute_start(M + 2)                     # 3rd high minute, its start
    mins = [r["minute"] for r in conn.execute(
        "SELECT minute FROM alert_minutes WHERE key = 's:T1' AND module_code = 'M1' ORDER BY minute")]
    assert minute_start(M) in mins and minute_start(M + 2) in mins   # first WARMING minute and the ALERTING minute
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_db.py tests/test_owner.py -q`
Expected: FAIL on `test_module_can_belong_to_two_services` (unique violation) and `test_module_rows_per_service`. If `test_first_alerting_minute_is_in_alert_minutes` already passes, the core plan got the timing right; keep the test as a pin.

- [ ] **Step 3: Implement**

`002_modules_per_service.sql`:
```sql
ALTER TABLE modules DROP CONSTRAINT modules_pkey;
ALTER TABLE modules ADD PRIMARY KEY (service, module_code);
CREATE INDEX modules_code ON modules (module_code);
```
In `owner.py`, key the known-modules set and the `ON CONFLICT` target by `(service, module_code)`. If the timing test failed, fix the owner so that `alert_minutes` rows come from `detector.not_normal()` *after* `detector.run(cm, …)`, and `Change.at = cm.minute * 60`.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/pytest tests -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Key modules by service and pin alert-minute timing"
```

---

### Task 2: ALERTING periods and the module query

**Files:**
- Modify: `logai2/alerts.py` (add `periods`), `logai2/review.py` (`suggest` calls `periods`)
- Create: `logai2/module_alerts.py`
- Test: `tests/test_alerts.py` (append), `tests/test_module_alerts.py`

**Interfaces:**
- Consumes: `alert_changes`, `alert_minutes`, `modules`, `templates`, `template_doc_links`, `documents` (core v1 Task 1 + Task 1 above). `timeutil.day_hour`, `timeutil.minute_of` (core Task 1).
- Produces:
  - `periods(changes: list[dict], states: set[str], end: float) -> dict[tuple[str, str], list[tuple[float, float | None]]]` in `alerts.py` (pure). `changes` are `alert_changes` rows (`service, key, at, from_state, to_state`) sorted by `at`, epoch seconds. A period opens at the first change *into* `states` and closes at the next change *out of* `states`. A key whose first change has `from_state` in `states` opens a period at `-inf`. Periods still open at `end` get `None`. `review.suggest` uses it with `{"WARMING", "ALERTING", "COOLING"}`, and its existing tests must still pass.
  - `parse_range(args: Mapping[str, str], now: float) -> tuple[str, float, float]` in `module_alerts.py`: returns `(module_code, start, end)` in epoch seconds and raises `ValueError(message)` for every 400 case in Global Constraints.
  - `module_alerts(conn, module_code: str, start: float, end: float) -> list[dict]`. Each item:
    ```
    {"service", "template_id", "text", "level", "doc_id": str|None, "doc_title": str|None,
     "periods": [{"start": float, "end": float|None}],            # raw period bounds, not clipped to the range
     "alerting_minutes": int,                                     # minutes of periods ∩ [start, end)
     "module":   {"total": int, "avg": float, "peak": int, "series": [{"minute": float, "count": int}]},
     "template": {"total": int, "avg": float, "peak": int}}
    ```

Query rules (`module_alerts`):
1. `services = SELECT service FROM modules WHERE module_code = %s`; none → `[]`.
2. Changes for those services on template keys (`key NOT LIKE '*%%' AND key NOT LIKE 'doc:%%'`): the latest row per `(service, key)` with `at < start` (`DISTINCT ON`), plus every row with `start <= at < end`. Then `periods(rows, {"ALERTING"}, end)`, keeping only periods that overlap `[start, end)`.
3. A minute `m` counts when `max(p.start, start) <= m < min(p.end or end, end)` for one of the key's periods. Read `alert_minutes` for those keys with `minute` in `[start, end)` and `day BETWEEN local day of start AND local day of end`. Sum over all modules gives `template`, rows with `module_code = M` give `module`; drop minutes outside the periods.
4. Leave out a template whose module total is 0. `alerting_minutes` = whole minutes in periods ∩ range (at least 1). `avg = round(total / alerting_minutes, 2)`; `series` holds stored minutes only (missing minutes mean 0). Sort by `module.total` descending, then `template_id`.
5. Text and level come from `templates`, the document from `template_doc_links` + `documents`. A template row missing from `templates` gives `text: ""`, not an error.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_alerts.py
def test_periods_open_closed_and_already_alerting():
    ch = [c("s", "s:T1", 100, "WARMING", "ALERTING"), c("s", "s:T1", 400, "ALERTING", "COOLING"),
          c("s", "s:T2", 50, "ALERTING", "COOLING")]   # first row leaves ALERTING → opened at -inf
    p = periods(ch, {"ALERTING"}, end=1000)
    assert p[("s", "s:T1")] == [(100, 400)]
    assert p[("s", "s:T2")] == [(float("-inf"), 50)]
    assert periods([c("s", "s:T3", 900, "WARMING", "ALERTING")], {"ALERTING"}, 1000)[("s", "s:T3")] == [(900, None)]

# tests/test_module_alerts.py — seed rows directly; t(h, m) = epoch of 2026-10-14 h:m local
def test_module_rates_inside_alerting_period(conn):
    # s:T1 ALERTING 08:02–08:06; alert_minutes 08:01 (WARMING) M1=9, 08:02..08:05 M1=10 M2=5
    items = module_alerts(conn, "M1", t(8, 0), t(8, 15))
    it = items[0]
    assert it["alerting_minutes"] == 4
    assert it["module"] == {"total": 40, "avg": 10.0, "peak": 10, "series": [...4 minutes...]}
    assert it["template"]["total"] == 60                   # 08:01 WARMING minute excluded

def test_started_before_range_and_still_open(conn):
    # NORMAL→…→ALERTING at 07:50, no change after; query 08:00–08:10
    it = module_alerts(conn, "M1", t(8, 0), t(8, 10))[0]
    assert it["periods"] == [{"start": t(7, 50), "end": None}] and it["alerting_minutes"] == 10

def test_excludes_cooling_before_range_and_other_module(conn):
    # s:T2 ALERTING→COOLING at 07:59; s:T3 ALERTING but only M2 minutes → query M1 returns neither
    assert [i["template_id"] for i in module_alerts(conn, "M1", t(8, 0), t(8, 15))] == ["s:T1"]

def test_module_in_two_services(conn):
    # M1 in services a and b, each with one ALERTING template → both returned
    assert {i["service"] for i in module_alerts(conn, "M1", t(8, 0), t(8, 15))} == {"a", "b"}

def test_range_across_local_midnight(conn):
    # ALERTING 23:58 (Oct 14) → 00:03 (Oct 15), M1 rows in both days' partitions
    assert module_alerts(conn, "M1", t(23, 55), t(23, 55) + 600)[0]["module"]["total"] == 5 * 7

def test_unknown_module_and_missing_template_text(conn):
    assert module_alerts(conn, "NOPE", t(8, 0), t(8, 15)) == []
    # alerting key with no templates row → text ""

def test_parse_range():
    now = 1_760_000_000.0
    assert parse_range({"module_code": " M1 "}, now) == ("M1", now - 900, now)
    assert parse_range({"module_code": "M1", "from": "2026-10-20T01:00:00Z", "to": "1792461600"}, now)[1:] \
        == (1792458000.0, 1792461600.0)                  # ISO from, epoch to
    for bad in ({}, {"module_code": ""}, {"module_code": "M", "from": "abc"},
                {"module_code": "M", "from": "200", "to": "100"},
                {"module_code": "M", "from": "0", "to": str(25 * 3600)}):
        with pytest.raises(ValueError):
            parse_range(bad, now)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_alerts.py tests/test_module_alerts.py tests/test_review.py -q`
Expected: FAIL (`ImportError: periods`, `module_alerts`).

- [ ] **Step 3: Implement `periods`, switch `review.suggest` to it, implement `module_alerts.py`.** Parse ISO with `datetime.fromisoformat(v.replace("Z", "+00:00"))` (a naive value is UTC), else `float(v)`. Clip `-inf`/`None` period bounds to the range only when counting minutes; return raw bounds in `periods`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests -q`
Expected: all PASS, including the unchanged `tests/test_review.py`.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Add ALERTING periods and module alert query"
```

---

### Task 3: `GET /api/module-alerts`, docs and the end-to-end check

**Files:**
- Modify: `logai2/web/app.py`, `scripts/incident_test.py`, `README.md`
- Test: `tests/test_web_module_alerts.py`

**Interfaces:**
- Consumes: `parse_range`, `module_alerts` (Task 2); the request-scoped `conn()` and JSON error style from core Task 11.
- Produces: the route. Response 200:

```json
{
  "module_code": "VTN_CNTT_VAS_066_301",
  "from": "2026-10-20T08:00:00Z", "to": "2026-10-20T08:15:00Z",
  "unit": "events_per_minute",
  "templates": [{
    "service": "payment-svc", "template_id": "payment-svc:T00431",
    "text": "Timeout acquiring connection <*>", "level": "ERROR",
    "doc_id": "DOC-017", "doc_title": "Restart DB pool",
    "periods": [{"start": "2026-10-20T07:58:00Z", "end": null}],
    "alerting_minutes": 15,
    "module":   {"total": 300, "avg": 20.0, "peak": 42, "series": [{"minute": "2026-10-20T08:00:00Z", "count": 18}]},
    "template": {"total": 420, "avg": 28.0, "peak": 55}
  }]
}
```
Times go through one helper `iso(ts: float | None) -> str | None`. A `-inf` period start becomes `null`, meaning "before retained history".

- [ ] **Step 1: Write the failing tests** (Flask client on the test schema, seeded as in Task 2)

```python
def test_module_alerts_200_shape(client, conn):
    body = client.get("/api/module-alerts?module_code=M1&from=2026-10-14T01:00:00Z&to=2026-10-14T01:15:00Z").get_json()
    assert body["unit"] == "events_per_minute" and body["from"].endswith("Z")
    assert body["templates"][0]["module"]["total"] == 40

def test_unknown_module_empty_200(client):
    r = client.get("/api/module-alerts?module_code=NOPE")
    assert r.status_code == 200 and r.get_json()["templates"] == []

@pytest.mark.parametrize("qs", ["", "module_code=", "module_code=M&from=abc",
                                "module_code=M&from=200&to=100", "module_code=M&from=0&to=90000"])
def test_bad_input_400(client, qs):
    r = client.get("/api/module-alerts?" + qs)
    assert r.status_code == 400 and r.get_json()["error"] == "invalid_request"

def test_module_code_unicode_and_slash_round_trips(client, conn):
    # seed module "VTN/066 é" → query with urlencoded value → body["module_code"] == "VTN/066 é" and 1 template
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_web_module_alerts.py -q`
Expected: FAIL (404).

- [ ] **Step 3: Implement the route** (catch `ValueError` from `parse_range` → 400). Add a "Module alert API" section to `README.md` with the request, the response above, the error codes and the rule "ALERTING minutes only, per minute". In `scripts/incident_test.py`, each target gains `module` (the moduleCode its injected lines carry). After a burst check passes, call `/api/module-alerts?module_code=<module>&from=<burst start>` and record check **6: the burst template is listed with `module.total > 0`** in the report.

- [ ] **Step 4: Run tests, then on the recharge stack**

Run: `.venv/bin/pytest tests -q` → all PASS.
Then `docker compose up -d --build logai2-web` and `python3 scripts/incident_test.py run && python3 scripts/incident_test.py report`. Expected: check 6 passes for every burst target.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Add GET /api/module-alerts"
```

---

## Spec coverage check

| Design section 7 requirement | Task |
|---|---|
| Same detection, read view filtered by module | 2 |
| module → service | 1 (multi-service), 2 |
| Templates ALERTING in `[from, to]`, state at `from` from the latest earlier change | 2 |
| Module M's rate per minute (series, average, peak) next to the template total | 2, 3 |
| Text and document per template | 2 |
| Alerted but no logs from M → left out | 2 |
| Unknown module 200 empty; bad time / `from >= to` / > 24 h → 400; ISO or epoch in, ISO UTC out | 2, 3 |
| `module_code` only in `alert_minutes`, incident minutes only | already core v1; timing pinned in 1 |

## Out of scope (add when asked)

- **`doc:` and service-wide alerts.** A diffuse alert on a document key has no per-template `alert_minutes` rows. To add it, have the owner also write `alert_minutes` for templates linked to a non-NORMAL `doc:` key, and return a `documents` list next to `templates`.
- WARMING/COOLING in the response, per-second unit, auth, pagination, a web page for the API.
- Normal rate (`mean ± σ`) per template in the response: one `baseline` lookup per item, if callers ask how far the rate is from normal.
