# logai2 — API cảnh báo theo module (hướng dẫn cho service gọi vào)

Tài liệu cho các team/service muốn hỏi logai2: **"Trong khoảng thời gian T1 → T2, module M của 
có những log template nào đang bị cảnh báo?"**

- Chỉ đọc (GET), không ghi gì vào logai.
- Dữ liệu tính theo **từng phút**, giữ được **90 ngày** gần nhất.

---

## 1. Gọi nhanh

```bash
curl -s "http://logai-web:5555/api/module-alerts?module_code=VTN_CNTT_VAS_066_301&from=2026-10-22T07:00:00Z&to=2026-10-22T07:15:00Z"
```

| Môi trường | Base URL |
|---|---|
| Kubernetes (trong cluster, cùng namespace) | `http://logai-web:5555` |
| Kubernetes (khác namespace) | `http://logai-web.<namespace>.svc.cluster.local:5555` |
| Ngoài cluster (qua NodePort) | `http://<IP-node-bất-kỳ>:8080` |

Web logai trên k8s được mở ra ngoài bằng một **NodePort 8080**: từ ngoài cluster gọi vào
`http://<IP của node bất kỳ>:8080` với cùng đường dẫn và tham số, ví dụ
`http://<IP-node>:8080/api/module-alerts?module_code=...&from=...&to=...`.

API chỉ mở trong mạng nội bộ; không public ra Internet.

## 2. Xác thực

- Hiện không để xác thực

## 3. Request

```
GET /api/module-alerts?module_code=<M>&from=<thời điểm>&to=<thời điểm>
```

| Tham số | Bắt buộc | Quy tắc |
|---|---|---|
| `module_code` | có | Khớp **chính xác**, phân biệt hoa/thường (giá trị trường `moduleCode` trong log). Có dấu cách, `/`, tiếng Việt thì **URL-encode**. |
| `from` | có | Đầu khoảng thời gian. |
| `to` | có | Cuối khoảng thời gian, phải `>= from`. |

### Định dạng thời gian (`from`, `to`)

Chấp nhận cả ba kiểu, kết quả như nhau:

| Kiểu | Ví dụ | Ghi chú |
|---|---|---|
| ISO-8601 UTC | `2026-09-30T07:00:30.500Z` | khuyên dùng |
| ISO-8601 có múi giờ | `2026-09-30T14:00:30.500+07:00` | URL-encode dấu `+` thành `%2B` |
| ISO không múi giờ | `2026-09-30T07:00:30` | **được hiểu là UTC**, không phải giờ VN |
| Epoch mili-giây | `1790751630500` | số > 10^11 được hiểu là ms |
| Epoch giây | `1790751630` hoặc `1790751630.5` | |

### Làm tròn: độ phân giải 1 phút, mở rộng ra ngoài

logai đếm và đánh giá theo phút, nên khoảng gửi được **nới ra cho phủ đủ các phút** chứa nó:
`from` làm tròn **xuống** đầu phút, `to` làm tròn **lên** đầu phút kế tiếp. Khoảng thực đọc là
`[effective_from, effective_to)` và được trả lại trong response.

| Bạn gửi | Khoảng thực đọc | Số phút |
|---|---|---|
| `07:00:30.500 → 07:00:31.200` | `07:00 → 07:01` | 1 |
| `07:00:59.999 → 07:01:00.001` | `07:00 → 07:02` | 2 |
| `07:00:00.000 → 07:05:00.000` | `07:00 → 07:05` | 5 |
| `07:03:12.345 → 07:03:12.345` (`from == to`) | `07:03 → 07:04` | 1 |

### Phạm vi

- Không giới hạn độ dài khoảng.
- `from` cũ hơn giới hạn lưu trữ (00:00 giờ VN của ngày hôm nay − 90) bị kẹp về giới hạn đó.
- `to` ở tương lai bị kẹp về hiện tại.
- Luôn xem `effective_from` / `effective_to` để biết khoảng thật sự được đọc.

## 4. Response `200`

```json
{
  "module_code": "VTN_CNTT_VAS_066_301",
  "from": "2026-09-30T07:00:30.500Z",
  "to": "2026-09-30T07:14:10.200Z",
  "effective_from": "2026-09-30T07:00:00Z",
  "effective_to": "2026-09-30T07:15:00Z",
  "resolution": "minute",
  "data_until": "2026-09-30T07:13:00Z",
  "unit": "events_per_minute",
  "templates": [
    {
      "service": "vtn_cntt_vas_066",
      "template_id": "vtn_cntt_vas_066:T00431",
      "text": "Timeout acquiring connection <*>",
      "level": "ERROR",
      "doc_id": "DOC-017",
      "doc_title": "Restart DB pool",
      "peak_state": "ALERTING",
      "periods": [{"start": "2026-09-30T06:58:00Z", "end": null}],
      "alerting_minutes": 15,
      "module":   {"total": 300, "avg": 20.0, "peak": 42,
                   "series": [{"minute": "2026-09-30T07:00:00Z", "count": 18}]},
      "template": {"total": 420, "avg": 28.0, "peak": 55}
    }
  ]
}
```

Mọi thời gian trả về là **UTC giờ chuẩn, đuôi `Z`**; phần mili-giây chỉ xuất hiện khi khác 0.

