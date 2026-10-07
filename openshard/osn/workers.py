"""Bounded parallel *writing* workers for OSN: isolated, scoped, individually routed and accounted.

A worker is one executor-style agent turn loop (``run_attempt_turns``) that
works on ONE subtask of a validated decomposition, in ITS OWN isolated copy
of the repository (never the run's main copy, never another worker's), with
write authority limited to the subtask's ``allowed_write_paths``: the
``ScopedFileMutationGate`` denies every other path before the ordinary
file-mutation policy even runs, so a worker on tests cannot touch production
infrastructure however it asks. Each worker has its own agent id, its own
model (routing may give each a different one), its own usage ledger, its own
action trace, and may run the verification command once in its own copy as
informational evidence only; the run's verification happens after synthesis.

Concurrency is capped (``HARD_MAX_WORKERS``). Nothing races: workers never
share a filesystem tree, and their results are combined afterwards by
``openshard.osn.synthesis``. The shared budget is consulted once before the
round (nothing is spent if it refuses) and each worker's calls are recorded on
it when the round ends.
"""
from __future__ import annotations

import fnmatch
import hashlib
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openshard.osn.agent_loop import run_attempt_turns
from openshard.osn.decompose import Subtask
from openshard.osn.model_provider import AGENT_SYSTEM_PROMPT, AttemptUsage, IterativeModelProvider
from openshard.policy.decision import PolicyDecision, make_deny
from openshard.policy.file_mutation import FileMutationGate, FileMutationOutcome

ROLE_WORKER = "worker"
HARD_MAX_WORKERS = 3
WORKER_MAX_TURNS = 8
WORKER_MAX_VERIFICATIONS = 1
SCOPE_SOURCE = "subtask_scope"

STATUS_CHANGED = "changed"  # the worker wrote files inside its scope
STATUS_NO_CHANGE = "no_change"  # the worker finished without writing
STATUS_FAILED = "failed"  # provider / malformed / budget / verifier problem; see reason
STATUS_BLOCKED = "blocked"  # a write outside scope or against policy ended the worker

WORKER_SYSTEM_NOTE = (
    " You are ONE worker of a parallel team: do only the subtask you are given, inside the write scope you are "
    "given (writes elsewhere are refused and end your work). Other workers handle the other subtasks in their own "
    "copies; do not do their work and do not assume it exists yet. You may request verification once; a failure "
    "caused by another subtask's missing part is expected and not yours to fix. Finish when your subtask is done."
)


def _matches_scope(rel: str, patterns: tuple[str, ...]) -> bool:
    norm = rel.replace("\\", "/").lstrip("./")
    for pat in patterns:
        p = pat.replace("\\", "/").strip().strip("/")
        if not p:
            continue
        if fnmatch.fnmatchcase(norm, p) or norm == p:
            return True
        if p.endswith("/**") and (norm == p[:-3] or norm.startswith(p[:-3] + "/")):
            return True
        if not any(ch in p for ch in "*?[") and norm.startswith(p + "/"):
            return True  # a directory scope covers everything under it
    return False


@dataclass
class ScopedFileMutationGate(FileMutationGate):
    """The ordinary file-mutation gate, with a worker's write scope enforced first."""

    allowed_patterns: tuple[str, ...] = ()
    worker_id: str = ""

    def authorize(self, rel: str) -> bool:
        if not _matches_scope(rel, self.allowed_patterns):
            decision: PolicyDecision = make_deny(
                "file_write", rel, f"outside the write scope of worker {self.worker_id}",
                source=SCOPE_SOURCE, severity="high",
            )
            self.outcomes.append(FileMutationOutcome(
                path=rel, decision="deny", reason=decision.reason, severity=decision.severity, policy=decision,
            ))
            return False
        return super().authorize(rel)


@dataclass
class WorkerSpec:
    worker_id: str
    subtask: Subtask
    model: str
    model_source: str = "routing"
    provider_name: str | None = None
    max_turns: int = WORKER_MAX_TURNS
    max_verifications: int = WORKER_MAX_VERIFICATIONS


@dataclass
class WorkerResult:
    worker_id: str
    subtask_id: str
    status: str
    reason: str | None = None
    requested_model: str | None = None
    model: str | None = None
    model_source: str | None = None
    provider: str | None = None
    sandbox_path: str | None = None  # the worker's own copy; synthesis reads from it, never stored in a Receipt
    changed_files: list[str] = field(default_factory=list)
    file_hashes: dict[str, str] = field(default_factory=dict)  # sha256 per changed file in the worker copy
    blocked: list[str] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    turns: int = 0
    calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: float | None = None
    cost_source: str | None = None
    duration_ms: int | None = None
    actions: list[dict] = field(default_factory=list)
    verification: dict | None = None  # the worker's own-copy verification, informational
    required: bool = True
    final_note: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "role": ROLE_WORKER,
            "subtask_id": self.subtask_id,
            "status": self.status,
            "reason": self.reason,
            "required": self.required,
            "requested_model": self.requested_model,
            "model": self.model,
            "model_source": self.model_source,
            "provider": self.provider,
            "changed_files": list(self.changed_files),
            "file_hashes": dict(self.file_hashes),
            "blocked": list(self.blocked),
            "decisions": [dict(d) for d in self.decisions],
            "turns": self.turns,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "total_tokens": (
                (self.prompt_tokens or 0) + (self.completion_tokens or 0)
                if self.prompt_tokens is not None or self.completion_tokens is not None else None
            ),
            "cost_usd": self.cost_usd,
            "cost_source": self.cost_source,
            "duration_ms": self.duration_ms,
            "actions": [dict(a) for a in self.actions][:40],
            "verification": dict(self.verification) if self.verification else None,
            "final_note": self.final_note,
            "evidence": {"actions": "agent_declared", "effects_and_scope": "openshard_observed"},
        }


