# Task bk03: Lab foundation (data model, loader, kinds, method contract, runner, storage)

Implementer: Sonnet 5.5 — the contracts every later lab task builds on.
Reviewer: Opus 5.5 (fresh).
Branch: task/bk03-lab-foundation (worktree ../logai2-task-bk03, created by the tech lead)
PR file: .team/prs/task-bk03-lab-foundation.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first (ignore its Gemini parts: only Claude models are used).
If anything below is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
The lab runs alerting methods walk-forward over recorded per-minute counts and stores results for the Evaluation page.
This task builds the skeleton every later task plugs into: how data is loaded and held, which key is which monitor
kind, what a method must implement, how a run is driven and stored. No real method and no scoring yet (bk04, bk05).

## Read first
- Spec: `docs/specs/2026-10-27-bakeoff-laptop.md` §1, §3, §5 (all), §6.1–6.3, §10, §11.
- Plan: `docs/plans/2026-10-27-bakeoff-laptop.md` "Task 3" (the interfaces are written there; this brief wins where they differ).
- Code: `logai2/owner/baseline.py` (`FLOOR_LEVEL`, `KEY_LEVEL`, `judge`), `logai2/owner/detect.py` (`step`, `AlertRow`),
  `logai2/links.py` (`ALERTING_SOURCES`, `level_class`), `logai2/timeutil.py`, `logai2/config.py`, `logai2/db.py`
  (`connect`), `logai2/migrations/010_lab.sql` (bk01), `tests/conftest.py` (`conn`, `schema`, `cfg` fixtures).

## Files
- Create: `scripts/lab/__init__.py`, `scripts/lab/data.py`, `scripts/lab/load.py`, `scripts/lab/kinds.py`,
  `scripts/lab/methods/__init__.py`, `scripts/lab/run.py`, `scripts/lab/store.py`,
  `tests/lab/test_lab_load.py`, `tests/lab/test_lab_kinds.py`, `tests/lab/test_lab_run.py`
- Do **not** create `tests/lab/__init__.py`: `tests/` has none and a root `lab/` directory will exist (scenarios);
  lab test files are named `test_lab_*.py` so basenames stay unique.
Touching any other file needs a QUESTION first.

## Interfaces (later tasks rely on these exact names)
### `scripts/lab/data.py`
```python
Key = tuple[str, str]
UNOBSERVED = -1
MIX = "*mix"                     # the M7 pseudo-key (one per service)

def slot_of(minute: int) -> tuple[date, int]       # local day, 0..1439 (via timeutil.local)
def minute_at(day: date, slot: int) -> int          # inverse of slot_of

@dataclass
class ServiceData:
    service: str
    days: list[date]                                  # contiguous local days of the per-minute window
    minutes: dict[str, dict[date, array]]             # key -> day -> array('i', 1440); UNOBSERVED where not observed
    observed: dict[date, array]                       # 1 observed / 0 not, from '*minutes'
    hourly: dict[tuple[str, date, int], tuple[int, int]]   # (key, day, hour) -> (sum, sumsq); goes back history_days
    n_minutes: dict[tuple[date, int], int]            # service_hours
    committed: set[date]
    excluded: set[tuple[date, int]]                   # Review 'exclude' hours (day, hour)
    levels: dict[str, str]                            # template_id -> level
    first_seen: dict[str, int]                        # template_id -> minute
    docs: dict[str, list[str]]                        # 'doc:<id>' -> linked template ids

    def count(self, key: str, minute: int) -> int     # UNOBSERVED when unknown
    def keys(self) -> list[str]                       # every key with a per-minute array, doc keys included
```
- A key's array holds the recorded count where the minute is observed, `0` where observed and absent, `UNOBSERVED`
  where `*minutes` is 0 or missing.
- `doc:` arrays: element-wise sum of the linked templates (a template without a row counts 0 on observed minutes);
  `UNOBSERVED` stays `UNOBSERVED`.

