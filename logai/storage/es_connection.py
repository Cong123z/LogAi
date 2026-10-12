"""Elasticsearch endpoint and credentials set on the web "Data sources" page.

The web is the only writer; the engine and training read it and fall back to
the env/YAML `elasticsearch` config while the file does not exist. The file
holds a password, so it is written with mode 0600 and the password never
leaves this module through `public_view()`. As with LLM profiles, a stored
password is never kept when the hosts move to another server.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from logai.config import ElasticsearchConfig
from logai.storage.base import atomic_write_json

SCHEMA_VERSION = 1
MAX_HOSTS, HOST_LIMIT, USER_LIMIT, PASSWORD_LIMIT = 10, 500, 200, 500

_write_lock = threading.Lock()


class ConnectionError_(ValueError):
    """Invalid connection data or a forbidden change."""


class ConnectionConflict(ConnectionError_):
    def __init__(self, current_revision: Optional[str]):
        super().__init__("The connection settings changed; reload and try again")
        self.current_revision = current_revision


def _origin(url: str) -> str:
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 9200)
    return f"{parsed.scheme}://{(parsed.hostname or '').lower()}:{port}"


def display_host(url: str) -> str:
    """The URL without any user:password@ (an env host may carry one)."""
    parsed = urlparse(str(url).strip())
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return parsed._replace(netloc=netloc).geturl()


def hosts_fingerprint(hosts: Iterable[str]) -> str:
    """Which cluster the hosts point at, independent of order and paths."""
    return ",".join(sorted({_origin(str(h).strip()) for h in hosts if str(h).strip()}))


def validate_hosts(raw: Any) -> List[str]:
    """A list (or comma separated string) of http(s) URLs without credentials."""
    values = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(values, list):
        raise ConnectionError_("hosts must be a list of URLs")
    hosts: List[str] = []
    for value in values:
        url = value.strip().rstrip("/") if isinstance(value, str) else ""
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or len(url) > HOST_LIMIT:
            raise ConnectionError_(f"Invalid endpoint {value!r}: use an http(s) URL")
        if parsed.username or parsed.password:
            # Credentials belong in the password field, which is never shown back.
            raise ConnectionError_("Endpoint must not contain a username or password")
        try:
            parsed.port
        except ValueError as exc:
            raise ConnectionError_(f"Endpoint {value!r} has an invalid port") from exc
        if url not in hosts:
            hosts.append(url)
    if not hosts:
        raise ConnectionError_("At least one endpoint is required")
    if len(hosts) > MAX_HOSTS:
        raise ConnectionError_(f"At most {MAX_HOSTS} endpoints")
    return hosts


def _text(value: Any, field: str, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    if field != "password":
        text = text.strip()
    if len(text) > limit:
        raise ConnectionError_(f"{field} must be at most {limit} characters")
    # Control characters end up in the Authorization header and make the
    # client raise an error that quotes it (and so the password) into logs.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise ConnectionError_(f"{field} must not contain control characters")
    return text


def _revision(conn: Dict[str, Any]) -> str:
    secret = hashlib.sha256((conn.get("password") or "").encode("utf-8")).hexdigest()
    encoded = json.dumps(
        [conn["hosts"], conn.get("username") or "", bool(conn.get("verify_certs", True)), secret],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def apply(config: ElasticsearchConfig, conn: Optional[Dict[str, Any]]) -> ElasticsearchConfig:
    """The env/YAML config with the web's connection on top (when saved)."""
    if not conn:
        return config
    return dataclasses.replace(
        config,
        hosts=list(conn["hosts"]),
        username=conn.get("username") or None,
        password=conn.get("password") or None,
        verify_certs=bool(conn.get("verify_certs", True)),
    )


class ESConnectionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def mtime_ns(self) -> Optional[int]:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def load(self) -> Optional[Dict[str, Any]]:
        """{hosts, username, password, verify_certs, updated_at, revision}
        (with the password), or None while the web has never saved one or the
        file is unreadable (the env config is used then)."""
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                raw = json.load(stream)
            conn = {
                "hosts": validate_hosts(raw.get("hosts")),
                "username": raw.get("username") if isinstance(raw.get("username"), str) else "",
                "password": raw.get("password") if isinstance(raw.get("password"), str) else "",
                "verify_certs": raw.get("verify_certs") is not False,
                "updated_at": raw.get("updated_at"),
            }
        except (OSError, ValueError, AttributeError):
            return None
        conn["revision"] = _revision(conn)
        return conn

    @staticmethod
    def public(conn: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if conn is None:
            return None
        return {
            "hosts": conn["hosts"], "username": conn.get("username") or "",
            "verify_certs": conn.get("verify_certs", True),
            "password_set": bool(conn.get("password")),
            "updated_at": conn.get("updated_at"), "revision": conn["revision"],
        }

    def public_view(self) -> Optional[Dict[str, Any]]:
        return self.public(self.load())

    def save(
        self,
        *,
        hosts: Any,
        username: Any,
        password: Any,
        verify_certs: Any,
        expected_revision: Optional[str],
    ) -> Dict[str, Any]:
        """Save the connection. An empty password keeps the stored one, except
        when the hosts move to another server."""
        cleaned = {
            "hosts": validate_hosts(hosts),
            "username": _text(username, "username", USER_LIMIT),
            "password": _text(password, "password", PASSWORD_LIMIT),
            "verify_certs": verify_certs is not False,
        }
        with _write_lock:
            current = self.load()
            current_revision = current["revision"] if current else None
            if expected_revision != current_revision:
                raise ConnectionConflict(current_revision)
            if not cleaned["password"] and current and current.get("password"):
                # Handing the stored password to another server would leak it.
                if hosts_fingerprint(cleaned["hosts"]) != hosts_fingerprint(current["hosts"]):
                    raise ConnectionError_("Re-enter the password when changing the endpoint")
                cleaned["password"] = current["password"]
            cleaned["updated_at"] = time.time()
            # mode 0o600: the file holds a password.
            atomic_write_json(
                self.path, {"schema_version": SCHEMA_VERSION, **cleaned},
                indent=2, ensure_ascii=False, mode=0o600,
            )
        cleaned["revision"] = _revision(cleaned)
        return cleaned

    def clear(self, expected_revision: Optional[str]) -> None:
        """Back to the env config."""
        with _write_lock:
            current = self.load()
            current_revision = current["revision"] if current else None
            if expected_revision != current_revision:
                raise ConnectionConflict(current_revision)
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
