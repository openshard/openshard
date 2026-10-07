"""Bounded OSN execution loop.

inspect -> plan -> policy -> isolated action -> direct verification
-> bounded retry (only if justified) -> receipt.

Two provider shapes drive an attempt:

* a *turn provider* (``.turn(state)``, see ``openshard.osn.agent_loop``): the
  model takes several bounded turns, choosing typed actions (list, read,
  search, diff, write, run_verification, finish) from what the previous
  actions returned, while the harness validates and performs each one;
* an *action provider* (``provider(ctx) -> [FileWriteAction]``): one call
  proposing whole-file writes, kept for callers of the original contract.

Evidence semantics: actions come from an agent/provider and are *declared*;
policy decisions, file effects and verification are *observed* by OpenShard.
Applying a change is never treated as verifying it. Changes are made only in
an isolated copy (a filesystem copy, not a process sandbox: the verify command
runs with host permissions and may execute agent-written code); promoting them
to the real repo is a separate, policy-gated step (see
openshard.native.sandbox_apply.apply_sandbox_changes).
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openshard.osn.actions import summarize_actions
from openshard.osn.agent_loop import (
    DEFAULT_MAX_TURNS,
    DEFAULT_MAX_VERIFICATIONS,
    STOP_BUDGET,
    STOP_MALFORMED_REPLY,
    STOP_POLICY_BLOCK,
    STOP_PROVIDER_ERROR,
    STOP_VERIFIER_SETUP,
    STOP_VERIFIER_TAINTED,
    STOP_VERIFIER_TIMEOUT,
    run_attempt_turns,
    state_fingerprint,
    verification_progress_fields,
)
from openshard.osn.budget import STATUS_BUDGET_EXHAUSTED, BudgetExhausted, BudgetLedger
from openshard.policy.command_execution import organisation_command_blocked
from openshard.policy.decision import PolicyDecision, make_deny
from openshard.policy.file_mutation import Approver, FileMutationGate
from openshard.safety.sanitize import sanitize_text
from openshard.security.paths import UnsafePathError, resolve_safe_repo_path
from openshard.verification.failed_tests import failing_test_ids
from openshard.verification.setup_failure import detect_setup_failure

SCHEMA_VERSION = 1
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".openshard", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules",
    # Local secrets/agent state never belong in the isolated working copy.
    ".env", ".env.*", ".claude", ".codex", ".opencode", ".codegraph", "*.pem", "*.key",
    ".mypy_cache", ".ruff_cache", "dist", ".tmp",
)

MODE_TURNS = "turns"  # iterative: the model chooses bounded actions turn by turn
MODE_WRITES = "writes"  # one-shot: the model proposes whole-file writes once per attempt


@dataclass(frozen=True)
class FileWriteAction:
    """A proposed whole-file write (declared by the agent, not yet evidence)."""

    path: str
    content: str


@dataclass
class LoopContext:
    """Inspect-phase output handed to the provider on every attempt."""

    task: str
    repo_files: list[str]
    attempt: int
    previous_failure: str | None = None  # verification output tail, in memory only
    blocked_paths: list[str] = field(default_factory=list)


# provider(context) -> proposed actions. In production this wraps a model call.
ActionProvider = Callable[[LoopContext], list[FileWriteAction]]
ProgressCallback = Callable[[str, dict[str, Any]], None]
# Role hooks (``openshard.osn.roles``). The planner sees the isolated copy and
# its file list and returns ``(plan or None, role record)``; the verifier sees
# the copy, the changed files, the observed verification and the attempt
# number and returns ``(review or None, role record)``. Both may raise
# ``BudgetExhausted`` before spending; the loop records the skip.
PlannerHook = Callable[[Path, list[str]], tuple[dict | None, dict]]
VerifierHook = Callable[[Path, list[str], "VerificationResult", int], tuple[dict | None, dict]]
# The parallel stage (``openshard.osn.workers`` / ``synthesis``): given the main
# copy, the plan and its file list it decides the topology, runs bounded isolated
# writing workers, synthesises their results INTO the main copy and returns a
# dict: ``topology`` (record), ``ran`` (bool), ``workers`` (records),
# ``synthesis`` (record), ``applied`` / ``blocked`` (paths), ``decisions``
# (policy decisions), ``advisory`` (text for the executor when conflicts or a
# missing required subtask need resolving, else None).
WorkersHook = Callable[[Path, dict | None, list[str]], dict]
# Durable state: called at every boundary the loop owns with the phase and the
# loop's resumable state (``resume_state``); the caller persists it.
CheckpointHook = Callable[[str, dict], None]
MAX_REVIEWS = 2
REVIEW_EVIDENCE = "model_reported"
RECOVERY_VERIFIED = "verified"  # the recovery attempt changed files and verification passed again
RECOVERY_NO_CHANGE = "no_change"  # the executor made no change; the verified state stands
RECOVERY_REVERTED = "reverted_to_verified_state"  # the recovery did not verify; its changes were undone


def _plan_progress_fields(plan: dict[str, Any] | None) -> dict[str, Any]:
    """The bounded, already-parsed plan as a progress payload: summary, steps, files, subtasks.

    These are the planner's own words (parsed and capped by ``parse_plan``),
    shown so the user sees what the executor was told to do, never the
    planner's deliberation.
    """
    if not isinstance(plan, dict):
        return {}
    steps = [s for s in (plan.get("steps") or []) if isinstance(s, str)]
    files = [f for f in (plan.get("files") or []) if isinstance(f, str)]
    subtasks = [s.get("id") for s in (plan.get("subtasks") or []) if isinstance(s, dict) and s.get("id")]
    return {
        "plan_summary": plan.get("summary") if isinstance(plan.get("summary"), str) else None,
        "plan_steps": steps,
        "plan_files": files,
        "plan_subtasks": subtasks,
        "plan_simple": bool(plan.get("simple")),
    }


def _emit_progress(progress: ProgressCallback | None, event: str, **data: Any) -> None:
    """Emit bounded run progress without ever letting UI code affect execution."""
    if progress is None:
        return
    try:
        progress(event, data)
    except Exception:
        pass


def _safe_error_message(exc: Exception) -> str | None:
    """A short, secret/path-safe provider error suitable for a Receipt."""
    return sanitize_text(str(exc), 180)


@dataclass
class VerificationResult:
    command: list[str]
    exit_code: int | None  # None: could not run / timed out
    passed: bool
    output_sha256: str
    output_bytes: int
    timed_out: bool = False
    tainted: bool = False  # the verifier modified the files it was verifying
    ran: bool = True  # False: the command could not be started; no outcome observed
    observed: bool = True  # run by OpenShard itself (equals ran)
    # Set when the output shows the verifier itself could not run (missing module,
    # command not found): an environment problem, no verdict on the change.
    setup_failure: str | None = None
    # Failing test ids read from the verifier's output (repo-relative
    # identifiers only, never the output itself); empty when none were named.
    failed_tests: list[str] = field(default_factory=list)


@dataclass
class AttemptRecord:
    n: int
    proposed: list[str]
    applied: list[str]
    blocked: list[str]
    policy: dict
    verification: VerificationResult | None = None
    # Canonical policy decisions for this attempt's proposed writes (file gate
    # plus path safety); the Shard entry's ``policy_decisions`` are built from these.
    decisions: list[dict] = field(default_factory=list)
    # Supervisor routing: what the supervisor decided after this attempt's
    # observed failure, and whether the loop acted on it.
    supervision: dict | None = None
    # Safe provider failure detail. Never raw output, stack traces, paths or secrets.
    error_class: str | None = None
    error_message: str | None = None
    # Iterative mode only: the model's turns and every declared action with
    # the harness's decision and observed effect (``ActionRecord.to_dict``).
    turns: int | None = None
    actions: list[dict] = field(default_factory=list)
    verifications_in_turn: int = 0
    turn_stop: str | None = None  # finished | max_turns | budget | provider_error | verifier_*
    final_note: str = ""
    # True for the bounded executor attempt an independent review asked for.
    review_recovery: bool = False
    # True when this attempt's writes came (at least partly) from parallel workers
    # combined by synthesis; its actions are then the executor's resolution turns only.
    parallel_stage: bool = False
    # True when that recovery attempt did not verify and its changes were undone:
    # its verification describes bytes that no longer exist, so it never
    # defines the run's verification state.
    reverted: bool = False
    # True for an attempt an earlier process finished and checkpointed; this
    # process restored it (evidence recorded by OpenShard then, not observed now).
    resumed: bool = False


@dataclass
class LoopReceipt:
    task_id: str
    status: str  # verified | failed | blocked | no_actions | error | budget_exhausted
    stop_reason: str
    attempts: list[AttemptRecord]
    changed_files: list[str]
    sandbox_path: str
    schema_version: int = SCHEMA_VERSION
    receipt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # sha256 of each changed file as verified; in memory only, used to refuse
    # promoting bytes that differ from what was verified.
    verified_file_hashes: dict[str, str] = field(default_factory=dict)
    # Privacy-safe command decision for the user-supplied verifier. Raw argv is
    # never stored; this is the same PolicyDecision shape as file mutations.
    command_decision: dict | None = None
    mode: str = MODE_WRITES
    # Roles that took part (``openshard.osn.roles.RoleRun`` records keyed by role),
    # the planner's plan, and every independent review with its outcome.
    roles: dict[str, dict] = field(default_factory=dict)
    plan: dict | None = None
    reviews: list[dict] = field(default_factory=list)
    # Execution topology (``openshard.osn.topology``), the parallel workers that
    # ran (``openshard.osn.workers`` records) and how their results were combined
    # (``openshard.osn.synthesis`` record). Empty / None for single-executor runs.
    topology: dict | None = None
    workers: list[dict] = field(default_factory=list)
    synthesis: dict | None = None
    # Parallel candidates (``openshard.osn.candidates``): every candidate's evaluation and the winner.
    candidates: dict | None = None
    # Set when this run continued an earlier process's checkpoint: what was carried over.
    resumed: dict | None = None

    @property
    def review_verdict(self) -> str | None:
        """The last independent review's verdict (model-reported), or None."""
        return self.reviews[-1].get("verdict") if self.reviews else None

    def effective_attempts(self) -> list[AttemptRecord]:
        """The attempts whose effects are still in the isolated copy (reverted recoveries excluded)."""
        return [a for a in self.attempts if not a.reverted]

    @property
    def verification_state(self) -> str:
        for a in reversed(self.effective_attempts()):
            if a.verification is None:
                continue
            if not a.verification.ran:
                return "not_run"
            if a.verification.timed_out:
                return "unknown"
            return "passed" if a.verification.passed else "failed"
        return "not_run"

    def to_dict(self) -> dict:
        attempts = []
        for a in self.attempts:
            item: dict[str, Any] = {
                "n": a.n,
                "proposed": [_display_path(p) for p in a.proposed],
                "applied": a.applied,
                "blocked": [_display_path(p) for p in a.blocked],
                "policy": _stored_policy(a.policy),
                "verification": _stored_verification(a.verification),
                "error": (
                    {"class": a.error_class, "message": a.error_message}
                    if a.error_class else None
                ),
            }
            if a.turns is not None:
                item["turns"] = a.turns
                item["turn_stop"] = a.turn_stop
                item["actions"] = [dict(x) for x in a.actions]
                item["action_summary"] = _action_summary_from_dicts(a.actions)
                item["verifications_in_turn"] = a.verifications_in_turn
                if a.final_note:
                    item["final_note"] = a.final_note
            if a.review_recovery:
                item["review_recovery"] = True
            if a.reverted:
                item["reverted"] = True
            if a.parallel_stage:
                item["parallel_stage"] = True
            if a.resumed:
                item["resumed_from_checkpoint"] = True
            attempts.append(item)
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "task_id": self.task_id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "mode": self.mode,
            "verification_state": self.verification_state,
            "changed_files": list(self.changed_files),
            "sandbox_path": self.sandbox_path,
            "attempts": attempts,
            "roles": {k: dict(v) for k, v in self.roles.items()},
            "plan": dict(self.plan) if self.plan else None,
            "reviews": [dict(r) for r in self.reviews],
            "topology": dict(self.topology) if self.topology else None,
            "workers": [dict(w) for w in self.workers],
            "synthesis": dict(self.synthesis) if self.synthesis else None,
            "candidates": dict(self.candidates) if self.candidates else None,
            "resumed": dict(self.resumed) if self.resumed else None,
            "command_policy": self.command_decision,
            "evidence": {
                "actions": "agent_declared",
                "policy_and_file_effects": "openshard_observed",
                "action_results": "openshard_observed",
                "verification": "openshard_observed",
                "task_text_stored": False,
            },
        }


