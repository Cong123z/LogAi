# LogAI Engine

Pipeline xử lý log: parse log → gom nhóm ngữ nghĩa → đối chiếu documentation
→ phát hiện bất thường → xuất Prometheus metrics.

```
Elasticsearch → LogAI Engine → Prometheus → Grafana
```

Tài liệu chi tiết về pipeline, input/output contracts, module ownership,
reliability và các lựa chọn thiết kế: [`ARCHITECTURE.md`](ARCHITECTURE.md).

## 1. Kiến trúc & vị trí file

```
logai-engine/
├── ARCHITECTURE.md              # kiến trúc, contracts và reliability semantics
├── config.yaml                  # cấu hình mặc định (ngưỡng, window, path...)
├── docker-compose.yml           # ES + LogAI Engine + Prometheus + Grafana
├── Dockerfile
├── prometheus.yml
├── requirements.txt
├── docs/documentation_corpus.yaml   # nguồn "documentation" để match (demo)
├── scripts/
│   ├── run_training.py          # chạy training pipeline (batch, offline)
│   └── run_realtime.py          # chạy realtime pipeline (long-running)
└── logai/
    ├── config.py                # AppConfig, load_config()
    ├── models.py                 # RawLog, ParsedEvent, GroupState, FeatureVector...
    ├── storage/                  # JSON/pickle registries, checkpoint, dedup
    ├── parsing/drain3_parser.py  # Drain3 wrapper (3.2 / 4.2)
    ├── embedding/embedder.py     # all-mpnet-base-v2 wrapper (3.4 / 4.4)
    ├── clustering/hdbscan_cluster.py   # HDBSCAN + centroid + nearest-group (3.5/3.6/4.4)
    ├── docmatch/doc_matcher.py   # cosine similarity vs doc corpus (3.7 / 4.5)
    ├── features/feature_engine.py     # sliding-window features (3.8 / 4.6)
    ├── anomaly/isolation_forest_model.py  # Global Isolation Forest (3.9 / 4.7)
    ├── alert/alert_state_machine.py   # hysteresis NORMAL/WARMING/ALERTING/COOLING (4.8)
    ├── metrics/prometheus_exporter.py # /metrics (4.9 / 8)
    ├── collector/es_collector.py      # ES polling + search_after + retry (4.1 / 3.1)
    ├── training/train_pipeline.py     # orchestrator toàn bộ Training Pipeline (mục 3)
    ├── realtime/realtime_pipeline.py  # orchestrator toàn bộ Realtime Pipeline (mục 4)
    └── reliability/                    # retry backoff, DLQ
```

Mapping đầy đủ giữa module, boundary và trách nhiệm được mô tả trong
`ARCHITECTURE.md`.

## 2. App sinh log của bạn cần làm gì?

App test chỉ cần **ghi document vào Elasticsearch** theo input contract dưới
đây:

```json
POST /app-logs-2026.09.07/_doc
{
  "@timestamp": "2026-09-07T10:00:01Z",
  "service": "payment",
  "level": "ERROR",
  "message": "DB connection timeout host=10.0.0.1"
}
```

- Index pattern mặc định trong `config.yaml`: `app-logs-*` (đổi trong
  `elasticsearch.index` nếu app của bạn dùng tên khác).
- `@timestamp` nên là ISO8601 UTC (`Z` suffix) hoặc epoch seconds. Code hiện
  chưa normalize epoch milliseconds.
- Các field khác ngoài `@timestamp/service/level/message` sẽ được giữ lại
  trong `RawLog.metadata`.

LogAI Engine **không** cần biết gì về cách bạn sinh log — nó chỉ poll
Elasticsearch bằng `search_after`, xử lý, và expose `/metrics`.

Các tham số historical training được cấu hình trong section `training` của
`config.yaml`, gồm `max_docs`, `batch_size`, `lookback_seconds` và
`dedup_buffer_size`. Mặc định training lấy tối đa `200000` logs.

## 3. Chạy thử (Docker Compose)

