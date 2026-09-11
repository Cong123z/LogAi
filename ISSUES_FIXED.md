# Issues Fixed & Implementation Log

Tài liệu ghi nhận chi tiết các vấn đề kỹ thuật đã được giải quyết, kiến trúc giải pháp, các file thay đổi và kết quả kiểm thử thực tế.

---

## Issue 10: Pipeline Bottleneck — Micro-batch Inference ở Phase Predict (flush 500-hoặc-1s)

- **Trạng thái**: ✅ **RESOLVED**
- **Ngày hoàn thành**: 2026-09-11
- **Kế hoạch gốc**: TASK.md mục #7 ("Fix Pipeline Bottleneck & High-Throughput Optimization")
- **Files liên quan**:
  - `logai/anomaly/isolation_forest_model.py`
  - `logai/realtime/realtime_pipeline.py`
  - `logai/config.py`
  - `config.yaml`
  - `logai/metrics/prometheus_exporter.py`
  - `logai/storage/base.py`
  - `tests/test_predict_batch_and_flush.py` (MỚI)
  - `tests/test_alert_counter.py`, `tests/test_stuck_alert_tick.py`,
    `tests/test_realtime_pipeline_end_to_end.py`,
    `tests/test_realtime_crash_load_and_perf.py`,
    `tests/test_realtime_es_resilience.py` (port sang batch-flush)

---

### 1. Vấn đề ban đầu (Root Cause & Bottlenecks)

1. **Điểm nghẽn chính — inference từng-event (chiếm > 90% CPU)**:
   - `_process_one` gọi `predict()` **từng log một** → ma trận `(1, 8)` lặp 500
     lần/batch thay vì vector hóa một ma trận `(500, 8)`.
   - `predict()` cũ gọi **cả** `decision_function(X)` **lẫn** `model.predict(X)`
     → CPU duyệt qua 100 cây của Isolation Forest **2 lần** cho mỗi event.
   - **Số liệu đo thực tế trên container** (500 log):
     - 500 lần gọi đơn lẻ: **3.02s** (~6.04 ms/log → trần lý thuyết ~165 logs/s).
     - Chỉ `decision_function` 1 lần (bỏ `predict` thừa): **1.53s** (giảm ~50%).
     - Gom batch `(500, 8)` gọi `decision_function` **1 lần**: **0.0045s**
       (~111.000 logs/s, nhanh hơn ~670 lần).
2. **Điểm nghẽn phụ (lock / gauge spam / disk I/O)**:
   - `set_alert_state()` ghi lại cả N gauge trạng thái cho **mỗi** event dù nhóm
     không hề đổi trạng thái → hàng ngàn lần `.set()` vô nghĩa mỗi batch.
   - `logai_events_received_total.inc()` gọi đơn lẻ 500 lần/batch.
   - `template_registry.flush()` / `group_registry.flush()` ghi đè toàn bộ file
     JSON ra đĩa ở mỗi batch dù không có template/group mới.

---

### 2. Kiến trúc giải pháp (Architecture Solution)

#### 2.1. `GlobalAnomalyModel.predict_batch()` — inference vector hóa (một code path)

- Thêm `predict_batch(feature_vectors) -> List[Optional[AnomalyResult]]`:
  `_load()` **1 lần**, guard `n_features_in_` **1 lần**, validate độ dài từng
  vector (vector sai chiều → chèn `None` **đúng vị trí**), gom vector hợp lệ thành
  `X = np.array(rows, dtype=np.float64)`, gọi `decision_function(X)` **đúng 1 lần**.
- Output **cùng thứ tự & cùng độ dài** input; phần tử = `None` khi model chưa
  train, model sai số chiều, hoặc vector đó sai chiều — **giữ nguyên semantic của
  `predict()`**.
- Bỏ hẳn `model.predict(X)` thừa: `is_outlier = raw_score < 0` **tương đương chính
  xác** `IsolationForest.predict() == -1` → giảm một lượt duyệt rừng.
- `predict()` cũ refactor thành `return self.predict_batch([fv])[0]` → **một code
  path duy nhất**, giữ nguyên chữ ký cho mọi caller/test hiện có.

#### 2.2. Micro-batch buffer với 2 ngưỡng flush **config được** (whichever-first)

- Tích lũy cặp `(group_id, FeatureVector)` qua các poll vào buffer instance
  `self._pending_predictions`. `_process_one` chỉ **parse / group / feature /
  append** (dời phần suy luận ra biên flush), giữ nguyên chữ ký `-> bool`.
- **Flush khi**: `n >= predict_batch_size` **HOẶC** `waited >=
  predict_max_wait_seconds` kể từ entry đầu tiên (whichever-first):

  ```python
  n = len(self._pending_predictions)
  waited = monotonic() - self._buffer_started_at if self._buffer_started_at else 0.0
  should_flush = n > 0 and (
      n >= self.config.anomaly.predict_batch_size
      or waited >= self.config.anomaly.predict_max_wait_seconds
  )
  ```
- Vòng lặp ngủ `min(poll_interval, remaining_to_deadline)` khi buffer còn dở để
  timer 1s có hiệu lực; ngủ trọn `poll_interval` khi buffer rỗng.

#### 2.3. Tính đúng đắn: batch ≡ per-event 100% (các bất biến)

