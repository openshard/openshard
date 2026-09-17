"""One receipt, one HTTPS ``POST``, one classified answer -- and the backoff
that keeps an unreachable or refusing Platform from being hammered.

``HttpsPlatformTransport`` uses stdlib ``urllib`` (no client dependency on
the sync path), strict timeouts and ``Authorization: Bearer <osk_...>``.
Plain ``http://`` is refused except to loopback, so the key never travels
unencrypted. ``RecordingPlatformTransport`` is for tests.

Classification of the Platform's answer (``docs/architecture/receipt-sync-contract.md``
in the Platform repository):

==============  ===========  =============================================
kind            HTTP         meaning for the sender
==============  ===========  =============================================
``created``     201          stored
``duplicate``   200          already stored with the same content
``conflict``    409          different content under this receipt_id: terminal
``rejected``    400/413/422  payload refused (schema, size, privacy): terminal
``unauthorized`` 401         the key is invalid or revoked: pause the link
``forbidden``   403          the key may not act in this organisation: pause
``not_found``   404          no such organisation: pause
``unavailable`` 429/5xx/net  try again later (exponential backoff)
==============  ===========  =============================================
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from openshard.adapters.claude_capture_client import capture_home
from openshard.sync.config import PlatformLink

BACKOFF_FILENAME = "platform-sync.backoff.json"
_BACKOFF_MIN_SECONDS = 60.0
_BACKOFF_MAX_SECONDS = 3600.0
_LINK_PAUSE_SECONDS = 3600.0
TOTAL_TIMEOUT_SECONDS = 10.0
_MAX_ERROR_BODY_BYTES = 64 * 1024

KIND_CREATED = "created"
KIND_DUPLICATE = "duplicate"
KIND_CONFLICT = "conflict"
KIND_REJECTED = "rejected"
KIND_UNAUTHORIZED = "unauthorized"
KIND_FORBIDDEN = "forbidden"
KIND_NOT_FOUND = "not_found"
KIND_UNAVAILABLE = "unavailable"

ACCEPTED_KINDS: frozenset[str] = frozenset({KIND_CREATED, KIND_DUPLICATE})
TERMINAL_KINDS: frozenset[str] = frozenset({KIND_CONFLICT, KIND_REJECTED})
LINK_KINDS: frozenset[str] = frozenset({KIND_UNAUTHORIZED, KIND_FORBIDDEN, KIND_NOT_FOUND})


@dataclass(frozen=True)
class SendResult:
    kind: str
    status: int | None = None
    code: str | None = None
    details: Any = None

    @property
    def accepted(self) -> bool:
        return self.kind in ACCEPTED_KINDS


class PlatformTransport(Protocol):
    def send(self, envelope: dict) -> SendResult: ...


class RecordingPlatformTransport:
    """Tests: answers from a script (cycled when exhausted) and remembers every envelope."""

    name = "recording"

    def __init__(self, results: list[SendResult] | None = None) -> None:
        self.results = list(results or [SendResult(KIND_CREATED, 201)])
        self.envelopes: list[dict] = []
        self._calls = 0

    def send(self, envelope: dict) -> SendResult:
        self.envelopes.append(json.loads(json.dumps(envelope)))
        result = self.results[min(self._calls, len(self.results) - 1)]
        self._calls += 1
        return result


def _parse_error(body: bytes) -> tuple[str | None, Any]:
    try:
        data = json.loads(body[:_MAX_ERROR_BODY_BYTES].decode("utf-8", "replace"))
    except ValueError:
        return None, None
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    return (code if isinstance(code, str) else None), error.get("details")


def classify_status(status: int, body: bytes = b"") -> SendResult:
    """Map an HTTP status (and error body) to a :class:`SendResult`."""
    if status == 201:
        return SendResult(KIND_CREATED, status)
    if status == 200:
        return SendResult(KIND_DUPLICATE, status)
    code, details = _parse_error(body) if status >= 400 else (None, None)
    if status == 409:
        return SendResult(KIND_CONFLICT, status, code or "receipt_conflict", details)
    if status in (400, 413, 422):
        return SendResult(KIND_REJECTED, status, code or "invalid_payload", details)
    if status == 401:
        return SendResult(KIND_UNAUTHORIZED, status, code or "unauthenticated", details)
    if status == 403:
        return SendResult(KIND_FORBIDDEN, status, code or "forbidden", details)
    if status == 404:
        return SendResult(KIND_NOT_FOUND, status, code or "not_found", details)
    return SendResult(KIND_UNAVAILABLE, status, code, None)


class HttpsPlatformTransport:
    name = "https"

    def __init__(self, link: PlatformLink, *, user_agent: str, timeout: float = TOTAL_TIMEOUT_SECONDS) -> None:
        self.url = link.ingest_url()
        self._api_key = link.api_key
        self.user_agent = user_agent
        self.timeout = timeout

    def send(self, envelope: dict) -> SendResult:
        import urllib.error
        import urllib.request

        body = json.dumps(envelope, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": self.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - https/loopback only
                return classify_status(int(response.status))
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(_MAX_ERROR_BODY_BYTES)
            except Exception:
                raw = b""
            return classify_status(int(exc.code), raw)
        except Exception:
            return SendResult(KIND_UNAVAILABLE, None)


# --- failure backoff (user-global: one Platform link, one wait) --------------


def _backoff_path(env: dict | os._Environ | None) -> Path:
    return Path(capture_home(env)) / BACKOFF_FILENAME


def read_backoff(env: dict | os._Environ | None = None) -> dict:
    try:
        with _backoff_path(env).open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def in_backoff(env: dict | os._Environ | None = None, *, now: float | None = None) -> str | None:
    """The reason sending is paused right now, or None."""
    data = read_backoff(env)
    until = data.get("until")
    if isinstance(until, (int, float)) and (now if now is not None else time.time()) < until:
        reason = data.get("reason")
        return reason if isinstance(reason, str) and reason else "unavailable"
    return None


def _write_backoff(env: dict | os._Environ | None, payload: dict) -> None:
    path = _backoff_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def record_failure(env: dict | os._Environ | None = None, *, now: float | None = None) -> None:
    """Endpoint unreachable or 5xx: double the wait after each consecutive failure, bounded. Never raises."""
    try:
        data = read_backoff(env)
        failures = int(data.get("failures") or 0) + 1
        wait = min(_BACKOFF_MIN_SECONDS * (2 ** (failures - 1)), _BACKOFF_MAX_SECONDS)
        current = now if now is not None else time.time()
        _write_backoff(env, {"failures": failures, "until": current + wait, "reason": KIND_UNAVAILABLE})
    except Exception:
        pass


def record_link_failure(kind: str, env: dict | os._Environ | None = None, *, now: float | None = None) -> None:
    """401/403/404: the link itself is wrong. Pause for an hour with the reason; ``connect`` clears it. Never raises."""
    try:
        data = read_backoff(env)
        current = now if now is not None else time.time()
        _write_backoff(env, {
            "failures": int(data.get("failures") or 0) + 1,
            "until": current + _LINK_PAUSE_SECONDS,
            "reason": kind,
        })
    except Exception:
        pass


def clear_backoff(env: dict | os._Environ | None = None) -> None:
    try:
        _backoff_path(env).unlink()
    except Exception:
        pass
