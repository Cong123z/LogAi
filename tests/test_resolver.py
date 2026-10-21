import json

import pytest

from logai2.llm import LlmError, LlmUnavailable
from logai2.resolver import Resolver
from tests.test_links import at, doc, tmpl, vec


class Chat:
    """Answers in order; records what the LLM was shown."""
    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen = []

    def __call__(self, profile, system, user):
        self.seen.append(json.loads(user))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def alerting(conn, tid, service="s"):
    conn.execute("INSERT INTO alert_state VALUES (%s, %s, 'ALERTING', now(), 3, 2.0, 9, 0, 3)", (service, tid))


@pytest.fixture
def profile(conn):
    conn.execute("INSERT INTO llm_profiles (name, base_url, model, api_key, active, revision) "
                 "VALUES ('p', 'http://x', 'm', 'k', true, 'r')")


def run(conn, cfg, chat):
    conn.commit()
    status = {"warnings": [], "last_error": None}
    Resolver(cfg, conn, chat=chat).run(status)
    conn.commit()
    return status


def row(conn, tid):
    return conn.execute("SELECT l.doc_id, l.source, l.note, d.status, d.verdict, d.source AS doc_source, d.error "
                        "FROM template_doc_links l JOIN documents d USING (doc_id) WHERE template_id = %s",
                        (tid,)).fetchone()


def test_no_active_profile_means_no_call(conn, cfg):
    tmpl(conn, "A", v=vec((0, 1.0))), alerting(conn, "A")
    chat = Chat()
    assert run(conn, cfg, chat)["warnings"] == ["no active LLM profile: templates are not sent to the LLM"]
    assert chat.seen == []


def test_confirm_a_listed_candidate_or_else_suggest_a_draft(conn, cfg, profile):
    doc(conn, "DOC-001", vec((0, 1.0)))
    tmpl(conn, "A", v=at(0.7, 5)), alerting(conn, "A")             # candidate DOC-001 at 0.7 (< corpus 0.85)
    tmpl(conn, "B", "INFO", v=at(0.65, 6)), alerting(conn, "B")    # candidate too, but the LLM says no
    chat = Chat({"doc_id": "DOC-001", "reason": "same timeout"},
                {"doc_id": "DOC-999", "reason": "not listed"},     # not a candidate: treated as none
                {"verdict": "safe", "title": "Send OK", "error_code": "SEND_OK", "error": "send ok <*>",
                 "handling": "SAFE: normal operation, no action needed.", "confidence": 3})
    status = run(conn, cfg, chat)
    assert status["llm"] == {"confirmed": 1, "suggested": 1, "none": 0, "failed": 0}
    a, b = row(conn, "A"), row(conn, "B")
    assert (a["doc_id"], a["source"], a["note"]) == ("DOC-001", "llm", "same timeout")
    assert (b["doc_id"], b["source"], b["status"], b["verdict"], b["doc_source"]) == (
        "DOC-002", "draft", "draft", "safe", "llm")
    assert conn.execute("SELECT confidence FROM documents WHERE doc_id = 'DOC-002'").fetchone()["confidence"] == 1.0
    assert [r["kind"] for r in conn.execute("SELECT kind FROM commands")] == ["reload_links"]   # the llm link only
    shown = chat.seen[0]
    assert shown["template"] == {"service": "s", "level": "ERROR", "text": "text A"}
    assert [d["doc_id"] for d in shown["documents"]] == ["DOC-001"] and "task" in shown
    assert "documents" not in chat.seen[2]                         # the suggestion sees the template only


def test_eligibility_order_and_limits(conn, cfg, profile):
    cfg.llm.per_service_hour = 1
    tmpl(conn, "OLD", v=vec((1, 1.0)))
    conn.execute("UPDATE templates SET first_seen = now() - interval '3 days' WHERE template_id = 'OLD'")  # not alerting
    tmpl(conn, "LINKED", v=vec((2, 1.0)), service="z"), alerting(conn, "LINKED", "z")
    doc(conn, "D1")
    conn.execute("INSERT INTO template_doc_links (service, template_id, doc_id, source) "
                 "VALUES ('z', 'LINKED', 'D1', 'rejected')")   # never resent (and counts in z's budget)
    tmpl(conn, "NOVEC"), alerting(conn, "NOVEC")                     # waits for its vector
    tmpl(conn, "NEW_INFO", "INFO", v=vec((3, 1.0)))                  # new (first seen now), not alerting
    tmpl(conn, "ALERT_INFO", "INFO", v=vec((4, 1.0))), alerting(conn, "ALERT_INFO")
    tmpl(conn, "O1", v=vec((5, 1.0)), service="o"), alerting(conn, "O1", "o")
    suggestion = {"title": "t", "handling": "h"}
    chat = Chat(dict(suggestion), dict(suggestion))
    run(conn, cfg, chat)
    # alerting first; one per service per hour: s gets ALERT_INFO, o gets O1; NEW_INFO waits
    assert {r["template_id"] for r in conn.execute("SELECT template_id FROM template_doc_links WHERE source = 'draft'")} \
        == {"ALERT_INFO", "O1"}


