from __future__ import annotations

import unittest
from unittest.mock import ANY, MagicMock, patch

from click.testing import CliRunner

from openshard.analysis.repo import RepoFacts
from openshard.cli.main import cli
from openshard.providers.base import ModelInfo
from openshard.providers.manager import InventoryEntry
from openshard.routing.engine import MODEL_CHEAP, MODEL_MAIN, MODEL_STRONG
from openshard.routing.workflow_selector import WorkflowDecision

_DEFAULT_CONFIG = {"approval_mode": "smart"}

_PYTHON_REPO = RepoFacts(
    languages=["python"], package_files=[], framework=None,
    test_command=None, risky_paths=[], changed_files=[],
)

_RISKY_PYTHON_REPO = RepoFacts(
    languages=["python"], package_files=[], framework=None,
    test_command=None, risky_paths=["auth"], changed_files=[],
)


def _make_entry(model_id: str, provider: str = "openrouter", **kwargs) -> InventoryEntry:
    return InventoryEntry(
        provider=provider,
        model=ModelInfo(
            id=model_id,
            name=model_id,
            pricing=kwargs.get("pricing", {}),
            context_window=kwargs.get("context_window"),
            max_output_tokens=None,
            supports_vision=kwargs.get("supports_vision", False),
            supports_tools=kwargs.get("supports_tools", False),
        ),
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
    g.model = "mock-default-model"
    g.fixer_model = "mock-fixer-model"
    return g


def _make_manager_mock(entries: list[InventoryEntry], provider_names: list[str]):
    m = MagicMock()
    inv = MagicMock()
    inv.models = entries
    m.get_inventory.return_value = inv
    m.providers = {p: MagicMock() for p in provider_names}
    return m


class TestScoredRoutingIntegration(unittest.TestCase):

    def _run(self, args: list[str], manager_mock, generator_mock):
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager_mock), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run"):
            runner = CliRunner()
            result = runner.invoke(cli, ["run"] + args)
        return result

    def test_scored_selection_used(self):
        """When inventory has a matching entry, its model ID reaches generate()."""
        task = "implement a feature"
        entry = _make_entry("openrouter/fast-model", pricing={"prompt": "0.0000005"})
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()

        self._run([task], manager, generator)

        generator.generate.assert_called_once_with(task, model="openrouter/fast-model", repo_facts=ANY, skills_context="", max_tokens=16384, is_review_task=False)

    def test_fallback_when_no_candidate(self):
        """When the only inventory entry fails hard filter, routing decision model is used."""
        task = "add a ui component"
        # visual category → needs_vision=True; this entry has supports_vision=False
        entry = _make_entry("openrouter/no-vision", supports_vision=False)
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()

        self._run([task], manager, generator)

        # routing_decision.model for "visual" category is moonshotai/kimi-k2.5
        generator.generate.assert_called_once_with(task, model="moonshotai/kimi-k2.5", repo_facts=ANY, skills_context="", max_tokens=16384, is_review_task=False)

    def test_provider_manager_failure_uses_fallback(self):
        """When ProviderManager.get_inventory raises, routing decision model is used."""
        task = "implement a feature"
        manager = MagicMock()
        manager.get_inventory.side_effect = RuntimeError("network error")
        generator = _make_generator_mock()

        self._run([task], manager, generator)

        # standard task → MODEL_MAIN
        generator.generate.assert_called_once_with(task, model="z-ai/glm-5.1", repo_facts=ANY, skills_context="", max_tokens=16384, is_review_task=False)

    def test_provider_flag_restricts_candidates(self):
        """With --provider openrouter, only openrouter entries are considered.

        The anthropic entry has a large context window (score bonus) that would
        win without filtering, proving the filter is applied when it matters.
        """
        task = "implement a feature"
        openrouter_entry = _make_entry("openrouter/basic", pricing={"prompt": "0.0000005"})
        # anthropic entry scores higher (200K context → +2 bonus) but must be excluded
        anthropic_entry = _make_entry(
            "anthropic/claude-large", provider="anthropic", context_window=200_000,
            pricing={"prompt": "0.0000005"},
        )
        manager = _make_manager_mock([openrouter_entry, anthropic_entry], ["openrouter", "anthropic"])
        generator = _make_generator_mock()

        self._run([task, "--provider", "openrouter"], manager, generator)

        generator.generate.assert_called_once_with(task, model="openrouter/basic", repo_facts=ANY, skills_context="", max_tokens=16384, is_review_task=False)

    def test_scored_routing_logged(self):
        """_log_run is called with a ScoredRoutingResult that reflects the winning candidate."""
        task = "implement a feature"
        entry = _make_entry("openrouter/fast-model", pricing={"prompt": "0.0000005"})
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()

        with patch("openshard.run.pipeline.ProviderManager", return_value=manager), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline._log_run") as mock_log:
            runner = CliRunner()
            runner.invoke(cli, ["run", task])

        mock_log.assert_called_once()
        _scored = mock_log.call_args.kwargs.get("_scored")
        self.assertIsNotNone(_scored)
        self.assertEqual(_scored.category, "standard")
        self.assertEqual(_scored.selected_model, "openrouter/fast-model")
        self.assertEqual(_scored.selected_provider, "openrouter")
        self.assertFalse(_scored.used_fallback)


