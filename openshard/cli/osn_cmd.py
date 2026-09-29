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


def _friendly_model(model: str | None) -> str:
    if not model:
        return "unknown model"
    from openshard.history.shard_contract import display_model_name

    return display_model_name(model)


class _OsnProgressRenderer:
    """Live, observable OSN progress. Shows actions and outcomes, never model reasoning."""

    def __init__(self) -> None:
        from openshard.cli.run_output import _Spinner

        self._spinner = _Spinner() if getattr(sys.stdout, "isatty", lambda: False)() else None
        self._spinning = False

    def _stop(self) -> None:
        if self._spinner is not None and self._spinning:
            self._spinner.stop()
        self._spinning = False

    def _start(self, label: str) -> None:
        self._stop()
        if self._spinner is not None:
            self._spinner.start(label)
            self._spinning = True
        else:
            click.echo(f"  {label}...")

    def close(self) -> None:
        self._stop()

    def __call__(self, event: str, data: dict) -> None:
        from openshard.cli.run_output import _safe_console_text

        def echo(text: str) -> None:
            click.echo(_safe_console_text(text))

        if event == "workspace_ready":
            echo("  ✓ Isolated workspace ready")
        elif event == "attempt_start":
            self._stop()
            model = _friendly_model(data.get("model"))
            echo(f"\nAttempt {data.get('attempt')} · {model}")
            self._start(f"Calling {model}")
        elif event == "model_response":
            self._stop()
            n = data.get("proposed") or 0
            echo(f"  ✓ Response received · {n} proposed write{'s' if n != 1 else ''}")
        elif event == "provider_error":
            self._stop()
            cls = data.get("error_class") or "provider error"
            msg = data.get("message")
            echo(f"  ✗ Provider failed · {cls}" + (f": {msg}" if msg else ""))
        elif event == "no_actions":
            self._stop()
            echo("  ✗ Model returned no usable changes")
        elif event == "policy_result":
            applied = data.get("applied") or 0
            blocked = data.get("blocked") or 0
            if blocked:
                echo(f"  ✗ Policy · {blocked} blocked, {applied} applied")
            else:
                echo(f"  ✓ Policy allowed · {applied} write{'s' if applied != 1 else ''}")
        elif event == "verification_start":
            echo("\nVerification")
            self._start("Running verification")
        elif event == "verification_result":
            self._stop()
            status = data.get("status")
            if status == "passed":
                echo("  ✓ PASSED")
            elif status == "failed":
                code = data.get("exit_code")
                echo("  ✗ FAILED" + (f" · exit {code}" if code is not None else ""))
            else:
                echo("  ? UNKNOWN · verification did not produce a verdict")
        elif event == "recovery_decision":
            self._stop()
            echo("\nRecovery")
            action = data.get("action")
            recovery_model = data.get("model")
            if action == "escalate" and isinstance(recovery_model, str) and recovery_model:
                echo("  Verification failure observed")
                echo(f"  → {_friendly_model(recovery_model)}")
            else:
                echo(f"  {action or 'stop'} · {data.get('reason') or 'no reason recorded'}")
        elif event == "budget_stop":
            self._stop()
            echo(f"  ✗ Budget stopped the run · {data.get('reason') or 'limit reached'}")


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
    if not as_json:
        click.echo("\nOpenshard Native (OSN)")
        click.echo(f"  Task    {task}")
        try:
            from openshard.analysis.repo_map import collect_git_info

            git = collect_git_info(repo_root)
            click.echo(f"  Repo    {repo_root.name}")
            if git.branch:
                click.echo(f"  Branch  {git.branch}")
        except Exception:
            click.echo(f"  Repo    {repo_root.name}")
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

    from openshard.config.settings import load_config_safe
    from openshard.sync.capabilities import LazyCapabilities
    from openshard.sync.policies import (
        PolicyUnavailable,
        combine_budget_limits,
        combine_model_policy,
        effective_policy_hash,
        enforce_models_allowed,
        repository_override_present,
        resolve_organisation_policy,
    )

    repo_config, config_valid, config_path = load_config_safe(cwd=repo_root)
    if not config_valid:
        raise click.UsageError(f"{config_path} could not be parsed; refusing to run with an unreadable config.")
    try:
        organisation_policy = resolve_organisation_policy()
        model_policy = combine_model_policy(repo_config, organisation_policy)
        effective_limits, local_limits, _org_limits = combine_budget_limits(repo_config, organisation_policy)
    except PolicyUnavailable as exc:
        raise click.ClickException(
            f"Organisation policy could not be refreshed ({exc}); refusing to start a linked OSN run."
        ) from None
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None

    # One fresh capability read at run start (when a feature asks), frozen for
    # the whole run: a dashboard toggle applies to the next new run, and never
    # to a run already in progress.
    capabilities = LazyCapabilities(refresh=True)
    explicit_model = model  # the user's --model, if any; never re-evaluated by routing or a supervisor
    budget, budget_record = _resolve_budget(
        capabilities,
        effective_limits=effective_limits,
        local_limits=local_limits,
        organisation_policy=organisation_policy,
    )
    attempts_allowed = max_attempts
    if budget is not None and budget.limits.max_attempts is not None:
        attempts_allowed = min(attempts_allowed, budget.limits.max_attempts)
    routing = _resolve_routing(task, repo_root, explicit_model=model, escalate=list(escalate),
                               capabilities=capabilities, model_policy=model_policy,
                               max_attempts=attempts_allowed,
                               cost_budget_usd=budget.limits.max_spend_usd if budget is not None else None)
    try:
        enforce_models_allowed(routing.models, model_policy)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    provider_name, provider_obj = _resolve_provider(provider, routing.first_model)
    if routing.applied and routing.decision is not None \
            and provider_name not in tuple(routing.decision.selected_via or ()):
        # The decision knew which providers can serve the model; the one that
        # will actually dispatch is not among them, so applying it would fail.
        from openshard.osn.routing import REASON_PROVIDER_MISMATCH
        from openshard.routing.engine import route

        routing.fall_back(REASON_PROVIDER_MISMATCH, route(task).model, provider=provider_name)
        try:
            enforce_models_allowed(routing.models, model_policy)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
    if routing.record is not None and not routing.record.get("applied"):
        click.echo(
            f"adaptive_routing: not applied ({routing.record.get('reason')}); using keyword routing.",
            err=True,
        )
    model = routing.first_model
    models = routing.models
    if not as_json:
        click.echo(f"  Route   {' → '.join(_friendly_model(m) for m in models)}")
        if routing.applied and routing.decision is not None:
            click.echo(f"  Policy  Adaptive Routing V2 · {routing.decision.resolved_class}")

    action_provider = ModelActionProvider(
        provider=provider_obj, models=models, repo_root=repo_root, context_files=list(context_files),
        budget=budget,
    )
    supervisor = _resolve_supervisor(routing, budget, action_provider, capabilities, user_ladder=list(escalate),
                                     explicit_model=explicit_model)

    started = time.monotonic()
    progress_renderer = _OsnProgressRenderer() if not as_json else None
    try:
        receipt = run_bounded_loop(
            repo_root, task, action_provider, argv, task_id=task_id, max_attempts=max_attempts,
            budget=budget, supervisor=supervisor, progress=progress_renderer,
        )
    finally:
        if progress_renderer is not None:
            progress_renderer.close()
    duration = time.monotonic() - started

    entry = build_osn_run_entry(
        receipt, task=task, usage=action_provider.usage, duration_seconds=duration,
        repo_path=repo_root, task_id=task_id,
        budget_record=budget.to_record() if budget is not None else budget_record,
        routing_decision=routing.decision, routing_record_mode=routing.record_mode,
        routing_record=routing.record, explicit_model=routing.first_model if routing.decision is None else None,
        supervisor_record=supervisor.to_record() if supervisor is not None else None,
        capability_snapshot=capabilities.to_record(),
        organisation_policy=(
            organisation_policy.receipt_record(
                effective_policy_hash=effective_policy_hash(model_policy, effective_limits),
                repository_override_applied=repository_override_present(
                    repo_config, has_config_file=config_path is not None,
                ),
            )
            if organisation_policy is not None else None
        ),
    )
    # Only present when a budget was configured / a capability was looked up:
    # the machine output is otherwise byte-for-byte what it was before.
    budget_output = {
        k: entry[k]
        for k in ("agent_budgets", "adaptive_routing", "supervisor_routing", "capability_snapshot", "organisation_policy")
        if k in entry
    }
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
    supervisor_line = _supervisor_line(entry.get("supervisor_routing"))
    if supervisor_line:
        click.echo(f"  supervisor: {supervisor_line}")
    for f in receipt.changed_files:
        click.echo(f"  changed: {f}")
    if receipt.status == "verified":
        click.echo("  Verified in an isolated workspace.")
        click.echo("  Real repository unchanged until promotion.")
    elif not receipt.changed_files:
        click.echo("  No files changed.")
        if receipt.verification_state == "not_run":
            click.echo("  Verification did not run.")
    else:
        click.echo("  Changes remain isolated; real repository unchanged.")
        if receipt.verification_state == "not_run":
            click.echo("  Verification did not run.")
    if promote and promoted:
        click.echo(f"  Promoted {len(promoted)} file(s) into the repository (not re-verified there).")
    elif receipt.status == "verified":
        click.echo(f"  Not promoted. Review the copy at {receipt.sandbox_path}, or re-run with --promote.")
    if skipped:
        click.echo(f"  Blocked by policy/skipped: {', '.join(skipped)}")


