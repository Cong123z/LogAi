# TASK: ES Error Handling — Sửa 4 kịch bản lỗi Elasticsearch

**Ngày tạo**: 2026-09-10  
**Trạng thái**: ✅ HOÀN THÀNH — 110/110 test pass  
**Ảnh hưởng**: `es_collector.py`, `retry.py`, `realtime_pipeline.py`, `prometheus_exporter.py`

> **Ghi chú triển khai**:
> - Fix 2c dùng phương án instance-method cho `_search` (đọc `_ES_NON_RETRYABLE`
>   ở runtime → testable qua patch module-global); giữ `retry_with_backoff`
>   decorator cho caller khác.
> - Fix 4b: wiring hooks đặt **sau** khi `self.metrics` được tạo (không thể đặt
>   ngay sau collector vì metrics chưa tồn tại lúc đó).
> - Fix 3 xóa contract generator của collector → 19 test legacy (mock
>   `collector.run_forever`) được **port** sang mock `poll_batch` + sentinel
>   `_StopLoop`, giữ nguyên mọi assertion. `ElasticsearchCollector.run_forever`
>   giờ là dead code, giữ lại cho tương thích.

---

## Bối cảnh

Phân tích code hiện tại phát hiện 4 kịch bản lỗi Elasticsearch mà hệ thống
**không xử lý hoặc xử lý sai**, gây crash loop, retry vô nghĩa, hoặc mất
toàn bộ batch dữ liệu hợp lệ:

1. Một malformed hit nằm giữa batch → crash toàn batch
2. ES 400/401/403/404 → retry 5 lần vô ích rồi crash
3. ES timeout nhiều lần → 181 giây chờ rồi process crash
4. ES unavailable rồi phục hồi → crash loop qua Docker restart

---

## Fix 1: Malformed hit isolation

**File**: `logai/collector/es_collector.py`  
**Mức độ**: 🔴 Critical — 1 hit bẩn giết 499 hit tốt + gây infinite crash loop

### Vấn đề gốc

`_hit_to_rawlog()` (L34-47) truy cập `hit["_source"]` và `hit["_id"]` trực
tiếp không có try/catch. `poll_batch()` (L94) và `stream_historical_batches()`
(L147) dùng list comprehension — 1 exception = toàn bộ batch mất. Retry gọi
lại ES trả y hệt batch đó → 5 lần crash cùng chỗ → process crash → Docker
restart → gặp lại hit bẩn → infinite crash loop.

### Hướng sửa

#### 1a. `_hit_to_rawlog()` — thêm validation rõ ràng

Thay `hit["_source"]` bằng `hit.get("_source")` + kiểm tra `isinstance(src, dict)`.
Thay `hit["_id"]` bằng `hit.get("_id")` + kiểm tra truthy.
Raise `ValueError` với message mô tả cụ thể thay vì để `KeyError` mơ hồ.

```python
def _hit_to_rawlog(hit: Dict[str, Any], index: str) -> RawLog:
    src = hit.get("_source")
    if not isinstance(src, dict):
        raise ValueError(
            f"Hit missing or invalid _source (got {type(src).__name__}): "
            f"_id={hit.get('_id', '<no_id>')}"
        )
    hit_id = hit.get("_id")
    if not hit_id:
        raise ValueError(f"Hit missing _id, _source keys: {list(src.keys())[:5]}")

    ts_raw = src.get("@timestamp")
    ts = _parse_timestamp(ts_raw)
    return RawLog(
        timestamp=ts,
        service=src.get("service", "unknown"),
        level=src.get("level", "INFO"),
        message=src.get("message", ""),
        metadata={
            k: v for k, v in src.items()
            if k not in ("@timestamp", "service", "level", "message")
        },
        event_id=hit_id,
        es_index=index,
        es_doc_id=hit_id,
    )
```

#### 1b. Thêm `_safe_hits_to_rawlogs()` — cô lập lỗi từng hit

Thêm hàm mới ngay sau `_hit_to_rawlog()`:

```python
def _safe_hits_to_rawlogs(
    hits: List[Dict[str, Any]],
    index: str,
    malformed_counter: Optional[Any] = None,
) -> List[RawLog]:
    """Convert hits to RawLog, skipping malformed ones individually.

    A single bad hit must never crash the entire batch.
    """
    raw_logs: List[RawLog] = []
    for hit in hits:
        try:
            raw_logs.append(_hit_to_rawlog(hit, index))
        except Exception as exc:  # noqa: BLE001
            hit_id = "<unknown>"
            try:
                hit_id = (
                    hit.get("_id", "<no_id>")
                    if isinstance(hit, dict)
                    else repr(hit)[:80]
                )
            except Exception:
                pass
            logger.warning("Skipping malformed ES hit %s: %s", hit_id, exc)
            if malformed_counter is not None:
                malformed_counter.inc()
    return raw_logs
```

#### 1c. `poll_batch()` — dùng `_safe_hits_to_rawlogs` + defensive sort

Thay dòng 94:
```python
# TRƯỚC:
raw_logs = [_hit_to_rawlog(h, self.config.index) for h in hits]
return raw_logs, hits[-1]["sort"]

# SAU:
raw_logs = _safe_hits_to_rawlogs(hits, self.config.index, self._malformed_counter)

last_sort = hits[-1].get("sort")
if last_sort is None:
    logger.error(
        "Last hit in batch missing 'sort' field; cannot advance cursor. "
        "Batch had %d hits, %d parsed successfully.",
        len(hits), len(raw_logs),
    )
    return raw_logs, None

return raw_logs, last_sort
```

#### 1d. `stream_historical_batches()` — cùng pattern

Thay dòng 147:
```python
# TRƯỚC:
batch = [_hit_to_rawlog(h, self.config.index) for h in hits]
total_fetched += len(batch)
search_after = hits[-1]["sort"]

# SAU:
batch = _safe_hits_to_rawlogs(hits, self.config.index, self._malformed_counter)
total_fetched += len(batch)
last_sort = hits[-1].get("sort")
if last_sort is None:
    logger.error("Historical hit missing 'sort'; stopping stream.")
    break
search_after = last_sort
```

#### 1e. Thêm attribute `_malformed_counter` vào `ElasticsearchCollector.__init__`

```python
def __init__(self, config: ElasticsearchConfig, checkpoint: CheckpointStore):
    self.config = config
    self.checkpoint = checkpoint
    self.client = _build_client(config)
    self._malformed_counter = None   # inject bởi pipeline cho Prometheus metric
```

---

## Fix 2: Retry phân loại exception

**Files**: `logai/reliability/retry.py`, `logai/collector/es_collector.py`  
**Mức độ**: 🔴 Critical — retry 400/401/403/404 chờ 31 giây vô ích

### Vấn đề gốc

`@retry_with_backoff(exceptions=(Exception,))` bắt mọi exception, không phân
biệt retryable hay non-retryable. ES Python client 8.x có exception hierarchy
rõ ràng:

| Exception | HTTP | Retryable? |
|-----------|:----:|:----------:|
| `ConnectionError` | — | ✅ Có |
| `ConnectionTimeout` | — | ✅ Có |
| `ApiError(429)` | 429 | ✅ Có |
| `ApiError(5xx)` | 5xx | ✅ Có |
| `BadRequestError` | 400 | ❌ Không |
| `AuthenticationException` | 401 | ❌ Không |
| `AuthorizationException` | 403 | ❌ Không |
| `NotFoundError` | 404 | ❌ Không |

### Hướng sửa

#### 2a. Nâng cấp `retry_with_backoff()` — backward-compatible

Thêm 2 tham số mới (default rỗng = behavior cũ 100%):

