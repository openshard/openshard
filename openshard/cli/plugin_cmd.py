"""``openshard plugin claude``: write the Claude plugin that captures every Claude session."""
from __future__ import annotations

import json
from pathlib import Path

import click


@click.group("plugin")
def plugin_group() -> None:
    """Plugins that bring Openshard capture to agents installed from an account, not a computer."""


@plugin_group.command("claude")
@click.option("--output", "-o", type=click.Path(dir_okay=False, path_type=Path), default=Path("openshard.plugin"),
              show_default=True, help="Where to write the .plugin file.")
@click.option("--token-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
              help="A file holding a connected-capture credential (osc_...) from the Platform's "
                   "Settings -> Agents page, so cloud sessions' Receipts reach the Platform.")
@click.option("--org", "organisation_id", default=None,
              help="Platform organisation id (default: the one `openshard login` linked).")
@click.option("--endpoint", default=None, help="Platform endpoint (default: the one `openshard login` linked).")
@click.option("--install-spec", default=None,
              help="What a new cloud machine installs (default: openshard from PyPI).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def plugin_claude(output: Path, token_file: Path | None, organisation_id: str | None, endpoint: str | None,
                  install_spec: str | None, as_json: bool) -> None:
    """Write openshard.plugin: Openshard capture for every Claude session, including cloud chats.

    Add the file to your Claude account once (Customize -> Plugins, or accept it when
    Claude shares it in a chat). Every later session, on desktop, web or phone, then
    gets Receipts for the repositories it works in. On a computer where
    `openshard capture install claude --global` already ran, the plugin stands aside.
    """
    from openshard import __version__
    from openshard.adapters.claude_plugin import (
        DEFAULT_INSTALL_SPEC,
        Connection,
        PluginError,
        build_plugin,
    )
    from openshard.sync.config import load_link

    connection = None
    if token_file is not None:
        link = load_link()
        endpoint = endpoint or (link.endpoint if link else None)
        organisation_id = organisation_id or (link.organisation_id if link else None)
        if not endpoint or not organisation_id:
            raise click.ClickException("Pass --endpoint and --org, or run `openshard login` first.")
        token = token_file.read_text(encoding="utf-8").strip()
        connection = Connection(endpoint=endpoint, organisation_id=organisation_id, token=token)
    try:
        path = build_plugin(output, version=__version__, install_spec=install_spec or DEFAULT_INSTALL_SPEC,
                            connection=connection)
    except PluginError as exc:
        raise click.ClickException(f"Plugin not written: {exc}.") from exc
    if as_json:
        click.echo(json.dumps({"path": str(path), "connected": connection is not None,
                               "install_spec": install_spec or DEFAULT_INSTALL_SPEC}, indent=2))
        return
    click.echo(f"Wrote {path}")
    click.echo("  Add it to your Claude account once; every later Claude session gets Receipts.")
    if connection is None:
        click.echo("  No Platform connection: a cloud session's Receipts are lost when its machine is deleted.")
        click.echo("  Rebuild with --token-file to send them to the Platform.")
    else:
        click.echo("  It holds a connected-capture credential the agent in each session can read.")
        click.echo("  That credential can only add capture evidence; revoke it on the Platform if it leaks.")
