"""Google Antigravity hook installation for OpenShard auto-capture (0.4.7).

Writes the hook configuration that delivers Antigravity's agent hooks to
OpenShard into the repository's project-local ``.agents/hooks.json``.

Layout
------
Antigravity's ``hooks.json`` maps *hook names* to event configurations.
``PreToolUse`` / ``PostToolUse`` take matcher groups (the matcher is a
regular expression; ``*`` is rejected, ``.*`` matches every tool) and the
other events take a plain handler list::

    {
      "openshard": {
        "PreInvocation": [ {"type": "command", "command": "openshard hooks antigravity --event PreInvocation"} ],
        "PostToolUse":   [ {"matcher": ".*", "hooks": [ {"type": "command", "command": "..."} ]} ],
        "Stop":          [ {"type": "command", "command": "..."} ]
      },
      "<someone else's hook>": { ... }
    }

OpenShard owns exactly one name, ``openshard``, and never touches any other
key. Install is idempotent (an unchanged file is left byte-for-byte
alone), never overwrites a file it cannot parse, refuses when an
``openshard`` entry exists that holds a command that is not OpenShard's,
and never raises from the public functions. File reading/writing and the
git-exclude step are the shared helpers.

Why the event is on the command line
------------------------------------
Antigravity's stdin document does not reliably name its event, so each
handler passes ``--event <Name>``. No ``timeout`` is written: the
documented default (30 s) is ample for a loopback POST and the unit of the
field is not pinned down by the reference, so guessing it could only make
the hook time out spuriously.

Why project-local
-----------------
Antigravity reads ``.agents/hooks.json`` in the workspace and
``~/.gemini/config/hooks.json`` for the user. Project scope matches every
other integration's "this user, this repository" choice. A file OpenShard
*creates* is added to ``.git/info/exclude``; a pre-existing (possibly
shared) file is merged into and its git status is left to the user.
"""

from __future__ import annotations

import copy
from pathlib import Path

from openshard.adapters.antigravity_hooks import ANTIGRAVITY_HOOK_EVENTS
from openshard.adapters.claude_hooks_install import (
    ClaudeHooksInstallResult,
    _read_settings,
    _write_settings,
    ensure_local_settings_ignored,
)

HOOK_NAME = "openshard"
HOOK_COMMAND = "openshard hooks antigravity"
HOOKS_RELPATH = Path(".agents") / "hooks.json"
MATCH_ALL_TOOLS = ".*"
HOOK_EVENTS: tuple[str, ...] = ("PreInvocation", "PostToolUse", "Stop")
assert set(HOOK_EVENTS) == set(ANTIGRAVITY_HOOK_EVENTS)
# Events whose configuration is a list of matcher groups rather than handlers.
_MATCHER_EVENTS: frozenset[str] = frozenset({"PostToolUse"})


def _handler(event: str) -> dict:
    return {"type": "command", "command": f"{HOOK_COMMAND} --event {event}"}


def _event_entries(event: str) -> list[dict]:
    if event in _MATCHER_EVENTS:
        return [{"matcher": MATCH_ALL_TOOLS, "hooks": [_handler(event)]}]
    return [_handler(event)]


def build_hook_config() -> dict[str, list[dict]]:
    """The exact value OpenShard installs under its ``openshard`` hook name."""
    return {event: _event_entries(event) for event in HOOK_EVENTS}


def is_openshard_antigravity_hook(handler: object) -> bool:
    """True for a handler that is OpenShard's Antigravity command hook."""
    if not isinstance(handler, dict) or handler.get("type") not in (None, "command"):
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    stripped = command.strip()
    return stripped == HOOK_COMMAND or stripped.startswith(HOOK_COMMAND + " ")


def _handlers(entries: object) -> list[object]:
    """Every handler in one event's configuration, whichever layout it uses."""
    if not isinstance(entries, list):
        return []
    found: list[object] = []
    for item in entries:
        if isinstance(item, dict) and isinstance(item.get("hooks"), list):
            found.extend(item["hooks"])
        else:
            found.append(item)
    return found


def _foreign_commands(hook: dict) -> bool:
    return any(
        not is_openshard_antigravity_hook(h)
        for key, entries in hook.items() if key != "enabled"
        for h in _handlers(entries)
    )


