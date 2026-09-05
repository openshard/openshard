"""Tests for PR8 zero-friction onboarding: `openshard setup`, the Claude Code
section of `openshard doctor`, and the openshard.adapters.claude_setup module
that orchestrates the existing MCP/hooks/statusline installers for both.

All `claude` CLI invocations are mocked via subprocess.run/shutil.which; no
test ever shells out to a real `claude` process or touches this machine's
real Claude Code configuration.

`Path.exists` is patched on the claude_mcp_install module (not just
`find_repo_root`) to force "no git repository anywhere" deterministically --
patching the class attribute this way affects every caller of `find_repo_root`
regardless of which module imported it, and sidesteps the fact that a dev
machine's own home directory can itself be a git repository (real ancestor
directories vary by machine).
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from openshard.adapters.claude_hooks_install import HOOK_EVENTS, SETTINGS_RELPATH, STATUS_COMMAND
from openshard.adapters.claude_setup import (
    HISTORY_RELPATH,
    detect_claude_integration,
    history_writable,
    run_setup,
)
from openshard.cli.main import cli

_MCP_MODULE = "openshard.adapters.claude_mcp_install"
_SETUP_MODULE = "openshard.adapters.claude_setup"

_NO_KEYS = {"OPENROUTER_API_KEY": "", "ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "",
            "OPENSHARD_CONFIG": ""}


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _which(name: str) -> str | None:
    return {"claude": "/usr/local/bin/claude", "openshard": "/usr/local/bin/openshard"}.get(name)


_NOT_FOUND_OUTPUT = 'No MCP server named "openshard". Configured servers: codegraph'


def _get_output(repo_path: str) -> str:
    return (
        "openshard:\n"
        "  Scope: Local config (private to you in this project)\n"
        "  Status: ✔ Connected\n"
        "  Type: stdio\n"
        "  Command: openshard\n"
        f"  Args: mcp serve --repo-path {repo_path}\n"
    )


def _subprocess_router(existing_repo_path: str | None = None):
    """A single router for every subprocess.run call a test may trigger.

    `claude_mcp_install` and `claude_hooks_install` both do a plain `import
    subprocess`, so they share the exact same module object -- patching
    `<either module>.subprocess.run` patches it globally for the duration of
    the `with` block, for *every* caller, not just the module named in the
    patch target. So a single test must route both `claude mcp ...` calls
    and the hook installer's `git check-ignore` / `git rev-parse` calls
    through one mock, or whichever patch was entered last wins for both.
    """
    def _run(argv, **kwargs):
        if "check-ignore" in argv:
            return _completed(returncode=0)
        if "rev-parse" in argv:
            return _completed(returncode=0, stdout=".git/info/exclude\n")
        if "get" in argv:
            if existing_repo_path is None:
                return _completed(returncode=1, stdout=_NOT_FOUND_OUTPUT)
            return _completed(returncode=0, stdout=_get_output(existing_repo_path))
        if "remove" in argv:
            return _completed(returncode=0, stdout="Removed")
        if "add" in argv:
            return _completed(returncode=0, stdout="Added stdio MCP server openshard")
        raise AssertionError(f"unexpected subprocess.run call: {argv}")
    return _run


def _git_ignored_router():
    return _subprocess_router()


def _no_repo_anywhere():
    """Context manager forcing find_repo_root() to find no .git anywhere."""
    return patch(f"{_MCP_MODULE}.Path.exists", return_value=False)


class _RepoCase(unittest.TestCase):
    """Base: a fresh isolated temp dir with a `.git`, `claude`/`openshard` resolvable."""

    def setUp(self):
        self._fs_ctx = CliRunner().isolated_filesystem()
        self._tmp = self._fs_ctx.__enter__()
        self.addCleanup(self._fs_ctx.__exit__, None, None, None)
        self.root = Path(self._tmp)
        (self.root / ".git").mkdir()

        self._git_patch = patch(
            "openshard.adapters.claude_hooks_install.subprocess.run",
            side_effect=_git_ignored_router(),
        )
        self._git_patch.start()
        self.addCleanup(self._git_patch.stop)


# ---------------------------------------------------------------------------
# detect_claude_integration -- read-only
# ---------------------------------------------------------------------------

class TestDetectClaudeIntegration(_RepoCase):
    def test_no_repo_root_is_all_unconfigured(self):
        status = detect_claude_integration(None)
        self.assertFalse(status.mcp_configured)
        self.assertEqual(status.hook_events_missing, list(HOOK_EVENTS))
        self.assertEqual(status.statusline_state, "absent")

    def test_fresh_repo_nothing_configured(self):
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            status = detect_claude_integration(self.root)
        self.assertTrue(status.claude_cli.available)
        self.assertFalse(status.mcp_configured)
        self.assertEqual(status.hook_events_installed, [])
        self.assertEqual(status.statusline_state, "absent")

    def test_never_writes_anything(self):
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()) as mock_run:
            detect_claude_integration(self.root)
        self.assertFalse((self.root / SETTINGS_RELPATH).exists())
        add_calls = [c for c in mock_run.call_args_list if "add" in c.args[0]]
        remove_calls = [c for c in mock_run.call_args_list if "remove" in c.args[0]]
        self.assertEqual(add_calls, [])
        self.assertEqual(remove_calls, [])

    def test_fully_configured_repo_detected(self):
        resolved = str(self.root.resolve())
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router(resolved)):
            run_setup(repo_path=self.root)
            status = detect_claude_integration(self.root)
        self.assertTrue(status.mcp_configured)
        self.assertEqual(status.hook_events_missing, [])
        self.assertEqual(status.statusline_state, "openshard")

    def test_custom_statusline_reported_as_custom(self):
        (self.root / ".claude").mkdir()
        (self.root / SETTINGS_RELPATH).write_text(
            json.dumps({"statusLine": {"type": "command", "command": "~/my-statusline.sh"}}),
            encoding="utf-8",
        )
        status = detect_claude_integration(self.root)
        self.assertEqual(status.statusline_state, "custom")

    def test_unparseable_settings_reported_as_error_not_crash(self):
        (self.root / ".claude").mkdir()
        (self.root / SETTINGS_RELPATH).write_text("{ broken", encoding="utf-8")
        status = detect_claude_integration(self.root)
        self.assertIsNotNone(status.hooks_settings_error)


# ---------------------------------------------------------------------------
# run_setup -- orchestration / writes
# ---------------------------------------------------------------------------

class TestRunSetup(_RepoCase):
    def test_not_a_git_repo(self):
        with _no_repo_anywhere():
            result = run_setup(repo_path=self.root)
        self.assertFalse(result.is_git)
        self.assertEqual(result.readiness, "not_ready")
        self.assertTrue(any("git repository" in s for s in result.next_steps))
        self.assertIsNone(result.mcp)

    def test_claude_cli_missing(self):
        with patch(f"{_MCP_MODULE}.shutil.which", return_value=None):
            result = run_setup(repo_path=self.root)
        self.assertTrue(result.is_git)
        self.assertFalse(result.claude_cli.available)
        self.assertEqual(result.readiness, "not_ready")
        self.assertTrue(any("Claude Code CLI" in s for s in result.next_steps))
        self.assertIsNone(result.mcp)

    def test_fresh_install_is_fully_ready(self):
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            result = run_setup(repo_path=self.root)
        self.assertEqual(result.readiness, "ready")
        self.assertEqual(result.mcp.status, "installed")
        self.assertEqual(result.hooks.status, "installed")
        self.assertEqual(result.statusline.status, "installed")
        self.assertEqual(result.next_steps, [])
        data = json.loads((self.root / SETTINGS_RELPATH).read_text(encoding="utf-8"))
        self.assertEqual(set(data["hooks"]), set(HOOK_EVENTS))
        self.assertEqual(data["statusLine"], {"type": "command", "command": STATUS_COMMAND})

    def test_idempotent_second_run_reports_already_installed_and_rewrites_nothing(self):
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            run_setup(repo_path=self.root)
            before = (self.root / SETTINGS_RELPATH).read_text(encoding="utf-8")
            resolved = str(self.root.resolve())
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router(resolved)) as mock_run2:
            result = run_setup(repo_path=self.root)
        after = (self.root / SETTINGS_RELPATH).read_text(encoding="utf-8")
        self.assertEqual(result.readiness, "ready")
        self.assertEqual(result.mcp.status, "already_installed")
        self.assertEqual(result.hooks.status, "already_installed")
        self.assertEqual(result.statusline.status, "already_installed")
        self.assertEqual(before, after)
        add_calls = [c for c in mock_run2.call_args_list if "add" in c.args[0]]
        self.assertEqual(add_calls, [])

    def test_custom_statusline_yields_ready_partial_with_actionable_step(self):
        (self.root / ".claude").mkdir()
        (self.root / SETTINGS_RELPATH).write_text(
            json.dumps({"statusLine": {"type": "command", "command": "~/my-statusline.sh"}}),
            encoding="utf-8",
        )
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            result = run_setup(repo_path=self.root)
        self.assertEqual(result.readiness, "ready_partial")
        self.assertEqual(result.statusline.status, "skipped_existing")
        self.assertTrue(any("statusLine" in s for s in result.next_steps))
        # The custom status line itself must survive untouched.
        data = json.loads((self.root / SETTINGS_RELPATH).read_text(encoding="utf-8"))
        self.assertEqual(data["statusLine"]["command"], "~/my-statusline.sh")

    def test_mcp_add_failure_is_not_ready_and_does_not_write_hooks(self):
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run",
                   side_effect=lambda argv, **kw: (
                       _completed(returncode=1, stdout=_NOT_FOUND_OUTPUT) if "get" in argv
                       else _completed(returncode=1, stderr="boom")
                   )):
            result = run_setup(repo_path=self.root)
        self.assertEqual(result.readiness, "not_ready")
        self.assertEqual(result.mcp.status, "error")
        self.assertIsNone(result.hooks)
        self.assertFalse((self.root / SETTINGS_RELPATH).exists())

    def test_history_path_and_writability(self):
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            result = run_setup(repo_path=self.root)
        # run_setup resolves repo_path (via find_repo_root -> Path.resolve()),
        # so compare against the resolved form too -- on Windows the raw temp
        # path and its resolved form can differ (short vs. long name).
        self.assertEqual(result.history_path, self.root.resolve() / HISTORY_RELPATH)
        self.assertTrue(result.history_writable)

    def test_repo_path_with_spaces(self):
        spacey = self.root / "spacey repo"
        spacey.mkdir()
        (spacey / ".git").mkdir()
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            result = run_setup(repo_path=spacey)
        self.assertEqual(result.readiness, "ready")


class TestHistoryWritable(unittest.TestCase):
    def test_existing_base_dir_is_writable(self):
        with CliRunner().isolated_filesystem() as tmp:
            self.assertTrue(history_writable(Path(tmp)))

    def test_never_creates_the_directory(self):
        with CliRunner().isolated_filesystem() as tmp:
            history_writable(Path(tmp))
            self.assertFalse((Path(tmp) / ".openshard").exists())


# ---------------------------------------------------------------------------
# CLI: `openshard setup`
# ---------------------------------------------------------------------------

class TestSetupCli(_RepoCase):
    def test_fresh_setup_human_output(self):
        runner = CliRunner()
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            result = runner.invoke(cli, ["setup", "--repo-path", str(self.root), "--yes"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("OpenShard is ready. Use Claude Code normally.", result.output)
        self.assertIn("openshard last", result.output)

    def test_setup_json_action_result(self):
        runner = CliRunner()
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            result = runner.invoke(cli, ["setup", "--json", "--yes", "--repo-path", str(self.root)])
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(data["readiness"], "ready")
        self.assertTrue(data["ready"])
        self.assertEqual(data["mcp"]["status"], "installed")

    def test_setup_agent_flag_is_read_only(self):
        runner = CliRunner()
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()) as mock_run:
            result = runner.invoke(cli, ["setup", "--agent", "--json", "--repo-path", str(self.root)])
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertIn("claude_code", data)
        self.assertFalse(data["claude_code"]["mcp_configured"])
        # --agent must never install/write anything.
        add_calls = [c for c in mock_run.call_args_list if "add" in c.args[0]]
        self.assertEqual(add_calls, [])
        self.assertFalse((self.root / SETTINGS_RELPATH).exists())

    def test_setup_idempotent_cli_second_run(self):
        runner = CliRunner()
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            runner.invoke(cli, ["setup", "--yes", "--repo-path", str(self.root)])
            before = (self.root / SETTINGS_RELPATH).read_text(encoding="utf-8")
            resolved = str(self.root.resolve())
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router(resolved)):
            result = runner.invoke(cli, ["setup", "--yes", "--repo-path", str(self.root)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("already configured", result.output)
        self.assertEqual((self.root / SETTINGS_RELPATH).read_text(encoding="utf-8"), before)

    def test_setup_preserves_existing_custom_claude_settings(self):
        (self.root / ".claude").mkdir()
        existing = {"permissions": {"allow": ["Bash(ls)"]}, "model": "opus"}
        (self.root / SETTINGS_RELPATH).write_text(json.dumps(existing), encoding="utf-8")
        runner = CliRunner()
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            result = runner.invoke(cli, ["setup", "--yes", "--repo-path", str(self.root)])
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads((self.root / SETTINGS_RELPATH).read_text(encoding="utf-8"))
        self.assertEqual(data["permissions"], existing["permissions"])
        self.assertEqual(data["model"], "opus")

    def test_setup_preserves_custom_statusline_and_reports_limitation(self):
        (self.root / ".claude").mkdir()
        (self.root / SETTINGS_RELPATH).write_text(
            json.dumps({"statusLine": {"type": "command", "command": "~/my-statusline.sh"}}),
            encoding="utf-8",
        )
        runner = CliRunner()
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            result = runner.invoke(cli, ["setup", "--yes", "--repo-path", str(self.root)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("limitation", result.output)
        data = json.loads((self.root / SETTINGS_RELPATH).read_text(encoding="utf-8"))
        self.assertEqual(data["statusLine"]["command"], "~/my-statusline.sh")

    def test_setup_non_git_directory_exits_nonzero_with_actionable_message(self):
        runner = CliRunner()
        with _no_repo_anywhere():
            result = runner.invoke(cli, ["setup", "--yes"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("git repository", result.output.lower())
        self.assertNotIn("Traceback", result.output)

    def test_setup_claude_cli_missing_exits_nonzero_with_actionable_message(self):
        runner = CliRunner()
        with patch(f"{_MCP_MODULE}.shutil.which", return_value=None):
            result = runner.invoke(cli, ["setup", "--yes", "--repo-path", str(self.root)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Claude Code CLI", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_yes_flag_skips_interactive_wizard_even_if_it_would_otherwise_run(self):
        runner = CliRunner()
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()), \
             patch("openshard.cli.ui.onboarding._should_run_onboarding", return_value=True), \
             patch("openshard.cli.ui.onboarding.run_onboarding_flow") as flow:
            runner.invoke(cli, ["setup", "--yes", "--repo-path", str(self.root)])
        flow.assert_not_called()

    def test_wizard_runs_when_should_run_onboarding_and_not_yes(self):
        runner = CliRunner()
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()), \
             patch("openshard.cli.ui.onboarding._should_run_onboarding", return_value=True), \
             patch("openshard.cli.ui.onboarding.run_onboarding_flow") as flow:
            runner.invoke(cli, ["setup", "--repo-path", str(self.root)])
        flow.assert_called_once()

    def test_repo_path_with_spaces_via_cli(self):
        spacey = self.root / "spacey repo"
        spacey.mkdir()
        (spacey / ".git").mkdir()
        runner = CliRunner()
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            result = runner.invoke(cli, ["setup", "--yes", "--repo-path", str(spacey)])
        self.assertEqual(result.exit_code, 0, result.output)


# ---------------------------------------------------------------------------
# CLI: `openshard doctor` Claude Code section
# ---------------------------------------------------------------------------

class TestDoctorClaudeSection(_RepoCase):
    def test_doctor_reports_fully_ready(self):
        runner = CliRunner()
        with patch.dict(os.environ, _NO_KEYS, clear=False), \
             patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            run_setup(repo_path=self.root)
            resolved = str(self.root.resolve())
        with patch.dict(os.environ, _NO_KEYS, clear=False), \
             patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router(resolved)):
            result = runner.invoke(cli, ["doctor", "--repo-path", str(self.root)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Ready -- use Claude Code normally.", result.output)
        self.assertIn("Claude Code", result.output)

    def test_doctor_json_includes_claude_code_block(self):
        runner = CliRunner()
        with patch.dict(os.environ, _NO_KEYS, clear=False), \
             patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            result = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(self.root)])
        data = json.loads(result.output)
        self.assertIn("claude_code", data)
        self.assertIn("mcp_configured", data["claude_code"])
        self.assertIn("history_writable", data["claude_code"])

    def test_doctor_broken_state_shows_specific_problem(self):
        runner = CliRunner()
        with patch.dict(os.environ, _NO_KEYS, clear=False), \
             patch(f"{_MCP_MODULE}.shutil.which", return_value=None):
            result = runner.invoke(cli, ["doctor", "--repo-path", str(self.root)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("CLI not found on PATH", result.output)
        self.assertIn("Not ready", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_doctor_partial_state_custom_statusline(self):
        (self.root / ".claude").mkdir()
        (self.root / SETTINGS_RELPATH).write_text(
            json.dumps({"statusLine": {"type": "command", "command": "~/my-statusline.sh"}}),
            encoding="utf-8",
        )
        runner = CliRunner()
        # MCP + hooks must actually be configured for this to be "ready, but
        # limited" rather than "not ready" -- run_setup first, same as a real
        # user would via `openshard setup`, which leaves the custom status
        # line alone (skipped_existing) while still wiring up capture.
        with patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router()):
            run_setup(repo_path=self.root)
            resolved = str(self.root.resolve())
        with patch.dict(os.environ, _NO_KEYS, clear=False), \
             patch(f"{_MCP_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MCP_MODULE}.subprocess.run", side_effect=_subprocess_router(resolved)):
            result = runner.invoke(cli, ["doctor", "--repo-path", str(self.root)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("custom status line present", result.output)
        self.assertIn("limited receipts", result.output)

    def test_doctor_non_git_directory(self):
        runner = CliRunner()
        with patch.dict(os.environ, _NO_KEYS, clear=False), _no_repo_anywhere():
            result = runner.invoke(cli, ["doctor"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("not a git repository", result.output)
        self.assertNotIn("Traceback", result.output)


if __name__ == "__main__":
    unittest.main()
