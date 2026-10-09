"""Central configuration for LogAI Engine.

Loaded from a YAML file (default: config.yaml at repo root) with environment
variable overrides for anything that is typically secret / deployment
specific (ES host, credentials, ports).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

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
    poll_interval_seconds: float = 1.0
    batch_size: int = 500
    request_timeout_seconds: float = 30.0
    # How often selected index patterns are re-resolved to concrete indices
    # (and the available-index list republished for the web).
    index_refresh_seconds: float = 30.0


@dataclass
class Drain3Config:
    persistence_path: str = "data/drain3_state.bin"
    sim_threshold: float = 0.6
    depth: int = 4
    max_children: int = 100
    # Wording-only normalisation before Drain3 (logai/parsing/preprocessor.py):
    # strips the date/time/LEVEL/[thread] prefix and all timestamps, masks
    # ids and numbers, keeps words.
    preprocess: bool = True
    max_chars: int = 8192
    max_tokens: int = 40
    level_search_tokens: int = 5
    # Optional extra Drain3 MaskingInstruction rules applied after preprocess.
    masking_rules: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class EmbeddingConfig:
    model_name: str = "BAAI/bge-m3"
    endpoint: str = ""
    api_format: str = "openai"
    api_key: str | None = None
    dimension: int = 1024
    batch_size: int = 64
    timeout_seconds: float = 60.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5
    # No local tokenizer for the remote model, so token count is approximated
    # as len(text) // 4. Templates estimated above this are truncated before
    # being sent, to avoid a server silently dropping an oversized item from
    # its response (which otherwise surfaces as an index mismatch error).
    max_template_tokens: int = 1000

    def validate(self, *, require_endpoint: bool = True) -> None:
        if require_endpoint and not self.endpoint.strip():
            raise ValueError(
                "Remote embedding endpoint is required; set "
                "LOGAI_EMBEDDING_ENDPOINT"
            )
        if self.api_format.lower() not in {"openai", "tei"}:
            raise ValueError(
                "LOGAI_EMBEDDING_API_FORMAT must be 'openai' or 'tei'"
            )
        if self.dimension <= 0:
            raise ValueError("LOGAI_EMBEDDING_DIMENSION must be positive")
        if self.batch_size <= 0:
            raise ValueError("LOGAI_EMBEDDING_BATCH_SIZE must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("LOGAI_EMBEDDING_TIMEOUT_SECONDS must be positive")
        if self.max_retries < 0:
            raise ValueError("LOGAI_EMBEDDING_MAX_RETRIES cannot be negative")
        if self.max_template_tokens <= 0:
            raise ValueError("max_template_tokens must be positive")


@dataclass
class LLMConfig:
    # OpenAI-compatible /chat/completions URL; empty disables incident analysis.
    endpoint: str = ""
    api_key: str | None = None
    model: str = ""
    timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    max_candidates: int = 5
    max_tokens: int = 800
    # One job (all retries included) gives up after this long, so a stuck
    # endpoint cannot hold the single LLM worker for minutes.
    job_deadline_seconds: float = 150.0


@dataclass
class ClusteringConfig:
    # Conservative defaults: prefer compact, high-confidence groups over
    # assigning borderline templates to an existing group.
    min_cluster_size: int = 4
    min_samples: int = 2
    metric: str = "euclidean"
    # cosine similarity threshold used at realtime to assign a new/unknown
    # template embedding to an existing group representation
    assignment_similarity_threshold: float = 0.88


@dataclass
class DocMatcherConfig:
    corpus_path: str = "data/documentation_corpus.json"
    seed_corpus_path: str = "docs/documentation_corpus.yaml"
    overrides_path: str = "data/documentation_overrides.json"
    status_path: str = "data/documentation_status.json"
    similarity_threshold: float = 0.75
    refresh_interval_seconds: float = 5.0


@dataclass
class FeatureConfig:
    windows_seconds: tuple = (10, 60, 300)  # 10s, 1m, 5m
    # Baseline span: closed 10-second and 1-minute buckets over this many
    # seconds feed the z-score / burstiness / slope features. Time-based, so a
    # sustained incident does not become "normal" after a few events.
    baseline_seconds: float = 1800.0
    history_retention_seconds: float = 3600.0
    # Stabilize ratio-based features when a group's baseline is near zero.
    rate_floor: float = 0.2


@dataclass
class AnomalyConfig:
    contamination: float = 0.05
    n_estimators: int = 100
    random_state: int = 42
    min_training_samples: int = 30
    score_alert_threshold: float = 0.6
    # Micro-batch inference at the predict phase: accumulate feature vectors and
    # flush->predict when the buffer reaches `predict_batch_size` events OR
    # `predict_max_wait_seconds` has elapsed since the buffer's first event
    # (whichever comes first). Both are configurable via the `anomaly:` section.
    predict_batch_size: int = 500
    predict_max_wait_seconds: float = 1.0


@dataclass
class AlertConfig:
    warm_consecutive: int = 2
    alert_consecutive: int = 3
    cool_consecutive: int = 3
    score_high: float = 0.6
    score_low: float = 0.4
    # High anomaly scores need this many events in the current 1-minute
    # window before they may escalate alert state.
    min_events_1m: int = 3
    # Idle-tick threshold: a non-NORMAL group that has been silent for at least
    # this many seconds is periodically re-evaluated so a stuck ALERTING alert
    # can cool down to NORMAL even with zero new events. Also the tick throttle
    # interval. Configurable via the `alert:` section of config.yaml.
    idle_eval_seconds: float = 5.0


@dataclass
class TrainingConfig:
    batch_size: int = 2000
    max_docs: int = 200_000
    lookback_seconds: float = 7 * 24 * 3600
    dedup_buffer_size: int = 10_000
    # Templates unseen this long are pruned at retrain (unless an override uses them).
    template_ttl_days: float = 30


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
    grouping_overrides_file: str = "grouping_overrides.json"
    grouping_status_file: str = "grouping_status.json"
    incident_analysis_file: str = "incident_analysis.json"
    service_analysis_file: str = "service_analysis.json"
    analysis_requests_file: str = "analysis_requests.json"
    llm_profiles_file: str = "llm_profiles.json"
    template_triage_file: str = "template_triage.json"
    analysis_history_file: str = "analysis_history.jsonl"
    incident_cases_file: str = "incident_cases.json"
    template_activity_file: str = "template_activity.json"
    es_index_selection_file: str = "es_index_selection.json"
    es_index_status_file: str = "es_index_status.json"
    group_lineage_file: str = "group_lineage.json"
    retrain_schedule_file: str = "retrain_schedule.json"
    retrain_status_file: str = "retrain_status.json"


@dataclass
class GroupingConfig:
    heartbeat_interval_seconds: float = 5.0
    engine_status_stale_seconds: float = 45.0


@dataclass
class ReliabilityConfig:
    max_retries: int = 5
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    dedup_ttl_seconds: float = 86400.0
    dedup_max_size: int = 200_000
    dedup_flush_interval_seconds: float = 30.0


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
    grouping: GroupingConfig = field(default_factory=GroupingConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)


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
    """Load config.yaml (if present) and merge with defaults.Load config

    Environment variables always win for ES connection details so secretss
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
    if os.environ.get("LOGAI_ES_INDEX"):
        cfg.elasticsearch.index = os.environ["LOGAI_ES_INDEX"]
    if os.environ.get("LOGAI_ES_USER"):
        cfg.elasticsearch.username = os.environ["LOGAI_ES_USER"]
    if os.environ.get("LOGAI_ES_PASSWORD"):
        cfg.elasticsearch.password = os.environ["LOGAI_ES_PASSWORD"]
    if os.environ.get("LOGAI_METRICS_PORT"):
        cfg.metrics.http_port = int(os.environ["LOGAI_METRICS_PORT"])
    if os.environ.get("LOGAI_METRICS_HOST"):
        cfg.metrics.http_host = os.environ["LOGAI_METRICS_HOST"]
    if os.environ.get("LOGAI_STORAGE_BASE_DIR"):
        base_dir = os.environ["LOGAI_STORAGE_BASE_DIR"]
        cfg.storage.base_dir = base_dir
        cfg.storage.model_dir = str(Path(base_dir) / "models")
        cfg.drain3.persistence_path = str(Path(base_dir) / "drain3_state.bin")
        cfg.doc_matcher.corpus_path = str(Path(base_dir) / "documentation_corpus.json")
        cfg.doc_matcher.overrides_path = str(Path(base_dir) / "documentation_overrides.json")
        cfg.doc_matcher.status_path = str(Path(base_dir) / "documentation_status.json")
    if os.environ.get("LOGAI_DOCUMENTATION_CORPUS_PATH"):
        cfg.doc_matcher.corpus_path = os.environ["LOGAI_DOCUMENTATION_CORPUS_PATH"]
    if os.environ.get("LOGAI_EMBEDDING_MODEL"):
        cfg.embedding.model_name = os.environ["LOGAI_EMBEDDING_MODEL"]
    if os.environ.get("LOGAI_EMBEDDING_ENDPOINT"):
        cfg.embedding.endpoint = os.environ["LOGAI_EMBEDDING_ENDPOINT"]
    if os.environ.get("LOGAI_EMBEDDING_API_FORMAT"):
        cfg.embedding.api_format = os.environ["LOGAI_EMBEDDING_API_FORMAT"]
    if os.environ.get("LOGAI_EMBEDDING_API_KEY"):
        cfg.embedding.api_key = os.environ["LOGAI_EMBEDDING_API_KEY"]
    if os.environ.get("LOGAI_EMBEDDING_DIMENSION"):
        cfg.embedding.dimension = int(os.environ["LOGAI_EMBEDDING_DIMENSION"])
    if os.environ.get("LOGAI_EMBEDDING_BATCH_SIZE"):
        cfg.embedding.batch_size = int(os.environ["LOGAI_EMBEDDING_BATCH_SIZE"])
    if os.environ.get("LOGAI_EMBEDDING_TIMEOUT_SECONDS"):
        cfg.embedding.timeout_seconds = float(
            os.environ["LOGAI_EMBEDDING_TIMEOUT_SECONDS"]
        )
    if os.environ.get("LOGAI_EMBEDDING_MAX_RETRIES"):
        cfg.embedding.max_retries = int(os.environ["LOGAI_EMBEDDING_MAX_RETRIES"])
    if os.environ.get("LOGAI_EMBEDDING_RETRY_BACKOFF_SECONDS"):
        cfg.embedding.retry_backoff_seconds = float(
            os.environ["LOGAI_EMBEDDING_RETRY_BACKOFF_SECONDS"]
        )
    if os.environ.get("LOGAI_LLM_ENDPOINT"):
        cfg.llm.endpoint = os.environ["LOGAI_LLM_ENDPOINT"]
    if os.environ.get("LOGAI_LLM_API_KEY"):
        cfg.llm.api_key = os.environ["LOGAI_LLM_API_KEY"]
    if os.environ.get("LOGAI_LLM_MODEL"):
        cfg.llm.model = os.environ["LOGAI_LLM_MODEL"]

    return cfg
