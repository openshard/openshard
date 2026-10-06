"""Persistent connected capture: one account connection, many agent sessions."""
from __future__ import annotations

import io
import json
import urllib.error
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
    KIND_FORBIDDEN,
    KIND_UNAUTHORIZED,
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


def _proxy_env(monkeypatch, surface: str = "claude-code-web") -> None:
    """Claude Cloud: the osc_ token is in the provider's credential store, not the environment."""
    monkeypatch.setenv(cconfig.ENDPOINT_ENV, ENDPOINT)
    monkeypatch.setenv(cconfig.ORG_ENV, ORG)
    monkeypatch.setenv(cconfig.TOKEN_ENV, cconfig.PROXY_INJECTED_TOKEN)
    monkeypatch.setenv(cconfig.SURFACE_ENV, surface)


class _Response:
    """A minimal ``urlopen`` success for the transport under test."""

    def __init__(self, status: int, body: bytes = b"{}") -> None:
        self.status = status
        self._body = body

    def read(self, _n: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc) -> None:
        return None


def _http_error(status: int, code: str) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"code": code, "message": code}}).encode("utf-8")
    return urllib.error.HTTPError(ENDPOINT, status, code, {}, io.BytesIO(body))  # type: ignore[arg-type]


def _capture_urlopen(monkeypatch, outcome):
    """Replace ``urlopen``; record each Request; return the recorded list."""
    seen: list = []

    def fake(request, timeout=None):
        seen.append(request)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return seen


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

    def test_proxy_injected_marker_is_a_proxy_backed_connection(self, home, monkeypatch):
        _proxy_env(monkeypatch)
        connection = cconfig.resolve_connection()
        assert connection is not None
        assert connection.endpoint == ENDPOINT
        assert connection.organisation_id == ORG
        assert connection.token == "proxy-injected"
        assert connection.source == cconfig.SOURCE_PROXY == "proxy"
        assert connection.proxy_backed is True
        assert cconfig.available_hint() is True

    def test_real_tokens_are_not_proxy_backed(self, home, monkeypatch):
        _connected_env(monkeypatch)
        connection = cconfig.resolve_connection()
        assert connection is not None and connection.proxy_backed is False
        monkeypatch.delenv(cconfig.TOKEN_ENV)
        sconfig.save_link(endpoint=ENDPOINT, organisation_id=ORG, api_key=OSK)
        linked = cconfig.resolve_connection()
        assert linked is not None and linked.proxy_backed is False

    @pytest.mark.parametrize("token", [
        "Proxy-Injected", "proxy_injected", "proxy-injected-1", "proxy", "injected",
        "bogus", "osc_short", "osk_" + "!" * 40, "Bearer " + OSC, "",
    ])
    def test_only_the_exact_marker_or_a_well_formed_token_is_accepted(self, home, monkeypatch, token):
        _connected_env(monkeypatch)
        monkeypatch.setenv(cconfig.TOKEN_ENV, token)
        assert cconfig.resolve_connection() is None

    def test_marker_tolerates_surrounding_whitespace_like_real_tokens(self, home, monkeypatch):
        _proxy_env(monkeypatch)
        monkeypatch.setenv(cconfig.TOKEN_ENV, "  proxy-injected\n")
        connection = cconfig.resolve_connection()
        assert connection is not None and connection.proxy_backed is True
        assert connection.token == "proxy-injected"

    def test_proxy_marker_still_yields_to_disable_switch(self, home, monkeypatch):
        _proxy_env(monkeypatch)
        monkeypatch.setenv(cconfig.DISABLE_ENV, "off")
        assert cconfig.resolve_connection() is None
        assert cconfig.available_hint() is False

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


