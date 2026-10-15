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

Claude Code's HTTP hooks carry no project directory (``$CLAUDE_PROJECT_DIR``
interpolates to an empty string there), so only the command hooks name the
workspace. ``SessionWorkspaces`` remembers it per session, and the transcript's
project folder names it after a service restart; an event that still resolves to
the workspace parent is routed like any other, never recorded into the parent. A
replayed ``SessionStart`` keeps the launch environment (provider, surface, cloud
session) its own command hook declared (``stamp_declared``).

Workspace routing is used only where hosted turns are sealed at ``Stop``
(``OPENSHARD_CONNECTED_SURFACE=claude-code-web``): a buffer then never spans a
turn it did not observe. Nothing here invents an event, a repository, an agent
or usage; prompts stay in memory and are never written by this module.

The same router serves an ordinary project session (``route`` with a
*checkout* and ``home_is_checkout=True``): the project root is itself a
checkout, so ``SessionStart`` and each prompt are recorded there (or in the
checkout the session's shell is in) as they arrive *and* held for replay into
every other checkout the turn touches -- a linked worktree under ``C:\\tmp``,
or another repository's checkout -- so each checkout's Receipt carries the
session's start and task while its file changes, branch and commits are
observed where the work happened. ``Stop``/``SessionEnd`` close every checkout
the turn touched; a turn's usage is counted once, on the first checkout it
touched, exactly as for a workspace.
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
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
    """True when *directory* is an opted-in workspace and not itself a git checkout.

    An ancestor repository (a home directory kept as a dotfiles checkout) does
    not disqualify it.
    """
    try:
        return (directory / WORKSPACE_MARKER).is_file() and not _is_checkout(directory)
    except OSError:
        return False


def _is_checkout(directory: Path) -> bool:
    return (directory / ".git").exists()


def _project_folder_name(directory: Path) -> str:
    """Claude Code's name for *directory* under ``projects/`` (every non-alphanumeric becomes ``-``)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(directory))


def workspace_from_transcript(transcript_path: str | None, cwd: str | None) -> Path | None:
    """The opted-in workspace a session was started in, from its transcript's project folder.

    Claude Code's HTTP hooks carry no project directory (``$CLAUDE_PROJECT_DIR``
    interpolates to an empty string), but the session's transcript lives in
    ``projects/<project directory with separators replaced>/``. When that
    folder names *cwd* or one of its ancestors and that directory is an
    opted-in workspace, the session belongs to it. Nothing else is inferred:
    no match, or an unreadable path, is ``None``. Never raises.
    """
    if not transcript_path or not cwd:
        return None
    try:
        folder = Path(transcript_path).parent.name
        directory = Path(os.path.realpath(cwd))
        for candidate in (directory, *directory.parents):
            if _project_folder_name(candidate) == folder:
                return candidate if is_workspace(candidate) else None
    except (OSError, ValueError):
        return None
    return None


def checkouts(workspace: Path) -> list[Path]:
    """Git checkouts directly under *workspace*, resolved, in name order. Never raises."""
    found: list[Path] = []
    try:
        for child in sorted(workspace.iterdir()):
            if len(found) >= _MAX_CHECKOUTS:
                break
            if child.is_dir() and not child.is_symlink() and _is_checkout(child):
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
        p = Path(raw)  # native: a Windows hook reports ``C:\\...`` paths
        if not p.is_absolute():
            if cwd is None:
                continue
            p = Path(cwd) / p
        candidates.append(p)
    if cwd is not None:
        candidates.append(Path(cwd))
    # Checkouts are resolved, so resolve candidates too: symlinked workspaces
    # and Windows short (8.3) names must still name the same checkout.
    candidates = [Path(os.path.realpath(c)) for c in candidates]
    for candidate in candidates:
        for repo in repos:
            if _within(candidate, repo):
                return repo
    return None


class SessionWorkspaces:
    """Which opted-in workspace each Claude session was started in.

    Only command hooks (SessionStart, the status line) carry the project
    directory, so the workspace is remembered per session from them and
    matched to the HTTP hooks that follow; after a service restart the
    transcript's project folder names it instead (``workspace_from_transcript``).
    """

    def __init__(self) -> None:
        self._by_key: dict[str, Path] = {}

    def remember(self, key: str, workspace: Path, payload: Any) -> None:
        """Remember *workspace* for the session; its SessionEnd forgets it."""
        from openshard.adapters.claude_hooks import EVENT_SESSION_END

        if getattr(payload, "event", None) == EVENT_SESSION_END:
            self._by_key.pop(key, None)
            return
        if len(self._by_key) >= _MAX_SESSIONS:
            self._by_key.pop(next(iter(self._by_key)))
        self._by_key[key] = workspace

    def infer(self, key: str, payload: Any, authorize: Callable[[Path], bool] | None = None) -> Path | None:
        """The workspace a Claude event with no project directory belongs to, else None.

        An inference never turns a refusal into a rejection: a workspace the
        presented credential is not valid for is simply not inferred.
        """
        if not workspace_enabled() or payload.agent != "claude_code":
            return None
        found = self._by_key.get(key) or workspace_from_transcript(payload.transcript_path, payload.cwd)
        return found if found is not None and (authorize is None or authorize(found)) else None


def declared_workspace(project_dir: str | None, agent: str) -> Path | None:
    """The opted-in multi-repository workspace *project_dir* names, if cloud routing applies."""
    if not project_dir or agent != "claude_code" or not workspace_enabled():
        return None
    try:
        directory = Path(project_dir).resolve()
    except OSError:
        return None
    return directory if is_workspace(directory) else None


def is_cloud_workspace(directory: Path, agent: str) -> bool:
    """Whether *directory* is an opted-in workspace parent that must never hold a Claude Receipt itself."""
    return agent == "claude_code" and workspace_enabled() and is_workspace(directory)


def stamp_declared(payload: Any, correlation: dict | None, task_id: str | None, agent_env: dict[str, str] | None) -> None:
    """Attach the request's declared context to *payload* before it can be held for replay.

    A held ``SessionStart`` is replayed later, on the back of another event; an
    HTTP hook carries neither the agent environment (provider, surface, cloud
    session) nor always the task, so the context must travel with the start.
    """
    from openshard.adapters.claude_hooks import StatusPayload, apply_agent_env
    from openshard.history.correlation import correlation_block
    from openshard.history.task_identity import is_task_id

    if isinstance(payload, StatusPayload):
        return
    if (block := correlation_block(correlation)) is not None:
        payload.correlation = block
    if is_task_id(task_id):
        payload.task_id = task_id
    apply_agent_env(payload, agent_env)


@dataclass
class _Turn:
    session_start: tuple[Any, str] | None = None
    prompt: tuple[Any, str] | None = None
    touched: list[Path] = field(default_factory=list)  # first entry owns the turn's usage
    opened: set[Path] = field(default_factory=set)  # checkouts this session's start was replayed into
    last_owner: Path | None = None  # owner of the turn Stop just closed (a SessionEnd after it has no turn)
    # A project session keeps one Receipt per checkout for its whole life, so
    # its usage has one owner for its whole life: the first checkout a turn
    # touched (not one that only saw the replayed session start).
    # (A workspace session seals a Receipt per turn, so each turn's first
    # checkout owns that turn.) Per-turn owners on long-lived Receipts counted
    # usage twice: a checkout owning a later turn read the transcript from its
    # own cursor, through the turns other checkouts had already counted.
    session_owner: Path | None = None


class WorkspaceRouter:
    """In-memory per-session turn state for workspace sessions. Thread-safe."""

    def __init__(self, state_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str], _Turn] = {}
        # Where a project session's usage owner outlives a service restart: beside
        # the capture service's state file, never in a repository (a project that
        # recorded nothing must hold no capture state). No state file: memory only.
        self._owner_dir = state_path.with_name("claude-usage-owners") if state_path is not None else None

    def _turn(self, workspace: Path, key: str) -> _Turn:
        marker = (str(workspace), key)
        turn = self._sessions.get(marker)
        if turn is None:
            if len(self._sessions) >= _MAX_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)))
            # A restarted service has no memory of the session: keep the owner it chose before.
            turn = self._sessions[marker] = _Turn(session_owner=_read_owner(self._owner_dir, workspace, key))
        return turn

    def _own(self, turn: _Turn, workspace: Path, key: str, repo: Path) -> None:
        if turn.session_owner is None:
            turn.session_owner = repo
            _write_owner(self._owner_dir, workspace, key, repo)

    def route(
        self, workspace: Path, key: str, payload: Any, at: str, *,
        checkout: Path | None = None, home_is_checkout: bool = False,
    ) -> list[tuple[Path, Any, str]]:
        """``(checkout, payload, at)`` deliveries for one event, in order.

        *workspace* keys the session's turn state: the workspace parent, or
        the project root of an ordinary session. *checkout* is the checkout
        the event touched when the caller already knows it (a project
        session resolves it from the payload's ``cwd`` and file paths);
        otherwise it is looked up among the workspace's checkouts. With
        *home_is_checkout* the preamble events (``SessionStart``, the turn's
        prompt) are delivered to *checkout* at once as well as held for
        replay, because the project root records them itself.

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
            if event in (EVENT_SESSION_START, EVENT_USER_PROMPT_SUBMIT):
                if event == EVENT_SESSION_START:
                    turn.session_start, turn.prompt, turn.touched, turn.opened = (payload, at), None, [], set()
                else:
                    turn.prompt, turn.touched = (payload, at), []
                if not home_is_checkout or checkout is None:
                    return []
                if event == EVENT_USER_PROMPT_SUBMIT:
                    # A harness notification is not work: the shell merely sitting
                    # in another checkout when one arrives never opens that
                    # checkout's Receipt (it is still held and replayed if the turn
                    # does work there). A user's prompt keeps opening the checkout
                    # its cwd is in *before* any command runs, so the checkout's
                    # baseline is taken before the turn's work, never after it.
                    if _is_notification(payload):
                        checkout = self._open_or_home(turn, workspace, checkout)
                    self._own(turn, workspace, key, checkout)  # where the session's first turn began
                return self._deliver(turn, checkout, payload, at)
            if event in (EVENT_STOP, EVENT_SESSION_END):
                targets = list(turn.touched)
                if event == EVENT_SESSION_END:
                    # The session ends in every checkout it was opened in, not
                    # only in the ones the last turn touched.
                    targets += [repo for repo in turn.opened if repo not in targets]
                    # The usage owner was opened in this session even when a restarted
                    # service no longer remembers it: it must see the end and fold its usage.
                    if turn.session_owner is not None and turn.session_owner not in targets:
                        targets.append(turn.session_owner)
                out = [(repo, self._for(turn, repo, payload), at) for repo in targets]
                if turn.touched:
                    turn.last_owner = turn.touched[0]
                turn.touched = []
                if event == EVENT_SESSION_END:
                    self._sessions.pop((str(workspace), key), None)
                    if self._owner_dir is not None:
                        _owner_path(self._owner_dir, workspace, key).unlink(missing_ok=True)
                return out
            repo = checkout if checkout is not None else checkout_for(workspace, payload)
            if repo is None:
                return []
            if home_is_checkout:
                if _only_reads(payload):
                    # Looking at another checkout (``git log``, ``ls``...) is not
                    # working in it: recorded with the session's own Receipt.
                    repo = self._open_or_home(turn, workspace, repo)
                self._own(turn, workspace, key, repo)
            return self._deliver(turn, repo, payload, at)

    @staticmethod
    def _open_or_home(turn: _Turn, home: Path, repo: Path) -> Path:
        """*repo* when this session already opened it (or it is *home*), else the session owner or *home*."""
        if repo == home or repo in turn.opened:
            return repo
        return turn.session_owner or home

    @staticmethod
    def _deliver(turn: _Turn, repo: Path, payload: Any, at: str) -> list[tuple[Path, Any, str]]:
        """*payload* for *repo*, preceded by the held start/prompt the first time this turn touches it."""
        out = []
        if repo not in turn.touched:
            turn.touched.append(repo)
            held = [turn.prompt]
            if repo not in turn.opened:
                # A session starts once per checkout; later turns resume it.
                turn.opened.add(repo)
                held.insert(0, turn.session_start)
            for item in held:
                if item is not None and item[0] is not payload:
                    out.append((repo, WorkspaceRouter._for(turn, repo, item[0]), item[1]))
        out.append((repo, WorkspaceRouter._for(turn, repo, payload), at))
        return out

    def usage_owner(self, workspace: Path, key: str) -> Path | None:
        """The checkout that owns the current usage: the session's owner, else the turn's first checkout."""
        with self._lock:
            turn = self._sessions.get((str(workspace), key))
            if turn is None:
                return None
            return turn.session_owner or (turn.touched[0] if turn.touched else None)

    @staticmethod
    def _for(turn: _Turn, repo: Path, payload: Any) -> Any:
        owner = turn.session_owner or (turn.touched[0] if turn.touched else turn.last_owner)
        if owner is None or owner == repo:
            return payload
        attrs = dict(payload.attrs or {})
        attrs["usage_elsewhere"] = USAGE_ELSEWHERE_REASON
        return replace(payload, attrs=attrs)


def _is_notification(payload: Any) -> bool:
    """True for a harness-delivered prompt (``<task-notification>``...), never for a user's prompt."""
    from openshard.adapters.claude_session_fidelity import prompt_source

    prompt = getattr(payload, "prompt", None)
    return isinstance(prompt, str) and prompt_source(prompt) is not None


def _only_reads(payload: Any) -> bool:
    """True for a shell tool event whose single command is on the narrow read-only allowlist."""
    from openshard.adapters.claude_capture_routing import is_read_only_command
    from openshard.adapters.claude_hooks import COMMAND_TOOLS

    if getattr(payload, "tool_name", None) not in COMMAND_TOOLS:
        return False
    if getattr(payload, "file_path", None) or getattr(payload, "file_paths", None):
        return False
    return is_read_only_command(getattr(payload, "command", None))


def _owner_path(owner_dir: Path, workspace: Path, key: str) -> Path:
    import hashlib

    project = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:16]
    return owner_dir / f"{project}-{key}.usage-owner"


