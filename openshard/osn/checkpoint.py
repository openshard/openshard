"""Durable run state for the OSN loop, and the rules for resuming it safely.

A run writes a checkpoint under ``.openshard/osn-runs/<run id>/`` at every
boundary the loop owns (started, planned, workers staged, each attempt done,
completed). The checkpoint carries what a later process needs to continue
the same run: the task and options, the repository's fingerprint at start,
the plan and role records, every finished attempt, the changed files'
bytes as verified or left by the last finished attempt, the model calls
made so far (with their cost provenance) and the budget ledger's counters.

``openshard osn resume <run id>`` continues from the last finished attempt
in a fresh isolated copy (never the old copy: whatever an interrupted
attempt wrote after the last checkpoint is discarded and said so). It
refuses when continuing would be unsafe or untruthful: no checkpoint, a
checkpoint this code cannot read, a run that already completed, a run whose
process is still alive, a repository whose HEAD or working tree changed
since the run started, or a verify command that differs.

Nothing here is a Receipt. A checkpoint is local, may hold local paths
(the sandbox), and is never synced; the Receipt of a resumed run records
that it was resumed and what was carried over.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CHECKPOINT_VERSION = 1
RUNS_DIR = "osn-runs"
CHECKPOINT_FILE = "checkpoint.json"
FILES_DIR = "files"

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_INTERRUPTED = "interrupted"

PHASE_STARTED = "started"
PHASE_PLANNED = "planned"
PHASE_WORKERS_STAGED = "workers_staged"
PHASE_ATTEMPT_DONE = "attempt_done"
PHASE_COMPLETED = "completed"

# Why a resume was refused (recorded on the refusal, never silently retried).
REFUSE_MISSING = "checkpoint_missing"
REFUSE_UNREADABLE = "checkpoint_unreadable"
REFUSE_VERSION = "checkpoint_version_unsupported"
REFUSE_COMPLETED = "run_already_completed"
REFUSE_PROCESS_ALIVE = "run_process_still_alive"
REFUSE_REPO_CHANGED = "repository_changed_since_run_started"
REFUSE_VERIFY_CHANGED = "verify_command_differs"
REFUSE_NO_PROGRESS = "nothing_to_resume"
REFUSE_FILES_MISSING = "checkpoint_files_missing"

# Why applying a completed run's verified result was refused.
REFUSE_NOT_COMPLETED = "run_not_completed"
REFUSE_NOT_VERIFIED = "run_not_verified"
REFUSE_NO_VERIFIED_FILES = "no_verified_files_retained"
REFUSE_ALREADY_APPLIED = "already_applied"
REFUSE_TARGET_CHANGED = "target_file_changed_since_run"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_stamp() -> str:
    """The checkpoint's UTC timestamp format, for records other modules attach to it."""
    return _now()


def runs_root(repo_root: Path) -> Path:
    return repo_root / ".openshard" / RUNS_DIR


def checkpoint_dir(repo_root: Path, run_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in run_id)[:120]
    return runs_root(repo_root) / safe


def repo_fingerprint(repo_root: Path) -> dict[str, Any]:
    """What the run's isolated copy was taken from: HEAD, and a digest of the working tree's differences.

    For a git repository the digest covers ``git status --porcelain`` and
    ``git diff HEAD`` (tracked changes and untracked paths), so a resume sees
    whether anything that would land in a fresh copy changed. Outside git it
    covers every file's path, size and mtime (``.openshard`` and ``.git``
    excluded).
    """
    from openshard.util.git import run_git

    head = run_git(repo_root, ["rev-parse", "HEAD"])
    if head:
        raw_status = run_git(repo_root, ["status", "--porcelain", "--untracked-files=all"]) or ""
        # OpenShard's own local state (runs, checkpoints, learning cache) changes
        # during a run and is never part of the isolated copy: not a repository change.
        status = "\n".join(
            line for line in raw_status.splitlines()
            if not line[3:].replace("\\", "/").lstrip('"').startswith(".openshard/")
        )
        diff = run_git(repo_root, ["diff", "HEAD", "--no-color", "--", ".", ":(exclude).openshard"]) or ""
        digest = hashlib.sha256((status + "\x00" + diff).encode("utf-8", "replace")).hexdigest()
        return {"kind": "git", "head": head.strip(), "worktree_sha256": digest,
                "dirty": bool(status.strip())}
    h = hashlib.sha256()
    for p in sorted(repo_root.rglob("*")):
        rel = p.relative_to(repo_root).as_posix()
        if rel.startswith((".openshard/", ".git/")) or not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(f"{rel}\x00{st.st_size}\x00{st.st_mtime_ns}\n".encode("utf-8", "replace"))
    return {"kind": "files", "head": None, "worktree_sha256": h.hexdigest(), "dirty": None}


