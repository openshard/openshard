from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from openshard.analysis.repo import RepoFacts
from openshard.cli.main import cli
from openshard.native.context import (
    CompactRunState,
    NativeApprovalReceipt,
    NativeApprovalRequest,
    NativeChangeBudget,
    NativeChangeBudgetPreview,
    NativeChangeBudgetSoftGate,
    NativeCommandPolicyPreview,
    NativeContextBudget,
    NativeContextPacket,
    NativeContextQualityAdvisory,
    NativeContextQualityScore,
    NativeDiffReview,
    NativeEvidence,
    NativeFileSnippet,
    NativeFinalReport,
    NativeObservation,
    NativePatchProposal,
    NativePlan,
    NativeVerificationCommandSummary,
    NativeVerificationLoop,
    OSNLoopMeta,
    OSNLoopStep,
    build_compact_run_state,
    build_initial_context_budget,
    build_native_approval_receipt,
    build_native_budget_gate_approval_request,
    build_native_change_budget,
    build_native_change_budget_preview,
    build_native_change_budget_soft_gate,
    build_native_command_policy_preview,
    build_native_context_quality_advisory,
    build_native_context_quality_score,
    build_native_diff_review,
    build_native_final_report,
    build_native_patch_proposal,
    build_native_verification_command_summary,
    render_native_change_budget,
    render_native_context_quality_advisory,
    render_native_evidence,
    render_native_observation,
    render_native_plan,
    render_verification_failure_context,
)
from openshard.native.executor import (
    _MAX_EXPLICIT_SNIPPET_FILES,
    _MAX_OSN_LOOP_STEPS,
    NativeAgentExecutor,
    NativeRunMeta,
    _build_native_plan,
    _extract_explicit_file_paths,
    _extract_search_query,
    _extract_snippet_lines,
    _infer_result_count,
    _infer_result_quality,
    _infer_zero_result_case,
    _parse_search_result_line,
)
from openshard.native.repo_context import NativeRepoContextSummary
from openshard.native.skills import NativeSkill, NativeSkillMatch
from openshard.native.tools import NativeToolCall

_DEFAULT_CONFIG = {"approval_mode": "smart"}

_PYTHON_REPO = RepoFacts(
    languages=["python"], package_files=[], framework=None,
    test_command=None, risky_paths=[], changed_files=[],
)


def _fake_result():
    r = MagicMock()
    r.usage = None
    r.files = []
    r.summary = "done"
    r.notes = []
    return r


def _make_generator_mock():
    g = MagicMock()
    g.generate.return_value = _fake_result()
    g.model = "mock-model"
    g.fixer_model = "mock-fixer"
    return g


def _make_native_mock():
    g = MagicMock(spec=NativeAgentExecutor)
    g.generate.return_value = _fake_result()
    g.model = "mock-model"
    g.fixer_model = "mock-fixer"
    g.native_meta = NativeRunMeta()
    g.build_change_budget_soft_gate.return_value = NativeChangeBudgetSoftGate(
        requires_approval=False, reason="", action="allow"
    )
    g.build_budget_gate_approval_request.return_value = NativeApprovalRequest(
        source="change_budget_soft_gate",
        requires_approval=False,
        reason="",
        action="allow",
    )
    return g


def _make_manager_mock():
    m = MagicMock()
    inv = MagicMock()
    inv.models = []
    m.get_inventory.return_value = inv
    m.providers = {"openrouter": MagicMock()}
    return m


class TestNativeRunMeta(unittest.TestCase):
    """NativeRunMeta default values."""

    def test_defaults(self):
        meta = NativeRunMeta()
        self.assertEqual(meta.workflow, "native")
        self.assertEqual(meta.executor, "native")
        self.assertEqual(meta.execution_depth, "fast")
        self.assertEqual(meta.selected_skills, [])
        self.assertIsNone(meta.context_budget)
        self.assertIsNone(meta.context_state)
        self.assertEqual(meta.context_warnings, [])
        self.assertEqual(meta.tool_trace, [])
        self.assertIsNone(meta.repo_context_summary)


class TestNativeContextBudget(unittest.TestCase):
    """Unit tests for NativeContextBudget, CompactRunState, and their builders."""

    def test_budget_defaults(self):
        budget = NativeContextBudget()
        self.assertIsNone(budget.context_window)
        self.assertEqual(budget.estimated_tokens_used, 0)
        self.assertIsNone(budget.estimated_tokens_remaining)
        self.assertEqual(budget.files_loaded, 0)
        self.assertEqual(budget.skills_loaded, 0)
        self.assertFalse(budget.repo_map_built)
        self.assertEqual(budget.warnings, [])

    def test_build_initial_budget_no_window(self):
        budget = build_initial_context_budget()
        self.assertIsNone(budget.context_window)
        self.assertIsNone(budget.estimated_tokens_remaining)
        self.assertEqual(budget.estimated_tokens_used, 0)

    def test_build_initial_budget_with_window(self):
        budget = build_initial_context_budget(context_window=8000)
        self.assertEqual(budget.context_window, 8000)
        self.assertEqual(budget.estimated_tokens_remaining, 8000)
        self.assertEqual(budget.estimated_tokens_used, 0)

    def test_compact_run_state_defaults(self):
        state = CompactRunState()
        self.assertEqual(state.task_goal, "")
        self.assertEqual(state.repo_facts_summary, "")
        self.assertEqual(state.files_touched, [])
        self.assertIsNone(state.verification_result)
        self.assertEqual(state.blockers, [])
        self.assertEqual(state.next_step, "")

    def test_build_compact_run_state_defaults(self):
        state = build_compact_run_state("fix the bug")
        self.assertEqual(state.task_goal, "fix the bug")
        self.assertEqual(state.repo_facts_summary, "")
        self.assertEqual(state.files_touched, [])
        self.assertIsNone(state.verification_result)
        self.assertEqual(state.blockers, [])
        self.assertEqual(state.next_step, "")

    def test_build_compact_run_state_with_values(self):
        state = build_compact_run_state(
            task_goal="refactor auth",
            repo_facts_summary="Python, Django",
            files_touched=["auth.py", "views.py"],
            verification_result="passed",
            blockers=["missing test"],
            next_step="add coverage",
        )
        self.assertEqual(state.task_goal, "refactor auth")
        self.assertEqual(state.repo_facts_summary, "Python, Django")
        self.assertEqual(state.files_touched, ["auth.py", "views.py"])
        self.assertEqual(state.verification_result, "passed")
        self.assertEqual(state.blockers, ["missing test"])
        self.assertEqual(state.next_step, "add coverage")

    def test_build_compact_run_state_none_lists_default_to_empty(self):
        state = build_compact_run_state("task", files_touched=None, blockers=None)
        self.assertEqual(state.files_touched, [])
        self.assertEqual(state.blockers, [])


class TestNativeAgentExecutor(unittest.TestCase):
    """NativeAgentExecutor unit tests — no real provider or API calls."""

    def _make_executor(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor, fake_gen

    def test_native_meta_defaults(self):
        executor, _ = self._make_executor()
        self.assertIsInstance(executor.native_meta, NativeRunMeta)
        self.assertEqual(executor.native_meta.workflow, "native")
        self.assertEqual(executor.native_meta.executor, "native")
        self.assertEqual(executor.native_meta.execution_depth, "fast")
        self.assertEqual(executor.native_meta.selected_skills, [])
        self.assertIsNone(executor.native_meta.context_budget)
        self.assertIsNone(executor.native_meta.context_state)
        self.assertEqual(executor.native_meta.context_warnings, [])
        self.assertEqual(executor.native_meta.tool_trace, [])

    def test_model_attributes_forwarded(self):
        executor, fake_gen = self._make_executor()
        self.assertEqual(executor.model, fake_gen.model)
        self.assertEqual(executor.fixer_model, fake_gen.fixer_model)

    def test_generate_delegates_to_inner_generator(self):
        executor, fake_gen = self._make_executor()
        result = executor.generate("fix the bug", model="some-model")
        fake_gen.generate.assert_called_once()
        _, kwargs = fake_gen.generate.call_args
        self.assertIn("[native plan]", kwargs["skills_context"])
        self.assertIs(result, fake_gen.generate.return_value)

    def test_generate_passes_repo_facts_and_skills(self):
        executor, fake_gen = self._make_executor()
        executor.generate("task", repo_facts=_PYTHON_REPO, skills_context="hint")
        _, kwargs = fake_gen.generate.call_args
        self.assertIn("[native plan]", kwargs["skills_context"])
        self.assertIn("hint", kwargs["skills_context"])

    def test_generate_scrubs_secret_before_model_context(self):
        # A secret-like value present in the candidate context must be redacted
        # before it is handed to the inner generator (i.e. before model use).
        executor, fake_gen = self._make_executor()
        raw_secret = "AKIA1234567890ABCDEF"
        executor.generate("task", skills_context=f"config AWS={raw_secret} end")
        _, kwargs = fake_gen.generate.call_args
        injected = kwargs["skills_context"]
        # Raw secret never reaches the model context...
        self.assertNotIn(raw_secret, injected)
        # ...but a redacted marker remains so context stays readable.
        self.assertIn("AKIA...CDEF", injected)
        # And the finding is recorded safely on native_meta.
        self.assertIsNotNone(executor.native_meta.pre_context_secret_scan)
        findings = executor.native_meta.pre_context_secret_scan.findings
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "aws_access_key_id")
        self.assertEqual(findings[0].path, "<model-context>")

    def test_generate_leaves_clean_context_untouched(self):
        executor, fake_gen = self._make_executor()
        executor.generate("task", skills_context="nothing secret here")
        _, kwargs = fake_gen.generate.call_args
        self.assertIn("nothing secret here", kwargs["skills_context"])
        self.assertIsNone(executor.native_meta.pre_context_secret_scan)

    def test_generate_updates_selected_skills(self):
        executor, _ = self._make_executor()
        fake_skill = NativeSkill(
            name="test-discovery",
            description="test",
            categories=["testing"],
            triggers=["test"],
        )
        fake_match = NativeSkillMatch(skill=fake_skill, reason="task matches: test", score=1.0)
        with patch(
            "openshard.native.executor.match_builtin_skills",
            return_value=[fake_match],
        ):
            executor.generate("run the tests")
        self.assertEqual(executor.native_meta.selected_skills, ["test-discovery"])


class TestNativeAgentExecutorRunTool(unittest.TestCase):
    """Tests for NativeAgentExecutor.run_tool() and tool_trace population."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor

    def _make_executor_no_repo(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor

    def test_run_tool_appends_trace_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x = 1")
            executor = self._make_executor_with_repo(root)
            call = NativeToolCall(tool_name="list_files", args={})
            executor.run_tool(call)
        self.assertEqual(len(executor.native_meta.tool_trace), 1)

    def test_run_tool_trace_has_no_output_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "b.py").write_text("y = 2")
            executor = self._make_executor_with_repo(root)
            call = NativeToolCall(tool_name="list_files", args={})
            executor.run_tool(call)
        entry = executor.native_meta.tool_trace[0]
        self.assertNotIn("output", entry)

    def test_run_tool_trace_entry_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            call = NativeToolCall(tool_name="list_files", args={})
            executor.run_tool(call)
        entry = executor.native_meta.tool_trace[0]
        self.assertIn("tool", entry)
        self.assertIn("ok", entry)
        self.assertIn("approved", entry)
        self.assertIn("output_chars", entry)
        self.assertIn("error", entry)

    def test_run_tool_no_repo_root_returns_error_and_appends_trace(self):
        executor = self._make_executor_no_repo()
        call = NativeToolCall(tool_name="list_files", args={})
        result = executor.run_tool(call)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        self.assertEqual(len(executor.native_meta.tool_trace), 1)
        self.assertFalse(executor.native_meta.tool_trace[0]["ok"])

    def test_generate_leaves_tool_trace_unchanged(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        executor.generate("do something")
        self.assertEqual(executor.native_meta.tool_trace, [])

    def test_preflight_stores_repo_context_summary_when_repo_root_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            (root / "utils.py").write_text("y = 2")
            fake_gen = _make_generator_mock()
            with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
                executor = NativeAgentExecutor(provider=MagicMock(), repo_root=root)
            executor.generate("fix bug")
        self.assertIsInstance(executor.native_meta.repo_context_summary, NativeRepoContextSummary)
        self.assertGreater(executor.native_meta.repo_context_summary.total_files, 0)
        self.assertTrue(executor.native_meta.context_budget.repo_map_built)

    def test_preflight_not_called_without_repo_root(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        executor.generate("fix bug")
        self.assertIsNone(executor.native_meta.repo_context_summary)
        self.assertIsNone(executor.native_meta.context_budget)

    def test_multiple_run_tool_calls_accumulate_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "c.py").write_text("z = 3")
            executor = self._make_executor_with_repo(root)
            executor.run_tool(NativeToolCall(tool_name="list_files", args={}))
            executor.run_tool(NativeToolCall(tool_name="run_command", args={}))
        self.assertEqual(len(executor.native_meta.tool_trace), 2)


class TestNativePreflight(unittest.TestCase):
    """Tests for NativeAgentExecutor._run_preflight() via generate()."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor, fake_gen

    def _make_executor_no_repo(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor, fake_gen

    def test_generate_with_repo_root_runs_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        self.assertGreaterEqual(len(executor.native_meta.tool_trace), 1)
        self.assertEqual(executor.native_meta.tool_trace[0]["tool"], "list_files")

    def test_preflight_records_list_files_in_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor, _ = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        entry = executor.native_meta.tool_trace[0]
        self.assertIn("tool", entry)
        self.assertIn("ok", entry)
        self.assertIn("approved", entry)
        self.assertIn("output_chars", entry)
        self.assertIn("error", entry)

    def test_preflight_does_not_store_full_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("a = 1")
            executor, _ = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        self.assertNotIn("output", executor.native_meta.tool_trace[0])

    def test_context_budget_updated_after_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor, _ = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        self.assertIsNotNone(executor.native_meta.context_budget)
        self.assertTrue(executor.native_meta.context_budget.repo_map_built)

    def test_context_budget_files_loaded_not_incremented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "b.py").write_text("b = 2")
            executor, _ = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        self.assertEqual(executor.native_meta.context_budget.files_loaded, 0)

    def test_generate_without_repo_root_still_works(self):
        executor, fake_gen = self._make_executor_no_repo()
        result = executor.generate("fix the bug")
        self.assertIs(result, fake_gen.generate.return_value)
        self.assertEqual(executor.native_meta.tool_trace, [])
        self.assertIsNone(executor.native_meta.context_budget)


