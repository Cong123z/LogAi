"""Documentation matcher: cosine similarity between a group centroid and the
embeddings of a documentation corpus (plan section 3.7 / 4.5).

The documentation corpus is a simple YAML file (docs/documentation_corpus.yaml)
of {id, title, text, error_code} entries - swap this loader out for a real
docs/KB source (Confluence, runbook repo, etc.) without touching callers.
"""
from __future__ import annotations

import hashlib
import logging
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
        self.reload()

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
                return True

            raw = yaml.safe_load(content.decode("utf-8"))
            new_entries = self._validate_entries(raw)
            if not new_entries:
                raise ValueError("Documentation corpus must contain at least one entry")

            new_embeddings = np.asarray(
                self.embedder.embed([entry.text for entry in new_entries])
            )
            self._validate_embeddings(new_entries, new_embeddings)
        except Exception as exc:  # noqa: BLE001 - malformed corpus/model is recoverable
            self.last_error = str(exc)
            logger.warning(
                "Documentation corpus refresh failed; keeping last known-good snapshot: %s",
                exc,
            )
            return False

        self.entries = new_entries
        self.embeddings = new_embeddings
        self.ready = True
        self.last_error = None
        self._corpus_digest = corpus_digest
        self._match_cache.clear()

        try:
            self._cache_store.save(
                {"entries": self.entries, "embeddings": self.embeddings}
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
