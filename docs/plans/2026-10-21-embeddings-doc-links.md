# logai2 Embeddings, Document Links and LLM Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: **FINAL, all decisions confirmed by the user on 2026-10-21**;
waiting for the go to implement. Covers sub-projects 2/3 (embeddings, links)
and absorbs sub-project 8 (AI drafts) as Phase B.

**Goal:** a corpus document has two parts: **ERROR** (the log line(s) it is
for) and **Handling** (what to do). Every template is matched against the
**ERROR field only**. A template gets its document in this order:

1. a person's manual link;
2. inherited from the nearest manually linked template of the same service
   and level class;
3. **automatic corpus link** when its similarity to a document's ERROR field
   passes the threshold;
4. otherwise the **LLM** either **confirms** one of the near candidates, or,
   when none fits, **writes a suggested document** (ERROR + Handling) for the
   template. This applies to every level. A line that shows normal operation
   gets a **SAFE** document ("normal operation, no action").

The operator later confirms or rejects every automatic result.

**Phases:** Phase A (Tasks 1–9) builds steps 1–3 with no LLM and is useful
on its own. Phase B (Tasks 10–13) adds step 4.

**Spec:** [`docs/design/per-service-baseline.md`](../design/per-service-baseline.md)
section 3 (option D and the 2026-10-19 test), section 4a (AI drafts), section
6 (schema). Both sections are synced to this plan once it is confirmed.

---

## How it works (what the user confirms)

### The corpus document

