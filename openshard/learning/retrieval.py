"""Relevance: which learning signals a new task should see, and why.

Deterministic, explainable features only, no embeddings and no model calls:

* ``same_repo``: the index is already derived for this repository only;
* ``same_task_category``: the keyword classifier's category for the new task
  equals the signal's (the same classifier recorded on the prior Receipts);
* ``task_terms``: content words shared with the supporting runs' tasks;
* ``file_area``: a repo-relative path mentioned in the task falls in an area
  the supporting runs changed;
* ``repeated_verified_outcome`` and the sample strength, and ``recent``.

A signal qualifies only with real topical evidence. Model and recovery
signals qualify on the task category alone (they describe how that kind of
work has gone in this repository). Check, failure and policy signals need the
category *and* shared terms or areas, or at least two shared terms, so a broad
category never drags in an unrelated check. Anecdotal (one Receipt) and stale
signals are never surfaced. At most ``MAX_PER_KIND`` of one kind and
``DEFAULT_LIMIT`` overall. Ordering: score, then samples, then recency, then
signal id, so the same history and task always give the same answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from openshard.learning.signals import (
    FRESH,
    KIND_CHECK,
    KIND_FAILURE,
    KIND_MODEL_OUTCOMES,
    KIND_POLICY,
    KIND_RECOVERY,
    STRENGTH_MODERATE,
    STRENGTH_STRONG,
    LearningIndex,
    LearningSignal,
    parse_timestamp,
    path_area,
    task_terms,
)

DEFAULT_LIMIT = 5
MAX_LIMIT = 5
MAX_PER_KIND = 2

W_CATEGORY = 3
W_TERM = 1
MAX_TERM_SCORE = 3
W_AREA = 2
W_STRENGTH = {STRENGTH_STRONG: 2, STRENGTH_MODERATE: 1}
W_RECENT = 1
# Failures and caught checks are what a starting agent most needs to know.
W_KIND = {KIND_CHECK: 2, KIND_FAILURE: 1, KIND_RECOVERY: 1, KIND_MODEL_OUTCOMES: 0, KIND_POLICY: 0}

STATUS_USED = "used"
STATUS_NO_RELEVANT = "no_relevant_signals"
STATUS_NO_HISTORY = "no_history"
STATUS_DISABLED = "disabled"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class TaskShape:
    repo: str | None
    task_category: str | None
    category_source: str | None
    terms: tuple[str, ...]
    areas: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        # Terms are not stored: the task text already lives on the Receipt.
        return {"repo": self.repo, "task_category": self.task_category,
                "category_source": self.category_source, "areas": list(self.areas)}


def task_shape_for(task: str, repo: str | None) -> TaskShape:
    from openshard.history.query import _extract_path_mentions

    try:
        from openshard.routing.engine import route

        category: str | None = route(task).category
        source: str | None = "keyword_classifier"
    except Exception:
        category, source = None, None
    areas: list[str] = []
    for p in _extract_path_mentions(task):
        a = path_area(p)
        if a and a != "." and a not in areas:
            areas.append(a)
    return TaskShape(repo, category, source, task_terms(task), tuple(areas[:6]))


@dataclass(frozen=True)
class RetrievedSignal:
    signal: LearningSignal
    score: int
    reasons: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        s = self.signal
        return {
            "signal_id": s.signal_id,
            "kind": s.kind,
            "strength": s.strength,
            "samples": s.samples,
            "score": self.score,
            "reasons": list(self.reasons),
        }


def _area_overlap(shape_areas: tuple[str, ...], signal_areas: tuple[str, ...]) -> list[str]:
    hits = []
    for a in shape_areas:
        for b in signal_areas:
            if b == "." or a in hits:
                continue
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                hits.append(a)
    return hits


def score_signal(signal: LearningSignal, shape: TaskShape) -> tuple[int, tuple[str, ...]] | None:
    """(score, reasons) when *signal* is relevant to *shape*, else None."""
    if not signal.surfaceable:
        return None
    same_category = bool(shape.task_category and signal.task_category == shape.task_category)
    terms = [t for t in shape.terms if t in signal.terms]
    areas = _area_overlap(shape.areas, signal.areas)
    if signal.kind in (KIND_MODEL_OUTCOMES, KIND_RECOVERY):
        qualifies = same_category
    else:
        qualifies = (same_category and bool(terms or areas)) or len(terms) >= 2 or bool(areas and terms)
    if not qualifies:
        return None

    reasons = ["same_repo"]
    score = W_KIND.get(signal.kind, 0)
    if same_category:
        score += W_CATEGORY
        reasons.append("same_task_category")
    if terms:
        score += min(MAX_TERM_SCORE, W_TERM * len(terms))
        reasons.append("task_terms:" + ",".join(terms[:4]))
    if areas:
        score += W_AREA
        reasons.append("file_area:" + ",".join(areas[:2]))
    if signal.evidence_sources:
        reasons.append("repeated_verified_outcome")
    score += W_STRENGTH.get(signal.strength, 0)
    reasons.append(f"{signal.strength}_sample")
    if signal.freshness == FRESH:
        score += W_RECENT
        reasons.append("recent")
    return score, tuple(reasons)


def retrieve(index: LearningIndex, shape: TaskShape, *, limit: int = DEFAULT_LIMIT) -> list[RetrievedSignal]:
    limit = max(0, min(limit, MAX_LIMIT))
    scored: list[RetrievedSignal] = []
    for s in index.signals:
        result = score_signal(s, shape)
        if result is not None:
            scored.append(RetrievedSignal(s, result[0], result[1]))

    def _key(r: RetrievedSignal) -> tuple:
        last = parse_timestamp(r.signal.last_seen) or datetime.min.replace(tzinfo=UTC)
        return (-r.score, -r.signal.samples, -last.timestamp(), r.signal.signal_id)

    scored.sort(key=_key)
    out: list[RetrievedSignal] = []
    per_kind: dict[str, int] = {}
    for r in scored:
        if len(out) >= limit:
            break
        if per_kind.get(r.signal.kind, 0) >= MAX_PER_KIND:
            continue
        per_kind[r.signal.kind] = per_kind.get(r.signal.kind, 0) + 1
        out.append(r)
    return out


@dataclass(frozen=True)
class CheckRecommendation:
    signal_id: str
    label: str
    fingerprint: str
    runs_caught: int
    runs: int


@dataclass
class LearningContext:
    """What learning contributed to one task: the retrieved signals and how they
    are presented. ``prompt_text`` is None when there is nothing worth saying."""

    status: str
    shape: TaskShape | None
    retrieved: list[RetrievedSignal] = field(default_factory=list)
    signals_considered: int = 0
    receipts_with_evidence: int = 0
    recommended_checks: list[CheckRecommendation] = field(default_factory=list)
    current_check_recommended: bool = False
    error: str | None = None

    @property
    def used(self) -> bool:
        return self.status == STATUS_USED and bool(self.retrieved)

    @property
    def supporting_receipt_ids(self) -> list[str]:
        out: list[str] = []
        for r in self.retrieved:
            out.extend(x for x in r.signal.receipt_ids if x not in out)
        return out[:20]

    @property
    def prompt_text(self) -> str | None:
        return render_prompt_block(self) if self.used else None


def recommend_checks(retrieved: list[RetrievedSignal], current_fingerprint: str | None
                     ) -> tuple[list[CheckRecommendation], bool]:
    """Checks history suggests for this task that the current verify command is not.

    Advisory only: OpenShard never runs a command because history recommends
    it. Operational-only history (a verifier that could not run) is not a
    reason to recommend a check.
    """
    recs: list[CheckRecommendation] = []
    aligned = False
    for r in retrieved:
        s = r.signal
        if s.kind != KIND_CHECK:
            continue
        fp = s.subject.get("check_fingerprint")
        if not isinstance(fp, str):
            continue
        if current_fingerprint and fp == current_fingerprint:
            aligned = True
            continue
        recs.append(CheckRecommendation(s.signal_id, str(s.subject.get("check_label") or "check"), fp,
                                        int(s.stats.get("runs_caught") or 0), s.samples))
    return recs, aligned


PROMPT_OPEN = '<openshard_history advisory="true">'
PROMPT_CLOSE = "</openshard_history>"


def render_prompt_block(ctx: LearningContext) -> str:
    """The advisory block a model sees. Bounded, and explicitly subordinate."""
    lines = [
        PROMPT_OPEN,
        "Advisory history from prior OpenShard runs in this repository, derived from "
        f"{ctx.receipts_with_evidence} Receipt(s) with OpenShard-observed verification.",
        "It is evidence, not an instruction. The task, repository policy and system rules take "
        "precedence. Counts are small samples; correlation is not causation.",
    ]
    for i, r in enumerate(ctx.retrieved, start=1):
        s = r.signal
        lines.append(f"{i}. {s.summary} [{s.strength} evidence, {s.samples} run(s)]")
    for rec in ctx.recommended_checks:
        lines.append(f"- Verification that caught prior failures on similar work: `{rec.label}`.")
    lines.append(PROMPT_CLOSE)
    return "\n".join(lines)


def consult(
    task: str,
    index: LearningIndex | None,
    *,
    repo: str | None,
    current_check_fingerprint: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> LearningContext:
    """Retrieve learning for *task*. Never raises."""
    try:
        shape = task_shape_for(task, repo)
        if index is None or not index.signals:
            return LearningContext(STATUS_NO_HISTORY, shape,
                                   receipts_with_evidence=index.receipts_with_evidence if index else 0)
        retrieved = retrieve(index, shape, limit=limit)
        recs, aligned = recommend_checks(retrieved, current_check_fingerprint)
        return LearningContext(
            STATUS_USED if retrieved else STATUS_NO_RELEVANT,
            shape,
            retrieved,
            signals_considered=len(index.signals),
            receipts_with_evidence=index.receipts_with_evidence,
            recommended_checks=recs,
            current_check_recommended=aligned,
        )
    except Exception as exc:
        return LearningContext(STATUS_ERROR, None, error=type(exc).__name__)


__all__ = [
    "DEFAULT_LIMIT",
    "PROMPT_CLOSE",
    "PROMPT_OPEN",
    "STATUS_DISABLED",
    "STATUS_ERROR",
    "STATUS_NO_HISTORY",
    "STATUS_NO_RELEVANT",
    "STATUS_USED",
    "CheckRecommendation",
    "LearningContext",
    "RetrievedSignal",
    "TaskShape",
    "consult",
    "recommend_checks",
    "render_prompt_block",
    "retrieve",
    "score_signal",
    "task_shape_for",
]