### `scripts/lab/load.py`
```python
def services(conn) -> list[str]
def load_service(conn, service: str, window_from: date, window_to: date, history_days: int) -> ServiceData
```
Reads only (`lab_minute_counts`, `hourly_counts`, `service_hours`, `day_status`, `review_exclusions`, `templates`,
`template_doc_links` with `source = ANY(ALERTING_SOURCES)` and `doc_id IS NOT NULL`). `window_to` inclusive.
`hourly`, `n_minutes`, `committed`, `excluded` go back to `window_from - history_days`. Unqualified table names: the
caller's `search_path` picks the schema.

### `scripts/lab/kinds.py`
```python
def assign(sd: ServiceData, tune_days: list[date]) -> dict[str, str]   # key -> 'M1'..'M5'; absent = not scored
def is_new(sd: ServiceData, key: str, minute: int) -> bool             # M6: template and minute - first_seen < 1440
KINDS = ('M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7')
```
Rules (spec §5.3), computed on `tune_days` observed minutes: `*total` M1; `*error` M2; `doc:*` M5; template with mean
≥ 2/min over its busy hours (hours with count > 0) M3; ERROR/WARN template (by `level_class`) with mean < 2/min M4;
not scored: INFO < 2/min, `*warn`, `*info`, `*minutes`, keys ending `:unparsed`. M6 is not a key kind: a template's
minutes where `is_new` is true are scored as M6, its other minutes as its base kind. M7 = `MIX` (one per service).
A template with no observed minute in the tune days gets its base kind from its level (ERROR/WARN → M4, else not scored).

### `scripts/lab/methods/__init__.py`
```python
@dataclass(frozen=True)
class Spec:
    name: str; number: str; label: str; kinds: frozenset[str]; knobs: tuple[float, ...]; default_knob: float
    band: bool; heavy: bool; cost: int; min_hour_days: int = 0; min_minute_days: int = 0

class DayOut(NamedTuple):
    expected: array | None; low: array | None; high: array | None   # 'd' arrays of 1440, band methods
    score: array                                                     # 'd' 1440
    high_flag: bytearray                                             # 1440, 1 = high after D4

class Method(Protocol):
    spec: Spec
    def fit(self, sd: ServiceData, day: date, cfg: AppConfig) -> object | None: ...
    def day(self, model, prev_model, sd: ServiceData, key: str, day: date, knobs, cfg: AppConfig) -> dict[float, DayOut]: ...

class Unavailable(Exception): ...          # a heavy method whose package is missing

def apply_d4(count, low, high, expected, key, level, cfg) -> bool
def floor_for(key: str, level: str, cfg) -> int | None
METHODS: dict[str, Callable[[], Method]]   # empty in this task; bk04+ register
GROUPS = {'core': [...], 'heavy': [...]}  # names; empty lists in this task
def select(arg: str) -> list[str]          # 'core' | 'heavy' | 'all' | 'a,b,c' -> names; unknown name -> ValueError
```
- `fit` sees only data strictly before `day`. `day()` receives the previous day's model too (σ needs it for minutes
  before the 02:00 commit; others ignore it).
- `apply_d4` (spec §6.2): high when `count > high` and `count >= floor` (floor from `floor_for`: the level as
  `judge` maps it — `KEY_LEVEL.get(key, level.upper())` then `FLOOR_LEVEL`; `cfg.floors[...]`; None = no floor,
  i.e. INFO keys need no floor), or for `key == "*total"` only, `count < low` and `expected >= cfg.drop.min_mean`.
  For score methods the caller passes `high = threshold` and `count = score`; document this in the docstring.

### `scripts/lab/store.py`
```python
def start_run(conn, *, site, test, code_version, image_tag, data_schema, window_from, window_to, tune_to, seed, params) -> int
def finish_run(conn, run_id, status: str, kinds: dict | None = None, error: str | None = None) -> None
def put_result(conn, run_id, method, kind, scope, knob, status, metrics: dict, verdict=None, strength=None) -> None
def put_episodes(conn, run_id, method, service, key, episodes) -> None      # rows (start, end, peak, alerted)
def put_bands(conn, run_id, method, service, key, step, rows) -> None        # rows (minute, count, expected, low, high, flag)
def put_sweep(conn, run_id, method, kind, knob, part, metrics) -> None
def prune(conn, site: str, keep: int = 5) -> int                             # delete older runs of the site
```
Each writes in its own `with conn.transaction()`. Unqualified names: the results connection's `search_path` picks the schema.

