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
- ``upsert_jsonl_with(path, build, match)`` — the same upsert, but build the
  record under the lock from the current raw lines and matched record.
- ``amend_last_jsonl(path, transform)`` — replace the *last* well-formed
  record with ``transform(record)`` in place; every other line is preserved
  byte-for-byte. The read-transform-write is one critical section.
- ``history_file_lock(path)`` — the same sidecar lock, for callers that need to
  read-modify-write a small companion file next to the history store.

All derive the same lock path from the data file, so an append, an upsert, an
amend and a rewrite of the same history file mutually exclude.

An upsert never rewrites the whole file when it does not have to. A replaced
line is overwritten where it is, in its own bytes, when the new record fits
(it is padded with spaces, which every JSON reader ignores); a live Receipt
in ``runs.jsonl`` is written with spare room so its later updates fit. The
overwrite is journaled in ``<file>.redo`` first, and the next locked access
redoes a write a crash interrupted. Only a record that outgrew its line is
placed by the crash-safe whole-file rewrite. Line order, and so every
record's history position, never changes.

After a write to a ``runs.jsonl`` returns, the written record is handed to
``history.receipt_signing.seal_after_write`` (it seals only records that
stopped changing; ``write_jsonl`` rewrites are never sealed).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import overload

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# Polling interval used only when a bounded `timeout` is requested (the
# blocking OS lock calls below are used as-is, with no polling, whenever
# timeout is None -- the default, unbounded-wait behavior every existing
# caller keeps).
_POLL_INTERVAL_SECONDS = 0.02

# Per-lock-path in-process gate -- see _intra_process_lock()'s docstring for
# why this exists. Keyed by the string form of the resolved lock path so two
# different history files never serialize against each other; guarded by its
# own lock only for the get-or-create step, never held across a file lock.
_intra_process_locks: dict[str, threading.Lock] = {}
_intra_process_locks_registry_guard = threading.Lock()


def _intra_process_lock(lock_path: Path) -> threading.Lock:
    """Return the process-local lock guarding *lock_path*, creating it once.

    Root cause this exists for: POSIX ``fcntl.flock()`` is scoped to the
    *open file description*, so two threads of this same process that each
    call ``open()`` on the same sidecar file get independent, correctly
    queueable locks -- the kernel blocks the second thread until the first
    unlocks, exactly like two separate processes would. Windows
    ``msvcrt.locking()`` has no such per-handle scoping: its lock is
    tracked by the OS against the file (effectively per-process), so two
    threads of the *same* process racing to lock the same byte range is a
    scenario the Windows CRT does not handle by queuing -- its blocking
    mode (``LK_LOCK``) internally retries only a bounded number of times
    and then raises ``OSError`` (errno 36, "Resource deadlock avoided"),
    even though nothing is actually deadlocked.

    Serializing same-process callers here, before any thread ever touches
    the OS-level lock, removes the race entirely: at most one thread of
    this process is ever inside the OS-lock section for a given path, so
    the OS-level primitive only ever has to arbitrate *across* processes
    -- which is what both ``fcntl.flock`` and the polling ``msvcrt``
    fallback below do correctly. Applied on both platforms for one
    uniform code path; on POSIX it is redundant with flock's own
    same-process correctness but harmless (an uncontended acquire in the
    single-writer-per-process common case).
    """
    key = str(lock_path)
    lock = _intra_process_locks.get(key)
    if lock is None:
        with _intra_process_locks_registry_guard:
            lock = _intra_process_locks.setdefault(key, threading.Lock())
    return lock


class LockTimeoutError(TimeoutError):
    """Raised when a bounded lock wait (``timeout=`` given) is not acquired in time.

    Distinguished from a plain ``TimeoutError`` so callers that want to
    fail open specifically on lock contention (never on some other
    ``TimeoutError``-raising failure) can catch it precisely.
    """


