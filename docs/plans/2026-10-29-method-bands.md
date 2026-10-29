# Method bands: one chart, 7 methods, k tuned per method (plan, tasks mb1-mb5)

Status: **approved** (2026-10-29, plan + mockup). Nothing dispatched until the user says start.
Mockup: https://claude.ai/artifact/MH5YqMboT6BAGKwbAr9u22 (sample data).

## Goal

Replace the Evaluate page with one chart, drawn like Baseline: a key's real hourly rate plus the band (low, high)
of **every method, drawn as soon as the page opens**, one chart per method in the Baseline style, each at
**its own best k** from the fit test below. Nothing is computed while the user waits: hourly methods take
milliseconds per key on request, and forecaster bands are read from a table filled by a nightly job. Above the
chart, one small table per kind shows each method's k, coverage and width. Everything else on the page goes:
verdicts, gates, sweeps, episodes, Test A/B/C, runs.

## Constraint: the company runs v15

The company runs `congdock123/logai-engine:v15` (master ~`960fe1e`, migrations 001-009). It has no lab recorder, no
`lab_*` tables and **no per-minute counts**. So:

- Every input comes from tables v15 already fills: `hourly_counts` (sum, sumsq, kept 90 days), `service_hours`
  (n_minutes), `day_status`, `review_exclusions`, `baseline`. The tune result goes into `settings`
  (key `bands_tune`). Only one new table, migration 012 `method_bands`, holds the forecaster bands. No recorder.
- Deploying to the company upgrades v15 to the new image. The engine then applies 010/011 (empty `lab_*` tables)
  and 012, in seconds. The recorder stays off: `LOGAI2_LAB_RECORD` defaults to false outside compose.

## The 7 methods, chosen from the bk17 results

Source: `docs/validation/2026-10-28-bakeoff-laptop-proof.md` (28-day world, sections 6 and 8). Rule: keep what
won there; add a method only where it fixes a loss bk17 measured; drop duplicates and losers.

| id | method | why it is in |
|---|---|---|
| `sigma` | live σ: m ± kσ, daytype split, last 5 weekdays / 4 weekend days | the reference; the live band |
| `nb` | negative binomial NB(m, v) quantiles (Poisson when v ≤ m) | tightest band at good coverage: M3 ratio 0.625 (coverage 0.998), M2 0.755, M4 0.943 (passes gates); spike recall 1.00 vs σ 0.58 |
| `within_between` | σ² = within-hour v + day-to-day var(m_d) | fixes nb/σ's measured loss: no day-to-day variance, so the band collapses on a shifted day (nb holiday ratio 2.91, coverage 0.83) |
| `median` | median of daily means ± k√v | fixes a measured loss: mad was tightest on M4 (0.498) but flapped. This keeps the robust centre and σ's spread, so one incident day in the week does not drag the band |
| `ewma` | recent days weigh more (half-life 2 days) | the worlds' 3 %/week trend; the cheap way to follow a level change |
| `ets` | Holt-Winters / ETS, season 24 (statsmodels) | best forecaster: weekend 0.75, holiday 0.78 (M1) / 0.52 (M2); best drop recall 0.47 vs σ 0.40 |
| `prophet` | Prophet on the hourly-mean series | the only other method that learns the calendar: weekend 0.79, holiday 0.84 / 0.56. Its tails under-cover (B17); tuning k by coverage corrects that |

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
| `cusum`, `pca`, `mprofile`, `mad` (minute) | need per-minute counts, which v15 does not keep |

Notation: per (service, key, daytype, hour) and per training day d there are n_d minutes, s_d sum and q_d sumsq.
The day's minute mean is m_d = s_d/n_d and its variance v_d = q_d/n_d − m_d². Pooled m and v are taken over the
training days, as REBUILD does. Bands are per-minute counts (alerting is per minute). For `nb`, k maps to α
through the normal tail (k = 3 gives α = 0.00135). ets and prophet forecast the hourly mean, and the band is
forecast ± k·√(v + forecast variance).

ets and prophet run in the lab image (`logai2-lab`, offline) as a nightly job, on `*total`, `*error`, `*warn`,
`*info` and busy templates (mean ≥ 2/min) only. Prophet takes about 1 s per fit, so all 3k keys would take hours
per service. Other keys show the 5 hourly methods.

## Data each method needs

Every method starts from the same v15 rows per (service, key, day, hour): `service_hours.n_minutes` (n, observed
minutes), `hourly_counts.sum` (s) and `hourly_counts.sumsq` (q). From them come the hour's minute mean
m_d = s/n and minute variance v_d = q/n − m_d². Hours with n = 0 or excluded in Review are skipped. `day_status`
gives committed days and their daytype.

