"""Local capability token for the capture service (v0.4.4).

The capture service listens on loopback only, but "anything that can reach
127.0.0.1 is the user" is not true: another local account, a sandboxed
process, or a web page doing a cross-origin ``fetch`` can all connect.
Every ``POST`` to the service therefore has to present a credential in the
``X-OpenShard-Capture-Token`` header; otherwise nothing is recorded and no
control action (shutdown) is taken. ``GET /health`` stays open and carries
nothing that authorises anything.

Two credential shapes, one server-side check
--------------------------------------------
* The **capture token**: 32 random bytes as 64 hex characters, generated
  locally on first use and stored at ``<OPENSHARD_HOME>/capture-token``
  (``~/.openshard/capture-token``, mode 0600). Everything that runs as this
  user in a process of ours -- ``openshard hooks claude|codex|cursor``,
  ``openshard hooks claude-status``, ``openshard capture stop``, the OpenCode
  plugin (which reads the same file) -- presents it. It authorises every
  endpoint, including shutdown.
* The **repository capability**: ``r1.`` + HMAC-SHA256(token, normalised
  repository root). Claude Code's HTTP hooks are the one integration where
  no process of ours runs and no file can be read at delivery time, so the
  installer writes this value into the hook header in
  ``.claude/settings.local.json`` (which the installer keeps out of git via
  ``.git/info/exclude``, and refuses to write into when git tracks it).
  It authorises hook/status events **for that repository only**, never
  shutdown, so a leaked settings file cannot forge evidence elsewhere or
  stop the service. Rotating the token (``openshard capture rotate-token``)
  invalidates every capability at once.

The token is never sent through telemetry (the telemetry grammar has no
free-text property), never logged, never printed by a normal CLI command,
and never returned by ``/health``.

This module is imported by the minimal-import hook client on every hook
invocation, so it uses only ``hashlib``/``hmac``/``os`` at import time.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys

TOKEN_FILENAME = "capture-token"
TOKEN_HEADER = "X-OpenShard-Capture-Token"
REPO_CAPABILITY_PREFIX = "r1."
_TOKEN_HEX_LEN = 64
_MAX_PRESENTED_LEN = 128
_HEX = frozenset("0123456789abcdef")


def _capture_home(env: dict | os._Environ | None) -> str:
    env = os.environ if env is None else env
    override = env.get("OPENSHARD_HOME")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return os.path.join(os.path.expanduser("~"), ".openshard")


def token_path(env: dict | os._Environ | None = None) -> str:
    return os.path.join(_capture_home(env), TOKEN_FILENAME)


def is_token(value: object) -> bool:
    """True for a well-formed capture token (64 lowercase hex characters)."""
    return isinstance(value, str) and len(value) == _TOKEN_HEX_LEN and set(value) <= _HEX


def is_repo_capability(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(REPO_CAPABILITY_PREFIX)
        and is_token(value[len(REPO_CAPABILITY_PREFIX):])
    )


def looks_like_credential(value: object) -> bool:
    """Cheap syntactic pre-check so a request with no plausible credential is
    refused before its body is even parsed."""
    return is_token(value) or is_repo_capability(value)


def load_token(env: dict | os._Environ | None = None) -> str | None:
    """The stored token, or None when absent or malformed. Never raises, never creates."""
    try:
        with open(token_path(env), encoding="utf-8") as fh:
            value = fh.read(_MAX_PRESENTED_LEN + 1).strip()
    except (OSError, ValueError):
        return None
    return value if is_token(value) else None


def _write_token_atomically(path: str, token: str) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        if sys.platform != "win32":
            os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _new_token() -> str:
    import secrets

    return secrets.token_hex(32)


def ensure_token(env: dict | os._Environ | None = None) -> str | None:
    """The stored token, creating it (0600) when absent. Returns None only on I/O failure.

    Race-safe: two processes creating at once both end up with the same
    stored value (the first ``O_EXCL`` create wins, the loser re-reads).
    A malformed file is replaced -- there is nothing valid to keep.
    """
    existing = load_token(env)
    if existing is not None:
        return existing
    path = token_path(env)
    token = _new_token()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        again = load_token(env)
        if again is not None:
            return again
        try:
            _write_token_atomically(path, token)
        except OSError:
            return None
        return token
    except OSError:
        return None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if sys.platform != "win32":
            os.chmod(path, 0o600)
    except OSError:
        return None
    return token


def rotate_token(env: dict | os._Environ | None = None) -> str | None:
    """Replace the stored token with a fresh one. Returns it, or None on I/O failure."""
    token = _new_token()
    try:
        _write_token_atomically(token_path(env), token)
    except OSError:
        return None
    return token


def normalise_root(root: object) -> str:
    """One canonical spelling of a repository root for the HMAC input.

    Both the installer (writing the capability) and the service (checking
    it) run on the same machine, so realpath + normpath + normcase is enough
    to agree on symlinks, trailing separators and case-insensitive drives.
    """
    if isinstance(root, str):
        text = root
    elif isinstance(root, bytes):
        text = root.decode("utf-8", "replace")
    else:
        fspath = getattr(root, "__fspath__", None)
        text = str(fspath()) if callable(fspath) else str(root)
    try:
        text = os.path.realpath(text)
    except (OSError, ValueError):
        pass
    return os.path.normcase(os.path.normpath(text))


def repo_capability(token: str, root: object) -> str:
    """The repository-scoped capability for *root* under *token*."""
    digest = hmac.new(token.encode("ascii"), normalise_root(root).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{REPO_CAPABILITY_PREFIX}{digest}"


def verify_presented(presented: object, token: str | None, root: object | None) -> str | None:
    """Constant-time check of a presented credential.

    Returns ``"token"`` when *presented* is the capture token, ``"repo"``
    when it is the capability for *root*, else ``None``. A ``None`` *root*
    means "no repository context" (e.g. shutdown), where only the token is
    acceptable.
    """
    if not is_token(token) or not isinstance(presented, str) or len(presented) > _MAX_PRESENTED_LEN:
        return None
    assert token is not None  # for type checkers; is_token guarantees it
    if hmac.compare_digest(presented.encode("utf-8"), token.encode("ascii")):
        return "token"
    if root is None or not is_repo_capability(presented):
        return None
    expected = repo_capability(token, root)
    if hmac.compare_digest(presented.encode("utf-8"), expected.encode("ascii")):
        return "repo"
    return None


_BROWSER_HEADERS = ("Origin", "Referer", "Sec-Fetch-Mode", "Sec-Fetch-Site")


def has_browser_headers(headers: object) -> bool:
    """True when the request carries headers only a browser context sets.

    Defence in depth, not the primary check: a page can never present the
    token, but refusing these outright also stops it from probing which
    paths exist.
    """
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return False
    return any(getter(name) not in (None, "") for name in _BROWSER_HEADERS)
