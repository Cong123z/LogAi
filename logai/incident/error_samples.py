"""Recent ERROR/FATAL log lines, kept raw, as the core of LLM evidence.

Templates are wording only: the preprocessor masks ids, numbers and hosts, so
the exception, its cause and the failing host never reach the LLM through
them. This module keeps the latest raw error lines per (service, template),
pulls a best-effort exception signature out of each (exception class,
"Caused by" chain, first stack frame, error codes) and builds the `errors`
evidence block: signatures ranked elevated-before-chronic, with samples, the
hosts/modules they come from and a per-minute timeline.

Written by the poll thread, read by the control thread.
"""
from __future__ import annotations

import re
import threading
from collections import Counter, deque
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

from logai.features.template_activity import RATE_FLOOR_PER_MIN, TemplateActivity, recent
from logai.models import DEFAULT_LEVEL, LEVEL_RANK, RawLog

SAMPLES_PER_TEMPLATE = 50
SAMPLE_CHARS = 400
# Stack traces can be long; the signature is searched in this much text.
SCAN_CHARS = 4000
WINDOW_SECONDS = 1800
NEW_TEMPLATE_SECONDS = 3600
MAX_SIGNATURES = 5
MAX_CAUSES = 3
MAX_CODES = 3
MAX_META_KEYS = 8
MAX_TOP_KEYS = 6
TOP_VALUES = 3
META_VALUE_CHARS = 100
# Running at less than this many times its normal rate = the usual noise.
CHRONIC_RATIO = 1.5
HOST_KEY = "host_name"

ERRORS_PROMPT = (
    " errors, when present, holds the recent ERROR/FATAL log lines grouped by "
    "exception signature (exception, caused_by chain, first stack frame, error "
    "codes) with raw samples: base the diagnosis on them first and name the "
    "exception or error code in your reasoning. A signature marked chronic runs "
    "at its usual rate; do not blame it unless nothing else explains the "
    "problem. errors.timeline_per_min counts error-template events per minute "
    "over the last 30 minutes, oldest first, showing when they started; "
    "errors.top_values shows whether they concentrate on one host or module. "
    "correlated_alerts, when present, lists other log windows alerting at the "
    "same time, which may point to an upstream or shared cause."
)

_CLASS = r"(?:[A-Za-z_$][\w$]*\.)*[A-Z][\w$]*"
_EXCEPTION_RE = re.compile(rf"\b({_CLASS}(?:Exception|Error|Fault))\b")
_CAUSED_BY_RE = re.compile(rf"Caused by:\s*({_CLASS})")
_FRAME_RE = re.compile(r"\bat\s+((?:[\w$]+\.)+[\w$<>]+\([^)\n]*\))")
_CODE_RE = re.compile(
    r"\b(ERR_[A-Z0-9_]+|(?:error_?code|err_?code|code|http_?status|status)"
    r"\s*[=:]\s*\"?[\w.-]+)",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|authorization)"
    r"(\"?\s*[=:]\s*\"?)([^\s,;&\"']+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)

# (timestamp, redacted text, signature, host, metadata)
Sample = Tuple[float, str, Optional[Dict[str, Any]], Optional[str], Dict[str, str]]


def redact(text: str) -> str:
    """Mask credentials; ids and error codes stay, they are the useful part."""
    text = _BEARER_RE.sub(r"\1 ***", text)
    return _SECRET_RE.sub(r"\1\2***", text)


def error_signature(message: str) -> Optional[Dict[str, Any]]:
    """Exception, Caused-by chain, first stack frame and error codes found in
    a raw message; None when it names no exception and no cause."""
    exception = _EXCEPTION_RE.search(message)
    causes = list(dict.fromkeys(_CAUSED_BY_RE.findall(message)))[:MAX_CAUSES]
    if exception is None and not causes:
        return None
    frame = _FRAME_RE.search(message)
    codes = list(dict.fromkeys(m.strip() for m in _CODE_RE.findall(message)))[:MAX_CODES]
    return {
        "exception": exception.group(1) if exception else causes[0],
        "caused_by": causes,
        "frame": frame.group(1) if frame else None,
        "codes": codes,
    }


def _signature_key(sample: Sample, template_id: str) -> Tuple[str, str]:
    signature = sample[2]
    if signature is None:
        return "", template_id
    return signature["exception"], (signature["caused_by"] or [""])[-1]


