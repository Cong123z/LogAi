"""Durable lightweight event index for resumable historical training."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Iterable, Iterator

from logai.models import ParsedEvent


class TrainingEventIndex:
    """Append-only index containing only data needed by later training phases."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_batch(self, events: Iterable[ParsedEvent]) -> None:
        records = [
            {
                "event_id": event.raw.event_id,
                "template_id": event.template_id,
                "template_text": event.template,
                "service": event.raw.service,
                "level": event.raw.level,
                "timestamp": event.raw.timestamp,
            }
            for event in events
        ]
        if not records:
            return
        with open(self.path, "a", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def records(self) -> Iterator[Dict[str, object]]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record

    def event_ids(self) -> set[str]:
        return {
            record["event_id"]
            for record in self.records()
            if isinstance(record.get("event_id"), str) and record["event_id"]
        }

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
