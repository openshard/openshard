"""A bounded local queue of already-validated events (``~/.openshard/telemetry.queue.jsonl``).

Telemetry is not evidence: loss is acceptable, growth is not. The queue is
capped by event count and by bytes and drops the *oldest* lines when full,
so an install that is offline for months holds at most a few hundred
small events. There is no fsync (a crash may lose the tail) and every
function swallows I/O errors. Concurrent writers (a CLI command and the
capture service) are serialised with the history store's file lock.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from openshard.adapters.claude_capture_client import capture_home

QUEUE_FILENAME = "telemetry.queue.jsonl"
MAX_EVENTS = 500
MAX_BYTES = 256 * 1024
_LOCK_TIMEOUT = 1.0


def queue_path(env: dict | os._Environ | None = None) -> Path:
    return Path(capture_home(env)) / QUEUE_FILENAME


def _lock(path: Path):
    from openshard.history.jsonl_store import history_file_lock

    return history_file_lock(path, timeout=_LOCK_TIMEOUT)


def _read_lines(path: Path) -> list[str]:
    try:
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(ln + "\n")
    os.replace(tmp, path)


def _bounded(lines: list[str]) -> list[str]:
    """Drop the oldest lines until both caps hold."""
    kept = lines[-MAX_EVENTS:]
    total = sum(len(ln) + 1 for ln in kept)
    while kept and total > MAX_BYTES:
        total -= len(kept[0]) + 1
        kept.pop(0)
    return kept


def append(event: dict, env: dict | os._Environ | None = None) -> bool:
    """Append one event. Returns False (and drops it) on any failure. Never raises."""
    try:
        line = json.dumps(event, separators=(",", ":"), sort_keys=True)
        if len(line) + 1 > MAX_BYTES:
            return False
        path = queue_path(env)
        with _lock(path):
            lines = _read_lines(path)
            lines.append(line)
            if len(lines) > MAX_EVENTS or sum(len(ln) + 1 for ln in lines) > MAX_BYTES:
                lines = _bounded(lines)
                _write_lines(path, lines)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        return True
    except Exception:
        return False


def take(max_events: int, env: dict | os._Environ | None = None) -> list[dict]:
    """Remove and return up to *max_events* oldest events. Never raises."""
    try:
        path = queue_path(env)
        with _lock(path):
            lines = _read_lines(path)
            if not lines:
                return []
            head, rest = lines[:max_events], lines[max_events:]
            _write_lines(path, rest)
        out: list[dict] = []
        for ln in head:
            try:
                item = json.loads(ln)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out
    except Exception:
        return []


def requeue(events: list[dict], env: dict | os._Environ | None = None) -> None:
    """Put events back at the *front* (a failed send keeps order). Never raises."""
    if not events:
        return
    try:
        path = queue_path(env)
        with _lock(path):
            lines = _read_lines(path)
            front = [json.dumps(e, separators=(",", ":"), sort_keys=True) for e in events]
            _write_lines(path, _bounded(front + lines))
    except Exception:
        pass


def peek(limit: int = 20, env: dict | os._Environ | None = None) -> list[dict]:
    """The newest *limit* queued events without removing them. Never raises."""
    try:
        lines = _read_lines(queue_path(env))[-limit:]
        out: list[dict] = []
        for ln in lines:
            try:
                item = json.loads(ln)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out
    except Exception:
        return []


def size(env: dict | os._Environ | None = None) -> int:
    try:
        return len(_read_lines(queue_path(env)))
    except Exception:
        return 0


def clear(env: dict | os._Environ | None = None) -> None:
    try:
        path = queue_path(env)
        with _lock(path):
            if path.exists():
                path.unlink()
    except Exception:
        pass
