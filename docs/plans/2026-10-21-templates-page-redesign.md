# Templates Page Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Templates page a link-review tool: a To review | All templates mode, a 5-column table, and a decision-first drawer that walks to the next item.

**Architecture:** One small API addition in `GET /api/templates` (`link_source=review`, `review_total`); the rest is the existing vanilla-JS view (`views/templates.js`), its markup in `index.html` and styles in `app.css`. No new files, no schema change.

**Tech Stack:** Flask + psycopg 3 (API), vanilla JS / CSS with the tokens in `tokens.css` (UI), pytest against the test Postgres on 127.0.0.1:5433.

**Spec:** `docs/specs/2026-10-21-templates-page-redesign.md`

## Global Constraints

- No charts on this page; no bulk confirm; no new keyboard shortcuts; mode not persisted.
- Colors only through existing CSS tokens (`var(--red)` etc.), never raw hex; must read in light and dark theme.
- State and "above normal" always carry text, never color alone.
- Mutation routes, link semantics, Apply to similar, rate/band computation: unchanged.
- Tests: `.venv/bin/pytest -q` (needs `docker compose up -d postgres`). Stage by explicit path; no AI attribution in commits.
- After code changes run `graphify update .`.

## Review Focus

1. **No automatic links at all** (`review_total` 0): the page must open on All templates, and To review shows "Nothing to review" with a button to All templates — not an empty table. (Task 2 manual check.)
2. **Next item meets a stale row** (the linker or another operator decided it meanwhile): Confirm answers 409; the drawer must show the error in `#tpl-drawer-error` and stay usable, not close or throw. (Task 3 manual check.)
3. **Deciding the last rows of the last page** in To review: the list shrinks under the operator; `loadTemplates` must fall back to the new last page (existing guard) and the drawer must close after the page's last row. (Task 3 manual check.)
4. **Filters + count**: a service/level/search filter must change the To review count in both modes, and `link_source` must not. (Task 1 test `test_review_total_follows_filters`.)
5. **Unknown `link_source`** still answers 400 after `review` is added. (Task 1 test `test_review_filter_and_order`.)

---

### Task 1: API — `link_source=review` and `review_total`

**Files:**
- Modify: `logai2/web/app.py` (`templates()` route, ~lines 480–528)
- Test: `tests/test_web_alerts_templates.py`

**Interfaces:**
- Produces: `GET /api/templates?link_source=review` → items whose `link_source` ∈ `AUTOMATIC_SOURCES`, ordered ALERTING first, then level class ERROR > WARN > INFO, then `last_seen DESC, template_id`. Every response (any `link_source`) gains `"review_total": int`.

- [ ] **Step 1: Write the failing tests** (reuse `add_template`, `add_doc`, `add_state` in the file)

```python
def _review_fixture(conn):
    # (template, level, last_seen minute, link source or None)
    rows = [("pay:T00001", "INFO", 5, "corpus"), ("pay:T00002", "ERROR", 3, "llm"),
            ("pay:T00003", "WARN", 0, "inherited"), ("pay:T00004", "ERROR", 1, "draft"),
            ("pay:T00005", "ERROR", 9, "manual"), ("pay:T00006", "ERROR", 8, None),
            ("web:T00001", "ERROR", 7, "corpus")]
    add_doc(conn, "D1")
    for tid, level, m, source in rows:
        svc = tid.split(":")[0]
        add_template(conn, tid, level=level, service=svc, last_seen=f"2026-10-20 10:0{m}Z")
        if source:
            conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) "
                         "VALUES (%s, %s, 'D1', %s)", (svc, tid, source))
    add_state(conn, "pay:T00003", "ALERTING", 20)
    conn.commit()


def test_review_filter_and_order(client, conn):
    _review_fixture(conn)
    body = client.get("/api/templates?link_source=review&service=pay").get_json()
    assert [i["template_id"] for i in body["items"]] == ["pay:T00003", "pay:T00002", "pay:T00004", "pay:T00001"]
    assert body["total"] == 4
    assert client.get("/api/templates?link_source=bogus").status_code == 400


def test_review_total_follows_filters(client, conn):
    _review_fixture(conn)
    get = lambda q: client.get(f"/api/templates?{q}").get_json()["review_total"]
    assert get("") == 5
    assert get("service=pay") == 4
    assert get("service=pay&level=ERROR") == 2
    assert get("link_source=manual") == 5     # link_source does not narrow the count
    assert get("q=nothing-matches") == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_web_alerts_templates.py -k review`
