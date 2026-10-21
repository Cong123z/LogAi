# Per-service templates, statistical baselines, document-centric alerts

Status: **sub-project 1 built** (2026-10-20, branch `core-engine-v1`): spec
[`docs/specs/2026-10-20-core-engine-design.md`](../specs/2026-10-20-core-engine-design.md),
as implemented in [`ARCHITECTURE.md`](../../ARCHITECTURE.md). Sub-project 2/3
(document links, sections 3 and 4a) built and merged into `master`
(2026-10-21). Sub-projects 0 and 4–7 are not built yet. Drafted 2026-10-19. On
2026-10-21 sections 3, 4a and 6 were revised for the layered document
matching (ERROR field, automatic corpus links, LLM confirm or suggest):
plan [`2026-10-21-embeddings-doc-links.md`](../plans/2026-10-21-embeddings-doc-links.md),
which also absorbs the AI drafts first proposed in
[`antigravity-proposed-additions.md`](antigravity-proposed-additions.md).
Built as a **new project from scratch**, borrowing modules from logai-engine
(collector, preprocessor/normalize, Drain3 wrapper, alert state machine, web
parts). Template and group IDs, manual overrides and the Isolation Forest
model are discarded. LLM evidence/analysis is added later.

## Goal

When something goes wrong in a service, the operator must see **what is
wrong and what to do**: the log line that shows it, how far it is from
normal, and the document (runbook) that fixes it.

## Why change

Everything is global today: one Drain3 tree, one HDBSCAN over all
templates, one Isolation Forest.

- Services write their own log structures, so templates and grouping are
  better per service.
- Per service, logs follow a stable pattern, which a per-service baseline
  can use.
- Services are stable (no rapid growth of new services), so per-service
  state does not explode.
- **Existing bug this fixes:** `TemplateState.service` is the *first*
  service that logged a template (`train_pipeline.py` `_rebuild_template_registry`;
  realtime `_assign_group` never updates it). `service_signature` and
  `build_service_evidence` filter `state.service == service`, so a template
  shared by two services is invisible to the second one's analysis.

**Throughput, staged:** the measured 41 logs/s vs ~915k logs/s on
`udcntt*` (2026-10-16) comes from ES search over 6,501 shards, full registry
rewrites per flush and `embed_one` per new template. v1 is one process
(section 5) built so it can later split into readers + Kafka + owners for
~900k logs/s without a rewrite.

## Design

### 1. Templates per service

**One Drain3 miner per service**, its state stored per service
(`drain3_state` table), so a worker can later load only the services it
owns.

- Template IDs are namespaced: `service:T00431`. No global counter, so
  several workers never collide.
- A template belongs to exactly one service.
- (Rejected alternative: one global miner with the service as the first
  token. It keeps IDs unique too, but the state is one blob that cannot be
  split across workers.)

### 2. Detection: statistical baseline, no training

Replaces the Isolation Forest. No model, no training data: running counters
updated live by the realtime engine. It needs **time, not volume**: about a
week of live running.

**Keys** (both independent of any grouping):

| Level | Key |
|---|---|
| Template | (service, template_id) |
| Service | service: total events, and ERROR+ events |

**Stored**, per key, per hour of day, per day: `sum` and `sumsq` of the
1-minute counts.

- Silent minutes add nothing; the number of observed minutes `n` per hour is
  shared by all keys (minutes the engine was running).
- `mean = sum / n`, `σ = √(sumsq / n − mean²)`.
- **Weekday and weekend kept separate:** last 5 weekdays and last 4 weekend
  days (2 weekends). Day type and hour of day in one configured timezone
  (Asia/Ho_Chi_Minh); ES stores UTC.
- Store only hours with events (most templates are sparse), as rows in
  `hourly_counts` (section 6). These rows are also the queryable history.
- Hours the engine was not running are skipped, not zero.
- A per-module detection key (all ERROR+ of one module) is possible, since
  each module belongs to one service. **Not in v1**; add it if operators ask
  "is module M healthy?".

**Two stages: staging day and committed baseline**

| | Staging (today) | Baseline (committed days) |
|---|---|---|
| Written by | live engine, every minute | the daily commit |
| Used for detection | no | yes |
| Content | today's `sum`/`sumsq` per key per hour | last 5 weekdays + last 4 weekend days |

- **Daily commit, on its own timer** (configured time, e.g. 02:00, in the
  configured timezone), independent of the retrain schedule: take each
  finished staging day, drop the hours the operator excluded, add it as the
  newest day of its type, drop the oldest day of that type. Detection lags
  one day; the baseline follows behavior changes (e.g. a release) within
  about 5 weekdays.
- If the engine was down at commit time, the next start commits every
  finished staging day not committed yet.
- **"Recompute baseline now"** button: applies exclusions and commits
  finished days immediately (seconds, no engine restart).
- Baseline and staging are **not** part of retrain: a failed retrain's
  restore never undoes a commit.

**Review: the engine suggests, the operator confirms**

