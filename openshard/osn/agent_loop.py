"""The iterative OSN agent loop: one attempt as a bounded sequence of model turns.

Each turn the model declares a few typed actions (``openshard.osn.actions``);
the harness decides whether each may happen, performs it in the isolated
copy, and hands the compact result back as the next turn's observation::

    observe -> model turn -> validate each action -> execute -> observe result
            -> model turn -> ... -> run_verification -> ... -> finish

Authority never moves to the model:

* read-only actions resolve through ``resolve_safe_repo_path`` against the
  isolated copy and refuse protected paths;
* every write passes the ``FileMutationGate`` (allow / ask -> approver / deny,
  built-in and organisation patterns), then the budget ledger, then
  ``NativeToolRunner.write_file`` which re-checks path and policy itself;
* verification is the loop's own command, run by OpenShard; the model may ask
  for it a bounded number of times per attempt and sees only its outcome and
  a short output tail;
* turns, actions per turn, verifications and the observation window are all
  capped, and a budget can stop any unit of work before it happens.

Evidence: every declared action becomes an :class:`ActionRecord` with the
decision, whether it executed, and counts/hashes of what happened. Tool
output, file contents and prompts stay in memory for the next turn only; the
Receipt never carries them.
"""
from __future__ import annotations

import difflib
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from openshard.native.tool_runner import NativeToolRunner
from openshard.native.tools import NativeToolCall, compact_tool_result
from openshard.osn.actions import (
    DECISION_ALLOW,
    DECISION_ASK,
    DECISION_DENY,
    DECISION_INVALID,
    DECISION_NOT_APPLICABLE,
    KIND_FINISH,
    KIND_GET_DIFF,
    KIND_LIST_FILES,
    KIND_READ_FILE,
    KIND_RUN_VERIFICATION,
    KIND_SEARCH_REPO,
    KIND_WRITE_FILE,
    ActionParseError,
    ActionRecord,
    AgentAction,
    TurnResult,
)
from openshard.osn.budget import BudgetExhausted, BudgetLedger
from openshard.policy.decision import make_deny
from openshard.policy.file_mutation import FileMutationGate, is_protected_path
from openshard.security.paths import UnsafePathError, resolve_safe_repo_path

ROLE_EXECUTOR = "executor"

DEFAULT_MAX_TURNS = 12
MAX_TURNS_HARD_CAP = 30
DEFAULT_MAX_VERIFICATIONS = 2
MAX_VERIFICATIONS_HARD_CAP = 5

# What one observation may carry into the next prompt, and how much the whole
# window may hold before older observations are compacted to one line.
READ_LIMIT_CHARS = 24_000
LIST_LIMIT_CHARS = 6_000
SEARCH_LIMIT_CHARS = 4_000
DIFF_LIMIT_CHARS = 6_000
VERIFY_TAIL_CHARS = 2_000
OBSERVATION_WINDOW_CHARS = 80_000
MAX_OBSERVATIONS_KEPT_FULL = 8

STOP_MAX_TURNS = "max_turns"
STOP_FINISHED = "finished"
STOP_BUDGET = "budget"
STOP_PROVIDER_ERROR = "provider_error"
STOP_VERIFIER_TAINTED = "verifier_modified_files"
STOP_VERIFIER_TIMEOUT = "verifier_timeout"
STOP_VERIFIER_SETUP = "verifier_setup_failed"
STOP_POLICY_BLOCK = "policy_block"  # a write was refused by path safety or policy
# The model's reply was not a usable action list even after the re-ask. The
# attempt ends here; what was written is verified like any other attempt.
STOP_MALFORMED_REPLY = "malformed_reply"

ERROR_UNSAFE_PATH = "unsafe_path"
ERROR_PROTECTED_PATH = "protected_path"
ERROR_TOOL_FAILED = "tool_failed"
ERROR_CAP_REACHED = "cap_reached"


@dataclass
class Observation:
    """What the model is shown about one executed (or refused) action. In memory only."""

    turn: int
    index: int
    kind: str
    target: str
    status: str  # ok | refused | failed
    text: str
    compacted: bool = False

    def one_line(self) -> str:
        return f"[turn {self.turn} #{self.index}] {self.kind} {self.target}: {self.status}"


