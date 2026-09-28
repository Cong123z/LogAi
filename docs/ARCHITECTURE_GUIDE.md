# LogAI Engine: Current Architecture Guide

This is the operational map of the implementation in this repository. It is
intended to answer three questions:

1. Which process owns each decision and file?
2. What object crosses each module boundary?
3. What is durable after a crash, and what is recomputed?

ARCHITECTURE.md remains the historical/reference design. This guide is the
shorter current-state companion; when prose and code disagree, the code and
tests are authoritative.

## 1. System Shape

~~~mermaid
flowchart LR
    Producer[Application logs] --> ES[(Elasticsearch)]
    ES --> Train[Training process]
    ES --> RT[Realtime process]
    Train --> State[(data/ artifacts)]
    RT <--> State
    Web[Flask Template Explorer] <--> State
    RT --> Metrics[/metrics :9108/]
    Prom[Prometheus] --> Metrics
    Prom --> Grafana[Grafana]
~~~

There are three cooperating processes, although only two are part of the
analysis engine:

- **Training** is an offline/batch publisher. It turns a historical ES range
  into templates, semantic groups, documentation matches, feature history,
  and one global Isolation Forest.
- **Realtime** is the single long-running writer. It polls new ES hits, parses
  and assigns them, updates windows and alert state, exports metrics, and
  commits the ES cursor only after durable processing.
- **Web** is a file-backed control/read surface. It writes user intent files
  (documentation and grouping overrides) and reads the registries and status
  written by the engine. It does not run clustering or anomaly detection.

The design assumes one realtime/training writer per data directory. Atomic file
replacement protects readers from partial files, but there is no distributed
lock or multi-process transaction.

## 2. End-to-End Flows

### 2.1 Historical training

Entry point: python scripts/run_training.py.

1. ElasticsearchCollector.stream_historical_batches reads a bounded time range
   with search_after pagination. Every page yields (RawLog list, cursor) and
   does not touch the realtime checkpoint.
2. Each RawLog is deduplicated by the training event index and parsed by
   Drain3Parser. The parsed compact record is appended to
   training_event_index.jsonl and fsynced before the training cursor moves.
3. The pipeline rebuilds TemplateRegistry from the durable parsed records,
   persists normalized embeddings in template_embeddings.pkl, and runs HDBSCAN
   over all template embeddings.
4. Noise templates become singleton groups. Each group receives a normalized
   centroid in group_centroids.pkl; template group_id fields are updated.
5. GroupAssignmentManager applies web grouping intent (anchors and stable
   manual groups), validates cycles/targets, and rebuilds affected groups.
6. Historical events are replayed through the same FeatureEngine used by
   realtime. The resulting eight-dimensional vectors train the single global
   Isolation Forest (models/global_v3.pkl).
7. DocumentationMatcher assigns the best corpus entry by cosine similarity.
   Manual documentation overrides are applied separately and win over the
   automatic result.
8. The complete template/group registries, centroids, model, documentation
   fields, and grouping status are flushed. Only after publication does the
   training checkpoint and temporary event index get cleared.

Training is therefore a publisher of a coherent snapshot. Realtime never runs
HDBSCAN and never retrains the anomaly model.

### 2.2 Realtime event loop

Entry point: python scripts/run_realtime.py.

Each loop performs these phases:

1. Synchronize control-plane revisions. Grouping overrides are applied at a
   batch boundary. The documentation refresh worker separately polls corpus
   and override revisions (five seconds by default).
2. Poll Elasticsearch. poll_batch returns raw logs and the last hit's sort
   cursor without advancing checkpoint.json. Malformed hits are skipped
   individually and counted.
3. Parse and assign. Drain3 updates its persisted tree. A template with a
   known group_id uses a direct mapping. A new/unmapped template is embedded
   and compared with stored group centroids. Similarity below the configured
   threshold becomes G_PENDING and waits for a future training run.
4. Aggregate. Every grouped event updates a sliding window keyed by
   (service, group_id), not just group_id. This keeps independent service
   baselines even when they share a semantic group.