### `scripts/lab/run.py`
CLI: `python -m scripts.lab.run --site laptop|company --test A|B|C --methods core|heavy|all|<list>
(--days N | --from YYYY-MM-DD --to YYYY-MM-DD) [--schema S] [--results-schema R] [--seed 7] [--check] [--dsn DSN]`.
- `--site company` with `--test A` or `C`: print an error and exit 2 **before** connecting.
- `--check`: import every selected method factory; print `name: available | unavailable (<reason>)`; exit 0 when at
  least one is available; writes nothing; does not connect.
- `--days N` = the N local days ending yesterday (today's local date from `timeutil`).
- Two connections: data (`search_path` = `--schema` default `public`, then `public`; transaction READ ONLY) and results
  (`search_path` = `--results-schema` default `public`).
- Split: with ≥ 14 days, tune = first half of the days (`len // 2`), test = the rest; below 14, all days are both, and
  keys are split by `crc32(service + key) % 2` (0 tune, 1 test). `params.split` records which.
- Flow: `start_run` → for each service: `load_service` → `assign` → for each method: walk-forward over `days`
  (`fit(sd, d)` per day; `day(...)` per scored key with the method's full knob grid; a `fit` returning None → status
  `skipped` for that method/kind) → pass the per-service outputs to `collect(run_id, service, method, outputs)`, a hook
  that in this task stores only `put_result(... status, metrics={"n_keys": .., "n_minutes": ..})` per kind (bk05/bk07
  replace it) → `finish_run(ok)` → `prune(site)`.
- A method raising on a service: `put_result(status='failed', metrics={'error': repr(exc)})` for its kinds; continue.
- Any other exception: `finish_run(failed, error=...)`, exit 1.
- `code_version`: `git rev-parse --short HEAD` in the repo dir, else env `LOGAI2_CODE_VERSION`, else `unknown`.
- Run lifecycle function `run(args, cfg, data_conn, results_conn, collect=default_collect) -> int` so tests call it
  without a subprocess.

## Requirements
1. Everything above, pure Python (stdlib `array`, `math`, `statistics`, `zlib`); no numpy.
2. The loader never writes; `load_service` works inside a READ ONLY transaction.
3. Memory: only one `ServiceData` alive at a time in `run`.
4. No change to any file outside the list.

## Tests (write first, see them fail)
- `test_lab_load.py` (fixture `conn`): insert by SQL two days of `lab_minute_counts` for one service (three keys,
  `*minutes` with one hour missing), `hourly_counts`/`service_hours`/`day_status` from 3 earlier days, one
  `review_exclusions` exclude, two templates with levels and `first_seen`, one doc link (source `manual`) over two
  templates and one with source `draft` (must be ignored) →
  `load_service` returns the exact arrays (`UNOBSERVED` in the missing hour, 0 for observed silent minutes),
  the doc array equals the element-wise sum, history fields go back `history_days`, `excluded` has the hour; it runs
  inside `SET TRANSACTION READ ONLY`.
- `test_lab_kinds.py`: hand-made `ServiceData` with one key per kind → `assign` gives M1–M5 and leaves the unscored
  keys out; `is_new` is true for exactly 1440 minutes after `first_seen`; `slot_of`/`minute_at` round-trip across a
  local midnight.
- `test_lab_run.py`: `--site company --test A` → exit 2 and no `lab_runs` row; `--check` with no methods registered
  prints nothing available and exits 1, writes nothing; a run with a dummy constant-band method registered **in the
  test** (monkeypatch `METHODS`) writes one `lab_runs` row (status ok, params with split mode), `lab_results` rows per
  kind with `n_keys`; a method raising gives status `failed` and the run still ends ok; `prune` keeps the 5 newest runs
  per site and cascades; `apply_d4` truth table (floors by level, INFO no floor, drop only on `*total` and only when
  expected ≥ 20).

## Done when
- [ ] new tests fail before the code and pass after (RED/GREEN in the PR file)
- [ ] `.venv/bin/pytest -q` passes, output clean (report the count)
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`), plus a list of any deviations
