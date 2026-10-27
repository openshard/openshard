"""Steering capture: every time a person steered the coding agent (docs/steering-capture.md).

One agent-neutral recorder on top of the shared hook fold
(``adapters/claude_hooks.py``). It turns what each agent's hooks and its own
local session log already show into a small list of steering facts on the
staging buffer, and stamps them on the Receipt as ``capture.steering``:

* ``initial_prompt`` / ``follow_up``: a human prompt (``UserPromptSubmit`` in
  the neutral vocabulary). A follow-up carries the intent the local
  ``rules-v1`` classifier (``adapters/steering_intent.py``) gave it.
* ``interjection``: a message the person sent while a turn was still running.
  Never inferred from hook order: only from a log that records it (Claude
  Code's ``queue-operation`` lines, Codex's ``user_message`` inside an open
  task).
* ``interrupt``: the person stopped a turn (Esc). From a hook where the agent
  has one (Codex ``Interrupt``, Cursor ``stop: aborted``, Hermes
  ``interrupted``) or from the log (Claude Code fires no hook on Esc; its
  transcript says ``[Request interrupted by user]``).
* ``permission_decision``: the person answered a permission prompt. Only where
  the agent says a person decided (Hermes interactive approvals; Claude Code's
  transcript records a person's rejection, never an approval).
* ``manual_edit``: files that changed in the working tree between a turn's end
  and the next work in the session, while no turn of this session was running
  (git). OpenShard cannot tell a person from another tool, so the claim is
  "not this agent", stated as such.

Privacy. Prompt text is read only here, on the user's machine, to classify
it, and is dropped at once: hook prompts are classified in
``reduce_hook_payload`` (the queue line carries only the result), log
messages at fold time. Stored per event: kind, intent, confidence, the
classifier rule id, prompt length in characters, times, counts and (manual
edits, local record only) repo-relative paths. Never prompt text.

Unknown stays unknown: a signal an agent does not expose is listed in
``CAPABILITIES`` with the reason and stamped as ``not_available`` on every
Receipt of that agent; it is never reported as zero.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.adapters.capture_agents import (
    AGENT_ANTIGRAVITY,
    AGENT_CLAUDE_CODE,
    AGENT_CODEX,
    AGENT_CURSOR,
    AGENT_GROK_BUILD,
    AGENT_HERMES,
    AGENT_OPENCODE,
)
from openshard.adapters.steering_intent import CONFIDENCES, INTENTS, METHOD, classify_intent

VERSION = 1

KIND_INITIAL = "initial_prompt"
KIND_FOLLOW_UP = "follow_up"
KIND_INTERJECTION = "interjection"
KIND_INTERRUPT = "interrupt"
KIND_PERMISSION = "permission_decision"
KIND_MANUAL_EDIT = "manual_edit"
SIGNALS: tuple[str, ...] = (
    KIND_INITIAL, KIND_FOLLOW_UP, KIND_INTERJECTION, KIND_INTERRUPT, KIND_PERMISSION, KIND_MANUAL_EDIT,
)

AFTER_VALUES: frozenset[str] = frozenset(
    {"turn_end", "failed_check", "tool_call", "permission_prompt", "none", "unknown"}
)
SOURCE_HOOK = "hook"
SOURCE_TRANSCRIPT = "transcript"
SOURCE_GIT = "git"
SOURCE_GITHUB = "github"  # derived from a pull request's reviews and comments (github/steering.py)
SOURCES: frozenset[str] = frozenset({SOURCE_HOOK, SOURCE_TRANSCRIPT, SOURCE_GIT, SOURCE_GITHUB})
DECISIONS: frozenset[str] = frozenset({"allow", "deny", "always_allow"})

LIVE_HOOK = "live_hook"
FROM_LOG = "from_log"
FROM_GIT = "from_git"
NOT_AVAILABLE = "not_available"

_GIT = (FROM_GIT, "working-tree changes between a turn's end and the next work, while no turn ran")

# agent -> signal -> (how, what / why not). The single source for the
# capability matrix in docs/steering-capture.md (a test keeps them equal) and
# for each Receipt's ``not_available`` list.
CAPABILITIES: dict[str, dict[str, tuple[str, str]]] = {
    AGENT_CLAUDE_CODE: {
        KIND_INITIAL: (LIVE_HOOK, "UserPromptSubmit"),
        KIND_FOLLOW_UP: (LIVE_HOOK, "UserPromptSubmit"),
        KIND_INTERJECTION: (FROM_LOG, "transcript queue-operation enqueue (a message queued while Claude worked)"),
        KIND_INTERRUPT: (FROM_LOG, "transcript [Request interrupted by user] marker (Esc fires no hook)"),
        KIND_PERMISSION: (FROM_LOG, "transcript tool rejections only; approvals are not recorded anywhere "
                                    "OpenShard reads, and Core installs no permission hook (it would decide)"),
        KIND_MANUAL_EDIT: _GIT,
    },
    AGENT_CODEX: {
        KIND_INITIAL: (LIVE_HOOK, "UserPromptSubmit"),
        KIND_FOLLOW_UP: (LIVE_HOOK, "UserPromptSubmit"),
        KIND_INTERJECTION: (FROM_LOG, "rollout user_message inside an open task"),
        KIND_INTERRUPT: (LIVE_HOOK, "Interrupt hook, and rollout turn_aborted (interrupted)"),
        KIND_PERMISSION: (NOT_AVAILABLE, "Codex's documented hook events include no approval event, and "
                                         "approval requests are not read from the rollout log"),
        KIND_MANUAL_EDIT: _GIT,
    },
    AGENT_CURSOR: {
        KIND_INITIAL: (LIVE_HOOK, "beforeSubmitPrompt"),
        KIND_FOLLOW_UP: (LIVE_HOOK, "beforeSubmitPrompt"),
        KIND_INTERJECTION: (NOT_AVAILABLE, "Cursor's hooks do not say whether a prompt was sent while a "
                                           "generation was running, and its transcript is deliberately not read"),
        KIND_INTERRUPT: (LIVE_HOOK, "stop with status aborted"),
        KIND_PERMISSION: (NOT_AVAILABLE, "permission decisions are only visible to blocking preToolUse / "
                                         "beforeShellExecution hooks, which OpenShard does not subscribe to"),
        KIND_MANUAL_EDIT: _GIT,
    },
    AGENT_OPENCODE: {
        KIND_INITIAL: (LIVE_HOOK, "chat.message"),
        KIND_FOLLOW_UP: (LIVE_HOOK, "chat.message"),
        KIND_INTERJECTION: (NOT_AVAILABLE, "the OpenShard plugin forwards no signal that a message was sent "
                                           "while a turn was running"),
        KIND_INTERRUPT: (NOT_AVAILABLE, "an aborted turn reaches the plugin only as session.idle, which also "
                                        "follows a normal turn end"),
        KIND_PERMISSION: (NOT_AVAILABLE, "the OpenShard plugin does not forward permission replies"),
        KIND_MANUAL_EDIT: _GIT,
    },
    AGENT_ANTIGRAVITY: {
        KIND_INITIAL: (NOT_AVAILABLE, "Antigravity delivers no user prompt to command hooks "
                                      "(PreInvocation fires per model call)"),
        KIND_FOLLOW_UP: (NOT_AVAILABLE, "Antigravity delivers no user prompt to command hooks"),
        KIND_INTERJECTION: (NOT_AVAILABLE, "no prompt hook, and the transcript has no published schema and is "
                                           "never read"),
        KIND_INTERRUPT: (NOT_AVAILABLE, "Stop's terminationReason values are not documented, so a user stop "
                                        "cannot be told from other ends"),
        KIND_PERMISSION: (NOT_AVAILABLE, "PreToolUse is a permission gate OpenShard does not subscribe to"),
        KIND_MANUAL_EDIT: (FROM_GIT, "working-tree changes between a Stop and the next model invocation"),
    },
    AGENT_GROK_BUILD: {
        KIND_INITIAL: (LIVE_HOOK, "UserPromptSubmit"),
        KIND_FOLLOW_UP: (LIVE_HOOK, "UserPromptSubmit"),
        KIND_INTERJECTION: (NOT_AVAILABLE, "Grok Build's hooks do not mark messages sent mid-turn, and its "
                                           "session files are never read"),
        KIND_INTERRUPT: (NOT_AVAILABLE, "StopCancelled fires alike for a user interrupt, a declined permission "
                                        "and --max-turns"),
        KIND_PERMISSION: (NOT_AVAILABLE, "PermissionDenied does not say whether a person, a rule or a policy "
                                         "denied"),
        KIND_MANUAL_EDIT: _GIT,
    },
    AGENT_HERMES: {
        KIND_INITIAL: (LIVE_HOOK, "pre_llm_call"),
        KIND_FOLLOW_UP: (LIVE_HOOK, "pre_llm_call"),
        KIND_INTERJECTION: (NOT_AVAILABLE, "Hermes' conversation history is never read, and no hook marks a "
                                           "message sent mid-turn"),
        KIND_INTERRUPT: (LIVE_HOOK, "on_session_end with interrupted"),
        KIND_PERMISSION: (LIVE_HOOK, "post_approval_response (once / session / always / deny); smart-mode "
                                     "decisions are not a person's and are left out"),
        KIND_MANUAL_EDIT: _GIT,
    },
}

FROM_GITHUB = "from_github"
AGENT_GROK_BOT = "grok_bot"  # adapters/grok_bot.py (not imported here: the hook path stays light)
AGENT_PR = "pull_request_agent"  # any agent driven from a pull request, with or without a Core adapter

# The environment dimension (docs/steering-capture.md, "Every environment"): the (agent,
# environment) pairs OpenShard has a capture path for. A pair that is absent is not claimed.
ENVIRONMENT_ROWS: tuple[tuple[str, str], ...] = (
    (AGENT_CLAUDE_CODE, "local_terminal"), (AGENT_CLAUDE_CODE, "local_ide"),
    (AGENT_CLAUDE_CODE, "cloud_workspace"), (AGENT_CLAUDE_CODE, "cloud_agent"), (AGENT_CLAUDE_CODE, "ci"),
    (AGENT_CODEX, "local_terminal"), (AGENT_CODEX, "cloud_workspace"), (AGENT_CODEX, "cloud_agent"),
    (AGENT_CODEX, "ci"),
    (AGENT_CURSOR, "local_ide"), (AGENT_CURSOR, "cloud_agent"),
    (AGENT_OPENCODE, "local_terminal"), (AGENT_OPENCODE, "cloud_workspace"),
    (AGENT_ANTIGRAVITY, "local_ide"),
    (AGENT_GROK_BUILD, "local_terminal"), (AGENT_GROK_BUILD, "cloud_workspace"),
    (AGENT_HERMES, "local_terminal"), (AGENT_HERMES, "cloud_workspace"),
    (AGENT_GROK_BOT, "cloud_agent"),
    (AGENT_PR, "web"),
)
_CI_HEADLESS = "a CI run is headless: no person is there to type, stop, answer or edit while it runs"
_GROK_BOT_WHY = ("Grok Bot runs no hooks; Action Recording exports actions, not the conversation, and its "
                 "self-report carries no steering")
_CLOUD_LOG = "; read inside the hosted VM, where the log is written (fixture-tested, not yet on a live hosted run)"
_CLOUD_EDIT = (FROM_GIT, "the hosted VM's working tree between turns; a person seldom has a shell there, "
                         "so this is usually another process")


def environment_capability(agent: str, environment: str, signal: str) -> tuple[str, str]:
    """How *signal* of *agent* is captured in *environment*: ``(how, what / why not)``. Pure.

    A local terminal, a local IDE and a cloud workspace run the same agent
    with Core beside it: the agent's own row (``CAPABILITIES``). A hosted
    agent VM runs the same hooks once Core is installed there
    (``openshard setup --repo``); its log is read inside the VM. CI is
    headless. ``web`` is what reached GitHub (``github/steering.py``).
    """
    if environment == "web":
        from openshard.github.steering import NOT_AVAILABLE as GITHUB_NOT_AVAILABLE

        if signal == KIND_FOLLOW_UP:
            return FROM_GITHUB, ("reviews (changes requested -> correction, approved -> approval, commented) and "
                                 "PR comments addressed to the agent, before each commit")
        return NOT_AVAILABLE, GITHUB_NOT_AVAILABLE[signal]
    if agent == AGENT_GROK_BOT:
        return NOT_AVAILABLE, _GROK_BOT_WHY
    how, why = CAPABILITIES[agent][signal]
    if environment == "ci" and signal != KIND_INITIAL:
        return NOT_AVAILABLE, _CI_HEADLESS
    if environment == "cloud_agent":
        if signal == KIND_MANUAL_EDIT:
            return _CLOUD_EDIT
        if how == FROM_LOG:
            return how, why + _CLOUD_LOG
    return how, why


# Permission prompt counts each agent can report: asked / allowed / denied.
_PERMISSION_COUNTS: dict[str, tuple[bool, bool, bool]] = {
    AGENT_CLAUDE_CODE: (False, False, True),
    AGENT_HERMES: (True, True, True),
}

THINK_CAP_SECONDS = 15 * 60  # a longer pause between turns is time away, not steering
_MAX_EVENTS = 100
_MAX_TURN_ENDS = 100
_MAX_PATHS = 20
_MAX_DEQUEUES = 20
_MAX_CANDIDATES = 200
_LOG_READ_BYTES = 16 * 1024 * 1024  # read in chunks of this size (bounded memory)
_LOG_MAX_BYTES = 256 * 1024 * 1024
_LOG_MAX_LINE = 2_000_000
_QUEUE_MATCH_SECONDS = 5.0
_DUPLICATE_SECONDS = 5.0
_PROMPT_KEYS = ("intent", "confidence", "rule", "chars")

# Hermes ``post_approval_response.choice`` a person made (smart-mode choices are a model's).
_HERMES_DECISIONS = {"once": "allow", "session": "allow", "always": "always_allow", "deny": "deny"}

_CODEX_MARKERS = (b'"task_started"', b'"task_complete"', b'"turn_aborted"', b'"user_message"')

_CLAUDE_INTERRUPT = "[Request interrupted by user]"
_CLAUDE_INTERRUPT_TOOL = "[Request interrupted by user for tool use]"
_CLAUDE_REJECTION = "The user doesn't want to proceed with this tool use"


# ---------------------------------------------------------------------------
# Reduce time: the only place prompt text is looked at for steering
# ---------------------------------------------------------------------------


def prompt_facts(prompt: object) -> dict[str, Any] | None:
    """``{intent, confidence, rule, chars}`` for a human prompt; the text itself is not kept."""
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    result = classify_intent(prompt)
    return {"intent": result.intent, "confidence": result.confidence, "rule": result.rule, "chars": len(prompt)}


def valid_prompt_facts(raw: object) -> dict[str, Any] | None:
    """A queued ``prompt_facts`` block, or None when absent or malformed (never a guess)."""
    if not isinstance(raw, Mapping):
        return None
    intent, confidence, rule, chars = (raw.get(k) for k in _PROMPT_KEYS)
    if intent not in INTENTS or confidence not in CONFIDENCES:
        return None
    if not isinstance(rule, str) or not rule.replace(".", "").replace("_", "").isalnum() or len(rule) > 60:
        return None
    if isinstance(chars, bool) or not isinstance(chars, int) or chars < 0:
        return None
    return {"intent": intent, "confidence": confidence, "rule": rule, "chars": chars}


# ---------------------------------------------------------------------------
# Buffer state
# ---------------------------------------------------------------------------


def _state(buf: dict) -> dict:
    state = buf.get("steering")
    if not isinstance(state, dict):
        state = buf["steering"] = {}
    for key in ("events", "turn_ends", "dequeues"):
        if not isinstance(state.get(key), list):
            state[key] = []
    return state


def _agent(buf: Mapping[str, Any]) -> str:
    agent = buf.get("agent")
    return agent if isinstance(agent, str) and agent in CAPABILITIES else AGENT_CLAUDE_CODE


def _parse(stamp: object) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo is not None else None


def _seconds(a: object, b: object) -> float | None:
    start, end = _parse(a), _parse(b)
    return (end - start).total_seconds() if start is not None and end is not None else None


def _turn_index(buf: Mapping[str, Any], *, opening: bool = False) -> int | None:
    """The turn this event belongs to: the Receipt's turn, or the session's turn count so far."""
    index = buf.get("turn_index")
    if buf.get("receipt_scope") == "turn" and isinstance(index, int) and not isinstance(index, bool) and index > 0:
        return index
    if buf.get("start_source") == "resume":
        return None  # a resumed session's earlier turns are on another Receipt: the number is not known here
    count = buf.get("turn_count")
    done = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
    if done is None:
        return None
    state = buf.get("steering")
    turn_open = isinstance(state, dict) and state.get("turn_open") is True
    return done + 1 if opening or turn_open else done or None


def _turn_index_at(buf: dict, at: str) -> int | None:
    """The turn a log line at *at* belongs to: counted from the prompts before it, not from the fold's state."""
    if buf.get("receipt_scope") == "turn" or buf.get("start_source") == "resume":
        return _turn_index(buf)
    moment = _parse(at) or _EPOCH
    opened = sum(1 for e in _state(buf)["events"]
                 if e.get("kind") in (KIND_INITIAL, KIND_FOLLOW_UP) and _order(e) <= moment)
    return opened or None


