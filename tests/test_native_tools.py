from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from openshard.native.executor import NativeAgentExecutor, NativeRunMeta
from openshard.native.tools import (
    NativeTool,
    NativeToolCall,
    NativeToolResult,
    _exec_get_git_diff,
    _exec_list_files,
    _exec_read_file,
    _exec_run_verification,
    _exec_search_repo,
    classify_native_tool,
    compact_tool_result,
    get_native_tool,
    list_native_tools,
)


def _run_git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)

_EXPECTED_BUILTIN_NAMES = {
    "list_files",
    "read_file",
    "search_repo",
    "get_git_diff",
    "write_file",
    "edit_file",
    "run_verification",
    "run_command",
}


class TestListNativeTools(unittest.TestCase):
    def test_returns_all_builtins(self):
        tools = list_native_tools()
        names = {t.name for t in tools}
        self.assertEqual(names, _EXPECTED_BUILTIN_NAMES)

    def test_returns_copy(self):
        tools = list_native_tools()
        tools.clear()
        self.assertEqual(len(list_native_tools()), len(_EXPECTED_BUILTIN_NAMES))

    def test_each_entry_is_native_tool(self):
        for tool in list_native_tools():
            self.assertIsInstance(tool, NativeTool)

    def test_each_tool_has_non_empty_description(self):
        for tool in list_native_tools():
            self.assertTrue(tool.description.strip(), f"{tool.name} has empty description")

    def test_each_tool_has_valid_risk(self):
        valid = {"safe", "needs_approval", "blocked"}
        for tool in list_native_tools():
            self.assertIn(tool.risk, valid, f"{tool.name} has invalid risk: {tool.risk!r}")

    def test_each_tool_has_at_least_one_category(self):
        for tool in list_native_tools():
            self.assertTrue(tool.categories, f"{tool.name} has no categories")


class TestGetNativeTool(unittest.TestCase):
    def test_found_known_tool(self):
        tool = get_native_tool("list_files")
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertEqual(tool.name, "list_files")
        self.assertEqual(tool.risk, "safe")

    def test_not_found_unknown_tool(self):
        self.assertIsNone(get_native_tool("nonexistent_tool"))

    def test_not_found_empty_string(self):
        self.assertIsNone(get_native_tool(""))


class TestClassifyNativeTool(unittest.TestCase):
    def test_safe_tools(self):
        for name in ("list_files", "read_file", "search_repo", "get_git_diff", "run_verification"):
            with self.subTest(name=name):
                self.assertEqual(classify_native_tool(name), "safe")

    def test_needs_approval_tool(self):
        self.assertEqual(classify_native_tool("write_file"), "needs_approval")

    def test_blocked_tool(self):
        self.assertEqual(classify_native_tool("run_command"), "blocked")

    def test_unknown_tool_is_blocked(self):
        self.assertEqual(classify_native_tool("rm_rf"), "blocked")
        self.assertEqual(classify_native_tool(""), "blocked")


class TestCompactToolResult(unittest.TestCase):
    def test_short_output_unchanged(self):
        text = "hello world"
        self.assertEqual(compact_tool_result(text), text)

    def test_empty_output_unchanged(self):
        self.assertEqual(compact_tool_result(""), "")

    def test_exact_limit_unchanged(self):
        text = "x" * 4000
        self.assertEqual(compact_tool_result(text, limit=4000), text)

    def test_over_limit_truncated(self):
        text = "a" * 5000
        result = compact_tool_result(text, limit=4000)
        self.assertTrue(result.startswith("a" * 4000))
        self.assertIn("[truncated:", result)
        self.assertIn("4000", result)

    def test_custom_limit(self):
        text = "b" * 200
        result = compact_tool_result(text, limit=100)
        self.assertTrue(result.startswith("b" * 100))
        self.assertIn("[truncated:", result)


class TestNativeToolDataclasses(unittest.TestCase):
    def test_native_tool_call_defaults(self):
        call = NativeToolCall(tool_name="list_files", args={"subdir": "."})
        self.assertFalse(call.approved)

    def test_native_tool_result_defaults(self):
        result = NativeToolResult(tool_name="list_files", ok=True)
        self.assertEqual(result.output, "")
        self.assertIsNone(result.error)
        self.assertEqual(result.metadata, {})

    def test_native_tool_result_metadata_not_shared(self):
        r1 = NativeToolResult(tool_name="list_files", ok=True)
        r2 = NativeToolResult(tool_name="read_file", ok=True)
        r1.metadata["key"] = "value"
        self.assertNotIn("key", r2.metadata)


