"""Per-turn Receipts for Claude Code (docs/per-turn-receipts.md): on by default, ``OPENSHARD_TURN_RECEIPTS=off`` opts out.

Every test drives the real hook path (``handle_claude_hook`` / ``apply_reduced_hook``)
in a throw-away git repository with an isolated ``OPENSHARD_HOME`` (conftest).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest

from openshard.adapters import claude_hooks as ch
from openshard.adapters import claude_turn_receipts as turns
from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.history import receipt_signing as rs
from openshard.history.store import load_history
from openshard.history.task_identity import is_task_id, new_task_id
from openshard.sync import client, config, envelope, transport
from tests.capture_fixtures import _git, _make_repo

SID = "0f1e2d3c-4b5a-4697-8877-665544332211"
ENDPOINT = "https://platform.example.test"
ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
KEY = "osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz"
SCHEMA = json.loads(
    (Path(__file__).parent / "fixtures" / "platform" / "receipt-sync-envelope.v1.json").read_text(encoding="utf-8")
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "widget")


@pytest.fixture
def turn_flag(monkeypatch):
    monkeypatch.setenv(turns.TURN_RECEIPTS_ENV, "on")


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setenv(turns.TURN_RECEIPTS_ENV, "off")


@pytest.fixture(autouse=True)
def _claude_config_dir(tmp_path: Path, monkeypatch) -> Path:
    config_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    return config_dir


def _transcript_path(session_id: str = SID) -> Path:
    directory = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "c--work-widget"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{session_id}.jsonl"


def _assistant(message_id: str, *, input_tokens: int, output_tokens: int) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    return json.dumps({"type": "assistant", "sessionId": SID, "timestamp": stamp,
                       "message": {"id": message_id, "model": "claude-opus-5-5", "role": "assistant",
                                   "content": [{"type": "text", "text": "RAW"}], "usage": usage}}) + "\n"


def _hook(repo: Path, event: str, **fields):
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event,
               "transcript_path": str(_transcript_path()), **fields}
    return handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _write(repo: Path, name: str) -> None:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _hook(repo, "PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / name)},
          tool_response={"filePath": str(repo / name)})


def _entries(repo: Path) -> list[dict]:
    return load_history(repo / ".openshard" / "runs.jsonl", coerce=False)


def _two_turns(repo: Path, *, end: bool = False) -> None:
    """Turn 1 writes and commits a.py; turn 2 writes b.py. Each turn's usage is in the transcript."""
    transcript = _transcript_path()
    _hook(repo, "SessionStart", source="startup")
    _hook(repo, "UserPromptSubmit", prompt="add a")
    _write(repo, "a.py")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "a")
    _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "git add a.py && git commit -m a"},
          tool_response={"stdout": "", "stderr": ""})
    transcript.write_text(_assistant("msg_1", input_tokens=100, output_tokens=10), encoding="utf-8")
    _hook(repo, "Stop")
    time.sleep(1.1)  # hook timestamps are whole seconds: turn 2 must start after turn 1's usage
    _hook(repo, "UserPromptSubmit", prompt="add b")
    _write(repo, "b.py")
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(_assistant("msg_2", input_tokens=200, output_tokens=20))
    _hook(repo, "Stop")
    if end:
        _hook(repo, "SessionEnd", reason="other")


def _paths(entry: dict) -> set[str]:
    return {f["path"] for f in entry.get("files_detail") or [] if f.get("attribution") != "pre_existing"}