def _resolve_routing(task: str, repo_root: Path, *, explicit_model: str | None, escalate: list[str],
                     capabilities: LazyCapabilities, model_policy, max_attempts: int | None = None,
                     cost_budget_usd: float | None = None) -> OsnRouting:
    """First model and escalation ladder: the user's choice, else Routing V2 when
    the ``adaptive_routing`` capability is on, else the keyword router as before."""
    from openshard.osn.routing import CAPABILITY as ROUTING_CAPABILITY
    from openshard.osn.routing import HARNESS, resolve_osn_routing
    from openshard.routing.engine import route

    def model_policy_loader():
        return model_policy

    def history_loader():
        from openshard.routing.adaptive.history_evidence import load_history_evidence

        return load_history_evidence(repo_root / ".openshard" / "runs.jsonl", harness=HARNESS)

    return resolve_osn_routing(
        task,
        explicit_model=explicit_model,
        escalate=escalate,
        capability_enabled=lambda: capabilities.enabled(ROUTING_CAPABILITY),
        legacy_model=lambda t: route(t).model,
        model_policy_loader=model_policy_loader,
        max_attempts=max_attempts,
        cost_budget_usd=cost_budget_usd,
        history_loader=history_loader,
    )


def _resolve_supervisor(routing: OsnRouting, budget: BudgetLedger | None, action_provider, capabilities: LazyCapabilities,
                        *, user_ladder: list[str], explicit_model: str | None):
    """A recovery supervisor for this run, or None.

    Never for an explicit ``--model`` (the user's choice is not re-evaluated and
    the capability is not looked up) and never without the ``supervisor_routing``
    capability. Applied only when adaptive routing applied the decision whose
    recovery plan the supervisor follows and the user typed no ladder; otherwise
    the supervisor runs in shadow and the record says why.
    """
    from openshard.osn.supervisor import (
        CAPABILITY,
        NOT_ACTED_ROUTING_NOT_APPLIED,
        NOT_ACTED_USER_LADDER,
        RECORD_APPLIED,
        RECORD_SHADOW,
        RecoverySupervisor,
    )

    decision = routing.decision
    if explicit_model or decision is None:
        # The user named the model (whatever the catalog thinks of it): nothing
        # to supervise and no lookup. Without a decision there is no plan to follow.
        return None
    if not capabilities.enabled(CAPABILITY):
        return None
    if user_ladder:
        not_acted: str | None = NOT_ACTED_USER_LADDER
    elif not routing.applied:
        not_acted = NOT_ACTED_ROUTING_NOT_APPLIED
    else:
        not_acted = None
    return RecoverySupervisor(
        plan=decision.recovery,
        usage_for=action_provider.usage_for,
        record_mode=RECORD_APPLIED if not_acted is None else RECORD_SHADOW,
        not_acted_reason=not_acted,
        cost_budget_usd=budget.limits.max_spend_usd if budget is not None else None,
        first_model=routing.first_model,
        first_class=decision.resolved_class,
        ladder_model_for=action_provider.model_for,
        # Routing V2 re-decides the repair step only when it chose the first one.
        reroute=routing.reroute if not_acted is None else None,
    )


