"""Native protocol handoff tests; providers and cloud agents are never called."""
from __future__ import annotations

import io
import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.history.receipt_evidence import learning_block
from openshard.learning import external
from openshard.learning.external_install import configure
from tests.learning_fixtures import MOBILE_CHECK, osn_entry, publish_learning


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "shop"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    store = root / ".openshard"
    store.mkdir()
    entries = [osn_entry(repo="shop", check=MOBILE_CHECK, attempts=[("model/a", "failed")], days_ago=0) for _ in range(3)]
    (store / "runs.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    publish_learning(root)
    from openshard.learning import snapshot

    lookup = snapshot.lookup_snapshot
    monkeypatch.setattr(snapshot, "lookup_snapshot", lambda *a, **kw: lookup(*a, **{**kw, "budget_ms": 5000}))
    return root


def payload(root, **patch):
    return {"hook_event_name": "UserPromptSubmit", "session_id": "session-1", "cwd": str(root), "prompt": "Fix responsive dashboard layout", **patch}


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_prompt_handoff_and_capture_provenance(repo, agent):
    output = io.StringIO()
    external.emit_hook(io.StringIO(json.dumps(payload(repo))), output, agent, env={})
    response = json.loads(output.getvalue())
    assert response["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "advisory" in response["hookSpecificOutput"]["additionalContext"]
    assert "decision" not in response and "continue" not in response
    learning = external.captured_learning(repo, external.AGENTS[agent], "session-1", "2026-01-01T00:00:00Z")
    assert learning["context_delivery"] == "hook_response_emitted"
    assert learning["context_supplied"] is False
    assert learning["routing"]["influenced"] is False
    assert learning["verification"]["influenced"] is False
    assert learning_block({"learning": learning})["context_delivery"] == "hook_response_emitted"
    assert "Fix responsive dashboard layout" not in external.delivery_path(repo, external.AGENTS[agent], "session-1").read_text()
    # A new capture segment cannot adopt the earlier handoff.
    future = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    assert external.captured_learning(repo, external.AGENTS[agent], "session-1", future) is None
    other = "codex" if agent == "claude" else "claude_code"
    assert external.captured_learning(repo, other, "session-1", "2026-01-01T00:00:00Z") is None


def test_claude_receipt_includes_emission_but_keeps_capture_output_silent(repo):
    env = {"CLAUDE_PROJECT_DIR": str(repo)}
    handle_claude_hook(payload(repo, hook_event_name="SessionStart", prompt=None), env=env)
    external.emit_hook(io.StringIO(json.dumps(payload(repo))), io.StringIO(), "claude", env=env)
    handle_claude_hook(payload(repo), env=env)
    handle_claude_hook(payload(repo, hook_event_name="SessionEnd", prompt=None), env=env)
    entries = [json.loads(line) for line in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()]
    captured = [e for e in entries if e.get("capture", {}).get("session_id") == "session-1"]
    assert len(captured) == 1
    assert captured[0]["learning"]["context_delivery"] == "hook_response_emitted"
    assert captured[0]["verification_passed"] is None


@pytest.mark.parametrize("raw", ["bad json", "x" * (external.MAX_INPUT + 1), "[]"])
def test_bad_inputs_fail_open_without_provenance(raw, tmp_path):
    output = io.StringIO()
    external.emit_hook(io.StringIO(raw), output, "claude", env={})
    assert json.loads(output.getvalue()) == {}
    assert not list(tmp_path.rglob("learning-deliveries"))


def test_only_prompt_event_and_valid_session_can_emit(repo):
    for patch in [{"hook_event_name": "SessionStart"}, {"session_id": "../../escape"}, {"prompt": ""}]:
        assert external.prepare_hook(payload(repo, **patch), "claude", env={}) == ({}, None, None)


def test_broken_stdout_never_records_a_handoff(repo):
    class Broken:
        def write(self, value):
            raise OSError("broken pipe")

    external.emit_hook(io.StringIO(json.dumps(payload(repo))), Broken(), "claude", env={})
    assert not external.delivery_path(repo, "claude_code", "session-1").exists()


def test_unavailable_snapshot_reports_unknown_and_emits_no_context(repo):
    from openshard.learning.snapshot import snapshot_path

    snapshot_path(repo / ".openshard").unlink()
    response, path, stored = external.prepare_hook(payload(repo), "claude", env={})
    assert response == {}
    assert stored["learning"]["status"] == "unavailable"
    assert stored["learning"]["signals_considered"] is None
    assert stored["learning"]["context_delivery"] == "not_emitted"


def test_agent_reported_history_cannot_become_advisory_evidence(repo):
    entries = [osn_entry(repo="shop", check=MOBILE_CHECK, source="agent_reported", attempts=[("model/a", "failed")], days_ago=0) for _ in range(4)]
    # An external agent's claimed failed check, without OpenShard's own
    # independently observed OSN stop reason.
    for entry in entries:
        entry.pop("osn_loop")
        entry["executor"] = "claude_code_hooks"
        entry["workflow"] = "claude_code_hook"
        entry["routing_provenance"]["context"]["harness"] = "claude_code"
    (repo / ".openshard" / "runs.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    publish_learning(repo)
    response, _, stored = external.prepare_hook(payload(repo), "claude", env={})
    assert response == {}
    assert stored["learning"]["used"] is False


def test_context_size_is_bounded_before_emission(repo, monkeypatch):
    from openshard.learning.retrieval import LearningContext

    monkeypatch.setattr(LearningContext, "prompt_text", property(lambda self: "x" * (external.MAX_CONTEXT + 1)))
    response, _, stored = external.prepare_hook(payload(repo), "claude", env={})
    assert response == {}
    assert stored["learning"]["status"] == "unavailable"
    assert stored["learning"]["used"] is False


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_install_is_opt_in_idempotent_and_preserves_native_settings(repo, agent):
    from openshard.learning.external_install import PATHS

    path = repo / PATHS[agent]
    path.parent.mkdir(parents=True, exist_ok=True)
    original = {"permissions": {"allow": ["Read"]}, "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-hook"}]}]}}
    path.write_text(json.dumps(original))
    assert configure(repo, agent)["change"] == "installed"
    content = path.read_text()
    assert configure(repo, agent)["change"] == "unchanged"
    assert path.read_text() == content
    assert json.loads(content)["permissions"] == original["permissions"]
    assert configure(repo, agent, remove=True)["change"] == "removed"
    assert json.loads(path.read_text()) == original


def test_malformed_settings_are_not_overwritten(repo):
    path = repo / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_text('{"hooks":{"UserPromptSubmit":"bad"}}')
    before = path.read_text()
    with pytest.raises(ValueError):
        configure(repo, "claude")
    assert path.read_text() == before
