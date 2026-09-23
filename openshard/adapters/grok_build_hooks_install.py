"""Grok Build hook installation for OpenShard auto-capture (unreleased).

Writes the hook configuration that delivers Grok Build's native hooks to
OpenShard into the repository's project-local ``.grok/hooks/openshard.json``.

Layout
------
Grok Build loads *every* ``*.json`` file in ``~/.grok/hooks/`` (personal,
always trusted) and ``<project>/.grok/hooks/`` (project, folder-trust
gated), in the same nested layout Claude Code uses::

    {"hooks": {"PostToolUse": [{"hooks": [
        {"type": "command", "command": "openshard hooks grok-build --event PostToolUse", "timeout": 5}
    ]}]}}

Because Grok merges the whole directory, OpenShard owns exactly one file,
``.grok/hooks/openshard.json``, and never touches any other file there. Inside
it the shared merge/remove helpers apply (idempotent -- an unchanged file is
left byte-for-byte alone -- and any entry that is not OpenShard's survives);
a file that cannot be parsed is never overwritten. No ``matcher`` is written:
an omitted matcher matches every tool.

Why command hooks
-----------------
Grok also offers an ``http`` handler, but its headers/authentication are not
documented and OpenShard's capture service requires a bearer token on every
request. A command hook (a fresh ``openshard hooks grok-build`` process) posts
the raw document to the service over authenticated loopback. ``--event`` is
passed on the command line even though Grok documents ``hookEventName``, so a
payload that omits it still routes; the command line wins.

Why never ``PreToolUse``
------------------------
It is the one Grok event whose stdout / exit code can deny a tool. OpenShard
observes; it installs nothing on that event and never exits 2.

Why project-local, and the trust step
-------------------------------------
Project scope matches every other integration's "this user, this repository"
choice. Grok will not run project hooks until the folder is trusted
(``/hooks-trust`` inside Grok, or launching with ``--trust``; the decision is
kept in ``~/.grok/trusted_folders.toml``). OpenShard cannot grant that trust,
does not parse the trust file (its format is undocumented), and says so in
``capture install`` / ``doctor`` output. A file OpenShard *creates* is added to
``.git/info/exclude``.
"""

from __future__ import annotations

from pathlib import Path

from openshard.adapters.claude_hooks_install import (
    TRANSPORT_COMMAND,
    ClaudeHooksInstallResult,
    HookSpec,
    _read_settings,
    _write_settings,
    ensure_local_settings_ignored,
    installed_events,
    merge_openshard_hooks,
    remove_openshard_hooks,
)
from openshard.adapters.grok_build_hooks import GROK_BUILD_HOOK_EVENTS

HOOK_COMMAND = "openshard hooks grok-build"
NO_SPAWN_FLAG = "--no-spawn"
HOOKS_RELPATH = Path(".grok") / "hooks" / "openshard.json"

HOOK_SPECS: tuple[HookSpec, ...] = (
    HookSpec("SessionStart", None, 15, TRANSPORT_COMMAND),  # starts the service when needed
    HookSpec("UserPromptSubmit", None, 5, TRANSPORT_COMMAND),
    HookSpec("PostToolUse", None, 5, TRANSPORT_COMMAND),
    HookSpec("PostToolUseFailure", None, 5, TRANSPORT_COMMAND),
    HookSpec("PermissionDenied", None, 5, TRANSPORT_COMMAND),
    HookSpec("Stop", None, 5, TRANSPORT_COMMAND),
    HookSpec("StopFailure", None, 5, TRANSPORT_COMMAND),
    HookSpec("StopCancelled", None, 5, TRANSPORT_COMMAND),
    HookSpec("SessionEnd", None, 5, TRANSPORT_COMMAND),
)
HOOK_EVENTS: tuple[str, ...] = tuple(s.event for s in HOOK_SPECS)
assert set(HOOK_EVENTS) == set(GROK_BUILD_HOOK_EVENTS)
# Events that never try to start a service (the session is over / failing).
_NO_SPAWN_EVENTS: frozenset[str] = frozenset({"SessionEnd", "StopFailure", "StopCancelled"})


def _hook_entry(spec: HookSpec, port: int = 0) -> dict:  # noqa: ARG001 - signature shared with the merge
    command = f"{HOOK_COMMAND} --event {spec.event}"
    if spec.event in _NO_SPAWN_EVENTS:
        command = f"{command} {NO_SPAWN_FLAG}"
    return {"type": "command", "command": command, "timeout": spec.timeout}