| method | rows it reads | days used | minimum to draw a band | notes |
|---|---|---|---|---|
| `sigma` | `baseline` table (mean, std per service, key, daytype, hour) | the rebuild's picked days: last 5 committed weekdays / 4 weekend days | 1 committed day of the daytype (young, k 4); full after `full_after_days` | read as is, not recomputed |
| `nb` | n, s, q of the picked days | same as sigma | 1 day (v comes from the minutes inside each hour) | Poisson when v ≤ m |
| `within_between` | n, s, q of the picked days | same as sigma | 2 days of the daytype (3+ to be steady) | adds var(m_d) across days |
| `median` | m_d per picked day, pooled v | same as sigma | 3 days (the median of 2 is the mean) | |
| `ewma` | n, s, q of every day of the daytype | all kept days, weight 0.5^(age/2 days) | 2 days | old days fade, no cap |
| `ets` | the hourly-mean series s/n, hour by hour, all days (no daytype split), plus v | all kept days (90-day retention) | 2 full days (48 hours, season 24) | gaps filled with the same hour's mean of other days; nightly, lab image |
| `prophet` | the same hourly series, plus v and `lab.holidays` | all kept days | 2 days (daily cycle); weekly cycle from 14 days | handles gaps; nightly, lab image |

The tune test needs its 3 test days plus each method's minimum before the first test day. With one week, the
weekend has 2 days (Sat, Sun): `median` has no weekend band yet and `within_between` is weak there. The tune table
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
- `python -m logai2.bands tune`: the 5 hourly methods, in the web or engine pod (pure Python).
- `python -m logai2.bands tune --forecast`: ets and prophet, in the lab image. It writes their tune rows and
  `method_bands` (bands at the best k for the last 7 days plus today; it replaces the previous rows).
- Nightly after the 02:00 commit: a k8s CronJob from the lab image (manifest in mb5); `--profile lab` on the laptop.

## Tasks

| # | Task | Role | Review |
|---|---|---|---|
| mb1 | `logai2/bands.py`: `sigma`, `nb`, `within_between`, `median`, `ewma` (fit from day-hour (n, s, q)), band at k, NB/Poisson/normal tail. Tests: `sigma` equals the `baseline` table and the Baseline series band exactly; each method against hand values on a seeded fixture; NB → Poisson when v ≤ m. | Sonnet | Opus |
| mb2 | `python -m logai2.bands tune`: walk-forward, estimated coverage, floors, best k per method × kind, `settings.bands_tune`. Estimate check on `lab_s_w28` and `lab_minute_counts`, plus timing; report `docs/validation/2026-10-xx-bands-tune.md`. | Sonnet | Opus |
| mb3 | `ets`, `prophet` behind the same method contract (lab image); `tune --forecast`; migration 012 `method_bands` (service, key, method, day, k, expected/low/high real[24]); key scope; timing on `lab_s_w28`; added to the report. | Sonnet | Opus |
| mb4 | API + page. `GET /api/bands/series?service&key&days` returns the rate and all 7 bands in one response (hourly methods computed at best k, forecasters from `method_bands`, hours outside each); a missing band says why (not tuned yet, sparse key outside the forecaster scope, not enough days); target under 300 ms per key. `GET /api/bands/tune`. Page `#bands/<service>/<key>/<days>` (nav "Bands") replaces `#evaluation`: opens on `*total`, 7 days, with **one chart per method** stacked (sigma first), each drawn like the Baseline chart: shaded band at the method's best k, the same white rate line, orange dot per hour outside, red box per run of such hours, a header line (k, coverage, width vs σ, hours outside); a method with no band shows a one-line reason instead of a chart; tune table per kind below; `keypicker.js`, `charts.js`. The key picker lists in-scope keys only (no new templates, documents or verdict groups); any other key gets 404. Delete `eval_view.py`, `/api/eval/*`, `evaluation.js`, `tests/test_web_eval_view.py`. Docs: WEB_UI, README, ARCHITECTURE. | Sonnet | Sonnet |
| mb5 | Laptop run on the live DB (`recharge`, 26-29 Oct: 4 days); Chrome look; deploy note v15 → new (engine first: 010-012, then web; `bands tune` in the pod, then the forecaster job; CronJob manifest). | lead | user |

Order: mb1 → (mb2, mb3) → mb4 → mb5. One branch + worktree per task; merge into master only after the user confirms.

## Limits

- bk17 was synthetic. The 7 were chosen on synthetic evidence; the tune table on real data may rank them
  differently, and that ranking is the point of the page.
- In-sample on the chart: as on Baseline today, the displayed days are drawn against the current fit. The tune test
  itself is out-of-sample (walk forward).
- Coverage is estimated from hourly moments (see the estimate check), not counted minute by minute.
- With 7 days, ets and prophet see one weekly cycle. Their bk17 gain was on weekends and holidays, so expect
  σ-like results until about 14 days of history (hourly counts are kept 90 days, so this improves by itself).
- The laptop has 4 days. The real week is on the company after the deploy.

## Not changed

Live alerting, Baseline, Review, the recorder, `scripts/lab/`, the lab image and the `lab_*` tables.
