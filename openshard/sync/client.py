"""Discover unsynced receipts and send them, safely, as often as you like.

``flush(root)`` is idempotent and retry-safe by construction:

* Pending work is derived from ``runs.jsonl`` against the outbox, never
  queued, so nothing can be lost or duplicated by a crash mid-flush.
* The Platform keys on ``receipt_id`` and answers ``duplicate`` for a
  replay, so sending the same receipt twice is harmless.
* A conflict or rejection is recorded once and never retried; an
  unreachable Platform stops the flush and starts an exponential backoff;
  an invalid key pauses the link until ``openshard sync connect`` runs again.
* An open agent session is left alone until it ends or goes quiet
  (``envelope.eligibility``).
* A hosted Receipt is never resent. Verification evidence recorded after it
  synced (``openshard verify``, ``openshard verify --ci``) is sent on its own
  route, once per distinct evidence set, and only for a Receipt whose hosted
  copy is still the local one (``sync/evidence.py``).

Callers: ``openshard sync now`` (one repository, foreground), the capture
service's timer (every known repository, background), and tests through
``configure(transport=...)``. Nothing here is on an agent's hook path.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.history.locate import HISTORY_RELPATH
from openshard.history.receipt_identity import stored_receipt_id
from openshard.history.shard_hash import stored_shard_hash
from openshard.history.store import load_history
from openshard.sync import outbox as _outbox
from openshard.sync import transport as _transport
from openshard.sync.config import PlatformLink, resolve_link, sync_disabled
from openshard.sync.envelope import (
    QUIESCENT_SECONDS,
    REASON_SESSION_IN_PROGRESS,
    REASON_SESSION_QUIESCENT,
    Eligibility,
    build_envelope,
    eligibility,
    payload_hash,
)

DEFAULT_FLUSH_LIMIT = 50
SYNC_INTERVAL_SECONDS = 300.0

_lock = threading.Lock()
_transport_override: _transport.PlatformTransport | None = None
_repo_config_override: dict | None = None


def configure(*, transport: _transport.PlatformTransport | None = None, repo_config: dict | None = None) -> None:
    """Inject a transport / repository config for this process (tests, embedding)."""
    global _transport_override, _repo_config_override
    with _lock:
        _transport_override = transport
        _repo_config_override = repo_config


def _advanced_osn_enabled() -> bool:
    """Whether this organisation's ``advanced_osn`` capability is on (one cached read; never assumed)."""
    try:
        from openshard.sync.capabilities import CAPABILITY_ADVANCED_OSN, LazyCapabilities

        return bool(LazyCapabilities().enabled(CAPABILITY_ADVANCED_OSN))
    except Exception:
        return False


def _version() -> str:
    try:
        from openshard import __version__

        return str(__version__)
    except Exception:
        return "unknown"


def _repo_config(root: Path) -> dict | None:
    if _repo_config_override is not None:
        return _repo_config_override
    try:
        from openshard.config.settings import load_config_safe

        config, _valid, _path = load_config_safe(cwd=root)
        return config if isinstance(config, dict) else None
    except Exception:
        return None


@dataclass(frozen=True)
class Candidate:
    receipt_id: str
    index: int
    entry: dict
    reason: str


@dataclass
class Discovery:
    """What one look at ``runs.jsonl`` found, for one link."""

    scanned: int = 0
    without_receipt_id: int = 0
    in_progress: int = 0
    synced: int = 0
    stale: int = 0
    conflict: int = 0
    rejected: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    newly_stale: list[str] = field(default_factory=list)

    @property
    def pending(self) -> int:
        return len(self.candidates)


