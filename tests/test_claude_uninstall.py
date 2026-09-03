"""Tests for PR8 reversibility: uninstall_claude_mcp/hooks/statusline and the
`openshard mcp uninstall claude` CLI command.

Same mocking conventions as test_mcp_install.py / test_claude_setup.py: all
`claude` CLI calls are mocked, nothing is written outside a throwaway
temporary repository, and `.openshard/` history is asserted to survive.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from openshard.adapters.claude_hooks_install import (
    HOOK_EVENTS,
    SETTINGS_RELPATH,
    install_claude_hooks,
    install_claude_statusline,
    remove_openshard_hooks,
    uninstall_claude_hooks,
    uninstall_claude_statusline,
)
from openshard.adapters.claude_mcp_install import uninstall_claude_mcp
from openshard.cli.main import cli

_MODULE = "openshard.adapters.claude_mcp_install"
_HOOKS_MODULE = "openshard.adapters.claude_hooks_install"


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _which(name: str) -> str | None:
    return {"claude": "/usr/local/bin/claude", "openshard": "/usr/local/bin/openshard"}.get(name)


_NOT_FOUND_OUTPUT = 'No MCP server named "openshard". Configured servers: codegraph'


def _get_output(repo_path: str) -> str:
    return (
        "openshard:\n"
        "  Scope: Local config (private to you in this project)\n"
        "  Command: openshard\n"
        f"  Args: mcp serve --repo-path {repo_path}\n"
    )


def _subprocess_router(existing_repo_path: str | None, remove_result=None):
    """A single router for every subprocess.run call a test may trigger.

    `claude_mcp_install` and `claude_hooks_install` both do a plain `import
    subprocess`, so they share the exact same module object -- patching
    `<either module>.subprocess.run` patches it globally for the duration of
    the `with` block, for *every* caller. A CLI-level test that exercises
    both mcp and hooks install/uninstall must route both `claude mcp ...`
    calls and the hook installer's `git check-ignore` / `git rev-parse`
    calls through this one mock.
    """
    remove_result = remove_result if remove_result is not None else _completed(returncode=0, stdout="Removed")

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
            return remove_result
        if "add" in argv:
            return _completed(returncode=0, stdout="Added stdio MCP server openshard")
        raise AssertionError(f"unexpected subprocess.run call: {argv}")
    return _run


def _git_ignored_router():
    return _subprocess_router(None)


class _RepoCase(unittest.TestCase):
    def setUp(self):
        self._fs_ctx = CliRunner().isolated_filesystem()
        self._tmp = self._fs_ctx.__enter__()
        self.addCleanup(self._fs_ctx.__exit__, None, None, None)
        self.root = Path(self._tmp)
        (self.root / ".git").mkdir()
        self._git_patch = patch(f"{_HOOKS_MODULE}.subprocess.run", side_effect=_git_ignored_router())
        self._git_patch.start()
        self.addCleanup(self._git_patch.stop)


# ---------------------------------------------------------------------------
# remove_openshard_hooks -- pure function
# ---------------------------------------------------------------------------

class TestRemoveOpenshardHooks(unittest.TestCase):
    def test_no_hooks_key_is_all_absent(self):
        _, changes = remove_openshard_hooks({})
        self.assertTrue(all(v == "absent" for v in changes.values()))

    def test_removes_only_openshard_entries(self):
        settings = {
            "permissions": {"allow": ["Bash(pytest)"]},
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "notify-send done"}]},
                    {"hooks": [{"type": "command", "command": "openshard hooks claude", "timeout": 15}]},
                ],
            },
        }
        merged, changes = remove_openshard_hooks(settings)
        self.assertEqual(changes["Stop"], "removed")
        self.assertEqual(len(merged["hooks"]["Stop"]), 1)
        self.assertEqual(merged["hooks"]["Stop"][0]["hooks"][0]["command"], "notify-send done")
        self.assertEqual(merged["permissions"], settings["permissions"])

    def test_input_not_mutated(self):
        original = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "openshard hooks claude"}]}]}}
        snapshot = json.loads(json.dumps(original))
        remove_openshard_hooks(original)
        self.assertEqual(original, snapshot)

    def test_idempotent(self):
        settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "openshard hooks claude"}]}]}}
        once, _ = remove_openshard_hooks(settings)
        twice, changes = remove_openshard_hooks(once)
        self.assertEqual(once, twice)
        self.assertTrue(all(v == "absent" for v in changes.values()))


# ---------------------------------------------------------------------------
# uninstall_claude_mcp
# ---------------------------------------------------------------------------

class TestUninstallClaudeMcp(_RepoCase):
    def test_claude_missing_reports_not_installed(self):
        with patch(f"{_MODULE}.shutil.which", return_value=None):
            result = uninstall_claude_mcp(repo_path=self.root)
        self.assertEqual(result.status, "not_installed")

    def test_outside_repository_reports_not_installed(self):
        with patch(f"{_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MODULE}.find_repo_root", return_value=None):
            result = uninstall_claude_mcp(repo_path=self.root)
        self.assertEqual(result.status, "not_installed")

    def test_nothing_configured_reports_not_installed(self):
        with patch(f"{_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MODULE}.subprocess.run", side_effect=_subprocess_router(None)) as mock_run:
            result = uninstall_claude_mcp(repo_path=self.root)
        self.assertEqual(result.status, "not_installed")
        remove_calls = [c for c in mock_run.call_args_list if "remove" in c.args[0]]
        self.assertEqual(remove_calls, [])

    def test_configured_for_different_repo_is_left_alone(self):
        with patch(f"{_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MODULE}.subprocess.run", side_effect=_subprocess_router("/some/other/repo")) as mock_run:
            result = uninstall_claude_mcp(repo_path=self.root)
        self.assertEqual(result.status, "not_installed")
        remove_calls = [c for c in mock_run.call_args_list if "remove" in c.args[0]]
        self.assertEqual(remove_calls, [])

    def test_matching_entry_is_removed(self):
        resolved = str(self.root.resolve())
        with patch(f"{_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MODULE}.subprocess.run", side_effect=_subprocess_router(resolved)) as mock_run:
            result = uninstall_claude_mcp(repo_path=self.root)
        self.assertEqual(result.status, "removed")
        remove_calls = [c for c in mock_run.call_args_list if "remove" in c.args[0]]
        self.assertEqual(len(remove_calls), 1)
        self.assertIn("local", remove_calls[0].args[0])

    def test_removal_failure_reported_as_error(self):
        resolved = str(self.root.resolve())
        with patch(f"{_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MODULE}.subprocess.run",
                   side_effect=_subprocess_router(resolved, remove_result=_completed(returncode=1, stderr="boom"))):
            result = uninstall_claude_mcp(repo_path=self.root)
        self.assertEqual(result.status, "error")
        self.assertIn("boom", result.message)

    def test_second_uninstall_is_idempotent(self):
        resolved = str(self.root.resolve())
        with patch(f"{_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MODULE}.subprocess.run", side_effect=_subprocess_router(resolved)):
            uninstall_claude_mcp(repo_path=self.root)
        with patch(f"{_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MODULE}.subprocess.run", side_effect=_subprocess_router(None)):
            again = uninstall_claude_mcp(repo_path=self.root)
        self.assertEqual(again.status, "not_installed")


# ---------------------------------------------------------------------------
# uninstall_claude_hooks / uninstall_claude_statusline
# ---------------------------------------------------------------------------

class TestUninstallClaudeHooksAndStatusline(_RepoCase):
    def test_uninstall_removes_only_our_hooks(self):
        (self.root / ".claude").mkdir()
        existing = {
            "permissions": {"allow": ["Bash(ls)"]},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]},
        }
        (self.root / SETTINGS_RELPATH).write_text(json.dumps(existing), encoding="utf-8")
        install_claude_hooks(repo_root=self.root)

        result = uninstall_claude_hooks(repo_root=self.root)
        self.assertEqual(result.status, "removed")
        data = json.loads((self.root / SETTINGS_RELPATH).read_text(encoding="utf-8"))
        self.assertEqual(data["permissions"], existing["permissions"])
        self.assertEqual(data["hooks"]["Stop"], [{"hooks": [{"type": "command", "command": "say done"}]}])
        for event in HOOK_EVENTS:
            if event == "Stop":
                continue
            self.assertEqual(data["hooks"].get(event, []), [], event)

    def test_nothing_installed_reports_not_installed(self):
        result = uninstall_claude_hooks(repo_root=self.root)
        self.assertEqual(result.status, "not_installed")

    def test_idempotent_second_uninstall(self):
        install_claude_hooks(repo_root=self.root)
        uninstall_claude_hooks(repo_root=self.root)
        again = uninstall_claude_hooks(repo_root=self.root)
        self.assertEqual(again.status, "not_installed")

    def test_invalid_json_left_untouched(self):
        (self.root / ".claude").mkdir()
        (self.root / SETTINGS_RELPATH).write_text("{ nope", encoding="utf-8")
        result = uninstall_claude_hooks(repo_root=self.root)
        self.assertEqual(result.status, "error")
        self.assertEqual((self.root / SETTINGS_RELPATH).read_text(encoding="utf-8"), "{ nope")

    def test_uninstall_statusline_removes_only_ours(self):
        install_claude_statusline(repo_root=self.root)
        result = uninstall_claude_statusline(repo_root=self.root)
        self.assertEqual(result.status, "removed")
        data = json.loads((self.root / SETTINGS_RELPATH).read_text(encoding="utf-8"))
        self.assertNotIn("statusLine", data)

    def test_uninstall_statusline_never_touches_custom_one(self):
        (self.root / ".claude").mkdir()
        foreign = {"statusLine": {"type": "command", "command": "~/my-statusline.sh"}}
        (self.root / SETTINGS_RELPATH).write_text(json.dumps(foreign), encoding="utf-8")
        result = uninstall_claude_statusline(repo_root=self.root)
        self.assertEqual(result.status, "not_installed")
        data = json.loads((self.root / SETTINGS_RELPATH).read_text(encoding="utf-8"))
        self.assertEqual(data["statusLine"], foreign["statusLine"])


# ---------------------------------------------------------------------------
# CLI: `openshard mcp uninstall claude`
# ---------------------------------------------------------------------------

class TestMcpUninstallClaudeCli(_RepoCase):
    def test_full_uninstall_removes_everything_but_history(self):
        history_dir = self.root / ".openshard"
        history_dir.mkdir()
        (history_dir / "runs.jsonl").write_text('{"shard_id": "abc"}\n', encoding="utf-8")

        runner = CliRunner()
        resolved = str(self.root.resolve())
        with patch(f"{_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MODULE}.subprocess.run", side_effect=_subprocess_router(None)):
            runner.invoke(cli, ["mcp", "install", "claude", "--repo-path", str(self.root)])
        with patch(f"{_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MODULE}.subprocess.run", side_effect=_subprocess_router(resolved)):
            result = runner.invoke(cli, ["mcp", "uninstall", "claude", "--repo-path", str(self.root)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("removed", result.output.lower())
        self.assertEqual(
            (history_dir / "runs.jsonl").read_text(encoding="utf-8"), '{"shard_id": "abc"}\n',
        )
        data = json.loads((self.root / SETTINGS_RELPATH).read_text(encoding="utf-8"))
        self.assertNotIn("statusLine", data)
        for event in HOOK_EVENTS:
            self.assertEqual(data.get("hooks", {}).get(event, []), [], event)

    def test_uninstall_json_output(self):
        runner = CliRunner()
        with patch(f"{_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MODULE}.subprocess.run", side_effect=_subprocess_router(None)):
            result = runner.invoke(cli, ["mcp", "uninstall", "claude", "--json", "--repo-path", str(self.root)])
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(data["mcp"]["status"], "not_installed")

    def test_uninstall_never_touches_custom_statusline(self):
        (self.root / ".claude").mkdir()
        foreign = {"statusLine": {"type": "command", "command": "~/my-statusline.sh"}}
        (self.root / SETTINGS_RELPATH).write_text(json.dumps(foreign), encoding="utf-8")
        runner = CliRunner()
        with patch(f"{_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MODULE}.subprocess.run", side_effect=_subprocess_router(None)):
            result = runner.invoke(cli, ["mcp", "uninstall", "claude", "--repo-path", str(self.root)])
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads((self.root / SETTINGS_RELPATH).read_text(encoding="utf-8"))
        self.assertEqual(data["statusLine"], foreign["statusLine"])

    def test_uninstall_twice_is_idempotent(self):
        runner = CliRunner()
        with patch(f"{_MODULE}.shutil.which", side_effect=_which), \
             patch(f"{_MODULE}.subprocess.run", side_effect=_subprocess_router(None)):
            runner.invoke(cli, ["mcp", "uninstall", "claude", "--repo-path", str(self.root)])
            result = runner.invoke(cli, ["mcp", "uninstall", "claude", "--repo-path", str(self.root)])
        self.assertEqual(result.exit_code, 0, result.output)


if __name__ == "__main__":
    unittest.main()
