# Baseline bake-off: test alerting methods on real data, judge them, choose per monitor kind

Status: **approved for build** (2026-10-26). Plan: `docs/plans/2026-10-26-baseline-bakeoff.md`.
Mockup (2 boards: Evaluation, Calibrate): https://claude.ai/artifact/EXqQaUkx4aAZTsEmj1A2be.
Research behind the method choice: 40 candidates surveyed (statistics, control charts, forecasting,
ML, foundation models, log-sequence models), cut to the roster below. Why each method was chosen and how it fits our problem: `docs/specs/2026-10-26-bakeoff-methods.md`.
Everything here is lab-only: **live alerting does not change**. Using a chosen method in production is a
separate, later spec.

## Context
Today `judge()` (`logai2/owner/baseline.py`) alerts when a minute's count is above `mean + kσ`
of a (service, key, daytype, hour) profile. The profile is rebuilt nightly from the last
5 weekdays / 4 weekend days (`jobs/commit.py`), plus floors, a `*total` drop rule and 2/3/3
hysteresis (`owner/detect.py`). The user wants to test old industrial and new ML methods on
real data and pick a method per kind of monitor.
The full 40-method catalogue was too much, so it is cut to **13 methods + 1 ensemble**:
one or two per family, chosen to cover every monitor kind.

**Constraints from the user:**
- Real data must **never leave the company servers** (no copy to a personal laptop).
- Analysis runs only on real data collected from the company's Elasticsearch.
- **The company run injects nothing and labels nothing** (decided 2026-10-26 evening). It only measures
  what each method predicts and whether its band fits the real counts. Everything that needs planted
  incidents (Tests A and C) runs on the laptop.
- Qwen 3.5 (~120B, company-hosted) is available but **not used** in the bake-off (no labels, no summary).
- The company cluster can **only pull images from Docker Hub**. It has no internet: no pip, no Hugging Face.
  Everything (packages, Chronos weights) must be **inside the image**, built on the laptop and pushed
  like today's `congdock123/logai2:v1`.
- The laptop gets **its own full deployment** on the user's own data (local ES fed by the replayer). It runs alongside the company one as rehearsal and adds Test C (real incidents injected into the local ES). Company data still never goes to the laptop.
- The user wants a new **Evaluation** page where **every method draws its line/band** over the real
  counts, to see what fits best and to choose and calibrate a method per monitor kind.

## Monitor kinds
| Kind | Example | Character |
|---|---|---|
| M1 | `*total` | high volume, daily/weekly shape, holidays, drops matter |
| M2 | `*error` | medium volume, follows traffic |
| M3 | busy INFO/WARN template | like M1, noisier |
| M4 | sparse ERROR template | 0–2/min |
| M5 | `doc:` key | sum of linked templates |
| M6 | new template | < 1 day history |
| M7 | service mix | template vector per service |

## The trimmed roster
| # | Method | Family | Runs on | Deps | Why it stays |
|---|---|---|---|---|---|
| 1 | mean ± kσ (today) | control | all keys | none | the method to beat |
| 2 | Negative-binomial tail (Poisson when var ≤ mean) | surveillance stats | all keys | none | fits counts; uses stored mean/var |
| 3 | #2 with Farrington-style outlier clipping at rebuild | surveillance | all keys | none | past incidents stop widening the band |
| 4 | Median ± k·MAD | robust stats (Twitter, Skyline) | all keys | none | robust tail on per-minute data |
| 5 | Error ratio (key ÷ `*total`, binomial test) | SRE practice | M2, M5 | none | removes traffic-driven alarms |
| 6 | Poisson CUSUM | SPC | all keys | none | catches slow, small, persistent rises |
| 7 | 168-hour weekly profile + VN holiday calendar | ops practice | M1, M2 | none (calendar in config) | real weekly shape + Tết |
| 8 | Holt-Winters / ETS | classic forecasting | M1–M3 | statsmodels | cheap seasonal forecast |
| 9 | **Prophet** | Meta forecasting | M1, M2, M5 | prophet | trend + holidays; the user asked for it |
| 10 | LightGBM global quantile model | ML (M5 competition) | all keys incl. M6 | lightgbm | one model for all keys, so it handles cold start |
| 11 | Chronos-Bolt (small) | foundation model | M1, M2, sample of M6 | torch + model weights | zero-shot, new ML |
| 12 | Matrix Profile (STUMPY) | top statistical on TSB-AD | M1, top-200 M3 | stumpy | odd shapes the bands miss |
| 13 | PCA on the per-service count vector (Xu 2009) | classic log ML | M7 | none (power iteration) | composition changes |
| E1 | 2-of-3 vote: #2, #6, best forecaster | ensemble | M1–M5 | none | computed from the others' outputs |

**Core set** = #1–#7, #13, E1. It needs no new packages, so it runs in the **existing engine image**.
**Heavy set** = #8–#12. It needs the lab image (see Images).

**Dropped, and what covers each** (revisit only if a monitor kind ends up weak):

