"""Known-line cache: (service, normalized text) -> template id, LRU."""
from __future__ import annotations

from collections import OrderedDict


class KnownLines:
    def __init__(self, cap: int):
        self.cap = cap
        self._lines: OrderedDict[tuple[str, str], str] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, service: str, text: str) -> str | None:
        template_id = self._lines.get((service, text))
        if template_id is None:
            self.misses += 1
            return None
        self._lines.move_to_end((service, text))
        self.hits += 1
        return template_id

    def put(self, service: str, text: str, template_id: str) -> None:
        self._lines[(service, text)] = template_id
        self._lines.move_to_end((service, text))
        if len(self._lines) > self.cap:
            self._lines.popitem(last=False)
