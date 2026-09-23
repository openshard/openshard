"""Hermes Agent hook installation for OpenShard auto-capture (0.4.7).

Hermes Agent reads its shell hooks from **one user-global file**,
``<hermes home>/config.yaml`` (there is no project-level hook file), and runs
a shell hook only once the ``(event, command)`` pair is in its allowlist,
``<hermes home>/shell-hooks-allowlist.json``. So unlike the repo-local
integrations this installer edits two files under the user's Hermes home:

* ``config.yaml`` -- adds OpenShard's entries under the top-level ``hooks:``
  mapping::

      hooks:
        post_tool_call:
          - command: "openshard hooks hermes"
            timeout: 15

  One entry per subscribed event (``HOOK_EVENTS``), no ``matcher`` (every
  tool is observed) and no ``fail_closed`` (a hook failure never blocks Hermes).
  Anything else -- other events, other commands under the same event,
  ``hooks.outbound``, ``hooks_auto_accept`` -- is never touched. When the file
  has no ``hooks:`` key the block is *appended* between marker comments, so
  every existing comment and byte survives; when ``hooks:`` already exists the
  mapping is merged and the file re-serialised (comments are not preserved),
  after a one-time backup ``config.yaml.openshard-backup``.
* ``shell-hooks-allowlist.json`` -- records Hermes' documented first-use
  consent for each ``(event, command)`` pair, exactly the entry Hermes itself
  writes when a person approves the prompt (``event``, ``command``,
  ``approved_at``, ``script_mtime_at_approval``). The person ran
  ``openshard capture install hermes`` for this; without it a non-interactive
  Hermes (gateway, cron) would silently skip the hooks.

The Hermes home is ``$HERMES_HOME`` when set, else ``%LOCALAPPDATA%\\hermes``
on Windows and ``~/.hermes`` elsewhere (Hermes' own rule); a Hermes profile
lives in its own home, so install once per profile with ``HERMES_HOME`` set.

Install is idempotent (an unchanged file is left byte-for-byte alone), never
writes a file it cannot parse, refuses a ``hooks`` value of an unexpected
shape, verifies its own output before keeping it, and never raises from the
public functions. Uninstall removes only OpenShard's own entries.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from openshard.adapters.claude_hooks_install import ClaudeHooksInstallResult
from openshard.adapters.hermes_hooks import HERMES_HOOK_EVENTS

HOOK_COMMAND = "openshard hooks hermes"
HOOK_EVENTS: tuple[str, ...] = HERMES_HOOK_EVENTS
# Generous next to a loopback POST, and Hermes caps a shell hook at 300 s.
HOOK_TIMEOUT_SECONDS = 15
CONFIG_FILENAME = "config.yaml"
ALLOWLIST_FILENAME = "shell-hooks-allowlist.json"
BACKUP_SUFFIX = ".openshard-backup"
BLOCK_BEGIN = "# >>> openshard hermes capture (managed by `openshard capture install hermes`) >>>"
BLOCK_END = "# <<< openshard hermes capture <<<"


def hermes_home(env: dict | os._Environ | None = None) -> Path:
    """The Hermes home directory, by Hermes' own resolution rule."""
    env = os.environ if env is None else env
    override = str(env.get("HERMES_HOME") or "").strip()
    if override:
        return Path(os.path.expanduser(os.path.expandvars(override)))
    if sys.platform == "win32":
        local = str(env.get("LOCALAPPDATA") or "").strip()
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"
    return Path.home() / ".hermes"


def config_path(home: Path | None = None) -> Path:
    return (home if home is not None else hermes_home()) / CONFIG_FILENAME


def allowlist_path(home: Path | None = None) -> Path:
    return (home if home is not None else hermes_home()) / ALLOWLIST_FILENAME


def is_openshard_hermes_hook(entry: object) -> bool:
    """True for a hook entry that is OpenShard's Hermes command."""
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    if not isinstance(command, str):
        return False
    stripped = command.strip()
    return stripped == HOOK_COMMAND or stripped.startswith(HOOK_COMMAND + " ")


