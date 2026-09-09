# Known Issues & Refactoring Tasks

Tài liệu ghi nhận các vấn đề đã phát hiện trong quá trình review code.
Mỗi mục ghi rõ: vấn đề, file liên quan, ảnh hưởng, và hướng sửa.

---

## 1. Anomaly Detection: Đổi sang 1 Global Model với feature chuẩn hóa

**Status**: ✅ RESOLVED (Đã giải quyết — xem chi tiết tại `ISSUES_FIXED.md`)  
**Files liên quan**:
- `logai/anomaly/isolation_forest_model.py`
- `logai/features/feature_engine.py`
- `logai/models.py` (FeatureVector)
- `logai/training/train_pipeline.py`
- `logai/realtime/realtime_pipeline.py`

### Vấn đề

Hiện tại train **1 Isolation Forest riêng cho mỗi group** (`GroupAnomalyModels`).
Với hệ thống thực tế (hàng trăm → hàng ngàn groups):
- Tốn bộ nhớ: mỗi IF ~1-5 MB × N groups.
- Cold-start: group chưa đủ `min_training_samples` (30) thì hoàn toàn không
  detect được anomaly.
- Phức tạp không cần thiết cho bài toán này.

### Feature set hiện tại (13 features, pha trộn tuyệt đối và tương đối)

```
Tuyệt đối (phụ thuộc vào volume của group):
  count_10s, count_1m, rate_1m, rate_5m,
  rolling_mean, rolling_std, rolling_median, max_recent_rate

Tương đối (so sánh được giữa các groups):
  z_score, growth_rate, burstiness, rate_delta, slope
```

Các feature tuyệt đối gây hại cho global model vì chúng encode thông tin
"group nào volume cao/thấp" thay vì "hành vi nào bất thường".

### Hướng sửa

**Bước 1**: Đổi `FeatureVector` — chỉ giữ feature tương đối (dimensionless):

| Feature mới       | Công thức                                  | Thay cho          |
|--------------------|--------------------------------------------|--------------------|
| `z_score`          | (rate_1m - rolling_mean) / rolling_std     | giữ nguyên         |
| `growth_rate`      | rate_1m / rate_5m                          | giữ nguyên         |
| `burstiness`       | rolling_std² / rolling_mean                | giữ nguyên         |
| `rate_delta_norm`  | (rate_1m - rate_5m) / (rolling_std + ε)    | thay `rate_delta`  |
| `slope_norm`       | slope / (rolling_mean + ε)                 | thay `slope`       |
| `spike_ratio`      | max_recent_rate / (rolling_mean + ε)       | thay `max_recent`  |

8 features, tất cả dimensionless → không cần thêm normalizer, global model
hoạt động trực tiếp.

**Bước 2**: Đổi `GroupAnomalyModels` — train 1 IF duy nhất trên tất cả groups:

```python
# Training: gộp tất cả normalized feature vectors
all_vectors = []
for gid, events in grouped.items():
    for e in events:
        fv = engine.update(gid, e.raw.timestamp)
        all_vectors.append(fv.as_vector())
model.fit(np.array(all_vectors))
model_store.save("global", model)

# Predict: 1 model cho mọi group
model = model_store.load("global")
model.predict(feature_vector.as_vector())
```

**Bước 3**: Cân nhắc thay IF bằng static thresholds (đơn giản hơn nữa):

```python
def detect(fv):
    return (
        abs(fv.z_score) > 3.0
        or fv.growth_rate > 5.0
        or fv.spike_ratio > 4.0
        or fv.burstiness > 2.0
    )
```

Với feature set toàn relative, z_score bản thân đã là phép phát hiện bất
thường thống kê. IF thêm giá trị chủ yếu ở phát hiện tổ hợp bất thường
(nhiều feature cùng hơi cao nhưng không feature nào vượt ngưỡng đơn lẻ) —
trường hợp này hiếm trong log monitoring.