### Trường cấp gốc

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `module_code` | string | module đã hỏi  |
| `from`, `to` | string | thời điểm bạn gửi, chuẩn hoá về ISO UTC |
| `effective_from`, `effective_to` | string | khoảng thực đọc (đã làm tròn, đã kẹp) |
| `resolution` | `"minute"` | luôn là phút |
| `data_until` | string \| null | cuối phút gần nhất logai2 **đã xử lý xong**, thường trễ 1–2 phút so với hiện tại. `null` = engine chưa chạy lần nào. Xem mục 6. |
| `unit` | `"events_per_minute"` | đơn vị của `avg`, `peak`, `series` |
| `templates` | array | danh sách template bị cảnh báo, sắp theo `module.total` giảm dần; rỗng nếu không có |

### Trường của mỗi template

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `service` | string | service ghi ra log (`service_code2`) |
| `template_id` | string | id ổn định của mẫu log, dạng `<service>:T<số>` |
| `text` | string | mẫu log, phần biến đổi thay bằng `<*>` |
| `level` | string | mức nặng nhất từng thấy: `ERROR`, `WARN`, ... |
| `doc_id`, `doc_title` | string \| null | runbook hướng dẫn xử lý gắn với template; `null` nếu chưa có |
| `peak_state` | `"ALERTING"` \| `"WARMING"` | `ALERTING` = sự cố đã xác nhận (≥ 3 phút cao liên tiếp); `WARMING` = tăng ngắn rồi tự hết |
| `periods` | array | các đợt cảnh báo chạm vào khoảng hỏi. `start` = phút đầu tiên; `end` = phút ngay sau đợt, **`null` = vẫn đang cảnh báo**. Không bị cắt theo khoảng hỏi (có thể bắt đầu trước `from`). |
| `alerting_minutes` | int | số phút cảnh báo **nằm trong** khoảng hỏi |
| `module.total` | int | số dòng log **của module bạn hỏi** thuộc template này trong các phút cảnh báo |
| `module.avg` | number | `total / alerting_minutes`, 2 chữ số thập phân |
| `module.peak` | int | số dòng cao nhất trong một phút |
| `module.series` | array | chi tiết theo phút `{"minute", "count"}` khi khoảng hỏi ≤ 1 ngày; theo giờ `{"hour", "count"}` khi > 1 ngày. Phút/giờ không có dòng thì **không xuất hiện** (= 0). |
| `template` | object | như `module` nhưng tính trên **mọi module** (không có `series`) — để so module của bạn chiếm bao nhiêu phần |

## 5. Khi nào một template được trả về
Một template xuất hiện khi **cả hai** điều sau đúng:
1. nó ở WARMING hoặc ALERTING trong khoảng hỏi
2. module_code có ghi ít nhất một dòng của nó trong các phút đó.


## 6. Lỗi

| HTTP | Body | Khi nào |
|---|---|---|
| 400 | `{"error": "module_code is required"}` | thiếu hoặc rỗng `module_code` |
| 400 | `{"error": "from and to are required"}` | thiếu `from` hoặc `to` |
| 400 | `{"error": "cannot parse time '...'"}` | thời gian không phải ISO-8601 hay số |
| 400 | `{"error": "from is after to"}` | `from > to` |
| 5xx | — | logai2 hoặc database đang lỗi: thử lại có backoff |

## 7. Ví dụ code

**Python (requests)**

```python
import os, requests

def module_alerts(module_code, start, end):
    r = requests.get(
        "http://logai-web:5555/api/module-alerts",
        params={"module_code": module_code, "from": start, "to": end},   # requests tự URL-encode
        timeout=10,
    )
    r.raise_for_status()
    return r.json()

res = module_alerts("VTN_CNTT_VAS_066_301", "2026-09-30T07:00:00Z", "2026-09-30T08:00:00Z")
for t in res["templates"]:
    if t["peak_state"] == "ALERTING":                       # chỉ lấy sự cố đã xác nhận
        print(t["level"], t["module"]["total"], t["text"], t["doc_title"])
```

**JavaScript / Node 18+**

```js
const qs = new URLSearchParams({
  module_code: "VTN_CNTT_VAS_066_301",
  from: new Date(Date.now() - 15 * 60_000).toISOString(),
  to: new Date().toISOString(),
});
const res = await fetch(`http://logai-web:5555/api/module-alerts?${qs}`);
if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
const { templates } = await res.json();
```

**Java (java.net.http)**

```java
String q = "module_code=" + URLEncoder.encode("VTN_CNTT_VAS_066_301", UTF_8)
         + "&from=" + URLEncoder.encode("2026-09-30T07:00:00Z", UTF_8)
         + "&to="   + URLEncoder.encode("2026-09-30T07:15:00Z", UTF_8);
HttpRequest req = HttpRequest.newBuilder(URI.create("http://logai-web:5555/api/module-alerts?" + q))
        .timeout(Duration.ofSeconds(10)).GET().build();
HttpResponse<String> resp = HttpClient.newHttpClient().send(req, HttpResponse.BodyHandlers.ofString());
```

**curl với module có ký tự đặc biệt**

```bash
curl -sG "http://logai-web:5555/api/module-alerts" \
  --data-urlencode "module_code=APP/066 é" \
  --data-urlencode "from=2026-09-30T14:00:00+07:00" \
  --data-urlencode "to=2026-09-30T14:15:00+07:00"
```


