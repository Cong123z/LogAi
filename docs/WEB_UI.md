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
| Templates | `/#templates` | only with Auto-refresh (10 s) | `GET /api/templates`, `/api/template-levels`, `/api/modules`, `/api/services`, `/api/documents` |
| Documentation | `/#documentation`, `/#documentation/suggestions` | on entry and after each change | `GET /api/documents`, `/api/services`, `/api/templates?link_source=draft` |
| Data sources | `/#sources` | every 2 s | `GET /api/sources` |
| LLM profiles | `/#llm` | every 5 s | `GET /api/llm/profiles`, `/api/llm/usage` |
| Baseline | `/#baseline/<service>/<key>/<days>` | on entry and after each change; no timers | `GET /api/services`, `/api/templates`, `/api/baseline/groups`, `/api/baseline/series`, `/api/baseline/matrix`, `/api/baseline/incident` |
| Review | `/#review/<service>/<key>/<days>[/<day>/<from>/<to>]` | on entry and after each change; every 1 s while a recompute runs | `GET /api/baseline/matrix`, `/api/baseline/groups`, `/api/baseline`, `/api/baseline/inspect`, `/api/services`, `/api/templates`, `/api/documents`, `GET /api/commands/<id>` |

The sidebar groups them into Monitor (Alerting, Pipeline), Catalog
(Templates, Documentation, Baseline, Review) and Settings (Data sources, LLM profiles). The top
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
Every line ends with a "chart" link to Baseline for its key. Under the
header, buttons (plain `<a href>` links) by kind of row: a document has
**Open document** (`#documentation/<doc_id>`), **Baseline** (`doc:<doc_id>`)
and **N templates** (`#templates/<service>?doc_id=<id>`), plus the first
step of its Handling; an undocumented template or cluster has **Link a
document** (`#templates/<service>/<template_id>`, opens its drawer), **AI
suggestion** when a draft exists (`#documentation/suggestions/<doc_id>`) and
**Baseline** (`*error`); the whole service has **Baseline** (`*total`) and
**Pipeline**. Documentation also takes `#documentation/<doc_id>` to select a
document; Templates parses `?doc_id=` itself from the part after the first `/`;
`#pipeline?services=a,b` keeps the query in the sub-view too.

