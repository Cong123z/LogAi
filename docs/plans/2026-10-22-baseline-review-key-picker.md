# Grouped key picker and combined series — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Baseline and Review pick what to show with one shared two-step picker (group, then combined series / document / template), backed by new view-only series `*warn`, `*info`, `verdict:action`, `verdict:safe`.

**Architecture:** `*warn` / `*info` are stored keys counted per line beside `*error` in `owner/counts.py`; the existing flush and nightly commit store and baseline them, the detector skips them. `verdict:<v>` keys are derived in `web/baseline_view.resolve_key` as kind `doc` over the service's templates linked to active documents of that verdict, so every existing `doc` code path serves them. The front end gets one `keypicker.js` used by `baseline.js` and `review.js`.

**Tech Stack:** Python 3.11, psycopg 3, Flask, Postgres (test DB on 127.0.0.1:5433), vanilla JS, pytest.

**Spec:** `docs/specs/2026-10-22-baseline-review-key-picker.md` (approved; read it first).

## Global Constraints

- New series are **view only**: never an `alert_state` row, nothing on Alerting.
- Key names exactly: `*warn`, `*info`, `verdict:action`, `verdict:safe`.
- Level classes come from `logai2.links.level_class` (ERROR = ERROR/FATAL/CRITICAL, WARN = WARN/WARNING, INFO = everything else); JS mirrors it exactly.
- "Linked" = link source in `ALERTING_SOURCES` (`manual`, `inherited`, `corpus`, `llm`) and document `status = 'active'`.
- No schema change, no migration, no new HTTP endpoint, no JS dependency.
- Routes stay `#baseline/<service>/<key>/<days>` and `#review/<service>/<key>/<days>[/…]`; old links keep working.
- Tests: `.venv/bin/pytest -q` (needs `docker compose up -d postgres`). Stage by explicit path; no AI attribution in commits.
- One branch + worktree for the build (`key-picker`); merge into `master` with `--no-ff`. Add an `agents.md` entry. After the merge run `graphify update .` in `~/Documents/logai2`.

## Review Focus

1. A service with **no SAFE (or no ERROR) documents linked**: `verdict:safe` must 404 like an empty `doc:` key, and the group button must be disabled with count 0, not open an error page. (Tests: Task 2 `test_verdict_key_unknown_or_empty_404`, Task 3 Chrome step 6.)
2. A **shared document** (`service IS NULL`) linked to templates of two services: `verdict:<v>` for one service must sum only that service's templates. (Task 2 `test_verdict_key_resolves_linked_templates_of_this_service`.)
3. **Draft / rejected links and draft documents** must not join a verdict key. (Same Task 2 test.)
4. **Lower-case or variant levels** (`warning`, `Fatal`, `notice`) land in the same group in the picker as in the counts. (Task 1 counts test; Task 3 `levelClass` mirrors `level_class`, Chrome step 5.)
5. **Old bookmarks**: `#review/<svc>/doc:DOC-021/14`, `#baseline/<svc>/<template>/7` and `#review/<svc>/*error/14` open with the matching group pressed and that key shown. (Task 3 Chrome step 4.)

---

### Task 1: Engine counts `*warn` / `*info`, detector skips them

**Files:**
- Modify: `logai2/owner/counts.py` (`OpenMinutes.add`, `close_before`)
- Modify: `logai2/owner/detect.py` (`VIEW_ONLY_KEYS`, `Detector.run`)
- Modify: `ARCHITECTURE.md` (the service-key list: four stored keys, view-only rule)
- Test: `tests/test_counts.py`, `tests/test_detect.py`

**Interfaces:**
- Produces: `close_before()` minutes always carry `(service, "*warn")` and `(service, "*info")` (0 when silent) for every service that gets `*total`. `logai2.owner.detect.VIEW_ONLY_KEYS: frozenset[str] = frozenset({"*warn", "*info"})`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_counts.py`:

```python
def test_level_class_keys_add_up_to_total():
    om = OpenMinutes(M)
    for level, n in (("ERROR", 1), ("Fatal", 2), ("WARN", 3), ("warning", 4), ("INFO", 5), ("debug", 6), ("notice", 7)):
        om.add("s", f"s:T{n}", level, "", M, n)
    c, silent = om.close_before(M + 2, ["s", "t"])
    k = c.keys
    assert (k[("s", "*error")], k[("s", "*warn")], k[("s", "*info")]) == (3, 7, 18)
    assert k[("s", "*error")] + k[("s", "*warn")] + k[("s", "*info")] == k[("s", "*total")] == 28
    for key in ("*total", "*error", "*warn", "*info"):
        assert silent.keys[("s", key)] == 0 and c.keys[("t", key)] == 0
