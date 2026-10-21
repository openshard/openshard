"""``openshard setup``'s human output: one line per component, then a readiness verdict.

"Ready" is said only on evidence (``onboarding/platform_link.py``): local
capture is set up *and* the Platform acknowledged what this machine sent. A
link not confirmed yet, or no link at all, is said as such, with the next step.
"""
from __future__ import annotations

import os

import click

from openshard.onboarding.platform_link import (
    DELIVERY_CONFIRMED,
    KIND_NONE,
    PlatformLinkStatus,
    platform_link,
)

_STATE_LABELS = {"installed": "installed", "updated": "updated", "already_installed": "already configured"}
_AGENT_STATE_LABELS = {
    **_STATE_LABELS,
    "skipped": "not found (skipped)", "skipped_existing": "skipped (custom file present)",
    "skipped_optin": "detected; run `openshard capture install hermes`",
    "error": "NOT configured",
}
_OTHER_AGENTS = (
    ("codex", "Codex:        "), ("opencode", "OpenCode:     "), ("cursor", "Cursor:       "),
    ("antigravity", "Antigravity:  "), ("grok_build", "Grok Build:   "), ("hermes", "Hermes:       "),
)


def render_setup_result(result) -> None:
    """Human-readable rendering of a claude_setup.SetupResult for `openshard setup`."""
    link = platform_link(getattr(result, "repo_root", None))
    click.echo("\nOpenshard setup\n")
    render_components(result, link)
    _render_readiness(result, link)


def render_components(result, link: PlatformLinkStatus | None = None) -> None:
    """One line per component of a claude_setup.SetupResult, as the installers reported it."""
    link = link if link is not None else platform_link(getattr(result, "repo_root", None))
    from openshard.adapters import agent_commit_trailer
    from openshard.adapters.claude_setup import HISTORY_RELPATH
    from openshard.cli.main import _telemetry_line, _telemetry_status

    click.echo(f"  Repository:    {'git repository' if result.is_git else 'not a git repository'}")
    if result.is_git:
        click.echo(f"  Claude Code:   {'detected' if result.claude_cli.available else 'not found'}")
    click.echo(f"  Local history: {'writable' if result.history_writable else 'not writable'} "
               f"({HISTORY_RELPATH.as_posix()})")
    if result.mcp is not None:
        click.echo(f"  MCP:           {_STATE_LABELS.get(result.mcp.status, result.mcp.status)}")
    if result.hooks is not None:
        click.echo(f"  Auto-capture:  {_STATE_LABELS.get(result.hooks.status, result.hooks.status)}")
    if result.statusline is not None:
        statusline_state = {
            "installed": "installed", "already_installed": "already configured",
            "skipped_existing": "skipped (custom status line present)",
        }.get(result.statusline.status, result.statusline.status)
        click.echo(f"  Enrichment:    {statusline_state}")
    if result.capture_service is not None:
        from openshard.adapters.capture_refresh import setup_service_line

        click.echo(f"  Capture:       {setup_service_line(result.capture_service)}")
    click.echo(f"  Platform:      {link.text}")
    ignore_status = (getattr(result, "history_ignore", None) or {}).get("status")
    if ignore_status in ("added", "already_ignored"):
        where = "added to .git/info/exclude" if ignore_status == "added" else "already ignored"
        click.echo(f"  Git ignore:    .openshard/ local state ({where})")
    agent_commit_trailer.echo_setup_line(result, click.echo)
    click.echo(f"  Improve Openshard: {_telemetry_line(_telemetry_status())}")
    for key, label in _OTHER_AGENTS:  # one line per additional agent, whether or not it was found
        agent_result = (result.agents or {}).get(key)
        if agent_result is not None:
            click.echo(f"  {label} {_AGENT_STATE_LABELS.get(agent_result.status, agent_result.status)}")


def configured_agent_names(result) -> list[str]:
    """Display names of the agents whose capture the installers actually configured."""
    from openshard.adapters.agent_setup import agent_label

    return ["Claude Code" if k == "claude_code" else agent_label(k) for k in result.configured_agents()]


def _restart_note(result) -> str | None:
    """Claude Code reads hooks when a session starts: a session already running misses new ones."""
    changed = getattr(result.hooks, "status", None) in ("installed", "updated")
    if changed and os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return ("This Claude Code session started before its hooks were installed: start a new "
                "session so capture covers your work.")
    return None


def _platform_lines(link: PlatformLinkStatus) -> list[str]:
    """Where Receipts go, said only as far as the evidence goes."""
    if link.delivery == DELIVERY_CONFIRMED:
        return ["Receipts reach the Openshard Platform."]
    if link.kind == KIND_NONE:
        return ["Receipts stay on this machine: not linked to the Openshard Platform.",
                "To see them on the Platform, run `openshard login`."]
    return ["Linked to the Openshard Platform, but no delivery has been confirmed yet.",
            "It confirms with the first captured work; check with `openshard sync status`."]


def _render_readiness(result, link: PlatformLinkStatus) -> None:
    """The readiness verdict and next steps of a claude_setup.SetupResult."""
    from openshard.adapters.claude_setup import display_next_steps

    agent_names = configured_agent_names(result)
    use_line = f"Use {', '.join(agent_names)} normally." if agent_names else "Use your coding agent normally."
    open_line = (
        f"Open {' or '.join(agent_names)} in this repository." if agent_names
        else "Open your coding agent in this repository."
    )
    click.echo("")
    if result.readiness == "not_ready":
        click.echo("Openshard setup is incomplete.")
        click.echo("\nNext step:")
        for step in display_next_steps(result):
            click.echo(f"  - {step}")
        return
    confirmed = link.delivery == DELIVERY_CONFIRMED
    if result.readiness == "ready" and confirmed:
        click.echo(f"Openshard is ready. {use_line}")
    elif result.readiness == "ready":
        click.echo(f"Openshard is capturing on this machine. {use_line}")
    else:
        click.echo("Openshard is capturing on this machine, with limitations (see below).")
        click.echo(f"{use_line} Shards and receipts are still recorded.")
    for line in _platform_lines(link):
        click.echo(line)
    click.echo("\nNext steps:")
    click.echo(f"  1. {open_line}")
    click.echo("  2. Complete a normal coding task.")
    click.echo("  3. Run `openshard last` to see the captured Receipt.")
    for step in [note for note in (_restart_note(result),) if note] + display_next_steps(result):
        click.echo(f"  ! {step}")
