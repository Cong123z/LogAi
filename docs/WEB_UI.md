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

`run_web.py` starts Flask's threaded development server on `web.host:web.port`
(default `0.0.0.0:5555`; compose publishes 5565). The look, stack and
sidebar are copied from logai-engine (vanilla JS, hash routes, light/dark
theme).

| View | URL | Polls | Data |
|---|---|---|---|
| Alerting | `/#alerting` | every 5 s | `GET /api/alerts` |
| Pipeline | `/#pipeline` (also the fallback for unknown hashes) | every 2 s | `GET /api/pipeline` |
| Templates | `/#templates` | only with Auto-refresh (10 s) | `GET /api/templates`, `/api/services`, `/api/documents` |
| Documentation | `/#documentation` | on entry and after each change | `GET /api/documents` |
| Data sources | `/#sources` | every 2 s | `GET /api/sources` |
| Baseline | `/#baseline` | on entry, after each change, and every 1 s while a recompute runs | `GET /api/services`, `/api/baseline` |

The sidebar groups them into Monitor (Alerting, Pipeline), Catalog
(Templates, Documentation) and Settings (Data sources, Baseline). The top
bar has an **engine health pill**, refreshed every 5 s from `/api/pipeline`:
red (unavailable) when the heartbeat is stale (older than 30 s), yellow
(degraded) when the engine reports warnings, green otherwise.

## Alerting

One card per **alert row** (ARCHITECTURE §5): a document, an undocumented
template, or the whole service. KPI cards count open rows and rows per state
(ALERTING, WARMING, COOLING); they and the state toggle filter the list.
Rows keep the server's order (ALERTING first, then oldest).

Each card shows the state badge, `service · title` (document title, or
"Undocumented", or "Whole service") and `since`. Its lines are:

- `totals` first: "All events" (`*total`), "Errors" (`*error`) or "Linked
  templates combined" (the `doc:` key);
- then each alerting template's text.

Each line shows the last closed minute's count per minute and "(normal mean
± σ)" of the **current local hour** from the committed baseline, with the
alert band (`mean ± kσ`) on hover; "(no baseline yet)" means floor-only.
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
- a table of indices: position, floor, and `failing` / `caught up` /
  `catching up` / `not reading` (an `es_cursors` row the engine no longer
  reports).

## Templates

A paginated table of templates (100 per page by default; 50, 100 or 200
selectable in the page, server cap 500), newest `last_seen` first. Filters:
service, level, and a text search (case-insensitive substring of the
template text, debounced 300 ms).

Columns: alert state, template id, text (`<*>` highlighted), level, **rate**
(average per minute in the current local hour = this hour's count /
counted minutes; red when above the band), **band** (`mean ± kσ` for this
hour, judged exactly as detection does; "—" without a baseline), the
**document** select, first seen, last seen.

Choosing a document in the select links the template (`PUT …/link`);
"— None —" unlinks it. Each change enqueues `reload_links`, so the
engine's doc keys and alert rows follow within a tick. The table is not
re-rendered while a select has focus.

## Documentation

The runbook corpus: id, title, body, error code and the number of linked
templates (expandable to the linked templates, from
`/api/templates?doc_id=…`). Columns sort client-side.

- **Add document** opens the editor; the server assigns the next
  `DOC-NNN` id.
- **Edit** sends the revision read with the list; a 409 `stale` says the
  document changed in another window.
- **Delete** asks for confirmation and is refused while templates are
  linked (also when a link raced in after the list loaded: 409 `linked`).

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
| `GET /api/pipeline` | — | `{status, cursors, stale}` | — | — |
| `GET /api/sources` | — | `{selection, connection, available_indices, resolution, indices}` | — | — |
| `PUT /api/sources/selection` | `{patterns: [str], revision}` | `{revision}` | 400, 409 | `reload_sources` |
| `PUT /api/sources/connection` | `{hosts: [url], username, password, verify_certs, revision}` | `{revision}` | 400, 409 | `reload_sources` |
| `GET /api/alerts` | — | `[alert row]` | — | — |
| `GET /api/services` | — | `[service]` sorted | — | — |
| `GET /api/templates` | query `page`, `size`, `service`, `level`, `doc_id`, `q` | `{total, items}` | 400 | — |
| `PUT /api/templates/<service>/<template_id>/link` | `{doc_id: str \| null}` | `{doc_id}` | 400, 404 | `reload_links` |
| `GET /api/documents` | — | `[document]` | — | — |
| `POST /api/documents` | `{title, body, error_code?}` | `201 {doc_id, revision}` | 400 | — |
| `PUT /api/documents/<doc_id>` | `{title, body, error_code?, revision}` | `{doc_id, revision}` | 400, 404, 409 | — |
| `DELETE /api/documents/<doc_id>` | — | `{doc_id}` | 404, 409 | — |
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
heartbeat is missing or older than 30 s.

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

`kind` is `document`, `template` or `service`; `doc_id` and `title` are null
except for documents. `templates` holds the template members, `totals` the
`*total`, `*error` and `doc:` members. `count` and the bands are from the
last judged minute; `mean` / `std` are this local hour's baseline (null
without one; for a doc key, the sums over its linked templates).

### `GET /api/templates`

- `page` (default 1) and `size` (default 100) are clamped to 1..10⁶ and
  1..500; a non-integer is 400 `"<name> must be an integer"`.
- `service`, `level`, `doc_id`: exact match; `q`: case-insensitive
  substring of the text.

```json
{"total": 308,
 "items": [{"service": "recharge", "template_id": "recharge:T00032",
            "text": "LixiHandler: <*> enterd Receiving subscriber incorrect: <*>",
            "level": "ERROR", "first_seen": 1792207560.0, "last_seen": 1792381140.0,
            "doc_id": "DOC-001", "doc_title": "...", "state": "NORMAL",
            "rate": 2.4, "band_low": 0.0, "band_high": 9.4}]}
```

`rate` is null when the service has no counted minute in the current hour;
`band_low` / `band_high` are null without a baseline.

### `PUT /api/templates/<service>/<template_id>/link`

`{"doc_id": "DOC-003"}` links (replacing any link, `source` `manual`),
`{"doc_id": null}` unlinks. 400 `"doc_id must be a string or null"`; 404
`"Unknown template"` or `"Unknown document"`. The link and its
`reload_links` command commit together. The template id may contain `/`;
the service may not.

### Documents

- `GET /api/documents`: `[{doc_id, title, body, error_code, revision,
  linked_templates}]` sorted by `doc_id`.
- `POST`: `title` (≤ 200) and `body` (≤ 20000) required and non-empty after
  trimming, `error_code` optional (≤ 100). The id is `DOC-` + the next number
  after the highest `DOC-<digits>` id, zero-padded to 3 (`DOC-013`); the
  table is locked during the insert so two creates never collide. 400
  `"<field> must be text of at most N characters"` or `"title and body are
  required"`.
- `PUT`: same fields plus `revision`; 404 `"Unknown document"`; 409 stale.
- `DELETE`: 409 `linked` with the number of linked templates (the foreign
  key decides, so a link created concurrently is caught); 404.

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
