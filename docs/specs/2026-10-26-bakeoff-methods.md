# Bake-off methods: the 13 methods, why each was chosen, why it fits our problem

Companion to `docs/specs/2026-10-26-baseline-bakeoff.md` (the roster table, judging protocol J1–J9,
history needs). This doc is the reasoning behind the roster: 40 candidates were surveyed and cut to
13 methods plus one ensemble.

## Our problem, in the terms that decide the choice
- **What we judge:** per-minute counts per (service, key). The keys are Drain3 templates, `*total`, `*error`
  and `doc:` keys, about 3k of them. These are **counts**: integers, often small, never negative.
- **What we have today:** `judge()` (`logai2/owner/baseline.py`) alerts when a minute is above `mean + kσ` of a
  (service, key, daytype, hour) profile. The profile is rebuilt nightly from the last 5 weekdays / 4 weekend
  days (`jobs/commit.py`), plus floors, a `*total` drop rule and 2/3/3 hysteresis (`owner/detect.py`).
- **Where it hurts, by monitor kind:**

  | Kind | Key | What goes wrong with σ |
  |---|---|---|
  | M1 | `*total` | the real shape is weekly, not weekday/weekend; holidays (Tết) look like a drop |
  | M2 | `*error` | follows traffic, so a traffic rise alarms even if nothing breaks |
  | M3 | busy INFO/WARN template | like M1, noisier |
  | M4 | sparse ERROR template, 0–2/min | the counts are skewed: σ is too tight at 0 and alarms on a single 3 |
  | M5 | `doc:` key | sum of linked templates, follows traffic like M2 |
  | M6 | new template, < 1 day old | no profile at all |
  | M7 | template mix of a service | σ judges each key alone; a mix change with a normal total is invisible |

  Two more weaknesses cross all kinds: a past incident widens the band for days (it is in the profile),
  and a small rise that lasts (+20 % for an hour) never crosses `mean + kσ` in any single minute.
- **Constraints on the methods:** the company cluster has no internet (packages and weights baked into the
  image); company data never leaves the company; the app image takes no new dependencies; ES keeps
  7 days, so long history comes from our own `hourly_counts` and the recorder.

## Selection rules
1. **Every monitor kind gets at least two serious candidates.** No kind is judged by σ alone.
2. **One or two methods per family**, the strongest or simplest of each. A dropped sibling must be
   covered by a kept method (table at the end).
