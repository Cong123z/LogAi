# logai2 Core Engine + Web v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: **implemented and merged into `master`** (`b1fe4d3`, 2026-10-21). ARCHITECTURE.md describes the result.

**Goal:** A Docker Compose stack (Postgres, engine, web) that reads logs from
Elasticsearch, builds per-service Drain3 templates, keeps hourly count
history, commits a daily mean ± kσ baseline, raises alerts per (service,
document), and shows them in a web console with logai-engine's look.

**Architecture:** One engine process: a reader (ES → normalize → known-line
cache → minute aggregates) hands a plain dict to an owner (Drain3 on misses →
open minutes → close → detect → one Postgres transaction per flush that also
stores the ES cursors). The web is a separate Flask process that only talks
to Postgres; buttons write rows to `commands`, which the engine polls.

**Tech Stack:** Python ≥ 3.11, psycopg 3, Flask 3, drain3 0.9.11,
elasticsearch 8 client, prometheus-client, PyYAML, pytest; Postgres 16
(`pgvector/pgvector:pg16`); vanilla JS front end copied from logai-engine.

**Spec:** [`docs/specs/2026-10-20-core-engine-design.md`](../specs/2026-10-20-core-engine-design.md)
(source design: `docs/design/per-service-baseline.md`). Read the spec first.

**Source repo for copied code:** `~/Documents/logai-engine` (called
*logai-engine* below). "Copy" means copy the named functions with their
comments, then trim what this plan says to trim.

## Global Constraints

- Repo `~/Documents/logai2`; work on branch `core-engine-v1` off `master`.
- Never add `Co-Authored-By` or any Claude attribution to commits.
- Package name `logai2`; env vars prefixed `LOGAI2_`.
- Timezone `Asia/Ho_Chi_Minh` for every day/hour bucket, applied only through `logai2/timeutil.py`.
- Template IDs `"{service}:T{cluster_id:05d}"`.
- Service keys `*total` (all events) and `*error` (ERROR, FATAL, CRITICAL); document keys `doc:{doc_id}`.
- Floors: ERROR+ 5/min, WARN 10/min, INFO never. Drop alerts only on `*total`, mean ≥ 20, day type ≥ 5 committed days.
- k = 3.0, young k = 4.0 while the day type has < 5 committed days; band = wider of this hour's and next hour's.
- Baseline days: last 5 weekdays, last 4 weekend days (Sat/Sun); weekend with < 2 days → weekday profile, upper only.
- Alert hysteresis copied from logai-engine `AlertStateMachine._transition` (warm 2, alert 3, cool 3).
- Lines truncated to 8,192 **bytes** (UTF-8 safe) before normalize; ES query upper bound `now - 30 s`; page size 10,000.
- Minutes closed more than 15 min after their end are historic: counted, never alert.
- No ORM; migrations are numbered `.sql` files; SQL uses no schema-qualified names (tests run in a throwaway schema).
- Web never connects to Elasticsearch and never returns the ES password.
- Prometheus never gets a per-template series.

## Review Focus

1. **A failing or deleted index** must not stall the watermark forever: after 3 consecutive failed polls it stops holding the watermark (and shows as failing on Pipeline) — test in Task 4.
2. **An index removed from the selection** drops its cursor and stops holding the watermark at once — test in Task 4.
3. **Catch-up from `days_back`** writes into days whose partitions must exist: partitions are created from the bootstrap day, not from today — test in Task 9.
4. **Engine down at 02:00:** the next start commits every finished uncommitted day — test in Task 7.
5. **Multibyte lines** (Vietnamese text) truncated by bytes must not raise or split a character — test in Task 3.

---

## File map

```
logai2/
  __init__.py
  config.py          AppConfig dataclasses, load_config()
  db.py              connect(), migrate(), ensure_partitions(), drop_partitions_before(), get/put_setting()
  timeutil.py        epoch-minute ↔ local day/hour/daytype
  migrations/001_init.sql
  reader/
    __init__.py
    normalize.py     copied preprocessor (normalize, find_level, PLACEHOLDER)
    es_source.py     EsSource (search + paging + hit → Line), copied helpers
    cache.py         KnownLines LRU
    reader.py        Reader → MinuteBatch
  owner/
    __init__.py
    miners.py        Miners: Drain3 per service, blob state
    counts.py        OpenMinutes, ClosedMinute
    baseline.py      Stat, Lookup, Baseline, judge()
    detect.py        AlertRow, step(), Detector
    owner.py         Owner.handle(), Owner.flush()
  jobs/__init__.py
  jobs/commit.py     commit()
  alerts.py          alert_rows() — shared by web and metrics
  review.py          suggest() — non-NORMAL periods → whole-hour ranges
  commands.py        enqueue(), take(), finish()
  metrics.py         Metrics
  engine.py          Engine (main loop, bootstrap, commands, timers)
  web/
    __init__.py
    app.py           create_app()
    static/          copied shell + 6 views
scripts/run_engine.py, scripts/run_web.py, scripts/incident_test.py
tests/conftest.py and tests/test_*.py
requirements.txt, requirements-dev.txt, Dockerfile, docker-compose.yml, config.yaml, README.md
```

---

### Task 1: Skeleton, config, database and schema

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `.gitignore`, `docker-compose.yml` (postgres only for now), `config.yaml`, `logai2/__init__.py`, `logai2/config.py`, `logai2/db.py`, `logai2/timeutil.py`, `logai2/migrations/001_init.sql`
- Test: `tests/conftest.py`, `tests/test_db.py`, `tests/test_timeutil.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `load_config(path: str | None = None) -> AppConfig` with sections `elasticsearch` (`hosts: list[str]=["http://elasticsearch:9200"]`, `username=""`, `password=""`, `verify_certs=True`, `index="app-logs-*"`, `batch_size=10000`, `poll_interval_seconds=1.0`, `index_refresh_seconds=60`), `database` (`dsn="postgresql://logai:logai@postgres:5432/logai"`), `reader` (`module_field="moduleCode"`, `cache_size=200000`, `max_line_bytes=8192`, `upper_bound_seconds=30`), `flush` (`seconds=2.0`, `max_events=50000`), `start` (`days_back=2`), `baseline` (`timezone="Asia/Ho_Chi_Minh"`, `commit_time="02:00"`, `sigma_multiplier=3.0`, `young_sigma_multiplier=4.0`, `full_after_days=5`, `historic_after_minutes=15`), `floors: dict={"ERROR": 5, "WARN": 10}`, `drop` (`min_mean=20.0`), `retention` (`counts_days=90`, `alert_changes_days=365`), `alert` (`warm_consecutive=2`, `alert_consecutive=3`, `cool_consecutive=3`), `drain3` (`sim_threshold=0.6`, `depth=4`, `max_children=100`), `metrics` (`host="0.0.0.0"`, `port=9108`), `web` (`host="0.0.0.0"`, `port=5555`), `documentation` (`seed_path=""`). Layering: defaults → YAML (default `config.yaml`, missing file OK) → env `LOGAI2_DATABASE_DSN`, `LOGAI2_ES_HOSTS` (comma list), `LOGAI2_ES_USER`, `LOGAI2_ES_PASSWORD`, `LOGAI2_ES_VERIFY_CERTS`, `LOGAI2_ES_INDEX`, `LOGAI2_METRICS_PORT`, `LOGAI2_WEB_PORT`, `LOGAI2_DOC_SEED_PATH`. Copy `_merge_dataclass` from logai-engine `logai/config.py`.
  - `db.connect(dsn: str) -> psycopg.Connection` (dict rows, autocommit off).
  - `db.migrate(conn) -> list[int]` — applies `migrations/NNN_*.sql` not yet in `schema_migrations`, in order, each in its own transaction; returns applied versions.
  - `db.ensure_partitions(conn, first: date, last: date) -> None` — daily partitions `service_hours_YYYYMMDD`, `hourly_counts_YYYYMMDD`, `alert_minutes_YYYYMMDD` for every day in `[first, last]`, idempotent.
  - `db.drop_partitions_before(conn, table: str, day: date) -> list[str]` — drops partitions of `table` whose day < `day`, returns names.
  - `db.get_setting(conn, key) -> dict | None`, `db.put_setting(conn, key, value: dict, revision: str | None = None) -> str` — returns the new revision (uuid4 hex); raises `db.StaleRevision(current)` when `revision` is given and differs.
  - `timeutil.TZ`, `timeutil.local(minute: int) -> datetime` (minute = UTC epoch seconds // 60), `timeutil.day_hour(minute) -> tuple[date, int]`, `timeutil.daytype(day: date) -> str` (`"weekend"` for Sat/Sun else `"weekday"`), `timeutil.minute_of(ts: float) -> int`.

- [ ] **Step 1: Create the branch and dependency files**

```bash
cd ~/Documents/logai2 && git checkout -b core-engine-v1
```
`requirements.txt`: `drain3==0.9.11`, `elasticsearch>=8.15.0,<9.0.0`, `prometheus-client>=0.20.0`, `PyYAML>=6.0.2`, `flask>=3.0.0`, `psycopg[binary]>=3.2`. `requirements-dev.txt`: `pytest>=8`. `docker-compose.yml`: one `postgres` service, image `pgvector/pgvector:pg16`, user/password/db `logai`, port `5433:5432`, named volume `logai2-pg`. Then:

```bash
docker compose up -d postgres
docker compose exec postgres createdb -U logai logai_test
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