| Dropped | Covered by |
|---|---|
| Poisson | #2 |
| Rolling quantile, SPOT | #4 |
| EWMA, Page-Hinkley | #6 |
| EARS | #10 / #11 |
| Changepoint (BOCPD) | #6 |
| Burn rate | a later alarm-rule study |
| STL, SARIMA | #8 |
| NeuralProphet, Greykite | #9 |
| TimesFM, Moirai | #11 |
| Donut, LSTM, Isolation Forest, Half-Space Trees | #10 / #12 (per-key training is heavy) |
| Robust PCA, chi-square mix test | #13 |

## Where everything runs (data stays inside the company)
```
company ES ─> engine ─> recorder ─> lab_minute_counts ─┐   (company Postgres)
                                    hourly_counts, Review, templates ─┤
                                                                       ▼
                 lab Job in the company cluster: run.py --test B ──> lab_runs / lab_results / lab_episodes / lab_bands
                                                                              │
                                                                              ▼
                                         Evaluation page /#evaluation (company network): lines, bands, fit scores, choices
laptop: its own data only (replay + synthetic world), Tests A, B and C; never company data
```

- **Recorder** (flag `lab.record: true`, in the engine): saves each `ClosedMinute.keys`
  (`logai2/owner/counts.py`) to Postgres as **one row per key-hour holding 60 per-minute counts**:
  `lab_minute_counts(service, key, day, hour, counts int[60])`.
  - Written in the owner's existing flush, once per closed hour, so it is exactly-once.
  - ~0.3–0.6 GB for 28 days at 3k keys.
  - The retrain re-read can backfill 7 past days from ES.
  - Temporary: turn it off and drop the table after the bake-off, unless the winner itself needs per-minute history.
- **Runner** (`scripts/lab/run.py --test B --methods ... --days 28`): runs **inside the company
  cluster** as a k8s Job, reading the real Postgres with a **read-only role**.
  - `--test A` and `--test C` are refused with `--site company` (exit with an error), and the CronJob
    manifest runs `--test B` only.
  - It writes results back into lab tables only: `lab_runs`, `lab_results`, `lab_episodes`.
  - No file leaves the cluster.
  - **Core set:** can also run in the engine pod: `kubectl exec deploy/logai2-engine -- python scripts/lab/run.py --test B --methods core`.
  - **Heavy set:** needs the lab image.
  - The Job has its own CPU/memory limits (e.g. 4 CPU / 8 Gi) and runs off-peak, so the engine is not slowed.
- **Report:** the new **Evaluation page** (`/#evaluation`, see its own section below), opened on the company network.

## Two deployments: laptop and company (same code, same images, same page)
| | Laptop (docker compose) | Company (k8s) |
|---|---|---|
| Data | the local stack's ES, fed by the replayer (`full_node97.log` at 1×); your own data, so it may stay on the laptop | company ES; never leaves the company |
| Runs | engine + web + recorder; lab as a compose service: `docker compose --profile lab run --rm logai2-lab python scripts/lab/run.py --test B` | engine + web + recorder; lab as the nightly k8s Job |
| Tests | A, B **and C** (below) | **B only** (fit; nothing injected) |
| Labels | none | none |
| Role | can each method **detect** planted incidents (A, C); tooling, run time, page | does each method **fit** real traffic: **the decision** per monitor kind, with the laptop's detect check as a gate |

### Test C (laptop only): real incidents written into ES, with known times
- `scripts/incident_test.py run` already injects bursts through the replayer's API. It only calls the
  replayer over HTTP and never changes the logai-engine stack, as today.
- **Every run's injection log becomes ground truth automatically:** it is loaded as confirmed incident
  windows (service, template, start, end). This is the only place where incidents pass through the
  **whole real path** (ES → reader → Drain3 → counts → recorder → methods) with exact times.
- **What it checks:**
  1. **Test A realism.** The same shapes and sizes are injected in memory (Test A) and in ES (Test C).
     If a method's recall differs by > 10 points between the two, Test A's injector is mis-modelling
     something, and that is fixed before Test A is used for the laptop detect check (J7).
  2. **Pipeline parity:** an alert a method raises in the lab replay matches what the engine would raise on the same minutes.
  3. **Lead time** against σ on real ES incidents.
- **Schedule:** a small incident set every few hours at 1× (e.g. the `--quick` set). It is
  recorded in `lab_incidents` with source `test-c`.

### How the two are compared
- **Same settings on both:** each run stores its code version, image tag and knob grids (J8), so the
  laptop and company runs use the same settings.
- **"Cross-check" panel on the Evaluation page:** a JSON summary of a run's **aggregate metrics only**
  (no log lines, no template text, no counts) can be exported from the company page and imported on the
  laptop, and vice versa. Both rankings are then shown side by side. It is metrics only; whether even that
  may leave the company is your call. Without it, compare by reading both pages.
- **Laptop → company import feeds the detect gate (J7).** The laptop's summary holds its own data's
  aggregate Test A recall per method × kind, so nothing leaves the company. Imported via
  `POST /api/eval/detect`; without an import, the column shows "not checked".
- **The two sites answer different questions.** The company ranks **fit** on real traffic; the laptop
  checks **detection** on planted incidents. A fit ranking from the laptop is only a rehearsal: if it
  differs from the company's, the company wins, shown as "laptop ranks X first; company ranks Y".