class TestExecListFiles(unittest.TestCase):
    def test_lists_files_in_tmp(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "foo.py").write_text("# foo")
            (root / "bar.txt").write_text("bar")
            subdir = root / "pkg"
            subdir.mkdir()
            (subdir / "mod.py").write_text("# mod")

            result = _exec_list_files(root)
            self.assertTrue(result.ok)
            self.assertIsNone(result.error)
            lines = result.output.splitlines()
            names = {Path(p).name for p in lines}
            self.assertIn("foo.py", names)
            self.assertIn("bar.txt", names)
            self.assertIn("mod.py", names)

    def test_ignores_git_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_dir = root / ".git"
            git_dir.mkdir()
            (git_dir / "HEAD").write_text("ref: refs/heads/main")
            (root / "real.py").write_text("# real")

            result = _exec_list_files(root)
            self.assertTrue(result.ok)
            lines = result.output.splitlines()
            self.assertFalse(any(".git" in p for p in lines))

    def test_ignores_pyc_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "module.py").write_text("# module")
            (root / "module.pyc").write_bytes(b"\x00\x00\x00\x00")

            result = _exec_list_files(root)
            self.assertTrue(result.ok)
            lines = result.output.splitlines()
            self.assertFalse(any(p.endswith(".pyc") for p in lines))

    def test_unsafe_subdir_returns_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _exec_list_files(root, subdir="../escape")
            self.assertFalse(result.ok)
            self.assertIsNotNone(result.error)


class TestExecReadFile(unittest.TestCase):
    def test_reads_safe_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "hello.txt").write_text("hello content")

            result = _exec_read_file(root, "hello.txt")
            self.assertTrue(result.ok)
            self.assertEqual(result.output, "hello content")
            self.assertIsNone(result.error)

    def test_unsafe_path_returns_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _exec_read_file(root, "../escape")
            self.assertFalse(result.ok)
            self.assertIsNotNone(result.error)

    def test_missing_file_returns_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _exec_read_file(root, "nonexistent.txt")
            self.assertFalse(result.ok)
            self.assertIsNotNone(result.error)

    def test_large_file_truncated(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            big = "z" * 5000
            (root / "big.txt").write_text(big)

            result = _exec_read_file(root, "big.txt")
            self.assertTrue(result.ok)
            self.assertIn("[truncated:", result.output)
            self.assertLessEqual(len(result.output), 5000)


class TestExecSearchRepo(unittest.TestCase):
    def test_finds_match_in_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "auth.py").write_text("def authenticate(user):\n    pass\n")
            result = _exec_search_repo(root, "authenticate")
        self.assertTrue(result.ok)
        self.assertIn("auth.py", result.output)

    def test_returns_line_numbers(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "mod.py").write_text("line one\ntarget line\nline three\n")
            result = _exec_search_repo(root, "target")
        self.assertTrue(result.ok)
        self.assertIn(":2:", result.output)

    def test_case_insensitive(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "mod.py").write_text("class Auth:\n    pass\n")
            result = _exec_search_repo(root, "auth")
        self.assertTrue(result.ok)
        self.assertIn("Auth", result.output)

    def test_empty_query_returns_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _exec_search_repo(root, "")
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_whitespace_query_returns_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _exec_search_repo(root, "   ")
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_ignores_git_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_dir = root / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text("needle in git")
            (root / "real.py").write_text("nothing here")
            result = _exec_search_repo(root, "needle")
        self.assertTrue(result.ok)
        self.assertNotIn(".git", result.output)

    def test_ignores_pyc_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "module.pyc").write_bytes(b"needle")
            result = _exec_search_repo(root, "needle")
        self.assertTrue(result.ok)
        self.assertNotIn(".pyc", result.output)

    def test_no_match_returns_ok_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "file.py").write_text("hello world\n")
            result = _exec_search_repo(root, "zzznomatch")
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "")
        self.assertEqual(result.metadata["matches"], 0)

    def test_max_matches_limits_results(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lines = "\n".join(["match line"] * 20)
            (root / "big.py").write_text(lines)
            result = _exec_search_repo(root, "match", max_matches=5)
        self.assertTrue(result.ok)
        self.assertLessEqual(result.metadata["matches"], 5)

    def test_truncated_flag_set_when_capped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lines = "\n".join(["hit"] * 20)
            (root / "hits.py").write_text(lines)
            result = _exec_search_repo(root, "hit", max_matches=5)
        self.assertTrue(result.metadata["truncated"])

    def test_truncated_false_when_not_capped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "few.py").write_text("hit\nhit\n")
            result = _exec_search_repo(root, "hit", max_matches=50)
        self.assertFalse(result.metadata["truncated"])

    def test_invalid_max_matches_falls_back_to_default(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "file.py").write_text("hello\n")
            result = _exec_search_repo(root, "hello", max_matches="bad")  # type: ignore[arg-type]
        self.assertTrue(result.ok)