@dataclass
class TurnState:
    """Everything a turn provider may use to build the next prompt."""

    task: str
    attempt: int
    turn: int
    max_turns: int
    repo_files: list[str]
    observations: list[Observation]
    changed_files: list[str]
    blocked_paths: list[str]
    previous_failure: str | None
    verifications_left: int
    writes_applied: int
    last_verification: str | None = None  # "passed" | "failed (exit 1)" | None


class TurnProvider(Protocol):
    """A model-backed provider for the iterative loop."""

    def turn(self, state: TurnState) -> TurnResult: ...


# verify(changed_paths) -> (VerificationResult-like, output). Supplied by the
# loop, which owns the command, the budget check, the taint check and the
# timeout / setup-failure classification.
VerifyFn = Callable[[list[str]], tuple[Any, str]]
ProgressFn = Callable[[str, dict[str, Any]], None] | None


@dataclass
class AttemptOutcome:
    records: list[ActionRecord] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)  # paths written (unique, in order)
    proposed: list[str] = field(default_factory=list)  # every write path the model declared
    blocked: list[str] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    turns: int = 0
    model_calls: int = 0
    stop: str = STOP_FINISHED
    budget_stop: BudgetExhausted | None = None
    error_class: str | None = None
    error_message: str | None = None
    verification: Any | None = None  # the last in-attempt verification result
    verification_output: str = ""
    verification_state: str | None = None  # fingerprint of changed files when it ran
    verifications_run: int = 0
    final_note: str = ""
    plan: dict[str, Any] | None = None  # the last structured plan a turn carried (planner role)
    findings: list[str] = field(default_factory=list)  # an explorer's compact answer (explorer role)
    sources: list[str] = field(default_factory=list)
    explorations: list[dict[str, Any]] = field(default_factory=list)  # what each exploration round produced

    @property
    def finished(self) -> bool:
        return self.stop == STOP_FINISHED


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _display_path(p: str) -> str:
    """Repo-relative display form; unsafe or escaping inputs are masked (same rule as the loop)."""
    import unicodedata

    norm = p.replace("\\", "/")
    if norm.startswith("/") or ":" in norm or ".." in norm.split("/") or norm.startswith("~"):
        return "<unsafe-path>"
    if any(unicodedata.category(ch) == "Cc" for ch in p):
        return "<unsafe-path>"
    return norm[:200]


def state_fingerprint(root: Path, rels: list[str]) -> str:
    """sha256 over (path, content hash) of *rels* under *root*: the artifact state a verification saw."""
    h = hashlib.sha256()
    for rel in sorted(rels):
        p = root / rel
        try:
            digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<missing>"
        except OSError:
            digest = "<unreadable>"
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\x1f")
        h.update(digest.encode())
        h.update(b"\x1e")
    return h.hexdigest()


def sandbox_diff_text(repo_root: Path, sandbox: Path, rels: list[str], *, limit: int = DIFF_LIMIT_CHARS) -> str:
    """Unified diff of *rels* between the real repository and the isolated copy. Bounded."""
    return _sandbox_diff(repo_root, sandbox, rels, limit=limit)


def _sandbox_diff(repo_root: Path, sandbox: Path, rels: list[str], *, limit: int) -> str:
    """Unified diff of *rels* between the real repository and the isolated copy. Bounded."""
    chunks: list[str] = []
    for rel in rels:
        try:
            after_p = resolve_safe_repo_path(sandbox, rel)
        except UnsafePathError:
            continue
        before_p = repo_root / rel
        before = before_p.read_text(encoding="utf-8", errors="replace").splitlines() if before_p.is_file() else []
        after = after_p.read_text(encoding="utf-8", errors="replace").splitlines() if after_p.is_file() else []
        diff = difflib.unified_diff(before, after, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="")
        text = "\n".join(diff)
        if text:
            chunks.append(text)
    return compact_tool_result("\n".join(chunks), limit) if chunks else "(no differences)"


