# Web UI, Documentation Editing, and Alerting

The Flask web application is a desktop-oriented operational interface over the
files produced by training and realtime processing. It does not maintain a
separate database. The web process and realtime engine must use the same data
directory.

## Run the UI

```bash
LOGAI_WEB_DATA_DIR=data python scripts/run_web.py --host 0.0.0.0 --port 5555
```

Open `http://localhost:5555`. The sidebar uses URL hashes, so each view can be
bookmarked directly:

| View | URL | Data source |
|---|---|---|
| Templates | `/#templates` | `template_registry.json`, `group_registry.json` |
| Groups | `/#groups` | Group registry plus documentation corpus/overrides |
| Alerting | `/#alerting` | `anomaly_state.json`, enriched from group/template registries |
| Documentation | `/#documentation` | Editable corpus and synchronization status |
| Data sources | `/#sources` | `es_index_selection.json`, `es_index_status.json` |

The sidebar remains fixed on desktop. Reload is available from every view and
performs a full page refresh. If an API request fails while the server restarts,
the page displays `Reconnecting...` and retries every two seconds; Chrome does
not need to be restarted.

## Templates

Templates support search, service and log-level filters, known/unknown status,
sortable columns, pagination, optional auto-refresh, and a detail dialog.
Template `level` is the most severe log level observed for that template and is
monotonically promoted by the pipeline.

The detail dialog can assign a known or pending template to an existing group,
or create a stable sequential manual group (`G_MANUAL_001`, `G_MANUAL_002`, and
so on). Assignment is asynchronous: the UI retains a
visible pending/partial/failed/applied result after the dialog closes and polls
engine status. A `202` response never produces a success notification by itself.

## Groups

The Groups view shows Total Groups, Documented, and Undocumented counts. Its
table can be sorted by group ID, service, representative template, error code,
documentation source, or event count. The Error code column displays the error
code from the active documentation match, not the internal documentation ID.
Each group row also has a template-count toggle. Expanding it shows every member
template with its ID, full template text, service, level, and event count inline;
selecting the toggle again collapses the list.

`documentation_source` has these values:

| Value | Meaning |
|---|---|
| `automatic` | Cosine matching selected the document. |
| `manual` | A valid user assignment overrides cosine matching and remains bound to the group ID when membership changes. |
| `stale_override` | The override references a document that is no longer in the corpus. |
| `none` | No document currently meets the matching rules. |

The `Undocumented only` checkbox filters the table without changing the summary
counts. Assign and Change update the persistent override file. Clear is
available for both manual and automatic documentation, asks for confirmation,
and persists a clear suppression so the group remains undocumented until a new
document is explicitly assigned. The realtime engine applies each new revision
asynchronously and the UI polls until that exact revision is applied. These
controls are disabled while a non-empty template-grouping revision is not yet
applied, preventing an assignment against obsolete group membership.

## Alerting

The Alerting view polls `GET /api/alerts` every two seconds only while that view
is active. It shows all current service/group alert cells and places active
alerts first by default. Groups without a persisted anomaly state are included
as `NORMAL`.

The alert condition comes exclusively from `AnomalyState.alert_state`:
`NORMAL`, `WARMING`, `ALERTING`, or `COOLING`. The page can filter by those
states and sort by state, level, group, service, error code, anomaly score,
consecutive count, or last evaluation time.

The Level column is derived display context. Groups do not own a log level and
`GroupState.severity` is not populated. For each service/group row, the API
looks at the group's templates and displays the most severe observed template
level using `LEVEL_RANK`. Level does not affect the alert-state machine. Alert
state is calculated from anomaly score, event-volume gating, and hysteresis.

## Documentation

`docs/documentation_corpus.yaml` is seed data only. On first startup it is
validated and imported into `data/documentation_corpus.json`; later YAML edits
do not replace the runtime corpus. Users manage the runtime corpus through the
Documentation view.

Each entry contains:

| Field | Rules |
|---|---|
| `id` | Generated in monotonic order (`DOC-001`, `DOC-002`, ...); stable on update and never reused after deletion. Existing seed and legacy IDs are preserved. |
| `title` | Optional string, at most 200 characters. |
| `text` | Required non-empty string, at most 20,000 characters. |
| `error_code` | Optional string, at most 100 characters. |

