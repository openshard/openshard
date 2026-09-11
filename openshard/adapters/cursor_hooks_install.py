"""Cursor hook installation for OpenShard auto-capture (0.4.2).

Writes the hook configuration that delivers Cursor's agent hooks to
OpenShard into the repository's project-local ``.cursor/hooks.json``.

Layout
------
Cursor's file is *not* the matcher-group layout Claude Code and Codex
share (``{"hooks": {Event: [{"matcher"?, "hooks": [entry, ...]}]}}``). It is
one level flatter, with a top-level version::

    {
      "version": 1,
      "hooks": {
        "<event>": [ {"command": "...", "timeout": 5, "failClosed": false}, ... ]
      }
    }

so this module carries its own small merge/remove for that shape, with the
same rules as ``claude_hooks_install.merge_openshard_hooks``: idempotent
(an unchanged file is left byte-for-byte alone), preserves every other
hook, event and key, never overwrites a file it cannot parse, never raises
from the public functions. File reading/writing and the git-exclude step
are the shared helpers.

Why project-local
-----------------
Cursor reads ``~/.cursor/hooks.json`` (every workspace on the machine)
and ``<project>/.cursor/hooks.json`` (this repository); enterprise/team
files take precedence over both. Project scope matches the other
integrations' "this user, this repository" choice. A file OpenShard
*creates* is added to ``.git/info/exclude``; a pre-existing (possibly
shared) file is merged into and its git status is left to the user.
Cursor watches the file and reloads it on save, so no restart is needed.

Why every hook is a command, and always fail-open
-------------------------------------------------
Cursor has ``command`` and ``prompt`` hook types only -- no HTTP hook --
so each event runs ``openshard hooks cursor``, a fresh process whose only
work is a loopback POST to the warm capture service plus the decision
reply Cursor expects on stdout. Every entry is installed with
``failClosed: false`` (Cursor's default, stated explicitly): a crash,
timeout or bad reply from OpenShard must never block a prompt or a tool.
``sessionEnd`` uses ``--no-spawn`` and a short timeout so it never tries
to start a service it could not wait for.
"""

from __future__ import annotations

import copy
from pathlib import Path

from openshard.adapters.claude_hooks_install import (
    TRANSPORT_COMMAND,
    ClaudeHooksInstallResult,
    HookSpec,
    _read_settings,
    _write_settings,
    ensure_local_settings_ignored,
)
from openshard.adapters.cursor_hooks import CURSOR_HOOK_EVENTS

HOOK_COMMAND = "openshard hooks cursor"
NO_SPAWN_FLAG = "--no-spawn"
HOOKS_RELPATH = Path(".cursor") / "hooks.json"
HOOKS_FILE_VERSION = 1

HOOK_SPECS: tuple[HookSpec, ...] = (
    HookSpec("sessionStart", None, 15, TRANSPORT_COMMAND),  # starts the service when needed
    HookSpec("beforeSubmitPrompt", None, 5, TRANSPORT_COMMAND),  # blocking; reply is always "continue"
    HookSpec("postToolUse", None, 5, TRANSPORT_COMMAND),
    HookSpec("postToolUseFailure", None, 5, TRANSPORT_COMMAND),
    HookSpec("afterFileEdit", None, 5, TRANSPORT_COMMAND),
    HookSpec("stop", None, 5, TRANSPORT_COMMAND),
    HookSpec("sessionEnd", None, 3, TRANSPORT_COMMAND),
)
HOOK_EVENTS: tuple[str, ...] = tuple(s.event for s in HOOK_SPECS)
assert set(HOOK_EVENTS) == set(CURSOR_HOOK_EVENTS)
# Events whose budget is too small to start a service in.
_NO_SPAWN_EVENTS: frozenset[str] = frozenset({"sessionEnd"})


def _hook_entry(spec: HookSpec) -> dict:
    command = HOOK_COMMAND
    if spec.event in _NO_SPAWN_EVENTS:
        command = f"{HOOK_COMMAND} {NO_SPAWN_FLAG}"
    return {"type": "command", "command": command, "timeout": spec.timeout, "failClosed": False}


def is_openshard_cursor_hook(hook: object) -> bool:
    """True for a hook entry that is OpenShard's Cursor command hook."""
    if not isinstance(hook, dict):
        return False
    if hook.get("type") not in (None, "command"):
        return False
    command = hook.get("command")
    if not isinstance(command, str):
        return False
    stripped = command.strip()
    return stripped == HOOK_COMMAND or stripped.startswith(HOOK_COMMAND + " ")


def build_hook_config() -> dict[str, list[dict]]:
    """The exact ``hooks`` block OpenShard installs (fresh-file shape)."""
    return {spec.event: [_hook_entry(spec)] for spec in HOOK_SPECS}


