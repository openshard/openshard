"""Stable repository identity derived from the configured Git ``origin`` remote.

Run records historically identify the repository only by ``repo_name`` — the
local folder name — which is neither stable (a clone can live in any folder)
nor unique (two different repos can share a folder name). This module adds an
*additive* ``repo_identity`` field: a canonical ``host/owner/repo`` string
derived from ``git config --get remote.origin.url``.

Design constraints
------------------
* Purely additive: ``repo_name`` keeps its existing meaning and is still
  written; readers must fall back to it for records without ``repo_identity``.
* Never raises and never makes a network call. Git failures, missing git, or
  a repo without an ``origin`` remote all yield ``None``.
* Credentials embedded in a remote URL (``https://user:token@host/...``) are
  stripped and never persisted.
* Local-path remotes (``/home/me/repo``, ``C:\\src\\repo``, ``file://...``) are
  rejected so a private absolute path is never written to history.
* SSH and HTTPS forms of the same remote canonicalise to the same identity.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_IDENTITY_FIELD = "repo_identity"

_GIT_TIMEOUT_SECONDS = 3

# See the matching comment in adapters/claude_code_import.py: this git call
# can run from the console-less background capture-service worker, which
# would otherwise cause Windows to pop a new console per git.exe child.
_NO_WINDOW_KW: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

# scp-like SSH syntax: ``[user@]host:path`` with no scheme. The host must not
# contain ``/`` and the path must not start with ``/`` (that would be a
# ``host:/abs/path`` local-ish form, still treated as remote here).
_SCP_LIKE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?P<path>[^/].*)$")
_SCHEME = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://(?P<rest>.*)$")
_WINDOWS_DRIVE = re.compile(r"^[a-zA-Z]:[\\/]")

_ACCEPTED_SCHEMES = frozenset({"https", "http", "ssh", "git", "git+ssh", "ssh+git"})


def canonicalize_remote_url(url: object) -> str | None:
    """Return the canonical ``host/owner/repo`` identity for a Git remote URL.

    Returns ``None`` for empty input, local paths, ``file://`` remotes, or
    anything that does not look like a hosted remote. Never raises.

    Equivalences handled: ``https://github.com/o/r.git``,
    ``git@github.com:o/r.git``, ``ssh://git@github.com/o/r`` and
    ``git://github.com/o/r`` all become ``github.com/o/r``. Host is
    lower-cased; the path keeps its case (some hosts are case-sensitive).
    Userinfo (credentials) and a trailing ``.git`` / ``/`` are dropped.
    """
    try:
        if not isinstance(url, str):
            return None
        raw = url.strip()
        if not raw:
            return None
        if _WINDOWS_DRIVE.match(raw) or raw.startswith(("/", "\\", ".", "~")):
            return None

        host: str
        path: str
        m = _SCHEME.match(raw)
        if m:
            scheme = m.group("scheme").lower()
            if scheme not in _ACCEPTED_SCHEMES:
                return None
            rest = m.group("rest")
            authority, _, path = rest.partition("/")
            # Drop userinfo — this is where embedded credentials live.
            if "@" in authority:
                authority = authority.rsplit("@", 1)[1]
            host = authority
        else:
            s = _SCP_LIKE.match(raw)
            if not s:
                return None
            host = s.group("host")
            path = s.group("path")

        host = host.strip().lower()
        if not host or "@" in host:
            return None
        # Strip an explicit default port so ``host:22`` == ``host``.
        if host.endswith((":22", ":443")):
            host = host.rsplit(":", 1)[0]

        path = path.strip().strip("/")
        if path.endswith(".git"):
            path = path[: -len(".git")]
        path = path.rstrip("/")
        if not path or "@" in path:
            return None
        return f"{host}/{path}"
    except Exception:
        return None


def _origin_remote_url(path: Path) -> str | None:
    """Return the raw ``remote.origin.url`` for *path*, or None. Never raises."""
    try:
        r = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(path), capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS,
            **_NO_WINDOW_KW,
        )
        if r.returncode != 0:
            return None
        out = r.stdout.strip()
        return out or None
    except Exception:
        return None


def capture_repo_identity(path: Path) -> str | None:
    """Return the canonical repository identity for the checkout at *path*.

    ``None`` when *path* is not a Git repo, has no ``origin`` remote, the
    remote is a local path, or git is unavailable/slow. Never raises; never
    touches the network (``git config`` is a local read).
    """
    return canonicalize_remote_url(_origin_remote_url(path))


def entry_matches_repo(entry: dict, repo: str) -> bool:
    """True when a run entry belongs to *repo*.

    *repo* may be a canonical identity, any remote URL form (canonicalised
    here), or a plain folder name. Matches, in order: ``repo_identity``
    (canonical, case-insensitive), then the legacy ``repo_name`` folder name,
    then the folder name of ``workspace_path`` — so historical records that
    predate ``repo_identity`` remain filterable.
    """
    wanted = (repo or "").strip()
    if not wanted:
        return True
    wanted_lower = wanted.lower()
    wanted_canonical = (canonicalize_remote_url(wanted) or wanted_lower).lower()

    identity = entry.get(REPO_IDENTITY_FIELD)
    if isinstance(identity, str) and identity:
        ident_lower = identity.lower()
        if ident_lower == wanted_canonical or ident_lower == wanted_lower:
            return True
        # Allow ``owner/repo`` and bare ``repo`` to match a full identity.
        if ident_lower.endswith("/" + wanted_lower):
            return True

    name = entry.get("repo_name")
    if isinstance(name, str) and name and name.lower() == wanted_lower:
        return True

    ws = entry.get("workspace_path")
    if isinstance(ws, str) and ws:
        folder = ws.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if folder and folder.lower() == wanted_lower:
            return True
    return False
