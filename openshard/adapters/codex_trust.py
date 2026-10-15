"""Detect whether Codex trusts a repository enough to run its OpenShard hooks.

Codex loads project hooks (``<repo>/.codex/hooks.json``) only once the
*project* is trusted. It records that in its user config,
``$CODEX_HOME/config.toml`` (default ``~/.codex/config.toml``)::

    [projects.'c:\\path\\to\\repo']
    trust_level = "trusted"

Observed with codex-cli 0.162.1: the first ``codex exec`` in a fresh repo
captures nothing and writes that table; later runs capture fully even with no
per-hook state. Per-hook entries come only from interactive ``/hooks`` review::

    [hooks.state.'<abs path to hooks.json>:session_start:0:0']
    trusted_hash = "sha256:..."

so they are informational (``codex_hooks_reviewed``). Presence is the signal;
Codex's hash is deliberately not recomputed. Paths match case-insensitively
on Windows with separators and trailing slashes normalised.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path

from openshard.adapters.codex_hooks_install import (
    HOOKS_RELPATH,
    installed_codex_events,
    load_codex_hooks,
)

TRUSTED = "trusted"
UNTRUSTED = "untrusted"
PARTIAL = "partial"
UNKNOWN = "unknown"
REVIEWED = "reviewed"
UNREVIEWED = "unreviewed"

HOOKS_TRUST_NEXT_STEP = (
    "Codex has not trusted this project yet: open Codex in this repository once and trust it; "
    "the first session there is not captured."
)
HOOKS_REVIEW_NOTE = (
    "Codex has not recorded a /hooks review for these hooks; `codex exec` still captures, "
    "but the interactive Codex UI may ask you to review them under /hooks."
)

_EVENT_KEYS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "user_prompt_submit",
    "PostToolUse": "post_tool_use",
    "Stop": "stop",
    "SessionEnd": "session_end",
    "Interrupt": "interrupt",
}


def _norm_path(value: str) -> str:
    text = value.replace("\\", "/")
    if text.startswith("//?/"):
        text = text[4:]
    text = text.rstrip("/")
    return text.lower() if os.name == "nt" else text


def codex_config_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    home = source.get("CODEX_HOME")
    base = Path(home) if home else Path(source.get("USERPROFILE") or source.get("HOME") or Path.home()) / ".codex"
    return base / "config.toml"


def _snake(event: str) -> str:
    if event in _EVENT_KEYS:
        return _EVENT_KEYS[event]
    out = []
    for i, ch in enumerate(event):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _load(env: Mapping[str, str] | None) -> dict | None:
    cfg_path = codex_config_path(env)
    if not cfg_path.is_file():
        return None
    with cfg_path.open("rb") as fh:
        return tomllib.load(fh)


def _approved_events(config: dict, hooks_path: Path) -> set[str]:
    state = (config.get("hooks") or {}).get("state") or {}
    if not isinstance(state, dict):
        return set()
    want = _norm_path(str(hooks_path.resolve()))
    approved: set[str] = set()
    for key, value in state.items():
        parts = str(key).rsplit(":", 3)
        if len(parts) != 4 or _norm_path(parts[0]) != want:
            continue
        if isinstance(value, dict) and not value.get("trusted_hash"):
            continue
        approved.add(parts[1])
    return approved


def _project_trusted(config: dict, repo_root: Path) -> bool:
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return False
    trusted = {
        _norm_path(str(k)) for k, v in projects.items()
        if isinstance(v, dict) and str(v.get("trust_level", "")).lower() == "trusted"
    }
    root = Path(repo_root).resolve()
    return any(_norm_path(str(p)) in trusted for p in (root, *root.parents))


def _present_events(repo_root: Path) -> list[str] | None:
    config_file, err = load_codex_hooks(repo_root)
    if err or config_file is None:
        return None
    return installed_codex_events(config_file) or None


def codex_hook_trust(repo_root: Path, *, env: Mapping[str, str] | None = None) -> str:
    """``trusted`` / ``untrusted`` / ``partial`` / ``unknown`` for the repo's OpenShard Codex hooks.

    ``trusted``: Codex trusts the project, or every OpenShard event has a
    ``hooks.state`` entry. ``partial``: project not trusted but only some
    events have entries. ``untrusted``: neither. ``unknown``: Codex's config is
    missing/unparseable or the repo has no OpenShard hooks. Never raises.
    """
    try:
        config = _load(env)
        present = _present_events(repo_root)
        if config is None or present is None:
            return UNKNOWN
        if _project_trusted(config, repo_root):
            return TRUSTED
        approved = _approved_events(config, Path(repo_root) / HOOKS_RELPATH)
        have = [e for e in present if _snake(e) in approved]
        if len(have) == len(present):
            return TRUSTED
        return PARTIAL if have else UNTRUSTED
    except Exception:  # noqa: BLE001 - detection must never break doctor/setup
        return UNKNOWN


def codex_hooks_reviewed(repo_root: Path, *, env: Mapping[str, str] | None = None) -> str:
    """Informational: ``reviewed`` / ``unreviewed`` (missing ``hooks.state`` entries) / ``unknown``.

    ``unreviewed`` does not stop ``codex exec`` capturing in a trusted project;
    only the interactive TUI may still ask to review hooks under ``/hooks``.
    """
    try:
        config = _load(env)
        present = _present_events(repo_root)
        if config is None or present is None:
            return UNKNOWN
        approved = _approved_events(config, Path(repo_root) / HOOKS_RELPATH)
        return REVIEWED if all(_snake(e) in approved for e in present) else UNREVIEWED
    except Exception:  # noqa: BLE001
        return UNKNOWN
