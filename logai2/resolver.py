"""LLM resolver (layers 3-4, design section 4a): for a template no layer 0-2 link covers, the LLM
either confirms one of the nearest documents (an `llm` link, drives alerts) or writes a suggested
document (`status draft`, a `draft` link, drives nothing until a person approves it).

The LLM sees the template (service, level, normalized text: numbers and ids are already <*>) and the
candidate documents only; no raw log lines.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Callable

import psycopg

from logai2.commands import enqueue
from logai2.config import AppConfig
from logai2.links import candidates, class_sql, next_doc_id
from logai2.llm import LlmError, active_profile, chat_json

logger = logging.getLogger("logai2.resolver")

PER_CYCLE = 3                 # LLM calls are slow (6-60 s); the linker cycle stays responsive
FAILURE_BACKOFF_SECONDS = 1800

SYSTEM = ("You link log templates of a telecom platform to operational runbooks. Answer only from the "
          "template and documents given; never invent facts about the system. In template text, <*> marks a "
          "masked value (a number, id, phone number or address). Reply with one JSON object and nothing else.")

CONFIRM_TASK = ("Pick the document whose ERROR field describes the same error as this template's log line and "
                "whose Handling applies to it. If none clearly fits, answer null. "
                'Reply {"doc_id": "<one of the given doc_id values>" or null, "reason": "<one sentence>"}.')

SUGGEST_TASK = ("No runbook covers this template. Write one. If the line shows normal operation (for example a "
                'success or progress message), set "verdict": "safe" and start "handling" with "SAFE: normal '
                'operation, no action needed." followed by what a spike or a drop of this line would mean. '
                'Otherwise set "verdict": "action" and give numbered, concrete steps. Reply {"verdict": "action" '
                'or "safe", "title": "<short title>", "error_code": "<UPPER_SNAKE_CASE>", "error": "<the '
                'template\'s log line, <*> kept>", "handling": "<text>", "confidence": <0..1>}.')


def _template(t: dict) -> dict:
    return {"service": t["service"], "level": t["level"], "text": t["text"]}


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


class Resolver:
    def __init__(self, cfg: AppConfig, conn, chat: Callable = chat_json, now: Callable[[], float] = time.time):
        self.cfg = cfg
        self.conn = conn
        self.chat = chat
        self.now = now
        self._failed: dict[tuple[str, str], float] = {}   # template -> retry after (in memory: a restart retries)

    def run(self, status: dict) -> None:
        lc = self.cfg.llm
        with self.conn.transaction():
            profile = active_profile(self.conn)
            if not profile:
                status["warnings"].append("no active LLM profile: templates are not sent to the LLM")
                return
            # what the LLM produced in the last day / hour (rejected rows came from it too)
            used = self.conn.execute(
                "SELECT service, count(*) FILTER (WHERE linked_at > now() - interval '1 hour') AS hour, "
                "count(*) AS day FROM template_doc_links WHERE source IN ('llm', 'draft', 'rejected') "
                "AND linked_at > now() - interval '1 day' GROUP BY service").fetchall()
            todo = self.conn.execute(f"""
                SELECT t.service, t.template_id, t.level, t.text FROM templates t
                LEFT JOIN alert_state a ON a.service = t.service AND a.key = t.template_id
                WHERE t.embedding IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM template_doc_links l
                                  WHERE l.service = t.service AND l.template_id = t.template_id)
                  AND (a.state = 'ALERTING' OR t.first_seen > now() - make_interval(hours => %s))
                ORDER BY (a.state = 'ALERTING') IS TRUE DESC,
                         CASE {class_sql('t.level')} WHEN 'ERROR' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
                         t.last_seen DESC
                LIMIT 50""", (lc.new_hours,)).fetchall()
        per_service = {r["service"]: r["hour"] for r in used}
        day = sum(r["day"] for r in used)
        done = {"confirmed": 0, "suggested": 0, "none": 0, "failed": 0}
        calls = 0
        for t in todo:
            key = (t["service"], t["template_id"])
            if calls >= PER_CYCLE or day >= lc.daily_max:
                break
            if per_service.get(t["service"], 0) >= lc.per_service_hour or self._failed.get(key, 0) > self.now():
                continue
            calls += 1
            try:
                outcome = self._resolve(profile, t)
            except LlmError as exc:
                self._failed[key] = self.now() + FAILURE_BACKOFF_SECONDS
                status["last_error"] = f"llm: {exc}"
                logger.warning("LLM failed for %s %s: %s", *key, exc)
                done["failed"] += 1
                continue
            done[outcome] += 1
            if outcome != "none":
                day += 1
                per_service[t["service"]] = per_service.get(t["service"], 0) + 1
        status["llm"] = done
        if day >= lc.daily_max:
            status["warnings"].append(f"LLM daily limit reached ({lc.daily_max})")

    def _resolve(self, profile: dict, t: dict) -> str:
        lc = self.cfg.llm
        with self.conn.transaction():
            docs = candidates(self.conn, t["service"], t["template_id"], lc.candidates, lc.candidate_similarity)
        if docs:
            pick, reason = self.confirm(profile, t, docs)
            if pick:
                return "confirmed" if self._write_llm_link(t, pick, reason) else "none"
        reply = self.chat(profile, SYSTEM, json.dumps({"template": _template(t), "task": SUGGEST_TASK},
                                                      ensure_ascii=False))
        return "suggested" if self._write_draft(t, reply) else "none"

    def confirm(self, profile: dict, t: dict, docs: list[dict]) -> tuple[dict | None, str]:
        """Ask which candidate fits; (the candidate or None, reason). Writes nothing (link_eval uses it)."""
        budget = max(200, (self.cfg.llm.max_input_chars - len(t["text"]) - 1000) // len(docs))
        listed = [{"doc_id": d["doc_id"], "title": d["title"], "error": d["error"][:budget // 2],
                   "handling": d["handling"][:budget // 2]} for d in docs]
        reply = self.chat(profile, SYSTEM, json.dumps({"template": _template(t), "documents": listed,
                                                       "task": CONFIRM_TASK}, ensure_ascii=False))
        pick = next((d for d in docs if d["doc_id"] == reply.get("doc_id")), None)   # only a listed one
        return pick, _text(reply.get("reason"), 500)

    def _write_llm_link(self, t: dict, doc: dict, reason: str) -> bool:
        with self.conn.transaction():
            # DO NOTHING: anything written meanwhile (a person, layers 1-2) wins
            if not self.conn.execute(
                    "INSERT INTO template_doc_links (service, template_id, doc_id, source, similarity, note) "
                    "VALUES (%s, %s, %s, 'llm', %s, %s) ON CONFLICT DO NOTHING",
                    (t["service"], t["template_id"], doc["doc_id"], doc["similarity"], reason)).rowcount:
                return False
            enqueue(self.conn, "reload_links")   # an llm link drives alerts
        return True

    def _write_draft(self, t: dict, reply: dict) -> bool:
        handling = _text(reply.get("handling"), 20000)
        title = _text(reply.get("title"), 200)
        if not handling or not title:
            raise LlmError("suggestion without title or handling")
        verdict = reply.get("verdict") if reply.get("verdict") in ("action", "safe") else "action"
        try:
            confidence = min(max(float(reply.get("confidence")), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = None
        written = False
        with self.conn.transaction():
            doc_id = next_doc_id(self.conn)
            self.conn.execute(
                "INSERT INTO documents (doc_id, title, error_code, error, handling, status, source, verdict, "
                "confidence, revision) VALUES (%s, %s, %s, %s, %s, 'draft', 'llm', %s, %s, %s)",
                (doc_id, title, _text(reply.get("error_code"), 100), _text(reply.get("error"), 5000) or t["text"],
                 handling, verdict, confidence, uuid.uuid4().hex))
            written = bool(self.conn.execute(
                "INSERT INTO template_doc_links (service, template_id, doc_id, source) "
                "VALUES (%s, %s, %s, 'draft') ON CONFLICT DO NOTHING",
                (t["service"], t["template_id"], doc_id)).rowcount)
            if not written:   # linked meanwhile: drop the draft document too
                raise psycopg.Rollback()
        return written
