import time

import pytest

from logai2.llm import LlmError, chat_json, parse_json_reply
from tests.fake_http import FakeHttp


def reply(content):
    return 200, {"choices": [{"message": {"role": "assistant", "content": content}}]}


@pytest.fixture
def server():
    servers = []

    def make(handler):
        servers.append(FakeHttp(handler))
        return servers[-1]
    yield make
    for s in servers:
        s.close()


def profile(url, **kw):
    return {"base_url": url + "/v1/", "model": "m", "api_key": "sk-secret", "timeout_seconds": 5, **kw}


def test_round_trip_sends_json_mode_and_the_key(server):
    s = server(lambda p, b: reply('{"doc_id": "D1", "reason": "same error"}'))
    assert chat_json(profile(s.url), "sys", "user") == {"doc_id": "D1", "reason": "same error"}
    path, body = s.calls[0]
    assert path == "/v1/chat/completions" and body["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in body["messages"]] == ["system", "user"] and body["model"] == "m"


def test_think_blocks_and_prose_are_stripped():
    assert parse_json_reply('<think>{"no": 1}</think>Sure! {"doc_id": null, "reason": "x"} hope it helps') == {
        "doc_id": None, "reason": "x"}
    for bad in ("no json here", "{broken", "[1, 2]"):
        pytest.raises(LlmError, parse_json_reply, bad)


def test_errors_raise_without_the_key_and_a_timeout_is_retried_once(server):
    err = pytest.raises(LlmError, chat_json, profile(server(lambda p, b: (500, {"error": "boom"})).url), "s", "u")
    assert "sk-secret" not in str(err.value)
    calls = []

    def slow_then_ok(p, b):
        calls.append(1)
        if len(calls) == 1:
            time.sleep(1.5)
        return reply('{"ok": true}')
    assert chat_json(profile(server(slow_then_ok).url, timeout_seconds=1), "s", "u") == {"ok": True}
    assert len(calls) == 2
