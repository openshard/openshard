"""``openshard verify --commit``: rendering for one exact-commit verification.

The work itself is ``verification/commit_run.verify_commit``; this module only
resolves the commit, prints the plan and results, and shapes ``--json``.
"""

from __future__ import annotations

import json
import sys

import click


def _emit(as_json: bool, envelope: dict, text: str) -> None:
    click.echo(json.dumps(envelope, indent=2) if as_json else text)


def _resolve_commit(repo_root, commit_ref: str) -> str | None:
    from openshard.util.git import run_git
    from openshard.verification.change_checks import is_sha

    resolved = (run_git(repo_root, ["rev-parse", "--verify", "--quiet", f"{commit_ref}^{{commit}}"]) or "").strip().lower()
    return resolved if is_sha(resolved) else None


def _print_plan(sha: str, receipt_label: str):
    def show(planned: list) -> None:
        click.echo(f"Verifying commit {sha[:12]} for receipt {receipt_label}")
        if not planned:
            click.echo("  No check to run for this change.")
        for check in planned:
            click.echo(f"  {check.name}  [{check.origin}, {check.safety}]" + (f"  - {check.why}" if check.why else ""))
    return show


def _sync_receipt_now(root, entry: dict) -> None:
    """Send this Receipt and its new commit evidence to the Platform now, if linked. Never raises.

    The automatic per-commit verification runs detached, after the agent
    committed and usually before it pushes; sending here lets a pull
    request's merge gate find the verdict without waiting for the capture
    service's next periodic sync. ``flush`` honours the link and every
    sync opt-out, and a failure here changes nothing: the gate waits while
    the Platform reports the commit as pending.
    """
    try:
        from openshard.sync.client import flush

        rid = entry.get("receipt_id")
        if isinstance(rid, str) and rid:
            flush(root, receipt_ids=frozenset({rid}))
    except Exception:
        pass


def run_verify_commit(entry: dict, loc, receipt_label: str, commit_ref: str, *, approve: bool, unattended: bool,
                      dry_run: bool, timeout: float, as_json: bool, strict: bool) -> None:
    from openshard.cli.main import (
        _echo_timeout_hint,
        _machine_envelope,
        _remote_request_delivery,
        _strict_verify_exit_code,
    )
    from openshard.config.settings import load_config_safe
    from openshard.history.jsonl_store import LockTimeoutError
    from openshard.history.receipt_evidence import base_commit_value
    from openshard.verification import commit_run
    from openshard.verification.post_session import (
        NO_CHECKS_DOCS_ONLY,
        display_heading,
        display_line,
        result_detail,
        summarize_attestation,
    )

    sha = _resolve_commit(loc.root, commit_ref)
    if sha is None:
        message = f"{commit_ref!r} is not a commit in this repository."
        _emit(as_json, _machine_envelope("verify", "not_found", message=message), message)
        sys.exit(1)
    config, _valid, _path = load_config_safe(cwd=loc.root)
    try:
        run = commit_run.verify_commit(
            loc.root, loc.runs_path.parent, entry, sha, config=config, base=base_commit_value(entry),
            approve=approve, unattended=unattended, timeout=timeout, dry_run=dry_run, stream=not as_json,
            on_plan=None if as_json else _print_plan(sha, receipt_label),
            on_start=None if as_json else (lambda c: click.echo(f"\n[verify] running {c.name}")),
        )
    except LockTimeoutError:
        message = "Another commit verification is still running; this one was not started."
        _emit(as_json, _machine_envelope("verify", "busy", message=message), message)
        return
    if run.status == commit_run.STATUS_NOT_SESSION_COMMIT:
        _emit(as_json, _machine_envelope("verify", run.status, receipt=receipt_label, commit=sha),
              f"Commit {sha[:12]} was not created by this receipt's session; nothing was run.")
        return
    if run.status == commit_run.STATUS_ALREADY_VERIFIED:
        _emit(as_json, _machine_envelope("verify", run.status, receipt=receipt_label, commit=sha),
              f"Commit {sha[:12]} is already verified for this receipt.")
        return
    if run.status == commit_run.STATUS_DRY_RUN:
        if as_json:
            click.echo(json.dumps(_machine_envelope(
                "verify", "dry_run", receipt=receipt_label, commit=sha,
                no_checks_applicable=run.no_checks_applicable,
                planned=[{"name": c.name, "kind": c.kind, "origin": c.origin, "safety": c.safety,
                          "reason": c.reason, "why": c.why, "cwd": c.cwd} for c in run.planned],
            ), indent=2))
        elif run.no_checks_applicable:
            click.echo(f"  {NO_CHECKS_DOCS_ONLY}. Nothing would run; the commit would not be verified.")
        return
    _remote_request_delivery(loc.root)
    if unattended:
        _sync_receipt_now(loc.root, entry)
    attestation = run.attestation or {}
    summary = summarize_attestation(attestation)
    strict_exit = _strict_verify_exit_code(run.results) if strict else 0
    if as_json:
        click.echo(json.dumps(_machine_envelope(
            "verify", "ok", receipt=receipt_label, commit=sha, attestation_id=attestation.get("attestation_id"),
            checks=attestation.get("checks"), checkout=attestation.get("checkout"),
            verification=summary["verification"], no_checks_applicable=run.no_checks_applicable,
            strict=strict, exit_code=strict_exit,
        ), indent=2))
    elif run.no_checks_applicable:
        click.echo(f"\n{NO_CHECKS_DOCS_ONLY}. Nothing was run, so this commit is not verified.")
        click.echo("Recorded in .openshard/verifications.jsonl (no checks run).")
    else:
        click.echo("")
        for result in run.results:
            detail = result_detail(result)
            click.echo(f"  {result.check.name}: {result.status}" + (f" ({detail})" if detail else ""))
        click.echo(f"\n{display_heading(summary)}: {display_line(summary)}")
        _echo_timeout_hint(run.results, timeout)
        click.echo("Recorded in .openshard/verifications.jsonl (evidence: directly_observed).")
    if strict_exit:
        sys.exit(strict_exit)


__all__ = ["run_verify_commit"]
