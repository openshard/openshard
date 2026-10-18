"""Receipt seals: a DSSE envelope over an in-toto Statement (docs/signing.md).

A seal is one narrow claim: *the holder of key K sealed a record whose
content hashed to H.* It adds no evidence level and says nothing about
whether the agent's claims are true, whether checks passed, or whether
capture was complete; the Statement's coverage list says so in fixed
words.

When a record is sealed
-----------------------
Only once it stops changing, at the same points at which it becomes
eligible for sync (``sync.envelope.eligibility``; mirrored by
:func:`seal_point` because the evidence core may not import delivery code,
and a parity test keeps the two equal): a one-shot record (``import``,
``wrap``, native runs) when written, a hook-captured record at session end
or at the idle sweep, a per-turn hook Receipt when its turn completed. An
amendment to a sealed record (``note``, ``feedback``, or an ``openshard
verify`` attestation that changes the projection) writes a new seal with
``seal_reason: amended`` whose ``previous_record_digest`` is the sha256 of
the previous seal's Statement bytes, so the seals of one Receipt form a
chain. A record that was never sealed (written before signing shipped, or
while it was off) is not sealed by a later amendment.

What is never done
------------------
* ``runs.jsonl`` is never touched: seals live in the sidecar
  ``.openshard/receipt-signatures.jsonl``. Only a seal made by sync itself
  (:func:`seal_for_sync`, seal v2, docs/receipt-seal-v2.md) is sent, beside
  the receipt it covers; the sidecar is never read by sync code.
* Sealing never blocks or fails a write. When it cannot sign it appends a
  "not signed" line with a reason (or, when the sidecar itself is not
  writable, a line in the OpenShard home) and returns.
* A record whose stored ``content_hash`` already disagrees with its content
  is never sealed: that would bless an edit nobody can account for.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from openshard.history import receipt_seal_v2 as v2
from openshard.history.receipt_identity import stored_receipt_id
from openshard.history.receipt_keys import (
    KeyUnavailableError,
    fingerprint8,
    load_or_create_key,
)
from openshard.history.shard_hash import (
    INTEGRITY_AS_STORED_FIELD,
    _canonical_json,
    compute_shard_hash,
    verify_shard_hash,
)
from openshard.util.home import openshard_home

SIDECAR_FILENAME = "receipt-signatures.jsonl"
FAILURES_FILENAME = "receipt-signing-failures.jsonl"  # in the OpenShard home
PAYLOAD_TYPE = "application/vnd.in-toto+json"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://github.com/openshard/openshard/blob/main/docs/signing.md#seal-v1"
NOT_SIGNED_TYPE = "https://github.com/openshard/openshard/blob/main/docs/signing.md#not-signed-v1"
SEAL_VERSION = "1"
VERDICT_RULES_VERSION = "1"

# Coverage statement, fixed by seal_version so a verifier prints it rather
# than interpreting free text.
COVERAGE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "1": (
        ("record_content",),
        ("agent_claims", "verification_outcome", "capture_completeness", "sealed_at"),
    ),
    v2.SEAL_VERSION: (v2.COVERS, v2.DOES_NOT_COVER),
}
# predicateType -> the one seal_version it may carry.
PREDICATE_VERSIONS = {PREDICATE_TYPE: SEAL_VERSION, v2.PREDICATE_TYPE: v2.SEAL_VERSION}

SEAL_CREATED = "created"
SEAL_SESSION_ENDED = "session_ended"
SEAL_IDLE_SWEPT = "idle_swept"
SEAL_TURN_COMPLETED = "turn_completed"  # a per-turn Receipt (docs/per-turn-receipts.md)
SEAL_AMENDED = "amended"

# Same threshold as sync.envelope.QUIESCENT_SECONDS and the capture idle sweep.
QUIESCENT_SECONDS = 60 * 60

DISABLE_ENV = "OPENSHARD_RECEIPT_SIGNING"  # off / 0 / false / no / disabled
CONFIG_KEY = "receipt_signing"  # ``receipt_signing: false`` in .openshard/config.yml
_OFF_VALUES = frozenset({"0", "off", "false", "no", "disabled"})

VERDICT_VERIFIED = "verified"
VERDICT_INVALID = "invalid"
VERDICT_NOT_SIGNED = "not_signed"
VERDICT_UNKNOWN_KEY = "signed_unknown_key"
# The envelope names a key whose public half is not available here, so its
# signature could not be checked at all. Not in verdict rules v1, which
# assume the key is present (a bundle carries it); never shown as signed.
VERDICT_KEY_UNAVAILABLE = "key_unavailable"

_FAILURES_MAX_BYTES = 1_000_000


# ---------------------------------------------------------------------------
# Configuration and sealing points
# ---------------------------------------------------------------------------


def _config_turns_off(config: Path) -> bool:
    """``receipt_signing: false`` in *config*. An unreadable file never turns signing off."""
    if not config.is_file():
        return False
    import yaml

    try:
        loaded = yaml.safe_load(config.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return False
    return isinstance(loaded, dict) and loaded.get(CONFIG_KEY) is False


def signing_enabled(history_dir: Path, env: dict | None = None) -> bool:
    """On by default. Turned off by ``OPENSHARD_RECEIPT_SIGNING=off`` in the
    environment of the process that writes the record (the capture service
    included), or ``receipt_signing: false`` in the repository's
    ``.openshard/config.yml`` (*history_dir*) or the user-global
    ``~/.openshard/config.yml``."""
    environ = os.environ if env is None else env
    value = environ.get(DISABLE_ENV)
    if isinstance(value, str) and value.strip().lower() in _OFF_VALUES:
        return False
    if _config_turns_off(Path(history_dir) / "config.yml"):
        return False
    return not _config_turns_off(Path(openshard_home(environ)) / "config.yml")


def _seconds_since(stamp: object, now: datetime.datetime) -> float | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        then = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.UTC)
    return (now - then).total_seconds()


def seal_point(entry: dict, *, now: datetime.datetime | None = None) -> str | None:
    """The seal reason for a record that has stopped changing, else ``None``.

    Mirrors ``sync.envelope.eligibility`` exactly (parity-tested).
    """
    if stored_receipt_id(entry) is None:
        return None
    capture = entry.get("capture")
    if not isinstance(capture, dict) or "session_end_observed" not in capture:
        return SEAL_CREATED
    if capture.get("session_end_observed") is True or capture.get("status") == "ended":
        return SEAL_SESSION_ENDED
    if capture.get("receipt_scope") == "turn" and isinstance(capture.get("turn_sealed_at"), str):
        return SEAL_TURN_COMPLETED
    completeness = capture.get("completeness")
    reasons = completeness.get("reasons", []) if isinstance(completeness, dict) else []
    if any(isinstance(r, dict) and r.get("kind") == "session_end_not_observed" for r in reasons):
        return SEAL_IDLE_SWEPT
    age = _seconds_since(capture.get("last_activity_at"), now or datetime.datetime.now(datetime.UTC))
    if age is None or age >= QUIESCENT_SECONDS:
        return SEAL_IDLE_SWEPT
    return None


# ---------------------------------------------------------------------------
# Digests, Statement and DSSE envelope
# ---------------------------------------------------------------------------


def pae(payload_type: str, body: bytes) -> bytes:
    """DSSE v1 pre-authentication encoding (decimal byte lengths)."""
    kind = payload_type.encode("utf-8")
    return b"DSSEv1 %d %s %d %s" % (len(kind), kind, len(body), body)


def record_digest(stored_entry: dict) -> str:
    """sha256 hex of the record as stored: ``compute_shard_hash`` without its prefix."""
    return compute_shard_hash(stored_entry).split(":", 1)[1]


def receipt_projection(stored_entry: dict, index: int, attestations: list[dict]) -> dict:
    """The privacy-bounded ``history --json`` projection of one stored record.

    Built exactly as ``history.query`` builds a row: the record coerced the
    way the loader coerces it, at its history position, with its latest
    ``openshard verify`` attestation, through ``receipt_to_dict(extended=True)``.
    """
    from openshard.history.shard_contract import build_shard_receipt
    from openshard.history.shard_schema import coerce_shard_entry
    from openshard.history.views import receipt_to_dict
    from openshard.verification.post_session import latest_for_entry

    coerced = coerce_shard_entry(stored_entry, stamp_hash=False)
    coerced[INTEGRITY_AS_STORED_FIELD] = verify_shard_hash(stored_entry)["status"]
    receipt = build_shard_receipt(
        coerced, index=index, post_session_verification=latest_for_entry(coerced, attestations),
    )
    return receipt_to_dict(receipt, extended=True)


def projection_digest(projection: dict) -> str:
    return hashlib.sha256(_canonical_json(projection).encode("utf-8")).hexdigest()


def build_statement(
    *,
    receipt_id: str,
    shard_id: str | None,
    record_sha256: str,
    projection_sha256: str,
    seal_reason: str,
    previous_seal_digest: str | None,
    core_version: str,
    sealed_at: str | None = None,
) -> dict:
    covers, does_not_cover = COVERAGE[SEAL_VERSION]
    return {
        "_type": STATEMENT_TYPE,
        "subject": [
            {"name": f"record:{receipt_id}", "digest": {"sha256": record_sha256}},
            {"name": f"projection:{receipt_id}", "digest": {"sha256": projection_sha256}},
        ],
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "seal_version": SEAL_VERSION,
            "receipt_id": receipt_id,
            "shard_id": shard_id,
            "sealed_at": sealed_at or datetime.datetime.now(datetime.UTC).isoformat(),
            "seal_reason": seal_reason,
            "previous_record_digest": previous_seal_digest,
            "core_version": core_version,
            "covers": list(covers),
            "does_not_cover": list(does_not_cover),
        },
    }


def statement_bytes(statement: dict) -> bytes:
    """The Statement bytes a seal signs (DSSE signs them as they are; nothing re-canonicalises them)."""
    return json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_statement(statement: dict, key) -> dict:
    """DSSE envelope over the Statement bytes as serialised here."""
    body = statement_bytes(statement)
    signature = key.sign(pae(PAYLOAD_TYPE, body))
    return {
        "payloadType": PAYLOAD_TYPE,
        "payload": base64.b64encode(body).decode("ascii"),
        "signatures": [{"keyid": key.keyid, "sig": base64.b64encode(signature).decode("ascii")}],
    }


def is_envelope(line: object) -> bool:
    return isinstance(line, dict) and line.get("payloadType") == PAYLOAD_TYPE and "payload" in line


def envelope_payload(envelope: dict) -> tuple[bytes, dict] | None:
    """``(Statement bytes, Statement)`` of a well-formed envelope, else ``None``."""
    try:
        body = base64.b64decode(str(envelope.get("payload", "")), validate=True)
        statement = json.loads(body.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return (body, statement) if isinstance(statement, dict) else None


def seal_digest(envelope: dict) -> str | None:
    """sha256 hex of an envelope's Statement bytes: what the next seal links to."""
    decoded = envelope_payload(envelope)
    return hashlib.sha256(decoded[0]).hexdigest() if decoded else None