5. Predict. Feature vectors accumulate in a micro-batch. They are scored by
   GlobalAnomalyModel.predict_batch when the count or max-wait threshold is
   reached. Idle alert cells also contribute snapshot vectors so alerts can
   cool down without new traffic.
6. Transition alert state. AlertStateMachine applies ordered results and
   persists the final state for each service/group cell.
7. Durability sequence. The pipeline flushes template and group registries,
   flushes dedup state, then commits checkpoint.json. If any earlier step
   fails, the cursor is not advanced and the batch is replayed safely.

The ordering is deliberately:

~~~text
predict/apply -> registry flush -> dedup flush -> checkpoint commit
~~~

An idle-only prediction flush does not commit a cursor. A real stream batch is
considered complete only after the checkpoint commit succeeds.

### 2.3 Unknown and pending templates

Drain3 can know a template before the engine has a group assignment. The
realtime fast path first checks the registry, then falls back to embedding and
nearest-centroid assignment. If there is no acceptable centroid match:

- the template is persisted with group_id = G_PENDING;
- its embedding is retained for later grouping;
- it contributes no group feature or anomaly score;
- a later training/grouping revision can make it known.

This prevents a low-confidence online assignment from silently changing group
identity.

### 2.4 Documentation refresh and manual controls

Documentation has two independent inputs:

- automatic cosine matching from the current corpus;
- a manual group_id -> documentation_id override.

The refresh worker loads both revisions, rebuilds document embeddings if the
corpus changed, computes automatic matches, then applies valid manual overrides.
A manual override remains attached to the group ID if its template membership
changes. A membership fingerprint is retained for audit; a mismatch is shown
as stale_override instead of being applied blindly.

The UI's Clear operation is a persisted suppression in
documentation_overrides.json (cleared_groups). It works for automatic and
manual documentation, requires force=true, and keeps the group undocumented
until a new explicit assignment removes the suppression.

Deleting a document follows a two-step contract:

1. If active groups use it, the first delete returns HTTP 409 with group IDs and
   details and requires_confirmation=true.
2. The confirmed delete repeats with force=true, removes manual overrides
   referring to that document, and increments the corpus/override revisions.

The refresh worker then rematches affected groups automatically or writes
documentation_source = none when no document clears the threshold.

### 2.5 Manual grouping flow

The web API writes a desired assignment to grouping_overrides.json with an
optimistic revision. A template can target an existing group anchor or a stable
numeric group such as G_MANUAL_001. The engine notices the new revision,
flushes any in-flight prediction batch, resolves assignments, rebuilds affected
groups and centroids, and records per-template results in grouping_status.json.

The API returns 202 for accepted asynchronous work. The UI must poll
GET /api/grouping/status until the exact revision is applied, partial, or
failed. Documentation mutations are blocked while grouping membership is
unsettled so a user cannot assign a document to an obsolete snapshot.

### 2.6 Alert and metrics flow

FeatureEngine emits a FeatureVector; the global model emits an AnomalyResult;
the alert machine emits an AnomalyState. Hysteresis uses NORMAL, WARMING,
ALERTING, and COOLING plus high/low score thresholds, consecutive counts, and a
minimum one-minute event count. State is keyed by (service, group_id) and
persisted in anomaly_state.json.

MetricsExporter exposes business counters, anomaly scores, alert states,
pipeline health, grouping application outcomes, retries, malformed ES hits,
and DLQ records. Prometheus scrapes the metrics process; Grafana is only a
consumer and does not participate in decisions.

## 3. Module Map and Contracts

