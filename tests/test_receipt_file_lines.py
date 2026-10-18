"""Lines added / removed for each file on a hook-captured Receipt.

The same ``git diff --numstat`` that gives the session's totals stamps each of
the session's ``files_detail`` entries with its own counts, or ``binary`` when
numstat prints ``-``. Paths and counts only. Unknown stays unknown: when the
totals could not be counted honestly, no file carries counts either.
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

from openshard.adapters.claude_git_outcome import (
    LINES_BINARY_FILE,
    LINES_NO_BASE_COMMIT,
    LINES_UNCOMMITTED,
    _session_numstat,
)
from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.history.receipt_render import file_lines_display, render_full_shard_receipt
from openshard.history.shard_contract import build_shard_receipt
from openshard.sync.envelope import receipt_payload
from tests.capture_fixtures import SID

_PER_FILE_KEYS = ("lines_added", "lines_removed", "binary")
_PLATFORM_SCHEMA = Path(__file__).parent / "fixtures" / "platform" / "receipt-sync-envelope.v1.json"


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
    _bash(repo, "git commit -m work", "commit", "-q", "-m", "work")


def _finish(repo: Path) -> dict:
    _run(repo, "Stop")
    _run(repo, "SessionEnd", reason="prompt_input_exit")
    path = repo / ".openshard" / "runs.jsonl"
    entries = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    matches = [e for e in entries if e.get("capture", {}).get("session_id") == SID]
    assert len(matches) == 1
    return matches[0]


def _counts(entry: dict) -> dict[str, dict]:
    return {
        f["path"]: {k: f[k] for k in _PER_FILE_KEYS if k in f}
        for f in entry["files_detail"]
    }


class TestPerFileCounts:
    def test_each_file_carries_its_numstat_counts(self, repo):
        (repo / "calc.py").write_text("a\nb\nc\n", encoding="utf-8")
        _git(repo, "add", "calc.py")
        _git(repo, "commit", "-q", "-m", "before")
        _start(repo)
        _write(repo, "calc.py", "a\nB\nc\nd\n")  # +2 / -1
        _write(repo, "new.py", "x = 1\ny = 2\n")  # +2 / -0
        _commit(repo)
        entry = _finish(repo)
        assert _counts(entry) == {
            "calc.py": {"lines_added": 2, "lines_removed": 1},
            "new.py": {"lines_added": 2, "lines_removed": 0},
        }
        # The per-file counts add up to the session totals from the same numstat.
        assert (entry["lines_added"], entry["lines_removed"]) == (4, 1)

    def test_rename_counts_old_and_new_path_as_numstat_does(self, repo):
        (repo / "old.py").write_text("1\n2\n3\n", encoding="utf-8")
        _git(repo, "add", "old.py")
        _git(repo, "commit", "-q", "-m", "before")
        base = _git(repo, "rev-parse", "HEAD")
        _start(repo)
        _bash(repo, "git mv old.py renamed.py", "mv", "old.py", "renamed.py")
        _write(repo, "renamed.py", "1\n2\n3\n4\n")
        _commit(repo)
        entry = _finish(repo)
        numstat = _git(repo, "diff", "--numstat", "--no-renames", base, "HEAD", "--", "old.py", "renamed.py")
        expected = {
            path: {"lines_added": int(a), "lines_removed": int(r)}
            for a, r, path in (row.split("\t") for row in numstat.splitlines())
        }
        assert expected == {"old.py": {"lines_added": 0, "lines_removed": 3},
                            "renamed.py": {"lines_added": 4, "lines_removed": 0}}
        # git's rename detection is off for the file list too: no "old.py<TAB>renamed.py" path.
        assert _counts(entry) == expected
        assert (entry["lines_added"], entry["lines_removed"]) == (4, 3)

    def test_binary_file_is_marked_and_text_files_keep_counts(self, repo):
        _start(repo)
        _write(repo, "notes.py", "one\n")
        _write(repo, "blob.bin", b"\x00\x01\x02\x00")
        _commit(repo)
        entry = _finish(repo)
        assert _counts(entry) == {"notes.py": {"lines_added": 1, "lines_removed": 0}, "blob.bin": {"binary": True}}
        # The session total stays unknown, with the existing reason.
        assert entry["lines_added"] is None and entry["lines_removed"] is None
        assert entry["capture"]["lines_not_recorded_reason"] == LINES_BINARY_FILE

    def test_files_outside_the_attributed_set_carry_no_counts(self, repo):
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "mine.py").write_text("1\n2\n", encoding="utf-8")
        (repo / "theirs.py").write_text("1\n2\n3\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "c")
        added, removed, per_file, reason = _session_numstat(repo, base, _git(repo, "rev-parse", "HEAD"), ["mine.py"])
        assert (added, removed, reason) == (2, 0, None)
        assert per_file == {"mine.py": {"lines_added": 2, "lines_removed": 0}}

    def test_pre_existing_change_is_excluded_and_uncounted(self, repo):
        (repo / "theirs.py").write_text("dirty before the session\n", encoding="utf-8")
        _start(repo)
        _write(repo, "mine.py", "x = 1\n")
        _git(repo, "add", "mine.py")
        _bash(repo, "git commit -m work", "commit", "-q", "-m", "work")
        entry = _finish(repo)
        by_path = {f["path"]: f for f in entry["files_detail"]}
        assert by_path["mine.py"]["lines_added"] == 1
        assert by_path["theirs.py"]["attribution"] == "pre_existing"
        assert not any(k in by_path["theirs.py"] for k in _PER_FILE_KEYS)

    def test_branch_switch_counts_from_the_files_base(self, repo):
        _git(repo, "branch", "-M", "work-a")
        (repo / "shared.py").write_text("v = 1\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-q", "-m", "shared")
        _git(repo, "branch", "main")
        (repo / "shared.py").write_text("v = 'a'\nmore = 1\nmore = 2\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-am", "a work")
        main_tip = _git(repo, "rev-parse", "main")
        _start(repo)
        _bash(repo, "git checkout main", "checkout", "-q", "main")
        _bash(repo, "git checkout -b demo", "checkout", "-q", "-b", "demo")
        _write(repo, "shared.py", "v = 2\nw = 3\n")
        _commit(repo)
        entry = _finish(repo)
        assert entry["capture"]["files_base_commit"] == main_tip
        # From main's "v = 1", not from the start branch's three lines.
        assert _counts(entry) == {"shared.py": {"lines_added": 2, "lines_removed": 1}}

    def test_missing_base_records_no_counts(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        _git(repo, "init", "-q")
        _start(repo)
        _write(repo, "calc.py", "x = 1\n")
        _commit(repo)
        entry = _finish(repo)
        assert entry["capture"]["lines_not_recorded_reason"] == LINES_NO_BASE_COMMIT
        assert all(not c for c in _counts(entry).values())
        head = _git(repo, "rev-parse", "HEAD")
        assert _session_numstat(repo, None, head, ["calc.py"]) == (None, None, None, LINES_NO_BASE_COMMIT)

    def test_uncommitted_work_records_no_counts(self, repo):
        _start(repo)
        _write(repo, "calc.py", "x = 1\n")
        _commit(repo)
        _write(repo, "calc.py", "x = 1\ny = 2\n")
        entry = _finish(repo)
        assert entry["capture"]["lines_not_recorded_reason"] == LINES_UNCOMMITTED
        assert all(not c for c in _counts(entry).values())


class TestRenderAndSync:
    def _entry(self, repo: Path) -> dict:
        _start(repo)
        _write(repo, "calc.py", "a\nb\n")
        _write(repo, "blob.bin", b"\x00\x01")
        _commit(repo)
        return _finish(repo)

    def test_full_receipt_shows_per_file_counts(self, repo):
        text = render_full_shard_receipt(build_shard_receipt(self._entry(repo)))
        changes = text.split("CHANGES", 1)[1]
        calc = next(ln for ln in changes.splitlines() if " calc.py" in ln)
        assert calc.endswith("+2 / -0")
        blob = next(ln for ln in changes.splitlines() if " blob.bin" in ln)
        assert blob.endswith("binary")

    def test_unknown_counts_render_nothing_never_zero(self):
        assert file_lines_display({"path": "a.py"}) == ""
        assert file_lines_display({"path": "a.py", "lines_added": 3}) == ""
        assert file_lines_display({"path": "a.py", "lines_added": True, "lines_removed": 0}) == ""
        assert file_lines_display({"path": "a.py", "lines_added": 0, "lines_removed": 0}) == "  +0 / -0"

    def test_sync_payload_carries_per_file_counts(self, repo):
        payload = receipt_payload(self._entry(repo), 0)
        files = {f["path"]: f for f in payload["files"]}
        assert files["calc.py"]["diff_added"] == 2
        assert files["calc.py"]["diff_removed"] == 0
        assert files["calc.py"]["binary"] is None
        assert files["blob.bin"]["binary"] is True
        assert files["blob.bin"]["diff_added"] is None and files["blob.bin"]["diff_removed"] is None
        # The Platform's file entry is strict: every key Core sends must be one it accepts.
        schema = json.loads(_PLATFORM_SCHEMA.read_text(encoding="utf-8"))
        item = schema["properties"]["receipt"]["properties"]["files"]["items"]
        assert item.get("additionalProperties") is False
        for f in payload["files"]:
            assert set(f) <= set(item["properties"])

    def test_unknown_per_file_counts_sync_as_null_never_zero(self, repo):
        entry = self._entry(repo)
        stripped = copy.deepcopy(entry)
        stripped["files_detail"] = [
            {k: v for k, v in f.items() if k not in _PER_FILE_KEYS} for f in stripped["files_detail"]
        ]
        for f in receipt_payload(stripped, 0)["files"]:
            assert f["diff_added"] is None and f["diff_removed"] is None and f["binary"] is None
