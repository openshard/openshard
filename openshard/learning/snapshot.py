"""The precomputed learning snapshot OSN reads at startup instead of history.

    runs.jsonl -(background worker)-> learning snapshot -(bounded lookup)-> OSN run

Learning Loop V1 derives signals from ``runs.jsonl`` with ``derive_signals``,
and Routing V2 reads harness-wide and repository + task-category history from
the same file. Doing that on every ``osn run`` makes startup cost grow with
history. Instead, ``openshard.learning.worker`` runs those *same* functions in
the background after each history write and publishes the result here; a run
then does one bounded read of this file and nothing else.

Semantics are unchanged: the snapshot holds exactly what ``derive_signals``,
``load_history_evidence`` and ``load_scoped_history`` would compute, with two
read-time adjustments that keep it honest as it ages:

* freshness is recomputed from each signal's ``last_seen`` when it is read
  (only signals surfaceable at derive time are stored, and time only makes a
  signal staler, so nothing that could surface is ever missing);
* ``signals_total`` keeps the full derived count, so "signals considered" and
  ``no_relevant_signals`` mean what they always did (except after a trim,
  below: signals dropped to fit were never considered and are not counted).

Each stored signal carries ``last_seen_epoch`` (whole seconds), so the read
recomputes freshness with integer arithmetic instead of parsing a timestamp
per signal; per-signal validation, not JSON parsing, dominated the read.

A snapshot is only ever published whole (temp file, fsync, atomic replace),
from one consistent copy of the history. A lookup that is late, finds no
snapshot, finds one it cannot fully validate, or finds one derived for a
different checkout fails open: the run starts without learning and records
*why* (``timeout`` / ``unavailable``), never ``no_history``, and with unknown
(None) counts, never zero. No remote call, no git subprocess and no history
read happens here.

Repository identity is checked without git: the worker records the checkout
root and the size and modification time of the git config that holds the
remote; the lookup compares them with ``stat`` calls. Any change (a new
remote, or any other config write) makes the snapshot unusable until the
worker re-derives it, rather than risk another repository's signals.

The lookup budget (``learning.lookup_budget_ms``, 25 ms by default, at most
100) is best effort, not a realtime guarantee: the read runs on a thread and
the caller stops waiting at the budget, but JSON parsing holds Python's GIL,
so a parse already under way finishes first. ``MAX_SNAPSHOT_BYTES`` keeps that
overrun small. A derivation over the cap is trimmed to fit: the weakest and
stalest signals are dropped first, and the snapshot records ``trimmed``,
``signals_stored`` and ``signals_dropped`` next to the full ``signals_total``.
A run then reports as considered only the signals it could score, and the
Receipt shows the derived total separately. Only when even a
snapshot with no signals cannot fit is a truthful ``oversized`` marker
published instead.
"""
from __future__ import annotations

import hashlib
import json
import math
import operator
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.history.jsonl_store import replace_with_retry
from openshard.learning.retrieval import (
    STATUS_ERROR,
    STATUS_NO_HISTORY,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
    LearningContext,
    consult,
    task_shape_for,
)
from openshard.learning.routing import ScopedHistoryEvidence
from openshard.learning.signals import (
    KINDS,
    LEARNING_SIGNAL_VERSION,
    STALE,
    STRENGTH_MODERATE,
    STRENGTH_STRONG,
    LearningIndex,
    LearningSignal,
    derive_signals,
    entries_from_lines,
    freshness_for_age,
    parse_timestamp,
    task_category_for,
)
from openshard.routing.adaptive.history_evidence import (
    HistoryEvidence,
    ModelHistory,
    build_history_evidence,
    history_evidence_from_lines,
)

