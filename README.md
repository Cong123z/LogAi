# logai2

Reads application logs from Elasticsearch, templates them per service with
Drain3, counts every template per minute in Postgres, learns a mean ± σ per
service, key, day type and local hour once a day, and alerts when a minute
leaves that band for 3 minutes in a row. Alerts are shown per document
(runbook) when the alerting templates are linked to one; templates are
linked by hand, by inheriting from a similar linked template, or by matching
a document's ERROR lines (bge-m3 embeddings). It replaces logai-engine's
design (Isolation Forest, global groups, file state): no training job and
no model of its own.

```
Elasticsearch → logai2-engine → Postgres ← logai2-web :5565
                     └──────→ /metrics :9118 (Prometheus)
                                  ↕
                            logai2-linker ──→ bge-m3 :8080 (the stack's embeddings service)
```

Architecture details: [`ARCHITECTURE.md`](ARCHITECTURE.md). Web UI and HTTP
API: [`docs/WEB_UI.md`](docs/WEB_UI.md). Spec:
[`docs/specs/2026-10-20-core-engine-design.md`](docs/specs/2026-10-20-core-engine-design.md);
source design for all sub-projects:
[`docs/design/per-service-baseline.md`](docs/design/per-service-baseline.md).

## How it works

- **Engine** (`scripts/run_engine.py`, one thread): polls each selected index
  → normalizes each line → known-line cache, else that service's Drain3 tree
  → per-minute counts. When every index has passed a minute, the minute
  closes: its counts go to `hourly_counts` / `service_hours`, and each key
  (template, `*total`, `*error`, `doc:<id>`) is judged against this and the
  next hour's band (`mean ± 3σ`, `± 4σ` while young; ERROR ≥ 5 and WARN ≥ 10
  per minute; INFO never alerts upward). States go
  `NORMAL → WARMING → ALERTING → COOLING`. Every 2 s one transaction writes
  everything, cursors included (exactly once). At 02:00 local the daily
  commit rebuilds the baseline from the last 5 weekdays / 4 weekend days.
- **Linker** (`scripts/run_linker.py`): every 30 s embeds new templates and
  documents' ERROR lines (bge-m3), then recomputes the automatic links:
  a template inherits the document of the most similar manually linked
  template of its service and level, else is linked to the document whose
  ERROR line it matches (threshold and margin). Changes enqueue
  `reload_links` for the engine. A document is **ERROR** (the log lines it
  is for, the only part matched) + **Handling** (what to do). Templates still
  unmatched go to the LLM of the active profile (LLM profiles page): it
  confirms a near document, or writes a suggestion (an action runbook, or
  SAFE for a normal-operation line) that waits for approval on the
  Documentation page.
- **Web** (`scripts/run_web.py`, Flask): seven pages (Alerting, Pipeline,
  Templates, Documentation, Data sources, LLM profiles, Baseline). It reads Postgres and
  writes settings, documents, links, review decisions and commands; it never
  talks to Elasticsearch or to the engine.

## Requirements

- Elasticsearch with a sortable `@timestamp` and a unique `_id`; `service`
  (or `service.name`, or `service_code2`), `level` (or `log.level`, or a
  level in the message prefix), `message`, and optionally `moduleCode`:
  ```json
  {"@timestamp": "2026-10-20T10:00:01.123Z", "service": "recharge",
   "level": "ERROR", "message": "DB connection timeout host=10.0.0.1", "moduleCode": "TOPUP"}
  ```
- ES retention on the selected indices longer than `start.days_back`
  (2 days).
- Docker Compose; the external network `aiops-net` (or `LOGAI2_NETWORK`).

## Run with Docker Compose

Compose starts Postgres, the engine, the web and the linker next to a stack
that already runs Elasticsearch and `bge-m3` on `aiops-net` (the recharge
stack, the same network as logai-engine). Without the stack's `bge-m3`, run
`docker compose --profile tei up -d` and set
`LOGAI2_EMBEDDING_URL=http://logai2-tei:80`.

```bash
docker compose up -d --build
docker compose logs -f logai2-engine      # first start, seeded documents, "first catch-up done: detection on"
curl -s localhost:9118/metrics | grep -E 'logai_lag_seconds|log_alert_state'
```

| Service | Container | Host |
|---|---|---|
| Postgres (`pgvector/pgvector:pg16`) | 5432 | `127.0.0.1:5433`, localhost only (default credentials) |
| Engine metrics | 9108 | 9118 |
| Web | 5555 | 5565 (`http://localhost:5565`) |

The documents table is seeded once, while empty, from logai-engine's corpus
in the `logai-data` volume (mounted read-only; `LOGAI2_DOC_SEED_VOLUME`
picks another volume). Postgres data lives in the `logai2-pg` volume.
`docker compose down -v` wipes it and makes the next start a first start.

Seeded logai-engine documents have no ERROR field yet. Split them once
(only documents still exactly as seeded are changed):

```bash
docker compose exec logai2-linker python scripts/split_doc_errors.py docs/plans/2026-10-21-corpus-error-split.yaml
```

(the file is not in the image; copy it in with `docker compose cp` first,
or run the script from a checkout with `LOGAI2_DATABASE_DSN` set).

### First start

1. The engine migrates the schema and records `bootstrap = {since: now − 2
   days, until: now}`. Every selected index is read from `since`
   (`start.days_back`); every document is read, no sampling.
2. During this **catch-up** minutes are counted but nothing alerts, not even
   the floors. Pipeline shows "Detection: Off".
