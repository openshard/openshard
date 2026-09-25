"""Bounded OSN execution-loop slice (v0).

inspect -> plan -> policy -> isolated action -> direct verification
-> bounded retry (only if justified) -> receipt.

Evidence semantics: actions come from an agent/provider and are *declared*;
policy decisions, file effects and verification are *observed* by OpenShard.
Applying a change is never treated as verifying it. Changes are made only in
an isolated copy (a filesystem copy, not a process sandbox: the verify command
runs with host permissions and may execute agent-written code); promoting them to the real repo is a separate, policy-gated
step (see openshard.native.sandbox_apply.apply_sandbox_changes).
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from openshard.policy.file_mutation import Approver, FileMutationGate
from openshard.security.paths import UnsafePathError, resolve_safe_repo_path

SCHEMA_VERSION = 1
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".openshard", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules",
    # Local secrets/agent state never belong in the isolated working copy.
    ".env", ".env.*", ".claude", ".codex", ".opencode", ".codegraph", "*.pem", "*.key",
    ".mypy_cache", ".ruff_cache", "dist", ".tmp",
)


@dataclass(frozen=True)
class FileWriteAction:
    """A proposed whole-file write (declared by the agent, not yet evidence)."""

    path: str
    content: str


@dataclass
class LoopContext:
    """Inspect-phase output handed to the provider on every attempt."""

    task: str
    repo_files: list[str]
    attempt: int
    previous_failure: str | None = None  # verification output tail, in memory only
    blocked_paths: list[str] = field(default_factory=list)


# provider(context) -> proposed actions. In production this wraps a model call.
ActionProvider = Callable[[LoopContext], list[FileWriteAction]]


@dataclass
class VerificationResult:
    command: list[str]
    exit_code: int | None  # None: could not run / timed out
    passed: bool
    output_sha256: str
    output_bytes: int
    timed_out: bool = False
    tainted: bool = False  # the verifier modified the files it was verifying
    ran: bool = True  # False: the command could not be started; no outcome observed
    observed: bool = True  # run by OpenShard itself (equals ran)


@dataclass
class AttemptRecord:
    n: int
    proposed: list[str]
    applied: list[str]
    blocked: list[str]
    policy: dict
    verification: VerificationResult | None = None


@dataclass
class LoopReceipt:
    task_id: str
    status: str  # verified | failed | blocked | no_actions | error
    stop_reason: str
    attempts: list[AttemptRecord]
    changed_files: list[str]
    sandbox_path: str
    schema_version: int = SCHEMA_VERSION
    receipt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # sha256 of each changed file as verified; in memory only, used to refuse
    # promoting bytes that differ from what was verified.
    verified_file_hashes: dict[str, str] = field(default_factory=dict)

    @property
    def verification_state(self) -> str:
        for a in reversed(self.attempts):
            if a.verification is not None:
                return "passed" if a.verification.passed else "failed"
        return "not_run"

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "task_id": self.task_id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "verification_state": self.verification_state,
            "changed_files": list(self.changed_files),
            "sandbox_path": self.sandbox_path,
            "attempts": [
                {
                    "n": a.n,
                    "proposed": [_display_path(p) for p in a.proposed],
                    "applied": a.applied,
                    "blocked": [_display_path(p) for p in a.blocked],
                    "policy": _stored_policy(a.policy),
                    "verification": _stored_verification(a.verification),
                }
                for a in self.attempts
            ],
            "evidence": {
                "actions": "agent_declared",
                "policy_and_file_effects": "openshard_observed",
                "verification": "openshard_observed",
                "task_text_stored": False,
            },
        }


def _display_path(p: str) -> str:
    """Model-supplied paths that are absolute or escaping are masked in receipts."""
    norm = p.replace("\\", "/")
    if norm.startswith("/") or ":" in norm or ".." in norm.split("/") or norm.startswith("~"):
        return "<unsafe-path>"
    return p


def _stored_policy(policy: dict) -> dict:
    """The gate summary lists raw proposed paths; mask unsafe ones like the rest."""
    return {
        k: [_display_path(x) if isinstance(x, str) else x for x in v] if isinstance(v, list) else v
        for k, v in policy.items()
    }


def _stored_verification(v: VerificationResult | None) -> dict | None:
    if v is None:
        return None
    d = dict(vars(v))
    # Keep only the executable's name: full argv can carry secrets or local paths.
    exe = v.command[0] if v.command else ""
    d["command"] = [exe.replace("\\", "/").rsplit("/", 1)[-1]] if exe else []
    return d


def _hash_files(root: Path, rels: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in rels:
        p = root / rel
        try:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<missing>"
        except OSError:
            out[rel] = "<unreadable>"
    return out


def create_isolated_copy(repo_root: Path) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="osn-loop-")) / "work"
    shutil.copytree(repo_root, dest, ignore=_COPY_IGNORE)
    return dest


def _list_files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


def _as_text(v: str | bytes | None) -> str:
    if v is None:
        return ""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v


def _run_verification(command: list[str], cwd: Path, timeout: float) -> tuple[VerificationResult, str]:
    try:
        proc = subprocess.run(
            command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        code: int | None = proc.returncode
        timed_out = False
        ran = True
    except subprocess.TimeoutExpired as exc:
        out = "".join(_as_text(x) for x in (exc.stdout, exc.stderr))
        code, timed_out, ran = None, True, True
    except OSError as exc:
        out, code, timed_out, ran = f"could not run: {exc}", None, False, False
    result = VerificationResult(
        command=list(command),
        exit_code=code,
        passed=code == 0,
        output_sha256=hashlib.sha256(out.encode("utf-8", "replace")).hexdigest(),
        output_bytes=len(out.encode("utf-8", "replace")),
        timed_out=timed_out,
        ran=ran,
        observed=ran,
    )
    return result, out


def run_bounded_loop(
    repo_root: Path,
    task: str,
    provider: ActionProvider,
    verify_command: list[str],
    *,
    task_id: str | None = None,
    max_attempts: int = 2,
    approver: Approver | None = None,
    verify_timeout: float = 120.0,
    sandbox_path: Path | None = None,
) -> LoopReceipt:
    """Run the bounded loop. Never writes to *repo_root*."""
    task_id = task_id or f"task_{uuid.uuid4()}"
    max_attempts = max(1, min(max_attempts, 5))  # hard bound
    sandbox = sandbox_path or create_isolated_copy(repo_root)
    rr, sb = repo_root.resolve(), sandbox.resolve()
    if sb == rr or rr in sb.parents or sb in rr.parents:
        raise ValueError("sandbox_path must be separate from repo_root")
    attempts: list[AttemptRecord] = []
    changed: list[str] = []
    prev_fingerprint: str | None = None
    prev_failure: str | None = None
    blocked_seen: list[str] = []
    prev_actions: str | None = None

    def _receipt(status: str, reason: str) -> LoopReceipt:
        return LoopReceipt(task_id, status, reason, attempts, changed, str(sandbox))

    for n in range(1, max_attempts + 1):
        ctx = LoopContext(task, _list_files(sandbox), n, prev_failure, list(blocked_seen))
        try:
            actions = provider(ctx)
        except Exception as exc:
            attempts.append(AttemptRecord(n, [], [], [], {"provider_error": type(exc).__name__}))
            return _receipt("error", "provider_error")
        if not actions:
            return _receipt("no_actions", "provider proposed no actions")

        actions_fp = hashlib.sha256(
            "\x1f".join(f"{a.path}\x1f{a.content}" for a in actions).encode("utf-8", "replace")
        ).hexdigest()
        if actions_fp == prev_actions:
            # Same writes as the last (failed) attempt: a retry is not justified.
            return _receipt("failed", "no_progress_identical_actions")
        prev_actions = actions_fp

        gate = FileMutationGate(approver=approver)
        applied: list[str] = []
        blocked: list[str] = []
        for act in actions:
            try:
                dest = resolve_safe_repo_path(sandbox, act.path)
            except UnsafePathError:
                blocked.append(act.path)
                continue
            if not gate.authorize(act.path):
                blocked.append(act.path)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(act.content, encoding="utf-8")
            gate.mark_executed(act.path)
            applied.append(act.path)
            if act.path not in changed:
                changed.append(act.path)
        rec = AttemptRecord(n, [a.path for a in actions], applied, blocked, gate.summary())
        attempts.append(rec)
        blocked_seen.extend(p for p in blocked if p not in blocked_seen)

        if blocked:
            # Policy/safety blocks are not retried automatically: the same
            # proposal would be blocked again and a human decision is needed.
            return _receipt("blocked", "policy_or_path_block")

        before = _hash_files(sandbox, changed)
        result, output = _run_verification(verify_command, sandbox, verify_timeout)
        rec.verification = result
        after = _hash_files(sandbox, changed)
        if after != before:
            # A pass on files the verifier itself rewrote proves nothing about
            # the proposed change, and those bytes must never be promoted.
            result.passed = False
            result.tainted = True
            return _receipt("failed", "verifier_modified_files")
        if result.passed:
            receipt = _receipt("verified", "verification_passed")
            receipt.verified_file_hashes = after
            return receipt

        fingerprint = result.output_sha256
        if fingerprint == prev_fingerprint:
            return _receipt("failed", "no_progress_identical_failure")
        status_line = "timed out" if result.timed_out else f"exit code {result.exit_code}"
        prev_fingerprint = fingerprint
        # Always non-empty, even when the verifier prints nothing.
        prev_failure = f"verify command failed ({status_line})\n{output[-2000:]}"

    return _receipt("failed", "max_attempts_exhausted")
