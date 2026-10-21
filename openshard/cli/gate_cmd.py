"""``openshard ci gate``: the merge gate for one commit (docs/merge-gate.md).

Reads the same evidence ``openshard verdict --commit`` reads, reduces it with
``ci/merge_gate.evaluate_gate`` and, in GitHub Actions, writes the job
summary and outputs. Nothing runs and nothing is written to the history.
Exit 1 only when the gate fails; a warning (unknown) exits 0.
"""

from __future__ import annotations

import json
import sys

import click

# Said on every summary: the evidence is whatever this job's checkout holds,
# so it is only as trustworthy as whoever wrote those files.
EVIDENCE_SOURCE = (
    "the Openshard history (.openshard/) available to this job. It is not independently witnessed "
    "unless the Platform-witnessed row says yes."
)


UNTRUSTED_HISTORY = "untrusted_history"
_EVIDENCE_FILES = (".openshard/runs.jsonl", ".openshard/verifications.jsonl")


def _tracked_history() -> bool:
    """True when the history files the verdict reads are committed to git (or git cannot tell)."""
    from openshard.history.locate import locate_history
    from openshard.util.git import run_git

    try:
        loc = locate_history(with_identity=False)
    except Exception:
        return False
    out = run_git(loc.root, ["ls-files", "--", *_EVIDENCE_FILES])
    if out is None:
        return (loc.root / ".git").exists()
    return bool(out.strip())


def _receipt_summary(loc, receipt_id: str | None):
    """The ``GateReceipt`` for *receipt_id* from this repository's history, or None. Never raises."""
    from openshard.ci.merge_gate import WITNESS_UNKNOWN, WITNESS_YES, GateReceipt

    if not receipt_id:
        return None
    try:
        from openshard.history.merge_check import merge_check_block
        from openshard.history.shard_contract import build_shard_receipt
        from openshard.history.store import load_history
        from openshard.remote.witness import WITNESS_PLATFORM, witness_for_entry

        entries = load_history(loc.runs_path, coerce=False) if loc.runs_path.exists() else []
        found = [(i, e) for i, e in enumerate(entries) if e.get("receipt_id") == receipt_id]
        if not found:
            return GateReceipt(receipt_id=receipt_id)
        index, entry = found[-1]
        receipt = build_shard_receipt(entry, index=index)
        block = witness_for_entry(entry)
        if block.get("capture_witness") == WITNESS_PLATFORM:
            witness = WITNESS_YES
            detail = f"{block['events_witnessed']} of {block['events_in_receipt']} events acknowledged"
        else:
            # The spool on this machine holds no acknowledgement. On a CI runner
            # that says nothing about the Platform, so it is unknown, never "no".
            witness, detail = WITNESS_UNKNOWN, "no acknowledgement recorded on this machine"
        return GateReceipt(
            receipt_id=receipt_id,
            agent=receipt.agent or None,
            model=None if receipt.model_display == "Not recorded" else receipt.model_display,
            cost=None if receipt.cost_raw is None else receipt.cost_display,
            witness=witness,
            witness_detail=detail,
            merge_check=_merge_check_line(merge_check_block(entry)),
        )
    except Exception:
        return GateReceipt(receipt_id=receipt_id)


def _merge_check_line(block: dict | None) -> str | None:
    """One informational line from the Receipt's ``merge_check``; never a gate criterion."""
    if not block:
        return None
    base = block["base_ref"]
    conflicts = block.get("conflicts")
    if conflicts is None:
        text = f"conflicts with {base}: unknown"
    elif conflicts:
        text = f"{len(conflicts)} conflicting path(s) with {base}"
    else:
        text = f"no conflicts with {base}"
    behind = block.get("behind_by")
    if isinstance(behind, int):
        text += f", {behind} commit(s) behind"
    return f"{text} (checked {block['checked_at']})"


