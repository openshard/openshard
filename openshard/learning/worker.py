"""Background learning worker: keeps the learning snapshot current, off the OSN startup path.

After a successful write to ``runs.jsonl`` (``jsonl_store``), ``schedule_update``
marks learning dirty and, unless a worker owns learning or is starting,
launches one detached interpreter. That worker:

* copies the history under the same lock writers use, a bounded chunk at a
  time, and closes the file before doing any work (on Windows an open handle
  would make a writer's atomic replace fail);
* re-derives everything from that one consistent copy with the Learning Loop's
  own functions (``learning.snapshot.build_snapshot``), so every snapshot it
  publishes is complete by construction;
* publishes atomically, then repeats while newer writes arrived meanwhile.

What a history write pays: one tiny dirty-marker write and one non-blocking
lock attempt (a constant cost, independent of history size), plus, on the one
write that starts a worker, the process launch itself. While the worker copies
a chunk it holds the history lock, so a concurrent writer can wait for that
one bounded read. History durability is untouched: the write is complete
before any of this runs.

Many writes coalesce into one worker: the owner lock is tried without waiting,
a launched worker stays owner for ``SETTLE_SECONDS`` after its last pass (so a
stream of writes is one process, see ``update_snapshot``),
a launch reservation (token and pid) covers the moment before a new interpreter
holds it, and a dirty *generation* (not a flag) means a write that lands
mid-pass is never lost.

Self-healing, without waiting for another history write:

* a failed pass (a Windows reader holding the snapshot during the replace, a
  copy that kept changing) is retried a bounded number of times with backoff;
* a reservation whose process is gone (it died before taking ownership) is
  reclaimed at once rather than blocking launches;
* ``nudge`` runs after each OSN startup lookup and starts a worker when the
  snapshot is missing, unusable, or behind the history it was derived from
  (one ``stat``), which also covers a worker that crashed mid-pass.

Each trigger starts at most one process, and only when no worker owns
learning, so there is no retry loop and no process storm. Receipts are never
touched. ``OPENSHARD_LEARNING_WORKER=0`` disables background refresh (OSN then
reports learning as unavailable rather than reading history itself).
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from openshard.history.jsonl_store import history_file_lock

DIRTY = "learning.dirty"
LAUNCH = "learning.launch"
OWNER = "learning-worker"
ENV_SWITCH = "OPENSHARD_LEARNING_WORKER"
LAUNCH_RESERVATION_SECONDS = 60  # upper bound even if a reserved pid is reused
OWNER_WAIT_SECONDS = 2.0  # a launched worker waits out a launcher's brief section
CHUNK_BYTES = 4 * 1024 * 1024
CHUNK_LOCK_WAIT_SECONDS = 2.0
MAX_COPY_ATTEMPTS = 5
PASS_RETRY_DELAYS = (0.1, 0.3, 0.9)  # then the pass gives up; ``nudge`` or the next write retries
SETTLE_SECONDS = 2.0  # a launched worker stays owner this long after its last pass (debounce)
SETTLE_POLL_SECONDS = 0.1
# A launched worker's pacing under a steady stream of writes: passes are at
# least this far apart (or twice the last pass, if longer), and it hands over to
# a fresh process (which runs the currently installed code) after this long or
# this many passes.
MIN_PASS_GAP_SECONDS = 10.0
PASS_GAP_FACTOR = 2.0
MAX_LIFETIME_SECONDS = 600.0
MAX_PASSES = 50

_clock = time.monotonic  # injectable in tests
_sleep = time.sleep


class RefreshFailed(RuntimeError):
    """A pass kept failing after its bounded retries; the generation stays pending."""


def enabled() -> bool:
    """Background refresh is on unless ``OPENSHARD_LEARNING_WORKER`` is 0/false/off/no."""
    return os.environ.get(ENV_SWITCH, "").strip().lower() not in ("0", "false", "off", "no")


def _stamp(path: Path) -> str:
    """The dirty generation: changes on every ``schedule_update``, even within one clock tick."""
    try:
        return f"{path.stat().st_mtime_ns}:{path.read_text(encoding='ascii')}"
    except OSError:
        return ""


# --------------------------------------------------------------------------
# Launching
# --------------------------------------------------------------------------


CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def worker_argv(runs: Path, token: str) -> list[str]:
    """The worker's command line.

    ``-P`` (safe path, Python 3.11+): ``-m`` would otherwise put the working
    directory first on ``sys.path``, and a cloned repository could ship its
    own ``openshard`` package there. The working directory is also a trusted
    one (``_trusted_cwd``), never the repository.
    """
    return [sys.executable, "-P", "-m", "openshard.learning.worker", str(runs.resolve()), token]


def _trusted_cwd() -> str:
    """A working directory the repository cannot control: the OpenShard home
    (as the capture service uses), else the interpreter's own directory."""
    from openshard.util.home import openshard_home

    home = os.path.abspath(openshard_home())
    try:
        os.makedirs(home, exist_ok=True)
        return home
    except OSError:
        return os.path.dirname(os.path.abspath(sys.executable))


