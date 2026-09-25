"""Warm local capture service for Claude Code hooks (PR9.5: near-zero blocking capture).

Why a service
-------------
Claude Code spawns a fresh process for every command-form hook. On this
project's Windows dev machine that costs ~115ms of interpreter + site
start-up (plus ~30ms for the Git Bash Claude Code wraps commands in) before
a single line of OpenShard runs -- and ``Stop`` / ``SessionEnd`` are
synchronous, so every assistant turn paid it, on top of the fold itself
(two ``git`` subprocesses and a whole-file ``runs.jsonl`` rewrite). No
per-hook-process design can reach the p50 < 25ms target, so the hot
events now use Claude Code's official *HTTP hooks* instead: Claude Code
POSTs the very same JSON payload to this service on loopback, and the
service's blocking path is deliberately tiny:

    POST /hooks/claude
      -> decode + validate (session id, event name, repository root)
      -> reduce to the privacy-safe ``ReducedHookPayload``
      -> append one line to <repo>/.openshard/claude_sessions/<sid>.queue.jsonl
         (Codex/OpenCode: <agent>.<sid>.queue.jsonl -- see queue_key)
         (fsync -- durable before the response is sent)
      -> 200 {}

A single background worker then replays each session's queue through the
unchanged ``adapters/claude_hooks.py`` fold logic (``apply_reduced_hook``
/ ``apply_status_payload``): git observation, Shard record creation,
``runs.jsonl`` upserts, receipt fields -- all identical to before, just no
longer on the path Claude Code waits on. Receipts are therefore eventually
consistent, normally within a few hundred milliseconds of the hook.

``SessionStart`` cannot be an HTTP hook (Claude Code skips HTTP hooks for
that event), so it stays a command hook -- and that is exactly the place
that starts this service when it is not running (see
``claude_capture_client.ensure_service``). The status line is a command
too; it forwards here and doubles as a watchdog that restarts the service
mid-session if it ever disappears.

Lifecycle
---------
* One instance per user, bound to ``127.0.0.1`` only. The port is
  ``DEFAULT_PORT``; on a conflict with a foreign listener the next ports
  in ``PORT_RANGE`` are tried and the chosen port is published in the state
  file (``~/.openshard/claude-capture.json``) that clients, ``doctor`` and
  the hook installer read. Losing a bind race to *another OpenShard
  service* is not an error: the loser exits 0.
* Windows binds with ``SO_EXCLUSIVEADDRUSE`` (``allow_reuse_address`` is
  off there) so two instances can never share a port.
* Exits on its own after ``IDLE_TIMEOUT_SECONDS`` without any request, on
  ``POST /shutdown`` (``openshard capture stop`` / ``mcp uninstall``), or on
  SIGTERM/SIGINT. Shutdown drains the queue first; the state file is
  removed only if it still belongs to this instance.
* On start (and on every ``SessionStart``) leftover queue files -- from a
  crash, a kill, or a service that exited mid-drain -- are replayed. Every
  queued line carries a unique id that the session buffer remembers, so a
  replay never double-counts.

Trust boundary (v0.4.4)
-----------------------
Loopback only, **and** authenticated: every ``POST`` must present the
per-user capture token or the repository-scoped capability derived from it
in ``X-OpenShard-Capture-Token`` (``adapters/capture_auth.py``). A request
without a valid credential is answered ``401`` before its body is looked at
and leaves no trace beyond a ``rejected`` counter; a request carrying
browser-only headers (``Origin``/``Referer``/``Sec-Fetch-Site``) is answered
``403``. A capability is scoped to one repository *and* one agent
(``capture_auth.repo_capability``) and is checked against the agent the
receiver path records under. ``POST /shutdown`` accepts the token only,
never a capability. ``GET /health`` is the single unauthenticated endpoint and
returns counters and an informational ``instance_id`` -- nothing that
authorises anything. Nothing is ever *returned* beyond ``{}``, that health
document (no paths), and shutdown acknowledgement. Queue lines hold only
the reduced payload (scrubbed excerpt, repo-relative path, summarized
command), never raw prompts, transcripts or absolute paths.

Declared task context
---------------------
A hook POST may carry the launch context (``OPENSHARD_TASK_ID``) in
``X-OpenShard-Task-Id``. It is validated (exactly one well-formed task id,
else ignored), honoured only for an authorized request, and travels as one
field of the reduced payload -- the agent's own JSON body is never a source
of task context. The fold binds the first valid id per session; see
``claude_hooks._bind_task_context``.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from openshard.adapters import capture_auth as auth
from openshard.adapters import claude_capture_client as client
from openshard.adapters.capture_agents import AGENT_CLAUDE_CODE, profile_for
from openshard.adapters.claude_hooks import (
    EVENT_MODEL_INVOCATION,
    EVENT_SESSION_START,
    HookPayload,
    ReducedHookPayload,
    StatusPayload,
    _snapshot_baseline,
    apply_capture_loss,
    apply_reduced_hook,
    apply_status_payload,
    extract_agent_payload,
    extract_status_payload,
    reduce_hook_payload,
    resolve_repo_root,
    sessions_dir,
)
from openshard.history.capture_completeness import REASON_CORRUPT_QUEUED_EVENT
from openshard.history.task_identity import is_task_id

# PR12: receiver path -> agent key. One service, one queue format, one
# fold; only the translator run on the blocking path differs.
_HOOK_PATH_AGENTS: dict[str, str] = {path: agent for agent, path in client.AGENT_HOOK_PATHS.items()}

DEFAULT_PORT = client.DEFAULT_PORT
PORT_RANGE = client.PORT_RANGE
SERVICE_NAME = client.SERVICE_NAME
QUEUE_SUFFIX = ".queue.jsonl"
# v0.4.4: undecodable queue lines are moved here (bounded copies) instead of
# being skipped; see _replay_file / _quarantine.
QUARANTINE_DIRNAME = "quarantine"
_QUARANTINE_LINE_CAP = 4096  # bytes of one damaged line kept for diagnosis
_QUARANTINE_MAX_FILES = 200
# Refused-request log throttle (see _Handler._reject).
_REJECT_LOG_FIRST = 20
_REJECT_LOG_EVERY = 100
IDLE_TIMEOUT_SECONDS = 4 * 60 * 60
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_RECENT_REPOS = 50
STATE_SCHEMA_VERSION = 1
_TIMING_WINDOW = 256
_ROOT_CACHE_MAX = 256
_IDLE_CHECK_SECONDS = 30.0
# Fixed (not exponential) retry interval for a session whose replay hit a
# transient error -- long enough to ride out a brief AV-scan-style file
# lock, short enough that "slow" stays measured in seconds, not minutes.
# Retried indefinitely: dropping already-durably-queued evidence is the one
# outcome this exists to prevent, so there is no retry cap or give-up.
_RETRY_BACKOFF_SECONDS = 2.0


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def queue_key(session_id: str, agent: str = AGENT_CLAUDE_CODE) -> str:
    """Queue-file stem for one agent session: ``<sid>`` for Claude Code, ``<agent>.<sid>`` otherwise.

    Mirrors ``claude_hooks.buffer_path``: session ids are minted by the
    agents, so two agents can hand the service the same id, and their
    queues (replay order, rotation, crash recovery) must never couple.
    Claude Code keeps the pre-PR12 name so leftover queues survive an
    upgrade. Replay never parses a session id back out of the stem -- the
    queued lines carry their own ``session_id`` and ``agent``.
    """
    if agent == AGENT_CLAUDE_CODE:
        return session_id
    return f"{agent}.{session_id}"


def _log(message: str) -> None:
    try:
        sys.stderr.write(f"{_now()} [capture] {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _version() -> str:
    try:
        import openshard

        return str(openshard.__version__)
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Recorder: blocking append + background replay
# ---------------------------------------------------------------------------


class _Timings:
    def __init__(self, window: int = _TIMING_WINDOW) -> None:
        self._window = window
        self._values: list[float] = []
        self._lock = threading.Lock()
        self.count = 0

    def add(self, seconds: float) -> None:
        with self._lock:
            self.count += 1
            self._values.append(seconds)
            if len(self._values) > self._window:
                del self._values[: len(self._values) - self._window]

    def summary(self) -> dict[str, float | int]:
        with self._lock:
            xs = sorted(self._values)
        if not xs:
            return {"n": 0}
        n = len(xs)

        def pct(p: float) -> float:
            return xs[min(n - 1, max(0, int(round(p * (n - 1)))))]

        return {
            "n": self.count,
            "window": n,
            "last_ms": round(self._values[-1] * 1000, 3) if self._values else 0.0,
            "p50_ms": round(pct(0.5) * 1000, 3),
            "p95_ms": round(pct(0.95) * 1000, 3),
            "max_ms": round(xs[-1] * 1000, 3),
        }


class _ReplayResult:
    """Outcome of replaying one queue file (see ``CaptureRecorder._replay_file``)."""

    __slots__ = ("ok", "corrupt", "session")

    def __init__(self, *, ok: bool) -> None:
        self.ok = ok
        self.corrupt = 0
        self.session: tuple[str, str] | None = None


def _session_from_stem(path: Path) -> tuple[str | None, str]:
    """``(session_id, agent)`` from a queue file name, for a file with no decodable line.

    ``<sid>.queue[.<ns>].jsonl`` is Claude Code; ``<agent>.<sid>.queue...`` the
    others (see ``queue_key``). Only the known agent keys are accepted as a
    prefix, so a session id that itself contains a dot is not mis-split.
    """
    from openshard.adapters.capture_agents import AGENT_PROFILES

    stem = path.name.split(".queue", 1)[0]
    for agent in AGENT_PROFILES:
        if agent != AGENT_CLAUDE_CODE and stem.startswith(agent + "."):
            return stem[len(agent) + 1:] or None, agent
    return (stem or None), AGENT_CLAUDE_CODE


class CaptureRecorder:
    """Durably queue reduced events (blocking path) and replay them (worker)."""

    def __init__(self, *, instance_id: str, state_path: Path | None = None, state: dict | None = None) -> None:
        self.instance_id = instance_id
        self._state_path = state_path
        self._state: dict = dict(state or {})
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._locks_guard = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._root_cache: dict[tuple[str | None, str | None, str], Path | None] = {}
        # Sessions already known to this service (see _opens_session).
        self._known_sessions: set[str] = set()
        self._pending: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, name="openshard-capture-worker", daemon=True)
        self._processing_enabled = threading.Event()
        self._processing_enabled.set()
        # Items enqueued but not yet fully drained -- an atomic counter,
        # not a queue-size check plus a separately-set Event (that pairing
        # had a real TOCTOU window: enqueue() cleared "idle" and then put()
        # the item as two separate steps, so a `wait_idle()` caller could
        # observe an instant between them where the queue was momentarily
        # empty and idle looked True even though real work was about to
        # land). Incremented in enqueue(), decremented only once the worker
        # has actually finished draining that item -- never during the
        # test-only "processing paused, re-queue" branch.
        self._inflight_lock = threading.Lock()
        self._inflight = 0
        # A session whose replay hit a transient error (observed cause: a
        # Windows PermissionError on runs.jsonl, most likely antivirus
        # briefly holding the file open right after the atomic replace) is
        # scheduled for a retry here instead of having its evidence silently
        # discarded -- see _drain_session/_replay_file. Slow folding is
        # acceptable; losing an event that was already durably queued is
        # not. Retries are driven by the worker's own idle-poll cadence
        # (_worker_loop), so no extra thread is needed.
        self._retry_lock = threading.Lock()
        self._retry_after: dict[tuple[str, str], float] = {}
        self._drain_deadline = float("inf")
        self.timings = _Timings()
        self.stats: dict[str, Any] = {
            "received": 0, "queued": 0, "ignored": 0, "replayed": 0, "duplicates": 0,
            "replay_errors": 0, "last_error": None, "last_event": None,
            # v0.4.4: requests refused for lack of a valid credential (nothing
            # recorded), and queued lines that could not be decoded (quarantined).
            "rejected": 0, "corrupt_lines": 0,
            # v0.4.5: per-agent evidence of *accepted* (authenticated + queued)
            # deliveries, keyed by capture agent. This is what lets `doctor` /
            # `capture status` say "OpenCode has actually delivered events"
            # instead of only "the plugin file exists" -- a plugin that never
            # loads (e.g. OpenCode `--pure`, a desktop build that skips project
            # plugins, or a stale plugin) shows zero here even though the file
            # and its capability are perfectly valid. Only ever written after
            # authorization succeeds, so it cannot be spoofed.
            "by_agent": {},
        }
        self._stats_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._worker.start()

    def stop(self, *, drain: bool = True, timeout: float = 30.0) -> None:
        """Stop the worker; with *drain* every already-queued session is replayed first.

        The drain is bounded by *timeout*: a session whose replay keeps
        failing (its retry never succeeds) is left on disk for the next
        service start to recover, rather than keeping this worker alive
        after the caller has stopped waiting for it.
        """
        self._stop.set()
        self._drain_deadline = time.monotonic() + (timeout if drain else 0.0)
        if drain:
            self._processing_enabled.set()
        self._pending.put(None)
        if self._worker.is_alive():
            # A little past the deadline so the worker's final wake-up (it
            # sleeps in short slices while a retry is pending) is covered.
            self._worker.join(timeout=timeout + 1.0 if drain else 0.5)

    def pause_processing(self) -> None:
        """Tests: keep queued lines on disk instead of replaying them."""
        self._processing_enabled.clear()

    def resume_processing(self) -> None:
        self._processing_enabled.set()

    def _is_idle(self) -> bool:
        with self._inflight_lock:
            return self._inflight == 0

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until the worker has nothing in flight (True) or *timeout* elapses."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._is_idle():
                return True
            time.sleep(0.01)
        return self._is_idle()

    @property
    def pending(self) -> int:
        """Items enqueued but not yet fully drained -- includes the one
        currently being processed, unlike a raw queue-size check (which
        would under-report by one while the worker is mid-drain)."""
        with self._inflight_lock:
            return self._inflight

    # -- blocking path -----------------------------------------------------

    def _bump(self, key: str, event: str | None = None) -> None:
        with self._stats_lock:
            self.stats[key] = int(self.stats.get(key) or 0) + 1
            if event:
                self.stats["last_event"] = event

    def _note_agent_received(self, agent: str | None, event: str | None = None) -> None:
        """Record that one authenticated event was accepted for *agent*.

        Called only after authorization passes and the line is queued, so a
        non-zero count is proof the agent's integration is actually delivering
        (not merely that its config file exists). Never raises.
        """
        if not agent:
            return
        with self._stats_lock:
            by_agent = self.stats.get("by_agent")
            if not isinstance(by_agent, dict):
                by_agent = {}
                self.stats["by_agent"] = by_agent
            entry = by_agent.get(agent)
            if not isinstance(entry, dict):
                entry = {"received": 0}
                by_agent[agent] = entry
            entry["received"] = int(entry.get("received") or 0) + 1
            entry["last_at"] = _now()
            if event:
                entry["last_event"] = event

    def snapshot_stats(self) -> dict:
        """A lock-safe copy of ``stats`` for serialization/exposure.

        The nested ``by_agent`` mapping is copied entry-by-entry under the
        stats lock so a concurrent accepted delivery cannot mutate it while it
        is being serialized (the flat counters are cheap ints).
        """
        with self._stats_lock:
            snap = dict(self.stats)
            by_agent = snap.get("by_agent")
            if isinstance(by_agent, dict):
                snap["by_agent"] = {k: dict(v) if isinstance(v, dict) else v for k, v in by_agent.items()}
            return snap

    def _next_id(self) -> str:
        with self._seq_lock:
            self._seq += 1
            return f"{self.instance_id}-{self._seq}"

    def _session_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._session_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[key] = lock
            return lock

    def resolve_root(
        self, project_dir: str | None, cwd: str | None, agent: str = AGENT_CLAUDE_CODE
    ) -> Path | None:
        """Repository root for (*project_dir*, *cwd*), cached -- the same rule as the hook path.

        *agent* matters for an agent whose hooks are configured user-globally
        (Hermes): it resolves only to a repository that has opted in.
        """
        key = (project_dir or None, cwd or None, agent)
        if key in self._root_cache:
            return self._root_cache[key]
        probe = HookPayload(event=EVENT_SESSION_START, session_id=None, cwd=cwd, agent=agent)
        env = {"CLAUDE_PROJECT_DIR": project_dir} if project_dir else {}
        root = resolve_repo_root(probe, env)
        if root is None and profile_for(agent).opt_in_repo:
            # "Not opted in" is not permanent: `openshard capture install
            # hermes` in that repository must take effect without a service
            # restart, so this answer is never cached.
            return None
        if len(self._root_cache) >= _ROOT_CACHE_MAX:
            self._root_cache.clear()
        self._root_cache[key] = root
        return root

    def record_hook(
        self,
        data: dict,
        *,
        project_dir: str | None = None,
        event_override: str | None = None,
        agent: str = AGENT_CLAUDE_CODE,
        authorize: Callable[[Path], bool] | None = None,
        task_id: str | None = None,
    ) -> tuple[str, str]:
        """Validate, reduce and durably queue one hook payload. Returns ``(action, detail)``.

        ``action`` is ``queued``, ``ignored`` or ``rejected``. Never raises
        for bad input; an I/O failure propagates so the HTTP layer can
        answer 500 (Claude Code then reports a non-blocking hook error
        rather than OpenShard silently acknowledging evidence it did not
        persist). *agent* picks the translator (PR12); a translator may
        yield a usage observation (``StatusPayload``), which is queued
        exactly like a status ping. *authorize* (v0.4.4) is consulted with
        the resolved repository root before anything is written; a refusal
        records nothing and returns ``rejected``. *task_id* is the declared
        launch context from the dedicated capture header (never from *data*):
        it is looked at only after authorization, a malformed value is
        dropped (the event is still recorded, unbound), and it rides on the
        reduced payload into the queue line.
        """
        t0 = time.perf_counter()
        self._bump("received")
        payload = extract_agent_payload(data, agent=agent, event_override=event_override)
        if payload is None:
            self._bump("ignored")
            return "ignored", "unsupported or missing hook_event_name"
        if payload.session_id is None:
            self._bump("ignored")
            return "ignored", "missing or invalid session_id"
        root = self.resolve_root(project_dir, payload.cwd, payload.agent)
        if root is None:
            self._bump("ignored")
            return "ignored", "could not resolve repository directory"
        if authorize is not None and not authorize(root):
            self._bump("rejected")
            return "rejected", "unauthenticated"
        if isinstance(payload, StatusPayload):
            key = queue_key(payload.session_id, payload.agent)
            line = {"id": self._next_id(), "kind": "status", "at": _now(), "data": payload.to_dict()}
            self._queue_line(root, key, line)
            self._bump("queued", "status")
            self._note_agent_received(payload.agent, "status")
            self.timings.add(time.perf_counter() - t0)
            self.enqueue(root, key)
            return "queued", "status"
        payload.task_id = task_id if is_task_id(task_id) else None
        reduced = reduce_hook_payload(payload, root)
        if reduced is None:
            self._bump("ignored")
            return "ignored", "missing or invalid session_id"
        key = queue_key(reduced.session_id, reduced.agent)
        opens_session = self._opens_session(root, key, payload.event)
        if opens_session:
            # v0.4.4: anchor change attribution at the moment the session was
            # observed, not at replay time (the worker may lag behind the
            # agent's first edits). SessionStart is a command hook for every
            # agent that has one, so this one-off git call is off the hot path.
            # An agent without a start hook (Antigravity) opens its session
            # with its first model invocation instead -- once per session.
            reduced.baseline = _snapshot_baseline(root, _now())
        line = {"id": self._next_id(), "kind": "hook", "at": _now(), "data": reduced.to_dict()}
        self._queue_line(root, key, line)
        self._bump("queued", payload.event)
        self._note_agent_received(reduced.agent, payload.event)
        self.timings.add(time.perf_counter() - t0)
        self.enqueue(root, key)
        if opens_session:
            self._note_repo(root)
            self.recover(root)
        return "queued", payload.event

    def _opens_session(self, root: Path, key: str, event: str) -> bool:
        """Whether this event starts a session the service must anchor now.

        Always for ``SessionStart``. For a ``ModelInvocation`` (an agent with
        no start hook) only the first one of a session: neither a staging
        buffer nor a queue file exists for it yet. Remembered in memory so
        later invocations -- one per model call -- pay no filesystem check.
        """
        if event == EVENT_SESSION_START:
            return True
        if event != EVENT_MODEL_INVOCATION:
            return False
        marker = f"{root}|{key}"
        if marker in self._known_sessions:
            return False
        if len(self._known_sessions) >= _ROOT_CACHE_MAX * 4:
            self._known_sessions.clear()
        self._known_sessions.add(marker)
        directory = sessions_dir(root)
        try:
            if (directory / f"{key}.json").exists() or (directory / f"{key}{QUEUE_SUFFIX}").exists():
                return False
            return not any(directory.glob(f"{key}.queue.*.jsonl"))
        except OSError:
            return False

    def record_status(
        self,
        data: dict,
        *,
        project_dir: str | None = None,
        authorize: Callable[[Path], bool] | None = None,
    ) -> tuple[str, str]:
        t0 = time.perf_counter()
        self._bump("received")
        payload = extract_status_payload(data)
        if payload is None or payload.session_id is None:
            self._bump("ignored")
            return "ignored", "missing or invalid session_id"
        root = self.resolve_root(project_dir, payload.cwd)
        if root is None:
            self._bump("ignored")
            return "ignored", "could not resolve repository directory"
        if authorize is not None and not authorize(root):
            self._bump("rejected")
            return "rejected", "unauthenticated"
        key = queue_key(payload.session_id, payload.agent)
        line = {"id": self._next_id(), "kind": "status", "at": _now(), "data": payload.to_dict()}
        self._queue_line(root, key, line)
        self._bump("queued", "status")
        self._note_agent_received(payload.agent, "status")
        self.timings.add(time.perf_counter() - t0)
        self.enqueue(root, key)
        return "queued", "status"

    def _queue_line(self, root: Path, queue_key: str, line: dict) -> None:
        path = sessions_dir(root) / f"{queue_key}{QUEUE_SUFFIX}"
        blob = (json.dumps(line, separators=(",", ":")) + "\n").encode("utf-8")
        with self._session_lock(f"{root}|{queue_key}"):
            try:
                fh = open(path, "ab")
            except FileNotFoundError:
                path.parent.mkdir(parents=True, exist_ok=True)
                fh = open(path, "ab")
            with fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())

    # -- state file --------------------------------------------------------

    def _note_repo(self, root: Path) -> None:
        if self._state_path is None:
            return
        try:
            repos = [r for r in self._state.get("recent_repos", []) if isinstance(r, str)]
            entry = str(root)
            if repos and repos[0] == entry:
                return
            repos = [entry] + [r for r in repos if r != entry]
            self._state["recent_repos"] = repos[:MAX_RECENT_REPOS]
            _write_state(self._state_path, self._state)
        except Exception as exc:
            _log(f"could not update state file: {type(exc).__name__}")

    # -- background --------------------------------------------------------

    def enqueue(self, root: Path, session_id: str) -> None:
        # Count this item as in-flight *before* it becomes visible to the
        # worker (queue.Queue.put), so a concurrent wait_idle()/pending
        # check can never observe "nothing in flight" for an item that is
        # about to be processed.
        with self._inflight_lock:
            self._inflight += 1
        self._pending.put((str(root), session_id))

    def recover(self, root: Path) -> int:
        """Queue every leftover queue file under *root* for replay. Returns sessions found."""
        found: set[str] = set()
        try:
            directory = sessions_dir(root)
            if not directory.is_dir():
                return 0
            for path in directory.glob(f"*{QUEUE_SUFFIX}"):
                found.add(path.name[: -len(QUEUE_SUFFIX)])
            for path in directory.glob("*.queue.*.jsonl"):
                found.add(path.name.split(".queue.", 1)[0])
        except OSError:
            return 0
        for sid in sorted(found):
            self.enqueue(root, sid)
        return len(found)

    def known_repos(self) -> list[Path]:
        """Repository roots this service has seen (most recent first). Never raises."""
        try:
            return [Path(r) for r in self._state.get("recent_repos", []) or [] if isinstance(r, str)]
        except Exception:
            return []

    def recover_known_repos(self) -> int:
        total = 0
        for raw in self._state.get("recent_repos", []) or []:
            if isinstance(raw, str):
                try:
                    total += self.recover(Path(raw))
                except Exception:
                    continue
        return total

    def _schedule_retry(self, root: Path, session_id: str) -> None:
        with self._retry_lock:
            self._retry_after[(str(root), session_id)] = time.monotonic() + _RETRY_BACKOFF_SECONDS

    def _due_retries(self) -> list[tuple[str, str]]:
        now = time.monotonic()
        due: list[tuple[str, str]] = []
        with self._retry_lock:
            for key, when in list(self._retry_after.items()):
                if now >= when:
                    due.append(key)
                    del self._retry_after[key]
        return due

    def _worker_loop(self) -> None:
        while True:
            try:
                item = self._pending.get(timeout=0.5)
            except queue.Empty:
                for root_str, sid in self._due_retries():
                    self.enqueue(Path(root_str), sid)
                if self._stop.is_set():
                    return
                continue
            if item is None:
                with self._retry_lock:
                    retries_pending = bool(self._retry_after)
                    next_due = min(self._retry_after.values(), default=None)
                if self._stop.is_set() and self._pending.empty() and not retries_pending:
                    return
                if retries_pending:
                    # A drain (stop()) is in progress with a retry still
                    # scheduled. Give the retry its chance rather than
                    # abandoning already-queued evidence -- but never by
                    # spinning: re-reading our own sentinel skips the
                    # queue.Empty branch above, so the due-retry check has
                    # to happen here, and the wait until it is due has to
                    # be a real sleep. Past the drain deadline the files
                    # stay on disk for the next service start to recover.
                    if self._stop.is_set() and time.monotonic() >= self._drain_deadline:
                        return
                    for root_str, sid in self._due_retries():
                        self.enqueue(Path(root_str), sid)
                    if next_due is not None:
                        now = time.monotonic()
                        time.sleep(max(0.0, min(next_due - now, self._drain_deadline - now, 0.2)))
                    self._pending.put(None)
                continue
            if not self._processing_enabled.is_set():
                # Tests only: leave the work queued until resumed -- not yet
                # drained, so _inflight must not be decremented for this pass.
                self._pending.put(item)
                self._processing_enabled.wait(timeout=0.2)
                continue
            root, sid = item
            try:
                self._drain_session(Path(root), sid)
            except Exception as exc:
                with self._stats_lock:
                    self.stats["replay_errors"] = int(self.stats.get("replay_errors") or 0) + 1
                    self.stats["last_error"] = f"{type(exc).__name__}"
                _log(f"replay failed for session {sid[:8]}...: {type(exc).__name__}")
            finally:
                with self._inflight_lock:
                    self._inflight = max(0, self._inflight - 1)

    def _drain_session(self, root: Path, session_id: str) -> None:
        directory = sessions_dir(root)
        live = directory / f"{session_id}{QUEUE_SUFFIX}"
        with self._session_lock(f"{root}|{session_id}"):
            try:
                if live.exists() and live.stat().st_size > 0:
                    rotated = directory / f"{session_id}.queue.{time.time_ns()}.jsonl"
                    os.replace(live, rotated)
                elif live.exists():
                    live.unlink()
            except OSError:
                pass
        try:
            files = sorted(directory.glob(f"{session_id}.queue.*.jsonl"))
        except OSError:
            return
        needs_retry = False
        for path in files:
            # A file is only ever removed once every line in it has been
            # durably applied, is a confirmed duplicate, or has been
            # quarantined as undecodable (v0.4.4: never silently skipped --
            # see _replay_file). A transient failure (observed cause: a
            # Windows PermissionError from antivirus briefly holding
            # runs.jsonl open right after the atomic replace) must never make
            # already durably-queued evidence disappear -- the file is left in
            # place and this session is retried after a short backoff (see
            # _schedule_retry) instead of being unlinked unconditionally.
            result = self._replay_file(root, path)
            if result.ok:
                try:
                    path.unlink()
                except OSError:
                    needs_retry = True
            else:
                needs_retry = True
            if result.corrupt and result.ok:
                # Only once the file is fully settled (so a retried file does
                # not count its damage twice): make the loss visible on the
                # session's record. The session comes from a valid neighbour
                # line when there is one, else from the queue-file stem.
                sid, agent = result.session or _session_from_stem(path)
                if sid:
                    apply_capture_loss(
                        sid, root, kind=REASON_CORRUPT_QUEUED_EVENT, count=result.corrupt, agent=agent,
                    )
        if needs_retry:
            self._schedule_retry(root, session_id)

    def _replay_file(self, root: Path, path: Path) -> _ReplayResult:
        """Apply every line in *path*.

        ``ok`` is True only when no line hit a *transient* failure (those keep
        the file for retry). An undecodable or structurally unusable line is
        not transient: it is copied to the quarantine directory, counted in
        ``corrupt_lines`` and reported in ``corrupt`` so the caller can mark
        the session's capture incomplete. Valid neighbours are still applied.
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return _ReplayResult(ok=False)
        result = _ReplayResult(ok=True)
        for lineno, raw in enumerate(text.splitlines(), start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                self._quarantine(root, path, lineno, raw, "not valid JSON")
                result.corrupt += 1
                continue
            if not isinstance(line, dict) or not isinstance(line.get("data"), dict):
                self._quarantine(root, path, lineno, raw, "not a queue line object")
                result.corrupt += 1
                continue
            dedup_id = line.get("id") if isinstance(line.get("id"), str) else None
            at = line.get("at") if isinstance(line.get("at"), str) else None
            kind = line.get("kind")
            data = line["data"]
            if kind == "hook":
                reduced = ReducedHookPayload.from_dict(data)
                if reduced is None:
                    self._quarantine(root, path, lineno, raw, "unusable hook payload")
                    result.corrupt += 1
                    continue
                result.session = (reduced.session_id, reduced.agent)
                outcome = apply_reduced_hook(reduced, root, dedup_id=dedup_id, at=at)
                if outcome.action == "error":
                    with self._stats_lock:
                        self.stats["replay_errors"] = int(self.stats.get("replay_errors") or 0) + 1
                        self.stats["last_error"] = outcome.detail
                    result.ok = False
                elif outcome.detail == "duplicate event id":
                    self._bump("duplicates")
                else:
                    self._bump("replayed")
            elif kind == "status":
                # Best-effort, non-critical metadata (documented throughout
                # this capture path: the status line only ever supplements
                # hook evidence, never replaces it) -- a failure here is not
                # worth the same retry-forever treatment as a hook event.
                status = StatusPayload.from_dict(data)
                if status is None:
                    self._quarantine(root, path, lineno, raw, "unusable status payload")
                    result.corrupt += 1
                    continue
                if result.session is None and isinstance(status.session_id, str):
                    result.session = (status.session_id, status.agent)
                apply_status_payload(status, root, dedup_id=dedup_id, at=at)
                self._bump("replayed")
            else:
                self._quarantine(root, path, lineno, raw, "unknown line kind")
                result.corrupt += 1
        return result

    def _quarantine(self, root: Path, source: Path, lineno: int, raw: str, reason: str) -> None:
        """Preserve one undecodable queue line for diagnosis. Never raises.

        The copy is bounded (``_QUARANTINE_LINE_CAP`` bytes) and lives next
        to the queues, under ``.openshard/claude_sessions/quarantine/``. A
        queue line only ever holds reduced material (see module docstring),
        so a damaged one holds at most a damaged copy of that; no raw prompt,
        transcript or absolute path is introduced here, and the header names
        the queue file by its repo-relative name only.
        """
        self._bump("corrupt_lines")
        try:
            directory = sessions_dir(root) / QUARANTINE_DIRNAME
            directory.mkdir(parents=True, exist_ok=True)
            existing = sorted(directory.glob("*.jsonl"))
            if len(existing) >= _QUARANTINE_MAX_FILES:
                _log("quarantine directory is full; an undecodable queue line was counted but not kept")
                return
            target = directory / f"{source.name}.quarantine.jsonl"
            record = {
                "quarantined_at": _now(),
                "queue_file": source.name,
                "line": lineno,
                "reason": reason,
                "truncated": len(raw.encode("utf-8", "replace")) > _QUARANTINE_LINE_CAP,
                "raw": raw.encode("utf-8", "replace")[:_QUARANTINE_LINE_CAP].decode("utf-8", "replace"),
            }
            with target.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            _log(f"could not quarantine an undecodable queue line: {type(exc).__name__}")


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class CaptureServer(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR on Windows lets a second listener bind the same port,
    # which is exactly the double-instance failure this service must never
    # have; POSIX semantics of the flag are safe (and avoid TIME_WAIT stalls).
    allow_reuse_address = sys.platform != "win32"

    def __init__(
        self, port: int, recorder: CaptureRecorder, *, instance_id: str, started_at: str,
        env: dict | os._Environ | None = None,
    ) -> None:
        self.recorder = recorder
        self.instance_id = instance_id
        self.started_at = started_at
        # Where the capture token lives (OPENSHARD_HOME). Read per request so
        # a rotation takes effect without a restart.
        self.env: dict | os._Environ = os.environ if env is None else env
        self.started_monotonic = time.monotonic()
        self.last_request_monotonic = time.monotonic()
        self.shutdown_requested = threading.Event()
        self.shutdown_reason = ""
        super().__init__(("127.0.0.1", port), _Handler)

    def server_bind(self) -> None:
        if sys.platform == "win32":
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def touch(self) -> None:
        self.last_request_monotonic = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_request_monotonic

    def begin_shutdown(self, reason: str) -> None:
        if self.shutdown_requested.is_set():
            return
        self.shutdown_reason = reason
        self.shutdown_requested.set()
        threading.Thread(target=self.shutdown, name="openshard-capture-shutdown", daemon=True).start()

    def health_document(self) -> dict:
        stats = self.recorder.snapshot_stats()
        return {
            "ok": True,
            "service": SERVICE_NAME,
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "port": self.port,
            "version": _version(),
            "started_at": self.started_at,
            "uptime_seconds": round(time.monotonic() - self.started_monotonic, 1),
            "pending": self.recorder.pending,
            "stats": stats,
            "blocking_ms": self.recorder.timings.summary(),
        }


class _Handler(BaseHTTPRequestHandler):
    server: CaptureServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _path_and_query(self) -> tuple[str, dict[str, str]]:
        path, _, query = self.path.partition("?")
        params: dict[str, str] = {}
        for part in query.split("&"):
            if "=" in part:
                key, value = part.split("=", 1)
                params[key] = value
        return path, params

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self.server.touch()
        path, _ = self._path_and_query()
        if path == client.HEALTH_PATH:
            self._send(200, json.dumps(self.server.health_document()).encode("utf-8"))
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        self.server.touch()
        path, params = self._path_and_query()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(413, b'{"error":"payload too large"}')
            return
        body = self.rfile.read(length) if length else b""
        if path not in _HOOK_PATH_AGENTS and path != client.STATUS_PATH and path != client.SHUTDOWN_PATH:
            self._send(404, b'{"error":"not found"}')
            return
        # v0.4.4 authentication -- before the body is looked at. Browser
        # contexts are refused outright (defence in depth); everything else
        # needs a syntactically plausible credential, checked for real once
        # the repository root is known (record_hook/record_status) so a
        # repository-scoped capability can be matched against the right root.
        if auth.has_browser_headers(self.headers):
            self._reject(path, "browser headers present")
            self._send(403, b'{"error":"browser origin refused"}')
            return
        presented = self.headers.get(auth.TOKEN_HEADER)
        token = auth.load_token(self.server.env)
        if token is None:
            # No token on disk: nothing can be authorised. The hook clients and
            # `capture start` create it; until then every POST is refused.
            self._reject(path, "no capture token on disk")
            self._send(503, b'{"error":"capture token unavailable"}')
            return
        if not auth.looks_like_credential(presented):
            self._reject(path, "no credential presented" if not presented else "malformed credential")
            self._send(401, b'{"error":"unauthenticated"}')
            return
        if path == client.SHUTDOWN_PATH:
            self._handle_shutdown(body, presented, token)
            return
        if self.server.shutdown_requested.is_set():
            self._send(503, b'{"error":"shutting down"}')
            return
        try:
            data = json.loads(body.decode("utf-8", "replace")) if body.strip() else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            # Malformed input is ignored, never surfaced to the user as a hook
            # error -- exactly like the command-form entrypoint.
            self._send(200, b"{}")
            return
        project_dir = self.headers.get(client.PROJECT_DIR_HEADER)
        project_dir = project_dir.strip() if isinstance(project_dir, str) and project_dir.strip() else None
        task_id = self._declared_task_id()

        # The agent a capability must be scoped to is the one this event will
        # be *recorded as* -- the receiver path picks the translator and the
        # record's agent, so a capability minted for another integration
        # (same repository or not) never authorises this request.
        agent_for_auth = _HOOK_PATH_AGENTS.get(path, AGENT_CLAUDE_CODE)

        def authorize(root: Path) -> bool:
            return auth.verify_presented(presented, token, root, agent_for_auth) is not None

        try:
            if path in _HOOK_PATH_AGENTS:
                event_override = params.get("event") or None
                action, _detail = self.server.recorder.record_hook(
                    data, project_dir=project_dir, event_override=event_override, agent=_HOOK_PATH_AGENTS[path],
                    authorize=authorize, task_id=task_id,
                )
            else:
                action, _detail = self.server.recorder.record_status(
                    data, project_dir=project_dir, authorize=authorize,
                )
        except Exception as exc:
            _log(f"record failed: {type(exc).__name__}")
            self._send(500, b'{"error":"record failed"}')
            return
        if action == "rejected":
            self._reject(path, "credential does not match the token or this repository", counted=True)
            self._send(401, b'{"error":"unauthenticated"}')
            return
        self._send(200, b"{}")

    def _declared_task_id(self) -> str | None:
        """The declared task id on this request's ``TASK_ID_HEADER``, or None.

        Exactly one well-formed header value counts. Absent, empty (Claude
        Code's interpolation of an unset ``$OPENSHARD_TASK_ID``), repeated,
        padded, malformed or literal-``$VAR`` values all read as "no
        declaration" -- nothing is repaired, and nothing is ever guessed.
        Only consulted for hook POSTs, and only handed on after
        authorization (see ``CaptureRecorder.record_hook``).
        """
        values = self.headers.get_all(client.TASK_ID_HEADER) or []
        if len(values) != 1:
            return None
        return values[0] if is_task_id(values[0]) else None

    def _reject(self, path: str, reason: str, *, counted: bool = False) -> None:
        """Count a refused request and log why (path and reason only; never the credential).

        Logging is throttled so a misbehaving local process cannot fill the
        service log: the first ``_REJECT_LOG_FIRST`` refusals are logged, then
        one in every ``_REJECT_LOG_EVERY``.
        """
        if not counted:
            self.server.recorder._bump("rejected")
        n = int(self.server.recorder.stats.get("rejected") or 0)
        if n <= _REJECT_LOG_FIRST or n % _REJECT_LOG_EVERY == 0:
            _log(f"refused POST {path}: {reason} (refused so far: {n})")

    def _handle_shutdown(self, body: bytes, presented: object, token: str) -> None:
        # Shutdown is a control action: only the capture token itself may
        # authorise it, never a repository capability and never anything
        # readable from /health.
        if auth.verify_presented(presented, token, None) != "token":
            self._reject(path=client.SHUTDOWN_PATH, reason="shutdown needs the capture token")
            self._send(401, b'{"error":"unauthenticated"}')
            return
        try:
            data = json.loads(body.decode("utf-8", "replace")) if body.strip() else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict) or data.get("instance_id") != self.server.instance_id:
            self._send(403, b'{"error":"instance id mismatch"}')
            return
        self._send(200, b'{"ok":true}')
        self.server.begin_shutdown("shutdown requested")


# ---------------------------------------------------------------------------
# Serve / status / stop
# ---------------------------------------------------------------------------


def _candidate_ports(env: dict | os._Environ, explicit: int | None) -> list[int]:
    if explicit is None:
        explicit = client.pinned_port(env)
    if explicit is not None:
        return [explicit]
    ports: list[int] = []
    state = client.read_state(env)
    stated = state.get("port") if state else None
    if isinstance(stated, int) and 0 < stated < 65536:
        ports.append(stated)
    for p in range(DEFAULT_PORT, DEFAULT_PORT + PORT_RANGE):
        if p not in ports:
            ports.append(p)
    return ports


_FOREIGN_PORT_GRACE_SECONDS = 2.0


def _wait_for_owner_health(port: int, *, timeout: float) -> bool:
    """True if an OpenShard service answers on *port* within *timeout*.

    A sibling that has just bound the port is briefly unable to answer
    ``/health`` until its own ``serve_forever`` loop starts (see ``serve``,
    which now starts that loop before anything else) -- without this grace
    window, a racing process could see one failed health probe, wrongly
    conclude the port belongs to an unrelated foreign program, and bind the
    *next* port instead, leaving two live services. Bounded, not a retry
    loop: gives up after *timeout* seconds either way.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if client.health(port) is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _bind(env: dict | os._Environ, explicit: int | None, recorder: CaptureRecorder,
          instance_id: str, started_at: str) -> CaptureServer | None:
    for port in _candidate_ports(env, explicit):
        try:
            return CaptureServer(port, recorder, instance_id=instance_id, started_at=started_at, env=env)
        except OSError:
            if _wait_for_owner_health(port, timeout=_FOREIGN_PORT_GRACE_SECONDS):
                _log(f"another OpenShard capture service already listens on {port}; exiting")
                return None
            _log(f"port {port} is in use by another program; trying the next one")
            continue
    return None


def _telemetry_service_event(env: dict | os._Environ, state: str, recorder: CaptureRecorder, server: CaptureServer) -> None:
    """``capture.service`` telemetry: lifecycle state plus this service's own counters. Never raises."""
    try:
        from openshard.telemetry import emit
        from openshard.telemetry.client import flush

        stats = dict(recorder.stats)
        timing = server.health_document().get("blocking_ms") or {}

        def _n(value: object) -> int:
            return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0

        emit(
            "capture.service", env,
            state=state, queued=_n(stats.get("queued")), folded=_n(stats.get("replayed")),
            replay_errors=_n(stats.get("replay_errors")),
            rejected=_n(stats.get("rejected")), corrupt_lines=_n(stats.get("corrupt_lines")),
            p50_ms=_n(timing.get("p50_ms")), p95_ms=_n(timing.get("p95_ms")),
        )
        if state != "started":
            flush(env=env)
    except Exception:
        pass


def serve(
    *,
    port: int | None = None,
    idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    env: dict | os._Environ | None = None,
    ready: threading.Event | None = None,
    server_box: list | None = None,
) -> int:
    """Run the capture service until shutdown. Returns the process exit code.

    *ready* (tests) is set once the server is bound; *server_box* (tests)
    receives the live ``CaptureServer`` so it can be stopped in-process.
    """
    env = os.environ if env is None else env
    home = Path(client.capture_home(env))
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log(f"cannot create {home}: {type(exc).__name__}")
        return 1
    state_path = Path(client.state_path(env))
    if auth.ensure_token(env) is None:
        # Without a credential nothing could ever be accepted; say so once
        # (the value itself is never logged) and keep going -- clients and
        # `capture start` retry creating it, and requests are refused until then.
        _log("could not create the capture token file; requests will be refused until it exists")
    instance_id = uuid.uuid4().hex[:12]
    started_at = _now()
    previous = client.read_state(env) or {}
    recorder = CaptureRecorder(
        instance_id=instance_id, state_path=state_path,
        state={"recent_repos": previous.get("recent_repos", [])},
    )
    server = _bind(env, port, recorder, instance_id, started_at)
    if server is None:
        if ready is not None:
            ready.set()
        return 0 if client.health(client.resolve_port(env)) is not None else 1

    # Start actually serving *before* anything else (state publication,
    # crash recovery, which can run real git calls and take a while): the
    # moment this port stops accepting new binds, it must also start
    # answering /health, or a racing sibling's _wait_for_owner_health grace
    # window can time out and wrongly conclude no OpenShard service owns
    # this port (see _bind). accept() on the listening socket already
    # succeeds as soon as CaptureServer.__init__ returns; running the serve
    # loop in its own thread from this point closes the gap between "port
    # taken" and "request answered" to essentially nothing.
    serve_thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.25},
        name="openshard-capture-http", daemon=True,
    )
    serve_thread.start()

    recorder._state.update({
        "schema_version": STATE_SCHEMA_VERSION,
        "service": SERVICE_NAME,
        "port": server.port,
        "pid": os.getpid(),
        "instance_id": instance_id,
        "started_at": started_at,
        "python": sys.executable,
        "version": _version(),
    })
    try:
        _write_state(state_path, recorder._state)
    except OSError as exc:
        _log(f"cannot write state file: {type(exc).__name__}")
        server.begin_shutdown("state write failed")
        serve_thread.join(timeout=10)
        server.server_close()
        return 1
    _log(f"listening on 127.0.0.1:{server.port} (pid {os.getpid()}, instance {instance_id})")
    if server_box is not None:
        server_box.append(server)

    # The worker thread is CPU-busy while folding; a short switch interval
    # keeps request threads (the blocking path) responsive under the GIL.
    # It is process-wide, so it is restored on exit: an in-process service
    # (tests) must not leave the interpreter on a 1 ms interval.
    previous_switch = sys.getswitchinterval()
    try:
        sys.setswitchinterval(0.001)
    except (ValueError, AttributeError):
        pass
    recorder.start()
    recovered = recorder.recover_known_repos()
    if recovered:
        _log(f"recovering {recovered} session queue(s) left behind")

    # Telemetry (0.4.2): the service is the natural long-running flusher for
    # the local event queue, and reports its own lifecycle. Both run off the
    # blocking path and never raise.
    _telemetry_service_event(env, "started", recorder, server)
    try:
        from openshard.telemetry.client import flush_periodically

        threading.Thread(
            target=flush_periodically, args=(server.shutdown_requested,), kwargs={"env": env},
            name="openshard-telemetry-flush-timer", daemon=True,
        ).start()
    except Exception:
        pass
    # Platform sync: the same long-running process flushes each known
    # repository's unsynced receipts on a timer. A no-op (one small file
    # read per tick) until `openshard sync connect` has stored a link.
    try:
        from openshard.sync.client import sync_periodically

        threading.Thread(
            target=sync_periodically, args=(server.shutdown_requested,),
            kwargs={"env": env, "repos": recorder.known_repos},
            name="openshard-platform-sync-timer", daemon=True,
        ).start()
    except Exception:
        pass

    def _idle_watch() -> None:
        while not server.shutdown_requested.wait(_IDLE_CHECK_SECONDS):
            if idle_timeout > 0 and server.idle_seconds() >= idle_timeout and recorder.pending == 0:
                server.begin_shutdown(f"idle for {int(server.idle_seconds())}s")
                return

    threading.Thread(target=_idle_watch, name="openshard-capture-idle", daemon=True).start()

    def _on_signal(signum: int, _frame: object) -> None:
        server.begin_shutdown(f"signal {signum}")

    if threading.current_thread() is threading.main_thread():
        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None),
                    getattr(signal, "SIGBREAK", None)):
            if sig is not None:
                try:
                    signal.signal(sig, _on_signal)
                except (ValueError, OSError):
                    pass
    if ready is not None:
        ready.set()
    try:
        server.shutdown_requested.wait()
    finally:
        _log(f"stopping ({server.shutdown_reason or 'shutdown requested'}); draining queue")
        recorder.stop(drain=True)
        _telemetry_service_event(
            env, "idle_exit" if str(server.shutdown_reason or "").startswith("idle") else "stopped",
            recorder, server,
        )
        # Stdlib-recommended order: join the thread running serve_forever()
        # (shutdown() was already triggered by begin_shutdown()) before
        # closing the socket out from under it.
        serve_thread.join(timeout=10)
        server.server_close()
        try:
            sys.setswitchinterval(previous_switch)
        except (ValueError, AttributeError):
            pass
        current = client.read_state(env)
        if current and current.get("instance_id") == instance_id:
            try:
                state_path.unlink()
            except OSError:
                pass
        _log("stopped")
    return 0


