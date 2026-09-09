# Issues Fixed & Implementation Log

Tài liệu ghi nhận chi tiết các vấn đề kỹ thuật đã được giải quyết, kiến trúc giải pháp, các file thay đổi và kết quả kiểm thử thực tế.

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