def _entry() -> dict[str, Any]:
    return {"command": HOOK_COMMAND, "timeout": HOOK_TIMEOUT_SECONDS}


def _is_desired(entry: object) -> bool:
    return isinstance(entry, dict) and entry == _entry()


def build_hooks_block() -> dict[str, list[dict[str, Any]]]:
    """The exact value OpenShard installs under ``hooks:``."""
    return {event: [_entry()] for event in HOOK_EVENTS}


def render_managed_block() -> str:
    lines = [BLOCK_BEGIN, "hooks:"]
    for event in HOOK_EVENTS:
        lines += [
            f"  {event}:",
            f'    - command: "{HOOK_COMMAND}"',
            f"      timeout: {HOOK_TIMEOUT_SECONDS}",
        ]
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# config.yaml
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> tuple[str | None, str | None]:
    """``(text, error)``; an absent file is ``("", None)``."""
    try:
        if not path.exists():
            return "", None
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{path}: cannot read ({type(exc).__name__})"


def _parse_config(text: str, path: Path) -> tuple[dict | None, str | None]:
    if not text.strip():
        return {}, None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None, f"{path}: not valid YAML; OpenShard will not modify it."
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, f"{path}: top level is not a mapping; OpenShard will not modify it."
    return data, None


def load_hermes_config(home: Path | None = None) -> tuple[dict | None, str | None]:
    """Read-only ``(config, error)`` for ``<hermes home>/config.yaml`` (absent -> ``{}``)."""
    path = config_path(home)
    text, err = _read_text(path)
    if err or text is None:
        return None, err
    return _parse_config(text, path)


def merge_hermes_hooks(config: dict) -> tuple[dict, dict[str, str]]:
    """Return ``(new_config, changes)`` with OpenShard's hooks set.

    ``changes`` maps each event to ``"added"`` / ``"updated"`` / ``"unchanged"``.
    The input is never mutated. Raises ``ValueError`` when ``hooks`` (or one
    of its event lists) has a shape OpenShard cannot merge into safely.
    """
    new_config = copy.deepcopy(config)
    hooks = new_config.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise ValueError("'hooks' is not a mapping")
    changes: dict[str, str] = {}
    for event in HOOK_EVENTS:
        current = hooks.get(event)
        if current is None:
            entries: list = []
        elif isinstance(current, list):
            entries = current
        else:
            raise ValueError(f"'hooks.{event}' is not a list")
        ours = [e for e in entries if is_openshard_hermes_hook(e)]
        foreign = [e for e in entries if not is_openshard_hermes_hook(e)]
        if len(ours) == 1 and _is_desired(ours[0]):
            changes[event] = "unchanged"
            continue
        changes[event] = "updated" if ours else "added"
        hooks[event] = [*foreign, _entry()]
    new_config["hooks"] = hooks
    return new_config, changes


