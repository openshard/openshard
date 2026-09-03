"""CLI tests for `openshard hooks claude` (Demo v1 PR5).

The command is the executable Claude Code's hooks invoke. It must read the
hook JSON from stdin, never write to stdout, never fail the user's Claude
session (exit 0), and land evidence in the repository's runs.jsonl via the
existing history layer.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli

SID = "12345678-aaaa-4bbb-8ccc-1234567890ab"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "hook repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _payload(event: str, repo: Path, **fields) -> str:
    data = {"session_id": SID, "cwd": str(repo), "hook_event_name": event,
            "transcript_path": "/tmp/t.jsonl", "permission_mode": "default"}
    data.update(fields)
    return json.dumps(data)


def _invoke(repo: Path, stdin: str | None, *args: str):
    runner = CliRunner()
    with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(repo)}, clear=False):
        return runner.invoke(cli, ["hooks", "claude", *args], input=stdin)


def _lines(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


class TestHelp:
    def test_hooks_group_help(self):
        result = CliRunner().invoke(cli, ["hooks", "--help"])
        assert result.exit_code == 0, result.output
        assert "claude" in result.output

    def test_hooks_claude_help(self):
        result = CliRunner().invoke(cli, ["hooks", "claude", "--help"])
        assert result.exit_code == 0, result.output
        assert "stdin" in result.output
        assert "--event" in result.output


class TestProtocol:
    def test_exit_zero_and_silent_stdout_on_valid_payload(self, repo: Path):
        result = _invoke(repo, _payload("UserPromptSubmit", repo, prompt="Fix the parser"))
        assert result.exit_code == 0, result.output
        assert result.stdout == ""
        assert result.exception is None

    def test_empty_stdin_is_harmless(self, repo: Path):
        result = _invoke(repo, "")
        assert result.exit_code == 0
        assert result.stdout == ""
        assert not (repo / ".openshard").exists()

    def test_malformed_stdin_is_harmless(self, repo: Path):
        result = _invoke(repo, "{this is not json")
        assert result.exit_code == 0
        assert result.stdout == ""
        assert not (repo / ".openshard").exists()

    def test_unknown_event_is_harmless(self, repo: Path):
        result = _invoke(repo, _payload("PreCompact", repo))
        assert result.exit_code == 0
        assert result.stdout == ""
        assert _lines(repo) == []

    def test_event_override_when_payload_has_no_event_name(self, repo: Path):
        data = json.loads(_payload("UserPromptSubmit", repo, prompt="Fix the parser"))
        del data["hook_event_name"]
        result = _invoke(repo, json.dumps(data), "--event", "UserPromptSubmit")
        assert result.exit_code == 0
        assert len(_lines(repo)) == 1

    def test_debug_diagnostics_go_to_stderr_only(self, repo: Path):
        with patch.dict(os.environ, {"OPENSHARD_HOOK_DEBUG": "1"}):
            result = _invoke(repo, _payload("Stop", repo))
        assert result.exit_code == 0
        assert result.stdout == ""
        assert "[openshard hooks]" in result.stderr


class TestEndToEnd:
    def test_session_recorded_through_cli(self, repo: Path):
        assert _invoke(repo, _payload("SessionStart", repo, source="startup")).exit_code == 0
        assert _invoke(repo, _payload("UserPromptSubmit", repo, prompt="Add a greeting helper")).exit_code == 0
        (repo / "greet.py").write_text("def hi():\n    return 'hi'\n", encoding="utf-8")
        assert _invoke(repo, _payload(
            "PostToolUse", repo, tool_name="Write",
            tool_input={"file_path": str(repo / "greet.py"), "content": "RAW"},
        )).exit_code == 0
        assert _invoke(repo, _payload("Stop", repo, last_assistant_message="RAW ASSISTANT")).exit_code == 0
        assert _invoke(repo, _payload("SessionEnd", repo, reason="prompt_input_exit")).exit_code == 0

        lines = _lines(repo)
        assert len(lines) == 1
        entry = lines[0]
        assert entry["executor"] == "claude_code_hooks"
        assert entry["task"] == "Add a greeting helper"
        assert [f["path"] for f in entry["files_detail"]] == ["greet.py"]
        assert entry["capture"]["session_end_observed"] is True
        raw = (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")
        assert "RAW" not in raw
        assert str(repo) not in raw

    def test_visible_via_openshard_last_and_query_layer(self, repo: Path):
        _invoke(repo, _payload("UserPromptSubmit", repo, prompt="Add a greeting helper"))
        _invoke(repo, _payload("SessionEnd", repo, reason="other"))
        runner = CliRunner()
        orig = os.getcwd()
        os.chdir(repo)
        try:
            result = runner.invoke(cli, ["last", "--json"])
        finally:
            os.chdir(orig)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["run"]["import_source"] == "claude_code"
        assert data["run"]["executor"] == "claude_code_hooks"

        from openshard.history.query import relevant_context

        ctx = relevant_context("improve the greeting helper", repo_path=repo)
        assert len(ctx.matches) == 1

    def test_repeated_invocation_is_idempotent_on_record_count(self, repo: Path):
        for _ in range(3):
            _invoke(repo, _payload("UserPromptSubmit", repo, prompt="task"))
            _invoke(repo, _payload("Stop", repo))
        assert len(_lines(repo)) == 1
        assert _lines(repo)[0]["capture"]["prompt_count"] == 3


def _status_json(repo: Path, **fields) -> str:
    data = {"session_id": SID, "cwd": str(repo)}
    data.update(fields)
    return json.dumps(data)


def _invoke_status(stdin: str, env: dict | None = None):
    runner = CliRunner()
    with patch.dict(os.environ, env or {}, clear=False):
        return runner.invoke(cli, ["hooks", "claude-status"], input=stdin)


class TestStatusLineCli:
    def test_status_line_help(self):
        result = CliRunner().invoke(cli, ["hooks", "claude-status", "--help"])
        assert result.exit_code == 0, result.output
        assert "status" in result.output.lower()

    def test_prints_status_text_and_exits_zero(self, repo: Path):
        payload = _status_json(repo, model={"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"})
        result = _invoke_status(payload, {"CLAUDE_PROJECT_DIR": str(repo)})
        assert result.exit_code == 0, result.output
        assert "Claude Sonnet 5" in result.output

    def test_empty_stdin_is_harmless(self):
        result = _invoke_status("")
        assert result.exit_code == 0
        assert result.output == "\n"  # click.echo("") still emits the trailing newline

    def test_malformed_stdin_is_harmless(self):
        result = _invoke_status("{not json")
        assert result.exit_code == 0

    def test_status_feeds_model_into_next_fold(self, repo: Path):
        assert _invoke(repo, _payload("UserPromptSubmit", repo, prompt="task")).exit_code == 0
        payload = _status_json(repo, model={"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"})
        assert _invoke_status(payload, {"CLAUDE_PROJECT_DIR": str(repo)}).exit_code == 0
        assert _invoke(repo, _payload("Stop", repo)).exit_code == 0
        entry = _lines(repo)[0]
        assert entry["execution_model"] == "claude-sonnet-5"

    def test_no_absolute_path_in_output_or_store(self, repo: Path):
        payload = _status_json(repo, model={"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"})
        result = _invoke_status(payload, {"CLAUDE_PROJECT_DIR": str(repo)})
        assert str(repo) not in result.output
