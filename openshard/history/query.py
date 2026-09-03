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
``relevant_context`` ranks Shards for a free-text task using deterministic
evidence scoring — see ``_score_group``. Two evidence channels, both read
directly from the raw run entry (no per-group ``ShardReceipt`` build):

* File/path overlap -- file-like tokens mentioned in the task (e.g.
  ``openshard/history/query.py``) are matched against the Shard's changed
  files and the paths of its non-Note findings. This is the strongest
  signal: it means the same code was actually touched or flagged, not just
  discussed in similar words. See ``_file_overlap_score``.
* Task-keyword overlap -- the existing overlap against task text, shard id,
  and agent, but with a small blacklist of generic engineering verbs
  ("fix", "test", "code", "add", ...) removed before scoring (see
  ``_GENERIC_TERMS``), since those words appear in nearly every task and
  carry no discriminative signal on their own.

A Shard qualifies only when at least one of these channels produces
evidence (score > 0 before bonuses); a recorded verification failure,
more than one attempt, or an earlier failure later resolved by a passing
attempt then each add a small fixed bonus on top -- never enough on their
own to pull in an unrelated Shard. Ordering is score descending with
recency only as a tie-break, so a strong old match is never buried by a
weak recent one. No embeddings, fuzzy matching, or model calls anywhere.

Like ``search_history``, this loads and groups ``runs.jsonl`` exactly once
per call; unlike it, per-attempt ``ShardReceipt`` objects are only built
for the shards that actually make the final ranked result, not for every
group in history.
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
    "HistoryPage",
    "RecentShard",
    "RelevantAttempt",
    "RelevantContext",
    "RelevantMatch",
    "SearchHit",
    "UnknownRunError",
    "UnknownShardError",
    "get_receipt",
    "get_shard",
    "list_shards",
    "recent_shards",
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


@dataclass
class RecentShard:
    """One Shard as ``openshard history`` / ``openshard stats`` see it.

    The receipt is the *latest attempt's* canonical receipt (unchanged from
    what ``openshard last`` renders); ``attempt_count`` / ``has_failure``
    summarise the whole attempt group so retries stay visible without
    listing the Shard more than once.
    """

    shard: Shard
    receipt: ShardReceipt
    attempt_count: int
    has_failure: bool


@dataclass
class HistoryPage:
    """A bounded, newest-first page of recent Shards plus honest totals."""

    total_shards: int
    total_attempts: int
    items: list[RecentShard] = field(default_factory=list)


def recent_shards(
    *,
    limit: int | None = DEFAULT_LIMIT,
    repo: str | None = None,
    repo_path: Path | None = None,
) -> HistoryPage:
    """Return the most recent Shards with their latest-attempt receipts.

    Same grouping, ordering and ``repo`` filter as ``list_shards`` --
    ``runs.jsonl`` is loaded and grouped exactly once, and a receipt is built
    only for the Shards that make the page (``limit=None`` means every
    Shard, which ``openshard stats`` needs). ``total_shards`` /
    ``total_attempts`` always describe the whole (repo-filtered) history so
    a caller can say "showing N of M".
    """
    groups = _load_groups(repo_path, repo)
    total_attempts = sum(len(g.attempts) for g in groups)
    if limit is not None and limit <= 0:
        return HistoryPage(total_shards=len(groups), total_attempts=total_attempts, items=[])
    page = groups if limit is None else groups[:limit]
    items: list[RecentShard] = []
    for group in page:
        receipt = _receipt_for(group)
        items.append(RecentShard(
            shard=receipt.shard or _shard_for(group),
            receipt=receipt,
            attempt_count=len(group.attempts),
            has_failure=_group_has_failure(group),
        ))
    return HistoryPage(total_shards=len(groups), total_attempts=total_attempts, items=items)


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

# Generic engineering vocabulary: words that show up in nearly every task
# ("fix the bug", "add a test", "update the code") and so match almost any
# history regardless of topic. Filtered out of the *query* only -- a Shard's
# own task text is left untouched, since it is compared against the
# filtered query terms either way. Kept separate from _STOPWORDS because
# these are content words (a plain grammar stopword list would not catch
# them), not because the tokenizer treats them differently.
_GENERIC_TERMS: frozenset[str] = frozenset({
    "fix", "fixed", "fixes", "fixing",
    "bug", "bugs", "issue", "issues",
    "test", "tests", "testing",
    "code", "coding",
    "add", "adds", "added", "adding",
    "implement", "implements", "implemented", "implementing", "implementation",
    "update", "updates", "updated", "updating",
    "change", "changes", "changed", "changing",
    "feature", "features",
    "task", "tasks",
    # "error"/"errors" deliberately NOT listed: unlike the filler verbs above
    # it is a subject-matter noun ("error boundary", "5xx errors", "error
    # budget") and is often the one distinctive term two tasks share. Its
    # worst case is a weak score-2 match ranked below any file evidence,
    # which is cheaper than dropping a genuine connection.
    "problem", "problems",
    "improve", "improves", "improved", "improvement",
    "handle", "handles", "handling",
    "make", "makes", "making",
    "work", "working",
    "new", "old",
})