```python
def retry_with_backoff(
    max_retries: int = 5,
    base_seconds: float = 1.0,
    max_seconds: float = 60.0,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    non_retryable_exceptions: Tuple[Type[BaseException], ...] = (),
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
):
    """Exponential backoff retry.

    Parameters
    ----------
    non_retryable_exceptions:
        Exception types raised immediately without retry, even if they
        match ``exceptions``. Checked first via isinstance.
    on_retry:
        Optional callback ``(attempt, exc) -> None`` invoked before each
        retry sleep. Use for metrics/observability.
    """
    def decorator(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except non_retryable_exceptions:
                    raise  # fail-fast
                except exceptions as exc:  # noqa: BLE001
                    attempt += 1
                    if attempt > max_retries:
                        logger.error(
                            "%s failed after %d attempts: %s",
                            fn.__name__, attempt - 1, exc,
                        )
                        raise
                    delay = min(base_seconds * (2 ** (attempt - 1)), max_seconds)
                    delay += random.uniform(0, delay * 0.1)
                    logger.warning(
                        "%s attempt %d/%d failed (%s), retrying in %.2fs",
                        fn.__name__, attempt, max_retries, exc, delay,
                    )
                    if on_retry is not None:
                        on_retry(attempt, exc)
                    time.sleep(delay)
        return wrapper
    return decorator
```

#### 2b. `_search()` — áp dụng phân loại

```python
from elasticsearch import (
    AuthenticationException,
    AuthorizationException,
    BadRequestError,
    NotFoundError,
)

_ES_NON_RETRYABLE = (
    AuthenticationException,
    AuthorizationException,
    BadRequestError,
    NotFoundError,
)

class ElasticsearchCollector:
    ...

    @retry_with_backoff(
        exceptions=(Exception,),
        non_retryable_exceptions=_ES_NON_RETRYABLE,
    )
    def _search(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.client.search(index=self.config.index, body=body)
```

#### 2c. Hook `on_retry` — inject runtime từ pipeline

Decorator level không có access vào instance metrics, nên dùng instance-level
hook. Thêm vào `ElasticsearchCollector`:

```python
def __init__(self, config, checkpoint):
    ...
    self._on_retry_hook: Optional[Callable[[int, BaseException], None]] = None
```

Và trong `_search`, thay decorator bằng instance method để có quyền truy cập
`self._on_retry_hook`:

```python
def _search(self, body: Dict[str, Any]) -> Dict[str, Any]:
    attempt = 0
    while True:
        try:
            return self.client.search(index=self.config.index, body=body)
        except _ES_NON_RETRYABLE:
            raise
        except Exception as exc:
            attempt += 1
            if attempt > 5:
                logger.error("_search failed after %d attempts: %s", attempt - 1, exc)
                raise
            delay = min(1.0 * (2 ** (attempt - 1)), 60.0)
            delay += random.uniform(0, delay * 0.1)
            logger.warning(
                "_search attempt %d/5 failed (%s), retrying in %.2fs",
                attempt, exc, delay,
            )
            if self._on_retry_hook is not None:
                self._on_retry_hook(attempt, exc)
            time.sleep(delay)
```

> **Ghi chú**: Giữ `retry_with_backoff` decorator cho các caller khác trong
> tương lai; `_search` chuyển sang instance method vì cần `self._on_retry_hook`.
> Hai cách tiếp cận cùng tồn tại, không xung đột.

---

## Fix 3: Graceful ES unavailable

**File**: `logai/realtime/realtime_pipeline.py`  
**Mức độ**: 🟡 High — crash loop khi ES down, mất Prometheus state

### Vấn đề gốc

`run_forever()` (L87-116) lặp qua `self.collector.run_forever()` generator.
Khi `_search()` throw exception (sau khi hết retry), exception nổ qua
generator → `run_forever()` crash → process exit → Docker restart → retry ngay
→ crash lại. Không có backoff ở vòng ngoài.

Exception xảy ra NGOÀI `_process_one()`, nên không có DLQ fallback.

### Hướng sửa

Tái cấu trúc `run_forever()` — không dùng generator của collector nữa, gọi
trực tiếp `poll_batch()` trong vòng while, bọc try/catch:

```python
def run_forever(self) -> None:
    self.start_metrics_server()
    logger.info("Realtime pipeline started, polling Elasticsearch...")
    consecutive_poll_failures = 0
    MAX_POLL_BACKOFF = 300.0  # 5 phút

    while True:
        # ── Phase 1: Poll ES ──────────────────────────────────
        try:
            batch, cursor = self.collector.poll_batch()
            consecutive_poll_failures = 0
        except Exception as exc:
            consecutive_poll_failures += 1
            backoff = min(
                self.config.elasticsearch.poll_interval_seconds
                * (2 ** consecutive_poll_failures),
                MAX_POLL_BACKOFF,
            )
            logger.error(
                "ES poll failed (%d consecutive): %s. Retrying in %.1fs...",
                consecutive_poll_failures, exc, backoff,
            )
            self.metrics.logai_es_poll_errors_total.inc()
            time.sleep(backoff)
            continue

        if not batch:
            time.sleep(self.config.elasticsearch.poll_interval_seconds)
            continue

        # ── Phase 2: Process batch (logic cũ giữ nguyên) ──────
        self.metrics.logai_queue_depth.set(len(batch))
        try:
            for raw in batch:
                self.metrics.logai_events_received_total.inc()
                if not self._process_one(raw):
                    raise RuntimeError(
                        f"Event {raw.event_id} did not reach a terminal state"
                    )

            self.template_registry.flush()
            self.group_registry.flush()
            self.dedup.gc()

            if batch and cursor is not None:
                self.checkpoint.commit(cursor, batch[-1].timestamp)
        finally:
            self.metrics.logai_queue_depth.set(0)
```

### Hành vi mới khi ES down

| Lần fail | Backoff (poll_interval=5s) | Hành vi |
|:--------:|:--------------------------:|---------|
| 1 | 10s | log error, sleep, retry |
| 2 | 20s | log error, sleep, retry |
| 3 | 40s | log error, sleep, retry |
| 4 | 80s | log error, sleep, retry |
| 5 | 160s | log error, sleep, retry |
| 6+ | 300s (cap) | log error, sleep, retry |
| ES up | — | `consecutive = 0`, resume ngay |

**Process KHÔNG crash, KHÔNG mất Prometheus metrics state, KHÔNG restart.**

---

## Fix 4: Metrics wiring

**File**: `logai/metrics/prometheus_exporter.py`, `logai/realtime/realtime_pipeline.py`  
**Mức độ**: 🟡 Medium

### Vấn đề gốc

- `logai_retry_total` đã khai báo nhưng chưa được nối vào retry helper → luôn = 0.
- Không có metric cho ES poll errors hoặc malformed hits.

### Hướng sửa

#### 4a. Thêm 2 Counter mới vào `MetricsExporter.__init__()`

Thêm sau `logai_queue_depth` (sau dòng 74):

```python
self.logai_es_poll_errors_total = Counter(
    "logai_es_poll_errors_total",
    "ES poll failures (transport errors, auth errors, etc.)",
)
self.logai_es_malformed_hits_total = Counter(
    "logai_es_malformed_hits_total",
    "ES hits skipped due to missing/invalid _source or _id",
)
```

#### 4b. Inject hooks trong `RealtimePipeline.__init__()`

Thêm sau dòng tạo collector (sau dòng 42):

```python
# Wire metrics hooks vào collector
self.collector._on_retry_hook = lambda attempt, exc: self.metrics.logai_retry_total.inc()
self.collector._malformed_counter = self.metrics.logai_es_malformed_hits_total
```

---

## Ma trận file thay đổi

| File | Thay đổi | Fix |
|------|----------|-----|
| `logai/reliability/retry.py` | Thêm `non_retryable_exceptions`, `on_retry` params | 2a |
| `logai/collector/es_collector.py` | `_hit_to_rawlog` defensive, thêm `_safe_hits_to_rawlogs`, `poll_batch` + `stream_historical_batches` dùng safe converter, `_search` phân loại, thêm `_malformed_counter` + `_on_retry_hook` attrs | 1a–1e, 2b–2c |
| `logai/realtime/realtime_pipeline.py` | `run_forever` bọc poll errors + backoff, inject hooks | 3, 4b |
| `logai/metrics/prometheus_exporter.py` | Thêm `logai_es_poll_errors_total`, `logai_es_malformed_hits_total` | 4a |

---

## Test plan

### Malformed hit tests

