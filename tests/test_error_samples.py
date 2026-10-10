"""Raw ERROR/FATAL evidence for the LLM (logai/incident/error_samples.py)."""
from __future__ import annotations

from logai.features.template_activity import TemplateActivity
from logai.incident.error_samples import ErrorSamples, error_signature, redact
from logai.models import RawLog

NOW = 1_800_000_030.0  # mid-minute, so NOW - 5 is the current minute

JAVA_TRACE = (
    "2026-10-09 10:00:01 ERROR [http-1] Payment call failed orderId=884213 code=PAY_504\n"
    "com.acme.pay.GatewayException: upstream timeout\n"
    "\tat com.acme.pay.PaymentClient.call(PaymentClient.java:88)\n"
    "\tat com.acme.pay.Service.run(Service.java:12)\n"
    "Caused by: java.net.SocketTimeoutException: Read timed out\n"
)


def _raw(message, level="ERROR", ts=NOW - 60, service="pay", host="pay-03", **meta):
    return RawLog(timestamp=ts, service=service, level=level, message=message,
                  metadata={"host_name": host, **meta})


def test_signature_java_python_and_plain():
    sig = error_signature(JAVA_TRACE)
    assert sig["exception"] == "com.acme.pay.GatewayException"
    assert sig["caused_by"] == ["java.net.SocketTimeoutException"]
    assert sig["frame"] == "com.acme.pay.PaymentClient.call(PaymentClient.java:88)"
    assert sig["codes"] == ["code=PAY_504"]
    assert error_signature("ValueError: bad input")["exception"] == "ValueError"
    assert error_signature("ERROR handleError failed for user 42") is None


def test_redact_masks_credentials_keeps_ids():
    text = redact("login password=hunter2 token: abc Authorization: Bearer xyz.123 orderId=123")
    assert "hunter2" not in text and "abc" not in text and "xyz.123" not in text
    assert "orderId=123" in text


def test_only_error_levels_are_kept():
    samples = ErrorSamples()
    for level in ("INFO", "WARN", "DEBUG"):
        samples.record(_raw("x", level=level), "T1")
    assert samples.block("pay", None, {}, NOW) is None
    samples.record(_raw("boom FATAL", level="fatal"), "T1")
    samples.record(_raw("boom CRITICAL", level="CRITICAL"), "T2")
    assert samples.block("pay", None, {}, NOW)["summary"]["error_templates"] == 2


def test_block_signatures_samples_and_chronic():
    samples = ErrorSamples()
    samples.record(_raw(JAVA_TRACE, ts=NOW - 600), "T1")       # first seen
    samples.record(_raw(JAVA_TRACE.replace("884213", "9"), ts=NOW - 30), "T1")
    samples.record(_raw(JAVA_TRACE, ts=NOW - 5000), "T9")        # outside 30 min
    samples.record(_raw("disk quota exceeded", ts=NOW - 10), "T2")
    counts = {
        "T1": {"count_15m": 40, "count_30m": 50, "rate_per_min": 2.7,
               "baseline_per_min": 0.1, "ratio": 27.0},
        "T2": {"count_15m": 900, "count_30m": 1800, "rate_per_min": 60.0,
               "baseline_per_min": 58.0, "ratio": 1.0},
    }
    block = samples.block("pay", None, counts, NOW,
                          first_seen=lambda t: {"T1": NOW - 600}.get(t))
    assert block["summary"]["error_templates"] == 2
    assert block["summary"]["new_error_templates_1h"] == 1
    first, chronic = block["signatures"]
    assert first["exception"] == "com.acme.pay.GatewayException"
    assert first["templates"] == ["T1"] and first["chronic"] is False and first["ratio"] == 27.0
    assert first["first_seen_minutes_ago"] == 10
    assert len(first["samples"]) == 2 and "884213" in first["samples"][0]
    assert chronic["templates"] == ["T2"] and chronic["chronic"] is True
    assert block["top_values"]["host"] == [["pay-03", 3]]


def test_top_values_skip_ids_and_scope_by_template():
    samples = ErrorSamples()
    for i in range(3):
        samples.record(_raw("err", trace_id=f"t{i}", moduleCode="M1"), "T1")
    samples.record(_raw("other"), "T2")
    block = samples.block("pay", ["T1"], {}, NOW)
    assert "trace_id" not in block["top_values"]
    assert block["top_values"]["moduleCode"] == [["M1", 3]]
    assert block["summary"]["error_templates"] == 1
    assert samples.block("other-service", None, {}, NOW) is None


def test_timeline_series():
    activity = TemplateActivity()
    activity.record("pay", "T1", NOW - 3600)   # older than 30 min
    activity.record("pay", "T1", NOW - 5)
    activity.record("pay", "T1", NOW - 5)
    activity.record("pay", "T2", NOW - 125)
    activity.record("api", "T1", NOW - 5)      # other service
    series = activity.series(NOW, "pay", ["T1", "T2"])
    assert len(series) == 30 and sum(series) == 3
    assert series[-1] == 2


def test_block_includes_timeline_when_activity_given():
    samples, activity = ErrorSamples(), TemplateActivity()
    samples.record(_raw("err", ts=NOW - 5), "T1")
    activity.record("pay", "T1", NOW - 5)
    assert samples.block("pay", None, {}, NOW, activity=activity)["timeline_per_min"][-1] == 1
