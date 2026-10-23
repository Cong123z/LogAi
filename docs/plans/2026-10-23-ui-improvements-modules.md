# UI improvements and template modules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Alerting links, per-service Pipeline throughput, Templates (first seen, all levels, modules),
exact Baseline counts, LLM call order 1-2-3, and a stored template ↔ module relation that stays fast at scale.

**Architecture:** Phase 1 is web only (Flask routes in `logai2/web/app.py`, vanilla JS views). Phase 2 adds
per-service minute counts to the engine status. Phase 3 adds two tables written by `Owner.flush` in the
cursor transaction (the `hourly_counts` pattern), read by the Templates API, and proven by a stress script.

**Tech Stack:** Python 3.11, Flask, psycopg 3, Postgres 16 (daily partitions), vanilla JS + ECharts, pytest.

**Spec:** docs/specs/2026-10-23-ui-improvements-modules.md (mockup linked there).

## Global Constraints

- Tests: `.venv/bin/pytest -q` against the test Postgres on 127.0.0.1:5433 (`docker compose up -d postgres`). The full suite passes after every task.
- One branch + worktree per task (`feat/<task-slug>`); merge into `master` with `git merge --no-ff` only after the user confirms that branch.
- Stage by explicit path; no `Co-Authored-By` / AI attribution in commits. Never push.
- Never touch `~/Documents/logai-engine`.
- Each task updates the doc it changes: `docs/WEB_UI.md` (pages, HTTP API), `ARCHITECTURE.md` (§5 contracts, §11 schema, §14 limits), `README.md` only for new config/env.
- Level order everywhere: `FATAL, CRITICAL, SEVERE, ERROR, WARNING, WARN, NOTICE, INFO, DEBUG, TRACE`, then unknown levels alphabetically.
- Module window: hourly buckets from (current local hour − 23) to the current hour, local time as `logai2/timeutil.day_hour`.
- `template_module_hours` retention: 7 days (`retention.module_hours_days = 7`). `template_modules`: never deleted.
- Missing module = `''`, shown as "(no module)". Keys ending in `:unparsed` are never written to module tables.
- LLM priority stored as 1..n over active profiles; inactive profiles keep their last priority but are not shown in the order.
- No new dependencies.

## Review Focus

- A service with more than 500 templates: Baseline group counts must be exact (Task 4 test seeds 520).
- A module seen 3 days ago and not since: listed grey with no share, filter still finds the template (Task 10 test `test_old_module_listed_without_share`).
- Engine restart or failed flush in the middle of an hour: module counts are not doubled or lost (Task 9 test `test_module_hours_exact_across_failed_flush`).
- A profile order request naming an unknown or duplicated profile: 400 and nothing changes (Task 5 tests).
- A level present in data but outside the known list (e.g. `ALERT`): listed last and filterable (Task 2 test `test_template_levels_includes_unknown_last`).

---

## Phase 1: web only

### Task 1: Alerting links and deep links

**Files:**
- Modify: `logai2/web/app.py` (`alerts()` route, ~L288)
- Modify: `logai2/web/static/js/views/alerting.js`, `documentation.js`, `templates.js`
- Test: `tests/test_web_alerts_templates.py`

