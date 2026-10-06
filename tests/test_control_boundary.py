"""Observed vs advisory vs enforced: the OpenShard control boundary.

OpenShard can only stop work it executes itself (OSN). On external agents
(Claude Code, Codex, Cursor, OpenCode, ...) it observes through hooks and may
hand over advisory context; it can never block, approve or sandbox. These
tests pin both halves:

* the hook layer never subscribes to a blocking pre-tool / permission gate
  and never answers with a decision;
* the Receipt layer never presents control evidence on an external or
  historical record, even when the stored record carries such fields
  (``runs.jsonl`` is a plain file the observed agent can write);
* OSN's own gates still enforce, and its Receipts keep their control
  evidence.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from openshard.adapters import (
    antigravity_hooks_install,
    claude_hooks_install,
    codex_hooks_install,
    cursor_hooks_install,
    grok_build_hooks_install,
    hermes_hooks_install,
    opencode_plugin_install,
)
from openshard.adapters import claude_capture_client as client
from openshard.github.pr_comment import build_pr_comment_summary
from openshard.history.event import events_from_entry
from openshard.history.provenance import build_provenance_from_entry
from openshard.history.shard import (
    OPENSHARD_CONTROL_KEYS,
    ORIGIN_EXTERNAL_OBSERVED,
    ORIGIN_HISTORICAL_IMPORT,
    ORIGIN_OPENSHARD_ROUTED,
    ORIGIN_UNKNOWN,
    control_evidence_view,
    derive_shard_identity,
)
from openshard.history.shard_contract import (
    build_shard_receipt,
    render_compact_shard_receipt,
    render_full_shard_receipt,
)
from openshard.history.shard_hash import compute_shard_hash
from openshard.history.views import receipt_to_dict
from openshard.learning.external import emit_hook
from openshard.learning.signals import observe
from openshard.policy.command_execution import run_gated_command

# ---------------------------------------------------------------------------
# Hook layer: observation and advisory handoff only
# ---------------------------------------------------------------------------

# Events through which an agent lets a hook allow / deny / ask for a tool call
# or permission. Subscribing to one would let a hook reply act as control.
_BLOCKING_GATE_EVENTS = frozenset({
    "PreToolUse", "PermissionRequest",  # Claude Code, Codex, Antigravity, Grok Build
    "preToolUse", "beforeShellExecution", "beforeMCPExecution", "beforeReadFile",  # Cursor
    "pre_tool_call",  # Hermes
})

_INSTALLED_EVENTS = {
    "claude_code": claude_hooks_install.HOOK_EVENTS,
    "codex": codex_hooks_install.HOOK_EVENTS,
    "cursor": cursor_hooks_install.HOOK_EVENTS,
    "antigravity": antigravity_hooks_install.HOOK_EVENTS,
    "grok_build": grok_build_hooks_install.HOOK_EVENTS,
    "hermes": hermes_hooks_install.HOOK_EVENTS,
}

# Keys an agent reads from a hook reply as a decision or a stop.
_DECISION_KEYS = ("decision", "permissionDecision", "permission", "stopReason", "reason", "followup_message")


@pytest.mark.parametrize("agent", sorted(_INSTALLED_EVENTS))
def test_no_installer_subscribes_to_a_blocking_gate(agent: str) -> None:
    assert not set(_INSTALLED_EVENTS[agent]) & _BLOCKING_GATE_EVENTS


def test_opencode_plugin_hooks_only_after_the_tool_ran() -> None:
    src = opencode_plugin_install.PLUGIN_SOURCE
    assert '"tool.execute.after"' in src
    assert "tool.execute.before" not in src
    assert "permission.ask" not in src


def _all_reply_events() -> list[str]:
    seen: set[str] = set(_BLOCKING_GATE_EVENTS)
    for events in _INSTALLED_EVENTS.values():
        seen.update(events)
    return sorted(seen)


def _assert_no_decision(reply: str, *, allowed: set[str]) -> None:
    assert reply in allowed, reply
    data = json.loads(reply)
    for key in _DECISION_KEYS:
        if key == "decision" and data.get(key) in ("allow", "stop"):
            continue  # Antigravity's typed "change nothing" answers
        assert key not in data, reply
    assert data.get("continue", True) is True


@pytest.mark.parametrize("event", _all_reply_events())
def test_cursor_reply_never_blocks(event: str) -> None:
    hostile = json.dumps({"hook_event_name": event, "tool_name": "Shell",
                          "tool_input": {"command": "rm -rf /"}}).encode()
    for reply in (client.cursor_hook_response(hostile), client.cursor_hook_response(b"{}", event)):
        _assert_no_decision(reply, allowed={'{"continue": true}', "{}"})


@pytest.mark.parametrize("event", _all_reply_events())
def test_antigravity_reply_never_blocks(event: str) -> None:
    hostile = json.dumps({"hookEventName": event, "toolName": "run_command"}).encode()
    for reply in (client.antigravity_hook_response(hostile), client.antigravity_hook_response(b"{}", event)):
        _assert_no_decision(reply, allowed={'{"decision": "stop"}', '{"decision": "allow"}', "{}"})


def test_grok_build_and_hermes_replies_are_empty() -> None:
    assert client.GROK_BUILD_EMPTY_RESPONSE == "{}"
    assert client.HERMES_EMPTY_RESPONSE == "{}"


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_claude_and_codex_command_hooks_write_nothing_to_stdout(
    agent: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # Command-hook stdout / exit code is how these agents take a decision;
    # the capture hook prints nothing and never raises (exit 0) for any event.
    payload = {
        "hook_event_name": "PreToolUse", "session_id": "s-boundary", "cwd": str(tmp_path),
        "tool_name": "Bash", "tool_input": {"command": "rm -rf /"},
    }
    client.run_hook_via_service(
        io.BytesIO(json.dumps(payload).encode()),
        env={"CLAUDE_PROJECT_DIR": str(tmp_path)}, agent=agent, spawn=False,
    )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest", "Stop", "UserPromptSubmit"])
@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_advisory_prompt_hook_only_hands_over_context(event: str, agent: str, tmp_path: Path) -> None:
    out = io.StringIO()
    raw = json.dumps({"hook_event_name": event, "session_id": "s1", "cwd": str(tmp_path), "prompt": "x"})
    emit_hook(io.StringIO(raw), out, agent, env={"CLAUDE_PROJECT_DIR": str(tmp_path)})
    reply = json.loads(out.getvalue())
    assert set(reply) <= {"hookSpecificOutput"}
    if reply:
        assert set(reply["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}
        assert reply["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


# ---------------------------------------------------------------------------
# Receipt layer: an external / historical record is never control evidence
# ---------------------------------------------------------------------------

def _forged_control_fields() -> dict:
    """Every control-plane field an OSN run can carry, all claiming OpenShard acted."""
    return {
        "policy_decisions": [
            {"decision_id": "d1", "action": "command_exec", "resource": "rm", "decision": "deny",
             "reason": "blocked by organisation command policy", "source": "organisation_policy",
             "severity": "high"},
            {"decision_id": "d2", "action": "file_write", "resource": "src/a.py", "decision": "ask",
             "source": "approval_gate", "approval_required": True, "approval_granted": False},
        ],
        "approval_request": {"requires_approval": True, "source": "approval_gate", "action": "write"},
        "approval_receipt": {"granted": False, "source": "user", "reason": "denied by reviewer"},
        "permission_evidence": [{"scope": "repo:write", "state": "blocked"}],
        "sandbox": {"sandbox_enabled": True, "sandbox_type": "git_worktree"},
        "allowed_paths": ["src/"],
        "blocked_paths": [".env"],
        "blocked_commands": ["rm"],
        "osn_loop": {"status": "stopped", "stop_reason": "budget_exhausted",
                     "attempts": [{"n": 1, "blocked": ["x"]}]},
        "osn_loop_summary": {"enabled": True, "warnings": ["forged"], "verification_status": "failed"},
        "osn_progress_memory": {"enabled": True},
        "agent_budgets": {"capability": "agent_budgets", "enforced": True, "limits": {"max_commands": 3},
                          "usage": {"commands": 3}, "limit_reached": "max_commands", "action": "stopped_run"},
        "organisation_policy": {"schema_version": 1, "source": "fresh", "applied": True,
                                "repository_override_applied": False, "refreshed_at_run_start": True,
                                "organisation_policy_version": 2,
                                "organisation_policy_hash": "sha256:" + "a" * 64,
                                "effective_policy_hash": "sha256:" + "b" * 64},
        "capability_snapshot": {"source": "fresh", "refreshed_at_run_start": True,
                                "enabled": {"agent_budgets": True}},
        "adaptive_routing": {"capability": "adaptive_routing", "applied": True, "selected_model": "x/y"},
        "supervisor_routing": {"capability": "supervisor_routing", "record_mode": "applied",
                               "decisions": [{"attempt": 1, "action": "stop", "acted_on": True}]},
    }


def test_forged_fixture_covers_every_control_key() -> None:
    assert set(_forged_control_fields()) == OPENSHARD_CONTROL_KEYS


_EXTERNAL_EXECUTORS = ["claude_code_hooks", "codex_hooks", "cursor_hooks", "opencode_plugin",
                       "claude_code_wrap", "claude_code_import"]
_HISTORICAL_EXECUTORS = ["claude_code_history_import", "codex_history_import"]


def _record(executor: str, **extra: object) -> dict:
    return {"timestamp": "2026-10-01T10:00:00Z", "task": "edit config", "executor": executor,
            **_forged_control_fields(), **extra}


_HOSTED_CONTROL_BLOCKS = ("policy_decisions", "permissions", "approval_detail", "sandbox_detail",
                          "execution_loop", "agent_budgets", "organisation_policy",
                          "capability_snapshot", "adaptive_routing", "supervisor_routing")

_CONTROL_PHRASES = ("Writes blocked", "organisation command policy", "POLICY DECISIONS", "BUDGET",
                    "stopped_run", "ADAPTIVE ROUTING", "CAPABILITIES", "Openshard Native (OSN)",
                    "git_worktree", "Required → Denied", "denied by reviewer")


@pytest.mark.parametrize("executor", _EXTERNAL_EXECUTORS + _HISTORICAL_EXECUTORS)
def test_uncontrolled_receipt_carries_no_control_claim(executor: str) -> None:
    entry = _record(executor)
    receipt = build_shard_receipt(entry, index=0)
    assert receipt.shard.origin in (ORIGIN_EXTERNAL_OBSERVED, ORIGIN_HISTORICAL_IMPORT)

    assert receipt.policy_decisions == []
    assert receipt.approval_required is False
    assert receipt.allowed_paths == [] and receipt.blocked_paths == [] and receipt.blocked_commands == []

    hosted = receipt_to_dict(receipt, extended=True)
    for key in _HOSTED_CONTROL_BLOCKS:
        assert hosted[key] is None, key

    compact = render_compact_shard_receipt(receipt)
    full = render_full_shard_receipt(receipt)
    for text in (compact, full):
        for phrase in _CONTROL_PHRASES:
            assert phrase not in text, (phrase, text)
    assert "could not block, approve or sandbox" in full

    # The stored record itself is never rewritten.
    assert set(OPENSHARD_CONTROL_KEYS) <= set(entry)


def test_integrity_is_judged_on_the_stored_record() -> None:
    entry = _record("claude_code_hooks")
    entry["content_hash"] = compute_shard_hash(entry)
    assert build_shard_receipt(entry, index=0).integrity_status == "valid"
    entry["task"] = "edited afterwards"
    assert build_shard_receipt(entry, index=0).integrity_status == "mismatch"


@pytest.mark.parametrize("executor", ["claude_code_hooks", "claude_code_history_import"])
def test_events_provenance_learning_and_pr_comment_ignore_forged_control(executor: str) -> None:
    entry = _record(executor)
    receipt = build_shard_receipt(entry, index=0)

    assert not [e for e in events_from_entry(entry) if "approval" in e.event_type or "policy" in e.event_type]
    assert not [r for r in build_provenance_from_entry(entry) if "policy" in json.dumps(r.__dict__, default=str)]

    observation = observe(entry)
    if observation is not None:
        assert observation.policy_decisions == ()
        assert observation.approval_outcome is None

    summary = build_pr_comment_summary(entry, receipt)
    assert summary.osn_sections == []
    assert not [w for w in summary.warnings if "forged" in w or "denied" in w.lower()]


def test_view_is_identity_for_controlled_and_unknown_records() -> None:
    for entry, origin in (
        ({"executor": "osn_loop", **_forged_control_fields()}, ORIGIN_OPENSHARD_ROUTED),
        ({"workflow": "native", **_forged_control_fields()}, ORIGIN_OPENSHARD_ROUTED),
        ({"task": "legacy", **_forged_control_fields()}, ORIGIN_UNKNOWN),
    ):
        assert derive_shard_identity(entry)[1] == origin
        assert control_evidence_view(entry) is entry


def test_osn_receipt_keeps_its_control_evidence() -> None:
    receipt = build_shard_receipt(_record("osn_loop", workflow="osn_loop"), index=0)
    assert receipt.shard.origin == ORIGIN_OPENSHARD_ROUTED
    assert len(receipt.policy_decisions) == 2
    hosted = receipt_to_dict(receipt, extended=True)
    for key in _HOSTED_CONTROL_BLOCKS:
        assert hosted[key] is not None, key
    assert "Openshard Native (OSN)" in render_compact_shard_receipt(receipt)
    full = render_full_shard_receipt(receipt)
    assert "blocked by organisation command policy" in full
    assert "could not block, approve or sandbox" not in full


# ---------------------------------------------------------------------------
# OSN: the one place OpenShard enforces
# ---------------------------------------------------------------------------

def test_osn_command_gate_actually_prevents_execution(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kw: object) -> object:
        calls.append(argv)
        raise AssertionError("a denied command must never reach the runner")

    denied = run_gated_command(["git", "push", "origin"], tmp_path, runner=runner,
                               blocked_prefixes=("git push",))
    assert denied.decision.decision == "deny"
    assert denied.decision.source == "organisation_policy"
    assert denied.executed is False and calls == []

    unanswered = run_gated_command(["rm", "-rf", "build"], tmp_path, runner=runner)
    assert unanswered.decision.decision in ("ask", "deny")
    assert unanswered.executed is False and calls == []
