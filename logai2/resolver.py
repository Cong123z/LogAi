"""LLM resolver (layers 3-4, design section 4a): for a template no layer 0-2 link covers, the LLM
either confirms one of the nearest documents (an `llm` link, drives alerts) or writes a suggested
document (`status draft`, a `draft` link, drives nothing until a person approves it).

The LLM sees the template (service, level, normalized text: numbers and ids are already <*>) and the
candidate documents only; no raw log lines.
"""
from __future__ import annotations

import json
import logging
import math
import time
import uuid
from typing import Callable

import psycopg

from logai2.commands import enqueue
from logai2.config import AppConfig
from logai2.links import candidates, class_sql, level_class, next_doc_id
from logai2.llm import LlmError, LlmUnavailable, active_profiles, chat_json

logger = logging.getLogger("logai2.resolver")

PER_CYCLE = 3                 # LLM calls per cycle (slow, 6-60 s); a template's second call may exceed it by one
FAILURE_BACKOFF_SECONDS = 1800
PROFILE_DOWN_SECONDS = 300    # a profile that failed (network, HTTP, timeout) is skipped this long

SYSTEM = ("You link log templates of a telecom platform to operational runbooks. Answer only from the "
          "template and documents given; never invent facts about the system. In template text, <*> marks a "
          "masked value (a number, id, phone number or address). \"level\" is the log level: of the template, "
          "and of each document's ERROR line (\"any\" when unknown). Judge by meaning, not by shared words: a "
          "success or progress line (usually INFO) is not the same event as an error line, even when most of "
          "their words and fields match. Reply with one JSON object and nothing else.")

CONFIRM_TASK = ("Pick the document whose ERROR field describes the same event as this template's log line and "
                "whose Handling applies to it. Prefer a document of the same level; pick one of another level only "
                "when the template clearly reports the same failure. If none clearly fits, answer null. "
                'Reply {"doc_id": "<one of the given doc_id values>" or null, "reason": "<one sentence>"}.')

SUGGEST_TASK = ("No runbook of this template's service covers it. Write one for this service. If \"references\" "
                "are given, they are runbooks other services wrote for a similar error: adapt them to this service "
                "and template, keep what applies, drop what is specific to the other service. "
                "Use the template's level: an INFO or DEBUG line is usually normal operation unless its text "
                "reports a failure; an ERROR, FATAL or CRITICAL line usually needs action. "
                "If the line shows normal operation (for example a "
                'success or progress message), set "verdict": "safe" and start "handling" with "SAFE: normal '
                'operation, no action needed." followed by what a spike or a drop of this line would mean. '
                'Otherwise set "verdict": "action" and give numbered, concrete steps, one step per line. Reply {"verdict": "action" '
                'or "safe", "title": "<short title>", "error_code": "<UPPER_SNAKE_CASE>", "error": "<the '
                'template\'s log line, <*> kept>", "handling": "<text>", "confidence": <0..1>}.')


def _template(t: dict) -> dict:
    return {"service": t["service"], "level": t["level"], "text": t["text"]}


def _text(value, limit: int) -> str:
    """LLM text made storable: no NUL (Postgres refuses it), no lone surrogates."""
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").encode("utf-8", "ignore").decode("utf-8").strip()[:limit]