def discover(
    root: Path,
    *,
    link: PlatformLink,
    now: datetime | None = None,
    records: dict[str, dict] | None = None,
) -> Discovery:
    """Compare the canonical history with the outbox for *link*. Read-only.

    A record synced to this link whose ``content_hash`` has since changed
    is reported in ``newly_stale`` (the caller decides whether to persist
    that). A record synced to another link is a candidate for this one.
    """
    root = Path(root)
    found = Discovery()
    entries = load_history(root / HISTORY_RELPATH, coerce=True)
    state = records if records is not None else _outbox.load_outbox(root)
    current = now if now is not None else datetime.now(UTC)
    for index, entry in enumerate(entries):
        found.scanned += 1
        rid = stored_receipt_id(entry)
        if rid is None:
            found.without_receipt_id += 1
            continue
        existing = state.get(rid)
        if existing is not None and _outbox.matches_link(
            existing, endpoint=link.endpoint, organisation_id=link.organisation_id
        ):
            st = existing.get("state")
            if st == _outbox.STATE_CONFLICT:
                found.conflict += 1
                continue
            if st == _outbox.STATE_REJECTED:
                found.rejected += 1
                continue
            if st == _outbox.STATE_STALE:
                found.stale += 1
                continue
            if st == _outbox.STATE_SYNCED:
                synced_hash = existing.get("record_hash")
                current_hash = stored_shard_hash(entry)
                if isinstance(synced_hash, str) and current_hash is not None and current_hash != synced_hash:
                    found.stale += 1
                    found.newly_stale.append(rid)
                else:
                    found.synced += 1
                continue
        verdict = eligibility(entry, now=current)
        if verdict.reason == REASON_SESSION_QUIESCENT and _capture_buffer_open(root, entry):
            # Quiet on disk, but capture has not closed the session yet (its
            # staging buffer may hold newer events, and the idle sweep has not
            # stamped ``session_end_not_observed``): sending now would ship a
            # copy that changes locally right after.
            verdict = Eligibility(False, REASON_SESSION_IN_PROGRESS)
        if not verdict.eligible:
            if verdict.reason == REASON_SESSION_IN_PROGRESS:
                found.in_progress += 1
            continue
        found.candidates.append(Candidate(rid, index, entry, verdict.reason))
    return found


def _capture_buffer_open(root: Path, entry: dict) -> bool:
    """True while the capture staging buffer of *entry*'s hook session still exists."""
    try:
        from openshard.adapters.claude_hooks import buffer_path

        capture = entry.get("capture")
        if not isinstance(capture, dict) or capture.get("session_end_observed") is True:
            return False
        sid, agent = capture.get("session_id"), capture.get("agent")
        if not isinstance(sid, str) or not isinstance(agent, str):
            return False
        return buffer_path(root, sid, agent).is_file()
    except Exception:
        return False


def _close_idle_sessions(root: Path, now: datetime | None) -> None:
    """Let capture close sessions idle past the sync threshold before choosing what to send.

    An agent with no session-end hook (Antigravity) is otherwise only swept
    when its next session starts in this repository -- after its Receipt was
    already sent without ``session_end_not_observed``.
    """
    try:
        from openshard.adapters.claude_hooks import sweep_stale_buffers

        sweep_stale_buffers(root, max_age_seconds=QUIESCENT_SECONDS, now=now)
    except Exception:
        pass


@dataclass
class FlushReport:
    """What one ``flush`` did. ``stopped`` names why it ended early, if it did."""

    connected: bool = False
    scanned: int = 0
    pending: int = 0
    sent: int = 0
    created: int = 0
    duplicate: int = 0
    conflict: int = 0
    rejected: int = 0
    stale: int = 0
    in_progress: int = 0
    without_receipt_id: int = 0
    stopped: str | None = None
    # Later verification evidence for Receipts that are already hosted.
    evidence_sent: int = 0
    evidence_recorded: int = 0
    evidence_not_accepted: int = 0
    evidence_unsupported: bool = False
    usage_pending: int = 0
    usage_sent: int = 0
    usage_recorded: int = 0
    usage_not_accepted: int = 0
    usage_unsupported: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _persist_stale(root: Path, found: Discovery, records: dict[str, dict], link: PlatformLink) -> None:
    for rid in found.newly_stale:
        try:
            _outbox.put(root, _outbox.make_record(
                rid, _outbox.STATE_STALE,
                endpoint=link.endpoint, organisation_id=link.organisation_id, previous=records.get(rid),
            ))
        except Exception:
            continue