def _subject_digest(statement: dict, name: str) -> str | None:
    for subject in statement.get("subject") or []:
        if isinstance(subject, dict) and subject.get("name") == name:
            digest = subject.get("digest")
            return digest.get("sha256") if isinstance(digest, dict) else None
    return None


def _statement_receipt_id(statement: dict) -> str | None:
    predicate = statement.get("predicate")
    rid = predicate.get("receipt_id") if isinstance(predicate, dict) else None
    return rid if isinstance(rid, str) else None


# ---------------------------------------------------------------------------
# Data-level verification (the CLI is ``openshard verify --signature``, cli/signing_cmd.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignatureCheck:
    verdict: str
    reason: str
    keyid: str | None = None
    statement: dict | None = field(default=None, compare=False)


def _signature_ok(public_raw: bytes, signature_b64: object, message: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        signature = base64.b64decode(str(signature_b64), validate=True)
        Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, message)
    except (InvalidSignature, binascii.Error, ValueError):
        return False
    return True


def _statement_problem(statement: dict, receipt_id: str | None) -> str | None:
    predicate = statement.get("predicate")
    version = PREDICATE_VERSIONS.get(str(statement.get("predicateType")))
    if statement.get("_type") != STATEMENT_TYPE or version is None:
        return "unknown_statement_type"
    if not isinstance(predicate, dict) or predicate.get("seal_version") != version:
        return "unknown_seal_version"
    rid = _statement_receipt_id(statement)
    if rid is None or (receipt_id is not None and rid != receipt_id):
        return "receipt_id_mismatch"
    if version == v2.SEAL_VERSION and _subject_digest(statement, v2.fields_subject(rid)) is None:
        return "malformed_statement"
    return None


