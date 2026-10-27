# Baseline Bake-off on the laptop: implementation plan (tasks 0–17)

Status: **draft for review** (2026-10-27). Agent roles are not assigned yet (to discuss).
Replaces the task list of `docs/plans/2026-10-26-baseline-bakeoff.md` (61–72) for the laptop build; the company
deployment (k8s Job, read-only role, company smoke) is deferred to a later plan once the laptop proof passes.

Design: `docs/specs/2026-10-26-baseline-bakeoff.md`. Methods and reasons: `docs/specs/2026-10-26-bakeoff-methods.md`.
Criteria: `docs/specs/2026-10-26-bakeoff-judging.md`. Mockup: https://claude.ai/artifact/EXqQaUkx4aAZTsEmj1A2be.

**Goal on the laptop:** record per-minute counts, build multi-week synthetic history from the laptop's own data,
run all 13 methods + E1 through Tests A, B and C with one judging protocol, and see every method's band on the
Evaluation page. Live alerting never changes.

## Facts this plan is built on (checked 2026-10-27)

| Fact | Value | Consequence |
|---|---|---|
| Laptop DB | 1 service (`recharge`), 180 templates, 2 days (26–27 Oct), migrations at 009 | Real laptop history is far too short for Test B: multi-week history comes from the synthetic world (task 6) |
| Laptop machine | 16 cores, 12 GB RAM, ~1 GB free with the stack running, 143 GB free disk | Heavy methods need the linker / bge-m3 stopped during lab runs, and a memory limit on the lab container |
| App venv | no numpy | Core methods are pure Python (`math`, `array`, `statistics`); heavy methods only in the lab image |
| Hourly storage | `hourly_counts` keeps `sum`, `sumsq` per (service, key, day, hour), upserted **additively** at each flush | The recorder must be additive per flush too (see D1) |
| Closed minutes | `ClosedMinute.keys` holds templates + `*total` / `*error` / `*warn` / `*info`; silent minutes get 0 for keys seen earlier in the hour; `doc:` keys are added only inside `Detector.run` | The recorder stores `ClosedMinute.keys`; `doc:` keys are rebuilt by the lab from links, as the Detector does |
| Time | VN has no DST: every local day is 1440 minutes; minute number = epoch seconds // 60; day/hour via `timeutil.day_hour` | Lab arrays are per local day, 1440 slots |
| Live σ | `judge()` + `Baseline.lookup` / `lookup_doc` + `detect.step` (2/3/3); rebuild SQL `REBUILD` in `jobs/commit.py`; commit at 02:00 for days before the owner's day | The lab's σ (task 4) must reproduce this exactly, commit timing included |
| Tests import `scripts.*` | `tests/test_stress_pipeline.py` imports `scripts.stress_pipeline` | Lab code lives in `scripts/lab/`, importable as `scripts.lab` |

## Decisions in this plan (please confirm or change)

- **D1. Recorder is additive per flush, not "one row when the hour closes".** The old plan held the open hour in
  memory and wrote it at hour end. That loses data on a crash: closed minutes of the current hour were already
  flushed and are not re-read on restart (the cursor is the oldest *open* minute). Instead each flush upserts the
  minutes it closed into the hour's `int[60]` row, adding element-wise, exactly like `hourly_counts.sum`. Exactly-once
  then follows from the existing flush transaction.
- **D2. Observed minutes are stored as key `*minutes`.** Per service and closed minute, `*minutes` gets 1. This tells
  "no data" (engine down, service not yet seen) from a real 0. Its hourly sum equals `service_hours.n_minutes`.
- **D3. `doc:` keys are not recorded.** The lab rebuilds them from `template_doc_links` (sources `ALERTING_SOURCES`)
  with today's links, as `Detector.run` does. Known limit: links changed during the window are applied backwards.