_WORD_RE = re.compile(r"[a-z0-9]+")

# File-like tokens in free task text: a path with at least one separator
# (e.g. "openshard/history/query.py"), or a bare filename with a common
# code/config extension (e.g. "query.py") -- narrow enough to skip "v4.6" or
# "e.g." style false positives, wide enough to cover most repositories.
_CODE_EXTENSIONS = (
    "py", "ts", "tsx", "js", "jsx", "mjs", "cjs", "go", "rs", "java", "kt",
    "rb", "php", "c", "cpp", "cc", "h", "hpp", "cs", "swift", "scala",
    "json", "yaml", "yml", "toml", "tf", "tfvars", "md", "sh", "ps1",
    "sql", "html", "css", "scss", "vue", "svelte", "ini", "cfg", "conf",
)
_PATH_RE = re.compile(
    r"(?:[\w.-]+[/\\])+[\w.-]+\.[A-Za-z0-9]{1,8}"
    r"|[\w-]{2,}\.(?:" + "|".join(_CODE_EXTENSIONS) + r")\b",
    re.IGNORECASE,
)
# Per-field keyword weights and fixed bonuses. Kept explicit and small so a
# match's score is always explainable from its `signals` list.
_WEIGHT_TASK = 2
_WEIGHT_SHARD_ID = 1
_WEIGHT_AGENT = 1
_WEIGHT_FILE_TOUCHED = 6
_WEIGHT_FILE_REFERENCED = 4
_BONUS_FAILURE = 2
_BONUS_MULTI_ATTEMPT = 1
_BONUS_RESOLVED = 2

_MAX_FILE_SIGNALS = 3
_MAX_CONTEXT_FILES = 8
_MAX_CONTEXT_FINDINGS = 5
_MAX_CONTEXT_ATTEMPTS = 5


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop trivial stopwords."""
    return [t for t in _WORD_RE.findall((text or "").lower()) if t not in _STOPWORDS]


def _query_terms(task: str) -> list[str]:
    """Content words from *task* for keyword-overlap scoring.

    Stopwords and generic engineering verbs (see ``_GENERIC_TERMS``) are
    removed and the result de-duplicated in first-seen order, so a task like
    "fix the bug in terraform validate" scores only on "terraform"/
    "validate" -- generic noise never contributes on its own.
    """
    return list(dict.fromkeys(t for t in _tokenize(task) if t not in _GENERIC_TERMS))


def _extract_path_mentions(task: str) -> list[str]:
    """File-like substrings mentioned in *task*, in first-seen order, deduped."""
    seen: list[str] = []
    for m in _PATH_RE.finditer(task or ""):
        token = m.group(0).strip(".,;:()[]{}'\"")
        if token and token not in seen:
            seen.append(token)
    return seen


def _normalize_path(p: str) -> str:
    return p.replace("\\", "/").strip().lower()


def _match_one_path(query_token: str, evidence: list[str]) -> str | None:
    """Return the evidence path *query_token* matches, or ``None``.

    Matching is on whole path segments, in either direction: an exact
    normalized path, or one path being a segment-boundary suffix of the
    other. So "backend/utils.py" matches "src/backend/utils.py", and a bare
    "utils.py" matches "frontend/utils.py" -- a bare filename is the only
    form that matches across directories, because it is the only form that
    names no directory to contradict.

    Directories are never ignored: "backend/utils.py" must NOT match
    "frontend/utils.py" just because both end in the same filename, or the
    match would assert "modified the same file" about a different file.

    *evidence* is scanned in order and callers pass it sorted, so the chosen
    path is the same on every run even when several candidates match.
    """
    norm_q = _normalize_path(query_token)
    for ev in evidence:
        norm_ev = _normalize_path(ev)
        if norm_ev == norm_q or norm_ev.endswith("/" + norm_q) or norm_q.endswith("/" + norm_ev):
            return ev
    return None


def _entry_touched_paths(entry: dict) -> list[str]:
    """Changed-file paths recorded on *entry* -- same source fields
    ``build_shard_receipt`` uses for ``files_touched``, read directly so
    scoring never needs a full receipt build for every group in history.

    Returned sorted: which path a query token matches must not depend on
    set iteration order, which varies between processes with the
    interpreter's hash seed (see ``_match_one_path``)."""
    touched: set[str] = set()
    for f in entry.get("files_detail") or []:
        if isinstance(f, dict) and isinstance(f.get("path"), str) and f["path"]:
            touched.add(f["path"])
    if not touched:
        diff_review = entry.get("diff_review") or {}
        for p in diff_review.get("changed_files") or []:
            if isinstance(p, str) and p:
                touched.add(p)
        fr = entry.get("final_report") or {}
        for p in fr.get("diff_files") or []:
            if isinstance(p, str) and p:
                touched.add(p)
    return sorted(touched)