- **Laptop data, checked 2026-10-26** (replayer `/status` and `/openapi.json`, local ES, `agents.md`;
  nothing in the logai-engine stack was changed):
  - **Sources:** two, `full_node96.log` and `full_node97.log`. Each is **one day, 00:00–16:07 (~16 h)**,
    ~1.8M events, **one service `recharge`** (host node96/97 is metadata only), ~316–342 templates.
  - **No loop:** `/replay/start` takes `source`, `start`/`end` as clock times and `speed` 0–100×. It
    plays once and goes idle. `/replay/inject` adds `pattern`, `per_minute` ≤ 3000, `minutes` ≤ 60, `label`.
  - **Right now:** the local ES has **no indices** (empty), the replayer is idle, and `logai2-engine`
    is not running.
- **What that means for the laptop's plain replay** (the synthetic world below removes these limits):
  - **Weekly and daily seasonality cannot be judged** (one day, one service). Laptop runs label weekly
    profile, Prophet, ETS, LightGBM and Chronos as "seasonality not testable here". Their laptop numbers
    check that they run, and their cost, not how well they fit.
  - **Only 1 key each for M1 and M2,** so the laptop has no per-kind ranking for those.
  - **16:07–24:00 has no source data.** The lab marks hours with no replayed data as **"no data"**
    (excluded), not as a drop to zero. Otherwise every method "alerts" on a fake drop each evening.
  - **Getting several laptop days:** start a 1× replay every morning by cron (alternate node96/node97)
    for 7–14 days. That gives repeated but not identical days: enough for tooling, Test C and run
    times, still no weekly pattern.
  - **No speed-ups for scoring:** replays at > 1× change per-minute counts, so they are used only for
    quick tooling smoke tests and never for scoring.
### Synthetic world on the laptop: 7 / 14 / 28 days without waiting
Built from the laptop's **own templates and documents**, it removes the replay limits above
(one 16 h day, one service, no weekly pattern), so the laptop can run the full Tests A, B and C at any length.

**Step 0, once: learn from the real replay.**
- One **overnight 1× replay** of node97 (00:00–16:07) through the laptop's logai2. This mines the
  templates (Drain3, ~316) and records the per-minute counts of every template (`lab.record`).
- Documents come from the seeded corpus. Links are made as usual (Templates page, linker).
- The output is a **world file** (`lab/world.json`): per template its text, level, document link,
  hourly shape over the day, and a library of real per-minute residuals (30-min blocks of
  `log(count / smooth)`).
- **Hours 16:07–24:00** (not in the source) get a shape mirrored from the 00:00–08:00 ramp, flagged as synthetic.

**The model** (a scenario YAML sets every number):

    count(service, template, minute) = shape_t(time of day) × day(d) × trend(d) × scale(service)
                                       × real residual block (bootstrapped)  + planted incidents

| Part | Default | Why |
|---|---|---|
| Services | the template set cloned into **6 services** with different scales (0.3×–3×), a random 70–90 % template subset each, and ±30 min phase shift | gives M1/M2 several keys |
| Day factor | weekdays 1.0 ± 5 % jitter, Sat 0.75, Sun 0.65 | weekly pattern to learn |
| Holidays | one 3-day holiday block at 0.4 (Tết-like), placed in the test half | holiday handling becomes testable |
| Trend | +2 % per week | trend handling |
| Noise | **bootstrapped real residual blocks** (autocorrelated, heavy-tailed as in real logs); NB(k) only as a fallback | keeps the generator from favouring any one method's assumptions |
| New templates | 3 per service per day, mutated text, some ERROR | M6 cold start |
| Planted incidents | random schedule from a catalogue (spike, drop, ramp, level shift, ERROR burst on a doc-linked template, mix swap, outage to 0), 3 sizes, ≥ 3 h apart per key | exact truth → `lab_incidents` (source `synthetic`) |

**Two run modes** (`scripts/lab/synth.py scenario.yaml --mode counts|engine --days 14`):

| Mode | What runs | Time for 14 days | For |
|---|---|---|---|
| `counts` | writes `lab_minute_counts`, `hourly_counts`, `service_hours`, `day_status` directly | ~1–2 min | iterating on methods, Tests A/B/C |
| `engine` | the **real Engine** (Reader, Drain3, Owner, recorder, σ detection, nightly commit) on the injected clock against a generated source, reusing `scripts/stress_pipeline.py`'s `GenSource`, `Clock`, pretraining and throwaway schema | ~30–60 min (at the real ~31 ev/s per service, 14 days × 6 services would be ~225M events; `scale` 0.15 brings it to ~34M, ≈ 30 min at ~20k ev/s) | proves the whole path, σ's real `alert_changes` for the parity test, recorder correctness |

- **Volume cap for `engine` mode:** `scale` multiplies all rates (default 0.15 at 6 services). Tests stay at
  realistic per-key levels for M1–M4 while staying around 20k ev/s × ≤ 1 h. `counts` mode needs no cap.
- **One schema or database per scenario:** each world goes to its own target, so a synthetic run never
  mixes with the laptop's real replay data. The run bar shows the site as `laptop · synth: <scenario>`.
