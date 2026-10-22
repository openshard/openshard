"""The ``openshard ci`` subcommands, registered here so ``cli/main.py`` does not grow with each one."""

from __future__ import annotations

import click


def register_ci_commands(group: click.Group) -> None:
    from openshard.cli.findings_cmd import ci_findings
    from openshard.cli.gate_cmd import ci_gate

    for command in (ci_gate, ci_findings):
        group.add_command(command)


__all__ = ["register_ci_commands"]
