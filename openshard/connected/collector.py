"""Offline-first streaming through an already-linked Openshard account.

This is the normal frictionless capture path once a user has linked the
Platform. Each external agent session gets an isolated durable spool under
the user's Openshard home. Hooks append locally first; a detached flusher
sends the batch to the Platform's connected-capture route.

Manual Remote Capture remains the stronger explicit override for disposable
runtimes that were given an osr_ token. This module is only used when no
manual attachment is active.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from openshard.adapters.claude_capture_client import capture_home
from openshard.remote import spool
from openshard.sync.config import (
    API_KEY_ENV,
    ENDPOINT_ENV,
    ORG_ENV,
    PlatformLink,
    resolve_link,
)
from openshard.sync.transport import LINK_KINDS, KIND_REJECTED, KIND_UNAVAILABLE

SOURCE_PRODUCT = "openshard-connected"
SURFACE_ENV = "OPENSHARD_CAPTURE_SURFACE"
DISABLE_ENV = "OPENSHARD_CONNECTED_CAPTURE"
NO_SPAWN_ENV = "OPENSHARD_CONNECTED_NO_SPAWN"

_FLUSH_INTERVAL_SECONDS = 5.0
_BACKOFF_MIN_SECONDS = 5.0
_BACKOFF_MAX_SECONDS = 300.0
_MAX_BATCHES_PER_FLUSH = 60
_SCOPE_DIR = "connected-captures"


@dataclass
class ConnectedFlushReport:
    linked: bool = False
    batches: int = 0
    events_sent: int = 0
    events_rejected: int = 0
    pending: int = 0
    receipts: dict[str, Any] | None = None
    stopped: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _version() -> str:
    try:
        from openshard import __version__

        return str(__version__)
    except Exception:
        return "unknown"


def _enabled(env: dict | os._Environ | None) -> bool:
    source = os.environ if env is None else env
    value = source.get(DISABLE_ENV)
    return not (
        isinstance(value, str)
        and value.strip().lower() in {"0", "off", "false", "no", "disabled"}
    )


def _surface(agent: str, env: dict | os._Environ | None) -> str:
    source = os.environ if env is None else env
    override = source.get(SURFACE_ENV)
    if isinstance(override, str):
        candidate = override.strip().lower().replace("_", "-")
        if (
            candidate
            and len(candidate) <= 64
            and all(ch.isalnum() or ch in ".:-" for ch in candidate)
        ):
            return candidate
    if str(source.get("CLAUDE_CODE_REMOTE", "")).lower() in {"1", "true", "yes"}:
        return "claude-code-web"
    if source.get("CURSOR_CODE_REMOTE"):
        return "cursor-cloud"
    labels = {
        "claude_code": "claude-code",
        "codex": "codex",
        "cursor": "cursor",
        "opencode": "opencode",
        "antigravity": "antigravity",
        "grok_build": "grok-build",
        "hermes": "hermes",
    }
    return labels.get(agent, agent.replace("_", "-")[:64] or "agent")


def _session_key(surface: str, external_session_id: str) -> str:
    return hashlib.sha256(
        f"{surface}\0{external_session_id}".encode()
    ).hexdigest()[:24]


def _capture_key(surface: str, external_session_id: str) -> str:
    return "connected_" + _session_key(surface, external_session_id)


def _scoped_env(
    env: dict | os._Environ | None,
    surface: str,
    external_session_id: str,
) -> dict[str, str]:
    source = dict(os.environ if env is None else env)
    root = (
        Path(capture_home(source))
        / _SCOPE_DIR
        / _session_key(surface, external_session_id)
    )
    source["OPENSHARD_HOME"] = str(root)
    return source


def _entry_context(
    entry: dict | None,
    repo_root: Path,
) -> tuple[str | None, str | None, str | None, str | None]:
    if not isinstance(entry, dict):
        return None, repo_root.name, None, None
    capture = entry.get("capture")
    capture = capture if isinstance(capture, dict) else {}
    provider = (
        capture.get("provider")
        if isinstance(capture.get("provider"), str)
        else None
    )
    identity = (
        entry.get("repo_identity")
        if isinstance(entry.get("repo_identity"), str)
        else None
    )
    branch = (
        entry.get("git_branch")
        if isinstance(entry.get("git_branch"), str)
        else None
    )
    return provider, repo_root.name, identity, branch


def _batch(state: dict, events: list[dict]) -> dict[str, Any]:
    return {
        "source": {"product": SOURCE_PRODUCT, "version": _version()},
        "collector_id": state["collector_id"],
        "events": events,
        "links": [
            x
            for x in (state.get("links") or [])
            if isinstance(x, dict)
        ][: spool.MAX_LINKS],
    }


def record(
    repo_root: Path,
    events: list[dict],
    *,
    record: dict | None = None,
    entry: dict | None = None,
    finalized: bool = False,
    session_id: str | None,
    agent: str,
    env: dict | os._Environ | None = None,
) -> int:
    """Append this hook's evidence to its session spool. Never touches network."""
    try:
        if (
            not _enabled(env)
            or not isinstance(session_id, str)
            or not session_id
        ):
            return 0
        link = resolve_link(env)
        if link is None:
            return 0
        surface = _surface(agent, env)
        scoped = _scoped_env(env, surface, session_id)
        capture_key = _capture_key(surface, session_id)
        file_events: list[dict] = []
        if isinstance(entry, dict):
            file_events = [
                e
                for e in (entry.get("events") or [])
                if isinstance(e, dict)
                and e.get("event_type") == "file.changed"
            ]
        count = spool.append(
            scoped,
            capture_key,
            [e for e in events if isinstance(e, dict)],
            link=record if isinstance(record, dict) else None,
            repo_root=repo_root,
            file_events=file_events,
        )
        provider, repo, identity, branch = _entry_context(entry, repo_root)
        previous = spool.read_state(scoped) or {}
        spool.update_state(
            scoped,
            capture_key,
            surface=surface,
            external_session_id=session_id,
            agent=agent,
            provider=provider,
            repo_identity=identity,
            repo=repo,
            branch=branch,
            deliver=bool(finalized) or bool(previous.get("deliver")),
        )
        if count or finalized:
            notify(scoped, link)
        return count
    except Exception:
        return 0


