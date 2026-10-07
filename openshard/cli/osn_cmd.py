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
            if data.get("after_workers"):
                tag = " · resolving what synthesis could not"
            echo(f"\nAttempt {data.get('attempt')} · {model}{tag}")
            self._start(f"Calling {model}")
        elif event == "stage_start":
            self._stop()
            echo(f"\nAttempt {data.get('attempt')} · deciding topology from the plan")
            self._start("Deciding topology")
        elif event == "stage_skipped":
            self._stop()
            echo(f"  No parallel workers ({data.get('reason') or 'not selected'})")
        elif event == "stage_end" and data.get("stage") == "candidates":
            self._stop()
            winner = data.get("winner")
            echo(f"  ✓ Candidates done · {data.get('workers')} candidate(s) · "
                 + (f"winner {winner} ({_friendly_model(data.get('winner_model'))}) · {data.get('applied')} file(s) applied"
                    if winner else "none verified in its own copy · executor takes over"))
        elif event == "stage_end":
            self._stop()
            echo(f"  ✓ Workers done · {data.get('workers')} worker(s) · synthesis applied {data.get('applied')} file(s)"
                 f" · {data.get('conflicts')} conflict(s) · "
                 + ("executor resolves the rest" if data.get("resolution") == "executor_turns"
                    else "no executor turns needed"))
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
        elif event == "malformed_reply":
            self._stop()
            echo(f"  ✗ Turn {data.get('turn')} · the model's reply was not a usable action list"
                 + (f" ({data.get('message')})" if data.get("message") else "") + " · attempt ends")
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
@click.option("--topology", "topology_request",
              type=click.Choice(["auto", "single", "roles", "parallel", "candidates"]),
              default="auto", show_default=True,
              help="Execution topology. auto: one executor unless the planner proposes independent subtasks "
                   "with disjoint write scopes on a non-trivial task, then bounded parallel workers + synthesis. "
                   "single: one executor, no roles. roles: planner/verifier but never workers. parallel: workers "
                   "whenever the planner's decomposition validates (falls back with the reason otherwise). "
                   "candidates: the whole task on up to --max-workers distinct models at once, each verified by "
                   "OpenShard in its own copy and ranked deterministically; the winner is verified again.")
@click.option("--max-workers", default=3, type=click.IntRange(1, 3), show_default=True,
              help="Most parallel writing workers or candidates (each in its own isolated copy).")
@click.option("--explore/--no-explore", "explore", default=True, show_default=True,
              help="Let the planner answer up to 3 independent questions with parallel read-only workers "
                   "(at most 3 at once, never writing). Only when the planner runs and only when it asks.")
@click.option("--task-id", default=None, help="Explicit task id (from `openshard task new`).")
@click.option("--promote", is_flag=True, default=False,
              help="After verified success, copy changed files into the repo through the policy gate.")
@click.option("--commit", "commit_result", is_flag=True, default=False,
              help="With --promote: commit the promoted files on the current branch, then re-run the "
                   "verification command on that exact commit so the Receipt's verification is bound to it "
                   "(requires a clean working tree apart from the promoted files).")
@click.option("--yes", "assume_yes", is_flag=True, default=False,
              help="Approve policy 'ask' paths during the OSN run and promotion without prompting.")
