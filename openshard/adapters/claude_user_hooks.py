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
  ``cwd``) must be inside one. A session in a folder that is not a
  repository, including a folder that holds several repositories, records
  nothing, and no ``.openshard/`` directory is created there;
* not the home directory: a home directory kept as a git repository is
  refused, exactly as the capture path itself refuses it;
* no repository hooks: a repository with its own Openshard hooks
  (``.claude/settings.json`` from ``openshard setup --repo``, or
  ``.claude/settings.local.json`` from ``openshard setup``) captures through
  those, so the user-level hook stands aside and nothing is recorded twice.

Nothing here invents an event, a repository or an agent; a skipped event is
simply not delivered.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
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


def run_user_scope_hook(
    stream: object, *, env: Mapping[str, str] | None = None, event_override: str | None = None
) -> str:
    """Body of ``openshard hooks claude --scope user``. Never raises, never prints to stdout."""
    from openshard.adapters.claude_capture_client import _read_all, run_hook_via_service

    env = os.environ if env is None else env
    raw = _read_all(stream)
    reason = user_scope_skip_reason(raw, env)
    if reason is not None:
        return f"skipped: {reason}"
    return run_hook_via_service(io.BytesIO(raw), env=dict(env), event_override=event_override)


__all__ = [
    "USER_HOOK_SPECS",
    "USER_SCOPE_ARGS",
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
