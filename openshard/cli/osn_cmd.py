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
    from openshard.learning.retrieval import LearningContext
    from openshard.learning.snapshot import LearningSnapshot
    from openshard.osn.budget import BudgetLedger, BudgetLimits
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
            tag = " · recovery after independent review" if data.get("review_recovery") else ""
            echo(f"\nAttempt {data.get('attempt')} · {model}{tag}")
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
            self._stop()
            if not data.get("in_turn"):
                echo("\nVerification")
            self._start("Running verification" + (" (requested by the model)" if data.get("in_turn") else ""))
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
            if action == "review_recovery":
                echo("  Independent review raised concerns (model-reported); one bounded recovery attempt")
            elif action == "escalate" and isinstance(recovery_model, str) and recovery_model:
                echo("  Verification failure observed")
                echo(f"  → {_friendly_model(recovery_model)}")
            else:
                echo(f"  {action or 'stop'} · {data.get('reason') or 'no reason recorded'}")
        elif event == "budget_stop":
            self._stop()
            echo(f"  ✗ Budget stopped the run · {data.get('reason') or 'limit reached'}")
        elif event == "turn_start":
            self._stop()
            role = data.get("role")
            prefix = f"{role.capitalize()} turn" if isinstance(role, str) and role != "executor" else "Turn"
            self._start(f"{prefix} {data.get('turn')}/{data.get('max_turns')} · {_friendly_model(data.get('model'))}")
        elif event == "explore_start":
            self._stop()
            n = data.get("questions") or 0
            echo(f"  Exploring {n} question{'s' if n != 1 else ''} in parallel (read-only) · "
                 f"{_friendly_model(data.get('model'))}")
            self._start("Explorers working")
        elif event == "explore_end":
            self._stop()
            echo(f"  ✓ Exploration done · {data.get('answered')}/{data.get('total')} answered")
        elif event == "role_start":
            self._stop()
            role = data.get("role")
            if role == "planner":
                echo("\nPlanning (read-only)")
            elif role == "verifier":
                echo("\nIndependent review")
                self._start("Reviewing the verified change")
        elif event == "role_end":
            self._stop()
            role = data.get("role")
            status = data.get("status")
            role_model = _friendly_model(data.get("model")) if data.get("model") else ""
            if role == "planner":
                if status == "ran" and data.get("has_plan"):
                    echo("  ✓ Plan ready" + (f" · {role_model}" if role_model else ""))
                else:
                    echo(f"  ? Planner {status or 'did not run'}" + (f" · {data.get('reason')}" if data.get("reason") else "")
                         + " · continuing without a plan")
            elif role == "verifier":
                verdict = data.get("verdict")
                if status == "ran" and verdict:
                    mark = "✓" if verdict == "pass" else "!" if verdict == "warn" else "✗"
                    echo(f"  {mark} Review {verdict.upper()} (model-reported" + (f", {role_model}" if role_model else "") + ")")
                else:
                    echo(f"  ? Review {status or 'unavailable'}" + (f" · {data.get('reason')}" if data.get("reason") else ""))
        elif event == "turn_response":
            self._stop()
            n = data.get("actions") or 0
            note = data.get("note")
            echo(f"  Turn {data.get('turn')} · {n} action{'s' if n != 1 else ''}" + (f" · {note}" if note else ""))
        elif event == "action":
            self._stop()
            kind = data.get("kind") or "action"
            target = data.get("target") or ""
            status = data.get("status")
            decision = data.get("decision")
            summary = data.get("summary")
            if kind == "finish":
                echo("    ■ finish" + (f" · {summary}" if summary else ""))
            elif status == "refused":
                echo(f"    ✗ {kind} {target} · refused ({decision})")
            elif status == "failed":
                echo(f"    ✗ {kind} {target}" + (f" · {summary}" if summary else " · failed"))
            else:
                echo(f"    → {kind} {target}" + (f" · {summary}" if summary else ""))
        elif event == "verification_reused":
            echo("\nVerification")
            echo("  ✓ Already observed on the final files (not run again)")


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
@click.option("--loop", "loop_mode", type=click.Choice(["agent", "writes"]), default="agent", show_default=True,
              help="agent: the model inspects, writes and verifies over several bounded turns. "
                   "writes: one whole-file proposal per attempt (the original one-shot loop).")
