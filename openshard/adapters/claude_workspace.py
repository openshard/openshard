"""Claude Code cloud workspaces that hold several repository checkouts.

A multi-repository Claude Code cloud session starts in the parent directory
(``/home/user``) with each repository cloned beneath it. Claude Code loads
project hooks only from that parent, so hooks installed inside each checkout
never fire. ``openshard capture install claude --workspace`` writes the hooks
into the parent too and marks it with ``.openshard/workspace.json``.

Events from such a workspace are recorded into the checkout they touched,
never into the parent:

* a tool event goes to the checkout containing its ``cwd`` or file path;
  anything else (the parent itself, a path outside every checkout) is not
  attributed to any repository;
* ``SessionStart`` and the turn's ``UserPromptSubmit`` are held in memory and
  replayed, with their original observation time: the start into a checkout
  the first time the session touches it, the prompt into each checkout the
  turn touches, so every Receipt keeps its task and start;
* ``Stop`` and ``SessionEnd`` go only to checkouts the turn touched;
* a turn's transcript usage (and status-line cost) is attributed to exactly one
  checkout: the first one the turn touched. Every other checkout's Receipt has
  no transcript and records why, so a turn is never counted twice.

Workspace routing is used only where hosted turns are sealed at ``Stop``
(``OPENSHARD_CONNECTED_SURFACE=claude-code-web``): a buffer then never spans a
turn it did not observe. Nothing here invents an event, a repository, an agent
or usage; prompts stay in memory and are never written by this module.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

WORKSPACE_MARKER = Path(".openshard") / "workspace.json"
WORKSPACE_SURFACES = frozenset({"claude-code-web"})
USAGE_ELSEWHERE_REASON = "workspace_usage_attributed_to_other_repository"
_MAX_SESSIONS = 256
_MAX_CHECKOUTS = 32


def workspace_enabled(env: dict | os._Environ | None = None) -> bool:
    env = env if env is not None else os.environ
    return env.get("OPENSHARD_CONNECTED_SURFACE") in WORKSPACE_SURFACES


def is_workspace(directory: Path) -> bool:
    """True when *directory* is an opted-in workspace and not itself inside a repository."""
    from openshard.adapters.claude_mcp_install import find_repo_root

    try:
        return (directory / WORKSPACE_MARKER).is_file() and find_repo_root(directory) is None
    except OSError:
        return False


def checkouts(workspace: Path) -> list[Path]:
    """Git checkouts directly under *workspace*, resolved, in name order. Never raises."""
    found: list[Path] = []
    try:
        for child in sorted(workspace.iterdir()):
            if len(found) >= _MAX_CHECKOUTS:
                break
            if child.is_dir() and not child.is_symlink() and (child / ".git").exists():
                found.append(child.resolve())
    except OSError:
        return found
    return found


def write_marker(workspace: Path, repos: list[Path]) -> None:
    path = workspace / WORKSPACE_MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"version": 1, "checkouts": [repo.name for repo in repos]}
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def remove_marker(workspace: Path) -> bool:
    try:
        (workspace / WORKSPACE_MARKER).unlink()
        return True
    except FileNotFoundError:
        return False


def _within(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except ValueError:
        return False


def checkout_for(workspace: Path, payload: Any) -> Path | None:
    """The checkout a tool event touched, from its file path(s) or ``cwd``. Never raises."""
    repos = checkouts(workspace)
    cwd = payload.cwd if isinstance(payload.cwd, str) and payload.cwd else None
    raw_paths = [payload.file_path] if isinstance(payload.file_path, str) and payload.file_path else []
    raw_paths += [raw for raw, _kind in (payload.file_paths or []) if isinstance(raw, str) and raw]
    candidates: list[Path] = []
    for raw in raw_paths:
        p = PurePosixPath(raw)
        if not p.is_absolute():
            if cwd is None:
                continue
            p = PurePosixPath(cwd) / p
        candidates.append(Path(os.path.normpath(str(p))))
    if cwd is not None:
        candidates.append(Path(os.path.normpath(cwd)))
    for candidate in candidates:
        for repo in repos:
            if _within(candidate, repo):
                return repo
    return None


@dataclass
class _Turn:
    session_start: tuple[Any, str] | None = None
    prompt: tuple[Any, str] | None = None
    touched: list[Path] = field(default_factory=list)  # first entry owns the turn's usage
    opened: set[Path] = field(default_factory=set)  # checkouts this session's start was replayed into


class WorkspaceRouter:
    """In-memory per-session turn state for workspace sessions. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str], _Turn] = {}

    def _turn(self, workspace: Path, key: str) -> _Turn:
        marker = (str(workspace), key)
        turn = self._sessions.get(marker)
        if turn is None:
            if len(self._sessions) >= _MAX_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)))
            turn = self._sessions[marker] = _Turn()
        return turn

    def route(self, workspace: Path, key: str, payload: Any, at: str) -> list[tuple[Path, Any, str]]:
        """``(checkout, payload, at)`` deliveries for one workspace event, in order.

        Replayed preamble events keep their original *at*. A payload going to
        a checkout that does not own the turn's usage loses its transcript
        path and carries ``USAGE_ELSEWHERE_REASON`` instead.
        """
        from openshard.adapters.claude_hooks import (
            EVENT_SESSION_END,
            EVENT_SESSION_START,
            EVENT_STOP,
            EVENT_USER_PROMPT_SUBMIT,
        )

        with self._lock:
            turn = self._turn(workspace, key)
            event = payload.event
            if event == EVENT_SESSION_START:
                turn.session_start, turn.prompt, turn.touched, turn.opened = (payload, at), None, [], set()
                return []
            if event == EVENT_USER_PROMPT_SUBMIT:
                turn.prompt, turn.touched = (payload, at), []
                return []
            if event in (EVENT_STOP, EVENT_SESSION_END):
                out = [(repo, self._for(turn, repo, payload), at) for repo in turn.touched]
                turn.touched = []
                if event == EVENT_SESSION_END:
                    self._sessions.pop((str(workspace), key), None)
                return out
            repo = checkout_for(workspace, payload)
            if repo is None:
                return []
            out = []
            if repo not in turn.touched:
                turn.touched.append(repo)
                held = [turn.prompt]
                if repo not in turn.opened:
                    # A session starts once per checkout; later turns resume it.
                    turn.opened.add(repo)
                    held.insert(0, turn.session_start)
                for item in held:
                    if item is not None:
                        out.append((repo, self._for(turn, repo, item[0]), item[1]))
            out.append((repo, self._for(turn, repo, payload), at))
            return out

    def usage_owner(self, workspace: Path, key: str) -> Path | None:
        """The checkout that owns the current turn's usage, if the turn touched one."""
        with self._lock:
            turn = self._sessions.get((str(workspace), key))
            return turn.touched[0] if turn and turn.touched else None

    @staticmethod
    def _for(turn: _Turn, repo: Path, payload: Any) -> Any:
        if turn.touched and turn.touched[0] == repo:
            return payload
        attrs = dict(payload.attrs or {})
        attrs["usage_elsewhere"] = USAGE_ELSEWHERE_REASON
        return replace(payload, transcript_path=None, attrs=attrs)