def _add(buf: dict, event: dict) -> bool:
    """Stage one steering fact, bounded. False when it repeats a log line already staged.

    Hook events are never de-duplicated here (the hook path's own ``dedup_id``
    does that, and two answers in one second are two answers); a log line can
    be read twice when a rebuilt buffer starts the log again from the top.
    """
    state = _state(buf)
    if event["source"] == SOURCE_TRANSCRIPT:
        same = ("kind", "source", "at", "decision")
        if any(all(seen.get(k) == event.get(k) for k in same) for seen in state["events"]):
            return False
    if len(state["events"]) >= _MAX_EVENTS:
        state["dropped"] = int(state.get("dropped") or 0) + 1
        return False
    state["events"].append(event)
    state["events"].sort(key=_order)
    return True


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _order(event: Mapping[str, Any]) -> datetime:
    return _parse(event.get("at")) or _EPOCH


def _activity_event(buf: dict, action: str, metadata: dict, at: str, evidence: str) -> None:
    from openshard.adapters.claude_hooks import _append_event

    _append_event(buf, event_type="session.activity", action=action, status="unknown", evidence=evidence,
                  metadata={k: v for k, v in metadata.items() if v is not None}, occurred_at=at)


# ---------------------------------------------------------------------------
# Hook-time notes (called from claude_hooks._apply)
# ---------------------------------------------------------------------------