@click.option("--no-learning", "no_learning", is_flag=True, default=False,
              help="Do not consult learning signals from this repository's prior OpenShard runs.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
@click.option("--resume-from", "resume_from", default=None, hidden=True,
              help="Internal: continue the checkpointed run with this id (use `openshard osn resume`).")
def osn_run(task, verify_cmd, model, escalate, provider, context_files, max_attempts, loop_mode, max_turns,
            roles_mode, planner_model, verifier_model, topology_request, max_workers, explore, task_id, promote,
            commit_result, assume_yes, no_learning, as_json, resume_from=None):
    """Run TASK through the bounded OSN loop."""
    if commit_result and not promote:
        raise click.UsageError("--commit requires --promote: only promoted files can be committed.")
    if topology_request == "single":
        roles_mode = "executor"  # a single executor: no planner, no review, no workers
    import uuid

    from openshard.cli.ingest import _repo_root
    from openshard.history.jsonl_store import append_jsonl
    from openshard.osn import checkpoint as ckpt
    from openshard.osn.loop import create_isolated_copy, run_bounded_loop
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

    prior_checkpoint: ckpt.RunCheckpoint | None = None
    if resume_from:
        prior_checkpoint = _load_resumable_checkpoint(repo_root, resume_from)
        argv = list(prior_checkpoint.verify_argv)  # the run's own command, never a new one
    # The checkpoint's own id: never a task id (those are minted only by `openshard task new`).
    checkpoint_id = prior_checkpoint.run_id if prior_checkpoint else f"osn-{uuid.uuid4().hex[:12]}"

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
    run_checkpoint = ckpt.RunCheckpoint(
        run_id=checkpoint_id, task=task, verify_argv=list(argv),
        args={
            "task_id": task_id, "provider": provider, "context_files": list(context_files), "max_attempts": max_attempts,
            "loop_mode": loop_mode, "max_turns": max_turns, "roles_mode": roles_mode,
            "planner_model": planner_model, "verifier_model": verifier_model,
            "topology_request": topology_request, "max_workers": max_workers, "explore": explore,
            "no_learning": no_learning,
        },
        repo=ckpt.repo_fingerprint(repo_root), models=list(models), routing_record=routing.record,
    )
    prior_usage: list = []
    resume_state: dict | None = None
    resume_sandbox: Path | None = None
    if prior_checkpoint is not None:
        run_checkpoint.created_at = prior_checkpoint.created_at
        run_checkpoint.resumed_from = [
            *prior_checkpoint.resumed_from,
            f"{prior_checkpoint.status}@{prior_checkpoint.phase}@{prior_checkpoint.updated_at}",
        ]
        run_checkpoint.routing_record = prior_checkpoint.routing_record
        resume_state = dict(prior_checkpoint.state)
        prior_usage = [ckpt.usage_from_record(d) for d in prior_checkpoint.usage]
        resume_sandbox = create_isolated_copy(repo_root)
        try:
            ckpt.restore_changed(repo_root, prior_checkpoint.run_id, prior_checkpoint.files, resume_sandbox)
        except FileNotFoundError as exc:
            raise click.ClickException(f"Cannot resume {prior_checkpoint.run_id}: {exc}") from None
        if ckpt.restore_budget(budget, prior_checkpoint.budget) and not as_json:
            click.echo("  Budget  counters carried over from the interrupted run")
        if not as_json:
            click.echo(f"  Resume  {prior_checkpoint.run_id} from '{prior_checkpoint.phase}' · "
                       f"{prior_checkpoint.attempts_done} attempt(s) done · "
                       f"{len(prior_checkpoint.files)} file(s) restored · "
                       f"{len(prior_usage)} earlier model call(s) carried"
                       + (" · planner skipped (plan restored)" if resume_state.get("plan") else ""))
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
        explore=explore, decompose=topology_request in ("auto", "parallel"),
        task=task, repo_root=repo_root, executor_model=model, routing=routing, provider_name=provider_name,
        provider_obj=provider_obj, model_policy=model_policy, budget=budget, learning=learning,
        context_files=[*context_files, *learning_files], action_provider=action_provider, argv=argv,
        progress=progress_renderer,
    )
    workers_hook = _resolve_workers(
        loop_mode=loop_mode, topology_request=topology_request, max_workers=max_workers,
        planner_enabled=planner_hook is not None, verifier_enabled=verifier_hook is not None,
        task=task, repo_root=repo_root, executor_model=model, routing=routing, provider_name=provider_name,
        provider_obj=provider_obj, model_policy=model_policy, budget=budget, argv=argv, role_usage=role_usage,
        permissions=permissions, progress=progress_renderer,
    )
    if not as_json and loop_mode == "agent":
        for line in _roles_preamble(role_skips, planner_hook is not None, verifier_hook is not None):
            click.echo(line)
        click.echo(f"  Topology {topology_request}" + (" (workers possible)" if workers_hook else ""))

    def _write_checkpoint(phase: str, state: dict) -> None:
        run_checkpoint.phase = phase
        run_checkpoint.sandbox_path = state.get("sandbox")
        run_checkpoint.state = state
        run_checkpoint.usage = [ckpt.usage_to_record(u) for u in (*prior_usage, *role_usage, *action_provider.usage)]
        run_checkpoint.budget = ckpt.budget_counters(budget)
        sandbox_dir = Path(state["sandbox"]) if state.get("sandbox") else None
        if sandbox_dir is not None:
            run_checkpoint.files = ckpt.snapshot_changed(sandbox_dir, list(state.get("changed") or []),
                                                         repo_root, checkpoint_id)
        ckpt.write_checkpoint(repo_root, run_checkpoint)

    def _mark_interrupted(reason: str) -> None:
        run_checkpoint.status = ckpt.STATUS_INTERRUPTED
        run_checkpoint.interrupted = {"reason": reason, "phase": run_checkpoint.phase,
                                      "attempts_done": run_checkpoint.attempts_done}
        try:
            ckpt.write_checkpoint(repo_root, run_checkpoint)
        except OSError:
            pass

    ckpt.write_checkpoint(repo_root, run_checkpoint)  # phase 'started': the run exists before any model call
    started = time.monotonic()
    try:
        receipt = run_bounded_loop(
            repo_root, task, action_provider, argv, task_id=task_id, max_attempts=max_attempts,
            budget=budget,
            supervisor=supervisor,
            progress=progress_renderer,
            checkpoint=_write_checkpoint,
            resume=resume_state,
            sandbox_path=resume_sandbox,
            max_turns=max_turns,
            planner=planner_hook,
            verifier=verifier_hook,
            workers=workers_hook,
            organisation_approver=run_approver,
            blocked_write_patterns=permissions.blocked_write_paths,
            approval_write_patterns=permissions.approval_write_paths,
            blocked_command_prefixes=permissions.blocked_command_prefixes,
        )
    except KeyboardInterrupt:
        _mark_interrupted("keyboard_interrupt")
        if progress_renderer is not None:
            progress_renderer.close()
        click.echo(f"\nInterrupted after '{run_checkpoint.phase}' ({run_checkpoint.attempts_done} attempt(s) done). "
                   f"Resume with: openshard osn resume {checkpoint_id}", err=True)
        raise click.Abort() from None
    except BaseException as exc:
        _mark_interrupted(f"{type(exc).__name__}")
        raise
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
        enumerate([*prior_usage, *role_usage, *action_provider.usage]),
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
    entry = build_osn_run_entry(
        receipt, task=task, usage=all_usage, duration_seconds=duration,
        repo_path=repo_root, task_id=task_id,
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

    # Promotion (and the optional commit) happen before the Receipt is written, so
    # the Receipt can carry the commit OpenShard itself created and observed.
    promoted: list[str] = []
    skipped: list[str] = []
    commit_record: dict | None = None
    if promote:
        if receipt.status != "verified":
            if not as_json:
                click.echo(f"Not promoting: loop status is '{receipt.status}'.")
        else:
            promoted, skipped = _promote(repo_root, receipt, entry, assume_yes, permissions)
            if commit_result and promoted:
                commit_record = _commit_promoted(repo_root, promoted, task, entry)
                _attach_commit(entry, commit_record)
    if prior_checkpoint is not None:
        prior_costs = [u.cost_usd for u in prior_usage]
        entry["osn_loop"]["resumed"] = {
            **(entry["osn_loop"].get("resumed") or {}),
            "from_run_id": prior_checkpoint.run_id,
            "checkpoint_phase": prior_checkpoint.phase,
            # 'interrupted' (Ctrl-C or an exception the run could still record) or
            # 'running' (the process died without a chance to say so: a crash or kill).
            "checkpoint_status": prior_checkpoint.status,
            "interrupted": prior_checkpoint.interrupted,
            "prior_model_calls": len(prior_usage),
            "prior_cost_usd": sum(c for c in prior_costs if c is not None) if prior_costs and all(c is not None for c in prior_costs) else None,
            "unsaved_progress_discarded": True,  # whatever ran after the last checkpoint is not in this run
            "original_routing": prior_checkpoint.routing_record,
            "times_resumed": len(run_checkpoint.resumed_from),
        }
    append_jsonl(store / "runs.jsonl", entry)
    run_checkpoint.status = ckpt.STATUS_COMPLETED
    run_checkpoint.phase = ckpt.PHASE_COMPLETED
    run_checkpoint.receipt_id = entry.get("receipt_id")
    run_checkpoint.files = {}
    try:
        ckpt.write_checkpoint(repo_root, run_checkpoint)
        files_dir = ckpt.checkpoint_dir(repo_root, checkpoint_id) / ckpt.FILES_DIR
        if files_dir.exists():
            import shutil

            shutil.rmtree(files_dir, ignore_errors=True)
    except OSError:
        pass

    bound_verification: dict | None = None
    if commit_record and commit_record.get("sha"):
        # Re-run the run's own verification command on the committed tree and record
        # it as later evidence bound to that commit (verifications.jsonl), the same
        # path `openshard verify` uses. The Receipt itself is not modified.
        bound_verification = _bind_verification(repo_root, entry, argv, as_json=as_json)

    if as_json:
        click.echo(json.dumps({
            "status": receipt.status, "stop_reason": receipt.stop_reason,
            "verification_state": receipt.verification_state,
            "receipt_id": entry.get("receipt_id"), "task_id": entry.get("task_id"),
            "provider": provider_name, "models": models, "attempts": len(receipt.attempts),
            "changed_files": receipt.changed_files, "promoted": promoted, "skipped": skipped,
            "sandbox_path": receipt.sandbox_path,
            "commit": commit_record,
            "bound_verification": bound_verification,
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
            "topology": entry["osn_loop"].get("topology"),
            "workers": [
                {k: w.get(k) for k in ("worker_id", "subtask_id", "status", "reason", "model", "requested_model",
                                       "changed_files", "turns", "calls", "total_tokens", "cost_usd", "cost_source",
                                       "duration_ms", "verification")}
                for w in entry["osn_loop"].get("workers") or []
            ],
            "synthesis": entry["osn_loop"].get("synthesis"),
            "candidates": entry["osn_loop"].get("candidates"),
            "economics": entry["osn_loop"].get("economics"),
            "resumed": entry["osn_loop"].get("resumed"),
            "checkpoint": {"run_id": checkpoint_id, "status": run_checkpoint.status},
            **budget_output,
            "learning": _learning_json(entry.get("learning")),
        }, indent=2))
        return
    click.echo(f"OSN loop: {receipt.status} ({receipt.stop_reason}); verification {receipt.verification_state}")
    used = entry["osn_loop"].get("implementation_models") or []
    models_text = ", ".join(used) if used else ", ".join(models) + " (ladder; none ran)"
    click.echo(f"  attempts: {len(receipt.attempts)}   implementation model(s): {models_text}   provider: {provider_name}")
    agent_line = _agent_loop_line(entry.get("osn_loop"), entry)
    if agent_line:
        click.echo(f"  agent loop: {agent_line}")
    for line in _roles_summary(entry.get("osn_loop")):
        click.echo(line)
    for line in _parallel_summary(entry.get("osn_loop")):
        click.echo(line)
    resumed = entry["osn_loop"].get("resumed")
    if isinstance(resumed, dict):
        prior_cost = resumed.get("prior_cost_usd")
        click.echo(f"  resumed: {resumed.get('attempts_restored', 0)} attempt(s) and "
                   f"{resumed.get('prior_model_calls', 0)} model call(s) carried from checkpoint "
                   f"'{resumed.get('checkpoint_phase')}' · prior cost "
                   + (f"${prior_cost:.4f}" if isinstance(prior_cost, (int, float)) else "unknown")
                   + " · unsaved progress after the checkpoint discarded")
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
        click.echo(f"  Promoted {len(promoted)} file(s) into the repository"
                   + ("." if commit_record else " (not re-verified there)."))
        if commit_record:
            if commit_record.get("sha"):
                click.echo(f"  Committed {commit_record['sha'][:12]} on {commit_record.get('branch') or 'HEAD'}"
                           f" ({len(commit_record.get('files') or [])} file(s)).")
            else:
                click.echo(f"  Not committed: {commit_record.get('reason') or 'unknown reason'}.")
        if bound_verification:
            status = bound_verification.get("status")
            if bound_verification.get("bound"):
                click.echo(f"  Re-verified on commit {str(bound_verification.get('artifact_sha') or '')[:12]}: "
                           f"{status} (evidence bound to the commit).")
            else:
                click.echo(f"  Re-verified after commit: {status}; NOT bound to the commit "
                           f"({bound_verification.get('reason') or 'working tree not clean'}).")
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
                   action_provider, argv, progress, explore=True, decompose=False):
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
                decompose=decompose,
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


