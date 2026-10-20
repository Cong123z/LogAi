"""LogAI2 console web API. Talks only to Postgres: never to the engine, never to Elasticsearch."""
from __future__ import annotations

import re
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any, List
from urllib.parse import urlparse

import psycopg
from flask import Flask, abort, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from logai2 import db
from logai2.alerts import alert_rows
from logai2.commands import enqueue
from logai2.config import AppConfig
from logai2.owner.baseline import DAY_CAP, Baseline, full_after, judge
from logai2.review import suggest
from logai2.timeutil import day_hour, daytype, minute_of

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
MAX_PAGE_SIZE = 500


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

    @app.errorhandler(Exception)
    def any_error(exc):
        if isinstance(exc, HTTPException):
            response = exc.get_response()   # keeps headers such as Allow on 405 and Location on redirects
            if exc.code is not None and exc.code >= 400:
                response.set_data(app.json.dumps({"error": exc.description}))
                response.content_type = "application/json"
            return response
        app.logger.exception("Unhandled error on %s %s", request.method, request.path)
        return jsonify({"error": "Internal server error"}), 500

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
                 "verify_certs": payload.get("verify_certs")}
        if not isinstance(value["verify_certs"], bool):
            raise BadRequest("verify_certs must be true or false")
        es = cfg.elasticsearch
        current, _ = setting("es_connection")
        current = current or {"hosts": es.hosts, "password": es.password}
        if not value["password"] and current["password"]:
            # Handing the stored password to another server would leak it.
            if cluster(value["hosts"]) != cluster(current["hosts"]):
                raise BadRequest("password required")
            value["password"] = current["password"]
        return jsonify({"revision": save("es_connection", value, payload.get("revision"))})

    def int_arg(name: str, default: int, high: int) -> int:
        try:
            value = int(request.args.get(name, default))
        except ValueError:
            raise BadRequest(f"{name} must be an integer") from None
        return min(max(value, 1), high)

    @app.get("/api/alerts")
    def alerts():
        c = conn()
        states = c.execute("SELECT * FROM alert_state WHERE state <> 'NORMAL'").fetchall()
        pairs = [(s["service"], s["key"]) for s in states]
        docs = [(svc, k[4:]) for svc, k in pairs if k.startswith("doc:")]
        # Links of the alerting templates (for grouping) and of the alerting doc keys (for their mean ± σ).
        link_rows = c.execute(
            "SELECT service, template_id, doc_id FROM template_doc_links "
            "WHERE (service, template_id) IN (SELECT * FROM unnest(%s::text[], %s::text[])) "
            "OR (service, doc_id) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
            ([s for s, _ in pairs], [k for _, k in pairs], [s for s, _ in docs], [d for _, d in docs])).fetchall()
        links = {(r["service"], r["template_id"]): r["doc_id"] for r in link_rows}
        doc_ids = list({r["doc_id"] for r in link_rows} | {d for _, d in docs})
        titles = {r["doc_id"]: r["title"] for r in c.execute(
            "SELECT doc_id, title FROM documents WHERE doc_id = ANY(%s)", (doc_ids,))}
        texts = {(r["service"], r["template_id"]): r["text"] for r in c.execute(
            "SELECT t.service, t.template_id, t.text FROM templates t JOIN alert_state a "
            "ON a.service = t.service AND a.key = t.template_id WHERE a.state <> 'NORMAL'")}

        # Normal mean and σ of this local hour, from the same lookups detection uses.
        day, hour = day_hour(minute_of(time.time()))
        baseline = Baseline.load(c, pairs + [(r["service"], r["template_id"]) for r in link_rows])

        def normal(svc: str, key: str) -> dict:
            if key.startswith("doc:"):
                tids = [r["template_id"] for r in link_rows if r["service"] == svc and r["doc_id"] == key[4:]]
                stat = baseline.lookup_doc(svc, tids, day, hour).this
            else:
                stat = baseline.lookup(svc, key, day, hour).this
            return {"mean": stat.mean if stat else None, "std": stat.std if stat else None}

        by_key = {(s["service"], s["key"]): s for s in states}
        rows = alert_rows(states, links, titles, texts)
        for r in rows:
            r["since"] = r["since"].timestamp()
            for t in r["templates"]:
                t.update(normal(r["service"], t["template_id"]))
            # service-wide and doc keys have no template line of their own: the card shows them from here
            r["totals"] = [{"key": k, "state": by_key[(r["service"], k)]["state"],
                            "count": by_key[(r["service"], k)]["count"],
                            "band_low": by_key[(r["service"], k)]["band_low"],
                            "band_high": by_key[(r["service"], k)]["band_high"], **normal(r["service"], k)}
                           for k in r["keys"] if k.startswith(("*", "doc:"))]
        return jsonify(rows)

    @app.get("/api/services")
    def services():
        return jsonify([r["service"] for r in conn().execute("SELECT service FROM services ORDER BY service")])

    @app.get("/api/documents")
    def documents():
        # Also feeds the Templates link select (doc_id, title).
        return jsonify(conn().execute(
            "SELECT d.doc_id, d.title, d.body, d.error_code, d.revision, count(l.template_id) AS linked_templates "
            "FROM documents d LEFT JOIN template_doc_links l USING (doc_id) GROUP BY d.doc_id ORDER BY d.doc_id"
        ).fetchall())

    def document_fields() -> dict:
        payload = body()
        fields = {"title": payload.get("title"), "body": payload.get("body"), "error_code": payload.get("error_code") or ""}
        for name, limit in (("title", 200), ("body", 20000), ("error_code", 100)):
            value = fields[name]
            if not isinstance(value, str) or len(value) > limit:
                raise BadRequest(f"{name} must be text of at most {limit} characters")
            fields[name] = value.strip()
        if not fields["title"] or not fields["body"]:
            raise BadRequest("title and body are required")
        return {**fields, "revision": payload.get("revision")}

    @app.post("/api/documents")
    def create_document():
        fields = document_fields()
        c = conn()
        c.execute("LOCK TABLE documents IN EXCLUSIVE MODE")   # two creates must not pick the same number
        top = c.execute("SELECT max(substring(doc_id FROM 5)::int) AS n FROM documents "
                        "WHERE doc_id ~ '^DOC-[0-9]{1,9}$'").fetchone()["n"] or 0
        doc_id, revision = f"DOC-{top + 1:03d}", uuid.uuid4().hex
        c.execute("INSERT INTO documents (doc_id, title, body, error_code, revision) VALUES (%s, %s, %s, %s, %s)",
                  (doc_id, fields["title"], fields["body"], fields["error_code"], revision))
        c.commit()   # abort/raise above leave it uncommitted; teardown rolls back
        return jsonify({"doc_id": doc_id, "revision": revision}), 201

    @app.put("/api/documents/<doc_id>")
    def update_document(doc_id: str):
        fields = document_fields()
        c = conn()
        row = c.execute("SELECT revision FROM documents WHERE doc_id = %s FOR UPDATE", (doc_id,)).fetchone()
        if not row:
            abort(404, "Unknown document")
        if row["revision"] != fields["revision"]:
            raise db.StaleRevision(row["revision"])
        revision = uuid.uuid4().hex
        c.execute("UPDATE documents SET title = %s, body = %s, error_code = %s, revision = %s, updated_at = now() "
                  "WHERE doc_id = %s", (fields["title"], fields["body"], fields["error_code"], revision, doc_id))
        c.commit()
        return jsonify({"doc_id": doc_id, "revision": revision})

    @app.delete("/api/documents/<doc_id>")
    def delete_document(doc_id: str):
        c = conn()
        # Delete first and let the FK on template_doc_links refuse: no window for a link to race in.
        try:
            deleted = c.execute("DELETE FROM documents WHERE doc_id = %s", (doc_id,)).rowcount
        except psycopg.errors.ForeignKeyViolation:
            c.rollback()
            n = c.execute("SELECT count(*) AS n FROM template_doc_links WHERE doc_id = %s", (doc_id,)).fetchone()["n"]
            return jsonify({"error": "linked", "templates": n}), 409
        if not deleted:
            abort(404, "Unknown document")
        c.commit()
        return jsonify({"doc_id": doc_id})

    def known_service(service: str | None) -> str:
        if not service or not conn().execute("SELECT 1 FROM services WHERE service = %s", (service,)).fetchone():
            abort(404, "Unknown service")
        return service

    @app.get("/api/baseline")
    def baseline_review():
        service = known_service(request.args.get("service"))
        c = conn()
        days = c.execute("SELECT day, committed, daytype FROM day_status WHERE service = %s ORDER BY day DESC",
                         (service,)).fetchall()
        decisions = c.execute("SELECT id, day, hour_from, hour_to, decision, created_at FROM review_exclusions "
                              "WHERE service = %s ORDER BY day DESC, hour_from", (service,)).fetchall()
        changes = c.execute("SELECT service, key, at, from_state, to_state FROM alert_changes "
                            "WHERE service = %s ORDER BY at", (service,)).fetchall()
        decided = {(service, d["day"], h) for d in decisions for h in range(d["hour_from"], d["hour_to"])}
        suggestions = suggest(changes, decided)
        for row in (*days, *decisions, *suggestions):
            row["day"] = row["day"].isoformat()
        for d in decisions:
            d["created_at"] = d["created_at"].timestamp()
        last_commit, _ = setting("last_commit")
        return jsonify({"days": days, "suggestions": suggestions, "decisions": decisions,
                        "last_commit": last_commit,
                        "full_after_days": {dt: full_after(cfg, dt) for dt in DAY_CAP}})

    @app.post("/api/baseline/decisions")
    def add_decision():
        payload = body()
        try:
            day = date.fromisoformat(payload.get("day"))
        except (TypeError, ValueError):
            raise BadRequest("day must be an ISO date (YYYY-MM-DD)") from None
        hour_from, hour_to, decision = payload.get("hour_from"), payload.get("hour_to"), payload.get("decision")
        if not (type(hour_from) is int and type(hour_to) is int and 0 <= hour_from < hour_to <= 24):
            raise BadRequest("hours must be whole numbers with 0 <= hour_from < hour_to <= 24")
        if decision not in ("exclude", "keep"):
            raise BadRequest("decision must be 'exclude' or 'keep'")
        service = known_service(payload.get("service"))
        c = conn()
        # The latest decision wins: drop overlapping rows of the opposite decision on that day.
        c.execute("DELETE FROM review_exclusions WHERE service = %s AND day = %s AND decision <> %s "
                  "AND hour_from < %s AND hour_to > %s", (service, day, decision, hour_to, hour_from))
        new_id = c.execute("INSERT INTO review_exclusions (service, day, hour_from, hour_to, decision) "
                           "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                           (service, day, hour_from, hour_to, decision)).fetchone()["id"]
        # Hours of this range left with < 3 of the days the rebuild uses (jobs/commit.py: last 5
        # committed weekdays / last 4 weekend days): their baseline comes from only those few days.
        kind = daytype(day)
        short = [r["h"] for r in c.execute(
            "WITH picked AS (SELECT day FROM day_status WHERE service = %s AND committed AND daytype = %s "
            "                ORDER BY day DESC LIMIT %s) "
            "SELECT h FROM generate_series(%s::int, %s::int - 1) h WHERE ("
            "  SELECT count(*) FROM picked p WHERE NOT EXISTS (SELECT 1 FROM review_exclusions x "
            "    WHERE x.service = %s AND x.day = p.day AND x.decision = 'exclude' "
            "    AND h >= x.hour_from AND h < x.hour_to)) < 3 ORDER BY h",
            (service, kind, DAY_CAP[kind], hour_from, hour_to, service))] if decision == "exclude" else []
        c.commit()
        out = {"id": new_id}
        if short:
            out["warning"] = short
        return jsonify(out), 201

    @app.delete("/api/baseline/decisions/<int:decision_id>")
    def delete_decision(decision_id: int):
        c = conn()
        if not c.execute("DELETE FROM review_exclusions WHERE id = %s", (decision_id,)).rowcount:
            abort(404, "Unknown decision")
        c.commit()
        return jsonify({"id": decision_id})

    @app.post("/api/baseline/recompute")
    def recompute():
        return jsonify({"id": enqueue(conn(), "recompute_baseline")}), 202

    @app.get("/api/commands/<int:command_id>")
    def command(command_id: int):
        row = conn().execute("SELECT id, kind, done_at IS NOT NULL AS done, result FROM commands WHERE id = %s",
                             (command_id,)).fetchone()
        if not row:
            abort(404, "Unknown command")
        return jsonify(row)

    @app.get("/api/templates")
    def templates():
        page, size = int_arg("page", 1, 10**6), int_arg("size", 100, MAX_PAGE_SIZE)
        where, args = ["true"], []
        for column, value in (("t.service", request.args.get("service")), ("t.level", request.args.get("level")),
                              ("l.doc_id", request.args.get("doc_id"))):
            if value:
                where.append(f"{column} = %s")
                args.append(value)
        if request.args.get("q"):
            where.append("strpos(lower(t.text), lower(%s)) > 0")   # substring, no LIKE wildcards to escape
            args.append(request.args["q"])
        where_sql = " AND ".join(where)
        c = conn()
        total = c.execute("SELECT count(*) AS n FROM templates t LEFT JOIN template_doc_links l "
                          f"ON l.service = t.service AND l.template_id = t.template_id WHERE {where_sql}", args).fetchone()["n"]
        items = c.execute(
            "SELECT t.service, t.template_id, t.text, t.level, t.first_seen, t.last_seen, l.doc_id, "
            "d.title AS doc_title, COALESCE(a.state, 'NORMAL') AS state FROM templates t "
            "LEFT JOIN template_doc_links l ON l.service = t.service AND l.template_id = t.template_id "
            "LEFT JOIN documents d ON d.doc_id = l.doc_id "
            "LEFT JOIN alert_state a ON a.service = t.service AND a.key = t.template_id "
            f"WHERE {where_sql} ORDER BY t.last_seen DESC, t.template_id LIMIT %s OFFSET %s",
            [*args, size, (page - 1) * size]).fetchall()

        # Rate and band for the current local hour, judged exactly as detection does.
        day, hour = day_hour(minute_of(time.time()))
        keys = [(i["service"], i["template_id"]) for i in items]
        svcs, tids = [s for s, _ in keys], [t for _, t in keys]
        minutes = {r["service"]: r["n_minutes"] for r in c.execute(
            "SELECT service, n_minutes FROM service_hours WHERE day = %s AND hour = %s AND service = ANY(%s)",
            (day, hour, svcs))}
        sums = {(r["service"], r["key"]): r["sum"] for r in c.execute(
            "SELECT service, key, sum FROM hourly_counts WHERE day = %s AND hour = %s "
            "AND (service, key) IN (SELECT * FROM unnest(%s::text[], %s::text[]))", (day, hour, svcs, tids))}
        baseline = Baseline.load(c, keys)
        for i in items:
            svc, tid = i["service"], i["template_id"]
            n = minutes.get(svc)
            i["rate"] = sums.get((svc, tid), 0) / n if n else None
            verdict = judge(0, tid, i["level"], baseline.lookup(svc, tid, day, hour), cfg)
            i["band_low"], i["band_high"] = verdict.band_low, verdict.band_high
            i["first_seen"], i["last_seen"] = i["first_seen"].timestamp(), i["last_seen"].timestamp()
        return jsonify({"total": total, "items": items})

    @app.put("/api/templates/<service>/<path:template_id>/link")
    def link_template(service: str, template_id: str):
        doc_id = body().get("doc_id")
        if doc_id is not None and not isinstance(doc_id, str):
            raise BadRequest("doc_id must be a string or null")
        c = conn()
        if not c.execute("SELECT 1 FROM templates WHERE service = %s AND template_id = %s",
                         (service, template_id)).fetchone():
            abort(404, "Unknown template")
        if doc_id is None:
            c.execute("DELETE FROM template_doc_links WHERE service = %s AND template_id = %s", (service, template_id))
        else:
            if not c.execute("SELECT 1 FROM documents WHERE doc_id = %s", (doc_id,)).fetchone():
                abort(404, "Unknown document")
            c.execute("INSERT INTO template_doc_links (service, template_id, doc_id) VALUES (%s, %s, %s) "
                      "ON CONFLICT (service, template_id) DO UPDATE "
                      "SET doc_id = EXCLUDED.doc_id, source = 'manual', linked_at = now()",
                      (service, template_id, doc_id))
        enqueue(c, "reload_links")
        c.commit()   # the link and its reload command land together
        return jsonify({"doc_id": doc_id})

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(STATIC_DIR, filename)

    return app