@dataclass
class RunCheckpoint:
    run_id: str
    task: str
    verify_argv: list[str]
    args: dict[str, Any]  # the run's CLI options, enough to rebuild the same run
    repo: dict[str, Any]  # repo_fingerprint() at run start
    models: list[str]  # the escalation ladder the run was started with
    version: int = CHECKPOINT_VERSION
    status: str = STATUS_RUNNING
    phase: str = PHASE_STARTED
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    pid: int | None = None
    sandbox_path: str | None = None
    state: dict[str, Any] = field(default_factory=dict)  # the loop's resumable state (see loop.resume_state)
    usage: list[dict[str, Any]] = field(default_factory=list)  # AttemptUsage records so far
    budget: dict[str, Any] | None = None  # BudgetLedger counters so far
    files: dict[str, str | None] = field(default_factory=dict)  # changed file -> sha256 (None: absent)
    routing_record: dict[str, Any] | None = None
    receipt_id: str | None = None
    interrupted: dict[str, Any] | None = None
    resumed_from: list[str] = field(default_factory=list)  # earlier checkpoints this run continued
    # A completed, verified run that was not promoted keeps its verified bytes
    # (``files`` manifest, same hashes the Receipt verified) so `osn apply` can put
    # them in the repository later without re-running anything. ``base_files`` is
    # what the repository held at those paths when the run finished (None: absent),
    # so an apply refuses to overwrite a file someone changed since.
    verified_files: dict[str, str] | None = None
    base_files: dict[str, str | None] | None = None
    applied: dict[str, Any] | None = None  # the apply record once the result reached the repository

    @property
    def attempts_done(self) -> int:
        return len(self.state.get("attempts") or [])

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RunCheckpoint:
        if not isinstance(data, dict) or data.get("version") != CHECKPOINT_VERSION:
            raise ValueError(REFUSE_VERSION)
        fields: dict[str, Any] = {k: data.get(k) for k in cls.__dataclass_fields__ if k in data}
        return cls(**fields)


def write_checkpoint(repo_root: Path, cp: RunCheckpoint) -> Path:
    """Write atomically (a crash mid-write leaves the previous checkpoint intact)."""
    d = checkpoint_dir(repo_root, cp.run_id)
    d.mkdir(parents=True, exist_ok=True)
    cp.updated_at = _now()
    cp.pid = cp.pid or os.getpid()
    target = d / CHECKPOINT_FILE
    fd, tmp = tempfile.mkstemp(prefix="checkpoint-", suffix=".tmp", dir=str(d))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cp.to_json(), fh, indent=1, sort_keys=True, default=str)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return target


def read_checkpoint(repo_root: Path, run_id: str) -> RunCheckpoint:
    path = checkpoint_dir(repo_root, run_id) / CHECKPOINT_FILE
    if not path.is_file():
        raise FileNotFoundError(REFUSE_MISSING)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{REFUSE_UNREADABLE}: {exc}") from None
    return RunCheckpoint.from_json(data)


def list_checkpoints(repo_root: Path) -> list[RunCheckpoint]:
    root = runs_root(repo_root)
    out: list[RunCheckpoint] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        try:
            out.append(read_checkpoint(repo_root, d.name))
        except (FileNotFoundError, ValueError):
            continue
    return out


def snapshot_changed(sandbox: Path, changed: list[str], repo_root: Path, run_id: str) -> dict[str, str | None]:
    """Copy the changed files' bytes out of the isolated copy; the manifest says what each hash was."""
    files_dir = checkpoint_dir(repo_root, run_id) / FILES_DIR
    if files_dir.exists():
        shutil.rmtree(files_dir, ignore_errors=True)
    files_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str | None] = {}
    for rel in changed:
        src = sandbox / rel
        if not src.is_file():
            manifest[rel] = None  # deleted or never written: a resume removes it from the fresh copy
            continue
        data = src.read_bytes()
        dest = files_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        manifest[rel] = hashlib.sha256(data).hexdigest()
    return manifest


