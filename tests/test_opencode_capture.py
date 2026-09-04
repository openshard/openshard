"""Tests for OpenCode capture (PR12): plugin, translator, installer, service path, CLI.

The plugin documents used here are the shapes the real plugin
(``opencode_plugin_install.PLUGIN_SOURCE``) produces from OpenCode's SDK
event types; ``TestPluginUnderNode`` additionally runs the actual plugin
source under ``node`` (type stripping) against representative OpenCode
events with a stubbed ``fetch`` and feeds what it POSTs through the real
translator, so the plugin -> service boundary is exercised end to end
without a running OpenCode. No real ``opencode`` binary is ever run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_capture_service as svc
from openshard.adapters import opencode_plugin as oc
from openshard.adapters.claude_hooks import StatusPayload, handle_hook, reduce_hook_payload
from openshard.adapters.opencode_plugin_install import (
    PLUGIN_MARKER,
    PLUGIN_RELPATH,
    PLUGIN_VERSION,
    detect_plugin,
    install_opencode_plugin,
    render_plugin_source,
    uninstall_opencode_plugin,
)
from openshard.cli.main import cli
from openshard.history.event import SOURCE_OPENCODE_PLUGIN, events_from_entry
from openshard.history.query import list_shards
from openshard.history.shard import CAPTURE_PARTIAL, ORIGIN_EXTERNAL_OBSERVED
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt

SID = "ses_8b1f2c3d4e5f60718293a4b5c6d7e8f9"
SID2 = "ses_ffffffffffffffffffffffffffffffff"
SECRET = "sk-ant-api03-SECRETSECRET12345678901234567890"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "opencode repo")


def _doc(event: str, repo: Path, session_id: str = SID, **fields) -> dict:
    base: dict = {"agent": "opencode", "event": event, "session_id": session_id,
                  "directory": str(repo), "worktree": str(repo)}
    base.update(fields)
    return base


def _usage(repo: Path, message_id: str, cost: float, inp: int, out: int, session_id: str = SID) -> dict:
    return _doc("message.updated", repo, session_id, message_id=message_id, provider_id="anthropic",
                model_id="claude-sonnet-4-5", cost=cost,
                tokens={"input": inp, "output": out, "reasoning": 0, "cache": {"read": 10, "write": 5}})


def _run(repo: Path, event: str, session_id: str = SID, **fields):
    return handle_hook(_doc(event, repo, session_id, **fields), env={}, agent="opencode")


def _drive_inline(repo: Path, session_id: str = SID) -> None:
    _run(repo, "session.created", session_id, parent_id=None)
    _run(repo, "chat.message", session_id, prompt=f"Add a calculator module {SECRET}",
         provider_id="anthropic", model_id="claude-sonnet-4-5")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _run(repo, "tool.execute.after", session_id, tool="write", file_path=str(repo / "calc.py"))
    _run(repo, "file.edited", session_id, file_path=str(repo / "calc.py"))
    _run(repo, "tool.execute.after", session_id, tool="bash", command="python -m pytest -q")
    _run(repo, "tool.execute.after", session_id, tool="read", file_path=str(repo / "README.md"))
    handle_hook(_usage(repo, "msg_1", 0.0123, 1000, 200, session_id), env={}, agent="opencode")
    handle_hook(_usage(repo, "msg_1", 0.0123, 1000, 200, session_id), env={}, agent="opencode")  # re-reported
    handle_hook(_usage(repo, "msg_2", 0.0077, 500, 100, session_id), env={}, agent="opencode")
    _run(repo, "session.idle", session_id)
    _run(repo, "session.deleted", session_id)


def _lines(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except PermissionError:
        return []
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------


class TestTranslator:
    def test_lifecycle_mapping(self, repo):
        p = oc.extract_opencode_payload(_doc("session.created", repo))
        assert p.event == "SessionStart" and p.source == "startup" and p.agent == "opencode"
        assert p.cwd == str(repo) and p.session_id == SID
        # Idle is a neutral boundary, never Claude's Stop (idle also follows an abort).
        assert oc.extract_opencode_payload(_doc("session.idle", repo)).event == "SessionIdle"
        end = oc.extract_opencode_payload(_doc("session.deleted", repo))
        assert end.event == "SessionEnd" and end.reason == "deleted"
        fe = oc.extract_opencode_payload(_doc("file.edited", repo, file_path="x.py"))
        assert fe.event == "FileEdited" and fe.file_path == "x.py"

    def test_chat_message_carries_prompt_and_model(self, repo):
        p = oc.extract_opencode_payload(_doc("chat.message", repo, prompt="fix login", provider_id="openai",
                                             model_id="gpt-5"))
        assert p.event == "UserPromptSubmit" and p.prompt == "fix login"
        assert p.provider_id == "openai" and p.model_id == "gpt-5"

    def test_tool_classification(self, repo):
        p = oc.extract_opencode_payload(_doc("tool.execute.after", repo, tool="edit", file_path="a.py"))
        assert p.event == "PostToolUse" and p.tool_kind == "file" and p.file_path == "a.py"
        assert p.tool_success is None  # tool.execute.after carries no outcome: never a success signal
        p = oc.extract_opencode_payload(_doc("tool.execute.after", repo, tool="bash", command="ls"))
        assert p.tool_kind == "command" and p.command == "ls"
        p = oc.extract_opencode_payload(_doc("tool.execute.after", repo, tool="webfetch", command="ls"))
        assert p.tool_kind == "other" and p.command is None and p.file_path is None

    def test_usage_report_is_a_status_observation(self, repo):
        p = oc.extract_opencode_payload(_usage(repo, "msg_9", 0.5, 10, 20))
        assert isinstance(p, StatusPayload)
        assert p.usage_key == "msg_9" and p.cost_total_usd == 0.5 and p.agent == "opencode"
        assert p.tokens_input == 10 and p.tokens_output == 20
        assert p.tokens_cache_read == 10 and p.tokens_cache_creation == 5
        assert p.provider_id == "anthropic" and p.model_id == "claude-sonnet-4-5"
        no_id = _usage(repo, "msg_9", 0.5, 10, 20)
        del no_id["message_id"]
        assert oc.extract_opencode_payload(no_id) is None

    def test_unknown_or_foreign_documents_are_ignored(self, repo):
        assert oc.extract_opencode_payload(_doc("session.updated", repo)) is None
        assert oc.extract_opencode_payload(_doc("tui.toast.show", repo)) is None
        assert oc.extract_opencode_payload({**_doc("session.idle", repo), "agent": "codex"}) is None
        assert oc.extract_opencode_payload({"event": "session.idle"}).session_id is None
        assert oc.extract_opencode_payload({}) is None

    def test_reduced_payload_carries_no_raw_text(self, repo):
        p = oc.extract_opencode_payload(_doc("tool.execute.after", repo, tool="bash",
                                             command=f"curl -H 'Authorization: {SECRET}' x"))
        reduced = reduce_hook_payload(p, repo)
        blob = json.dumps(reduced.to_dict())
        assert SECRET not in blob and reduced.agent == "opencode" and reduced.tool_kind == "command"
        p = oc.extract_opencode_payload(_doc("tool.execute.after", repo, tool="edit", file_path="/etc/hosts"))
        reduced = reduce_hook_payload(p, repo)
        assert reduced.file_target is None and reduced.file_dropped is True


# ---------------------------------------------------------------------------
# Payload -> canonical record / receipt (inline path)
# ---------------------------------------------------------------------------


class TestCanonicalRecord:
    def test_session_becomes_one_opencode_shard(self, repo):
        _drive_inline(repo)
        lines = _lines(repo)
        assert len(lines) == 1
        entry = lines[0]
        assert entry["executor"] == "opencode_plugin" and entry["import_source"] == "opencode"
        assert entry["execution_model"] == "anthropic/claude-sonnet-4-5"
        cap = entry["capture"]
        assert cap["source"] == "opencode_plugin" and cap["agent"] == "opencode"
        assert cap["agent_vendor"] is None and cap["provider"] == "anthropic"
        assert cap["model_source"] == "opencode_plugin"
        assert cap["models_seen"] == ["anthropic/claude-sonnet-4-5"]
        assert cap["prompt_count"] == 1 and cap["tool_call_count"] == 3
        # session.idle is neutral: no completed turn is ever claimed from it.
        assert cap["turn_count"] == 0 and cap["idle_count"] == 1 and cap["last_turn_completed_at"] is None
        assert cap["task_status"] == "ended_no_turn" and "duration_seconds" not in entry
        assert cap["session_end_observed"] is True and cap["session_end_reason"] == "deleted"
        assert entry["task"].startswith("Add a calculator module")
        assert SECRET not in json.dumps(entry)
        # Usage: two distinct messages summed, the re-reported one counted once.
        assert entry["estimated_cost"] == pytest.approx(0.02, abs=1e-6)
        assert entry["cost_provenance"] == "agent_reported" and entry["tokens_provenance"] == "agent_reported"
        assert entry["prompt_tokens"] == 1500 and entry["completion_tokens"] == 300
        assert entry["cache_read_tokens"] == 20 and entry["cache_creation_tokens"] == 10
        assert set(cap["usage_by_key"]) == {"msg_1", "msg_2"}
        assert entry["verification_attempted"] is False and entry["verification_passed"] is None
        assert {f["path"] for f in entry["files_detail"]} >= {"calc.py"}

    def test_events_carry_opencode_identity(self, repo):
        _drive_inline(repo)
        events = events_from_entry(_lines(repo)[0])
        assert events and all(e.source == SOURCE_OPENCODE_PLUGIN and e.actor == "opencode" for e in events)
        tools = [e for e in events if e.event_type == "tool.invoked"]
        assert {e.metadata.get("tool") for e in tools} == {"write", "bash", "read"}
        write_ev = next(e for e in tools if e.metadata.get("tool") == "write")
        # No provider success signal -> never "passed"; the path is still the attempted target.
        assert write_ev.target == "calc.py" and write_ev.status == "unknown" and write_ev.evidence == "agent_reported"
        assert not [e for e in tools if e.status == "passed"]
        bash_ev = next(e for e in tools if e.metadata.get("tool") == "bash")
        assert bash_ev.action.startswith("bash: ") and bash_ev.metadata.get("command_kind") == "test"
        assert not [e for e in events if e.event_type.startswith("verification.")]
        assert any("OpenCode session observed" in e.action for e in events)
        assert any("session idle" in e.action for e in events)
        assert not any("turn completed" in e.action for e in events)
        assert any("OpenCode session ended" in e.action for e in events)

    def test_receipt_identity_is_opencode_not_the_provider(self, repo):
        _drive_inline(repo)
        receipt = build_shard_receipt(_lines(repo)[0])
        assert receipt.agent == "OpenCode (external)"
        assert receipt.shard.origin == ORIGIN_EXTERNAL_OBSERVED and receipt.shard.capture_depth == CAPTURE_PARTIAL
        assert receipt.tokens_input == 1500 and receipt.tokens_provenance == "agent_reported"
        assert receipt.cost_provenance == "agent_reported"
        text = render_compact_shard_receipt(receipt)
        assert "OpenCode (external)" in text and "est." in text
        assert list_shards(repo_path=repo)[0].agent == "OpenCode (external)"

    def test_missing_provider_model_and_usage_stay_unknown(self, repo):
        _run(repo, "session.created")
        _run(repo, "chat.message", prompt="no model exposed")
        _run(repo, "session.idle")
        entry = _lines(repo)[0]
        assert entry["execution_model"] == "unknown" and entry["capture"]["provider"] is None
        for key in ("estimated_cost", "prompt_tokens", "tokens_provenance"):
            assert key not in entry

    def test_idle_is_never_a_completed_turn(self, repo):
        _run(repo, "session.created")
        _run(repo, "chat.message", prompt="do a thing")
        _run(repo, "tool.execute.after", tool="bash", command="echo hi")
        outcome = _run(repo, "session.idle")
        assert outcome.action in ("record_updated", "record_created")  # idle still snapshots the record
        entry = _lines(repo)[0]
        cap = entry["capture"]
        assert cap["turn_count"] == 0 and cap["task_status"] == "in_progress"
        assert cap["idle_count"] == 1 and cap["last_turn_completed_at"] is None and cap["last_idle_at"]
        assert "duration_seconds" not in entry
        actions = [e["action"] for e in entry["events"]]
        assert any("session idle" in a for a in actions) and not any("turn completed" in a for a in actions)
        assert "turn completion not confirmed" in entry["summary"]
        # A second idle (e.g. after the user aborted the next turn) still adds no turn.
        _run(repo, "chat.message", prompt="again")
        _run(repo, "session.idle")
        cap = _lines(repo)[0]["capture"]
        assert cap["turn_count"] == 0 and cap["idle_count"] == 2 and cap["prompt_count"] == 2
        assert build_shard_receipt(_lines(repo)[0]).status != "passed"

    def test_abort_then_idle_records_no_completion(self, repo):
        # An aborted turn: prompt, a file tool call, then OpenCode goes idle with
        # no completed assistant message -- nothing may claim a completed turn or
        # a successful edit.
        _run(repo, "chat.message", prompt="abort me")
        _run(repo, "tool.execute.after", tool="edit", file_path=str(repo / "README.md"))
        _run(repo, "session.idle")
        entry = _lines(repo)[0]
        assert entry["capture"]["turn_count"] == 0 and entry["capture"]["session_end_observed"] is False
        tool = next(e for e in entry["events"] if e["event_type"] == "tool.invoked")
        assert tool["status"] == "unknown" and tool["target"] == "README.md"
        _run(repo, "session.deleted")
        cap = _lines(repo)[0]["capture"]
        assert cap["turn_count"] == 0 and cap["task_status"] == "ended_no_turn" and cap["session_end_observed"]
        assert build_shard_receipt(_lines(repo)[0]).status != "passed"

    def test_idle_without_work_records_nothing(self, repo):
        _run(repo, "session.created")
        assert _run(repo, "session.idle").action == "buffered"
        assert _lines(repo) == []

    def test_file_edited_is_the_only_hook_reported_file_signal(self, tmp_path):
        """Without git, OpenCode's file.edited (published only after a successful write)
        feeds the hook-reported list; tool.execute.after alone never does."""
        with_edit = tmp_path / "with edit"
        without = tmp_path / "without"
        with_edit.mkdir()
        without.mkdir()
        with patch("openshard.adapters.claude_mcp_install.find_repo_root", return_value=None), \
             patch("openshard.adapters.claude_code_import.subprocess.run",
                   side_effect=FileNotFoundError("no git")):
            for root in (with_edit, without):
                _run(root, "chat.message", prompt="task")
                _run(root, "tool.execute.after", tool="write", file_path=str(root / "made.py"))
            _run(with_edit, "file.edited", file_path=str(with_edit / "made.py"))
            for root in (with_edit, without):
                _run(root, "session.idle")
        a, b = _lines(with_edit)[0], _lines(without)[0]
        assert a["files_source"] == "opencode_plugin_reported"
        assert a["files_detail"] == [{"path": "made.py", "change_type": "update",
                                      "summary": "reported by OpenCode hook"}]
        assert b["files_source"] == "not_available" and b["files_detail"] == []
        for entry in (a, b):
            tool = next(e for e in entry["events"] if e["event_type"] == "tool.invoked")
            assert tool["status"] == "unknown" and tool["target"] == "made.py"

    def test_zero_cost_is_not_a_cost_claim(self, repo):
        _run(repo, "chat.message", prompt="unpriced model", provider_id="ollama", model_id="llama3")
        handle_hook(_usage(repo, "msg_1", 0, 100, 20), env={}, agent="opencode")
        handle_hook(_usage(repo, "msg_2", 0.0, 50, 10), env={}, agent="opencode")
        _run(repo, "session.idle")
        entry = _lines(repo)[0]
        assert "estimated_cost" not in entry and "cost_provenance" not in entry
        assert entry["capture"]["cost_total_usd"] is None
        # Tokens are trustworthy on their own and stay recorded.
        assert entry["prompt_tokens"] == 150 and entry["completion_tokens"] == 30
        assert entry["tokens_provenance"] == "agent_reported"
        receipt = build_shard_receipt(entry)
        assert receipt.cost_provenance is None and receipt.tokens_input == 150
        assert "$0.00" not in render_compact_shard_receipt(receipt)

    def test_mixed_reported_costs_sum_only_positive(self, repo):
        _run(repo, "chat.message", prompt="mixed")
        handle_hook(_usage(repo, "msg_1", 0, 100, 20), env={}, agent="opencode")
        handle_hook(_usage(repo, "msg_2", 0.004, 50, 10), env={}, agent="opencode")
        handle_hook(_usage(repo, "msg_3", -1, 5, 1), env={}, agent="opencode")
        unpriced = _usage(repo, "msg_4", 0.0, 7, 3)
        unpriced["cost"] = None
        handle_hook(unpriced, env={}, agent="opencode")
        _run(repo, "session.idle")
        entry = _lines(repo)[0]
        assert entry["estimated_cost"] == pytest.approx(0.004) and entry["cost_provenance"] == "agent_reported"
        assert entry["prompt_tokens"] == 162 and entry["completion_tokens"] == 34
        # The one priced message re-reported as unpriced replaces its cost: nothing positive is left.
        handle_hook(_usage(repo, "msg_2", 0, 50, 10), env={}, agent="opencode")
        _run(repo, "session.idle")
        entry = _lines(repo)[0]
        assert "estimated_cost" not in entry and "cost_provenance" not in entry
        assert entry["prompt_tokens"] == 162

    def test_usage_before_any_lifecycle_hook_is_not_recorded(self, repo):
        outcome = handle_hook(_usage(repo, "msg_1", 1.0, 1, 1), env={}, agent="opencode")
        assert outcome.action == "ignored"
        assert not (repo / ".openshard").exists() or _lines(repo) == []

    def test_repositories_are_isolated(self, tmp_path):
        a = _make_repo(tmp_path / "a")
        b = _make_repo(tmp_path / "b")
        _drive_inline(a)
        _run(b, "chat.message", prompt="other worktree")
        _run(b, "session.idle")
        assert len(_lines(a)) == 1 and len(_lines(b)) == 1 and _lines(b)[0]["task"] == "other worktree"

    def test_routed_opencode_and_observed_opencode_are_distinct_identities(self, repo):
        from openshard.history.shard import derive_shard_identity

        assert derive_shard_identity({"executor": "opencode_plugin"}) == (
            "OpenCode (external)", ORIGIN_EXTERNAL_OBSERVED, CAPTURE_PARTIAL)
        agent, origin, depth = derive_shard_identity({"executor": "opencode", "workflow": "opencode"})
        assert agent == "OpenCode" and origin != ORIGIN_EXTERNAL_OBSERVED


# ---------------------------------------------------------------------------
# Service path: POST /hooks/opencode
# ---------------------------------------------------------------------------


class _Service:
    def __init__(self, env: dict) -> None:
        self.env = env
        self.ready = threading.Event()
        self.box: list = []
        self.thread = threading.Thread(
            target=svc.serve, kwargs={"port": 0, "idle_timeout": 0.0, "env": env,
                                      "ready": self.ready, "server_box": self.box}, daemon=True)
        self.thread.start()
        assert self.ready.wait(10)

    @property
    def server(self):
        return self.box[0]

    @property
    def port(self) -> int:
        return self.server.port

    def stop(self) -> None:
        if self.box:
            self.box[0].begin_shutdown("test")
        self.thread.join(60)


@pytest.fixture
def capture_env(monkeypatch) -> dict:
    monkeypatch.delenv("OPENSHARD_CAPTURE_DISABLE", raising=False)
    monkeypatch.setenv("OPENSHARD_CAPTURE_NO_SPAWN", "1")
    return dict(os.environ)


@pytest.fixture
def service(capture_env):
    running = _Service(capture_env)
    yield running
    running.stop()


def _wait_for(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _post(port: int, doc: dict) -> bool:
    return client.post_hook(port, json.dumps(doc).encode("utf-8"), hook_path=client.OPENCODE_HOOK_PATH)


def _stable(entry: dict) -> dict:
    keys = ("event_type", "action", "status", "evidence", "target", "actor", "source")
    volatile = {"started_at", "last_activity_at", "first_prompt_at", "last_turn_completed_at",
                "last_status_ping_at", "last_idle_at", "applied_event_ids"}
    return {
        "task": entry["task"], "executor": entry["executor"], "execution_model": entry["execution_model"],
        "estimated_cost": entry.get("estimated_cost"), "prompt_tokens": entry.get("prompt_tokens"),
        "files_detail": entry["files_detail"], "summary": entry["summary"],
        "capture": {k: v for k, v in entry["capture"].items() if k not in volatile},
        "events": [{k: e.get(k) for k in keys} for e in entry["events"]],
    }


class TestServicePath:
    def test_http_session_matches_inline_record(self, service, tmp_path):
        via_http = _make_repo(tmp_path / "http")
        via_inline = _make_repo(tmp_path / "inline")
        docs = [
            _doc("session.created", via_http, parent_id=None),
            _doc("chat.message", via_http, prompt=f"Add a calculator module {SECRET}", provider_id="anthropic",
                 model_id="claude-sonnet-4-5"),
        ]
        for d in docs:
            assert _post(service.port, d)
        (via_http / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        for d in (
            _doc("tool.execute.after", via_http, tool="write", file_path=str(via_http / "calc.py")),
            _doc("file.edited", via_http, file_path=str(via_http / "calc.py")),
            _doc("tool.execute.after", via_http, tool="bash", command="python -m pytest -q"),
            _doc("tool.execute.after", via_http, tool="read", file_path=str(via_http / "README.md")),
            _usage(via_http, "msg_1", 0.0123, 1000, 200),
            _usage(via_http, "msg_1", 0.0123, 1000, 200),
            _usage(via_http, "msg_2", 0.0077, 500, 100),
            _doc("session.idle", via_http),
            _doc("session.deleted", via_http),
        ):
            assert _post(service.port, d)
        _drive_inline(via_inline)
        assert _wait_for(lambda: bool(_lines(via_http)) and _lines(via_http)[0]["capture"]["session_end_observed"])
        assert service.server.recorder.wait_idle(20)
        assert _stable(_lines(via_http)[0]) == _stable(_lines(via_inline)[0])
        assert SECRET not in (via_http / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")

    def test_usage_is_queued_as_status_and_deduplicated(self, service, repo):
        service.server.recorder.pause_processing()
        assert _post(service.port, _doc("chat.message", repo, prompt="t"))
        assert _post(service.port, _usage(repo, "msg_1", 0.5, 10, 20))
        # Agent-scoped queue file: an OpenCode session never shares a replay with a
        # Claude session that happens to carry the same id.
        queue_file = repo / ".openshard" / "claude_sessions" / f"opencode.{SID}{svc.QUEUE_SUFFIX}"
        assert not (repo / ".openshard" / "claude_sessions" / f"{SID}{svc.QUEUE_SUFFIX}").exists()
        lines = [json.loads(ln) for ln in queue_file.read_text(encoding="utf-8").splitlines()]
        assert [ln["kind"] for ln in lines] == ["hook", "status"]
        assert lines[1]["data"]["agent"] == "opencode" and lines[1]["data"]["usage_key"] == "msg_1"
        service.server.recorder.resume_processing()
        assert _post(service.port, _usage(repo, "msg_1", 0.5, 10, 20))
        assert _post(service.port, _doc("session.idle", repo))
        assert _wait_for(lambda: bool(_lines(repo)) and _lines(repo)[0]["capture"]["idle_count"] == 1)
        assert service.server.recorder.wait_idle(20)
        assert _lines(repo)[0]["estimated_cost"] == pytest.approx(0.5)

    def test_blocking_path_stays_within_budget(self, service, repo):
        assert _post(service.port, _doc("session.created", repo))
        assert _post(service.port, _doc("chat.message", repo, prompt="warm"))
        roundtrips: list[float] = []
        for i in range(40):
            doc = _doc("tool.execute.after", repo, tool="bash", command=f"echo {i}")
            t0 = time.perf_counter()
            assert _post(service.port, doc)
            roundtrips.append(time.perf_counter() - t0)
        # Only the blocking path is under test; the worker's drain time on a
        # contended box is not asserted on (see the Codex counterpart).
        service.server.recorder.wait_idle(60)
        timing = client.health(service.port)["blocking_ms"]
        assert timing["p50_ms"] < 25, timing
        assert timing["p95_ms"] < 50, timing
        roundtrips.sort()
        assert roundtrips[len(roundtrips) // 2] < 0.05, roundtrips

    def test_malformed_documents_never_error(self, service, repo):
        for body in (b"", b"[]", b'{"event":"session.idle"}', b'{"event":"nope","session_id":"x"}'):
            status, reply = client._request("POST", service.port, client.OPENCODE_HOOK_PATH, body)
            assert status == 200 and reply == b"{}", body
        assert client.health(service.port)["stats"]["queued"] == 0


# ---------------------------------------------------------------------------
# The real plugin under node: representative OpenCode events -> POST documents
# ---------------------------------------------------------------------------


def _node_status() -> tuple[bool, str]:
    """``(usable, reason)`` -- the reason names the exact node found, so a skip is never silent."""
    node = shutil.which("node")
    if not node:
        return False, "node not found on PATH; the real-plugin tests need node >= 23 (TypeScript type stripping)"
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        major = int(out.lstrip("v").split(".", 1)[0])
    except Exception as exc:
        return False, f"could not determine the version of {node} ({type(exc).__name__}); need node >= 23"
    if major < 23:
        return False, f"node {out} at {node} is too old; the real-plugin tests need node >= 23 (type stripping)"
    return True, f"node {out} at {node}"


_NODE_OK, _NODE_REASON = _node_status()
# CI can turn the skip into a hard failure so a silently-missing runtime is noticed.
_NODE_REQUIRED_ENV = "OPENSHARD_REQUIRE_NODE_PLUGIN_TESTS"


def _require_node() -> str:
    if not _NODE_OK:
        if os.environ.get(_NODE_REQUIRED_ENV):
            pytest.fail(f"{_NODE_REQUIRED_ENV} is set but the OpenCode plugin runtime tests cannot run: {_NODE_REASON}")
        pytest.skip(_NODE_REASON)
    return shutil.which("node") or "node"


def _run_node_harness(harness_source: str, tmp_path: Path, *args: str) -> list[dict]:
    """Run *harness_source* under node against the rendered plugin; returns the JSON lines it printed."""
    node = _require_node()
    plugin = tmp_path / "openshard.ts"
    plugin.write_text(render_plugin_source(port=47899), encoding="utf-8")
    harness = tmp_path / "harness.mjs"
    harness.write_text(harness_source, encoding="utf-8")
    result = subprocess.run(
        [node, "--no-warnings", str(harness), plugin.resolve().as_uri(), *args],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"plugin harness failed under {_NODE_REASON}:\n{result.stderr}\n{result.stdout}"
    return [json.loads(line) for line in result.stdout.strip().splitlines() if line.strip()]


HARNESS = r"""
const captured = [];
globalThis.fetch = async (url, init) => { captured.push({ url, body: JSON.parse(init.body) }); return { ok: true }; };
const mod = await import(process.argv[2]);
const plugin = mod.OpenShardCapture;
const hooks = await plugin({ directory: process.argv[3], worktree: process.argv[3], $: undefined });
const sid = process.argv[4];
const S = (info) => ({ id: sid, projectID: "prj", directory: process.argv[3], title: "t", version: "1", time: { created: 1 }, ...info });
await hooks.event({ event: { type: "session.created", properties: { info: S({}) } } });
await hooks.event({ event: { type: "session.created", properties: { info: S({ id: "ses_child", parentID: sid }) } } });
await hooks["chat.message"](
  { sessionID: sid, model: { providerID: "anthropic", modelID: "claude-sonnet-4-5" }, messageID: "msg_u1" },
  { message: { id: "msg_u1", sessionID: sid, role: "user", time: { created: 1 }, agent: "build",
               model: { providerID: "anthropic", modelID: "claude-sonnet-4-5" } },
    parts: [{ id: "p1", sessionID: sid, messageID: "msg_u1", type: "text", text: "Fix the login bug " + "x".repeat(1000) }] });
