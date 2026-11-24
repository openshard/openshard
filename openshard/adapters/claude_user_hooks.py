"""Install once, capture every repository: Claude Code hooks in the user's own settings.

``openshard setup`` and ``openshard capture install claude`` write hooks into one
repository, so a repository nobody set up records nothing. Claude Code also
reads hooks from the user's settings (``~/.claude/settings.json``, or
``$CLAUDE_CONFIG_DIR/settings.json``) for every session it starts, wherever it
starts. ``install_user_hooks`` writes Openshard's command hooks there once.

Each event still goes through ``openshard hooks claude`` and the same capture
path as a repository hook: this module decides only *whether* a user-level
event is delivered, never what is recorded. Before delivering, it requires:

* a git repository: the session's project directory (else the payload's
  ``cwd``) must be inside one. A session started in a folder that is not a
  repository (a home folder, a folder holding several repositories, a cloud
  chat's scratch folder) is *followed* instead: see "Sessions that move"
  below. No ``.openshard/`` directory is ever created outside a repository;
* not the home directory: a home directory kept as a git repository is
  refused, exactly as the capture path itself refuses it;
* no repository hooks: a repository with its own Openshard hooks
  (``.claude/settings.json`` from ``openshard setup --repo``, or
  ``.claude/settings.local.json`` from ``openshard setup``) captures through
  those, so the user-level hook stands aside and nothing is recorded twice.

Sessions that move
------------------
A session started outside any repository may still work inside one: it edits
``~/code/alpha/app.py``, or runs ``cd ~/code/alpha && pytest``. Each event's own
evidence names where it worked: the file an edit tool wrote, the directory a
shell command ``cd``-ed into (or ``git -C``), or the payload's ``cwd`` once
that is inside a repository. The first such event *binds* the session to that
repository; later events with no place of their own (a prompt, the end of a
turn, the end of the session) go to every repository the session is bound to,
so each gets its own Receipt. The session start and any prompts that came
before the first binding are held (a few, briefly) and delivered to a
repository when the session first binds to it, in their original order:
they are the session's real events, delivered late, never invented.

A file merely *read* never binds a session: reading a repository is not work
in it. The binding lives in ``~/.openshard/sessions-followed/`` (``OPENSHARD_HOME``
applies) and is deleted
at ``SessionEnd``. A repository's own hooks never fired for such a session
(Claude Code loads project hooks only from the folder the session started
in), so here the user-level hook does not stand aside for them.

Nothing here invents an event, a repository or an agent; a skipped event is
simply not delivered.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from openshard.adapters.claude_hooks_install import (
    HOOK_SPECS,
    SETTINGS_RELPATH,
    TRANSPORT_COMMAND,
    ClaudeHooksInstallResult,
    HookSpec,
    _read_settings,
    _write_settings,
    installed_events,
    is_openshard_hook,
    merge_openshard_hooks,
    remove_openshard_hooks,
)

USER_SCOPE_ARGS = "hooks claude --scope user"
_PROJECT_SETTINGS: tuple[Path, ...] = (Path(".claude") / "settings.json", SETTINGS_RELPATH)

# Every capture event, delivered as a command hook: a user-level HTTP hook
# would need a credential valid for every repository, which the capture
# service deliberately never issues (adapters/capture_auth.py).
USER_HOOK_SPECS: tuple[HookSpec, ...] = tuple(
    HookSpec(s.event, s.matcher, s.timeout, TRANSPORT_COMMAND) for s in HOOK_SPECS
)


def user_settings_path(env: Mapping[str, str] | None = None) -> Path:
    """Claude Code's user settings file: ``$CLAUDE_CONFIG_DIR/settings.json``, else ``~/.claude/settings.json``."""
    env = os.environ if env is None else env
    configured = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return base / "settings.json"


def _running_openshard() -> str | None:
    """The ``openshard`` program running this process, when it is one. Never raises."""
    try:
        argv0 = Path(sys.argv[0])
        if argv0.name.lower() in ("openshard", "openshard.exe") and argv0.is_file():
            return str(argv0.resolve())
    except (OSError, IndexError):
        return None
    return None


def user_hook_command(executable: str | None = None) -> str:
    """The hook command: this installation's ``openshard`` by absolute path, so PATH at hook time does not matter."""
    exe = executable or shutil.which("openshard") or _running_openshard() or "openshard"
    return f'"{Path(exe).as_posix()}" {USER_SCOPE_ARGS}'