def verify_envelope(
    envelope: dict,
    *,
    public_keys: dict[str, bytes],
    trusted_keyids: frozenset[str] | set[str] = frozenset(),
    receipt_id: str | None = None,
    record_sha256: str | None = None,
    projection_sha256: str | None = None,
    sync_sha256: str | None = None,
) -> SignatureCheck:
    """Check one envelope under verdict rules v1. Never raises.

    ``record_sha256`` / ``projection_sha256`` / ``sync_sha256`` are the
    digests of the current content; a digest left ``None`` is not checked (a
    bundle without the record). ``sync_sha256`` is the JCS digest of a hosted
    receipt (seal v2 ``sync:`` subject); a seal without that subject never
    matches it. ``public_keys`` maps keyid -> raw public key.
    """
    decoded = envelope_payload(envelope) if is_envelope(envelope) else None
    if decoded is None:
        return SignatureCheck(VERDICT_INVALID, "malformed_envelope")
    body, statement = decoded
    signatures = [s for s in envelope.get("signatures") or [] if isinstance(s, dict)]
    usable = [s for s in signatures if s.get("keyid") in public_keys]
    if not usable:
        keyid = next((s.get("keyid") for s in signatures if isinstance(s.get("keyid"), str)), None)
        return SignatureCheck(VERDICT_KEY_UNAVAILABLE if keyid else VERDICT_INVALID,
                              "public_key_not_available" if keyid else "no_signature", keyid, statement)
    message = pae(str(envelope.get("payloadType")), body)
    good = next((s for s in usable if _signature_ok(public_keys[s["keyid"]], s.get("sig"), message)), None)
    if good is None:
        return SignatureCheck(VERDICT_INVALID, "bad_signature", usable[0]["keyid"], statement)
    keyid = good["keyid"]
    problem = _statement_problem(statement, receipt_id)
    if problem:
        return SignatureCheck(VERDICT_INVALID, problem, keyid, statement)
    rid = _statement_receipt_id(statement)
    if record_sha256 is not None and _subject_digest(statement, f"record:{rid}") != record_sha256:
        return SignatureCheck(VERDICT_INVALID, "record_changed", keyid, statement)
    if projection_sha256 is not None and _subject_digest(statement, f"projection:{rid}") != projection_sha256:
        return SignatureCheck(VERDICT_INVALID, "projection_changed", keyid, statement)
    if sync_sha256 is not None and _subject_digest(statement, v2.sync_subject(str(rid))) != sync_sha256:
        return SignatureCheck(VERDICT_INVALID, "synced_receipt_changed", keyid, statement)
    if keyid not in trusted_keyids:
        return SignatureCheck(VERDICT_UNKNOWN_KEY, "key_not_trusted", keyid, statement)
    return SignatureCheck(VERDICT_VERIFIED, "ok", keyid, statement)


