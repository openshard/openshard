"""Which experimental capabilities the Platform has switched on for the linked organisation.

One read, ``GET {endpoint}/v1/orgs/{organisation_id}/capabilities`` with the
same link (endpoint, organisation, ``osk_`` key) that receipt sync uses; no
second credential. The Platform lists only the capabilities that are *on*
for that organisation, and a key can read no other organisation's list.

Core caches the key set briefly in ``<OPENSHARD_HOME>/capabilities.json``
so a run makes at most one request. The cache answers only for the link
that wrote it: endpoint, organisation id and key prefix must match and the
entry carries an HMAC over its contents keyed with the API key itself, so
an entry written under another key (or edited by hand) is ignored without
the key ever being stored. A failed read is remembered for one minute so an
offline Platform does not cost every run a full timeout.

Everything that is not a fresh or validly cached answer -- no link, the
``OPENSHARD_PLATFORM_SYNC`` kill switch, 401/403/404/5xx, a timeout, bad
JSON, a body for a different organisation, an expired or foreign cache --
means *every* capability is off. A capability is never assumed on.

The Platform contract this follows: ``docs/architecture/capabilities.md``
in the Platform repository ("How Core will query capabilities").
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openshard.adapters.claude_capture_client import capture_home
from openshard.sync.config import (
    PlatformLink,
    _write_private,
    resolve_link,
    sync_disabled,
)
from openshard.sync.transport import TOTAL_TIMEOUT_SECONDS

CACHE_FILENAME = "capabilities.json"
CACHE_SCHEMA_VERSION = 1
CACHE_TTL_SECONDS = 600.0
NEGATIVE_TTL_SECONDS = 60.0
_MAX_BODY_BYTES = 256 * 1024

CAPABILITY_AGENT_BUDGETS = "agent_budgets"

SOURCE_FRESH = "fresh"
SOURCE_CACHE = "cache"
SOURCE_UNAVAILABLE = "unavailable"

REASON_NO_LINK = "no_platform_link"
REASON_UNAVAILABLE = "platform_unreachable_or_refused"
REASON_SYNC_DISABLED = "platform_sync_disabled"

# Platform registry key shape (packages/capabilities/src/registry.ts).
_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_MAX_KEY_LEN = 64

Fetcher = Callable[[PlatformLink], "frozenset[str] | None"]


@dataclass(frozen=True)
class CapabilityState:
    """The enabled key set and where it came from. ``unavailable`` means everything is off."""

    keys: frozenset[str]
    source: str
    reason: str | None = None
    organisation_id: str | None = None

    def enabled(self, key: str) -> bool:
        return self.source != SOURCE_UNAVAILABLE and key in self.keys


def cache_path(env: dict | os._Environ | None = None) -> Path:
    return Path(capture_home(env)) / CACHE_FILENAME


def _version() -> str:
    try:
        from openshard import __version__

        return str(__version__)
    except Exception:
        return "unknown"


def _parse_keys(body: bytes, *, organisation_id: str) -> frozenset[str] | None:
    """The enabled key set from a contract response body, or None when it is not one."""
    try:
        data = json.loads(body[:_MAX_BODY_BYTES].decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    org = data.get("organisation_id")
    if not isinstance(org, str) or org.strip().lower() != organisation_id:
        # A list for some other organisation is never ours.
        return None
    items = data.get("capabilities")
    if not isinstance(items, list):
        return None
    keys: set[str] = set()
    for item in items:
        # The Platform lists enabled capabilities only; a reader that fails
        # closed still requires the flag to say so.
        if not isinstance(item, dict) or item.get("enabled") is not True:
            continue
        key = item.get("key")
        if isinstance(key, str) and len(key) <= _MAX_KEY_LEN and _KEY_RE.match(key):
            keys.add(key)
    return frozenset(keys)


def fetch_enabled_capabilities(
    link: PlatformLink,
    *,
    user_agent: str | None = None,
    timeout: float = TOTAL_TIMEOUT_SECONDS,
) -> frozenset[str] | None:
    """One GET for the organisation's enabled capabilities. None on any failure. Never raises.

    Redirects are refused: the bearer key travels to the linked endpoint only.
    """
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
            return None

    request = urllib.request.Request(
        link.capabilities_url(),
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {link.api_key}",
            "User-Agent": user_agent or f"openshard/{_version()}",
        },
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - https/loopback only
            if int(response.status) != 200:
                return None
            body = response.read(_MAX_BODY_BYTES + 1)
    except Exception:
        return None
    if len(body) > _MAX_BODY_BYTES:
        return None
    return _parse_keys(body, organisation_id=link.organisation_id)


def _cache_payload(link: PlatformLink, keys: frozenset[str] | None, *, now: float) -> dict:
    """``keys`` is None for a remembered failure (short negative TTL)."""
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "endpoint": link.endpoint,
        "organisation_id": link.organisation_id,
        "key_prefix": link.key_prefix,
        "fetched_at": now,
        "keys": sorted(keys) if keys is not None else None,
    }


def _signature(link: PlatformLink, payload: dict) -> str:
    """HMAC-SHA256 over the canonical payload, keyed with the API key (which is never stored here)."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hmac.new(link.api_key.encode("utf-8"), blob, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class _Cached:
    keys: frozenset[str] | None  # None: a remembered failure


