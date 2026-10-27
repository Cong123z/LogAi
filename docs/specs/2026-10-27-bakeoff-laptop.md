# Baseline bake-off on the laptop: detailed spec

Status: **approved** (2026-10-27). Plan: `docs/plans/2026-10-27-bakeoff-laptop.md` (tasks 0–17).
Parent design: `docs/specs/2026-10-26-baseline-bakeoff.md`. Methods: `docs/specs/2026-10-26-bakeoff-methods.md`.
Criteria: `docs/specs/2026-10-26-bakeoff-judging.md`. Mockup: https://claude.ai/artifact/EXqQaUkx4aAZTsEmj1A2be.

This spec says **what** the laptop build does and the exact contracts between its parts. The plan says in which
order and with which tests. Where this spec and the parent design differ, this spec wins for the laptop build.

## 1. Purpose and scope

**Purpose.** Compare 13 alerting methods + one vote against today's σ on per-minute log counts, with one judging
protocol, and choose a method and knob per monitor kind (M1–M7). The laptop build proves the whole chain on data
that may stay on the laptop, before anything runs in the company.

**In scope (laptop):**
- the recorder (per-minute counts) in the live engine and in the re-read lane;
- the lab: loader, kinds, 14 methods, walk-forward runner, scoring, tuning, bootstrap, decision;
- a synthetic world that gives weeks of history shaped like the laptop's real data;
- Tests A (in-memory injections), B (fit) and C (real incidents through ES);
- the Evaluation API and page;
- a lab image for the heavy methods and a compose service for it.

**Out of scope (later plan):** the k8s CronJob, the read-only DB role, the company run and its smoke test, any
change to live alerting. A chosen method moves into the engine only through a separate spec.

**Invariants (must hold at every task):**
1. Live alerting output is byte-for-byte unchanged with the recorder on or off (`alert_state`, `alert_changes`,
   `baseline`, `hourly_counts`, `service_hours`).
2. Lab code writes only `lab_*` tables in `public`, and the tables of its own synthetic schema.
3. `requirements.txt` is unchanged; heavy packages exist only in the lab image.
4. The lab never talks to Elasticsearch and never reads log text other than template texts.

## 2. Laptop environment

| Item | Value |
|---|---|
| CPU / RAM / disk | 16 cores, 12 GB RAM, 143 GB free |
| Docker | all containers in one VM (qemu) limited to 7.75 GB |
| Current DB | 1 service (`recharge`), 180 templates, 2 days (26–27 Oct), schema at migration 009 |
| Test DB | the same Postgres container, `127.0.0.1:5433` |
| App venv | Python 3.11, no numpy (core methods are pure Python) |

**Container modes** (the user starts and stops containers per mode):

| Mode | Running | Stopped |
|---|---|---|
| Building code | postgres (+ web to check pages) | bge-m3, linker, elasticsearch, replayer, engine |
| Recording real data (after tasks 1–2) | postgres, elasticsearch, replayer, engine | bge-m3, linker |
| Test C | postgres, elasticsearch, replayer, engine, web | bge-m3, linker |
| Heavy lab runs | postgres, lab container (memory limit ~5 GB) | bge-m3, linker, elasticsearch when not recording |
| Linking docs (outside the bake-off) | bge-m3, linker | – |

bge-m3 alone uses 5.3 GB of the VM's 7.75 GB; it must be off during heavy lab runs.

## 3. Data flow

```
                    (laptop only)                                 (any site)
replayer ──> ES ──> engine Reader ──> Drain3 ──> OpenMinutes ──> owner flush ──┐
 incident_test.py injects ─┘                                                  │ one transaction
                                                                               ▼
               public: hourly_counts, service_hours, templates, day_status, review_exclusions,
                       template_doc_links, lab_minute_counts (recorder)
                                                                               │
synth.py --mode counts ──> lab_s_<scenario>: the same tables, generated ───────┤
synth.py --mode engine ──> real Engine on a generated source ──> lab_s_<scenario>
                                                                               ▼
                 scripts/lab/run.py: load (one service at a time) → kinds → fit/score walk-forward
                                     → episodes → metrics → tune → bootstrap → decide
                                                                               ▼
                 public: lab_runs, lab_results, lab_episodes, lab_bands, lab_sweeps, lab_incidents
                                                                               ▼
                 Evaluation page (/#evaluation) via /api/eval/*  ──>  lab_choices
```

## 4. Recorder

