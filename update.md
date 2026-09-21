# Template-to-Group Precision Improvements

## Objective

Improve the precision of semantic template grouping. Cross-service groups remain
supported, but avoiding incorrect merges takes priority over reducing singleton
groups. A template that cannot be assigned unambiguously should remain a
singleton during training or pending during realtime processing.

This work is separate from confidence calibration. Membership similarity makes
group quality observable; the changes below improve the groups themselves.

## Current Limitations

### Untyped placeholders

Drain3 currently masks IP addresses, paths, block IDs, ports, status codes, and
other numbers into the same `<*>` token. This removes distinctions that can be
important to semantic grouping.

For example, these retain more meaning than generic wildcards:

```text
Connection refused to <IP>:<PORT>
Block <BLOCK_ID> missing from <PATH>
HTTP request failed with status <STATUS_CODE>
```

### Unvalidated HDBSCAN clusters

HDBSCAN labels are accepted directly. A cluster can form through a chain of
locally similar templates even when some members are not sufficiently similar
to the group as a whole. There is no post-clustering cohesion check or weak
member rejection.

### Centroid-only realtime assignment

A new template joins the nearest group when its cosine similarity exceeds a
single threshold. The pipeline does not account for an almost equally good
second candidate, nor does it validate the assignment against an actual group
member.

### Generic semantic embeddings

The embedding model is useful for candidate generation but can underweight
negation, operation direction, and opposite outcomes such as `success` versus
`failed`. A high cosine score alone is therefore insufficient evidence for
borderline assignments.

## Proposed Changes

### 1. Preserve variable types

Replace generic masks with typed placeholders such as `<IP>`, `<PORT>`,
`<PATH>`, `<STATUS_CODE>`, `<BLOCK_ID>`, and `<NUMBER>`. Define specific rules
before general numeric masking so meaningful structured values are not erased.

Use the same canonical representation for training and realtime embedding.
Changing the masking scheme invalidates existing Drain3 and embedding artifacts,
so rollout must rebuild them together.

### 2. Validate clusters after HDBSCAN

After HDBSCAN produces candidate clusters:

- Compute the normalized centroid and medoid for each cluster.
- Calculate every member's cosine similarity to the centroid and medoid.
- Reject members below the configured cohesion requirement into singleton
  groups.
- Recompute the cluster representation after removals.
- Split or reject any remaining cluster that still violates the minimum
  cohesion requirement.

Use the medoid, the actual member nearest the center, to guard against a
centroid that does not represent any real template.

### 3. Prefer finer density clusters

Expose HDBSCAN cluster-selection configuration and use
`cluster_selection_method="leaf"` as the precision-oriented default. Evaluate
`min_cluster_size` and `min_samples` against labeled examples rather than
selecting them from cluster counts alone.

Noise points remain valid singleton groups; they are not failed results.

### 4. Add narrow incompatibility guards

Reject otherwise similar memberships when strong evidence indicates opposite
semantics. Initial guards should cover:

- Successful or routine outcomes versus explicit failures.
- Contradictory actions such as `started/stopped` and `accepted/rejected`.
- Incompatible severity categories where the message meaning confirms the
  distinction.
- Structured operation names or error/status codes that should remain distinct.

Keep these rules small and auditable. They should validate semantic candidates,
not replace embedding-based grouping with a large hand-written classifier.

### 5. Require realtime assignment separation

Realtime assignment must satisfy both an absolute similarity threshold and a
minimum margin over the second-best candidate:

```text
best_similarity >= minimum_similarity
best_similarity - second_best_similarity >= minimum_margin
```

If either condition fails, retain the template as pending. Persist the best and
second-best similarities in diagnostic logs so thresholds can be evaluated.

### 6. Validate realtime assignments against group members

Do not accept a new template using centroid similarity alone. Require agreement
with the group's medoid, or with a configured proportion of a bounded member
sample, in addition to the centroid threshold. Keep the trained centroid and
medoid fixed until retraining so online assignments are deterministic.

### 7. Rerank borderline candidates

Use the existing sentence embedding model to retrieve a small candidate set.
For scores near the acceptance boundary, optionally apply a semantic pair
reranker or cross-encoder before assigning the group. This stage should remain
optional because it increases latency and model cost.

### 8. Measure grouping quality explicitly

Build a labeled set of template pairs and expected groups covering multiple
services, severities, operations, and contradictory outcomes. Track:

- Pairwise precision as the primary metric.
- Number and rate of false merges.
- Pairwise recall.
- Singleton rate.
- Mean and minimum within-group similarity.
- Realtime pending/rejection rate.
- Realtime ambiguity rate based on the top-two margin.

Threshold and model changes should be accepted only when they improve precision
without exceeding an agreed singleton or pending-rate budget.

## Implementation Order

1. Add the labeled evaluation dataset and baseline metrics.
2. Introduce typed placeholders and rebuild parsing/embedding artifacts.
3. Add post-cluster centroid and medoid cohesion validation.
4. Add the top-two margin and member validation to realtime assignment.
5. Tune HDBSCAN selection and density parameters against the evaluation set.
6. Add narrow incompatibility guards for demonstrated false-merge patterns.
7. Evaluate a reranker only if the cheaper controls leave material ambiguity.

## Relationship to Membership Similarity

Grouping quality and reporting must use explicit, separate concepts:

- Per-template membership similarity is cosine similarity to the final group
  centroid. An original singleton member has a value of `1.0`.
- Group mean and minimum membership similarities summarize cohesion.
- Documentation similarity is unrelated and must not be presented as grouping
  confidence.
- Raw cosine similarity is not a calibrated probability.

Membership metrics expose weak groups and support evaluation, but they do not by
themselves prevent an incorrect merge. The validation and rejection stages above
are what improve grouping precision.

## Acceptance Criteria

- Cross-service semantic groups remain possible.
- Singleton templates receive membership similarity `1.0`.
- Every multi-template group passes configured centroid and medoid cohesion
  checks.
- Ambiguous realtime candidates remain pending instead of being force-assigned.
- Opposite outcomes in the labeled dataset are not merged.
- Pairwise precision improves over the current baseline.
- All grouping decisions remain reproducible from persisted configuration and
  model artifacts.
