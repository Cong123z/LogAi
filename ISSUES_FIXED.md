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

## Issue 7 (Bước 1): Mở rộng FeatureVector từ 6 lên 8 chiều chuẩn hóa (Dimensionless)

- **Trạng thái**: ✅ **RESOLVED** (Đã giải quyết Bước 1)
- **Ngày hoàn thành**: 2026-09-09
- **Files liên quan**:
  - `logai/models.py`
  - `logai/features/feature_engine.py`
  - `logai/anomaly/isolation_forest_model.py`
  - `ARCHITECTURE.md`
  - `tests/test_feature_vector_8d.py`

### 1. Vấn đề giải quyết
- **Bỏ phí cửa sổ 10s**: `count_10s` trước đây bị bỏ phí, các đợt micro-bursts bùng nổ log chỉ trong vài giây bị làm loãng bởi tốc độ 1 phút.
- **Ô nhiễm baseline (Baseline Contamination)**: Giá trị rate hiện tại từng bị đẩy vào history trước khi tính mean/std, khiến chính log đột biến kéo vọt baseline lên và làm giảm z-score của nó.
- **Fano factor trên 1m chưa scale-independent**: Chuyển sang hệ số biến thiên $CV^2 = \sigma^2 / \mu^2$ trên cửa sổ 10s.

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
- **Neutral Baseline (Cold-start)**: `[0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0]`.
- **An toàn mô hình**: Nâng `MODEL_VERSION` thành `if-global-v2`. `GlobalAnomalyModel` kiểm tra `EXPECTED_NUM_FEATURES = 8`. Nếu load trúng mô hình cũ 6D (`n_features_in_ != 8`), log cảnh báo và trả về `None` thay vì làm sập tiến trình.

### 3. Kết quả kiểm thử
- Tạo bộ kiểm thử toàn diện tại `tests/test_feature_vector_8d.py` bao gồm 9 unit tests:
  - Kiểm tra schema, thứ tự, tên 8 đặc trưng.
  - Kiểm tra Cold-start Neutral Baseline.
  - Kiểm tra kích thước `_GroupWindow` tuân thủ `rolling_window_points`.
  - Kiểm tra dòng log đều (steady stream).
  - Kiểm tra độ nhạy khi xảy ra micro-burst trong 10s.
  - Kiểm tra các ngưỡng clipping bảo vệ số học.
  - Kiểm tra từ chối tương thích mô hình cũ 6D và dự đoán thành công với 8D.
- Toàn bộ 9/9 tests đều PASS.