Giữ nguyên Alert State Machine (hysteresis NORMAL→WARMING→ALERTING→COOLING)
bất kể chọn IF hay static thresholds.

---

## 2. Bottleneck: DedupIndex sẽ sập ở quy mô lớn

**Status**: ✅ RESOLVED (Đã giải quyết — xem chi tiết tại `ISSUES_FIXED.md`)  
**Severity**: Critical — gây OOM hoặc pipeline stall  
**Files liên quan**:
- `logai/storage/dedup.py`
- `logai/config.py`
- `config.yaml`
- `logai/realtime/realtime_pipeline.py`
- `tests/test_dedup_index.py`

### Vấn đề

`DedupIndex` cũ dùng `JSONStore` (Python dict backed by JSON file) lưu tất cả
`event_id` đã xử lý trong 24h (TTL = 86400s).

Ở quy mô 10GB/ngày (~35M events/ngày):
- Dict chứa 35 triệu entries → **~5 GB RAM** chỉ riêng dedup.
- `gc()` chạy mỗi batch, duyệt toàn bộ 35M entries → **pipeline stall vài giây**.
- `flush()` serialize 35M entries ra JSON file → **file ~1-2 GB**, ghi chậm.

### Giải pháp đã triển khai

Thay thế bằng Bounded LRU Cache sử dụng `OrderedDict` với giới hạn `max_size` (mặc định 200.000 entries, cấu hình qua `reliability.dedup_max_size`):
- Tự động evict phần tử cũ nhất ở đầu trong $O(1)$ amortized (`popitem(last=False)`).
- RAM tiêu thụ cố định ở mức **~20 MB** (hoặc ~50 MB với 500k entries).
- Hàm `gc()` triệt tiêu hoàn toàn độ trễ $O(D)$ stall.
- Cơ chế ghi snapshot đĩa compact JSON không indent chỉ khi dirty.
- Tương thích ngược 100% với file JSON cũ và các caller hiện có.

---

## 3. Bottleneck: `_update_template_metrics()` là O(T) mỗi event

**Status**: ✅ RESOLVED (Đã giải quyết — xem chi tiết tại `ISSUES_FIXED.md`)  
**Severity**: High — từng tốn ~10 giờ CPU/ngày ở quy mô 10GB/ngày  
**Files liên quan**:
- `logai/storage/registries.py`
- `logai/realtime/realtime_pipeline.py`
- `tests/test_template_metrics.py`
- `tests/test_realtime_pipeline_end_to_end.py`

### Vấn đề

Trước đây, mỗi event đến, code scan toàn bộ Template Registry để đếm templates theo
service:

```python
def _update_template_metrics(self, service: str) -> None:
    count = sum(
        1 for t in self.template_registry.all_templates()
        if t.service == service
    )
    self.metrics.set_template_count(service, count)
```

`all_templates()` tạo mới toàn bộ `TemplateState` objects từ dict mỗi lần
gọi. Với vài ngàn templates × 35M events/ngày = hàng tỷ object instantiation.

### Giải pháp đã áp dụng

Chuyển sang bộ đếm incremental $O(1)$ quản lý tập trung trong `TemplateRegistry`:
1. `TemplateRegistry` duy trì map trong RAM `_counts_by_service: Dict[str, int]`, nạp 1 lần lúc start process.
2. Hàm `upsert(state)` tăng biến đếm trong $O(1)$ và trả về `is_new: bool` (chỉ tăng khi template mới xuất hiện).
3. `RealtimePipeline` loại bỏ hoàn toàn `_update_template_metrics()` khỏi hot path `_process_one()`; chỉ kích hoạt khi `is_new=True`.
4. `_update_template_metrics()` truy vấn `TemplateRegistry.count_by_service(svc)` trực tiếp trong $O(1)$.
5. Giảm độ trễ từ $625 \ \mu\text{s}$/event xuống $0.12 \ \mu\text{s}$ khi có template mới ($0 \ \mu\text{s}$ trên hot path bình thường), tăng tốc độ xử lý hơn **5.000 lần** ($5200\times$).