def is_user_hook(hook: object) -> bool:
    """True for a hook entry written by ``install_user_hooks``."""
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    command = hook.get("command")
    return isinstance(command, str) and command.rstrip().endswith(USER_SCOPE_ARGS)


def install_user_hooks(
    *, settings_path: Path | None = None, executable: str | None = None
) -> ClaudeHooksInstallResult:
    """Merge Openshard's user-level hooks into Claude Code's user settings. Never raises.

    Only Openshard's own user-level entries are added or replaced; every other
    hook and setting is left as it was. Safe to re-run.
    """
    path = settings_path or user_settings_path()
    try:
        settings, err = _read_settings(path)
        if err or settings is None:
            return ClaudeHooksInstallResult(status="error", settings_path=path,
                                            message=err or "Could not read Claude Code settings.")
        command = user_hook_command(executable)

        def build(spec: HookSpec, _port: int) -> dict:
            return {"type": "command", "command": command, "timeout": spec.timeout}

        merged, changes = merge_openshard_hooks(settings, specs=USER_HOOK_SPECS, build_entry=build,
                                                is_ours=is_user_hook)
        if all(v == "unchanged" for v in changes.values()):
            return ClaudeHooksInstallResult(status="already_installed", settings_path=path, events=changes,
                                            message="Openshard capture is already on for every repository.")
        _write_settings(path, merged)
        installed = any(v == "added" for v in changes.values()) and not any(
            v == "updated" for v in changes.values())
        return ClaudeHooksInstallResult(
            status="installed" if installed else "updated", settings_path=path, events=changes,
            message="Openshard capture is on for every repository Claude Code works in.",
        )
    except ValueError as exc:
        return ClaudeHooksInstallResult(status="error", settings_path=path,
                                        message=f"Unexpected hooks layout in {path} ({exc}); nothing was changed.")
    except Exception as exc:
        return ClaudeHooksInstallResult(status="error", settings_path=path,
                                        message=f"Failed to write {path}: {type(exc).__name__}")


def uninstall_user_hooks(*, settings_path: Path | None = None) -> ClaudeHooksInstallResult:
    """Remove only Openshard's user-level hooks. Never raises; never touches any ``.openshard/`` history."""
    path = settings_path or user_settings_path()
    try:
        settings, err = _read_settings(path)
        if err or settings is None:
            return ClaudeHooksInstallResult(status="error", settings_path=path,
                                            message=err or "Could not read Claude Code settings.")
        merged, changes = remove_openshard_hooks(settings, specs=USER_HOOK_SPECS, is_ours=is_user_hook)
        if all(v == "absent" for v in changes.values()):
            return ClaudeHooksInstallResult(status="not_installed", settings_path=path, events=changes,
                                            message="Openshard capture was not on for every repository.")
        hooks = merged.get("hooks")
        if isinstance(hooks, dict):
            # Drop only the event lists this removal emptied, then an emptied ``hooks``.
            for event, change in changes.items():
                if change == "removed" and hooks.get(event) == []:
                    del hooks[event]
            if not hooks:
                del merged["hooks"]
        _write_settings(path, merged)
        return ClaudeHooksInstallResult(status="removed", settings_path=path, events=changes,
                                        message="Openshard capture is off for repositories without their own hooks.")
    except Exception as exc:
        return ClaudeHooksInstallResult(status="error", settings_path=path,
                                        message=f"Failed to update {path}: {type(exc).__name__}")


def user_hooks_installed(settings_path: Path | None = None) -> bool:
    """Whether Openshard's user-level hooks are present. Never raises."""
    settings, err = _read_settings(settings_path or user_settings_path())
    return not err and bool(installed_events(settings, is_ours=is_user_hook))


def _repo_hooks_can_run(env: Mapping[str, str]) -> bool:
    """Whether a repository's command hooks would find ``openshard``, exactly as their shell does. Never raises.

    They run ``openshard`` from PATH, else ``$HOME/.local/bin/openshard``
    (``repo_setup._RESOLVE``); when neither exists they record nothing.
    """
    try:
        if shutil.which("openshard", path=env.get("PATH")):
            return True
        home = (env.get("HOME") or "").strip()
        local = (Path(home) if home else Path.home()) / ".local" / "bin" / "openshard"
        return local.is_file() and os.access(local, os.X_OK)
    except Exception:
        return False