- **D4. Same alert shape for every method.** A method's minute is *high* when `count > high` **and** the live floor
  for the key's level is met (`cfg.floors`: ERROR 5, WARN 10; none for INFO), or, for `*total` only, when
  `count < low` with `expected ≥ drop.min_mean`. Score-only methods (#6, #12, #13) are high when `score > threshold`,
  floor included. Then `detect.step` with the live `AlertConfig`. This keeps differences down to the method.
- **D5. Episode** = a run of states that reaches ALERTING. Start = the minute of its first high minute (the one that
  left NORMAL); end = the minute it returns to NORMAL (or the window end). WARMING → NORMAL without ALERTING is not
  an episode.
- **D6. Synthetic data lives in its own schema; results always go to `public.lab_*`.** `lab_bands` stores the real
  count beside the band, so the Evaluation page reads only `lab_*` and never needs the scenario schema.
- **D7. Lab memory is bounded per service.** The runner loads, fits and scores one service at a time (M7 needs the
  whole service vector anyway). Arrays are `array('i')` with `-1` for unobserved minutes.
- **D8. Holidays** are a list in `config.yaml` under `lab.holidays` (ISO dates, VN 2025–2027: Tết, 30/4, 1/5, 2/9,
  Hùng Kings, New Year). Synthetic scenarios add their own holiday block.

## Task overview

| # | Task | Depends on | Old id |
|---|---|---|---|
| 0 | Preflight: facts, fixtures plan, docs cleanup | — | – |
| 1 | Migration 010, `LabConfig`, live recorder | 0 | 61 |
| 2 | Recorder in the re-read lane | 1 | 61 |
| 3 | Lab foundation: data model, loader, kinds, method contract, runner skeleton, storage | 1 | 62 |
| 4 | σ method, episodes, parity with the engine | 3 | 62 |
| 5 | Scoring: shared normal minutes and Test B metrics | 3 | 64 |
| 6 | Synthetic world, `counts` mode | 3 | 70 |
| 7 | Tuning, sweeps, bootstrap, decision tree | 5 | 64 |
| 8 | Core methods A: #2 NB, #3 clipped NB, #4 MAD, #7 weekly + holidays | 4, 5 | 63 |
| 9 | Core methods B: #5 ratio, #6 CUSUM, #13 PCA, E1 vote | 8 | 63 |
| 10 | Test A injector, recall and delay | 5, 6 | 65 |
| 11 | Lab image and heavy methods #8–#12 | 4, 5 | 66 |
| 12 | Synthetic world, `engine` mode | 6, 1 | 70 |
| 13 | Test C: incidents from `incident_test.py` runs, A vs C gap | 10 | 71 |
| 14 | Evaluation API | 7 | 68 |
| 15 | Evaluation page | 14 | 69 |
| 16 | Compose lab service, run commands, docs | 11, 15 | 71 |
| 17 | Laptop proof | all | 72 |

Order: 0 → 1 → (2, 3) → (4, 5, 6) → (7, 8, 10, 11, 12) → (9, 13, 14) → 15 → 16 → 17.
The recorder (1) goes first so the laptop starts collecting real per-minute data while the rest is built.

## Rules for every task

- Tests: `.venv/bin/pytest -q` against the test Postgres on 127.0.0.1:5433. Write tests first and see them fail.
  The full suite passes before a task is done.
- Stage by explicit path; no AI attribution in commits. Never push. Never touch `~/Documents/logai-engine`.
- **Live alerting never changes:** lab code never writes `alert_state`, `alert_changes`, `baseline`, `day_status`,
  `hourly_counts`, `service_hours` in `public`. It writes only `lab_*` (and the synthetic schema's tables in task 6/12).
- App image: `requirements.txt` unchanged. Heavy packages only in `requirements-lab.txt` / `Dockerfile.lab`.
  Heavy imports are lazy (inside the method module), so `--methods core` runs with the app venv.
- Every task updates the docs it changes (`ARCHITECTURE.md`, `docs/WEB_UI.md`, `README.md`, `docs/BUGS.md`) and
  the spec where the code departs from it.
- Code reads like the code around it (style of `owner.py`, `commit.py`, `baseline_view.py`).
- One branch + worktree per task: `task/<n>-<slug>` from `master`.

---

## Task 0: Preflight (no feature code)

**Goal:** settle facts and decisions so later briefs are exact.

**Steps**
1. Merge `docs/bakeoff-plan` into master (after the user confirms), so briefs reference merged docs.
2. Write `docs/validation/2026-10-27-bakeoff-preflight.md` with:
   - laptop stack inventory: container names, ports, `docker stats` memory per container at rest;
   - replayer API: list endpoints actually used by `scripts/incident_test.py` (`/status`, `/replay/inject`,
     `/replay/inject/stop`) and their request bodies; replay source file, time span (00:00–16:07), speed control;
   - the `incident_test.py` run directory format (`meta.json`, `phases.jsonl`, `samples.jsonl`): fields used by task 13;
   - DB facts: services, templates per level, days present, `template_doc_links` count per source;
   - free RAM with linker + bge-m3 stopped (decides the lab memory limit in task 16).
3. Confirm D1–D8 with the user; record each as a ruling in `.team/ledger.md`.
4. Mark `docs/plans/2026-10-26-baseline-bakeoff.md` "superseded for the laptop build by 2026-10-27-bakeoff-laptop.md";
   update the spec's "Files" section: path `scripts/lab/`, recorder D1/D2, D6.

**Done when:** the preflight doc exists, rulings are logged, docs point to this plan.

---

## Task 1: Migration 010, `LabConfig`, live recorder

**Files:** `logai2/migrations/010_lab.sql`, `logai2/db.py` (`PARTITIONED`), `logai2/config.py`, `config.yaml`,
`logai2/owner/owner.py`, `logai2/jobs/commit.py`, `docker-compose.yml`, `tests/test_recorder.py`, `tests/test_db.py`,
`tests/test_commit.py`, `ARCHITECTURE.md`, `README.md`.

### 1.1 Schema (migration 010, all lab tables at once)
```sql
CREATE TABLE lab_minute_counts (
  service text NOT NULL, key text NOT NULL, day date NOT NULL, hour smallint NOT NULL,
  counts int[] NOT NULL CHECK (array_length(counts, 1) = 60),
  PRIMARY KEY (service, key, day, hour)) PARTITION BY RANGE (day);

CREATE TABLE lab_runs (
  id bigserial PRIMARY KEY, site text NOT NULL CHECK (site IN ('laptop', 'company')),
  test text NOT NULL CHECK (test IN ('A', 'B', 'C')), status text NOT NULL DEFAULT 'running',
  started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
  code_version text NOT NULL, image_tag text, data_schema text NOT NULL DEFAULT 'public',
  window_from date NOT NULL, window_to date NOT NULL, tune_to date NOT NULL, seed int NOT NULL,
  params jsonb NOT NULL,          -- methods, knob grids, scenario, budgets, split mode
  kinds jsonb,                    -- key count per kind, and the assignment hash
  error text);

CREATE TABLE lab_results (        -- one row per run x method x kind x scope ('all' or a service)
  run_id bigint REFERENCES lab_runs ON DELETE CASCADE, method text, kind text, scope text,
  knob double precision, status text NOT NULL,   -- ok | skipped (not enough history) | unavailable | failed
  metrics jsonb NOT NULL, verdict text, strength text,
  PRIMARY KEY (run_id, method, kind, scope));

CREATE TABLE lab_episodes (
  run_id bigint REFERENCES lab_runs ON DELETE CASCADE, method text, service text, key text,
  start_minute int, end_minute int, peak_score double precision, alerted_minutes int,
  PRIMARY KEY (run_id, method, service, key, start_minute));

CREATE TABLE lab_bands (          -- drawn series; step 5 for overview, 1 around episodes
  run_id bigint REFERENCES lab_runs ON DELETE CASCADE, method text, service text, key text,
  step smallint, minute int, count int, expected real, low real, high real, high_flag boolean,
  PRIMARY KEY (run_id, method, service, key, step, minute));

CREATE TABLE lab_sweeps (
  run_id bigint REFERENCES lab_runs ON DELETE CASCADE, method text, kind text, knob double precision,
  part text CHECK (part IN ('tune', 'test')), metrics jsonb NOT NULL,
  PRIMARY KEY (run_id, method, kind, knob, part));

CREATE TABLE lab_choices (
  kind text PRIMARY KEY, method text NOT NULL, knob double precision, run_id bigint,
  chosen_at timestamptz NOT NULL DEFAULT now(), note text);

CREATE TABLE lab_incidents (
  id bigserial PRIMARY KEY, source text NOT NULL CHECK (source IN ('synthetic', 'test-a', 'test-c')),
  data_schema text NOT NULL DEFAULT 'public', run_id bigint, service text NOT NULL, key text,
  type text NOT NULL, size double precision, start_minute int NOT NULL, end_minute int NOT NULL, meta jsonb);
```
- Add `"lab_minute_counts"` to `db.PARTITIONED`, so `ensure_partitions` creates its daily partitions and
  `drop_partitions_before` accepts it. No partitions are created in the migration (nothing seeds the table).

### 1.2 Config
```python
@dataclass
class LabConfig:
    record: bool = False        # env LOGAI2_LAB_RECORD
    days: int = 28              # lab_minute_counts retention
    holidays: list = field(default_factory=list)   # ISO dates (D8)
```
`AppConfig.lab`. Env: `LOGAI2_LAB_RECORD` true/false parsed like `LOGAI2_ES_VERIFY_CERTS`.
`config.yaml`: `lab: {record: false, days: 28, holidays: [...]}`. `docker-compose.yml`: engine env
`LOGAI2_LAB_RECORD: ${LOGAI2_LAB_RECORD:-true}` (laptop records by default).

### 1.3 Recorder in `Owner`
- New pending dict in `_clear()`: `self._lab: dict[tuple[str, str, date, int], list[int]] = {}`
  ((service, key, day, hour) → 60 counts).
- In `handle()`, inside the loop over closed minutes, when `cfg.lab.record`:
  - `slot = local(cm.minute).minute` (from `timeutil`; equals `cm.minute % 60` at +07:00, but stays right for any
    whole-hour zone and reads clearly);
  - for each `(service, key), n` in `cm.keys` with `n > 0`: `row[slot] += n`;
  - for each service with a `*total` key in `cm.keys` (all services zero-filled for this minute):
    `self._lab[(service, "*minutes", day, hour)][slot] = 1`.
- In `flush()`, inside the transaction, after `hourly_counts`: `copy_upsert(cur, "lab_minute_counts", rows)` with
  ```sql
  INSERT INTO lab_minute_counts (service, key, day, hour, counts) SELECT * FROM tmp_lab_minute_counts
  ON CONFLICT (service, key, day, hour) DO UPDATE SET counts = ARRAY(
    SELECT a + b FROM unnest(lab_minute_counts.counts, EXCLUDED.counts) WITH ORDINALITY AS t(a, b, i) ORDER BY i)
  ```
  (`WITH ORDINALITY … ORDER BY i` makes the element order explicit; both arrays have 60 elements.) For `*minutes`
  the add is safe because each minute is closed exactly once.
- `copy_upsert` COPYs `int[]` values: the test must cover it (psycopg may need `cp.set_types([..., "int4[]"])`
  for list columns in binary COPY; text COPY adapts lists).
- Not recorded: rows that are all zero; `doc:` keys (D3). `:unparsed` templates are recorded (the lab ignores them).
- Days older than the partition range: the recorder skips rows with `day < today - lab.days` (as the module hours do
  with `mod_cutoff`), so a catch-up from `days_back` never writes into a missing partition.

### 1.4 Retention
`commit()`: in housekeeping, `dropped += drop_partitions_before(conn, "lab_minute_counts", today - timedelta(days=cfg.lab.days))`.

### 1.5 Tests (`tests/test_recorder.py`, using the owner fixtures of `tests/test_owner.py`)
1. Flag off: after a flush with closed minutes, `lab_minute_counts` is empty.
2. Flag on, one hour fed across **three flushes** (minutes 0–19, 20–39, 40–59): the row equals the minute-by-minute
   counts fed; `sum(counts)` equals `hourly_counts.sum` for every key; `*minutes` sums to `service_hours.n_minutes`.
3. Crash mid-hour: flush minutes 0–29, drop the owner without flushing minutes 30–34 (open), restart a new Owner from
   the cursor and feed minutes 30–59 again: no minute is double-counted or missing (compare to a never-crashed run).
4. A failed flush (raise inside the transaction, as `test_flush_failure_keeps_pending`) keeps `_lab` pending; the next
   flush writes it once.
5. Silent minutes: a template seen at minute 3 and silent after has zeros after; `*minutes` has 1 for every closed minute.
6. A day older than `today - lab.days` is not written and does not raise.
7. `commit()` drops `lab_minute_counts` partitions older than `lab.days` and keeps newer ones.
8. Live tables unchanged: run the same feed with flag on and off; `hourly_counts`, `service_hours`, `alert_changes`,
   `alert_state` are identical.
9. `tests/test_db.py`: migration 010 applies on an empty DB and on a DB at 009; `schema_migrations` = 10.

### 1.6 Docs
ARCHITECTURE §7.3 (flush writes `lab_minute_counts` when `lab.record`), §11 schema, README (flag, size: 60 ints per
key-hour ≈ 0.3–0.6 GB / 28 days at 3k keys; laptop ≈ 30 MB). Upgrade note: migration 010.

**Done when:** tests 1–9 pass, full suite green, and the laptop engine (rebuilt image) writes rows within an hour.

---

## Task 2: Recorder in the re-read lane

**Files:** `logai2/jobs/reread.py` (`LaneHour`, `Lane._close`), `logai2/owner/owner.py` (`_flush_lane`, `SQL["lane_delete"]`),
`tests/test_reread.py`, `ARCHITECTURE.md`.

- `LaneHour.minutes: dict[str, list[int]]` (key → 60 counts) filled in `_close` from `cm.keys` (n > 0), and
  `minutes["*minutes"][slot] = 1` for every closed minute of a kept service (same rule as `n_minutes += 1`).
- `_flush_lane`: `lab_minute_counts` joins `lane_delete` (whole hour deleted), then the hour's rows are inserted when
  `cfg.lab.record` and the day is within `lab.days`. Delete always runs, so turning the flag off and re-reading leaves
  no stale rows.
- **Tests**
  1. A lane hour replaces an existing recorded hour completely (stale keys deleted).
  2. Exactness: the same synthetic docs read live and through the lane give identical `lab_minute_counts` rows.
  3. A skipped service (`gone`) writes nothing for that hour.
  4. Flag off: the lane deletes the hour's lab rows and writes none.

**Done when:** tests pass; a 1-day re-read on the laptop fills `lab_minute_counts` for that day.

**Operations after 1 + 2 are merged (laptop):** rebuild the image, restart `logai2-engine` with
`LOGAI2_LAB_RECORD=true`, Retrain → Re-read the days ES still holds, then one overnight 1× replay. This real data
seeds the synthetic world (task 6) and the Test C runs (task 13).

---

## Task 3: Lab foundation

**Files:** `scripts/lab/__init__.py`, `scripts/lab/data.py`, `scripts/lab/load.py`, `scripts/lab/kinds.py`,
`scripts/lab/methods/__init__.py`, `scripts/lab/run.py`, `scripts/lab/store.py`, `tests/lab/__init__.py`,
`tests/lab/conftest.py`, `tests/lab/test_load.py`, `tests/lab/test_kinds.py`, `tests/lab/test_run.py`.

### 3.1 Data model (`data.py`)
```python
Key = tuple[str, str]                      # (service, key)

@dataclass
class ServiceData:                         # everything the lab knows about one service, for a window
    service: str
    days: list[date]                       # local days of the window, ascending, contiguous
    minutes: dict[str, dict[date, array]]  # key -> day -> array('i', 1440), -1 = unobserved
    observed: dict[date, array]            # from *minutes: 1 observed, 0 not
    hourly: dict[tuple[str, date, int], tuple[int, int]]   # (key, day, hour) -> (sum, sumsq), may predate `days`
    n_minutes: dict[tuple[date, int], int]                 # service_hours
    committed: set[date]                   # day_status committed
    excluded: set[tuple[date, int]]        # Review 'exclude' hours
    levels: dict[str, str]                 # template_id -> level
    first_seen: dict[str, int]             # template_id -> minute
    docs: dict[str, list[str]]             # 'doc:<id>' -> linked template ids (ALERTING_SOURCES)
```
- `doc:` arrays are built in the loader as the element-wise sum of linked templates (unobserved stays -1).
- Helper `minute_at(day, slot) -> int` and `slot_of(minute) -> (day, slot)` via `timeutil`.

### 3.2 Loader (`load.py`)
- `load_service(conn, service, window_from, window_to, history_days) -> ServiceData`: reads `lab_minute_counts` for
  the window, and `hourly_counts`, `service_hours`, `day_status` back to `window_from - history_days` (σ and the
  forecasters need older hourly history than the per-minute window).
- `services(conn) -> list[str]`. Reads only; works with `SET TRANSACTION READ ONLY` (the test asserts it).
- `schema` argument: `SET search_path TO <schema>, public` for synthetic schemas.

### 3.3 Kinds (`kinds.py`, J1)
`assign(sd: ServiceData, tune_days) -> dict[str, str]` (key → M1..M6, or absent = not scored) and M7 per service:
M1 `*total`; M2 `*error`; M5 `doc:*`; M3 template with mean ≥ 2/min over its busy hours (hours where it is > 0);
M4 ERROR/WARN template with mean < 2/min; M6 is **per minute**: a template is M6 while `minute - first_seen < 1440`,
so `assign` returns its base kind plus `born` minute and scoring splits its minutes. INFO < 2/min, `*warn`, `*info`,
`*minutes`, `:unparsed` are not scored.

### 3.4 Method contract (`methods/__init__.py`)
```python
@dataclass(frozen=True)
class Spec:
    name: str              # 'sigma', 'nb', ... (stable id used in tables)
    number: str            # '#1' .. '#13', 'E1'
    label: str
    kinds: frozenset[str]  # kinds served
    knobs: tuple[float, ...]
    band: bool             # draws low/expected/high; else score + threshold
    heavy: bool
    cost: int              # tie-break rank (lower = cheaper)
    min_hour_days: int = 0
    min_minute_days: int = 0

class DayOut(NamedTuple):            # one key, one day, one knob
    expected: array | None           # 1440 floats (band methods)
    low: array | None
    high: array | None
    score: array                     # 1440 floats
    high_flag: bytearray             # 1440: 1 = high after D4 rules

class Method(Protocol):
    spec: Spec
    def fit(self, sd: ServiceData, day: date) -> object | None: ...   # history strictly before `day`; None = not enough history
    def day(self, model, sd: ServiceData, key: str, day: date, knobs) -> dict[float, DayOut]: ...
```
- **Walk-forward:** the runner calls `fit(sd, day)` once per test day, then `day(...)` for every key with all knobs at
  once (sweeps need every knob; one pass is cheaper than one pass per knob).
- `apply_d4(count, low, high, expected, key, level, cfg) -> bool` lives here and is the only implementation of D4.
- Registry: `METHODS = {name: factory}`; `--methods core|heavy|all|<comma list>`; heavy factories import lazily and
  raise `Unavailable` when a package is missing.

### 3.5 Runner (`run.py`) and storage (`store.py`)
- CLI: `--site laptop|company --test A|B|C --methods ... --from --to (or --days N ending yesterday) --schema
  --scenario --seed --check`. `--site company` with `--test A|C` exits 2 before opening a DB connection
  (test). `--check` imports every selected method and prints available/unavailable, writes nothing.
- Split: tune = first half of the days, test = second half; with fewer than 14 days, keys are split in half
  (by a seeded hash of the key) instead. The split mode goes in `params`.
- Flow: create `lab_runs` row (status running) → for each service: load, assign kinds, for each method: walk-forward
  over all days, collect per-minute outputs in memory for this service → hand to scoring (task 5) → write results →
  next service. Finish: status ok/failed, `finished_at`; keep the last 5 runs per site (delete older, cascades).
- A method that raises on one service: status `failed` for that method with the error in `metrics`, others continue.
- `code_version` = `git rev-parse --short HEAD` when available, else env `LOGAI2_CODE_VERSION`.

### 3.6 Tests
- `test_load.py`: a hand-filled DB (two days, three keys, one doc link, one unobserved hour) loads into exactly the
  expected arrays, `-1` where unobserved, the doc array is the sum; the loader runs in a read-only transaction.
- `test_kinds.py`: one key of each kind on a hand-made `ServiceData`; a template born mid-window is M6 for 1440 minutes
  then its base kind.
- `test_run.py`: `--site company --test A` exits non-zero and creates no `lab_runs` row; `--check` writes nothing;
  a run with a dummy "constant band" method writes one `lab_runs`, `lab_results` rows per kind, and keeps 5 runs.

**Done when:** the dummy method runs end to end on a tiny DB.

---

## Task 4: σ method, episodes, parity

**Files:** `scripts/lab/methods/sigma.py`, `scripts/lab/episodes.py`, `tests/lab/test_sigma.py`, `tests/lab/test_parity.py`.

### 4.1 σ (#1)
- `fit(sd, day)`: rebuild the `Baseline` **as the live commit would have** for `day`, in Python, from `sd.hourly`,
  `sd.n_minutes`, `sd.committed`, `sd.excluded`: last 5 committed weekdays / 4 weekend days strictly before `day`
  (the commit on `day` marks days `< day`), exclusions removed, `n_minutes > 0` only, the `born` rule for
  `*warn`/`*info`, mean = Σsum / Σn_minutes, std = sqrt(max(Σsumsq/Σn − mean², 0)). Return a real
  `logai2.owner.baseline.Baseline(stats, days)`.
- **Commit timing:** minutes of `day` before `cfg.baseline.commit_time` use the model fitted for `day - 1`
  (the live commit has not run yet). `day()` therefore takes both models; the runner passes the previous day's model.
- `day(...)`: per minute, `Baseline.lookup` (or `lookup_doc` for `doc:` keys) + `judge(count, key, level, lk, cfg)`;
  the knob replaces `cfg.baseline.sigma_multiplier` (young multiplier scales by the same ratio, 4/3). Band = the
  verdict's `band_low` / `band_high`; expected = `lk.this.mean`. Unobserved minutes: no verdict.
- Knobs: k ∈ {2.0, 2.5, 3.0, 3.5, 4.0, 5.0}; live default 3.0.

### 4.2 Episodes (`episodes.py`)
`episodes(flags: Iterable[(minute, high: bool)], cfg.alert) -> list[Episode]` steps `detect.step` from a fresh
`AlertRow` and applies D5. Unobserved minutes are skipped (the state machine does not advance), as the live engine
never closes them.

### 4.3 Tests
- `test_sigma.py`: the Python rebuild equals the SQL `REBUILD` result on a hand-made DB (run `commit()` on the test
  DB, read `baseline`, compare every row to 1e-9); weekend borrow (`upper_only`); young vs full k; exclusions; born rule.
- `test_parity.py` (marked slow): drive the real Engine with `scripts/stress_pipeline.py` (`GenSource`, `Clock`,
  throwaway schema) over **5 local days starting on a Thursday** (crosses a weekend), 1 service × 20 templates + one
  doc link, one planted burst per day, recorder on, commits at 02:00 by the engine. Then run lab σ (k = 3) over days
  2–5 on the same schema and compare transitions `(key, minute, to_state)` with `alert_changes`.
  **Exact match required**, except minutes within 10 minutes after each commit instant (the engine reloads the
  baseline at the first tick after its commit; the lab switches at `commit_time`). Those minutes are counted and the
  test asserts there are no mismatches outside them and ≤ 10 per commit inside them.

**Done when:** parity is exact outside commit windows. This is the gate for trusting every later comparison.

---

## Task 5: Scoring, Test B metrics

**Files:** `scripts/lab/score.py`, `tests/lab/test_score.py`.

- **Shared normal minutes** per service: observed minutes of the test part, minus Review-excluded hours, minus every
  `lab_incidents` window ± 15 min for that service (synthetic, test-a, test-c). One set for all methods.
- Per (method, kind, knob) over shared normal minutes of keys of that kind (J3):
  - coverage = share with `low ≤ count ≤ high` (band methods);
  - Winkler interval score at α = 0.005 per key, divided by the key's mean count (keys with mean 0 use 1),
    averaged over keys; then **relative to σ at its tuned knob** (σ = 1.00);
  - sharpness = median (high − low) / max(expected, 1e-9);
  - MASE vs seasonal naive (same minute 7 days earlier when all history exists, else 1 day; reported which);
  - alert rate = episodes starting on shared normal minutes per 100 key-days (M1: per service-week; M7: per service-day);
  - flap share = episodes < 5 min or starting < 30 min after the previous episode end on the same key.
- Per-service variants of interval score and alert rate (scope = service) for the service check.
- Output: `Metrics` dataclass → `lab_results.metrics` JSON (all numbers, plus n_keys, n_minutes).
- **Tests:** hand-made series with known answers: a band covering 995/1000 minutes gives 99.5 %; one miss of size d
  adds `(2/α)·d / 1000` to the interval score; a flapping series gives the expected flap share; incident windows are
  removed from shared minutes for every method alike.

---

## Task 6: Synthetic world, `counts` mode

**Files:** `scripts/lab/world.py`, `scripts/lab/synth.py`, `lab/scenarios/default.yaml`, `lab/scenarios/tiny.yaml`,
`tests/lab/test_synth.py`, `README.md`.

### 6.1 World file (`world.py` → `lab/world.json`, not committed: it contains the laptop's template texts)
From the laptop DB (recorded replay + hourly counts): per template: id, text, level, doc link; mean per local hour
(0–23; hours 16:07–24:00 missing in the replay are mirrored from 00:00–07:53 and flagged); per-minute residual blocks
(60-minute windows of `count / hourly mean`) for a residual bootstrap; NB dispersion per template from per-minute
variance (fallback Poisson).

### 6.2 Scenario YAML
```yaml
seed: 7
days: 28                     # ending yesterday
services: {count: 3, scale: [1.0, 0.5, 2.0], subset: 0.8, phase_minutes: [0, 20, -15]}
day_factors: {weekday: 1.0, jitter: 0.05, sat: 0.75, sun: 0.65}
holiday: {start_day: 18, length: 3, factor: 0.4}     # in the test half
trend: {per_week: 0.03}
residuals: bootstrap          # or nb
new_templates: {per_service_day: 3, error_share: 0.3}
incidents:                    # planted, written to lab_incidents source=synthetic
  - {type: spike, size: 3, minutes: 10, every_days: 2}
  - {type: drop, size: 0.3, minutes: 30, key: '*total', every_days: 5}
  - {type: ramp, size: 2, minutes: 60, every_days: 4}
  - {type: level_shift, size: 1.5, minutes: 240, every_days: 7}
  - {type: mix_swap, minutes: 60, every_days: 6}
```

### 6.3 `synth.py --mode counts --scenario X --schema lab_s_X`
- Creates the schema, runs migrations into it (`search_path`), creates partitions for the days.
- Per service × template × minute: λ = hourly_mean[h] × scale × day_factor × holiday × trend(t) × residual; count ~
  NB(λ, dispersion) (seeded `random.Random`). Incidents modify λ on their targets.
- **Derived keys exactly as `OpenMinutes` would:** `*total` = Σ templates; `*error`/`*warn`/`*info` by
  `level_class(level)`; `*minutes` = 1. Writes `lab_minute_counts`, `hourly_counts` (sum, sumsq from the same minute
  counts), `service_hours` (n_minutes 60), `day_status` (committed for every day but the last, daytype), `templates`
  (first_seen = birth minute), `template_doc_links` (source `manual`), `lab_incidents`.
- Never touches `public` (test asserts row counts in `public` unchanged).

### 6.4 Tests
Same seed → byte-identical tables (hash of ordered rows); planted incidents raise the counts in their windows;
weekday/weekend and holiday factors show in daily totals within 5 %; Σ minutes = `hourly_counts.sum` for every key-hour;
`*total` = Σ templates for every minute; tiny scenario (1 service, 5 templates, 3 days) runs in < 10 s.

---

## Task 7: Tuning, sweeps, bootstrap, decision

**Files:** `scripts/lab/tune.py`, `scripts/lab/bootstrap.py`, `scripts/lab/decide.py`, `scripts/lab/run.py` (wire),
`tests/lab/test_decide.py`.

- **Tune (J4):** on the tune part: band methods → knob whose coverage is closest to 99.5 % (tie → wider band);
  score-only → most sensitive knob with alert rate ≤ budget (1/100 key-days; M1 1/service-week). σ tuned the same way.
  Every knob's tune and test metrics → `lab_sweeps`.
- **Bootstrap (J6):** resample whole test days with replacement, 1,000 times, seeded; recompute the interval-score
  ratio vs σ, MASE and alert rate per (method, kind); 5th–95th percentiles → `metrics.ci90`.
- **Decide (J7)** per kind: gates (band for M1–M5; cost; coverage 99.0–99.9 %; alert rate ≤ budget; flap ≤ 10 %;
  detect check: latest laptop Test A run of the same `code_version`, recall at medium size not worse than σ by > 5
  points, else `detect: not checked`), rank by interval-score ratio, ties = overlapping CIs → lower `cost`, then MASE;
  service check (leader worse than σ on > 20 % of services → `mixed`). Verdicts: `adopt candidate`, `sigma stays`,
  `mixed`, `not suitable`. Strength: exploratory < 14 days, provisional 14, final ≥ 28 and ≥ 2 test weekends; stability
  across two consecutive runs noted in `metrics`.
- **Tests:** a hand-made scoreboard gives each verdict; a method with CI crossing 1.00 is not "fits better"; ties
  go to the cheaper method; the bootstrap is reproducible with a seed and changes with another seed.

---

## Task 8: Core methods A (#2, #3, #4, #7)

**Files:** `scripts/lab/methods/{nb.py,mad.py,weekly.py}`, `scripts/lab/holidays.py`, `tests/lab/test_methods.py`.

| Method | Fit (before `day`) | Band at knob | Knobs |
|---|---|---|---|
| #2 NB | σ's profile (same days/exclusions): mean μ, var v per (key, daytype, hour) | low/high = NB quantiles at p, 1−p with r = μ²/(v−μ); Poisson when v ≤ μ | tail p ∈ {1e-2, 3e-3, 1e-3, 3e-4, 1e-4, 1e-5} |
| #3 clipped NB | as #2, but per-minute values > NB(1e-4) quantile of a first fit are clipped before the refit | as #2 | as #2 |
| #4 MAD | last 7 days of per-minute counts per (key, hour), observed only | median ± k·1.4826·MAD (MAD 0 → 1 floor of 1 count) | k ∈ {3, 4, 5, 6, 8, 10} |
| #7 weekly | 168-hour profile from the last 2–4 weeks of hourly data; holidays use a "holiday" profile (Sundays by default) | mean ± k·σ of that hour-of-week | k ∈ {2, 2.5, 3, 3.5, 4, 5} |

- NB quantiles computed in pure Python (incremental pmf) with a cap at μ + 50√v for speed; unit test vs a brute-force sum.
- `holidays.py`: `is_holiday(day, cfg)` from `cfg.lab.holidays` plus the scenario's block.
- **Tests per method:** clean Poisson(5) data → alert rate ≤ budget at the default knob; spike ×3 for 10 min,
  drop to 0.3× on `*total` for 30 min, ramp to ×2 over 60 min → each caught by its intended method (#7 must catch
  a holiday-free drop and stay quiet on a configured holiday); not enough history → `fit` returns None.

---

## Task 9: Core methods B (#5, #6, #13, E1)

**Files:** `scripts/lab/methods/{ratio.py,cusum.py,pca.py,vote.py}`, `tests/lab/test_methods.py`.

- **#5 ratio** (M2, M5): share p̂ = Σkey / Σ`*total` per (daytype, hour) from σ's days; per minute a binomial test
  of key | total; band = binomial quantiles × total. Knob: tail p (as #2). Minutes with total 0: no verdict.
- **#6 CUSUM** (all kinds, score-only): expected λ from σ's profile; S = max(0, S + x·ln(λ1/λ0) − (λ1−λ0)) with
  λ1 = 1.5·λ0; high when S > h. Reset to 0 at alert end. Knob h ∈ {2, 3, 4, 5, 7, 10}.
- **#13 PCA** (M7, score-only): per service, vectors of per-minute template counts (log1p) over the fit days; top
  components by power iteration until 90 % variance (max 10); score = squared prediction error (Q statistic);
  threshold = knob quantile of fit-day Q. Also stores the top 3 contributing templates per high minute in `metrics`.
- **E1 vote**: high when ≥ 2 of {#2 at tuned knob, #6 at tuned knob, best forecaster available (by tune interval
  score) else #7}. Runs after the others in the same run; no fit of its own.
- **Tests:** ratio stays quiet when traffic doubles with errors and alerts when errors double alone; CUSUM catches
  +20 % for 60 min that NB misses; PCA alerts on a mix swap with unchanged total; E1 needs 2 voters.

---

## Task 10: Test A injector

**Files:** `scripts/lab/inject.py`, `scripts/lab/score.py` (recall, delay), `tests/lab/test_inject_score.py`.

- Works on an in-memory copy of `ServiceData` for the test part; `--site company` never reaches it (task 3 guard).
- Types × sizes (relative to the key's expected level at that minute, from σ's profile):
  spike ×{2, 3, 5} 10 min; drop ×{0.5, 0.3, 0.1} 30 min (M1); ramp to ×{1.5, 2, 3} over 60 min; level shift
  ×{1.3, 1.5, 2} 4 h; burst 0 → {5, 20, 50}/min new template 15 min (M6); mix swap of two templates' rates 60 min (M7).
- **Consistency:** an injection into a template also changes `*total`, its level-class key and every `doc:` key it
  belongs to by the same delta (the test asserts `*total` = Σ templates after injection).
- Placement: random in the test part, never within ± 15 min of a synthetic incident, ≥ 3 h apart per key; 5 passes
  with seeds `seed+0..4`; fits are reused across passes (only `day()` and scoring rerun).
- Injections are written to `lab_incidents` (source `test-a`, `run_id`) so shared normal minutes exclude them.
- Recall = injections with an episode start in [start, start + 15 min] / injections; delay = median, p90 of
  (episode start − start). Per method × type × size; headline = medium size.
- Methods tuned to the alert budget for Test A (J5).
- **Tests:** one injected spike on a quiet series gives exactly one hit with the expected delay; derived keys stay
  consistent; no injection overlaps a planted window; same seed → same placements.

---

## Task 11: Lab image and heavy methods (#8–#12)

**Files:** `requirements-lab.txt` (pinned versions), `Dockerfile.lab`, `scripts/lab/methods/{ets.py,prophet_m.py,lgbm.py,chronos_m.py,mprofile.py}`,
`scripts/lab/check.py`, `tests/lab/test_heavy.py` (skips without the packages).

- `Dockerfile.lab`: `FROM logai2:<tag>`; `pip install -r requirements-lab.txt` (torch CPU wheel index); Chronos-Bolt-small
  weights downloaded **at build time** into `/models/chronos-bolt-small`; `ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`.
- Methods (all predict hourly, then per minute: expected = hourly/60 shaped by σ's within-hour profile; band from the
  method's quantiles scaled the same way):
  - #8 ETS: statsmodels `ExponentialSmoothing` additive trend off, seasonal 24 (and 168 when ≥ 3 weeks); daily refit.
  - #9 Prophet: daily + weekly seasonality, holidays from `holidays.py`; `interval_width` from the knob; daily refit;
    only M1, M2, M5 (cost).
  - #10 LightGBM: one global model per run per fit day, quantile objective at {knob, 0.5, 1−knob}; features hour,
    weekday, holiday, lag 1h/24h/168h, key level (log mean), kind; trained on all keys of the service; serves M6.
  - #11 Chronos-Bolt: context = last 7 days hourly, horizon 24; quantiles → band; M1, M2 + 20 % sample of M6.
  - #12 Matrix Profile: `stumpy.stump` window 60 on per-minute series; score = distance; threshold knob; M1 + top-200 M3.
- `--check`: imports each package, loads the Chronos weights, runs a 1-key fit; prints a table; failing methods are
  marked unavailable, exit 0 if at least the core set works.
- Image check: `docker run --network none logai2-lab:<tag> python -m scripts.lab.run --check`.
- Memory: each heavy method processes one service at a time and frees its model; Prophet and Chronos timing per key
  recorded in `metrics.cost_s`.
- **Tests (skipped without packages):** each heavy method fits a 3-week synthetic sine + Poisson series and its band
  covers ≥ 98 % of clean minutes; Prophet with a holiday covers the holiday dip.

---

## Task 12: Synthetic world, `engine` mode

**Files:** `scripts/lab/synth.py`, `tests/lab/test_synth.py`.

- `--mode engine`: builds a generated source from the same scenario (the λ model of task 6 emitting log lines from
  the world's template texts) and drives the real `Engine` on an injected clock (`stress_pipeline.Clock`, `GenSource`,
  `pretrain`) into the scenario schema, with the recorder on and σ detection live. `scale` caps events/s.
- **Test:** a 1-day tiny scenario in both modes gives the same `lab_minute_counts` for ≥ 99.9 % of (key, minute)
  after mapping template ids by text (Drain3 can merge or split a few templates).
- Use: proves that the counts-mode shortcut matches the real pipeline, and gives the parity run of task 17.

---

## Task 13: Test C

**Files:** `scripts/lab/load_incidents.py`, `scripts/lab/score.py` (A vs C), `tests/lab/test_load_incidents.py`, `README.md`.

- `load_incidents.py <run_dir>`: reads `meta.json` and `phases.jsonl` (format from the task 0 preflight doc); every
  `inject` event becomes a `lab_incidents` row (source `test-c`): service, template id resolved by matching the
  inject `pattern` regex against `templates.text` of that service (no match or several → row with `key` NULL and a
  warning), start = phase start minute, end = start + phase minutes, size = `per_minute`.
- Test C run = Test B data + `test-c` incidents: recall and delay per method on those windows (same J5 formulas).
- **A vs C check:** per method, recall gap between Test A (same type/size bucket) and Test C; > 10 points →
  `metrics.injector_flag` and a warning on the page.
- **Tests:** a fixture run dir loads into the expected rows; regex resolution; an ambiguous pattern is reported.

---

## Task 14: Evaluation API

**Files:** `logai2/web/eval_view.py`, `logai2/web/app.py`, `tests/test_web_eval_view.py`, `docs/WEB_UI.md`.

| Endpoint | Returns |
|---|---|
| `GET /api/eval/runs` | last runs: id, site, test, status, window, strength, data_schema, started/finished |
| `GET /api/eval/scoreboard?run=` | per kind: decision + per method metrics, CI, gates pass/fail, status; Test A columns only for laptop runs |
| `GET /api/eval/series?run=&service=&key=&from=&to=&methods=&knob=` | `lab_bands` rows (5-min outside episodes, 1-min ± 2 h around them) + episodes |
| `GET /api/eval/sweep?run=&kind=` | `lab_sweeps` rows for the Calibrate view |
| `GET /api/eval/keys?run=&kind=` | keys with bands stored, for the key picker |
| `POST /api/eval/choices` | `{kind, method, knob, run_id, note}` → `lab_choices` (lab only, never the live config) |
| `POST /api/eval/detect` | import a laptop run's metrics-only summary (company use later; validated, no counts) |

- Reads only `lab_*`; the API token is checked as on the other routes; JSON shapes documented in `docs/WEB_UI.md`.
- **Tests:** synthetic lab rows → each endpoint's JSON; a company run has no Test A columns; a choice POST writes
  `lab_choices` only (assert live tables unchanged); bad `run` → 404.

---

## Task 15: Evaluation page

**Files:** `logai2/web/static/js/views/evaluation.js`, `logai2/web/static/index.html` (nav "Evaluate"),
`logai2/web/static/js/main.js`, `logai2/web/static/css/app.css`, `docs/WEB_UI.md`.

- As the mockup (2 boards): run bar (site, test, window, strength, kind counts); decision per kind; scoreboard with
  pass/fail cells and 90 % CI; fit chart as small multiples with shared zoom (ECharts via `charts.js`), overlay mode;
  episodes as swimlanes; Calibrate: sweep chart, knob pills redraw the band, "Choose" → POST choices.
- Key picker: reuse `keypicker.js` as Baseline does.
- Laptop extras: Test A recall/delay columns, Test C and the A vs C flag.
- Manual check list in the PR: every method draws on one key; zoom to 1-min around an episode; choose a knob.

---

## Task 16: Compose lab service, run commands, docs

**Files:** `docker-compose.yml` (`logai2-lab`, `profiles: [lab]`, `mem_limit` from the preflight), `README.md`,
`ARCHITECTURE.md` (lab section), `docs/BUGS.md`.

- Compose service uses `logai2-lab:<tag>`, the same DB DSN, `--network` default, no ES env.
- README (single short lines): build the lab image; `docker compose --profile lab run --rm logai2-lab python -m scripts.lab.synth --mode counts --scenario default`;
  `... python -m scripts.lab.run --site laptop --test B --schema lab_s_default --days 28`; Test A; Test C after an
  `incident_test.py` run; stop linker/bge-m3 before heavy runs.

---

## Task 17: Laptop proof

**Files:** `docs/validation/2026-11-xx-bakeoff-laptop-proof.md`, `agents.md`.

1. Synthetic `counts` worlds of 7, 14 and 28 days: full roster, Tests A and B. Record run time and peak memory per
   method vs the spec's estimates.
2. One 14-day `engine`-mode run: parity (task 4 rule) holds on it.
3. Real laptop data: Test B on the recorded days + Test C after one `incident_test.py` run; the A vs C gap.
4. Checks: forecasters learn the planted weekly pattern and holiday (interval score on holiday days vs σ);
   each planted incident type is caught by at least the method designed for it; the page draws every method.
5. BUGS.md entries for anything found. Then a separate plan for the company deployment (k8s Job, read-only role,
   company smoke) using the proven code.
