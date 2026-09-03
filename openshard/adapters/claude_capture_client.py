"""Thin client for the local Claude Code capture service (PR9.5).

This module is what the two *command-form* Claude Code entrypoints run
(``openshard hooks claude`` for ``SessionStart``, and the ``statusLine``
command ``openshard hooks claude-status``). Both are spawned as a fresh
Python process by Claude Code, so everything here is written to import as
little as possible: ``json``, ``os``, ``socket``, ``sys``, ``time`` and
nothing from :mod:`openshard` unless the service is unreachable and the
synchronous fallback (:mod:`openshard.adapters.claude_hooks`) is needed.

Responsibilities
----------------
* Find the service: the state file the service writes on start
  (``<capture home>/claude-capture.json``, see :func:`capture_home`) names
  its port; without one the default port is tried.
* Forward a payload with a hand-rolled loopback HTTP POST (``http.client``
  alone costs more to import than the whole request takes).
* Start the service when it is not running (``ensure_service``): a
  detached, log-to-file child of the *same* interpreter, then wait briefly
  for its health endpoint. Idempotent: a concurrent start that loses the
  port race simply exits, so two hooks racing never leave two services.
* Fall back to in-process handling when the service cannot be reached, so
  capture still works (more slowly) if the service cannot start at all.

Environment knobs (all optional, none required for normal use):

* ``OPENSHARD_HOME`` -- where the state file / log live (default
  ``~/.openshard``). Tests point this at a temp dir.
* ``OPENSHARD_CAPTURE_DISABLE=1`` -- never contact or start the service;
  handle everything in-process exactly as before PR9.5. This is a full
  kill switch: no socket is opened at all.
* ``OPENSHARD_CAPTURE_NO_SPAWN=1`` -- contact the service if it is
  running, but never start one (tests; CI).
* ``OPENSHARD_CAPTURE_PORT=N`` -- pin the service port (both what the
  service binds and what clients/installers use) instead of the default
  range.

Spawn coordination
-------------------
Every hook (and the status line, which can fire far more often than any
hook) independently asks "is a service running?" and, if not, tries to
start one. Without coordination that is a spawn storm: several concurrent
hooks each launch their own ``capture serve`` child. Two mechanisms bound
this, both scoped to *this* module only (no dependency on the history
stack):

* A cross-process file lock (``claude-capture.startlock``, a minimal
  reimplementation of the same sidecar-lock technique
  ``history/jsonl_store`` uses, kept local here so importing this module
  never pulls in the history stack) serializes the check-then-spawn
  sequence: whoever holds it is the only process allowed to actually spawn;
  everyone else re-checks health once the lock is available (or once their
  own bounded wait for it expires) and never spawns a second time.
* A short fixed cooldown (``claude-capture.backoff.json``) recorded after a
  spawn attempt that never became healthy stops the *next* caller from
  immediately trying again. It is intentionally simple (a fixed window, not
  exponential backoff) -- this is storm prevention, not a retry policy.

Every wait in this module is bounded; nothing here retries in a loop
across multiple attempts within one call.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import time

DEFAULT_PORT = 47811
PORT_RANGE = 10  # DEFAULT_PORT .. DEFAULT_PORT + PORT_RANGE - 1 are tried on conflict
STATE_FILENAME = "claude-capture.json"
LOG_FILENAME = "claude-capture.log"
START_LOCK_FILENAME = "claude-capture.startlock"
BACKOFF_FILENAME = "claude-capture.backoff.json"
SERVICE_NAME = "openshard-claude-capture"
HOOK_PATH = "/hooks/claude"
STATUS_PATH = "/status/claude"
HEALTH_PATH = "/health"
SHUTDOWN_PATH = "/shutdown"
PROJECT_DIR_HEADER = "X-OpenShard-Project-Dir"

_CONNECT_TIMEOUT = 0.25  # loopback connect; refused/absent is instant, a hang must not stall a hook
_REQUEST_TIMEOUT = 5.0
_START_WAIT_SECONDS = 6.0
_START_POLL_SECONDS = 0.05
_LOCK_ACQUIRE_TIMEOUT = 8.0  # ensure_service: worth a real wait, still bounded
_LOCK_POLL_SECONDS = 0.05
_SPAWN_COOLDOWN_SECONDS = 5.0  # fixed window after a failed start before trying again
_STATUS_LOCK_TIMEOUT = 0.5  # maybe_spawn_service: the status line must not block long
_STATUS_SPAWN_WAIT_SECONDS = 1.0


def capture_home(env: dict | os._Environ | None = None) -> str:
    env = os.environ if env is None else env
    override = env.get("OPENSHARD_HOME")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return os.path.join(os.path.expanduser("~"), ".openshard")


def state_path(env: dict | os._Environ | None = None) -> str:
    return os.path.join(capture_home(env), STATE_FILENAME)


def log_path(env: dict | os._Environ | None = None) -> str:
    return os.path.join(capture_home(env), LOG_FILENAME)


def _start_lock_path(env: dict | os._Environ | None) -> str:
    return os.path.join(capture_home(env), START_LOCK_FILENAME)


def _backoff_path(env: dict | os._Environ | None) -> str:
    return os.path.join(capture_home(env), BACKOFF_FILENAME)


def read_state(env: dict | os._Environ | None = None) -> dict | None:
    """The service's state file as a dict, or None when absent/unreadable."""
    try:
        with open(state_path(env), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def pinned_port(env: dict | os._Environ | None = None) -> int | None:
    """``OPENSHARD_CAPTURE_PORT`` as an int, or None when unset/invalid."""
    env = os.environ if env is None else env
    raw = env.get("OPENSHARD_CAPTURE_PORT")
    if isinstance(raw, str) and raw.strip().isdigit():
        port = int(raw.strip())
        if 0 < port < 65536:
            return port
    return None


def disabled(env: dict | os._Environ | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("OPENSHARD_CAPTURE_DISABLE"))


def resolve_port(env: dict | os._Environ | None = None) -> int:
    """Port the service is expected on: the pinned one, else the state file's, else the default."""
    pinned = pinned_port(env)
    if pinned is not None:
        return pinned
    state = read_state(env)
    port = state.get("port") if state else None
    if isinstance(port, int) and 0 < port < 65536:
        return port
    return DEFAULT_PORT


# ---------------------------------------------------------------------------
# Minimal loopback HTTP
# ---------------------------------------------------------------------------


def _request(
    method: str,
    port: int,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    *,
    timeout: float = _REQUEST_TIMEOUT,
) -> tuple[int, bytes] | None:
    """One HTTP/1.0 request to 127.0.0.1:*port*. Returns (status, body) or None on any failure."""
    if disabled():
        return None
    lines = [f"{method} {path} HTTP/1.0", "Host: 127.0.0.1", "Connection: close"]
    for key, value in (headers or {}).items():
        safe = str(value).replace("\r", "").replace("\n", "")
        lines.append(f"{key}: {safe}")
    if body is not None:
        lines.append("Content-Type: application/json")
        lines.append(f"Content-Length: {len(body)}")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii", "replace")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_CONNECT_TIMEOUT) as sock:
            sock.settimeout(timeout)
            sock.sendall(head + (body or b""))
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except (OSError, ValueError):
        return None
    raw = b"".join(chunks)
    header_end = raw.find(b"\r\n\r\n")
    if header_end < 0:
        return None
    status_line = raw[:header_end].split(b"\r\n", 1)[0]
    parts = status_line.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1]), raw[header_end + 4:]


