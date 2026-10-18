"""Whether Claude Code will actually run OpenShard's hooks, and a hash of what is installed.

Claude Code's command and HTTP hooks fail open: a hook that times out,
errors, points at a command that is not on the PATH or at a port nothing
listens on only produces a non-blocking notice, and the agent carries on.
So "hooks configured" is not "capture happened". This module reads what can
be known locally, without running Claude Code:

* every settings file Claude Code merges hooks from (managed, user, project,
  local) and the OpenShard hook entries in each;
* settings that switch those hooks off (``disableAllHooks`` anywhere,
  ``allowManagedHooksOnly`` in managed settings);
* whether each hook's target resolves: the command's executable on this
  shell's PATH (or the script path exists and is executable), something
  listening on the HTTP hook's local port;
* timeouts short enough to drop events or long enough to stall the agent.

``hook_config_hash`` is a stable digest of the installed OpenShard hook
entries (scope, event, matcher, type, target, timeout, async) and of the two
switches above. It never includes headers, so the capture credential is not
part of it. The hook path records it when a session is first observed.

Read-only and never raises. Structural evidence only: a clean check says the
configuration looks runnable here, not that any event was delivered.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import socket
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openshard.adapters.claude_hooks_install import (
    SETTINGS_RELPATH,
    _hook_entry_port,
    is_openshard_hook,
)

SCOPE_MANAGED = "managed"
SCOPE_USER = "user"
SCOPE_PROJECT = "project"
SCOPE_LOCAL = "local"

SEVERITY_BLOCKING = "blocking"
SEVERITY_WARNING = "warning"

MIN_TIMEOUT_SECONDS = 2
MAX_TIMEOUT_SECONDS = 60
_CONNECT_TIMEOUT_SECONDS = 0.3
_HASH_VERSION = 1
# 96 bits is plenty to tell configurations apart, and stays under the
# 32-character run the Event scrubber withholds as a possible secret.
_HASH_HEX = 24


def _managed_dirs() -> list[Path]:
    """Where Claude Code reads file-based managed settings on this OS."""
    if sys.platform == "darwin":
        return [Path("/Library/Application Support/ClaudeCode")]
    if os.name == "nt":
        return [Path(r"C:\Program Files\ClaudeCode"), Path(r"C:\ProgramData\ClaudeCode")]
    return [Path("/etc/claude-code")]


@dataclass(frozen=True)
class SettingsSource:
    scope: str
    path: Path
    data: dict | None  # None: absent or unreadable
    error: str | None = None


def _read(scope: str, path: Path) -> SettingsSource:
    try:
        if not path.is_file():
            return SettingsSource(scope, path, None)
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return SettingsSource(scope, path, None, f"could not be read ({type(exc).__name__})")
    if not text.strip():
        return SettingsSource(scope, path, {})
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return SettingsSource(scope, path, None, "is not valid JSON")
    if not isinstance(data, dict):
        return SettingsSource(scope, path, None, "does not hold a JSON object")
    return SettingsSource(scope, path, data)


def settings_sources(
    repo_root: Path | None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    managed_dirs: list[Path] | None = None,
) -> list[SettingsSource]:
    """Every settings file Claude Code merges hooks from that exists here, managed first."""
    source_env = os.environ if env is None else env
    out: list[SettingsSource] = []
    for directory in (_managed_dirs() if managed_dirs is None else managed_dirs):
        candidates = [directory / "managed-settings.json"]
        try:
            drop_in = directory / "managed-settings.d"
            if drop_in.is_dir():
                candidates.extend(sorted(p for p in drop_in.glob("*.json") if p.is_file()))
        except OSError:
            pass
        out.extend(_read(SCOPE_MANAGED, p) for p in candidates)
    config_dir = (source_env.get("CLAUDE_CONFIG_DIR") or "").strip()
    user_dir = Path(config_dir) if config_dir else (home or Path.home()) / ".claude"
    out.append(_read(SCOPE_USER, user_dir / "settings.json"))
    if repo_root is not None:
        out.append(_read(SCOPE_PROJECT, Path(repo_root) / ".claude" / "settings.json"))
        out.append(_read(SCOPE_LOCAL, Path(repo_root) / SETTINGS_RELPATH))
    return [s for s in out if s.data is not None or s.error is not None]


def openshard_entries(sources: list[SettingsSource]) -> list[dict[str, Any]]:
    """OpenShard's hook entries in *sources*: scope, event, matcher, type, target, timeout, async."""
    found: list[dict[str, Any]] = []
    for source in sources:
        hooks = (source.data or {}).get("hooks")
        if not isinstance(hooks, dict):
            continue
        for event, groups in hooks.items():
            for group in groups if isinstance(groups, list) else []:
                if not isinstance(group, dict):
                    continue
                matcher = group.get("matcher") if isinstance(group.get("matcher"), str) else None
                for hook in group.get("hooks") or []:
                    if not is_openshard_hook(hook):
                        continue
                    kind = hook.get("type")
                    target = hook.get("url") if kind == "http" else hook.get("command")
                    timeout = hook.get("timeout")
                    found.append({
                        "scope": source.scope, "event": str(event), "matcher": matcher, "type": kind,
                        "target": str(target).strip(),
                        "timeout": timeout if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) else None,
                        "async": hook.get("async") is True,
                    })
    return found


