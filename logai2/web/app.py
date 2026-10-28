"""LogAI2 console web API. Talks only to Postgres: never to the engine, never to Elasticsearch."""
from __future__ import annotations

import hmac
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List
from urllib.parse import urlparse

import psycopg
from flask import Flask, abort, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from logai2 import db
from logai2.alerts import alert_rows
from logai2.commands import enqueue
from logai2.jobs.reread import Scope
from logai2.config import AppConfig
from logai2.module_alerts import data_until, module_alerts, parse_window
from logai2.links import ALERTING_SOURCES, LEVEL_ORDER, class_sql, cluster_edges, error_lines, level_class, next_doc_id, similar
from logai2.owner.baseline import DAY_CAP, Baseline, full_after, judge
from logai2.review import suggest
from logai2.timeutil import TZ, day_hour, minute_of
from logai2.web import eval_view, modules_view
from logai2.web.baseline_view import UnknownKey, incident, inspect, left_days, matrix, series
from logai2.web.pipeline_view import stage_states, throughput
from logai2.web.pipeline_view import services as service_rows

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
VERDICTS = ("action", "safe")
KEEP = object()   # a field left out of an update keeps its value
LEVEL_CLASSES = ("ERROR", "WARN", "INFO")   # shown as ERROR (something to fix) and SAFE (normal operation)
AUTOMATIC_SOURCES = ("inherited", "corpus", "llm", "draft")
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
LINK_SOURCES = ("manual", "inherited", "corpus", "llm", "draft", "rejected")
# known = linked to a document that drives alerting; drafts, rejected, "no document" and unlinked are unknown.
# One %s: ALERTING_SOURCES. IS TRUE so an unlinked template (NULLs) reads false, not NULL.
KNOWN_SQL = "(l.doc_id IS NOT NULL AND l.source = ANY(%s)) IS TRUE"


class BadRequest(ValueError):
    pass


