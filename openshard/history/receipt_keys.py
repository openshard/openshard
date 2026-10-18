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


# ---------------------------------------------------------------------------
# Keyring: public keys only (``openshard keys``, ``openshard verify --signature``)
# ---------------------------------------------------------------------------

RETIRED_DIRNAME = "retired"  # this install's earlier keys, after ``keys rotate``
TRUSTED_DIRNAME = "trusted"  # other installs' keys pinned with ``keys trust``
REVOCATIONS_FILENAME = "revoked.json"  # {keyid: revoked_at}

SOURCE_THIS_INSTALL = "this_install"
SOURCE_RETIRED = "this_install_retired"
SOURCE_PINNED = "pinned"


@dataclass(frozen=True)
class PublicKey:
    """A public key this install trusts. Never carries private material."""

    keyid: str
    public_raw: bytes
    source: str
    retired_at: str | None = None
    revoked_at: str | None = None


def public_key_record(keyid: str, public_raw: bytes) -> dict:
    """The exportable form: what ``keys export`` writes and ``keys trust`` reads."""
    return {"algorithm": ALGORITHM, "keyid": keyid, "public_key": base64.b64encode(public_raw).decode("ascii")}


def parse_public_key(data: object) -> tuple[str, bytes] | None:
    """``(keyid, raw)`` from an exported record, or ``None`` when it is not a
    well-formed Ed25519 public key whose keyid matches its bytes."""
    if not isinstance(data, dict) or data.get("algorithm", ALGORITHM) != ALGORITHM:
        return None
    try:
        raw = base64.b64decode(str(data.get("public_key", "")), validate=True)
    except ValueError:
        return None
    if len(raw) != 32:
        return None
    keyid = keyid_for(raw)
    return (keyid, raw) if data.get("keyid", keyid) == keyid else None


def _hex(keyid: str) -> str:
    return keyid[len(KEYID_PREFIX):] if keyid.startswith(KEYID_PREFIX) else keyid


def _read_key_dir(directory: Path) -> list[tuple[str, bytes, dict]]:
    out: list[tuple[str, bytes, dict]] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.pub")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        parsed = parse_public_key(data)
        if parsed:
            out.append((parsed[0], parsed[1], data))
    return out


