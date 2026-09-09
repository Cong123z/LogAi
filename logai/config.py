"""Central configuration for LogAI Engine.

Loaded from a YAML file (default: config.yaml at repo root) with environment
variable overrides for anything that is typically secret / deployment
specific (ES host, credentials, ports).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.yaml"


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass
class ElasticsearchConfig:
    hosts: list = field(default_factory=lambda: ["http://localhost:9200"])
    index: str = "app-logs-*"
    username: str | None = None
    password: str | None = None
    poll_interval_seconds: float = 5.0
    batch_size: int = 500
    request_timeout_seconds: float = 30.0


@dataclass
class Drain3Config:
    persistence_path: str = "data/drain3_state.bin"
    sim_threshold: float = 0.4
    depth: int = 4
    max_children: int = 100


@dataclass
class EmbeddingConfig:
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    device: str = "cpu"
    batch_size: int = 64


@dataclass
class ClusteringConfig:
    min_cluster_size: int = 3
    min_samples: int = 1
    metric: str = "euclidean"
    # cosine similarity threshold used at realtime to assign a new/unknown
    # template embedding to an existing group representation
    assignment_similarity_threshold: float = 0.80


@dataclass
class DocMatcherConfig:
    corpus_path: str = "docs/documentation_corpus.yaml"
    similarity_threshold: float = 0.75
    refresh_interval_seconds: float = 300.0


@dataclass
class FeatureConfig:
    windows_seconds: tuple = (10, 60, 300)  # 10s, 1m, 5m
    rolling_window_points: int = 30
    history_retention_seconds: float = 3600.0


@dataclass
class AnomalyConfig:
    contamination: float = 0.05
    n_estimators: int = 100
    random_state: int = 42
    min_training_samples: int = 30
    score_alert_threshold: float = 0.6


@dataclass
class AlertConfig:
    warm_consecutive: int = 2
    alert_consecutive: int = 3
    cool_consecutive: int = 3
    score_high: float = 0.6
    score_low: float = 0.4


@dataclass
class TrainingConfig:
    batch_size: int = 2000
    max_docs: int = 200_000
    lookback_seconds: float = 7 * 24 * 3600
    dedup_buffer_size: int = 10_000


@dataclass
class StorageConfig:
    base_dir: str = "data"
    template_registry_file: str = "template_registry.json"
    template_embeddings_file: str = "template_embeddings.pkl"
    group_registry_file: str = "group_registry.json"
    group_centroids_file: str = "group_centroids.pkl"
    checkpoint_file: str = "checkpoint.json"
    training_checkpoint_file: str = "training_checkpoint.json"
    training_event_index_file: str = "training_event_index.jsonl"
    window_state_file: str = "window_state.json"
    anomaly_state_file: str = "anomaly_state.json"
    dedup_index_file: str = "dedup_index.json"
    dlq_file: str = "dlq.jsonl"
    model_dir: str = "data/models"
    doc_embeddings_file: str = "doc_embeddings.pkl"


@dataclass
class ReliabilityConfig:
    max_retries: int = 5
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    dedup_ttl_seconds: float = 86400.0
    dedup_max_size: int = 200_000


@dataclass
class MetricsConfig:
    http_port: int = 9108
    http_host: str = "0.0.0.0"


@dataclass
class AppConfig:
    elasticsearch: ElasticsearchConfig = field(default_factory=ElasticsearchConfig)
    drain3: Drain3Config = field(default_factory=Drain3Config)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    clustering: ClusteringConfig = field(default_factory=ClusteringConfig)
    doc_matcher: DocMatcherConfig = field(default_factory=DocMatcherConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    anomaly: AnomalyConfig = field(default_factory=AnomalyConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    reliability: ReliabilityConfig = field(default_factory=ReliabilityConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    training: TrainingConfig = field(default_factory=TrainingConfig)


def _merge_dataclass(instance: Any, overrides: Dict[str, Any]) -> Any:
    for key, value in (overrides or {}).items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge_dataclass(current, value)
        else:
            setattr(instance, key, value)
    return instance


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config.yaml (if present) and merge with defaults.

    Environment variables always win for ES connection details so secrets
    never need to live in the YAML file:
      LOGAI_ES_HOSTS (comma separated), LOGAI_ES_USER, LOGAI_ES_PASSWORD
    """
    cfg = AppConfig()
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _merge_dataclass(cfg, raw)

    if os.environ.get("LOGAI_ES_HOSTS"):
        cfg.elasticsearch.hosts = os.environ["LOGAI_ES_HOSTS"].split(",")
    if os.environ.get("LOGAI_ES_USER"):
        cfg.elasticsearch.username = os.environ["LOGAI_ES_USER"]
    if os.environ.get("LOGAI_ES_PASSWORD"):
        cfg.elasticsearch.password = os.environ["LOGAI_ES_PASSWORD"]
    if os.environ.get("LOGAI_METRICS_PORT"):
        cfg.metrics.http_port = int(os.environ["LOGAI_METRICS_PORT"])

    return cfg
