"""Claude Code hook installation for OpenShard auto-capture (Demo v1 PR5, PR9.5).

Writes the hook configuration that delivers Claude Code's lifecycle hooks
to OpenShard into the repository's ``.claude/settings.local.json``. Since
PR9.5 every hook that can be an HTTP hook is one (``UserPromptSubmit``,
``PostToolUse``, ``PostToolUseFailure``, ``Stop``, ``SessionEnd`` POST
their payload to the local capture service,
``adapters/claude_capture_service.py``); ``SessionStart`` remains the
``openshard hooks claude`` command, because Claude Code does not deliver
HTTP hooks for it and because that command is what starts the service.

Why this file and scope
-----------------------
Claude Code's documented hook mechanism is the ``hooks`` block of its
settings files; there is no ``claude hooks add`` CLI (the ``/hooks`` menu
is read-only). Of the documented locations, ``.claude/settings.local.json``
is the project-local, not-shared one -- the same "this user, this
repository" scope PR4 chose for the MCP server with ``claude mcp add
--scope local``. Project scope (``.claude/settings.json``) would commit the
hooks for every teammate; user scope would fire them in every repository
on the machine. The command written contains no machine-specific path: the
adapter locates the repository from ``CLAUDE_PROJECT_DIR``/``cwd`` at run
time, so even an accidental commit of the file would be harmless.

Design constraints
------------------
* Idempotent: re-running never duplicates an OpenShard hook; an unchanged
  configuration is left byte-for-byte alone; a stale one (different
  matcher/timeout/async) is updated in place.
* Preserves everything else in the file: unrelated hooks (other commands,
  other events), permissions, any other key. Only OpenShard's own hook
  entries -- identified by their exact command -- are ever touched.
* Never overwrites a file it cannot parse: invalid JSON is reported as an
  error and left untouched.
* Never raises from the public functions; the CLI decides how to surface
  a failure.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from openshard.adapters.claude_capture_client import (
    DEFAULT_PORT,
    HOOK_PATH,
    PROJECT_DIR_HEADER,
)

HOOK_COMMAND = "openshard hooks claude"
STATUS_COMMAND = "openshard hooks claude-status"
SETTINGS_RELPATH = Path(".claude") / "settings.local.json"
_GIT_TIMEOUT_SECONDS = 5.0

# Tools whose PostToolUse / PostToolUseFailure payloads the adapter records:
# file edits (tool_input.file_path) and shell commands (tool_input.command).
TOOL_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"

TRANSPORT_COMMAND = "command"
TRANSPORT_HTTP = "http"
_HOOK_URL_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost):(\d{1,5})" + re.escape(HOOK_PATH) + r"/?$")


@dataclass(frozen=True)
class HookSpec:
    event: str
    matcher: str | None
    timeout: int
    # PR9.5: every hook that can be an HTTP hook is one -- Claude Code POSTs
    # the payload straight to the warm local capture service (see
    # adapters/claude_capture_service.py), so no Python process is spawned
    # and the blocking cost is a loopback round trip. Only ``SessionStart``
    # stays a command hook: Claude Code does not deliver HTTP hooks for that
    # event, and it is precisely the hook that starts the service when it is
    # not running. All hooks are synchronous now -- a warm service answers in
    # milliseconds, and synchronous delivery keeps events strictly ordered
    # (an async Stop could otherwise overtake the tool hooks before it).
    transport: str
    run_async: bool = False


HOOK_SPECS: tuple[HookSpec, ...] = (
    HookSpec("SessionStart", None, 15, TRANSPORT_COMMAND),
    HookSpec("UserPromptSubmit", None, 5, TRANSPORT_HTTP),
    HookSpec("PostToolUse", TOOL_MATCHER, 5, TRANSPORT_HTTP),
    HookSpec("PostToolUseFailure", TOOL_MATCHER, 5, TRANSPORT_HTTP),
    HookSpec("Stop", None, 5, TRANSPORT_HTTP),
    HookSpec("SessionEnd", None, 5, TRANSPORT_HTTP),
)
SYNC_EVENTS: frozenset[str] = frozenset(s.event for s in HOOK_SPECS if not s.run_async)
HTTP_EVENTS: tuple[str, ...] = tuple(s.event for s in HOOK_SPECS if s.transport == TRANSPORT_HTTP)
HOOK_EVENTS: tuple[str, ...] = tuple(s.event for s in HOOK_SPECS)


def hook_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}{HOOK_PATH}"


def _hook_entry(spec: HookSpec, port: int = DEFAULT_PORT) -> dict:
    if spec.transport == TRANSPORT_HTTP:
        entry: dict = {
            "type": "http",
            "url": hook_url(port),
            "timeout": spec.timeout,
            # Lets the service anchor the event to the project Claude Code
            # was started in, exactly like CLAUDE_PROJECT_DIR does for the
            # command form; when Claude Code does not interpolate it the
            # header is empty and the payload's ``cwd`` is used instead.
            "headers": {PROJECT_DIR_HEADER: "$CLAUDE_PROJECT_DIR"},
            "allowedEnvVars": ["CLAUDE_PROJECT_DIR"],
        }
        return entry
    entry = {"type": "command", "command": HOOK_COMMAND, "timeout": spec.timeout}
    if spec.run_async:
        entry["async"] = True
    return entry


def _group_entry(spec: HookSpec, port: int = DEFAULT_PORT, build_entry: Callable[[HookSpec, int], dict] | None = None) -> dict:
    group: dict = {}
    if spec.matcher:
        group["matcher"] = spec.matcher
    group["hooks"] = [(build_entry or _hook_entry)(spec, port)]
    return group


def build_hook_config(port: int = DEFAULT_PORT) -> dict[str, list[dict]]:
    """The exact ``hooks`` block OpenShard installs (fresh-file shape) for service *port*."""
    return {spec.event: [_group_entry(spec, port)] for spec in HOOK_SPECS}


def _hook_entry_port(hook: object) -> int | None:
    """The service port an OpenShard HTTP hook entry points at, else None."""
    if not isinstance(hook, dict) or hook.get("type") != "http":
        return None
    url = hook.get("url")
    if not isinstance(url, str):
        return None
    match = _HOOK_URL_RE.match(url.strip())
    if match is None:
        return None
    port = int(match.group(1))
    return port if 0 < port < 65536 else None


def is_openshard_hook(hook: object) -> bool:
    """True for a hook entry that is OpenShard's: the command form or the HTTP form."""
    if not isinstance(hook, dict):
        return False
    if hook.get("type") == "http":
        return _hook_entry_port(hook) is not None
    if hook.get("type") != "command":
        return False
    command = hook.get("command")
    if not isinstance(command, str):
        return False
    stripped = command.strip()
    return stripped == HOOK_COMMAND or stripped.startswith(HOOK_COMMAND + " ")


