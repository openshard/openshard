"""Lines added / removed on a hook-captured Receipt.

Counted with ``git diff --numstat`` between the session's base commit (HEAD
when it started) and its own result commit, over the files counted as this
session's work. Unknown stays unknown: no base, no commit, a binary file or
uncommitted work on those files records null with a reason, never 0.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from openshard.adapters.claude_git_outcome import (
    LINES_BINARY_FILE,
    LINES_NO_BASE_COMMIT,
    LINES_NO_COMMIT,
    LINES_UNCOMMITTED,
    _session_line_counts,
)
from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.history.receipt_render import lines_changed_display
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt
from openshard.history.views import receipt_to_dict
from openshard.sync.envelope import receipt_payload
from tests.capture_fixtures import SID


def _run(repo: Path, event: str, **fields) -> None:
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, **fields}
    assert handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)}).action != "error"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=repo, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, stdout=subprocess.PIPE,
                          text=True).stdout.strip()


def _start(repo: Path) -> None:
    _run(repo, "SessionStart", source="startup")
    _run(repo, "UserPromptSubmit", prompt="Do the work")


def _write(repo: Path, rel: str, data: str | bytes) -> None:
    path = repo / rel
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    _run(repo, "PostToolUse", tool_name="Write", tool_input={"file_path": str(path), "content": "x"})


def _commit(repo: Path) -> None:
    _git(repo, "add", "-A", "--", ":!.openshard", ":!.claude")
    _git(repo, "commit", "-q", "-m", "work")
    _run(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "git commit -m work"})


def _finish(repo: Path) -> dict:
    _run(repo, "Stop")
    _run(repo, "SessionEnd", reason="prompt_input_exit")
    path = repo / ".openshard" / "runs.jsonl"
    entries = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    matches = [e for e in entries if e.get("capture", {}).get("session_id") == SID]
    assert len(matches) == 1
    return matches[0]


class TestHookCapture:
    def test_committed_session_records_added_and_removed_lines(self, repo):
        (repo / "calc.py").write_text("a\nb\nc\n", encoding="utf-8")
        _git(repo, "add", "calc.py")
        _git(repo, "commit", "-q", "-m", "before")
        _start(repo)
        _write(repo, "calc.py", "a\nB\nc\nd\n")  # 1 changed line, 1 new line: +2 / -1
        _write(repo, "new.py", "x = 1\ny = 2\n")  # +2
        _commit(repo)
        entry = _finish(repo)
        assert entry["lines_added"] == 4
        assert entry["lines_removed"] == 1
        assert "lines_not_recorded_reason" not in entry["capture"]
        receipt = build_shard_receipt(entry)
        assert (receipt.diff_added, receipt.diff_removed) == (4, 1)
        assert lines_changed_display(receipt) in ("+4 / −1", "+4 / -1")
        assert "Lines changed" in render_compact_shard_receipt(receipt)
        payload = receipt_payload(entry, 0)
        assert (payload["diff_added"], payload["diff_removed"]) == (4, 1)
        assert "lines_not_recorded_reason" not in payload

    def test_binary_file_makes_lines_unknown_not_zero(self, repo):
        _start(repo)
        _write(repo, "notes.py", "one\n")
        _write(repo, "blob.bin", b"\x00\x01\x02\x00")
        _commit(repo)
        entry = _finish(repo)
        assert entry["lines_added"] is None and entry["lines_removed"] is None
        assert entry["capture"]["lines_not_recorded_reason"] == LINES_BINARY_FILE
        receipt = build_shard_receipt(entry)
        assert receipt.diff_added is None and receipt.diff_removed is None
        assert receipt.lines_not_recorded_reason == LINES_BINARY_FILE
        assert lines_changed_display(receipt) == "not captured yet"
        assert receipt_to_dict(receipt, extended=True)["lines_not_recorded_reason"] == LINES_BINARY_FILE
        payload = receipt_payload(entry, 0)
        assert payload["diff_added"] is None and payload["diff_removed"] is None
        # Not in the Platform contract yet: never sent.
        assert "lines_not_recorded_reason" not in payload

    def test_no_commit_records_unknown(self, repo):
        _start(repo)
        _write(repo, "calc.py", "x = 1\n")
        entry = _finish(repo)  # changed, never committed
        assert entry["lines_added"] is None and entry["lines_removed"] is None
        assert entry["capture"]["lines_not_recorded_reason"] == LINES_NO_COMMIT
        assert "not captured yet" in render_compact_shard_receipt(build_shard_receipt(entry))

    def test_no_base_commit_records_unknown(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        _git(repo, "init", "-q")  # no commit: HEAD at session start is unknown
        _start(repo)
        _write(repo, "calc.py", "x = 1\n")
        _commit(repo)
        entry = _finish(repo)
        assert entry["lines_added"] is None and entry["lines_removed"] is None
        assert entry["capture"]["lines_not_recorded_reason"] == LINES_NO_BASE_COMMIT

    def test_uncommitted_edit_after_commit_is_unknown_not_an_undercount(self, repo):
        _start(repo)
        _write(repo, "calc.py", "x = 1\n")
        _commit(repo)
        _write(repo, "calc.py", "x = 1\ny = 2\n")  # more work, not committed
        entry = _finish(repo)
        assert entry["lines_added"] is None
        assert entry["capture"]["lines_not_recorded_reason"] == LINES_UNCOMMITTED


class TestLineCounts:
    def test_only_attributed_paths_are_counted(self, repo):
        base = _head(repo)
        (repo / "mine.py").write_text("1\n2\n", encoding="utf-8")
        (repo / "theirs.py").write_text("1\n2\n3\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "c")
        assert _session_line_counts(repo, base, _head(repo), ["mine.py"]) == (2, 0, None)

    def test_missing_base_or_commit_is_unknown(self, repo):
        head = _head(repo)
        assert _session_line_counts(repo, None, head, ["README.md"]) == (None, None, LINES_NO_BASE_COMMIT)
        assert _session_line_counts(repo, head, None, ["README.md"]) == (None, None, LINES_NO_COMMIT)

    def test_old_record_without_counts_stays_unknown(self):
        receipt = build_shard_receipt({"task": "t", "timestamp": "2026-01-01T00:00:00Z", "executor": "claude_code"})
        assert receipt.diff_added is None and receipt.lines_not_recorded_reason is None
        assert lines_changed_display(receipt) == "not captured yet"
