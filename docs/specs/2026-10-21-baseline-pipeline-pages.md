# Baseline and Pipeline pages: show the data, Watchdog style

Date: 2026-10-21. Status: **proposed**, waiting for the user's rulings
(section 9). Not built.

Inputs: Antigravity's proposal
[`AGY-PROP-2026-002`](../design/antigravity-proposal-datadog-visual-watchdog.md)
(its §5.1 and §5.2, and its "Review against the code" section), Datadog's
public docs on Watchdog and Observability Pipelines (section 2), and the
code on `master` (`25302bd`). Where this spec and the proposal differ, this
spec follows the code.

## 1. Problem

Both pages show **state** but not **data**.

**Baseline** (`views/baseline.js`, WEB_UI "Baseline") is four tables:
suggestions, add a range, decisions and days. An operator asked "exclude
02:00–04:00 on Tuesday?" sees only the keys that left NORMAL. The page does
not show how far the hour was from normal, whether the rest of the day was
normal, which days the baseline is built from, or which hours have too few
days left. The data to show all of this is already stored (section 3).

**Pipeline** (`views/pipeline.js`) is cards plus a sparkline of the last 60
heartbeats that lives only in the open tab. It cannot say whether today's
log volume is normal, which stage is the slow one, or what happened before
the page was opened.

Questions each page must answer at a glance:

| Page | Question |
|---|---|
| Baseline | Which hours of the last two weeks were abnormal, and by how much? |
| Baseline | For this hour: what was expected, what happened, which templates caused it? |
| Baseline | Which days and hours is the baseline built from, and where is it thin or idle? |
| Baseline | Exclude or keep this period, and what does that leave? |
| Pipeline | Is every stage healthy? If not, which one and why? |
| Pipeline | Is the volume we read normal for this hour, per service? |
| Pipeline | Are we behind, and is it getting better or worse? |

## 2. What Datadog does, and what we take