def chain_problems(envelopes: list[dict]) -> list[str]:
    """Breaks in one Receipt's seal chain, oldest seal first. Empty when every
    seal links to the one before it and the first links to nothing."""
    problems: list[str] = []
    previous: str | None = None
    for position, envelope in enumerate(envelopes):
        decoded = envelope_payload(envelope)
        if decoded is None:
            problems.append(f"seal {position}: malformed")
            previous = None
            continue
        predicate = decoded[1].get("predicate") or {}
        if predicate.get("previous_record_digest") != previous:
            problems.append(f"seal {position}: does not link to the previous seal")
        previous = hashlib.sha256(decoded[0]).hexdigest()
    return problems


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


def sidecar_path(history_dir: Path) -> Path:
    return Path(history_dir) / SIDECAR_FILENAME


def _parse_lines(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    for raw in lines:
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue  # torn or corrupt line: skipped, never repaired
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def read_sidecar(history_dir: Path) -> list[dict]:
    """Every well-formed line of the sidecar (envelopes and not-signed notes). Never raises."""
    try:
        text = sidecar_path(history_dir).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return _parse_lines(text.splitlines())


def _line_receipt_id(line: dict) -> str | None:
    if line.get("_type") == NOT_SIGNED_TYPE:
        rid = line.get("receipt_id")
        return rid if isinstance(rid, str) else None
    decoded = envelope_payload(line) if is_envelope(line) else None
    return _statement_receipt_id(decoded[1]) if decoded else None


def seals_for(lines: list[dict], receipt_id: str) -> list[dict]:
    """Every envelope sealing *receipt_id*, oldest first."""
    return [ln for ln in lines if is_envelope(ln) and _line_receipt_id(ln) == receipt_id]


def _latest_line(lines: list[dict], receipt_id: str) -> dict | None:
    for line in reversed(lines):
        if _line_receipt_id(line) == receipt_id:
            return line
    return None


def _append_line(path: Path, line: dict) -> None:
    """Append one line; the caller holds the sidecar lock."""
    blob = json.dumps(line, sort_keys=True) + "\n"
    torn = False
    if path.exists() and path.stat().st_size:
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            torn = fh.read(1) != b"\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(("\n" if torn else "") + blob)
        fh.flush()
        os.fsync(fh.fileno())


def _not_signed_line(receipt_id: str, reason: str) -> dict:
    return {
        "_type": NOT_SIGNED_TYPE,
        "receipt_id": receipt_id,
        "reason": reason,
        "recorded_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }


def _record_failure_in_home(receipt_id: str, reason: str) -> None:
    """Last resort when the sidecar cannot be written. Never raises."""
    try:
        path = Path(openshard_home()) / FAILURES_FILENAME
        if path.exists() and path.stat().st_size > _FAILURES_MAX_BYTES:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_not_signed_line(receipt_id, reason), sort_keys=True) + "\n")
    except OSError:
        return