def test_failures_back_off_and_bad_suggestions_fail(conn, cfg, profile):
    tmpl(conn, "A", v=vec((0, 1.0))), alerting(conn, "A")
    resolver_chat = Chat(LlmError("timeout"))
    status = run(conn, cfg, resolver_chat)
    assert status["llm"]["failed"] == 1 and status["last_error"] == "llm: timeout"
    chat = Chat({"title": "", "handling": "h"})
    status = run(conn, cfg, chat)
    assert status["llm"]["failed"] == 1 and "without title" in status["last_error"]
    assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 0


def test_bad_answers_are_stored_cleanly_or_fail_without_stopping(conn, cfg, profile):
    tmpl(conn, "A", v=vec((0, 1.0))), alerting(conn, "A")
    tmpl(conn, "B", v=vec((1, 1.0))), alerting(conn, "B")
    chat = Chat({"title": "t\x00x", "handling": "h\ud800", "confidence": float("nan")},
                {"title": "ok", "handling": "h", "error_code": 7})
    status = run(conn, cfg, chat)
    assert status["llm"]["suggested"] == 2
    docs = {r["title"]: r for r in conn.execute("SELECT title, handling, confidence, error_code FROM documents")}
    assert docs["tx"]["handling"] == "h" and docs["tx"]["confidence"] is None and docs["ok"]["error_code"] == ""


def test_failed_calls_count_against_the_budget_and_capped_services_do_not_block(conn, cfg, profile):
    cfg.llm.per_service_hour = 2
    for n in range(4):
        tmpl(conn, f"S{n}", v=vec((n, 1.0))), alerting(conn, f"S{n}")
    tmpl(conn, "O", "INFO", v=vec((9, 1.0)), service="o"), alerting(conn, "O", "o")   # ordered last
    chat = Chat(LlmError("junk"), LlmError("junk"), {"title": "t", "handling": "h"})
    status = run(conn, cfg, chat)
    # two failures use up s's hour; the third call goes to service o instead of a third s template
    assert status["llm"] == {"confirmed": 0, "suggested": 1, "none": 0, "failed": 2}
    assert [r["template_id"] for r in conn.execute("SELECT template_id FROM template_doc_links")] == ["O"]


def test_progress_is_reported_after_each_template(conn, cfg, profile):
    tmpl(conn, "A", v=vec((0, 1.0))), alerting(conn, "A")
    conn.commit()
    beats = []
    Resolver(cfg, conn, chat=Chat({"title": "t", "handling": "h"})).run(
        {"warnings": [], "last_error": None}, progress=lambda: beats.append(1))
    assert beats == [1]


def test_a_dead_profile_fails_over_to_the_next_and_is_skipped_for_a_while(conn, cfg, profile):
    conn.execute("INSERT INTO llm_profiles (name, base_url, model, active, priority, revision) "
                 "VALUES ('first', 'http://dead', 'm', true, 1, 'r'), ('off', 'http://x', 'm', false, 0, 'r')")
    for n in range(3):
        tmpl(conn, f"T{n}", v=vec((n, 1.0))), alerting(conn, f"T{n}")
    asked = []

    def chat(profile, system, user):
        asked.append(profile["name"])
        if profile["name"] == "first":
            raise LlmUnavailable("HTTPError: HTTP Error 502")
        return {"title": "t", "handling": "h"}
    status = {"warnings": [], "last_error": None}
    conn.commit()
    resolver = Resolver(cfg, conn, chat=chat)
    resolver.run(status)
    # the dead profile is tried once (that call counts in the 3 per cycle), then skipped; "off" never
    assert status["llm"]["suggested"] == 2 and status["llm"]["failed"] == 0
    assert asked == ["first", "p", "p"]
    assert status["llm_profiles"]["first"]["error"] == "HTTPError: HTTP Error 502"
    assert status["llm_profiles"]["p"] == {"down_until": None, "error": None}
    assert status["llm_last_error"] == "first: HTTPError: HTTP Error 502"


def test_all_profiles_down_stops_the_cycle_without_blaming_templates(conn, cfg, profile):
    tmpl(conn, "A", v=vec((0, 1.0))), alerting(conn, "A")
    conn.commit()
    resolver = Resolver(cfg, conn, chat=Chat(LlmUnavailable("timeout")))
    status = {"warnings": [], "last_error": None}
    resolver.run(status)
    assert status["warnings"] == ["LLM unavailable: no active LLM profile answered"]
    assert resolver._failed == {} and resolver._spent == []