SNAPSHOT_FORMAT = "openshard-learning-snapshot"
# 2: signals carry ``last_seen_epoch`` and no stored freshness; a version-1
# snapshot reads as ``incompatible`` (unavailable, fail-open) and is rebuilt.
SNAPSHOT_VERSION = 2
SNAPSHOT_DIR = "learning-cache"
SNAPSHOT_NAME = "learning-snapshot.json"
# Bounds the read, and so the GIL-held parse that can overrun the budget. A
# lookup that is late (a heavily loaded machine) says so: ``timeout``.
MAX_SNAPSHOT_BYTES = 384_000
MAX_MODELS_PER_SCOPE = 256
MAX_SUMMARY_CHARS = 600

DEFAULT_BUDGET_MS = 25.0
MAX_BUDGET_MS = 100.0

# Lookup outcomes (``LearningSnapshot.status``).
AVAILABLE = "available"
MISSING = "missing"  # no snapshot yet: history may exist, the worker has not published
NO_HISTORY = "no_history"  # no snapshot and no (or an empty) runs.jsonl: genuinely nothing to learn from
UNREADABLE = "unreadable"
CORRUPT = "corrupt"
INCOMPATIBLE = "incompatible"
INCOMPLETE = "incomplete"
OVERSIZED = "oversized"
TIMEOUT = "timeout"  # not read within the budget (a zero budget included)
BUSY = "busy"  # an earlier read is still stalled on the filesystem
REPO_MISMATCH = "repo_mismatch"  # derived for another checkout root
IDENTITY_CHANGED = "identity_changed"  # the repository's identity source changed since derivation

# Lookups that mean the snapshot must be re-derived (``worker.nudge``).
REBUILD_STATUSES = frozenset({MISSING, UNREADABLE, CORRUPT, INCOMPATIBLE, INCOMPLETE,
                              REPO_MISMATCH, IDENTITY_CHANGED})

_reader_slot = threading.BoundedSemaphore(1)


def snapshot_path(store: Path) -> Path:
    return store / SNAPSHOT_DIR / SNAPSHOT_NAME


def stat_key(path: Path) -> list[int] | None:
    """``[size, mtime_ns]`` of *path*, or None when it cannot be stat'ed."""
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def _root_key(root: Path) -> str:
    return os.path.normcase(str(root.resolve()))


def identity_basis(root: Path) -> dict[str, Any]:
    """What the repository identity of *root* rests on, cheaply re-checkable (worker only).

    ``repo_key`` reads ``remote.origin.url`` from the git config in the
    repository's common git dir (shared by worktrees). Recording the root and
    that file's stat lets a lookup confirm, with ``stat`` calls and no git,
    that the identity cannot have changed.
    """
    from openshard.util.git import run_git

    config: Path | None = None
    out = run_git(root, ["rev-parse", "--git-common-dir"])
    if out and out.strip():
        common = Path(out.strip())
        config = (common if common.is_absolute() else root / common).resolve() / "config"
    return {
        "root": _root_key(root),
        "git_dir_present": (root / ".git").exists(),
        "git_config": str(config) if config is not None else None,
        "git_config_stat": stat_key(config) if config is not None else None,
    }


def _check_identity(raw: object, root: Path) -> None:
    if not isinstance(raw, dict) or raw.get("root") != _root_key(root):
        raise _Unusable(REPO_MISMATCH)
    if raw.get("git_dir_present") is not (root / ".git").exists():
        raise _Unusable(IDENTITY_CHANGED)
    config = raw.get("git_config")
    if config is not None and (not isinstance(config, str)
                               or stat_key(Path(config)) != raw.get("git_config_stat")):
        raise _Unusable(IDENTITY_CHANGED)


def lookup_budget_ms(config: object) -> float:
    """``learning.lookup_budget_ms`` from repository config: 25 by default, at most 100.

    Invalid values (non-numbers, booleans, NaN/inf) fall back to the default
    rather than becoming zero; a negative value is zero (skip the lookup).
    """
    raw = config.get("learning") if isinstance(config, dict) else None
    value = raw.get("lookup_budget_ms", DEFAULT_BUDGET_MS) if isinstance(raw, dict) else DEFAULT_BUDGET_MS
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return DEFAULT_BUDGET_MS
    try:
        number = float(value)
    except OverflowError:
        return DEFAULT_BUDGET_MS
    if not math.isfinite(number):
        return DEFAULT_BUDGET_MS
    return min(MAX_BUDGET_MS, max(0.0, number))