Expected: FAIL (400 for `link_source=review`; `KeyError: 'review_total'`).

- [ ] **Step 3: Implement in `templates()`**

- Accept `review` in the `link_source` validation; the error message lists it.
- Build the filter list in two parts: the shared filters (service, level, `doc_id`, `q`) and the link filter (`none` → `l.template_id IS NULL`; `review` → `l.source = ANY(%s)` with `list(AUTOMATIC_SOURCES)`; else `l.source = %s`).
- `review_total` = count over the shared filters plus `l.source = ANY(AUTOMATIC_SOURCES)`, same `FROM templates t LEFT JOIN template_doc_links l` as `total`.
- Order for `review`: `(a.state = 'ALERTING') IS TRUE DESC, CASE {class_sql('t.level')} WHEN 'ERROR' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, t.last_seen DESC, t.template_id` (`class_sql` from `logai2.links`, as `resolver.py` does). Other modes keep `t.last_seen DESC, t.template_id`.

- [ ] **Step 4: Run the file, then the suite**

Run: `.venv/bin/pytest -q tests/test_web_alerts_templates.py` then `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add logai2/web/app.py tests/test_web_alerts_templates.py
git commit -m "Templates API: link_source=review (alerting, level, recency) and review_total"
```

### Task 2: Toolbar mode and the 5-column table

**Files:**
- Modify: `logai2/web/static/index.html` (`#view-templates`, lines ~87–123)
- Modify: `logai2/web/static/js/views/templates.js`
- Modify: `logai2/web/static/css/app.css` (near `.rate-high` / `.link-cell`, ~lines 891–951)

**Interfaces:**
- Consumes: Task 1's `link_source=review` and `review_total`.
- Produces (used by Task 3): `tpl.mode` (`'review' | 'all'`), `tpl.order` (array of `tplKey`s of the current page in table order), `tplRateHtml(t)` (rate + band, two lines, used by the table and the drawer header).

