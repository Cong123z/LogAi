"""Container readiness check for the realtime engine."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logai.config import load_config
from logai.storage.grouping import GroupingOverrideStore
from logai.storage.retrain_schedule import RetrainScheduleStore


def main() -> int:
    config = load_config()
    # A retrain pauses the poll loop (and its heartbeat) for minutes; it keeps
    # its own heartbeat in the retrain status instead.
    retrain = RetrainScheduleStore.from_config(config).load_status()
    if retrain.get("state") == "running" and time.time() - float(
        retrain.get("heartbeat_at") or 0
    ) <= config.grouping.engine_status_stale_seconds:
        print("engine ready; retraining")
        return 0
    store = GroupingOverrideStore.from_config(config)
    status = store.synchronization_status(
        stale_seconds=config.grouping.engine_status_stale_seconds,
        now=time.time(),
    )
    if status.get("state") == "engine_unavailable":
        print("engine heartbeat is unavailable or stale")
        return 1
    print(f"engine ready; grouping={status.get('state', 'unknown')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