# ---------------------------------------------------------------------------
# Sealing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SealOutcome:
    """What one sealing attempt did. ``status``: ``sealed``, ``not_signed``
    (a reason was recorded) or ``skipped`` (nothing to do; not a failure)."""

    status: str
    reason: str
    receipt_id: str | None = None
    envelope: dict | None = field(default=None, compare=False)


def _core_version() -> str:
    try:
        from openshard import __version__

        return str(__version__)
    except (ImportError, AttributeError):
        return "unknown"


def _find(stored: list[dict], receipt_id: str) -> tuple[int, dict] | None:
    for index in range(len(stored) - 1, -1, -1):
        if stored_receipt_id(stored[index]) == receipt_id:
            return index, stored[index]
    return None


def _note_not_signed(history_dir: Path, receipt_id: str, reason: str) -> SealOutcome:
    """Record "not signed" + *reason* for a caller that does not hold the sidecar lock."""
    from openshard.history.jsonl_store import history_file_lock

    path = sidecar_path(history_dir)
    try:
        with history_file_lock(path, timeout=10.0):
            _append_not_signed(history_dir, receipt_id, reason)
    except (OSError, TimeoutError):
        _record_failure_in_home(receipt_id, reason)
    return SealOutcome("not_signed", reason, receipt_id)


def _append_not_signed(history_dir: Path, receipt_id: str, reason: str) -> SealOutcome:
    """Append a "not signed" line (caller holds the sidecar lock), unless the
    latest line for this Receipt already says the same thing."""
    latest = _latest_line(read_sidecar(history_dir), receipt_id)
    if not (latest and latest.get("_type") == NOT_SIGNED_TYPE and latest.get("reason") == reason):
        _append_line(sidecar_path(history_dir), _not_signed_line(receipt_id, reason))
    return SealOutcome("not_signed", reason, receipt_id)


