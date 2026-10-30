# Evaluation page: 4 band methods computed on read (spec)

Status: draft for review, 2026-10-30. Replaces the plan `docs/plans/2026-10-29-method-bands.md` (7 methods), whose
rulings it keeps except where marked. Plan: `docs/plans/2026-10-30-evaluation-bands.md`.
Mockup: https://claude.ai/artifact/MH5YqMboT6BAGKwbAr9u22 (sample data).

## 1. Goal

The Evaluation page stops showing lab runs (verdicts, gates, sweeps, episodes, Test A/B/C). For one key it shows
the key's hourly rate and the range (low, high) of 4 methods, one chart per method, each at its own best k, so the
operator sees which method fits this service's real counts best. Live alerting does not change: it stays σ.

The page keeps its name **Evaluation** and its nav slot (label "Evaluate" becomes "Evaluation").

## 2. Constraint: the company runs v15

Every input comes from tables v15 already fills: `hourly_counts` (sum, sumsq), `service_hours` (n_minutes),
`day_status`, `review_exclusions`, `templates`. No new table, no migration, no lab image, no job. The only stored
result is `settings` key `bands_tune`. The page reads no `lab_*` table.

## 3. The 4 methods

Per (service, key, daytype, hour) and per training day d: n_d minutes, s_d sum, q_d sumsq; m_d = s_d/n_d,
v_d = max(0, q_d/n_d − m_d²). Bands are per-minute counts (alerting is per minute).

| id | centre | spread / range | training days | min days |
|---|---|---|---|---|
| `sigma` | m = Σs/Σn | m ± k·√(Σq/Σn − m²) | REBUILD's picked days: last 5 committed weekdays / 4 weekend days before the day judged | 1 |
| `nb` | m (as sigma) | NB(m, v) quantiles at tail α = 1 − Φ(k) per side; Poisson when v ≤ m; normal when m > 1000 | as sigma | 1 |
| `median` | median(m_d) | ± k·√(median(v_d) + (1.4826·MAD(m_d))²) | as sigma | 3 |
| `ewma` | Σw·s/Σw·n | ± k·√(Σw·q/Σw·n − m²), w_d = 0.5^(age_d/2), age_d = days before the newest training day | every committed day of the daytype in the 28 days before the day judged | 2 |

- Day selection, Review exclusions, n = 0 hours and missing `hourly_counts` rows follow REBUILD
  (`logai2/jobs/commit.py`) exactly: an hour with a `service_hours` row but no `hourly_counts` row counts as
  s = q = 0; `*warn` / `*info` start at their first day (the born rule).
- `sigma` here is the per-hour band. The Baseline page also blends in the next hour and uses k 4 while young; the
  Evaluation page does not (it compares methods on the same footing).
- Dropped: `within_between` (equals σ by the law of total variance), `ets`, `prophet` (need a nightly job and the
  lab image; user, 2026-10-30), and the methods the 2026-10-29 plan dropped.

## 4. Keys in scope

`*total`, `*error`, `*warn`, `*info`, and every template whose `first_seen` is more than 24 h before now. Out of
scope (404 on the API, not listed in the picker): new templates, `doc:<id>`, `verdict:*`. Services switched Off
(pattern switch) are 404, as on every page.

Kind of a key (tune groups): `*total`, `*error`, `*warn/*info`, `busy` (template with mean ≥ 2 per minute over its
training rows), `sparse` (any other template).

## 5. The fit test that picks k (`tune`)

Walk forward over the last 3 full days (test days), every in-scope key of every monitored service:

1. For each test day d and hour h, each method is fitted on the days before d (section 3) and gives (low, high).
2. Expected minutes outside, from that hour's (n, m, v) on day d, with X ~ NB(m, v) (Poisson when v ≤ m, normal
   when m > 1000):
   - above: n · P(X ≥ t), t = max(⌊high⌋ + 1, floor); floor = `cfg.floors` of the key's level (ERROR 5, WARN 10;
     `*error` is ERROR; `*total`, `*info`, INFO templates have none; `*warn` is view-only and has none).
   - below: only for `*total`: n · P(X ≤ ⌈low⌉ − 1).