def restore_changed(repo_root: Path, run_id: str, manifest: dict[str, str | None], sandbox: Path) -> list[str]:
    """Put the checkpointed bytes into a fresh isolated copy. Returns the files restored.

    Raises ``FileNotFoundError(REFUSE_FILES_MISSING)`` when a file the manifest
    promises is not in the checkpoint or its bytes do not match the manifest.
    """
    files_dir = checkpoint_dir(repo_root, run_id) / FILES_DIR
    restored: list[str] = []
    for rel, digest in manifest.items():
        dest = sandbox / rel
        if digest is None:
            if dest.exists():
                dest.unlink()
            restored.append(rel)
            continue
        src = files_dir / rel
        if not src.is_file():
            raise FileNotFoundError(f"{REFUSE_FILES_MISSING}: {rel}")
        data = src.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise FileNotFoundError(f"{REFUSE_FILES_MISSING}: {rel} (bytes differ from the manifest)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        restored.append(rel)
    return restored


def pid_alive(pid: int | None) -> bool:
    if not pid or pid == os.getpid():
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))  # type: ignore[attr-defined]
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))  # type: ignore[attr-defined]
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Resumability:
    ok: bool
    reason: str | None = None
    detail: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "detail": self.detail}


def check_resumable(cp: RunCheckpoint, repo_root: Path, verify_argv: list[str] | None = None) -> Resumability:
    """Every refusal rule, in order. The first that applies wins; none applies means safe to resume."""
    if cp.version != CHECKPOINT_VERSION:
        return Resumability(False, REFUSE_VERSION, f"version {cp.version}")
    if cp.status == STATUS_COMPLETED:
        return Resumability(False, REFUSE_COMPLETED, f"Receipt {cp.receipt_id or 'recorded'}")
    if cp.status == STATUS_RUNNING and pid_alive(cp.pid):
        return Resumability(False, REFUSE_PROCESS_ALIVE, f"pid {cp.pid}")
    now = repo_fingerprint(repo_root)
    if now.get("head") != cp.repo.get("head") or now.get("worktree_sha256") != cp.repo.get("worktree_sha256"):
        return Resumability(
            False, REFUSE_REPO_CHANGED,
            f"HEAD {str(cp.repo.get('head'))[:12]} -> {str(now.get('head'))[:12]}"
            if now.get("head") != cp.repo.get("head") else "working tree differs",
        )
    if verify_argv is not None and list(verify_argv) != list(cp.verify_argv):
        return Resumability(False, REFUSE_VERIFY_CHANGED)
    if cp.phase == PHASE_STARTED and not cp.state:
        return Resumability(False, REFUSE_NO_PROGRESS, "the run had not planned or attempted anything")
    return Resumability(True)


def hash_repo_files(repo_root: Path, rels: list[str]) -> dict[str, str | None]:
    """sha256 of each repository file now (None when absent); what an apply would overwrite."""
    out: dict[str, str | None] = {}
    for rel in rels:
        p = repo_root / rel
        try:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        except OSError:
            out[rel] = None
    return out


def retain_verified(repo_root: Path, cp: RunCheckpoint, sandbox: Path, changed: list[str],
                    verified_hashes: dict[str, str]) -> bool:
    """Keep a completed run's verified bytes for a later `osn apply`. Returns whether they were kept.

    The bytes are snapshotted from the isolated copy and kept only when every
    hash equals what the Receipt verified; otherwise nothing is retained, so an
    apply can never put unverified bytes in the repository.
    """
    if not changed or not verified_hashes:
        return False
    manifest = snapshot_changed(sandbox, changed, repo_root, cp.run_id)
    if any(manifest.get(rel) is None or manifest.get(rel) != verified_hashes.get(rel) for rel in changed):
        discard_retained(repo_root, cp.run_id)
        cp.files, cp.verified_files = {}, None
        return False
    cp.files = dict(manifest)
    cp.verified_files = {rel: str(manifest[rel]) for rel in changed}
    cp.base_files = hash_repo_files(repo_root, changed)
    return True


def discard_retained(repo_root: Path, run_id: str) -> None:
    files_dir = checkpoint_dir(repo_root, run_id) / FILES_DIR
    if files_dir.exists():
        shutil.rmtree(files_dir, ignore_errors=True)


