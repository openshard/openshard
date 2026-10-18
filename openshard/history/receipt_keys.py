"""The per-install Ed25519 key that seals Receipts (docs/signing.md, Phase 1).

One key per install, kept in the user-global OpenShard home
(``~/.openshard/keys/``, ``OPENSHARD_HOME`` applies), never inside a
repository, never logged and never synced. It is created lazily, on the
first seal, under a lock so a capture service and a CLI process racing to
create it end up with the same key.

* ``receipt-signing.ed25519``      PKCS#8 PEM, unencrypted, mode 0600
* ``receipt-signing.ed25519.pub``  JSON: algorithm, keyid, raw public key
  (base64), created_at

The directory is mode 0700. On Windows POSIX modes mean nothing; the key
file is restricted to the current user with ``icacls`` (inheritance
removed, owner-only full control). When that cannot be confirmed the key
is still used and :attr:`SigningKey.owner_only` is ``False`` so a later
``doctor`` check can warn.

What the key proves, and what it does not, is in docs/signing.md: a
same-user agent can read this file and sign with it.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from openshard.util.home import openshard_home

KEYS_DIRNAME = "keys"
PRIVATE_KEY_FILENAME = "receipt-signing.ed25519"
PUBLIC_KEY_FILENAME = "receipt-signing.ed25519.pub"
ALGORITHM = "ed25519"
KEYID_PREFIX = "ed25519:"


class KeyUnavailableError(RuntimeError):
    """No usable signing key: the home is not writable, the key file is
    unreadable or corrupt, or the crypto library is missing. ``reason`` is a
    short machine token recorded with the "not signed" outcome."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SigningKey:
    private_key: object  # cryptography Ed25519PrivateKey; typed loosely so import stays lazy
    public_raw: bytes
    keyid: str
    owner_only: bool

    @property
    def fingerprint8(self) -> str:
        return fingerprint8(self.keyid)

    def sign(self, message: bytes) -> bytes:
        return self.private_key.sign(message)  # type: ignore[attr-defined]


def keys_dir(env: dict | None = None) -> Path:
    return Path(openshard_home(env)) / KEYS_DIRNAME


def keyid_for(public_raw: bytes) -> str:
    """``ed25519:`` + sha256 hex of the 32-byte raw public key."""
    return KEYID_PREFIX + hashlib.sha256(public_raw).hexdigest()


def fingerprint8(keyid: str) -> str:
    """The first 8 hex characters of a keyid, for display."""
    return keyid[len(KEYID_PREFIX):][:8] if keyid.startswith(KEYID_PREFIX) else keyid[:8]


def _restrict_windows(path: Path) -> bool:
    """Owner-only ACL on *path* via icacls. True only when icacls succeeded."""
    user = os.environ.get("USERNAME")
    if not user:
        return False
    try:
        done = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


_WINDOWS_CONFIRMED: dict[str, bool] = {}  # icacls runs once per key file per process


def _owner_only(path: Path) -> bool:
    """Best-effort: make *path* owner-only and report whether that is confirmed."""
    if os.name == "nt":
        if str(path) not in _WINDOWS_CONFIRMED:
            _WINDOWS_CONFIRMED[str(path)] = _restrict_windows(path)
        return _WINDOWS_CONFIRMED[str(path)]
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            os.chmod(path, 0o600)
        return not stat.S_IMODE(path.stat().st_mode) & 0o077
    except OSError:
        return False


def _ensure_dir(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
    except OSError as exc:
        raise KeyUnavailableError("key_dir_unwritable") from exc


def _write_owner_only(path: Path, data: bytes) -> None:
    """Write *data* to *path* via a 0600 temp file and an atomic replace."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _public_record(public_raw: bytes) -> dict:
    return {
        "algorithm": ALGORITHM,
        "keyid": keyid_for(public_raw),
        "public_key": base64.b64encode(public_raw).decode("ascii"),
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }


def _load(private_path: Path) -> SigningKey:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        loaded = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    except OSError as exc:
        raise KeyUnavailableError("key_unreadable") from exc
    except (ValueError, TypeError) as exc:
        raise KeyUnavailableError("key_corrupt") from exc
    if not isinstance(loaded, Ed25519PrivateKey):
        raise KeyUnavailableError("key_corrupt")
    public_raw = loaded.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return SigningKey(loaded, public_raw, keyid_for(public_raw), _owner_only(private_path))


def _create(private_path: Path, public_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    _write_owner_only(private_path, pem)
    _write_owner_only(public_path, (json.dumps(_public_record(public_raw), indent=2) + "\n").encode())


def load_or_create_key(env: dict | None = None) -> SigningKey:
    """This install's signing key, created on first use. Raises
    :class:`KeyUnavailableError` (never anything else) when there is none.

    An existing key file that cannot be read or parsed is never replaced:
    seals made with it must stay checkable against its public key.
    """
    try:
        import cryptography  # noqa: F401
    except ImportError as exc:
        raise KeyUnavailableError("crypto_unavailable") from exc
    from openshard.history.jsonl_store import LockTimeoutError, history_file_lock

    directory = keys_dir(env)
    private_path = directory / PRIVATE_KEY_FILENAME
    public_path = directory / PUBLIC_KEY_FILENAME
    if private_path.is_file():
        return _load(private_path)
    _ensure_dir(directory)
    try:
        with history_file_lock(private_path, timeout=10.0):
            if not private_path.is_file():
                _create(private_path, public_path)
    except LockTimeoutError as exc:
        raise KeyUnavailableError("key_lock_timeout") from exc
    except OSError as exc:
        raise KeyUnavailableError("key_dir_unwritable") from exc
    return _load(private_path)


def read_public_key(env: dict | None = None) -> tuple[str, bytes] | None:
    """``(keyid, raw public key)`` of this install's key, or ``None``. Never
    creates a key and never raises. Read from the private key when present
    (the ``.pub`` file is a convenience copy that could be edited)."""
    private_path = keys_dir(env) / PRIVATE_KEY_FILENAME
    if not private_path.is_file():
        return None
    try:
        key = _load(private_path)
    except (KeyUnavailableError, ImportError):
        return None
    return key.keyid, key.public_raw


__all__ = [
    "KeyUnavailableError",
    "SigningKey",
    "fingerprint8",
    "keyid_for",
    "keys_dir",
    "load_or_create_key",
    "read_public_key",
]