Excluding incident hours keeps an incident out of its own baseline. Doing it
automatically for every alert is a trap: a *false* alert's normal-but-bursty
minutes would be excluded, the band would never widen, and the same false
alert would fire every day. Only a person can tell the two apart.

```
payment-svc  14:00–16:00  ALERTING  DOC-017 "Restart DB pool"  (T00431, T00433)   LLM: real incident
             [Exclude: real incident]  [Keep: normal]
lixi-svc     09:00–10:00  WARMING   undocumented (T00210)
             [Exclude]  [Keep]
+ Add range: service ▾  from ▾  to ▾        (maintenance, load test, missed incident)
```

- Suggestions: each service's non-NORMAL periods (WARMING/ALERTING/COOLING)
  widened to whole hours; saved incident cases (`occurred_at`); the LLM
  verdict when the alert was analyzed (a hint).
- Scope: one service (all its templates + the service window), whole hours.
- Any of the stored days can be edited, not only yesterday.
- **Unreviewed hours are kept.** Keeping a real incident widens one day of 5
  and heals within a week; excluding a false alert never heals.
- Warn when an exclusion leaves an hour with fewer than 3 days (that hour
  falls back to floor-only).

**Rule**

| Setting | Value |
|---|---|
| Band | `mean ± 3σ` of the current day type, using the wider of this hour's and the next hour's band (no alerts at hour edges when a ramp starts early) |
| Observed | count of the current clock minute (same unit the stats use) |
| Upper alert | outside the band **and** count ≥ floor: ERROR+ 5/min, WARN 10/min, INFO never |
| Lower alert (drop) | service level only, and only when mean ≥ 20/min |
| Hysteresis | existing state machine, 3 in a row |
| Score | maps the band edge to `score_high` so the state machine is unchanged |
| Fallback | no committed day yet: floor only; weekend → weekday profile (upper alerts only) while weekend has < 2 days |
| Young baseline | while a day type has < 5 days: `mean ± 4σ` (`young_sigma_multiplier`); drop alerts off. One day already gives 60 samples per hour; what is missing is day-to-day variation (busy Monday, month-end) |

Floors cover low counts (normal assumption invalid) and σ = 0.

**Retrain stays as today** (web schedule + "Retrain now"), and is only
Drain3 + embedding new templates. It does not touch detection or the
baseline; the baseline has its own daily commit. A template keeps its own
baseline whatever document or neighbor it is linked to.

### 3. From alert to document: templates link directly (option D, confirmed)

No stored groups. Chosen over "template → group → document" after the test
below (groups gave no better automatic matching and split one family into
two groups).

**A document has two parts** (revised 2026-10-21): **ERROR**, the log
line(s) it is for, and **Handling**, what to do. Only the ERROR lines are
embedded, each normalized like a template, so matching compares log text
with log text. The 2026-10-19 test embedded whole documents (prose, partly
Vietnamese) and was right for 7 of 76 templates; inheritance, which compares
template with template, had 0 wrong at 0.85. A document with an empty ERROR
field (incident runbooks such as DOC-016/017/018) is never auto-matched.

**A template's document, in layers** (a lower number always wins):

