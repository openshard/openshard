"""Tests for openshard.history.locate: resolving the history root from any
directory inside a repository (PR9 local visibility)."""

from __future__ import annotations

from pathlib import Path

from openshard.history.locate import (
    HISTORY_RELPATH,
    RESOLVED_CWD,
    RESOLVED_FALLBACK,
    RESOLVED_GIT_ROOT,
    RESOLVED_HISTORY_DIR,
    history_log_path,
    locate_history,
    resolve_history_root,
)


def _git_repo(path: Path, *, runs: bool) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    if runs:
        (path / HISTORY_RELPATH).parent.mkdir(parents=True)
        (path / HISTORY_RELPATH).write_text('{"task": "x"}\n', encoding="utf-8")
    return path


class TestResolveHistoryRoot:
    def test_repo_root_itself(self, tmp_path: Path):
        repo = _git_repo(tmp_path / "repo", runs=True)
        assert resolve_history_root(repo) == repo.resolve()

    def test_subdirectory_resolves_to_repo_root(self, tmp_path: Path):
        repo = _git_repo(tmp_path / "repo", runs=True)
        sub = repo / "src" / "pkg"
        sub.mkdir(parents=True)
        assert resolve_history_root(sub) == repo.resolve()
        assert resolve_history_root(repo / "src") == repo.resolve()

    def test_deeper_subdirectory_same_root(self, tmp_path: Path):
        repo = _git_repo(tmp_path / "repo", runs=True)
        deep = repo / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        assert history_log_path(deep) == repo.resolve() / HISTORY_RELPATH

    def test_fresh_repo_without_history_uses_git_root(self, tmp_path: Path):
        repo = _git_repo(tmp_path / "repo", runs=False)
        sub = repo / "src"
        sub.mkdir()
        loc = locate_history(sub, with_identity=False)
        assert loc.root == repo.resolve()
        assert loc.resolved_from == RESOLVED_GIT_ROOT
        assert not loc.runs_path.exists()

    def test_nested_repo_does_not_read_parent(self, tmp_path: Path):
        outer = _git_repo(tmp_path / "outer", runs=True)
        inner = _git_repo(outer / "vendor" / "inner", runs=True)
        deep = inner / "lib"
        deep.mkdir()
        assert resolve_history_root(deep) == inner.resolve()
        assert resolve_history_root(inner) == inner.resolve()
        # A plain subdirectory of the outer repo still resolves to outer.
        other = outer / "docs"
        other.mkdir()
        assert resolve_history_root(other) == outer.resolve()

    def test_sibling_repo_is_never_reached(self, tmp_path: Path):
        a = _git_repo(tmp_path / "a", runs=True)
        _git_repo(tmp_path / "b", runs=True)
        sub = a / "x"
        sub.mkdir()
        assert resolve_history_root(sub) == a.resolve()

    def test_stray_history_dir_in_subdir_does_not_shadow_root_history(self, tmp_path: Path):
        repo = _git_repo(tmp_path / "repo", runs=True)
        stray = repo / "pkg"
        (stray / ".openshard").mkdir(parents=True)  # no runs.jsonl here
        assert resolve_history_root(stray) == repo.resolve()

    def test_subdir_history_wins_only_when_root_has_none(self, tmp_path: Path):
        repo = _git_repo(tmp_path / "repo", runs=False)
        sub = repo / "pkg"
        (sub / HISTORY_RELPATH).parent.mkdir(parents=True)
        (sub / HISTORY_RELPATH).write_text('{"task": "old"}\n', encoding="utf-8")
        deeper = sub / "inner"
        deeper.mkdir()
        loc = locate_history(deeper, with_identity=False)
        assert loc.root == sub.resolve()
        assert loc.resolved_from == RESOLVED_HISTORY_DIR
        # Once the root records history, the root wins for every subdirectory.
        (repo / HISTORY_RELPATH).parent.mkdir(parents=True)
        (repo / HISTORY_RELPATH).write_text('{"task": "new"}\n', encoding="utf-8")
        assert resolve_history_root(deeper) == repo.resolve()

    def test_git_file_marker_counts_as_boundary(self, tmp_path: Path):
        """Worktrees/submodules use a `.git` file, not a directory."""
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: ../.git/worktrees/wt\n", encoding="utf-8")
        sub = wt / "src"
        sub.mkdir()
        assert resolve_history_root(sub) == wt.resolve()

    def test_non_git_directory_with_history_is_cwd(self, tmp_path: Path):
        plain = tmp_path / "plain"
        (plain / ".openshard").mkdir(parents=True)
        loc = locate_history(plain, with_identity=False)
        assert loc.root == plain.resolve()
        assert loc.resolved_from == RESOLVED_CWD
        assert not loc.from_subdirectory

    def test_no_marker_anywhere_falls_back_to_start(self, tmp_path: Path, monkeypatch):
        """With no .git or .openshard up the tree the old cwd behaviour holds."""
        import openshard.history.locate as locate

        bare = tmp_path / "bare"
        bare.mkdir()
        # Make the walk blind to markers above tmp_path so the machine's own
        # checkouts (e.g. a version-controlled home directory) cannot leak in.
        real_git, real_hist = locate._has_git_marker, locate._has_history_dir
        ceiling = tmp_path.resolve()

        def _inside(p: Path) -> bool:
            try:
                p.resolve().relative_to(ceiling)
                return True
            except ValueError:
                return False

        monkeypatch.setattr(locate, "_has_git_marker", lambda p: _inside(p) and real_git(p))
        monkeypatch.setattr(locate, "_has_history_dir", lambda p: _inside(p) and real_hist(p))
        loc = locate_history(bare, with_identity=False)
        assert loc.root == bare.resolve()
        assert loc.resolved_from == RESOLVED_FALLBACK


class TestHistoryLocationDescription:
    def test_to_dict_has_no_absolute_path(self, tmp_path: Path):
        repo = _git_repo(tmp_path / "widget", runs=True)
        sub = repo / "src"
        sub.mkdir()
        loc = locate_history(sub, with_identity=False)
        d = loc.to_dict()
        assert d["name"] == "widget"
        assert d["history"] == ".openshard/runs.jsonl"
        assert d["from_subdirectory"] is True
        assert d["identity"] is None
        assert str(repo) not in str(d) and str(tmp_path) not in str(d)

    def test_display_name_prefers_identity(self, tmp_path: Path):
        repo = _git_repo(tmp_path / "widget", runs=True)
        from unittest.mock import patch

        with patch("openshard.history.repo_identity.capture_repo_identity", return_value="github.com/acme/widget"):
            loc = locate_history(repo)
        assert loc.repo_identity == "github.com/acme/widget"
        assert loc.display_name == "github.com/acme/widget"

    def test_identity_lookup_failure_is_swallowed(self, tmp_path: Path):
        repo = _git_repo(tmp_path / "widget", runs=True)
        from unittest.mock import patch

        with patch("openshard.history.repo_identity.capture_repo_identity", side_effect=RuntimeError("boom")):
            loc = locate_history(repo)
        assert loc.repo_identity is None
        assert loc.display_name == "widget"