class TestRoutingDisplayConsistency(unittest.TestCase):
    """Verify that the early [routing] line in --more output matches the final selected model."""

    def _run(self, args: list[str], manager_mock, generator_mock):
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager_mock), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run"):
            runner = CliRunner()
            result = runner.invoke(cli, ["run"] + args)
        return result

    def test_routing_line_shows_scored_model_not_keyword_model(self):
        """With --more, the Routing section shows the scored model, not the keyword-routed one.

        Keyword routing for 'implement a feature' picks GLM-5.1, but the inventory
        has fast-model which scoring selects instead.  The display must agree.
        """
        task = "implement a feature"
        entry = _make_entry("openrouter/fast-model", pricing={"prompt": "0.0000005"})
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()

        result = self._run([task, "--more"], manager, generator)

        # Find the initial candidate line in the Routing section
        candidate_lines = [
            ln for ln in result.output.splitlines()
            if "Initial candidate:" in ln
        ]
        self.assertEqual(len(candidate_lines), 1, result.output)
        # _model_label("openrouter/fast-model") → "Fast Model"
        self.assertIn("Fast Model", candidate_lines[0])
        # Keyword-routed model (GLM-5.1) must NOT appear in this line
        self.assertNotIn("GLM", candidate_lines[0])

    def test_routing_line_uses_fallback_model_when_scoring_finds_no_candidate(self):
        """When no inventory entry passes the hard filter, fallback keyword routing is used."""
        task = "add a ui component"  # routes to visual category → needs_vision=True
        # Entry lacks vision support → hard-filtered out → fallback
        entry = _make_entry("openrouter/no-vision", supports_vision=False)
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()

        result = self._run([task, "--more"], manager, generator)

        # Fallback is signalled by the candidates line
        self.assertIn("fallback keyword routing", result.output, result.output)
        # The hard-filtered entry must not appear as an initial candidate
        self.assertNotIn("Initial candidate:", result.output, result.output)

    def test_default_routing_line_shows_scored_model(self):
        """Default (no --more) routing line shows the scored model, not the keyword-routed one."""
        task = "implement a feature"
        entry = _make_entry("openrouter/fast-model", pricing={"prompt": "0.0000005"})
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()

        result = self._run([task], manager, generator)

        routing_lines = [
            ln for ln in result.output.splitlines()
            if ln.strip().startswith("Routing -")
        ]
        self.assertEqual(len(routing_lines), 1, result.output)
        self.assertIn("Fast Model", routing_lines[0])
        self.assertNotIn("GLM", routing_lines[0])