def flush(
    root: Path,
    *,
    env: dict | os._Environ | None = None,
    link: PlatformLink | None = None,
    transport: _transport.PlatformTransport | None = None,
    now: datetime | None = None,
    limit: int = DEFAULT_FLUSH_LIMIT,
    receipt_ids: frozenset[str] | None = None,
) -> FlushReport:
    """Send every eligible unsynced receipt in *root* to the Platform. Never raises."""
    env = os.environ if env is None else env
    root = Path(root)
    report = FlushReport()
    try:
        link = link or resolve_link(env)
        if link is None:
            report.stopped = "not_connected"
            return report
        report.connected = True
        disabled = sync_disabled(env, _repo_config(root))
        if disabled is not None:
            report.stopped = disabled
            return report
        paused = _transport.in_backoff(env, now=now.timestamp() if now is not None else None)
        if paused is not None:
            report.stopped = f"paused: {paused}"
            return report

        _close_idle_sessions(root, now)
        records = _outbox.load_outbox(root)
        found = discover(root, link=link, now=now, records=records)
        if receipt_ids is not None:
            found.candidates = [c for c in found.candidates if c.receipt_id in receipt_ids]
            found.newly_stale = [rid for rid in found.newly_stale if rid in receipt_ids]
        _persist_stale(root, found, records, link)
        report.scanned = found.scanned
        report.pending = found.pending
        report.stale = found.stale
        report.in_progress = found.in_progress
        report.without_receipt_id = found.without_receipt_id

        sender = transport or _transport_override or _transport.HttpsPlatformTransport(
            link, user_agent=f"openshard/{_version()}"
        )
        version = _version()
        stamp = now.timestamp() if now is not None else None
        advanced_osn = _advanced_osn_enabled()
        for candidate in found.candidates[: max(0, int(limit))]:
            envelope = build_envelope(candidate.entry, candidate.index, core_version=version,
                                      multi_agent=advanced_osn)
            result = sender.send(envelope)
            report.sent += 1
            previous = records.get(candidate.receipt_id)
            if result.accepted:
                _transport.clear_backoff(env)
                if result.kind == _transport.KIND_CREATED:
                    report.created += 1
                else:
                    report.duplicate += 1
                _outbox.put(root, _outbox.make_record(
                    candidate.receipt_id, _outbox.STATE_SYNCED,
                    endpoint=link.endpoint, organisation_id=link.organisation_id,
                    payload_hash=payload_hash(envelope["receipt"]),
                    record_hash=stored_shard_hash(candidate.entry),
                    previous=previous,
                ))
                report.pending -= 1
            elif result.kind == _transport.KIND_CONFLICT:
                report.conflict += 1
                report.pending -= 1
                _outbox.put(root, _outbox.make_record(
                    candidate.receipt_id, _outbox.STATE_CONFLICT,
                    endpoint=link.endpoint, organisation_id=link.organisation_id,
                    status=result.status, code=result.code, details=result.details, previous=previous,
                ))
            elif result.kind == _transport.KIND_REJECTED:
                report.rejected += 1
                report.pending -= 1
                _outbox.put(root, _outbox.make_record(
                    candidate.receipt_id, _outbox.STATE_REJECTED,
                    endpoint=link.endpoint, organisation_id=link.organisation_id,
                    status=result.status, code=result.code, details=result.details, previous=previous,
                ))
            elif result.kind in _transport.LINK_KINDS:
                _transport.record_link_failure(result.kind, env, now=now.timestamp() if now is not None else None)
                report.stopped = f"paused: {result.kind}"
                return report
            else:
                _transport.record_failure(env, now=now.timestamp() if now is not None else None)
                report.stopped = "paused: unavailable"
                return report
        _flush_evidence(root, link, sender, report, env=env, version=version, stamp=stamp, limit=limit, receipt_ids=receipt_ids)
        _flush_usage(root, link, sender, report, env=env, version=version, stamp=stamp, limit=limit, receipt_ids=receipt_ids)
        return report
    except Exception:
        report.stopped = report.stopped or "error"
        return report


