"""Which project a Claude capture credential belongs to, and which checkout an event touched.

Two questions the capture service answers before it records a Claude Code
event, both answered here so the service stays about queueing and folding:

* ``CheckoutResolver.capability_root``: a repository capability that does
  not verify for the root the payload's ``cwd`` resolves to may still be the
  session's project (Claude Code sends the project-directory header empty, so
  a session in a linked worktree or another checkout presents its project's
  credential from elsewhere). The candidates are the root the session was
  authorised for, the main checkout of a linked worktree, and the
  repositories the service recently recorded; only a root the credential
  verifies for is returned.
* ``CheckoutResolver.checkout_of``: the checkout an event touched -- the
  nearest enclosing ``.git`` of its file paths or ``cwd`` -- else the project.

``event_file_paths`` and ``drop_from_baseline`` support opening a checkout
with a tool event that already wrote files: those files are the session's own
work, not pre-existing changes.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openshard.adapters.claude_hooks import _is_forbidden_capture_root, _to_repo_relative
from openshard.adapters.claude_mcp_install import find_repo_root
from openshard.util.git import main_checkout_of

_CACHE_MAX = 512
# A shell command that starts by changing directory names where it really ran:
# ``cd /c/tmp/wt && git commit`` acts in ``wt`` whatever the hook's ``cwd`` says
# (Claude Code reports the shell's persistent directory, not the chain's).
_LEADING_CD = re.compile(r"""^\s*cd\s+(?P<q>["']?)(?P<path>[^"'&;|
]+?)(?P=q)\s*(?:&&|;)""")
_MSYS_DRIVE = re.compile(r"^/([A-Za-z])/(.*)$")


def command_directory_hint(command: object, cwd: str | None) -> str | None:
    """The directory a ``cd <dir> && ...`` command acts in, as a usable path, else None. Never raises."""
    if not isinstance(command, str):
        return None
    match = _LEADING_CD.match(command)
    if match is None:
        return None
    raw = match.group("path").strip()
    if not raw or raw.startswith("-"):
        return None
    if raw == "~" or raw.startswith("~/"):
        raw = str(Path.home()) + raw[1:]
    drive = _MSYS_DRIVE.match(raw)
    if drive is not None and os.name == "nt":
        raw = f"{drive.group(1).upper()}:/{drive.group(2)}"
    try:
        path = Path(raw)
        if not path.is_absolute():
            if not cwd:
                return None
            path = Path(cwd) / path
        return str(path)
    except (OSError, ValueError):
        return None


def event_file_paths(payload: Any) -> list[str]:
    """The file paths a hook event reports (raw, as the agent gave them). Never raises."""
    out: list[str] = []
    file_path = getattr(payload, "file_path", None)
    if isinstance(file_path, str) and file_path:
        out.append(file_path)
    for raw, _kind in getattr(payload, "file_paths", None) or []:
        if isinstance(raw, str) and raw and raw not in out:
            out.append(raw)
    return out


def drop_from_baseline(baseline: dict | None, raw_paths: list[str], root: Path) -> None:
    """Remove *raw_paths* (relativised to *root*) from a baseline snapshot's dirty paths, in place."""
    if not isinstance(baseline, dict) or not isinstance(baseline.get("paths"), dict):
        return
    for raw in raw_paths:
        rel = _to_repo_relative(raw, root)
        if rel is not None:
            baseline["paths"].pop(rel, None)
    if "dirty_paths" in baseline:
        baseline["dirty_paths"] = len(baseline["paths"])