class TestApprovalFlag(unittest.TestCase):
    """Verify --approval flag overrides config and triggers gates correctly."""

    def _run_with_write(self, args: list[str], generator_mock, approval_mode="smart",
                        repo=None, config_override=None):
        config = config_override if config_override is not None else {"approval_mode": approval_mode}
        with patch("openshard.run.pipeline.ProviderManager"), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock), \
             patch("openshard.cli.main.load_config", return_value=config), \
             patch("openshard.run.pipeline.analyze_repo", return_value=repo or _PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run"), \
             patch("openshard.run.pipeline._write_files"):
            runner = CliRunner()
            result = runner.invoke(cli, ["run"] + args, input="n\n")
        return result

    def test_approval_ask_flag_triggers_gate(self):
        """--approval ask shows gate prompt before file write without editing config."""
        generator = _make_generator_mock()
        generator.generate.return_value.files = [MagicMock(path="helper.py")]
        result = self._run_with_write(
            ["add a helper", "--write", "--approval", "ask"],
            generator,
            approval_mode="auto",
        )
        assert "[gate]" in result.output, result.output

    def test_config_approval_mode_honored_without_flag(self):
        """When --approval is not passed, config approval_mode is used."""
        generator = _make_generator_mock()
        generator.generate.return_value.files = [MagicMock(path="helper.py")]
        result = self._run_with_write(
            ["add a helper", "--write"],
            generator,
            approval_mode="ask",
        )
        assert "[gate]" in result.output, result.output

    def test_auto_config_no_gate_without_flag(self):
        """With config auto and no --approval flag, no gate prompt appears."""
        generator = _make_generator_mock()
        generator.generate.return_value.files = [MagicMock(path="helper.py")]
        result = self._run_with_write(
            ["add a helper", "--write"],
            generator,
            approval_mode="auto",
        )
        assert "[gate]" not in result.output, result.output

    def test_invalid_approval_flag_fails_cleanly(self):
        """--approval with an invalid value exits with a usage error."""
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "some task", "--approval", "banana"])
        assert result.exit_code != 0
        assert "Invalid value" in result.output or "Error" in result.output

    def test_default_approval_mode_is_smart(self):
        """Empty config falls back to smart; smart mode prompts on a risky-path write."""
        generator = _make_generator_mock()
        generator.generate.return_value.files = [MagicMock(path="src/auth/login.py")]
        result = self._run_with_write(
            ["add a helper", "--write"],
            generator,
            config_override={},
            repo=_RISKY_PYTHON_REPO,
        )
        assert "[gate]" in result.output, result.output

    def test_approval_auto_flag_overrides_smart_config(self):
        """--approval auto suppresses gates even when config specifies smart."""
        generator = _make_generator_mock()
        generator.generate.return_value.files = [MagicMock(path="src/auth/login.py")]
        result = self._run_with_write(
            ["add a helper", "--write", "--approval", "auto"],
            generator,
            approval_mode="smart",
            repo=_RISKY_PYTHON_REPO,
        )
        assert "[gate]" not in result.output, result.output


class TestHistoryScoringDisplay(unittest.TestCase):
    """Verify history-scoring lines appear in --more output when the flag is set."""

    def _run(self, args: list[str], manager_mock, generator_mock,
             adjustments=None, reasons=None):
        adjustments = adjustments or {}
        reasons = reasons or {}
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager_mock), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run"), \
             patch("openshard.run.pipeline.load_runs", return_value=[]), \
             patch("openshard.run.pipeline.compute_history_adjustments", return_value=adjustments), \
             patch("openshard.run.pipeline.compute_history_adjustment_reasons", return_value=reasons):
            runner = CliRunner()
            result = runner.invoke(cli, ["run"] + args)
        return result

    def test_history_scoring_enabled_line_shown(self):
        """[routing] history scoring: enabled appears in --more output when flag is set."""
        entry = _make_entry("openrouter/fast-model", pricing={"prompt": "0.0000005"})
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()

        result = self._run(["implement a feature", "--more", "--history-scoring"], manager, generator)

        self.assertIn("History scoring: enabled", result.output, result.output)

    def test_history_nonzero_adjustment_shown(self):
        """Non-zero adjustment for selected model shows value and reason in --more output."""
        entry = _make_entry("openrouter/fast-model", pricing={"prompt": "0.0000005"})
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()
        adjustments = {"openrouter/fast-model": 1.0}
        reasons = {"openrouter/fast-model": "high pass rate"}

        result = self._run(
            ["implement a feature", "--more", "--history-scoring"],
            manager, generator,
            adjustments=adjustments, reasons=reasons,
        )

        self.assertIn("+1.0", result.output, result.output)
        self.assertIn("high pass rate", result.output, result.output)
        self.assertIn("<- selected", result.output, result.output)

    def test_history_scoring_hidden_without_flag(self):
        """history scoring lines must NOT appear when --history-scoring is absent."""
        entry = _make_entry("openrouter/fast-model", pricing={"prompt": "0.0000005"})
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()

        result = self._run(["implement a feature", "--more"], manager, generator)

        self.assertNotIn("history scoring", result.output, result.output)