```

In `tests/test_detect.py`:

```python
def test_view_only_keys_never_alert(cfg):
    det = Detector(cfg, Baseline({}, {}), {}, {("s", "*warn"): "WARN", ("s", "*info"): "ERROR"}, {})
    for m in range(10):
        det.run(cm(m, {("s", "*warn"): 10_000, ("s", "*info"): 10_000}), historic=False)
    assert det.states == {} and det.touched == set()
```

(The levels map deliberately gives them alerting levels: only the skip keeps them out.)

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest -q tests/test_counts.py tests/test_detect.py`
Expected: both new tests FAIL (`KeyError: ('s', '*warn')`; `det.states` not empty).

- [ ] **Step 3: Implement**

- `counts.py`: in `add`, replace the `if level.upper() in ERROR_LEVELS` branch with one add to `"*" + level_class(level).lower()` (import `level_class` from `logai2.links`). Keep `ERROR_LEVELS` and `SEVERITY` (other modules import them). In `close_before`, zero-fill `*warn` and `*info` beside `*total` / `*error`.
- `detect.py`: add `VIEW_ONLY_KEYS` and `continue` on `key in VIEW_ONLY_KEYS` at the top of the `for (svc, key), count in keys.items()` loop.
- `ARCHITECTURE.md`: where `*total` / `*error` are described, list `*warn` / `*info` (stored, per line level class, view only, no backfill) and the derived `verdict:` keys (Task 2).

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all pass. If an existing test lists the exact key set of a closed minute or of `hourly_counts` rows (look in `tests/test_owner.py`, `tests/test_engine.py`, `tests/test_counts.py`), add the two new keys to its expectation; change no other assertion.

- [ ] **Step 5: Commit**

```bash
git add logai2/owner/counts.py logai2/owner/detect.py ARCHITECTURE.md tests/test_counts.py tests/test_detect.py
git commit -m "Engine: count *warn and *info per line beside *error; view only, the detector skips them"
```
(add any test file changed in Step 4 by path)

---

### Task 2: Web resolves `verdict:` keys and serves the new keys

**Files:**
- Modify: `logai2/web/baseline_view.py` (`SERVICE_KEYS`, `resolve_key`, `incident`)
- Test: `tests/test_web_baseline_view.py`

**Interfaces:**
- Consumes: `*warn` / `*info` rows in `hourly_counts` and `baseline` (Task 1; tests insert them directly).
- Produces: `SERVICE_KEYS = ("*total", "*error", "*warn", "*info")`. `resolve_key(conn, service, "verdict:<v>") -> ("doc", [template ids, sorted])` for `v in ("action", "safe")`, else `UnknownKey`. `/api/baseline/{series,matrix,inspect,incident}` accept all six group keys.

