from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
from click.testing import CliRunner

from openshard.cli.main import _render_log_entry
from openshard.cli.run_output import _native_meta_from_entry, _render_native_demo_block
from openshard.native.context import NativeSandboxMeta
from openshard.native.sandbox import (
    _detect_git_root,
    _detect_git_state,
    _safe_branch_name,
    create_run_sandbox,
)
from openshard.security.paths import UnsafePathError, resolve_safe_repo_path


def configure_test_git_identity(repo_path: Path) -> None:
    """Set a Git identity local to the temp repo so commits never depend on
    global config. Scoped to repo_path only; never touches the user's real
    Git config."""
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "OpenShard Test"],
        cwd=str(repo_path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=str(repo_path), capture_output=True, check=True,
    )


def _init_git_repo(path: Path) -> None:
    """Create a git repo with an initial commit so worktrees can be created."""
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    configure_test_git_identity(path)
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(path), capture_output=True, check=True)


def _render(entry: dict, detail: str = "more") -> str:
    @click.command()
    def cmd():
        _render_log_entry(entry, detail)

    return CliRunner().invoke(cmd).output


def _make_native_entry(**overrides) -> dict:
    base = {
        "task": "test task",
        "workflow": "native",
        "executor": "native",
        "write_path": "pipeline",
        "native_loop_steps": [],
        "native_loop_trace": [],
        "read_search_findings": [],
    }
    base.update(overrides)
    return base