def _action_summary_from_dicts(actions: list[dict]) -> dict[str, int]:
    from openshard.osn.actions import ActionRecord

    records = []
    for d in actions:
        try:
            records.append(ActionRecord(**{k: v for k, v in d.items() if k in ActionRecord.__dataclass_fields__}))
        except TypeError:
            continue
    return summarize_actions(records)


def _display_path(p: str) -> str:
    """Model-supplied paths that are absolute, escaping or carry control characters are masked."""
    norm = p.replace("\\", "/")
    if norm.startswith("/") or ":" in norm or ".." in norm.split("/") or norm.startswith("~"):
        return "<unsafe-path>"
    if any(unicodedata.category(ch) == "Cc" for ch in p):
        return "<unsafe-path>"
    return p


def _stored_decision(decision: PolicyDecision | None, approval_source: str | None = None) -> dict:
    """A policy decision as stored: the same shape the run pipeline writes, with any
    model-supplied path masked exactly like the rest of the receipt, plus the
    observed approval channel (``approver_error`` when the approver itself failed)."""
    if decision is None:
        return {}
    d = asdict(decision)
    if isinstance(d.get("resource"), str):
        d["resource"] = _display_path(d["resource"])
    if approval_source:
        d["approval_source"] = approval_source
    return d


def _stored_policy(policy: dict) -> dict:
    """The gate summary lists raw proposed paths; mask unsafe ones like the rest."""
    return {
        k: [_display_path(x) if isinstance(x, str) else x for x in v] if isinstance(v, list) else v
        for k, v in policy.items()
    }


