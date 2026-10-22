# logai2 Architecture

This document describes the architecture **as implemented in the code** on
`master` (sub-project 1, merged 2026-10-20, and sub-project 2/3, document
links, merged 2026-10-21). When the document and the code disagree, the code
(and its tests) is the source of truth, and this document must be corrected.

Related docs: [`README.md`](README.md) (run, test, incident test),
[`docs/WEB_UI.md`](docs/WEB_UI.md) (web pages and the full HTTP API),
[`docs/specs/2026-10-20-core-engine-design.md`](docs/specs/2026-10-20-core-engine-design.md)
(the approved spec for this sub-project),
[`docs/design/per-service-baseline.md`](docs/design/per-service-baseline.md)
(the source design for all sub-projects). Where this document says "spec",
it means the first one; §15 lists where the code departs from it.

## 1. Purpose and scope

logai2 reads application logs from Elasticsearch, turns every line into a
per-service Drain3 template, counts every template per minute in Postgres,
learns a mean ± σ per (service, key, day type, local hour) once a day, and
alerts when a minute is outside that band for several minutes in a row. An
alert is shown per **document** (runbook) when the alerting templates are
linked to one. Results are exported through Prometheus and a web console.

There is no training job and no model of its own: the baseline is plain
statistics rebuilt nightly from the stored counts. Embeddings (bge-m3, the
stack's existing service) are used only to link templates to documents
(§5.1).

Three processes share one Postgres database and never call each other:

- **Engine** (`scripts/run_engine.py`): one thread that reads Elasticsearch,
  templates lines, counts minutes, detects, flushes to Postgres, runs the
  daily commit and executes commands queued by the web.
- **Web** (`scripts/run_web.py`): Flask JSON API plus a static vanilla-JS
  console. It reads and writes Postgres only; it never connects to
  Elasticsearch and never runs migrations.
- **Linker** (`scripts/run_linker.py`, `logai2/linker.py`): every
  `embedding.cycle_seconds` (30 s) embeds templates and documents' ERROR
  lines and recomputes the automatic document links (§5.1); a change
  enqueues `reload_links` for the engine. It waits for the engine to create
  the schema and never migrates.

This covers **sub-projects 1 and 2/3** of the per-service-baseline design
(2/3: embeddings, layered document links, the LLM resolver and its
profiles; plan
[`docs/plans/2026-10-21-embeddings-doc-links.md`](docs/plans/2026-10-21-embeddings-doc-links.md)).
Out of scope (later sub-projects):

| # | Sub-project |
|---|---|
| 0 | Benchmark spike on `udcntt*` (ES `took`, docs/s per core, cache hit rate, ingest-time field) |
| 4 | Module alert API (`module_code` and `alert_minutes` are collected now so the history exists; plan `docs/plans/2026-10-20-module-alert-api.md` on `master`) |
| 5 | Sampled catch-up on first start |
| 6 | AI chat in the document editor; LLM pages (AI Insights, Incidents) (LLM profiles were built with 2/3) |
| 7 | v2: Kafka, many readers and owners |
| 9 | Operator console, Datadog Watchdog style: Baseline charts, Review matrix + expected-bounds inspector + coverage, Pipeline topology + volume vs expected (phases 1-2 built); Watchdog tags on Alerting/Templates (phase 3, not built) (design [section 9](docs/design/per-service-baseline.md), from AGY-PROP-2026-002) |

Also out of scope: notifications (state is only exported), authentication on
the web API, log sources other than Elasticsearch, and more than one engine
per database.

## 2. System context

```mermaid
flowchart LR
    APP[Application / replayer]
    ES[(Elasticsearch)]
    ENG[logai2-engine<br/>reader + owner<br/>one thread]
    PG[(Postgres 16<br/>logai DB)]
    WEB[logai2-web<br/>Flask :5555]
    MET[/metrics :9108/]
    PROM[(Prometheus)]
    USER[Operator browser]
    LNK[logai2-linker]
    EMB[bge-m3<br/>embeddings]

    APP -->|index documents| ES
    ES -->|range query per index| ENG
    ENG -->|flush transaction,<br/>daily commit, worker_status| PG
    PG -->|settings, commands,<br/>links, exclusions| ENG
    WEB <-->|read state,<br/>write settings / documents /<br/>links / decisions / commands| PG
    ENG --> MET
    PROM -->|scrape| MET
    USER --> WEB
    LNK <-->|vectors, links,<br/>reload_links, worker_status| PG
    LNK -->|/v1/embeddings| EMB
```

Docker Compose (`docker-compose.yml`) runs `postgres`
(`pgvector/pgvector:pg16`; the `vector` extension is created in `public`),
`logai2-engine`, `logai2-web` and `logai2-linker`, plus `logai2-tei`
(bge-m3) under the `tei` profile. Elasticsearch, the replayer and the
stack's `bge-m3` are expected to run already on the external `aiops-net`
network (the same one logai-engine joins). Host ports avoid a running logai-engine:

| Service | Container port | Host port |
|---|---|---|
| Postgres | 5432 | `127.0.0.1:5433` (localhost only: default credentials) |
| Engine metrics | 9108 | 9118 |
| Web | 5555 | 5565 |

The web talks to the engine only through Postgres: it writes **settings**
and **commands**; the engine writes **worker_status** and everything it
counts.

## 3. Configuration

`logai2/config.py` defines the configuration dataclasses. Values resolve in
this order, later layers overriding earlier ones:

1. Dataclass defaults.
2. YAML file, `config.yaml` at the repo root by default (or the path given as
   the first argument of `run_engine.py` / `run_web.py`). Missing file =
   defaults. Unknown keys are ignored silently. A dict value (`floors`) is
   merged key by key, so `floors: {ERROR: 3}` keeps `WARN: 10`.
3. `LOGAI2_*` environment variables (table below). An empty value is ignored,
   which is how compose passes "unset".
4. At runtime, the Elasticsearch connection saved on the web (`es_connection`
   setting) replaces `hosts`, `username`, `password` and `verify_certs`; the
   index selection saved on the web (`index_selection`) replaces
   `elasticsearch.index` (§4.3).

**Environment variables**

| Variable | Sets | Read by |
|---|---|---|
| `LOGAI2_DATABASE_DSN` | `database.dsn` | engine, web |
| `LOGAI2_ES_HOSTS` | `elasticsearch.hosts` (comma-separated) | engine; web shows it until a connection is saved |
| `LOGAI2_ES_USER` | `elasticsearch.username` | engine, web (display) |
| `LOGAI2_ES_PASSWORD` | `elasticsearch.password` | engine; web keeps it when a save leaves the password empty on the same cluster |
| `LOGAI2_ES_VERIFY_CERTS` | `elasticsearch.verify_certs`; `0`, `false`, `no`, `off` = false | engine, web (display) |
| `LOGAI2_ES_INDEX` | `elasticsearch.index` | engine, web (fallback selection) |
| `LOGAI2_METRICS_PORT` | `metrics.port` | engine |
| `LOGAI2_WEB_PORT` | `web.port` | web |
| `LOGAI2_DOC_SEED_PATH` | `documentation.seed_path` | engine |
| `LOGAI2_EMBEDDING_URL` | `embedding.url` | linker |

Compose-only variables: `LOGAI2_NETWORK` (external network name, default
`aiops-net`) and `LOGAI2_DOC_SEED_VOLUME` (volume holding logai-engine's
`documentation_corpus.json`, default `logai-data`, mounted read-only at
`/seed`). Test-only variables: `LOGAI2_TEST_DSN` (pytest database) and
`LOGAI2_TEST_ENGINE` / `LOGAI2_TEST_WEB` / `LOGAI2_TEST_REPLAYER`
(`scripts/incident_test.py` endpoints).

**`config.yaml` sections and defaults**

| Section.key | Default | Meaning |
|---|---|---|
| `elasticsearch.hosts` | `["http://elasticsearch:9200"]` | ES endpoints |
| `elasticsearch.username` / `password` | `""` | basic auth when both are set |
| `elasticsearch.verify_certs` | `true` | TLS verification |
| `elasticsearch.index` | `app-logs-*` (repo `config.yaml`: `recharge-logs`) | index or pattern read when no selection is saved |
| `elasticsearch.batch_size` | 10000 | page size per index per poll; also the "caught up" threshold |
| `elasticsearch.poll_interval_seconds` | 1.0 | sleep after a tick that read no event |
| `elasticsearch.index_refresh_seconds` | 60 | re-resolve the selected patterns |
| `database.dsn` | `postgresql://logai:logai@postgres:5432/logai` | Postgres |
| `reader.module_field` | `moduleCode` | `_source` field copied into `module` |
| `reader.cache_size` | 200000 | known-line LRU entries |
| `reader.max_line_bytes` | 8192 | message cut (UTF-8 bytes) before normalizing |
| `reader.upper_bound_seconds` | 30 | only docs with `@timestamp <= now - 30 s` are read |
| `flush.seconds` | 2.0 | flush at least this often |
| `flush.max_events` | 50000 | flush when this many events are pending |
| `start.days_back` | 2 | first start reads from `now - days_back` days |
| `baseline.timezone` | `Asia/Ho_Chi_Minh` | **unused**: the zone is hard-coded in `logai2/timeutil.py` |
| `baseline.commit_time` | `"02:00"` | local time of the daily commit |
| `baseline.sigma_multiplier` | 3.0 | k of a full baseline |
| `baseline.young_sigma_multiplier` | 4.0 | k of a young baseline |
| `baseline.full_after_days` | 5 | committed days for a full baseline (weekend capped at 4, §8.3) |
| `baseline.historic_after_minutes` | 15 | a minute closed later than this after its end is counted but not detected |
| `floors.ERROR` / `floors.WARN` | 5 / 10 | minimum count per minute for an upper alert |
| `drop.min_mean` | 20.0 | `*total` drop alerts only where the mean is at least this |
| `retention.counts_days` | 90 | partitions of `service_hours`, `hourly_counts`, `alert_minutes` |
| `retention.alert_changes_days` | 365 | rows of `alert_changes` |
| `alert.warm_consecutive` / `alert_consecutive` / `cool_consecutive` | 2 / 3 / 3 | hysteresis, in minutes (§8.5) |
| `drain3.sim_threshold` / `depth` / `max_children` | 0.6 / 4 / 100 | Drain3 tree |
| `metrics.port` | 9108 | Prometheus endpoint (`metrics.host` exists but is unused: the exporter binds all interfaces) |
| `web.host` / `web.port` | `0.0.0.0` / 5555 | Flask |
| `documentation.seed_path` | `""` | corpus seeded into `documents` once, while the table is empty |
| `documentation.inherit_similarity` | `{ERROR: 0.85, WARN: 0.88, INFO: 0.92}` | layer 1 threshold per level class (§5.1) |
| `documentation.corpus_similarity` | `{ERROR: 0.85, WARN: 0.88, INFO: 0.92}` | layer 2 threshold per level class |
| `documentation.corpus_margin` | 0.05 | layer 2: best document over the next one |
| `documentation.suggest_similarity` | 0.80 | "apply to similar" list |
| `documentation.cluster_similarity` | 0.85 | undocumented alerting templates joined into one row |
| `embedding.url` / `model` | `http://bge-m3:8080` / `bge-m3` | OpenAI-style `/v1/embeddings`; empty url = nothing is embedded |
| `embedding.batch` / `cycle_seconds` | 32 / 30 | texts per request; linker cycle |
| `llm.candidate_similarity` / `candidates` | 0.60 / 3 | documents the LLM may confirm |
| `llm.per_service_hour` / `daily_max` | 0 / 0 | LLM results per service per hour, in all per day; 0 = no limit |
| `llm.max_input_chars` | 8000 | prompt size (candidate texts are cut) |

The repo's `config.yaml` sets only `elasticsearch.index: recharge-logs`,
`baseline.sigma_multiplier: 3.0` and the two floors. Secrets and endpoints
come from env vars.

## 4. External input contract

### 4.1 Elasticsearch document

The reader asks only for these `_source` fields: `@timestamp`, `service`,
`service_code2`, `level`, `log.level`, `message` and the module field.

| Field | Rule (`logai2/reader/es_source.py`) |
|---|---|
| `_id` | required; a hit without `_id` or without a dict `_source` is counted as malformed and skipped |
| time | the hit's first sort value (epoch ms of `@timestamp`); if missing, `@timestamp` parsed (ISO string or epoch number; > 1e11 = ms); unparseable = now |
| `service` | `service` (string, or ECS `{"name": ...}`), else `service_code2` (this deployment's Logstash field; `service_code` holds a host IP and is never used), else `"unknown"` |
| `level` | ECS `log.level`, else flat `level`, else a level token in the first 5 tokens of the message preceded only by date/time/pid/`[thread]`-like tokens, else `INFO`. Upper-cased |
| `message` | any type coerced to a string (null = `""`), cut to `reader.max_line_bytes` UTF-8 bytes (a split multibyte char is dropped) |
| `module` | `_source[reader.module_field]` as a string, `""` when missing |

The cut message is then normalized (`logai2/reader/normalize.py`, copied from
logai-engine's preprocessor): the leading date/level/thread prefix and all
timestamps are removed, XML/SOAP is reduced to element names and text,
identifiers (UUID, hex, long ids, IP, URL host, e-mail) and every digit run
become `<*>`, separators are dropped, runs of `<*>` collapse, and only the
first **40 tokens** are kept. A line whose normalization raises is counted as
`<service>:unparsed`.

### 4.2 Query, paging and the millisecond re-read

One page per selected index per poll (`EsSource.page`):

- `range @timestamp gte from_ms lte upper_ms` (`epoch_millis`), where
  `from_ms` is the index's position and `upper_ms = now - 30 s`;
- sort `@timestamp asc` (`numeric_type: date`, so the sort value is epoch ms
  also for `date_nanos`), then `_doc asc`; `size = elasticsearch.batch_size`;
- `ignore_unavailable=true` (a deleted index does not fail the others),
  `filter_path` limited to `_id`, `_source`, `sort`;
- one attempt per search, no retry; the client timeout is a hard-coded 30 s.

The range starts **at** the position's millisecond (inclusive), so docs
sharing the last millisecond of the previous page are read again and dropped
by `_id` (§6). This replaces logai-engine's `search_after` on `_doc`, which
skipped docs sharing a millisecond at a page boundary (`_doc` changes on
segment merges). When one millisecond fills a whole page, re-reading it would
never advance, so it is read whole with `size` 10,000, and if it still fills
the page, one `search_after` page continues it (ceiling: §14).

### 4.3 Index selection and connection

- **Selection** (`settings.index_selection`): `{"entries": [{"pattern",
  "added_at"}]}`, `added_at` in epoch seconds. Never saved, or saved empty =
  the configured `elasticsearch.index` with `added_at` 0 (engine and web
  agree). Patterns are resolved with `indices.get(expand_wildcards=open)`
  every `index_refresh_seconds` (60 s) and on the `reload_sources` command.
  Data-stream backing indices are included, hidden ones are not.
- **Floor** of an index: an entry added before the first start ended
  (`added_at <= bootstrap.until`) is read from `bootstrap.since`; a later one
  from its `added_at`. An index matched by several patterns takes the
  earliest floor. A newly selected index starts at its saved cursor, else at
  its floor; an index that stays selected keeps its in-memory position.
  Deselected indices are dropped from the reader and their `es_cursors` row
  is deleted at the next flush.
- **Available indices** (`cat.indices`, non-hidden, sorted) are listed every
  30 s for the web picker.
- **Connection** (`settings.es_connection`): `{"hosts", "username",
  "password", "verify_certs"}`. The engine applies it at start, on every
  retry while ES is unreachable at start, and on `reload_sources`. The
  password is stored in clear in Postgres (readable by the database role)
  and never returned by the API.

### 4.4 Documentation seed

On every start, if `documentation.seed_path` is set and `documents` is
empty, the file is loaded once: JSON `{"entries": [...]}` (logai-engine's
`documentation_corpus.json`) or `{"documents": [...]}`, or a bare YAML list.
Each item gives `id` → `doc_id`, `title` (default the id), `error_code`,
`error` (optional), `handling` (else `text`). logai-engine's corpus has no
ERROR field: `scripts/split_doc_errors.py` splits the seeded documents once
(`docs/plans/2026-10-21-corpus-error-split.yaml`). Compose mounts logai-engine's `logai-data` volume
read-only for this.

## 5. Core data contracts

| Type | Where | Fields |
|---|---|---|
| `Line` | `reader/es_source.py` | `ts` (epoch s), `service`, `level`, `message`, `module`, `doc_id` (ES `_id`) |
| `Count` | `reader/reader.py` | `service`, `template_id` (set on a cache hit) or `text` (normalized, on a miss), `level`, `module`, `minute`, `n`, `sample` (first raw line of the aggregate) |
| `MinuteBatch` | `reader/reader.py` | `counts: [Count]`, `watermark_ms`, `events`, `positions: {index: {pos_ms, caught_up, failing}}` |
| `ClosedMinute` | `owner/counts.py` | `minute` (epoch s // 60), local `day`, `hour`, `keys {(service, key): n}`, `modules {(service, template_id, module): n}`, `levels {(service, template_id): worst level}` |
| `Stat` | `owner/baseline.py` | `mean`, `std` |
| `Lookup` | `owner/baseline.py` | `this`, `next` (Stat or None for this and the next hour), `days` (committed days of the profile used, capped), `upper_only`, `daytype` (profile used) |
| `Verdict` | `owner/baseline.py` | `high`, `score`, `band_low`, `band_high` |
| `AlertRow` | `owner/detect.py` | `service`, `key`, `state`, `streak`, `since`, `score`, `count`, `band_low`, `band_high` (one `alert_state` row) |
| `Change` | `owner/detect.py` | `service`, `key`, `at`, `from_state`, `to_state`, `count`, `band_low`, `band_high` (one `alert_changes` row) |

`Count` rows are aggregated per (service, template id or text, hit/miss,
level, module, minute) inside one poll. `MinuteBatch` is a plain dict on
purpose: it is the message shape Kafka would carry in v2. `reader/` never
imports `owner/`; they meet in `engine.py`.

**Keys.** A `key` is a template id (`<service>:T00042`, the Drain3 cluster id
of that service's miner), `<service>:unparsed`, `*total`, `*error`, `*warn`, `*info` or
`doc:<doc_id>`. `*error` / `*warn` / `*info` are the line level classes
(`links.level_class`) and add up to `*total`. `*warn` and `*info` are **view
only**: stored and zero-filled like `*error` (no backfill), skipped by the
detector (`detect.VIEW_ONLY_KEYS`), so they never alert. The console also
derives `verdict:` keys from alert history (not stored).

**Alert rows** (`logai2/alerts.py`, used by the web and the metrics): every
non-NORMAL `alert_state` row is grouped into one of:

| Row kind | Members | Metric label `doc_id` |
|---|---|---|
| `document` | alerting templates linked to the document, plus its `doc:` key | the doc id |
| `template` | one undocumented alerting template | `undocumented` |
| `cluster` | undocumented alerting templates of one service joined by embedding similarity ≥ `cluster_similarity` (web only; the metrics pass no similarity) | `undocumented` |
| `service` | `*total` and/or `*error` of the service ("no single cause") | `service` |

A row's state is its worst member (ALERTING > COOLING > WARMING), `since`
its earliest member. Rows are sorted by state rank, then oldest first.

### 5.1 Document links

A document is **ERROR** (the log line(s) it is for, one per line) plus
**Handling** (what to do). Only the ERROR lines are embedded, each through
`normalize()` first (`links.error_lines`), so they compare with template
texts; a document with no ERROR line is never matched automatically.

**Documents belong to a service** (`documents.service`; NULL = shared,
applying to every service). Automatic matching (layer 2) and the LLM's
candidates (layer 3) only consider documents of the template's own service
and shared ones, so each service's links point to its own, specific
runbook. Migration 004 gave each existing document the one service whose
templates use it, else shared. A manual link may still point anywhere.

**A document's ERROR line has a level class** (`documents.level`: ERROR /
WARN / INFO; NULL = any). Layer 2 links only templates of that class, for
the same reason inheritance stays within a class: a success line and an
error line can share most of their words (`DBAdapter: Log success request
his TransactionInfo …` scored 0.95 against `DBAdapter: Error when log
request his TransactionInfo …`). Such a cross-class match is left to the
LLM, which is shown both levels. Migration 005 set each existing
document's level from the templates linked to it by hand or that it was
written for, when they agree on one class; LLM drafts take their
template's class.

One `template_doc_links` row per template; its `source` is the layer
(`logai2/links.py`, design section 3):

| # | `source` | Rule | Drives alerts |
|---|---|---|---|
| 0 | `manual` | set on the web; `doc_id NULL` = "no document" | yes (NULL: no) |
| 1 | `inherited` | nearest **manual** template of the same service and level class (ERROR/FATAL/CRITICAL, WARN/WARNING, other), cosine ≥ `inherit_similarity[class]`; `inherited_from` names it | yes |
| 2 | `corpus` | best **active** document **of the template's service or shared**, whose ERROR line has **the template's level class** (or no level), by its best ERROR line, ≥ `corpus_similarity[class]` and ≥ `corpus_margin` above the next document | yes |
| 3 | `llm` | the LLM picked one of the template's candidate documents; `note` = its reason | yes |
| 4 | `draft` | the LLM wrote a document (`status draft`, `source llm`, `verdict` action or safe) | no, until approved |
| — | `rejected` | the operator rejected a layer 3–4 result | no |

"Drives alerts" = loaded by the detector (`doc:` keys, metrics) and used
for alert rows: `doc_id IS NOT NULL AND source IN (manual, inherited,
corpus, llm)` (`links.ALERTING_SOURCES`).

`apply_links` (each linker cycle, one transaction) computes layers 1 and 2
for every template with a vector and no `manual` / `rejected` row (layer 1
wins), upserts what changed, replaces `llm` / `draft` rows (an unused
draft document becomes `rejected`), and deletes `inherited` / `corpus` rows
that no longer qualify, except while the template, or the manual template
it inherits from, has no vector (its text just changed). Writes are guarded
so a `manual` or `rejected` row written meanwhile is never overwritten.
Inheritance only reads manual links, so links never drift. Templates are
embedded with `AND text = <text read>`, documents with `AND revision =
<revision read>`; the engine clears a template's vector when its text
changes.

**LLM resolver** (`logai2/resolver.py`, layers 3–4, run by the linker after
`apply_links`). With at least one active `llm_profiles` row, it takes templates that
have a vector and no link row at all, of any level, alert state or age,
ALERTING first then ERROR > WARN > INFO, about 3 LLM
calls per cycle (a template's second call may exceed it by one), and, when
set above 0, `llm.per_service_hour` per service and `llm.daily_max` in all
(both 0 = no limit by default).
Templates resolved count, from `llm` / `draft` / `rejected` rows by
`linked_at` (a rejection keeps it), and so do calls that wrote nothing
(failed or junk answers, kept in memory). Capped services and templates in
back-off are left out of the query, so they never block the others. The
linker writes its status after each template, so a slow model does not
look stale. For each:

1. candidates = active documents whose best ERROR line is ≥
   `llm.candidate_similarity` (0.60), top `llm.candidates` (3). If any, the
   LLM is asked which fits; a listed `doc_id` becomes an `llm` link (with
   `reload_links`).
2. Otherwise (or `null`), the LLM writes `{verdict, title, error_code, error,
   handling, confidence}` for the template's service; it is stored as a
   `draft` document of that service (next `DOC-NNN` id) linked as `draft`,
   in one transaction. When other services already document a similar
   error (their documents' ERROR lines ≥ `llm.candidate_similarity`, top
   `llm.candidates`), those are sent as `references` to adapt, and the draft
   records them in `documents.based_on`.

The LLM sees only service, level and the normalized template text, plus the
candidates' service, level, title, ERROR and Handling (≤
`llm.max_input_chars`); never raw log lines. The prompts tell it to judge
by meaning rather than shared words (a success line is not the error line
it resembles), to prefer documents of the template's level, and that an
INFO line is usually normal operation unless its text reports a failure. Any link written meanwhile wins (`ON CONFLICT DO NOTHING`; a
draft is rolled back). A failed call backs the template off for 30 min (in
memory); so does an answer Postgres cannot store (NUL characters are
stripped first) or any other database error on its write. The client
(`logai2/llm.py`) sends JSON mode, strips `<think>` blocks, takes the first
JSON object of the reply, retries once on a timeout only, and never follows
a redirect (it would carry the API key elsewhere). A base URL without a
path gets `/v1` (`https://host` → `https://host/v1/chat/completions`).

**Failover.** Every active profile is used, lowest `priority` first, then
by name. A profile that fails with a network or HTTP error or a timeout
(`LlmUnavailable`) is skipped for 5 minutes and the next one answers the
same question; the failed call counts in the 3 per cycle. A reply that is
not a JSON object fails the template, not the profile. When no active
profile answers, the cycle stops with a warning and no template is blamed.
Each profile's state (`down_until`, `error`) and the last LLM error are in
`worker_status['linker']` (`llm_profiles`, `llm_last_error`).

## 6. Reader

`logai2/reader/reader.py`, `Reader.poll()` once per engine tick:

1. For each selected index, `page(index, pos, upper_ms)`. On an exception
   the index's failure count goes up, it is marked not caught up, and the
   other indices continue. After 3 consecutive failures (`FAILING_AFTER`)
   the index is **failing**.
2. Each hit: skip if its `_id` is in the index's seen-set; advance the
   position to the hit's sort value (malformed hits too, so junk never pins
   it); remember `_id → ts`; convert to a `Line`; normalize; look up the
   known-line cache `(service, normalized text) → template id`; add to the
   batch aggregate.
3. **Caught up** = the page was shorter than `batch_size` (it reached
   `upper_ms`), or it held no new id (stall guard).
4. **Watermark** = min over healthy (not failing) indices of `upper_ms` if
   caught up, else its position. With no healthy index the watermark is held
   at the min over all indices (so an ES outage never closes minutes);
   with no index at all it is `upper_ms`.

After a successful flush the engine calls `forget_before(boundary)`: ids
older than min(boundary, position) are dropped from the seen-set, so it holds
only the open minutes (never the position's millisecond, which is re-read).
`learn()` puts the owner's resolved misses into the cache.

## 7. Owner and exactly-once flush

`logai2/owner/owner.py`. The owner holds one Drain3 miner per service, the
open minutes, the detector and everything pending for the next flush.

### 7.1 Templates

- `Miners.match(service, text)` (`owner/miners.py`): each service has its
  own Drain3 tree, so one service's traffic never reshapes another's.
  Normalizer `<*>` is handed to Drain3 as the literal `<#>` (Drain3 skips
  its own wildcard in similarity, which made placeholder-heavy lines create a
  new cluster each time) and turned back to `<*>` for display.
- The template's level is the most severe level seen (FATAL = CRITICAL >
  ERROR > WARN = WARNING > INFO > anything else).
- A miss that fails in Drain3, or a line the reader could not normalize, is
  counted as `<service>:unparsed`, text `(unparsed)`, logged at most once per
  service per minute.
- Changed trees are serialized once per flush (`dirty()`) into
  `drain3_state`; on start every blob is restored.
- **First seen.** `templates.first_seen` / `last_seen` are minute
  timestamps of the event time. A new service gets `partition =
  crc32(service) % 60` (reserved for v2) and is zero-filled from its first
  minute only; on restart a known service is zero-filled from
  max(start minute, its oldest template's `first_seen`).

### 7.2 Open and closed minutes

`OpenMinutes` (`owner/counts.py`) keeps one `ClosedMinute` per open minute.
Each count adds `n` to its template key, to `*total`, to the level-class key
(`*error` for ERROR, FATAL, CRITICAL; `*warn` for WARN, WARNING; `*info` for
anything else; case-insensitive), and to the (template, module) count. A count for a minute already closed is **late**: it
is dropped and counted in `logai_late_counts_total` (expected to stay 0).

`close_before(watermark_ms // 60000)` closes every minute before the
watermark's minute, in order, including minutes with no event:

- **Silent zeros.** Every known service gets `*total`, `*error`, `*warn` and `*info` = 0
  in every closed minute from its first minute on; every template seen
  earlier in the same local hour gets 0. A template not seen in the hour adds
  nothing.
- For each closed minute: `service_hours.n_minutes += 1` per service
  (every service with a `*total` key), and `hourly_counts` gets
  `sum += n, sumsq += n²` for every key with n > 0. The baseline's
  denominator is `n_minutes`, so silent minutes count as zeros.
- Samples of ERROR+ templates are queued only when their minute closes (an
  open minute is re-read after a crash), at most 20 per template.
- Detection runs on the closed minute (§8) when detection is enabled.

All local-time bucketing goes through `logai2/timeutil.py`
(`Asia/Ho_Chi_Minh`, the only place local time is computed).

### 7.3 The flush transaction

`Engine._flush` calls `Owner.flush(cursors, status)` every `flush.seconds`
(2 s) or `flush.max_events` (50,000) pending events, and before every commit.
Everything goes in **one** transaction, in this order:

1. `services`, `modules` (insert new)
2. `templates` (upsert new templates with text; `last_seen` keeps the max),
   then a `last_seen`/`level` update for templates known only from the cache
3. `drain3_state` (changed services only, `version + 1`)
4. `service_hours` (`n_minutes +=`), `hourly_counts` (`sum +=, sumsq +=`)
5. `alert_minutes` (insert, ignore conflicts)
6. `alert_state` (upsert every key stepped since the last flush, so streaks
   survive a restart), `alert_changes` (insert)
7. `error_samples` (insert, then trim to the newest 20 per template)
8. `es_cursors` (upsert per selected index; delete rows of unselected ones)
9. `worker_status` (`engine` row)

The connection must be idle (`Owner.flush` raises otherwise: inside an open
transaction the block would only be a savepoint). Pending rows are cleared
only after the commit; a failed flush keeps them all and the retry writes
them once.

**Cursor = oldest open minute.** The cursor written for every index is
`since_ms = max(boundary_ms, floor_ms)`, where `boundary_ms` is the start of
the owner's oldest open minute. After a crash every open minute is re-read
whole from ES and every closed minute was written exactly once, in the same
transaction as its cursor. Open-minute counts are never written.

## 8. Detection

`logai2/owner/detect.py` and `logai2/owner/baseline.py` (pure).

### 8.1 Keys per closed minute

| Key | Count |
|---|---|
| `(service, template_id)` | that template's events (0 when silent in a seen hour) |
| `(service, '*total')` | all events of the service |
| `(service, '*error')` | ERROR, FATAL, CRITICAL events of the service |
| `(service, '*warn')`, `(service, '*info')` | WARN / other events of the service; stored, view only, never judged |
| `(service, 'doc:' + doc_id)` | sum of the minute's counts of the templates linked to the document |

Also judged with count 0: any key whose state is not NORMAL or has a partial
streak, so an alert can recover (and a streak reset) after its key leaves
the minute. A key that is not high and has no state row creates no row.

Doc keys are computed only at detection; they have no `hourly_counts` or
`baseline` rows. A doc key's level is the worst level of its linked
templates.

### 8.2 Floors and levels

`floor(level)`: ERROR / FATAL / CRITICAL → `floors.ERROR` (5); WARN /
WARNING → `floors.WARN` (10); any other level → no floor, so **no upper
alert ever**. `*total` uses the INFO rule (never an upper alert, only a
drop) and `*error` the ERROR rule.

### 8.3 Band

For a key at local (day, hour), the lookup takes this hour's and the next
hour's `Stat` from `baseline[service, key, daytype, hour]`:

- **No stats** for either hour: high = `count >= floor` (floor only; INFO
  never). Bands are null.
- **k** = `sigma_multiplier` (3.0) when the profile's committed days ≥
  `min(full_after_days, DAY_CAP[daytype])` (weekday 5, weekend **4**),
  else `young_sigma_multiplier` (4.0). Committed days are counted from
  `day_status`, capped at 5 weekdays / 4 weekend days.
- `band_high` = max over the two hours of `mean + kσ`; `band_low` = min of
  `mean − kσ` (the wider of the two bands).
- **Upper alert:** `count > band_high` and `count >= floor`.
- **Drop alert:** `*total` only, profile full, not `upper_only`, this hour
  has a stat with `mean >= drop.min_mean` (20), and `count < band_low`.
- **Weekend fallback:** a weekend day whose service has fewer than 2
  committed weekend days uses the **weekday** profile with `upper_only`
  (no drop alerts).
- **Doc key band:** `Σmean_i ± k·Σσ_i` over the linked templates that have a
  stat (this and next hour); days = min over all linked templates; `upper_only` if any.
  This is the spec's "never too narrow" band, up to √n wider than
  `√Σσ²` (§14).
- **Score** = `count / band_high` (or `count > 0` when `band_high` is 0),
  display only; the state machine is driven by `high`.

### 8.4 Historic minutes

A minute closed more than `historic_after_minutes` (15) after its end is
counted into history but not detected, and writes no `alert_minutes`.

### 8.5 Hysteresis

`step()` is logai-engine's `AlertStateMachine` with minutes instead of
events (low = not high):

| From | Minute | To |
|---|---|---|
| NORMAL | high, 2nd in a row (`warm_consecutive`) | WARMING |
| NORMAL | low | NORMAL, streak reset |
| WARMING | high, streak reaches 3 (`alert_consecutive`): the 3rd high minute in a row | ALERTING |
| WARMING | low | NORMAL |
| ALERTING | high | ALERTING |
| ALERTING | low | COOLING (streak 1) |
| COOLING | high | ALERTING |
| COOLING | low, streak reaches 3 (`cool_consecutive`): 3 low minutes in a row | NORMAL |

`since` and `alert_changes.at` are the **end** of the closed minute
(`(minute + 1) * 60`). Every state change becomes an `alert_changes` row.
`alert_minutes` gets one row per (service, template, module) for each
closed minute in which the template's state is not NORMAL (template keys
only, not `*` or doc keys).

## 9. Daily commit and review exclusions

`logai2/jobs/commit.py`, `commit(conn, cfg, today)`, one transaction,
idempotent. The daily timer, the first catch-up, a missed commit at start and
the web's **Recompute baseline now** all call it.

1. **Mark days:** every (service, day) in `service_hours` with `day < today`
   is inserted into `day_status` as committed, with its day type (ISO
   weekday 6–7 = weekend). An already-committed day is not re-marked.
2. **Rebuild** `baseline` from scratch (`DELETE` then one `INSERT … SELECT`):
   - per service, the last 5 committed weekdays and the last 4 committed
     weekend days;
   - their hours with `n_minutes > 0`, minus hours covered by an
     `exclude` decision in `review_exclusions` (`hour_from` ≤ h <
     `hour_to`);
   - every key with a count in those hours;
   - `mean = Σsum / Σn_minutes`, `std = sqrt(max(Σsumsq / Σn_minutes −
     mean², 0))` (population σ over minutes; missing count rows are zeros),
     `days` = number of contributing days.
3. Create partitions for today .. today + 2; drop partitions older than
   `today − retention.counts_days`; delete `alert_changes` older than
   `retention.alert_changes_days`.
4. Save `settings.last_commit = {"at": <UTC ISO>, "today": <local date>}`.

The engine then reloads the baseline into the detector (no `NOTIFY`: the
commit runs in the engine process).

**`today` is the owner's day**, the local date of the oldest open minute,
not the wall clock, so a lagging engine never commits a day whose minutes
are still open.

**Review exclusions.** `logai2/review.py` `suggest()` turns one service's
`alert_changes` into its non-NORMAL periods, widened to whole local hours,
split per day, merged when touching, minus hours already decided. The
operator records `exclude` (real incident: the hours leave the baseline) or
`keep` (normal). Unreviewed hours are kept. A new decision deletes
overlapping rows of the opposite decision on that day (the latest wins). An
exclusion takes effect at the next commit or recompute.

## 10. Engine loop

`logai2/engine.py`. One thread; two Postgres connections: the owner's
(`conn`, idle between flushes) and `db` for settings, commands, cursors,
status and `commit()`, each unit of work in its own transaction.

### 10.1 Start

1. `migrate()` (only the engine migrates).
2. `settings.bootstrap`: on the very first start it is created as
   `{"since": now − days_back·86400, "until": now}` (epoch s); later starts
   reuse it. Read `last_commit`; seed documents (§4.4).
3. Apply the saved ES connection; select indices. If ES is unreachable,
   retry with backoff 1, 2, 4 … 30 s, writing `worker_status` with an
   "Elasticsearch unreachable" warning and re-reading the saved connection on
   every retry (the web's fix is picked up before the loop runs commands).
4. `start_ms` = min over selected indices of (saved `since_ms`, else floor).
   Create partitions from that local day (or today) to today + 2.
5. Build the owner with `start_minute = start_ms // 60000`; load Drain3
   blobs, template levels, services, modules, alert states, links,
   `detection_enabled` and the baseline. Log a warning when non-NORMAL states
   were loaded (they are exported as they were until catch-up re-judges
   them).
6. If a commit is due (§10.3), run it now ("missed daily commit").

### 10.2 Tick

`run_forever()` calls `tick()`; when a tick read no event it sleeps
`poll_interval_seconds`, otherwise it polls again at once.

1. Run pending commands (§12.2).
2. Refresh: re-resolve index patterns every 60 s; list available indices
   every 30 s; at local midnight create partitions for today .. today + 2.
3. `reader.poll()` → `owner.handle(batch)` → `reader.learn(resolved)`.
4. Update counters and the 5-min miss window.
5. Flush when due.
6. **First catch-up:** while detection is off, as soon as every healthy
   index is caught up (failing ones ignored): run the commit, save
   `detection_enabled = true`, enable detection and flush. Before that,
   nothing alerts, not even the floors.
7. Otherwise run the daily commit when due.

A tick that raises is logged; the engine sleeps 30 s, reconnects `db` if it
was closed and continues. Open minutes stay in memory.

### 10.3 Commit timing

A commit is due when detection is on, the owner's day (local date of the
oldest open minute) is later than `last_commit.today`, and the wall clock has
passed `commit_time` (02:00) on that owner day. In steady state this is
02:00 local for the previous days. During a catch-up after downtime the
owner day moves through the missed days, and a commit runs each time it
crosses a midnight.

### 10.4 Bootstrap and `days_back`

On the first start every index reads from `now − days_back` (2 days) and
every document is read (no sampling). With `days_back: 0` reading starts at
now; the first catch-up commit then has no finished day, so only floors
apply until the next nightly commit. ES retention on the selected indices
must exceed `days_back`.

### 10.5 `worker_status`

The engine writes `worker_status['engine'].status` at every flush:

| Field | Meaning |
|---|---|
| `heartbeat` | epoch s of the flush |
| `lag_seconds` | now − watermark (≥ 30 s by the upper bound) |
| `flush_seconds` | duration of the last completed flush (0 before the first) |
| `flush_seconds_max` | slowest flush of the last 15 min |
| `late_counts` | events for an already-closed minute since start (should stay 0) |
| `events_per_second_15m` | events / seconds covered, over the last 15 min (at least 1 s covered) |
| `history` | `[{t, lag, eps}]`, one point per wall minute, last 60 kept in memory (lost on restart) |
| `events_per_second` | pending events / time since the last flush |
| `cache_hit_ratio` | lifetime known-line hits / lookups |
| `miss_ratio` | per service, misses / events over the last 5 min |
| `detection_enabled` | first catch-up done |
| `bootstrap` | `{since, until}` |
| `last_commit` | `{at, today}` or null |
| `indices` | `{index: {pos_ms, caught_up, failing, floor}}` |
| `available_indices` | `[{index, docs_count, health, status}]` |
| `resolution` | `{pattern: [index, ...]}` |
| `warnings` | `"high miss ratio: <service> NN%"` when a service's 5-min miss ratio > 50% on ≥ 100 events, once 5 min of data exist; or the ES-unreachable warning at start |

The status is built before a flush runs, so it reports the previous completed
flush. The web treats a heartbeat older than 30 s as stale (engine not running).

The linker's status (`worker_status['linker']`) also carries `llm_today`, the
rolling 24 h count (checked against `llm.daily_max` when it is above 0): `llm` / `draft` / `rejected` link rows of the last 24 h (rolling) plus calls that wrote nothing. `llm_profiles[name].answers` is per profile since the linker started.
The Pipeline page reads both rows; its stage states are derived in
`logai2/web/pipeline_view.py` (rules in [`docs/WEB_UI.md`](docs/WEB_UI.md)).

### 10.6 Prometheus metrics (`logai2/metrics.py`, port 9108)

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `logai_events_total` | counter | — | events read |
| `logai_cache_hits_total` | counter | — | known-line cache hits |
| `logai_cache_misses_total` | counter | — | known-line cache misses |
| `logai_late_counts_total` | counter | — | events for an already-closed minute (should stay 0) |
| `logai_lag_seconds` | gauge | — | now − watermark, at the last flush |
| `logai_miss_ratio` | gauge | `service` | cache miss ratio over 5 min; series removed when a service leaves the window |
| `logai_flush_seconds` | gauge | — | duration of the last flush attempt (the timer restarts on each retry, so time spent retrying is not included) |
| `logai_engine_heartbeat_timestamp_seconds` | gauge | — | time of the last flush |
| `log_alert_state` | gauge | `service`, `doc_id`, `state` | 1 for the alert row's current state, 0 for the other three; `doc_id` = doc id, `undocumented` (all undocumented template rows of a service share one series, worst state wins) or `service` |

Never a per-template series. Alert rows that return to NORMAL have their
series removed (new series are set before stale ones are dropped). Malformed
hits are counted in the reader but not exported.

## 11. Postgres schema

`logai2/migrations/` (001 core, 002 document links, 003 LLM failover, 004
document service, 005 document level), applied by `db.migrate()` (numbered
`NNN_*.sql` files, tracked in `schema_migrations`). Plain SQL, no ORM,
`psycopg` 3 with dict rows. `service` is the first column of every
service-scoped primary key.

| Table | Primary key | Columns | Written by |
|---|---|---|---|
| `schema_migrations` | `version` | `applied_at` | engine |
| `services` | `service` | `partition` (crc32 % 60) | flush |
| `modules` | `module_code` | `service` (FK) — the first service seen | flush |
| `es_cursors` | `index` | `since_ms`, `floor_ms`, `updated_at` | flush |
| `templates` | `service, template_id` | `level`, `text`, `first_seen`, `last_seen`, `embedding` (vector(1024), NULL until embedded; cleared when the text changes) | flush; linker (`embedding`) |
| `drain3_state` | `service` | `blob` (bytea), `version` | flush |
| `service_hours` * | `service, day, hour` | `n_minutes` | flush |
| `hourly_counts` * | `service, key, day, hour` | `sum`, `sumsq` (only hours with events) | flush |
| `alert_minutes` * | `service, key, module_code, minute, day` | `count` | flush |
| `day_status` | `service, day` | `committed`, `daytype` | commit |
| `review_exclusions` | `id` | `service`, `day`, `hour_from`, `hour_to` (`[from, to)`), `decision` (`exclude`/`keep`), `created_by` (unused, `''`), `created_at` | web |
| `baseline` | `service, key, daytype, hour` | `mean`, `std`, `days` | commit |
| `alert_state` | `service, key` | `state`, `since`, `streak`, `score`, `count`, `band_low`, `band_high` | flush |
| `alert_changes` | `service, key, at` | `from_state`, `to_state`, `count`, `band_low`, `band_high` | flush |
| `documents` | `doc_id` | `service` (NULL = shared), `level` (class of the ERROR line; NULL = any), `title`, `error_code`, `error`, `handling`, `status` (`active`/`draft`/`rejected`), `source` (`manual`/`llm`), `verdict` (`action` = ERROR / `safe` = SAFE), `confidence`, `based_on` (documents an LLM draft adapted), `revision`, `embedded_revision`, `created_at`, `updated_at` | web, seed; linker (`embedded_revision`, drafts) |
| `document_errors` | `doc_id, n` | `text` (normalized ERROR line), `embedding` | linker |
| `template_doc_links` | `service, template_id` | `doc_id` (FK → documents, NULL = no document), `source` (§5.1), `similarity`, `inherited_from`, `note`, `linked_at` | web, linker |
| `llm_profiles` | `name` | `base_url`, `model`, `api_key` (clear text, never returned by the API), `timeout_seconds`, `priority` (lowest tried first), `active` (any number), `revision`, `updated_at` | web |
| `error_samples` | — (index `service, template_id, at DESC`) | `service`, `template_id`, `at`, `line` | flush |
| `settings` | `key` | `value` (jsonb), `revision`, `updated_at` | web, engine |
| `commands` | `id` | `kind`, `payload` (jsonb), `created_at`, `done_at`, `result` (jsonb) | web enqueues, engine finishes |
| `worker_status` | `worker` | `status` (jsonb), `updated_at` | engine (`engine`), linker (`linker`) |

\* `PARTITION BY RANGE (day)`, one partition per local day named
`<table>_YYYYMMDD`. Partitions are created at start (from the start minute's
day), at every local midnight and at every commit, always through today + 2.

**Retention**

| Data | Kept |
|---|---|
| `service_hours`, `hourly_counts`, `alert_minutes` | 90 days (whole partitions dropped at commit) |
| `alert_changes` | 365 days (rows deleted at commit) |
| `error_samples` | newest 20 per template (trimmed at every flush) |
| everything else (`templates`, `alert_state`, `commands`, `day_status`, `review_exclusions`, `documents`) | never pruned |

## 12. Web ↔ engine contract

### 12.1 Settings keys

| Key | Value | Written by | Units |
|---|---|---|---|
| `index_selection` | `{"entries": [{"pattern", "added_at"}]}` | web | `added_at` epoch s |
| `es_connection` | `{"hosts", "username", "password", "verify_certs"}` | web | — |
| `bootstrap` | `{"since", "until"}` | engine, once | epoch s |
| `last_commit` | `{"at", "today"}` | commit | ISO UTC timestamp, local ISO date |
| `detection_enabled` | `true` | engine, once | — |

Every write gets a new random `revision`. The web's writes are optimistic:
the client sends the revision it read (`null` before the first save) and a
mismatch is a 409. A web write and its command are committed in the same
transaction.

### 12.2 Commands

The web (and the linker) insert a row into `commands`; the engine takes all undone commands
oldest first (`FOR UPDATE SKIP LOCKED`) at the start of every tick (no
`LISTEN`), runs them between flushes and writes `done_at` and `result`. A
failing command stores `{"error": "<Type>: <message>"}` and the engine
continues.

| Kind | Enqueued by | Engine action | Result |
|---|---|---|---|
| `reload_sources` | `PUT /api/sources/selection`, `PUT /api/sources/connection` | re-apply the saved connection, re-resolve the selection | `{"resolution": {...}}` |
| `reload_links` | the web's link routes (`PUT`/`DELETE …/link`, `…/link/confirm`, `…/link/reject`), the linker when `apply_links` changed a link | reload the links that drive alerts into the detector | `{}` |
| `recompute_baseline` | `POST /api/baseline/recompute` | flush, then the daily commit for the owner's day | `{"days_committed", "baseline_rows"}` |

Any other kind fails with `unknown command kind`.

## 13. Failure handling

| Failure | Behavior |
|---|---|
| One index fails | the others continue; after 3 failures in a row it stops holding the watermark and shows `failing`. Its docs for minutes closed meanwhile are lost (§14) |
| Every index fails (ES down) | the watermark is held at the indices' positions: no minute closes, nothing is written as zero; reading resumes where it stopped |
| ES unreachable at start | start retries with backoff up to 30 s and reports the warning in `worker_status`; no crash loop |
| A hung ES host | each search waits up to the 30 s client timeout, per failing index per poll |
| Postgres down at flush | the flush is retried with backoff (1 → 30 s), reconnecting when the connection is closed; no reading meanwhile; open minutes and pending rows stay in memory; cursors have not moved, so nothing is lost |
| Postgres down elsewhere in a tick | the tick fails, the engine sleeps 30 s, reconnects `db` and continues |
| Line breaks normalize or Drain3 | counted as `<service>:unparsed`, logged once per service per minute |
| Engine crash or restart | restart reads from `es_cursors` (oldest open minute): open minutes are re-read whole, closed minutes were written once; Drain3 trees, alert states and streaks are restored |
| Restart after downtime | partitions are created for every day since the cursor; catch-up reads the gap; the minutes it closes more than 15 min late are counted but not detected; a commit runs at start if one is due (§10.3), and again each time the catch-up crosses a local midnight |
| Web started before the engine | the web does not migrate; it sees missing tables until the engine has started once |
| Container restart | `logai2-engine`, `logai2-web`, `logai2-linker` (and `logai2-tei`) use `restart: unless-stopped`; Postgres data lives in the `logai2-pg` volume |
| Bad command | `commands.result.error`; the engine continues |
| Embeddings service down | the linker logs it, reports `last_error` and pending counts in `worker_status['linker']`, keeps every existing link and retries next cycle |
| Linker down | no new vectors or automatic links; existing links keep working; the Pipeline page shows the linker stale after 90 s |
| LLM down, slow or answering junk | the template is skipped (30 min back-off) and `last_error` is reported; each call waits up to the profile timeout, retried once on a timeout |
| No active LLM profile | the resolver idles with a warning in `worker_status['linker']` |
| One LLM profile dead (502, timeout, refused) | the next active profile answers; the dead one is retried after 5 min; the LLM profiles page marks it failing |

No DLQ.

## 14. Known limits

| Area | Limit | Impact |
|---|---|---|
| Reader | One millisecond with more than 20k docs in one index: the whole-ms read (10k) plus one `search_after` page | docs beyond that are lost or the index stalls; use PIT + `_shard_doc` if a source ever logs that much in one ms |
| Reader | The ES request timeout is a hard-coded 30 s | a black-holed host blocks one poll up to 30 s per failing index; lower it or make it config if hung hosts appear |
| Reader | A failing index gets the boundary cursor like the others | its docs for minutes closed while it failed are lost (they would be late counts anyway); saving its older position would re-open closed minutes on restart |
| Reader | The cursor is `@timestamp` (event time), not an ingest-time field | docs indexed later than `upper_bound_seconds` + the open-minute window after their timestamp are late and dropped (sub-project 0) |
| Reader | The seen-id set holds every id of the open minutes per index in memory | fine for one or two minutes of traffic; large catch-up pages cost memory |
| Detection | Doc key band is `Σmean ± k·Σσ` | up to √n wider than `√Σσ²`; spread-out increases inside each template's band can go unseen |
| Detection | `*total` never raises an upper alert (INFO has no floor); only drops on a full profile with mean ≥ 20 | a service-wide volume surge is visible only through its ERROR/WARN templates |
| Detection | `alert_state` rows are never evicted; the detector scans all states per minute | grows with every key that was ever high |
| Detection | Score is `count / band_high`, not logai-engine's 0..1 scale | dashboards reading `score` need rescaling |
| Baseline | Silent minutes count as zeros from a service's first minute | a replayer that only runs some hours gives mean = σ = 0 for the idle hours, so every ERROR/WARN template at its floor alerts there (seen in the 2026-10-20 deploy) |
| Baseline | Review suggestions step hours in epoch seconds | correct only for a whole-hour offset without DST (`Asia/Ho_Chi_Minh` is +07) |
| Config | `baseline.timezone` and `metrics.host` are unused | the zone is fixed in `timeutil.py`; metrics bind all interfaces |
| Drain3 | A plain match (cluster size only) does not mark the tree dirty | persisted cluster sizes lag; template ids and texts are exact |
| Flush | `executemany` per table, not `COPY` | switch when flush time shows on the Pipeline page |
| Commit | The rebuild recomputes every key nightly | restrict to keys touched since the last commit if it gets slow |
| Commands | Polled every tick, no `LISTEN`; `recompute_baseline` is not deduplicated | a burst of recompute clicks runs the commit several times |
| Web | Flask development server, one connection per request, no auth, no pagination on `/api/baseline` | expose only on a trusted network; add a pool if many operators |
| Web | A `/` in a service name cannot be linked through `PUT /api/templates/<service>/<id>/link` | 404 |
| Database | `migrate()` has no advisory lock; the ES password and LLM API keys are stored in clear | only the engine migrates; review before production |
| LLM | Up to 3 calls per cycle, one at a time; failure back-off is in memory | a cycle can take minutes with a slow model; a restart retries failed templates |
| Scale | One engine thread per database | v2 (Kafka, many readers/owners) is sub-project 7 |
| Links | `apply_links` compares every template with every ERROR line and every manual template each cycle, exact search, no vector index | fine for thousands of templates; restrict to changed rows past ~50k |
| Links | Thresholds are starting values (design test, same model) | calibrate with `scripts/link_eval.py` on real manual links |

## 15. Where the code departs from the spec

| Spec | Code |
|---|---|
| `es_cursors.sort_values jsonb`; `search_after` paging | `since_ms`, `floor_ms`; range from the position's millisecond (inclusive) and `_id` dedup |
| MinuteBatch `index_progress` | `watermark_ms`, `events`, `positions` |
| Caught up = last poll returned nothing | caught up = a short page (or no new id) |
| Weekend full after `full_after_days` (5) | weekend full at its 4-day cap |
| Score scaled so the band edge equals `score_high` | `count / band_high`, display only |
| `LISTEN commands` / `NOTIFY baseline` | commands polled per tick; the engine reloads the baseline itself |
| `COPY` into temp tables | `executemany` |
| `alert_minutes` partitioned by `minute` | partitioned by `day` (and `day` is in the key) |
| `review_exclusions` without a decision | `decision` `exclude`/`keep` |
| settings `baseline.commit_time`, `bootstrap_until` | `bootstrap {since, until}`, `last_commit`, `detection_enabled`; commit time only in config |
| `db.py` connection pool, `tx()` | plain connections, `conn.transaction()` |
| Timezone applied in `owner/counts.py`, `baseline.timezone` config | `logai2/timeutil.py`, hard-coded |

## 16. Reading the code

1. `logai2/config.py`, `logai2/timeutil.py`, `logai2/migrations/` (001 core, 002 document links, 003 LLM failover, 004 document service, 005 document level), `logai2/db.py`.
2. `logai2/reader/es_source.py`, `normalize.py`, `cache.py`, `reader.py`: input and the watermark.
3. `logai2/owner/miners.py`, `counts.py`: templates and minute closing.
4. `logai2/owner/baseline.py`, `detect.py`, `logai2/alerts.py`: bands, hysteresis, alert rows.
5. `logai2/owner/owner.py`: the flush transaction.
6. `logai2/jobs/commit.py`, `logai2/review.py`: the daily commit and suggestions.
7. `logai2/engine.py`, `logai2/commands.py`, `logai2/metrics.py`: the loop.
8. `logai2/links.py`, `logai2/embed.py`, `logai2/linker.py`, `logai2/llm.py`, `logai2/resolver.py`: document links (§5.1).
9. `logai2/web/app.py`, `logai2/web/baseline_view.py` (Review matrix, inspector, Baseline series and incidents), `logai2/web/pipeline_view.py` (services, volume, stage states) and [`docs/WEB_UI.md`](docs/WEB_UI.md).

Tests under `tests/` are executable examples of these contracts. Good starting
points: `test_owner.py` (exact counts across a crash), `test_reader.py`
(watermark, millisecond re-read), `test_engine.py` (first catch-up, restart,
failing index), `test_detect.py` / `test_baseline.py` (bands and hysteresis),
`test_commit.py`.

## 17. When to update this document

Update it with any change to: ES fields or timestamp semantics; the
MinuteBatch or alert-row shapes; the order of the flush transaction or the
cursor rule; band, floor or hysteresis rules; the commit or its timing;
table names, partitions or retention; settings keys or command kinds; metric
names or labels; failure behavior; process topology.
