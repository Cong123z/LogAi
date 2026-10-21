# Web UI and HTTP API

This document describes the web console **as implemented** in
`logai2/web/app.py` and `logai2/web/static/`. When it and the code disagree,
the code (and `tests/test_web_*.py`) is the source of truth.

Related docs: [`README.md`](../README.md) (run), [`ARCHITECTURE.md`](../ARCHITECTURE.md)
(engine, schema, the web ↔ engine contract in §12).

The web is a Flask app over Postgres only. It never connects to
Elasticsearch or to the engine and never runs migrations (the engine does).
It reads what the engine wrote and writes settings, documents, links, review
decisions and **commands** that the engine picks up on its next tick.

## Run the UI

```bash
docker compose up -d logai2-web          # http://localhost:5565
# or, on the host:
LOGAI2_DATABASE_DSN=postgresql://logai:logai@localhost:5433/logai \
  LOGAI2_WEB_PORT=5555 .venv/bin/python scripts/run_web.py
```

Only the engine runs `migrate()`. On a fresh database, start `logai2-engine`
(or `docker compose up -d`) once before the web's API works; until then the
tables do not exist.

`run_web.py` starts Flask's threaded development server on `web.host:web.port`
(default `0.0.0.0:5555`; compose publishes 5565). The look, stack and
sidebar are copied from logai-engine (vanilla JS, hash routes, light/dark
theme).

| View | URL | Polls | Data |
|---|---|---|---|
| Alerting | `/#alerting` | every 5 s | `GET /api/alerts` |
| Pipeline | `/#pipeline` (also the fallback for unknown hashes) | every 2 s | `GET /api/pipeline` |
| Templates | `/#templates` | only with Auto-refresh (10 s) | `GET /api/templates`, `/api/services`, `/api/documents` |
| Documentation | `/#documentation`, `/#documentation/suggestions` | on entry and after each change | `GET /api/documents`, `/api/services`, `/api/templates?link_source=draft` |
| Data sources | `/#sources` | every 2 s | `GET /api/sources` |
| LLM profiles | `/#llm` | every 5 s | `GET /api/llm/profiles`, `/api/llm/usage` |
| Baseline | `/#baseline` | on entry, after each change, and every 1 s while a recompute runs | `GET /api/services`, `/api/baseline` |

The sidebar groups them into Monitor (Alerting, Pipeline), Catalog
(Templates, Documentation) and Settings (Data sources, LLM profiles, Baseline). The top
bar has an **engine health pill**, refreshed every 5 s from `/api/pipeline`:
red (unavailable) when the heartbeat is stale (older than 30 s), yellow
(degraded) when the engine reports warnings, green otherwise.

## Alerting

One card per **alert row** (ARCHITECTURE §5): a document, an undocumented
template, a **cluster** of similar undocumented templates of one service
(embedding similarity ≥ `documentation.cluster_similarity`), or the whole
service. KPI cards count open rows and rows per state
(ALERTING, WARMING, COOLING); they and the state toggle filter the list.
Rows keep the server's order (ALERTING first, then oldest).

Each card shows the state badge, `service · title` (document title, or
"Undocumented", "Undocumented · N similar templates", or "Whole service")
and `since`. Its lines are:

- `totals` first: "All events" (`*total`), "Errors" (`*error`) or "Linked
  templates combined" (the `doc:` key);
- then each alerting template's text.

