"""Changed files on a hook-captured Receipt when the agent switches branch mid-session.

Only work on the branch the session ended on, since the session moved onto
it, counts. Files that differ only between the start branch and the final
branch are not this session's changes. When git cannot place the switch,
the git-observed file set is unknown, never a guess.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from openshard.adapters.claude_git_outcome import FILES_BRANCH_SWITCH_UNKNOWN
from openshard.adapters.claude_hooks import handle_claude_hook
from tests.capture_fixtures import SID


def _run(repo: Path, event: str, **fields) -> None:
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, **fields}
    assert handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)}).action != "error"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()


def _bash(repo: Path, command: str, *git_args: str) -> None:
    _git(repo, *git_args)
    _run(repo, "PostToolUse", tool_name="Bash", tool_input={"command": command})


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.write_text(text, encoding="utf-8")
    _run(repo, "PostToolUse", tool_name="Write", tool_input={"file_path": str(path), "content": "x"})


def _commit(repo: Path) -> None:
    _git(repo, "add", "-A", "--", ":!.openshard", ":!.claude")
    _bash(repo, "git commit -m work", "commit", "-q", "-m", "work")


def _finish(repo: Path) -> dict:
    _run(repo, "Stop")
    _run(repo, "SessionEnd", reason="prompt_input_exit")
    path = repo / ".openshard" / "runs.jsonl"
    entries = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    matches = [e for e in entries if e.get("capture", {}).get("session_id") == SID]
    assert len(matches) == 1
    return matches[0]


def _two_branches(repo: Path) -> str:
    """Start branch ``work-a`` carries 5 files ``main`` lacks; returns ``main``'s tip."""
    _git(repo, "branch", "-M", "work-a")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "shared.py").write_text("v = 1\n", encoding="utf-8")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-q", "-m", "shared")
    _git(repo, "branch", "main")  # main: README + shared.py, no only_a files
    for i in range(5):
        (repo / f"only_a_{i}.py").write_text(f"a = {i}\n", encoding="utf-8")
    (repo / "shared.py").write_text("v = 'a'\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a work")
    assert base
    return _git(repo, "rev-parse", "main")


def _paths(entry: dict) -> list[str]:
    return sorted(f["path"] for f in entry["files_detail"])


class TestBranchSwitch:
    def test_only_files_changed_on_the_final_branch_count(self, repo):
        main_tip = _two_branches(repo)
        _run(repo, "SessionStart", source="startup")
        _run(repo, "UserPromptSubmit", prompt="Do the work")
        _bash(repo, "git checkout main", "checkout", "-q", "main")
        _bash(repo, "git checkout -b demo/receipt-check", "checkout", "-q", "-b", "demo/receipt-check")
        _write(repo, "shared.py", "v = 2\nw = 3\n")
        _write(repo, "new.py", "x = 1\n")
        _commit(repo)
        entry = _finish(repo)
        assert _paths(entry) == ["new.py", "shared.py"]
        assert entry["files_deleted"] == 0
        assert (entry["files_created"], entry["files_updated"]) == (1, 1)
        assert entry["capture"]["files_base_commit"] == main_tip
        assert "git_files_unknown_reason" not in entry["capture"]
        # Lines count from the same base: shared.py "v = 1" -> 2 lines, new.py 1 line.
        assert (entry["lines_added"], entry["lines_removed"]) == (3, 1)

    def test_uncommitted_work_after_switch_counts_only_that_work(self, repo):
        _two_branches(repo)
        _run(repo, "SessionStart", source="startup")
        _run(repo, "UserPromptSubmit", prompt="Do the work")
        _bash(repo, "git checkout main", "checkout", "-q", "main")
        _write(repo, "note.txt", "hi\n")
        entry = _finish(repo)
        assert _paths(entry) == ["note.txt"]
        assert entry["files_deleted"] == 0

    def test_round_trip_back_to_the_start_branch_keeps_the_start_base(self, repo):
        _two_branches(repo)
        _run(repo, "SessionStart", source="startup")
        _run(repo, "UserPromptSubmit", prompt="Do the work")
        _write(repo, "only_a_0.py", "a = 'edited'\n")
        _commit(repo)
        _bash(repo, "git checkout main", "checkout", "-q", "main")
        _bash(repo, "git checkout work-a", "checkout", "-q", "work-a")
        _write(repo, "only_a_1.py", "a = 'edited too'\n")
        entry = _finish(repo)
        assert _paths(entry) == ["only_a_0.py", "only_a_1.py"]
        assert "files_base_commit" not in entry["capture"]

    def test_switch_git_cannot_place_leaves_git_files_unknown(self, repo):
        _two_branches(repo)
        _git(repo, "config", "core.logAllRefUpdates", "false")
        for log in (repo / ".git" / "logs").rglob("*"):
            if log.is_file():
                log.unlink()  # no reflog: where the final branch's work began is unknown
        _run(repo, "SessionStart", source="startup")
        _run(repo, "UserPromptSubmit", prompt="Do the work")
        _bash(repo, "git checkout main", "checkout", "-q", "main")
        _write(repo, "new.py", "x = 1\n")
        entry = _finish(repo)
        assert entry["capture"]["git_files_unknown_reason"] == FILES_BRANCH_SWITCH_UNKNOWN
        # Only what the agent itself reported; nothing from the branch diff.
        assert _paths(entry) == ["new.py"]
        assert all(f["attribution"] == "agent_reported" for f in entry["files_detail"])
        assert entry["files_deleted"] == 0
