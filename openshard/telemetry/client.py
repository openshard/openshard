"""``emit``: the only way an event is recorded, and ``flush``: the only way one leaves.

Contract (every caller relies on it):

* ``emit`` never raises, never prints, never blocks on the network, and
  returns in well under a millisecond when telemetry is off (an
  environment/consent check and nothing else). When on, it validates the
  event against the schema and appends it to the bounded local queue.
* Sending happens from ``flush``, which the CLI calls on a background
  daemon thread it never joins (a short command may exit with events still
  queued -- they go with the next one) and the capture service calls on a
  timer. Sends have strict timeouts and back off exponentially on failure.
* Nothing here is ever on a coding agent's hook path: hooks are handled by
  the capture service, which emits after a fold on its worker thread.

The transport is chosen once per process from ``OPENSHARD_TELEMETRY_ENDPOINT``,
then the repository's ``telemetry.endpoint`` config, then the built-in
default; an unset/invalid endpoint means ``NullTransport`` (nothing sent).
Tests inject a ``RecordingTransport`` via ``configure``.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Any

from openshard.telemetry import queue as _queue
from openshard.telemetry import state as _state
from openshard.telemetry import transport as _transport
from openshard.telemetry.schema import CONSENT_IMPROVE, build_event, platform_info

# The shipped ingest endpoint. Overridable with OPENSHARD_TELEMETRY_ENDPOINT
# or ``telemetry: {endpoint: ...}`` in .openshard/config.yml; both must be
# https:// (or http:// to loopback for local testing).
DEFAULT_ENDPOINT = "https://telemetry.openshard.dev/v1/events"
ENDPOINT_ENV = "OPENSHARD_TELEMETRY_ENDPOINT"
# Tests / embedding: skip the background flush thread entirely.
NO_BACKGROUND_ENV = "OPENSHARD_TELEMETRY_NO_BACKGROUND"
_FLUSH_DELAY_SECONDS = 0.25  # let a command's last few events batch
_FLUSH_INTERVAL_SECONDS = 60.0  # capture service timer

_lock = threading.Lock()
_transport_override: _transport.Transport | None = None
_repo_config_override: dict | None = None
_flush_scheduled = False
_last_drop_reason: str | None = None
_dropped = 0
_emitted = 0


def configure(*, transport: _transport.Transport | None = None, repo_config: dict | None = None) -> None:
    """Inject a transport / repository config for this process (tests, embedding)."""
    global _transport_override, _repo_config_override, _flush_scheduled
    with _lock:
        _transport_override = transport
        _repo_config_override = repo_config
        _flush_scheduled = False


def counters() -> dict[str, Any]:
    with _lock:
        return {"emitted": _emitted, "dropped": _dropped, "last_drop_reason": _last_drop_reason}


def _version() -> str:
    try:
        from openshard import __version__

        return str(__version__)
    except Exception:
        return "unknown"


def _repo_config() -> dict | None:
    if _repo_config_override is not None:
        return _repo_config_override
    try:
        from openshard.config.settings import load_config_safe

        config, _valid, _path = load_config_safe()
        return config if isinstance(config, dict) else None
    except Exception:
        return None


def resolve_endpoint(env: dict | os._Environ | None = None, repo_config: dict | None = None) -> str | None:
    """The endpoint that would be used, or None when nothing valid is configured."""
    env = os.environ if env is None else env
    candidate: object = env.get(ENDPOINT_ENV)
    if not (isinstance(candidate, str) and candidate.strip()):
        block = repo_config.get("telemetry") if isinstance(repo_config, dict) else None
        candidate = block.get("endpoint") if isinstance(block, dict) else None
    if not (isinstance(candidate, str) and candidate.strip()):
        candidate = DEFAULT_ENDPOINT
    return candidate.strip() if _transport.endpoint_allowed(candidate) else None


def _make_transport(env: dict | os._Environ | None, repo_config: dict | None) -> _transport.Transport:
    if _transport_override is not None:
        return _transport_override
    endpoint = resolve_endpoint(env, repo_config)
    if endpoint is None:
        return _transport.NullTransport()
    try:
        return _transport.HttpsTransport(endpoint, user_agent=f"openshard/{_version()}")
    except ValueError:
        return _transport.NullTransport()


def status(env: dict | os._Environ | None = None) -> dict[str, Any]:
    """Everything ``openshard telemetry status`` shows. Never raises."""
    env = os.environ if env is None else env
    try:
        repo_config = _repo_config()
        current = _state.load_state(env)
        effective = _state.effective_status(env=env, repo_config=repo_config, state=current)
        return {
            "enabled": effective.enabled,
            "reason": effective.reason,
            "consent": effective.consent,
            "consent_decided_at": current.improve_decided_at if current else None,
            "consent_source": current.improve_source if current else None,
            "installation_id": current.installation_id if current else None,
            "endpoint": resolve_endpoint(env, repo_config),
            "queued": _queue.size(env),
            "in_backoff": _transport.in_backoff(env),
            "schema_version": 1,
            "state_path": str(_state.state_path(env)),
        }
    except Exception:
        return {"enabled": False, "reason": "unavailable", "consent": "unset"}


def emit(event_type: str, /, env: dict | os._Environ | None = None, **properties: Any) -> bool:
    """Record one event if telemetry is on. Returns True when queued. Never raises."""
    global _dropped, _emitted, _last_drop_reason
    try:
        env = os.environ if env is None else env
        current = _state.load_state(env)
        effective = _state.effective_status(env=env, repo_config=_repo_config(), state=current)
        if not effective.enabled or current is None:
            return False
        event, reason = build_event(
            event_type, properties,
            installation_id=current.installation_id,
            openshard_version=_version(),
            consent_level=CONSENT_IMPROVE,
            platform=platform_info(),
        )
        if event is None:
            with _lock:
                _dropped += 1
                _last_drop_reason = reason
            return False
        if not _queue.append(event, env):
            return False
        with _lock:
            _emitted += 1
        _schedule_flush(env)
        return True
    except Exception:
        return False


def _schedule_flush(env: dict | os._Environ) -> None:
    global _flush_scheduled
    if env.get(NO_BACKGROUND_ENV):
        return
    with _lock:
        if _flush_scheduled:
            return
        _flush_scheduled = True
    env_copy = dict(env)

    def run() -> None:
        global _flush_scheduled
        time.sleep(_FLUSH_DELAY_SECONDS)
        try:
            flush(env=env_copy)
        finally:
            with _lock:
                _flush_scheduled = False

    threading.Thread(target=run, name="openshard-telemetry-flush", daemon=True).start()


def flush(*, env: dict | os._Environ | None = None, transport: _transport.Transport | None = None,
          max_batches: int = 4) -> int:
    """Send queued events. Returns the number sent. Never raises, never blocks past its timeouts."""
    env = os.environ if env is None else env
    sent = 0
    try:
        repo_config = _repo_config()
        effective = _state.effective_status(env=env, repo_config=repo_config)
        if not effective.enabled:
            return 0
        if _transport.in_backoff(env):
            return 0
        chosen = transport or _make_transport(env, repo_config)
        for _ in range(max_batches):
            batch = _queue.take(_transport.MAX_BATCH, env)
            if not batch:
                break
            try:
                ok = bool(chosen.send(batch))
            except Exception:
                # A transport that raises is a failed send, never a lost batch.
                ok = False
            if ok:
                sent += len(batch)
                _transport.clear_backoff(env)
            else:
                _queue.requeue(batch, env)
                _transport.record_failure(env)
                break
    except Exception:
        pass
    return sent


def flush_periodically(stop: threading.Event, *, env: dict | os._Environ | None = None,
                       interval: float = _FLUSH_INTERVAL_SECONDS) -> None:
    """Long-running processes (the capture service): flush every *interval* until *stop*."""
    while not stop.wait(interval):
        flush(env=env)
    flush(env=env)


@contextmanager
def timed_command(command: str, *, env: dict | os._Environ | None = None):
    """Emit ``command.invoked`` with the duration and a closed error category.

    The exception, if any, is re-raised unchanged; only its category is
    recorded, never its message.
    """
    from openshard.telemetry.events import error_category

    t0 = time.perf_counter()
    try:
        yield
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        emit("command.invoked", env, command=command, duration_ms=int((time.perf_counter() - t0) * 1000),
             result="ok" if code == 0 else "error", error_category=None if code == 0 else "unknown")
        raise
    except BaseException as exc:
        emit("command.invoked", env, command=command, duration_ms=int((time.perf_counter() - t0) * 1000),
             result="error", error_category=error_category(exc))
        raise
    emit("command.invoked", env, command=command, duration_ms=int((time.perf_counter() - t0) * 1000),
         result="ok", error_category=None)