# --------------------------------------------------------------------------
# Building (background worker only)
# --------------------------------------------------------------------------


def _history_to_dict(h: HistoryEvidence) -> dict[str, Any]:
    models = [
        [m.model, m.verified, m.successes, m.cost_known, m.total_cost_usd]
        for m in sorted(h.per_model.values(), key=lambda m: m.model)
    ]
    return {
        "entries_scanned": h.entries_scanned,
        "models": models[:MAX_MODELS_PER_SCOPE],
        "truncated": len(models) > MAX_MODELS_PER_SCOPE,
    }


MAX_SCOPED_CATEGORIES = 64


def build_snapshot(lines: list[str], *, repo: str, harness: str, now: datetime | None = None,
                   identity: dict[str, Any] | None = None, source: list[int] | None = None,
                   routing_readable: bool = True) -> dict[str, Any]:
    """Everything an OSN run would derive from *lines* (a ``runs.jsonl``), precomputed. Pure.

    Signals: ``derive_signals`` over the most recent entries, as
    ``load_learning_index`` does. Routing: ``load_history_evidence`` over the
    first entries (harness-wide) and ``load_scoped_history`` per task category
    for *repo*, exactly as Routing V2 reads them. *identity*
    (``identity_basis``) is what lets a lookup trust *repo*; without it the
    snapshot is never used. *source* is the history's ``[size, mtime_ns]``
    at the copy, so a reader can tell the snapshot is behind.

    *routing_readable* False: the history is not strict UTF-8 where
    ``load_history_evidence`` reads it, so the live path would get no routing
    history at all (its loader raises and the run uses none). The snapshot
    then says routing history is unavailable rather than inventing one from a
    lenient decode. Signals are unaffected: the live path decodes them
    leniently too (``read_entries``).
    """
    from openshard.history.repo_identity import entry_matches_repo
    from openshard.routing.adaptive.outcome import outcome_from_receipt

    now = now or datetime.now(UTC)
    entries, bad = entries_from_lines(lines)
    index = derive_signals(entries, repo=repo, now=now, unreadable=bad)

    scoped: dict[str, Any] = {}
    by_category: dict[str, list] = {}
    try:
        for e in entries:
            if entry_matches_repo(e, repo):
                category = task_category_for(e)[0]
                if category:
                    by_category.setdefault(category, []).append(e)
        # Recorded categories are free text; keep the snapshot bounded (and readable:
        # the reader rejects more) with the best-evidenced ones. A category left out
        # falls back to harness-wide history, as ``load_scoped_history`` does on error.
        kept = sorted(by_category, key=lambda c: (-len(by_category[c]), c))[:MAX_SCOPED_CATEGORIES]
        scoped_truncated = len(by_category) > len(kept)
        by_category = {c: by_category[c] for c in kept}
        for category, members in sorted(by_category.items()):
            scoped[category] = _history_to_dict(
                build_history_evidence([outcome_from_receipt(e) for e in members], harness=harness)
            )
        scoped_error = False
    except Exception:
        # ``load_scoped_history`` falls back to the harness-wide evidence on any
        # error; the lookup does the same when this is set.
        scoped, scoped_error, scoped_truncated = {}, True, False

    return {
        "format": SNAPSHOT_FORMAT,
        "version": SNAPSHOT_VERSION,
        "complete": True,
        "availability": AVAILABLE,
        "generated_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": repo,
        "identity": identity,
        "source": source,
        "harness": harness,
        "signal_version": LEARNING_SIGNAL_VERSION,
        "index": {
            "entries_scanned": index.entries_scanned,
            "receipts_observed": index.receipts_observed,
            "receipts_with_evidence": index.receipts_with_evidence,
            "excluded": dict(index.excluded),
            "unreadable": index.unreadable,
            "signals_total": len(index.signals),
        },
        # Anecdotal and stale signals are never surfaced, and age only makes a
        # signal staler, so only the surfaceable ones are worth storing.
        "signals": [_signal_to_dict(s) for s in index.signals if s.surfaceable],
        "routing": {
            "broad": _history_to_dict(history_evidence_from_lines(lines, harness=harness)),
            "scoped": scoped,
            "scoped_error": scoped_error,
            "scoped_truncated": scoped_truncated,
        } if routing_readable else {"unreadable": True},
    }


