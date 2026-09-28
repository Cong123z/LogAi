"""Contract tests for the remote-only BGE-M3 embedding client."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import requests

from logai.config import EmbeddingConfig, load_config
from logai.embedding.embedder import EmbeddingServiceError, TemplateEmbedder


class FakeResponse:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


class TestRemoteEmbedder(unittest.TestCase):
    def test_endpoint_is_required(self):
        with self.assertRaisesRegex(ValueError, "LOGAI_EMBEDDING_ENDPOINT"):
            EmbeddingConfig().validate()

    def test_tei_batches_and_normalizes_vectors(self):
        session = FakeSession([
            FakeResponse(200, [[3.0, 4.0], [0.0, 2.0]]),
            FakeResponse(200, [[1.0, 1.0]]),
        ])
        config = EmbeddingConfig(
            endpoint="http://bge-m3:8080/embed",
            api_format="tei",
            dimension=2,
            batch_size=2,
        )

        vectors = TemplateEmbedder(config, session=session).embed(["a", "b", "c"])

        self.assertEqual(vectors.shape, (3, 2))
        for row in vectors.tolist():
            self.assertAlmostEqual(sum(value * value for value in row), 1.0, places=6)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["json"], {
            "inputs": ["a", "b"],
            "normalize": True,
        })

    def test_openai_response_is_ordered_and_authenticated(self):
        session = FakeSession([FakeResponse(200, {
            "data": [
                {"index": 1, "embedding": [0.0, 5.0]},
                {"index": 0, "embedding": [2.0, 0.0]},
            ]
        })])
        config = EmbeddingConfig(
            endpoint="https://embeddings.example/v1/embeddings",
            api_format="openai",
            api_key="secret-token",
            dimension=2,
        )

        vectors = TemplateEmbedder(config, session=session).embed(["first", "second"])

        self.assertEqual(vectors.tolist(), [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(
            session.calls[0]["headers"]["Authorization"],
            "Bearer secret-token",
        )
        self.assertEqual(session.calls[0]["json"]["model"], "BAAI/bge-m3")

    def test_retryable_status_is_retried(self):
        session = FakeSession([
            FakeResponse(503, {"error": "starting"}),
            FakeResponse(200, [[1.0, 0.0]]),
        ])
        config = EmbeddingConfig(
            endpoint="http://bge-m3:8080/embed",
            api_format="tei",
            dimension=2,
            max_retries=1,
            retry_backoff_seconds=0,
        )

        vectors = TemplateEmbedder(config, session=session).embed_one("hello")

        self.assertEqual(vectors.tolist(), [1.0, 0.0])
        self.assertEqual(len(session.calls), 2)

    def test_authentication_error_is_not_retried(self):
        session = FakeSession([FakeResponse(401, {"error": "unauthorized"})])
        config = EmbeddingConfig(
            endpoint="https://embeddings.example/v1/embeddings",
            api_format="openai",
            dimension=2,
            max_retries=3,
            retry_backoff_seconds=0,
        )

        with self.assertRaisesRegex(EmbeddingServiceError, "after 1 attempt"):
            TemplateEmbedder(config, session=session).embed_one("hello")
        self.assertEqual(len(session.calls), 1)

    def test_invalid_dimension_is_rejected(self):
        session = FakeSession([FakeResponse(200, [[1.0, 2.0, 3.0]])])
        config = EmbeddingConfig(
            endpoint="http://bge-m3:8080/embed",
            api_format="tei",
            dimension=2,
            max_retries=0,
        )

        with self.assertRaisesRegex(EmbeddingServiceError, "shape"):
            TemplateEmbedder(config, session=session).embed_one("hello")

    def test_environment_overrides_remote_settings(self):
        values = {
            "LOGAI_EMBEDDING_ENDPOINT": "http://bge-m3:8080/embed",
            "LOGAI_EMBEDDING_API_FORMAT": "tei",
            "LOGAI_EMBEDDING_API_KEY": "token",
            "LOGAI_EMBEDDING_MODEL": "BAAI/bge-m3",
            "LOGAI_EMBEDDING_DIMENSION": "1024",
            "LOGAI_EMBEDDING_BATCH_SIZE": "16",
            "LOGAI_EMBEDDING_TIMEOUT_SECONDS": "15",
            "LOGAI_EMBEDDING_MAX_RETRIES": "2",
            "LOGAI_EMBEDDING_RETRY_BACKOFF_SECONDS": "0.25",
        }

        with patch.dict(os.environ, values, clear=False):
            config = load_config().embedding

        self.assertEqual(config.endpoint, values["LOGAI_EMBEDDING_ENDPOINT"])
        self.assertEqual(config.api_format, "tei")
        self.assertEqual(config.api_key, "token")
        self.assertEqual(config.dimension, 1024)
        self.assertEqual(config.batch_size, 16)
        self.assertEqual(config.timeout_seconds, 15.0)
        self.assertEqual(config.max_retries, 2)
        self.assertEqual(config.retry_backoff_seconds, 0.25)


if __name__ == "__main__":
    unittest.main()