---

## 4. Bug: Checkpoint lưu trước khi xử lý xong batch — mất dữ liệu

**Status**: TODO  
**Severity**: Critical — có thể mất events vĩnh viễn khi crash  
**File liên quan**: `logai/collector/es_collector.py` (dòng 74-95)

### Vấn đề

Trong `poll_batch()`, checkpoint được lưu **ngay sau khi fetch** từ ES,
**trước khi** caller xử lý xong batch:

```python
def poll_batch(self) -> List[RawLog]:
    search_after = self.checkpoint.get_search_after()
    body = { ... "search_after": search_after ... }
    response = self._search(body)
    hits = response["hits"]["hits"]

    raw_logs = [_hit_to_rawlog(h, ...) for h in hits]
    last_hit = hits[-1]
    self.checkpoint.set_search_after(last_hit["sort"])   # ← LƯU NGAY
    self.checkpoint.set_last_timestamp(raw_logs[-1].timestamp)
    return raw_logs
```

Kịch bản mất dữ liệu:

```
1. poll_batch() fetch events [F, G, H, I, J]
2. checkpoint.save(sort_value_of_J)     ← ghi disk
3. _process_one(F) ✅
4. _process_one(G) ✅
5. _process_one(H) ✅
6. CRASH                                ← I, J chưa xử lý
7. Restart → load checkpoint = sau J
8. ES trả events SAU J                  ← I, J bị bỏ sót vĩnh viễn
```

Dedup index không cứu được vì nó chỉ ngăn **double-count**, không ngăn
**bỏ sót**. Checkpoint đã nhảy qua I, J.

### Hướng sửa

Tách checkpoint ra khỏi `poll_batch()`, để caller lưu **sau khi xử lý xong**:

```python
# es_collector.py — poll_batch trả thêm sort value, không tự lưu checkpoint
def poll_batch(self) -> Tuple[List[RawLog], Optional[List[Any]]]:
    search_after = self.checkpoint.get_search_after()
    body = { ... }
    if search_after:
        body["search_after"] = search_after
    response = self._search(body)
    hits = response["hits"]["hits"]
    if not hits:
        return [], None

    raw_logs = [_hit_to_rawlog(h, ...) for h in hits]
    last_sort = hits[-1]["sort"]
    # KHÔNG lưu checkpoint ở đây
    return raw_logs, last_sort

# realtime_pipeline.py — lưu checkpoint SAU khi xử lý xong batch
def run_forever(self):
    self.start_metrics_server()
    for batch, sort_value in self.collector.run_forever():
        for raw in batch:
            self._process_one(raw)
        # Chỉ advance checkpoint sau khi toàn bộ batch đã xử lý
        if sort_value is not None:
            self.checkpoint.set_search_after(sort_value)
            self.checkpoint.set_last_timestamp(batch[-1].timestamp)
        self.dedup.gc()
```

Đảm bảo: nếu crash giữa batch, restart sẽ fetch lại batch đó từ ES.
Dedup index sẽ skip các events đã xử lý, chỉ xử lý tiếp phần còn lại.

---

## 5. Training Pipeline: Stream Fetch + Parse theo từng batch để chống OOM

**Status**: ✅ RESOLVED
**Severity**: Critical — đã giảm đáng kể RAM cho raw/parsed logs; feature vectors vẫn là issue riêng
**Files liên quan**:
- `logai/collector/es_collector.py`
- `logai/training/train_pipeline.py`

### Vấn đề cũ
`fetch_historical_range()` từng gom toàn bộ `List[RawLog]` vào RAM, sau đó `_parse_all()` lại sort và tạo thêm một list `List[ParsedEvent]`.
- `raw_logs` + `parsed_events` chiếm tới ~30–50 GB RAM, chắc chắn gây crash OOM.
- Drain3 là một thuật toán online/streaming, hoàn toàn có khả năng cập nhật theo từng batch mà không cần giữ toàn bộ log thô.

