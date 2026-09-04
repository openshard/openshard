"""Repository materialisation and strict per-arm workspace isolation.

Every experimental arm gets its own clone of the target repository at the
exact base commit. Nothing here ever substitutes a commit, a branch or a
fixture: when git cannot produce precisely what the scenario asks for, the
call raises ``BenchmarkError`` with a stable code and the benchmark stops.

Seed repositories
-----------------
A scenario may ship its target repository as a plain directory (``seed``).
It is turned into a git repository with a fixed author, committer, date
and message, text files normalised to LF and mode 100644, so the commit id
is the same on every machine -- and it must equal the ``base_commit`` the
scenario pins, or the benchmark refuses to run.

Resetting code without losing history
-------------------------------------
After burn-in the treatment workspace's *code* must go back to the base
commit while its ``.openshard/`` history survives. ``git clean -fdx``
alone would delete ``.openshard/`` (it is untracked), so the reset uses
``-e .openshard`` and then proves, from ``git status`` and a content hash
of ``runs.jsonl``, that nothing but the history directory remains.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.pr13.benchmark.errors import BenchmarkError

HISTORY_DIR = ".openshard"
LOCAL_STATE_DIRS = (".openshard", ".claude", ".codex", ".opencode")
_GIT_TIMEOUT = 300.0

# Fixed identity so a seed commit hashes identically everywhere.
SEED_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "OpenShard Benchmark",
    "GIT_AUTHOR_EMAIL": "benchmark@openshard.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_NAME": "OpenShard Benchmark",
    "GIT_COMMITTER_EMAIL": "benchmark@openshard.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000",
}
SEED_COMMIT_MESSAGE = "Baseline for OpenShard PR13 benchmark scenario"
# Git config that must not vary with the machine running the benchmark.
_DETERMINISTIC_CONFIG = (
    "-c", "core.autocrlf=false",
    "-c", "core.filemode=false",
    "-c", "commit.gpgsign=false",
)


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    # Never let the user's global/system git config leak identity, hooks or
    # line-ending rules into a workspace; the deterministic -c flags above
    # cover what the benchmark needs.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    if extra:
        env.update(extra)
    return env


def git(
    args: list[str],
    cwd: Path | None,
    *,
    env_extra: dict[str, str] | None = None,
    check: bool = True,
    code: str = "git_failed",
    timeout: float = _GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run one git command with deterministic config. Raises ``BenchmarkError`` on failure."""
    argv = ["git", *_DETERMINISTIC_CONFIG, *args]
    try:
        result = subprocess.run(
            argv, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=timeout, env=_git_env(env_extra),
        )
    except FileNotFoundError as exc:
        raise BenchmarkError("git_missing", "git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkError(code, f"git timed out after {timeout}s: {' '.join(args)}") from exc
    if check and result.returncode != 0:
        raise BenchmarkError(
            code, f"git {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}",
            details={"argv": args, "cwd": str(cwd) if cwd else None, "returncode": result.returncode},
        )
    return result


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _copy_seed_tree(seed_dir: Path, dest: Path) -> list[str]:
    """Copy *seed_dir* into *dest*, LF-normalising text files and forcing mode 0644."""
    copied: list[str] = []
    for src in sorted(seed_dir.rglob("*")):
        rel = src.relative_to(seed_dir)
        if any(part == "__pycache__" or part.endswith(".pyc") for part in rel.parts):
            continue
        target = dest / rel
        if src.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        try:
            data.decode("utf-8")
            data = data.replace(b"\r\n", b"\n")
        except UnicodeDecodeError:
            pass
        target.write_bytes(data)
        try:
            os.chmod(target, 0o644)
        except OSError:
            pass
        copied.append(rel.as_posix())
    return copied


def build_seed_repository(seed_dir: Path, dest: Path, *, default_branch: str = "main") -> str:
    """Create a git repository at *dest* from *seed_dir* and return its commit id."""
    if dest.exists():
        raise BenchmarkError("workspace_exists", f"refusing to build a seed repository over {dest}")
    dest.mkdir(parents=True)
    copied = _copy_seed_tree(seed_dir, dest)
    if not copied:
        raise BenchmarkError("seed_empty", f"seed directory {seed_dir} holds no files")
    git(["init", "--quiet", "-b", default_branch], dest, code="seed_build_failed")
    git(["add", "-A"], dest, code="seed_build_failed")
    git(["commit", "--quiet", "--no-verify", "-m", SEED_COMMIT_MESSAGE], dest,
        env_extra=SEED_COMMIT_ENV, code="seed_build_failed")
    return head_commit(dest)


@dataclass(frozen=True)
class SourceRepo:
    """What every arm is cloned from: a path or URL plus the pinned commit."""

    kind: str
    location: str
    base_commit: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "location": self.location, "base_commit": self.base_commit}


