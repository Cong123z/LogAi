"""Dead Letter Queue - append-only JSONL file for events that failed
processing after exhausting retries (plan section 7)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator


class DeadLetterQueue:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def push(self, payload: Dict[str, Any], error: str) -> None:
        record = {
            "failed_at": time.time(),
            "error": error,
            "payload": payload,
        }
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")

    def replay(self) -> Iterator[Dict[str, Any]]:
        """Yield DLQ records for manual/automatic reprocessing. Does not
        remove them - caller is responsible for truncating once reprocessed
        successfully (see `clear`)."""
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def clear(self) -> None:
        with self._lock:
            if self.path.exists():
                self.path.unlink()

    def count(self) -> int:
        if not self.path.exists():
            return 0
        with open(self.path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
