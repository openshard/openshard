"""``openshard verify --snapshot``: re-run checks on a private copy of uncommitted changes.

The work itself is ``verification/snapshot_run.verify_snapshot``; this module
only prints the plan and results and shapes ``--json``. Capture starts it
detached (``--snapshot --auto``) when a session ends with changed files and
no commit; a person can run it too.
"""

from __future__ import annotations

import json
import sys

import click

from openshard.cli.verify_commit import _emit, _sync_receipt_now


def run_verify_snapshot(entry: dict, loc, receipt_label: str, *, unattended: bool, dry_run: bool, timeout: float,
                        as_json: bool, strict: bool) -> None:
    from openshard.cli.main import (
        _machine_envelope,
        _remote_request_delivery,
        _strict_verify_exit_code,
    )
    from openshard.config.settings import load_config_safe
    from openshard.history.jsonl_store import LockTimeoutError
    from openshard.verification import snapshot_run
    from openshard.verification.post_session import (
        display_line,
        result_detail,
        summarize_attestation,
    )

    def show(planned: list) -> None:
        click.echo(f"Verifying the uncommitted changes for receipt {receipt_label} (on a private copy)")
        if not planned:
            click.echo("  No check to run for this change.")
        for check in planned:
            click.echo(f"  {check.name}  [{check.origin}, {check.safety}]" + (f"  - {check.why}" if check.why else ""))

    config, _valid, _path = load_config_safe(cwd=loc.root)
    try:
        run = snapshot_run.verify_snapshot(
            loc.root, loc.runs_path.parent, entry, config=config, timeout=timeout, dry_run=dry_run,
            stream=not as_json, on_plan=None if as_json else show,
            on_start=None if as_json else (lambda c: click.echo(f"\n[verify] running {c.name}")),
            lock_wait=snapshot_run.UNATTENDED_LOCK_WAIT_SECONDS if unattended else None,
        )
    except LockTimeoutError:
        message = "Another verification is still running; this one was not started."
        _emit(as_json, _machine_envelope("verify", "busy", message=message), message)
        return
    messages = {
        snapshot_run.STATUS_NO_SNAPSHOT: "Git could not snapshot this checkout; nothing was run.",
        snapshot_run.STATUS_NO_CHANGE: "Nothing is uncommitted; commit verification covers committed work.",
        snapshot_run.STATUS_ALREADY_VERIFIED: "These exact uncommitted changes are already verified for this receipt.",
    }
    if run.status in messages:
        _emit(as_json, _machine_envelope("verify", run.status, receipt=receipt_label), messages[run.status])
        return
    if run.status == snapshot_run.STATUS_DRY_RUN:
        if as_json:
            click.echo(json.dumps(_machine_envelope(
                "verify", "dry_run", receipt=receipt_label,
                planned=[{"name": c.name, "kind": c.kind, "origin": c.origin, "safety": c.safety,
                          "reason": c.reason, "why": c.why, "cwd": c.cwd} for c in run.planned],
            ), indent=2))
        return
    _remote_request_delivery(loc.root)
    if unattended:
        _sync_receipt_now(loc.root, entry)
    attestation = run.attestation or {}
    summary = summarize_attestation(attestation)
    strict_exit = _strict_verify_exit_code(run.results) if strict else 0
    if as_json:
        click.echo(json.dumps(_machine_envelope(
            "verify", "ok", receipt=receipt_label, snapshot=attestation.get("snapshot"),
            attestation_id=attestation.get("attestation_id"), checks=attestation.get("checks"),
            checkout=attestation.get("checkout"), verification=summary["verification"],
            strict=strict, exit_code=strict_exit,
        ), indent=2))
    else:
        click.echo("")
        for result in run.results:
            detail = result_detail(result)
            click.echo(f"  {result.check.name}: {result.status}" + (f" ({detail})" if detail else ""))
        click.echo(f"\nRe-verified: {display_line(summary)}")
        click.echo("Recorded in .openshard/verifications.jsonl (evidence: directly_observed).")
    if strict_exit:
        sys.exit(strict_exit)


__all__ = ["run_verify_snapshot"]
