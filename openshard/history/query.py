"""Read/query layer over the local run history (``.openshard/runs.jsonl``).

This is the retrieval surface an OpenShard MCP server (or any other read
consumer) uses to fetch canonical Shards and Receipts. It adds no storage of
its own: every call loads the existing store via ``metrics.load_runs`` and
builds canonical objects with ``build_shard_receipt`` / ``build_shard``.

    runs.jsonl -> load_runs -> group attempts by shard_id -> Shard / Receipt

Multi-attempt Shards
--------------------
Several run entries can share one persisted ``shard_id`` (see
``run_attempt.py``). This module always groups them and treats the
*latest attempt* as the Shard's current state: highest ``attempt_number``,
ties broken by later position in the file (append order is chronological).
``list_shards`` therefore never lists one Shard twice, and ``get_receipt``
defaults to the latest attempt's receipt unless a ``run_id`` is given.

Legacy entries with no persisted ``shard_id`` get the same render-time id
``build_shard_receipt`` derives from their file position, so they remain
addressable exactly as the CLI already addresses them.

Privacy
-------
Only existing structured evidence is returned (canonical ``Shard`` /
``ShardReceipt``). Search reads task text, shard id, agent label and
verification status only — never summaries, notes, transcripts, diffs,
stdout/stderr or any blocked field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from openshard.history.metrics import load_runs
from openshard.history.repo_identity import entry_matches_repo
from openshard.history.run_attempt import UnknownShardError
from openshard.history.shard import Shard
from openshard.history.shard_contract import ShardReceipt, _make_shard_id, build_shard_receipt

__all__ = [
    "SearchHit",
    "UnknownRunError",
    "UnknownShardError",
    "get_receipt",
    "get_shard",
    "list_shards",
    "search_history",
]

DEFAULT_LIMIT = 20

# Fields search may read. Deliberately excludes summary/notes/agent_notes and
# every free-text field that could carry model output or private content.
SEARCH_FIELDS: tuple[str, ...] = ("task_short", "task_full", "shard_id", "agent", "status")


class UnknownRunError(ValueError):
    """Raised when no run entry matches the requested ``run_id``.

    Mirrors ``UnknownShardError``: fails closed rather than returning an
    unrelated run.
    """


@dataclass
class SearchHit:
    """One ``search_history`` result: the canonical Shard plus why it matched."""

    shard: Shard
    score: int
    matched_fields: list[str] = field(default_factory=list)
    status: str = ""
    repo: str | None = None


@dataclass
class _ShardGroup:
    """All persisted attempts of one Shard, in file order."""

    shard_id: str
    attempts: list[tuple[int, dict]]  # (file_index, entry)

    @property
    def latest(self) -> tuple[int, dict]:
        """The attempt representing the Shard's current state.

        Highest ``attempt_number`` wins; a missing/invalid number counts as 1.
        Ties (or legacy entries without numbers) resolve to the later entry in
        the file, matching the CLI's ``entries[-1]`` convention.
        """
        def _key(item: tuple[int, dict]) -> tuple[int, int]:
            idx, entry = item
            n = entry.get("attempt_number")
            return (n if isinstance(n, int) else 1, idx)

        return max(self.attempts, key=_key)

    @property
    def sort_key(self) -> tuple[str, int]:
        """Newest-first ordering key: latest attempt's timestamp, then file position."""
        idx, entry = self.latest
        ts = entry.get("timestamp")
        return (ts if isinstance(ts, str) else "", idx)


def _entry_shard_id(entry: dict, index: int) -> str:
    """The id ``build_shard_receipt(entry, index)`` would assign this entry."""
    persisted = entry.get("shard_id")
    if isinstance(persisted, str) and persisted:
        return persisted
    return _make_shard_id(entry.get("timestamp") or "", index)


def _group_entries(entries: list[dict]) -> list[_ShardGroup]:
    """Group run entries by shard_id, preserving first-seen order."""
    groups: dict[str, _ShardGroup] = {}
    for idx, entry in enumerate(entries):
        sid = _entry_shard_id(entry, idx)
        group = groups.get(sid)
        if group is None:
            group = _ShardGroup(shard_id=sid, attempts=[])
            groups[sid] = group
        group.attempts.append((idx, entry))
    return list(groups.values())


def _load_groups(repo_path: Path | None, repo: str | None) -> list[_ShardGroup]:
    """Load, group, filter by repo, and order newest-first."""
    groups = _group_entries(load_runs(repo_path))
    if repo:
        groups = [g for g in groups if entry_matches_repo(g.latest[1], repo)]
    groups.sort(key=lambda g: g.sort_key, reverse=True)
    return groups


def _receipt_for(group: _ShardGroup, attempt: tuple[int, dict] | None = None) -> ShardReceipt:
    idx, entry = attempt if attempt is not None else group.latest
    return build_shard_receipt(entry, index=idx)


def _shard_for(group: _ShardGroup) -> Shard:
    receipt = _receipt_for(group)
    # build_shard_receipt always embeds a Shard; fall back defensively so a
    # caller never gets None for a canonical object.
    if receipt.shard is not None:
        return receipt.shard
    return Shard(
        shard_id=receipt.shard_id,
        created_at=receipt.created_at,
        task_short=receipt.task_short,
        task_full=receipt.task_full,
        agent=receipt.agent,
        origin="unknown",
        capture_depth="unknown",
    )


