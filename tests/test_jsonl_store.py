"""Tests for the locked JSONL store (openshard.history.jsonl_store)."""

from __future__ import annotations

import json
import multiprocessing as mp
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from openshard.history.jsonl_store import (
    LockTimeoutError,
    _file_lock,
    _intra_process_lock,
    _lock_path_for,
    append_jsonl,
    history_file_lock,
    upsert_jsonl,
    write_jsonl,
)

# Records per writer in the concurrency tests.
_RECORDS_PER_WRITER = 50


# --- Top-level worker (must be importable / picklable for Windows `spawn`). ---
def _append_worker(args: tuple[str, int, int]) -> None:
    """Append ``count`` records tagged with ``proc`` to the shared file."""
    path_str, proc, count = args
    path = Path(path_str)
    for i in range(count):
        append_jsonl(path, {"id": f"proc_{proc}_rec_{i}", "proc": proc, "i": i})


def _read_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_locked_append_writes_well_formed_lines(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    append_jsonl(path, {"a": 1})
    append_jsonl(path, {"a": 2})

    lines = _read_lines(path)
    assert [json.loads(ln) for ln in lines] == [{"a": 1}, {"a": 2}]
    # Sidecar lock file is created alongside the data file.
    assert _lock_path_for(path).exists()


def test_lock_released_after_serialization_error(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    append_jsonl(path, {"ok": 1})

    # A non-serializable record must raise and write nothing.
    with pytest.raises(TypeError):
        append_jsonl(path, {"bad": object()})

    # The failed write left no partial line behind...
    assert [json.loads(ln) for ln in _read_lines(path)] == [{"ok": 1}]

    # ...and the lock was not leaked: a subsequent normal write still succeeds.
    append_jsonl(path, {"ok": 2})
    assert [json.loads(ln) for ln in _read_lines(path)] == [{"ok": 1}, {"ok": 2}]


def test_cross_process_appends_do_not_interleave(tmp_path: Path) -> None:
    """The load-bearing test: real processes contending for one file."""
    path = tmp_path / "shared.jsonl"
    n_procs = 4
    jobs = [(str(path), proc, _RECORDS_PER_WRITER) for proc in range(n_procs)]

    ctx = mp.get_context("spawn")  # spawn-safe everywhere, matches Windows
    with ctx.Pool(processes=n_procs) as pool:
        pool.map(_append_worker, jobs)

    lines = _read_lines(path)
    # Exact line count: nothing lost, nothing duplicated by torn writes.
    assert len(lines) == n_procs * _RECORDS_PER_WRITER
    # Every line is intact JSON (no interleaving mid-line).
    ids = [json.loads(ln)["id"] for ln in lines]
    # Every expected id appears exactly once.
    expected = {
        f"proc_{p}_rec_{i}"
        for p in range(n_procs)
        for i in range(_RECORDS_PER_WRITER)
    }
    counts = Counter(ids)
    assert set(counts) == expected
    assert all(c == 1 for c in counts.values())


def test_thread_appends_do_not_interleave(tmp_path: Path) -> None:
    """Secondary in-process smoke check of the same property."""
    path = tmp_path / "shared.jsonl"
    n_threads = 8
    with ThreadPoolExecutor(max_workers=n_threads) as ex:
        list(
            ex.map(
                _append_worker,
                [(str(path), t, _RECORDS_PER_WRITER) for t in range(n_threads)],
            )
        )

    lines = _read_lines(path)
    assert len(lines) == n_threads * _RECORDS_PER_WRITER
    assert all(isinstance(json.loads(ln), dict) for ln in lines)


def test_thread_appends_do_not_interleave_under_repetition(tmp_path: Path) -> None:
    """Same property as above, repeated: a single lucky pass isn't enough
    confidence for a lock-contention regression (this is what caught the
    Windows same-process msvcrt.locking() deadlock-avoidance bug -- see
    test_file_lock_never_has_more_than_one_thread_inside_at_once for the
    structural regression test, and _intra_process_lock's docstring in
    jsonl_store.py for the full root-cause explanation)."""
    n_threads = 6
    records_per_writer = 10
    for rep in range(15):
        path = tmp_path / f"shared_{rep}.jsonl"
        with ThreadPoolExecutor(max_workers=n_threads) as ex:
            list(
                ex.map(
                    _append_worker,
                    [(str(path), t, records_per_writer) for t in range(n_threads)],
                )
            )
        lines = _read_lines(path)
        assert len(lines) == n_threads * records_per_writer, f"rep {rep}"
        assert all(isinstance(json.loads(ln), dict) for ln in lines), f"rep {rep}"


def test_file_lock_never_has_more_than_one_thread_inside_at_once(tmp_path: Path) -> None:
    """Structural regression test for the Windows same-process locking bug.

    msvcrt.locking() is scoped per-process rather than per-handle (unlike
    POSIX fcntl.flock, scoped to the open file description), so two threads
    of the same process each opening their own handle and racing for the
    same byte range is exactly the scenario that tripped the Windows CRT's
    deadlock-avoidance check (OSError, errno 36) even though nothing was
    really deadlocked. The fix serializes same-process callers with an
    in-process lock (_intra_process_lock) before either thread ever touches
    the OS-level lock, so at most one thread of this process can be "inside"
    _file_lock for a given path at any instant -- this test asserts that
    invariant directly and platform-independently (it does not need
    msvcrt/Windows to fail if the invariant is ever broken: two threads
    inside at once here means the same race this fix closed has reopened)."""
    lock_path = tmp_path / "buffer.json.lock"
    n_threads = 16
    iterations_per_thread = 20
    inside = 0
    max_inside_seen = 0
    guard = threading.Lock()

    def worker() -> None:
        nonlocal inside, max_inside_seen
        for _ in range(iterations_per_thread):
            with _file_lock(lock_path):
                with guard:
                    inside += 1
                    max_inside_seen = max(max_inside_seen, inside)
                # A small window with the lock held and nothing else
                # synchronizing gives any race the chance to manifest.
                time.sleep(0.001)
                with guard:
                    inside -= 1

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a worker thread hung -- possible lock leak/deadlock"

    assert max_inside_seen == 1
    assert inside == 0


def test_intra_process_lock_registry_is_keyed_by_path(tmp_path: Path) -> None:
    """Same path -> same lock object (real serialization); different paths
    -> different lock objects (unrelated files never serialize against
    each other)."""
    a = tmp_path / "a.jsonl.lock"
    b = tmp_path / "b.jsonl.lock"
    assert _intra_process_lock(a) is _intra_process_lock(a)
    assert _intra_process_lock(a) is not _intra_process_lock(b)
    assert isinstance(_intra_process_lock(a), type(threading.Lock()))


def test_append_round_trips_through_real_reader(tmp_path: Path) -> None:
    """Format is unchanged: records written here load via the real reader."""
    import openshard.history.run_checkpoints as rc

    path = tmp_path / ".openshard" / "run_checkpoints.jsonl"
    events = [
        rc.NativeRunCheckpointEvent(run_id="r1", stage="plan", status="started"),
        rc.NativeRunCheckpointEvent(run_id="r1", stage="final", status="passed"),
    ]
    for ev in events:
        append_jsonl(path, rc._event_to_dict(ev))

    # Patch cwd-relative path resolution by writing where the reader looks.
    loaded = [
        rc._dict_to_event(json.loads(ln)) for ln in _read_lines(path)
    ]
    assert [(e.run_id, e.stage, e.status) for e in loaded] == [
        ("r1", "plan", "started"),
        ("r1", "final", "passed"),
    ]


def test_write_jsonl_is_crash_safe_and_shares_lock(tmp_path: Path) -> None:
    path = tmp_path / "runs.jsonl"
    append_jsonl(path, {"n": 0})

    records = [{"n": 1}, {"n": 2}, {"n": 3}]
    write_jsonl(path, records)

    assert [json.loads(ln) for ln in _read_lines(path)] == records
    # No leftover temp file from the atomic-replace rewrite.
    assert not (tmp_path / "runs.jsonl.tmp").exists()
    # Append and rewrite resolve to the same lock path (mutual exclusion).
    assert _lock_path_for(path) == path.with_name("runs.jsonl.lock")


# --- PR7: bounded lock waits (Requirement 5) ---------------------------------


class _BackgroundHolder:
    """Holds a `_file_lock` on a background thread until told to release it."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._release = threading.Event()
        self._acquired = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with _file_lock(self.lock_path):
            self._acquired.set()
            self._release.wait(timeout=30)

    def start_and_wait_until_held(self) -> None:
        self._thread.start()
        assert self._acquired.wait(timeout=5), "background holder never acquired the lock"

    def release(self) -> None:
        self._release.set()
        self._thread.join(timeout=5)


def test_bounded_timeout_raises_promptly_when_lock_is_held(tmp_path: Path) -> None:
    lock_path = tmp_path / "buffer.json.lock"
    holder = _BackgroundHolder(lock_path)
    holder.start_and_wait_until_held()
    try:
        t0 = time.monotonic()
        with pytest.raises(LockTimeoutError):
            with _file_lock(lock_path, timeout=0.3):
                pass  # pragma: no cover - never reached
        elapsed = time.monotonic() - t0
        # Bounded: raises close to the requested timeout, never hangs, and
        # never fires (much) sooner than requested either.
        assert 0.25 <= elapsed < 5.0
    finally:
        holder.release()


def test_bounded_timeout_succeeds_once_contender_releases(tmp_path: Path) -> None:
    lock_path = tmp_path / "buffer.json.lock"
    holder = _BackgroundHolder(lock_path)
    holder.start_and_wait_until_held()
    releaser = threading.Timer(0.2, holder.release)
    releaser.start()
    try:
        with _file_lock(lock_path, timeout=5.0):
            pass  # acquired without raising once the holder let go
    finally:
        releaser.cancel()


def test_default_timeout_none_still_blocks_until_released(tmp_path: Path) -> None:
    """Every existing caller's unbounded-wait behavior is unchanged by default."""
    lock_path = tmp_path / "buffer.json.lock"
    holder = _BackgroundHolder(lock_path)
    holder.start_and_wait_until_held()
    releaser = threading.Timer(0.2, holder.release)
    releaser.start()
    try:
        t0 = time.monotonic()
        with _file_lock(lock_path):  # timeout=None: blocks, never raises
            elapsed = time.monotonic() - t0
        assert elapsed >= 0.15
    finally:
        releaser.cancel()


def test_history_file_lock_and_upsert_jsonl_accept_timeout_kwarg(tmp_path: Path) -> None:
    """Public wrappers plumb `timeout` through to the same bounded wait."""
    path = tmp_path / "runs.jsonl"
    lock_path = _lock_path_for(path)
    holder = _BackgroundHolder(lock_path)
    holder.start_and_wait_until_held()
    try:
        with pytest.raises(LockTimeoutError):
            with history_file_lock(path, timeout=0.2):
                pass  # pragma: no cover - never reached
        with pytest.raises(LockTimeoutError):
            upsert_jsonl(path, {"n": 1}, lambda e: True, timeout=0.2)
    finally:
        holder.release()
    # Contention gone: the same calls now succeed normally.
    upsert_jsonl(path, {"n": 1}, lambda e: True, timeout=5.0)
    assert [json.loads(ln) for ln in _read_lines(path)] == [{"n": 1}]
