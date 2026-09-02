"""RunAttempt: one execution of a Shard's durable engineering task.

A Shard (see ``shard.py``) is the task; a RunAttempt is one execution of it.
Multiple RunAttempts can reference the same persistent ``shard_id`` — a retry,
or a different agent/executor picking up the same task. Linkage is always
explicit (an existing, persisted ``shard_id`` supplied by the caller), never
inferred from task text or timing, so unrelated historical runs are never
silently grouped together.
"""

from __future__ import annotations

from dataclasses import dataclass

from openshard.history.shard import Shard
from openshard.history.shard_contract import _make_shard_id


class UnknownShardError(ValueError):
    """Raised when a caller asks to attach a new attempt to a shard_id that
    has no persisted match in run history.

    Deliberately fails closed: there is nothing durable to attach to, and
    guessing would invent a relationship between unrelated runs.
    """


@dataclass
class RunAttempt:
    """One execution of a Shard's task. No proof/evidence fields — see ShardReceipt."""

    run_id: str
    shard_id: str
    attempt_number: int
    created_at: str
    retry_triggered: bool
    agent: str
    origin: str
    capture_depth: str


def build_run_attempt(entry: dict, shard: Shard) -> RunAttempt:
    """Build the RunAttempt for a raw run entry. Never raises.

    Reuses the Shard already built for this entry (via ``build_shard``) so
    agent/origin/capture_depth stay in sync between the two.
    """
    return RunAttempt(
        run_id=entry.get("run_id") or entry.get("timestamp") or "",
        shard_id=shard.shard_id,
        attempt_number=entry.get("attempt_number") if isinstance(entry.get("attempt_number"), int) else 1,
        created_at=shard.created_at,
        retry_triggered=bool(entry.get("retry_triggered")),
        agent=shard.agent,
        origin=shard.origin,
        capture_depth=shard.capture_depth,
    )


def resolve_shard_for_attempt(
    requested_shard_id: str | None,
    existing_entries: list[dict],
    timestamp: str,
    run_index: int | None,
) -> tuple[str, int]:
    """Resolve the ``(shard_id, attempt_number)`` for a new attempt being persisted.

    If ``requested_shard_id`` is given, it must match a persisted ``shard_id``
    on at least one entry already in ``existing_entries`` (i.e. an entry
    written by this or a prior migration's write-time stamping — never a
    render-time-only reconstruction). On a match, the new attempt reuses that
    shard_id and becomes the next attempt number. On no match, raises
    ``UnknownShardError`` rather than silently starting a new chain under a
    caller-supplied id.

    If ``requested_shard_id`` is not given, a fresh shard_id is minted exactly
    as before this migration (``_make_shard_id``), and this is attempt 1 of a
    new Shard.
    """
    if requested_shard_id:
        matches = [e for e in existing_entries if e.get("shard_id") == requested_shard_id]
        if not matches:
            raise UnknownShardError(
                f"No existing Shard found with id '{requested_shard_id}'. "
                "Shards are created automatically by a run; pass the Shard ID "
                "from a previous run's receipt to attach a new attempt to it."
            )
        return requested_shard_id, len(matches) + 1
    return _make_shard_id(timestamp, run_index), 1