def _supervisor_line(record: dict | None) -> str | None:
    if not isinstance(record, dict):
        return None
    decisions = record.get("decisions") or []
    mode = record.get("record_mode")
    head = f"{mode}" + (f" ({record.get('not_applied_reason')})" if record.get("not_applied_reason") else "")
    if not decisions:
        return f"{head}; never consulted"
    parts = [
        f"after attempt {d.get('attempt')}: {d.get('action')} ({d.get('reason')})"
        + (f" -> {d['recommended_model']}" if d.get("recommended_model") else "")
        + ("" if d.get("acted_on") else " [not acted on]")
        for d in decisions
    ]
    return f"{head}; " + "; ".join(parts)


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


def _resolve_budget(
    capabilities: LazyCapabilities,
    *,
    effective_limits: BudgetLimits,
    local_limits: BudgetLimits,
    organisation_policy,
) -> tuple[BudgetLedger | None, dict | None]:
    """Resolve the hard budget for this run.

    A saved organisation policy is authoritative, so its effective budget
    (including stricter repository overrides) is enforced without a feature
    toggle. Local-only budgets retain their existing capability gate.
    """
    from openshard.osn.budget import BudgetLedger, not_enforced_record

    if organisation_policy is not None and organisation_policy.applied:
        return (BudgetLedger(effective_limits), None) if effective_limits.configured else (None, None)
    if not local_limits.configured:
        return None, None

    from openshard.sync.capabilities import CAPABILITY_AGENT_BUDGETS

    state = capabilities.state
    if state.enabled(CAPABILITY_AGENT_BUDGETS):
        return BudgetLedger(local_limits), None
    reason = state.reason or "capability_not_enabled"
    click.echo(
        f"agent_budgets: not enabled for this organisation ({reason}); the configured budget is not enforced.",
        err=True,
    )
    return None, not_enforced_record(local_limits, reason)

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