def _read_owner(owner_dir: Path | None, workspace: Path, key: str) -> Path | None:
    """The checkout a project session's usage was given to before a service restart."""
    if owner_dir is None:
        return None
    try:
        raw = _owner_path(owner_dir, workspace, key).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(raw) if raw and (Path(raw) / ".git").exists() else None


def _write_owner(owner_dir: Path | None, workspace: Path, key: str, repo: Path) -> None:
    """Only project sessions keep one Receipt per checkout for life; a workspace marker is harmless."""
    if owner_dir is None:
        return
    try:
        path = _owner_path(owner_dir, workspace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(repo), encoding="utf-8")
    except OSError:
        pass


def note_usage_owner(buf: dict, payload: Any) -> None:
    """Mark a Receipt whose session usage another checkout's Receipt owns, for good.

    Such a Receipt still reads the transcript for the model that ran, but
    never counts or prices its tokens (claude_hooks.build_hook_entry), so a
    later event carrying the path cannot make it count the same usage twice.
    """
    if (payload.attrs or {}).get("usage_elsewhere") == USAGE_ELSEWHERE_REASON:
        buf["usage_elsewhere"] = USAGE_ELSEWHERE_REASON
    if payload.transcript_path:
        # Transient: read at fold for usage and model identity (claude_hooks._transcript_usage).
        buf["transcript_path"] = payload.transcript_path


def install_workspace(workspace: Path, *, port: int | None = None) -> dict[str, Any]:
    """Configure Claude capture for every checkout under *workspace* and for *workspace* itself.

    Per-checkout hooks keep single-repository sessions (which start inside the
    checkout) working; the workspace hooks cover sessions that start in
    *workspace*. Never raises; problems are reported in the result.
    """
    from openshard.adapters.claude_hooks_install import install_claude_hooks

    workspace = workspace.resolve()
    if _is_checkout(workspace):
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
