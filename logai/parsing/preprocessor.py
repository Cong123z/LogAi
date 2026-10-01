"""Message normalisation applied before Drain3.

Drain3 partitions messages by token count and then by leading tokens, so any
token that varies per event (dates, thread names, ids, numbers, payload
values) either splits one logical message into many templates or drags noise
into the template text that the embedder later reads. This module keeps only
the *wording* of a log line:

* the leading ``<date> <time> <LEVEL> [<thread>]`` prefix and every timestamp
  are removed outright - ``@timestamp`` and ``RawLog.level`` already carry
  them;
* XML/SOAP payloads are reduced to their element names and text;
* identifiers (UUID, hex, secrets/long ids, IP, URL host, e-mail) become ``<*>`` and
  every digit run inside a word becomes ``<*>`` while its letters survive
  (``con-1-Sender`` -> ``con-<*>-Sender``);
* punctuation separators are dropped and repeated placeholders collapsed so
  the token count stays stable.

The same function runs in training and realtime (both go through
``Drain3Parser``), which keeps template ids consistent between the two.
"""
from __future__ import annotations

import html
import re
from typing import Optional, Tuple

PLACEHOLDER = "<*>"

LEVELS = (
    "TRACE", "DEBUG", "INFO", "NOTICE", "WARN", "WARNING",
    "ERROR", "SEVERE", "FATAL", "CRITICAL",
)

_TOKEN_RE = re.compile(r"\S+")
_HAS_DIGIT = re.compile(r"\d").search
_HAS_ALNUM = re.compile(r"[^\W_]").search
_LEVEL_TOKEN_RE = re.compile(
    r"[\[<(]?(" + "|".join(LEVELS) + r")[\]>)]?:?",
    re.IGNORECASE,
)
# What may follow the level inside the prefix: one "[thread]" and a
# separator such as "-", ":" or "|".
_AFTER_LEVEL_RE = re.compile(r"\s*(?:\[[^\]\n]*\]\s*)?(?:[-:|]+(?:\s+|$))?")


def _is_prefix_token(token: str) -> bool:
    """Tokens allowed *before* the level: dates, times, pids, ``[thread]``
    or pure punctuation - never a plain word, so ``Connection ERROR ...``
    is not mistaken for a prefix."""
    return bool(token[0] in "[(" or _HAS_DIGIT(token) or not _HAS_ALNUM(token))


def find_level(message: str, search_tokens: int = 5) -> Optional[Tuple[str, int]]:
    """Locate the log level among the first ``search_tokens`` tokens.

    Returns ``(LEVEL, prefix_end)`` where ``prefix_end`` is the offset just
    after the level plus an optional following ``[thread]`` and separator,
    or ``None`` when no level is found.
    """
    if not message:
        return None
    for index, match in enumerate(_TOKEN_RE.finditer(message)):
        if index >= search_tokens:
            return None
        token = match.group()
        level_match = _LEVEL_TOKEN_RE.fullmatch(token)
        if level_match:
            after = _AFTER_LEVEL_RE.match(message, match.end())
            return level_match.group(1).upper(), after.end()
        if not _is_prefix_token(token):
            return None
    return None


# -- timestamps (removed, not masked) --------------------------------------
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
_DAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*"
_CLOCK = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,9})?"
_ZONE = r"(?:\s?(?:Z|[A-Z]{2,5}(?:[+-]\d{2}:?\d{2})?|[+-]\d{2}:?\d{2}))?"
_TIMESTAMP_RE = re.compile(
    r"(?<![\w.:/-])(?:"
    # Java Date.toString(): Thu Aug 13 18:52:11 GMT+07:00 2020
    rf"{_DAY},?\s+{_MONTH}\s+\d{{1,2}}\s+{_CLOCK}{_ZONE}\s+\d{{4}}"
    # ISO / SQL: 2026-09-30T07:00:32.036Z, 2026-09-30 07:00:32
    rf"|\d{{4}}-\d{{2}}-\d{{2}}(?:[T\s]{_CLOCK}{_ZONE})?"
    # 27/07/2026 00:00:00.024, 2026/07/27 00:00
    rf"|(?:\d{{1,2}}/\d{{1,2}}/\d{{4}}|\d{{4}}/\d{{2}}/\d{{2}})(?:[\sT]{_CLOCK}{_ZONE})?"
    # 13 Aug 2020 18:52:11, 13-Aug-2020
    rf"|\d{{1,2}}[\s-]{_MONTH}[\s-]\d{{4}}(?:\s{_CLOCK}{_ZONE})?"
    # bare clock with seconds: 18:52:11, 00:00:00.024
    r"|\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,9})?"
    r")(?![\w:])"
)

