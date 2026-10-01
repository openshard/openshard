"""Persistent connected capture: one account connection, many agent sessions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.connected import config as cconfig
from openshard.history.store import load_history
from openshard.remote import collector, spool
from openshard.remote import config as rconfig
from openshard.sync import config as sconfig
from openshard.sync.transport import (
    KIND_CREATED,
    KIND_DUPLICATE,
    KIND_UNAVAILABLE,
    SendResult,
)
from tests.capture_fixtures import _git, _make_repo

ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
ENDPOINT = "https://platform.example.test"
OSC = "osc_AbCd1234_" + "c" * 43
OSK = "osk_AbCd1234_" + "k" * 43
OSR = "osr_AbCd1234_" + "r" * 43
OSA = "osa_AbCd1234_" + "a" * 43
OSM = "osm_AbCd1234_" + "m" * 43
OSF = "osf_AbCd1234_" + "f" * 43
CAPTURE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SID = "12121212-3434-4565-8787-909090909090"


class FakeConnectedPlatform:
    def __init__(self, event_results: list[SendResult] | None = None) -> None:
        self.event_results = list(event_results or [SendResult(KIND_DUPLICATE, 200)])
        self.batches: list[dict] = []
        self.receipts: list[dict] = []
        self.evidence: list[tuple[str, dict]] = []

    def send_events(self, batch: dict) -> SendResult:
        self.batches.append(json.loads(json.dumps(batch)))
        return self.event_results.pop(0) if len(self.event_results) > 1 else self.event_results[0]

    def send(self, envelope: dict) -> SendResult:
        self.receipts.append(json.loads(json.dumps(envelope)))
        return SendResult(KIND_CREATED, 201)

    def send_evidence(self, receipt_id: str, envelope: dict) -> SendResult:
        self.evidence.append((receipt_id, json.loads(json.dumps(envelope))))
        return SendResult(KIND_CREATED, 201)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("OPENSHARD_HOME", str(path))
    monkeypatch.setenv("OPENSHARD_REMOTE_NO_SPAWN", "1")
    for name in (
        rconfig.URL_ENV, rconfig.TOKEN_ENV, rconfig.DISABLE_ENV,
        cconfig.ENDPOINT_ENV, cconfig.ORG_ENV, cconfig.TOKEN_ENV,
        cconfig.SURFACE_ENV, cconfig.DISABLE_ENV,
        sconfig.ENDPOINT_ENV, sconfig.ORG_ENV, sconfig.API_KEY_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = _make_repo(tmp_path / "repo")
    _git(root, "remote", "add", "origin", "https://github.com/openshard/widget.git")
    return root


def _connected_env(monkeypatch, surface: str = "claude-code-web") -> None:
    monkeypatch.setenv(cconfig.ENDPOINT_ENV, ENDPOINT)
    monkeypatch.setenv(cconfig.ORG_ENV, ORG)
    monkeypatch.setenv(cconfig.TOKEN_ENV, OSC)
    monkeypatch.setenv(cconfig.SURFACE_ENV, surface)


def _hook(repo: Path, event: str, **extra) -> None:
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, **extra}
    handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _work(repo: Path, *, end: bool = False) -> None:
    _hook(repo, "SessionStart", source="startup")
    _hook(repo, "UserPromptSubmit", prompt="make connected capture work")
    _hook(
        repo,
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": "python -m pytest -q"},
    )
    if end:
        _hook(repo, "SessionEnd", reason="other")


class TestConnectedConfig:
    def test_explicit_scoped_connection(self, home, monkeypatch):
        _connected_env(monkeypatch, "chatgpt-work")
        connection = cconfig.resolve_connection()
        assert connection is not None
        assert connection.endpoint == ENDPOINT
        assert connection.organisation_id == ORG
        assert connection.token == OSC
        assert connection.source == "env"

    def test_trusted_local_platform_link_is_automatically_a_connection(self, home):
        sconfig.save_link(endpoint=ENDPOINT, organisation_id=ORG, api_key=OSK)
        connection = cconfig.resolve_connection()
        assert connection is not None
        assert connection.source == "platform_link"
        assert connection.token == OSK

    def test_remote_cloud_surface_is_inferred_without_per_run_setup(self, home, monkeypatch):
        _connected_env(monkeypatch)
        monkeypatch.delenv(cconfig.SURFACE_ENV)
        monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
        entry = {
            "capture": {"session_id": SID, "agent": "claude_code", "provider": "anthropic"},
            "repo_identity": "github.com/openshard/widget",
            "repo": "widget",
            "branch": "main",
        }
        session = cconfig.session_from_entry(entry, {"run_id": "run-1"})
        assert session is not None
        assert session.surface == "claude-code-web"
        assert session.external_session_id == SID


class TestConnectedCollector:
    def test_connected_session_spools_without_remote_create_or_attach(self, home, repo, monkeypatch):
        _connected_env(monkeypatch)
        assert rconfig.resolve_attachment() is None
        _work(repo)

        state = spool.read_state()
        assert state is not None
        assert state["capture_id"].startswith("connected-")
        assert state["connected"]["surface"] == "claude-code-web"
        assert state["connected"]["external_session_id"] == SID
        assert spool.pending_count() > 0

    def test_offline_keeps_evidence_then_retry_drains_it(self, home, repo, monkeypatch):
        _connected_env(monkeypatch)
        _work(repo)
        before = spool.pending_count()
        assert before > 0

        fake = FakeConnectedPlatform([
            SendResult(KIND_UNAVAILABLE, None),
            SendResult(KIND_DUPLICATE, 200),
        ])
        first = collector.flush(client=fake, now=100.0)
        assert first.stopped == "unavailable"
        assert spool.pending_count() == before

        # Clear the deterministic retry timer rather than waiting in a unit test.
        state = spool.read_state()
        assert state is not None
        spool.update_state(None, state["capture_id"], backoff_until=None)
        second = collector.flush(client=fake, now=200.0)
        assert second.events_sent == before
        assert spool.pending_count() == 0

    def test_completed_connected_session_delivers_canonical_receipt(self, home, repo, monkeypatch):
        _connected_env(monkeypatch)
        _work(repo, end=True)
        entries = load_history(repo / ".openshard" / "runs.jsonl", coerce=True)
        assert entries and entries[-1]["receipt_id"].startswith("rcpt_")

        fake = FakeConnectedPlatform()
        report = collector.flush(client=fake, deliver=True)
        assert report.stopped is None
        assert fake.receipts
        assert fake.receipts[-1]["receipt"]["receipt_id"] == entries[-1]["receipt_id"]

    def test_explicit_manual_remote_capture_wins(self, home, repo, monkeypatch):
        _connected_env(monkeypatch)
        rconfig.save_attachment(
            capture_url=f"{ENDPOINT}/v1/remote-captures/{CAPTURE_ID}",
            token=OSR,
            organisation_id=ORG,
        )
        _work(repo)
        state = spool.read_state()
        assert state is not None
        assert state["capture_id"] == CAPTURE_ID
        assert "connected" not in state

    @pytest.mark.parametrize("secret", [OSC, OSA, OSM, OSF])
    def test_openshard_secret_is_withheld_before_spooling(self, home, secret):
        event = {
            "schema_version": 1,
            "event_id": "evt-connected-secret",
            "event_type": "tool.invoked",
            "occurred_at": "2026-10-01T00:00:00Z",
            "run_id": "run-1",
            "shard_id": "shard-20261001-0001",
            "attempt_number": 1,
            "actor": "codex_hooks",
            "source": "codex_hooks",
            "action": f"Bash used {secret}",
            "target": "python",
            "status": "unknown",
            "evidence": "agent_reported",
            "metadata": {},
            "raw_content_stored": False,
        }
        wire = spool.wire_event(event)
        assert wire is not None
        assert wire["action"] == "[withheld]"
        assert secret not in json.dumps(wire)