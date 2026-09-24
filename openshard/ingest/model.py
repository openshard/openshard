"""Neutral data model for Historical Ingestion v1.

Three layers, each with a different privacy status:

* :class:`SourceObject` -- *where* some bytes live (a locator). Never content.
* :class:`ParsedSession` -- what a parser recovered from one native session.
  Format-specific, **transient**: it may hold raw prompt text, commands and
  tool output, lives only in memory, and is never persisted anywhere.
* :class:`HistoricalSession` -- the neutral IR after the scrub boundary
  (``normalize.py``). Every field is a :class:`Fact`; every string has been
  secret-scrubbed; every path is repo-relative. Only this reaches storage.

Every recovered fact carries its evidence and a reference into the source, so
anyone holding the original file (``source_sha256``) can re-check it. A fact
that could not be recovered is an explicit ``unknown`` -- never omitted and
never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openshard.history.event import (
    EVIDENCE_AGENT_REPORTED,
    EVIDENCE_GIT_OBSERVED,
    EVIDENCE_GIT_VERIFIED,
    EVIDENCE_IMPORTED_TRANSCRIPT,
    EVIDENCE_INDEPENDENTLY_VERIFIED,
    EVIDENCE_UNKNOWN,
)

# Cost derived by OpenShard from a pricing table (not produced in v1).
EVIDENCE_ESTIMATED = "estimated"

# The only evidence levels a historical fact may carry. ``directly_observed``
# is deliberately absent: OpenShard never observed an imported session live.
FACT_EVIDENCE: frozenset[str] = frozenset(
    {
        EVIDENCE_IMPORTED_TRANSCRIPT,
        EVIDENCE_AGENT_REPORTED,
        EVIDENCE_GIT_OBSERVED,
        EVIDENCE_GIT_VERIFIED,
        EVIDENCE_INDEPENDENTLY_VERIFIED,
        EVIDENCE_ESTIMATED,
        EVIDENCE_UNKNOWN,
    }
)

# Command outcomes read from a transcript (same vocabulary as the hook fold).
OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_NOT_COMPLETED = "not_completed"
OUTCOME_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Fact:
    """One recovered fact: ``{value, evidence, ref}``.

    ``ref`` is a locator inside the source (``"L12"``, ``"L3-L90"``,
    ``"git:rev-list"``) -- never content.
    """

    value: Any
    evidence: str
    ref: str | None = None

    def __post_init__(self) -> None:
        if self.evidence not in FACT_EVIDENCE:
            raise ValueError(f"evidence not allowed for a historical fact: {self.evidence!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "evidence": self.evidence, "ref": self.ref}


def unknown_fact(ref: str | None = None) -> Fact:
    return Fact(None, EVIDENCE_UNKNOWN, ref)


# ---------------------------------------------------------------------------
# Connector side
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceObject:
    """One addressable blob a connector can stream.

    ``object_id`` is stable across runs (used for resume). ``locator`` is how
    the connector reopens it (a local path here); it stays local and is only
    ever persisted as ``locator_hash`` / a home-relative ``locator_display``.
    """

    connector: str
    object_id: str
    locator: str
    size: int | None = None
    mtime: float | None = None
    etag: str | None = None
    hint: str | None = None  # parser name the connector expects, if any


# ---------------------------------------------------------------------------
# Parser side (transient -- never persisted)
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    call_id: str | None
    name: str
    ref: str
    at: str | None = None
    command: str | None = None  # raw shell command (transient; reduced at normalize)
    cwd: str | None = None
    paths: list[tuple[str, str]] = field(default_factory=list)  # (raw path, change_type) for edits
    outcome: str = OUTCOME_UNKNOWN  # from the tool result, when the source records one
    outcome_ref: str | None = None
    exit_code: int | None = None
    commit_shas: list[str] = field(default_factory=list)  # SHAs printed by git in the tool output


@dataclass
class ParsedSession:
    """Everything one parser recovered from one native session. In memory only."""

    parser: str  # "claude_code_jsonl@1"
    agent: str  # "claude_code" | "codex"
    native_session_id: str
    session_ref: str | None = None
    start: str | None = None
    end: str | None = None
    window_ref: str | None = None
    cwd: str | None = None
    cwd_ref: str | None = None
    branch: str | None = None
    branch_ref: str | None = None
    head_at_start: str | None = None  # only when the source records it (Codex)
    head_ref: str | None = None
    repository_url: str | None = None
    repository_ref: str | None = None
    agent_version: str | None = None
    task: str | None = None  # raw first prompt (transient)
    task_ref: str | None = None
    title: str | None = None  # agent-generated title, if recorded
    title_ref: str | None = None
    models: list[str] = field(default_factory=list)
    model_ref: str | None = None
    provider: str | None = None
    provider_ref: str | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    tokens_ref: str | None = None
    approval_policy: str | None = None
    approval_ref: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    final_message: str | None = None  # raw last assistant message (transient)
    final_message_ref: str | None = None
    claimed_shas: list[str] = field(default_factory=list)  # SHAs the agent *said* it committed
    pr_url: str | None = None
    pr_ref: str | None = None
    turns: int = 0
    record_count: int = 0
    losses: dict[str, int] = field(default_factory=dict)  # kind -> count

    def add_loss(self, kind: str, n: int = 1) -> None:
        self.losses[kind] = self.losses.get(kind, 0) + n


# ---------------------------------------------------------------------------
# Neutral IR (post scrub boundary)
# ---------------------------------------------------------------------------


@dataclass
class HistoricalCommand:
    action: str  # scrubbed "Bash: pytest -q"
    kind: str  # test | lint | other
    tool: str
    at: str | None
    ref: str
    outcome: str  # passed | failed | not_completed | unknown
    outcome_ref: str | None = None
    exit_code: int | None = None


@dataclass
class HistoricalFileEdit:
    path: str  # repo-relative
    change_type: str  # create | update | delete
    evidence: str  # imported_transcript | git_verified
    ref: str


@dataclass
class HistoricalCommit:
    sha: str
    evidence: str  # git_verified | git_observed | agent_reported
    ref: str
    reason: str


@dataclass
class HistoricalSession:
    """Scrubbed, repo-relative, provenance-labelled facts about one session."""

    parser: str
    agent: str
    native_session_id: str
    import_key: str
    source_sha256: str
    repo_root: str  # local only; never persisted
    facts: dict[str, Fact] = field(default_factory=dict)
    tool_counts: dict[str, int] = field(default_factory=dict)
    tool_events: list[dict] = field(default_factory=list)  # {tool, at, ref}
    commands: list[HistoricalCommand] = field(default_factory=list)
    file_edits: list[HistoricalFileEdit] = field(default_factory=list)
    commits: list[HistoricalCommit] = field(default_factory=list)
    tool_output_shas: list[str] = field(default_factory=list)
    claimed_shas: list[str] = field(default_factory=list)
    losses: dict[str, int] = field(default_factory=dict)  # source records the parser could not read
    dropped: dict[str, int] = field(default_factory=dict)  # facts deliberately not kept (e.g. outside the repo)

    def fact(self, name: str) -> Fact:
        return self.facts.get(name) or unknown_fact()

    def value(self, name: str) -> Any:
        return self.fact(name).value
