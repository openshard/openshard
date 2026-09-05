"""Tests for the `openshard` console-script fast dispatcher (PR7).

`openshard.cli.entrypoint` fast-paths `hooks claude` / `hooks claude-status`
around the full Click app's import graph (see the module docstring for why:
those two commands are the ones Claude Code spawns as a fresh process per
hook, with `Stop`/`SessionEnd` and the status line running synchronously).
These tests exercise the *real installed console script* end to end, since
the whole point is a property of process start-up that in-process
`CliRunner` tests (used by tests/test_cli_claude_hooks.py) cannot see.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SID = "0f1e2d3c-4b5a-4697-8877-665544332211"


def _openshard_argv() -> list[str]:
    exe = shutil.which("openshard")
    return [exe] if exe else [sys.executable, "-m", "openshard.cli.entrypoint"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _payload(event: str, repo: Path, **fields: object) -> str:
    data: dict[str, object] = {
        "session_id": SID, "cwd": str(repo), "hook_event_name": event,
        "transcript_path": "/tmp/t.jsonl", "permission_mode": "default",
    }
    data.update(fields)
    return json.dumps(data)


def _lines(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _run(argv: list[str], stdin: str, repo: Path) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
    return subprocess.run(_openshard_argv() + argv, input=stdin, text=True, capture_output=True, env=env, timeout=30)


class TestFastPathParity:
    """The fast dispatcher must behave identically to the full Click command."""

    def test_hooks_claude_writes_the_expected_record(self, repo: Path):
        result = _run(["hooks", "claude"], _payload("UserPromptSubmit", repo, prompt="Add a widget"), repo)
        assert result.returncode == 0
        assert result.stdout == ""
        result2 = _run(["hooks", "claude"], _payload("Stop", repo), repo)
        assert result2.returncode == 0
        lines = _lines(repo)
        assert len(lines) == 1
        assert lines[0]["task"] == "Add a widget"
        assert lines[0]["executor"] == "claude_code_hooks"

    def test_hooks_claude_empty_stdin_is_harmless(self, repo: Path):
        result = _run(["hooks", "claude"], "", repo)
        assert result.returncode == 0
        assert result.stdout == ""
        assert not (repo / ".openshard").exists() or _lines(repo) == []

    def test_hooks_claude_event_override_long_form(self, repo: Path):
        data = json.loads(_payload("UserPromptSubmit", repo, prompt="task"))
        del data["hook_event_name"]
        result = _run(["hooks", "claude", "--event", "UserPromptSubmit"], json.dumps(data), repo)
        assert result.returncode == 0
        assert len(_lines(repo)) == 1

    def test_hooks_claude_event_override_equals_form(self, repo: Path):
        data = json.loads(_payload("UserPromptSubmit", repo, prompt="task"))
        del data["hook_event_name"]
        result = _run(["hooks", "claude", "--event=UserPromptSubmit"], json.dumps(data), repo)
        assert result.returncode == 0
        assert len(_lines(repo)) == 1

    def test_claude_status_empty_stdin_matches_click_echo_newline(self, repo: Path):
        # Parity with the full CLI's `click.echo(text)`, which always emits
        # a trailing newline even for an empty string.
        result = _run(["hooks", "claude-status"], "", repo)
        assert result.returncode == 0
        assert result.stdout == "\n"

    def test_claude_status_prints_text_and_exits_zero(self, repo: Path):
        _run(["hooks", "claude"], _payload("UserPromptSubmit", repo, prompt="task"), repo)
        payload = json.dumps({
            "session_id": SID, "cwd": str(repo),
            "model": {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"},
        })
        result = _run(["hooks", "claude-status"], payload, repo)
        assert result.returncode == 0
        assert "Claude Sonnet 5" in result.stdout

    def test_unknown_flag_falls_through_to_full_cli_like_before(self, repo: Path):
        """Anything the fast path doesn't recognize must fall through unchanged."""
        fast = _run(["hooks", "claude", "--bogus-flag"], "", repo)
        full = subprocess.run(
            [sys.executable, "-c", "from openshard.cli.main import cli; cli()", "hooks", "claude", "--bogus-flag"],
            input="", text=True, capture_output=True, timeout=30,
        )
        assert fast.returncode == full.returncode

    def test_help_falls_through_to_full_cli(self, repo: Path):
        result = _run(["hooks", "claude", "--help"], "", repo)
        assert result.returncode == 0
        assert "stdin" in result.stdout

    def test_non_hook_commands_still_work(self):
        result = subprocess.run(_openshard_argv() + ["--version"], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0
        assert "openshard" in result.stdout.lower()


class TestFastPathAvoidsHeavyImports:
    """The whole point of the dispatcher: it must not import the full CLI's dependency graph."""

    def test_hooks_claude_does_not_import_run_pipeline_or_httpx(self):
        probe = (
            "import sys; sys.argv=['openshard','hooks','claude']\n"
            "import io; sys.stdin = io.StringIO('')\n"
            "from openshard.cli.entrypoint import main\n"
            "main()\n"
            "print('PIPELINE=' + str('openshard.run.pipeline' in sys.modules))\n"
            "print('HTTPX=' + str('httpx' in sys.modules))\n"
            "print('CLI_MAIN=' + str('openshard.cli.main' in sys.modules))\n"
        )
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert "PIPELINE=False" in result.stdout
        assert "HTTPX=False" in result.stdout
        assert "CLI_MAIN=False" in result.stdout

    def test_non_fast_path_command_does_import_full_cli(self):
        probe = (
            "import sys; sys.argv=['openshard','--version']\n"
            "from openshard.cli.entrypoint import main\n"
            "try:\n"
            "    main()\n"
            "except SystemExit:\n"
            "    pass\n"
            "print('CLI_MAIN=' + str('openshard.cli.main' in sys.modules))\n"
        )
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert "CLI_MAIN=True" in result.stdout


class TestStdioEncodingHardening:
    """Regression tests for the Windows Unicode crash (narrow/legacy console
    code pages such as CP1252 raising ``UnicodeEncodeError`` on output that
    contains glyphs like "->", "OK" or dashes).

    Click's own built-in safety net (``click._compat._stream_is_misconfigured``)
    only recovers from a stream that reports pure ASCII; it does not cover
    CP1252 and similar narrow encodings, which is exactly what a default
    Windows console uses. ``_harden_stdio_encoding`` closes that gap at the
    single real entrypoint (``openshard.cli.entrypoint:main``) instead of
    hand-replacing every Unicode glyph in the CLI's output.
    """

    def _run_probe(self, code: str, encoding: str) -> subprocess.CompletedProcess:
        import os

        env = {**os.environ, "PYTHONIOENCODING": encoding}
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=30)

    def test_cp1252_stdout_without_hardening_crashes(self):
        """Control: confirms the underlying bug still exists in plain click.echo."""
        result = self._run_probe("import click\nclick.echo('A \\u2192 B')\n", "cp1252")
        assert result.returncode != 0
        assert "UnicodeEncodeError" in result.stderr

    def test_cp1252_stdout_with_hardening_does_not_crash(self):
        code = (
            "from openshard.cli.entrypoint import _harden_stdio_encoding\n"
            "_harden_stdio_encoding()\n"
            "import click\n"
            "click.echo('A \\u2192 B')\n"
        )
        result = self._run_probe(code, "cp1252")
        assert result.returncode == 0, result.stderr
        assert "A" in result.stdout and "B" in result.stdout

    def test_utf8_stdout_output_unaffected_by_hardening(self):
        code = (
            "from openshard.cli.entrypoint import _harden_stdio_encoding\n"
            "_harden_stdio_encoding()\n"
            "import click\n"
            "click.echo('A \\u2192 B \\u2713 \\u2717')\n"
        )
        result = self._run_probe(code, "utf-8")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "A \u2192 B \u2713 \u2717"

    def test_real_entrypoint_survives_cp1252_stdout(self, tmp_path: Path):
        """End-to-end: a real CLI command with no Unicode-safe wrapping of
        its own (`openshard roster validate`, which echoes an em dash)
        must not crash when stdout reports CP1252, via the installed
        console script or `python -m openshard.cli.entrypoint`."""
        import os

        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        result = subprocess.run(
            _openshard_argv() + ["roster", "validate"],
            cwd=tmp_path, capture_output=True, env=env, timeout=30,
        )
        stdout = result.stdout.decode("cp1252")
        stderr = result.stderr.decode("cp1252")
        assert result.returncode == 0, stderr
        assert "UnicodeEncodeError" not in stderr
        assert "nothing to validate" in stdout

    def test_harden_skips_streams_without_reconfigure(self):
        """Streams that don't support `.reconfigure()` (e.g. test capture
        buffers) must be left alone rather than raising."""
        import io

        from openshard.cli.entrypoint import _harden_stdio_encoding

        with patch.object(sys, "stdout", io.StringIO()), patch.object(sys, "stderr", io.StringIO()):
            _harden_stdio_encoding()  # must not raise

    def test_harden_swallows_reconfigure_errors(self):
        """A stream whose `.reconfigure()` itself raises must not take down
        the CLI at start-up."""
        from openshard.cli.entrypoint import _harden_stdio_encoding

        class _Boom:
            def reconfigure(self, **kwargs: object) -> None:
                raise ValueError("nope")

        with patch.object(sys, "stdout", _Boom()), patch.object(sys, "stderr", _Boom()):
            _harden_stdio_encoding()  # must not raise
