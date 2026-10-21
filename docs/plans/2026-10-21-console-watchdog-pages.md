# Baseline, Review and Pipeline Pages, Watchdog Style — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: **built** (branch `console-watchdog`).

**Goal:** A chart-only Baseline page (a service or one template against its baseline, with the period's possible incidents), a new Review page for every exclude/keep decision, and a rebuilt Pipeline page (sub-project 9, phases 1–2), all drawn with Apache ECharts.

**Architecture:** Two new read-only view modules compute everything from tables that exist (`hourly_counts`, `service_hours`, `baseline`, `day_status`, `review_exclusions`, `alert_changes`, `templates`, `worker_status`), reusing `Baseline.lookup` / `judge` so every band is the one detection uses. Flask routes in `app.py` stay thin. The engine and the resolver add a few status fields. The front end gets a vendored ECharts, one helper (`charts.js`), a new `review.js` (the decision page), and rewritten `baseline.js` / `pipeline.js`.

**Tech Stack:** Python 3, Flask, psycopg 3, pytest against the test Postgres on 127.0.0.1:5433; vanilla JS, Apache ECharts 5.5.1 (vendored, Apache-2.0).

**Spec:** `docs/specs/2026-10-21-baseline-pipeline-pages.md` (design: `docs/design/per-service-baseline.md` section 9). Mockup for look and behavior: https://claude.ai/artifact/TStddKaZPCS7YEFWB2HZep (sample data).

## Global Constraints

- New branch from `master`: `console-watchdog`. Never push. Stage by explicit path. No AI attribution in commits.
- Tests: `.venv/bin/pytest -q` (`docker compose up -d postgres`). After code changes: `graphify update .`.
- No migration, no new Python dependency. ECharts `5.5.1` full build, vendored at `logai2/web/static/vendor/echarts.min.js`; no CDN at run time.
- Matrix default key `*total`; days 14 (default) or up to 28; `days` outside 1..28 → 400. Baseline series: `days` ∈ {1, 7, 14} (default 14), else 400 `"days must be 1, 7 or 14"`.
- Routes: `#baseline/<service>/<key>/<days>` (charts, read-only), `#review/<service>/<key>/<days>` (decisions). Documents and Templates pages are not changed.
- Possible incident = consecutive hours outside the `judge` band (below only for `*total` / `*error` when the profile alerts low); `ongoing` when it contains the newest hour with data. Sample lines only for ongoing incidents (`error_samples` keeps the newest 20 per ERROR+ template); never invent or keep older ones.
- z = `(rate − mean) / σ`, σ floored at `1.0` event/min when mean > 0; no baseline (no row, or mean = σ = 0) → `z: null, no_baseline: true`.
- Colour steps on z: inside `|z| ≤ k` (k = 3 full, 4 young) gray; above: k–6, 6–10, > 10; below the same, only for `*total` and `*error`.
- Thresholds: lag Degraded `> 300 s`; flush Degraded `flush_seconds_max > 10 s`; commit overdue when `last_commit.at` older than `26 h`; Reader Down when the last 5 history points have `eps == 0` while some index is not caught up; linker stale `90 s` (existing `LINKER_STALE_SECONDS`); engine stale `30 s` (existing).
- Contributors: top `10` increased templates; new templates = first seen from 1 h before the range start to its end.
- Pipeline small multiples: at most `6`, worst |z| first, then "Show all".
- Every chart: themes from `tokens.css`, `aria.enabled: true`, `animation: false` and no particle effect under `prefers-reduced-motion`; instances disposed on every view change and theme change; Pipeline's 2 s poll updates existing instances with `setOption` (never re-creates them).
- Copy as in the spec sections 3, 3B and 4 (e.g. "Exclude: real incident", "Keep: normal", "Takes effect at the next commit (02:00) or Recompute").

## Review Focus

1. **A service with no committed day** (fresh install): matrix returns rates with `z: null, no_baseline: true`, coverage `days: 0`, profiles `none`, no crash. → Task 2 `test_matrix_without_baseline`.
2. **Weekend with fewer than 2 committed weekend days** borrows the weekday profile (upper alerts only): weekend z uses weekday mean/σ and below-band is not coloured. → Task 2 `test_weekend_borrows_weekday_and_no_drop_colour`.
3. **σ = 0 with mean > 0** (a perfectly flat key): z uses the 1.0 floor, never divides by zero. → Task 2 `test_flat_key_uses_sigma_floor`.
4. **Today's partial and future hours**: the current hour's rate uses its minutes so far; hours with no `service_hours` row are left out, not shown as 0. → Task 2 `test_partial_and_missing_hours`.
5. **Bad keys**: unknown template, a template of another service, a doc with no linked template → 404 `"Unknown key"`, not an empty matrix. → Task 2 `test_unknown_keys_404`.

---

### Task 1: Non-NORMAL minutes per hour (shared helper)

**Files:**
- Modify: `logai2/review.py` (extract the period walk from `suggest`)
- Test: `tests/test_review.py`

**Interfaces:**
- Produces:
  - `periods(changes: list[dict], end_now: float) -> list[tuple[str, float, float]]` — `(key, start_s, end_s)` per non-NORMAL period; open periods end at `end_now`. `suggest` calls it (behaviour unchanged).
  - `minutes_by_hour(periods) -> dict[tuple[date, int], int]` — per local `(day, hour)`, minutes covered by **any** key (union of periods, not a sum).
  - `keys_by_hour(periods) -> dict[tuple[date, int], set[str]]` — keys not NORMAL in that hour.

- [ ] **Step 1: Write the failing tests**

```python
def test_minutes_by_hour_is_a_union_across_keys():
    t0 = datetime(2026, 10, 20, 2, 10, tzinfo=TZ).timestamp()
    ps = [("*total", t0, t0 + 30 * 60), ("k1", t0 + 20 * 60, t0 + 70 * 60)]
    m = minutes_by_hour(ps)
    assert m[(date(2026, 10, 20), 2)] == 50 and m[(date(2026, 10, 20), 3)] == 20
    assert keys_by_hour(ps)[(date(2026, 10, 20), 3)] == {"k1"}

def test_periods_close_open_ones_at_now():
    at = datetime(2026, 10, 20, 2, 0, tzinfo=TZ)
    changes = [{"service": "s", "key": "k", "at": at, "from_state": "NORMAL", "to_state": "WARMING"}]
    assert periods(changes, at.timestamp() + 600) == [("k", at.timestamp(), at.timestamp() + 600)]
```

- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_review.py` — Expected: FAIL (`ImportError: cannot import name 'minutes_by_hour'`).
- [ ] **Step 3: Implement** the three functions in `review.py`; `suggest` uses `periods()`. Keep the existing `ponytail:` comment on epoch-hour stepping where the hour walk lives.
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_review.py tests/test_web_docs_baseline.py` — Expected: all pass (suggestions unchanged).
- [ ] **Step 5: Commit** `git add logai2/review.py tests/test_review.py && git commit -m "review: periods, minutes and keys per local hour"`

---

### Task 2: Review matrix and coverage API (`/api/baseline/matrix`)

**Files:**
- Create: `logai2/web/baseline_view.py`
- Modify: `logai2/web/app.py` (route; `add_decision` uses `left_days`)
- Test: `tests/test_web_baseline_view.py`

**Interfaces:**
- Consumes: `review.periods`, `review.minutes_by_hour`, `review.suggest`; `owner.baseline.Baseline.load / lookup / lookup_doc`, `judge`, `full_after`, `DAY_CAP`; `links.ALERTING_SOURCES`; `timeutil.daytype`.
- Produces (all in `baseline_view.py`; `conn` is a psycopg dict-row connection, `cfg` an `AppConfig`):
  - `class UnknownKey(Exception)`.
  - `resolve_key(conn, service: str, key: str) -> tuple[str, list[str]]` — returns `(kind, template_ids)`: `("total", ["*total"])`, `("error", ["*error"])`, `("doc", [linked template ids, alerting sources only])`, `("template", [key])`; raises `UnknownKey` for an unknown template, another service's template, or a doc with no linked template.
  - `picked_days(conn, service: str) -> dict[str, list[date]]` — per daytype the committed days the rebuild uses (newest first, `DAY_CAP`), same rule as `jobs/commit.py` `REBUILD`.
  - `left_days(conn, service: str, day: date, hour_from: int, hour_to: int, extra_exclude: bool) -> list[dict]` — `[{"hour", "days", "of"}]` usable days per hour of that day's daytype, counting an exclusion of the range when `extra_exclude`.
  - `matrix(conn, cfg, service: str, key: str, ndays: int, today: date, now: float) -> dict` — the JSON of spec 3.6 `/matrix`: `service, key, k {weekday, weekend}, days [{day, daytype, in_baseline, hours [{hour, rate, mean, std, band_low, band_high, z, no_baseline, cls, not_normal_minutes, suggested, decision}]}], coverage {weekday|weekend: [{hour, days, of, idle, used, excluded}]}, profiles {weekday, weekend}`.
    - `cls` ∈ `"in" | "up1" | "up2" | "up3" | "down1" | "down2" | "down3" | "none"` from the Global Constraints steps (`"none"` = no baseline; down classes only for `*total` / `*error` and never when `upper_only`). The UI maps `cls` to colours; it never recomputes z.
    - `profiles` values: `"none" | "young" | "full" | "borrowed"` (weekend with < 2 days).
    - `idle`: the `*total` baseline row for that daytype/hour exists with mean 0.
- Route: `GET /api/baseline/matrix?service&key=*total&days=14` → `matrix(...)` with `today, now` from the clock; `known_service` 404; `days` not an int in 1..28 → 400 `"days must be an integer from 1 to 28"`; `UnknownKey` → 404 `"Unknown key"`.
- `add_decision`'s `warning` = hours of `left_days(..., extra_exclude=True)` with `days < 3` (replaces its inline SQL; existing tests must still pass).

- [ ] **Step 1: Write the failing tests** (seed with helpers in the test file: `service(conn, name)`, `hours(conn, service, key, day, {hour: (sum, sumsq, n_minutes)})`, `base(conn, service, key, daytype, hour, mean, std)`, `committed(conn, service, days)`; call `matrix()` directly with `today=date(2026, 10, 21)`).

```python
def test_matrix_z_class_and_marks(conn, cfg):
    # 5 committed weekdays → full (k 3); Tue 20 Oct 02:00 at 1840/min vs 540 ± 57
    ...
    m = matrix(conn, cfg, "pay", "*total", 14, date(2026, 10, 21), NOW)
    h2 = cell(m, "2026-10-20", 2)
    assert (round(h2["rate"]), h2["mean"], h2["std"], round(h2["z"], 1), h2["cls"]) == (1840, 540.0, 57.0, 22.8, "up3")
    assert h2["suggested"] and h2["not_normal_minutes"] == 41 and h2["decision"] is None
    assert cell(m, "2026-10-16", 10)["decision"] == "exclude"
    assert m["k"] == {"weekday": 3.0, "weekend": 4.0} and m["profiles"]["weekday"] == "full"
    assert [d["day"] for d in m["days"]][:2] == ["2026-10-21", "2026-10-20"] and m["days"][1]["in_baseline"]

def test_matrix_without_baseline(conn, cfg):
    h = cell(matrix(conn, cfg, "new", "*total", 14, TODAY, NOW), "2026-10-20", 9)
    assert h["z"] is None and h["no_baseline"] and h["cls"] == "none"
    assert all(c["days"] == 0 for c in matrix(conn, cfg, "new", "*total", 14, TODAY, NOW)["coverage"]["weekday"])

def test_weekend_borrows_weekday_and_no_drop_colour(conn, cfg):
    # 1 committed weekend day, weekday profile mean 100 σ 10; Sat 17 Oct 10:00 at 20/min
    h = cell(matrix(conn, cfg, "pay", "*total", 14, TODAY, NOW), "2026-10-17", 10)
    assert h["mean"] == 100.0 and h["z"] == -8.0 and h["cls"] == "in"
    assert matrix(conn, cfg, "pay", "*total", 14, TODAY, NOW)["profiles"]["weekend"] == "borrowed"

def test_flat_key_uses_sigma_floor(conn, cfg):
    # mean 5, σ 0, rate 9 → z = 4.0 (σ floored at 1)
    assert cell(matrix(conn, cfg, "pay", "*error", 14, TODAY, NOW), "2026-10-20", 4)["z"] == 4.0

def test_partial_and_missing_hours(conn, cfg):
    # today 14:00 has 20 minutes, sum 600 → 30/min; 15:00 has no service_hours row
    today = {d["day"]: d for d in matrix(conn, cfg, "pay", "*total", 14, TODAY, NOW)["days"]}["2026-10-21"]
    assert {h["hour"]: h["rate"] for h in today["hours"]}.get(14) == 30.0 and 15 not in [h["hour"] for h in today["hours"]]

def test_doc_key_sums_linked_templates(conn, cfg):
    # T1 sum 120 and T2 sum 60 over 60 minutes, baselines 1±0.5 and 0.5±0.5 → rate 3, mean 1.5, std 1.0 (Σσ)
    h = cell(matrix(conn, cfg, "pay", "doc:DOC-001", 14, TODAY, NOW), "2026-10-20", 2)
    assert (h["rate"], h["mean"], h["std"]) == (3.0, 1.5, 1.0)

def test_unknown_keys_404(client):
    for key in ("pay:T09999", "other:T00001", "doc:DOC-404", "doc:DOC-EMPTY"):
        assert client.get(f"/api/baseline/matrix?service=pay&key={key}").status_code == 404
    assert client.get("/api/baseline/matrix?service=pay&days=29").status_code == 400

def test_coverage_counts_used_days_minus_exclusions_and_idle(conn, cfg):
    cov = matrix(conn, cfg, "pay", "*total", 14, TODAY, NOW)["coverage"]
    assert cov["weekday"][2] == {"hour": 2, "days": 3, "of": 5, "idle": False,
                                 "used": [...], "excluded": ["2026-10-19", "2026-10-15"]}
    assert cov["weekend"][3]["idle"] is True

def test_left_days_before_the_click(conn, cfg):
    assert left_days(conn, "pay", date(2026, 10, 20), 2, 4, True) == [
        {"hour": 2, "days": 2, "of": 5}, {"hour": 3, "days": 4, "of": 5}]
```

(`...` = the seeding the comment states; write it with the helpers, plus `cell(m, day_iso, hour)` that finds one hour in the result. Weekday `*total` baseline: hour 2 = 540 ± 57, hour 3 = 520 ± 50 (so the widened band at 2 stays 369–711). `used` lists the picked weekdays not excluded at hour 2, newest first.)

- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_web_baseline_view.py` — Expected: FAIL (`ModuleNotFoundError: logai2.web.baseline_view`).
- [ ] **Step 3: Implement** `baseline_view.py`. Approach: one query for `service_hours` over the days, one for `hourly_counts` `WHERE service = %s AND key = ANY(%s) AND day >= %s GROUP BY day, hour` (sums the doc's templates), `Baseline.load(conn, [(service, t) for t in template_ids])`, then per cell `lookup` / `lookup_doc` for mean/std/k/upper_only and `judge(0, key, level, lk, cfg)` for `band_low/high` (level `"ERROR"` for `*error`, the template's level, `"INFO"` otherwise). Suggestions and minutes from `alert_changes` via Task 1. Add the route and switch `add_decision` to `left_days`.
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_web_baseline_view.py tests/test_web_docs_baseline.py` — Expected: all pass.
- [ ] **Step 5: Commit** `git add logai2/web/baseline_view.py logai2/web/app.py tests/test_web_baseline_view.py && git commit -m "Baseline matrix and coverage API"`

---

### Task 3: Review inspector API (`/api/baseline/inspect`)

**Files:**
- Modify: `logai2/web/baseline_view.py`, `logai2/web/app.py`
- Test: `tests/test_web_baseline_view.py`

**Interfaces:**
- Consumes: Task 2 `resolve_key`, `left_days`, its cell computation (factor a `_cells(conn, cfg, service, template_ids, kind, days)` helper in Task 2 if not done; both use it).
- Produces: `inspect(conn, cfg, service: str, key: str, day: date, hour_from: int, hour_to: int, now: float) -> dict` = spec 3.6 `/inspect`: `sentence {keys, not_normal_minutes, peak {hour, rate, z}, expected {mean, std}, in_baseline, decision}`, `day [24 × {hour, rate|null, band_low, band_high, last_week|null, not_normal_minutes}]`, `increased [≤10 × {template_id, text, level, rate, mean, excess, times_normal|null, not_normal_minutes, doc_id, doc_title}]` (excess > 0 only, largest first), `new [{template_id, text, level, first_seen, rate, doc_id}]`, `keys [{key, periods [{from, to, state}]}]`, `left_days`.
- Route: `GET /api/baseline/inspect?service&key&day&hour_from&hour_to` — same validation as `POST /api/baseline/decisions` for day and hours (same 400 messages), 404 for service/key.

- [ ] **Step 1: Write the failing tests**

```python
def test_inspect_day_series_band_and_last_week(conn, cfg):
    # Task 2's seeding; Tue 13 Oct 02:00 at 530/min (last week)
    r = inspect(conn, cfg, "pay", "*total", date(2026, 10, 20), 2, 4, NOW)
    h2 = r["day"][2]
    assert (round(h2["rate"]), h2["band_low"], h2["band_high"], round(h2["last_week"])) == (1840, 369.0, 711.0, 530)
    assert r["day"][5]["rate"] is None            # no service_hours row
    assert r["sentence"]["peak"]["hour"] == 2 and r["sentence"]["in_baseline"] is True

def test_inspect_contributors_increased_and_new(conn, cfg):
    r = inspect(conn, cfg, "pay", "*total", date(2026, 10, 20), 2, 4, NOW)
    assert [i["template_id"] for i in r["increased"]][:2] == ["pay:T00032", "pay:T00141"]
    top = r["increased"][0]
    assert (top["rate"], top["mean"], top["excess"], top["times_normal"]) == (175.0, 36.0, 139.0, 4.9)
    assert all(i["excess"] > 0 for i in r["increased"]) and len(r["increased"]) <= 10
    assert [n["template_id"] for n in r["new"]] == ["pay:T00309"]   # first seen 01:30; T00001 (19 Oct) is not new
    assert r["left_days"] == [{"hour": 2, "days": 2, "of": 5}, {"hour": 3, "days": 4, "of": 5}]

def test_inspect_validation(client):
    assert client.get("/api/baseline/inspect?service=pay&key=*total&day=2026-10-20&hour_from=4&hour_to=4").status_code == 400
    assert client.get("/api/baseline/inspect?service=pay&key=*total&day=nope&hour_from=1&hour_to=2").status_code == 400
```

- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_web_baseline_view.py -k inspect` — Expected: FAIL (`ImportError: inspect`).
- [ ] **Step 3: Implement** `inspect` and the route. `times_normal` = `round(rate / mean, 1)` when mean > 0 else null. Increased = templates with `hourly_counts` in the selected hours; rate = Σsum / Σn_minutes over the range; mean = average `lookup().this.mean` over the range (0 when none).
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_web_baseline_view.py` — Expected: all pass.
- [ ] **Step 5: Commit** `git add logai2/web/baseline_view.py logai2/web/app.py tests/test_web_baseline_view.py && git commit -m "Baseline inspector API"`

---

### Task 4: ECharts, chart helper, chart colour tokens

**Files:**
- Create: `logai2/web/static/vendor/echarts.min.js`, `logai2/web/static/vendor/ECHARTS-LICENSE`, `logai2/web/static/js/charts.js`
- Modify: `logai2/web/static/index.html` (script tags), `logai2/web/static/css/tokens.css` (chart tokens in all three blocks), `logai2/web/static/js/main.js` (dispose on view/theme change), `README.md` (vendored ECharts + licence)
- Test: `tests/test_web_alerts_templates.py` (static asset served)

**Interfaces:**
- Produces (globals in `charts.js`, loaded `defer` after `ui.js`, before the views):
  - `chartFor(el, renderer = 'canvas') -> echarts.ECharts` — returns the existing instance for `el` or creates one; registers a `ResizeObserver`.
  - `disposeCharts() -> void` — disposes every instance.
  - `chartTokens() -> object` — `{text, dim, muted, grid, border, card, accent, band, line, lastweek, cls: {in, up1, up2, up3, down1, down2, down3, none}, seq: [s1, s2, s3, s4], status: {ok, warn, bad}}` read from CSS custom properties.
  - `baseOption() -> object` — `{animation: !reduced, aria: {enabled: true}, textStyle, tooltip}` with tooltip colours from tokens.
  - `REDUCED_MOTION: boolean`.
- Tokens (add to the dark `:root` and both light blocks of `tokens.css`; values from the mockup):

| Token | Dark | Light |
|---|---|---|
| `--chart-down3/2/1` | `#93c5fd` `#3b82f6` `#1e3a8a` | `#1d4ed8` `#60a5fa` `#bfdbfe` |
| `--chart-in` | `#2a3550` | `#e5e9f0` |
| `--chart-up1/2/3` | `#7c2d12` `#ea580c` `#fdba74` | `#fed7aa` `#fb923c` `#c2410c` |
| `--chart-seq1..4` | `#312e81` `#4338ca` `#6366f1` `#a5b4fc` | `#e0e7ff` `#a5b4fc` `#6366f1` `#3730a3` |
| `--chart-band` | `rgba(148,163,184,.22)` | `rgba(100,116,139,.18)` |
| `--chart-line` / `--chart-lastweek` / `--chart-grid` | `#e2e8f0` / `#94a3b8` / `#26324a` | `#0f172a` / `#64748b` / `#e8edf3` |

- [ ] **Step 1: Write the failing test**

```python
def test_echarts_is_served_locally(client):
    r = client.get("/static/vendor/echarts.min.js")
    assert r.status_code == 200 and b"echarts" in r.data[:4096]
```

- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_web_alerts_templates.py -k echarts` — Expected: FAIL (404).
- [ ] **Step 3: Vendor and wire.** `curl -fsSL https://cdnjs.cloudflare.com/ajax/libs/echarts/5.5.1/echarts.min.js -o logai2/web/static/vendor/echarts.min.js` and `curl -fsSL https://cdn.jsdelivr.net/npm/echarts@5.5.1/LICENSE -o logai2/web/static/vendor/ECHARTS-LICENSE`. `<script defer src="/static/vendor/echarts.min.js">` and `charts.js` before the view scripts. In `main.js`: `setView` calls `disposeCharts()` when the view changes; the theme toggle and a `prefers-color-scheme` change listener call `disposeCharts(); loadData();`. README "Repo layout": the vendored file, its version and licence.
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_web_alerts_templates.py` — Expected: pass. Open the console; in DevTools `echarts.version === '5.5.1'` and no console error on any page.
- [ ] **Step 5: Commit** `git add logai2/web/static/vendor/echarts.min.js logai2/web/static/vendor/ECHARTS-LICENSE logai2/web/static/js/charts.js logai2/web/static/index.html logai2/web/static/css/tokens.css logai2/web/static/js/main.js README.md tests/test_web_alerts_templates.py && git commit -m "Vendor ECharts 5.5.1; chart helper and chart tokens"`

---

### Task 5: Review page UI (decisions)

**Files:**
- Create: `logai2/web/static/js/views/review.js` (the decision page; the old `baseline.js` code it replaces goes in Task 7)
- Modify: `logai2/web/static/index.html` (new `#view-review` section and sidebar link after Baseline; the old `#view-baseline` tables and form are removed), `logai2/web/static/js/main.js` (`viewTitles.review = 'Review'`, `loadData` calls `loadReview`), `logai2/web/static/css/app.css`
- Test: manual in Chrome (below)

**Interfaces:**
- Consumes: Task 2 `/api/baseline/matrix`, Task 3 `/api/baseline/inspect`, existing `/api/baseline` (cards, suggestions, decisions, last commit), `POST/DELETE /api/baseline/decisions`, `POST /api/baseline/recompute` + command polling (keep `blPollCommand`); Task 4 `chartFor`, `chartTokens`, `baseOption`.
- Produces: route `#review/<service>/<key>/<days>[/<day>/<hour_from>/<hour_to>]` (key URL-encoded; `setView` keeps everything after the first `/` in `state.subView`); the optional tail preselects a range — Task 7's Review buttons use it.

Build exactly what spec section 3 lists (the mockup's Review tab is the reference):
- Header (service, Show, 14 | 28, Recompute), four cards.
- Matrix: ECharts heatmap, x hours on top, y days newest first (`inverse: true`), `●` prefix on `in_baseline`, weekend labels muted; per-item colour from `chartTokens().cls[cell.cls]`; label marks `▲` (suggested) / `✕` (exclude) / `✓` (keep); selected cells `borderWidth 3` accent. Click selects (a suggested cell selects its suggestion), shift+click extends within the day. Chart | Table toggle (HTML table of z). To review list and Decisions (Undo) beside it.
- Inspector: sentence; bounds chart (stacked lower + band series, this day line, last week dashed, out-of-band scatter, `markArea` selection, `▲` scatter for `not_normal_minutes`); increased bar chart (labels `×N normal`, click → `#templates` filtered to that template); new templates table; key chips; Exclude / Keep with the `left_days` sentence shown before the click (warning style when any `< 3`); after a decision reload matrix + inspector and select the next suggestion.
- Coverage: heatmap 2 × 24, `seq` colour by `days/of`, label = days or `idle`, orange outline when `days < 3` or idle.
- Remove the old Suggestions table, Add-a-range form and Days table (markup and JS).

- [ ] **Step 1: Implement** the markup, view and styles.
- [ ] **Step 2: Check in Chrome** on a scratch copy of the live database (`createdb -T logai logai_ui_check`, run `scripts/run_web.py` with `LOGAI2_DATABASE_DSN` pointing at it; drop it after). Pass when: matrix colours match the API `cls`; hover shows rate / expected / z; click and shift+click select; a suggestion selects its range; Exclude shows the `left_days` text before the click, then marks `✕` and moves on; Undo restores; Show = Errors / a doc / a template reloads; 28 days fits; light, dark, 390 px width (matrix scrolls in its box, page does not), keyboard reaches every control, reduced motion has no animation; DevTools shows no error and the instance count stays flat after switching views 10 times (`echarts.getInstanceByDom` on old nodes is undefined).
- [ ] **Step 3: Run** `.venv/bin/pytest -q` — Expected: all pass.
- [ ] **Step 4: Commit** `git add logai2/web/static/index.html logai2/web/static/js/views/review.js logai2/web/static/js/main.js logai2/web/static/css/app.css && git commit -m "Review page: matrix, inspector, coverage, decisions (ECharts)"`

---

### Task 6: Baseline series and incident API

**Files:**
- Modify: `logai2/web/baseline_view.py`, `logai2/web/app.py`
- Test: `tests/test_web_baseline_view.py`

**Interfaces:**
- Consumes: Task 2 `resolve_key`, `_cells` (rate, mean, std, band per day/hour), `UnknownKey`; Task 1 `minutes_by_hour`; Task 3's day/hour validation.
- Produces (in `baseline_view.py`):
  - `series(conn, cfg, service: str, key: str, ndays: int, today: date, now: float) -> dict` — spec 3B.7: `key, baseline_state ("full"|"young"|"borrowed"|"none"), points [{day, hour, rate|null, mean, std, band_low, band_high, last_week, z, out}], incidents [{day, hour_from, hour_to, ongoing, hours, peak {hour, rate, mean, band_low, band_high, z}, times_normal|null, decision ("exclude"|"keep"|"review"|null)}], profile {weekday [{hour, mean, band_low, band_high}], weekend [...], today [{hour, rate}]}`. Points run oldest → newest; hours without a `service_hours` row have `rate: null`; an incident never spans a missing hour; incidents crossing midnight are split per day (a decision is per day). `decision` = the covering `review_exclusions` row, else `"review"` when `minutes_by_hour` has minutes in its hours, else null.
  - `incident(conn, service: str, key: str, day: date, hour_from: int, hour_to: int, now: float) -> dict` — `{ongoing, modules [{module, lines}] (Σ alert_minutes.count by module_code in the range, desc; the key's template, or all the service's templates for *total/*error), samples [{at, template_id, line}]}`; `samples` = newest `error_samples` (≤ 10) of the key's template, or of the 3 ERROR+ templates with the largest excess for `*total` / `*error`, **only when `ongoing`**, else `[]`.
- Routes: `GET /api/baseline/series?service&key&days`, `GET /api/baseline/incident?service&key&day&hour_from&hour_to` (404 / 400 as `/inspect`).

- [ ] **Step 1: Write the failing tests** (Task 2's helpers and seeding, plus `T00032` with weekday baseline mean 36 σ 6.5 at hours 2–3, ×22 on Tue 20 Oct 02:00–04:00, ×2.4 today 13:00–14:00 with today's last hour 14).

```python
def test_series_points_and_incidents(conn, cfg):
    r = series(conn, cfg, "pay", "pay:T00032", 14, TODAY, NOW)
    tue = [i for i in r["incidents"] if i["day"] == "2026-10-20"][0]
    assert (tue["hour_from"], tue["hour_to"], tue["ongoing"], tue["times_normal"]) == (2, 4, False, 22.0)
    assert (tue["peak"]["band_low"], tue["peak"]["band_high"], round(tue["peak"]["z"], 1)) == (16.5, 55.5, 116.3)
    today = [i for i in r["incidents"] if i["day"] == "2026-10-21"][0]
    assert today["ongoing"] and (today["hour_from"], today["hour_to"]) == (13, 15)
    assert r["points"][0]["day"] == "2026-10-08" and r["baseline_state"] == "full"

def test_series_decision_review_and_none(conn, cfg):
    # 16 Oct 10:00 excluded on Review; 20 Oct 02:00 has alert_changes, no decision; 12 Oct 18:00 out but never alerted
    d = {(i["day"], i["hour_from"]): i["decision"]
         for i in series(conn, cfg, "pay", "pay:T00032", 14, TODAY, NOW)["incidents"]}
    assert d[("2026-10-16", 10)] == "exclude" and d[("2026-10-20", 2)] == "review" and d[("2026-10-12", 18)] is None

def test_series_drops_only_for_service_keys(conn, cfg):
    # T00011 (INFO) and *total both at 0.45 × mean on Thu 15 Oct 16:00
    assert not any(i["day"] == "2026-10-15" for i in series(conn, cfg, "pay", "pay:T00011", 14, TODAY, NOW)["incidents"])
    assert any(i["day"] == "2026-10-15" for i in series(conn, cfg, "pay", "*total", 14, TODAY, NOW)["incidents"])

def test_series_new_template_has_no_band(conn, cfg):
    r = series(conn, cfg, "pay", "pay:T00309", 1, TODAY, NOW)
    assert r["baseline_state"] == "none" and r["incidents"] == [] and r["profile"]["weekday"] == []
    assert all(p["band_high"] is None for p in r["points"])

def test_incident_modules_and_samples_only_when_ongoing(conn, cfg):
    live = incident(conn, "pay", "pay:T00032", date(2026, 10, 21), 13, 15, NOW)
    assert live["ongoing"] and live["modules"][0] == {"module": "LIXI_PROMO", "lines": 5210} and len(live["samples"]) == 3
    old = incident(conn, "pay", "pay:T00032", date(2026, 10, 20), 2, 4, NOW)
    assert not old["ongoing"] and old["samples"] == [] and old["modules"]

def test_series_validation(client):
    assert client.get("/api/baseline/series?service=pay&key=*total&days=5").status_code == 400
    assert client.get("/api/baseline/series?service=pay&key=pay:T09999").status_code == 404
```

- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_web_baseline_view.py -k "series or incident"` — Expected: FAIL (`ImportError: cannot import name 'series'`).
- [ ] **Step 3: Implement** `series`, `incident` and the two routes. `last_week` = the same key's rate 7 days earlier (null without a row); `profile` from the key's `baseline` rows (weekday / weekend × 24, band with that daytype's k), `today` from today's points.
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_web_baseline_view.py` — Expected: all pass.
- [ ] **Step 5: Commit** `git add logai2/web/baseline_view.py logai2/web/app.py tests/test_web_baseline_view.py && git commit -m "Baseline API: series with possible incidents, incident modules and samples"`

---

### Task 7: Baseline page UI (charts)

**Files:**
- Modify: `logai2/web/static/index.html` (`#view-baseline` markup), `logai2/web/static/js/views/baseline.js` (rewrite), `logai2/web/static/css/app.css`
- Test: manual in Chrome (below)

**Interfaces:**
- Consumes: Task 6 `/api/baseline/series`, `/api/baseline/incident`; Task 2 `/api/baseline/matrix` (read-only grid); `/api/services`; `/api/templates?service=<s>&size=500` for the Show list (id, level, text; "(new)" when `first_seen` < 24 h); Task 4 chart helper; Task 5's `#review/...` route.
- Produces: route `#baseline/<service>/<key>/<days>`.

Build exactly what spec section 3B lists (the mockup's Baseline tab is the reference): header and four cards; the actual-vs-expected chart (stacked band, actual, a week earlier dashed, out-of-band scatter, one `markArea` per incident labelled `✕` / `✓` by decision, `dataZoom` inside + slider when days > 1); the incidents table (state `● ongoing` / `resolved HH:00`; Review button → `#review/<service>/<key>/14/<day>/<hour_from>/<hour_to>`); a row click zooms ±12 h and loads the incident detail (module bar chart in %, then the sample-lines table or the "Not available for this period" note naming the future upgrade); the read-only matrix; the baseline-itself chart.

- [ ] **Step 1: Implement** the markup, view and styles.
- [ ] **Step 2: Check in Chrome** on the scratch database. Pass when: band and points match `/series`; every incident is shaded and listed; ongoing vs resolved is right; a row click zooms and shows modules; samples show only for an ongoing incident, the note otherwise; Review opens Review with the range selected; a new template shows no band and "None yet"; Today / 7 / 14 switch; light, dark, 390 px, keyboard, reduced motion; no console error; instance count flat after 10 view switches.
- [ ] **Step 3: Run** `.venv/bin/pytest -q` — Expected: all pass.
- [ ] **Step 4: Commit** `git add logai2/web/static/index.html logai2/web/static/js/views/baseline.js logai2/web/static/css/app.css && git commit -m "Baseline page: actual vs expected, possible incidents, modules, samples (ECharts)"`

---

### Task 8: Engine and resolver status fields

**Files:**
- Modify: `logai2/engine.py` (`_status`, `_flush`, `_observe`), `logai2/resolver.py` (`run`)
- Test: `tests/test_engine.py`, `tests/test_resolver.py`

**Interfaces:**
- Produces in `worker_status['engine']`: `flush_seconds` (last completed flush, `0.0` before the first), `flush_seconds_max` (max over the last 900 s), `late_counts` (`owner.minutes.late`), `events_per_second_15m` (events observed in the last 900 s / the window actually covered, ≥ 1 s), `history` (≤ 60 points `{"t": minute_start_epoch_s, "lag": float, "eps": float}`, one per wall minute, `eps` = events in the last 60 s / 60).
- Produces in `worker_status['linker']`: `llm_today` (the resolver's `day` count after the run: results in the last 24 h plus calls that wrote nothing; set before any early return once profiles exist).

- [ ] **Step 1: Write the failing tests**

```python
def test_status_history_and_flush_fields(conn, cfg, make):
    # run ticks across 3 wall minutes with traffic, using the existing fake clock pattern
    ...
    s = q(conn, "SELECT status FROM worker_status WHERE worker = 'engine'")[0]["status"]
    assert 2 <= len(s["history"]) <= 60 and set(s["history"][0]) == {"t", "lag", "eps"}
    assert s["history"][-1]["t"] % 60 == 0 and s["events_per_second_15m"] > 0
    assert s["flush_seconds"] >= 0 and s["flush_seconds_max"] >= s["flush_seconds"] and s["late_counts"] == 0

def test_history_keeps_60_minutes(conn, cfg, make):
    # 70 wall minutes of ticks → exactly 60 points, oldest first
    ...
    assert len(h) == 60 and h[0]["t"] < h[-1]["t"]

def test_llm_today_counts_results_and_failed_calls(conn, cfg, profile):
    ...   # one draft written, one failed call
    assert status["llm_today"] == 2
```

- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_engine.py tests/test_resolver.py -k "history or flush_fields or llm_today"` — Expected: FAIL (`KeyError: 'history'`).
- [ ] **Step 3: Implement.** Engine: `deque` of `(t, duration)` for flushes (measure after `owner.flush` returns; the status written by a flush reports the previous flush, which is fine and documented), `deque` of `(t, events)` for 900 s, `deque(maxlen=60)` for history appended in `_status` when `minute_of(now)` differs from the last point. Resolver: `status["llm_today"] = day`.
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_engine.py tests/test_resolver.py` — Expected: all pass.
- [ ] **Step 5: Commit** `git add logai2/engine.py logai2/resolver.py tests/test_engine.py tests/test_resolver.py && git commit -m "Status: flush times, late counts, 15-min rate, 60-min history, LLM answers today"`

---

### Task 9: Pipeline API: services, volume, stages

**Files:**
- Create: `logai2/web/pipeline_view.py`
- Modify: `logai2/web/app.py` (`pipeline()` adds three fields)
- Test: `tests/test_web_pipeline_view.py`

**Interfaces:**
- Consumes: Task 8 status fields; `Baseline`, `judge`; `timeutil`.
- Produces (in `pipeline_view.py`):
  - `services(conn, cfg, engine: dict, day: date, hour: int) -> list[dict]` — per service `{service, rate, last_week, z, miss_ratio, templates, new_24h, unparsed_1h, not_normal}`; `rate` = `*total` this hour / its minutes so far (null without a row); `z` as Task 2 (σ floor 1.0, null without baseline); `unparsed_1h` = `hourly_counts` key `<service>:unparsed` this hour; `not_normal` = `alert_state` rows with state ≠ NORMAL; sorted by |z| desc, nulls last.
  - `volume(conn, cfg, day: date, hour: int) -> list[dict]` — per service `{service, hours: [{hour, rate|null, band_low, band_high, last_week|null}]}` for hours 0..`hour`, band via `judge` on `*total`.
  - `stage_states(engine: dict, engine_stale: bool, linker: dict | None, linker_stale: bool, unparsed_1h: int, now: float) -> dict[str, dict]` — keys `elasticsearch, reader, templates, detection, flush, prometheus, linker, llm`, each `{"status": "ok"|"warn"|"bad", "reason": str|None}`, rules exactly as Global Constraints and spec 4.2. Pure: no database.
- `GET /api/pipeline` adds `services`, `volume`, `stages` (existing fields unchanged).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("engine, stale, linker, lstale, stage, expected", [
    ({"lag_seconds": 301, "detection_enabled": True}, False, LINKER_OK, False, "detection", "warn"),
    ({"lag_seconds": 30, "detection_enabled": False}, False, LINKER_OK, False, "detection", "warn"),
    (ENGINE_OK, True, LINKER_OK, False, "flush", "bad"),
    ({**ENGINE_OK, "flush_seconds_max": 10.5}, False, LINKER_OK, False, "flush", "warn"),
    ({**ENGINE_OK, "last_commit": {"at": AT_27H_AGO, "today": "2026-10-20"}}, False, LINKER_OK, False, "flush", "warn"),
    ({**ENGINE_OK, "warnings": ["high miss ratio: pay 62%"]}, False, LINKER_OK, False, "reader", "warn"),
    ({**ENGINE_OK, "history": ZERO_EPS_5, "indices": {"a": {"caught_up": False, "failing": False}}}, False, LINKER_OK, False, "reader", "bad"),
    ({**ENGINE_OK, "indices": {"a": {"failing": True}, "b": {"caught_up": True}}}, False, LINKER_OK, False, "elasticsearch", "warn"),
    ({**ENGINE_OK, "indices": {"a": {"failing": True}}}, False, LINKER_OK, False, "elasticsearch", "bad"),
    (ENGINE_OK, False, None, True, "linker", "bad"),
    (ENGINE_OK, False, {**LINKER_OK, "llm_profiles": {"g": {"down_until": NOW + 60}, "o": {"down_until": None}}}, False, "llm", "warn"),
    (ENGINE_OK, False, {**LINKER_OK, "llm_profiles": {"g": {"down_until": NOW + 60}}}, False, "llm", "bad"),
    (ENGINE_OK, False, LINKER_OK, False, "templates", "ok"),
])
def test_stage_states(engine, stale, linker, lstale, stage, expected):
    s = stage_states(engine, stale, linker, lstale, 0, NOW)[stage]
    assert s["status"] == expected and (s["reason"] is None) == (expected == "ok")

def test_unparsed_lines_degrade_templates():
    assert stage_states(ENGINE_OK, False, LINKER_OK, False, 3, NOW)["templates"]["status"] == "warn"

def test_services_rate_last_week_z_and_order(conn, cfg):
    ...
    rows = services(conn, cfg, ENGINE_OK, date(2026, 10, 21), 14)
    assert [r["service"] for r in rows] == ["notify", "recharge", "quiet"]   # |z| desc, null last
    assert rows[1]["rate"] == 30.0 and rows[1]["last_week"] == 25.0 and rows[1]["unparsed_1h"] == 2

def test_volume_today_hours_with_band(conn, cfg):
    # recharge weekday *total baseline at hours 14 and 15: 540 ± 57 (full, k 3); no service_hours row at 03:00
    v = {x["service"]: x for x in volume(conn, cfg, date(2026, 10, 21), 14)}["recharge"]["hours"]
    assert len(v) == 15 and v[14]["band_high"] == 711.0 and v[3]["rate"] is None

def test_pipeline_route_has_new_fields(client):
    body = client.get("/api/pipeline").get_json()
    assert {"services", "volume", "stages", "status", "cursors", "stale", "linker", "linker_stale"} <= set(body)
```

(`ENGINE_OK`, `LINKER_OK`, `ZERO_EPS_5`, `AT_27H_AGO`, `NOW` are module constants in the test file: a healthy status per Task 8's fields, a healthy linker status, five history points with `eps` 0, an ISO time 27 h before `NOW`.)

- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_web_pipeline_view.py` — Expected: FAIL (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** `pipeline_view.py` and extend `pipeline()` (day/hour from `day_hour(minute_of(time.time()))`).
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_web_pipeline_view.py tests/test_web_sources.py` — Expected: all pass.
- [ ] **Step 5: Commit** `git add logai2/web/pipeline_view.py logai2/web/app.py tests/test_web_pipeline_view.py && git commit -m "Pipeline API: services, volume vs expected, stage states"`

---

### Task 10: Pipeline page UI

**Files:**
- Modify: `logai2/web/static/index.html` (`#view-pipeline` markup), `logai2/web/static/js/views/pipeline.js` (rewrite), `logai2/web/static/css/app.css`
- Test: manual in Chrome (below)

**Interfaces:**
- Consumes: Task 9 `/api/pipeline` (`stages`, `services`, `volume`, plus existing fields), Task 8 status fields, Task 4 chart helper.

Build exactly what spec section 4 lists; the mockup is the reference:
- Status line (kept).
- Topology: ECharts `graph` on a hidden `cartesian2d` grid (x, y 0..100), nodes at the mockup positions (main row y 70: Elasticsearch 7, Reader 24, Templates 41, Minutes & detection 58, Postgres flush 75, Prometheus 92; row y 18: LLM resolver 58, Linker 75), `roundRect` 150 × 82, border = status colour, label rich text name / headline / detail / `● OK|Degraded|Down`; edges with arrows, Linker → Postgres flush dashed labelled "via Postgres"; a `lines` series with `effect` on the main path, off under reduced motion or when `stale`. Click a node → stage details under the chart (indices table, flush and commit, LLM profiles with `down_until`, …). Container scrolls horizontally under 980 px.
- Lag and Events/s: two line charts from `status.history`, lag with a dashed `markLine` at 300 s.
- Tiles: lag now, events/s (15 min), cache hit, late counts, detection, bootstrap.
- Volume small multiples: first 6 of `volume` in `services` order, SVG renderer, band + today + last week dashed, out-of-band points; "Show all"; click → `#baseline/<service>/*total/14`.
- Services table; indices table (kept).
- Remove the browser sparkline (`pl.samples`, `#pl-spark`).
- Polling every 2 s calls `setOption` on the existing instances (`chartFor` returns them); nothing is re-created per poll.

- [ ] **Step 1: Implement** the markup, view and styles.
- [ ] **Step 2: Check in Chrome** on the scratch database with the engine and linker running against it. Pass when: every stage shows the state `/api/pipeline` `stages` gives (stop the linker → Linker Down after 90 s); particles stop with reduced motion and when the engine is stopped; lag/eps charts show history after a page reload; a small multiple click opens Baseline for that service; light, dark, 390 px; after 5 minutes on the page the ECharts instance count is unchanged and memory is flat in DevTools; no console error.
- [ ] **Step 3: Run** `.venv/bin/pytest -q` — Expected: all pass.
- [ ] **Step 4: Commit** `git add logai2/web/static/index.html logai2/web/static/js/views/pipeline.js logai2/web/static/css/app.css && git commit -m "Pipeline page: topology, history, volume vs expected (ECharts)"`

---

### Task 11: Docs, merge readiness

**Files:**
- Modify: `docs/WEB_UI.md` (Baseline, new Review and Pipeline sections; HTTP API: `/api/baseline/matrix`, `/inspect`, `/series`, `/incident`, new `/api/pipeline` fields), `ARCHITECTURE.md` (§10.5 engine fields, linker `llm_today`; §16 reading list adds `web/baseline_view.py`, `web/pipeline_view.py`), `docs/specs/2026-10-21-baseline-pipeline-pages.md` (Status: built), `docs/design/per-service-baseline.md` (section 9: phases 1–2 built), this plan (Status line)

- [ ] **Step 1: Update the docs** to describe the code as built (fields and copy exactly as implemented).
- [ ] **Step 2: Run** `.venv/bin/pytest -q` and `graphify update .` — Expected: all pass.
- [ ] **Step 3: Commit** `git add docs/WEB_UI.md ARCHITECTURE.md docs/specs/2026-10-21-baseline-pipeline-pages.md docs/design/per-service-baseline.md docs/plans/2026-10-21-console-watchdog-pages.md && git commit -m "Docs: Baseline and Pipeline pages, new API and status fields"`

Order: 1 → 2 → 3 → 5 (Review UI) and 2 → 6 → 7 (Baseline UI); 4 before 5, 7 and 10; 8 → 9 → 10 (Pipeline); 11 last. The Review/Baseline chain (1–7) and the Pipeline chain (8–10) are independent.
