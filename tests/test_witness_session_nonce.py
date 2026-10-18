"""The session nonce and the hosted witness acknowledgement (remote capture).

A session mints one random, non-secret nonce when its buffer opens. It rides
in the first remote-capture Event's metadata and in the Receipt's capture
block and local projection, so the Platform can bind a Receipt to the journal
it witnessed. The Platform receipt-sync contract does not accept it yet, so
the sync payload leaves it out. An accepted append may carry a witness
acknowledgement; the spool keeps it and ``openshard remote status`` shows it.
Unknown stays unknown: no acknowledgement is never shown as one.

Nothing reaches the network: a scripted client, a loopback HTTP server, a
temporary ``OPENSHARD_HOME`` and no detached flusher.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime, timedelta
from http.server import HTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters.claude_hooks import buffer_path
from openshard.history.receipt_evidence import recorded_session_nonce, session_block
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.store import load_history
from openshard.history.views import receipt_to_dict
from openshard.remote import collector, spool
from openshard.remote import config as rconfig
from openshard.sync.envelope import receipt_payload
from openshard.sync.transport import KIND_DUPLICATE, KIND_REJECTED, KIND_UNAVAILABLE, SendResult
from tests.capture_fixtures import _git, _make_repo
from tests.test_remote_capture import (
    CAPTURE_ID,
    ORG,
    RUNS,
    SID,
    TOKEN,
    URL,
    WIRE_EVENT_KEYS,
    FakeClient,
    _hook,
    _Platform,
    _work,
)

NONCE_RE = re.compile(r"^[a-z2-7]{26}$")


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "vm-home"
    monkeypatch.setenv("OPENSHARD_HOME", str(path))
    monkeypatch.setenv("OPENSHARD_REMOTE_NO_SPAWN", "1")
    for name in (rconfig.URL_ENV, rconfig.TOKEN_ENV, rconfig.DISABLE_ENV):
        monkeypatch.delenv(name, raising=False)
    return path


@pytest.fixture
def attached(home: Path) -> rconfig.Attachment:
    return rconfig.save_attachment(capture_url=URL, token=TOKEN, organisation_id=ORG, agent="claude-code")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = _make_repo(tmp_path / "workspace")
    _git(root, "remote", "add", "origin", "https://github.com/openshard/widget.git")
    return root


@pytest.fixture
def platform():
    _Platform.events, _Platform.requests, _Platform.status_override = [], [], None
    httpd = HTTPServer(("127.0.0.1", 0), _Platform)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/v1/remote-captures/{CAPTURE_ID}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _entries(repo: Path) -> list[dict]:
    return load_history(repo / RUNS, coerce=False)


def _nonce_events(events: list[dict]) -> list[dict]:
    return [e for e in events if "session_nonce" in (e.get("metadata") or {})]


def _witness(seq_to: int, **extra) -> dict:
    return {"witness": {"seq_from": 1, "seq_to": seq_to, "chain_head": "sha256:" + "c" * 64,
                        "received_at": "2026-10-18T09:00:00.000Z", **extra}}


# ---------------------------------------------------------------------------
# session nonce
# ---------------------------------------------------------------------------


class TestSessionNonce:
    def test_minted_once_and_sent_in_the_first_event_and_the_receipt(self, attached, repo):
        _work(repo, tool_calls=3)
        _hook(repo, "Stop")
        events, _ = spool.pending(None, CAPTURE_ID)
        nonce = events[0]["metadata"]["session_nonce"]
        assert events[0]["event_type"] == "session.started" and NONCE_RE.match(nonce)
        assert _nonce_events(events) == [events[0]]  # the first Event only
        entry = _entries(repo)[-1]
        assert recorded_session_nonce(entry) == nonce  # the Receipt keeps its session.started Event
        assert "session_nonce" not in entry["capture"]  # stored once, in the Event
        projection = receipt_to_dict(build_shard_receipt(entry, index=0), extended=True)
        assert projection["session"]["session_nonce"] == nonce

    def test_sessions_get_different_nonces(self, attached, repo):
        _work(repo, tool_calls=1)
        _hook(repo, "UserPromptSubmit", prompt="another", session_id="99999999-3434-4565-8787-909090909090")
        firsts = [e for e in spool.pending(None, CAPTURE_ID)[0] if e["event_type"] == "session.started"]
        nonces = {e["metadata"]["session_nonce"] for e in firsts}
        assert len(firsts) == 2 and len(nonces) == 2

    def test_survives_a_lost_buffer_and_a_resumed_segment(self, attached, repo):
        _work(repo, tool_calls=1)
        _hook(repo, "Stop")
        nonce = recorded_session_nonce(_entries(repo)[-1])
        # The buffer is gone (restart): it is rebuilt from the record, not re-minted.
        buffer_path(repo, SID).unlink()
        _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "ls"})
        _hook(repo, "Stop")
        assert recorded_session_nonce(_entries(repo)[-1]) == nonce
        # A rebuilt buffer re-spools its Events under their own ids (the Platform de-duplicates).
        assert len({e["event_id"] for e in _nonce_events(spool.pending(None, CAPTURE_ID)[0])}) == 1
        # The session ends; ``--resume`` opens a new Receipt segment of the same agent session.
        _hook(repo, "SessionEnd", reason="exit")
        _hook(repo, "SessionStart", source="resume")
        _hook(repo, "UserPromptSubmit", prompt="carry on")
        _hook(repo, "Stop")
        first, resumed = _entries(repo)[-2], _entries(repo)[-1]
        assert resumed["receipt_id"] != first["receipt_id"]
        assert resumed["capture"]["start_source"] == "resume"
        assert recorded_session_nonce(resumed) == nonce
        assert {e["metadata"]["session_nonce"] for e in _nonce_events(spool.pending(None, CAPTURE_ID)[0])} == {nonce}

    def test_a_record_without_one_stays_without_one(self, attached, repo):
        _work(repo, tool_calls=1)
        _hook(repo, "Stop")
        path = repo / RUNS
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for line in lines:
            for event in line.get("events", []):  # a pre-nonce record
                event.get("metadata", {}).pop("session_nonce", None)
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        buffer_path(repo, SID).unlink()
        _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "ls"})
        _hook(repo, "Stop")
        entry = _entries(repo)[-1]
        assert recorded_session_nonce(entry) is None  # unknown, never invented mid-session
        assert not _nonce_events(entry["events"])
        block = session_block(entry)
        assert block is not None and "session_nonce" not in block

    def test_payload_privacy_unchanged(self, attached, repo):
        _work(repo, tool_calls=1)
        _hook(repo, "Stop")
        events, _ = spool.pending(None, CAPTURE_ID)
        assert all(set(e) == WIRE_EVENT_KEYS for e in events)
        assert set(events[0]["metadata"]) == {"hook", "session_nonce"}
        entry = _entries(repo)[-1]
        without = json.loads(json.dumps(entry))
        for event in without["events"]:
            event["metadata"].pop("session_nonce", None)
        # The sync payload is exactly what it was before the nonce existed:
        # the Platform's session schema is strict and does not accept it yet.
        def payload(record: dict) -> dict:
            # The nonce is covered by the record's checksum, so only the tamper verdict differs.
            return {k: v for k, v in receipt_payload(record, 0).items() if k not in ("integrity", "content_hash")}

        assert payload(entry) == payload(without)
        assert receipt_payload(entry, 0)["integrity"] == "Checksum matches"
        assert recorded_session_nonce(entry) not in json.dumps(receipt_payload(entry, 0))


# ---------------------------------------------------------------------------
# witness acknowledgement
# ---------------------------------------------------------------------------


class TestWitnessAck:
    def test_an_acknowledgement_is_stored_and_described(self, attached, repo):
        _work(repo, tool_calls=2)
        collector.flush(client=FakeClient([SendResult(KIND_DUPLICATE, 200, details=_witness(214))]))
        stored = spool.read_state()["witness"]
        assert stored["seq_to"] == 214 and stored["chain_head"] == "sha256:" + "c" * 64
        assert stored["received_at"] == "2026-10-18T09:00:00.000Z"
        when = datetime.strptime(stored["acknowledged_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        status = collector.witness_status(spool.read_state(), now=when + timedelta(seconds=12))
        assert status["state"] == "acknowledged" and status["events"] == 214
        assert status["text"] == "214 events acknowledged · last 12s ago"

    def test_offline_and_unknown_states(self, attached, repo):
        assert collector.witness_status(None)["text"] == "not connected"
        _work(repo, tool_calls=1)
        collector.flush(client=FakeClient([SendResult(KIND_UNAVAILABLE, 503)]))
        assert collector.witness_status(spool.read_state())["state"] == "not_connected"
        # Delivered, but this Platform returned no witness block: unknown, never assumed.
        collector.flush(client=FakeClient([SendResult(KIND_DUPLICATE, 200)]), now=10**10)
        status = collector.witness_status(spool.read_state())
        assert status["state"] == "unknown" and status["events"] is None
        assert "witness" not in spool.read_state()

    def test_a_later_smaller_acknowledgement_does_not_go_backwards(self, attached, repo):
        _work(repo, tool_calls=1)
        collector.flush(client=FakeClient([SendResult(KIND_DUPLICATE, 200, details=_witness(40))]))
        _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "ls"})
        collector.flush(client=FakeClient([SendResult(KIND_DUPLICATE, 200, details=_witness(3))]))
        assert spool.read_state()["witness"]["seq_to"] == 40

    def test_unusable_blocks_are_ignored_and_bounded(self):
        for bad in (None, {}, {"witness": "x"}, {"witness": {"seq_to": 0}}, {"witness": {"seq_to": True}},
                    {"witness": {"seq_to": 3, "seq_from": 5}}, {"witness": {"seq_to": -1}}):
            assert collector.wire_witness(bad) is None
        kept = collector.wire_witness(_witness(5, chain_head="has space", received_at="yesterday",
                                               sig={"payload": "p" * 5000}))
        assert kept == {"seq_from": 1, "seq_to": 5, "chain_head": None, "received_at": None, "sig": None}

    def test_rejected_answers_never_carry_a_witness(self, attached, repo):
        _work(repo, tool_calls=1)
        details = {"witness": {"seq_to": 9}, "code": "invalid_payload"}
        collector.flush(client=FakeClient([SendResult(KIND_REJECTED, 422, "invalid_payload", details)]))
        assert "witness" not in spool.read_state()

    def test_connected_transport_hands_the_accepted_body_back(self, home, monkeypatch):
        from openshard.connected.config import ConnectedConnection, ConnectedSession
        from openshard.connected.transport import ConnectedCaptureClient

        client = ConnectedCaptureClient(
            ConnectedConnection("https://platform.example.test", "org", "osk_x", "env"),
            ConnectedSession("claude-code-local", SID, "claude-code", None, None, None, None),
            user_agent="openshard/test",
        )
        monkeypatch.setattr(client, "_request", lambda *a: (SendResult(KIND_DUPLICATE, 200), _witness(7)))
        assert collector.wire_witness(client.send_events({"events": []}).details)["seq_to"] == 7
        monkeypatch.setattr(client, "_request", lambda *a: (SendResult(KIND_UNAVAILABLE, 503), _witness(7)))
        assert client.send_events({"events": []}).details is None

    def test_remote_status_shows_the_acknowledgement(self, home, repo, platform, monkeypatch):
        from openshard.cli.main import cli

        original = _Platform._answer

        def with_witness(handler, status, body):
            if status == 200 and "accepted" in body:
                body = {**body, **_witness(len(type(handler).events))}
            return original(handler, status, body)

        monkeypatch.setattr(_Platform, "_answer", with_witness)
        monkeypatch.chdir(repo)
        rconfig.save_attachment(capture_url=platform, token=TOKEN)
        out = CliRunner().invoke(cli, ["remote", "status"], catch_exceptions=False)
        assert "witness:   not connected" in out.output
        _work(repo, tool_calls=2)
        collector.flush()
        out = CliRunner().invoke(cli, ["remote", "status"], catch_exceptions=False)
        assert re.search(r"witness:   4 events acknowledged · last \d+s ago", out.output), out.output
        doc = json.loads(CliRunner().invoke(cli, ["remote", "status", "--json"], catch_exceptions=False).output)
        assert doc["local"]["witness"]["state"] == "acknowledged" and doc["local"]["witness"]["events"] == 4
        assert TOKEN not in out.output

    def test_not_attached_status_says_not_connected(self, home, repo, monkeypatch):
        from openshard.cli.main import cli

        monkeypatch.chdir(repo)
        out = CliRunner().invoke(cli, ["remote", "status"], catch_exceptions=False)
        assert "witness:   not connected" in out.output