class TestNativeContextInjection(unittest.TestCase):
    """Tests for repo context summary injection into ExecutionGenerator.generate()."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor, fake_gen

    def _make_executor_no_repo(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor, fake_gen

    def test_generate_with_repo_root_passes_rendered_context_to_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        _, kwargs = fake_gen.generate.call_args
        self.assertIn("[repo context]", kwargs["skills_context"])

    def test_generate_combines_repo_context_with_existing_skills_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("fix the bug", skills_context="hint")
        _, kwargs = fake_gen.generate.call_args
        self.assertIn("[repo context]", kwargs["skills_context"])
        self.assertIn("hint", kwargs["skills_context"])

    def test_generate_without_repo_root_plan_and_skills_context_present(self):
        executor, fake_gen = self._make_executor_no_repo()
        executor.generate("fix the bug", skills_context="hint")
        _, kwargs = fake_gen.generate.call_args
        self.assertIn("[native plan]", kwargs["skills_context"])
        self.assertIn("hint", kwargs["skills_context"])

    def test_generate_with_repo_root_updates_estimated_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor, _ = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        self.assertGreater(executor.native_meta.context_budget.estimated_tokens_used, 0)

    def test_generate_without_repo_root_no_context_budget(self):
        executor, _ = self._make_executor_no_repo()
        executor.generate("fix the bug")
        self.assertIsNone(executor.native_meta.context_budget)


class TestNativeWorkflowIntegration(unittest.TestCase):
    """Integration tests via CLI runner — verify routing and log metadata."""

    def _run(self, args, native_mock=None, generator_mock=None):
        native_mock = native_mock or _make_native_mock()
        generator_mock = generator_mock or _make_generator_mock()
        manager_mock = _make_manager_mock()
        logged = {}

        def _capture_log(*a, **kw):
            logged.update(kw.get("extra_metadata") or {})

        with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock), \
             patch("openshard.run.pipeline.ProviderManager", return_value=manager_mock), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run", side_effect=_capture_log):
            runner = CliRunner()
            result = runner.invoke(cli, ["run"] + args)
        return result, native_mock, logged

    def test_native_workflow_selects_native_executor(self):
        """--workflow native instantiates NativeAgentExecutor, not ExecutionGenerator."""
        native_mock = _make_native_mock()
        generator_mock = _make_generator_mock()

        with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock) as native_cls, \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock) as gen_cls, \
             patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run"):
            runner = CliRunner()
            runner.invoke(cli, ["run", "--workflow", "native", "add a feature"])

        native_cls.assert_called_once()
        gen_cls.assert_not_called()

    def test_native_log_contains_metadata_fields(self):
        result, _, logged = self._run(["--workflow", "native", "add a feature"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(logged.get("workflow"), "native")
        self.assertEqual(logged.get("executor"), "native")
        self.assertEqual(logged.get("execution_depth"), "fast")
        self.assertEqual(logged.get("selected_skills"), [])
        self.assertIsNone(logged.get("context_budget"))
        self.assertIsNone(logged.get("context_state"))
        self.assertEqual(logged.get("context_warnings"), [])
        self.assertEqual(logged.get("tool_trace"), [])
        self.assertIn("repo_context_summary", logged)

    def test_native_log_repo_context_summary_is_none_without_repo_root(self):
        result, _, logged = self._run(["--workflow", "native", "add a feature"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(logged.get("repo_context_summary"))

    def test_native_log_contains_evidence_key(self):
        result, _, logged = self._run(["--workflow", "native", "add a feature"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("evidence", logged)
        self.assertIsNone(logged.get("evidence"))

    def test_native_dry_run(self):
        """--workflow native --dry-run exits 0 without writing files."""
        result, native_mock, _ = self._run(["--workflow", "native", "--dry-run", "add a feature"])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_direct_workflow_unchanged(self):
        """--workflow direct still instantiates ExecutionGenerator."""
        generator_mock = _make_generator_mock()
        with patch("openshard.run.pipeline.NativeAgentExecutor") as native_cls, \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock) as gen_cls, \
             patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run"):
            runner = CliRunner()
            runner.invoke(cli, ["run", "--workflow", "direct", "add a feature"])

        gen_cls.assert_called_once()
        native_cls.assert_not_called()

    def test_staged_workflow_unchanged(self):
        """--workflow staged still uses ExecutionGenerator."""
        generator_mock = _make_generator_mock()
        with patch("openshard.run.pipeline.NativeAgentExecutor") as native_cls, \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock) as gen_cls, \
             patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run"):
            runner = CliRunner()
            runner.invoke(cli, ["run", "--workflow", "staged", "add a feature"])

        gen_cls.assert_called_once()
        native_cls.assert_not_called()

    def test_opencode_workflow_unchanged(self):
        """--workflow opencode still uses OpenCodeExecutor."""
        with patch("openshard.run.pipeline.NativeAgentExecutor") as native_cls, \
             patch("openshard.run.pipeline.ExecutionGenerator") as gen_cls, \
             patch("openshard.run.pipeline.OpenCodeExecutor") as oc_cls, \
             patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run"):
            oc_mock = _make_generator_mock()
            oc_cls.return_value = oc_mock
            runner = CliRunner()
            runner.invoke(cli, ["run", "--workflow", "opencode", "add a feature"])

        oc_cls.assert_called_once()
        native_cls.assert_not_called()
        gen_cls.assert_not_called()

    def test_claude_code_still_rejected(self):
        """--workflow claude-code must still raise a ClickException."""
        with patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG):
            runner = CliRunner()
            result = runner.invoke(cli, ["run", "--workflow", "claude-code", "task"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not yet available", result.output)

    def test_non_native_log_has_no_native_metadata(self):
        """--workflow direct produces no native metadata in the log entry."""
        generator_mock = _make_generator_mock()
        logged = {}

        def _capture_log(*a, **kw):
            logged.update(kw.get("extra_metadata") or {})

        with patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock), \
             patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run", side_effect=_capture_log):
            runner = CliRunner()
            runner.invoke(cli, ["run", "--workflow", "direct", "add a feature"])

        self.assertNotIn("workflow", logged)
        self.assertNotIn("execution_depth", logged)

    def test_native_cli_passes_repo_root_to_executor(self):
        """pipeline passes repo_root=Path.cwd() when instantiating NativeAgentExecutor."""
        captured = {}

        def _capture(provider=None, repo_root=None, backend_name="builtin", **kwargs):
            captured["repo_root"] = repo_root
            return _make_native_mock()

        with tempfile.TemporaryDirectory() as tmp:
            fake_cwd = Path(tmp)
            with patch("openshard.run.pipeline.NativeAgentExecutor", side_effect=_capture), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.Path.cwd", return_value=fake_cwd):
                runner = CliRunner()
                runner.invoke(cli, ["run", "--workflow", "native", "create hello file"])

        self.assertEqual(captured.get("repo_root"), fake_cwd)

    def test_native_write_uses_existing_pipeline_write_path(self):
        """--write with native workflow writes files via existing _write_files, not a native tool."""
        from openshard.execution.generator import ChangedFile

        safe_file = ChangedFile(
            path="hello.txt", content="hello world", change_type="create", summary="created"
        )
        fake_result = MagicMock()
        fake_result.files = [safe_file]
        fake_result.summary = "done"
        fake_result.notes = []
        fake_result.usage = None

        native_mock = _make_native_mock()
        native_mock.generate.return_value = fake_result

        with tempfile.TemporaryDirectory() as workspace_dir:
            from openshard.native.context import NativeSandboxMeta
            _sb_meta = NativeSandboxMeta(sandbox_enabled=True, sandbox_type="temp", fallback_reason="test")
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.native.sandbox.create_run_sandbox",
                       return_value=(Path(workspace_dir), _sb_meta)):
                runner = CliRunner()
                result = runner.invoke(
                    cli, ["run", "--workflow", "native", "--write", "create hello file"]
                )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue((Path(workspace_dir) / "hello.txt").exists())
            self.assertEqual((Path(workspace_dir) / "hello.txt").read_text(), "hello world")
            tool_trace = native_mock.native_meta.tool_trace
            write_file_calls = [e for e in tool_trace if e.get("tool") == "write_file"]
            self.assertEqual(write_file_calls, [])

    def test_native_write_rejects_unsafe_path(self):
        """--write with native workflow rejects dotdot paths via existing _write_files safety."""
        from openshard.execution.generator import ChangedFile

        unsafe_file = ChangedFile(
            path="../escape.txt", content="evil", change_type="create", summary=""
        )
        fake_result = MagicMock()
        fake_result.files = [unsafe_file]
        fake_result.summary = "done"
        fake_result.notes = []
        fake_result.usage = None

        native_mock = _make_native_mock()
        native_mock.generate.return_value = fake_result

        with CliRunner().isolated_filesystem():
            parent = Path("..").resolve()
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run"):
                runner = CliRunner()
                result = runner.invoke(
                    cli, ["run", "--workflow", "native", "--write", "do something"]
                )

            self.assertFalse((parent / "escape.txt").exists())
            self.assertIn("[skip] unsafe path rejected", result.output)

    def test_native_dry_run_does_not_write(self):
        """--dry-run with native workflow does not write files to disk."""
        from openshard.execution.generator import ChangedFile

        safe_file = ChangedFile(
            path="hello.txt", content="hello world", change_type="create", summary="created"
        )
        fake_result = MagicMock()
        fake_result.files = [safe_file]
        fake_result.summary = "done"
        fake_result.notes = []
        fake_result.usage = None

        native_mock = _make_native_mock()
        native_mock.generate.return_value = fake_result

        with CliRunner().isolated_filesystem():
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run"):
                runner = CliRunner()
                result = runner.invoke(
                    cli, ["run", "--workflow", "native", "--dry-run", "create hello file"]
                )

            self.assertFalse(Path("hello.txt").exists())
            self.assertEqual(result.exit_code, 0, result.output)


class TestNativeObservePhase(unittest.TestCase):
    """Tests for NativeAgentExecutor._run_observe_phase()."""

    def _run_git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor

    def _make_executor_no_repo(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor

    def test_observe_returns_early_without_runner(self):
        executor = self._make_executor_no_repo()
        executor._run_observe_phase("fix the bug")
        self.assertEqual(executor.native_meta.tool_trace, [])
        self.assertIsNone(executor.native_meta.observation)

    def test_observe_records_get_git_diff_trace_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("fix the bug")
        tools = [e["tool"] for e in executor.native_meta.tool_trace]
        self.assertIn("get_git_diff", tools)

    def test_observe_get_git_diff_trace_has_no_raw_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("fix the bug")
        diff_entry = next(e for e in executor.native_meta.tool_trace if e["tool"] == "get_git_diff")
        self.assertNotIn("output", diff_entry)
        self.assertIn("output_chars", diff_entry)

    def test_observe_runs_search_repo_for_where_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.py").write_text("DATABASE_URL = 'sqlite:///db'\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("where is the database config")
        tools = [e["tool"] for e in executor.native_meta.tool_trace]
        self.assertIn("search_repo", tools)

    def test_observe_runs_search_repo_for_find_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("find the login function")
        tools = [e["tool"] for e in executor.native_meta.tool_trace]
        self.assertIn("search_repo", tools)

    def test_observe_runs_search_repo_for_search_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("search for payment handler")
        tools = [e["tool"] for e in executor.native_meta.tool_trace]
        self.assertIn("search_repo", tools)

    def test_observe_runs_search_repo_for_locate_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("locate error handling code")
        tools = [e["tool"] for e in executor.native_meta.tool_trace]
        self.assertIn("search_repo", tools)

    def test_observe_does_not_run_search_repo_for_non_search_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("fix the bug in auth.py")
        tools = [e["tool"] for e in executor.native_meta.tool_trace]
        self.assertNotIn("search_repo", tools)

    def test_observe_does_not_run_search_repo_for_refactor_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("refactor the database module")
        tools = [e["tool"] for e in executor.native_meta.tool_trace]
        self.assertNotIn("search_repo", tools)

    def test_observe_does_not_run_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("fix the bug")
        tools = [e["tool"] for e in executor.native_meta.tool_trace]
        self.assertNotIn("run_verification", tools)

    def test_observe_stores_native_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("fix the bug")
        self.assertIsInstance(executor.native_meta.observation, NativeObservation)

    def test_observe_observation_observed_tools_contains_get_git_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("fix the bug")
        self.assertIn("get_git_diff", executor.native_meta.observation.observed_tools)

    def test_observe_dirty_diff_present_true_when_diff_has_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._run_git(root, "init")
            self._run_git(root, "config", "user.email", "test@test.com")
            self._run_git(root, "config", "user.name", "Test")
            (root / "hello.txt").write_text("original\n")
            self._run_git(root, "add", "hello.txt")
            (root / "hello.txt").write_text("modified\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("fix the bug")
        self.assertTrue(executor.native_meta.observation.dirty_diff_present)

    def test_observe_dirty_diff_present_false_for_non_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("fix the bug")
        self.assertFalse(executor.native_meta.observation.dirty_diff_present)

    def test_observe_search_matches_count_reflects_actual_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def login(): pass\ndef logout(): pass\n")
            executor = self._make_executor_with_repo(root)
            # "find login" → query = "login", which matches the file content
            executor._run_observe_phase("find login")
        self.assertGreater(executor.native_meta.observation.search_matches_count, 0)

    def test_observe_search_matches_count_zero_for_non_search_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("fix the bug")
        self.assertEqual(executor.native_meta.observation.search_matches_count, 0)

    def test_generate_still_delegates_to_execution_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_gen = _make_generator_mock()
            with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
                executor = NativeAgentExecutor(provider=MagicMock(), repo_root=root)
            result = executor.generate("fix the bug")
        fake_gen.generate.assert_called_once()
        self.assertIs(result, fake_gen.generate.return_value)

    def test_generate_sets_observation_after_observe_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            fake_gen = _make_generator_mock()
            with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
                executor = NativeAgentExecutor(provider=MagicMock(), repo_root=root)
            executor.generate("fix the bug")
        self.assertIsInstance(executor.native_meta.observation, NativeObservation)

    def test_generate_observation_is_none_without_repo_root(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        executor.generate("fix the bug")
        self.assertIsNone(executor.native_meta.observation)

    def test_observe_search_repo_trace_has_no_raw_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("find the login function")
        search_entry = next(e for e in executor.native_meta.tool_trace if e["tool"] == "search_repo")
        self.assertNotIn("output", search_entry)
        self.assertIn("output_chars", search_entry)


class TestExtractSearchQuery(unittest.TestCase):
    """Unit tests for the _extract_search_query() helper."""

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(_extract_search_query(""))

    def test_returns_none_when_only_stop_and_trigger_words(self):
        self.assertIsNone(_extract_search_query("find the a"))

    def test_returns_none_when_remaining_words_too_short(self):
        self.assertIsNone(_extract_search_query("find is db"))

    def test_basic_extraction(self):
        result = _extract_search_query("find the login function")
        self.assertIsNotNone(result)
        self.assertIn("login", result)

    def test_trigger_word_removed(self):
        result = _extract_search_query("where is the config file")
        self.assertIsNotNone(result)
        self.assertNotIn("where", result.split())

    def test_stop_words_removed(self):
        result = _extract_search_query("search for the payment handler")
        self.assertIsNotNone(result)
        self.assertNotIn("the", result.split())
        self.assertNotIn("for", result.split())

    def test_takes_at_most_three_words(self):
        result = _extract_search_query("find login authentication middleware service")
        self.assertIsNotNone(result)
        self.assertLessEqual(len(result.split()), 3)

    def test_minimum_word_length_filter(self):
        result = _extract_search_query("find db api config")
        self.assertIsNotNone(result)
        self.assertNotIn("db", result.split())
        self.assertIn("api", result.split())

    def test_case_insensitive(self):
        result = _extract_search_query("Find THE Config File")
        self.assertIsNotNone(result)
        self.assertEqual(result, result.lower())

    def test_punctuation_stripped(self):
        result = _extract_search_query("where is config?")
        self.assertIsNotNone(result)
        self.assertNotIn("config?", result.split())
        self.assertIn("config", result.split())

    def test_punctuation_stripped_various(self):
        result = _extract_search_query("find (login) function.")
        self.assertIsNotNone(result)
        for word in result.split():
            self.assertFalse(any(c in word for c in ".,:;!?()[]{}\"'`"))


class TestRenderNativeObservation(unittest.TestCase):
    """Unit tests for render_native_observation()."""

    def _obs(self, **kwargs) -> NativeObservation:
        defaults = dict(
            observed_tools=[],
            dirty_diff_present=False,
            search_matches_count=0,
            verification_available=False,
            warnings=[],
        )
        defaults.update(kwargs)
        return NativeObservation(**defaults)

    def test_starts_with_observation_header(self):
        result = render_native_observation(self._obs())
        self.assertEqual(result.splitlines()[0], "[observation]")

    def test_includes_tools(self):
        result = render_native_observation(self._obs(observed_tools=["get_git_diff"]))
        self.assertIn("tools: get_git_diff", result)

    def test_no_tools_line_when_empty(self):
        result = render_native_observation(self._obs(observed_tools=[]))
        self.assertNotIn("tools:", result)

    def test_dirty_diff_yes(self):
        result = render_native_observation(self._obs(dirty_diff_present=True))
        self.assertIn("dirty diff: yes", result)

    def test_dirty_diff_no(self):
        result = render_native_observation(self._obs(dirty_diff_present=False))
        self.assertIn("dirty diff: no", result)

    def test_search_matches(self):
        result = render_native_observation(self._obs(search_matches_count=3))
        self.assertIn("search matches: 3", result)

    def test_verification_available_yes(self):
        result = render_native_observation(self._obs(verification_available=True))
        self.assertIn("verification available: yes", result)

    def test_verification_available_no(self):
        result = render_native_observation(self._obs(verification_available=False))
        self.assertIn("verification available: no", result)

    def test_warnings_capped_at_3(self):
        obs = self._obs(warnings=["w1", "w2", "w3", "w4", "w5"])
        result = render_native_observation(obs)
        self.assertIn("w1", result)
        self.assertIn("w3", result)
        self.assertNotIn("w4", result)
        self.assertNotIn("w5", result)

    def test_no_warnings_line_when_empty(self):
        result = render_native_observation(self._obs(warnings=[]))
        self.assertNotIn("warnings:", result)

    def test_bounded_by_limit(self):
        obs = self._obs(observed_tools=["get_git_diff", "search_repo"], warnings=["x" * 200])
        result = render_native_observation(obs, limit=50)
        self.assertLessEqual(len(result), 50)

    def test_truncation_marker_appears(self):
        obs = self._obs(observed_tools=["get_git_diff"], warnings=["x" * 200])
        result = render_native_observation(obs, limit=50)
        self.assertIn("[truncated]", result)

    def test_no_truncation_marker_when_within_limit(self):
        result = render_native_observation(self._obs())
        self.assertNotIn("[truncated]", result)

    def test_empty_observation_renders_minimal_block(self):
        result = render_native_observation(self._obs())
        self.assertIn("[observation]", result)
        self.assertIn("dirty diff: no", result)
        self.assertIn("search matches: 0", result)


class TestReadSearchLoop(unittest.TestCase):
    """Tests for NativeAgentExecutor._run_read_search_loop()."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor

    def test_returns_early_without_runner(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=Path("."))
        executor._runner = None
        executor._run_read_search_loop("fix the failing test")
        self.assertEqual(executor.native_meta.read_search_findings, [])
        self.assertNotIn("read_search", executor.native_meta.native_loop_steps)

    def test_no_runner_guard_prevents_read_search_step(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=Path("."))
        executor._runner = None
        executor._run_read_search_loop("add a cli argument")
        self.assertNotIn("read_search", executor.native_meta.native_loop_steps)
        self.assertEqual(executor.native_meta.read_search_findings, [])

    def test_loop_step_recorded_with_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_read_search_loop("fix the bug")
        self.assertIn("read_search", executor.native_meta.native_loop_steps)

    def test_strategy_test_for_test_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_read_search_loop("fix the failing test")
        event = next(e for e in executor.native_meta.native_loop_trace.events if e.phase == "read_search")
        self.assertEqual(event.metadata["strategy"], "test")

    def test_strategy_test_for_pytest_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_read_search_loop("run pytest suite")
        event = next(e for e in executor.native_meta.native_loop_trace.events if e.phase == "read_search")
        self.assertEqual(event.metadata["strategy"], "test")

    def test_strategy_cli_for_cli_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_read_search_loop("add a cli argument")
        event = next(e for e in executor.native_meta.native_loop_trace.events if e.phase == "read_search")
        self.assertEqual(event.metadata["strategy"], "cli")

    def test_strategy_default_for_generic_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_read_search_loop("refactor the payment module")
        event = next(e for e in executor.native_meta.native_loop_trace.events if e.phase == "read_search")
        self.assertEqual(event.metadata["strategy"], "default")

    def test_findings_bounded_by_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            for i in range(10):
                (root / "tests" / f"test_mod{i}.py").write_text(f"def test_{i}(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_preflight()
            executor._run_read_search_loop("fix the failing test")
        self.assertLessEqual(len(executor.native_meta.read_search_findings), 5)

    def test_steps_bounded_by_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor = self._make_executor_with_repo(root)
            executor._run_preflight()
            executor._run_read_search_loop("fix the bug")
        event = next(e for e in executor.native_meta.native_loop_trace.events if e.phase == "read_search")
        self.assertLessEqual(event.metadata["steps"], 3)

    def test_findings_contain_test_marker_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_auth.py").write_text("def test_login(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_preflight()
            executor._run_read_search_loop("fix the failing test")
        any_test_marker = any(
            f.startswith("test-marker:") for f in executor.native_meta.read_search_findings
        )
        self.assertTrue(any_test_marker)

    def test_no_raw_content_in_trace_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("SECRET_KEY = 'abc123'\n")
            executor = self._make_executor_with_repo(root)
            executor._run_preflight()
            executor._run_read_search_loop("fix the bug")
        event = next(e for e in executor.native_meta.native_loop_trace.events if e.phase == "read_search")
        meta_str = str(event.metadata)
        self.assertNotIn("SECRET_KEY", meta_str)
        self.assertNotIn("abc123", meta_str)

    def test_generate_calls_read_search_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            fake_gen = _make_generator_mock()
            with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
                executor = NativeAgentExecutor(provider=MagicMock(), repo_root=root)
            executor.generate("fix the bug")
        self.assertIn("read_search", executor.native_meta.native_loop_steps)

    def test_read_search_findings_default_empty(self):
        meta = NativeRunMeta()
        self.assertEqual(meta.read_search_findings, [])

    def test_truncated_flag_set_when_findings_overflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            for i in range(20):
                (root / "tests" / f"test_file{i}.py").write_text(f"def test_{i}(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_preflight()
            executor._run_read_search_loop("fix the failing test")
        event = next(e for e in executor.native_meta.native_loop_trace.events if e.phase == "read_search")
        if len(executor.native_meta.read_search_findings) == 5:
            self.assertIn("truncated", event.metadata)

    def test_metadata_contains_counts_not_raw_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_read_search_loop("fix the bug")
        event = next(e for e in executor.native_meta.native_loop_trace.events if e.phase == "read_search")
        self.assertIn("steps", event.metadata)
        self.assertIn("findings", event.metadata)
        self.assertIn("files_checked", event.metadata)
        self.assertIn("matched_terms", event.metadata)
        self.assertIn("truncated", event.metadata)
        self.assertIn("strategy", event.metadata)
        self.assertIsInstance(event.metadata["steps"], int)
        self.assertIsInstance(event.metadata["findings"], int)
        self.assertIsInstance(event.metadata["files_checked"], int)
        self.assertIsInstance(event.metadata["matched_terms"], int)
        self.assertIsInstance(event.metadata["truncated"], bool)
        self.assertIsInstance(event.metadata["strategy"], str)


class TestNativeObservationInjection(unittest.TestCase):
    """Tests that render_native_observation output is injected into generate()."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor, fake_gen

    def _make_executor_no_repo(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor, fake_gen

    def test_observation_injected_with_repo_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        _, kwargs = fake_gen.generate.call_args
        self.assertIn("[observation]", kwargs["skills_context"])

    def test_context_order_repo_then_observation_then_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("fix the bug", skills_context="user-skills")
        _, kwargs = fake_gen.generate.call_args
        ctx = kwargs["skills_context"]
        repo_pos = ctx.index("[repo context]")
        obs_pos = ctx.index("[observation]")
        skills_pos = ctx.index("user-skills")
        self.assertLess(repo_pos, obs_pos)
        self.assertLess(obs_pos, skills_pos)

    def test_no_observation_without_repo_root(self):
        executor, fake_gen = self._make_executor_no_repo()
        executor.generate("fix the bug", skills_context="hint")
        _, kwargs = fake_gen.generate.call_args
        self.assertNotIn("[observation]", kwargs["skills_context"])

    def test_raw_diff_not_in_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        _, kwargs = fake_gen.generate.call_args
        ctx = kwargs["skills_context"]
        self.assertNotIn("diff --git", ctx)
        self.assertNotIn("@@", ctx)

    def test_raw_search_not_in_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.py").write_text("DATABASE_URL = 'sqlite:///db'\n")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("where is the database config")
        _, kwargs = fake_gen.generate.call_args
        ctx = kwargs["skills_context"]
        self.assertNotIn("DATABASE_URL", ctx)

    def test_context_budget_populated_with_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor, _ = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        self.assertIsNotNone(executor.native_meta.context_budget)
        self.assertGreater(executor.native_meta.context_budget.estimated_tokens_used, 0)
        self.assertIsNotNone(executor.native_meta.observation)


class TestRenderNativeEvidence(unittest.TestCase):
    """Unit tests for render_native_evidence()."""

    def _ev(self, **kwargs) -> NativeEvidence:
        defaults = dict(search_results=[], truncated=False)
        defaults.update(kwargs)
        return NativeEvidence(**defaults)

    def test_starts_with_evidence_header(self):
        result = render_native_evidence(self._ev())
        self.assertEqual(result.splitlines()[0], "[evidence]")

    def test_includes_search_results(self):
        result = render_native_evidence(self._ev(search_results=["src/auth.py:12: def login_user(*)"]))
        self.assertIn("- src/auth.py:12: def login_user(*)", result)
        self.assertIn("search:", result)

    def test_caps_search_results_at_3(self):
        items = [f"file.py:{i}: line" for i in range(5)]
        result = render_native_evidence(self._ev(search_results=items))
        self.assertIn("file.py:0:", result)
        self.assertIn("file.py:2:", result)
        self.assertNotIn("file.py:3:", result)
        self.assertNotIn("file.py:4:", result)

    def test_truncated_flag_adds_marker(self):
        result = render_native_evidence(self._ev(truncated=True))
        self.assertIn("[truncated]", result)

    def test_no_truncated_marker_when_false(self):
        result = render_native_evidence(self._ev(truncated=False))
        self.assertNotIn("[truncated]", result)

    def test_bounded_by_limit(self):
        items = ["x" * 100 for _ in range(3)]
        result = render_native_evidence(self._ev(search_results=items), limit=50)
        self.assertLessEqual(len(result), 50)

    def test_truncation_suffix_on_char_overflow(self):
        items = ["x" * 100 for _ in range(3)]
        result = render_native_evidence(self._ev(search_results=items), limit=50)
        self.assertIn("[truncated]", result)

    def test_empty_evidence_renders_header_only(self):
        result = render_native_evidence(self._ev())
        self.assertIn("[evidence]", result)
        self.assertNotIn("search:", result)


class TestNativeEvidenceObservePhase(unittest.TestCase):
    """Tests for evidence population in _run_observe_phase()."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor

    def _make_executor_no_repo(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor

    def test_observe_stores_evidence_for_search_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login_user(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("where is the login function")
        self.assertIsInstance(executor.native_meta.evidence, NativeEvidence)

    def test_observe_evidence_none_for_non_search_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("fix the bug")
        self.assertIsNone(executor.native_meta.evidence)

    def test_observe_evidence_has_at_most_3_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(10):
                (root / f"file{i}.py").write_text(f"def func_{i}(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("where is the func")
        if executor.native_meta.evidence is not None:
            self.assertLessEqual(len(executor.native_meta.evidence.search_results), 3)

    def test_observe_evidence_does_not_include_diff_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("where is the login")
        if executor.native_meta.evidence is not None:
            for line in executor.native_meta.evidence.search_results:
                self.assertNotIn("diff --git", line)
                self.assertNotIn("@@", line)

    def test_observe_evidence_truncated_when_more_than_3_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(10):
                (root / f"mod{i}.py").write_text(f"def target_func_{i}(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("where is the target func")
        if executor.native_meta.evidence is not None and executor.native_meta.observation is not None:
            if executor.native_meta.observation.search_matches_count > 3:
                self.assertTrue(executor.native_meta.evidence.truncated)


class TestNativeEvidenceInjection(unittest.TestCase):
    """Tests that evidence is injected into generate() context."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor, fake_gen

    def _make_executor_no_repo(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor, fake_gen

    def test_evidence_injected_for_search_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login_user(): pass\n")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("where is the login function")
        _, kwargs = fake_gen.generate.call_args
        self.assertIn("[evidence]", kwargs["skills_context"])

    def test_evidence_not_injected_for_non_search_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        _, kwargs = fake_gen.generate.call_args
        self.assertNotIn("[evidence]", kwargs["skills_context"])

    def test_context_order_repo_observation_evidence_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login_user(): pass\n")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("where is the login function", skills_context="user-skills")
        _, kwargs = fake_gen.generate.call_args
        ctx = kwargs["skills_context"]
        repo_pos = ctx.index("[repo context]")
        obs_pos = ctx.index("[observation]")
        ev_pos = ctx.index("[evidence]")
        skills_pos = ctx.index("user-skills")
        self.assertLess(repo_pos, obs_pos)
        self.assertLess(obs_pos, ev_pos)
        self.assertLess(ev_pos, skills_pos)

    def test_no_evidence_without_repo_root(self):
        executor, fake_gen = self._make_executor_no_repo()
        executor.generate("where is the login", skills_context="hint")
        _, kwargs = fake_gen.generate.call_args
        self.assertNotIn("[evidence]", kwargs["skills_context"])

    def test_token_estimate_includes_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login_user(): pass\n")
            executor, _ = self._make_executor_with_repo(root)
            executor.generate("where is the login function")
        self.assertIsNotNone(executor.native_meta.context_budget)
        self.assertGreater(executor.native_meta.context_budget.estimated_tokens_used, 0)
        self.assertIsNotNone(executor.native_meta.evidence)


class TestRenderNativePlan(unittest.TestCase):
    """Tests for render_native_plan()."""

    def test_renders_header(self):
        plan = NativePlan()
        self.assertIn("[native plan]", render_native_plan(plan))

    def test_renders_intent(self):
        plan = NativePlan(intent="search")
        self.assertIn("intent: search", render_native_plan(plan))

    def test_renders_risk(self):
        plan = NativePlan(risk="medium")
        self.assertIn("risk: medium", render_native_plan(plan))

    def test_renders_suggested_steps(self):
        plan = NativePlan(suggested_steps=["step one", "step two"])
        rendered = render_native_plan(plan)
        self.assertIn("suggested steps:", rendered)
        self.assertIn("- step one", rendered)
        self.assertIn("- step two", rendered)

    def test_caps_steps_at_five(self):
        plan = NativePlan(suggested_steps=[f"step {i}" for i in range(10)])
        rendered = render_native_plan(plan)
        self.assertIn("- step 4", rendered)
        self.assertNotIn("- step 5", rendered)

    def test_renders_warnings(self):
        plan = NativePlan(warnings=["watch out"])
        rendered = render_native_plan(plan)
        self.assertIn("warnings:", rendered)
        self.assertIn("- watch out", rendered)

    def test_caps_warnings_at_three(self):
        plan = NativePlan(warnings=[f"warn {i}" for i in range(5)])
        rendered = render_native_plan(plan)
        self.assertIn("- warn 2", rendered)
        self.assertNotIn("- warn 3", rendered)

    def test_respects_limit(self):
        plan = NativePlan(suggested_steps=["x" * 200] * 5)
        rendered = render_native_plan(plan, limit=100)
        self.assertLessEqual(len(rendered), 100)

    def test_truncation_marker_when_limit_exceeded(self):
        plan = NativePlan(suggested_steps=["x" * 200] * 5)
        rendered = render_native_plan(plan, limit=100)
        self.assertIn("[truncated]", rendered)

    def test_no_truncation_when_within_limit(self):
        plan = NativePlan(intent="standard", risk="low")
        rendered = render_native_plan(plan, limit=600)
        self.assertNotIn("[truncated]", rendered)


class TestBuildNativePlan(unittest.TestCase):
    """Tests for _build_native_plan()."""

    def test_search_intent(self):
        for word in ("search", "find", "where", "locate"):
            with self.subTest(word=word):
                plan = _build_native_plan(f"please {word} the module")
                self.assertEqual(plan.intent, "search")

    def test_debug_intent(self):
        for word in ("debug", "fix", "error", "fail", "verify", "test"):
            with self.subTest(word=word):
                plan = _build_native_plan(f"please {word} this")
                self.assertEqual(plan.intent, "debug")

    def test_refactor_intent(self):
        plan = _build_native_plan("refactor the auth module")
        self.assertEqual(plan.intent, "refactor")

    def test_implementation_intent(self):
        for word in ("add", "create", "implement", "build"):
            with self.subTest(word=word):
                plan = _build_native_plan(f"please {word} a feature")
                self.assertEqual(plan.intent, "implementation")

    def test_standard_intent_default(self):
        plan = _build_native_plan("do something useful")
        self.assertEqual(plan.intent, "standard")

    def test_word_boundary_matching(self):
        # "searchable" should NOT match "search"
        plan = _build_native_plan("make it searchable")
        self.assertNotEqual(plan.intent, "search")

    def test_security_skill_sets_medium_risk(self):
        plan = _build_native_plan("update the config", selected_skills=["security-sensitive-change"])
        self.assertEqual(plan.risk, "medium")

    def test_security_skill_adds_warning(self):
        plan = _build_native_plan("update the config", selected_skills=["security-sensitive-change"])
        self.assertIn("security-sensitive task", plan.warnings)

    def test_dirty_diff_sets_medium_risk(self):
        obs = NativeObservation(dirty_diff_present=True)
        plan = _build_native_plan("update the config", observation=obs)
        self.assertEqual(plan.risk, "medium")

    def test_dirty_diff_adds_warning(self):
        obs = NativeObservation(dirty_diff_present=True)
        plan = _build_native_plan("update the config", observation=obs)
        self.assertIn("dirty working tree detected", plan.warnings)

    def test_clean_diff_is_low_risk(self):
        obs = NativeObservation(dirty_diff_present=False)
        plan = _build_native_plan("update the config", observation=obs)
        self.assertEqual(plan.risk, "low")

    def test_evidence_prepends_step(self):
        ev = NativeEvidence(search_results=["some result"])
        plan = _build_native_plan("find the thing", evidence=ev)
        self.assertEqual(plan.suggested_steps[0], "use bounded search evidence to choose files")

    def test_no_evidence_no_prepend(self):
        plan = _build_native_plan("find the thing", evidence=None)
        self.assertNotIn("use bounded search evidence to choose files", plan.suggested_steps)

    def test_debug_intent_appends_verification_step(self):
        plan = _build_native_plan("fix the bug")
        self.assertIn("run verification after changes", plan.suggested_steps)

    def test_non_debug_no_verification_step(self):
        plan = _build_native_plan("add a feature")
        self.assertNotIn("run verification after changes", plan.suggested_steps)

    def test_dirty_diff_appends_avoid_overwrite_step(self):
        obs = NativeObservation(dirty_diff_present=True)
        plan = _build_native_plan("update config", observation=obs)
        self.assertIn("avoid overwriting existing user changes", plan.suggested_steps)

    def test_always_includes_base_steps(self):
        plan = _build_native_plan("do something")
        self.assertIn("inspect relevant files", plan.suggested_steps)
        self.assertIn("make the smallest safe change", plan.suggested_steps)
        self.assertIn("review the diff", plan.suggested_steps)


class TestNativePlanInjection(unittest.TestCase):
    """Tests that native plan is injected into generate() context."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor, fake_gen

    def _make_executor_no_repo(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor, fake_gen

    def test_plan_injected_with_repo_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1\n")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        _, kwargs = fake_gen.generate.call_args
        self.assertIn("[native plan]", kwargs["skills_context"])

    def test_context_order_repo_observation_evidence_plan_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login_user(): pass\n")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("where is the login function", skills_context="user-skills")
        _, kwargs = fake_gen.generate.call_args
        ctx = kwargs["skills_context"]
        repo_pos = ctx.index("[repo context]")
        obs_pos = ctx.index("[observation]")
        ev_pos = ctx.index("[evidence]")
        plan_pos = ctx.index("[native plan]")
        skills_pos = ctx.index("user-skills")
        self.assertLess(repo_pos, obs_pos)
        self.assertLess(obs_pos, ev_pos)
        self.assertLess(ev_pos, plan_pos)
        self.assertLess(plan_pos, skills_pos)

    def test_no_repo_root_plan_present_others_absent(self):
        executor, fake_gen = self._make_executor_no_repo()
        executor.generate("fix the bug", skills_context="hint")
        _, kwargs = fake_gen.generate.call_args
        ctx = kwargs["skills_context"]
        self.assertIn("[native plan]", ctx)
        self.assertNotIn("[repo context]", ctx)
        self.assertNotIn("[observation]", ctx)
        self.assertNotIn("[evidence]", ctx)

    def test_plan_stored_on_native_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1\n")
            executor, _ = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        self.assertIsNotNone(executor.native_meta.plan)


class TestNativePlanMetaSerialization(unittest.TestCase):
    """Tests that plan is serialized correctly in native_meta."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor, fake_gen

    def test_plan_has_expected_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1\n")
            executor, _ = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        plan = executor.native_meta.plan
        self.assertIsNotNone(plan)
        self.assertIsInstance(plan.intent, str)
        self.assertIsInstance(plan.risk, str)
        self.assertIsInstance(plan.suggested_steps, list)
        self.assertIsInstance(plan.warnings, list)

    def test_plan_fields_contain_no_raw_tool_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1\n")
            executor, _ = self._make_executor_with_repo(root)
            executor.generate("fix the bug")
        plan = executor.native_meta.plan
        for step in plan.suggested_steps:
            self.assertNotIn("diff --git", step)
            self.assertNotIn("@@", step)
        for warning in plan.warnings:
            self.assertNotIn("diff --git", warning)

    def test_asdict_produces_expected_keys(self):
        from dataclasses import asdict
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1\n")
            executor, _ = self._make_executor_with_repo(root)
            executor.generate("add a feature")
        d = asdict(executor.native_meta.plan)
        self.assertIn("intent", d)
        self.assertIn("risk", d)
        self.assertIn("suggested_steps", d)
        self.assertIn("warnings", d)


class TestRenderNativeEvidenceSnippets(unittest.TestCase):
    """Tests for snippet rendering in render_native_evidence()."""

    def _ev(self, **kwargs):
        defaults = dict(search_results=[], file_snippets=[], truncated=False)
        defaults.update(kwargs)
        return NativeEvidence(**defaults)

    def test_snippets_section_rendered_when_present(self):
        snippet = NativeFileSnippet(path="src/auth.py", lines=["12: def login_user():"])
        result = render_native_evidence(self._ev(file_snippets=[snippet]))
        self.assertIn("snippets:", result)
        self.assertIn("src/auth.py:", result)
        self.assertIn("12: def login_user():", result)

    def test_no_snippets_section_when_empty(self):
        result = render_native_evidence(self._ev(file_snippets=[]))
        self.assertNotIn("snippets:", result)

    def test_capped_at_2_files(self):
        snippets = [
            NativeFileSnippet(path=f"file{i}.py", lines=[f"{i}: line"])
            for i in range(4)
        ]
        result = render_native_evidence(self._ev(file_snippets=snippets))
        self.assertIn("file0.py:", result)
        self.assertIn("file1.py:", result)
        self.assertNotIn("file2.py:", result)
        self.assertNotIn("file3.py:", result)

    def test_snippet_lines_capped_at_8(self):
        lines = [f"{i}: x = {i}" for i in range(12)]
        snippet = NativeFileSnippet(path="big.py", lines=lines)
        result = render_native_evidence(self._ev(file_snippets=[snippet]))
        self.assertIn("7: x = 7", result)
        self.assertNotIn("8: x = 8", result)

    def test_output_bounded_by_limit_with_snippets(self):
        lines = ["x" * 100 for _ in range(8)]
        snippet = NativeFileSnippet(path="big.py", lines=lines)
        result = render_native_evidence(self._ev(file_snippets=[snippet]), limit=100)
        self.assertLessEqual(len(result), 100)
        self.assertIn("[truncated]", result)

    def test_default_limit_is_1000(self):
        import inspect
        sig = inspect.signature(render_native_evidence)
        self.assertEqual(sig.parameters["limit"].default, 1000)


class TestSearchResultParsing(unittest.TestCase):
    """Unit tests for _parse_search_result_line() and _extract_snippet_lines()."""

    def test_valid_line_returns_path_and_lineno(self):
        result = _parse_search_result_line("src/auth.py:42:def login_user():")
        self.assertEqual(result, ("src/auth.py", 42))

    def test_two_part_line_returns_none(self):
        self.assertIsNone(_parse_search_result_line("src/auth.py:content"))

    def test_non_integer_lineno_returns_none(self):
        self.assertIsNone(_parse_search_result_line("src/auth.py:abc:content"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_search_result_line(""))

    def test_extract_snippet_includes_target_line(self):
        content = "\n".join(f"line {i}" for i in range(1, 11))
        lines = _extract_snippet_lines(content, lineno=5)
        joined = "\n".join(lines)
        self.assertIn("5: line 5", joined)

    def test_extract_snippet_lines_are_prefixed_with_line_numbers(self):
        content = "alpha\nbeta\ngamma\ndelta\nepsilon"
        lines = _extract_snippet_lines(content, lineno=3)
        for line in lines:
            parts = line.split(": ", 1)
            self.assertEqual(len(parts), 2)
            self.assertTrue(parts[0].isdigit())

    def test_extract_snippet_handles_first_line(self):
        content = "first\nsecond\nthird"
        lines = _extract_snippet_lines(content, lineno=1)
        self.assertTrue(len(lines) > 0)
        self.assertIn("1: first", lines[0])

    def test_extract_snippet_handles_last_line(self):
        content = "alpha\nbeta\ngamma"
        lines = _extract_snippet_lines(content, lineno=3)
        self.assertTrue(len(lines) > 0)
        joined = "\n".join(lines)
        self.assertIn("3: gamma", joined)

    def test_extract_snippet_empty_content_returns_empty(self):
        self.assertEqual(_extract_snippet_lines("", lineno=1), [])

    def test_extract_snippet_respects_max_lines(self):
        content = "\n".join(f"line {i}" for i in range(1, 20))
        lines = _extract_snippet_lines(content, lineno=10, radius=10, max_lines=4)
        self.assertLessEqual(len(lines), 4)


class TestFileSnippetsObservePhase(unittest.TestCase):
    """Tests for file_snippets population in _run_observe_phase()."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor

    def test_search_task_stores_file_snippets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login_user(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("where is the login function")
        self.assertIsNotNone(executor.native_meta.evidence)
        self.assertIsInstance(executor.native_meta.evidence.file_snippets, list)

    def test_at_most_2_files_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(5):
                (root / f"mod{i}.py").write_text(f"def login_{i}(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("where is the login function")
        if executor.native_meta.evidence is not None:
            self.assertLessEqual(len(executor.native_meta.evidence.file_snippets), 2)

    def test_read_file_traces_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login_user(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("where is the login function")
        read_traces = [t for t in executor.native_meta.tool_trace if t["tool"] == "read_file"]
        if executor.native_meta.evidence and executor.native_meta.evidence.file_snippets:
            self.assertGreater(len(read_traces), 0)

    def test_snippets_do_not_contain_full_file_content(self):
        big_content = "\n".join(f"line_{i} = {i}" for i in range(200))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "big.py").write_text(big_content)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("where is the line function")
        if executor.native_meta.evidence:
            for snippet in executor.native_meta.evidence.file_snippets:
                self.assertLessEqual(len(snippet.lines), 8)

    def test_non_search_task_has_no_file_snippets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("fix the bug")
        self.assertIsNone(executor.native_meta.evidence)

    def test_read_failure_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login_user(): pass\n")
            executor = self._make_executor_with_repo(root)
            original_run = executor._runner.run

            def patched_run(call):
                if call.tool_name == "read_file":
                    from openshard.native.tools import NativeToolResult
                    return NativeToolResult(tool_name="read_file", ok=False, error="read error")
                return original_run(call)

            executor._runner.run = patched_run
            executor._run_observe_phase("where is the login function")
        self.assertIsNotNone(executor.native_meta.evidence)
        self.assertEqual(executor.native_meta.evidence.file_snippets, [])


class TestFileSnippetsInjection(unittest.TestCase):
    """Tests that file snippets are injected into generate() context."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor, fake_gen

    def test_evidence_block_includes_snippets_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login_user(): pass\n")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("where is the login function")
        _, kwargs = fake_gen.generate.call_args
        ctx = kwargs["skills_context"]
        if executor.native_meta.evidence and executor.native_meta.evidence.file_snippets:
            self.assertIn("snippets:", ctx)
            self.assertIn("auth.py:", ctx)

    def test_context_order_unchanged_with_snippets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login_user(): pass\n")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("where is the login function", skills_context="user-skills")
        _, kwargs = fake_gen.generate.call_args
        ctx = kwargs["skills_context"]
        repo_pos = ctx.index("[repo context]")
        obs_pos = ctx.index("[observation]")
        ev_pos = ctx.index("[evidence]")
        plan_pos = ctx.index("[native plan]")
        skills_pos = ctx.index("user-skills")
        self.assertLess(repo_pos, obs_pos)
        self.assertLess(obs_pos, ev_pos)
        self.assertLess(ev_pos, plan_pos)
        self.assertLess(plan_pos, skills_pos)

    def test_no_raw_full_file_in_context(self):
        big_content = "\n".join(f"x_{i} = {i}" for i in range(200))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "big.py").write_text(big_content)
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("where is the x function")
        _, kwargs = fake_gen.generate.call_args
        ctx = kwargs["skills_context"]
        self.assertNotIn("x_100 = 100", ctx)


class TestNativeEvidenceMetaSerialization(unittest.TestCase):
    """Tests that NativeEvidence serializes correctly with file_snippets."""

    def test_asdict_includes_file_snippets(self):
        from dataclasses import asdict
        snippet = NativeFileSnippet(path="src/auth.py", lines=["1: def f():"])
        ev = NativeEvidence(search_results=["src/auth.py:1: def f():"], file_snippets=[snippet])
        d = asdict(ev)
        self.assertIn("file_snippets", d)
        self.assertEqual(len(d["file_snippets"]), 1)
        self.assertEqual(d["file_snippets"][0]["path"], "src/auth.py")
        self.assertEqual(d["file_snippets"][0]["lines"], ["1: def f():"])

    def test_asdict_empty_file_snippets(self):
        from dataclasses import asdict
        ev = NativeEvidence()
        d = asdict(ev)
        self.assertIn("file_snippets", d)
        self.assertEqual(d["file_snippets"], [])


_SAMPLE_DIFF = """\
diff --git a/foo.py b/foo.py
index 1234567..abcdefg 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 x = 1
-y = 2
+y = 3
+z = 4
"""


class TestNativeDiffReviewBuilder(unittest.TestCase):
    """Unit tests for build_native_diff_review()."""

    def test_empty_diff_has_no_diff(self):
        review = build_native_diff_review("")
        self.assertFalse(review.has_diff)

    def test_has_diff_true_for_nonempty_output(self):
        review = build_native_diff_review(_SAMPLE_DIFF)
        self.assertTrue(review.has_diff)

    def test_detects_changed_file_from_diff_git_line(self):
        review = build_native_diff_review(_SAMPLE_DIFF)
        self.assertIn("foo.py", review.changed_files)

    def test_detects_changed_file_from_plus_header(self):
        diff = "+++ b/bar.py\n--- a/bar.py\n+added\n"
        review = build_native_diff_review(diff)
        self.assertIn("bar.py", review.changed_files)

    def test_counts_added_lines(self):
        review = build_native_diff_review(_SAMPLE_DIFF)
        self.assertEqual(review.added_lines, 2)

    def test_counts_removed_lines(self):
        review = build_native_diff_review(_SAMPLE_DIFF)
        self.assertEqual(review.removed_lines, 1)

    def test_does_not_count_diff_headers_as_added_removed(self):
        review = build_native_diff_review(_SAMPLE_DIFF)
        # +++ and --- lines must not be counted
        self.assertEqual(review.added_lines, 2)
        self.assertEqual(review.removed_lines, 1)

    def test_caps_changed_files_at_20(self):
        lines = []
        for i in range(25):
            lines.append(f"diff --git a/file{i}.py b/file{i}.py")
        review = build_native_diff_review("\n".join(lines))
        self.assertEqual(len(review.changed_files), 20)

    def test_sets_output_chars(self):
        review = build_native_diff_review(_SAMPLE_DIFF)
        self.assertEqual(review.output_chars, len(_SAMPLE_DIFF))

    def test_sets_truncated_flag(self):
        review = build_native_diff_review(_SAMPLE_DIFF, truncated=True)
        self.assertTrue(review.truncated)

    def test_truncated_false_by_default(self):
        review = build_native_diff_review(_SAMPLE_DIFF)
        self.assertFalse(review.truncated)

    def test_empty_diff_has_zero_lines(self):
        review = build_native_diff_review("")
        self.assertEqual(review.added_lines, 0)
        self.assertEqual(review.removed_lines, 0)
        self.assertEqual(review.changed_files, [])

    def test_changed_files_sorted(self):
        diff = "diff --git a/z.py b/z.py\ndiff --git a/a.py b/a.py\n"
        review = build_native_diff_review(diff)
        self.assertEqual(review.changed_files, sorted(review.changed_files))


class TestNativeDiffReviewExecutor(unittest.TestCase):
    """Tests for NativeAgentExecutor.review_diff()."""

    def _run_git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor

    def _make_executor_no_repo(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor

    def test_review_diff_returns_none_without_runner(self):
        executor = self._make_executor_no_repo()
        result = executor.review_diff()
        self.assertIsNone(result)

    def test_review_diff_returns_none_for_non_git_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            result = executor.review_diff()
        # get_git_diff returns ok=False outside a git repo
        self.assertIsNone(result)

    def test_review_diff_stores_review_on_native_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._run_git(root, "init")
            self._run_git(root, "config", "user.email", "test@test.com")
            self._run_git(root, "config", "user.name", "Test")
            (root / "hello.txt").write_text("original\n")
            self._run_git(root, "add", "hello.txt")
            (root / "hello.txt").write_text("modified\n")
            executor = self._make_executor_with_repo(root)
            result = executor.review_diff()
        self.assertIsNotNone(result)
        self.assertIsInstance(executor.native_meta.diff_review, NativeDiffReview)
        self.assertTrue(executor.native_meta.diff_review.has_diff)

    def test_review_diff_detects_changed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._run_git(root, "init")
            self._run_git(root, "config", "user.email", "test@test.com")
            self._run_git(root, "config", "user.name", "Test")
            (root / "hello.txt").write_text("original\n")
            self._run_git(root, "add", "hello.txt")
            (root / "hello.txt").write_text("modified\n")
            executor = self._make_executor_with_repo(root)
            executor.review_diff()
        self.assertIsNotNone(executor.native_meta.diff_review)
        self.assertIn("hello.txt", executor.native_meta.diff_review.changed_files)

    def test_review_diff_appends_tool_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            before = len(executor.native_meta.tool_trace)
            executor.review_diff()
        self.assertEqual(len(executor.native_meta.tool_trace), before + 1)
        self.assertEqual(executor.native_meta.tool_trace[-1]["tool"], "get_git_diff")

    def test_review_diff_trace_has_no_raw_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor.review_diff()
        entry = executor.native_meta.tool_trace[-1]
        self.assertNotIn("output", entry)
        self.assertIn("output_chars", entry)

    def test_review_diff_returns_none_on_git_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            from openshard.native.tools import NativeToolResult
            original_run = executor._runner.run

            def patched_run(call):
                if call.tool_name == "get_git_diff":
                    return NativeToolResult(tool_name="get_git_diff", ok=False, error="git error")
                return original_run(call)

            executor._runner.run = patched_run
            result = executor.review_diff()
        self.assertIsNone(result)
        self.assertIsNone(executor.native_meta.diff_review)

    def test_review_diff_does_not_store_diff_review_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            from openshard.native.tools import NativeToolResult

            def patched_run(call):
                if call.tool_name == "get_git_diff":
                    return NativeToolResult(tool_name="get_git_diff", ok=False, error="fail")
                return executor._runner.__class__.run(executor._runner, call)

            executor._runner.run = patched_run
            executor.review_diff()
        self.assertIsNone(executor.native_meta.diff_review)


class TestNativeDiffReviewPipeline(unittest.TestCase):
    """Pipeline wiring tests for review_diff()."""

    def _run(self, args, native_mock=None):
        native_mock = native_mock or _make_native_mock()
        generator_mock = _make_generator_mock()
        manager_mock = _make_manager_mock()
        logged = {}

        def _capture_log(*a, **kw):
            logged.update(kw.get("extra_metadata") or {})

        with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock), \
             patch("openshard.run.pipeline.ProviderManager", return_value=manager_mock), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run", side_effect=_capture_log):
            runner = CliRunner()
            result = runner.invoke(cli, ["run"] + args)
        return result, native_mock, logged

    def test_native_dry_run_does_not_call_review_diff(self):
        result, native_mock, _ = self._run(["--workflow", "native", "--dry-run", "add a feature"])
        self.assertEqual(result.exit_code, 0, result.output)
        native_mock.review_diff.assert_not_called()

    def test_native_log_contains_diff_review_key(self):
        result, _, logged = self._run(["--workflow", "native", "add a feature"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("diff_review", logged)


def _safe_cmd():
    from openshard.verification.plan import (
        CommandSafety,
        VerificationCommand,
        VerificationKind,
        VerificationSource,
    )
    return VerificationCommand(
        name="pytest",
        argv=["python", "-m", "pytest"],
        kind=VerificationKind.test,
        source=VerificationSource.config,
        safety=CommandSafety.safe,
        reason="safe test runner",
    )


def _unsafe_cmd(safety_label):
    from openshard.verification.plan import (
        CommandSafety,
        VerificationCommand,
        VerificationKind,
        VerificationSource,
    )
    return VerificationCommand(
        name="make",
        argv=["make", "test"],
        kind=VerificationKind.test,
        source=VerificationSource.config,
        safety=CommandSafety(safety_label),
        reason="needs approval",
    )


def _safe_plan():
    from openshard.verification.plan import VerificationPlan
    return VerificationPlan(commands=[_safe_cmd()])


def _empty_plan():
    from openshard.verification.plan import VerificationPlan
    return VerificationPlan(commands=[])


def _mixed_plan():
    from openshard.verification.plan import VerificationPlan
    return VerificationPlan(commands=[_safe_cmd(), _unsafe_cmd("needs_approval")])


class TestNativeVerificationLoop(unittest.TestCase):
    """Tests for the native-only controlled verification loop in RunPipeline."""

    def _run_write_simple(self, native_mock, plan=None, verify_returns=None):
        from openshard.execution.generator import ChangedFile
        safe_file = ChangedFile(
            path="out.txt", content="ok", change_type="create", summary="created"
        )
        if not native_mock.generate.side_effect:
            r = MagicMock()
            r.files = [safe_file]
            r.summary = "done"
            r.notes = []
            r.usage = None
            native_mock.generate.return_value = r

        plan = plan if plan is not None else _safe_plan()
        verify_returns = verify_returns if verify_returns is not None else [(0, "")]
        logged = {}

        def _capture_log(*a, **kw):
            logged.update(kw.get("extra_metadata") or {})

        verify_iter = iter(verify_returns)

        def _verify_side(*a, **kw):
            return next(verify_iter)

        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run", side_effect=_capture_log), \
                 patch("openshard.run.pipeline.build_verification_plan", return_value=plan), \
                 patch("openshard.run.pipeline._run_verification_plan", side_effect=_verify_side), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                result = runner.invoke(cli, ["run", "--workflow", "native", "--write", "fix the bug"])
        return result, native_mock, logged

    def test_native_write_calls_review_diff_once(self):
        from openshard.execution.generator import ChangedFile
        safe_file = ChangedFile(
            path="out.txt", content="hello", change_type="create", summary="created"
        )
        fake_result = MagicMock()
        fake_result.files = [safe_file]
        fake_result.summary = "done"
        fake_result.notes = []
        fake_result.usage = None

        native_mock = _make_native_mock()
        native_mock.generate.return_value = fake_result

        with tempfile.TemporaryDirectory() as workspace_dir:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=workspace_dir):
                runner = CliRunner()
                result = runner.invoke(
                    cli, ["run", "--workflow", "native", "--write", "create file"]
                )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(native_mock.review_diff.call_count, 1)

    def test_native_retry_calls_review_diff_twice(self):
        from openshard.execution.generator import ChangedFile
        from openshard.verification.plan import (
            CommandSafety,
            VerificationCommand,
            VerificationKind,
            VerificationPlan,
            VerificationSource,
        )

        safe_file = ChangedFile(
            path="out.txt", content="hello", change_type="create", summary="created"
        )
        fake_result = MagicMock()
        fake_result.files = [safe_file]
        fake_result.summary = "done"
        fake_result.notes = []
        fake_result.usage = None

        native_mock = _make_native_mock()
        native_mock.generate.return_value = fake_result

        _fake_plan = VerificationPlan(commands=[
            VerificationCommand(
                name="pytest",
                argv=["python", "-m", "pytest"],
                kind=VerificationKind.test,
                source=VerificationSource.detected,
                safety=CommandSafety.safe,
                reason="detected",
            )
        ])

        # Verification sequence: initial fails (1), capture returns (1, ""), retry passes (0)
        verify_calls = [1, 1, "", 0]

        def _fake_verify_plan(plan, workspace, gate=None, capture=False, label="[verify]", detail="default", **_kw):
            if capture:
                return verify_calls.pop(0), verify_calls.pop(0)
            return verify_calls.pop(0)

        with tempfile.TemporaryDirectory() as workspace_dir:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.build_verification_plan", return_value=_fake_plan), \
                 patch("openshard.run.pipeline._run_verification_plan", side_effect=_fake_verify_plan), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=workspace_dir):
                runner = CliRunner()
                runner.invoke(
                    cli, ["run", "--workflow", "native", "--write", "--verify", "create file"]
                )
        # Two writes happened: initial + retry → two review_diff calls
        self.assertEqual(native_mock.review_diff.call_count, 2)

    def test_native_write_runs_safe_verification(self):
        native_mock = _make_native_mock()
        result, native_mock, _ = self._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "passed")]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        loop = native_mock.native_meta.verification_loop
        self.assertIsNotNone(loop)
        self.assertTrue(loop.attempted)
        self.assertTrue(loop.passed)
        self.assertFalse(loop.retried)
        self.assertEqual(loop.exit_code, 0)

    def test_native_write_retries_once_on_verification_failure(self):
        from openshard.execution.generator import ChangedFile

        first_result = MagicMock()
        first_result.files = [ChangedFile(path="out.txt", content="broken", change_type="create", summary="")]
        first_result.summary = "done"
        first_result.notes = []
        first_result.usage = None

        retry_result = MagicMock()
        retry_result.files = [ChangedFile(path="out.txt", content="fixed", change_type="create", summary="")]
        retry_result.summary = "done retry"
        retry_result.notes = []
        retry_result.usage = None

        native_mock = _make_native_mock()
        native_mock.generate.side_effect = [first_result, retry_result]

        result, native_mock, _ = self._run_write_simple(
            native_mock,
            plan=_safe_plan(),
            verify_returns=[(1, "test failed"), (0, "passed")],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(native_mock.generate.call_count, 2)
        loop = native_mock.native_meta.verification_loop
        self.assertIsNotNone(loop)
        self.assertTrue(loop.attempted)
        self.assertTrue(loop.retried)
        self.assertTrue(loop.passed)
        self.assertEqual(loop.exit_code, 0)

    def test_retry_context_contains_verification_failure_marker(self):
        from openshard.execution.generator import ChangedFile

        first_result = MagicMock()
        first_result.files = [ChangedFile(path="out.txt", content="x", change_type="create", summary="")]
        first_result.summary = "done"
        first_result.notes = []
        first_result.usage = None

        retry_result = MagicMock()
        retry_result.files = []
        retry_result.summary = "done"
        retry_result.notes = []
        retry_result.usage = None

        native_mock = _make_native_mock()
        native_mock.generate.side_effect = [first_result, retry_result]

        self._run_write_simple(
            native_mock,
            plan=_safe_plan(),
            verify_returns=[(1, "FAIL output"), (0, "")],
        )
        self.assertEqual(native_mock.generate.call_count, 2)
        _, kwargs = native_mock.generate.call_args_list[1]
        self.assertIn("[verification failure]", kwargs["skills_context"])

    def test_native_write_does_not_retry_when_verification_passes(self):
        native_mock = _make_native_mock()
        result, native_mock, _ = self._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(native_mock.generate.call_count, 1)
        loop = native_mock.native_meta.verification_loop
        self.assertFalse(loop.retried)

    def test_native_write_does_not_verify_without_command(self):
        native_mock = _make_native_mock()
        verify_mock = MagicMock(return_value=(0, ""))
        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline.build_verification_plan", return_value=_empty_plan()), \
                 patch("openshard.run.pipeline._run_verification_plan", verify_mock), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                runner.invoke(cli, ["run", "--workflow", "native", "--write", "fix the bug"])
        verify_mock.assert_not_called()
        loop = native_mock.native_meta.verification_loop
        self.assertIsNotNone(loop)
        self.assertFalse(loop.attempted)

    def test_non_native_write_does_not_call_review_diff(self):
        from openshard.execution.generator import ChangedFile
        safe_file = ChangedFile(
            path="out.txt", content="hello", change_type="create", summary="created"
        )
        fake_result = MagicMock()
        fake_result.files = [safe_file]
        fake_result.summary = "done"
        fake_result.notes = []
        fake_result.usage = None

        generator_mock = _make_generator_mock()
        generator_mock.generate.return_value = fake_result

        with tempfile.TemporaryDirectory() as workspace_dir:
            with patch("openshard.run.pipeline.NativeAgentExecutor") as native_cls, \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=workspace_dir):
                runner = CliRunner()
                runner.invoke(
                    cli, ["run", "--workflow", "direct", "--write", "create file"]
                )
        native_cls.assert_not_called()

    def test_native_write_does_not_verify_needs_approval(self):
        from openshard.verification.plan import VerificationPlan
        plan = VerificationPlan(commands=[_unsafe_cmd("needs_approval")])
        verify_mock = MagicMock(return_value=(0, ""))
        native_mock = _make_native_mock()
        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline.build_verification_plan", return_value=plan), \
                 patch("openshard.run.pipeline._run_verification_plan", verify_mock), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                runner.invoke(cli, ["run", "--workflow", "native", "--write", "fix the bug"])
        verify_mock.assert_not_called()

    def test_native_write_does_not_verify_blocked(self):
        from openshard.verification.plan import VerificationPlan
        plan = VerificationPlan(commands=[_unsafe_cmd("blocked")])
        verify_mock = MagicMock(return_value=(0, ""))
        native_mock = _make_native_mock()
        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline.build_verification_plan", return_value=plan), \
                 patch("openshard.run.pipeline._run_verification_plan", verify_mock), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                runner.invoke(cli, ["run", "--workflow", "native", "--write", "fix the bug"])
        verify_mock.assert_not_called()

    def test_native_write_does_not_verify_when_any_command_unsafe(self):
        verify_mock = MagicMock(return_value=(0, ""))
        native_mock = _make_native_mock()
        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline.build_verification_plan", return_value=_mixed_plan()), \
                 patch("openshard.run.pipeline._run_verification_plan", verify_mock), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                runner.invoke(cli, ["run", "--workflow", "native", "--write", "fix the bug"])
        verify_mock.assert_not_called()

    def test_native_write_does_not_verify_in_dry_run(self):
        verify_mock = MagicMock(return_value=(0, ""))
        native_mock = _make_native_mock()
        with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
             patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline.build_verification_plan", return_value=_safe_plan()), \
             patch("openshard.run.pipeline._run_verification_plan", verify_mock), \
             patch("openshard.run.pipeline._log_run"):
            runner = CliRunner()
            runner.invoke(cli, ["run", "--workflow", "native", "--dry-run", "fix the bug"])
        verify_mock.assert_not_called()

    def test_native_verification_loop_metadata_logged(self):
        native_mock = _make_native_mock()
        result, _, logged = self._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("verification_loop", logged)
        vl = logged["verification_loop"]
        self.assertIsNotNone(vl)
        self.assertIsInstance(vl, dict)
        for key in ("attempted", "passed", "retried", "exit_code", "output_chars", "truncated"):
            self.assertIn(key, vl)

    def test_native_verification_loop_metadata_none_when_no_commands(self):
        native_mock = _make_native_mock()
        result, _, logged = self._run_write_simple(
            native_mock, plan=_empty_plan(), verify_returns=[]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        vl = logged.get("verification_loop")
        if vl is not None:
            self.assertFalse(vl.get("attempted"))

    def test_verification_failure_context_is_bounded(self):
        large_output = "x" * 2000
        result = render_verification_failure_context(large_output, exit_code=1, limit=1200)
        self.assertLessEqual(len(result), 1200)
        self.assertIn("[truncated]", result)

    def test_verification_failure_context_contains_exit_code(self):
        result = render_verification_failure_context("some output", exit_code=2, limit=1200)
        self.assertIn("exit_code: 2", result)
        self.assertIn("[verification failure]", result)

    def test_verification_failure_context_small_output_not_truncated(self):
        result = render_verification_failure_context("short", exit_code=1, limit=1200)
        self.assertNotIn("[truncated]", result)
        self.assertIn("short", result)

    def test_native_write_calls_build_final_report(self):
        native_mock = _make_native_mock()
        result, native_mock, logged = self._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        native_mock.build_final_report.assert_called_once()

    def test_native_write_final_report_in_logged_metadata(self):
        native_mock = _make_native_mock()
        result, _, logged = self._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("final_report", logged)

    def test_native_verification_loop_metadata_final_state_after_retry(self):
        from openshard.execution.generator import ChangedFile

        first_result = MagicMock()
        first_result.files = [ChangedFile(path="out.txt", content="x", change_type="create", summary="")]
        first_result.summary = "done"
        first_result.notes = []
        first_result.usage = None

        retry_result = MagicMock()
        retry_result.files = []
        retry_result.summary = "done"
        retry_result.notes = []
        retry_result.usage = None

        native_mock = _make_native_mock()
        native_mock.generate.side_effect = [first_result, retry_result]

        result, native_mock, logged = self._run_write_simple(
            native_mock,
            plan=_safe_plan(),
            verify_returns=[(1, "failed initially"), (0, "passed after retry")],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        loop = native_mock.native_meta.verification_loop
        self.assertTrue(loop.retried)
        self.assertTrue(loop.passed)
        self.assertEqual(loop.exit_code, 0)
        vl = logged.get("verification_loop")
        self.assertIsNotNone(vl)
        self.assertTrue(vl["retried"])
        self.assertTrue(vl["passed"])


class TestNativeVerificationCommandSummary(unittest.TestCase):
    """Unit tests for NativeVerificationCommandSummary and its builder."""

    def _cmd(self, safety_label: str):
        from openshard.verification.plan import (
            CommandSafety,
            VerificationCommand,
            VerificationKind,
            VerificationSource,
        )
        return VerificationCommand(
            name="pytest",
            argv=["python", "-m", "pytest"],
            kind=VerificationKind.test,
            source=VerificationSource.detected,
            safety=CommandSafety(safety_label),
            reason="test",
        )

    def _plan(self, *safety_labels):
        from openshard.verification.plan import VerificationPlan
        return VerificationPlan(commands=[self._cmd(s) for s in safety_labels])

    def test_builder_defaults_when_no_loop_no_plan(self):
        s = build_native_verification_command_summary(
            verification_loop=None, verification_plan=None
        )
        self.assertIsInstance(s, NativeVerificationCommandSummary)
        self.assertFalse(s.attempted)
        self.assertEqual(s.command_count, 0)
        self.assertEqual(s.safe_count, 0)
        self.assertEqual(s.needs_approval_count, 0)
        self.assertEqual(s.blocked_count, 0)
        self.assertFalse(s.passed)
        self.assertFalse(s.retried)
        self.assertEqual(s.warnings, [])

    def test_builder_copies_attempted_passed_retried(self):
        loop = NativeVerificationLoop(attempted=True, passed=True, retried=True)
        s = build_native_verification_command_summary(
            verification_loop=loop, verification_plan=None
        )
        self.assertTrue(s.attempted)
        self.assertTrue(s.passed)
        self.assertTrue(s.retried)

    def test_builder_counts_command_count(self):
        plan = self._plan("safe", "safe", "safe")
        s = build_native_verification_command_summary(
            verification_loop=None, verification_plan=plan
        )
        self.assertEqual(s.command_count, 3)

    def test_builder_counts_safe_commands(self):
        plan = self._plan("safe", "safe", "needs_approval")
        s = build_native_verification_command_summary(
            verification_loop=None, verification_plan=plan
        )
        self.assertEqual(s.safe_count, 2)

    def test_builder_counts_needs_approval_commands(self):
        plan = self._plan("safe", "needs_approval", "needs_approval")
        s = build_native_verification_command_summary(
            verification_loop=None, verification_plan=plan
        )
        self.assertEqual(s.needs_approval_count, 2)

    def test_builder_counts_blocked_commands(self):
        plan = self._plan("safe", "blocked")
        s = build_native_verification_command_summary(
            verification_loop=None, verification_plan=plan
        )
        self.assertEqual(s.blocked_count, 1)

    def test_builder_does_not_store_command_strings(self):
        plan = self._plan("safe")
        s = build_native_verification_command_summary(
            verification_loop=None, verification_plan=plan
        )
        for forbidden in ("argv", "name", "commands", "stdout", "stderr", "output"):
            self.assertFalse(
                hasattr(s, forbidden),
                f"summary should not have attribute '{forbidden}'",
            )

    def test_builder_warning_when_attempted_but_no_commands(self):
        loop = NativeVerificationLoop(attempted=True, passed=False, retried=False)
        plan = self._plan()
        s = build_native_verification_command_summary(
            verification_loop=loop, verification_plan=plan
        )
        self.assertTrue(any("no commands" in w for w in s.warnings))

    def test_executor_stores_verification_command_summary(self):
        meta = NativeRunMeta()
        meta.verification_loop = NativeVerificationLoop(attempted=True, passed=True, retried=False)
        executor = MagicMock(spec=NativeAgentExecutor)
        executor.native_meta = meta
        executor.record_loop_step = MagicMock()
        executor.build_verification_command_summary = NativeAgentExecutor.build_verification_command_summary.__get__(executor)
        plan = self._plan("safe")
        executor.build_verification_command_summary(plan)
        self.assertIsNotNone(meta.verification_command_summary)
        self.assertIsInstance(meta.verification_command_summary, NativeVerificationCommandSummary)

    def test_executor_records_verification_summary_loop_step(self):
        meta = NativeRunMeta()
        meta.verification_loop = NativeVerificationLoop(attempted=True, passed=True, retried=False)
        executor = MagicMock(spec=NativeAgentExecutor)
        executor.native_meta = meta
        executor.record_loop_step = lambda step, **kw: (
            meta.native_loop_steps.append(step)
            if step not in meta.native_loop_steps else None
        )
        executor.build_verification_command_summary = NativeAgentExecutor.build_verification_command_summary.__get__(executor)
        plan = self._plan("safe")
        executor.build_verification_command_summary(plan)
        self.assertIn("verification_summary", meta.native_loop_steps)

    def test_pipeline_serializes_verification_command_summary(self):
        native_mock = _make_native_mock()

        def _side_build_summary(plan):
            s = build_native_verification_command_summary(
                verification_loop=native_mock.native_meta.verification_loop,
                verification_plan=plan,
            )
            native_mock.native_meta.verification_command_summary = s
            return s

        native_mock.build_verification_command_summary.side_effect = _side_build_summary

        result, _, logged = self._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "")]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("verification_command_summary", logged)
        vcs = logged["verification_command_summary"]
        self.assertIsNotNone(vcs)
        self.assertIsInstance(vcs, dict)
        for key in ("command_count", "safe_count", "needs_approval_count", "blocked_count", "passed", "retried", "attempted"):
            self.assertIn(key, vcs, f"key '{key}' missing from verification_command_summary")

    def _run_write_simple(self, native_mock, plan=None, verify_returns=None):
        from openshard.execution.generator import ChangedFile
        safe_file = ChangedFile(
            path="out.txt", content="ok", change_type="create", summary="created"
        )
        if not native_mock.generate.side_effect:
            r = MagicMock()
            r.files = [safe_file]
            r.summary = "done"
            r.notes = []
            r.usage = None
            native_mock.generate.return_value = r
        plan = plan if plan is not None else _safe_plan()
        verify_returns = verify_returns if verify_returns is not None else [(0, "")]
        logged = {}

        def _capture_log(*a, **kw):
            logged.update(kw.get("extra_metadata") or {})

        verify_iter = iter(verify_returns)

        def _verify_side(*a, **kw):
            return next(verify_iter)

        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run", side_effect=_capture_log), \
                 patch("openshard.run.pipeline.build_verification_plan", return_value=plan), \
                 patch("openshard.run.pipeline._run_verification_plan", side_effect=_verify_side), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                result = runner.invoke(cli, ["run", "--workflow", "native", "--write", "fix the bug"])
        return result, native_mock, logged


class TestNativeCommandPolicyPreview(unittest.TestCase):
    """Unit tests for NativeCommandPolicyPreview and its builder."""

    def _cmd(self, safety_label: str):
        from openshard.verification.plan import (
            CommandSafety,
            VerificationCommand,
            VerificationKind,
            VerificationSource,
        )
        return VerificationCommand(
            name="pytest",
            argv=["python", "-m", "pytest"],
            kind=VerificationKind.test,
            source=VerificationSource.detected,
            safety=CommandSafety(safety_label),
            reason="test",
        )

    def _plan(self, *safety_labels):
        from openshard.verification.plan import VerificationPlan
        return VerificationPlan(commands=[self._cmd(s) for s in safety_labels])

    def test_builder_defaults_no_plan(self):
        p = build_native_command_policy_preview(None)
        self.assertIsInstance(p, NativeCommandPolicyPreview)
        self.assertEqual(p.safe_count, 0)
        self.assertEqual(p.needs_approval_count, 0)
        self.assertEqual(p.blocked_count, 0)
        self.assertEqual(p.command_classes, [])
        self.assertEqual(p.warnings, [])

    def test_builder_counts_safe(self):
        plan = self._plan("safe", "safe")
        p = build_native_command_policy_preview(plan)
        self.assertEqual(p.safe_count, 2)

    def test_builder_counts_needs_approval(self):
        plan = self._plan("safe", "needs_approval", "needs_approval")
        p = build_native_command_policy_preview(plan)
        self.assertEqual(p.needs_approval_count, 2)

    def test_builder_counts_blocked(self):
        plan = self._plan("safe", "blocked")
        p = build_native_command_policy_preview(plan)
        self.assertEqual(p.blocked_count, 1)

    def test_builder_no_command_strings(self):
        plan = self._plan("safe")
        p = build_native_command_policy_preview(plan)
        from dataclasses import asdict
        d = asdict(p)
        for forbidden in ("argv", "name", "commands", "stdout", "stderr", "output"):
            self.assertNotIn(forbidden, d, f"preview should not have key '{forbidden}'")

    def test_builder_command_classes_unique_and_sorted(self):
        plan = self._plan("safe", "safe", "blocked")
        p = build_native_command_policy_preview(plan)
        self.assertEqual(p.command_classes, ["blocked", "safe"])

    def test_builder_warning_when_no_classified_commands(self):
        plan = self._plan()
        p = build_native_command_policy_preview(plan)
        self.assertTrue(any("no commands" in w for w in p.warnings))

    def test_executor_stores_preview(self):
        meta = NativeRunMeta()
        executor = MagicMock(spec=NativeAgentExecutor)
        executor.native_meta = meta
        executor.record_loop_step = MagicMock()
        executor.build_command_policy_preview = NativeAgentExecutor.build_command_policy_preview.__get__(executor)
        plan = self._plan("safe")
        executor.build_command_policy_preview(plan)
        self.assertIsNotNone(meta.command_policy_preview)
        self.assertIsInstance(meta.command_policy_preview, NativeCommandPolicyPreview)

    def test_executor_records_command_policy_loop_step(self):
        meta = NativeRunMeta()
        executor = MagicMock(spec=NativeAgentExecutor)
        executor.native_meta = meta
        executor.record_loop_step = lambda step, **kw: (
            meta.native_loop_steps.append(step)
            if step not in meta.native_loop_steps else None
        )
        executor.build_command_policy_preview = NativeAgentExecutor.build_command_policy_preview.__get__(executor)
        plan = self._plan("safe")
        executor.build_command_policy_preview(plan)
        self.assertIn("command_policy", meta.native_loop_steps)

    def test_pipeline_serializes_command_policy_preview(self):
        native_mock = _make_native_mock()

        def _side_build_preview(plan):
            p = build_native_command_policy_preview(plan)
            native_mock.native_meta.command_policy_preview = p
            return p

        native_mock.build_command_policy_preview.side_effect = _side_build_preview

        result, _, logged = self._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "")]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("command_policy_preview", logged)
        cpp = logged["command_policy_preview"]
        self.assertIsNotNone(cpp)
        self.assertIsInstance(cpp, dict)
        for key in ("safe_count", "needs_approval_count", "blocked_count", "command_classes", "warnings"):
            self.assertIn(key, cpp, f"key '{key}' missing from command_policy_preview")

    def _run_write_simple(self, native_mock, plan=None, verify_returns=None):
        from openshard.execution.generator import ChangedFile
        safe_file = ChangedFile(
            path="out.txt", content="ok", change_type="create", summary="created"
        )
        if not native_mock.generate.side_effect:
            r = MagicMock()
            r.files = [safe_file]
            r.summary = "done"
            r.notes = []
            r.usage = None
            native_mock.generate.return_value = r
        plan = plan if plan is not None else _safe_plan()
        verify_returns = verify_returns if verify_returns is not None else [(0, "")]
        logged = {}

        def _capture_log(*a, **kw):
            logged.update(kw.get("extra_metadata") or {})

        verify_iter = iter(verify_returns)

        def _verify_side(*a, **kw):
            return next(verify_iter)

        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run", side_effect=_capture_log), \
                 patch("openshard.run.pipeline.build_verification_plan", return_value=plan), \
                 patch("openshard.run.pipeline._run_verification_plan", side_effect=_verify_side), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                result = runner.invoke(cli, ["run", "--workflow", "native", "--write", "fix the bug"])
        return result, native_mock, logged


class TestNativeFinalReportBuilder(unittest.TestCase):
    """Unit tests for build_native_final_report() in isolation."""

    def _build(self, **kw):
        defaults = dict(
            selected_skills=[],
            observation=None,
            evidence=None,
            plan=None,
            verification_loop=None,
            diff_review=None,
        )
        defaults.update(kw)
        return build_native_final_report(**defaults)

    def test_empty_inputs_produce_defaults(self):
        r = self._build()
        self.assertIsInstance(r, NativeFinalReport)
        self.assertFalse(r.used_native_context)
        self.assertEqual(r.observed_tools, [])
        self.assertEqual(r.selected_skills, [])
        self.assertIsNone(r.plan_intent)
        self.assertIsNone(r.plan_risk)
        self.assertEqual(r.evidence_items, 0)
        self.assertEqual(r.snippet_files, 0)
        self.assertFalse(r.verification_attempted)
        self.assertFalse(r.verification_passed)
        self.assertFalse(r.verification_retried)
        self.assertEqual(r.diff_files, [])
        self.assertEqual(r.added_lines, 0)
        self.assertEqual(r.removed_lines, 0)
        self.assertEqual(r.warnings, [])

    def test_selected_skills_copied(self):
        r = self._build(selected_skills=["python", "testing"])
        self.assertEqual(r.selected_skills, ["python", "testing"])

    def test_observation_tools_copied(self):
        obs = NativeObservation(observed_tools=["get_git_diff", "search_repo"])
        r = self._build(observation=obs)
        self.assertEqual(r.observed_tools, ["get_git_diff", "search_repo"])

    def test_dirty_diff_adds_warning(self):
        obs = NativeObservation(dirty_diff_present=True)
        r = self._build(observation=obs)
        self.assertIn("dirty working tree detected", r.warnings)

    def test_observation_warnings_included(self):
        obs = NativeObservation(warnings=["some warning"])
        r = self._build(observation=obs)
        self.assertIn("some warning", r.warnings)

    def test_evidence_counts_search_results(self):
        ev = NativeEvidence(search_results=["a", "b", "c"])
        r = self._build(evidence=ev)
        self.assertEqual(r.evidence_items, 3)

    def test_evidence_counts_file_snippets(self):
        ev = NativeEvidence(file_snippets=[NativeFileSnippet(path="a.py"), NativeFileSnippet(path="b.py")])
        r = self._build(evidence=ev)
        self.assertEqual(r.snippet_files, 2)

    def test_plan_intent_and_risk_copied(self):
        plan = NativePlan(intent="debug", risk="medium")
        r = self._build(plan=plan)
        self.assertEqual(r.plan_intent, "debug")
        self.assertEqual(r.plan_risk, "medium")

    def test_plan_warnings_included(self):
        plan = NativePlan(warnings=["security-sensitive task"])
        r = self._build(plan=plan)
        self.assertIn("security-sensitive task", r.warnings)

    def test_verification_fields_copied(self):
        vl = NativeVerificationLoop(attempted=True, passed=True, retried=False)
        r = self._build(verification_loop=vl)
        self.assertTrue(r.verification_attempted)
        self.assertTrue(r.verification_passed)
        self.assertFalse(r.verification_retried)

    def test_verification_retried_copied(self):
        vl = NativeVerificationLoop(attempted=True, passed=True, retried=True)
        r = self._build(verification_loop=vl)
        self.assertTrue(r.verification_retried)

    def test_failed_verification_adds_warning(self):
        vl = NativeVerificationLoop(attempted=True, passed=False)
        r = self._build(verification_loop=vl)
        self.assertIn("verification failed", r.warnings)

    def test_passed_verification_no_warning(self):
        vl = NativeVerificationLoop(attempted=True, passed=True)
        r = self._build(verification_loop=vl)
        self.assertNotIn("verification failed", r.warnings)

    def test_diff_review_fields_copied(self):
        dr = NativeDiffReview(changed_files=["src/a.py", "src/b.py"], added_lines=10, removed_lines=3)
        r = self._build(diff_review=dr)
        self.assertEqual(r.diff_files, ["src/a.py", "src/b.py"])
        self.assertEqual(r.added_lines, 10)
        self.assertEqual(r.removed_lines, 3)

    def test_warnings_deduplicated_and_sorted(self):
        obs = NativeObservation(warnings=["b warning", "a warning", "b warning"])
        r = self._build(observation=obs)
        self.assertEqual(r.warnings, sorted(set(r.warnings)))
        self.assertEqual(len(r.warnings), len(set(r.warnings)))

    def test_used_native_context_true_when_observation_present(self):
        r = self._build(observation=NativeObservation())
        self.assertTrue(r.used_native_context)

    def test_used_native_context_true_when_evidence_present(self):
        r = self._build(evidence=NativeEvidence())
        self.assertTrue(r.used_native_context)

    def test_used_native_context_true_when_plan_present(self):
        r = self._build(plan=NativePlan())
        self.assertTrue(r.used_native_context)

    def test_no_raw_content_in_report_fields(self):
        obs = NativeObservation(observed_tools=["get_git_diff"])
        ev = NativeEvidence(search_results=["src/foo.py:10: def bar()"], file_snippets=[])
        dr = NativeDiffReview(changed_files=["src/foo.py"], added_lines=5, removed_lines=2)
        r = self._build(observation=obs, evidence=ev, diff_review=dr)
        all_str_fields = [r.plan_intent, r.plan_risk] + r.observed_tools + r.selected_skills + r.diff_files + r.warnings
        for field_val in all_str_fields:
            if field_val is not None:
                self.assertNotIn("def bar()", field_val)
                self.assertNotIn("diff --git", field_val)


class TestNativeFinalReportExecutor(unittest.TestCase):
    """Unit tests for NativeAgentExecutor.build_final_report()."""

    def _make_executor(self):
        executor = NativeAgentExecutor.__new__(NativeAgentExecutor)
        executor.native_meta = NativeRunMeta()
        executor._runner = None
        return executor

    def test_returns_native_final_report(self):
        executor = self._make_executor()
        result = executor.build_final_report()
        self.assertIsInstance(result, NativeFinalReport)

    def test_stores_in_native_meta(self):
        executor = self._make_executor()
        report = executor.build_final_report()
        self.assertIs(executor.native_meta.final_report, report)

    def test_selected_skills_flow_through(self):
        executor = self._make_executor()
        executor.native_meta.selected_skills = ["python", "security-sensitive-change"]
        report = executor.build_final_report()
        self.assertEqual(report.selected_skills, ["python", "security-sensitive-change"])

    def test_diff_review_values_reflected(self):
        executor = self._make_executor()
        executor.native_meta.diff_review = NativeDiffReview(
            changed_files=["main.py"], added_lines=4, removed_lines=1
        )
        report = executor.build_final_report()
        self.assertEqual(report.diff_files, ["main.py"])
        self.assertEqual(report.added_lines, 4)
        self.assertEqual(report.removed_lines, 1)

    def test_all_optional_fields_none_returns_defaults(self):
        executor = self._make_executor()
        executor.native_meta.observation = None
        executor.native_meta.evidence = None
        executor.native_meta.plan = None
        executor.native_meta.verification_loop = None
        executor.native_meta.diff_review = None
        report = executor.build_final_report()
        self.assertFalse(report.used_native_context)
        self.assertEqual(report.warnings, [])


class TestNativeFinalReportPipeline(unittest.TestCase):
    """Pipeline integration tests for NativeFinalReport serialization."""

    def _run_write_simple(self, native_mock, plan=None, verify_returns=None):
        from openshard.execution.generator import ChangedFile
        safe_file = ChangedFile(
            path="out.txt", content="ok", change_type="create", summary="created"
        )
        if not native_mock.generate.side_effect:
            r = MagicMock()
            r.files = [safe_file]
            r.summary = "done"
            r.notes = []
            r.usage = None
            native_mock.generate.return_value = r

        plan = plan if plan is not None else _safe_plan()
        verify_returns = verify_returns if verify_returns is not None else [(0, "")]
        logged = {}

        def _capture_log(*a, **kw):
            logged.update(kw.get("extra_metadata") or {})

        verify_iter = iter(verify_returns)

        def _verify_side(*a, **kw):
            return next(verify_iter)

        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run", side_effect=_capture_log), \
                 patch("openshard.run.pipeline.build_verification_plan", return_value=plan), \
                 patch("openshard.run.pipeline._run_verification_plan", side_effect=_verify_side), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                result = runner.invoke(cli, ["run", "--workflow", "native", "--write", "fix the bug"])
        return result, native_mock, logged

    def test_native_workflow_calls_build_final_report(self):
        native_mock = _make_native_mock()
        result, native_mock, _ = self._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        native_mock.build_final_report.assert_called_once()

    def test_non_native_workflow_does_not_call_build_final_report(self):
        from openshard.execution.generator import ChangedFile
        safe_file = ChangedFile(path="out.txt", content="ok", change_type="create", summary="created")
        fake_result = MagicMock()
        fake_result.files = [safe_file]
        fake_result.summary = "done"
        fake_result.notes = []
        fake_result.usage = None

        generator_mock = _make_generator_mock()
        generator_mock.generate.return_value = fake_result

        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor") as native_cls, \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                runner.invoke(cli, ["run", "--workflow", "direct", "--write", "fix the bug"])
        native_cls.assert_not_called()

    def test_final_report_key_in_logged_metadata(self):
        native_mock = _make_native_mock()
        result, _, logged = self._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("final_report", logged)

    def test_final_report_serialized_as_dict(self):
        native_mock = _make_native_mock()

        def _set_report():
            native_mock.native_meta.final_report = NativeFinalReport()

        native_mock.build_final_report.side_effect = _set_report
        result, _, logged = self._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        fr = logged.get("final_report")
        self.assertIsNotNone(fr)
        self.assertIsInstance(fr, dict)

    def test_final_report_contains_expected_keys(self):
        native_mock = _make_native_mock()

        def _set_report():
            native_mock.native_meta.final_report = NativeFinalReport()

        native_mock.build_final_report.side_effect = _set_report
        result, _, logged = self._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        fr = logged["final_report"]
        self.assertIsNotNone(fr)
        for key in (
            "used_native_context", "observed_tools", "selected_skills",
            "plan_intent", "plan_risk", "evidence_items", "snippet_files",
            "verification_attempted", "verification_passed", "verification_retried",
            "diff_files", "added_lines", "removed_lines", "warnings",
        ):
            self.assertIn(key, fr)

    def test_final_report_captures_verification_retried(self):
        from openshard.execution.generator import ChangedFile

        first_result = MagicMock()
        first_result.files = [ChangedFile(path="out.txt", content="x", change_type="create", summary="")]
        first_result.summary = "done"
        first_result.notes = []
        first_result.usage = None

        retry_result = MagicMock()
        retry_result.files = []
        retry_result.summary = "done"
        retry_result.notes = []
        retry_result.usage = None

        native_mock = _make_native_mock()
        native_mock.generate.side_effect = [first_result, retry_result]

        result, native_mock, _ = self._run_write_simple(
            native_mock,
            plan=_safe_plan(),
            verify_returns=[(1, "failed"), (0, "passed")],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        loop = native_mock.native_meta.verification_loop
        self.assertTrue(loop.retried)
        native_mock.build_final_report.assert_called_once()


import click  # noqa: E402 — needed for CliRunner command wrapper below


def _render_native_summary(report: NativeFinalReport | None) -> str:
    from click.testing import CliRunner

    from openshard.cli.run_output import _print_native_summary

    meta = NativeRunMeta()
    meta.final_report = report

    @click.command()
    def cmd():
        _print_native_summary(meta)

    return CliRunner().invoke(cmd).output


def _render_native_demo_block_str(meta: NativeRunMeta | None) -> str:
    """Wrap _print_native_demo_block in CliRunner for unit testing."""
    from click.testing import CliRunner

    from openshard.cli.run_output import _print_native_demo_block

    @click.command()
    def cmd():
        _print_native_demo_block(meta)

    return CliRunner().invoke(cmd).output


class TestNativePatchProposal(unittest.TestCase):
    """Unit tests for NativePatchProposal builder and executor integration."""

    def _make_file(self, path, change_type="update", summary="a change", content="body"):
        f = MagicMock()
        f.path = path
        f.change_type = change_type
        f.summary = summary
        f.content = content
        return f

    def test_builder_defaults_empty(self):
        proposal = build_native_patch_proposal([])
        self.assertEqual(proposal.file_count, 0)
        self.assertEqual(proposal.files, [])
        self.assertEqual(proposal.change_types, [])
        self.assertEqual(proposal.summaries, [])
        self.assertEqual(proposal.warnings, [])

    def test_builder_counts_files(self):
        files = [self._make_file("a.py"), self._make_file("b.py")]
        proposal = build_native_patch_proposal(files)
        self.assertEqual(proposal.file_count, 2)

    def test_builder_records_paths(self):
        files = [self._make_file("src/foo.py"), self._make_file("src/bar.py")]
        proposal = build_native_patch_proposal(files)
        self.assertEqual(proposal.files, ["src/foo.py", "src/bar.py"])

    def test_builder_records_change_types(self):
        files = [self._make_file("a.py", change_type="create"), self._make_file("b.py", change_type="update")]
        proposal = build_native_patch_proposal(files)
        self.assertEqual(proposal.change_types, ["create", "update"])

    def test_builder_records_summaries(self):
        files = [self._make_file("a.py", summary="added handler"), self._make_file("b.py", summary="fixed bug")]
        proposal = build_native_patch_proposal(files)
        self.assertEqual(proposal.summaries, ["added handler", "fixed bug"])

    def test_builder_does_not_store_content(self):
        from dataclasses import asdict
        files = [self._make_file("a.py", content="secret content")]
        proposal = build_native_patch_proposal(files)
        d = asdict(proposal)
        self.assertNotIn("content", d)

    def test_executor_build_patch_proposal_stores_meta(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen), \
             patch("openshard.native.executor.NativeToolRunner"):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=None)
        files = [self._make_file("x.py"), self._make_file("y.py")]
        executor.build_patch_proposal(files)
        self.assertIsNotNone(executor.native_meta.patch_proposal)
        self.assertEqual(executor.native_meta.patch_proposal.file_count, 2)

    def test_executor_build_patch_proposal_records_loop_step(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen), \
             patch("openshard.native.executor.NativeToolRunner"):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=None)
        executor.build_patch_proposal([self._make_file("a.py")])
        self.assertIn("proposal", executor.native_meta.native_loop_steps)

    def test_pipeline_calls_build_patch_proposal_on_native_write(self):
        from openshard.execution.generator import ChangedFile
        safe_file = ChangedFile(path="out.txt", content="ok", change_type="create", summary="created")
        native_mock = _make_native_mock()
        r = MagicMock()
        r.files = [safe_file]
        r.summary = "done"
        r.notes = []
        r.usage = None
        native_mock.generate.return_value = r

        plan = _safe_plan()
        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.build_verification_plan", return_value=plan), \
                 patch("openshard.run.pipeline._run_verification_plan", return_value=(0, "")), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                result = runner.invoke(cli, ["run", "--workflow", "native", "--write", "fix the bug"])
        self.assertEqual(result.exit_code, 0, result.output)
        native_mock.build_patch_proposal.assert_called_once()

    def test_metadata_serializes_patch_proposal(self):
        from openshard.execution.generator import ChangedFile
        safe_file = ChangedFile(path="out.txt", content="ok", change_type="create", summary="created")
        native_mock = _make_native_mock()

        def _set_proposal(*_a, **_kw):
            native_mock.native_meta.patch_proposal = NativePatchProposal(
                file_count=1, files=["out.txt"], change_types=["create"], summaries=["created"]
            )

        native_mock.build_patch_proposal.side_effect = _set_proposal
        r = MagicMock()
        r.files = [safe_file]
        r.summary = "done"
        r.notes = []
        r.usage = None
        native_mock.generate.return_value = r
        logged = {}

        def _capture_log(*a, **kw):
            logged.update(kw.get("extra_metadata") or {})

        plan = _safe_plan()
        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run", side_effect=_capture_log), \
                 patch("openshard.run.pipeline.build_verification_plan", return_value=plan), \
                 patch("openshard.run.pipeline._run_verification_plan", return_value=(0, "")), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                result = runner.invoke(cli, ["run", "--workflow", "native", "--write", "fix the bug"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("patch_proposal", logged)
        pp = logged["patch_proposal"]
        self.assertIsNotNone(pp)
        self.assertIsInstance(pp, dict)
        self.assertEqual(pp["file_count"], 1)


class TestNativeSummaryRenderer(unittest.TestCase):
    """Unit tests for _print_native_summary CLI renderer."""

    def _blank(self) -> NativeFinalReport:
        return NativeFinalReport()

    # 1
    def test_no_output_when_final_report_is_none(self):
        out = _render_native_summary(None)
        self.assertNotIn("[native summary]", out)

    # 2
    def test_context_yes(self):
        r = self._blank()
        r.used_native_context = True
        out = _render_native_summary(r)
        self.assertIn("context: yes", out)

    # 3
    def test_context_no(self):
        r = self._blank()
        r.used_native_context = False
        out = _render_native_summary(r)
        self.assertIn("context: no", out)

    # 4
    def test_skills_rendered(self):
        r = self._blank()
        r.selected_skills = ["repo mapping", "safe file editing"]
        out = _render_native_summary(r)
        self.assertIn("skills: repo mapping, safe file editing", out)

    # 5
    def test_plan_renders(self):
        r = self._blank()
        r.plan_intent = "implementation"
        r.plan_risk = "medium"
        out = _render_native_summary(r)
        self.assertIn("plan: implementation / medium", out)

    # 6
    def test_evidence_renders(self):
        r = self._blank()
        r.evidence_items = 3
        r.snippet_files = 2
        out = _render_native_summary(r)
        self.assertIn("evidence: 3 items, 2 snippets", out)

    # 7
    def test_verification_retried_passed(self):
        r = self._blank()
        r.verification_attempted = True
        r.verification_retried = True
        r.verification_passed = True
        out = _render_native_summary(r)
        self.assertIn("verification: retried, passed", out)

    # 8
    def test_verification_failed(self):
        r = self._blank()
        r.verification_attempted = True
        r.verification_retried = False
        r.verification_passed = False
        out = _render_native_summary(r)
        self.assertIn("verification: failed", out)
        self.assertNotIn("retried", out)

    # 9
    def test_diff_counts_render(self):
        r = self._blank()
        r.diff_files = ["a.py", "b.py"]
        r.added_lines = 34
        r.removed_lines = 8
        out = _render_native_summary(r)
        self.assertIn("diff: 2 files, +34 / -8", out)

    # 10
    def test_diff_singular_file(self):
        r = self._blank()
        r.diff_files = ["a.py"]
        r.added_lines = 5
        r.removed_lines = 1
        out = _render_native_summary(r)
        self.assertIn("diff: 1 file,", out)
        self.assertNotIn("1 files", out)

    # 11
    def test_warnings_render_compact(self):
        r = self._blank()
        r.warnings = ["dirty working tree detected", "large diff"]
        out = _render_native_summary(r)
        self.assertIn("  warning: dirty working tree detected", out)
        self.assertIn("  warning: large diff", out)

    # 12
    def test_no_raw_diff_filenames(self):
        r = self._blank()
        r.diff_files = ["src/secret_internal_path.py"]
        r.added_lines = 1
        r.removed_lines = 0
        out = _render_native_summary(r)
        self.assertNotIn("src/secret_internal_path.py", out)

    # 13 — integration: native workflow shows receipt in default mode, summary in --more
    def test_pipeline_native_shows_receipt_in_default(self):
        from openshard.execution.generator import ChangedFile

        native_mock = _make_native_mock()
        safe_file = ChangedFile(path="out.txt", content="ok", change_type="create", summary="created")
        r = MagicMock()
        r.files = [safe_file]
        r.summary = "done"
        r.notes = []
        r.usage = None
        native_mock.generate.return_value = r

        def _set_report():
            native_mock.native_meta.final_report = NativeFinalReport(
                used_native_context=True,
                selected_skills=["repo mapping"],
            )

        native_mock.build_final_report.side_effect = _set_report

        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.build_verification_plan", return_value=_safe_plan()), \
                 patch("openshard.run.pipeline._run_verification_plan", return_value=(0, "")), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                result = runner.invoke(cli, ["run", "--workflow", "native", "--write", "fix the bug"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Receipt saved", result.output)
        self.assertNotIn("[native summary]", result.output)
        self.assertNotIn("[native]", result.output)

    def test_pipeline_native_shows_summary_with_more(self):
        from openshard.execution.generator import ChangedFile

        native_mock = _make_native_mock()
        safe_file = ChangedFile(path="out.txt", content="ok", change_type="create", summary="created")
        r = MagicMock()
        r.files = [safe_file]
        r.summary = "done"
        r.notes = []
        r.usage = None
        native_mock.generate.return_value = r

        def _set_report():
            native_mock.native_meta.final_report = NativeFinalReport(
                used_native_context=True,
                selected_skills=["repo mapping"],
            )

        native_mock.build_final_report.side_effect = _set_report

        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.build_verification_plan", return_value=_safe_plan()), \
                 patch("openshard.run.pipeline._run_verification_plan", return_value=(0, "")), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                result = runner.invoke(cli, ["run", "--workflow", "native", "--write", "--more", "fix the bug"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("[native summary]", result.output)
        self.assertIn("context: yes", result.output)
        self.assertIn("skills: repo mapping", result.output)

    # 14 — integration: non-native workflow omits native summary
    def test_pipeline_non_native_no_summary(self):
        from openshard.execution.generator import ChangedFile

        safe_file = ChangedFile(path="out.txt", content="ok", change_type="create", summary="created")
        fake_result = MagicMock()
        fake_result.files = [safe_file]
        fake_result.summary = "done"
        fake_result.notes = []
        fake_result.usage = None

        generator_mock = _make_generator_mock()
        generator_mock.generate.return_value = fake_result

        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor") as _native_cls, \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                runner = CliRunner()
                result = runner.invoke(cli, ["run", "--workflow", "direct", "--write", "fix the bug"])

        self.assertNotIn("[native summary]", result.output)

    # 15 — getattr guard: passing None directly must not crash
    def test_renderer_accepts_none_meta(self):
        from click.testing import CliRunner

        from openshard.cli.run_output import _print_native_summary

        @click.command()
        def cmd():
            _print_native_summary(None)

        result = CliRunner().invoke(cmd)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output.strip(), "")


class TestNativeLoopSteps(unittest.TestCase):
    """Tests for native_loop_steps field and record_loop_step method."""

    def _make_executor(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor

    # 1
    def test_native_loop_steps_defaults_to_empty_list(self):
        meta = NativeRunMeta()
        self.assertEqual(meta.native_loop_steps, [])

    # 2
    def test_native_loop_steps_instances_not_shared(self):
        a = NativeRunMeta()
        b = NativeRunMeta()
        a.native_loop_steps.append("repo_context")
        self.assertEqual(b.native_loop_steps, [])

    # 3
    def test_record_loop_step_appends_in_order(self):
        executor = self._make_executor()
        executor.record_loop_step("repo_context")
        executor.record_loop_step("observation")
        executor.record_loop_step("plan")
        self.assertEqual(
            executor.native_meta.native_loop_steps,
            ["repo_context", "observation", "plan"],
        )

    # 4
    def test_record_loop_step_no_duplicates(self):
        executor = self._make_executor()
        executor.record_loop_step("plan")
        executor.record_loop_step("plan")
        self.assertEqual(executor.native_meta.native_loop_steps, ["plan"])

    # 5
    def test_preflight_records_repo_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = self._make_executor_with_repo(Path(tmp))
            executor._run_preflight()
        self.assertIn("repo_context", executor.native_meta.native_loop_steps)

    # 6
    def test_observe_phase_records_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = self._make_executor_with_repo(Path(tmp))
            executor._run_observe_phase("fix the bug")
        self.assertIn("observation", executor.native_meta.native_loop_steps)

    # 7
    def test_observe_phase_records_evidence_for_search_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.py").write_text("DATABASE_URL = 'sqlite:///db'\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("where is the database config")
        if executor.native_meta.evidence is not None:
            self.assertIn("evidence", executor.native_meta.native_loop_steps)

    # 8
    def test_generate_records_plan_and_generation(self):
        executor = self._make_executor()
        executor.generate("fix the bug")
        self.assertIn("plan", executor.native_meta.native_loop_steps)
        self.assertIn("generation", executor.native_meta.native_loop_steps)

    # 9
    def test_review_diff_records_diff_review(self):
        from openshard.native.tools import NativeToolResult
        executor = self._make_executor()
        runner_mock = MagicMock()
        runner_mock.run.return_value = NativeToolResult(tool_name="get_git_diff", ok=True, output="diff --git a/x\n+line")
        runner_mock.trace_entry.return_value = {"tool": "get_git_diff", "ok": True, "output_chars": 10}
        executor._runner = runner_mock
        executor.review_diff()
        self.assertIn("diff_review", executor.native_meta.native_loop_steps)

    # 10
    def test_build_final_report_records_final_report(self):
        executor = self._make_executor()
        executor.build_final_report()
        self.assertIn("final_report", executor.native_meta.native_loop_steps)

    # 11
    def test_pipeline_serializes_native_loop_steps(self):
        native_mock = _make_native_mock()
        _, _, logged = TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertIn("native_loop_steps", logged)
        self.assertIsInstance(logged["native_loop_steps"], list)

    # 12
    def test_pipeline_records_write_and_verification_steps(self):
        native_mock = _make_native_mock()
        native_mock.record_loop_step.side_effect = lambda step: (
            native_mock.native_meta.native_loop_steps.append(step)
            if step not in native_mock.native_meta.native_loop_steps
            else None
        )
        TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertIn("write", native_mock.native_meta.native_loop_steps)
        self.assertIn("verification", native_mock.native_meta.native_loop_steps)

    # 13
    def test_native_loop_steps_labels_only_no_raw_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = self._make_executor_with_repo(Path(tmp))
            executor._run_preflight()
            executor._run_observe_phase("fix the bug")
        for step in executor.native_meta.native_loop_steps:
            self.assertIsInstance(step, str)
            self.assertLess(len(step), 30, f"step label unexpectedly long: {step!r}")
            self.assertNotIn("\n", step)

    # 14
    def test_live_native_output_renders_loop_line(self):
        meta = NativeRunMeta()
        meta.native_loop_steps = ["repo_context", "observation", "plan", "generation"]
        meta.plan = NativePlan(intent="standard", risk="low")
        out = _render_native_demo_block_str(meta)
        self.assertIn("loop:", out)
        self.assertIn("repo_context -> observation -> plan -> generation", out)


class TestNativeLoopTrace(unittest.TestCase):
    """Tests for NativeLoopTrace, NativeLoopEvent, and their integration with record_loop_step."""

    def _make_executor(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor

    # 1
    def test_native_loop_trace_defaults_to_empty(self):
        from openshard.native.loop import NativeLoopTrace
        trace = NativeLoopTrace()
        self.assertEqual(trace.events, [])
        self.assertEqual(trace.phases(), [])

    # 2
    def test_record_preserves_order(self):
        from openshard.native.loop import NativeLoopTrace
        trace = NativeLoopTrace()
        trace.record("repo_context")
        trace.record("observation")
        trace.record("plan")
        self.assertEqual(trace.phases(), ["repo_context", "observation", "plan"])

    # 3
    def test_record_loop_step_still_updates_native_loop_steps(self):
        executor = self._make_executor()
        executor.record_loop_step("plan")
        self.assertIn("plan", executor.native_meta.native_loop_steps)

    # 4
    def test_record_loop_step_records_trace_event(self):
        executor = self._make_executor()
        executor.record_loop_step("plan")
        self.assertEqual(len(executor.native_meta.native_loop_trace.events), 1)
        self.assertEqual(executor.native_meta.native_loop_trace.events[0].phase, "plan")

    # 5
    def test_trace_events_metadata_not_shared(self):
        from openshard.native.loop import NativeLoopTrace
        trace = NativeLoopTrace()
        trace.record("write", metadata={"files": 1})
        trace.record("verification", metadata={"passed": True})
        trace.events[0].metadata["extra"] = "x"
        self.assertNotIn("extra", trace.events[1].metadata)

    # 6
    def test_pipeline_serializes_native_loop_trace(self):
        native_mock = _make_native_mock()
        _, _, logged = TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertIn("native_loop_trace", logged)
        self.assertIsInstance(logged["native_loop_trace"], list)

    # 7
    def test_trace_metadata_compact_labels_only(self):
        from openshard.native.loop import NativeLoopTrace
        trace = NativeLoopTrace()
        trace.record("write", metadata={"files": 2})
        trace.record("verification", metadata={"passed": True})
        trace.record("diff_review", metadata={"added": 34, "removed": 8})
        for event in trace.events:
            for val in event.metadata.values():
                self.assertIsInstance(val, (bool, int, str))
                if isinstance(val, str):
                    self.assertNotIn("\n", val)
                    self.assertLess(len(val), 100)

    # 8
    def test_no_raw_content_in_trace(self):
        executor = self._make_executor()
        executor.record_loop_step("repo_context")
        executor.record_loop_step("generation")
        for event in executor.native_meta.native_loop_trace.events:
            summary = event.summary
            self.assertNotIn("diff --git", summary)
            for val in event.metadata.values():
                if isinstance(val, str):
                    self.assertLess(len(val), 200, f"metadata value suspiciously long: {val!r}")

    # 9 — NativeRunMeta has native_loop_trace field
    def test_native_run_meta_has_loop_trace_field(self):
        from openshard.native.loop import NativeLoopTrace
        meta = NativeRunMeta()
        self.assertIsInstance(meta.native_loop_trace, NativeLoopTrace)
        self.assertEqual(meta.native_loop_trace.events, [])

    # 10 — instances do not share trace
    def test_native_run_meta_trace_instances_not_shared(self):
        a = NativeRunMeta()
        b = NativeRunMeta()
        a.native_loop_trace.record("plan")
        self.assertEqual(b.native_loop_trace.events, [])


class TestNativeDemoBlock(unittest.TestCase):
    """Unit and integration tests for the [native] demo block renderer."""

    def _blank_meta(self) -> NativeRunMeta:
        meta = NativeRunMeta()
        meta.repo_context_summary = NativeRepoContextSummary(
            likely_stack_markers=["python"],
            test_markers=["tests/test_foo.py"],
        )
        meta.observation = NativeObservation(dirty_diff_present=False, search_matches_count=5)
        meta.plan = NativePlan(intent="implementation", risk="medium")
        meta.write_path = "pipeline"
        meta.verification_loop = NativeVerificationLoop(attempted=True, passed=True, retried=True)
        meta.diff_review = NativeDiffReview(
            has_diff=True, changed_files=["a.py", "b.py"], added_lines=34, removed_lines=8
        )
        return meta

    # 1 — repo/context signal
    def test_repo_line_shows_stack_and_tests(self):
        out = _render_native_demo_block_str(self._blank_meta())
        self.assertIn("[native]", out)
        self.assertIn("repo: python, tests detected", out)

    def test_repo_no_markers_shows_unknown(self):
        meta = self._blank_meta()
        meta.repo_context_summary = NativeRepoContextSummary()
        out = _render_native_demo_block_str(meta)
        self.assertIn("repo: unknown", out)

    # 2 — observation signal
    def test_observation_dirty_tree_yes(self):
        meta = self._blank_meta()
        meta.observation = NativeObservation(dirty_diff_present=True, search_matches_count=3)
        out = _render_native_demo_block_str(meta)
        self.assertIn("observation: dirty tree yes, search evidence yes", out)

    def test_observation_absent_skips_line(self):
        meta = self._blank_meta()
        meta.observation = None
        out = _render_native_demo_block_str(meta)
        self.assertNotIn("observation:", out)

    # 3 — plan/risk signal
    def test_plan_line_shows_intent_and_risk(self):
        out = _render_native_demo_block_str(self._blank_meta())
        self.assertIn("plan: implementation / medium", out)

    def test_plan_absent_skips_line(self):
        meta = self._blank_meta()
        meta.plan = None
        out = _render_native_demo_block_str(meta)
        self.assertNotIn("  plan:", out)

    # 4 — write path always shown
    def test_write_path_always_present(self):
        out = _render_native_demo_block_str(self._blank_meta())
        self.assertIn("write path: pipeline", out)

    # 5 — verification state
    def test_verification_retried_passed(self):
        out = _render_native_demo_block_str(self._blank_meta())
        self.assertIn("verification: retried, passed", out)

    def test_verification_not_attempted_skips_line(self):
        meta = self._blank_meta()
        meta.verification_loop = NativeVerificationLoop(attempted=False, passed=False, retried=False)
        out = _render_native_demo_block_str(meta)
        self.assertNotIn("verification:", out)

    # 6 — diff summary (counts only, no paths)
    def test_diff_line_shows_counts(self):
        out = _render_native_demo_block_str(self._blank_meta())
        self.assertIn("diff: 2 files, +34 / -8", out)

    def test_diff_no_diff_skips_line(self):
        meta = self._blank_meta()
        meta.diff_review = NativeDiffReview(has_diff=False)
        out = _render_native_demo_block_str(meta)
        self.assertNotIn("  diff:", out)

    # 7 — non-native workflow omits [native] block
    def test_non_native_workflow_no_native_block(self):
        from openshard.execution.generator import ChangedFile
        safe_file = ChangedFile(path="out.txt", content="ok", change_type="create", summary="created")
        fake_result = MagicMock()
        fake_result.files = [safe_file]
        fake_result.summary = "done"
        fake_result.notes = []
        fake_result.usage = None
        generator_mock = _make_generator_mock()
        generator_mock.generate.return_value = fake_result
        with tempfile.TemporaryDirectory() as ws:
            with patch("openshard.run.pipeline.NativeAgentExecutor"), \
                 patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock), \
                 patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
                 patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
                 patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
                 patch("openshard.run.pipeline._log_run"), \
                 patch("openshard.run.pipeline.tempfile.mkdtemp", return_value=ws):
                result = CliRunner().invoke(cli, ["run", "--workflow", "direct", "--write", "fix the bug"])
        self.assertNotIn("[native]", result.output)

    # 8 — raw output is never printed
    def test_no_raw_diff_file_paths_in_output(self):
        meta = self._blank_meta()
        meta.diff_review = NativeDiffReview(
            has_diff=True, changed_files=["src/secret_internal_path.py"],
            added_lines=10, removed_lines=2,
        )
        out = _render_native_demo_block_str(meta)
        self.assertNotIn("src/secret_internal_path.py", out)
        self.assertIn("diff: 1 file,", out)

    def test_no_raw_search_count_in_output(self):
        meta = self._blank_meta()
        meta.observation = NativeObservation(dirty_diff_present=False, search_matches_count=99)
        out = _render_native_demo_block_str(meta)
        self.assertIn("search evidence yes", out)
        self.assertNotIn("99", out)

    def test_renderer_accepts_none_meta(self):
        out = _render_native_demo_block_str(None)
        self.assertNotIn("[native]", out)
        self.assertEqual(out.strip(), "")

    def test_no_block_when_no_useful_metadata(self):
        from types import SimpleNamespace

        native_meta = SimpleNamespace(
            repo_context_summary=None,
            observation=None,
            plan=None,
            write_path="pipeline",
            verification_loop=None,
            diff_review=None,
            final_report=None,
        )
        out = _render_native_demo_block_str(native_meta)
        self.assertNotIn("[native]", out)
        self.assertEqual(out.strip(), "")


class TestNativeBackends(unittest.TestCase):
    """Tests for the backend seam in openshard.native.backends."""

    def test_builtin_available(self):
        from openshard.native.backends import BuiltinNativeBackend
        self.assertTrue(BuiltinNativeBackend().available())

    def test_builtin_run_returns_result(self):
        from openshard.native.backends import BuiltinNativeBackend
        result = BuiltinNativeBackend().run(task="t", context={})
        self.assertIsInstance(result.summary, str)
        self.assertEqual(result.metadata["backend"], "builtin")

    def test_deepagents_unavailable_when_not_installed(self):
        import sys

        from openshard.native.backends import DeepAgentsNativeBackend

        with patch.dict(sys.modules, {"deepagents": None}):
            backend = DeepAgentsNativeBackend()
            self.assertFalse(backend.available())
            result = backend.run(task="t", context={})
            self.assertIn("unavailable", result.summary)
            self.assertEqual(result.metadata["available"], False)
            self.assertTrue(len(result.notes) > 0)

    def test_deepagents_available_when_installed(self):
        import sys

        from openshard.native.backends import DeepAgentsNativeBackend

        with patch.dict(sys.modules, {"deepagents": MagicMock()}):
            backend = DeepAgentsNativeBackend()
            self.assertTrue(backend.available())
            result = backend.run(task="t", context={})
            self.assertEqual(result.metadata["available"], True)
            self.assertEqual(result.metadata["mode"], "stub")

    def test_get_backend_returns_builtin_by_default(self):
        from openshard.native.backends import BuiltinNativeBackend, get_backend
        self.assertIsInstance(get_backend("builtin"), BuiltinNativeBackend)

    def test_get_backend_unknown_name_returns_builtin(self):
        from openshard.native.backends import BuiltinNativeBackend, get_backend
        self.assertIsInstance(get_backend("unknown"), BuiltinNativeBackend)

    def test_get_backend_returns_deepagents(self):
        from openshard.native.backends import DeepAgentsNativeBackend, get_backend
        self.assertIsInstance(get_backend("deepagents"), DeepAgentsNativeBackend)


class TestNativeRunMetaBackendDefaults(unittest.TestCase):
    def test_native_backend_defaults(self):
        meta = NativeRunMeta()
        self.assertEqual(meta.native_backend, "builtin")
        self.assertTrue(meta.native_backend_available)
        self.assertEqual(meta.native_backend_notes, [])


class TestNativeAgentExecutorBackend(unittest.TestCase):
    def _make_executor(self, backend_name="builtin"):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), backend_name=backend_name)
        return executor

    def test_executor_records_builtin_backend_in_meta(self):
        executor = self._make_executor()
        self.assertEqual(executor.native_meta.native_backend, "builtin")
        self.assertTrue(executor.native_meta.native_backend_available)
        self.assertEqual(executor.native_meta.native_backend_notes, [])

    def test_executor_deepagents_unavailable_records_note(self):
        import sys
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen), \
             patch.dict(sys.modules, {"deepagents": None}):
            executor = NativeAgentExecutor(provider=MagicMock(), backend_name="deepagents")
        self.assertEqual(executor.native_meta.native_backend, "deepagents")
        self.assertFalse(executor.native_meta.native_backend_available)
        self.assertIn("Install deepagents", executor.native_meta.native_backend_notes[0])

    def test_executor_deepagents_available_records_no_notes(self):
        import sys
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen), \
             patch.dict(sys.modules, {"deepagents": MagicMock()}):
            executor = NativeAgentExecutor(provider=MagicMock(), backend_name="deepagents")
        self.assertEqual(executor.native_meta.native_backend, "deepagents")
        self.assertTrue(executor.native_meta.native_backend_available)
        self.assertEqual(executor.native_meta.native_backend_notes, [])

    def test_selecting_builtin_leaves_generate_behaviour_unchanged(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), backend_name="builtin")
        result = executor.generate("fix bug", model="some-model")
        fake_gen.generate.assert_called_once()
        self.assertIs(result, fake_gen.generate.return_value)


class TestDeepAgentsReadonlyProof(unittest.TestCase):
    """Tests for the minimal read-only DeepAgents proof invocation."""

    def test_missing_deepagents_returns_unavailable(self):
        import sys

        from openshard.native.backends import _default_deepagents_proof
        with patch.dict(sys.modules, {"deepagents": None}):
            result = _default_deepagents_proof("fix bug")
        self.assertFalse(result["available"])
        self.assertEqual(result["mode"], "unavailable")

    def test_installed_no_model_returns_unconfigured(self):
        # No deepagents_model in context → unconfigured without calling create_deep_agent
        import sys

        from openshard.native.backends import _default_deepagents_proof
        mock_create = MagicMock()
        fake_da = MagicMock()
        fake_da.create_deep_agent = mock_create
        with patch.dict(sys.modules, {"deepagents": fake_da}):
            with patch("openshard.native.backends._import_deepagents_create", return_value=mock_create):
                result = _default_deepagents_proof("fix bug", context={})
        self.assertTrue(result["available"])
        self.assertEqual(result["mode"], "readonly_agent_unconfigured")
        mock_create.assert_not_called()

    def test_create_deep_agent_called_only_with_experimental_flag(self):
        import sys
        # Inject deepagents_model so the model guard passes and create_deep_agent is called
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = "ok"
        mock_create = MagicMock(return_value=mock_agent)
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            with patch.dict(sys.modules, {"deepagents": MagicMock()}):
                with patch("openshard.native.backends._import_deepagents_create", return_value=mock_create):
                    executor = NativeAgentExecutor(
                        provider=MagicMock(),
                        backend_name="deepagents",
                        experimental_deepagents_run=True,
                        deepagents_model="mock-model",
                    )
                    executor.generate("fix bug")
        mock_create.assert_called_once()

    def test_create_deep_agent_not_called_without_flag(self):
        import sys
        mock_create = MagicMock()
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            with patch.dict(sys.modules, {"deepagents": MagicMock()}):
                with patch("openshard.native.backends._import_deepagents_create", return_value=mock_create):
                    executor = NativeAgentExecutor(
                        provider=MagicMock(),
                        backend_name="deepagents",
                        experimental_deepagents_run=False,
                    )
                    executor.generate("fix bug")
        mock_create.assert_not_called()

    def test_no_write_edit_shell_tools_passed(self):
        # Pass deepagents_model so model guard passes, then verify tools=[]
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = "ok"
        mock_create = MagicMock(return_value=mock_agent)
        with patch("openshard.native.backends._import_deepagents_create", return_value=mock_create):
            from openshard.native.backends import _default_deepagents_proof
            _default_deepagents_proof("fix bug", context={"deepagents_model": "mock-model"})
        _, kwargs = mock_create.call_args
        self.assertEqual(kwargs.get("tools", []), [])

    def test_long_output_truncated(self):
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = "x" * 1000
        mock_create = MagicMock(return_value=mock_agent)
        with patch("openshard.native.backends._import_deepagents_create", return_value=mock_create):
            from openshard.native.backends import _default_deepagents_proof
            result = _default_deepagents_proof(
                "fix bug", context={"deepagents_model": "mock-model"}
            )
        self.assertLessEqual(len(result["summary"]), 300)

    def test_default_builtin_path_unchanged(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        executor.generate("fix bug")
        self.assertIsNone(executor.native_meta.native_backend_proof)

    def test_deepagents_backend_without_experimental_flag_no_call(self):
        import sys
        mock_create = MagicMock()
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            with patch.dict(sys.modules, {"deepagents": MagicMock()}):
                with patch("openshard.native.backends._import_deepagents_create", return_value=mock_create):
                    executor = NativeAgentExecutor(
                        provider=MagicMock(),
                        backend_name="deepagents",
                        experimental_deepagents_run=False,
                    )
                    executor.generate("fix bug")
        mock_create.assert_not_called()

    def test_proof_metadata_stored_on_native_meta(self):
        import sys
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = "summary text"
        mock_create = MagicMock(return_value=mock_agent)
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            with patch.dict(sys.modules, {"deepagents": MagicMock()}):
                with patch("openshard.native.backends._import_deepagents_create", return_value=mock_create):
                    executor = NativeAgentExecutor(
                        provider=MagicMock(),
                        backend_name="deepagents",
                        experimental_deepagents_run=True,
                        deepagents_model="mock-model",
                    )
                    executor.generate("fix bug")
        proof = executor.native_meta.native_backend_proof
        self.assertIsNotNone(proof)
        self.assertIn("mode", proof)
        self.assertIn("summary", proof)
        self.assertIn("backend", proof)

    def test_deepagents_default_tools_warning_path(self):
        import sys
        # Production code returns readonly_agent_unconfigured when deepagents_model is not
        # injected — proves accidental live invocation with default built-in tools cannot happen
        mock_create = MagicMock()
        fake_da = MagicMock()
        fake_da.create_deep_agent = mock_create
        with patch.dict(sys.modules, {"deepagents": fake_da}):
            with patch("openshard.native.backends._import_deepagents_create", return_value=mock_create):
                from openshard.native.backends import _default_deepagents_proof
                result = _default_deepagents_proof("fix bug", context={})
        mock_create.assert_not_called()
        self.assertEqual(result["mode"], "readonly_agent_unconfigured")

    def test_proof_metadata_compact_shape(self):
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = "short"
        mock_create = MagicMock(return_value=mock_agent)
        with patch("openshard.native.backends._import_deepagents_create", return_value=mock_create):
            from openshard.native.backends import _default_deepagents_proof
            result = _default_deepagents_proof(
                "fix bug", context={"deepagents_model": "mock-model"}
            )
        for key in ("backend", "available", "mode", "summary", "notes"):
            self.assertIn(key, result)
        self.assertIsInstance(result["summary"], str)
        self.assertIsInstance(result["notes"], list)


class TestNativeContextPacket(unittest.TestCase):
    """Unit tests for NativeContextPacket dataclass and build_native_context_packet()."""

    from openshard.native.context import (
        NativeContextPacket as _NativeContextPacket,
    )
    from openshard.native.context import (
        build_native_context_packet as _build_packet,
    )

    def _make_executor(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor, fake_gen

    def _build(self, **kwargs):
        from openshard.native.context import build_native_context_packet
        return build_native_context_packet(task=kwargs.pop("task", ""), **kwargs)

    def test_builder_defaults_empty(self):
        from openshard.native.context import build_native_context_packet
        packet = build_native_context_packet(task="")
        self.assertEqual(packet.read_search_count, 0)
        self.assertEqual(packet.test_marker_count, 0)
        self.assertEqual(packet.package_file_count, 0)
        self.assertEqual(packet.compact_paths, [])
        self.assertEqual(packet.selected_skills, [])
        self.assertEqual(packet.task_preview, "")

    def test_builder_task_preview_bounded(self):
        from openshard.native.context import build_native_context_packet
        long_task = "x" * 400
        packet = build_native_context_packet(task=long_task)
        self.assertEqual(len(packet.task_preview), 300)
        self.assertEqual(packet.task_preview, "x" * 300)

    def test_builder_includes_repo_context_source(self):
        from openshard.native.context import build_native_context_packet
        from openshard.native.repo_context import NativeRepoContextSummary
        summary = NativeRepoContextSummary(
            likely_stack_markers=["python", "pytest"],
            test_markers=["tests/"],
            package_files=["pyproject.toml", "setup.py"],
            total_files=10,
            top_level_dirs=["openshard"],
            truncated=False,
        )
        packet = build_native_context_packet(task="fix bug", repo_context_summary=summary)
        self.assertIn("repo_context", packet.sources)
        self.assertEqual(packet.repo_stack, ["python", "pytest"])
        self.assertEqual(packet.test_marker_count, 1)
        self.assertEqual(packet.package_file_count, 2)

    def test_builder_counts_read_search_findings(self):
        from openshard.native.context import build_native_context_packet
        findings = ["file:src/a.py", "test-marker:tests/", "package:pyproject.toml"]
        packet = build_native_context_packet(task="fix", read_search_findings=findings)
        self.assertEqual(packet.read_search_count, 3)
        self.assertIn("read_search", packet.sources)

    def test_builder_includes_backend(self):
        from openshard.native.context import build_native_context_packet
        packet = build_native_context_packet(task="t", native_backend="builtin")
        self.assertIn("backend", packet.sources)
        self.assertEqual(packet.backend, "builtin")

    def test_builder_includes_backend_proof_mode(self):
        from openshard.native.context import build_native_context_packet
        proof = {"mode": "strict", "available": True}
        packet = build_native_context_packet(task="t", native_backend_proof=proof)
        self.assertEqual(packet.backend_proof_mode, "strict")

    def test_builder_bounds_compact_paths(self):
        from openshard.native.context import build_native_context_packet
        findings = [f"file:src/f{i}.py" for i in range(10)]
        packet = build_native_context_packet(task="t", read_search_findings=findings)
        self.assertLessEqual(len(packet.compact_paths), 8)
        self.assertIn("context packet paths truncated", packet.warnings)

    def test_builder_includes_skills(self):
        from openshard.native.context import build_native_context_packet
        packet = build_native_context_packet(task="t", selected_skills=["python"])
        self.assertIn("skills", packet.sources)
        self.assertEqual(packet.selected_skills, ["python"])

    def test_builder_does_not_store_raw_content(self):
        import dataclasses

        from openshard.native.context import build_native_context_packet
        packet = build_native_context_packet(
            task="fix the bug",
            read_search_findings=["file:src/main.py"],
            selected_skills=["python"],
        )
        for f in dataclasses.fields(packet):
            val = getattr(packet, f.name)
            if isinstance(val, str):
                self.assertLess(len(val), 1000, f"field {f.name!r} looks like raw content")

    def test_executor_stores_context_packet(self):
        executor, fake_gen = self._make_executor()
        executor.generate("fix the bug")
        self.assertIsNotNone(executor.native_meta.context_packet)

    def test_executor_records_context_packet_loop_step(self):
        executor, fake_gen = self._make_executor()
        executor.generate("fix the bug")
        phases = executor.native_meta.native_loop_trace.phases()
        self.assertIn("context_packet", phases)

    def test_generate_builds_context_packet_before_plan(self):
        executor, fake_gen = self._make_executor()
        executor.generate("fix the bug")
        phases = executor.native_meta.native_loop_trace.phases()
        self.assertIn("context_packet", phases)
        self.assertIn("plan", phases)
        self.assertLess(phases.index("context_packet"), phases.index("plan"))

    def test_pipeline_serializes_context_packet(self):
        from dataclasses import asdict

        from openshard.native.context import NativeContextPacket
        packet = NativeContextPacket(
            task_preview="do something",
            sources=["backend", "repo_context"],
            repo_stack=["python"],
            backend="builtin",
        )
        d = asdict(packet)
        self.assertEqual(d["task_preview"], "do something")
        self.assertEqual(d["sources"], ["backend", "repo_context"])
        self.assertEqual(d["repo_stack"], ["python"])
        self.assertEqual(d["backend"], "builtin")


class TestNativeContextPacketUse(unittest.TestCase):
    """Tests for render_native_context_packet() and its integration into generate()."""

    def _make_executor(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor, fake_gen

    def _get_combined_context(self, fake_gen):
        kwargs = fake_gen.generate.call_args.kwargs
        return kwargs.get("skills_context", "")

    def test_render_empty_returns_empty_string(self):
        from openshard.native.context import render_native_context_packet
        self.assertEqual(render_native_context_packet(None), "")

    def test_render_includes_sources_and_paths(self):
        from openshard.native.context import NativeContextPacket, render_native_context_packet
        packet = NativeContextPacket(
            sources=["backend", "repo_context"],
            compact_paths=["file:src/main.py"],
            backend="builtin",
        )
        rendered = render_native_context_packet(packet)
        self.assertIn("OpenShard Native context packet:", rendered)
        self.assertIn("sources:", rendered)
        self.assertIn("compact paths:", rendered)
        self.assertIn("file:src/main.py", rendered)

    def test_render_does_not_include_raw_content(self):
        from openshard.native.context import NativeContextPacket, render_native_context_packet
        packet = NativeContextPacket(
            sources=["read_search"],
            compact_paths=["file:src/main.py"],
            warnings=["context packet paths truncated"],
        )
        rendered = render_native_context_packet(packet)
        self.assertNotIn("SECRET_RAW_FILE_CONTENT", rendered)
        self.assertNotIn("RAW_DIFF", rendered)
        self.assertNotIn("STDERR", rendered)
        self.assertNotIn("CHAIN_OF_THOUGHT", rendered)

    def test_generate_passes_context_packet_to_generator(self):
        executor, fake_gen = self._make_executor()
        executor.generate("fix the bug")
        combined = self._get_combined_context(fake_gen)
        self.assertIn("OpenShard Native context packet:", combined)

    def test_generate_preserves_existing_skills_context(self):
        executor, fake_gen = self._make_executor()
        executor.generate("fix the bug", skills_context="EXISTING_SKILLS_CONTEXT")
        combined = self._get_combined_context(fake_gen)
        self.assertIn("EXISTING_SKILLS_CONTEXT", combined)

    def test_generate_context_packet_includes_selected_skills(self):
        executor, fake_gen = self._make_executor()
        executor.native_meta.selected_skills = ["python", "pytest"]
        with patch("openshard.native.executor.match_builtin_skills", return_value=[]):
            with patch("openshard.native.executor.selected_skill_names", return_value=["python", "pytest"]):
                executor.generate("fix the bug")
        combined = self._get_combined_context(fake_gen)
        self.assertIn("selected skills:", combined)
        self.assertNotEqual(executor.native_meta.context_packet.selected_skills, [])

    def test_context_packet_built_after_skill_selection(self):
        executor, fake_gen = self._make_executor()
        executor.generate("fix the bug")
        self.assertEqual(
            executor.native_meta.context_packet.selected_skills,
            executor.native_meta.selected_skills,
        )

    def test_no_packet_no_extra_context(self):
        from openshard.native.context import render_native_context_packet
        combined = render_native_context_packet(None)
        self.assertNotIn("OpenShard Native context packet:", combined)

    def test_packet_context_is_bounded(self):
        from openshard.native.context import NativeContextPacket, render_native_context_packet
        packet = NativeContextPacket(
            sources=["backend", "repo_context", "read_search", "skills"],
            repo_stack=["python", "pytest", "django", "fastapi", "celery", "redis", "postgres", "docker"],
            test_marker_count=99,
            package_file_count=50,
            read_search_count=100,
            selected_skills=["python", "pytest", "django", "fastapi", "celery", "redis", "postgres", "docker"],
            backend="builtin",
            backend_available=True,
            backend_proof_mode="strict",
            compact_paths=[f"file:src/module_{i}.py" for i in range(8)],
            warnings=[f"warning {i}" for i in range(5)],
        )
        rendered = render_native_context_packet(packet)
        self.assertLess(len(rendered), 2000)


class TestNativeFileContext(unittest.TestCase):
    """Unit tests for NativeAgentExecutor._run_file_context_phase()."""

    def _make_executor(self, runner=None):
        executor = NativeAgentExecutor.__new__(NativeAgentExecutor)
        executor.native_meta = NativeRunMeta()
        executor._runner = runner
        return executor

    def _make_runner(self, responses=None):
        """Return a mock runner that returns NativeToolResult objects."""
        from openshard.native.tools import NativeToolResult
        runner = MagicMock()
        responses = responses or {}

        def _run(call):
            path = call.args.get("path", "")
            if path in responses:
                content, ok = responses[path]
            else:
                content, ok = "", False
            return NativeToolResult(
                tool_name=call.tool_name,
                ok=ok,
                output=content,
            )

        runner.run.side_effect = _run
        runner.trace_entry.side_effect = lambda call, result: {
            "tool": call.tool_name,
            "ok": result.ok,
            "approved": False,
            "output_chars": len(result.output),
            "error": result.error,
        }
        return runner

    def test_default_file_context_is_none(self):
        meta = NativeRunMeta()
        self.assertIsNone(meta.file_context)

    def test_no_runner_returns_early(self):
        executor = self._make_executor(runner=None)
        executor.native_meta.read_search_findings = ["file:src/main.py"]
        executor._run_file_context_phase()
        self.assertIsNone(executor.native_meta.file_context)

    def test_no_findings_returns_early(self):
        runner = self._make_runner()
        executor = self._make_executor(runner=runner)
        executor.native_meta.read_search_findings = []
        executor._run_file_context_phase()
        self.assertIsNone(executor.native_meta.file_context)

    def test_reads_bounded_number_of_files(self):
        responses = {f"file{i}.py": (f"content{i}", True) for i in range(6)}
        runner = self._make_runner(responses)
        executor = self._make_executor(runner=runner)
        executor.native_meta.read_search_findings = [
            f"file:{p}" for p in responses
        ]
        executor._run_file_context_phase()
        self.assertIsNotNone(executor.native_meta.file_context)
        self.assertLessEqual(executor.native_meta.file_context.files_read, 3)

    def test_skips_absolute_paths(self):
        runner = self._make_runner()
        executor = self._make_executor(runner=runner)
        executor.native_meta.read_search_findings = ["file:/etc/passwd"]
        executor._run_file_context_phase()
        runner.run.assert_not_called()

    def test_skips_traversal_paths(self):
        runner = self._make_runner()
        executor = self._make_executor(runner=runner)
        executor.native_meta.read_search_findings = ["file:../secret.py"]
        executor._run_file_context_phase()
        runner.run.assert_not_called()

    def test_skips_git_paths(self):
        runner = self._make_runner()
        executor = self._make_executor(runner=runner)
        executor.native_meta.read_search_findings = ["file:.git/config"]
        executor._run_file_context_phase()
        runner.run.assert_not_called()

    def test_stores_only_metadata_not_content(self):
        runner = self._make_runner({"src/main.py": ("def main(): pass", True)})
        executor = self._make_executor(runner=runner)
        executor.native_meta.read_search_findings = ["file:src/main.py"]
        executor._run_file_context_phase()
        fc = executor.native_meta.file_context
        self.assertIsNotNone(fc)
        self.assertFalse(hasattr(fc, "content"))
        self.assertIn("src/main.py", fc.paths)
        self.assertGreater(fc.total_chars, 0)

    def test_records_file_context_loop_step(self):
        runner = self._make_runner({"a.py": ("hello", True)})
        executor = self._make_executor(runner=runner)
        executor.native_meta.read_search_findings = ["file:a.py"]
        executor._run_file_context_phase()
        self.assertIn("file_context", executor.native_meta.native_loop_steps)

    def test_serializes_file_context(self):
        from dataclasses import asdict
        runner = self._make_runner({"a.py": ("hello", True)})
        executor = self._make_executor(runner=runner)
        executor.native_meta.read_search_findings = ["file:a.py"]
        executor._run_file_context_phase()
        fc = executor.native_meta.file_context
        self.assertIsNotNone(fc)
        d = asdict(fc)
        for key in ("files_read", "paths", "total_chars", "truncated", "warnings"):
            self.assertIn(key, d)

    def test_total_chars_bounded(self):
        from openshard.native.executor import _MAX_FILE_CONTEXT_TOTAL_CHARS
        big_content = "x" * (_MAX_FILE_CONTEXT_TOTAL_CHARS + 1)
        runner = self._make_runner({
            "a.py": (big_content, True),
            "b.py": ("small", True),
            "c.py": ("small", True),
        })
        executor = self._make_executor(runner=runner)
        executor.native_meta.read_search_findings = [
            "file:a.py", "file:b.py", "file:c.py"
        ]
        executor._run_file_context_phase()
        fc = executor.native_meta.file_context
        self.assertIsNotNone(fc)
        self.assertTrue(fc.truncated)

    def test_failed_read_records_warning(self):
        runner = self._make_runner({"tests/": ("", False)})
        executor = self._make_executor(runner=runner)
        executor.native_meta.read_search_findings = ["test-marker:tests/"]
        executor._run_file_context_phase()
        fc = executor.native_meta.file_context
        self.assertIsNotNone(fc)
        self.assertEqual(fc.files_read, 0)
        self.assertTrue(any("tests/" in w for w in fc.warnings))


class TestNativeContextQualityScore(unittest.TestCase):

    def test_empty_packet_gives_weak_score(self):
        packet = NativeContextPacket()
        result = build_native_context_quality_score(packet)
        self.assertEqual(result.level, "weak")
        self.assertLess(result.score, 35)

    def test_strong_packet_gives_high_score(self):
        packet = NativeContextPacket(
            sources=["repo_context", "read_search", "skills"],
            repo_stack=["python"],
            test_marker_count=2,
            selected_skills=["testing"],
            backend="builtin",
            compact_paths=["file:src/foo.py"],
            file_context_files=1,
        )
        result = build_native_context_quality_score(packet)
        self.assertGreaterEqual(result.score, 80)
        self.assertEqual(result.level, "strong")

    def test_reasons_are_compact_strings(self):
        packet = NativeContextPacket(
            sources=["repo_context"],
            backend="builtin",
        )
        result = build_native_context_quality_score(packet)
        for reason in result.reasons:
            self.assertIsInstance(reason, str)
            self.assertLess(len(reason), 50)

    def test_warnings_is_list_no_raw_content(self):
        packet = NativeContextPacket()
        result = build_native_context_quality_score(packet)
        self.assertIsInstance(result.warnings, list)
        for w in result.warnings:
            self.assertIsInstance(w, str)

    def test_score_capped_at_100(self):
        packet = NativeContextPacket(
            sources=["repo_context", "read_search", "skills"],
            repo_stack=["python", "django"],
            test_marker_count=3,
            selected_skills=["testing", "linting"],
            backend="deepagents",
            compact_paths=["file:a.py", "file:b.py"],
            file_context_files=2,
        )
        result = build_native_context_quality_score(packet)
        self.assertLessEqual(result.score, 100)

    def test_native_run_meta_has_context_quality_score_field(self):
        meta = NativeRunMeta()
        self.assertIsNone(meta.context_quality_score)

    def test_file_context_score_uses_file_context_files_not_compact_paths(self):
        packet_with_file_path_but_zero_count = NativeContextPacket(
            compact_paths=["file:src/foo.py"],
            file_context_files=0,
        )
        result = build_native_context_quality_score(packet_with_file_path_but_zero_count)
        self.assertNotIn("file_context", result.reasons)

        packet_with_count_but_no_file_prefix = NativeContextPacket(
            compact_paths=["test-marker:tests/"],
            file_context_files=1,
        )
        result2 = build_native_context_quality_score(packet_with_count_but_no_file_prefix)
        self.assertIn("file_context", result2.reasons)

    def _make_executor(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor

    def test_build_context_quality_score_sets_meta_field(self):
        executor = self._make_executor()
        executor.native_meta.context_packet = NativeContextPacket(
            sources=["repo_context"],
            backend="builtin",
        )
        score = executor.build_context_quality_score()
        self.assertIsNotNone(executor.native_meta.context_quality_score)
        self.assertIsInstance(score, NativeContextQualityScore)

    def test_build_context_quality_score_works_without_packet(self):
        executor = self._make_executor()
        executor.native_meta.context_packet = None
        score = executor.build_context_quality_score()
        self.assertEqual(score.level, "weak")

    def test_loop_trace_records_context_quality_phase(self):
        executor = self._make_executor()
        executor.native_meta.context_packet = NativeContextPacket()
        executor.build_context_quality_score()
        phases = executor.native_meta.native_loop_trace.phases()
        self.assertIn("context_quality", phases)


class TestNativeContextQualityAdvisory(unittest.TestCase):
    """Tests for NativeContextQualityAdvisory and build_native_context_quality_advisory."""

    def _make_executor(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor

    def _score(self, level: str) -> NativeContextQualityScore:
        return NativeContextQualityScore(score=50, max_score=100, level=level)

    def test_builder_none_score_returns_unknown(self):
        advisory = build_native_context_quality_advisory(None)
        self.assertIsInstance(advisory, NativeContextQualityAdvisory)
        self.assertEqual(advisory.level, "unknown")
        self.assertFalse(advisory.should_block)
        self.assertTrue(len(advisory.warnings) > 0)

    def test_strong_score_recommendation(self):
        advisory = build_native_context_quality_advisory(self._score("strong"))
        self.assertEqual(advisory.level, "strong")
        self.assertIn("strong", advisory.recommendation)
        self.assertEqual(advisory.warnings, [])
        self.assertFalse(advisory.should_block)

    def test_good_score_recommendation(self):
        advisory = build_native_context_quality_advisory(self._score("good"))
        self.assertEqual(advisory.level, "good")
        self.assertIn("good", advisory.recommendation)
        self.assertEqual(advisory.warnings, [])
        self.assertFalse(advisory.should_block)

    def test_fair_score_has_warning_not_blocking(self):
        advisory = build_native_context_quality_advisory(self._score("fair"))
        self.assertEqual(advisory.level, "fair")
        self.assertFalse(advisory.should_block)
        self.assertTrue(len(advisory.warnings) > 0)

    def test_weak_score_has_warning_not_blocking(self):
        advisory = build_native_context_quality_advisory(self._score("weak"))
        self.assertEqual(advisory.level, "weak")
        self.assertFalse(advisory.should_block)
        self.assertTrue(len(advisory.warnings) > 0)

    def test_advisory_no_raw_content(self):
        for level in ("strong", "good", "fair", "weak"):
            advisory = build_native_context_quality_advisory(self._score(level))
            self.assertNotIn("diff --git", advisory.recommendation)
            self.assertNotIn("\n", advisory.recommendation)
            for w in advisory.warnings:
                self.assertNotIn("diff --git", w)
                self.assertLess(len(w), 200)

    def test_native_run_meta_advisory_default_none(self):
        meta = NativeRunMeta()
        self.assertIsNone(meta.context_quality_advisory)

    def test_build_context_quality_advisory_sets_meta(self):
        executor = self._make_executor()
        executor.native_meta.context_quality_score = self._score("good")
        advisory = executor.build_context_quality_advisory()
        self.assertIsNotNone(executor.native_meta.context_quality_advisory)
        self.assertIsInstance(advisory, NativeContextQualityAdvisory)
        self.assertEqual(executor.native_meta.context_quality_advisory.level, "good")

    def test_build_context_quality_advisory_records_loop_step(self):
        executor = self._make_executor()
        executor.native_meta.context_quality_score = self._score("fair")
        executor.build_context_quality_advisory()
        phases = executor.native_meta.native_loop_trace.phases()
        self.assertIn("context_quality_advisory", phases)

    def test_generate_builds_advisory(self):
        executor = self._make_executor()
        executor.generate("add a docstring")
        self.assertIsNotNone(executor.native_meta.context_quality_advisory)
        self.assertIsInstance(
            executor.native_meta.context_quality_advisory, NativeContextQualityAdvisory
        )

    def test_pipeline_serializes_context_quality_advisory(self):
        native_mock = _make_native_mock()
        native_mock.native_meta.context_quality_advisory = NativeContextQualityAdvisory(
            level="good",
            recommendation="context is good enough for normal generation",
            should_block=False,
            warnings=[],
        )
        _, _, logged = TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertIn("context_quality_advisory", logged)
        cqa = logged["context_quality_advisory"]
        self.assertIsNotNone(cqa)
        self.assertEqual(cqa["level"], "good")
        self.assertFalse(cqa["should_block"])


class TestNativeContextQualityAdvisoryUse(unittest.TestCase):
    """Tests for render_native_context_quality_advisory and its injection into generate()."""

    def _make_executor(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor, fake_gen

    def _advisory(self, level: str = "weak") -> NativeContextQualityAdvisory:
        return NativeContextQualityAdvisory(
            level=level,
            recommendation=f"context is {level}; prefer smaller changes",
            should_block=False,
            warnings=[f"{level} warning text"],
        )

    def test_render_none_returns_empty_string(self):
        self.assertEqual(render_native_context_quality_advisory(None), "")

    def test_render_includes_level_recommendation_and_should_block(self):
        advisory = self._advisory("fair")
        rendered = render_native_context_quality_advisory(advisory)
        self.assertIn("level: fair", rendered)
        self.assertIn("recommendation:", rendered)
        self.assertIn("should block:", rendered)

    def test_render_does_not_include_warnings(self):
        advisory = self._advisory("weak")
        rendered = render_native_context_quality_advisory(advisory)
        for warning in advisory.warnings:
            self.assertNotIn(warning, rendered)

    def test_generate_passes_advisory_to_generator_context(self):
        executor, fake_gen = self._make_executor()
        executor.generate("add a docstring")
        _, kwargs = fake_gen.generate.call_args
        self.assertIn("OpenShard Native context advisory:", kwargs["skills_context"])

    def test_generate_preserves_existing_skills_context(self):
        executor, fake_gen = self._make_executor()
        executor.generate("add a docstring", skills_context="caller hint")
        _, kwargs = fake_gen.generate.call_args
        self.assertIn("caller hint", kwargs["skills_context"])
        self.assertIn("OpenShard Native context advisory:", kwargs["skills_context"])

    def test_advisory_context_does_not_include_raw_content(self):
        forbidden = [
            "SECRET_RAW_FILE_CONTENT",
            "RAW_DIFF",
            "STDERR",
            "CHAIN_OF_THOUGHT",
        ]
        advisory = NativeContextQualityAdvisory(
            level="weak",
            recommendation="context is weak; prefer smaller changes",
            should_block=False,
            warnings=["SECRET_RAW_FILE_CONTENT", "RAW_DIFF", "STDERR", "CHAIN_OF_THOUGHT"],
        )
        rendered = render_native_context_quality_advisory(advisory)
        for token in forbidden:
            self.assertNotIn(token, rendered)


class TestNativeChangeBudget(unittest.TestCase):
    """Tests for NativeChangeBudget, build_native_change_budget, and render_native_change_budget."""

    def _make_executor(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor, fake_gen

    def _advisory(self, level: str) -> NativeContextQualityAdvisory:
        return NativeContextQualityAdvisory(
            level=level,
            recommendation=f"context is {level}",
            should_block=False,
            warnings=[],
        )

    def test_builder_none_defaults_small(self):
        budget = build_native_change_budget(None)
        self.assertEqual(budget.level, "unknown")
        self.assertEqual(budget.max_files, 1)
        self.assertEqual(budget.max_change_size, "small")
        self.assertTrue(any("advisory missing" in w for w in budget.warnings))

    def test_builder_weak_allows_one_file(self):
        budget = build_native_change_budget(self._advisory("weak"))
        self.assertEqual(budget.max_files, 1)
        self.assertEqual(budget.level, "weak")

    def test_builder_fair_allows_two_files(self):
        budget = build_native_change_budget(self._advisory("fair"))
        self.assertEqual(budget.max_files, 2)
        self.assertEqual(budget.level, "fair")

    def test_builder_good_allows_three_files(self):
        budget = build_native_change_budget(self._advisory("good"))
        self.assertEqual(budget.max_files, 3)
        self.assertEqual(budget.level, "good")

    def test_builder_strong_allows_five_files(self):
        budget = build_native_change_budget(self._advisory("strong"))
        self.assertEqual(budget.max_files, 5)
        self.assertEqual(budget.level, "strong")

    def test_render_none_returns_empty_string(self):
        self.assertEqual(render_native_change_budget(None), "")

    def test_render_includes_level_files_size_guidance(self):
        budget = NativeChangeBudget(
            level="good",
            max_files=3,
            max_change_size="normal",
            guidance="normal generation is acceptable",
            warnings=[],
        )
        rendered = render_native_change_budget(budget)
        self.assertIn("level: good", rendered)
        self.assertIn("max files: 3", rendered)
        self.assertIn("max change size: normal", rendered)
        self.assertIn("guidance: normal generation is acceptable", rendered)

    def test_render_does_not_include_warnings(self):
        budget = NativeChangeBudget(
            level="fair",
            max_files=2,
            max_change_size="small",
            guidance="prefer a cautious change",
            warnings=["SECRET_WARNING_TEXT"],
        )
        rendered = render_native_change_budget(budget)
        self.assertNotIn("SECRET_WARNING_TEXT", rendered)

    def test_executor_stores_change_budget(self):
        executor, _ = self._make_executor()
        executor.native_meta.context_quality_advisory = self._advisory("good")
        budget = executor.build_change_budget()
        self.assertIsInstance(budget, NativeChangeBudget)
        self.assertIsNotNone(executor.native_meta.change_budget)
        self.assertIs(executor.native_meta.change_budget, budget)

    def test_executor_records_change_budget_loop_step(self):
        executor, _ = self._make_executor()
        executor.native_meta.context_quality_advisory = self._advisory("fair")
        executor.build_change_budget()
        phases = executor.native_meta.native_loop_trace.phases()
        self.assertIn("change_budget", phases)

    def test_generate_passes_change_budget_to_generator_context(self):
        executor, fake_gen = self._make_executor()
        executor.generate("add a function")
        _, kwargs = fake_gen.generate.call_args
        self.assertIn("OpenShard Native change budget:", kwargs["skills_context"])

    def test_generate_preserves_existing_skills_context(self):
        executor, fake_gen = self._make_executor()
        executor.generate("add a function", skills_context="caller hint")
        _, kwargs = fake_gen.generate.call_args
        self.assertIn("caller hint", kwargs["skills_context"])
        self.assertIn("OpenShard Native change budget:", kwargs["skills_context"])

    def test_no_raw_content_in_budget_context(self):
        forbidden = ["SECRET_RAW_FILE_CONTENT", "RAW_DIFF", "STDERR", "CHAIN_OF_THOUGHT"]
        budget = NativeChangeBudget(
            level="weak",
            max_files=1,
            max_change_size="small",
            guidance="prefer the smallest safe change",
            warnings=["SECRET_RAW_FILE_CONTENT", "RAW_DIFF", "STDERR", "CHAIN_OF_THOUGHT"],
        )
        rendered = render_native_change_budget(budget)
        for token in forbidden:
            self.assertNotIn(token, rendered)

    def test_pipeline_serializes_change_budget(self):
        native_mock = _make_native_mock()
        native_mock.native_meta.change_budget = NativeChangeBudget(
            level="good",
            max_files=3,
            max_change_size="normal",
            guidance="normal generation is acceptable",
            warnings=[],
        )
        _, _, logged = TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertIn("change_budget", logged)
        cb = logged["change_budget"]
        self.assertIsNotNone(cb)
        self.assertEqual(cb["level"], "good")
        self.assertEqual(cb["max_files"], 3)
        self.assertEqual(cb["max_change_size"], "normal")


class TestNativeChangeBudgetPreview(unittest.TestCase):
    """Tests for NativeChangeBudgetPreview, build_native_change_budget_preview, and executor integration."""

    def _make_executor(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor, fake_gen

    def _budget(self, max_files: int) -> NativeChangeBudget:
        return NativeChangeBudget(
            level="good",
            max_files=max_files,
            max_change_size="normal",
            guidance="",
            warnings=[],
        )

    def _proposal(self, file_count: int) -> NativePatchProposal:
        return NativePatchProposal(
            file_count=file_count,
            files=[f"file{i}.py" for i in range(file_count)],
            change_types=["modify"] * file_count,
            summaries=[],
            warnings=[],
        )

    # --- builder unit tests ---

    def test_builder_missing_budget_allows_with_warning(self):
        preview = build_native_change_budget_preview(budget=None, proposal=self._proposal(2))
        self.assertEqual(preview.action, "allow")
        self.assertTrue(len(preview.warnings) > 0)

    def test_builder_missing_proposal_zero_files(self):
        preview = build_native_change_budget_preview(budget=self._budget(3), proposal=None)
        self.assertEqual(preview.proposed_files, 0)

    def test_builder_within_budget_allows(self):
        preview = build_native_change_budget_preview(budget=self._budget(3), proposal=self._proposal(1))
        self.assertTrue(preview.within_budget)
        self.assertFalse(preview.would_exceed_budget)
        self.assertEqual(preview.action, "allow")
        self.assertEqual(preview.warnings, [])

    def test_builder_exceeding_budget_warns(self):
        preview = build_native_change_budget_preview(budget=self._budget(2), proposal=self._proposal(4))
        self.assertFalse(preview.within_budget)
        self.assertTrue(preview.would_exceed_budget)
        self.assertEqual(preview.action, "warn")
        self.assertTrue(len(preview.warnings) > 0)

    def test_builder_counts_only_no_content(self):
        preview = build_native_change_budget_preview(budget=self._budget(2), proposal=self._proposal(4))
        self.assertIsInstance(preview, NativeChangeBudgetPreview)
        self.assertFalse(hasattr(preview, "files"))
        self.assertFalse(hasattr(preview, "diff"))
        self.assertFalse(hasattr(preview, "content"))

    # --- executor integration tests ---

    def test_executor_stores_budget_preview(self):
        executor, _ = self._make_executor()
        executor.native_meta.change_budget = self._budget(3)
        executor.native_meta.patch_proposal = self._proposal(1)
        preview = executor.build_change_budget_preview()
        self.assertIsInstance(preview, NativeChangeBudgetPreview)
        self.assertIsNotNone(executor.native_meta.change_budget_preview)
        self.assertIs(executor.native_meta.change_budget_preview, preview)

    def test_executor_records_budget_preview_loop_step(self):
        executor, _ = self._make_executor()
        executor.native_meta.change_budget = self._budget(3)
        executor.native_meta.patch_proposal = self._proposal(2)
        executor.build_change_budget_preview()
        phases = executor.native_meta.native_loop_trace.phases()
        self.assertIn("change_budget_preview", phases)

    # --- pipeline tests ---

    def test_pipeline_calls_budget_preview_after_patch_proposal(self):
        native_mock = _make_native_mock()
        native_mock.native_meta.change_budget = self._budget(3)
        TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        native_mock.build_change_budget_preview.assert_called()

    def test_pipeline_serializes_budget_preview(self):
        native_mock = _make_native_mock()
        native_mock.native_meta.change_budget_preview = NativeChangeBudgetPreview(
            budget_max_files=3,
            proposed_files=1,
            within_budget=True,
            would_exceed_budget=False,
            action="allow",
            warnings=[],
        )
        _, _, logged = TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertIn("change_budget_preview", logged)
        cbp = logged["change_budget_preview"]
        self.assertIsNotNone(cbp)
        self.assertEqual(cbp["budget_max_files"], 3)
        self.assertEqual(cbp["proposed_files"], 1)
        self.assertEqual(cbp["action"], "allow")

    def test_budget_preview_does_not_block_write(self):
        from openshard.execution.generator import ChangedFile
        native_mock = _make_native_mock()
        safe_file = ChangedFile(
            path="out.txt", content="ok", change_type="create", summary="created"
        )
        r = MagicMock()
        r.files = [safe_file]
        r.summary = "done"
        r.notes = []
        r.usage = None
        native_mock.generate.return_value = r
        native_mock.native_meta.change_budget_preview = NativeChangeBudgetPreview(
            budget_max_files=1,
            proposed_files=5,
            within_budget=False,
            would_exceed_budget=True,
            action="warn",
            warnings=["proposal has 5 files but budget allows 1"],
        )
        result, _, _ = TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertEqual(result.exit_code, 0)


class TestNativeChangeBudgetSoftGate(unittest.TestCase):
    """Tests for NativeChangeBudgetSoftGate, build_native_change_budget_soft_gate, and executor/pipeline integration."""

    def _make_executor(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor, fake_gen

    def _preview(self, would_exceed: bool, proposed: int = 4, max_files: int = 2) -> NativeChangeBudgetPreview:
        return NativeChangeBudgetPreview(
            budget_max_files=max_files,
            proposed_files=proposed,
            within_budget=not would_exceed,
            would_exceed_budget=would_exceed,
            action="warn" if would_exceed else "allow",
            warnings=(
                [f"proposal has {proposed} files but budget allows {max_files}"]
                if would_exceed
                else []
            ),
        )

    # --- builder unit tests ---

    def test_builder_missing_preview_allows_with_warning(self):
        gate = build_native_change_budget_soft_gate(None)
        self.assertEqual(gate.action, "allow")
        self.assertFalse(gate.requires_approval)
        self.assertTrue(len(gate.warnings) > 0)

    def test_builder_within_budget_allows(self):
        preview = self._preview(would_exceed=False, proposed=1, max_files=3)
        gate = build_native_change_budget_soft_gate(preview)
        self.assertFalse(gate.requires_approval)
        self.assertEqual(gate.action, "allow")
        self.assertEqual(gate.warnings, [])

    def test_builder_exceeding_budget_requires_approval(self):
        preview = self._preview(would_exceed=True)
        gate = build_native_change_budget_soft_gate(preview)
        self.assertTrue(gate.requires_approval)
        self.assertEqual(gate.action, "require_approval")
        self.assertIn("proposal exceeds advisory change budget", gate.reason)

    def test_builder_does_not_store_raw_content(self):
        gate = build_native_change_budget_soft_gate(self._preview(would_exceed=True))
        self.assertIsInstance(gate, NativeChangeBudgetSoftGate)
        self.assertFalse(hasattr(gate, "files"))
        self.assertFalse(hasattr(gate, "diff"))
        self.assertFalse(hasattr(gate, "content"))

    # --- executor integration tests ---

    def test_executor_stores_soft_gate(self):
        executor, _ = self._make_executor()
        executor.native_meta.change_budget_preview = self._preview(would_exceed=False, proposed=1, max_files=3)
        gate = executor.build_change_budget_soft_gate()
        self.assertIsInstance(gate, NativeChangeBudgetSoftGate)
        self.assertIsNotNone(executor.native_meta.change_budget_soft_gate)
        self.assertIs(executor.native_meta.change_budget_soft_gate, gate)

    def test_executor_records_soft_gate_loop_step(self):
        executor, _ = self._make_executor()
        executor.native_meta.change_budget_preview = self._preview(would_exceed=True)
        executor.build_change_budget_soft_gate()
        phases = executor.native_meta.native_loop_trace.phases()
        self.assertIn("change_budget_soft_gate", phases)

    # --- pipeline integration tests ---

    def test_pipeline_calls_soft_gate_after_budget_preview(self):
        native_mock = _make_native_mock()
        native_mock.native_meta.change_budget_preview = self._preview(would_exceed=False, proposed=1, max_files=3)
        native_mock.build_change_budget_soft_gate.return_value = NativeChangeBudgetSoftGate(
            requires_approval=False, reason="within budget", action="allow"
        )
        TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        native_mock.build_change_budget_soft_gate.assert_called()

    def test_pipeline_serializes_soft_gate(self):
        native_mock = _make_native_mock()
        native_mock.native_meta.change_budget_soft_gate = NativeChangeBudgetSoftGate(
            requires_approval=False,
            reason="within budget",
            action="allow",
            warnings=[],
        )
        native_mock.build_change_budget_soft_gate.return_value = native_mock.native_meta.change_budget_soft_gate
        _, _, logged = TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertIn("change_budget_soft_gate", logged)
        cbsg = logged["change_budget_soft_gate"]
        self.assertIsNotNone(cbsg)
        self.assertEqual(cbsg["action"], "allow")
        self.assertEqual(cbsg["requires_approval"], False)

    def test_soft_gate_does_not_block_when_within_budget(self):
        native_mock = _make_native_mock()
        native_mock.build_change_budget_soft_gate.return_value = NativeChangeBudgetSoftGate(
            requires_approval=False, reason="within budget", action="allow"
        )
        result, _, _ = TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertEqual(result.exit_code, 0)

    def test_soft_gate_requires_approval_when_exceeded(self):
        native_mock = _make_native_mock()
        native_mock.build_change_budget_soft_gate.return_value = NativeChangeBudgetSoftGate(
            requires_approval=True,
            reason="proposal exceeds advisory change budget",
            action="require_approval",
        )
        with patch("openshard.run.pipeline.confirm_or_abort") as mock_confirm:
            TestNativeVerificationLoop()._run_write_simple(
                native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
            )
        mock_confirm.assert_called_once_with("proposal exceeds advisory change budget")


class TestNativeApprovalRequest(unittest.TestCase):
    """Tests for NativeApprovalRequest, build_native_budget_gate_approval_request, and pipeline integration."""

    def _gate(self, requires: bool, action: str = "allow", warnings: list | None = None) -> NativeChangeBudgetSoftGate:
        return NativeChangeBudgetSoftGate(
            requires_approval=requires,
            reason="proposal exceeds advisory change budget" if requires else "within budget",
            action=action,
            warnings=warnings or [],
        )

    def _preview(self, proposed: int = 4, max_files: int = 2) -> NativeChangeBudgetPreview:
        return NativeChangeBudgetPreview(
            budget_max_files=max_files,
            proposed_files=proposed,
            within_budget=proposed <= max_files,
            would_exceed_budget=proposed > max_files,
            action="warn" if proposed > max_files else "allow",
        )

    # --- builder unit tests ---

    def test_builder_missing_gate_allows_with_warning(self):
        result = build_native_budget_gate_approval_request(gate=None, preview=None)
        self.assertFalse(result.requires_approval)
        self.assertEqual(result.action, "allow")
        self.assertTrue(len(result.warnings) > 0)
        self.assertEqual(result.source, "change_budget_soft_gate")

    def test_builder_no_approval_needed(self):
        gate = self._gate(requires=False, action="allow")
        preview = self._preview(proposed=1, max_files=3)
        result = build_native_budget_gate_approval_request(gate=gate, preview=preview)
        self.assertFalse(result.requires_approval)
        self.assertEqual(result.action, "allow")
        self.assertEqual(result.prompt, "")
        self.assertEqual(result.proposed_files, 1)
        self.assertEqual(result.budget_max_files, 3)

    def test_builder_required_approval_builds_prompt(self):
        gate = self._gate(requires=True, action="require_approval", warnings=["exceeds budget"])
        preview = self._preview(proposed=4, max_files=2)
        result = build_native_budget_gate_approval_request(gate=gate, preview=preview)
        self.assertTrue(result.requires_approval)
        self.assertIn("4", result.prompt)
        self.assertIn("2", result.prompt)
        self.assertIn("Proceed?", result.prompt)
        self.assertEqual(result.proposed_files, 4)
        self.assertEqual(result.budget_max_files, 2)

    def test_builder_counts_only_no_raw_content(self):
        gate = self._gate(requires=True, action="require_approval")
        preview = self._preview(proposed=3, max_files=1)
        result = build_native_budget_gate_approval_request(gate=gate, preview=preview)
        self.assertIsInstance(result, NativeApprovalRequest)
        self.assertFalse(hasattr(result, "files"))
        self.assertFalse(hasattr(result, "diff"))
        self.assertFalse(hasattr(result, "content"))

    # --- executor integration tests ---

    def test_executor_stores_approval_request(self):
        from unittest.mock import patch as _patch

        from openshard.native.executor import NativeAgentExecutor
        fake_gen = _make_generator_mock()
        with _patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        executor.native_meta.change_budget_soft_gate = self._gate(requires=False)
        executor.native_meta.change_budget_preview = self._preview(proposed=1, max_files=3)
        request = executor.build_budget_gate_approval_request()
        self.assertIsInstance(request, NativeApprovalRequest)
        self.assertIsNotNone(executor.native_meta.approval_request)
        self.assertIs(executor.native_meta.approval_request, request)

    def test_executor_records_approval_request_loop_step(self):
        from unittest.mock import patch as _patch

        from openshard.native.executor import NativeAgentExecutor
        fake_gen = _make_generator_mock()
        with _patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        executor.native_meta.change_budget_soft_gate = self._gate(requires=True, action="require_approval")
        executor.native_meta.change_budget_preview = self._preview(proposed=4, max_files=2)
        executor.build_budget_gate_approval_request()
        phases = executor.native_meta.native_loop_trace.phases()
        self.assertIn("approval_request", phases)

    # --- pipeline integration tests ---

    def test_pipeline_builds_approval_request_after_soft_gate(self):
        native_mock = _make_native_mock()
        native_mock.build_change_budget_soft_gate.return_value = NativeChangeBudgetSoftGate(
            requires_approval=False, reason="within budget", action="allow"
        )
        TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        native_mock.build_budget_gate_approval_request.assert_called()

    def test_pipeline_uses_approval_request_prompt_for_confirm(self):
        native_mock = _make_native_mock()
        native_mock.build_change_budget_soft_gate.return_value = NativeChangeBudgetSoftGate(
            requires_approval=True,
            reason="proposal exceeds advisory change budget",
            action="require_approval",
        )
        structured_prompt = "Proposal exceeds advisory change budget: 4 files proposed, budget allows 2. Proceed?"
        native_mock.build_budget_gate_approval_request.return_value = NativeApprovalRequest(
            source="change_budget_soft_gate",
            requires_approval=True,
            reason="proposal exceeds advisory change budget",
            action="require_approval",
            proposed_files=4,
            budget_max_files=2,
            prompt=structured_prompt,
        )
        with patch("openshard.run.pipeline.confirm_or_abort") as mock_confirm:
            TestNativeVerificationLoop()._run_write_simple(
                native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
            )
        mock_confirm.assert_called_once_with(structured_prompt)

    def test_pipeline_serializes_approval_request(self):
        native_mock = _make_native_mock()
        approval = NativeApprovalRequest(
            source="change_budget_soft_gate",
            requires_approval=False,
            reason="within budget",
            action="allow",
            proposed_files=1,
            budget_max_files=3,
        )
        native_mock.native_meta.approval_request = approval
        _, _, logged = TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertIn("approval_request", logged)
        ar = logged["approval_request"]
        self.assertIsNotNone(ar)
        self.assertEqual(ar["source"], "change_budget_soft_gate")
        self.assertEqual(ar["requires_approval"], False)

    def test_no_prompt_rendered_when_approval_not_required(self):
        native_mock = _make_native_mock()
        native_mock.build_change_budget_soft_gate.return_value = NativeChangeBudgetSoftGate(
            requires_approval=False, reason="within budget", action="allow"
        )
        with patch("openshard.run.pipeline.confirm_or_abort") as mock_confirm:
            TestNativeVerificationLoop()._run_write_simple(
                native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
            )
        mock_confirm.assert_not_called()


class TestNativeApprovalReceipt(unittest.TestCase):
    """Tests for NativeApprovalReceipt, build_native_approval_receipt, and pipeline integration."""

    def _request(self, requires: bool = True, source: str = "change_budget_soft_gate") -> NativeApprovalRequest:
        return NativeApprovalRequest(
            source=source,
            requires_approval=requires,
            reason="proposal exceeds advisory change budget" if requires else "within budget",
            action="require_approval" if requires else "allow",
        )

    # --- builder unit tests ---

    def test_builder_missing_request_returns_safe_default(self):
        result = build_native_approval_receipt(None, granted=True)
        self.assertFalse(result.requested)
        self.assertEqual(result.action, "allow")
        self.assertEqual(result.reason, "approval request missing")
        self.assertEqual(result.source, "")

    def test_builder_copies_source_action_reason(self):
        request = self._request(requires=True)
        result = build_native_approval_receipt(request, granted=True)
        self.assertEqual(result.source, request.source)
        self.assertEqual(result.action, request.action)
        self.assertEqual(result.reason, request.reason)

    def test_builder_granted_true_records_granted_true(self):
        request = self._request(requires=True)
        result = build_native_approval_receipt(request, granted=True)
        self.assertTrue(result.granted)

    # --- executor integration tests ---

    def test_executor_stores_approval_receipt(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        executor.native_meta.approval_request = self._request(requires=True)
        receipt = executor.build_approval_receipt(granted=True)
        self.assertIsInstance(receipt, NativeApprovalReceipt)
        self.assertIsNotNone(executor.native_meta.approval_receipt)
        self.assertIs(executor.native_meta.approval_receipt, receipt)

    def test_executor_records_approval_receipt_loop_step(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        executor.native_meta.approval_request = self._request(requires=True)
        executor.build_approval_receipt(granted=True)
        phases = executor.native_meta.native_loop_trace.phases()
        self.assertIn("approval_receipt", phases)

    # --- pipeline integration tests ---

    def test_pipeline_builds_receipt_after_confirm_or_abort_succeeds(self):
        native_mock = _make_native_mock()
        native_mock.build_change_budget_soft_gate.return_value = NativeChangeBudgetSoftGate(
            requires_approval=True,
            reason="proposal exceeds advisory change budget",
            action="require_approval",
        )
        native_mock.build_budget_gate_approval_request.return_value = NativeApprovalRequest(
            source="change_budget_soft_gate",
            requires_approval=True,
            reason="proposal exceeds advisory change budget",
            action="require_approval",
            prompt="Proposal exceeds advisory change budget: 4 files proposed. Proceed?",
        )
        with patch("openshard.run.pipeline.confirm_or_abort"):
            TestNativeVerificationLoop()._run_write_simple(
                native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
            )
        native_mock.build_approval_receipt.assert_called_once_with(granted=True)

    def test_pipeline_does_not_build_receipt_when_approval_not_required(self):
        native_mock = _make_native_mock()
        native_mock.build_change_budget_soft_gate.return_value = NativeChangeBudgetSoftGate(
            requires_approval=False, reason="within budget", action="allow"
        )
        TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        native_mock.build_approval_receipt.assert_not_called()

    def test_pipeline_serializes_approval_receipt(self):
        native_mock = _make_native_mock()
        receipt = NativeApprovalReceipt(
            source="change_budget_soft_gate",
            requested=True,
            granted=True,
            action="require_approval",
            reason="proposal exceeds advisory change budget",
        )
        native_mock.native_meta.approval_receipt = receipt
        _, _, logged = TestNativeVerificationLoop()._run_write_simple(
            native_mock, plan=_safe_plan(), verify_returns=[(0, "ok")]
        )
        self.assertIn("approval_receipt", logged)
        ar = logged["approval_receipt"]
        self.assertIsNotNone(ar)
        self.assertEqual(ar["source"], "change_budget_soft_gate")
        self.assertTrue(ar["granted"])

    # --- no-leakage tests ---

    def test_receipt_does_not_store_prompt_text(self):
        result = build_native_approval_receipt(self._request(), granted=True)
        self.assertIsInstance(result, NativeApprovalReceipt)
        self.assertFalse(hasattr(result, "prompt"))

    def test_receipt_does_not_store_raw_content(self):
        result = build_native_approval_receipt(self._request(), granted=True)
        self.assertFalse(hasattr(result, "files"))
        self.assertFalse(hasattr(result, "diff"))
        self.assertFalse(hasattr(result, "content"))


class TestNativeRunMetaOSNField(unittest.TestCase):
    """osn_loop field on NativeRunMeta."""

    def test_defaults_none(self):
        meta = NativeRunMeta()
        self.assertIsNone(meta.osn_loop)

    def test_accepts_osn_loop_meta(self):
        meta = NativeRunMeta()
        loop = OSNLoopMeta(enabled=True, steps_run=2, steps_queued=3, max_steps=5, terminated_reason="complete")
        meta.osn_loop = loop
        self.assertIs(meta.osn_loop, loop)
        self.assertTrue(meta.osn_loop.enabled)
        self.assertEqual(meta.osn_loop.steps_run, 2)

    def test_serializes_via_asdict(self):
        from dataclasses import asdict
        meta = NativeRunMeta()
        loop = OSNLoopMeta(
            enabled=True,
            steps_run=1,
            steps_queued=2,
            max_steps=5,
            consecutive_empty=0,
            terminated_reason="complete",
            steps=[OSNLoopStep(
                step_index=0, tool_name="read_file", target_label="src/foo.py",
                reason="read_search_finding", ok=True, output_chars=100,
            )],
            paths_surfaced=["src/foo.py"],
            warnings=[],
            truncated=False,
        )
        meta.osn_loop = loop
        d = asdict(meta)
        self.assertIn("osn_loop", d)
        self.assertIsNotNone(d["osn_loop"])
        self.assertTrue(d["osn_loop"]["enabled"])
        self.assertEqual(d["osn_loop"]["steps_run"], 1)
        self.assertEqual(d["osn_loop"]["paths_surfaced"], ["src/foo.py"])
        self.assertEqual(len(d["osn_loop"]["steps"]), 1)
        self.assertEqual(d["osn_loop"]["steps"][0]["tool_name"], "read_file")

    def test_serializes_none_osn_loop(self):
        from dataclasses import asdict
        meta = NativeRunMeta()
        d = asdict(meta)
        self.assertIn("osn_loop", d)
        self.assertIsNone(d["osn_loop"])

    def test_json_serializable_with_loop(self):
        import json
        from dataclasses import asdict
        meta = NativeRunMeta()
        meta.osn_loop = OSNLoopMeta(
            enabled=True, steps_run=1, steps_queued=1, max_steps=5,
            terminated_reason="complete",
            steps=[OSNLoopStep(
                step_index=0, tool_name="list_files", target_label="src/",
                reason="task_keyword", ok=True, output_chars=50,
            )],
            paths_surfaced=[],
        )
        d = asdict(meta)
        serialized = json.dumps(d)
        self.assertIn("osn_loop", serialized)

    def test_json_serializable_without_loop(self):
        import json
        from dataclasses import asdict
        meta = NativeRunMeta()
        d = asdict(meta)
        serialized = json.dumps(d)
        self.assertIn('"osn_loop": null', serialized)


class TestGenerateOSNIntegration(unittest.TestCase):
    """Integration tests for --native-loop experimental in generate()."""

    def _make_executor(self, native_loop=None, with_repo=False):
        import tempfile
        fake_gen = _make_generator_mock()
        kwargs: dict = {"provider": MagicMock(), "native_loop": native_loop}
        if with_repo:
            tmp = tempfile.mkdtemp()
            kwargs["repo_root"] = Path(tmp)
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(**kwargs)
        return executor, fake_gen

    def test_no_flag_osn_loop_is_none(self):
        executor, _ = self._make_executor(native_loop=None)
        executor.generate("fix the bug")
        self.assertIsNone(executor.native_meta.osn_loop)

    def test_no_flag_osn_loop_not_in_phases(self):
        executor, _ = self._make_executor(native_loop=None)
        executor.generate("fix the bug")
        self.assertNotIn("osn_loop", executor.native_meta.native_loop_steps)

    def test_no_flag_prompt_no_osn_block(self):
        executor, fake_gen = self._make_executor(native_loop=None)
        executor.generate("fix the bug")
        _, kwargs = fake_gen.generate.call_args
        skills_context = kwargs.get("skills_context", "")
        self.assertNotIn("[osn loop]", skills_context)

    def test_experimental_flag_no_repo_no_crash(self):
        # No repo_root → _runner is None → _run_experimental_loop returns early (no crash)
        executor, _ = self._make_executor(native_loop="experimental", with_repo=False)
        executor.generate("fix the bug")
        self.assertIsNone(executor.native_meta.osn_loop)

    def test_experimental_flag_with_repo_enabled_true(self):
        executor, _ = self._make_executor(native_loop="experimental", with_repo=True)
        executor.generate("fix the bug")
        self.assertIsNotNone(executor.native_meta.osn_loop)
        self.assertTrue(executor.native_meta.osn_loop.enabled)

    def test_experimental_flag_osn_loop_in_phases(self):
        executor, _ = self._make_executor(native_loop="experimental", with_repo=True)
        executor.generate("fix the bug")
        self.assertIn("osn_loop", executor.native_meta.native_loop_steps)

    def test_experimental_flag_terminated_reason_valid(self):
        from openshard.native.context import _VALID_OSN_STOP_REASONS
        executor, _ = self._make_executor(native_loop="experimental", with_repo=True)
        executor.generate("fix the bug")
        if executor.native_meta.osn_loop is not None:
            self.assertIn(executor.native_meta.osn_loop.terminated_reason, _VALID_OSN_STOP_REASONS)

    def test_experimental_flag_no_write_file_in_trace(self):
        executor, _ = self._make_executor(native_loop="experimental", with_repo=True)
        executor.generate("fix the bug")
        for entry in executor.native_meta.tool_trace:
            tool_name = (
                entry.get("tool_name", "") if isinstance(entry, dict)
                else getattr(entry, "tool_name", "")
            )
            self.assertNotEqual(tool_name, "write_file",
                                "write_file must never appear in tool trace from OSN loop")

    def test_experimental_flag_no_run_command_in_trace(self):
        executor, _ = self._make_executor(native_loop="experimental", with_repo=True)
        executor.generate("fix the bug")
        for entry in executor.native_meta.tool_trace:
            tool_name = (
                entry.get("tool_name", "") if isinstance(entry, dict)
                else getattr(entry, "tool_name", "")
            )
            self.assertNotEqual(tool_name, "run_command",
                                "run_command must never appear in tool trace from OSN loop")

    def test_experimental_flag_no_run_verification_in_trace(self):
        executor, _ = self._make_executor(native_loop="experimental", with_repo=True)
        executor.generate("fix the bug")
        for entry in executor.native_meta.tool_trace:
            tool_name = (
                entry.get("tool_name", "") if isinstance(entry, dict)
                else getattr(entry, "tool_name", "")
            )
            self.assertNotEqual(tool_name, "run_verification",
                                "run_verification must never appear in tool trace from OSN loop")

    def test_experimental_flag_max_steps_enforced(self):
        executor, _ = self._make_executor(native_loop="experimental", with_repo=True)
        executor.native_meta.read_search_findings = [
            f"file:src/file{i}.py" for i in range(20)
        ]
        executor.generate("fix the bug")
        if executor.native_meta.osn_loop is not None:
            self.assertLessEqual(executor.native_meta.osn_loop.steps_run, _MAX_OSN_LOOP_STEPS)

    def test_no_flag_plan_phase_still_present(self):
        executor, _ = self._make_executor(native_loop=None)
        executor.generate("fix the bug")
        phases = executor.native_meta.native_loop_steps
        self.assertNotIn("osn_loop", phases)
        self.assertIn("plan", phases)

    def test_osn_context_block_absent_when_flag_inactive(self):
        executor, fake_gen = self._make_executor(native_loop=None)
        executor.generate("fix the bug")
        _, kwargs = fake_gen.generate.call_args
        skills_context = kwargs.get("skills_context", "")
        self.assertNotIn("[osn loop]", skills_context)

    def test_osn_context_block_present_only_when_steps_ran(self):
        executor, fake_gen = self._make_executor(native_loop="experimental", with_repo=True)
        executor.generate("fix the bug")
        _, kwargs = fake_gen.generate.call_args
        skills_context = kwargs.get("skills_context", "")
        loop = executor.native_meta.osn_loop
        if loop is not None and loop.enabled and loop.steps_run > 0:
            self.assertIn("[osn loop]", skills_context)
        else:
            self.assertNotIn("[osn loop]", skills_context)


class TestExtractExplicitFilePaths(unittest.TestCase):
    """Unit tests for _extract_explicit_file_paths()."""

    def test_detects_path_with_dirs(self):
        paths = _extract_explicit_file_paths("what does openshard/cli/main.py do?")
        self.assertIn("openshard/cli/main.py", paths)

    def test_detects_known_extension_no_dir(self):
        paths = _extract_explicit_file_paths("see README.md for details")
        self.assertIn("README.md", paths)

    def test_detects_multiple_paths(self):
        paths = _extract_explicit_file_paths("compare openshard/cli/main.py and tests/test_foo.py")
        self.assertIn("openshard/cli/main.py", paths)
        self.assertIn("tests/test_foo.py", paths)

    def test_deduplicates(self):
        paths = _extract_explicit_file_paths("openshard/cli/main.py and openshard/cli/main.py again")
        self.assertEqual(paths.count("openshard/cli/main.py"), 1)

    def test_ignores_url_fragment(self):
        paths = _extract_explicit_file_paths("see http://example.com/openshard/cli/main.py")
        self.assertNotIn("openshard/cli/main.py", paths)

    def test_empty_task_returns_empty(self):
        self.assertEqual(_extract_explicit_file_paths(""), [])

    def test_path_without_extension_not_matched(self):
        paths = _extract_explicit_file_paths("look at openshard/cli")
        self.assertNotIn("openshard/cli", paths)

    def test_absolute_path_not_returned(self):
        paths = _extract_explicit_file_paths("see /etc/passwd please")
        self.assertNotIn("/etc/passwd", paths)


class TestExplicitFileSnippetsObservePhase(unittest.TestCase):
    """Explicit file paths in task text must end up in NativeEvidence.file_snippets."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor

    def _make_executor_no_repo(self):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock())
        return executor

    def test_explicit_file_included_when_named_in_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "foo").mkdir()
            (root / "foo" / "bar.py").write_text("def hello(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("what does foo/bar.py do?")
        evidence = executor.native_meta.evidence
        self.assertIsNotNone(evidence)
        paths = [s.path for s in evidence.file_snippets]
        self.assertIn("foo/bar.py", paths)

    def test_snippet_lines_are_non_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "foo").mkdir()
            (root / "foo" / "bar.py").write_text("x = 1\ny = 2\nz = 3\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("what does foo/bar.py do?")
        evidence = executor.native_meta.evidence
        snippet = next(s for s in evidence.file_snippets if s.path == "foo/bar.py")
        self.assertGreater(len(snippet.lines), 0)

    def test_nonexistent_path_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("what does openshard/missing.py do?")
        self.assertIsNone(executor.native_meta.evidence)

    def test_unsafe_traversal_path_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("what does ../secret.py do?")
        self.assertIsNone(executor.native_meta.evidence)

    def test_snippet_lines_bounded(self):
        big = "\n".join(f"line_{i} = {i}" for i in range(100))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "big.py").write_text(big)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("what does big.py do?")
        evidence = executor.native_meta.evidence
        self.assertIsNotNone(evidence)
        snippet = next(s for s in evidence.file_snippets if s.path == "big.py")
        self.assertLessEqual(len(snippet.lines), 25)

    def test_no_runner_returns_early(self):
        executor = self._make_executor_no_repo()
        executor._run_observe_phase("what does openshard/cli/main.py do?")
        self.assertIsNone(executor.native_meta.evidence)

    def test_explicit_snippet_not_duplicated_when_search_also_finds_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "foo").mkdir()
            (root / "foo" / "bar.py").write_text("def login(): pass\n")
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("where is login in foo/bar.py")
        evidence = executor.native_meta.evidence
        self.assertIsNotNone(evidence)
        paths = [s.path for s in evidence.file_snippets]
        self.assertEqual(paths.count("foo/bar.py"), 1)

    def test_explicit_snippets_respect_max_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(5):
                (root / f"file{i}.py").write_text(f"x = {i}\n")
            task = " ".join(f"file{i}.py" for i in range(5))
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase(f"what do {task} do?")
        evidence = executor.native_meta.evidence
        if evidence is not None:
            self.assertLessEqual(len(evidence.file_snippets), _MAX_EXPLICIT_SNIPPET_FILES)

    def test_no_evidence_without_file_paths_or_search_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("fix the bug in the code")
        self.assertIsNone(executor.native_meta.evidence)

    def test_python_outline_exposes_def_not_just_imports(self):
        content = "import os\n\ndef main(): pass\nclass Config:\n    pass\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mod.py").write_text(content)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("what does mod.py do?")
        evidence = executor.native_meta.evidence
        self.assertIsNotNone(evidence)
        snippet = next(s for s in evidence.file_snippets if s.path == "mod.py")
        joined = "\n".join(snippet.lines)
        self.assertIn("main", joined)
        self.assertIn("Config", joined)

    def test_python_outline_exposes_click_commands(self):
        content = "import click\n\n@click.command()\ndef deploy(): pass\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cli.py").write_text(content)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("what does cli.py do?")
        evidence = executor.native_meta.evidence
        self.assertIsNotNone(evidence)
        snippet = next(s for s in evidence.file_snippets if s.path == "cli.py")
        joined = "\n".join(snippet.lines)
        self.assertIn("[commands]", joined)
        self.assertIn("deploy", joined)


class TestBuildExplicitFileOutline(unittest.TestCase):
    """Unit tests for _build_explicit_file_outline() and its sub-helpers."""

    def _outline(self, content: str, path: str, **kw) -> str:
        from openshard.native.executor import _build_explicit_file_outline
        return "\n".join(_build_explicit_file_outline(content, path, **kw))

    # ── Python outline ────────────────────────────────────────────────────

    def test_python_includes_def_names(self):
        content = "def foo(): pass\ndef bar(): pass\n"
        result = self._outline(content, "mod.py")
        self.assertIn("foo", result)
        self.assertIn("bar", result)

    def test_python_includes_class_names(self):
        content = "class Foo:\n    pass\nclass Bar:\n    pass\n"
        result = self._outline(content, "mod.py")
        self.assertIn("Foo", result)
        self.assertIn("Bar", result)

    def test_python_labels_defs_and_classes_separately(self):
        content = "class A:\n    pass\ndef b(): pass\n"
        result = self._outline(content, "mod.py")
        self.assertIn("[classes]", result)
        self.assertIn("[defs]", result)

    def test_python_module_docstring_captured(self):
        content = '"""Handles CLI entry points."""\nimport click\n'
        result = self._outline(content, "main.py")
        self.assertIn("[docstring]", result)
        self.assertIn("Handles CLI entry points", result)

    def test_python_imports_summarized(self):
        content = "import os\nimport re\nfrom pathlib import Path\ndef foo(): pass\n"
        result = self._outline(content, "main.py")
        self.assertIn("[imports]", result)
        self.assertTrue(
            any(name in result for name in ("os", "re", "pathlib")),
            msg=f"Expected import names in outline: {result!r}",
        )

    def test_python_click_command_decorator_labeled_as_command(self):
        content = "import click\n\n@click.command()\ndef run(): pass\n"
        result = self._outline(content, "cli.py")
        self.assertIn("[commands]", result)
        self.assertIn("run", result)

    def test_python_app_group_decorator_labeled_as_command(self):
        content = "import typer\napp = typer.Typer()\n\n@app.command()\ndef deploy(): pass\n"
        result = self._outline(content, "cli.py")
        self.assertIn("[commands]", result)
        self.assertIn("deploy", result)

    def test_python_non_command_def_not_labeled_as_command(self):
        content = "def helper(): pass\n"
        result = self._outline(content, "util.py")
        self.assertNotIn("[commands]", result)
        self.assertIn("[defs]", result)

    def test_python_indented_method_not_in_defs(self):
        content = "class Foo:\n    def bar(self): pass\n"
        result = self._outline(content, "mod.py")
        self.assertIn("Foo", result)
        # bar is a method (indented), must NOT appear under [defs]
        if "[defs]" in result:
            defs_part = result.split("[defs]")[1].split("\n")[0]
            self.assertNotIn("bar", defs_part)

    def test_python_large_file_bounded_by_max_lines(self):
        many_defs = "\n".join(f"def fn_{i}(): pass" for i in range(200))
        from openshard.native.executor import _build_explicit_file_outline
        lines = _build_explicit_file_outline(many_defs, "big.py", max_lines=25)
        self.assertLessEqual(len(lines), 25)

    def test_python_empty_file_returns_empty(self):
        from openshard.native.executor import _build_explicit_file_outline
        self.assertEqual(_build_explicit_file_outline("", "empty.py"), [])

    def test_python_imports_only_shows_imports_no_defs(self):
        content = "import os\nimport sys\n"
        result = self._outline(content, "imports_only.py")
        self.assertIn("[imports]", result)
        self.assertNotIn("[defs]", result)
        self.assertNotIn("[classes]", result)

    def test_python_fallback_to_generic_when_no_symbols(self):
        content = "x = 1\ny = 2\nz = 3\n"
        from openshard.native.executor import _build_explicit_file_outline
        lines = _build_explicit_file_outline(content, "data.py")
        # Fallback to generic outline: at least one line, all non-blank
        self.assertGreater(len(lines), 0)

    def test_python_long_import_list_truncated_with_ellipsis(self):
        content = "\n".join(f"import mod{i}" for i in range(10)) + "\ndef go(): pass\n"
        result = self._outline(content, "heavy.py")
        self.assertIn("...", result)

    # ── Non-Python outline ────────────────────────────────────────────────

    def test_non_python_returns_meaningful_lines(self):
        content = "\n\nHello world\nThis is a readme\n\n"
        from openshard.native.executor import _build_explicit_file_outline
        lines = _build_explicit_file_outline(content, "README.md")
        joined = "\n".join(lines)
        self.assertIn("Hello world", joined)
        self.assertIn("This is a readme", joined)

    def test_non_python_skips_leading_blank_lines(self):
        content = "\n\n\nfirst real line\n"
        from openshard.native.executor import _build_explicit_file_outline
        lines = _build_explicit_file_outline(content, "notes.txt")
        self.assertTrue(lines[0].strip())

    def test_non_python_bounded_by_max_lines(self):
        content = "\n".join(f"line {i}" for i in range(100))
        from openshard.native.executor import _build_explicit_file_outline
        lines = _build_explicit_file_outline(content, "data.txt", max_lines=20)
        self.assertLessEqual(len(lines), 20)

    def test_non_python_empty_returns_empty(self):
        from openshard.native.executor import _build_explicit_file_outline
        self.assertEqual(_build_explicit_file_outline("", "file.txt"), [])


class TestExplicitFileContextInGeneratedPrompt(unittest.TestCase):
    """Explicit file content must reach the model context via evidence rendering."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor, fake_gen

    def test_generate_includes_explicit_file_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "foo").mkdir()
            (root / "foo" / "bar.py").write_text("def hello(): pass\n")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("what does foo/bar.py do?")
        _, kwargs = fake_gen.generate.call_args
        ctx = kwargs["skills_context"]
        self.assertIn("[evidence]", ctx)
        self.assertIn("foo/bar.py", ctx)

    def test_generate_no_evidence_for_plain_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("improve the error handling")
        _, kwargs = fake_gen.generate.call_args
        ctx = kwargs.get("skills_context", "")
        self.assertNotIn("[evidence]", ctx)

    def test_explicit_file_does_not_require_search_trigger_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_foo.py").write_text("def test_it(): pass\n")
            executor, fake_gen = self._make_executor_with_repo(root)
            executor.generate("explain tests/test_foo.py")
        _, kwargs = fake_gen.generate.call_args
        ctx = kwargs["skills_context"]
        self.assertIn("[evidence]", ctx)
        self.assertIn("tests/test_foo.py", ctx)


class TestStagedDryRunNoGeneratorCall(unittest.TestCase):
    """Staged dry-run must not produce any provider calls."""

    _RISKY_REPO = RepoFacts(
        languages=["python"], package_files=[], framework=None,
        test_command=None, risky_paths=["openshard/config/settings.py"], changed_files=[],
    )

    def _invoke_staged_dry_run(self, gen_mock, task="fix the bug", extra_args=()):
        with patch("openshard.run.pipeline.ExecutionGenerator", return_value=gen_mock), \
             patch("openshard.run.pipeline.NativeAgentExecutor", return_value=_make_native_mock()), \
             patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=self._RISKY_REPO), \
             patch("openshard.run.pipeline.build_verification_plan", return_value=_empty_plan()), \
             patch("openshard.run.pipeline._log_run"), \
             patch("openshard.run.pipeline._build_explicit_file_context", return_value=""):
            runner = CliRunner()
            result = runner.invoke(cli, ["run", "--dry-run"] + list(extra_args) + [task])
        return result, gen_mock

    def test_staged_dry_run_does_not_call_generate(self):
        gen_mock = _make_generator_mock()
        result, gen_mock = self._invoke_staged_dry_run(gen_mock)
        self.assertEqual(result.exit_code, 0, result.output)
        gen_mock.generate.assert_not_called()

    def test_staged_dry_run_does_not_call_planning_stage(self):
        gen_mock = _make_generator_mock()
        with patch("openshard.run.pipeline.run_planning_stage") as plan_mock:
            self._invoke_staged_dry_run(gen_mock)
        plan_mock.assert_not_called()

    def test_non_dry_run_staged_does_call_generate(self):
        gen_mock = _make_generator_mock()
        with patch("openshard.run.pipeline.ExecutionGenerator", return_value=gen_mock), \
             patch("openshard.run.pipeline.NativeAgentExecutor", return_value=_make_native_mock()), \
             patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=self._RISKY_REPO), \
             patch("openshard.run.pipeline.build_verification_plan", return_value=_empty_plan()), \
             patch("openshard.run.pipeline._log_run"), \
             patch("openshard.run.pipeline._build_explicit_file_context", return_value=""):
            runner = CliRunner()
            result = runner.invoke(cli, ["run", "fix the bug"])
        self.assertEqual(result.exit_code, 0, result.output)
        gen_mock.generate.assert_called_once()


class TestBuildExplicitFileContext(unittest.TestCase):
    """Unit tests for _build_explicit_file_context() in pipeline.py."""

    def _helper(self, task, *, root=None):
        from openshard.run.pipeline import _build_explicit_file_context
        return _build_explicit_file_context(task, root=root)

    def test_builds_evidence_for_named_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "openshard" / "cli").mkdir(parents=True)
            (root / "openshard" / "cli" / "main.py").write_text("def main(): pass\n")
            result = self._helper("what does openshard/cli/main.py do?", root=root)
        self.assertIn("[evidence]", result)
        self.assertIn("openshard/cli/main.py", result)
        self.assertIn("main", result)

    def test_returns_empty_for_plain_task(self):
        result = self._helper("improve the error handling")
        self.assertEqual(result, "")

    def test_rejects_unsafe_absolute_path(self):
        result = self._helper("look at /etc/passwd please")
        self.assertEqual(result, "")

    def test_returns_empty_for_nonexistent_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._helper("what does openshard/cli/main.py do?", root=Path(tmp))
        self.assertEqual(result, "")

    def test_python_outline_includes_def_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "openshard" / "cli").mkdir(parents=True)
            (root / "openshard" / "cli" / "main.py").write_text(
                "import click\n\n@click.command()\ndef run(): pass\n\ndef helper(): pass\n"
            )
            result = self._helper("what does openshard/cli/main.py do?", root=root)
        self.assertIn("[defs]", result)
        self.assertIn("helper", result)

    def test_python_outline_exposes_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "openshard" / "cli").mkdir(parents=True)
            (root / "openshard" / "cli" / "main.py").write_text(
                "import click\n\n@click.command()\ndef run(): pass\n"
            )
            result = self._helper("what does openshard/cli/main.py do?", root=root)
        self.assertIn("[commands]", result)
        self.assertIn("run", result)


class TestPipelineExplicitFileContextInjection(unittest.TestCase):
    """Pipeline's direct/staged generation path must receive explicit file context."""

    _SAFE_REPO = RepoFacts(
        languages=["python"], package_files=[], framework=None,
        test_command=None, risky_paths=[], changed_files=[],
    )
    _RISKY_REPO = RepoFacts(
        languages=["python"], package_files=[], framework=None,
        test_command=None, risky_paths=["openshard/config/settings.py"], changed_files=[],
    )
    _KNOWN_CTX = "[evidence]\nsnippets:\nfoo/bar.py:\n  1: def hello(): pass"

    def _run_with_explicit_ctx(self, repo_facts, extra_args=()):
        gen_mock = _make_generator_mock()
        with patch("openshard.run.pipeline.ExecutionGenerator", return_value=gen_mock), \
             patch("openshard.run.pipeline.NativeAgentExecutor", return_value=_make_native_mock()), \
             patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=repo_facts), \
             patch("openshard.run.pipeline.build_verification_plan", return_value=_empty_plan()), \
             patch("openshard.run.pipeline._log_run"), \
             patch("openshard.run.pipeline._build_explicit_file_context", return_value=self._KNOWN_CTX):
            runner = CliRunner()
            result = runner.invoke(cli, ["run"] + list(extra_args) + ["what does foo/bar.py do?"])
        return result, gen_mock

    def test_direct_path_receives_explicit_file_content(self):
        result, gen_mock = self._run_with_explicit_ctx(self._SAFE_REPO)
        self.assertEqual(result.exit_code, 0, result.output)
        gen_mock.generate.assert_called_once()
        ctx = gen_mock.generate.call_args[1].get("skills_context", "")
        self.assertIn("[evidence]", ctx)
        self.assertIn("foo/bar.py", ctx)

    def test_staged_path_receives_explicit_file_content(self):
        result, gen_mock = self._run_with_explicit_ctx(self._RISKY_REPO)
        self.assertEqual(result.exit_code, 0, result.output)
        gen_mock.generate.assert_called_once()
        ctx = gen_mock.generate.call_args[1].get("skills_context", "")
        self.assertIn("[evidence]", ctx)
        self.assertIn("foo/bar.py", ctx)

    def test_native_path_does_not_call_build_explicit_file_context(self):
        native_mock = _make_native_mock()
        with patch("openshard.run.pipeline.NativeAgentExecutor", return_value=native_mock), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=_make_generator_mock()), \
             patch("openshard.run.pipeline.ProviderManager", return_value=_make_manager_mock()), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=self._SAFE_REPO), \
             patch("openshard.run.pipeline.build_verification_plan", return_value=_empty_plan()), \
             patch("openshard.run.pipeline._log_run"), \
             patch("openshard.run.pipeline._build_explicit_file_context") as ctx_mock:
            runner = CliRunner()
            runner.invoke(cli, ["run", "--workflow", "native", "what does foo/bar.py do?"])
        ctx_mock.assert_not_called()


class TestInferResultHelpers(unittest.TestCase):
    """Unit tests for _infer_result_count and _infer_result_quality."""

    def _ok(self, tool_name, output="", metadata=None):
        from openshard.native.tools import NativeToolResult
        return NativeToolResult(tool_name=tool_name, ok=True, output=output, metadata=metadata or {})

    def _fail(self, tool_name):
        from openshard.native.tools import NativeToolResult
        return NativeToolResult(tool_name=tool_name, ok=False)

    def test_search_repo_uses_matches_metadata(self):
        result = self._ok("search_repo", output="file.py:1:x", metadata={"matches": 3})
        self.assertEqual(_infer_result_count("search_repo", result), 3)

    def test_search_repo_zero_matches(self):
        result = self._ok("search_repo", output="", metadata={"matches": 0})
        self.assertEqual(_infer_result_count("search_repo", result), 0)

    def test_list_files_counts_nonempty_lines(self):
        result = self._ok("list_files", output="a.py\nb.py\n\nc.py\n")
        self.assertEqual(_infer_result_count("list_files", result), 3)

    def test_list_files_empty_output(self):
        result = self._ok("list_files", output="")
        self.assertEqual(_infer_result_count("list_files", result), 0)

    def test_get_git_diff_nonempty_returns_one(self):
        result = self._ok("get_git_diff", output="diff --git a/x.py b/x.py\n+x = 1")
        self.assertEqual(_infer_result_count("get_git_diff", result), 1)

    def test_get_git_diff_empty_returns_zero(self):
        result = self._ok("get_git_diff", output="   ")
        self.assertEqual(_infer_result_count("get_git_diff", result), 0)

    def test_failed_result_returns_zero(self):
        result = self._fail("search_repo")
        self.assertEqual(_infer_result_count("search_repo", result), 0)

    def test_quality_empty_when_count_zero(self):
        self.assertEqual(_infer_result_quality(0, False), "empty")
        self.assertEqual(_infer_result_quality(0, True), "empty")

    def test_quality_useful_when_injected(self):
        self.assertEqual(_infer_result_quality(5, True), "useful")

    def test_quality_weak_when_not_injected(self):
        self.assertEqual(_infer_result_quality(3, False), "weak")

    def test_zero_result_case_true_for_empty_successful_search(self):
        result = self._ok("search_repo", output="", metadata={"matches": 0})
        self.assertTrue(_infer_zero_result_case("search_repo", result, 0))

    def test_zero_result_case_false_when_search_found_matches(self):
        result = self._ok("search_repo", output="f.py:1:x", metadata={"matches": 3})
        self.assertFalse(_infer_zero_result_case("search_repo", result, 3))

    def test_zero_result_case_false_when_tool_failed(self):
        # A failed call also reports count 0, but that is an error, not an
        # empty-result signal.
        result = self._fail("search_repo")
        self.assertFalse(_infer_zero_result_case("search_repo", result, 0))

    def test_zero_result_case_false_for_empty_read_file(self):
        # Successfully reading an existing empty file is a valid state, not a
        # search miss — it must not flag as a zero-result signal.
        result = self._ok("read_file", output="")
        self.assertFalse(_infer_zero_result_case("read_file", result, 0))

    def test_zero_result_case_false_for_empty_git_diff(self):
        # A clean working tree is an observation, not a failed search.
        result = self._ok("get_git_diff", output="")
        self.assertFalse(_infer_zero_result_case("get_git_diff", result, 0))

    def test_zero_result_case_false_for_non_search_tool(self):
        result = self._ok("run_verification", output="")
        self.assertFalse(_infer_zero_result_case("run_verification", result, 0))

    def test_zero_result_case_true_for_empty_list_files(self):
        result = self._ok("list_files", output="")
        self.assertTrue(_infer_zero_result_case("list_files", result, 0))


class TestNativeToolSearchEventRecording(unittest.TestCase):
    """NativeAgentExecutor records NativeToolSearchEvent at call sites."""

    def _make_executor_with_repo(self, tmp_path: Path):
        fake_gen = _make_generator_mock()
        with patch("openshard.native.executor.ExecutionGenerator", return_value=fake_gen):
            executor = NativeAgentExecutor(provider=MagicMock(), repo_root=tmp_path)
        return executor

    def test_preflight_records_list_files_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor = self._make_executor_with_repo(root)
            executor._run_preflight()
        events = executor.native_meta.tool_search_events
        self.assertTrue(any(e.tool_name == "list_files" for e in events))

    def test_preflight_event_selected_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor = self._make_executor_with_repo(root)
            executor._run_preflight()
        ev = next(e for e in executor.native_meta.tool_search_events if e.tool_name == "list_files")
        self.assertEqual(ev.selected_reason, "preflight scan")

    def test_observe_phase_records_git_diff_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("fix the bug")
        events = executor.native_meta.tool_search_events
        self.assertTrue(any(e.tool_name == "get_git_diff" for e in events))

    def test_empty_search_result_quality_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            executor = self._make_executor_with_repo(root)
            executor._run_observe_phase("find something")
        search_events = [e for e in executor.native_meta.tool_search_events if e.tool_name == "search_repo"]
        for ev in search_events:
            if ev.result_count == 0:
                self.assertEqual(ev.result_quality, "empty")

    def test_event_no_raw_content_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor = self._make_executor_with_repo(root)
            executor._run_preflight()
        from dataclasses import asdict
        for ev in executor.native_meta.tool_search_events:
            d = asdict(ev)
            for forbidden in ("output", "snippets", "diff", "stdout", "stderr"):
                self.assertNotIn(forbidden, d)

    def test_record_tool_search_event_useful_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor = self._make_executor_with_repo(root)
        from openshard.native.tools import NativeToolResult
        result = NativeToolResult(tool_name="search_repo", ok=True, output="main.py:1:x", metadata={"matches": 1})
        ev = executor._record_tool_search_event(
            "search_repo", result,
            selected_reason="test",
            query="x",
            context_injected=True,
        )
        self.assertEqual(ev.result_quality, "useful")
        self.assertEqual(ev.result_count, 1)
        self.assertFalse(ev.zero_result_case)

    def test_record_tool_search_event_zero_result_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
        from openshard.native.tools import NativeToolResult
        result = NativeToolResult(tool_name="search_repo", ok=True, output="", metadata={"matches": 0})
        ev = executor._record_tool_search_event(
            "search_repo", result,
            selected_reason="test",
            query="missing-symbol",
            context_injected=False,
        )
        self.assertTrue(ev.zero_result_case)
        self.assertEqual(ev.result_count, 0)
        self.assertEqual(ev.result_quality, "empty")

    def test_record_tool_search_event_weak_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
        from openshard.native.tools import NativeToolResult
        result = NativeToolResult(tool_name="search_repo", ok=True, output="main.py:1:x", metadata={"matches": 2})
        ev = executor._record_tool_search_event(
            "search_repo", result,
            selected_reason="test",
            query="x",
            context_injected=False,
        )
        self.assertEqual(ev.result_quality, "weak")

    def test_available_tools_populated_in_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("x = 1")
            executor = self._make_executor_with_repo(root)
            executor._run_preflight()
        for ev in executor.native_meta.tool_search_events:
            self.assertIsInstance(ev.available_tools, list)
            self.assertGreater(len(ev.available_tools), 0)
            self.assertIn("list_files", ev.available_tools)

    def test_available_tools_contains_only_name_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = self._make_executor_with_repo(root)
        from openshard.native.tools import NativeToolResult
        result = NativeToolResult(tool_name="list_files", ok=True, output="main.py\n")
        ev = executor._record_tool_search_event("list_files", result, context_injected=True)
        for item in ev.available_tools:
            self.assertIsInstance(item, str)
            self.assertNotIn(" ", item.strip() if " " not in item else "")