### Đã triển khai
1. `stream_historical_batches()` lấy dữ liệu theo batch và không dùng realtime checkpoint.
2. Mỗi batch được đưa vào Drain3 ngay; dedup training dùng `LocalTrainingDedup` trong RAM.
3. `training_event_index.jsonl` lưu durable các trường nhẹ cần cho grouping/feature replay.
4. Cursor training chỉ commit sau khi event index của batch đã `fsync`; chỉ xóa index/checkpoint sau khi toàn bộ artifacts train thành công.
5. Khi resume, event index được replay toàn bộ để không mất event trước checkpoint.

Residual: phase feature generation vẫn giữ các sample cần train trong RAM; đây là phạm vi của Issue 7 (reservoir sampling).

---

## 6. Training Pipeline: Tối ưu cơ chế lưu `GroupRegistry` (loại bỏ duyệt lại O(N) events)

**Status**: ✅ RESOLVED (Đã giải quyết — xem chi tiết tại `ISSUES_FIXED.md`)  
**Severity**: Medium  
**File liên quan**: `logai/training/train_pipeline.py` (`_build_group_registry`)

### Vấn đề
Hiện tại `_build_group_registry()` quét lại toàn bộ danh sách `parsed_events` (O(N)) chỉ để tính:
- `event_count` của group
- `first_seen` và `last_seen`
- `service`

Điều này vừa tốn thêm một vòng lặp lớn vừa đòi hỏi phải giữ danh sách events trong RAM.

### Hướng sửa
Sau bước clustering (HDBSCAN), ta đã có ánh xạ `template_id -> group_id`. Trong khi đó, `TemplateRegistry` đã lưu đầy đủ `event_count`, `first_seen`, `last_seen`, `service` của từng template.
Ta hoàn toàn có thể tính toán trạng thái của group trực tiếp từ các template cấu thành (độ phức tạp O(T), với T là số templates ≪ N):
```python
for gid, tids in group_templates.items():
    templates = [self.template_registry.get(tid) for tid in tids]
    state = GroupState(
        group_id=gid,
        template_ids=tids,
        service=templates[0].service if templates else "unknown",
        representative_template=templates[0].template_text if templates else "",
        event_count=sum(t.event_count for t in templates),
        first_seen=min(t.first_seen for t in templates),
        last_seen=max(t.last_seen for t in templates),
    )
    self.group_registry.upsert(state, flush=False)
```
Không cần duyệt lại $N$ events, giảm độ phức tạp thời gian từ $O(N)$ xuống $O(T)$ và không cần giữ full events trong bộ nhớ.

---

## 7. Training Pipeline: Tối ưu Phase 9 (Feature Generation & Training Anomaly Model)

**Status**: 🟡 PARTIALLY RESOLVED (Bước 1 đã hoàn thành — xem chi tiết tại `ISSUES_FIXED.md`)  
**Severity**: Medium (Đã giải quyết chất lượng feature 8D và parity; Bước tiếp theo là Reservoir Sampling cho training)  
**Files liên quan**:
- `logai/features/feature_engine.py`
- `logai/models.py` (`FeatureVector`)
- `logai/training/train_pipeline.py`
- `logai/realtime/realtime_pipeline.py`
- `logai/anomaly/isolation_forest_model.py`
- `logai/config.py`
- `config.yaml`
- `tests/test_feature_vector_8d.py`
- `tests/test_end_to_end_parity.py`

### Tiến độ giải quyết & Quyết định kiến trúc

