"""LLM chat over an OpenAI-compatible endpoint, with the active llm_profiles row (as in logai-engine).

Lessons from logai-engine's runs: deepseek answered `<think>…` plus prose when JSON mode was not sent,
so JSON mode is always sent and think blocks are stripped; the proxy timed out at 60 s, so the
profile timeout defaults to 120 s with one retry on a timeout only; calls are made one at a time.
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request

_THINK = re.compile(r"<think>.*?</think>", re.S)


class LlmError(RuntimeError):
    pass


def active_profile(conn) -> dict | None:
    return conn.execute("SELECT name, base_url, model, api_key, timeout_seconds FROM llm_profiles "
                        "WHERE active").fetchone()


def parse_json_reply(content: str) -> dict:
    """The first JSON object in a reply, after dropping <think> blocks and any prose around it."""
    text = _THINK.sub("", content or "")
    start = text.find("{")
    if start < 0:
        raise LlmError("reply has no JSON object")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise LlmError(f"invalid JSON in reply: {exc}") from exc
    if not isinstance(value, dict):
        raise LlmError("reply is not a JSON object")
    return value


def _timed_out(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, socket.timeout)) or (
        isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout)))


def chat_json(profile: dict, system: str, user: str) -> dict:
    """One chat call answered with a JSON object. Raises LlmError."""
    body = json.dumps({"model": profile["model"], "temperature": 0,
                       "response_format": {"type": "json_object"},
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    headers = {"Content-Type": "application/json"}
    if profile.get("api_key"):
        headers["Authorization"] = f"Bearer {profile['api_key']}"
    url = profile["base_url"].rstrip("/") + "/chat/completions"
    for attempt in (1, 2):
        request = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=profile.get("timeout_seconds") or 120) as response:
                content = json.load(response)["choices"][0]["message"]["content"]
            return parse_json_reply(content)
        except LlmError:
            raise
        except Exception as exc:  # noqa: BLE001 - network, HTTP and shape errors alike
            if attempt == 1 and _timed_out(exc):
                continue
            # never the request: it carries the API key
            raise LlmError(f"{type(exc).__name__}: {exc}") from None
    raise AssertionError("unreachable")