def project_has_openshard_hooks(repo_root: Path, env: Mapping[str, str] | None = None) -> bool:
    """Whether *repo_root*'s own Openshard hooks will deliver this session's events. Never raises.

    HTTP hooks post to the local capture service and always count. Command
    hooks count only when they can find ``openshard``: otherwise they record
    nothing, and the user-level hook must not stand aside for them.
    """
    from openshard.adapters.repo_setup import is_repo_hook

    env = os.environ if env is None else env
    commands_run = _repo_hooks_can_run(env)

    def delivers(hook: object) -> bool:
        if isinstance(hook, dict) and hook.get("type") == "http":
            return is_openshard_hook(hook)
        return commands_run and (is_openshard_hook(hook) or is_repo_hook(hook))

    for rel in _PROJECT_SETTINGS:
        try:
            settings, err = _read_settings(repo_root / rel)
        except Exception:
            continue
        if not err and installed_events(settings, is_ours=delivers):
            return True
    return False


def user_scope_skip_reason(raw: bytes, env: Mapping[str, str]) -> str | None:
    """Why a user-level event must not be delivered, or None to deliver it. Never raises."""
    from openshard.adapters.claude_hooks import _is_forbidden_capture_root, parse_hook_payload
    from openshard.adapters.claude_mcp_install import find_repo_root

    try:
        data = parse_hook_payload(raw)
        if data is None:
            return "empty or malformed payload"
        project = (env.get("CLAUDE_PROJECT_DIR") or "").strip()
        cwd = data.get("cwd")
        start = project or (cwd.strip() if isinstance(cwd, str) else "")
        if not start or not Path(start).is_dir():
            return "no project directory"
        root = find_repo_root(Path(start))
        if root is None:
            return "not inside a git repository"
        if _is_forbidden_capture_root(root):
            return "the home directory is never captured"
        if project_has_openshard_hooks(root, env):
            return "this repository has its own Openshard hooks"
        return None
    except Exception as exc:
        return f"could not check the project ({type(exc).__name__})"


# --- sessions that move ---------------------------------------------------------

FOLLOW_DIRNAME = "sessions-followed"
_EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
_HELD_EVENTS = frozenset({"SessionStart", "UserPromptSubmit"})
_MAX_HELD = 8
_MAX_REPOS = 10
_STALE_SECONDS = 7 * 24 * 3600
_ARG = r"""(?:"([^"]+)"|'([^']+)'|([^\s;&|]+))"""
_CD_RE = re.compile(r"^\s*(?:cd|pushd|Set-Location)\s+" + _ARG + r"\s*(?:&&|;|\||$)")
_GIT_C_RE = re.compile(r"\bgit\s+-C\s+" + _ARG)


def _follow_dir(env: Mapping[str, str]) -> Path:
    from openshard.util.home import openshard_home

    return Path(openshard_home(dict(env))) / FOLLOW_DIRNAME


def _state_path(env: Mapping[str, str], session_id: str) -> Path:
    return _follow_dir(env) / (hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32] + ".json")


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"repos": [], "held": []}
    repos = [r for r in data.get("repos") or [] if isinstance(r, str)][:_MAX_REPOS]
    held = [h for h in data.get("held") or [] if isinstance(h, str)][:_MAX_HELD]
    return {"repos": repos, "held": held}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _sweep_stale(folder: Path) -> None:
    """Delete bindings of sessions that never sent ``SessionEnd``. Best effort."""
    try:
        cutoff = time.time() - _STALE_SECONDS
        for item in folder.glob("*.json"):
            if item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
    except OSError:
        pass


def _arg(match: re.Match[str]) -> str:
    return next(g for g in match.groups() if g)


def _work_places(data: Mapping[str, object]) -> list[Path]:
    """Where this event did work, from the event's own evidence. Never raises."""
    places: list[str] = []
    raw_cwd = data.get("cwd")
    cwd = raw_cwd.strip() if isinstance(raw_cwd, str) else ""
    tool = data.get("tool_name")
    raw_input = data.get("tool_input")
    tool_input: Mapping[str, object] = raw_input if isinstance(raw_input, dict) else {}
    if tool in _EDIT_TOOLS:
        target = tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(target, str) and target.strip():
            places.append(target)
    elif tool in _SHELL_TOOLS:
        command = tool_input.get("command")
        if isinstance(command, str):
            first = _CD_RE.match(command)
            if first:
                places.append(_arg(first))
            places += [_arg(m) for m in _GIT_C_RE.finditer(command)][:3]
    if cwd:
        places.append(cwd)
    out: list[Path] = []
    for raw in places:
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                if not cwd:
                    continue
                path = Path(cwd) / path
            out.append(path if path.is_dir() else path.parent)
        except (OSError, ValueError, RuntimeError):
            continue
    return out