class TestTwoPromptsOneSession:
    def test_two_receipts_with_own_ids_one_task_and_per_turn_evidence(self, repo, turn_flag):
        head_before = (ch.run_git(repo, ["rev-parse", "HEAD"]) or "").strip()
        _two_turns(repo)
        first, second = _entries(repo)
        assert first["receipt_id"] != second["receipt_id"] and first["run_id"] != second["run_id"]
        assert is_task_id(first["task_id"]) and first["task_id"] == second["task_id"]
        for n, entry in enumerate((first, second), start=1):
            capture = entry["capture"]
            assert capture["receipt_scope"] == "turn" and capture["turn_index"] == n
            assert capture["task_grouping"] == "session"
            assert isinstance(capture["turn_sealed_at"], str) and capture["status"] == "turn_completed"
            assert capture["session_end_observed"] is False  # a completed turn is not a session end
            assert capture["prompt_count"] == 1 and capture["turn_count"] == 1
        assert second["capture"]["start_source"] == "turn"
        assert second["capture"]["resumed_from_receipt_id"] == first["receipt_id"]
        assert (first["task"], second["task"]) == ("add a", "add b")
        # Files: the diff between each turn's start and end.
        assert _paths(first) == {"a.py"} and _paths(second) == {"b.py"}
        # Commits: only the ones made during the turn.
        commit_a = (ch.run_git(repo, ["rev-parse", "HEAD"]) or "").strip()
        assert first["git_head_commit_hash"] == head_before and first["session_commits"]["shas"] == [commit_a]
        assert second["git_head_commit_hash"] == commit_a and second["session_commits"]["shas"] == []
        # Usage: the transcript between the two turn boundaries.
        assert (first["prompt_tokens"], first["completion_tokens"]) == (100, 10)
        assert (second["prompt_tokens"], second["completion_tokens"]) == (200, 20)

    def test_turn_receipts_are_signed_when_the_turn_completes(self, repo, turn_flag):
        _two_turns(repo)
        history = repo / ".openshard"
        seals = [line for line in rs.read_sidecar(history) if rs.is_envelope(line)]
        by_receipt = {}
        for env_line in seals:
            statement = rs.envelope_payload(env_line)[1]
            by_receipt[rs._statement_receipt_id(statement)] = statement["predicate"]["seal_reason"]
        stored = _entries(repo)
        assert by_receipt == {e["receipt_id"]: rs.SEAL_TURN_COMPLETED for e in stored}
        for index, entry in enumerate(stored):
            assert rs.signature_status(history / "runs.jsonl", entry, index).verdict == rs.VERDICT_VERIFIED

    def test_session_end_after_a_sealed_turn_adds_nothing_and_changes_nothing(self, repo, turn_flag):
        _two_turns(repo)
        before = (repo / ".openshard" / "runs.jsonl").read_bytes()
        out = _hook(repo, "SessionEnd", reason="other")
        assert out.action == "ignored"
        assert (repo / ".openshard" / "runs.jsonl").read_bytes() == before

    def test_agent_continuing_after_stop_opens_the_next_receipt_instead_of_dropping_it(self, repo, turn_flag):
        _hook(repo, "UserPromptSubmit", prompt="add a")
        _write(repo, "a.py")
        _hook(repo, "Stop")
        _write(repo, "c.py")  # another Stop hook blocked; the agent kept working
        _hook(repo, "Stop", stop_hook_active=True)
        first, second = _entries(repo)
        assert _paths(second) == {"c.py"} and second["capture"]["turn_index"] == 2
        assert second["task_id"] == first["task_id"]

    def test_declared_task_wins_and_is_marked_explicit(self, repo, turn_flag):
        root = repo.resolve()
        task = new_task_id()
        for event, extra in (("UserPromptSubmit", {"task_excerpt": "one"}), ("Stop", {}),
                             ("UserPromptSubmit", {"task_excerpt": "two"}), ("Stop", {})):
            ch.apply_reduced_hook(ch.ReducedHookPayload(event=event, session_id=SID, task_id=task, **extra), root)
        first, second = _entries(repo)
        assert first["task_id"] == second["task_id"] == task
        assert first["capture"]["task_grouping"] == second["capture"]["task_grouping"] == "explicit"
        doc = envelope.build_envelope(second, 1, core_version="9.9.9")
        assert doc["receipt"]["task_id"] == task and doc["receipt"]["task_grouping"] == "explicit"
        errors = list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(doc))
        assert not errors, [e.message for e in errors[:3]]