await hooks["tool.execute.after"](
  { tool: "edit", sessionID: sid, callID: "c1", args: { filePath: process.argv[3] + "/auth.py", oldString: "a", newString: "b" } },
  { title: "auth.py", output: "SECRET-OUTPUT-NEVER-SENT", metadata: {} });
await hooks["tool.execute.after"](
  { tool: "bash", sessionID: sid, callID: "c2", args: { command: "pytest -q", description: "run tests" } },
  { title: "pytest", output: "3 passed", metadata: { exit: 0 } });
await hooks["tool.execute.after"](
  { tool: "bash", sessionID: "ses_child", callID: "c3", args: { command: "child work" } },
  { title: "x", output: "", metadata: {} });
await hooks.event({ event: { type: "file.edited", properties: { file: process.argv[3] + "/auth.py" } } });
await hooks.event({ event: { type: "message.updated", properties: { info: {
  id: "msg_a1", sessionID: sid, role: "assistant", time: { created: 1 }, parentID: "msg_u1",
  modelID: "claude-sonnet-4-5", providerID: "anthropic", mode: "build", path: { cwd: ".", root: "." },
  cost: 0.01, tokens: { input: 100, output: 50, reasoning: 0, cache: { read: 1, write: 2 } } } } } });
await hooks.event({ event: { type: "message.updated", properties: { info: {
  id: "msg_a1", sessionID: sid, role: "assistant", time: { created: 1, completed: 2 }, parentID: "msg_u1",
  modelID: "claude-sonnet-4-5", providerID: "anthropic", mode: "build", path: { cwd: ".", root: "." },
  cost: 0.02, tokens: { input: 100, output: 50, reasoning: 0, cache: { read: 1, write: 2 } } } } } });