def _seal_one(history_dir: Path, runs_path: Path, receipt_id: str, *, amendment: bool) -> SealOutcome:
    """Seal the current stored version of *receipt_id*.

    Everything is decided under the sidecar lock from a fresh read of
    ``runs.jsonl``, so two processes sealing the same Receipt (the capture
    service and a CLI amendment) can never append a seal over a stale
    version after a seal over the newer one.
    """
    from openshard.history.jsonl_store import history_file_lock
    from openshard.history.store import load_history

    path = sidecar_path(history_dir)
    with history_file_lock(path, timeout=10.0):
        stored = load_history(runs_path, coerce=False)
        found = _find(stored, receipt_id)
        if found is None:
            return SealOutcome("skipped", "record_not_found", receipt_id)
        index, entry = found
        point = seal_point(entry)
        if point is None:
            return SealOutcome("skipped", "record_still_changing", receipt_id)
        integrity = verify_shard_hash(entry)["status"]
        if integrity == "missing":
            return SealOutcome("skipped", "no_content_hash", receipt_id)
        previous = seals_for(read_sidecar(history_dir), receipt_id)
        if amendment and not previous:
            return SealOutcome("skipped", "never_sealed", receipt_id)
        if integrity != "valid":
            return _append_not_signed(history_dir, receipt_id, "content_hash_mismatch")
        record_sha = record_digest(entry)
        projection_sha = projection_digest(receipt_projection(entry, index, _attestations(history_dir)))
        last = envelope_payload(previous[-1]) if previous else None
        if last is not None and (
            _subject_digest(last[1], f"record:{receipt_id}") == record_sha
            and _subject_digest(last[1], f"projection:{receipt_id}") == projection_sha
        ):
            return SealOutcome("skipped", "already_sealed", receipt_id)
        return _write_seal(history_dir, receipt_id, entry, record_sha, projection_sha,
                           reason=SEAL_AMENDED if previous else point, previous=previous)


def _write_seal(history_dir: Path, receipt_id: str, entry: dict, record_sha: str, projection_sha: str, *,
                reason: str, previous: list[dict], sync_sha256: str | None = None) -> SealOutcome:
    """Sign and append one v2 seal (docs/receipt-seal-v2.md); the caller holds the sidecar lock.

    The salts line is written first, so a seal never exists without the
    salts its field commitments need for a later reveal.
    """
    try:
        key = load_or_create_key()
    except KeyUnavailableError as exc:
        return _append_not_signed(history_dir, receipt_id, exc.reason)
    salts, commitments, uncommitted = v2.commit_fields(receipt_id, entry)
    statement = v2.build_statement(
        receipt_id=receipt_id,
        shard_id=entry.get("shard_id") if isinstance(entry.get("shard_id"), str) else None,
        record_sha256=record_sha,
        projection_sha256=projection_sha,
        fields_sha256=v2.fields_digest(commitments),
        uncommitted=uncommitted,
        seal_reason=reason,
        previous_seal_digest=seal_digest(previous[-1]) if previous else None,
        core_version=_core_version(),
        sealed_at=datetime.datetime.now(datetime.UTC).isoformat(),
        sync_sha256=sync_sha256,
    )
    v2.append_salts(history_dir, receipt_id=receipt_id,
                    statement_sha256=hashlib.sha256(statement_bytes(statement)).hexdigest(),
                    salts=salts, commitments=commitments)
    envelope = sign_statement(statement, key)
    _append_line(sidecar_path(history_dir), envelope)
    return SealOutcome("sealed", reason, receipt_id, envelope)


def _seal_for_sync(history_dir: Path, receipt_id: str, record_hash: str, sync_sha256: str) -> SealOutcome:
    from openshard.history.jsonl_store import history_file_lock
    from openshard.history.store import load_history

    with history_file_lock(sidecar_path(history_dir), timeout=10.0):
        found = _find(load_history(history_dir / "runs.jsonl", coerce=False), receipt_id)
        if found is None:
            return SealOutcome("skipped", "record_not_found", receipt_id)
        index, entry = found
        if entry.get("content_hash") != record_hash:
            return SealOutcome("skipped", "record_changed_since_discovery", receipt_id)
        lines = read_sidecar(history_dir)
        previous = seals_for(lines, receipt_id)
        if not previous:
            return SealOutcome("skipped", "never_sealed", receipt_id)
        if not is_envelope(_latest_line(lines, receipt_id)):
            return SealOutcome("skipped", "latest_change_not_signed", receipt_id)
        if verify_shard_hash(entry)["status"] != "valid":
            return _append_not_signed(history_dir, receipt_id, "content_hash_mismatch")
        record_sha = record_digest(entry)
        projection_sha = projection_digest(receipt_projection(entry, index, _attestations(history_dir)))
        last = envelope_payload(previous[-1])
        if last is not None and all(
            _subject_digest(last[1], name) == digest for name, digest in (
                (f"record:{receipt_id}", record_sha), (f"projection:{receipt_id}", projection_sha),
                (v2.sync_subject(receipt_id), sync_sha256))):
            return SealOutcome("skipped", "already_sealed", receipt_id, previous[-1])
        return _write_seal(history_dir, receipt_id, entry, record_sha, projection_sha,
                           reason=v2.SEAL_SYNCED, previous=previous, sync_sha256=sync_sha256)


