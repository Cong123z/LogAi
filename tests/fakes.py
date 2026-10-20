"""Test doubles shared by the reader tests."""
from __future__ import annotations

import fnmatch


def hit(_id, ms, **src):
    return {"_id": _id, "sort": [ms, 1], "_source": {"@timestamp": ms, **src}}


class FakeSource:
    """EsSource.page over in-memory hits; `fail(index)` makes the next page() raise."""

    def __init__(self, hits_by_index: dict[str, list[dict]]):
        self.hits = hits_by_index
        self.failing: set[str] = set()

    def fail(self, index: str) -> None:
        self.failing.add(index)

    def page(self, index: str, from_ms: int, upper_ms: int) -> list[dict]:
        if index in self.failing:
            self.failing.discard(index)
            raise ConnectionError(f"{index} down")
        hits = [h for h in self.hits.get(index, []) if from_ms <= h["sort"][0] <= upper_ms]
        return sorted(hits, key=lambda h: h["sort"])[:10_000]

    def resolve(self, patterns: list[str]) -> dict[str, list[str]]:
        return {p: sorted(fnmatch.filter(self.hits, p)) for p in patterns}

    def list_indices(self) -> list[dict]:
        return [{"index": i} for i in sorted(self.hits)]

    def reconnect(self, cfg) -> None:
        self.connected = cfg