def health(port: int, *, timeout: float = 1.0) -> dict | None:
    """The service's health document if an OpenShard capture service answers on *port*."""
    result = _request("GET", port, HEALTH_PATH, timeout=timeout)
    if result is None or result[0] != 200:
        return None
    try:
        data = json.loads(result[1].decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("service") != SERVICE_NAME:
        return None
    return data


def post_hook(
    port: int, raw: bytes, *, project_dir: str | None = None, event_override: str | None = None
) -> bool:
    """POST one raw hook payload. True when the service accepted (durably queued) it."""
    path = HOOK_PATH
    if event_override:
        safe = "".join(ch for ch in event_override if ch.isalnum())
        path = f"{HOOK_PATH}?event={safe}"
    headers = {PROJECT_DIR_HEADER: project_dir} if project_dir else None
    result = _request("POST", port, path, raw, headers)
    return result is not None and result[0] == 200


def post_status(port: int, raw: bytes, *, project_dir: str | None = None) -> bool:
    headers = {PROJECT_DIR_HEADER: project_dir} if project_dir else None
    result = _request("POST", port, STATUS_PATH, raw, headers)
    return result is not None and result[0] == 200


# ---------------------------------------------------------------------------
# Spawn coordination: cross-process lock + cooldown (see module docstring)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _start_lock(env: dict | os._Environ | None, *, timeout: float):
    """Cross-process mutual exclusion around "check health, then maybe spawn".

    Yields ``True`` if the lock was acquired within *timeout* seconds,
    ``False`` otherwise -- a caller that gets ``False`` must not spawn (someone
    else owns the decision right now); it may re-check health once and stop.
    Self-contained (no import of ``openshard.history.jsonl_store``): this
    module stays minimal-import (see module docstring), and reimplementing
    ~20 lines of the same sidecar-lock technique here is cheaper than pulling
    in the history stack just to avoid a spawn race. Never raises.
    """
    path = _start_lock_path(env)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path, "a+")
    except OSError:
        yield False
        return
    acquired = False
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        if sys.platform == "win32":
            import msvcrt

            try:
                if os.fstat(fh.fileno()).st_size < 1:
                    fh.write("\0")
                    fh.flush()
            except OSError:
                pass
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_LOCK_POLL_SECONDS)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_LOCK_POLL_SECONDS)
        yield acquired
    finally:
        try:
            if acquired:
                if sys.platform == "win32":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()


