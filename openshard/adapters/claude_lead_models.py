"""Which models a Claude Code session's lead agent ran, as opposed to its subagents.

A Claude Code transcript holds the lead agent's messages in the main file and
each subagent's in ``<session>/subagents/*.jsonl``; a subagent's messages can
also appear in the main file marked ``isSidechain``. This module keeps the
lead's own output per model while the transcript is read
(``note_lead_output`` / ``lead_models_block``), ranks the headline model
lead-first (``transcript_models``) and, when the transcript proves it, labels
each observed model as the lead's or a subagent's (``model_roles``).

Nothing here guesses: without lead messages in the window the ranking is the
plain output-token order, and no roles are recorded.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_OUTPUT = 2  # a transcript row is [model, input, output, ...]


def note_lead_output(state: dict, index: int, rec: Mapping[str, Any], previous: object, row: list) -> None:
    """Count one message's output for the lead when it is in the main file and not a sidechain.

    A streamed message's later line replaces its earlier one (*previous*), so
    the earlier output is taken back before the latest is added.
    """
    if index != 0 or rec.get("isSidechain") is True:
        return
    lead = state.setdefault("lead_output", {})
    if isinstance(previous, list) and len(previous) > _OUTPUT and previous[0] in lead:
        lead[previous[0]] = max(0, int(lead[previous[0]]) - int(previous[_OUTPUT]))
    lead[row[0]] = int(lead.get(row[0]) or 0) + int(row[_OUTPUT])


def lead_models_block(state: Mapping[str, Any], by_model: Mapping[str, Any]) -> dict:
    """``{"lead_models": [...]}`` ranked by the lead's own output, or ``{}`` when there are none."""
    raw = state.get("lead_output")
    lead_output: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    ranked = sorted(
        ((m, out) for m, out in lead_output.items() if isinstance(m, str) and m in by_model),
        key=lambda kv: -int(kv[1] or 0),
    )
    return {"lead_models": [m for m, _ in ranked]} if ranked else {}


def _sanitize(model_id: object) -> str | None:
    from openshard.adapters.claude_code_import import _sanitize_model

    if not isinstance(model_id, str) or not model_id:
        return None
    safe = _sanitize_model(model_id)
    return None if safe == "unknown" else safe


def _output(row: Mapping[str, Any]) -> int:
    value = row.get("output")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def transcript_models(usage: Mapping[str, Any]) -> list[str]:
    """API-reported model ids in transcript *usage*, the lead agent's first (sanitized).

    The lead's models (main transcript, not subagent transcripts or sidechain
    messages) come first, ranked by the lead's own output tokens, so a
    subagent that wrote more never headlines the session. Then every other
    model, most output tokens first -- the only ranking when the transcript
    did not say which messages were the lead's.
    """
    by_model = usage.get("by_model")
    if not isinstance(by_model, Mapping):
        return []
    ranked = sorted(
        ((m, row) for m, row in by_model.items() if isinstance(m, str) and isinstance(row, Mapping)),
        key=lambda kv: -_output(kv[1]),
    )
    lead = [m for m in (usage.get("lead_models") or []) if isinstance(m, str) and m in by_model]
    models: list[str] = []
    for raw in [*lead, *(m for m, _row in ranked)]:
        safe = _sanitize(raw)
        if safe and not safe.startswith("<") and safe not in models:
            models.append(safe)
    return models


def model_roles(reported: list[str], models: list[str], usage: Mapping[str, Any] | None) -> dict[str, str] | None:
    """``{model: "lead" | "subagent"}`` for *models*, only when the transcript proves it.

    A model the agent itself reported (hook / status line: the session's own
    model) or that wrote the main transcript's non-sidechain messages is the
    lead's; one with usage only in subagent transcripts or sidechain messages
    is a subagent's. None when the transcript did not say which messages were
    the lead's, or when every model is the lead's (a mid-session switch).
    """
    from openshard.models.pricing import official_rate

    lead_raw = (usage or {}).get("lead_models")
    if not isinstance(lead_raw, list) or not lead_raw:
        return None

    def identity(model: str) -> str:
        rate = official_rate(model)
        return rate.model_id if rate else model.casefold()

    lead_ids = {identity(s) for s in (_sanitize(m) for m in [*reported, *lead_raw]) if s}
    roles = {m: ("lead" if identity(m) in lead_ids else "subagent") for m in models}
    return roles if "subagent" in roles.values() and "lead" in roles.values() else None


__all__ = ["lead_models_block", "model_roles", "note_lead_output", "transcript_models"]