def iso(ts: float | None) -> str | None:
    """UTC ISO-8601 with Z; milliseconds only when non-zero."""
    if ts is None:
        return None
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.isoformat(timespec="milliseconds" if d.microsecond else "seconds").replace("+00:00", "Z")


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

    def save(key: str, value: dict, revision: str | None, notify: bool = True) -> str:
        _, current = setting(key)
        # put_setting treats revision None as "overwrite"; for the web None means "nothing saved yet"
        if revision != current:
            raise db.StaleRevision(current)
        new = db.put_setting(conn(), key, value, revision)
        if notify:
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
        light = {"status": status or None, "cursors": cursors, "stale": stale,
                 "linker": linker, "linker_stale": linker_stale}
        if request.args.get("lite"):      # the header pill polls this on every page: skip the per-service queries
            return jsonify(light)
        now = time.time()
        day, hour = day_hour(minute_of(now))
        rows = service_rows(conn(), cfg, status, day, hour)
        return jsonify({**light, "services": rows, "llm_daily_max": cfg.llm.daily_max,
                        "throughput": throughput(status),
                        "stages": stage_states(status, stale, linker, linker_stale,
                                               sum(r["unparsed_1h"] for r in rows), now)})

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

    @app.get("/api/module-alerts")
    def module_alerts_route():
        token = cfg.web.api_token
        if token and not hmac.compare_digest(request.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()):
            return jsonify({"error": "unauthorized"}), 401
        try:
            module, from_s, to_s, start, end = parse_window(request.args, time.time(), cfg.retention.counts_days)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        c = conn()
        items = module_alerts(c, module, start, end, cfg.alert.warm_consecutive)
        for it in items:
            for p in it["periods"]:
                p["start"], p["end"] = iso(p["start"]), iso(p["end"])
            for s in it["module"]["series"]:
                k = "minute" if "minute" in s else "hour"
                s[k] = iso(s[k])
        return jsonify({"module_code": module, "from": iso(from_s), "to": iso(to_s),
                        "effective_from": iso(start), "effective_to": iso(end), "resolution": "minute",
                        "data_until": iso(data_until(c)), "unit": "events_per_minute", "templates": items})

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
        handling = {r["doc_id"]: r["handling"] for r in c.execute(
            "SELECT doc_id, handling FROM documents WHERE doc_id = ANY(%s)", (doc_ids,))}
        drafts = {(r["service"], r["template_id"]): r["doc_id"] for r in c.execute(
            "SELECT service, template_id, doc_id FROM template_doc_links WHERE source = 'draft' AND doc_id IS NOT NULL "
            "AND (service, template_id) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
            ([s for s, _ in pairs], [k for _, k in pairs]))}
        for r in rows:
            r["since"] = r["since"].timestamp()
            if r["kind"] == "document":   # the first step of Handling, without its list marker
                lines = [re.sub(r"^(\d+[.)]|-)\s*", "", ln.strip()) for ln in (handling.get(r["doc_id"]) or "").splitlines()]
                r["handling_step"] = next((ln for ln in lines if ln), None)
            elif r["kind"] in ("template", "cluster"):
                r["draft_doc_id"] = next((drafts[(r["service"], k)] for k in r["keys"] if (r["service"], k) in drafts), None)
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

    @app.get("/api/template-levels")
    def template_levels():
        # every level present in the data (levels are stored as read from the logs), with its template count
        service = request.args.get("service")
        rows = conn().execute("SELECT level, count(*) AS n FROM templates WHERE %s::text IS NULL OR service = %s "
                              "GROUP BY level", (service, service)).fetchall()
        rows.sort(key=lambda r: (LEVEL_ORDER.index(r["level"]) if r["level"] in LEVEL_ORDER else len(LEVEL_ORDER), r["level"]))
        return jsonify(rows)

    @app.get("/api/documents")
    def documents():
        # Also feeds the Templates link select (doc_id, title). Drafts and rejected ones: ?status=draft|rejected.
        status = request.args.get("status", "active")
        if status not in DOC_STATUSES:
            raise BadRequest(f"status must be one of {', '.join(DOC_STATUSES)}")
        rows = conn().execute(
            "SELECT d.doc_id, d.service, d.level, d.based_on, d.title, d.error_code, d.error, d.handling, d.status, d.source, d.verdict, "
            "d.confidence, d.revision, d.embedded_revision IS NOT DISTINCT FROM d.revision AS embedded, "
            "count(l.template_id) FILTER (WHERE l.source = ANY(%s)) AS linked_templates, "
            "max(t.level) FILTER (WHERE l.source = 'draft') AS template_level "   # level of the template a draft was written for
            "FROM documents d LEFT JOIN template_doc_links l USING (doc_id) "
            "LEFT JOIN templates t ON t.service = l.service AND t.template_id = l.template_id WHERE d.status = %s "
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
        verdict = payload.get("verdict")   # ERROR (action) or SAFE; None on an update keeps the current one
        if verdict is not None and verdict not in VERDICTS:
            raise BadRequest("verdict must be action or safe")
        # service: absent keeps it (update) / shared (create); "" or null = shared (every service)
        service = KEEP
        if "service" in payload:
            service = payload["service"] or None
            if service is not None and not (isinstance(service, str) and conn().execute(
                    "SELECT 1 FROM services WHERE service = %s", (service,)).fetchone()):
                raise BadRequest("service must be a known service, or empty for shared")
        # level of the ERROR line: ERROR / WARN / INFO, "" or null = any; absent keeps it (update) / any (create)
        level = KEEP
        if "level" in payload:
            level = payload["level"] or None
            if level is not None and level not in LEVEL_CLASSES:
                raise BadRequest("level must be ERROR, WARN, INFO or empty for any")
        return {**fields, "verdict": verdict, "service": service, "level": level, "revision": payload.get("revision")}

    @app.post("/api/documents")
    def create_document():
        fields = document_fields()
        c = conn()
        doc_id, revision = next_doc_id(c), uuid.uuid4().hex
        c.execute("INSERT INTO documents (doc_id, service, level, title, error_code, error, handling, verdict, revision) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                  (doc_id, None if fields["service"] is KEEP else fields["service"],
                   None if fields["level"] is KEEP else fields["level"], fields["title"],
                   fields["error_code"], fields["error"], fields["handling"], fields["verdict"] or "action", revision))
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
        keep_service = fields["service"] is KEEP
        c.execute("UPDATE documents SET title = %s, error_code = %s, error = %s, handling = %s, "
                  "verdict = COALESCE(%s, verdict), service = CASE WHEN %s THEN service ELSE %s END, "
                  "level = CASE WHEN %s THEN level ELSE %s END, revision = %s, updated_at = now() WHERE doc_id = %s",
                  (fields["title"], fields["error_code"], fields["error"], fields["handling"], fields["verdict"],
                   keep_service, None if keep_service else fields["service"],
                   fields["level"] is KEEP, None if fields["level"] is KEEP else fields["level"], revision, doc_id))
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

    def parse_range(args, strict: bool = False) -> tuple[date, int, int]:
        """day, hour_from, hour_to of a query string, or of a JSON body with strict=True (hours must be real ints)."""
        try:
            day = date.fromisoformat(args.get("day"))
        except (TypeError, ValueError):
            raise BadRequest("day must be an ISO date (YYYY-MM-DD)") from None
        try:
            hour_from, hour_to = args.get("hour_from"), args.get("hour_to")
            if not strict:
                hour_from, hour_to = int(hour_from), int(hour_to)
            if not (type(hour_from) is int and type(hour_to) is int and 0 <= hour_from < hour_to <= 24):
                raise ValueError
        except (TypeError, ValueError):
            raise BadRequest("hours must be whole numbers with 0 <= hour_from < hour_to <= 24") from None
        return day, hour_from, hour_to

    def known_service(service: str | None) -> str:
        if not service or not conn().execute("SELECT 1 FROM services WHERE service = %s", (service,)).fetchone():
            abort(404, "Unknown service")
        return service

    @app.get("/api/baseline/groups")
    def baseline_groups():
        """Exact key picker counts: templates per level class, and active documents per verdict with a linked template here."""
        service = request.args.get("service")
        if not service:
            raise BadRequest("service is required")
        c = conn()
        cls = class_sql("level")
        row = c.execute(
            "SELECT count(*) AS n, " + ", ".join(f"count(*) FILTER (WHERE {cls} = '{k}') AS \"{k}\"" for k in LEVEL_CLASSES)
            + " FROM templates WHERE service = %s", (service,)).fetchone()
        docs = {r["verdict"]: r["n"] for r in c.execute(
            "SELECT d.verdict, count(DISTINCT d.doc_id) AS n FROM documents d JOIN template_doc_links l ON l.doc_id = d.doc_id "
            "WHERE d.status = 'active' AND (d.service IS NULL OR d.service = %s) AND l.service = %s AND l.source = ANY(%s) "
            "GROUP BY d.verdict", (service, service, list(ALERTING_SOURCES)))}
        return jsonify({"all": row["n"], **{k: row[k] for k in LEVEL_CLASSES}, **{v: docs.get(v, 0) for v in VERDICTS}})

    @app.get("/api/baseline/matrix")
    def baseline_matrix():
        service = known_service(request.args.get("service"))
        try:
            ndays = int(request.args.get("days", "14"))
            if not 1 <= ndays <= 28:
                raise ValueError
        except ValueError:
            raise BadRequest("days must be an integer from 1 to 28") from None
        try:
            return jsonify(matrix(conn(), cfg, service, request.args.get("key", "*total"), ndays,
                                  datetime.now(TZ).date(), time.time()))
        except UnknownKey:
            abort(404, "Unknown key")

    @app.get("/api/baseline/inspect")
    def baseline_inspect():
        service = known_service(request.args.get("service"))
        day, hour_from, hour_to = parse_range(request.args)
        try:
            return jsonify(inspect(conn(), cfg, service, request.args.get("key", "*total"), day,
                                   hour_from, hour_to, time.time()))
        except UnknownKey:
            abort(404, "Unknown key")

    def eval_bad_param(name: str):
        raise eval_view.BadRequest(f"{name} is required")

    # ---- evaluation (lab_* tables only; eval_view.py)
    def lab_run():
        return eval_view.get_run(conn(), request.args.get("run"))

    @app.errorhandler(eval_view.BadRequest)
    def eval_bad(exc):
        return jsonify({"error": str(exc)}), 400

    @app.errorhandler(eval_view.NoRun)
    def eval_no_run(exc):
        return jsonify({"error": str(exc)}), 404

    @app.get("/api/eval/runs")
    def eval_runs():
        return jsonify(eval_view.runs(conn(), iso))

    @app.get("/api/eval/scoreboard")
    def eval_scoreboard():
        return jsonify(eval_view.scoreboard(conn(), lab_run()))

    @app.get("/api/eval/keys")
    def eval_keys():
        run = lab_run()
        return jsonify(eval_view.keys(conn(), run, request.args.get("kind") or eval_bad_param("kind")))

    @app.get("/api/eval/series")
    def eval_series():
        run, a = lab_run(), request.args
        for name in ("kind", "service", "key", "from", "to"):
            if not a.get(name):
                eval_bad_param(name)
        try:
            d_from, d_to = date.fromisoformat(a["from"]), date.fromisoformat(a["to"])
            knob = float(a["knob"]) if a.get("knob") else None
        except ValueError:
            raise eval_view.BadRequest("from/to must be ISO dates and knob a number") from None
        methods = [m for m in a.get("methods", "").split(",") if m] or None
        return jsonify(eval_view.series(conn(), run, iso, a["kind"], a["service"], a["key"], methods, knob, d_from, d_to))

    @app.get("/api/eval/sweep")
    def eval_sweep():
        run = lab_run()
        return jsonify(eval_view.sweep(conn(), run, request.args.get("kind") or eval_bad_param("kind")))

    @app.get("/api/eval/choices")
    def eval_choices_list():
        return jsonify(eval_view.choices(conn(), iso))

    @app.post("/api/eval/choices")
    def eval_choices():
        return jsonify(eval_view.choose(conn(), body()))

    @app.get("/api/baseline/series")
    def baseline_series():
        service = known_service(request.args.get("service"))
        if request.args.get("days", "14") not in ("1", "7", "14"):
            raise BadRequest("days must be 1, 7 or 14")
        try:
            return jsonify(series(conn(), cfg, service, request.args.get("key", "*total"),
                                  int(request.args.get("days", "14")), datetime.now(TZ).date(), time.time()))
        except UnknownKey:
            abort(404, "Unknown key")

    @app.get("/api/baseline/incident")
    def baseline_incident():
        service = known_service(request.args.get("service"))
        day, hour_from, hour_to = parse_range(request.args)
        try:
            return jsonify(incident(conn(), service, request.args.get("key", "*total"), day,
                                    hour_from, hour_to, time.time()))
        except UnknownKey:
            abort(404, "Unknown key")

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
                        "last_commit": last_commit, "commit_time": cfg.baseline.commit_time,
                        "full_after_days": {dt: full_after(cfg, dt) for dt in DAY_CAP}})

    @app.post("/api/baseline/decisions")
    def add_decision():
        payload = body()
        day, hour_from, hour_to = parse_range(payload, strict=True)
        decision = payload.get("decision")
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
        # Hours of this range left with < 3 of the days the rebuild uses: their baseline comes from only those few.
        short = [d["hour"] for d in left_days(c, service, day, hour_from, hour_to, True) if d["days"] < 3] \
            if decision == "exclude" else []
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

    # --- Retrain: reads retrain_runs / settings / worker_status; actions are queued as engine commands.
    RUN_COLS = ("id, kind, trigger, scope, state, extract(epoch FROM started_at)::float8 AS started_at, "
                "extract(epoch FROM finished_at)::float8 AS finished_at, steps, result, error, error_type, failed_step")

    def retrain_payload() -> dict:
        c = conn()
        saved, revision = setting("retrain_schedule")
        status = engine_status()
        schedule = {"enabled": True, "time": cfg.baseline.commit_time, "weekdays": list(range(7))}
        schedule.update(saved or {})
        runs = c.execute(f"SELECT {RUN_COLS} FROM retrain_runs ORDER BY started_at DESC, id DESC LIMIT 50").fetchall()
        current = c.execute(f"SELECT {RUN_COLS} FROM retrain_runs WHERE state = 'running' "
                            "ORDER BY started_at DESC LIMIT 1").fetchone()
        return {"schedule": schedule, "revision": revision, "saved": saved is not None,
                "next_run_at": status.get("next_commit_at"), "timezone": str(TZ), "current": current, "runs": runs,
                "es_oldest": status.get("es_oldest") or {},
                "services": [r["service"] for r in c.execute("SELECT service FROM services ORDER BY service")],
                "llm_active": c.execute("SELECT EXISTS (SELECT 1 FROM llm_profiles WHERE active) AS a").fetchone()["a"],
                "engine_stale": time.time() - (status.get("heartbeat") or 0) > STALE_HEARTBEAT_SECONDS,
                "default_rate": cfg.retrain.default_rate}

    @app.get("/api/retrain")
    def retrain():
        return jsonify(retrain_payload())

    @app.get("/api/retrain/runs/<int:run_id>")
    def retrain_run_detail(run_id: int):
        row = conn().execute(f"SELECT {RUN_COLS}, log FROM retrain_runs WHERE id = %s", (run_id,)).fetchone()
        if not row:
            abort(404, "Unknown run")
        return jsonify(row)

    @app.put("/api/retrain/schedule")
    def put_retrain_schedule():
        payload = body()
        raw = payload.get("schedule")
        if not isinstance(raw, dict):
            raise BadRequest("schedule required")
        enabled, at, days = raw.get("enabled"), raw.get("time"), raw.get("weekdays")
        if not isinstance(enabled, bool):
            raise BadRequest("enabled must be true or false")
        if not (isinstance(at, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at)):
            raise BadRequest("time must be HH:MM")
        if not isinstance(days, list) or not days:
            raise BadRequest("choose at least one day")
        if not all(type(d) is int and 0 <= d <= 6 for d in days) or len(set(days)) != len(days):
            raise BadRequest("weekdays must be distinct numbers 0-6")
        save("retrain_schedule", {"enabled": enabled, "time": at, "weekdays": sorted(days)},
             payload.get("revision"), notify=False)   # the engine re-reads it every 60 s
        return jsonify(retrain_payload())

    def reread_scope() -> dict:
        p = body()
        days, services = p.get("days"), p.get("services")
        hour_from, hour_to = p.get("hour_from"), p.get("hour_to")
        if not isinstance(days, list) or not days:
            raise BadRequest("choose at least one day")
        today = datetime.now(TZ).date()
        parsed = []
        for d in days:
            try:
                parsed.append(date.fromisoformat(d))
            except (TypeError, ValueError):
                raise BadRequest("days must be YYYY-MM-DD") from None
        if len(set(parsed)) != len(parsed):
            raise BadRequest("days must be distinct")
        if any(not today - timedelta(days=7) <= d <= today for d in parsed):
            raise BadRequest("days must be within the last 7 days")
        if not all(type(h) is int for h in (hour_from, hour_to)) or not 0 <= hour_from < hour_to <= 24:
            raise BadRequest("hours must satisfy 0 <= hour_from < hour_to <= 24")
        if services is not None:
            known = {r["service"] for r in conn().execute("SELECT service FROM services")}
            if not isinstance(services, list) or not services or not all(isinstance(s, str) and s in known for s in services):
                raise BadRequest("services must be a non-empty list of known services")
        return Scope(services, parsed, hour_from, hour_to).to_json()

    @app.post("/api/retrain/runs")
    def retrain_start():
        c = conn()
        kind = body().get("kind")
        if kind == "rebuild":
            command_id = enqueue(c, "recompute_baseline")
        elif kind == "reread":
            scope = reread_scope()
            if c.execute("SELECT 1 FROM retrain_runs WHERE kind = 'reread' AND state = 'running'").fetchone():
                return jsonify({"error": "a retrain is running"}), 409
            command_id = enqueue(c, "retrain_run", scope)
        else:
            raise BadRequest("kind must be rebuild or reread")
        c.commit()
        return jsonify({"command_id": command_id}), 202

    @app.post("/api/retrain/estimate")
    def retrain_estimate():
        scope = reread_scope()
        command_id = enqueue(conn(), "retrain_estimate", scope)
        conn().commit()
        return jsonify({"command_id": command_id}), 202

    @app.post("/api/retrain/runs/<int:run_id>/cancel")
    def retrain_cancel(run_id: int):
        row = conn().execute("SELECT kind, state FROM retrain_runs WHERE id = %s", (run_id,)).fetchone()
        if not row:
            abort(404, "Unknown run")
        if (row["kind"], row["state"]) != ("reread", "running"):
            return jsonify({"error": "only a running re-read can be cancelled"}), 409
        command_id = enqueue(conn(), "retrain_cancel", {"run_id": run_id})
        conn().commit()
        return jsonify({"command_id": command_id}), 202

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
        link_source = request.args.get("link_source") or None
        if link_source not in (None, "none", "review", *LINK_SOURCES):
            raise BadRequest(f"link_source must be none, review or one of {', '.join(LINK_SOURCES)}")
        status = request.args.get("status") or None
        if status not in (None, "known", "unknown"):
            raise BadRequest("status must be known or unknown")
        sort = request.args.get("sort") or "last_seen"
        if sort not in ("first_seen", "last_seen"):
            raise BadRequest("sort must be first_seen or last_seen")
        where, args = ["true"], []   # every filter but link_source: review_total counts within these
        for column, value in (("t.service", request.args.get("service")), ("t.level", request.args.get("level")),
                              ("l.doc_id", request.args.get("doc_id")),
                              ("t.template_id", request.args.get("template_id"))):
            if value:
                where.append(f"{column} = %s")
                args.append(value)
        if request.args.get("q"):
            where.append("strpos(lower(t.text), lower(%s)) > 0")   # substring, no LIKE wildcards to escape
            args.append(request.args["q"])
        module = request.args.get("module")
        if module is not None:   # '' = "(no module)"; narrows total, stats and the items alike
            where.append("EXISTS (SELECT 1 FROM template_modules m WHERE m.service = t.service "
                         "AND m.template_id = t.template_id AND m.module_code = %s)")
            args.append(module)
        base_sql, base_args = " AND ".join(where), list(args)   # the stats cards count within these too
        review_sql, review_args = " AND ".join([*where, "l.source = ANY(%s)"]), [*args, list(AUTOMATIC_SOURCES)]
        if link_source == "none":
            where.append("l.template_id IS NULL")
        elif link_source == "review":
            where, args = [review_sql], list(review_args)   # a copy: status appends to args
        elif link_source:
            where.append("l.source = %s")
            args.append(link_source)
        if status:   # like link_source, it does not narrow review_total or the stats cards
            where.append(f"{'' if status == 'known' else 'NOT '}{KNOWN_SQL}")
            args.append(list(ALERTING_SOURCES))
        where_sql = " AND ".join(where)
        order = f"t.{sort} DESC, t.template_id"
        if link_source == "review":   # what needs a decision first: alerting, then the worst level
            order = (f"(a.state = 'ALERTING') IS TRUE DESC, CASE {class_sql('t.level')} WHEN 'ERROR' THEN 0 "
                     f"WHEN 'WARN' THEN 1 ELSE 2 END, t.last_seen DESC, t.template_id")   # sort is ignored here
        order_args = []
        if module is not None:   # lines in the window in that module first, then the usual order
            days, bdays, bhours = modules_view.window_args(modules_view.clock())
            order = ("EXISTS (SELECT 1 FROM template_module_hours h WHERE h.service = t.service AND h.template_id = "
                     f"t.template_id AND h.module_code = %s AND {modules_view.WINDOW_SQL}) DESC, " + order)
            order_args = [module, days, bdays, bhours]
        c = conn()
        count_sql = ("SELECT count(*) AS n FROM templates t LEFT JOIN template_doc_links l "
                     "ON l.service = t.service AND l.template_id = t.template_id WHERE ")
        total = c.execute(count_sql + where_sql, args).fetchone()["n"]
        review_total = c.execute(count_sql + review_sql, review_args).fetchone()["n"]
        level = class_sql("t.level")
        stats = c.execute(
            f"SELECT count(*) AS total, count(*) FILTER (WHERE {level} = 'ERROR') AS error, "
            f"count(*) FILTER (WHERE {level} = 'WARN') AS warn, count(*) FILTER (WHERE {level} = 'INFO') AS info, "
            f"count(*) FILTER (WHERE {KNOWN_SQL}) AS known FROM templates t "
            "LEFT JOIN template_doc_links l ON l.service = t.service AND l.template_id = t.template_id WHERE " + base_sql,
            [list(ALERTING_SOURCES), *base_args]).fetchone()
        stats["unknown"] = stats["total"] - stats["known"]
        items = c.execute(
            "SELECT t.service, t.template_id, t.text, t.level, t.first_seen, t.last_seen, l.doc_id, "
            "d.title AS doc_title, d.verdict AS doc_verdict, l.source AS link_source, l.similarity, l.inherited_from, "
            f"l.note, t.embedding IS NOT NULL AS embedded, COALESCE(a.state, 'NORMAL') AS state, {KNOWN_SQL} AS known "
            "FROM templates t "
            "LEFT JOIN template_doc_links l ON l.service = t.service AND l.template_id = t.template_id "
            "LEFT JOIN documents d ON d.doc_id = l.doc_id "
            "LEFT JOIN alert_state a ON a.service = t.service AND a.key = t.template_id "
            f"WHERE {where_sql} ORDER BY {order} LIMIT %s OFFSET %s",
            [list(ALERTING_SOURCES), *args, *order_args, size, (page - 1) * size]).fetchall()

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
        modules = modules_view.modules_for(c, keys, modules_view.clock())
        for i in items:
            svc, tid = i["service"], i["template_id"]
            i["modules"] = modules[(svc, tid)]
            n = minutes.get(svc)
            i["rate"] = sums.get((svc, tid), 0) / n if n else None
            verdict = judge(0, tid, i["level"], baseline.lookup(svc, tid, day, hour), cfg)
            i["band_low"], i["band_high"] = verdict.band_low, verdict.band_high
            i["first_seen"], i["last_seen"] = i["first_seen"].timestamp(), i["last_seen"].timestamp()
        return jsonify({"total": total, "review_total": review_total, "stats": stats, "items": items})

    @app.get("/api/modules")
    def modules():
        """A service's modules: templates ever seen in each and its lines over the last 24 h."""
        service = request.args.get("service")
        if not service:
            raise BadRequest("service is required")
        days, bdays, bhours = modules_view.window_args(modules_view.clock())
        rows = conn().execute(
            "SELECT m.module_code AS module, count(*) AS templates, COALESCE(sum(w.n), 0)::bigint AS lines_24h "
            "FROM template_modules m LEFT JOIN (SELECT template_id, module_code, sum(count) AS n "
            f"FROM template_module_hours h WHERE service = %s AND {modules_view.WINDOW_SQL} GROUP BY 1, 2) w "
            "ON w.template_id = m.template_id AND w.module_code = m.module_code "
            "WHERE m.service = %s GROUP BY m.module_code ORDER BY templates DESC, module",
            (service, days, bdays, bhours, service)).fetchall()
        return jsonify(rows)

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
            "SELECT name, base_url, model, timeout_seconds, priority, active, api_key <> '' AS has_api_key, revision "
            "FROM llm_profiles ORDER BY active DESC, priority, name").fetchall())

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
            "last_cycle": linker.get("llm"), "last_error": linker.get("llm_last_error"),
            "profiles": linker.get("llm_profiles") or {},
            "warnings": linker.get("warnings") or []})

    @app.put("/api/llm/profiles/order")
    def put_llm_order():
        """The call order: the listed profiles are active with priority 1..n, all others inactive."""
        order = body().get("order")
        if not isinstance(order, list) or not all(isinstance(n, str) for n in order) or len(set(order)) != len(order):
            raise BadRequest("order must be a list of distinct profile names")
        c = conn()
        known = {r["name"] for r in c.execute("SELECT name FROM llm_profiles FOR UPDATE").fetchall()}
        if not set(order) <= known:
            raise BadRequest("order names an unknown profile")
        c.execute("UPDATE llm_profiles SET active = false WHERE name <> ALL(%s)", (order,))
        for i, name in enumerate(order, 1):
            c.execute("UPDATE llm_profiles SET active = true, priority = %s WHERE name = %s", (i, name))
        c.commit()
        return jsonify({"order": order})

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
        timeout, priority = payload.get("timeout_seconds", 120), payload.get("priority")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 5 <= timeout <= 600:
            raise BadRequest("timeout_seconds must be an integer from 5 to 600")
        if priority is not None and (not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 1000):
            raise BadRequest("priority must be an integer from 0 to 1000")
        api_key = _text(payload.get("api_key"), "api_key")   # no control characters: it goes into a header
        if not api_key.isascii():
            raise BadRequest("api_key must be ASCII")
        c = conn()
        row = c.execute("SELECT revision, api_key, priority FROM llm_profiles WHERE name = %s FOR UPDATE", (name,)).fetchone()
        if (row["revision"] if row else None) != payload.get("revision"):
            raise db.StaleRevision(row["revision"] if row else None)
        if priority is None:   # an edit keeps its place; a new profile goes after the active ones
            priority = row["priority"] if row else c.execute(
                "SELECT coalesce(max(priority), 0) + 1 AS p FROM llm_profiles WHERE active").fetchone()["p"]
        revision = uuid.uuid4().hex
        # a create (no row read) must not overwrite one created meanwhile: DO UPDATE only on an update
        written = c.execute(
            "INSERT INTO llm_profiles (name, base_url, model, api_key, timeout_seconds, priority, revision) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (name) DO UPDATE SET base_url = EXCLUDED.base_url, "
            "model = EXCLUDED.model, api_key = EXCLUDED.api_key, timeout_seconds = EXCLUDED.timeout_seconds, "
            "priority = EXCLUDED.priority, revision = EXCLUDED.revision, updated_at = now() WHERE %s",
            (name, base_url.rstrip("/"), model, api_key or (row["api_key"] if row else ""), timeout, priority,
             revision, row is not None)).rowcount
        if not written:
            c.rollback()
            current = c.execute("SELECT revision FROM llm_profiles WHERE name = %s", (name,)).fetchone()
            raise db.StaleRevision(current["revision"] if current else None)
        c.commit()
        return jsonify({"name": name, "revision": revision})

    @app.post("/api/llm/profiles/<name>/activate")
    def activate_llm_profile(name: str):
        """Every active profile is used, in priority order: a dead one fails over to the next."""
        active = body().get("active")
        if not isinstance(active, bool):
            raise BadRequest("active must be true or false")
        c = conn()
        profile_or_404(c, name)
        c.execute("UPDATE llm_profiles SET active = %s WHERE name = %s", (active, name))
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