def is_openshard_grok_build_hook(hook: object) -> bool:
    """True for a hook entry that is OpenShard's Grok Build command hook."""
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    command = hook.get("command")
    if not isinstance(command, str):
        return False
    stripped = command.strip()
    return stripped == HOOK_COMMAND or stripped.startswith(HOOK_COMMAND + " ")


def build_hook_config() -> dict[str, list[dict]]:
    """The exact ``hooks`` block OpenShard installs (fresh-file shape)."""
    return {spec.event: [{"hooks": [_hook_entry(spec)]}] for spec in HOOK_SPECS}


def installed_grok_build_events(settings: object) -> list[str]:
    return installed_events(settings, events=HOOK_EVENTS, is_ours=is_openshard_grok_build_hook)


def load_grok_build_hooks(repo_root: Path) -> tuple[dict | None, str | None]:
    """Read-only ``(config, error)`` for ``<repo_root>/.grok/hooks/openshard.json``."""
    return _read_settings(Path(repo_root) / HOOKS_RELPATH)


def _error(message: str, path: Path | None = None) -> ClaudeHooksInstallResult:
    return ClaudeHooksInstallResult(status="error", settings_path=path, message=message)


def install_grok_build_hooks(*, repo_root: Path) -> ClaudeHooksInstallResult:
    """Write OpenShard's Grok Build hooks into ``<repo_root>/.grok/hooks/openshard.json``. Never raises."""
    try:
        root = Path(repo_root)
        path = root / HOOKS_RELPATH
        existed = path.exists()
        settings, err = _read_settings(path)
        if err or settings is None:
            return _error(err or "Could not read Grok Build hooks configuration.", path)
        try:
            merged, changes = merge_openshard_hooks(
                settings, specs=HOOK_SPECS, build_entry=_hook_entry, is_ours=is_openshard_grok_build_hook,
            )
        except ValueError as exc:
            return _error(f"{path} has an unexpected hooks layout ({exc}); OpenShard will not modify it.", path)
        warnings: list[str] = []
        if all(v == "unchanged" for v in changes.values()):
            status = "already_installed"
            message = "Grok Build auto-capture hooks already configured for this repository."
        else:
            _write_settings(path, merged)
            status = "installed" if all(v == "added" for v in changes.values()) else "updated"
            message = "Grok Build auto-capture hooks configured."
        if not existed:
            ignore_warning = ensure_local_settings_ignored(
                root, HOOKS_RELPATH.as_posix(), note="added by openshard capture install grok-build",
            )
            if ignore_warning:
                warnings.append(ignore_warning)
        return ClaudeHooksInstallResult(
            status=status, settings_path=path, events=changes, message=message, warnings=warnings,
        )
    except Exception as exc:
        return _error(f"Failed to configure Grok Build hooks: {type(exc).__name__}")


def uninstall_grok_build_hooks(*, repo_root: Path) -> ClaudeHooksInstallResult:
    """Remove OpenShard's Grok Build hooks from ``<repo_root>/.grok/hooks/openshard.json``. Never raises.

    Only entries identified by ``is_openshard_grok_build_hook`` are removed;
    unrelated hooks, matchers and keys survive. ``.openshard/`` is untouched.
    """
    try:
        root = Path(repo_root)
        path = root / HOOKS_RELPATH
        settings, err = _read_settings(path)
        if err or settings is None:
            return _error(err or "Could not read Grok Build hooks configuration.", path)
        merged, changes = remove_openshard_hooks(settings, specs=HOOK_SPECS, is_ours=is_openshard_grok_build_hook)
        if all(v == "absent" for v in changes.values()):
            return ClaudeHooksInstallResult(
                status="not_installed", settings_path=path, events=changes,
                message="No OpenShard Grok Build hooks were configured.",
            )
        hooks = merged.get("hooks")
        if isinstance(hooks, dict):
            for event in [e for e, groups in hooks.items() if groups == []]:
                del hooks[event]  # the shared remover leaves an empty list behind
            if not hooks:
                del merged["hooks"]
        if not merged:
            path.unlink()  # OpenShard's own file, and nothing else was ever in it
        else:
            _write_settings(path, merged)
        return ClaudeHooksInstallResult(
            status="removed", settings_path=path, events=changes, message="Grok Build auto-capture hooks removed.",
        )
    except Exception as exc:
        return _error(f"Failed to remove Grok Build hooks: {type(exc).__name__}")
