"""Merge check on sealed Receipts (adapters/merge_check.py, history/merge_check.py).

Real git repositories in tmp dirs. The check compares the Receipt's HEAD with
the default branch the checkout already knows, records facts only (refs, shas,
authors, subjects, paths, line numbers) and never file contents.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import jsonschema
import pytest

from openshard.adapters import claude_hooks as ch
from openshard.adapters import claude_turn_receipts as turns
from openshard.adapters import merge_check as mc
from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.history import receipt_signing as rs
from openshard.history.merge_check import normalize
from openshard.history.store import load_history
from openshard.sync import envelope

SID = "0f1e2d3c-4b5a-4697-8877-665544332211"
SCHEMA = json.loads(
    (Path(__file__).parent / "fixtures" / "platform" / "receipt-sync-envelope.v1.json").read_text(encoding="utf-8")
)
# Distinctive file contents: none of these may ever appear in a Receipt or the sync payload.
HEAD_TEXT = "HEADSIDECONTENT_7f3a"
BASE_TEXT = "BASESIDECONTENT_91c2"
COMMON_TEXT = "COMMONCONTENT_55d0"


def _git(repo: Path, *args: str, author: str = "Head Hana") -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", f"user.name={author}", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return proc.stdout.decode("utf-8").strip()


def _lines(n: int) -> list[str]:
    return [f"{COMMON_TEXT} {i}" for i in range(1, n + 1)]


def _write(repo: Path, rel: str, lines: list[str]) -> None:
    path = repo.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def _commit(repo: Path, message: str, author: str = "Head Hana") -> str:
    _git(repo, "add", "-A", author=author)
    _git(repo, "commit", "-q", "-m", message, author=author)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "widget"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / ".git" / "info" / "exclude").write_text(".openshard/\n", encoding="utf-8")
    _write(root, "src/app.py", _lines(30))
    _write(root, "README.md", ["hello"])
    _commit(root, "init", author="Init Ian")
    _git(root, "checkout", "-q", "-b", "feature")
    return root


def _on_main(repo: Path, fn) -> str:
    _git(repo, "checkout", "-q", "main")
    try:
        return fn()
    finally:
        _git(repo, "checkout", "-q", "feature")


def _both_sides(repo: Path, rel: str = "src/app.py") -> tuple[str, str]:
    """HEAD edits lines 10 and 20; the base inserts 3 lines at the top, edits 10 and 20 too."""
    head_lines = _lines(30)
    head_lines[9], head_lines[19] = f"{HEAD_TEXT} ten", f"{HEAD_TEXT} twenty"
    _write(repo, rel, head_lines)
    head = _commit(repo, "Changed ten and twenty on the feature")

    def base_side() -> str:
        base_lines = _lines(30)
        base_lines[9], base_lines[19] = f"{BASE_TEXT} ten", f"{BASE_TEXT} twenty"
        _write(repo, rel, ["top 1", "top 2", "top 3", *base_lines])
        return _commit(repo, "Changed ten and twenty on main\n\nBody text stays out.", author="Base Bea")

    return head, _on_main(repo, base_side)


class TestCompute:
    def test_clean_merge_records_an_empty_conflict_list(self, repo):
        _write(repo, "README.md", ["hello", "feature"])
        head = _commit(repo, "readme")

        def base() -> str:
            _write(repo, "docs/new.md", ["new"])
            return _commit(repo, "Added a doc", author="Base Bea")

        base_sha = _on_main(repo, base)
        block = mc.check(repo)
        assert block is not None
        assert (block["base_ref"], block["base_sha"], block["head_sha"]) == ("main", base_sha, head)
        assert block["conflicts"] == []
        assert block["behind_by"] == 1
        assert block["base_commits"] == [{"sha": base_sha, "author": "Base Bea", "subject": "Added a doc",
                                          "committed_at": block["base_commits"][0]["committed_at"]}]
        assert block["base_commits"][0]["committed_at"].endswith("Z")
        assert block["checked_at"].endswith("Z")

    def test_base_already_in_head_is_clean_without_merging(self, repo, monkeypatch):
        _write(repo, "README.md", ["feature"])
        _commit(repo, "readme")
        monkeypatch.setattr(mc, "_conflicting_paths", lambda *a: pytest.fail("merge-tree not needed"))
        block = mc.check(repo)
        assert block is not None
        assert (block["behind_by"], block["base_commits"], block["conflicts"]) == (0, [], [])

    def test_both_sides_edit_records_path_head_lines_and_base_author(self, repo):
        head, base = _both_sides(repo)
        block = mc.check(repo)
        assert block is not None
        assert block["behind_by"] == 1 and block["head_sha"] == head and block["base_sha"] == base
        assert block["base_commits"][0]["subject"] == "Changed ten and twenty on main"
        assert block["conflicts"] == [{"path": "src/app.py", "lines": [10, 20], "base_author": "Base Bea",
                                       "base_sha": base}]

    def test_base_ahead_without_conflict_lists_newest_first(self, repo):
        def base() -> list[str]:
            shas = []
            for n in range(3):
                _write(repo, f"docs/{n}.md", [str(n)])
                shas.append(_commit(repo, f"Doc {n}", author=f"Author {n}"))
            return shas

        shas = _on_main(repo, base)
        block = mc.check(repo)
        assert block is not None
        assert block["behind_by"] == 3 and block["conflicts"] == []
        assert [c["sha"] for c in block["base_commits"]] == list(reversed(shas))
        assert [c["author"] for c in block["base_commits"]] == ["Author 2", "Author 1", "Author 0"]

    def test_base_commits_are_capped_at_twenty(self, repo):
        def base() -> None:
            for n in range(23):
                _write(repo, "docs/log.md", [str(n)])
                _commit(repo, f"Doc {n}", author="Base Bea")

        _on_main(repo, base)
        block = mc.check(repo)
        assert block is not None and block["behind_by"] == 23 and len(block["base_commits"]) == 20

    def test_no_default_branch_means_no_block(self, tmp_path):
        root = tmp_path / "trunk-only"
        root.mkdir()
        _git(root, "init", "-q", "-b", "trunk")
        _write(root, "a.txt", ["a"])
        _commit(root, "init")
        assert mc.check(root) is None

    def test_on_the_base_branch_itself_means_no_block(self, repo):
        _git(repo, "checkout", "-q", "main")
        assert mc.check(repo) is None

    def test_not_a_repository_means_no_block(self, tmp_path):
        assert mc.check(tmp_path) is None

    def test_origin_head_is_the_preferred_base(self, repo, tmp_path):
        remote = tmp_path / "remote.git"
        _git(repo, "clone", "-q", "--bare", str(repo), str(remote))
        _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
        clone = tmp_path / "clone"
        _git(tmp_path, "clone", "-q", str(remote), str(clone))
        _git(clone, "checkout", "-q", "-b", "work")
        _write(clone, "README.md", ["work"])
        _commit(clone, "work")
        block = mc.check(clone)
        assert block is not None and block["base_ref"] == "origin/main"
        assert block["base_sha"] == _git(clone, "rev-parse", "origin/main")

    def test_merge_tree_failure_leaves_conflicts_unknown(self, repo, monkeypatch):
        _both_sides(repo)
        real = mc._Git.run

        def run(self, args, *, env=None):
            if args[0] == "merge-tree":
                return 129, b""  # what git older than 2.38 does with --write-tree
            return real(self, args, env=env)

        monkeypatch.setattr(mc._Git, "run", run)
        block = mc.check(repo)
        assert block is not None and block["conflicts"] is None and block["behind_by"] == 1

    def test_unrelated_history_leaves_everything_it_cannot_say_unknown(self, repo, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        _git(other, "init", "-q", "-b", "trunk")
        _write(other, "x.txt", ["x"])
        _commit(other, "unrelated")
        _git(repo, "fetch", "-q", str(other), "+trunk:main")
        block = mc.check(repo)
        assert block is not None
        assert (block["behind_by"], block["base_commits"], block["conflicts"]) == (None, None, None)

    def test_the_repository_object_store_is_left_untouched(self, repo):
        _both_sides(repo)
        objects = repo / ".git" / "objects"
        before = sorted(p.relative_to(objects).as_posix() for p in objects.rglob("*"))
        assert mc.check(repo)["conflicts"]
        assert sorted(p.relative_to(objects).as_posix() for p in objects.rglob("*")) == before

    def test_paths_are_posix_relative_with_spaces_and_unicode(self, repo):
        rel = "dir with space/ünï cödé.txt"

        def seed() -> None:
            _write(repo, rel, _lines(30))
            _commit(repo, "seed", author="Init Ian")

        _on_main(repo, seed)
        _git(repo, "merge", "-q", "--ff-only", "main")
        _both_sides(repo, rel=rel)
        block = mc.check(repo)
        assert block is not None
        assert [c["path"] for c in block["conflicts"]] == ["dir with space/ünï cödé.txt"]
        assert block["conflicts"][0]["lines"] == [10, 20]

    def test_opt_out(self, repo):
        _both_sides(repo)
        assert mc.check(repo, env={mc.MERGE_CHECK_ENV: "off"}) is None


class TestBudget:
    def test_zero_budget_runs_no_git(self, repo, monkeypatch):
        monkeypatch.setattr(mc.subprocess, "run", lambda *a, **k: pytest.fail("git ran past the budget"))
        assert mc.check(repo, budget=0) is None

    def test_every_call_is_bounded_by_what_is_left(self, repo, monkeypatch):
        _both_sides(repo)
        real = subprocess.run
        timeouts: list[float] = []

        def run(cmd, **kwargs):
            timeouts.append(kwargs["timeout"])
            if "blame" in cmd:  # a slow blame: it uses up the rest of the budget
                time.sleep(kwargs["timeout"])
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return real(cmd, **kwargs)

        monkeypatch.setattr(mc.subprocess, "run", run)
        started = time.monotonic()
        block = mc.check(repo, budget=1.0)
        assert time.monotonic() - started < 1.0 + 0.5
        assert timeouts and all(0 < t <= 1.0 for t in timeouts)
        assert block is not None
        assert block["conflicts"] == [{"path": "src/app.py", "lines": [10, 20], "base_author": None, "base_sha": None}]

    def test_default_budget_is_three_seconds(self):
        assert mc.BUDGET_SECONDS <= 3.0


class TestShape:
    def _base(self, **extra):
        return {"base_ref": "main", "base_sha": "a" * 40, "head_sha": "b" * 40, "checked_at": "2026-10-20T15:02:00Z",
                "behind_by": 1, "base_commits": [], "conflicts": [], **extra}

    def test_bounds_and_single_lines(self):
        block = normalize(self._base(
            base_commits=[{"sha": "c" * 40, "author": "A\nB", "subject": "x\r\ny" + "z" * 300, "committed_at": "bad"}] * 25,
            conflicts=[{"path": f"p{n}.py", "lines": list(range(40, 0, -1)), "base_author": "Bea", "base_sha": "d" * 40}
                       for n in range(60)],
        ))
        assert block is not None
        assert len(block["base_commits"]) == 20 and len(block["conflicts"]) == 50
        commit = block["base_commits"][0]
        assert commit["author"] == "A B" and "\n" not in commit["subject"] and len(commit["subject"]) <= 200
        assert commit["committed_at"] is None
        assert block["conflicts"][0]["lines"] == list(range(1, 21))

    @pytest.mark.parametrize("path", ["/etc/passwd", "C:\\x.py", "../up.py", "a\nb", ""])
    def test_unsafe_paths_are_dropped(self, path):
        block = normalize(self._base(conflicts=[{"path": path, "lines": [1], "base_author": None, "base_sha": None}]))
        assert block is not None and block["conflicts"] is None  # never "clean" when a conflict was dropped

    def test_a_dropped_path_beside_a_safe_one_keeps_the_safe_one(self):
        conflicts = [{"path": p, "lines": [1], "base_author": None, "base_sha": None} for p in ("/abs.py", "ok.py")]
        block = normalize(self._base(conflicts=conflicts))
        assert block is not None and [c["path"] for c in block["conflicts"]] == ["ok.py"]

    def test_unknown_stays_unknown(self):
        block = normalize(self._base(behind_by=None, base_commits=None, conflicts=None))
        assert block is not None and (block["behind_by"], block["base_commits"], block["conflicts"]) == (None, None, None)

    @pytest.mark.parametrize("field", ["base_sha", "head_sha", "checked_at", "base_ref"])
    def test_malformed_identity_drops_the_block(self, field):
        assert normalize(self._base(**{field: "not valid .."})) is None

    def test_secret_shaped_subject_is_dropped(self):
        block = normalize(self._base(base_commits=[{"sha": "c" * 40, "author": "A", "subject": "token=abc123",
                                                    "committed_at": None}]))
        assert block is not None and block["base_commits"][0]["subject"] is None


# ---------------------------------------------------------------------------
# End to end: the real hook path seals a turn Receipt with the check, and the
# sync payload carries it, without any file content.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _claude_config_dir(tmp_path: Path, monkeypatch) -> Path:
    config_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    return config_dir


def _hook(repo: Path, event: str, **fields):
    transcript = Path(str(repo) + "-transcript.jsonl")
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, "transcript_path": str(transcript), **fields}
    return handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _turn_with_conflict(repo: Path) -> None:
    _hook(repo, "UserPromptSubmit", prompt="edit app")
    _both_sides(repo)
    _hook(repo, "PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / "src" / "app.py")},
          tool_response={"filePath": str(repo / "src" / "app.py")})
    _hook(repo, "Stop")


class TestReceipt:
    @pytest.fixture(autouse=True)
    def _turns_on(self, monkeypatch):
        monkeypatch.setenv(turns.TURN_RECEIPTS_ENV, "on")
        monkeypatch.delenv(mc.MERGE_CHECK_ENV, raising=False)

    def test_sealed_turn_carries_the_check_signed_and_synced_without_contents(self, repo, monkeypatch):
        monkeypatch.setattr(ch, "_PR_LOOKUP_ENABLED", True)  # as in the capture service worker
        monkeypatch.setattr(ch, "_gh_pull_request", lambda *a: None)
        _turn_with_conflict(repo)
        history = repo / ".openshard"
        [entry] = load_history(history / "runs.jsonl", coerce=False)
        block = entry["merge_check"]
        assert block["conflicts"][0]["path"] == "src/app.py" and block["conflicts"][0]["lines"] == [10, 20]
        assert block["conflicts"][0]["base_author"] == "Base Bea"
        assert rs.signature_status(history / "runs.jsonl", entry, 0).verdict == rs.VERDICT_VERIFIED

        sent = envelope.build_envelope(entry, 0, core_version="0.5.0")
        jsonschema.validate(sent, SCHEMA)
        assert sent["receipt"]["merge_check"] == block
        stored = (history / "runs.jsonl").read_text(encoding="utf-8")
        for text in (json.dumps(sent), stored):
            for content in (HEAD_TEXT, BASE_TEXT, COMMON_TEXT, "Body text stays out"):
                assert content not in text

    def test_inline_hooks_do_not_check(self, repo, monkeypatch):
        monkeypatch.setattr(ch, "_PR_LOOKUP_ENABLED", False)  # an in-process hook the agent waits on
        monkeypatch.setattr(mc, "check", lambda *a, **k: pytest.fail("merge check on the hook path"))
        _turn_with_conflict(repo)
        [entry] = load_history(repo / ".openshard" / "runs.jsonl", coerce=False)
        assert "merge_check" not in entry
        assert "merge_check" not in envelope.receipt_payload(entry, 0)

    def test_a_record_without_the_check_syncs_unchanged(self):
        entry = {"receipt_id": "rcpt_" + "0" * 32, "shard_id": "s", "task": "t", "timestamp": "2026-10-20T15:02:00Z"}
        assert "merge_check" not in envelope.receipt_payload(entry, 0)
