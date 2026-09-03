"""Locate the OpenShard history root for the current working directory.

Every local visibility command (``openshard last`` / ``history`` /
``context`` / ``stats``) must read the *same* ``.openshard/runs.jsonl``
whether it is run from the repository root, ``repo/subdir`` or
``repo/subdir/deeper``. Historically these commands used ``Path.cwd()``
directly, so running from a subdirectory silently reported "no history".

Resolution rule (deterministic, no network, never raises)
----------------------------------------------------------
Walk upwards from the start directory (default: cwd) to the nearest
``.git`` (file or directory, so worktrees and submodules count) -- that is
the repository boundary and the walk never goes above it. Then:

1. if that repository root already holds ``.openshard/runs.jsonl``, it is
   the history root (this is where Claude Code hooks and ``openshard run``
   from the root record to, and what every subdirectory must see);
2. otherwise, the nearest directory *between* the start and the root that
   has an ``.openshard/`` directory is used -- history recorded there is
   never silently hidden;
3. otherwise the repository root is the history root (a fresh repository
   with nothing recorded yet).

If no ``.git`` is found anywhere up the tree, the nearest ``.openshard/``
directory wins, and failing that the start directory itself -- exactly the
old cwd-relative behaviour, so non-git usage is unchanged.

Stopping at the *nearest* ``.git`` keeps a nested repository from reading
its parent's history; walking only upwards keeps sibling repositories out
of reach. Rule 2 also protects a directory that is not itself a repository
but sits under a version-controlled parent (a home directory under git,
for example): the parent is only preferred when it really has history.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

HISTORY_DIRNAME = ".openshard"
RUNS_FILENAME = "runs.jsonl"
HISTORY_RELPATH = Path(HISTORY_DIRNAME) / RUNS_FILENAME

RESOLVED_CWD = "cwd"
RESOLVED_HISTORY_DIR = "history_dir"
RESOLVED_GIT_ROOT = "git_root"
RESOLVED_FALLBACK = "fallback"


@dataclass(frozen=True)
class HistoryLocation:
    """Where local history lives for the directory a command was run from.

    ``root`` is the directory that holds (or would hold) ``.openshard/``.
    ``repo_name`` is only the folder name and ``repo_identity`` the
    canonical ``host/owner/repo`` derived from the ``origin`` remote -- no
    absolute path is ever part of the user-facing surface built from this.
    """

    root: Path
    runs_path: Path
    resolved_from: str
    repo_name: str
    repo_identity: str | None
    from_subdirectory: bool

    @property
    def display_name(self) -> str:
        """Repository identity when known, otherwise the folder name."""
        return self.repo_identity or self.repo_name

    def to_dict(self) -> dict:
        """Privacy-safe projection for ``--json`` output (no absolute paths)."""
        return {
            "identity": self.repo_identity,
            "name": self.repo_name,
            "history": HISTORY_RELPATH.as_posix(),
            "resolved_from": self.resolved_from,
            "from_subdirectory": self.from_subdirectory,
        }


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _has_history_dir(directory: Path) -> bool:
    try:
        return (directory / HISTORY_DIRNAME).is_dir()
    except OSError:
        return False


def _has_git_marker(directory: Path) -> bool:
    try:
        return (directory / ".git").exists()
    except OSError:
        return False


def _has_runs_file(directory: Path) -> bool:
    try:
        return (directory / HISTORY_RELPATH).is_file()
    except OSError:
        return False


def _label(chosen: Path, start: Path, default: str) -> str:
    return RESOLVED_CWD if chosen == start else default


def _walk(start: Path) -> tuple[Path, str]:
    nearest_history: Path | None = None
    for candidate in (start, *start.parents):
        if nearest_history is None and _has_history_dir(candidate):
            nearest_history = candidate
        if _has_git_marker(candidate):
            if _has_runs_file(candidate):
                return candidate, _label(candidate, start, RESOLVED_GIT_ROOT)
            if nearest_history is not None:
                return nearest_history, _label(nearest_history, start, RESOLVED_HISTORY_DIR)
            return candidate, _label(candidate, start, RESOLVED_GIT_ROOT)
    if nearest_history is not None:
        return nearest_history, _label(nearest_history, start, RESOLVED_HISTORY_DIR)
    return start, RESOLVED_FALLBACK


def resolve_history_root(start: Path | None = None) -> Path:
    """Return the directory whose ``.openshard/`` this command should read.

    See the module docstring for the rule. Never raises; falls back to
    *start* (default cwd) when no marker is found.
    """
    root, _ = _walk(_safe_resolve(start if start is not None else Path.cwd()))
    return root


def history_log_path(start: Path | None = None) -> Path:
    """``<history root>/.openshard/runs.jsonl`` for *start* (default cwd)."""
    return resolve_history_root(start) / HISTORY_RELPATH


def locate_history(start: Path | None = None, *, with_identity: bool = True) -> HistoryLocation:
    """Resolve the history root plus a privacy-safe description of the repository.

    ``with_identity=False`` skips the (local, single ``git config`` read)
    repository-identity lookup for callers that only need the path.
    """
    begin = _safe_resolve(start if start is not None else Path.cwd())
    root, resolved_from = _walk(begin)
    identity: str | None = None
    if with_identity:
        try:
            from openshard.history.repo_identity import capture_repo_identity

            identity = capture_repo_identity(root)
        except Exception:
            identity = None
    return HistoryLocation(
        root=root,
        runs_path=root / HISTORY_RELPATH,
        resolved_from=resolved_from,
        repo_name=root.name or str(root),
        repo_identity=identity,
        from_subdirectory=root != begin,
    )
