# UI improvements and template modules: design

Status: agreed with the user 2026-10-23. Mockup: Design canvas
https://claude.ai/artifact/PwiXQK3bA17cK28yETYZcB (boards marked "v2" win where they differ).
Plan: docs/plans/2026-10-23-ui-improvements-modules.md.

Out of scope: Data sources (backfill is its own sub-project), Review, auto-approving SAFE
suggestions (the existing "Approve all SAFE" button stays as is).

## 1. Alerting: links out of each card
- Document row: buttons **Open document** (`#documentation/<doc_id>`), **Baseline**
  (`#baseline/<service>/doc:<doc_id>/1`), **N templates** (`#templates?service=<s>&doc_id=<id>`), and
  the first step of the document's Handling under the header.
- Undocumented template / cluster row: **Link a document** (`#templates/<service>/<template_id>`, opens
  its drawer), **AI suggestion** when a draft exists (`#documentation/suggestions/<doc_id>`), **Baseline**
  (`#baseline/<service>/*error/1`).
- Whole-service row: **Baseline** (`*total`) and **Pipeline**.
- Every line gets a small "chart" link to Baseline for its key.

## 2. Pipeline: throughput per service, chosen by the user
- The engine reports each service's events per closed minute, last 60 minutes, in its status.
- "Volume vs expected, today" is removed (Baseline covers it).
- Services table: columns Last min, 15-min avg, 60-min sparkline, vs 1 h ago (replacing "events/min this
  hour", which averaged the hour so far); a checkbox per row picks the services drawn in the
  **Throughput, last 60 min** chart; a search box filters the table. Default: the 5 busiest; at most 8;
  one combined chart or one small chart per service (toggle). Selection kept in the URL
  (`#pipeline?services=a,b`) and in localStorage.

## 3. Templates
- **First seen** column, sortable (`sort=first_seen`).
- Service filter is a type-to-filter input (datalist), not a plain select.
- Level filter lists **every level present in the data** with its count (not a fixed list), known levels
  by severity (FATAL, CRITICAL, SEVERE, ERROR, WARNING, WARN, NOTICE, INFO, DEBUG, TRACE), others after,
  alphabetically. Same list on Documentation → AI suggestions (by the template's level).
- **Modules** (section 6): one row per template; the Modules column shows **every** module of the
  template; a module filter row; the drawer lists each module.

## 4. Baseline: exact counts
The key picker's group counts come from the server (no `500+`). The "Show" list searches the server
when the service has more templates than were loaded.

## 5. LLM profiles: call order 1, 2, 3
logai-web layout (header with status, table/list, Edit/Delete). Profiles in use form a numbered list:
priority **1** is called first; when it fails (network, HTTP error, timeout) the next is called; a failing
profile is skipped for 5 minutes (unchanged). ↑/↓ reorder, **Stop using** moves a profile to "Not used",
**Add as #N** appends it. Stored as `priority` = 1..n on active profiles; one request saves the whole
order.

## 6. Template modules
- A template (Drain3 per service) can be logged by several modules of its service. Never split it into one
  template per module; show its modules instead. The same module name in two services is two modules.
- **Window: last 24 h, by hour**: the hourly buckets from (current hour − 23) to the current hour.
  Lines and share (% of the template's lines in that window) per module.
- A module that logged the template earlier but not in the window stays listed, grey and dashed, with no
  %, its last seen on hover (last seen kept indefinitely).
- Lines without a module are stored with `module_code = ''` and shown as "(no module)".
  `<service>:unparsed` is not stored.
- Operator note: column header "Modules · last 24 h", one note line above the table, and the drawer
  header "Last 24 h · N modules".
- Module filter = templates **ever seen** in that module; those with lines in the window first.
- Storage:
  - `template_module_hours (service, template_id, module_code, day, hour, count)`, partitioned by day,
    kept **7 days** (`retention.module_hours_days`).
  - `template_modules (service, template_id, module_code, first_seen, last_seen)`, kept forever.
  - Written by the engine's flush in the same transaction as the ES cursors (exactly once), including
    bootstrap catch-up minutes.
  - Migration seeds both from `alert_minutes` (templates that alerted); others fill as they log again.
- API: `GET /api/templates?module=<m>` and a `modules` list per item; `GET /api/modules?service=<s>`.
- **Stress test** at a large scale before merge: flush cost and query latency must meet the targets in
  the plan.