def observe(buf: dict, payload: Any, repo_root: Path, now: str) -> None:
    """The steering side of one neutral hook event (prompts: ``note_prompt``). Never raises."""
    try:
        event = payload.event
        if event in _TURN_ENDS:
            if event == "Interrupt":
                note_interrupt(buf, now)
            note_turn_end(buf, repo_root, now, how=_TURN_ENDS[event])
        elif event == "ModelInvocation":
            note_model_invocation(buf, repo_root, now)  # the first call after a Stop: work resumed
        elif event == "ApprovalDecision":
            note_permission(buf, (payload.attrs or {}).get("choice"), now)
        elif event == "SessionEnd":
            forget(buf, repo_root)
    except Exception:
        pass


_TURN_ENDS = {"Stop": "stop", "Interrupt": "interrupt", "SessionIdle": "idle"}


def note_prompt(buf: dict, facts: dict | None, repo_root: Path, now: str) -> None:
    """A human prompt was submitted: an initial prompt, or a follow-up after a turn. Never raises."""
    try:
        events = buf.get("events") or []
        last = events[-1] if events and isinstance(events[-1], dict) else None
        prompt_event = last if last is not None and (last.get("metadata") or {}).get("hook") == "UserPromptSubmit" else None
        _note_prompt(buf, valid_prompt_facts(facts), repo_root, now, prompt_event)
    except Exception:
        pass