A template line also shows how it got its document when that was not by
hand: `inherited`, `auto 0.87` (matched on the document's ERROR lines),
`AI-confirmed`, or `AI suggestion: <title>` (a draft: shown, drives
nothing), plus `SAFE` for a normal-operation document.
A "Live · updated Ns ago" label turns into a stale warning after 15 s
without a successful poll.

## Pipeline

Live status from `worker_status` (engine and linker) and the database,
refreshed by polling `/api/pipeline`. All charts are ECharts; every poll
updates the existing chart instances.

- status line (engine running with its heartbeat / not running with the last
  heartbeat and "Showing its last numbers" / never reported) and the engine's
  warnings;
- **Topology**: eight stage nodes (Elasticsearch, Reader, Templates, Minutes
  & detection, Postgres flush, Prometheus on the main row; Linker and LLM
  resolver above, with Linker to Postgres flush dashed "via Postgres"). Each
  node's border and word show OK / Degraded / Down as the server reports it
  (`stages`; the page never recomputes a status). Headline and second line
  per stage:

  | Stage | Headline | Second line |
  |---|---|---|
  | Elasticsearch | `N / M indices` not failing | oldest position age |
  | Reader | events/s (15-min average) | cache hit ratio |
  | Templates | templates in all services | new in 24 h, unparsed lines in the last hour |
  | Minutes & detection | lag | detection On/Off, keys not NORMAL |
  | Postgres flush | last flush time | slowest flush of the last 15 min |
  | Prometheus | "metrics export" | "scraped by Prometheus" |
  | Linker | pending templates + documents | links changed last cycle or not |
  | LLM resolver | "N / M in 24 h" (`llm_today` of `llm_daily_max`: rolling 24 h LLM calls counted against `llm.daily_max`, including calls that wrote nothing; "N in 24 h" when `llm.daily_max` is 0, no limit) | profiles up / down |

  Particles move along the main row at a speed stepped by events/s; they stop
  under reduced motion or when the engine is not running. Click a stage for
  its details under the chart (index table, per-service table, flush and
  commit times with late counts, linker pending and last error, LLM profiles
  with state and answers);
- six tiles: **Lag now** (amber above 5 min), **Events/s** (15-min average),
  **Cache hit ratio**, **Late counts** (since start; should stay 0),
  **Detection**, **Bootstrap**;
- **Lag, last 60 min** and **Events/s, last 60 min**: one point per minute
  from the engine's `history`; a dashed line at 5 min marks where lag turns
  Degraded;
- **Throughput, last 60 min**: events per closed minute (engine status
  `service_minutes`, in memory: empty right after an engine restart) for the
  services ticked in the table. Default the 5 busiest by 15-min average; at
  most 8 (a 9th tick is refused with a short message). **Combined** draws one
  line per service in one chart, **One per service** one small chart each.
  The choice is kept in the URL (`#pipeline?services=a,b`) and in
  `localStorage` (`pl.services`, `pl.mode`); the URL wins. With a stale
  engine the last numbers stay; with no points an empty state is shown;
- **Services** table (sortable, filtered by a search box): a checkbox per
  row (the chart), last minute, 15-min average, 60-min sparkline, change vs
  1 h ago (only when the oldest point is 55-60 min old), 5-min miss ratio,
  templates, new in 24 h, unparsed in 1 h, keys not NORMAL. A row click
  filters the Reader and Templates details to that service;
- a table of indices: position, floor, and `failing` / `caught up` /
  `catching up` / `not reading` (an `es_cursors` row the engine no longer
  reports).

Stage rules (constants in `logai2/web/pipeline_view.py`):

| Stage | Degraded | Down |
|---|---|---|
| Elasticsearch | an index failing, one more than 5 min behind, or engine stale (position unknown) | all indices failing |
| Reader | a high-miss-ratio warning | engine stale, or 5 minutes of history at 0 events/s while an index is not caught up |
| Templates | any unparsed line in the last hour | — |
| Minutes & detection | lag over 5 min, or detection off | engine stale |
| Postgres flush | slowest flush of the last 15 min over 10 s, or last commit older than 26 h | engine stale |
| Prometheus | — | — (always OK) |
| Linker | `last_error` set | no linker status, or stale |
| LLM resolver | a profile down | no linker, no active profile, or all profiles down |

## Review

Route `#review/<service>/<key>/<days>[/<day>/<hour_from>/<hour_to>]` (the
selection part is optional; `main.js` keeps everything after the first `/`
in `state.subView`). This is the only page that records decisions.

- header: a service select, 14 / 28 days, the same **key picker** (below), **Recompute baseline** (queues
  `recompute_baseline` and polls the command every second until it reports
  `days_committed` and `baseline_rows`, or an error);
- Decisions are per service, day and hours, so excluding or keeping a period
  while looking at `*warn` works as for any key; the **To review** list is
  service-wide;
- cards: **weekday profile** and **weekend profile** (none, young, full,
  borrowed; with the days counted and the multiplier k), **To review**
  (opens the first one), **Last commit**;
- **Days x hours**: an ECharts heatmap (or a table, by the Chart / Table
  toggle). One cell per hour, coloured by how far the rate was from the
  baseline (`z = (rate - mean) / sigma`): seven steps, neutral for no
  baseline. Marks: a triangle needs review, a cross excluded, a tick kept,
  a dot a day the next rebuild uses. Click selects, shift+click extends
  within the day, arrow keys move, Enter selects. The side lists show **To
  review** (periods in which any key of the service was not NORMAL and that
  have no decision) and **Decisions** with **Undo**;
- **Inspector** for the selection: a sentence (peak rate and z, expected
  mean and sigma, minutes not NORMAL, keys that left NORMAL, whether the day
  is in the baseline, an earlier decision), the whole day against the
  expected band, **Increased** templates (bar chart and table: rate,
  expected, times normal, minutes not NORMAL, document), **New templates**,
  **Keys that left NORMAL** with their periods, and the buttons **Exclude:
  real incident** / **Keep: normal** (a note says what the days left would be
  and warns below 3; decisions take effect at the next commit or Recompute);
- **Coverage**: usable days per hour for weekday and weekend, out of the
  days the rebuild picks (5 weekdays, 4 weekend days); an hour with fewer than
  3 has an orange outline and falls back to floor-only alerts.

Unreviewed hours are kept in the baseline.

## Templates

A stats row on top (`stats` in `GET /api/templates`): **Templates**, **Known**
(linked to a document by `manual`, `inherited`, `corpus` or `llm`),
**Unknown** (the rest: not linked, AI suggestion, rejected, no document) and
**ERROR / WARN / INFO** (level classes). They follow the service, level and
search filters, not the mode, the link filter or the known filter.

Templates, Known and Unknown are buttons: a click opens All templates with the
**All · Known · Unknown** toggle set to match (`?status=known|unknown`; the
pressed card is outlined). The toggle sits next to the mode toggle, is hidden
and cleared in To review like the link filter, and each row carries a
**Known** / **Unknown** badge next to its level (`known` per item).

Two modes, a toggle at the left of the filter bar:

- **To review (N)**: templates whose link is automatic (`inherited`,
  `corpus`, `llm`, `draft`) and waits for a person: ALERTING first, then
  ERROR > WARN > INFO, then newest `last_seen`. N follows the service,
  level and search filters. The page opens here when N > 0, else on All
  templates (the choice is not kept across reloads).
- **All templates**: every template, newest `last_seen` first (the
  **First seen** and **Last seen** headers sort, `aria-sort`), with a
  **link** filter (not linked, or one link source); hidden and cleared in
  To review.

Both modes: service (a text box with a suggestion list of the known
services; an exact name filters, empty = all), level (every level present in the data with its
count, "ERROR (12)"; follows the service) and a text search (case-insensitive substring,
debounced 300 ms); 100 per page by default (50, 100 or 200; server cap
500). To review with nothing left says so and offers All templates.

**Module** filter row (only with one service selected; hidden for All services; cleared
when the service changes): "All modules" and a button per module with its template count
(`GET /api/modules?service=`). A module shows the templates ever seen in it (`?module=`;
`""` = "(no module)" is sent as an explicit empty value), those with lines in the last
24 h first; its chip is highlighted and listed first in each row. A note line above the
table explains the chips. The drawer has a **Last 24 h · N modules** section: lines,
share bar and last seen per module.

Columns: **state** (WARMING / ALERTING / COOLING badge; NORMAL as muted
text), **template** (id and level, then the full text, `<*>`
highlighted), **modules · last 24 h** (one chip per module that ever logged
the template, `name share%`; in the last 24 h by lines, then grey dashed chips without %
that logged it earlier, last seen on hover; no module = "(no module)"; every chip
is shown), **rate** (average per minute this local hour; above the band
in red with "▲ above normal"; under it the band as `normal ≤ X/min` or
`normal A–B/min`, judged exactly as detection does; "—" without data),
**document** (`DOC-007 · title`, or "Not linked" / "No document (set by
hand)"; under it how it was linked: `manual`, `inherited 0.91`,
`auto-matched 0.87`, `AI-confirmed`, `AI suggestion · unverified`, `AI
result rejected`, and SAFE), **first seen** and **last seen** (relative, full time on
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
  A second button group ("Any level · ERROR 3 …") filters by the level of
  the template each was written for; it combines with the kind buttons.

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
limit. The **Call order** list shows the active profiles as 1, 2, 3: a
badge says IN USE (the first one not failing), STANDBY, or FAILING ·
skipped until hh:mm (with the error); each has ↑ / ↓ (real buttons, disabled
at the ends), Edit and Stop using, and "if it fails, call #n+1" sits between
items. Every move saves the whole order (`PUT /api/llm/profiles/order`).
**Not used** lists the other profiles with Add as #n, Edit and Delete. A
last line gives the 24 h calls (of `llm.daily_max`), results and the
suggestions waiting. When one profile fails (network, HTTP error,
timeout) the next one answers and the failing one is skipped for 5
minutes; the banner says whether another profile is answering. IN USE
shows the answers counted since the linker started.

Each profile, as in logai-engine: name, OpenAI-compatible base URL
(`…/v1`, or a bare host which gets `/v1`; the linker posts to
`…/chat/completions`), model, API key, timeout (5–600 s, default 120) and
no priority field: the order is set by the list (a new profile gets the
next number after the active ones; an edit keeps its place). With no active profile, templates are not
sent to an LLM. The API key is
write-only: the list shows only whether one is saved, and a save with an
empty key keeps the saved one.

## Baseline

Route `#baseline/<service>/<key>/<days>` (days 1, 7 or 14). Read-only: it
shows how one key behaved against its baseline; decisions are made on the
Review page.

- header: a service select and Today / 7 days / 14 days, then the **key
  picker** (below);
- **Key picker** (`js/keypicker.js`, shared with Review): group buttons
  with counts, All events, then Level (ERROR, WARN, INFO) and Documentation
  (ERROR docs, SAFE docs); a click shows the group's combined series
  (`*total`, `*error`, `*warn`, `*info`, `verdict:action`, `verdict:safe`).
  Counts are exact, from `GET /api/baseline/groups` (not capped by the 500 templates the page loads). A group with nothing in it for the service is disabled. A documentation group's count is the active documents with a linked template (manual, inherited, auto-matched, AI-confirmed) in this service; a verdict series is the sum of those templates. The Errors incident list shows modules of ERROR templates only. **Show** opens a
  searchable list: the combined key, then documents, then templates (newest
  first). When the service has more templates than the page holds, typing searches the server (`/api/templates?q=`, 50 rows, 300 ms debounce) and the list ends with "Showing N of M". Keys: Up / Down, Enter, Esc. A line under it says how the series is
  counted (stored key, new stored key with a young baseline, derived from
  linked templates, single template). The group follows the key, so old
  links keep working. A template belongs to the group of its stored level
  (its worst level seen), so its lines can be counted in `*warn` while the
  template is listed under ERROR. `*warn`, `*info` and the verdict keys are
  view-only: they have no alerts of their own;
- cards: **Now** (rate and its distance from the band), **Possible
  incidents** (count and hours outside the band), **Needs review** (links to
  Review), **Baseline** (Full, Young, Borrowed or None yet);
- **Actual vs expected**: an ECharts line of the hourly rate against the
  expected band and the same hour last week, with out-of-band stretches
  shaded as possible incidents and a cross / tick where Review excluded /
  kept the period; zoom by scroll or drag;
- **Possible incidents**: runs of consecutive hours outside the band (an
  hourly view; detection judges minutes), with when, state (ongoing /
  resolved), duration, peak, expected, times normal and decision (excluded, kept, or
  "review" when the keys left NORMAL and nothing is decided). A row zooms the
  chart and loads the **Incident** detail: modules the lines came from and,
  for an ongoing incident, the newest sample lines;
- **Days x hours**: the same grid as Review, read-only;
- **The baseline itself**: weekday band and mean, weekend mean (dashed), and
  today so far.

## HTTP API

All routes are JSON except `/` and `/static/…`. Times are epoch seconds
unless noted; days are ISO dates (`YYYY-MM-DD`) in local time.

| Method and path | Input | Output | Errors | Command |
|---|---|---|---|---|
| `GET /` | — | `index.html` | — | — |
| `GET /static/<path>` | — | static file | 404 | — |
| `GET /api/pipeline` | query `lite=1` (only `status, cursors, stale, linker, linker_stale`: the header pill's poll) | `{status, cursors, stale, linker, linker_stale, services, llm_daily_max, throughput, stages}` | — | — |
| `GET /api/sources` | — | `{selection, connection, available_indices, resolution, indices}` | — | — |
| `PUT /api/sources/selection` | `{patterns: [str], revision}` | `{revision}` | 400, 409 | `reload_sources` |
| `PUT /api/sources/connection` | `{hosts: [url], username, password, verify_certs, revision}` | `{revision}` | 400, 409 | `reload_sources` |
| `GET /api/alerts` | — | `[alert row]` | — | — |
| `GET /api/services` | — | `[service]` sorted | — | — |
| `GET /api/template-levels` | query `service` (optional) | `[{level, n}]`: every level present, FATAL … TRACE then others A-Z | — | — |
| `GET /api/templates` | query `page`, `size`, `service`, `level`, `doc_id`, `template_id`, `link_source`, `q`, `module`, `sort`, `status` | `{total, review_total, stats, items}`; each item has `modules: [{module, lines, share, last_seen}]` | 400 | — |
| `GET /api/modules` | query `service` (required) | `[{module, templates, lines_24h}]` by `templates` desc | 400 | — |
| | | `modules` are over the last 24 hourly buckets (current local hour - 23 .. current hour): in-window modules by `lines` desc with `share` (fraction of the template's window lines), then older ones by `last_seen` desc with `lines` 0 and `share` null. `module` (`""` = no module) keeps templates ever seen in that module, those with window lines in it first, and narrows `total` and `stats` too. | | |
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
| `PUT /api/llm/profiles/<name>` | `{base_url, model, api_key?, timeout_seconds?, priority?, revision}` (no `priority`: an edit keeps it, a new profile gets max(active)+1, 1 if none) | `{name, revision}` | 400, 409 | — |
| `PUT /api/llm/profiles/order` | `{order: [name, ...]}` | `{order}`: the listed profiles are active with priority 1..n, every other one inactive (one transaction) | 400 (not a list of distinct names, unknown profile) | — |
| `POST /api/llm/profiles/<name>/activate` | `{active: bool}` | `{name, active}` | 400, 404 | — |
| `DELETE /api/llm/profiles/<name>` | — | `{name}` | 404 | — |
| `GET /api/baseline/groups` | query `service` (required) | `{all, ERROR, WARN, INFO, action, safe}`: templates per level class; active documents per verdict with a template of this service linked by manual, inherited, corpus or llm | 400 | — |
| `GET /api/baseline/matrix` | query `service`, `key` (default `*total`; also `*error`, `*warn`, `*info`, `verdict:action`, `verdict:safe`, `doc:<id>`, a template id; same for the other `/api/baseline/*` routes), `days` (1–28, default 14) | `{service, key, k, baseline_state, days, coverage, profiles}` | 400, 404 | — |
| `GET /api/baseline/inspect` | query `service`, `key`, `day`, `hour_from`, `hour_to` | `{sentence, day, increased, new, keys, left_days}` | 400, 404 | — |
| `GET /api/baseline/series` | query `service`, `key`, `days` (1, 7 or 14, default 14) | `{key, baseline_state, points, incidents, profile}` | 400, 404 | — |
| `GET /api/baseline/incident` | query `service`, `key`, `day`, `hour_from`, `hour_to` | `{ongoing, modules, samples}` | 400, 404 | — |
| `GET /api/baseline` | query `service` | `{days, suggestions, decisions, last_commit, commit_time, full_after_days}` | 404 | — |
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
`warnings`, `last_error`, `llm`, `llm_today`, `llm_profiles`) or null;
`linker_stale` is true when it is missing or older than 90 s.

`services` has one row per service, by name; `throughput` and `stages` follow.
`last`, `avg15` (mean of the last 15 minutes) and `delta_1h` (last / first
point - 1, only when the first point is 55-60 min older and above 0) come
from `throughput` and are null without points. `unparsed_1h` is the
`<service>:unparsed` count of the current hour:

```json
{"services": [{"service": "recharge", "last": 412, "avg15": 405.2, "delta_1h": 0.08,
               "miss_ratio": 0.02, "templates": 87, "new_24h": 3,
               "unparsed_1h": 0, "not_normal": 1}],
 "throughput": {"recharge": {"points": [[1792381080, 398], [1792381140, 412]], "last": 412,
                             "avg15": 405.2, "delta_1h": 0.08}},
 "stages": {"elasticsearch": {"status": "ok", "reason": null},
            "reader": {"status": "ok", "reason": null},
            "templates": {"status": "warn", "reason": "4 unparsed lines in the last hour"},
            "detection": {"status": "ok", "reason": null},
            "flush": {"status": "ok", "reason": null},
            "prometheus": {"status": "ok", "reason": null},
            "linker": {"status": "ok", "reason": null},
            "llm": {"status": "bad", "reason": "no active LLM profile"}}}
```

`throughput[service].points` is `[minute_start_epoch_s, events]`, oldest
first, up to 60 closed minutes, zero minutes included; a service with no
points is absent. `stages`
status is `ok`, `warn` (Degraded) or `bad` (Down); the rules are in the
Pipeline section. The `status` object also carries `flush_seconds`,
`flush_seconds_max`, `late_counts`, `events_per_second_15m` and `history`
(`[{t, lag, eps}]`, one point per wall minute, 60 kept); see ARCHITECTURE
§10.5.

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
`title` are null except for documents. Document rows also have
`handling_step` (first non-empty line of the Handling without its `1.`, `1)`
or `-` marker, or null); template and cluster rows have `draft_doc_id` (a
`draft` link of any of their templates, or null). Each template line has `link`:
null, or `{doc_id, source, similarity, note, title, verdict}` of its
`template_doc_links` row, drafts and rejected ones included (badges only:
grouping uses only the links that drive alerts). `templates` holds the template members, `totals` the
`*total`, `*error` and `doc:` members. `count` and the bands are from the
last judged minute; `mean` / `std` are this local hour's baseline (null
without one; for a doc key, the sums over its linked templates).

### `GET /api/module-alerts`

For other services: which templates were WARMING or ALERTING in a window, and how many
lines one module wrote in them. Spec: `docs/specs/2026-10-22-module-alert-api.md`;
caller guide (Vietnamese): `docs/API.md`.

```
GET /api/module-alerts?module_code=VTN_CNTT_VAS_066_301&from=2026-09-30T07:00:30.500Z&to=2026-09-30T07:14:10.200Z
Authorization: Bearer <token>
```

| Parameter | Rule |
|---|---|
| `module_code` | required, trimmed, exact (case-sensitive); spaces, `/` and Unicode allowed (URL-encode) |
| `from`, `to` | both required: ISO-8601 (offset allowed, none = UTC) or epoch (> 1e11 = ms, else s) |
| `Authorization` | only when `LOGAI2_API_TOKEN` is set: `Bearer <token>` |

```json
{"module_code": "VTN_CNTT_VAS_066_301",
 "from": "2026-09-30T07:00:30.500Z", "to": "2026-09-30T07:14:10.200Z",
 "effective_from": "2026-09-30T07:00:00Z", "effective_to": "2026-09-30T07:15:00Z",
 "resolution": "minute", "data_until": "2026-09-30T07:13:00Z", "unit": "events_per_minute",
 "templates": [{"service": "vtn_cntt_vas_066", "template_id": "vtn_cntt_vas_066:T00431",
   "text": "Timeout acquiring connection <*>", "level": "ERROR",
   "doc_id": "DOC-017", "doc_title": "Restart DB pool", "peak_state": "ALERTING",
   "periods": [{"start": "2026-09-30T06:58:00Z", "end": null}], "alerting_minutes": 15,
   "module": {"total": 300, "avg": 20.0, "peak": 42,
              "series": [{"minute": "2026-09-30T07:00:00Z", "count": 18}]},
   "template": {"total": 420, "avg": 28.0, "peak": 55}}]}
```

- Times are UTC with `Z`; milliseconds appear only when non-zero.
- Rounding is outward to whole minutes: `effective_from` = `from` rounded down,
  `effective_to` = `to` rounded up (unchanged on a boundary), window `[effective_from, effective_to)`.
  `07:00:30.500 -> 07:00:31.200` reads `[07:00, 07:01)`; `07:00:59.999 -> 07:01:00.001` reads
  `[07:00, 07:02)`; `from == to` reads the one minute holding it. `from` older than
  `retention.counts_days` (local midnight) and `to` after now are clamped; `effective_*` show it.
- A template counts while WARMING or ALERTING (COOLING and NORMAL minutes do not).
  `peak_state` is `ALERTING` if it reached that state in the window, else `WARMING`.
  A period opened by NORMAL to WARMING is back-dated to its first high minute.
- `periods[].end: null` = still open at the window's end. Periods are minute starts, not clipped to the window.
- `data_until` = end of the last minute the engine judged (`null` before the engine ran). When
  `effective_to > data_until` the tail is not judged yet: an empty list there is not "no alert".
- `series` is per minute (`minute`) for a window of one day or less, per hour (`hour`, hours in the
  deployment zone, +07) above that.
- Only ERROR/WARN templates alert; DEBUG/INFO templates never appear.
- Errors: 400 `{"error": ...}` for a missing or empty `module_code`, a missing `from`/`to`, an
  unparsable time, or `from > to`; 401 `{"error": "unauthorized"}` when a token is set and the header
  is missing or wrong (checked before the parameters); an unknown module is 200 with `templates: []`.
- Token: `LOGAI2_API_TOKEN` (`web.api_token`); empty = open like the rest of the API. It guards this
  route only.

### `GET /api/templates`

- `page` (default 1) and `size` (default 100) are clamped to 1..10⁶ and
  1..500; a non-integer is 400 `"<name> must be an integer"`.
- `service`, `level`, `doc_id`, `template_id`: exact match; `q`: case-insensitive
  substring of the text; `link_source`: `none` (no row), `review` (an
  automatic link: `inherited`, `corpus`, `llm`, `draft`) or one of
  `manual`, `inherited`, `corpus`, `llm`, `draft`, `rejected` (else 400).
- `sort`: `last_seen` (default) or `first_seen`, newest first, then
  `template_id`; anything else is 400; ignored with `link_source=review`.
- Order: by `sort`; with
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
  error_lines, linked_templates, template_level}]` sorted by `doc_id`. `error_lines` = the
  ERROR field normalized as the linker embeds it; `embedded` = the linker
  has embedded this revision; `linked_templates` counts links that drive
  alerts; `template_level` = level of the template a draft was written
  for (its `draft` link), else null.
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
  `llm.daily_max` (0 = no limit, shown as "no limit"); `linker_running` (heartbeat within 90 s), `last_cycle`
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

### `GET /api/baseline/matrix`

`service` (404 `"Unknown service"`), `key` is `*total` (default), `*error`, `*warn`, `*info`,
`verdict:action`, `verdict:safe` (the service's templates linked (manual, inherited, auto-matched, AI-confirmed) to active documents of that verdict,
404 when none is linked in the service), `doc:<doc_id>` (the document's alerting templates) or a template id (404
`"Unknown key"`), `days` is an integer 1–28 (400 `"days must be an integer
from 1 to 28"`). `days` are newest first and hold only hours with minutes
read:

