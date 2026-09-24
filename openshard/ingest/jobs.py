"""Resumable import jobs: state machine, checkpoints, retries, cancellation (§5).

Job states::

    created -> discovering -> processing -> finalizing -> completed
                   |              |
                   +--> paused <--+   (Ctrl-C / crash: resumable)
                   +--> cancelled     (cancel request honoured)
                   +--> failed        (fatal: source access gone)

Item states: ``fetched -> parsed -> written | attached | skipped_duplicate |
skipped_filtered | failed_retryable | failed | quarantined``.

Exactly-once: an item's terminal line is appended to ``items.jsonl`` only
after its receipt/attachment append succeeded. A crash in between leaves the
item non-terminal; on resume it is processed again and the dedupe snapshot --
rebuilt from ``runs.jsonl`` itself, not from the cache -- finds the receipt
already written and records ``skipped_duplicate``. Nothing is written twice.

The job directory never holds content: object keys (hashes of object ids),
a path-free display name, source hashes, import keys, states and error
classes.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from openshard.ingest import dedupe
from openshard.ingest.connectors.base import ConnectorFatalError, ConnectorIOError, SourceConnector
from openshard.ingest.enrich.git_local import enrich_git
from openshard.ingest.model import HistoricalSession, ParsedSession, SourceObject
from openshard.ingest.normalize import import_key, normalize
from openshard.ingest.parsers.base import HEAD_BYTES, ParseError, Parser, parser_id
from openshard.ingest.receipt_builder import (
    build_attachment,
    build_entry,
    locator_display,
    object_key,
)
from openshard.ingest.registry import (
    EXECUTOR_FOR_PARSER,
    LIVE_EXECUTOR_FOR_AGENT,
    SOURCES,
    select_parser,
)
from openshard.ingest.shard_builder import route_session
from openshard.ingest.store import (
    ImportRecord,
    LocalHistoryStore,
    LocalJobStore,
    StoreSnapshot,
)

# Job states
CREATED = "created"
DISCOVERING = "discovering"
PROCESSING = "processing"
FINALIZING = "finalizing"
COMPLETED = "completed"
PAUSED = "paused"
CANCELLED = "cancelled"
FAILED = "failed"
RESUMABLE_STATES = frozenset({CREATED, DISCOVERING, PROCESSING, FINALIZING, PAUSED})

# Item states
FETCHED = "fetched"
PARSED = "parsed"
WRITTEN = "written"
ATTACHED = "attached"
SKIPPED_DUPLICATE = "skipped_duplicate"
SKIPPED_FILTERED = "skipped_filtered"
FAILED_RETRYABLE = "failed_retryable"
ITEM_FAILED = "failed"
QUARANTINED = "quarantined"
TERMINAL_ITEM_STATES = frozenset({WRITTEN, ATTACHED, SKIPPED_DUPLICATE, SKIPPED_FILTERED, ITEM_FAILED, QUARANTINED})

QUIESCENCE_SECONDS = 30 * 60
MAX_OBJECT_BYTES = 512 * 1024 * 1024
MAX_ATTEMPTS = 5
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0

FILTER_POSSIBLY_LIVE = "possibly_live"
FILTER_BEFORE_SINCE = "before_since"
FILTER_AFTER_UNTIL = "after_until"
FILTER_NO_TIMESTAMP = "no_session_timestamp"
FILTER_SOURCE_MISSING = "source_missing"
FILTER_NO_UPDATE = "no_update"


class JobCancelled(Exception):
    """Raised inside a long parse when a cancel request is seen."""


class JobNotFound(LookupError):
    pass


class JobNotResumable(RuntimeError):
    pass


@dataclass
class JobSpec:
    sources: list[str]
    since: str | None = None
    until: str | None = None
    all_repos: bool = False
    allow_home_repo: bool = False
    no_update: bool = False
    enrich_git: bool = True
    quiescence_seconds: int = QUIESCENCE_SECONDS

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> JobSpec:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Counters:
    discovered: int = 0
    written: int = 0
    superseded: int = 0
    attached: int = 0
    skipped_duplicate: int = 0
    skipped_filtered: int = 0
    quarantined: int = 0
    failed: int = 0
    retries: int = 0
    filtered_by_reason: dict[str, int] = field(default_factory=dict)

    def filtered(self, reason: str) -> None:
        self.skipped_filtered += 1
        self.filtered_by_reason[reason] = self.filtered_by_reason.get(reason, 0) + 1


@dataclass
class JobResult:
    job_id: str
    state: str
    counters: Counters
    error: str | None = None

    def to_dict(self) -> dict:
        return {"job_id": self.job_id, "state": self.state, "counters": asdict(self.counters), "error": self.error}


ProgressCb = Callable[[dict], None]


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_job_id() -> str:
    return f"ijob_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(3)}"


def _in_range(start: str, since: str | None, until: str | None) -> str | None:
    """Filter reason for a session starting at *start*, else None. Plain ISO compare."""
    if since and start < _as_bound(since, end=False):
        return FILTER_BEFORE_SINCE
    if until and start > _as_bound(until, end=True):
        return FILTER_AFTER_UNTIL
    return None


def _as_bound(value: str, *, end: bool) -> str:
    """``YYYY-MM-DD`` -> a comparable UTC stamp; full stamps are normalized."""
    from openshard.ingest.parsers.claude_code import normalize_stamp

    if len(value) == 10:
        return value + ("T23:59:59.999Z" if end else "T00:00:00.000Z")
    return normalize_stamp(value) or value


class _HashingReader:
    """Wrap a binary stream, hashing every byte the parser reads (and any left over)."""

    def __init__(self, raw: BinaryIO, limit: int) -> None:
        self._raw = raw
        self._hash = hashlib.sha256()
        self._n = 0
        self._limit = limit

    def _take(self, data: bytes) -> bytes:
        self._n += len(data)
        if self._n > self._limit:
            raise _TooLarge()
        self._hash.update(data)
        return data

    def read(self, n: int = -1) -> bytes:
        return self._take(self._raw.read(n))

    def readline(self, n: int = -1) -> bytes:
        return self._take(self._raw.readline(n))

    def drain(self) -> None:
        while chunk := self._raw.read(1 << 20):
            self._take(chunk)

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


class _TooLarge(Exception):
    pass


# ---------------------------------------------------------------------------
# Per-object processing shared by scan (dry run) and run
# ---------------------------------------------------------------------------


@dataclass
class _Prepared:
    parser: Parser
    source_sha256: str
    sessions: list[ParsedSession]


class _Pipeline:
    def __init__(self, spec: JobSpec, target_root: Path, *, sleep: Callable[[float], None],
                 checkpoint: Callable[[], None] | None = None) -> None:
        self.spec = spec
        self.target_root = target_root.resolve()
        self.sleep = sleep
        self.checkpoint = checkpoint
        self._stores: dict[Path, tuple[LocalHistoryStore, StoreSnapshot]] = {}

    def store(self, root: Path) -> tuple[LocalHistoryStore, StoreSnapshot]:
        if root not in self._stores:
            store = LocalHistoryStore(root)
            self._stores[root] = (store, store.snapshot())
        return self._stores[root]

    def fetch_and_parse(self, connector: SourceConnector, obj: SourceObject, counters: Counters) -> _Prepared:
        """Open (with retries), sniff, stream-parse and hash one object."""
        delay = BACKOFF_START
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self._fetch_once(connector, obj)
            except (ParseError, _TooLarge, JobCancelled, FileNotFoundError):
                raise
            except (ConnectorIOError, OSError):
                if attempt >= MAX_ATTEMPTS:
                    raise
                counters.retries += 1
                self.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)
        raise ConnectorIOError("unreachable")

    def _fetch_once(self, connector: SourceConnector, obj: SourceObject) -> _Prepared:
        if obj.size is not None and obj.size > MAX_OBJECT_BYTES:
            raise _TooLarge()
        with connector.open(obj) as raw:
            head = raw.read(HEAD_BYTES)
            parser = select_parser(head, obj)
            if parser is None:
                raise ParseError("unrecognized_format")
            raw.seek(0)
            reader = _HashingReader(raw, MAX_OBJECT_BYTES)
            sessions = list(parser.parse(reader, obj, checkpoint=self.checkpoint))  # type: ignore[arg-type]
            reader.drain()
            return _Prepared(parser, reader.hexdigest(), sessions)

    def plan(self, parsed: ParsedSession, source_sha256: str) -> tuple[str, Any]:
        """``(status, detail)``: ``("filtered", reason)`` or ``("ok", (root, hs, decision))``."""
        route = route_session(parsed.cwd, self.target_root, all_repos=self.spec.all_repos,
                              allow_home_repo=self.spec.allow_home_repo)
        if not route.ok or route.repo_root is None:
            return "filtered", route.status
        if not parsed.start:
            return "filtered", FILTER_NO_TIMESTAMP
        reason = _in_range(parsed.start, self.spec.since, self.spec.until)
        if reason:
            return "filtered", reason
        root = route.repo_root
        hs = normalize(parsed, root, source_sha256)
        _store, snap = self.store(root)
        decision = dedupe.decide(
            snap, import_key=hs.import_key, source_sha256=source_sha256,
            live_executor=LIVE_EXECUTOR_FOR_AGENT.get(parsed.agent),
            native_session_id=parsed.native_session_id, no_update=self.spec.no_update,
        )
        return "ok", (root, hs, decision)

    def write(self, root: Path, hs: HistoricalSession, decision: dedupe.Decision, obj: SourceObject,
              parser_name: str, job_id: str | None) -> tuple[str, str]:
        """Enrich, seal and append. Returns ``(item_state, receipt_or_attachment_id)``."""
        store, snap = self.store(root)
        if self.spec.enrich_git:
            enrich_git(hs, root)
        if decision.action == dedupe.ATTACH_LIVE and decision.live is not None:
            att = build_attachment(hs, obj, receipt_id=decision.live.receipt_id,
                                   pins_content_hash=decision.live.content_hash, job_id=job_id,
                                   supersedes=decision.supersedes_attachment_id)
            store.append_attachment(att)
            snap.imports[hs.import_key] = ImportRecord(hs.import_key, hs.source_sha256,
                                                       attachment_id=att["attachment_id"])
            return ATTACHED, att["attachment_id"]
        entry = build_entry(hs, obj, executor=EXECUTOR_FOR_PARSER[parser_name], job_id=job_id,
                            run_index=snap.record_count, supersedes=decision.supersedes_receipt_id)
        store.append_receipt(entry)
        snap.record_count += 1
        snap.imports[hs.import_key] = ImportRecord(hs.import_key, hs.source_sha256, receipt_id=entry["receipt_id"])
        return WRITTEN, entry["receipt_id"]


def _connectors(names: Iterable[str], env: Mapping[str, str] | None) -> list[tuple[str, SourceConnector]]:
    out = []
    for name in names:
        spec = SOURCES.get(name)
        if spec is None:
            raise ValueError(f"unknown source: {name}")
        out.append((name, spec.connector(env)))
    return out


def _quiescent(obj: SourceObject, seconds: int, now: float | None = None) -> bool:
    if obj.mtime is None or seconds <= 0:
        return True
    return ((now if now is not None else time.time()) - obj.mtime) >= seconds


# ---------------------------------------------------------------------------
# Sources and scan (read-only)
# ---------------------------------------------------------------------------


def discover_sources(repo_path: Path, *, env: Mapping[str, str] | None = None) -> list[dict]:
    """Detected local history sources with session counts (reads only file heads)."""
    from openshard.ingest.registry import PARSERS

    target = repo_path.resolve()
    rows = []
    for name, connector in _connectors(SOURCES, env):
        access = connector.check_access()
        row: dict[str, Any] = {"source": name, "label": SOURCES[name].label, "available": access.ok,
                               "sessions": 0, "in_this_repo": 0}
        if access.ok:
            parser = PARSERS.get(getattr(connector, "parser_hint", ""), None)
            for obj in connector.discover():
                row["sessions"] += 1
                if parser is None:
                    continue
                try:
                    with connector.open(obj) as fh:
                        meta = parser.peek(fh.read(HEAD_BYTES))
                except OSError:
                    continue
                route = route_session(meta.get("cwd"), target)
                if route.ok:
                    row["in_this_repo"] += 1
        rows.append(row)
    return rows


def scan(spec: JobSpec, repo_path: Path, *, env: Mapping[str, str] | None = None,
         sleep: Callable[[float], None] = time.sleep) -> dict:
    """Dry run: discover, parse and plan every object. Writes nothing."""
    pipe = _Pipeline(spec, repo_path, sleep=sleep)
    counters = Counters()
    decisions: dict[str, int] = {}
    coverage: dict[str, dict[str, int]] = {}
    repos: dict[str, int] = {}
    first: str | None = None
    last: str | None = None
    for _name, connector in _connectors(spec.sources, env):
        for obj in connector.discover():
            counters.discovered += 1
            if not _quiescent(obj, spec.quiescence_seconds):
                counters.filtered(FILTER_POSSIBLY_LIVE)
                continue
            try:
                prepared = pipe.fetch_and_parse(connector, obj, counters)
            except FileNotFoundError:
                counters.filtered(FILTER_SOURCE_MISSING)
                continue
            except (ParseError, _TooLarge):
                counters.quarantined += 1
                continue
            except OSError:
                counters.failed += 1
                continue
            for parsed in prepared.sessions:
                status, detail = pipe.plan(parsed, prepared.source_sha256)
                if status == "filtered":
                    counters.filtered(detail)
                    continue
                root, hs, decision = detail
                decisions[decision.action] = decisions.get(decision.action, 0) + 1
                repos[root.name] = repos.get(root.name, 0) + 1
                start = parsed.start
                first = min(first, start) if first and start else (first or start)
                last = max(last, start) if last and start else (last or start)
                for fname, fact in hs.facts.items():
                    bucket = coverage.setdefault(fname, {})
                    bucket[fact.evidence] = bucket.get(fact.evidence, 0) + 1
    return {
        "counters": asdict(counters),
        "decisions": decisions,
        "repos": repos,
        "date_range": {"first": first, "last": last},
        "evidence_coverage": coverage,
    }


# ---------------------------------------------------------------------------
# Run / resume / cancel
# ---------------------------------------------------------------------------


def run_job(spec: JobSpec, *, repo_path: Path, progress_cb: ProgressCb | None = None,
            env: Mapping[str, str] | None = None, sleep: Callable[[float], None] = time.sleep,
            job_id: str | None = None) -> JobResult:
    """Create a job for *spec* in *repo_path* and run it in the foreground."""
    jobs = LocalJobStore(repo_path)
    job_id = job_id or _new_job_id()
    _connectors(spec.sources, env)  # validate names before creating anything
    job = {
        "job_id": job_id, "created_at": _now(), "updated_at": _now(), "state": CREATED,
        "spec": asdict(spec), "counters": asdict(Counters()), "error": None,
        "importer_version": "historical-ingestion@1",
    }
    with jobs.active_lock():
        jobs.write_job(job_id, job)
        return _drive(job, jobs, repo_path, progress_cb=progress_cb, env=env, sleep=sleep)


def resume_job(job_id: str, *, repo_path: Path, progress_cb: ProgressCb | None = None,
               env: Mapping[str, str] | None = None, sleep: Callable[[float], None] = time.sleep) -> JobResult:
    jobs = LocalJobStore(repo_path)
    with jobs.active_lock():
        job = jobs.read_job(job_id)
        if job is None:
            raise JobNotFound(job_id)
        if job.get("state") not in RESUMABLE_STATES:
            raise JobNotResumable(f"job is {job.get('state')}")
        return _drive(job, jobs, repo_path, progress_cb=progress_cb, env=env, sleep=sleep)


def cancel_job(job_id: str, *, repo_path: Path) -> bool:
    """Request cancellation. A running job stops at its next check; an idle one is marked now."""
    jobs = LocalJobStore(repo_path)
    job = jobs.read_job(job_id)
    if job is None or job.get("state") not in RESUMABLE_STATES:
        return False
    jobs.request_cancel(job_id)
    if job.get("state") == PAUSED:
        job.update(state=CANCELLED, updated_at=_now())
        jobs.write_job(job_id, job)
    return True


def job_status(job_id: str, *, repo_path: Path) -> dict | None:
    jobs = LocalJobStore(repo_path)
    job = jobs.read_job(job_id)
    if job is None:
        return None
    items = jobs.items(job_id)
    latest: dict[str, dict] = {}
    for it in items:
        latest[str(it.get("object_key"))] = it
    job["items"] = {
        "total": len(latest),
        "quarantined": [
            {k: it.get(k) for k in ("object_key", "locator_display", "source_sha256", "error_class", "parser")}
            for it in latest.values() if it.get("state") == QUARANTINED
        ],
    }
    return job


def list_jobs(*, repo_path: Path) -> list[dict]:
    return LocalJobStore(repo_path).list_jobs()


def _drive(job: dict, jobs: LocalJobStore, repo_path: Path, *, progress_cb: ProgressCb | None,
           env: Mapping[str, str] | None, sleep: Callable[[float], None]) -> JobResult:
    job_id = job["job_id"]
    spec = JobSpec.from_dict(job.get("spec") or {})
    counters = Counters(**{k: v for k, v in (job.get("counters") or {}).items() if k in Counters.__dataclass_fields__})

    def save(state: str, error: str | None = None) -> None:
        job.update(state=state, updated_at=_now(), counters=asdict(counters), error=error)
        jobs.write_job(job_id, job)

    def emit(kind: str, **data: Any) -> None:
        if progress_cb is not None:
            try:
                progress_cb({"kind": kind, "job_id": job_id, "counters": asdict(counters), **data})
            except Exception:
                pass

    def check_cancel() -> None:
        if jobs.cancel_requested(job_id):
            raise JobCancelled()

    done: dict[str, str] = {}
    for it in jobs.items(job_id):
        if it.get("state") in TERMINAL_ITEM_STATES:
            done[str(it.get("object_key"))] = str(it.get("state"))

    pipe = _Pipeline(spec, repo_path, sleep=sleep, checkpoint=check_cancel)
    cursors = jobs.read_cursor(job_id) or {}
    try:
        save(DISCOVERING)
        connectors = _connectors(spec.sources, env)
        for name, connector in connectors:
            access = connector.check_access()
            if not access.ok:
                emit("source_unavailable", source=name, detail=access.detail)
        save(PROCESSING)
        for name, connector in connectors:
            if not connector.check_access().ok:
                continue
            for obj in connector.discover(cursors.get(name)):
                check_cancel()
                key = object_key(obj.object_id)
                if key in done:
                    continue
                counters.discovered += 1
                state = _process_object(pipe, connector, obj, key, job_id, jobs, counters)
                done[key] = state
                cursors[name] = {"after": obj.object_id}
                jobs.write_cursor(job_id, cursors)
                save(PROCESSING)
                emit("item", state=state)
        save(FINALIZING)
        save(COMPLETED)
        emit("completed")
        return JobResult(job_id, COMPLETED, counters)
    except JobCancelled:
        save(CANCELLED)
        emit("cancelled")
        return JobResult(job_id, CANCELLED, counters)
    except KeyboardInterrupt:
        save(PAUSED)
        emit("paused")
        return JobResult(job_id, PAUSED, counters)
    except ConnectorFatalError as exc:
        save(FAILED, type(exc).__name__)
        return JobResult(job_id, FAILED, counters, type(exc).__name__)


def _process_object(pipe: _Pipeline, connector: SourceConnector, obj: SourceObject, key: str, job_id: str,
                    jobs: LocalJobStore, counters: Counters) -> str:
    base = {"object_key": key, "locator_display": locator_display(obj)}

    def log(state: str, **extra: Any) -> None:
        jobs.append_item(job_id, {**base, "state": state, "at": _now(), **extra})

    if not _quiescent(obj, pipe.spec.quiescence_seconds):
        counters.filtered(FILTER_POSSIBLY_LIVE)
        log(SKIPPED_FILTERED, reason=FILTER_POSSIBLY_LIVE)
        return SKIPPED_FILTERED
    try:
        prepared = pipe.fetch_and_parse(connector, obj, counters)
    except FileNotFoundError:
        counters.filtered(FILTER_SOURCE_MISSING)
        log(SKIPPED_FILTERED, reason=FILTER_SOURCE_MISSING)
        return SKIPPED_FILTERED
    except (ParseError, _TooLarge) as exc:
        counters.quarantined += 1
        error_class = str(exc) if isinstance(exc, ParseError) and str(exc) else type(exc).__name__.strip("_")
        log(QUARANTINED, error_class=error_class[:64], parser=obj.hint)
        return QUARANTINED
    except JobCancelled:
        raise
    except OSError as exc:
        counters.failed += 1
        log(ITEM_FAILED, error_class=type(exc).__name__, attempts=MAX_ATTEMPTS)
        return ITEM_FAILED
    except Exception as exc:  # a parser bug is quarantined, never crashes the job
        counters.quarantined += 1
        log(QUARANTINED, error_class=f"parser_error:{type(exc).__name__}"[:64], parser=obj.hint)
        return QUARANTINED
    pid = parser_id(prepared.parser)
    log(PARSED, source_sha256=prepared.source_sha256, parser=pid, sessions=len(prepared.sessions))

    final = SKIPPED_FILTERED
    for parsed in prepared.sessions:
        key_for_log = import_key(parsed.agent, parsed.native_session_id)
        try:
            status, detail = pipe.plan(parsed, prepared.source_sha256)
        except Exception as exc:
            counters.quarantined += 1
            log(QUARANTINED, error_class=f"normalize_error:{type(exc).__name__}"[:64], parser=pid)
            final = QUARANTINED
            continue
        if status == "filtered":
            counters.filtered(detail)
            final = SKIPPED_FILTERED
            continue
        root, hs, decision = detail
        if decision.action == dedupe.SKIP_DUPLICATE:
            counters.skipped_duplicate += 1
            final = SKIPPED_DUPLICATE
            continue
        if decision.action == dedupe.SKIP_NO_UPDATE:
            counters.filtered(FILTER_NO_UPDATE)
            final = SKIPPED_FILTERED
            continue
        try:
            state, written_id = pipe.write(root, hs, decision, obj, prepared.parser.name, job_id)
        except ValueError as exc:
            counters.quarantined += 1
            log(QUARANTINED, error_class=f"build_error:{str(exc)[:40]}", parser=pid, import_key=key_for_log)
            final = QUARANTINED
            continue
        if state == ATTACHED:
            counters.attached += 1
        else:
            counters.written += 1
            if decision.action == dedupe.SUPERSEDE:
                counters.superseded += 1
        final = state
        log(state, import_key=key_for_log, source_sha256=prepared.source_sha256, id=written_id,
            decision=decision.action)
    if final in (SKIPPED_DUPLICATE, SKIPPED_FILTERED):
        log(final, source_sha256=prepared.source_sha256)
    elif final == QUARANTINED and not prepared.sessions:
        log(QUARANTINED, error_class="no_sessions", parser=pid)
    return final


def default_env() -> Mapping[str, str]:
    return os.environ