class CheckoutResolver:
    """Per-service caches for the two questions above. Not thread-safe beyond dict atomicity."""

    def __init__(self, *, known_repos: Callable[[], list[Path]]) -> None:
        self._known_repos = known_repos
        # Session queue key -> the project root it was authorised for.
        self._session_roots: dict[str, Path] = {}
        # Checkout root -> the main checkout it is a linked worktree of.
        self._main_checkout_cache: dict[str, Path | None] = {}
        # cwd / file path -> the checkout containing it.
        self._checkout_cache: dict[str, Path | None] = {}

    def checkout_of(self, payload: Any, home: Path) -> Path:
        """The checkout a Claude event touched: its file paths' or ``cwd``'s repository, else *home*.

        A command that begins with ``cd <dir> &&`` acts in that directory
        and is attributed there (see ``command_directory_hint``). The
        nearest enclosing ``.git`` wins (a linked worktree has one), as for
        the hook path's own root resolution; a path outside every
        repository, or one that would resolve to the user's home directory,
        stays with the project. Cached per path.
        """
        cwd = payload.cwd if isinstance(getattr(payload, "cwd", None), str) and payload.cwd else None
        raw_paths: list[str] = []
        file_path = getattr(payload, "file_path", None)
        if isinstance(file_path, str) and file_path:
            raw_paths.append(file_path)
        raw_paths += [raw for raw, _kind in (getattr(payload, "file_paths", None) or []) if isinstance(raw, str) and raw]
        candidates: list[str] = []
        hint = command_directory_hint(getattr(payload, "command", None), cwd)
        if hint is not None:
            candidates.append(hint)
        for raw in raw_paths:
            try:
                candidate = Path(raw)
                if not candidate.is_absolute():
                    if cwd is None:
                        continue
                    candidate = Path(cwd) / candidate
                candidates.append(str(candidate.parent))
            except (OSError, ValueError):
                continue
        if cwd is not None:
            candidates.append(cwd)
        for location in candidates:
            if location in self._checkout_cache:
                found = self._checkout_cache[location]
            else:
                found = None
                try:
                    directory = Path(location)
                    if directory.is_dir():
                        root = find_repo_root(directory)
                        if root is not None and not _is_forbidden_capture_root(root):
                            found = root
                except (OSError, ValueError):
                    found = None
                if len(self._checkout_cache) >= _CACHE_MAX:
                    self._checkout_cache.clear()
                self._checkout_cache[location] = found
            if found is not None:
                return found
        return home

    def capability_root(self, cwd_root: Path, key: str, authorize: Callable[[Path], bool]) -> Path | None:
        """The project a credential refused for *cwd_root* was issued for, when the service can tell.

        Claude Code's HTTP hooks never carry the project directory: the
        ``$CLAUDE_PROJECT_DIR`` reference in the configured header is
        interpolated to an empty string, because that variable exists only
        in command-hook environments. The service therefore resolves the
        root from the payload's ``cwd`` alone, and once a session leaves its
        project root -- a linked worktree, or another checkout -- that root
        is a different repository and the capability minted for the project
        (the one whose ``.claude/settings.local.json`` fired the hook) no
        longer verifies. The event still belongs to that project, exactly
        as the command path records it (``CLAUDE_PROJECT_DIR`` first), so
        the same credential is checked against the roots the project can
        be: the one this session was opened under (``SessionStart`` is a
        command hook, so it arrives with the project directory), the main
        checkout when *cwd_root* is a linked worktree, then the repositories
        this service has recently recorded. Only a root the credential
        verifies for is returned; a capability for an unrelated repository
        is still refused, a request that explicitly named another project
        directory is refused as before, and the token never reaches this
        path.
        """
        candidates: list[Path] = []
        known = self._session_roots.get(key)
        if known is not None:
            candidates.append(known)
        main = self.main_checkout(cwd_root)
        if main is not None:
            candidates.append(main)
        candidates.extend(self._known_repos())
        seen = {str(cwd_root)}
        for candidate in candidates:
            if str(candidate) in seen:
                continue
            seen.add(str(candidate))
            try:
                if authorize(candidate):
                    return candidate
            except Exception:
                continue
        return None

    def main_checkout(self, root: Path) -> Path | None:
        """The main checkout *root* is a linked worktree of, else None. Cached per root."""
        key = str(root)
        if key not in self._main_checkout_cache:
            if len(self._main_checkout_cache) >= _CACHE_MAX:
                self._main_checkout_cache.clear()
            self._main_checkout_cache[key] = main_checkout_of(root)
        return self._main_checkout_cache[key]

    def remember(self, key: str, root: Path) -> None:
        """Remember the project root session *key* was authorised for."""
        if len(self._session_roots) >= _CACHE_MAX * 4:
            self._session_roots.clear()
        self._session_roots[key] = root