```json
{"service": "recharge", "key": "*total", "k": {"weekday": 3.0, "weekend": 4.0},
 "days": [{"day": "2026-10-21", "daytype": "weekday", "in_baseline": true,
           "hours": [{"hour": 9, "rate": 412.0, "mean": 120.5, "std": 30.1,
                      "band_low": 30.2, "band_high": 210.8, "z": 9.7, "no_baseline": false,
                      "cls": "up2", "upper_only": false, "not_normal_minutes": 12,
                      "suggested": true, "decision": null}]}],
 "coverage": {"weekday": [{"hour": 9, "days": 5, "of": 5, "idle": false,
                           "used": ["2026-10-20"], "excluded": []}], "weekend": ["..."]},
 "profiles": {"weekday": "full", "weekend": "young"}}
```

`cls` is the colour step: `in`, `up1`–`up3`, `down1`–`down3` (|z| up to 6,
up to 10, beyond; "down" only where the detector raises a drop: `*total`, full profile, mean ≥ `drop.min_mean`) or `none` (no baseline). A baseline of mean 0 and σ 0 gives
`up3` for any event and `in` otherwise. `rate` is events per minute,
`z = (rate - mean) / max(σ, 1)` when mean > 0, else `(rate - mean) / σ`. `suggested` means keys of the service left
NORMAL in that hour and no decision covers it; `decision` is the latest
`exclude` / `keep` covering it. `coverage` lists 24 hours per day type;
`profiles` is `none`, `young`, `full` or `borrowed` (a weekend with fewer
than 2 days that borrows the weekday profile).

