"""Turn guards into planned verification checks for one exact commit.

Guards are read as they were **before** the change (at *base*), so the work
being verified cannot weaken the rules it is checked against. A guard whose
file this change edited or deleted, or whose command names a file this
change edited, is still planned, but as ``needs_approval``: it does not run
unattended, and the verification is reported as incomplete until a person
runs it with ``openshard verify --commit <sha> --approve``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from openshard.guards.model import GUARDS_DIR, Guard, GuardError, load_guards_at
from openshard.guards.select import guards_for_change

MAX_GUARD_CHECKS = 12


@dataclass
class GuardPlan:
    checks: list = field(default_factory=list)  # list[post_session.PlannedCheck]
    errors: list[GuardError] = field(default_factory=list)  # invalid guard files at base
    truncated: bool = False  # more guards applied than MAX_GUARD_CHECKS
    failed: bool = False  # guards could not be planned; the verification must say it is incomplete


def _rev_exists(repo_root: Path, rev: str) -> bool:
    from openshard.util.git import run_git

    return run_git(repo_root, ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"]) is not None


def _referenced_paths(guard: Guard) -> set[str]:
    """Command tokens that look like repository paths (so a change to them changes the guard)."""
    out: set[str] = set()
    for token in guard.command or ():
        if token.startswith("-") or "/" not in token and "." not in token:
            continue
        rel = PurePosixPath(guard.cwd) / token.split("::", 1)[0] if guard.cwd else PurePosixPath(token.split("::", 1)[0])
        if not rel.is_absolute() and ".." not in rel.parts:
            out.add(rel.as_posix())
    return out


def plan_guard_checks(
    repo_root: Path, commit: str, base: str | None, changed: list[PurePosixPath] | None,
) -> GuardPlan:
    """The guard checks *commit* calls for. Never raises; no guards means an empty plan."""
    from openshard.verification.plan import (
        CommandSafety,
        VerificationSource,
        classify_command_safety,
    )
    from openshard.verification.post_session import ORIGIN_GUARD, PlannedCheck

    plan = GuardPlan()
    try:
        root = Path(repo_root)
        base_rev = base or f"{commit}^"
        if not _rev_exists(root, base_rev):
            return plan  # first commit, or base unknown: there were no approved guards before it
        before = load_guards_at(root, base_rev)
        plan.errors = list(before.errors)
        if not before.guards:
            return plan
        after = {g.source_path: g for g in load_guards_at(root, commit).guards}
        if changed is None:
            plan.failed = True  # guards exist but the change could not be listed: never report it as covered
            return plan
        changed_set = {p.as_posix() for p in changed}

        def tampered(guard: Guard) -> str:
            mine = after.get(guard.source_path)
            if guard.source_path in changed_set and mine is None:
                return f"this change removed or broke guard file {guard.source_path}"
            if guard.source_path in changed_set and mine != guard:
                return f"this change edited guard file {guard.source_path}"
            touched = sorted(_referenced_paths(guard) & changed_set)
            if touched:
                return f"this change edited {touched[0]}, which guard {guard.id} runs"
            return ""

        wanted: list[tuple[Guard, str]] = list(guards_for_change(before.guards, sorted(changed_set)))
        listed = {g.id for g, _ in wanted}
        # A guard this change edited or removed is planned even when the change is
        # outside its scope, so removing a guard is never silent.
        for guard in before.guards:
            if guard.runnable and guard.id not in listed and guard.source_path in changed_set:
                wanted.append((guard, guard.source_path))
                listed.add(guard.id)

        if len(wanted) > MAX_GUARD_CHECKS:
            plan.truncated = True
            wanted = wanted[:MAX_GUARD_CHECKS]
        for guard, hit in wanted:
            argv = list(guard.command or ())
            shared, shared_reason = classify_command_safety(argv, VerificationSource.config)
            why_tamper = tampered(guard)
            if shared == CommandSafety.blocked:
                safety, reason = CommandSafety.blocked.value, shared_reason
            elif why_tamper:
                safety, reason = CommandSafety.needs_approval.value, why_tamper
            elif shared == CommandSafety.needs_approval:
                safety, reason = shared.value, shared_reason
            else:
                safety, reason = CommandSafety.safe.value, f"guard {guard.id} (v{guard.version}) as committed before this change"
            plan.checks.append(PlannedCheck(
                name=guard.label, argv=argv, kind="test", origin=ORIGIN_GUARD, safety=safety,
                reason=reason, cwd=guard.cwd, why=f"{hit} is in the scope of guard {guard.id}: {guard.title}",
            ))
    except Exception:  # noqa: BLE001 -- planning must never break verification
        # Unknown stays unknown: the caller records the run as incomplete
        # rather than as verified without its guards.
        return GuardPlan(failed=True)
    return plan


__all__ = ["GUARDS_DIR", "MAX_GUARD_CHECKS", "GuardPlan", "plan_guard_checks"]
