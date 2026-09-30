# LogAI Engine – Handoff (2026-09-30, branch `calibrate`)

> Đọc file này để tiếp tục công việc ở session/tài khoản khác. Tất cả thay đổi dưới đây
> **CHƯA COMMIT** (10 file, +449/-12). Plan chi tiết gốc:
> `/home/cong/.claude/plans/pasted-content-id-dcca-index-udcntt-vtn-buzzing-dijkstra.md`

## Môi trường
- Luôn chạy test bằng venv của project: `.venv/bin/python3 -m pytest -q` → hiện **246/246 pass**.
  (`python3` hệ thống thiếu drain3/flask/hdbscan → fail giả, đừng dùng.)
- Không có kubectl context trên máy này (không xem được Job trên cluster).
- Dữ liệu log thật để thử: `/home/cong/Downloads/logs_recharge-wq455tmz9ibd5fhm7wspoiwfde/full_node96.log`
  (~600MB, ~1,86 triệu bản ghi/ngày, log NHIỀU DÒNG: ~59% dòng là dòng nối tiếp).
- Manifest deploy thực tế của user: `/home/cong/trainning.yaml` (image `congdock123/logai-engine:v4`).

## Đã làm (uncommitted)
1. **`LOGAI_ES_INDEX` config từ ngoài** – `logai/config.py` đọc env; đã thêm vào `.env.example`,
   `k8s/.env.example`, `k8s/.env` (gitignored), `k8s/training.yaml`, `docker-compose.yml`.
   ⚠️ `/home/cong/trainning.yaml` của user CHƯA có `LOGAI_ES_INDEX`.
2. **Mapping field ES production** – `logai/collector/es_collector.py`:
   - `_extract_service`: `service` / `service.name` → `service_code2` → "unknown"
     (KHÔNG dùng `service_code` – đó là `{host_ip}`). Sửa crash `'dict' object has no attribute 'strip'`.
   - `_extract_level`: `log.level` → `level` → **token thứ 3 của message** (format cố định
     `DATE TIME LEVEL [THREAD] body`) → "INFO".
   - `_NOISE_KEYS`: loại khỏi metadata `ecs, agent, @version, input, logstash_instance, kafka_cluster,
     log, host, service_code, service_code2`; giữ `moduleCode`, `groupModule`.
3. **Realtime không tua lại index** – `logai/storage/checkpoint.py` thêm `start_ts`;
   `poll_batch()` khi chưa có cursor thì query `@timestamp >= start_ts` (thời điểm realtime chạy lần đầu,
   được persist) thay vì `match_all` từ đầu index.
4. **Lớp regex tiền xử lý trước Drain3** – `Drain3Config.masking_rules` + `extra_delimiters=["="]`
   trong `logai/config.py`; `logai/parsing/drain3_parser.py` áp rule `mask_with=""` bằng `re.sub`
   riêng (drain3 lib luôn bọc `<`+mask+`>` nên rỗng vẫn để lại `<>`). Thứ tự rule:
   0 cắt prefix `date time LEVEL [thread]` (case-insensitive) → 1 gộp XML/SOAP thành `<XML>` →
   UUID → 3 dạng timestamp → IP → blk → path → secret(≥16 ký tự có số) → số →
   `k=v` trong danh sách (giá trị nhiều từ, chỉ khi theo sau `, key=` hoặc `}`) → `k=v` 1 token.
   Không dùng `:` làm trigger (từng mask nhầm "Exception" trong `50010:Exception`).
   Kết quả trên 119k bản ghi thật: 219→186 template, singleton 16→8, token TB 125,8→15,8,
   max 23.285→131; template giữ tên key (dạng `key <*>`).
5. Tests mới: `tests/test_es_malformed_hits.py` (service/level/metadata),
   `tests/test_log_masking.py::TestDrain3UniversalMaskingRules`.