def check_applicable(cp: RunCheckpoint, repo_root: Path) -> Resumability:
    """Every rule that refuses `osn apply`, in order; none applying means the verified bytes may be applied."""
    if cp.version != CHECKPOINT_VERSION:
        return Resumability(False, REFUSE_VERSION, f"version {cp.version}")
    if cp.status != STATUS_COMPLETED:
        return Resumability(False, REFUSE_NOT_COMPLETED, f"status {cp.status}; `osn resume` continues it")
    if cp.applied is not None:
        return Resumability(False, REFUSE_ALREADY_APPLIED, f"at {cp.applied.get('at')}")
    if not cp.verified_files:
        return Resumability(False, REFUSE_NO_VERIFIED_FILES, "the run did not verify, or was promoted already")
    now = repo_fingerprint(repo_root)
    if now.get("head") != cp.repo.get("head"):
        return Resumability(False, REFUSE_REPO_CHANGED,
                            f"HEAD {str(cp.repo.get('head'))[:12]} -> {str(now.get('head'))[:12]}")
    current = hash_repo_files(repo_root, list(cp.verified_files))
    base = cp.base_files or {}
    for rel in cp.verified_files:
        if current.get(rel) != base.get(rel) and current.get(rel) != cp.verified_files.get(rel):
            return Resumability(False, REFUSE_TARGET_CHANGED, rel)
    return Resumability(True)


def budget_counters(budget: Any) -> dict[str, Any] | None:
    if budget is None:
        return None
    return {k: getattr(budget, k) for k in ("spend_usd", "spend_known", "model_calls", "attempts", "commands", "writes")
            if hasattr(budget, k)}


def restore_budget(budget: Any, counters: dict[str, Any] | None) -> bool:
    """Carry the earlier process's spend and counts into this run's ledger, so limits cover the whole run."""
    if budget is None or not counters:
        return False
    for k, v in counters.items():
        if hasattr(budget, k):
            setattr(budget, k, v)
    return True


def usage_from_record(d: dict[str, Any]) -> Any:
    from openshard.osn.model_provider import AttemptUsage

    u = AttemptUsage(
        attempt=int(d.get("attempt") or 0), model=str(d.get("model") or "unknown"),
        prompt_tokens=int(d.get("prompt_tokens") or 0), completion_tokens=int(d.get("completion_tokens") or 0),
        cost_usd=d.get("cost_usd") if isinstance(d.get("cost_usd"), (int, float)) else None,
        requested_model=d.get("requested_model"), turn=int(d.get("turn") or 1), role=str(d.get("role") or "executor"),
        duration_ms=d.get("duration_ms") if isinstance(d.get("duration_ms"), int) else None,
        cost_source=d.get("cost_source"),
        cache_read_tokens=d.get("cache_read_tokens") if isinstance(d.get("cache_read_tokens"), int) else None,
    )
    if d.get("worker_id"):
        u.worker_id = d["worker_id"]  # type: ignore[attr-defined]
    return u


def usage_to_record(u: Any) -> dict[str, Any]:
    d = dict(u.to_record())
    if getattr(u, "worker_id", None):
        d["worker_id"] = u.worker_id
    return d


def elapsed_since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return max(0.0, time.time() - then.timestamp())


__all__ = [
    "CHECKPOINT_VERSION",
    "PHASE_ATTEMPT_DONE",
    "PHASE_COMPLETED",
    "PHASE_PLANNED",
    "PHASE_STARTED",
    "PHASE_WORKERS_STAGED",
    "REFUSE_COMPLETED",
    "REFUSE_FILES_MISSING",
    "REFUSE_MISSING",
    "REFUSE_NO_PROGRESS",
    "REFUSE_PROCESS_ALIVE",
    "REFUSE_REPO_CHANGED",
    "REFUSE_UNREADABLE",
    "REFUSE_VERIFY_CHANGED",
    "REFUSE_VERSION",
    "REFUSE_ALREADY_APPLIED",
    "REFUSE_NOT_COMPLETED",
    "REFUSE_NOT_VERIFIED",
    "REFUSE_NO_VERIFIED_FILES",
    "REFUSE_TARGET_CHANGED",
    "STATUS_COMPLETED",
    "STATUS_INTERRUPTED",
    "STATUS_RUNNING",
    "Resumability",
    "RunCheckpoint",
    "budget_counters",
    "check_applicable",
    "check_resumable",
    "checkpoint_dir",
    "discard_retained",
    "hash_repo_files",
    "list_checkpoints",
    "now_stamp",
    "pid_alive",
    "read_checkpoint",
    "repo_fingerprint",
    "restore_budget",
    "restore_changed",
    "retain_verified",
    "snapshot_changed",
    "usage_from_record",
    "usage_to_record",
    "write_checkpoint",
]