- **ES path:** still covered by the real replay + `incident_test.py` (Test C). An optional `--mode es` that
  bulk-writes backdated docs into a separate local index is left out until needed.

**How synthetic results are used:**
- **Valid there:**
  - every method runs at 7/14/28 days;
  - run times and memory;
  - Test A's in-memory injector vs truth planted through the engine (Test C at full scale);
  - **whether forecasters learn a known weekly pattern, trend and holiday** (now testable on the laptop);
  - the Evaluation page with realistic volumes.
- **Not valid:** the final ranking. The world is still a model, so the company run decides. The page labels
  synthetic runs "synthetic world: sanity check, not a decision".
- **Generator-bias check:** a method that wins on synthetic data but loses badly on company data is flagged,
  and the generator is adjusted (e.g. residuals, day factors).

**Tests:**
- `tests/lab/test_synth.py`:
  - same seed gives the same world;
  - planted incidents appear exactly in the counts;
  - the day factors and holiday are visible in the hourly totals;
  - `engine` mode on a 1-day tiny world gives the same per-minute counts as `counts` mode (pretrained Drain3 maps generated lines back to their templates, ≥ 99.9 %).

### Laptop build/run additions
- **Compose:**
  - a `logai2-lab` service (`profiles: [lab]`, `Dockerfile.lab`);
  - `LOGAI2_LAB_RECORD=true` for the engine;
  - an optional cron line on the host for a nightly lab run.
- **Incident log loader:** `scripts/lab/load_incidents.py reports/incident-<run>` writes `lab_incidents`.
- **Tests:** `tests/lab/test_load_incidents.py` (an incident_test run directory becomes the right windows).
- Same Evaluation page; the run bar shows "laptop" or "company" as the run's site.

## Images (Docker Hub only, nothing downloaded in the cluster)
- **Engine/web image `congdock123/logai2:vN`:** unchanged dependencies. It gains the recorder, the core
  methods (pure Python) and the Evaluation page.
- **Lab image `congdock123/logai2-lab:v1`:** `FROM` the app image, plus `requirements-lab.txt`
  (numpy, pandas, statsmodels, prophet with its prebuilt cmdstan, lightgbm, stumpy, torch CPU-only,
  chronos-forecasting).
  - **Chronos-Bolt-small weights are downloaded on the laptop at build time** and `COPY`ed into the
    image (~200 MB, Apache-2.0).
  - Offline flags are set: `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`.
  - Expected size ~2–2.5 GB.
- **Build:** on the laptop, `docker build -f Dockerfile.lab` then `docker push`. The cluster pulls it for the lab Job.
  - Code only, no company data, goes into either image.
  - Use a private Docker Hub repository if the code should not be public.
- **Startup check:** the lab Job verifies at start that every method's package and weights load. If one
  fails, that method is marked "unavailable" in the run and the others still run.

## Performance estimate (lab Job, 4–8 cores)
**Assumptions:**
- 10 services, 3,000 keys, of which ~120 are M1/M2/M5 series.
- 28 days of per-minute counts (121M points, mostly zeros) and 90 days hourly.
- Walk-forward with a daily refit for forecasters.

| Method | Estimate per full run |
|---|---|
| Core set #1–#7, #13, E1 (pure Python, engine image) | ~10–30 min (no numpy, so slower than vectorised) |
| #8 ETS | ~3 min |
| #9 Prophet | ~10–15 min on 8 cores |
| #10 LightGBM | ~10–20 min |
| #11 Chronos-Bolt small, CPU | ~10–40 min |
| #12 Matrix Profile | ~20–40 min |

- **Whole run:** ~1–2.5 h, at night. `--methods` and `--days` give quick runs. The company run is
  Test B only, so it has no injection passes and is at the lower end.
- **Measured:** each method records its real fit and score time in `lab_results`.

## Test A: injected incidents (laptop only; detection, exact ground truth)
Injections are added **in the lab's memory** to the laptop's recorded or synthetic counts. Test A never
runs on company data (`run.py` refuses it with `--site company`).

1. **Injections** into real, clean series. Per monitor kind, ~200 per type at random keys and times, at 3 sizes:

   | Type | Shape | Kinds |
   |---|---|---|
   | spike | ×2/×3/×5 for 10 min | M1–M5 |
   | drop | to 50/20/0 % for 15 min | M1 only |
   | ramp | +30/50/100 % over 2 h | M1–M5 |
   | level shift | +50/100/200 %, lasting a day | M1–M5 |
   | burst | 0 → 5/20/50 per min | M6 |
   | mix swap | two templates' rates swapped, total unchanged | M7 |

2. **Same alarm path for every method:** verdict per minute → `detect.step` (2/3/3) → episodes.
3. **Per event:**
   - **Hit:** ALERTING starts within 15 min of the window start.
   - **False alarm:** an episode overlapping no window (± 15 min).
4. **Equal false-alarm rate:** each method's one sensitivity knob is tuned on weeks 1–2 to the same
   budget: 1 per 100 key-days, and 1 per service-week for M1. Methods are then compared on weeks 3–4
   (equal ARL0, compare ARL1, as SPC does).