def _switches(sources: list[SettingsSource]) -> tuple[list[SettingsSource], bool]:
    disabled = [s for s in sources if (s.data or {}).get("disableAllHooks") is True]
    managed_only = any(
        s.scope == SCOPE_MANAGED and (s.data or {}).get("allowManagedHooksOnly") is True for s in sources
    )
    return disabled, managed_only


def config_hash(sources: list[SettingsSource]) -> str:
    """``sha256:<24 hex>`` of the OpenShard hook entries and the switches that disable them. Order-independent."""
    disabled, managed_only = _switches(sources)
    rows = sorted(
        json.dumps([e["scope"], e["event"], e["matcher"], e["type"], e["target"], e["timeout"], e["async"]])
        for e in openshard_entries(sources)
    )
    payload = {
        "v": _HASH_VERSION, "hooks": rows,
        "disable_all_hooks": sorted({s.scope for s in disabled}), "managed_hooks_only": managed_only,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return "sha256:" + digest.hexdigest()[:_HASH_HEX]


def hook_config_hash(repo_root: Path | None, **kwargs: Any) -> str | None:
    """The installed hook configuration's hash for *repo_root*, or None if it cannot be read. Never raises."""
    try:
        return config_hash(settings_sources(repo_root, **kwargs))
    except Exception:  # observation must never break the hook that calls it
        return None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _issue(code: str, severity: str, detail: str, fix: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "detail": detail, "fix": fix}


def _command_issue(command: str, which: Callable[[str], str | None]) -> dict[str, str] | None:
    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        parts = command.split()
    if not parts:
        return None
    exe = parts[0]
    if "/" in exe or "\\" in exe:
        path = Path(os.path.expanduser(exe))
        if not path.exists():
            return _issue("command_not_found", SEVERITY_BLOCKING,
                          f"the hook command {exe} does not exist, so the hook fails and Claude Code carries on without it",
                          "run `openshard setup` to rewrite the hooks")
        if not os.access(path, os.X_OK):
            return _issue("command_not_executable", SEVERITY_BLOCKING,
                          f"the hook command {exe} is not executable, so the hook fails and Claude Code carries on without it",
                          f"make it executable (chmod +x {exe}) or run `openshard setup`")
        return None
    if which(exe) is None:
        return _issue("command_not_found", SEVERITY_BLOCKING,
                      f"`{exe}` is not on this shell's PATH, so the SessionStart hook fails and Claude Code "
                      "carries on without it (the capture service is not started)",
                      f"install OpenShard where the shell that starts `claude` finds `{exe}` "
                      "(for example `pipx install openshard`), then run `openshard setup`")
    return None


def _switch_issues(sources: list[SettingsSource], entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    disabled, managed_only = _switches(sources)
    unmanaged = [e for e in entries if e["scope"] != SCOPE_MANAGED]
    for source in disabled:
        if source.scope == SCOPE_MANAGED or unmanaged:
            issues.append(_issue(
                "hooks_disabled", SEVERITY_BLOCKING,
                f"disableAllHooks is set in {source.scope} settings ({source.path}), so Claude Code runs none of OpenShard's hooks",
                f"remove \"disableAllHooks\" from {source.path}" if source.scope != SCOPE_MANAGED
                else "ask your administrator to remove disableAllHooks from managed settings",
            ))
    if managed_only and unmanaged:
        issues.append(_issue(
            "managed_hooks_only", SEVERITY_BLOCKING,
            "managed settings set allowManagedHooksOnly, so the OpenShard hooks in "
            f"{', '.join(sorted({e['scope'] for e in unmanaged}))} settings do not run",
            "ask your administrator to add OpenShard's hooks to managed settings or to allow non-managed hooks",
        ))
    return issues


def _timeout_issue(entry: dict[str, Any]) -> dict[str, str] | None:
    timeout = entry.get("timeout")
    if timeout is None:
        return None
    if timeout < MIN_TIMEOUT_SECONDS:
        return _issue("timeout_too_short", SEVERITY_WARNING,
                      f"the {entry['event']} hook times out after {timeout}s; a slower delivery is dropped and the action goes ahead",
                      "run `openshard setup` to restore the default timeouts")
    if timeout > MAX_TIMEOUT_SECONDS:
        return _issue("timeout_too_long", SEVERITY_WARNING,
                      f"the {entry['event']} hook may hold Claude Code for up to {timeout}s if capture stalls",
                      "run `openshard setup` to restore the default timeouts")
    return None


def check_claude_hooks(
    repo_root: Path | None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    managed_dirs: list[Path] | None = None,
    which: Callable[[str], str | None] | None = None,
    port_open: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    """Health of the installed Claude Code hooks for ``openshard doctor``. Never raises.

    ``installed``: an OpenShard hook entry exists in some settings file;
    ``blocked``: the first blocking problem as one plain sentence with its
    fix (None when none); ``issues``: every problem found.
    """
    which = which or shutil.which
    port_open = port_open or _port_open
    try:
        sources = settings_sources(repo_root, env=env, home=home, managed_dirs=managed_dirs)
        entries = openshard_entries(sources)
        issues: list[dict[str, str]] = []
        for source in sources:
            if source.error and source.scope != SCOPE_LOCAL:  # the local file has its own doctor line
                issues.append(_issue("settings_unreadable", SEVERITY_WARNING,
                                     f"{source.scope} settings ({source.path}) {source.error}; Claude Code skips what it cannot read",
                                     f"fix or remove {source.path}"))
        if entries:
            issues.extend(_switch_issues(sources, entries))
            seen_targets: set[str] = set()
            for entry in entries:
                target = entry["target"]
                if target in seen_targets:
                    continue
                seen_targets.add(target)
                if entry["type"] == "command":
                    problem = _command_issue(target, which)
                    if problem:
                        issues.append(problem)
                    continue
                port = _hook_entry_port({"type": "http", "url": target})
                if port is not None and not port_open(port):
                    issues.append(_issue(
                        "capture_service_unreachable", SEVERITY_WARNING,
                        f"nothing listens on 127.0.0.1:{port}; Claude Code lets each action through and only the "
                        "background watchdog can still record it",
                        "run `openshard capture start`, or start a new Claude Code session (SessionStart starts the service)",
                    ))
            for entry in entries:
                problem = _timeout_issue(entry)
                if problem and problem not in issues:
                    issues.append(problem)
        blocking = [i for i in issues if i["severity"] == SEVERITY_BLOCKING]
        events: dict[str, list[str]] = {}
        for entry in entries:
            scopes = events.setdefault(entry["event"], [])
            if entry["scope"] not in scopes:
                scopes.append(entry["scope"])
        return {
            "agent": "claude_code",
            "installed": bool(entries),
            "ok": not issues,
            "blocked": f"{blocking[0]['detail']}; {blocking[0]['fix']}" if blocking else None,
            "issues": issues,
            "events": events,
            "sources": [{"scope": s.scope, "path": str(s.path), "error": s.error} for s in sources],
            "config_hash": config_hash(sources),
        }
    except Exception:  # doctor must still print the rest
        return {"agent": "claude_code", "installed": None, "ok": None, "blocked": None, "issues": [],
                "events": {}, "sources": [], "config_hash": None}


def doctor_rows(health: Mapping[str, Any]) -> list[tuple[str, bool, str]]:
    """The doctor's ``Hook health`` row: warnings with their fixes. Blocking problems ride on the hooks row."""
    if not health.get("installed"):
        return []
    warnings = [i for i in health.get("issues") or [] if i.get("severity") == SEVERITY_WARNING]
    if not warnings:
        return [("Hook health", not health.get("blocked"), "see the hooks line above")]
    return [("Hook health", False, "; ".join(f"{i['detail']} (fix: {i['fix']})" for i in warnings))]
