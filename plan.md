# Manual Template-to-Group Assignment Implementation Plan

## 1. Objective

Allow a user to change template membership without restarting realtime processing:

- Assign an unknown or `UNASSIGNED_PENDING` template to an existing group.
- Move an already-grouped template from group A to group B.
- Create a new stable manual group from a template.
- Preserve manual decisions across engine restarts and later training runs.
- Recompute affected group metadata and centroids before subsequent events are processed.
- Keep steady-state throughput within 2% of the current 5,000 logs/second.

An assignment becomes effective at a realtime batch boundary. Events completed before
the boundary retain their old group. Every event processed after the boundary uses the
new group for feature generation, prediction, alerts, and metrics.

## 2. Required Semantics

For a template `T` moved from group A to group B:

1. Finish and score any already-buffered vectors under A.
2. Update `T.group_id` to B.
3. Rebuild metadata and centroids for A and B.
4. Persist the template registry, group registry, and centroid registry.
5. Mark the grouping revision as applied.
6. Process the next event for T as `(service, B)`.

The first post-change event must execute:

```python
GroupedEvent(parsed=parsed, group_id="B")
feature_engine.update((parsed.raw.service, "B"), parsed.raw.timestamp)
```

It must never add another event from T to A after the applied boundary.

Historical events and completed predictions are not rewritten. If A remains nonempty,
its existing sliding-window history remains and the contribution from T ages out under
the configured 10-second, 1-minute, and 5-minute windows. B retains its existing
baseline and starts receiving new T events. If A becomes empty, delete A and remove its
feature and alert state because it can no longer receive events.

## 3. Storage Ownership and Schemas

Use two new JSON files because the web process and engine are separate OS processes and
the current file stores have no cross-process transaction or locking mechanism.

- `grouping_overrides.json` is written only by the web API. It represents desired user
  intent.
- `grouping_status.json` is written only by the realtime or training engine. It records
  whether that intent has been applied.

Do not combine these files. Combining them would allow the web and engine processes to
overwrite one another during atomic `os.replace` operations.

Add these fields to `StorageConfig` and `config.yaml`:

```yaml
storage:
  grouping_overrides_file: "grouping_overrides.json"
  grouping_status_file: "grouping_status.json"
```

### 3.1 `grouping_overrides.json`

```json
{
  "schema_version": 1,
  "revision": "sha256-of-assignments-and-manual-groups",
  "updated_at": 1780000000.0,
  "assignments": {
    "T00012": {
      "target_kind": "anchor",
      "target_id": "T00004",
      "assigned_at": 1780000000.0
    },
    "T00025": {
      "target_kind": "manual_group",
      "target_id": "G_MANUAL_a1b2c3d4e5f6",
      "assigned_at": 1780000010.0
    }
  },
  "manual_groups": {
    "G_MANUAL_a1b2c3d4e5f6": {
      "created_at": 1780000010.0
    }
  }
}
```

Rules:

- `revision` is a deterministic SHA-256 over `assignments` and `manual_groups` only.
- An `anchor` means the source template follows the effective group containing the
  anchor template. This survives unstable HDBSCAN `Gnnnn` IDs across retraining.
- A `manual_group` target is a stable server-generated ID matching
  `G_MANUAL_[0-9a-f]{12}`.
- Reassigning a source template replaces its previous assignment.
- Anchor graphs must be acyclic.
- A manual-group definition is removed when no assignment targets it and it has no
  effective members.

### 3.2 `grouping_status.json`

```json
{
  "schema_version": 1,
  "applied_revision": "revision-or-null",
  "attempted_revision": "revision-or-null",
  "last_attempt_at": 1780000001.0,
  "last_applied_at": 1780000001.1,
  "unresolved": {
    "T00012": "anchor template T00004 is not present"
  },
  "error": null
}
```

Synchronization state is derived as follows:

- `applied`: `applied_revision` equals the current override revision and `error` is null.
- `failed`: `attempted_revision` equals the current revision and `error` is non-null.
- `pending`: the current revision differs from both applied and failed revisions.
- `engine_unavailable`: no status has been written.

Unresolved individual assignments do not fail the entire revision. Apply all resolvable
assignments, report missing source or anchor templates in `unresolved`, and retry them
when a relevant template later appears.

## 4. Grouping Store

Create `logai/storage/grouping.py` with a `GroupingOverrideStore` responsible for:

- Initializing the override file with an empty revisioned snapshot.
- Loading and validating both schemas.
- Atomic writes using a temporary file plus `os.replace`.
- Optimistic revision checks for web mutations.
- Creating stable manual group IDs.
- Replacing a template assignment.
- Detecting prospective anchor cycles before writing.
- Loading and writing engine synchronization status.
- Returning the current revision and derived synchronization state.