def _epoch(value: str | None) -> int | None:
    ts = parse_timestamp(value)
    return int(ts.timestamp()) if ts is not None else None


def _signal_to_dict(s: LearningSignal) -> dict[str, Any]:
    body = s.to_dict()
    body.pop("version", None)  # the snapshot's ``signal_version`` covers every signal
    body.pop("freshness", None)  # recomputed at read time from ``last_seen_epoch``
    # Whole seconds: ``last_seen`` has second resolution, so this is exact and the
    # reader's whole-day arithmetic matches ``freshness_for`` on ``last_seen``.
    body["last_seen_epoch"] = _epoch(s.last_seen)
    return body


_STRENGTH_RANK = {STRENGTH_STRONG: 0, STRENGTH_MODERATE: 1}


def _keep_rank(raw: dict[str, Any]) -> tuple:
    """Trim order: strongest first, then most samples, then most recently seen."""
    last = raw.get("last_seen_epoch")
    samples = raw.get("samples")
    return (_STRENGTH_RANK.get(str(raw.get("strength")), 2), -(samples if isinstance(samples, int) else 0),
            -(last if isinstance(last, int) else 0), str(raw.get("signal_id")))


def _encode(snapshot: dict[str, Any]) -> bytes:
    body = dict(snapshot)
    body.pop("snapshot_id", None)
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    body["snapshot_id"] = "lsnap_" + digest[:24]
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def _trimmed(snapshot: dict[str, Any], keep: int) -> dict[str, Any]:
    """*snapshot* with only its *keep* best-ranked signals, in their derived order."""
    signals = snapshot["signals"]
    kept = set(sorted(range(len(signals)), key=lambda i: _keep_rank(signals[i]))[:keep])
    return {
        **snapshot,
        "signals": [s for i, s in enumerate(signals) if i in kept],
        "index": {**snapshot["index"], "signals_stored": keep, "signals_dropped": len(signals) - keep,
                  "trimmed": True},
    }


def fit_snapshot(snapshot: dict[str, Any]) -> bytes:
    """*snapshot* encoded within ``MAX_SNAPSHOT_BYTES``. Deterministic.

    Over the cap, signals are dropped weakest and stalest first (``_keep_rank``)
    until it fits; ``signals_total`` still counts every derived signal and
    ``signals_dropped`` how many were left out. Only if
    a snapshot with no signals cannot fit either is it a small, complete
    ``oversized`` marker, so the lookup stays bounded and says why it has
    nothing instead of serving an older snapshot as if it were current.
    """
    data = _encode(snapshot)
    if len(data) <= MAX_SNAPSHOT_BYTES:
        return data
    signals = snapshot.get("signals")
    if isinstance(signals, list) and isinstance(snapshot.get("index"), dict):
        # Estimate the longest prefix of the ranking that fits from per-signal
        # sizes, then confirm by encoding (stepping down on a rare overshoot).
        room = MAX_SNAPSHOT_BYTES - len(_encode(_trimmed(snapshot, 0))) - 64  # digits of signals_stored
        keep = 0
        for raw in sorted(signals, key=_keep_rank):
            room -= len(json.dumps(raw, separators=(",", ":"))) + 1
            if room < 0:
                break
            keep += 1
        while keep >= 0:
            data = _encode(_trimmed(snapshot, keep))
            if len(data) <= MAX_SNAPSHOT_BYTES:
                return data
            keep -= 1
    return _encode({
        "format": SNAPSHOT_FORMAT, "version": SNAPSHOT_VERSION, "complete": True,
        "availability": OVERSIZED, "generated_at": snapshot.get("generated_at"),
    })