def revocations(env: dict | None = None) -> dict[str, str]:
    try:
        data = json.loads((keys_dir(env) / REVOCATIONS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}


def keyring(env: dict | None = None) -> dict[str, PublicKey]:
    """Every public key trusted here: this install's current key, its retired
    keys and pinned keys, with revocation times. Never creates a key; never raises."""
    directory = keys_dir(env)
    revoked = revocations(env)
    ring: dict[str, PublicKey] = {}
    for keyid, raw, _data in _read_key_dir(directory / TRUSTED_DIRNAME):
        ring[keyid] = PublicKey(keyid, raw, SOURCE_PINNED, revoked_at=revoked.get(keyid))
    for keyid, raw, data in _read_key_dir(directory / RETIRED_DIRNAME):
        retired_at = data.get("retired_at") if isinstance(data.get("retired_at"), str) else None
        ring[keyid] = PublicKey(keyid, raw, SOURCE_RETIRED, retired_at, revoked.get(keyid))
    own = read_public_key(env)
    if own:
        ring[own[0]] = PublicKey(own[0], own[1], SOURCE_THIS_INSTALL, revoked_at=revoked.get(own[0]))
    return ring


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def rotate_key(env: dict | None = None) -> tuple[str | None, str]:
    """Replace this install's key. The old public key moves to ``retired/`` so
    old seals keep verifying; records are not re-signed. Returns
    ``(old keyid or None, new keyid)``. Raises :class:`KeyUnavailableError`
    when the current key cannot be read (it is then left untouched)."""
    try:
        import cryptography  # noqa: F401
    except ImportError as exc:
        raise KeyUnavailableError("crypto_unavailable") from exc
    from openshard.history.jsonl_store import LockTimeoutError, history_file_lock

    directory = keys_dir(env)
    private_path = directory / PRIVATE_KEY_FILENAME
    _ensure_dir(directory)
    old: str | None = None
    try:
        with history_file_lock(private_path, timeout=10.0):
            if private_path.is_file():
                current = _load(private_path)
                old = current.keyid
                retired = directory / RETIRED_DIRNAME
                retired.mkdir(mode=0o700, exist_ok=True)
                record = {**public_key_record(current.keyid, current.public_raw), "retired_at": _now()}
                (retired / f"{_hex(current.keyid)}.pub").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            _create(private_path, directory / PUBLIC_KEY_FILENAME)
    except LockTimeoutError as exc:
        raise KeyUnavailableError("key_lock_timeout") from exc
    except OSError as exc:
        raise KeyUnavailableError("key_dir_unwritable") from exc
    return old, _load(private_path).keyid


def trust_key(data: object, env: dict | None = None) -> str:
    """Pin another install's public key (an exported record). Returns its keyid.
    Raises ``ValueError`` when *data* is not a valid exported Ed25519 key."""
    parsed = parse_public_key(data)
    if parsed is None:
        raise ValueError("not an exported Ed25519 public key (algorithm, keyid, public_key)")
    keyid, raw = parsed
    directory = keys_dir(env) / TRUSTED_DIRNAME
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {**public_key_record(keyid, raw), "trusted_at": _now()}
    (directory / f"{_hex(keyid)}.pub").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return keyid


def revoke_key(keyid: str, env: dict | None = None) -> str:
    """Record that *keyid* is revoked from now. Returns ``revoked_at`` (the first
    one, if it was already revoked). Refuses this install's current key:
    rotate first, or every new seal would be invalid."""
    own = read_public_key(env)
    if own and own[0] == keyid:
        raise ValueError("this is the current key of this install; run `openshard keys rotate` first")
    if not keyid.startswith(KEYID_PREFIX) or len(_hex(keyid)) != 64:
        raise ValueError("expected a keyid like ed25519:<64 hex characters>")
    current = revocations(env)
    if keyid not in current:
        current[keyid] = _now()
        directory = keys_dir(env)
        _ensure_dir(directory)
        (directory / REVOCATIONS_FILENAME).write_text(json.dumps(current, indent=2, sort_keys=True) + "\n",
                                                      encoding="utf-8")
    return current[keyid]


def _is_windows() -> bool:
    return os.name == "nt"


def key_health(env: dict | None = None) -> dict:
    """What ``openshard doctor`` reports about the signing key. Never creates
    or prints the key; never raises. On POSIX the mode is only read; on
    Windows the owner-only ACL is (re)applied once and its outcome reported."""
    try:
        import cryptography  # noqa: F401
        crypto = True
    except ImportError:
        crypto = False
    private_path = keys_dir(env) / PRIVATE_KEY_FILENAME
    health: dict = {
        "crypto_available": crypto, "key_present": private_path.is_file(), "keyid": None,
        "problem": None, "owner_only": None, "platform": "windows" if _is_windows() else "posix",
    }
    if not crypto or not health["key_present"]:
        return health
    try:
        key = _load(private_path) if _is_windows() else None
        if key is None:
            # Read the mode before loading: loading tightens a loose mode.
            health["owner_only"] = not stat.S_IMODE(private_path.stat().st_mode) & 0o077
            own = read_public_key(env)
            if own is None:
                raise KeyUnavailableError("key_corrupt")
            health["keyid"] = own[0]
        else:
            health["keyid"], health["owner_only"] = key.keyid, key.owner_only
    except KeyUnavailableError as exc:
        health["problem"] = exc.reason
    except OSError:
        health["problem"] = "key_unreadable"
    return health


__all__ = [
    "KeyUnavailableError",
    "PublicKey",
    "SigningKey",
    "fingerprint8",
    "key_health",
    "keyid_for",
    "keyring",
    "keys_dir",
    "load_or_create_key",
    "parse_public_key",
    "public_key_record",
    "read_public_key",
    "revocations",
    "revoke_key",
    "rotate_key",
    "trust_key",
]
