"""Tests for `openshard mcp install claude` (Demo v1 PR4).

Covers both the adapter (openshard.adapters.claude_mcp_install) directly and
the CLI wiring. All `claude` CLI invocations are mocked via
subprocess.run/shutil.which -- the real Claude Code configuration on the
machine running these tests is never touched.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from openshard.adapters.claude_mcp_install import (
    MCP_TOOLS,
    _extract_repo_path,
    _parse_get_output,
    _same_repo,
    build_server_argv,
    detect_claude_cli,
    detect_openshard_cli,
    find_repo_root,
    install_claude_mcp,
)
from openshard.cli.main import cli

_MODULE = "openshard.adapters.claude_mcp_install"


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# Claude Code always echoes paths with forward slashes, even on Windows,
# and never quotes a spacey value in this text view -- mirrors real
# `claude mcp get` output observed against the installed CLI.
_GET_OUTPUT_TEMPLATE = """openshard:
  Scope: Local config (private to you in this project)
  Status: ✔ Connected
  Type: stdio
  Command: openshard
  Args: mcp serve --repo-path {repo_path}
  Environment:

To remove this server, run: claude mcp remove openshard -s local
"""


def _posix_display(path: str) -> str:
    return path.replace("\\", "/")

_NOT_FOUND_OUTPUT = (
    'No MCP server named "openshard". Configured servers: codegraph'
)


def _make_get_result(repo_path: str | None) -> subprocess.CompletedProcess:
    if repo_path is None:
        return _completed(returncode=1, stdout=_NOT_FOUND_OUTPUT)
    return _completed(
        returncode=0, stdout=_GET_OUTPUT_TEMPLATE.format(repo_path=_posix_display(repo_path))
    )


def _subprocess_run_router(existing_repo_path: str | None, add_result=None, remove_result=None):
    """Build a subprocess.run side_effect that answers get/add/remove by argv shape."""
    add_result = add_result if add_result is not None else _completed(returncode=0, stdout="Added stdio MCP server openshard")
    remove_result = remove_result if remove_result is not None else _completed(returncode=0, stdout="Removed")

    def _run(argv, **kwargs):
        if "get" in argv:
            return _make_get_result(existing_repo_path)
        if "remove" in argv:
            return remove_result
        if "add" in argv:
            return add_result
        raise AssertionError(f"unexpected subprocess.run call: {argv}")

    return _run


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):
    def test_build_server_argv(self):
        argv = build_server_argv(Path("/repo"))
        self.assertEqual(argv, ["openshard", "mcp", "serve", "--repo-path", str(Path("/repo"))])

    def test_find_repo_root_finds_git_dir(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            sub = root / "a" / "b"
            sub.mkdir(parents=True)
            found = find_repo_root(sub)
            self.assertEqual(found, root.resolve())

    def test_find_repo_root_none_outside_repo(self):
        # Real ancestor directories vary by machine (a dev box's home directory
        # can itself be a git repo, e.g. a dotfiles checkout) -- fake "no .git
        # anywhere" deterministically rather than depending on the real tree.
        with CliRunner().isolated_filesystem() as tmp:
            with patch("openshard.adapters.claude_mcp_install.Path.exists", return_value=False):
                self.assertIsNone(find_repo_root(Path(tmp)))

    def test_extract_repo_path(self):
        self.assertEqual(_extract_repo_path("mcp serve --repo-path /x/y"), "/x/y")
        self.assertIsNone(_extract_repo_path("mcp serve"))
        self.assertIsNone(_extract_repo_path(None))

    def test_extract_repo_path_with_spaces(self):
        self.assertEqual(
            _extract_repo_path("mcp serve --repo-path /x/my repo"), "/x/my repo"
        )

    def test_same_repo_true_for_equivalent_paths(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = Path(tmp).resolve()
            self.assertTrue(_same_repo(str(root), root))

    def test_same_repo_false_for_different_paths(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = Path(tmp).resolve()
            self.assertFalse(_same_repo(str(root / "elsewhere"), root))

    def test_same_repo_false_for_none(self):
        self.assertFalse(_same_repo(None, Path.cwd()))

    def test_parse_get_output(self):
        entry = _parse_get_output(_GET_OUTPUT_TEMPLATE.format(repo_path="/some/repo"))
        self.assertEqual(entry.command, "openshard")
        self.assertIn("Local config", entry.scope or "")
        self.assertEqual(entry.args_raw, "mcp serve --repo-path /some/repo")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class TestDetection(unittest.TestCase):
    def test_detect_claude_cli_found(self):
        with patch(f"{_MODULE}.shutil.which", return_value="/usr/local/bin/claude"):
            avail = detect_claude_cli()
            self.assertTrue(avail.available)
            self.assertEqual(avail.path, "/usr/local/bin/claude")

    def test_detect_claude_cli_missing_has_guidance(self):
        with patch(f"{_MODULE}.shutil.which", return_value=None):
            avail = detect_claude_cli()
            self.assertFalse(avail.available)
            self.assertTrue(avail.install_guidance)

    def test_detect_openshard_cli_found(self):
        with patch(f"{_MODULE}.shutil.which", return_value="/usr/local/bin/openshard"):
            avail = detect_openshard_cli()
            self.assertTrue(avail.available)

    def test_detect_openshard_cli_missing(self):
        with patch(f"{_MODULE}.shutil.which", return_value=None):
            avail = detect_openshard_cli()
            self.assertFalse(avail.available)


# ---------------------------------------------------------------------------
# install_claude_mcp -- the core adapter function
# ---------------------------------------------------------------------------

class TestInstallClaudeMcp(unittest.TestCase):
    def _which(self, name):
        return {"claude": "/usr/local/bin/claude", "openshard": "/usr/local/bin/openshard"}.get(name)

    def test_claude_missing(self):
        with patch(f"{_MODULE}.shutil.which", return_value=None):
            result = install_claude_mcp(repo_path=Path.cwd())
            self.assertEqual(result.status, "error")
            self.assertIn("claude", result.message.lower())

    def test_openshard_missing(self):
        with patch(f"{_MODULE}.shutil.which", side_effect=lambda n: "/x/claude" if n == "claude" else None):
            result = install_claude_mcp(repo_path=Path.cwd())
            self.assertEqual(result.status, "error")
            self.assertIn("openshard", result.message.lower())

    def test_outside_repository(self):
        with CliRunner().isolated_filesystem() as tmp:
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.find_repo_root", return_value=None):
                result = install_claude_mcp(repo_path=Path(tmp))
                self.assertEqual(result.status, "error")
                self.assertIn("git repository", result.message.lower())

    def test_fresh_install_success(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run) as mock_run:
                result = install_claude_mcp(repo_path=root)
            self.assertEqual(result.status, "installed")
            self.assertEqual(result.repo_root, root.resolve())
            self.assertEqual(
                result.command,
                ["openshard", "mcp", "serve", "--repo-path", str(root.resolve())],
            )
            add_calls = [c for c in mock_run.call_args_list if "add" in c.args[0]]
            self.assertEqual(len(add_calls), 1)
            argv = add_calls[0].args[0]
            self.assertEqual(argv[0], "/usr/local/bin/claude")
            self.assertIn("--scope", argv)
            self.assertIn("local", argv)
            self.assertIn("--", argv)
            # everything after "--" is the exact server launch command
            tail = argv[argv.index("--") + 1 :]
            self.assertEqual(tail, ["openshard", "mcp", "serve", "--repo-path", str(root.resolve())])

    def test_fresh_install_never_uses_shell(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run) as mock_run:
                install_claude_mcp(repo_path=root)
            for c in mock_run.call_args_list:
                self.assertNotIn("shell", c.kwargs)
                self.assertIsInstance(c.args[0], list)

    def test_repo_path_with_spaces_stays_one_argv_element(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = Path(tmp) / "my repo with spaces"
            root.mkdir()
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run) as mock_run:
                result = install_claude_mcp(repo_path=root)
            self.assertEqual(result.status, "installed")
            add_call = [c for c in mock_run.call_args_list if "add" in c.args[0]][0]
            argv = add_call.args[0]
            self.assertIn(str(root.resolve()), argv)

    def test_already_installed_is_noop(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            resolved = str(root.resolve())
            run = _subprocess_run_router(existing_repo_path=resolved)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run) as mock_run:
                result = install_claude_mcp(repo_path=root)
            self.assertEqual(result.status, "already_installed")
            add_calls = [c for c in mock_run.call_args_list if "add" in c.args[0]]
            remove_calls = [c for c in mock_run.call_args_list if "remove" in c.args[0]]
            self.assertEqual(add_calls, [])
            self.assertEqual(remove_calls, [])

    def test_existing_points_elsewhere_gets_updated(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path="/some/other/repo")
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run) as mock_run:
                result = install_claude_mcp(repo_path=root)
            self.assertEqual(result.status, "updated")
            remove_calls = [c for c in mock_run.call_args_list if "remove" in c.args[0]]
            add_calls = [c for c in mock_run.call_args_list if "add" in c.args[0]]
            self.assertEqual(len(remove_calls), 1)
            self.assertEqual(len(add_calls), 1)

    def test_claude_add_failure_is_reported(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            run = _subprocess_run_router(
                existing_repo_path=None,
                add_result=_completed(returncode=1, stderr="boom"),
            )
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run):
                result = install_claude_mcp(repo_path=root)
            self.assertEqual(result.status, "error")
            self.assertIn("boom", result.message)

    def test_mcp_extra_missing_warns_but_still_installs(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run), \
                 patch(f"{_MODULE}.mcp_extra_installed", return_value=False):
                result = install_claude_mcp(repo_path=root)
            self.assertEqual(result.status, "installed")
            self.assertTrue(any("mcp" in w for w in result.warnings))

    def test_no_secrets_in_result(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run):
                result = install_claude_mcp(repo_path=root)
            blob = json.dumps(
                {
                    "status": result.status,
                    "command": result.command,
                    "message": result.message,
                    "warnings": result.warnings,
                }
            )
            for needle in ("API_KEY", "api_key", "token", "password", "secret"):
                self.assertNotIn(needle, blob)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

class TestCliHelp(unittest.TestCase):
    def test_mcp_group_help_shows_install(self):
        result = CliRunner().invoke(cli, ["mcp", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("install", result.output)

    def test_mcp_install_group_help_shows_claude(self):
        result = CliRunner().invoke(cli, ["mcp", "install", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("claude", result.output)

    def test_mcp_install_claude_help_exits_zero(self):
        result = CliRunner().invoke(cli, ["mcp", "install", "claude", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)


class TestCliInstall(unittest.TestCase):
    def _which(self, name):
        return {"claude": "/usr/local/bin/claude", "openshard": "/usr/local/bin/openshard"}.get(name)

    def test_successful_install_output(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run):
                result = runner.invoke(cli, ["mcp", "install", "claude"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("OpenShard MCP installed for Claude Code.", result.output)
            self.assertIn("Restart Claude Code", result.output)
            for tool in MCP_TOOLS:
                self.assertIn(tool, result.output)

    def test_json_output_is_valid(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run):
                result = runner.invoke(cli, ["mcp", "install", "claude", "--json"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            data = json.loads(result.output)
            self.assertEqual(data["status"], "installed")
            self.assertTrue(data["command"][0].endswith("openshard"))

    def test_claude_missing_exits_nonzero_with_actionable_message(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            with patch(f"{_MODULE}.shutil.which", return_value=None):
                result = runner.invoke(cli, ["mcp", "install", "claude"])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("claude", result.output.lower())

    def test_outside_repository_exits_nonzero(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.find_repo_root", return_value=None):
                result = runner.invoke(cli, ["mcp", "install", "claude"])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("git repository", result.output.lower())

    def test_repeated_install_is_idempotent_no_duplicate(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            resolved = str(root.resolve())
            run = _subprocess_run_router(existing_repo_path=resolved)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run) as mock_run:
                result = runner.invoke(cli, ["mcp", "install", "claude"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("already configured", result.output.lower())
            add_calls = [c for c in mock_run.call_args_list if "add" in c.args[0]]
            self.assertEqual(add_calls, [])

    def test_repo_with_spaces_via_repo_path_option(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as tmp:
            root = Path(tmp) / "spacey repo"
            root.mkdir()
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run):
                result = runner.invoke(cli, ["mcp", "install", "claude", "--repo-path", str(root)])
            self.assertEqual(result.exit_code, 0, msg=result.output)

    def test_never_touches_real_claude_binary(self):
        """The CLI path must never shell out to a real `claude` process in tests."""
        runner = CliRunner()
        with runner.isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run) as mock_run:
                runner.invoke(cli, ["mcp", "install", "claude"])
            self.assertTrue(mock_run.called)


if __name__ == "__main__":
    unittest.main()
