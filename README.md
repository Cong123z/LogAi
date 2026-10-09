# LogAI Engine

Đọc log từ Elasticsearch, gom thành template/nhóm ngữ nghĩa, đối chiếu tài
liệu, phát hiện bất thường và xuất Prometheus metrics.

```
Elasticsearch → LogAI Engine → /metrics (Prometheus) → Grafana
                     └──────→ Web UI :5555
```

Chi tiết kiến trúc: [`ARCHITECTURE.md`](ARCHITECTURE.md). Web UI/API:
[`docs/WEB_UI.md`](docs/WEB_UI.md). Kubernetes: [`k8s/README.md`](k8s/README.md).

## Cách hoạt động

- **Training** (`scripts/run_training.py`, chạy một lần hoặc định kỳ): lấy log
  lịch sử → Drain3 template → embedding BGE-M3 → HDBSCAN gom nhóm → khớp tài
  liệu → train một Global Isolation Forest → lưu vào `data/`.
- **Realtime** (`scripts/run_realtime.py`, chạy liên tục): poll log mới → gán
  vào nhóm có sẵn → tính điểm bất thường → cập nhật trạng thái cảnh báo
  (`NORMAL/WARMING/ALERTING/COOLING`) → xuất `/metrics` ở cổng 9108. Không
  gom nhóm lại; template mới quá khác sẽ ở trạng thái Unknown tới lần training
  sau.
- **Web UI** (`scripts/run_web.py`, cổng 5555): xem template, nhóm, cảnh báo;
  sửa tài liệu; gán template vào nhóm bằng tay; AI Insights (gọi LLM khi bấm).

## Yêu cầu

- Elasticsearch có log trong index `app-logs-*`, mỗi document dạng:
  ```json
  {"@timestamp": "2026-09-07T10:00:01Z", "service": "payment",
   "level": "ERROR", "message": "DB connection timeout host=10.0.0.1"}
  ```
- Một dịch vụ embedding `BAAI/bge-m3` (API kiểu OpenAI, 1024 chiều).
- (Tùy chọn) LLM endpoint kiểu OpenAI `/chat/completions` cho AI Insights.

## Chạy bằng Docker Compose

Compose **không** tự chạy Elasticsearch/Prometheus; nó gắn vào network
`aiops-net` có sẵn.

```bash
cp .env.example .env          # sửa ES, embedding, LLM
docker network create aiops-net   # nếu chưa có

docker compose build
docker compose run --rm logai-training            # train (mặc định 24h log)
docker compose up -d logai-engine                 # realtime + web UI
docker compose --profile ui up -d grafana         # tùy chọn
```

Kiểm tra:

```bash
curl http://localhost:9108/metrics | grep log_anomaly_score
curl http://localhost:5555/api/health
```

### Retrain

Cách mặc định: trang **Retrain** trên Web UI (`#retrain`) — đặt giờ, ngày trong
tuần, múi giờ, lookback và max documents, hoặc bấm **Retrain now**. Engine tự
tạm dừng, train trong chính tiến trình của nó rồi tự khởi động lại; không cần
dừng container. Đừng chạy thêm job `logai-training` trong lúc engine đang chạy.

Cách thủ công (dự phòng) — dừng engine trong lúc train:

```bash
docker compose stop logai-engine
docker compose run --rm logai-training
docker compose start logai-engine
```

Template và group cũ được giữ nguyên qua retrain; template mới chỉ được thêm vào
group cũ hoặc tạo group mới, ghi ở `data/group_lineage.json` (chi tiết:
`ARCHITECTURE.md` §6.2). Retrain lỗi hoặc bị tắt giữa chừng thì tự rollback. Log phát sinh
lúc dừng không mất: engine đọc tiếp từ checkpoint. Theo dõi tới khi đuổi kịp:
`time() - logai_last_processed_event_timestamp_seconds`.

## Chạy local

```bash
pip install -r requirements.txt
export LOGAI_ES_HOSTS=http://localhost:9200
export LOGAI_EMBEDDING_ENDPOINT=http://localhost:8080/v1/embeddings
python scripts/run_training.py --lookback-hours 24
python scripts/run_realtime.py
LOGAI_WEB_DATA_DIR=data python scripts/run_web.py
```

Test: `pip install pytest && pytest tests/`

## Cấu hình

- `config.yaml`: ngưỡng, window, đường dẫn. Biến môi trường `LOGAI_*` (xem
  `.env.example`) ghi đè lên file này.
- `docs/documentation_corpus.yaml`: tài liệu mẫu, chỉ dùng để khởi tạo lần
  đầu; sau đó sửa trong Web UI.
- Các ngưỡng mặc định cần tune lại bằng log thật.

## Dữ liệu

Toàn bộ state là file JSON/pickle trong `data/` (volume `logai-data`):
registry template/nhóm, checkpoint ES, trạng thái cảnh báo, DLQ
(`dlq.jsonl`), tài liệu, model `models/global_v3.pkl`.

Chỉ chạy **một** tiến trình realtime trên mỗi thư mục `data/`.

## Lưu ý

- API ghi của Web UI không có xác thực — giới hạn truy cập cổng 5555.
- Training và realtime phải dùng cùng model embedding.