| # | Link source | Rule | Drives alerts |
|---|---|---|---|
| 0 | `manual` | set by a person; `doc_id NULL` = "no document" (sticky) | yes |
| 1 | `inherited` | nearest **manually** linked template of the same service and level class, similarity ≥ the class threshold. Never from a non-manual link (links don't drift) | yes |
| 2 | `corpus` | best active document by template ↔ ERROR-line similarity ≥ the class threshold and ≥ a margin above every other document | yes, badged "auto" |
| 3 | `llm` | the LLM picks one of the top 3 candidate documents (section 4a) | yes, badged "AI-confirmed" |
| 4 | `draft` | the LLM writes a suggested document, action or SAFE (section 4a) | no, until approved |

The operator can confirm any automatic link (it becomes manual) or reject
it.

**Documents per service** (decided 2026-10-21): a document belongs to one
service, or is shared by all. One shared document stretched across services
would turn vague over time, so each service gets its own specific runbook:
layers 2–3 only use the template's service's and shared documents, and when
another service already documents a similar error, the LLM adapts that
document into a draft for this service. The All-services view groups
documents with the same ERROR line into one row, so the same error is not
listed once per service.

**Same level class for ERROR matching too** (2026-10-21): a document's
ERROR line has a level class; layer 2 only links templates of that class
(an INFO "Log success …" line scored 0.95 against an error line sharing its
field list). Cross-class cases go to the LLM, which is shown the levels. Same level class only: an ERROR line must not inherit a document from an
INFO "success" line, however similar the wording.

**Thresholds per level class** (defaults are guesses; tune on real data):

```yaml
documentation:
  inherit_similarity:      # nearest manually linked template, same service and level
    ERROR: 0.85            # ERROR, FATAL, CRITICAL: documents are written for errors, inherit easily
    WARN: 0.88
    INFO: 0.92             # rarely an error: only a near-identical line inherits
  corpus_similarity: {ERROR: 0.85, WARN: 0.88, INFO: 0.92}   # template vs ERROR lines
  corpus_margin: 0.05      # over the best ERROR line of any other document
  suggest_similarity: 0.80 # "apply to similar" list; pre-ticked at the level's inherit value
  cluster_similarity: 0.85 # undocumented alerting templates joined into one row
```

**Linking a document** (from the web, or saving an LLM suggestion): the UI
lists the same service's same-level templates with similarity ≥
`suggest_similarity`, pre-ticks those ≥ the level's inherit threshold, the
person confirms. This covers siblings just under the threshold (G0000 case
in the test) with a human decision.

**Alert rows:** one per (service, document), listing its alerting templates.
Alerting templates with no document: the few currently alerting templates
of a service are clustered on the fly by embedding similarity, one row per
cluster (`cluster_similarity`). Undocumented templates go to the LLM
resolver (section 4a).

**Diffuse incidents:** a safe band over the templates linked to one
document, `Σmean_i ± 3·Σσ_i`. `σ(ΣX) ≤ Σσ` always, so the band is never too
narrow (never adds false alerts) and is recomputed from current links.
Weakness: up to √n too wide for independent templates.

**Removed:** HDBSCAN, group registry and centroids, `assignment_manager.py`,
grouping overrides/status, group lineage, the grouping UI.

**Risk:** on day one few documents exist, so merging alert rows relies on the
on-the-fly clustering.

**What the operator sees**

```
ALERTING  payment-svc · DOC-017 "Restart DB pool"
          triggered by: T00431 "Timeout acquiring connection <*>"  42/min (normal 3 ± 2)
                        T00433 "Pool exhausted for <*>"            18/min (normal 0 ± 1)
```

- Whole-service alert (diffuse spike or drop): no single cause, no
  document; offers the whole-service LLM analysis.
- Prometheus: `log_alert_state{service, doc_id}` (`doc_id="undocumented"` for the
  rest), never per template (~27k templates × 4 states would be ~108k series).

### 4. Writing documentation: by hand, with an AI chat alongside

The document editor gets an AI chat panel. **Every LLM call here is started
by the operator**; nothing in the editor is drafted automatically. The
background drafts of section 4a are separate: they land in a review queue
and change neither the editor nor any alert until a person approves them.

```
┌ Document for payment-svc · T00431, T00433 ──────────────┬ AI chat ───────────────────────────┐
│ Title      [DB connection pool exhausted          ]     │ you: why does this fail at 14:00?  │
│ Error code [DB_POOL                               ]     │ AI:  the raw lines show            │
│ Text       [1. Check active connections ...       ]     │      SQLTimeoutException from ...  │
│            [2. Restart the pool ...               ]     │      ┌ Suggested document ───────┐ │
│                                                         │      │ DB pool exhausted ...     │ │
│                                                         │      │ [Insert into editor]      │ │
│ [Check with AI]                       [Save]            │      └───────────────────────────┘ │
│                                                         │ [message...]            [Send]     │
└─────────────────────────────────────────────────────────┴────────────────────────────────────┘
```

- **The operator can always write by hand.** The editor changes only when the
  operator clicks "Insert into editor" on a suggestion.
- Each AI reply is `{reply, suggestion: {title, text, error_code} | null}`;
  a suggestion is rendered as a card the operator uses or ignores.
- **Context sent each turn:** the templates being documented (text, level,
  rate vs normal), their recent raw error lines, recalled incident cases, the
  current editor content, and the chat history.
- **Chat history lives in the browser only** (no server storage); trimmed to
  the last N turns so the prompt does not grow without bound. The evidence
  block is size-capped (see the 2026-10-13 audit: evidence size caused
  failures).
- **"Check with AI"** (reconfirm, advisory): does the editor's text explain
  and fix these templates and their raw lines? yes / partly / no, with why.
- **Save** → the "apply to similar" list (section 3) → links. Only saved,
  human-reviewed documents are ever inherited.

**Architecture**

- The **web calls the LLM directly** for this panel, with the active LLM
  profile (`llm_profiles` table, owned by the web). An exception to "only
  the engine calls the LLM", for interactive use only; analyses stay in the
  engine.
- The engine keeps the last 20 raw error lines per template in the
  `error_samples` table; the web reads them from there.
- Limits in the web: requests per minute, evidence size, request timeout.
  The API key never reaches the browser.

### 4a. LLM confirm or suggest, reviewed by a person

Revised 2026-10-21 (plan
[`2026-10-21-embeddings-doc-links.md`](../plans/2026-10-21-embeddings-doc-links.md)
Phase B; replaces the former sub-project 8 and the drafts-only adoption of
[`antigravity-proposed-additions.md`](antigravity-proposed-additions.md)).
Problem it solves: a new template at night alerts as `undocumented` with no
guidance, and nobody has time to write runbooks for hundreds of templates.

**Process.** `logai2-linker`, a process sharing only Postgres like the web;
the engine thread never waits on an LLM. For a template with no link at
layers 0–2 (section 3), of **any level, alert state or age** (revised
2026-10-21: no longer only ALERTING or first seen in the last 24 h; spec
[`2026-10-21-llm-resolve-all-templates.md`](../specs/2026-10-21-llm-resolve-all-templates.md)),
ALERTING first then ERROR > WARN > INFO, at most 5 per service per hour and
50 per day, one call at a time:

1. **Confirm (layer 3):** if some active documents have an ERROR line with
   similarity ≥ 0.60, the LLM gets the template and the top 3 of them (title,
   ERROR, Handling) and answers `{doc_id | null, reason}`. A listed `doc_id`
   becomes an `llm` link, which drives alerts, badged "AI-confirmed".
2. **Suggest (layer 4):** otherwise the LLM writes `{verdict, title,
   error_code, error, handling, confidence}`. `verdict` is `action`, or
   `safe` for a line that shows normal operation ("SAFE: normal operation,
   no action needed", plus what a spike or drop of it would mean). The
   document is stored as `status draft`, linked as `draft`, and drives
   nothing until approved.

**LLM input = template + corpus only:** service, level and template text
(already masked by normalize: numbers and ids are `<*>`), plus candidate
documents. No raw log lines for now, so no customer data leaves the network
through the LLM provider. More evidence (raw lines, rates) can be added
later behind masking.

**Operator.** Confirm turns any automatic link into manual; approving a
draft also makes the document active, and its ERROR line then matches the
same error in other services at layer 2 without another LLM call. Reject
stores `rejected` and the template is never sent to the LLM again. Views:
badges on alert cards and templates, and **Documentation → AI suggestions**
(filter SAFE for bulk approval).

**LLM profiles** as in logai-engine: `llm_profiles` (name, OpenAI-compatible
base URL, model, API key, timeout, one active), managed on a web page. The
client always sends JSON mode and strips `<think>` blocks (deepseek), waits
120 s with one retry on timeout (the proxy's 60 s timeouts).

### 5. Pipeline: Elasticsearch → engine, one process first

Elasticsearch (`udcntt*`) is the **only** source. Raw logs never reach the
database: readers turn them into counts per (service, template, module,
minute), so storage sees aggregates and state only.

**v1: one process**

```
Elasticsearch ──► engine (one pod)
                   reader loop: search_after → normalize → known-line cache
                                → aggregate per (service, template, module, minute)
                        │  plain function call, dict messages (the shape Kafka carries later)
                   owner loop: Drain3 on misses → close minute → detect → alert state
                        │  one Postgres transaction per flush: counts + alerts + templates + ES cursor
                   ▼
                 Postgres ◄── web (one URL)
```

- **Reader:** per index, sorted `search_after` on (`@timestamp`, `_doc`),
  re-reading the cursor's millisecond with `_id` dedup (existing collector
  logic); `size` 10,000; `_source` limited to the needed fields;
  `filter_path` on hits only; upper bound `@timestamp <= now - 30s`.
  Lines truncated to 8 KB before `normalize`.
- **Known-line cache:** `(service, normalized text) → template_id`, LRU with
  a size cap. A hit is only a counter increment; a miss is sent once per
  distinct text per flush with its count, so a wall of identical new lines
  costs one Drain3 call.
- **Owner:** Drain3 on misses, sums counts, closes a minute when the reader
  is past it, then for each key: adds `c` and `c²` to `hourly_counts`,
  compares with the band, steps the alert state machine.
- **Exactly once without Kafka:** the ES cursor is written in the same
  Postgres transaction as the counts it produced.
- **Ceiling (estimate):** ~25k logs/s per core (36 µs/event measured for
  normalize + Drain3 on recharge logs). Covers a slice of `udcntt*`, not
  900k/s. Measure first (step 0 below).

**Decided now, because they are expensive to change later**

| Decision | Why |
|---|---|
| Reader and owner are separate modules, joined by one call with plain dict messages | the call becomes Kafka produce/consume; nothing else changes |
| All state in Postgres, keyed by service | a worker later loads only its own services |
| Template IDs `service:T00431` | no global counter between workers |
| `services.partition = hash(service) % 60`, fixed at creation | becomes the Kafka partition; changing it reshuffles every service |
| Local-time buckets, `now - 30s` bound, 8 KB line cap, miss cap per flush | correctness rules in both versions |

**v2: horizontal, for ~900k logs/s**

```
Elasticsearch udcntt*
   │ READER ×R (StatefulSet, stateless CPU; index split by hash(index) % R,
   │            big indices split by shard with preference=_shards:k)
   ▼ one Kafka transaction per ~2 s flush: aggregates + cursor + watermark marker
Kafka (Strimzi, 3 brokers, RF 3, min.insync 2, acks=all)
   logai.counts    key = service, P = 60, retention 24 h
   logai.cursors   compacted
   logai.templates compacted (text → template_id; readers load their cache from it)
   ▼ consumer group, read_committed, cooperative-sticky, static membership
OWNER ×M (≤ 60): load partition state from Postgres on assign
   ▼ one Postgres transaction per flush, incl. Kafka offset, with partition epoch
Postgres ◄── EMBEDDER ×1 (TEI batches, inheritance) · COMMIT job (daily) · WEB ×n
```

- Kafka client: **confluent-kafka** (librdkafka; transactions and
  `read_committed`).
- Reader `transactional.id` = StatefulSet ordinal, so Kafka fences a zombie
  reader.
- Owners write with a partition **epoch**; writes with a stale epoch are
  rejected (zombie owner).
- **Watermark:** a minute closes when every reader is past `minute + 1 min`,
  or at a 5 min cap. Minutes closed by the cap are marked *incomplete*:
  counted into history, but no drop checks, and a "pipeline degraded"
  alert fires.
- Hot service: lag per partition is a metric; split its key to `service#k`
  only when that metric shows the need.

**Capacity at 900k/s (estimates, to benchmark)**

| Stage | Estimate |
|---|---|
| Readers: decode + normalize + cache lookup | ~35–45 cores |
| Owners: aggregates only | 2–4 cores |
| Kafka | 3 brokers; counts, not raw logs |
| Postgres | ~1–5k row writes/s, one primary |
| ES | **unknown, the real ceiling**: ~90 sorted pages of 10k per second |

**Build order**

| Step | What | Proves |
|---|---|---|
| 0 | Benchmark one reader on one big index: `took`, docs/s per core, normalize cost, cache hit rate; check for an ingest-time field | whether 900k/s is reachable and with how many cores |
| 1 | v1: one process, Postgres | logic, exactly-once, recovery |
| 2 | Kafka between reader and owner, ES cursor moves into the Kafka transaction, owner stores its offset | the split works |
| 3 | R readers, M owners, watermark across readers, epochs | linear scaling |
| 4 | Hot-service split, faster normalize | only if step 3 shows the need |

**One website in both versions.** Workers never serve pages. The web reads
Postgres only:

| Today | New |
|---|---|
| web reads files and engine memory | web reads Postgres |
| progress/heartbeat in engine memory | each worker writes `worker_status` every few seconds; one status page |
| buttons call the engine | a row in `commands` + `NOTIFY`; the right worker picks it up |
| live alerts from memory | `alert_state` table, polled or `LISTEN/NOTIFY` |

Prometheus scrapes `/metrics` on every pod; Grafana sums them.

### 6. Storage: PostgreSQL only

**One Postgres pod** (StatefulSet, 1 replica, image `pgvector/pgvector:pg16`,
SSD PVC). Estimated 2–4 vCPU, 8 GB RAM, 100–200 GB disk for 90 days of
history. Enough even at 900k logs/s, since only aggregates are written.

| Option | Verdict |
|---|---|
| **PostgreSQL** | **Use.** Transactions (cursor/offset with counts), several writers later, partitions for history, `pgvector`, Grafana data source |
| Prometheus | no: scrape time ≠ log time; module × template cardinality too high. Keeps `log_alert_state` for Grafana alerts only |
| Elasticsearch | no: not our cluster, no transactions, ~1 s refresh |
| SQLite | no: one writer on one node; cannot scale out |
| ClickHouse / Timescale | not now. Escape hatch for `alert_minutes` alone if it ever passes ~50k rows/s |
| Redis | nothing needs it |

**If the pod dies:** no log data is lost. ES (later Kafka) is the buffer, and
the cursor or offset commits only with the counts, so the engine waits and
catches up; detection is delayed, not wrong. **If the disk dies:**
templates, baseline, history, documents and links are lost, so backups are
required: nightly `pg_dump` CronJob to object storage (WAL archiving or a
replica later if one day of loss is too much). No PgBouncer (few pods).

**Separating services:** `service` is the **first column of every
service-scoped primary key**. Every service query (owner load, web view,
module API) is an index range scan, and services never mix. No schema or
table per service (136 × 15 tables, a migration per new service), no hash
partitioning by service. Owners later load with
`WHERE partition = ANY(my_partitions)` through `services`.

**Tables**

```sql
-- registry
services      (service PK, partition int)
modules       (module_code PK, service FK)                 -- each module belongs to one service
es_cursors    (index, shard, PK(index, shard)) → sort_values jsonb, updated_at

-- templates
templates     (service, template_id, PK(service, template_id))
              → level, text, first_seen, last_seen, embedding vector(1024)
drain3_state  (service PK) → blob bytea, version

-- counts and history, PARTITION BY RANGE (day)
service_hours (service, day, hour, PK) → n_minutes                      -- the shared n
hourly_counts (service, template_id | '*', day, hour, PK) → sum, sumsq  -- '*' = whole service ERROR+
alert_minutes (service, template_id, module_code, minute, PK) → count   -- incident minutes only

-- baseline (staging = hourly_counts of days not committed yet)
day_status    (service, day, PK) → committed bool, daytype
review_exclusions (service, day, hour_from, hour_to) → by, at
baseline      (service, template_id | '*', daytype, hour, PK) → mean, std, version

-- alerts
alert_state   (service, key, PK) → state, since, streak                 -- current state
alert_changes (service, key, at, PK) → from_state, to_state, value, band

-- documentation, web, ops
documents (doc_id PK) → title, error_code, error, handling, status (active | draft | rejected),
          source (manual | llm), verdict (action | safe), confidence             -- sections 3, 4a
document_errors (doc_id, n, PK) → text, embedding vector(1024)              -- one row per ERROR line
template_doc_links (service, template_id, PK) → doc_id (NULL = no document),
          source (manual | inherited | corpus | llm | draft | rejected), similarity, inherited_from, note
error_samples (service, template_id, at) → line                         -- last 20 per template
settings, llm_profiles, retrain_schedule, commands, worker_status
kafka_offsets, partition_epochs                                         -- v2 only
```

- `module_code` lives only in `alert_minutes`. Detection stays per
  (service, template); one template can be logged by several modules of its
  service, so the module is a label inside an incident.
- `alert_minutes` holds only minutes where the key is not NORMAL (WARMING →
  ALERTING → COOLING): exactly what the module API needs, and small. Normal
  rates come from `hourly_counts` per hour.
- Embeddings in `pgvector` replace the 113 MB pickle. Inheritance searches
  one service and level, a small set: exact search, no vector index.

**Write path:** one transaction per flush (every few seconds).

| Step | SQL | Rows (estimate) |
|---|---|---|
| minute closed | `hourly_counts` upsert `sum = sum + c, sumsq = sumsq + c²`; `service_hours.n_minutes + 1` | one per active key per minute, ~500/s |
| key not NORMAL | insert `alert_minutes` | incidents only |
| state changed | update `alert_state`, insert `alert_changes` | rare |
| new templates / Drain3 changed | insert `templates`, update `drain3_state` | a few |
| progress | update `es_cursors` (v1) or `kafka_offsets` (v2) | one |

Batches go in with psycopg3 `COPY` into a temporary table, then one
`INSERT … SELECT … ON CONFLICT`.

**Daily commit job:** marks `day_status.committed`, recomputes `baseline`
from `hourly_counts` (last 5 weekdays / 4 weekend days minus
`review_exclusions`), creates tomorrow's partitions and drops partitions
past retention (no pg_partman). "Recompute baseline now" runs the same SQL;
owners reload the baseline on `NOTIFY`.

**History kept**

| Data | Granularity | Retention (default) | Size (estimate) |
|---|---|---|---|
| `hourly_counts` | every template, every hour | 90 days | ~30k keys × 24 ≈ 720k rows/day ≈ 150 MB/day with index ≈ 13 GB |
| `alert_minutes` | per minute and module, incidents only | 90 days | small |
| `alert_changes` | every state change | 1 year | tiny |
| `error_samples` | last 20 per template | rolling | tiny |

Minute counts for every template are not kept. If needed later: the same
`alert_minutes` table without the "not NORMAL" filter, and the one table that
would move to ClickHouse.

### 7. Module alert API

Other teams ask: which templates were alerting for module M in a time range,
and at what rate. **Same detection, no second detector**: the API is a read
view over the alerts of section 2, filtered by module.

`GET /api/module-alerts?module_code=M&from=…&to=…`

```sql
-- 1. module → service
SELECT service FROM modules WHERE module_code = $M;

-- 2. templates of that service ALERTING at some point in [from, to]
--    (state at `from` = latest change before it, plus changes inside the range)
SELECT key, at, to_state FROM alert_changes
WHERE service = $S AND at <= $to
  AND (at >= $from OR (key, at) IN (SELECT key, max(at) FROM alert_changes
                                     WHERE service = $S AND at < $from GROUP BY key));

-- 3. module M's minute rates for those templates
SELECT template_id, minute, count FROM alert_minutes
WHERE service = $S AND module_code = $M AND minute BETWEEN $from AND $to
  AND template_id = ANY($alerting);
```

- Response: per template its text, document, alert periods, and module M's
  rate per minute (series, average, peak) next to the template's total.
- A template that alerted but had no logs from M is left out.
- Unknown module: 200 with an empty list. Bad time, `from >= to` or a range
  over 24 h: 400. ISO-8601 or epoch input, ISO-8601 UTC output.
- Replaces the old plan `docs/superpowers/plans/2026-10-19-module-alert-api.md`
  (groups, file store), which needs rewriting to this design.

### 8. Starting on a running server (v1: sampled catch-up)

The engine is deployed next to a server that has been logging for a long
time, with an empty Postgres. **One reader, one cursor**: no backfill
running alongside live.

```
T0 = first start (stored as settings.bootstrap_until)

cursor = T0 − 2 days
   │ sampled: read 6 whole minutes per hour, jump to the next sample
   │ minutes are historic: counted into hourly_counts, Drain3 learns, no alerts
   ▼ cursor reaches T0
   │ full reads; still historic until within 15 min of now
   ▼ caught up
first commit runs by itself → baseline from 2 days → detection on (young band, 4σ)
day 3+ : each daily commit adds one live day
day 5  : that day type has 5 days → 3σ, drop alerts on
day 14 : 2 full weekends → full baseline
```

**Sampling**

- Applies only to the range before `bootstrap_until` (T0). Catch-up after a
  later outage reads every document.
- One random whole minute in each 10-minute block of the hour (6 of 60), so
  samples spread over the hour. Each sampled minute is read completely (all
  indices, all services).
- Each sampled minute adds 1 to `service_hours.n_minutes`, so
  `mean = sum / n` and `σ` stay correct estimates; σ is just noisier.
- Templates too rare to appear in the samples get no history and use the
  floor until live days add it.
- The day T0 falls on is partly sampled and partly full; `n_minutes` covers
  both.

**Commit and review**

- When the cursor first catches up, the engine runs the commit at once (the
  same code as "Recompute baseline now"), without waiting for the 02:00
  timer.
- The sampled days hold old incidents with no alert history. The review page
  suggests hours where a service's count is far from the median of the same
  hour on the other days; the operator excludes or keeps them. Unreviewed
  hours are kept.

**Config**

```yaml
start:
  days_back: 2              # 0 = start at T0 with floors only
  sample_minutes: 6         # per hour, before bootstrap_until; 60 = read everything
baseline:
  sigma_multiplier: 3.0
  young_sigma_multiplier: 4.0
  full_after_days: 5        # per day type; also the minimum for drop alerts
```

**Wait before alerts** (estimate) = docs to read ÷ (reader speed − live rate):

| Slice | 2 days at 6/60 | 14 days at 6/60 | 2 days full |
|---|---|---|---|
| 2k/s, one process at ~25k/s | ~25 min | ~3 h | ~4 h |

Before the first commit nothing alerts, not even the floor.

**Rejected:** backfill in parallel with live (two cursors into one owner,
harder to reason about); full catch-up (hours to a day with no alerts); a
separate bootstrap job (a second program, same result).

**Big system later:** sampling 2 days at 900k/s is still ~10 B documents.
There: service-level history from ES `date_histogram` aggregations (exact,
minutes) plus template baselines built live (`days_back: 0`).

**Carried over from logai-engine:** the documentation corpus (plain text).
Manual links are not (they were per group; old IDs are gone); operators
relink with "apply to similar".

## Pipeline risks and fixes

| # | Stage | Problem | Fix |
|---|---|---|---|
| 1 | ES | document indexed after the cursor passed its `@timestamp` (shipper lag, back-fill) is lost | cursor on an ingest-time field (`event.ingested`) if present; else re-read a 2 min overlap with `_id` dedup |
| 2 | ES | refresh delay: cursor passes t before the doc is searchable | query `@timestamp <= now - 30s` |
| 3 | ES | host with a future clock moves the cursor ahead | same upper bound; metric for future docs |
| 4 | ES | `_doc` tiebreak is per shard: equal-ms docs on other shards skipped | shard-split readers; else re-read the cursor's ms + `_id` dedup |
| 5 | ES | 900k docs/s search load hurts real users | per-reader rate limit, prefer replicas, back off on 429; measure `took` first |
| 6 | ES | ILM deletes an index while the reader lags | metric: cursor age vs retention, alert early |
| 7 | ES | new / rolled-over indices | re-resolve `udcntt*` on a timer |
| 8 | ES | service or level field named differently per index, or missing | field mapping per index pattern; `unknown` service + metric; lowercase/trim |
| 9 | ES | 100 KB stack traces | truncate to 8 KB before normalize |
| 10 | reader → Kafka | crash between send and cursor save | aggregates + cursor in one Kafka transaction (v1: one Postgres transaction) |
| 11 | reader → Kafka | two readers think they own one index | stable `transactional.id`; Kafka fences |
| 12 | reader | **weak normalize** (unmasked ids/UUIDs): every line is a miss, owners run Drain3 at full rate | miss-rate metric per service; cap misses per flush (sample + `unparsed` count); fix the mask. **Most likely real problem** |
| 13 | reader | known-line cache grows without bound | LRU with a size cap |
| 14 | Kafka | broker down | RF 3, `min.insync.replicas=2`, `acks=all` |
| 15 | Kafka → owner | owner outage longer than retention | 24 h retention + consumer-lag alert |
| 16 | owner | rebalance storm reloads all state | cooperative-sticky + static membership |
| 17 | owner | zombie owner writes after its partition moved | partition epoch on every write |
| 18 | owner | crash mid-minute | offset committed only with flushed state; replay rebuilds open minutes |
| 19 | owner | hot partition | lag per partition; split key only when shown |
| 20 | owner → Postgres | Postgres slow | batch per flush; ES/Kafka absorbs the backlog |
| 21 | detection | stalled reader + 5 min cap → services look silent → false drops | cap-closed minutes marked incomplete: no drop checks, "pipeline degraded" alert |
| 22 | detection | ES or all readers down: no minute closes, no alerts, silently | "engine blind" alert on watermark age |
| 23 | detection | catch-up after an outage fires stale alerts | stage counts, no alerts for minutes older than 15 min, shown as historic |
| 24 | detection | ES stores UTC | buckets in Asia/Ho_Chi_Minh, in one place |
| 25 | detection | first start: no history, or reading 14 days fully takes a day | start 2 days back, sampled minutes (section 8) |

**Settle first:** #1 (is there an ingest-time field?), #12 (cache hit rate
on real data, step 0), #5 (ES `took` decides R and whether 900k/s is
possible).

