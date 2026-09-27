"""Tests for first-run UX: load_config() defaults, env-var injection, provider detection."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.config.settings import (
    detect_provider,
    is_agent_environment,
    is_ci_or_agent_environment,
    load_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NO_API_KEYS = {
    "OPENROUTER_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "OPENAI_API_KEY": "",
    "OPENSHARD_CONFIG": "",
}

_REQUIRED_KEYS = {"model_tiers", "planning_model", "execution_model", "workflow", "approval_mode"}


def _runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# 1. load_config() returns defaults when no file exists (never raises)
# ---------------------------------------------------------------------------


class TestLoadConfigNoFile(unittest.TestCase):

    def test_load_config_returns_defaults_when_no_file(self):
        """In an isolated dir with no config, load_config() must return a dict, not raise."""
        with patch.dict(os.environ, _NO_API_KEYS, clear=False):
            with _runner().isolated_filesystem():
                result = load_config()
                self.assertIsInstance(result, dict)

    def test_load_config_default_has_required_keys(self):
        """Returned default dict has the keys the pipeline expects."""
        with patch.dict(os.environ, _NO_API_KEYS, clear=False):
            with _runner().isolated_filesystem():
                cfg = load_config()
                for key in _REQUIRED_KEYS:
                    self.assertIn(key, cfg, f"Missing required key: {key!r}")
                self.assertIsInstance(cfg["model_tiers"], list)
                self.assertGreater(len(cfg["model_tiers"]), 0)


# ---------------------------------------------------------------------------
# 2. Env-var API key injection into default config
# ---------------------------------------------------------------------------


class TestLoadConfigApiKeyInjection(unittest.TestCase):

    def test_load_config_injects_openrouter_key_from_env(self):
        """When OPENROUTER_API_KEY is set, the default config contains it."""
        env = {**_NO_API_KEYS, "OPENROUTER_API_KEY": "test-openrouter-key"}
        with patch.dict(os.environ, env, clear=False):
            with _runner().isolated_filesystem():
                cfg = load_config()
                self.assertEqual(cfg.get("openrouter_api_key"), "test-openrouter-key")

    def test_load_config_injects_anthropic_key_from_env(self):
        """When ANTHROPIC_API_KEY is set, the default config contains it."""
        env = {**_NO_API_KEYS, "ANTHROPIC_API_KEY": "test-anthropic-key"}
        with patch.dict(os.environ, env, clear=False):
            with _runner().isolated_filesystem():
                cfg = load_config()
                self.assertEqual(cfg.get("anthropic_api_key"), "test-anthropic-key")

    def test_load_config_injects_openai_key_from_env(self):
        """When OPENAI_API_KEY is set, the default config contains it."""
        env = {**_NO_API_KEYS, "OPENAI_API_KEY": "test-openai-key"}
        with patch.dict(os.environ, env, clear=False):
            with _runner().isolated_filesystem():
                cfg = load_config()
                self.assertEqual(cfg.get("openai_api_key"), "test-openai-key")

    def test_load_config_env_key_not_injected_when_absent(self):
        """Unset env vars do not appear as keys (or are None/empty) in the default config."""
        with patch.dict(os.environ, _NO_API_KEYS, clear=False):
            with _runner().isolated_filesystem():
                cfg = load_config()
                # Key should not be present or should be falsy when env var is empty/unset
                self.assertFalse(cfg.get("openrouter_api_key"))
                self.assertFalse(cfg.get("anthropic_api_key"))
                self.assertFalse(cfg.get("openai_api_key"))


# ---------------------------------------------------------------------------
# 3. Existing config file is still read normally
# ---------------------------------------------------------------------------


class TestLoadConfigFileStillWorks(unittest.TestCase):

    def test_load_config_file_still_works_when_present(self):
        """An existing .openshard/config.yml is read normally — injection path not taken."""
        env = {**_NO_API_KEYS, "OPENROUTER_API_KEY": "should-not-appear"}
        with patch.dict(os.environ, env, clear=False):
            with _runner().isolated_filesystem():
                config_dir = Path(".openshard")
                config_dir.mkdir(parents=True)
                config_file = config_dir / "config.yml"
                file_data = {
                    "approval_mode": "ask",
                    "model_tiers": [],
                    "workflow": "direct",
                }
                config_file.write_text(
                    yaml.safe_dump(file_data, sort_keys=False), encoding="utf-8"
                )
                cfg = load_config()
                # Config values from file are present
                self.assertEqual(cfg["approval_mode"], "ask")
                self.assertEqual(cfg["workflow"], "direct")
                # API key is NOT injected when reading from a real file
                self.assertNotIn("openrouter_api_key", cfg)


# ---------------------------------------------------------------------------
# 4. openshard init --help still exits 0 (init command still works)
# ---------------------------------------------------------------------------


class TestInitCommandStillWorks(unittest.TestCase):

    def test_init_command_still_exits_zero(self):
        """openshard init --help still exits 0."""
        result = _runner().invoke(cli, ["init", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)


# ---------------------------------------------------------------------------
# 5. detect_provider: auto-detects from available API keys
# ---------------------------------------------------------------------------

_NO_KEYS = {
    "OPENROUTER_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "OPENAI_API_KEY": "",
    "CI": "",
    "GITHUB_ACTIONS": "",
    "GITLAB_CI": "",
    "NO_COLOR": "",
    "OPENSHARD_AGENT": "",
    "OPENSHARD_CONFIG": "",
}


class TestDetectProvider(unittest.TestCase):

    def test_returns_openrouter_when_key_set(self):
        with patch.dict(os.environ, {**_NO_KEYS, "OPENROUTER_API_KEY": "key"}, clear=True):
            self.assertEqual(detect_provider(), "openrouter")

    def test_returns_anthropic_without_openrouter(self):
        with patch.dict(os.environ, {**_NO_KEYS, "ANTHROPIC_API_KEY": "ant"}, clear=True):
            self.assertEqual(detect_provider(), "anthropic")

    def test_returns_openai_when_only_openai_set(self):
        with patch.dict(os.environ, {**_NO_KEYS, "OPENAI_API_KEY": "oai"}, clear=True):
            self.assertEqual(detect_provider(), "openai")

    def test_openrouter_wins_over_anthropic(self):
        env = {**_NO_KEYS, "OPENROUTER_API_KEY": "or", "ANTHROPIC_API_KEY": "ant"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(detect_provider(), "openrouter")

    def test_raises_when_no_key_set(self):
        with patch.dict(os.environ, _NO_KEYS, clear=True):
            with self.assertRaises(ValueError) as cm:
                detect_provider()
            msg = str(cm.exception)
            self.assertIn("OPENROUTER_API_KEY", msg)
            self.assertIn("ANTHROPIC_API_KEY", msg)
            self.assertIn("OPENAI_API_KEY", msg)
            self.assertIn("openshard init", msg)


# ---------------------------------------------------------------------------
# 6. is_agent_environment: CI/agent env var detection
# ---------------------------------------------------------------------------


class TestIsAgentEnvironment(unittest.TestCase):

    def test_false_by_default(self):
        with patch.dict(os.environ, _NO_KEYS, clear=True):
            self.assertFalse(is_agent_environment())

    def test_true_for_ci(self):
        with patch.dict(os.environ, {**_NO_KEYS, "CI": "true"}, clear=True):
            self.assertTrue(is_agent_environment())

    def test_true_for_github_actions(self):
        with patch.dict(os.environ, {**_NO_KEYS, "GITHUB_ACTIONS": "true"}, clear=True):
            self.assertTrue(is_agent_environment())

    def test_true_for_openshard_agent(self):
        with patch.dict(os.environ, {**_NO_KEYS, "OPENSHARD_AGENT": "1"}, clear=True):
            self.assertTrue(is_agent_environment())

    def test_false_for_no_color(self):
        """NO_COLOR is a colour preference, not an agent environment (audit fix)."""
        with patch.dict(os.environ, {**_NO_KEYS, "NO_COLOR": "1"}, clear=True):
            self.assertFalse(is_agent_environment())

    def test_never_raises(self):
        with patch.dict(os.environ, _NO_KEYS, clear=True):
            try:
                result = is_agent_environment()
                self.assertIsInstance(result, bool)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"is_agent_environment() raised unexpectedly: {exc}")


class TestIsCiOrAgentEnvironment(unittest.TestCase):
    """is_ci_or_agent_environment() is the onboarding-gate predicate; unlike
    is_agent_environment() it excludes NO_COLOR (a human colour preference)."""

    def test_false_by_default(self):
        with patch.dict(os.environ, _NO_KEYS, clear=True):
            self.assertFalse(is_ci_or_agent_environment())

    def test_true_for_ci(self):
        with patch.dict(os.environ, {**_NO_KEYS, "CI": "true"}, clear=True):
            self.assertTrue(is_ci_or_agent_environment())

    def test_true_for_github_actions(self):
        with patch.dict(os.environ, {**_NO_KEYS, "GITHUB_ACTIONS": "true"}, clear=True):
            self.assertTrue(is_ci_or_agent_environment())

    def test_true_for_gitlab_ci(self):
        with patch.dict(os.environ, {**_NO_KEYS, "GITLAB_CI": "true"}, clear=True):
            self.assertTrue(is_ci_or_agent_environment())

    def test_true_for_openshard_agent(self):
        with patch.dict(os.environ, {**_NO_KEYS, "OPENSHARD_AGENT": "1"}, clear=True):
            self.assertTrue(is_ci_or_agent_environment())

    def test_false_for_no_color(self):
        """NO_COLOR alone is NOT an agent/CI signal (regression guard)."""
        with patch.dict(os.environ, {**_NO_KEYS, "NO_COLOR": "1"}, clear=True):
            self.assertFalse(is_ci_or_agent_environment())


# ---------------------------------------------------------------------------
# 7. load_config: agent mode injection
# ---------------------------------------------------------------------------


class TestLoadConfigAgentMode(unittest.TestCase):

    def test_sets_agent_json_in_ci_without_config(self):
        with _runner().isolated_filesystem():
            with patch.dict(os.environ, {**_NO_KEYS, "CI": "true"}, clear=True):
                cfg = load_config()
        self.assertEqual(cfg.get("output_mode"), "agent_json")

    def test_does_not_override_explicit_config_file(self):
        with _runner().isolated_filesystem():
            Path(".openshard").mkdir()
            Path(".openshard/config.yml").write_text(
                yaml.safe_dump({"output_mode": "human"}), encoding="utf-8"
            )
            with patch.dict(os.environ, {**_NO_KEYS, "CI": "true"}, clear=True):
                cfg = load_config()
        self.assertEqual(cfg.get("output_mode"), "human")


# ---------------------------------------------------------------------------
# 8. run command: clean error message when no API key
# ---------------------------------------------------------------------------


class TestRunNoKeyCleanError(unittest.TestCase):

    def test_no_traceback_on_missing_key(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_KEYS, clear=True):
                result = runner.invoke(cli, ["run", "say hello"])
        self.assertNotIn("Traceback", result.output)
        self.assertNotIn("RuntimeError", result.output)

    def test_error_mentions_all_three_providers(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_KEYS, clear=True):
                result = runner.invoke(cli, ["run", "say hello"])
        self.assertIn("OPENROUTER_API_KEY", result.output)
        self.assertIn("ANTHROPIC_API_KEY", result.output)
        self.assertIn("OPENAI_API_KEY", result.output)

    def test_error_suggests_init(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_KEYS, clear=True):
                result = runner.invoke(cli, ["run", "say hello"])
        self.assertIn("openshard init", result.output)

    def test_exits_nonzero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_KEYS, clear=True):
                result = runner.invoke(cli, ["run", "say hello"])
        self.assertNotEqual(result.exit_code, 0)


# ---------------------------------------------------------------------------
# 9. openshard env command
# ---------------------------------------------------------------------------


class TestEnvCommand(unittest.TestCase):

    def _run(self, extra_env: dict | None = None) -> object:
        env = dict(_NO_KEYS)
        if extra_env:
            env.update(extra_env)
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, env, clear=True):
                return runner.invoke(cli, ["env"])

    def test_exits_zero(self):
        result = self._run()
        self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_shows_agent_no_by_default(self):
        result = self._run()
        self.assertIn("no", result.output)

    def test_shows_agent_yes_with_ci(self):
        result = self._run({"CI": "true"})
        self.assertIn("yes", result.output)
        self.assertIn("CI", result.output)

    def test_never_prints_key_value(self):
        result = self._run({"OPENROUTER_API_KEY": "sk-super-secret-key-xyz"})
        self.assertNotIn("sk-super-secret-key-xyz", result.output)

    def test_shows_provider_name_when_key_present(self):
        result = self._run({"OPENROUTER_API_KEY": "any-key"})
        self.assertIn("openrouter", result.output)

    def test_shows_not_set_when_no_key(self):
        result = self._run()
        self.assertIn("not set", result.output)


# ---------------------------------------------------------------------------
# 10. First-run gate: _should_run_onboarding + CLI entrypoint
# ---------------------------------------------------------------------------

_NO_AGENT_ENV = {
    **_NO_API_KEYS,
    "CI": "",
    "GITHUB_ACTIONS": "",
    "GITLAB_CI": "",
    "OPENSHARD_AGENT": "",
    "NO_COLOR": "",
}


class TestFirstRunGate(unittest.TestCase):

    def test_cli_no_args_calls_render_home_after_onboarding_complete(self):
        """After completed_at set, openshard no-args calls render_home (not onboarding)."""
        import yaml
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".openshard").mkdir()
            Path(".openshard/config.yml").write_text(
                yaml.safe_dump({"onboarding": {"completed_at": "2026-06-13T10:00:00+00:00"}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                with patch("openshard.cli.ui.onboarding.run_onboarding_flow") as mock_flow:
                    with patch("openshard.cli.ui.home.render_home") as mock_home:
                        runner.invoke(cli, [])
        mock_flow.assert_not_called()
        mock_home.assert_called_once()

    def test_cli_no_args_does_not_launch_textual_in_ci(self):
        """In CI environment, run_onboarding_flow is never called."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, {**_NO_AGENT_ENV, "CI": "true"}, clear=False):
                with patch("openshard.cli.ui.onboarding.run_onboarding_flow") as mock_flow:
                    with patch("openshard.cli.ui.home.render_home"):
                        runner.invoke(cli, [])
        mock_flow.assert_not_called()

    def test_skip_writes_completed_at(self):
        """After run_onboarding_flow writes a skip, completed_at is present in config."""
        import yaml
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                # Simulate _write_onboarding_config with skipped=True
                from openshard.cli.ui.onboarding import _write_onboarding_config
                _write_onboarding_config({"skipped": True})
                cfg = yaml.safe_load(Path(".openshard/config.yml").read_text(encoding="utf-8"))
        self.assertIn("completed_at", cfg["onboarding"])
        self.assertTrue(cfg["onboarding"]["skipped"])

    def test_skip_writes_marker_so_flow_does_not_reappear(self):
        """After skip, is_first_run() returns False."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, {**_NO_AGENT_ENV, "OPENSHARD_CONFIG": ""}, clear=False):
                from openshard.cli.ui.onboarding import _write_onboarding_config
                _write_onboarding_config({"skipped": True})
                # Point config loader at the written file
                cfg_path = str(Path(".openshard/config.yml"))
                with patch.dict(os.environ, {"OPENSHARD_CONFIG": cfg_path}, clear=False):
                    from openshard.config.onboarding import is_first_run
                    self.assertFalse(is_first_run())

    def test_api_key_status_shows_no_values(self):
        """_build_key_status_body() output contains no actual key values."""
        from openshard.cli.ui.onboarding import _build_key_status_body
        env = {**_NO_AGENT_ENV, "ANTHROPIC_API_KEY": "sk-super-secret-anthropic-key-xyz"}
        with patch.dict(os.environ, env, clear=False):
            body = _build_key_status_body()
        self.assertNotIn("sk-super-secret-anthropic-key-xyz", body)
        self.assertIn("✓ set", body)

    def test_api_key_status_no_keys_shows_not_set(self):
        """_build_key_status_body() shows not-set when no keys present."""
        from openshard.cli.ui.onboarding import _build_key_status_body
        with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
            body = _build_key_status_body()
        self.assertIn("✗ not set", body)

    def test_no_api_keys_written_to_config(self):
        """After writing onboarding config, no _API_KEY appears in config file."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, {**_NO_AGENT_ENV, "ANTHROPIC_API_KEY": "sk-secret"}, clear=False):
                from openshard.cli.ui.onboarding import _write_onboarding_config
                _write_onboarding_config({"user_type": "human", "executor": "native",
                                          "provider_route": "openrouter", "provider": "openrouter",
                                          "safety_profile": "recommended"})
                raw = Path(".openshard/config.yml").read_text(encoding="utf-8")
        self.assertNotIn("API_KEY", raw)
        self.assertNotIn("sk-secret", raw)

    def test_onboarding_config_saves_executor(self):
        """_write_onboarding_config saves executor field."""
        import yaml
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                from openshard.cli.ui.onboarding import _write_onboarding_config
                _write_onboarding_config({"executor": "claude_code"})
                cfg = yaml.safe_load(Path(".openshard/config.yml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["onboarding"]["executor"], "claude_code")

    def test_onboarding_config_saves_provider_route(self):
        """_write_onboarding_config saves provider_route field."""
        import yaml
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                from openshard.cli.ui.onboarding import _write_onboarding_config
                _write_onboarding_config({"provider_route": "direct", "provider": "anthropic"})
                cfg = yaml.safe_load(Path(".openshard/config.yml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["onboarding"]["provider_route"], "direct")
        self.assertEqual(cfg["onboarding"]["provider"], "anthropic")

    def test_onboarding_config_saves_safety_profile(self):
        """_write_onboarding_config saves safety_profile field."""
        import yaml
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                from openshard.cli.ui.onboarding import _write_onboarding_config
                _write_onboarding_config({"safety_profile": "strict"})
                cfg = yaml.safe_load(Path(".openshard/config.yml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["onboarding"]["safety_profile"], "strict")

    def test_planned_executor_saves_value_no_routing_change(self):
        """Selecting a planned executor saves its value; no routing fields are set."""
        import yaml
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                from openshard.cli.ui.onboarding import _write_onboarding_config
                _write_onboarding_config({"executor": "goose"})
                cfg = yaml.safe_load(Path(".openshard/config.yml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["onboarding"]["executor"], "goose")
        # No routing-related keys outside onboarding block
        self.assertNotIn("routing", cfg)

    def test_planned_direct_provider_saves_value(self):
        """Selecting a planned direct provider saves provider value."""
        import yaml
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                from openshard.cli.ui.onboarding import _write_onboarding_config
                _write_onboarding_config({"provider_route": "direct", "provider": "google"})
                cfg = yaml.safe_load(Path(".openshard/config.yml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["onboarding"]["provider"], "google")

    def test_onboarding_preserves_existing_config_keys(self):
        """Writing onboarding block preserves other config keys."""
        import yaml
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".openshard").mkdir()
            Path(".openshard/config.yml").write_text(
                yaml.safe_dump({"approval_mode": "ask", "workflow": "direct"}), encoding="utf-8"
            )
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                from openshard.cli.ui.onboarding import _write_onboarding_config
                _write_onboarding_config({"user_type": "human"})
                cfg = yaml.safe_load(Path(".openshard/config.yml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["approval_mode"], "ask")
        self.assertEqual(cfg["workflow"], "direct")

    def test_windows_path_compatibility(self):
        """Config path resolved via pathlib.Path — no hardcoded forward slashes."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                from openshard.cli.ui.onboarding import _write_onboarding_config
                _write_onboarding_config({"user_type": "human"})
                # File must exist (path was created correctly on this platform)
                cfg_path = Path(".openshard") / "config.yml"
                self.assertTrue(cfg_path.exists())


# ---------------------------------------------------------------------------
# 11. setup --agent --json command
# ---------------------------------------------------------------------------


class TestSetupAgentJson(unittest.TestCase):

    def _run(self, extra_env: dict | None = None) -> object:
        env = {**_NO_AGENT_ENV, **(extra_env or {})}
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, env, clear=False):
                return runner.invoke(cli, ["setup", "--agent", "--json"])

    def test_exits_zero(self):
        result = self._run()
        self.assertEqual(result.exit_code, 0, result.output)

    def test_returns_valid_json(self):
        import json
        result = self._run()
        data = json.loads(result.output)
        self.assertIsInstance(data, dict)

    def test_interactive_false(self):
        import json
        result = self._run()
        data = json.loads(result.output)
        self.assertFalse(data["interactive"])

    def test_onboarding_completed_false_when_no_config(self):
        import json
        result = self._run()
        data = json.loads(result.output)
        self.assertFalse(data["onboarding_completed"])

    def test_onboarding_completed_true_after_init(self):
        import json

        import yaml
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".openshard").mkdir()
            Path(".openshard/config.yml").write_text(
                yaml.safe_dump({"onboarding": {"completed_at": "2026-06-13T10:00:00+00:00"}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                result = runner.invoke(cli, ["setup", "--agent", "--json"])
        data = json.loads(result.output)
        self.assertTrue(data["onboarding_completed"])

    def test_detected_providers_booleans_only(self):
        import json
        result = self._run({"ANTHROPIC_API_KEY": "sk-test-key"})
        data = json.loads(result.output)
        # Key names are present but no key values in output
        self.assertNotIn("sk-test-key", result.output)
        self.assertIn("anthropic", data["detected_providers"])

    def test_next_actions_present(self):
        import json
        result = self._run()
        data = json.loads(result.output)
        self.assertIsInstance(data["next_actions"], list)
        self.assertGreater(len(data["next_actions"]), 0)

    def test_clearer_fields_present(self):
        import json
        result = self._run()
        data = json.loads(result.output)
        for field in ("selected_route", "selected_provider", "available_routes",
                      "recommended_next_action"):
            self.assertIn(field, data)
        # Back-compat fields are retained.
        self.assertIn("provider_route", data)
        self.assertIn("provider", data)

    def test_defaults_to_demo_when_unconfigured(self):
        import json
        result = self._run()
        data = json.loads(result.output)
        self.assertEqual(data["selected_route"], "demo")
        self.assertEqual(data["selected_provider"], "none")
        self.assertIn("demo", data["available_routes"])

    def test_recommended_next_action_when_key_but_demo(self):
        import json
        result = self._run({"OPENROUTER_API_KEY": "any-key"})
        data = json.loads(result.output)
        self.assertIn("openrouter", data["detected_providers"])
        self.assertIn("openrouter", data["available_routes"])
        self.assertIsNotNone(data["recommended_next_action"])
        self.assertIn("openshard setup", data["recommended_next_action"])

    def test_no_recommended_action_when_no_key(self):
        import json
        result = self._run()
        data = json.loads(result.output)
        self.assertIsNone(data["recommended_next_action"])

    def test_legacy_executor_value_preserved(self):
        import json

        import yaml
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".openshard").mkdir()
            Path(".openshard/config.yml").write_text(
                yaml.safe_dump({"onboarding": {
                    "completed_at": "2026-06-13T10:00:00+00:00",
                    "executor": "claude_code",
                }}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                result = runner.invoke(cli, ["setup", "--agent", "--json"])
        data = json.loads(result.output)
        # Legacy executor value passes through unchanged (advisory only).
        self.assertEqual(data["recommended_executor"], "claude_code")


# ---------------------------------------------------------------------------
# 12. Existing commands still work after onboarding changes
# ---------------------------------------------------------------------------


class TestExistingCommandsUnbroken(unittest.TestCase):

    def test_init_help_exits_zero(self):
        result = _runner().invoke(cli, ["init", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_env_exits_zero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                result = runner.invoke(cli, ["env"])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_doctor_exits_zero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                result = runner.invoke(cli, ["doctor"])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_init_yes_exits_zero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                result = runner.invoke(cli, ["init", "--yes"])
        self.assertEqual(result.exit_code, 0, result.output)


# ---------------------------------------------------------------------------
# 13. Interactive Textual flow — driven via the run_test pilot harness
# ---------------------------------------------------------------------------


class TestCliSelectScreenPilot(unittest.TestCase):
    """Arrow-key selection path is testable: drive _SelectScreen via pilot."""

    def test_navigate_and_confirm_selects_value(self):
        import asyncio

        from openshard.cli.ui.onboarding import _build_select_app_class
        from openshard.onboarding.choices import USER_TYPE_CHOICES

        async def _run():
            Sel = _build_select_app_class()
            app = Sel("Who is using OpenShard?", USER_TYPE_CHOICES)
            async with app.run_test() as pilot:
                await pilot.press("down")   # highlight index 1
                await pilot.press("enter")  # confirm
            return app.selected_value, app.skipped

        value, skipped = asyncio.run(_run())
        self.assertEqual(value, "agent")
        self.assertFalse(skipped)

    def test_escape_skips(self):
        import asyncio

        from openshard.cli.ui.onboarding import _build_select_app_class
        from openshard.onboarding.choices import USER_TYPE_CHOICES

        async def _run():
            Sel = _build_select_app_class()
            app = Sel("Who is using OpenShard?", USER_TYPE_CHOICES)
            async with app.run_test() as pilot:
                await pilot.press("escape")
            return app.selected_value, app.skipped

        value, skipped = asyncio.run(_run())
        self.assertIsNone(value)
        self.assertTrue(skipped)

    def test_info_screen_enter_continues(self):
        import asyncio

        from openshard.cli.ui.onboarding import _build_info_app_class

        async def _run(key):
            Info = _build_info_app_class()
            app = Info("body text", "footer")
            async with app.run_test() as pilot:
                await pilot.press(key)
            return app.skipped

        self.assertFalse(asyncio.run(_run("enter")))
        self.assertTrue(asyncio.run(_run("escape")))


class TestTuiOnboardingScreenPilot(unittest.TestCase):
    """TUI first-run behaviour: drive OnboardingScreen via pilot in a host app."""

    def _drive(self, presses: list[str]) -> dict:
        import asyncio

        import yaml
        from textual.app import App

        from openshard.tui.onboarding_screen import OnboardingScreen

        class _Host(App):
            def __init__(self) -> None:
                super().__init__()
                self.dismissed = False

            def on_mount(self) -> None:
                self.push_screen(OnboardingScreen(), lambda _r: setattr(self, "dismissed", True))

        async def _run(td: Path) -> dict:
            app = _Host()
            async with app.run_test() as pilot:
                for p in presses:
                    await pilot.press(p)
                await pilot.pause()
            cfg = td / ".openshard" / "config.yml"
            out: dict = {"exists": cfg.exists(), "dismissed": app.dismissed}
            if cfg.exists():
                out["onboarding"] = yaml.safe_load(cfg.read_text(encoding="utf-8")).get("onboarding")
            return out

        runner = CliRunner()
        with runner.isolated_filesystem() as fs:
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                return asyncio.run(_run(Path(fs)))

    def test_full_completion_writes_all_fields(self):
        # 4 select screens (user_type, executor, route, safety) + 3 info screens
        out = self._drive(["enter"] * 7)
        self.assertTrue(out["exists"])
        self.assertTrue(out["dismissed"])
        ob = out["onboarding"]
        self.assertFalse(ob["skipped"])
        self.assertEqual(ob["user_type"], "human")
        self.assertEqual(ob["executor"], "native")
        self.assertEqual(ob["provider_route"], "openrouter")
        self.assertEqual(ob["provider"], "openrouter")
        self.assertEqual(ob["safety_profile"], "recommended")

    def test_escape_skips_and_writes_marker(self):
        out = self._drive(["escape"])
        self.assertTrue(out["exists"])
        self.assertTrue(out["dismissed"])
        self.assertTrue(out["onboarding"]["skipped"])
        self.assertIn("completed_at", out["onboarding"])

    def test_direct_route_injects_provider_screen(self):
        # user_type(enter) + executor(enter) + route: down->direct(enter)
        # + direct provider(enter=anthropic) + key info(enter) + safety(enter)
        # + local-first(enter) + finish(enter)
        out = self._drive(["enter", "enter", "down", "enter", "enter", "enter", "enter", "enter", "enter"])
        self.assertTrue(out["exists"])
        ob = out["onboarding"]
        self.assertEqual(ob["provider_route"], "direct")
        self.assertEqual(ob["provider"], "anthropic")

    def test_no_api_keys_in_written_config(self):
        env = {**_NO_AGENT_ENV, "ANTHROPIC_API_KEY": "sk-secret-xyz"}
        import asyncio

        from textual.app import App

        from openshard.tui.onboarding_screen import OnboardingScreen

        class _Host(App):
            def on_mount(self) -> None:
                self.push_screen(OnboardingScreen())

        async def _run(td: Path) -> str:
            app = _Host()
            async with app.run_test() as pilot:
                await pilot.press("escape")
                await pilot.pause()
            return (td / ".openshard" / "config.yml").read_text(encoding="utf-8")

        runner = CliRunner()
        with runner.isolated_filesystem() as fs:
            with patch.dict(os.environ, env, clear=False):
                raw = asyncio.run(_run(Path(fs)))
        self.assertNotIn("API_KEY", raw)
        self.assertNotIn("sk-secret-xyz", raw)


class TestTuiAppFirstRunGate(unittest.TestCase):
    """OpenShardTui.on_mount pushes OnboardingScreen only when the gate allows."""

    def _push_happened(self, gate_value: bool) -> bool:
        import asyncio

        from openshard.tui.app import OpenShardTui
        from openshard.tui.onboarding_screen import OnboardingScreen

        async def _run() -> bool:
            app = OpenShardTui()
            with patch("openshard.cli.ui.onboarding._should_run_onboarding", return_value=gate_value):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    return any(isinstance(s, OnboardingScreen) for s in app.screen_stack)

        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, _NO_AGENT_ENV, clear=False):
                return asyncio.run(_run())

    def test_pushes_screen_when_gate_true(self):
        self.assertTrue(self._push_happened(True))

    def test_no_screen_when_gate_false(self):
        self.assertFalse(self._push_happened(False))


class TestOnboardingWordingAndSummary(unittest.TestCase):
    """Executor question wording + finish-summary rendering (incl. legacy values)."""

    def test_executor_question_reworded(self):
        from openshard.tui.onboarding_screen import _SELECT_SCREENS
        questions = [q for _f, q, _c in _SELECT_SCREENS]
        self.assertIn("How do you want to use OpenShard?", questions)
        self.assertNotIn("Which executor should OpenShard use?", questions)

    def test_finish_summary_new_executor_label(self):
        from openshard.cli.ui.onboarding import _finish_summary_body
        body = _finish_summary_body({"executor": "cli_agent"})
        self.assertIn("Connect an installed CLI agent", body)

    def test_finish_summary_renders_legacy_executor(self):
        from openshard.cli.ui.onboarding import _finish_summary_body
        # A config carrying a legacy value still renders a friendly label, no crash.
        body = _finish_summary_body({"executor": "goose"})
        self.assertIn("Goose", body)

    def test_finish_summary_native_recommended_wording(self):
        from openshard.cli.ui.onboarding import _finish_summary_body
        body = _finish_summary_body({"executor": "native"})
        self.assertIn("OpenShard Native", body)
        self.assertNotIn("local runner", body.lower())


class TestIsFirstRunGuards(unittest.TestCase):
    """is_first_run() must not treat bundled defaults / env keys as completed."""

    def test_no_user_config_is_first_run(self):
        from openshard.config import onboarding as ob
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, {**_NO_AGENT_ENV, "OPENSHARD_CONFIG": ""}, clear=False):
                self.assertTrue(ob.is_first_run())

    def test_openrouter_key_does_not_complete_onboarding(self):
        """Detecting OPENROUTER_API_KEY must not falsely mark onboarding complete."""
        from openshard.config import onboarding as ob
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, {**_NO_AGENT_ENV, "OPENSHARD_CONFIG": "",
                                          "OPENROUTER_API_KEY": "sk-test"}, clear=False):
                self.assertTrue(ob.is_first_run())

    def test_default_config_with_marker_ignored_without_user_file(self):
        """Even if load_config synthesised a completed onboarding block, the absence
        of a real on-disk user config means it is still a first run."""
        from openshard.config import onboarding as ob
        runner = CliRunner()
        completed = ({"onboarding": {"completed_at": "2026-01-01T00:00:00+00:00"}}, True, None)
        with runner.isolated_filesystem():
            with patch.dict(os.environ, {**_NO_AGENT_ENV, "OPENSHARD_CONFIG": ""}, clear=False):
                # find_config_path() returns None (no user file) -> first run wins
                # before load_config_safe is even consulted.
                with patch("openshard.config.settings.load_config_safe", return_value=completed):
                    self.assertTrue(ob.is_first_run())


class TestFreshNewUserScenario(unittest.TestCase):
    """Regression for the manual fake-new-user report: fresh isolated home +
    fresh repo + no onboarding marker."""

    def _fake_home_env(self, tmp: Path) -> dict:
        home = tmp / "home"
        (home / "AppData" / "Roaming").mkdir(parents=True, exist_ok=True)
        (home / "AppData" / "Local").mkdir(parents=True, exist_ok=True)
        return {
            **_NO_AGENT_ENV,
            "OPENSHARD_CONFIG": "",
            "USERPROFILE": str(home),
            "HOME": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
        }

    def test_human_tty_launches_onboarding(self):
        """Fresh human + TTY in a fresh repo => onboarding gate opens."""
        import io

        from openshard.cli.ui.onboarding import _should_run_onboarding

        class _FakeTTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        runner = CliRunner()
        with runner.isolated_filesystem() as fs:
            Path(".git").mkdir()
            with patch.dict(os.environ, self._fake_home_env(Path(fs)), clear=False):
                with patch("sys.stdin", _FakeTTY()), patch("sys.stdout", _FakeTTY()):
                    self.assertTrue(_should_run_onboarding())

    def test_agent_json_non_interactive_and_not_completed(self):
        """The agent JSON path stays non-interactive and reports not-completed."""
        import json

        runner = CliRunner()
        with runner.isolated_filesystem() as fs:
            Path(".git").mkdir()
            with patch.dict(os.environ, self._fake_home_env(Path(fs)), clear=False):
                with patch("openshard.cli.ui.onboarding.run_onboarding_flow") as flow:
                    result = runner.invoke(cli, ["setup", "--agent", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertFalse(data["interactive"])
        self.assertFalse(data["config_found"])
        self.assertFalse(data["onboarding_completed"])
        flow.assert_not_called()

    def test_completed_user_not_nagged(self):
        """An existing user with completed onboarding is not sent through it again."""
        import yaml

        from openshard.config import onboarding as ob
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".openshard").mkdir()
            Path(".openshard/config.yml").write_text(
                yaml.safe_dump({"onboarding": {"completed_at": "2026-06-13T10:00:00+00:00"}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {**_NO_AGENT_ENV, "OPENSHARD_CONFIG": ""}, clear=False):
                self.assertFalse(ob.is_first_run())


if __name__ == "__main__":
    unittest.main()