| Field | Content | Embedded |
|---|---|---|
| `title`, `error_code` | as today | no |
| **`error`** | the log line(s) this document is for, one per text line ("paste the line this runbook fixes") | **yes**: each line through the reader's `normalize()` (same `<*>` shape as a template), one vector per line |
| **`handling`** | what to do (today's `body`, renamed) | no |
| `status` | `active` \| `draft` \| `rejected` | — |
| `verdict` | `action` (something to do) \| `safe` (normal operation, no action) | — |
| `source` | `manual` \| `llm` (who wrote it) | — |

A document with an empty ERROR field is never auto-matched (the incident
runbooks DOC-016/017/018 have no log line: they stay manual + inherited).
Only `active` documents are matched.

**Why ERROR only.** The 2026-10-19 test (same model, bge-m3) embedded whole
documents: median best score 0.598, right for 7 of 76 templates. The seeded
corpus shows why: 9 of 12 documents start with a log phrase and then explain
it in prose, partly in Vietnamese (`DOC-001`: "LixiHandler entered Receiving
subscriber incorrect. The subscriber typed an invalid receiving number…").
Comparing a template with the ERROR field only compares log text with log
text, which is what inheritance does (template ↔ template: 0 wrong at 0.85
in the test).

### The layers

| # | Link `source` | Rule | Drives alerts | Operator | Phase |
|---|---|---|---|---|---|
| 0 | `manual` | set by a person; `doc_id NULL` = "no document" (sticky) | yes | — | v1 |
| 1 | `inherited` | nearest **manual** template, same service and level class, cosine ≥ `inherit_similarity[class]` | yes | Confirm / Reject | A |
| 2 | `corpus` | best **active** document by template ↔ ERROR-line cosine ≥ `corpus_similarity[class]` **and** ≥ `corpus_margin` above every other document | yes, badged "auto 0.87" | Confirm / Reject | A |
| 3 | `llm` | below layer 2: the LLM picks one of the top 3 candidate documents (ERROR + Handling) for the template | per D10 | Confirm / Reject | B |
| 4 | `draft` | the LLM finds no fitting candidate and writes a new document (`status draft`, verdict `action` or `safe`) | **no**, shown as "AI suggestion" | Approve / Edit / Reject | B |
| — | `rejected` | the operator rejected a layer 3 or 4 result; the template is not sent to the LLM again | no | can link by hand | B |

Rules:

- A lower number always wins. The linker recomputes layers 1–2 every cycle,
  so a new manual link, a new or edited document, or a changed template
  moves a template to the right layer by itself, replacing an `llm` or
  `draft` link (an unused draft is then marked `rejected`).
- Only **manual** links are inheritance sources. Layers 2–4 never spread to
  siblings (no drift).
- **Confirm** turns any automatic link into `manual`. **Reject** stores the
  sticky manual "no document" for layers 1–2, and `rejected` for layers 3–4.
- **Approving a draft** sets the document `active` and the link `manual`.
  Its ERROR line then becomes a match target, so the same error in other
  services links at layer 2 without another LLM call.
- **Detection uses** `manual`, `inherited`, `corpus` and (per D10) `llm`
  links with a document; never `draft`, `rejected` or NULL.

**Level classes:** ERROR = {ERROR, FATAL, CRITICAL}, WARN = {WARN, WARNING},
INFO = everything else.

### Other behavior

- **Apply to similar:** when a person links template T, the web lists the
  same service's same-class templates with cosine ≥ `suggest_similarity`
  (0.80) to T, pre-ticked at or above the inherit threshold. Confirming
  makes them manual (one request, one `reload_links`).
- **Clustering:** in `/api/alerts`, undocumented alerting templates of one
  service are joined when cosine ≥ `cluster_similarity` (0.85), single link,
  into one `kind: "cluster"` row. Prometheus is unchanged.
- **Embedded:** templates (display text; the vector is cleared when the
  engine changes the text) and active-or-draft documents' ERROR lines
  (rebuilt when `revision` changes). Title and Handling are never embedded.

---

## Decisions (confirmed 2026-10-21 unless marked)

| # | Decision |
|---|---|
| D1 | **bge-m3**, called through the OpenAI `/v1/embeddings` format at the stack's existing `http://bge-m3:8080` (only called, never changed); optional own `logai2-tei` compose profile |
| D2 | Inherit thresholds **ERROR 0.85, WARN 0.88, INFO 0.92**, kept as config and calibrated later |
| D3 | Corpus auto-link on the ERROR field only, **0.85 / 0.88 / 0.92**, margin **0.05**; calibrated later (Task 9 measures) |
| D4 | Clustering **0.85**, single link; calibrated later |
| D5 | Inherit only from manual links |
| D6 | One process, **`logai2-linker`** (embed, links, LLM resolver); never the engine thread |
| D7 | Reject / unlink of layers 0–2 = sticky manual "no document" |
| D8 | The 12 seeded documents are split by the tech lead: [`2026-10-21-corpus-error-split.yaml`](2026-10-21-corpus-error-split.yaml), applied by `scripts/split_doc_errors.py` in Task 9 |
| D9 | `body` renamed to **`handling`** |
| D10 | An LLM-confirmed link (layer 3) **drives alerts** before the operator confirms it, badged "AI-confirmed" |
| D11 | **Every level** goes to the LLM. Normal-operation lines get a **SAFE** document (`verdict safe`) |
| D12 | LLM **as in logai-engine**: `llm_profiles` (name, OpenAI-compatible base URL, model, API key, timeout, active), managed on a web page, one active profile. The table and page move here from sub-project 6 |
| D13 | **LLM input = template + corpus only**: service, level and template text (already masked by `normalize()`: numbers and ids are `<*>`), plus the candidate documents' ERROR + Handling. **No raw log lines** for now, so no customer data (msisdn, ids) leaves the network through the LLM proxy. More evidence (raw lines, rates) can be added later behind masking |

## Global Constraints

- Branch `embeddings-doc-links` off `master`; Phase B on the same branch
  after Phase A is reviewed, or on `llm-resolver`. Migration: the next free
  number. Never add `Co-Authored-By` or AI attribution.
- Detection logic does not change. The engine changes only in how links are
  loaded (which sources) and in clearing a stale template vector.
- Only `logai2-linker` calls the embedding and LLM services. The web reads
  Postgres only.
- The LLM never receives raw log lines (D13): only template and corpus
  fields.
- Linker, embedding service or LLM down: no new vectors or resolutions,
  existing links stay, and the Pipeline page shows `worker_status['linker']`
  (stale after 90 s) with pending counts.
- Exact vector search, no index.
- Tests use fake HTTP servers (`http.server` in a thread) for embeddings and
  the LLM; never real models.

## Edge cases (each has a test)

1. Template text changes after it was embedded → vector cleared,
   re-embedded, links recomputed.
2. Manual link removed → its inherited siblings lose their link next cycle.
3. Equal-distance ties → highest similarity, then lowest `template_id` /
   `doc_id`.
4. An automatic link later linked by hand → manual wins, never overwritten.
5. Level change (INFO, later ERROR) → class changes, links recomputed.
6. A linked document is deleted → blocked by the foreign key (v1).
7. The embedding or LLM service errors, or returns a wrong dimension or
   invalid JSON → that item is skipped and retried next cycle within the
   rate limit; the others continue.
8. A template qualifies for layers 1 and 2 → layer 1 wins.
9. Two documents within the margin → no corpus link; it goes to the
   resolver. Two ERROR lines of the same document never compete.
10. A document's ERROR field is edited → its vectors are rebuilt and links
    recomputed; a Handling-only edit embeds nothing.
11. Empty ERROR field, or a line that normalizes to empty → never matched;
    the web shows a warning.
12. A new document now matches a template that has an `llm` or `draft` link
    → layer 2 replaces it; the unused draft becomes `rejected`.
13. The LLM names a `doc_id` that was not among its candidates → treated as
    "none".
14. Rejected layer 3 or 4 → never resent to the LLM; a manual link still
    works.
15. The LLM reply wraps JSON in `<think>…</think>` or prose (seen with
    deepseek in logai-engine) → the client strips it and parses the first
    JSON object; otherwise invalid.
16. An INFO template judged `safe` → a SAFE draft. Once approved and linked,
    it still alerts on a spike like any other link: SAFE describes the line,
    not its volume.

---

## File map

```
logai2/migrations/00N_doc_links.sql   new (Phase A + B columns in one migration)
logai2/config.py                      EmbeddingConfig, LlmConfig, DocumentationConfig thresholds; env vars
logai2/embed.py                       new: /v1/embeddings client
logai2/links.py                       new: level_class, desired_inherited, desired_corpus, apply_links, similar, candidates, cluster_edges
logai2/linker.py                      new: Linker.cycle() (embed, links, resolver, status)
logai2/llm.py                         new (B): chat client on the active llm_profiles row, JSON-only replies
logai2/resolver.py                    new (B): eligibility, confirm prompt, draft prompt
scripts/run_linker.py, scripts/link_eval.py
logai2/owner/owner.py                 upsert clears vector; _load_links filters sources
logai2/engine.py                      seed: text → handling, optional error
logai2/alerts.py                      clustering edges; ignore non-alerting sources
logai2/web/app.py + static views      fields, badges, confirm/reject, similar, drafts queue
docker-compose.yml, README.md, ARCHITECTURE.md, docs/WEB_UI.md, design sections 3/4a/6
tests/...
```

---

## Phase A: embeddings and automatic links (no LLM)

### Task 1: Schema and config

```sql
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE templates ADD COLUMN embedding vector(1024);
ALTER TABLE documents RENAME COLUMN body TO handling;                 -- D9
ALTER TABLE documents ADD COLUMN error text NOT NULL DEFAULT '',
  ADD COLUMN status text NOT NULL DEFAULT 'active',                   -- active | draft | rejected
  ADD COLUMN source text NOT NULL DEFAULT 'manual',                   -- manual | llm
  ADD COLUMN confidence real, ADD COLUMN embedded_revision text;
CREATE TABLE document_errors (doc_id text NOT NULL REFERENCES documents ON DELETE CASCADE,
  n smallint NOT NULL, text text NOT NULL, embedding vector(1024) NOT NULL, PRIMARY KEY (doc_id, n));
ALTER TABLE template_doc_links
  ALTER COLUMN doc_id DROP NOT NULL,     -- manual "no document"
  ADD COLUMN similarity real, ADD COLUMN inherited_from text,
  ADD COLUMN note text;                  -- LLM reason (B)
ALTER TABLE documents ADD COLUMN verdict text NOT NULL DEFAULT 'action';   -- action | safe (B)
CREATE TABLE llm_profiles (name text PRIMARY KEY, base_url text NOT NULL, model text NOT NULL,
  api_key text NOT NULL DEFAULT '', timeout_seconds int NOT NULL DEFAULT 120,
  active boolean NOT NULL DEFAULT false, revision text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX llm_profiles_one_active ON llm_profiles (active) WHERE active;   -- (B)
```

```yaml
embedding: {url: "http://bge-m3:8080", dims: 1024, batch: 32, cycle_seconds: 30}
documentation:
  inherit_similarity: {ERROR: 0.85, WARN: 0.88, INFO: 0.92}
  suggest_similarity: 0.80
  corpus_similarity: {ERROR: 0.85, WARN: 0.88, INFO: 0.92}
  corpus_margin: 0.05
  cluster_similarity: 0.85
```

- [ ] Tests: migrates a fresh and a v1 database (body text preserved as
  handling); NULL `doc_id` accepted; partial threshold dicts merge.

### Task 2: Engine touch points

- Template upsert: `embedding = CASE WHEN templates.text IS DISTINCT FROM
  EXCLUDED.text THEN NULL ELSE templates.embedding END`.
- `_load_links`: `doc_id IS NOT NULL AND source IN ('manual', 'inherited',
  'corpus', 'llm')` (`'llm'` per D10). The web's `/api/alerts` link query
  uses the same filter.
- Seed: `text` → `handling`, optional `error`.
- [ ] Tests: edge case 1; draft, rejected and NULL links create no `doc:`
  key.

### Task 3: Embeddings client (`embed.py`)

POST `{url}/v1/embeddings` `{"input": [...], "model": "bge-m3"}`; vectors in
`index` order, normalized to unit length; 30 s timeout; raises on an HTTP
error or a wrong dimension.

- [ ] Tests against the fake server.

### Task 4: Links SQL (`links.py`), the core

- `desired_inherited`: per template with a vector and no manual row, the
  nearest manual template (non-NULL doc) of the same service and class, kept
  if ≥ the class threshold:

```sql
SELECT t.service, t.template_id, m.doc_id, m.sim, m.template_id AS inherited_from
FROM templates t CROSS JOIN LATERAL (
  SELECT l.doc_id, s.template_id, 1 - (s.embedding <=> t.embedding) AS sim
  FROM template_doc_links l JOIN templates s USING (service, template_id)
  WHERE l.service = t.service AND l.source = 'manual' AND l.doc_id IS NOT NULL
    AND s.embedding IS NOT NULL AND <class(s.level) = class(t.level)>
  ORDER BY s.embedding <=> t.embedding, s.template_id LIMIT 1) m
WHERE t.embedding IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM template_doc_links x WHERE x.service = t.service
                  AND x.template_id = t.template_id AND x.source IN ('manual', 'rejected'))
  AND m.sim >= <threshold(class(t.level))>
```

- `candidates(conn, template, k=3)`: active documents ranked by their best
  ERROR line (`max(1 - (e.embedding <=> t.embedding))` grouped by `doc_id`).
- `desired_corpus`: for templates without a manual, rejected or layer 1
  result, keep top-1 if ≥ `corpus_similarity[class]` and top1 − top2 ≥
  `corpus_margin`. (Built without a `corpus_mode` switch: a
  `corpus_similarity` above 1 turns layer 2 off.)
- `apply_links(conn, cfg) -> bool`: desired = layer 1 ∪ layer 2 (layer 1
  wins). Diff against the current `inherited` / `corpus` rows, and replace
  `llm` / `draft` rows that now have a layer 1–2 result (edge case 12).
  Never touches `manual` or `rejected`. Returns whether anything changed.
- `similar(...)`, `cluster_edges(...)` for the web.
- [ ] Tests: thresholds per class; never across classes or services; never
  from non-manual links; edge cases 2–5, 8–12; idempotent second run;
  leave-one-out fixture with 0 wrong.

### Task 5: Linker process (`linker.py`, `scripts/run_linker.py`)

`cycle()`, each step its own transaction:

1. Templates with no vector (ALERTING first, then newest) → embed →
   `UPDATE … WHERE text = %s` (guards a text change in between).
2. Documents with `embedded_revision IS DISTINCT FROM revision` → split
   `error` into lines → `normalize()` → embed → replace `document_errors` →
   stamp the revision (guarded `AND revision = %s`).
3. `apply_links`; if changed, `enqueue("reload_links")` in the same
   transaction.
4. (Phase B) `resolver.run()`.
5. `worker_status['linker']`: heartbeat, pending counts, last error.

- [ ] Tests (fake server): new template → vector → inherited or corpus link
  and a `reload_links` command; a Handling-only edit sends nothing; service
  down → status error only.

### Task 6: Alert clustering (`alerts.py`)

`alert_rows(..., edges=())`: union-find over undocumented template rows of
one service → `kind: "cluster"`. Metrics pass no edges.

- [ ] Tests: joined by an edge, not across services or into document rows.

### Task 7: Web API

- Documents: fields `error`, `handling`, `status`, `source`; GET adds the
  normalized `error_lines` and whether the vectors are current.
- Templates and alert template lines: `link_source`, `similarity`,
  `inherited_from`, `note`; filter `link_source=…` for review lists.
- `POST /api/templates/<svc>/<id>/link/confirm` → manual (same doc).
  Reject = `PUT …/link {"doc_id": null}` (layers 1–2); layers 3–4 are in
  Task 12.
- `GET …/similar`; `PUT …/link` with `also: [...]` (same service only).
- [ ] Tests: confirm/reject per source; `also` across services → 400;
  sticky NULL survives `apply_links`.

### Task 8: Web views (Gemini-suited)

- Documentation editor: **Title, Error code, ERROR** (log lines, with the
  normalized form shown and an "empty: never auto-matched" note),
  **Handling**.
- Templates page and alert cards: badges `manual` / `inherited 0.91 from
  T00042` / `auto 0.87`, Confirm / Reject; apply-to-similar dialog; cluster
  card.
- Pipeline page: linker status row.

### Task 9: Compose, docs, Phase A validation

- Compose: `logai2-linker` on `aiops-net`; `logai2-tei` under a `tei`
  profile.
- `scripts/split_doc_errors.py`: applies
  [`2026-10-21-corpus-error-split.yaml`](2026-10-21-corpus-error-split.yaml)
  to documents whose `handling` still equals the original seeded text (new
  revision, so the linker embeds them).
- README, ARCHITECTURE, WEB_UI, design sections 3 and 6.
- `scripts/link_eval.py` (read-only) → `docs/validation/<date>-doc-links.md`.
  Ground truth: the deploy's manual links plus logai-engine's overrides and
  incident targets, re-entered by hand; the 12 ERROR fields filled (D8).
  Report:
  - layer 1: leave-one-out right / wrong / none per threshold;
  - layer 2: for thresholds 0.70…0.92 × margins 0 / 0.05 / 0.10: links,
    right, wrong (precision), coverage;
  - the same table for whole-document embedding (title + body), to confirm
    ERROR-only is better.

  Set thresholds to the lowest values with **0 wrong**. If none gives a
  right link, set `corpus_similarity` above 1 (layer 2 off) and say so.

---

## Phase B: LLM confirm or suggest (absorbs sub-project 8)

### Task 10: LLM client (`llm.py`)

Uses the **active `llm_profiles` row** (D12), re-read every cycle so a
profile switch on the web applies without a restart. OpenAI-compatible
`{base_url}/chat/completions` with `response_format: json_object`. Lessons
from logai-engine's runs:
- deepseek answered `<think>…` plus prose without JSON mode → always send
  JSON mode and strip think blocks (edge case 15);
- the proxy timed out at 60 s → the profile timeout defaults to 120 s, with
  one retry on timeout only;
- latency was 6–60 s and doubled with 3 concurrent calls → one call at a
  time.

Prompt size ≤ `llm.max_input_chars` (8000; candidate Handling texts are cut
first). The API key is never logged and never returned by the API.

- [ ] Tests (fake server): JSON round trip; think-block stripping; invalid
  JSON and HTTP errors raise; timeout retried once; no active profile →
  resolver idles with a status warning.

### Task 11: Resolver (`resolver.py`)

Per cycle, eligible templates (D11: **every level**; no link of any
source; ALERTING or first seen < 24 h), ordered ALERTING
first, then ERROR > WARN > INFO, within the rate limits (5 per service per hour, `llm.daily_max` 50; counted from
`template_doc_links.linked_at` of `llm` / `draft` rows plus a failure
counter in `worker_status`):

1. **Confirm:** if `candidates(k=3)` has any document ≥
   `llm.candidate_similarity` (0.60), send the template (service, level,
   text) and each candidate's title, ERROR and Handling. Reply `{"doc_id": … | null,
   "reason": …}`. A `doc_id` among the candidates → link `source = 'llm'`,
   `note = reason`, `similarity` = its score.
2. **Suggest:** otherwise (no candidates, or `null`), ask for `{"verdict":
   "action" | "safe", "title", "error_code", "error", "handling",
   "confidence"}`. `safe` = the template shows normal operation (e.g. "Sent
   request charge success"); its handling starts with "SAFE: normal
   operation, no action needed" and says what a spike or drop of it would
   mean. The `error` must be the template's line in plain form. Insert the
   document (`status draft`, `source llm`, `verdict`) and link `source =
   'draft'`, in one transaction.
3. Any change → `reload_links` (only `llm` links affect detection).

Prompts live in `resolver.py` as constants and say: answer only from the
given template and documents; `<*>` marks a masked value.

- [ ] Tests: confirm path, suggest path, candidate not in the list (edge
  case 13), rate limits, a rejected template is skipped (edge case 14),
  invalid JSON → no row and a retry later.

### Task 12: Web for Phase B

- `POST …/link/confirm` also accepts `llm` and `draft` links. For a draft,
  confirming means approve: document `active`, link `manual`.
- `POST …/link/reject` for `llm` / `draft`: link `rejected` (a draft
  document becomes `rejected` too).
- **Documentation → AI suggestions** queue: drafts with their template, rate
  vs normal, ERROR + Handling, confidence; Approve / Edit (opens
  the editor; Save = approve) / Reject; bulk reject.
- Badges: "AI-confirmed: <reason>" (layer 3), "AI suggestion, unverified:
  check commands before running them" (layer 4), and **SAFE** on documents
  with `verdict safe`. The AI suggestions queue can filter SAFE, so many
  normal lines can be bulk-approved.
- **LLM profiles page** (as in logai-engine): list, add, edit, delete,
  activate, "test" (one tiny JSON call); the API key is write-only (never
  returned, kept when a save leaves it empty).

### Task 13: Phase B validation and docs

- `link_eval.py --llm`: on the ground-truth templates *without* a layer 1–2
  link, report LLM confirm right / wrong / none, the action/safe split, and a
  sample of 5 drafts for a person to rate.
- Design section 4a is rewritten to this flow, sub-project 8 is marked
  "merged into embeddings-doc-links Phase B", and ARCHITECTURE and WEB_UI
  are updated.

---

## Review Focus

1. Precedence: manual > inherited > corpus > llm > draft. Only manual links
   are inheritance sources. Nothing automatic overwrites `manual` or
   `rejected`.
2. Draft, rejected and NULL links never reach detection or metrics.
3. `reload_links` is enqueued in the same transaction as each link change.
4. Vector writes are guarded against concurrent text or revision changes.
5. Phase B: rate limits hold under an error storm; the API key never leaves
   the linker; the LLM can only pick a listed candidate.

## Out of scope

The editor AI chat, AI Insights and Incidents (sub-project 6); a vector index; embedding Title, Handling or raw lines; sending raw lines
or rates to the LLM (later, with masking).
Later, if useful: "add this template's line to the document's ERROR field"
when linking by hand.
