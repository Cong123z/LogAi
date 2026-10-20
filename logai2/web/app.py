"""LogAI2 console web API. Talks only to Postgres: never to the engine, never to Elasticsearch."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, List
from urllib.parse import urlparse

from flask import Flask, g, jsonify, request, send_from_directory

from logai2 import db
from logai2.commands import enqueue
from logai2.config import AppConfig

STATIC_DIR = Path(__file__).parent / "static"
STALE_HEARTBEAT_SECONDS = 30

# Copied from logai-engine logai/storage/index_selection.py.
MAX_ENTRIES = 50
PATTERN_LIMIT = 255
# Lowercase ES index name characters plus the `*` wildcard; must not start
# with - _ + (reserved by Elasticsearch) and never contain a comma.
_PATTERN_RE = re.compile(r"^[a-z0-9*][a-z0-9*._#-]*$")
MAX_HOSTS = 10
TEXT_LIMIT = 500


class BadRequest(ValueError):
    pass


def validate_patterns(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        raise BadRequest("patterns must be a list")
    patterns: List[str] = []
    for value in raw:
        pattern = value.strip() if isinstance(value, str) else ""
        if not pattern or len(pattern) > PATTERN_LIMIT or not _PATTERN_RE.fullmatch(pattern):
            raise BadRequest(
                f"Invalid index or pattern {value!r}: use lowercase letters, digits, "
                "'.', '_', '-', '#' and '*'"
            )
        if pattern not in patterns:
            patterns.append(pattern)
    if len(patterns) > MAX_ENTRIES:
        raise BadRequest(f"At most {MAX_ENTRIES} indices or patterns")
    return patterns


def validate_hosts(raw: Any) -> List[str]:
    """http(s) URLs without credentials (from logai-engine logai/storage/es_connection.py)."""
    if not isinstance(raw, list):
        raise BadRequest("hosts must be a list of URLs")
    hosts: List[str] = []
    for value in raw:
        url = value.strip().rstrip("/") if isinstance(value, str) else ""
        if not url:
            continue
        parsed = urlparse(url)
        try:
            parsed.port
        except ValueError as exc:
            raise BadRequest(f"Endpoint {value!r} has an invalid port") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or len(url) > 2000:
            raise BadRequest(f"Invalid endpoint {value!r}: use an http(s) URL")
        if parsed.username or parsed.password:
            # Credentials belong in the password field, which is never shown back.
            raise BadRequest("Endpoint must not contain a username or password")
        if url not in hosts:
            hosts.append(url)
    if not hosts or len(hosts) > MAX_HOSTS:
        raise BadRequest(f"Give 1 to {MAX_HOSTS} endpoints")
    return hosts


def _text(value: Any, field: str) -> str:
    text = value if isinstance(value, str) else ""
    # Control characters end up in the Authorization header and make the
    # client raise an error that quotes it (and so the password) into logs.
    if len(text) > TEXT_LIMIT or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise BadRequest(f"{field} must be at most {TEXT_LIMIT} characters, without control characters")
    return text


def cluster(hosts: List[str]) -> set[str]:
    """The host:port set: which cluster the hosts point at, independent of order and paths."""
    out = set()
    for h in hosts:
        p = urlparse(h)
        out.add(f"{(p.hostname or '').lower()}:{p.port or (443 if p.scheme == 'https' else 9200)}")
    return out


def create_app(cfg: AppConfig) -> Flask:
    app = Flask(__name__, static_folder=None)

    def conn():
        # ponytail: connection per request; add psycopg_pool if many operators
        if "conn" not in g:
            g.conn = db.connect(cfg.database.dsn)
        return g.conn

    @app.teardown_appcontext
    def close_conn(_exc):
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    @app.errorhandler(BadRequest)
    def bad_request(exc):
        return jsonify({"error": str(exc)}), 400

    @app.errorhandler(db.StaleRevision)
    def stale(exc):
        return jsonify({"error": "stale", "revision": exc.current}), 409

    def setting(key: str) -> tuple[dict | None, str | None]:
        row = conn().execute("SELECT value, revision FROM settings WHERE key = %s", (key,)).fetchone()
        return (row["value"], row["revision"]) if row else (None, None)

    def save(key: str, value: dict, revision: str | None) -> str:
        _, current = setting(key)
        # put_setting treats revision None as "overwrite"; for the web None means "nothing saved yet"
        if revision != current:
            raise db.StaleRevision(current)
        new = db.put_setting(conn(), key, value, revision)
        enqueue(conn(), "reload_sources")
        conn().commit()   # the SELECT above opened a transaction: the setting and its command land together
        return new

    def selection_entries() -> list[dict]:
        value, _ = setting("index_selection")
        # never saved (or saved empty): the engine reads the configured index from added_at 0
        return (value or {}).get("entries") or [{"pattern": cfg.elasticsearch.index, "added_at": 0}]

    def engine_status() -> dict:
        row = conn().execute("SELECT status FROM worker_status WHERE worker = 'engine'").fetchone()
        return row["status"] if row else {}

    def body() -> dict:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise BadRequest("JSON object body required")
        return payload

    @app.get("/api/pipeline")
    def pipeline():
        status = engine_status()
        cursors = conn().execute("SELECT index, since_ms, floor_ms FROM es_cursors ORDER BY index").fetchall()
        stale = time.time() - (status.get("heartbeat") or 0) > STALE_HEARTBEAT_SECONDS
        return jsonify({"status": status or None, "cursors": cursors, "stale": stale})

    @app.get("/api/sources")
    def sources():
        _, sel_rev = setting("index_selection")
        saved, conn_rev = setting("es_connection")
        es = cfg.elasticsearch
        shown = saved or {"hosts": es.hosts, "username": es.username, "verify_certs": es.verify_certs,
                          "password": es.password}
        status = engine_status()
        return jsonify({
            "selection": {"entries": selection_entries(), "revision": sel_rev},
            "connection": {"hosts": shown["hosts"], "username": shown["username"],
                           "verify_certs": shown["verify_certs"], "has_password": bool(shown["password"]),
                           "revision": conn_rev},
            "available_indices": status.get("available_indices"),
            "resolution": status.get("resolution"),
            "indices": status.get("indices"),
        })

    @app.put("/api/sources/selection")
    def put_selection():
        payload = body()
        patterns = validate_patterns(payload.get("patterns"))
        added = {e["pattern"]: e["added_at"] for e in selection_entries()}
        now = time.time()
        value = {"entries": [{"pattern": p, "added_at": added.get(p, now)} for p in patterns]}
        return jsonify({"revision": save("index_selection", value, payload.get("revision"))})

    @app.put("/api/sources/connection")
    def put_connection():
        payload = body()
        value = {"hosts": validate_hosts(payload.get("hosts")),
                 "username": _text(payload.get("username"), "username").strip(),
                 "password": _text(payload.get("password"), "password"),
                 "verify_certs": payload.get("verify_certs") is not False}
        es = cfg.elasticsearch
        current, _ = setting("es_connection")
        current = current or {"hosts": es.hosts, "password": es.password}
        if not value["password"] and current["password"]:
            # Handing the stored password to another server would leak it.
            if cluster(value["hosts"]) != cluster(current["hosts"]):
                raise BadRequest("password required")
            value["password"] = current["password"]
        return jsonify({"revision": save("es_connection", value, payload.get("revision"))})

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(STATIC_DIR, filename)

    return app