@click.option("--max-turns", default=12, type=click.IntRange(1, 30), show_default=True,
              help="Model turns per attempt in --loop agent mode.")
@click.option("--roles", "roles_mode", type=click.Choice(["auto", "executor", "full"]), default="auto",
              show_default=True,
              help="auto: a read-only planner before the executor when the task is not trivial, and an "
                   "independent model review after a verified result when a distinct model is available. "
                   "executor: no planner, no review. full: both, always (the review may reuse the executor's model).")
@click.option("--planner-model", default=None, help="Model for the planner role (default: routed).")
@click.option("--verifier-model", default=None, help="Model for the independent review (default: routed, never the executor's).")
@click.option("--explore/--no-explore", "explore", default=True, show_default=True,
              help="Let the planner answer up to 3 independent questions with parallel read-only workers "
                   "(at most 3 at once, never writing). Only when the planner runs and only when it asks.")
@click.option("--task-id", default=None, help="Explicit task id (from `openshard task new`).")
@click.option("--promote", is_flag=True, default=False,
              help="After verified success, copy changed files into the repo through the policy gate.")
@click.option("--yes", "assume_yes", is_flag=True, default=False,
              help="Approve policy 'ask' paths during the OSN run and promotion without prompting.")
@click.option("--no-learning", "no_learning", is_flag=True, default=False,
              help="Do not consult learning signals from this repository's prior OpenShard runs.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def osn_run(task, verify_cmd, model, escalate, provider, context_files, max_attempts, loop_mode, max_turns,
            roles_mode, planner_model, verifier_model, explore, task_id, promote, assume_yes, no_learning,
            as_json):
    """Run TASK through the bounded OSN loop."""
    from openshard.cli.ingest import _repo_root
    from openshard.history.jsonl_store import append_jsonl
    from openshard.osn.loop import run_bounded_loop
    from openshard.osn.model_provider import IterativeModelProvider, ModelActionProvider
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
        organisation_permissions,
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
        permissions = organisation_permissions(organisation_policy)
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
    from openshard.learning.record import check_identity

    check = check_identity(argv)
    check_fingerprint = (check or {}).get("fingerprint")
    # One bounded read of the precomputed learning snapshot, frozen for the whole
    # run: routing history, the model's context and the Receipt all come from it.
    snapshot = None if no_learning else _lookup_learning(repo_root, repo_config)
    learning = snapshot.consult(task, current_check_fingerprint=check_fingerprint) if snapshot else None
    routing = _resolve_routing(task, repo_root, explicit_model=model, escalate=list(escalate),
                               capabilities=capabilities, model_policy=model_policy,
                               max_attempts=attempts_allowed,
                               cost_budget_usd=budget.limits.max_spend_usd if budget is not None else None,
                               learning=learning, snapshot=snapshot)
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
    learning_for = _learning_by_model(snapshot, task, check_fingerprint)
    if learning_for is not None:
        learning = learning_for(model)  # the first model's view: never another model's statistics
    learning_files = _learning_context_files(learning, repo_root, list(context_files))
    if not as_json:
        click.echo(f"  Route   {' → '.join(_friendly_model(m) for m in models)}")
        if routing.applied and routing.decision is not None:
            click.echo(f"  Policy  Adaptive Routing V2 · {routing.decision.resolved_class}")
        for line in _learning_preamble(learning, snapshot):
            click.echo(line)
        if learning is not None and snapshot is not None and snapshot.trimmed \
                and learning.status in ("used", "no_relevant_signals"):
            click.echo(f"    (learning snapshot trimmed to fit: {snapshot.signals_stored} of "
                       f"{snapshot.signals_derived} derived signal(s) stored)")
        for f in learning_files:
            click.echo(f"    + context {f} (a test that failed on similar work; shown as untrusted content)")

    def run_approver(rel, _decision):
        if assume_yes:
            return True, "flag_yes"
        if as_json:
            return False, "json_no_prompt"
        try:
            granted = click.confirm(f"Policy requires approval to write {rel}. Continue in the isolated workspace?", default=False)
        except click.Abort:
            granted = False
        return granted, "interactive_prompt"

    provider_cls = IterativeModelProvider if loop_mode == "agent" else ModelActionProvider
    action_provider = provider_cls(
        provider=provider_obj, models=models, repo_root=repo_root,
        context_files=[*context_files, *learning_files],
        budget=budget,
        learning_context=learning.prompt_text if learning is not None else None,
        learning_context_for=(lambda m: learning_for(m).prompt_text) if learning_for is not None else None,
    )
    if not as_json:
        click.echo(f"  Loop    {'agent (bounded turns: inspect → write → verify)' if loop_mode == 'agent' else 'one-shot writes'}")
    supervisor = _resolve_supervisor(routing, budget, action_provider, capabilities, user_ladder=list(escalate),
                                     explicit_model=explicit_model)
    progress_renderer = _OsnProgressRenderer() if not as_json else None
    planner_hook, verifier_hook, role_skips, role_usage = _resolve_roles(
        loop_mode=loop_mode, roles_mode=roles_mode, planner_model=planner_model, verifier_model=verifier_model,
        explore=explore,
        task=task, repo_root=repo_root, executor_model=model, routing=routing, provider_name=provider_name,
        provider_obj=provider_obj, model_policy=model_policy, budget=budget, learning=learning,
        context_files=[*context_files, *learning_files], action_provider=action_provider, argv=argv,
        progress=progress_renderer,
    )
    if not as_json and loop_mode == "agent":
        for line in _roles_preamble(role_skips, planner_hook is not None, verifier_hook is not None):
            click.echo(line)

    started = time.monotonic()
    try:
        receipt = run_bounded_loop(
            repo_root, task, action_provider, argv, task_id=task_id, max_attempts=max_attempts,
            budget=budget,
            supervisor=supervisor,
            progress=progress_renderer,
            max_turns=max_turns,
            planner=planner_hook,
            verifier=verifier_hook,
            organisation_approver=run_approver,
            blocked_write_patterns=permissions.blocked_write_paths,
            approval_write_patterns=permissions.approval_write_paths,
            blocked_command_prefixes=permissions.blocked_command_prefixes,
        )
    finally:
        if progress_renderer is not None:
            progress_renderer.close()
    duration = time.monotonic() - started
    for skipped_role, (skip_reason, skip_choice) in role_skips.items():
        if skipped_role not in receipt.roles:
            from openshard.osn.roles import RoleRun

            receipt.roles[skipped_role] = RoleRun.skipped(skipped_role, skip_reason, skip_choice).to_record()
    # Every model call of the run, in order: the planner's (attempt 0), then each
    # attempt's executor calls followed by any review of that attempt.
    all_usage = sorted(
        enumerate([*role_usage, *action_provider.usage]),
        key=lambda iu: (iu[1].attempt, {"planner": 0, "executor": 1}.get(iu[1].role, 2), iu[0]),
    )
    all_usage = [u for _, u in all_usage]

    from openshard.learning.record import build_learning_record

    learning_record = build_learning_record(
        _supplied_learning(learning_for, action_provider.learning_models, learning),
        check=check,
        attempt_models=_attempt_models(action_provider.usage),
        context_supplied=action_provider.learning_supplied,
        routing_record=routing.record,
        context_files_added=learning_files if action_provider.learning_supplied else [],
        snapshot=snapshot.record() if snapshot is not None else None,
    )
    runs_path = repo_root / ".openshard" / "runs.jsonl"
    run_index = sum(1 for _ in runs_path.open(encoding="utf-8")) if runs_path.exists() else 0
    entry = build_osn_run_entry(
        receipt, task=task, usage=all_usage, duration_seconds=duration,
        repo_path=repo_root, task_id=task_id, run_index=run_index,
        budget_record=budget.to_record() if budget is not None else budget_record,
        routing_decision=routing.decision, routing_record_mode=routing.record_mode,
        routing_record=routing.record, explicit_model=routing.first_model if routing.decision is None else None,
        supervisor_record=supervisor.to_record() if supervisor is not None else None,
        capability_snapshot=capabilities.to_record(),
        organisation_policy=(
            organisation_policy.receipt_record(
                effective_policy_hash=effective_policy_hash(model_policy, effective_limits, permissions),
                repository_override_applied=repository_override_present(
                    repo_config, has_config_file=config_path is not None,
                ),
            )
            if organisation_policy is not None else None
        ),
        learning_record=learning_record,
    )
    # Only present when a budget was configured / a capability was looked up.
    # The machine output otherwise gained only the compact ``learning`` summary.
    budget_output = {
        k: entry[k]
        for k in ("agent_budgets", "adaptive_routing", "supervisor_routing", "capability_snapshot", "organisation_policy")
        if k in entry
    }
    store = repo_root / ".openshard"
    store.mkdir(parents=True, exist_ok=True)
    append_jsonl(runs_path, entry)

    promoted: list[str] = []
    skipped: list[str] = []
    if promote:
        if receipt.status != "verified":
            if not as_json:
                click.echo(f"Not promoting: loop status is '{receipt.status}'.")
        else:
            promoted, skipped = _promote(repo_root, receipt, entry, assume_yes, permissions)

    if as_json:
        click.echo(json.dumps({
            "status": receipt.status, "stop_reason": receipt.stop_reason,
            "verification_state": receipt.verification_state,
            "receipt_id": entry.get("receipt_id"), "task_id": entry.get("task_id"),
            "provider": provider_name, "models": models, "attempts": len(receipt.attempts),
            "changed_files": receipt.changed_files, "promoted": promoted, "skipped": skipped,
            "sandbox_path": receipt.sandbox_path,
            "mode": receipt.mode,
            "turns": entry["osn_loop"].get("turns_total"),
            "action_summary": entry["osn_loop"].get("action_summary"),
            "actions": [
                {k: a.get(k) for k in ("turn", "kind", "target", "decision", "executed", "ok", "role", "model")}
                for att in entry["osn_loop"].get("attempts") or [] for a in att.get("actions") or []
            ],
            "model_calls": entry["osn_loop"].get("model_calls"),
            "cost_provenance": entry.get("cost_provenance"),
            "roles": entry["osn_loop"].get("roles"),
            "plan": entry["osn_loop"].get("plan"),
            "reviews": entry["osn_loop"].get("reviews"),
            **budget_output,
            "learning": _learning_json(entry.get("learning")),
        }, indent=2))
        return
    click.echo(f"OSN loop: {receipt.status} ({receipt.stop_reason}); verification {receipt.verification_state}")
    click.echo(f"  attempts: {len(receipt.attempts)}   model(s): {', '.join(models)}   provider: {provider_name}")
    agent_line = _agent_loop_line(entry.get("osn_loop"), entry)
    if agent_line:
        click.echo(f"  agent loop: {agent_line}")
    for line in _roles_summary(entry.get("osn_loop")):
        click.echo(line)
    budget_line = _budget_line(entry.get("agent_budgets"))
    if budget_line:
        click.echo(f"  budget: {budget_line}")
    routing_line = _routing_line(entry.get("adaptive_routing"))
    if routing_line:
        click.echo(f"  routing: {routing_line}")
    supervisor_line = _supervisor_line(entry.get("supervisor_routing"))
    if supervisor_line:
        click.echo(f"  supervisor: {supervisor_line}")
    for line in _learning_summary(entry.get("learning")):
        click.echo(line)
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