def _flush_evidence(
    root: Path,
    link: PlatformLink,
    sender: Any,
    report: FlushReport,
    *,
    env: dict | os._Environ,
    version: str,
    stamp: float | None,
    limit: int,
    receipt_ids: frozenset[str] | None = None,
) -> None:
    """Send later verification evidence for Receipts whose hosted copy is the local one.

    Derived like receipts are: every flush compares what this machine would
    send now (``evidence_hash``) with what the outbox says it last decided
    with the Platform, so a new attestation is picked up and an unchanged
    one costs nothing. A Platform without the route is skipped quietly; a
    Receipt it does not hold yet is tried again next flush.
    """
    send_evidence = getattr(sender, "send_evidence", None)
    if not callable(send_evidence):
        return
    from openshard.sync.evidence import build_evidence_envelope, evidence_hash
    from openshard.verification.post_session import load_attestations

    attestations = load_attestations(root / HISTORY_RELPATH.parent)
    if not attestations:
        return
    records = _outbox.load_outbox(root)
    entries = load_history(root / HISTORY_RELPATH, coerce=True)
    budget = max(0, int(limit))
    for index, entry in enumerate(entries):
        if budget <= 0:
            return
        rid = stored_receipt_id(entry)
        if receipt_ids is not None and rid not in receipt_ids:
            continue
        record = records.get(rid) if rid is not None else None
        if rid is None or record is None or record.get("state") != _outbox.STATE_SYNCED:
            continue
        if not _outbox.matches_link(record, endpoint=link.endpoint, organisation_id=link.organisation_id):
            continue
        synced_hash = record.get("record_hash")
        current_hash = stored_shard_hash(entry)
        if isinstance(synced_hash, str) and current_hash is not None and current_hash != synced_hash:
            continue  # changed locally since sync: the hosted Receipt is a different one
        envelope = build_evidence_envelope(entry, index, attestations, core_version=version)
        if envelope is None:
            continue
        digest = evidence_hash(envelope)
        if record.get("evidence_hash") == digest:
            continue  # already decided for exactly this evidence
        result = send_evidence(rid, envelope)
        budget -= 1
        report.evidence_sent += 1
        if result.accepted:
            _transport.clear_backoff(env)
            report.evidence_recorded += 1
            _outbox.put(root, _outbox.with_evidence(record, _outbox.STATE_SYNCED, evidence_hash=digest))
        elif result.kind in (_transport.KIND_CONFLICT, _transport.KIND_REJECTED):
            report.evidence_not_accepted += 1
            state = _outbox.STATE_CONFLICT if result.kind == _transport.KIND_CONFLICT else _outbox.STATE_REJECTED
            _outbox.put(root, _outbox.with_evidence(
                record, state, evidence_hash=digest,
                status=result.status, code=result.code, details=result.details,
            ))
        elif result.kind == _transport.KIND_RECEIPT_PENDING:
            continue
        elif result.kind == _transport.KIND_UNSUPPORTED:
            report.evidence_unsupported = True
            return
        elif result.kind in _transport.LINK_KINDS:
            _transport.record_link_failure(result.kind, env, now=stamp)
            report.stopped = f"paused: {result.kind}"
            return
        else:
            _transport.record_failure(env, now=stamp)
            report.stopped = "paused: unavailable"
            return