def materialize_source(kind: str, base_commit: str, *, seed_dir: Path | None, url: str | None,
                       bench_root: Path, default_branch: str = "main") -> SourceRepo:
    """Produce the clone source for a scenario, enforcing the exact base commit."""
    if kind == "seed":
        if seed_dir is None:
            raise BenchmarkError("scenario_invalid", "seed repository without a seed directory")
        dest = bench_root / "source_repo"
        actual = build_seed_repository(seed_dir, dest, default_branch=default_branch)
        if actual != base_commit:
            raise BenchmarkError(
                "seed_commit_mismatch",
                "the seed directory does not reproduce the pinned base commit "
                f"(expected {base_commit}, built {actual}); the scenario is not the one pinned",
                details={"expected": base_commit, "built": actual, "seed_dir": str(seed_dir)},
            )
        return SourceRepo(kind="seed", location=str(dest), base_commit=base_commit)
    if kind == "git":
        if not url:
            raise BenchmarkError("scenario_invalid", "git repository without a url")
        return SourceRepo(kind="git", location=url, base_commit=base_commit)
    raise BenchmarkError("scenario_invalid", f"unknown repository kind {kind!r}")


def head_commit(ws: Path) -> str:
    return git(["rev-parse", "HEAD"], ws, code="git_failed").stdout.strip()


def status_lines(ws: Path, *, ignored: bool = False) -> list[str]:
    args = ["status", "--porcelain", "--untracked-files=all"]
    if ignored:
        args.append("--ignored")
    out = git(args, ws).stdout
    return [line for line in out.splitlines() if line.strip()]


def create_workspace(source: SourceRepo, dest: Path, *, label: str) -> Path:
    """Clone *source* into *dest* and check out exactly ``source.base_commit``.

    Fails loudly (``clone_failed`` / ``commit_unavailable`` /
    ``checkout_mismatch`` / ``workspace_dirty``) instead of falling back.
    """
    if dest.exists():
        raise BenchmarkError("workspace_exists", f"workspace {label} already exists at {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    clone_args = ["clone", "--quiet", "--no-hardlinks", source.location, str(dest)]
    git(clone_args, None, code="clone_failed")
    probe = git(["cat-file", "-e", f"{source.base_commit}^{{commit}}"], dest, check=False)
    if probe.returncode != 0:
        raise BenchmarkError(
            "commit_unavailable",
            f"base commit {source.base_commit} is not present in the clone of {source.location}",
            details={"workspace": label, "stderr": probe.stderr.strip()},
        )
    git(["checkout", "--quiet", "--detach", source.base_commit], dest, code="checkout_failed")
    actual = head_commit(dest)
    if actual != source.base_commit:
        raise BenchmarkError(
            "checkout_mismatch", f"workspace {label} is at {actual}, expected {source.base_commit}",
        )
    # A local identity so an agent that runs `git commit` does not fail on
    # configuration -- identical in every arm, never the machine's own.
    git(["config", "user.name", "OpenShard Benchmark Agent"], dest)
    git(["config", "user.email", "agent@openshard.invalid"], dest)
    git(["config", "commit.gpgsign", "false"], dest)
    leftovers = status_lines(dest, ignored=True)
    if leftovers:
        raise BenchmarkError("workspace_dirty", f"fresh workspace {label} is not clean: {leftovers[:5]}")
    return dest


def assert_isolated(a: Path, b: Path) -> None:
    """Two workspaces must be distinct directories, neither inside the other."""
    ra, rb = a.resolve(), b.resolve()
    if ra == rb:
        raise BenchmarkError("isolation_violated", f"arms share one workspace: {ra}")
    for outer, inner in ((ra, rb), (rb, ra)):
        try:
            inner.relative_to(outer)
        except ValueError:
            continue
        raise BenchmarkError("isolation_violated", f"workspace {inner} is nested inside {outer}")
    if not ra.is_dir() or not rb.is_dir():
        raise BenchmarkError("isolation_violated", "both workspaces must exist")


@dataclass
class ChangedPaths:
    """Repository-relative paths the agent changed, from git's point of view."""

    modified: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def all(self) -> list[str]:
        return sorted(set(self.modified) | set(self.added) | set(self.deleted))

    def to_dict(self) -> dict[str, Any]:
        return {"modified": sorted(self.modified), "added": sorted(self.added), "deleted": sorted(self.deleted)}


def _is_local_state(path: str) -> bool:
    top = path.replace("\\", "/").split("/", 1)[0]
    return top in LOCAL_STATE_DIRS


def changed_paths(ws: Path, base_commit: str) -> ChangedPaths:
    """Files differing from *base_commit* (tracked changes) plus untracked files."""
    out = ChangedPaths()
    diff = git(["diff", "--name-status", base_commit, "--"], ws).stdout
    for line in diff.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0].strip(), parts[-1].strip()
        if _is_local_state(path):
            continue
        if status.startswith("A"):
            out.added.append(path)
        elif status.startswith("D"):
            out.deleted.append(path)
        else:
            out.modified.append(path)
    for line in status_lines(ws):
        if line.startswith("??"):
            path = line[3:].strip()
            if not _is_local_state(path) and path not in out.added:
                out.added.append(path)
    return out


