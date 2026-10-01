"""Connected capture: the normal link-once path for agent sessions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.connected import collector
from openshard.remote import config as remote_config
from openshard.remote import spool
from openshard.sync.config import API_KEY_ENV, ENDPOINT_ENV, ORG_ENV
from openshard.sync.transport import KIND_CREATED, SendResult
from tests.capture_fixtures import _git, _make_repo

ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
API_KEY = "osk_1234567890abcdefghijklmnop"
SID = "12121212-3434-4565-8787-909090909090"
SID_2 = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class FakeConnectedClient:
    def __init__(self) -> None:
        self.batches: list[dict] = []

    def send_events(self, batch: dict) -> SendResult:
        self.batches.append(json.loads(json.dumps(batch)))
        return SendResult(KIND_CREATED, 201)


@pytest.fixture
def linked_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("OPENSHARD_HOME", str(home))
    monkeypatch.setenv(ENDPOINT_ENV, "http://127.0.0.1:8787")
    monkeypatch.setenv(ORG_ENV, ORG)
    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    monkeypatch.setenv(collector.NO_SPAWN_ENV, "1")
    monkeypatch.delenv(remote_config.URL_ENV, raising=False)
    monkeypatch.delenv(remote_config.TOKEN_ENV, raising=False)
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = _make_repo(tmp_path / "workspace")
    _git(root, "remote", "add", "origin", "https://github.com/openshard/widget.git")
    return root


def _event(i: int) -> dict:
    return {
        "schema_version": 1,
        "event_id": f"evt-{i:04d}",
        "event_type": "tool.invoked",
        "occurred_at": f"2026-10-01T00:00:{i:02d}Z",
        "run_id": "run-1",
        "shard_id": "shard-20261001-0001",
        "attempt_number": 1,
        "actor": "claude_code_hooks",
        "source": "claude_code_hooks",
        "action": f"Bash: step {i}",
        "target": "python",
        "status": "passed",
        "evidence": "agent_reported",
        "metadata": {"hook": "PostToolUse", "tool": "Bash"},
        "raw_content_stored": False,
    }


def _scope_env(home: Path, session_id: str, surface: str = "claude-code") -> dict[str, str]:
    key = collector._session_key(surface, session_id)
    return {
        "OPENSHARD_HOME": str(home / "connected-captures" / key),
        ENDPOINT_ENV: "http://127.0.0.1:8787",
        ORG_ENV: ORG,
        API_KEY_ENV: API_KEY,
        collector.NO_SPAWN_ENV: "1",
    }


def _hook(repo: Path, event: str, **extra) -> None:
    handle_claude_hook(
        {
            "session_id": SID,
            "cwd": str(repo),
            "hook_event_name": event,
            **extra,
        },
        env={"CLAUDE_PROJECT_DIR": str(repo)},
    )


def test_linked_session_spools_without_manual_remote_attachment(
    linked_home: Path,
    repo: Path,
) -> None:
    assert remote_config.resolve_attachment() is None
    count = collector.record(
        repo,
        [_event(1), _event(2)],
        session_id=SID,
        agent="claude_code",
    )
    assert count == 2
    scoped = _scope_env(linked_home, SID)
    events, state = spool.pending(
        scoped,
        collector._capture_key("claude-code", SID),
    )
    assert [e["event_id"] for e in events] == ["evt-0001", "evt-0002"]
    assert state["surface"] == "claude-code"
    assert state["external_session_id"] == SID
    assert state["repos"] == [str(repo)]


def test_flush_uses_connected_source_and_acknowledges_only_after_send(
    linked_home: Path,
    repo: Path,
) -> None:
    collector.record(
        repo,
        [_event(1), _event(2)],
        session_id=SID,
        agent="claude_code",
    )
    scoped = _scope_env(linked_home, SID)
    client = FakeConnectedClient()
    report = collector.flush(scoped, client=client)
    assert report.events_sent == 2
    assert report.pending == 0
    assert client.batches[0]["source"]["product"] == "openshard-connected"
    assert client.batches[0]["collector_id"].startswith("col_")
    assert spool.pending_count(scoped) == 0


def test_concurrent_sessions_use_separate_durable_spools(
    linked_home: Path,
    repo: Path,
) -> None:
    collector.record(
        repo,
        [_event(1)],
        session_id=SID,
        agent="claude_code",
    )
    collector.record(
        repo,
        [_event(2)],
        session_id=SID_2,
        agent="claude_code",
    )
    first = _scope_env(linked_home, SID)
    second = _scope_env(linked_home, SID_2)
    assert spool.pending_count(first) == 1
    assert spool.pending_count(second) == 1
    assert Path(first["OPENSHARD_HOME"]) != Path(second["OPENSHARD_HOME"])


def test_real_hook_path_auto_spools_after_account_link(
    linked_home: Path,
    repo: Path,
) -> None:
    _hook(repo, "SessionStart", source="startup")
    _hook(repo, "UserPromptSubmit", prompt="fix the test")
    _hook(
        repo,
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
    )
    scoped = _scope_env(linked_home, SID)
    events, state = spool.pending(
        scoped,
        collector._capture_key("claude-code", SID),
    )
    assert [e["event_type"] for e in events] == [
        "session.started",
        "session.activity",
        "tool.invoked",
    ]
    assert state["links"][0]["receipt_id"].startswith("rcpt_")
    assert state["repos"] == [str(repo)]


def test_manual_remote_capture_wins_over_connected_fallback(
    linked_home: Path,
    repo: Path,
) -> None:
    capture_id = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    remote_config.save_attachment(
        capture_url=(
            "https://platform.example.test/v1/remote-captures/"
            + capture_id
        ),
        token="osr_AbCd1234_" + "t" * 43,
        organisation_id=ORG,
        agent="claude-code",
    )
    _hook(repo, "SessionStart", source="startup")
    events, state = spool.pending(None, capture_id)
    assert len(events) == 1
    assert state["capture_id"] == capture_id
    connected_root = linked_home / "connected-captures"
    assert not connected_root.exists()


def test_cloud_environment_is_visible_as_the_surface(
    linked_home: Path,
    repo: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    collector.record(
        repo,
        [_event(1)],
        session_id=SID,
        agent="claude_code",
    )
    scoped = _scope_env(linked_home, SID, "claude-code-web")
    state = spool.read_state(scoped)
    assert state is not None
    assert state["surface"] == "claude-code-web"


def test_later_verification_wakes_sessions_that_touched_the_repo(
    linked_home: Path,
    repo: Path,
) -> None:
    collector.record(
        repo,
        [_event(1)],
        session_id=SID,
        agent="claude_code",
    )
    scoped = _scope_env(linked_home, SID)
    assert (spool.read_state(scoped) or {}).get("deliver") is not True
    assert collector.request_delivery(repo_root=repo) is True
    assert (spool.read_state(scoped) or {})["deliver"] is True


def test_disconnected_runtime_is_a_noop(
    linked_home: Path,
    repo: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv(ENDPOINT_ENV)
    monkeypatch.delenv(ORG_ENV)
    monkeypatch.delenv(API_KEY_ENV)
    assert collector.record(
        repo,
        [_event(1)],
        session_id=SID,
        agent="claude_code",
    ) == 0
