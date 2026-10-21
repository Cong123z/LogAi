# Baseline and Pipeline pages: what they show (sub-project 9, phases 1–2)

Date: 2026-10-21. Status: **approved direction, detail for review**. Not
built. Design: [`per-service-baseline.md` section 9](../design/per-service-baseline.md)
(adopted from Antigravity's
[`AGY-PROP-2026-002`](../design/antigravity-proposal-datadog-visual-watchdog.md)).
Phase 3 (Watchdog tags on Alerting and Templates) is specified in the
design and detailed later.

**User rulings (2026-10-21)**

1. **Apache ECharts draws every chart**, self-hosted (section 2).
2. **The Baseline matrix shows all events (`*total`) by default.**

Model: Datadog Watchdog. An anomaly is a card with a sentence, a graph of
the series over a gray band of expected values, and what caused it, split
into **new** and **increased** log patterns. Pipeline health is shown per
component, averaged over 15 minutes, compared with the same time last week.
Sources: [Watchdog alerts](https://docs.datadoghq.com/watchdog/alerts/),
[Watchdog Insights](https://docs.datadoghq.com/watchdog/insights/),
[anomalies()](https://docs.datadoghq.com/dashboards/functions/algorithms/),
[Observability Pipelines monitoring](https://docs.datadoghq.com/observability_pipelines/monitoring/).

## 1. Questions each page answers

| Page | Question | Answered by |
|---|---|---|
| Baseline | Which hours of the last two weeks were abnormal, and how much? | 3.2 matrix |
| Baseline | For this period: what was expected, what happened, what caused it? | 3.3 inspector |
| Baseline | What is the baseline built from, and where is it thin or idle? | 3.4 coverage |
| Baseline | Exclude or keep, and what does that leave? | 3.3 decision |
| Pipeline | Is every stage healthy? If not, which and why? | 4.2 topology |
| Pipeline | Are we behind, getting better or worse? | 4.3 lag and throughput |
| Pipeline | Is the volume we read normal for this hour, per service? | 4.4 volume |
| Pipeline | Which service is the problem? | 4.5 services table |

## 2. ECharts in this console

- **Vendored**: `logai2/web/static/vendor/echarts.min.js`, the full build
  of one pinned 5.x release (≈1 MB; heatmap, graph, lines, bar, line are
  all used), loaded by `index.html` before the views. No CDN at run time
  (air-gapped deployments). The version and its licence go into README.
- **One helper**, `static/js/charts.js`: `chart(el)` creates an instance
  for an element, remembers it, and `disposeCharts()` disposes all of them
  on every route change (no canvas leaks on a wall monitor). A
  `ResizeObserver` per element calls `resize()`.
- **Theme from tokens**: two themes, `logai-light` and `logai-dark`, built
  at start from `tokens.css` custom properties (text, muted text, grid
  line, surface, the diverging pair, the sequential hue, the band gray,
  status colors) with `echarts.registerTheme`. Changing `data-theme`
  disposes and re-creates the open charts with the other theme.
- **Accessibility**: `aria: {enabled: true}` (generated chart
  descriptions) and `aria.decal` where fill carries meaning; every chart
  has an HTML title and a one-line summary above it; the matrix has a table
  view. Animations are off under `prefers-reduced-motion` (`animation:
  false`, no particle effect).
- **Renderer**: canvas by default; SVG renderer for the small multiples
  (crisper at small sizes, cheap at these point counts).
- **Colors**: diverging pair (warm above, cool below, neutral gray inside
  the band) for the matrix; one sequential hue for coverage; band gray +
  one line color + a dashed muted line for bounds charts; status colors
  (OK / Degraded / Down) only for pipeline states, always with an icon and
  a word. Palettes are checked for color blindness and contrast in both
  themes before building.

## 3. Baseline page

Layout, top to bottom:

```
┌ Service [recharge ▾]  Show [All events ▾]  Days [14 | 28]   [Recompute baseline]  ┐
├ Profile cards: Weekday · Weekend · To review · Last commit                         ┤
├ 3.2 Matrix (days × hours)                       │ To review (list of suggestions)  ┤
├ 3.3 Inspector for the selection (opens under the matrix)                           ┤
├ 3.4 Coverage (weekday / weekend × hours)                                           ┤
└ Decisions (compact, Undo)                                                          ┘
```

### 3.1 Header and cards

- **Service** select (every service seen). **Show** select: **All events**
  (`*total`, default), **Errors** (`*error`), a **document** (the sum of its
  linked templates, as detection judges a doc key), or one **template**
  (search box). **Days**: 14 (default) or 28. The choice is kept in the URL
  hash (`#baseline/recharge/*total/14`) so a link opens the same view.
- **Cards** (kept from today, compacted): Weekday profile and Weekend
  profile ("No days yet: floor-only" / "Young: mean ± 4σ, 3 of 5 days" /
  "Full: mean ± 3σ" / "weekend borrows weekday, upper alerts only"),
  **To review** (open suggestions; click = first one in the inspector),
  **Last commit** (time, owner day, next due). **Recompute baseline** stays
  in the header with today's command polling.

### 3.2 Matrix: days × hours (ECharts heatmap)

- **Axes**: x = hours 00–23 (local), y = days, newest at the top. Y label:
  `Tue 20 Oct`, with **weekend** in muted text and a `●` in front of the
  days the next rebuild uses (last 5 committed weekdays / 4 weekend days).
  Today is the top row; its future hours are empty.
- **Cell value**: the selected key's rate in that hour,
  `rate = hourly_counts.sum / service_hours.n_minutes`, against that day's
  profile for that hour: `z = (rate − mean) / std`.
  - **Fill** (`visualMap` piecewise, diverging, 9 steps): gray for
    `|z| ≤ k` (inside the band; k = 3 full, 4 young), then three warm
    steps above (k..6, 6..10, > 10) and three cool steps below. Below the
    band only colors for `*total` and `*error` (drops); for templates and
    documents it stays gray (they never alert low).
  - **No baseline** for that hour (no committed day, or mean = std = 0):
    a neutral outline cell; with events against a zero baseline, the top
    warm step and "no normal traffic at this hour" in the tooltip.
  - **std = 0, mean > 0**: z from the floor of 1 event/min instead of σ
    (stated in the tooltip).
  - **Empty**: no `service_hours` row (service idle or not read).
- **Marks** in the cell (`label`), never color alone: `▲` part of a
  suggestion (any key of the service was not NORMAL), `✕` excluded (also a
  diagonal `decal` hatch), `✓` kept. Current hour: a thin outline.
- **Tooltip**: `Tue 20 Oct 02:00–03:00 · All events · 1,840/min ·
  expected 600 ± 90 (≤ 870) · z +13.8 · 3 keys not NORMAL for 41 min ·
  not reviewed`.
- **Interaction**: click selects a cell and opens the inspector;
  shift+click extends the selection within the same day (that range is
  what a decision applies to); click on a `▲` cell selects its whole
  suggestion. Keyboard: the chart is focusable, arrows move a highlighted
  cell (`dispatchAction` highlight + tooltip), Enter selects, Shift+arrows
  extend. **Table view** toggle shows the same data as an HTML table.
- **To review list** (right of the matrix, below it on phones): the open
  suggestions as small cards, newest first: `Tue 20 Oct 02:00–04:00 · 3
  keys · peak z +13.8`. Click = select it in the matrix.

### 3.3 Inspector: one Watchdog card for the selection

Opens under the matrix; Esc closes it.

1. **Sentence**: "Tue 20 Oct, 02:00–04:00 · recharge · 3 keys left NORMAL
   for 41 min. All events peaked at 1,840/min against 600 ± 90 expected.
   Not reviewed; this day is in the next baseline."
2. **Expected-bounds chart** (line, the whole day):
   - x = 24 hours of the day; y = events/min (one axis);
   - **gray band** = `max(0, mean − kσ)` .. `mean + kσ` per hour, drawn as
     two stacked line series (an invisible lower bound, the band height
     with `areaStyle`), the hour's band widened with the next hour's as
     detection does;
   - **actual** rate as a solid line, points outside the band in the warm
     color (`visualMap` on the series by a per-point flag);
   - **same day last week** as a dashed muted line (toggle in the legend);
   - **selection** shaded with `markArea`; hours with non-NORMAL minutes
     marked `▲` with `markPoint`;
   - tooltip on the axis: hour, actual, band, last week, minutes not
     NORMAL.
3. **Contributors** (Watchdog's "increased" and "new" patterns), for the
   selected hours:
   - **Increased**: horizontal bar chart of the top 10 templates by excess
     `rate − mean` (events/min above normal), bar label `×4.8 normal`, the
     level as a prefix to the name, the bar in the warm color; a click opens
     the template's drawer on the Templates page. Below it the same 10 as a
     table: template text, level, rate, expected, ×normal, minutes not
     NORMAL, document.
   - **New**: templates first seen in the range or the hour before, as a
     table (text, level, first seen, rate, document or "Not linked"), no
     baseline yet.
   - **Keys that left NORMAL**: chips (`*total`, `*error`, `doc:DOC-001`,
     templates) with their state periods.
4. **Decision**: **Exclude: real incident** and **Keep: normal** for the
   selected hours (`POST /api/baseline/decisions`, unchanged). Before the
   click, a line states the effect: "Excluding leaves 02:00 and 03:00 with
   2 of 5 weekday days (fewer than 3: those hours fall back to floor-only
   alerts)". After it: the matrix marks change, the To review list drops
   the item, the inspector moves to the next suggestion, and "Takes effect
   at the next commit (02:00) or Recompute" stays visible. An existing
   decision shows **Undo** instead.

### 3.4 Coverage: what the baseline is built from (ECharts heatmap)

- Two rows (Weekday, Weekend) × 24 hours.
- **Value**: usable days for that hour = days the rebuild picks minus the
  ones excluded at that hour, out of 5 / 4. Sequential fill, one hue,
  light = few; the number is the cell label. Below 3: a warning outline.
- **Idle**: where the `*total` baseline mean is 0, the label is `idle` and
  the tooltip says "no traffic expected: any ERROR/WARN template reaching
  its floor alerts here".
- Tooltip lists the days used: `Mon 19, Fri 16, Thu 15 (Wed 14 excluded
  02:00–04:00)`.
- Under it, the profile in words, per daytype: "Weekday: full, mean ± 3σ
  from 5 days" / "Weekend: 1 day, borrowing weekday (upper alerts only)".

### 3.5 Removed

"Add a range" (replaced by cell selection), "Days" table (the matrix
rows), the suggestions table (the To review list). Decisions stay as a
compact list with Undo.

### 3.6 API

`POST /api/baseline/decisions`, `DELETE …/<id>`, `POST /api/baseline/recompute`
and `GET /api/baseline` are unchanged. New, read-only:

**`GET /api/baseline/matrix?service&key=*total&days=14`**

```json
{"service": "recharge", "key": "*total", "k": {"weekday": 3.0, "weekend": 4.0},
 "days": [{"day": "2026-10-20", "daytype": "weekday", "in_baseline": true,
           "hours": [{"hour": 2, "rate": 1840.0, "mean": 600.0, "std": 90.0,
                      "band_low": 330.0, "band_high": 870.0, "z": 13.8,
                      "not_normal_minutes": 41, "suggested": true,
                      "decision": null}]}],
 "coverage": {"weekday": [{"hour": 2, "days": 4, "of": 5, "idle": false,
                           "used": ["2026-10-19", "2026-10-16"], "excluded": ["2026-10-14"]}],
              "weekend": []},
 "profiles": {"weekday": "full", "weekend": "borrowed"}}
```

`key` is `*total`, `*error`, `doc:<id>` (summed over linked templates in
SQL) or a template id; `days` 1..28. Hours with no `service_hours` row are
left out.

**`GET /api/baseline/inspect?service&key&day&hour_from&hour_to`**

```json
{"sentence": {"keys": 3, "not_normal_minutes": 41, "peak": {"hour": 2, "rate": 1840.0},
              "expected": {"mean": 600.0, "std": 90.0}, "in_baseline": true, "decision": null},
 "day": [{"hour": 0, "rate": 580.0, "band_low": 310.0, "band_high": 860.0,
          "last_week": 570.0, "not_normal_minutes": 0}],
 "increased": [{"template_id": "recharge:T00032", "text": "...", "level": "ERROR",
                "rate": 175.0, "mean": 36.0, "excess": 139.0, "times_normal": 4.9,
                "not_normal_minutes": 38, "doc_id": "DOC-001", "doc_title": "..."}],
 "new": [{"template_id": "recharge:T00309", "text": "...", "level": "ERROR",
          "first_seen": 1792462440.0, "rate": 12.0, "doc_id": null}],
 "keys": [{"key": "*total", "periods": [{"from": 1792462440.0, "to": 1792464900.0, "state": "ALERTING"}]}],
 "left_days": [{"hour": 2, "days": 2, "of": 5}]}
```

`left_days` is what an exclusion of the range would leave (the rule of
today's `warning`, computed before the click).

## 4. Pipeline page

Layout, top to bottom:

```
┌ Status line: Engine running · heartbeat 3 s ago · warnings                    ┐
├ 4.2 Topology (ECharts graph)                                                  ┤
├ Stage details (opens under the topology for the clicked stage)                ┤
├ 4.3 Lag and throughput, last 60 min        │ Headline tiles                   ┤
├ 4.4 Volume vs expected, per service (small multiples)                         ┤
├ 4.5 Services table                                                            ┤
└ Indices table (kept)                                                          ┘
```

### 4.1 Status line

Kept: running / not running with the last heartbeat / never reported, and
the engine's warnings. A stale engine dims every chart and says the
numbers are the last ones.

### 4.2 Topology (ECharts `graph`, fixed layout)

Nodes at fixed positions in data order; edges with arrows:

```
Elasticsearch ─▶ Reader ─▶ Templates ─▶ Minutes & detection ─▶ Postgres flush ─▶ Prometheus
                                                                   ▲   │
                                                     LLM resolver ◀ Linker (via Postgres)
```

| Node | Headline (big) | Second line | Degraded (amber) | Down (red) |
|---|---|---|---|---|
| Elasticsearch | `2 / 2 indices` | oldest position age | an index catching up > 5 min or failing | ES unreachable, all indices failing |
| Reader | `events/s` (15 min avg) | cache hit 99.6 % | a service's miss ratio warning | no event read for 5 min while ES has newer docs |
| Templates | `316 templates` | `+3 in 1 h · 0 unparsed` | unparsed > 0 in the last hour | — |
| Minutes & detection | `lag 32 s` | detection On · 2 keys not NORMAL | lag > 5 min, detection Off | engine stale |
| Postgres flush | `flush 14 ms` | max 15 min, last commit | flush > 10 s, commit overdue | engine stale (no flush) |
| Prometheus | `:9108` | export on | — | — |
| Linker | `0 pending` | links changed last cycle | last error, pending growing | stale > 90 s |
| LLM resolver | `12 / 50 today` | 2 profiles up, 0 down | a profile down | no active profile, all down |

- Node fill = status color with the status word in the label
  (`OK` / `Degraded` / `Down`); node size fixed; label = name, headline,
  second line.
- Edge width ∝ events/s on the main path; a `lines` series with `effect`
  draws moving particles at a speed ∝ events/s (the proposal's flow), off
  under reduced motion and when the engine is stale.
- Tooltip: the stage's full numbers. **Click** opens the stage details
  under the chart: Elasticsearch → indices table; Reader / Templates →
  services table filtered; Linker / LLM → pending counts, last error, the
  profiles with `down_until`; Postgres → flush times, last and next
  commit.

### 4.3 Lag and throughput, last 60 minutes (ECharts line)

- Two charts side by side (never two y-axes on one): **Lag** (seconds, a
  dashed 5-min threshold line via `markLine`) and **Events/s**, one point
  per minute from the engine's new `history` (section 4.6), so they survive
  reloads and are the same for every viewer. Replaces the browser
  sparkline.
- Tiles beside them: **Lag** now, **Events/s** (15 min avg), **Cache hit
  ratio**, **Late counts** (should stay 0), **Detection**, **Bootstrap**.

### 4.4 Volume vs expected, per service (ECharts line, small multiples)

- One small chart per service (grid of up to 6, sorted by worst deviation
  now; "Show all" for more): x = today's 24 hours, the `*total` band in
  gray, today's hourly rate as a solid line (warm where outside), the same
  hour last week dashed. Title: service and `now 1,840/min · expected ≤ 870
  · ▲ +111 % vs last week`.
- The current hour uses its minutes so far (`service_hours.n_minutes`).
- Click a chart → Baseline page for that service and today.

### 4.5 Services table (HTML)

service · events/min this hour · vs same hour last week (%) · vs expected
(z) · miss ratio (5 min) · templates · new 24 h · unparsed (1 h) · keys not
NORMAL. Sortable; a row click filters the stage details and opens the
service's volume chart.

### 4.6 Engine and API changes

**`worker_status['engine']`** gains (all computed in the engine already or
trivially):

- `flush_seconds` (last), `flush_seconds_max` (15 min),
- `late_counts` (since start), `events_per_second_15m`,
- `history`: one point per minute for the last 60 minutes, `[{t, lag,
  eps}]` (a ring buffer in the engine, 60 × 3 numbers).

**`worker_status['linker']`** gains `llm_today` (answers counted toward
`daily_max`). Profiles up / down are derived on the web from the existing
`llm_profiles` (`down_until`).

**`GET /api/pipeline`** keeps its fields and adds:

```json
{"services": [{"service": "recharge", "rate": 1840.0, "last_week": 870.0, "z": 13.8,
               "miss_ratio": 0.004, "templates": 316, "new_24h": 3, "unparsed_1h": 0,
               "not_normal": 2}],
 "volume": [{"service": "recharge",
             "hours": [{"hour": 0, "rate": 580.0, "band_low": 310.0, "band_high": 860.0,
                        "last_week": 570.0}]}],
 "stages": {"elasticsearch": {"status": "ok", "reason": null}, "...": {}}}
```

`stages` holds the status rule of 4.2 computed on the server, so the
thresholds live in one place. `services` and `volume` come from
`hourly_counts`, `service_hours`, `baseline`, `templates`, `alert_state`.

## 5. Build order

1. Vendor ECharts, `charts.js` (instances, dispose, resize, themes from
   tokens), palette check in both themes.
2. Baseline API (`/matrix`, `/inspect`) with tests on the test Postgres.
3. Baseline page: header, matrix, To review list, inspector, coverage,
   decisions; remove Add a range / Days / suggestions table.
4. Engine and linker status fields (4.6) with tests.
5. Pipeline API (`services`, `volume`, `stages`) with tests.
6. Pipeline page: topology, details, lag/throughput, volume, services
   table; remove the sparkline.
7. WEB_UI.md (both pages, the new endpoints), ARCHITECTURE §10.5 (new
   status fields), README (vendored ECharts and its licence).

Steps 2–3 and 4–6 are independent after step 1. Checked in Chrome on a
scratch copy of the live database: light, dark, 390 px, keyboard only,
reduced motion.

## 6. Out of scope

Phase 3 of sub-project 9 (Watchdog tags and banner on Alerting, `×N
normal` on Templates, exclude from an alert card through 3.3): design
section 9, detailed later. Also out: minute-level storage of NORMAL
minutes, notifications.

## 7. Open questions

1. Threshold for a Degraded flush (10 s proposed) and for Reader Down (no
   event for 5 min while ES has newer docs)?
2. Small multiples: cap at 6 services, sorted by deviation?
3. Matrix z steps (k, 6, 10) fine, or tune on the live data first?