### 4.1 What is stored
`lab_minute_counts(service, key, day, hour, counts int[60])`, partitioned by `day`, one row per key-hour with the
60 per-minute counts of that **local** hour (slot = local minute 0–59).

Keys stored: every key of `ClosedMinute.keys` (templates, `*total`, `*error`, `*warn`, `*info`, `:unparsed`) plus
`*minutes`. Not stored: `doc:` keys (§4.4), and rows whose 60 values are all 0.

### 4.2 `*minutes`: observed vs zero
For each service and each closed minute, `*minutes[slot] = 1`. A minute with `*minutes = 0` was not observed (engine
down, service not yet seen, catch-up skipped). Its hourly sum equals `service_hours.n_minutes`. The lab marks
unobserved minutes as `-1` and never scores them.

### 4.3 Exactly-once: additive per flush (decision D1)
- The owner keeps a pending dict `(service, key, day, hour) → [60 ints]` of the minutes **closed since the last
  flush**, and upserts it in the flush transaction, adding element-wise:
  ```sql
  INSERT INTO lab_minute_counts (service, key, day, hour, counts) SELECT * FROM tmp_lab_minute_counts
  ON CONFLICT (service, key, day, hour) DO UPDATE SET counts = ARRAY(
    SELECT a + b FROM unnest(lab_minute_counts.counts, EXCLUDED.counts) WITH ORDINALITY AS t(a, b, i) ORDER BY i)
  ```
- Why not "write the hour when it closes": after a crash the engine resumes from the oldest **open** minute. Closed
  minutes of the current hour are already in `hourly_counts` and are not re-read, so an in-memory hour would lose them.
- The pending dict is cleared only after the commit (as every other pending row in `Owner`); a failed flush retries
  it whole.
- Rows for days older than `today - lab.days` are not written (no partition exists for them).

### 4.4 `doc:` keys (decision D3)
The Detector builds `doc:` keys per minute from `template_doc_links` (sources `manual`, `inherited`, `corpus`, `llm`).
The lab does the same in its loader, with the links of the day of the run. Known limit: a link changed during the
window is applied to the whole window.

### 4.5 Re-read lane
`LaneHour` carries `minutes: dict[key, list[60]]` built in `Lane._close` with the same rules. `_flush_lane` deletes the
hour's `lab_minute_counts` rows with the other replaced tables and inserts the lane's rows. A lane hour and the same
docs read live give identical rows (tested).

### 4.6 Retention, size, config
- `lab.record` (env `LOGAI2_LAB_RECORD`, default false; laptop compose sets true), `lab.days` (28), `lab.holidays`.
- The daily commit drops `lab_minute_counts` partitions older than `lab.days`.
- Size: 60 ints per key-hour ≈ 0.3–0.6 GB per 28 days at 3k keys; laptop (180 templates) ≈ 30 MB.

## 5. Lab data model

### 5.1 `ServiceData` (one service, one window)
| Field | Content |
|---|---|
| `days` | contiguous local days of the per-minute window |
| `minutes[key][day]` | `array('i', 1440)`, `-1` = unobserved |
| `observed[day]` | from `*minutes` |
| `hourly[(key, day, hour)]` | `(sum, sumsq)` from `hourly_counts`, back to `window_from - history_days` |
| `n_minutes[(day, hour)]` | from `service_hours` |
| `committed` | days with `day_status.committed` |
| `excluded` | Review `exclude` hours `(day, hour)` |
| `levels`, `first_seen` | per template |
| `docs` | `doc:<id>` → linked template ids |

`doc:` arrays = element-wise sum of the linked templates' arrays (`-1` stays `-1`).

### 5.2 Memory (decision D7)
One `ServiceData` at a time. 4 bytes per minute: a 300-key service × 28 days × 1440 ≈ 48 MB. Method outputs for the
service (expected/low/high/score per knob) are reduced to metrics and the drawn subset before the next service.

### 5.3 Monitor kinds (J1)
| Kind | Rule (computed on the tune part) |
|---|---|
| M1 | `*total` |
| M2 | `*error` |
| M5 | `doc:*` |
| M3 | template, mean ≥ 2/min over its busy hours (hours where it is > 0) |
| M4 | ERROR/WARN template, mean < 2/min |
| M6 | template **minutes** while `minute − first_seen < 1440`; after that, the template's base kind |
| M7 | the service's template vector (one "key" per service) |
| not scored | INFO < 2/min, `*warn`, `*info`, `*minutes`, `:unparsed` |

## 6. Method contract