def _note_prompt(buf: dict, facts: dict | None, repo_root: Path, now: str, prompt_event: dict | None) -> None:
    state = _state(buf)
    earlier = state.get("prompts_before") or any(
        e.get("kind") in (KIND_INITIAL, KIND_FOLLOW_UP) for e in state["events"])
    event: dict[str, Any] = {"at": now, "turn_index": _turn_index(buf, opening=True), "source": SOURCE_HOOK}
    if not earlier and buf.get("start_source") not in ("turn", "resume"):
        event.update(kind=KIND_INITIAL, after="none")
    else:
        event["kind"] = KIND_FOLLOW_UP
        end = state.get("last_turn_end_at")
        if state.get("turn_open") or not isinstance(end, str):
            event["after"] = "unknown"  # no turn end was observed since the last prompt (e.g. Esc fires no hook)
        else:
            event["after"] = "failed_check" if state.get("last_turn_failed_check") else "turn_end"
            pause = _seconds(end, now)
            if pause is not None and pause >= 0:
                event["think_seconds"] = round(min(pause, THINK_CAP_SECONDS), 1)
                if pause > THINK_CAP_SECONDS:
                    event["think_capped"] = True
    if facts is not None:
        event["prompt_chars"] = facts["chars"]
        if event["kind"] == KIND_FOLLOW_UP:
            event.update(intent=facts["intent"], intent_confidence=facts["confidence"], intent_rule=facts["rule"])
    elif event["kind"] == KIND_FOLLOW_UP:
        event.update(intent="unknown", intent_confidence="low", intent_rule="no_prompt_text")
    _add(buf, event)
    if prompt_event is not None and isinstance(prompt_event.get("metadata"), dict):
        meta = prompt_event["metadata"]
        if len(meta) < 10:
            meta["steering_kind"] = event["kind"]
        if "intent" in event and len(meta) < 10:
            meta["intent"] = event["intent"]
    state["turn_open"] = True
    state.pop("prompts_before", None)
    note_work_resumed(buf, repo_root, now)


def note_work_resumed(buf: dict, repo_root: Path, now: str) -> None:
    """The session works again after a turn end: anything changed meanwhile was not this agent.

    Reached only through ``note_prompt`` / ``observe``, which keep observation
    from ever raising into the hook path.
    """
    state = _state(buf)
    state["turn_open"] = True
    snapshot = _take_turn_end_snapshot(buf, repo_root)
    if snapshot is None:
        return
    edit = _manual_edit(buf, repo_root, snapshot, now)
    if edit is not None and _add(buf, edit):
        _activity_event(buf, f"files changed between turns: {edit['files']}",
                        {"steering_kind": KIND_MANUAL_EDIT, "files": edit["files"],
                         "lines_added": edit.get("lines_added"), "lines_removed": edit.get("lines_removed")},
                        now, "git_observed")


def note_model_invocation(buf: dict, repo_root: Path, now: str) -> None:
    """A model call (Antigravity): the first after a turn end means the session works again."""
    state = _state(buf)
    if state.get("turn_open") is False and isinstance(state.get("last_turn_end_at"), str):
        note_work_resumed(buf, repo_root, now)
    else:
        state["turn_open"] = True


def note_turn_end(buf: dict, repo_root: Path, now: str, *, how: str) -> None:
    """A turn ended (``stop``, ``interrupt`` or ``idle``): remember when, and the tree as it was."""
    state = _state(buf)
    state["turn_open"] = False
    state["last_turn_end_at"] = now
    state["last_turn_failed_check"] = _last_check_failed(buf)
    state["turn_ends"] = (state["turn_ends"] + [{"at": now, "how": how}])[-_MAX_TURN_ENDS:]
    if buf.get("record"):
        try:
            _write_turn_end_snapshot(buf, repo_root, now)
        except OSError:
            pass  # no snapshot: the next turn simply records no manual edit