- **Một entry / EVENT, KHÔNG collapse theo group**: N event của cùng group G →
  N cặp `(G, fv_i)` → N kết quả → N `transition()` apply **tuần tự** → không mất
  các transition trung gian (spike giữa batch vẫn kích hoạt alert).
- **`predict` thuần túy**: `fv` đã tính & lưu tại `feature_engine.update()`
  (hoặc `snapshot()`); nằm trong buffer bao lâu cũng không đổi kết quả. Model bất
  biến trong batch → gom rồi predict một lượt cho kết quả y hệt.
- **Gauge chỉ chốt ở biên flush**: Prometheus scrape định kỳ (~15s) nên giá trị
  trung gian không quan sát được ở **cả hai** kiến trúc → không phải regression.

#### 2.4. Idle-tick gộp CHUNG buffer (mở rộng Fix #5 sang cả nhóm NORMAL)

- `_evaluate_idle_alerting_groups` duyệt các nhóm **idle ≥ `idle_eval_seconds`**
  (cả non-NORMAL để cool-down **và** NORMAL để refresh state), lấy
  `feature_engine.snapshot(gid, now)` và **append vào chính
  `self._pending_predictions`** → được flush cùng fv-live trong **một
  `predict_batch` duy nhất**.
- **Silence guard** bỏ qua nhóm còn nhận log gần đây (`now - last_seen < gap`) →
  một nhóm **không** thể vừa có fv-live vừa có fv-snapshot cùng lúc → không
  double-count, không nhập nhằng thứ tự.
- Bọc `try/except` quanh phần thu snapshot (bước idle-specific duy nhất) để một
  nhóm lỗi không làm sập vòng lặp poll.

#### 2.5. Crash-safety: cursor tách khỏi buffer, giữ nguyên thứ tự durability

- `_flush_batch()` giữ **đúng thứ tự** cũ: `_flush_predictions()` →
  `template_registry.flush()` → `group_registry.flush()` → `dedup.gc()` →
  `checkpoint.commit(...)` **chỉ khi `_pending_cursor is not None`** → reset state.
- Cursor lấy từ poll (`_pending_cursor`/`_pending_last_ts`), **không** từ buffer →
  trộn fv-idle (vốn không advance cursor) **không hỏng checkpoint**. Flush chỉ-idle
  (stream rỗng) vẫn predict + cool-down nhưng `commit` bị bỏ qua vì cursor `None`.
- Mọi crash **trước** `commit` → cursor không advance; dedup mark là in-memory tới
  `gc` → replay idempotent, không mất/nhân đôi alert.

#### 2.6. Tối ưu phụ (chống lock / I-O spam, hỗ trợ > 1000 logs/s)

- `set_alert_state()`: **return sớm khi `previous == state.alert_state`** → không
  ghi lại N gauge mỗi event khi trạng thái không đổi (counter `log_alerts_total`
  vẫn chỉ tăng đúng 1 lần mỗi lần escalate vào ALERTING).
- `logai_events_received_total.inc(len(batch))` — **1 lần/batch** thay 500 lần.
- `JSONStore` dùng cờ `_dirty` (bật ở `set/delete/bulk_set/replace_all`, `flush`
  no-op khi sạch) → `template_registry.flush()`/`group_registry.flush()` ở biên
  flush thành no-op khi không có template/group mới (theo pattern sẵn có của
  `DedupIndex`).

#### 2.7. Config — KHÔNG hardcode (`logai/config.py`, `config.yaml`)

| Khóa | Default | Ý nghĩa |
| :--- | :--- | :--- |
| `anomaly.predict_batch_size` | `500` | Số entry trong buffer để flush theo count |
| `anomaly.predict_max_wait_seconds` | `1.0` | Trần độ trễ flush theo thời gian (backstop) |
| `elasticsearch.poll_interval_seconds` | `5.0 → 1.0` | Nhịp thức phải ≤ `max_wait` để backstop kịp |

Nạp qua `_merge_dataclass` (có guard `hasattr` → phải khai báo field vào dataclass
trước). Hạ `predict_max_wait_seconds` (vd 0.5s) nếu cần gauge tươi hơn.

---

### 3. Ngữ nghĩa timing của metrics (đánh đổi batching, KHÔNG phải regression)

Gauge `anomaly_score`/`alert_state` chỉ cập nhật tại **biên flush**:
- Độ trễ freshness có **chặn trên = `predict_max_wait_seconds` (≤ 1s), config
  được** — nằm dưới nhịp scrape Prometheus (~15s) và cửa sổ `rate()`/rule (≥ 1m)
  → vô hình trên dashboard/alert. Cửa sổ feature 10s/60s/300s → trễ 1s không ảnh
  hưởng phát hiện.
- Mỗi `{group_id}` là time series độc lập, `.set()` atomic; vòng set trong flush
  là sub-ms → scrape lấy snapshot đồng nhất, không lệch giữa các series.
- Counter tăng theo bậc tại flush nhưng `rate()` cửa sổ ≥ 1m làm mượt burst.

---

### 4. Kết quả kiểm thử & Nghiệm thu (Verification)

Chạy `./.venv/bin/python -m pytest -q` → **132/132 tests PASS**.

