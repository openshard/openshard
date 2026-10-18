"""Offline checks of Receipt seals, and shareable bundles (docs/signing.md).

``openshard verify --signature`` runs nothing. It reads the seals of one
Receipt (from this repository's sidecar, or from a bundle someone sent) and
returns exactly one verdict under verdict rules v1, first match wins:

* ``invalid``            a seal does not verify, the latest seal's digest
                         differs from the current record or projection, the
                         seal chain is broken, or a key was revoked before
                         the seal's (self-asserted) ``sealed_at``;
* ``not_signed``         no seal, or the latest change was recorded as not
                         signed (shown as "Checksum only (not signed)",
                         never as failed);
* ``signed_unknown_key`` every seal verifies against a key that is not
                         trusted here, or a seal names a key whose public half
                         is not available (then its signature was not checked);
* ``verified``           every seal verifies, the chain links, the latest
                         seal matches the content, every key is trusted.

A verdict binds only what the Statement names: the record digest, the
projection digest, the receipt and shard ids, and for seal v2
(docs/receipt-seal-v2.md) the record field commitments and, on a seal made
by sync, the digest of the hosted receipt. ``sealed_at``, the seal
reason and the Core version are signed but self-asserted. Nothing here says
whether the agent's claims are true, whether checks passed, or whether
capture was complete; and a coding agent running as the same OS user can
read the key and seal a forged record.
"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from pathlib import Path

from openshard.history import receipt_keys as rk
from openshard.history import receipt_seal_v2 as v2
from openshard.history import receipt_signing as rs
from openshard.history.receipt_identity import stored_receipt_id
from openshard.history.shard_hash import verify_shard_hash

BUNDLE_MEDIA_TYPE = "application/vnd.openshard.receipt-bundle+json"
BUNDLE_VERSION = "1"

VERDICTS = (rs.VERDICT_VERIFIED, rs.VERDICT_NOT_SIGNED, rs.VERDICT_UNKNOWN_KEY, rs.VERDICT_INVALID)

# Exit codes (``--strict`` turns the two weaker verdicts into 2).
EXIT_CODES = {rs.VERDICT_VERIFIED: 0, rs.VERDICT_NOT_SIGNED: 0, rs.VERDICT_UNKNOWN_KEY: 0, rs.VERDICT_INVALID: 1}
STRICT_EXIT_CODES = {**EXIT_CODES, rs.VERDICT_NOT_SIGNED: 2, rs.VERDICT_UNKNOWN_KEY: 2}

BOUND = ("record digest", "projection digest", "receipt id", "shard id")
BOUND_V2 = ("record field commitments",)
BOUND_SYNC = ("hosted receipt digest",)
CLAIMED = ("sealed_at (local clock)", "seal reason", "Core version")


class BundleError(ValueError):
    """A bundle file that is not a readable OpenShard receipt bundle."""


@dataclass
class SignatureVerdict:
    """The outcome of checking one Receipt's seals. ``to_dict`` is the ``--json`` shape."""

    verdict: str
    reason: str
    receipt_id: str | None
    source: str  # "local" or "bundle"
    signature_checked: bool = False
    keyid: str | None = None
    key_source: str | None = None  # receipt_keys.SOURCE_* or "bundle_only"
    sealed_at: str | None = None
    seal_reason: str | None = None
    core_version: str | None = None
    record_check: str = "not_checked"  # matches | changed | not_provided | not_checked
    projection_check: str = "not_checked"
    sealed_record_sha256: str | None = None
    current_record_sha256: str | None = None
    sealed_projection_sha256: str | None = None
    current_projection_sha256: str | None = None
    content_hash_status: str | None = None
    previously_sealed: bool = False
    revoked_at: str | None = None
    chain: list[dict] = field(default_factory=list)
    chain_problems: list[str] = field(default_factory=list)
    seal_version: str | None = None
    sync_sealed: bool = False  # the latest seal names the hosted receipt (seal v2 ``sync:`` subject)
    reveals: list[dict] = field(default_factory=list)  # [{"field", "check"}], bundles only

    def to_dict(self) -> dict:
        body = asdict(self)
        body["verdict_rules"] = rs.VERDICT_RULES_VERSION
        bound = list(BOUND)
        if self.seal_version == v2.SEAL_VERSION:
            bound += list(BOUND_V2) + (list(BOUND_SYNC) if self.sync_sealed else [])
        body["bound_by_signature"] = bound if self.signature_checked else []
        body["claimed_not_proven"] = list(CLAIMED) if self.signature_checked else []
        body["not_proven"] = ["agent_claims", "verification_outcome", "capture_completeness"]
        return body


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _parse_time(stamp: object) -> datetime.datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