class TestDeepSeekBoilerplateModel(unittest.TestCase):
    """DeepSeek V4 Flash is the boilerplate model; V3.2 is no longer the default."""

    def test_boilerplate_keyword_routes_to_v4_flash(self):
        from openshard.routing.engine import MODEL_CHEAP, route
        decision = route("add a simple validation helper")
        self.assertEqual(decision.category, "boilerplate")
        self.assertEqual(decision.model, MODEL_CHEAP)
        self.assertIn("v4.1-flash", MODEL_CHEAP)  # the current Flash, never the retired v4-flash alias

    def test_model_cheap_is_not_v3_2(self):
        from openshard.routing.engine import MODEL_CHEAP
        self.assertNotIn("v3.2", MODEL_CHEAP)


class TestExecutionProfileDisplay(unittest.TestCase):
    """Verify [profile] line appears in --more output and --profile override is respected."""

    def _run(self, args: list[str]):
        manager = _make_manager_mock([], ["openrouter"])
        generator = _make_generator_mock()
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run"):
            runner = CliRunner()
            result = runner.invoke(cli, ["run"] + args)
        return result

    def test_more_shows_profile_line(self):
        result = self._run(["implement a feature", "--more"])
        self.assertIn("Execution", result.output, result.output)

    def test_security_task_shows_native_deep(self):
        result = self._run(["add login endpoint with jwt auth", "--more"])
        self.assertIn("Deep Run", result.output, result.output)

    def test_simple_task_shows_native_light(self):
        result = self._run(["fix typo in README", "--more"])
        self.assertIn("Run", result.output, result.output)

    def test_profile_override_native_swarm(self):
        result = self._run(["fix typo in README", "--more", "--profile", "native_swarm"])
        self.assertIn("Deep Run", result.output, result.output)
        self.assertIn("explicit override", result.output, result.output)

    def test_profile_line_absent_without_more(self):
        result = self._run(["implement a feature"])
        self.assertNotIn("[profile]", result.output, result.output)

    def test_readonly_task_shows_ask(self):
        result = self._run(["what does this function do", "--more"])
        self.assertIn("Mode: Ask", result.output, result.output)
        self.assertNotIn("Mode: Run", result.output, result.output)
        self.assertNotIn("Mode: Deep Run", result.output, result.output)