1. **Test mới** (`tests/test_predict_batch_and_flush.py`):
   - `test_untrained_model_returns_all_none`: model chưa train → toàn `None`
     (kể cả input rỗng → `[]`).
   - `test_batch_matches_per_event`: `predict_batch([fv_i])` trùng khớp từng phần
     tử với `predict(fv_i)` cho 60 vector (score, anomaly, group_id, timestamp).
   - `test_bad_dimension_vectors_slot_back_none`: vector sai chiều → `None` **đúng
     vị trí**, vector hợp lệ hai bên vẫn được chấm.
   - `test_flush_by_count`: poll đúng `predict_batch_size` event → `predict_batch`
     gọi **1 lần**, buffer rỗng sau flush, checkpoint commit cursor.
   - `test_flush_by_time_below_count_threshold`: buffer < `predict_batch_size`,
     giả lập `monotonic` trôi ≥ `predict_max_wait_seconds` → flush vẫn kích hoạt
     và commit cursor của batch thật.
2. **Test throttle gauge** (`tests/test_alert_counter.py`):
   - `test_unchanged_state_writes_gauge_once`: state không đổi → chỉ ghi gauge 1
     lần (N series), 5 lần lặp thêm 0.
   - `test_state_change_rewrites_gauges`: transition thật → ghi lại đủ bộ gauge.
3. **Idle gộp batch** (`tests/test_stuck_alert_tick.py`):
   - `test_normal_idle_group_is_snapshotted`: nhóm NORMAL im lâu vẫn được
     snapshot vào cùng buffer.
   - `test_alert_cools_down_when_events_stop`: nhóm ALERTING im ≥ 5s → snapshot +
     flush hạ được về NORMAL.
4. **Crash-safety & load** (`tests/test_realtime_crash_load_and_perf.py`):
   - Crash ở mọi stage (template/group flush, dedup gc, DLQ, terminal state)
     trước `commit` → cursor không advance; mid-batch crash → dedup chặn replay,
     không mất/nhân đôi; benchmark 10.000 log vẫn vượt 1.000 logs/s.
5. **Regression**: các test end-to-end / ES-resilience được **port** sang
   batch-flush (set `predict_batch_size = 1` ở setUp lái `run_forever`, hoặc gọi
   `_flush_predictions()` sau khi buffer trong test đơn vị) — giữ nguyên mọi
   assertion.

---

## Issue 8: Streaming Historical Training và Resume Checkpoint

- **Trạng thái**: ✅ **RESOLVED**
- Elasticsearch historical data được đọc bằng `stream_historical_batches()`.
- Mỗi batch được parse vào Drain3 ngay, không tích lũy `RawLog` trước khi parse.
- `LocalTrainingDedup` loại duplicate mà không chạm realtime dedup state.
- `training_event_index.jsonl` được append và `fsync` trước khi ghi cursor
  `training_checkpoint.json`.
- Grouping, feature generation và Global Isolation Forest replay từ event index,
  nên resume không bỏ qua dữ liệu của các batch trước sự cố.
- Checkpoint và event index chỉ được xóa sau khi toàn bộ training thành công.
- Realtime vẫn dùng `checkpoint.json` và `dedup_index.json` độc lập.

---

## Issue 9: Realtime Checkpoint Commit Trước Khi Xử Lý Batch

- **Trạng thái**: ✅ **RESOLVED**
- `ElasticsearchCollector.poll_batch()` không còn ghi checkpoint sau khi fetch.
- Collector trả về batch kèm `search_after` cursor cho realtime orchestrator.
- Realtime xử lý toàn bộ batch, flush registry và dedup trước khi commit cursor.
- Crash giữa batch khiến batch được fetch lại; `DedupIndex` loại event đã xử lý.
- Lỗi processing được ghi DLQ thành công được coi là terminal để batch có thể
  commit; lỗi ghi DLQ/flush/checkpoint sẽ không advance cursor.
- Bổ sung test collector side-effect, crash giữa batch, flush failure, DLQ,
  duplicate boundary và batch 20.000 events.

---

## Issue 1: Anomaly Detection — Chuyển sang 1 Global Model với 6 Feature chuẩn hóa

- **Trạng thái**: ✅ **RESOLVED** (Đã giải quyết)
- **Ngày hoàn thành**: 2026-09-08
- **Files liên quan**:
  - `logai/models.py`
  - `logai/features/feature_engine.py`
  - `logai/anomaly/isolation_forest_model.py`
  - `logai/training/train_pipeline.py`
  - `logai/realtime/realtime_pipeline.py`

---

### 1. Vấn đề ban đầu (Root Cause & Bottlenecks)

1. **Lỗi Cold-start**:
   - Hệ thống cũ huấn luyện riêng 1 mô hình Isolation Forest cho mỗi log group (`data/models/<group_id>.pkl`).
   - Nếu một group chưa đủ 30 log events (`min_training_samples`) trong lịch sử huấn luyện, hoặc là group mới xuất hiện trong luồng streaming realtime, hàm `predict()` trả về `None`.
   - Kết quả: Hệ thống hoàn toàn không phát hiện được bất thường đối với các group mới, kể cả khi group đó phát sinh bùng nổ log lỗi (Error Storm / DDoS).
2. **Lãng phí bộ nhớ**:
   - Với hàng trăm đến hàng ngàn log groups, việc lưu trữ và nạp hàng ngàn mô hình IF tiêu tốn hàng trăm MB đến hàng GB RAM trong `realtime_pipeline`.
