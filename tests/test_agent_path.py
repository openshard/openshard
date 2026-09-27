"""Tests for agent/CI environment auto-detection."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import click.testing
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.config.settings import is_agent_environment, load_config

# ---------------------------------------------------------------------------
# is_agent_environment
# ---------------------------------------------------------------------------

_AGENT_VARS = ("OPENSHARD_AGENT", "CI", "GITHUB_ACTIONS", "GITLAB_CI", "NO_COLOR")


def _clean_env() -> dict[str, str]:
    """Return os.environ minus all agent/CI detection vars."""
    return {k: v for k, v in os.environ.items() if k not in _AGENT_VARS}


class TestIsAgentEnvironment(unittest.TestCase):

    def test_is_agent_environment_false_by_default(self):
        with patch.dict(os.environ, _clean_env(), clear=True):
            self.assertFalse(is_agent_environment())

    def test_is_agent_environment_true_for_openshard_agent(self):
        env = {**_clean_env(), "OPENSHARD_AGENT": "1"}
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(is_agent_environment())

    def test_is_agent_environment_true_for_ci(self):
        env = {**_clean_env(), "CI": "true"}
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(is_agent_environment())

    def test_is_agent_environment_true_for_github_actions(self):
        env = {**_clean_env(), "GITHUB_ACTIONS": "true"}
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(is_agent_environment())

    def test_is_agent_environment_false_for_no_color(self):
        """NO_COLOR is a colour preference (no-color.org), never an agent signal."""
        env = {**_clean_env(), "NO_COLOR": "1"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(is_agent_environment())

    def test_is_agent_environment_never_raises(self):
        """Must not raise under any circumstances."""
        with patch.dict(os.environ, _clean_env(), clear=True):
            try:
                result = is_agent_environment()
                self.assertIsInstance(result, bool)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"is_agent_environment() raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# load_config agent-mode injection
# ---------------------------------------------------------------------------

class TestLoadConfigAgentMode(unittest.TestCase):

    def test_load_config_sets_agent_json_when_ci(self):
        """With CI=true and no config file, output_mode should be agent_json."""
        env = {**_clean_env(), "CI": "true"}
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, env, clear=True):
                cfg = load_config()
        self.assertEqual(cfg.get("output_mode"), "agent_json")

    def test_load_config_does_not_override_explicit_config(self):
        """If a config file sets output_mode, the env var must not override it."""
        import yaml

        env = {**_clean_env(), "CI": "true"}
        runner = CliRunner()
        with runner.isolated_filesystem():
            # Write a config file that explicitly sets output_mode to "human".
            cfg_dir = Path(".openshard")
            cfg_dir.mkdir()
            cfg_path = cfg_dir / "config.yml"
            cfg_path.write_text(
                yaml.safe_dump({"output_mode": "human", "model_tiers": []}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, env, clear=True):
                cfg = load_config()
        # The file-based config must win; agent injection only happens in default path.
        self.assertEqual(cfg.get("output_mode"), "human")


# ---------------------------------------------------------------------------
# openshard env command
# ---------------------------------------------------------------------------

class TestEnvCommand(unittest.TestCase):

    def _run(self, extra_env: dict[str, str] | None = None) -> click.testing.Result:
        env = _clean_env()
        if extra_env:
            env.update(extra_env)
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch.dict(os.environ, env, clear=True):
                return runner.invoke(cli, ["env"])

    def test_openshard_env_exits_zero(self):
        result = self._run()
        self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_openshard_env_shows_agent_yes_when_ci(self):
        result = self._run(extra_env={"CI": "true"})
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("yes", result.output)

    def test_openshard_env_shows_agent_no_by_default(self):
        result = self._run()
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("no", result.output)

    def test_openshard_env_does_not_print_api_key_value(self):
        sentinel = "sk-test-sentinel-value-1234567890"
        result = self._run(extra_env={"OPENROUTER_API_KEY": sentinel})
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertNotIn(sentinel, result.output)


if __name__ == "__main__":
    unittest.main()