class TestSandboxCreation(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_worktree_meta_in_git_repo(self):
        repo = self._tmpdir / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        workspace, meta = create_run_sandbox(repo, "2026-05-15T10-30-00")

        self.assertTrue(meta.sandbox_enabled)
        self.assertEqual(meta.sandbox_type, "worktree")
        self.assertIsNotNone(meta.worktree_branch)
        self.assertTrue(meta.worktree_branch.startswith("osn/run-"))
        self.assertIsNotNone(meta.worktree_path)
        self.assertTrue(workspace.exists())
        self.assertIsNone(meta.fallback_reason)

    def test_fallback_outside_git_repo(self):
        with patch("openshard.native.sandbox._detect_git_root", return_value=None):
            workspace, meta = create_run_sandbox(self._tmpdir, "2026-05-15T10-30-00")

        self.assertTrue(meta.sandbox_enabled)
        self.assertEqual(meta.sandbox_type, "temp")
        self.assertTrue(workspace.exists())

    def test_fallback_reason_recorded_outside_git_repo(self):
        with patch("openshard.native.sandbox._detect_git_root", return_value=None):
            _, meta = create_run_sandbox(self._tmpdir, "run-001")

        self.assertEqual(meta.sandbox_type, "temp")
        self.assertIsNotNone(meta.fallback_reason)
        self.assertIn("git repo", meta.fallback_reason)

    def test_worktree_creation_failure_records_reason(self):
        repo = self._tmpdir / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        with patch("openshard.native.sandbox.subprocess.run") as mock_run:
            # 1. _detect_git_root: git rev-parse --show-toplevel
            detect_result = MagicMock()
            detect_result.returncode = 0
            detect_result.stdout = str(repo) + "\n"
            # 2. _detect_git_state: git rev-parse --abbrev-ref HEAD
            branch_result = MagicMock()
            branch_result.returncode = 0
            branch_result.stdout = "main\n"
            # 3. _detect_git_state: git rev-parse HEAD
            commit_result = MagicMock()
            commit_result.returncode = 0
            commit_result.stdout = "abc1234567890\n"
            # 4. git worktree add fails
            failed_result = MagicMock()
            failed_result.returncode = 128
            failed_result.stderr = "fatal: branch already exists"
            mock_run.side_effect = [detect_result, branch_result, commit_result, failed_result]

            workspace, meta = create_run_sandbox(repo, "run-002")

        self.assertTrue(meta.sandbox_enabled)
        self.assertEqual(meta.sandbox_type, "temp")
        self.assertIsNotNone(meta.fallback_reason)
        self.assertIn("already exists", meta.fallback_reason)
        self.assertTrue(workspace.exists())

    def test_safe_branch_name_format(self):
        branch = _safe_branch_name("2026-05-15T10:30:00.123456Z")
        self.assertTrue(branch.startswith("osn/run-"))
        self.assertNotIn(":", branch)

    def test_safe_branch_name_truncated(self):
        long_id = "a" * 100
        branch = _safe_branch_name(long_id)
        # osn/run- prefix + 40 chars max
        self.assertLessEqual(len(branch), len("osn/run-") + 40)

    def test_detect_git_root_returns_none_when_subprocess_fails(self):
        with patch("openshard.native.sandbox.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = _detect_git_root(self._tmpdir)
        self.assertIsNone(result)

    def test_detect_git_root_returns_path_inside_repo(self):
        repo = self._tmpdir / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        result = _detect_git_root(repo)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, Path)

    def test_detect_git_state_returns_branch_and_commit(self):
        repo = self._tmpdir / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        branch, commit = _detect_git_state(repo)
        self.assertIsNotNone(branch)
        self.assertNotEqual(branch, "HEAD")
        self.assertIsNotNone(commit)
        self.assertGreater(len(commit), 0)

    def test_detect_git_state_detached_head(self):
        repo = self._tmpdir / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        subprocess.run(["git", "checkout", "--detach", "HEAD"], cwd=str(repo), capture_output=True, check=True)
        branch, commit = _detect_git_state(repo)
        self.assertIsNone(branch)
        self.assertIsNotNone(commit)

    def test_detect_git_state_non_git_dir_returns_none_none(self):
        plain = self._tmpdir / "plain"
        plain.mkdir()
        branch, commit = _detect_git_state(plain)
        self.assertIsNone(branch)
        self.assertIsNone(commit)

    def test_detect_git_state_exception_returns_none_none(self):
        with patch("openshard.native.sandbox.subprocess.run", side_effect=Exception("fail")):
            branch, commit = _detect_git_state(self._tmpdir)
        self.assertIsNone(branch)
        self.assertIsNone(commit)

    def test_create_run_sandbox_worktree_populates_git_fields(self):
        repo = self._tmpdir / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        _, meta = create_run_sandbox(repo, "2026-05-31T10-00-00")
        self.assertIsNotNone(meta.git_base_branch)
        self.assertIsNotNone(meta.git_base_commit_hash)
        self.assertEqual(meta.safe_workspace_display_name, meta.worktree_branch)

    def test_create_run_sandbox_commit_hash_is_full(self):
        repo = self._tmpdir / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        _, meta = create_run_sandbox(repo, "2026-05-31T10-00-00")
        self.assertIsNotNone(meta.git_base_commit_hash)
        self.assertGreaterEqual(len(meta.git_base_commit_hash), 40)

    def test_create_run_sandbox_safe_display_name_not_absolute_path(self):
        repo = self._tmpdir / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        _, meta = create_run_sandbox(repo, "2026-05-31T10-00-00")
        name = meta.safe_workspace_display_name
        self.assertIsNotNone(name)
        self.assertFalse(name.startswith("/"), f"display name looks like absolute path: {name}")
        import re
        self.assertFalse(bool(re.match(r"[A-Za-z]:\\", name)), f"display name looks like Windows path: {name}")

    def test_create_run_sandbox_temp_fallback_has_osn_temp_display_name(self):
        with patch("openshard.native.sandbox._detect_git_root", return_value=None):
            _, meta = create_run_sandbox(self._tmpdir, "run-no-git")
        self.assertEqual(meta.safe_workspace_display_name, "osn-temp")

    def test_create_run_sandbox_worktree_fail_still_has_git_state(self):
        repo = self._tmpdir / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        with patch("openshard.native.sandbox.subprocess.run") as mock_run:
            detect_result = MagicMock()
            detect_result.returncode = 0
            detect_result.stdout = str(repo) + "\n"
            branch_result = MagicMock()
            branch_result.returncode = 0
            branch_result.stdout = "main\n"
            commit_result = MagicMock()
            commit_result.returncode = 0
            commit_result.stdout = "abc" * 14 + "de\n"  # 44 chars
            failed_result = MagicMock()
            failed_result.returncode = 128
            failed_result.stderr = "fatal: branch already exists"
            mock_run.side_effect = [detect_result, branch_result, commit_result, failed_result]

            _, meta = create_run_sandbox(repo, "run-fail")

        self.assertEqual(meta.sandbox_type, "temp")
        self.assertEqual(meta.git_base_branch, "main")
        self.assertIsNotNone(meta.git_base_commit_hash)
        self.assertEqual(meta.safe_workspace_display_name, "osn-temp")


class TestSandboxRendering(unittest.TestCase):

    def _sandbox_dict(self, sandbox_type="worktree", branch="osn/run-20260515-103000",
                      path="/tmp/wt-abc/wt", fallback=None) -> dict:
        return {
            "sandbox_enabled": True,
            "sandbox_type": sandbox_type,
            "worktree_branch": branch if sandbox_type == "worktree" else None,
            "worktree_path": path if sandbox_type == "worktree" else None,
            "fallback_reason": fallback,
        }

    def test_default_output_no_sandbox(self):
        entry = _make_native_entry(sandbox=self._sandbox_dict())
        out = _render(entry, detail="default")
        self.assertNotIn("sandbox", out)

    def test_more_shows_compact_worktree_sandbox(self):
        entry = _make_native_entry(sandbox=self._sandbox_dict())
        out = _render(entry, detail="full")
        self.assertIn("sandbox: worktree (osn/run-20260515-103000)", out)

    def test_more_does_not_show_sandbox_path(self):
        entry = _make_native_entry(sandbox=self._sandbox_dict(path="/tmp/wt-abc/wt"))
        out = _render(entry, detail="more")
        self.assertNotIn("sandbox path:", out)
        self.assertNotIn("sandbox cleanup:", out)

    def test_full_shows_detailed_worktree_sandbox(self):
        entry = _make_native_entry(sandbox=self._sandbox_dict(path="/tmp/wt-abc/wt"))
        out = _render(entry, detail="full")
        self.assertIn("sandbox: worktree (osn/run-20260515-103000)", out)
        self.assertIn("sandbox path: /tmp/wt-abc/wt", out)
        self.assertIn("sandbox cleanup: git worktree remove /tmp/wt-abc/wt", out)

    def test_more_shows_temp_fallback(self):
        entry = _make_native_entry(sandbox=self._sandbox_dict(
            sandbox_type="temp", fallback="not a git repo"
        ))
        out = _render(entry, detail="full")
        self.assertIn("sandbox: temp", out)
        self.assertIn("not a git repo", out)

    def test_old_run_records_no_sandbox_key_render_safely(self):
        entry = _make_native_entry()  # no "sandbox" key
        out_more = _render(entry, detail="more")
        out_full = _render(entry, detail="full")
        self.assertNotIn("sandbox", out_more)
        self.assertNotIn("sandbox", out_full)

    def test_sandbox_none_renders_safely(self):
        entry = _make_native_entry(sandbox=None)
        out = _render(entry, detail="more")
        self.assertNotIn("sandbox:", out)

    def test_render_native_demo_block_direct_worktree(self):
        from types import SimpleNamespace
        meta = SimpleNamespace(
            repo_context_summary=None,
            native_backend=None,
            native_backend_available=True,
            native_backend_notes=[],
            native_backend_proof=None,
            deepagents_adapter=None,
            observation=None,
            plan=None,
            write_path="pipeline",
            sandbox=SimpleNamespace(
                sandbox_enabled=True,
                sandbox_type="worktree",
                worktree_branch="osn/run-abc",
                worktree_path="/tmp/wt/abc",
                fallback_reason=None,
            ),
            verification_loop=None,
            verification_command_summary=None,
            command_policy_preview=None,
            diff_review=None,
            native_loop_steps=[],
            native_loop_trace=SimpleNamespace(events=[]),
            read_search_findings=[],
            osn_loop=None,
            osn_loop_summary=None,
            context_packet=None,
            context_quality_score=None,
            context_quality_advisory=None,
            final_report=None,
            change_budget=None,
            verification_plan=None,
            clarification_request=None,
            validation_contract=None,
        )
        lines_more = _render_native_demo_block(meta, detail="more")
        lines_full = _render_native_demo_block(meta, detail="full")
        self.assertTrue(any("sandbox: worktree (osn/run-abc)" in line for line in lines_more))
        self.assertTrue(any("sandbox path:" in line for line in lines_full))
        self.assertTrue(any("sandbox cleanup:" in line for line in lines_full))

    def test_render_native_demo_block_dict_backed(self):
        """Sandbox dict (as loaded from JSONL) renders correctly via _native_meta_from_entry."""
        entry = _make_native_entry(sandbox={
            "sandbox_enabled": True,
            "sandbox_type": "worktree",
            "worktree_branch": "osn/run-dict",
            "worktree_path": "/tmp/wt/dict",
            "fallback_reason": None,
        })
        native_meta = _native_meta_from_entry(entry)
        lines = _render_native_demo_block(native_meta, detail="more")
        self.assertTrue(any("sandbox: worktree (osn/run-dict)" in line for line in lines))


class TestSandboxPathSafety(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_path_safety_blocks_traversal_in_sandbox(self):
        """resolve_safe_repo_path still blocks traversal even when called with a worktree root."""
        sentinel = "openshard_sandbox_escape_marker.txt"
        with self.assertRaises(UnsafePathError):
            resolve_safe_repo_path(self._tmpdir, f"../../{sentinel}")

    def test_safe_write_path_works_in_worktree(self):
        """resolve_safe_repo_path accepts a valid relative path inside the worktree root."""
        # resolve_safe_repo_path resolves both repo_root and the candidate
        # (Path.resolve(), for containment checking), so compare against the
        # resolved form too -- on Windows the raw temp path and its resolved
        # form can differ (short vs. long name).
        target = (self._tmpdir / "output" / "result.txt").resolve()
        resolved = resolve_safe_repo_path(self._tmpdir, "output/result.txt")
        self.assertEqual(resolved, target)


class TestSandboxRunHistory(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sandbox_meta_serializes_via_asdict(self):
        from dataclasses import asdict
        meta = NativeSandboxMeta(
            sandbox_enabled=True,
            sandbox_type="worktree",
            worktree_path="/tmp/wt/abc",
            worktree_branch="osn/run-abc",
        )
        d = asdict(meta)
        self.assertEqual(d["sandbox_type"], "worktree")
        self.assertEqual(d["worktree_branch"], "osn/run-abc")
        self.assertIsNone(d["fallback_reason"])

    def test_sandbox_meta_temp_fallback_serializes(self):
        from dataclasses import asdict
        meta = NativeSandboxMeta(
            sandbox_enabled=True,
            sandbox_type="temp",
            fallback_reason="not a git repo",
        )
        d = asdict(meta)
        self.assertEqual(d["sandbox_type"], "temp")
        self.assertIsNone(d["worktree_path"])
        self.assertIsNone(d["worktree_branch"])
        self.assertEqual(d["fallback_reason"], "not a git repo")

    def test_sandbox_meta_new_fields_serialize_via_asdict(self):
        from dataclasses import asdict
        meta = NativeSandboxMeta(
            sandbox_enabled=True,
            sandbox_type="worktree",
            worktree_branch="osn/run-abc",
            git_base_branch="main",
            git_base_commit_hash="a" * 40,
            safe_workspace_display_name="osn/run-abc",
        )
        d = asdict(meta)
        self.assertEqual(d["git_base_branch"], "main")
        self.assertEqual(d["git_base_commit_hash"], "a" * 40)
        self.assertEqual(d["safe_workspace_display_name"], "osn/run-abc")

    def test_sandbox_meta_new_fields_default_none(self):
        meta = NativeSandboxMeta()
        self.assertIsNone(meta.git_base_branch)
        self.assertIsNone(meta.git_base_commit_hash)
        self.assertIsNone(meta.safe_workspace_display_name)


class TestPipelineSandboxPromotion(unittest.TestCase):

    def _promote(self, extra_metadata):
        from openshard.run.pipeline import _promote_sandbox_git_metadata
        _promote_sandbox_git_metadata(extra_metadata)

    def test_promotion_adds_top_level_git_keys(self):
        em = {"sandbox": {"git_base_branch": "main", "git_base_commit_hash": "abc" * 14}}
        self._promote(em)
        self.assertEqual(em["git_base_branch"], "main")
        self.assertEqual(em["git_base_commit_hash"], "abc" * 14)

    def test_promotion_skips_when_sandbox_is_none(self):
        em = {"sandbox": None}
        self._promote(em)
        self.assertNotIn("git_base_branch", em)
        self.assertNotIn("git_base_commit_hash", em)

    def test_promotion_skips_when_extra_metadata_is_none(self):
        self._promote(None)  # must not raise

    def test_promotion_does_not_invent_values(self):
        em = {"sandbox": {}}
        self._promote(em)
        self.assertNotIn("git_base_branch", em)
        self.assertNotIn("git_base_commit_hash", em)

    def test_promotion_skips_when_sandbox_key_missing(self):
        em = {"other": "value"}
        self._promote(em)
        self.assertNotIn("git_base_branch", em)
