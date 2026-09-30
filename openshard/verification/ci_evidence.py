"""Independent CI evidence for a Shard: an exact-commit verdict, attached as an attestation.

``openshard verify`` gives a Receipt an outcome OpenShard observed itself
(``directly_observed``). A CI system that ran the repository's checks on the
same commit is stronger still: it is independent of the agent *and* of the
machine OpenShard ran on (``independently_verified`` / ``ci_report``).

    commit X exists and OpenShard has evidence X is this Shard's artifact
    -> ask the forge for the check runs of exactly X
    -> every run reported for X is classified (never a run for another SHA)
    -> the verdict is appended to ``.openshard/verifications.jsonl`` as a
       ``ci_verification`` attestation naming the receipt and bound to X

Which commit
------------
A hook Receipt records HEAD at session *start*, not the commit the session
produced, so the artifact must be established by OpenShard:

* the commit of the newest bound ``openshard verify`` re-run for the receipt
  (OpenShard saw a clean tree at that commit and tested it), else
* HEAD, when the working tree is a clean commit that descends from the
  session's start commit and is not that commit itself while the session
  changed files (those changes would not be in it).

A dirty tree is never bound: what CI tested is not what is on disk.

What is recorded
----------------
Only a verdict: ``passed`` (every reported run succeeded), ``failed`` (any
run failed, even while others are still running), or ``cancelled`` (recorded
as ``unknown`` with ``ci_cancelled``). ``pending`` and ``unavailable`` are
reported to the caller and recorded nowhere: nothing was observed yet. The
attestation carries run names and conclusions, no logs, no URLs.

The Receipt is never modified. This is evidence, not policy.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openshard.history.verification import (
    CHECK_FAILED,
    CHECK_PASSED,
    CHECK_SKIPPED,
    CHECK_UNKNOWN,
    MAX_CHECKS,
    MODE_CI_REPORT,
    REASON_CI_CANCELLED,
    SOURCE_INDEPENDENTLY_VERIFIED,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_UNKNOWN,
    build_verification,
)
from openshard.verification.post_session import (
    ATTESTATION_VERSION,
    KIND_CI,
    KIND_POST_SESSION,
    TreeState,
)

PROVIDER_GITHUB = "github_checks"

OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_PENDING = "pending"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_UNAVAILABLE = "unavailable"
# Outcomes that state something CI concluded and are therefore recorded.
RECORDED_OUTCOMES: frozenset[str] = frozenset({OUTCOME_PASSED, OUTCOME_FAILED, OUTCOME_CANCELLED})

BINDING_RERUN = "openshard_rerun"  # the commit of a bound ``openshard verify`` re-run
BINDING_CLEAN_HEAD = "clean_head"  # HEAD, a clean commit descending from the session start

REFUSAL_NO_COMMIT = "no_commit"
REFUSAL_DIRTY_TREE = "dirty_tree"
REFUSAL_SESSION_CHANGES_NOT_COMMITTED = "session_changes_not_committed"
REFUSAL_NOT_DESCENDED = "commit_not_descended_from_session"

REFUSAL_TEXT: dict[str, str] = {
    REFUSAL_NO_COMMIT: "git reports no commit here; there is nothing CI could have checked.",
    REFUSAL_DIRTY_TREE: (
        "the working tree has uncommitted changes, so no commit is this Shard's artifact. "
        "Commit the work (or run `openshard verify` on a clean tree) first."
    ),
    REFUSAL_SESSION_CHANGES_NOT_COMMITTED: (
        "HEAD is still the commit the session started from, but the session changed files: "
        "those changes are not in any commit CI could have checked."
    ),
    REFUSAL_NOT_DESCENDED: (
        "HEAD does not descend from the commit the session started from, "
        "so it is not this session's work."
    ),
}

_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required", "startup_failure"})
_SKIPPED_CONCLUSIONS = frozenset({"skipped", "neutral"})
_MAX_RUNS = 200


@dataclass
class CIRun:
    name: str
    state: str  # passed | failed | pending | cancelled | skipped | unknown


@dataclass
class CIResult:
    outcome: str
    sha: str
    runs: list[CIRun] = field(default_factory=list)
    detail: str = ""

    def count(self, state: str) -> int:
        return sum(1 for r in self.runs if r.state == state)


@dataclass
class CITarget:
    sha: str | None
    binding: str | None = None
    refusal: str | None = None
    head_moved: bool = False  # bound to a re-run's commit while HEAD is now elsewhere


# ---------------------------------------------------------------------------
# Which commit is this Shard's artifact
# ---------------------------------------------------------------------------


def _sha(value: object) -> str | None:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if _SHA_RE.match(text) else None


def _changed_file_count(entry: dict) -> int:
    total = 0
    for key in ("files_created", "files_updated", "files_deleted"):
        value = entry.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


def resolve_ci_target(
    entry: dict,
    evidence: list[dict],
    tree: TreeState,
    *,
    is_ancestor: Callable[[str, str], bool | None],
) -> CITarget:
    """The one commit CI evidence may be attached to for *entry*, or why there is none.

    *evidence* is ``post_session.evidence_for_entry``; *is_ancestor(a, b)*
    answers whether commit *a* is an ancestor of (or equal to) *b*, ``None``
    when git cannot tell.
    """
    for item in reversed(evidence):
        if item.get("kind") != KIND_POST_SESSION:
            continue
        bound = _sha((item.get("verification") or {}).get("artifact_sha"))
        if bound:
            return CITarget(sha=bound, binding=BINDING_RERUN, head_moved=tree.head != bound)
    head = _sha(tree.head)
    if head is None:
        return CITarget(sha=None, refusal=REFUSAL_NO_COMMIT)
    if tree.dirty is not False:
        return CITarget(sha=None, refusal=REFUSAL_DIRTY_TREE)
    base = _sha(entry.get("git_head_commit_hash"))
    if base is not None:
        if base == head:
            if _changed_file_count(entry) > 0:
                return CITarget(sha=None, refusal=REFUSAL_SESSION_CHANGES_NOT_COMMITTED)
        elif is_ancestor(base, head) is False:
            return CITarget(sha=None, refusal=REFUSAL_NOT_DESCENDED)
    return CITarget(sha=head, binding=BINDING_CLEAN_HEAD)


def git_is_ancestor(repo_root: Path) -> Callable[[str, str], bool | None]:
    def check(ancestor: str, descendant: str) -> bool | None:
        try:
            proc = subprocess.run(
                ["git", "merge-base", "--is-ancestor", ancestor, descendant],
                cwd=repo_root, capture_output=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode == 0:
            return True
        return False if proc.returncode == 1 else None

    return check


# ---------------------------------------------------------------------------
# Classifying what the forge reported for exactly that commit
# ---------------------------------------------------------------------------


def _run_state(run: dict) -> str:
    if run.get("status") != "completed":
        return "pending"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "passed"
    if conclusion in _FAILED_CONCLUSIONS:
        return "failed"
    if conclusion == "cancelled":
        return "cancelled"
    if conclusion in _SKIPPED_CONCLUSIONS:
        return "skipped"
    return "unknown"  # ``stale`` or a conclusion this version does not know


def classify_check_runs(check_runs: object, sha: str) -> CIResult:
    """Turn a forge's check-run list into a verdict for *sha*. Pure; never raises.

    A run whose ``head_sha`` is not exactly *sha* is dropped: CI for another
    commit says nothing about this one. When a check was re-run, only its
    newest run counts.
    """
    latest: dict[str, tuple[str, dict]] = {}
    foreign = 0
    for run in (check_runs if isinstance(check_runs, list) else [])[:_MAX_RUNS]:
        if not isinstance(run, dict):
            continue
        if _sha(run.get("head_sha")) != sha:
            foreign += 1
            continue
        name = run.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        stamp = str(run.get("started_at") or run.get("completed_at") or "")
        key = name.strip()
        if key not in latest or stamp >= latest[key][0]:
            latest[key] = (stamp, run)
    runs = [CIRun(name=name, state=_run_state(run)) for name, (_, run) in latest.items()]
    result = CIResult(outcome=OUTCOME_UNAVAILABLE, sha=sha, runs=runs)
    if not runs:
        result.detail = (
            "the forge reported check runs only for other commits" if foreign
            else "no check run is reported for this commit"
        )
        return result
    if result.count("failed"):
        result.outcome = OUTCOME_FAILED
    elif result.count("pending"):
        result.outcome = OUTCOME_PENDING
        result.detail = "CI has not finished for this commit"
    elif result.count("cancelled") or result.count("unknown"):
        result.outcome = OUTCOME_CANCELLED
        result.detail = "CI was cancelled (or went stale) before giving a verdict"
    elif result.count("passed"):
        result.outcome = OUTCOME_PASSED
    else:
        result.detail = "every check run for this commit was skipped"
    return result


Runner = Callable[..., Any]


def fetch_github_check_runs(repo_root: Path, sha: str, *, runner: Runner | None = None) -> tuple[list | None, str]:
    """``(check_runs, "")`` from the GitHub CLI for *sha*, or ``(None, reason)``. Never raises.

    Read-only: one ``gh api`` GET against the repository ``gh`` resolves for
    *repo_root*. No token is read, stored or logged here.
    """
    exe = shutil.which("gh") if runner is None else "gh"
    if exe is None:
        return None, "the GitHub CLI (gh) is not installed"
    run = runner or subprocess.run
    try:
        proc = run(
            [exe, "api", "-H", "Accept: application/vnd.github+json",
             f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs?per_page=100"],
            cwd=repo_root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, "the GitHub CLI could not be run"
    if getattr(proc, "returncode", 1) != 0:
        return None, "GitHub did not return check runs for this commit (not pushed, no access, or not a GitHub repository)"
    try:
        body = json.loads(getattr(proc, "stdout", "") or "")
    except (json.JSONDecodeError, ValueError, TypeError):
        return None, "GitHub returned an unreadable response"
    runs = body.get("check_runs") if isinstance(body, dict) else None
    if not isinstance(runs, list):
        return None, "GitHub returned an unreadable response"
    return runs, ""


def fetch_github_pr_head(repo_root: Path, *, runner: Runner | None = None) -> str | None:
    """The head commit of the current branch's pull request, when ``gh`` can tell. Never raises."""
    exe = shutil.which("gh") if runner is None else "gh"
    if exe is None:
        return None
    run = runner or subprocess.run
    try:
        proc = run(
            [exe, "pr", "view", "--json", "headRefOid"],
            cwd=repo_root, capture_output=True, text=True, timeout=30, check=False,
        )
        if getattr(proc, "returncode", 1) != 0:
            return None
        body = json.loads(getattr(proc, "stdout", "") or "")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError, TypeError):
        return None
    return _sha(body.get("headRefOid")) if isinstance(body, dict) else None