## Open questions

1. Is there an ingest-time field (`event.ingested` or one added by Logstash)?
2. Postgres self-hosted in each cluster or managed (must allow `pgvector`)?
3. Retention: 90 days of hourly history and 1 year of alert changes OK?
4. Nightly `pg_dump` enough, or is one day of loss too much?
5. Module API rate unit: per minute or per second?
6. Name and location of the new project.
7. How many days does ES keep on `udcntt*`? (`start.days_back` must fit.)
8. ~~Raw lines to the LLM?~~ Settled 2026-10-21: the LLM gets template and
   corpus fields only (section 4a); raw lines later, behind masking.

## Test: template → document vs group centroid → document

Question: does matching **each template's embedding** to the documentation
corpus pick the right document as well as matching the **group centroid**?

Data: the live recharge stack (`logai-data` volume): template embeddings,
group centroids, documentation corpus embeddings, and the human-assigned
documents (manual overrides, incident-test targets) as ground truth.

Measure, for templates in documented groups:

1. Agreement: template's best document == group's document.
2. Similarity distributions: template→doc vs centroid→doc, best and
   runner-up (margin).
3. Coverage at the 0.75 threshold: share of templates that get a document
   each way.
4. Disagreements listed for manual review (which one is right?).

Decide D if template matching agrees with ground truth at least as often as
the centroid; otherwise G.