```bash
docker compose up -d elasticsearch
# đợi ES healthy, rồi cho app sinh log của bạn bắn dữ liệu vào app-logs-*

# 1) chạy training 1 lần để build Template/Group Registry + Global Isolation Forest
docker compose run --rm logai-engine python scripts/run_training.py --lookback-hours 24

# 2) chạy realtime engine (long-running), expose /metrics:9108
docker compose up -d logai-engine prometheus grafana
```

Kiểm tra:
```bash
curl http://localhost:9108/metrics | grep log_anomaly_score
```

Prometheus: http://localhost:9090 · Grafana: http://localhost:3000 (admin/admin)

### Template Explorer web UI

The optional Flask Template Explorer reads the same persisted registry files as
the engine (`template_registry.json` and `group_registry.json`). In Docker it
mounts the shared `logai-data` volume at `/app/data`, so it displays the actual
templates produced by training/realtime rather than demo data.

Start it with:

```bash
docker compose up -d logai-engine
```

The realtime engine starts the web UI automatically via `depends_on`. Then open
http://localhost:5555. The UI supports known/unknown status tabs,
service and level filters, free-text search, sortable columns, pagination,
auto-refresh, and a detail view for each template.
The page also includes a Semantic Groups table showing every entry currently
stored in the live `group_registry.json`, including its group ID, service,
representative template, documentation status, and event count. The same data
is available from `GET /api/groups`.

Templates, Semantic Groups, Alerting, and Documentation are separate sidebar
views. The Reload control performs a full browser refresh, while automatic
reconnect keeps retrying the API after a web-service restart, so the Chrome
process does not need to be restarted.

The Alerting sidebar view reads `anomaly_state.json` every two seconds while it
is open. It lists the current `NORMAL`, `WARMING`, `ALERTING`, and `COOLING`
condition for each service/group cell, with status filtering and sortable log
level, anomaly score, and evaluation time columns. Groups do not own a log
level: this column is derived from the highest template level in that group and
is display context only. `AnomalyState.alert_state` is the alert condition.

The Documentation view is writable. Users can add, edit, and delete corpus
entries, then assign one entry to an undocumented semantic group. Manual
assignments override cosine matching until cleared. Changes are persisted in
`documentation_corpus.json` and `documentation_overrides.json` on the shared
data volume and are applied by the realtime engine within the configured
refresh interval (5 seconds by default). The write API has no built-in
authentication; restrict port 5555 with your ingress or network policy.

See [docs/WEB_UI.md](docs/WEB_UI.md) for page behavior, API request/response
contracts, synchronization states, persistence files, and operational limits.

For a local run against a mounted/copied data directory:

```bash
LOGAI_WEB_DATA_DIR=data python scripts/run_web.py
```

### Chạy local không cần Docker

```bash
pip install -r requirements.txt
export LOGAI_ES_HOSTS=http://localhost:9200
python scripts/run_training.py --lookback-hours 24
python scripts/run_realtime.py
```

## 4. Vòng đời training → realtime (đúng theo plan mục 3 & 4)

- **Training** (`run_training.py`, chạy định kỳ — cron/Airflow, KHÔNG chạy
  liên tục): kéo log lịch sử từ ES → Drain3 → Template Registry → embedding
  → HDBSCAN → centroid → doc matcher → feature history → train **1 Global
  Isolation Forest chung cho mọi groups** (trên 8 feature chuẩn hóa dimensionless)
  → lưu tất cả registry + model xuống `data/`.
- **Realtime** (`run_realtime.py`, long-running process): chỉ
  Collect→Parse→Assign→Match→Aggregate→Predict→Export. **Không** cluster
  lại. Sử dụng Global Isolation Forest đã huấn luyện để dự đoán bất thường
  ngay lập tức cho mọi group (bao gồm cả các group mới xuất hiện, loại bỏ
  triệt để cold-start). Template mới quá khác biệt với mọi group hiện có sẽ
  ở trạng thái `Unknown/Pending` (log warning) cho tới lần training tiếp theo
  — đúng hành vi mô tả ở mục 4.4.

Vì vậy: chạy `run_training.py` trước khi bật `run_realtime.py` lần đầu
để tạo Global Isolation Forest (nếu chưa có model, engine vẫn chạy bình
thường và bỏ qua bước predict cho tới khi có model).