- [ ] **Step 1: Markup.** Before the search box, add a tablist copying `#doc-mode-toggle`: `<div class="toggle-group" id="tpl-mode-toggle" role="tablist" aria-label="Templates to review or all">` with buttons `data-tpl-mode="review"` ("To review" + `<span class="count-pill" id="tpl-review-count">`) and `data-tpl-mode="all"` ("All templates"). Header row becomes: `State · Template · Rate · Document · Last seen · <th><span class="visually-hidden">Action</span></th>` (use the console's existing visually-hidden class if one exists, else add it); empty/loading `colspan="6"`.

- [ ] **Step 2: Mode state.** `tpl.mode` starts `null` and fetches as `'all'`; when that first response arrives with `mode === null`, set `tpl.mode = review_total > 0 ? 'review' : 'all'` and, if it became `'review'`, call `loadTemplates()` again before rendering. In review mode `fetchTemplates` sends `link_source=review` and ignores `tpl.link`; `#filter-link` is `hidden` in review mode, and switching to review clears `tpl.link` and the select. Clicking a tab sets `tpl.mode`, `tpl.page = 1`, updates `aria-selected`/`.active` like `documentation.js:36`, and calls `loadData()`. `#tpl-review-count` shows `review_total` (hidden when 0); the tab's `aria-label` reads "To review, N templates".

- [ ] **Step 3: Row rendering** in `renderTemplates`, per the spec's column table:
  - State: badge for non-NORMAL; `<span class="link-meta">normal</span>` for NORMAL.
  - Template: `.tmpl-id` + level badge on line 1, wrapped `.tmpl-text` with `highlightWildcards` on line 2.
  - Rate: `tplRateHtml(t)` → line 1 `14/min`, plus when above band `class="rate-high"` and text `▲ above normal`; line 2 muted `normal ≤ X/min` when `band_low ≤ 0`, else `normal A–B/min`; `—` with the existing tooltips when `rate`/`band_high` is null.
  - Document: line 1 `DOC-007 · title` / `Not linked` (+ `· not embedded yet`) / `No document (set by hand)`; line 2 `linkSourceBadge(t)` + SAFE badge. No button in this cell.
  - Last seen: `formatTs`, title `formatFullTs`.
  - Action cell: the existing `.tpl-review` button (Review / Link… / Change), right-aligned.
  - Row click (outside a button or link) opens the drawer for that row: put `data-key` on the `<tr>` and extend the tbody click handler.
  - Fill `tpl.order` with the page's keys in render order.
  - Empty state: in review mode "Nothing to review" + a button that switches to All templates; in all mode the existing message.

- [ ] **Step 4: CSS.** Reuse `.link-meta`, `.rate-high`, `.tpl-num`; add only what the new cells need (two-line cell stack, `tr[data-key]{cursor:pointer}` with a token-based hover background, right-aligned action cell). Remove `.link-cell` rules if nothing uses them any more.

- [ ] **Step 5: Check in the browser.** Run the web (`README.md`, `scripts/run_web.py`) against the local compose data: both modes, count follows service/level/search, link filter hidden in To review, Review Focus 1 (temporarily filter to a service with no automatic links), light + dark theme, 390 px width (table scrolls horizontally inside `.table-wrap`, page does not). Run `.venv/bin/pytest -q` (unchanged: all pass).

- [ ] **Step 6: Commit**

```bash
git add logai2/web/static/index.html logai2/web/static/js/views/templates.js logai2/web/static/css/app.css
git commit -m "Templates page: To review | All templates mode, five-column table"
```

### Task 3: Decision-first drawer and Next item

**Files:**
- Modify: `logai2/web/static/js/views/templates.js` (`openTemplateDrawer`, drawer click handler)
- Modify: `logai2/web/static/css/app.css` (`.drawer-actions`, ~line 953)

**Interfaces:**
- Consumes: `tpl.mode`, `tpl.order`, `tplRateHtml(t)` from Task 2; `linkedDocument(t)` (existing).

- [ ] **Step 1: Reorder `openTemplateDrawer`** into the spec's sections: header (service, level badge, state badge, `tplRateHtml(t)`, first and last seen with `formatTs`/`formatFullTs`); "Proposed document" (automatic source) or "Document" (manual) with the template text in a `.code-block`, then the document id + title and its ERROR line `.code-block` directly under it, then the `how` sentence and the LLM note, then Handling inside `<details><summary>Handling</summary>…</details>` (closed); not linked → one line "Not linked to a document."; actions; Link by hand unchanged; `#tpl-drawer-error`. The document part still loads asynchronously into `#tpl-drawer-doc` with the existing stale-key guard.

- [ ] **Step 2: Sticky actions.** `.drawer-actions` becomes `position: sticky; bottom: 0` with `background: var(--surface)` (or the drawer's existing background token) so Confirm/Reject stay visible while the drawer scrolls. Focus: after the drawer opens, focus `#tpl-drawer-title` (give it `tabindex="-1"` in `index.html` if it has none).

- [ ] **Step 3: Next item.** In the confirm/reject branch: after the mutation succeeds and the toast shows, if `tpl.mode === 'review'`, take the key after the decided one in `tpl.order` (captured before the reload); `await loadData()`; if that key is still in `tpl.items`, `openTemplateDrawer` on it, else close the drawer. All mode and Link by hand keep today's close-then-reload behavior. On error (e.g. 409 from a row decided elsewhere) keep today's path: message in `#tpl-drawer-error`, button re-enabled, drawer stays open (Review Focus 2).

- [ ] **Step 4: Check in the browser.** In To review: confirm one → next opens; reject (dialog) → next opens; decide the last row of a page → drawer closes; decide everything on the last page → the list falls back to the previous page (Review Focus 3); force a 409 by confirming a row then re-opening its stale drawer from an old tab (Review Focus 2). A draft shows "Approve suggestion". Light, dark, 390 px. `.venv/bin/pytest -q` all pass.

- [ ] **Step 5: Commit**

```bash
git add logai2/web/static/js/views/templates.js logai2/web/static/css/app.css logai2/web/static/index.html
git commit -m "Templates drawer: decision first, sticky actions, next item in To review"
```

### Task 4: Docs and wrap-up

**Files:**
- Modify: `docs/WEB_UI.md` (Templates section ~line 95, and its HTTP API table)
- Modify: `agents.md` (local, git-ignored)

- [ ] **Step 1:** Rewrite the Templates section: the two modes and when the page opens on each, the five columns, the drawer order, Next item. Add `link_source=review` (ordering) and `review_total` (filters it follows, ignores `link_source`) to the `GET /api/templates` row of the API table.
- [ ] **Step 2:** `graphify update .`; `.venv/bin/pytest -q` all pass.
- [ ] **Step 3:** Commit `git add docs/WEB_UI.md && git commit -m "WEB_UI: Templates review mode, columns, drawer"`; add the task's entry to `agents.md`.