def _entry_referenced_paths(entry: dict) -> list[str]:
    """Paths of *entry*'s non-Note findings only -- the same subset of
    findings that actually crosses into a relevant_context match (see the
    module docstring's privacy note), so this never scores on evidence the
    match itself would not also expose. Sorted, for the same determinism
    reason as ``_entry_touched_paths``."""
    referenced: set[str] = set()
    sources = [entry.get("findings"), (entry.get("final_report") or {}).get("findings")]
    for raw in sources:
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            severity = item.get("severity") or "Note"
            path = item.get("path")
            if severity != "Note" and isinstance(path, str) and path:
                referenced.add(path)
    return sorted(referenced)


def _file_overlap_score(query_paths: list[str], entry: dict) -> tuple[int, list[str]]:
    """Score + explain file/path evidence between *query_paths* and *entry*.

    Checks touched files first (strongest -- the same code was changed),
    then referenced-finding paths, each capped so one Shard can't dominate
    purely on file count. A path matched once (e.g. both touched and
    referenced) is never double-counted. Every input is in a fixed order
    (query tokens as mentioned, evidence sorted), so both the score and the
    exact path named in each signal are reproducible run to run.
    """
    if not query_paths:
        return 0, []
    score = 0
    signals: list[str] = []
    matched_paths: set[str] = set()
    for evidence, weight, verb in (
        (_entry_touched_paths(entry), _WEIGHT_FILE_TOUCHED, "modified the same file"),
        (_entry_referenced_paths(entry), _WEIGHT_FILE_REFERENCED, "prior finding referenced the same file"),
    ):
        if not evidence:
            continue
        for token in query_paths:
            if len(signals) >= _MAX_FILE_SIGNALS:
                return score, signals
            hit = _match_one_path(token, evidence)
            if hit and hit not in matched_paths:
                matched_paths.add(hit)
                score += weight
                signals.append(f"{verb}: {hit}")
    return score, signals


def _entry_verification_failed(entry: dict) -> bool:
    """True if *entry* itself (one attempt) recorded a verification/check failure."""
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


def _entry_verification_passed(entry: dict) -> bool:
    """True if *entry* itself (one attempt) recorded a clean verification pass."""
    if entry.get("verification_passed") is True:
        return True
    osn = entry.get("osn_verification_contract")
    if isinstance(osn, dict) and str(osn.get("status") or "") == "passed":
        return True
    checks = entry.get("review_checks")
    if isinstance(checks, list) and checks and all(
        isinstance(c, dict) and c.get("status") == "passed" for c in checks
    ):
        return True
    return False


def _resolution_signal(group: _ShardGroup) -> str | None:
    """Signal text when an earlier attempt failed and a later one passed.

    Distinct from the plain "multiple attempts" bonus: this specifically
    rewards the case a starting agent most wants to know about -- this area
    had a failure, and it was subsequently resolved.
    """
    if len(group.attempts) < 2:
        return None
    ordered = sorted(group.attempts, key=lambda item: item[0])
    earliest_entry = ordered[0][1]
    latest_entry = ordered[-1][1]
    if _entry_verification_failed(earliest_entry) and _entry_verification_passed(latest_entry):
        return "earlier attempt failed verification; a later attempt passed"
    return None


