"""``openshard osn run``: a bounded, policy-gated, directly verified coding task.

task -> context -> model proposes writes -> policy -> isolated copy ->
OpenShard runs the verify command -> bounded retry/escalation -> receipt.
Nothing touches the real repository unless ``--promote`` is given AND the
loop's own verification passed; promotion goes through the same policy gate
as ``apply-last``.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from openshard.osn.budget import BudgetLedger
    from openshard.osn.routing import OsnRouting
    from openshard.sync.capabilities import LazyCapabilities


@click.group("osn")
def osn_group() -> None:
    """OpenShard Native (OSN) bounded execution loop."""


def _split_command(text: str) -> list[str]:
    """Split a command line the way the platform's shell would (POSIX vs Windows paths)."""
    if os.name != "nt":
        return shlex.split(text)
    # posix=False keeps backslashes in Windows paths but leaves quotes on tokens.
    parts = shlex.split(text, posix=False)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]


def _resolve_provider(name: str | None, model: str):
    from openshard.providers.manager import ProviderManager

    providers = ProviderManager().providers
    if not providers:
        raise click.ClickException("No provider configured (set OPENROUTER_API_KEY, ANTHROPIC_API_KEY or OPENAI_API_KEY).")
    if name:
        if name not in providers:
            raise click.ClickException(f"Provider '{name}' is not configured. Configured: {', '.join(providers)}")
        return name, providers[name]
    if len(providers) == 1:
        return next(iter(providers.items()))
    raise click.ClickException(f"Several providers are configured ({', '.join(providers)}); pass --provider for model '{model}'.")


@osn_group.command("run")
@click.argument("task")
@click.option("--verify-cmd", required=True,
              help="Command OpenShard runs itself to verify the result (exit 0 = pass), e.g. \"pytest -q tests/test_x.py\". A leading python/python3 runs under the interpreter OpenShard uses.")
@click.option("--model", default=None, help="Model for the first attempt (default: existing keyword routing).")
@click.option("--escalate-model", "escalate", multiple=True,
              help="Model for later attempts, in order. Used only after a verification failure.")
@click.option("--provider", default=None, help="Provider name when several are configured.")
@click.option("--context-file", "context_files", multiple=True, help="Repo-relative file to show the model. Repeatable.")
@click.option("--max-attempts", default=2, type=click.IntRange(1, 5), show_default=True)
@click.option("--task-id", default=None, help="Explicit task id (from `openshard task new`).")
@click.option("--promote", is_flag=True, default=False,
              help="After verified success, copy changed files into the repo through the policy gate.")