| Module | Owns | Input | Output / consumer |
|---|---|---|---|
| collector/es_collector.py | ES search, sort cursor, retry classification | ES hits | RawLog; training/realtime |
| parsing/drain3_parser.py | Template mining and parameter extraction | RawLog | ParsedEvent; registries/grouping |
| embedding/embedder.py | L2-normalized sentence embeddings | template/document text | NumPy vectors; clustering/matcher |
| clustering/hdbscan_cluster.py | Offline HDBSCAN, centroids, nearest-centroid lookup | template vectors | labels/centroids/group candidate |
| grouping/assignment_manager.py | Resolve web intent and rebuild memberships | base mapping, states, overrides | mapping, status, affected groups |
| storage/registries.py | Template/group metadata and vectors | dataclasses | JSON/pickle state |
| storage/checkpoint.py | Realtime/training search cursor commits | search_after, timestamp | checkpoint JSON |
| storage/dedup.py | Realtime bounded idempotency and training LRU | event IDs | dedup index / in-memory buffer |
| storage/training_event_index.py | Durable parsed-event handoff between training phases | ParsedEvent | append-only JSONL |
| storage/base.py | Atomic JSON, pickle, and model-store primitives | Python objects | file artifacts |
| docmatch/doc_matcher.py | Corpus validation, embedding cache, cosine match | corpus + centroid | MatchResult; refresh/training |
| docmatch/refresh_worker.py | Async documentation revision application | corpus/override revisions | group documentation fields |
| features/feature_engine.py | Sliding windows and baseline statistics | (service, group_id), event time | FeatureVector; training/model |
| anomaly/isolation_forest_model.py | One global model, vectorized inference | vectors | AnomalyResult; realtime |
| alert/alert_state_machine.py | Hysteresis and persisted alert cells | AnomalyResult | AnomalyState; metrics/web |
| reliability/retry.py | Generic retry decorator | callable | retry/backoff behavior |
| reliability/dlq.py | Append-only failed-event records | payload/error | dlq.jsonl; manual replay |
| metrics/prometheus_exporter.py | Prometheus names/labels | events/states | /metrics |
| web/app.py | HTTP reads/mutations and conflicts | JSON + files | API responses; UI |
| web/static/index.html | Browser views, polling, confirmation dialogs | API responses | operator actions |
| config.py | Dataclass defaults, YAML merge, environment overrides | config.yaml/env | AppConfig |

### 3.1 Core runtime objects

These dataclasses in logai/models.py are the main in-process contracts.

**RawLog**

~~~text
timestamp: float       # epoch seconds
service: str
level: str
message: str
metadata: dict         # all non-core ES fields
event_id: str          # ES _id in collector paths
es_index: str | None
es_doc_id: str | None
~~~

The collector defaults missing service/level/message to unknown/INFO/empty
string. Invalid timestamps fall back to processing time; numeric timestamps are
treated as epoch seconds, not milliseconds.

**ParsedEvent**

~~~text
raw: RawLog
template_id: str      # T plus zero-padded Drain3 cluster ID
template: str         # may contain <*>
parameters: list[str] # best-effort wildcard values
is_new_template: bool
~~~

**TemplateState**

~~~text
template_id, template_text, service
level, module
first_seen, last_seen, event_count
group_id: str | None
~~~

level is promoted monotonically to the most severe known level. It is display
context and does not control anomaly state.

**GroupState**

~~~text
group_id, service, module
template_ids, representative_template
event_count, first_seen, last_seen, active
documented, documentation_id, confidence
error_code, documentation_source
~~~

documentation_source is one of automatic, manual, stale_override, or none.
Group identity is a plain string; service-specific identity appears only in
windows, anomaly results, and alert state.

GroupedEvent carries the parsed event, its group ID, and the similarity used
for a nearest-centroid assignment. Direct known-template mappings use
similarity 1.0.

FeatureVector has the window key (service, group_id), timestamp, and exactly
eight model dimensions in this order:

~~~text
z_score_10s, z_score_1m, short_growth_rate, growth_rate,
burstiness_10s, rate_delta_norm, slope_norm, spike_ratio_10s
~~~

count_1m is alert-gating metadata and is intentionally excluded from
as_vector(). Changing this order or dimension requires retraining and a model
version change.

AnomalyResult contains the same tuple key, timestamp, normalized anomaly_score
in [0, 1], boolean anomaly flag, model version if-global-v3, and optional
count_1m.

AnomalyState adds consecutive count and alert_state. On disk the tuple key is
encoded as a JSON-list string for compatibility; the alert machine converts it
back to a tuple at its boundary.

