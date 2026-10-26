"""retrain_runs helpers: one row per Rebuild (and later Re-read). Each call is its own transaction;
call on an idle connection."""
from __future__ import annotations

import logging
import time

from psycopg.types.json import Jsonb

MAX_LOG = 50


def start(conn, kind: str, trigger: str, scope: dict | None = None) -> int:
    with conn.transaction():
        return conn.execute(
            "INSERT INTO retrain_runs (kind, trigger, scope, state) VALUES (%s, %s, %s, 'running') RETURNING id",
            (kind, trigger, Jsonb(scope or {}))).fetchone()["id"]


def step(conn, run_id: int, key: str, label: str, detail: dict, started_at: float,
         finished_at: float | None) -> None:
    new = {"key": key, "label": label, "started_at": started_at, "finished_at": finished_at, "detail": detail}
    with conn.transaction():
        steps = conn.execute("SELECT steps FROM retrain_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()["steps"]
        for i, s in enumerate(steps):
            if s["key"] == key:
                steps[i] = new
                break
        else:
            steps.append(new)
        conn.execute("UPDATE retrain_runs SET steps = %s WHERE id = %s", (Jsonb(steps), run_id))


def finish(conn, run_id: int, state: str, result: dict | None = None, error: str | None = None,
           error_type: str | None = None, failed_step: str | None = None, log: list[dict] | None = None) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE retrain_runs SET state = %s, finished_at = now(), result = %s, error = %s, error_type = %s, "
            "failed_step = %s, log = %s WHERE id = %s",
            (state, Jsonb(result or {}), error, error_type, failed_step, Jsonb(log or []), run_id))


class RunLog(logging.Handler):
    """Keeps the first 50 WARNING+ records of the logai2 logger while a run is going."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.lines: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.lines) < MAX_LOG:
            self.lines.append({"ts": record.created, "level": record.levelname, "logger": record.name,
                               "message": self.format(record)})

    def attach(self) -> None:
        logging.getLogger("logai2").addHandler(self)

    def detach(self) -> None:
        logging.getLogger("logai2").removeHandler(self)