@click.option("--yes", "assume_yes", is_flag=True, default=False,
              help="Approve policy 'ask' paths during --promote without prompting.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def osn_run(task, verify_cmd, model, escalate, provider, context_files, max_attempts, task_id,
            promote, assume_yes, as_json):
    """Run TASK through the bounded OSN loop."""
    from openshard.cli.ingest import _repo_root
    from openshard.history.jsonl_store import append_jsonl
    from openshard.osn.loop import run_bounded_loop
    from openshard.osn.model_provider import ModelActionProvider
    from openshard.osn.run_entry import build_osn_run_entry

    repo_root = _repo_root(None, False)
    argv = _split_command(verify_cmd)
    if not argv:
        raise click.UsageError("--verify-cmd must not be empty")
    if argv[0] in ("python", "python3"):
        # Run the verifier with the interpreter OpenShard itself runs under; a
        # bare "python" can resolve to a different environment (e.g. one
        # without the project's test dependencies) and fail for the wrong reason.
        argv[0] = sys.executable
    if promote and Path.cwd().resolve() != repo_root:
        raise click.ClickException("Run from the repository root to use --promote.")
    for rel in context_files:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise click.UsageError(f"--context-file must be repo-relative: {rel}")

    from openshard.sync.capabilities import LazyCapabilities

    capabilities = LazyCapabilities()
    budget, budget_record = _resolve_budget(repo_root, capabilities)
    attempts_allowed = max_attempts
    if budget is not None and budget.limits.max_attempts is not None:
        attempts_allowed = min(attempts_allowed, budget.limits.max_attempts)
    routing = _resolve_routing(task, repo_root, explicit_model=model, escalate=list(escalate),
                               capabilities=capabilities, max_attempts=attempts_allowed)
    provider_name, provider_obj = _resolve_provider(provider, routing.first_model)
    if routing.applied and routing.decision is not None \
            and provider_name not in tuple(routing.decision.selected_via or ()):
        # The decision knew which providers can serve the model; the one that
        # will actually dispatch is not among them, so applying it would fail.
        from openshard.osn.routing import REASON_PROVIDER_MISMATCH
        from openshard.routing.engine import route

        routing.fall_back(REASON_PROVIDER_MISMATCH, route(task).model, provider=provider_name)
    if routing.record is not None and not routing.record.get("applied"):
        click.echo(
            f"adaptive_routing: not applied ({routing.record.get('reason')}); using keyword routing.",
            err=True,
        )
    model = routing.first_model
    models = routing.models

    action_provider = ModelActionProvider(
        provider=provider_obj, models=models, repo_root=repo_root, context_files=list(context_files),
        budget=budget,
    )

    started = time.monotonic()
    receipt = run_bounded_loop(
        repo_root, task, action_provider, argv, task_id=task_id, max_attempts=max_attempts,
        budget=budget,
    )
    duration = time.monotonic() - started

    entry = build_osn_run_entry(
        receipt, task=task, usage=action_provider.usage, duration_seconds=duration,
        repo_path=repo_root, task_id=task_id,
        budget_record=budget.to_record() if budget is not None else budget_record,
        routing_decision=routing.decision, routing_record_mode=routing.record_mode,
        routing_record=routing.record, explicit_model=routing.first_model if routing.decision is None else None,
    )
    # Only present when a budget was configured / the routing capability is
    # on: the machine output is otherwise byte-for-byte what it was before.
    budget_output = {k: entry[k] for k in ("agent_budgets", "adaptive_routing") if k in entry}
    store = repo_root / ".openshard"
    store.mkdir(parents=True, exist_ok=True)
    append_jsonl(store / "runs.jsonl", entry)

    promoted: list[str] = []
    skipped: list[str] = []
    if promote:
        if receipt.status != "verified":
            if not as_json:
                click.echo(f"Not promoting: loop status is '{receipt.status}'.")
        else:
            promoted, skipped = _promote(repo_root, receipt, entry, assume_yes)

    if as_json:
        click.echo(json.dumps({
            "status": receipt.status, "stop_reason": receipt.stop_reason,
            "verification_state": receipt.verification_state,
            "receipt_id": entry.get("receipt_id"), "task_id": entry.get("task_id"),
            "provider": provider_name, "models": models, "attempts": len(receipt.attempts),
            "changed_files": receipt.changed_files, "promoted": promoted, "skipped": skipped,
            "sandbox_path": receipt.sandbox_path,
            **budget_output,
        }, indent=2))
        return
    click.echo(f"OSN loop: {receipt.status} ({receipt.stop_reason}); verification {receipt.verification_state}")
    click.echo(f"  attempts: {len(receipt.attempts)}   model(s): {', '.join(models)}   provider: {provider_name}")
    budget_line = _budget_line(entry.get("agent_budgets"))
    if budget_line:
        click.echo(f"  budget: {budget_line}")
    routing_line = _routing_line(entry.get("adaptive_routing"))
    if routing_line:
        click.echo(f"  routing: {routing_line}")
    for f in receipt.changed_files:
        click.echo(f"  changed: {f}")
    click.echo("  Changes were made in an isolated copy, verified there by OpenShard.")
    if promote and promoted:
        click.echo(f"  Promoted {len(promoted)} file(s) into the repository (not re-verified there).")
    elif receipt.status == "verified":
        click.echo(f"  Not promoted. Review the copy at {receipt.sandbox_path}, or re-run with --promote.")
    if skipped:
        click.echo(f"  Blocked by policy/skipped: {', '.join(skipped)}")


def _resolve_routing(task: str, repo_root: Path, *, explicit_model: str | None, escalate: list[str],
                     capabilities: LazyCapabilities, max_attempts: int | None = None) -> OsnRouting:
    """First model and escalation ladder: the user's choice, else adaptive routing when
    the ``adaptive_routing`` capability is on, else the keyword router as before."""
    from openshard.osn.routing import CAPABILITY as ROUTING_CAPABILITY
    from openshard.osn.routing import resolve_osn_routing
    from openshard.routing.engine import route

    def model_policy_loader():
        from openshard.config.settings import load_config_safe
        from openshard.routing.model_policy import model_policy_from_config

        config, _valid, _path = load_config_safe(cwd=repo_root)
        return model_policy_from_config(config if isinstance(config, dict) else {})

    return resolve_osn_routing(
        task,
        explicit_model=explicit_model,
        escalate=escalate,
        capability_enabled=lambda: capabilities.enabled(ROUTING_CAPABILITY),
        legacy_model=lambda t: route(t).model,
        model_policy_loader=model_policy_loader,
        max_attempts=max_attempts,
    )