def _revoked_before(sealed_at: object, revoked_at: str | None) -> bool:
    if not revoked_at:
        return False
    sealed, revoked = _parse_time(sealed_at), _parse_time(revoked_at)
    # An unreadable sealed_at cannot show the seal predates the revocation.
    return sealed is None or revoked is None or sealed >= revoked


def _predicate(check: rs.SignatureCheck) -> dict:
    predicate = (check.statement or {}).get("predicate")
    return predicate if isinstance(predicate, dict) else {}


def evaluate(
    lines: list[dict],
    *,
    receipt_id: str | None,
    source: str,
    ring: dict[str, rk.PublicKey],
    extra_keys: dict[str, bytes] | None = None,
    record: dict | None = None,
    projection: dict | None = None,
) -> SignatureVerdict:
    """Apply verdict rules v1 to one Receipt's sidecar lines (oldest first).

    *ring* holds the keys trusted here; *extra_keys* public keys that only
    came with a bundle (they let a signature be checked, never trusted).
    Never raises on malformed input.
    """
    out = SignatureVerdict(rs.VERDICT_NOT_SIGNED, "no_seal", receipt_id, source)
    if record is not None:
        out.content_hash_status = verify_shard_hash(record)["status"]
        out.current_record_sha256 = rs.record_digest(record)
    else:
        out.record_check = "not_provided"
    if projection is not None:
        out.current_projection_sha256 = rs.projection_digest(projection)
    else:
        out.projection_check = "not_provided"
    seals = [ln for ln in lines if rs.is_envelope(ln)]
    latest = lines[-1] if lines else None
    if not seals:
        if latest is not None and latest.get("_type") == rs.NOT_SIGNED_TYPE:
            out.reason = str(latest.get("reason") or "not_signed")
        elif out.content_hash_status not in (None, "valid"):
            out.reason = f"content_hash_{out.content_hash_status}"
        return out

    public = {k: v.public_raw for k, v in ring.items()}
    for keyid, raw in (extra_keys or {}).items():
        public.setdefault(keyid, raw)
    trusted = set(ring)
    checks: list[rs.SignatureCheck] = []
    for position, envelope in enumerate(seals):
        is_latest = position == len(seals) - 1 and latest is envelope
        checks.append(rs.verify_envelope(
            envelope, public_keys=public, trusted_keyids=trusted, receipt_id=receipt_id,
            record_sha256=out.current_record_sha256 if is_latest else None,
            projection_sha256=out.current_projection_sha256 if is_latest else None,
        ))
    for position, check in enumerate(checks):
        predicate = _predicate(check)
        key = ring.get(check.keyid or "")
        out.chain.append({
            "position": position, "keyid": check.keyid,
            "key_source": key.source if key else ("bundle_only" if check.keyid in public else None),
            "sealed_at": predicate.get("sealed_at"), "seal_reason": predicate.get("seal_reason"),
            "verdict": check.verdict, "reason": check.reason,
        })
    out.chain_problems = rs.chain_problems(seals)
    last = checks[-1]
    predicate = _predicate(last)
    out.keyid, out.sealed_at = last.keyid, predicate.get("sealed_at")
    out.seal_reason, out.core_version = predicate.get("seal_reason"), predicate.get("core_version")
    out.key_source = out.chain[-1]["key_source"]
    statement = last.statement or {}
    rid = receipt_id or rs._statement_receipt_id(statement)
    out.sealed_record_sha256 = rs._subject_digest(statement, f"record:{rid}")
    out.sealed_projection_sha256 = rs._subject_digest(statement, f"projection:{rid}")
    out.seal_version = str(predicate.get("seal_version")) if predicate.get("seal_version") else None
    out.sync_sealed = rs._subject_digest(statement, v2.sync_subject(str(rid))) is not None
    out.signature_checked = all(c.verdict != rs.VERDICT_KEY_UNAVAILABLE for c in checks)
    _decide(out, checks, latest_is_seal=latest is seals[-1], latest=latest, ring=ring)
    return out