def note_interrupt(buf: dict, now: str, *, source: str = SOURCE_HOOK, after: str = "unknown") -> bool:
    """The person stopped a turn. False when the same interrupt was already staged."""
    state = _state(buf)
    for seen in state["events"]:
        if seen.get("kind") == KIND_INTERRUPT:
            gap = _seconds(seen.get("at"), now)
            if gap is not None and abs(gap) <= _DUPLICATE_SECONDS:
                if source == SOURCE_TRANSCRIPT and seen.get("source") == SOURCE_HOOK:
                    seen["at"] = now  # same Esc: the log has the time to the millisecond, the hook to the second
                    state["events"].sort(key=_order)
                return False  # one Esc, seen by the hook and again in the log
    index = _turn_index_at(buf, now) if source == SOURCE_TRANSCRIPT else _turn_index(buf)
    event = {"at": now, "turn_index": index, "kind": KIND_INTERRUPT, "after": after, "source": source}
    added = _add(buf, event)
    if added and source == SOURCE_TRANSCRIPT:
        # The hook's own Interrupt Event is this fact's timeline entry; an Esc only the log saw gets one here.
        _activity_event(buf, "assistant turn interrupted by user", {"steering_kind": KIND_INTERRUPT}, now,
                        "agent_reported")
    return added


def person_decision(choice: object) -> bool:
    """True for a Hermes approval choice a person made (smart-mode choices are a model's)."""
    return isinstance(choice, str) and choice in _HERMES_DECISIONS


def note_permission(buf: dict, choice: object, now: str) -> None:
    """A person's answer to the agent's approval prompt (Hermes); a smart-mode or missing answer is no one's."""
    decision = _HERMES_DECISIONS.get(choice) if isinstance(choice, str) else None
    if decision is None:
        return
    _add(buf, {"at": now, "turn_index": _turn_index(buf), "kind": KIND_PERMISSION, "decision": decision,
               "after": "permission_prompt", "source": SOURCE_HOOK})


def _last_check_failed(buf: Mapping[str, Any]) -> bool:
    latest = buf.get("check_latest")
    if not isinstance(latest, dict) or not latest:
        return False
    last = list(latest.values())[-1]
    status = last.get("status") if isinstance(last, dict) else last
    return status == "failed"


# ---------------------------------------------------------------------------
# Segments: a Claude Code turn Receipt / resumed session continues the session
# ---------------------------------------------------------------------------


def inherit(buf: dict, persisted: Mapping[str, Any]) -> None:
    """Carry what the next segment needs from the previous Receipt of the same agent session."""
    capture = persisted.get("capture")
    if not isinstance(capture, dict):
        return
    state = _state(buf)
    if int(capture.get("prompt_count") or 0) > 0:
        state["prompts_before"] = True
    previous = capture.get("steering")
    if isinstance(previous, dict):
        end = previous.get("last_turn_end_at")
        if isinstance(end, str) and capture.get("session_end_observed") is not True:
            state["last_turn_end_at"] = end
            state["turn_open"] = False
            state["last_turn_failed_check"] = previous.get("last_turn_failed_check") is True


def restore(buf: dict, persisted: Mapping[str, Any]) -> None:
    """Rebuild the buffer's steering state from a persisted Receipt (a lost staging buffer)."""
    capture = persisted.get("capture")
    stored = capture.get("steering") if isinstance(capture, dict) else None
    if not isinstance(stored, dict):
        return
    state = _state(buf)
    state["events"] = [dict(e) for e in stored.get("events") or [] if isinstance(e, dict)][:_MAX_EVENTS]
    state["turn_ends"] = [dict(t) for t in stored.get("turn_ends") or [] if isinstance(t, dict)][:_MAX_TURN_ENDS]
    state["dropped"] = int(stored.get("events_dropped") or 0)
    for key in ("last_turn_end_at",):
        if isinstance(stored.get(key), str):
            state[key] = stored[key]
    state["last_turn_failed_check"] = stored.get("last_turn_failed_check") is True
    openers = [e for e in state["events"] if e.get("kind") in (KIND_INITIAL, KIND_FOLLOW_UP)]
    end = _parse(state.get("last_turn_end_at"))
    state["turn_open"] = bool(openers) and (end is None or _order(openers[-1]) > end)


def forget(buf: Mapping[str, Any], repo_root: Path) -> None:
    """The session ended: drop its turn-end snapshot, and the folder once no session needs one. Never raises."""
    path = _snapshot_path(buf, repo_root)
    try:
        path.unlink()
    except OSError:
        pass
    try:
        path.parent.rmdir()  # only when empty: another live session's snapshot keeps it
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Manual edits (git): the tree at a turn's end vs. when work resumes
# ---------------------------------------------------------------------------


def _snapshot_path(buf: Mapping[str, Any], repo_root: Path) -> Path:
    from openshard.adapters.claude_hooks import sessions_dir

    # Hashed: agent session ids may hold characters a Windows file name cannot.
    key = hashlib.sha256(f"{_agent(buf)}\0{buf.get('session_id')}".encode()).hexdigest()[:24]
    return sessions_dir(repo_root) / "steering" / f"{key}.json"


def _write_turn_end_snapshot(buf: dict, repo_root: Path, now: str) -> None:
    from openshard.adapters.claude_hooks import _snapshot_baseline, _snapshot_start_head

    tree = _snapshot_baseline(repo_root, now)
    if tree.get("source") != "git_status":
        return
    blob = {"at": now, "head": _snapshot_start_head(repo_root), "paths": tree.get("paths") or {},
            "truncated": bool(tree.get("truncated"))}
    path = _snapshot_path(buf, repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(blob), encoding="utf-8")
    os.replace(tmp, path)
    _prune_snapshots(path.parent)


_SNAPSHOT_MAX_AGE_SECONDS = 7 * 24 * 3600


