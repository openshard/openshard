from __future__ import annotations

import unittest

from click.testing import CliRunner

from openshard.cli.main import cli


class TestCliPackage(unittest.TestCase):

    def test_cli_importable(self):
        self.assertIsNotNone(cli)

    def test_version_exits_zero(self):
        result = CliRunner().invoke(cli, ["--version"])
        self.assertEqual(result.exit_code, 0)

    def test_version_contains_version_string(self):
        import openshard
        result = CliRunner().invoke(cli, ["--version"])
        self.assertIn(openshard.__version__, result.output)

    def test_no_args_renders_home_exits_zero(self):
        result = CliRunner().invoke(cli, [])
        self.assertEqual(result.exit_code, 0)

    def test_package_version_importable(self):
        import openshard
        self.assertRegex(openshard.__version__, r"^\d+\.\d+")

    def test_run_help_exits_zero(self):
        result = CliRunner().invoke(cli, ["run", "--help"])
        self.assertEqual(result.exit_code, 0)

    def test_reflect_last_help_exits_zero(self):
        result = CliRunner().invoke(cli, ["reflect", "last", "--help"])
        self.assertEqual(result.exit_code, 0)

    def test_pr_comment_help_exits_zero(self):
        result = CliRunner().invoke(cli, ["pr", "comment", "--help"])
        self.assertEqual(result.exit_code, 0)

    def test_init_help_exits_zero(self):
        result = CliRunner().invoke(cli, ["init", "--help"])
        self.assertEqual(result.exit_code, 0)

    def test_doctor_help_exits_zero(self):
        result = CliRunner().invoke(cli, ["doctor", "--help"])
        self.assertEqual(result.exit_code, 0)

    def test_config_show_help_exits_zero(self):
        result = CliRunner().invoke(cli, ["config", "show", "--help"])
        self.assertEqual(result.exit_code, 0)

    def test_run_dry_run_no_config_crash(self):
        """Config loading must not crash; any failure should be API-related, not config-related."""
        runner = CliRunner()
        with runner.isolated_filesystem():  # a run records history in the cwd: never this checkout's
            result = runner.invoke(cli, ["run", "test task", "--dry-run"])
        output = result.output or ""
        self.assertNotIn("Config file not found", output)
        self.assertNotIn("dist-packages/config.yml", output)
