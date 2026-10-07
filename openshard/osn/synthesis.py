"""Synthesis: combine parallel workers' results into the run's main copy, safely and on the record.

Workers wrote into their own copies. Synthesis decides, deterministically and
file by file, what reaches the run's main copy:

* a file changed by exactly one worker, inside that worker's write scope, is
  applied (its bytes copied, its hash recorded as applied);
* a file changed by two or more workers is a *conflict*: it is NOT applied by
  copying either version; the conflict is surfaced with each worker's version
  and handed to the designated synthesis agent (the executor role's turn loop,
  with bounded diffs as context) to resolve inside policy;
* a file outside the worker's scope is rejected (defence in depth: the scoped
  gate should already have refused it);
* a failed *required* subtask is reported as missing work so the executor can
  pick it up; a failed optional one is recorded and skipped.

Nothing here claims verification. The run verifies the synthesised copy with
its own command afterwards, like any other attempt.
"""
from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openshard.osn.workers import STATUS_CHANGED, WorkerResult, _matches_scope

MAX_CONFLICT_DIFF_CHARS = 6_000
MAX_CONFLICTS_IN_CONTEXT = 4

DECISION_APPLIED = "applied"
DECISION_CONFLICT = "conflict"
DECISION_OUT_OF_SCOPE = "rejected_out_of_scope"
DECISION_WORKER_FAILED = "skipped_worker_failed"
DECISION_MISSING = "rejected_missing_in_worker_copy"


@dataclass
class SynthesisResult:
    applied: list[str] = field(default_factory=list)  # files copied into the main copy
    applied_hashes: dict[str, str] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)  # {path, workers}
    rejected: list[dict[str, Any]] = field(default_factory=list)  # {path, worker_id, reason}
    file_decisions: list[dict[str, Any]] = field(default_factory=list)  # every file, in order
    workers_accepted: list[str] = field(default_factory=list)
    workers_rejected: list[dict[str, Any]] = field(default_factory=list)  # {worker_id, status, reason, required}
    missing_required: list[dict[str, Any]] = field(default_factory=list)  # required subtasks with no usable output
    conflict_context: str = ""  # bounded per-worker diffs for the synthesis agent; in memory only

    @property
    def needs_resolution(self) -> bool:
        return bool(self.conflicts or self.missing_required)

    def to_record(self) -> dict[str, Any]:
        return {
            "applied": list(self.applied),
            "applied_hashes": dict(self.applied_hashes),
            "conflicts": [dict(c) for c in self.conflicts],
            "rejected": [dict(r) for r in self.rejected],
            "file_decisions": [dict(d) for d in self.file_decisions][:60],
            "workers_accepted": list(self.workers_accepted),
            "workers_rejected": [dict(w) for w in self.workers_rejected],
            "missing_required": [dict(m) for m in self.missing_required],
            "resolution": "executor_turns" if self.needs_resolution else "none_needed",
            "evidence": {"file_effects": "openshard_observed", "verification": "run_after_synthesis"},
        }


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


