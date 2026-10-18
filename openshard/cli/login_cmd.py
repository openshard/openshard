"""`openshard login`: link this computer to a Platform organisation by approving it in the browser.

The device flow itself lives in :mod:`openshard.sync.device_auth`. Once the
Platform hands over an organisation API key, the link is stored with the
same write as `openshard connect` / `openshard sync connect`
(``_store_platform_link``: ``~/.openshard/platform.json``, mode 0600, never
in a repository) and then verified and followed by agent capture setup by
the same helper `connect` uses (``_finish_connect``). The key and the
device code are never printed.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

import click

DEFAULT_ENDPOINT = "https://api.openshard.dev"
CLIENT_NAME = "Openshard CLI"

_NEXT_STEP = {
    "denied": "Run `openshard login` again and approve the request, or ask an organisation owner or admin to approve it.",
    "expired": "Run `openshard login` again to get a new code; it is valid for 10 minutes.",
    "network": "Check that {endpoint} is reachable from this machine, then run `openshard login` again.",
    "cancelled": "Run `openshard login` again when ready. A code approved but never collected expires without creating a key.",
    "rejected": "Run `openshard login` again, or use `openshard connect --endpoint <url> --org <uuid>` with an API key.",
}


def _default_endpoint() -> str:
    """The endpoint of a link already in place (environment or file), else the hosted Platform."""
    from openshard.sync.config import resolve_link

    link = resolve_link()
    return link.endpoint if link is not None else DEFAULT_ENDPOINT


def _has_display() -> bool:
    """False where a browser cannot open for the person at the keyboard (SSH, headless Linux)."""
    if sys.platform in ("win32", "darwin"):
        return not os.environ.get("SSH_CONNECTION")
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _open_browser(url: str) -> bool:
    if urlsplit(url).scheme not in ("https", "http"):
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:  # a broken browser setup must not end the flow; the URL is printed anyway
        return False


def _hostname() -> str | None:
    try:
        name = socket.gethostname().strip()
    except OSError:
        return None
    return name or None


def _fail(kind: str, message: str, *, endpoint: str, as_json: bool, exit_code: int = 1) -> None:
    from openshard.cli.main import _machine_envelope

    step = _NEXT_STEP.get(kind, _NEXT_STEP["rejected"]).format(endpoint=endpoint)
    if as_json:
        click.echo(json.dumps(_machine_envelope(
            "login", kind, connected=False, endpoint=endpoint, error=message, next_steps=[step],
        ), indent=2))
    else:
        click.echo(f"\n{message} Nothing was stored.", err=True)
        click.echo(f"Next: {step}", err=True)
    raise SystemExit(exit_code)


@click.command("login")
@click.option("--endpoint", default=None,
              help=f"Platform API base URL (default: the linked one, else {DEFAULT_ENDPOINT}).")
@click.option("--no-browser", is_flag=True, default=False, help="Print the approval URL without opening a browser.")
@click.option(
    "--repo-path", "repo_path", type=click.Path(exists=True, file_okay=False, path_type=Path), default=None,
    help="Repository to configure agent capture for (default: current directory).",
)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable result on stdout; the code and URL go to stderr.")
def login_cmd(endpoint: str | None, no_browser: bool, repo_path: Path | None, as_json: bool) -> None:
    """Link this computer to Openshard Platform by approving it in your browser: no key to copy.

    Shows a short code and opens the approval page; an organisation owner or
    admin approves it there. The Platform then hands this computer an
    organisation API key once, which is stored exactly as `openshard connect`
    stores it (~/.openshard/platform.json, mode 0600, never printed). Then
    the link is verified and agent capture is set up, as `openshard connect`
    does.
    """
    from openshard.cli.main import _finish_connect, _store_platform_link
    from openshard.sync import device_auth
    from openshard.sync.config import ENDPOINT_ENV, link_from_env, normalise_endpoint

    raw = endpoint if endpoint is not None else _default_endpoint()
    base = normalise_endpoint(raw)
    if base is None:
        raise click.UsageError("--endpoint must be an https:// URL (http:// is allowed for loopback only).")
    say = (lambda text="": click.echo(text, err=True)) if as_json else (lambda text="": click.echo(text))

    hostname = _hostname()
    say(f"Contacting Openshard Platform at {base} to request a sign-in code.")
    say(f"  Sent: client name, Openshard version{', this computer name (' + hostname + ')' if hostname else ''}.")
    try:
        code = device_auth.request_code(base, client_name=CLIENT_NAME, hostname=hostname)
        say("")
        say(f"  Your code:  {code.user_code}")
        say(f"  Approve at: {code.verification_uri_complete}")
        say(f"  (or open {code.verification_uri} and enter the code)")
        say("")
        if not no_browser and _has_display() and _open_browser(code.verification_uri_complete):
            say("Opened your browser. Check the code matches, then approve.")
        else:
            say("Open the URL above in a browser, check the code matches, then approve.")
        say("Waiting for approval (Ctrl+C to cancel)...")
        token = device_auth.poll_for_token(base, code)
    except device_auth.DeviceAuthError as exc:
        _fail(exc.kind, exc.message, endpoint=base, as_json=as_json)
        return
    except KeyboardInterrupt:
        _fail("cancelled", "Cancelled.", endpoint=base, as_json=as_json, exit_code=130)
        return

    link = _store_platform_link(base, token.org_id, token.api_key)
    say(f"Approved for {token.org_name or 'the organisation'} ({token.org_id}).")
    if link_from_env() is not None:
        say(f"Note: OPENSHARD_PLATFORM_* variables ({ENDPOINT_ENV}, ...) are set and take precedence "
            "over the stored link until they are unset.")
    _finish_connect(link, True, repo_path=repo_path, as_json=as_json, command="login",
                    extra={"organisation_name": token.org_name, "device": code.to_public_dict()})
