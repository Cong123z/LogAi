# logai2 core engine + web v1: design

Status: **built** (branch `core-engine-v1`, merged into `master` as `b1fe4d3`,
2026-10-21). ARCHITECTURE.md describes the code; its §15 lists where the code
departs from this spec.
Source design: [`docs/design/per-service-baseline.md`](../design/per-service-baseline.md)
(copied from logai-engine, branch `design/per-service-baseline`). This spec
is sub-project 1 of that design; where the two disagree, this spec wins for
v1.

## Scope

**In:** one engine process (reader + owner), Postgres, per-service Drain3,
minute counts, hourly history, daily baseline commit, mean ± kσ detection,
alert state, Prometheus metrics, and a web console with the same look and
stack as logai-engine's (six pages).

**Out (later sub-projects):**

| # | Sub-project |
|---|---|
| 0 | Benchmark spike on `udcntt*` (ES `took`, docs/s per core, cache hit rate, ingest-time field) |
| 2/3 | **Built 2026-10-21.** Embeddings, layered document links (manual → inherited → corpus ERROR field → LLM confirm or suggest), "apply to similar", clustering of undocumented alerts, LLM profiles. Plan [`docs/plans/2026-10-21-embeddings-doc-links.md`](../plans/2026-10-21-embeddings-doc-links.md); absorbs the former sub-project 8 (AI drafts) |
| 4 | Module alert API (`module_code` is collected now so its history exists) |
| 5 | Sampled catch-up on first start |
| 6 | AI chat in the document editor; LLM pages (AI Insights, Incidents); LLM profiles move to 2/3 |
| 7 | v2: Kafka, many readers and owners |

**First target:** the recharge stack under Docker Compose (node96/97 replayer
data), checked with a reworked `scripts/incident_test.py`.

## Decisions

| Decision | Choice |
|---|---|
| Location | new repo `~/Documents/logai2` |
| Processes | two containers, `engine` and `web`, sharing only Postgres; they never call each other |
| Stack | Python 3.11, Flask, `drain3`, `elasticsearch` 8 client, `prometheus-client`, `psycopg[binary]` 3, PyYAML. No ORM: plain SQL, numbered migrations. No HDBSCAN, scikit-learn, SciPy |
| Database | `pgvector/pgvector:pg16` in compose (pgvector unused until sub-project 3) |
| Web look | copy `tokens.css`, `app.css`, `core.js`, `ui.js`, `main.js`, sidebar and hash-routed vanilla JS views from logai-engine |
| Timezone | Asia/Ho_Chi_Minh, applied in one place (`owner/counts.py`) |

## 1. Repo layout

```
logai2/
  config.py           dataclasses + YAML + env (copied from logai-engine, trimmed)
  db.py               connection pool, migrate(), tx() context manager
  migrations/001_init.sql
  reader/
    es_source.py      ElasticsearchCollector (copied): index selection, per-index cursor,
                      re-read of the cursor's millisecond + _id dedup
    normalize.py      preprocessor.normalize / find_level (copied)
    reader.py         poll -> normalize -> known-line cache -> MinuteBatch
  owner/
    miners.py         one Drain3 miner per service, state in drain3_state
    counts.py         open minutes, watermark, minute closing, local-time bucketing
    baseline.py       band lookup (pure functions)
    detect.py         count vs band -> score -> AlertStateMachine (copied, re-keyed)
    owner.py          handle(batch) -> flush transaction
  jobs/commit.py      daily commit and "recompute now" (same function)
  commands.py         LISTEN/poll the commands table, dispatch
  metrics.py          Prometheus exporter
  engine.py           main loop
  web/
    app.py            Flask; reads Postgres; writes settings, documents, links, exclusions, commands
    static/
scripts/run_engine.py, run_web.py, incident_test.py
tests/
docker-compose.yml    postgres, engine, web
Dockerfile
```

**Boundaries**

