"""Cross-platform locked JSONL writes for OpenShard's local history store.

Every ``.openshard/*.jsonl`` record write funnels through this module so that
concurrent OpenShard processes writing the same history file cannot interleave
or tear each other's lines. Locking is stdlib-only: ``fcntl`` on Unix-like
systems and ``msvcrt`` on Windows, applied to a sidecar ``<file>.lock`` so we
never tangle with append-mode seek semantics on the data file descriptor.

Helpers exposed:

- ``append_jsonl(path, record)`` — append one record as a single JSON line.
- ``write_jsonl(path, records)`` — crash-safe whole-file rewrite (temp + replace).
- ``upsert_jsonl(path, record, match)`` — replace the first line whose record
  satisfies ``match`` in place, else append; other lines are preserved
  byte-for-byte (malformed lines included).
- ``history_file_lock(path)`` — the same sidecar lock, for callers that need to
  read-modify-write a small companion file next to the history store.

All derive the same lock path from the data file, so an append, an upsert and a
rewrite of the same history file mutually exclude.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# Polling interval used only when a bounded `timeout` is requested (the
# blocking OS lock calls below are used as-is, with no polling, whenever
# timeout is None -- the default, unbounded-wait behavior every existing
# caller keeps).
_POLL_INTERVAL_SECONDS = 0.02


class LockTimeoutError(TimeoutError):
    """Raised when a bounded lock wait (``timeout=`` given) is not acquired in time.

    Distinguished from a plain ``TimeoutError`` so callers that want to
    fail open specifically on lock contention (never on some other
    ``TimeoutError``-raising failure) can catch it precisely.
    """


@contextmanager
def _file_lock(lock_path: Path, *, timeout: float | None = None):
    """Hold an exclusive cross-process lock on a sidecar ``.lock`` file.

    With ``timeout=None`` (default, unchanged from before), acquisition
    blocks on the OS's own blocking lock call until the lock is held -- this
    is the behavior every existing caller relies on (a run/history write
    that must not be silently skipped). With a numeric ``timeout``, this
    instead polls a *non-blocking* lock attempt every
    ``_POLL_INTERVAL_SECONDS`` and raises :class:`LockTimeoutError` once
    *timeout* seconds have elapsed without acquiring it -- for callers (the
    Claude Code hook/status-line path) that must never hang Claude Code on
    a stuck or contended lock, and would rather skip one capture than block.

    Release is guaranteed in a ``finally`` even on exception, and the handle
    is closed in a nested ``finally`` so the OS lock is freed even if the
    unlock call itself raises. No path leaves a lock held, so there is no
    deadlock.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    acquired = False
    try:
        if sys.platform == "win32":
            # msvcrt.locking locks a 1-byte range, so the sidecar must have at
            # least one byte; empty-file byte-range behavior is ambiguous.
            if os.fstat(fh.fileno()).st_size < 1:
                fh.write("\0")
                fh.flush()
            fh.seek(0)
            if timeout is None:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)  # blocking exclusive
                acquired = True
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise LockTimeoutError(
                                f"timed out after {timeout}s waiting for lock: {lock_path}"
                            ) from None
                        time.sleep(_POLL_INTERVAL_SECONDS)
        else:
            if timeout is None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                acquired = True
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise LockTimeoutError(
                                f"timed out after {timeout}s waiting for lock: {lock_path}"
                            ) from None
                        time.sleep(_POLL_INTERVAL_SECONDS)
        yield
    finally:
        try:
            if acquired:
                if sys.platform == "win32":
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _lock_path_for(path: Path) -> Path:
    """Return the sidecar lock path for *path* (e.g. ``runs.jsonl.lock``)."""
    return path.with_name(path.name + ".lock")


@contextmanager
def history_file_lock(path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """Hold the exclusive sidecar lock that guards *path*.

    Public wrapper over ``_file_lock`` for callers that must read-modify-write
    a small JSON companion file (not a JSONL history) atomically across
    concurrent OpenShard processes -- e.g. the per-session staging buffer the
    Claude Code hook adapter keeps while a session is live. Not re-entrant:
    never call ``append_jsonl``/``write_jsonl``/``upsert_jsonl`` on the same
    *path* while holding it.

    ``timeout`` (seconds) bounds the wait and raises :class:`LockTimeoutError`
    on expiry instead of blocking forever; ``None`` (default) preserves the
    original unbounded-blocking behavior.
    """
    with _file_lock(_lock_path_for(Path(path)), timeout=timeout):
        yield


def _atomic_replace(path: Path, blob: str) -> None:
    """Write *blob* to a sibling temp file, fsync, then rename over *path*."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def upsert_jsonl(
    path: Path, record: dict, match: Callable[[dict], bool], *, timeout: float | None = None
) -> str:
    """Replace the first record satisfying *match* with *record*, else append it.

    Returns ``"replaced"`` or ``"appended"``. Lines that are blank, malformed,
    or do not match are preserved verbatim -- this never re-serializes or
    re-coerces any record other than the one being written. A replace is a
    crash-safe temp+rename rewrite; an append is a plain locked append. Both
    happen under the same sidecar lock as :func:`append_jsonl`, and the whole
    read-decide-write sequence is one critical section, so two concurrent
    upserts for the same key can never both append.

    ``timeout`` bounds the lock wait (see :func:`history_file_lock`); raises
    :class:`LockTimeoutError` on expiry instead of blocking forever.
    """
    path = Path(path)
    line = json.dumps(record) + "\n"  # serialize BEFORE locking
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(_lock_path_for(path), timeout=timeout):
        existing: list[str] = []
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                existing = fh.read().splitlines(keepends=True)
        for i, raw in enumerate(existing):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and match(parsed):
                existing[i] = line
                _atomic_replace(path, "".join(existing))
                return "replaced"
        with path.open("a", encoding="utf-8") as fh:
            if existing and not existing[-1].endswith("\n"):
                fh.write("\n")
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        return "appended"


def append_jsonl(path: Path, record: dict) -> None:
    """Append one record to *path* as a single locked, fsync'd JSON line.

    Serialization happens before the lock is acquired and before the file is
    opened, so a non-serializable record raises with nothing written and no
    lock taken.
    """
    path = Path(path)
    line = json.dumps(record) + "\n"  # serialize BEFORE locking / opening
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(_lock_path_for(path)):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


def write_jsonl(path: Path, records: list[dict]) -> None:
    """Crash-safe locked whole-file rewrite of *path* with *records*.

    Writes to a sibling temp file, fsyncs it, then ``os.replace``s it over the
    target (atomic on both Windows and POSIX) so the real file is never
    truncated mid-write. Uses the same sidecar lock as :func:`append_jsonl`, so
    a rewrite and a concurrent append of the same file serialize.
    """
    path = Path(path)
    blob = "".join(json.dumps(r) + "\n" for r in records)  # serialize BEFORE locking
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(_lock_path_for(path)):
        _atomic_replace(path, blob)  # atomic rename over target
