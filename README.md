# LogAI Engine

Reads logs from Elasticsearch, groups them into templates and semantic groups,
matches them against documentation, detects anomalies and exports Prometheus
metrics.

```
Elasticsearch → LogAI Engine → /metrics (Prometheus) → Grafana
                     └──────→ Web UI :5555
```

Architecture details: [`ARCHITECTURE.md`](ARCHITECTURE.md). Web UI/API:
[`docs/WEB_UI.md`](docs/WEB_UI.md). Kubernetes: [`k8s/README.md`](k8s/README.md).

## How it works

- **Training** (`scripts/run_training.py`, run once or on a schedule): reads
  historical logs → Drain3 templates → BGE-M3 embeddings → HDBSCAN groups →
  documentation matching → trains one global Isolation Forest → saves to `data/`.
- **Realtime** (`scripts/run_realtime.py`, long-running): polls new logs →
  assigns them to existing groups → scores anomalies → updates the alert state
  (`NORMAL/WARMING/ALERTING/COOLING`) → exports `/metrics` on port 9108. It does
  not regroup; a new template that is too different stays Unknown until the
  next training.
- **Web UI** (`scripts/run_web.py`, port 5555): browse templates, groups and
  alerts; edit documentation; move templates between groups by hand; AI
  Insights (calls the LLM only when you click).

## Requirements

- Elasticsearch with logs in the index `app-logs-*` (or the indices you pick on
  the **Data sources** page), each document shaped like:
  ```json
  {"@timestamp": "2026-09-07T10:00:01Z", "service": "payment",
   "level": "ERROR", "message": "DB connection timeout host=10.0.0.1"}
  ```
- A `BAAI/bge-m3` embedding service (OpenAI-style API, 1024 dimensions).
- (Optional) An OpenAI-style `/chat/completions` LLM endpoint for AI Insights.

## Run with Docker Compose

Compose does **not** start Elasticsearch/Prometheus; it joins the existing
`aiops-net` network.

```bash
cp .env.example .env          # set ES, embedding, LLM
docker network create aiops-net   # if it does not exist yet

docker compose build
docker compose run --rm logai-training            # train (24 h of logs by default)
docker compose up -d logai-engine                 # realtime + web UI
docker compose --profile ui up -d grafana         # optional
```

Check:

```bash
curl http://localhost:9108/metrics | grep log_anomaly_score
curl http://localhost:5555/api/health
```

The code is copied into the image at build time: after changing it, run
`docker compose build` and recreate the containers.

### Retrain

Default: the **Retrain** page of the Web UI (`#retrain`). Set the time,
weekdays, time zone, lookback and max documents, or click **Retrain now**. The
engine pauses, trains inside its own process and restarts itself; the container
does not need to be stopped. Do not run a `logai-training` job while the engine
is running.

Manual fallback (stop the engine while training):

```bash
docker compose stop logai-engine
docker compose run --rm logai-training
docker compose start logai-engine
```

Existing templates and groups are kept across retrains; new templates are only
added to existing groups or form new ones, recorded in
`data/group_lineage.json` (details: `ARCHITECTURE.md` §6.2). A retrain that
fails or is killed midway rolls back automatically. Logs produced while the
engine is stopped are not lost: it resumes from its checkpoint. Watch it catch
up with `time() - logai_last_processed_event_timestamp_seconds`.

Max documents goes up to 20,000,000. When the lookback holds more, the newest
logs are kept. Memory stays around 0.3 GB whatever the size; time and disk grow
with it (about 1.9M logs in ~3 minutes; ~230 B per log of temporary disk in
`data/`, checked before training starts).

## Run locally

```bash
pip install -r requirements.txt
export LOGAI_ES_HOSTS=http://localhost:9200
export LOGAI_EMBEDDING_ENDPOINT=http://localhost:8080/v1/embeddings
python scripts/run_training.py --lookback-hours 24
python scripts/run_realtime.py
LOGAI_WEB_DATA_DIR=data python scripts/run_web.py
```

Tests: `pip install pytest && pytest tests/`

## Configuration

- `config.yaml`: thresholds, windows, paths. `LOGAI_*` environment variables
  (see `.env.example`) override this file.
- `docs/documentation_corpus.yaml`: sample documentation, only used to seed the
  first start; edit it in the Web UI afterwards.
- The default thresholds need tuning on real logs.

## Data

All state is JSON/pickle files in `data/` (the `logai-data` volume): template
and group registries, ES checkpoints, alert state, DLQ (`dlq.jsonl`),
documentation, and the model `models/global_v3.pkl`.

Run only **one** realtime process per `data/` directory.

## Notes

- The Web UI write API has no authentication: restrict access to port 5555.
- Training and realtime must use the same embedding model.
