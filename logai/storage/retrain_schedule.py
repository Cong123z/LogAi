"""Retrain schedule set on the web "Retrain" page.

The web is the only writer of the schedule file (when to retrain, how much
history to read, and "retrain now" requests); the engine is the only writer of
the status file (running/finished, next run, history) and the web only reads it.
The engine runs the retrain itself: see RealtimePipeline._run_retrain.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from logai.storage.index_selection import _atomic_write, read_json

SCHEMA_VERSION = 1
MAX_LOOKBACK_HOURS = 720
MIN_MAX_DOCS = 1_000
MAX_MAX_DOCS = 5_000_000
HISTORY_LIMIT = 20
_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")

_write_lock = threading.Lock()


class RetrainScheduleError(ValueError):
    """Invalid schedule or run request."""


class RetrainScheduleConflict(RetrainScheduleError):
    def __init__(self, current_revision: str):
        super().__init__("The retrain schedule changed; reload and try again")
        self.current_revision = current_revision


def _revision(schedule: Dict[str, Any]) -> str:
    encoded = json.dumps(schedule, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_training_data(lookback_hours: Any, max_docs: Any) -> Dict[str, Any]:
    if isinstance(lookback_hours, bool) or not isinstance(lookback_hours, (int, float)) \
            or not 0 < lookback_hours <= MAX_LOOKBACK_HOURS:
        raise RetrainScheduleError(
            f"lookback_hours must be a number above 0 and at most {MAX_LOOKBACK_HOURS}"
        )
    if isinstance(max_docs, bool) or not isinstance(max_docs, int) \
            or not MIN_MAX_DOCS <= max_docs <= MAX_MAX_DOCS:
        raise RetrainScheduleError(
            f"max_docs must be a whole number from {MIN_MAX_DOCS} to {MAX_MAX_DOCS}"
        )
    return {"lookback_hours": float(lookback_hours), "max_docs": max_docs}


def validate_schedule(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise RetrainScheduleError("schedule must be an object")
    if not isinstance(raw.get("enabled"), bool):
        raise RetrainScheduleError("enabled must be true or false")
    if not isinstance(raw.get("time"), str) or not _TIME_RE.fullmatch(raw["time"]):
        raise RetrainScheduleError("time must be HH:MM (24-hour)")
    weekdays = raw.get("weekdays")
    if not isinstance(weekdays, list) or not weekdays or not all(
        isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in weekdays
    ):
        raise RetrainScheduleError("weekdays must list at least one day, 0 (Monday) to 6 (Sunday)")
    zone = raw.get("timezone")
    try:
        if not isinstance(zone, str) or not zone:
            raise ValueError
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise RetrainScheduleError(f"Unknown timezone {zone!r}") from None
    return {
        "enabled": raw["enabled"],
        "time": raw["time"],
        "weekdays": sorted(set(weekdays)),
        "timezone": zone,
        **validate_training_data(raw.get("lookback_hours"), raw.get("max_docs")),
    }


def next_run_at(schedule: Dict[str, Any], after_ts: float) -> Optional[float]:
    """First scheduled instant strictly after ``after_ts`` (None when disabled).

    Wall-clock time in the schedule's zone: on a DST change day a time that
    does not exist resolves to the shifted instant, a repeated one to its
    first occurrence.
    """
    if not schedule.get("enabled"):
        return None
    zone = ZoneInfo(schedule["timezone"])
    hour, minute = (int(part) for part in schedule["time"].split(":"))
    start = datetime.fromtimestamp(after_ts, zone).date()
    for offset in range(8):
        day = start + timedelta(days=offset)
        if day.weekday() not in schedule["weekdays"]:
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).timestamp()
        if candidate > after_ts:
            return candidate
    return None


class RetrainScheduleStore:
    def __init__(self, schedule_path: str | Path, status_path: str | Path, defaults: Dict[str, Any]):
        self.schedule_path = Path(schedule_path)
        self.status_path = Path(status_path)
        self.defaults = defaults

    @classmethod
    def from_config(cls, config: Any) -> "RetrainScheduleStore":
        base = Path(config.storage.base_dir)
        return cls(
            base / config.storage.retrain_schedule_file,
            base / config.storage.retrain_status_file,
            {
                "enabled": False,
                "time": "02:00",
                "weekdays": list(range(7)),
                "timezone": "UTC",
                "lookback_hours": round(config.training.lookback_seconds / 3600, 2),
                "max_docs": int(config.training.max_docs),
            },
        )

    def load(self) -> Dict[str, Any]:
        """{schedule, revision, run_request}; defaults when never saved or invalid."""
        raw = read_json(self.schedule_path)
        try:
            schedule, saved = validate_schedule(raw.get("schedule")), True
        except RetrainScheduleError:
            schedule, saved = dict(self.defaults), False
        request = raw.get("run_request")
        return {
            "schedule": schedule,
            "saved": saved,
            "updated_at": raw.get("updated_at"),
            "revision": _revision(schedule),
            "run_request": request if isinstance(request, dict) else None,
        }

    def _write(self, schedule: Optional[Dict[str, Any]], run_request: Optional[Dict[str, Any]]) -> None:
        _atomic_write(self.schedule_path, {
            "schema_version": SCHEMA_VERSION,
            "updated_at": time.time(),
            "schedule": schedule,
            "run_request": run_request,
        })

    def save(self, raw: Any, expected_revision: Optional[str]) -> Dict[str, Any]:
        schedule = validate_schedule(raw)
        with _write_lock:
            current = self.load()
            if expected_revision != current["revision"]:
                raise RetrainScheduleConflict(current["revision"])
            self._write(schedule, current["run_request"])
        return self.load()

    def request_run(
        self, lookback_hours: Any = None, max_docs: Any = None, now: Optional[float] = None
    ) -> Dict[str, Any]:
        """Ask the engine to retrain now; missing values come from the schedule."""
        with _write_lock:
            current = self.load()
            schedule = current["schedule"]
            values = validate_training_data(
                schedule["lookback_hours"] if lookback_hours is None else lookback_hours,
                schedule["max_docs"] if max_docs is None else max_docs,
            )
            request = {"requested_at": time.time() if now is None else now, **values}
            # A run request must not turn the defaults into a saved schedule.
            self._write(schedule if current["saved"] else None, request)
        return request

    def load_status(self) -> Dict[str, Any]:
        status = read_json(self.status_path)
        status.setdefault("state", "idle")
        status.setdefault("history", [])
        return status

    def write_status(self, status: Dict[str, Any]) -> None:
        status = {**status, "history": list(status.get("history", []))[-HISTORY_LIMIT:]}
        _atomic_write(self.status_path, {**status, "schema_version": SCHEMA_VERSION})


# -- rollback ----------------------------------------------------------------

BACKUP_DIR = "retrain_backup"
_MANIFEST = "manifest.json"


def retrain_artifacts(config: Any) -> list[Path]:
    """Every file (or directory) a training run rewrites."""
    base = Path(config.storage.base_dir)
    st = config.storage
    return [
        base / st.template_registry_file,
        base / st.template_embeddings_file,
        base / st.group_registry_file,
        base / st.group_centroids_file,
        base / st.doc_embeddings_file,
        base / st.grouping_status_file,
        base / st.group_lineage_file,
        Path(config.drain3.persistence_path),
        Path(config.doc_matcher.status_path),
        Path(st.model_dir),
    ]


def _copy(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def snapshot_artifacts(config: Any) -> Path:
    """Copy the current artifacts aside. The manifest is written last, so a
    backup without one is incomplete and never restored."""
    backup = Path(config.storage.base_dir) / BACKUP_DIR
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True)
    entries = []
    for index, path in enumerate(retrain_artifacts(config)):
        name = str(index) if path.exists() else None
        if name is not None:
            _copy(path, backup / name)
        entries.append({"path": str(path), "saved": name})
    _atomic_write(backup / _MANIFEST, {"created_at": time.time(), "entries": entries})
    return backup


def restore_artifacts(config: Any) -> bool:
    """Put back the artifacts from before the retrain; False if no complete
    backup exists. Safe to repeat (a crash during restore restores again)."""
    backup = Path(config.storage.base_dir) / BACKUP_DIR
    manifest = read_json(backup / _MANIFEST)
    if not isinstance(manifest.get("entries"), list):
        return False
    for entry in manifest["entries"]:
        target = Path(entry["path"])
        tmp = target.with_name(target.name + ".restore-tmp")
        shutil.rmtree(tmp, ignore_errors=True) if tmp.is_dir() else tmp.unlink(missing_ok=True)
        if entry.get("saved") is not None:
            _copy(backup / entry["saved"], tmp)
        if target.is_dir():
            shutil.rmtree(target)
        if entry.get("saved") is not None:
            os.replace(tmp, target)
        else:
            target.unlink(missing_ok=True)
    # A half-done training run must not be resumed on top of the old state.
    base = Path(config.storage.base_dir)
    for name in (config.storage.training_checkpoint_file, config.storage.training_event_index_file):
        (base / name).unlink(missing_ok=True)
    return True