def merge_antigravity_hooks(settings: dict) -> tuple[dict, dict[str, str]]:
    """Return ``(new_settings, changes)`` with OpenShard's ``openshard`` hook set.

    ``changes`` maps each event to ``"added"`` / ``"updated"`` /
    ``"unchanged"``. The input is never mutated. Raises ``ValueError`` when
    an ``openshard`` entry exists that is not OpenShard's, so the caller
    refuses to write rather than clobber a user's configuration.
    """
    new_settings = copy.deepcopy(settings)
    existing = new_settings.get(HOOK_NAME)
    if existing is not None:
        if not isinstance(existing, dict):
            raise ValueError(f"'{HOOK_NAME}' is not a JSON object")
        if _foreign_commands(existing):
            raise ValueError(f"'{HOOK_NAME}' holds commands that are not OpenShard's")
    existing = existing or {}
    changes: dict[str, str] = {}
    for event in HOOK_EVENTS:
        desired = _event_entries(event)
        current = existing.get(event)
        if current is None:
            changes[event] = "added"
        elif current == desired:
            changes[event] = "unchanged"
        else:
            changes[event] = "updated"
    extra = [k for k in existing if k not in HOOK_EVENTS and k != "enabled"]
    if extra:
        changes.update({k: "updated" for k in extra})  # stale events OpenShard no longer subscribes to
    new_settings[HOOK_NAME] = build_hook_config()
    if "enabled" in existing:
        new_settings[HOOK_NAME]["enabled"] = existing["enabled"]
    return new_settings, changes


def remove_antigravity_hooks(settings: dict) -> tuple[dict, dict[str, str]]:
    """Return ``(new_settings, changes)`` without OpenShard's ``openshard`` hook.

    Only an ``openshard`` entry made entirely of OpenShard's own commands is
    removed; every other key survives. The input is never mutated.
    """
    new_settings = copy.deepcopy(settings)
    existing = new_settings.get(HOOK_NAME)
    if not isinstance(existing, dict) or _foreign_commands(existing):
        return new_settings, {event: "absent" for event in HOOK_EVENTS}
    changes = {event: ("removed" if event in existing else "absent") for event in HOOK_EVENTS}
    del new_settings[HOOK_NAME]
    if all(v == "absent" for v in changes.values()):
        changes = {event: "removed" for event in HOOK_EVENTS}  # an empty entry of ours is still ours
    return new_settings, changes


def installed_antigravity_events(settings: object) -> list[str]:
    """Events (of ``HOOK_EVENTS``) that already carry OpenShard's handler in *settings*."""
    if not isinstance(settings, dict) or not isinstance(settings.get(HOOK_NAME), dict):
        return []
    hook = settings[HOOK_NAME]
    return [
        event for event in HOOK_EVENTS
        if any(is_openshard_antigravity_hook(h) for h in _handlers(hook.get(event)))
    ]


def load_antigravity_hooks(repo_root: Path) -> tuple[dict | None, str | None]:
    """Read-only ``(config, error)`` for ``<repo_root>/.agents/hooks.json``."""
    return _read_settings(Path(repo_root) / HOOKS_RELPATH)


def _error(message: str, path: Path | None = None) -> ClaudeHooksInstallResult:
    return ClaudeHooksInstallResult(status="error", settings_path=path, message=message)


def install_antigravity_hooks(*, repo_root: Path) -> ClaudeHooksInstallResult:
    """Add OpenShard's hook to ``<repo_root>/.agents/hooks.json``. Never raises."""
    try:
        root = Path(repo_root)
        path = root / HOOKS_RELPATH
        existed = path.exists()
        settings, err = _read_settings(path)
        if err or settings is None:
            return _error(err or "Could not read Antigravity hooks configuration.", path)
        try:
            merged, changes = merge_antigravity_hooks(settings)
        except ValueError as exc:
            return _error(f"{path}: {exc}; OpenShard will not modify it.", path)
        warnings: list[str] = []
        if merged == settings:
            status = "already_installed"
            message = "Google Antigravity auto-capture hooks already configured for this repository."
        else:
            _write_settings(path, merged)
            status = "installed" if all(v == "added" for v in changes.values()) else "updated"
            message = "Google Antigravity auto-capture hooks configured."
        if not existed:
            ignore_warning = ensure_local_settings_ignored(
                root, HOOKS_RELPATH.as_posix(), note="added by openshard capture install antigravity",
            )
            if ignore_warning:
                warnings.append(ignore_warning)
        return ClaudeHooksInstallResult(
            status=status, settings_path=path, events=changes, message=message, warnings=warnings,
        )
    except Exception as exc:
        return _error(f"Failed to configure Antigravity hooks: {type(exc).__name__}")


def uninstall_antigravity_hooks(*, repo_root: Path) -> ClaudeHooksInstallResult:
    """Remove OpenShard's hook from ``<repo_root>/.agents/hooks.json``. Never raises."""
    try:
        root = Path(repo_root)
        path = root / HOOKS_RELPATH
        settings, err = _read_settings(path)
        if err or settings is None:
            return _error(err or "Could not read Antigravity hooks configuration.", path)
        merged, changes = remove_antigravity_hooks(settings)
        if merged == settings:
            return ClaudeHooksInstallResult(
                status="not_installed", settings_path=path, events=changes,
                message="No OpenShard Antigravity hooks were configured.",
            )
        _write_settings(path, merged)
        return ClaudeHooksInstallResult(
            status="removed", settings_path=path, events=changes,
            message="Google Antigravity auto-capture hooks removed.",
        )
    except Exception as exc:
        return _error(f"Failed to remove Antigravity hooks: {type(exc).__name__}")