def _client(link: PlatformLink, state: dict):
    from openshard.connected.transport import ConnectedCaptureClient

    return ConnectedCaptureClient(
        link,
        surface=str(state.get("surface") or "agent"),
        external_session_id=str(
            state.get("external_session_id")
            or state.get("capture_id")
            or "unknown"
        ),
        agent=str(state.get("agent") or "other"),
        provider=(
            state.get("provider")
            if isinstance(state.get("provider"), str)
            else None
        ),
        repo_identity=(
            state.get("repo_identity")
            if isinstance(state.get("repo_identity"), str)
            else None
        ),
        repo=(
            state.get("repo")
            if isinstance(state.get("repo"), str)
            else None
        ),
        branch=(
            state.get("branch")
            if isinstance(state.get("branch"), str)
            else None
        ),
        user_agent=f"openshard/{_version()}",
    )


def _backoff(
    env: dict | os._Environ | None,
    capture_key: str,
    state: dict,
    now: float,
) -> None:
    failures = int(state.get("failures") or 0) + 1
    wait = min(
        _BACKOFF_MIN_SECONDS * (2 ** (failures - 1)),
        _BACKOFF_MAX_SECONDS,
    )
    spool.update_state(
        env,
        capture_key,
        failures=failures,
        backoff_until=now + wait,
    )


def _send_batch(
    env: dict | os._Environ | None,
    capture_key: str,
    client: Any,
    state: dict,
    events: list[dict],
    report: ConnectedFlushReport,
) -> str:
    result = client.send_events(_batch(state, events))
    report.batches += 1
    last_seq = events[-1]["seq"]
    if result.accepted:
        spool.acknowledge(
            env,
            capture_key,
            last_seq,
            sent=len(events),
        )
        report.events_sent += len(events)
        return "ok"
    if result.kind in LINK_KINDS:
        spool.update_state(env, capture_key, stopped=result.kind)
        return result.kind
    if result.kind == KIND_REJECTED:
        if len(events) == 1:
            spool.acknowledge(
                env,
                capture_key,
                last_seq,
                rejected=1,
            )
            report.events_rejected += 1
            return "ok"
        for event in events:
            outcome = _send_batch(
                env,
                capture_key,
                client,
                state,
                [event],
                report,
            )
            if outcome != "ok":
                return outcome
        return "ok"
    return KIND_UNAVAILABLE


def _deliver(
    env: dict | os._Environ | None,
    link: PlatformLink,
    state: dict,
) -> dict[str, Any] | None:
    from openshard.sync import client as sync_client

    totals: dict[str, Any] = {
        "sent": 0,
        "created": 0,
        "duplicate": 0,
        "conflict": 0,
        "rejected": 0,
        "pending": 0,
        "in_progress": 0,
        "evidence_recorded": 0,
        "stopped": None,
    }
    for raw in [
        r
        for r in (state.get("repos") or [])
        if isinstance(r, str)
    ]:
        root = Path(raw)
        if not (root / ".openshard" / "runs.jsonl").is_file():
            continue
        report = sync_client.flush(
            root,
            env=env,
            link=link,
            limit=spool.MAX_LINKS,
        )
        for key in (
            "sent",
            "created",
            "duplicate",
            "conflict",
            "rejected",
            "pending",
            "in_progress",
            "evidence_recorded",
        ):
            totals[key] += int(getattr(report, key, 0) or 0)
        totals["stopped"] = totals["stopped"] or report.stopped
    return totals