def install_workspace(workspace: Path, *, port: int | None = None) -> dict[str, Any]:
    """Configure Claude capture for every checkout under *workspace* and for *workspace* itself.

    Per-checkout hooks keep single-repository sessions (which start inside the
    checkout) working; the workspace hooks cover sessions that start in
    *workspace*. Never raises; problems are reported in the result.
    """
    from openshard.adapters.claude_hooks_install import install_claude_hooks
    from openshard.adapters.claude_mcp_install import find_repo_root

    workspace = workspace.resolve()
    if find_repo_root(workspace) is not None:
        return {"status": "error", "message": "The workspace is itself inside a git repository; install per repository instead."}
    repos = checkouts(workspace)
    if not repos:
        return {"status": "error", "message": "No git checkouts were found directly under the workspace."}
    results = {}
    for repo in repos:
        result = install_claude_hooks(repo_root=repo, port=port)
        results[repo.name] = result.status
    parent = install_claude_hooks(repo_root=workspace, port=port)
    if parent.status == "error":
        return {"status": "error", "message": parent.message, "checkouts": results}
    write_marker(workspace, repos)
    return {
        "status": "installed",
        "workspace": str(workspace),
        "checkouts": results,
        "workspace_hooks": parent.status,
        "message": "Capture hooks are configured for the workspace and each checkout; this does not install Claude or MCP.",
    }
