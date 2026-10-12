"""Reconstructing a Receipt from a Claude Code transcript whose hook events were lost.

The transcript is the only surviving evidence of such a session. The
reconstruction must keep its timestamps and counts, label every fact as
reconstructed, store nothing sensitive, invent nothing (no git state, cost or
verification), and never write over a session that already has a Receipt.
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from openshard.adapters import claude_transcript_import as cti
from openshard.history.store import load_history
from tests.capture_fixtures import _make_repo

SID = "46b08ca8-5ca7-4f40-8b41-d68bd1e3c94a"
SECRET = "sk-ant-api03-ThisIsAFakeSecretValueThatMustNeverBeStored0123456789"


def _record(kind: str, at: str, cwd: str, content, **extra) -> dict:
    base = {"type": kind, "timestamp": at, "cwd": cwd, "sessionId": SID, "gitBranch": "wt-branch",
            "version": "2.1.296", "uuid": f"u-{at}-{kind}", "isSidechain": False,
            "message": {"role": "user" if kind == "user" else "assistant", "content": content}}
    base.update(extra)
    return base


def _assistant(at: str, cwd: str, blocks: list[dict], *, msg_id: str, stop: str, usage: dict | None = None) -> dict:
    rec = _record("assistant", at, cwd, blocks)
    rec["message"].update({"id": msg_id, "model": "claude-sonnet-5-5", "stop_reason": stop,
                           "usage": usage or {"input_tokens": 10, "output_tokens": 5,
                                              "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}})
    return rec


def _tool_use(tool_id: str, name: str, **tool_input) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def _tool_result(tool_id: str, *, is_error: bool = False) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": "raw output never stored", "is_error": is_error}


def _write_transcript(path: Path, repo: Path, *, worktree: Path | None = None) -> None:
    wt = str(worktree or repo)
    records = [
        {"type": "attachment", "timestamp": "2026-10-12T09:22:40.000Z", "cwd": str(repo), "sessionId": SID,
         "attachment": {"type": "environment", "snapshot": {"isWorktree": False}}},
        _record("user", "2026-10-12T09:22:47.000Z", str(repo), f"Fix the parser, token {SECRET} is the key"),
        _assistant("2026-10-12T09:22:50.000Z", str(repo), [_tool_use("t1", "Bash", command=f"git status && echo {SECRET}")],
                   msg_id="m1", stop="tool_use"),
        _record("user", "2026-10-12T09:22:51.000Z", str(repo), [_tool_result("t1")]),
        _assistant("2026-10-12T09:22:52.000Z", wt, [_tool_use("t2", "Write", file_path=str(Path(wt) / "parser.py"), content="RAW")],
                   msg_id="m2", stop="tool_use"),
        _record("user", "2026-10-12T09:22:53.000Z", wt, [_tool_result("t2")]),
        _assistant("2026-10-12T09:22:54.000Z", wt, [_tool_use("t3", "Write", file_path="C:/elsewhere/notes.txt", content="x")],
                   msg_id="m3", stop="tool_use"),
        _record("user", "2026-10-12T09:22:55.000Z", wt, [_tool_result("t3", is_error=True)]),
        _assistant("2026-10-12T09:22:56.000Z", wt, [_tool_use("t4", "Bash", command="python -m pytest -q")],
                   msg_id="m4", stop="tool_use"),
        # The same message streamed twice: its tool use and usage must count once.
        _assistant("2026-10-12T09:22:56.500Z", wt, [_tool_use("t4", "Bash", command="python -m pytest -q")],
                   msg_id="m4", stop="tool_use"),
        _record("user", "2026-10-12T09:22:57.000Z", wt, [_tool_result("t4")]),
        _assistant("2026-10-12T09:22:58.000Z", wt, [{"type": "text", "text": "done"}], msg_id="m5", stop="end_turn"),
        _record("user", "2026-10-12T09:23:10.000Z", wt, [{"type": "text", "text": "Thanks, now add a test"}]),
        _assistant("2026-10-12T09:23:12.000Z", wt, [{"type": "text", "text": "ok"}], msg_id="m6", stop="end_turn"),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = _make_repo(tmp_path / "platform")
    worktree = tmp_path / "plat-wt"
    worktree.mkdir()
    transcript = tmp_path / "claude" / "projects" / "p" / f"{SID}.jsonl"
    _write_transcript(transcript, repo, worktree=worktree)
    return repo, worktree, transcript


def test_reconstruction_keeps_counts_timestamps_and_usage_and_labels_itself(tmp_path):
    repo, _worktree, transcript = _setup(tmp_path)
    report = cti.import_transcript(transcript, repo_path=repo)
    assert report.status == "dry_run" and report.session_id == SID
    entry = report.entry
    capture = entry["capture"]
    assert entry["executor"] == cti.EXECUTOR and entry["import_method"] == cti.IMPORT_METHOD
    assert capture["source"] == cti.CAPTURE_SOURCE and capture["status"] == "reconstructed"
    assert capture["session_id"] == SID
    assert capture["started_at"] == "2026-10-12T09:22:47.000Z" and capture["last_activity_at"] == "2026-10-12T09:23:12.000Z"
    assert entry["timestamp"] == "2026-10-12T09:22:47.000Z"
    assert capture["prompt_count"] == 2 and capture["turn_count"] == 2
    assert capture["tool_call_count"] == 4 and capture["tool_failure_count"] == 1
    assert capture["models_seen"] == ["claude-sonnet-5-5"] and capture["model_source"] == "transcript"
    assert capture["completeness"]["status"] == "partial"
    for reason in (cti.REASON_RECONSTRUCTED, cti.REASON_NO_GIT, cti.REASON_NO_END, cti.REASON_NO_COST):
        assert reason in capture["completeness"]["reasons"]
    # Usage is the provider's, counted once per message (m4 streamed twice, 6 messages).
    assert entry["prompt_tokens"] == 60 and entry["completion_tokens"] == 30
    assert entry["cache_read_tokens"] == 600 and entry["cache_creation_tokens"] == 120
    assert entry["tokens_provenance"] == "provider_reported"
    # Nothing invented.
    assert entry["git_branch"] is None and entry["git_head_commit_hash"] is None
    assert "estimated_cost" not in entry and capture["cost_not_recorded_reason"] == cti.REASON_NO_COST
    assert entry["verification"]["status"] == "unknown" and entry["verification_attempted"] is False
    assert capture["reconstructed"]["branches_reported"] == ["wt-branch"]
    assert capture["reconstructed"]["checkouts"] == ["platform", "plat-wt"]
    assert capture["session_end_observed"] is False
    # Every Event is marked as transcript evidence, with the transcript's own timestamps.
    assert entry["events"][0]["event_type"] == "session.started"
    tool_events = [e for e in entry["events"] if e["event_type"] == "tool.invoked"]
    assert [e["occurred_at"] for e in tool_events] == [
        "2026-10-12T09:22:50.000Z", "2026-10-12T09:22:52.000Z", "2026-10-12T09:22:54.000Z", "2026-10-12T09:22:56.000Z",
    ]
    assert {e["evidence"] for e in entry["events"]} == {"imported_transcript"}
    assert [e["status"] for e in tool_events] == ["unknown", "unknown", "failed", "unknown"]


def test_reconstruction_stores_nothing_sensitive_and_drops_paths_outside_the_repository(tmp_path):
    repo, _worktree, transcript = _setup(tmp_path)
    entry = cti.import_transcript(transcript, repo_path=repo).entry
    blob = json.dumps(entry)
    assert SECRET not in blob
    assert "raw output never stored" not in blob and "RAW" not in blob
    assert "elsewhere" not in blob and "notes.txt" not in blob
    assert str(repo) not in blob  # never an absolute path
    assert entry["task"].startswith("Fix the parser")
    tool_events = [e for e in entry["events"] if e["event_type"] == "tool.invoked"]
    assert tool_events[2]["action"] == "Write (path outside the repository)" and tool_events[2]["target"] is None
    assert tool_events[3]["action"].startswith("Bash:") and "pytest" in tool_events[3]["action"]
    assert tool_events[3]["metadata"]["command_kind"] == "test"
    assert tool_events[1]["metadata"]["checkout"] == "plat-wt"


def test_a_worktree_file_is_outside_the_project_and_not_claimed(tmp_path):
    """The Write happened in a separate worktree directory: the project's reconstruction does not list it."""
    repo, worktree, transcript = _setup(tmp_path)
    entry = cti.import_transcript(transcript, repo_path=repo).entry
    assert entry["files_detail"] == []
    # Reconstructing for the worktree itself attributes the file there.
    wt_entry = cti.import_transcript(transcript, repo_path=worktree).entry
    assert [f["path"] for f in wt_entry["files_detail"]] == ["parser.py"]
    assert wt_entry["files_detail"][0]["attribution"] == "agent_reported"