def remove_hermes_hooks(config: dict) -> tuple[dict, dict[str, str]]:
    """Return ``(new_config, changes)`` without OpenShard's hooks.

    Only OpenShard's own entries are removed; an event left with no entries is
    dropped, and ``hooks`` itself only if it ends up empty. The input is never
    mutated. Events that hold none of ours read ``"absent"``.
    """
    new_config = copy.deepcopy(config)
    hooks = new_config.get("hooks")
    changes = {event: "absent" for event in HOOK_EVENTS}
    if not isinstance(hooks, dict):
        return new_config, changes
    for event in list(hooks):
        entries = hooks.get(event)
        if not isinstance(entries, list) or not any(is_openshard_hermes_hook(e) for e in entries):
            continue
        if event in changes:
            changes[event] = "removed"
        kept = [e for e in entries if not is_openshard_hermes_hook(e)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks and any(v == "removed" for v in changes.values()):
        del new_config["hooks"]
    return new_config, changes


def installed_hermes_events(config: object) -> list[str]:
    """Events (of ``HOOK_EVENTS``) that already carry OpenShard's hook in *config*."""
    if not isinstance(config, dict) or not isinstance(config.get("hooks"), dict):
        return []
    hooks = config["hooks"]
    return [
        event for event in HOOK_EVENTS
        if isinstance(hooks.get(event), list) and any(is_openshard_hermes_hook(e) for e in hooks[event])
    ]


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".openshard-tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def _dump_config(config: dict) -> str:
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _strip_managed_block(text: str) -> str | None:
    """*text* without OpenShard's marker-delimited block, or None when there is none."""
    lines = text.splitlines(keepends=True)
    begin = next((i for i, ln in enumerate(lines) if ln.strip() == BLOCK_BEGIN), None)
    if begin is None:
        return None
    end = next((i for i in range(begin + 1, len(lines)) if lines[i].strip() == BLOCK_END), None)
    if end is None:
        return None
    return "".join(lines[:begin] + lines[end + 1:])


def _backup_once(path: Path, text: str) -> None:
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if text and not backup.exists():
        _write_text(backup, text)


# ---------------------------------------------------------------------------
# shell-hooks-allowlist.json (Hermes' documented consent record)
# ---------------------------------------------------------------------------


def load_allowlist(home: Path | None = None) -> tuple[dict | None, str | None]:
    """Read-only ``(allowlist, error)``; an absent file is ``{"approvals": []}``."""
    path = allowlist_path(home)
    text, err = _read_text(path)
    if err or text is None:
        return None, err
    if not text.strip():
        return {"approvals": []}, None
    try:
        data = json.loads(text)
    except ValueError:
        return None, f"{path}: not valid JSON; OpenShard will not modify it."
    if not isinstance(data, dict) or not isinstance(data.get("approvals", []), list):
        return None, f"{path}: unexpected shape; OpenShard will not modify it."
    data.setdefault("approvals", [])
    return data, None


def approved_hermes_events(allowlist: object) -> list[str]:
    """Events (of ``HOOK_EVENTS``) whose OpenShard command Hermes has approved."""
    if not isinstance(allowlist, dict) or not isinstance(allowlist.get("approvals"), list):
        return []
    approved = {
        a.get("event") for a in allowlist["approvals"]
        if isinstance(a, dict) and isinstance(a.get("command"), str) and a["command"] == HOOK_COMMAND
    }
    return [e for e in HOOK_EVENTS if e in approved]


def _with_approvals(allowlist: dict) -> dict:
    new = copy.deepcopy(allowlist)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    kept = [
        a for a in new["approvals"]
        if not (isinstance(a, dict) and a.get("command") == HOOK_COMMAND and a.get("event") in HOOK_EVENTS)
    ]
    existing = {
        a.get("event"): a for a in new["approvals"]
        if isinstance(a, dict) and a.get("command") == HOOK_COMMAND
    }
    for event in HOOK_EVENTS:
        kept.append(existing.get(event) or {
            "event": event, "command": HOOK_COMMAND, "approved_at": now, "script_mtime_at_approval": None,
        })
    new["approvals"] = kept
    return new


def _without_approvals(allowlist: dict) -> dict:
    new = copy.deepcopy(allowlist)
    new["approvals"] = [
        a for a in new["approvals"]
        if not (isinstance(a, dict) and isinstance(a.get("command"), str) and is_openshard_hermes_hook(a))
    ]
    return new


def _write_allowlist(path: Path, data: dict) -> None:
    _write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Public install / uninstall
# ---------------------------------------------------------------------------


def _error(message: str, path: Path | None = None) -> ClaudeHooksInstallResult:
    return ClaudeHooksInstallResult(status="error", settings_path=path, message=message)


def install_hermes_hooks(*, home: Path | None = None) -> ClaudeHooksInstallResult:
    """Add OpenShard's hooks to Hermes' ``config.yaml`` and allowlist them. Never raises."""
    try:
        home = home if home is not None else hermes_home()
        path = config_path(home)
        text, err = _read_text(path)
        if err or text is None:
            return _error(err or "Could not read Hermes configuration.", path)
        config, err = _parse_config(text, path)
        if err or config is None:
            return _error(err or "Could not read Hermes configuration.", path)
        allow_path = allowlist_path(home)
        allowlist, err = load_allowlist(home)
        if err or allowlist is None:
            return _error(err or "Could not read Hermes hook allowlist.", allow_path)
        try:
            merged, changes = merge_hermes_hooks(config)
        except ValueError as exc:
            return _error(f"{path}: {exc}; OpenShard will not modify it.", path)

        warnings: list[str] = []
        config_changed = merged != config
        if config_changed:
            if "hooks" not in config:
                # Nothing to merge into: append a marker-delimited block, so
                # every existing comment and byte of the file survives.
                new_text = text + ("" if not text or text.endswith("\n") else "\n") + render_managed_block()
            else:
                new_text = _dump_config(merged)
                warnings.append(
                    f"{path.name} already had a `hooks:` section, so it was re-written (comments are not "
                    f"preserved); the original is saved as {path.name}{BACKUP_SUFFIX}."
                )
            check, cerr = _parse_config(new_text, path)
            if cerr or check is None or installed_hermes_events(check) != list(HOOK_EVENTS) or (
                {k: v for k, v in check.items() if k != "hooks"} != {k: v for k, v in config.items() if k != "hooks"}
            ):
                return _error(f"{path}: could not produce a valid configuration; left unchanged.", path)
            if "hooks" in config:
                _backup_once(path, text)
            _write_text(path, new_text)

        new_allowlist = _with_approvals(allowlist)
        if new_allowlist != allowlist or not allow_path.exists():
            _write_allowlist(allow_path, new_allowlist)
            consent_changed = True
        else:
            consent_changed = False

        if not config_changed and not consent_changed:
            status, message = "already_installed", "Hermes Agent auto-capture hooks already configured."
        else:
            status = "installed" if all(v == "added" for v in changes.values()) else "updated"
            message = "Hermes Agent auto-capture hooks configured."
        return ClaudeHooksInstallResult(
            status=status, settings_path=path, events=changes, message=message, warnings=warnings,
        )
    except Exception as exc:
        return _error(f"Failed to configure Hermes hooks: {type(exc).__name__}")


def uninstall_hermes_hooks(*, home: Path | None = None) -> ClaudeHooksInstallResult:
    """Remove OpenShard's hooks and allowlist entries from Hermes' home. Never raises."""
    try:
        home = home if home is not None else hermes_home()
        path = config_path(home)
        text, err = _read_text(path)
        if err or text is None:
            return _error(err or "Could not read Hermes configuration.", path)
        config, err = _parse_config(text, path)
        if err or config is None:
            return _error(err or "Could not read Hermes configuration.", path)
        allow_path = allowlist_path(home)
        allowlist, err = load_allowlist(home)
        if err or allowlist is None:
            return _error(err or "Could not read Hermes hook allowlist.", allow_path)

        stripped = _strip_managed_block(text)
        new_config, changes = remove_hermes_hooks(config)
        if stripped is not None and _parse_config(stripped, path)[0] == new_config:
            _write_text(path, stripped)  # OpenShard's own block: remove exactly it
            config_changed = True
        elif new_config != config:
            _backup_once(path, text)
            _write_text(path, _dump_config(new_config))
            config_changed = True
        else:
            config_changed = False

        new_allowlist = _without_approvals(allowlist)
        allow_changed = new_allowlist != allowlist
        if allow_changed:
            _write_allowlist(allow_path, new_allowlist)

        if not config_changed and not allow_changed:
            return ClaudeHooksInstallResult(
                status="not_installed", settings_path=path, events=changes,
                message="No OpenShard Hermes hooks were configured.",
            )
        return ClaudeHooksInstallResult(
            status="removed", settings_path=path, events=changes,
            message="Hermes Agent auto-capture hooks removed.",
        )
    except Exception as exc:
        return _error(f"Failed to remove Hermes hooks: {type(exc).__name__}")