| # | Test | Input | Expected |
|---|------|-------|----------|
| 1 | `test_malformed_hit_skipped_batch_continues` | 10 hits, hit #5 thiếu `_source` | 9 RawLog, 1 skip, log warning |
| 2 | `test_missing_id_hit_skipped` | Hit có `_source` nhưng thiếu `_id` | skip, log warning |
| 3 | `test_source_not_dict_skipped` | `_source: "string"` | skip, log warning |
| 4 | `test_missing_sort_on_last_hit` | Hit cuối thiếu `sort` | `cursor = None` |
| 5 | `test_all_hits_malformed_returns_empty` | 5 hits đều lỗi | `([], last_sort)` |
| 6 | `test_malformed_hit_metric_incremented` | 3 hit lỗi | counter += 3 |

### Retry classification tests

| # | Test | Input | Expected |
|---|------|-------|----------|
| 7 | `test_400_bad_request_no_retry` | `BadRequestError` | raise ngay, 0 sleep |
| 8 | `test_401_auth_no_retry` | `AuthenticationException` | raise ngay |
| 9 | `test_403_forbidden_no_retry` | `AuthorizationException` | raise ngay |
| 10 | `test_404_not_found_no_retry` | `NotFoundError` | raise ngay |
| 11 | `test_connection_error_retries` | `ConnectionError` | retry 5 lần |
| 12 | `test_timeout_retries` | `ConnectionTimeout` | retry 5 lần |
| 13 | `test_retry_metric_incremented` | 3 retries | `logai_retry_total` += 3 |

### Graceful ES unavailable tests

| # | Test | Input | Expected |
|---|------|-------|----------|
| 14 | `test_run_forever_es_unavailable_no_crash` | `poll_batch` throws | pipeline không crash, sleep + retry |
| 15 | `test_run_forever_backoff_increases` | 3 lần fail liên tiếp | backoff: 10s → 20s → 40s |
| 16 | `test_run_forever_recovery_resets_backoff` | fail 3 lần → success | `consecutive = 0` |
| 17 | `test_run_forever_backoff_caps_at_max` | 10 lần fail | backoff ≤ 300s |
| 18 | `test_poll_error_metric_incremented` | 2 lần fail | counter += 2 |
| 19 | `test_checkpoint_not_advanced_on_poll_failure` | poll fail | checkpoint unchanged |

---

# TRIỂN KHAI DOCKER — Nhật ký tiến trình (2026-09-10)

Mục tiêu: deploy engine **song song** với stack đang chạy (Elasticsearch +
generator log + Prometheus) trên network external `aiops-net`, chạy training
rồi chuyển sang realtime.

## Bối cảnh hạ tầng đang chạy

