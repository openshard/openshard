"""Add/remove only the opt-in prompt learning hook, preserving native settings."""
from __future__ import annotations

from pathlib import Path

from openshard.adapters.claude_hooks_install import (
    _read_settings,
    _write_settings,
    ensure_local_settings_ignored,
)

PATHS = {"claude": ".claude/settings.local.json", "codex": ".codex/hooks.json"}


def configure(root: Path, agent: str, *, remove: bool = False) -> dict:
    if agent not in PATHS:
        raise ValueError("Unsupported learning hook agent")
    path = root / PATHS[agent]
    settings, error = _read_settings(path)
    if error or settings is None:
        raise ValueError(error or "Cannot read native hook settings")
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Unexpected hooks layout; settings were left unchanged")
    groups = hooks.get("UserPromptSubmit", [])
    if not isinstance(groups, list) or any(not isinstance(g, dict) or not isinstance(g.get("hooks"), list) or any(not isinstance(h, dict) for h in g["hooks"]) for g in groups):
        raise ValueError("Unexpected prompt hook layout; settings were left unchanged")
    command = f"openshard learn hook {agent}"
    found = any(h.get("type") == "command" and h.get("command") == command for g in groups for h in g["hooks"])
    if remove:
        if not found:
            return {"agent": agent, "change": "unchanged", "warning": None}
        kept = []
        for group in groups:
            remaining = [h for h in group["hooks"] if not (h.get("type") == "command" and h.get("command") == command)]
            if remaining:
                kept.append({**group, "hooks": remaining})
        hooks["UserPromptSubmit"] = kept
    else:
        if found:
            return {"agent": agent, "change": "unchanged", "warning": None}
        hooks["UserPromptSubmit"] = [*groups, {"hooks": [{"type": "command", "command": command, "timeout": 2}]}]
    settings["hooks"] = hooks
    _write_settings(path, settings)
    warning = None if remove else ensure_local_settings_ignored(root, rel=PATHS[agent], note="OpenShard per-user learning hook")
    return {"agent": agent, "change": "removed" if remove else "installed", "warning": warning}