**Interfaces:**
- Produces: each `/api/alerts` row of kind `document` gains `handling_step: str | None` (first non-empty line of the document's Handling with a leading `1.`/`1)`/`-` removed); rows of kind `template`/`cluster` gain `draft_doc_id: str | None` (a `source = 'draft'` link of any of its templates).
- Produces routes (hash): `#documentation/<doc_id>`, `#documentation/suggestions/<doc_id>`, `#templates/<service>/<template_id>` (opens that template's drawer; `state.subView` already carries the part after the first `/`).

- [ ] **Step 1: Write the failing tests**

```python
def test_alert_document_row_has_handling_step(client, conn):
    add_doc(conn, "D1")
    conn.execute("UPDATE documents SET handling = %s WHERE doc_id = 'D1'", ("\n1. Check gateway status\n2. Switch route",))
    add_template(conn, "pay:T1", level="ERROR")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES ('pay', 'pay:T1', 'D1', 'manual')")
    add_state(conn, "pay:T1", "ALERTING", 9)
    conn.commit()
    row = client.get("/api/alerts").get_json()[0]
    assert row["handling_step"] == "Check gateway status"

def test_alert_template_row_has_draft_doc_id(client, conn):
    add_doc(conn, "D9")
    conn.execute("UPDATE documents SET status = 'draft' WHERE doc_id = 'D9'")
    add_template(conn, "pay:T2", level="ERROR")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES ('pay', 'pay:T2', 'D9', 'draft')")
    add_state(conn, "pay:T2", "ALERTING", 9)
    conn.commit()
    row = client.get("/api/alerts").get_json()[0]
    assert row["kind"] == "template" and row["draft_doc_id"] == "D9"
```

- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_web_alerts_templates.py -k "handling_step or draft_doc_id"` — Expected: FAIL (KeyError).
- [ ] **Step 3: Implement** in `alerts()`: one query for `handling` of `doc_ids`, one for draft links of the rows' template keys; add the two fields after `alert_rows(...)`.
- [ ] **Step 4: Front end.** `alerting.js`: header buttons and per-line "chart" links per spec §1 (links are `<a href>`, not buttons). `documentation.js`: on load select the doc named in `state.subView` (`<doc_id>` or `suggestions/<doc_id>`). `templates.js`: `state.subView` = `<service>/<template_id>` → set the service filter, open that template's drawer (fetch it with `/api/templates?service=&q=` if not on the page).
- [ ] **Step 5: Run** the full suite — Expected: PASS. Check in the browser (`run` skill) that each link lands on the right page and item.
- [ ] **Step 6: Docs** `docs/WEB_UI.md` Alerting + `GET /api/alerts` fields; routes list.
- [ ] **Step 7: Commit** `git add logai2/web/app.py logai2/web/static/js/views/alerting.js logai2/web/static/js/views/documentation.js logai2/web/static/js/views/templates.js tests/test_web_alerts_templates.py docs/WEB_UI.md && git commit -m "Alerting: links to documents, baseline and templates; deep links"`

### Task 2: Every level in the filters

**Files:**
- Modify: `logai2/web/app.py` (new route; `documents()` adds `template_level` for drafts)
- Modify: `logai2/web/static/js/views/templates.js` (drop `TPL_LEVELS`), `documentation.js` (level buttons on AI suggestions)
- Test: `tests/test_web_alerts_templates.py`, `tests/test_web_docs_baseline.py`

**Interfaces:**
- Produces: `GET /api/template-levels?service=<s>` → `[{"level": "ERROR", "n": 12}, ...]` in the Global Constraints order; `service` optional.
- Produces: `/api/documents?status=draft` rows gain `template_level: str | None` (level of the template the draft was written for, via its `source = 'draft'` link).
- Produces: `LEVEL_ORDER: tuple[str, ...]` in `logai2/links.py` (the order above), reused by Task 4.

- [ ] **Step 1: Write the failing tests**

```python
def test_template_levels_includes_unknown_last(client, conn):
    for tid, lvl in (("pay:A", "INFO"), ("pay:B", "ERROR"), ("pay:C", "ALERT"), ("pay:D", "DEBUG"), ("pay:E", "ERROR")):
        add_template(conn, tid, level=lvl)
    conn.commit()
    assert client.get("/api/template-levels?service=pay").get_json() == [
        {"level": "ERROR", "n": 2}, {"level": "INFO", "n": 1}, {"level": "DEBUG", "n": 1}, {"level": "ALERT", "n": 1}]

def test_draft_documents_carry_template_level(client, conn):
    # draft D5 linked (source 'draft') to template pay:W of level WARNING
    assert next(d for d in client.get("/api/documents?status=draft").get_json() if d["doc_id"] == "D5")["template_level"] == "WARNING"
```

- [ ] **Step 2: Run** them — Expected: FAIL (404 / KeyError).
- [ ] **Step 3: Implement** `SELECT level, count(*) ... GROUP BY level`, sorted in Python by `(LEVEL_ORDER.index(l) if l in LEVEL_ORDER else len(LEVEL_ORDER), l)`; the documents query LEFT JOINs the draft link and `templates`.
- [ ] **Step 4: Front end.** Templates level `<select>` filled from `/api/template-levels` (refetched when the service changes) with "LEVEL (n)". AI suggestions: a second toggle group "Any level · <level> n …" built from the drafts' `template_level`, combined with the All/ERROR/SAFE filter.
- [ ] **Step 5: Run** the full suite — Expected: PASS.
- [ ] **Step 6: Docs** `docs/WEB_UI.md` Templates/Documentation filters + the new route.
- [ ] **Step 7: Commit** (explicit paths as in Task 1) `"Filters list every level present in the data"`

### Task 3: Templates first seen sort and service search

**Files:**
- Modify: `logai2/web/app.py` (`templates()`), `logai2/web/static/js/views/templates.js`, `logai2/web/static/index.html` (service input + datalist)
- Test: `tests/test_web_alerts_templates.py`

**Interfaces:**
- Produces: `/api/templates?sort=first_seen|last_seen` (default `last_seen`; `first_seen` orders `t.first_seen DESC, t.template_id`; ignored in `link_source=review`); any other value → 400.

- [ ] **Step 1: Write the failing tests**

```python
def test_templates_sort_first_seen(client, conn):
    # A first seen 10-19, last seen 10-22; B first seen 10-21, last seen 10-20
    assert [i["template_id"] for i in client.get("/api/templates?sort=first_seen").get_json()["items"]] == ["pay:B", "pay:A"]

def test_templates_sort_rejects_unknown(client):
    assert client.get("/api/templates?sort=rate").status_code == 400
```

- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** the `sort` argument next to `order`.
- [ ] **Step 4: Front end.** First seen column (relative, full time on hover) with a sortable header (`aria-sort`); clicking Last seen / First seen switches `sort`. Service filter: `<input list>` + `<datalist>` from `/api/services`; an exact match applies the filter, empty = all services.
- [ ] **Step 5: Run** the full suite — Expected: PASS.
- [ ] **Step 6: Docs** `docs/WEB_UI.md` Templates columns, `sort` parameter.
- [ ] **Step 7: Commit** `"Templates: first seen column and sort, service search"`

### Task 4: Baseline exact group counts

**Files:**
- Modify: `logai2/web/app.py` (new route), `logai2/web/static/js/keypicker.js`, `views/baseline.js`, `views/review.js`
- Test: `tests/test_web_docs_baseline.py`

**Interfaces:**
- Consumes: `class_sql`, `ALERTING_SOURCES` (`logai2/links.py`).
- Produces: `GET /api/baseline/groups?service=<s>` (required; 400 without) → `{"all": n, "ERROR": n, "WARN": n, "INFO": n, "action": n, "safe": n}`: templates per level class; `action`/`safe` = active documents of that verdict (of this service or shared) with at least one template of this service linked by an `ALERTING_SOURCES` link — the definition `keypicker.js` uses today.

- [ ] **Step 1: Write the failing test**

```python
def test_baseline_groups_exact_over_500(client, conn):
    for n in range(520):
        add_template(conn, f"pay:E{n}", level="ERROR")
    add_template(conn, "pay:W1", level="WARNING")
    conn.commit()
    g = client.get("/api/baseline/groups?service=pay").get_json()
    assert g["all"] == 521 and g["ERROR"] == 520 and g["WARN"] == 1 and g["INFO"] == 0
```

plus `test_baseline_groups_verdict_counts_linked_active_docs` (one active `safe` doc linked manually, one draft-linked action doc not counted → `safe == 1, action == 0`).

- [ ] **Step 2: Run** — Expected: FAIL (404).
- [ ] **Step 3: Implement** one `count(*) FILTER` query for the classes and one for the verdicts.
- [ ] **Step 4: Front end.** `keypicker.js`: button counts from `st.groups` (set by the pages from the new route); drop `KP_CAP` and the `+`. In the Show list, when the service has more templates than `st.templates` holds, typing queries `/api/templates?service=&q=&size=50` (debounced 300 ms) and the list footer says "Showing N of M".
- [ ] **Step 5: Run** the full suite — Expected: PASS.
- [ ] **Step 6: Docs** `docs/WEB_UI.md` Baseline key picker + route.
- [ ] **Step 7: Commit** `"Baseline: exact key picker counts from the server"`

### Task 5: LLM profiles call order

**Files:**
- Modify: `logai2/web/app.py` (new route; default priority on create), `logai2/web/static/js/views/llm.js`, `logai2/web/static/index.html`, `logai2/web/static/css/app.css`
- Test: `tests/test_web_llm.py`

**Interfaces:**
- Consumes: `llm.active_profiles` order (`ORDER BY priority, name`, `logai2/llm.py:28`) — unchanged.
- Produces: `PUT /api/llm/profiles/order` body `{"order": ["a", "b"]}` → listed profiles `active = true, priority = 1..n` in that order, every other profile `active = false`; one transaction; returns `{"order": [...]}`. 400 when `order` is not a list of strings, has duplicates, or names an unknown profile (nothing changes).
- Produces: a new profile (PUT without `priority`) gets `max(priority of active) + 1` (1 if none).

- [ ] **Step 1: Write the failing tests**

```python
def put_profile(client, name):   # new helper in this file
    assert client.put(f"/api/llm/profiles/{name}", json={"base_url": "http://x/v1", "model": "m", "api_key": "k"}).status_code == 200

def test_profile_order_sets_priority_and_active(client, conn):
    for name in ("a", "b", "c"):
        put_profile(client, name)
    assert client.put("/api/llm/profiles/order", json={"order": ["c", "a"]}).status_code == 200
    rows = {r["name"]: (r["active"], r["priority"]) for r in client.get("/api/llm/profiles").get_json()}
    assert rows["c"] == (True, 1) and rows["a"] == (True, 2) and rows["b"][0] is False

def test_profile_order_rejects_unknown_and_duplicates(client):
    put_profile(client, "a")
    assert client.put("/api/llm/profiles/order", json={"order": ["a", "zz"]}).status_code == 400
    assert client.put("/api/llm/profiles/order", json={"order": ["a", "a"]}).status_code == 400
```

plus `test_new_profile_priority_goes_last`.

- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** the route and the default.
- [ ] **Step 4: Front end** per the mockup board "v2 LLM profiles": status line + Add profile; **Call order** list (number badge, name, priority n, model · endpoint · key hint, IN USE / STANDBY / FAILING · skipped until hh:mm from the linker status, ↑/↓, Edit, Stop using; "↓ if it fails, call #n+1" between items); **Not used** list (Add as #n, Edit, Delete); edit form without a priority field (order is set by the list); 24 h usage line. Every move saves the whole order with the new route.
- [ ] **Step 5: Run** the full suite — Expected: PASS. Browser check: reorder, stop, add back.
- [ ] **Step 6: Docs** `docs/WEB_UI.md` LLM profiles + route; `ARCHITECTURE.md` LLM failover sentence (priority 1 first).
- [ ] **Step 7: Commit** `"LLM profiles: call order 1, 2, 3 with fallback"`

## Phase 2: Pipeline throughput

### Task 6: Engine reports events per minute per service

**Files:**
- Modify: `logai2/owner/owner.py` (close loop), `logai2/engine.py` (`_status`)
- Test: `tests/test_owner.py`, `tests/test_engine.py`

**Interfaces:**
- Produces: `Owner.service_minutes: dict[str, collections.deque[tuple[int, int]]]` — per service `(minute, n)` of `*total` for each closed minute, `maxlen=60`; filled in the close loop for every closed minute (also before detection is enabled).
- Produces: engine status key `"service_minutes": {service: [[minute_start_epoch_s, n], ...]}` oldest first.

- [ ] **Step 1: Write the failing tests** — `test_service_minutes_keeps_last_60_closed_minutes` (feed 61 minutes for one service through `tick`; deque length 60, last item `(M+60, n)`), and in `test_engine.py` `test_status_has_service_minutes` (key present, epoch seconds = minute × 60).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite — Expected: PASS.
- [ ] **Step 5: Docs** `ARCHITECTURE.md` §5 engine status.
- [ ] **Step 6: Commit** `"Engine: per-service events per closed minute in the status"`

### Task 7: Pipeline page: pick services, throughput chart

**Files:**
- Modify: `logai2/web/pipeline_view.py` (remove `volume()`; add `throughput()`; `services()` drops `rate`/`last_week`/`z`), `logai2/web/app.py` (`pipeline()`), `logai2/web/static/js/views/pipeline.js`, `index.html`, `app.css`
- Test: `tests/test_web_pipeline_view.py`

**Interfaces:**
- Consumes: status key `service_minutes` (Task 6).
- Produces: `throughput(engine: dict) -> dict[str, dict]` → per service `{"points": [[t, n], ...], "last": n | None, "avg15": float | None, "delta_1h": float | None}`; `delta_1h` = last / first point − 1 when the first point is 55–60 minutes older and > 0, else None. `/api/pipeline` gains `throughput`, loses `volume`; `services` rows gain `last`, `avg15`, `delta_1h`.

- [ ] **Step 1: Write the failing tests** — `test_throughput_last_avg_delta` (60 points 100..159: `last == 159`, `avg15 == mean(145..159)`, `delta_1h == 159/100 - 1`), `test_throughput_short_history_has_no_delta` (10 points → `delta_1h is None`), `test_pipeline_api_has_no_volume`.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement**; delete `volume()` and its tests.
- [ ] **Step 4: Front end** per spec §2: search box, checkbox column (max 8; the 9th is refused with a short message), default 5 busiest by `avg15`, combined/small-multiples toggle, `#pipeline?services=` + localStorage (`try/catch`), ECharts one line per service.
- [ ] **Step 5: Run** the full suite — Expected: PASS. Browser check against the local engine.
- [ ] **Step 6: Docs** `docs/WEB_UI.md` Pipeline + `/api/pipeline`.
- [ ] **Step 7: Commit** `"Pipeline: per-service throughput, chosen services"`

## Phase 3: template modules

### Task 8: Migration 007 and retention

**Files:**
- Create: `logai2/migrations/007_template_modules.sql`
- Modify: `logai2/db.py` (`PARTITIONED`), `logai2/config.py` (`RetentionConfig.module_hours_days: int = 7`), `logai2/jobs/commit.py` (drop `template_module_hours` partitions before `today - module_hours_days`)
- Test: `tests/test_db.py`, `tests/test_commit.py`

**Interfaces:**
- Produces tables:
  - `template_module_hours (service text, template_id text, module_code text, day date, hour smallint, count bigint, PRIMARY KEY (service, template_id, module_code, day, hour)) PARTITION BY RANGE (day)`
  - `template_modules (service text, template_id text, module_code text, first_seen timestamptz, last_seen timestamptz, PRIMARY KEY (service, template_id, module_code))` + `CREATE INDEX template_modules_module ON template_modules (service, module_code)`
- The migration creates partitions for the days present in `alert_minutes`, then seeds: hours from `alert_minutes` rows of the last 7 days (`sum(count)` per hour, local hour as `hourly_counts` uses), pairs from all `alert_minutes` (`min/max(minute)`), skipping `key LIKE '*%'` and `doc:%` keys.

- [ ] **Step 1: Write the failing tests** — `test_migration_007_seeds_from_alert_minutes` (insert alert_minutes, re-run the 007 SQL in the test schema; one pair with first/last seen = min/max minute, hourly sum correct, `*total` rows skipped); `test_commit_drops_module_hours_after_7_days` (partition of today−8 gone, today−6 kept, `hourly_counts` of today−8 kept).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite — Expected: PASS.
- [ ] **Step 5: Docs** `ARCHITECTURE.md` §11 schema, §16 migration list; `README.md` retention config.
- [ ] **Step 6: Commit** `"Migration 007: template modules, hourly per module, 7-day retention"`

### Task 9: Engine writes module counts

**Files:**
- Modify: `logai2/owner/owner.py`
- Test: `tests/test_owner.py`

**Interfaces:**
- Consumes: tables of Task 8; `cm.modules` (`counts.py:51`).
- Produces: `Owner._mod_hours: dict[tuple[str, str, str, date, int], int]` and `Owner._pairs: dict[tuple[str, str, str], list[int]]` (`[first_minute, last_minute]`), both filled in the close loop **before** the `detection_enabled` check, cleared only after a successful flush (like `_sums`); SQL keys `"template_module_hours"` (upsert `count = count + EXCLUDED.count`) and `"template_modules"` (upsert `first_seen = LEAST`, `last_seen = GREATEST`), executed in the flush transaction.

- [ ] **Step 1: Write the failing tests** (pattern: `make`/`tick` in `tests/test_owner.py`; `hit(...)` takes the module field)
  - `test_module_hours_and_pairs_written` — 3 lines of one template from modules `A`, `A`, `B` in one minute → hours rows `A: 2`, `B: 1`; two pairs.
  - `test_module_hours_exact_across_failed_flush` — make one flush fail the way `test_flush_failure_keeps_pending` does (monkeypatch), retry, restart with `make` → counts equal a run without failure. Lines carry the module as `moduleCode=` in `lines(...)`/`hit(...)` (`cfg.reader.module_field` default).
  - `test_missing_module_stored_as_empty` and `test_unparsed_not_stored`.
  - `test_module_hours_written_before_detection_enabled`.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite — Expected: PASS.
- [ ] **Step 5: Docs** `ARCHITECTURE.md` flush section.
- [ ] **Step 6: Commit** `"Engine: store template modules per hour"`

### Task 10: Modules in the API

**Files:**
- Create: `logai2/web/modules_view.py`
- Modify: `logai2/web/app.py` (`templates()` filter + items; new route)
- Test: `tests/test_web_modules.py`

**Interfaces:**
- Produces: `window_hours(now: float) -> list[tuple[date, int]]` — the 24 `(day, hour)` buckets of the window, local time.
- Produces: `modules_for(conn, keys: list[tuple[str, str]], now: float) -> dict[tuple[str, str], list[dict]]` — per template `[{"module": str, "lines": int, "share": float | None, "last_seen": float}]`, in-window modules by lines desc, then out-of-window by last_seen desc; `share` None out of the window. One query on `template_modules` + one on `template_module_hours` restricted to the window's days.
- Produces: `/api/templates?module=<m>` → `EXISTS (SELECT 1 FROM template_modules m WHERE m.service = t.service AND m.template_id = t.template_id AND m.module_code = %s)`; with `module`, order puts templates with window lines in that module first. Each item gains `modules`.
- Produces: `GET /api/modules?service=<s>` (required) → `[{"module": str, "templates": n, "lines_24h": n}]` by `templates` desc.

- [ ] **Step 1: Write the failing tests**
  - `test_window_hours_crosses_midnight` — now = 01:30 local → 24 buckets, first `(yesterday, 2)`, last `(today, 1)`.
  - `test_modules_share_in_window` — A 30 lines, B 10 lines in window → shares 0.75 / 0.25.
  - `test_old_module_listed_without_share` — C last seen 3 days ago, no hours in window → listed last, `share is None`, and `?module=C` still returns the template.
  - `test_empty_module_listed` — `''` module returned as `""`.
  - `test_api_modules_counts`.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite — Expected: PASS.
- [ ] **Step 5: Docs** `docs/WEB_UI.md` HTTP API (`module`, `modules`, `/api/modules`); `docs/API.md` short section for `/api/modules` (Vietnamese, as that file).
- [ ] **Step 6: Commit** `"API: template modules over the last 24 h, filter by module"`

### Task 11: Templates page modules UI

**Files:**
- Modify: `logai2/web/static/js/views/templates.js`, `index.html`, `app.css`

**Interfaces:**
- Consumes: Task 10 (`modules` per item, `module` filter, `/api/modules`).

- [ ] **Step 1: Implement** per mockup board "v2 Templates": column header "Modules · last 24 h" with every module as a chip `name share%` (filtered module first and highlighted; out-of-window chips grey dashed, no %, `title="last seen …"`; `""` as "(no module)"); MODULE filter row from `/api/modules` for the selected service (hidden for All services); the note line above the table (spec §6 text); drawer section "Last 24 h · N modules" with lines, share bar and last seen per module.
- [ ] **Step 2: Run** the full suite — Expected: PASS. Browser check with the local stack (seed modules if the stack has none).
- [ ] **Step 3: Docs** `docs/WEB_UI.md` Templates.
- [ ] **Step 4: Commit** `"Templates: modules per template, module filter"`

### Task 12: Stress test at scale

**Files:**
- Create: `scripts/stress_modules.py`, `docs/perf/2026-10-23-template-modules.md`
- Test: `tests/test_stress_modules.py` (runs the script at `--scale tiny` only)

**Interfaces:**
- Produces: `python scripts/stress_modules.py --dsn <dsn> --services 50 --templates 2000 --modules 3 --active 0.3 --days 7 [--keep]` — creates a throwaway schema, migrates, loads with `COPY`, measures, prints a markdown table, drops the schema unless `--keep`.

Default scale ("prod ×3"): 50 services × 2,000 templates = 100k templates; 3 modules each = 300k pairs; 30 % of pairs active per hour → 90k rows/hour × 168 h ≈ 15M `template_module_hours` rows; 1M `alert_minutes` rows.

Measurements and targets (p95 of 20 runs unless noted):

| Measure | Target |
|---|---|
| Flush of the two module upserts, 20k pairs in one transaction (10 runs) | ≤ 1.0 s |
| Same with 60k pairs (catch-up page) | ≤ 3.0 s |
| `GET /api/templates?service=s&size=100` | ≤ 300 ms |
| `GET /api/templates?service=s&size=500` | ≤ 800 ms |
| `GET /api/templates?service=s&module=m&size=100` | ≤ 500 ms |
| `GET /api/modules?service=s` | ≤ 300 ms |
| Retention: drop one day partition | ≤ 2 s |
| Migration 007 seed from 1M `alert_minutes` rows (once) | ≤ 60 s |
| Table + index size of 7 days (reported, no target) | — |

- [ ] **Step 1: Write the failing test** `test_stress_tiny_runs` — runs `main(["--dsn", TEST_DSN, "--scale", "tiny"])`, asserts every measure row is present and numeric.
- [ ] **Step 2: Run** — Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the script; also print `EXPLAIN (ANALYZE, BUFFERS)` of the window query and the module filter, and fail (exit 1) when a target is missed.
- [ ] **Step 4: Run** `.venv/bin/python scripts/stress_modules.py --dsn postgresql://logai:logai@localhost:5433/logai_test` — Expected: every target met. If the flush target is missed: switch the two upserts to `COPY` into a temp table + `INSERT … SELECT … ON CONFLICT` (the ponytail note in `owner.py`); if a query target is missed: fix the index or query using the EXPLAIN output; rerun.
- [ ] **Step 5: Record** results, machine, Postgres version and EXPLAIN plans in `docs/perf/2026-10-23-template-modules.md`; add the measured limits to `ARCHITECTURE.md` §14.
- [ ] **Step 6: Commit** `"Stress test for template modules, results"`

## After the build

- [ ] Every task merged into `master` on the user's go; full suite on the merged result.
- [ ] `graphify update .` in `~/Documents/logai2`.
- [ ] `agents.md` entry; `docs/BUGS.md` for anything found and not fixed.
- [ ] Deploy order: engine (migration 007) → web → linker unchanged.
