"""Tests for openshard.history.query (Demo v1 PR1: History Query Service)
and openshard.history.repo_identity (stable repository identity)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from openshard.history.jsonl_store import write_jsonl
from openshard.history.query import (
    SearchHit,
    UnknownRunError,
    UnknownShardError,
    get_receipt,
    get_shard,
    list_shards,
    search_history,
)
from openshard.history.repo_identity import (
    REPO_IDENTITY_FIELD,
    canonicalize_remote_url,
    capture_repo_identity,
    entry_matches_repo,
)
from openshard.history.shard import Shard
from openshard.history.shard_contract import (
    ShardReceipt,
    _make_shard_id,
    build_shard_receipt,
    render_compact_shard_receipt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(task: str, ts: str, **kwargs) -> dict:
    """A minimal native pipeline entry, the same shape _log_run persists."""
    base: dict = {
        "schema_version": "1.2",
        "timestamp": ts,
        "run_id": ts,
        "task": task,
        "workflow": "native",
        "executor": "native",
        "retry_triggered": False,
        "verification_attempted": True,
        "verification_passed": True,
        "summary": "done",
        "repo_name": "alpha",
    }
    base.update(kwargs)
    return base


def _write(repo_path: Path, entries: list[dict]) -> None:
    write_jsonl(repo_path / ".openshard" / "runs.jsonl", entries)


T1 = "2026-08-01T10:00:00Z"
T2 = "2026-08-02T10:00:00Z"
T3 = "2026-08-03T10:00:00Z"
T4 = "2026-08-04T10:00:00Z"


@pytest.fixture
def history(tmp_path: Path) -> Path:
    """Three single-attempt Shards across two repos, written in time order."""
    _write(tmp_path, [
        _entry("add JWT auth", T1, shard_id="shard-a", repo_name="alpha"),
        _entry("fix flaky test", T2, shard_id="shard-b", repo_name="beta",
               **{REPO_IDENTITY_FIELD: "github.com/acme/beta"}),
        _entry("refactor db layer", T3, shard_id="shard-c", repo_name="alpha",
               **{REPO_IDENTITY_FIELD: "github.com/acme/alpha"}),
    ])
    return tmp_path


@pytest.fixture
def multi_attempt(tmp_path: Path) -> Path:
    """One Shard with two attempts plus one unrelated single-attempt Shard."""
    _write(tmp_path, [
        _entry("add JWT auth", T1, shard_id="shard-multi", attempt_number=1,
               verification_passed=False),
        _entry("unrelated task", T2, shard_id="shard-solo", attempt_number=1),
        _entry("add JWT auth (retry)", T3, shard_id="shard-multi", attempt_number=2,
               retry_triggered=True, verification_passed=True),
    ])
    return tmp_path


# ---------------------------------------------------------------------------
# list_shards
# ---------------------------------------------------------------------------


class TestListShards:
    def test_empty_history_returns_empty_list(self, tmp_path: Path):
        assert list_shards(repo_path=tmp_path) == []

    def test_missing_openshard_dir_returns_empty_list(self, tmp_path: Path):
        assert not (tmp_path / ".openshard").exists()
        assert list_shards(repo_path=tmp_path) == []

    def test_returns_canonical_shard_objects(self, history: Path):
        shards = list_shards(repo_path=history)
        assert shards and all(isinstance(s, Shard) for s in shards)

    def test_newest_first(self, history: Path):
        ids = [s.shard_id for s in list_shards(repo_path=history)]
        assert ids == ["shard-c", "shard-b", "shard-a"]

    def test_limit(self, history: Path):
        ids = [s.shard_id for s in list_shards(repo_path=history, limit=2)]
        assert ids == ["shard-c", "shard-b"]

    def test_non_positive_limit_returns_empty(self, history: Path):
        assert list_shards(repo_path=history, limit=0) == []

    def test_repo_filter_by_legacy_folder_name(self, history: Path):
        ids = [s.shard_id for s in list_shards(repo_path=history, repo="alpha")]
        assert ids == ["shard-c", "shard-a"]

    def test_repo_filter_by_canonical_identity(self, history: Path):
        ids = [s.shard_id for s in list_shards(repo_path=history, repo="github.com/acme/beta")]
        assert ids == ["shard-b"]

    def test_repo_filter_by_remote_url_form(self, history: Path):
        ids = [s.shard_id for s in list_shards(repo_path=history, repo="git@github.com:acme/beta.git")]
        assert ids == ["shard-b"]

    def test_repo_filter_no_match(self, history: Path):
        assert list_shards(repo_path=history, repo="nope") == []

    def test_multi_attempt_shard_listed_once(self, multi_attempt: Path):
        shards = list_shards(repo_path=multi_attempt)
        ids = [s.shard_id for s in shards]
        assert ids.count("shard-multi") == 1
        assert len(ids) == 2

    def test_multi_attempt_shard_reflects_latest_attempt(self, multi_attempt: Path):
        shards = {s.shard_id: s for s in list_shards(repo_path=multi_attempt)}
        latest = shards["shard-multi"]
        assert latest.task_full == "add JWT auth (retry)"
        assert latest.created_at == T3

    def test_multi_attempt_shard_ordered_by_latest_attempt(self, multi_attempt: Path):
        ids = [s.shard_id for s in list_shards(repo_path=multi_attempt)]
        # shard-multi's latest attempt (T3) is newer than shard-solo (T2)
        assert ids == ["shard-multi", "shard-solo"]

    def test_legacy_entries_without_shard_id_are_distinct(self, tmp_path: Path):
        _write(tmp_path, [
            {"task": "old one", "timestamp": T1},
            {"task": "old two", "timestamp": T2},
        ])
        shards = list_shards(repo_path=tmp_path)
        assert [s.shard_id for s in shards] == [_make_shard_id(T2, 1), _make_shard_id(T1, 0)]


# ---------------------------------------------------------------------------
# get_shard
# ---------------------------------------------------------------------------


class TestGetShard:
    def test_found(self, history: Path):
        shard = get_shard("shard-b", repo_path=history)
        assert isinstance(shard, Shard)
        assert shard.shard_id == "shard-b"
        assert shard.task_short == "fix flaky test"
        assert shard.agent == "OpenShard Native"

    def test_multi_attempt_returns_latest_attempt_state(self, multi_attempt: Path):
        shard = get_shard("shard-multi", repo_path=multi_attempt)
        assert shard.shard_id == "shard-multi"
        assert shard.task_full == "add JWT auth (retry)"
        assert shard.created_at == T3

    def test_highest_attempt_number_wins_over_file_order(self, tmp_path: Path):
        _write(tmp_path, [
            _entry("second", T2, shard_id="s", attempt_number=2),
            _entry("first", T1, shard_id="s", attempt_number=1),
        ])
        assert get_shard("s", repo_path=tmp_path).task_full == "second"

    def test_not_found_raises_unknown_shard_error(self, history: Path):
        with pytest.raises(UnknownShardError):
            get_shard("shard-does-not-exist", repo_path=history)

    def test_not_found_on_empty_history(self, tmp_path: Path):
        with pytest.raises(UnknownShardError):
            get_shard("shard-a", repo_path=tmp_path)

    def test_legacy_entry_addressable_by_derived_id(self, tmp_path: Path):
        _write(tmp_path, [{"task": "old one", "timestamp": T1}, {"task": "old two", "timestamp": T2}])
        shard = get_shard(_make_shard_id(T2, 1), repo_path=tmp_path)
        assert shard.task_full == "old two"


# ---------------------------------------------------------------------------
# get_receipt
# ---------------------------------------------------------------------------


class TestGetReceipt:
    def test_by_shard_id(self, history: Path):
        receipt = get_receipt("shard-a", repo_path=history)
        assert isinstance(receipt, ShardReceipt)
        assert receipt.shard_id == "shard-a"
        assert receipt.run_id == T1
        assert receipt.status == "Passed"

    def test_by_run_id(self, history: Path):
        receipt = get_receipt(run_id=T2, repo_path=history)
        assert receipt.shard_id == "shard-b"
        assert receipt.run_id == T2

    def test_shard_and_run_id_together(self, multi_attempt: Path):
        receipt = get_receipt("shard-multi", run_id=T1, repo_path=multi_attempt)
        assert receipt.attempt_number == 1
        assert receipt.status == "Failed"

    def test_run_id_not_under_shard_raises(self, multi_attempt: Path):
        with pytest.raises(UnknownRunError):
            get_receipt("shard-multi", run_id=T2, repo_path=multi_attempt)

    def test_multi_attempt_defaults_to_latest_attempt(self, multi_attempt: Path):
        receipt = get_receipt("shard-multi", repo_path=multi_attempt)
        assert receipt.attempt_number == 2
        assert receipt.run_id == T3
        assert receipt.status == "Passed"
        assert receipt.shard is not None and receipt.shard.shard_id == "shard-multi"

    def test_shard_not_found(self, history: Path):
        with pytest.raises(UnknownShardError):
            get_receipt("shard-zzz", repo_path=history)

    def test_run_not_found(self, history: Path):
        with pytest.raises(UnknownRunError):
            get_receipt(run_id="2030-01-01T00:00:00Z", repo_path=history)

    def test_requires_shard_or_run(self, history: Path):
        with pytest.raises(ValueError):
            get_receipt(repo_path=history)

    def test_receipt_identical_to_cli_build(self, multi_attempt: Path):
        """Same ShardReceipt/rendering as build_shard_receipt(entries[-1], index=len-1)."""
        from openshard.history.metrics import load_runs

        entries = load_runs(multi_attempt)
        expected = build_shard_receipt(entries[-1], index=len(entries) - 1)
        got = get_receipt("shard-multi", repo_path=multi_attempt)
        assert render_compact_shard_receipt(got) == render_compact_shard_receipt(expected)
        assert got == expected

    def test_legacy_run_addressable_by_timestamp(self, tmp_path: Path):
        _write(tmp_path, [{"task": "old", "timestamp": T1}])
        receipt = get_receipt(run_id=T1, repo_path=tmp_path)
        assert receipt.shard_id == _make_shard_id(T1, 0)


# ---------------------------------------------------------------------------
# search_history
# ---------------------------------------------------------------------------


class TestSearchHistory:
    def test_task_short_match(self, history: Path):
        hits = search_history("flaky", repo_path=history)
        assert [h.shard.shard_id for h in hits] == ["shard-b"]
        assert isinstance(hits[0], SearchHit)
        assert "task_short" in hits[0].matched_fields

    def test_task_full_match_beyond_short_prefix(self, tmp_path: Path):
        long_task = "x" * 80 + " needle-at-the-end"
        _write(tmp_path, [_entry(long_task, T1, shard_id="shard-long")])
        hits = search_history("needle-at-the-end", repo_path=tmp_path)
        assert [h.shard.shard_id for h in hits] == ["shard-long"]
        assert hits[0].matched_fields == ["task_full"]

    def test_case_insensitive(self, history: Path):
        assert [h.shard.shard_id for h in search_history("JWT", repo_path=history)] == ["shard-a"]
        assert [h.shard.shard_id for h in search_history("jwt", repo_path=history)] == ["shard-a"]
        assert [h.shard.shard_id for h in search_history("Jwt", repo_path=history)] == ["shard-a"]

    def test_shard_id_match(self, history: Path):
        assert [h.shard.shard_id for h in search_history("shard-c", repo_path=history)] == ["shard-c"]

    def test_repo_filter(self, history: Path):
        # Two shards mention "layer"/"auth"... use a broad term across repos.
        _write(history, [
            _entry("add auth", T1, shard_id="s1", repo_name="alpha"),
            _entry("add auth", T2, shard_id="s2", repo_name="beta"),
        ])
        assert [h.shard.shard_id for h in search_history("auth", repo_path=history)] == ["s2", "s1"]
        assert [h.shard.shard_id for h in search_history("auth", repo_path=history, repo="beta")] == ["s2"]

    def test_unrelated_entries_excluded(self, history: Path):
        ids = [h.shard.shard_id for h in search_history("refactor", repo_path=history)]
        assert ids == ["shard-c"]

    def test_all_terms_must_match(self, history: Path):
        assert search_history("refactor flaky", repo_path=history) == []
        assert [h.shard.shard_id for h in search_history("refactor db", repo_path=history)] == ["shard-c"]

    def test_equal_scores_are_newest_first(self, tmp_path: Path):
        _write(tmp_path, [
            _entry("auth one", T1, shard_id="s1"),
            _entry("auth two", T2, shard_id="s2"),
            _entry("auth three", T3, shard_id="s3"),
        ])
        assert [h.shard.shard_id for h in search_history("auth", repo_path=tmp_path)] == ["s3", "s2", "s1"]

    def test_higher_score_ranks_first(self, tmp_path: Path):
        # "native" matches agent only for s-new, but task_short+task_full+agent for s-old.
        _write(tmp_path, [
            _entry("native bindings", T1, shard_id="s-old"),
            _entry("other work", T2, shard_id="s-new"),
        ])
        hits = search_history("native", repo_path=tmp_path)
        assert [h.shard.shard_id for h in hits] == ["s-old", "s-new"]
        assert hits[0].score > hits[1].score
        assert hits[1].matched_fields == ["agent"]

    def test_status_is_searchable(self, multi_attempt: Path):
        hits = search_history("failed", repo_path=multi_attempt)
        # shard-multi's latest attempt passed, so "failed" must not surface it.
        assert hits == []
        hits = search_history("passed", repo_path=multi_attempt)
        assert {h.shard.shard_id for h in hits} == {"shard-multi", "shard-solo"}

    def test_multi_attempt_shard_hit_once(self, multi_attempt: Path):
        hits = search_history("jwt", repo_path=multi_attempt)
        assert [h.shard.shard_id for h in hits] == ["shard-multi"]

    def test_deterministic_across_calls(self, history: Path):
        first = search_history("a", repo_path=history)
        second = search_history("a", repo_path=history)
        assert [(h.shard.shard_id, h.score) for h in first] == [(h.shard.shard_id, h.score) for h in second]

    def test_limit(self, history: Path):
        assert len(search_history("a", repo_path=history)) == 3
        assert len(search_history("a", repo_path=history, limit=1)) == 1

    def test_empty_query_returns_empty(self, history: Path):
        assert search_history("", repo_path=history) == []
        assert search_history("   ", repo_path=history) == []

    def test_no_match_returns_empty(self, history: Path):
        assert search_history("zzz-no-such-term", repo_path=history) == []

    def test_empty_history_returns_empty(self, tmp_path: Path):
        assert search_history("auth", repo_path=tmp_path) == []

    def test_summary_notes_and_blocked_fields_are_not_searched(self, tmp_path: Path):
        _write(tmp_path, [_entry(
            "plain task", T1, shard_id="s1",
            summary="secret-summary-token",
            notes=["secret-note-token"],
            agent_notes=["secret-agent-note"],
            raw_prompt="secret-prompt-token",
            transcript="secret-transcript-token",
            workspace_path="C:/Users/private/secret-folder",
        )])
        for term in ("secret-summary-token", "secret-note-token", "secret-agent-note",
                     "secret-prompt-token", "secret-transcript-token", "secret-folder"):
            assert search_history(term, repo_path=tmp_path) == [], term

    def test_hit_carries_status_and_repo(self, history: Path):
        hit = search_history("jwt", repo_path=history)[0]
        assert hit.status == "Passed"
        assert hit.repo == "alpha"


# ---------------------------------------------------------------------------
# repository identity — canonicalisation
# ---------------------------------------------------------------------------


class TestCanonicalizeRemoteUrl:
    @pytest.mark.parametrize("url", [
        "https://github.com/Acme/Widget.git",
        "https://github.com/Acme/Widget",
        "https://github.com/Acme/Widget/",
        "http://GitHub.com/Acme/Widget.git",
        "git@github.com:Acme/Widget.git",
        "git@github.com:Acme/Widget",
        "ssh://git@github.com/Acme/Widget.git",
        "ssh://git@github.com:22/Acme/Widget.git",
        "git://github.com/Acme/Widget.git",
        "git+ssh://git@github.com/Acme/Widget.git",
        "  https://github.com/Acme/Widget.git\n",
    ])
    def test_equivalent_forms_share_identity(self, url: str):
        assert canonicalize_remote_url(url) == "github.com/Acme/Widget"

    def test_credentials_are_stripped(self):
        assert canonicalize_remote_url(
            "https://user:ghp_secrettoken@github.com/Acme/Widget.git"
        ) == "github.com/Acme/Widget"
        assert canonicalize_remote_url(
            "https://x-access-token:ghp_secrettoken@github.com/Acme/Widget.git"
        ) == "github.com/Acme/Widget"
        assert canonicalize_remote_url(
            "https://oauth2:glpat-secret@gitlab.com/group/sub/proj.git"
        ) == "gitlab.com/group/sub/proj"

    def test_credential_never_appears_in_output(self):
        out = canonicalize_remote_url("https://bob:hunter2@example.com/team/repo.git")
        assert out is not None
        assert "hunter2" not in out and "bob" not in out

    def test_nested_groups_preserved(self):
        assert canonicalize_remote_url("git@gitlab.com:group/sub/proj.git") == "gitlab.com/group/sub/proj"

    @pytest.mark.parametrize("url", [
        "",
        "   ",
        None,
        42,
        "/home/me/repos/widget",
        "/home/me/repos/widget.git",
        "~/repos/widget",
        "./widget",
        "../widget",
        r"C:\Users\me\repos\widget",
        "C:/Users/me/repos/widget",
        "file:///home/me/repos/widget",
        "file://C:/Users/me/repos/widget",
        "not a url",
        "https://github.com",
        "https://github.com/",
    ])
    def test_local_paths_and_junk_yield_none(self, url):
        assert canonicalize_remote_url(url) is None

    def test_host_lowercased_path_case_kept(self):
        assert canonicalize_remote_url("https://GitHub.COM/Acme/Widget") == "github.com/Acme/Widget"


# ---------------------------------------------------------------------------
# repository identity — capture from a real git checkout
# ---------------------------------------------------------------------------

_HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not _HAS_GIT, reason="git not available")


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(path), check=True, capture_output=True, timeout=30)


@pytest.fixture
def _no_parent_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop git from discovering a repository above tmp_path.

    On some machines the pytest temp root lives inside a Git checkout, which
    would make a "non-git directory" resolve to that parent repo.
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent).replace("\\", "/"))


@needs_git
@pytest.mark.usefixtures("_no_parent_repo")
class TestCaptureRepoIdentity:
    def test_git_repo_with_origin(self, tmp_path: Path):
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "remote", "add", "origin", "git@github.com:Acme/Widget.git")
        assert capture_repo_identity(tmp_path) == "github.com/Acme/Widget"

    def test_https_origin_gives_same_identity_as_ssh(self, tmp_path: Path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        _git(a, "init", "-q")
        _git(a, "remote", "add", "origin", "https://github.com/Acme/Widget.git")
        _git(b, "init", "-q")
        _git(b, "remote", "add", "origin", "git@github.com:Acme/Widget.git")
        assert capture_repo_identity(a) == capture_repo_identity(b) == "github.com/Acme/Widget"

    def test_repo_without_origin(self, tmp_path: Path):
        _git(tmp_path, "init", "-q")
        assert capture_repo_identity(tmp_path) is None

    def test_non_git_directory(self, tmp_path: Path):
        assert capture_repo_identity(tmp_path) is None

    def test_credential_bearing_origin_is_sanitised(self, tmp_path: Path):
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "remote", "add", "origin",
             "https://user:ghp_secrettoken@github.com/Acme/Widget.git")
        identity = capture_repo_identity(tmp_path)
        assert identity == "github.com/Acme/Widget"
        assert "ghp_secrettoken" not in (identity or "")

    def test_local_path_origin_is_not_recorded(self, tmp_path: Path):
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        _git(upstream, "init", "-q")
        clone = tmp_path / "clone"
        clone.mkdir()
        _git(clone, "init", "-q")
        _git(clone, "remote", "add", "origin", str(upstream))
        assert capture_repo_identity(clone) is None

    def test_safe_git_info_includes_identity_when_remote_present(self, tmp_path: Path):
        from openshard.run._pipeline_helpers import _safe_git_info

        _git(tmp_path, "init", "-q")
        _git(tmp_path, "remote", "add", "origin", "https://github.com/Acme/Widget.git")
        info = _safe_git_info(tmp_path)
        assert info["repo_name"] == tmp_path.name
        assert info[REPO_IDENTITY_FIELD] == "github.com/Acme/Widget"
        assert "git_branch" in info and "git_dirty" in info

    def test_safe_git_info_omits_identity_without_remote(self, tmp_path: Path):
        from openshard.run._pipeline_helpers import _safe_git_info

        _git(tmp_path, "init", "-q")
        info = _safe_git_info(tmp_path)
        assert info["repo_name"] == tmp_path.name
        assert REPO_IDENTITY_FIELD not in info

    def test_safe_git_info_non_git_dir_unchanged(self, tmp_path: Path):
        from openshard.run._pipeline_helpers import _safe_git_info

        assert _safe_git_info(tmp_path) == {"repo_name": tmp_path.name}

    def test_safe_git_info_never_stores_absolute_path(self, tmp_path: Path):
        from openshard.run._pipeline_helpers import _safe_git_info

        _git(tmp_path, "init", "-q")
        _git(tmp_path, "remote", "add", "origin", "https://github.com/Acme/Widget.git")
        info = _safe_git_info(tmp_path)
        assert str(tmp_path) not in " ".join(str(v) for v in info.values())


class TestCaptureRepoIdentityFailures:
    def test_git_lookup_failure_yields_none(self, tmp_path: Path):
        with patch("openshard.history.repo_identity.subprocess.run", side_effect=OSError("no git")):
            assert capture_repo_identity(tmp_path) is None

    def test_git_timeout_yields_none(self, tmp_path: Path):
        with patch(
            "openshard.history.repo_identity.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=3),
        ):
            assert capture_repo_identity(tmp_path) is None

    def test_safe_git_info_survives_identity_failure(self, tmp_path: Path):
        from openshard.run._pipeline_helpers import _safe_git_info

        with patch(
            "openshard.history.repo_identity.capture_repo_identity",
            side_effect=RuntimeError("boom"),
        ):
            info = _safe_git_info(tmp_path)
        assert info["repo_name"] == tmp_path.name


# ---------------------------------------------------------------------------
# repository identity — matching historical entries
# ---------------------------------------------------------------------------


class TestEntryMatchesRepo:
    def test_historical_entry_without_identity_matches_repo_name(self):
        assert entry_matches_repo({"repo_name": "Widget"}, "widget")

    def test_historical_entry_matches_workspace_folder(self):
        assert entry_matches_repo({"workspace_path": "C:\\Users\\me\\Widget"}, "widget")
        assert entry_matches_repo({"workspace_path": "/home/me/Widget/"}, "widget")

    def test_identity_matches_canonical_and_url_forms(self):
        entry = {REPO_IDENTITY_FIELD: "github.com/Acme/Widget", "repo_name": "somewhere-else"}
        assert entry_matches_repo(entry, "github.com/acme/widget")
        assert entry_matches_repo(entry, "https://github.com/Acme/Widget.git")
        assert entry_matches_repo(entry, "git@github.com:Acme/Widget.git")
        assert entry_matches_repo(entry, "Acme/Widget")
        assert entry_matches_repo(entry, "Widget")

    def test_no_match(self):
        entry = {REPO_IDENTITY_FIELD: "github.com/Acme/Widget", "repo_name": "widget"}
        assert not entry_matches_repo(entry, "github.com/Other/Widget")
        assert not entry_matches_repo(entry, "gadget")

    def test_empty_filter_matches_everything(self):
        assert entry_matches_repo({}, "")

    def test_entries_with_and_without_identity_share_a_listing(self, tmp_path: Path):
        """A pre-identity record and a post-identity record for the same repo
        both surface under the folder-name filter; only the new one under the
        canonical identity filter."""
        _write(tmp_path, [
            _entry("old run", T1, shard_id="old", repo_name="Widget"),
            _entry("new run", T2, shard_id="new", repo_name="Widget",
                   **{REPO_IDENTITY_FIELD: "github.com/Acme/Widget"}),
        ])
        assert [s.shard_id for s in list_shards(repo_path=tmp_path, repo="widget")] == ["new", "old"]
        assert [s.shard_id for s in list_shards(repo_path=tmp_path, repo="github.com/acme/widget")] == ["new"]


# ---------------------------------------------------------------------------
# adapters persist the identity additively
# ---------------------------------------------------------------------------


class TestAdaptersRecordIdentity:
    def test_wrap_entry_carries_identity_when_available(self, tmp_path: Path):
        from openshard.adapters.wrap_exec import build_wrap_entry

        with patch("openshard.adapters.wrap_exec._parse_git_changed_files", return_value=([], "none")), \
             patch("openshard.history.repo_identity.capture_repo_identity", return_value="github.com/a/b"):
            entry = build_wrap_entry(
                task="t", model="m", repo_path=tmp_path,
                pre_state={"git_branch": "main"}, exit_code=0,
            )
        assert entry[REPO_IDENTITY_FIELD] == "github.com/a/b"

    def test_wrap_entry_omits_identity_when_unavailable(self, tmp_path: Path):
        from openshard.adapters.wrap_exec import build_wrap_entry

        with patch("openshard.adapters.wrap_exec._parse_git_changed_files", return_value=([], "none")), \
             patch("openshard.history.repo_identity.capture_repo_identity", return_value=None):
            entry = build_wrap_entry(
                task="t", model="m", repo_path=tmp_path,
                pre_state={"git_branch": "main"}, exit_code=0,
            )
        assert REPO_IDENTITY_FIELD not in entry
