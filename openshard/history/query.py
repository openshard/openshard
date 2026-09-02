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
stdout/stderr or any blocked field. ``relevant_context`` reuses the same
receipt objects but additionally drops every Note-severity finding, since
that severity is where free-form ``agent_notes`` content lands (see
``shard_contract._extract_findings``) alongside genuinely structured
warnings, and the two cannot be told apart at the finding level. Only
Critical/High/Medium/Low findings — which originate from actual review/
verification results, never raw notes — cross into relevant context.

Task-aware relevance (relevant_context)
----------------------------------------
``relevant_context`` ranks Shards for a free-text task using simple,
deterministic keyword-overlap scoring over task text/shard id/agent, plus
small bonuses for a shard with a recorded verification failure or more than
one attempt (prior failures and retries are the most useful history for an
agent starting a similar task). No embeddings, fuzzy matching, or model
calls — see ``_score_group``. Like ``search_history``, this loads and
groups ``runs.jsonl`` exactly once per call; unlike it, per-attempt
``ShardReceipt`` objects are only built for the shards that actually make
the final ranked result, not for every group in history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from openshard.history.metrics import load_runs
from openshard.history.repo_identity import entry_matches_repo
from openshard.history.run_attempt import UnknownShardError
from openshard.history.shard import Shard, derive_shard_identity
from openshard.history.shard_contract import (
    ShardFinding,
    ShardReceipt,
    _make_shard_id,
    build_shard_receipt,
)

__all__ = [
    "DEFAULT_CONTEXT_LIMIT",
    "RelevantAttempt",
    "RelevantContext",
    "RelevantMatch",
    "SearchHit",
    "UnknownRunError",
    "UnknownShardError",
    "get_receipt",
    "get_shard",
    "list_shards",
    "relevant_context",
    "search_history",
]

DEFAULT_LIMIT = 20
DEFAULT_CONTEXT_LIMIT = 5

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


# ---------------------------------------------------------------------------
# relevant_context
# ---------------------------------------------------------------------------

# Deliberately small and generic — this is normalization, not NLP. Words that
# would otherwise dominate every task's token set without adding relevance.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "by", "with", "is", "are", "was", "were", "be", "been", "being", "this",
    "that", "these", "those", "it", "its", "as", "from", "into", "onto", "up",
    "down", "out", "about", "than", "then", "so", "not", "no", "do", "does",
    "did", "can", "could", "should", "would", "will", "shall", "may", "might",
    "must", "have", "has", "had", "i", "we", "you", "they", "he", "she",
})
_WORD_RE = re.compile(r"[a-z0-9]+")

# Per-field keyword weights and fixed bonuses. Kept explicit and small so a
# match's score is always explainable from its `signals` list.
_WEIGHT_TASK = 2
_WEIGHT_SHARD_ID = 1
_WEIGHT_AGENT = 1
_BONUS_FAILURE = 2
_BONUS_MULTI_ATTEMPT = 1

