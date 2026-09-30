"""Task-shaped history evidence for Adaptive Routing V2.

Routing V2 already ranks on observed history, but only per harness: every
verified OSN run in ``runs.jsonl`` counts toward a model's rate, whatever
repository or kind of task it was. This adds the narrower, more relevant
scope first:

    same repository + same task category + same harness   (scope ``repo_task_category``)
    -> else the existing harness-wide evidence, unchanged (scope ``harness``)

The narrower scope is used only when it clears the *same* gate
(``history_evidence.MIN_VERIFIED_SAMPLES`` per model and
``MIN_MODELS_WITH_EVIDENCE`` models), so a handful of runs can never bias a
decision, and a single lucky run never can. When it does not, the decision is
exactly what it was before, and the record says why the scoped evidence was
not used. No model ranking is hard-coded; the policy still ranks the eligible
candidates it chose within the repository's model policy and budget.

It plugs in through the ``history`` object Routing V2 already accepts (it
only calls ``gate``), so the capability gate, explicit ``--model`` and
explicit ladders are untouched: this is consulted only on the applied path.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from openshard.routing.adaptive.history_evidence import (
    HistoryEvidence,
    build_history_evidence,
    load_history_evidence,
)
from openshard.routing.requirements import ObservedEvidence

SCOPE_TASK = "repo_task_category"
SCOPE_HARNESS = "harness"


class ScopedHistoryEvidence:
    """Tries repository + task-category evidence first, then the harness-wide evidence."""

    def __init__(self, scoped: HistoryEvidence, broad: HistoryEvidence, *, repo: str | None,
                 task_category: str | None) -> None:
        self.scoped = scoped
        self.broad = broad
        self.repo = repo
        self.task_category = task_category
        # Read by nothing in the policy; kept so the object still looks like evidence.
        self.harness = broad.harness
        self.per_model = broad.per_model
        self.entries_scanned = broad.entries_scanned

    def gate(self, candidates: Iterable[str]) -> tuple[Mapping[str, ObservedEvidence] | None, dict[str, Any]]:
        ids = list(candidates)
        evidence, record = self.scoped.gate(ids)
        scope = {"repo": self.repo, "task_category": self.task_category}
        if evidence is not None:
            return evidence, {**record, "scope": SCOPE_TASK, **scope}
        broad_evidence, broad_record = self.broad.gate(ids)
        return broad_evidence, {
            **broad_record,
            "scope": SCOPE_HARNESS,
            "scoped": {
                **scope,
                "reason": record.get("reason"),
                "candidates_with_evidence": record.get("candidates_with_evidence"),
                "verified_outcomes_for_candidates": record.get("verified_outcomes_for_candidates"),
            },
        }


def load_scoped_history(runs_path: Path, *, harness: str, repo: str | None,
                        task_category: str | None) -> ScopedHistoryEvidence | HistoryEvidence:
    """History for Routing V2. Never raises; without a repo or category, the broad evidence alone."""
    broad = load_history_evidence(runs_path, harness=harness)
    if not repo or not task_category:
        return broad
    try:
        from openshard.history.repo_identity import entry_matches_repo
        from openshard.learning.signals import read_entries, task_category_for
        from openshard.routing.adaptive.outcome import outcome_from_receipt

        entries, _bad = read_entries(runs_path)
        outcomes = [
            outcome_from_receipt(e) for e in entries
            if entry_matches_repo(e, repo) and task_category_for(e)[0] == task_category
        ]
        scoped = build_history_evidence(outcomes, harness=harness)
    except Exception:
        return broad
    return ScopedHistoryEvidence(scoped, broad, repo=repo, task_category=task_category)


__all__ = ["SCOPE_HARNESS", "SCOPE_TASK", "ScopedHistoryEvidence", "load_scoped_history"]