class TestHistoryScoringProfileSelection(unittest.TestCase):
    """Verify --history-scoring wires profile history into select_profile()."""

    _POOR_PASS_RUNS = [
        {"execution_profile": "native_light", "verification_passed": False}
        for _ in range(5)
    ]
    _HIGH_RETRY_RUNS = [
        {"execution_profile": "native_light", "verification_passed": True, "retry_triggered": True}
        for _ in range(5)
    ]

    def _run(self, args: list[str], runs: list[dict] | None = None):
        manager = _make_manager_mock([], ["openrouter"])
        generator = _make_generator_mock()
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run"), \
             patch("openshard.run.pipeline.load_runs", return_value=runs or []):
            runner = CliRunner()
            result = runner.invoke(cli, ["run"] + args)
        return result

    def test_without_history_scoring_poor_history_does_not_escalate(self):
        result = self._run(["fix typo in README", "--more"], runs=self._POOR_PASS_RUNS)
        self.assertIn("Run", result.output, result.output)
        self.assertNotIn("Deep Run", result.output, result.output)

    def test_history_scoring_poor_pass_rate_escalates_to_native_deep(self):
        result = self._run(["fix typo in README", "--more", "--history-scoring"], runs=self._POOR_PASS_RUNS)
        self.assertIn("Deep Run", result.output, result.output)

    def test_history_scoring_high_retry_rate_escalates_to_native_deep(self):
        result = self._run(["fix typo in README", "--more", "--history-scoring"], runs=self._HIGH_RETRY_RUNS)
        self.assertIn("Deep Run", result.output, result.output)

    def test_profile_override_wins_even_with_poor_history(self):
        result = self._run(
            ["fix typo in README", "--more", "--history-scoring", "--profile", "native_light"],
            runs=self._POOR_PASS_RUNS,
        )
        self.assertIn("Run", result.output, result.output)
        self.assertIn("explicit override", result.output, result.output)

    def test_native_swarm_never_auto_selected_with_history_scoring(self):
        result = self._run(["fix typo in README", "--more", "--history-scoring"], runs=self._POOR_PASS_RUNS)
        lines = [ln for ln in result.output.splitlines() if "Mode:" in ln]
        for line in lines:
            self.assertNotIn("native_swarm", line, result.output)


class TestVerificationPlanDisplay(unittest.TestCase):

    def _run(self, args: list[str], repo_facts=None, config=None):
        manager_mock = _make_manager_mock(
            [_make_entry("openrouter/fast-model", pricing={"prompt": 1.0, "completion": 1.0})],
            ["openrouter"],
        )
        generator_mock = _make_generator_mock()
        cfg = config if config is not None else _DEFAULT_CONFIG
        facts = repo_facts if repo_facts is not None else _PYTHON_REPO
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager_mock), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock), \
             patch("openshard.cli.main.load_config", return_value=cfg), \
             patch("openshard.run.pipeline.analyze_repo", return_value=facts), \
             patch("openshard.run.pipeline._log_run"):
            runner = CliRunner()
            result = runner.invoke(cli, ["run"] + args)
        return result

    def test_detected_pytest_shown_in_more(self):
        repo = RepoFacts(
            languages=["python"], package_files=[], framework=None,
            test_command="pytest", risky_paths=[], changed_files=[],
        )
        result = self._run(["fix a bug", "--more"], repo_facts=repo)
        self.assertIn("Verification", result.output, result.output)
        self.assertIn("safe", result.output, result.output)
        self.assertIn("detected", result.output, result.output)

    def test_config_command_shown_in_more(self):
        cfg = {"approval_mode": "smart", "verification_command": ["pytest"]}
        result = self._run(["fix a bug", "--more"], config=cfg)
        self.assertIn("Verification", result.output, result.output)
        self.assertIn("config", result.output, result.output)

    def test_config_takes_priority_over_detected(self):
        repo = RepoFacts(
            languages=["python"], package_files=[], framework=None,
            test_command="npm test", risky_paths=[], changed_files=[],
        )
        cfg = {"approval_mode": "smart", "verification_command": ["pytest"]}
        result = self._run(["fix a bug", "--more"], repo_facts=repo, config=cfg)
        self.assertIn("config", result.output, result.output)
        self.assertNotIn("detected", result.output, result.output)

    def test_no_command_shows_not_detected(self):
        result = self._run(["fix a bug", "--more"])
        self.assertIn("No verification command detected", result.output, result.output)

    def test_not_shown_in_default_detail(self):
        repo = RepoFacts(
            languages=["python"], package_files=[], framework=None,
            test_command="pytest", risky_paths=[], changed_files=[],
        )
        result = self._run(["fix a bug"], repo_facts=repo)
        self.assertNotIn("[verification]", result.output, result.output)


