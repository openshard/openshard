"""Later verification evidence reaches the Platform without the Receipt being resent.

A hosted Receipt is a first copy. ``openshard verify`` and ``openshard verify
--ci`` record stronger evidence afterwards; these tests pin how that evidence
is projected (``sync/evidence.py``), that it is sent once per distinct
evidence set on its own route, and that the Receipt payload itself never
changes because of it.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.history.store import amend_latest_record, load_history
from openshard.sync import client, config, envelope, outbox, transport
from openshard.sync import evidence as sync_evidence
from openshard.sync.transport import SendResult
from openshard.verification import ci_evidence as ci
from openshard.verification import post_session as ps
from tests.capture_fixtures import _git, _make_repo

ENDPOINT = "https://platform.example.test"
ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
KEY = "osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz"
RUNS = Path(".openshard") / "runs.jsonl"
SHA = "7239828109a4f27f672ea34006149bfac9b5e803"
SID = "66666666-7777-4888-8999-aaaaaaaaaaaa"

# The closed key sets of the Platform's verification evidence contract v1
# (openshard/platform: packages/contracts/src/verification-evidence.ts). An
# unknown key is a 400 there, so Core must produce exactly these.
ENVELOPE_KEYS = {"contract", "contract_version", "source", "receipt_id", "evidence", "state"}
EVIDENCE_KEYS = {"attestation_id", "created_at", "kind", "verification"}
STATE_KEYS = {
    "version", "state", "authority", "effective_status", "basis", "session_status", "session_source",
    "claim_status", "claim_source", "artifact_sha", "checks_passed", "checks_failed", "checks_attempted",
    "failed_checks", "history",
}
HISTORY_KEYS = {"kind", "at", "source", "status", "checks_passed", "checks_failed", "checks_attempted", "artifact_sha"}


@pytest.fixture
def env(tmp_path: Path) -> dict:
    return {"OPENSHARD_HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "")}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = _make_repo(tmp_path / "widget")
    _git(root, "remote", "add", "origin", "https://github.com/openshard/widget.git")
    return root


@pytest.fixture
def link(env: dict) -> config.PlatformLink:
    return config.save_link(endpoint=ENDPOINT, organisation_id=ORG, api_key=KEY, env=env)


@pytest.fixture
def recording():
    rt = transport.RecordingPlatformTransport()
    client.configure(transport=rt, repo_config={})
    yield rt
    client.configure(transport=None, repo_config=None)


def _hook(repo: Path, event: str, **extra) -> None:
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, **extra}
    handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _agent_session(repo: Path) -> dict:
    """A Claude Code session whose agent reported a passing test run, then ended."""
    _hook(repo, "UserPromptSubmit", prompt="fix it and run the tests")
    _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "python -m pytest -q"})
    _hook(repo, "Stop")
    _hook(repo, "SessionEnd", reason="exit")
    return load_history(repo / RUNS, coerce=False)[-1]


def _rerun(repo: Path, entry: dict, status: str = "passed", *, sha: str | None = SHA,
           at: str = "2026-09-30T14:42:00Z") -> dict:
    check = ps.PlannedCheck(name="python -m pytest -q", argv=["pytest"], kind="test", origin="contract",
                            safety="safe", reason="")
    tree = ps.TreeState(head=sha or SHA, dirty=sha is None, tracked_dirty=sha is None)
    att = ps.build_attestation(
        entry, [ps.CheckRun(check, status, exit_code=0 if status == "passed" else 1)],
        before=tree, after=tree, started_at=at, completed_at=at,
    )
    ps.record_attestation(repo, att)
    return att


def _ci(repo: Path, entry: dict, *conclusions: str, sha: str = SHA, at: str = "2026-09-30T14:44:00Z") -> dict:
    runs = [{"name": f"job-{i}", "head_sha": sha, "status": "completed", "conclusion": c}
            for i, c in enumerate(conclusions)]
    att = ci.build_ci_attestation(
        entry, ci.classify_check_runs(runs, sha), ci.CITarget(sha=sha, binding=ci.BINDING_RERUN), created_at=at,
    )
    assert att is not None
    ps.record_attestation(repo, att)
    return att


def _envelope(repo: Path) -> dict | None:
    entries = load_history(repo / RUNS, coerce=True)
    return sync_evidence.build_evidence_envelope(
        entries[-1], len(entries) - 1, ps.load_attestations(repo / ".openshard"), core_version="9.9.9",
    )


# ---------------------------------------------------------------------------
# the envelope
# ---------------------------------------------------------------------------


class TestEvidenceEnvelope:
    def test_no_later_evidence_means_nothing_to_send(self, repo):
        _agent_session(repo)
        assert _envelope(repo) is None

    def test_rerun_then_ci_is_projected_with_contract_exact_keys(self, repo):
        entry = _agent_session(repo)
        rerun = _rerun(repo, entry)
        ci_att = _ci(repo, entry, "success", "success")
        doc = _envelope(repo)
        assert doc is not None and set(doc) == ENVELOPE_KEYS
        assert doc["contract"] == "openshard.verification-evidence" and doc["contract_version"] == "1"
        assert doc["source"] == {"product": "openshard-core", "version": "9.9.9"}
        assert doc["receipt_id"] == entry["receipt_id"]

        first, second = doc["evidence"]
        assert set(first) == EVIDENCE_KEYS and set(second) == EVIDENCE_KEYS | {"ci"}
        assert (first["attestation_id"], first["kind"]) == (rerun["attestation_id"], "post_session_verification")
        assert (second["attestation_id"], second["kind"]) == (ci_att["attestation_id"], "ci_verification")
        assert second["ci"] == {"provider": "github_checks", "binding": "openshard_rerun", "outcome": "passed"}
        assert first["verification"]["source"] == "directly_observed"
        assert second["verification"]["source"] == "independently_verified"
        assert second["verification"]["artifact_sha"] == SHA

        state = doc["state"]
        assert set(state) == STATE_KEYS and all(set(row) == HISTORY_KEYS for row in state["history"])
        assert (state["state"], state["authority"], state["basis"]) == (
            "verified_passed", "independently_verified", "ci")
        assert state["artifact_sha"] == SHA and (state["checks_passed"], state["checks_attempted"]) == (2, 2)
        # The original session is carried as what it was, not as what it became.
        assert (state["session_status"], state["session_source"]) == ("passed", "agent_reported")
        assert [(h["kind"], h["source"], h["status"]) for h in state["history"]] == [
            ("session", "agent_reported", "passed"),
            ("rerun", "directly_observed", "passed"),
            ("ci", "independently_verified", "passed"),
        ]

    def test_structured_fields_only_no_rendered_label_no_output_no_paths(self, repo):
        entry = _agent_session(repo)
        _rerun(repo, entry, "failed", sha=None)
        doc = _envelope(repo)
        assert doc is not None
        blob = json.dumps(doc)
        for forbidden in ("label", "integrity", "stdout", "stderr", "raw_output", str(repo), "https://"):
            assert forbidden not in blob, forbidden
        assert doc["state"]["state"] == "verified_failed" and doc["state"]["artifact_sha"] is None
        assert doc["state"]["failed_checks"] == ["python -m pytest -q"]

    def test_the_receipt_payload_is_unchanged_by_later_evidence(self, repo):
        entry = _agent_session(repo)
        before = envelope.receipt_payload(load_history(repo / RUNS, coerce=True)[-1], 0)
        _rerun(repo, entry)
        _ci(repo, entry, "success")
        after = envelope.receipt_payload(load_history(repo / RUNS, coerce=True)[-1], 0)
        assert after == before
        assert (after["verification"]["status"], after["verification"]["source"]) == ("passed", "agent_reported")
        assert envelope.payload_hash(after) == envelope.payload_hash(before)

    def test_an_attestation_without_a_usable_id_is_left_out(self, repo):
        entry = _agent_session(repo)
        good = _rerun(repo, entry)
        ps.record_attestation(repo, {**_rerun_dict(entry), "attestation_id": "not-an-id"})
        doc = _envelope(repo)
        assert doc is not None and [e["attestation_id"] for e in doc["evidence"]] == [good["attestation_id"]]

    def test_nothing_is_sent_when_the_state_rests_on_evidence_that_cannot_be_sent(self, repo):
        entry = _agent_session(repo)
        # The only conclusive attestation has no usable id: the state would rest on it.
        ps.record_attestation(repo, {**_rerun_dict(entry), "attestation_id": None})
        assert _envelope(repo) is None

    def test_cancelled_ci_is_sent_as_history_and_the_state_stays_the_sessions(self, repo):
        entry = _agent_session(repo)
        _ci(repo, entry, "cancelled")
        doc = _envelope(repo)
        assert doc is not None and doc["evidence"][0]["verification"]["status"] == "unknown"
        assert (doc["state"]["state"], doc["state"]["basis"]) == ("agent_reported_passed", "session")
        assert doc["state"]["effective_status"] == "unknown"  # an agent's pass never becomes a pass


def _rerun_dict(entry: dict) -> dict:
    check = ps.PlannedCheck(name="pytest", argv=["pytest"], kind="test", origin="contract", safety="safe", reason="")
    tree = ps.TreeState(head=SHA, dirty=False, tracked_dirty=False)
    return ps.build_attestation(
        entry, [ps.CheckRun(check, "passed", exit_code=0)], before=tree, after=tree,
        started_at="2026-09-30T14:43:00Z", completed_at="2026-09-30T14:43:00Z",
    )


# ---------------------------------------------------------------------------
# flush
# ---------------------------------------------------------------------------


class TestEvidenceFlush:
    def test_evidence_follows_an_already_synced_receipt_and_is_sent_once(self, repo, env, link, recording):
        entry = _agent_session(repo)
        first = client.flush(repo, env=env)
        assert (first.created, first.evidence_sent) == (1, 0)

        _rerun(repo, entry)
        second = client.flush(repo, env=env)
        assert (second.sent, second.evidence_sent, second.evidence_recorded) == (0, 1, 1)
        assert recording.evidence_envelopes[0]["state"]["basis"] == "post_session"

        # Repeated sync: nothing is sent again, for the receipt or the evidence.
        for _ in range(3):
            again = client.flush(repo, env=env)
            assert (again.sent, again.evidence_sent) == (0, 0)
        assert len(recording.envelopes) == 1 and len(recording.evidence_envelopes) == 1

        # CI arrives later: one more evidence request, carrying both attestations.
        _ci(repo, entry, "success", "success")
        third = client.flush(repo, env=env)
        assert (third.sent, third.evidence_sent, third.evidence_recorded) == (0, 1, 1)
        latest = recording.evidence_envelopes[-1]
        assert [e["kind"] for e in latest["evidence"]] == ["post_session_verification", "ci_verification"]
        assert latest["state"]["basis"] == "ci"
        assert len(recording.envelopes) == 1  # the Receipt was never resent
        assert client.flush(repo, env=env).evidence_sent == 0

        record = outbox.load_outbox(repo)[entry["receipt_id"]]
        assert record["state"] == "synced" and record["evidence_state"] == "synced"
        assert record["evidence_hash"] == sync_evidence.evidence_hash(latest)

    def test_receipt_and_its_evidence_can_go_in_the_same_flush(self, repo, env, link, recording):
        entry = _agent_session(repo)
        _rerun(repo, entry)
        report = client.flush(repo, env=env)
        assert (report.created, report.evidence_recorded) == (1, 1)
        assert recording.evidence_envelopes[0]["receipt_id"] == recording.envelopes[0]["receipt"]["receipt_id"]

    def test_a_platform_without_the_route_is_skipped_quietly(self, repo, env, link):
        entry = _agent_session(repo)
        _rerun(repo, entry)
        old = transport.RecordingPlatformTransport(
            evidence_results=[SendResult(transport.KIND_UNSUPPORTED, 404, "not_found")])
        report = client.flush(repo, env=env, transport=old)
        assert report.created == 1 and report.evidence_unsupported is True and report.stopped is None
        assert transport.in_backoff(env) is None  # not a link failure
        assert "evidence_hash" not in outbox.load_outbox(repo)[entry["receipt_id"]]
        # Once the Platform accepts evidence, the same evidence is sent.
        new = transport.RecordingPlatformTransport()
        assert client.flush(repo, env=env, transport=new).evidence_recorded == 1

    def test_a_transport_that_cannot_send_evidence_is_left_alone(self, repo, env, link):
        class ReceiptsOnly:
            def __init__(self):
                self.sent = 0

            def send(self, _envelope):
                self.sent += 1
                return SendResult(transport.KIND_CREATED, 201)

        entry = _agent_session(repo)
        _rerun(repo, entry)
        sender = ReceiptsOnly()
        report = client.flush(repo, env=env, transport=sender)
        assert (report.created, report.evidence_sent, sender.sent) == (1, 0, 1)

    def test_receipt_not_hosted_yet_is_retried_on_the_next_flush(self, repo, env, link):
        entry = _agent_session(repo)
        _rerun(repo, entry)
        pending = transport.RecordingPlatformTransport(
            evidence_results=[SendResult(transport.KIND_RECEIPT_PENDING, 404, "receipt_not_found"),
                              SendResult(transport.KIND_CREATED, 201)])
        assert client.flush(repo, env=env, transport=pending).evidence_recorded == 0
        assert transport.in_backoff(env) is None
        assert client.flush(repo, env=env, transport=pending).evidence_recorded == 1

    @pytest.mark.parametrize(("kind", "status", "code", "state"), [
        (transport.KIND_CONFLICT, 409, "verification_evidence_conflict", "conflict"),
        (transport.KIND_REJECTED, 400, "invalid_payload", "rejected"),
    ])
    def test_a_refusal_is_recorded_once_and_retried_only_with_new_evidence(
        self, repo, env, link, kind, status, code, state,
    ):
        entry = _agent_session(repo)
        _rerun(repo, entry)
        refusing = transport.RecordingPlatformTransport(evidence_results=[
            SendResult(kind, status, code, [{"path": "state.basis", "message": "no"}]),
            SendResult(transport.KIND_CREATED, 201),
        ])
        report = client.flush(repo, env=env, transport=refusing)
        assert report.evidence_not_accepted == 1 and report.stopped is None
        record = outbox.load_outbox(repo)[entry["receipt_id"]]
        assert record["state"] == "synced"  # the Receipt's own sync state is untouched
        assert record["evidence_state"] == state and record["evidence_error"]["code"] == code
        assert client.flush(repo, env=env, transport=refusing).evidence_sent == 0  # same evidence: never retried

        _ci(repo, entry, "success")
        assert client.flush(repo, env=env, transport=refusing).evidence_recorded == 1
        assert outbox.load_outbox(repo)[entry["receipt_id"]]["evidence_error"] is None

    def test_unavailable_backs_off_and_a_bad_key_pauses_the_link(self, repo, env, link):
        entry = _agent_session(repo)
        _rerun(repo, entry)
        down = transport.RecordingPlatformTransport(evidence_results=[SendResult(transport.KIND_UNAVAILABLE, 503)])
        assert client.flush(repo, env=env, transport=down).stopped == "paused: unavailable"
        assert transport.in_backoff(env) == "unavailable"
        transport.clear_backoff(env)
        revoked = transport.RecordingPlatformTransport(evidence_results=[SendResult(transport.KIND_UNAUTHORIZED, 401)])
        assert client.flush(repo, env=env, transport=revoked).stopped == "paused: unauthorized"
        assert "evidence_hash" not in outbox.load_outbox(repo)[entry["receipt_id"]]

    def test_no_evidence_for_a_receipt_that_changed_locally_after_sync(self, repo, env, link, recording):
        entry = _agent_session(repo)
        client.flush(repo, env=env)
        amend_latest_record(repo / RUNS, "note", lambda rec: rec.__setitem__("note", "reviewed"))
        _rerun(repo, entry)
        report = client.flush(repo, env=env)
        assert report.stale == 1 and report.evidence_sent == 0 and recording.evidence_envelopes == []

    def test_evidence_state_does_not_leak_to_another_organisation(self, repo, env, link, recording):
        entry = _agent_session(repo)
        _rerun(repo, entry)
        client.flush(repo, env=env)
        other = config.save_link(endpoint=ENDPOINT, organisation_id="11111111-2222-4333-8444-555555555555",
                                 api_key=KEY, env=env)
        report = client.flush(repo, env=env, link=other)
        assert (report.created, report.evidence_recorded) == (1, 1)  # sent afresh there
        assert len(recording.evidence_envelopes) == 2


# ---------------------------------------------------------------------------
# transport + CLI
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    status = 201
    body = b"{}"
    seen: list[dict] = []

    def do_POST(self):  # noqa: N802 - http.server API
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(raw)})
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *_args):
        return


@pytest.fixture
def server():
    _Handler.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


class TestEvidenceTransport:
    def test_classification_never_treats_a_404_as_a_broken_link(self):
        pending = transport.classify_evidence_status(404, b'{"error":{"code":"receipt_not_found","message":"m"}}')
        assert pending.kind == transport.KIND_RECEIPT_PENDING
        assert transport.classify_evidence_status(404, b'{"error":{"code":"not_found"}}').kind == transport.KIND_UNSUPPORTED
        assert transport.classify_evidence_status(404, b"<html>").kind == transport.KIND_UNSUPPORTED
        assert transport.classify_evidence_status(405).kind == transport.KIND_UNSUPPORTED
        assert transport.classify_evidence_status(201).kind == "created"
        assert transport.classify_evidence_status(200).kind == "duplicate"
        assert transport.classify_evidence_status(409).kind == "conflict"
        assert transport.classify_evidence_status(400).kind == "rejected"
        assert transport.classify_evidence_status(401).kind == "unauthorized"
        assert transport.classify_evidence_status(503).kind == "unavailable"

    def test_posts_to_the_receipts_evidence_route_with_the_bearer_key(self, server):
        port = server.server_address[1]
        link = config.PlatformLink(endpoint=f"http://127.0.0.1:{port}", organisation_id=ORG, api_key=KEY,
                                   linked_at=None, source="file")
        t = transport.HttpsPlatformTransport(link, user_agent="openshard/test", timeout=5.0)
        rid = "rcpt_" + "a" * 32
        _Handler.status, _Handler.body = 201, b"{}"
        assert t.send_evidence(rid, {"contract": "openshard.verification-evidence"}).kind == "created"
        seen = _Handler.seen[-1]
        assert seen["path"] == f"/v1/orgs/{ORG}/receipts/{rid}/verification-evidence"
        assert seen["auth"] == f"Bearer {KEY}" and seen["body"] == {"contract": "openshard.verification-evidence"}
        _Handler.status, _Handler.body = 404, b'{"error":{"code":"receipt_not_found","message":"m"}}'
        assert t.send_evidence(rid, {}).kind == transport.KIND_RECEIPT_PENDING
        _Handler.status, _Handler.body = 404, b'{"error":{"code":"not_found","message":"Route not found."}}'
        assert t.send_evidence(rid, {}).kind == transport.KIND_UNSUPPORTED


class TestCli:
    def test_sync_now_reports_evidence_updates(self, repo, env, monkeypatch, recording):
        from openshard.cli.main import cli

        monkeypatch.setenv("OPENSHARD_HOME", env["OPENSHARD_HOME"])
        monkeypatch.chdir(repo)
        config.save_link(endpoint=ENDPOINT, organisation_id=ORG, api_key=KEY)
        entry = _agent_session(repo)
        assert "Sent 1: 1 new" in CliRunner().invoke(cli, ["sync", "now"], catch_exceptions=False).output
        _rerun(repo, entry)
        out = CliRunner().invoke(cli, ["sync", "now"], catch_exceptions=False).output
        assert "Sent 0" in out and "Verification evidence: 1 Receipt(s) updated." in out
        payload = json.loads(CliRunner().invoke(cli, ["sync", "now", "--json"], catch_exceptions=False).output)
        assert payload["evidence_sent"] == 0 and payload["evidence_unsupported"] is False