def seal_for_sync(history_dir: Path, receipt_id: str, *, record_hash: str, sync_sha256: str) -> SealOutcome:
    """The seal that covers the exact receipt sync is about to send (seal v2).

    *sync_sha256* is ``receipt_seal_v2.sync_digest`` of that receipt and
    *record_hash* the stored ``content_hash`` it was built from. Returns the
    latest seal when it already names this receipt, else appends a chained
    ``synced`` seal. A Receipt that was never sealed, whose latest change was
    not signed, or that changed since sync read it gets no seal
    (``envelope`` is ``None``): it is sent unsigned, never sealed after the
    fact. Never raises.
    """
    history_dir = Path(history_dir)
    if not signing_enabled(history_dir):
        return SealOutcome("skipped", "signing_disabled", receipt_id)
    return _guarded(history_dir, receipt_id, partial(_seal_for_sync, history_dir, receipt_id, record_hash, sync_sha256))


def _guarded(history_dir: Path, receipt_id: str, attempt: Callable[[], SealOutcome]) -> SealOutcome:
    """Run one sealing attempt; any failure becomes a recorded "not signed"."""
    from openshard.history.jsonl_store import LockTimeoutError

    try:
        return attempt()
    except LockTimeoutError:
        _record_failure_in_home(receipt_id, "sidecar_lock_timeout")
        return SealOutcome("not_signed", "sidecar_lock_timeout", receipt_id)
    except OSError:
        _record_failure_in_home(receipt_id, "sidecar_unwritable")
        return SealOutcome("not_signed", "sidecar_unwritable", receipt_id)
    except Exception as exc:  # sealing must never fail the write it follows
        return _note_not_signed(history_dir, receipt_id, f"seal_failed:{type(exc).__name__}")


def _attestations(history_dir: Path) -> list[dict]:
    from openshard.verification.post_session import load_attestations

    return load_attestations(history_dir)


def _disabled(history_dir: Path, receipt_id: str) -> SealOutcome:
    """Signing is off. A Receipt sealed earlier gets a "not signed" line so its
    latest state reads as unsigned rather than as changed after sealing."""
    if seals_for(read_sidecar(history_dir), receipt_id):
        return _note_not_signed(history_dir, receipt_id, "signing_disabled")
    return SealOutcome("skipped", "signing_disabled", receipt_id)


def seal_after_write(runs_path: Path, written: dict, *, amendment: bool = False) -> SealOutcome:
    """Seal the record just written to *runs_path*, if it has stopped changing.

    Called by ``jsonl_store`` after every write to a ``runs.jsonl``. Cheap
    when there is nothing to do (no receipt id, a live session). Never raises.
    """
    receipt_id = stored_receipt_id(written) if isinstance(written, dict) else None
    if receipt_id is None or seal_point(written) is None:
        return SealOutcome("skipped", "record_still_changing" if receipt_id else "no_receipt_id", receipt_id)
    history_dir = Path(runs_path).parent
    if not signing_enabled(history_dir):
        return _disabled(history_dir, receipt_id)

    return _guarded(history_dir, receipt_id, lambda: _seal_one(
        history_dir, Path(runs_path), receipt_id, amendment=amendment))