await hooks.event({ event: { type: "session.idle", properties: { sessionID: sid } } });
await hooks.event({ event: { type: "session.idle", properties: { sessionID: "ses_child" } } });
await hooks.event({ event: { type: "session.deleted", properties: { info: S({}) } } });
await new Promise((r) => setTimeout(r, 50));
console.log(JSON.stringify(captured));
"""


RESTART_HARNESS = r"""
// Service availability is simulated through `fetch`; `$` counts start attempts;
// Date.now is faked so the 60 s start cooldown can be crossed instantly. Timers
// (the post-start flush) are real.
let up = false
let starts = 0
let fakeNow = 1_000_000
const captured = []
Date.now = () => fakeNow
globalThis.fetch = async (url, init) => {
  if (!up) throw new Error("ECONNREFUSED")
  captured.push(JSON.parse(init.body))
  return { ok: true }
}
const $ = (_strings, ..._values) => {
  starts += 1
  const done = Promise.resolve({ exitCode: 0 })
  return { quiet: () => ({ nothrow: () => done }) }
}
const mod = await import(process.argv[2])
const hooks = await mod.OpenShardCapture({ directory: process.argv[3], worktree: process.argv[3], $ })
const sid = process.argv[4]
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const idle = () => hooks.event({ event: { type: "session.idle", properties: { sessionID: sid } } })
const report = (label) => console.log(JSON.stringify({ label, starts, delivered: captured.map((c) => c.event) }))