5. **Metrics per method × kind × type × size:** recall, delay (median/p90), held-out false alarms, cost.

## Test B: fit on real data (what it predicts, does its band fit)
Nothing is injected and nothing is labelled. Every number comes from the real counts alone.

| Criterion | How it is measured | Good if |
|---|---|---|
| **Expected-value error** | MASE of the expected line vs real count; < 1 beats "same minute last week" | lower |
| **Band fit: coverage** | on **shared normal minutes** (one set for all methods, see the judging protocol), the share of real counts inside the band | near the target (99.5 %) |
| **Band fit: sharpness** | median band width ÷ expected value | narrower at equal coverage |
| **Band fit: interval score** | Winkler score (width + 2/α × distance outside) | lower; **main ranking** |
| Alerts | episodes per 100 key-days, and per service-week for M1, at the chosen knob | ≤ budget |
| Stability | flaps: episodes < 5 min, or re-alert within 30 min | ≤ 10 % |
| Coverage of keys | share of keys it could judge | ≥ 95 % (M6 excluded) |
| Cost | measured fit time, µs per key-minute, memory | within budget |
| Band | produces `band_low/high` | required for M1–M5 |

- **Review `exclude` hours** (outages people already marked) are left out of the shared normal minutes, so
  a band is not punished for missing an outage. Nothing else needs confirming.
- **Why a detect check is still needed:** a fit score alone can favour a method that bends towards whatever
  happens, incidents included. So a method is adopted only if it also passes the **laptop detect check**
  (Test A recall at the budget not worse than σ by more than 5 points; see J7).

## Timeline: how long to collect before a test is useful (ES keeps ~7 days)
The recorder keeps per-minute counts in Postgres beyond ES's 7 days. Day 0 is backfilled from ES
(the last 7 days), and every day after that is recorded live. Hourly history (`hourly_counts`, up to 90 days)
already exists for as long as the engine has run; forecasters and profiles train on that.

| Day (from turning the recorder on) | Per-minute data | Run | Useful for | Confidence |
|---|---|---|---|---|
| 0 | 7 days (backfill) | quick: all methods, Test B tune and test on the same 7 days (split by keys) | fit charts, coverage/interval score, alerts per method, catching gross misfits | low: one weekend |
| 7 | 14 days | Test B tune week 1 / test week 2 | **first credible ranking** per kind | medium |
| 21–28 | 28 days | full run as planned (2 weeks tune, 2 weeks test, 4 weekends) | **the decision** (choose per kind) | good |

### Dated timeline for the current company system (it has recorded since Fri 23 Oct 2026)
Every day without action, ES drops one more day that cannot be recovered. That gives the order:

| When | Action | History after it |
|---|---|---|
| **Now (Mon 26 Oct), no new code** | Run a **Retrain re-read of the last 7 days** (already built: tasks 58–59), if that version is deployed in the company | hourly from ~Tue 20 Oct |
| **First build task: the recorder only** (small; the rest is built while data collects). Deploy ~Fri 30 Oct, then backfill 7 days through the re-read lane | lane writes `lab_minute_counts` too | per-minute from ~Fri 23 Oct |
| ~Fri 30 Oct: quick run | – | hourly ~10 d, per-minute 7 d: σ/NB (weekend still young), MAD, CUSUM, Chronos, PCA. Forecasters show "not enough history" |
| ~Tue 3 Nov | – | hourly 2 weeks: weekly profile, ETS, Prophet, LightGBM reach their minimum |
| **~Fri 6 Nov: first credible ranking** | 14-day run (tune 1 week, test 1 week) | per-minute 14 d |
| **~Fri 20 Nov: decision run** | 28-day run | per-minute 28 d, hourly ~31 d: forecasters at "better" |

- **Build order changes:** the recorder (and the re-read lane writing it) becomes task 1 and is
  deployed first. The methods, lab Job, image and Evaluation page follow during the collection weeks,
  so nothing waits on data.
- **If the re-read is not run now:** the hourly history starts on 23 Oct and every date above slips by ~3 days.
- **No holiday** falls in this window. Holiday benefit is checked only with the synthetic holiday day;
  keep recording through Tết (~6 Feb 2027) for a real check.

- **Run time per run:** ~20–40 min at 7 days, ~45–90 min at 14 days, ~1–2.5 h at 28 days.
- **Training history the methods need:** a minimum (below it the method shows "not enough history" and is skipped, not failed), and a better amount.

  | Methods | Minimum | Better |
  |---|---|---|
  | σ / NB | 5 weekdays + 4 weekend days of hourly data | – |
  | Weekly profile, ETS | 2 weeks of hourly | 4 weeks |
  | Prophet | 2 weeks of hourly | ≥ 4 weeks |
  | MAD, Matrix Profile | ≥ 7 days of per-minute | – |
  | LightGBM | ≥ 2 weeks of hourly | – |
  | Chronos | none (zero-shot) | – |
- **Holidays:** the holiday benefit of #7/#9 can only be measured on company data if the hourly history
  contains a holiday. Until then it is checked only on the laptop's synthetic holiday block.
- **Storage over time:** 28 days of per-minute counts ≈ 0.3–0.6 GB. Recording can run longer (e.g. through Tết) by raising `lab.days`.