@contextmanager
def _file_lock(lock_path: Path, *, timeout: float | None = None):
    """Hold an exclusive lock on a sidecar ``.lock`` file, safe both across
    threads of this process and across separate processes.

    Two layers, always both held together:

    1. An in-process :class:`threading.Lock` per *lock_path*
       (:func:`_intra_process_lock`), acquired first. This is what makes
       same-process multi-threaded callers safe on Windows -- see that
       function's docstring for the exact failure it prevents.
    2. The OS-level lock on the sidecar file itself, which is what makes
       *separate processes* safe.

    With ``timeout=None`` (default, unchanged from before), acquisition of
    both layers blocks until held, with no deadline -- the behavior every
    existing caller relies on (a run/history write that must not be
    silently skipped). On POSIX the OS-level layer is the kernel's own
    blocking call (``fcntl.flock``); on Windows it polls a *non-blocking*
    lock attempt every ``_POLL_INTERVAL_SECONDS`` forever, since
    ``msvcrt.locking``'s own "blocking" mode is not actually unbounded
    (see :func:`_intra_process_lock`). With a numeric ``timeout``, both
    platforms poll the same way but give up and raise
    :class:`LockTimeoutError` once *timeout* seconds have elapsed in total
    (across both layers) without acquiring the lock -- for callers (the
    Claude Code hook/status-line path) that must never hang Claude Code on
    a stuck or contended lock, and would rather skip one capture than
    block.

    Release is guaranteed in a ``finally`` even on exception, and the
    handle is closed in a nested ``finally`` so the OS lock is freed even
    if the unlock call itself raises. No path leaves a lock held, so there
    is no deadlock.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = None if timeout is None else time.monotonic() + timeout

    intra_lock = _intra_process_lock(lock_path)
    if deadline is None:
        intra_lock.acquire()
    elif not intra_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise LockTimeoutError(f"timed out after {timeout}s waiting for lock: {lock_path}")

    try:
        fh = open(lock_path, "a+")
        acquired = False
        try:
            if sys.platform == "win32":
                # msvcrt.locking locks a 1-byte range, so the sidecar must have at
                # least one byte; empty-file byte-range behavior is ambiguous.
                # Several processes can open a brand-new sidecar while it is
                # still empty; the first to seed it then locks byte 0, and
                # Windows locks are mandatory, so a second seeding write into
                # that byte fails with PermissionError (errno 13). That failure
                # only ever means "someone else already seeded and holds the
                # lock", so it is ignored and the polling loop below waits for
                # the lock like any other contention. The seed is written
                # unbuffered so no pending byte can fail again on close.
                if os.fstat(fh.fileno()).st_size < 1:
                    try:
                        os.write(fh.fileno(), b"\0")
                    except OSError:
                        pass
                fh.seek(0)
                # msvcrt.locking's own "blocking" mode (LK_LOCK) is not truly
                # unbounded: the Windows CRT retries internally only about a
                # dozen times over roughly a second, then raises OSError
                # (errno 36, "Resource deadlock avoided") even though no
                # deadlock exists -- just contention that outlasted its
                # built-in retry budget. Poll a non-blocking attempt instead
                # (forever when timeout is None), so "no timeout" actually
                # means no timeout, the same as fcntl.flock(LOCK_EX) below.
                # The intra-process lock above already rules out the
                # same-process case this budget most reliably trips on;
                # this loop is what keeps a genuine cross-process wait
                # correct too, however long it takes.
                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                        break
                    except OSError:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise LockTimeoutError(
                                f"timed out after {timeout}s waiting for lock: {lock_path}"
                            ) from None
                        time.sleep(_POLL_INTERVAL_SECONDS)
            else:
                if deadline is None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                    acquired = True
                else:
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
    finally:
        intra_lock.release()


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
    with _locked(Path(path), timeout=timeout):
        yield


@contextmanager
def _locked(path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """The sidecar lock for *path*, with any interrupted in-place write redone first."""
    with _file_lock(_lock_path_for(path), timeout=timeout):
        _redo_interrupted_write(path)
        yield


def _atomic_replace(path: Path, blob: str | bytes) -> None:
    """Write *blob* to a sibling temp file, fsync, then rename over *path*."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("wb") as fh:
            # A str is written as text mode would write it (os.linesep line ends).
            fh.write(blob if isinstance(blob, bytes) else blob.replace("\n", os.linesep).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        replace_with_retry(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def replace_with_retry(source: Path, target: Path) -> None:
    """``os.replace`` that retries a transient Windows sharing violation (at most 6 ms of sleep).

    On Windows a rename over a file another process has open fails. Readers
    that follow the learning worker's rule (copy under the history lock, then
    close) hold it only briefly, so a short retry is enough; a caller that can
    afford to wait longer (the learning worker) retries the whole pass.
    """
    for attempt in range(4):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 3:
                raise
            time.sleep(0.002)


def _learning_update(function):
    """After a successful write to a ``runs.jsonl``, ask the learning worker to refresh.

    Runs only once the write returned (its lock released), so durability is
    unaffected. Never raises and never waits on a lock: it costs a dirty-marker
    write and a non-blocking lock attempt, plus a process launch when no worker
    is running. Learning is derived, and a capture must not depend on it.
    """
    @wraps(function)
    def wrapped(path, *args, **kwargs):
        result = function(path, *args, **kwargs)
        if Path(path).name == "runs.jsonl":
            try:
                from openshard.learning.worker import schedule_update

                schedule_update(Path(path))
            except Exception:
                pass
        return result
    return wrapped


def _seal_on_write(extract: Callable[[tuple, dict, object], object], *, amendment: bool = False):
    """After a write to a ``runs.jsonl``, seal the written record if it stopped changing.

    Like the learning refresh, this runs once the write returned and its lock
    is released, and it never raises: a Receipt is written whether or not it
    can be signed (``history.receipt_signing`` records why when it cannot).
    """
    def decorate(function):
        @wraps(function)
        def wrapped(path, *args, **kwargs):
            result = function(path, *args, **kwargs)
            if Path(path).name == "runs.jsonl":
                try:
                    record = extract(args, kwargs, result)
                    if isinstance(record, dict):
                        from openshard.history.receipt_signing import seal_after_write

                        seal_after_write(Path(path), record, amendment=amendment)
                except Exception:
                    pass
            return result
        return wrapped
    return decorate


# ---------------------------------------------------------------------------
# Upsert: find the line, then overwrite it where it is when the record fits
# ---------------------------------------------------------------------------

_REDO_SUFFIX = ".redo"
# Spare room given to a live Receipt's line: as much again as the record, at
# least this many bytes, so its next updates are overwrites, not rewrites.
_MIN_HEADROOM_BYTES = 1024
_EOL = os.linesep.encode("ascii")  # what text-mode writes (append_jsonl's line end)

# Where a keyed record's line was, per history file, valid only while the
# file is exactly as this process last left it ((dev, ino, size, mtime_ns)).
# Read and written only under that file's lock.
_line_index: dict[str, tuple[tuple[int, int, int, int], dict[str, tuple[int, int]]]] = {}


@dataclass
class _Line:
    offset: int
    raw: bytes
    record: dict


class _LinesUnderLock(Sequence[str]):
    """The file's raw lines exactly as a text-mode read gives them, read on first use.

    A builder that only needs the matched record never pays for reading the
    whole history; one that inspects the lines sees the state under the lock.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lines: list[str] | None = None

    def _all(self) -> list[str]:
        if self._lines is None:
            self._lines = []
            if self._path.exists():
                with self._path.open("r", encoding="utf-8") as fh:
                    self._lines = fh.read().splitlines(keepends=True)
        return self._lines

    def __len__(self) -> int:
        return len(self._all())

    def __iter__(self) -> Iterator[str]:
        return iter(self._all())

    @overload
    def __getitem__(self, index: int) -> str: ...
    @overload
    def __getitem__(self, index: slice) -> Sequence[str]: ...
    def __getitem__(self, index: int | slice) -> str | Sequence[str]:
        return self._all()[index]


def _signature(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _parse_line(raw: bytes) -> dict | None:
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:  # malformed JSON or undecodable bytes: not a record
        return None
    return parsed if isinstance(parsed, dict) else None


def _remember(path: Path, key: str | None, offset: int, length: int, *, keep: bool) -> None:
    """Record where *key*'s line is, against the file as it is now (under the lock)."""
    try:
        signature = _signature(path.stat())
    except OSError:
        _line_index.pop(str(path), None)
        return
    known = _line_index.get(str(path))
    lines = dict(known[1]) if keep and known is not None else {}
    if key is not None:
        lines[key] = (offset, length)
    _line_index[str(path)] = (signature, lines)


def _indexed_line(fh, st: os.stat_result, path: Path, key: str, match: Callable[[dict], bool]) -> _Line | None:
    """The remembered line for *key*, if the file is unchanged and it still matches."""
    known = _line_index.get(str(path))
    if known is None or known[0] != _signature(st) or key not in known[1]:
        return None
    offset, length = known[1][key]
    fh.seek(max(offset - 1, 0))
    chunk = fh.read(length + (1 if offset else 0))
    raw = chunk[1:] if offset else chunk
    if (offset and chunk[:1] != b"\n") or len(raw) != length:
        return None
    record = _parse_line(raw)
    return _Line(offset, raw, record) if record is not None and match(record) else None


def _scan(data: bytes, match: Callable[[dict], bool], key: str | None) -> _Line | None:
    """The first line whose record satisfies *match*.

    With *key*, only lines holding its JSON encoding are parsed: a caller
    passes a key only when every line that can match contains it (a record's
    ``receipt_id``, as ``json.dumps`` writes it).
    """
    if key is None:
        offset = 0
        for raw in data.splitlines(keepends=True):
            record = _parse_line(raw)
            if record is not None and match(record):
                return _Line(offset, raw, record)
            offset += len(raw)
        return None
    needle = json.dumps(key).encode("utf-8")
    at = data.find(needle)
    while at != -1:
        start = data.rfind(b"\n", 0, at) + 1
        end = data.find(b"\n", at)
        end = len(data) if end == -1 else end + 1
        record = _parse_line(data[start:end])
        if record is not None and match(record):
            return _Line(start, data[start:end], record)
        at = data.find(needle, end)
    return None


def _find_line(path: Path, match: Callable[[dict], bool], key: str | None) -> tuple[_Line | None, bytes | None]:
    """The matched line (and the whole file, when it had to be read). Lock held."""
    try:
        fh = path.open("rb")
    except FileNotFoundError:
        return None, None
    with fh:
        st = os.fstat(fh.fileno())
        if key is not None:
            hit = _indexed_line(fh, st, path, key, match)
            if hit is not None:
                return hit, None
        fh.seek(0)
        data = fh.read()
    found = _scan(data, match, key)
    known = _line_index.get(str(path))
    unchanged = known is not None and known[0] == _signature(st)  # its other entries still hold
    if found is not None:
        _remember(path, key, found.offset, len(found.raw), keep=unchanged)
    else:
        _remember(path, None, 0, 0, keep=unchanged)
    return found, data


def _line_bytes(path: Path, record: dict, serialized: bytes) -> bytes:
    """The line to place for *record*: a still-changing Receipt gets spare room."""
    headroom = 0
    if path.name == "runs.jsonl":
        from openshard.history.receipt_signing import seal_point, stored_receipt_id

        if stored_receipt_id(record) is not None and seal_point(record) is None:
            headroom = max(_MIN_HEADROOM_BYTES, len(serialized))
    return serialized + b" " * headroom + _EOL


def _redo_path(path: Path) -> Path:
    return path.with_name(path.name + _REDO_SUFFIX)


def _write_durably(fh, data: bytes) -> None:
    fh.write(data)
    fh.flush()
    os.fsync(fh.fileno())


def _overwrite_line(path: Path, offset: int, payload: bytes) -> None:
    """Overwrite one line's bytes in place, journaled first so a crash is redone.

    *payload* has exactly the old line's length and ends its line, so no other
    byte of the file moves or changes.
    """
    st = path.stat()
    header = json.dumps({
        "offset": offset, "length": len(payload), "dev": st.st_dev, "ino": st.st_ino,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }).encode("ascii")
    redo = _redo_path(path)
    with redo.open("wb") as fh:
        _write_durably(fh, header + b"\n" + payload)
    with path.open("r+b") as fh:
        fh.seek(offset)
        _write_durably(fh, payload)
    redo.unlink()


def _redo_interrupted_write(path: Path) -> None:
    """Finish an in-place overwrite a crash interrupted (lock held). Cheap when none.

    The journal is applied only when it is whole (its digest matches) and the
    file is still the one it was written for, with a line boundary on both
    sides of the slot; otherwise the file was never touched or has since been
    replaced, and the journal is dropped.
    """
    redo = _redo_path(path)
    try:
        blob = redo.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return
    head, _, payload = blob.partition(b"\n")
    try:
        meta = json.loads(head)
        offset, length = int(meta["offset"]), int(meta["length"])
        whole = len(payload) == length and hashlib.sha256(payload).hexdigest() == meta["sha256"]
    except (ValueError, KeyError, TypeError):
        whole = False
    if whole:
        try:
            with path.open("r+b") as fh:
                st = os.fstat(fh.fileno())
                fh.seek(max(offset - 1, 0))
                around = fh.read(length + (1 if offset else 0))
                if ((st.st_dev, st.st_ino) == (meta["dev"], meta["ino"]) and len(around) == length + (1 if offset else 0)
                        and around.endswith(b"\n") and (offset == 0 or around[:1] == b"\n")):
                    fh.seek(offset)
                    _write_durably(fh, payload)
        except FileNotFoundError:
            pass
    redo.unlink(missing_ok=True)


def _upsert_locked(
    path: Path,
    build: Callable[[Sequence[str], dict | None], dict],
    match: Callable[[dict], bool],
    key: str | None,
    serialized: bytes | None = None,
) -> tuple[dict, str]:
    """The upsert itself; the caller holds the lock (see :func:`upsert_jsonl_with`)."""
    found, data = _find_line(path, match, key)
    record = build(_LinesUnderLock(path), found.record if found is not None else None)
    new = serialized if serialized is not None else json.dumps(record).encode("utf-8")
    if found is not None:
        end = len(found.raw) - (2 if found.raw.endswith(b"\r\n") else 1)
        if found.raw.endswith(b"\n") and len(new) <= end:
            # Fits: overwrite in place, padded to the old length.
            _overwrite_line(path, found.offset, new + b" " * (end - len(new)) + found.raw[end:])
            _remember(path, key, found.offset, len(found.raw), keep=True)
            return record, "replaced"
        if data is None:
            data = path.read_bytes()
        line = _line_bytes(path, record, new)
        _atomic_replace(path, data[:found.offset] + line + data[found.offset + len(found.raw):])
        _remember(path, key, found.offset, len(line), keep=False)
        return record, "replaced"
    line = _line_bytes(path, record, new)
    torn = _last_line_unterminated(path)
    with path.open("ab") as fh:
        if torn:
            fh.write(_EOL)
        offset = fh.tell()
        _write_durably(fh, line)
    _remember(path, key, offset, len(line), keep=True)
    return record, "appended"


@_learning_update
@_seal_on_write(lambda args, kwargs, result: args[0] if args else kwargs.get("record"))
def upsert_jsonl(
    path: Path,
    record: dict,
    match: Callable[[dict], bool],
    *,
    timeout: float | None = None,
    key: str | None = None,
) -> str:
    """Replace the first record satisfying *match* with *record*, else append it.

    Returns ``"replaced"`` or ``"appended"``. Lines that are blank, malformed,
    or do not match are preserved byte-for-byte -- this never re-serializes or
    re-coerces any record other than the one being written, and never moves a
    line. A replace overwrites the line in place when the record fits it
    (journaled, see the module docstring), else is a crash-safe temp+rename
    rewrite; an append is a plain locked append. All happen under the same
    sidecar lock as :func:`append_jsonl`, and the whole read-decide-write
    sequence is one critical section, so two concurrent upserts for the same
    key can never both append.

    *key*, when given, is a string whose JSON encoding every line that can
    satisfy *match* contains (a record's ``receipt_id``): the line is then
    found without parsing the rest of the history, and remembered.

    ``timeout`` bounds the lock wait (see :func:`history_file_lock`); raises
    :class:`LockTimeoutError` on expiry instead of blocking forever.
    """
    path = Path(path)
    serialized = json.dumps(record).encode("utf-8")  # serialize BEFORE locking
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path, timeout=timeout):
        return _upsert_locked(path, lambda _lines, _matched: record, match, key, serialized)[1]


@_learning_update
@_seal_on_write(lambda args, kwargs, result: result[0] if isinstance(result, tuple) else None)
def upsert_jsonl_with(
    path: Path,
    build: Callable[[Sequence[str], dict | None], dict],
    match: Callable[[dict], bool],
    *,
    timeout: float | None = None,
    key: str | None = None,
) -> tuple[dict, str]:
    """Build and upsert one record while holding the history lock.

    This is the position-dependent counterpart to upsert_jsonl. The first
    parsed record satisfying match is found under the lock and passed to
    build(existing_lines, matched_record), where existing_lines are the
    current raw lines (read only if the builder uses them). The builder
    therefore sees the exact history state that will be written and can safely
    finalize an identity derived from history position.

    Returns (record_as_written, "replaced"|"appended"). Blank, malformed and
    unrelated lines are preserved byte-for-byte. The builder runs under the
    lock, so it must stay cheap and must not re-enter this module on path.
    *key* is as for :func:`upsert_jsonl`.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path, timeout=timeout):
        return _upsert_locked(path, build, match, key)


@_learning_update
@_seal_on_write(lambda args, kwargs, result: result, amendment=True)
def amend_last_jsonl(
    path: Path, transform: Callable[[dict], dict], *, timeout: float | None = None
) -> dict | None:
    """Replace the last well-formed record in *path* with ``transform(record)``.

    The "last record" is the last line that parses to a JSON object -- the
    same rule :func:`openshard.history.store.load_history` uses for its final
    element, so an amendment always lands on the record readers report as
    latest. Blank and malformed lines (including any *after* that record) are
    preserved verbatim and never re-serialized. Returns the record as written,
    or ``None`` when the file is missing or holds no well-formed record (the
    file is then left untouched).

    The read, the transform and the crash-safe temp+rename rewrite happen in
    one critical section under the same sidecar lock as :func:`append_jsonl`,
    so a concurrent append cannot be lost and two amendments cannot both read
    the same pre-amendment line. ``transform`` must return a JSON-serializable
    dict; it runs under the lock, so keep it cheap and never re-enter this
    module on the same *path* from inside it.
    """
    path = Path(path)
    if not path.exists():
        return None  # nothing to amend; take no lock and create no directory
    with _locked(path, timeout=timeout):
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            existing = fh.read().splitlines(keepends=True)
        for i in range(len(existing) - 1, -1, -1):
            stripped = existing[i].strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            amended = transform(parsed)
            existing[i] = json.dumps(amended) + "\n"
            _atomic_replace(path, "".join(existing))
            return amended
        return None


def _last_line_unterminated(path: Path) -> bool:
    """True when *path* is non-empty and does not end with a newline. Reads one byte."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                return False
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except FileNotFoundError:
        return False


@_learning_update
@_seal_on_write(lambda args, kwargs, result: args[0] if args else kwargs.get("record"))
def append_jsonl(path: Path, record: dict) -> None:
    """Append one record to *path* as a single locked, fsync'd JSON line.

    Serialization happens before the lock is acquired and before the file is
    opened, so a non-serializable record raises with nothing written and no
    lock taken. A last line left without its newline (a writer killed
    mid-write) is ended first, so the new record never joins it and is lost
    with it.
    """
    path = Path(path)
    line = json.dumps(record) + "\n"  # serialize BEFORE locking / opening
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        torn = _last_line_unterminated(path)
        with path.open("a", encoding="utf-8") as fh:
            if torn:
                fh.write("\n")
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


@_learning_update
@_seal_on_write(lambda args, kwargs, result: result)
def append_jsonl_with(path: Path, build: Callable[[list[str]], dict]) -> dict:
    """Append the record ``build(existing_lines)`` returns, deciding it under the lock.

    For writers whose record depends on what the file already holds (e.g. a
    position-derived identity that must not collide with a record another
    process appended a moment earlier): the read, ``build`` and the append are
    one critical section under the same sidecar lock as :func:`append_jsonl`.
    ``build`` receives the raw existing lines (possibly blank or malformed),
    must return a JSON-serializable dict, runs under the lock (keep it cheap)
    and must never re-enter this module on the same *path*. Returns the record
    as written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        existing: list[str] = []
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                existing = fh.read().splitlines(keepends=True)
        record = build(existing)
        line = json.dumps(record) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            if existing and not existing[-1].endswith("\n"):
                fh.write("\n")
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        return record


@_learning_update
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
    with _locked(path):
        _atomic_replace(path, blob)  # atomic rename over target