def _decide(out: SignatureVerdict, checks: list[rs.SignatureCheck], *, latest_is_seal: bool,
            latest: dict | None, ring: dict[str, rk.PublicKey]) -> None:
    last = checks[-1]
    if latest_is_seal:
        # Digests are compared only once the signature verified (verify_envelope's order).
        signed_ok = last.verdict in (rs.VERDICT_VERIFIED, rs.VERDICT_UNKNOWN_KEY) or last.reason in (
            "record_changed", "projection_changed")
        if out.current_record_sha256 is not None and signed_ok:
            out.record_check = "changed" if last.reason == "record_changed" else "matches"
        if out.current_projection_sha256 is not None and signed_ok and last.reason != "record_changed":
            out.projection_check = "changed" if last.reason == "projection_changed" else "matches"
    if latest_is_seal and last.verdict == rs.VERDICT_INVALID:
        out.verdict, out.reason = rs.VERDICT_INVALID, last.reason
        return
    earlier_bad = next((c for c in checks if c.verdict == rs.VERDICT_INVALID), None)
    if earlier_bad is not None:
        out.verdict, out.reason = rs.VERDICT_INVALID, f"earlier_seal_{earlier_bad.reason}"
        return
    if out.chain_problems:
        out.verdict, out.reason = rs.VERDICT_INVALID, "chain_broken"
        return
    for check in checks:
        key = ring.get(check.keyid or "")
        if key and _revoked_before(_predicate(check).get("sealed_at"), key.revoked_at):
            out.verdict, out.reason, out.revoked_at = rs.VERDICT_INVALID, "key_revoked", key.revoked_at
            return
    if not latest_is_seal:
        out.verdict, out.previously_sealed = rs.VERDICT_NOT_SIGNED, True
        out.reason = str((latest or {}).get("reason") or "not_signed")
        return
    if not out.signature_checked:
        out.verdict, out.reason = rs.VERDICT_UNKNOWN_KEY, "public_key_not_available"
        return
    if any(c.verdict == rs.VERDICT_UNKNOWN_KEY for c in checks):
        out.verdict, out.reason = rs.VERDICT_UNKNOWN_KEY, "key_not_trusted"
        return
    revoked = next((ring[c.keyid].revoked_at for c in checks if c.keyid in ring and ring[c.keyid].revoked_at), None)
    out.verdict, out.reason, out.revoked_at = rs.VERDICT_VERIFIED, "ok", revoked


# ---------------------------------------------------------------------------
# Local Receipts
# ---------------------------------------------------------------------------


def _lines_for(history_dir: Path, receipt_id: str) -> list[dict]:
    return [ln for ln in rs.read_sidecar(history_dir) if rs._line_receipt_id(ln) == receipt_id]