## Qwen
Not used in the bake-off (decided 2026-10-26 evening): no episode labels, no report summary. The verdict
comes only from the numeric rules below.

## Judging protocol (precise rules, the same for every method)

### J1. Which keys are which monitor kind (fixed per run, computed from the tune window)
| Kind | Rule |
|---|---|
| M1 | key `*total` |
| M2 | key `*error` |
| M5 | key `doc:*` |
| M3 | a template with mean ≥ 2 per minute in its busy hours |
| M4 | an ERROR/WARN template with mean < 2 per minute |
| M6 | any template while it is < 1 day old (a key leaves M6 when it turns 1 day old) |
| M7 | the template vector of each service |

INFO templates under 2 per minute are not scored (no product use). Counts per kind are shown on the run bar.

### J2. Units
- **Minute verdict:** `high` or not, from the method at its chosen knob.
- **Episode:** the minute verdicts through `detect.step` (2/3/3), from leaving NORMAL to returning to NORMAL. The same rule applies to every method.
- **Shared normal minutes:** all minutes minus Review `exclude` hours (and, on the laptop, minus every
  planted or injected window ± 15 min). The **same set for every method**. Never "outside this method's
  own alerts", which would let a noisy method hide its misses.

### J3. Formulas
Company and laptop (Test B):
- **Coverage** = share of shared normal minutes where `low ≤ count ≤ high`. The target is 99.5 %; it passes if within 99.0–99.9 %.
- **Interval score (Winkler, α = 0.005)** = mean over shared normal minutes of `(high − low) + (2/α)·(low − y)₊ + (2/α)·(y − high)₊`, divided by the key's mean count, then averaged over keys and reported relative to σ (σ = 1.00).
- **Sharpness** = median `(high − low) / expected`.
- **MASE** = MAE(expected, count) ÷ MAE(seasonal naive, count). Seasonal naive = the same minute 7 days earlier, or 1 day earlier when there is no 7-day history (the report says which).
- **Alert rate** = episodes per 100 key-days (per service-week for M1, per service-day for M7), counted on shared normal minutes.
- **Flap share** = episodes < 5 min long, or starting < 30 min after the previous one on the same key, ÷ episodes.

Laptop only (Tests A and C):
- **Recall** = injections whose window gets an episode start within [start, start + 15 min] ÷ injections. Reported per type × size; the headline is "medium" size.
- **Delay** = episode start − injection start, over hits (median, p90).

### J4. Fair tuning (equal coverage, compare sharpness)
1. **Windows:** tune = the first half of the per-minute data, test = the second half. On the 7-day quick run, keys are split in half instead (time is too short).
2. **Knob grid** per method (5–7 values: k, α, z, quantile, CUSUM h).
3. **Band methods:** on the tune window, pick the knob whose coverage is closest to 99.5 %. Every method is then
   compared at the same nominal coverage, so a narrower band (lower interval score) is a real win, not a looser one.
4. **Score-only methods** (CUSUM, Matrix Profile, PCA): no coverage, so pick the most sensitive knob whose alert
   rate on the tune window is within the budget (1 per 100 key-days; 1 per service-week for M1).
5. **σ is tuned the same way.** "Recalibrated σ (k = …)" is also a valid outcome.
6. Every knob value's numbers go to `lab_sweeps` for the Calibrate view. The test window is never used for tuning.

### J5. Injections (laptop only)
- **Relative size:** each injection is relative to the key's own expected level at that minute (×2, ×3, …), so busy and quiet hours get fair sizes.
- **Placement:** random times in the test window, never overlapping a planted incident. On one key, injections are ≥ 3 h apart.
- **Passes:** 5 passes (different seeds) so injections do not touch each other. Each pass reruns only the scoring; fits are cached.
- **Laptop tuning for Test A:** the knob is tuned to the alert-rate budget (equal false alarms), then recall is compared.

### J6. Confidence with little data
- **Day-block bootstrap:** resample whole days of the test window (1,000 times). This gives 90 % intervals for the interval-score ratio vs σ, MASE and the alert rate.
- **"Fits better than σ"** = interval-score ratio ≤ 0.90 **and** the upper end of its 90 % interval < 1.00.
- **Simplest within the interval wins:** methods whose intervals overlap the leader count as tied. The tie goes to the cheapest, simplest method: core set before heavy set, then lower cost. A heavy model must be clearly better to earn its image and runtime.
- **Verdict strength per run:**

  | Strength | Data needed |
  |---|---|
  | **exploratory** | < 14 days |
  | **provisional** | 14 days |
  | **final** | ≥ 28 days, ≥ 2 test weekends |

  The page shows the strength next to every verdict.
- **Stability:** a final choice needs the same leader (or a tie that includes it) in two consecutive runs, e.g. ~6 Nov and ~20 Nov.

### J7. Decision tree per monitor kind (company run)
1. **Hard gates (fail any → not suitable):**
   - band present (M1–M5);
   - cost within budget;
   - coverage 99.0–99.9 % on the test window;
   - alert rate within budget on the test window;
   - flaps ≤ 10 %;
   - **laptop detect check:** on the latest laptop run of the same code version, Test A recall (medium size, at the budget) is not worse than σ by more than 5 points. A method with no laptop run shows "detect: not checked" and cannot be adopted.
