# Module alert API

Date: 2026-10-22. Status: **built**, merged into `master` as `466efad`; docs/WEB_UI.md describes the API as built. Sub-project 4. Plan:
[`docs/plans/2026-10-20-module-alert-api.md`](../plans/2026-10-20-module-alert-api.md).
Design: [`per-service-baseline.md`](../design/per-service-baseline.md) §7.

## Goal

Other services ask: **"which templates of module M were alerting between T1 and T2?"**
logai2 answers from the detection it already runs. No second detector, no new table:
the API is a read view over `modules`, `alert_changes` and `alert_minutes`.

Success: a caller sends `module_code=VTN_CNTT_VAS_066_301` and a window, and gets each
template that was WARMING or ALERTING in it with module M's lines per minute, the
template's total, its text and its runbook.

## 1. Input data (checked on a real `udcntt*` document)

```json
"_index": "udcntt-vtn_cntt_vas_066-2026.09.30",
"_source": {
  "@timestamp": "2026-09-30T07:00:32.036Z",
  "service_code2": "vtn_cntt_vas_066",
  "moduleCode": "VTN_CNTT_VAS_066_301",
  "groupModule": "GOSSIP",
  "message": "30/09/2026 14:00:31 DEBUG [gossip-handlers-321] jgroups:name=…",
  "service_code": {"host_ip": "10.240.175.121"}, "host": {"name": "VAS-APP121"}, …
}
```

Run through the current reader (`hit_to_line`, unchanged code):

| Field | Read as | Rule |
|---|---|---|
| `service_code2` | service `vtn_cntt_vas_066` | no `service` field, so `service_code2` (`service_code` holds the host IP and is never used) |
| `moduleCode` | module `VTN_CNTT_VAS_066_301` | `reader.module_field`, default `moduleCode`: a top-level string, **no change needed** |
| message prefix `DEBUG` | level `DEBUG` | no `level` / `log.level` field, so the level token after the date/time |
| `message` | template text `jgroups:name NewGossipRouterWARNING_EXTEND_DATA_MI_HT<*>: ConnectionHandler peer: <*> …` | normalized, then Drain3 per service |

So module codes are collected from the first read of a `udcntt*` index. The test stack's
`recharge-logs` has no `moduleCode`, which is why `modules` is empty there today.

**Note for callers:** only ERROR/FATAL/CRITICAL (≥ 5 per minute) and WARN/WARNING (≥ 10
per minute) templates can alert upward. DEBUG and INFO templates, like the line above,
never alert, so they never appear in this API.

## 2. Request

```
GET /api/module-alerts?module_code=VTN_CNTT_VAS_066_301&from=2026-09-30T07:00:00.000Z&to=2026-09-30T07:15:00.000Z
Authorization: Bearer <token>
```

| Parameter | Rule |
|---|---|
| `module_code` | **required**, trimmed; exact match, case-sensitive; spaces, `/` and Unicode allowed (URL-encoded) |
| `from`, `to` | **both required**. ISO-8601 with milliseconds (`2026-09-30T07:00:32.036Z`, an offset such as `+07:00` allowed, no offset = UTC) or epoch (> 1e11 = milliseconds, else seconds) |
| `Authorization` | required only when `LOGAI2_API_TOKEN` is set: `Bearer <token>` |

### Time window: 1-minute resolution, rounded outward

logai2 counts and judges per minute, so the window is widened to whole minutes and
covers every minute that holds any part of `[from, to]`:

- `effective_from` = `from` rounded **down** to its minute;
- `effective_to` = `to` rounded **up** to the next minute boundary (unchanged when already on one);
- window = `[effective_from, effective_to)`; `from == to` = the one minute holding that instant.

| Caller sends | Window | Minutes |
|---|---|---|
| `07:00:30.500 → 07:00:31.200` | `[07:00, 07:01)` | 1 |
| `07:00:59.999 → 07:01:00.001` | `[07:00, 07:02)` | 2 |
| `07:00:00.000 → 07:05:00.000` | `[07:00, 07:05)` | 5 |
| `07:03:12.345 → 07:03:12.345` | `[07:03, 07:04)` | 1 |

### Range: now back to the retention limit

No length cap. The oldest queryable minute is 00:00 local (Asia/Ho_Chi_Minh) of
`today − retention.counts_days` (90 days by default), the oldest `alert_minutes`
partition the nightly commit keeps. An older `from` is clamped to it; a `to` after now
is clamped to now (then rounded up). `effective_from` / `effective_to` show any clamp.

## 3. What counts as "alerting"

A template is returned when it was **WARMING or ALERTING** in the window and module M
logged it in those minutes. With the default hysteresis (2 / 3 / 3):

| Minute | Engine state | Counted |
|---|---|---|
| 1st high minute | NORMAL, streak 1 | **yes**: the incident's first minute |
| 2nd high minute | WARMING | yes |
| 3rd high minute on | ALERTING | yes |
| low minutes after | COOLING | no |
| back to normal | NORMAL | no |

- A streak that reaches WARMING and falls back to NORMAL without reaching ALERTING is
  returned too, with `peak_state: "WARMING"`. A caller that wants confirmed incidents
  keeps `peak_state == "ALERTING"`.
- **Template keys only.** A document key (`doc:DOC-003` over its band while no linked
  template alerts on its own) and the service keys (`*error`, `*total`) are not returned.
- A template that alerted but had no line from module M in the counted minutes is left out.
- A module logged by several services returns the templates of each.

## 4. Response