def _lookup_learning(repo_root: Path, repo_config: dict) -> LearningSnapshot:
    """The precomputed learning snapshot, read once within ``learning.lookup_budget_ms``.

    No history read and no remote call: the background worker derived it
    (``openshard.learning.worker``). Never raises; a late or unusable snapshot
    comes back saying so and the run goes ahead without learning.
    """
    from openshard.learning.snapshot import lookup_budget_ms, lookup_snapshot
    from openshard.learning.worker import nudge

    snapshot = lookup_snapshot(repo_root / ".openshard", budget_ms=lookup_budget_ms(repo_config))
    # After the lookup, outside its budget: a snapshot that is missing, unusable or
    # behind history (one stat) gets a background re-derivation for the next run.
    nudge(repo_root / ".openshard" / "runs.jsonl", snapshot)
    return snapshot


def _learning_by_model(snapshot: LearningSnapshot | None, task: str, check_fingerprint: str | None):
    """``model -> LearningContext`` from the one frozen snapshot, computed once per model.

    Each model gets the task's relevant signals minus other models' results.
    None when learning is off.
    """
    if snapshot is None:
        return None
    cache: dict[str, LearningContext] = {}

    def learning_for(model: str) -> LearningContext:
        if model not in cache:
            cache[model] = snapshot.consult(task, current_check_fingerprint=check_fingerprint, model=model)
        return cache[model]

    return learning_for