// 1. Service unavailable: the first failed delivery asks for a start; further
//    failures inside the cooldown do not.
await hooks.event({ event: { type: "session.created", properties: { info: { id: sid } } } })
await sleep(20)
await hooks["chat.message"]({ sessionID: sid }, { parts: [{ type: "text", text: "hi" }] })
await sleep(20)
report("down")
// 2. The service comes up: the post-start flush delivers the buffer, in order.
up = true
await sleep(1700)
report("recovered")
// 3. The service dies again: a failure inside the cooldown never respawns...
up = false
fakeNow += 30_000
await hooks["tool.execute.after"]({ tool: "bash", sessionID: sid, args: { command: "ls" } }, {})
await sleep(20)
report("died")
// 4. ...the first failure after the cooldown restarts it exactly once, and
//    the buffered documents are delivered once it is back.
fakeNow += 31_000
await idle()
await sleep(20)
report("restarting")
up = true
await sleep(1700)
report("recovered_again")
// 5. The pending queue stays bounded while the service is down (a document
//    arriving at a full queue is dropped), and the next delivery attempt
//    flushes it without waiting for any timer.
up = false
fakeNow += 61_000
for (let i = 0; i < 230; i++) await idle()
await sleep(50)
report("flooded")
up = true
await hooks.event({ event: { type: "session.deleted", properties: { info: { id: sid } } } })
await sleep(300)
report("flushed")
await hooks.event({ event: { type: "session.deleted", properties: { info: { id: sid } } } })
await sleep(50)
report("final")
"""


class TestPluginUnderNode:
    def test_plugin_posts_bounded_documents_the_translator_accepts(self, tmp_path, repo):
        node = _require_node()
        plugin = tmp_path / "openshard.ts"
        plugin.write_text(render_plugin_source(port=47899), encoding="utf-8")
        harness = tmp_path / "harness.mjs"
        harness.write_text(HARNESS, encoding="utf-8")
        result = subprocess.run(
            [node, "--no-warnings", str(harness), plugin.resolve().as_uri(), str(repo), SID],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, f"plugin harness failed under {_NODE_REASON}:\n{result.stderr}"
        captured = json.loads(result.stdout.strip().splitlines()[-1])
        assert captured and all(c["url"] == f"http://127.0.0.1:47899{client.OPENCODE_HOOK_PATH}" for c in captured)
        docs = [c["body"] for c in captured]
        events = [d["event"] for d in docs]
        # Child (subagent) sessions are filtered by the plugin; the parent session's flow is complete.
        assert events == ["session.created", "chat.message", "tool.execute.after", "tool.execute.after",
                          "file.edited", "message.updated", "session.idle", "session.deleted"]
        assert all(d["session_id"] == SID and d["agent"] == "opencode" and d["worktree"] == str(repo) for d in docs)
        blob = json.dumps(docs)
        assert "SECRET-OUTPUT-NEVER-SENT" not in blob and "3 passed" not in blob
        assert "oldString" not in blob and "newString" not in blob
        chat = docs[1]
        assert chat["prompt"].startswith("Fix the login bug") and len(chat["prompt"]) <= 400
        assert chat["provider_id"] == "anthropic" and chat["model_id"] == "claude-sonnet-4-5"
        assert docs[2]["tool"] == "edit" and docs[2]["file_path"].endswith("auth.py") and docs[2].get("command") is None
        assert docs[3]["tool"] == "bash" and docs[3]["command"] == "pytest -q"
        usage = docs[5]
        assert usage["message_id"] == "msg_a1" and usage["cost"] == 0.02  # only the completed report
        assert usage["tokens"]["input"] == 100 and usage["tokens"]["cache"]["write"] == 2
        # The real translator accepts every document the plugin produced.
        for d in docs:
            translated = oc.extract_opencode_payload(d)
            assert translated is not None, d
        # ...and the whole stream folds into one OpenCode Shard.
        for d in docs:
            handle_hook(d, env={}, agent="opencode")
        entry = _lines(repo)[0]
        assert entry["executor"] == "opencode_plugin"
        assert entry["execution_model"] == "anthropic/claude-sonnet-4-5"
        assert entry["estimated_cost"] == pytest.approx(0.02)
        assert entry["capture"]["tool_call_count"] == 2
        assert entry["capture"]["turn_count"] == 0 and entry["capture"]["idle_count"] == 1
        assert entry["task"] == "Fix the login bug " + "x" * 282  # scrubbed excerpt, 300-char cap

    def test_plugin_restarts_the_service_with_a_bounded_cooldown(self, tmp_path, repo):
        reports = {r["label"]: r for r in _run_node_harness(RESTART_HARNESS, tmp_path, str(repo), SID)}
        # Down: one start attempt for the first failure, none for the next one inside the cooldown.
        assert reports["down"]["starts"] == 1 and reports["down"]["delivered"] == []
        # Up: the post-start flush delivered the buffer in order.
        assert reports["recovered"]["delivered"] == ["session.created", "chat.message"]
        # Died inside the cooldown: no respawn.
        assert reports["died"]["starts"] == 1
        # First failure after the cooldown: exactly one restart, then recovery in order.
        assert reports["restarting"]["starts"] == 2
        assert reports["recovered_again"]["delivered"] == [
            "session.created", "chat.message", "tool.execute.after", "session.idle"]
        assert reports["recovered_again"]["starts"] == 2
        # Flooded while down: one more start (cooldown crossed), nothing delivered.
        assert reports["flooded"]["starts"] == 3 and len(reports["flooded"]["delivered"]) == 4
        # The next delivery attempt flushed the bounded queue (200 kept of 230, oldest
        # first); the document that arrived at the full queue was dropped, not delivered.
        delivered = reports["flushed"]["delivered"]
        assert len(delivered) == 4 + 200 and set(delivered[4:]) == {"session.idle"}
        assert reports["flushed"]["starts"] == 3
        # With the queue drained, delivery is direct again.
        assert reports["final"]["delivered"][-1] == "session.deleted" and len(reports["final"]["delivered"]) == 205
        assert reports["final"]["starts"] == 3


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


class TestInstaller:
    def test_fresh_install(self, repo):
        result = install_opencode_plugin(repo_root=repo, port=47811)
        assert result.status == "installed", result.message
        path = repo / PLUGIN_RELPATH
        text = path.read_text(encoding="utf-8")
        assert text.startswith(PLUGIN_MARKER) and f"v{PLUGIN_VERSION}" in text.splitlines()[0]
        assert "const PORT = 47811" in text and client.OPENCODE_HOOK_PATH in text
        assert "export const OpenShardCapture" in text and "export default" not in text
        for hook in ("session.created", "session.idle", "session.deleted", "file.edited", "message.updated",
                     '"chat.message"', '"tool.execute.after"'):
            assert hook in text
        assert detect_plugin(repo) == {"state": "openshard", "port": 47811, "version": PLUGIN_VERSION, "error": None}
        assert PLUGIN_RELPATH.as_posix() in (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")

    def test_idempotent_and_port_update(self, repo):
        install_opencode_plugin(repo_root=repo, port=47811)
        before = (repo / PLUGIN_RELPATH).read_bytes()
        again = install_opencode_plugin(repo_root=repo, port=47811)
        assert again.status == "already_installed" and (repo / PLUGIN_RELPATH).read_bytes() == before
        moved = install_opencode_plugin(repo_root=repo, port=47812)
        assert moved.status == "updated" and detect_plugin(repo)["port"] == 47812

    def test_preserves_unrelated_plugins_and_config(self, repo):
        plugins = repo / ".opencode" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "notify.ts").write_text("export const Notify = async () => ({})\n", encoding="utf-8")
        (repo / "opencode.json").write_text('{"$schema": "https://opencode.ai/config.json", "plugin": ["x"]}\n',
                                            encoding="utf-8")
        install_opencode_plugin(repo_root=repo, port=47811)
        assert (plugins / "notify.ts").read_text(encoding="utf-8") == "export const Notify = async () => ({})\n"
        assert (repo / "opencode.json").read_text(encoding="utf-8") == \
            '{"$schema": "https://opencode.ai/config.json", "plugin": ["x"]}\n'
        uninstall_opencode_plugin(repo_root=repo)
        assert (plugins / "notify.ts").exists() and not (repo / PLUGIN_RELPATH).exists()

    def test_user_owned_file_is_never_overwritten(self, repo):
        path = repo / PLUGIN_RELPATH
        path.parent.mkdir(parents=True)
        path.write_text("export const Mine = async () => ({})\n", encoding="utf-8")
        result = install_opencode_plugin(repo_root=repo, port=47811)
        assert result.status == "skipped_existing"
        assert path.read_text(encoding="utf-8") == "export const Mine = async () => ({})\n"
        assert detect_plugin(repo)["state"] == "custom"
        assert uninstall_opencode_plugin(repo_root=repo).status == "not_installed"
        assert path.exists()

    def test_uninstall(self, repo):
        assert uninstall_opencode_plugin(repo_root=repo).status == "not_installed"
        install_opencode_plugin(repo_root=repo, port=47811)
        _drive_inline(repo)
        result = uninstall_opencode_plugin(repo_root=repo)
        assert result.status == "removed" and not (repo / PLUGIN_RELPATH).exists()
        assert not (repo / ".opencode").exists()  # only the directories OpenShard created
        assert len(_lines(repo)) == 1  # history untouched


# ---------------------------------------------------------------------------
# CLI: capture install/uninstall opencode, setup, doctor
# ---------------------------------------------------------------------------


def _which(name: str):
    return {"opencode": "/usr/local/bin/opencode", "openshard": "/usr/local/bin/openshard"}.get(name)


class TestCli:
    def test_capture_install_and_uninstall_opencode(self, repo):
        runner = CliRunner()
        result = runner.invoke(cli, ["capture", "install", "opencode", "--repo-path", str(repo), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "installed" and data["configured"] is True
        assert (repo / PLUGIN_RELPATH).exists()
        result = runner.invoke(cli, ["capture", "uninstall", "opencode", "--repo-path", str(repo)])
        assert result.exit_code == 0 and "removed" in result.output
        assert not (repo / PLUGIN_RELPATH).exists()

    def test_setup_configures_opencode_when_detected(self, repo):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which):
            result = runner.invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(repo)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["agents"]["opencode"]["status"] == "installed"
        assert data["configured_agents"] == ["opencode"]
        assert (repo / PLUGIN_RELPATH).exists()
        with patch("shutil.which", side_effect=_which):
            again = runner.invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(repo)])
        assert json.loads(again.output)["agents"]["opencode"]["status"] == "already_installed"

    def test_doctor_reports_opencode_independently(self, repo):
        runner = CliRunner()
        with patch("shutil.which", side_effect=_which):
            before = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
            install_opencode_plugin(repo_root=repo, port=47811)
            after = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
            human = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
        assert json.loads(before.output)["opencode"]["configured"] is False
        data = json.loads(after.output)
        assert data["opencode"]["configured"] is True and data["opencode"]["port"] == 47811
        assert data["codex"]["cli_available"] is False
        assert "use OpenCode normally" in human.output
