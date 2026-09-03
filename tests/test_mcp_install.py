"""Tests for `openshard mcp install claude` (Demo v1 PR4, extended by PR5).

Covers both the adapter (openshard.adapters.claude_mcp_install) directly and
the CLI wiring, plus the PR5 auto-capture hook installer
(openshard.adapters.claude_hooks_install). All `claude` CLI invocations are
mocked via subprocess.run/shutil.which, and hook settings are only ever
written under a throw-away temporary repository -- the real Claude Code
configuration on the machine running these tests is never touched.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from openshard.adapters.claude_capture_client import DEFAULT_PORT
from openshard.adapters.claude_hooks_install import (
    HOOK_COMMAND,
    HOOK_EVENTS,
    HTTP_EVENTS,
    SETTINGS_RELPATH,
    STATUS_COMMAND,
    SYNC_EVENTS,
    TOOL_MATCHER,
    build_hook_config,
    ensure_local_settings_ignored,
    install_claude_hooks,
    install_claude_statusline,
    installed_events,
    installed_hook_port,
    is_openshard_hook,
    is_openshard_statusline,
    merge_openshard_hooks,
)
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
_HOOKS_MODULE = "openshard.adapters.claude_hooks_install"


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _git_ignored_router(ignored: bool = True):
    """subprocess.run side_effect for the hook installer's git calls."""
    def _run(argv, **kwargs):
        if "check-ignore" in argv:
            return _completed(returncode=0 if ignored else 1)
        if "rev-parse" in argv:
            return _completed(returncode=0, stdout=".git/info/exclude\n")
        raise AssertionError(f"unexpected git call: {argv}")
    return _run


