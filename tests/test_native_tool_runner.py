from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

from openshard.native.tool_runner import NativeToolRunner
from openshard.native.tools import NativeToolCall, NativeToolSearchEvent


def _run_git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _make_runner(tmp_path: Path) -> NativeToolRunner:
    return NativeToolRunner(repo_root=tmp_path)


class TestNativeToolRunnerListFiles(unittest.TestCase):
    def test_run_list_files_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "foo.py").write_text("x = 1")
            runner = _make_runner(root)
            call = NativeToolCall(tool_name="list_files", args={"subdir": "."})
            result = runner.run(call)
        self.assertTrue(result.ok)
        self.assertIn("foo.py", result.output)
        self.assertIsNone(result.error)

    def test_run_list_files_default_subdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bar.py").write_text("")
            runner = _make_runner(root)
            call = NativeToolCall(tool_name="list_files", args={})
            result = runner.run(call)
        self.assertTrue(result.ok)
        self.assertIn("bar.py", result.output)


class TestNativeToolRunnerReadFile(unittest.TestCase):
    def test_run_read_file_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hello.py").write_text("print('hello')")
            runner = _make_runner(root)
            call = NativeToolCall(tool_name="read_file", args={"path": "hello.py"})
            result = runner.run(call)
        self.assertTrue(result.ok)
        self.assertIn("print('hello')", result.output)
        self.assertIsNone(result.error)


class TestNativeToolRunnerBlockedAndUnknown(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._runner = _make_runner(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_tool_returns_error(self):
        call = NativeToolCall(tool_name="no_such_tool", args={})
        result = self._runner.run(call)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_blocked_run_command_returns_error(self):
        call = NativeToolCall(tool_name="run_command", args={"cmd": "ls"})
        result = self._runner.run(call)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_write_file_without_approval_returns_error(self):
        call = NativeToolCall(tool_name="write_file", args={}, approved=False)
        result = self._runner.run(call)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_write_file_with_approval_still_returns_error(self):
        call = NativeToolCall(tool_name="write_file", args={}, approved=True)
        result = self._runner.run(call)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_malformed_args_does_not_crash(self):
        call = NativeToolCall(tool_name="list_files", args=None)  # type: ignore[arg-type]
        result = self._runner.run(call)
        self.assertIsInstance(result.ok, bool)


class TestNativeToolRunnerSearchRepo(unittest.TestCase):
    def test_run_search_repo_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "greet.py").write_text("def hello():\n    pass\n")
            runner = _make_runner(root)
            call = NativeToolCall(tool_name="search_repo", args={"query": "hello"})
            result = runner.run(call)
        self.assertTrue(result.ok)
        self.assertIn("greet.py", result.output)
        self.assertIsNone(result.error)

    def test_run_search_repo_empty_query_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = _make_runner(root)
            call = NativeToolCall(tool_name="search_repo", args={"query": ""})
            result = runner.run(call)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_run_search_repo_missing_query_key_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = _make_runner(root)
            call = NativeToolCall(tool_name="search_repo", args={})
            result = runner.run(call)
        self.assertFalse(result.ok)

    def test_run_search_repo_respects_max_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "many.py").write_text("\n".join(["hit"] * 30))
            runner = _make_runner(root)
            call = NativeToolCall(
                tool_name="search_repo",
                args={"query": "hit", "max_matches": 5},
            )
            result = runner.run(call)
        self.assertTrue(result.ok)
        self.assertLessEqual(result.metadata["matches"], 5)
        self.assertTrue(result.metadata["truncated"])

    def test_trace_entry_search_repo_no_full_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.py").write_text("find me here\n")
            runner = _make_runner(root)
            call = NativeToolCall(tool_name="search_repo", args={"query": "find me"})
            result = runner.run(call)
            entry = runner.trace_entry(call, result)
        self.assertIn("tool", entry)
        self.assertIn("ok", entry)
        self.assertIn("output_chars", entry)
        self.assertNotIn("output", entry)
        self.assertEqual(entry["output_chars"], len(result.output))