def installed_hook_port(settings: object) -> int | None:
    """Port the installed OpenShard HTTP hooks point at (first found), or None."""
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks"), dict):
        return None
    for event in HOOK_EVENTS:
        groups = settings["hooks"].get(event)
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks") or []:
                port = _hook_entry_port(hook)
                if port is not None:
                    return port
    return None


def merge_openshard_hooks(
    settings: dict,
    *,
    port: int = DEFAULT_PORT,
    specs: tuple[HookSpec, ...] = HOOK_SPECS,
    build_entry: Callable[[HookSpec, int], dict] | None = None,
    is_ours: Callable[[object], bool] | None = None,
) -> tuple[dict, dict[str, str]]:
    """Return ``(new_settings, changes)`` with OpenShard's hooks merged in.

    ``changes`` maps each event to ``"added"`` / ``"updated"`` /
    ``"unchanged"``. The input is never mutated. Raises ``ValueError`` when
    the existing ``hooks`` structure is not the documented shape (so the
    caller can refuse to write rather than clobber the file). An older
    command-form entry (pre-PR9.5) or an HTTP entry for a different port is
    reported as ``"updated"`` and replaced in place.

    The ``hooks`` block shape (event -> matcher groups -> hook entries) is
    shared by Claude Code and Codex, so the Codex installer
    (``codex_hooks_install``) reuses this merge with its own *specs*,
    *build_entry* and *is_ours* (PR12); the defaults are Claude Code's.
    """
    build = build_entry or _hook_entry
    ours_fn = is_ours or is_openshard_hook
    new_settings = copy.deepcopy(settings)
    hooks = new_settings.get("hooks")
    if hooks is None:
        hooks = {}
        new_settings["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise ValueError("'hooks' is not a JSON object")

    changes: dict[str, str] = {}
    for spec in specs:
        groups = hooks.get(spec.event)
        if groups is None:
            groups = []
            hooks[spec.event] = groups
        if not isinstance(groups, list):
            raise ValueError(f"'hooks.{spec.event}' is not a JSON array")

        desired_hook = build(spec, port)
        kept = False
        changed = False
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            group_hooks: list = group["hooks"]
            others = [h for h in group_hooks if not ours_fn(h)]
            ours = [h for h in group_hooks if ours_fn(h)]
            if not ours:
                continue
            if kept:
                # A duplicate OpenShard entry in another group: drop it.
                group["hooks"] = others
                changed = True
                continue
            matcher_ok = (group.get("matcher") or None) == spec.matcher
            if others and not matcher_ok:
                # Shared group with a different matcher: leave the user's
                # hooks (and their matcher) alone; re-home ours below.
                group["hooks"] = others
                changed = True
                continue
            kept = True
            if len(ours) > 1 or ours[0] != desired_hook or not matcher_ok:
                changed = True
                group["hooks"] = others + [desired_hook]
                if spec.matcher:
                    group["matcher"] = spec.matcher
                else:
                    group.pop("matcher", None)
        # Remove groups we emptied.
        groups[:] = [
            g for g in groups
            if not (isinstance(g, dict) and isinstance(g.get("hooks"), list) and not g["hooks"]
                    and not any(True for k in g if k not in ("hooks", "matcher")))
        ]
        if not kept:
            groups.append(_group_entry(spec, port, build))
            # "updated" when we had to move/dedupe an existing OpenShard entry.
            changes[spec.event] = "updated" if changed else "added"
        else:
            changes[spec.event] = "updated" if changed else "unchanged"
    return new_settings, changes


def remove_openshard_hooks(
    settings: dict,
    *,
    specs: tuple[HookSpec, ...] = HOOK_SPECS,
    is_ours: Callable[[object], bool] | None = None,
) -> tuple[dict, dict[str, str]]:
    """Return ``(new_settings, changes)`` with OpenShard's hooks removed.

    ``changes`` maps each event to ``"removed"`` / ``"absent"``. Mirrors
    ``merge_openshard_hooks``'s identification (``is_openshard_hook``) so
    only OpenShard's own entries are ever removed -- unrelated hooks, their
    matchers, and every other settings key are left exactly as they were.
    The input is never mutated. *specs*/*is_ours* as for the merge.
    """
    ours_fn = is_ours or is_openshard_hook
    new_settings = copy.deepcopy(settings)
    hooks = new_settings.get("hooks")
    if not isinstance(hooks, dict):
        return new_settings, {spec.event: "absent" for spec in specs}

    changes: dict[str, str] = {}
    for spec in specs:
        groups = hooks.get(spec.event)
        if not isinstance(groups, list):
            changes[spec.event] = "absent"
            continue
        removed_any = False
        new_groups: list = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                new_groups.append(group)
                continue
            others = [h for h in group["hooks"] if not ours_fn(h)]
            if len(others) != len(group["hooks"]):
                removed_any = True
            if others:
                new_group = dict(group)
                new_group["hooks"] = others
                new_groups.append(new_group)
            # else: the group had only our hook(s) -- drop it entirely.
        hooks[spec.event] = new_groups
        changes[spec.event] = "removed" if removed_any else "absent"
    return new_settings, changes


def installed_events(
    settings: object,
    *,
    events: tuple[str, ...] = HOOK_EVENTS,
    is_ours: Callable[[object], bool] | None = None,
) -> list[str]:
    """Events (of *events*) that already carry an OpenShard hook in *settings*."""
    ours_fn = is_ours or is_openshard_hook
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks"), dict):
        return []
    found: list[str] = []
    for event in events:
        groups = settings["hooks"].get(event)
        if not isinstance(groups, list):
            continue
        for group in groups:
            if isinstance(group, dict) and any(ours_fn(h) for h in group.get("hooks") or []):
                found.append(event)
                break
    return found


@dataclass
class ClaudeHooksInstallResult:
    status: str  # "installed" | "updated" | "already_installed" | "skipped_existing" | "removed" | "not_installed" | "error"
    settings_path: Path | None
    events: dict[str, str] = field(default_factory=dict)
    message: str = ""
    warnings: list[str] = field(default_factory=list)


def _error(message: str, settings_path: Path | None = None) -> ClaudeHooksInstallResult:
    return ClaudeHooksInstallResult(status="error", settings_path=settings_path, message=message)


def load_settings(repo_root: Path) -> tuple[dict | None, str | None]:
    """Read-only ``(settings, error)`` for ``<repo_root>/.claude/settings.local.json``.

    Public wrapper around the same parsing ``install_claude_hooks`` uses, for
    callers (``openshard doctor``, ``openshard setup --agent``) that only
    ever need to inspect the file, never write it.
    """
    return _read_settings(Path(repo_root) / SETTINGS_RELPATH)


def _read_settings(path: Path) -> tuple[dict | None, str | None]:
    """Return ``(settings, error)``; a missing or empty file is ``{}``."""
    if not path.exists():
        return {}, None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"Could not read {path.name}: {type(exc).__name__}"
    if not text.strip():
        return {}, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, (
            f"{path} is not valid JSON; OpenShard will not modify it. "
            "Fix the file (or remove it) and re-run."
        )
    if not isinstance(data, dict):
        return None, f"{path} does not contain a JSON object; OpenShard will not modify it."
    return data, None


def _write_settings(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(settings, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def ensure_local_settings_ignored(
    repo_root: Path, rel: str | None = None, *, note: str = "added by openshard mcp install claude"
) -> str | None:
    """Make sure ``.claude/settings.local.json`` (or *rel*) is git-ignored in *repo_root*.

    Claude Code only adds the ignore rule when *it* creates the file, so
    OpenShard adds ``.claude/settings.local.json`` to the repository's
    local ``.git/info/exclude`` (documented git, never committed) when git
    does not already ignore it. Returns a warning string when that could
    not be confirmed; never raises. The Codex/OpenCode installers pass
    their own *rel* for the files they created (PR12).
    """
    rel = rel or SETTINGS_RELPATH.as_posix()
    try:
        check = subprocess.run(
            ["git", "check-ignore", "-q", rel],
            cwd=str(repo_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=_GIT_TIMEOUT_SECONDS,
        )
    except Exception:
        return f"Could not run git to confirm {rel} is ignored; make sure it is not committed."
    if check.returncode == 0:
        return None
    if check.returncode != 1:
        return f"Could not confirm {rel} is git-ignored; make sure it is not committed."
    try:
        where = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=str(repo_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=_GIT_TIMEOUT_SECONDS,
        )
        if where.returncode != 0 or not where.stdout.strip():
            return f"Could not locate .git/info/exclude; add {rel} to your gitignore."
        exclude_path = Path(where.stdout.strip())
        if not exclude_path.is_absolute():
            exclude_path = repo_root / exclude_path
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        if rel in {ln.strip() for ln in existing.splitlines()}:
            return None
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        with exclude_path.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(f"# {note}\n{rel}\n")
        return None
    except Exception:
        return f"Could not update .git/info/exclude; add {rel} to your gitignore."


def install_claude_hooks(*, repo_root: Path, port: int | None = None) -> ClaudeHooksInstallResult:
    """Merge OpenShard's Claude Code hooks into ``<repo_root>/.claude/settings.local.json``.

    Idempotent and additive (see module docstring). Never raises. *port*
    is the capture service port the HTTP hooks should target (default: the
    port a running service publishes, else ``DEFAULT_PORT``).
    """
    try:
        root = Path(repo_root)
        settings_path = root / SETTINGS_RELPATH
        settings, err = _read_settings(settings_path)
        if err or settings is None:
            return _error(err or "Could not read Claude Code settings.", settings_path)
        if port is None:
            from openshard.adapters.claude_capture_client import resolve_port

            port = resolve_port()
        try:
            merged, changes = merge_openshard_hooks(settings, port=port)
        except ValueError as exc:
            return _error(
                f"{settings_path} has an unexpected hooks layout ({exc}); OpenShard will not modify it.",
                settings_path,
            )
        warnings: list[str] = []
        if all(v == "unchanged" for v in changes.values()):
            status = "already_installed"
            message = "Auto-capture hooks already configured for this repository."
        else:
            _write_settings(settings_path, merged)
            status = "installed" if all(v == "added" for v in changes.values()) else "updated"
            message = "Auto-capture hooks configured."
        ignore_warning = ensure_local_settings_ignored(root)
        if ignore_warning:
            warnings.append(ignore_warning)
        return ClaudeHooksInstallResult(
            status=status, settings_path=settings_path, events=changes, message=message, warnings=warnings,
        )
    except Exception as exc:
        return _error(f"Failed to configure Claude Code hooks: {type(exc).__name__}")


def _desired_statusline_entry() -> dict:
    return {"type": "command", "command": STATUS_COMMAND}


def is_openshard_statusline(value: object) -> bool:
    """True for a ``statusLine`` config object that runs OpenShard's status command."""
    if not isinstance(value, dict) or value.get("type") != "command":
        return False
    command = value.get("command")
    if not isinstance(command, str):
        return False
    stripped = command.strip()
    return stripped == STATUS_COMMAND or stripped.startswith(STATUS_COMMAND + " ")


def install_claude_statusline(*, repo_root: Path) -> ClaudeHooksInstallResult:
    """Configure ``<repo_root>/.claude/settings.local.json``'s ``statusLine`` for capture.

    Claude Code's status line is the only documented, local, no-network
    surface that carries model id, cumulative session cost, and token
    counts (see ``adapters/claude_hooks.py`` module docstring) -- hooks never
    do. Only one ``statusLine`` command can be configured at a time, so this
    is deliberately conservative: it is installed only when the project has
    no ``statusLine`` of its own yet. An existing, different command is left
    completely untouched (status is ``"skipped_existing"``) rather than
    wrapped or replaced -- OpenShard never risks breaking a user's status
    line to capture metadata. Never raises.
    """
    try:
        root = Path(repo_root)
        settings_path = root / SETTINGS_RELPATH
        settings, err = _read_settings(settings_path)
        if err or settings is None:
            return _error(err or "Could not read Claude Code settings.", settings_path)

        existing = settings.get("statusLine")
        if is_openshard_statusline(existing):
            return ClaudeHooksInstallResult(
                status="already_installed", settings_path=settings_path,
                message="Status line already configured for OpenShard capture.",
            )
        if existing is not None:
            return ClaudeHooksInstallResult(
                status="skipped_existing", settings_path=settings_path,
                message=(
                    "A custom status line is already configured for this repository; OpenShard will not "
                    "replace it. Model/cost/token capture will stay Unknown/Not recorded until this is "
                    "resolved (remove the existing statusLine entry and re-run install to enable it)."
                ),
            )

        merged = copy.deepcopy(settings)
        merged["statusLine"] = _desired_statusline_entry()
        _write_settings(settings_path, merged)
        return ClaudeHooksInstallResult(
            status="installed", settings_path=settings_path,
            message="Status line configured for model/cost/token capture.",
        )
    except Exception as exc:
        return _error(f"Failed to configure Claude Code status line: {type(exc).__name__}")


def uninstall_claude_hooks(*, repo_root: Path) -> ClaudeHooksInstallResult:
    """Remove OpenShard's Claude Code hooks from ``<repo_root>/.claude/settings.local.json``.

    Reverses ``install_claude_hooks``: only entries identified by
    ``is_openshard_hook`` are removed. Unrelated hooks, matchers, and every
    other settings key survive untouched. Never raises, and never touches
    ``.openshard/`` history.
    """
    try:
        root = Path(repo_root)
        settings_path = root / SETTINGS_RELPATH
        settings, err = _read_settings(settings_path)
        if err or settings is None:
            return _error(err or "Could not read Claude Code settings.", settings_path)
        merged, changes = remove_openshard_hooks(settings)
        if all(v == "absent" for v in changes.values()):
            return ClaudeHooksInstallResult(
                status="not_installed", settings_path=settings_path, events=changes,
                message="No OpenShard auto-capture hooks were configured.",
            )
        _write_settings(settings_path, merged)
        return ClaudeHooksInstallResult(
            status="removed", settings_path=settings_path, events=changes,
            message="Auto-capture hooks removed.",
        )
    except Exception as exc:
        return _error(f"Failed to remove Claude Code hooks: {type(exc).__name__}")


def uninstall_claude_statusline(*, repo_root: Path) -> ClaudeHooksInstallResult:
    """Remove OpenShard's ``statusLine`` entry, but only if it is OpenShard's own.

    A custom status line (anything other than exactly OpenShard's command)
    is left completely untouched -- there is nothing of OpenShard's to
    remove, and OpenShard never removes configuration it did not add. Never
    raises.
    """
    try:
        root = Path(repo_root)
        settings_path = root / SETTINGS_RELPATH
        settings, err = _read_settings(settings_path)
        if err or settings is None:
            return _error(err or "Could not read Claude Code settings.", settings_path)
        existing = settings.get("statusLine")
        if not is_openshard_statusline(existing):
            return ClaudeHooksInstallResult(
                status="not_installed", settings_path=settings_path,
                message="No OpenShard status line was configured; nothing removed.",
            )
        merged = copy.deepcopy(settings)
        merged.pop("statusLine", None)
        _write_settings(settings_path, merged)
        return ClaudeHooksInstallResult(
            status="removed", settings_path=settings_path,
            message="Status line configuration removed.",
        )
    except Exception as exc:
        return _error(f"Failed to remove Claude Code status line: {type(exc).__name__}")