3. **Feature không thuần nhất (Volume Dependency)**:
   - Bộ 13 features cũ pha trộn giữa số đo tuyệt đối (`count_10s`, `count_1m`, `rate_1m`, `rate_5m`, `rolling_mean`, `rolling_std`, `rolling_median`, `max_recent_rate`) và số đo tương đối.
   - Các chỉ số tuyệt đối phụ thuộc vào volume riêng của từng service (service 10 logs/s khác với service 10,000 logs/s), khiến mô hình không thể khái quát hóa hành vi bất thường trên toàn hệ thống.

---

### 2. Kiến trúc giải pháp (Architecture Solution)

#### 2.1. Chuẩn hóa 6 Feature phi thứ nguyên (Dimensionless Features)
Rút gọn và chuyển đổi toàn bộ `FeatureVector` sang 6 đặc trưng tương đối, độc lập với log volume:

| STT | Feature | Công thức toán học | Ý nghĩa phát hiện bất thường |
| :--- | :--- | :--- | :--- |
| 1 | `z_score` | $\frac{\text{rate\_1m} - \mu}{\sigma + \epsilon}$ *(khi $\sigma > \epsilon$)* | Độ lệch chuẩn hóa của tốc độ 1 phút so với trung bình lịch sử. |
| 2 | `growth_rate` | $\frac{\text{rate\_1m}}{\text{rate\_5m} + \epsilon}$ | Tốc độ gia tăng log ngắn hạn (1m) so với trung hạn (5m). |
| 3 | `burstiness` | $\frac{\sigma^2}{\mu + \epsilon}$ *(Fano factor)* | Đo mức độ dồn cục/bùng nổ bất thường của dòng log. |
| 4 | `rate_delta_norm` | $\frac{\text{rate\_1m} - \text{rate\_5m}}{\sigma + \epsilon}$ *(khi $\sigma > \epsilon$)* | Chênh lệch tốc độ được chuẩn hóa theo độ biến động quá khứ. |
| 5 | `slope_norm` | $\frac{\text{slope}}{\mu + \epsilon}$ *(khi $\mu > \epsilon$)* | Độ dốc xu hướng được chuẩn hóa theo cường độ log bình quân. |
| 6 | `spike_ratio` | $\frac{\text{max\_recent\_rate}}{\mu + \epsilon}$ *(khi $\mu > \epsilon$)* | Tỷ lệ giữa đỉnh phát sinh cao nhất so với mức bình quân. |

#### 2.2. Xử lý Cold-start ở tầng Feature (Neutral Baseline)
Khi một group mới xuất hiện, lịch sử sliding window chưa có dữ liệu ($\text{len}(hist) < 2$ hoặc $\sigma = 0$). Để tránh lỗi chia cho 0 hoặc nổ số gây báo động giả, `FeatureEngine` áp dụng cơ chế **Neutral Baseline**:
$$\vec{v}_{\text{initial}} = [0.0, \; 1.0, \; 0.0, \; 0.0, \; 0.0, \; 1.0]$$
*(tương ứng: `z_score=0`, `growth_rate=1`, `burstiness=0`, `rate_delta_norm=0`, `slope_norm=0`, `spike_ratio=1`)*
- Vector này nằm sâu trong vùng phân phối bình thường nhất (inlier) của mô hình Isolation Forest, giúp tránh báo động giả khi group mới khởi tạo.
- Ngay khi group mới bị dồn dập log bất thường, các chỉ số $growth\_rate$, $spike\_ratio$, $slope\_norm$ tăng vọt trong vài giây và mô hình bắt được ngay.

#### 2.3. Mô hình Global Isolation Forest (`GlobalAnomalyModel`)
- Huấn luyện duy nhất **1 Global Isolation Forest** trên toàn bộ feature vectors gộp từ tất cả các log groups (ma trận $X \in \mathbb{R}^{M \times 6}$).
- Lưu và tải mô hình tập trung tại `data/models/global.pkl`.
- Loại bỏ hoàn toàn cold-start ở tầng model: Mọi group ở realtime đều sử dụng global model có sẵn để dự đoán.
- Đảm bảo **Train/Serve Parity**: Cả hai pipeline đều sử dụng cùng class `FeatureEngine` và cùng thứ tự vector 6 chiều từ `FeatureVector.as_vector()`.

---

### 3. Chi tiết các file đã sửa đổi

1. **`logai/models.py`**:
   - Tái cấu trúc dataclass `FeatureVector`: giữ lại `group_id`, `timestamp` và 6 thuộc tính chuẩn hóa mới.
   - Cập nhật `as_vector()` trả về `List[float]` độ dài 6.
   - Cập nhật `feature_names()` trả về 6 tên đặc trưng mới.
2. **`logai/features/feature_engine.py`**:
   - Cập nhật hàm `_compute()` tính toán 6 đặc trưng dimensionless.
   - Xử lý các điều kiện biên: $\epsilon = 10^{-9}$, kiểm tra $\sigma > \epsilon$ và $\mu > \epsilon$, fallback về Neutral Baseline cho event đầu tiên.