def _read_backoff(env: dict | os._Environ | None) -> dict | None:
    try:
        with open(_backoff_path(env), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _in_cooldown(env: dict | os._Environ | None) -> bool:
    """True when a spawn attempt failed too recently to try again yet."""
    data = _read_backoff(env)
    if not data:
        return False
    failed_at = data.get("failed_at")
    if not isinstance(failed_at, (int, float)):
        return False
    return (time.time() - failed_at) < _SPAWN_COOLDOWN_SECONDS


def _record_spawn_failure(env: dict | os._Environ | None) -> None:
    try:
        os.makedirs(capture_home(env), exist_ok=True)
        with open(_backoff_path(env), "w", encoding="utf-8") as fh:
            json.dump({"failed_at": time.time()}, fh)
    except OSError:
        pass


def _clear_backoff(env: dict | os._Environ | None) -> None:
    try:
        os.unlink(_backoff_path(env))
    except OSError:
        pass


def _spawn_once_and_wait(env: dict | os._Environ, *, wait_seconds: float) -> tuple[int | None, str]:
    """Spawn exactly once (respecting cooldown) and wait up to *wait_seconds*.

    Caller must already hold ``_start_lock``. Returns ``(port, state)`` with
    state in ``running`` (someone finished just before we spawned) /
    ``started`` / ``unavailable``. Never spawns more than once per call, and
    never retries after a failure within this call -- see
    ``_SPAWN_COOLDOWN_SECONDS`` for the *cross-call* throttle instead.
    """
    port = resolve_port(env)
    if health(port) is not None:
        return port, "running"
    if _in_cooldown(env):
        return None, "unavailable"
    if spawn_service(env) is None:
        _record_spawn_failure(env)
        return None, "unavailable"
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        port = resolve_port(env)
        if health(port) is not None:
            _clear_backoff(env)
            return port, "started"
        if time.monotonic() >= deadline:
            _record_spawn_failure(env)
            return None, "unavailable"
        time.sleep(_START_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Lifecycle: start / ensure / stop
# ---------------------------------------------------------------------------


def spawn_service(env: dict | os._Environ | None = None) -> int | None:
    """Start a detached capture service with this interpreter. Returns its pid, or None.

    The child gets no inherited stdio (a sync hook's stdout pipe must close
    when the hook exits, or Claude Code keeps waiting), logs to
    ``<capture home>/claude-capture.log`` (truncated per start, so it stays
    bounded), and is put in its own session / process group so it outlives
    the hook that started it.
    """
    import subprocess

    env = os.environ if env is None else env
    home = capture_home(env)
    try:
        os.makedirs(home, exist_ok=True)
    except OSError:
        return None
    argv = [sys.executable, "-m", "openshard.cli.entrypoint", "capture", "serve"]
    child_env = dict(env)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        log_fh = open(log_path(env), "w", encoding="utf-8")
    except OSError:
        log_fh = None
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fh if log_fh is not None else subprocess.DEVNULL,
        "stderr": subprocess.STDOUT if log_fh is not None else subprocess.DEVNULL,
        "cwd": home,
        "env": child_env,
        "close_fds": True,
    }
    if sys.platform == "win32":
        # CREATE_NO_WINDOW rather than DETACHED_PROCESS: with a venv launcher
        # in the chain (launcher -> real python) a detached child ends up
        # allocating a *visible* console for the interpreter; a hidden one
        # is inherited instead, and the service then drops it entirely
        # (FreeConsole) so no console control event can ever reach it.
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except Exception:
        return None
    finally:
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass
    return proc.pid


def ensure_service(
    env: dict | os._Environ | None = None,
    *,
    wait_seconds: float = _START_WAIT_SECONDS,
) -> tuple[int | None, str]:
    """Return ``(port, state)`` with state in ``running`` | ``started`` | ``disabled`` | ``unavailable``.

    Idempotent and race-safe: a service that is already up is left alone.
    Concurrent callers converge on exactly one spawn attempt (see module
    docstring: the ``_start_lock`` file lock), which is retried again only
    after ``_SPAWN_COOLDOWN_SECONDS`` since the last failed attempt. Every
    wait here is bounded; this never loops indefinitely. Never raises.
    """
    env = os.environ if env is None else env
    if disabled(env):
        return None, "disabled"
    port = resolve_port(env)
    if health(port) is not None:
        return port, "running"
    if pinned_port(env) is None and port != DEFAULT_PORT and health(DEFAULT_PORT) is not None:
        return DEFAULT_PORT, "running"
    if env.get("OPENSHARD_CAPTURE_NO_SPAWN"):
        return None, "unavailable"
    with _start_lock(env, timeout=_LOCK_ACQUIRE_TIMEOUT) as acquired:
        if not acquired:
            # Someone else is already deciding; converge on their result
            # with one more bounded look rather than racing a second spawn.
            port = resolve_port(env)
            return (port, "running") if health(port) is not None else (None, "unavailable")
        return _spawn_once_and_wait(env, wait_seconds=wait_seconds)


def maybe_spawn_service(
    env: dict | os._Environ | None = None,
    *,
    wait_seconds: float = _STATUS_SPAWN_WAIT_SECONDS,
    lock_wait_seconds: float = _STATUS_LOCK_TIMEOUT,
) -> None:
    """Best-effort, storm-safe attempt to get a service running. Never blocks long, never raises.

    For callers (the status line) that must return promptly even when no
    service exists yet: unlike :func:`ensure_service`, the lock wait and the
    post-spawn health wait here are both short and fixed by design -- a
    bounded worst case of about ``lock_wait_seconds + wait_seconds`` seconds
    added to a status-line render, only on a cold start, never per ping.
    Uses the same lock and cooldown as ``ensure_service``, so a hook and the
    status line racing to start a service still converge on one spawn.
    """
    env = os.environ if env is None else env
    if disabled(env) or env.get("OPENSHARD_CAPTURE_NO_SPAWN"):
        return
    if health(resolve_port(env)) is not None:
        return
    with _start_lock(env, timeout=lock_wait_seconds) as acquired:
        if not acquired:
            return
        _spawn_once_and_wait(env, wait_seconds=wait_seconds)


def request_shutdown(env: dict | os._Environ | None = None, *, wait_seconds: float = 5.0) -> bool:
    """Ask a running service to drain and exit. True when it is gone afterwards."""
    env = os.environ if env is None else env
    port = resolve_port(env)
    doc = health(port)
    if doc is None:
        return True
    body = json.dumps({"instance_id": doc.get("instance_id")}).encode("utf-8")
    _request("POST", port, SHUTDOWN_PATH, body, timeout=2.0)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if health(port, timeout=0.5) is None:
            return True
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
# Entrypoint helpers (what the console script runs)
# ---------------------------------------------------------------------------


def _read_all(stream: object) -> bytes:
    try:
        source = getattr(stream, "buffer", None) or stream
        reader = getattr(source, "read", None)
        raw = reader() if callable(reader) else b""
    except Exception:
        return b""
    if isinstance(raw, str):
        return raw.encode("utf-8", "replace")
    return bytes(raw) if isinstance(raw, bytes | bytearray) else b""


def _project_dir(env: dict | os._Environ) -> str | None:
    value = env.get("CLAUDE_PROJECT_DIR")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _inline_hook(raw: bytes, env: dict | os._Environ, event_override: str | None) -> str:
    from openshard.adapters.claude_hooks import handle_claude_hook, parse_hook_payload

    data = parse_hook_payload(raw)
    if data is None:
        return "ignored"
    outcome = handle_claude_hook(data, env=env, event_override=event_override)
    if outcome.action == "error" or env.get("OPENSHARD_HOOK_DEBUG"):
        try:
            sys.stderr.write(f"[openshard hooks] {outcome.event or '?'}: {outcome.action} ({outcome.detail})\n")
        except Exception:
            pass
    return outcome.action


def run_hook_via_service(
    stream: object, *, env: dict | os._Environ | None = None, event_override: str | None = None
) -> str:
    """Console-script body for ``openshard hooks claude``. Never raises, never prints to stdout.

    Returns a short label of what happened: ``forwarded`` (the service
    durably queued it), or the synchronous outcome label when the service
    was unavailable and the payload was handled in-process.
    """
    env = os.environ if env is None else env
    raw = _read_all(stream)
    if not raw.strip():
        return "ignored"
    try:
        if not disabled(env):
            project_dir = _project_dir(env)
            port = resolve_port(env)
            if post_hook(port, raw, project_dir=project_dir, event_override=event_override):
                return "forwarded"
            port_after, _state = ensure_service(env)
            if port_after is not None and post_hook(
                port_after, raw, project_dir=project_dir, event_override=event_override
            ):
                return "forwarded"
    except Exception:
        pass
    return _inline_hook(raw, env, event_override)


def _fallback_status_text(raw: bytes) -> str:
    """Folder name + model display name, mirroring ``claude_hooks._status_line_text``."""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(data, dict):
            return ""
        cwd = data.get("cwd")
        folder = None
        if isinstance(cwd, str) and cwd:
            last = cwd.rstrip("\\/").replace("\\", "/").rsplit("/", 1)[-1]
            # A bare drive ("C:") is not a folder name, exactly as PureWindowsPath.name says.
            folder = last if last and not (len(last) == 2 and last[1] == ":") else None
        model = data.get("model")
        display = model.get("display_name") if isinstance(model, dict) else None
        display = display if isinstance(display, str) and display else None
        return " · ".join(p for p in (folder, display) if p)
    except Exception:
        return ""


def run_status_via_service(stream: object, *, env: dict | os._Environ | None = None) -> str:
    """Console-script body for ``openshard hooks claude-status``: returns the status text.

    The service only *records*; the text Claude Code renders is computed
    here from the payload. When the service is down, a start is attempted
    through the same lock-and-cooldown path as every other caller
    (``maybe_spawn_service`` -- never a bare, unconditional spawn: the status
    line can render far more often than any hook, so an unconditional spawn
    on every failed ping is exactly the spawn-storm this module's spawn
    coordination exists to prevent) and the ping is recorded in-process.
    """
    env = os.environ if env is None else env
    raw = _read_all(stream)
    if not raw.strip():
        return ""
    text = _fallback_status_text(raw)
    try:
        if disabled(env):
            return _inline_status(raw, env, text)
        project_dir = _project_dir(env)
        if post_status(resolve_port(env), raw, project_dir=project_dir):
            return text
        maybe_spawn_service(env)
        return _inline_status(raw, env, text)
    except Exception:
        return text


def _inline_status(raw: bytes, env: dict | os._Environ, fallback: str) -> str:
    try:
        from openshard.adapters.claude_hooks import handle_claude_status, parse_hook_payload

        data = parse_hook_payload(raw)
        if data is None:
            return fallback
        return handle_claude_status(data, env=env)
    except Exception:
        return fallback