def _subtask_task_text(task: str, plan: dict[str, Any] | None, subtask: Subtask) -> str:
    parts = [f"Overall task:\n{task}\n"]
    if plan and plan.get("summary"):
        parts.append(f"Plan summary: {plan['summary']}")
    parts.append(f"Your subtask ({subtask.id}): {subtask.objective}")
    parts.append("You may write ONLY these paths: " + ", ".join(subtask.allowed_write_paths))
    if subtask.likely_scope:
        parts.append("Paths likely relevant to read: " + ", ".join(subtask.likely_scope))
    if subtask.expected_output:
        parts.append(f"Expected output: {subtask.expected_output}")
    if subtask.required_evidence:
        parts.append("Required evidence: " + "; ".join(subtask.required_evidence))
    if subtask.verification_criteria:
        parts.append("Verification must show: " + "; ".join(subtask.verification_criteria))
    return "\n".join(parts)


def _hash_files(root: Path, rels: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in rels:
        p = root / rel
        try:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<missing>"
        except OSError:
            out[rel] = "<unreadable>"
    return out


def _usage_summary(usage: list[AttemptUsage]) -> dict[str, Any]:
    if not usage:
        return {"prompt_tokens": None, "completion_tokens": None, "cache_read_tokens": None, "cost_usd": None,
                "cost_source": None, "model": None}
    costs = [u.cost_usd for u in usage]
    sources = {u.cost_source for u in usage}
    cache = [u.cache_read_tokens for u in usage]
    return {
        "prompt_tokens": sum(u.prompt_tokens for u in usage),
        "completion_tokens": sum(u.completion_tokens for u in usage),
        "cache_read_tokens": sum(c or 0 for c in cache) if any(c is not None for c in cache) else None,
        "cost_usd": sum(c for c in costs if c is not None) if all(c is not None for c in costs) else None,
        "cost_source": next(iter(sources)) if len(sources) == 1 else None,
        "model": usage[-1].model,
    }


def _worker_progress(progress: Any, spec: WorkerSpec) -> Any:
    """The run's progress callback, with every event stamped with the worker it came from.

    Workers run concurrently and share one callback, so without the stamp their
    turn and action lines interleave indistinguishably. Events pass through
    otherwise unchanged; a renderer that ignores ``worker_id`` sees what it saw before.
    """
    if progress is None:
        return None

    def _stamped(event: str, data: dict[str, Any]) -> None:
        progress(event, {**data, "worker_id": spec.worker_id, "subtask_id": spec.subtask.id})

    return _stamped


def _run_one(
    spec: WorkerSpec,
    *,
    provider: Any,
    task: str,
    plan: dict[str, Any] | None,
    repo_root: Path,
    base_sandbox: Path,
    verify: Any,
    blocked_write_patterns: tuple[str, ...],
    approval_write_patterns: tuple[str, ...],
    progress: Any,
) -> tuple[WorkerResult, list[AttemptUsage]]:
    started = time.monotonic()
    sandbox = Path(tempfile.mkdtemp(prefix=f"osn-worker-{spec.worker_id}-")) / "work"
    shutil.copytree(base_sandbox, sandbox)
    gate = ScopedFileMutationGate(
        blocked_patterns=blocked_write_patterns, approval_patterns=approval_write_patterns,
        allowed_patterns=spec.subtask.allowed_write_paths, worker_id=spec.worker_id,
    )
    turn_provider = IterativeModelProvider(
        provider, [spec.model], repo_root, budget=None, role=ROLE_WORKER,
        system_prompt=AGENT_SYSTEM_PROMPT + WORKER_SYSTEM_NOTE,
    )
    turn_provider.set_plan(plan)
    repo_files = sorted(p.relative_to(sandbox).as_posix() for p in sandbox.rglob("*") if p.is_file())
    worker_text = _subtask_task_text(task, plan, spec.subtask)

    def _verify(paths: list[str]):
        return verify(sandbox, paths)

    outcome = run_attempt_turns(
        repo_root=repo_root, sandbox=sandbox, task=worker_text, attempt=1, provider=turn_provider, gate=gate,
        verify=_verify, budget=None, previous_failure=None, blocked_seen=[], changed_so_far=[],
        max_turns=spec.max_turns, max_verifications=spec.max_verifications,
        progress=_worker_progress(progress, spec),
        role=ROLE_WORKER, model_label=lambda: spec.model, repo_files=repo_files,
        blocked_write_patterns=blocked_write_patterns, approval_write_patterns=approval_write_patterns,
    )
    summary = _usage_summary(turn_provider.usage)
    if outcome.stop == "policy_block":
        status, reason = STATUS_BLOCKED, "write_outside_scope_or_policy"
    elif outcome.stop in ("provider_error", "malformed_reply", "budget"):
        status, reason = STATUS_FAILED, f"{outcome.stop}:{outcome.error_class or ''}".rstrip(":")
    elif outcome.stop in ("verifier_modified_files", "verifier_timeout", "verifier_setup_failed"):
        status, reason = STATUS_FAILED, outcome.stop
    elif outcome.applied:
        status, reason = STATUS_CHANGED, None
    else:
        status, reason = STATUS_NO_CHANGE, outcome.stop
    verification = None
    if outcome.verification is not None:
        v = outcome.verification
        verification = {
            "status": "unknown" if v.timed_out else "passed" if v.passed else "failed",
            "exit_code": v.exit_code, "failed_tests": list(v.failed_tests or []),
            "scope": "worker_copy_informational",
        }
    result = WorkerResult(
        worker_id=spec.worker_id, subtask_id=spec.subtask.id, status=status, reason=reason,
        requested_model=spec.model, model=summary["model"] or spec.model, model_source=spec.model_source,
        provider=spec.provider_name, sandbox_path=str(sandbox),
        changed_files=list(outcome.applied), file_hashes=_hash_files(sandbox, list(outcome.applied)),
        blocked=list(outcome.blocked), decisions=list(outcome.decisions), turns=outcome.turns,
        calls=len(turn_provider.usage), prompt_tokens=summary["prompt_tokens"],
        completion_tokens=summary["completion_tokens"], cache_read_tokens=summary["cache_read_tokens"],
        cost_usd=summary["cost_usd"], cost_source=summary["cost_source"],
        duration_ms=int((time.monotonic() - started) * 1000), actions=[r.to_dict() for r in outcome.records],
        verification=verification, required=spec.subtask.required, final_note=outcome.final_note,
    )
    # Worker usage is attributed to the worker, not the executor; the role is the worker id's role.
    for u in turn_provider.usage:
        u.role = ROLE_WORKER
    return result, [_tag(u, spec.worker_id) for u in turn_provider.usage]


def _tag(u: AttemptUsage, worker_id: str) -> AttemptUsage:
    u.worker_id = worker_id  # type: ignore[attr-defined]
    return u


def run_workers(
    specs: list[WorkerSpec],
    *,
    provider: Any,
    task: str,
    plan: dict[str, Any] | None,
    repo_root: Path,
    base_sandbox: Path,
    verify: Any,
    budget: Any | None = None,
    max_workers: int = HARD_MAX_WORKERS,
    blocked_write_patterns: tuple[str, ...] = (),
    approval_write_patterns: tuple[str, ...] = (),
    progress: Any = None,
) -> tuple[list[WorkerResult], list[AttemptUsage]]:
    """Run *specs* concurrently (at most *max_workers* at once), each in its own copy of *base_sandbox*.

    *verify* is ``verify(sandbox, changed_paths) -> (VerificationResult, output)``:
    the loop's observed verification, run in the WORKER's copy. ``BudgetExhausted``
    propagates before any worker starts when the spend cap is already reached.
    A worker whose thread crashes is recorded as failed; nothing is raised.
    """
    specs = list(specs)[:HARD_MAX_WORKERS]
    if not specs:
        return [], []
    if budget is not None:
        budget.before_model_call()
    workers = max(1, min(int(max_workers), HARD_MAX_WORKERS, len(specs)))
    results: list[WorkerResult] = []
    usage: list[AttemptUsage] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="osn-worker") as pool:
        futures = [
            pool.submit(
                _run_one, spec, provider=provider, task=task, plan=plan, repo_root=repo_root,
                base_sandbox=base_sandbox, verify=verify, blocked_write_patterns=blocked_write_patterns,
                approval_write_patterns=approval_write_patterns, progress=progress,
            )
            for spec in specs
        ]
        for spec, fut in zip(specs, futures):
            try:
                result, worker_usage = fut.result()
            except Exception as exc:
                result, worker_usage = WorkerResult(
                    worker_id=spec.worker_id, subtask_id=spec.subtask.id, status=STATUS_FAILED,
                    reason=f"worker_error:{type(exc).__name__}", requested_model=spec.model,
                    model_source=spec.model_source, provider=spec.provider_name, required=spec.subtask.required,
                ), []
            results.append(result)
            usage.extend(worker_usage)
    if budget is not None:
        for u in usage:
            budget.record_model_call(u.cost_usd)
    return results, usage


__all__ = [
    "HARD_MAX_WORKERS",
    "ROLE_WORKER",
    "STATUS_BLOCKED",
    "STATUS_CHANGED",
    "STATUS_FAILED",
    "STATUS_NO_CHANGE",
    "WORKER_MAX_TURNS",
    "ScopedFileMutationGate",
    "WorkerResult",
    "WorkerSpec",
    "run_workers",
]