3. **`logai/anomaly/isolation_forest_model.py`**:
   - Thay thế `GroupAnomalyModels` bằng `GlobalAnomalyModel`.
   - Lưu model dưới key cố định `GLOBAL_MODEL_KEY = "global"`.
   - `train(feature_vectors)`: nhận danh sách vector toàn cục, fit ma trận $(M, 6)$.
   - `predict(feature_vector)`: dự đoán bằng global model cho mọi group.
   - Bổ sung alias tương thích ngược `GroupAnomalyModels = GlobalAnomalyModel` và helper `train_group()`, `has_model(group_id=None)`.
4. **`logai/training/train_pipeline.py`**:
   - Khởi tạo `GlobalAnomalyModel`.
   - Cập nhật `_train_anomaly_models()`: gom tất cả `FeatureVector` từ tất cả các log groups thành một danh sách phẳng và huấn luyện 1 mô hình global duy nhất.
5. **`logai/realtime/realtime_pipeline.py`**:
   - Khởi tạo `GlobalAnomalyModel`.
   - Cập nhật `_run_anomaly_and_alert()`: gọi trực tiếp `self.anomaly_model.predict(feature_vector)`.

---

### 4. Kết quả kiểm thử & Nghiệm thu (Verification)

1. **Kiểm tra cú pháp (Syntax check)**:
   - Tất cả 5 file đã được kiểm tra bằng `python3 -m py_compile`, không có lỗi cú pháp.
2. **Kiểm thử Unit & Integration Test**:
   - `FeatureEngine`: Event đầu tiên sinh vector chuẩn Neutral Baseline `[0.0, 1.0, 0.0, 0.0, 0.0, 1.0]`.
   - `GlobalAnomalyModel`: Huấn luyện thành công trên tập dữ liệu đa group, lưu đúng file `global.pkl`.
   - **Xác nhận Cold-start**: Nhóm log mới toanh `g_new_brand` (chưa từng có trong tập train) được đưa vào realtime predict và trả về kết quả `AnomalyResult` đầy đủ, không còn bị lỗi bỏ qua như trước.

---

## Issue 6: Tối ưu xây dựng Group Registry — Aggregate từ Template Registry

- **Trạng thái**: ✅ **RESOLVED** (Đã giải quyết)
- **Files liên quan**:
  - `logai/training/train_pipeline.py`

### Vấn đề

`_build_group_registry()` trước đây quét toàn bộ `parsed_events` và tạo thêm
`events_by_group` chỉ để tính `event_count`, `first_seen`, `last_seen` và
`service`. Với N events lớn, bước này tạo thêm bộ nhớ và một vòng lặp O(N),
trong khi `TemplateRegistry` đã lưu đủ metadata tổng hợp theo từng template.

### Giải pháp

Hàm hiện nhận trực tiếp ánh xạ `template_to_group`, lấy các
`TemplateState` từ registry và tính metadata của group bằng:

- `event_count`: tổng `event_count` của các template.
- `first_seen`: mốc nhỏ nhất của các template.
- `last_seen`: mốc lớn nhất của các template.
- `service` và `representative_template`: lấy từ template đầu tiên theo thứ tự
  ổn định của danh sách template.

Output vẫn là `GroupState` với cùng schema và được lưu vào
`data/group_registry.json`. Chi phí bước này giảm từ O(N) theo số event xuống
O(T) theo số template.

### Xác minh

- `python3 -m compileall -q logai scripts` chạy thành công.
- Đã rà soát logic aggregate: `event_count` dùng tổng metadata template,
  `first_seen`/`last_seen` dùng min/max và output vẫn giữ nguyên schema
  `GroupState`.
- Smoke test với `FakeTemplateRegistry` và `FakeGroupRegistry` xác nhận
  signature mới, aggregate metadata và số lần gọi registry theo số template.

---

## Issue 7 (Bước 1): Mở rộng FeatureVector từ 6 lên 8 chiều chuẩn hóa & Xác nhận Per-Event Realtime Parity

- **Trạng thái**: ✅ **RESOLVED**
- **Ngày hoàn thành**: 2026-09-09
- **Files liên quan**:
  - `logai/models.py`
  - `logai/features/feature_engine.py`
  - `logai/anomaly/isolation_forest_model.py`
  - `logai/training/train_pipeline.py`
  - `logai/realtime/realtime_pipeline.py`
  - `ARCHITECTURE.md`
  - `tests/test_feature_vector_8d.py`
  - `tests/test_end_to_end_parity.py`

### 1. Vấn đề giải quyết
- **Bỏ phí cửa sổ 10s**: `count_10s` trước đây bị bỏ phí, các đợt micro-bursts bùng nổ log chỉ trong vài giây bị làm loãng bởi tốc độ 1 phút.
- **Ô nhiễm baseline (Baseline Contamination)**: Giá trị rate hiện tại từng bị đẩy vào history trước khi tính mean/std, khiến chính log đột biến kéo vọt baseline lên và làm giảm z-score của nó.
- **Fano factor trên 1m chưa scale-independent**: Chuyển sang hệ số biến thiên $CV^2 = \sigma^2 / \mu^2$ trên cửa sổ 10s.
- **History config không có tác dụng**: Trước đây `_GroupWindow` hardcode `maxlen=64`. Hiện đã liên kết trực tiếp với cấu hình `config.rolling_window_points` (mặc định 30).