def merge_cursor_hooks(settings: dict) -> tuple[dict, dict[str, str]]:
    """Return ``(new_settings, changes)`` with OpenShard's Cursor hooks merged in.

    ``changes`` maps each event to ``"added"`` / ``"updated"`` /
    ``"unchanged"``. The input is never mutated. Raises ``ValueError`` when
    the existing structure is not Cursor's documented shape, so the caller
    refuses to write rather than clobber the file.
    """
    new_settings = copy.deepcopy(settings)
    hooks = new_settings.get("hooks")
    if hooks is None:
        hooks = {}
        new_settings["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise ValueError("'hooks' is not a JSON object")
    if "version" not in new_settings:
        new_settings["version"] = HOOKS_FILE_VERSION

    changes: dict[str, str] = {}
    for spec in HOOK_SPECS:
        entries = hooks.get(spec.event)
        if entries is None:
            entries = []
            hooks[spec.event] = entries
        if not isinstance(entries, list):
            raise ValueError(f"'hooks.{spec.event}' is not a JSON array")
        desired = _hook_entry(spec)
        ours = [h for h in entries if is_openshard_cursor_hook(h)]
        others = [h for h in entries if not is_openshard_cursor_hook(h)]
        if not ours:
            entries.append(desired)
            changes[spec.event] = "added"
        elif len(ours) == 1 and ours[0] == desired:
            changes[spec.event] = "unchanged"
        else:
            entries[:] = others + [desired]
            changes[spec.event] = "updated"
    return new_settings, changes


def remove_cursor_hooks(settings: dict) -> tuple[dict, dict[str, str]]:
    """Return ``(new_settings, changes)`` with OpenShard's Cursor hooks removed.

    ``changes`` maps each event to ``"removed"`` / ``"absent"``. Only
    entries identified by ``is_openshard_cursor_hook`` are removed;
    unrelated entries and every other key survive. The input is never
    mutated.
    """
    new_settings = copy.deepcopy(settings)
    hooks = new_settings.get("hooks")
    if not isinstance(hooks, dict):
        return new_settings, {spec.event: "absent" for spec in HOOK_SPECS}
    changes: dict[str, str] = {}
    for spec in HOOK_SPECS:
        entries = hooks.get(spec.event)
        if not isinstance(entries, list):
            changes[spec.event] = "absent"
            continue
        others = [h for h in entries if not is_openshard_cursor_hook(h)]
        changes[spec.event] = "removed" if len(others) != len(entries) else "absent"
        hooks[spec.event] = others
    return new_settings, changes


def installed_cursor_events(settings: object) -> list[str]:
    """Events (of ``HOOK_EVENTS``) that already carry an OpenShard hook in *settings*."""
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks"), dict):
        return []
    found: list[str] = []
    for event in HOOK_EVENTS:
        entries = settings["hooks"].get(event)
        if isinstance(entries, list) and any(is_openshard_cursor_hook(h) for h in entries):
            found.append(event)
    return found


def load_cursor_hooks(repo_root: Path) -> tuple[dict | None, str | None]:
    """Read-only ``(config, error)`` for ``<repo_root>/.cursor/hooks.json``."""
    return _read_settings(Path(repo_root) / HOOKS_RELPATH)


def _error(message: str, path: Path | None = None) -> ClaudeHooksInstallResult:
    return ClaudeHooksInstallResult(status="error", settings_path=path, message=message)


def install_cursor_hooks(*, repo_root: Path) -> ClaudeHooksInstallResult:
    """Merge OpenShard's Cursor hooks into ``<repo_root>/.cursor/hooks.json``. Never raises."""
    try:
        root = Path(repo_root)
        path = root / HOOKS_RELPATH
        existed = path.exists()
        settings, err = _read_settings(path)
        if err or settings is None:
            return _error(err or "Could not read Cursor hooks configuration.", path)
        try:
            merged, changes = merge_cursor_hooks(settings)
        except ValueError as exc:
            return _error(f"{path} has an unexpected hooks layout ({exc}); OpenShard will not modify it.", path)
        warnings: list[str] = []
        if all(v == "unchanged" for v in changes.values()):
            status = "already_installed"
            message = "Cursor auto-capture hooks already configured for this repository."
        else:
            _write_settings(path, merged)
            status = "installed" if all(v == "added" for v in changes.values()) else "updated"
            message = "Cursor auto-capture hooks configured."
        if not existed:
            ignore_warning = ensure_local_settings_ignored(
                root, HOOKS_RELPATH.as_posix(), note="added by openshard capture install cursor",
            )
            if ignore_warning:
                warnings.append(ignore_warning)
        return ClaudeHooksInstallResult(
            status=status, settings_path=path, events=changes, message=message, warnings=warnings,
        )
    except Exception as exc:
        return _error(f"Failed to configure Cursor hooks: {type(exc).__name__}")


def uninstall_cursor_hooks(*, repo_root: Path) -> ClaudeHooksInstallResult:
    """Remove OpenShard's Cursor hooks from ``<repo_root>/.cursor/hooks.json``. Never raises.

    Only entries identified by ``is_openshard_cursor_hook`` are removed;
    unrelated hooks and keys survive. ``.openshard/`` is untouched.
    """
    try:
        root = Path(repo_root)
        path = root / HOOKS_RELPATH
        settings, err = _read_settings(path)
        if err or settings is None:
            return _error(err or "Could not read Cursor hooks configuration.", path)
        merged, changes = remove_cursor_hooks(settings)
        if all(v == "absent" for v in changes.values()):
            return ClaudeHooksInstallResult(
                status="not_installed", settings_path=path, events=changes,
                message="No OpenShard Cursor hooks were configured.",
            )
        _write_settings(path, merged)
        return ClaudeHooksInstallResult(
            status="removed", settings_path=path, events=changes, message="Cursor auto-capture hooks removed.",
        )
    except Exception as exc:
        return _error(f"Failed to remove Cursor hooks: {type(exc).__name__}")