class ErrorSamples:
    def __init__(self) -> None:
        # ponytail: grows with the number of error templates ever seen; prune
        # keys idle for 24 h if memory matters.
        self._recent: Dict[Tuple[str, str], Deque[Sample]] = {}
        self._first: Dict[Tuple[str, str], Sample] = {}
        self._lock = threading.Lock()

    def record(self, raw: RawLog, template_id: str) -> None:
        level = str(raw.level or DEFAULT_LEVEL).strip().upper()
        if LEVEL_RANK.get(level, LEVEL_RANK[DEFAULT_LEVEL]) < LEVEL_RANK["ERROR"]:
            return
        message = str(raw.message or "")[:SCAN_CHARS]
        metadata = raw.metadata or {}
        meta = {
            str(key): str(value)[:META_VALUE_CHARS]
            for key, value in list(metadata.items())
            if key != HOST_KEY and isinstance(value, (str, int, float, bool))
        }
        sample: Sample = (
            float(raw.timestamp),
            redact(message[:SAMPLE_CHARS * 2])[:SAMPLE_CHARS],
            error_signature(message),
            str(metadata[HOST_KEY]) if metadata.get(HOST_KEY) else None,
            dict(list(meta.items())[:MAX_META_KEYS]),
        )
        key = (raw.service, template_id)
        with self._lock:
            buffer = self._recent.get(key)
            if buffer is None:
                buffer = self._recent[key] = deque(maxlen=SAMPLES_PER_TEMPLATE)
            buffer.append(sample)
            self._first.setdefault(key, sample)

    def block(
        self,
        service: str,
        template_ids: Optional[Iterable[str]],
        counts: Dict[str, Dict[str, Any]],
        now: float,
        first_seen: Callable[[str], Optional[float]] = lambda template_id: None,
        activity: Optional[TemplateActivity] = None,
    ) -> Optional[Dict[str, Any]]:
        """The `errors` evidence block for `service`, limited to
        `template_ids` (None: every template of the service), or None when
        none of them logged an error in the last 30 minutes. `counts` are
        TemplateActivity.counts(); they give rates and chronic/elevated."""
        since = now - WINDOW_SECONDS
        with self._lock:
            if template_ids is None:
                keys = [key for key in self._recent if key[0] == service]
            else:
                keys = [(service, tid) for tid in dict.fromkeys(template_ids)
                        if (service, tid) in self._recent]
            by_template = {key[1]: [s for s in self._recent[key] if s[0] >= since] for key in keys}
            firsts = {key[1]: self._first.get(key) for key in keys}
        by_template = {tid: samples for tid, samples in by_template.items() if samples}
        if not by_template:
            return None

        groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for tid, samples in by_template.items():
            for sample in samples:
                group = groups.setdefault(_signature_key(sample, tid), {
                    "signature": sample[2], "samples": [], "per_template": Counter(),
                })
                group["samples"].append((tid, sample))
                group["per_template"][tid] += 1

        signatures = [
            self._signature_entry(key, group, by_template, firsts, counts, now, since, first_seen)
            for key, group in groups.items()
        ]
        signatures.sort(key=lambda entry: (entry["chronic"], -entry["count_30m"]))
        block: Dict[str, Any] = {
            "summary": {
                "error_templates": len(by_template),
                "signatures": len(groups),
                "error_events_15m": sum(recent(counts, t)["count_15m"] for t in by_template),
                "error_events_30m": sum(recent(counts, t)["count_30m"] for t in by_template),
                "new_error_templates_1h": sum(
                    1 for t in by_template
                    if (seen := first_seen(t)) is not None and now - seen < NEW_TEMPLATE_SECONDS
                ),
            },
            "signatures": signatures[:MAX_SIGNATURES],
            "top_values": _top_values(s for samples in by_template.values() for s in samples),
        }
        if activity is not None:
            block["timeline_per_min"] = activity.series(now, service, list(by_template))
        return block

    @staticmethod
    def _signature_entry(
        key, group, by_template, firsts, counts, now, since, first_seen,
    ) -> Dict[str, Any]:
        # A template's events are shared among its signatures by sample share,
        # so a template whose <*> covers two exceptions is not counted twice.
        count_15m = count_30m = rate = baseline = 0.0
        known = True
        for tid, n in group["per_template"].items():
            share = n / len(by_template[tid])
            numbers = recent(counts, tid)
            count_15m += numbers["count_15m"] * share
            count_30m += numbers["count_30m"] * share
            rate += numbers["rate_per_min"] * share
            if numbers["baseline_per_min"] is None:
                known = False
            else:
                baseline += numbers["baseline_per_min"] * share
        ratio = round(rate / max(baseline, RATE_FLOOR_PER_MIN), 1) if known else None

        ordered = sorted(group["samples"], key=lambda item: item[1][0])
        first_samples = [
            sample for tid in group["per_template"]
            if (sample := firsts.get(tid)) is not None and sample[0] >= since
            and _signature_key(sample, tid) == key
        ]
        first = min(first_samples, key=lambda s: s[0]) if first_samples else ordered[0][1]
        texts = [first[1]]
        for _, sample in reversed(ordered):
            if sample[1] not in texts:
                texts.append(sample[1])
                break
        seen = [s for tid in group["per_template"] if (s := first_seen(tid)) is not None]
        return {
            **(group["signature"] or {}),
            "templates": sorted(group["per_template"]),
            "count_15m": round(count_15m),
            "count_30m": max(round(count_30m), len(group["samples"])),
            "rate_per_min": round(rate, 2),
            "baseline_per_min": round(baseline, 3) if known else None,
            "ratio": ratio,
            "chronic": ratio is not None and ratio < CHRONIC_RATIO,
            "first_seen_minutes_ago": round((now - min(seen)) / 60.0) if seen else None,
            "samples": texts,
        }


def _top_values(samples: Iterable[Sample]) -> Dict[str, List[List[Any]]]:
    """Most common host and metadata values among the samples; a key whose
    values are all different (an id) says nothing and is left out."""
    values: Dict[str, Counter] = {}
    for sample in samples:
        if sample[3]:
            values.setdefault("host", Counter())[sample[3]] += 1
        for key, value in sample[4].items():
            values.setdefault(key, Counter())[value] += 1
    top: Dict[str, List[List[Any]]] = {}
    for key in sorted(values, key=lambda k: (k != "host", -sum(values[k].values()), k)):
        counter = values[key]
        total = sum(counter.values())
        if total > 1 and len(counter) == total:
            continue
        top[key] = [[value, count] for value, count in counter.most_common(TOP_VALUES)]
        if len(top) == MAX_TOP_KEYS:
            break
    return top