def publish_snapshot(store: Path, snapshot: dict[str, Any]) -> str:
    """Atomically replace the published snapshot with *snapshot*, fitted to the cap; returns its id."""
    data = fit_snapshot(snapshot)
    directory = store / SNAPSHOT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f"{SNAPSHOT_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        replace_with_retry(temporary, directory / SNAPSHOT_NAME)
    finally:
        temporary.unlink(missing_ok=True)
    return str(json.loads(data)["snapshot_id"])


# --------------------------------------------------------------------------
# Reading (OSN startup)
# --------------------------------------------------------------------------


class _Unusable(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


def _str_list(value: object, *, limit: int, item_limit: int = 200) -> tuple[str, ...]:
    """A bounded list of strings. One C-level ``join`` checks every item is a
    string (anything else raises) and bounds the total size, with no per-item loop."""
    if type(value) is not list or len(value) > limit:
        raise _Unusable(CORRUPT)
    try:
        size = len("".join(value))
    except TypeError:
        raise _Unusable(CORRUPT) from None
    if size > limit * item_limit:
        raise _Unusable(CORRUPT)
    return tuple(value)


def _opt_str(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise _Unusable(CORRUPT)


_DAY_SECONDS = 86400
# Every key a stored signal must have (``_signal_to_dict``), fetched in one C call.
_SIGNAL_FIELDS = operator.itemgetter(
    "kind", "signal_id", "summary", "samples", "strength", "subject", "stats", "last_seen_epoch",
    "repo", "task_category", "first_seen", "last_seen",
    "evidence_sources", "receipt_ids", "shard_ids", "terms", "areas",
)
_OPTIONAL_STR = (str, type(None))


def _signal(raw: object, now_ts: float) -> LearningSignal:
    """One stored signal, validated for structure and types (what later code relies on).

    Kept deliberately cheap: it runs once per stored signal inside the lookup
    budget. Freshness is recomputed from the integer ``last_seen_epoch``.
    """
    try:
        (kind, sid, summary, samples, strength, subject, stats, epoch, repo, category, first_seen, last_seen,
         sources, receipts, shards, terms, areas) = _SIGNAL_FIELDS(raw)  # type: ignore[arg-type]
    except (TypeError, KeyError):
        raise _Unusable(CORRUPT) from None
    if (type(raw) is not dict or kind not in KINDS or type(sid) is not str or type(summary) is not str
            or len(summary) > MAX_SUMMARY_CHARS or type(strength) is not str
            or type(samples) is not int or samples < 0
            or type(subject) is not dict or type(stats) is not dict
            or (epoch is not None and type(epoch) is not int)
            or type(repo) not in _OPTIONAL_STR or type(category) not in _OPTIONAL_STR
            or type(first_seen) not in _OPTIONAL_STR or type(last_seen) not in _OPTIONAL_STR):
        raise _Unusable(CORRUPT)
    # As of this read, not the derive: whole days since last seen, as ``freshness_for``.
    freshness = STALE if epoch is None else freshness_for_age(int((now_ts - epoch) // _DAY_SECONDS))
    # The parsed dicts belong to this read alone, so they are used as they are.
    return LearningSignal(
        sid, kind, repo, category, subject, samples, stats, strength, freshness,
        _str_list(sources, limit=8), _str_list(receipts, limit=10), _str_list(shards, limit=10),
        first_seen, last_seen, _str_list(terms, limit=16, item_limit=24), _str_list(areas, limit=6),
        summary,
    )


def _count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _Unusable(CORRUPT)
    return value


def _history(raw: object, harness: str, snapshot_id: str) -> HistoryEvidence:
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), list):
        raise _Unusable(CORRUPT)
    rows = raw["models"]
    if len(rows) > MAX_MODELS_PER_SCOPE:
        raise _Unusable(CORRUPT)
    per_model: dict[str, ModelHistory] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 5 or not isinstance(row[0], str):
            raise _Unusable(CORRUPT)
        model, verified, successes, cost_known = row[0], _count(row[1]), _count(row[2]), _count(row[3])
        total = row[4]
        if total is not None and (not isinstance(total, (int, float)) or isinstance(total, bool)):
            raise _Unusable(CORRUPT)
        per_model[model] = ModelHistory(model, harness, verified, successes, cost_known,
                                        float(total) if total is not None else None)
    return HistoryEvidence(
        per_model=per_model,
        harness=harness,
        entries_scanned=_count(raw.get("entries_scanned")),
        availability="truncated" if raw.get("truncated") is True else AVAILABLE,
        snapshot_id=snapshot_id,
    )


@dataclass(frozen=True)
class LearningSnapshot:
    """One frozen lookup result. An OSN run takes exactly one, at startup, and uses
    it for routing history, the model's context and the Receipt's provenance."""

    status: str
    lookup_ms: float
    budget_ms: float
    index: LearningIndex | None = None
    repo: str | None = None
    harness: str | None = None
    snapshot_id: str | None = None
    generated_at: str | None = None
    routing: dict[str, Any] | None = None
    source: list[int] | None = None  # the history's [size, mtime_ns] when it was derived
    # Set when the derivation was trimmed to fit the size cap: how many signals were
    # derived in all, and how many were stored (and so could be considered).
    signals_derived: int | None = None
    signals_stored: int | None = None

    @property
    def trimmed(self) -> bool:
        return self.signals_derived is not None

    @property
    def available(self) -> bool:
        return self.status == AVAILABLE

    @property
    def learning_status(self) -> str:
        """The Learning Loop status this lookup implies when it is not available."""
        if self.status == NO_HISTORY:
            return STATUS_NO_HISTORY
        return STATUS_TIMEOUT if self.status in (TIMEOUT, BUSY) else STATUS_UNAVAILABLE

    def record(self) -> dict[str, Any]:
        """Provenance for the Receipt's ``learning`` block (no history content)."""
        return {
            "source": "local_snapshot",
            "status": self.status,
            "snapshot_id": self.snapshot_id,
            "generated_at": self.generated_at,
            "lookup_ms": round(self.lookup_ms, 3),
            "budget_ms": self.budget_ms,
            **({"trimmed": True, "signals_stored": self.signals_stored, "signals_derived": self.signals_derived}
               if self.trimmed else {}),
        }

    def consult(self, task: str, *, current_check_fingerprint: str | None = None,
                model: str | None = None) -> LearningContext:
        """``retrieval.consult`` over this frozen index. Never raises, never reads a file."""
        if not self.available:
            try:
                shape = task_shape_for(task, None)
            except Exception:
                shape = None
            if self.status == NO_HISTORY:  # known empty: zero is the truth here
                return LearningContext(STATUS_NO_HISTORY, shape)
            # Nothing was read: how many signals or Receipts exist is unknown, not zero.
            return LearningContext(self.learning_status, shape, signals_considered=None,
                                   receipts_with_evidence=None)
        try:
            return consult(task, self.index, repo=self.repo,
                           current_check_fingerprint=current_check_fingerprint, model=model)
        except Exception as exc:  # consult never raises; belt and braces
            return LearningContext(STATUS_ERROR, None, signals_considered=None, receipts_with_evidence=None,
                                   error=type(exc).__name__)

    def history(self, task_category: str | None, *, harness: str) -> HistoryEvidence | ScopedHistoryEvidence:
        """Routing V2 history from this snapshot, as ``load_scoped_history`` would give it.

        Unavailable, late or mismatched snapshots give history that *says so*
        (``history_unavailable`` / ``history_timeout``), never empty history.
        """
        if self.status == NO_HISTORY:
            return HistoryEvidence(harness=harness)  # known empty: the gate says no_history, truthfully
        if not self.available or self.routing is None or self.harness != harness:
            reason = "timeout" if self.status in (TIMEOUT, BUSY) else "unavailable"
            return HistoryEvidence(harness=harness, entries_scanned=None, availability=reason,
                                   snapshot_id=self.snapshot_id)
        if self.routing.get("unreadable") is True:  # the live loader would have had none either
            return HistoryEvidence(harness=harness, entries_scanned=None, availability="unavailable",
                                   snapshot_id=self.snapshot_id)
        sid = self.snapshot_id or ""
        broad = _history(self.routing.get("broad"), harness, sid)
        if not self.repo or not task_category or self.routing.get("scoped_error") is True:
            return broad
        scoped_raw = self.routing.get("scoped")
        raw = scoped_raw.get(task_category) if isinstance(scoped_raw, dict) else None  # validated by _read
        if raw is None and self.routing.get("scoped_truncated") is True:
            return broad  # left out to bound the snapshot: unknown, so harness-wide only
        scoped = (
            _history(raw, harness, sid) if raw is not None
            else HistoryEvidence(harness=harness, entries_scanned=0, snapshot_id=sid)
        )
        return ScopedHistoryEvidence(scoped, broad, repo=self.repo, task_category=task_category)


def _read(path: Path, now: datetime) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_SNAPSHOT_BYTES + 1)
    except FileNotFoundError:
        # One stat, not a read: with no history at all there is nothing to wait for.
        try:
            history_size = (path.parent.parent / "runs.jsonl").stat().st_size
        except FileNotFoundError:
            history_size = 0
        except OSError:
            raise _Unusable(MISSING) from None
        raise _Unusable(NO_HISTORY if history_size == 0 else MISSING) from None
    except OSError:
        raise _Unusable(UNREADABLE) from None
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise _Unusable(OVERSIZED)
    try:
        raw = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise _Unusable(CORRUPT) from None
    if not isinstance(raw, dict) or raw.get("format") != SNAPSHOT_FORMAT:
        raise _Unusable(INCOMPATIBLE)
    if raw.get("version") != SNAPSHOT_VERSION:
        raise _Unusable(INCOMPATIBLE)
    if raw.get("complete") is not True:
        raise _Unusable(INCOMPLETE)
    if raw.get("availability") != AVAILABLE:
        raise _Unusable(OVERSIZED if raw.get("availability") == OVERSIZED else INCOMPLETE)
    if raw.get("signal_version") != LEARNING_SIGNAL_VERSION:
        raise _Unusable(INCOMPATIBLE)
    _check_identity(raw.get("identity"), path.parent.parent.parent)
    meta, signals, routing = raw.get("index"), raw.get("signals"), raw.get("routing")
    if not isinstance(meta, dict) or not isinstance(signals, list) or not isinstance(routing, dict):
        raise _Unusable(CORRUPT)
    excluded = meta.get("excluded")
    if not isinstance(excluded, dict) or any(
        not isinstance(k, str) or not isinstance(v, int) for k, v in excluded.items()
    ):
        raise _Unusable(CORRUPT)
    snapshot_id, repo, harness = raw.get("snapshot_id"), raw.get("repo"), raw.get("harness")
    if not isinstance(snapshot_id, str) or not isinstance(repo, str) or not isinstance(harness, str):
        raise _Unusable(CORRUPT)
    now_ts = now.timestamp()
    total = _count(meta.get("signals_total"))
    trimmed: dict[str, int] = {}
    if meta.get("trimmed") is True:
        stored, dropped = _count(meta.get("signals_stored")), _count(meta.get("signals_dropped"))
        if stored != len(signals) or dropped > total:
            raise _Unusable(CORRUPT)
        trimmed = {"signals_derived": total, "signals_stored": stored}
        # Signals dropped to fit were never scored: "considered" counts only the rest.
        total -= dropped
    index = LearningIndex(
        repo=repo,
        signals=tuple([_signal(s, now_ts) for s in signals]),
        entries_scanned=_count(meta.get("entries_scanned")),
        receipts_observed=_count(meta.get("receipts_observed")),
        receipts_with_evidence=_count(meta.get("receipts_with_evidence")),
        excluded=dict(excluded),
        unreadable=_count(meta.get("unreadable")),
        signals_total=total,
    )
    # Validate routing now, so a corrupt snapshot fails here, inside the budget,
    # and never half-way through a routing decision.
    if routing.get("unreadable") is not True:
        _history(routing.get("broad"), harness, snapshot_id)
        scoped = routing.get("scoped")
        if not isinstance(scoped, dict) or len(scoped) > MAX_SCOPED_CATEGORIES:
            raise _Unusable(CORRUPT)
        for value in scoped.values():
            _history(value, harness, snapshot_id)
    source = raw.get("source")
    if source is not None and (type(source) is not list or len(source) != 2
                               or any(type(v) is not int for v in source)):
        raise _Unusable(CORRUPT)
    return {"index": index, "repo": repo, "harness": harness, "snapshot_id": snapshot_id,
            "generated_at": _opt_str(raw.get("generated_at")), "routing": routing, "source": source,
            **trimmed}


def lookup_snapshot(store: Path, *, budget_ms: float = DEFAULT_BUDGET_MS,
                    now: datetime | None = None) -> LearningSnapshot:
    """One bounded, synchronous read of the published snapshot. Never raises.

    The read runs on a daemon thread and the caller stops waiting at
    *budget_ms*: a stalled filesystem delays a run by the budget and no more,
    and a stalled read occupies the single reader slot instead of piling up
    threads. Best effort, not realtime: a JSON parse in progress holds the GIL
    and finishes first (bounded by ``MAX_SNAPSHOT_BYTES``); a result that
    arrives after the budget is reported as ``timeout`` and not used.
    """
    start = time.perf_counter()
    now = now or datetime.now(UTC)

    def done(status: str, **found: Any) -> LearningSnapshot:
        return LearningSnapshot(status, (time.perf_counter() - start) * 1000, budget_ms, **found)

    if budget_ms <= 0:
        return done(TIMEOUT)
    if not _reader_slot.acquire(blocking=False):
        return done(BUSY)
    results: queue.Queue = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            value: Any = _read(snapshot_path(store), now)
        except _Unusable as exc:
            value = exc.status
        except Exception:
            value = CORRUPT
        finally:
            _reader_slot.release()
        results.put(value)

    try:
        threading.Thread(target=read, daemon=True, name="learning-snapshot").start()
    except Exception:
        _reader_slot.release()
        return done(UNREADABLE)
    try:
        value = results.get(timeout=max(0.0, budget_ms / 1000 - (time.perf_counter() - start)))
    except queue.Empty:
        return done(TIMEOUT)
    if (time.perf_counter() - start) * 1000 >= budget_ms:
        return done(TIMEOUT)  # it arrived, but after the budget: not used
    if isinstance(value, str):
        return done(value)
    return done(AVAILABLE, **value)


__all__ = [
    "AVAILABLE",
    "REBUILD_STATUSES",
    "DEFAULT_BUDGET_MS",
    "MAX_BUDGET_MS",
    "MAX_SNAPSHOT_BYTES",
    "LearningSnapshot",
    "build_snapshot",
    "identity_basis",
    "lookup_budget_ms",
    "lookup_snapshot",
    "publish_snapshot",
    "snapshot_path",
    "stat_key",
]