### `GET /api/baseline/inspect`

`day` and `hour_from` / `hour_to` are validated like the decisions (400 with
the same messages; `hour_to` exclusive).

```json
{"sentence": {"keys": 2, "not_normal_minutes": 35,
              "peak": {"hour": 10, "rate": 412.0, "z": 9.7},
              "expected": {"mean": 120.5, "std": 30.1}, "in_baseline": true, "decision": null},
 "day": [{"hour": 0, "rate": 118.0, "band_low": 30.2, "band_high": 210.8,
          "last_week": 112.0, "not_normal_minutes": 0}],
 "increased": [{"template_id": "recharge:T00032", "text": "...", "level": "ERROR", "rate": 90.0,
                "mean": 4.0, "excess": 86.0, "times_normal": 22.5, "not_normal_minutes": 35,
                "doc_id": "d1", "doc_title": "Runbook"}],
 "new": [{"template_id": "recharge:T00099", "text": "...", "level": "ERROR",
          "first_seen": 1792400000.0, "rate": 2.0, "doc_id": null}],
 "keys": [{"key": "recharge:T00032",
           "periods": [{"from": 1792400000.0, "to": 1792402100.0, "state": "ALERTING"}]}],
 "left_days": [{"hour": 10, "of": 5, "days": 4}]}
```