@dataclass
class ResetReport:
    base_commit: str
    history_path: str
    history_hash_before: str | None
    history_hash_after: str | None
    remaining_status: list[str]
    removed_local_state: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_commit": self.base_commit,
            "history_path": self.history_path,
            "history_hash_before": self.history_hash_before,
            "history_hash_after": self.history_hash_after,
            "history_preserved": (
                self.history_hash_before is not None and self.history_hash_before == self.history_hash_after
            ),
            "remaining_status": list(self.remaining_status),
            "removed_local_state": list(self.removed_local_state),
        }


def reset_code_preserving_history(ws: Path, base_commit: str) -> ResetReport:
    """Return *ws*'s code to *base_commit*, keeping ``.openshard/`` byte-for-byte.

    Raises ``history_missing`` when there is no history to preserve (the
    burn-in did not record anything), ``history_lost`` when the reset
    changed it, and ``reset_incomplete`` when anything but the history
    directory survives the reset.
    """
    runs_path = ws / HISTORY_DIR / "runs.jsonl"
    before = file_sha256(runs_path)
    if before is None:
        raise BenchmarkError("history_missing", f"no {HISTORY_DIR}/runs.jsonl to preserve in {ws}")
    removed = [d for d in LOCAL_STATE_DIRS if d != HISTORY_DIR and (ws / d).exists()]
    git(["reset", "--hard", "--quiet", base_commit], ws, code="reset_failed")
    git(["clean", "-fdx", "--quiet", "-e", HISTORY_DIR], ws, code="reset_failed")
    # Belt and braces: the clean must not have been able to touch it, but
    # only a stat proves it.
    after = file_sha256(runs_path)
    if after != before:
        raise BenchmarkError("history_lost", f"{runs_path} changed during the code reset")
    actual = head_commit(ws)
    if actual != base_commit:
        raise BenchmarkError("checkout_mismatch", f"after reset {ws} is at {actual}, expected {base_commit}")
    remaining = status_lines(ws, ignored=True)
    unexpected = [line for line in remaining if not line[3:].replace("\\", "/").startswith(HISTORY_DIR + "/")]
    if unexpected:
        raise BenchmarkError("reset_incomplete", f"paths other than {HISTORY_DIR}/ survived the reset: {unexpected[:5]}")
    return ResetReport(
        base_commit=base_commit, history_path=str(runs_path), history_hash_before=before,
        history_hash_after=after, remaining_status=remaining, removed_local_state=removed,
    )


def copy_history(src_ws: Path, dest_ws: Path) -> str | None:
    """Replicate ``src_ws/.openshard`` into ``dest_ws`` byte-for-byte; returns the runs.jsonl hash."""
    src = src_ws / HISTORY_DIR
    dest = dest_ws / HISTORY_DIR
    if not src.is_dir():
        raise BenchmarkError("history_missing", f"no {HISTORY_DIR}/ in {src_ws} to replicate")
    if dest.exists():
        raise BenchmarkError("isolation_violated", f"{dest} already exists; refusing to merge histories")
    shutil.copytree(src, dest)
    return file_sha256(dest / "runs.jsonl")


def history_present(ws: Path) -> bool:
    return (ws / HISTORY_DIR / "runs.jsonl").is_file()