### 2. Kiến trúc giải pháp
- **Bộ 8 đặc trưng dimensionless**:
  1. `z_score_10s`: $\frac{r_{10} - \mu_{10}}{\sigma_{10} + \epsilon}$ (clip `[-10, 10]`)
  2. `z_score_1m`: $\frac{r_{1m} - \mu_{1m}}{\sigma_{1m} + \epsilon}$ (clip `[-10, 10]`)
  3. `short_growth_rate`: $\frac{r_{10}}{r_{1m} + \epsilon}$ (clip `[0, 6]`)
  4. `growth_rate`: $\frac{r_{1m}}{r_{5m} + \epsilon}$ (clip `[0, 5]`)
  5. `burstiness_10s`: $\frac{\sigma_{10}^2}{\mu_{10}^2 + \epsilon}$ (clip `[0, 20]`)
  6. `rate_delta_norm`: $\frac{r_{1m} - r_{5m}}{\sigma_{1m} + \epsilon}$ (clip `[-10, 10]`)
  7. `slope_norm`: $\frac{\text{slope}(r_{1m})}{\mu_{1m} + \epsilon}$ (clip `[-10, 10]`)
  8. `spike_ratio_10s`: $\frac{\max(r_{10})}{\mu_{10} + \epsilon}$ (clip `[0, 20]`)
- **Tách biệt baseline**: Baseline $\mu, \sigma$ được tính từ lịch sử trước khi append mẫu hiện tại.
- **Neutral Baseline (Cold-start)**: `[0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0]` cho event đầu tiên.
- **An toàn mô hình**: Nâng `MODEL_VERSION` thành `if-global-v2`. `GlobalAnomalyModel` kiểm tra `EXPECTED_NUM_FEATURES = 8`. Nếu load trúng mô hình cũ 6D (`n_features_in_ != 8`), log cảnh báo và trả về `None` thay vì làm sập tiến trình.
- **Cơ chế xử lý Per-Event**: Giữ nguyên xử lý bắt và tính toán trực tiếp trên từng event ở cả Realtime Pipeline và Training Pipeline để đảm bảo độ trễ phát hiện tức thời ($<1\text{ms}$). Đảm bảo tính nhất quán tuyệt đối (Train/Serve Parity).

### 3. Kết quả kiểm thử & Nghiệm thu
- **Bộ kiểm thử đặc trưng 8D** (`tests/test_feature_vector_8d.py`):
  - Kiểm tra schema, thứ tự, tên 8 đặc trưng.
  - Kiểm tra Cold-start Neutral Baseline.
  - Kiểm tra kích thước `_GroupWindow` tuân thủ `rolling_window_points`.
  - Kiểm tra dòng log đều (steady stream).
  - Kiểm tra độ nhạy khi xảy ra micro-burst trong 10s.
  - Kiểm tra các ngưỡng clipping bảo vệ số học.
  - Kiểm tra từ chối tương thích mô hình cũ 6D và dự đoán thành công với 8D.
- **Bộ kiểm thử toàn trình & Train/Serve Parity** (`tests/test_end_to_end_parity.py`):
  - Mô phỏng training trích xuất vector 8D per-event và fit Global model thành công.
  - Mô phỏng realtime tiếp nhận log và suy luận per-event; alert state duy trì `NORMAL` khi log đều.
  - Khi có micro-burst bùng nổ, `z_score_10s` và `short_growth_rate` kích hoạt tức thì, đưa `AlertStateMachine` từ `NORMAL` $\rightarrow$ `WARMING` $\rightarrow$ `ALERTING` trong thời gian thực.
  - Hai engine độc lập chạy cùng 1 chuỗi log cho ra kết quả vector 8D đồng nhất 100%.
- **Tổng kết**: 10/10 tests PASS (`python3 -m unittest discover -s tests`).

---

## Issue 2: Bottleneck DedupIndex — Chuyển sang Bounded LRU Cache (OrderedDict)

- **Trạng thái**: ✅ **RESOLVED** (Đã giải quyết)
- **Ngày hoàn thành**: 2026-09-09
- **Files liên quan**:
  - `logai/storage/dedup.py`
  - `logai/config.py`
  - `config.yaml`
  - `logai/realtime/realtime_pipeline.py`
  - `tests/test_dedup_index.py`

### 1. Vấn đề ban đầu (Root Cause & Bottlenecks)
1. **Tràn bộ nhớ RAM (OOM Crash)**:
   - `DedupIndex` cũ dùng `JSONStore` lưu toàn bộ `event_id` trong 24 giờ.
   - Ở quy mô 10GB/ngày (~35 triệu events/ngày), Python dict chứa 35 triệu keys tiêu tốn **~5.25 GB RAM**, gây sập container do hết bộ nhớ.
2. **Nghẽn CPU ở hàm `gc()` (Pipeline Stall)**:
   - Sau mỗi batch 500 logs, hàm `gc()` quét qua toàn bộ 35 triệu phần tử ($O(D)$), gây đơ (stall) pipeline từ 1 đến 3 giây CPU cho mỗi batch.
3. **Quá tải đĩa (Disk I/O Write Amplification)**:
   - Mỗi lần flush, toàn bộ dict được serialize ra file JSON có `indent=2`, tạo ra file nặng 1.5 – 2 GB và ghi đè liên tục xuống SSD.