### 3.2 HTTP API contracts

The Flask app is a thin adapter over the files above. Read endpoints are
snapshot reads; mutation endpoints use the revision returned by the preceding
read:

| Endpoint | Contract |
|---|---|
| GET /api/templates | filters, sorting, pagination, and full template stats |
| GET /api/templates/<id> | one template, current group, and grouping sync context |
| PUT /api/templates/<id>/group | exactly one of target_group_id or create_new=true; returns accepted grouping revision |
| GET /api/groups | current groups, documentation fields, counts, and override revision |
| GET /api/grouping/status | engine heartbeat plus per-template revision results |
| GET /api/alerts | persisted alert cells enriched with current group/template context |
| GET /api/documentation | corpus entries, group counts, revisions, and sync status |
| POST/PUT /api/documentation | revision plus validated title/text/error_code; returns new corpus revision |
| DELETE /api/documentation/<id> | first use while assigned returns 409 details; force=true confirms deletion |
| PUT /api/groups/<id>/documentation | documentation_id plus override_revision; clears suppression |
| DELETE /api/groups/<id>/documentation | force=true confirms clear and persists cleared_groups suppression |
| GET /api/health | grouping/documentation synchronization and runtime heartbeat |

Important status codes are 400 for malformed payloads, 404 for unknown IDs,
409 for stale revisions, confirmation-required deletes/clears, or unsettled
grouping, and 500 for storage failures. A 202 response means the engine has
accepted asynchronous work; the status endpoint is the completion signal.

## 4. Persistent State and Ownership

All paths below are relative to StorageConfig.base_dir (normally data/).

| Artifact | Writer | Meaning |
|---|---|---|
| template_registry.json | training/realtime engine | current template metadata and group IDs |
| template_embeddings.pkl | training/realtime engine | normalized template vectors |
| group_registry.json | training/realtime/refresh worker | group metadata and documentation fields |
| group_centroids.pkl | training/grouping engine | normalized group centroids |
| drain3_state.bin | Drain3 parser | parser tree and template continuity |
| models/global_v3.pkl | training | global Isolation Forest |
| checkpoint.json | realtime | last committed ES search_after and timestamp |
| training_checkpoint.json | training | resumable historical cursor |
| training_event_index.jsonl | training | fsynced parsed-event handoff |
| dedup_index.json | realtime | bounded LRU of processed IDs |
| dlq.jsonl | realtime | events that exhausted failure paths |
| anomaly_state.json | realtime alert machine | latest state per service/group |
| documentation_corpus.json | web/API | runtime documentation source |
| documentation_overrides.json | web/API | assignments and cleared_groups |
| documentation_status.json | refresh worker | applied revisions and errors |
| grouping_overrides.json | web/API | desired template-to-group intent |
| grouping_status.json | training/realtime engine | applied revision, heartbeat, results |

JSON metadata and pickle artifacts use temporary files plus os.replace; the
grouping and documentation stores also flush and fsync before replacement.
JSON stores are thread-safe inside one process. They are not cross-process
transactions.

## 5. Revision and Consistency Contracts

### 5.1 Optimistic revisions

Documentation and grouping mutations include the revision most recently read
by the client. A mismatch returns HTTP 409. This prevents two browser tabs from
silently overwriting each other, but it is not a distributed transaction with
the engine.

### 5.2 Asynchronous application

The web writer changes intent first; the engine applies it later. Therefore a
successful write means accepted, not already visible in the registries. The
status files are the acknowledgement channel. The UI should wait for the same
revision to be reported as applied before presenting the mutation as complete.

### 5.3 Crash/replay semantics

If realtime dies before checkpoint.json is committed, ES replays the batch. The
dedup index and idempotent registry updates prevent duplicate business effects.
If it dies after the checkpoint commit, the batch is durable by the pipeline's
contract. If a registry write or dedup flush fails, the checkpoint is deliberately
left behind and the process raises so an operator can inspect the failure.

