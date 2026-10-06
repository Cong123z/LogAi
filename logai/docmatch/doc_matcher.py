"""Documentation matcher: cosine similarity between a group centroid and the
embeddings of a documentation corpus (plan section 3.7 / 4.5).

The documentation corpus is a simple YAML file (docs/documentation_corpus.yaml)
of {id, title, text, error_code} entries - swap this loader out for a real
docs/KB source (Confluence, runbook repo, etc.) without touching callers.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import yaml

from logai.config import DocMatcherConfig
from logai.embedding.embedder import TemplateEmbedder
from logai.storage.base import PickleStore

logger = logging.getLogger(__name__)


@dataclass
class DocEntry:
    doc_id: str
    title: str
    text: str
    error_code: str = ""


@dataclass
class MatchResult:
    group_id: str
    documentation_id: Optional[str]
    similarity: float
    documented: bool
    error_code: str = ""


class DocumentationMatcher:
    def __init__(
        self,
        config: DocMatcherConfig,
        embedder: TemplateEmbedder,
        embeddings_cache_path: str | Path,
    ):
        self.config = config
        self.embedder = embedder
        self._cache_store = PickleStore(embeddings_cache_path)
        self.entries: List[DocEntry] = []
        self.embeddings: np.ndarray = np.zeros((0, 0))
        self.ready = False
        self.last_error: Optional[str] = None
        self._corpus_digest: Optional[str] = None
        self._match_cache: Dict[str, tuple[bytes, MatchResult]] = {}
        self._embedding_cache: Dict[str, np.ndarray] = {}
        self._lock = threading.RLock()
        self.last_reload_changed = False
        cached = self._cache_store.load({})
        if (
            isinstance(cached, dict)
            and cached.get("model_signature") == self._embedding_signature()
        ):
            cached_entries = cached.get("entries", [])
            cached_embeddings = cached.get("embeddings", [])
            try:
                for entry, vector in zip(cached_entries, cached_embeddings):
                    text = entry.text if isinstance(entry, DocEntry) else entry.get("text")
                    if isinstance(text, str):
                        self._embedding_cache[self._embedding_key(text)] = np.asarray(vector)
            except (AttributeError, TypeError):
                self._embedding_cache = {}
        self.reload()

    def _embedding_key(self, text: str) -> str:
        return hashlib.sha256(
            f"{self._embedding_signature()}\0{text}".encode("utf-8")
        ).hexdigest()

    def _embedding_signature(self) -> str:
        config = getattr(self.embedder, "config", None)
        model_name = str(getattr(config, "model_name", "default"))
        dimension = str(getattr(config, "dimension", "unknown"))
        return f"{model_name}:{dimension}"

    def reload(self) -> bool:
        """Build and atomically activate a validated corpus snapshot.

        A failed refresh never replaces the last known-good in-memory state.
        The persisted cache is only an optimization, so a cache write failure
        must not disable an otherwise valid matcher snapshot.
        """
        path = Path(self.config.corpus_path)
        try:
            if not path.exists():
                raise FileNotFoundError(f"Documentation corpus not found: {path}")

            content = path.read_bytes()
            corpus_digest = hashlib.sha256(content).hexdigest()
            if self.ready and corpus_digest == self._corpus_digest:
                self.last_error = None
                self.last_reload_changed = False
                return True

            if path.suffix.lower() == ".json":
                envelope = json.loads(content.decode("utf-8"))
                if not isinstance(envelope, dict):
                    raise ValueError("Documentation corpus JSON root must be an object")
                raw = envelope.get("entries", [])
            else:
                # Legacy/test compatibility. Runtime deployments use JSON.
                raw = yaml.safe_load(content.decode("utf-8"))
            new_entries = self._validate_entries(raw)
            if not new_entries and path.suffix.lower() != ".json":
                raise ValueError("Documentation corpus must contain at least one entry")
            if new_entries:
                missing_keys: list[str] = []
                missing_texts: list[str] = []
                for entry in new_entries:
                    key = self._embedding_key(entry.text)
                    if key not in self._embedding_cache and key not in missing_keys:
                        missing_keys.append(key)
                        missing_texts.append(entry.text)
                if missing_texts:
                    encoded = np.asarray(self.embedder.embed(missing_texts))
                    for key, vector in zip(missing_keys, encoded):
                        self._embedding_cache[key] = np.asarray(vector)
                new_embeddings = np.asarray([
                    self._embedding_cache[self._embedding_key(entry.text)]
                    for entry in new_entries
                ])
                self._validate_embeddings(new_entries, new_embeddings)
            else:
                new_embeddings = np.zeros((0, 0))
        except Exception as exc:  # noqa: BLE001 - malformed corpus/model is recoverable
            self.last_error = str(exc)
            self.last_reload_changed = False
            logger.warning(
                "Documentation corpus refresh failed; keeping last known-good snapshot: %s",
                exc,
            )
            return False

        with self._lock:
            self.entries = new_entries
            self.embeddings = new_embeddings
            self.ready = True
            self.last_error = None
            self._corpus_digest = corpus_digest
            self._match_cache.clear()
            self.last_reload_changed = True

        try:
            self._cache_store.save(
                {
                    "model_signature": self._embedding_signature(),
                    "entries": self.entries,
                    "embeddings": self.embeddings,
                }
            )
        except Exception as exc:  # noqa: BLE001 - cache is not source of truth
            logger.warning("Unable to persist documentation embedding cache: %s", exc)
        return True

    @staticmethod
    def _validate_entries(raw: Any) -> List[DocEntry]:
        if not isinstance(raw, list):
            raise ValueError("Documentation corpus root must be a list")

        entries: List[DocEntry] = []
        seen_ids: set[str] = set()
        for position, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Documentation entry {position} must be an object")

            doc_id = item.get("id")
            text = item.get("text")
            title = item.get("title", "")
            error_code = item.get("error_code", "")
            if not isinstance(doc_id, (str, int)) or not str(doc_id).strip():
                raise ValueError(f"Documentation entry {position} has an invalid id")
            normalized_id = str(doc_id).strip()
            if normalized_id in seen_ids:
                raise ValueError(f"Duplicate documentation id: {normalized_id}")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(
                    f"Documentation entry {normalized_id} has empty or invalid text"
                )
            if not isinstance(title, str) or not isinstance(error_code, str):
                raise ValueError(
                    f"Documentation entry {normalized_id} title/error_code must be strings"
                )

            seen_ids.add(normalized_id)
            entries.append(
                DocEntry(
                    doc_id=normalized_id,
                    title=title,
                    text=text,
                    error_code=error_code,
                )
            )
        return entries

    @staticmethod
    def _validate_embeddings(
        entries: List[DocEntry], embeddings: np.ndarray
    ) -> None:
        if embeddings.ndim != 2 or embeddings.shape[0] != len(entries):
            raise ValueError(
                "Documentation embeddings must be a 2D matrix with one row per entry"
            )
        if embeddings.shape[1] == 0 or not np.isfinite(embeddings).all():
            raise ValueError("Documentation embeddings contain invalid values")

    def match(self, group_id: str, centroid: np.ndarray) -> MatchResult:
        with self._lock:
            if not self.ready or len(self.entries) == 0:
                return MatchResult(group_id, None, 0.0, False)
            candidate = np.asarray(centroid)
            if (
                candidate.ndim != 1
                or candidate.shape[0] != self.embeddings.shape[1]
                or not np.isfinite(candidate).all()
            ):
                raise ValueError(
                    "Group centroid is invalid or incompatible with documentation embeddings"
                )
            fingerprint = hashlib.sha256(candidate.tobytes()).digest()
            cached = self._match_cache.get(group_id)
            if cached is not None and cached[0] == fingerprint:
                return cached[1]

            sims = self.embeddings @ candidate
            best_idx = int(np.argmax(sims))
            best_sim = float(sims[best_idx])
            if best_sim >= self.config.similarity_threshold:
                best = self.entries[best_idx]
                result = MatchResult(
                    group_id, best.doc_id, best_sim, True, best.error_code
                )
            else:
                result = MatchResult(group_id, None, best_sim, False)
            self._match_cache[group_id] = (fingerprint, result)
            return result

    def match_document(self, group_id: str, documentation_id: str, centroid: np.ndarray) -> MatchResult:
        """Force a specific document while retaining its real cosine similarity."""
        with self._lock:
            if not self.ready:
                return MatchResult(group_id, None, 0.0, False)
            index = next(
                (i for i, entry in enumerate(self.entries) if entry.doc_id == documentation_id),
                None,
            )
            if index is None:
                raise KeyError(documentation_id)
            candidate = np.asarray(centroid)
            if candidate.ndim != 1 or candidate.shape[0] != self.embeddings.shape[1]:
                raise ValueError("Group centroid is incompatible with documentation embeddings")
            similarity = float(np.dot(self.embeddings[index], candidate))
            entry = self.entries[index]
            return MatchResult(group_id, entry.doc_id, similarity, True, entry.error_code)

    def top_k(self, centroid: Any, k: int) -> List[tuple[DocEntry, float]]:
        """The k closest corpus entries to ``centroid``, most similar first;
        [] when nothing can be compared (no corpus, no/incompatible centroid)."""
        with self._lock:
            if not self.ready or not self.entries or centroid is None:
                return []
            candidate = np.asarray(centroid)
            if candidate.ndim != 1 or candidate.shape[0] != self.embeddings.shape[1]:
                return []
            sims = self.embeddings @ candidate
            return [
                (self.entries[i], float(sims[i])) for i in np.argsort(-sims)[:k]
            ]

    def match_all(
        self, group_centroids: Dict[str, np.ndarray]
    ) -> Dict[str, MatchResult]:
        results: Dict[str, MatchResult] = {}
        for group_id, centroid in group_centroids.items():
            try:
                results[group_id] = self.match(group_id, centroid)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "Skipping documentation match for group %s: %s", group_id, exc
                )
        return results