def service_status(env: dict | os._Environ | None = None) -> dict:
    """Read-only status for ``doctor`` / ``capture status``. Never raises."""
    env = os.environ if env is None else env
    state = client.read_state(env)
    port = client.resolve_port(env)
    doc = client.health(port)
    stale_state = bool(state) and doc is None and not _pid_alive((state or {}).get("pid"))
    result: dict = {
        "running": doc is not None,
        "port": port,
        "state_file": client.state_path(env),
        "stale_state": stale_state,
        "default_port": DEFAULT_PORT,
    }
    if doc is not None:
        result.update({
            "pid": doc.get("pid"),
            "instance_id": doc.get("instance_id"),
            "version": doc.get("version"),
            "uptime_seconds": doc.get("uptime_seconds"),
            "pending": doc.get("pending"),
            "stats": doc.get("stats"),
            "blocking_ms": doc.get("blocking_ms"),
        })
    elif state:
        result["pid"] = state.get("pid")
    return result


def stop_service(env: dict | os._Environ | None = None, *, wait_seconds: float = 30.0) -> dict:
    """Ask the service to drain and exit; clean a stale state file. Never raises.

    *wait_seconds* bounds how long to wait for the drain (a fold in flight
    can take a few hundred milliseconds; much longer only on a loaded box).
    """
    env = os.environ if env is None else env
    before = service_status(env)
    stopped = client.request_shutdown(env, wait_seconds=wait_seconds) if before.get("running") else True
    if stopped:
        state = client.read_state(env)
        if state and not _pid_alive(state.get("pid")):
            try:
                os.unlink(client.state_path(env))
            except OSError:
                pass
    return {"was_running": bool(before.get("running")), "stopped": stopped, "port": before.get("port")}
