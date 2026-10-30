# Method bands: 4 methods computed on read, k tuned per method (plan, tasks mb1, mb2, mb4, mb5)

Status: **superseded 2026-10-30** by docs/specs/2026-10-30-evaluation-bands.md and docs/plans/2026-10-30-evaluation-bands.md. Was: **approved** (2026-10-29, plan + mockup). 2026-10-30: cut to 5 methods (ets, prophet and their nightly
job dropped, so no new table, no migration, no lab image). Same day: cut to 4 (`within_between` dropped: it equals σ, see below;
`median` gets a robust spread). Nothing dispatched until the user says start.
Mockup: https://claude.ai/artifact/MH5YqMboT6BAGKwbAr9u22 (sample data).

## Goal

Replace the current Evaluation page (the name stays "Evaluation") with one chart, drawn like Baseline: a key's real hourly rate plus the band (low, high)
of **every method, drawn as soon as the page opens**, one chart per method in the Baseline style, each at
**its own best k** from the fit test below. All 4 methods are computed on request from the hourly tables, in
milliseconds per key; nothing is stored and no job runs except the k tune. Above the
chart, one small table per kind shows each method's k, coverage and width. Everything else on the page goes:
verdicts, gates, sweeps, episodes, Test A/B/C, runs.

## Constraint: the company runs v15

The company runs `congdock123/logai-engine:v15` (master ~`960fe1e`, migrations 001-009). It has no lab recorder, no
`lab_*` tables and **no per-minute counts**. So:

- Every input comes from tables v15 already fills: `hourly_counts` (sum, sumsq, kept 90 days), `service_hours`
  (n_minutes), `day_status`, `review_exclusions`, `baseline`. The tune result goes into `settings`
  (key `bands_tune`). No new table, no migration. No recorder.
- Deploying to the company upgrades v15 to the new image. The engine then applies 010/011 (empty `lab_*` tables)
  and 012 (`services.monitored`, from pattern switch), in seconds. The recorder stays off: `LOGAI2_LAB_RECORD` defaults to false outside compose.

## The 4 methods, chosen from the bk17 results

Source: `docs/validation/2026-10-28-bakeoff-laptop-proof.md` (28-day world, sections 6 and 8). Rule: keep what
won there; add a method only where it fixes a loss bk17 measured; drop duplicates and losers.

| id | method | why it is in |
|---|---|---|
| `sigma` | live σ: m ± kσ, daytype split, last 5 weekdays / 4 weekend days | the reference; the live band |
| `nb` | negative binomial NB(m, v) quantiles (Poisson when v ≤ m) | tightest band at good coverage: M3 ratio 0.625 (coverage 0.998), M2 0.755, M4 0.943 (passes gates); spike recall 1.00 vs σ 0.58 |
| `median` | median(m_d) ± k·√(median(v_d) + (1.4826·MAD(m_d))²) | fixes a measured loss: mad was tightest on M4 (0.498) but flapped. Robust centre and robust spread: one incident day in the week moves neither (example below: high 9.8 vs σ 12.6) |
| `ewma` | recent days weigh more (half-life 2 days) | the worlds' 3 %/week trend; the cheap way to follow a level change |

Dropped, with the bk17 reason:

| method | reason |
|---|---|
| `nb_clip` | identical to nb in every bk17 row; `median` covers robustness |
| `poisson`, `quasi_poisson` | nb already falls back to Poisson when v ≤ m |
| `weekly`, `last_week` | weekly ≈ σ (weekend 0.89, holiday 1.03); last_week needs 8+ days |
| `chronos` | worse than σ (M1 1.60, M2 1.41), fails coverage |
| `lgbm` | worse than σ almost everywhere (M1 1.44, M2 2.77, M4 8.27) |
| `mstl`, `greykite` | not in bk17. Both overlap ets/prophet; Greykite also needs its own image (pins pandas < 2) |
| `sigma_all`, `sigma_smooth`, `trimmed`, `sqrt` | not in bk17 and no measured loss for them to fix |
| `within_between` | dropped 2026-10-30: σ is pooled over every minute of the picked days, so its variance already is mean(v_d) + var(m_d) (law of total variance); adding var(m_d) again only adds the n−1 correction (example: 13.2 vs σ 12.6) |
| `ets`, `prophet` | dropped 2026-10-30 (user): about 1 s per fit, so they need a nightly job, a table and the lab image; the 5 hourly methods are computed on read |
| `cusum`, `pca`, `mprofile`, `mad` (minute) | need per-minute counts, which v15 does not keep |

