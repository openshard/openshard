"""Run checks against one exact commit, never against whatever the working tree holds.

A verification is only worth a green badge when it is bound to the commit it
tested. The user's checkout is often not that commit: another agent may have
uncommitted edits in it, or HEAD may have moved on. So:

* **in place** -- when the checkout's HEAD *is* the commit and the tree is
  clean (OpenShard's own ``.openshard/`` state aside), checks run there, with
  the dependencies already installed;
* **isolated** -- otherwise a detached ``git worktree`` of the commit is made
  in the temp directory and checks run there. A JavaScript project's
  dependencies are prepared from its lockfile *offline and without install
  scripts* (``pnpm install --frozen-lockfile --offline --ignore-scripts`` or
  the npm/yarn equivalent). Python checks use the project's interpreter with
  ``PYTHONPATH`` pointing at the worktree, so the commit's code is imported.
  The worktree is removed afterwards.

If the isolated copy cannot be prepared (git refuses, dependencies are not in
the local store), nothing runs and the caller records every check as
``unknown`` -- never ``failed``: a missing package cache says nothing about
the change. Never raises.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

MODE_IN_PLACE = "in_place"
MODE_ISOLATED = "isolated_worktree"
DEPS_TIMEOUT_SECONDS = 600.0

_OFFLINE_INSTALL: dict[str, list[str]] = {
    "pnpm": ["install", "--frozen-lockfile", "--offline", "--ignore-scripts"],
    "yarn": ["install", "--frozen-lockfile", "--offline", "--ignore-scripts"],
    "npm": ["ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"],
}


@dataclass
class CommitCheckout:
    path: Path | None
    mode: str
    ready: bool
    problem: str = ""
    notes: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def resolve_tool(argv: list[str] | tuple[str, ...]) -> list[str] | None:
    """*argv* with its executable resolved, falling back to ``corepack <pm>`` for pnpm/yarn. None if absent."""
    if not argv:
        return None
    exe = shutil.which(argv[0])
    if exe:
        return [exe, *argv[1:]]
    if argv[0] in ("pnpm", "yarn"):
        corepack = shutil.which("corepack")
        if corepack:
            return [corepack, *argv]
    return None


def _clean_at(root: Path, sha: str) -> bool:
    from openshard.verification.post_session import tree_state

    state = tree_state(root)
    return state.head is not None and state.head.startswith(sha.lower()) and state.dirty is False


def _git(root: Path, args: list[str], timeout: float = 120.0) -> bool:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args], cwd=root, capture_output=True, timeout=timeout, check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _package_manager(root: Path) -> str | None:
    import json

    manifest = root / "package.json"
    if not manifest.is_file():
        return None
    try:
        declared = json.loads(manifest.read_text(encoding="utf-8")).get("packageManager")
    except (OSError, ValueError, AttributeError):
        declared = None
    if isinstance(declared, str) and declared.split("@", 1)[0] in _OFFLINE_INSTALL:
        return declared.split("@", 1)[0]
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    return "npm"


def _prepare_js(path: Path, timeout: float) -> tuple[bool, str]:
    pm = _package_manager(path)
    if pm is None:
        return True, ""
    argv = resolve_tool([pm, *_OFFLINE_INSTALL[pm]])
    if argv is None:
        return False, f"{pm} is not installed, so dependencies could not be prepared"
    from openshard.verification.post_session import check_environment, run_process_tree

    try:
        proc = run_process_tree(  # fixed argv, no shell, no install scripts
            argv, cwd=path, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, env=check_environment(),
        )
    except subprocess.TimeoutExpired:
        return False, f"preparing dependencies offline with {pm} timed out"
    except OSError as exc:
        return False, f"could not start {pm} ({type(exc).__name__})"
    if proc.returncode != 0:
        return False, f"{pm} could not prepare dependencies offline (exit {proc.returncode})"
    return True, f"dependencies prepared offline with {pm}, install scripts disabled"


def _remove_worktree(root: Path, path: Path) -> None:
    _git(root, ["worktree", "remove", "--force", str(path)], timeout=300.0)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    _git(root, ["worktree", "prune"])


STALE_COPY_SECONDS = 6 * 3600


def _sweep_stale(root: Path, parent: Path) -> None:
    """Remove isolated copies a killed run left behind (older than `STALE_COPY_SECONDS`)."""
    import time

    try:
        cutoff = time.time() - STALE_COPY_SECONDS
        stale = [p for p in parent.iterdir() if p.is_dir() and p.stat().st_mtime < cutoff]
    except OSError:
        return
    for path in stale:
        shutil.rmtree(path, ignore_errors=True)
    if stale:
        _git(root, ["worktree", "prune"])


@contextmanager
def commit_checkout(
    root: Path,
    sha: str,
    *,
    prepare_js: bool,
    deps_timeout: float = DEPS_TIMEOUT_SECONDS,
    temp_parent: Path | None = None,
    force_isolated: bool = False,
) -> Iterator[CommitCheckout]:
    """Yield a checkout whose tree is exactly *sha* (see module docstring).

    *force_isolated* (unattended runs) never uses the live checkout, which the
    session's next turn may already be editing.
    """
    if not force_isolated and _clean_at(root, sha):
        yield CommitCheckout(path=root, mode=MODE_IN_PLACE, ready=True,
                             notes=["ran in the checkout, which was clean at this commit"])
        return
    parent = temp_parent or Path(tempfile.gettempdir()) / "openshard-verify"
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield CommitCheckout(path=None, mode=MODE_ISOLATED, ready=False,
                             problem="could not create a temporary directory for an isolated copy")
        return
    _sweep_stale(root, parent)
    path = parent / f"{sha[:12]}-{uuid.uuid4().hex[:8]}"
    if not _git(root, ["worktree", "add", "--detach", str(path), sha], timeout=300.0):
        shutil.rmtree(path, ignore_errors=True)
        yield CommitCheckout(path=None, mode=MODE_ISOLATED, ready=False,
                             problem="git could not create an isolated copy of the commit")
        return
    try:
        notes = ["ran in an isolated copy of the commit (the checkout was not clean at it)"]
        if prepare_js:
            ok, note = _prepare_js(path, deps_timeout)
            if not ok:
                yield CommitCheckout(path=path, mode=MODE_ISOLATED, ready=False, problem=note, notes=notes)
                return
            if note:
                notes.append(note)
        # The commit's own code first: a src-layout package lives under src/,
        # where an editable install of the main checkout would otherwise win.
        roots = [str(path / "src")] if (path / "src").is_dir() else []
        existing = os.environ.get("PYTHONPATH")
        env = {"PYTHONPATH": os.pathsep.join([*roots, str(path), *([existing] if existing else [])])}
        yield CommitCheckout(path=path, mode=MODE_ISOLATED, ready=True, notes=notes, env=env)
    finally:
        _remove_worktree(root, path)


__all__ = ["MODE_IN_PLACE", "MODE_ISOLATED", "CommitCheckout", "commit_checkout", "resolve_tool"]
