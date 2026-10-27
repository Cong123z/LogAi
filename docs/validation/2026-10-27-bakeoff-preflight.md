# Bake-off preflight (task 0), 2026-10-27

Facts for the briefs of tasks 1–17 (plan `docs/plans/2026-10-27-bakeoff-laptop.md`, spec
`docs/specs/2026-10-27-bakeoff-laptop.md`). Checked on the laptop at 15:00 local.

## Laptop and containers

| Item | Value |
|---|---|
| Machine | 16 cores, 12.9 GiB RAM, 143 GB free disk |
| Docker | Docker Desktop (qemu VM, ~7.75 GB limit). The VM keeps memory it once used: stopping a container does not return RAM to the host; restarting Docker Desktop does |
| Containers (compose project `logai2`) | `logai2-postgres-1` (pgvector pg16, 127.0.0.1:5433 → 5432; live DB `logai` and test DB `logai_test`), `logai2-engine`, `logai2-web` (5565), `logai2-linker` |
| Other containers | `elasticsearch` (9.4.3, ~1.2 GB), `recharge_log_replayer` (:8001, ~80 MB), `bge-m3` (TEI cpu-1.9.4, ~5.3 GB) |
| Memory at rest (before the bge-m3 stop) | bge-m3 5.3 GB, ES 1.2–1.3 GB, replayer 77 MB, postgres 49 MB, engine 44 MB, web 29 MB, linker 13 MB |
| Restart policy | all `unless-stopped`: a stopped container stays stopped after a Docker restart |
| User decision | bge-m3 and linker stopped for the bake-off (linking is not needed) |

Container modes per step: spec §2.

## Live database (laptop)

| Item | Value |
|---|---|
| Migrations | 009 |
| Services | 1: `recharge` |
| Templates | 180 |
| Days with data | 2026-10-26, 2026-10-27 (872 observed minutes in total at check time) |

## Replayer API (used by `scripts/incident_test.py`, never changed by us)

| Call | Body / reply | Used for |
|---|---|---|
| `GET :8001/status` | `{"replay": {"is_running": bool, ...}}` | preflight: the replay must run |
| `POST :8001/replay/inject` | `{"label", "pattern", "per_minute", "minutes"}` → 200 | one inject per target per phase |
| `POST :8001/replay/inject/stop` | – | end of a run |

Replay source: `full_node97.log`, one day 00:00–16:07 local, service `recharge`, started with `POST :8001/replay/start`
(speed 1×).

## `incident_test.py` run directory (input of task 13)

Default location `reports/incident-YYYYMMDD-HHMM/` (one old run: `reports/incident-20261020-2351`).

| File | Content |
|---|---|
| `meta.json` | `started_at` (epoch s), `quick`, `targets`: label → `{service, template_id, text, doc}` (resolved **at run time** against `/api/templates`), `scenario`: the phases with minutes scaled |
| `phases.jsonl` | `{"event": "phase", "name", "kind" (warmup/control/burst/spread/recovery), "start", "end" (epoch s), "targets": [labels]}` and `{"event": "inject", "ts", "status", "label": "<phase>-<target label>", "pattern", "per_minute"}` |
| `samples.jsonl` | every 5 s: `ts`, `phase`, `alerts` (from `/api/alerts`), engine metrics |
| `report.md` | the human report |

Consequences for task 13:
- An incident window = a `phase` event of kind `burst` or `spread`: `[start, end)`, targets = its labels.
- The template id comes from `meta.targets[label].template_id`. It is only valid for the DB that existed at run time
  (the 2026-10-20 run predates the DB rebuild): resolve again by **exact text** against the current `templates`, then
  by the inject `pattern` regex; ambiguous or missing → `key` NULL and a warning.
- `per_minute` is the size. Inject events with `status` ≠ 200 are skipped and reported.

## Spec/plan facts confirmed in code

- `Owner.flush` writes everything in one transaction; pending dicts are cleared only after it (`_clear()`).
- `hourly_counts` upserts add `sum`/`sumsq`, so a recorder upsert that adds arrays matches (D1).
- `OpenMinutes.close_before` zero-fills `*total`, `*error`, `*warn`, `*info` per listed service and every template seen
  earlier in the hour; `doc:` keys exist only inside `Detector.run` (D3).
- `db.PARTITIONED` drives both `ensure_partitions` and `drop_partitions_before`; the engine calls
  `ensure_partitions` at start, daily and before a re-read.
- `tests/test_stress_pipeline.py` imports `scripts.stress_pipeline`: `scripts/lab/` is importable as `scripts.lab`.
- The app venv has no numpy.

## Decisions

D1–D8 of the plan approved by the user on 2026-10-27 (ledger rulings). Roles: Sonnet implementers; Opus reviewers on
1–12, Opus adversaries on 1, 4, 7; Sonnet reviewers on 13–16; no Gemini.
