"""v0.4.4 Phase 1/5 -- change causality and file attribution.

Git proves the repository state changed; it does not prove which process
changed it. These tests pin the attribution model: files dirty before the
session are excluded, files the agent reported (with a positive success
signal) are ``agent_reported``, everything else git shows is
``git_observed`` (actor unknown), and files another live session reported
are ``other_session``. The temporal diff is never turned into actor
attribution.

All sessions here run through the synchronous in-process path
(``handle_claude_hook``) so the assertions are deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

from openshard.adapters.claude_hooks import HookOutcome, handle_claude_hook
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt
from tests.capture_fixtures import SID, SID2


def _payload(event: str, repo: Path, session_id: str = SID, **fields) -> dict:
    base: dict = {"session_id": session_id, "cwd": str(repo), "hook_event_name": event}
    base.update(fields)
    return base


def _run(repo: Path, event: str, session_id: str = SID, **fields) -> HookOutcome:
    return handle_claude_hook(_payload(event, repo, session_id, **fields), env={"CLAUDE_PROJECT_DIR": str(repo)})


def _start(repo: Path, sid: str = SID) -> None:
    assert _run(repo, "SessionStart", sid, source="startup").action != "error"
    assert _run(repo, "UserPromptSubmit", sid, prompt="Do the work").action != "error"


def _agent_write(repo: Path, rel: str, text: str, sid: str = SID, tool: str = "Write") -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")
    out = _run(repo, "PostToolUse", sid, tool_name=tool, tool_input={"file_path": str(repo / rel), "content": text})
    assert out.action != "error"


def _finish(repo: Path, sid: str = SID) -> dict:
    assert _run(repo, "Stop", sid).action != "error"
    assert _run(repo, "SessionEnd", sid, reason="prompt_input_exit").action != "error"
    return _entry_for(repo, sid)


def _entries(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _entry_for(repo: Path, sid: str) -> dict:
    matches = [e for e in _entries(repo) if e.get("capture", {}).get("session_id") == sid]
    assert len(matches) == 1, matches
    return matches[0]


def _by_path(entry: dict) -> dict[str, dict]:
    return {f["path"]: f for f in entry["files_detail"]}


class TestBaselineAndAttribution:
    def test_clean_repo_agent_edit_is_agent_reported(self, repo):
        _start(repo)
        _agent_write(repo, "calc.py", "x = 1\n")
        entry = _finish(repo)
        files = _by_path(entry)
        assert set(files) == {"calc.py"}
        assert files["calc.py"]["attribution"] == "agent_reported"
        assert entry["files_created"] + entry["files_updated"] == 1
        changes = entry["changes"]
        assert changes["agent_reported"] == 1
        assert changes["git_observed"] == 0
        assert changes["pre_existing_excluded"] == 0
        assert changes["baseline"]["source"] == "git_status"
        assert changes["baseline"]["dirty_paths"] == 0

    def test_file_dirty_before_session_is_excluded(self, repo):
        # A tracked file is modified *before* the agent session starts.
        (repo / "README.md").write_text("edited by a human earlier\n", encoding="utf-8")
        _start(repo)
        _agent_write(repo, "calc.py", "x = 1\n")
        entry = _finish(repo)
        files = _by_path(entry)
        assert files["README.md"]["attribution"] == "pre_existing"
        assert files["calc.py"]["attribution"] == "agent_reported"
        # The receipt's changed-file count must not include the pre-existing edit.
        assert entry["files_created"] + entry["files_updated"] + entry["files_deleted"] == 1
        assert entry["changes"]["pre_existing_excluded"] == 1
        assert entry["changes"]["baseline"]["dirty_paths"] == 1
        receipt = build_shard_receipt(entry)
        assert receipt.files_changed == 1
        rendered = render_compact_shard_receipt(receipt)
        assert "Excluded" in rendered and "1 pre-existing" in rendered
        # The event stream does not claim README.md as this session's change.
        changed_targets = [e["target"] for e in entry["events"] if e["event_type"] == "file.changed"]
        assert "README.md" not in changed_targets

    def test_untracked_file_before_session_is_excluded(self, repo):
        (repo / "scratch.txt").write_text("left over from before\n", encoding="utf-8")
        _start(repo)
        _agent_write(repo, "calc.py", "x = 1\n")
        entry = _finish(repo)
        files = _by_path(entry)
        assert files["scratch.txt"]["attribution"] == "pre_existing"
        assert files["scratch.txt"]["change_type"] == "create"
        assert entry["files_created"] == 1  # calc.py only
        assert entry["changes"]["pre_existing_excluded"] == 1

    def test_git_only_change_is_git_observed_not_agent(self, repo):
        _start(repo)
        # Something (a human, another tool) edits a file; no hook reports it.
        (repo / "notes.md").write_text("typed by hand during the session\n", encoding="utf-8")
        entry = _finish(repo)
        files = _by_path(entry)
        assert files["notes.md"]["attribution"] == "git_observed"
        assert entry["changes"]["git_observed"] == 1
        assert entry["changes"]["agent_reported"] == 0
        ev = next(e for e in entry["events"] if e["event_type"] == "file.changed" and e["target"] == "notes.md")
        assert ev["evidence"] == "git_observed"
        assert ev["metadata"]["attribution"] == "git_observed"
        rendered = render_compact_shard_receipt(build_shard_receipt(entry))
        assert "git-observed" in rendered.lower()
        # Never described as the agent's work.
        assert "reported by Claude Code hook" not in files["notes.md"]["summary"]

    def test_pre_existing_file_modified_again_during_session_is_git_observed_with_flag(self, repo):
        (repo / "README.md").write_text("dirty before\n", encoding="utf-8")
        _start(repo)
        # Concurrent independent change to the *same* pre-existing dirty file.
        (repo / "README.md").write_text("dirty before, then changed again during the session\n", encoding="utf-8")
        entry = _finish(repo)
        files = _by_path(entry)
        assert files["README.md"]["attribution"] == "git_observed"
        assert files["README.md"]["pre_existing"] is True
        assert entry["changes"]["git_observed"] == 1
        assert entry["changes"]["pre_existing_excluded"] == 0

    def test_agent_edit_of_pre_existing_dirty_file_stays_agent_reported_and_flagged(self, repo):
        (repo / "README.md").write_text("dirty before\n", encoding="utf-8")
        _start(repo)
        _agent_write(repo, "README.md", "dirty before, then edited by the agent\n", tool="Edit")
        entry = _finish(repo)
        files = _by_path(entry)
        assert files["README.md"]["attribution"] == "agent_reported"
        assert files["README.md"]["pre_existing"] is True

    def test_deleted_pre_existing_change_is_excluded(self, repo):
        (repo / "README.md").unlink()
        _start(repo)
        entry = _finish(repo)
        files = _by_path(entry)
        assert files["README.md"]["attribution"] == "pre_existing"
        assert entry["files_deleted"] == 0
        assert entry["changes"]["pre_existing_excluded"] == 1

    def test_baseline_is_taken_at_first_observed_hook(self, repo):
        _start(repo)
        entry = _finish(repo)
        assert entry["changes"]["baseline"]["at"] == entry["capture"]["started_at"]
        assert entry["changes"]["baseline"]["truncated"] is False


class TestMultipleSessions:
    def test_other_session_reported_paths_are_excluded(self, repo):
        _start(repo, SID)
        _start(repo, SID2)
        _agent_write(repo, "a.py", "a\n", sid=SID)
        _agent_write(repo, "b.py", "b\n", sid=SID2)
        entry_a = _finish(repo, SID)
        files_a = _by_path(entry_a)
        assert files_a["a.py"]["attribution"] == "agent_reported"
        assert files_a["b.py"]["attribution"] == "other_session"
        assert entry_a["files_created"] == 1
        assert entry_a["changes"]["other_session_excluded"] == 1
        entry_b = _finish(repo, SID2)
        files_b = _by_path(entry_b)
        assert files_b["b.py"]["attribution"] == "agent_reported"
        # Session A has ended; its file is still not B's work.
        assert files_b["a.py"]["attribution"] in ("other_session", "git_observed")
        assert files_b["a.py"]["attribution"] != "agent_reported"
        assert entry_b["files_created"] == 1 if files_b["a.py"]["attribution"] == "other_session" else 2

    def test_second_session_baseline_excludes_first_sessions_uncommitted_work(self, repo):
        _start(repo, SID)
        _agent_write(repo, "a.py", "a\n", sid=SID)
        _finish(repo, SID)
        # A new session starts while a.py is still uncommitted: it is baseline
        # for the second session, never counted as the second session's change.
        _start(repo, SID2)
        _agent_write(repo, "b.py", "b\n", sid=SID2)
        entry_b = _finish(repo, SID2)
        files_b = _by_path(entry_b)
        assert files_b["a.py"]["attribution"] == "pre_existing"
        assert files_b["b.py"]["attribution"] == "agent_reported"
        assert entry_b["files_created"] == 1


class TestCompatibility:
    def test_old_record_without_attribution_still_renders(self):
        entry = {
            "schema_version": "1.1",
            "timestamp": "2026-09-01T10:00:00Z",
            "task": "old record",
            "executor": "claude_code_hooks",
            "files_created": 1, "files_updated": 0, "files_deleted": 0,
            "files_detail": [{"path": "calc.py", "change_type": "create", "summary": "inferred from git diff"}],
            "capture": {"session_id": SID, "task_status": "turn_completed"},
        }
        receipt = build_shard_receipt(entry)
        assert receipt.files_changed == 1
        rendered = render_compact_shard_receipt(receipt)
        assert "1 file" in rendered
        assert "Excluded" not in rendered