def _supplied_learning(learning_for, supplied_models: list[str], default: LearningContext | None):
    """The learning the Receipt reports: the signals that actually reached a model.

    With escalation, attempts on different models may have seen different
    model-specific signals; the record lists every signal shown, in the order
    first shown. When nothing reached a model it is *default* (what was
    retrieved) and the record's ``context_supplied`` says it was not shown.
    """
    if learning_for is None or not supplied_models:
        return default
    contexts = [learning_for(m) for m in supplied_models]
    if len(contexts) == 1:
        return contexts[0]
    from dataclasses import replace

    retrieved, seen = [], set()
    checks, seen_checks = [], set()
    for ctx in contexts:
        for r in ctx.retrieved:
            if r.signal.signal_id not in seen:
                seen.add(r.signal.signal_id)
                retrieved.append(r)
        for c in ctx.recommended_checks:
            if c.signal_id not in seen_checks:
                seen_checks.add(c.signal_id)
                checks.append(c)
    first = contexts[0]
    return replace(first, retrieved=retrieved, recommended_checks=checks,
                   status="used" if retrieved else first.status,
                   current_check_recommended=any(c.current_check_recommended for c in contexts))


def _learning_context_files(learning: LearningContext | None, repo_root: Path, explicit: list[str]) -> list[str]:
    """Files learning adds for the model to read: existing, repo-relative, not hidden,
    not already given by the user. Never raises."""
    if learning is None:
        return []
    from openshard.safety.sanitize import looks_like_secret
    from openshard.security.paths import UnsafePathError, resolve_safe_repo_path

    out: list[str] = []
    for rel in learning.suggested_context_files:
        if rel in explicit or looks_like_secret(rel) or any(part.startswith(".") for part in rel.split("/")):
            continue
        try:
            if resolve_safe_repo_path(repo_root, rel).is_file():
                out.append(rel)
        except (UnsafePathError, OSError, ValueError):
            continue
    return out


