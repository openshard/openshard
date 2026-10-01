"""Persistent account connection for connected capture.

A provider or IDE stores one narrow osc_ credential. Trusted local installs
may reuse the existing osk_ Platform link. Per-run identity is separate from
that credential, so one connection safely serves many sessions.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any

from openshard.sync.config import normalise_endpoint, resolve_link

ENDPOINT_ENV = "OPENSHARD_CONNECTED_ENDPOINT"
ORG_ENV = "OPENSHARD_CONNECTED_ORG_ID"
TOKEN_ENV = "OPENSHARD_CONNECTED_TOKEN"
SURFACE_ENV = "OPENSHARD_CONNECTED_SURFACE"
DISABLE_ENV = "OPENSHARD_CONNECTED_CAPTURE"

_FALSEY = frozenset({"0", "off", "false", "no", "disabled"})
_TOKEN_RE = re.compile(r"^os[ck]_[A-Za-z0-9_-]{8,200}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_SURFACE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


@dataclass(frozen=True)
class ConnectedConnection:
    endpoint: str
    organisation_id: str
    token: str
    source: str


@dataclass(frozen=True)
class ConnectedSession:
    surface: str
    external_session_id: str
    agent: str
    provider: str | None
    repo_identity: str | None
    repo: str | None
    branch: str | None

    def to_state(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "external_session_id": self.external_session_id,
            "agent": self.agent,
            "provider": self.provider,
            "repo_identity": self.repo_identity,
            "repo": self.repo,
            "branch": self.branch,
        }

    @classmethod
    def from_state(cls, value: object) -> ConnectedSession | None:
        if not isinstance(value, dict):
            return None
        surface = value.get("surface")
        sid = value.get("external_session_id")
        agent = value.get("agent")
        if not (isinstance(surface, str) and _SURFACE_RE.match(surface)):
            return None
        if not (isinstance(sid, str) and _ID_RE.match(sid)):
            return None
        if not isinstance(agent, str) or not agent:
            return None

        def opt(key: str) -> str | None:
            v = value.get(key)
            return v if isinstance(v, str) and v else None

        return cls(surface, sid, agent[:80], opt("provider"), opt("repo_identity"), opt("repo"), opt("branch"))


def disabled(env: dict | os._Environ | None = None) -> bool:
    source = os.environ if env is None else env
    raw = source.get(DISABLE_ENV)
    return isinstance(raw, str) and raw.strip().lower() in _FALSEY


def _explicit(env: dict | os._Environ) -> ConnectedConnection | None:
    endpoint = normalise_endpoint(env.get(ENDPOINT_ENV))
    org = env.get(ORG_ENV)
    token = env.get(TOKEN_ENV)
    if endpoint is None or not isinstance(org, str) or not _UUID_RE.match(org):
        return None
    if not isinstance(token, str) or not _TOKEN_RE.match(token.strip()):
        return None
    return ConnectedConnection(endpoint, org.lower(), token.strip(), "env")


def resolve_connection(env: dict | os._Environ | None = None) -> ConnectedConnection | None:
    source = os.environ if env is None else env
    if disabled(source):
        return None
    explicit = _explicit(source)
    if explicit is not None:
        return explicit
    link = resolve_link(source)
    if link is None:
        return None
    return ConnectedConnection(link.endpoint, link.organisation_id, link.api_key, "platform_link")


def available_hint(env: dict | os._Environ | None = None) -> bool:
    """Cheap hook-path check: could persistent connected capture be available?

    This intentionally avoids parsing either credential file on every hook.
    A false positive is harmless because the collector resolves and validates
    the connection before spooling; a false negative would lose streaming.
    """
    source = os.environ if env is None else env
    if disabled(source):
        return False
    if source.get(ENDPOINT_ENV) and source.get(ORG_ENV) and source.get(TOKEN_ENV):
        return True
    try:
        from openshard.sync.config import API_KEY_ENV, ENDPOINT_ENV as PLATFORM_ENDPOINT_ENV
        from openshard.sync.config import ORG_ENV as PLATFORM_ORG_ENV, config_path

        if source.get(PLATFORM_ENDPOINT_ENV) and source.get(PLATFORM_ORG_ENV) and source.get(API_KEY_ENV):
            return True
        return config_path(source).exists()
    except OSError:
        return False


def _safe_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if _ID_RE.match(value):
        return value
    return "sid_" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:32]


def _surface(agent: str, env: dict | os._Environ) -> str:
    configured = env.get(SURFACE_ENV)
    if isinstance(configured, str):
        configured = configured.strip().lower()
        if _SURFACE_RE.match(configured):
            return configured
    if agent == "claude_code" and str(env.get("CLAUDE_CODE_REMOTE", "")).lower() in {"1", "true", "yes"}:
        return "claude-code-web"
    if agent == "cursor" and str(env.get("CURSOR_CODE_REMOTE", "")).lower() in {"1", "true", "yes"}:
        return "cursor-cloud"
    clean = re.sub(r"[^a-z0-9_.:-]+", "-", agent.lower()).strip("-") or "agent"
    return (clean[:54] + "-local")[:64]


def session_from_entry(
    entry: dict | None,
    record: dict | None,
    *,
    env: dict | os._Environ | None = None,
) -> ConnectedSession | None:
    source = os.environ if env is None else env
    entry_dict: dict = entry if isinstance(entry, dict) else {}
    record_dict: dict = record if isinstance(record, dict) else {}
    capture_raw = entry_dict.get("capture")
    capture: dict = capture_raw if isinstance(capture_raw, dict) else {}
    sid = _safe_id(capture.get("session_id")) or _safe_id(record_dict.get("run_id"))
    if sid is None:
        return None
    agent = capture.get("agent")
    if not isinstance(agent, str) or not agent:
        agent = "other"
    provider = capture.get("provider")
    if not isinstance(provider, str) or not provider:
        provider = None

    def opt(value: object, limit: int = 256) -> str | None:
        return value[:limit] if isinstance(value, str) and value else None

    return ConnectedSession(
        surface=_surface(agent, source),
        external_session_id=sid,
        agent=agent[:80],
        provider=opt(provider, 80),
        repo_identity=opt(entry_dict.get("repo_identity")),
        repo=opt(entry_dict.get("repo")),
        branch=opt(entry_dict.get("branch")),
    )


def sink_id(connection: ConnectedConnection, session: ConnectedSession) -> str:
    raw = "|".join((connection.endpoint, connection.organisation_id, session.surface, session.external_session_id))
    return "connected-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "ConnectedConnection", "ConnectedSession", "DISABLE_ENV", "ENDPOINT_ENV", "ORG_ENV",
    "SURFACE_ENV", "TOKEN_ENV", "available_hint", "disabled", "resolve_connection", "session_from_entry", "sink_id",
]