# ---------------------------------------------------------------------------
# Attestation
# ---------------------------------------------------------------------------

_CHECK_STATUS = {
    "passed": CHECK_PASSED, "failed": CHECK_FAILED, "skipped": CHECK_SKIPPED,
    "cancelled": CHECK_UNKNOWN, "unknown": CHECK_UNKNOWN, "pending": CHECK_UNKNOWN,
}
_NAME_ORDER = {"failed": 0, "cancelled": 1, "unknown": 1, "pending": 1, "passed": 2, "skipped": 3}


def build_ci_attestation(
    entry: dict | None,
    result: CIResult,
    target: CITarget,
    *,
    created_at: str,
    pr_head: str | None = None,
) -> dict | None:
    """One ``verifications.jsonl`` line for a CI verdict, or None when nothing was concluded."""
    if result.outcome not in RECORDED_OUTCOMES or target.sha is None or result.sha != target.sha:
        return None
    # Failures first, so their names survive the block's check cap.
    ordered = sorted(result.runs, key=lambda r: (_NAME_ORDER.get(r.state, 1), r.name))
    passed, failed, skipped = result.count("passed"), result.count("failed"), result.count("skipped")
    status = {OUTCOME_PASSED: STATUS_PASSED, OUTCOME_FAILED: STATUS_FAILED}.get(result.outcome, STATUS_UNKNOWN)
    block = build_verification(
        source=SOURCE_INDEPENDENTLY_VERIFIED,
        observation_mode=MODE_CI_REPORT,
        checks=[{"name": r.name, "kind": "other", "status": _CHECK_STATUS[r.state]} for r in ordered[:MAX_CHECKS]],
        status=status,
        checks_attempted=len(result.runs) - skipped,
        checks_passed=passed,
        checks_failed=failed,
        checks_skipped=skipped,
        completed_at=created_at,
        artifact_sha=target.sha,
        reason=f"GitHub check runs for commit {target.sha[:12]}: {result.outcome}.",
        incomplete_reasons=[REASON_CI_CANCELLED] if result.outcome == OUTCOME_CANCELLED else [],
    )
    entry = entry or {}
    return {
        "version": ATTESTATION_VERSION,
        "attestation_id": f"vat_{uuid.uuid4().hex}",
        "kind": KIND_CI,
        "created_at": created_at,
        "receipt_id": entry.get("receipt_id") if isinstance(entry.get("receipt_id"), str) else None,
        "run_id": entry.get("run_id") if isinstance(entry.get("run_id"), str) else None,
        "shard_id": entry.get("shard_id") if isinstance(entry.get("shard_id"), str) else None,
        "executor": entry.get("executor") if isinstance(entry.get("executor"), str) else None,
        "ci": {
            "provider": PROVIDER_GITHUB,
            "commit": target.sha,
            "binding": target.binding,
            "outcome": result.outcome,
            "runs_total": len(result.runs),
            "head_moved": target.head_moved,
            # None: no pull request known. False: the PR has moved past this commit.
            "pr_head_matches": (pr_head == target.sha) if pr_head else None,
        },
        "raw_output_stored": False,
        "verification": block,
    }


__all__ = [
    "BINDING_CLEAN_HEAD",
    "BINDING_RERUN",
    "CIResult",
    "CIRun",
    "CITarget",
    "OUTCOME_CANCELLED",
    "OUTCOME_FAILED",
    "OUTCOME_PASSED",
    "OUTCOME_PENDING",
    "OUTCOME_UNAVAILABLE",
    "PROVIDER_GITHUB",
    "RECORDED_OUTCOMES",
    "REFUSAL_DIRTY_TREE",
    "REFUSAL_NOT_DESCENDED",
    "REFUSAL_NO_COMMIT",
    "REFUSAL_SESSION_CHANGES_NOT_COMMITTED",
    "REFUSAL_TEXT",
    "build_ci_attestation",
    "classify_check_runs",
    "fetch_github_check_runs",
    "fetch_github_pr_head",
    "git_is_ancestor",
    "resolve_ci_target",
]
