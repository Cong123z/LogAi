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
from logai2.links import ALERTING_SOURCES, cluster_edges, error_lines, level_class, next_doc_id, similar
from logai2.owner.baseline import DAY_CAP, Baseline, full_after, judge
from logai2.review import suggest
from logai2.timeutil import day_hour, daytype, minute_of

STATIC_DIR = Path(__file__).parent / "static"
STALE_HEARTBEAT_SECONDS = 30
LINKER_STALE_SECONDS = 90   # the linker writes its status once per cycle (30 s), longer with LLM calls

# Copied from logai-engine logai/storage/index_selection.py.
MAX_ENTRIES = 50
PATTERN_LIMIT = 255
# Lowercase ES index name characters plus the `*` wildcard; must not start
# with - _ + (reserved by Elasticsearch) and never contain a comma.
_PATTERN_RE = re.compile(r"^[a-z0-9*][a-z0-9*._#-]*$")
MAX_HOSTS = 10
TEXT_LIMIT = 500
MAX_PAGE_SIZE = 500
MAX_ERROR_LINES = 50
DOC_STATUSES = ("active", "draft", "rejected")
AUTOMATIC_SOURCES = ("inherited", "corpus", "llm", "draft")
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
LINK_SOURCES = ("manual", "inherited", "corpus", "llm", "draft", "rejected")


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
        row = conn().execute("SELECT status FROM worker_status WHERE worker = 'linker'").fetchone()
        linker = row["status"] if row else None
        linker_stale = linker is None or time.time() - (linker.get("heartbeat") or 0) > LINKER_STALE_SECONDS
        return jsonify({"status": status or None, "cursors": cursors, "stale": stale,
                        "linker": linker, "linker_stale": linker_stale})

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
            "SELECT service, template_id, doc_id FROM template_doc_links WHERE doc_id IS NOT NULL AND source = ANY(%s) "
            "AND ((service, template_id) IN (SELECT * FROM unnest(%s::text[], %s::text[])) "
            "OR (service, doc_id) IN (SELECT * FROM unnest(%s::text[], %s::text[])))",
            (list(ALERTING_SOURCES), [s for s, _ in pairs], [k for _, k in pairs],
             [s for s, _ in docs], [d for _, d in docs])).fetchall()
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
        undocumented = [(svc, k) for svc, k in pairs if not k.startswith(("*", "doc:")) and (svc, k) not in links]
        edges = cluster_edges(c, undocumented, cfg.documentation.cluster_similarity)
        # every link of the alerting templates, drafts and rejected included, for the card's badges
        link_info = {(r["service"], r["template_id"]): r for r in c.execute(
            "SELECT l.service, l.template_id, l.doc_id, l.source, l.similarity, l.note, d.title, d.verdict "
            "FROM template_doc_links l LEFT JOIN documents d USING (doc_id) "
            "WHERE (l.service, l.template_id) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
            ([s for s, _ in pairs], [k for _, k in pairs]))}
        rows = alert_rows(states, links, titles, texts, edges)
        for r in rows:
            r["since"] = r["since"].timestamp()
            for t in r["templates"]:
                t.update(normal(r["service"], t["template_id"]))
                info = link_info.get((r["service"], t["template_id"])) or {}
                t["link"] = {k: info.get(k) for k in ("doc_id", "source", "similarity", "note", "title", "verdict")} \
                    if info else None
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
        # Also feeds the Templates link select (doc_id, title). Drafts and rejected ones: ?status=draft|rejected.
        status = request.args.get("status", "active")
        if status not in DOC_STATUSES:
            raise BadRequest(f"status must be one of {', '.join(DOC_STATUSES)}")
        rows = conn().execute(
            "SELECT d.doc_id, d.title, d.error_code, d.error, d.handling, d.status, d.source, d.verdict, "
            "d.confidence, d.revision, d.embedded_revision IS NOT DISTINCT FROM d.revision AS embedded, "
            "count(l.template_id) FILTER (WHERE l.source = ANY(%s)) AS linked_templates "
            "FROM documents d LEFT JOIN template_doc_links l USING (doc_id) WHERE d.status = %s "
            "GROUP BY d.doc_id ORDER BY d.doc_id", (list(ALERTING_SOURCES), status)).fetchall()
        for r in rows:
            r["error_lines"] = error_lines(r["error"])   # what the linker embeds; empty = never auto-matched
        return jsonify(rows)

    def document_fields() -> dict:
        payload = body()
        fields = {name: payload.get(name) or "" for name in ("title", "error_code", "error", "handling")}
        for name, limit in (("title", 200), ("error_code", 100), ("error", 5000), ("handling", 20000)):
            value = fields[name]
            if not isinstance(value, str) or len(value) > limit:
                raise BadRequest(f"{name} must be text of at most {limit} characters")
            fields[name] = value.strip()
        if not fields["title"] or not (fields["error"] or fields["handling"]):
            raise BadRequest("title and an ERROR or Handling text are required")
        if len(fields["error"].splitlines()) > MAX_ERROR_LINES:
            raise BadRequest(f"error must have at most {MAX_ERROR_LINES} lines")
        return {**fields, "revision": payload.get("revision")}

    @app.post("/api/documents")
    def create_document():
        fields = document_fields()
        c = conn()
        doc_id, revision = next_doc_id(c), uuid.uuid4().hex
        c.execute("INSERT INTO documents (doc_id, title, error_code, error, handling, revision) "
                  "VALUES (%s, %s, %s, %s, %s, %s)",
                  (doc_id, fields["title"], fields["error_code"], fields["error"], fields["handling"], revision))
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
        c.execute("UPDATE documents SET title = %s, error_code = %s, error = %s, handling = %s, revision = %s, "
                  "updated_at = now() WHERE doc_id = %s",
                  (fields["title"], fields["error_code"], fields["error"], fields["handling"], revision, doc_id))
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
        if request.args.get("link_source") not in (None, "", "none", *LINK_SOURCES):
            raise BadRequest(f"link_source must be none or one of {', '.join(LINK_SOURCES)}")
        if request.args.get("link_source") == "none":
            where.append("l.template_id IS NULL")
        for column, value in (("t.service", request.args.get("service")), ("t.level", request.args.get("level")),
                              ("l.doc_id", request.args.get("doc_id")),
                              ("l.source", request.args.get("link_source") if request.args.get("link_source") != "none" else None)):
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
            "d.title AS doc_title, d.verdict AS doc_verdict, l.source AS link_source, l.similarity, l.inherited_from, "
            "l.note, t.embedding IS NOT NULL AS embedded, COALESCE(a.state, 'NORMAL') AS state FROM templates t "
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

    def known_template(c, service: str, template_id: str) -> None:
        if not c.execute("SELECT 1 FROM templates WHERE service = %s AND template_id = %s",
                         (service, template_id)).fetchone():
            abort(404, "Unknown template")

    def set_manual(c, service: str, template_id: str, doc_id: str | None) -> None:
        # a draft this replaces leaves the AI suggestions queue (its template is decided)
        c.execute("UPDATE documents SET status = 'rejected', updated_at = now() WHERE status = 'draft' "
                  "AND doc_id IS DISTINCT FROM %s AND doc_id = (SELECT doc_id FROM template_doc_links "
                  "WHERE service = %s AND template_id = %s AND source = 'draft')", (doc_id, service, template_id))
        c.execute("INSERT INTO template_doc_links (service, template_id, doc_id) VALUES (%s, %s, %s) "
                  "ON CONFLICT (service, template_id) DO UPDATE SET doc_id = EXCLUDED.doc_id, source = 'manual', "
                  "similarity = NULL, inherited_from = NULL, note = NULL, linked_at = now()",
                  (service, template_id, doc_id))

    @app.put("/api/templates/<service>/<path:template_id>/link")
    def link_template(service: str, template_id: str):
        """Manual link; doc_id null = "no document" (sticky: never linked automatically).
        also: more templates of the same service linked to the same document ("apply to similar")."""
        payload = body()
        doc_id, also = payload.get("doc_id"), payload.get("also") or []
        if doc_id is not None and not isinstance(doc_id, str):
            raise BadRequest("doc_id must be a string or null")
        if not isinstance(also, list) or not all(isinstance(t, str) for t in also) or len(also) > 500:
            raise BadRequest("also must be a list of at most 500 template ids")
        c = conn()
        for tid in [template_id, *also]:
            known_template(c, service, tid)   # also: same service only, by construction
        if doc_id is not None and not c.execute("SELECT 1 FROM documents WHERE doc_id = %s", (doc_id,)).fetchone():
            abort(404, "Unknown document")
        for tid in sorted({template_id, *also}):   # sorted: same lock order as the linker
            set_manual(c, service, tid, doc_id)
        enqueue(c, "reload_links")
        c.commit()   # the links and their reload command land together
        return jsonify({"doc_id": doc_id, "linked": len({template_id, *also})})

    @app.delete("/api/templates/<service>/<path:template_id>/link")
    def unlink_template(service: str, template_id: str):
        """Remove the row, whatever its source: the template goes back to automatic matching."""
        c = conn()
        known_template(c, service, template_id)
        gone = c.execute("DELETE FROM template_doc_links WHERE service = %s AND template_id = %s RETURNING doc_id, source",
                         (service, template_id)).fetchone()
        if gone and gone["source"] == "draft":   # its draft document leaves the suggestions queue
            c.execute("UPDATE documents SET status = 'rejected', updated_at = now() WHERE doc_id = %s AND status = 'draft'",
                      (gone["doc_id"],))
        enqueue(c, "reload_links")
        c.commit()
        return jsonify({"doc_id": None})

    def automatic_link(c, service: str, template_id: str) -> dict:
        known_template(c, service, template_id)
        row = c.execute("SELECT doc_id, source FROM template_doc_links WHERE service = %s AND template_id = %s "
                        "FOR UPDATE", (service, template_id)).fetchone()
        if not row or row["source"] not in AUTOMATIC_SOURCES:
            abort(409, "No automatic link to confirm or reject")
        return row

    @app.post("/api/templates/<service>/<path:template_id>/link/confirm")
    def confirm_link(service: str, template_id: str):
        """An inherited, corpus, llm or draft link becomes manual; a draft document becomes active."""
        c = conn()
        row = automatic_link(c, service, template_id)
        if row["source"] == "draft":
            c.execute("UPDATE documents SET status = 'active', updated_at = now() WHERE doc_id = %s", (row["doc_id"],))
        set_manual(c, service, template_id, row["doc_id"])
        enqueue(c, "reload_links")
        c.commit()
        return jsonify({"doc_id": row["doc_id"], "source": "manual"})

    @app.post("/api/templates/<service>/<path:template_id>/link/reject")
    def reject_link(service: str, template_id: str):
        """inherited / corpus -> manual "no document"; llm / draft -> rejected (never sent to the LLM again)."""
        c = conn()
        row = automatic_link(c, service, template_id)
        if row["source"] in ("inherited", "corpus"):
            set_manual(c, service, template_id, None)
        else:
            # linked_at stays: it dates the LLM call, which the resolver's rate limits count
            c.execute("UPDATE template_doc_links SET source = 'rejected' "
                      "WHERE service = %s AND template_id = %s", (service, template_id))
            if row["source"] == "draft":
                c.execute("UPDATE documents SET status = 'rejected', updated_at = now() "
                          "WHERE doc_id = %s AND status = 'draft'", (row["doc_id"],))
        enqueue(c, "reload_links")
        c.commit()
        return jsonify({"source": "manual" if row["source"] in ("inherited", "corpus") else "rejected"})

    @app.get("/api/templates/<service>/<path:template_id>/similar")
    def similar_templates(service: str, template_id: str):
        """Apply-to-similar list: same service and level class, pre-ticked at the inherit threshold."""
        c = conn()
        row = c.execute("SELECT level, embedding IS NOT NULL AS embedded FROM templates "
                        "WHERE service = %s AND template_id = %s", (service, template_id)).fetchone()
        if not row:
            abort(404, "Unknown template")
        tick = cfg.documentation.inherit_similarity[level_class(row["level"])]
        items = similar(c, service, template_id, cfg.documentation.suggest_similarity)
        for i in items:
            i["pre_ticked"] = i["similarity"] >= tick and i["current_source"] != "manual"
        return jsonify({"embedded": row["embedded"], "items": items})

    # --- LLM profiles (as in logai-engine): the linker's resolver uses the active one. The API key is
    # write-only: never returned, kept when a save leaves it empty.

    def profile_or_404(c, name: str) -> dict:
        row = c.execute("SELECT * FROM llm_profiles WHERE name = %s FOR UPDATE", (name,)).fetchone()
        if not row:
            abort(404, "Unknown profile")
        return row

    @app.get("/api/llm/profiles")
    def llm_profiles():
        return jsonify(conn().execute(
            "SELECT name, base_url, model, timeout_seconds, active, api_key <> '' AS has_api_key, revision "
            "FROM llm_profiles ORDER BY name").fetchall())

    @app.get("/api/llm/usage")
    def llm_usage():
        """What the resolver produced (by link source) in the last hour and day, and the linker's LLM state."""
        c = conn()
        rows = c.execute(
            "SELECT source, count(*) FILTER (WHERE linked_at > now() - interval '1 hour') AS hour, count(*) AS day "
            "FROM template_doc_links WHERE source IN ('llm', 'draft', 'rejected') "
            "AND linked_at > now() - interval '1 day' GROUP BY source").fetchall()
        pending = c.execute("SELECT count(*) AS n FROM documents WHERE status = 'draft'").fetchone()["n"]
        row = c.execute("SELECT status FROM worker_status WHERE worker = 'linker'").fetchone()
        linker = row["status"] if row else {}
        return jsonify({
            "hour": {r["source"]: r["hour"] for r in rows}, "day": {r["source"]: r["day"] for r in rows},
            "drafts_waiting": pending, "limits": {"per_service_hour": cfg.llm.per_service_hour,
                                                  "daily_max": cfg.llm.daily_max},
            "linker_running": bool(linker) and time.time() - (linker.get("heartbeat") or 0) <= LINKER_STALE_SECONDS,
            "last_cycle": linker.get("llm"), "last_error": linker.get("last_error"),
            "warnings": linker.get("warnings") or []})

    @app.put("/api/llm/profiles/<name>")
    def put_llm_profile(name: str):
        payload = body()
        if not PROFILE_NAME_RE.fullmatch(name):
            raise BadRequest("name must be 1-64 letters, digits, '.', '_' or '-'")
        base_url, model = _text(payload.get("base_url"), "base_url"), _text(payload.get("model"), "model")
        url = urlparse(base_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise BadRequest("base_url must be an http(s) URL without credentials")
        if not model:
            raise BadRequest("model is required")
        timeout = payload.get("timeout_seconds", 120)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 5 <= timeout <= 600:
            raise BadRequest("timeout_seconds must be an integer from 5 to 600")
        api_key = _text(payload.get("api_key"), "api_key")   # no control characters: it goes into a header
        if not api_key.isascii():
            raise BadRequest("api_key must be ASCII")
        c = conn()
        row = c.execute("SELECT revision, api_key FROM llm_profiles WHERE name = %s FOR UPDATE", (name,)).fetchone()
        if (row["revision"] if row else None) != payload.get("revision"):
            raise db.StaleRevision(row["revision"] if row else None)
        revision = uuid.uuid4().hex
        # a create (no row read) must not overwrite one created meanwhile: DO UPDATE only on an update
        written = c.execute(
            "INSERT INTO llm_profiles (name, base_url, model, api_key, timeout_seconds, revision) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (name) DO UPDATE SET base_url = EXCLUDED.base_url, "
            "model = EXCLUDED.model, api_key = EXCLUDED.api_key, timeout_seconds = EXCLUDED.timeout_seconds, "
            "revision = EXCLUDED.revision, updated_at = now() WHERE %s",
            (name, base_url.rstrip("/"), model, api_key or (row["api_key"] if row else ""), timeout, revision,
             row is not None)).rowcount
        if not written:
            c.rollback()
            current = c.execute("SELECT revision FROM llm_profiles WHERE name = %s", (name,)).fetchone()
            raise db.StaleRevision(current["revision"] if current else None)
        c.commit()
        return jsonify({"name": name, "revision": revision})

    @app.post("/api/llm/profiles/<name>/activate")
    def activate_llm_profile(name: str):
        """{"active": true} makes it the only active profile; false leaves none active (the resolver idles)."""
        active = body().get("active")
        if not isinstance(active, bool):
            raise BadRequest("active must be true or false")
        c = conn()
        profile_or_404(c, name)
        c.execute("UPDATE llm_profiles SET active = false WHERE active")
        if active:
            c.execute("UPDATE llm_profiles SET active = true WHERE name = %s", (name,))
        c.commit()
        return jsonify({"name": name, "active": active})

    @app.delete("/api/llm/profiles/<name>")
    def delete_llm_profile(name: str):
        c = conn()
        profile_or_404(c, name)
        c.execute("DELETE FROM llm_profiles WHERE name = %s", (name,))
        c.commit()
        return jsonify({"name": name})

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(STATIC_DIR, filename)

    return app