### Results (2026-10-19, live recharge stack)

Data: 1 service, 325 templates, 143 groups, 12 documents. Ground truth: 11
groups (76 templates) with a document chosen by people (8 manual overrides +
4 incident-test targets). **Small sample, one service: a direction, not
proof.** Script: `doc_match_test.py` (read-only, run in the engine
container).

**1. Automatic matching is weak either way, and the centroid is not better.**

| | Right | Best-similarity median | Share ≥ 0.75 |
|---|---|---|---|
| Group centroid → doc | 3 of 11 groups | 0.596 | 7% |
| Template → doc | 7 of 76 templates (same 3 groups) | 0.598 | 5% |

Most documents people chose describe an *incident type* (DOC-018 "USSD
traffic spike", DOC-017 "DB performance", DOC-016 "LIXI volume spike"). They
were assigned to groups of normal log lines (send/receive, worker params).
No log text resembles them, so no embedding match can find them. Automatic
matching is not a reason to keep groups.

**2. Manual links: groups spread one decision, nearest-neighbor nearly does.**
One manual link on G0000 covers its 15 templates. With option D,
leave-one-out inheritance from the nearest labeled template:

| Inherit threshold | Right | Wrong | No neighbor (multi-template groups) |
|---|---|---|---|
| 0.80 | 69 | 0 | 2 |
| 0.85 | 58 | 0 | 13 |
| 0.88 | 51 | 0 | 20 (13 in G0000) |

(Single-template groups always miss in leave-one-out; not counted.)

**3. Groups fragment the same family; template neighbors don't.**
G0029 (13 templates, undocumented) is the same USSD request/worker family as
G0005 (documented DOC-018). A later retrain formed it as a new group instead
of joining G0005, because placement compares against the group **centroid**.
Its templates are 0.89–0.99 similar to individual G0005 templates. At 0.88,
template inheritance gives 10 unlabeled templates the right document that
groups missed. At 0.80 it adds dubious links (e.g. "Sent request charge
success" ← DOC-016 at 0.81).

**Conclusion: option D**, with:

- inheritance from the nearest directly linked template at **0.88** (no
  wrong links seen, no dubious ones);
- an **"apply to similar templates"** step when a person links a document:
  list the same service's templates with cos ≥ 0.80, pre-tick those ≥ 0.88,
  the person confirms. This covers the G0000 case (siblings below 0.88)
  with a human decision instead of a lower threshold;
- automatic corpus matching kept only as a hint (shown, not used for
  alerts) since it is right so rarely.

## Known weak points (accepted for v1)

- Diffuse incidents across many templates, each below the floor: caught only
  by the service ERROR window (and the safe band, which is not sensitive).
- Holidays are not modeled: a holiday weekday looks like a quiet day (no
  per-group drop alerts, so no storm; service-level drop may fire).
- Saturday and Sunday share one profile.
- Until the first commit after start: no alerts. Days 1–4: young band
  (4σ) and no drop alerts; full baseline after 2 weekends (section 8).
- 3σ false-alert rate is unknown for heavy-tailed counts over thousands of
  keys; the multiplier stays configurable and is tuned with
  `scripts/incident_test.py`.
- `incident_test.py` hardcodes group IDs (`G0010`, `G_SINGLE_0051`); targets
  must be looked up by template text after the rebuild.

## Validation (after building)

With `scripts/incident_test.py`:

1. Burst on one template → it alerts within 3 checks.
2. Its (service, document) row shows ALERTING.
3. Burst spread over several templates of one row → the safe band fires.
4. The expected document is shown.
5. False alerts per hour on normal traffic vs the current Isolation Forest.
