# logai2

Log-anomaly engine: Elasticsearch → per-service Drain3 templates → minute
counts in Postgres → mean ± kσ baseline alerts → Flask web console.
Design: `docs/specs/2026-10-20-core-engine-design.md`.

## Run

Next to a stack that already runs Elasticsearch on the external `aiops-net`
network (the recharge stack, same network as logai-engine):

```bash
docker compose up -d --build
docker compose logs -f logai2-engine      # bootstrap, catch-up, "first commit", detection on
curl -s localhost:9118/metrics | grep -E 'logai_lag_seconds|log_alert_state'
```

| | container | host |
|---|---|---|
| Postgres | 5432 | 5433 |
| engine metrics | 9108 | 9118 |
| web | 5555 | 5565 |

Settings: `config.yaml` overrides the defaults in `logai2/config.py`;
`LOGAI2_*` env vars (see `docker-compose.yml`) override both. The documents
table is seeded once, while empty, from logai-engine's corpus in the
`logai-data` volume (mounted read-only; `LOGAI2_DOC_SEED_VOLUME` picks another).

## Test

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
docker compose up -d postgres
.venv/bin/pytest -q                        # LOGAI2_TEST_DSN overrides the 5433 test DB
```

## Incident test

With the replayer (:8001) replaying at 1x and the targets' documents linked
on `/#templates` (targets at the top of `scripts/incident_test.py`):

```bash
python3 scripts/incident_test.py run       # ~3 h; --quick halves every phase
python3 scripts/incident_test.py report reports/incident-YYYYmmdd-HHMM
```

The report has the 5 checks of spec section 8: burst template ALERTING within
3 checks, its document row ALERTING, a spread burst fires the doc key, the
expected document is shown, and false alerts per hour in the control phase.

## Validation

Not run yet. logai-engine's reference (Isolation Forest, run
incident-20261013-0822): 7 false onsets in the 30-min control phase = 14/h,
28.6/h over the whole run.
