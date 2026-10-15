"""`openshard verify --ci`: attach an independent CI verdict for a Shard's exact commit."""

from __future__ import annotations

import json
import sys

import click


def run_verify_from_ci(entry: dict, loc, receipt_label, *, as_json: bool, strict: bool, dry_run: bool) -> None:
    """``openshard verify --ci``: attach the CI verdict for this Shard's exact commit, if there is one."""
    from openshard.cli.main import _machine_envelope, _remote_request_delivery, _utc_stamp
    from openshard.verification import ci_evidence as ci
    from openshard.verification.post_session import (
        evidence_for_entry,
        load_attestations,
        record_attestation,
        tree_state,
    )

    repo_root = loc.root
    tree = tree_state(repo_root)
    evidence = evidence_for_entry(entry, load_attestations(loc.runs_path.parent))
    target = ci.resolve_ci_target(entry, evidence, tree, is_ancestor=ci.git_is_ancestor(repo_root))

    def finish(status: str, message: str, exit_code: int, **fields) -> None:
        if as_json:
            click.echo(json.dumps(_machine_envelope(
                "verify", status, receipt=receipt_label, ci=True, message=message,
                strict=strict, exit_code=exit_code if strict else 0, **fields,
            ), indent=2))
        else:
            click.echo(message)
        if strict and exit_code:
            sys.exit(exit_code)

    if target.sha is None:
        finish(
            "not_bound",
            f"No CI evidence attached to receipt {receipt_label}: {ci.REFUSAL_TEXT[str(target.refusal)]}",
            2, refusal=target.refusal,
        )
        return
    note = ""
    if target.head_moved:
        note = (
            f" HEAD has moved on since OpenShard verified commit {target.sha[:12]}; CI is looked up for that "
            "commit only. Run `openshard verify` on a clean tree to bind the new commit."
        )
    if dry_run:
        finish("dry_run", f"Would look up GitHub check runs for commit {target.sha[:12]} ({target.binding}).{note}",
               0, commit=target.sha, binding=target.binding)
        return

    runs, error = ci.fetch_github_check_runs(repo_root, target.sha)
    if runs is None:
        finish("ci_unavailable", f"CI unavailable for commit {target.sha[:12]}: {error}. Nothing recorded.{note}",
               2, commit=target.sha, outcome=ci.OUTCOME_UNAVAILABLE)
        return
    result = ci.classify_check_runs(runs, target.sha)
    counts = {state: result.count(state) for state in ("passed", "failed", "pending", "cancelled", "skipped")}
    if result.outcome not in ci.RECORDED_OUTCOMES:
        finish(
            f"ci_{result.outcome}",
            f"CI {result.outcome} for commit {target.sha[:12]}: {result.detail}. Nothing recorded.{note}",
            2, commit=target.sha, outcome=result.outcome, runs=counts,
        )
        return
    pr_head = ci.fetch_github_pr_head(repo_root)
    attestation = ci.build_ci_attestation(entry, result, target, created_at=_utc_stamp(), pr_head=pr_head)
    if attestation is None:
        finish("ci_unavailable", "CI verdict could not be recorded.", 2, commit=target.sha)
        return
    record_attestation(repo_root, attestation)
    _remote_request_delivery(repo_root)
    if pr_head and pr_head != target.sha:
        note += (
            f" The pull request head is now {pr_head[:12]}; this verdict is for {target.sha[:12]} "
            "and says nothing about the newer commit."
        )
    lines = [
        f"CI {result.outcome} for commit {target.sha[:12]}: "
        f"{counts['passed']} passed, {counts['failed']} failed"
        + (f", {counts['cancelled']} cancelled" if counts["cancelled"] else "")
        + (f", {counts['skipped']} skipped" if counts["skipped"] else "") + "."
    ]
    failed_names = [r.name for r in result.runs if r.state == "failed"][:5]
    if failed_names:
        lines.append("Failed: " + "; ".join(failed_names))
    lines.append(
        "Recorded in .openshard/verifications.jsonl (evidence: independently_verified). "
        "The Receipt and earlier evidence are unchanged." + note
    )
    exit_code = {ci.OUTCOME_PASSED: 0, ci.OUTCOME_FAILED: 1}.get(result.outcome, 2)
    finish("ok", "\n".join(lines), exit_code, commit=target.sha, binding=target.binding,
           outcome=result.outcome, runs=counts, attestation_id=attestation["attestation_id"],
           verification=attestation["verification"])


__all__ = ["run_verify_from_ci"]