Watchdog for logs (sources: [Watchdog alerts](https://docs.datadoghq.com/watchdog/alerts/),
[Watchdog Insights](https://docs.datadoghq.com/watchdog/insights/),
[anomalies function](https://docs.datadoghq.com/dashboards/functions/algorithms/),
[Observability Pipelines monitoring](https://docs.datadoghq.com/observability_pipelines/monitoring/)):

| Datadog | How it presents | Take for logai2 |
|---|---|---|
| Watchdog alert card | status (ongoing / resolved / expired), start → end, one-sentence message, a graph, scope tags, impact | **Yes**: the inspector (4.2) is one such card per period: a sentence, the graph, the keys |
| "Show expected bounds" | the series drawn over a gray band of expected values; outside the band = anomalous color | **Yes**: the band is our `mean ± kσ` per hour, drawn exactly as detection judges it |
| `anomalies()` gray band | width set by `bounds` (2–3 typical) | Same idea; our k is 3 (full) / 4 (young) |
| Log anomaly types | "new log patterns" and "increases in existing log patterns" | **Yes**: the contributors list (4.2) splits **new** templates from **increased** ones |
| Severe log anomaly | error logs, ≥ 10 min, significant increase, low noise | Already ours: floors + 3-minute hysteresis. No new rule |
| Insights carousel | highest priority first, "View all" side panel, "filter on insight" | Partly: suggestions are already ordered; the matrix replaces the list |
| Error outlier bar | a field's share of errors vs its share of all logs | Not now (we have no tag facets beyond `module`) |
| OP pipelines list | per pipeline: status, events/s, bytes/s averaged over **15 min** | **Yes**: stage headline numbers averaged, not one flush |
| OP Health tab | per component: events in/out, errors, discarded, utilization, buffer, **week-over-week** change | **Yes**: per-stage in/out where we have it; **this hour vs the same hour last week** for volume |

Not taken from the proposal: the animated particle DAG (decoration; the
stage order in its diagram is also wrong, see its review §5), the
one-click exclude without seeing the effect (section 4.4 keeps the
few-days warning, shown before the click), and Apache ECharts (section 6).

## 3. Data we already have

No migration is needed for the Baseline page. Per service:

| Need | Source | Granularity |
|---|---|---|
| actual rate of a key in an hour | `hourly_counts.sum / service_hours.n_minutes` | key × day × hour, 90 days |
| spread inside that hour | `sqrt(sumsq/n − (sum/n)²)` | same |
| expected band | `baseline (mean, std, days)` per daytype × hour, k from `full_after` | key × daytype × hour |
| which days the rebuild uses | last 5 committed weekdays / 4 weekend days (`jobs/commit.py` `REBUILD`) | day |
| non-NORMAL periods | `alert_changes` (what `review.suggest` already reads) | key, to the minute |
| non-NORMAL minute counts | `alert_minutes` (only minutes a key was not NORMAL) | key × module × minute |
| new templates | `templates.first_seen` | template |
| decisions | `review_exclusions` | day × hour range |

Keys in `hourly_counts`: every template, `*total`, `*error`. **Doc keys are
not stored**; a document's line is the sum of its linked templates (the
same sum detection uses, ARCHITECTURE §14). Minute-by-minute counts of
NORMAL minutes are **not** stored, so charts are per hour, with
non-NORMAL minutes as markers. That is enough to judge an hour, which is
the unit an exclusion acts on.

Pipeline gaps (small engine change, section 5.4): `worker_status` has no
flush duration (only the Prometheus gauge), no late-count total, and no
events per service. Volume history per service comes from `hourly_counts`
`*total`, which survives reloads.

## 4. Baseline page

Layout, top to bottom: service select and key select (4.1), profile cards
(kept as today), the **matrix** (4.1), the **inspector** for the selected
cell (4.2), **coverage** (4.3), decisions with Undo (kept, compact), and
Recompute (kept). The "Add a range" form and the "Days" table are removed:
the matrix shows the days and selecting cells adds a range.

### 4.1 Matrix: days × hours

A grid, one row per local day (newest at the top, the last **14** days by
default, up to 28), 24 columns of hours. Row label: date, weekday, and an
**in baseline** mark on the days the next rebuild will use.

- **Key**: `*total` by default; `*error`, a document (sum of its linked
  templates) or one template can be picked. The cell is that key's hour.
- **Cell fill: diverging**, the hour's actual rate against its band, in σ:
  `z = (rate − mean) / std` of that day's daytype profile, clipped at ±4.
  Neutral gray inside ±k (inside the band), one warm hue above, one cool
  hue below, darker as |z| grows. Above the band is the alerting side for
  every key; below matters for `*total` (drops). Hours with `mean = std =
  0` and no events are blank; with events against a zero baseline they are
  the darkest warm step and get a "no baseline" tooltip.
- **Glyphs** (never color alone): `▲` an hour in a suggestion (any key of
  the service not NORMAL), `✕` excluded, `✓` kept. An excluded cell is also
  hatched.
- **Empty cell** (no `service_hours` row): the service was idle or not read.
- **Hover**: date and hour, actual rate, expected `mean ± kσ`, z, minutes
  not NORMAL, decision. **Click** opens the inspector; **shift+click**
  extends a selection within one day (the range for a decision). Arrow
  keys move the focus; Enter opens.
- A table view of the same data is one toggle away (accessibility; also
  what `prefers-reduced-motion` and screen readers get).

### 4.2 Inspector: one Watchdog-style card per selection

Opens under the matrix for the selected cell or range (a suggestion
selects its whole range).

1. **Sentence**: "Tue 20 Oct, 02:00–04:00 · recharge · 3 keys left NORMAL.
   `*total` 1,840/min, expected 600 ± 90 (z +13.8). Not in baseline yet."
2. **Expected-bounds chart** for that whole day: x = 24 hours, a gray band
   per hour (`mean − kσ` .. `mean + kσ` of the daytype profile; the next
   hour's band is joined as detection does), the day's actual rate as a
   line, the selected hours shaded, `▲` markers on hours with non-NORMAL
   minutes. One axis, one series plus the band; a crosshair tooltip per
   hour. The same day last week is an optional dashed gray line
   (week-over-week, as Datadog's Health tab).
3. **Contributors** for the selected hours, two groups as Watchdog splits
   log anomalies:
   - **Increased**: templates with the largest excess `rate − mean` over
     the selected hours, top 10, with level, rate, expected, ×-normal,
     minutes not NORMAL and linked document;
   - **New**: templates first seen in or just before the range (no
     baseline yet).
   Each row opens the template on the Templates page.
4. **Decision**: **Exclude: real incident** and **Keep: normal** for the
   selected hours. Before the click the panel already says what an
   exclusion leaves: "02:00 and 03:00 keep 2 of 5 weekday days" (the same
   rule as today's `warning`, computed up front). After the click the
   matrix cells change glyph; the decision takes effect at the next commit
   or Recompute (said in the panel).

### 4.3 Coverage: what the baseline is built from

Two rows (weekday, weekend) × 24 hours, small cells:

- number in the cell: usable days for that hour (committed days the rebuild
  picks, minus excluded hours), out of 5 / 4; fill sequential, one hue,
  light = few;
- **idle** mark where the `*total` baseline mean is 0: the known case
  (ARCHITECTURE §14) where every ERROR/WARN template at its floor alerts.
  The tooltip says so;
- the profile state as today: none / young (k = 4) / full (k = 3), and
  "weekend borrows weekday, upper alerts only" when it applies.

### 4.4 API

All read-only additions; `POST /api/baseline/decisions` is unchanged.

- `GET /api/baseline/matrix?service&key=*total&days=14` →
  `{"key", "k": {"weekday", "weekend"}, "days": [{"day", "daytype",
  "in_baseline", "hours": [{"hour", "rate", "sd", "mean", "std", "z",
  "alert_minutes", "decision"}]}], "coverage": {"weekday": [{"hour",
  "days", "idle"}], "weekend": [...]}}`. A doc key sums its linked
  templates in SQL. `days` capped at 28.
- `GET /api/baseline/hours?service&day&hour_from&hour_to` → the inspector:
  `{"sentence_facts", "day_series": [{"hour", "rate", "band_low",
  "band_high", "last_week"}], "increased": [...], "new": [...],
  "left_days": [{"hour", "days", "of"}]}`.
- `GET /api/baseline` keeps its shape (suggestions, decisions, last commit).

Cost: one service × 28 days × 24 hours over `hourly_counts` joined to
`baseline`, using the primary keys; templates for "increased" limited to
the selected hours. No pagination needed.

## 5. Pipeline page

### 5.1 Stage strip

A row of stage cards in data order (wraps to a column at phone width),
plain HTML/CSS with arrows between them, no graph library:

```
Elasticsearch → Reader → Templates → Minutes & detection → Postgres flush
                                                              ↑
                                         Linker → LLM resolver (via Postgres)
```

| Stage | Headline | Second line | Degraded when |
|---|---|---|---|
| Elasticsearch | indices read / selected | oldest position, failing count | an index failing, ES unreachable |
| Reader | events/s (15 min average) | cache hit ratio, worst service miss ratio | miss ratio warning |
| Templates | templates total | new in the last hour / 24 h, `unparsed` count | new templates/h far above usual, any unparsed |
| Minutes & detection | lag | detection On/Off, keys not NORMAL | lag > 5 min, detection off |
| Postgres flush | flush time (last, max of 15 min) | last commit, next commit due | flush > 10 s, commit overdue |
| Linker | pending vectors | links changed last cycle | stale (90 s), last error |
| LLM resolver | answers today / `daily_max` | profiles up / down | no active profile, all profiles down |

Each card has a state with icon and word (OK / Degraded / Down) and the
reason in words. A click opens its details below the strip (the indices
table for Elasticsearch, the per-service table for Reader and Templates,
the profiles for LLM).

### 5.2 Volume: is what we read normal?

Per service, an **expected-bounds chart** for today: hourly `*total` rate
(line) against the baseline band (gray), with the same hour last week as a
dashed line. One small chart per service (small multiples) rather than many
lines on one chart; at most 6, then a "show all". This is the pipeline's
own watchdog: a reader stall or a service that stopped logging shows as a
line under the band before anyone opens Alerting.

### 5.3 Per-service table

service · events last hour · vs same hour last week (%) · miss ratio (5 min)
· templates · new 24 h · unparsed last hour · keys not NORMAL.

### 5.4 Engine and API changes

- `worker_status['engine']` gains `flush_seconds` (last), `flush_seconds_max`
  (15 min), `late_counts` (since start) and `events_per_second_15m`. All
  are already computed for Prometheus or trivially derived.
- `GET /api/pipeline` gains `services` (5.3, from `hourly_counts`,
  `templates`, `alert_state`) and `volume` (5.2: per service, 24 hourly
  points of rate, band, last week). LLM budget comes from the same counts
  the resolver uses (`template_doc_links` by `linked_at`).
- The browser sparkline is removed; history comes from the database.

## 6. Rendering

Inline SVG and CSS grid, drawn by hand in the existing vanilla JS, colors
from `tokens.css`, light and dark. Two chart forms only (a heatmap grid and
a line over a band), plus the existing badges. No chart library:

- the proposal's ECharts is about 1 MB minified for the full build, for
  two simple forms; the page already draws its sparkline in SVG;
- a grid of `<button>` cells is keyboard- and screen-reader-friendly for
  free, which a canvas heatmap is not.

Colors: diverging pair (warm above, cool below, gray midpoint) for the
matrix, one sequential hue for coverage, the gray band and one line color
for the bounds charts, status colors only for stage state. Palettes are
validated (CVD, contrast) for both themes before building, with
`scripts/validate_palette.js` from the dataviz guidance.

## 7. Build order

1. Baseline API (`/matrix`, `/hours`) with tests on the test Postgres.
2. Baseline matrix + inspector + coverage; remove Add a range and Days.
3. Engine `worker_status` fields; Pipeline API `services` and `volume`.
4. Pipeline stage strip, volume charts, per-service table; remove the
   sparkline.
5. WEB_UI.md and ARCHITECTURE (§10.5 fields) with the code.

Each step is a branch-sized task; 1–2 and 3–4 are independent.

## 8. Out of scope

Alerting-page tags and banners (proposal §5.3 Pillar 1), the delta column
on Templates (Pillar 2, against the Templates ruling), one-click exclude
from Alerting cards (Pillar 3; could reuse 4.2 later), minute-level storage
of NORMAL minutes, notifications.

## 9. Questions for the user

1. **Matrix key default**: `*total` (volume, shows drops) or `*error`
   (errors, closer to what alerts)?
2. **Days shown**: 14 by default, 28 max?
3. **Remove "Add a range" and "Days"** in favor of cell selection and the
   matrix, or keep them collapsed?
4. **Week-over-week line** on both charts, or Baseline only?
5. **No chart library** (hand SVG), declining the proposal's ECharts?
6. **Pipeline volume charts**: one per service (cap 6), or only the
   selected service?