2. **Rank by interval score** (relative to σ). Ties (J6) go to the simpler method; then lower MASE.
3. **Service check:** the leader must not have a worse interval score than σ on more than 20 % of services. Otherwise "mixed: depends on service" is shown with the per-service table.
4. **Outcome:**
   - **adopt candidate**: fits better than σ (J6) and passes every gate (strength from J6);
   - **σ stays**: nothing fits better than σ, or σ recalibrated wins;
   - **mixed**: see the service check.

### J8. Reproducible
Each run stores:
- the code version, image tag and site;
- the data window, seed and knob grids;
- the kind assignment.

The same inputs give the same numbers. "Compare with run" shows what changed: data, code or knobs.

## Tests to create
| Test | Where | What it proves |
|---|---|---|
| `tests/lab/test_methods.py` | laptop, synthetic | each method catches a synthetic spike/drop/ramp and stays quiet on clean Poisson data; heavy methods are skipped if not installed |
| `tests/lab/test_inject_score.py` | laptop | injector + event scoring + band-fit metrics give the expected numbers on a hand-made case |
| `tests/lab/test_replay.py` | laptop, test Postgres | Tests A and B run end to end on a small **synthetic** dataset (`scripts/stress_pipeline.py` generator); the same seed gives the same results |
| `tests/lab/test_parity.py` | laptop, test Postgres | method #1 in the runner reproduces the engine's own `alert_changes` on the same minutes |
| `tests/lab/test_run.py` | laptop | `run.py --site company --test A` (or C) exits with an error and writes nothing |
| `tests/test_recorder.py` | laptop, test Postgres | the recorder writes exactly the closed hours, once, and changes nothing else |
| `tests/test_web_eval_view.py` | laptop, test Postgres | the eval API returns runs, scoreboard, series (bands at a chosen knob, 5-min vs 1-min), episodes and sweeps from synthetic lab rows; choice POSTs save; a company run returns no Test A columns; the page never touches live tables |
| Lab image check | laptop | `docker run congdock123/logai2-lab:v1 python scripts/lab/run.py --check` loads every package and the Chronos weights **with the network off** (`--network none`) |
| In-cluster smoke | company cluster | `run.py --test B --methods 1,2,6 --days 3` finishes and the Evaluation page draws all three methods' lines for one key |

## Files
- **New**
  - `scripts/lab/`: `run.py`, `load.py` (read-only DB reads), `inject.py`, `score.py`, `methods/`, `synth.py`, `world.py`, `load_incidents.py`.
  - `requirements-lab.txt`, `Dockerfile.lab` (weights baked in). These are the **only** new dependencies, and they are lab-image only; the app image's `requirements.txt` does not change.
  - `k8s/logai-lab-job.yaml`; a `logai2-lab` compose service (`profiles: [lab]`).
  - Migration `010_lab.sql`: `lab_minute_counts`, `lab_runs`, `lab_results`, `lab_episodes`, `lab_bands`, `lab_sweeps`, `lab_choices`, `lab_incidents`.
  - The Evaluation page: `logai2/web/eval_view.py` (API, following `baseline_view.py`), `static/js/views/evaluation.js`, a nav entry in `static/index.html`.
- **Touched:** `logai2/owner/owner.py` (recorder behind the flag) and `logai2/config.py` (`LabConfig`).
- **Reused:**
  - `judge`, `Baseline` (`owner/baseline.py`) and `step` (`owner/detect.py`).
  - `daytype` (`timeutil.py`).
  - The retrain re-read; the stress generator for synthetic tests.

## Evaluation page (`/#evaluation`): see every method's line, choose, calibrate
A new nav section **"Evaluate"** with one page, built like the other views: `views/evaluation.js`,
ECharts (already vendored at `static/vendor/echarts.min.js`, wrappers in `static/js/charts.js`) and the key
picker from the Baseline page (`static/js/keypicker.js`). It reads only lab tables, so live
alerting is untouched.

### 1. Run bar (top)
- Pick a run (`lab_runs`: site, date, tests run, days, methods, status, duration, unavailable methods).
- A **company** run is Test B only; its page shows no Test A columns or curves.
- A "compare with run" picker, to see a recalibrated run against the previous one.

### 2. Scoreboard (per monitor kind M1–M7)
- One table per kind: a method per row, with the Test B criteria as columns (MASE, coverage, interval score, sharpness, alerts, flaps, cost) and a pass ✔ / fail ✖ badge per criterion.
- One more column, **laptop detect**: ✔ / ✖ / "not checked", from the latest laptop run of the same code version (J7).
- On a laptop run, the Test A columns (recall, delay) are shown too.
- Rows are sorted by the decision rule, with the leader marked.

### 3. Fit chart (the main view): each method draws its line
- **Pick:** service + key (key picker), or "jump to" from the scoreboard, plus a time range.
- **Default: small multiples.** One panel per method, stacked, all sharing the x-axis (zoom one, all follow). Each panel shows:
  - the **real count** line (same in every panel);
  - the method's **expected line** (dashed);
  - its **band** (shaded low–high);
  - red marks where the real line is outside the band;
  - bars for its alert episodes.