def ranking_explanation() -> dict:
    """The exact, static rules ``relevant_context`` ranks by -- for display.

    Exposed so ``openshard context`` can explain a score from the same
    constants ``_score_group`` uses, rather than a hand-written description
    that could drift from the code. Pure data; no history is read.
    """
    return {
        "method": (
            "deterministic evidence scoring: file/path overlap plus task-"
            "keyword overlap (no embeddings, no model calls)"
        ),
        "weights": {
            "task_text": _WEIGHT_TASK,
            "shard_id": _WEIGHT_SHARD_ID,
            "agent": _WEIGHT_AGENT,
            "file_touched": _WEIGHT_FILE_TOUCHED,
            "file_referenced": _WEIGHT_FILE_REFERENCED,
        },
        "bonuses": {
            "prior_verification_failure": _BONUS_FAILURE,
            "multiple_attempts": _BONUS_MULTI_ATTEMPT,
            "resolved_after_failure": _BONUS_RESOLVED,
        },
        "fields_read": [
            "task text", "shard id", "agent", "status", "verification result",
            "changed file paths", "non-Note finding paths", "non-Note findings",
        ],
        "fields_never_read": [
            "transcripts", "assistant responses", "tool output", "notes",
            "environment", "absolute paths",
        ],
        "tie_break": "newest first, then shard id",
    }


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
    """Bounded result of relevant_context: ranked matches plus injectable text.

    ``total_shards`` is how many (repo-filtered) Shards were considered, so a
    caller can say "N of M matched" without loading history a second time.
    It is 0 when nothing was loaded (blank task / non-positive limit).
    """

    task: str
    matches: list[RelevantMatch]
    context_text: str
    total_shards: int = 0


def _group_has_failure(group: _ShardGroup) -> bool:
    """True if any attempt of *group* recorded a verification or check failure."""
    return any(_entry_verification_failed(entry) for _, entry in group.attempts)


def _score_group(
    query_terms: list[str], query_paths: list[str], group: _ShardGroup, entry: dict,
) -> tuple[int, list[str]]:
    """Deterministic evidence score for one Shard group against a query.

    Combines two independent evidence channels -- file/path overlap
    (``_file_overlap_score``) and task-keyword overlap against the latest
    attempt's task text, shard id, and agent identity -- read directly from
    the raw entry, never a built ShardReceipt, so scoring every group in
    history costs no extra I/O or receipt-construction work. Returns
    ``(0, [])`` when neither channel finds any evidence at all: relevance
    always requires file or topical overlap, never failure/retry signals
    alone (those only ever add to an already-relevant match).
    """
    file_score, file_signals = _file_overlap_score(query_paths, entry)

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

    total = file_score + keyword_score
    if total <= 0:
        return 0, []

    signals: list[str] = list(file_signals)
    if matched:
        signals.append(f"task overlap: {', '.join(matched)}")

    if _group_has_failure(group):
        total += _BONUS_FAILURE
        signals.append("prior verification failure")
    if len(group.attempts) > 1:
        total += _BONUS_MULTI_ATTEMPT
        signals.append(f"multiple attempts ({len(group.attempts)})")
    resolved = _resolution_signal(group)
    if resolved:
        total += _BONUS_RESOLVED
        signals.append(resolved)

    return total, signals


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
    fuzzy matching, or model calls. A Shard is included only when it carries
    real evidence: a file/path mentioned in *task* was touched or referenced
    by a finding, or a non-generic content term of *task* overlaps its task
    text, shard id, or agent. A recorded verification failure, more than one
    attempt, or an earlier failure later resolved by a passing attempt then
    each add a small bonus on top, so failed/retried history about the same
    topic ranks above a single passing run about it -- but these signals
    alone never pull in a Shard with no file or topical overlap at all.

    Ordering is fully deterministic: score descending, ties broken by
    recency (newest first), remaining ties broken by shard_id. ``repo``
    filters by repository identity exactly like ``list_shards``/
    ``search_history``. Blank task, non-positive ``limit``, empty history, or
    no scoring match all return an empty ``matches`` list with an honest
    ``context_text`` explaining why — never irrelevant history padded in to
    fill the limit.
    """
    clean_task = task or ""
    query_terms = _query_terms(clean_task)
    query_paths = _extract_path_mentions(clean_task)
    if (not query_terms and not query_paths) or limit <= 0:
        return RelevantContext(task=clean_task, matches=[], context_text=_no_match_text(clean_task))

    groups = _load_groups(repo_path, repo)  # one load + group pass, newest first
    scored: list[tuple[int, int, str, list[str], _ShardGroup]] = []
    for position, group in enumerate(groups):
        _, entry = group.latest
        score, signals = _score_group(query_terms, query_paths, group, entry)
        if score > 0:
            scored.append((score, position, group.shard_id, signals, group))

    # Higher score first; ties newest first (lower position); remaining ties by shard_id.
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))

    matches = [_build_relevant_match(group, score, signals) for score, _, _, signals, group in scored[:limit]]
    return RelevantContext(
        task=clean_task,
        matches=matches,
        context_text=_render_context_text(clean_task, matches),
        total_shards=len(groups),
    )
