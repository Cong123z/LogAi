"""Configuration: dataclass defaults -> YAML (config.yaml, optional) -> LOGAI2_* env vars."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.yaml"


@dataclass
class ElasticsearchConfig:
    hosts: list = field(default_factory=lambda: ["http://elasticsearch:9200"])
    username: str = ""
    password: str = ""
    verify_certs: bool = True
    index: str = "app-logs-*"
    batch_size: int = 10000
    poll_interval_seconds: float = 1.0
    index_refresh_seconds: int = 60


@dataclass
class DatabaseConfig:
    dsn: str = "postgresql://logai:logai@postgres:5432/logai"


@dataclass
class ReaderConfig:
    module_field: str = "moduleCode"
    cache_size: int = 200000
    max_line_bytes: int = 8192
    upper_bound_seconds: int = 30


@dataclass
class FlushConfig:
    seconds: float = 2.0
    max_events: int = 50000


@dataclass
class StartConfig:
    days_back: int = 2


@dataclass
class BaselineConfig:
    timezone: str = "Asia/Ho_Chi_Minh"
    commit_time: str = "02:00"
    sigma_multiplier: float = 3.0
    young_sigma_multiplier: float = 4.0
    full_after_days: int = 5
    historic_after_minutes: int = 15


@dataclass
class DropConfig:
    min_mean: float = 20.0


@dataclass
class RetentionConfig:
    counts_days: int = 90
    alert_changes_days: int = 365


@dataclass
class AlertConfig:
    warm_consecutive: int = 2
    alert_consecutive: int = 3
    cool_consecutive: int = 3


@dataclass
class Drain3Config:
    sim_threshold: float = 0.6
    depth: int = 4
    max_children: int = 100


@dataclass
class MetricsConfig:
    host: str = "0.0.0.0"
    port: int = 9108


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 5555


@dataclass
class DocumentationConfig:
    seed_path: str = ""


@dataclass
class AppConfig:
    elasticsearch: ElasticsearchConfig = field(default_factory=ElasticsearchConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    reader: ReaderConfig = field(default_factory=ReaderConfig)
    flush: FlushConfig = field(default_factory=FlushConfig)
    start: StartConfig = field(default_factory=StartConfig)
    baseline: BaselineConfig = field(default_factory=BaselineConfig)
    floors: dict = field(default_factory=lambda: {"ERROR": 5, "WARN": 10})
    drop: DropConfig = field(default_factory=DropConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    drain3: Drain3Config = field(default_factory=Drain3Config)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    web: WebConfig = field(default_factory=WebConfig)
    documentation: DocumentationConfig = field(default_factory=DocumentationConfig)


def _merge_dataclass(instance: Any, overrides: Dict[str, Any]) -> Any:
    for key, value in (overrides or {}).items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge_dataclass(current, value)
        elif isinstance(current, dict) and isinstance(value, dict):
            # Partial YAML (e.g. only floors.ERROR) keeps the other defaults.
            current.update(value)
        else:
            setattr(instance, key, value)
    return instance


def load_config(path: str | None = None) -> AppConfig:
    cfg = AppConfig()
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            _merge_dataclass(cfg, yaml.safe_load(f) or {})

    env = os.environ.get
    if env("LOGAI2_DATABASE_DSN"):
        cfg.database.dsn = env("LOGAI2_DATABASE_DSN")
    if env("LOGAI2_ES_HOSTS"):
        cfg.elasticsearch.hosts = [h.strip() for h in env("LOGAI2_ES_HOSTS").split(",") if h.strip()]
    if env("LOGAI2_ES_USER"):
        cfg.elasticsearch.username = env("LOGAI2_ES_USER")
    if env("LOGAI2_ES_PASSWORD"):
        cfg.elasticsearch.password = env("LOGAI2_ES_PASSWORD")
    if env("LOGAI2_ES_VERIFY_CERTS"):
        cfg.elasticsearch.verify_certs = env("LOGAI2_ES_VERIFY_CERTS").strip().lower() not in {
            "0", "false", "no", "off"
        }
    if env("LOGAI2_ES_INDEX"):
        cfg.elasticsearch.index = env("LOGAI2_ES_INDEX")
    if env("LOGAI2_METRICS_PORT"):
        cfg.metrics.port = int(env("LOGAI2_METRICS_PORT"))
    if env("LOGAI2_WEB_PORT"):
        cfg.web.port = int(env("LOGAI2_WEB_PORT"))
    if env("LOGAI2_DOC_SEED_PATH"):
        cfg.documentation.seed_path = env("LOGAI2_DOC_SEED_PATH")
    return cfg
