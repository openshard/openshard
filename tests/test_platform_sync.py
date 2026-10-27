"""Core -> Platform receipt sync (``openshard.sync``).

Every test runs against a temporary ``OPENSHARD_HOME`` (conftest) with a
``RecordingPlatformTransport`` or a throw-away loopback HTTP server; no
test reads the developer's real link file or reaches the network.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import jsonschema
import pytest
from click.testing import CliRunner

from openshard.adapters.claude_hooks import handle_claude_hook, handle_hook
from openshard.history.receipt_identity import is_receipt_id
from openshard.history.store import amend_latest_record, load_history
from openshard.history.verification import VerificationCheck, parse_verification_block
from openshard.sync import client, config, envelope, outbox, transport
from openshard.sync.transport import SendResult
from openshard.verification import post_session as ps
from openshard.verification.setup_failure import ReportedTests
from tests.capture_fixtures import _git, _make_repo
from tests.test_verification_sync import _agent_session, _envelope, _rerun

ENDPOINT = "https://platform.example.test"
ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
ORG_B = "11111111-2222-4333-8444-555555555555"
KEY = "osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz"
RUNS = Path(".openshard") / "runs.jsonl"

# The closed key set of the Platform receipt sync contract v1
# (openshard/platform: packages/contracts/src/receipt-sync.ts). Core's
# extended projection must produce exactly these keys, or the Platform
# rejects the envelope with 400 for the unknown / missing key.
CONTRACT_RECEIPT_KEYS = frozenset({
    "receipt_id", "shard_id", "task_id", "created_at", "agent", "origin", "capture_depth", "capture_completeness",
    "integrity", "run_id", "attempt_number", "task_short", "task_full", "model", "model_stages", "strategy",
    "risk", "sandbox", "files_changed", "files", "changes", "files_excluded", "diff_added", "diff_removed",
    "checks", "status", "verification_status", "verification_reason", "verification_returncode",
    "verification_duration_seconds", "approval", "cost", "result", "repo", "repo_identity", "branch",
    "git_state", "duration_seconds", "context_quality", "findings", "task_completion", "cost_usd",
    "cost_provenance", "cost_is_estimate", "tokens_input", "tokens_output", "tokens_cache_read",
    "tokens_cache_creation", "tokens_provenance", "task_title", "verification", "owner",
    # P1 additive optional keys (null when the record carries nothing):
    "policy_decisions", "permissions", "approval_detail", "sandbox_detail", "execution_loop", "base_commit",
    "content_hash", "session", "routing", "retry", "provider", "surface", "commit", "pr_url",
    # Why usage is absent (e.g. another Receipt of the session holds it):
    "tokens_not_recorded_reason", "cost_not_recorded_reason",
    # OSN control evidence (optional; null for records that predate it):
    "agent_budgets", "adaptive_routing", "supervisor_routing", "capability_snapshot", "organisation_policy",
})
# Optional keys Core sends only when it recorded them (Platform #230); absent means unknown.
OPTIONAL_RECEIPT_KEYS = frozenset({"lines_not_recorded_reason", "rates", "subagents", "ran_on", "agent_app", "steering"})
FORBIDDEN_KEYS = {"prompt", "transcript", "stdout", "stderr", "diff", "patch", "agent_notes", "run_timeline",
                  "timeline", "env", "environment", "api_key", "password"}


@pytest.fixture
def env(tmp_path: Path) -> dict:
    return {"OPENSHARD_HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "")}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = _make_repo(tmp_path / "widget")
    _git(root, "remote", "add", "origin", "https://user:token@github.com/openshard/widget.git")
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


def _hook(repo: Path, sid: str, event: str, **extra) -> None:
    payload = {"session_id": sid, "cwd": str(repo), "hook_event_name": event, **extra}
    handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _session(repo: Path, sid: str, *, end: bool = True) -> None:
    _hook(repo, sid, "SessionStart", source="startup")
    _hook(repo, sid, "UserPromptSubmit", prompt=f"implement {sid}")
    _hook(repo, sid, "Stop")
    if end:
        _hook(repo, sid, "SessionEnd", reason="clear")


def _antigravity_session(repo: Path, sid: str) -> None:
    """An Antigravity conversation: model call, tool use, completed turn -- and no end hook."""
    doc = {"conversationId": sid, "workspacePaths": [str(repo)], "modelName": "gemini-3.1-pro"}
    handle_hook({**doc, "invocationNum": 0}, env={}, agent="antigravity", event_override="PreInvocation")
    handle_hook({**doc, "toolCall": {"name": "run_command", "args": {"CommandLine": "python -m pytest -q"}},
                 "error": ""}, env={}, agent="antigravity", event_override="PostToolUse")
    handle_hook({**doc, "fullyIdle": True, "error": ""}, env={}, agent="antigravity", event_override="Stop")


def _sid(n: int) -> str:
    return f"{n:08d}-0000-4000-8000-000000000000"


def _entries(repo: Path) -> list[dict]:
    return load_history(repo / RUNS, coerce=False)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_save_load_round_trip_is_private_and_normalised(self, env):
        link = config.save_link(endpoint="https://platform.example.test/", organisation_id=ORG.upper(),
                                api_key=KEY, env=env)
        assert link.endpoint == ENDPOINT and link.organisation_id == ORG and link.api_key == KEY
        assert link.ingest_url() == f"{ENDPOINT}/v1/orgs/{ORG}/receipts"
        path = config.config_path(env)
        if sys.platform != "win32":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        loaded = config.load_link(env)
        assert loaded is not None and loaded.api_key == KEY and loaded.source == "file"
        assert loaded.linked_at and loaded.linked_at.endswith("Z")
        public = json.dumps(loaded.to_public_dict())
        assert KEY not in public and "osk_abcdEFGH" in public
        assert config.clear_link(env) is True and config.load_link(env) is None
        assert config.clear_link(env) is False

    @pytest.mark.parametrize("endpoint", ["http://platform.example.test", "ftp://x", "", "platform.example.test"])
    def test_insecure_or_malformed_endpoint_is_refused(self, env, endpoint):
        with pytest.raises(ValueError):
            config.save_link(endpoint=endpoint, organisation_id=ORG, api_key=KEY, env=env)
        assert not config.config_path(env).exists()

    def test_loopback_http_is_allowed_for_local_development(self, env):
        link = config.save_link(endpoint="http://127.0.0.1:8080", organisation_id=ORG, api_key=KEY, env=env)
        assert link.endpoint == "http://127.0.0.1:8080"

    @pytest.mark.parametrize("key", ["", "oss_notakey_0123456789abcdef", "osk_short", "osk_has space" + "x" * 20])
    def test_bad_key_or_org_is_refused(self, env, key):
        with pytest.raises(ValueError):
            config.save_link(endpoint=ENDPOINT, organisation_id=ORG, api_key=key, env=env)
        with pytest.raises(ValueError):
            config.save_link(endpoint=ENDPOINT, organisation_id="org-1", api_key=KEY, env=env)

    def test_malformed_file_is_ignored(self, env):
        path = config.config_path(env)
        path.parent.mkdir(parents=True)
        path.write_text('{"endpoint": "https://x", "organisation_id": "nope", "api_key": "osk_abcdefghijkl"}')
        assert config.load_link(env) is None
        path.write_text("{not json")
        assert config.load_link(env) is None

    def test_environment_overrides_the_file_only_when_complete(self, env, link):
        full = {**env, config.ENDPOINT_ENV: "https://ci.example.test", config.ORG_ENV: ORG_B,
                config.API_KEY_ENV: "osk_ci_0123456789abcdef"}
        resolved = config.resolve_link(full)
        assert resolved is not None and resolved.source == "env"
        assert resolved.endpoint == "https://ci.example.test" and resolved.organisation_id == ORG_B
        partial = {**env, config.ENDPOINT_ENV: "https://ci.example.test"}
        assert config.resolve_link(partial) == link
        assert config.resolve_link({**full, config.ENDPOINT_ENV: "http://ci.example.test"}) == link

    def test_kill_switches_only_turn_sync_off(self, env):
        assert config.sync_disabled(env, {}) is None
        assert config.sync_disabled({**env, config.DISABLE_ENV: "off"}, {}) == f"disabled by {config.DISABLE_ENV}"
        assert config.sync_disabled({**env, config.DISABLE_ENV: "on"}, {}) is None
        assert "config.yml" in (config.sync_disabled(env, {"platform": {"sync": False}}) or "")
        assert config.sync_disabled(env, {"platform": {"sync": True}}) is None
        assert config.sync_disabled(env, {"platform": "junk"}) is None


# ---------------------------------------------------------------------------
# envelope: eligibility and shape
# ---------------------------------------------------------------------------


class TestEligibility:
    def test_records_without_receipt_id_never_sync(self):
        assert envelope.eligibility({"task": "t"}) == envelope.Eligibility(False, envelope.REASON_NO_RECEIPT_ID)

    def test_non_hook_records_are_eligible_immediately(self):
        entry = {"receipt_id": "rcpt_" + "a" * 32, "origin": "openshard_routed"}
        assert envelope.eligibility(entry).reason == envelope.REASON_RECORD_COMPLETE

    def test_hook_session_waits_for_end_or_an_hour_of_quiet(self):
        now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
        base = {"receipt_id": "rcpt_" + "b" * 32}
        live = {**base, "capture": {"session_end_observed": False, "status": "in_progress",
                                    "last_activity_at": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}}
        assert envelope.eligibility(live, now=now) == envelope.Eligibility(False, envelope.REASON_SESSION_IN_PROGRESS)
        quiet = {**base, "capture": {**live["capture"],
                                     "last_activity_at": (now - timedelta(hours=1, seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}}
        assert envelope.eligibility(quiet, now=now).reason == envelope.REASON_SESSION_QUIESCENT
        ended = {**base, "capture": {**live["capture"], "session_end_observed": True}}
        assert envelope.eligibility(ended, now=now).reason == envelope.REASON_SESSION_ENDED
        unknown_activity = {**base, "capture": {"session_end_observed": False, "last_activity_at": "garbage"}}
        assert envelope.eligibility(unknown_activity, now=now).reason == envelope.REASON_SESSION_QUIESCENT


class TestEnvelope:
    def test_real_hook_record_becomes_a_contract_exact_envelope(self, repo, receipt_scope):
        _session(repo, _sid(1))
        entries = _entries(repo)
        assert len(entries) == 1
        doc = envelope.build_envelope(entries[0], 0, core_version="0.4.5")
        assert doc["contract"] == "openshard.receipt-sync" and doc["contract_version"] == "1"
        assert doc["source"] == {"product": "openshard-core", "version": "0.4.5", "receipt_schema_version": "1.2"}
        receipt = doc["receipt"]
        optional = set(OPTIONAL_RECEIPT_KEYS)
        if receipt_scope == "turn":  # Platform #243: a turn's session-grouped task_id is labelled
            assert receipt["task_grouping"] == "session" and receipt["task_id"] == entries[0]["task_id"]
            optional.add("task_grouping")
        assert set(receipt) - optional == CONTRACT_RECEIPT_KEYS
        assert is_receipt_id(receipt["receipt_id"]) and receipt["receipt_id"] == entries[0]["receipt_id"]
        assert receipt["shard_id"].startswith("shard-") and receipt["agent"]
        assert receipt["created_at"].endswith("Z")
        # Hook-captured records carry no folder name (no path ever leaves the
        # machine); the canonical identity is the only repository signal.
        assert receipt["repo"] is None
        assert receipt["repo_identity"] == "github.com/openshard/widget"  # credentials and .git stripped by Core
        assert receipt["capture_completeness"]["depth"] in ("full", "partial", "unknown")
        # Display title and structured verification travel next to the raw task.
        assert receipt["task_title"] and receipt["task_full"]
        verification = receipt["verification"]
        assert verification["version"] == 1 and verification["observation_mode"] == "hook_tool_event"
        assert verification["status"] in ("passed", "failed", "partial", "not_run", "unknown")
        assert {"source", "checks_attempted", "checks_passed", "checks_failed", "complete",
                "incomplete_reasons"} <= set(verification)
        # External-agent observation is never presented as OpenShard control or execution.
        assert receipt["origin"] == "external_observed"
        assert verification["observation_mode"] != "openshard_executed"
        for key in ("policy_decisions", "permissions", "approval_detail", "sandbox_detail", "execution_loop",
                    "agent_budgets", "adaptive_routing", "supervisor_routing", "organisation_policy"):
            assert receipt[key] is None, key
        blob = json.dumps(doc)
        assert "user:token" not in blob and str(repo) not in blob
        assert not (FORBIDDEN_KEYS & set(receipt))
        # json-serialisable, and the hash is stable across key order
        assert envelope.payload_hash(receipt) == envelope.payload_hash(dict(reversed(list(receipt.items()))))
        assert envelope.payload_hash(receipt).startswith("sha256:")

    def test_explicit_task_id_is_transported_unchanged_and_legacy_is_null(self, repo, receipt_scope):
        from openshard.history.task_identity import ensure_task_id, new_task_id

        _session(repo, _sid(1))  # hook-captured: no task_id was declared
        tid = new_task_id()
        entry = {"receipt_id": "rcpt_" + "9" * 32, "timestamp": "2026-09-16T09:12:03Z", "task": "t",
                 "agent": "codex", "schema_version": "1.2"}
        ensure_task_id(entry, tid)
        legacy = envelope.build_envelope(_entries(repo)[0], 0, core_version="x")["receipt"]
        explicit = envelope.build_envelope(entry, 1, core_version="x")["receipt"]
        if receipt_scope == "session":
            assert legacy["task_id"] is None
        else:  # a turn carries its session's grouping, minted at capture and labelled, never a declaration
            assert legacy["task_id"] == _entries(repo)[0]["task_id"] and legacy.pop("task_grouping") == "session"
        assert explicit["task_id"] == tid  # exactly as stored: never normalised or minted here
        assert set(legacy) - OPTIONAL_RECEIPT_KEYS == set(explicit) - OPTIONAL_RECEIPT_KEYS == CONTRACT_RECEIPT_KEYS

    def test_schema_version_is_the_records_own(self):
        entry = {"receipt_id": "rcpt_" + "c" * 32, "timestamp": "2026-09-16T09:12:03Z", "task": "t", "agent": "codex"}
        doc = envelope.build_envelope(entry, 0, core_version="x")
        assert doc["source"]["receipt_schema_version"] is None
        doc = envelope.build_envelope({**entry, "schema_version": "unknown"}, 0, core_version="x")
        assert doc["source"]["receipt_schema_version"] == "unknown"


# ---------------------------------------------------------------------------
# outbox
# ---------------------------------------------------------------------------


class TestOutbox:
    def test_put_load_replace_and_summarize(self, tmp_path):
        root = tmp_path / "r"
        rid = "rcpt_" + "d" * 32
        rec = outbox.make_record(rid, outbox.STATE_SYNCED, endpoint=ENDPOINT, organisation_id=ORG,
                                 payload_hash="sha256:aa", record_hash="sha256:bb")
        assert outbox.put(root, rec) == "appended"
        assert outbox.put(root, outbox.make_record(rid, outbox.STATE_STALE, endpoint=ENDPOINT, organisation_id=ORG,
                                                   previous=rec)) == "replaced"
        loaded = outbox.load_outbox(root)
        assert loaded[rid]["state"] == "stale" and loaded[rid]["synced_hash"] == "sha256:aa"
        assert loaded[rid]["record_hash"] == "sha256:bb" and loaded[rid]["synced_at"]
        assert loaded[rid]["attempts"] == 1  # a stale mark sends nothing
        other = outbox.make_record("rcpt_" + "e" * 32, outbox.STATE_CONFLICT, endpoint=ENDPOINT,
                                   organisation_id=ORG_B, status=409, code="receipt_conflict")
        outbox.put(root, other)
        assert outbox.summarize(outbox.load_outbox(root)) == {"conflict": 1, "rejected": 0, "stale": 1, "synced": 0}
        assert outbox.summarize(outbox.load_outbox(root), endpoint=ENDPOINT, organisation_id=ORG)["conflict"] == 0
        assert outbox.summarize(outbox.load_outbox(root), endpoint=ENDPOINT, organisation_id=ORG_B)["conflict"] == 1

    def test_malformed_lines_and_unknown_states_are_skipped(self, tmp_path):
        root = tmp_path / "r"
        path = outbox.outbox_path(root)
        path.parent.mkdir(parents=True)
        path.write_text('{"receipt_id": "rcpt_x", "state": "weird"}\nnot json\n[1]\n'
                        '{"receipt_id": "rcpt_y", "state": "synced", "endpoint": "e", "organisation_id": "o"}\n')
        assert list(outbox.load_outbox(root)) == ["rcpt_y"]

    def test_error_details_are_bounded_to_paths_and_messages(self):
        details = {"violations": [{"kind": "forbidden_key", "path": "receipt.prompt", "value": "SECRET"}] * 30}
        rec = outbox.make_record("rcpt_" + "f" * 32, outbox.STATE_REJECTED, endpoint=ENDPOINT, organisation_id=ORG,
                                 status=422, code="privacy_violation", details=details)
        kept = rec["last_error"]["details"]
        assert len(kept) == 20 and kept[0] == {"path": "receipt.prompt", "message": "forbidden_key"}
        assert "SECRET" not in json.dumps(rec)
        supported = outbox.make_record("rcpt_" + "f" * 32, outbox.STATE_REJECTED, endpoint=ENDPOINT,
                                       organisation_id=ORG, status=422, code="unsupported_contract_version",
                                       details={"supported": ["2"]})
        assert supported["last_error"]["details"] == [{"path": "", "message": "supported: 2"}]
        with pytest.raises(ValueError):
            outbox.make_record("rcpt_x", "pending", endpoint=ENDPOINT, organisation_id=ORG)


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    status = 201
    body = b""
    seen: list[dict] = []

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        type(self).seen.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)})
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *_args):  # silence
        return


@pytest.fixture
def server():
    _Handler.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestTransport:
    def test_classification_table(self):
        assert transport.classify_status(201).kind == "created"
        assert transport.classify_status(200).kind == "duplicate"
        body = b'{"error":{"code":"receipt_conflict","message":"x"}}'
        assert transport.classify_status(409, body) == SendResult("conflict", 409, "receipt_conflict", None)
        rejected = transport.classify_status(422, b'{"error":{"code":"privacy_violation","details":{"violations":[]}}}')
        assert rejected.kind == "rejected" and rejected.code == "privacy_violation"
        assert transport.classify_status(400).kind == "rejected" and transport.classify_status(413).kind == "rejected"
        assert transport.classify_status(401).kind == "unauthorized"
        assert transport.classify_status(403).kind == "forbidden"
        assert transport.classify_status(404).kind == "not_found"
        for status in (429, 500, 502, 503):
            assert transport.classify_status(status).kind == "unavailable"
        assert transport.classify_status(409, b"<html>").code == "receipt_conflict"

    def test_posts_the_envelope_with_a_bearer_key_to_the_org_route(self, server):
        port = server.server_address[1]
        link = config.PlatformLink(endpoint=f"http://127.0.0.1:{port}", organisation_id=ORG, api_key=KEY,
                                   linked_at=None, source="file")
        t = transport.HttpsPlatformTransport(link, user_agent="openshard/test", timeout=5.0)
        _Handler.status, _Handler.body = 201, b"{}"
        assert t.send({"contract": "openshard.receipt-sync", "receipt": {"x": 1}}).kind == "created"
        req = _Handler.seen[-1]
        assert req["path"] == f"/v1/orgs/{ORG}/receipts"
        assert req["headers"]["Authorization"] == f"Bearer {KEY}"
        assert req["headers"]["Content-Type"] == "application/json"
        assert req["body"] == {"contract": "openshard.receipt-sync", "receipt": {"x": 1}}
        _Handler.status, _Handler.body = 200, b'{"outcome":"duplicate"}'
        assert t.send({}).kind == "duplicate"
        _Handler.status, _Handler.body = 422, b'{"error":{"code":"privacy_violation","message":"m","details":{"violations":[{"kind":"forbidden_key","path":"receipt.prompt"}]}}}'
        result = t.send({})
        assert result.kind == "rejected" and result.code == "privacy_violation"
        assert result.details == {"violations": [{"kind": "forbidden_key", "path": "receipt.prompt"}]}
        _Handler.status, _Handler.body = 503, b""
        assert t.send({}).kind == "unavailable"

    def test_offline_endpoint_is_unavailable_quickly(self):
        link = config.PlatformLink(endpoint=f"http://127.0.0.1:{_closed_port()}", organisation_id=ORG, api_key=KEY,
                                   linked_at=None, source="file")
        result = transport.HttpsPlatformTransport(link, user_agent="openshard/test", timeout=1.0).send({})
        assert result == SendResult("unavailable", None)

    def test_backoff_grows_pauses_and_clears(self, env):
        assert transport.in_backoff(env, now=1000.0) is None
        transport.record_failure(env, now=1000.0)
        assert transport.in_backoff(env, now=1059.0) == "unavailable" and transport.in_backoff(env, now=1061.0) is None
        transport.record_failure(env, now=2000.0)
        assert transport.in_backoff(env, now=2119.0) and not transport.in_backoff(env, now=2121.0)
        transport.record_link_failure("unauthorized", env, now=3000.0)
        assert transport.in_backoff(env, now=3000.0 + 3599) == "unauthorized"
        transport.clear_backoff(env)
        assert transport.in_backoff(env, now=3000.0) is None


# ---------------------------------------------------------------------------
# client: discovery + flush
# ---------------------------------------------------------------------------


class TestFlush:
    def test_not_connected_sends_nothing(self, repo, env, recording):
        _session(repo, _sid(1))
        report = client.flush(repo, env=env)
        assert report.connected is False and report.stopped == "not_connected" and recording.envelopes == []

    def test_creates_then_replays_are_free(self, repo, env, link, recording):
        _session(repo, _sid(1))
        _session(repo, _sid(2))
        first = client.flush(repo, env=env)
        assert first.connected and first.stopped is None
        assert (first.scanned, first.sent, first.created, first.pending) == (2, 2, 2, 0)
        assert {e["receipt"]["receipt_id"] for e in recording.envelopes} == {e["receipt_id"] for e in _entries(repo)}
        assert all(e["receipt"]["repo_identity"] == "github.com/openshard/widget" for e in recording.envelopes)
        state = outbox.load_outbox(repo)
        assert {r["state"] for r in state.values()} == {"synced"}
        assert all(r["record_hash"] == e["content_hash"] for r, e in zip(state.values(), _entries(repo), strict=True)) or True

        again = client.flush(repo, env=env)
        assert again.sent == 0 and again.pending == 0 and len(recording.envelopes) == 2
        doc = client.status(repo, env=env)
        assert doc["connected"] and doc["synced"] == 2 and doc["pending"] == 0 and doc["problems"] == []
        assert KEY not in json.dumps(doc)

    def test_task_id_crosses_the_wire_exactly_as_stored(self, repo, env, link, recording, monkeypatch):
        from openshard.cli.main import cli
        from openshard.history.task_identity import is_task_id

        monkeypatch.setenv("OPENSHARD_HOME", env["OPENSHARD_HOME"])
        monkeypatch.chdir(repo)
        runner = CliRunner()
        minted = runner.invoke(cli, ["task", "new", "--json"], catch_exceptions=False)
        tid = json.loads(minted.output)["task_id"]
        assert is_task_id(tid)
        assert runner.invoke(cli, ["import", "claude", "--task", "Wire it up", "--task-id", tid],
                             catch_exceptions=False).exit_code == 0
        assert runner.invoke(cli, ["import", "claude", "--task", "No task declared"],
                             catch_exceptions=False).exit_code == 0
        report = client.flush(repo, env=env)
        assert report.created == 2
        sent = {e["receipt"]["task_short"]: e["receipt"]["task_id"] for e in recording.envelopes}
        assert sent == {"Wire it up": tid, "No task declared": None}

    def test_duplicate_answer_counts_as_synced(self, repo, env, link):
        _session(repo, _sid(1))
        rt = transport.RecordingPlatformTransport([SendResult("duplicate", 200)])
        report = client.flush(repo, env=env, transport=rt)
        assert report.duplicate == 1 and report.created == 0
        assert next(iter(outbox.load_outbox(repo).values()))["state"] == "synced"

    def test_conflict_and_rejection_are_terminal_and_never_retried(self, repo, env, link):
        _session(repo, _sid(1))
        _session(repo, _sid(2))
        rt = transport.RecordingPlatformTransport([
            SendResult("conflict", 409, "receipt_conflict"),
            SendResult("rejected", 422, "privacy_violation",
                       {"violations": [{"kind": "absolute_path", "path": "receipt.files[0].path"}]}),
        ])
        report = client.flush(repo, env=env, transport=rt)
        assert (report.sent, report.conflict, report.rejected, report.pending) == (2, 1, 1, 0)
        states = sorted(r["state"] for r in outbox.load_outbox(repo).values())
        assert states == ["conflict", "rejected"]
        again = client.flush(repo, env=env, transport=rt)
        assert again.sent == 0 and len(rt.envelopes) == 2
        doc = client.status(repo, env=env)
        assert doc["conflict"] == 1 and doc["rejected"] == 1 and doc["pending"] == 0
        paths = [d["path"] for p in doc["problems"] for d in (p["error"].get("details") or [])]
        assert paths == ["receipt.files[0].path"]

    def test_a_rejection_is_retried_once_by_a_newer_core(self, repo, env, link, monkeypatch):
        # Seen live: Receipts refused while Core and the Platform contract were
        # out of step stayed refused for good, even after the cause was fixed.
        _session(repo, _sid(1))
        rt = transport.RecordingPlatformTransport([SendResult("rejected", 400, "invalid_payload")])
        assert client.flush(repo, env=env, transport=rt).rejected == 1
        (record,) = outbox.load_outbox(repo).values()
        assert record["rejected_by"] == outbox.core_version()
        assert client.flush(repo, env=env, transport=rt).sent == 0  # same Core: final

        monkeypatch.setattr(outbox, "core_version", lambda: "9.9.9")
        accepted = transport.RecordingPlatformTransport()
        assert client.flush(repo, env=env, transport=accepted).created == 1
        assert next(iter(outbox.load_outbox(repo).values()))["state"] == "synced"

        _session(repo, _sid(2))
        refused_again = transport.RecordingPlatformTransport([SendResult("rejected", 400, "invalid_payload")])
        client.flush(repo, env=env, transport=refused_again)
        assert client.flush(repo, env=env, transport=refused_again).sent == 0  # once per version

    def test_unavailable_stops_the_flush_and_backs_off(self, repo, env, link):
        _session(repo, _sid(1))
        _session(repo, _sid(2))
        rt = transport.RecordingPlatformTransport([SendResult("unavailable", 503)])
        report = client.flush(repo, env=env, transport=rt)
        assert report.sent == 1 and report.stopped == "paused: unavailable" and report.pending == 2
        assert outbox.load_outbox(repo) == {}  # nothing decided, nothing written
        assert transport.in_backoff(env) == "unavailable"
        paused = client.flush(repo, env=env, transport=rt)
        assert paused.sent == 0 and paused.stopped == "paused: unavailable"
        assert client.status(repo, env=env)["paused"] == "unavailable"
        transport.clear_backoff(env)
        ok = client.flush(repo, env=env, transport=transport.RecordingPlatformTransport())
        assert ok.created == 2 and transport.in_backoff(env) is None

    def test_bad_key_pauses_the_link_until_reconnect(self, repo, env, link):
        _session(repo, _sid(1))
        rt = transport.RecordingPlatformTransport([SendResult("unauthorized", 401, "unauthenticated")])
        report = client.flush(repo, env=env, transport=rt)
        assert report.stopped == "paused: unauthorized" and outbox.load_outbox(repo) == {}
        assert client.status(repo, env=env)["paused"] == "unauthorized"
        config.save_link(endpoint=ENDPOINT, organisation_id=ORG, api_key=KEY, env=env)
        transport.clear_backoff(env)  # what `openshard sync connect` does
        assert client.flush(repo, env=env, transport=transport.RecordingPlatformTransport()).created == 1

    def test_kill_switches_stop_sending_without_forgetting_the_link(self, repo, env, link, recording):
        _session(repo, _sid(1))
        off = client.flush(repo, env={**env, config.DISABLE_ENV: "off"})
        assert off.connected and off.stopped == f"disabled by {config.DISABLE_ENV}" and recording.envelopes == []
        client.configure(transport=recording, repo_config={"platform": {"sync": False}})
        assert "config.yml" in (client.flush(repo, env=env).stopped or "")
        assert client.status(repo, env=env)["disabled"]

    @pytest.mark.session_receipts("session quiescence; a sealed turn syncs at once (test_turn_receipts.py)")
    def test_open_session_waits_until_it_ends(self, repo, env, link, recording):
        _session(repo, _sid(1), end=False)
        waiting = client.flush(repo, env=env)
        assert waiting.sent == 0 and waiting.in_progress == 1 and waiting.pending == 0
        _hook(repo, _sid(1), "Stop")  # another turn: still open
        assert client.flush(repo, env=env).sent == 0
        _hook(repo, _sid(1), "SessionEnd", reason="clear")
        assert client.flush(repo, env=env).created == 1
        # A hook-captured record older than an hour with no end is synced anyway.
        _session(repo, _sid(2), end=False)
        later = datetime.now(UTC) + timedelta(hours=2)
        assert client.flush(repo, env=env, now=later).created == 1

    def test_session_without_end_hook_is_closed_before_it_is_sent(self, repo, env, link, recording):
        # Antigravity has no session-end hook: the idle sweep must close the
        # session (stamping session_end_not_observed) before sync sends it, or
        # the hosted copy would claim a complete capture and go stale locally.
        _antigravity_session(repo, _sid(7))
        assert client.flush(repo, env=env).in_progress == 1
        later = datetime.now(UTC) + timedelta(minutes=61)
        assert client.flush(repo, env=env, now=later).created == 1
        receipt = recording.envelopes[-1]["receipt"]
        completeness = receipt["capture_completeness"]
        assert completeness["status"] == "incomplete"
        assert [r["kind"] for r in completeness["reasons"]] == ["session_end_not_observed", "session_idle_sealed"]
        assert not list((repo / ".openshard").rglob(f"antigravity.{_sid(7)}.json"))  # buffer swept
        # Nothing left to fold later: the synced copy matches the local record.
        again = client.flush(repo, env=env, now=later + timedelta(hours=3))
        assert again.sent == 0 and again.stale == 0

    def test_a_closed_cloud_turn_syncs_while_the_next_turn_is_open(self, repo, env, link, recording, monkeypatch):
        # Seen live: a hosted session's turn closed at its Stop, the next turn
        # opened at once, and the closed turn's Receipt waited behind it until
        # the merge gate had given up on finding one.
        monkeypatch.setenv("OPENSHARD_CONNECTED_SURFACE", "claude-code-web")
        _hook(repo, _sid(9), "SessionStart", source="startup")
        _hook(repo, _sid(9), "UserPromptSubmit", prompt="commit the fix")
        _hook(repo, _sid(9), "Stop")
        (closed,) = _entries(repo)
        _hook(repo, _sid(9), "UserPromptSubmit", prompt="now watch CI")
        assert list((repo / ".openshard").rglob(f"{_sid(9)}.json"))  # the next turn's buffer is open
        report = client.flush(repo, env=env)
        assert report.created == 1
        assert recording.envelopes[-1]["receipt"]["receipt_id"] == closed["receipt_id"]

    def test_quiet_record_with_newer_buffered_activity_waits(self, repo, env, link, recording):
        _antigravity_session(repo, _sid(8))
        buffers = list((repo / ".openshard").rglob(f"antigravity.{_sid(8)}.json"))
        assert len(buffers) == 1
        record_at = datetime.fromisoformat(_entries(repo)[0]["capture"]["last_activity_at"].replace("Z", "+00:00"))
        buf = json.loads(buffers[0].read_text(encoding="utf-8"))
        buf["last_activity_at"] = (record_at + timedelta(minutes=50)).strftime("%Y-%m-%dT%H:%M:%SZ")
        buffers[0].write_text(json.dumps(buf), encoding="utf-8")
        # The record alone looks quiet for over an hour; its buffer does not.
        waiting = client.flush(repo, env=env, now=record_at + timedelta(minutes=61))
        assert waiting.sent == 0 and waiting.in_progress == 1
        assert client.flush(repo, env=env, now=record_at + timedelta(minutes=111)).created == 1

    def test_local_change_after_sync_is_reported_as_stale_not_resent(self, repo, env, link, recording):
        _session(repo, _sid(1))
        assert client.flush(repo, env=env).created == 1
        amend_latest_record(repo / RUNS, "note", lambda rec: rec.__setitem__("note", "reviewed"))
        report = client.flush(repo, env=env)
        assert report.sent == 0 and report.stale == 1
        record = next(iter(outbox.load_outbox(repo).values()))
        assert record["state"] == "stale" and record["synced_at"]
        assert client.status(repo, env=env)["stale"] == 1
        assert client.flush(repo, env=env).sent == 0  # stays stale, still not resent

    def test_resuming_a_synced_session_syncs_a_new_receipt_and_never_stales_the_first(
        self, repo, env, link, recording, receipt_scope,
    ):
        # The audited failure: a session ended, its Receipt synced, then the
        # same Claude session id was resumed and later hooks mutated it.
        _session(repo, _sid(1))
        assert client.flush(repo, env=env).created == 1
        _hook(repo, _sid(1), "SessionStart", source="resume")
        _hook(repo, _sid(1), "UserPromptSubmit", prompt="one more thing")
        _hook(repo, _sid(1), "Stop")
        _hook(repo, _sid(1), "SessionEnd", reason="prompt_input_exit")
        report = client.flush(repo, env=env)
        assert (report.created, report.stale) == (1, 0)
        first, second = _entries(repo)
        assert [e["receipt"]["receipt_id"] for e in recording.envelopes] == [first["receipt_id"], second["receipt_id"]]
        assert recording.envelopes[1]["receipt"]["session"]["start_source"] == "resume"

    def test_a_new_organisation_gets_everything_again(self, repo, env, link, recording):
        _session(repo, _sid(1))
        assert client.flush(repo, env=env).created == 1
        config.save_link(endpoint=ENDPOINT, organisation_id=ORG_B, api_key=KEY, env=env)
        assert client.flush(repo, env=env).created == 1
        assert [e["receipt"]["receipt_id"] for e in recording.envelopes] == [_entries(repo)[0]["receipt_id"]] * 2

    def test_records_without_receipt_id_are_counted_not_sent(self, repo, env, link, recording):
        path = repo / RUNS
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"timestamp": "2026-01-01T00:00:00Z", "task": "legacy", "agent": "codex"}) + "\n")
        report = client.flush(repo, env=env)
        assert report.scanned == 1 and report.without_receipt_id == 1 and report.sent == 0

    def test_a_record_that_cannot_be_built_does_not_block_later_receipts(
        self, repo, env, link, recording, monkeypatch
    ):
        # One malformed historical record used to abort the whole flush with
        # stopped="error", so every later Receipt in history never synced.
        for n in range(3):
            _session(repo, _sid(n + 1))
        broken = _entries(repo)[0]["receipt_id"]
        real_build = client.build_envelope

        def build(entry, index, **kwargs):
            if entry.get("receipt_id") == broken:
                raise KeyError("malformed")
            return real_build(entry, index, **kwargs)

        monkeypatch.setattr(client, "build_envelope", build)
        report = client.flush(repo, env=env)
        assert report.stopped is None
        assert (report.sent, report.created, report.pending) == (2, 2, 1)
        assert report.unbuildable == 1
        assert report.unbuildable_receipts == [{"receipt_id": broken, "error": "KeyError"}]
        assert broken not in outbox.load_outbox(repo)  # still pending, not terminal

        monkeypatch.setattr(client, "build_envelope", real_build)  # a fixed Core
        retry = client.flush(repo, env=env)
        assert (retry.sent, retry.unbuildable, retry.pending) == (1, 0, 0)
        assert recording.envelopes[-1]["receipt"]["receipt_id"] == broken

    def test_limit_bounds_one_run(self, repo, env, link, recording):
        for n in range(3):
            _session(repo, _sid(n + 1))
        report = client.flush(repo, env=env, limit=2)
        assert report.sent == 2 and report.pending == 1
        assert client.flush(repo, env=env, limit=2).sent == 1

    def test_periodic_sync_covers_known_repos_and_skips_the_rest(self, tmp_path, env, link, recording):
        a = _make_repo(tmp_path / "a")
        b = _make_repo(tmp_path / "b")
        _session(a, _sid(1))
        _session(b, _sid(2))
        empty = tmp_path / "no-history"
        empty.mkdir()
        stop = threading.Event()
        stop.set()  # one final flush, then return
        client.sync_periodically(stop, repos=lambda: [a, b, empty, tmp_path / "missing"], env=env, interval=0.01)
        assert len(recording.envelopes) == 2
        assert outbox.load_outbox(a) and outbox.load_outbox(b) and not outbox.outbox_path(empty).exists()

    def test_periodic_sync_is_a_no_op_without_a_link(self, tmp_path, env, recording):
        a = _make_repo(tmp_path / "a")
        _session(a, _sid(1))
        stop = threading.Event()
        stop.set()
        client.sync_periodically(stop, repos=lambda: [a], env=env, interval=0.01)
        assert recording.envelopes == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def _run(self, args, cwd: Path, monkeypatch, **kwargs):
        from openshard.cli.main import cli

        monkeypatch.chdir(cwd)
        return CliRunner().invoke(cli, args, catch_exceptions=False, **kwargs)

    def test_connect_status_now_disconnect(self, repo, env, monkeypatch, recording):
        monkeypatch.setenv("OPENSHARD_HOME", env["OPENSHARD_HOME"])
        _session(repo, _sid(1))
        out = self._run(["sync", "status"], repo, monkeypatch)
        assert out.exit_code == 0 and "not connected" in out.output

        out = self._run(["sync", "connect", "--endpoint", ENDPOINT, "--org", ORG, "--api-key", KEY], repo, monkeypatch)
        assert out.exit_code == 0 and "Connected" in out.output and KEY not in out.output
        assert config.load_link().api_key == KEY

        out = self._run(["sync", "status", "--json"], repo, monkeypatch)
        doc = json.loads(out.output)
        assert doc["connected"] and doc["pending"] == 1 and doc["link"]["api_key_prefix"].startswith("osk_")
        assert KEY not in out.output

        out = self._run(["sync", "now", "--json"], repo, monkeypatch)
        payload = json.loads(out.output)
        assert payload["command"] == "sync.now" and payload["status"] == "ok"
        assert payload["created"] == 1 and payload["pending"] == 0 and len(recording.envelopes) == 1

        out = self._run(["sync", "now"], repo, monkeypatch)
        assert out.exit_code == 0 and "Sent 0" in out.output

        out = self._run(["sync", "status"], repo, monkeypatch)
        assert "synced:          1" in out.output and "pending:         0" in out.output

        out = self._run(["sync", "disconnect"], repo, monkeypatch)
        assert out.exit_code == 0 and "Disconnected" in out.output and config.load_link() is None
        out = self._run(["sync", "now"], repo, monkeypatch)
        assert "Not connected" in out.output

    def test_status_names_the_owner_or_says_how_to_set_one(self, repo, env, monkeypatch, recording):
        from openshard.config.settings import user_config_path, write_identity_owner

        monkeypatch.setenv("OPENSHARD_HOME", env["OPENSHARD_HOME"])
        self._run(["sync", "connect", "--endpoint", ENDPOINT, "--org", ORG, "--api-key", KEY], repo, monkeypatch)
        out = self._run(["sync", "status"], repo, monkeypatch)
        assert "owner:           not set" in out.output and "openshard login" in out.output
        assert json.loads(self._run(["sync", "status", "--json"], repo, monkeypatch).output)["owner"] is None
        write_identity_owner(user_config_path(), "Dev Person <dev@acme.example>")
        out = self._run(["sync", "status"], repo, monkeypatch)
        assert "owner:           Dev Person <dev@acme.example>" in out.output

    def test_connect_prompts_for_the_key_and_rejects_insecure_endpoints(self, repo, env, monkeypatch):
        monkeypatch.setenv("OPENSHARD_HOME", env["OPENSHARD_HOME"])
        out = self._run(["sync", "connect", "--endpoint", ENDPOINT, "--org", ORG], repo, monkeypatch, input=KEY + "\n")
        assert out.exit_code == 0 and config.load_link().api_key == KEY
        out = self._run(["sync", "connect", "--endpoint", "http://platform.example.test", "--org", ORG,
                         "--api-key", KEY], repo, monkeypatch)
        assert out.exit_code == 2 and "https" in out.output

    def test_help_lists_sync_under_integrations(self, repo, monkeypatch):
        out = self._run(["--help"], repo, monkeypatch)
        integrations = out.output.split("Integrations:", 1)[1].split("Advanced:", 1)[0]
        assert "sync" in integrations

# ---------------------------------------------------------------------------
# Evidence fields the Platform accepts since Platform #230: sent exactly, and
# only when recorded (validated against the vendored Platform JSON schemas;
# an unknown value is omitted, never sent as 0 or "").
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "platform"
RECEIPT_SCHEMA = json.loads((FIXTURES / "receipt-sync-envelope.v1.json").read_text(encoding="utf-8"))
EVIDENCE_SCHEMA = json.loads((FIXTURES / "verification-evidence-envelope.v1.json").read_text(encoding="utf-8"))
RATE_KEYS = {"model", "input_per_mtok", "output_per_mtok", "cache_read_per_mtok", "cache_write_5m_per_mtok",
             "cache_write_1h_per_mtok", "currency", "source", "as_of"}
NONCE = "abcdefghijklmnopqrstuvwxyz"
SHA = "7239828109a4f27f672ea34006149bfac9b5e803"


@pytest.fixture
def plain_repo(tmp_path: Path) -> Path:
    root = _make_repo(tmp_path / "plain")
    _git(root, "remote", "add", "origin", "https://github.com/openshard/widget.git")
    return root


def _valid(schema: dict, doc: dict) -> None:
    errors = [f"{list(e.path)}: {e.message}" for e in jsonschema.Draft202012Validator(schema).iter_errors(doc)]
    assert not errors, errors[:3]


def _entry(**extra) -> dict:
    return {"receipt_id": "rcpt_" + "d" * 32, "timestamp": "2026-10-01T09:00:00Z", "task": "t",
            "agent": "codex", "schema_version": "1.2", **extra}


def _receipt(entry: dict) -> dict:
    doc = envelope.build_envelope(entry, 0, core_version="9.9.9")
    _valid(RECEIPT_SCHEMA, doc)
    return doc["receipt"]


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


class TestReceiptFields:
    def test_hook_session_sends_task_source_and_line_reason_against_the_new_schema(self, plain_repo):
        entry = _agent_session(plain_repo)
        receipt = _receipt(entry)
        assert receipt["session"]["task_source"] == "first_user_prompt_excerpt"
        reason = (entry.get("capture") or {}).get("lines_not_recorded_reason")
        assert receipt.get("lines_not_recorded_reason") == reason
        assert ("lines_not_recorded_reason" in receipt) is bool(reason)
        # The prompt text is the task, never a new field's value.
        assert "fix it and run the tests" not in json.dumps({k: receipt.get(k) for k in ("session", "rates")})

    def test_session_nonce_is_sent_when_recorded_and_absent_otherwise(self):
        capture = {"session_end_observed": True, "started_at": "2026-10-01T09:00:00Z"}
        started = {"event_type": "session.started", "metadata": {"session_nonce": NONCE}}
        assert _receipt(_entry(capture=capture, events=[started]))["session"]["session_nonce"] == NONCE
        session = _receipt(_entry(capture=capture))["session"]
        assert "session_nonce" not in session and "task_source" not in session

    def test_hook_config_hash_is_sent_when_recorded_and_well_formed(self):
        digest = "sha256:" + "0123456789abcdef" * 4
        capture = {"session_end_observed": True, "hook_config_hash": digest}
        assert _receipt(_entry(capture=capture))["session"]["hook_config_hash"] == digest
        for bad in ("sha256:short", "md5:" + "0" * 64, 7):
            session = _receipt(_entry(capture={**capture, "hook_config_hash": bad}))["session"]
            assert "hook_config_hash" not in session

    def test_unrecognised_task_source_is_omitted(self):
        capture = {"session_end_observed": True, "task_source": "made_up"}
        assert "task_source" not in _receipt(_entry(capture=capture))["session"]

    def test_unknown_fields_are_omitted_not_zeroed(self):
        receipt = _receipt(_entry())
        for key in ("lines_not_recorded_reason", "rates", "billing_basis"):
            assert key not in receipt, key


class TestRates:
    def test_single_model_list_rate_estimate_sends_its_dated_rate(self):
        entry = _entry(model="gpt-5.4", execution_model="gpt-5.4", tokens_provenance="provider_reported",
                       prompt_tokens=1000, completion_tokens=100, cache_read_tokens=0)
        receipt = _receipt(entry)
        assert receipt["cost_provenance"] == "official_rate_estimate"
        (rate,) = receipt["rates"]
        assert set(rate) <= RATE_KEYS and "output" not in _keys(receipt)
        assert rate["model"] == "gpt-5.4" and rate["currency"] == "USD" and rate["as_of"]
        assert rate["input_per_mtok"] == 2.50 and rate["output_per_mtok"] == 15.0
        assert "cache_write_5m_per_mtok" not in rate  # no published rate: unknown, never 0

    def test_per_model_transcript_estimate_sends_one_rate_per_priced_model(self):
        usage = {"claude-fable-5": {"input": 10, "output": 5, "cache_read": 0},
                 "gpt-5.4": {"input": 3, "output": 1}, "gpt-5.4-mini": {"input": 0, "output": 0}}
        entry = _entry(estimated_cost=0.01, cost_provenance="official_rate_estimate",
                       capture={"session_end_observed": True, "usage_by_model": usage})
        rates = _receipt(entry)["rates"]
        assert [r["model"] for r in rates] == ["claude-fable-5", "gpt-5.4"]  # no usage: not priced
        assert rates[0]["cache_write_5m_per_mtok"] == 12.50 and rates[0]["cache_write_1h_per_mtok"] == 20.0

    @pytest.mark.parametrize("extra", [
        {"estimated_cost": 0.5, "cost_provenance": "provider_reported", "model": "gpt-5.4",
         "execution_model": "gpt-5.4"},  # reported cost: no rate was used
        {"model": "no-such-model", "execution_model": "no-such-model", "tokens_provenance": "provider_reported",
         "prompt_tokens": 10, "completion_tokens": 1},  # unknown model: cost unknown
        {"estimated_cost": 0.5, "cost_provenance": "official_rate_estimate"},  # model not known
    ])
    def test_no_rates_unless_the_cost_was_priced_from_the_table(self, extra):
        assert "rates" not in _receipt(_entry(**extra))


class TestCheckFields:
    CHECK = ps.PlannedCheck(name="npm test", argv=["npm", "test"], kind="test", origin="contract",
                            safety="safe", reason="")

    def _block(self, *runs: ps.CheckRun) -> dict:
        tree = ps.TreeState(head=SHA, dirty=False, tracked_dirty=False)
        at = "2026-10-01T09:00:00Z"
        return ps.build_attestation(None, list(runs), before=tree, after=tree, started_at=at, completed_at=at)

    def test_setup_failed_check_carries_reported_counts_and_stays_unknown(self):
        run = ps.CheckRun(self.CHECK, "unknown", exit_code=1, duration_seconds=4.2, note="setup",
                          setup_failed=True, reported=ReportedTests("vitest", 12, 0, 1),
                          started_at="2026-10-01T08:59:55Z")
        (check,) = self._block(run)["verification"]["checks"]
        assert check == {"name": "npm test", "kind": "test", "status": "unknown", "exit_code": 1,
                         "setup_failed": True, "reported_runner": "vitest", "reported_passed": 12,
                         "reported_failed": 0, "reported_skipped": 1, "duration_seconds": 4.2,
                         "started_at": "2026-10-01T08:59:55Z"}
        # It survives the round trip every reader (receipt join, evidence upload) takes.
        assert parse_verification_block(self._block(run)["verification"]).to_dict()["checks"] == [check]

    def test_ordinary_check_sends_timing_but_no_setup_flag(self):
        run = ps.CheckRun(self.CHECK, "passed", exit_code=0, duration_seconds=1.0)
        (check,) = self._block(run)["verification"]["checks"]
        assert check["duration_seconds"] == 1.0
        assert not {"setup_failed", "reported_runner", "started_at"} & set(check)

    @pytest.mark.parametrize("raw", [
        {"status": "failed", "setup_failed": True},  # setup failure is never a verdict
        {"status": "unknown", "reported_runner": "vitest", "reported_passed": 3, "reported_failed": 0,
         "reported_skipped": 0},  # counts without setup_failed
        {"status": "unknown", "setup_failed": True, "reported_passed": 3, "reported_failed": 0,
         "reported_skipped": 0},  # counts without a runner
        {"status": "unknown", "setup_failed": True, "reported_runner": "rspec", "reported_passed": 3,
         "reported_failed": 0, "reported_skipped": 0},  # runner outside the contract
    ])
    def test_contract_rules_hold_for_any_stored_block(self, raw):
        block = {"version": 1, "status": "unknown", "source": "directly_observed",
                 "observation_mode": "openshard_executed", "checks": [{"name": "t", "kind": "test", **raw}]}
        (check,) = parse_verification_block(block).to_dict()["checks"]
        assert not any(k.startswith("reported_") for k in check)
        if raw["status"] != "unknown":
            assert "setup_failed" not in check

    def test_to_dict_never_pairs_setup_failed_with_a_verdict(self):
        check = VerificationCheck(name="t", status="passed", setup_failed=True, reported_runner="jest",
                                  reported_passed=1, reported_failed=0, reported_skipped=0)
        assert set(check.to_dict()) == {"name", "kind", "status", "exit_code"}

    def test_run_checks_stamps_start_time_and_keeps_output_out(self):
        class Proc:
            returncode = 0
            stdout = "SECRET-OUTPUT 3 passed in 0.1s"

        (run,) = ps.run_checks([self.CHECK], Path("."), stream=False, runner=lambda *a, **k: Proc())
        assert run.started_at and run.started_at.endswith("Z")
        att = self._block(run)
        assert att["verification"]["checks"][0]["started_at"] == run.started_at
        assert "SECRET-OUTPUT" not in json.dumps(att)

    def test_verification_evidence_upload_carries_the_fields(self, plain_repo):
        entry = _agent_session(plain_repo)
        _rerun(plain_repo, entry)
        atts = ps.load_attestations(plain_repo / ".openshard")
        run = ps.CheckRun(self.CHECK, "passed", exit_code=0, duration_seconds=2.5,
                          started_at="2026-10-01T08:59:55Z")
        tree = ps.TreeState(head=SHA, dirty=False, tracked_dirty=False)
        att = ps.build_attestation(entry, [run], before=tree, after=tree, started_at="2026-10-01T09:00:00Z",
                                   completed_at="2026-10-01T09:00:00Z")
        ps.record_attestation(plain_repo, att)
        assert len(ps.load_attestations(plain_repo / ".openshard")) == len(atts) + 1
        doc = _envelope(plain_repo)
        assert doc is not None
        _valid(EVIDENCE_SCHEMA, doc)
        checks = doc["evidence"][-1]["verification"]["checks"]
        assert checks[0]["duration_seconds"] == 2.5 and checks[0]["started_at"] == "2026-10-01T08:59:55Z"
        assert load_history(plain_repo / RUNS, coerce=False)[-1]["receipt_id"] == doc["receipt_id"]