def _attempt_models(usage) -> list[tuple[int, str]]:
    """The model each attempt requested (its last call), for later learning."""
    by_attempt: dict[int, str] = {}
    for u in usage:
        model = u.requested_model or u.model
        if model:
            by_attempt[u.attempt] = model
    return sorted(by_attempt.items())


# Why an ``unavailable`` snapshot could not be used, where that is more specific
# than "could not be read".
_SNAPSHOT_UNAVAILABLE = {
    "missing": "learning snapshot not built yet; continuing without it",
    "oversized": "learning snapshot exceeded the size cap; continuing without it",
}


def _learning_preamble(learning: LearningContext | None, snapshot: LearningSnapshot | None = None) -> list[str]:
    """What learning found, shown before the run starts. Advisory wording only."""
    if learning is None:
        return ["  Learning  off (--no-learning)"]
    if not learning.used:
        specific = _SNAPSHOT_UNAVAILABLE.get(snapshot.status) if snapshot is not None else None
        if learning.status == "unavailable" and specific:
            return [f"  Learning  {specific}"]
        reason = {
            "no_history": "no prior verified evidence in this repository",
            "unavailable": "learning evidence could not be read; continuing without it",
            "timeout": "the bounded learning lookup did not finish in time; continuing without it",
            "no_relevant_signals": f"{learning.signals_considered} signal(s) known, none relevant to this task",
            "error": "history could not be read; continuing without it",
        }.get(learning.status, learning.status)
        return [f"  Learning  {reason}"]
    lines = [
        f"  Learning  {len(learning.retrieved)} prior signal(s) considered · "
        f"{len(learning.supporting_receipt_ids)} supporting Receipt(s) · advisory"
    ]
    for r in learning.retrieved:
        lines.append(f"    - {r.signal.summary}")
    for rec in learning.recommended_checks:
        lines.append(f"    ! History suggests also verifying with `{rec.label}` (not run automatically)")
    return lines