### 6.1 Interface
```python
Spec(name, number, label, kinds, knobs, band, heavy, cost, min_hour_days, min_minute_days)
Method.fit(sd, day) -> model | None          # history strictly before `day`; None = not enough history
Method.day(model, sd, key, day, knobs) -> {knob: DayOut(expected, low, high, score, high_flag)}  # 1440 slots each
```
- **Walk-forward:** one `fit` per method per test day, then `day()` for every key with all knobs at once.
- A method with too little history is `skipped` (not failed) for that kind; a method that raises is `failed` for that
  service with the error recorded; the run continues.

### 6.2 Same alert shape for every method (decision D4)
A minute is **high** when:
- band methods: `count > high` and the live floor is met (`cfg.floors`: ERROR 5, WARN 10, by the key's level as in
  `judge`), or, for `*total` only, `count < low` with `expected ≥ drop.min_mean` (20);
- score methods (#6, #12, #13): `score > threshold(knob)` and the floor is met.
- A key whose level has **no floor** (INFO templates, and `*total`, whose level is INFO) is never high on the upper
  side, as in live `judge()`: `*total` alerts only through the drop rule, INFO templates never alert. Their bands are
  still scored for fit (coverage, interval score). (Ruling 2026-10-27, bk03.)
`apply_d4()` is the single implementation.

### 6.3 Episodes (decision D5)
Minute flags go through `detect.step` with the live `AlertConfig` (warm 2, alert 3, cool 3), from a fresh state at the
start of each scored part. An episode is a run that reaches ALERTING: start = its first high minute, end = the return
to NORMAL. Unobserved minutes do not advance the state.

### 6.4 σ in the lab equals σ in the engine
- Profile for day D = what `commit()` would build on D: last 5 committed weekdays / 4 weekend days **before D**,
  Review exclusions removed, `n_minutes > 0`, the `born` rule for `*warn`/`*info`; then the real `Baseline` and `judge`.
- Commit timing: minutes of D before `baseline.commit_time` (02:00) use D−1's profile.
- Parity gate: on an engine-driven world, lab σ at k = 3 gives the same transitions as `alert_changes`, except within
  10 minutes after each commit (the engine reloads at its first tick after the commit; window [commit − 2, commit + 10]
  because the engine may close 01:58–01:59 after its commit). Result (bk04): 64/64 transitions identical; an
  Owner-driven 12-day world (full baseline, `*total` drop, outage, weekend borrow, floors, doc levels): 82/82 identical.
- **Known lab-vs-live differences** (found in the bk04 reviews; none occurs in the synthetic worlds):
  1. Minutes the engine closes late (`historic_after_minutes`) or before `detection_enabled` are recorded but never
     judged live; the lab judges them (the recorder does not mark them).
  2. Live rebuilds by the `retrain_schedule` setting (time, weekdays, enabled) and on manual / re-read rebuilds; the
     lab assumes a daily rebuild at `commit_time`.
  3. A re-read replaces recorded counts; the lab judges the re-read counts, live judged the originals.
  4. A service with no committed day: live judges on the floor alone, the lab skips the day.
  5. Template levels and doc links: the lab uses those at load time; live uses the most severe level seen so far and
     the links of each minute (adversary test `test_level_escalation_parity`, xfail strict, documents it).
  6. Idle templates absent from a minute: live skips them, the lab judges 0 — identical while every floor ≥ 1.

### 6.5 The 14 methods (detail in the methods doc)
| # | Name | Kinds | Fits on | Band | Knob grid |
|---|---|---|---|---|---|
| 1 | σ | all | hourly, σ's days | yes | k 2.0–5.0 (6) |
| 2 | NB tail | all | hourly, σ's days | yes | tail p 1e-2–1e-5 (6) |
| 3 | NB clipped | all | as #2, outliers clipped | yes | as #2 |
| 4 | Median ± k·MAD | M3, M4 | 7 days per-minute | yes | k 3–10 (6) |
| 5 | Error ratio | M2, M5 | hourly shares | yes | tail p (6) |
| 6 | Poisson CUSUM | all | σ's expected | no | h 2–10 (6) |
| 7 | Weekly + holidays | M1, M2 | 2–4 weeks hourly | yes | k 2.0–5.0 (6) |
| 8 | ETS | M1–M3 | hourly, daily refit | yes | quantile (5) |
| 9 | Prophet | M1, M2, M5 | hourly + holidays | yes | interval width (5) |
| 10 | LightGBM quantile | all incl. M6 | all keys of the service | yes | quantile (5) |
| 11 | Chronos-Bolt | M1, M2, 20 % of M6 | last 7 days, zero-shot | yes | quantile (5) |
| 12 | Matrix Profile | M1, top-200 M3 | per-minute, window 60 | no | threshold (5) |
| 13 | PCA | M7 | per-minute vectors | no | Q quantile (5) |
| E1 | 2-of-3 vote | M1–M5 | #2, #6, best forecaster or #7 | no | – |

Hourly forecasters (#8–#11) predict per hour; per minute, expected = hourly forecast × σ's within-hour shape, and the
band is scaled the same way.

## 7. Synthetic world

### 7.1 Why
The laptop has 2 days of one service. Test B needs ≥ 14–28 days with weekly shape, weekends, a holiday and trend; the
methods' history needs are up to 4 weeks. The synthetic world is made **from the laptop's own templates and shapes**,
so it stays realistic and stays on the laptop.

### 7.2 World file (`lab/world.json`, git-ignored)
Per template: id, text, level, doc link, mean per local hour (missing replay hours 16:07–24:00 mirrored and flagged),
per-minute residual blocks (`count / hourly mean`, 60-minute windows), NB dispersion (fallback Poisson).

### 7.3 Scenario (`lab/scenarios/*.yaml`)
Seed; days; services (count, scale, subset of templates, phase shift); day factors (weekday 1.0 ± jitter, Sat, Sun);
holiday block (start, length, factor) placed in the test half; weekly trend; residual mode (bootstrap | nb); new
templates per service-day (M6) with an error share; planted incident catalogue (spike, drop, ramp, level shift, mix
swap) with sizes, durations and frequency.

### 7.4 `counts` mode
Per service × template × minute: λ = hourly mean × scale × day factor × holiday × trend × residual; count ~ NB(λ).
Derived keys exactly as `OpenMinutes`: `*total` = Σ templates; `*error`/`*warn`/`*info` by `level_class`;
`*minutes` = 1. Writes, in schema `lab_s_<scenario>` (migrated like `public`): `lab_minute_counts`, `hourly_counts`,
`service_hours`, `day_status`, `templates`, `template_doc_links`, `lab_incidents` (source `synthetic`). Same seed →
identical tables.

### 7.5 `engine` mode
The same λ model emits log lines from the world's template texts into a generated source; the real Engine runs on an
injected clock into the scenario schema with the recorder on and σ detection live. Used to prove that `counts` mode
matches the real pipeline (≥ 99.9 % of key-minutes after mapping templates by text) and for the parity gate.

## 8. Tests A, B, C on the laptop

| Test | Data | Incidents | Measures |
|---|---|---|---|
| **B (fit)** | synthetic world, or recorded real days | none added; planted windows excluded from normal minutes | coverage, interval score vs σ, sharpness, MASE, alert rate, flap share |
| **A (detect, in memory)** | as B | injected into a copy of the counts: spike, drop, ramp, level shift, burst (M6), mix swap (M7) × 3 sizes, 5 seeded passes | recall within 15 min, delay (median, p90) per type × size |
| **C (detect, real path)** | recorded real days | `incident_test.py` bursts through the replayer → ES → engine | recall and delay on real windows; Test A vs C gap per method (> 10 points flags the injector) |

Injection consistency: an injection into a template changes `*total`, its level-class key and its `doc:` keys by the
same delta. Placement: never within ± 15 min of a planted or real incident; ≥ 3 h apart per key.

## 9. Judging

As `docs/specs/2026-10-26-bakeoff-judging.md`: shared normal minutes; tune/test split (halves by time, or by key
under 14 days); equal coverage 99.5 % for band methods, equal alert budget (1 per 100 key-days; M1 1 per service-week)
for score methods, σ tuned the same way; ranking by Winkler interval score (α = 0.005) relative to σ; day-block
bootstrap (1,000, seeded) with 90 % intervals; "fits better than σ" = ratio ≤ 0.90 and CI upper < 1.00; ties to the
cheaper method; gates (band for M1–M5, cost, coverage 99.0–99.9 %, alert rate, flaps ≤ 10 %, Test A recall not worse
than σ by > 5 points); service check (> 20 % of services worse → mixed); verdict strength exploratory / provisional /
final.

## 10. Storage (migration 010)

| Table | Key | Content |
|---|---|---|
| `lab_minute_counts` | service, key, day, hour | `counts int[60]`; partitioned by day |
| `lab_runs` | id | site, test, status, times, code version, image tag, data schema, window, tune end, seed, params (methods, knob grids, scenario, budgets, split), kinds, error |
| `lab_results` | run, method, kind, scope | chosen knob, status (ok / skipped / unavailable / failed), metrics JSON (incl. CI, cost), verdict, strength |
| `lab_episodes` | run, method, service, key, start | end, peak score, alerted minutes |
| `lab_bands` | run, method, service, key, step, minute | count, expected, low, high, high flag; step 5 overview, 1 within ± 2 h of episodes |
| `lab_sweeps` | run, method, kind, knob, part | metrics for every knob on tune and test |
| `lab_choices` | kind | method, knob, run, note (lab only) |
| `lab_incidents` | id | source (synthetic / test-a / test-c), data schema, run, service, key, type, size, start, end (minutes; end exclusive), meta |

Every `lab_*` table except `lab_minute_counts`, `lab_choices` and `lab_incidents` cascades from `lab_runs`.
The last 5 runs per site are kept. `lab_bands` stores the real count so the page never reads count tables.

## 11. Runner

```
python -m scripts.lab.run --site laptop --test B --methods core|heavy|all|<list>
       (--days N | --from D --to D) [--schema lab_s_<scenario>] [--seed S] [--check]
```
- `--site company` with `--test A|C` exits 2 before connecting.
- `--check` imports each selected method (and the Chronos weights), prints available / unavailable, writes nothing.
- Lifecycle: insert `lab_runs` (running) → per service: load, kinds, per method walk-forward, (Test A: inject passes),
  episodes, metrics, drawn subset → tune, sweeps, bootstrap, decide → write → status ok / failed with error.
- Drawn subset: per kind the 5 busiest keys + every key with an episode, capped at 50 keys per method.
- Reproducible: same code, data, params and seed → same numbers.

## 12. Evaluation API and page

API (reads only `lab_*`; token as other routes): `GET /api/eval/runs`, `/scoreboard?run=`, `/series?run=&service=&key=&from=&to=&methods=&knob=`,
`/sweep?run=&kind=`, `/keys?run=&kind=`; `POST /api/eval/choices`, `/detect` (metrics-only import, used by the company later).

Page (`/#evaluation`, nav "Evaluate"), as the mockup: run bar; decision per kind with strength; scoreboard with
gate cells and CI; fit chart as small multiples with shared zoom and an overlay mode; episode swimlanes; Calibrate
(sweep chart, knob pills redraw the band, Choose). Laptop runs add Test A recall/delay, Test C and the A vs C flag.

## 13. Performance targets (laptop)

| Run | Target |
|---|---|
| Recorder overhead | flush p95 + < 10 %, no change in lag (measured with the pipeline stress scenario N1) |
| `synth --mode counts`, 28 days, 3 services × 180 templates | < 10 min |
| Lab, core methods, 28 days, Test B | < 20 min |
| Lab, all methods, 28 days, Test B | < 2.5 h, peak RSS < 5 GB |
| Test A, 5 passes, core methods | < 2× the Test B time (fits reused) |

## 14. Correctness gates

1. Recorder: Σ of a row = `hourly_counts.sum`; Σ `*minutes` = `n_minutes`; crash mid-hour loses and duplicates nothing.
2. Lane rows = live rows for the same docs.
3. σ rebuild in Python = SQL `REBUILD` (to 1e-9).
4. Parity: lab σ transitions = `alert_changes`, outside 10 min after commits.
5. Synthetic: `*total` = Σ templates every minute; same seed → same tables; engine vs counts ≥ 99.9 %.
6. Injections keep derived keys consistent.
7. Live tables unchanged by any lab run (asserted in tests).

## 15. Risks

| Risk | Effect | Handling |
|---|---|---|
| Synthetic world too easy (too regular) | forecasters look better than on real data | residual bootstrap from real blocks; Test C on real data; the company run decides |
| Laptop real data is one service, one day shape | weekly and holiday effects only synthetic | stated on every laptop verdict; final decisions only on company data |
| Commit-instant gap in parity | a few mismatches after 02:00 | allowed window, counted and reported |
| Today's links applied to the whole window | doc keys differ from what live judged | stated; window short |
| Heavy methods exceed RAM | lab run killed | bge-m3 off; memory limit; one service at a time; Prophet/Chronos on subsets |
| Drain3 merges/splits templates differently in engine mode | counts vs engine mismatch | compare after mapping by text; threshold 99.9 % |
| `int[]` in COPY | adaptation errors | tested in task 1 |