def _checks(doc: dict, loc, sha: str) -> tuple[str | None, tuple[str, ...]]:
    """Check counts and failed names for the evidence the verdict used. Never raises."""
    try:
        from openshard.history.verification_truth import counts_phrase, interpret_evidence
        from openshard.verification.post_session import (
            commit_tree,
            load_attestations,
            summarize_attestation,
        )
        from openshard.verification.verdict import commit_evidence

        matched, _ = commit_evidence(sha, load_attestations(loc.runs_path.parent),
                                     tree_oid=commit_tree(loc.root, sha))
        if not matched:
            return None, ()
        truth = interpret_evidence(None, post_session_verification={
            "evidence": [summarize_attestation(item) for item in matched]})
        phrase = counts_phrase(truth.checks_passed, truth.checks_failed, truth.checks_attempted)
        return phrase or None, tuple(truth.failed_checks)
    except Exception:
        return None, ()


@click.command("gate")
@click.option("--commit", "commit_ref", required=True, metavar="SHA",
              help="The commit to gate: for a pull request, its head commit.")
@click.option("--strict", is_flag=True, default=False,
              help="Fail on unknown too (default: unknown is a warning and exits 0).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
@click.option("--github-step-summary", "step_summary", is_flag=True, default=False,
              help="Append the gate summary to $GITHUB_STEP_SUMMARY.")
@click.option("--github-output", "github_output", is_flag=True, default=False,
              help="Write openshard_gate_status and openshard_gate_verdict to $GITHUB_OUTPUT.")
def ci_gate(commit_ref: str, strict: bool, as_json: bool, step_summary: bool, github_output: bool) -> None:
    """Gate a merge on one commit's recorded verification.

    Fail when verification failed for the commit, or when the gate could not
    answer. Warn (exit 0) when it is unknown, for example a human-written
    change with no Receipt; --strict fails on that too. Pass when an
    Openshard re-run or CI observed the checks pass. Nothing runs.
    """
    from openshard.ci.merge_gate import evaluate_gate
    from openshard.cli.main import (
        _machine_envelope,
        _write_github_output,
        _write_github_step_summary,
    )
    from openshard.cli.verdict_cmd import _answer
    from openshard.verification import verdict

    try:
        doc = _answer(commit_ref, None)
    except Exception as exc:  # an unreadable store fails the gate, never passes it
        doc = verdict.error("commit", commit_ref, type(exc).__name__)
    if doc.get("verdict") != "error" and _tracked_history():
        # History files committed to the repository arrive with the change
        # under review, so its author wrote them: never evidence for it.
        doc = {**doc, "verdict": "unknown", "exit_code": 2, "reason": UNTRUSTED_HISTORY, "evidence": []}

    receipt = checks = None
    failed: tuple[str, ...] = ()
    if doc.get("verdict") != "error":
        from openshard.history.locate import locate_history

        loc = locate_history(with_identity=False)
        sha = doc["subject"]["id"]
        rows = [r for r in doc.get("evidence") or [] if isinstance(r, dict)]
        receipt = _receipt_summary(loc, rows[-1].get("receipt_id") if rows else None)
        checks, failed = _checks(doc, loc, sha)

    result = evaluate_gate(doc, strict=strict, receipt=receipt, checks=checks, failed_checks=failed,
                           evidence_source=EVIDENCE_SOURCE)

    if step_summary and not _write_github_step_summary(result.summary_markdown)[0]:
        click.echo("warning: GITHUB_STEP_SUMMARY is not set; step summary not written.", err=True)
    if github_output and not _write_github_output(
        {"openshard_gate_status": result.status, "openshard_gate_verdict": result.verdict}
    )[0]:
        click.echo("warning: GITHUB_OUTPUT is not set; outputs not written.", err=True)

    if as_json:
        click.echo(json.dumps(_machine_envelope(
            "ci gate", result.status, exit_code=result.exit_code, strict=strict, verdict=doc,
            headline=result.headline, reasons=result.reasons,
        ), indent=2))
    else:
        click.echo(f"Openshard gate: {result.status}")
        click.echo(result.headline)
    sys.exit(result.exit_code)


__all__ = ["ci_gate"]
