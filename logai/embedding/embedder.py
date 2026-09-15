"""Template embedding via sentence-transformers/all-mpnet-base-v2
(plan section 3.4 / 4.4)."""
from __future__ import annotations

from typing import List

import numpy as np
from sentence_transformers import SentenceTransformer

from logai.config import EmbeddingConfig


class TemplateEmbedder:
    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._model = SentenceTransformer(config.model_name, device=config.device)

    def embed(self, texts: List[str]) -> np.ndarray:
        """Returns an (n, d) L2-normalized embedding matrix so that a plain
        dot product equals cosine similarity."""
        if not texts:
            return np.zeros((0, self._model.get_sentence_embedding_dimension()))
        vectors = self._model.encode(
            texts,
            batch_size=self.config.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors)

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]