1. **Đã giải quyết ở Bước 1 (2026-09-09)**:
   - ✅ **Mở rộng FeatureVector lên 8 chiều chuẩn hóa (Dimensionless)**: Tận dụng triệt để cửa sổ 10s bắt micro-bursts (`z_score_10s`, `short_growth_rate`, `spike_ratio_10s`).
   - ✅ **Chống ô nhiễm baseline (Baseline Contamination)**: Baseline $\mu, \sigma$ được tính toán độc lập trước khi append rate mới vào history.
   - ✅ **Chuẩn hóa Burstiness**: Đổi sang hệ số biến thiên bậc hai ($CV^2 = \sigma^2/\mu^2$) trên cửa sổ 10s, scale-independent.
   - ✅ **Kích hoạt cấu hình history**: `_GroupWindow` sử dụng trực tiếp `config.rolling_window_points` thay vì hardcode 64.
   - ✅ **Cơ chế Cold-start an toàn**: Áp dụng Neutral Baseline `[0, 0, 1, 1, 0, 0, 0, 1]` cho event đầu tiên.
   - ✅ **Nâng cấp Model**: Nâng `MODEL_VERSION` lên `if-global-v2`, kiểm tra xác thực shape $(M, 8)$ và từ chối an toàn model 6D cũ.
2. **Quyết định kiến trúc xác nhận**:
   - **Giữ nguyên cơ chế Per-Event Evaluation**: Cả hai pipeline Training và Realtime duy trì đánh giá và suy luận trực tiếp trên từng event để đảm bảo phát hiện bất thường và kích hoạt cảnh báo tức thời ($<1\text{ms}$). Đảm bảo 100% Train/Serve Parity.
3. **Hạng mục tiếp theo (Bước 2)**:
   - Áp dụng **Reservoir Sampling / Subsampling** trong `train_pipeline.py` để giới hạn tập huấn luyện toàn cục ở mức tối ưu **50.000 – 100.000 vectors**, chống tràn RAM khi tập log lịch sử có hàng chục triệu events.

### Proposed design: fixed-interval evaluation 2 giây (Lưu trữ tham khảo)

Đây là thiết kế đề xuất nghiên cứu trước đây (hiện hệ thống lựa chọn giữ per-event để ưu tiên độ nhạy thời gian thực).

Tách việc nhận event khỏi việc tạo feature:

```python
feature_engine.record_event(group_id, event_timestamp)

# Scheduler/event-time clock gọi mỗi 2 giây
vectors = feature_engine.evaluate(evaluation_timestamp)
```

- `record_event()` chỉ ghi nhận timestamp vào sliding-window state.
- `evaluate()` chạy theo cadence cố định 2 giây và tạo tối đa một vector cho
  mỗi active group.
- Không aggregate event thành micro-bucket; timestamp gốc vẫn được giữ trong
  cửa sổ retention để count 10s/1m/5m chính xác.
- Training và realtime bắt buộc dùng cùng interval và cùng feature logic để
  bảo đảm train/serve parity.

Số vectors thay đổi từ số event sang số tick của active group:

```text
event-level:     M ~= N
fixed 2 seconds: M ~= sum(active_duration_per_group / 2 seconds)
```

Một group active liên tục trong 24 giờ tạo tối đa 43.200 vectors thay vì số
vector phụ thuộc log volume.

### Sliding-window state đề xuất

Giữ ba deque timestamp cho mỗi group:

```python
class GroupWindow:
    timestamps_10s: deque[float]
    timestamps_1m: deque[float]
    timestamps_5m: deque[float]
    rate_10s_history: deque[float]
    rate_1m_history: deque[float]
    last_event_timestamp: float
```

Mỗi event được append vào ba deque. Tại mỗi evaluation tick:

```python
while timestamps_10s[0] < now - 10:
    timestamps_10s.popleft()
while timestamps_1m[0] < now - 60:
    timestamps_1m.popleft()
while timestamps_5m[0] < now - 300:
    timestamps_5m.popleft()
```

Sau khi prune:

```python
count_10s = len(timestamps_10s)
count_1m = len(timestamps_1m)
count_5m = len(timestamps_5m)

rate_10s = count_10s / 10.0
rate_1m = count_1m / 60.0
rate_5m = count_5m / 300.0
```

Append/prune có chi phí O(1) amortized cho mỗi event. Không scan toàn window
và không sort lại toàn deque trong happy path.

#### Event order và late event

Thiết kế phải định nghĩa rõ event-time semantics:

- Historical Elasticsearch stream phải được sort tăng dần theo
  `(@timestamp, _id)`.
- Realtime giữ `max_seen_timestamp` và một `allowed_lateness_seconds` cấu hình
  được.
- Tick chỉ finalize khi nhỏ hơn hoặc bằng watermark:
  `max_seen_timestamp - allowed_lateness_seconds`.
- Event cũ hơn watermark phải được log/metric/DLQ theo policy; không âm thầm
  sửa một vector đã dùng để transition alert.

Nếu phase đầu chưa hỗ trợ watermark, phải xác nhận collector luôn cung cấp
event theo thứ tự và reject timestamp giảm. Không dùng `sorted(deque)` trên
hot path.

### FeatureVector v2 đề xuất

Tại evaluation time $t$:

```text
r10  = count(t - 10s,  t) / 10
r1m  = count(t - 60s,  t) / 60
r5m  = count(t - 300s, t) / 300
```

Baseline phải được tính từ **các sample trước tick hiện tại**. Chỉ append
`r10`/`r1m` vào history sau khi vector hiện tại đã được tạo.

Đề xuất vector 8 chiều, tất cả được chuẩn hóa để dùng chung trong global model:

| Feature | Công thức | Mục đích |
|---|---|---|
| `z_score_10s` | `(r10 - mu10) / (sigma10 + eps)` | Burst ngắn so với baseline 10s |
| `z_score_1m` | `(r1m - mu1m) / (sigma1m + eps)` | Lệch trung hạn |
| `short_growth_rate` | `r10 / (r1m + eps)` | 10s hiện tại so với 1 phút |
| `growth_rate` | `r1m / (r5m + eps)` | 1 phút so với 5 phút |
| `burstiness_10s` | `sigma10^2 / (mu10^2 + eps)` | Relative variance (CV squared) |
| `rate_delta_norm` | `(r1m - r5m) / (sigma1m + eps)` | Rate delta chuẩn hóa |
| `slope_norm` | `slope(r1m vs seconds) * 60 / (mu1m + eps)` | Relative trend mỗi phút |
| `spike_ratio_10s` | `max(recent_r10) / (mu10 + eps)` | Đỉnh 10s so với baseline |

Do các cửa sổ lồng nhau, `short_growth_rate` có giới hạn lý thuyết gần 6 và
`growth_rate` gần 5 khi denominator có dữ liệu. Đây là feature dimensionless,
không phụ thuộc service có volume thấp hay cao.

#### Cold-start neutral baseline

Trước khi đủ lịch sử tối thiểu, trả neutral values:

```text
z_score_10s       = 0
z_score_1m        = 0
short_growth_rate = 1
growth_rate       = 1
burstiness_10s    = 0
rate_delta_norm   = 0
slope_norm        = 0
spike_ratio_10s   = 1
```

Với interval 2 giây, `min_baseline_points=30` tương ứng khoảng 60 giây. Cần
đánh giá lại trên dữ liệu thật; 60 giây có thể quá ngắn với workload có chu kỳ
dài.

#### Numerical guards và clipping

Volume thấp hoặc baseline bằng 0 có thể tạo ratio rất lớn. Implementation phải:

- Dùng `eps` cho denominator.
- Trả neutral value nếu chưa đủ baseline hoặc cả numerator/denominator bằng 0.
- Clip trước khi đưa vào model, giá trị khởi điểm để benchmark:

```text
z_score_*        in [-10, 10]
short_growth     in [0, 6]
growth_rate      in [0, 5]
burstiness_10s   in [0, 20]
rate_delta_norm  in [-10, 10]
slope_norm       in [-10, 10]
spike_ratio_10s  in [0, 20]
```

Các cap này là proposed defaults, phải tune bằng distribution thật và không
được coi là business thresholds cố định.

### Zero interval và active-group lifecycle

Group không có event tại một tick vẫn cần sample count bằng 0 để rolling rate
giảm đúng và alert có thể chuyển sang COOLING/NORMAL.

Không evaluate group vô hạn sau khi ngừng phát log:

1. Tiếp tục evaluate trong `history_retention_seconds`, mặc định 5 phút hoặc
   giá trị mới thống nhất với longest feature window.
2. Khi ba timestamp deque rỗng và alert đã NORMAL, loại group khỏi active set.
3. Event mới sau đó kích hoạt lại group với cold-start hoặc restored state tùy
   policy persistence.

### Reservoir sampling cho model training

Fixed interval giảm số vectors nhưng vẫn có thể tạo hàng triệu vectors khi có
nhiều group active. Không giữ toàn bộ vectors trong `all_feature_vectors`.

Áp dụng Algorithm R với reservoir size cấu hình được, ví dụ 100.000:

```python
seen += 1
if len(reservoir) < max_size:
    reservoir.append(vector)
else:
    j = random.randrange(seen)
    if j < max_size:
        reservoir[j] = vector
```

Memory training trở thành O(S), với S là reservoir size. Cần cân nhắc stratified
reservoir theo group/service nếu global reservoir vẫn bị group active lâu chiếm
đa số sample.

### Quan hệ với Issue 5 (streaming historical training)

Issue 5 và Issue 7 phải phối hợp nhưng giải quyết hai lớp khác nhau:

- Issue 5 giảm `RawLog`/`ParsedEvent` memory bằng batch streaming và event index
  trên disk.
- Issue 7 giảm số feature calculations và feature-vector memory bằng fixed
  interval + reservoir sampling.

Bounded-memory training hoàn chỉnh:

```text
RAM = O(batch_size + template_count + active_window_events + reservoir_size)
Disk temp = O(event_count) cho lightweight event index
```

Chỉ sửa một trong hai issue chưa đủ để tuyên bố toàn training pipeline không
OOM.

### Tác động API và model compatibility

1. `FeatureEngine.update() -> FeatureVector` không còn phù hợp. Nên tách thành:
   - `record_event(group_id, timestamp) -> None`
   - `evaluate(timestamp) -> List[FeatureVector]`
   - `flush_until(timestamp) -> List[FeatureVector]` cho training/end-of-stream.
2. `FeatureVector` đổi từ 6 thành 8 chiều; thứ tự `as_vector()` và
   `feature_names()` phải cập nhật đồng thời.
3. Model `global.pkl` 6 chiều hiện tại không tương thích. Phải retrain và bump
   `MODEL_VERSION` từ `if-global-v1` lên `if-global-v2`.
4. Model artifact nên lưu metadata `feature_names`, `feature_schema_version`,
   `evaluation_interval_seconds`; startup phải fail rõ ràng nếu schema mismatch.
5. `min_training_samples` chuyển nghĩa từ số events sang số interval vectors.
6. Alert transition chạy mỗi 2 giây, nên consecutive thresholds đổi nghĩa:
   - `warm_consecutive=2` tương ứng khoảng 4 giây.
   - `alert_consecutive=3` tương ứng khoảng 6 giây.
   - `cool_consecutive=3` tương ứng khoảng 6 giây.
   Các threshold phải tune lại theo detection latency mong muốn.

### Cấu hình đề xuất

```yaml
features:
  evaluation_interval_seconds: 2
  allowed_lateness_seconds: 5
  min_baseline_points: 30
  baseline_history_seconds: 300
  history_retention_seconds: 300
  emit_zero_intervals: true

training:
  feature_reservoir_size: 100000
  reservoir_random_seed: 42
```