def _learning_summary(record: dict | None) -> list[str]:
    if not isinstance(record, dict) or not record.get("used"):
        return []
    routing = record.get("routing") or {}
    verification = record.get("verification") or {}
    return [
        f"  learning: {record.get('signals_used', 0)} prior signal(s) considered; "
        f"context supplied: {'yes' if record.get('context_supplied') else 'no'}; "
        f"routing influenced: {'yes' if routing.get('influenced') else 'no'}; "
        f"verification influenced: {'yes' if verification.get('influenced') else 'no (advisory only)'}"
    ]


def _learning_json(record: dict | None) -> dict | None:
    if not isinstance(record, dict):
        return None
    return {
        "status": record.get("status"),
        "signals_used": record.get("signals_used", 0),
        "signal_ids": list(record.get("signal_ids") or []),
        "context_supplied": bool(record.get("context_supplied")),
        "routing_influenced": bool((record.get("routing") or {}).get("influenced")),
        "verification_influenced": bool((record.get("verification") or {}).get("influenced")),
        "recommended_checks": [
            c.get("label") for c in (record.get("verification") or {}).get("recommended_checks") or []
        ],
    }


def _resolve_routing(task: str, repo_root: Path, *, explicit_model: str | None, escalate: list[str],
                     capabilities: LazyCapabilities, model_policy, max_attempts: int | None = None,
                     cost_budget_usd: float | None = None,
                     learning: LearningContext | None = None,
                     snapshot: LearningSnapshot | None = None) -> OsnRouting:
    """First model and escalation ladder: the user's choice, else Routing V2 when
    the ``adaptive_routing`` capability is on, else the keyword router as before.

    With learning on, Routing V2 first tries history from this repository and
    task category, under the same sample gate, before the harness-wide history."""
    from openshard.osn.routing import CAPABILITY as ROUTING_CAPABILITY
    from openshard.osn.routing import HARNESS, resolve_osn_routing
    from openshard.routing.engine import route

    def model_policy_loader():
        return model_policy

    def history_loader():
        if snapshot is not None:
            # Learning on: the frozen snapshot's precomputed history, never a re-read.
            shape = learning.shape if learning is not None else None
            return snapshot.history(shape.task_category if shape is not None else None, harness=HARNESS)
        # --no-learning (the only way there is no snapshot): harness-wide history, as always.
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

