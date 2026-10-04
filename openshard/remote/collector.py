"""The remote-capture collector: spool on the hook path, send off it.

Three entry points:

``record``   called by the hook fold (``adapters/claude_hooks.py``) with the
             Events a hook just produced. Appends them to the local spool
             and returns; it never touches the network. A no-op unless this
             runtime is attached to a remote capture.
``flush``    sends what the spool holds, in batches, acknowledging each only
             after the Platform accepted it; then, when a session has ended
             or a verification was recorded, delivers the Receipt and its
             verification evidence through the capture.
``notify``   asks for a flush soon without waiting for it: wakes the capture
             service's flusher thread when there is one, else starts a
             detached one-shot flusher (throttled, one at a time).

Streaming, not end-of-run upload. The runtime may be destroyed at any
moment and no provider guarantees a session-end event, so Events leave a few
seconds after they happen. The end of the session is just one more Event:
if it never arrives, the Platform says so.

Failure handling, all local to this machine:

* network / 429 / 5xx: exponential backoff (5 s doubling to 5 min); the spool
  keeps growing and drains when the Platform is back;
* 400 / 422 for a batch: the batch is resent one Event at a time and only
  the Events the Platform refuses are dropped (counted as ``rejected``), so
  one bad Event cannot hold the journal back;
* 401: the token is dead (expired or revoked): stop, keep the spool, say so;
* 409 journal full: stop sending Events.

Nothing here raises into a hook, and nothing here logs the token.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openshard.connected.config import (
    ConnectedSession,
    resolve_connection,
    session_from_entry,
)
from openshard.connected.config import sink_id as connected_sink_id
from openshard.remote import spool
from openshard.remote.config import resolve_attachment

CONTRACT = "openshard.remote-capture"
CONTRACT_VERSION = "1"
SOURCE_PRODUCT = "openshard-core"

FLUSH_INTERVAL_SECONDS = 5.0  # how soon after an Event a flush is attempted, and the least time between flushers
HEARTBEAT_SECONDS = 60.0  # while a flusher is alive and idle, how often it tells the Platform so
_BACKOFF_MIN_SECONDS = 5.0
_BACKOFF_MAX_SECONDS = 300.0
_MAX_BATCHES_PER_FLUSH = 60
_FLUSH_LOCK_TIMEOUT = 0.2

STOP_UNAUTHORIZED = "unauthorized"  # the token is expired or revoked
STOP_FULL = "journal_full"

_wake = threading.Event()
_background_active = False


def _version() -> str:
    try:
        from openshard import __version__

        return str(__version__)
    except Exception:
        return "unknown"


@dataclass
class RemoteFlushReport:
    attached: bool = False
    batches: int = 0
    events_sent: int = 0
    events_rejected: int = 0
    pending: int = 0
    heartbeat: bool = False
    receipts: dict[str, Any] | None = None
    stopped: str | None = None
    repos: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Hook path: spool only
# ---------------------------------------------------------------------------


def record(
    repo_root: Path,
    events: list[dict],
    *,
    record: dict | None = None,
    entry: dict | None = None,
    finalized: bool = False,
    env: dict | os._Environ | None = None,
) -> int:
    """Spool the Events a hook just produced. Returns how many were spooled (0 when not attached). Never raises.

    *record* is the session's identity (``receipt_id`` / ``shard_id`` /
    ``run_id``) once minted; *entry* the folded record when this hook
    folded one, whose git-observed file Events are spooled once each;
    *finalized* that the session's Receipt is now complete and should be
    delivered.
    """
    try:
        attachment = resolve_attachment(env)
        connection = None
        connected = None
        if attachment is not None:
            capture_id = attachment.capture_id
        else:
            connection = resolve_connection(env)
            connected = session_from_entry(entry, record, env=env)
            if connection is None or connected is None:
                return 0
            capture_id = connected_sink_id(connection, connected)
        file_events: list[dict] = []
        if isinstance(entry, dict):
            file_events = [
                e for e in (entry.get("events") or [])
                if isinstance(e, dict) and e.get("event_type") == "file.changed"
            ]
        env = spool.session_env(env, capture_id)
        count = spool.append(
            env, capture_id, [e for e in events if isinstance(e, dict)],
            link=record if isinstance(record, dict) else None, repo_root=repo_root, file_events=file_events,
        )
        if connected is not None:
            spool.update_state(env, capture_id, connected=connected.to_state())
        if finalized:
            spool.update_state(env, capture_id, deliver=True)
        if count or finalized:
            notify(env)
        return count
    except Exception:
        return 0


def request_delivery(env: dict | os._Environ | None = None, *, repo_root: Path | None = None) -> bool:
    """Ask the next flush to deliver Receipts and verification evidence (after ``openshard verify``). Never raises."""
    try:
        attachment = resolve_attachment(env)
        source = os.environ if env is None else env
        if attachment is None and spool._SCOPE_ENV not in source:
            candidates = spool.connected_envs(env)
            requested = False
            for scoped in candidates:
                candidate = spool.read_state(scoped) or {}
                if repo_root is not None and str(repo_root.resolve()) not in candidate.get("repos", []):
                    continue
                requested = request_delivery(scoped, repo_root=repo_root) or requested
            if candidates:
                return requested
        state = spool.read_state(env) or {}
        capture_id: str
        if attachment is not None:
            capture_id = attachment.capture_id
        else:
            connected = ConnectedSession.from_state(state.get("connected"))
            if resolve_connection(env) is None or connected is None:
                return False
            raw_capture_id = state.get("capture_id")
            if not isinstance(raw_capture_id, str) or not raw_capture_id:
                return False
            capture_id = raw_capture_id
        env = spool.session_env(env, capture_id)
        if repo_root is not None:
            spool.append(env, capture_id, [], repo_root=repo_root)
        spool.update_state(env, capture_id, deliver=True)
        notify(env)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Flush
# ---------------------------------------------------------------------------


def _batch(state: dict, events: list[dict]) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "source": {"product": SOURCE_PRODUCT, "version": _version()},
        "collector_id": state["collector_id"],
        "events": events,
        "links": [link for link in (state.get("links") or []) if isinstance(link, dict)][: spool.MAX_LINKS],
    }


def _backoff(env: dict | os._Environ | None, capture_id: str, state: dict, now: float) -> None:
    failures = int(state.get("failures") or 0) + 1
    wait = min(_BACKOFF_MIN_SECONDS * (2 ** (failures - 1)), _BACKOFF_MAX_SECONDS)
    spool.update_state(env, capture_id, failures=failures, backoff_until=now + wait)


def _deliver(env: dict | os._Environ | None, link: Any, client: Any, state: dict) -> dict[str, Any] | None:
    """Deliver finalised Receipts and later verification through the selected capture transport."""
    from openshard.sync import client as sync_client
    totals: dict[str, Any] = {"sent": 0, "created": 0, "duplicate": 0, "conflict": 0, "rejected": 0, "pending": 0,
                              "in_progress": 0, "evidence_recorded": 0, "stopped": None}
    for repo in [r for r in (state.get("repos") or []) if isinstance(r, str)]:
        root = Path(repo)
        if not (root / ".openshard" / "runs.jsonl").is_file():
            continue
        receipt_ids = frozenset(item["receipt_id"] for item in state.get("links", [])
                                if isinstance(item, dict) and isinstance(item.get("receipt_id"), str))
        report = sync_client.flush(root, env=env, link=link, transport=client,
                                   limit=spool.MAX_LINKS, receipt_ids=receipt_ids)
        for key in ("sent", "created", "duplicate", "conflict", "rejected", "pending", "in_progress", "evidence_recorded"):
            totals[key] += int(getattr(report, key, 0) or 0)
        totals["stopped"] = totals["stopped"] or report.stopped
    return totals


def flush(
    env: dict | os._Environ | None = None,
    *,
    client: Any = None,
    now: float | None = None,
    deliver: bool | None = None,
    heartbeat: bool = False,
) -> RemoteFlushReport:
    """Send what the spool holds. Idempotent and safe to run as often as you like. Never raises.

    *deliver*: True always delivers Receipts, False never, None (default)
    only when the spool state asks for it (a session ended, or
    ``openshard verify`` ran).
    """
    # Flush every connected session independently; no shared queue can replace another.
    source = os.environ if env is None else env
    if spool._SCOPE_ENV not in source and resolve_attachment(env) is None:
        scopes = spool.connected_envs(env)
        legacy = spool.session_env(env, "legacy")
        if spool.read_state(legacy) is not None:
            scopes.insert(0, legacy)
        if scopes:
            total = RemoteFlushReport()
            for scoped in scopes:
                part = flush(scoped, client=client, now=now, deliver=deliver, heartbeat=heartbeat)
                total.attached = total.attached or part.attached
                for name in ("batches", "events_sent", "events_rejected", "pending"):
                    setattr(total, name, getattr(total, name) + getattr(part, name))
                total.heartbeat = total.heartbeat or part.heartbeat
                total.stopped = total.stopped or part.stopped
                total.repos = list(dict.fromkeys([*total.repos, *part.repos]))
                if part.receipts is not None:
                    if total.receipts is None:
                        total.receipts = dict(part.receipts)
                    else:
                        for key, value in part.receipts.items():
                            if isinstance(value, int) and not isinstance(value, bool):
                                total.receipts[key] = int(total.receipts.get(key) or 0) + value
                            elif value and not total.receipts.get(key):
                                total.receipts[key] = value
            return total
    report = RemoteFlushReport()
    try:
        attachment = resolve_attachment(env)
        state_before = spool.read_state(env) or {}
        connection = None
        connected = None
        capture_id: str
        if attachment is not None:
            capture_id = attachment.capture_id
        else:
            connection = resolve_connection(env)
            connected = ConnectedSession.from_state(state_before.get("connected"))
            raw_capture_id = state_before.get("capture_id")
            if connection is None or connected is None or not isinstance(raw_capture_id, str) or not raw_capture_id:
                report.stopped = "not_attached"
                return report
            capture_id = raw_capture_id
            report.attached = True
            if capture_id != connected_sink_id(connection, connected):
                report.stopped = "connection_changed"
                report.pending = spool.pending_count(env)
                return report
        report.attached = True
        current = now if now is not None else time.time()

        from openshard.history.jsonl_store import LockTimeoutError, history_file_lock

        lock_target = spool.spool_dir(env) / "flush"
        spool.spool_dir(env).mkdir(parents=True, exist_ok=True)
        try:
            lock = history_file_lock(lock_target, timeout=_FLUSH_LOCK_TIMEOUT)
            lock.__enter__()
        except LockTimeoutError:
            report.stopped = "busy"
            report.pending = spool.pending_count(env)
            return report
        try:
            state = spool.update_state(env, capture_id, last_attempt_at=spool.now_stamp())
            report.repos = [r for r in (state.get("repos") or []) if isinstance(r, str)]
            if state.get("stopped") == STOP_UNAUTHORIZED:
                report.stopped = STOP_UNAUTHORIZED
                report.pending = spool.pending_count(env)
                return report
            until = state.get("backoff_until")
            if isinstance(until, (int, float)) and current < until:
                report.stopped = "backoff"
                report.pending = spool.pending_count(env)
                return report
            if client is None:
                if attachment is not None:
                    from openshard.remote.transport import RemoteCaptureClient

                    client = RemoteCaptureClient(attachment, user_agent=f"openshard/{_version()}")
                else:
                    from openshard.connected.transport import ConnectedCaptureClient

                    assert connection is not None and connected is not None
                    client = ConnectedCaptureClient(connection, connected, user_agent=f"openshard/{_version()}")

            contacted = False
            while report.batches < _MAX_BATCHES_PER_FLUSH and state.get("stopped") != STOP_FULL:
                events, state = spool.pending(env, capture_id)
                if not events:
                    break
                outcome = _send_batch(env, capture_id, client, state, events, report)
                if outcome != "ok":
                    report.stopped = outcome
                    if outcome == "unavailable":
                        _backoff(env, capture_id, state, current)
                    break
                contacted = True

            if report.stopped is None and not contacted and heartbeat:
                result = client.send_events(_batch(state, []))
                if result.accepted:
                    report.heartbeat = True
                    spool.update_state(env, capture_id, last_contact_at=spool.now_stamp(), failures=0, backoff_until=None)
                elif result.kind == "unauthorized":
                    spool.update_state(env, capture_id, stopped=STOP_UNAUTHORIZED)
                    report.stopped = STOP_UNAUTHORIZED

            state = spool.read_state(env) or state
            wants = bool(state.get("deliver")) if deliver is None else deliver
            if wants and report.stopped in (None, STOP_FULL):
                from openshard.sync.config import SOURCE_ENV, PlatformLink

                if attachment is not None:
                    delivery_link = PlatformLink(
                        endpoint=attachment.capture_url,
                        organisation_id=attachment.organisation_id or f"remote-capture:{attachment.capture_id}",
                        api_key=attachment.token,
                        linked_at=None,
                        source=SOURCE_ENV,
                    )
                else:
                    assert connection is not None
                    delivery_link = PlatformLink(
                        endpoint=connection.endpoint,
                        organisation_id=connection.organisation_id,
                        api_key=connection.token,
                        linked_at=None,
                        source=SOURCE_ENV,
                    )
                report.receipts = _deliver(env, delivery_link, client, state)
                settled = report.receipts is not None and not report.receipts.get("stopped") and not report.receipts.get("pending")
                if settled and state.get("deliver"):
                    spool.update_state(env, capture_id, deliver=False)
            report.pending = spool.pending_count(env)
            return report
        finally:
            lock.__exit__(None, None, None)
    except Exception:
        report.stopped = report.stopped or "error"
        return report


def _send_batch(
    env: dict | os._Environ | None, capture_id: str, client: Any, state: dict, events: list[dict],
    report: RemoteFlushReport,
) -> str:
    """Send one batch and settle it. Returns ``ok`` or why the flush must stop."""
    result = client.send_events(_batch(state, events))
    report.batches += 1
    last_seq = events[-1]["seq"]
    if result.accepted:
        spool.acknowledge(env, capture_id, last_seq, sent=len(events))
        report.events_sent += len(events)
        return "ok"
    if result.kind == "unauthorized":
        spool.update_state(env, capture_id, stopped=STOP_UNAUTHORIZED)
        return STOP_UNAUTHORIZED
    if result.kind == "conflict":
        spool.update_state(env, capture_id, stopped=STOP_FULL)
        return STOP_FULL
    if result.kind == "rejected":
        if len(events) == 1:
            spool.acknowledge(env, capture_id, last_seq, rejected=1)
            report.events_rejected += 1
            return "ok"
        # One Event in the batch is unacceptable: find it without losing the others.
        for event in events:
            outcome = _send_batch(env, capture_id, client, state, [event], report)
            if outcome != "ok":
                return outcome
        return "ok"
    return "unavailable"


# ---------------------------------------------------------------------------
# Asking for a flush without waiting for it
# ---------------------------------------------------------------------------


def _spawn_detached_flusher(env: dict | os._Environ | None) -> bool:
    argv = [sys.executable, "-m", "openshard.cli.entrypoint", "remote", "flush", "--background"]
    child_env = dict(os.environ if env is None else env)
    kwargs: dict = {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "env": child_env, "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)  # noqa: S603 - fixed argv, no shell
    return True


def notify(env: dict | os._Environ | None = None) -> None:
    """Ask for a flush soon. Returns immediately; never raises.

    Inside the capture service the flusher thread is woken. Anywhere else a
    detached one-shot flusher is started, at most one per
    ``FLUSH_INTERVAL_SECONDS``: it waits a moment so that a burst of hooks
    becomes one batch, drains the spool and exits.
    """
    try:
        if _background_active:
            _wake.set()
            return
        source = os.environ if env is None else env
        if source.get("OPENSHARD_REMOTE_NO_SPAWN"):
            return
        state = spool.read_state(env) or {}
        last = state.get("last_spawn_at")
        current = time.time()
        if isinstance(last, (int, float)) and current - last < FLUSH_INTERVAL_SECONDS:
            return
        attachment = resolve_attachment(env)
        capture_id: str
        if attachment is not None:
            capture_id = attachment.capture_id
        else:
            connected = ConnectedSession.from_state(state.get("connected"))
            connection = resolve_connection(env)
            raw_capture_id = state.get("capture_id")
            if connection is None or connected is None or not isinstance(raw_capture_id, str) or not raw_capture_id:
                return
            capture_id = raw_capture_id
        spool.update_state(env, capture_id, last_spawn_at=current)
        _spawn_detached_flusher(env)
    except Exception:
        return


def run_background_flusher(env: dict | os._Environ | None = None, *, settle_seconds: float = 2.0, max_seconds: float = 120.0) -> RemoteFlushReport:
    """The detached one-shot flusher: let a burst settle, then flush until drained (or stopped)."""
    deadline = time.time() + max_seconds
    time.sleep(max(0.0, settle_seconds))
    report = flush(env)
    while report.pending and report.stopped is None and time.time() < deadline:
        time.sleep(1.0)
        report = flush(env)
    return report


def flush_periodically(stop: threading.Event, *, env: dict | os._Environ | None = None) -> None:
    """The capture service's flusher thread: flush shortly after every Event, heartbeat while idle."""
    global _background_active
    _background_active = True
    last_contact = 0.0
    try:
        while not stop.is_set():
            woke = _wake.wait(FLUSH_INTERVAL_SECONDS)
            if stop.is_set():
                break
            if woke:
                _wake.clear()
                # Let the burst that woke us finish arriving.
                stop.wait(min(1.0, FLUSH_INTERVAL_SECONDS))
            if resolve_attachment(env) is None:
                state = spool.read_state(env) or {}
                if resolve_connection(env) is None or (
                    ConnectedSession.from_state(state.get("connected")) is None and not spool.connected_envs(env)
                ):
                    continue
            idle_for = time.time() - last_contact
            if spool.pending_count(env) or woke or idle_for >= HEARTBEAT_SECONDS:
                report = flush(env, heartbeat=idle_for >= HEARTBEAT_SECONDS)
                if report.events_sent or report.heartbeat:
                    last_contact = time.time()
    finally:
        _background_active = False
        try:
            flush(env)
        except Exception:
            pass


__all__ = [
    "FLUSH_INTERVAL_SECONDS",
    "HEARTBEAT_SECONDS",
    "RemoteFlushReport",
    "flush",
    "flush_periodically",
    "notify",
    "record",
    "request_delivery",
    "run_background_flusher",
]