The Groups count is the number of current `group_registry.json`
records whose `documentation_id` points to that entry. The table defaults to
highest group count first and can also sort by ID, title, or error code.

Every mutation includes the revision returned by the latest read. A stale
revision returns HTTP 409 instead of overwriting another user's change. The
first delete request for a document used by an active group returns HTTP 409
with the affected group IDs and details; the UI then asks for explicit
confirmation. A confirmed request repeats the delete with `force: true`,
removes manual overrides for that document, and returns pending corpus and
override revisions. The refresh worker clears the deleted assignment and
recomputes automatic matching, leaving groups with no replacement
undocumented. Overrides for deleted groups are orphans and do not block
deletion.

The realtime refresh worker checks corpus and override revisions every five
seconds by default. It reloads document embeddings, recomputes automatic
matches, applies valid manual overrides, and atomically updates only the
documentation fields in the group registry. The Documentation page reports one
of these synchronization states:

| State | Meaning |
|---|---|
| `applied` | The engine applied the current corpus and override revisions. |
| `pending` | The web files changed but the engine has not applied them yet. |
| `failed` | The engine attempted this revision and recorded an error. |
| `engine_unavailable` | No refresh-worker status has been written. |

## Data sources

Choose which Elasticsearch indices the engine reads. Tick indices from the
**Available indices** list (published by the engine, which owns the ES
credentials) or type a pattern such as `app-logs-*`, then **Save selection**.
The engine applies it within about a second, with no restart:

- every selected index is read from the moment it is added, with no backfill;
- a pattern is re-resolved every 30 seconds, and an index created later under it
  is read from its first document;
- removing an index stops reading it, and adding it again starts from that moment.

Each selected pattern lists the indices it resolves to, with the last event
time and the number of events read. The status line shows `Waiting for the
engine…` until the engine has applied the saved revision. Until the first save,
the engine reads `elasticsearch.index` (`LOGAI_ES_INDEX`) as before. Two browsers
saving from the same snapshot get `409`; the page keeps the unsaved changes.

## HTTP API

| Method and path | Input | Output |
|---|---|---|
| `GET /api/templates` | Search/filter/sort/page query parameters | Paginated templates and global template stats |
| `GET /api/templates/<id>` | Template ID in path | Template plus group details |
| `PUT /api/templates/<id>/group` | `expected_revision` plus `target_group_id` or `create_new` | Accepted assignment revision (`202`) or effective no-op (`200`) |
| `GET /api/grouping/status` | None | Current revision, engine state, per-template results and retryability |
| `GET /api/health` | None | Heartbeat-based healthy/degraded/unavailable state and pipeline progress timestamps |
| `GET /api/groups` | Optional `service`, `documented`, `search` | Groups, manual override metadata, override revision |
| `GET /api/alerts` | None | Current alert rows, total, and counts by alert state |
| `GET /api/documentation` | None | Entries, group counts, corpus revision, synchronization status |
| `POST /api/documentation` | `revision`, `title`, `text`, `error_code` | Created entry and new corpus revision |
| `PUT /api/documentation/<id>` | `revision`, `title`, `text`, `error_code` | Updated entry and new corpus revision |
| `DELETE /api/documentation/<id>` | `revision`, optional `force` | Protected 409 with affected groups, or confirmed deletion with corpus/override revisions |
| `PUT /api/groups/<id>/documentation` | `documentation_id`, `override_revision` | Pending assignment and new override revision |
| `DELETE /api/groups/<id>/documentation` | `override_revision`, `force` | Confirmation-required 409 or pending clear suppression and new override revision |
| `GET /api/es-indices` | None | Selection (`entries`, `revision`, `source`), engine `state`, `resolved`, per-index progress, `available` indices |
| `PUT /api/es-indices` | `patterns`, `revision` (`null` before the first save) | Saved selection (`202`); `409` on a stale revision, `400` on an invalid name |

Mutation errors use JSON with an `error` and `message`. Important status codes
are `400` for invalid input, `404` for an unknown document/group, `409` for a
revision conflict, an assigned document awaiting confirmation, or unsettled
template grouping, and `500` for a storage failure. An assigned-document
response includes `group_ids`, a `groups` detail list, and
`requires_confirmation: true`.