3. When every (non-failing) index is caught up, the engine runs the first
   **commit** (all finished days become baseline days) and turns
   **detection on**. On the recharge host this took 2 min 19 s for 2.83M
   events (about 20k events/s); the lag then sits at 30 s, the ES upper
   bound.
4. Link templates to documents on `/#templates`; the doc keys and alert rows
   follow within a tick.
5. The baseline is **young** (`± 4σ`) until 5 committed weekdays (4 weekend
   days) exist. A weekend with fewer than 2 weekend days borrows the
   weekday profile, upper alerts only. Hours with no committed day alert on
   the floors only.

`start.days_back: 0` starts at now: only floors apply until the next nightly
commit. Restarts continue from the saved cursors (no new bootstrap).

## Configuration

Layers, later overriding earlier: dataclass defaults in `logai2/config.py` →
`config.yaml` (only values that differ) → `LOGAI2_*` env vars → the ES
connection and index selection saved on the **Data sources** page.

| Env var | Sets |
|---|---|
| `LOGAI2_DATABASE_DSN` | Postgres DSN |
| `LOGAI2_ES_HOSTS`, `LOGAI2_ES_USER`, `LOGAI2_ES_PASSWORD`, `LOGAI2_ES_VERIFY_CERTS` | ES connection (until one is saved on the web) |
| `LOGAI2_ES_INDEX` | index or pattern read until a selection is saved |
| `LOGAI2_METRICS_PORT`, `LOGAI2_WEB_PORT` | container ports |
| `LOGAI2_DOC_SEED_PATH` | documentation corpus to seed |
| `LOGAI2_EMBEDDING_URL` | embeddings service (OpenAI `/v1/embeddings`, e.g. TEI); default `http://bge-m3:8080` |

Every `config.yaml` key and default (floors, k, commit time, retention,
flush, Drain3) is listed in [ARCHITECTURE §3](ARCHITECTURE.md#3-configuration).

## Test

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
docker compose up -d postgres
docker compose exec postgres createdb -U logai logai_test   # once
.venv/bin/pytest -q                        # LOGAI2_TEST_DSN overrides the 5433 test DB
```

Each test runs in its own throwaway schema of `logai_test`, so the suite is
safe next to a running deployment (which uses the `logai` database). The
`cfg` fixture reads the repo's `config.yaml` and any `LOGAI2_*` variables in
your shell.

## Incident test

`scripts/incident_test.py` (stdlib only, runs on the host) injects bursts
through the recharge replayer and samples `/api/alerts` and
`log_alert_state` every 5 s. Before `run`:

1. The replay runs at 1x (`curl -X POST localhost:8001/replay/start`).
2. The engine is caught up (lag < 60 s) and its heartbeat is fresh.
3. Each target template exists and is linked on `/#templates`: `lixi` →
   DOC-001, `slowdb` and `sqltimeout` → DOC-003 (targets are found by text
   regex, at the top of the script). `sqltimeout` appears in the node97
   replay about 16 min after a replay starts from the beginning of the file,
   so start the replay, wait for it, then link it.

```bash
python3 scripts/incident_test.py run       # ~3 h; --quick halves every phase
python3 scripts/incident_test.py report reports/incident-YYYYmmdd-HHMM
```

Scenario: 30 min warm-up, 30 min control, then five 10-min injection phases
(×50, a spread over DOC-003's two templates, ×20, a rare error from zero,
×10), each followed by 15 min recovery, alternating DOC-001 and DOC-003.
`--engine`, `--web`, `--replayer` (or `LOGAI2_TEST_ENGINE` / `_WEB` /
`_REPLAYER`) override the endpoints.

The report has the 5 checks of spec section 8: burst template ALERTING within
3 checks, its document row ALERTING, a spread burst fires the doc key, the
expected document is shown, and false alerts per hour in the control phase.
Only a transition into ALERTING inside a phase counts. Afterwards, exclude
the test hours on the **Baseline** page so the bursts stay out of the
baseline.

## Repo layout

```
logai2/
  config.py, timeutil.py, db.py      config layers, local time, Postgres (migrate, partitions, settings)
  migrations/                        001_init.sql (core), 002_doc_links.sql (vectors, ERROR/Handling, link sources)
  reader/                            es_source.py (ES query, hit → Line), normalize.py, cache.py, reader.py (watermark)
  owner/                             miners.py (Drain3 per service), counts.py (open minutes), baseline.py (bands),
                                     detect.py (hysteresis), owner.py (flush transaction)
  jobs/commit.py                     daily commit / recompute
  alerts.py, review.py               alert rows, baseline review suggestions
  commands.py, metrics.py, engine.py the engine loop
  embed.py, links.py, linker.py      embeddings client, layered links (SQL), the linker loop
  llm.py, resolver.py                LLM client (active profile), confirm-or-suggest resolver
  web/app.py, web/static/            Flask API and the six pages
scripts/run_engine.py, run_web.py, run_linker.py, incident_test.py,
        split_doc_errors.py (seeded ERROR/Handling split), link_eval.py (measure link thresholds and the LLM)
tests/                               pytest (Postgres on 5433)
docs/specs/, docs/plans/, docs/design/, docs/WEB_UI.md
ARCHITECTURE.md, config.yaml, docker-compose.yml, Dockerfile
```

## Validation

2026-10-20 smoke test (one partial baseline day, not an acceptance run): [docs/validation/2026-10-20-incident-smoke-test.md](docs/validation/2026-10-20-incident-smoke-test.md).