- **Panel corner:** that key's numbers for the shown range: coverage %, interval score, median width, alerts.
  The best panel for the range is outlined.
- **Overlay mode:** pick 2–3 methods and draw their bands on one chart, for a close comparison.
- **Score-only methods** (CUSUM #6, Matrix Profile #12, PCA #13): the panel shows the score line and its
  threshold instead of a band. PCA is shown per service, with the top contributing templates listed.
- **Background shading:**
  - Review `exclude` (red) and `keep` (green) hours;
  - laptop runs only: the injected or planted windows with type and size, plus a toggle to show the series with and without the injection.
- **Timelines:** a swimlane under the chart, one row per method, showing where each method alerted for this key. A per-service version shows all keys.

### 4. Calibrate
- **Method picker:** the leader for the chosen monitor kind is selected first; the list holds every method that passes
  the gates, plus σ. Switching method only reads its stored sweep; nothing is retrained on the page. Each method opens
  on its auto knob (closest to 99.5 % coverage on the tune window, J4), marked "auto".
- **Per method, a sensitivity sweep** precomputed by the run (5–7 knob values, e.g. k = 2.5…5,
  α = 1e-2…1e-5, CUSUM h). Charts:
  - **Test B:** coverage, interval score and alerts per day vs knob, with the target coverage and the alert budget as lines.
  - **Test A (laptop runs only):** recall vs false alarms per 100 key-days, one curve per method, with the budget as a
    vertical line.
- **Picking a knob** redraws the fit chart's band for that knob (the bands for each sweep value are stored, see storage below).
- **Choose:** per monitor kind, "choose method + knob" saves to `lab_choices` (who, when, run, metrics
  at the time).
  - This is the outcome of the bake-off. It **does not change live alerting**.
  - Using a choice in production is a later, separate task, after your confirmation.

### What the run stores for drawing (sized so Postgres stays small)
| Data | Resolution | Which keys | Approx. size |
|---|---|---|---|
| Real counts | 1 min | all | already in `lab_minute_counts` |
| Band (low, centre, high) per method at the chosen knob | 5 min (min low / max high in the bucket) | all M1, M2, M5 and top-200 M3 keys (~300) | 300 keys × 28 d × 288 × 13 methods ≈ 31M points ≈ 0.4 GB as `real[]` arrays per (run, method, key, day) |
| Same, 1 min | 1 min | ± 2 h around every episode | small |
| Bands for the sweep knob values | 5 min | only the ~300 drawn keys, the last 7 days | ≈ 0.3 GB |
| Test A injected series (laptop runs) | 1 min | 20 samples per type × kind | small |
| Other keys (long tail) | none drawn | only metrics, plus episode windows | – |

- Old runs are deleted by "delete run", or automatically after N runs (default keep 5).
- Opening a key that has no stored band shows "not drawn in this run", plus its metrics.

### API (read-only, plus choices)
| Endpoint | Returns |
|---|---|
| `GET /api/eval/runs` | runs |
| `GET /api/eval/scoreboard?run=` | the scoreboard tables |
| `GET /api/eval/series?run=&service=&key=&from=&to=&methods=&knob=` | real counts + per-method expected/band/score + episodes + shading |
| `GET /api/eval/sweep?run=&kind=` | the sweep curves |
| `POST /api/eval/choices` | save a choice |

Documented in `docs/WEB_UI.md` when built.

## Decisions (answered 2026-10-26)
1. A **read-only DB role** and a nightly k8s **Job** (lab image, ~4 CPU / 8 Gi) may be added, so the full roster runs, heavy methods included.
2. The lab image's Docker Hub repository is set up by the user. The plan only produces `Dockerfile.lab` and the image.
3. The page goes in a **new "Evaluate" nav section** holding Evaluation.

4. **The laptop** runs its own full deployment on the user's data (replay + synthetic world); company data never goes to it.
5. **The retrain build** (tasks 51–60) is deployed to the company on 2026-10-26 afternoon. A 7-day re-read is run right after, to keep hourly history from ~20 Oct.
6. **Build order:** the recorder (task 61) first, deployed as soon as it is merged. Every other task is built while data collects.
7. **Company = fit only** (2026-10-26 evening): nothing injected (no Test A, no Test C) and nothing labelled (no Qwen judge, no human labels, no precision, no known-incident list). The CronJob runs `--test B`. Detection is checked only on the laptop, and that check is a gate for adoption.

### Mockup
Updated 2026-10-26 evening (version 6) to the company fit-only run: two boards (Evaluation, Calibrate),
verdict strength, 90 % interval on the interval score, site in the run bar, the laptop detect column; the
Episodes board, Qwen summary and label columns are gone. Task 69 builds it as drawn.

## Verification
- `.venv/bin/pytest -q` green, including `tests/lab/` (synthetic data only).
- Company cluster:
  1. Turn on `lab.record`; engine lag is unchanged.
  2. After a few days, the in-cluster smoke run shows on the Evaluation page.
  3. After 28 days, the full Test B run decides per kind (with the laptop detect gate).
