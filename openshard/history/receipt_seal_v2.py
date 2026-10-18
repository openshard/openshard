"""Seal v2: field commitments, the synced-receipt subject and reveals (docs/receipt-seal-v2.md).

A v2 Statement keeps both v1 subjects (``record:`` and ``projection:``,
digested exactly as in v1) and adds:

* ``record_fields:<receipt_id>``: sha256 over the RFC 8785 (JCS) bytes of a
  map ``{field: commitment}`` with one salted commitment per top-level field
  of the stored record (``content_hash`` aside). The salts stay on this
  machine, in ``.openshard/receipt-seal-salts.jsonl``; they are never synced
  and never put in a bundle except for a field the owner chooses to reveal.
* ``sync:<receipt_id>``, only on a seal made by sync: sha256 over the JCS
  bytes of the exact ``receipt`` object sent to the Platform, so the hosted
  copy can be checked against this install's public key.

A commitment is ``sha256(JCS({"field", "receipt_id", "salt", "value"}))``
with a fresh random 256-bit salt per field per seal, so the digests say
nothing about a value without its salt (field *names* are visible to
whoever holds the map, which is only ever revealed with a field).

This module has no key handling and no I/O beyond the salts file; sealing
itself stays in ``receipt_signing``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path

from openshard.util import jcs

SEAL_VERSION = "2"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://github.com/openshard/openshard/blob/main/docs/receipt-seal-v2.md#seal-v2"
CANONICALIZATION = "https://www.rfc-editor.org/rfc/rfc8785"
SALTS_FILENAME = "receipt-seal-salts.jsonl"
REVEAL_TYPE = "https://github.com/openshard/openshard/blob/main/docs/receipt-seal-v2.md#reveal-v1"
SEAL_SYNCED = "synced"

COVERS = ("record_content", "record_fields", "synced_receipt")
DOES_NOT_COVER = ("agent_claims", "verification_outcome", "capture_completeness", "sealed_at")

_NOT_COMMITTED = frozenset({"content_hash"})


def fields_subject(receipt_id: str) -> str:
    return f"record_fields:{receipt_id}"


def sync_subject(receipt_id: str) -> str:
    return f"sync:{receipt_id}"


def new_salt() -> str:
    """256 random bits, base64url without padding."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def commitment(receipt_id: str, name: str, value: object, salt: str) -> str:
    """sha256 hex of the JCS bytes of the four-key commitment object. Raises ``ValueError``
    when *value* is not a JSON value."""
    return jcs.sha256_hex({"field": name, "receipt_id": receipt_id, "salt": salt, "value": value})


def fields_digest(commitments: dict[str, str]) -> str:
    return jcs.sha256_hex(commitments)


def commit_fields(receipt_id: str, entry: dict) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """``(salts, commitments, uncommitted)`` for every top-level field of the
    stored *entry*. A field whose value has no JSON form (NaN, an unpaired
    surrogate) cannot be committed; it is named in *uncommitted*, never
    silently dropped."""
    salts: dict[str, str] = {}
    commitments: dict[str, str] = {}
    uncommitted: list[str] = []
    for name in sorted(entry):
        if name in _NOT_COMMITTED:
            continue
        salt = new_salt()
        try:
            commitments[name] = commitment(receipt_id, name, entry[name], salt)
        except ValueError:
            uncommitted.append(name)
            continue
        salts[name] = salt
    return salts, commitments, uncommitted


def sync_digest(receipt: dict) -> str:
    """sha256 hex of the JCS bytes of *receipt* as sync puts it on the wire
    (``json.dumps(..., ensure_ascii=True, default=str)``, then parsed back), so
    the digest is over exactly what the Platform receives. Raises
    ``ValueError`` when the receipt has no JSON form (NaN)."""
    wire = json.loads(json.dumps(receipt, separators=(",", ":"), ensure_ascii=True, default=str))
    return jcs.sha256_hex(wire)


def build_statement(
    *,
    receipt_id: str,
    shard_id: str | None,
    record_sha256: str,
    projection_sha256: str,
    fields_sha256: str,
    uncommitted: list[str],
    seal_reason: str,
    previous_seal_digest: str | None,
    core_version: str,
    sealed_at: str,
    sync_sha256: str | None = None,
) -> dict:
    subjects = [
        {"name": f"record:{receipt_id}", "digest": {"sha256": record_sha256}},
        {"name": f"projection:{receipt_id}", "digest": {"sha256": projection_sha256}},
        {"name": fields_subject(receipt_id), "digest": {"sha256": fields_sha256}},
    ]
    if sync_sha256 is not None:
        subjects.append({"name": sync_subject(receipt_id), "digest": {"sha256": sync_sha256}})
    return {
        "_type": STATEMENT_TYPE,
        "subject": subjects,
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "seal_version": SEAL_VERSION,
            "receipt_id": receipt_id,
            "shard_id": shard_id,
            "sealed_at": sealed_at,
            "seal_reason": seal_reason,
            "previous_record_digest": previous_seal_digest,
            "core_version": core_version,
            "canonicalization": CANONICALIZATION,
            "uncommitted_fields": list(uncommitted),
            "covers": list(COVERS),
            "does_not_cover": list(DOES_NOT_COVER),
        },
    }