3. **A spread of costs.** The core set (#1–7, #13, E1) is pure Python and runs in today's engine image.
   The heavy set (#8–12) needs the lab image. A cheap method that ties an expensive one wins (J6).
4. **Runs offline on about 3k keys in a nightly Job.** Anything that needs one deep model per key is out.

## The methods

### #1 mean ± kσ (today)
- **Why chosen:** the method to beat. Every other method is judged on the same minutes at the same
  false-alarm budget against it.
- **Fit to our problem:** it is what runs in production, so the lab must reproduce the engine's own alerts
  (parity test, task 62) before any comparison is trusted.

### #2 Negative-binomial tail (Poisson when var ≤ mean)
- **Family:** disease-surveillance statistics.
- **Why chosen:** NB is the standard model for overdispersed counts. It needs only the mean and variance,
  which the profile already stores, so it is a drop-in replacement for σ.
- **Fit:** our keys are counts. For M4 (0–2/min) the tail of a count distribution is far from a
  bell curve: σ is too tight and alarms on single errors. NB gives a correct tail probability at
  every volume, from `*total` to a sparse ERROR template.

### #3 #2 with Farrington-style outlier clipping at rebuild
- **Family:** surveillance (Farrington algorithm, UK health agency).
- **Why chosen:** it isolates one question: does removing past outliers from the history improve the band?
- **Fit:** the nightly rebuild takes the last 5 weekdays / 4 weekend days as they are. An incident last
  Tuesday inflates the variance and the band stays wide for a week, which hides the next incident.
  Clipping removes it before refitting.

### #4 Median ± k·MAD
- **Family:** robust statistics (used by Twitter's and Etsy's Skyline detectors).
- **Why chosen:** the robust family's representative; a few huge minutes cannot drag the median or MAD.
- **Fit:** per-minute data has bursts (retries, batch jobs) that pull a mean and σ up. MAD stays put,
  so it is the robust alternative for noisy M3 keys. Needs 7 days of per-minute counts (the recorder).

### #5 Error ratio (key ÷ `*total`, binomial test)
- **Family:** SRE practice (error rate, not error count).
- **Why chosen:** the simplest way to separate "more traffic" from "more failures".
- **Fit:** M2 and M5 follow traffic. When traffic doubles, errors double too and σ alarms. Testing the
  ratio against `*total` removes traffic-driven alarms; a real failure raises the ratio.

### #6 Poisson CUSUM
- **Family:** statistical process control (factory quality control).
- **Why chosen:** bands judge one minute at a time; CUSUM adds up small deviations over time.
  It is the only accumulator kept (it covers EWMA, Page-Hinkley and changepoint detection).
- **Fit:** catches a slow, small, persistent rise (a leak, a growing retry loop) that never crosses
  the band in any single minute. Score + threshold, no band.

### #7 168-hour weekly profile + VN holiday calendar
- **Family:** ops practice.
- **Why chosen:** the cheapest possible improvement to today's profile, with no ML. If it wins,
  the change to production is small.
- **Fit:** today's profile knows only weekday vs weekend. Real traffic has an hour-of-week shape
  (Monday morning ≠ Thursday morning) and Vietnamese holidays (Tết, 30/4, 2/9) where traffic falls
  and σ calls a drop. Holidays live in `config.yaml`. Needs 2 weeks of hourly history.

### #8 Holt-Winters / ETS
- **Family:** classic forecasting (statsmodels).
- **Why chosen:** the cheapest seasonal forecaster; it stands in for STL and SARIMA.
- **Fit:** M1–M3 have a smooth daily and weekly shape plus slow level changes (a service gains users).
  ETS follows the level, which a fixed 5-day profile cannot. If it matches Prophet, the simpler one wins.

### #9 Prophet
- **Family:** Meta's forecasting library.
- **Why chosen:** the user asked for it; it models trend, weekly and daily seasonality and holidays
  explicitly. It stands in for NeuralProphet and Greykite.
- **Fit:** the holiday term is exactly the Tết problem of M1/M2/M5. On company data the holiday benefit is
  only measurable once the history contains a holiday; until then it is checked on the laptop's
  synthetic holiday block.

### #10 LightGBM global quantile model
- **Family:** gradient-boosted trees (the approach that won the M5 forecasting competition).
- **Why chosen:** one model trained over all keys (features: hour, weekday, holiday, lags, key level),
  predicting quantiles that form a band.
- **Fit:** the only method that judges **new templates** (M6) from day one: it learns from similar keys
  instead of the key's own history. One model for ~3k keys also keeps the nightly Job cheap.

### #11 Chronos-Bolt (small)
- **Family:** time-series foundation model (Amazon), pretrained, zero-shot.
- **Why chosen:** the "new ML" representative; it stands in for TimesFM and Moirai.
- **Fit:** needs no training history, so it also covers M6 (a sample, it is the slowest method). The weights
  are baked into the lab image at build time, so it runs in the offline cluster.

### #12 Matrix Profile (STUMPY)
- **Family:** shape anomaly detection; top statistical method on the TSB-AD benchmark.
- **Why chosen:** it finds odd shapes, not odd values.
- **Fit:** some incidents keep every minute inside the band but break the pattern (a flat line where
  traffic should oscillate, a missing batch peak). Expensive, so it runs only on M1 and the top-200 M3 keys.
  It covers Isolation Forest, LSTM and Donut, which need one trained model per key.

### #13 PCA on the per-service template vector (Xu et al. 2009)
- **Family:** classic log-mining ML.
- **Why chosen:** the method from the paper that started log anomaly detection on template counts,
  which is exactly our data. Power iteration in pure Python, no packages.
- **Fit:** the only method that looks across keys. M7: the service total looks normal but the mix shifts
  (one template replaces another after a bad deploy). It also names the top contributing templates,
  which helps whoever reads the alert.

### E1 2-of-3 vote (#2, #6, best forecaster; #7 when no forecaster is present)
- **Why chosen:** tests whether combining a band, an accumulator and a forecast cuts false alarms
  without missing incidents. Computed from the others' outputs, no new model.
- **Fit:** the three see different failures (a value outside the band, a slow rise, a break from the
  expected shape). Requiring two to agree targets the false alarms that make people mute monitors.

## Coverage per monitor kind
| Kind | Candidates beside σ |
|---|---|
| M1 `*total` | #2, #3, #6, #7, #8, #9, #10, #11, #12, E1 |
| M2 `*error` | #2, #3, #5, #6, #7, #8, #9, #10, #11, E1 |
| M3 busy template | #2, #3, #4, #6, #8, #10, #12 (top-200), E1 |
| M4 sparse ERROR | #2, #3, #4, #6, #10, E1 |
| M5 `doc:` key | #2, #3, #5, #6, #9, #10, E1 |
| M6 new template | #10, #11 (sample) |
| M7 service mix | #13 |

M6 and M7 have the thinnest cover. If neither wins, the dropped table below says what to bring back.

## Dropped, and what covers each
| Dropped | Covered by | Why dropped |
|---|---|---|
| Poisson | #2 | special case of NB (#2 falls back to it) |
| Rolling quantile, SPOT | #4 | same robust-tail idea |
| EWMA, Page-Hinkley | #6 | same accumulate-small-shifts idea |
| EARS | #10 / #11 | short-baseline method, weaker than both |
| Changepoint (BOCPD) | #6 | detects the same persistent shifts |
| Burn rate | a later alarm-rule study | an alarm rule on top of a method, not a method |
| STL, SARIMA | #8 | ETS is cheaper with the same seasonal idea |
| NeuralProphet, Greykite | #9 | Prophet variants |
| TimesFM, Moirai | #11 | one foundation model is enough to test the family |
| Donut, LSTM, Isolation Forest, Half-Space Trees | #10 / #12 | one trained model per key is too heavy for ~3k keys |
| Robust PCA, chi-square mix test | #13 | same composition question |
| Log-sequence models (DeepLog, LogBERT) | – | judge event order, not counts; our engine keeps counts only |