The API has no authentication or authorization. Do not expose the write routes
directly to an untrusted network; enforce access at the ingress, reverse proxy,
or network-policy layer.

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| `doc_matcher.corpus_path` | `data/documentation_corpus.json` | Runtime corpus |
| `doc_matcher.seed_corpus_path` | `docs/documentation_corpus.yaml` | First-start seed |
| `doc_matcher.overrides_path` | `data/documentation_overrides.json` | Manual assignments |
| `doc_matcher.status_path` | `data/documentation_status.json` | Refresh status |
| `doc_matcher.refresh_interval_seconds` | `5` | Engine revision polling interval |
| `storage.grouping_overrides_file` | `grouping_overrides.json` | Web-owned grouping intent |
| `storage.grouping_status_file` | `grouping_status.json` | Engine-owned application results |
| `grouping.heartbeat_interval_seconds` | `5` | Engine status heartbeat cadence |
| `grouping.engine_status_stale_seconds` | `45` | Unavailable threshold used by API/readiness |
| `LOGAI_WEB_DATA_DIR` | Storage base directory | Web registry/alert data directory |
| `LOGAI_WEB_HOST` | `0.0.0.0` | Web bind address |
| `LOGAI_WEB_PORT` | `5555` | Web bind port |

`LOGAI_STORAGE_BASE_DIR` relocates the engine's storage and documentation files
together. `LOGAI_DOCUMENTATION_CORPUS_PATH` can override only the corpus path.
Keep the web data directory and all documentation paths on the same shared
volume as the realtime engine.

## Persistent Files

| File | Writer | Purpose |
|---|---|---|
| `documentation_corpus.json` | Web UI/API | Runtime documentation source of truth and revision |
| `documentation_overrides.json` | Web UI/API | Manual group-to-document assignments and group fingerprints |
| `documentation_status.json` | Realtime refresh worker | Attempted/applied revisions, stale groups, and last error |
| `grouping_overrides.json` | Web UI/API | Desired anchor and stable-manual-group assignments |
| `grouping_status.json` | Training/realtime engine | Per-template results, failure codes, heartbeat, and progress |
| `group_registry.json` | Training/realtime engine | Group metadata and currently applied documentation match |
| `anomaly_state.json` | Realtime alert state machine | Latest state for each `(service, group_id)` alert cell |
| `es_index_selection.json` | Web UI/API | Selected Elasticsearch indices/patterns and when each was added |
| `es_index_status.json` | Realtime engine | Available indices, pattern resolution, per-index progress, applied revision |

Corpus, override, and status writes use a temporary file followed by
`os.replace`. The design assumes one realtime writer and one shared data volume;
it does not provide multi-process transactions or distributed locking.

## Implementation Map

| File | Responsibility |
|---|---|
| `logai/web/static/index.html` | Sidebar views, editing dialogs, sorting, filters, reload/reconnect, alert polling |
| `logai/web/app.py` | Read APIs plus documentation and assignment mutation APIs |
| `logai/storage/documentation.py` | Validation, revisions, atomic persistence, synchronization status |
| `logai/storage/grouping.py` | Grouping schemas, revisions, conflicts, status and heartbeat |
| `logai/grouping/assignment_manager.py` | Override resolution and affected-group rebuilding |
| `logai/docmatch/refresh_worker.py` | Runtime corpus reload and group documentation refresh |
| `logai/docmatch/doc_matcher.py` | JSON corpus loading, embeddings, automatic/forced matching |
| `logai/storage/registries.py` | Atomic documentation-field updates on groups |
| `logai/realtime/realtime_pipeline.py` | Starts and stops the documentation refresh worker |
| `logai/training/train_pipeline.py` | Initializes and consumes the runtime documentation corpus |
| `tests/test_web_documentation_api.py` | Web/API, alert enrichment, revision, and assignment coverage |
| `tests/test_documentation_store.py` | Corpus validation and persistence coverage |
| `tests/test_group_documentation_refresh.py` | Automatic/manual/stale refresh behavior |
| `tests/test_grouping_store.py` | Grouping persistence, conflicts, cycles, freshness |
| `tests/test_group_assignment_manager.py` | Resolution, centroids, deletes, snapshot replacement |
| `tests/test_web_grouping_api.py` | Mutation/read contracts and orphan alert filtering |
| `tests/test_realtime_grouping_refresh.py` | Batch boundary and write-failure behavior |
