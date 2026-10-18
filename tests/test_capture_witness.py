"""``capture_witness``: platform-witnessed only after a returned acknowledgement, with counts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history.store import load_history
from openshard.remote import collector, spool, witness
from openshard.remote import config as rconfig
from openshard.sync.envelope import build_envelope
from openshard.sync.transport import KIND_DUPLICATE, KIND_UNAVAILABLE, SendResult
from tests.capture_fixtures import _make_repo
from tests.test_remote_capture import CAPTURE_ID, ORG, RUNS, TOKEN, URL, FakeClient, _hook, _work


def _ack(seq_to: int) -> dict:
    return {"witness": {"seq_from": 1, "seq_to": seq_to, "chain_head": "sha256:" + "c" * 64,
                        "received_at": "2026-10-18T09:00:00.000Z"}}


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
    return _make_repo(tmp_path / "workspace")


def _session(repo: Path) -> dict:
    _work(repo, tool_calls=2)
    _hook(repo, "Stop")
    [entry, *_] = load_history(repo / RUNS, coerce=False)
    return entry


def _last(repo: Path, monkeypatch, *args: str) -> str:
    monkeypatch.chdir(repo)
    return CliRunner().invoke(cli, ["last", *args], catch_exceptions=False).output


class TestCaptureWitness:
    def test_not_attached_is_self_held_without_counts(self, home, repo, monkeypatch):
        entry = _session(repo)
        assert witness.witness_for_entry(entry) == {
            "capture_witness": "self_held", "events_witnessed": None, "events_in_receipt": None}
        doc = json.loads(_last(repo, monkeypatch, "--json"))
        assert doc["capture_witness"]["capture_witness"] == "self_held"

    def test_sent_without_an_acknowledgement_is_never_platform(self, attached, repo):
        entry = _session(repo)
        collector.flush(client=FakeClient([SendResult(KIND_DUPLICATE, 200)]))  # accepted, no witness block
        assert spool.read_state()["acked_seq"] > 0
        assert witness.witness_for_entry(entry)["capture_witness"] == "self_held"

    def test_an_unreachable_platform_is_self_held(self, attached, repo):
        entry = _session(repo)
        collector.flush(client=FakeClient([SendResult(KIND_UNAVAILABLE, 503)]))
        assert witness.witness_for_entry(entry)["capture_witness"] == "self_held"

    def test_an_acknowledged_batch_makes_it_platform_with_reconciliation_counts(self, attached, repo, monkeypatch):
        _session(repo)
        collector.flush(client=FakeClient([SendResult(KIND_DUPLICATE, 200, details=_ack(50))]))
        [entry, *_] = load_history(repo / RUNS, coerce=False)
        block = witness.witness_for_entry(entry)
        in_receipt = len({e["event_id"] for e in entry["events"]})
        assert block["capture_witness"] == "platform" and block["events_in_receipt"] == in_receipt
        assert block["events_witnessed"] == in_receipt  # every Event of the Receipt was in an acknowledged batch
        doc = json.loads(_last(repo, monkeypatch, "--json"))
        assert doc["capture_witness"] == block
        assert "Capture witness: platform · " in _last(repo, monkeypatch, "--full")

    @pytest.mark.session_receipts("only a session Receipt gains Events after its first batch")
    def test_events_added_after_the_acknowledgement_are_not_counted(self, attached, repo):
        _session(repo)
        collector.flush(client=FakeClient([SendResult(KIND_DUPLICATE, 200, details=_ack(50))]))
        _hook(repo, "SessionEnd", reason="other")
        collector.flush(client=FakeClient([SendResult(KIND_DUPLICATE, 200)]))  # this batch: no witness block
        entries = load_history(repo / RUNS, coerce=False)
        block = witness.witness_for_entries(entries)[entries[0]["receipt_id"]]
        assert block["capture_witness"] == "platform"
        assert block["events_witnessed"] < block["events_in_receipt"]

    def test_history_json_carries_it_and_sync_does_not(self, attached, repo, monkeypatch):
        entry = _session(repo)
        collector.flush(client=FakeClient([SendResult(KIND_DUPLICATE, 200, details=_ack(50))]))
        monkeypatch.chdir(repo)
        doc = json.loads(CliRunner().invoke(cli, ["history", "--json"], catch_exceptions=False).output)
        assert doc["shards"][0]["capture_witness"]["capture_witness"] == "platform"
        assert "capture_witness" not in json.dumps(build_envelope(entry, 0, core_version="9.9.9"))

    def test_a_capture_switch_forgets_the_old_witnessed_ids(self, attached, repo):
        _session(repo)
        collector.flush(client=FakeClient([SendResult(KIND_DUPLICATE, 200, details=_ack(50))]))
        assert spool.witnessed_ids()
        spool.update_state(None, "11111111-2222-4333-8444-555555555555")  # the spool now belongs to another capture
        assert spool.witnessed_ids() == set()

    def test_the_witnessed_record_is_bounded(self, attached):
        ids = [f"evt-{i:05d}" for i in range(spool.MAX_SPOOLED_EVENTS + 10)]
        spool.record_witnessed(None, CAPTURE_ID, ids)
        spool.record_witnessed(None, CAPTURE_ID, ["evt-late"])
        assert len(spool.witnessed_ids()) == spool.MAX_SPOOLED_EVENTS
