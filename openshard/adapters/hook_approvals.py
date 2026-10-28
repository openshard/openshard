"""The agent's own permission prompts, observed in the shared hook fold (``adapters/claude_hooks.py``).

* ``ApprovalRequest`` / ``ApprovalDecision``: the agent's human-approval gate
  (Hermes ``pre_approval_request`` / ``post_approval_response``; OpenCode
  ``permission.asked`` / ``permission.replied``, plugin v6). Only a documented
  grant / deny choice becomes a granted / denied Event; a timeout, withdrawal
  or undeliverable prompt means nobody decided, and is recorded as exactly that.
* ``PermissionPrompt``: Claude Code showed a person a permission prompt
  (``Notification`` ``permission_prompt``); its answer is steering's to infer
  (``adapters/steering.py``).

Observed only: OpenShard neither raises nor answers any of these prompts.
"""

from __future__ import annotations

from typing import Any

from openshard.adapters import steering

# Which ``HookPayload.attrs`` reach an approval Event's metadata (``sanitize_metadata`` keeps ten keys).
APPROVAL_ATTR_KEYS: tuple[str, ...] = (
    "choice", "surface", "pattern_key", "decided_by", "tool_call_id", "turn_id",
)
# Approval choices (Hermes' ``post_approval_response.choice``; OpenCode's replies mapped onto them). A
# grant lets the command run; ``deny`` / ``smart_deny`` is a refusal; the rest mean nobody answered
# (or the prompt could not be delivered), so the command did not run and no one refused it either.
APPROVAL_GRANTED = frozenset({"once", "session", "always", "smart_approve"})
APPROVAL_DENIED = frozenset({"deny", "smart_deny"})


def apply_approval_event(buf: dict, event: str, payload: Any, now: str) -> tuple[str, bool, bool]:
    """Stage one approval-gate fact; never work done, so it neither opens nor folds a record."""
    from openshard.adapters.claude_hooks import EVENT_APPROVAL_REQUEST, _append_event

    counts = buf.get("approvals")
    if not isinstance(counts, dict):
        counts = buf["approvals"] = {}
    meta = {"hook": event}
    for attr_key in APPROVAL_ATTR_KEYS:
        if attr_key in payload.attrs:
            meta[attr_key] = payload.attrs[attr_key]
    command_text = payload.command_action or "command"
    if event == EVENT_APPROVAL_REQUEST:
        counts["requested"] = int(counts.get("requested") or 0) + 1
        _append_event(
            buf, event_type="approval.requested", action=f"approval requested: {command_text}",
            target=payload.command_target, status="started", evidence="agent_reported",
            metadata=meta, occurred_at=now,
        )
        return "approval requested", False, False
    choice = payload.attrs.get("choice")
    if choice in APPROVAL_GRANTED:
        counts["granted"] = int(counts.get("granted") or 0) + 1
        event_type, status, verb = "approval.granted", "passed", f"approval granted ({choice})"
    elif choice in APPROVAL_DENIED:
        counts["denied"] = int(counts.get("denied") or 0) + 1
        event_type, status, verb = "approval.denied", "failed", f"approval denied ({choice})"
    else:
        counts["unanswered"] = int(counts.get("unanswered") or 0) + 1
        event_type, status = "session.activity", "unknown"
        verb = f"approval not decided ({choice})" if isinstance(choice, str) else "approval not decided"
    meta.update({"steering_kind": steering.KIND_PERMISSION} if steering.person_decision(choice) else {})
    _append_event(
        buf, event_type=event_type, action=f"{verb}: {command_text}", target=payload.command_target,
        status=status, evidence="agent_reported", metadata=meta, occurred_at=now,
    )
    return "approval decision", False, False


def apply_permission_prompt(buf: dict, payload: Any, now: str) -> tuple[str, bool, bool]:
    """Claude Code showed a person a permission prompt (steering staged it); not work done."""
    from openshard.adapters.claude_hooks import _append_event

    named = payload.attrs.get("tool")
    _append_event(
        buf, event_type="session.activity",
        action=f"permission prompt shown: {named if isinstance(named, str) else 'a tool'}", status="unknown",
        evidence="agent_reported", metadata={"hook": payload.event}, occurred_at=now,
    )
    return "permission prompt noted", False, False


def claude_permission_payload(data: Any, session_id: str | None) -> Any:
    """A Claude Code ``Notification``: the permission prompt it announces, or None for any other notice."""
    from openshard.adapters.claude_hooks import (
        EVENT_PERMISSION_PROMPT,
        HookPayload,
        _str_or_none,
        _valid_transcript_path,
    )

    if data.get("notification_type") != "permission_prompt":
        return None  # idle and auth notifications carry no steering fact
    tool = steering.claude_permission_tool(data.get("message"))  # only the tool name is read from the notice
    return HookPayload(event=EVENT_PERMISSION_PROMPT, session_id=session_id, cwd=_str_or_none(data.get("cwd"), 1_000),
                       tool_name=tool, attrs={"tool": tool} if tool else {},
                       transcript_path=_valid_transcript_path(data.get("transcript_path"), session_id))


__all__ = [
    "APPROVAL_ATTR_KEYS", "APPROVAL_DENIED", "APPROVAL_GRANTED", "apply_approval_event", "apply_permission_prompt",
    "claude_permission_payload",
]