_DISPATCH_ENTRY = _make_entry("openrouter/test-model", pricing={"prompt": "0.0000005"})
_DISPATCH_ROUTED = "openrouter/test-model"


class TestTierDispatchRouting(unittest.TestCase):
    """Verify --experimental-tier-dispatch wires dispatch models into staged execution."""

    def _run_staged(self, extra_args: list[str]):
        manager = _make_manager_mock([_DISPATCH_ENTRY], ["openrouter"])
        generator = _make_generator_mock()
        plan_mock = MagicMock(return_value=("mock plan", None))
        log_mock = MagicMock()
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline.run_planning_stage", plan_mock), \
             patch("openshard.run.pipeline.select_workflow",
                   return_value=WorkflowDecision("staged", "test forced")), \
             patch("openshard.run.pipeline._log_run", log_mock):
            runner = CliRunner()
            runner.invoke(cli, ["run", "implement a feature"] + extra_args)
        return generator, plan_mock, log_mock

    def test_flag_off_uses_routed_model(self):
        """Without dispatch flag, implementation stage uses the scored-routing model."""
        generator, _, _ = self._run_staged([])
        generator.generate.assert_called_once()
        self.assertEqual(generator.generate.call_args.kwargs["model"], _DISPATCH_ROUTED)

    def _run_staged_with_config(self, config: dict):
        manager = _make_manager_mock([_DISPATCH_ENTRY], ["openrouter"])
        generator = _make_generator_mock()
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator), \
             patch("openshard.cli.main.load_config", return_value=config), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline.run_planning_stage",
                   return_value=("mock plan", None)), \
             patch("openshard.run.pipeline.select_workflow",
                   return_value=WorkflowDecision("staged", "test forced")), \
             patch("openshard.run.pipeline._log_run"):
            runner = CliRunner()
            runner.invoke(cli, ["run", "implement a feature"])  # no CLI dispatch flag
        return generator

    def test_config_enables_dispatch_without_flag(self):
        """tier_dispatch: true in config enables per-role dispatch with no CLI flag."""
        generator = self._run_staged_with_config({**_DEFAULT_CONFIG, "tier_dispatch": True})
        generator.generate.assert_called_once()
        self.assertEqual(generator.generate.call_args.kwargs["model"], MODEL_MAIN)

    def test_config_false_leaves_dispatch_off(self):
        """tier_dispatch: false (the default) leaves single-model routing in place."""
        generator = self._run_staged_with_config({**_DEFAULT_CONFIG, "tier_dispatch": False})
        generator.generate.assert_called_once()
        self.assertEqual(generator.generate.call_args.kwargs["model"], _DISPATCH_ROUTED)

    def test_flag_on_executor_uses_dispatch_model(self):
        """With dispatch flag, standard task routes implementation to MODEL_MAIN (GLM-5.1).

        This test would fail if the implementation stage still used _routed_model
        (_DISPATCH_ROUTED) instead of _dispatch_executor_model (MODEL_MAIN).
        """
        generator, _, _ = self._run_staged(["--experimental-tier-dispatch"])
        generator.generate.assert_called_once()
        self.assertEqual(generator.generate.call_args.kwargs["model"], MODEL_MAIN)

    def test_flag_on_planner_uses_dispatch_model(self):
        """With dispatch flag, planning stage receives frontier-reasoning-model (MODEL_STRONG)."""
        _, plan_mock, _ = self._run_staged(["--experimental-tier-dispatch"])
        plan_mock.assert_called_once()
        self.assertEqual(plan_mock.call_args.kwargs["model"], MODEL_STRONG)

    def test_flag_on_stage_runs_logged_with_dispatch_models(self):
        """With dispatch flag, _log_run gets planning + implementation + validation stage_runs."""
        _, _, log_mock = self._run_staged(["--experimental-tier-dispatch"])
        log_mock.assert_called_once()
        stage_runs = log_mock.call_args.kwargs.get("stage_runs", [])
        self.assertEqual(len(stage_runs), 3)
        plan_run = next(sr for sr in stage_runs if sr.stage.stage_type == "planning")
        impl_run = next(sr for sr in stage_runs if sr.stage.stage_type == "implementation")
        val_run = next(sr for sr in stage_runs if sr.stage.stage_type == "validation")
        self.assertEqual(plan_run.model, MODEL_STRONG)
        self.assertEqual(impl_run.model, MODEL_MAIN)
        self.assertEqual(val_run.model, MODEL_STRONG)

    def test_unresolved_executor_falls_back_to_routed(self):
        """When dispatch executor resolves to None (unknown category), falls back to routed model."""
        manager = _make_manager_mock([_DISPATCH_ENTRY], ["openrouter"])
        generator = _make_generator_mock()
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline.run_planning_stage",
                   return_value=("mock plan", None)), \
             patch("openshard.run.pipeline.select_workflow",
                   return_value=WorkflowDecision("staged", "test forced")), \
             patch("openshard.native.dispatch.resolve_tier_for_category",
                   return_value=(None, "", True, "unknown category in test")), \
             patch("openshard.run.pipeline._log_run"):
            runner = CliRunner()
            runner.invoke(cli, ["run", "implement a feature", "--experimental-tier-dispatch"])
        generator.generate.assert_called_once()
        self.assertEqual(generator.generate.call_args.kwargs["model"], _DISPATCH_ROUTED)

    def test_unroutable_role_models_fall_back_to_single(self):
        """When the routable pool excludes a role's tier models, per-role
        dispatch falls back to single-model execution rather than dispatching
        to a model whose provider is not configured."""
        from openshard.models.registry import get_model
        from openshard.routing.provider_availability import RoutablePool
        # Non-empty pool, but the executor tier's models (balanced-coding:
        # MODEL_MAIN plus its MODEL_CHEAP fallback) are excluded, so the
        # executor role resolves to None and dispatch falls back to single.
        pool = RoutablePool(
            routable=(get_model(MODEL_STRONG),),
            excluded=((MODEL_MAIN, "no_api_key"), (MODEL_CHEAP, "no_api_key")),
            available_providers=("openrouter",),
            executor="staged",
        )
        manager = _make_manager_mock([_DISPATCH_ENTRY], ["openrouter"])
        generator = _make_generator_mock()
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator), \
             patch("openshard.run.pipeline.build_routable_pool", return_value=pool), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline.run_planning_stage",
                   return_value=("mock plan", None)), \
             patch("openshard.run.pipeline.select_workflow",
                   return_value=WorkflowDecision("staged", "test forced")), \
             patch("openshard.run.pipeline._log_run"):
            runner = CliRunner()
            runner.invoke(cli, ["run", "implement a feature", "--experimental-tier-dispatch"])
        generator.generate.assert_called_once()
        self.assertEqual(generator.generate.call_args.kwargs["model"], _DISPATCH_ROUTED)