def _prune_snapshots(directory: Path) -> None:
    """Drop snapshots of sessions that never came back or never sent an end (bounded, best effort)."""
    import time

    cutoff = time.time() - _SNAPSHOT_MAX_AGE_SECONDS
    try:
        for old in sorted(directory.glob("*.json"))[:200]:
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _take_turn_end_snapshot(buf: dict, repo_root: Path) -> dict | None:
    path = _snapshot_path(buf, repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return data if isinstance(data, dict) and isinstance(data.get("paths"), dict) else None


def _tree_blobs(repo_root: Path, head: str | None, paths: list[str]) -> dict[str, str | None] | None:
    """``path -> blob`` in commit *head* (None: not in it); None when git cannot answer."""
    from openshard.util.git import run_git

    if not paths:
        return {}
    if not head:
        return None
    text = run_git(repo_root, ["ls-tree", "-z", head, "--", *paths])
    if text is None:
        return None
    out: dict[str, str | None] = dict.fromkeys(paths)
    for item in text.split("\0"):
        meta, _, name = item.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and name in out:
            out[name] = parts[2]
    return out


def _manual_edit(buf: dict, repo_root: Path, before: dict, now: str) -> dict | None:
    from openshard.adapters.claude_hooks import (
        _other_sessions_reported_paths,
        _snapshot_baseline,
        _snapshot_start_head,
    )
    from openshard.util.git import run_git

    current = _snapshot_baseline(repo_root, now)
    if current.get("source") != "git_status":
        return None
    head_before = before.get("head") if isinstance(before.get("head"), str) else None
    head_now = _snapshot_start_head(repo_root)
    dirty_before: dict = before["paths"]
    dirty_now: dict = current.get("paths") or {}
    candidates = set(dirty_before) | set(dirty_now)
    if head_before != head_now:
        if not head_before or not head_now:
            return None  # cannot compare across an unknown HEAD
        names = run_git(repo_root, ["diff", "--name-only", "-z", head_before, head_now])
        if names is None:
            return None
        candidates |= {n for n in names.split("\0") if n}
    candidates -= _other_sessions_reported_paths(repo_root, buf)  # another live agent session said it made these
    ordered = sorted(c for c in candidates if isinstance(c, str))[:_MAX_CANDIDATES]
    truncated = len(candidates) > _MAX_CANDIDATES or bool(before.get("truncated")) or bool(current.get("truncated"))
    in_before = _tree_blobs(repo_root, head_before, [p for p in ordered if p not in dirty_before])
    in_now = _tree_blobs(repo_root, head_now, [p for p in ordered if p not in dirty_now])
    if in_before is None or in_now is None:
        return None
    changed: list[str] = []
    unknown = 0
    for path in ordered:
        old = dirty_before[path] if path in dirty_before else in_before.get(path)
        new = dirty_now[path] if path in dirty_now else in_now.get(path)
        if old == "?" or new == "?":
            unknown += 1  # a blob id could not be computed: cannot prove a change either way
        elif old != new:
            changed.append(path)
    if not changed:
        return None
    added, removed, lines_unknown = _line_counts(repo_root, head_before, changed, dirty_before)
    from openshard.safety.sanitize import sanitize_path

    event: dict[str, Any] = {
        "at": now, "turn_index": _turn_index(buf, opening=True), "kind": KIND_MANUAL_EDIT, "after": "turn_end",
        "source": SOURCE_GIT, "files": len(changed),
        "lines_added": added if lines_unknown < len(changed) else None,
        "lines_removed": removed if lines_unknown < len(changed) else None,
        "paths": [p for p in (sanitize_path(c, 200) for c in changed[:_MAX_PATHS]) if p],
        "since": before.get("at") if isinstance(before.get("at"), str) else None,
    }
    if lines_unknown:
        event["lines_unknown_files"] = lines_unknown
    if truncated or unknown:
        event["incomplete"] = True
    return event


def _line_counts(repo_root: Path, head: str | None, paths: list[str], dirty_before: Mapping[str, Any]) -> tuple[int, int, int]:
    """+/- lines of *paths* since the turn end, where git can tell: ``(added, removed, unknown_files)``.

    Exact for a file that matched the turn-end HEAD then (``git diff --numstat
    <head> -- path`` against the working tree; an untracked new file counts its
    lines). A file the agent had already left modified at the turn end has no
    earlier content OpenShard kept, so its lines are unknown, never guessed.
    """
    from openshard.util.git import run_git

    known = [p for p in paths if p not in dirty_before]
    unknown = len(paths) - len(known)
    if not known or not head:
        return 0, 0, len(paths)
    added = removed = 0
    seen: set[str] = set()
    text = run_git(repo_root, ["diff", "--numstat", "-z", head, "--", *known])
    if text is None:
        return 0, 0, len(paths)
    for item in text.split("\0"):
        parts = item.split("\t")
        if len(parts) == 3 and parts[2] in known:
            if parts[0] == "-" or parts[1] == "-":
                unknown += 1  # binary
            else:
                added += int(parts[0])
                removed += int(parts[1])
            seen.add(parts[2])
    for path in known:
        if path in seen:
            continue
        file = repo_root / path
        try:
            if file.is_file() and file.stat().st_size <= 1_000_000:
                data = file.read_bytes()
                if b"\0" in data:
                    unknown += 1
                else:
                    added += data.count(b"\n") + (0 if not data or data.endswith(b"\n") else 1)
            elif file.exists():
                unknown += 1
        except OSError:
            unknown += 1
    return added, removed, unknown


# ---------------------------------------------------------------------------
# Fold time: each agent's own session log
# ---------------------------------------------------------------------------


def scan_logs(buf: dict) -> None:
    """Read new lines of the session's own log (Claude Code / Codex) for log-only signals. Never raises."""
    try:
        agent = _agent(buf)
        if agent not in (AGENT_CLAUDE_CODE, AGENT_CODEX):
            return
        from openshard.adapters.claude_transcript_usage import _valid_transcript_path

        raw = _valid_transcript_path(buf.get("transcript_path"), buf.get("session_id"), agent=agent)
        if raw is None:
            return
        _scan(buf, Path(raw), agent)
    except Exception:
        pass


def _scan(buf: dict, path: Path, agent: str) -> None:
    state = _state(buf)
    raw_log = state.get("log")
    log: dict = raw_log if isinstance(raw_log, dict) else {}
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size > _LOG_MAX_BYTES:
        state["log"] = {"path": str(path), "offset": 0, "too_large": True}
        return
    if log.get("path") == str(path) and log.get("foreign"):
        return  # this file proved to be another session's: never read again
    same = log.get("path") == str(path) and isinstance(log.get("offset"), int)
    offset = log["offset"] if same and log["offset"] <= size else 0
    if offset == 0:
        log = {}  # a new or rewritten file: its task state starts again too
    log = {"path": str(path), "offset": offset, **{k: v for k, v in log.items() if k in ("task",)}}
    window = _parse(buf.get("started_at"))
    with path.open("rb") as stream:
        stream.seek(offset)
        while True:
            chunk = stream.read(_LOG_READ_BYTES)  # bounded memory; the whole new part is read
            end = chunk.rfind(b"\n")
            if end < 0:
                if len(chunk) == _LOG_READ_BYTES:  # one line longer than a chunk: skip past it
                    log["offset"] += len(chunk)
                    continue
                break
            for raw in chunk[: end + 1].splitlines():
                if not raw or len(raw) > _LOG_MAX_LINE:
                    continue
                if agent == AGENT_CLAUDE_CODE:
                    _claude_line(buf, raw, window)
                elif not _codex_line(buf, raw, window, log):
                    state["log"] = {"path": str(path), "offset": 0, "foreign": True}
                    return
            log["offset"] += end + 1
            stream.seek(log["offset"])
    state["log"] = log
    _mark_from_queue(buf)
    _settle_after_interrupt(buf)


def _in_window(stamp: object, window: datetime | None, *, slack: float = 0.0) -> str | None:
    at = _parse(stamp)
    if at is None:
        return None
    if window is not None and (at - window).total_seconds() < -slack:
        return None  # before this Receipt began: an earlier Receipt's
    return at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"  # the log's own precision (hooks: whole seconds)


def _claude_line(buf: dict, raw: bytes, window: datetime | None) -> None:
    if b'"queue-operation"' not in raw and b"Request interrupted by user" not in raw and _CLAUDE_REJECTION.encode() not in raw:
        return
    try:
        record = json.loads(raw)
    except (ValueError, RecursionError):
        return
    if not isinstance(record, dict) or record.get("isSidechain") is True:
        return
    # A dequeue may be stamped a moment before the prompt hook that opened this
    # Receipt (whole seconds); nothing else from before its start is this Receipt's.
    dequeue = record.get("type") == "queue-operation" and record.get("operation") == "dequeue"
    at = _in_window(record.get("timestamp"), window, slack=1.0 if dequeue else 0.0)
    if at is None:
        return
    if record.get("type") == "queue-operation":
        _claude_queue_operation(buf, record, at)
    elif record.get("type") == "user":
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for item in content if isinstance(content, list) else [{"type": "text", "text": content}]:
            if isinstance(item, dict):
                _claude_user_item(buf, item, at)


def _claude_queue_operation(buf: dict, record: Mapping[str, Any], at: str) -> None:
    """A message queued while Claude worked (an interjection), or one taken off the queue."""
    operation = record.get("operation")
    if operation == "enqueue":
        content = record.get("content")
        text = content if isinstance(content, str) else None
        if text is not None and text.lstrip().startswith(("<", "/")):
            return  # a slash command or harness plumbing, not a message to the agent
        _interjection(buf, at, text)
    elif operation == "dequeue":
        state = _state(buf)
        state["dequeues"] = (state["dequeues"] + [at])[-_MAX_DEQUEUES:]


def _claude_user_item(buf: dict, item: Mapping[str, Any], at: str) -> None:
    """A tool rejection by the person, or an interrupt marker; anything else is not read."""
    if item.get("type") == "tool_result":
        body = item.get("content")
        body_text = body if isinstance(body, str) else " ".join(
            c.get("text", "") for c in body if isinstance(c, dict)) if isinstance(body, list) else ""
        if isinstance(body_text, str) and body_text.startswith(_CLAUDE_REJECTION) and _add(
            buf, {"at": at, "turn_index": _turn_index_at(buf, at), "kind": KIND_PERMISSION,
                  "decision": "deny", "after": "permission_prompt", "source": SOURCE_TRANSCRIPT}):
            _activity_event(buf, "permission prompt answered: deny",
                            {"steering_kind": KIND_PERMISSION, "decision": "deny"}, at, "agent_reported")
        return
    text = item.get("text") if item.get("type") == "text" else None
    stripped = text.strip() if isinstance(text, str) else None
    if stripped == _CLAUDE_INTERRUPT_TOOL:
        if not _denied_just_before(buf, at):
            note_interrupt(buf, at, source=SOURCE_TRANSCRIPT, after="tool_call")
    elif stripped == _CLAUDE_INTERRUPT:
        note_interrupt(buf, at, source=SOURCE_TRANSCRIPT, after="unknown")


def _denied_just_before(buf: dict, at: str) -> bool:
    """An Esc at a permission prompt is recorded as the rejection, not as a second input."""
    for seen in _state(buf)["events"]:
        if seen.get("kind") == KIND_PERMISSION and seen.get("decision") == "deny":
            gap = _seconds(seen.get("at"), at)
            if gap is not None and 0 <= gap <= _DUPLICATE_SECONDS:
                return True
    return False


def _codex_line(buf: dict, raw: bytes, window: datetime | None, log: dict) -> bool:
    """One rollout line; False when the file proves to be another session's."""
    if b'"session_meta"' not in raw and not any(marker in raw for marker in _CODEX_MARKERS):
        return True
    try:
        record = json.loads(raw)
    except (ValueError, RecursionError):
        return True
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        return True
    if record.get("type") == "session_meta":
        sid = payload.get("id")
        if "task" not in log:
            log["task"] = {"open": False, "messages": 0}
        return not isinstance(sid, str) or sid == buf.get("session_id")
    if record.get("type") != "event_msg":
        return True
    task = log.setdefault("task", {"open": False, "messages": 0})
    kind = payload.get("type")
    if kind == "task_started":
        task.update(open=True, messages=0)
    elif kind in ("task_complete", "turn_aborted"):
        task.update(open=False, messages=0)
        if kind == "turn_aborted" and payload.get("reason") == "interrupted":
            at = _in_window(record.get("timestamp"), window)
            if at is not None:
                note_interrupt(buf, at, source=SOURCE_TRANSCRIPT, after="unknown")
    elif kind == "user_message":
        task["messages"] = int(task.get("messages") or 0) + 1
        if task.get("open") and task["messages"] > 1:
            at = _in_window(record.get("timestamp"), window)
            message = payload.get("message")
            if at is not None:
                _interjection(buf, at, message if isinstance(message, str) else None)
    return True


def _interjection(buf: dict, at: str, text: str | None) -> None:
    facts = prompt_facts(text)  # classified here, on this machine; the text goes no further
    event: dict[str, Any] = {"at": at, "turn_index": _turn_index_at(buf, at), "kind": KIND_INTERJECTION,
                             "after": _after_activity(buf, at), "source": SOURCE_TRANSCRIPT}
    if facts is not None:
        event.update(intent=facts["intent"], intent_confidence=facts["confidence"], intent_rule=facts["rule"],
                     prompt_chars=facts["chars"])
    else:
        event.update(intent="unknown", intent_confidence="low", intent_rule="no_prompt_text")
    if _add(buf, event):
        _activity_event(buf, "message sent while the agent was working",
                        {"steering_kind": KIND_INTERJECTION, "intent": event["intent"]}, at, "agent_reported")


def _after_activity(buf: Mapping[str, Any], at: str) -> str:
    """What the agent was last seen doing before *at* in this turn: a tool call, else unknown."""
    last = None
    moment = _parse(at)
    for event in buf.get("events") or []:
        when = _parse(event.get("occurred_at")) if isinstance(event, dict) else None
        if when is None or moment is None or when > moment:
            continue
        raw_meta = event.get("metadata")
        meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
        if meta.get("hook") == "UserPromptSubmit":
            last = None
        elif event.get("event_type") == "tool.invoked":
            last = "tool_call"
    return last or "unknown"


def _settle_after_interrupt(buf: dict) -> None:
    """A follow-up whose turn ended on an Esc the agent sent no hook for: the log's interrupt ended that turn."""
    state = _state(buf)
    events = state["events"]
    for i, event in enumerate(events):
        if event.get("kind") != KIND_FOLLOW_UP or event.get("after") != "unknown":
            continue
        stop = None
        for earlier in reversed(events[:i]):
            if earlier.get("kind") in (KIND_INITIAL, KIND_FOLLOW_UP):
                break
            if earlier.get("kind") == KIND_INTERRUPT:
                stop = earlier.get("at")
                break
        pause = _seconds(stop, event.get("at"))
        if stop is None or pause is None or pause < 0:
            continue
        event["after"] = "turn_end"
        event["think_seconds"] = round(min(pause, THINK_CAP_SECONDS), 1)
        if pause > THINK_CAP_SECONDS:
            event["think_capped"] = True
        if not any(t.get("at") == stop for t in state["turn_ends"]):
            state["turn_ends"] = sorted(state["turn_ends"] + [{"at": stop, "how": "interrupt"}],
                                        key=lambda t: str(t.get("at") or ""))[-_MAX_TURN_ENDS:]


def _mark_from_queue(buf: dict) -> None:
    """A follow-up delivered from the queue was typed (and counted) as an interjection already."""
    state = _state(buf)
    for event in state["events"]:
        if event.get("kind") != KIND_FOLLOW_UP or event.get("from_queue"):
            continue
        for dequeued in state["dequeues"]:
            gap = _seconds(dequeued, event.get("at"))
            if gap is not None and -1.0 <= gap <= _QUEUE_MATCH_SECONDS:
                event["from_queue"] = True
                break


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def not_available(agent: str) -> dict[str, str]:
    return {signal: why for signal, (how, why) in CAPABILITIES.get(agent, {}).items() if how == NOT_AVAILABLE}


def permission_counts_available(agent: str) -> tuple[bool, bool, bool]:
    return _PERMISSION_COUNTS.get(agent, (False, False, False))


def environment_facts(agent_host: object) -> dict[str, str]:
    """``environment`` (+ ``environment_source``) from where the agent ran, or why it is not known."""
    from openshard.adapters.agent_env import agent_host_or_none
    from openshard.history.steering import environment_from_host

    environment, reason = environment_from_host(agent_host_or_none(agent_host))
    if environment is None:
        return {"environment_not_recorded_reason": str(reason)}
    return {"environment": environment, "environment_source": "agent_env"}


def stamp(entry: dict, buf: dict) -> None:
    """``capture.steering`` on the Receipt being built: the staged facts, nothing derived from text."""
    try:
        scan_logs(buf)
        state = _state(buf)
        agent = _agent(buf)
        raw_approvals = buf.get("approvals")
        approvals: dict = raw_approvals if isinstance(raw_approvals, dict) else {}
        block: dict[str, Any] = {
            "version": VERSION,
            "intent_method": METHOD,
            "events": [dict(e) for e in state["events"]][:_MAX_EVENTS],
            "events_dropped": int(state.get("dropped") or 0),
            "turn_ends": [dict(t) for t in state["turn_ends"]][-_MAX_TURN_ENDS:],
            "not_available": not_available(agent),
        }
        if agent == AGENT_HERMES:
            block["permission_prompts_asked"] = int(approvals.get("requested") or 0)
        block.update(environment_facts(buf.get("agent_host")))
        for key in ("last_turn_end_at",):
            if isinstance(state.get(key), str):
                block[key] = state[key]
        if state.get("last_turn_failed_check"):
            block["last_turn_failed_check"] = True
        raw_log = state.get("log")
        log: dict = raw_log if isinstance(raw_log, dict) else {}
        if log.get("too_large"):
            block["log_not_read_reason"] = "log_too_large"
        elif log.get("foreign"):
            block["log_not_read_reason"] = "log_session_mismatch"
        entry.setdefault("capture", {})["steering"] = block
    except Exception:
        pass


__all__ = [
    "CAPABILITIES",
    "ENVIRONMENT_ROWS",
    "FROM_GIT",
    "FROM_GITHUB",
    "FROM_LOG",
    "LIVE_HOOK",
    "NOT_AVAILABLE",
    "SIGNALS",
    "SOURCE_GITHUB",
    "THINK_CAP_SECONDS",
    "environment_capability",
    "environment_facts",
    "inherit",
    "note_interrupt",
    "note_model_invocation",
    "note_permission",
    "note_prompt",
    "observe",
    "note_turn_end",
    "note_work_resumed",
    "not_available",
    "permission_counts_available",
    "person_decision",
    "prompt_facts",
    "restore",
    "forget",
    "scan_logs",
    "stamp",
    "valid_prompt_facts",
]