def request_delivery(
    env: dict | os._Environ | None = None,
    *,
    repo_root: Path | None = None,
) -> bool:
    """Wake every connected session that can strengthen this repository.

    Post-session verification may happen after the agent process is gone, so
    there is no session id to address directly. The bounded session directory
    is scanned for spools that saw this repository; each is marked for normal
    Receipt/evidence sync and flushed asynchronously.
    """
    try:
        link = resolve_link(env)
        if link is None or repo_root is None:
            return False
        source = dict(os.environ if env is None else env)
        base = Path(capture_home(source)) / _SCOPE_DIR
        if not base.is_dir():
            return False
        wanted = str(Path(repo_root))
        found = False
        for child in base.iterdir():
            if not child.is_dir():
                continue
            scoped = dict(source)
            scoped["OPENSHARD_HOME"] = str(child)
            state = spool.read_state(scoped)
            if not isinstance(state, dict):
                continue
            repos = [r for r in (state.get("repos") or []) if isinstance(r, str)]
            capture_key = state.get("capture_id")
            if wanted not in repos or not isinstance(capture_key, str):
                continue
            spool.update_state(scoped, capture_key, deliver=True)
            notify(scoped, link)
            found = True
        return found
    except Exception:
        return False


def flush(
    env: dict | os._Environ | None = None,
    *,
    link: PlatformLink | None = None,
    client: Any = None,
    now: float | None = None,
) -> ConnectedFlushReport:
    """Drain one scoped session spool. Idempotent and retry-safe. Never raises."""
    report = ConnectedFlushReport()
    try:
        link = link or resolve_link(env)
        if link is None:
            report.stopped = "not_connected"
            return report
        report.linked = True
        state = spool.read_state(env)
        if state is None or not isinstance(state.get("capture_id"), str):
            report.stopped = "no_spool"
            return report
        capture_key = state["capture_id"]
        current = time.time() if now is None else now
        stopped = state.get("stopped")
        if isinstance(stopped, str) and stopped:
            report.stopped = stopped
            report.pending = spool.pending_count(env)
            return report
        until = state.get("backoff_until")
        if isinstance(until, (int, float)) and current < until:
            report.stopped = "backoff"
            report.pending = spool.pending_count(env)
            return report
        sender = client or _client(link, state)

        while report.batches < _MAX_BATCHES_PER_FLUSH:
            events, state = spool.pending(env, capture_key)
            if not events:
                break
            outcome = _send_batch(
                env,
                capture_key,
                sender,
                state,
                events,
                report,
            )
            if outcome != "ok":
                report.stopped = outcome
                if outcome == KIND_UNAVAILABLE:
                    _backoff(
                        env,
                        capture_key,
                        state,
                        current,
                    )
                break

        state = spool.read_state(env) or state
        if state.get("deliver") and report.stopped is None:
            report.receipts = _deliver(env, link, state)
            settled = (
                report.receipts is not None
                and not report.receipts.get("stopped")
                and not report.receipts.get("pending")
            )
            if settled:
                spool.update_state(
                    env,
                    capture_key,
                    deliver=False,
                )
        report.pending = spool.pending_count(env)
        return report
    except Exception:
        report.stopped = report.stopped or "error"
        return report


def _spawn_env(
    env: dict | os._Environ | None,
    link: PlatformLink,
) -> dict[str, str]:
    child = dict(os.environ if env is None else env)
    child[ENDPOINT_ENV] = link.endpoint
    child[ORG_ENV] = link.organisation_id
    child[API_KEY_ENV] = link.api_key
    return child


def notify(
    env: dict | os._Environ | None,
    link: PlatformLink,
) -> None:
    """Start one throttled detached flusher for this session. Never blocks agent."""
    try:
        source = os.environ if env is None else env
        if source.get(NO_SPAWN_ENV):
            return
        state = spool.read_state(source) or {}
        capture_key = state.get("capture_id")
        if not isinstance(capture_key, str):
            return
        current = time.time()
        last = state.get("last_spawn_at")
        if (
            isinstance(last, (int, float))
            and current - last < _FLUSH_INTERVAL_SECONDS
        ):
            return
        spool.update_state(
            source,
            capture_key,
            last_spawn_at=current,
        )
        argv = [
            sys.executable,
            "-m",
            "openshard.cli.entrypoint",
            "connected",
            "flush",
            "--background",
        ]
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": _spawn_env(source, link),
            "close_fds": True,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                getattr(
                    subprocess,
                    "CREATE_NO_WINDOW",
                    0x08000000,
                )
                | getattr(
                    subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0x00000200,
                )
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv,
            **kwargs,
        )
    except Exception:
        return


def run_background_flusher(
    env: dict | os._Environ | None = None,
    *,
    settle_seconds: float = 1.0,
    max_seconds: float = 120.0,
) -> ConnectedFlushReport:
    """Coalesce a hook burst, then drain or leave it durable for next hook."""
    deadline = time.time() + max_seconds
    time.sleep(max(0.0, settle_seconds))
    report = flush(env)
    while (
        report.pending
        and report.stopped is None
        and time.time() < deadline
    ):
        time.sleep(1.0)
        report = flush(env)
    return report


__all__ = [
    "ConnectedFlushReport",
    "DISABLE_ENV",
    "NO_SPAWN_ENV",
    "SOURCE_PRODUCT",
    "SURFACE_ENV",
    "flush",
    "notify",
    "record",
    "request_delivery",
    "run_background_flusher",
]