def synthesize(
    results: list[WorkerResult],
    *,
    main_sandbox: Path,
    scopes: dict[str, tuple[str, ...]],
) -> SynthesisResult:
    """Combine worker copies into *main_sandbox*. *scopes* maps worker id -> allowed write patterns."""
    out = SynthesisResult()
    by_path: dict[str, list[WorkerResult]] = {}
    for r in results:
        if r.status != STATUS_CHANGED or not r.sandbox_path:
            out.workers_rejected.append({"worker_id": r.worker_id, "status": r.status, "reason": r.reason,
                                         "required": r.required})
            if r.required and r.status != "no_change":
                out.missing_required.append({"worker_id": r.worker_id, "subtask_id": r.subtask_id,
                                             "status": r.status, "reason": r.reason})
            continue
        for rel in r.changed_files:
            by_path.setdefault(rel, []).append(r)
    accepted: set[str] = set()
    for rel in sorted(by_path):
        writers = by_path[rel]
        if len(writers) > 1:
            out.conflicts.append({"path": rel, "workers": [w.worker_id for w in writers]})
            out.file_decisions.append({"path": rel, "decision": DECISION_CONFLICT, "workers": [w.worker_id for w in writers]})
            continue
        w = writers[0]
        if not _matches_scope(rel, scopes.get(w.worker_id, ())):
            out.rejected.append({"path": rel, "worker_id": w.worker_id, "reason": DECISION_OUT_OF_SCOPE})
            out.file_decisions.append({"path": rel, "decision": DECISION_OUT_OF_SCOPE, "worker_id": w.worker_id})
            continue
        data = _read(Path(w.sandbox_path or "") / rel) if w.sandbox_path else None
        if data is None:
            out.rejected.append({"path": rel, "worker_id": w.worker_id, "reason": DECISION_MISSING})
            out.file_decisions.append({"path": rel, "decision": DECISION_MISSING, "worker_id": w.worker_id})
            continue
        dest = main_sandbox / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        out.applied.append(rel)
        out.applied_hashes[rel] = digest
        accepted.add(w.worker_id)
        out.file_decisions.append({"path": rel, "decision": DECISION_APPLIED, "worker_id": w.worker_id,
                                   "sha256": digest})
    out.workers_accepted = sorted(accepted)
    if out.conflicts:
        out.conflict_context = _conflict_context(out.conflicts, results, main_sandbox)
    return out


def _conflict_context(conflicts: list[dict[str, Any]], results: list[WorkerResult], main_sandbox: Path) -> str:
    """Each worker's version of each conflicting file as a diff against the main copy. Bounded, in memory only."""
    by_id = {r.worker_id: r for r in results}
    chunks: list[str] = []
    for c in conflicts[:MAX_CONFLICTS_IN_CONTEXT]:
        rel = c["path"]
        base = _read(main_sandbox / rel)
        base_lines = base.decode("utf-8", "replace").splitlines() if base is not None else []
        for wid in c["workers"]:
            w = by_id.get(wid)
            if w is None or not w.sandbox_path:
                continue
            data = _read(Path(w.sandbox_path) / rel)
            if data is None:
                continue
            lines = data.decode("utf-8", "replace").splitlines()
            diff = "\n".join(difflib.unified_diff(base_lines, lines, fromfile=f"main/{rel}", tofile=f"{wid}/{rel}", lineterm=""))
            chunks.append(f"<untrusted kind=\"worker_diff\" worker=\"{wid}\" path=\"{rel}\">\n{diff}\n</untrusted>")
    text = "\n".join(chunks)
    return text if len(text) <= MAX_CONFLICT_DIFF_CHARS else text[:MAX_CONFLICT_DIFF_CHARS] + "\n[diffs truncated]"


def resolution_advisory(synth: SynthesisResult, results: list[WorkerResult]) -> str:
    """What the synthesis agent (the executor's turn loop) is told to resolve. Never raw tool output."""
    parts = ["Parallel workers finished; OpenShard applied their non-conflicting files to this copy."]
    if synth.applied:
        parts.append("Applied from workers: " + ", ".join(synth.applied))
    if synth.conflicts:
        parts.append(
            "CONFLICTS (the same file changed by several workers; nothing was applied for these paths, resolve them "
            "by writing the correct complete file): "
            + "; ".join(f"{c['path']} <- {', '.join(c['workers'])}" for c in synth.conflicts)
        )
        if synth.conflict_context:
            parts.append(synth.conflict_context)
    if synth.missing_required:
        by_id = {r.worker_id: r for r in results}
        for m in synth.missing_required:
            r = by_id.get(m["worker_id"])
            parts.append(
                f"REQUIRED SUBTASK NOT DELIVERED by worker {m['worker_id']} (subtask {m['subtask_id']}, "
                f"{m['status']}: {m.get('reason') or 'no reason recorded'}). Complete it yourself: "
                + (r.final_note if r and r.final_note else "see the plan.")
            )
    parts.append("Then request verification and finish.")
    return "\n".join(parts)


__all__ = [
    "DECISION_APPLIED",
    "DECISION_CONFLICT",
    "DECISION_MISSING",
    "DECISION_OUT_OF_SCOPE",
    "DECISION_WORKER_FAILED",
    "SynthesisResult",
    "resolution_advisory",
    "synthesize",
]