_MAX_CONTEXT_FILES = 8
_MAX_CONTEXT_FINDINGS = 5
_MAX_CONTEXT_ATTEMPTS = 5


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop trivial stopwords."""
    return [t for t in _WORD_RE.findall((text or "").lower()) if t not in _STOPWORDS]


@dataclass
class RelevantAttempt:
    """One attempt of a matched Shard, bounded to status/verification only."""

    run_id: str
    attempt_number: int | None
    status: str
    verification_status: str
    verification_reason: str


@dataclass
class RelevantMatch:
    """One Shard relevant_context judged relevant, plus why and its attempt history."""

    shard: Shard
    score: int
    signals: list[str]
    status: str
    verification_status: str
    verification_reason: str
    result: str
    repo: str | None
    files: list[str] = field(default_factory=list)
    findings: list[ShardFinding] = field(default_factory=list)
    attempts: list[RelevantAttempt] = field(default_factory=list)


@dataclass
class RelevantContext:
    """Bounded result of relevant_context: ranked matches plus injectable text."""

    task: str
    matches: list[RelevantMatch]
    context_text: str


def _group_has_failure(group: _ShardGroup) -> bool:
    """True if any attempt of *group* recorded a verification or check failure."""
    for _, entry in group.attempts:
        if entry.get("verification_passed") is False:
            return True
        osn = entry.get("osn_verification_contract")
        if isinstance(osn, dict) and str(osn.get("status") or "") == "failed":
            return True
        checks = entry.get("review_checks")
        if isinstance(checks, list) and any(
            isinstance(c, dict) and c.get("status") == "failed" for c in checks
        ):
            return True
    return False


def _score_group(query_terms: list[str], group: _ShardGroup, entry: dict) -> tuple[int, list[str]]:
    """Deterministic keyword-overlap score for one Shard group against *query_terms*.

    Reads only the latest attempt's ``task``/agent identity and the group's
    own attempt records — never a built ShardReceipt, so scoring every group
    in history costs no extra I/O or receipt-construction work. Returns
    ``(0, [])`` when no query term overlaps the task text, shard id, or agent
    at all: relevance always requires topical overlap, never failure/retry
    signals alone (those only ever add to an already-relevant match).
    """
    task_tokens = set(_tokenize(str(entry.get("task") or "")))
    shard_id_tokens = set(re.split(r"[^a-z0-9]+", group.shard_id.lower()))
    agent, _, _ = derive_shard_identity(entry)
    agent_tokens = set(_tokenize(agent))

    matched: list[str] = []
    keyword_score = 0
    for term in query_terms:
        hit = False
        if term in task_tokens:
            keyword_score += _WEIGHT_TASK
            hit = True
        if term in shard_id_tokens:
            keyword_score += _WEIGHT_SHARD_ID
            hit = True
        if term in agent_tokens:
            keyword_score += _WEIGHT_AGENT
            hit = True
        if hit:
            matched.append(term)

    if keyword_score <= 0:
        return 0, []

    signals = [f"task overlap: {', '.join(matched)}"]
    bonus = 0
    if _group_has_failure(group):
        bonus += _BONUS_FAILURE
        signals.append("prior verification failure")
    if len(group.attempts) > 1:
        bonus += _BONUS_MULTI_ATTEMPT
        signals.append(f"multiple attempts ({len(group.attempts)})")

    return keyword_score + bonus, signals


def _bounded_attempts(attempts: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
    """Chronological attempts, capped to _MAX_CONTEXT_ATTEMPTS.

    Keeps the earliest attempts plus the final one when there are more than
    the cap, so a shard's eventual outcome is never dropped for one lost in
    the middle of a long retry chain.
    """
    ordered = sorted(attempts, key=lambda item: item[0])
    if len(ordered) <= _MAX_CONTEXT_ATTEMPTS:
        return ordered
    return ordered[: _MAX_CONTEXT_ATTEMPTS - 1] + [ordered[-1]]


def _build_relevant_match(group: _ShardGroup, score: int, signals: list[str]) -> RelevantMatch:
    receipt = _receipt_for(group)
    shard = receipt.shard or _shard_for(group)

    attempts: list[RelevantAttempt] = []
    for attempt in _bounded_attempts(group.attempts):
        _, entry = attempt
        a_receipt = receipt if attempt is group.latest else _receipt_for(group, attempt)
        attempts.append(RelevantAttempt(
            run_id=_entry_run_id(entry),
            attempt_number=a_receipt.attempt_number,
            status=a_receipt.status,
            verification_status=a_receipt.verification_status,
            verification_reason=a_receipt.verification_reason,
        ))

    findings = [f for f in receipt.findings if f.severity != "Note"][:_MAX_CONTEXT_FINDINGS]

    return RelevantMatch(
        shard=shard,
        score=score,
        signals=signals,
        status=receipt.status,
        verification_status=receipt.verification_status,
        verification_reason=receipt.verification_reason,
        result=receipt.result,
        repo=receipt.repo,
        files=list(receipt.files_touched[:_MAX_CONTEXT_FILES]),
        findings=findings,
        attempts=attempts,
    )


def _no_match_text(task: str) -> str:
    stripped = task.strip()
    header = f'Relevant OpenShard context for: "{stripped}"' if stripped else "Relevant OpenShard context"
    if not stripped:
        return header + "\n\nNo task given — nothing to match against local OpenShard history.\n"
    return header + "\n\nNo relevant prior OpenShard history found for this task.\n"


def _render_context_text(task: str, matches: list[RelevantMatch]) -> str:
    """Render a compact, deterministic text block a coding agent can consume.

    Only reproduces bounded, already-privacy-approved receipt fields (status,
    verification, result, file paths, non-Note findings) — never a full
    receipt dump.
    """
    if not matches:
        return _no_match_text(task)

    lines = [f'Relevant OpenShard context for: "{task.strip()}"', ""]
    for i, m in enumerate(matches, start=1):
        lines.append(f"{i}. Shard {m.shard.shard_id} — {m.shard.task_short}")
        v = f" | Verification: {m.verification_status}" if m.verification_status else ""
        lines.append(f"   Status: {m.status}{v}")
        if m.result:
            lines.append(f"   Result: {m.result}")
        if m.signals:
            lines.append(f"   Why relevant: {'; '.join(m.signals)}")
        if len(m.attempts) > 1:
            attempt_summary = ", ".join(
                f"{a.attempt_number if a.attempt_number is not None else n}: {a.status}"
                for n, a in enumerate(m.attempts, start=1)
            )
            lines.append(f"   Attempts: {attempt_summary}")
        if m.files:
            lines.append(f"   Files: {', '.join(m.files)}")
        for f in m.findings:
            lines.append(f"   [{f.severity}] {f.message}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def relevant_context(
    task: str,
    *,
    limit: int = DEFAULT_CONTEXT_LIMIT,
    repo: str | None = None,
    repo_path: Path | None = None,
) -> RelevantContext:
    """Return a bounded, ranked set of prior Shards relevant to *task*.

    Deterministic local scoring only (see ``_score_group``) — no embeddings,
    fuzzy matching, or model calls. A Shard is included only when at least
    one non-stopword term of *task* overlaps its task text, shard id, or
    agent; a recorded verification failure or more than one attempt then adds
    a small bonus on top, so failed/retried history about the same topic
    ranks above a single passing run about it, but failure/retry signals
    alone never pull in an unrelated Shard.

    Ordering is fully deterministic: score descending, ties broken by
    recency (newest first), remaining ties broken by shard_id. ``repo``
    filters by repository identity exactly like ``list_shards``/
    ``search_history``. Blank task, non-positive ``limit``, empty history, or
    no scoring match all return an empty ``matches`` list with an honest
    ``context_text`` explaining why — never irrelevant history padded in to
    fill the limit.
    """
    clean_task = task or ""
    query_terms = list(dict.fromkeys(_tokenize(clean_task)))
    if not query_terms or limit <= 0:
        return RelevantContext(task=clean_task, matches=[], context_text=_no_match_text(clean_task))

    groups = _load_groups(repo_path, repo)  # one load + group pass, newest first
    scored: list[tuple[int, int, str, list[str], _ShardGroup]] = []
    for position, group in enumerate(groups):
        _, entry = group.latest
        score, signals = _score_group(query_terms, group, entry)
        if score > 0:
            scored.append((score, position, group.shard_id, signals, group))

    # Higher score first; ties newest first (lower position); remaining ties by shard_id.
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))

    matches = [_build_relevant_match(group, score, signals) for score, _, _, signals, group in scored[:limit]]
    return RelevantContext(task=clean_task, matches=matches, context_text=_render_context_text(clean_task, matches))