# ---------------------------------------------------------------------------
# Salts file (local only; never synced)
# ---------------------------------------------------------------------------


def salts_path(history_dir: Path) -> Path:
    return Path(history_dir) / SALTS_FILENAME


def append_salts(history_dir: Path, *, receipt_id: str, statement_sha256: str,
                 salts: dict[str, str], commitments: dict[str, str]) -> None:
    """Append one seal's salts, fsync'd; the caller holds the sidecar lock.
    Written before the envelope, so no v2 seal exists without its salts."""
    line = {"receipt_id": receipt_id, "statement_sha256": statement_sha256,
            "salts": salts, "commitments": commitments}
    path = salts_path(history_dir)
    torn = False
    if path.exists() and path.stat().st_size:
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            torn = fh.read(1) != b"\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(("\n" if torn else "") + json.dumps(line, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def find_salts(history_dir: Path, statement_sha256: str) -> dict | None:
    """The salts line of the seal whose Statement bytes hash to *statement_sha256*. Never raises."""
    try:
        text = salts_path(history_dir).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for raw in reversed(text.splitlines()):
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if isinstance(line, dict) and line.get("statement_sha256") == statement_sha256:
            return line
    return None


# ---------------------------------------------------------------------------
# Reveals
# ---------------------------------------------------------------------------


class RevealError(ValueError):
    """A field that cannot be revealed from this seal (and why, in plain words)."""


def build_reveal(history_dir: Path, *, statement_bytes: bytes, receipt_id: str, entry: dict, name: str) -> dict:
    """Disclose one stored field of the record sealed by *statement_bytes*: its
    value, its salt and the seal's commitment map (digests only)."""
    statement_sha = hashlib.sha256(statement_bytes).hexdigest()
    line = find_salts(history_dir, statement_sha)
    if line is None:
        raise RevealError("this seal has no field commitments here (a v1 seal, or its salts file is gone)")
    salts, commitments = line.get("salts") or {}, line.get("commitments") or {}
    if name not in commitments or name not in salts:
        raise RevealError(f"the seal holds no commitment for field {name!r}")
    if name not in entry:
        raise RevealError(f"the record has no field {name!r}")
    value = entry[name]
    if commitment(receipt_id, name, value, salts[name]) != commitments[name]:
        raise RevealError(f"field {name!r} changed after it was sealed")
    return {"_type": REVEAL_TYPE, "receipt_id": receipt_id, "statement_sha256": statement_sha,
            "field": name, "value": value, "salt": salts[name], "commitments": commitments}


def check_reveal(reveal: object, *, statement: dict, statement_bytes: bytes) -> str:
    """``matches`` or why not: the reveal names this seal, its value and salt
    give the committed digest, and the map is the one the seal signed."""
    if not isinstance(reveal, dict) or reveal.get("_type") != REVEAL_TYPE:
        return "malformed"
    if reveal.get("statement_sha256") != hashlib.sha256(statement_bytes).hexdigest():
        return "not_for_latest_seal"
    raw_predicate = statement.get("predicate")
    predicate: dict = raw_predicate if isinstance(raw_predicate, dict) else {}
    receipt_id, name = predicate.get("receipt_id"), reveal.get("field")
    commitments, salt = reveal.get("commitments"), reveal.get("salt")
    if reveal.get("receipt_id") != receipt_id or not isinstance(name, str) or not isinstance(salt, str):
        return "malformed"
    if not isinstance(commitments, dict) or not all(isinstance(v, str) for v in commitments.values()):
        return "malformed"
    signed = next((s.get("digest", {}).get("sha256") for s in statement.get("subject") or []
                   if isinstance(s, dict) and s.get("name") == fields_subject(str(receipt_id))), None)
    try:
        if signed is None or fields_digest(commitments) != signed:
            return "commitments_not_sealed"
        if commitments.get(name) != commitment(str(receipt_id), name, reveal.get("value"), salt):
            return "value_does_not_match"
    except ValueError:
        return "malformed"
    return "matches"


__all__ = [
    "CANONICALIZATION",
    "PREDICATE_TYPE",
    "SALTS_FILENAME",
    "SEAL_SYNCED",
    "SEAL_VERSION",
    "RevealError",
    "append_salts",
    "build_reveal",
    "build_statement",
    "check_reveal",
    "commit_fields",
    "commitment",
    "fields_digest",
    "fields_subject",
    "find_salts",
    "sync_digest",
    "sync_subject",
]