class TestFeedbackScoringDisplay(unittest.TestCase):
    """Verify feedback-scoring lines appear in --more output only when the flag is set."""

    def _run(self, args, manager_mock, generator_mock, adjustments=None, reasons=None):
        adjustments = adjustments or {}
        reasons = reasons or {}
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager_mock), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator_mock), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.run.pipeline._log_run"), \
             patch("openshard.run.pipeline.load_runs", return_value=[]), \
             patch(
                 "openshard.run.pipeline.compute_feedback_adjustments",
                 return_value=adjustments,
             ), \
             patch(
                 "openshard.run.pipeline.compute_feedback_adjustment_reasons",
                 return_value=reasons,
             ):
            runner = CliRunner()
            result = runner.invoke(cli, ["run"] + args)
        return result

    def test_feedback_scoring_enabled_line_shown(self):
        """[routing] Feedback scoring: enabled appears in --more output when flag is set."""
        entry = _make_entry("openrouter/fast-model", pricing={"prompt": "0.0000005"})
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()
        result = self._run(
            ["implement a feature", "--more", "--feedback-scoring"],
            manager, generator,
        )
        self.assertIn("Feedback scoring: enabled", result.output, result.output)

    def test_feedback_nonzero_adjustment_shown_with_reason(self):
        """Non-zero adjustment for a candidate model shows value and reason in --more output."""
        entry = _make_entry("openrouter/fast-model", pricing={"prompt": "0.0000005"})
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()
        adjustments = {"openrouter/fast-model": -0.25}
        reasons = {"openrouter/fast-model": "feedback: 3 rejected"}
        result = self._run(
            ["implement a feature", "--more", "--feedback-scoring"],
            manager, generator,
            adjustments=adjustments, reasons=reasons,
        )
        self.assertIn("-0.2", result.output, result.output)
        self.assertIn("feedback: 3 rejected", result.output, result.output)

    def test_no_adjustment_shows_no_relevant_feedback_line(self):
        """When no candidates have nonzero adjustments, the no-adjustment message is shown."""
        entry = _make_entry("openrouter/fast-model", pricing={"prompt": "0.0000005"})
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()
        result = self._run(
            ["implement a feature", "--more", "--feedback-scoring"],
            manager, generator,
            adjustments={}, reasons={},
        )
        self.assertIn("No relevant feedback (no adjustment)", result.output, result.output)

    def test_feedback_scoring_hidden_without_flag(self):
        """Feedback scoring lines must NOT appear when --feedback-scoring is absent."""
        entry = _make_entry("openrouter/fast-model", pricing={"prompt": "0.0000005"})
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()
        result = self._run(["implement a feature", "--more"], manager, generator)
        self.assertNotIn("Feedback scoring", result.output, result.output)


