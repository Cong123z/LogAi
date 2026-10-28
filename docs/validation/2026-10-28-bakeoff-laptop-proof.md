# Bake-off lab: laptop proof (task bk17)

Date: 2026-10-28. Branch `task/bk17-laptop-proof`. Everything here was measured on the laptop with synthetic data. No real
data, no company data. All numbers come from runs listed in section 2; nothing is estimated unless the text says
"extrapolated".

## 1. What this document answers

The lab compares 14 detection methods against the one that runs today (sigma, "mean plus k standard deviations"). Before it
goes to the company, this proof runs the whole lab end to end and asks four questions:

1. Does every method run, and in what time and memory?
2. Does the decision logic produce sane verdicts?
3. Do the forecasters learn the patterns planted in the data (weekly cycle, holiday)?
4. Is each kind of planted incident caught by the method meant for it?

Short answers: (1) yes, the full roster runs in the lab container; a 28-day Test A + Test B takes 2 h 6 min and peaks at
1.4 GB. (2) Yes, no NaN, a verdict per kind M1-M7, M6/M7 no longer pending. (3) Yes for ETS and Prophet on holidays and
weekends; the weekly profile does not beat sigma. (4) Partly: spike, drop, level shift on the error rate and new-template
burst are caught; ramps, sparse error templates (M4), the template-mix swap (M7) are not, for reasons that are mostly
test-design limits listed in section 9.

## 2. Setup

- Machine: laptop, Docker Desktop VM. Postgres 127.0.0.1:5433 (database `logai`, only new `lab_s_*` schemas written).
  During the long runs `elasticsearch` and `logai2-web` were stopped and started again afterwards.
- Heavy methods (ETS, Prophet, LightGBM, Chronos-Bolt, Matrix Profile) run only in the lab container:
  `docker compose --profile lab run --rm -v <worktree>/scripts:/app/scripts:ro -e PYTHONPATH=/app:/app/lab/timing logai2-lab ...`.
  The image was not rebuilt: the worktree `scripts/` is mounted read-only over the image's copy so the container runs the
  fixed code. (The image would need a rebuild before anyone relies on it without the mount.)
- Measurement helper `lab/timing/sitecustomize.py` (loaded through `PYTHONPATH`) wraps `fit` and `day` of every method and
  prints seconds and peak memory at exit. "Wall" is the whole `docker compose run`. "RSS" is the process peak; the
  container's cgroup `memory.peak` is shown in brackets (it is sometimes lower, sometimes higher, because it counts page cache and
  excludes some mapped memory).
- Worlds (built by `python -m scripts.lab.synth --mode counts`, from `lab/world.json` = 180 templates, 3 services, 274 keys
  per service). Scenario files `lab/scenarios/w7.yaml`, `w14.yaml`, `w28.yaml`: each has N + 14 days so every method has
  history before the N-day window (ruling R1); runs use `--days N`. Build time (wall, RSS): w7 15.7 s / 290 MB, w14 21.8 s /
  394 MB, w28 33.5 s / 636 MB. Planted in the data: weekday / Saturday / Sunday factors, a 3-day holiday (factor 0.4, w28 only,
  2026-10-18..20), a 2-hour gap, trend 3 %/week, new templates, and synthetic incidents (spike, drop, ramp, level shift, mix swap).