| Container | Image | Vai trò |
|---|---|---|
| `elasticsearch` | elasticsearch:8.12.2 :9200 | ES chung (network `aiops-net`) |
| `hdfs_log_generator` | FastAPI :8000 | Sinh log → index **`hdfs-logs`** (UI tại http://localhost:8000) |
| `prometheus` | prom/prometheus :9090 | Scrape metrics |

Generator ghi `_source`: `@timestamp`, `service="hdfs"`, `level`, `module`,
`pid`, `block_id`, `message`, `raw_log`. Khớp `_hit_to_rawlog` của engine
(4 field bắt buộc có đủ; field thừa → `metadata`). **Không cần sửa generator.**
Cả 2 chế độ ingest (realtime-stream & fast-bulk) đều dùng `datetime.now()` →
timestamp hiện tại (2026), KHÔNG phải 2008.

## Thay đổi cấu hình đã thực hiện

1. **`config.yaml`**: `elasticsearch.index` đổi `app-logs-*` → **`hdfs-logs`**
   (index không override được bằng env, chỉ hosts/user/password/metrics_port).
2. **`docker-compose.yml`** (bản gốc): thêm volume `hf-cache:/root/.cache/huggingface`
   để cache model embedding (~90MB), khỏi tải lại mỗi lần recreate.
3. **`docker-compose.reuse.yml`** (MỚI): compose chỉ chạy `logai-engine`
   (realtime), `logai-training` (profile `training`, one-off), `grafana`
   (profile `ui`); gắn network **external `aiops-net`**, KHÔNG dựng lại
   ES/Prometheus. Volume đặt `name:` tường minh (`logai-data`, `hf-cache`) để
   dùng chung giữa job training và service realtime.
4. **Prometheus** (`~/prometheus/prometheus.yml`): target đổi
   `host.docker.internal:9108` → **`logai-engine:9108`**; đã `docker network
   connect aiops-net prometheus` + restart để nạp. ⚠️ Kết nối mạng thủ công
   này MẤT nếu prometheus bị recreate — xem "Việc còn lại" #2.
5. **Elasticsearch cluster setting**: đã bật
   `PUT _cluster/settings {"persistent":{"indices.id_field_data.enabled":true}}`
   — xem lý do ở "Sự cố đã xử lý".

## Sự cố đã xử lý trong lúc training

**Lỗi**: training fail exit 1 với
`BadRequestError(400): Fielddata access on the _id field is disallowed`.
**Gốc rễ**: engine phân trang `search_after` sort `[{"@timestamp":"asc"},
{"_id":"asc"}]` (`es_collector.py:182` và `:248`); ES 8.x mặc định cấm
fielddata trên `_id`. (Fix 2 chạy đúng: `BadRequestError` là non-retryable →
fail-fast, không retry vô ích.)
**Đã xử lý (cách A, nhanh)**: bật `indices.id_field_data.enabled=true` trên ES.
**Cách B (chuẩn hơn, CHƯA làm — TODO)**: đổi tiebreaker sort khỏi `_id` (PIT +
`_shard_doc`, hoặc thêm field keyword id vào doc). To hơn vì ảnh hưởng
checkpoint `search_after` + dedup. Nếu triển khai production nên làm cách B.

## ✅ Training ĐÃ HOÀN TẤT (exit 0)

- Dữ liệu: `hdfs-logs` = 180.000 log, timestamp 2026-09-10 (trong lookback 24h).
- Kết quả: **30 templates, 11 groups**, Isolation Forest train trên 180.000 mẫu.
- Artifacts ghi vào volume **`logai-data`** (`/app/data` trong container):
  `template_registry.json`, `template_embeddings.pkl`, `group_registry.json`,
  `group_centroids.pkl`, `drain3_state.bin`, `training_checkpoint.json`,
  `doc_embeddings.pkl`, và model trong `data/models/`.

## ▶️ BƯỚC TIẾP THEO — Chạy realtime

Realtime dùng lại templates/groups/model từ `logai-data` (cùng volume), chỉ cần
KHÔNG xóa volume. Checkpoint realtime (`checkpoint.json`) tách biệt với training
→ lần đầu đọc `hdfs-logs` từ log cũ nhất tiến dần, xử lý bằng model đã train.

```bash
cd ~/Documents/logai-engine
docker compose -f docker-compose.reuse.yml up -d logai-engine

# xác nhận nạp lại registries/model (không có bước "Training..."):
docker logs logai-engine 2>&1 | grep -iE "load|registr|model|template|group|polling"

# kiểm tra metrics + prometheus target:
curl http://localhost:9108/metrics | grep logai_events_received
curl -s http://localhost:9090/api/v1/targets | grep -o '"health":"[^"]*"'   # kỳ vọng "up"
```

## Việc còn lại (TODO)

1. **Fix B** cho sort `_id` (bỏ phụ thuộc `id_field_data` — xem trên) nếu lên
   production, để không phải bật setting tốn RAM trên ES.
2. **Prometheus + `aiops-net` bền vững**: thêm `aiops-net` (external) vào
   compose gốc của prometheus (`~/prometheus/...`) thay cho `docker network
   connect` thủ công.
3. (Tùy chọn) Grafana dashboard: `docker compose -f docker-compose.reuse.yml
   --profile ui up -d grafana` (http://localhost:3000, admin/admin).
4. **Fix Drain3 Generalized Template Persistence (Lưu template `<*>` chuẩn)**:
   - **Vấn đề**: `template_registry.json` và `group_registry.json` (`representative_template`) hiện lưu chuỗi log thô nguyên bản của event đầu tiên (chứa tham số cụ thể: IP, Block ID, timestamp, size...), thay vì lưu template tổng quát hóa có wildcard `<*>` do Drain3 khai phá.
   - **Gốc rễ**: Trong `logai/training/train_pipeline.py`, hàm `_rebuild_template_registry()` đọc từ `event_index` và chỉ gán `template_text = record.get("template_text")` ở bản ghi đầu tiên khi `state is None`. Tại thời điểm event đầu tiên tạo cluster, Drain3 chưa có mẫu thứ 2 để thay thế token biến thiên thành `<*>`. Khi Drain3 học thêm hàng chục ngàn mẫu và cập nhật `<*>` nội bộ (lưu trong `drain3_state.bin`), pipeline không hỏi lại Drain3 miner mà ghi nguyên chuỗi thô ban đầu ra JSON.
   - **Giải pháp**:
     - Trong `_rebuild_template_registry()`: Lấy `template_text` chính thức từ cluster của Drain3:
       ```python
       cluster_id = int(template_id[1:])  # T00001 -> 1
       cluster = self.parser.miner.drain.id_to_cluster.get(cluster_id)
       if cluster:
           state.template_text = cluster.get_template()
       ```
     - Trong `realtime_pipeline.py`: Khi Drain3 cập nhật template của cluster (`change_type == "cluster_template_changed"`), đồng bộ lại `template_text` mới vào `TemplateRegistry`.
   - **Mức độ**: 🟡 Medium — Ảnh hưởng trực quan hiển thị và độ sạch của vector embedding SentenceTransformer (loại bỏ nhiễu do IP/ID cụ thể gây ra).
5. **Fix Stuck Alert via Periodic Sliding Window Tick (Cập nhật cửa sổ trượt định kỳ khi zero-event)**:
   - **Files ảnh hưởng**: `logai/realtime/realtime_pipeline.py`, `logai/features/feature_engine.py`, `logai/alert/alert_state_machine.py`
   - **Mức độ**: 🔴 High — Gây kẹt cảnh báo giả (Stuck False Alert) vĩnh viễn sau khi hệ thống phục hồi do thiếu cơ chế cập nhật định kỳ.
   - **Bối cảnh & Vấn đề gốc**:
     - Hiện tại pipeline đánh giá anomaly và alert hoàn toàn theo cơ chế hướng sự kiện per-event (`_process_one` $\rightarrow$ `_run_anomaly_and_alert`).
     - Khi sự cố kết thúc (hệ thống phục hồi), các nhóm log lỗi (`G0001`, `G0002`, `G_SINGLE_0003`, `G_SINGLE_0004`, `G_SINGLE_0005`) **ngừng sinh log hoàn toàn (tần suất = 0, zero-event)**.
     - Vì không có event mới nào thuộc các nhóm lỗi đi qua pipeline, hàm tính điểm `_run_anomaly_and_alert` không bao giờ được gọi lại cho các nhóm này.
     - Cửa sổ trượt (sliding window) không được nạp giá trị rate = 0, điểm `log_anomaly_score` không được tính lại, và Gauge `log_alert_state{state="ALERTING"}` trong Prometheus bị kẹt vĩnh viễn ở mức `1.0` (Stale / Sticky Gauge).
   - **Hướng sửa chi tiết**:
     1. **Tận dụng `FeatureEngine.snapshot()` có sẵn**:
        - Trong `logai/features/feature_engine.py` (dòng 61) đã có sẵn hàm `snapshot(group_id, timestamp)`:
          ```python
          def snapshot(self, group_id: str, timestamp: float) -> FeatureVector:
              """Compute the current feature vector without adding a new event -
              useful for periodic re-evaluation of idle groups."""
              gw = self._windows[group_id]
              self._prune(gw, timestamp)
              return self._compute(group_id, gw, timestamp)
          ```
          Hàm này sẽ tự động loại bỏ (`_prune`) các timestamp cũ đã trôi qua khỏi khoảng retention (10s, 60s, 300s) và tính lại rate về 0 mà không cần thêm event mới.
     2. **Thêm cơ chế Periodic Tick trong `RealtimePipeline`**:
        - Thêm hàm `_evaluate_idle_alerting_groups(current_timestamp: float)`:
          - Duyệt qua các nhóm trong `self.group_registry` đang có trạng thái khác `NORMAL` (tức là đang ở `ALERTING`, `WARMING`, hoặc `COOLING`).
          - Nếu nhóm đó không nhận log mới trong vòng $\ge 5$ giây (`current_timestamp - last_seen >= 5.0`):
            - Gọi `feature_vector = self.feature_engine.snapshot(group_id, current_timestamp)`.
            - Đánh giá lại: `self._run_anomaly_and_alert(group_id, feature_vector)`.
        - **Điểm kích hoạt Tick**:
          - Gọi `_evaluate_idle_alerting_groups` định kỳ trong vòng lặp `run_forever()` sau mỗi lần poll Elasticsearch (cả khi batch có dữ liệu lẫn khi batch rỗng `not batch`).
     3. **Kết quả đạt được**:
        - Khi sự cố dứt, sau khi các timestamp lỗi cũ trôi khỏi cửa sổ 10s/60s, hàm tick sẽ tính ra `rate = 0` $\rightarrow$ điểm `anomaly_score` tụt dốc về `< 0.4` $\rightarrow$ máy trạng thái `AlertStateMachine` tự động chuyển từ `ALERTING` $\rightarrow$ `COOLING` $\rightarrow$ `NORMAL` $\rightarrow$ Prometheus Gauge hạ về `0.0` hoàn toàn tự động.
   - **Test plan**:
     - `test_alert_cools_down_when_events_stop`: Bơm 20 log lỗi liên tiếp để kích hoạt `ALERTING = 1` $\rightarrow$ giả lập thời gian trôi qua 30 giây (không gửi log nào nữa) $\rightarrow$ gọi periodic tick $\rightarrow$ xác nhận trạng thái chuyển sang `COOLING` rồi về `NORMAL = 1` và `ALERTING = 0`.
     - `test_idle_groups_pruned_to_zero_rate`: Xác nhận `snapshot()` trả về vector rate = 0 khi toàn bộ timestamp cũ đã quá hạn retention.
6. **Counter `log_alerts_total`**:
   - Thêm metric Counter trong `prometheus_exporter.py` tăng khi chuyển trạng thái sang `ALERTING` để theo dõi tổng số lần cảnh báo lũy kế.
7. **Fix Pipeline Bottleneck & High-Throughput Optimization (Tối ưu hóa điểm nghẽn chính & phụ để nâng thông lượng từ 70 lên > 1,500 logs/s)**:
   - **Files ảnh hưởng**: 
     - `logai/anomaly/isolation_forest_model.py`
     - `logai/realtime/realtime_pipeline.py`
     - `logai/metrics/prometheus_exporter.py`
     - `logai/storage/registries.py`
     - `logai/storage/base.py`
   - **Mức độ**: 🔴 Critical / High — Giới hạn tốc độ xử lý ở mức 70–80 logs/s, gây tồn đọng hàng trăm ngàn log trong Elasticsearch khi ingest rate cao, dẫn đến độ trễ phát hiện cảnh báo lên tới hàng chục phút/tiếng.
   - **Bối cảnh & Phân tích nguyên nhân gốc**:
     1. **Điểm nghẽn chính (Chiếm > 90% CPU)**:
        - Trong `logai/anomaly/isolation_forest_model.py` (L97-103), hàm `predict()` gọi liên tiếp:
          `raw_score = float(model.decision_function(X)[0])` rồi lại gọi `is_outlier = model.predict(X)[0] == -1`.
        - Trong Scikit-learn, `predict()` thực chất gọi lại `decision_function(X) < 0` $\rightarrow$ CPU phải duyệt qua 100 cây của rừng cô lập **2 lần** cho mỗi một event log.
        - `_process_one` gọi inference từng dòng đơn lẻ ma trận `(1, 8)` lặp 500 lần/batch thay vì vector hóa ma trận `(500, 8)`.
        - **Số liệu đo thực tế trên container**:
          - Gọi 500 lần đơn lẻ: **3.0215s** (~6.04 ms/log $\rightarrow$ giới hạn trần lý thuyết ~165 logs/s).
          - Chỉ gọi `decision_function` 1 lần: **1.5273s** (giảm 50% thời gian).
          - Gọi batch 500 dòng cùng lúc `model.decision_function(X_batch)`: **0.0045s** (tương đương **111,343 logs/s**, nhanh hơn **670 lần**!).
     2. **Điểm nghẽn phụ (Metrics Lock, Gauge Spam & Disk I/O)**:
        - **Prometheus Histogram**: `logai_processing_latency_seconds.observe(...)` gọi ở từng event (500 lần/batch) chiếm lock luồng để duyệt bucket.
        - **Alert State Gauge Spam**: `set_alert_state()` duyệt cả 4 trạng thái (`NORMAL`, `WARMING`, `ALERTING`, `COOLING`) và gọi `.set()` 4 lần cho mỗi event log $\rightarrow$ 2,000 lần gán gauge mỗi batch dù trạng thái nhóm không hề thay đổi.
        - **Disk I/O Flush**: Ở cuối mỗi batch, `template_registry.flush()` và `group_registry.flush()` gọi `json.dump(indent=2)` ghi đè toàn bộ file JSON ra đĩa.
        - **Dedup GC**: `self.dedup.gc()` chạy quét dọn hash map ở mọi batch.
   - **Hướng sửa chi tiết**:
     1. **Tối ưu hóa `GlobalAnomalyModel` (File: `logai/anomaly/isolation_forest_model.py`)**:
        - Bỏ gọi `model.predict(X)` thừa, dùng trực tiếp `is_outlier = raw_score < 0`.
        - Thêm hàm `predict_batch(feature_vectors: List[FeatureVector]) -> List[AnomalyResult]`:
          - Gom toàn bộ vector thành 1 mảng NumPy 2D `X = np.array([fv.as_vector() for fv in feature_vectors])`.
          - Gọi `raw_scores = model.decision_function(X)` đúng 1 lần duy nhất cho cả batch.
          - Vector hóa tính toán `anomaly_scores = np.clip(0.5 - raw_scores, 0.0, 1.0)` và `anomalies = (raw_scores < 0) | (anomaly_scores >= threshold)`.
     2. **Tái cấu trúc luồng xử lý Batch trong `RealtimePipeline` (File: `logai/realtime/realtime_pipeline.py`)**:
        - Thay vì lặp `_process_one` khép kín từng event:
          - **Phase 1 (Parse & Features)**: Parse Drain3 + phân loại nhóm + cập nhật `FeatureEngine` cho các event trong batch.
          - **Phase 2 (Batch Anomaly Inference)**: Gom danh sách `FeatureVector` của batch gửi vào `anomaly_model.predict_batch()`.
          - **Phase 3 (Alert & State Machine)**: Chuyển dịch trạng thái cảnh báo theo kết quả inference theo từng nhóm.
     3. **Tối ưu hóa Metrics (File: `logai/metrics/prometheus_exporter.py`)**:
        - **State-Change Only**: Thêm bộ nhớ đệm `_current_states: Dict[str, str]` lưu trạng thái cảnh báo hiện tại của mỗi group. Chỉ gọi loop gán 4 gauge khi trạng thái thực tế thay đổi (`new_state != old_state`).
        - **Batch Counters**: Dùng `logai_events_processed_total.inc(len(batch))` thay vì gọi `.inc()` đơn lẻ 500 lần. Gom nhóm các đếm theo `service` và `error_code` rồi `.inc(count)` ở cuối batch.
        - **Batch Latency**: Đo thời gian của cả batch `logai_batch_processing_latency_seconds.observe(batch_duration)` hoặc tính latency trung bình per-event `observe(batch_duration / len(batch))` 1 lần mỗi batch.
     4. **Tối ưu hóa Disk I/O & Dedup (File: `logai/storage/registries.py`, `logai/realtime/realtime_pipeline.py`)**:
        - Áp dụng Dirty Flag: Chỉ kích hoạt `flush()` tức thì khi có template mới hoặc group mới được tạo (`is_new=True`). Với cập nhật `last_seen` và `event_count`, chỉ flush định kỳ (ví dụ mỗi 10 giây hoặc sau 20 batch).
        - Đặt chu kỳ cho `dedup.gc()`: Chỉ chạy sau mỗi 60 giây thay vì sau mỗi batch.
   - **Kỳ vọng đạt được**:
     - Thông lượng xử lý tăng từ **~70 logs/s lên > 1,500 – 3,000 logs/s**.
     - Giảm tải CPU tiêu hao vô ích, loại bỏ triệt để hiện tượng backlog bị dồn ứ khi stream log với tốc độ cao.
   - **Test plan**:
     - Viết unit test cho `predict_batch()` đảm bảo kết quả trùng khớp 100% với `predict()` từng phần tử.
     - Kiểm tra state-change metrics đảm bảo không bỏ sót việc chuyển trạng thái `NORMAL` $\leftrightarrow$ `ALERTING`.
     - Chạy benchmark đo throughput trước và sau khi tối ưu.
   - **Ghi chú triển khai (thiết kế đã duyệt — 2026-09-11)**:
     - **Micro-batch cho giai đoạn PREDICT với 2 ngưỡng (whichever-first)**: tích luỹ
       `(group_id, FeatureVector)` qua các poll và flush→predict khi **(a)** buffer ≥
       `anomaly.predict_batch_size`, **hoặc (b)** poll trả `< elasticsearch.batch_size`
       (ES cạn → predict ngay, độ trễ thấp khi tải nhẹ), **hoặc (c)** đã đợi ≥
       `anomaly.predict_max_wait_seconds` kể từ event đầu trong buffer (backstop).
     - **Con số config (KHÔNG hardcode, nạp qua `_merge_dataclass`)**:
       `anomaly.predict_batch_size = 1024` (dải 512–2048),
       `anomaly.predict_max_wait_seconds = 1.0` (dải 0.5–2.0),
       `elasticsearch.poll_interval_seconds` đổi `5.0 → 1.0` (nhịp thức phải ≤ max_wait),
       `elasticsearch.batch_size = 500` (giữ). Vòng lặp ngủ theo `min(poll_interval,
       thời-gian-còn-lại-tới-max_wait)` để timer max_wait có hiệu lực.
     - **Predict-aligned durability (giữ nguyên crash-safety khi gom qua nhiều poll)**:
       dời `template_registry.flush()` → `group_registry.flush()` → `dedup.gc()` →
       `checkpoint.commit()` xuống **đúng biên flush-predict**, giữ **nguyên thứ tự**.
       Mọi crash trong Phase 1 (`_process_one`) hay trong `_flush_predictions` đều xảy
       ra TRƯỚC `checkpoint.commit` → cursor không advance; dedup mark là in-memory tới
       lúc flush nên replay được chặn idempotent, không mất/nhân bản alert.
     - **Giữ `_process_one` per-event (không đổi chữ ký, trả `bool`)**: chỉ dời phần
       *suy luận* ra biên flush qua buffer instance `self._pending_predictions`. Nhờ mọi
       test hiện có dùng batch partial (`< batch_size`) hoặc ≥ `predict_batch_size` nên
       chúng flush mỗi poll y như cũ → accumulator vô hình; chỉ **3 test** phải port
       (predict tách khỏi `_process_one`). `_run_anomaly_and_alert` GIỮ NGUYÊN cho
       idle-tick Fix #5; `predict()` đơn ủy quyền `predict_batch([fv])[0]`.
     - **I/O & metrics phụ**: `JSONStore.flush()` thêm cờ `_dirty` (no-op khi sạch;
       `DedupIndex` đã có sẵn); `set_alert_state()` return sớm khi trạng thái không đổi
       (không ghi 4 gauge mỗi event); `logai_events_received_total.inc(len(batch))`.
     - **Trạng thái**: ✅ HOÀN THÀNH (2026-09-11) — 132/132 test pass. Chi tiết
       triển khai & nghiệm thu: xem `ISSUES_FIXED.md` mục "Issue 10", kiến trúc
       cập nhật ở `ARCHITECTURE.md` §7.5 và §10.3. Con số chốt lại:
       `predict_batch_size=500`, `predict_max_wait_seconds=1.0`,
       `poll_interval_seconds=1.0` (đều config qua `config.yaml`).

---

## 11. Báo cáo Kiểm thử Tải 100 logs/s (3 Giờ) & Kế hoạch Khắc phục Nhiễu Nền (Sparse Noise Alert)

**Ngày thực hiện**: 2026-09-13 (19:37:40 — 22:37:40)
**Trạng thái kiểm thử**: ✅ HOÀN TẤT THÀNH CÔNG — Đã xử lý 1,080,000 logs (100% trọn vẹn)
**Tài liệu liên quan**: `logai/features/feature_engine.py`, `logai/alert/alert_state_machine.py`, `config.yaml`

### 11.1. Tóm tắt Kết quả Bài Test 3 Giờ

1. **Thông số & Độ tin cậy**:
   - **Tốc độ stream**: 100 logs/giây liên tục trong 3 giờ (10,800 giây = 1,080,000 logs).
   - **Độ toàn vẹn**: 100% logs được ghi vào Elasticsearch (`hdfs-logs`) và xử lý bởi `logai-engine`. Checkpoint (`1789313860.454843`) khớp chính xác micro-giây với log cuối cùng.
   - **Lỗi / Thất bại**: 0 lỗi (`logai_events_failed_total = 0.0`), 0 log rơi vào DLQ.
   - **Hiệu năng & Tài nguyên**: Độ trễ trung bình ~0.139 ms/event, hàng đợi ổn định ~195 logs (dưới giới hạn 1,000), RAM tiêu thụ chỉ ~472 MiB (15% limit).
2. **Diễn biến Cảnh báo (Alert Timeline)**:
   - **Phút 0 – 50 (19:37 – 20:27)**: Sau 5 phút cold start ban đầu, các nhóm log chính (`G0000`, `G0003`, `G0004`...) giữ trạng thái `NORMAL`.
   - **Phút 50 – 60 (20:27 – 20:37, Pha Sự Cố)**: Bơm sự cố `mixed` (85% log lỗi). Lưu lượng tăng lên ~151 logs/s. Sau 3 phút tích lũy (`alert_consecutive: 3`), hệ thống kích hoạt chính xác cảnh báo `ALERTING` trên các nhóm template liên quan (tăng vọt lên 8/11 nhóm).
   - **Khoảnh khắc kết thúc sự cố (20:36 – 20:38)**: Số alert tạm thời tụt về 0 do baseline $\mu$ trong cửa sổ trượt 5 phút bị bão hòa ở mức cực cao trong suốt 10 phút sự cố dồn dập.
   - **Phút 60 – 180 (20:40 – 22:38, 2 tiếng phục hồi)**: Đồ thị Grafana tăng trở lại 5 alert (`G0001`, `G0002`, `G_SINGLE_0003`, `G_SINGLE_0004`, `G_SINGLE_0005`) và giữ nguyên suốt 2 tiếng.
   - **Sau khi bài test dừng (22:42 – Nay)**: Luồng log ngừng hẳn, cơ chế Idle Tick kích hoạt, 100% 11/11 nhóm đã hạ nhiệt thành công về `NORMAL` với điểm số cơ sở `0.3426`.

---

### 11.2. Phân tích Nguyên nhân: Nhiễu nhỏ gây kéo dài Alert (Root Cause)

* **Nguyên nhân từ Generator**: Trong cả pha bình thường lẫn phục hồi, generator giữ `noise_rate = 0.004` (0.4% log lỗi ngẫu nhiên, tương đương 1 log lỗi mỗi 2.5 giây).
* **Hiệu ứng "Gai nhọn trên nền 0" (Sparse Spikes on Zero Baseline)**:
  - Khi 5 phút sự cố trôi qua, đường cơ sở $\mu$ của các nhóm lỗi trong `FeatureEngine` tụt về xấp xỉ 0 ($\mu \approx 0.003$ log/s).
  - Khi chỉ cần 1 log lỗi xuất hiện trong 10 giây (`rate_10s = 0.1`):
    * `spike_ratio_10s = rate_10s / (mu + eps)` bị thổi phồng toán học lên kịch trần `20.0`.
    * `short_growth_rate` vọt lên mức tối đa `6.0`.
    * `burstiness_10s` ($\sigma^2 / \mu^2$) vọt lên cực đại.
  - Mô hình Isolation Forest chấm điểm ngoại lai cao: `anomaly_score ≈ 0.70 – 0.75` (vượt ngưỡng cảnh báo `0.6`).
* **Cơ chế Cooldown bị nghẽn**:
  - `AlertStateMachine` yêu cầu 3 lần đánh giá liên tiếp có score $< 0.4$ (`cool_consecutive: 3`).
  - Do cứ 2–3 giây lại có 1 log lỗi ngẫu nhiên đến, điểm số lại vọt lên ~0.72 khiến bộ đếm cooldown liên tục bị reset về 0.
  - Kết quả: Hệ thống rơi vào trạng thái cảnh báo giả kéo dài (Persistent False Positive).

---

### 11.3. Kế hoạch Khắc phục (Action Items / TODO)

Mục tiêu: Đảm bảo các log lỗi nhỏ lẻ tẻ, thưa thớt (noise / low-frequency) không kích hoạt hoặc kéo dài trạng thái `ALERTING`, chỉ báo động khi có đột biến về mặt lưu lượng thực sự.

- [x] **Nhiệm vụ 1: Bộ lọc Ngưỡng Tần suất Tối thiểu (Minimum Volume / Noise Floor Filter)**
  - *Vị trí*: `logai/features/feature_engine.py` hoặc `logai/alert/alert_state_machine.py`.
  - *Giải pháp*: Bổ sung điều kiện kiểm tra số lượng sự kiện tuyệt đối:
    - Nếu tổng số log trong cửa sổ gần nhất quá nhỏ (ví dụ: `count_1m < 3` hoặc `rate_1m < 0.05` log/s), vector đặc trưng sẽ được gán điểm bình thường hoặc không cho phép chuyển trạng thái sang `ALERTING`.
    - Điều này ngăn việc 1 log đơn lẻ kích hoạt báo động sai.

- [x] **Nhiệm vụ 2: Áp dụng Epsilon Động / Sàn Tốc độ (Rate Floor for Baseline)**
  - *Vị trí*: `logai/features/feature_engine.py` (trong hàm `_compute`).
  - *Giải pháp*: Khi tính các tỷ số chia cho $\mu$ (`spike_ratio_10s`, `short_growth_rate`, `burstiness_10s`), thay vì chia cho `(mu + eps)` với `eps = 1e-9`, đặt một giá trị sàn tối thiểu `mu_effective = max(mu, MIN_RATE_FLOOR)` (ví dụ: `MIN_RATE_FLOOR = 0.2` log/s):
    $$\text{spike\_ratio\_10s} = \frac{\text{rate\_10s}}{\max(\mu_{10}, 0.2)}$$
    Nhờ đó, khi $\mu \approx 0$ và chỉ có 1 log (`rate_10s = 0.1`), tỷ số chỉ đạt $0.1 / 0.2 = 0.5$ (không bị phóng đại lên trần 20.0).

- [ ] **Nhiệm vụ 3: Cải tiến Cooldown theo Thời gian (Time-Window Cooldown)**
  - *Vị trí*: `logai/alert/alert_state_machine.py`.
  - *Giải pháp*: Bổ sung cơ chế Cooldown theo thời gian trôi qua (time-based decay) song song với đếm số lần liên tiếp. Nếu trong vòng $T$ giây (ví dụ 60 giây) không xuất hiện đợt burst lớn nào mà chỉ có log thưa thớt lẻ tẻ, nhóm sẽ tự động chuyển dần từ `ALERTING` $\rightarrow$ `COOLING` $\rightarrow$ `NORMAL`.

- [ ] **Nhiệm vụ 4: Kiểm thử và Xác minh (Test & Verification)**
  - Viết unit test mô phỏng kịch bản log lỗi lẻ tẻ (`noise_rate = 0.004` ở tải 100 logs/s) để đảm bảo không bị kích hoạt `ALERTING`.
  - Chạy lại kiểm thử thực tế với generator để xác nhận đồ thị Grafana hạ nhiệt hoàn toàn về `NORMAL` khi hết sự cố bất thường.

**Ghi chú triển khai (2026-09-14)**:
- Đã thêm `features.rate_floor=0.2` cho `short_growth_rate`,
  `burstiness_10s`, `spike_ratio_10s`; model input vẫn đúng 8 chiều.
- Đã thêm metadata `count_1m` và gate `alert.min_events_1m=3`. Sparse score
  vẫn được export nhưng không thể leo thang alert và được coi là recovery.
- Đã tách artifact mới thành `if-global-v3` / `models/global_v3.pkl`; bắt buộc
  retrain trước khi chạy realtime, không tái sử dụng model công thức cũ.
- Unit/regression suite: ✅ 138/138 pass trong `.venv`. Load test thực tế 3 giờ
  và Nhiệm vụ 3 vẫn chưa thực hiện.

---

## 12. Batch Persistence cho Alert State

**Ngày thực hiện**: 2026-09-14
**Trạng thái**: ✅ HOÀN THÀNH — 142/142 test pass

### Vấn đề

`predict_batch()` đã vector hóa inference, nhưng realtime vẫn gọi
`AlertStateMachine.transition()` cho từng result. Mỗi transition dùng
`JSONStore.set(flush=True)`, khiến toàn bộ `anomaly_state.json` bị serialize và
atomic replace tới 500 lần trong một predict batch. Chi phí tăng theo cả số
event trong batch và tổng số group đã persist.

### Giải pháp

- Thêm `transition_batch(results)`: áp dụng toàn bộ transition tuần tự trong RAM,
  giữ intermediate states cho Prometheus, nhưng chỉ persist final state của mỗi
  group qua một lần `JSONStore.bulk_set()`.
- Giữ `transition(result)` tương thích ngược bằng cách delegate qua batch một
  phần tử.
- `RealtimePipeline._flush_predictions()` chỉ clear pending buffer sau khi batch
  state đã persist thành công. Nếu ghi file lỗi, exception propagate, cursor
  không commit và buffer vẫn còn để retry/restart.

### Kết quả

Benchmark 500 transitions: nhanh hơn khoảng 73x với 11 group, 218x với 100
group và 200x trong workload 1.000 group. Không thay đổi model, config, thứ tự
hysteresis hoặc metric `log_alerts_total`.

---

## 13. Khắc phục Bottleneck `_assign_group`: Fast-Path cho Pending/Unassigned Templates và Tối ưu Disk I/O

**Ngày tạo**: 2026-09-14  
**Trạng thái**: 🟡 ĐANG THỰC HIỆN  
**Mức độ nghiêm trọng**: 🔴 Critical — Gây sập thông lượng 99% (từ 6.400 logs/s xuống 43 logs/s), CPU 200%, tích tụ backlog hàng trăm nghìn log và phá vỡ hoàn toàn tính realtime của pipeline.  
**Files liên quan**:
- `logai/realtime/realtime_pipeline.py` (`_assign_group`, `RealtimePipeline.__init__`)
- `logai/storage/registries.py` (`TemplateRegistry.set_embedding`)
- `tests/test_pending_template_fast_path.py` (MỚI)
- `tests/test_realtime_pipeline_end_to_end.py`

---

### 13.1. Hiện trạng & Đo đạc Thực tế (Production Evidence)

Khi generator stream log trực tiếp với tốc độ 200 logs/s:
- **Tốc độ generator**: 200 logs/giây.
- **Tốc độ xử lý của `logai-engine`**: chỉ đạt **~43 – 67.4 logs/giây**.
- **Lag tích tụ**: Checkpoint engine (`08:46:08 UTC`) trễ hơn 16–23 phút so với log mới nhất trong Elasticsearch (`09:02:15 UTC`). Tồn đọng **> 193.400 logs backlog**.
- **Thời gian ước tính nếu không sửa**: $\approx 193.400 / 67.4 \approx 2.869\text{s} \approx \mathbf{48\text{ phút}}$.

**Kết quả cProfile và thời gian đo đạc trên 500 unseen logs thực tế**:
- `parse` (Drain3): `0.0169s`
- `doc` (Doc Matcher): `0.0177s`
- `fe_update` (Feature Engine): `0.0627s`
- `flush` (Anomaly predict + Checkpoint commit): `0.0169s`
- **`assign` (`_assign_group`): `11.5021s` (Chiếm 99.0% tổng thời gian xử lý toàn batch!)**
- **Tổng thời gian cho 500 logs**: `11.6164s` $\rightarrow$ Thông lượng thực tế chỉ đạt **43.0 logs/s**.

---

### 13.2. Phân tích Nguyên nhân Cốt lõi (Root Cause Analysis)

1. **Vòng lặp Re-Embedding vô tận đối với Template Pending/Unassigned**:
   - Khi có một template mới xuất hiện (ví dụ `T00031`: `<*>:Transmitted block <*> to <*>`), Drain3 parse và trả về `template_id`.
   - Pipeline kiểm tra:
     ```python
     existing_template = self.template_registry.get(parsed.template_id)
     if existing_template and existing_template.group_id:
         # Fast path O(1) chỉ áp dụng khi group_id tồn tại!
         return GroupedEvent(...)
     ```
   - Nếu template chưa có group hoặc độ tương đồng với các centroid không đủ ngưỡng (`similarity = 0.687 < threshold 0.80`), template được đánh dấu `state.group_id = None` (Pending).
   - **Bug chí mạng**: Ở các log tiếp theo mang cùng template `T00031`, vì `existing_template.group_id` là `None`, điều kiện `if existing_template and existing_template.group_id:` **luôn trả về `False`**.
   - Dẫn đến việc mỗi event log tiếp theo đều bị xem như template mới lạ:
     - Gọi `self.embedder.embed_one(parsed.template)`: Chạy lại mô hình Transformer NLP trên CPU tốn **~250 – 300ms cho từng dòng log**.
     - Gọi `assign_to_nearest_group()` quét lại toàn bộ centroids.
     - Gọi `set_embedding()` ghi đè file pickle ra đĩa liên tục.
   - Trong luồng HDFS, log `Transmitted block` chiếm ~8% tổng lượng log (~16 logs/giây). 
     $$16 \text{ logs/s} \times 0.3\text{s CPU} \approx \mathbf{4.8\text{ giây CPU cho mỗi giây thực tế!}}$$
     Khiến CPU luôn đạt mức trần 200% và thông lượng toàn hệ thống sụt giảm 99%.

2. **Ghi đĩa Pickle đồng bộ trên hot path**:
   - `TemplateRegistry.set_embedding()` hiện đang gọi trực tiếp `self._embeddings.save(self._embedding_cache)` (ghi ra đĩa bằng `tempfile` và `os.replace`) ngay trong vòng lặp per-event, tạo ra I/O disk stall không đáng có.

---

### 13.3. Kiến trúc Giải pháp (Architecture Solution)

1. **Cơ chế Fast-Path 3 trạng thái trong `_assign_group`**:
   - **Trạng thái 1 (Known & Grouped)**: `group_id` hợp lệ và khác `PENDING_GROUP_ID` $\rightarrow$ Fast-path $O(1)$ như hiện tại.
   - **Trạng thái 2 (Known & Pending/Unassigned)**: Template đã có trong registry nhưng `group_id is None` hoặc `group_id == PENDING_GROUP_ID`:
     - Cập nhật `last_seen = parsed.raw.timestamp`, tăng `event_count += 1`.
     - `self.template_registry.upsert(existing_template, flush=False)`.
     - **Return `None` ngay lập tức** trong $O(1)$ RAM, bỏ qua 100% việc gọi `embed_one()` và clustering.
   - **Trạng thái 3 (Truly Unknown)**: Lần đầu tiên nhìn thấy template:
     - Chạy `embed_one()` và `assign_to_nearest_group()` **đúng 1 lần duy nhất**.
     - Nếu không đủ ngưỡng similarity: gán `state.group_id = PENDING_GROUP_ID` (hoặc `None`), lưu registry một lần.
     - Đưa vào bộ nhớ đệm `self._pending_templates: set[str]` để các log sau đi thẳng vào Fast-path.

2. **Tối ưu Disk I/O trong `TemplateRegistry.set_embedding`**:
   - Thêm tham số `flush: bool = False` vào `set_embedding`.
   - Trên hot path, chỉ cập nhật `self._embedding_cache` trong RAM.
   - Việc ghi đĩa file pickle `template_embeddings.pkl` được gom lại và thực thi duy nhất 1 lần ở biên `_flush_batch()` (`TemplateRegistry.flush()`).

3. **Kết quả kỳ vọng**:
   - Thời gian xử lý 500 logs giảm từ **11.61s xuống 0.077s**.
   - Thông lượng tăng từ **43 logs/s lên > 6.400 logs/s** (Tăng tốc **150 lần**).
   - Xử lý sạch 193.400 logs tồn đọng chỉ trong **~30 giây**.

---

### 13.4. Kế hoạch Thực hiện & Checklist (Action Items)

- [x] **Nhiệm vụ 1**: Sửa logic phân loại 3 trạng thái trong `_assign_group` tại `logai/realtime/realtime_pipeline.py`.
- [x] **Nhiệm vụ 2**: Tối ưu `TemplateRegistry.set_embedding` trong `logai/storage/registries.py` (loại bỏ synchronous pickle save trên hot path).
- [x] **Nhiệm vụ 3**: Viết Unit Test `tests/test_pending_template_fast_path.py` (kiểm tra 100 event lặp lại của pending template chỉ gọi `embed_one` đúng 1 lần - 3/3 pass).
- [x] **Nhiệm vụ 4**: Chạy toàn bộ test suite hồi quy (148 tests passed 100% trong 8.84s).
- [x] **Nhiệm vụ 5**: Xử lý và xác nhận trên nhánh riêng `fix/assign-group-bottleneck` (Commit `3f0d3f9`). Toàn bộ backlog 193.400 logs đã được xả sạch về 0, checkpoint engine đã đuổi kịp log cuối cùng tại `09:02:15 UTC` (`1789376535.671149`). Tốc độ nhàn rỗi về 0.00 logs/s và tổng event processed đạt 1.094.159.

---

## 14. Phương án Huấn luyện lại từ đầu (Retrain from Scratch) với Masking Rules mới

**Ngày tạo**: 2026-09-14  
**Mục tiêu**: Tái cấu trúc toàn bộ kho tri thức AIOps (Drain3 templates, MPNet embeddings, HDBSCAN clusters, Isolation Forest anomaly models) dựa trên bộ luật `masking_rules` mới và `sim_threshold: 0.5` để đạt độ chính xác tối đa và zero pending templates khi realtime stream.

---

### 14.1. Tại sao cần Huấn luyện lại từ đầu?

1. **Chuẩn hóa Template theo Masking mới**:
   - Khi cấu hình regex `masking_rules` (mask IP/Port `/?\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?`, Block ID `blk_-?\d+`, đường dẫn và số), các chuỗi log thô như `10.250.10.4:50010:Transmitted...` được Drain3 phân tích chuẩn thành `<*>:Transmitted block <*> to <*>`.
   - Dữ liệu `drain3_state.bin` cũ được huấn luyện trước đây chỉ biết 30 template cũ (không có token `*:Transmitted`).
2. **Loại bỏ hoàn toàn trạng thái "Pending"**:
   - Khi train lại từ đầu trên dữ liệu lịch sử phong phú, tất cả các dạng log (kể cả các log hiếm và log truyền tải) đều được Drain3 tạo template và HDBSCAN gom nhóm vào các cụm ngữ nghĩa `G0000`, `G0001`, ...
   - Khi đưa vào Realtime, **100% log đến đều là Known Grouped Template**, đi qua Fast-Path $O(1)$ ngay lập tức, triệt tiêu hoàn toàn độ trễ nhúng NLP.
3. **Huấn luyện lại Baseline Anomaly Detection (Isolation Forest)**:
   - Feature engine 8D sẽ tính toán lại tần suất, tốc độ thay đổi và entropy của các group mới.
   - Mô hình Isolation Forest sẽ học chính xác phân phối bình thường của các cụm theo luật template mới, tránh báo động giả (False Positives) hoặc bỏ sót bất thường (False Negatives).

---

### 14.2. Quy trình Thực hiện Huấn luyện lại từng bước (Step-by-Step Runbook)

#### Bước 1: Merge branch và Rebuild Docker Image
Đảm bảo mã nguồn mới nhất (đã vá `_assign_group`, cập nhật `masking_rules` trong `config.yaml` và `drain3_parser.py`) được đóng gói vào image:
```bash
git checkout master
git merge fix/assign-group-bottleneck
docker compose -f docker-compose.reuse.yml build logai-engine
```

#### Bước 2: Tạm dừng Service Realtime
Dừng container realtime để giải phóng CPU/RAM cho tác vụ training và tránh xung đột truy cập file:
```bash
docker compose -f docker-compose.reuse.yml stop logai-engine
```

#### Bước 3: Sao lưu dữ liệu cũ và Dọn sạch State Volume
Để mô hình mới được học hoàn toàn sạch sẽ, không bị lẫn các cluster cũ trong `drain3_state.bin` hay `group_registry.json`:

1. **Tạo bản sao lưu (Backup) an toàn**:
   ```bash
   mkdir -p backup
   docker run --rm -v logai-data:/data -v $(pwd)/backup:/backup alpine \
       tar czf /backup/logai_data_backup_$(date +%Y%m%d_%H%M%S).tar.gz -C /data .
   ```

2. **Dọn dẹp các file state cũ trong Volume `logai-data`**:
   ```bash
   docker run --rm -v logai-data:/data alpine sh -c "\
       rm -f /data/drain3_state.bin \
             /data/template_registry.json \
             /data/template_embeddings.pkl \
             /data/group_registry.json \
             /data/group_centroids.pkl \
             /data/models/* \
             /data/anomaly_state.json \
             /data/window_state.json \
             /data/training_checkpoint.json \
             /data/training_event_index.jsonl"
   ```
   *(Lưu ý: Giữ lại `checkpoint.json` nếu muốn Realtime chỉ đọc các log mới sinh sau này, hoặc xóa luôn `checkpoint.json` nếu muốn Realtime replay lại từ log đầu tiên trong Elasticsearch).*

#### Bước 4: Chạy Tác vụ Huấn luyện Độc lập (One-off Training Job)
Khởi chạy container huấn luyện đọc dữ liệu lịch sử từ Elasticsearch:
```bash
# Huấn luyện trên 48 giờ (hoặc toàn bộ log hiện có trong ES)
docker compose -f docker-compose.reuse.yml run --rm logai-training \
    python scripts/run_training.py --lookback-hours 48
```
**Tiến trình huấn luyện tự động bao gồm:**
1. **Drain3 Parsing**: Quét các log lịch sử, áp dụng regex masking và tạo các template chuẩn hóa.
2. **MPNet Embedding**: Nhúng ngữ nghĩa các template thành vector 768 chiều.
3. **HDBSCAN Clustering**: Phân cụm các template thành các Semantic Groups (`G0000`, `G0001`, ...) và tính vector centroid cho từng nhóm.
4. **Feature Extraction**: Tạo chuỗi Feature Vectors 8D cho từng group theo các cửa sổ thời gian.
5. **Model Fitting**: Huấn luyện các mô hình Isolation Forest (`data/models/`) và lưu cấu hình trạng thái.

#### Bước 5: Kiểm tra & Nghiệm thu Artifacts sau Training
Chạy lệnh kiểm tra tính toàn vẹn của dữ liệu vừa train trong volume:
```bash
docker run --rm -v logai-data:/data alpine ls -la /data /data/models
```
Yêu cầu nghiệm thu:
- `drain3_state.bin` > 0 bytes.
- `template_registry.json` chứa các template mới (toàn bộ đều có `group_id` hợp lệ, không có `UNASSIGNED_PENDING`).
- `group_registry.json` và `group_centroids.pkl` đã được cập nhật.
- Thư mục `models/` chứa file artifact model `.joblib`.

#### Bước 6: Khởi động lại Service Realtime
Bật lại service realtime với tri thức mới đã được học hoàn chỉnh:
```bash
docker compose -f docker-compose.reuse.yml up -d logai-engine
```

#### Bước 7: Khởi động lại Log Generator và Giám sát trên Grafana
1. Bật generator stream log (với tải 200 – 400 logs/s).
2. Kiểm tra Grafana Dashboard:
   - Thông lượng (`rate(logai_events_processed_total[1m])`) bám sát tốc độ generator.
   - Thời gian xử lý (`logai_processing_latency_seconds`) ở mức dưới **1ms/event**.
   - CPU usage của `logai-engine` duy trì ổn định ở mức thấp (< 15%).