def verify_local(runs_path: Path, stored_entry: dict, index: int) -> SignatureVerdict:
    """Check one stored record against this repository's sidecar and the keys
    trusted here. Never creates a key."""
    history_dir = Path(runs_path).parent
    receipt_id = stored_receipt_id(stored_entry)
    if receipt_id is None:
        # "Checksum only" is said only of a record whose checksum is valid.
        integrity = verify_shard_hash(stored_entry)["status"]
        reason = "no_receipt_id" if integrity == "valid" else f"content_hash_{integrity}"
        return SignatureVerdict(rs.VERDICT_NOT_SIGNED, reason, None, "local", content_hash_status=integrity)
    projection = rs.receipt_projection(stored_entry, index, rs._attestations(history_dir))
    return evaluate(_lines_for(history_dir, receipt_id), receipt_id=receipt_id, source="local",
                    ring=rk.keyring(), record=stored_entry, projection=projection)


def status_label(result: SignatureVerdict) -> str:
    """The receipt view's one-line wording (``openshard last`` Signature row)."""
    unchecked = result.verdict == rs.VERDICT_UNKNOWN_KEY and not result.signature_checked
    verdict = rs.VERDICT_KEY_UNAVAILABLE if unchecked else result.verdict
    return rs.signature_label(rs.SignatureCheck(verdict, result.reason, result.keyid))


def signature_status(runs_path: Path, stored_entry: dict, index: int) -> dict:
    """``signature_status`` for ``last --json`` and ``history --json`` rows. Never raises."""
    try:
        result = verify_local(runs_path, stored_entry, index)
    except Exception:  # a view must render even when the check cannot run
        return {"verdict": None, "reason": "check_failed", "label": "Seal not checked", "keyid": None,
                "signature_checked": False, "verdict_rules": rs.VERDICT_RULES_VERSION}
    return {"verdict": result.verdict, "reason": result.reason, "label": status_label(result),
            "keyid": result.keyid, "signature_checked": result.signature_checked,
            "verdict_rules": rs.VERDICT_RULES_VERSION}


def attach_signature_status(rows: list[dict], runs_path: Path) -> None:
    """Add ``signature_status`` to each ``history --json`` row, from one read of the history."""
    from openshard.history.store import load_history

    try:
        stored = load_history(runs_path, coerce=False)
    except Exception:
        stored = []
    latest = {rid: (i, e) for i, e in enumerate(stored) if (rid := stored_receipt_id(e))}
    for row in rows:
        found = latest.get(row.get("receipt_id") or "")
        row["signature_status"] = signature_status(runs_path, found[1], found[0]) if found else None


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------


def build_bundle(runs_path: Path, stored_entry: dict, index: int, *, include_record: bool,
                 reveal: tuple[str, ...] = ()) -> dict:
    """A file a third party can check offline: this Receipt's sidecar lines, the
    public keys of its seals and the projection (the ``history --json`` row).
    The projection is sealed whole, so it cannot be trimmed: it carries the
    task text, file names, repository and branch. The full record only with
    *include_record*; one stored field, with its salt, for each name in
    *reveal* (seal v2), checked against the latest seal's commitments.
    Raises :class:`BundleError` when a field cannot be revealed."""
    history_dir = Path(runs_path).parent
    receipt_id = stored_receipt_id(stored_entry)
    if receipt_id is None:
        raise BundleError("this record has no receipt id")
    lines = _lines_for(history_dir, receipt_id)
    ring = rk.keyring()
    keyids: set[str] = {s["keyid"] for ln in lines if rs.is_envelope(ln) for s in ln.get("signatures") or []
                        if isinstance(s, dict) and isinstance(s.get("keyid"), str)}
    bundle: dict = {
        "mediaType": BUNDLE_MEDIA_TYPE,
        "bundle_version": BUNDLE_VERSION,
        "receipt_id": receipt_id,
        "exported_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "core_version": rs._core_version(),
        "projection": rs.receipt_projection(stored_entry, index, rs._attestations(history_dir)),
        "seals": lines,
        "public_keys": [rk.public_key_record(k, ring[k].public_raw) for k in sorted(keyids & set(ring))],
    }
    if include_record:
        bundle["record"] = stored_entry
    if reveal:
        latest = lines[-1] if lines else None
        decoded = rs.envelope_payload(latest) if latest is not None and rs.is_envelope(latest) else None
        if decoded is None:
            raise BundleError("the latest change to this receipt is not sealed; nothing can be revealed")
        try:
            bundle["reveals"] = [v2.build_reveal(history_dir, statement_bytes=decoded[0], receipt_id=receipt_id,
                                                 entry=stored_entry, name=name) for name in dict.fromkeys(reveal)]
        except v2.RevealError as exc:
            raise BundleError(str(exc)) from exc
    return bundle


