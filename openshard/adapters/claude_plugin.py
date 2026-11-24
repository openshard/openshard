"""A Claude plugin that brings Openshard capture to every Claude session, including cloud chats.

Claude Code loads hooks from the folder a session starts in, and from the user's
settings on the machine it runs on. A Claude chat that writes code (desktop, web
or phone) runs in a fresh cloud machine that has neither: nothing Openshard
installed on a laptop reaches it. A *plugin* added to the person's Claude account
does: Claude loads its hooks in every session, wherever it runs.

This module writes that plugin. Every capture event runs ``hooks/capture.sh``,
which:

1. stands aside when Openshard's user-level hooks are already installed on this
   machine (``openshard capture install claude --global``), so a laptop never
   records a session twice;
2. loads the person's connection (``connection.env``: Platform endpoint,
   organisation and a connected-capture credential), if the plugin was built
   with one, so a cloud machine's Receipts reach the Platform before it is
   destroyed;
3. hands the event to ``openshard hooks claude --scope user``, which follows the
   session into the repositories it works in (``claude_user_hooks``);
4. when ``openshard`` is not installed yet (every new cloud machine), keeps the
   event in a small spool, installs Openshard once in the background, and
   delivers the spooled events, in order, with the first event after the install
   finishes. Events are delivered late, never invented.

Truth rules: the plugin observes what the hooks deliver, exactly as the
user-level hooks do. A credential written into ``connection.env`` sits in a file
the agent can read; ``build_plugin`` says so wherever it writes one.
"""
from __future__ import annotations

import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

from openshard.adapters.claude_hooks_install import HOOK_SPECS

PLUGIN_NAME = "openshard"
PLUGIN_FILE = "openshard.plugin"
CONNECTION_FILE = "connection.env"
DEFAULT_INSTALL_SPEC = "openshard"
MAX_SPOOLED = 200

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/@+=,#-]{1,300}$")
_TOKEN = re.compile(r"^os[ck]_[A-Za-z0-9_-]{8,200}$")
_ENDPOINT = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/?$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class PluginError(ValueError):
    """A value that cannot be written into the plugin safely."""


@dataclass(frozen=True)
class Connection:
    endpoint: str
    organisation_id: str
    token: str
    surface: str = "claude-chat"


def _check(name: str, value: str, pattern: re.Pattern[str]) -> str:
    value = value.strip()
    if not pattern.match(value):
        raise PluginError(f"{name} is not valid")
    return value


def _hooks_json() -> dict:
    hooks: dict[str, list] = {}
    for spec in HOOK_SPECS:
        entry: dict = {"hooks": [{
            "type": "command",
            "command": 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/capture.sh"',
            "timeout": max(spec.timeout, 10),
        }]}
        if spec.matcher:
            entry["matcher"] = spec.matcher
        hooks.setdefault(spec.event, []).append(entry)
    return {"hooks": hooks}


def _capture_script(install_spec: str) -> str:
    spec = _check("install spec", install_spec, _SAFE_VALUE)
    return f"""#!/bin/sh
# Openshard capture for every Claude session (written by `openshard plugin claude`).
# Never blocks or fails a session: every path exits 0. The event name is in the payload.
ROOT="${{CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}}"
SETTINGS="${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}/settings.json"
# Openshard's user-level hooks already capture every session on this machine.
if grep -qs "hooks claude --scope user" "$SETTINGS"; then exit 0; fi
if [ -f "$ROOT/{CONNECTION_FILE}" ]; then
  set -a; . "$ROOT/{CONNECTION_FILE}"; set +a
fi
OSH="${{OPENSHARD_HOME:-$HOME/.openshard}}"
SPOOL="$OSH/plugin-spool"
OS=$(command -v openshard 2>/dev/null)
[ -n "$OS" ] || OS="$HOME/.local/bin/openshard"
if [ -x "$OS" ] && "$OS" --version >/dev/null 2>&1; then
  if [ -d "$SPOOL" ] && mkdir "$SPOOL.lock" 2>/dev/null; then
    for f in $(ls "$SPOOL" 2>/dev/null | sort); do
      "$OS" hooks claude --scope user < "$SPOOL/$f" >/dev/null 2>&1
      rm -f "$SPOOL/$f"
    done
    rmdir "$SPOOL" "$SPOOL.lock" 2>/dev/null
  fi
  "$OS" hooks claude --scope user >/dev/null 2>&1
  exit 0
fi
# Not installed yet (a new cloud machine): keep this event, install once in the background.
mkdir -p "$SPOOL" 2>/dev/null || exit 0
count=$(ls "$SPOOL" 2>/dev/null | wc -l)
if [ "$count" -lt {MAX_SPOOLED} ]; then
  cat > "$SPOOL/$(date +%s)-$(printf '%06d' "$count")-$$" 2>/dev/null
fi
if mkdir "$OSH/plugin-install.lock" 2>/dev/null; then
  ( ( pipx install "{spec}" || python3 -m pip install --quiet "{spec}" \\
    || python3 -m pip install --quiet --user "{spec}" \\
    || python3 -m pip install --quiet --user --break-system-packages "{spec}" ) </dev/null >/dev/null 2>&1
    rmdir "$OSH/plugin-install.lock" 2>/dev/null ) &
fi
exit 0
"""