Notation: per (service, key, daytype, hour) and per training day d there are n_d minutes, s_d sum and q_d sumsq.
The day's minute mean is m_d = s_d/n_d and its variance v_d = q_d/n_d − m_d². Pooled m and v are taken over the
training days, as REBUILD does. Bands are per-minute counts (alerting is per minute). For `nb`, k maps to α
through the normal tail (k = 3 gives α = 0.00135).

## Data each method needs

Every method starts from the same v15 rows per (service, key, day, hour): `service_hours.n_minutes` (n, observed
minutes), `hourly_counts.sum` (s) and `hourly_counts.sumsq` (q). From them come the hour's minute mean
m_d = s/n and minute variance v_d = q/n − m_d². Hours with n = 0 or excluded in Review are skipped. `day_status`
gives committed days and their daytype.

| method | rows it reads | days used | minimum to draw a band | notes |
|---|---|---|---|---|
| `sigma` | `baseline` table (mean, std per service, key, daytype, hour) | the rebuild's picked days: last 5 committed weekdays / 4 weekend days | 1 committed day of the daytype (young, k 4); full after `full_after_days` | read as is, not recomputed |
| `nb` | n, s, q of the picked days | same as sigma | 1 day (v comes from the minutes inside each hour) | Poisson when v ≤ m |
| `median` | m_d and v_d per picked day | same as sigma | 3 days (the median of 2 is the mean) | robust centre and spread |
| `ewma` | n, s, q of every day of the daytype | all kept days, weight 0.5^(age/2 days) | 2 days | old days fade, no cap |

The tune test needs its 3 test days plus each method's minimum before the first test day. With one week, the
weekend has 2 days (Sat, Sun): `median` has no weekend band yet. The tune table
flags both.

## The test that picks k (`bands tune`)

Fit test, walk forward, the same for every method, run on every key in scope of every service.

**Key scope** (the only keys the page and tune show): `*total`, `*error`, `*warn`, `*info` and every template
past its first 24 hours. Out of scope are keys where only σ could apply: new templates (first 24 h, the born rule,
no history to fit), documents `doc:<id>` and verdict groups (summed keys whose minute variance can't be rebuilt
from per-template sumsq). They keep their live σ on Baseline and Alerting, unchanged.



1. Test days = the last 3 full days (`--test-days`, default 3). Each method is fitted only on the days before each
   test day d and gives the band for every hour of d.
2. **Minutes outside the band, estimated.** v15 has no minute counts, only each hour's n_d, mean m_d and
   variance v_d. Expected minutes above high = n_d · P(X > high), with X ~ NB(m_d, v_d) (Poisson when v_d ≤ m_d,
   normal when m_d > 1000). Below low likewise, but only for `*total`, the only key live alerts low on. Upward,
   the live floor applies (ERROR 5, WARN 10): high_eff = max(high, floor). INFO templates have no floor and are
   tuned on the band alone.
3. Coverage = 1 − Σ minutes outside / Σ n_d, per method × kind × k.
   Kinds: `*total`, `*error`, `*warn`/`*info`, busy templates (mean ≥ 2/min), sparse templates.
4. Grid k ∈ {2, 2.5, 3, 3.5, 4, 5, 6, 8}. **Best k** = the smallest k with coverage ≥ 99.5 % (the bk17 target).
   If no k reaches it: k = 8, flagged "never reaches 99.5 %".
5. At the best k: mean band width per minute, and the ratio to σ's width at its own best k. Narrower at the same
   coverage = fits better. The tune table sorts methods by this ratio.

This measures fit only, with no injections and no labels. bk17 Test A covered detection.

**Estimate check (laptop, mb2).** Run tune on `lab_s_w28` twice: once from its hourly sums, once from the exact
minutes. Gate: coverage gap ≤ 0.2 pp and the same best k (one grid step apart allowed, with a note). Repeat on
`lab_minute_counts` (2 real laptop days). If the gate fails, the page labels coverage "estimated" and we discuss.

