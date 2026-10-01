"""Remote dense embedding client used by training and realtime pipelines."""
from __future__ import annotations

import logging
import time
from typing import List, Tuple

import numpy as np
import requests

from logai.config import EmbeddingConfig

logger = logging.getLogger(__name__)

# No local tokenizer for the remote model (BAAI/bge-m3 or whatever is
# configured) - approximate token count from character length. This is a
# conservative heuristic (~4 chars/token for latin-script text), not an
# exact count; it only needs to keep requests safely under the server's
# real limit, not match it exactly.
_CHARS_PER_TOKEN = 4


class EmbeddingServiceError(RuntimeError):
    """Raised when the remote embedding service cannot return valid vectors."""


class TemplateEmbedder:
    _RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

    def __init__(
        self,
        config: EmbeddingConfig,
        session: requests.Session | None = None,
    ):
        self.config = config
        self.config.validate(require_endpoint=False)
        self._session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _request(self, texts: List[str]) -> object:
        self.config.validate(require_endpoint=True)
        api_format = self.config.api_format.lower()
        if api_format == "tei":
            body = {"inputs": texts, "normalize": True}
        else:
            body = {
                "model": self.config.model_name,
                "input": texts,
                "encoding_format": "float",
            }

        attempts = self.config.max_retries + 1
        last_error: Exception | None = None
        completed_attempts = 0
        for attempt in range(attempts):
            response = None
            completed_attempts = attempt + 1
            try:
                response = self._session.post(
                    self.config.endpoint,
                    headers=self._headers(),
                    json=body,
                    timeout=self.config.timeout_seconds,
                )
                if (
                    response.status_code in self._RETRYABLE_STATUS_CODES
                    and attempt + 1 < attempts
                ):
                    last_error = EmbeddingServiceError(
                        f"Embedding service returned HTTP {response.status_code}"
                    )
                else:
                    response.raise_for_status()
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise EmbeddingServiceError(
                            "Embedding service returned invalid JSON"
                        ) from exc
            except (requests.RequestException, EmbeddingServiceError) as exc:
                last_error = exc
                status_code = getattr(response, "status_code", None)
                if (
                    attempt + 1 >= attempts
                    or (
                        status_code is not None
                        and status_code not in self._RETRYABLE_STATUS_CODES
                    )
                ):
                    break

            delay = self.config.retry_backoff_seconds * (2 ** attempt)
            if delay > 0:
                time.sleep(delay)

        raise EmbeddingServiceError(
            f"Embedding request failed after {completed_attempts} attempt(s): "
            f"{last_error}"
        ) from last_error

    def _parse_response(self, payload: object, expected_count: int) -> np.ndarray:
        if self.config.api_format.lower() == "tei":
            raw_vectors = payload
            if isinstance(payload, dict):
                raw_vectors = payload.get("embeddings")
        else:
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise EmbeddingServiceError(
                    "OpenAI embedding response must contain a data array"
                )
            items = payload["data"]
            try:
                ordered = sorted(items, key=lambda item: int(item["index"]))
                indexes = [int(item["index"]) for item in ordered]
                if indexes != list(range(expected_count)):
                    missing = sorted(set(range(expected_count)) - set(indexes))
                    duplicates = sorted({i for i in indexes if indexes.count(i) > 1})
                    logger.error(
                        "Embedding response index mismatch: expected %d item(s), "
                        "received %d | missing_indexes=%s duplicate_indexes=%s "
                        "raw_indexes=%s",
                        expected_count,
                        len(items),
                        missing[:20],
                        duplicates[:20],
                        indexes[:50],
                    )
                    raise ValueError("response indexes are incomplete or duplicated")
                raw_vectors = [item["embedding"] for item in ordered]
            except (KeyError, TypeError, ValueError) as exc:
                raise EmbeddingServiceError(
                    "OpenAI embedding response contains invalid items"
                ) from exc

        try:
            vectors = np.asarray(raw_vectors, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise EmbeddingServiceError(
                "Embedding response does not contain numeric vectors"
            ) from exc

        expected_shape = (expected_count, self.config.dimension)
        if vectors.shape != expected_shape:
            raise EmbeddingServiceError(
                f"Embedding response has shape {vectors.shape}; expected {expected_shape}"
            )
        if not np.isfinite(vectors).all():
            raise EmbeddingServiceError("Embedding response contains non-finite values")

        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if np.any(norms <= np.finfo(np.float32).eps):
            raise EmbeddingServiceError("Embedding response contains a zero vector")
        return vectors / norms

    def _truncate_oversized(self, texts: List[str]) -> List[str]:
        """Caps each text's estimated token count at config.max_template_tokens.

        Returns a new list (the caller's list/its callers' id-alignment is
        never mutated); only the text sent over the wire is shortened.
        """
        max_chars = self.config.max_template_tokens * _CHARS_PER_TOKEN
        truncated: List[str] = []
        examples: List[Tuple[str, int]] = []
        for text in texts:
            if len(text) > max_chars:
                examples.append((text[:60], len(text) // _CHARS_PER_TOKEN))
                truncated.append(text[:max_chars])
            else:
                truncated.append(text)

        if examples:
            logger.warning(
                "Truncated %d/%d template(s) exceeding max_template_tokens=%d "
                "(~%d chars); examples (preview, ~original tokens): %s",
                len(examples),
                len(texts),
                self.config.max_template_tokens,
                max_chars,
                [f"{preview!r}~{tokens}tok" for preview, tokens in examples[:3]],
            )
        return truncated

    def embed(self, texts: List[str]) -> np.ndarray:
        """Returns an (n, d) L2-normalized embedding matrix so that a plain
        dot product equals cosine similarity."""
        if not texts:
            return np.zeros((0, self.config.dimension), dtype=np.float32)

        texts = self._truncate_oversized(texts)

        total_batches = (len(texts) + self.config.batch_size - 1) // self.config.batch_size
        batches = []
        for batch_index, start in enumerate(range(0, len(texts), self.config.batch_size), start=1):
            batch = texts[start:start + self.config.batch_size]
            t_batch = time.time()
            try:
                batches.append(self._parse_response(self._request(batch), len(batch)))
            except Exception:
                lengths = [len(text) for text in batch]
                logger.error(
                    "Embedding batch %d/%d failed: %d text(s), char_len min=%d "
                    "max=%d avg=%.0f | first=%r last=%r",
                    batch_index,
                    total_batches,
                    len(batch),
                    min(lengths),
                    max(lengths),
                    sum(lengths) / len(lengths),
                    batch[0][:80],
                    batch[-1][:80],
                )
                raise
            logger.info(
                "Embedding batch %d/%d complete: %d text(s) in %.2fs",
                batch_index,
                total_batches,
                len(batch),
                time.time() - t_batch,
            )
        return np.vstack(batches)

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]