def _readme(connected: bool) -> str:
    lines = [
        "# Openshard capture for Claude",
        "",
        "Adds Openshard's capture hooks to every Claude session that writes code, including",
        "chats on desktop, web and phone, which run on a fresh cloud machine each time.",
        "",
        "- On a new machine the first events are kept while Openshard installs itself",
        "  (about a minute), then delivered in order.",
        "- A session is followed into the git repositories it edits; each gets a Receipt.",
        "- On a computer where `openshard capture install claude --global` already ran,",
        "  this plugin stands aside so nothing is recorded twice.",
    ]
    if connected:
        lines += [
            "",
            f"`{CONNECTION_FILE}` holds your Platform connection, including a connected-capture",
            "credential. The agent in each session can read that file. The credential can only",
            "add capture evidence to your organisation; revoke it on the Platform if it leaks.",
        ]
    else:
        lines += [
            "",
            "Built without a Platform connection: Receipts stay on the machine that made them.",
            "A cloud machine is deleted after the session, so its Receipts are lost. Rebuild with",
            "`openshard plugin claude --token-file ...` to send them to the Platform.",
        ]
    return "\n".join(lines) + "\n"


def _connection_env(connection: Connection) -> str:
    endpoint = _check("endpoint", connection.endpoint, _ENDPOINT).rstrip("/")
    org = _check("organisation id", connection.organisation_id, _UUID)
    token = _check("credential", connection.token, _TOKEN)
    surface = _check("surface", connection.surface, _SAFE_VALUE)
    return (
        "# Openshard Platform connection for this plugin. The agent can read this file.\n"
        f"OPENSHARD_CONNECTED_ENDPOINT={endpoint}\n"
        f"OPENSHARD_CONNECTED_ORG_ID={org}\n"
        f"OPENSHARD_CONNECTED_TOKEN={token}\n"
        f"OPENSHARD_CONNECTED_SURFACE={surface}\n"
    )


def plugin_files(
    *, version: str, install_spec: str = DEFAULT_INSTALL_SPEC, connection: Connection | None = None
) -> dict[str, str]:
    """Every file of the plugin, by path inside it. Raises ``PluginError`` for an unsafe value."""
    files = {
        ".claude-plugin/plugin.json": json.dumps({
            "name": PLUGIN_NAME,
            "version": version,
            "description": "Openshard Receipts for every Claude session that writes code, "
                           "including cloud chats on desktop, web and phone.",
            "author": {"name": "Openshard", "url": "https://openshard.dev"},
            "homepage": "https://openshard.dev",
            "license": "Apache-2.0",
        }, indent=2) + "\n",
        "hooks/hooks.json": json.dumps(_hooks_json(), indent=2) + "\n",
        "hooks/capture.sh": _capture_script(install_spec),
        "README.md": _readme(connection is not None),
    }
    if connection is not None:
        files[CONNECTION_FILE] = _connection_env(connection)
    return files


def build_plugin(
    output: Path, *, version: str, install_spec: str = DEFAULT_INSTALL_SPEC, connection: Connection | None = None
) -> Path:
    """Write the plugin as a ``.plugin`` archive (a zip) at *output*. Returns the path written."""
    files = plugin_files(version=version, install_spec=install_spec, connection=connection)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            mode = 0o755 if name.endswith(".sh") else 0o600 if name == CONNECTION_FILE else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(buffer.getvalue())
    if connection is not None:
        output.chmod(0o600)
    return output


__all__ = [
    "CONNECTION_FILE",
    "Connection",
    "DEFAULT_INSTALL_SPEC",
    "PLUGIN_FILE",
    "PLUGIN_NAME",
    "PluginError",
    "build_plugin",
    "plugin_files",
]