## Phát hiện quan trọng (chưa sửa)
- **Training "chạy mãi" trên k8s**: dòng `INFO drain3.template_miner: Saving state of N clusters with
  M messages, B bytes, reason: ...` là drain3 tự serialize + ghi TOÀN BỘ state mỗi khi template
  tạo/đổi (không phải checkpoint commit). State `/app/data/drain3_state.bin` dùng chung với realtime
  và tích luỹ mãi → mỗi lần ghi ngày càng chậm. Đo local (state rỗng): chậm hơn ~36%.
  `messages` = tổng log cộng dồn (kể cả state restore) → trừ số ở dòng `Restored ... from Y messages`
  để biết tiến độ. Cần user gửi `kubectl logs job/logai-training --timestamps | grep "Saving state" | tail -5`.
- **`max_docs=200_000` cắt im lặng**: với node96, bản ghi thứ 200k ở 01:48 → `--lookback-hours 24`
  thực chất chỉ train ~1h48 đầu (query sort cũ→mới). Không có env/CLI để đổi (chỉ `config.yaml`).
  Log tiến độ Phase 1 chỉ in mỗi 100 batch = đúng 200k → gần như không thấy tiến độ.
- Job không có `activeDeadlineSeconds`.
- `ParsedEvent.parameters` gần như luôn rỗng (template gộp token) – chỉ dùng ở `to_dict()`.

## Việc tiếp theo (đang chờ user quyết)
1. **Tóm tắt XML thay vì xoá trắng** (đề xuất cuối cùng): giữ tên phần tử nghiệp vụ chính (bỏ
   Envelope/Body/Header) + thẻ trạng thái phổ biến (`error`, `resultcode`, `status`, `description`...)
   khi giá trị ngắn → `<XML gwOperationResponse error=0 description=success>`; làm bằng `re.sub`
   với hàm trong `Drain3Parser`. Lý do: success/fail hiện ra cùng template.
2. Danh sách key giữ nguyên giá trị enum (`error_code`, `command`…)? – quyết định nghiệp vụ.
3. Sửa hiệu năng drain3: bỏ auto-save per-change; lưu 1 lần cuối Phase 1 (training) và cùng
   `checkpoint.commit()` (realtime) – phải lưu state TRƯỚC/CÙNG lúc tiến cursor. Tối thiểu: set
   logger `drain3` lên WARNING trong `scripts/run_*.py`.
4. Log tiến độ training mỗi ~10 batch (docs/max_docs + @timestamp đã tới) + WARNING khi chạm
   `max_docs`; thêm env `LOGAI_TRAINING_MAX_DOCS` / `--max-docs`.
5. Hợp nhất danh sách level (đang lặp ở `config.py` rule 0 và `es_collector._LEVEL_TOKEN_RE`).
6. Nhỏ: `BCCSUtil: <*>` gộp `Request:<XML>` với dòng `=====http://...`.


## Khi deploy
- Log nhiều dòng: cấu hình multiline ở Filebeat/Logstash, pattern `^\d{2}/\d{2}/\d{4}` (không sửa được trong app).
- Rule masking/format template đã đổi (`key=<*>` → `key <*>`) → **xoá state cũ trên PVC rồi train lại**:
  `rm -f /app/data/drain3_state.bin /app/data/training_checkpoint.json /app/data/training_event_index.jsonl`
  (+ template/group registry, models).
- Job k8s immutable: đổi image cùng tag không chạy lại → `kubectl delete job logai-training` trước
  khi apply; nên dùng tag theo git SHA; cân nhắc `ttlSecondsAfterFinished`.
- `enableServiceLinks: false` nên có ở mọi pod (Service `logai-web`/`logai-metrics` sinh env
  `LOGAI_WEB_PORT`/`LOGAI_METRICS_PORT=tcp://...` trùng tên biến app → `int()` có thể crash).
- Giữ PVC `ReadWriteOnce` (single-writer); RWX mất hàng rào an toàn, PVC accessModes immutable.