class TestFailureMemoryRoutingIntegration(unittest.TestCase):
    """Failure-memory adjustments flow through the routing merge end to end.
    Active by default (no flag), so no flag is passed."""

    def _events(self, model: str, n: int):
        from openshard.history.failure_memory import NativeFailureMemoryEvent
        return [
            NativeFailureMemoryEvent(
                model=model, failure_type="test_failure",
                retry_attempted=True, retry_succeeded=False,
            )
            for _ in range(n)
        ]

    def test_failure_adjustments_reach_run_metadata(self):
        entry = _make_entry("openrouter/fast-model", pricing={"prompt": "0.0000005"})
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()
        log_mock = MagicMock()
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.history.failure_memory.load_failure_memory_events",
                   return_value=self._events("openrouter/fast-model", 3)), \
             patch("openshard.run.pipeline._log_run", log_mock):
            runner = CliRunner()
            result = runner.invoke(cli, ["run", "implement a feature"])
        self.assertEqual(result.exit_code, 0, result.output)
        log_mock.assert_called_once()
        meta = log_mock.call_args.kwargs.get("extra_metadata") or {}
        self.assertTrue(meta.get("routing_failure_memory_scoring_used"))
        self.assertIn(
            "openrouter/fast-model",
            meta.get("routing_failure_memory_adjustments", {}),
        )

    def test_disabled_by_config_skips_failure_signal(self):
        entry = _make_entry("openrouter/fast-model", pricing={"prompt": "0.0000005"})
        manager = _make_manager_mock([entry], ["openrouter"])
        generator = _make_generator_mock()
        log_mock = MagicMock()
        with patch("openshard.run.pipeline.ProviderManager", return_value=manager), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=generator), \
             patch("openshard.cli.main.load_config",
                   return_value={"approval_mode": "smart", "failure_memory_scoring": False}), \
             patch("openshard.run.pipeline.analyze_repo", return_value=_PYTHON_REPO), \
             patch("openshard.history.failure_memory.load_failure_memory_events",
                   return_value=self._events("openrouter/fast-model", 3)), \
             patch("openshard.run.pipeline._log_run", log_mock):
            runner = CliRunner()
            result = runner.invoke(cli, ["run", "implement a feature"])
        self.assertEqual(result.exit_code, 0, result.output)
        meta = log_mock.call_args.kwargs.get("extra_metadata") or {}
        self.assertNotIn("routing_failure_memory_scoring_used", meta)