3. Coverage = 1 − Σ outside / Σ n, per method × kind × k, k ∈ {2, 2.5, 3, 3.5, 4, 5, 6, 8}.
4. Best k = the smallest k with coverage ≥ 99.5 %. None reaches it: k = 8, flag "never reaches 99.5 %".
5. Width at best k = mean over hours of (high − max(low, 0)); ratio = width / σ's width at σ's best k.
6. A method without a band for a key-hour (min days) skips that hour; a method with no fitted hour in a kind gets
   flag "not enough days" and no k.

Result in `settings.bands_tune`:
`{"version": 1, "run_at": ISO, "test_days": [ISO, ISO, ISO], "seconds": float,
  "kinds": {kind: {"keys": int, "methods": {method: {"k": float|null, "coverage": float|null,
  "width": float|null, "ratio": float|null, "flag": str}}}}}`.
Written once, at the end, in one transaction. A stored value whose `version` is not 1 counts as not tuned.

Runs: `python -m logai2.bands tune [config.yaml] [--test-days N]`, or the page's Re-tune button.

**Estimate check.** Coverage is estimated from hourly moments. On `lab_s_w28` (synthetic, per-minute counts in
`lab_minute_counts`) the estimated coverage and the coverage counted minute by minute must differ by ≤ 0.2 pp per
method × kind at each k, and give the same best k (one grid step apart allowed, with a note). If it fails, the page
labels coverage "estimated" and we discuss before eb3 merges.

## 6. HTTP API (replaces every `/api/eval/*` route)

- `GET /api/eval/bands?service=S&key=K&days=1|7` →
  `{"key", "kind", "tuned": bool, "run_at": ISO|null, "test_days": [...],
    "points": [{"day", "hour", "rate": float|null}],
    "methods": [{"id", "k": float, "tuned": bool, "coverage": float|null, "ratio": float|null,
                 "bands": [{"day", "hour", "low", "high"} | null per point], "outside": int, "missing": str|null}]}`.
  Methods in the order sigma, nb, median, ewma. Each shown day d is fitted on the days before d (as tonight's
  commit would for that day). k = the tuned k of the key's kind, else 3. `rate` = s/n of the hour, null without a
  `service_hours` row; today's hours up to now only. `outside` counts hours with rate > high, or rate < low for
  `*total`. `missing` = "not enough days" when no hour has a band. 400 on a bad `days`, 404 on an unknown,
  out-of-scope or Off key/service. Target: under 300 ms per key on the laptop DB.
- `GET /api/eval/tune` → `{"status": "idle"|"running", "started_at": ISO|null, "error": str|null,
  "result": bands_tune|null}`.
- `POST /api/eval/tune` → 202 `{"status": "running"}`; 409 if a run is already going. The run uses its own
  connection in a web thread; the status lives in the web process (web is 1 replica, `Recreate`).
- `GET /api/eval/keys?service=S` → the in-scope keys: `[{"key", "label", "kind"}]`, service keys first, then
  templates by level class then text.

## 7. Page (`#evaluation/<service>/<key>/<days>`)

As the mockup: header with service, key (in-scope keys only), 1 day / 7 days; a tune status bar (tuned time and test
days / "Not tuned yet: every method at k 3" / "Re-tuning…, the last result stays") with the Re-tune button; 4 method
cards (k, coverage, hours outside, width vs σ bar, "Narrowest at 99.5 %" on the smallest ratio among methods
without a flag; `sigma` tagged live); one panel with 4 stacked ECharts charts (shaded band, rate line, a dot per
hour outside, a box per run of such hours) on one shared y-scale and a synced hover (`echarts.connect`); an hour
slider for keyboard reading; the tune table per kind. Opens on `*total`, 7 days. Console tokens (`tokens.css`),
light and dark.

## 8. Restarts and deploys

Nothing but `settings.bands_tune` is stored; bands are computed per request with no cache. A web pod killed during
Re-tune loses that run; the previous result stays. Engine restarts do not affect the page (missed minutes lower n,
every method weights by n). Upgrade from v15: engine first (010-012), then web; then Re-tune once. A browser tab
opened before the deploy runs old JS (B10): reload.

## 9. Not changed

Live alerting, Baseline, Review, the recorder, `scripts/lab/`, the lab image, the `lab_*` tables (still written by
the lab, no longer read by the web).