def _read_cache(
    path: Path, link: PlatformLink, *, now: float, ttl_seconds: float, negative_ttl_seconds: float,
) -> _Cached | None:
    """The cached answer when it was written for exactly this link, signed with its key, and still fresh."""
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    if (
        data.get("endpoint") != link.endpoint
        or data.get("organisation_id") != link.organisation_id
        or data.get("key_prefix") != link.key_prefix
    ):
        return None
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, (int, float)) or isinstance(fetched_at, bool):
        return None
    keys = data.get("keys")
    if keys is not None and (not isinstance(keys, list) or not all(isinstance(k, str) for k in keys)):
        return None
    signature = data.get("signature")
    payload = {k: v for k, v in data.items() if k != "signature"}
    if not isinstance(signature, str) or not hmac.compare_digest(signature, _signature(link, payload)):
        return None
    age = now - float(fetched_at)
    ttl = ttl_seconds if keys is not None else negative_ttl_seconds
    if age < 0 or age >= ttl:
        return None
    return _Cached(frozenset(keys) if keys is not None else None)


def _write_cache(path: Path, link: PlatformLink, keys: frozenset[str] | None, *, now: float) -> None:
    try:
        payload = _cache_payload(link, keys, now=now)
        payload["signature"] = _signature(link, payload)
        _write_private(path, payload)
    except Exception:
        pass  # a cache that cannot be written only costs a request next time


def resolve_capabilities(
    env: dict | os._Environ | None = None,
    *,
    now: float | None = None,
    fetcher: Fetcher | None = None,
    ttl_seconds: float = CACHE_TTL_SECONDS,
    negative_ttl_seconds: float = NEGATIVE_TTL_SECONDS,
) -> CapabilityState:
    """The enabled capabilities for the linked organisation. Fails closed. Never raises.

    ``OPENSHARD_PLATFORM_SYNC=off`` stops this read as well as receipt sync: a
    user who switched Platform traffic off gets no request and every
    capability off. The repository-level ``platform: {sync: false}`` governs
    receipts only and is not consulted here.
    """
    link = resolve_link(env)
    if link is None:
        return CapabilityState(frozenset(), SOURCE_UNAVAILABLE, REASON_NO_LINK)
    if sync_disabled(env, None) is not None:
        return CapabilityState(frozenset(), SOURCE_UNAVAILABLE, REASON_SYNC_DISABLED, link.organisation_id)
    current = time.time() if now is None else float(now)
    path = cache_path(env)
    cached = _read_cache(
        path, link, now=current, ttl_seconds=ttl_seconds, negative_ttl_seconds=negative_ttl_seconds,
    )
    if cached is not None:
        if cached.keys is None:
            return CapabilityState(frozenset(), SOURCE_UNAVAILABLE, REASON_UNAVAILABLE, link.organisation_id)
        return CapabilityState(cached.keys, SOURCE_CACHE, None, link.organisation_id)
    try:
        keys = (fetcher or fetch_enabled_capabilities)(link)
    except Exception:
        keys = None
    if keys is None:
        _write_cache(path, link, None, now=current)  # remember the failure briefly; still off
        return CapabilityState(frozenset(), SOURCE_UNAVAILABLE, REASON_UNAVAILABLE, link.organisation_id)
    _write_cache(path, link, frozenset(keys), now=current)
    return CapabilityState(frozenset(keys), SOURCE_FRESH, None, link.organisation_id)


def capability_enabled(key: str, env: dict | os._Environ | None = None, **kwargs) -> bool:
    """True only when the Platform confirmed *key* is on for the linked organisation."""
    return resolve_capabilities(env, **kwargs).enabled(key)


__all__ = [
    "CACHE_FILENAME",
    "CACHE_TTL_SECONDS",
    "CAPABILITY_AGENT_BUDGETS",
    "NEGATIVE_TTL_SECONDS",
    "REASON_NO_LINK",
    "REASON_SYNC_DISABLED",
    "REASON_UNAVAILABLE",
    "SOURCE_CACHE",
    "SOURCE_FRESH",
    "SOURCE_UNAVAILABLE",
    "CapabilityState",
    "cache_path",
    "capability_enabled",
    "fetch_enabled_capabilities",
    "resolve_capabilities",
]
