"""Test doubles shared by the reader tests."""
from __future__ import annotations

import fnmatch


def hit(_id, ms, **src):
    return {"_id": _id, "sort": [ms, 1], "_source": {"@timestamp": ms, **src}}


class FakeSource:
    """EsSource.page over in-memory hits; `fail(target)` makes the next page() of that target raise."""

    def __init__(self, hits_by_index: dict[str, list[dict]]):
        self.hits = hits_by_index
        self.failing: set[str] = set()
        self.calls: list[str] = []
        self.size = 10_000   # EsSource's page size (cfg.elasticsearch.batch_size)
        self.oldest_failing = False
        self.oldest_calls: list[str] = []

    def fail(self, target: str) -> None:
        self.failing.add(target)

    def _match(self, target: str) -> list[str]:
        first, *excl = target.split(",")
        return sorted(i for i in fnmatch.filter(self.hits, first)
                      if not any(fnmatch.fnmatch(i, e[1:]) for e in excl))

    def page(self, target: str, from_ms: int, upper_ms: int) -> list[dict]:
        self.calls.append(target)
        if target in self.failing:
            self.failing.discard(target)
            raise ConnectionError(f"{target} down")
        hits = [{**h, "_index": i} for i in self._match(target) for h in self.hits[i]
                if from_ms <= h["sort"][0] <= upper_ms]
        return sorted(hits, key=lambda h: h["sort"])[:self.size]

    def resolve(self, patterns: list[str]) -> dict[str, list[str]]:
        return {p: sorted(fnmatch.filter(self.hits, p)) for p in patterns}

    def oldest_ms(self, target: str) -> int | None:
        self.oldest_calls.append(target)
        if self.oldest_failing:
            raise ConnectionError("oldest down")
        return min((h["sort"][0] for i in self._match(target) for h in self.hits[i]), default=None)

    def list_indices(self) -> list[dict]:
        return [{"index": i} for i in sorted(self.hits)]

    def reconnect(self, cfg) -> None:
        self.connected = cfg