def _read_settings(root: Path) -> dict:
    return json.loads((root / SETTINGS_RELPATH).read_text(encoding="utf-8"))


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

    def setUp(self):
        # The hook installer shells out to git for the ignore check; keep it
        # deterministic (and never touching a real repository) in every test.
        self._git_patch = patch(f"{_HOOKS_MODULE}.subprocess.run", side_effect=_git_ignored_router())
        self._git_patch.start()
        self.addCleanup(self._git_patch.stop)

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
            # PR5: hooks are installed alongside MCP by default.
            self.assertIn("Auto-capture hooks: installed", result.output)
            self.assertTrue((root / SETTINGS_RELPATH).exists())
            self.assertEqual(installed_events(_read_settings(root)), list(HOOK_EVENTS))

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
            self.assertEqual(data["hooks"]["status"], "installed")
            self.assertEqual(set(data["hooks"]["events"]), set(HOOK_EVENTS))
            self.assertTrue(data["hooks"]["settings_path"].endswith("settings.local.json"))

    def test_no_hooks_flag_skips_hook_installation(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run):
                result = runner.invoke(cli, ["mcp", "install", "claude", "--no-hooks"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("skipped", result.output)
            self.assertFalse((root / SETTINGS_RELPATH).exists())
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run):
                as_json = runner.invoke(cli, ["mcp", "install", "claude", "--no-hooks", "--json"])
            self.assertEqual(json.loads(as_json.output)["hooks"], {"status": "skipped"})

    def test_statusline_configured_alongside_hooks_by_default(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run):
                result = runner.invoke(cli, ["mcp", "install", "claude"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Status line: installed", result.output)
            self.assertEqual(_read_settings(root)["statusLine"], {"type": "command", "command": STATUS_COMMAND})

    def test_no_statusline_flag_skips_only_statusline(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run):
                result = runner.invoke(cli, ["mcp", "install", "claude", "--no-statusline"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Auto-capture hooks: installed", result.output)
            self.assertIn("Status line (model/cost/token capture): skipped.", result.output)
            self.assertNotIn("statusLine", _read_settings(root))

    def test_second_install_reports_hooks_already_configured(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run):
                runner.invoke(cli, ["mcp", "install", "claude"])
                first = (root / SETTINGS_RELPATH).read_text(encoding="utf-8")
                result = runner.invoke(cli, ["mcp", "install", "claude"])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Auto-capture hooks: already configured", result.output)
            self.assertEqual((root / SETTINGS_RELPATH).read_text(encoding="utf-8"), first)

    def test_hook_install_failure_is_reported_nonzero_after_mcp_success(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".claude").mkdir()
            (root / SETTINGS_RELPATH).write_text("{ broken json", encoding="utf-8")
            run = _subprocess_run_router(existing_repo_path=None)
            with patch(f"{_MODULE}.shutil.which", side_effect=self._which), \
                 patch(f"{_MODULE}.subprocess.run", side_effect=run):
                result = runner.invoke(cli, ["mcp", "install", "claude"])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("OpenShard MCP installed for Claude Code.", result.output)
            self.assertIn("NOT configured", result.output)
            # The unparsable file was left exactly as it was.
            self.assertEqual((root / SETTINGS_RELPATH).read_text(encoding="utf-8"), "{ broken json")

    def test_mcp_failure_does_not_write_hooks(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            with patch(f"{_MODULE}.shutil.which", return_value=None):
                result = runner.invoke(cli, ["mcp", "install", "claude"])
            self.assertNotEqual(result.exit_code, 0)
            self.assertFalse((root / SETTINGS_RELPATH).exists())

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


# ---------------------------------------------------------------------------
# PR5: Claude Code auto-capture hook installer
# ---------------------------------------------------------------------------

class TestHookConfigShape(unittest.TestCase):
    def test_config_covers_every_supported_event(self):
        # PR9.5: every hot event is an HTTP hook to the local capture service;
        # SessionStart (which Claude Code never delivers over HTTP) stays the
        # command that starts the service.
        config = build_hook_config()
        self.assertEqual(set(config), set(HOOK_EVENTS))
        self.assertEqual(set(HTTP_EVENTS), set(HOOK_EVENTS) - {"SessionStart"})
        for event, groups in config.items():
            self.assertEqual(len(groups), 1, event)
            hooks = groups[0]["hooks"]
            self.assertEqual(len(hooks), 1)
            self.assertIsInstance(hooks[0]["timeout"], int)
            if event == "SessionStart":
                self.assertEqual(hooks[0]["type"], "command")
                self.assertEqual(hooks[0]["command"], HOOK_COMMAND)
            else:
                self.assertEqual(hooks[0]["type"], "http")
                self.assertEqual(hooks[0]["url"], f"http://127.0.0.1:{DEFAULT_PORT}/hooks/claude")
                self.assertEqual(hooks[0]["allowedEnvVars"], ["CLAUDE_PROJECT_DIR"])
                self.assertEqual(hooks[0]["headers"], {"X-OpenShard-Project-Dir": "$CLAUDE_PROJECT_DIR"})

    def test_port_is_honoured(self):
        config = build_hook_config(port=50123)
        for event in HTTP_EVENTS:
            self.assertEqual(config[event][0]["hooks"][0]["url"], "http://127.0.0.1:50123/hooks/claude")
        self.assertEqual(installed_hook_port({"hooks": config}), 50123)
        self.assertIsNone(installed_hook_port({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": HOOK_COMMAND}]}]}}))

    def test_tool_events_carry_matcher_others_do_not(self):
        config = build_hook_config()
        for event in ("PostToolUse", "PostToolUseFailure"):
            self.assertEqual(config[event][0]["matcher"], TOOL_MATCHER)
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
            self.assertNotIn("matcher", config[event][0])

    def test_every_hook_is_synchronous(self):
        # A warm service answers in milliseconds, and synchronous delivery
        # keeps events strictly ordered (an async Stop could overtake the
        # tool hooks before it). Nothing is marked async any more.
        config = build_hook_config()
        self.assertEqual(SYNC_EVENTS, set(HOOK_EVENTS))
        for event in HOOK_EVENTS:
            self.assertNotIn("async", config[event][0]["hooks"][0], event)

    def test_command_contains_no_machine_specific_path(self):
        blob = json.dumps(build_hook_config())
        self.assertNotIn("\\", blob)
        self.assertNotIn("/Users/", blob)
        self.assertNotIn("C:", blob)
        self.assertNotIn(str(Path.cwd()), blob)

    def test_is_openshard_hook(self):
        self.assertTrue(is_openshard_hook({"type": "command", "command": HOOK_COMMAND}))
        self.assertTrue(is_openshard_hook({"type": "command", "command": HOOK_COMMAND + " --event Stop"}))
        self.assertTrue(is_openshard_hook({"type": "http", "url": f"http://127.0.0.1:{DEFAULT_PORT}/hooks/claude"}))
        self.assertTrue(is_openshard_hook({"type": "http", "url": "http://localhost:47815/hooks/claude/"}))
        self.assertFalse(is_openshard_hook({"type": "http", "url": "http://127.0.0.1:47811/other"}))
        self.assertFalse(is_openshard_hook({"type": "http", "url": "http://example.com:47811/hooks/claude"}))
        self.assertFalse(is_openshard_hook({"type": "http", "url": "https://127.0.0.1:47811/hooks/claude"}))
        self.assertFalse(is_openshard_hook({"type": "command", "command": "openshard hooks claudette"}))
        self.assertFalse(is_openshard_hook({"type": "command", "command": "echo hi"}))
        self.assertFalse(is_openshard_hook({"type": "prompt", "command": HOOK_COMMAND}))
        self.assertFalse(is_openshard_hook("openshard hooks claude"))

    def test_legacy_command_hooks_are_upgraded_in_place(self):
        # A pre-PR9.5 settings file (command hooks everywhere) becomes the
        # HTTP layout on the next install, reported as "updated", with no
        # duplicate and no leftover command entry for the HTTP events.
        legacy = {"hooks": {
            event: [{"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 15, "async": True}]}]
            for event in HOOK_EVENTS
        }}
        merged, changes = merge_openshard_hooks(legacy)
        self.assertEqual(merged["hooks"], build_hook_config())
        for event in HTTP_EVENTS:
            self.assertEqual(changes[event], "updated")

    def test_port_change_is_an_update(self):
        current, _ = merge_openshard_hooks({}, port=DEFAULT_PORT)
        moved, changes = merge_openshard_hooks(current, port=DEFAULT_PORT + 1)
        self.assertTrue(all(changes[e] == "updated" for e in HTTP_EVENTS))
        self.assertEqual(changes["SessionStart"], "unchanged")
        self.assertEqual(installed_hook_port(moved), DEFAULT_PORT + 1)


class TestMergeOpenshardHooks(unittest.TestCase):
    def test_fresh_settings(self):
        merged, changes = merge_openshard_hooks({})
        self.assertEqual(merged["hooks"], build_hook_config())
        self.assertTrue(all(v == "added" for v in changes.values()))

    def test_idempotent(self):
        once, _ = merge_openshard_hooks({})
        twice, changes = merge_openshard_hooks(once)
        self.assertEqual(once, twice)
        self.assertTrue(all(v == "unchanged" for v in changes.values()))

    def test_input_not_mutated(self):
        original = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
        snapshot = json.loads(json.dumps(original))
        merge_openshard_hooks(original)
        self.assertEqual(original, snapshot)

    def test_unrelated_hooks_and_settings_preserved(self):
        settings = {
            "permissions": {"allow": ["Bash(pytest)"]},
            "model": "opus",
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./guard.sh"}]}],
                "PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "prettier"}]}],
            },
        }
        merged, changes = merge_openshard_hooks(settings)
        self.assertEqual(merged["permissions"], settings["permissions"])
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(merged["hooks"]["PreToolUse"], settings["hooks"]["PreToolUse"])
        self.assertEqual(merged["hooks"]["Stop"][0], settings["hooks"]["Stop"][0])
        self.assertEqual(merged["hooks"]["PostToolUse"][0], settings["hooks"]["PostToolUse"][0])
        self.assertEqual(len(merged["hooks"]["Stop"]), 2)
        self.assertEqual(len(merged["hooks"]["PostToolUse"]), 2)
        self.assertTrue(all(v == "added" for v in changes.values()))

    def test_existing_openshard_hook_not_duplicated(self):
        once, _ = merge_openshard_hooks({})
        for _ in range(3):
            once, _ = merge_openshard_hooks(once)
        for event in HOOK_EVENTS:
            ours = [
                h for g in once["hooks"][event] for h in g["hooks"] if is_openshard_hook(h)
            ]
            self.assertEqual(len(ours), 1, event)

    def test_stale_openshard_hook_updated_in_place(self):
        stale = {"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [
            {"type": "command", "command": HOOK_COMMAND, "timeout": 99}]}]}}
        merged, changes = merge_openshard_hooks(stale)
        self.assertEqual(changes["PostToolUse"], "updated")
        self.assertEqual(len(merged["hooks"]["PostToolUse"]), 1)
        self.assertEqual(merged["hooks"]["PostToolUse"][0]["matcher"], TOOL_MATCHER)
        self.assertEqual(merged["hooks"]["PostToolUse"][0]["hooks"], build_hook_config()["PostToolUse"][0]["hooks"])

    def test_our_hook_inside_a_shared_user_group_is_rehomed_without_touching_theirs(self):
        shared = {"hooks": {"PostToolUse": [{"matcher": "Write", "hooks": [
            {"type": "command", "command": "prettier"},
            {"type": "command", "command": HOOK_COMMAND},
        ]}]}}
        merged, changes = merge_openshard_hooks(shared)
        self.assertEqual(changes["PostToolUse"], "updated")
        groups = merged["hooks"]["PostToolUse"]
        self.assertEqual(groups[0], {"matcher": "Write", "hooks": [{"type": "command", "command": "prettier"}]})
        self.assertEqual(groups[1], build_hook_config()["PostToolUse"][0])

    def test_duplicate_openshard_groups_collapse_to_one(self):
        dup = {"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": HOOK_COMMAND}]},
            {"hooks": [{"type": "command", "command": HOOK_COMMAND}]},
        ]}}
        merged, changes = merge_openshard_hooks(dup)
        self.assertEqual(changes["Stop"], "updated")
        self.assertEqual(merged["hooks"]["Stop"], build_hook_config()["Stop"])

    def test_unexpected_layout_is_refused(self):
        with self.assertRaises(ValueError):
            merge_openshard_hooks({"hooks": []})
        with self.assertRaises(ValueError):
            merge_openshard_hooks({"hooks": {"Stop": {"not": "a list"}}})

    def test_installed_events(self):
        self.assertEqual(installed_events({}), [])
        self.assertEqual(installed_events("nope"), [])
        merged, _ = merge_openshard_hooks({})
        self.assertEqual(installed_events(merged), list(HOOK_EVENTS))
        partial = {"hooks": {"Stop": build_hook_config()["Stop"]}}
        self.assertEqual(installed_events(partial), ["Stop"])


class TestInstallClaudeHooks(unittest.TestCase):
    def _root(self, tmp: str) -> Path:
        root = Path(tmp) / "repo with spaces"
        root.mkdir()
        (root / ".git").mkdir()
        return root

    def test_fresh_install_writes_local_settings(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = self._root(tmp)
            with patch(f"{_HOOKS_MODULE}.subprocess.run", side_effect=_git_ignored_router()):
                result = install_claude_hooks(repo_root=root)
            self.assertEqual(result.status, "installed", result.message)
            self.assertEqual(result.settings_path, root / SETTINGS_RELPATH)
            self.assertEqual(result.warnings, [])
            self.assertEqual(_read_settings(root)["hooks"], build_hook_config())
            text = (root / SETTINGS_RELPATH).read_text(encoding="utf-8")
            self.assertTrue(text.endswith("\n"))
            self.assertNotIn(str(root), text)

    def test_repeated_install_is_idempotent_and_does_not_rewrite(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = self._root(tmp)
            with patch(f"{_HOOKS_MODULE}.subprocess.run", side_effect=_git_ignored_router()):
                install_claude_hooks(repo_root=root)
                path = root / SETTINGS_RELPATH
                before = path.read_text(encoding="utf-8")
                mtime = path.stat().st_mtime_ns
                again = install_claude_hooks(repo_root=root)
            self.assertEqual(again.status, "already_installed")
            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_existing_unrelated_hooks_preserved(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = self._root(tmp)
            (root / ".claude").mkdir()
            existing = {
                "permissions": {"allow": ["Bash(ls)"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]},
            }
            (root / SETTINGS_RELPATH).write_text(json.dumps(existing), encoding="utf-8")
            with patch(f"{_HOOKS_MODULE}.subprocess.run", side_effect=_git_ignored_router()):
                result = install_claude_hooks(repo_root=root)
            self.assertEqual(result.status, "installed")
            data = _read_settings(root)
            self.assertEqual(data["permissions"], existing["permissions"])
            self.assertEqual(data["hooks"]["Stop"][0], existing["hooks"]["Stop"][0])
            self.assertEqual(installed_events(data), list(HOOK_EVENTS))

    def test_stale_config_reports_updated(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = self._root(tmp)
            (root / ".claude").mkdir()
            stale = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 1}]}]}}
            (root / SETTINGS_RELPATH).write_text(json.dumps(stale), encoding="utf-8")
            with patch(f"{_HOOKS_MODULE}.subprocess.run", side_effect=_git_ignored_router()):
                result = install_claude_hooks(repo_root=root)
            self.assertEqual(result.status, "updated")
            self.assertEqual(result.events["Stop"], "updated")
            self.assertEqual(result.events["SessionEnd"], "added")

    def test_invalid_json_left_untouched(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = self._root(tmp)
            (root / ".claude").mkdir()
            (root / SETTINGS_RELPATH).write_text("{ nope", encoding="utf-8")
            with patch(f"{_HOOKS_MODULE}.subprocess.run", side_effect=_git_ignored_router()):
                result = install_claude_hooks(repo_root=root)
            self.assertEqual(result.status, "error")
            self.assertIn("not valid JSON", result.message)
            self.assertEqual((root / SETTINGS_RELPATH).read_text(encoding="utf-8"), "{ nope")

    def test_empty_file_treated_as_empty_settings(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = self._root(tmp)
            (root / ".claude").mkdir()
            (root / SETTINGS_RELPATH).write_text("", encoding="utf-8")
            with patch(f"{_HOOKS_MODULE}.subprocess.run", side_effect=_git_ignored_router()):
                result = install_claude_hooks(repo_root=root)
            self.assertEqual(result.status, "installed")

    def test_not_ignored_adds_git_info_exclude(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = self._root(tmp)
            with patch(f"{_HOOKS_MODULE}.subprocess.run", side_effect=_git_ignored_router(ignored=False)):
                result = install_claude_hooks(repo_root=root)
                warning = ensure_local_settings_ignored(root)
            self.assertEqual(result.status, "installed")
            self.assertEqual(result.warnings, [])
            self.assertIsNone(warning)
            exclude = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertEqual(exclude.count(SETTINGS_RELPATH.as_posix()), 1)  # appended once

    def test_git_unavailable_yields_warning_not_failure(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = self._root(tmp)
            with patch(f"{_HOOKS_MODULE}.subprocess.run", side_effect=FileNotFoundError("git")):
                result = install_claude_hooks(repo_root=root)
            self.assertEqual(result.status, "installed")
            self.assertTrue(any("ignored" in w for w in result.warnings))

    def test_never_raises(self):
        with patch(f"{_HOOKS_MODULE}._read_settings", side_effect=RuntimeError("boom")):
            result = install_claude_hooks(repo_root=Path("does-not-matter"))
        self.assertEqual(result.status, "error")


class TestInstallClaudeStatusline(unittest.TestCase):
    """PR6: status-line install -- the only surface that carries model/cost/tokens."""

    def _root(self, tmp: str) -> Path:
        root = Path(tmp) / "repo with spaces"
        root.mkdir()
        (root / ".git").mkdir()
        return root

    def test_fresh_install_writes_statusline(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = self._root(tmp)
            result = install_claude_statusline(repo_root=root)
            self.assertEqual(result.status, "installed", result.message)
            data = _read_settings(root)
            self.assertEqual(data["statusLine"], {"type": "command", "command": STATUS_COMMAND})

    def test_repeated_install_is_idempotent_and_does_not_rewrite(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = self._root(tmp)
            install_claude_statusline(repo_root=root)
            path = root / SETTINGS_RELPATH
            before = path.read_text(encoding="utf-8")
            again = install_claude_statusline(repo_root=root)
            self.assertEqual(again.status, "already_installed")
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_existing_foreign_statusline_is_never_touched(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = self._root(tmp)
            (root / ".claude").mkdir()
            foreign = {"statusLine": {"type": "command", "command": "~/.claude/my-statusline.sh"}}
            (root / SETTINGS_RELPATH).write_text(json.dumps(foreign), encoding="utf-8")
            result = install_claude_statusline(repo_root=root)
            self.assertEqual(result.status, "skipped_existing")
            data = _read_settings(root)
            self.assertEqual(data["statusLine"], foreign["statusLine"])

    def test_unrelated_settings_preserved(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = self._root(tmp)
            (root / ".claude").mkdir()
            existing = {"permissions": {"allow": ["Bash(ls)"]}}
            (root / SETTINGS_RELPATH).write_text(json.dumps(existing), encoding="utf-8")
            result = install_claude_statusline(repo_root=root)
            self.assertEqual(result.status, "installed")
            data = _read_settings(root)
            self.assertEqual(data["permissions"], existing["permissions"])

    def test_invalid_json_left_untouched(self):
        with CliRunner().isolated_filesystem() as tmp:
            root = self._root(tmp)
            (root / ".claude").mkdir()
            (root / SETTINGS_RELPATH).write_text("{ nope", encoding="utf-8")
            result = install_claude_statusline(repo_root=root)
            self.assertEqual(result.status, "error")
            self.assertEqual((root / SETTINGS_RELPATH).read_text(encoding="utf-8"), "{ nope")

    def test_is_openshard_statusline(self):
        self.assertTrue(is_openshard_statusline({"type": "command", "command": STATUS_COMMAND}))
        self.assertFalse(is_openshard_statusline({"type": "command", "command": "other"}))
        self.assertFalse(is_openshard_statusline("not-a-dict"))
        self.assertFalse(is_openshard_statusline(None))

    def test_never_raises(self):
        with patch(f"{_HOOKS_MODULE}._read_settings", side_effect=RuntimeError("boom")):
            result = install_claude_statusline(repo_root=Path("does-not-matter"))
        self.assertEqual(result.status, "error")
        self.assertNotIn("boom", result.message)


if __name__ == "__main__":
    unittest.main()