# -- XML / SOAP ------------------------------------------------------------
_XML_HINT_RE = re.compile(r"<[A-Za-z_/!?]")
_XML_NOISE_RE = re.compile(r"<\?.*?\?>|<!--.*?-->", re.DOTALL)
_XML_TAG_RE = re.compile(r"<(/?)(?:[\w.-]+:)?([\w.-]+)[^<>]*>")
_XML_WRAPPERS = {"envelope", "body", "header"}


def _xml_tag(match: "re.Match[str]") -> str:
    closing, name = match.group(1), match.group(2)
    if closing or name.lower() in _XML_WRAPPERS:
        return " "
    return f" {name} "


# "key=" with no value becomes "key=<*>" so it keeps the token count of the
# same key carrying a value (msisdnOther=, vs msisdnOther=843...).
_EMPTY_VALUE_RE = re.compile(r"=(?=\s*(?:[,;}\])&]|$))", re.MULTILINE)

# -- tokens that carry no wording -------------------------------------------
_SEPARATORS_RE = re.compile(r"[=,;{}()\[\]\"']+")
# Each rule only runs when its cheap marker is present in the message.
_URL_HOST_RE = re.compile(r"\b[a-zA-Z][\w+.-]*://[^\s/?#]+")  # scheme + host[:port]; path kept
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?![\w.])")
# Candidate runs are matched cheaply and the callbacks decide, which is much
# faster than lookaheads evaluated at every position.
_HEX_RUN_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{8,}\b")
# Long id-like runs: secrets, tokens, base64, UUIDs, transaction ids.
_LONG_RUN_RE = re.compile(r"(?<![\w+/])[\w+/-]{16,}=*")
_HEX_LETTER = re.compile(r"[a-fA-F]").search
_HAS_ALPHA = re.compile(r"[A-Za-z]").search
_DIGIT_CHARS = re.compile(r"\d").findall
_DIGITS_RE = re.compile(r"\d+")


def _mask_hex(match: "re.Match[str]") -> str:
    token = match.group()
    return PLACEHOLDER if _HAS_DIGIT(token) and _HEX_LETTER(token) else token


def _mask_long_id(match: "re.Match[str]") -> str:
    token = match.group()
    if _HAS_ALPHA(token) and len(_DIGIT_CHARS(token)) >= 3:
        return PLACEHOLDER
    return token


def _collapse(tokens: list[str]) -> list[str]:
    """A token made only of placeholders and punctuation becomes one ``<*>``;
    runs of ``<*>`` shrink to one so variable-length lists keep one token."""
    out: list[str] = []
    for token in tokens:
        if PLACEHOLDER in token and not _HAS_ALNUM(token.replace(PLACEHOLDER, "")):
            token = PLACEHOLDER
        if token == PLACEHOLDER and out and out[-1] == PLACEHOLDER:
            continue
        out.append(token)
    return out


def normalize(
    message: str,
    max_chars: int = 8192,
    max_tokens: int = 40,
    level_search_tokens: int = 5,
) -> str:
    text = (message or "")[:max_chars]

    found = find_level(text, level_search_tokens)
    if found is not None:
        text = text[found[1]:]

    if "&" in text and ";" in text:
        # HTML-escaped payloads (&lt;soap:Envelope ...) are reduced like XML.
        text = html.unescape(text)
    if "=" in text:
        text = _EMPTY_VALUE_RE.sub("=" + PLACEHOLDER, text)

    if "<" in text and _XML_HINT_RE.search(text):
        text = _XML_NOISE_RE.sub(" ", text)
        text = _XML_TAG_RE.sub(_xml_tag, text)

    if _HAS_DIGIT(text):
        text = _TIMESTAMP_RE.sub(" ", text)
    text = _SEPARATORS_RE.sub(" ", text)
    if "://" in text:
        text = _URL_HOST_RE.sub(PLACEHOLDER, text)
    if "@" in text:
        text = _EMAIL_RE.sub(PLACEHOLDER, text)
    if _HAS_DIGIT(text):
        if "." in text:
            text = _IPV4_RE.sub(PLACEHOLDER, text)
        text = _HEX_RUN_RE.sub(_mask_hex, text)
        text = _LONG_RUN_RE.sub(_mask_long_id, text)
        text = _DIGITS_RE.sub(PLACEHOLDER, text)

    tokens = _collapse(text.split())
    return " ".join(tokens[:max_tokens])