def _stored_verification(v: VerificationResult | None) -> dict | None:
    if v is None:
        return None
    d = dict(vars(v))
    # Keep only the executable's name: full argv can carry secrets or local paths.
    exe = v.command[0] if v.command else ""
    d["command"] = [exe.replace("\\", "/").rsplit("/", 1)[-1]] if exe else []
    return d


def _hash_files(root: Path, rels: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in rels:
        p = root / rel
        try:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<missing>"
        except OSError:
            out[rel] = "<unreadable>"
    return out


def create_isolated_copy(repo_root: Path) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="osn-loop-")) / "work"
    shutil.copytree(repo_root, dest, ignore=_COPY_IGNORE)
    return dest


def _list_files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


def _as_text(v: str | bytes | None) -> str:
    if v is None:
        return ""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v


def _run_verification(command: list[str], cwd: Path, timeout: float) -> tuple[VerificationResult, str]:
    try:
        proc = subprocess.run(
            command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        code: int | None = proc.returncode
        timed_out = False
        ran = True
    except subprocess.TimeoutExpired as exc:
        out = "".join(_as_text(x) for x in (exc.stdout, exc.stderr))
        code, timed_out, ran = None, True, True
    except OSError as exc:
        out, code, timed_out, ran = f"could not run: {exc}", None, False, False
    result = VerificationResult(
        command=list(command),
        exit_code=code,
        passed=code == 0,
        output_sha256=hashlib.sha256(out.encode("utf-8", "replace")).hexdigest(),
        output_bytes=len(out.encode("utf-8", "replace")),
        timed_out=timed_out,
        ran=ran,
        observed=ran,
    )
    return result, out


def _observe_verification(
    sandbox: Path,
    changed: list[str],
    verify_command: list[str],
    timeout: float,
    budget: BudgetLedger | None,
) -> tuple[VerificationResult, str]:
    """Run the verifier once in *sandbox* and classify what OpenShard observed.

    Raises ``BudgetExhausted`` before launching when the command budget is spent.
    Sets ``tainted`` when the verifier rewrote the files it was checking (a pass
    then proves nothing), ``setup_failure`` when the output shows the verifier
    itself could not run (no verdict on the change), and ``failed_tests`` on an
    ordinary observed failure.
    """
    if budget is not None:
        budget.authorize_command()
    before = _hash_files(sandbox, changed)
    result, output = _run_verification(verify_command, sandbox, timeout)
    after = _hash_files(sandbox, changed)
    if after != before:
        result.passed = False
        result.tainted = True
        return result, output
    if result.passed or result.timed_out:
        return result, output
    kind = detect_setup_failure(result.exit_code, output)
    if kind is not None:
        # A missing tool is not something another model call can fix. No outcome was
        # observed for the proposed change, so this is not a failed verification.
        result.setup_failure = kind
        result.ran = False
        result.observed = False
        result.passed = False
        return result, output
    result.failed_tests = failing_test_ids(output)
    return result, output


def _is_turn_provider(provider: Any) -> bool:
    return callable(getattr(provider, "turn", None))


def resume_state(*, sandbox: Path, attempts: list[AttemptRecord], changed: list[str], prev_fingerprint: str | None,
                 prev_failure: str | None, blocked_seen: list[str], prev_actions: str | None, roles: dict,
                 plan: dict | None, reviews: list[dict], topology: dict | None, workers: list[dict],
                 synthesis: dict | None, candidates: dict | None = None) -> dict:
    """The loop's resumable state as plain data (attempts in full, verification results included)."""
    return {
        "sandbox": str(sandbox),
        "attempts": [asdict(a) for a in attempts],
        "changed": list(changed),
        "prev_fingerprint": prev_fingerprint,
        "prev_failure": prev_failure,
        "blocked_seen": list(blocked_seen),
        "prev_actions": prev_actions,
        "roles": {k: dict(v) for k, v in roles.items()},
        "plan": dict(plan) if plan else None,
        "reviews": [dict(r) for r in reviews],
        "topology": dict(topology) if topology else None,
        "workers": [dict(w) for w in workers],
        "synthesis": dict(synthesis) if synthesis else None,
        "candidates": dict(candidates) if candidates else None,
    }


def _restore_state(state: dict) -> dict:
    """``resume_state`` back into live objects; every restored attempt is flagged ``resumed``."""
    attempts: list[AttemptRecord] = []
    for d in state.get("attempts") or []:
        if not isinstance(d, dict):
            continue
        v = d.get("verification")
        verification = None
        if isinstance(v, dict):
            verification = VerificationResult(**{k: val for k, val in v.items()
                                                 if k in VerificationResult.__dataclass_fields__})
        fields = {k: val for k, val in d.items() if k in AttemptRecord.__dataclass_fields__ and k != "verification"}
        rec = AttemptRecord(**fields)
        rec.verification = verification
        rec.resumed = True
        attempts.append(rec)
    return {
        "attempts": attempts,
        "changed": [str(p) for p in state.get("changed") or []],
        "prev_fingerprint": state.get("prev_fingerprint"),
        "prev_failure": state.get("prev_failure"),
        "blocked_seen": [str(p) for p in state.get("blocked_seen") or []],
        "prev_actions": state.get("prev_actions"),
        "roles": {str(k): dict(v) for k, v in (state.get("roles") or {}).items() if isinstance(v, dict)},
        "plan": dict(state["plan"]) if isinstance(state.get("plan"), dict) else None,
        "reviews": [dict(r) for r in state.get("reviews") or [] if isinstance(r, dict)],
        "topology": dict(state["topology"]) if isinstance(state.get("topology"), dict) else None,
        "workers": [dict(w) for w in state.get("workers") or [] if isinstance(w, dict)],
        "synthesis": dict(state["synthesis"]) if isinstance(state.get("synthesis"), dict) else None,
        "candidates": dict(state["candidates"]) if isinstance(state.get("candidates"), dict) else None,
    }


def _snapshot(root: Path, rels: list[str]) -> dict[str, bytes | None]:
    """The bytes of *rels* under *root* (None for an absent file), to restore a verified state."""
    out: dict[str, bytes | None] = {}
    for rel in rels:
        p = root / rel
        try:
            out[rel] = p.read_bytes() if p.is_file() else None
        except OSError:
            out[rel] = None
    return out


def _restore(root: Path, snapshot: dict[str, bytes | None], changed_after: list[str]) -> None:
    """Put the isolated copy back to *snapshot*; files created since are removed."""
    for rel in changed_after:
        if rel not in snapshot:
            try:
                (root / rel).unlink(missing_ok=True)
            except OSError:
                pass
    for rel, data in snapshot.items():
        p = root / rel
        try:
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
        except OSError:
            pass


def run_bounded_loop(
    repo_root: Path,
    task: str,
    provider: Any,
    verify_command: list[str],
    *,
    task_id: str | None = None,
    max_attempts: int = 2,
    approver: Approver | None = None,
    organisation_approver: Approver | None = None,
    verify_timeout: float = 120.0,
    sandbox_path: Path | None = None,
    budget: BudgetLedger | None = None,
    supervisor: Any | None = None,
    progress: ProgressCallback | None = None,
    blocked_write_patterns: tuple[str, ...] = (),
    approval_write_patterns: tuple[str, ...] = (),
    blocked_command_prefixes: tuple[str, ...] = (),
    max_turns: int = DEFAULT_MAX_TURNS,
    max_verifications_per_attempt: int = DEFAULT_MAX_VERIFICATIONS,
    planner: PlannerHook | None = None,
    verifier: VerifierHook | None = None,
    max_reviews: int = MAX_REVIEWS,
    workers: WorkersHook | None = None,
    checkpoint: CheckpointHook | None = None,
    resume: dict | None = None,
) -> LoopReceipt:
    """Run the bounded loop. Never writes to *repo_root*.

    With a *checkpoint* hook the loop hands its resumable state to the caller
    at every boundary it owns (planned, workers staged, each attempt done).
    With *resume* (a state such a hook received, see ``resume_state``) the
    loop restores the plan, role records, topology and finished attempts,
    skips the planner and the workers stage, and continues with the next
    attempt in *sandbox_path*, which the caller prepared with the
    checkpointed files.

    *provider* is either a turn provider (``.turn(state)``; the iterative
    agent loop, ``max_turns`` turns per attempt, ``max_verifications_per_attempt``
    model-requested verifications per attempt) or an action provider
    (``provider(ctx) -> [FileWriteAction]``; one proposal per attempt).

    A *planner* hook runs once before the first attempt (read-only) and its
    plan reaches a turn provider through ``set_plan``. A *verifier* hook runs
    after an attempt OpenShard itself verified; its verdict is recorded as
    model-reported evidence and never changes the verification status. A
    ``fail`` verdict may trigger one bounded executor recovery attempt (turn
    providers only); if that attempt does not verify, its changes are undone
    and the verified state stands. At most ``max_reviews`` reviews per run.

    With a *budget* (Agent Budgets, capability-gated by the caller) each
    attempt, verify-command launch and file write is authorized first; the
    first limit that would be exceeded ends the run with status
    ``budget_exhausted`` and no further model call, command or write.

    With a *supervisor* (Supervisor Routing, capability-gated by the caller)
    each observed verification failure that the loop would retry is first put
    to ``supervisor.after_failed_attempt``; its decision is recorded on the
    attempt and, when the supervisor is applied, a ``stop`` ends the run and
    an ``escalate`` sets the next attempt's model on the provider.
    """
    task_id = task_id or f"task_{uuid.uuid4()}"
    max_attempts = max(1, min(max_attempts, 5))  # hard bound
    sandbox = sandbox_path or create_isolated_copy(repo_root)
    rr, sb = repo_root.resolve(), sandbox.resolve()
    if sb == rr or rr in sb.parents or sb in rr.parents:
        raise ValueError("sandbox_path must be separate from repo_root")
    iterative = _is_turn_provider(provider)
    mode = MODE_TURNS if iterative else MODE_WRITES
    attempts: list[AttemptRecord] = []
    changed: list[str] = []
    prev_fingerprint: str | None = None
    prev_failure: str | None = None
    blocked_seen: list[str] = []
    prev_actions: str | None = None
    roles: dict[str, dict] = {}
    plan: dict | None = None
    reviews: list[dict] = []
    topology: dict | None = None
    worker_records: list[dict] = []
    synthesis_record: dict | None = None
    candidates_record: dict | None = None
    max_reviews = max(0, min(int(max_reviews), MAX_REVIEWS))
    resumed_record: dict | None = None
    if resume:
        restored = _restore_state(resume)
        attempts = restored["attempts"]
        changed = restored["changed"]
        prev_fingerprint = restored["prev_fingerprint"]
        prev_failure = restored["prev_failure"]
        blocked_seen = restored["blocked_seen"]
        prev_actions = restored["prev_actions"]
        roles = restored["roles"]
        plan = restored["plan"]
        reviews = restored["reviews"]
        topology = restored["topology"]
        worker_records = restored["workers"]
        synthesis_record = restored["synthesis"]
        candidates_record = restored["candidates"]
        resumed_record = {
            "attempts_restored": len(attempts), "plan_restored": plan is not None,
            "topology_restored": topology is not None, "files_restored": len(changed),
            "evidence": "checkpoint_recorded_by_openshard",
        }
        if plan is not None:
            set_plan = getattr(provider, "set_plan", None)
            if callable(set_plan):
                set_plan(plan)
    start_attempt = len(attempts) + 1

    _emit_progress(progress, "workspace_ready", mode=mode, resumed=bool(resume))
    command_record: dict | None = None

    def _checkpoint(phase: str) -> None:
        if checkpoint is None:
            return
        try:
            checkpoint(phase, resume_state(
                sandbox=sandbox, attempts=attempts, changed=changed, prev_fingerprint=prev_fingerprint,
                prev_failure=prev_failure, blocked_seen=blocked_seen, prev_actions=prev_actions, roles=roles,
                plan=plan, reviews=reviews, topology=topology, workers=worker_records, synthesis=synthesis_record,
                candidates=candidates_record,
            ))
        except Exception:
            # Durability must never change what the run does.
            _emit_progress(progress, "checkpoint_failed", phase=phase)

    def _receipt(status: str, reason: str) -> LoopReceipt:
        if verifier is not None and status != "verified" and "verifier" not in roles:
            # A review was configured but there was never a verified result to review.
            roles["verifier"] = {"role": "verifier", "status": "skipped", "reason": "no_verified_result_to_review"}
        return LoopReceipt(
            task_id,
            status,
            reason,
            attempts,
            changed,
            str(sandbox),
            command_decision=command_record,
            mode=mode,
            roles=roles,
            plan=plan,
            reviews=reviews,
            topology=topology,
            workers=worker_records,
            synthesis=synthesis_record,
            candidates=candidates_record,
            resumed=resumed_record,
        )

    # --verify-cmd is an explicit user choice and historically runs as supplied.
    # Organisation policy may tighten that boundary with an explicit prefix deny,
    # but the generic command classifier must not reinterpret existing verifier
    # forms (for example python -c with punctuation) and break compatibility.
    command_blocked = organisation_command_blocked(
        verify_command,
        blocked_command_prefixes,
    )
    command_record = {
        "scope": "verification:execute",
        "state": "blocked" if command_blocked else "granted",
    }
    if command_blocked:
        return _receipt("blocked", "verification_command_policy_block")

    # An applied supervisor escalation is confirmed only when the next attempt
    # really calls the recommended model; until then the previous attempt's
    # record says "pending". If the run ends first, it says so.
    pending_supervision: AttemptRecord | None = None

    def _settle_supervision(reason: str | None) -> None:
        nonlocal pending_supervision
        if pending_supervision is None or supervisor is None:
            return
        if reason is None:
            supervisor.mark_acted()
        else:
            supervisor.mark_not_acted(reason)
        pending_supervision.supervision = supervisor.decisions[-1].to_record()
        pending_supervision = None

    def _verify(paths: list[str]) -> tuple[VerificationResult, str]:
        return _observe_verification(sandbox, paths, verify_command, verify_timeout, budget)

    if planner is not None and not resume:
        # Read-only planning before any write. A plan is advisory context for the
        # executor; a planner that fails or is stopped by the budget is recorded,
        # and the run goes on without a plan.
        _emit_progress(progress, "role_start", role="planner")
        try:
            plan, planner_record = planner(sandbox, _list_files(sandbox))
        except BudgetExhausted as exc:
            planner_record = {"role": "planner", "status": "skipped", "reason": exc.stop_reason}
            plan = None
        roles["planner"] = dict(planner_record)
        _emit_progress(progress, "role_end", role="planner", status=planner_record.get("status"),
                       model=planner_record.get("model"), has_plan=plan is not None,
                       reason=planner_record.get("reason"), **_plan_progress_fields(plan))
        if plan is not None:
            set_plan = getattr(provider, "set_plan", None)
            if callable(set_plan):
                set_plan(plan)
        _checkpoint("planned")

    def _review_recovery(n: int, concerns: list[str], same_model: str | None) -> tuple[bool, str]:
        """One bounded executor attempt in answer to a failed independent review.

        Returns ``(verified again, outcome token)``. On anything but a new
        verified state the isolated copy is restored to the verified bytes.
        """
        snapshot = _snapshot(sandbox, changed)
        changed_before = list(changed)
        if budget is not None:
            try:
                budget.start_attempt()
            except BudgetExhausted as exc:
                roles["verifier"] = {**roles.get("verifier", {}), "recovery_skipped": exc.stop_reason}
                return False, RECOVERY_NO_CHANGE
        setter = getattr(provider, "set_next_model", None)
        if same_model and callable(setter):
            setter(same_model)  # no escalation: the deterministic verification passed
        begin = getattr(provider, "begin_attempt", None)
        if callable(begin):
            begin(n)
        model_getter = getattr(provider, "pending_model_for", None)
        pending = model_getter(n) if callable(model_getter) else same_model
        _emit_progress(progress, "attempt_start", attempt=n, model=pending, review_recovery=True)
        gate = FileMutationGate(
            approver=approver, organisation_approver=organisation_approver,
            blocked_patterns=blocked_write_patterns, approval_patterns=approval_write_patterns,
        )
        advisory = (
            "An independent review of your verified change raised concerns (model-reported; the deterministic "
            "verification PASSED). Address them only where they are valid, and keep verification passing:\n- "
            + "\n- ".join(concerns or ["(no specific concern recorded)"])
        )
        outcome = run_attempt_turns(
            repo_root=repo_root, sandbox=sandbox, task=task, attempt=n, provider=provider, gate=gate,
            verify=_verify, budget=budget, previous_failure=advisory, blocked_seen=list(blocked_seen),
            changed_so_far=list(changed), max_turns=max_turns, max_verifications=max_verifications_per_attempt,
            progress=progress, model_label=lambda: pending,
            blocked_write_patterns=blocked_write_patterns, approval_write_patterns=approval_write_patterns,
            repo_files=_list_files(sandbox),
        )
        rec = AttemptRecord(
            n, list(outcome.proposed), list(outcome.applied), list(outcome.blocked), gate.summary(),
            decisions=list(outcome.decisions), turns=outcome.turns,
            actions=[r.to_dict() for r in outcome.records], verifications_in_turn=outcome.verifications_run,
            turn_stop=outcome.stop, final_note=outcome.final_note, review_recovery=True,
        )
        if outcome.stop == STOP_PROVIDER_ERROR:
            rec.error_class, rec.error_message = outcome.error_class, outcome.error_message
        attempts.append(rec)
        for p in outcome.applied:
            if p not in changed:
                changed.append(p)
        _emit_progress(progress, "policy_result", attempt=n, applied=len(outcome.applied),
                       blocked=len(outcome.blocked), turns=outcome.turns)
        if outcome.stop not in ("finished", "max_turns") or not outcome.applied:
            _restore(sandbox, snapshot, changed)
            del changed[len(changed_before):]
            if outcome.applied:
                rec.reverted = True
            return False, RECOVERY_NO_CHANGE if outcome.stop in ("finished", "max_turns") else RECOVERY_REVERTED
        fp = state_fingerprint(sandbox, changed)
        if outcome.verification is not None and outcome.verification_state == fp:
            result, _output = outcome.verification, outcome.verification_output
            _emit_progress(progress, "verification_reused", attempt=n)
        else:
            try:
                _emit_progress(progress, "verification_start", attempt=n)
                result, _output = _verify(changed)
            except BudgetExhausted:
                _restore(sandbox, snapshot, changed)
                del changed[len(changed_before):]
                rec.reverted = True
                return False, RECOVERY_REVERTED
        rec.verification = result
        _emit_progress(progress, "verification_result", attempt=n, **verification_progress_fields(result, _output))
        if result.passed and not result.tainted:
            return True, RECOVERY_VERIFIED
        _restore(sandbox, snapshot, changed)
        del changed[len(changed_before):]
        rec.reverted = True
        return False, RECOVERY_REVERTED

    def _verified(n: int, result: VerificationResult) -> LoopReceipt:
        """The attempt verified: run the independent review (bounded) and finish."""
        nonlocal reviews
        reviews_run = 0
        current_attempt = n
        while verifier is not None and reviews_run < max_reviews:
            _emit_progress(progress, "role_start", role="verifier", attempt=current_attempt)
            try:
                review, verifier_record = verifier(sandbox, list(changed), result, current_attempt)
            except BudgetExhausted as exc:
                roles["verifier"] = {"role": "verifier", "status": "skipped", "reason": exc.stop_reason}
                _emit_progress(progress, "role_end", role="verifier", status="skipped", reason=exc.stop_reason)
                break
            reviews_run += 1
            roles["verifier"] = dict(verifier_record)
            _emit_progress(progress, "role_end", role="verifier", status=verifier_record.get("status"),
                           model=verifier_record.get("model"), verdict=(review or {}).get("verdict"))
            if review is None:
                break
            concerns: list[str] = [c for c in (review.get("concerns") or []) if isinstance(c, str)]
            entry: dict[str, Any] = {
                "attempt": current_attempt, "verdict": review.get("verdict"), "summary": review.get("summary"),
                "concerns": concerns, "evidence": REVIEW_EVIDENCE,
                "model": verifier_record.get("model"), "independent": verifier_record.get("independent"),
                "recovery_requested": False, "recovery_outcome": None,
            }
            reviews.append(entry)
            if review.get("verdict") != "fail" or not iterative:
                break
            if reviews_run >= max_reviews or len(attempts) >= max_attempts:
                break
            # The review found a problem the tests did not. One bounded executor
            # attempt on the same model; its result is verified like any other.
            entry["recovery_requested"] = True
            usage_for = getattr(provider, "usage_for", None)
            same_model = None
            if callable(usage_for):
                try:
                    same_model = usage_for(current_attempt)[0]
                except Exception:
                    same_model = None
            current_attempt = len(attempts) + 1
            _emit_progress(progress, "recovery_decision", attempt=current_attempt - 1, action="review_recovery",
                           reason="independent_review_fail", model=same_model, acted_on=True)
            ok, outcome_token = _review_recovery(current_attempt, concerns, same_model)
            entry["recovery_outcome"] = outcome_token
            if not ok:
                break
            result = attempts[-1].verification  # type: ignore[assignment]
        receipt = _receipt("verified", "verification_passed")
        receipt.verified_file_hashes = _hash_files(sandbox, changed)
        return receipt

    for n in range(start_attempt, max_attempts + 1):
        if budget is not None:
            try:
                budget.start_attempt()
            except BudgetExhausted as exc:
                _settle_supervision("run_ended_before_retry")
                return _receipt(STATUS_BUDGET_EXHAUSTED, exc.stop_reason)
        model_getter = getattr(provider, "pending_model_for", None)
        pending_model = model_getter(n) if callable(model_getter) else None
        workers_stage = iterative and workers is not None and n == 1 and topology is None
        if not workers_stage:
            _emit_progress(progress, "attempt_start", attempt=n, model=pending_model)
        gate = FileMutationGate(
            approver=approver,
            organisation_approver=organisation_approver,
            blocked_patterns=blocked_write_patterns,
            approval_patterns=approval_write_patterns,
        )
        applied: list[str] = []
        blocked: list[str] = []
        attempt_decisions: list[dict] = []  # in the order the model proposed the writes
        budget_stop: BudgetExhausted | None = None
        rec: AttemptRecord
        # Set when the iterative attempt already ran the verifier on the
        # artifact state it finished with; the loop then does not run it again.
        in_turn_result: VerificationResult | None = None
        in_turn_output = ""

        if iterative:
            # Parallel stage, first attempt only: the hook decides the topology; when
            # it ran workers, their synthesised files are already in the main copy and
            # the executor's turns are needed only to resolve what synthesis could not.
            stage: dict | None = None
            stage_advisory: str | None = None
            skip_turns = False
            if workers_stage and workers is not None:
                _emit_progress(progress, "stage_start", stage="workers", attempt=n)
                try:
                    stage = workers(sandbox, plan, _list_files(sandbox))
                except BudgetExhausted as exc:
                    topology = {"topology_requested": "auto", "topology_selected": "single",
                                "topology_reason": "budget_headroom_insufficient", "worker_count": 0}
                    _settle_supervision("run_ended_before_retry")
                    attempts.append(AttemptRecord(n, [], [], [], {"budget_stop": exc.stop_reason}))
                    return _receipt(STATUS_BUDGET_EXHAUSTED, exc.stop_reason)
                topology = dict(stage.get("topology") or {}) or None
                if stage.get("ran"):
                    worker_records.extend(dict(w) for w in stage.get("workers") or [])
                    synthesis_record = dict(stage.get("synthesis") or {}) or None
                    candidates_record = dict(stage.get("candidates") or {}) or None
                    for p in stage.get("applied") or []:
                        if p not in changed:
                            changed.append(p)
                    stage_advisory = stage.get("advisory") or None
                    skip_turns = stage_advisory is None and bool(stage.get("applied"))
                    _emit_progress(
                        progress, "stage_end", stage="candidates" if candidates_record else "workers",
                        workers=len(stage.get("workers") or []),
                        applied=len(stage.get("applied") or []), conflicts=len((synthesis_record or {}).get("conflicts") or []),
                        resolution="executor_turns" if stage_advisory else "none_needed",
                        winner=(candidates_record or {}).get("winner"), winner_model=(candidates_record or {}).get("winner_model"),
                    )
                else:
                    stage = None
                    _emit_progress(progress, "stage_skipped", stage="workers", attempt=n,
                                   reason=(topology or {}).get("topology_reason"))
                _checkpoint("workers_staged")
                if not skip_turns:
                    _emit_progress(progress, "attempt_start", attempt=n, model=pending_model,
                                   after_workers=stage is not None)
            begin = getattr(provider, "begin_attempt", None)
            if callable(begin):
                begin(n)
            if skip_turns:
                from openshard.osn.agent_loop import AttemptOutcome

                outcome = AttemptOutcome(stop="finished", final_note="parallel workers' files synthesised")
                outcome.turns = 0
            else:
                outcome = run_attempt_turns(
                    repo_root=repo_root, sandbox=sandbox, task=task, attempt=n, provider=provider,
                    gate=gate, verify=_verify, budget=budget,
                    previous_failure=stage_advisory if stage_advisory else prev_failure,
                    blocked_seen=list(blocked_seen), changed_so_far=list(changed),
                    max_turns=max_turns, max_verifications=max_verifications_per_attempt, progress=progress,
                    model_label=lambda: pending_model,
                    blocked_write_patterns=blocked_write_patterns, approval_write_patterns=approval_write_patterns,
                    repo_files=_list_files(sandbox),
                )
            stage_applied = list((stage or {}).get("applied") or [])
            stage_blocked = list((stage or {}).get("blocked") or [])
            stage_decisions = list((stage or {}).get("decisions") or [])
            rec = AttemptRecord(
                n, [*stage_applied, *stage_blocked, *outcome.proposed],
                [*stage_applied, *[p for p in outcome.applied if p not in stage_applied]],
                [*stage_blocked, *outcome.blocked], gate.summary(),
                decisions=[*stage_decisions, *outcome.decisions], turns=outcome.turns,
                actions=[r.to_dict() for r in outcome.records],
                verifications_in_turn=outcome.verifications_run, turn_stop=outcome.stop,
                final_note=outcome.final_note, parallel_stage=stage is not None,
            )
            attempts.append(rec)
            for p in outcome.applied:
                if p not in changed:
                    changed.append(p)
            blocked_seen.extend(p for p in outcome.blocked if p not in blocked_seen)
            if outcome.model_calls:
                _settle_supervision(None)  # the recommended model was called
            # A verification the model requested on exactly the files the attempt
            # ends with is evidence OpenShard observed; it is kept on the attempt
            # even when the attempt ends early, never replaced by "not run".
            if outcome.verification is not None and outcome.verification_state == state_fingerprint(sandbox, changed):
                rec.verification = outcome.verification
            if outcome.stop == STOP_PROVIDER_ERROR:
                _settle_supervision("run_ended_before_retry")
                rec.error_class, rec.error_message = outcome.error_class, outcome.error_message
                rec.policy = {**rec.policy, "provider_error": outcome.error_class}
                _emit_progress(
                    progress, "provider_error", attempt=n, model=pending_model,
                    error_class=outcome.error_class, message=outcome.error_message,
                )
                return _receipt("error", "provider_error")
            if outcome.stop == STOP_MALFORMED_REPLY:
                rec.error_class, rec.error_message = outcome.error_class, outcome.error_message
                if not outcome.applied:
                    rec.policy = {**rec.policy, "model_response_error": outcome.error_class}
                    if n < max_attempts:
                        # The model stopped speaking the contract before writing anything:
                        # a model failure, so the next attempt (the ladder's next rung)
                        # gets its chance, told why the previous one ended.
                        _emit_progress(progress, "malformed_attempt", attempt=n, next_attempt=n + 1)
                        prev_failure = (
                            "The previous attempt ended because the model's replies were not valid action "
                            f"lists ({outcome.error_message or 'unusable reply'}); nothing was written."
                        )
                        _checkpoint("attempt_done")
                        continue
                    # Nothing was written and no attempt remains: the same outcome as a bad one-shot reply.
                    _settle_supervision("run_ended_before_retry")
                    return _receipt("error", "provider_error")
                # Writes exist: they face verification like any other attempt, and a
                # failure can still be retried or escalated.
            if outcome.stop == STOP_BUDGET and outcome.budget_stop is not None:
                _settle_supervision("run_ended_before_retry")
                rec.policy = {**rec.policy, "budget_stop": outcome.budget_stop.stop_reason}
                return _receipt(STATUS_BUDGET_EXHAUSTED, outcome.budget_stop.stop_reason)
            _emit_progress(
                progress, "policy_result", attempt=n, applied=len(outcome.applied), blocked=len(outcome.blocked),
                turns=outcome.turns,
            )
            if outcome.stop in (STOP_VERIFIER_TAINTED, STOP_VERIFIER_TIMEOUT, STOP_VERIFIER_SETUP):
                rec.verification = outcome.verification
                if outcome.stop == STOP_VERIFIER_TAINTED:
                    return _receipt("failed", "verifier_modified_files")
                if outcome.stop == STOP_VERIFIER_TIMEOUT:
                    return _receipt("error", "verifier_timeout")
                return _receipt("error", "verifier_setup_failed")
            if outcome.stop == STOP_POLICY_BLOCK or outcome.blocked:
                # Policy/safety blocks are not retried automatically: the same
                # proposal would be blocked again and a human decision is needed.
                # Writes applied before the refusal stay in the isolated copy, unverified.
                return _receipt("blocked", "policy_or_path_block")
            if not outcome.applied and not stage_applied:
                _emit_progress(progress, "no_actions", attempt=n)
                return _receipt("no_actions", "provider proposed no actions")
            # Files synthesised from workers count as this attempt's writes: they are
            # verified below exactly like the executor's own.
            applied = [*stage_applied, *[p for p in outcome.applied if p not in stage_applied]]
            blocked = [*stage_blocked, *outcome.blocked]
            actions_fp = state_fingerprint(sandbox, changed)
            if actions_fp == prev_actions:
                # The attempt ended with the same bytes as the last (failed) one: no progress.
                return _receipt("failed", "no_progress_identical_actions")
            prev_actions = actions_fp
            if outcome.verification is not None and outcome.verification_state == actions_fp:
                in_turn_result, in_turn_output = outcome.verification, outcome.verification_output
        else:
            ctx = LoopContext(task, _list_files(sandbox), n, prev_failure, list(blocked_seen))
            try:
                actions = provider(ctx)
            except BudgetExhausted as exc:
                _emit_progress(progress, "budget_stop", attempt=n, reason=exc.stop_reason)
                # The provider consulted the same ledger before a call and refused
                # it. The attempt had started (an earlier call in it may have been
                # paid for, e.g. before a re-ask), so it is recorded like a
                # provider error: proposed nothing, applied nothing.
                _settle_supervision("run_ended_before_retry")
                attempts.append(AttemptRecord(n, [], [], [], {"budget_stop": exc.stop_reason}))
                return _receipt(STATUS_BUDGET_EXHAUSTED, exc.stop_reason)
            except Exception as exc:
                _settle_supervision("run_ended_before_retry")
                error_class = type(exc).__name__
                error_message = _safe_error_message(exc)
                attempts.append(AttemptRecord(
                    n, [], [], [], {"provider_error": error_class},
                    error_class=error_class, error_message=error_message,
                ))
                _emit_progress(
                    progress, "provider_error", attempt=n, model=pending_model,
                    error_class=error_class, message=error_message,
                )
                return _receipt("error", "provider_error")
            _settle_supervision(None)  # the recommended model was called
            _emit_progress(progress, "model_response", attempt=n, model=pending_model, proposed=len(actions))
            if not actions:
                _emit_progress(progress, "no_actions", attempt=n)
                return _receipt("no_actions", "provider proposed no actions")

            actions_fp = hashlib.sha256(
                "\x1f".join(f"{a.path}\x1f{a.content}" for a in actions).encode("utf-8", "replace")
            ).hexdigest()
            if actions_fp == prev_actions:
                # Same writes as the last (failed) attempt: a retry is not justified.
                return _receipt("failed", "no_progress_identical_actions")
            prev_actions = actions_fp

            for act in actions:
                try:
                    dest = resolve_safe_repo_path(sandbox, act.path)
                except UnsafePathError:
                    blocked.append(act.path)
                    attempt_decisions.append(_stored_decision(make_deny(
                        "file_write", act.path, "path escapes the repository or is unsafe",
                        source="path_safety", severity="high",
                    )))
                    continue
                permitted = gate.authorize(act.path)
                attempt_decisions.append(_stored_decision(gate.outcomes[-1].policy, gate.outcomes[-1].approval_source))
                if not permitted:
                    blocked.append(act.path)
                    continue
                if budget is not None:
                    try:
                        budget.authorize_write()
                    except BudgetExhausted as exc:
                        # Nothing past this point is written; what was already
                        # applied stays in the isolated copy only.
                        budget_stop = exc
                        break
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(act.content, encoding="utf-8")
                gate.mark_executed(act.path)
                applied.append(act.path)
                if act.path not in changed:
                    changed.append(act.path)
            rec = AttemptRecord(n, [a.path for a in actions], applied, blocked, gate.summary())
            rec.decisions = attempt_decisions
            attempts.append(rec)
            blocked_seen.extend(p for p in blocked if p not in blocked_seen)
            _emit_progress(
                progress, "policy_result", attempt=n, applied=len(applied), blocked=len(blocked),
            )

            if budget_stop is not None:
                return _receipt(STATUS_BUDGET_EXHAUSTED, budget_stop.stop_reason)
            if blocked:
                # Policy/safety blocks are not retried automatically: the same
                # proposal would be blocked again and a human decision is needed.
                return _receipt("blocked", "policy_or_path_block")

        # ---- verification (shared) ------------------------------------------
        if in_turn_result is not None:
            result, output = in_turn_result, in_turn_output
            _emit_progress(progress, "verification_reused", attempt=n)
        else:
            try:
                _emit_progress(progress, "verification_start", attempt=n)
                result, output = _verify(changed)
            except BudgetExhausted as exc:
                return _receipt(STATUS_BUDGET_EXHAUSTED, exc.stop_reason)
        rec.verification = result
        _emit_progress(progress, "verification_result", attempt=n, **verification_progress_fields(result, output))
        if result.tainted:
            # A pass on files the verifier itself rewrote proves nothing about
            # the proposed change, and those bytes must never be promoted.
            return _receipt("failed", "verifier_modified_files")
        if result.passed:
            return _verified(n, result)

        if result.timed_out:
            # The command started, but OpenShard did not observe a pass or fail
            # outcome. Retrying with another model would turn verifier uncertainty
            # into model-quality evidence, so stop without consulting recovery.
            return _receipt("error", "verifier_timeout")

        if result.setup_failure is not None:
            return _receipt("error", "verifier_setup_failed")

        fingerprint = result.output_sha256
        if fingerprint == prev_fingerprint:
            return _receipt("failed", "no_progress_identical_failure")
        status_line = "timed out" if result.timed_out else f"exit code {result.exit_code}"
        prev_fingerprint = fingerprint
        # Always non-empty, even when the verifier prints nothing.
        prev_failure = f"verify command failed ({status_line})\n{output[-2000:]}"
        # The run goes on: everything a later process needs to continue from here.
        # (A supervisor escalation decided below is not carried; a resume follows the ladder.)
        _checkpoint("attempt_done")

        budget_would_stop = budget is not None and budget.would_stop_next_attempt() is not None
        if supervisor is not None and n < max_attempts and not budget_would_stop:
            # The one meaningful boundary in this loop: an observed failure the
            # loop is about to retry (and that a budget would not refuse anyway:
            # the budget's own stop is never pre-empted). The supervisor sees
            # exactly what happened.
            decision = supervisor.after_failed_attempt(
                n, verification_observed=bool(result.observed), loop_max_attempts=max_attempts,
            )
            rec.supervision = decision.to_record()
            _emit_progress(
                progress, "recovery_decision", attempt=n, action=decision.action,
                reason=decision.reason, model=decision.recommended_model,
                acted_on=decision.acted_on,
            )
            if decision.acted_on is not False:
                if decision.action == "stop":
                    return _receipt("failed", f"supervisor_stop:{decision.reason}")
                if decision.action == "escalate" and decision.recommended_model:
                    setter = getattr(provider, "set_next_model", None)
                    if callable(setter):
                        setter(decision.recommended_model)
                        pending_supervision = rec
                    else:
                        supervisor.mark_not_acted("provider_cannot_switch")
                        rec.supervision = decision.to_record()

    return _receipt("failed", "max_attempts_exhausted")
