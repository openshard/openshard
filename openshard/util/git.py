"""Safe, observational ``git`` subprocess helper.

Every OpenShard git call is a local read with a short timeout that must
never raise into the caller (a hook, the capture-service worker, a repo
scan). This module is that one call site.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 5.0

# Global git options for every observational call. ``--no-optional-locks``
# (git >= 2.15) stops ``status`` / ``diff`` from writing a refreshed index,
# so OpenShard never holds ``.git/index.lock`` against the agent it observes.
GIT_OBSERVE_FLAGS: tuple[str, ...] = ("--no-optional-locks",)

# These git calls can run from OpenShard's background capture-service worker
# (adapters/claude_capture_service.py), a process that may itself have no
# console (e.g. spawned with CREATE_NO_WINDOW). Without this flag, such a
# console-less parent causes Windows to allocate a brand-new visible console
# for each git.exe child. Output is always captured via PIPE here regardless,
# so no window is ever needed; harmless for every other (already-consoled)
# caller.
# subprocess.CREATE_NO_WINDOW only exists in typeshed's Windows stubs, so a
# bare attribute access fails mypy on this cross-platform module even inside
# a sys.platform guard (the guard narrows reachability, not module-attribute
# existence) -- getattr sidesteps the static lookup; the fallback is never
# used since the whole expression is a no-op off Windows anyway.
NO_WINDOW_KW: dict = (
    {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
)


def run_git(
    root: Path,
    args: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    stdin: str | None = None,
) -> str | None:
    """stdout of ``git <args>`` run in *root*, or ``None`` on any failure.

    ``None`` covers a non-zero exit, a timeout, a missing ``git`` binary and
    any other OS error. Output is decoded as UTF-8 with undecodable bytes
    replaced, so an odd file name can never turn into an exception. Never
    raises.

    Runs with ``--no-optional-locks``: ``git status`` and ``git diff``
    otherwise refresh the index through ``.git/index.lock`` when stat data
    is stale, and an agent's own ``git commit`` racing that refresh fails
    with "Unable to create index.lock: File exists". Observation must never
    make the agent's git fail. Commands that need the lock (``add``) still
    take it; only the optional refresh is skipped.
    """
    try:
        result = subprocess.run(
            ["git", *GIT_OBSERVE_FLAGS, *args],
            cwd=str(root),
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **NO_WINDOW_KW,
        )
    except Exception:
        return None
    return result.stdout if result.returncode == 0 else None


# ---------------------------------------------------------------------------
# Read-only commit-graph queries (Historical Ingestion v1, ingest/enrich/).
# These never look at the working tree or index: historical evidence must come
# from git objects, never from today's checkout. Arguments that come from
# imported history (SHAs, branch names) are validated before use and always
# passed as fixed argv entries -- nothing is ever interpreted by a shell.
# ---------------------------------------------------------------------------

_SHA_CHARS = frozenset("0123456789abcdef")


def _is_sha_like(value: object) -> bool:
    return isinstance(value, str) and 7 <= len(value) <= 64 and set(value.lower()) <= _SHA_CHARS


def _is_safe_ref(value: object) -> bool:
    """A branch/ref name that cannot be mistaken for an option or a range."""
    if not isinstance(value, str) or not value or len(value) > 255:
        return False
    if value.startswith("-") or ".." in value or any(ch.isspace() or ord(ch) < 32 for ch in value):
        return False
    return True


def commit_exists(root: Path, sha: str) -> str | None:
    """The full SHA when *sha* names a commit object in *root*, else ``None``."""
    if not _is_sha_like(sha):
        return None
    out = run_git(root, ["rev-parse", "--verify", "--quiet", f"{sha.lower()}^{{commit}}"])
    full = out.strip() if out else ""
    return full if _is_sha_like(full) else None


def ref_exists(root: Path, ref: str) -> bool:
    """True when *ref* (e.g. a branch name) resolves to a commit in *root*."""
    if not _is_safe_ref(ref):
        return False
    return run_git(root, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"]) is not None


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool | None:
    """``True``/``False`` from ``merge-base --is-ancestor``; ``None`` when unknown."""
    if not _is_sha_like(ancestor) or not (_is_sha_like(descendant) or _is_safe_ref(descendant)):
        return None
    try:
        import subprocess

        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=DEFAULT_TIMEOUT_SECONDS, **NO_WINDOW_KW,
        )
    except Exception:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def commit_files(root: Path, sha: str) -> list[str] | None:
    """Repo-relative paths changed by commit *sha* (vs its first parent), or ``None``."""
    if not _is_sha_like(sha):
        return None
    out = run_git(root, ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", sha])
    if out is None:
        return None
    return [p for p in out.split("\0") if p]


def rev_before(root: Path, ref: str, before: str) -> str | None:
    """The newest commit on *ref* committed at or before *before* (ISO 8601), or ``None``."""
    if not _is_safe_ref(ref) or not isinstance(before, str) or not before:
        return None
    out = run_git(root, ["rev-list", "-1", f"--before={before}", ref, "--"])
    sha = out.strip() if out else ""
    return sha if _is_sha_like(sha) else None


def commits_in_window(
    root: Path, ref: str, since: str, until: str, paths: list[str] | None = None, *, limit: int = 50
) -> list[str] | None:
    """Commits on *ref* committed within ``[since, until]`` (ISO 8601), newest first.

    Restricted to commits touching *paths* when given. ``None`` on any failure.
    """
    if not _is_safe_ref(ref) or not since or not until:
        return None
    args = ["log", f"--max-count={int(limit)}", "--format=%H", f"--since={since}", f"--until={until}", ref, "--"]
    args += [p for p in (paths or []) if isinstance(p, str) and p and not p.startswith("-")]
    out = run_git(root, args)
    if out is None:
        return None
    return [line.strip() for line in out.splitlines() if _is_sha_like(line.strip())]


def main_checkout_of(root: Path) -> Path | None:
    """The main checkout *root* is a linked ``git worktree`` of, else None. Never raises.

    A linked worktree's ``--git-common-dir`` is the main checkout's ``.git``;
    a main checkout (or a directory that is not a git checkout) yields None.
    """
    try:
        out = run_git(root, ["rev-parse", "--git-common-dir"])
        if not out or not out.strip():
            return None
        common = Path(out.strip())
        common = (common if common.is_absolute() else root / common).resolve()
        if common.name == ".git" and common.parent != root.resolve():
            return common.parent
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def checkout_facts(root: Path) -> dict:
    """What kind of checkout a Receipt's git evidence was observed in. Never raises.

    ``kind`` is ``linked_worktree`` when *root* is a ``git worktree`` of
    another checkout, ``main_checkout`` for the checkout that owns the git
    dir, or ``not_git``. ``name`` is a linked worktree's directory basename
    (never its absolute path), which tells worktrees of one repository
    apart; a main checkout is identified by its repository already and
    carries no name. A session that works in several checkouts produces one
    Receipt per checkout, all with the same ``capture.session_id``; this
    block says which one each is.
    """
    try:
        inside = run_git(root, ["rev-parse", "--is-inside-work-tree"])
        if inside is None or inside.strip() != "true":
            return {"kind": "not_git", "name": None}
        if main_checkout_of(root) is not None:
            return {"kind": "linked_worktree", "name": root.name}
    except (OSError, RuntimeError, ValueError):
        return {"kind": "not_git", "name": None}
    return {"kind": "main_checkout", "name": None}


_SHA_RE = re.compile(r"[0-9a-f]{40}")


def head_at(root: Path, when: str) -> str | None:
    """HEAD of *root* as of *when* (an ISO 8601 stamp), from its reflog; None when git cannot say. Never raises.

    ``HEAD@{<time>}`` is the reflog's answer, which reaches back to the
    checkout's first entry: a worktree created after *when* answers with
    its initial HEAD, which is the right anchor for work done in it.
    """
    if not isinstance(when, str) or not when.strip():
        return None
    out = run_git(root, ["rev-parse", "--verify", "--quiet", f"HEAD@{{{when.strip()}}}"])
    sha = (out or "").strip()
    return sha if _SHA_RE.fullmatch(sha) else None