- [ ] **Step 2: Write `tests/conftest.py`**

Fixture `conn`: connect to `LOGAI2_TEST_DSN` (default `postgresql://logai:logai@localhost:5433/logai_test`), `CREATE SCHEMA t_<uuid8>`, `SET search_path TO t_<uuid8>`, commit, `migrate(conn)`, `ensure_partitions(conn, date(2026,10,1), date(2026,10,31))`, yield, then `DROP SCHEMA … CASCADE`. Fixture `cfg`: `load_config(None)` with no YAML.

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_db.py
def test_migrate_is_idempotent(conn):
    assert migrate(conn) == []

def test_tables_exist(conn):
    names = {r["tablename"] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
    assert {"services", "templates", "hourly_counts_20261015", "baseline",
            "alert_state", "documents", "commands", "worker_status"} <= names

def test_partitions_idempotent_and_droppable(conn):
    ensure_partitions(conn, date(2026, 10, 1), date(2026, 10, 2))   # already there: no error
    dropped = drop_partitions_before(conn, "hourly_counts", date(2026, 10, 3))
    assert dropped == ["hourly_counts_20261001", "hourly_counts_20261002"]

def test_setting_revision_conflict(conn):
    rev = put_setting(conn, "x", {"a": 1})
    put_setting(conn, "x", {"a": 2}, revision=rev)
    with pytest.raises(StaleRevision):
        put_setting(conn, "x", {"a": 3}, revision=rev)
    assert get_setting(conn, "x") == {"a": 2}

# tests/test_timeutil.py
def test_local_day_hour_crosses_midnight():
    # 2026-10-17 17:30 UTC = 2026-10-18 00:30 local (UTC+7), a Sunday
    m = minute_of(datetime(2026, 10, 17, 17, 30, tzinfo=timezone.utc).timestamp())
    assert day_hour(m) == (date(2026, 10, 18), 0)
    assert daytype(date(2026, 10, 18)) == "weekend"
    assert daytype(date(2026, 10, 19)) == "weekday"

# tests/test_config.py
def test_env_overrides_yaml(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"; p.write_text("start:\n  days_back: 5\nfloors:\n  ERROR: 7\n")
    monkeypatch.setenv("LOGAI2_ES_HOSTS", "http://a:9200,http://b:9200")
    cfg = load_config(str(p))
    assert cfg.start.days_back == 5 and cfg.floors["ERROR"] == 7 and cfg.floors["WARN"] == 10
    assert cfg.elasticsearch.hosts == ["http://a:9200", "http://b:9200"]
```

- [ ] **Step 4: Run them to verify they fail**

Run: `.venv/bin/pytest tests -q`
Expected: FAIL (`ModuleNotFoundError: logai2`).

- [ ] **Step 5: Write `logai2/migrations/001_init.sql`**

This is the schema the spec fixes; column types are decided here:

```sql
CREATE TABLE schema_migrations (version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE services (service text PRIMARY KEY, partition int NOT NULL);
CREATE TABLE modules (module_code text PRIMARY KEY, service text NOT NULL REFERENCES services);
CREATE TABLE es_cursors (index text PRIMARY KEY, since_ms bigint NOT NULL, floor_ms bigint NOT NULL,
                         updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE templates (service text NOT NULL, template_id text NOT NULL, level text NOT NULL,
                        text text NOT NULL, first_seen timestamptz NOT NULL, last_seen timestamptz NOT NULL,
                        PRIMARY KEY (service, template_id));
CREATE TABLE drain3_state (service text PRIMARY KEY, blob bytea NOT NULL, version int NOT NULL DEFAULT 1);
CREATE TABLE service_hours (service text NOT NULL, day date NOT NULL, hour smallint NOT NULL,
                            n_minutes int NOT NULL, PRIMARY KEY (service, day, hour)) PARTITION BY RANGE (day);
CREATE TABLE hourly_counts (service text NOT NULL, key text NOT NULL, day date NOT NULL, hour smallint NOT NULL,
                            sum bigint NOT NULL, sumsq bigint NOT NULL,
                            PRIMARY KEY (service, key, day, hour)) PARTITION BY RANGE (day);
CREATE TABLE alert_minutes (service text NOT NULL, key text NOT NULL, module_code text NOT NULL,
                            day date NOT NULL, minute timestamptz NOT NULL, count int NOT NULL,
                            PRIMARY KEY (service, key, module_code, minute, day)) PARTITION BY RANGE (day);
CREATE TABLE day_status (service text NOT NULL, day date NOT NULL, committed boolean NOT NULL DEFAULT false,
                         daytype text NOT NULL, PRIMARY KEY (service, day));
CREATE TABLE review_exclusions (id bigserial PRIMARY KEY, service text NOT NULL, day date NOT NULL,
                                hour_from smallint NOT NULL, hour_to smallint NOT NULL,   -- [from, to)
                                decision text NOT NULL CHECK (decision IN ('exclude', 'keep')),
                                created_by text NOT NULL DEFAULT '', created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE baseline (service text NOT NULL, key text NOT NULL, daytype text NOT NULL, hour smallint NOT NULL,
                       mean double precision NOT NULL, std double precision NOT NULL, days int NOT NULL,
                       PRIMARY KEY (service, key, daytype, hour));
CREATE TABLE alert_state (service text NOT NULL, key text NOT NULL, state text NOT NULL, since timestamptz NOT NULL,
                          streak int NOT NULL, score double precision NOT NULL, count int NOT NULL,
                          band_low double precision, band_high double precision, PRIMARY KEY (service, key));
CREATE TABLE alert_changes (service text NOT NULL, key text NOT NULL, at timestamptz NOT NULL,
                            from_state text NOT NULL, to_state text NOT NULL, count int NOT NULL,
                            band_low double precision, band_high double precision, PRIMARY KEY (service, key, at));
CREATE TABLE documents (doc_id text PRIMARY KEY, title text NOT NULL, error_code text NOT NULL DEFAULT '',
                        body text NOT NULL, revision text NOT NULL,
                        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE template_doc_links (service text NOT NULL, template_id text NOT NULL,
                                 doc_id text NOT NULL REFERENCES documents,
                                 source text NOT NULL DEFAULT 'manual', linked_at timestamptz NOT NULL DEFAULT now(),
                                 PRIMARY KEY (service, template_id));
CREATE TABLE error_samples (service text NOT NULL, template_id text NOT NULL, at timestamptz NOT NULL, line text NOT NULL);
CREATE INDEX error_samples_key ON error_samples (service, template_id, at DESC);
CREATE TABLE settings (key text PRIMARY KEY, value jsonb NOT NULL, revision text NOT NULL,
                       updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE commands (id bigserial PRIMARY KEY, kind text NOT NULL, payload jsonb NOT NULL DEFAULT '{}',
                       created_at timestamptz NOT NULL DEFAULT now(), done_at timestamptz, result jsonb);
CREATE TABLE worker_status (worker text PRIMARY KEY, status jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
```

(Plan detail vs spec: `es_cursors` stores `since_ms`/`floor_ms` instead of a `sort_values` list, because the persisted cursor is always a minute boundary; `review_exclusions.decision` records "keep" too, so reviewed suggestions disappear.)

- [ ] **Step 6: Implement `config.py`, `db.py`, `timeutil.py`** to the signatures above. `migrate` reads `schema_migrations` only if the table exists (`to_regclass('schema_migrations')`). Partition DDL: `CREATE TABLE IF NOT EXISTS {t}_{YYYYMMDD} PARTITION OF {t} FOR VALUES FROM ('{d}') TO ('{d+1}')`. `drop_partitions_before` lists children via `pg_inherits` and parses the date suffix.

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/pytest tests -q`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add -A && git commit -m "Add config, Postgres schema, migrations and partitions"
```

---

### Task 2: Copied text processing (normalize) and its tests

**Files:**
- Create: `logai2/reader/__init__.py`, `logai2/reader/normalize.py`
- Test: `tests/test_normalize.py`, `tests/test_log_masking.py`

**Interfaces:**
- Produces: `normalize(message: str, max_chars=8192, max_tokens=40, level_search_tokens=5) -> str`, `find_level(message, search_tokens=5) -> tuple[str, int] | None`, `PLACEHOLDER`.

- [ ] **Step 1: Copy the tests** — `tests/test_preprocessor.py` → `tests/test_normalize.py` and `tests/test_log_masking.py` from logai-engine; change imports to `logai2.reader.normalize`. Drop any test that needs `Drain3Parser` or other logai-engine modules (they come back in Task 5).

- [ ] **Step 2: Run them to verify they fail** — `.venv/bin/pytest tests/test_normalize.py tests/test_log_masking.py -q` → FAIL (import error).

- [ ] **Step 3: Copy `logai/parsing/preprocessor.py`** to `logai2/reader/normalize.py` unchanged apart from imports.

- [ ] **Step 4: Run tests** → PASS.

- [ ] **Step 5: Commit** — `git commit -am "Copy log normalizer and its tests"` (add new files first).

---

### Task 3: Elasticsearch source (search, paging, hit → Line)

**Files:**
- Create: `logai2/reader/es_source.py`
- Test: `tests/test_es_source.py`

**Interfaces:**
- Consumes: `EsConfig`, `ReaderConfig` (Task 1); `find_level` (Task 2).
- Produces:
  - `Line = NamedTuple(ts: float, service: str, level: str, message: str, module: str, doc_id: str)`
  - `hit_to_line(hit: dict, module_field: str, max_line_bytes: int) -> Line | None` — `None` for a hit without `_id` or `_source` (counted by the caller as malformed).
  - `class EsSource(cfg: EsConfig, reader_cfg: ReaderConfig)` with `reconnect(cfg: EsConfig) -> None`, `page(index: str, from_ms: int, upper_ms: int) -> list[dict]` (raw hits sorted by `@timestamp`, `_doc`; filter `@timestamp >= from_ms AND <= upper_ms`, `epoch_millis`; `_source` limited to `@timestamp, service, service_code2, level, log.level, message, <module_field>`; `filter_path` `hits.hits._id,hits.hits._source,hits.hits.sort`), `resolve(patterns: list[str]) -> dict[str, list[str]]`, `list_indices() -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
def hit(_id, ms, **src):
    return {"_id": _id, "sort": [ms, 1], "_source": {"@timestamp": ms, **src}}

def test_hit_to_line_fields():
    line = hit_to_line(hit("a", 1_760_000_000_000, service_code2="lixi",
                           message="2026-10-18 ERROR boom", moduleCode="M1"), "moduleCode", 8192)
    assert (line.service, line.level, line.module, line.doc_id) == ("lixi", "ERROR", "M1", "a")
    assert line.ts == 1_760_000_000.0

def test_missing_module_and_service():
    line = hit_to_line(hit("a", 1_760_000_000_000, message="x"), "moduleCode", 8192)
    assert (line.service, line.module, line.level) == ("unknown", "", "INFO")

def test_multibyte_truncation_is_safe():
    msg = "Lỗi kết nối " * 2000
    line = hit_to_line(hit("a", 1_760_000_000_000, message=msg), "moduleCode", 8192)
    assert len(line.message.encode("utf-8")) <= 8192
    assert msg.startswith(line.message)

def test_malformed_hit_is_none():
    assert hit_to_line({"_source": {}}, "moduleCode", 8192) is None

def test_page_reads_whole_millisecond(monkeypatch):
    # copy the "millisecond fills the page" case from logai-engine
    # tests/test_es_index_selection.py: a page whose last hit sits on from_ms
    # is re-requested with size 10,000.
```

Also copy `tests/test_es_malformed_hits.py` and `tests/test_es_retry_classification.py` from logai-engine, rewritten against `hit_to_line` / `EsSource._search`.

- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest tests/test_es_source.py -q` → FAIL.

- [ ] **Step 3: Implement `es_source.py`** — copy from logai-engine `logai/collector/es_collector.py`: `_build_client`, `_extract_service`, `_extract_level_from_message`, `_extract_level`, `_coerce_message`, `_parse_timestamp`, `_ES_NON_RETRYABLE`, `_SORT`, `_WHOLE_MILLISECOND`, `_search` (retry/backoff), `_page` (whole-millisecond logic), `resolve`, `list_indices`. Drop `CheckpointStore`, `RawLog`, metadata/noise keys, host extraction and the legacy cursor. Truncate with `message.encode("utf-8")[:max_line_bytes].decode("utf-8", "ignore")`.

- [ ] **Step 4: Run tests** → PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add Elasticsearch source copied from logai-engine collector"`

---

### Task 4: Reader (cursors, dedup, cache, MinuteBatch)

**Files:**
- Create: `logai2/reader/cache.py`, `logai2/reader/reader.py`
- Test: `tests/test_reader.py`, `tests/fakes.py` (FakeSource)

**Interfaces:**
- Consumes: `EsSource.page` (Task 3), `normalize` (Task 2), `timeutil.minute_of` (Task 1).
- Produces:
  - `KnownLines(cap: int)`: `get(service, text) -> str | None`, `put(service, text, template_id)`, `hits: int`, `misses: int` (LRU via `OrderedDict`).
  - `MinuteBatch` (TypedDict): `counts: list[Count]`, `watermark_ms: int`, `events: int`, `positions: dict[str, dict]` (`{"pos_ms": int, "caught_up": bool, "failing": bool}`).
  - `Count` (TypedDict): `service, template_id (str|None), text (str|None), level, module, minute (int), n (int), sample (str)`.
  - `class Reader(source, cfg: AppConfig, cache: KnownLines, now=time.time)`:
    - `set_indices(floors: dict[str, int], saved: dict[str, int]) -> None` — `floors`: index → floor ms for every selected index; `saved`: index → `since_ms` from `es_cursors`. Position = `saved` if present else floor. Indices not in `floors` are dropped (position and dedup ids).
    - `poll() -> MinuteBatch` — one `page()` per index from its position; dedup by `_id` within the ids kept since the last `forget_before`; normalize; cache lookup; aggregate.
    - `learn(resolved: dict[tuple[str, str], str]) -> None` → cache puts.
    - `cursors(boundary_ms: int) -> dict[str, tuple[int, int]]` — index → `(since_ms, floor_ms)` with `since_ms = max(boundary_ms, floor_ms)`.
    - `forget_before(boundary_ms: int) -> None` — drop dedup ids of docs before `boundary_ms`.
    - `floors: dict[str, int]` (read-only view).

Rules (decided here):
- `upper_ms = (now() - upper_bound_seconds) * 1000`.
- Position after a non-empty page = last hit's `@timestamp` ms; `caught_up` = page returned no new (non-duplicate) docs.
- `watermark_ms = min(pos_ms if not caught_up else upper_ms)` over indices that are not `failing`; with no healthy index, `watermark_ms = upper_ms`.
- An index is `failing` after 3 consecutive exceptions from `page()`; one success clears it. Exceptions are logged, never raised.
- A miss (`cache.get` is None) is aggregated by `(service, text, level, module, minute)` with `template_id=None`; hits by `(service, template_id, level, module, minute)` with `text=None`. `sample` = first raw message of that aggregate.

- [ ] **Step 1: Write `tests/fakes.py`** — move the `hit(_id, ms, **src)` helper from `tests/test_es_source.py` here; `FakeSource(hits_by_index: dict[str, list[dict]])` whose `page(index, from_ms, upper_ms)` returns hits with `from_ms <= ts <= upper_ms`, sorted, at most 10,000; `fail(index)` makes the next `page()` raise.

- [ ] **Step 2: Write the failing tests**

```python
def test_aggregates_and_dedups(cfg):
    src = FakeSource({"a": [hit("1", T0, service="s", message="boom 1"),
                            hit("2", T0, service="s", message="boom 2")]})
    r = Reader(src, cfg, KnownLines(10), now=lambda: T0 / 1000 + 600)
    r.set_indices({"a": T0}, {})
    b = r.poll()
    assert sum(c["n"] for c in b["counts"]) == 2 and b["events"] == 2
    assert len(b["counts"]) == 1 and b["counts"][0]["template_id"] is None   # same normalized text
    b2 = r.poll()                       # re-reads T0 ms; both ids already seen
    assert b2["counts"] == [] and b2["positions"]["a"]["caught_up"]

def test_cache_hit_sends_template_id(cfg): ...       # learn() then poll → template_id set, text None

def test_watermark_is_slowest_index(cfg): ...        # a at T0+120s, b at T0+30s → watermark = b

def test_failing_index_stops_holding_watermark(cfg):
    # b fails 3 polls in a row → positions["b"]["failing"] and watermark ignores b

def test_removed_index_dropped(cfg):
    r.set_indices({"a": T0}, {})        # b removed → not in positions, not in watermark

def test_cursors_use_boundary_but_not_before_floor(cfg):
    r.set_indices({"a": T0, "b": T0 + 600_000}, {})
    assert r.cursors(T0 + 60_000) == {"a": (T0 + 60_000, T0), "b": (T0 + 600_000, T0 + 600_000)}
```

- [ ] **Step 3: Run to verify they fail** — `.venv/bin/pytest tests/test_reader.py -q` → FAIL.

- [ ] **Step 4: Implement `cache.py` and `reader.py`** to the interfaces and rules above. Dedup ids live in `dict[index, dict[_id, ts_ms]]`; `forget_before` deletes entries with `ts_ms < boundary_ms`. Add `# ponytail: dedup ids kept for every open minute in memory; bounded by one or two minutes of docs per index, move to a ts-bucketed set if catch-up memory shows up.`

- [ ] **Step 5: Run tests** → PASS.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "Add reader: per-index positions, dedup, known-line cache, minute aggregates"`

---

### Task 5: Drain3 per service

**Files:**
- Create: `logai2/owner/__init__.py`, `logai2/owner/miners.py`
- Test: `tests/test_miners.py`, `tests/test_template_generalization.py` (copied, adapted)

**Interfaces:**
- Consumes: `Drain3Config` (Task 1), `PLACEHOLDER` (Task 2).
- Produces: `class Miners(cfg: Drain3Config, blobs: dict[str, bytes])`:
  - `match(service: str, text: str) -> tuple[str, str, bool]` — `(template_id, display_text, is_new)`; `template_id = f"{service}:T{cluster_id:05d}"`.
  - `dirty() -> dict[str, bytes]` — serialized state of services changed since the last call; clears the dirty set.

- [ ] **Step 1: Write the failing tests**

```python
def test_ids_are_per_service(cfg):
    m = Miners(cfg.drain3, {})
    a = m.match("lixi", "connect timeout host <*>")
    b = m.match("topup", "connect timeout host <*>")
    assert a[0] == "lixi:T00001" and b[0] == "topup:T00001" and a[2] and b[2]
    assert m.match("lixi", "connect timeout host <*>")[0] == "lixi:T00001"

def test_state_round_trip(cfg):
    m = Miners(cfg.drain3, {}); m.match("lixi", "a b c")
    blobs = m.dirty()
    assert set(blobs) == {"lixi"} and m.dirty() == {}
    m2 = Miners(cfg.drain3, blobs)
    assert m2.match("lixi", "a b c") == ("lixi:T00001", "a b c", False)

def test_placeholder_display(cfg):
    assert "<*>" in Miners(cfg.drain3, {}).match("s", "user <*> logged in")[1]
```

Copy logai-engine `tests/test_template_generalization.py`, driving it through `Miners.match` instead of `Drain3Parser.parse`.

- [ ] **Step 2: Run to verify they fail** → FAIL.

- [ ] **Step 3: Implement `miners.py`** — copy `DRAIN_LITERAL_PLACEHOLDER`, `display_template` and the `TemplateMinerConfig` setup from logai-engine `logai/parsing/drain3_parser.py`. Per service, an in-memory persistence object (`save_state(bytes)` stores, `load_state()` returns the blob) passed to `TemplateMiner` so it restores, then detached (`persistence_handler = None`). `dirty()` reattaches, calls `miner.save_state("flush")`, detaches. Input text is already normalized: do not normalize again.

- [ ] **Step 4: Run tests** → PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add per-service Drain3 miners with blob state"`

---

### Task 6: Open minutes and minute closing

**Files:**
- Create: `logai2/owner/counts.py`
- Test: `tests/test_counts.py`

**Interfaces:**
- Consumes: `timeutil.day_hour` (Task 1).
- Produces:
  - `ClosedMinute` (dataclass): `minute: int`, `day: date`, `hour: int`, `keys: dict[tuple[str, str], int]` (`(service, key)` → count, key = template ID, `*total`, `*error`), `modules: dict[tuple[str, str, str], int]` (`(service, template_id, module)` → count), `levels: dict[tuple[str, str], str]` (template → most severe level seen in this minute).
  - `class OpenMinutes(start_minute: int)`:
    - `add(service, template_id, level, module, minute, n) -> None` — counts for `minute < next_to_close` are dropped and counted in `late: int` (should be 0; a metric).
    - `close_before(minute_excl: int, services: Iterable[str]) -> list[ClosedMinute]` — closes every minute from `next_to_close` to `minute_excl - 1`, **including minutes with no events**.
    - `next_to_close: int` — the oldest open minute; the owner's boundary is `next_to_close * 60_000`.

Rules (decided here):
- `*error` counts levels ERROR, FATAL, CRITICAL; `*total` counts all.
- Every service in `services` gets `*total` and `*error` in every closed minute (0 when silent).
- Silent template keys: a template seen earlier in the same local `(day, hour)` gets 0 in the minutes after, until the hour changes. Keep `hour_keys: dict[(day, hour), set[(service, tid)]]`; drop older hours on close.

- [ ] **Step 1: Write the failing tests**

```python
def test_close_includes_empty_minutes_and_service_keys():
    om = OpenMinutes(start_minute=M)
    om.add("s", "s:T00001", "ERROR", "M1", M, 3)
    closed = om.close_before(M + 3, ["s", "t"])
    assert [c.minute for c in closed] == [M, M + 1, M + 2]
    assert closed[0].keys[("s", "s:T00001")] == 3
    assert closed[0].keys[("s", "*error")] == 3 and closed[0].keys[("s", "*total")] == 3
    assert closed[0].keys[("t", "*total")] == 0
    assert closed[1].keys[("s", "s:T00001")] == 0          # silent, same hour
    assert closed[0].modules[("s", "s:T00001", "M1")] == 3
    assert om.next_to_close == M + 3

def test_silent_key_forgotten_next_hour(): ...            # M at :59 → next hour's minute has no ("s","s:T00001")

def test_late_count_is_dropped():
    om = OpenMinutes(M); om.close_before(M + 1, [])
    om.add("s", "s:T1", "INFO", "", M, 1)
    assert om.late == 1
```

- [ ] **Step 2: Run to verify they fail** → FAIL.
- [ ] **Step 3: Implement `counts.py`.**
- [ ] **Step 4: Run tests** → PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add open minutes with silent-key zeros and minute closing"`

---

### Task 7: Baseline band and daily commit

**Files:**
- Create: `logai2/owner/baseline.py`, `logai2/jobs/__init__.py`, `logai2/jobs/commit.py`
- Test: `tests/test_baseline.py`, `tests/test_commit.py`

**Interfaces:**
- Consumes: `db.ensure_partitions`, `db.drop_partitions_before`, `db.put_setting`, `timeutil` (Task 1).
- Produces:
  - `Stat = NamedTuple(mean: float, std: float)`
  - `Lookup = NamedTuple(this: Stat | None, next: Stat | None, days: int, upper_only: bool)`
  - `Verdict = NamedTuple(high: bool, score: float, band_low: float | None, band_high: float | None)`
  - `class Baseline(stats: dict[tuple[str, str, str, int], Stat], days: dict[tuple[str, str], int])` with `Baseline.load(conn) -> Baseline` and `lookup(service, key, day: date, hour: int) -> Lookup`.
  - `judge(count: int, key: str, level: str, lk: Lookup, cfg: AppConfig) -> Verdict`
  - `commit(conn, cfg: AppConfig, today: date) -> dict` → `{"days_committed": int, "baseline_rows": int}`; idempotent; commits its own transaction.

`lookup` rules: `days[(service, daytype)]` = committed days of that type, capped at 5 (weekday) / 4 (weekend). Weekend with < 2 days → use the weekday profile, `upper_only=True`, `days` = weekday days. `next` = hour + 1 of the same day type (hour 23 → hour 0). No row → `None`.

`judge` rules:
- floor: `ERROR`/`FATAL`/`CRITICAL` → `floors["ERROR"]`; `WARN`/`WARNING` → `floors["WARN"]`; else none. Key `*total` uses INFO, `*error` uses ERROR, `doc:` keys use the level passed in.
- `k = sigma_multiplier` if `lk.days >= full_after_days` else `young_sigma_multiplier`.
- Both stats `None` → floor only: `high = floor is not None and count >= floor`, `score = count / floor` (0 without floor), bands `None`.
- Else `band_high = max(m + kσ)`, `band_low = min(m − kσ)` over the stats present; upper = `floor is not None and count > band_high and count >= floor`; drop = `key == "*total" and not lk.upper_only and lk.days >= full_after_days and lk.this and lk.this.mean >= drop.min_mean and count < band_low`; `high = upper or drop`; `score = count / band_high` if `band_high > 0` else `float(count > 0)`.

- [ ] **Step 1: Write the failing tests**

```python
LK = lambda this, nxt=None, days=5, upper_only=False: Lookup(this, nxt, days, upper_only)

def test_upper_needs_band_and_floor(cfg):
    assert judge(20, "s:T1", "ERROR", LK(Stat(3, 2)), cfg).high          # 20 > 9 and >= 5
    assert not judge(4, "s:T1", "ERROR", LK(Stat(0, 0.5)), cfg).high     # above band, below floor
    assert not judge(500, "s:T1", "INFO", LK(Stat(3, 2)), cfg).high      # INFO never

def test_next_hour_widens(cfg):
    assert not judge(20, "s:T1", "ERROR", LK(Stat(3, 2), Stat(15, 3)), cfg).high   # edge 24

def test_young_band_uses_4_sigma(cfg):
    assert not judge(10, "s:T1", "ERROR", LK(Stat(3, 2), days=2), cfg).high       # edge 11

def test_floor_only_without_baseline(cfg):
    assert judge(5, "s:T1", "ERROR", LK(None), cfg).high
    assert not judge(9, "s:T1", "WARN", LK(None), cfg).high

def test_drop_rules(cfg):
    assert judge(0, "*total", "INFO", LK(Stat(100, 10)), cfg).high
    assert not judge(0, "*total", "INFO", LK(Stat(100, 10), days=3), cfg).high      # young
    assert not judge(0, "*total", "INFO", LK(Stat(10, 2)), cfg).high                # mean < 20
    assert not judge(0, "*total", "INFO", LK(Stat(100, 10), upper_only=True), cfg).high
    assert not judge(0, "s:T1", "ERROR", LK(Stat(100, 10)), cfg).high               # templates never drop

def test_weekend_falls_back_to_weekday():
    b = Baseline({("s", "k", "weekday", 10): Stat(5, 1)}, {("s", "weekday"): 5, ("s", "weekend"): 1})
    lk = b.lookup("s", "k", date(2026, 10, 18), 10)          # Sunday
    assert lk.this == Stat(5, 1) and lk.upper_only and lk.days == 5

# tests/test_commit.py — helper `seed(conn, service, day, hour, n_minutes, {key: (sum, sumsq)})`
def test_commit_mean_std_and_silent_minutes(conn, cfg):
    # Mon 2026-10-12 hour 10: 60 minutes observed, key seen in 2 minutes with counts 3 and 3
    seed(conn, "s", date(2026, 10, 12), 10, 60, {"s:T1": (6, 18)})
    commit(conn, cfg, today=date(2026, 10, 13))
    row = conn.execute("SELECT mean, std, days FROM baseline WHERE key='s:T1'").fetchone()
    assert row["mean"] == pytest.approx(0.1) and row["std"] == pytest.approx((0.3 - 0.01) ** 0.5)
    assert row["days"] == 1

def test_commit_keeps_last_5_weekdays_and_skips_today(conn, cfg): ...   # 7 weekdays seeded → 5 used; today's staging not committed

def test_exclusion_removes_hour(conn, cfg): ...      # decision='exclude' [10, 11) → that day missing for hour 10, 'keep' changes nothing

def test_commit_is_idempotent_and_catches_up_missed_days(conn, cfg):
    # three finished days never committed (engine was down at 02:00) → all three committed in one call;
    # calling again changes nothing
```

- [ ] **Step 2: Run to verify they fail** → FAIL.

- [ ] **Step 3: Implement `baseline.py`** to the rules above. `Baseline.load` reads `baseline` and `SELECT service, daytype, count(*) FROM day_status WHERE committed GROUP BY 1, 2`, capping counts at 5/4.

- [ ] **Step 4: Implement `commit()`** in one transaction:
  1. Upsert `day_status (service, day, committed=true, daytype)` for every `(service, day)` in `service_hours` with `day < today`; `daytype` = `CASE WHEN extract(isodow FROM day) >= 6 THEN 'weekend' ELSE 'weekday' END`.
  2. `DELETE FROM baseline`, then insert from this query (n comes from `service_hours`, so silent minutes count as zeros):

```sql
WITH ranked AS (
  SELECT service, day, daytype,
         row_number() OVER (PARTITION BY service, daytype ORDER BY day DESC) AS rn
  FROM day_status WHERE committed),
picked AS (
  SELECT service, day, daytype FROM ranked
  WHERE (daytype = 'weekday' AND rn <= 5) OR (daytype = 'weekend' AND rn <= 4)),
hours AS (
  SELECT sh.service, p.daytype, sh.day, sh.hour, sh.n_minutes
  FROM service_hours sh JOIN picked p USING (service, day)
  WHERE sh.n_minutes > 0 AND NOT EXISTS (
    SELECT 1 FROM review_exclusions x
    WHERE x.decision = 'exclude' AND x.service = sh.service AND x.day = sh.day
      AND sh.hour >= x.hour_from AND sh.hour < x.hour_to)),
keys AS (
  SELECT DISTINCT hc.service, hc.key FROM hourly_counts hc
  JOIN hours h USING (service, day, hour))
INSERT INTO baseline (service, key, daytype, hour, mean, std, days)
SELECT k.service, k.key, h.daytype, h.hour,
       sum(coalesce(hc.sum, 0))::float / sum(h.n_minutes),
       sqrt(greatest(sum(coalesce(hc.sumsq, 0))::float / sum(h.n_minutes)
                     - (sum(coalesce(hc.sum, 0))::float / sum(h.n_minutes))^2, 0)),
       count(*)
FROM keys k JOIN hours h ON h.service = k.service
LEFT JOIN hourly_counts hc
  ON hc.service = k.service AND hc.key = k.key AND hc.day = h.day AND hc.hour = h.hour
GROUP BY k.service, k.key, h.daytype, h.hour;
```

  3. `ensure_partitions(conn, today, today + 2 days)`; `drop_partitions_before` for `service_hours`/`hourly_counts`/`alert_minutes` at `today − counts_days`; `DELETE FROM alert_changes WHERE at < now() − alert_changes_days`.
  4. `put_setting(conn, "last_commit", {"at": <iso>, "today": <iso date>})`.

  Add `# ponytail: keys × hours join recomputes every key nightly; restrict to keys touched since last commit if it gets slow at 27k templates.`

- [ ] **Step 5: Run tests** → PASS.
- [ ] **Step 6: Commit** — `git add -A && git commit -m "Add band rules and daily baseline commit"`

---

### Task 8: Detection and alert state

**Files:**
- Create: `logai2/owner/detect.py`, `logai2/alerts.py`
- Test: `tests/test_detect.py`, `tests/test_alerts.py`

**Interfaces:**
- Consumes: `ClosedMinute` (Task 6), `Baseline`, `judge` (Task 7), `AlertConfig` (Task 1).
- Produces:
  - `AlertRow` (dataclass): `service, key, state="NORMAL", streak=0, since: float = 0.0, score=0.0, count=0, band_low: float|None=None, band_high: float|None=None`.
  - `step(prev: AlertRow, high: bool, cfg: AlertConfig) -> AlertRow` — logai-engine `AlertStateMachine._transition` with `high` as given and `low = not high` (no mid-range, no `min_events_1m` gate: floors replace it); returns a new row, `since` updated when `state` changes.
  - `Change` (NamedTuple): `service, key, at: float, from_state, to_state, count, band_low, band_high`.
  - `class Detector(cfg: AppConfig, baseline: Baseline, links: dict[tuple[str, str], str], levels: dict[tuple[str, str], str], states: dict[tuple[str, str], AlertRow])`:
    - `run(cm: ClosedMinute, historic: bool) -> list[Change]` — no-op returning `[]` when `historic`. Adds doc keys `(service, "doc:"+doc_id)` = sum of linked templates' counts in `cm.keys` (level = most severe linked level). Judges every key with `baseline.lookup(service, key, cm.day, cm.hour)`; steps every key whose verdict is high **or** whose current state is not NORMAL; updates `states`; returns state changes.
    - `not_normal() -> set[tuple[str, str]]`.
    - `states`, `links`, `levels`, `baseline` are public attributes so the owner can swap them on reload.
  - `alert_rows(states: list[dict], links: dict[tuple[str, str], str], titles: dict[str, str], texts: dict[tuple[str, str], str]) -> list[dict]` in `logai2/alerts.py` (pure; `states` are `alert_state` rows as dicts). Returns one dict per non-NORMAL row: `{"service", "kind": "document"|"template"|"service", "doc_id": str|None, "title": str|None, "state", "since", "templates": [{"template_id", "text", "state", "count", "band_low", "band_high"}], "keys": [...]}`. Grouping: template keys with a link + their `doc:` key → `document` row; unlinked template keys → one `template` row each; `*total`/`*error` → one `service` row per service. Row state = worst of its keys by rank ALERTING > COOLING > WARMING > NORMAL; sorted by that rank then `since`.

- [ ] **Step 1: Write the failing tests**

```python
def test_step_matches_logai_engine_sequence(cfg):
    r, seq = AlertRow("s", "k"), []
    for h in [1, 1, 1, 1, 1, 0, 0, 0]:
        r = step(r, bool(h), cfg.alert); seq.append(r.state)
    assert seq == ["NORMAL", "WARMING", "ALERTING", "ALERTING", "ALERTING",
                   "COOLING", "COOLING", "NORMAL"]   # 3 highs in a row alert, 3 lows recover

def test_burst_alerts_and_recovers(cfg):
    det = Detector(cfg, Baseline({("s", "s:T1", "weekday", 10): Stat(3, 1)}, {("s", "weekday"): 5}),
                   {}, {("s", "s:T1"): "ERROR"}, {})
    # five minutes at 40/min then zeros → ALERTING then back to NORMAL; Changes recorded

def test_historic_minute_changes_nothing(cfg): ...

def test_doc_key_sums_linked_templates(cfg):
    # T1 and T2 linked to DOC-1, each 4/min (below floor alone) with mean 0 std 0.5 → doc key 8/min ≥ floor → high

def test_rows_group_by_document():
    rows = alert_rows(
        [{"service": "s", "key": "s:T1", "state": "ALERTING", ...},
         {"service": "s", "key": "s:T2", "state": "WARMING", ...},
         {"service": "s", "key": "s:T3", "state": "ALERTING", ...},
         {"service": "s", "key": "*total", "state": "COOLING", ...}],
        {("s", "s:T1"): "DOC-1", ("s", "s:T2"): "DOC-1"}, {"DOC-1": "Restart pool"}, {...})
    assert [(r["kind"], r["doc_id"], r["state"]) for r in rows] == [
        ("document", "DOC-1", "ALERTING"), ("template", None, "ALERTING"), ("service", None, "COOLING")]
```

- [ ] **Step 2: Run to verify they fail** → FAIL.
- [ ] **Step 3: Implement `detect.py`** (copy the `_transition` body from logai-engine `logai/alert/alert_state_machine.py:96-153`, adapted to `AlertRow`) **and `alerts.py`.**
- [ ] **Step 4: Run tests** → PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add detection, alert hysteresis and alert rows"`

---

### Task 9: Owner and the flush transaction (exactly once)

**Files:**
- Create: `logai2/owner/owner.py`
- Test: `tests/test_owner.py`

**Interfaces:**
- Consumes: `Miners` (5), `OpenMinutes` (6), `Baseline` (7), `Detector` (8), `db` (1), `MinuteBatch` (4).
- Produces: `class Owner(conn, cfg: AppConfig, start_minute: int, now=time.time)`:
  - Loads `drain3_state`, `templates` (levels), `services`, `alert_state`, `template_doc_links`, `Baseline.load`.
  - `detection_enabled: bool` (from setting `detection_enabled`, default False).
  - `handle(batch: MinuteBatch) -> dict[tuple[str, str], str]` — for each miss: `miners.match`; new template → pending template row; level promoted to the most severe seen. New service → `services` row with `partition = zlib.crc32(service) % 60`; new module → pending `modules` row. Adds counts to `OpenMinutes`; samples of ERROR+ templates → pending `error_samples`. Then `close_before(batch["watermark_ms"] // 60_000, services)`; for each closed minute: pending `hourly_counts`/`service_hours` increments, and, if `detection_enabled`, `detector.run(cm, historic=now() - (cm.minute + 1) * 60 > historic_after_minutes * 60)`; pending `alert_minutes` rows for template keys not NORMAL; pending changes. Returns `{(service, text): template_id}` for misses resolved.
  - `boundary_ms -> int` = `open_minutes.next_to_close * 60_000`.
  - `flush(cursors: dict[str, tuple[int, int]], status: dict) -> None` — one transaction in this order: `services`, `modules`, `templates` (upsert, `last_seen`, level), `drain3_state` (`miners.dirty()`), `service_hours` (`n_minutes = n_minutes + EXCLUDED`), `hourly_counts` (`sum = sum + EXCLUDED.sum, sumsq = sumsq + EXCLUDED.sumsq`), `alert_minutes`, `alert_state` (upsert changed rows), `alert_changes`, `error_samples` (insert, then delete rows beyond the newest 20 for touched templates), `es_cursors` (upsert, delete indices not in `cursors`), `worker_status` (`"engine"` → `status`). Clears pending only after commit; on exception rolls back and keeps pending (raises to caller).
  - `reload_baseline()`, `reload_links()`.
  - A line whose `miners.match` raises is counted under template `"{service}:unparsed"` (level of the line); logged once per minute per service.

Use `executemany` with `ON CONFLICT` per table. Add `# ponytail: executemany per table; switch to COPY into temp tables (spec section 4) when flush time shows in the Pipeline stats.`

- [ ] **Step 1: Write the failing tests** (Owner + Reader + FakeSource, real Postgres)

```python
def run_minutes(conn, cfg, hits, until_ms):          # helper: Reader→Owner→flush loop until watermark passes until_ms
    ...

def test_counts_exact(conn, cfg):
    # 3 lines in minute M, 2 in M+1 (same template), watermark past M+2
    run_minutes(conn, cfg, hits, ...)
    rows = conn.execute("SELECT key, sum, sumsq FROM hourly_counts WHERE key LIKE 's:%'").fetchall()
    assert [(r["sum"], r["sumsq"]) for r in rows] == [(5, 13)]
    assert conn.execute("SELECT n_minutes FROM service_hours").fetchone()["n_minutes"] == 3

def test_crash_between_flushes_is_exact(conn, cfg):
    # flush once after minute M closes; feed M+1 into a second Owner but do NOT flush ("crash");
    # build a fresh Reader/Owner from the database (es_cursors since_ms, drain3_state) and run to the end.
    # hourly_counts equals a single clean run: no double count, no loss.

def test_flush_failure_keeps_pending(conn, cfg, monkeypatch):
    # make one executemany raise → flush raises, nothing committed; second flush commits everything once

def test_catch_up_days_back_writes_old_partitions(conn, cfg):
    # hits two days before the test partitions' range → after ensure_partitions(bootstrap day) the flush succeeds

def test_cursor_is_open_minute_boundary(conn, cfg):
    # after a flush, es_cursors.since_ms == owner.boundary_ms (≤ last hit read)

def test_error_samples_capped_at_20(conn, cfg): ...
```

- [ ] **Step 2: Run to verify they fail** → FAIL.
- [ ] **Step 3: Implement `owner.py`.**
- [ ] **Step 4: Run tests** → PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add owner with exactly-once flush transaction"`

---

### Task 10: Engine loop, bootstrap, commands, metrics

**Files:**
- Create: `logai2/commands.py`, `logai2/metrics.py`, `logai2/engine.py`, `scripts/run_engine.py`
- Test: `tests/test_engine.py`, `tests/test_commands.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `commands.enqueue(conn, kind: str, payload: dict | None = None) -> int`, `commands.take(conn) -> list[dict]` (undone rows, oldest first, `FOR UPDATE SKIP LOCKED`), `commands.finish(conn, id: int, result: dict) -> None`. Kinds: `recompute_baseline`, `reload_sources`, `reload_links`.
  - `class Metrics(port: int | None)` (None = don't start the HTTP server, for tests): counters `logai_events_total`, `logai_cache_hits_total`, `logai_cache_misses_total`, `logai_late_counts_total`; gauges `logai_lag_seconds`, `logai_miss_ratio{service}`, `logai_flush_seconds`, `logai_engine_heartbeat_timestamp_seconds`, `log_alert_state{service, doc_id, state}` (1 for the row's current state, 0 for the others; `doc_id` = document id, `undocumented`, or `service`). `export_rows(rows: list[dict]) -> None` replaces all `log_alert_state` series.
  - `class Engine(cfg: AppConfig, conn, source, now=time.time)`: `start() -> None` (migrate, bootstrap, seed documents, build Reader/Owner), `tick() -> bool` (one loop iteration; returns whether the batch had events), `run_forever() -> None`.

Engine rules (decided here):
- **Bootstrap** on first start (setting `bootstrap` missing): `{"until": now, "since": now - days_back*86400}`; `ensure_partitions(day(since), today + 2)`. On every start: `ensure_partitions(today, today + 2)`, then run `commit()` if `last_commit.today` < today and local time ≥ `commit_time` (missed commit).
- **Index floors:** selection = setting `index_selection` `{"entries": [{"pattern", "added_at"}]}`, else `[{"pattern": cfg.elasticsearch.index, "added_at": 0}]`. Resolve patterns every `index_refresh_seconds` and on `reload_sources`. Floor of an index = `bootstrap.since` if its entry's `added_at <= bootstrap.until`, else `added_at` (ms). `Reader.set_indices(floors, saved_cursors)`.
- **Connection:** setting `es_connection` (`hosts`, `username`, `password`, `verify_certs`) overrides `cfg.elasticsearch` when present; `reload_sources` re-reads it and calls `source.reconnect`.
- **Tick:** take and run commands → `reader.poll()` → `owner.handle()` → `reader.learn()` → flush when `flush.seconds` elapsed or `flush.max_events` reached: `owner.flush(reader.cursors(owner.boundary_ms), status)` then `reader.forget_before(owner.boundary_ms)`. If flush raises: log, sleep with backoff (1 s doubling to 30 s), retry the flush only (no polling) until it succeeds.
- **First catch-up:** when `detection_enabled` is False and every healthy index is `caught_up`: flush, `commit(conn, cfg, today)`, set `detection_enabled = True` (setting + owner), `owner.reload_baseline()`.
- **Daily commit:** when local time passes `commit_time` and `last_commit.today` < today: flush, `commit()`, `owner.reload_baseline()`.
- **Status** (`worker_status["engine"]`): `{"heartbeat", "lag_seconds", "events_per_second", "cache_hit_ratio", "miss_ratio": {service: r}, "detection_enabled", "bootstrap", "last_commit", "indices": {index: positions + floor}, "available_indices": source.list_indices() (refreshed every 30 s), "resolution": {pattern: [indices]}, "warnings": [...]}`. Warning `high miss ratio` when a service's miss ratio > 0.5 over the last 5 minutes.
- Metrics: after each flush, `export_rows(alert_rows(...))` from the owner's states.
- Sleep `poll_interval_seconds` when a tick read nothing.

- [ ] **Step 1: Write the failing tests** (Engine with FakeSource and a fake clock starting 2026-10-15 12:00 local, inside the conftest partitions)

```python
def test_first_start_reads_days_back_then_enables_detection(conn, cfg):
    # hits spread over the 2 days before now; run ticks until caught up
    # → setting detection_enabled true, last_commit set, baseline rows exist

def test_burst_after_commit_alerts_within_3_minutes(conn, cfg):
    # 2 days of 3/min on an ERROR template, then 40/min live → alert_state ALERTING by the 3rd closed
    # burst minute, alert_changes rows present, Metrics log_alert_state{doc_id="undocumented", state="ALERTING"} == 1

def test_missed_commit_runs_on_start(conn, cfg): ...       # last_commit.today two days ago, clock 03:00 → start() commits

def test_recompute_command(conn, cfg):
    commands.enqueue(conn, "recompute_baseline"); engine.tick()
    assert conn.execute("SELECT result FROM commands").fetchone()["result"]["days_committed"] >= 0

def test_index_added_later_starts_at_added_at(conn, cfg): ...

def test_flush_retry_does_not_poll(conn, cfg, monkeypatch): ...  # flush raises once → no second poll before it succeeds
```

- [ ] **Step 2: Run to verify they fail** → FAIL.
- [ ] **Step 3: Implement `commands.py`, `metrics.py`, `engine.py`, `scripts/run_engine.py`** (`load_config`, `db.connect`, `EsSource`, `Metrics(cfg.metrics.port)`, `Engine(...).run_forever()`, logging to stdout). Document seeding: when `documents` is empty and `documentation.seed_path` is set, load JSON (logai-engine `documentation_corpus.json`: a list or `{"documents": [...]}` of `{id, title, text, error_code}`) or YAML (same fields) into `documents` (`doc_id=id`, `body=text`).
- [ ] **Step 4: Run tests** → PASS (`.venv/bin/pytest -q`, whole suite).
- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add engine loop, bootstrap, commands and metrics"`

---

### Task 11: Web shell, Pipeline and Data sources pages

**Files:**
- Create: `logai2/web/__init__.py`, `logai2/web/app.py`, `scripts/run_web.py`, `logai2/web/static/{index.html, css/tokens.css, css/app.css, js/core.js, js/ui.js, js/main.js, js/views/pipeline.js, js/views/sources.js}`
- Test: `tests/test_web_sources.py`

**Interfaces:**
- Consumes: `db`, `commands.enqueue`, `get_setting`/`put_setting` (Task 1/10).
- Produces: `create_app(cfg: AppConfig) -> Flask`; a request-scoped connection helper `conn()` (one `db.connect` per request, closed on teardown; `# ponytail: connection per request; add psycopg_pool if many operators`). JSON errors `{"error": str}`; 409 body `{"error": "stale", "revision": current}`.

Routes:
- `GET /` and static files.
- `GET /api/pipeline` → `worker_status["engine"]` + `es_cursors` + `{"stale": heartbeat older than 30 s}`.
- `GET /api/sources` → `{"selection": {...entries, revision}, "connection": {hosts, username, verify_certs, has_password, revision}, "available_indices", "resolution", "indices"}` (the last three from `worker_status`).
- `PUT /api/sources/selection` `{patterns: [str], revision}` → validates with logai-engine `validate_patterns` (copy from `logai/storage/index_selection.py`), keeps `added_at` of existing patterns, new ones get `now`; `put_setting`; `enqueue("reload_sources")`.
- `PUT /api/sources/connection` `{hosts, username, password, verify_certs, revision}` → empty password keeps the saved one unless the hosts' host:port set changes (then 400 "password required"); never echo the password.

UI: copy logai-engine `static/index.html`, `css/*`, `js/core.js`, `js/ui.js`, `js/main.js`; nav shows exactly Alerting, Pipeline (Monitor); Templates, Documentation (Catalog); Data sources, Baseline (Settings). Rename the console label to `LogAI2 Console`. `pipeline.js`: adapt logai-engine `views/pipeline.js` to `/api/pipeline` (lag, events/s, cache hit ratio, per-index position/caught-up/failing, warnings, detection on/off, bootstrap, last commit). `sources.js`: adapt logai-engine `views/sources.js` to `/api/sources`.

- [ ] **Step 1: Write the failing tests** (Flask test client on the `conn` schema; `cfg.database.dsn` pointed at it with `options=-csearch_path=<schema>`)

```python
def test_selection_round_trip_and_command(client, conn): ...      # PUT → GET shows entries; commands has reload_sources
def test_selection_stale_revision_409(client): ...
def test_password_never_returned(client):
    client.put("/api/sources/connection", json={"hosts": ["http://es:9200"], "username": "u",
                                                "password": "p", "verify_certs": True, "revision": None})
    body = client.get("/api/sources").get_json()
    assert "p" not in json.dumps(body["connection"]) and body["connection"]["has_password"]
def test_empty_password_on_new_host_rejected(client): ...
def test_pipeline_stale_flag(client, conn): ...
```

- [ ] **Step 2: Run to verify they fail** → FAIL.
- [ ] **Step 3: Implement `app.py` routes, `run_web.py`, copy and adapt the static files.**
- [ ] **Step 4: Run tests** → PASS. Then manually: `docker compose up -d postgres`, `LOGAI2_DATABASE_DSN=postgresql://logai:logai@localhost:5433/logai .venv/bin/python scripts/run_web.py`, open `http://localhost:5555/#pipeline` and `#sources`; both render with no console errors, light and dark theme.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add web shell, Pipeline and Data sources pages"`

---

### Task 12: Alerting and Templates pages

**Files:**
- Modify: `logai2/web/app.py`
- Create: `logai2/web/static/js/views/alerting.js`, `logai2/web/static/js/views/templates.js`
- Test: `tests/test_web_alerts_templates.py`

**Interfaces:**
- Consumes: `alert_rows` (Task 8), `timeutil` (Task 1).
- Produces routes:
  - `GET /api/alerts` → `alert_rows(...)` over `alert_state` (non-NORMAL), links, document titles, template texts.
  - `GET /api/services` → `[service]`.
  - `GET /api/templates?service=&q=&level=&page=1&size=100` → `{"total", "items": [{"template_id", "text", "level", "first_seen", "last_seen", "rate": float|None, "band_low", "band_high", "doc_id", "doc_title", "state"}]}`. `rate` = current local hour's `hourly_counts.sum / service_hours.n_minutes`; band from `baseline` for the current day type/hour with the k rule of Task 7; `q` is a case-insensitive substring of `text`.
  - `PUT /api/templates/<service>/<path:template_id>/link` `{"doc_id": str | null}` → upsert/delete `template_doc_links`; 404 for an unknown template or document; `enqueue("reload_links")`.

UI: `alerting.js` adapts logai-engine `views/alerting.js`: one card per row — state chip, service · document title (or "Undocumented" / "Whole service"), and per template `text  count/min (normal mean ± σ)`, polled every 5 s. `templates.js` adapts logai-engine `views/templates.js`: service filter, search, level filter, pagination, rate vs band, document column with a select to link/unlink. Remove the rate SSE stream and all group columns.

- [ ] **Step 1: Write the failing tests**

```python
def test_alerts_grouped_by_document(client, conn): ...    # seeded alert_state + link → one document row with 2 templates
def test_templates_rate_and_band(client, conn): ...       # seeded hourly_counts/service_hours/baseline for the current hour → rate and band numbers
def test_templates_paginated_and_filtered(client, conn): ...
def test_link_unknown_doc_404_and_link_enqueues_reload(client, conn): ...
```

- [ ] **Step 2: Run to verify they fail** → FAIL.
- [ ] **Step 3: Implement routes and the two views.**
- [ ] **Step 4: Run tests** → PASS; then check both pages in the browser against a database filled by a short engine run (Task 10 tests' FakeSource, or the recharge stack).
- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add Alerting and Templates pages"`

---

### Task 13: Documentation and Baseline pages

**Files:**
- Create: `logai2/review.py`, `logai2/web/static/js/views/documentation.js`, `logai2/web/static/js/views/baseline.js`
- Modify: `logai2/web/app.py`
- Test: `tests/test_review.py`, `tests/test_web_docs_baseline.py`

**Interfaces:**
- Produces:
  - `suggest(changes: list[dict], decided: set[tuple[str, date, int]]) -> list[dict]` in `logai2/review.py` — from one service's `alert_changes` (ordered by `at`), the periods from leaving NORMAL to returning to NORMAL (an open period ends at now), converted to local time, widened to whole hours, split per local day, merged when overlapping; each `{"service", "day", "hour_from", "hour_to", "keys": [...]}`; hours already in `decided` (`(service, day, hour)` of any `review_exclusions` row) are removed.
  - Routes:
    - `GET /api/documents` → list with `linked_templates` counts; `POST /api/documents` `{title, body, error_code}` → `DOC-NNN` (next number above the max existing numeric ID); `PUT /api/documents/<id>` with `revision` (409 stale); `DELETE /api/documents/<id>` → 409 `{"error": "linked", "templates": n}` while linked.
    - `GET /api/baseline?service=` → `{"days": day_status rows, "suggestions": suggest(...), "decisions": review_exclusions rows, "last_commit"}`.
    - `POST /api/baseline/decisions` `{service, day, hour_from, hour_to, decision}` → insert; response includes `"warning"` when, for any hour in the range, committed days of that day type minus excluded ones would be < 3.
    - `DELETE /api/baseline/decisions/<id>`.
    - `POST /api/baseline/recompute` → `enqueue("recompute_baseline")`, returns the command id; `GET /api/commands/<id>` → status/result.

UI: `documentation.js` adapts logai-engine `views/documentation.js` (list, editor, revision conflict message) with a "Linked templates" list per document. `baseline.js` is new, built from `ui.js` components: per service, committed days (weekday/weekend counts, young or full), the suggestion list with **Exclude: real incident** / **Keep: normal** buttons, "Add range" (service, day, from, to), existing decisions with undo, and **Recompute baseline now** showing the command result.

- [ ] **Step 1: Write the failing tests**

```python
def test_suggest_widens_to_whole_hours():
    ch = [chg("s:T1", "NORMAL", "WARMING", local(2026, 10, 14, 14, 20)),
          chg("s:T1", "COOLING", "NORMAL", local(2026, 10, 14, 15, 5))]
    assert [(s["day"], s["hour_from"], s["hour_to"]) for s in suggest(ch, set())] == [(date(2026, 10, 14), 14, 16)]

def test_suggest_splits_at_midnight_and_skips_decided(): ...
def test_document_crud_revision_and_delete_blocked_while_linked(client, conn): ...
def test_decision_warns_under_3_days(client, conn): ...
def test_recompute_enqueues(client, conn): ...
```

- [ ] **Step 2: Run to verify they fail** → FAIL.
- [ ] **Step 3: Implement `review.py`, the routes and the two views.**
- [ ] **Step 4: Run tests** → PASS; check both pages in the browser.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add Documentation and Baseline review pages"`

---

### Task 14: Containers, recharge run and incident test

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `README.md`, `scripts/incident_test.py`
- Modify: `docker-compose.yml`, `config.yaml` (recharge values)

**Interfaces:**
- Consumes: `scripts/run_engine.py`, `scripts/run_web.py`, the web API (Tasks 11–13), metrics (Task 10).

- [ ] **Step 1: Dockerfile and compose** — `python:3.11-slim`, install `requirements.txt`, copy `logai2/` and `scripts/`. Compose services: `postgres` (as Task 1, plus a healthcheck `pg_isready`), `logai2-engine` (`python scripts/run_engine.py`, port 9108, `depends_on: postgres: condition: service_healthy`), `logai2-web` (`python scripts/run_web.py`, port 5555); both on the external `aiops-net` network (same as logai-engine) with `LOGAI2_DATABASE_DSN=postgresql://logai:logai@postgres:5432/logai`, `LOGAI2_ES_HOSTS`, `LOGAI2_ES_INDEX`, `LOGAI2_DOC_SEED_PATH`. Ports chosen so they don't clash with a running logai-engine: engine metrics `9118`, web `5565` on the host.

- [ ] **Step 2: Port `scripts/incident_test.py`** from logai-engine: keep the replayer injection, sampling loop and report; remove LLM probes, retrain checks and the score sweep. Targets become `(label, service, regex, expected_doc)`; at start, resolve each target's template by `GET /api/templates?service=…&q=…` (regex over the returned texts) and fail fast if none or several match. Each 5 s sample stores `/api/alerts` and `log_alert_state` from `/metrics`. Report the 5 checks from the spec section 8: (1) burst template ALERTING within 3 checks after the burst starts (count from the first closed minute), (2) its (service, document) row ALERTING, (3) spread burst fires the doc key, (4) expected document shown, (5) false alerts per hour during the control phase.

- [ ] **Step 3: Deploy on the recharge stack** (node96 replayer) and verify:

```bash
docker compose up -d --build
docker compose logs -f logai2-engine     # bootstrap, catch-up over 2 days, "first commit", detection on
curl -s localhost:9118/metrics | grep -E 'logai_lag_seconds|log_alert_state' | head
```
Expected: lag falls to < 60 s; `/#pipeline` shows detection on and a last commit; `/#templates` shows rates with bands.

- [ ] **Step 4: Link documents** for the incident-test targets on `/#templates`, then run `python3 scripts/incident_test.py run` and `report`. Expected: checks 1–4 pass; record check 5 next to logai-engine's number from `reports/` for the same window. Note results in `README.md` ("Validation").

- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add containers, recharge config and incident test"`

---

## Spec coverage check

| Spec section | Task |
|---|---|
| 1 Layout, reader/owner boundary | 4, 9, 10 |
| 2 Data flow, watermark, exactly once, silent keys, historic, first start | 4, 6, 9, 10 |
| 3 Keys, band, rule, commit, alert rows, review suggestions | 6, 7, 8, 13 |
| 4 Schema, flush transaction | 1, 9 |
| 5 Web pages | 11, 12, 13 |
| 6 Error handling (index failure, Postgres down, unparsed, miss ratio, bad command) | 4, 9, 10 |
| 7 Configuration | 1 |
| 8 Testing, incident test | all, 14 |