class TestProxyBackedTransport:
    """The marker travels through the unchanged transport; the Platform's answer is reported as is."""

    def _client(self, monkeypatch):
        from openshard.connected.transport import ConnectedCaptureClient

        _proxy_env(monkeypatch)
        connection = cconfig.resolve_connection()
        assert connection is not None and connection.proxy_backed
        session = cconfig.ConnectedSession("claude-code-web", SID, "claude_code", None, None, None, None)
        return ConnectedCaptureClient(connection, session, user_agent="openshard/test")

    def test_marker_is_sent_as_the_bearer_for_the_proxy_to_replace(self, home, monkeypatch):
        client = self._client(monkeypatch)
        seen = _capture_urlopen(monkeypatch, _Response(201, b'{"accepted":0}'))
        result = client.send_events({"source": "claude_hooks", "collector_id": "c1", "events": []})
        assert result.kind == KIND_CREATED
        assert len(seen) == 1
        assert seen[0].full_url == f"{ENDPOINT}/v1/orgs/{ORG}/connected-captures/events"
        assert seen[0].get_header("Authorization") == "Bearer proxy-injected"

    def test_missing_injection_is_reported_as_unauthorized_not_accepted(self, home, monkeypatch):
        client = self._client(monkeypatch)
        _capture_urlopen(monkeypatch, _http_error(401, "unauthenticated"))
        result = client.send_events({"source": "claude_hooks", "collector_id": "c1", "events": []})
        assert result.accepted is False
        assert result.kind == KIND_UNAUTHORIZED
        assert result.status == 401
        assert result.code == "unauthenticated"

    def test_wrong_injected_credential_is_reported_as_forbidden(self, home, monkeypatch):
        client = self._client(monkeypatch)
        _capture_urlopen(monkeypatch, _http_error(403, "forbidden"))
        result = client.send({"receipt_id": "rcpt_x"})
        assert result.accepted is False
        assert result.kind == KIND_FORBIDDEN
        assert result.status == 403