Each line shows the last closed minute's count per minute and "(normal mean
± σ)" of the **current local hour** from the committed baseline, with the
alert band (`mean ± kσ`) on hover; "(no baseline yet)" means floor-only.
A template line also shows how it got its document when that was not by
hand: `inherited`, `auto 0.87` (matched on the document's ERROR lines),
`AI-confirmed`, or `AI suggestion: <title>` (a draft: shown, drives
nothing), plus `SAFE` for a normal-operation document.
A "Live · updated Ns ago" label turns into a stale warning after 15 s
without a successful poll.

## Pipeline

Live engine status from `worker_status`, written at every flush:

- status line (running / not running with the last heartbeat / never
  reported) and the engine's warnings (ES unreachable, high miss ratio);
- **Lag** (amber above 5 min), **Events/s** since the last flush, **Cache
  hit ratio**, and a sparkline of events/s over the last 60 heartbeats (kept
  only in the browser tab);
- **Detection** On/Off ("catching up; counts only" until the first
  catch-up commit), **Bootstrap** (`since`, `until`), **Last commit**
  (time and baseline day);
- **Linker**: Running / Stale (no report for 90 s) / Not running, with its
  pending counts (templates and documents waiting for a vector) or its last
  error;
- a table of indices: position, floor, and `failing` / `caught up` /
  `catching up` / `not reading` (an `es_cursors` row the engine no longer
  reports).

## Templates

Two modes, a toggle at the left of the filter bar:

- **To review (N)**: templates whose link is automatic (`inherited`,
  `corpus`, `llm`, `draft`) and waits for a person: ALERTING first, then
  ERROR > WARN > INFO, then newest `last_seen`. N follows the service,
  level and search filters. The page opens here when N > 0, else on All
  templates (the choice is not kept across reloads).
- **All templates**: every template, newest `last_seen` first, with a
  **link** filter (not linked, or one link source); hidden and cleared in
  To review.

Both modes: service, level and a text search (case-insensitive substring,
debounced 300 ms); 100 per page by default (50, 100 or 200; server cap
500). To review with nothing left says so and offers All templates.

Columns: **state** (WARMING / ALERTING / COOLING badge; NORMAL as muted
text), **template** (id and level, then the full text, `<*>`
highlighted), **rate** (average per minute this local hour; above the band
in red with "▲ above normal"; under it the band as `normal ≤ X/min` or
`normal A–B/min`, judged exactly as detection does; "—" without data),
**document** (`DOC-007 · title`, or "Not linked" / "No document (set by
hand)"; under it how it was linked: `manual`, `inherited 0.91`,
`auto-matched 0.87`, `AI-confirmed`, `AI suggestion · unverified`, `AI
result rejected`, and SAFE), **last seen** (relative, full time on
hover), and a button: **Review** for an automatic link, else **Link…** /
**Change**. A click anywhere on the row opens the same drawer.

The drawer puts the decision first (ARCHITECTURE §5.1 for the layers):

- service, level, state, rate against normal, first and last seen;
- **Proposed document** (automatic link) or **Document**: the template
  text with the document's ERROR line directly under it, Handling
  collapsed under them, then how it was linked in words and the LLM's
  reason in full;