class TestSync:
    @pytest.fixture
    def env(self, tmp_path: Path) -> dict:
        return {"OPENSHARD_HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "")}

    @pytest.fixture
    def recording(self, env):
        config.save_link(endpoint=ENDPOINT, organisation_id=ORG, api_key=KEY, env=env)
        rt = transport.RecordingPlatformTransport()
        client.configure(transport=rt, repo_config={})
        yield rt
        client.configure(transport=None, repo_config=None)

    def test_a_sealed_turn_syncs_while_the_session_is_still_open(self, repo, turn_flag, env, recording):
        _hook(repo, "UserPromptSubmit", prompt="add a")
        _write(repo, "a.py")
        _hook(repo, "Stop")
        _hook(repo, "UserPromptSubmit", prompt="add b")  # turn 2 in progress, session open
        sealed, live = _entries(repo)
        assert envelope.eligibility(sealed) == envelope.Eligibility(True, envelope.REASON_TURN_SEALED)
        assert envelope.eligibility(live).eligible is False
        report = client.flush(repo, env=env)
        assert (report.sent, report.in_progress) == (1, 1)
        [sent] = recording.envelopes
        assert sent["receipt"]["receipt_id"] == sealed["receipt_id"]
        # A session grouping is sent, labelled as one (Platform #243), never as a declared task.
        assert sent["receipt"]["task_id"] == sealed["task_id"] and sent["receipt"]["task_grouping"] == "session"
        session = sent["receipt"]["session"]
        assert session["ended"] is False
        assert (session["receipt_scope"], session["turn_index"], session["previous_receipt_id"]) == ("turn", 1, None)
        errors = list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(sent))
        assert not errors, [e.message for e in errors[:3]]

    def test_turn_two_names_the_previous_turn_and_validates_strictly(self, repo, turn_flag):
        _two_turns(repo)
        first, second = _entries(repo)
        doc = envelope.build_envelope(second, 1, core_version="9.9.9")
        session = doc["receipt"]["session"]
        assert (session["receipt_scope"], session["turn_index"]) == ("turn", 2)
        assert session["previous_receipt_id"] == first["receipt_id"]
        assert session["session_key"] == envelope.build_envelope(first, 0, core_version="9.9.9")["receipt"]["session"]["session_key"]
        assert doc["receipt"]["task_id"] == first["task_id"] and doc["receipt"]["task_grouping"] == "session"
        errors = list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(doc))
        assert not errors, [e.message for e in errors[:3]]

    def test_session_receipts_send_none_of_the_turn_fields(self, repo, flag_off):
        _two_turns(repo, end=True)
        [entry] = _entries(repo)
        receipt = envelope.build_envelope(entry, 0, core_version="9.9.9")["receipt"]
        assert "task_grouping" not in receipt
        assert not {"receipt_scope", "turn_index", "previous_receipt_id"} & set(receipt["session"])

    def test_seal_point_matches_sync_eligibility_for_turn_receipts(self):
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for capture in (
            {"session_end_observed": False, "receipt_scope": "turn", "last_activity_at": now},
            {"session_end_observed": False, "receipt_scope": "turn", "turn_sealed_at": now, "last_activity_at": now},
        ):
            entry = {"receipt_id": "rcpt_" + "a" * 32, "task": "t", "capture": capture}
            assert (rs.seal_point(entry) is not None) == envelope.eligibility(entry).eligible

    def test_request_sync_wakes_the_periodic_loop(self, tmp_path, monkeypatch):
        flushed = threading.Event()
        monkeypatch.setattr(client, "resolve_link", lambda env: object())
        monkeypatch.setattr(client, "flush_repos", lambda roots, env=None, transport=None: flushed.set() or [])
        stop = threading.Event()
        thread = threading.Thread(target=client.sync_periodically, args=(stop,),
                                  kwargs={"repos": lambda: [tmp_path], "env": {}, "interval": 3600.0}, daemon=True)
        thread.start()
        try:
            client.request_sync()
            assert flushed.wait(5.0), "a sealed turn must not wait for the next periodic tick"
        finally:
            stop.set()
            thread.join(5.0)
        assert not thread.is_alive()