class TestExecGetGitDiff(unittest.TestCase):
    def test_returns_changes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _run_git(root, "init")
            (root / "demo.txt").write_text("before\n")
            _run_git(root, "add", "demo.txt")
            (root / "demo.txt").write_text("after\n")
            result = _exec_get_git_diff(root)
        self.assertTrue(result.ok)
        self.assertIn("demo.txt", result.output)
        self.assertIn("after", result.output)

    def test_no_changes_returns_empty_output(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _run_git(root, "init")
            (root / "stable.txt").write_text("unchanged\n")
            _run_git(root, "add", "stable.txt")
            result = _exec_get_git_diff(root)
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "")

    def test_non_git_repo_returns_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _exec_get_git_diff(root)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_compacts_large_output(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _run_git(root, "init")
            (root / "big.txt").write_text("before\n")
            _run_git(root, "add", "big.txt")
            (root / "big.txt").write_text("after\n" * 1000)
            result = _exec_get_git_diff(root, limit=200)
        self.assertTrue(result.ok)
        self.assertTrue(result.metadata["truncated"])
        self.assertIn("[truncated:", result.output)

    def test_timeout_returns_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".git").mkdir()
            with patch(
                "openshard.native.tools.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["git"], 10.0),
            ):
                result = _exec_get_git_diff(root)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)

    def test_uses_no_shell(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".git").mkdir()
            mock_completed = MagicMock(returncode=0, stdout="", stderr="")
            with patch(
                "openshard.native.tools.subprocess.run",
                return_value=mock_completed,
            ) as mock_run:
                _exec_get_git_diff(root)
            call_kwargs = mock_run.call_args
            self.assertIsInstance(call_kwargs[0][0], list)
            self.assertNotEqual(call_kwargs[1].get("shell"), True)


class TestNativeFastPathToolTrace(unittest.TestCase):
    """Importing native tools must not pollute tool_trace on NativeRunMeta."""

    def test_tool_trace_empty_by_default(self):
        meta = NativeRunMeta()
        self.assertEqual(meta.tool_trace, [])

    def test_tool_trace_empty_after_generate(self):
        fake_gen = MagicMock()
        fake_gen.generate.return_value = MagicMock(usage=None, files=[], summary="ok", notes=[])
        fake_gen.model = "mock"
        fake_gen.fixer_model = "mock-fixer"

        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())

        executor.generate("add a feature")
        self.assertEqual(executor.native_meta.tool_trace, [])