- **Confirm link** (→ manual) / **Approve suggestion** (draft → active)
  and **Reject**, pinned to the drawer's bottom edge while it scrolls and
  disabled while one is running; they act on the template shown. In
  To review, a successful decision opens the **next row** of the page
  (the drawer closes after the page's last row); in All templates it
  closes. A link decided elsewhere meanwhile shows the API's 409 message
  in the drawer;
- **Link by hand**: a document (a manual link, then the **Apply to
  similar** dialog when similar templates of the same service and level
  exist, pre-ticked at the inherit threshold), **No document** (a sticky
  manual "no document"), or **Automatic** (removes the row; the linker may
  link it again). Always closes the drawer.

Each change enqueues `reload_links`, so the engine's doc keys and alert
rows follow within a tick. A short confirmation appears bottom right (a
polite live region).

## Documentation

A list on the left and the selected item in full on the right, with its
actions pinned under the text; ↑/↓ move through the list, a search box
filters it (title, error code, ERROR, Handling, template). Below 900 px the
two stack. Two tabs (deep links `#documentation` and
`#documentation/suggestions`):

Every document belongs to a **service**, or is **shared** (applies to
every service). The service filter shows **All services** or one
service's documents only (shared ones appear under All services). With
**All services**, documents whose ERROR
lines are the same (after normalizing) form one row with a chip per
service ("same ERROR in 3: recharge · topup · payment"), so one error is
never listed three times; the pane has a switch between the services'
versions. Automatic matching only uses a template's own service's and the
shared documents. An AI suggestion belongs to its template's service and
says which other services' documents it was adapted from.

Every document is of one **kind**: **ERROR** (`verdict` `action`: an
error, something to fix) or **SAFE** (`verdict` `safe`: normal operation,
no action needed). The **All / ERROR / SAFE** buttons, with counts, filter
the list in both tabs.

- **Documents** (active): each item shows id, kind, title, error code,
  linked template count and "no log line" when it has no ERROR line. The pane shows the title,
  the ERROR block with the lines as matched ("embedding…" until the linker
  has embedded this revision; no ERROR line = never matched automatically),
  the Handling text (numbered steps written on one line are shown one per
  line), and the linked templates in full with their link source.
  **Edit** opens the form in the pane (title, error code, service, log level of
  the ERROR line, kind, ERROR, Handling;
  a 409 `stale` says it changed in another window), **Delete** asks first
  and is refused while templates are linked. **Mark as SAFE / Mark as
  ERROR** changes only the kind, in one click. **Add document** opens an
  empty form; the server assigns the next `DOC-NNN` id.
- **AI suggestions** (count in the tab): the LLM's drafts. Each item shows
  ACTION or SAFE, the confidence and the template it was written for. The
  pane warns that it is unverified, then shows that template in full
  (level, state, rate), the ERROR and the Handling. **Approve** (the
  document becomes active and the link manual), **Edit** (saving
  approves), **Reject** (asks first; the template is never sent to the LLM
  again); the next suggestion is selected after each decision. **Approve
  all SAFE** approves every normal-operation suggestion after listing them.

On the first engine start, an empty table is seeded from
`documentation.seed_path` (ARCHITECTURE §4.4).

## Data sources

**Indices.** The selected patterns, with what each currently resolves to
(per index: position, caught up, failing), and when it was added ("engine
start" for an entry read since bootstrap). Below, the available indices
listed by the engine (doc count, health), filterable; ticking an index adds
it, an index already covered by a selected pattern is shown as "via
<pattern>". Free-text patterns (`*` wildcard) can be added. Edits are a
local draft until **Save**; **Discard** drops it. A 409 keeps the draft and
reloads the saved selection.

Before any save the engine reads `elasticsearch.index` from its
configuration; saving an empty list falls back to it too. An index added
later is read from the moment it was added, not backfilled
(ARCHITECTURE §4.3).

**Connection.** Endpoints (comma-separated), username, password, verify
certificates. The password is never shown; leave it empty to keep the
stored one. Saving endpoints that point at another cluster asks for
confirmation ("earlier logs are not backfilled") and the server requires
the password again. The engine reconnects on the `reload_sources` command.

## LLM profiles

A page of its own (Settings → LLM profiles, polled every 5 s), as in
logai-engine. A banner says when nothing is sent to an LLM (no active
profile, linker not running) or shows the last LLM error or the daily
limit. Cards: the active profile, results in the last hour and 24 h
(confirmed / suggested / rejected, with the limits), suggestions waiting
for review, and the linker's last cycle. Then the profiles table with Add,
Edit, Activate / Deactivate and Delete. Every active profile is used,
lowest **priority** first: when one fails (network, HTTP error, timeout)
the next one answers and the failing one is skipped for 5 minutes; its
row shows "failing" with the error, and the banner says whether another
profile is answering. **Answers** counts each active profile's answers
since the linker started.

Each profile, as in logai-engine: name, OpenAI-compatible base URL
(`…/v1`, or a bare host which gets `/v1`; the linker posts to
`…/chat/completions`), model, API key, timeout (5–600 s, default 120) and
priority (0–1000, default 100). With no active profile, templates are not
sent to an LLM. The API key is
write-only: the list shows only whether one is saved, and a save with an
empty key keeps the saved one.

## Baseline

Per service (a select lists every service the engine has seen):

- cards: committed **weekday** and **weekend** days with the profile in use
  ("No days yet: floor-only alerts" / "weekday profile, upper alerts only",
  "Young: mean ± 4σ until N days", "Full: mean ± 3σ"; N is 5 for weekdays
  and 4 for weekends), open suggestions, last commit (time and owner day);
- **Suggestions**: periods in which any key of the service was not NORMAL,
  as whole local hours per day, with the keys involved, and two buttons:
  **Exclude: real incident** and **Keep: normal**. Unreviewed hours are
  kept in the baseline;
- **Add a range**: day, from, to, decision, for hours the engine did not
  suggest;
- **Decisions** with **Undo** (the hours are suggested again);
- **Days**: every day in `day_status`, weekday/weekend, committed;
- **Recompute baseline now**: queues `recompute_baseline` and polls the
  command every second until it reports `days_committed` and
  `baseline_rows` (or an error).

An exclusion that leaves an hour with fewer than 3 of the days the rebuild
uses (last 5 weekdays / 4 weekend days of that day type) shows a "Few
baseline days left" warning. Decisions take effect at the next commit or
recompute.

## HTTP API

All routes are JSON except `/` and `/static/…`. Times are epoch seconds
unless noted; days are ISO dates (`YYYY-MM-DD`) in local time.

| Method and path | Input | Output | Errors | Command |
|---|---|---|---|---|
| `GET /` | — | `index.html` | — | — |
| `GET /static/<path>` | — | static file | 404 | — |
| `GET /api/pipeline` | — | `{status, cursors, stale, linker, linker_stale}` | — | — |
| `GET /api/sources` | — | `{selection, connection, available_indices, resolution, indices}` | — | — |
| `PUT /api/sources/selection` | `{patterns: [str], revision}` | `{revision}` | 400, 409 | `reload_sources` |
| `PUT /api/sources/connection` | `{hosts: [url], username, password, verify_certs, revision}` | `{revision}` | 400, 409 | `reload_sources` |
| `GET /api/alerts` | — | `[alert row]` | — | — |
| `GET /api/services` | — | `[service]` sorted | — | — |
| `GET /api/templates` | query `page`, `size`, `service`, `level`, `doc_id`, `link_source`, `q` | `{total, review_total, items}` | 400 | — |
| `PUT /api/templates/<service>/<template_id>/link` | `{doc_id: str \| null, also?: [template_id]}` | `{doc_id, linked}` | 400, 404 | `reload_links` |
| `DELETE /api/templates/<service>/<template_id>/link` | — | `{doc_id: null}` | 404 | `reload_links` |
| `POST /api/templates/<service>/<template_id>/link/confirm` | — | `{doc_id, source}` | 404, 409 | `reload_links` |
| `POST /api/templates/<service>/<template_id>/link/reject` | — | `{source}` | 404, 409 | `reload_links` |
| `GET /api/templates/<service>/<template_id>/similar` | — | `{embedded, items}` | 404 | — |
| `GET /api/documents` | query `status` (`active` default, `draft`, `rejected`) | `[document]` | 400 | — |
| `POST /api/documents` | `{title, error?, handling?, error_code?, verdict?, service?, level?}` | `201 {doc_id, revision}` | 400 | — |
| `PUT /api/documents/<doc_id>` | `{title, error?, handling?, error_code?, verdict?, service?, level?, revision}` | `{doc_id, revision}` | 400, 404, 409 | — |
| `DELETE /api/documents/<doc_id>` | — | `{doc_id}` | 404, 409 | — |
| `GET /api/llm/profiles` | — | `[profile]` (no key) | — | — |
| `GET /api/llm/usage` | — | `{hour, day, drafts_waiting, limits, linker_running, last_cycle, last_error, profiles, warnings}` | — | — |
| `PUT /api/llm/profiles/<name>` | `{base_url, model, api_key?, timeout_seconds?, priority?, revision}` | `{name, revision}` | 400, 409 | — |
| `POST /api/llm/profiles/<name>/activate` | `{active: bool}` | `{name, active}` | 400, 404 | — |
| `DELETE /api/llm/profiles/<name>` | — | `{name}` | 404 | — |
| `GET /api/baseline` | query `service` | `{days, suggestions, decisions, last_commit, full_after_days}` | 404 | — |
| `POST /api/baseline/decisions` | `{service, day, hour_from, hour_to, decision}` | `201 {id, warning?}` | 400, 404 | — |
| `DELETE /api/baseline/decisions/<id>` | — | `{id}` | 404 | — |
| `POST /api/baseline/recompute` | — | `202 {id}` | — | `recompute_baseline` |
| `GET /api/commands/<id>` | — | `{id, kind, done, result}` | 404 | — |

### Error bodies

| Status | Body | When |
|---|---|---|
| 400 | `{"error": "<message>"}` | invalid input (messages below), or a body that is not a JSON object (`"JSON object body required"`) |
| 404 | `{"error": "Unknown service" \| "Unknown template" \| "Unknown document" \| "Unknown decision" \| "Unknown command"}` | unknown id; any other unknown path gets Werkzeug's description |
| 405 | `{"error": "..."}` with the `Allow` header kept | wrong method |
| 409 | `{"error": "stale", "revision": "<current or null>"}` | the revision sent is not the current one (settings, documents) |
| 409 | `{"error": "linked", "templates": <n>}` | `DELETE /api/documents/<id>` while templates link to it |
| 500 | `{"error": "Internal server error"}` | anything else (logged with the traceback) |

### `GET /api/pipeline`

```json
{"status": {...worker_status of the engine, or null...},
 "cursors": [{"index": "recharge-logs", "since_ms": 1792381140000, "floor_ms": 1792207554000}],
 "stale": false}
```

`status` fields are listed in ARCHITECTURE §10.5. `stale` is true when the
heartbeat is missing or older than 30 s. `linker` is
`worker_status['linker']` (`heartbeat`, `embedded_templates`,
`embedded_documents`, `links_changed`, `pending {templates, documents}`,
`warnings`, `last_error`) or null; `linker_stale` is true when it is missing
or older than 90 s.

### `GET /api/sources`

```json
{"selection": {"entries": [{"pattern": "recharge-logs", "added_at": 0}], "revision": null},
 "connection": {"hosts": ["http://elasticsearch:9200"], "username": "", "verify_certs": true,
                "has_password": false, "revision": null},
 "available_indices": [{"index": "recharge-logs", "docs_count": 2825214, "health": "yellow", "status": "open"}],
 "resolution": {"recharge-logs": ["recharge-logs"]},
 "indices": {"recharge-logs": {"pos_ms": 1792381154797, "caught_up": true, "failing": false, "floor": 1792207554000}}}
```

With nothing saved, `selection.entries` is the configured index with
`added_at` 0 and `connection` shows the configuration (env). The last three
fields come from the engine's status and are null until it reports.

### `PUT /api/sources/selection`

- `patterns`: a list; each entry lowercase letters, digits, `.`, `_`, `-`,
  `#`, `*`, starting with a letter, digit or `*`, at most 255 characters;
  duplicates dropped; at most 50.
- `revision`: the selection revision read (`null` before the first save).
- A kept pattern keeps its `added_at`; a new one gets now.
- 400 `"patterns must be a list"`, `"Invalid index or pattern …"`,
  `"At most 50 indices or patterns"`; 409 stale.

### `PUT /api/sources/connection`

- `hosts`: 1–10 `http(s)` URLs without credentials in the URL (trailing `/`
  dropped, duplicates dropped, at most 2000 characters each).
- `username`, `password`: at most 500 characters, no control characters.
- `verify_certs`: boolean.
- An empty `password` keeps the stored one (saved, else the env one) when
  the hosts point at the same cluster (same host:port set); otherwise, if a
  password is stored, 400 `"password required"`.
- 400 also for `"hosts must be a list of URLs"`, `"Invalid endpoint …"`,
  `"Endpoint … has an invalid port"`, `"Endpoint must not contain a username
  or password"`, `"Give 1 to 10 endpoints"`, `"verify_certs must be true or
  false"`; 409 stale.

### `GET /api/alerts`

```json
[{"service": "recharge", "kind": "document", "doc_id": "DOC-001", "title": "...",
  "state": "ALERTING", "since": 1792390000.0,
  "keys": ["recharge:T00032", "doc:DOC-001"],
  "templates": [{"template_id": "recharge:T00032", "text": "...", "state": "ALERTING",
                 "count": 150, "band_low": -3.1, "band_high": 9.4, "mean": 2.0, "std": 1.8}],
  "totals": [{"key": "doc:DOC-001", "state": "ALERTING", "count": 150,
              "band_low": -3.1, "band_high": 9.4, "mean": 2.0, "std": 1.8}]}]
```

`kind` is `document`, `template`, `cluster` or `service`; `doc_id` and
`title` are null except for documents. Each template line has `link`:
null, or `{doc_id, source, similarity, note, title, verdict}` of its
`template_doc_links` row, drafts and rejected ones included (badges only:
grouping uses only the links that drive alerts). `templates` holds the template members, `totals` the
`*total`, `*error` and `doc:` members. `count` and the bands are from the
last judged minute; `mean` / `std` are this local hour's baseline (null
without one; for a doc key, the sums over its linked templates).

### `GET /api/templates`

- `page` (default 1) and `size` (default 100) are clamped to 1..10⁶ and
  1..500; a non-integer is 400 `"<name> must be an integer"`.
- `service`, `level`, `doc_id`: exact match; `q`: case-insensitive
  substring of the text; `link_source`: `none` (no row), `review` (an
  automatic link: `inherited`, `corpus`, `llm`, `draft`) or one of
  `manual`, `inherited`, `corpus`, `llm`, `draft`, `rejected` (else 400).
- Order: newest `last_seen` first, then `template_id`; with
  `link_source=review`, ALERTING first, then level class ERROR > WARN >
  INFO, then the same.
- `review_total`: templates with an automatic link that match `service`,
  `level`, `doc_id` and `q` (never narrowed by `link_source`).

```json
{"total": 308, "review_total": 7,
 "items": [{"service": "recharge", "template_id": "recharge:T00032",
            "text": "LixiHandler: <*> enterd Receiving subscriber incorrect: <*>",
            "level": "ERROR", "first_seen": 1792207560.0, "last_seen": 1792381140.0,
            "doc_id": "DOC-001", "doc_title": "...", "doc_verdict": "action",
            "link_source": "inherited", "similarity": 0.91, "inherited_from": "recharge:T00031",
            "note": null, "embedded": true, "state": "NORMAL",
            "rate": 2.4, "band_low": 0.0, "band_high": 9.4}]}
```

`rate` is null when the service has no counted minute in the current hour;
`band_low` / `band_high` are null without a baseline.

### `PUT /api/templates/<service>/<template_id>/link`

`{"doc_id": "DOC-003"}` links (replacing any link, `source` `manual`);
`{"doc_id": null}` stores a manual "no document", which nothing automatic
overwrites. `also` (≤ 500 template ids of the same service) links those
too ("apply to similar"). 400 `"doc_id must be a string or null"`, `"also
must be a list of at most 500 template ids"`; 404 `"Unknown template"`
(also for an `also` id) or `"Unknown document"`. The links and their
`reload_links` command commit together. The template id may contain `/`;
the service may not.

`DELETE …/link` removes the row whatever its source (back to automatic; a
draft's document becomes `rejected`). `POST …/link/confirm` turns an
`inherited`, `corpus`, `llm` or `draft` link into `manual` (a draft
document becomes `active`); `POST …/link/reject` turns `inherited` /
`corpus` into a manual "no document" and `llm` / `draft` into `rejected`
(never sent to the LLM again). Both: 409 when the template has no automatic
link.

`GET …/similar`: templates of the same service and level class with
similarity ≥ `documentation.suggest_similarity`, most similar first (≤
200): `{template_id, text, level, similarity, current_doc_id,
current_source, pre_ticked}`; `pre_ticked` = similarity ≥ the class's
inherit threshold and not already manual. `embedded` is false while the
template has no vector (then the list is empty).

### Documents

- `GET /api/documents?status=active`: `[{doc_id, service, level, based_on, title, error_code, error,
  handling, status, source, verdict, confidence, revision, embedded,
  error_lines, linked_templates}]` sorted by `doc_id`. `error_lines` = the
  ERROR field normalized as the linker embeds it; `embedded` = the linker
  has embedded this revision; `linked_templates` counts links that drive
  alerts.
- `POST`: `title` (≤ 200) required, and `error` (≤ 5000, ≤ 50 lines) or
  `handling` (≤ 20000) non-empty after trimming; `error_code` optional (≤
  100); `verdict` `action` (ERROR, the default) or `safe` (SAFE), else 400
  `"verdict must be action or safe"`; `service` a known service, or `""` /
  null for shared (the default), else 400 `"service must be a known
  service, or empty for shared"`; `level` `ERROR`, `WARN`, `INFO`, or `""` /
  null for any level (the default), else 400 `"level must be ERROR, WARN,
  INFO or empty for any"`. The id is `DOC-` + the next number
  after the highest `DOC-<digits>` id, zero-padded to 3 (`DOC-013`); the
  table is locked during the insert so two creates never collide. 400
  `"<field> must be text of at most N characters"`, `"title and an ERROR or
  Handling text are required"` or `"error must have at most 50 lines"`.
- `PUT`: same fields plus `revision`; a missing `verdict`, `service` or
  `level` keeps the current one; 404 `"Unknown document"`; 409 stale.
- `DELETE`: 409 `linked` with the number of linked templates (the foreign
  key decides, so a link created concurrently is caught); 404.

### LLM profiles

- `GET /api/llm/profiles`: `[{name, base_url, model, timeout_seconds,
  priority, active, has_api_key, revision}]`, active first in the order
  they are tried; never the key.
- `PUT /api/llm/profiles/<name>`: creates (`revision` null) or updates
  (the revision read; else 409 stale). `name` 1–64 of letters, digits, `.`,
  `_`, `-`; `base_url` http(s) without credentials (trailing `/` dropped);
  `model` required; `timeout_seconds` integer 5–600; `priority` integer
  0–1000; `api_key` without
  control characters (it goes into a header), empty keeps the saved one.
- `GET /api/llm/usage`: `hour` / `day` = `{source: count}` of `llm`,
  `draft` and `rejected` links made in the last hour / 24 h;
  `drafts_waiting` = draft documents; `limits` = `llm.per_service_hour`,
  `llm.daily_max`; `linker_running` (heartbeat within 90 s), `last_cycle`
  (`{confirmed, suggested, none, failed}`), `last_error` (the last LLM
  error, kept across cycles), `profiles` (`{name: {down_until, error, answers}}` of
  the active profiles), `warnings` from the linker's status.
- `POST …/activate {"active": true|false}` turns one profile on or off;
  any number can be active. `DELETE` removes it. Unknown name: 404
  `"Unknown profile"`.

### `GET /api/baseline?service=<service>`

```json
{"days": [{"day": "2026-10-19", "committed": true, "daytype": "weekday"}],
 "suggestions": [{"service": "recharge", "day": "2026-10-20", "hour_from": 9, "hour_to": 11,
                  "keys": ["*error", "recharge:T00032"]}],
 "decisions": [{"id": 1, "day": "2026-10-20", "hour_from": 9, "hour_to": 11,
                "decision": "exclude", "created_at": 1792400000.0}],
 "last_commit": {"at": "2026-10-20T16:28:13+00:00", "today": "2026-10-20"},
 "full_after_days": {"weekday": 5, "weekend": 4}}
```

404 `"Unknown service"` for a missing or unknown `service`. Days and
decisions are newest first. Not paginated.

### `POST /api/baseline/decisions`

`{"service", "day": "YYYY-MM-DD", "hour_from": int, "hour_to": int,
"decision": "exclude" | "keep"}` with `0 <= hour_from < hour_to <= 24`
(`hour_to` exclusive). Validation order: day (400 `"day must be an ISO date
(YYYY-MM-DD)"`), hours (400 `"hours must be whole numbers with 0 <=
hour_from < hour_to <= 24"`), decision (400 `"decision must be 'exclude' or
'keep'"`), service (404). Overlapping rows of the opposite decision on that
day are deleted first. For an `exclude`, `warning` lists the hours left with
fewer than 3 usable days.

### Commands

`POST /api/baseline/recompute` returns `202 {"id"}`; poll
`GET /api/commands/<id>` until `done` is true. `result` is
`{"days_committed", "baseline_rows"}` or `{"error": "..."}`. Every write that
enqueues a command is listed in the table above; the command kinds are in
ARCHITECTURE §12.2.

## Security

The API has no authentication or authorization, and the connection endpoint
lets a caller point the engine at any http(s) host. Expose the web only on a
trusted network. The ES password is stored in Postgres in clear and never
returned.

## Implementation map

| Area | Files |
|---|---|
| Routes, validation | `logai2/web/app.py` |
| Shell, routing, polling, health pill | `static/index.html`, `static/js/main.js`, `core.js`, `ui.js` |
| Views | `static/js/views/{alerting,pipeline,templates,documentation,sources,baseline}.js` |
| Styles | `static/css/tokens.css`, `app.css` (copied from logai-engine) |
| Shared with the engine | `logai2/alerts.py` (alert rows), `logai2/owner/baseline.py` (bands), `logai2/review.py` (suggestions), `logai2/commands.py` |
| Tests | `tests/test_web_sources.py`, `test_web_alerts_templates.py`, `test_web_docs_baseline.py` |
