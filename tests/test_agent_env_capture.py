"""Claude Code provider and surface, read from the environment Claude Code gives its hooks.

``capture.provider`` comes from Claude Code's own provider selection
(``CLAUDE_CODE_USE_BEDROCK`` / ``_VERTEX`` / ``_FOUNDRY``, else the Anthropic
API unless ``ANTHROPIC_BASE_URL`` routes to a gateway OpenShard cannot name)
with ``provider_source: agent_env``; ``capture.surface`` is the raw
``CLAUDE_CODE_ENTRYPOINT``. The capture service does not share Claude's
environment, so the command client derives both and forwards them on
``X-OpenShard-Agent-Env`` -- never inside the agent's payload.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_capture_service as svc
from openshard.adapters import claude_hooks as ch
from openshard.adapters.agent_env import claude_agent_env, format_agent_env, parse_agent_env
from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.views import receipt_to_dict
from tests.capture_fixtures import SID, _lines, _make_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "my repo")


def _docs(repo: Path, sid: str = SID) -> list[dict]:
    base = {"session_id": sid, "cwd": str(repo)}
    return [
        {**base, "hook_event_name": "SessionStart", "source": "startup"},
        {**base, "hook_event_name": "UserPromptSubmit", "prompt": "Fix the login button"},
        {**base, "hook_event_name": "Stop"},
    ]


class TestDerivation:
    @pytest.mark.parametrize(("env", "expected"), [
        ({}, {"provider": "anthropic"}),
        ({"CLAUDE_CODE_USE_BEDROCK": "1"}, {"provider": "amazon_bedrock"}),
        ({"CLAUDE_CODE_USE_VERTEX": "true"}, {"provider": "google_vertex"}),
        ({"CLAUDE_CODE_USE_FOUNDRY": "1"}, {"provider": "microsoft_foundry"}),
        ({"CLAUDE_CODE_USE_BEDROCK": "0"}, {"provider": "anthropic"}),
        # A custom gateway: the provider behind it is unknown, never guessed.
        ({"ANTHROPIC_BASE_URL": "https://gateway.example.com"}, {}),
        # Conflicting selections are unknown, not a pick of one.
        ({"CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1"}, {}),
        ({"CLAUDE_CODE_ENTRYPOINT": "claude-vscode"}, {"provider": "anthropic", "surface": "claude-vscode"}),
        ({"CLAUDE_CODE_ENTRYPOINT": "has spaces/and slashes"}, {"provider": "anthropic"}),
    ])
    def test_provider_and_surface(self, env, expected):
        assert claude_agent_env(env) == expected

    def test_header_round_trip_and_malformed_values_are_dropped(self):
        value = format_agent_env({"provider": "amazon_bedrock", "surface": "sdk-cli"})
        assert value == "provider=amazon_bedrock;surface=sdk-cli"
        assert parse_agent_env(value) == {"provider": "amazon_bedrock", "surface": "sdk-cli"}
        assert parse_agent_env("provider=openai;surface=a b") == {}
        assert parse_agent_env(None) == {}
        assert format_agent_env({}) is None


class TestInProcessCapture:
    def test_bedrock_session_records_provider_surface_and_source(self, repo: Path):
        env = {"CLAUDE_PROJECT_DIR": str(repo), "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}
        for doc in _docs(repo):
            handle_claude_hook(doc, env=env)
        (entry,) = _lines(repo)
        capture = entry["capture"]
        assert (capture["provider"], capture["provider_source"], capture["surface"]) == (
            "amazon_bedrock", "agent_env", "cli")
        projected = receipt_to_dict(build_shard_receipt(entry), extended=True)
        assert (projected["provider"], projected["surface"]) == ("amazon_bedrock", "cli")
        assert "CLAUDE_CODE_USE_BEDROCK" not in json.dumps(entry)

    def test_gateway_session_leaves_provider_unknown(self, repo: Path):
        env = {"CLAUDE_PROJECT_DIR": str(repo), "ANTHROPIC_BASE_URL": "https://gw.example.com"}
        for doc in _docs(repo):
            handle_claude_hook(doc, env=env)
        (entry,) = _lines(repo)
        assert entry["capture"]["provider"] is None
        assert "provider_source" not in entry["capture"] and "surface" not in entry["capture"]
        projected = receipt_to_dict(build_shard_receipt(entry), extended=True)
        assert projected["provider"] is None and projected["surface"] is None


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []

    def _request(method, port, path, body=None, headers=None, *, timeout=5.0):
        calls.append({"path": path, "body": body, "headers": dict(headers or {})})
        return 200, b"{}"

    monkeypatch.setattr(client, "_request", _request)
    return calls


class TestCaptureServicePath:
    def test_client_forwards_it_on_its_own_header_never_in_the_body(self, sent, tmp_path: Path):
        raw = json.dumps({"hook_event_name": "SessionStart", "session_id": SID, "source": "startup"}).encode()
        env = {"OPENSHARD_HOME": str(tmp_path / "home"), "CLAUDE_CODE_USE_VERTEX": "1",
               "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}
        assert client._run_hook_raw(raw, env, event_override=None, agent="claude_code", spawn=False) == "forwarded"
        (call,) = sent
        assert call["headers"][client.AGENT_ENV_HEADER] == "provider=google_vertex;surface=sdk-cli"
        assert call["body"] == raw

    def test_other_agents_send_no_agent_env(self, sent, tmp_path: Path):
        raw = b'{"hook_event_name": "Stop", "session_id": "s"}'
        env = {"OPENSHARD_HOME": str(tmp_path / "home"), "CLAUDE_CODE_USE_BEDROCK": "1"}
        client._run_hook_raw(raw, env, event_override=None, agent="codex", spawn=False)
        assert client.AGENT_ENV_HEADER not in sent[0]["headers"]

    def test_queued_session_start_binds_it_and_later_hooks_without_it_keep_it(self, repo: Path):
        recorder = svc.CaptureRecorder(instance_id="t")  # replay driven by hand
        start, prompt, stop = _docs(repo)
        # SessionStart is a command hook (client-derived env); the rest are
        # Claude Code HTTP hooks, which carry none.
        agent_env = {"provider": "microsoft_foundry", "surface": "claude-desktop"}
        assert recorder.record_hook(start, project_dir=str(repo), agent_env=agent_env)[0] == "queued"
        for doc in (prompt, stop):
            assert recorder.record_hook(doc, project_dir=str(repo))[0] == "queued"
        root = recorder.resolve_root(str(repo), None)
        assert root is not None
        queued = (ch.sessions_dir(root) / f"{svc.queue_key(SID)}{svc.QUEUE_SUFFIX}").read_text(encoding="utf-8")
        first = json.loads(queued.splitlines()[0])["data"]
        assert (first["agent_provider"], first["agent_surface"]) == ("microsoft_foundry", "claude-desktop")
        recorder._drain_session(root, svc.queue_key(SID))
        (entry,) = _lines(repo)
        assert entry["capture"]["provider"] == "microsoft_foundry"
        assert entry["capture"]["surface"] == "claude-desktop"

    def test_in_process_fallback_reads_the_hook_environment(self, repo: Path, tmp_path: Path):
        env = {"OPENSHARD_HOME": str(tmp_path / "home"), "OPENSHARD_CAPTURE_DISABLE": "1",
               "CLAUDE_PROJECT_DIR": str(repo), "CLAUDE_CODE_ENTRYPOINT": "cli"}
        for doc in _docs(repo):
            assert client.run_hook_via_service(io.BytesIO(json.dumps(doc).encode()), env=env) != "forwarded"
        (entry,) = _lines(repo)
        assert (entry["capture"]["provider"], entry["capture"]["surface"]) == ("anthropic", "cli")