- `reader/` never imports `owner/`. They meet in `engine.py` through one
  call carrying a plain dict, the shape Kafka carries in v2:

  ```python
  MinuteBatch = {
      "counts": [  # aggregated
          {"service": str, "template_id": str | None, "text": str | None,
           "level": str, "module": str, "minute": int, "n": int,
           "sample": str},          # one raw line, for error_samples
      ],
      "index_progress": {index: {"cursor": [...], "caught_up": bool}},
  }
  ```

  A known line has `template_id` set and `text` None. A miss has
  `template_id` None and the normalized `text`; it is sent once per distinct
  (service, text) per batch with its total `n`.
- `owner.handle()` returns `{(service, text): template_id}` for the misses
  it resolved; the engine hands it to the reader's cache.
- `web/` imports only `db.py` and `config.py`.

## 2. Data flow

One engine thread loops:

1. **Read.** Per selected index: sorted `search_after` on (`@timestamp`,
   `_doc`), `@timestamp <= now - 30 s`, `size` 10,000, `_source` limited to
   `@timestamp`, service fields, level fields, `message` and the module field
   (`reader.module_field`, default `moduleCode`; empty string when missing).
   Lines truncated to 8 KB, then normalized, then looked up in the known-line
   cache (LRU, `reader.cache_size`, default 200,000) keyed
   `(service, normalized text)`.