Define grouping-specific exceptions for revision conflicts, invalid targets, cycles,
and invalid schema. Map them to the same HTTP error shape currently used by the
documentation APIs.

Cache the last observed `st_mtime_ns` and revision. An unchanged check must perform only
`Path.stat()` and must not reopen or parse the JSON file.

## 5. Assignment Resolution and Registry Mutation

Create `logai/grouping/assignment_manager.py` with a `GroupAssignmentManager` shared by
training and realtime.

### 5.1 Resolution algorithm

Input:

- Current `template_id -> group_id` mapping.
- Current template registry.
- Override snapshot.

Output:

- Effective `template_id -> group_id` mapping.
- Set of changed templates.
- Set of affected old and new groups.
- Per-template unresolved reasons.

Resolve each assignment recursively:

- `manual_group`: return the stable manual group ID.
- `anchor`: resolve the anchor template's effective group, including any override on
  the anchor.
- Missing source templates are unresolved and skipped.
- Missing anchor templates are unresolved; the source retains its automatic/current
  group until the anchor returns.
- Cycles are rejected by the web API and guarded again in the engine.

### 5.2 Applying a realtime mapping change

For every changed template:

- Ensure its embedding exists. Use the cached template embedding when available.
- If a legacy pending template lacks an embedding, call `embed_one(template_text)` once
  in the engine and cache it with `flush=False`.
- Set the new `TemplateState.group_id` with `flush=False`.

For affected groups only:

- Rebuild membership from effective template states.
- Sort `template_ids` for deterministic persistence.
- Choose the representative template by highest `event_count`, then lowest
  `template_id` as a tie-breaker.
- Derive `service` from the representative template, preserving current cross-service
  grouping behavior.
- Set `first_seen` to the minimum member value.
- Set `last_seen` to the maximum member value.
- Set `event_count` to the sum of member counts.
- Preserve existing documentation fields until the documentation worker refreshes them.
- Compute the centroid as the L2-normalized mean of all member embeddings.
- If no members remain, delete the group and its centroid.

Extend `GroupRegistry` with locked, deferred-flush operations:

- `delete(group_id, flush=False)`
- `set_centroid(group_id, centroid, flush=False)`
- `delete_centroid(group_id, flush=False)`
- `replace_all(groups, centroids)` for a complete training snapshot

Do not save the entire centroid pickle for every individual group mutation. Save it once
after all affected groups have been updated.

## 6. Web API Contract

Add one mutation endpoint:

```http
PUT /api/templates/<template_id>/group
```

Assign to an existing group:

```json
{
  "target_group_id": "G0004",
  "expected_revision": "current-grouping-revision"
}
```

Create a new manual group:

```json
{
  "create_new": true,
  "expected_revision": "current-grouping-revision"
}
```

For an existing automatic group, choose its anchor deterministically from its current
members, excluding the source template: highest `event_count`, then lowest
`template_id`. For an existing manual group, store a `manual_group` target directly.

Successful mutation response:

```json
{
  "template_id": "T00012",
  "requested_target": {
    "kind": "anchor",
    "id": "T00004",
    "current_group_id": "G0004"
  },
  "revision": "new-revision",
  "state": "pending"
}
```

Return codes:

- `202`: new assignment accepted and awaiting engine application.
- `200`: requested assignment is already the effective assignment; no new revision.
- `400`: malformed body, pending target, or invalid target.
- `404`: source template or target group does not exist.
- `409`: stale revision or anchor cycle.

Extend `GET /api/templates/<template_id>` with:

```json
{
  "manual_assignment": {
    "target_kind": "anchor",
    "target_id": "T00004",
    "assigned_at": 1780000000.0
  },
  "grouping_revision": "...",
  "grouping_synchronization": {
    "state": "applied"
  }
}
```

Extend `GET /api/groups` with `grouping_revision` and
`grouping_synchronization`. Registry files remain read-only to the web process; the web
API writes only the override and existing documentation files.

## 7. Realtime Integration and Activation Latency

Instantiate the grouping store and assignment manager in `RealtimePipeline`.

Check for grouping changes twice per loop:

1. Immediately before `collector.poll_batch()`.
2. Immediately after polling returns and before processing the fetched batch.

This is not tied to the 5-second documentation refresh. With the current
`poll_interval_seconds: 1`, an idle healthy engine normally observes a command within
one second. Under continuous load, it applies at the next batch boundary. If an ES
request is already blocked, detection can be delayed up to the configured
`request_timeout_seconds` of 30 seconds.