class TestNativeToolRunnerTraceEntry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        (self._root / "sample.py").write_text("x = 1")
        self._runner = _make_runner(self._root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_trace_entry_structure(self):
        call = NativeToolCall(tool_name="list_files", args={})
        result = self._runner.run(call)
        entry = self._runner.trace_entry(call, result)
        self.assertIn("tool", entry)
        self.assertIn("ok", entry)
        self.assertIn("approved", entry)
        self.assertIn("output_chars", entry)
        self.assertIn("error", entry)

    def test_trace_entry_no_full_output(self):
        call = NativeToolCall(tool_name="read_file", args={"path": "sample.py"})
        result = self._runner.run(call)
        entry = self._runner.trace_entry(call, result)
        self.assertNotIn("output", entry)

    def test_trace_entry_output_chars_matches_output_length(self):
        call = NativeToolCall(tool_name="list_files", args={})
        result = self._runner.run(call)
        entry = self._runner.trace_entry(call, result)
        self.assertEqual(entry["output_chars"], len(result.output))

    def test_trace_entry_ok_false_for_blocked(self):
        call = NativeToolCall(tool_name="run_command", args={})
        result = self._runner.run(call)
        entry = self._runner.trace_entry(call, result)
        self.assertFalse(entry["ok"])
        self.assertIsNotNone(entry["error"])

    def test_trace_entry_approved_reflects_call(self):
        call = NativeToolCall(tool_name="list_files", args={}, approved=True)
        result = self._runner.run(call)
        entry = self._runner.trace_entry(call, result)
        self.assertTrue(entry["approved"])


class TestNativeToolRunnerGitDiff(unittest.TestCase):
    def test_run_get_git_diff_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _run_git(root, "init")
            (root / "change.txt").write_text("original\n")
            _run_git(root, "add", "change.txt")
            (root / "change.txt").write_text("modified\n")
            runner = _make_runner(root)
            call = NativeToolCall(tool_name="get_git_diff", args={})
            result = runner.run(call)
        self.assertTrue(result.ok)
        self.assertIn("change.txt", result.output)

    def test_run_get_git_diff_non_git_repo_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = _make_runner(root)
            call = NativeToolCall(tool_name="get_git_diff", args={})
            result = runner.run(call)
        self.assertFalse(result.ok)

    def test_trace_entry_get_git_diff_no_full_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _run_git(root, "init")
            (root / "t.txt").write_text("a\n")
            _run_git(root, "add", "t.txt")
            (root / "t.txt").write_text("b\n")
            runner = _make_runner(root)
            call = NativeToolCall(tool_name="get_git_diff", args={})
            result = runner.run(call)
            entry = runner.trace_entry(call, result)
        self.assertIn("output_chars", entry)
        self.assertNotIn("output", entry)
        self.assertEqual(entry["output_chars"], len(result.output))

    def test_run_get_git_diff_respects_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _run_git(root, "init")
            (root / "big.txt").write_text("before\n")
            _run_git(root, "add", "big.txt")
            (root / "big.txt").write_text("after\n" * 1000)
            runner = _make_runner(root)
            call = NativeToolCall(tool_name="get_git_diff", args={"limit": 200})
            result = runner.run(call)
        self.assertTrue(result.ok)
        self.assertTrue(result.metadata["truncated"])


class TestNativeToolRunnerRunVerification(unittest.TestCase):
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

    def test_no_command_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = _make_runner(root)
            plan = self._make_plan(safety=None)
            p1, p2, p3 = self._patches(plan)
            with p1, p2, p3:
                result = runner.run(NativeToolCall("run_verification", {}))
        self.assertFalse(result.ok)
        self.assertIn("No verification command", result.error)

    def test_safe_command_passes(self):
        from openshard.verification.plan import CommandSafety
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = _make_runner(root)
            plan = self._make_plan(safety=CommandSafety.safe)
            p1, p2, p3 = self._patches(plan, run_return=(0, "1 passed"))
            with p1, p2, p3:
                result = runner.run(NativeToolCall("run_verification", {}))
        self.assertTrue(result.ok)

    def test_limit_arg_passed_through(self):
        from openshard.verification.plan import CommandSafety
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = _make_runner(root)
            plan = self._make_plan(safety=CommandSafety.safe)
            big_output = "x" * 5000
            p1, p2, p3 = self._patches(plan, run_return=(0, big_output))
            with p1, p2, p3:
                result = runner.run(NativeToolCall("run_verification", {"limit": 100}))
        self.assertTrue(result.ok)
        self.assertTrue(result.metadata["truncated"])

    def test_invalid_limit_falls_back_to_default(self):
        from openshard.verification.plan import CommandSafety
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = _make_runner(root)
            plan = self._make_plan(safety=CommandSafety.safe)
            p1, p2, p3 = self._patches(plan, run_return=(0, "ok"))
            with p1, p2, p3:
                result = runner.run(NativeToolCall("run_verification", {"limit": -1}))
        self.assertTrue(result.ok)

    def test_needs_approval_without_approved_returns_error(self):
        from openshard.verification.plan import CommandSafety
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = _make_runner(root)
            plan = self._make_plan(safety=CommandSafety.needs_approval)
            p1, p2, _ = self._patches(plan)
            with p1, p2:
                result = runner.run(NativeToolCall("run_verification", {}, approved=False))
        self.assertFalse(result.ok)
        self.assertIn("requires approval", result.error)

    def test_needs_approval_with_approved_executes(self):
        from openshard.verification.plan import CommandSafety
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = _make_runner(root)
            plan = self._make_plan(safety=CommandSafety.needs_approval)
            p1, p2, p3 = self._patches(plan, run_return=(0, "ok"))
            with p1, p2, p3:
                result = runner.run(NativeToolCall("run_verification", {}, approved=True))
        self.assertTrue(result.ok)


class TestNativeToolSearchEvent(unittest.TestCase):
    """NativeToolSearchEvent serialization and field contracts."""

    def test_serializes_cleanly(self):
        event = NativeToolSearchEvent(tool_name="search_repo")
        d = asdict(event)
        self.assertIsInstance(d, dict)
        self.assertEqual(d["tool_name"], "search_repo")

    def test_default_values(self):
        event = NativeToolSearchEvent(tool_name="list_files")
        self.assertEqual(event.selected_reason, "")
        self.assertEqual(event.query, "")
        self.assertEqual(event.result_count, 0)
        self.assertEqual(event.result_quality, "unknown")
        self.assertEqual(event.retry_count, 0)
        self.assertIsNone(event.fallback_tool)
        self.assertFalse(event.context_injected)
        self.assertFalse(event.changed_plan)
        self.assertEqual(event.warnings, [])

    def test_no_raw_content_keys_in_serialized_dict(self):
        event = NativeToolSearchEvent(tool_name="get_git_diff")
        d = asdict(event)
        forbidden = {"output", "snippets", "diff", "stdout", "stderr"}
        for key in forbidden:
            self.assertNotIn(key, d, f"Raw content key '{key}' must not appear in serialized event")

    def test_result_quality_values_are_from_allowed_set(self):
        allowed = {"unknown", "empty", "weak", "useful"}
        for quality in allowed:
            event = NativeToolSearchEvent(tool_name="search_repo", result_quality=quality)
            self.assertIn(asdict(event)["result_quality"], allowed)

    def test_full_fields_round_trip(self):
        event = NativeToolSearchEvent(
            tool_name="search_repo",
            selected_reason="observe search trigger",
            query="auth token",
            result_count=5,
            result_quality="useful",
            retry_count=0,
            fallback_tool=None,
            context_injected=True,
            changed_plan=False,
            warnings=["truncated"],
        )
        d = asdict(event)
        self.assertEqual(d["tool_name"], "search_repo")
        self.assertEqual(d["result_count"], 5)
        self.assertEqual(d["result_quality"], "useful")
        self.assertTrue(d["context_injected"])
        self.assertEqual(d["warnings"], ["truncated"])


class TestNativeToolRunnerWriteFile(unittest.TestCase):
    """write_file is a real, controlled tool: path-safe, policy-checked, evidence without content."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "a.py").write_bytes(b"x=1\ny=2\n")  # bytes: no newline translation on Windows
        self.runner = NativeToolRunner(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, path, content, approved=True, runner=None):
        return (runner or self.runner).run(
            NativeToolCall("write_file", {"path": path, "content": content}, approved=approved)
        )

    def test_update_reports_hashes_sizes_and_line_delta_but_never_content(self):
        import json

        res = self._write("a.py", "x=1\ny=3\nz=4\n")
        self.assertTrue(res.ok)
        self.assertEqual(res.metadata["change_type"], "update")
        self.assertEqual((res.metadata["lines_added"], res.metadata["lines_removed"]), (2, 1))
        self.assertEqual((res.metadata["bytes_before"], res.metadata["bytes_after"]), (8, 12))
        self.assertNotEqual(res.metadata["sha256_before"], res.metadata["sha256_after"])
        self.assertNotIn("y=3", json.dumps(res.metadata) + res.output)
        self.assertFalse(res.metadata["raw_content_stored"])
        self.assertEqual((self.root / "a.py").read_bytes(), b"x=1\ny=3\nz=4\n")

    def test_unchanged_create_and_nested_directories(self):
        same = self._write("a.py", "x=1\ny=2\n")
        self.assertTrue(same.ok)
        self.assertEqual(same.metadata["change_type"], "unchanged")
        created = self._write("new/dir/f.txt", "hi")
        self.assertTrue(created.ok)
        self.assertEqual(created.metadata["change_type"], "create")
        self.assertEqual((created.metadata["lines_added"], created.metadata["lines_removed"]), (1, 0))
        self.assertTrue((self.root / "new" / "dir" / "f.txt").exists())

    def test_unapproved_unsafe_and_protected_writes_are_refused(self):
        self.assertFalse(self._write("a.py", "x", approved=False).ok)
        self.assertEqual((self.root / "a.py").read_bytes(), b"x=1\ny=2\n")
        self.assertFalse(self._write("../escape.txt", "x").ok)
        self.assertFalse((self.root.parent / "escape.txt").exists())
        denied = self._write(".env", "S=1")  # approved, yet a built-in deny is never approved away
        self.assertFalse(denied.ok)
        self.assertEqual(denied.metadata["policy_decision"], "deny")
        self.assertFalse((self.root / ".env").exists())
        self.assertFalse(self._write("a.py", None).ok)  # type: ignore[arg-type]

    def test_edit_and_write_keep_a_crlf_files_line_endings(self):
        (self.root / "win.py").write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")
        res = self.runner.run(NativeToolCall(
            "edit_file", {"path": "win.py", "old_string": "b = 2\n", "new_string": "b = 20\nb2 = 21\n"}, approved=True,
        ))
        self.assertTrue(res.ok, res.error)
        self.assertEqual((self.root / "win.py").read_bytes(), b"a = 1\r\nb = 20\r\nb2 = 21\r\nc = 3\r\n")
        self.assertEqual((res.metadata["lines_added"], res.metadata["lines_removed"]), (2, 1))
        res = self._write("win.py", "x = 1\ny = 2\n")
        self.assertTrue(res.ok)
        self.assertEqual((self.root / "win.py").read_bytes(), b"x = 1\r\ny = 2\r\n")
        missing = self.runner.run(NativeToolCall(
            "edit_file", {"path": "win.py", "old_string": "nope", "new_string": "x"}, approved=True,
        ))
        self.assertFalse(missing.ok)
        self.assertEqual(missing.metadata["occurrences"], 0)

    def test_organisation_blocked_patterns_apply(self):
        org = NativeToolRunner(self.root, blocked_write_patterns=("src/**",))
        res = self._write("src/g.py", "x", runner=org)
        self.assertFalse(res.ok)
        self.assertEqual(res.metadata["policy_source"], "organisation_policy")
        self.assertFalse((self.root / "src" / "g.py").exists())
        self.assertTrue(self._write("other.py", "x", runner=org).ok)


if __name__ == "__main__":
    unittest.main()
