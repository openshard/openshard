"""The run's own settings as Claude Code reported them: effort and permission mode.

Moved out of ``history/receipt_evidence.py`` unchanged (it is re-exported
there); ``project_entry_evidence`` projects it as ``runtime_configuration``.
"""

from __future__ import annotations

from typing import Any

_CLAUDE_PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk")


def runtime_configuration_block(entry: dict) -> dict | None:
    """Effective settings explicitly reported by Claude hooks, never requested env settings.

    ``effort`` is the effective effort level; ``permission_mode`` is the
    permission regime Claude Code reported running under (last seen) with
    every distinct mode in ``permission_modes_seen``, first-seen order. Both
    are the agent's own report (``agent_reported``): OpenShard observed them
    and enforced neither.
    """
    raw = entry.get("capture")
    capture: dict = raw if isinstance(raw, dict) else {}
    block: dict[str, Any] = {}
    level = capture.get("effort_level")
    if capture.get("effort_source") == "claude_hook" and level in ("low", "medium", "high", "xhigh", "max"):
        block["effort"] = level
    mode = capture.get("permission_mode")
    if capture.get("permission_mode_source") == "claude_hook" and mode in _CLAUDE_PERMISSION_MODES:
        block["permission_mode"] = mode
        seen = capture.get("permission_modes_seen")
        block["permission_modes_seen"] = [
            m for m in (seen if isinstance(seen, list) else []) if m in _CLAUDE_PERMISSION_MODES
        ][:len(_CLAUDE_PERMISSION_MODES)] or [mode]
    if not block:
        return None
    block.update({"source": "claude_hook", "evidence": "agent_reported"})
    return block


__all__ = ["runtime_configuration_block"]