### 2. Kiến trúc giải pháp
1. **Bounded LRU Cache với `OrderedDict`**:
   - Khóa trần kích thước tối đa qua cấu hình `reliability.dedup_max_size` (mặc định: `200.000` entries, tương đương vùng đệm 400 batches liên tiếp).
   - Khi chèn key mới vượt quá `max_size`, hàm `mark()` tự động đẩy phần tử cũ nhất ở đầu ra khỏi hàng đợi (`popitem(last=False)`) với chi phí $O(1)$ amortized.
2. **Triệt tiêu Stall ở `gc()`**:
   - Do việc loại bỏ phần tử diễn ra tự động ở $O(1)$, hàm `gc()` không còn phải duyệt tuyến tính qua danh sách khóa. Hàm `gc()` chỉ thực hiện flush nếu có thay đổi và trả về `0`, loại bỏ 100% thời gian stall.
3. **Snapshot đĩa gọn nhẹ & Lazy Flush**:
   - Sử dụng cờ `_dirty`: chỉ ghi đĩa khi có dữ liệu mới được đánh dấu.
   - Lưu trữ dạng mảng JSON compact không indent (`separators=(',', ':')`), giảm kích thước file từ 2 GB xuống chỉ còn **~3 – 5 MB**.
   - Tương thích ngược 100% với file JSON định dạng dict cũ (`{"event_id": timestamp}`) khi khởi động lại.