class Resolver:
    def __init__(self, cfg: AppConfig, conn, chat: Callable = chat_json, now: Callable[[], float] = time.time):
        self.cfg = cfg
        self.conn = conn
        self.chat = self._counted(chat)
        self.now = now
        # in memory (a restart retries): template -> retry after; (service, time) of calls that wrote nothing
        self._failed: dict[tuple[str, str], float] = {}
        self._spent: list[tuple[str, float]] = []
        self._calls = 0
        self.profiles: list[dict] = []                      # active ones, in the order they are tried
        self._down: dict[str, tuple[float, str]] = {}      # profile name -> (skipped until, error)
        self.last_error: str | None = None                  # kept across cycles for the web's banner
        self.answers: dict[str, int] = {}                   # profile name -> answers since the linker started

    def ask(self, system: str, user: str) -> dict:
        """One question to the first working active profile; a profile that fails is skipped for
        PROFILE_DOWN_SECONDS and the next one is tried. Raises LlmUnavailable when none answers."""
        now = self.now()
        for profile in self.profiles:
            if self._down.get(profile["name"], (0, ""))[0] > now:
                continue
            try:
                reply = self.chat(profile, system, user)
            except LlmUnavailable as exc:
                self._down[profile["name"]] = (self.now() + PROFILE_DOWN_SECONDS, str(exc))
                self.last_error = f"{profile['name']}: {exc}"
                logger.warning("LLM profile %s failed, trying the next one: %s", profile["name"], exc)
                continue
            self.answers[profile["name"]] = self.answers.get(profile["name"], 0) + 1
            return reply
        raise LlmUnavailable("no active LLM profile answered")

    def _counted(self, chat: Callable) -> Callable:
        def call(*args):
            self._calls += 1
            return chat(*args)
        return call

    def run(self, status: dict, progress: Callable[[], None] = lambda: None) -> None:
        """At most PER_CYCLE calls (a template can need 2: confirm, then suggest); progress() after each
        template, so a slow model does not leave the linker's heartbeat stale."""
        lc = self.cfg.llm
        now = self.now()
        self._failed = {k: t for k, t in self._failed.items() if t > now}
        self._spent = [(svc, t) for svc, t in self._spent if t > now - 86400]
        self._down = {name: v for name, v in self._down.items() if v[0] > now}
        with self.conn.transaction():
            self.profiles = active_profiles(self.conn)
            if not self.profiles:
                status["warnings"].append("no active LLM profile: templates are not sent to the LLM")
                return
            # results in the last day / hour (rejected rows came from the LLM too), plus calls that failed
            used = self.conn.execute(
                "SELECT service, count(*) FILTER (WHERE linked_at > now() - interval '1 hour') AS hour, "
                "count(*) AS day FROM template_doc_links WHERE source IN ('llm', 'draft', 'rejected') "
                "AND linked_at > now() - interval '1 day' GROUP BY service").fetchall()
            per_service = {r["service"]: r["hour"] for r in used}
            day = sum(r["day"] for r in used) + len(self._spent)
            for svc, t in self._spent:
                if t > now - 3600:
                    per_service[svc] = per_service.get(svc, 0) + 1
            capped = [svc for svc, n in per_service.items() if n >= lc.per_service_hour]
            # capped services and backed-off templates are left out here, so they never fill the LIMIT
            todo = self.conn.execute(f"""
                SELECT t.service, t.template_id, t.level, t.text FROM templates t
                LEFT JOIN alert_state a ON a.service = t.service AND a.key = t.template_id
                WHERE t.embedding IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM template_doc_links l
                                  WHERE l.service = t.service AND l.template_id = t.template_id)
                  AND NOT t.service = ANY(%s)
                  AND (t.service, t.template_id) NOT IN (SELECT * FROM unnest(%s::text[], %s::text[]))
                ORDER BY (a.state = 'ALERTING') IS TRUE DESC,
                         CASE {class_sql('t.level')} WHEN 'ERROR' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
                         t.last_seen DESC
                LIMIT 50""", (capped, [k[0] for k in self._failed],
                              [k[1] for k in self._failed])).fetchall()
        done = {"confirmed": 0, "suggested": 0, "none": 0, "failed": 0}
        self._calls = 0
        for t in todo:
            key = (t["service"], t["template_id"])
            if self._calls >= PER_CYCLE or day >= lc.daily_max:
                break
            if per_service.get(t["service"], 0) >= lc.per_service_hour:
                continue
            try:
                outcome = self._resolve(t)
            except LlmUnavailable as exc:   # every profile is down: not this template's fault, stop here
                status["warnings"].append(f"LLM unavailable: {exc}")
                break
            except (LlmError, psycopg.Error, ValueError) as exc:   # a bad answer must not stop the linker
                self._failed[key] = self.now() + FAILURE_BACKOFF_SECONDS
                outcome = "failed"
                self.last_error = status["last_error"] = f"llm: {exc}"
                logger.warning("LLM resolution failed for %s %s: %s", *key, exc)
            done[outcome] += 1
            if outcome in ("failed", "none"):   # a call that wrote nothing still costs budget
                self._spent.append((t["service"], self.now()))
            day += 1
            per_service[t["service"]] = per_service.get(t["service"], 0) + 1
            status["llm"] = done
            progress()
        status["llm"] = done
        status["llm_today"] = day
        status["llm_profiles"] = {p["name"]: {"down_until": self._down.get(p["name"], (None, None))[0],
                                              "error": self._down.get(p["name"], (None, None))[1],
                                              "answers": self.answers.get(p["name"], 0)}
                                  for p in self.profiles}
        status["llm_last_error"] = self.last_error
        if day >= lc.daily_max:
            status["warnings"].append(f"LLM daily limit reached ({lc.daily_max})")

    def _resolve(self, t: dict) -> str:
        lc = self.cfg.llm
        with self.conn.transaction():
            docs = candidates(self.conn, t["service"], t["template_id"], lc.candidates, lc.candidate_similarity)
        if docs:
            pick, reason = self.confirm(t, docs)
            if pick:
                return "confirmed" if self._write_llm_link(t, pick, reason) else "none"
        # the same error documented by other services: the LLM adapts them instead of starting blank
        with self.conn.transaction():
            refs = candidates(self.conn, t["service"], t["template_id"], lc.candidates, lc.candidate_similarity,
                              other_services=True)
        question = {"template": _template(t), "task": SUGGEST_TASK}
        if refs:
            question["references"] = self._listed(t, refs)
        reply = self.ask(SYSTEM, json.dumps(question, ensure_ascii=False))
        return "suggested" if self._write_draft(t, reply, [r["doc_id"] for r in refs]) else "none"

    def confirm(self, t: dict, docs: list[dict]) -> tuple[dict | None, str]:
        """Ask which candidate fits; (the candidate or None, reason). Writes nothing (link_eval uses it)."""
        listed = self._listed(t, docs)
        reply = self.ask(SYSTEM, json.dumps({"template": _template(t), "documents": listed,
                                             "task": CONFIRM_TASK}, ensure_ascii=False))
        pick = next((d for d in docs if d["doc_id"] == reply.get("doc_id")), None)   # only a listed one
        return pick, _text(reply.get("reason"), 500)

    def _listed(self, t: dict, docs: list[dict]) -> list[dict]:
        budget = max(200, (self.cfg.llm.max_input_chars - len(t["text"]) - 1000) // len(docs))
        return [{"doc_id": d["doc_id"], "service": d["service"] or "shared", "level": d["level"] or "any",
                 "title": d["title"],
                 "error": d["error"][:budget // 2], "handling": d["handling"][:budget // 2]} for d in docs]

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

    def _write_draft(self, t: dict, reply: dict, based_on: list[str] = ()) -> bool:
        handling = _text(reply.get("handling"), 20000)
        title = _text(reply.get("title"), 200)
        if not handling or not title:
            raise LlmError("suggestion without title or handling")
        verdict = reply.get("verdict") if reply.get("verdict") in ("action", "safe") else "action"
        try:
            confidence = min(max(float(reply.get("confidence")), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None and not math.isfinite(confidence):   # json accepts NaN; JSON output does not
            confidence = None
        written = False
        with self.conn.transaction():
            doc_id = next_doc_id(self.conn)
            self.conn.execute(
                "INSERT INTO documents (doc_id, service, level, title, error_code, error, handling, status, source, "
                "verdict, confidence, based_on, revision) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft', 'llm', %s, %s, %s, %s)",
                (doc_id, t["service"], level_class(t["level"]), title, _text(reply.get("error_code"), 100),
                 _text(reply.get("error"), 5000) or t["text"], handling, verdict, confidence, list(based_on),
                 uuid.uuid4().hex))
            written = bool(self.conn.execute(
                "INSERT INTO template_doc_links (service, template_id, doc_id, source) "
                "VALUES (%s, %s, %s, 'draft') ON CONFLICT DO NOTHING",
                (t["service"], t["template_id"], doc_id)).rowcount)
            if not written:   # linked meanwhile: drop the draft document too
                raise psycopg.Rollback()
        return written
