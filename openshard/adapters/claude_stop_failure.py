"""Claude Code ``StopFailure``: the turn ended on an API error instead of ``Stop``.

Claude Code fires ``StopFailure`` in place of ``Stop`` when a turn ends
because of an API error, with ``error`` set to one of a documented set of
values. It is folded as a ``SessionIdle`` boundary (a snapshot, never a
completed turn) carrying the error value, and it adds a completeness reason
so the Receipt's ``Partial`` says why the turn stopped:

* ``stopped_at_cap`` -- ``error: max_output_tokens`` (the response hit the
  output-token limit);
* ``stopped_on_error`` -- any other documented value (``rate_limit``,
  ``server_error``, ``billing_error``, ...). An undocumented value is
  recorded as ``unknown``, never guessed.

Both are what the agent reported (``agent_reported``). The other termination
causes stay as they were: OpenShard's own idle timeout is
``session_idle_sealed`` and a missing end is ``session_end_not_observed``. A
lost VM is not observable from inside it, so no local reason claims one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from openshard.history.capture_completeness import (
    REASON_STOPPED_AT_CAP,
    REASON_STOPPED_ON_ERROR,
    make_reason,
)

EVENT_STOP_FAILURE = "StopFailure"
ATTR_STOP_ERROR = "stop_error"
# Claude Code hooks reference, ``StopFailure`` matcher values.
STOP_ERRORS: frozenset[str] = frozenset({
    "rate_limit", "overloaded", "authentication_failed", "oauth_org_not_allowed", "account_on_hold",
    "billing_error", "invalid_request", "model_not_found", "server_error", "max_output_tokens",
    "cloud_credential_error", "unknown",
})
CAP_ERRORS: frozenset[str] = frozenset({"max_output_tokens"})


def extract(data: Mapping[str, Any], extract_hook: Callable[..., Any], *, event_override: str | None = None) -> Any:
    """``extract_hook(data)``, with a ``StopFailure`` read as ``SessionIdle`` plus its error value."""
    event = data.get("hook_event_name") or event_override
    if event != EVENT_STOP_FAILURE:
        return extract_hook(data, event_override=event_override)
    payload = extract_hook({**data, "hook_event_name": "SessionIdle"}, event_override=None)
    if payload is not None:
        error = data.get("error")
        payload.attrs = {**payload.attrs, ATTR_STOP_ERROR: error if error in STOP_ERRORS else "unknown"}
    return payload


def note_stop_failure(buf: dict, attrs: Mapping[str, Any]) -> None:
    """Add (or count again) the completeness reason for a reported ``StopFailure``."""
    error = attrs.get(ATTR_STOP_ERROR)
    if error not in STOP_ERRORS:
        return
    kind = REASON_STOPPED_AT_CAP if error in CAP_ERRORS else REASON_STOPPED_ON_ERROR
    losses = [r for r in (buf.get("capture_losses") or []) if isinstance(r, dict)]
    held = next((r for r in losses if r.get("kind") == kind), None)
    held_count = held.get("count") if held else None
    count = (held_count if isinstance(held_count, int) else 0) + 1
    # One stop keeps its error value in the detail; repeated stops read as the count (merge_reasons).
    reason = make_reason(kind, count, f"Claude Code reported the turn stopped: {error}")
    buf["capture_losses"] = [r for r in losses if r is not held] + [reason]


__all__ = ["ATTR_STOP_ERROR", "CAP_ERRORS", "EVENT_STOP_FAILURE", "STOP_ERRORS", "extract", "note_stop_failure"]
