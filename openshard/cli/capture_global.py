"""``openshard capture install|uninstall claude --global``: Claude Code's user-level hooks."""
from __future__ import annotations

import json

import click


def run(*, install: bool, as_json: bool) -> None:
    """Install or remove Openshard's user-level Claude Code hooks and report the result."""
    from openshard.adapters.claude_user_hooks import install_user_hooks, uninstall_user_hooks
    from openshard.sync.config import load_link

    result = install_user_hooks() if install else uninstall_user_hooks()
    linked = load_link() is not None
    if as_json:
        click.echo(json.dumps({
            "scope": "user",
            "status": result.status,
            "settings_path": str(result.settings_path) if result.settings_path else None,
            "events": result.events,
            "message": result.message,
            "platform_linked": linked,
        }, indent=2))
    else:
        click.echo(result.message)
        if result.settings_path is not None:
            click.echo(f"  settings: {result.settings_path}")
        if install and result.status != "error":
            click.echo("  Every git repository Claude Code works in now gets Receipts, with no per-repository setup.")
            click.echo("  Repositories with their own Openshard hooks keep using them; nothing is recorded twice.")
            click.echo("  A session started in a folder that is not a git repository records nothing.")
            click.echo("  Restart open Claude Code sessions to pick this up.")
            if not linked:
                click.echo("  Receipts stay on this computer until you run `openshard login` (once, for every repository).")
    if result.status == "error":
        raise SystemExit(1)