def _count_repo_files(repo_root: Path, cap: int = 200) -> int:
    """How many files the repository has, up to *cap* (enough to tell a tiny fixture from a project)."""
    ignored = {".git", ".openshard", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache"}
    count = 0
    try:
        for p in repo_root.rglob("*"):
            if any(part in ignored for part in p.relative_to(repo_root).parts):
                continue
            if p.is_file():
                count += 1
                if count >= cap:
                    break
    except OSError:
        return count
    return count


def _resolve_roles(*, loop_mode, roles_mode, planner_model, verifier_model, task, repo_root, executor_model,
                   routing, provider_name, provider_obj, model_policy, budget, learning, context_files,
                   action_provider, argv, progress, explore=True):
    """Planner and verifier hooks for this run, the roles that will not run and why, and their usage list.

    Role models come from ``openshard.osn.roles.select_role_model`` and must pass
    the same ``models`` policy as the executor's. Nothing here calls a model;
    the hooks do, inside the loop, at the boundaries the loop owns.
    """
    role_usage: list = []
    role_skips: dict = {}
    if loop_mode != "agent":
        return None, None, role_skips, role_usage
    from openshard.osn import roles as osn_roles
    from openshard.routing.engine import route
    from openshard.sync.policies import enforce_models_allowed

    catalog_knows = None
    try:
        from openshard.models.catalog import load_catalog

        _catalog = load_catalog(refresh="never")

        def catalog_knows(model_id: str) -> bool:  # noqa: E306
            try:
                return _catalog.resolve(model_id) is not None
            except Exception:
                return False
    except Exception:
        catalog_knows = None
    planner_choice = osn_roles.select_role_model(
        osn_roles.ROLE_PLANNER, explicit=planner_model, executor_model=executor_model, routing=routing,
        provider_name=provider_name, catalog_knows=catalog_knows,
    )
    verifier_choice = osn_roles.select_role_model(
        osn_roles.ROLE_VERIFIER, explicit=verifier_model, executor_model=executor_model, routing=routing,
        provider_name=provider_name, catalog_knows=catalog_knows,
    )
    try:
        enforce_models_allowed(
            [m for m in (planner_choice.model, verifier_choice.model) if m and m != executor_model], model_policy,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None

    task_category = route(task).category
    repo_file_count = _count_repo_files(repo_root)
    want_planner, planner_skip = osn_roles.planner_wanted(
        roles_mode, task_category=task_category, repo_file_count=repo_file_count,
    )
    planner_hook = None
    if want_planner and planner_choice.model:
        explorer_model = None
        if explore:
            # Exploration workers: a fast control-plane model when routing offers one,
            # else the planner's own model. Reads only, so independence is not required.
            explorer_choice = osn_roles.select_role_model(
                osn_roles.ROLE_EXPLORER, explicit=None, executor_model=executor_model, routing=routing,
                provider_name=provider_name, catalog_knows=catalog_knows,
            )
            explorer_model = (
                explorer_choice.model if explorer_choice.source != osn_roles.SOURCE_EXECUTOR_REUSED
                else planner_choice.model
            )
            try:
                enforce_models_allowed([m for m in (explorer_model,) if m and m != executor_model], model_policy)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from None

        def planner_hook(sandbox, repo_files):  # noqa: E306
            plan, role, usage = osn_roles.run_planner_turns(
                provider_obj, planner_choice.model, task=task, repo_root=repo_root, sandbox=sandbox,
                repo_files=repo_files, choice=planner_choice, provider_name=provider_name, budget=budget,
                learning_context=learning.prompt_text if learning is not None else None,
                context_files=list(context_files), progress=progress, explorer_model=explorer_model,
            )
            role_usage.extend(usage)
            return plan, role.to_record()
    else:
        role_skips[osn_roles.ROLE_PLANNER] = (planner_skip or "no_planner_model", planner_choice)

    want_verifier, verifier_skip = osn_roles.verifier_wanted(
        roles_mode, verifier_choice, task_category=task_category, repo_file_count=repo_file_count,
    )
    verifier_hook = None
    if want_verifier and verifier_choice.model:
        check_name = argv[0].replace("\\", "/").rsplit("/", 1)[-1] if argv else ""

        def verifier_hook(sandbox, changed, result, attempt):  # noqa: E306
            from openshard.osn.agent_loop import sandbox_diff_text

            diff_text = sandbox_diff_text(repo_root, sandbox, changed, limit=osn_roles.MAX_REVIEW_DIFF_CHARS)
            verification = {
                "status": "passed" if result.passed else "failed", "exit_code": result.exit_code,
                "failed_tests": list(result.failed_tests), "command": check_name,
            }
            review, usage, err = osn_roles.run_verifier_call(
                provider_obj, verifier_choice.model, task=task, plan=getattr(action_provider, "plan", None),
                diff_text=diff_text, verification=verification, changed_files=list(changed), attempt=attempt,
                budget=budget,
            )
            role_usage.extend(usage)
            role = osn_roles.RoleRun.from_usage(
                osn_roles.ROLE_VERIFIER, usage, choice=verifier_choice, provider=provider_name,
                status=osn_roles.STATUS_RAN if review else osn_roles.STATUS_FAILED, reason=err,
            )
            return review, role.to_record()
    else:
        role_skips[osn_roles.ROLE_VERIFIER] = (verifier_skip or "no_verifier_model", verifier_choice)
    return planner_hook, verifier_hook, role_skips, role_usage


def _roles_preamble(role_skips: dict, planner: bool, verifier: bool) -> list[str]:
    parts = []
    parts.append("planner" if planner else f"no planner ({role_skips.get('planner', ('?',))[0]})")
    parts.append("executor")
    parts.append("independent review" if verifier else f"no review ({role_skips.get('verifier', ('?',))[0]})")
    return [f"  Roles   {' → '.join(parts)}"]


def _roles_summary(loop: dict | None) -> list[str]:
    """One line per role after the run: what ran on which model, what it cost, what it concluded."""
    if not isinstance(loop, dict) or not isinstance(loop.get("roles"), dict):
        return []
    out: list[str] = []
    for role in ("planner", "executor", "verifier"):
        rec = loop["roles"].get(role)
        if not isinstance(rec, dict):
            continue
        if rec.get("status") != "ran":
            out.append(f"  {role}: {rec.get('status')} ({rec.get('reason') or 'no reason recorded'})")
            continue
        cost = rec.get("cost_usd")
        label = {"provider_reported": "provider-reported", "list_rate_estimate": "list-rate estimate"}.get(
            str(rec.get("cost_source") or ""), "origin not recorded",
        )
        cost_text = f"${cost:.4f} ({label})" if isinstance(cost, (int, float)) else "cost unknown"
        tokens = rec.get("total_tokens")
        bits = [f"{_friendly_model(rec.get('model'))}", f"{rec.get('calls')} call(s)"]
        if isinstance(tokens, int):
            bits.append(f"{tokens:,} tokens")
        bits.append(cost_text)
        if rec.get("independent") is True:
            bits.append("independent model")
        elif rec.get("independent") is False and role == "verifier":
            bits.append("same model as executor")
        out.append(f"  {role}: " + " · ".join(bits))
        for ex in rec.get("explorers") or []:
            ex_cost = ex.get("cost_usd")
            ex_cost_text = f"${ex_cost:.4f}" if isinstance(ex_cost, (int, float)) else "cost unknown"
            out.append(f"    explorer {int(ex.get('index', 0)) + 1}: {ex.get('status')} · "
                       f"{_friendly_model(ex.get('model'))} · {ex.get('findings_count', 0)} finding(s) · {ex_cost_text}")
    for r in loop.get("reviews") or []:
        line = f"  review (attempt {r.get('attempt')}): {str(r.get('verdict', '?')).upper()} (model-reported)"
        if r.get("summary"):
            line += f" · {r['summary']}"
        if r.get("recovery_requested"):
            line += f" · recovery: {r.get('recovery_outcome') or 'requested'}"
        out.append(line)
    return out


def _agent_loop_line(loop: dict | None, entry: dict) -> str | None:
    """One line on how the agent spent its turns and what the model calls cost, with provenance."""
    if not isinstance(loop, dict) or not isinstance(loop.get("action_summary"), dict):
        return None
    acts = loop["action_summary"]
    inspect = (acts.get("reads") or 0) + (acts.get("searches") or 0) + (acts.get("listings") or 0)
    parts = [f"{loop.get('turns_total')} turn(s)", f"{acts.get('actions', 0)} action(s)", f"{inspect} inspect"]
    parts.append(f"{acts.get('writes_applied', 0)} write(s)")
    if acts.get("writes_blocked"):
        parts.append(f"{acts['writes_blocked']} refused")
    parts.append(f"{acts.get('verifications', 0)} model-requested verification(s)")
    calls = loop.get("model_calls") or []
    if calls:
        cost = entry.get("estimated_cost")
        prov = entry.get("cost_provenance")
        label = {"provider_reported": "provider-reported", "official_rate_estimate": "list-rate estimate"}.get(
            prov or "", "origin not recorded",
        )
        cost_text = f"${cost:.4f} ({label})" if isinstance(cost, (int, float)) else "cost unknown"
        parts.append(f"{len(calls)} model call(s) · {entry.get('total_tokens', 0):,} tokens · {cost_text}")
    return " · ".join(parts)


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


def _promote(repo_root: Path, receipt, entry: dict, assume_yes: bool, permissions) -> tuple[list[str], list[str]]:
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
        blocked_patterns=permissions.blocked_write_paths,
        approval_patterns=permissions.approval_write_paths,
    )
    log_sandbox_apply_receipt(SandboxApplyReceipt(
        source_run_id=entry.get("timestamp", ""), sandbox_path="",
        applied=result.applied, files_applied=list(result.files_applied),
        files_skipped=list(result.files_skipped), dry_run=False, reason=result.reason,
        policy=dict(result.policy_summary),
    ))
    return list(result.files_applied), list(result.files_skipped)