`peak` and `expected` are null when no hour of the range has minutes read;
in `day` (the whole day, 24 rows, with the same hour a week earlier) `rate`
is null for an hour with no data. `increased` is the 10 templates whose rate rose most over their
baseline (a template in `new` is left out); `new` are templates first seen
from one hour before the range to its end. `left_days` is what the rebuild
would have per hour of the range if this range were excluded.

### `GET /api/baseline/series`

`days` is 1, 7 or 14 (400 `"days must be 1, 7 or 14"`); `1` is today.
`points` run oldest first, one per hour (today up to the current hour):

```json
{"key": "*total", "baseline_state": "full",
 "points": [{"day": "2026-10-21", "hour": 9, "rate": 412.0, "mean": 120.5, "std": 30.1,
             "band_low": 30.2, "band_high": 210.8, "last_week": 380.0, "z": 9.7, "out": true}],
 "incidents": [{"day": "2026-10-21", "hour_from": 9, "hour_to": 11, "hours": 2, "ongoing": true,
                "peak": {"hour": 10, "rate": 412.0, "mean": 120.5, "band_low": 30.2,
                         "band_high": 210.8, "z": 9.7},
                "times_normal": 3.4, "decision": "review"}],
 "profile": {"weekday": [{"hour": 0, "mean": 118.0, "band_low": 30.0, "band_high": 206.0}],
             "weekend": [], "today": [{"hour": 0, "rate": 120.0}]}}
```