When a new revision is found:

1. Flush `_pending_predictions` and any pending cursor under the old mapping.
2. Resolve and apply assignments.
3. Remove runtime feature and alert state for groups deleted because they became empty.
4. Flush template metadata/embeddings and group metadata/centroids once.
5. Write `grouping_status.json` only after those artifacts are durable.
6. Invalidate affected groups in the documentation refresh worker.
7. Continue with the fetched or next ES batch.

If the command arrives while a batch is being processed, finish that batch under the
old mapping and apply the command before processing the next batch. Never split or
retroactively reclassify an in-progress batch.

The documentation worker remains asynchronous. Group mapping and centroids are active
immediately after step 5; documentation may refresh on its next worker pass. Add a
thread-safe invalidation set so affected groups are rematched even when corpus and
documentation-override revisions did not change.

Once applied, `_assign_group()` continues using its existing known-template O(1) path.
No override lookup, JSON parsing, centroid computation, or additional lock is allowed
on the per-event hot path.

## 8. Feature and Alert State Changes

Add `FeatureEngine.drop_group(group_id)` to remove all `(service, group_id)` windows for
a deleted empty group. Do not reset windows for groups that still have members.

Add `AlertStateMachine.drop_group(group_id)` to:

- Delete every persisted `(service, group_id)` state for the deleted group.
- Remove matching entries from `_non_normal`.
- Flush once after all matching cells are removed.

Ensure `/api/alerts` ignores persisted states whose group no longer exists, protecting
the UI from legacy orphan states.

No state is moved from A to B. This avoids incorrectly transferring aggregate events
from unrelated templates that remain in A. The global anomaly model requires no
retraining because it is shared across groups and accepts feature vectors for new group
identities.

## 9. Training Integration

Load one grouping override snapshot at the start of the grouping phases. Change phase
ordering to:

1. Parse historical logs and rebuild templates.
2. Embed all templates.
3. Run HDBSCAN and build the automatic `template_id -> group_id` mapping.
4. Apply manual override resolution to that mapping.
5. Build a complete group registry snapshot from the effective mapping.
6. Compute a complete centroid snapshot.
7. Match documentation.
8. Group historical events using the effective mapping.
9. Generate features and train the global model.
10. Persist artifacts and grouping status.

Anchor assignments follow the group containing the anchor after each retraining, even
if its automatic `Gnnnn` ID changes. Manual groups retain their stable
`G_MANUAL_*` IDs. An unresolved anchor leaves the source in its automatic group for that
run and is recorded in grouping status.

Use `GroupRegistry.replace_all()` during training so old groups and centroids cannot
survive a retraining snapshot. Keep the operational constraint that training and
realtime must not write the same state directory concurrently.

## 10. Documentation Behavior

Membership changes alter the existing documentation group fingerprint:

- Automatic documentation matching reruns against the new centroid.
- A manual documentation override with an old membership fingerprint becomes
  `stale_override` and is not force-applied.
- Documentation overrides for deleted groups are treated as orphaned: ignore them in
  usage counts and do not let them block document deletion.
- Documentation refresh failure must not roll back an already-applied grouping change.

## 11. UI Changes

Update the Template Details modal in `logai/web/static/index.html`:

- Add an `Assign group` command for both known and unknown templates.
- Open a dedicated modal with a segmented existing/new mode.
- Existing mode contains a searchable group select and excludes the current group.
- New mode shows the server-generated identity after submission; it does not request a
  custom group name.
- Submit the currently loaded grouping revision.
- Show pending, applied, conflict, and failed states.
- On HTTP 409, reload grouping state and require the user to submit again.
- Poll existing read APIs until the submitted revision is applied or failed, then
  refresh template and group views.

Use the selected template text as the representative text for a newly created group.

## 12. Throughput Requirements

The existing observed throughput is approximately 5,000 logs/second with ES and
prediction batch sizes of 2,000.

Steady state:

- Perform only two cached `stat()` checks per poll iteration.
- Do not parse override JSON unless `st_mtime_ns` changes.
- Do not add work to `_process_one()` or the known-template `_assign_group()` branch.
- Do not recompute centroids without a new grouping revision.
- Maintain at least 4,900 logs/second in the existing load benchmark.

During a manual operation:

- Recompute only affected source/target centroids in realtime.
- Use cached embeddings when present.
- Perform one template-registry flush and one group/centroid flush.
- Keep documentation rematching off the ingestion thread.
- Accept a short one-time pause at the safe batch boundary, then resume normal
  throughput and drain any accumulated backlog.