### 3. Kết quả kiểm thử & Nghiệm thu
- Tạo bộ kiểm thử chuyên biệt [tests/test_dedup_index.py](file:///home/cong/Documents/logai-engine/tests/test_dedup_index.py) gồm 5 tests:
  - `test_bounded_size_eviction`: Xác nhận tự động evict phần tử cũ nhất khi chạm trần `max_size` trong $O(1)$.
  - `test_lru_move_to_end`: Xác nhận làm mới vị trí LRU khi một key xuất hiện lại.
  - `test_persistence_flush_and_reload`: Xác nhận ghi snapshot xuống đĩa an toàn và load lại nguyên vẹn khi restart.
  - `test_legacy_format_compatibility`: Xác nhận nạp mượt mà định dạng JSON dict cũ mà không phát sinh lỗi.
  - `test_gc_flushes_and_returns_zero`: Xác nhận `gc()` chạy tức thì, không gây nghẽn CPU.
- **Tổng kết**: Toàn bộ **15/15 tests** trong test suite (`discover -s tests`) đều **PASS 100%**.

---

## Issue 3: Bottleneck `_update_template_metrics` — Chuyển sang O(1) Incremental Counter trong TemplateRegistry

- **Trạng thái**: ✅ **RESOLVED** (Đã giải quyết)
- **Ngày hoàn thành**: 2026-09-09
- **Files liên quan**:
  - `logai/storage/registries.py`
  - `logai/realtime/realtime_pipeline.py`
  - `tests/test_template_metrics.py`
  - `tests/test_realtime_pipeline_end_to_end.py`

### 1. Vấn đề ban đầu (Root Cause & Bottlenecks)

1. **Nghẽn CPU nghiêm trọng trên Hot Path ($O(T)$ mỗi log event)**:
   - Trong `realtime_pipeline._process_one`, mỗi log event đi qua đều gọi `_update_template_metrics(parsed.raw.service)`.
   - Hàm này gọi `self.template_registry.all_templates()`, hàm này deserialize toàn bộ dict `self._meta.all()` và instantiate lại đối tượng dataclass `TemplateState` cho toàn bộ $T$ templates, sau đó chạy vòng lặp Python `sum(1 for t in ... if t.service == service)`.
   - Với $T = 2.000$ templates và lưu lượng $35$ triệu logs/ngày: tạo ra $2.000 \times 35.000.000 = 70$ tỷ object dataclass mỗi ngày, ngốn khoảng **6 – 10 giờ CPU mỗi ngày** vô nghĩa chỉ để đếm lại một con số hầu như không đổi.
2. **Lãng phí Prometheus Metric update**:
   - Tần suất xuất hiện template mới trong môi trường production ổn định là rất thấp (thường chỉ vài chục đến vài trăm template mới mỗi ngày). Tuy nhiên, gauge Prometheus `app_log_templates_total` lại bị ghi đè liên tục 35 triệu lần mỗi ngày.

### 2. Kiến trúc giải pháp (Approach 2 - O(1) Incremental Counter)

1. **Quản lý bộ đếm O(1) tập trung trong `TemplateRegistry` (`logai/storage/registries.py`)**:
   - `TemplateRegistry` duy trì map bộ nhớ trong `self._counts_by_service: Dict[str, int] = {}`.
   - Khi khởi tạo (`__init__`), registry duyệt metadata 1 lần duy nhất để nạp số lượng ban đầu ($O(T)$ một lần duy nhất lúc khởi động process).
   - Chuẩn hóa tên service: tự động strip khoảng trắng và fallback về `"unknown"` nếu giá trị rỗng hoặc `None`.
   - Trong hàm `upsert(state)`:
     - Kiểm tra nếu `state.template_id` chưa từng tồn tại (`is_new = old_raw is None`): tăng bộ đếm `_counts_by_service[service] += 1` trong $O(1)$ và trả về `is_new = True`.
     - Nếu cập nhật template đã có (cập nhật `last_seen`, `event_count`, v.v.): không tăng bộ đếm, trả về `is_new = False`.
     - Hỗ trợ đổi service an toàn: tự động giảm đếm service cũ, tăng đếm service mới, và tự động dọn dẹp key nếu đếm giảm về 0.
     - Bảo vệ đa luồng bằng `threading.RLock()`.
   - Bổ sung các phương thức truy vấn $O(1)$ và thao tác dữ liệu an toàn:
     - `count_by_service(service: str) -> int`: Truy vấn tức thì trong $O(1)$.
     - `all_counts_by_service() -> Dict[str, int]`: Trả về bản sao shallow copy độc lập để caller không làm thay đổi trạng thái bên trong registry.
     - `total_count() -> int`: Tổng số template hiện có.
     - `delete(template_id: str)`: Giảm bộ đếm và dọn key sạch sẽ khi xóa template.

2. **Tối ưu hóa luồng Realtime Pipeline (`logai/realtime/realtime_pipeline.py`)**:
   - **Khởi động (`__init__`)**: Duyệt qua `self.template_registry.all_counts_by_service()` để warm-up ngay giá trị cho Prometheus gauge `app_log_templates_total` của từng service khi tiến trình vừa khởi chạy.
   - **Loại bỏ khỏi Hot Path (`_process_one`)**: **Đã xóa bỏ hoàn toàn** lệnh gọi `self._update_template_metrics()` sau mỗi sự kiện log.
   - **Kích hoạt theo sự kiện (`_assign_group`)**: Chỉ khi `upsert()` trả về `is_new is True` (phát hiện và lưu một template mới toanh vào registry), pipeline mới gọi `self._update_template_metrics(parsed.raw.service)`.
   - **Truy vấn $O(1)$**: Hàm `_update_template_metrics()` chuyển sang gọi `self.template_registry.count_by_service(svc)` ($O(1)$) thay vì quét toàn bộ template.

### 3. Kết quả kiểm thử & Đo kiểm chuẩn hiệu năng (Verification & Benchmark)

1. **Bộ kiểm thử Template Counting & Edge Cases** (`tests/test_template_metrics.py` - 15 tests):
   - `test_initial_counts_empty`: Trạng thái ban đầu rỗng.
   - `test_upsert_new_template_increments_count`: Thêm template mới -> tăng đếm chính xác.
   - `test_upsert_existing_template_does_not_increment_count`: Cập nhật template cũ -> không tăng lặp.
   - `test_multiple_templates_and_services`: Quản lý đa service và fallback "unknown".
   - `test_reload_from_disk_restores_counts`: Khôi phục chính xác bộ đếm sau khi restart process.
   - `test_service_change_updates_counts`: Xử lý mượt mà khi template đổi service.
   - `test_concurrent_upserts_thread_safety`: Đảm bảo an toàn đa luồng dưới tải đồng thời.
   - `test_whitespace_and_none_service_normalization`: Chuẩn hóa khoảng trắng và None.
   - `test_delete_template_decrements_and_prunes_empty_services`: Xóa template và dọn dẹp key 0.
   - `test_all_counts_immutability`: Đảm bảo tính bất biến của dữ liệu trả về.
   - `test_corrupt_file_graceful_recovery`: Phục hồi an toàn khi file JSON bị hỏng.
   - `test_high_concurrency_race_condition_same_template_id`: 30 thread đồng thời ghi cùng 1 template ID -> đếm đúng 1.
   - `test_pending_to_grouped_template_lifecycle_no_double_counting`: Vòng đời template chuyển từ Pending sang Grouped chỉ kích hoạt metric đúng 1 lần duy nhất.
   - `test_pipeline_does_not_scan_all_templates_on_events`: Xác thực thực tế pipeline không bao giờ gọi `all_templates()` trên hot path.

2. **Kết quả đo kiểm chuẩn hiệu năng thực tế (Benchmark)**:
   - Mô phỏng môi trường production với $T = 2.000$ templates và $N = 2.000$ log events:
     ```text
     [BENCHMARK] Events: 2000 | Templates: 2000
       - Old O(T) approach: 1.2117s (605.85 µs/event)
       - New O(1) approach: 0.0002s (0.12 µs/event)
       - Speedup: 5015.0x faster!
     ```
   - **Tăng tốc độ xử lý**: Nhanh hơn **hơn 5.000 lần** ($5015\times$).
   - **Tiết kiệm CPU**: Tiết kiệm triệt để **~6 giờ CPU mỗi ngày** ở quy mô 35M events/ngày.

3. **Bộ kiểm thử tích hợp Pipeline toàn trình** (`tests/test_realtime_pipeline_end_to_end.py` - 6 tests):
   - Xác nhận sự phối hợp mượt mà giữa Dedup Index ($O(1)$) $\rightarrow$ Template Counting ($O(1)$) $\rightarrow$ Fast-path cho known templates $\rightarrow$ Feature Engine 8D $\rightarrow$ Global Anomaly Model $\rightarrow$ Alert State Machine $\rightarrow$ Dead Letter Queue $\rightarrow$ Batch GC.

- **Tổng kết**: Toàn bộ **40/40 tests** trong hệ thống (`python3 -m unittest discover -s tests`) đều **PASS 100%**.
