"""Promote files from a native run sandbox/worktree into the real repo."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from openshard.policy.file_mutation import Approver, FileMutationGate
from openshard.security.paths import UnsafePathError, resolve_safe_repo_path

_WALK_EXCLUDES = {".git", ".openshard", "__pycache__", ".pytest_cache"}


@dataclass
class SandboxApplyResult:
    applied: bool = False
    sandbox_path: str = ""
    files_applied: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    reason: str = ""
    raw_content_stored: bool = False
    files_denied: list[str] = field(default_factory=list)
    policy_summary: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.raw_content_stored = False


def extract_sandbox_path_from_entry(entry: dict) -> str:
    """Return the worktree path from a run entry, or '' if not applicable."""
    sandbox = entry.get("sandbox")
    if not sandbox or not isinstance(sandbox, dict):
        return ""
    if sandbox.get("sandbox_type") != "worktree":
        return ""
    worktree_path = sandbox.get("worktree_path")
    if not worktree_path:
        return ""
    if not Path(worktree_path).exists():
        return ""
    return worktree_path


def get_candidate_records_from_entry(entry: dict) -> list[dict]:
    """Return normalized candidate records from a run entry, or [] if none."""
    try:
        cs = entry.get("candidate_summary")
        if not cs:
            return []
        if isinstance(cs, dict):
            raw = cs.get("candidates") or []
        else:
            raw = getattr(cs, "candidates", []) or []
        result = []
        for item in raw:
            if isinstance(item, dict):
                rec = dict(item)
            else:
                try:
                    rec = vars(item)
                except TypeError:
                    continue
            rec.setdefault("score", 0.0)
            rec.setdefault("score_reasons", [])
            result.append(rec)
        return result
    except Exception:
        return []


def extract_candidate_sandbox_path_from_entry(entry: dict, candidate_index: int) -> str:
    """Return the sandbox_path for a specific candidate (1-based index), or ''."""
    try:
        for r in get_candidate_records_from_entry(entry):
            if r.get("candidate_index") == candidate_index:
                sp = r.get("sandbox_path") or ""
                if sp and Path(sp).exists():
                    return sp
                return ""
        return ""
    except Exception:
        return ""


def list_sandbox_changed_files(repo_root: Path, sandbox_path: Path) -> list[str]:
    """Return relative paths of files changed in the sandbox vs HEAD.

    Runs two git commands (diff + ls-files) and returns their deduped union.
    Falls back to a filesystem walk if git fails.
    """
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=str(sandbox_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        ls = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(sandbox_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if diff.returncode == 0 and ls.returncode == 0:
            seen: set[str] = set()
            result: list[str] = []
            for line in diff.stdout.splitlines() + ls.stdout.splitlines():
                f = line.strip()
                if f and f not in seen:
                    seen.add(f)
                    result.append(f)
            return result
    except Exception:
        pass

    # Fallback: walk the sandbox, skip excluded dirs
    files: list[str] = []
    for p in sandbox_path.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(sandbox_path)
        if rel.parts and rel.parts[0] in _WALK_EXCLUDES:
            continue
        files.append(str(rel).replace("\\", "/"))
    return files


def filter_sandbox_changed_files(
    files: list[str],
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[str]:
    """Return a filtered, deduplicated, order-preserving subset of files.

    Slashes are normalized to / before matching. No glob expansion in v0.
    """
    seen: set[str] = set()
    result: list[str] = []
    for f in files:
        f = f.replace("\\", "/")
        if f not in seen:
            seen.add(f)
            result.append(f)

    if include is not None:
        include_set = {i.replace("\\", "/") for i in include}
        result = [f for f in result if f in include_set]

    if exclude is not None:
        exclude_set = {e.replace("\\", "/") for e in exclude}
        result = [f for f in result if f not in exclude_set]

    return result


def apply_sandbox_changes(
    repo_root: Path,
    sandbox_path: Path,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    approver: Approver | None = None,
    explicit_files: list[str] | None = None,
) -> SandboxApplyResult:
    """Copy changed sandbox files into repo_root, gated by file-mutation policy.

    Denied files (and ask-files without a granting approver) are never written.
    No deletions in v0.
    """
    # explicit_files: the caller already knows exactly what changed (e.g. the
    # OSN loop's own record), so skip git-based discovery, which is unreliable
    # for a plain copy that lives inside another git checkout.
    discovered = (
        list(explicit_files) if explicit_files is not None
        else list_sandbox_changed_files(repo_root, sandbox_path)
    )
    files = filter_sandbox_changed_files(
        discovered,
        include=include,
        exclude=exclude,
    )
    if not files:
        reason = (
            "No sandbox changes matched the apply selection."
            if include or exclude
            else "No sandbox changes to apply."
        )
        return SandboxApplyResult(sandbox_path=str(sandbox_path), reason=reason)

    result = SandboxApplyResult(sandbox_path=str(sandbox_path))
    gate = FileMutationGate(approver=approver)
    for rel in files:
        try:
            dest = resolve_safe_repo_path(repo_root, rel)
        except UnsafePathError as exc:
            result.files_skipped.append(f"{rel} (unsafe: {exc})")
            continue

        if not gate.authorize(rel):
            result.files_denied.append(rel)
            result.files_skipped.append(f"{rel} (blocked by policy)")
            continue

        src = sandbox_path / rel
        if src.is_symlink() or (src.exists() and not src.resolve().is_relative_to(sandbox_path.resolve())):
            result.files_skipped.append(f"{rel} (unsafe source: symlink or outside sandbox)")
            continue
        if not src.exists() or src.is_dir():
            result.files_skipped.append(f"{rel} (not found in sandbox)")
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        result.files_applied.append(rel)
        gate.mark_executed(rel)

    result.policy_summary = gate.summary()
    result.applied = bool(result.files_applied)
    return result
