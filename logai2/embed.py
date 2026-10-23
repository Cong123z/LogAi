"""Embeddings over HTTP: OpenAI-style /v1/embeddings, as served by TEI (the stack's bge-m3)."""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request

from logai2.llm import _OPENER   # no redirects: urllib would re-send the API key to the new location

DIMS = 1024   # bge-m3; the vector(1024) columns of 002_doc_links.sql


class EmbeddingError(RuntimeError):
    pass


def embed(url: str, model: str, texts: list[str], api_key: str = "", timeout: float = 30.0,
          max_retries: int = 0, backoff_seconds: float = 1.0, dims: int = DIMS) -> list[list[float]]:
    """Unit-length vectors in input order. Raises EmbeddingError on any failure.
    url is the base (…/v1/embeddings is added) or already the full endpoint. Network errors, timeouts
    and 5xx/429 are retried max_retries times, waiting backoff_seconds × attempt; other 4xx are not."""
    url = url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url if url.endswith("/v1/embeddings") else url + "/v1/embeddings",
        data=json.dumps({"input": texts, "model": model}).encode(), headers=headers, method="POST")
    for attempt in range(max_retries + 1):
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                data = json.load(response)["data"]
            break
        except Exception as exc:  # noqa: BLE001 - HTTP, network, JSON and shape errors alike
            client_error = isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code != 429
            if attempt == max_retries or client_error or isinstance(exc, (ValueError, KeyError, TypeError)):
                raise EmbeddingError(f"{type(exc).__name__}: {exc}") from exc
            time.sleep(backoff_seconds * (attempt + 1))
    vectors = [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
    if len(vectors) != len(texts) or any(len(v) != dims for v in vectors):
        raise EmbeddingError(f"expected {len(texts)} vectors of {dims} dims")
    return [_unit(v) for v in vectors]


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def literal(vector: list[float]) -> str:
    """pgvector text form, for %s::vector."""
    return "[" + ",".join(f"{x:.6g}" for x in vector) + "]"
