"""logai2-linker: embeds templates and documents' ERROR lines, keeps layer 1-2 links current and
(Phase B) runs the LLM resolver. Shares only Postgres; the engine reloads links on reload_links."""
from __future__ import annotations

import logging
import time
from typing import Callable

from psycopg.types.json import Jsonb

from logai2.commands import enqueue
from logai2.config import AppConfig
from logai2.embed import EmbeddingError, embed, literal
from logai2.links import apply_links, error_lines

logger = logging.getLogger("logai2.linker")

TEMPLATES_PER_CYCLE = 10      # batches of templates embedded per cycle
DOCUMENTS_PER_CYCLE = 100
RETRY_SECONDS = 30


class Linker:
    def __init__(self, cfg: AppConfig, conn, embed_fn: Callable = embed, resolver=None,
                 now: Callable[[], float] = time.time):
        self.cfg = cfg
        self.conn = conn
        self.embed_fn = embed_fn
        self.resolver = resolver
        self.now = now

    def _embed(self, texts: list[str]) -> list[list[float]]:
        e = self.cfg.embedding
        return self.embed_fn(e.url, e.model, texts, e.api_key, e.timeout_seconds, e.max_retries,
                             e.retry_backoff_seconds, e.dimension)

    def cycle(self) -> dict:
        status = {"heartbeat": self.now(), "embedded_templates": 0, "embedded_documents": 0,
                  "links_changed": False, "warnings": [], "last_error": None}
        if self.cfg.embedding.url:
            try:
                self._embed_templates(status)
                self._embed_documents(status)
            except EmbeddingError as exc:   # service down: links below still use what is embedded
                status["last_error"] = f"embedding: {exc}"
                logger.warning("embedding failed: %s", exc)
        else:
            status["warnings"].append("embedding.url is empty: nothing is embedded")
        with self.conn.transaction():
            if apply_links(self.conn, self.cfg.documentation):
                enqueue(self.conn, "reload_links")   # same transaction as the link change
                status["links_changed"] = True
        if self.resolver:
            # LLM calls take up to minutes: report after each one so the web does not see a stale linker
            self.resolver.run(status, progress=lambda: self._write_status(status))
        with self.conn.transaction():
            status["pending"] = self.conn.execute(
                "SELECT (SELECT count(*) FROM templates WHERE embedding IS NULL "
                "AND service NOT IN (SELECT service FROM services WHERE NOT monitored)) AS templates, "
                "(SELECT count(*) FROM documents WHERE status <> 'rejected' "
                "AND embedded_revision IS DISTINCT FROM revision) AS documents").fetchone()
        self._write_status(status)
        return status

    def _write_status(self, status: dict) -> None:
        status["heartbeat"] = self.now()
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO worker_status (worker, status) VALUES ('linker', %s) ON CONFLICT (worker) "
                "DO UPDATE SET status = EXCLUDED.status, updated_at = now()", (Jsonb(status),))

    def _embed_templates(self, status: dict) -> None:
        batch = self.cfg.embedding.batch
        with self.conn.transaction():
            rows = self.conn.execute(
                "SELECT t.service, t.template_id, t.text FROM templates t LEFT JOIN alert_state a "
                "ON a.service = t.service AND a.key = t.template_id WHERE t.embedding IS NULL "
                "AND t.service NOT IN (SELECT service FROM services WHERE NOT monitored) "
                "ORDER BY (a.state = 'ALERTING') IS TRUE DESC, t.last_seen DESC LIMIT %s",
                (batch * TEMPLATES_PER_CYCLE,)).fetchall()
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            vectors = self._embed([r["text"] for r in chunk])
            with self.conn.transaction(), self.conn.cursor() as cur:
                # AND text = ...: the engine may have changed the text meanwhile; it is embedded next cycle
                cur.executemany(
                    "UPDATE templates SET embedding = %s::vector WHERE service = %s AND template_id = %s AND text = %s",
                    [(literal(v), r["service"], r["template_id"], r["text"]) for r, v in zip(chunk, vectors)])
                status["embedded_templates"] += cur.rowcount   # rows whose text changed meanwhile are not counted

    def _embed_documents(self, status: dict) -> None:
        with self.conn.transaction():
            docs = self.conn.execute(
                "SELECT doc_id, error, revision FROM documents WHERE status <> 'rejected' "
                "AND embedded_revision IS DISTINCT FROM revision ORDER BY doc_id LIMIT %s",
                (DOCUMENTS_PER_CYCLE,)).fetchall()
        for d in docs:
            lines = error_lines(d["error"])
            vectors = self._embed(lines) if lines else []
            with self.conn.transaction():
                # AND revision = ...: an edit meanwhile gets its own pass next cycle
                if not self.conn.execute("UPDATE documents SET embedded_revision = revision "
                                         "WHERE doc_id = %s AND revision = %s",
                                         (d["doc_id"], d["revision"])).rowcount:
                    continue
                self.conn.execute("DELETE FROM document_errors WHERE doc_id = %s", (d["doc_id"],))
                with self.conn.cursor() as cur:
                    cur.executemany("INSERT INTO document_errors (doc_id, n, text, embedding) "
                                    "VALUES (%s, %s, %s, %s::vector)",
                                    [(d["doc_id"], n, t, literal(v)) for n, (t, v) in enumerate(zip(lines, vectors))])
            status["embedded_documents"] += 1

    def run_forever(self, sleep: Callable[[float], None] = time.sleep) -> None:
        while True:
            try:
                self.cycle()
                sleep(self.cfg.embedding.cycle_seconds)
            except Exception:  # noqa: BLE001 - keep running; Postgres or a bug, logged with its trace
                logger.exception("linker cycle failed; retrying in %d s", RETRY_SECONDS)
                if not self.conn.closed:
                    self.conn.rollback()
                sleep(RETRY_SECONDS)