## 5. Storage (theo yêu cầu: file-based JSON/pickle, không cần DB engine)

Tất cả state nằm dưới `data/` (mount volume `logai-data` trong Docker):

| File | Nội dung |
|---|---|
| `template_registry.json` / `template_embeddings.pkl` | Template State + embedding (mục 6) |
| `group_registry.json` / `group_centroids.pkl` | Group State + centroid (mục 6) |
| `checkpoint.json` | `search_after` cursor của ES collector (mục 7) |
| `training_checkpoint.json` | cursor riêng cho historical training; xóa sau khi training hoàn tất |
| `training_event_index.jsonl` | event index nhẹ để resume/replay training trước các phase grouping và model |
| `anomaly_state.json` | Alert state machine per `(service, group)` cell (mục 6) |
| `dedup_index.json` | idempotency index theo `event_id` (mục 7) |
| `dlq.jsonl` | events lỗi sau khi retry hết (mục 7) |
| `documentation_corpus.json` | Editable documentation entries; seeded once from `docs/documentation_corpus.yaml` |
| `documentation_overrides.json` | Persistent manual group-to-document assignments |
| `documentation_status.json` | Last corpus/override revisions applied by the engine |
| `models/global_v3.pkl` | Global Isolation Forest v3 với rate-floor features |
| `drain3_state.bin` | state cây Drain3 (persist riêng, thư viện tự quản) |

Ghi file dùng atomic write (`os.replace`) nên an toàn khi crash giữa
chừng. **Giới hạn**: chỉ an toàn với **một tiến trình** ghi (không chạy 2
instance `run_realtime.py` song song trên cùng thư mục `data/`).

## 6. Reliability đã implement (mục 7)

- Retry exponential backoff cho mọi lệnh gọi Elasticsearch
  (`logai/reliability/retry.py`).
- DLQ (`data/dlq.jsonl`) khi 1 event lỗi sau khi retry hết trong pipeline
  realtime — không làm nghẽn toàn bộ batch.
- Realtime checkpoint (`search_after`) + `event_id` dedup index hỗ trợ resume và
  idempotency. Historical training dùng checkpoint và event index riêng; cursor
  chỉ commit sau khi batch đã được ghi bền vững.

## 7. Metrics (mục 8 + 4.9)

```
# Business / log metrics
app_log_events_total{service}
app_log_errors_total{service, error_code}
app_log_templates_total{service}

# Kết quả phân tích
log_anomaly_score{group_id, documented}
log_alert_state{group_id, state}      # 1 cho state hiện tại, 0 cho state khác

# Sức khoẻ engine
logai_events_received_total
logai_events_processed_total
logai_events_failed_total
logai_retry_total
logai_processing_latency_seconds
logai_queue_depth
```

## 8. Những điểm cần bạn tinh chỉnh trước khi coi là "production"

- `docs/documentation_corpus.yaml` hiện chỉ là seed demo. Thay seed trước lần
  khởi tạo đầu tiên, hoặc quản lý runtime corpus sau đó qua Documentation view.
- Các ngưỡng (`clustering.assignment_similarity_threshold`,
  `doc_matcher.similarity_threshold`, `anomaly.*`, `alert.*`) đều đặt giá
  trị mặc định hợp lý nhưng **cần tune lại bằng dữ liệu log thật** của bạn
  — mình đã unit-test logic (state machine, feature engine, clustering,
  Isolation Forest) bằng dữ liệu tổng hợp, không phải log thật của bạn.
- File-based storage phù hợp MVP/single-instance; nếu sau này cần scale
  ngang hoặc nhiều instance cùng ghi, cần chuyển `storage/` sang
  Postgres/Redis (interface `JSONStore`/`PickleStore`/`ModelStore` được
  thiết kế để swap được mà không đổi code gọi).
- Embedding model (`all-mpnet-base-v2`) sẽ tự tải về từ HuggingFace lần
  chạy đầu — cần mạng ra ngoài lần đầu tiên (hoặc pre-cache trong image).