def _routing_line(record: dict | None) -> str | None:
    if not isinstance(record, dict):
        return None
    if not record.get("applied"):
        return f"adaptive routing not applied ({record.get('reason') or 'unknown'})"
    ladder = ", ".join(record.get("escalation_ladder") or []) or "none"
    return (
        f"adaptive routing applied: {record.get('selected_model')} ({record.get('routing_class')}); "
        f"ladder {ladder} [{record.get('ladder_source')}]"
    )


def _resolve_budget(repo_root: Path, capabilities: LazyCapabilities) -> tuple[BudgetLedger | None, dict | None]:
    """``(ledger, not_enforced_record)`` for this run: at most one of the two is set.

    A budget comes from the repository's ``agent_budgets:`` config block and is
    enforced only when the Platform confirms the ``agent_budgets`` capability
    for the linked organisation. No block: nothing is looked up and nothing
    changes. A block the Platform did not confirm is recorded as not enforced.
    A config file that cannot be parsed is refused outright: whether it holds
    a budget is unknowable, and a budget must never vanish silently.
    """
    from openshard.config.settings import load_config_safe
    from openshard.osn.budget import CONFIG_KEY, BudgetLedger, BudgetLimits, not_enforced_record

    config, valid, path = load_config_safe(cwd=repo_root)
    if not valid:
        raise click.UsageError(f"{path} could not be parsed; refusing to run with an unreadable config.")
    try:
        limits = BudgetLimits.from_config(config.get(CONFIG_KEY) if isinstance(config, dict) else None)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None
    if not limits.configured:
        return None, None

    from openshard.sync.capabilities import CAPABILITY_AGENT_BUDGETS

    state = capabilities.state
    if state.enabled(CAPABILITY_AGENT_BUDGETS):
        return BudgetLedger(limits), None
    reason = state.reason or "capability_not_enabled"
    click.echo(
        f"agent_budgets: not enabled for this organisation ({reason}); the configured budget is not enforced.",
        err=True,
    )
    return None, not_enforced_record(limits, reason)


def _budget_line(record: dict | None) -> str | None:
    if not isinstance(record, dict):
        return None
    limits = ", ".join(f"{k}={v}" for k, v in (record.get("limits") or {}).items())
    if not record.get("enforced"):
        return f"not enforced ({record.get('reason') or 'unknown'}); configured {limits}"
    usage = record.get("usage") or {}
    spend = usage.get("spend_usd")
    spend_text = f"${spend:.4f}" if isinstance(spend, (int, float)) else "unknown"
    used = (
        f"spend {spend_text}, attempts {usage.get('attempts')}, commands {usage.get('commands')}, "
        f"writes {usage.get('writes')}"
    )
    tail = ""
    if record.get("limit_reached"):
        tail = f"; reached {record['limit_reached']}"
    if record.get("action") and record["action"] != "none":
        tail += f"; {record['action']}"
    return f"enforced ({limits}); used {used}{tail}"


def _promote(repo_root: Path, receipt, entry: dict, assume_yes: bool) -> tuple[list[str], list[str]]:
    from openshard.history.sandbox_apply_receipts import (
        SandboxApplyReceipt,
        log_sandbox_apply_receipt,
    )
    from openshard.native.sandbox_apply import apply_sandbox_changes

    # Refuse to promote bytes that differ from what OpenShard verified.
    from openshard.osn.loop import _hash_files

    if _hash_files(Path(receipt.sandbox_path), list(receipt.changed_files)) != receipt.verified_file_hashes:
        raise click.ClickException("Sandbox files changed after verification; not promoting.")

    def approver(rel, _decision):
        if assume_yes:
            return True, "flag_yes"
        try:
            granted = click.confirm(f"Policy requires approval to write {rel}. Apply?", default=False)
        except click.Abort:
            granted = False
        return granted, "interactive_prompt"

    result = apply_sandbox_changes(
        repo_root, Path(receipt.sandbox_path), include=None, approver=approver,
        explicit_files=list(receipt.changed_files),
    )
    log_sandbox_apply_receipt(SandboxApplyReceipt(
        source_run_id=entry.get("timestamp", ""), sandbox_path="",
        applied=result.applied, files_applied=list(result.files_applied),
        files_skipped=list(result.files_skipped), dry_run=False, reason=result.reason,
        policy=dict(result.policy_summary),
    ))
    return list(result.files_applied), list(result.files_skipped)