- [ ] **Step 1: Write the failing tests** (use the file's `service`, `hours`, `base`, `committed`, `cell`, `WEEKDAYS`, `TODAY`, `NOW` helpers)

```python
def _verdict_fixture(conn):
    service(conn, "pay"); service(conn, "web")
    for doc, verdict, status, svc in (("D-SAFE", "safe", "active", None), ("D-ACT", "action", "active", "pay"),
                                      ("D-DRAFT", "safe", "draft", "pay")):
        conn.execute("INSERT INTO documents (doc_id, title, handling, revision, verdict, status, service) "
                     "VALUES (%s, 't', 'h', 'r', %s, %s, %s)", (doc, verdict, status, svc))
    rows = [("pay", "pay:T1", "D-SAFE", "manual"), ("pay", "pay:T2", "D-SAFE", "corpus"),
            ("pay", "pay:T3", "D-SAFE", "draft"), ("pay", "pay:T4", "D-ACT", "llm"),
            ("pay", "pay:T5", "D-DRAFT", "manual"), ("web", "web:T1", "D-SAFE", "manual")]
    for svc, tid, doc, source in rows:
        conn.execute("INSERT INTO templates VALUES (%s, %s, 'INFO', 'x', now(), now())", (svc, tid))
        conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) VALUES (%s, %s, %s, %s)",
                     (svc, tid, doc, source))
    conn.commit()


def test_verdict_key_resolves_linked_templates_of_this_service(conn):
    _verdict_fixture(conn)
    assert resolve_key(conn, "pay", "verdict:safe") == ("doc", ["pay:T1", "pay:T2"])   # not the draft link, not web, not the draft doc
    assert resolve_key(conn, "pay", "verdict:action") == ("doc", ["pay:T4"])


def test_verdict_key_unknown_or_empty_404(client, conn):
    _verdict_fixture(conn)
    for key in ("verdict:nope", "verdict:", "verdict:action"):
        svc = "web" if key == "verdict:action" else "pay"      # web has no action-linked template
        assert client.get(f"/api/baseline/series?service={svc}&key={key}").status_code == 404


def test_verdict_key_sums_like_a_document(conn, cfg):
    _verdict_fixture(conn)
    committed(conn, "pay", WEEKDAYS)
    base(conn, "pay", "pay:T1", "weekday", 2, 1, 0.5)
    base(conn, "pay", "pay:T2", "weekday", 2, 0.5, 0.5)
    hours(conn, "pay", "pay:T1", date(2026, 10, 20), {2: (120, 0, 60)})
    hours(conn, "pay", "pay:T2", date(2026, 10, 20), {2: (60, 0, 60)})
    h = cell(matrix(conn, cfg, "pay", "verdict:safe", 14, TODAY, NOW), "2026-10-20", 2)
    assert (h["rate"], h["mean"], h["std"]) == (3.0, 1.5, 1.0)


def test_level_keys_are_service_keys(client, conn, cfg):
    service(conn, "pay")
    committed(conn, "pay", WEEKDAYS)
    base(conn, "pay", "*warn", "weekday", 2, 2, 1)
    hours(conn, "pay", "*warn", date(2026, 10, 20), {2: (240, 0, 60)})
    assert cell(matrix(conn, cfg, "pay", "*warn", 14, TODAY, NOW), "2026-10-20", 2)["rate"] == 4.0
    assert series(conn, cfg, "pay", "*warn", 14, TODAY, NOW)["baseline_state"] == "full"
    assert client.get("/api/baseline/series?service=pay&key=*info").status_code == 200   # no rows yet: empty, not 404
```

Plus, extending `test_incident_modules_and_samples_only_when_ongoing` (its `seed_series` has ERROR templates only): add

```python
    warn = incident(conn, "pay", "*warn", date(2026, 10, 21), 13, 15, NOW)
    assert warn["modules"] == [] and warn["samples"] == []        # no WARN templates: no modules, never ERROR samples
```

Import `resolve_key` at the top of the test file.

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest -q tests/test_web_baseline_view.py`
Expected: the four new tests FAIL (`UnknownKey` / 404 / `*warn` not a service key); the extended incident test FAILS (`*warn` gets every template's modules).

- [ ] **Step 3: Implement**

- `SERVICE_KEYS` gets `*warn`, `*info` (update its comment).
- `resolve_key`: a `verdict:` branch before the `doc:` one. Query: the service's `template_doc_links` with `source = ANY(ALERTING_SOURCES)` joined to `documents` with `status = 'active' AND verdict = %s`, ordered by `template_id`; `UnknownKey` when `v not in ("action", "safe")` or no row.
- `incident`: for `*total` keep every template; for `*error` / `*warn` / `*info` select templates whose `class_sql('level')` equals the key's class (`"*warn"[1:].upper()`). Samples: `_top_excess` only for `*total` / `*error`; for `*warn` / `*info`, `tids = []`.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add logai2/web/baseline_view.py tests/test_web_baseline_view.py
git commit -m "Web: verdict:action / verdict:safe derived keys; *warn and *info served as service keys"
```

---

### Task 3: Shared key picker on Baseline and Review

**Files:**
- Create: `logai2/web/static/js/keypicker.js`
- Modify: `logai2/web/static/index.html` (Baseline and Review headers; script tag before `views/baseline.js`)
- Modify: `logai2/web/static/js/views/baseline.js` (`blRenderControls`, `blKeyName`, `bl-key` listener)
- Modify: `logai2/web/static/js/views/review.js` (`rvRenderControls`, key name, `rv-key` / `rv-tpl` listeners)
- Modify: `logai2/web/static/css/app.css` (`.kp-*` styles)
- Modify: `docs/WEB_UI.md` (Baseline and Review headers, the picker, view-only note)
- Reference: approved mockup `/tmp/claude-1000/-home-cong-Documents-logai2/048d1637-4b7c-4f26-92c0-1a14cad13176/scratchpad/mockup/index.html` — its markup, CSS and option-building logic are the starting point; copy and adapt, keep its behaviour.

**Interfaces:**
- Consumes: Task 2 keys over the existing `/api/baseline/*` routes; `/api/templates?service=…&size=500` items (`template_id`, `level`, `text`, `doc_id`, `doc_verdict`, `link_source`, `last_seen`); `/api/documents` rows (`doc_id`, `title`, `verdict`, `service`, `linked_templates`).
- Produces (global, like the other view files):
  - `levelClass(level: string) -> 'ERROR'|'WARN'|'INFO'` — same sets as `links.level_class`.
  - `keyGroup(key: string, templates, docs) -> 'all'|'ERROR'|'WARN'|'INFO'|'action'|'safe'` — `*total`→all, `*error|*warn|*info`→level, `verdict:v`→v, `doc:id`→that doc's verdict (unknown doc → `all`), template id→`levelClass` of its level (unknown → `all`).
  - `keyLabel(key, templates, docs) -> string` — `All events`, `All ERROR lines combined`, `All WARN lines combined`, `All INFO lines combined`, `All templates of ERROR documents`, `All templates of SAFE documents`, `DOC-021 · title`, `pay:T1 · text`.
  - `createKeyPicker(root: HTMLElement, {onPick(key: string)}) -> {render({service, key, templates, docs})}` — builds the group buttons, Show button, popover and the "Showing …" line inside `root`; calls `onPick` on a group click (with the group's combined key) or an option pick.

- [ ] **Step 1: Write `keypicker.js`**

Group order, labels, keys and copy exactly as the spec's Picker section and the mockup. Counts: templates in the group for All events and levels; documents of that verdict (active, `service` equal or null) for the documentation groups. A group with count 0 renders `disabled` with `title="Nothing in this group for <service>"`. Linked-template test for the documentation groups and the Documents section: `link_source` in `['manual','inherited','corpus','llm']` and `doc_verdict === v`. Templates newest `last_seen` first. Keyboard and ARIA as in the mockup (combobox + listbox, ↑/↓/Enter/Esc, focus back to the Show button). The "how" text: `stored key` / `new stored key: young baseline, full after 5 weekdays + 4 weekend days` (for `*warn`/`*info` while the page's `baseline_state` is `none` or `young`; `render` takes an optional `baselineState`) / `derived: sum of linked templates` / `single template`.

- [ ] **Step 2: Wire Baseline**

Replace the `bl-key` label + select with `<div id="bl-picker"></div>` on its own row under the service / period bar. `blRenderControls` calls `picker.render({service: bl.service, key: bl.key, templates: bl.templates, docs, baselineState: bl.series?.baseline_state})`; load `/api/documents` alongside templates the way Review does. `onPick` sets `bl.key` and calls `blChangeRoute()`. `blKeyName` returns `keyLabel(...)`. Remove the now-dead select code.

- [ ] **Step 3: Wire Review**

Same with `<div id="rv-picker"></div>`; remove `rv-key`, `rv-tpl` and the `__tpl` branch. `onPick` sets `rv.key` and calls `rvChangeRoute()`. The key-name function returns `keyLabel(...)`.

- [ ] **Step 4: Styles and docs**

Move the mockup's `.kp-*` rules into `app.css` (tokens only, no raw colours). Update `docs/WEB_UI.md`: the Baseline and Review header bullets describe the picker (groups, combined series, view-only note, the level-of-a-template caveat from the spec); add `*warn`, `*info`, `verdict:<v>` to the `key` parameter of `/api/baseline/*` in the HTTP API table.

- [ ] **Step 5: Run the suite and check the JS parses**

Run: `.venv/bin/pytest -q && node --check logai2/web/static/js/keypicker.js logai2/web/static/js/views/baseline.js logai2/web/static/js/views/review.js`
Expected: all pass, no syntax error.

- [ ] **Step 6: Check in Chrome on the real database**

Run the web on the host (`LOGAI2_DATABASE_DSN=postgresql://logai:logai@localhost:5433/logai LOGAI2_WEB_PORT=5599 .venv/bin/python scripts/run_web.py`) and open `http://localhost:5599/`. Pass when:
1. Baseline: each group button shows its count; clicking WARN shows `All WARN lines combined` and the chart loads (rate, no band yet is fine).
2. Show popover: typing `USSD` under SAFE docs lists documents and templates; ↑/↓/Enter picks; Esc closes and focus returns to the button.
3. Picking a document and a template loads the chart for each; the URL hash carries the key.
4. Old routes `#review/recharge/doc:DOC-021/14`, `#baseline/recharge/recharge:T00141/7`, `#review/recharge/*error/14` open with the matching group pressed.
5. A template with level `WARNING` or lower-case `warn` is listed under WARN.
6. A group with count 0 is disabled (pick a service without SAFE documents, or confirm by reading the rendered `disabled` attribute).
7. Review: the same picker; excluding / keeping a period still works while `*warn` is shown. No console errors.

- [ ] **Step 7: Commit**

```bash
git add logai2/web/static/js/keypicker.js logai2/web/static/index.html logai2/web/static/js/views/baseline.js \
        logai2/web/static/js/views/review.js logai2/web/static/css/app.css docs/WEB_UI.md
git commit -m "Console: one grouped key picker on Baseline and Review (level and document verdict groups)"
```

---

## After the build

- Mark the spec `Status: built (merge <sha>)`; `agents.md` entry; `graphify update .` in the main checkout.
- Deploy (on the user's go): rebuild and restart `logai2-engine` and `logai2-web`; the linker is unchanged. `*warn` / `*info` start counting at the engine restart; their baseline is young after the first nightly commit.
