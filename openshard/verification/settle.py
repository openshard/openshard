"""Run the opt-in working-tree re-run once a Task settles, not after every turn.

With ``post_session_verify: safe`` a per-turn Receipt (docs/per-turn-receipts.md)
used to start ``openshard verify`` as soon as a turn that changed files
sealed: a chat of ten small turns ran the suite ten times while the agent was
still working. Now a sealed turn that changed files only *notes* its Receipt
as the session's pending re-run, and the run starts when the Task settles:

* no new prompt in that agent session for ``post_session_verify_settle_minutes``
  (default 10) after its latest sealed turn, or
* the session's next sealed turn belongs to a different Task (the branch
  changed), which ends the previous one at once.

A turn whose Receipt names its resulting commit is verified on commit
(``auto.schedule_commit_verify``, unchanged); it clears the pending working-tree
run, which that commit supersedes. Never re-run the same thing twice: a
pending Receipt starts at most one settle run, and a clean checkout whose HEAD
already has an OpenShard re-run on record is not run again.

Due checks run from the capture service's sync timer and after any later
sealed turn or session end in the repository (inline hooks). Never raises.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SETTLE_MINUTES_KEY = "post_session_verify_settle_minutes"
DEFAULT_SETTLE_MINUTES = 10.0
_DIR = "verify-requests"
_PENDING = "settle-"
_DONE = "settled-"


def settle_seconds(repo_root: Path) -> float:
    """The quiet period after a sealed turn, from config (minutes, >= 0), else the default."""
    try:
        path = repo_root / ".openshard" / "config.yml"
        if path.is_file() and SETTLE_MINUTES_KEY.encode() in path.read_bytes():
            from openshard.config.settings import load_config_safe

            config, valid, _ = load_config_safe(cwd=repo_root)
            value = config.get(SETTLE_MINUTES_KEY) if valid else None
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                return float(value) * 60
    except (OSError, ValueError, TypeError):
        pass
    return DEFAULT_SETTLE_MINUTES * 60


def _safe(raw: object, cap: int = 64) -> str:
    return "".join(ch for ch in str(raw) if ch.isalnum() or ch in "_-")[:cap]


def _session(entry: dict) -> tuple[str, str] | None:
    capture = entry.get("capture")
    if not isinstance(capture, dict):
        return None
    sid, agent = capture.get("session_id"), capture.get("agent")
    return (sid, agent) if isinstance(sid, str) and sid and isinstance(agent, str) and agent else None


def _pending_path(repo_root: Path, session: tuple[str, str]) -> Path:
    return repo_root / ".openshard" / _DIR / f"{_PENDING}{_safe(session[1], 32)}-{_safe(session[0])}.json"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("receipt_ref"), str) else None


def _write(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def note_sealed_turn(repo_root: Path, entry: dict, *, has_commit: bool, now: float | None = None,
                     spawner: Any = None) -> bool:
    """Record a sealed turn against its session's pending re-run. Returns whether a run started now.

    Called only with ``post_session_verify: safe`` on. A turn of a new Task
    first settles the previous Task's pending run (started now).
    """
    from openshard.verification.auto import sealed_turn_changed_files

    session = _session(entry)
    ref = entry.get("receipt_id") or entry.get("run_id")
    if session is None or not isinstance(ref, str) or not ref:
        return False
    now = time.time() if now is None else now
    path = _pending_path(repo_root, session)
    pending = _read(path)
    task_id = entry.get("task_id") if isinstance(entry.get("task_id"), str) else None
    started = False
    if pending is not None and pending.get("task_id") != task_id:
        started = _start(repo_root, pending, spawner)  # the branch changed: the previous Task has settled
        _unlink(path)
        pending = None
    if has_commit:
        _unlink(path)  # verified on commit; that run supersedes the pending working-tree one
        return started
    if sealed_turn_changed_files(entry):
        pending = {"receipt_ref": ref, "task_id": task_id, "session_id": session[0], "agent": session[1]}
    if pending is not None:
        pending["sealed_at"] = now  # the quiet period restarts at every sealed turn of the session
        _write(path, pending)
    return started


def run_due(repo_root: Path, *, now: float | None = None, spawner: Any = None) -> int:
    """Start the pending re-run of every session quiet for the settle period. Returns how many started."""
    directory = repo_root / ".openshard" / _DIR
    try:
        paths = sorted(directory.glob(f"{_PENDING}*.json"))
    except OSError:
        return 0
    if not paths:
        return 0
    now = time.time() if now is None else now
    quiet = settle_seconds(repo_root)
    started = 0
    for path in paths:
        pending = _read(path)
        if pending is None:
            _unlink(path)
            continue
        sealed_at = pending.get("sealed_at")
        if not isinstance(sealed_at, (int, float)) or now - sealed_at < quiet or _turn_open(repo_root, pending):
            continue
        _unlink(path)
        started += _start(repo_root, pending, spawner)
    return started


def run_due_all(repos: Any) -> int:
    """``run_due`` for every repository *repos()* names (the capture service's known ones). Never raises."""
    started = 0
    try:
        for root in list(repos()):
            started += run_due(Path(root))
    except Exception:  # a timer thread: one bad repository must never stop Platform sync
        pass
    return started


def _turn_open(repo_root: Path, pending: dict) -> bool:
    """A new prompt opened a turn in the same session (its capture buffer exists): not settled."""
    try:
        from openshard.adapters.claude_hooks import buffer_path

        return buffer_path(repo_root, str(pending.get("session_id")), str(pending.get("agent"))).is_file()
    except (ImportError, OSError, ValueError):
        return False


def _start(repo_root: Path, pending: dict, spawner: Any) -> bool:
    """Start one safe-only working-tree run for the pending Receipt unless it would repeat one."""
    from openshard.verification.auto import auto_verify_enabled, schedule_post_session_verify
    from openshard.verification.post_session import tree_state

    ref = pending.get("receipt_ref")
    if not isinstance(ref, str) or not ref or not auto_verify_enabled(repo_root):
        return False
    done = repo_root / ".openshard" / _DIR / f"{_DONE}{_safe(ref)}"
    state = tree_state(repo_root)
    clean_head = state.head if state.dirty is False and state.head else None
    head_done = repo_root / ".openshard" / _DIR / f"{_DONE}{_safe(clean_head)}" if clean_head else None
    if done.exists() or (head_done is not None and (head_done.exists() or _head_attested(repo_root, clean_head))):
        return False
    if not schedule_post_session_verify(repo_root, {"receipt_id": ref}, spawner=spawner):
        return False
    for marker in (done, head_done):
        if marker is not None:
            _write(marker, {"receipt_ref": ref})
    return True


def _head_attested(repo_root: Path, head: str | None) -> bool:
    """An OpenShard re-run bound to exactly *head* (a clean checkout) is already recorded."""
    from openshard.verification.post_session import KIND_POST_SESSION, load_attestations

    if not head:
        return False
    for item in load_attestations(repo_root / ".openshard"):
        block = item.get("verification")
        if item.get("kind") == KIND_POST_SESSION and isinstance(block, dict) and block.get("artifact_sha") == head:
            return True  # bound: what ran was exactly this commit
    return False


__all__ = [
    "DEFAULT_SETTLE_MINUTES", "SETTLE_MINUTES_KEY", "note_sealed_turn", "run_due", "run_due_all", "settle_seconds",
]