```json
{
  "module_code": "VTN_CNTT_VAS_066_301",
  "from": "2026-09-30T07:00:30.500Z", "to": "2026-09-30T07:14:10.200Z",
  "effective_from": "2026-09-30T07:00:00Z", "effective_to": "2026-09-30T07:15:00Z",
  "resolution": "minute",
  "data_until": "2026-09-30T07:13:00Z",
  "unit": "events_per_minute",
  "templates": [{
    "service": "vtn_cntt_vas_066",
    "template_id": "vtn_cntt_vas_066:T00431",
    "text": "Timeout acquiring connection <*>",
    "level": "ERROR",
    "doc_id": "DOC-017", "doc_title": "Restart DB pool",
    "peak_state": "ALERTING",
    "periods": [{"start": "2026-09-30T06:58:00Z", "end": null}],
    "alerting_minutes": 15,
    "module":   {"total": 300, "avg": 20.0, "peak": 42,
                 "series": [{"minute": "2026-09-30T07:00:00Z", "count": 18}]},
    "template": {"total": 420, "avg": 28.0, "peak": 55}
  }]
}
```

| Field | Meaning |
|---|---|
| `from`, `to` | as sent, normalized to ISO UTC |
| `effective_from`, `effective_to` | the window actually read (rounded, clamped) |
| `data_until` | end of the last minute the engine has closed and judged (`heartbeat − lag`, floored). Usually 1–2 min behind now. When `effective_to > data_until`, the tail is **not judged yet**: an empty list there does not mean "no alert" |
| `templates[]` | sorted by `module.total` descending, then service, template id |
| `level` | the template's most severe level seen |
| `doc_id`, `doc_title` | the runbook linked to the template (manual, inherited, corpus or LLM-confirmed link); `null` when undocumented |
| `peak_state` | `ALERTING` if the template reached it in the window, else `WARMING` |
| `periods` | each WARMING/ALERTING period overlapping the window, as minute starts: `start` = its first minute, `end` = the first minute after it, `null` = still open. Not clipped to the window. A period that began before `from` may show a start later than its true one (always ≤ `effective_from`) |
| `alerting_minutes` | minutes of the periods inside the window |
| `module` | module M's lines in those minutes: `total`, `avg = total / alerting_minutes` (2 decimals), `peak` (max per minute), `series` (minutes with lines; a missing minute = 0) |
| `template` | the same for the template over all modules (no series) |
| `series` granularity | per minute for a window ≤ 1 day; per hour (`{"hour", "count"}`) above, so a 90-day answer stays small |

## 5. Errors

| Case | Status | Body |
|---|---|---|
| `module_code` missing or empty | 400 | `{"error": "<message>"}` |
| `from` or `to` missing | 400 | same |
| `from` / `to` not ISO-8601 or a number | 400 | same |
| `from > to` | 400 | same |
| token set and header missing or wrong | 401 | `{"error": "unauthorized"}` |
| unknown module (never seen) | 200 | `templates: []` |

## 6. How it is computed

1. Services of the module: `SELECT service FROM modules WHERE module_code = M`.
2. State changes of those services' template keys: the latest one before the window
   (state at its start) plus every one inside it. Each WARMING/ALERTING run becomes a
   period. `alert_changes.at` is the end of a minute, so each bound moves back one minute
   to a minute start, and a period opened by NORMAL → WARMING moves back one more minute
   (the 1st high minute).
3. Counts: `alert_minutes` rows of those keys in the window (the query carries the local
   `day` range, so only those days' partitions are read). Rows of module M give `module`,
   all modules give `template`; minutes outside the periods are dropped.
4. Text and level from `templates`; runbook from `template_doc_links` + `documents`.

## 7. Engine changes (small, needed by the API)

1. **A module per service.** `modules` is keyed by `module_code` alone today, so a module
   seen in a second service would never be recorded for it. Migration `006` keys it by
   `(service, module_code)`, and the owner tracks `(service, module)` pairs. (In
   `udcntt*` the module code carries its service, e.g. `VTN_CNTT_VAS_066_301` under
   `vtn_cntt_vas_066`, so this is a safety net.)
2. **The incident's first minute.** `alert_minutes` is written only for non-NORMAL
   minutes today, so the 1st high minute (NORMAL, streak 1) has no row. The owner also
   writes rows for a template that is NORMAL with `streak > 0`. A one-minute blip writes
   rows too, but no period covers it, so the API never shows it. Rows are only written
   from the deploy on; older incidents start at their WARMING minute.

Detection, bands, alert states and every other page are unchanged.

## 8. Configuration and deploy

- `LOGAI2_API_TOKEN` (env, `web.api_token`): empty = the route is open like the rest of
  the web API; set = `Authorization: Bearer <token>` required on this route only.
  Compose and `k8s/logai-web.yaml` (from Secret `logai-env`) pass it to the web.
- Deploy order: `logai2-engine` first (migration 006, owner), then `logai2-web`. The linker is unchanged.

## 9. Out of scope (add when asked)

- Document and service alerts (`doc:`, `*error`, `*total`): would need `alert_minutes`
  for the templates of an alerting document key and a `documents` list in the response.
- Query by `groupModule` (e.g. `GOSSIP`): not collected today; would need a second
  reader field and column.
- COOLING minutes, a per-second unit, per-caller keys, pagination, a web page.
- The normal rate (`mean ± σ`) per template next to the counts.

## 10. Decisions (2026-10-22, user)

| # | Decision |
|---|---|
| 1 | Counted states: WARMING and ALERTING, from the streak's first high minute; COOLING not |
| 2 | `from` and `to` required; missing → 400 |
| 3 | Millisecond input, 1-minute resolution, window rounded outward |
| 4 | Range from now back to the retention limit; no other cap |
| 5 | Optional static bearer token (`LOGAI2_API_TOKEN`) |
| 6 | Template keys only; document and service alerts later |