class TestExecRunVerification(unittest.TestCase):
    def _make_plan(self, safety=None):
        from openshard.verification.plan import (
            CommandSafety,
            VerificationCommand,
            VerificationKind,
            VerificationPlan,
            VerificationSource,
        )
        if safety is None:
            return VerificationPlan(commands=[])
        reason = "safe test runner" if safety == CommandSafety.safe else "requires review"
        cmd = VerificationCommand(
            name="tests",
            argv=["python", "-m", "pytest"],
            kind=VerificationKind.test,
            source=VerificationSource.detected,
            safety=safety,
            reason=reason,
        )
        return VerificationPlan(commands=[cmd])

    def _patches(self, plan, run_return=(0, "")):
        fake_facts = MagicMock()
        return (
            patch("openshard.analysis.repo.analyze_repo", return_value=fake_facts),
            patch("openshard.verification.plan.build_verification_plan", return_value=plan),
            patch("openshard.verification.executor.run_verification_plan", return_value=run_return),
        )

    def test_no_commands_detected_returns_error(self):
        plan = self._make_plan(safety=None)
        p1, p2, p3 = self._patches(plan)
        with p1, p2, p3:
            result = _exec_run_verification(Path("/fake"))
        self.assertFalse(result.ok)
        self.assertIn("No verification command", result.error)
        self.assertFalse(result.metadata["attempted"])

    def test_safe_command_passes(self):
        from openshard.verification.plan import CommandSafety
        plan = self._make_plan(safety=CommandSafety.safe)
        p1, p2, p3 = self._patches(plan, run_return=(0, "1 passed"))
        with p1, p2, p3:
            result = _exec_run_verification(Path("/fake"))
        self.assertTrue(result.ok)
        self.assertTrue(result.metadata["passed"])
        self.assertEqual(result.metadata["exit_code"], 0)
        self.assertIsNone(result.error)
        self.assertIn("1 passed", result.output)

    def test_safe_command_fails_nonzero_exit(self):
        from openshard.verification.plan import CommandSafety
        plan = self._make_plan(safety=CommandSafety.safe)
        p1, p2, p3 = self._patches(plan, run_return=(2, "1 failed"))
        with p1, p2, p3:
            result = _exec_run_verification(Path("/fake"))
        self.assertFalse(result.ok)
        self.assertFalse(result.metadata["passed"])
        self.assertEqual(result.metadata["exit_code"], 2)
        self.assertIn("exit code 2", result.error)

    def test_needs_approval_without_approved_returns_error(self):
        from openshard.verification.plan import CommandSafety
        plan = self._make_plan(safety=CommandSafety.needs_approval)
        p1, p2, _ = self._patches(plan)
        with p1, p2:
            result = _exec_run_verification(Path("/fake"), approved=False)
        self.assertFalse(result.ok)
        self.assertIn("requires approval", result.error)
        self.assertFalse(result.metadata["attempted"])

    def test_needs_approval_with_approved_executes(self):
        from openshard.verification.plan import CommandSafety
        plan = self._make_plan(safety=CommandSafety.needs_approval)
        p1, p2, p3 = self._patches(plan, run_return=(0, "ok"))
        with p1, p2, p3:
            result = _exec_run_verification(Path("/fake"), approved=True)
        self.assertTrue(result.ok)
        self.assertTrue(result.metadata["attempted"])

    def test_blocked_command_returns_error_without_executing(self):
        from openshard.verification.plan import CommandSafety
        plan = self._make_plan(safety=CommandSafety.blocked)
        p1, p2, p3 = self._patches(plan)
        with p1, p2, p3 as mock_run:
            result = _exec_run_verification(Path("/fake"))
        self.assertFalse(result.ok)
        self.assertIn("blocked", result.error.lower())
        self.assertFalse(result.metadata["attempted"])
        mock_run.assert_not_called()

    def test_large_output_truncated(self):
        from openshard.verification.plan import CommandSafety
        plan = self._make_plan(safety=CommandSafety.safe)
        big_output = "x" * 5000
        p1, p2, p3 = self._patches(plan, run_return=(0, big_output))
        with p1, p2, p3:
            result = _exec_run_verification(Path("/fake"), limit=100)
        self.assertTrue(result.ok)
        self.assertTrue(result.metadata["truncated"])
        self.assertEqual(result.metadata["output_chars"], 5000)
        self.assertIn("[truncated:", result.output)
        self.assertLessEqual(len(result.output), 200)

    def test_not_truncated_when_output_fits(self):
        from openshard.verification.plan import CommandSafety
        plan = self._make_plan(safety=CommandSafety.safe)
        p1, p2, p3 = self._patches(plan, run_return=(0, "short output"))
        with p1, p2, p3:
            result = _exec_run_verification(Path("/fake"), limit=4000)
        self.assertFalse(result.metadata["truncated"])

    def test_command_count_in_metadata(self):
        from openshard.verification.plan import CommandSafety
        plan = self._make_plan(safety=CommandSafety.safe)
        p1, p2, p3 = self._patches(plan, run_return=(0, ""))
        with p1, p2, p3:
            result = _exec_run_verification(Path("/fake"))
        self.assertEqual(result.metadata["command_count"], 1)

    def test_all_blocked_checked_not_just_first(self):
        from openshard.verification.plan import (
            CommandSafety,
            VerificationCommand,
            VerificationKind,
            VerificationPlan,
            VerificationSource,
        )
        safe_cmd = VerificationCommand(
            name="tests",
            argv=["python", "-m", "pytest"],
            kind=VerificationKind.test,
            source=VerificationSource.detected,
            safety=CommandSafety.safe,
            reason="ok",
        )
        blocked_cmd = VerificationCommand(
            name="danger",
            argv=["rm", "-rf", "/"],
            kind=VerificationKind.unknown,
            source=VerificationSource.detected,
            safety=CommandSafety.blocked,
            reason="destructive command",
        )
        plan = VerificationPlan(commands=[safe_cmd, blocked_cmd])
        fake_facts = MagicMock()
        with (
            patch("openshard.analysis.repo.analyze_repo", return_value=fake_facts),
            patch("openshard.verification.plan.build_verification_plan", return_value=plan),
            patch("openshard.verification.executor.run_verification_plan") as mock_run,
        ):
            result = _exec_run_verification(Path("/fake"))
        self.assertFalse(result.ok)
        self.assertIn("blocked", result.error.lower())
        mock_run.assert_not_called()