def _flush_usage(
    root: Path,
    link: PlatformLink,
    sender: Any,
    report: FlushReport,
    *,
    env: dict | os._Environ,
    version: str,
    stamp: float | None,
    limit: int,
    receipt_ids: frozenset[str] | None = None,
) -> None:
    """Send later usage evidence for Receipts whose hosted copy is the local one.

    Same derivation as verification evidence: compare ``usage_hash`` with the
    outbox. A Platform without the route is skipped quietly.
    """
    send_usage = getattr(sender, "send_usage", None)
    if not callable(send_usage):
        return
    from openshard.history.usage_evidence import load_usage_attestations
    from openshard.sync.usage import build_usage_envelope, usage_hash

    records = _outbox.load_outbox(root)
    entries = load_history(root / HISTORY_RELPATH, coerce=True)
    # One bounded provider poll per flush; existing outbox stores retry cadence.
    from openshard.adapters.cursor_usage import AUTO_POLL_SECONDS, auto_reconcile_usage

    current = stamp if stamp is not None else datetime.now(UTC).timestamp()
    for entry in entries:
        rid = stored_receipt_id(entry)
        record = records.get(rid) if rid is not None else None
        if (rid is None or record is None or record.get("state") != _outbox.STATE_SYNCED
                or (receipt_ids is not None and rid not in receipt_ids)
                or not _outbox.matches_link(record, endpoint=link.endpoint, organisation_id=link.organisation_id)
                or stored_shard_hash(entry) != record.get("record_hash")):
            continue
        # Keep delayed provider usage eligible for bounded background polling.
        synced_at = record.get("synced_at")
        try:
            synced = datetime.fromisoformat(str(synced_at).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if current - synced > 7 * 24 * 60 * 60:
            continue
        if record.get("cursor_usage_polling"):
            report.usage_pending += 1
        checked = record.get("cursor_usage_poll_at")
        if isinstance(checked, (int, float)) and current - checked < AUTO_POLL_SECONDS:
            continue
        if auto_reconcile_usage(root, entries, entry, env=env):
            record = dict(record, cursor_usage_poll_at=current, cursor_usage_polling=True)
            if not records[rid].get("cursor_usage_polling"):
                report.usage_pending += 1
            _outbox.put(root, record)
            records[rid] = record
            break
    attestations = load_usage_attestations(root / HISTORY_RELPATH.parent)
    if not attestations:
        return
    budget = max(0, int(limit))
    for entry in entries:
        if budget <= 0:
            return
        rid = stored_receipt_id(entry)
        if receipt_ids is not None and rid not in receipt_ids:
            continue
        record = records.get(rid) if rid is not None else None
        if rid is None or record is None or record.get("state") != _outbox.STATE_SYNCED:
            continue
        if not _outbox.matches_link(record, endpoint=link.endpoint, organisation_id=link.organisation_id):
            continue
        synced_hash = record.get("record_hash")
        current_hash = stored_shard_hash(entry)
        if isinstance(synced_hash, str) and current_hash is not None and current_hash != synced_hash:
            continue
        envelope = build_usage_envelope(entry, attestations, core_version=version)
        if envelope is None:
            continue
        digest = usage_hash(envelope)
        if record.get("usage_hash") == digest:
            continue
        result = send_usage(rid, envelope)
        budget -= 1
        report.usage_sent += 1
        if result.accepted:
            _transport.clear_backoff(env)
            report.usage_recorded += 1
            _outbox.put(root, _outbox.with_usage(record, _outbox.STATE_SYNCED, usage_hash=digest))
        elif result.kind in (_transport.KIND_CONFLICT, _transport.KIND_REJECTED):
            report.usage_not_accepted += 1
            state = _outbox.STATE_CONFLICT if result.kind == _transport.KIND_CONFLICT else _outbox.STATE_REJECTED
            _outbox.put(root, _outbox.with_usage(
                record, state, usage_hash=digest,
                status=result.status, code=result.code, details=result.details,
            ))
        elif result.kind == _transport.KIND_RECEIPT_PENDING:
            report.usage_pending += 1
            continue
        elif result.kind == _transport.KIND_UNSUPPORTED:
            report.usage_unsupported = True
            return
        elif result.kind in _transport.LINK_KINDS:
            _transport.record_link_failure(result.kind, env, now=stamp)
            report.stopped = f"paused: {result.kind}"
            return
        else:
            _transport.record_failure(env, now=stamp)
            report.stopped = "paused: unavailable"
            return


def status(root: Path, *, env: dict | os._Environ | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Everything ``openshard sync status`` shows for *root*. Read-only, never raises."""
    env = os.environ if env is None else env
    root = Path(root)
    try:
        link = resolve_link(env)
        doc: dict[str, Any] = {
            "connected": link is not None,
            "link": link.to_public_dict() if link else None,
            "disabled": sync_disabled(env, _repo_config(root)),
            "paused": _transport.in_backoff(env, now=now.timestamp() if now is not None else None),
            "outbox": str(_outbox.OUTBOX_RELPATH.as_posix()),
        }
        if link is None:
            return doc
        records = _outbox.load_outbox(root)
        found = discover(root, link=link, now=now, records=records)
        counts = _outbox.summarize(records, endpoint=link.endpoint, organisation_id=link.organisation_id)
        counts[_outbox.STATE_STALE] += len(found.newly_stale)
        counts[_outbox.STATE_SYNCED] = found.synced
        doc.update({
            "scanned": found.scanned,
            "pending": found.pending,
            "in_progress": found.in_progress,
            "without_receipt_id": found.without_receipt_id,
            **counts,
            "problems": [
                {"receipt_id": rid, "state": rec.get("state"), "error": rec.get("last_error")}
                for rid, rec in sorted(records.items())
                if rec.get("state") in _outbox.TERMINAL_STATES
                and _outbox.matches_link(rec, endpoint=link.endpoint, organisation_id=link.organisation_id)
            ][:20],
        })
        return doc
    except Exception:
        return {"connected": False, "link": None, "disabled": None, "paused": None, "error": "unavailable"}


def flush_repos(
    roots: Iterable[Path],
    *,
    env: dict | os._Environ | None = None,
    transport: _transport.PlatformTransport | None = None,
) -> list[FlushReport]:
    """``flush`` for each repository root that still has a history file. Never raises."""
    reports: list[FlushReport] = []
    for raw in roots:
        try:
            root = Path(raw)
            if not (root / HISTORY_RELPATH).is_file():
                continue
            reports.append(flush(root, env=env, transport=transport))
        except Exception:
            continue
    return reports


def sync_periodically(
    stop: threading.Event,
    *,
    repos: Callable[[], Iterable[Path]],
    env: dict | os._Environ | None = None,
    interval: float = SYNC_INTERVAL_SECONDS,
    transport: _transport.PlatformTransport | None = None,
) -> None:
    """Long-running processes (the capture service): flush every known repository until *stop*.

    Costs nothing when no link is stored: ``resolve_link`` reads one small
    file and the loop goes back to waiting.
    """
    env = os.environ if env is None else env
    while not stop.wait(interval):
        if resolve_link(env) is None:
            continue
        try:
            flush_repos(list(repos()), env=env, transport=transport)
        except Exception:
            continue
    if resolve_link(env) is not None:
        try:
            flush_repos(list(repos()), env=env, transport=transport)
        except Exception:
            pass