def _load_resumable_checkpoint(repo_root: Path, run_id: str):
    """The checkpoint for *run_id*, or a ClickException naming the rule that refuses the resume."""
    from openshard.osn import checkpoint as ckpt

    try:
        cp = ckpt.read_checkpoint(repo_root, run_id)
    except FileNotFoundError:
        raise click.ClickException(f"No checkpoint for run '{run_id}' under .openshard/osn-runs/ "
                                   f"({ckpt.REFUSE_MISSING}). `openshard osn runs` lists the runs here.") from None
    except ValueError as exc:
        raise click.ClickException(f"Cannot resume '{run_id}': {exc}") from None
    verdict = ckpt.check_resumable(cp, repo_root)
    if not verdict.ok:
        detail = f" ({verdict.detail})" if verdict.detail else ""
        raise click.ClickException(f"Refusing to resume '{run_id}': {verdict.reason}{detail}.")
    return cp


@osn_group.command("resume")
@click.argument("run_id")
@click.option("--promote", is_flag=True, default=False, help="As for `osn run`: promote verified files after the run.")
@click.option("--commit", "commit_result", is_flag=True, default=False, help="As for `osn run --commit`.")
@click.option("--yes", "assume_yes", is_flag=True, default=False, help="Approve policy 'ask' paths without prompting.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
@click.pass_context
def osn_resume(ctx, run_id, promote, commit_result, assume_yes, as_json):
    """Continue an interrupted OSN run from its last checkpoint.

    The run continues in a fresh isolated copy with the checkpointed files, the
    same task, verify command, model ladder and options, the plan and finished
    attempts restored, and the earlier model calls and budget carried into the
    Receipt. Refused when the checkpoint is missing or unreadable, the run already
    completed, its process is still alive, or the repository's HEAD or working
    tree changed since the run started.
    """
    from openshard.cli.ingest import _repo_root

    repo_root = _repo_root(None, False)
    cp = _load_resumable_checkpoint(repo_root, run_id)
    args = dict(cp.args or {})
    ctx.invoke(
        osn_run, task=cp.task, verify_cmd=" ".join(cp.verify_argv), model=cp.models[0] if cp.models else None,
        escalate=tuple(cp.models[1:]), provider=args.get("provider"),
        context_files=tuple(args.get("context_files") or ()), max_attempts=int(args.get("max_attempts") or 2),
        loop_mode=args.get("loop_mode") or "agent", max_turns=int(args.get("max_turns") or 12),
        roles_mode=args.get("roles_mode") or "auto", planner_model=args.get("planner_model"),
        verifier_model=args.get("verifier_model"), topology_request=args.get("topology_request") or "auto",
        max_workers=int(args.get("max_workers") or 3), explore=bool(args.get("explore", True)),
        task_id=args.get("task_id"), promote=promote, commit_result=commit_result, assume_yes=assume_yes,
        no_learning=bool(args.get("no_learning", False)), as_json=as_json, resume_from=run_id,
    )


@osn_group.command("runs")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def osn_runs(as_json):
    """List this repository's checkpointed OSN runs and whether each can be resumed."""
    from openshard.cli.ingest import _repo_root
    from openshard.osn import checkpoint as ckpt

    repo_root = _repo_root(None, False)
    rows = []
    for cp in ckpt.list_checkpoints(repo_root):
        verdict = ckpt.check_resumable(cp, repo_root)
        rows.append({
            "run_id": cp.run_id, "status": cp.status, "phase": cp.phase, "attempts_done": cp.attempts_done,
            "updated_at": cp.updated_at, "receipt_id": cp.receipt_id, "resumable": verdict.ok,
            "refusal": verdict.reason, "task": cp.task[:80],
        })
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("No checkpointed OSN runs under .openshard/osn-runs/.")
        return
    for r in rows:
        state = "resumable" if r["resumable"] else f"not resumable ({r['refusal']})"
        click.echo(f"{r['run_id']}  {r['status']}/{r['phase']}  attempts {r['attempts_done']}  {r['updated_at']}  "
                   f"{state}" + (f"  receipt {r['receipt_id']}" if r["receipt_id"] else ""))
        click.echo(f"    {r['task']}")


def _resolve_workers(*, loop_mode, topology_request, max_workers, planner_enabled, verifier_enabled, task,
                     repo_root, executor_model, routing, provider_name, provider_obj, model_policy, budget, argv,
                     role_usage, permissions, progress):
    """The parallel-stage hook for this run, or None when workers can never run.

    The hook decides the topology from the planner's decomposition at run time
    (``openshard.osn.topology``), routes each worker (distinct models when
    routing can offer them), runs them in isolated copies, synthesises their
    files into the run's copy and reports every decision for the Receipt.
    """
    if loop_mode != "agent" or topology_request in ("single", "roles"):
        return None
    if topology_request != "candidates" and not planner_enabled:
        return None  # workers need the planner's decomposition; candidates do not
    from openshard.osn import roles as osn_roles
    from openshard.osn.candidates import (
        candidate_specs,
        candidates_advisory,
        select_candidate,
        verify_candidates,
    )
    from openshard.osn.decompose import decomposition_from_plan
    from openshard.osn.loop import _observe_verification
    from openshard.osn.synthesis import resolution_advisory, synthesize
    from openshard.osn.topology import (
        TOPOLOGY_PARALLEL_CANDIDATES,
        TOPOLOGY_PARALLEL_SUBTASKS,
        decide_topology,
    )
    from openshard.osn.workers import WorkerSpec, run_workers
    from openshard.routing.engine import route
    from openshard.sync.policies import enforce_models_allowed

    task_category = route(task).category
    repo_file_count = _count_repo_files(repo_root)
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

    def verify_in_copy(copy, paths):
        return _observe_verification(copy, paths, argv, 120.0, None)

    def candidates_hook(sandbox, plan, repo_files):
        """The whole task on distinct models at once; the best verified candidate wins."""
        headroom = None if budget is None else budget.would_stop_next_attempt() is None
        requirement_class = getattr(getattr(routing, "decision", None), "resolved_class", None) or "routine_coding"
        chosen: list[tuple[str, str]] = [(executor_model, "executor")]
        while len(chosen) < max_workers:
            choice = osn_roles.select_role_model(
                osn_roles.ROLE_WORKER, explicit=None, executor_model=executor_model, routing=routing,
                provider_name=provider_name, catalog_knows=catalog_knows, requirement_class=requirement_class,
                exclude=tuple(m for m, _ in chosen),
            )
            if not choice.model or choice.model in {m for m, _ in chosen} \
                    or choice.source == osn_roles.SOURCE_EXECUTOR_REUSED:
                break
            chosen.append((choice.model, choice.source))
        decision = decide_topology(
            "candidates", planner_ran=plan is not None, verifier_wanted=verifier_enabled, decomposition=None,
            task_complex=True, budget_headroom=headroom, distinct_models_available=len(chosen),
            max_workers=max_workers,
        )
        record = decision.to_record()
        if decision.selected != TOPOLOGY_PARALLEL_CANDIDATES:
            return {"topology": record, "ran": False}
        try:
            enforce_models_allowed([m for m, _ in chosen if m != executor_model], model_policy)
        except ValueError as exc:
            record["topology_selected"] = "planner_executor_verifier" if verifier_enabled else (
                "planner_executor" if plan is not None else "single")
            record["topology_reason"] = f"candidate_model_not_allowed:{str(exc)[:80]}"
            return {"topology": record, "ran": False}
        specs = candidate_specs(chosen[: decision.worker_count], task, provider_name)
        record["workers"] = [{"worker_id": s.worker_id, "subtask_id": s.subtask.id, "model": s.model,
                              "model_source": s.model_source} for s in specs]
        record["distinct_models"] = len({s.model for s in specs})
        results, usage = run_workers(
            specs, provider=provider_obj, task=task, plan=plan, repo_root=repo_root, base_sandbox=sandbox,
            verify=verify_in_copy, budget=budget, max_workers=max_workers,
            blocked_write_patterns=permissions.blocked_write_paths,
            approval_write_patterns=permissions.approval_write_paths, progress=progress,
        )
        role_usage.extend(usage)
        verify_candidates(results, verify_in_copy)
        winner, evaluation = select_candidate(results)
        record["actual_extra_cost_usd"] = evaluation.get("losers_cost_usd")
        applied: list[str] = []
        synth_record = None
        if winner is not None:
            synth = synthesize([winner], main_sandbox=sandbox, scopes={winner.worker_id: winner_scope(winner)})
            applied = list(synth.applied)
            synth_record = synth.to_record()
        return {
            "topology": record, "ran": True,
            "workers": [r.to_record() for r in results],
            "synthesis": synth_record,
            "candidates": evaluation,
            "applied": applied,
            "blocked": [],
            "decisions": [d for r in results for d in r.decisions],
            "advisory": None if winner is not None else candidates_advisory(evaluation),
        }

    def winner_scope(winner):
        from openshard.osn.candidates import CANDIDATE_SCOPE

        return CANDIDATE_SCOPE

    if topology_request == "candidates":
        return candidates_hook

    def workers_hook(sandbox, plan, repo_files):
        decomposition = decomposition_from_plan(plan)
        headroom = None if budget is None else budget.would_stop_next_attempt() is None
        planner_cost = None
        planner_costs = [u.cost_usd for u in role_usage if getattr(u, "role", "") == osn_roles.ROLE_PLANNER]
        if planner_costs and all(c is not None for c in planner_costs):
            planner_cost = sum(planner_costs)
        candidates: list[osn_roles.RoleModelChoice] = []
        if decomposition is not None and decomposition.valid:
            taken: list[str] = []
            for subtask in decomposition.parallel_subtasks[:max_workers]:
                choice = osn_roles.select_role_model(
                    osn_roles.ROLE_WORKER, explicit=None, executor_model=executor_model, routing=routing,
                    provider_name=provider_name, catalog_knows=catalog_knows,
                    requirement_class=subtask.preferred_capability, exclude=tuple(taken),
                )
                if choice.model:
                    taken.append(choice.model)
                candidates.append(choice)
        distinct = len({c.model for c in candidates if c.model}) or 1
        decision = decide_topology(
            topology_request, planner_ran=plan is not None, verifier_wanted=verifier_enabled,
            decomposition=decomposition, task_complex=(task_category in ("complex", "security") or repo_file_count > 12),
            budget_headroom=headroom, distinct_models_available=distinct, max_workers=max_workers,
            planner_cost_usd=planner_cost,
        )
        record = decision.to_record()
        if decision.selected != TOPOLOGY_PARALLEL_SUBTASKS or decomposition is None:
            return {"topology": record, "ran": False}
        subtasks = decomposition.parallel_subtasks[: decision.worker_count]
        models = [c.model or executor_model for c in candidates[: len(subtasks)]]
        try:
            enforce_models_allowed([m for m in models if m != executor_model], model_policy)
        except ValueError as exc:
            record["topology_selected"] = "planner_executor_verifier" if verifier_enabled else "planner_executor"
            record["topology_reason"] = f"worker_model_not_allowed:{str(exc)[:80]}"
            return {"topology": record, "ran": False}
        specs = [
            WorkerSpec(worker_id=f"worker-{i + 1}", subtask=st, model=models[i],
                       model_source=candidates[i].source if i < len(candidates) else "routing",
                       provider_name=provider_name)
            for i, st in enumerate(subtasks)
        ]
        record["workers"] = [{"worker_id": s.worker_id, "subtask_id": s.subtask.id, "model": s.model,
                              "model_source": s.model_source} for s in specs]
        record["distinct_models"] = len({s.model for s in specs})

        results, usage = run_workers(
            specs, provider=provider_obj, task=task, plan=plan, repo_root=repo_root, base_sandbox=sandbox,
            verify=verify_in_copy, budget=budget, max_workers=max_workers,
            blocked_write_patterns=permissions.blocked_write_paths,
            approval_write_patterns=permissions.approval_write_paths, progress=progress,
        )
        role_usage.extend(usage)
        synth = synthesize(results, main_sandbox=sandbox,
                           scopes={s.worker_id: s.subtask.allowed_write_paths for s in specs})
        costs = [r.cost_usd for r in results]
        record["actual_extra_cost_usd"] = sum(c for c in costs if c is not None) if all(c is not None for c in costs) else None
        return {
            "topology": record, "ran": True,
            "workers": [r.to_record() for r in results],
            "synthesis": synth.to_record(),
            "applied": list(synth.applied),
            "blocked": [r["path"] for r in synth.rejected],
            "decisions": [d for r in results for d in r.decisions],
            "advisory": resolution_advisory(synth, results) if synth.needs_resolution else None,
        }

    return workers_hook


def _parallel_summary(loop: dict | None) -> list[str]:
    """Topology, workers and synthesis after the run, one line each."""
    if not isinstance(loop, dict):
        return []
    out: list[str] = []
    topo = loop.get("topology")
    if isinstance(topo, dict):
        line = f"  topology: {topo.get('topology_selected')} (requested {topo.get('topology_requested')}; {topo.get('topology_reason')})"
        if topo.get("worker_count"):
            line += f" · {topo['worker_count']} worker(s)"
        if isinstance(topo.get("actual_extra_cost_usd"), (int, float)):
            line += f" · extra cost ${topo['actual_extra_cost_usd']:.4f}"
        out.append(line)
    for w in loop.get("workers") or []:
        cost = w.get("cost_usd")
        cost_text = f"${cost:.4f}" if isinstance(cost, (int, float)) else "cost unknown"
        v = (w.get("verification") or {}).get("status")
        out.append(f"  {w.get('worker_id')} [{w.get('subtask_id')}]: {w.get('status')}"
                   + (f" ({w.get('reason')})" if w.get("reason") else "")
                   + f" · {_friendly_model(w.get('model'))} · {w.get('turns')} turn(s) · "
                   f"{len(w.get('changed_files') or [])} file(s) · {cost_text}"
                   + (f" · own-copy verification {v}" if v else ""))
    cands = loop.get("candidates")
    if isinstance(cands, dict):
        out.append(f"  candidates: {cands.get('count')} on {len(set(cands.get('models') or []))} model(s) · "
                   + (f"winner {cands.get('winner')} ({_friendly_model(cands.get('winner_model'))})"
                      if cands.get("winner") else "no candidate verified")
                   + f" · policy {cands.get('policy')}")
        for e in cands.get("evaluated") or []:
            cost = e.get("cost_usd")
            out.append(f"    #{e.get('rank')} {e.get('worker_id')} · {_friendly_model(e.get('model'))} · "
                       f"own-copy verification {e.get('verification') or 'not run'} · {e.get('files_changed')} file(s) · "
                       + (f"${cost:.4f}" if isinstance(cost, (int, float)) else "cost unknown")
                       + (" · selected" if e.get("selected") else ""))
    synth = loop.get("synthesis")
    if isinstance(synth, dict):
        out.append(f"  synthesis: applied {len(synth.get('applied') or [])} file(s), "
                   f"{len(synth.get('conflicts') or [])} conflict(s), {len(synth.get('rejected') or [])} rejected, "
                   f"resolution {synth.get('resolution')}")
    econ = loop.get("economics")
    if isinstance(econ, dict) and econ.get("cost_per_verified_success") is not None:
        out.append(f"  cost per verified success: ${econ['cost_per_verified_success']:.4f}")
    return out


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
    workers = [w for w in loop.get("workers") or [] if isinstance(w, dict)]
    if workers:
        w_turns = sum(int(w.get("turns") or 0) for w in workers)
        w_actions = sum(len(w.get("actions") or []) for w in workers)
        parts.append(f"workers {w_turns} turn(s) · {w_actions} action(s)")
    calls = loop.get("model_calls") or []
    if calls:
        from openshard.history.run_cost import run_total_cost

        cost, complete = run_total_cost(entry)  # first attempt plus every retry, or None when any is unknown
        if cost is not None and not complete:
            cost = None
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


_COMMIT_TITLE_CAP = 72


def _commit_title(task: str, entry: dict) -> str:
    """A commit title from what the run recorded: the executor's final note, else the plan summary,
    else the task's first sentence, cut at a word boundary."""
    loop: dict = entry["osn_loop"] if isinstance(entry.get("osn_loop"), dict) else {}
    candidates: list[str] = []
    for attempt in reversed(loop.get("attempts") or []):
        note = attempt.get("final_note") if isinstance(attempt, dict) else None
        if isinstance(note, str) and note.strip():
            candidates.append(note)
            break
    plan: dict = loop["plan"] if isinstance(loop.get("plan"), dict) else {}
    if isinstance(plan.get("summary"), str) and plan["summary"].strip():
        candidates.append(plan["summary"])
    candidates.append(task)
    for text in candidates:
        clean = " ".join(text.split())
        first = clean.split(". ")[0].rstrip(".")
        if len(first) <= _COMMIT_TITLE_CAP and first:
            return first
        words = first.split()
        out: list[str] = []
        for w in words:
            if len(" ".join([*out, w])) > _COMMIT_TITLE_CAP - 1:
                break
            out.append(w)
        if out:
            return " ".join(out).rstrip(",;:(`'\"") + "…"
    return "OSN change"


def _commit_promoted(repo_root: Path, files: list[str], task: str, entry: dict) -> dict:
    """Commit exactly the promoted *files* on the current branch. Never raises.

    Returns ``{"sha", "branch", "files", "reason"}``: ``sha`` is None with a
    reason when nothing was committed (nothing staged, git identity missing,
    a hook refused). Only the promoted paths are staged, so unrelated local
    changes are never swept into the commit.
    """
    from openshard.util.git import run_git

    title = _commit_title(task, entry)
    receipt_id = entry.get("receipt_id") or ""
    task_text = " ".join(task.split())
    message = (
        f"{title}\n\nTask: {task_text}\n\n"
        f"Made by Openshard Native (OSN) from an isolated, verified copy.\nReceipt: {receipt_id}\n"
    )
    branch = (run_git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"]) or "").strip() or None
    record: dict = {"sha": None, "branch": branch, "files": list(files), "reason": None}
    if run_git(repo_root, ["add", "--", *files]) is None:
        record["reason"] = "git_add_failed"
        return record
    staged = run_git(repo_root, ["diff", "--cached", "--name-only"])
    if staged is None or not staged.strip():
        record["reason"] = "nothing_staged"
        return record
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "commit", "-q", "-F", "-"], cwd=str(repo_root), input=message, text=True,
            capture_output=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        record["reason"] = f"git_commit_failed:{type(exc).__name__}"
        return record
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        record["reason"] = "git_commit_refused:" + (tail[-1][:120] if tail else f"exit {proc.returncode}")
        return record
    sha = (run_git(repo_root, ["rev-parse", "HEAD"]) or "").strip().lower()
    record["sha"] = sha if len(sha) >= 40 else None
    if record["sha"] is None:
        record["reason"] = "head_unreadable_after_commit"
    return record


def _attach_commit(entry: dict, commit_record: dict | None) -> None:
    """Record the commit OpenShard created on the Receipt, in the shape capture uses for git-observed commits."""
    if not commit_record or not commit_record.get("sha"):
        if commit_record:
            entry["osn_commit"] = {"attempted": True, "sha": None, "reason": commit_record.get("reason")}
        return
    sha = commit_record["sha"]
    entry["git_end_head"] = sha
    entry["session_commits"] = {"source": "git_observed", "shas": [sha], "truncated": False}
    entry["osn_commit"] = {
        "attempted": True, "sha": sha, "branch": commit_record.get("branch"),
        "files": list(commit_record.get("files") or []), "source": "openshard_committed",
    }


def _bind_verification(repo_root: Path, entry: dict, argv: list[str], *, as_json: bool) -> dict:
    """Run the run's verify command on the committed tree and record it as evidence bound to that commit.

    Uses the post-session verification path (``openshard verify``): the check
    is OpenShard-executed, ``directly_observed``, and bound to HEAD only when
    the tree was clean before and after. Nothing is claimed otherwise.
    """
    from openshard.cli.main import _utc_stamp
    from openshard.history.verification import CHECK_FAILED, CHECK_PASSED, CHECK_UNKNOWN
    from openshard.osn.loop import _run_verification
    from openshard.verification.plan import VerificationSource
    from openshard.verification.post_session import (
        CheckRun,
        _planned,
        build_attestation,
        record_attestation,
        summarize_attestation,
        tree_state,
    )

    # The run's --verify-cmd is an explicit user command that OpenShard already
    # executed in the isolated copy (organisation command policy was applied at
    # run start); it is executed here the same way, never through a shell. The
    # generic post-session classifier is not re-applied to it.
    planned = _planned(list(argv), "osn_run", VerificationSource.user)
    started = _utc_stamp()
    before = tree_state(repo_root)
    t0 = time.monotonic()
    observed, _output = _run_verification(list(argv), repo_root, 600.0)
    duration = round(time.monotonic() - t0, 2)
    if observed.timed_out:
        results = [CheckRun(planned, CHECK_UNKNOWN, duration_seconds=duration, note="timed out; no exit code")]
    elif not observed.ran:
        results = [CheckRun(planned, CHECK_UNKNOWN, duration_seconds=duration, note="could not start")]
    else:
        results = [CheckRun(planned, CHECK_PASSED if observed.passed else CHECK_FAILED,
                            exit_code=observed.exit_code, duration_seconds=duration)]
    after = tree_state(repo_root)
    attestation = build_attestation(entry, results, before=before, after=after, started_at=started,
                                    completed_at=_utc_stamp())
    record_attestation(repo_root, attestation)
    summary = summarize_attestation(attestation)
    verification = summary.get("verification") if isinstance(summary, dict) else {}
    verification = verification if isinstance(verification, dict) else {}
    sha = verification.get("artifact_sha")
    reason = None
    if not sha:
        if before.head is None:
            reason = "no_git_head"
        elif before.dirty or (after.tracked_dirty if after.tracked_dirty is not None else after.dirty):
            reason = "working_tree_not_clean"
        elif after.head != before.head:
            reason = "head_moved_during_verification"
        else:
            reason = "not_bound"
    return {
        "attestation_id": attestation.get("attestation_id"),
        "status": verification.get("status"),
        "source": verification.get("source"),
        "artifact_sha": sha,
        "bound": bool(sha),
        "reason": reason,
        "checks": [{"name": r.check.name, "status": r.status, "exit_code": r.exit_code} for r in results],
    }


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
