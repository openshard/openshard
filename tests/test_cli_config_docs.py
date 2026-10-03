from __future__ import annotations

from pathlib import Path

from openshard.cli.main import config_cmd

_CLI_REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "cli-reference.md"


def test_cli_reference_documents_every_config_subcommand():
    text = _CLI_REFERENCE.read_text(encoding="utf-8")
    names = sorted(config_cmd.commands)
    assert names, "config group has no subcommands"
    missing = [name for name in names if f"openshard config {name}" not in text]
    assert not missing, f"docs/cli-reference.md does not document: {missing}"