def _detached_kwargs() -> dict[str, Any]:
    """Same conventions as OpenShard's other detached processes.

    Normal priority on purpose: a below-normal worker is starved whenever the
    machine is busy (a build, a test run), which leaves learning stale.
    """
    kwargs: dict[str, Any] = {
        "cwd": _trusted_cwd(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        # CREATE_NO_WINDOW (not DETACHED_PROCESS) so a venv launcher never pops
        # a console; a new process group so a console control event aimed at
        # the writer (Ctrl+C in a hook's console) never reaches the worker;
        # breakaway so a job that kills its processes when a hook ends does not
        # take the worker with it (``_spawn`` retries without it where the job
        # forbids breakaway).
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | CREATE_BREAKAWAY_FROM_JOB
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _spawn(argv: list[str], **kwargs: Any) -> Any:
    flags = kwargs.get("creationflags", 0)
    if flags & CREATE_BREAKAWAY_FROM_JOB:
        try:
            return subprocess.Popen(argv, **kwargs)
        except OSError:
            # Access denied: the job this process runs in does not allow
            # breakaway. The worker then shares the job, as before.
            kwargs = {**kwargs, "creationflags": flags & ~CREATE_BREAKAWAY_FROM_JOB}
    return subprocess.Popen(argv, **kwargs)


def _reservation_live(store: Path) -> bool:
    """True while a launched worker may still be starting (its process is alive)."""
    path = store / LAUNCH
    try:
        text = path.read_text(encoding="ascii")
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    if age >= LAUNCH_RESERVATION_SECONDS:
        return False
    _token, _, pid = text.partition(" ")
    if not pid.isdigit():
        return True  # being written; the age bound still applies
    from openshard.util.process import pid_alive

    return pid_alive(int(pid))


def _release_launch(store: Path, token: str | None) -> None:
    if token is None:
        return
    path = store / LAUNCH
    try:
        if path.read_text(encoding="ascii").partition(" ")[0] == token:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _launch_if_idle(runs: Path) -> bool:
    """Start one worker unless one owns learning or is starting. Never waits.

    Raises ``TimeoutError`` (a worker owns learning) or ``OSError``; callers
    swallow both.
    """
    store = runs.parent
    with history_file_lock(store / OWNER, timeout=0):
        if _reservation_live(store):
            return False  # a worker is starting; it reads the generation already written
        token = uuid.uuid4().hex
        proc = _spawn(worker_argv(runs, token), **_detached_kwargs())
        # Written while still holding the owner lock: the worker cannot take
        # ownership (and remove this) before it exists.
        (store / LAUNCH).write_text(f"{token} {getattr(proc, 'pid', '')}", encoding="ascii")
        return True


def schedule_update(path: Path) -> None:
    """Mark learning dirty and start a worker if none is running. Never raises, never waits."""
    if path.name != "runs.jsonl" or not enabled():
        return
    try:
        (path.parent / DIRTY).write_text(uuid.uuid4().hex, encoding="ascii")
        _launch_if_idle(path)
    except (OSError, ValueError, TimeoutError):
        pass  # a busy owner lock means a worker is running; capture never depends on learning


def nudge(runs: Path, snapshot: Any) -> bool:
    """After an OSN startup lookup: start a worker if the snapshot is missing, unusable or behind.

    "Behind" is one ``stat``: the history no longer has the size and
    modification time the snapshot was derived from. A late lookup or an
    oversized snapshot is not rebuilt from here. Never raises, never waits.
    """
    if snapshot is None or not enabled():
        return False
    try:
        from openshard.learning import snapshot as snap

        if snapshot.status == snap.AVAILABLE:
            # No recorded source (a snapshot that predates it) cannot be shown to
            # be behind: treat it as current rather than relaunch on every run.
            if snapshot.source is None or snapshot.source == snap.stat_key(runs):
                return False
        elif snapshot.status not in snap.REBUILD_STATUSES:
            return False
        return _launch_if_idle(runs)
    except Exception:
        return False


# --------------------------------------------------------------------------
# The worker
# --------------------------------------------------------------------------


def copy_history_with_source(runs: Path) -> tuple[bytes, list[int] | None]:
    """A consistent copy of *runs* and the ``[size, mtime_ns]`` it was copied at.

    Read in bounded chunks under the history lock; the file is opened only
    while the lock is held and closed before it is released. A copy restarts
    if the file was replaced or truncated between chunks; appends made after
    the copy began are left for the next pass.
    """
    for _attempt in range(MAX_COPY_ATTEMPTS):
        parts: list[bytes] = []
        identity: tuple[int, int] | None = None
        source: list[int] | None = None
        size = offset = 0
        while True:
            with history_file_lock(runs, timeout=CHUNK_LOCK_WAIT_SECONDS):
                try:
                    fh = runs.open("rb")
                except FileNotFoundError:
                    if identity is None:
                        return b"", None
                    break  # removed mid-copy: start over
                with fh:
                    st = os.fstat(fh.fileno())
                    current = (st.st_ino, st.st_dev)
                    if identity is None:
                        identity, size, source = current, st.st_size, [st.st_size, st.st_mtime_ns]
                    elif current != identity or st.st_size < offset:
                        break  # replaced or truncated mid-copy: start over
                    fh.seek(offset)
                    data = fh.read(min(CHUNK_BYTES, size - offset))
            parts.append(data)
            offset += len(data)
            if offset >= size or not data:
                return b"".join(parts), source
    raise TimeoutError("history kept changing during the learning copy")


def _strict_utf8_head(data: bytes) -> bool:
    """True when the lines ``load_history_evidence`` reads (the first
    ``MAX_ENTRIES_SCANNED``) decode as strict UTF-8, as that loader requires."""
    from openshard.routing.adaptive.history_evidence import MAX_ENTRIES_SCANNED

    head = data.split(b"\n", MAX_ENTRIES_SCANNED)[:MAX_ENTRIES_SCANNED]
    try:
        for line in head:
            line.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def copy_history(runs: Path) -> bytes:
    return copy_history_with_source(runs)[0]


def refresh_snapshot(runs: Path) -> str:
    """Copy, derive and publish one complete snapshot for *runs*; returns its id."""
    from datetime import UTC, datetime

    from openshard.learning.signals import repo_key
    from openshard.learning.snapshot import build_snapshot, identity_basis, publish_snapshot
    from openshard.osn.routing import HARNESS

    store = runs.parent
    root = store.parent
    data, source = copy_history_with_source(runs)
    identity = identity_basis(root)  # before the identity itself: a later change only invalidates
    # Decoded as the live readers decode: leniently for signals (``read_entries``);
    # routing history only if strict UTF-8 where ``load_history_evidence`` reads.
    lines = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="replace").readlines()
    snapshot = build_snapshot(lines, repo=repo_key(root), harness=HARNESS, now=datetime.now(UTC),
                              identity=identity, source=source, routing_readable=_strict_utf8_head(data))
    return publish_snapshot(store, snapshot)


def _refresh_with_retries(runs: Path) -> str:
    for delay in (*PASS_RETRY_DELAYS, None):
        try:
            return refresh_snapshot(runs)
        except Exception as exc:
            if delay is None:
                raise RefreshFailed(f"{type(exc).__name__}: {exc}") from exc
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _changed_within(dirty: Path, generation: str, seconds: float) -> bool:
    """True once the dirty generation differs from *generation*, checking for up to *seconds*."""
    deadline = _clock() + seconds
    while True:
        if _stamp(dirty) != generation:
            return True
        remaining = deadline - _clock()
        if remaining <= 0:
            return False
        _sleep(min(SETTLE_POLL_SECONDS, remaining))


def update_snapshot(runs: Path, *, launch_token: str | None = None, settle_seconds: float = 0.0,
                    min_gap_seconds: float = 0.0, max_lifetime_seconds: float | None = None,
                    max_passes: int | None = None) -> bool:
    """Become the one owner and publish until no newer write is pending.

    *settle_seconds* (the launched worker uses ``SETTLE_SECONDS``) is the
    debounce: after its last pass the owner keeps the owner lock and watches
    the dirty generation that long. A write in that window cannot launch a
    second process (the owner lock is held), and is not lost either: the
    owner sees its generation and runs another pass. So a stream of history
    writes costs one process, not one per write, and every generation is
    still published by this owner, by the hand-off below, or by ``nudge``.

    Bounded under a steady stream of writes (the launched worker passes the
    module defaults): consecutive passes are at least *min_gap_seconds* apart,
    or ``PASS_GAP_FACTOR`` times the last pass if that is longer, so one
    process never re-derives back to back; and after *max_lifetime_seconds* or
    *max_passes* the owner stops with the newest generation still pending and
    the hand-off below starts a fresh worker, which also picks up an upgraded
    OpenShard. The gap is only ever waited while newer writes are pending.

    Returns False when another owner holds the lock (it will see this
    generation). Raises ``RefreshFailed`` when a pass keeps failing after its
    bounded retries; the generation then stays pending for ``nudge`` or the
    next write.
    """
    runs = runs.resolve()
    store = runs.parent
    generation: str | None = None
    try:
        with history_file_lock(store / OWNER, timeout=OWNER_WAIT_SECONDS if launch_token else 0):
            _release_launch(store, launch_token)  # started: the owner lock now says a worker runs
            started, passes = _clock(), 0
            while True:
                generation = _stamp(store / DIRTY)
                pass_start = _clock()
                _refresh_with_retries(runs)
                pass_end, passes = _clock(), passes + 1
                if not _changed_within(store / DIRTY, generation, settle_seconds):
                    break  # nothing newer: done
                if ((max_passes is not None and passes >= max_passes)
                        or (max_lifetime_seconds is not None and _clock() - started >= max_lifetime_seconds)):
                    break  # newer writes pending: handed to a fresh worker below
                if min_gap_seconds > 0:
                    gap = max(min_gap_seconds, PASS_GAP_FACTOR * (pass_end - pass_start))
                    wait = pass_end + gap - _clock()
                    if max_lifetime_seconds is not None:
                        wait = min(wait, started + max_lifetime_seconds - _clock())
                    if wait > 0:
                        _sleep(wait)
    except TimeoutError:
        return False
    finally:
        _release_launch(store, launch_token)
    # A write that marked learning dirty after the last check, while this owner
    # still held the lock, could not start a worker. Hand it to a new one.
    if generation != _stamp(store / DIRTY):
        try:
            _launch_if_idle(runs)
        except (OSError, TimeoutError):
            pass
    return True


def main(argv: list[str]) -> None:
    """The launched worker: ``worker_argv`` gives it the history path and its launch token."""
    try:
        update_snapshot(Path(argv[0]), launch_token=argv[1] if len(argv) > 1 else None,
                        settle_seconds=SETTLE_SECONDS, min_gap_seconds=MIN_PASS_GAP_SECONDS,
                        max_lifetime_seconds=MAX_LIFETIME_SECONDS, max_passes=MAX_PASSES)
    except Exception:
        pass  # detached and best effort: the generation stays pending for nudge or the next write


if __name__ == "__main__":
    main(sys.argv[1:])