class TestDefaultAndOptOut:
    def test_on_by_default(self, repo, monkeypatch):
        monkeypatch.delenv(turns.TURN_RECEIPTS_ENV, raising=False)
        _two_turns(repo)
        assert [e["capture"]["receipt_scope"] for e in _entries(repo)] == ["turn", "turn"]

    def test_opt_out_keeps_one_session_receipt(self, repo, flag_off):
        _two_turns(repo, end=True)
        [entry] = _entries(repo)
        capture = entry["capture"]
        assert "task_id" not in entry
        for key in ("receipt_scope", "turn_index", "turn_sealed_at", "task_grouping"):
            assert key not in capture
        assert capture["session_end_observed"] is True and capture["prompt_count"] == 2
        assert capture["start_source"] == "startup"
        assert _paths(entry) == {"a.py", "b.py"}

    def test_config_opts_out_and_the_environment_wins(self, repo):
        assert turns.enabled(repo, env={}) is True and turns.enabled(repo / "missing", env={}) is True
        (repo / ".openshard").mkdir(exist_ok=True)
        config = repo / ".openshard" / "config.yml"
        config.write_text("turn_receipts: true\n", encoding="utf-8")
        assert turns.enabled(repo, env={}) is True
        assert turns.enabled(repo, env={turns.TURN_RECEIPTS_ENV: "off"}) is False
        config.write_text("turn_receipts: false\n", encoding="utf-8")
        assert turns.enabled(repo, env={}) is False
        assert turns.enabled(repo, env={turns.TURN_RECEIPTS_ENV: "on"}) is True

    def test_a_session_already_open_at_upgrade_stays_one_session_receipt(self, repo, turn_flag):
        # A buffer an older Core opened has no scope; after two prompts it is not one turn.
        assert turns.turn_scoped({"agent": "claude_code", "prompt_count": 2}, repo) is False
        assert turns.turn_scoped({"agent": "claude_code", "prompt_count": 1, "turn_count": 2}, repo) is False
        assert turns.turn_scoped({"agent": "claude_code", "prompt_count": 1, "turn_count": 1}, repo) is True

    def test_a_session_open_across_the_upgrade_keeps_its_session_receipt(self, repo, flag_off, monkeypatch):
        _hook(repo, "UserPromptSubmit", prompt="add a")
        _write(repo, "a.py")
        _hook(repo, "Stop")
        [before] = _entries(repo)
        buffer = ch.buffer_path(repo.resolve(), SID, "claude_code")
        staged = json.loads(buffer.read_text(encoding="utf-8"))
        staged.pop("receipt_scope", None)  # the buffer an older Core left behind
        buffer.write_text(json.dumps(staged), encoding="utf-8")
        monkeypatch.setenv(turns.TURN_RECEIPTS_ENV, "on")
        _hook(repo, "UserPromptSubmit", prompt="add b")
        _write(repo, "b.py")
        _hook(repo, "Stop")
        [after] = _entries(repo)
        assert after["receipt_id"] == before["receipt_id"] and after["capture"]["prompt_count"] == 2
        for key in ("receipt_scope", "turn_index", "turn_sealed_at", "task_grouping"):
            assert key not in after["capture"]

    def test_other_agents_are_never_turn_scoped(self, repo, turn_flag):
        assert turns.turn_scoped({"agent": "codex"}, repo) is False
        assert turns.turn_scoped({"agent": "claude_code"}, repo) is True