def _compact_window(observations: list[Observation]) -> None:
    """Keep the newest observations in full; compact older ones to one line, bounded overall."""
    full = [o for o in observations if not o.compacted]
    while len(full) > MAX_OBSERVATIONS_KEPT_FULL:
        oldest = full.pop(0)
        oldest.text, oldest.compacted = "", True
    total = sum(len(o.text) for o in observations if not o.compacted)
    full = [o for o in observations if not o.compacted]
    while total > OBSERVATION_WINDOW_CHARS and len(full) > 1:
        oldest = full.pop(0)
        total -= len(oldest.text)
        oldest.text, oldest.compacted = "", True


def _emit(progress: ProgressFn, event: str, **data: Any) -> None:
    if progress is None:
        return
    try:
        progress(event, data)
    except Exception:
        pass


def run_attempt_turns(
    *,
    repo_root: Path,
    sandbox: Path,
    task: str,
    attempt: int,
    provider: TurnProvider,
    gate: FileMutationGate,
    verify: VerifyFn,
    budget: BudgetLedger | None,
    previous_failure: str | None,
    blocked_seen: list[str],
    changed_so_far: list[str],
    max_turns: int = DEFAULT_MAX_TURNS,
    max_verifications: int = DEFAULT_MAX_VERIFICATIONS,
    progress: ProgressFn = None,
    role: str = ROLE_EXECUTOR,
    model_label: Callable[[], str | None] | None = None,
    blocked_write_patterns: tuple[str, ...] = (),
    approval_write_patterns: tuple[str, ...] = (),
    initial_observations: list[Observation] | None = None,
    repo_files: list[str] | None = None,
    read_only: bool = False,
    explore_hook: Callable[[list[dict[str, Any]], int], tuple[list[Observation], list[dict[str, Any]]]] | None = None,
) -> AttemptOutcome:
    """Run one attempt of the iterative loop inside *sandbox*. Never touches *repo_root*.

    Returns what happened; the caller (``run_bounded_loop``) decides the
    attempt's verification, retry and the Receipt status from it. With
    ``read_only`` (the planner role) every write and verification request is
    refused as invalid and reported back; nothing in the copy changes.
    """
    max_turns = max(1, min(int(max_turns), MAX_TURNS_HARD_CAP))
    max_verifications = max(0, min(int(max_verifications), MAX_VERIFICATIONS_HARD_CAP))
    runner = NativeToolRunner(
        sandbox, blocked_write_patterns=blocked_write_patterns, approval_write_patterns=approval_write_patterns,
    )
    out = AttemptOutcome()
    observations: list[Observation] = list(initial_observations or [])
    changed: list[str] = list(changed_so_far)
    action_index = 0
    last_verification: str | None = None
    files = repo_files if repo_files is not None else sorted(
        p.relative_to(sandbox).as_posix() for p in sandbox.rglob("*") if p.is_file()
    )

    def model_name() -> str | None:
        try:
            return model_label() if model_label is not None else None
        except Exception:
            return None

    def record(action: AgentAction, turn: int, *, decision: str, **kw: Any) -> ActionRecord:
        nonlocal action_index
        rec = ActionRecord(
            index=action_index, turn=turn, kind=action.kind,
            target=_display_path(action.target) if action.kind != KIND_SEARCH_REPO else action.target[:120],
            intent=action.intent, role=role, model=model_name(), decision=decision, started_at=_now(), **kw,
        )
        action_index += 1
        out.records.append(rec)
        return rec

    def observe(rec: ActionRecord, status: str, text: str) -> None:
        observations.append(Observation(rec.turn, rec.index, rec.kind, rec.target, status, text))
        _compact_window(observations)
        _emit(progress, "action", attempt=attempt, turn=rec.turn, kind=rec.kind, target=rec.target,
              decision=rec.decision, executed=rec.executed, ok=rec.ok, status=status,
              summary=rec.result.get("summary"))

    for turn in range(1, max_turns + 1):
        out.turns = turn
        state = TurnState(
            task=task, attempt=attempt, turn=turn, max_turns=max_turns, repo_files=files,
            observations=observations, changed_files=list(changed), blocked_paths=list(blocked_seen) + out.blocked,
            previous_failure=previous_failure, verifications_left=max_verifications - out.verifications_run,
            writes_applied=len(out.applied), last_verification=last_verification,
        )
        _emit(progress, "turn_start", attempt=attempt, turn=turn, max_turns=max_turns, model=model_name(), role=role)
        started = time.monotonic()
        try:
            result = provider.turn(state)
        except BudgetExhausted as exc:
            out.stop, out.budget_stop = STOP_BUDGET, exc
            return out
        except ActionParseError as exc:
            # A model that stops speaking the contract is a model problem, not a
            # provider failure: the attempt ends and its writes face verification.
            out.model_calls += 1
            out.stop = STOP_MALFORMED_REPLY
            out.error_class = type(exc).__name__
            out.error_message = str(exc)[:180]
            _emit(progress, "malformed_reply", attempt=attempt, turn=turn, role=role, message=out.error_message)
            return out
        except Exception as exc:  # provider failure: recorded, never raised into the loop
            from openshard.safety.sanitize import sanitize_text

            out.stop = STOP_PROVIDER_ERROR
            out.error_class = type(exc).__name__
            out.error_message = sanitize_text(str(exc), 180)
            return out
        out.model_calls += 1
        if result.plan is not None:
            out.plan = result.plan
        if result.findings or result.sources:
            out.findings, out.sources = list(result.findings), list(result.sources)
        _emit(progress, "turn_response", attempt=attempt, turn=turn, actions=len(result.actions),
              note=result.note, duration_ms=int((time.monotonic() - started) * 1000), role=role)
        if result.explore and explore_hook is not None and turn < max_turns:
            # Bounded parallel exploration (``openshard.osn.explore``): the hook runs
            # read-only workers and hands back their compact answers as observations
            # for this role's next turn. The role stays the single reasoning owner.
            try:
                new_obs, records = explore_hook(result.explore, turn)
            except BudgetExhausted as exc:
                out.stop, out.budget_stop = STOP_BUDGET, exc
                return out
            for obs in new_obs:
                observations.append(obs)
            _compact_window(observations)
            out.explorations.extend(records)

        finished = False
        for action in result.actions:
            t0 = time.monotonic()
            if read_only and action.kind in (KIND_WRITE_FILE, KIND_RUN_VERIFICATION):
                rec = record(action, turn, decision=DECISION_INVALID, error_class=ERROR_CAP_REACHED)
                rec.decision_source, rec.decision_reason = "role_policy", f"the {role} role is read-only"
                rec.duration_ms = 0
                if action.kind == KIND_WRITE_FILE:
                    out.proposed.append(action.target)
                observe(rec, "refused", f"{action.kind} refused: the {role} role is read-only")
                continue
            if action.kind == KIND_FINISH:
                rec = record(action, turn, decision=DECISION_NOT_APPLICABLE, executed=True, ok=True)
                rec.duration_ms = 0
                out.final_note = action.intent or result.note
                finished = True
                _emit(progress, "action", attempt=attempt, turn=turn, kind=KIND_FINISH, target="",
                      decision=rec.decision, executed=True, ok=True, status="ok", summary=out.final_note)
                break

            if action.kind in (KIND_LIST_FILES, KIND_READ_FILE, KIND_SEARCH_REPO, KIND_GET_DIFF):
                rec = record(action, turn, decision=DECISION_ALLOW)
                _execute_read(action, rec, runner=runner, repo_root=repo_root, sandbox=sandbox, changed=changed)
                rec.duration_ms = int((time.monotonic() - t0) * 1000)
                status = "ok" if rec.ok else "refused" if rec.decision != DECISION_ALLOW else "failed"
                observe(rec, status, rec.result.pop("_text", ""))
                continue

            if action.kind == KIND_WRITE_FILE:
                out.proposed.append(action.target)
                rec = record(action, turn, decision=DECISION_INVALID)
                try:
                    resolve_safe_repo_path(sandbox, action.target)
                except UnsafePathError as exc:
                    rec.decision, rec.decision_source = DECISION_DENY, "path_safety"
                    rec.decision_reason, rec.error_class = "path escapes the repository or is unsafe", ERROR_UNSAFE_PATH
                    out.blocked.append(action.target)
                    out.decisions.append(_stored(make_deny(
                        "file_write", action.target, "path escapes the repository or is unsafe",
                        source="path_safety", severity="high",
                    )))
                    rec.duration_ms = int((time.monotonic() - t0) * 1000)
                    observe(rec, "refused", f"write refused: {exc}")
                    out.stop = STOP_POLICY_BLOCK
                    return out
                permitted = gate.authorize(action.target)
                outcome = gate.outcomes[-1]
                policy = outcome.policy
                rec.decision = policy.decision if policy is not None else DECISION_DENY
                rec.decision_source = policy.source if policy is not None else None
                rec.decision_reason = policy.reason if policy is not None else None
                rec.approval_granted = outcome.approval_granted
                out.decisions.append(_stored(policy, outcome.approval_source))
                if not permitted:
                    # Policy refused the write. As in the one-shot loop this ends the
                    # attempt: the same proposal would be refused again and a human
                    # decision is needed; nothing written so far is verified or promoted.
                    out.blocked.append(action.target)
                    rec.duration_ms = int((time.monotonic() - t0) * 1000)
                    why = rec.decision_reason or "policy refused the write"
                    if rec.decision == DECISION_ASK and rec.approval_granted is None:
                        why = "approval required but no approver is available"
                    elif rec.decision == DECISION_ASK:
                        why = "approval refused"
                    observe(rec, "refused", f"write refused ({rec.decision}): {why}")
                    out.stop = STOP_POLICY_BLOCK
                    return out
                if budget is not None:
                    try:
                        budget.authorize_write()
                    except BudgetExhausted as exc:
                        rec.duration_ms = int((time.monotonic() - t0) * 1000)
                        rec.error_class = "budget_exhausted"
                        out.stop, out.budget_stop = STOP_BUDGET, exc
                        _emit(progress, "budget_stop", attempt=attempt, reason=exc.stop_reason)
                        return out
                call = NativeToolCall("write_file", {"path": action.target, "content": action.content}, approved=True)
                res = runner.run(call)
                rec.executed, rec.ok = True, res.ok
                rec.duration_ms = int((time.monotonic() - t0) * 1000)
                if res.ok:
                    md = res.metadata
                    gate.mark_executed(action.target)
                    rel = md.get("path") or action.target
                    if rel not in out.applied:
                        out.applied.append(rel)
                    if rel not in changed:
                        changed.append(rel)
                    rec.result = {
                        "change_type": md.get("change_type"),
                        "bytes_before": md.get("bytes_before"),
                        "bytes_after": md.get("bytes_after"),
                        "sha256_before": md.get("sha256_before"),
                        "sha256_after": md.get("sha256_after"),
                        "lines_added": md.get("lines_added"),
                        "lines_removed": md.get("lines_removed"),
                        "summary": res.output,
                    }
                    observe(rec, "ok", res.output)
                else:
                    rec.error_class = ERROR_TOOL_FAILED
                    rec.result = {"summary": "write failed"}
                    if (res.metadata or {}).get("policy_decision") == "deny":
                        # The runner's own re-check refused it: a block, not a tool error.
                        rec.decision, rec.decision_reason = DECISION_DENY, res.metadata.get("policy_reason")
                        rec.decision_source = res.metadata.get("policy_source")
                        rec.error_class = ERROR_PROTECTED_PATH
                        out.blocked.append(action.target)
                        observe(rec, "refused", f"write refused: {res.error}")
                        out.stop = STOP_POLICY_BLOCK
                        return out
                    observe(rec, "failed", f"write failed: {res.error}")
                continue

            if action.kind == KIND_RUN_VERIFICATION:
                rec = record(action, turn, decision=DECISION_NOT_APPLICABLE)
                if out.verifications_run >= max_verifications:
                    rec.decision, rec.error_class = DECISION_INVALID, ERROR_CAP_REACHED
                    rec.decision_reason = "verification cap for this attempt reached"
                    rec.duration_ms = 0
                    observe(rec, "refused", "verification refused: cap for this attempt reached; "
                                            "finish and let OpenShard run the final verification")
                    continue
                _emit(progress, "verification_start", attempt=attempt, turn=turn, in_turn=True)
                try:
                    vres, output = verify(list(changed))
                except BudgetExhausted as exc:
                    rec.error_class = "budget_exhausted"
                    rec.duration_ms = int((time.monotonic() - t0) * 1000)
                    out.stop, out.budget_stop = STOP_BUDGET, exc
                    _emit(progress, "budget_stop", attempt=attempt, reason=exc.stop_reason)
                    return out
                out.verifications_run += 1
                out.verification, out.verification_output = vres, output
                out.verification_state = state_fingerprint(sandbox, changed)
                rec.executed, rec.ok = True, bool(getattr(vres, "passed", False))
                rec.duration_ms = int((time.monotonic() - t0) * 1000)
                if getattr(vres, "timed_out", False):
                    status_text = "unknown"
                elif not getattr(vres, "ran", True):
                    status_text = "not_run"
                else:
                    status_text = "passed" if vres.passed else "failed"
                rec.result = {
                    "status": status_text,
                    "exit_code": getattr(vres, "exit_code", None),
                    "output_sha256": getattr(vres, "output_sha256", None),
                    "output_bytes": getattr(vres, "output_bytes", None),
                    "tainted": bool(getattr(vres, "tainted", False)),
                    "setup_failure": getattr(vres, "setup_failure", None),
                    "failed_tests": list(getattr(vres, "failed_tests", []) or []),
                    "summary": f"verification {status_text}",
                }
                _emit(progress, "verification_result", attempt=attempt, turn=turn, in_turn=True,
                      status=status_text, exit_code=getattr(vres, "exit_code", None),
                      ran=getattr(vres, "ran", True))
                if getattr(vres, "tainted", False):
                    out.stop = STOP_VERIFIER_TAINTED
                    observe(rec, "failed", "verification invalid: the verifier modified the files it checked")
                    return out
                if getattr(vres, "timed_out", False):
                    out.stop = STOP_VERIFIER_TIMEOUT
                    observe(rec, "failed", "verification timed out before an outcome was observed")
                    return out
                if getattr(vres, "setup_failure", None):
                    out.stop = STOP_VERIFIER_SETUP
                    observe(rec, "failed", "verification could not run in this environment")
                    return out
                last_verification = status_text if vres.passed else f"failed (exit {vres.exit_code})"
                tail = output[-VERIFY_TAIL_CHARS:]
                observe(rec, "ok" if vres.passed else "failed",
                        f"verification {last_verification}\n{tail}" if tail else f"verification {last_verification}")
                continue

        _emit(progress, "turn_end", attempt=attempt, turn=turn, actions=len(result.actions), finished=finished)
        if finished:
            out.stop = STOP_FINISHED
            return out

    out.stop = STOP_MAX_TURNS
    return out


