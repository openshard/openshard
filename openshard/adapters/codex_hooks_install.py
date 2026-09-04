"""Codex hook installation for OpenShard auto-capture (PR12).

Writes the hook configuration that delivers Codex's lifecycle hooks to
OpenShard into the repository's project-local ``.codex/hooks.json``. The
file shape (``{"hooks": {<Event>: [{"matcher"?, "hooks": [...]}]}}``) is the
same matcher-group layout Claude Code uses, so the merge/remove logic is
``claude_hooks_install.merge_openshard_hooks`` / ``remove_openshard_hooks``
with Codex's own hook specs and identification -- nothing is duplicated.

Why project-local
-----------------
Codex reads ``~/.codex/hooks.json`` (every repository on the machine) and
``<repo>/.codex/hooks.json`` (this repository, once the project is
trusted). Project scope matches the Claude Code integration's "this user,
this repository" choice: user scope would fire OpenShard in every
repository, including ones where the user never asked for capture. When
OpenShard *creates* ``.codex/hooks.json`` it also adds it to the
repository's local ``.git/info/exclude`` so it is not accidentally
committed; a pre-existing (possibly shared) file is merged into and its
git status is left to the user.

Why every hook is a command
---------------------------
Codex supports ``command`` and ``mcp_tool`` handlers only -- no HTTP hook.
Each event therefore runs ``openshard hooks codex``, a fresh process whose
only work is a loopback POST to the warm capture service (see
``claude_capture_client.run_hook_via_service``); the fold never runs in
that process while a service is reachable. ``PostToolUse`` (the most
frequent event) is ``async`` so Codex's tool loop never waits on it;
lifecycle events stay synchronous so their order is preserved.
``SessionEnd``/``Interrupt`` carry Codex's capped timeouts and use
``--no-spawn`` so they never try to start a service they could not wait for.

Trust
-----
Codex asks the user to review new or changed non-managed hooks once
(``/hooks`` in the CLI) before running them. The installer cannot and
does not bypass that; ``openshard setup`` reports it as a next step.

Design constraints (same as the Claude installer)
-------------------------------------------------
* Idempotent; an unchanged file is left byte-for-byte alone.
* Preserves everything else: other hooks, other events, ``description``.
* Never overwrites a file it cannot parse.
* Never raises from the public functions.
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

HOOK_COMMAND = "openshard hooks codex"
NO_SPAWN_FLAG = "--no-spawn"
HOOKS_RELPATH = Path(".codex") / "hooks.json"

# ``async`` is not part of HookSpec's transport vocabulary for Claude (HTTP
# hooks are always synchronous); for Codex, run_async marks the one
# high-frequency event that must never block the tool loop.
HOOK_SPECS: tuple[HookSpec, ...] = (
    HookSpec("SessionStart", None, 15, TRANSPORT_COMMAND),  # starts the service when needed
    HookSpec("UserPromptSubmit", None, 5, TRANSPORT_COMMAND),
    HookSpec("PostToolUse", None, 5, TRANSPORT_COMMAND, run_async=True),
    HookSpec("Stop", None, 5, TRANSPORT_COMMAND),
    HookSpec("SessionEnd", None, 3, TRANSPORT_COMMAND),  # Codex caps SessionEnd at a few seconds
    HookSpec("Interrupt", None, 3, TRANSPORT_COMMAND),
)
HOOK_EVENTS: tuple[str, ...] = tuple(s.event for s in HOOK_SPECS)
# Events whose Codex timeout budget is too small to start a service in.
_NO_SPAWN_EVENTS: frozenset[str] = frozenset({"SessionEnd", "Interrupt"})


def _hook_entry(spec: HookSpec, port: int = 0) -> dict:  # noqa: ARG001 - signature shared with the merge
    command = HOOK_COMMAND
    if spec.event in _NO_SPAWN_EVENTS:
        command = f"{HOOK_COMMAND} {NO_SPAWN_FLAG}"
    entry: dict = {"type": "command", "command": command, "timeout": spec.timeout}
    if spec.run_async:
        entry["async"] = True
    return entry


def is_openshard_codex_hook(hook: object) -> bool:
    """True for a hook entry that is OpenShard's Codex command hook."""
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


def installed_codex_events(settings: object) -> list[str]:
    return installed_events(settings, events=HOOK_EVENTS, is_ours=is_openshard_codex_hook)


def load_codex_hooks(repo_root: Path) -> tuple[dict | None, str | None]:
    """Read-only ``(config, error)`` for ``<repo_root>/.codex/hooks.json``."""
    return _read_settings(Path(repo_root) / HOOKS_RELPATH)


def _error(message: str, path: Path | None = None) -> ClaudeHooksInstallResult:
    return ClaudeHooksInstallResult(status="error", settings_path=path, message=message)


def install_codex_hooks(*, repo_root: Path) -> ClaudeHooksInstallResult:
    """Merge OpenShard's Codex hooks into ``<repo_root>/.codex/hooks.json``. Never raises."""
    try:
        root = Path(repo_root)
        path = root / HOOKS_RELPATH
        existed = path.exists()
        settings, err = _read_settings(path)
        if err or settings is None:
            return _error(err or "Could not read Codex hooks configuration.", path)
        try:
            merged, changes = merge_openshard_hooks(
                settings, specs=HOOK_SPECS, build_entry=_hook_entry, is_ours=is_openshard_codex_hook,
            )
        except ValueError as exc:
            return _error(f"{path} has an unexpected hooks layout ({exc}); OpenShard will not modify it.", path)
        warnings: list[str] = []
        if all(v == "unchanged" for v in changes.values()):
            status = "already_installed"
            message = "Codex auto-capture hooks already configured for this repository."
        else:
            _write_settings(path, merged)
            status = "installed" if all(v == "added" for v in changes.values()) else "updated"
            message = "Codex auto-capture hooks configured."
        if not existed:
            ignore_warning = ensure_local_settings_ignored(
                root, HOOKS_RELPATH.as_posix(), note="added by openshard capture install codex",
            )
            if ignore_warning:
                warnings.append(ignore_warning)
        return ClaudeHooksInstallResult(
            status=status, settings_path=path, events=changes, message=message, warnings=warnings,
        )
    except Exception as exc:
        return _error(f"Failed to configure Codex hooks: {type(exc).__name__}")


def uninstall_codex_hooks(*, repo_root: Path) -> ClaudeHooksInstallResult:
    """Remove OpenShard's Codex hooks from ``<repo_root>/.codex/hooks.json``. Never raises.

    Only entries identified by ``is_openshard_codex_hook`` are removed;
    unrelated hooks, matchers and keys survive. ``.openshard/`` is untouched.
    """
    try:
        root = Path(repo_root)
        path = root / HOOKS_RELPATH
        settings, err = _read_settings(path)
        if err or settings is None:
            return _error(err or "Could not read Codex hooks configuration.", path)
        merged, changes = remove_openshard_hooks(settings, specs=HOOK_SPECS, is_ours=is_openshard_codex_hook)
        if all(v == "absent" for v in changes.values()):
            return ClaudeHooksInstallResult(
                status="not_installed", settings_path=path, events=changes,
                message="No OpenShard Codex hooks were configured.",
            )
        _write_settings(path, merged)
        return ClaudeHooksInstallResult(
            status="removed", settings_path=path, events=changes, message="Codex auto-capture hooks removed.",
        )
    except Exception as exc:
        return _error(f"Failed to remove Codex hooks: {type(exc).__name__}")