- Order per world: Test A, then Test B (Test B's decision for M6/M7 reads the latest Test A run).
- Engine-mode world: `lab/scenarios/engine_14.yaml` (14 days; the last 7 go through the real Engine on a simulated clock).
- Scripts used for the numbers are in `lab/proof/` (run, interval score by day group, recall tables, decisions, parity,
  floor reachability). Raw outputs: `/home/cong/Documents/logai2/.team/bk17-out/` (git-ignored).
- Schemas kept for you to open on the Evaluation page: `lab_s_w7`, `lab_s_w14`, `lab_s_w28`, `lab_s_engine_14_engine`
  (and the first-attempt worlds `lab_s_default_7`, `_14`, `_28`).

## 3. How to read the lab (for a reader who knows the system but not the lab)

**Kinds.** Every counted key is judged as one of seven monitor kinds: M1 `*total` (drop detection), M2 `*error`, M3 busy
templates (mean at least 2 per minute in busy hours), M4 sparse ERROR/WARN templates, M5 document keys, M6 new templates
(first 24 h), M7 the template mix of a service. The decision is made per kind.

**Tests.**
- *Test B (fit)* walks forward day by day: each method is fitted on earlier days only and produces a band
  (expected, low, high) for every minute of the next day. Nothing is injected. It measures how well the band fits normal
  minutes.
- *Test A (detect)* copies the counts in memory, plants incidents (spike, drop, ramp, level shift, burst, mix swap in three
  sizes, five seeded passes), and asks whether the method alerts within 15 minutes of the start. It measures detection.
- *Engine mode* sends the same kind of world through the real Engine and compares its alerts with the lab's sigma
  (parity): this proves the lab's sigma is the live sigma.

**Numbers.**
- *Coverage*: share of normal minutes inside the band. Target 99.5 %; the gate accepts 99.0-99.9 %.
- *Interval score* (Winkler): width of the band plus a penalty for every minute outside it; lower is better. Averaged per minute.
- *Ratio vs sigma*: method interval score divided by sigma's, with a 90 % bootstrap interval over days. "Fits better" means
  ratio at most 0.90 and the interval's upper end below 1.00.
- *Knob*: the sensitivity setting of a method (k for sigma, tail probability for nb, ...). For each method it is tuned so every
  method is compared at the same footing: band methods at coverage closest to 99.5 %, score methods at equal alert budget.
- *Alert budget*: at most 1 episode per 100 key-days (for M1: 1 per service-week). An episode is a run of flagged minutes that
  reached ALERTING in the live alert state machine.
- *Recall*: share of injected incidents of a size (medium unless stated) that raised an alert within 15 minutes. *Delay*:
  minutes from the incident start to the first alert (median, p90).
- *Gates*: band, cost, coverage, alert rate, flaps (at most 10 % of episodes), detect (Test A medium recall not worse than
  sigma's by more than 5 points). A method that fails a gate is "not suitable".
- *Verdict strength*: exploratory (under 14 days), provisional (14 to 27 days, or fewer than 2 test weekends), final
  (28 days, 2 or more test weekends).
- *Decision per kind*: "sigma stays", "adopt candidate" (a method fits better and passes every gate), "no method", or
  "pending" (no Test A run yet).

## 4. What changed between the first runs and the final runs, and why

The first runs (worlds `lab_s_default_*`, built with the window = the whole world) exposed lab problems, fixed on this
branch, each with a failing test first and one commit.

| # | Problem found in the first runs | Fix (commit) | Effect |
|---|---|---|---|
| F7 | Test A stayed `running` with no recall rows: its closing writes were in an uncommitted transaction. | one `commit` (`b5118d6`) | Test A runs finish ok. |
| R1 | The world started at the window start, so forecasters (14 days of history) and minute methods (7 days) had no fitted tune day: 7 methods had `skipped` rows with empty metrics. | worlds of N + 14 days, `--days N` (`b797040`); `load_service` keeps the minute arrays of the 7 days before the window (`4cf66fb`) | ets, prophet, lgbm, weekly (28 d) and mad, mprofile, pca (all worlds) now have scoreboard rows. |
| R2 | Spike/ramp/level shift were injected into any template, including INFO ones that the live rule (D4) can never flag high, and bursts were INFO templates: recall about 0 by construction. | upward incidents only on keys with a floor; bursts are ERROR (`9cde33a`) | recall is now measurable for M2 and M6; M3 loses all its injections (no eligible key), see B20. |
| F9 | Test A chose the numerically lowest knob; for tail-probability grids that is the least sensitive. nb recall was 0.07 (drop) / 0 (spike). | `tune` follows the grid order (`58ea334`) | nb spike recall on M2: 0.00 to 1.00. |
| R3 | A tie at recall 0 named a "leader" (M6 "nb burst recall 0.00 vs sigma 0.00"). | no leader, note "no method detects" (`84fa91f`) | M7 reads "no method detects". |

Before / after on the 28-day world (medium recall at the chosen knob):

| | first run (`lab_s_default_28`) | final (`lab_s_w28`) |
|---|---|---|
| M1 drop, sigma / nb | 0.20 / 0.07 | 0.40 / 0.40 |
| M2 spike, nb / sigma | 0.00 / 0.50 | 1.00 / 0.58 |
| M3 (all methods) | 0.00, 122 injections (INFO templates) | no injection possible (n = 0) |
| M6 burst, sigma / nb | 0.00 / 0.00 | 1.00 / 0.00 |
| M7 pca | 0.00 | 0.00 |
| Forecasters on the scoreboard | none (skipped) | ets, prophet, lgbm, weekly |
| M3 decision | adopt candidate nb | sigma stays: "nb fits better but is not verified: detect" |

The last row matters: the first run's "adopt nb for M3" rested on a Test A that could not detect anything. With honest injections
M3 has no detection evidence at all on this world, so the lab (correctly) refuses to adopt.

Not fixed, recorded in `docs/BUGS.md`: B20 (injections on sparse keys cannot reach the count floor; ramp hit window too short),
B21 (pca cannot see a swap), B22 (no doc keys in the worlds), B23 (a window needs history before it).

## 5. Time and memory (all 14 methods, lab container)

Spec 13 targets: all methods, 28 days, Test B under 2.5 h and under 5 GB peak. Measured:

| world | test | wall | RSS (cgroup) |
|---|---|---|---|
| 7 d (21-day world) | A | 1394 s (23.2 min) | 1128 MB (1665) |
| 7 d | B | 459 s (7.7 min) | 1094 MB (706) |
| 14 d (28-day world) | A | 2828 s (47.1 min) | 1240 MB (853) |
| 14 d | B | 765 s (12.8 min) | 1201 MB (820) |
| 28 d (42-day world) | A | 5763 s (96.1 min) | 1436 MB (1054) |
| 28 d | B | 1805 s (30.1 min) | 1424 MB (1040) |

The target is met with a wide margin (30 min against 2.5 h; 1.4 GB against 5 GB). Per-method seconds (fit + scoring) at
28 days:

| method | Test B | Test A |
|---|---|---|
| sigma | 126 | 599 |
| nb | 70 | 198 |
| nb_clip | 68 | 194 |
| mad | 53 | 215 |
| cusum | 24 | 97 |
| vote | 28 | 98 |
| pca | 14 | 34 |
| ratio | 10 | 14 |
| weekly | 0.7 | 3.5 |
| ets | 105 | 146 |
| prophet | 16 | 54 |
| lgbm | 340 (fit 214 + scoring 126) | 870 (fit 217 + scoring 653) |
| chronos | 75 | 113 |
| mprofile | 380 | 1160 |
| other (container start, loading, injection, scoring, tuning, bootstrap, database; wall minus the rows above) | 495 | 1968 |

- Test A / Test B time at 28 days: 3.2 x for all methods; 3.7 x for the nine core methods (1452 s against 394 s of method
  time). The spec target "Test A under 2 x Test B for core methods" is **not met**: five injected copies are walked, and only
  the fit is shared. It is a time target, not a correctness one.
- The expensive methods are mprofile (29 % of Test B method time, 31 % of Test A), lgbm (fit dominates in B, scoring in A) and
  sigma itself (the Python band loop).
- Synthetic world build and engine run: see sections 2 and 7.

## 6. Results per world

Every table row is from the final runs (`lab_s_w7`, `lab_s_w14`, `lab_s_w28`, Test B after Test A).

### 6.1 Decision per kind

| kind | w7 (exploratory) | w14 (provisional) | w28 (final; M5 provisional) |
|---|---|---|---|
| M1 `*total` | adopt candidate: nb fits better (ratio 0.79) | adopt candidate: nb (ratio 0.80) | sigma stays: weekly does not fit better than sigma (ratio 0.957, interval 0.913-1.000) |
| M2 `*error` | adopt candidate: nb | adopt candidate: nb (ratio 0.75) | sigma stays: weekly does not fit better (ratio 1.004) |
| M3 busy templates | adopt candidate: nb (ratio 0.63) | adopt candidate: nb (0.63) | sigma stays: nb fits better (0.625) but "not verified: detect" (no Test A evidence) |
| M4 sparse ERROR/WARN | sigma stays: no method passed the gates | adopt candidate: mad (ratio 0.38) | sigma stays: nb does not fit better (0.943); mad (0.498) fails flaps and coverage |
| M5 document keys | sigma stays (no data, B22) | same | same |
| M6 new templates | sigma stays: lgbm burst recall 1.00 vs sigma 1.00 | sigma stays: lgbm 1.00 vs sigma 1.00 | sigma stays: lgbm 1.00 vs sigma 1.00 |
| M7 template mix | no method: no method detects | no method | no method |

Reading: at 7 and 14 days nb "wins" M1-M3 on interval score because those windows hold no holiday and few weekends; at 28
days, with a holiday and 7 weekend days, nb's band collapses on the holiday (coverage 0.83) and the verdict returns to sigma
for M1/M2 (a method must also pass the detect gate and the coverage band). M4's mad win at 14 days disappears at 28 days
(flaps, coverage 0.9991 above the 99.9 % ceiling). Short windows give unstable verdicts, as the strength labels say.

### 6.2 Fit (Test B), w28, every method that reached the scoreboard

Ratio = interval score relative to sigma (below 1 is a tighter band); coverage at the tuned knob. "gates" lists failed gates.

| kind | method | ratio (90 % interval) | coverage | failed gates |
|---|---|---|---|---|
| M1 | sigma (k 3.0) | 1.00 | 0.991 | none |
| M1 | weekly | 0.957 (0.913-1.000) | 0.995 | none |
| M1 | ets | 0.937 (0.828-1.083) | 0.990 | coverage |
| M1 | prophet | 1.012 (0.897-1.146) | 0.990 | detect, coverage |
| M1 | nb / nb_clip | 1.176 (0.823-1.711) | 0.962 | flaps, coverage, alert rate |
| M1 | lgbm | 1.441 | 0.973 | detect, coverage |
| M1 | chronos | 1.600 | 0.973 | detect, coverage |
| M2 | sigma (k 5) | 1.00 | 0.9995 | coverage (above band) |
| M2 | weekly | 1.004 (0.952-1.066) | 0.996 | none |
| M2 | nb / nb_clip | 0.755 (0.664-0.913) | 0.990 | coverage |
| M2 | ets / prophet | 1.080 / 1.452 | 0.978 / 0.965 | flaps, coverage, alert rate |
| M2 | lgbm / chronos | 2.769 / 1.407 | 0.995 / 0.964 | detect, alert rate / flaps, coverage, alert rate |
| M3 | sigma (k 5) | 1.00 | 0.9955 | none |
| M3 | nb / nb_clip | 0.625 (0.620-0.631) | 0.998 | none (detect not checked) |
| M3 | ets / lgbm / mad | 2.05 / 2.14 / 2.96 | 0.954 / 0.961 / 0.940 | coverage |
| M4 | sigma (k 5) | 1.00 | 0.988 | coverage |
| M4 | nb / nb_clip | 0.943 (0.914-0.966) | 0.992 | none |
| M4 | mad | 0.498 (0.400-0.623) | 0.9991 | flaps, coverage |
| M4 | lgbm | 8.27 | 0.964 | coverage |
| M6 | sigma (k 5) | 1.00 | 0.705 | coverage |
| M6 | nb / lgbm / chronos / mad | 0.983 / 0.395 / 0.686 / 0.031 | 0.717 / 0.631 / 0.763 / 0.9999 | coverage (and detect for nb, chronos, mad) |
| M7 | pca (knob 0.995) | n/a (score method) | n/a | none |

Score-only methods (cusum, vote, mprofile) have no band and no interval score; they are judged on alert rate and detection.
Their rows read "band: fail" by design and they cannot be adopted for M1-M5.

### 6.3 Detection (Test A), medium size, chosen knob

w28 (n = injections over 5 passes x 3 services):

| kind (n) | incident | best methods | sigma | notes |
|---|---|---|---|---|
| M1 (45) | drop | ets 0.47, sigma / nb / nb_clip / weekly 0.40 | 0.40 | cusum, lgbm, mprofile 0 |
| M2 (65) | spike | cusum / nb / nb_clip / ratio / vote 1.00; weekly 0.92 | 0.58 | lgbm 0.42 (late tune) |
| M2 | level shift | cusum 0.50, ratio 0.50 | 0.00 | all others 0 |
| M2 | ramp | none | 0.00 | all methods 0 (B20: 15-minute hit window on a 60-minute ramp) |
| M3 | all | n = 0, no eligible key | | the detect gate cannot be checked for M3 |
| M4 (49) | spike / ramp / shift | none | 0.00 | peak never reaches the floor (0 of 49, B20) |
| M6 (45) | burst | lgbm 1.00, sigma 1.00 | 1.00 | nb, nb_clip, mad 0.00; chronos 0.07 |
| M7 (45) | mix swap | none | | pca 0.00 (B21) |

w7 and w14 give the same shape with fewer fitted methods (w14: M2 spike 1.00 for nb, sigma, weekly, ets, lgbm; M6 burst 1.00
for sigma and lgbm; M1 drop nb 0.67, sigma 0.47). Full tables: `lab/proof/checks_a.py <schema>`.

### 6.4 Why nb, mad, chronos miss the new-template burst

M6 is judged by the "born" rule: a template in its first 24 hours that reaches the count floor alerts (sigma implements it).
nb, nb_clip and mad have no history for the new key, so they cannot place a band and stay silent; lgbm predicts zero for an
unknown key and flags the burst. That is a method property, not a bug, and it is why sigma (the existing rule) is the M6 baseline.

## 7. Engine mode and parity

World `engine_14` (one service, 35 keys): 994,537 log lines, the last 7 days through the real Engine on a simulated clock:
416.7 s, 944 alert changes recorded (the first attempt with `tiny.yaml` produced 0 alerts, so it proved nothing; this scenario
is built to alert). Test B with the core methods on it: 21.4 s, 92 MB.

Parity rule of task 4 (lab sigma at k = 3 against the engine's `alert_changes`, except in the 12-minute window around each
02:00 commit): lab 902 transitions, engine 900, **899 identical**, **0 mismatches outside the commit windows**, 4 inside
them (allowed: at most 10 per window). The lab's sigma is the live sigma on this world too.

## 8. Checks from the brief (w28)

1. **Do the forecasters learn the planted pattern?** Interval score relative to sigma on the same days (lower is better), each
   method at its tuned knob, w28 (`lab/proof/checks.py`):

   | | M1 weekend (7 days) | M1 holiday (3 days) | M2 weekend | M2 holiday |
   |---|---|---|---|---|
   | ets | 0.75 | 0.78 | 0.70 | 0.52 |
   | prophet | 0.79 | 0.84 | 0.70 | 0.56 |
   | weekly | 0.89 | 1.03 | 1.01 | 1.03 |
   | nb (no calendar) | 0.74 | **2.91** (coverage 0.83) | 0.73 | 1.10 |

   ETS and Prophet beat sigma on weekends (ratio 0.70-0.79) and on the holiday (0.52-0.84): they learn the weekly cycle and the
   holiday dip. The weekly profile helps only a little on weekends (0.89) and not on the holiday. nb, which has no calendar,
   is as good as them on weekends but loses badly on the holiday. On weekdays ets / prophet are no better than sigma (1.06 /
   1.16 on M1), which is why they do not win overall (M1 ets ratio 0.937 with an interval that crosses 1).
2. **Each incident type caught by its designed method?** spike: nb / nb_clip 1.00 on M2 (mad serves M3/M4 where injections
   cannot reach the floor); drop: sigma 0.40, nb 0.40; level shift: cusum 0.50 (ets 0); ramp: nobody (B20); burst: sigma 1.00;
   mix swap: pca 0.00 (B21). So 3 of 6 designed pairs are confirmed; ramp and mix swap are limits of the tests, not evidence
   that the methods fail.
3. **Decision rows sane?** 219 result rows in each Test B run, none with NaN or infinity; a verdict for every kind M1-M7; M6
   and M7 decided from Test A ("sigma stays", "no method: no method detects"), none pending.
4. **Evaluation page API on the 28-day run** (`scripts/run_web.py`, port 5598, DSN search path `lab_s_w28`; stopped afterwards):
   `/api/eval/runs` 200; `/scoreboard?run=3` 200 (24.7 kB, every kind with decision); `/keys?run=3&kind=M3` 200
   (65 keys with their methods); `/series?run=3&service=synth1&key=*total&kind=M1&...` 200 (1.7 MB, nine methods with points,
   knob and episodes) and for an M3 key 200 (583 kB); `/sweep?run=3&kind=M2` and `kind=M3` 200 (33 kB and 25 kB). `series` and
   `sweep` need `kind`; without it they answer 400 "kind is required" (as designed).

## 9. Known items

(a) **Test A wrote no results (earlier seeded `mid` world).** Reproduced on the 7-day world: per-service rows were written,
    but the `scope = 'all'` rows and the run status were lost. Cause: `test_a.finish` committed, then read (opening a new
    transaction), then wrote inside it as a savepoint; closing the connection rolled it back. Fixed (F7).
(b) **mprofile with fewer than two prior days.** The thresholds come from the left matrix profile of the history without its first
    day (`hist[1440:]`); with under two prior days the list is empty, the score stays 0 and nothing flags. In practice this
    does not occur: `fit` returns None unless 7 prior days exist, and with the 7-day minute history now loaded every window day
    has 7. Before the loader fix, mprofile (like mad and pca) did nothing in the first 7 days of any window, so it was `skipped`
    on 7- and 14-day worlds.
(c) **LightGBM at 3k keys.** Measured at 274 keys per service, 28-day Test B: 87 fits of 11 quantile models in 214 s (2.5 s
    per fit), scoring 126 s for 10,752 key-days (11.7 ms per key-day), process peak 1.4 GB. **Extrapolated linearly** (not
    measured) to 3,000 keys per service: about 27 s per fit and 16 min of scoring, 28 min per service for a 28-day Test B;
    memory unknown. LightGBM also failed most gates (coverage), so it is unlikely to reach a decision. Measure one real 3k-key
    service before the company run.
(d) **Prophet tail coverage.** Measured, M2, test part: knobs 0.98 / 0.99 / 0.995 / 0.999 / 0.9999 give coverage 0.847 / 0.882 /
    0.908 / 0.942 / 0.965 (ETS: 0.891 / 0.920 / 0.939 / 0.961 / 0.978). The tails never approach their nominal level and
    the last two knobs differ by 2 points: confirmed (BUGS B17).

Other findings: the lab loaded minutes only for the window (fixed, F10); score-only methods and band methods share one knob
rule (fixed, F9); M5 is never exercised (B22).

## 10. Plain-language conclusion per kind (synthetic data only)

- **M1 `*total` (drops).** Keep sigma. weekly is 4 % tighter but its interval crosses 1.0, so the lab does not call it better;
  ets is 6 % tighter with a wide interval and fails coverage. Drop detection recall is 0.40 for sigma, nb, weekly (0.47 for ets).
  Confidence: final on this world. What would change it: real traffic with stronger weekly structure than the synthetic one.
- **M2 `*error`.** Keep sigma. nb is 25 % tighter but does not satisfy the coverage band, and fails on holidays; weekly equals sigma.
  Spikes: nb and cusum detect 100 %, sigma 58 %. A real alert-quality win would need a Test C on real errors. Confidence: final
  on synthetic.
- **M3 busy templates.** nb's band is 37 % tighter than sigma's with excellent coverage (0.998), but the lab cannot verify detection:
  no injectable key exists on this world (INFO templates cannot alert, no busy ERROR template). Adopting needs Test A/C on a
  world or real data with busy ERROR/WARN templates. Confidence in "nb fits better": high; in "nb detects": none.
- **M4 sparse ERROR/WARN.** Keep sigma. nb is 6 % tighter (interval 0.914-0.966, passes the gates) but detection is untestable
  with the current injections (0 of 49 reach the floor, B20). mad looks tight (0.498) only because its band is wider than the
  data (coverage 0.9991) and it flaps.
- **M5 documents.** Not evaluated: no data (B22).
- **M6 new templates.** Keep the existing born rule (sigma): burst recall 1.00, delay 0. lgbm also scores 1.00 but costs far more,
  and nb / mad / chronos cannot see new templates.
- **M7 template mix.** No method detects a swap on these worlds (pca recall 0.00, B21). Keep M7 off; test it on real data
  before spending effort.
- **Forecasters.** ETS and Prophet learn the weekly cycle and the holiday (ratio 0.52-0.84 on holiday and weekend days) but are not
  better than sigma on weekdays; LightGBM and Chronos-Bolt are worse than sigma almost everywhere on this data and expensive.

## 11. Limits

- Synthetic only. The world's shapes come from 5 active hours of recording; amplitudes, dispersion and incident mix are
  made up. Verdicts here say whether the lab works, not which method is best for the company.
- No Test C (real incidents), no real recorded days (recording started today; `incident_test.py` was not run).
- Test A has the weaknesses in B20 and B21; its M3/M4/ramp/mix-swap rows must not be read as method quality.
- Only one world seed per length; no repeat to estimate the run-to-run spread beyond the bootstrap intervals.
- The company has about 3k keys per service; the laptop 274. Cost numbers are laptop numbers (the LightGBM figure is extrapolated).
- The lab image was not rebuilt: runs used the mounted worktree `scripts/`.

## 12. Ready for the company plan?

**Yes for the machinery, with three conditions.** The runner, storage, decisions, parity with the live sigma (899 of 899
matching transitions outside the commit windows) and the Evaluation API all work end to end, and the 14-method roster fits in
1.4 GB and 30 minutes for a 28-day fit. Before the company plan: (1) fix Test A injection sizing (B20) so M3/M4 and ramps
have meaning, and rebuild the lab image; (2) the company run needs at least window + 14 days of recording (B23); (3) time one
real 3k-key service for LightGBM and Matrix Profile (the two heavy hitters). Real-data Test B/C then decides the verdicts.

## 13. Reproduce

```
python -m scripts.lab.world --dsn postgresql://logai:logai@127.0.0.1:5433/logai
python -m scripts.lab.synth --mode counts --scenario lab/scenarios/w28.yaml --dsn ...        # lab_s_w28
lab/proof/runc.sh f28A A 28 lab_s_w28     # Test A, all methods, lab container (then B)
lab/proof/runc.sh f28B B 28 lab_s_w28
PYTHONPATH=. python lab/proof/decisions.py lab_s_w28 ; python lab/proof/checks.py lab_s_w28 ; python lab/proof/checks_a.py lab_s_w28
python -m scripts.lab.synth --mode engine --scenario lab/scenarios/engine_14.yaml --dsn ... ; python lab/proof/parity.py lab_s_engine_14_engine 7
```