def _find_group(groups: list[_ShardGroup], shard_id: str) -> _ShardGroup:
    for group in groups:
        if group.shard_id == shard_id:
            return group
    raise UnknownShardError(
        f"No existing Shard found with id '{shard_id}'. "
        "Shards are created automatically by a run; use list_shards() to see "
        "the ids recorded in this repository's history."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_shards(
    *,
    limit: int = DEFAULT_LIMIT,
    repo: str | None = None,
    repo_path: Path | None = None,
) -> list[Shard]:
    """Return the most recent canonical Shards, newest first.

    Each Shard appears once even when it has several attempts; the returned
    object reflects the latest attempt. ``repo`` filters by canonical
    repository identity, any remote-URL form, or legacy folder name (see
    ``repo_identity.entry_matches_repo``). ``repo_path`` is the checkout whose
    ``.openshard/runs.jsonl`` to read (defaults to the current directory, as
    ``load_runs`` does). Empty history returns ``[]``.
    """
    if limit <= 0:
        return []
    groups = _load_groups(repo_path, repo)
    return [_shard_for(g) for g in groups[:limit]]


def get_shard(shard_id: str, *, repo_path: Path | None = None) -> Shard:
    """Return the canonical Shard for *shard_id* (latest attempt's state).

    Raises ``UnknownShardError`` when no persisted entry carries that id;
    never falls back to an unrelated run.
    """
    groups = _group_entries(load_runs(repo_path))
    return _shard_for(_find_group(groups, shard_id))


def get_receipt(
    shard_id: str | None = None,
    *,
    run_id: str | None = None,
    repo_path: Path | None = None,
) -> ShardReceipt:
    """Return the canonical ShardReceipt for a Shard or a specific run.

    * ``shard_id`` only: the receipt of the Shard's latest attempt.
    * ``run_id`` only: the receipt of that exact run (``run_id`` falls back to
      ``timestamp`` for entries written before run ids existed, matching
      ``build_shard_receipt``).
    * both: the run must belong to that Shard, otherwise ``UnknownRunError``.

    Receipts are built with ``build_shard_receipt`` and are unchanged from
    what the CLI renders. Raises ``UnknownShardError`` / ``UnknownRunError``
    rather than returning a different run.
    """
    if not shard_id and not run_id:
        raise ValueError("get_receipt() requires a shard_id or a run_id")

    entries = load_runs(repo_path)
    groups = _group_entries(entries)

    if shard_id:
        group = _find_group(groups, shard_id)
        if run_id is None:
            return _receipt_for(group)
        for attempt in group.attempts:
            if _entry_run_id(attempt[1]) == run_id:
                return _receipt_for(group, attempt)
        raise UnknownRunError(
            f"No run '{run_id}' found under Shard '{shard_id}'."
        )

    for group in groups:
        for attempt in group.attempts:
            if _entry_run_id(attempt[1]) == run_id:
                return _receipt_for(group, attempt)
    raise UnknownRunError(f"No run found with id '{run_id}'.")


def _entry_run_id(entry: dict) -> str:
    return str(entry.get("run_id") or entry.get("timestamp") or "")


def _searchable_fields(receipt: ShardReceipt) -> dict[str, str]:
    """The lower-cased text search is allowed to look at, keyed by field name."""
    return {
        "task_short": (receipt.task_short or "").lower(),
        "task_full": (receipt.task_full or "").lower(),
        "shard_id": (receipt.shard_id or "").lower(),
        "agent": (receipt.agent or "").lower(),
        "status": (receipt.status or "").lower(),
    }


def search_history(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    repo: str | None = None,
    repo_path: Path | None = None,
) -> list[SearchHit]:
    """Simple, deterministic local search over canonical Shards.

    Matching rule: the query is split on whitespace into terms; a Shard
    matches when **every** term is a case-insensitive substring of at least
    one of ``SEARCH_FIELDS`` (task_short, task_full, shard_id, agent,
    status), evaluated on the Shard's latest attempt.

    Ordering: score descending, where score is the number of (term, field)
    pairs that matched; ties resolve newest first. Plain substring matching —
    no embeddings, fuzzy matching, or model calls. An empty query or no
    matches returns ``[]``.
    """
    terms = [t for t in (query or "").lower().split() if t]
    if not terms or limit <= 0:
        return []

    hits: list[tuple[int, int, SearchHit]] = []
    groups = _load_groups(repo_path, repo)  # newest first
    for position, group in enumerate(groups):
        receipt = _receipt_for(group)
        fields = _searchable_fields(receipt)
        score = 0
        matched: list[str] = []
        for term in terms:
            term_hit = False
            for name in SEARCH_FIELDS:
                if term in fields[name]:
                    score += 1
                    term_hit = True
                    if name not in matched:
                        matched.append(name)
            if not term_hit:
                score = 0
                break
        if score == 0:
            continue
        shard = receipt.shard or _shard_for(group)
        hits.append((score, position, SearchHit(
            shard=shard,
            score=score,
            matched_fields=matched,
            status=receipt.status,
            repo=receipt.repo,
        )))

    # Higher score first; among equal scores keep newest-first (lower position).
    hits.sort(key=lambda h: (-h[0], h[1]))
    return [h[2] for h in hits[:limit]]