def _check_reveals(result: SignatureVerdict, reveals: object, seals: list[dict]) -> None:
    """Check a bundle's reveals against its latest seal. A reveal that does
    not match what was sealed makes the bundle ``invalid``; reveals are never
    checked against a seal whose signature was not."""
    if not isinstance(reveals, list) or not reveals:
        return
    latest = seals[-1] if seals else None
    decoded = rs.envelope_payload(latest) if latest is not None and rs.is_envelope(latest) else None
    for item in reveals:
        name = item.get("field") if isinstance(item, dict) else None
        check = ("seal_not_checked" if decoded is None or not result.signature_checked
                 else v2.check_reveal(item, statement=decoded[1], statement_bytes=decoded[0]))
        result.reveals.append({"field": name if isinstance(name, str) else None, "check": check})
    if result.verdict != rs.VERDICT_INVALID and any(r["check"] != "matches" for r in result.reveals):
        result.verdict, result.reason = rs.VERDICT_INVALID, "reveal_mismatch"


def verify_bundle(bundle: object) -> SignatureVerdict:
    """Check a bundle offline against the keys trusted here. Keys carried in the
    bundle let a signature be checked but never make it ``verified``.
    Raises :class:`BundleError` when *bundle* is not a receipt bundle."""
    if not isinstance(bundle, dict) or bundle.get("mediaType") != BUNDLE_MEDIA_TYPE:
        raise BundleError("not an OpenShard receipt bundle")
    if bundle.get("bundle_version") != BUNDLE_VERSION:
        raise BundleError(f"unsupported bundle version {bundle.get('bundle_version')!r}")
    receipt_id, projection = bundle.get("receipt_id"), bundle.get("projection")
    seals, record = bundle.get("seals"), bundle.get("record")
    if not isinstance(receipt_id, str) or not isinstance(projection, dict) or not isinstance(seals, list):
        raise BundleError("bundle is missing receipt_id, projection or seals")
    extra: dict[str, bytes] = {}
    for item in bundle.get("public_keys") or []:
        parsed = rk.parse_public_key(item)  # a keyid that does not match its bytes is ignored
        if parsed:
            extra[parsed[0]] = parsed[1]
    result = evaluate([s for s in seals if isinstance(s, dict)], receipt_id=receipt_id, source="bundle",
                      ring=rk.keyring(), extra_keys=extra,
                      record=record if isinstance(record, dict) else None, projection=projection)
    inner = {projection.get("receipt_id"), stored_receipt_id(record) if isinstance(record, dict) else receipt_id}
    if inner - {receipt_id, None}:
        result.verdict, result.reason = rs.VERDICT_INVALID, "receipt_id_mismatch"
    _check_reveals(result, bundle.get("reveals"), [s for s in seals if isinstance(s, dict)])
    return result


__all__ = [
    "BUNDLE_MEDIA_TYPE",
    "BUNDLE_VERSION",
    "EXIT_CODES",
    "STRICT_EXIT_CODES",
    "VERDICTS",
    "BundleError",
    "SignatureVerdict",
    "attach_signature_status",
    "build_bundle",
    "evaluate",
    "signature_status",
    "status_label",
    "verify_bundle",
    "verify_local",
]
