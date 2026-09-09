"""Documentation matcher: cosine similarity between a group centroid and the
embeddings of a documentation corpus (plan section 3.7 / 4.5).

The documentation corpus is a simple YAML file (docs/documentation_corpus.yaml)
of {id, title, text, error_code} entries - swap this loader out for a real
docs/KB source (Confluence, runbook repo, etc.) without touching callers.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import yaml

from logai.config import DocMatcherConfig
from logai.embedding.embedder import TemplateEmbedder
from logai.storage.base import PickleStore


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
        self.embeddings: np.ndarray = np.zeros((0, 384))
        self.reload()

    def reload(self) -> None:
        path = Path(self.config.corpus_path)
        self.entries = []
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or []
            for item in raw:
                self.entries.append(
                    DocEntry(
                        doc_id=str(item["id"]),
                        title=item.get("title", ""),
                        text=item["text"],
                        error_code=item.get("error_code", ""),
                    )
                )
        if self.entries:
            texts = [e.text for e in self.entries]
            self.embeddings = self.embedder.embed(texts)
            self._cache_store.save(
                {"entries": self.entries, "embeddings": self.embeddings}
            )
        else:
            self.embeddings = np.zeros((0, 384))

    def match(self, group_id: str, centroid: np.ndarray) -> MatchResult:
        if len(self.entries) == 0:
            return MatchResult(group_id, None, 0.0, False)
        sims = self.embeddings @ centroid
        best_idx = int(np.argmax(sims))
        best_sim = float(sims[best_idx])
        if best_sim >= self.config.similarity_threshold:
            best = self.entries[best_idx]
            return MatchResult(
                group_id, best.doc_id, best_sim, True, best.error_code
            )
        return MatchResult(group_id, None, best_sim, False)

    def match_all(
        self, group_centroids: Dict[str, np.ndarray]
    ) -> Dict[str, MatchResult]:
        return {gid: self.match(gid, c) for gid, c in group_centroids.items()}