class TestConnectedCollector:
    def test_proxy_backed_session_streams_and_stops_honestly_when_nothing_is_injected(
        self, home, repo, monkeypatch,
    ):
        _proxy_env(monkeypatch)
        _work(repo)
        pending = spool.pending_count()
        assert pending > 0

        # The proxy did not replace the marker: the Platform refuses it.
        _capture_urlopen(monkeypatch, _http_error(401, "unauthenticated"))
        report = collector.flush(now=100.0)
        assert report.attached is True
        assert report.stopped == "unauthorized"
        assert report.events_sent == 0
        assert spool.pending_count() == pending
        state = spool.read_state()
        assert state is not None and state["stopped"] == "unauthorized"

    def test_proxy_backed_session_drains_through_the_proxy(self, home, repo, monkeypatch):
        _proxy_env(monkeypatch)
        _work(repo)
        pending = spool.pending_count()
        seen = _capture_urlopen(monkeypatch, _Response(201, b'{"accepted":1}'))
        report = collector.flush(now=100.0)
        assert report.stopped is None
        assert report.events_sent == pending
        assert spool.pending_count() == 0
        assert seen and all(r.get_header("Authorization") == "Bearer proxy-injected" for r in seen)


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
class TestConcurrentConnectedSessions:
    def test_switching_sessions_keeps_both_pending_journals(self, home, repo, monkeypatch):
        _connected_env(monkeypatch)
        _work(repo)
        first = spool.pending_count()
        other_sid = "abababab-3434-4565-8787-909090909090"
        for event, extra in [("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "second task"}), ("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "python -m pytest -q"}})]:
            handle_claude_hook({"session_id": other_sid, "cwd": str(repo), "hook_event_name": event, **extra})
        assert spool.pending_count() > first
        fake = FakeConnectedPlatform()
        report = collector.flush(client=fake)
        assert report.events_sent > first
        assert len({batch["collector_id"] for batch in fake.batches}) == 2
        assert spool.pending_count() == 0

    def test_changing_organisation_never_sends_queued_session_to_new_account(self, home, repo, monkeypatch):
        _connected_env(monkeypatch)
        _work(repo)
        pending = spool.pending_count()
        monkeypatch.setenv(cconfig.ORG_ENV, "1f1e2d3c-4b5a-4697-8877-665544332211")
        fake = FakeConnectedPlatform()
        report = collector.flush(client=fake)
        assert report.stopped == "connection_changed"
        assert fake.batches == []
        assert spool.pending_count() == pending

    def test_delivery_requests_reach_every_session_for_the_repository(self, home, repo, monkeypatch):
        _connected_env(monkeypatch)
        _work(repo)
        handle_claude_hook({"session_id": "abababab-3434-4565-8787-909090909090", "cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"})
        assert collector.request_delivery(repo_root=repo)
        assert all((spool.read_state(env) or {}).get("deliver") for env in spool.connected_envs())

    def test_delivery_does_not_include_unrelated_receipts(self, home, repo, monkeypatch):
        _connected_env(monkeypatch)
        _work(repo, end=True)
        own = load_history(repo / ".openshard" / "runs.jsonl", coerce=True)[-1]
        foreign = dict(own, receipt_id="rcpt_" + "f" * 32, run_id="foreign-run")
        history = repo / ".openshard" / "runs.jsonl"
        with history.open("a") as stream:
            stream.write(json.dumps(foreign) + "\n")
        fake = FakeConnectedPlatform()
        collector.flush(client=fake, deliver=True)
        assert [r["receipt"]["receipt_id"] for r in fake.receipts] == [own["receipt_id"]]

    def test_status_reports_connected_sessions_without_claiming_manual_attachment(self, home, repo, monkeypatch):
        from click.testing import CliRunner

        from openshard.cli.main import cli

        _connected_env(monkeypatch)
        _work(repo)
        pending = spool.pending_count()
        result = CliRunner().invoke(cli, ["remote", "status", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.output)
        assert body["status"] == "connected"
        assert body["connected"] is True and body["attachment"] is None
        assert len(body["sessions"]) == 1
        assert body["local"]["pending"] == pending
        assert OSC not in result.output
        assert body["connection_source"] == "env"
        plain = CliRunner().invoke(cli, ["remote", "status"])
        assert "Connected capture: 1 session(s)" in plain.output
        assert "credential: from env" in plain.output
        assert "not attached" not in plain.output

    def test_status_names_a_proxy_backed_credential_without_claiming_injection(self, home, repo, monkeypatch):
        from click.testing import CliRunner

        from openshard.cli.main import cli

        _proxy_env(monkeypatch)
        _work(repo)
        result = CliRunner().invoke(cli, ["remote", "status", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.output)
        assert body["status"] == "connected"
        assert body["connection_source"] == "proxy"
        plain = CliRunner().invoke(cli, ["remote", "status"])
        assert "credential: from proxy" in plain.output
        assert "cannot see whether it did" in plain.output

    def test_legacy_connected_journal_survives_new_session_and_receives_delivery_request(self, home, repo, monkeypatch):
        import shutil

        _connected_env(monkeypatch)
        _work(repo)
        original = spool.connected_envs()[0]
        original_dir = spool.spool_dir(original)
        legacy = spool.session_env(None, "legacy")
        legacy_dir = spool.spool_dir(legacy)
        for name in (spool.STATE_FILENAME, spool.SPOOL_FILENAME):
            shutil.copyfile(original_dir / name, legacy_dir / name)
        shutil.rmtree(original_dir)
        first = spool.pending_count(legacy)
        handle_claude_hook({"session_id": "abababab-3434-4565-8787-909090909090", "cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"})
        assert spool.pending_count(legacy) == first
        assert len(spool.connected_envs()) == 1
        assert collector.request_delivery(repo_root=repo)
        assert (spool.read_state(legacy) or {})["deliver"] is True
        assert all((spool.read_state(env) or {}).get("deliver") for env in spool.connected_envs())
        fake = FakeConnectedPlatform()
        report = collector.flush(client=fake)
        assert report.events_sent > first
        assert len({batch["collector_id"] for batch in fake.batches}) == 2
        assert spool.pending_count() == 0