def reseal_after_attestation(history_dir: Path, attestation: dict) -> list[SealOutcome]:
    """Re-seal every sealed record a new ``openshard verify`` attestation names.

    An attestation never changes the record. A new seal is written only when
    it changes the sealed projection (same record digest, new projection
    digest); today's ``history --json`` projection does not carry
    attestations, so the seal chain is then left as it is. The attestation
    file itself is not covered by any seal. Never raises.
    """
    history_dir = Path(history_dir)
    if not signing_enabled(history_dir):
        rid = attestation.get("receipt_id") if isinstance(attestation, dict) else None
        return [_disabled(history_dir, rid)] if isinstance(rid, str) and rid else []
    outcomes: list[SealOutcome] = []
    try:
        from openshard.history.store import load_history
        from openshard.verification.post_session import evidence_for_entry

        stored = load_history(history_dir / "runs.jsonl", coerce=False)
        named = [rid for e in stored if (rid := stored_receipt_id(e)) and evidence_for_entry(e, [attestation])]
    except Exception:  # an unreadable history never fails the attestation write
        return outcomes
    for rid in dict.fromkeys(named):
        outcomes.append(_guarded(history_dir, rid, partial(
            _seal_one, history_dir, history_dir / "runs.jsonl", rid, amendment=True)))
    return outcomes


# ---------------------------------------------------------------------------
# Status for the receipt view
# ---------------------------------------------------------------------------


def signature_status(runs_path: Path, stored_entry: dict, index: int) -> SignatureCheck:
    """The current seal verdict for one stored record: the whole seal chain,
    checked against the keys trusted here (``receipt_verify.verify_local``,
    the same check ``openshard verify --signature`` runs). Never raises;
    never creates a key."""
    from openshard.history.receipt_verify import verify_local

    try:
        result = verify_local(runs_path, stored_entry, index)
    except Exception:  # a view must render even when the check cannot run
        return SignatureCheck(VERDICT_KEY_UNAVAILABLE, "check_failed")
    unchecked = result.verdict == VERDICT_UNKNOWN_KEY and not result.signature_checked
    return SignatureCheck(VERDICT_KEY_UNAVAILABLE if unchecked else result.verdict, result.reason, result.keyid)


def signature_label(check: SignatureCheck) -> str:
    """The receipt view's wording (docs/signing.md)."""
    fp = fingerprint8(check.keyid) if check.keyid else ""
    if check.verdict == VERDICT_VERIFIED:
        return f"Signed · key {fp}"
    if check.verdict == VERDICT_INVALID:
        return f"Invalid ({_INVALID_WORDS.get(check.reason, check.reason.replace('_', ' '))})"
    if check.verdict == VERDICT_UNKNOWN_KEY:
        return "Signed · key not registered"
    if check.verdict == VERDICT_KEY_UNAVAILABLE:
        return f"Seal by key {fp} not checked (public key not available here)" if fp else "Seal not checked"
    if check.reason in ("content_hash_missing", "content_hash_mismatch"):
        return "Not signed"
    return "Checksum only (not signed)"


_INVALID_WORDS = {
    "record_changed": "record changed after it was sealed",
    "projection_changed": "receipt changed after it was sealed",
    "bad_signature": "seal does not verify",
    "chain_broken": "seal chain broken",
    "key_revoked": "key revoked before sealing",
    "earlier_seal_bad_signature": "an earlier seal does not verify",
    "synced_receipt_changed": "hosted receipt differs from the sealed copy",
}


def signature_line(runs_path: Path, stored_entry: dict, index: int) -> str:
    return f"Signature  {signature_label(signature_status(runs_path, stored_entry, index))}"


__all__ = [
    "COVERAGE",
    "DISABLE_ENV",
    "SEAL_VERSION",
    "SIDECAR_FILENAME",
    "VERDICT_RULES_VERSION",
    "SealOutcome",
    "SignatureCheck",
    "build_statement",
    "chain_problems",
    "pae",
    "read_sidecar",
    "receipt_projection",
    "reseal_after_attestation",
    "seal_after_write",
    "seal_for_sync",
    "seal_point",
    "seals_for",
    "sign_statement",
    "signature_label",
    "statement_bytes",
    "signature_line",
    "signature_status",
    "signing_enabled",
    "verify_envelope",
]
