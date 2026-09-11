"""How a batch of events leaves the machine -- and how it fails quietly.

``HttpsTransport`` is the only real transport: one ``POST`` of a JSON
batch over HTTPS, stdlib ``urllib`` (imported lazily so the hot paths pay
nothing), strict timeouts, and a failure backoff file so an unreachable or
misbehaving endpoint is retried with exponential spacing (1 min .. 1 h)
instead of on every command. Plain ``http://`` is refused except to
loopback, so nothing is ever sent unencrypted over a network.
``NullTransport`` (no endpoint configured, or disabled) and
``RecordingTransport`` (tests) never touch the network.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from openshard.adapters.claude_capture_client import capture_home

BACKOFF_FILENAME = "telemetry.backoff.json"
_BACKOFF_MIN_SECONDS = 60.0
_BACKOFF_MAX_SECONDS = 3600.0
CONNECT_TIMEOUT_SECONDS = 1.0
TOTAL_TIMEOUT_SECONDS = 3.0
MAX_BATCH = 50


class Transport(Protocol):
    def send(self, batch: list[dict]) -> bool: ...


class NullTransport:
    """Accepts and discards. Used when no endpoint is configured."""

    name = "null"

    def send(self, batch: list[dict]) -> bool:  # noqa: ARG002 - protocol
        return True


class RecordingTransport:
    """Tests: remembers every batch; ``fail`` makes sends fail."""

    name = "recording"

    def __init__(self, *, fail: bool = False) -> None:
        self.batches: list[list[dict]] = []
        self.fail = fail

    def send(self, batch: list[dict]) -> bool:
        if self.fail:
            return False
        self.batches.append([dict(e) for e in batch])
        return True

    @property
    def events(self) -> list[dict]:
        return [e for b in self.batches for e in b]


def endpoint_allowed(endpoint: object) -> bool:
    """HTTPS anywhere, or plain HTTP to loopback only (local testing)."""
    if not isinstance(endpoint, str) or not endpoint.strip():
        return False
    try:
        parts = urlsplit(endpoint.strip())
    except ValueError:
        return False
    if parts.scheme == "https" and parts.netloc:
        return True
    return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")


class HttpsTransport:
    name = "https"

    def __init__(self, endpoint: str, *, user_agent: str, timeout: float = TOTAL_TIMEOUT_SECONDS) -> None:
        if not endpoint_allowed(endpoint):
            raise ValueError("telemetry endpoint must be https:// (or http:// to loopback)")
        self.endpoint = endpoint.strip()
        self.user_agent = user_agent
        self.timeout = timeout

    def send(self, batch: list[dict]) -> bool:
        import urllib.error
        import urllib.request

        body = json.dumps({"schema_version": 1, "events": batch}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - https/loopback only
                return 200 <= int(response.status) < 300
        except urllib.error.HTTPError as exc:
            # A 4xx means the server rejected the batch; retrying would not
            # help, and holding the events would only grow the queue.
            return 400 <= int(exc.code) < 500
        except Exception:
            return False


# --- failure backoff ---------------------------------------------------------


def _backoff_path(env: dict | os._Environ | None) -> Path:
    return Path(capture_home(env)) / BACKOFF_FILENAME


def read_backoff(env: dict | os._Environ | None = None) -> dict:
    try:
        with _backoff_path(env).open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def in_backoff(env: dict | os._Environ | None = None, *, now: float | None = None) -> bool:
    until = read_backoff(env).get("until")
    return isinstance(until, (int, float)) and (now if now is not None else time.time()) < until


def record_failure(env: dict | os._Environ | None = None, *, now: float | None = None) -> None:
    """Double the wait after each consecutive failure, bounded. Never raises."""
    try:
        data = read_backoff(env)
        failures = int(data.get("failures") or 0) + 1
        wait = min(_BACKOFF_MIN_SECONDS * (2 ** (failures - 1)), _BACKOFF_MAX_SECONDS)
        path = _backoff_path(env)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump({"failures": failures, "until": (now if now is not None else time.time()) + wait}, fh)
        os.replace(tmp, path)
    except Exception:
        pass


def clear_backoff(env: dict | os._Environ | None = None) -> None:
    try:
        _backoff_path(env).unlink()
    except Exception:
        pass
