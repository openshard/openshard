"""Cloud hooks carry observed runtime metadata without a status line."""
import pytest

from openshard.adapters.claude_hooks import (
    ReducedHookPayload,
    extract_hook_payload,
    handle_claude_hook,
    reduce_hook_payload,
)
from openshard.sync.envelope import receipt_payload
from tests.capture_fixtures import _lines
from tests.test_task_context_capture import SID1, _claude_docs


def test_cloud_session_model_and_effective_effort(repo):
    docs = _claude_docs(repo, SID1)
    docs[0]["model"] = "claude-opus-5-5"
    for doc in docs:
        doc["effort"] = {"level": "xhigh"}
        outcome = handle_claude_hook(doc, env={"ANTHROPIC_MODEL": "requested-not-observed"})
        assert outcome.action != "error", outcome.detail
    entry = _lines(repo)[0]
    assert entry["capture"]["model_source"] == "claude_hook"
    assert "claude-opus-5-5" in str(entry)
    assert "requested-not-observed" not in str(entry)
    # The fixture's payloads carry Claude Code's permission mode too; it is
    # the agent's own report, recorded next to the effort level, and the
    # hosted projection sends the same block (Platform contract, see
    # tests/fixtures/platform/receipt-sync-envelope.v1.json).
    from openshard.history.receipt_evidence import runtime_configuration_block

    block = {
        "effort": "xhigh", "permission_mode": "default", "permission_modes_seen": ["default"],
        "source": "claude_hook", "evidence": "agent_reported"}
    assert runtime_configuration_block(entry) == block
    assert receipt_payload(entry, 1)["runtime_configuration"] == block


def test_model_switch_is_observed_and_queue_preserves_effort(repo):
    docs = _claude_docs(repo, SID1)
    for doc in docs[:2]:
        handle_claude_hook(doc, env={})
    switch = {"hook_event_name": "PostModelSwitch", "session_id": SID1, "cwd": str(repo),
              "to_model": "claude-opus-5-5", "requested_model": "opus", "effort": {"level": "max"}}
    payload = extract_hook_payload(switch)
    assert payload is not None
    reduced = reduce_hook_payload(payload, repo)
    assert reduced is not None
    restored = ReducedHookPayload.from_dict(reduced.to_dict())
    assert restored is not None and restored.effort_level == "max"
    handle_claude_hook(switch, env={})
    for doc in docs[2:]:
        handle_claude_hook(doc, env={})
    assert _lines(repo)[0]["capture"]["model_source"] == "claude_hook"
    assert _lines(repo)[0]["capture"]["effort_level"] == "max"


@pytest.mark.parametrize("level", ["auto", "secret-value", {}, None, 42])
def test_requested_or_invalid_configuration_never_becomes_evidence(repo, level):
    for doc in _claude_docs(repo, SID1):
        handle_claude_hook({**doc, "effort": {"level": level}}, env={
            "ANTHROPIC_MODEL": "claude-opus-5-5", "CLAUDE_CODE_EFFORT_LEVEL": "max"})
    entry = _lines(repo)[0]
    assert entry["capture"]["model_source"] == "not_captured"
    assert "effort_level" not in entry["capture"]
    # The block still carries the permission mode the fixture reports, locally
    # and hosted: the Platform contract needs effort or permission_mode.
    assert receipt_payload(entry, 1)["runtime_configuration"] == {
        "permission_mode": "default", "permission_modes_seen": ["default"],
        "source": "claude_hook", "evidence": "agent_reported"}
