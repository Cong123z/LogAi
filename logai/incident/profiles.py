"""LLM profiles: named endpoint / api_key / model sets managed from the web.

The web is the only writer; the engine reads the active profile every loop
and switches without a restart. API keys never leave this module through
`public_view()`; the browser only ever sees a short hint. The file holds
secrets, so it is written with mode 0600.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from logai.storage.base import atomic_write_json

SCHEMA_VERSION = 1
NAME_LIMIT, MODEL_LIMIT, KEY_LIMIT, ENDPOINT_LIMIT = 100, 200, 500, 500
CHAT_PATH = "/chat/completions"

# One web process writes this file from several request threads.
_write_lock = threading.Lock()


class ProfileError(ValueError):
    """Invalid profile data or a forbidden operation."""


class ProfileStoreUnreadable(ProfileError):
    """The profile file exists but cannot be read or parsed."""


def normalize_endpoint(value: Any) -> str:
    """Accept a base URL and turn it into the OpenAI-compatible chat URL:
    https://host -> https://host/v1/chat/completions, .../v1 -> .../v1/chat/completions.
    Any other explicit path is kept as typed."""
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or len(url) > ENDPOINT_LIMIT:
        raise ProfileError("endpoint must be an http(s) URL")
    if parsed.username or parsed.password:
        # Credentials belong in the API key field, which is never shown back.
        raise ProfileError("endpoint must not contain a username or password")
    try:
        parsed.port
    except ValueError as exc:
        raise ProfileError("endpoint has an invalid port") from exc
    path = parsed.path.rstrip("/")
    if path in {"", "/v1"}:
        path = "/v1" + CHAT_PATH
    return parsed._replace(path=path).geturl()


def key_hint(api_key: Optional[str]) -> str:
    if not api_key:
        return ""
    if len(api_key) < 8:
        return "•••"
    return f"{api_key[:3]}…{api_key[-3:]}"


def _text(value: Any, field: str, limit: int, required: bool = True) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if required and not text:
        raise ProfileError(f"{field} is required")
    if len(text) > limit:
        raise ProfileError(f"{field} must be at most {limit} characters")
    return text


def _api_key(value: Any) -> str:
    key = _text(value, "api_key", KEY_LIMIT, required=False)
    # A key with whitespace/control characters makes requests raise an error
    # that quotes the whole header (and so the key) into logs and records.
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in key):
        raise ProfileError("api_key must not contain spaces or control characters")
    return key


def _origin(endpoint: str) -> tuple[str, str]:
    parsed = urlparse(endpoint)
    return parsed.scheme, parsed.netloc.lower()


class LLMProfileStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    # -- reading -------------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        """A missing file is empty; an unreadable or corrupt one raises, so a
        later write can never silently replace every stored profile."""
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                raw = json.load(stream)
        except FileNotFoundError:
            raw = {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileStoreUnreadable(f"LLM profile file is unreadable: {exc}") from exc
        if not isinstance(raw, dict):
            raise ProfileStoreUnreadable("LLM profile file is unreadable: not a JSON object")
        profiles = raw.get("profiles") if isinstance(raw, dict) else None
        profiles = {
            pid: p for pid, p in (profiles or {}).items()
            if isinstance(p, dict) and isinstance(pid, str)
        } if isinstance(profiles, dict) else {}
        active = raw.get("active_profile_id") if isinstance(raw, dict) else None
        return {
            "active_profile_id": active if active in profiles else None,
            "profiles": profiles,
        }

    def get(self, profile_id: str) -> Dict[str, Any]:
        profile = self._load()["profiles"].get(profile_id)
        if profile is None:
            raise KeyError(profile_id)
        return dict(profile)

    def active_profile(self) -> Optional[Dict[str, Any]]:
        """The full active profile (with its key) for the engine, or None."""
        data = self._load()
        active = data["active_profile_id"]
        return dict(data["profiles"][active]) if active else None

    @staticmethod
    def _public(profile: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": profile["id"], "name": profile.get("name", ""),
            "endpoint": profile.get("endpoint", ""), "model": profile.get("model", ""),
            "api_key_hint": key_hint(profile.get("api_key")),
            "created_at": profile.get("created_at"), "updated_at": profile.get("updated_at"),
        }

    def public_view(self) -> Dict[str, Any]:
        data = self._load()
        profiles = sorted(data["profiles"].values(), key=lambda p: (p.get("created_at") or 0, p["id"]))
        return {
            "active_profile_id": data["active_profile_id"],
            "profiles": [self._public(p) for p in profiles],
        }

    # -- writing ---------------------------------------------------------------

    def _write(self, data: Dict[str, Any]) -> None:
        # mode 0o600: the file holds API keys.
        atomic_write_json(
            self.path, {"schema_version": SCHEMA_VERSION, **data},
            indent=2, ensure_ascii=False, mode=0o600,
        )

    def create(
        self, *, name: Any, endpoint: Any, api_key: Any, model: Any, activate: bool = False
    ) -> Dict[str, Any]:
        now = time.time()
        profile = {
            "id": f"llm-{uuid.uuid4().hex[:12]}",
            "name": _text(name, "name", NAME_LIMIT),
            "endpoint": normalize_endpoint(endpoint),
            "api_key": _api_key(api_key),
            "model": _text(model, "model", MODEL_LIMIT),
            "created_at": now, "updated_at": now,
        }
        with _write_lock:
            data = self._load()
            data["profiles"][profile["id"]] = profile
            if activate:
                data["active_profile_id"] = profile["id"]
            self._write(data)
        return self._public(profile)

    def update(self, profile_id: str, **fields: Any) -> Dict[str, Any]:
        """Change name/endpoint/model; a blank or missing api_key keeps the stored one."""
        with _write_lock:
            data = self._load()
            profile = data["profiles"].get(profile_id)
            if profile is None:
                raise KeyError(profile_id)
            if "name" in fields:
                profile["name"] = _text(fields["name"], "name", NAME_LIMIT)
            new_key = _api_key(fields.get("api_key"))
            if "endpoint" in fields:
                endpoint = normalize_endpoint(fields["endpoint"])
                # Keeping the stored key while moving to another host would
                # hand it to whoever runs that host.
                if (
                    _origin(endpoint) != _origin(profile.get("endpoint", ""))
                    and profile.get("api_key") and not new_key
                ):
                    raise ProfileError("Re-enter the API key when changing the endpoint host")
                profile["endpoint"] = endpoint
            if "model" in fields:
                profile["model"] = _text(fields["model"], "model", MODEL_LIMIT)
            if new_key:
                profile["api_key"] = new_key
            profile["updated_at"] = time.time()
            self._write(data)
            return self._public(profile)

    def delete(self, profile_id: str) -> None:
        with _write_lock:
            data = self._load()
            if profile_id not in data["profiles"]:
                raise KeyError(profile_id)
            if data["active_profile_id"] == profile_id:
                raise ProfileError("Switch to another profile before deleting the active one")
            del data["profiles"][profile_id]
            self._write(data)

    def set_active(self, profile_id: Optional[str]) -> None:
        """Make a profile active; None falls back to the engine's env config."""
        with _write_lock:
            data = self._load()
            if profile_id is not None and profile_id not in data["profiles"]:
                raise KeyError(profile_id)
            data["active_profile_id"] = profile_id
            self._write(data)
