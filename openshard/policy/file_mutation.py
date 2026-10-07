"""Enforcing policy gate for OpenShard-controlled file mutations (v1).

Flow per file: proposal -> policy evaluation -> allow / ask / deny ->
approval evidence (ask only) -> execution effect. Allowed != executed and
executed != verified: this module never claims verification.
"""
from __future__ import annotations

import fnmatch
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from openshard.policy.decision import (
    PolicyDecision,
    make_allow,
    make_ask,
    make_deny,
    resolve_policy_decisions,
)

ACTION_FILE_WRITE = "file_write"
SOURCE = "file_mutation_policy"
# The approval "source" recorded when the approver itself raised: nobody refused.
APPROVER_ERROR = "approver_error"

# Never written by OpenShard on the agent's behalf.
_DENY_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa", "id_ed25519",
    ".openshard/*", ".git/*",
)
# Written only with explicit approval.
_ASK_PATTERNS = (
    ".github/*", "Dockerfile", "Dockerfile.*", "docker-compose*.yml",
    "docker-compose*.yaml", "pyproject.toml", "package.json",
)

# approver(rel_path, decision) -> (granted, approval_source)
Approver = Callable[[str, PolicyDecision], tuple[bool, str]]


def _matches(rel: str, patterns: tuple[str, ...]) -> bool:
    # Case-fold: Windows/macOS filesystems are case-insensitive (.ENV == .env).
    # Strip trailing dots/spaces per component: Win32 normalises ".env " to ".env".
    parts = [c.rstrip(" .") for c in rel.replace("\\", "/").lower().split("/")]
    p = PurePosixPath("/".join(parts))
    text = str(p)
    return any(
        fnmatch.fnmatchcase(text, pat.lower()) or fnmatch.fnmatchcase(p.name, pat.lower())
        for pat in patterns
    )


def is_protected_path(rel: str) -> bool:
    """True for paths OpenShard never writes *or* reads on an agent's behalf (secrets, VCS, OpenShard state)."""
    return ":" in rel or _matches(rel, _DENY_PATTERNS)


def evaluate_file_write(
    rel: str,
    *,
    blocked_patterns: tuple[str, ...] = (),
    approval_patterns: tuple[str, ...] = (),
) -> PolicyDecision:
    """Evaluate a proposed write to *rel* (repo-relative). Pure; no I/O.

    Organisation patterns can only tighten the built-in policy. A built-in
    deny always wins, then organisation denies, then either source may require
    approval.
    """
    if ":" in rel:  # NTFS alternate data streams / drive-relative forms
        return make_deny(
            ACTION_FILE_WRITE, rel, "path contains ':' (alternate stream or drive form)",
            source=SOURCE, severity="high",
        )
    if _matches(rel, _DENY_PATTERNS):
        return make_deny(
            ACTION_FILE_WRITE, rel, "protected path (secrets/VCS/OpenShard state)",
            source=SOURCE, severity="high",
        )
    if blocked_patterns and _matches(rel, blocked_patterns):
        return make_deny(
            ACTION_FILE_WRITE, rel, "blocked by organisation write-path policy",
            source="organisation_policy", severity="high",
        )
    if _matches(rel, _ASK_PATTERNS):
        return make_ask(ACTION_FILE_WRITE, rel, "sensitive path requires approval", source=SOURCE)
    if approval_patterns and _matches(rel, approval_patterns):
        return make_ask(
            ACTION_FILE_WRITE, rel, "organisation policy requires approval for this path",
            source="organisation_policy",
        )
    return make_allow(ACTION_FILE_WRITE, rel, "ordinary path", source=SOURCE)


@dataclass
class FileMutationOutcome:
    path: str
    decision: str  # allow | ask | deny, as evaluated
    approval_granted: bool | None = None
    approval_source: str | None = None  # observed channel, e.g. "interactive_prompt"
    executed: bool = False
    reason: str | None = None  # the policy's own reason, kept for the Receipt
    severity: str | None = None
    policy: PolicyDecision | None = None  # the decision as evaluated (the one an approver saw)


@dataclass
class FileMutationGate:
    """Collects per-file outcomes and decides which proposals may execute."""

    approver: Approver | None = None
    organisation_approver: Approver | None = None
    blocked_patterns: tuple[str, ...] = ()
    approval_patterns: tuple[str, ...] = ()
    outcomes: list[FileMutationOutcome] = field(default_factory=list)

    def authorize(self, rel: str) -> bool:
        """True only if policy (plus approval when asked) permits the write."""
        decision = resolve_policy_decisions([
            evaluate_file_write(
                rel,
                blocked_patterns=self.blocked_patterns,
                approval_patterns=self.approval_patterns,
            )
        ])
        outcome = FileMutationOutcome(
            path=rel, decision=decision.decision, reason=decision.reason, severity=decision.severity,
            policy=decision,
        )
        self.outcomes.append(outcome)
        if decision.decision == "allow":
            return True
        if decision.decision == "ask":
            selected_approver = (
                self.organisation_approver
                if decision.source == "organisation_policy" and self.organisation_approver is not None
                else self.approver
            )
            if selected_approver is None:
                return False
            try:
                granted, source = selected_approver(rel, decision)
            except Exception:
                granted, source = False, APPROVER_ERROR
            outcome.approval_granted = bool(granted)
            outcome.approval_source = source
            decision.approval_granted = bool(granted)
            return bool(granted)
        return False  # deny, or ask without an approver: fail closed

    def mark_executed(self, rel: str) -> None:
        for o in self.outcomes:
            if o.path == rel:
                o.executed = True

    def decisions(self) -> list[PolicyDecision]:
        """The canonical ``PolicyDecision`` per evaluated write, in evaluation order,
        for the Receipt's ``policy_decisions``: what was allowed, what needed
        approval (and whether anyone granted it: ``approval_granted`` None means no
        approver was available), what was denied. These are the very decisions this
        gate evaluated, not re-made ones."""
        return [o.policy for o in self.outcomes if o.policy is not None]

    def summary(self) -> dict:
        return {
            "denied": [o.path for o in self.outcomes if o.decision == "deny"],
            "approval_required": [o.path for o in self.outcomes if o.decision == "ask"],
            "approval_granted": [o.path for o in self.outcomes if o.approval_granted is True],
            # Observed refusals only: an approver was consulted and said no.
            "approval_denied": [o.path for o in self.outcomes if o.approval_granted is False],
            # Ask paths where no approver existed: nothing was requested or refused.
            "approval_unavailable": [
                o.path for o in self.outcomes
                if o.decision == "ask" and o.approval_granted is None
            ],
            "executed": [o.path for o in self.outcomes if o.executed],
            "approval_sources": sorted(
                {o.approval_source for o in self.outcomes if o.approval_source}
            ),
            # Executing a write is not verification; that is a separate step.
            "verification": "not_run",
        }
