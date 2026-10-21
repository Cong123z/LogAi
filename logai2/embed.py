"""Embeddings over HTTP: OpenAI-style /v1/embeddings, as served by TEI (the stack's bge-m3)."""
from __future__ import annotations

import json
import math
import urllib.request

DIMS = 1024   # bge-m3; the vector(1024) columns of 002_doc_links.sql
TIMEOUT_SECONDS = 30


class EmbeddingError(RuntimeError):
    pass


def embed(url: str, model: str, texts: list[str]) -> list[list[float]]:
    """Unit-length vectors in input order. Raises EmbeddingError on any failure."""
    request = urllib.request.Request(
        url.rstrip("/") + "/v1/embeddings", data=json.dumps({"input": texts, "model": model}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.load(response)["data"]
    except Exception as exc:  # noqa: BLE001 - HTTP, network, JSON and shape errors alike
        raise EmbeddingError(f"{type(exc).__name__}: {exc}") from exc
    vectors = [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
    if len(vectors) != len(texts) or any(len(v) != DIMS for v in vectors):
        raise EmbeddingError(f"expected {len(texts)} vectors of {DIMS} dims")
    return [_unit(v) for v in vectors]


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def literal(vector: list[float]) -> str:
    """pgvector text form, for %s::vector."""
    return "[" + ",".join(f"{x:.6g}" for x in vector) + "]"
