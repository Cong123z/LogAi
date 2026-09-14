"""Core data contracts shared across training & realtime pipelines.

These mirror the "Data Contract" and "State cần lưu" sections of the
implementation plan.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


def now_ts() -> float:
    return time.time()


@dataclass
class RawLog:
    timestamp: float           # epoch seconds (UTC)
    service: str
    level: str
    message: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    es_index: Optional[str] = None
    es_doc_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ParsedEvent:
    raw: RawLog
    template_id: str
    template: str
    parameters: List[str]
    is_new_template: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.raw.event_id,
            "timestamp": self.raw.timestamp,
            "service": self.raw.service,
            "level": self.raw.level,
            "template_id": self.template_id,
            "template": self.template,
            "parameters": self.parameters,
            "is_new_template": self.is_new_template,
        }


@dataclass
class TemplateState:
    template_id: str
    template_text: str
    service: str
    module: str = ""
    first_seen: float = field(default_factory=now_ts)
    last_seen: float = field(default_factory=now_ts)
    event_count: int = 0
    group_id: Optional[str] = None


@dataclass
class GroupState:
    group_id: str
    service: str = ""
    module: str = ""
    template_ids: List[str] = field(default_factory=list)
    representative_template: str = ""
    error_code: str = ""
    documented: bool = False
    documentation_id: Optional[str] = None
    confidence: float = 0.0
    severity: str = "unknown"
    first_seen: float = field(default_factory=now_ts)
    last_seen: float = field(default_factory=now_ts)
    event_count: int = 0
    active: bool = True


@dataclass
class GroupedEvent:
    parsed: ParsedEvent
    group_id: str
    group_similarity: float = 1.0  # 1.0 for known-template direct mapping


@dataclass
class WindowState:
    group_id: str
    window_start: float
    window_end: float
    count: int = 0
    rate: float = 0.0
    rolling_mean: float = 0.0
    rolling_std: float = 0.0
    growth_rate: float = 0.0
    z_score: float = 0.0
    burstiness: float = 0.0


@dataclass
class FeatureVector:
    group_id: str
    timestamp: float
    z_score_10s: float = 0.0
    z_score_1m: float = 0.0
    short_growth_rate: float = 1.0
    growth_rate: float = 1.0
    burstiness_10s: float = 0.0
    rate_delta_norm: float = 0.0
    slope_norm: float = 0.0
    spike_ratio_10s: float = 1.0
    # Alert eligibility metadata. This is deliberately excluded from
    # as_vector() so the Isolation Forest input remains eight-dimensional.
    count_1m: int | None = None

    def as_vector(self) -> List[float]:
        return [
            self.z_score_10s,
            self.z_score_1m,
            self.short_growth_rate,
            self.growth_rate,
            self.burstiness_10s,
            self.rate_delta_norm,
            self.slope_norm,
            self.spike_ratio_10s,
        ]

    @staticmethod
    def feature_names() -> List[str]:
        return [
            "z_score_10s",
            "z_score_1m",
            "short_growth_rate",
            "growth_rate",
            "burstiness_10s",
            "rate_delta_norm",
            "slope_norm",
            "spike_ratio_10s",
        ]


class AlertStateEnum(str, Enum):
    NORMAL = "NORMAL"
    WARMING = "WARMING"
    ALERTING = "ALERTING"
    COOLING = "COOLING"


@dataclass
class AnomalyResult:
    group_id: str
    timestamp: float
    anomaly_score: float
    anomaly: bool
    model_version: str = "if-global-v3"
    count_1m: int | None = None


@dataclass
class AnomalyState:
    group_id: str
    timestamp: float
    anomaly_score: float = 0.0
    anomaly: bool = False
    consecutive_anomaly_count: int = 0
    alert_state: str = AlertStateEnum.NORMAL.value
    model_version: str = "if-global-v3"