`windows_seconds` vẫn là `[10, 60, 300]`. Validation phải bảo đảm interval > 0
và nhỏ hơn hoặc bằng shortest window.

### Files và thay đổi dự kiến

| File | Thay đổi |
|---|---|
| `logai/features/feature_engine.py` | Tách record/evaluate, ba sliding queues, fixed cadence, zero intervals, baseline-before-current, FeatureVector v2 |
| `logai/models.py` | Đổi schema `FeatureVector` từ 6 sang 8 fields và cập nhật vector order |
| `logai/training/train_pipeline.py` | Replay event stream vào engine, evaluate theo event-time ticks, reservoir sampling, flush end-of-stream |
| `logai/realtime/realtime_pipeline.py` | Record mỗi event; predict/alert chỉ khi interval vector được finalize |
| `logai/anomaly/isolation_forest_model.py` | Model version/schema metadata và validation 8 dimensions |
| `logai/config.py` | Thêm cadence, lateness, baseline và training reservoir config |
| `config.yaml` | Khai báo defaults mới |
| `ARCHITECTURE.md` | Cập nhật feature schema, training/realtime pipeline và alert semantics |
| `README.md` | Cập nhật latency/feature summary nếu behavior public thay đổi |
| `ISSUES_FIXED.md` | Ghi implementation/benchmark sau khi hoàn tất |

### Test plan bắt buộc

Unit tests:

1. Count chính xác tại boundary `[t-10, t]`, `[t-60, t]`, `[t-300, t]`.
2. Nhiều event trong cùng 2 giây chỉ tạo một vector.
3. Tick không event tạo zero sample và làm rate giảm đúng.
4. Active group được evict sau retention khi alert NORMAL.
5. Cold-start trả đúng neutral vector.
6. Baseline không chứa sample hiện tại.
7. Burst 10 giây làm `z_score_10s`, `short_growth_rate`,
   `burstiness_10s`, `spike_ratio_10s` tăng.
8. Steady rate ở service volume thấp/cao tạo feature gần tương đương.
9. Late event trước/sau watermark tuân theo policy.
10. Feature order/schema mismatch bị model từ chối rõ ràng.
11. Reservoir không vượt max size và tái lập với random seed.

Integration tests:

1. Training và realtime tạo cùng vector cho cùng event stream.
2. So sánh event-level v1 và interval v2 trên steady, gradual growth, short
   burst, long burst và idle/recovery scenarios.
3. Model v2 bắt được group chưa xuất hiện trong training.
4. Alert detection delay đúng với interval/consecutive config.
5. Peak RAM không tăng theo tổng số historical feature vectors.

### Acceptance criteria

Chỉ đánh dấu RESOLVED khi đạt đồng thời:

- Không còn tạo một vector cho mỗi event.
- Không còn scan tuyến tính toàn timestamp window tại mỗi evaluation.
- Training không giữ toàn bộ feature vectors trong RAM.
- Train/serve feature parity được test.
- Model artifact từ chối schema/version không tương thích.
- Detection recall trên tập anomaly chuẩn không thấp hơn ngưỡng đã thống nhất.
- False-positive rate không tăng vượt tolerance đã thống nhất.
- Detection delay P95 phù hợp interval 2 giây và alert thresholds.
- Benchmark ghi rõ CPU, peak RAM, vectors generated và model fit time so với v1.

### Quyết định cần chốt trước implementation

1. Dùng global reservoir hay stratified reservoir theo group/service.
2. Baseline history 5 phút có đủ cho workload theo giờ/ngày hay không.
3. Allowed lateness và policy cho event quá trễ.
4. Persist feature windows qua restart hay chấp nhận cold-start lại.
5. Các clipping caps dựa trên phân phối dữ liệu thật.
6. Alert latency mục tiêu để tune consecutive thresholds.
