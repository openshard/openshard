"""Claude Code hook installation for OpenShard auto-capture (Demo v1 PR5).

Writes the hook configuration that makes Claude Code run
``openshard hooks claude`` at the lifecycle points the hook adapter
(``adapters/claude_hooks.py``) understands, into the repository's
``.claude/settings.local.json``.

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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

HOOK_COMMAND = "openshard hooks claude"
SETTINGS_RELPATH = Path(".claude") / "settings.local.json"
_GIT_TIMEOUT_SECONDS = 5.0

# Tools whose PostToolUse / PostToolUseFailure payloads the adapter records:
# file edits (tool_input.file_path) and shell commands (tool_input.command).
TOOL_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"


@dataclass(frozen=True)
class HookSpec:
    event: str
    matcher: str | None
    timeout: int
    # ``async: true`` -> Claude Code does not wait for the hook and ignores
    # its output, so the ~1s Python start-up never delays the user or the
    # model on the frequent, purely-staging hooks (per tool call, per
    # prompt, session start). Stop and SessionEnd stay synchronous: they are
    # the points that snapshot staged evidence into runs.jsonl, and a
    # background hook can be torn down with the Claude process before it
    # writes (observed in `claude -p` smoke testing). Roughly one second per
    # turn is the price of a guaranteed per-turn snapshot; SessionEnd's
    # timeout also raises Claude Code's default 1.5s SessionEnd budget.
    run_async: bool


HOOK_SPECS: tuple[HookSpec, ...] = (
    HookSpec("SessionStart", None, 15, True),
    HookSpec("UserPromptSubmit", None, 15, True),
    HookSpec("PostToolUse", TOOL_MATCHER, 15, True),
    HookSpec("PostToolUseFailure", TOOL_MATCHER, 15, True),
    HookSpec("Stop", None, 15, False),
    HookSpec("SessionEnd", None, 10, False),
)
SYNC_EVENTS: frozenset[str] = frozenset(s.event for s in HOOK_SPECS if not s.run_async)
HOOK_EVENTS: tuple[str, ...] = tuple(s.event for s in HOOK_SPECS)


def _hook_entry(spec: HookSpec) -> dict:
    entry: dict = {"type": "command", "command": HOOK_COMMAND, "timeout": spec.timeout}
    if spec.run_async:
        entry["async"] = True
    return entry


def _group_entry(spec: HookSpec) -> dict:
    group: dict = {}
    if spec.matcher:
        group["matcher"] = spec.matcher
    group["hooks"] = [_hook_entry(spec)]
    return group


def build_hook_config() -> dict[str, list[dict]]:
    """The exact ``hooks`` block OpenShard installs (fresh-file shape)."""
    return {spec.event: [_group_entry(spec)] for spec in HOOK_SPECS}


def is_openshard_hook(hook: object) -> bool:
    """True for a command hook entry that runs OpenShard's Claude hook command."""
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    command = hook.get("command")
    if not isinstance(command, str):
        return False
    stripped = command.strip()
    return stripped == HOOK_COMMAND or stripped.startswith(HOOK_COMMAND + " ")


def merge_openshard_hooks(settings: dict) -> tuple[dict, dict[str, str]]:
    """Return ``(new_settings, changes)`` with OpenShard's hooks merged in.

    ``changes`` maps each event to ``"added"`` / ``"updated"`` /
    ``"unchanged"``. The input is never mutated. Raises ``ValueError`` when
    the existing ``hooks`` structure is not the documented shape (so the
    caller can refuse to write rather than clobber the file).
    """
    new_settings = copy.deepcopy(settings)
    hooks = new_settings.get("hooks")
    if hooks is None:
        hooks = {}
        new_settings["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise ValueError("'hooks' is not a JSON object")

    changes: dict[str, str] = {}
    for spec in HOOK_SPECS:
        groups = hooks.get(spec.event)
        if groups is None:
            groups = []
            hooks[spec.event] = groups
        if not isinstance(groups, list):
            raise ValueError(f"'hooks.{spec.event}' is not a JSON array")

        desired_hook = _hook_entry(spec)
        kept = False
        changed = False
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            group_hooks: list = group["hooks"]
            others = [h for h in group_hooks if not is_openshard_hook(h)]
            ours = [h for h in group_hooks if is_openshard_hook(h)]
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
            groups.append(_group_entry(spec))
            # "updated" when we had to move/dedupe an existing OpenShard entry.
            changes[spec.event] = "updated" if changed else "added"
        else:
            changes[spec.event] = "updated" if changed else "unchanged"
    return new_settings, changes


def installed_events(settings: object) -> list[str]:
    """Events (of HOOK_EVENTS) that already carry an OpenShard hook in *settings*."""
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks"), dict):
        return []
    found: list[str] = []
    for event in HOOK_EVENTS:
        groups = settings["hooks"].get(event)
        if not isinstance(groups, list):
            continue
        for group in groups:
            if isinstance(group, dict) and any(is_openshard_hook(h) for h in group.get("hooks") or []):
                found.append(event)
                break
    return found


@dataclass
class ClaudeHooksInstallResult:
    status: str  # "installed" | "updated" | "already_installed" | "error"
    settings_path: Path | None
    events: dict[str, str] = field(default_factory=dict)
    message: str = ""
    warnings: list[str] = field(default_factory=list)


def _error(message: str, settings_path: Path | None = None) -> ClaudeHooksInstallResult:
    return ClaudeHooksInstallResult(status="error", settings_path=settings_path, message=message)


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


def ensure_local_settings_ignored(repo_root: Path) -> str | None:
    """Make sure ``.claude/settings.local.json`` is git-ignored in *repo_root*.

    Claude Code only adds the ignore rule when *it* creates the file, so
    OpenShard adds ``.claude/settings.local.json`` to the repository's
    local ``.git/info/exclude`` (documented git, never committed) when git
    does not already ignore it. Returns a warning string when that could
    not be confirmed; never raises.
    """
    rel = SETTINGS_RELPATH.as_posix()
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
            fh.write(f"# added by openshard mcp install claude\n{rel}\n")
        return None
    except Exception:
        return f"Could not update .git/info/exclude; add {rel} to your gitignore."


def install_claude_hooks(*, repo_root: Path) -> ClaudeHooksInstallResult:
    """Merge OpenShard's Claude Code hooks into ``<repo_root>/.claude/settings.local.json``.

    Idempotent and additive (see module docstring). Never raises.
    """
    try:
        root = Path(repo_root)
        settings_path = root / SETTINGS_RELPATH
        settings, err = _read_settings(settings_path)
        if err or settings is None:
            return _error(err or "Could not read Claude Code settings.", settings_path)
        try:
            merged, changes = merge_openshard_hooks(settings)
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
