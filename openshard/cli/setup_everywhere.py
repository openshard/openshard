"""``openshard setup --everywhere``: one setup for every place a person works with Claude.

1. This computer: Openshard's user-level Claude Code hooks, so every Claude Code
   session here (terminal or the desktop app's Code tab) gets Receipts, in
   whichever repository it works.
2. The Platform: link this computer once (``openshard login``), so Receipts leave it.
3. Claude chats that write code (desktop, web, phone): they run on a fresh cloud
   machine this computer cannot reach, so a plugin for the person's Claude account
   carries the hooks there (``openshard plugin claude``).

Each step says what it did and what is still the person's to do. Nothing is
claimed working that this command did not check.
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import click


def agents_page(endpoint: str) -> str | None:
    """The Platform web page that creates a connected-capture credential, when it can be told. Never raises."""
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname or ""
        if not host.startswith("api."):
            return None
        netloc = "app." + host[len("api."):] + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, "/settings/agents", "", ""))
    except ValueError:
        return None


def default_plugin_path() -> Path:
    desktop = Path.home() / "Desktop"
    return (desktop if desktop.is_dir() else Path.cwd()) / "openshard.plugin"


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def run(ctx: click.Context, *, token_file: Path | None, plugin_path: Path | None, install_spec: str | None,
        skip_cloud: bool) -> None:
    from openshard import __version__
    from openshard.adapters.claude_plugin import (
        DEFAULT_INSTALL_SPEC,
        Connection,
        PluginError,
        build_plugin,
    )
    from openshard.adapters.claude_user_hooks import install_user_hooks
    from openshard.sync.config import load_link

    todo: list[str] = []
    click.echo("1. This computer (Claude Code in a terminal or the desktop app's Code tab)")
    hooks = install_user_hooks()
    if hooks.status == "error":
        click.echo(f"   Not done: {hooks.message}")
        todo.append("Fix the Claude Code settings error above, then run this again.")
    else:
        click.echo(f"   Done: {hooks.message}")
        click.echo("   Open Claude Code sessions pick this up when they restart.")

    click.echo("\n2. The Platform (so Receipts leave this computer)")
    link = load_link()
    if link is None and _interactive():
        from openshard.cli.login_cmd import login_cmd

        ctx.invoke(login_cmd, endpoint=None, no_browser=False, repo_path=None, as_json=False)
        link = load_link()
    if link is None:
        click.echo("   Not done: this computer is not linked.")
        todo.append("Run `openshard login` to link this computer to the Platform.")
    else:
        click.echo(f"   Done: linked to organisation {link.organisation_id} at {link.endpoint}.")

    click.echo("\n3. Claude chats that write code (desktop, web, phone)")
    if skip_cloud:
        click.echo("   Skipped (--no-cloud).")
    elif link is None:
        click.echo("   Not done: needs the Platform link from step 2.")
    else:
        token = token_file.read_text(encoding="utf-8").strip() if token_file is not None else ""
        if not token and _interactive():
            page = agents_page(link.endpoint)
            click.echo("   Chats run on a fresh cloud machine each time; a plugin for your Claude account")
            click.echo("   brings capture there. It needs a connected-capture credential (osc_...), which")
            click.echo("   can only add capture evidence, never read Receipts.")
            click.echo(f"   Create one at {page or 'the Platform: Settings -> Agents'} and paste it here.")
            token = click.prompt("   Credential (hidden; Enter to skip)", default="", show_default=False,
                                 hide_input=True).strip()
        if not token:
            click.echo("   Not done: no credential given.")
            todo.append("Run `openshard plugin claude --token-file <file>` to cover Claude chats.")
        else:
            out = plugin_path or default_plugin_path()
            try:
                build_plugin(out, version=__version__, install_spec=install_spec or DEFAULT_INSTALL_SPEC,
                             connection=Connection(endpoint=link.endpoint, organisation_id=link.organisation_id,
                                                   token=token))
            except PluginError as exc:
                click.echo(f"   Not done: {exc}.")
                todo.append("Check the credential and run this again.")
            else:
                click.echo(f"   Wrote {out}")
                todo.append(f"Add {out.name} to your Claude account once: Customize -> Plugins "
                            "(or share it in a chat and accept it).")

    click.echo("")
    if todo:
        click.echo("Still to do:")
        for item in todo:
            click.echo(f"  - {item}")
    click.echo("Then do one small coding task; its Receipt shows on the Platform a few minutes after it ends.")