def test_writing_is_opt_in_and_idempotent(tmp_path):
    repo, _worktree, transcript = _setup(tmp_path)
    runs = repo / ".openshard" / "runs.jsonl"
    assert cti.import_transcript(transcript, repo_path=repo).status == "dry_run"
    assert not runs.exists()
    first = cti.import_transcript(transcript, repo_path=repo, write=True)
    assert first.status == "written" and first.receipt_id == cti.receipt_id_for_session(SID)
    second = cti.import_transcript(transcript, repo_path=repo, write=True)
    assert second.status == "already_recorded" and second.existing == [first.receipt_id]
    (stored,) = load_history(runs, coerce=False)
    assert stored["receipt_id"] == first.receipt_id and stored["content_hash"].startswith("sha256:")


def test_a_session_with_a_hook_captured_receipt_is_never_reconstructed(tmp_path):
    repo, _worktree, transcript = _setup(tmp_path)
    runs = repo / ".openshard" / "runs.jsonl"
    runs.parent.mkdir(parents=True)
    sealed = {"receipt_id": "rcpt_" + "a" * 32, "timestamp": "2026-10-12T09:22:44Z", "task": "captured live",
              "capture": {"source": "claude_code_hooks", "session_id": SID, "status": "ended"}}
    runs.write_text(json.dumps(sealed) + "\n", encoding="utf-8")
    report = cti.import_transcript(transcript, repo_path=repo, write=True)
    assert report.status == "already_recorded" and report.existing == [sealed["receipt_id"]]
    assert runs.read_text(encoding="utf-8") == json.dumps(sealed) + "\n"


def test_a_file_that_is_not_a_transcript_is_refused(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    bogus = tmp_path / "notes.jsonl"
    bogus.write_text('{"type": "summary"}\nnot json\n', encoding="utf-8")
    assert cti.import_transcript(bogus, repo_path=repo).status == "not_a_transcript"


def test_cli_dry_run_reports_and_write_records(tmp_path):
    from openshard.cli.main import cli

    repo, _worktree, transcript = _setup(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["import", "claude-transcript", str(transcript), "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "Would write reconstructed Receipt" in result.output and "Dry run" in result.output
    assert "cost: not recorded" in result.output and "git state: not observed" in result.output
    assert not (repo / ".openshard" / "runs.jsonl").exists()
    result = runner.invoke(cli, ["import", "claude-transcript", str(transcript), "--repo", str(repo), "--write", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "written" and payload["entry"]["capture"]["status"] == "reconstructed"
    result = runner.invoke(cli, ["import", "claude-transcript", str(transcript), "--repo", str(repo), "--write"])
    assert result.exit_code == 0 and "already has a Receipt here" in result.output