def _work_repos(data: Mapping[str, object]) -> list[Path]:
    """The capturable repositories this event worked in, first seen first. Never raises."""
    from openshard.adapters.claude_hooks import _is_forbidden_capture_root
    from openshard.adapters.claude_mcp_install import find_repo_root

    repos: list[Path] = []
    for place in _work_places(data):
        try:
            if not place.is_dir():
                continue
            root = find_repo_root(place)
            if root is None or _is_forbidden_capture_root(root):
                continue
            root = root.resolve()
            if root not in repos:
                repos.append(root)
        except (OSError, ValueError):
            continue
    return repos


def follow_session(raw: bytes, env: Mapping[str, str]) -> tuple[list[tuple[Path, bytes]], str]:
    """For a session started outside any repository: which ``(repository, event)`` pairs to deliver now.

    Returns the deliveries and a short note for the hook's outcome label. Never raises.
    """
    from openshard.adapters.claude_hooks import parse_hook_payload

    try:
        data = parse_hook_payload(raw)
        if data is None:
            return [], "empty or malformed payload"
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return [], "not inside a git repository"
        event = data.get("hook_event_name")
        path = _state_path(env, session_id.strip())
        state = _load_state(path)
        deliveries: list[tuple[Path, bytes]] = []
        here = _work_repos(data)
        for repo in here:
            if str(repo) in state["repos"] or len(state["repos"]) >= _MAX_REPOS:
                continue
            state["repos"].append(str(repo))
            deliveries += [(repo, held.encode("utf-8")) for held in state["held"]]
        targets = here or [Path(r) for r in state["repos"]]
        if not targets:
            if event in _HELD_EVENTS and len(state["held"]) < _MAX_HELD:
                state["held"].append(raw.decode("utf-8", "replace"))
                _save_state(path, state)
                return [], "held until the session works in a repository"
            if event == "SessionEnd":
                path.unlink(missing_ok=True)
            return [], "not inside a git repository"
        deliveries += [(repo, raw) for repo in targets if str(repo) in state["repos"]]
        if event == "SessionEnd":
            path.unlink(missing_ok=True)
            _sweep_stale(path.parent)
        else:
            if state["repos"]:
                state["held"] = []
            _save_state(path, state)
        return deliveries, "followed"
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return [], f"could not follow the session ({type(exc).__name__})"


def _session_started_in_a_repo(raw: bytes, env: Mapping[str, str]) -> bool:
    """Whether the session's own project folder is a capturable repository (the original rule). Never raises."""
    from openshard.adapters.claude_hooks import _is_forbidden_capture_root, parse_hook_payload
    from openshard.adapters.claude_mcp_install import find_repo_root

    try:
        project = (env.get("CLAUDE_PROJECT_DIR") or "").strip()
        if not project:
            data = parse_hook_payload(raw) or {}
            cwd = data.get("cwd")
            project = cwd.strip() if isinstance(cwd, str) else ""
        if not project or not Path(project).is_dir():
            return False
        root = find_repo_root(Path(project))
        return root is not None and not _is_forbidden_capture_root(root)
    except (OSError, ValueError, TypeError):
        return False


def run_user_scope_hook(
    stream: object, *, env: Mapping[str, str] | None = None, event_override: str | None = None
) -> str:
    """Body of ``openshard hooks claude --scope user``. Never raises, never prints to stdout."""
    from openshard.adapters.claude_capture_client import _read_all, run_hook_via_service

    env = os.environ if env is None else env
    raw = _read_all(stream)
    if _session_started_in_a_repo(raw, env):
        reason = user_scope_skip_reason(raw, env)
        if reason is not None:
            return f"skipped: {reason}"
        return run_hook_via_service(io.BytesIO(raw), env=dict(env), event_override=event_override)
    deliveries, note = follow_session(raw, env)
    if not deliveries:
        return f"skipped: {note}"
    outcomes = []
    for repo, payload in deliveries:
        # The repository this event belongs to, exactly as a hook started there would name it.
        outcomes.append(run_hook_via_service(io.BytesIO(payload), env={**env, "CLAUDE_PROJECT_DIR": str(repo)},
                                             event_override=event_override))
    return f"followed: {', '.join(outcomes)}"


__all__ = [
    "USER_HOOK_SPECS",
    "USER_SCOPE_ARGS",
    "follow_session",
    "install_user_hooks",
    "is_user_hook",
    "project_has_openshard_hooks",
    "run_user_scope_hook",
    "uninstall_user_hooks",
    "user_hook_command",
    "user_hooks_installed",
    "user_scope_skip_reason",
    "user_settings_path",
]