2. **Own.** `owner.handle(batch)`: Drain3 (that service's miner) on each
   miss, add counts to open minutes, close every minute before the
   watermark.
3. **Flush** every 2 s or 50,000 events: one transaction (section 4).
4. **Commands:** `LISTEN commands`, with a 1 s poll as fallback; executed
   between flushes.
5. **Commit timer:** at `baseline.commit_time` (default 02:00 local).

**Minute clock and watermark**

- Minutes are event time, bucketed in local time.
- The watermark is the minimum over selected indices of each index's
  position: the newest `@timestamp` read, or `now - 30 s` (the query's upper
  bound) for an index whose last poll returned nothing (caught up).
- A minute closes when `watermark >= minute + 60 s`.

**Exactly once**

- The cursor written for an index is the position of the **start of its
  oldest open minute**, not of the last hit. After a crash every open minute
  is re-read whole; closed minutes are never read again.
- Docs sharing the cursor's millisecond are re-read and dropped by an
  in-memory `_id` set kept for the current open minutes only.

**Silent keys.** A `(service, template)` key with counts earlier in the
current hour and none in this minute is detected with count 0 (needed for
recovery). Keys not seen in the hour add nothing to the sums.

**Historic minutes.** A minute closed more than 15 min after its end is
counted into history but does not drive alerts.

**First start (no sampling).** The first cursor of every index is
`now - start.days_back` (default 2 days); catch-up reads every document.
When every index is caught up for the first time, the engine runs the commit
once and detection starts. Before that first commit nothing alerts, not
even the floor. `start.days_back: 0` starts at now with floors only after
the first nightly commit.

## 3. Detection

**Keys, per closed minute**

| Key | Count |
|---|---|
| `(service, template_id)` | that template's events |
| `(service, '*total')` | all events of the service |
| `(service, '*error')` | ERROR, FATAL, CRITICAL events of the service |
| `(service, 'doc:' + doc_id)` | sum over templates manually linked to the document |

**Band** (`owner/baseline.py`, pure):

- `mean, σ` from `baseline[service, key, daytype, hour]`, held in memory and
  reloaded on `NOTIFY baseline`.
- `k = baseline.sigma_multiplier` (3.0); `young_sigma_multiplier` (4.0)
  while the current day type has fewer than `full_after_days` (5) committed
  days.
- Band = the wider of this hour's and the next hour's band.
- Doc key: `Σmean_i ± k·Σσ_i` over its linked templates (rebuilt when links
  or the baseline change).

**Rule**

| Case | Behavior |
|---|---|
| Upper alert | `c > mean + kσ` **and** `c >= floor(level)`: ERROR+ 5, WARN 10, INFO never. `*total` uses the INFO rule; `*error` uses ERROR |
| Lower alert | `*total` only, `mean >= 20`, and the day type has ≥ `full_after_days` days |
| No committed day | floor only |
| Weekend with < 2 committed days | use the weekday profile, upper alerts only |
| Score | `c / (mean + kσ)` scaled so the band edge equals `alert.score_high`; `AlertStateMachine` unchanged (3 in a row) |

**Daily commit** (`jobs/commit.py`, idempotent; "Recompute baseline now"
calls the same function):

1. Mark finished days in `day_status` committed with their day type.
2. Rebuild `baseline` from `hourly_counts` and `service_hours` over the last
   5 weekdays and last 4 weekend days, minus `review_exclusions`.
3. Create partitions for the next 2 days; drop partitions past retention
   (90 days for counts and `alert_minutes`, 1 year for `alert_changes`).
4. `NOTIFY baseline`.

A missed commit (engine down at 02:00) runs at the next start.

**Alert rows** (web and metrics):

- One row per (service, document) for alerting templates with a manual
  link, plus the doc key.
- Each undocumented alerting template is its own row (clustering comes with
  embeddings).
- `*total` / `*error` alerts are one service-wide row, "no single cause".
- Prometheus: `log_alert_state{service, doc_id}`, with `doc_id` set to
  `undocumented` for template rows without a document and `service` for
  service-wide rows. Never per template.

**Review suggestions** (Baseline page): each service's periods not NORMAL,
widened to whole hours, with Exclude / Keep. Unreviewed hours are kept. Warn
when an exclusion leaves an hour with fewer than 3 days.

## 4. Postgres schema (`001_init.sql`)

`service` is the first column of every service-scoped primary key.

```sql
services          (service PK, partition int)            -- hash(service) % 60, fixed at insert
modules           (module_code PK, service FK)           -- filled on first sight
es_cursors        (index PK) -> sort_values jsonb, updated_at
templates         (service, template_id PK) -> level, text, first_seen, last_seen
drain3_state      (service PK) -> blob bytea, version
service_hours     (service, day, hour PK) -> n_minutes                 -- PARTITION BY RANGE (day)
hourly_counts     (service, key, day, hour PK) -> sum, sumsq           -- PARTITION BY RANGE (day)
alert_minutes     (service, key, module_code, minute PK) -> count      -- PARTITION BY RANGE (minute)
day_status        (service, day PK) -> committed, daytype
review_exclusions (id PK, service, day, hour_from, hour_to, created_by, created_at)
baseline          (service, key, daytype, hour PK) -> mean, std, days
alert_state       (service, key PK) -> state, since, streak, score, count, band_low, band_high
alert_changes     (service, key, at PK) -> from_state, to_state, count, band_low, band_high
documents         (doc_id PK) -> title, error_code, body, revision, created_at, updated_at
template_doc_links(service, template_id PK) -> doc_id FK, source ('manual'), linked_at
error_samples     (service, template_id, at) -> line                  -- last 20 per template
settings          (key PK) -> value jsonb, revision, updated_at
commands          (id PK, kind, payload jsonb, created_at, done_at, result jsonb)
worker_status     (worker PK) -> status jsonb, updated_at
schema_migrations (version PK, applied_at)
```

- `key` is a template ID, `*total`, `*error` or `doc:<id>`.
- `hourly_counts` stores only hours with events.
- Template IDs are `service:T00431` (the Drain3 cluster ID of that service's
  miner).
- `settings` keys: `index_selection`, `es_connection` (password stored as
  today, readable only by the database role; to review before production),
  `baseline.commit_time`, `bootstrap_until`.
- Documents are seeded on first start from a corpus file
  (`documentation.seed_path`: logai-engine's `documentation_corpus.json` or
  YAML).

**Flush transaction**

| Step | SQL |
|---|---|
| minutes closed | `hourly_counts` upsert `sum += c, sumsq += c²`; `service_hours.n_minutes += 1` per service per closed minute |
| key not NORMAL | insert `alert_minutes` per module |
| state changed | upsert `alert_state`, insert `alert_changes` |
| new templates / miner changed | insert `templates`, upsert `drain3_state` for changed services only |
| samples | `error_samples` insert, trim to 20 per template |
| progress | upsert `es_cursors`, `worker_status` |

Batches use `COPY` into a temp table, then one `INSERT … SELECT … ON
CONFLICT`.

## 5. Web

Flask, same routes style as logai-engine (`/` + static, JSON under `/api`).
All reads are paginated where lists can be large.

| Page | Reads | Writes |
|---|---|---|
| Alerting | `alert_state`, links, `baseline` (poll 5 s) | — |
| Pipeline | `worker_status`, `es_cursors` | — |
| Templates | `templates` + current-hour rate + band, by service | link / unlink a document |
| Documentation | `documents`, links | create / edit / delete (409 on stale `revision`; delete blocked while linked) |
| Data sources | `settings`, `worker_status.indices` | `index_selection`, `es_connection` + command `reload_sources` |
| Baseline | `day_status`, suggestions, `review_exclusions` | exclude / keep / add range + command `recompute_baseline` |

The web never connects to Elasticsearch; the engine publishes available
indices and pattern resolution in `worker_status` every 30 s. The ES
password is never returned by the API.

## 6. Error handling

| Failure | Behavior |
|---|---|
| One index fails | others continue; back off only when all fail |
| Postgres down at flush | keep open minutes in memory, stop reading, retry with backoff; cursors have not moved, nothing lost |
| Line breaks normalize or Drain3 | counted under template `<service>:unparsed`, logged once per minute |
| Miss rate > 50% for 5 min on a service | metric `logai_miss_ratio{service}` + Pipeline warning |
| Bad command payload | `commands.result` gets the error; engine continues |

No DLQ.

## 7. Configuration

```yaml
reader:
  module_field: moduleCode
  cache_size: 200000
  max_line_bytes: 8192
  upper_bound_seconds: 30
flush:
  seconds: 2
  max_events: 50000
start:
  days_back: 2
baseline:
  timezone: Asia/Ho_Chi_Minh
  commit_time: "02:00"
  sigma_multiplier: 3.0
  young_sigma_multiplier: 4.0
  full_after_days: 5
  historic_after_minutes: 15
floors:
  ERROR: 5
  WARN: 10
drop:
  min_mean: 20
retention:
  counts_days: 90
  alert_changes_days: 365
```

## 8. Testing

- pytest against the compose Postgres, a fresh schema per test session.
- **Pure:** band math (k, young band, next-hour widening, floors, fallbacks,
  doc key sum), score mapping, minute closing and the watermark, local-time
  bucketing across midnight.
- **Integration** with a fake ES source:
  - fixed hits → exact `hourly_counts` and `service_hours`;
  - kill between flushes, restart → counts still exact (no double count, no
    loss);
  - commit → baseline rows → burst alerts within 3 minutes, recovery
    returns to NORMAL;
  - review exclusion removes that hour's day from the baseline.
- **Ported with their modules:** log masking, preprocessor, malformed ES
  hits, index selection, ES retry classification, alert state machine.
- **End to end** on the recharge stack with `incident_test.py` (targets
  found by template text, not group IDs):
  1. a burst on one template alerts within 3 checks;
  2. its (service, document) row shows ALERTING;
  3. a burst spread over a document's templates fires the doc key;
  4. the expected document is shown;
  5. false alerts per hour on normal traffic, compared with logai-engine's
     Isolation Forest on the same window.

## Open items carried from the source design

- Ingest-time field for the cursor (sub-project 0).
- ES retention on the target indices must exceed `start.days_back`.
- Postgres hosting and backups for production (`pg_dump` CronJob) — not
  needed for the recharge stack.