`baseline_state` is `none`, `young`, `full` or `borrowed`. A point is `out`
when its rate is above the band (or below it, where the detector raises a drop: `*total`, full profile, mean ≥ `drop.min_mean`); an incident is a run of consecutive `out` hours.
`decision` is `exclude`, `keep`, `"review"` (keys left NORMAL in the run and
nothing decided) or null. `rate` is null for an hour with no `service_hours`
row (its band is still given). For a `doc:` key `profile.weekday` and `profile.weekend` are empty; `today` is still filled.

### `GET /api/baseline/incident`

Same `day` / hour validation as `inspect`.

For `*error` the modules come from ERROR templates only; `*warn` lists WARN templates only (and never returns samples).

```json
{"ongoing": true, "modules": [{"module": "RechargeService", "lines": 340}],
 "samples": [{"at": 1792402000.0, "template_id": "recharge:T00032", "line": "ERROR ..."}]}
```

`modules` are the modules of the `alert_minutes` counts in the range, most
lines first. `samples` are empty unless the range includes the newest hour
read (`ongoing`): then the 10 newest `error_samples` lines of the key's
templates (for `*total` and `*error`, of the 3 ERROR+ templates that rose
most over their baseline).

### `POST /api/baseline/decisions`

`{"service", "day": "YYYY-MM-DD", "hour_from": int, "hour_to": int,
"decision": "exclude" | "keep"}` with `0 <= hour_from < hour_to <= 24`
(`hour_to` exclusive; the hours must be real integers in the body, not
strings). Validation order: day (400 `"day must be an ISO date
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

The API has no authentication or authorization (except the optional shared bearer token on
`GET /api/module-alerts`), and the connection endpoint
lets a caller point the engine at any http(s) host. Expose the web only on a
trusted network. The ES password is stored in Postgres in clear and never
returned.

## Implementation map

| Area | Files |
|---|---|
| Routes, validation | `logai2/web/app.py` |
| Shell, routing, polling, health pill | `static/index.html`, `static/js/main.js`, `core.js`, `ui.js` |
| Views | `static/js/views/{alerting,pipeline,templates,documentation,sources,baseline,review,llm}.js` |
| Charts | `static/js/charts.js` (ECharts helpers, colour tokens); `static/vendor/echarts.min.js` (ECharts 5.5.1, Apache-2.0, `ECHARTS-LICENSE`, self-hosted) |
| Baseline and Pipeline data | `logai2/web/baseline_view.py` (matrix, inspect, series, incident), `logai2/web/pipeline_view.py` (services, throughput, stage states) |
| Styles | `static/css/tokens.css`, `app.css` (copied from logai-engine) |
| Shared with the engine | `logai2/alerts.py` (alert rows), `logai2/owner/baseline.py` (bands), `logai2/review.py` (suggestions; minutes and keys per hour), `logai2/commands.py` |
| Tests | `tests/test_web_sources.py`, `test_web_alerts_templates.py`, `test_web_docs_baseline.py` |