def _stored(decision: Any, approval_source: str | None = None) -> dict:
    """A policy decision as the loop stores it (masked resource, observed approval channel)."""
    from dataclasses import asdict

    if decision is None:
        return {}
    d = asdict(decision)
    if isinstance(d.get("resource"), str):
        d["resource"] = _display_path(d["resource"])
    if approval_source:
        d["approval_source"] = approval_source
    return d


def _posix_paths(text: str, kind: str) -> str:
    """Show repository paths in one style whatever the host separator (``a\\b.py`` -> ``a/b.py``)."""
    if "\\" not in text:
        return text
    out: list[str] = []
    for line in text.splitlines():
        if kind == KIND_SEARCH_REPO:
            path, sep, rest = line.partition(":")
            out.append(path.replace("\\", "/") + sep + rest if sep else line)
        else:
            out.append(line.replace("\\", "/"))
    return "\n".join(out)


def _execute_read(
    action: AgentAction,
    rec: ActionRecord,
    *,
    runner: NativeToolRunner,
    repo_root: Path,
    sandbox: Path,
    changed: list[str],
) -> None:
    """Run a read-only action against the isolated copy; fill *rec* (and ``rec.result['_text']`` for the prompt)."""
    target = action.target
    if action.kind in (KIND_READ_FILE, KIND_LIST_FILES) and target not in ("", "."):
        try:
            resolved = resolve_safe_repo_path(sandbox, target)
            rel = resolved.relative_to(sandbox.resolve()).as_posix()
        except (UnsafePathError, ValueError) as exc:
            rec.decision, rec.decision_source, rec.decision_reason = DECISION_DENY, "path_safety", str(exc)[:160]
            rec.error_class, rec.ok = ERROR_UNSAFE_PATH, False
            rec.result = {"_text": f"refused: {exc}", "summary": "unsafe path"}
            return
        if is_protected_path(rel):
            rec.decision, rec.decision_source = DECISION_DENY, "file_mutation_policy"
            rec.decision_reason = "protected path (secrets/VCS/OpenShard state)"
            rec.error_class, rec.ok = ERROR_PROTECTED_PATH, False
            rec.result = {"_text": "refused: protected path", "summary": "protected path"}
            return
    if action.kind == KIND_GET_DIFF:
        rels = [target] if target else list(changed)
        text = _sandbox_diff(repo_root, sandbox, rels, limit=DIFF_LIMIT_CHARS)
        rec.executed, rec.ok = True, True
        rec.result = {"files": len(rels), "chars": len(text), "summary": f"diff of {len(rels)} file(s)", "_text": text}
        return
    if action.kind == KIND_LIST_FILES:
        call = NativeToolCall("list_files", {"subdir": target or "."})
    elif action.kind == KIND_READ_FILE:
        call = NativeToolCall("read_file", {"path": target, "limit": READ_LIMIT_CHARS})
    else:
        call = NativeToolCall("search_repo", {"query": target, "max_matches": action.args.get("max_matches", 50)})
    res = runner.run(call)
    rec.executed, rec.ok = True, res.ok
    if not res.ok:
        rec.error_class = ERROR_TOOL_FAILED
        rec.result = {"summary": "tool failed", "_text": f"failed: {res.error}"}
        return
    text = _posix_paths(res.output, action.kind)
    if action.kind == KIND_LIST_FILES:
        count = len([ln for ln in text.splitlines() if ln.strip()])
        text = compact_tool_result(text, LIST_LIMIT_CHARS)
        rec.result = {"entries": count, "truncated": len(res.output) > LIST_LIMIT_CHARS,
                      "summary": f"{count} entries", "_text": text}
    elif action.kind == KIND_READ_FILE:
        md = res.metadata or {}
        rec.result = {"chars": md.get("chars", len(text)), "truncated": bool(md.get("truncated")),
                      "summary": f"{md.get('chars', len(text))} chars", "_text": text}
    else:
        md = res.metadata or {}
        text = compact_tool_result(text, SEARCH_LIMIT_CHARS)
        rec.result = {"matches": md.get("matches", 0), "truncated": bool(md.get("truncated")),
                      "summary": f"{md.get('matches', 0)} matches", "_text": text}


__all__ = [
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MAX_VERIFICATIONS",
    "MAX_TURNS_HARD_CAP",
    "MAX_VERIFICATIONS_HARD_CAP",
    "ROLE_EXECUTOR",
    "STOP_BUDGET",
    "STOP_FINISHED",
    "STOP_MALFORMED_REPLY",
    "STOP_MAX_TURNS",
    "STOP_POLICY_BLOCK",
    "STOP_PROVIDER_ERROR",
    "STOP_VERIFIER_SETUP",
    "STOP_VERIFIER_TAINTED",
    "STOP_VERIFIER_TIMEOUT",
    "AttemptOutcome",
    "Observation",
    "TurnProvider",
    "TurnState",
    "run_attempt_turns",
    "sandbox_diff_text",
    "state_fingerprint",
]