Output: `settings.bands_tune` = {run_at, test days, per kind: {method: {k, coverage, width, ratio, flag}}}.
Run:
- `python -m logai2.bands tune` (pure Python, any logai2 pod), and a "Re-tune" button on the page that runs the
  same function in a web thread and shows the last run time. Until the first run every method is drawn at k = 3,
  labelled "k 3 (not tuned)". The result is written in one transaction at the end, with a `version`; a result of
  another version counts as not tuned.

## Tasks

| # | Task | Role | Review |
|---|---|---|---|
| mb1 | `logai2/bands.py`: `sigma`, `nb`, `median` (robust), `ewma` (fit from day-hour (n, s, q)), band at k, NB/Poisson/normal tail. Tests: `sigma` equals the `baseline` table and the Baseline series band exactly; each method against hand values on a seeded fixture; NB → Poisson when v ≤ m. | Sonnet | Opus |
| mb2 | `python -m logai2.bands tune`: walk-forward, estimated coverage, floors, best k per method × kind, `settings.bands_tune` (with `version`, one transaction). Estimate check on `lab_s_w28` and `lab_minute_counts`, plus timing; report `docs/validation/2026-10-xx-bands-tune.md`. | Sonnet | Opus |
| mb4 | API + page. `GET /api/bands/series?service&key&days` returns the rate and all 4 bands in one response (each at its best k, or k 3 not tuned; hours outside each); a missing band says why (not enough days); target under 300 ms per key. `GET /api/bands/tune`, `POST /api/bands/tune` (Re-tune). Page `#evaluation/<service>/<key>/<days>` (nav name stays "Evaluation") replaces the current Evaluation page: opens on `*total`, 7 days, with **one chart per method** stacked (sigma first), each drawn like the Baseline chart: shaded band at the method's best k, the same white rate line, orange dot per hour outside, red box per run of such hours, a header line (k, coverage, width vs σ, hours outside); a method with no band shows a one-line reason instead of a chart; tune table per kind below; `keypicker.js`, `charts.js`. The key picker lists in-scope keys only (no new templates, documents or verdict groups); any other key gets 404. Delete `eval_view.py`, `/api/eval/*`, `evaluation.js`, `tests/test_web_eval_view.py`. Docs: WEB_UI, README, ARCHITECTURE. | Sonnet | Sonnet |
| mb5 | Laptop run on the live DB (`recharge`, 26-29 Oct: 4 days); Chrome look; deploy note v15 → new (engine first: 010-012, then web; then `bands tune` once). | lead | user |

Order: mb1 → mb2 → mb4 → mb5 (mb3 dropped with ets/prophet). One branch + worktree per task; merge into master only after the user confirms.

## Limits

- bk17 was synthetic. The 4 were chosen on synthetic evidence; the tune table on real data may rank them
  differently, and that ranking is the point of the page.
- In-sample on the chart: as on Baseline today, the displayed days are drawn against the current fit. The tune test
  itself is out-of-sample (walk forward).
- Coverage is estimated from hourly moments (see the estimate check), not counted minute by minute.
- No method learns the calendar: holidays look like weekdays (bk17: nb holiday ratio 2.91). `median` resists
  one odd day in the week; that is all.
- The laptop has 4 days. The real week is on the company after the deploy.

## Image update and pod restarts (k8s)

- Stored state is only `settings.bands_tune` in Postgres. Bands are computed per request, with no cache, so a new
  web pod draws them on its first request, with no warm-up.
- No migration for bands. Upgrading v15: engine first (it applies 010-012), then web (it never migrates).
- Web pod killed during a Re-tune (rollout, OOM, node drain): that run is lost; the previous result stays (one
  transaction at the end). The page shows the last run time; press Re-tune again. The run status is held in the web
  process only, which is safe because web is 1 replica with `Recreate`.
- Engine restart: the charts keep working (they read Postgres). Hours the engine missed have smaller n; every
  method weights by n, so a gap is not read as silence. `sigma` follows the `baseline` table, rebuilt at the 02:00
  commit as today.
- A new image whose tune changes the result format bumps `version`: charts fall back to "k 3 (not tuned)" until
  Re-tune.
- A browser tab opened before the deploy runs the old JS (B10): the old Evaluate page gets 404 from the deleted
  `/api/eval/*`. Reload the tab.

## Not changed

Live alerting, Baseline, Review, the recorder, `scripts/lab/`, the lab image and the `lab_*` tables.