## 13. Failure and Recovery

- A crash before registry flush leaves the override revision pending; restart retries it.
- A crash after registry flush but before status write replays the idempotent assignment
  and repairs status on restart.
- Always evaluate the current effective mapping before mutating so replay does not
  double-count metadata.
- Invalid or corrupt override input records a failed attempt without stopping realtime
  ingestion.
- A missing embedding is generated once; an embedding failure marks that template
  unresolved and leaves its old mapping intact.
- Never advance the ES checkpoint solely because a grouping command was applied.
- Never mark a grouping revision applied until all registry and centroid writes finish.

## 14. Affected Files

New files:

- `logai/storage/grouping.py`
- `logai/grouping/__init__.py`
- `logai/grouping/assignment_manager.py`
- `tests/test_grouping_store.py`
- `tests/test_group_assignment_manager.py`
- `tests/test_web_grouping_api.py`
- `tests/test_realtime_grouping_refresh.py`

Modified core files:

- `logai/config.py`
- `config.yaml`
- `logai/storage/registries.py`
- `logai/realtime/realtime_pipeline.py`
- `logai/training/train_pipeline.py`
- `logai/features/feature_engine.py`
- `logai/alert/alert_state_machine.py`
- `logai/docmatch/refresh_worker.py`

Modified web and deployment files:

- `logai/web/app.py`
- `logai/web/static/index.html`
- `logai/storage/documentation.py`
- `docker-compose.yml`
- Kubernetes deployment manifests where the web volume is currently read-only
- `README.md`
- `ARCHITECTURE.md`
- `docs/WEB_UI.md`

## 15. Test and Acceptance Matrix

### Storage and API

- Empty-store initialization and deterministic revision.
- Atomic mutation and reload.
- Two clients using the same revision: first succeeds, second receives 409.
- Unknown template assigned to an existing automatic group.
- Known template moved from A to B.
- Template assigned to a new manual group.
- Reassigning an already manually assigned template replaces its target.
- Missing source/target, pending target, malformed request, same-group no-op, and cycle.

### Centroids and registries

- Moving T changes both nonempty source and target centroids.
- Resulting centroids have unit L2 norm.
- Source/target membership and aggregate metadata are correct.
- Moving the last member deletes the source group and centroid.
- A missing cached embedding is generated exactly once.
- Registry persistence occurs once per operation, not once per member.

### Realtime boundary

- A vector buffered before the assignment is predicted under A.
- The first event after the applied revision is predicted under B.
- No post-boundary event from T updates A.
- A revision arriving during ES polling applies before the fetched batch is processed.
- A revision arriving during batch processing applies before the next batch.
- Updated centroids are immediately used for subsequent unknown-template matching.
- Restart before and after status persistence converges to the same state.

### Windows and alerts

- Nonempty A retains its history and naturally ages out T's old events.
- B retains its baseline and receives only new T events.
- Empty A has all feature and alert cells removed.
- The global model scores B without group-specific retraining.

### Training and documentation

- Overrides are applied before historical event grouping and feature generation.
- Anchor intent survives changed automatic group IDs across retraining.
- Manual group IDs remain stable.
- Missing anchors are reported and fall back to automatic grouping.
- Documentation automatic matches refresh after centroid changes.
- Membership changes suspend stale manual documentation overrides.

### Performance

- Representative sustained-load test remains at or above 4,900 logs/second.
- Profiling confirms no override JSON parsing on unchanged revisions.
- Profiling confirms no new per-event embedding or centroid work.
- A manual assignment causes one bounded pause and processing returns to the prior rate.

## 16. Implementation Order

1. Add configuration fields and the grouping store with unit tests.
2. Add deferred centroid mutation and complete-snapshot registry APIs.
3. Implement the resolver and assignment manager with pure mapping tests.
4. Integrate overrides into training and verify retraining persistence.
5. Integrate batch-boundary refresh into realtime.
6. Add deleted-group cleanup for feature and alert state.
7. Add documentation invalidation behavior.
8. Add the web API and optimistic conflict handling.
9. Add the assignment modal and synchronization UI.
10. Run targeted tests, the full suite, and the 5,000 logs/second load benchmark.
11. Update architecture, storage ownership, deployment, and operational documentation.

## 17. Explicit Non-Goals and Defaults

- Version one assigns one template per request; no bulk selection.
- No historical event or prediction rewrite.
- No custom group display-name field; representative template text is used.
- Cross-service groups remain allowed.
- No authentication changes are included.
- No database or distributed lock is introduced.
- Grouping activation is tied to safe poll/batch boundaries, not the 5-second
  documentation timer.