The bounded dedup index is not an infinite ledger: old IDs can be evicted. A
replay older than the retained window can therefore be processed again.

### 5.4 Documentation safety

The matcher keeps the last known-good in-memory corpus if a reload fails. A
failed revision remains visible as failed in status. A deleted document is never
silently replaced by a stale manual override: the override is removed, then
automatic matching or none is applied.

## 6. Configuration and Local Operation

Configuration is loaded in this order: dataclass defaults, YAML, then supported
environment overrides. Important variables include LOGAI_ES_HOSTS, LOGAI_ES_USER,
LOGAI_ES_PASSWORD, LOGAI_STORAGE_BASE_DIR,
LOGAI_DOCUMENTATION_CORPUS_PATH, LOGAI_EMBEDDING_ENDPOINT,
LOGAI_EMBEDDING_API_FORMAT, LOGAI_EMBEDDING_MODEL,
LOGAI_EMBEDDING_DIMENSION, LOGAI_EMBEDDING_BATCH_SIZE,
LOGAI_EMBEDDING_TIMEOUT_SECONDS, LOGAI_EMBEDDING_MAX_RETRIES,
LOGAI_METRICS_HOST, and LOGAI_METRICS_PORT.

The embedding endpoint is mandatory for training and realtime. It must return
the 1024-dimensional dense output from `BAAI/bge-m3`; LogAI validates and L2
normalizes every response. An optional bearer token is read from
`LOGAI_EMBEDDING_API_KEY` and must be supplied through a Secret.

For the local web UI on the requested address:

~~~bash
LOGAI_WEB_DATA_DIR=data \
  python scripts/run_web.py --host 127.0.0.1 --port 5556
~~~

The realtime process exposes Prometheus on the configured metrics port (default
9108). The web health endpoints are:

~~~text
GET /api/health
GET /api/grouping/status
GET /api/documentation
~~~

Start training once before realtime so registries, centroids, and the global
model exist. The web service may be restarted independently because it reads
the shared files on each request.

## 7. Failure Modes and Recovery

| Failure | Behavior | Recovery |
|---|---|---|
| malformed ES hit | skip one hit; increment malformed metric | repair source data; cursor still advances |
| transient ES/network error | exponential backoff with jitter | collector retries; realtime adds poll backoff |
| non-retryable ES 400/401/403/404 | fail fast | fix query, permissions, or mapping |
| event processing exception | write the event as a terminal DLQ record | inspect/replay dlq.jsonl manually |
| model missing or wrong dimension | no anomaly result | run training and verify model/features |
| low-similarity new template | persist G_PENDING | run training/grouping revision |
| invalid grouping revision | status failed with reason code | correct override through API |
| stale grouping heartbeat | web health unavailable | restart realtime/training writer |
| documentation revision conflict | HTTP 409 | reload current revision and retry |
| documentation refresh error | keep last known-good matcher | fix corpus/embeddings |
| crash during file write | old complete file remains | restart; replay from checkpoint |

## 8. Reading the Code in Dependency Order

For a first deep read, follow this order:

1. logai/models.py - shared vocabulary and tuple-key boundaries.
2. logai/config.py - defaults and artifact locations.
3. logai/collector/es_collector.py and logai/parsing/drain3_parser.py -
   external input normalization.
4. logai/training/train_pipeline.py - how a model snapshot is published.
5. logai/realtime/realtime_pipeline.py - runtime ordering and durability.
6. logai/storage/registries.py, storage/documentation.py, and storage/grouping.py -
   file ownership and revision contracts.
7. logai/docmatch/refresh_worker.py - asynchronous documentation application.
8. logai/features/feature_engine.py, anomaly/isolation_forest_model.py, and
   alert/alert_state_machine.py - serving inference and alerting.
9. logai/web/app.py and docs/WEB_UI.md - HTTP/UI behavior and operator workflows.

The tests under tests/ are executable examples of the contracts. Start with
test_realtime_crash_load_and_perf.py, test_realtime_grouping_refresh.py,
test_group_documentation_refresh.py, test_web_documentation_api.py, and
test_web_grouping_api.py.