class TestCaptureService:
    def test_service_syncs_a_completed_turn_within_seconds_while_the_chat_stays_open(
        self, turn_flag, capture_env, tmp_path
    ):
        from tests.capture_fixtures import _payload, _post, _Service, _wait_for

        repo = _make_repo(tmp_path / "svc repo")
        config.save_link(endpoint=ENDPOINT, organisation_id=ORG, api_key=KEY, env=capture_env)
        rt = transport.RecordingPlatformTransport()
        client.configure(transport=rt, repo_config={})
        running = _Service(capture_env)
        try:
            for event, extra in (("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "add a"})):
                assert _post(running.port, _payload(event, repo, **extra), project_dir=str(repo))
            (repo / "a.py").write_text("a\n", encoding="utf-8")
            assert _post(running.port, _payload("PostToolUse", repo, tool_name="Write",
                                                tool_input={"file_path": str(repo / "a.py")},
                                                tool_response={"filePath": str(repo / "a.py")}),
                         project_dir=str(repo))
            started = time.monotonic()
            assert _post(running.port, _payload("Stop", repo), project_dir=str(repo))
            assert _wait_for(lambda: len(rt.envelopes) == 1, timeout=20.0), "turn was not synced"
            elapsed = time.monotonic() - started
            assert elapsed < 15.0  # the periodic tick is 300 s; a wake must not wait for it
            assert rt.envelopes[0]["receipt"]["session"]["ended"] is False
        finally:
            running.stop()
            client.configure(transport=None, repo_config=None)


class TestRendering:
    def _receipts(self, repo: Path):
        from openshard.history.shard_contract import build_shard_receipt

        return [build_shard_receipt(e, index=i) for i, e in enumerate(_entries(repo))]

    def test_last_and_history_name_the_turn(self, repo, turn_flag):
        from openshard.cli.visibility import _history_row_lines
        from openshard.history.query import recent_shards
        from openshard.history.receipt_render import (
            render_compact_shard_receipt,
            render_full_shard_receipt,
        )

        _two_turns(repo)
        first, second = self._receipts(repo)
        key = second.recorded_evidence["session"]["session_key"]
        assert f"  Turn        2 of session {key[:12]}" in render_compact_shard_receipt(second).splitlines()
        assert f"  Turn        1 of session {key[:12]}" in render_full_shard_receipt(first).splitlines()
        rows = [line for item in recent_shards(limit=5, repo_path=repo).items for line in _history_row_lines(item)]
        assert any(f"Turn 2 of session {key[:12]}" in line for line in rows)

    def test_session_receipts_render_without_a_turn_row(self, repo, flag_off):
        from openshard.history.receipt_render import render_compact_shard_receipt

        _two_turns(repo, end=True)
        [receipt] = self._receipts(repo)
        assert not any(line.startswith("  Turn ") for line in render_compact_shard_receipt(receipt).splitlines())


class TestPostSessionVerifyPerTurn:
    def _calls(self, monkeypatch) -> list[str]:
        from openshard.verification import auto

        calls: list[str] = []
        monkeypatch.setattr(auto, "schedule_post_session_verify",
                            lambda root, entry, **_: calls.append(entry["receipt_id"]) or True)
        return calls

    def test_runs_for_a_sealed_turn_that_changed_files_only(self, repo, turn_flag, monkeypatch):
        from openshard.verification import auto

        _two_turns(repo)  # turn 1 commits a.py; turn 2 leaves b.py uncommitted
        _hook(repo, "UserPromptSubmit", prompt="explain b")  # turn 3 changes nothing
        _hook(repo, "Stop")
        first, second, third = _entries(repo)
        calls = self._calls(monkeypatch)
        monkeypatch.setattr(auto, "schedule_commit_verify", lambda root, entry, **_: True)
        for entry in (first, second, third):
            auto.schedule_for_receipt(repo, entry, finalized=False, turn_ended=True)
        assert [auto.sealed_turn_changed_files(e) for e in (first, second, third)] == [True, True, False]
        assert calls == [second["receipt_id"]]  # turn 1 has its commit verified instead; turn 3 has nothing to run

    def test_an_open_turn_and_a_session_receipt_are_not_run_at_a_stop(self, repo, flag_off, monkeypatch):
        from openshard.verification import auto

        _hook(repo, "UserPromptSubmit", prompt="add a")
        _write(repo, "a.py")
        _hook(repo, "Stop")
        [entry] = _entries(repo)
        calls = self._calls(monkeypatch)
        assert auto.schedule_for_receipt(repo, entry, finalized=False, turn_ended=True) is False
        assert calls == [] and auto.sealed_turn_changed_files(entry) is False


def test_each_sealed_turn_is_a_completed_receipt_for_telemetry(repo, turn_flag, monkeypatch):
    actions: list[tuple[str, str]] = []
    monkeypatch.setattr(ch, "_emit_receipt_telemetry", lambda entry, action: actions.append((entry["receipt_id"], action)))
    _two_turns(repo)
    first, second = _entries(repo)
    for entry in (first, second):
        assert (entry["receipt_id"], "record_finalized") in actions
        assert [a for r, a in actions if r == entry["receipt_id"]].count("record_finalized") == 1
