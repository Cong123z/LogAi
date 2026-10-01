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
4. **Lớp regex tiền xử lý trước Drain3 (bản thử nghiệm/lịch sử, chưa có trong checkout hiện tại)** – `Drain3Config.masking_rules` + `extra_delimiters=["="]`
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
- **Giới hạn `max_docs` cắt im lặng**: run trước trên node96 dùng `200_000`, bản ghi thứ
  200k ở 01:48 nên `--lookback-hours 24` thực chất chỉ train ~1h48 đầu. Checkout hiện tại
  có `training.max_docs: 1_000_000` trong `config.yaml` (dataclass default vẫn là 200.000).
  Không có env/CLI override cho `max_docs`; log tiến độ Phase 1 chỉ in mỗi 100 batch nên khó
  nhận biết đã chạm giới hạn.
- Job không có `activeDeadlineSeconds`.
- `ParsedEvent.parameters` gần như luôn rỗng (template gộp token) – chỉ dùng ở `to_dict()`.

## Cập nhật điều tra training không kết thúc (2026-10-01)

### Phạm vi thời gian historical là hữu hạn

`run_training_from_elasticsearch()` chụp `end_ts = time.time()` đúng một lần khi
Job bắt đầu, sau đó đặt `start_ts = end_ts - lookback`. Collector dùng range
`@timestamp >= start_ts` và `@timestamp <= end_ts`, sort tăng dần theo
`@timestamp`/`_doc`, rồi dừng khi hết hit hoặc đạt `max_docs`. Job historical
không dùng `run_forever()` và không tiếp tục nhận log mới phát sinh sau `end_ts`.

Vì vậy việc Phase 1 chạy lâu không phải do lookback tự trượt theo log mới. Nếu
Pod restart, process mới sẽ chụp một `end_ts` mới và đó là một lần training mới.

### Nguyên nhân Phase 1 có thể kéo dài hàng giờ

Log dạng:

```text
INFO drain3.template_miner: Saving state of N clusters with M messages, B bytes, reason: ...
```

là log nội bộ của Drain3 khi `TemplateMiner.add_log_message()` thấy cluster/template
thay đổi. Drain3 serialize và ghi **toàn bộ** cây state vào
`/app/data/drain3_state.bin`, không chỉ cluster vừa thay đổi. Hiện tại việc này
xảy ra theo từng thay đổi template, nên với state khoảng `140000 clusters` chi phí
mỗi lần ghi rất lớn và có thể xuất hiện liên tục trong Phase 1.

- `clusters`: số template cluster hiện đang tồn tại trong Drain3.
- `messages`: tổng số message được giữ trong toàn bộ Drain3 state, không chỉ run
  hiện tại; có thể bao gồm state được restore từ PVC.
- `bytes`: kích thước state đã serialize/compress để ghi xuống file.
- `messages / clusters` thấp, ví dụ khoảng `1.424.590 / 140.000`, cho thấy
  template bị phân mảnh mạnh.

Lookback ngắn không loại bỏ chi phí state cũ: `drain3_state.bin` vẫn được load
và tiếp tục được serialize. `training_checkpoint.json` chỉ điều khiển cursor
Elasticsearch, không làm giảm số cluster hoặc số lần Drain3 save.

Để xác định còn ở Phase 1 hay đã chuyển phase:

```bash
kubectl logs job/logai-training --timestamps | grep -E 'Phase 1 complete|Phase [2-9]|Saving state'
kubectl logs job/logai-training --timestamps | grep 'Restored'
kubectl get pod -l app.kubernetes.io/component=training -o wide
```

Nếu chưa có `Phase 1 complete` và log chủ yếu là `Saving state`, bottleneck là
parse/Drain3 persistence. Nếu đã có `Phase 1 complete`, cần kiểm tra embedding,
HDBSCAN, replay feature và Isolation Forest. Timestamp trước dòng `Saving state`
chỉ là thời điểm bắt đầu log; chưa phải thời gian chính xác của thao tác ghi.

### Checkpoint và crash recovery

Training hiện ghi parsed events vào `training_event_index.jsonl` và `fsync` trước
khi ghi `training_checkpoint.json`. Checkpoint chỉ bị xóa sau khi toàn bộ artifact
training được persist thành công. Khi chạy lại với checkpoint còn tồn tại, pipeline
resume cursor và dùng event ID trong event index để dedup.

Không được chỉ tắt Drain3 auto-save mà giữ nguyên thứ tự hiện tại một cách mù quáng:
nếu event index đã ghi nhưng Drain3 state chưa ghi rồi Pod crash, lần chạy sau có
thể dedup event đó trong khi parser state chưa chứa event. Cách sửa an toàn là
persist Drain3 một lần ở cuối batch, có batch commit/journal, rồi chỉ commit
`training_checkpoint.json` sau khi state và event index của batch đã bền vững.

### Cách reset để train sạch từ đầu

Dừng realtime trước vì training và realtime dùng chung `/app/data` và storage file
không có inter-process lock. Backup PVC, sau đó xóa các artifact training:

```text
/app/data/drain3_state.bin
/app/data/training_checkpoint.json
/app/data/training_event_index.jsonl
/app/data/template_registry.json
/app/data/template_embeddings.pkl
/app/data/group_registry.json
/app/data/group_centroids.pkl
/app/data/models/global_v3.pkl
```

Giữ lại mặc định `documentation_corpus.json`, `documentation_overrides.json`,
`grouping_overrides.json`, `checkpoint.json`, `dedup_index.json` và
`anomaly_state.json`; chỉ xóa các file realtime/documentation này nếu chủ đích
là replay toàn bộ realtime và làm mất state vận hành hiện tại.

Sau reset, log khởi động phải xác nhận `initial_search_after=null`, state Drain3
không tồn tại/size bằng 0 và phải in `start_ts`, `end_ts`, `lookback`, `max_docs`
và `batch_size`. Khi chạm `max_docs`, phải ghi WARNING vì range đã bị cắt.

### Regex/masking thực tế trong code hiện tại

Phần mô tả regex mở rộng ở mục “Đã làm” bên trên **không phản ánh code hiện tại
trong commit đang checkout**. `config.yaml` và default `Drain3Config` hiện chỉ có
4 rule, áp dụng theo đúng thứ tự sau:

```regex
/?\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?
\bblk_-?\d+\b
/(?:[a-zA-Z0-9_.-]+/)+[a-zA-Z0-9_.-]+
\b\d+\b
```

Tất cả đều có `mask_with: '*'` và được chuyển cho Drain3 `MaskingInstruction`,
nên thay bằng placeholder `<*>`. Hiện không có `_strip_patterns` nào chạy bằng
`re.sub` trước Drain3. `extra_delimiters` cũng không được khai báo trong
`Drain3Config`/`config.yaml`, nên dấu `=` chưa được tách riêng.

Các giá trị **chưa** được mask hiện tại gồm prefix ngày giờ/level/thread, UUID,
ISO timestamp, XML/SOAP, URL, hex ID, `key=value`, secret/token, JSON và các ID
chữ-số. `Drain3` chỉ nhận `raw.message`; field `level` của Elasticsearch không
được dùng để loại prefix khỏi message.

Bộ 4 regex hiện tại có thể tạo template phân mảnh mạnh và giải thích số cluster
rất lớn. Image Kubernetes `congdock123/logai-engine:v4` phải được xác nhận có
cùng code/config với repo; tag cũ không được giả định là đã có regex mở rộng.

### Hướng sửa hiệu năng và observability

Không để Drain3 auto-save theo từng cluster trong training. Parse một batch, persist
state một lần ở cuối batch, ghi event index/checkpoint theo thứ tự crash-safe. Với
`batch_size=2000`, 1.000.000 event sẽ giảm từ rất nhiều lần save xuống khoảng 500
lần. Mỗi batch nên log: batch number, hits, parsed, duplicate, tổng event,
timestamp cuối, cluster count, Drain3 messages, state bytes, event-index bytes,
elapsed và throughput. Cuối mỗi phase phải có count + duration; cuối Job phải có
`TRAINING SUCCEEDED` hoặc `TRAINING FAILED` và cho biết checkpoint/event index có
được giữ lại hay không.

Nên bổ sung `--max-docs`, `--batch-size` và env tương ứng, dùng image tag bất biến
theo git SHA, và đặt `activeDeadlineSeconds` trong Job để tránh chạy vô hạn nếu có
lỗi bất thường. `restartPolicy: OnFailure` chỉ restart process bị lỗi; nó không
làm một process đang chạy tự lặp vô hạn.

## Done 2026-10-01: training Phase 1 bottleneck fix (uncommitted)
- `Drain3Parser(config, autosave=False)` in training: no per-template-change full-state save;
  `save_state()` once per batch via `AtomicFilePersistence` (tmp + fsync + rename).
  Commit order per batch: Drain3 state -> event index -> ES cursor. Realtime still autosaves (unchanged).
- Benchmark, 6,000 distinct clusters: old 345s vs new 0.9s (old cost ~quadratic).
- Per-batch Phase 1 log (fetched/parsed/dup, last event ts, clusters, state/index size,
  fetch/parse/save seconds, rate), job config log with ISO range, WARNING when `max_docs`
  truncates, `TRAINING SUCCEEDED/FAILED`. On resume, already-indexed events count towards `max_docs`.
- `config.yaml` `max_docs` back to 200000 (no env/CLI override, by decision).
- 6 tests in `TestDrain3UniversalMaskingRules` fail already before this change (regex work pending).

## Done 2026-10-01 (02:10–02:55): regex layer before Drain3 (uncommitted)
Supersedes the "Regex/masking thực tế" section and items 1, 2, 5 below. The design and
numbers are in `agents.md`. In short: new `logai/parsing/preprocessor.py`:
- level is searched in the first 5 tokens (shared with `es_collector`);
- prefix and every timestamp are **deleted**;
- XML is reduced to element names + words;
- ids and numbers become `<*>`, words are kept.

Config `masking_rules` now defaults to `[]`. Before deploy: reset the PVC training
artifacts and retrain. Known timing-flaky test: `test_high_load_throughput_and_stress`
(also fails on HEAD on this machine).

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
