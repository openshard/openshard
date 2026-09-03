import datetime
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import click

from openshard import __version__
from openshard.cli.run_output import (
    _PUBLIC_MODE_LABEL,
    _RATIONALE_SHORT,
    _model_label,
    _native_meta_from_entry,
    _profile_display_label,
    _render_native_inspection,
    _truncate_note,
)
from openshard.cli.run_output import (
    _build_model_line as _build_model_line,
)
from openshard.cli.run_output import (
    _build_routing_line as _build_routing_line,
)
from openshard.cli.run_output import (
    _exec_message as _exec_message,
)
from openshard.cli.run_output import (
    _format_model_slug as _format_model_slug,
)
from openshard.cli.run_output import (
    _print_dry_run as _print_dry_run,
)
from openshard.cli.run_output import (
    _print_shrunk as _print_shrunk,
)
from openshard.cli.run_output import (
    _print_summary as _print_summary,
)
from openshard.cli.run_output import (
    _render_repo_map as _render_repo_map,
)
from openshard.cli.run_output import (
    _render_repo_plan as _render_repo_plan,
)
from openshard.cli.run_output import (
    _render_repo_summary as _render_repo_summary,
)
from openshard.cli.run_output import (
    _should_shrink as _should_shrink,
)
from openshard.cli.run_output import (
    _Spinner as _Spinner,
)
from openshard.config.settings import (
    find_config_path,
    get_anthropic_api_key,
    get_api_key,
    get_onboarding,
    get_openai_api_key,
    is_agent_environment,
    load_config,
    load_config_safe,
    save_config,
)
from openshard.evals.registry import load_eval_tasks
from openshard.evals.runner import append_eval_result, run_eval_task
from openshard.history.jsonl_store import write_jsonl
from openshard.history.sandbox_apply_receipts import (
    SandboxApplyReceipt,
    log_sandbox_apply_receipt,
    recent_sandbox_apply_receipts,
)
from openshard.planning.generator import PlanGenerator
from openshard.providers.base import ProviderAuthError, ProviderError, ProviderRateLimitError
from openshard.run.pipeline import (
    _LOG_PATH,
    RunPipeline,
    _copy_cwd_to_workspace,
)
from openshard.run.pipeline import (
    _build_retry_prompt as _build_retry_prompt,
)
from openshard.run.pipeline import (
    _detect_command as _detect_command,
)
from openshard.run.pipeline import (
    _log_run as _log_run,
)
from openshard.run.pipeline import (
    _parse_cost_hint as _parse_cost_hint,
)
from openshard.run.pipeline import (
    _pre_run_cost_hint as _pre_run_cost_hint,
)
from openshard.run.pipeline import (
    _run_verification as _run_verification,
)
from openshard.run.pipeline import (
    _run_verification_plan as _run_verification_plan,
)
from openshard.run.pipeline import (
    _suggest_executor as _suggest_executor,
)
from openshard.run.pipeline import (
    _write_files as _write_files,
)
from openshard.run.pipeline import (
    confirm_or_abort as confirm_or_abort,
)


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="openshard")
@click.pass_context
def cli(ctx: click.Context):
    """OpenShard - intelligent task routing and execution."""
    if ctx.invoked_subcommand is None:
        from openshard.cli.ui.onboarding import _should_run_onboarding, run_onboarding_flow

        if _should_run_onboarding():
            run_onboarding_flow()

        from openshard.cli.ui.home import render_home

        render_home()


@cli.command()
@click.argument("task")
def plan(task: str):
    """Analyse TASK and produce a structured execution plan."""
    try:
        generator = PlanGenerator()
    except ValueError as exc:
        raise click.ClickException(str(exc))

    try:
        result = generator.generate(task)
    except ProviderAuthError:
        raise click.ClickException(
            "Authentication failed. Check that your provider API key is valid."
        )
    except ProviderRateLimitError:
        raise click.ClickException("Rate limit exceeded. Wait a moment then try again.")
    except ProviderError as exc:
        raise click.ClickException(f"API error: {exc}")

    click.echo(f"\nTask: {task}\n")
    click.echo(f"Summary: {result.summary}\n")
    for i, stage in enumerate(result.stages, 1):
        click.echo(f"Stage {i}: {stage.name} [{stage.tier}]")
        click.echo(f"  {stage.reasoning}")


@cli.command()
@click.argument("task")
@click.option("--write", is_flag=True, default=False, help="Write generated files to disk.")
@click.option("--verify", is_flag=True, default=False, help="Run verification after writing (requires --write).")
@click.option("--dry-run", is_flag=True, default=False, help="Preview files without writing.")
@click.option("--more", is_flag=True, default=False, help="Show file list, retry info, model names, and token breakdown.")
@click.option("--full", is_flag=True, default=False, help="Show all details: workspace, verification command, retry prompt, raw output.")
@click.option("--no-shrink", is_flag=True, default=False, hidden=True, help="Disable output shrinking for long results.")
@click.option(
    "--workflow",
    type=click.Choice(
        ["auto", "direct", "staged", "native", "opencode", "claude-code", "codex"],
        case_sensitive=False,
    ),
    default=None,
    help=(
        "Execution workflow: auto (default, policy-driven), direct (single-pass API call), "
        "staged (planning then implementation), native (native agent), "
        "opencode (OpenCode CLI)."
    ),
)
@click.option(
    "--profile",
    type=click.Choice(["native_light", "native_deep", "native_swarm"], case_sensitive=False),
    default=None,
    hidden=True,
    help="Execution profile: native_light (fast/simple), native_deep (thorough/complex), native_swarm (experimental, never auto-selected).",
)
@click.option(
    "--executor",
    type=click.Choice(["direct", "opencode"], case_sensitive=False),
    default=None,
    hidden=True,
    help="[DEPRECATED] Use --workflow instead. Execution backend: direct or opencode.",
)
@click.option(
    "--native-backend",
    "native_backend",
    type=click.Choice(["builtin", "deepagents"], case_sensitive=False),
    default=None,
    hidden=True,
    help="Native workflow backend (default: builtin). deepagents is experimental/stub only. Ignored for non-native workflows.",
)
@click.option(
    "--experimental-deepagents-run",
    "experimental_deepagents_run",
    is_flag=True,
    default=False,
    hidden=True,
    help="Invoke a minimal read-only DeepAgents agent as a proof step. Requires --native-backend deepagents. No write or shell tools are provided.",
)
@click.option(
    "--experimental-tier-dispatch",
    "experimental_tier_dispatch",
    is_flag=True,
    default=False,
    hidden=True,
    help="[Experimental] Resolve routing tier names to model IDs and use them during execution. Recorded in run log; shown at --more/--full.",
)
@click.option(
    "--native-loop",
    "native_loop",
    type=click.Choice(["experimental"], case_sensitive=False),
    default=None,
    hidden=True,
    help="Enable experimental bounded native loop. Requires --workflow native. Runs additional deterministic read-only tool steps before generation.",
)
@click.option("--plan", "plan_flag", is_flag=True, default=False, help="Show execution plan and prompt for approval before running.")
@click.option(
    "--approval",
    type=click.Choice(["auto", "smart", "ask"], case_sensitive=False),
    default=None,
    help="Override config approval_mode for this run: auto (silent), smart (prompt on risk), ask (always prompt).",
)
@click.option(
    "--provider",
    type=click.Choice(["openrouter", "anthropic", "openai"], case_sensitive=False),
    default=None,
    help="API provider: openrouter (default), anthropic (requires ANTHROPIC_API_KEY), or openai (requires OPENAI_API_KEY).",
)
@click.option("--history-scoring", "history_scoring", is_flag=True, default=False, hidden=True, help="Apply run-history bonuses/penalties to model scoring (opt-in).")
@click.option("--eval-scoring", "eval_scoring", is_flag=True, default=False, hidden=True, help="Apply eval-run bonuses/penalties to model scoring (opt-in).")
@click.option("--feedback-scoring", "feedback_scoring", is_flag=True, default=False, hidden=True, help="Apply developer-feedback bonuses/penalties to model scoring (opt-in).")
@click.option(
    "--model-policy",
    "model_policy",
    type=click.Choice(["auto", "cheapest-safe", "frontier-heavy", "open-source-only", "local-only", "custom"], case_sensitive=False),
    default=None,
    hidden=True,
    help="Model selection policy mode (metadata-only v1): auto, cheapest-safe, frontier-heavy, open-source-only, local-only, custom.",
)
@click.option(
    "--candidates",
    default=1,
    type=click.IntRange(1, 3),
    hidden=True,
    help="Run multiple native candidate agents and select the best verified result (1–3, native --write only).",
)
@click.option(
    "--shard",
    "shard_id",
    default=None,
    help=(
        "Attach this run as another attempt on an existing Shard, rather than "
        "starting a new one. Must reference a Shard ID from a previous run "
        "(see 'openshard shard attempts' or a prior receipt's Shard ID)."
    ),
)
def run(task: str, write: bool, verify: bool, dry_run: bool, more: bool, full: bool, no_shrink: bool, workflow: str | None, profile: str | None, executor: str | None, native_backend: str | None, experimental_deepagents_run: bool, experimental_tier_dispatch: bool, native_loop: str | None, plan_flag: bool, approval: str | None, provider: str | None, history_scoring: bool, eval_scoring: bool, feedback_scoring: bool, model_policy: str | None, candidates: int, shard_id: str | None):
    """Execute TASK and return a structured result."""
    if native_loop is not None and workflow != "native":
        raise click.UsageError("--native-loop experimental requires --workflow native")
    try:
        config = load_config()
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc))
    if config.get("output_mode") == "agent_json":
        click.echo("[openshard] agent mode: JSON output enabled", err=True)
    detail = "full" if full else ("more" if more else "default")
    pipeline = RunPipeline(
        config,
        write=write,
        verify=verify,
        dry_run=dry_run,
        no_shrink=no_shrink,
        workflow=workflow,
        profile=profile,
        executor=executor,
        plan_flag=plan_flag,
        approval=approval,
        provider=provider,
        history_scoring=history_scoring,
        eval_scoring=eval_scoring,
        feedback_scoring=feedback_scoring,
        detail=detail,
        native_backend=native_backend,
        experimental_deepagents_run=experimental_deepagents_run,
        experimental_tier_dispatch=experimental_tier_dispatch,
        native_loop=native_loop,
        model_policy=model_policy,
        candidates=candidates,
        shard_id=shard_id,
    )
    from openshard.history.run_attempt import UnknownShardError
    try:
        result = pipeline.run(task)
    except UnknownShardError as exc:
        raise click.UsageError(str(exc))
    if result.exit_code != 0:
        sys.exit(result.exit_code)


@cli.command("env")
def env_cmd() -> None:
    """Show a short environment summary: agent mode, output mode, and API key status."""
    _AGENT_VARS = (
        "OPENSHARD_AGENT",
        "CI",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "NO_COLOR",
    )

    # Determine which env var triggered agent mode (first match wins).
    triggered_by: str | None = None
    for _v in _AGENT_VARS:
        if os.environ.get(_v, ""):
            triggered_by = _v
            break

    agent_active = is_agent_environment()
    agent_label = "yes" if agent_active else "no"
    if agent_active and triggered_by:
        agent_display = f"{agent_label}   ({triggered_by}={os.environ.get(triggered_by, '')})"
    else:
        agent_display = agent_label

    try:
        config = load_config()
    except Exception:  # noqa: BLE001
        config = {}

    output_mode = config.get("output_mode", "human")

    # Determine API key source without revealing the value.
    _KEY_VARS = (
        ("OPENROUTER_API_KEY", "openrouter"),
        ("ANTHROPIC_API_KEY", "anthropic"),
        ("OPENAI_API_KEY", "openai"),
    )
    key_label = "not set"
    for _env_var, _provider_name in _KEY_VARS:
        _env_val = os.environ.get(_env_var, "")
        if _env_val:
            key_label = f"{_provider_name} (from env)"
            break
    else:
        # Check whether the config dict carries a key (injected from env by load_config).
        for _cfg_key, _provider_name in (
            ("openrouter_api_key", "openrouter"),
            ("anthropic_api_key", "anthropic"),
            ("openai_api_key", "openai"),
        ):
            if config.get(_cfg_key):
                key_label = f"{_provider_name} (from config)"
                break

    click.echo("Environment")
    click.echo(f"  Agent mode:    {agent_display}")
    click.echo(f"  Output mode:   {output_mode}")
    click.echo(f"  API key:       {key_label}")


@cli.command("setup")
@click.option("--agent", "as_agent", is_flag=True, default=False,
              help="Read-only machine-readable status; makes no changes (for agent/CI use).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Run setup and print the result as JSON (implied by --agent).")
@click.option("--yes", "-y", "assume_yes", is_flag=True, default=False,
              help="Non-interactive: skip the provider onboarding wizard even in a TTY.")
@click.option(
    "--repo-path",
    "repo_path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository to configure Claude Code for (default: current directory).",
)
def setup_cmd(as_agent: bool, as_json: bool, assume_yes: bool, repo_path: Path | None) -> None:
    """Set up OpenShard for this repository: the one command a new user needs.

    Detects the environment, configures Claude Code capture for this
    repository (MCP server, auto-capture hooks, and status-line receipt
    enrichment where safe -- reusing the same installers as `openshard mcp
    install claude`), and reports whether it is ready to use. Safe to re-run:
    already-configured components are left untouched. Use --agent for a
    read-only status snapshot with no side effects (CI/agent discovery).
    """
    from openshard.config import onboarding as ob

    if as_agent:
        # Machine-readable path: no interactive UI, no configuration writes.
        state = _current_state()
        keys_present = ob.api_key_present()
        detected_providers = [p for p, present in keys_present.items() if present]
        # Clearer, additive fields. provider_route/provider are kept for back-compat.
        selected_route = state.get("provider_route") or "demo"
        selected_provider = state.get("provider") or "none"
        available_routes = [*detected_providers, "demo"]
        # Surface a next action when a provider key exists but the route is demo/skip.
        recommended_next_action = None
        if detected_providers and selected_route in ("demo", "skip"):
            recommended_next_action = (
                f"Run `openshard setup` to switch from demo to {detected_providers[0]}."
            )
        from openshard.adapters.claude_mcp_install import find_repo_root
        from openshard.adapters.claude_setup import detect_claude_integration

        claude_status = detect_claude_integration(find_repo_root(repo_path))
        payload = {
            "mode": "agent",
            "interactive": False,
            "repo_detected": ob.detect_git_repo(),
            "config_found": state["config_found"],
            "onboarding_completed": not ob.is_first_run(),
            "recommended_executor": state.get("executor") or "native",
            "detected_providers": detected_providers,
            "available_routes": available_routes,
            "selected_route": selected_route,
            "selected_provider": selected_provider,
            "recommended_next_action": recommended_next_action,
            "provider_route": state.get("provider_route"),
            "provider": state.get("provider"),
            "safety_profile": state.get("safety_profile"),
            "receipts": {
                "enabled": True,
                "storage": ".openshard/",
            },
            "claude_code": claude_status.to_dict(),
            "next_actions": [
                "openshard env --json",
                'openshard run "explain this repo"',
                "openshard last --json",
            ],
        }
        click.echo(json.dumps(payload, indent=2))
        return

    # Real setup: offer the provider onboarding wizard first (unchanged UX),
    # then configure Claude Code capture -- this is the part PR8 adds so a
    # new user never needs to discover `mcp install claude` on their own.
    from openshard.cli.ui.onboarding import _should_run_onboarding, run_onboarding_flow

    if not assume_yes and _should_run_onboarding():
        run_onboarding_flow()

    from openshard.adapters.claude_setup import run_setup

    result = run_setup(repo_path=repo_path)

    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2))
        if result.readiness == "not_ready":
            raise SystemExit(1)
        return

    _render_setup_result(result)
    if result.readiness == "not_ready":
        raise SystemExit(1)


def _render_setup_result(result) -> None:
    """Human-readable rendering of a claude_setup.SetupResult for `openshard setup`."""
    click.echo("\nOpenShard Setup\n")
    click.echo(f"  Repository:    {'git repository' if result.is_git else 'not a git repository'}")
    if result.is_git:
        cli_label = "detected" if result.claude_cli.available else "not found"
        click.echo(f"  Claude Code:   {cli_label}")
    from openshard.adapters.claude_setup import HISTORY_RELPATH

    history_label = "writable" if result.history_writable else "not writable"
    click.echo(f"  Local history: {history_label} ({HISTORY_RELPATH.as_posix()})")

    if result.mcp is not None:
        mcp_state = {
            "installed": "installed", "updated": "updated", "already_installed": "already configured",
        }.get(result.mcp.status, result.mcp.status)
        click.echo(f"  MCP:           {mcp_state}")
    if result.hooks is not None:
        hooks_state = {
            "installed": "installed", "updated": "updated", "already_installed": "already configured",
        }.get(result.hooks.status, result.hooks.status)
        click.echo(f"  Auto-capture:  {hooks_state}")
    if result.statusline is not None:
        statusline_state = {
            "installed": "installed", "already_installed": "already configured",
            "skipped_existing": "skipped (custom status line present)",
        }.get(result.statusline.status, result.statusline.status)
        click.echo(f"  Enrichment:    {statusline_state}")

    click.echo("")
    if result.readiness == "ready":
        click.echo("OpenShard is ready. Use Claude Code normally.")
        click.echo("\nNext steps:")
        click.echo("  1. Open Claude Code in this repository.")
        click.echo("  2. Complete a normal coding task.")
        click.echo("  3. Run `openshard last` to see the captured Shard receipt.")
        for step in result.next_steps:
            click.echo(f"  ! {step}")
    elif result.readiness == "ready_partial":
        click.echo("OpenShard is ready, with one limitation: model/cost/token capture is unavailable.")
        click.echo("Use Claude Code normally -- Shards and receipts are still recorded.")
        click.echo("\nNext steps:")
        click.echo("  1. Open Claude Code in this repository.")
        click.echo("  2. Complete a normal coding task.")
        click.echo("  3. Run `openshard last` to see the captured Shard receipt.")
        for step in result.next_steps:
            click.echo(f"  ! {step}")
    else:
        click.echo("OpenShard setup is incomplete.")
        click.echo("\nNext step:")
        for step in result.next_steps:
            click.echo(f"  - {step}")


@cli.command()
@click.argument("task")
def explain(task: str):
    """Explain how OpenShard would approach TASK and which model strengths apply."""
    try:
        generator = PlanGenerator()
    except ValueError as exc:
        raise click.ClickException(str(exc))

    try:
        result = generator.generate(task)
    except ProviderAuthError:
        raise click.ClickException(
            "Authentication failed. Check that your provider API key is valid."
        )
    except ProviderRateLimitError:
        raise click.ClickException("Rate limit exceeded. Wait a moment then try again.")
    except ProviderError as exc:
        raise click.ClickException(f"API error: {exc}")

    strong = [s for s in result.stages if s.tier == "strong"]
    medium = [s for s in result.stages if s.tier == "medium"]
    cheap  = [s for s in result.stages if s.tier == "cheap"]

    click.echo(f"\nTask: {task}\n")
    click.echo(f"Summary: {result.summary}\n")

    if strong:
        click.echo("Hard parts (strong model):")
        for s in strong:
            click.echo(f"  - {s.name}: {s.reasoning}")
        click.echo("")

    if medium:
        click.echo("Standard parts:")
        for s in medium:
            click.echo(f"  - {s.name}: {s.reasoning}")
        click.echo("")

    if cheap:
        click.echo("Low-risk parts (cheap model):")
        for s in cheap:
            click.echo(f"  - {s.name}: {s.reasoning}")
        click.echo("")

    click.echo("Retry / fix:")
    if strong:
        click.echo("  Complex task - fixer would benefit from a stronger model.")
    else:
        click.echo("  Straightforward task - default fixer model should be sufficient.")


@cli.group(invoke_without_command=True)
@click.pass_context
@click.option(
    "--provider",
    type=click.Choice(["openrouter", "anthropic", "openai"], case_sensitive=False),
    default=None,
    help="Filter by provider. Omit to show all.",
)
@click.option("--refresh", is_flag=True, default=False, help="Fetch models from provider API and update cache.")
def models(ctx: click.Context, provider: str | None, refresh: bool):
    """List cached models and capabilities, or run a subcommand."""
    if ctx.invoked_subcommand is not None:
        return
    if provider is None:
        from openshard.providers.manager import ProviderManager
        manager = ProviderManager()
        if not manager.providers:
            click.echo("No providers configured. Set at least one API key.")
            return
        inventory = manager.get_inventory(refresh=refresh)
        if refresh:
            from collections import Counter
            counts = Counter(e.provider for e in inventory.models)
            for pname, count in sorted(counts.items()):
                click.echo(f"  {pname}: {count} models cached")
            return
        header = f"{'Provider':<12}  {'Model ID':<50}  {'Context':>9}  {'MaxOut':>7}  {'Vision':<6}  {'Tools':<5}"
        click.echo(header)
        click.echo("-" * len(header))
        for entry in inventory.models:
            m = entry.model
            ctx2 = str(m.context_window or "-")
            out = str(m.max_output_tokens or "-")
            vis = "yes" if m.supports_vision else "no"
            tls = "yes" if m.supports_tools else "no"
            click.echo(f"{entry.provider:<12}  {m.id:<50}  {ctx2:>9}  {out:>7}  {vis:<6}  {tls:<5}")
        return

    from openshard.providers.cache import (
        CACHE_TTL_HOURS,
        build_cache_entry,
        is_stale,
        load_cache,
        save_cache,
    )

    _all_providers = ["openrouter", "anthropic", "openai"]
    targets = [provider.lower()] if provider else _all_providers

    if refresh:
        cache = load_cache() or {"cached_at": 0.0, "models": {}}
        for pname in targets:
            try:
                if pname == "openrouter":
                    from openshard.providers.openrouter import OpenRouterClient
                    client = OpenRouterClient(get_api_key())
                elif pname == "anthropic":
                    from openshard.providers.anthropic import AnthropicProvider
                    client = AnthropicProvider(get_anthropic_api_key())  # type: ignore[assignment]  # client varies by provider branch; all share list_models()
                else:
                    from openshard.providers.openai import OpenAIProvider
                    client = OpenAIProvider(get_openai_api_key())  # type: ignore[assignment]  # client varies by provider branch; all share list_models()
                model_list = client.list_models()
                cache["models"].update(build_cache_entry(pname, model_list))
                click.echo(f"  {pname}: {len(model_list)} models cached")
            except (ValueError, ProviderAuthError, ProviderError) as exc:
                click.echo(f"  {pname}: skipped ({exc})")
        cache["cached_at"] = time.time()
        save_cache(cache)
        return

    cache = load_cache()  # type: ignore[assignment]  # load_cache() returns dict | None; None case handled immediately below
    if cache is None:
        click.echo("No model cache found. Run 'openshard models --refresh' to populate it.")
        return
    if is_stale(cache.get("cached_at", 0.0)):
        click.echo(f"Cache is older than {CACHE_TTL_HOURS}h. Run 'openshard models --refresh' to update.")
        return

    header = f"{'Provider':<12}  {'Model ID':<50}  {'Context':>9}  {'MaxOut':>7}  {'Vision':<6}  {'Tools':<5}"
    click.echo(header)
    click.echo("-" * len(header))
    for pname in targets:
        for m in cache.get("models", {}).get(pname, []):
            ctx2 = str(m.get("context_window") or "-")
            out = str(m.get("max_output_tokens") or "-")
            vis = "yes" if m.get("supports_vision") else "no"
            tls = "yes" if m.get("supports_tools") else "no"
            click.echo(f"{pname:<12}  {m['id']:<50}  {ctx2:>9}  {out:>7}  {vis:<6}  {tls:<5}")


@models.command("stats")
def models_stats():
    """Show per-model performance stats from run history."""
    from openshard.history.metrics import compute_model_stats, load_runs

    runs = load_runs()
    if not runs:
        log_path = Path.cwd() / _LOG_PATH
        if not log_path.exists():
            click.echo("No run history found. Run 'openshard run' to get started.")
        else:
            click.echo("No runs recorded yet.")
        return

    stats = compute_model_stats(runs)
    if not stats:
        click.echo("No model data in run history.")
        return

    total_runs = len(runs)
    model_count = len(stats)
    click.echo(f"[model stats]  {model_count} model{'s' if model_count != 1 else ''}  (from {total_runs} run{'s' if total_runs != 1 else ''})\n")

    col_model = 48
    header = (
        f"  {'model':<{col_model}}  {'runs':>5}  {'avg cost':>9}  {'avg dur':>8}  {'pass rate':>9}  {'retry':>6}"
    )
    click.echo(header)
    click.echo("  " + "-" * (len(header) - 2))

    for model_id, s in list(stats.items())[:10]:
        runs_n = s["runs_count"]
        avg_cost = f"${s['avg_cost']:.4f}" if s["avg_cost"] is not None else "-"
        avg_dur = f"{s['avg_duration']:.1f}s" if s["avg_duration"] is not None else "-"
        pass_rate = f"{s['verification_pass_rate']:.0%}" if s["verification_pass_rate"] is not None else "-"
        retry = f"{s['retry_rate']:.0%}"
        mid = model_id if len(model_id) <= col_model else model_id[: col_model - 1] + "..."
        click.echo(f"  {mid:<{col_model}}  {runs_n:>5}  {avg_cost:>9}  {avg_dur:>8}  {pass_rate:>9}  {retry:>6}")


# ---------------------------------------------------------------------------
# Registry inspection helpers and commands.
# ---------------------------------------------------------------------------

_COL_ID = 45
_COL_PROV = 12
_COL_TIER = 17
_COL_COST = 10
_COL_EXP_R = 13


def _print_registry_table(entries: list) -> None:
    header = (
        f"  {'ID':<{_COL_ID}}  {'Provider':<{_COL_PROV}}  {'Tier':<{_COL_TIER}}  Experimental"
    )
    click.echo(header)
    click.echo("  " + "-" * (len(header) - 2))
    for entry in entries:
        exp = "yes" if entry.experimental else "no"
        mid = entry.id if len(entry.id) <= _COL_ID else entry.id[: _COL_ID - 1] + "..."
        click.echo(f"  {mid:<{_COL_ID}}  {entry.provider:<{_COL_PROV}}  {entry.tier:<{_COL_TIER}}  {exp}")


@models.command("list")
def models_list():
    """List all registered models."""
    from openshard.models.registry import all_models

    _print_registry_table(all_models())


@models.command("show")
@click.argument("model_id")
def models_show(model_id: str):
    """Show full details for a model."""
    from openshard.models.registry import get_model

    entry = get_model(model_id)
    if entry is None:
        raise click.ClickException(f"Model not found: {model_id}")
    w = 12
    roles_str = ", ".join(entry.roles) if entry.roles else "-"
    ctx_str = str(entry.context_length) if entry.context_length is not None else "-"
    click.echo(f"  {'Model':<{w}}  {entry.display_name}")
    click.echo(f"  {'ID':<{w}}  {entry.id}")
    click.echo(f"  {'Provider':<{w}}  {entry.provider}")
    click.echo(f"  {'Tier':<{w}}  {entry.tier}")
    click.echo(f"  {'Roles':<{w}}  {roles_str}")
    click.echo(f"  {'Experimental':<{w}}  {'yes' if entry.experimental else 'no'}")
    click.echo(f"  {'Context':<{w}}  {ctx_str}")
    click.echo(f"  {'Tools':<{w}}  {'yes' if entry.supports_tools else 'no'}")
    click.echo(f"  {'Structured':<{w}}  {'yes' if entry.supports_structured_outputs else 'no'}")
    click.echo(f"  {'Reasoning':<{w}}  {'yes' if entry.supports_reasoning else 'no'}")
    click.echo(f"  {'Multimodal':<{w}}  {'yes' if entry.supports_multimodal else 'no'}")
    click.echo(f"  {'Latency':<{w}}  {entry.latency_class}")
    click.echo(f"  {'Cost class':<{w}}  {entry.cost_class}")


@models.command("role")
@click.argument("role")
def models_role(role: str):
    """List models that have the given role."""
    from openshard.models.registry import models_by_role

    entries = models_by_role(role)
    if not entries:
        click.echo(f"No models found for role: {role}")
        return
    _print_registry_table(entries)


_VALID_CAPABILITIES = ("tools", "structured_outputs", "reasoning", "multimodal")


@models.command("capabilities")
@click.argument("capability")
def models_capabilities(capability: str):
    """List models that support a capability (tools, structured_outputs, reasoning, multimodal)."""
    from openshard.models.registry import models_by_capability

    if capability not in _VALID_CAPABILITIES:
        click.echo(f"Unknown capability: {capability}")
        click.echo(f"Accepted: {', '.join(_VALID_CAPABILITIES)}")
        return
    entries = models_by_capability(capability)
    if not entries:
        click.echo(f"No models found for capability: {capability}")
        return
    _print_registry_table(entries)


@models.command("experimental")
def models_experimental():
    """List all experimental models."""
    from openshard.models.registry import all_models

    entries = [e for e in all_models() if e.experimental]
    if not entries:
        click.echo("No experimental models registered.")
        return
    _print_registry_table(entries)


_VALID_COST_CLASSES = ("free", "tiny", "cheap", "mid", "expensive")


@models.command("recommend")
@click.option("--role", default=None, help="Filter/score by role name.")
@click.option(
    "--risk",
    type=click.Choice(["low", "medium", "high"], case_sensitive=False),
    default=None,
    help="Risk level hint.",
)
@click.option(
    "--capability",
    "capabilities",
    multiple=True,
    help="Required capability (tools, structured_outputs, reasoning, multimodal). Repeatable.",
)
@click.option(
    "--max-cost",
    "max_cost_class",
    type=click.Choice(_VALID_COST_CLASSES, case_sensitive=False),
    default=None,
    help="Maximum cost class.",
)
@click.option("--include-experimental", "include_experimental", is_flag=True, default=False)
@click.option("--limit", default=5, show_default=True, type=click.IntRange(min=1))
def models_recommend(
    role: str | None,
    risk: str | None,
    capabilities: tuple[str, ...],
    max_cost_class: str | None,
    include_experimental: bool,
    limit: int,
) -> None:
    """Recommend advisory models for a use case (does not change routing)."""
    from openshard.models.advisory import recommend_models
    from openshard.models.registry import CAPABILITY_NAMES

    unknown = [c for c in capabilities if c not in CAPABILITY_NAMES]
    if unknown:
        click.echo(
            f"No results: unknown capability '{unknown[0]}'. "
            f"Accepted: {', '.join(sorted(CAPABILITY_NAMES))}"
        )
        return

    results = recommend_models(
        role=role,
        risk=risk,
        required_capabilities=tuple(capabilities),
        max_cost_class=max_cost_class,
        include_experimental=include_experimental,
        limit=limit,
    )

    if not results:
        click.echo("No models matched the given criteria.")
        return

    header = (
        f"  {'ID':<{_COL_ID}}  {'Tier':<{_COL_TIER}}  "
        f"{'Cost':<{_COL_COST}}  {'Experimental':<{_COL_EXP_R}}  Reasons"
    )
    click.echo(header)
    click.echo("  " + "-" * (len(header) - 2))
    for advisory in results:
        m = advisory.model
        mid = m.id if len(m.id) <= _COL_ID else m.id[: _COL_ID - 1] + "..."
        exp = "yes" if m.experimental else "no"
        reasons_str = "; ".join(advisory.reasons) if advisory.reasons else "-"
        click.echo(
            f"  {mid:<{_COL_ID}}  {m.tier:<{_COL_TIER}}  "
            f"{m.cost_class:<{_COL_COST}}  {exp:<{_COL_EXP_R}}  {reasons_str}"
        )


@models.command("mode")
@click.argument("mode")
def models_mode(mode: str) -> None:
    """Show the advisory model policy for a mode (ask, plan, run)."""
    from openshard.models.mode_policy import model_policy_for_mode
    from openshard.models.registry import display_name_for

    _LABEL = 12
    mode = mode.strip().lower()
    if mode not in ("ask", "plan", "run"):
        raise click.ClickException(
            f"Unknown mode '{mode}'. Supported modes: ask, plan, run."
        )
    policy = model_policy_for_mode(mode)
    if policy is None:
        click.echo(f"{'Mode':<{_LABEL}}run")
        click.echo(
            f"{'Status':<{_LABEL}}Run routing remains controlled by existing routing policy"
        )
        return
    default_display = display_name_for(policy.default_model_id, policy.default_model_id)
    fallback_display = ", ".join(
        display_name_for(fid, fid) for fid in policy.fallback_model_ids
    )
    if mode == "ask":
        status = "Advisory only - Ask Mode is still local deterministic"
    else:
        status = "Advisory only - Plan Mode is still local deterministic"
    click.echo(f"{'Mode':<{_LABEL}}{mode}")
    click.echo(f"{'Default':<{_LABEL}}{default_display}")
    click.echo(f"{'Fallbacks':<{_LABEL}}{fallback_display}")
    click.echo(f"{'Status':<{_LABEL}}{status}")


@models.command("sync-openrouter")
def models_sync_openrouter() -> None:
    """Fetch and cache OpenRouter model metadata locally."""
    from openshard.models.openrouter_fetcher import (
        _DEFAULT_CACHE_PATH,
        OpenRouterFetchError,
        fetch_openrouter_models,
        normalize_model,
        save_openrouter_cache,
    )

    click.echo("Fetching model list from OpenRouter...")
    try:
        raw_models = fetch_openrouter_models()
    except OpenRouterFetchError as exc:
        raise click.ClickException(str(exc)) from exc

    normalized = [normalize_model(m) for m in raw_models]
    save_openrouter_cache(normalized)

    synced_count = len(normalized)
    example_ids = [m["id"] for m in normalized if m.get("id")][:5]

    from openshard.models.openrouter_fetcher import load_openrouter_cache

    cache = load_openrouter_cache()
    synced_at = cache.get("synced_at", "") if cache else ""

    click.echo(f"Synced {synced_count} models")
    click.echo(f"  Cache:     {_DEFAULT_CACHE_PATH}")
    click.echo(f"  Synced at: {synced_at}")
    if example_ids:
        click.echo("  Example IDs:")
        for mid in example_ids:
            click.echo(f"    {mid}")


@models.command("openrouter-cache")
def models_openrouter_cache() -> None:
    """Inspect the local OpenRouter model metadata cache."""
    from openshard.models.openrouter_fetcher import (
        _DEFAULT_CACHE_PATH,
        OpenRouterCacheError,
        load_openrouter_cache,
    )

    try:
        cache = load_openrouter_cache()
    except OpenRouterCacheError as exc:
        raise click.ClickException(str(exc)) from exc

    if cache is None:
        click.echo("No OpenRouter cache found. Run: openshard models sync-openrouter")
        return

    synced_at = cache.get("synced_at", "unknown")
    model_count = cache.get("model_count", 0)
    models_list = cache.get("models", [])
    top_ids = [m["id"] for m in models_list if m.get("id")][:10]

    click.echo("OpenRouter model cache")
    click.echo("  Status:    present")
    click.echo(f"  Cache:     {_DEFAULT_CACHE_PATH}")
    click.echo(f"  Synced at: {synced_at}")
    click.echo(f"  Models:    {model_count}")
    if top_ids:
        click.echo("\nTop 10 model IDs:")
        for mid in top_ids:
            click.echo(f"  {mid}")


@cli.group(invoke_without_command=True)
@click.pass_context
def profiles(ctx: click.Context):
    """Profile management commands."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@profiles.command("stats")
def profiles_stats():
    """Show per-profile performance stats from run history."""
    from openshard.history.metrics import compute_profile_stats, load_runs

    runs = load_runs()
    if not runs:
        log_path = Path.cwd() / _LOG_PATH
        if not log_path.exists():
            click.echo("No run history found. Run 'openshard run' to get started.")
        else:
            click.echo("No runs recorded yet.")
        return

    stats = compute_profile_stats(runs)
    profiled_runs = sum(s["runs_count"] for s in stats.values())

    click.echo(f"[profile stats]  {profiled_runs} run{'s' if profiled_runs != 1 else ''} with profile data\n")

    col = 16
    header = f"  {'profile':<{col}}  {'runs':>5}  {'avg cost':>9}  {'avg dur':>8}  {'pass rate':>9}  {'retry':>6}"
    click.echo(header)
    click.echo("  " + "-" * (len(header) - 2))

    for profile, s in stats.items():
        n = s["runs_count"]
        avg_cost = f"${s['avg_cost']:.4f}" if s["avg_cost"] is not None else "-"
        avg_dur = f"{s['avg_duration']:.1f}s" if s["avg_duration"] is not None else "-"
        pass_rate = f"{s['verification_pass_rate']:.0%}" if s["verification_pass_rate"] is not None else "-"
        retry = f"{s['retry_rate']:.0%}" if s["retry_rate"] is not None else "-"
        click.echo(f"  {profile:<{col}}  {n:>5}  {avg_cost:>9}  {avg_dur:>8}  {pass_rate:>9}  {retry:>6}")


@cli.group(invoke_without_command=True)
@click.pass_context
def skills(ctx: click.Context):
    """Skills management commands."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@skills.command("list")
def skills_list():
    """List all skills discovered in the current repository."""
    from openshard.skills.discovery import discover_skills

    skills_ = discover_skills(Path.cwd())
    if not skills_:
        click.echo("No skills found. Add skill definitions to .openshard/skills/*/SKILL.md")
        return

    col_slug = 28
    col_name = 36
    for s in skills_:
        slug = s.slug if len(s.slug) <= col_slug else s.slug[: col_slug - 1] + "..."
        name = s.name if len(s.name) <= col_name else s.name[: col_name - 1] + "..."
        click.echo(f"  {slug:<{col_slug}}  {name:<{col_name}}  {s.category}")


@skills.command("stats")
def skills_stats():
    """Show per-skill performance stats from run history."""
    from openshard.history.metrics import compute_skill_stats, load_runs

    runs = load_runs()
    if not runs:
        log_path = Path.cwd() / _LOG_PATH
        if not log_path.exists():
            click.echo("No run history found. Run 'openshard run' to get started.")
        else:
            click.echo("No runs recorded yet.")
        return

    stats = compute_skill_stats(runs)
    if not stats:
        click.echo("No skill data in run history.")
        return

    skill_runs = sum(s["runs_count"] for s in stats.values())
    click.echo(f"[skill stats]  {len(stats)} skill{'s' if len(stats) != 1 else ''}  (from {skill_runs} skill-matched run{'s' if skill_runs != 1 else ''})\n")

    col = 28
    header = f"  {'skill':<{col}}  {'runs':>5}  {'avg cost':>9}  {'avg dur':>8}  {'pass rate':>9}  {'retry':>6}"
    click.echo(header)
    click.echo("  " + "-" * (len(header) - 2))

    for slug, s in stats.items():
        n = s["runs_count"]
        avg_cost = f"${s['avg_cost']:.4f}" if s["avg_cost"] is not None else "-"
        avg_dur = f"{s['avg_duration']:.1f}s" if s["avg_duration"] is not None else "-"
        pass_rate = f"{s['verification_pass_rate']:.0%}" if s["verification_pass_rate"] is not None else "-"
        retry = f"{s['retry_rate']:.0%}" if s["retry_rate"] is not None else "-"
        label = slug if len(slug) <= col else slug[: col - 1] + "..."
        click.echo(f"  {label:<{col}}  {n:>5}  {avg_cost:>9}  {avg_dur:>8}  {pass_rate:>9}  {retry:>6}")


@cli.group(invoke_without_command=True)
@click.pass_context
def advisory(ctx: click.Context):
    """Executor advisory ranking commands (does not change routing defaults)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@advisory.command("rank")
@click.option("--task", required=True, help="Task description to rank executors for.")
@click.option(
    "--risk",
    type=click.Choice(["low", "medium", "high"], case_sensitive=False),
    default="low",
    show_default=True,
    help="Risk level hint.",
)
@click.option(
    "--category",
    default="standard",
    show_default=True,
    help="Task category (security, complex, boilerplate, visual, standard).",
)
@click.option(
    "--opencode-preference",
    "opencode_preference",
    is_flag=True,
    default=False,
    help="Signal that OpenCode is preferred when available.",
)
def advisory_rank(
    task: str,
    risk: str,
    category: str,
    opencode_preference: bool,
) -> None:
    """Rank executor paths for a task. Advisory only — does not change execution defaults."""
    from openshard.execution.opencode_adapter import detect_opencode
    from openshard.routing.engine import is_readonly_task
    from openshard.routing.executor_advisory import rank_executors, render_executor_advisory

    availability = detect_opencode()
    read_only = is_readonly_task(task)

    result = rank_executors(
        task,
        category=category,
        risk_level=risk,
        read_only=read_only,
        opencode_available=availability.available,
        opencode_preference=opencode_preference,
        risky_paths=[],
    )

    for line in render_executor_advisory(result):
        click.echo(line)


@cli.command()
def report():
    """Display a summary report of recent executions."""
    log_path = Path.cwd() / _LOG_PATH

    if not log_path.exists():
        click.echo("No run history found. Run 'openshard run' to get started.")
        return

    entries = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not entries:
        click.echo("No runs recorded yet.")
        return

    total         = len(entries)
    verify_passed = sum(1 for e in entries if e.get("verification_passed") is True)
    verify_failed = sum(1 for e in entries if e.get("verification_passed") is False)
    retry_count   = sum(1 for e in entries if e.get("retry_triggered") is True)
    avg_duration  = sum(e.get("duration_seconds", 0) for e in entries) / total
    total_tokens  = sum(e.get("total_tokens", 0) for e in entries)
    costs = [e["estimated_cost"] for e in entries if e.get("estimated_cost") is not None]
    total_cost    = sum(costs) if costs else None
    avg_cost      = total_cost / len(costs) if costs else None

    click.echo("\n[report]")
    click.echo(f"  total runs:             {total}")
    click.echo(f"  successful verifications: {verify_passed}")
    click.echo(f"  failed verifications:   {verify_failed}")
    click.echo(f"  retries triggered:      {retry_count}")
    click.echo(f"  average duration:       {avg_duration:.1f}s")
    click.echo(f"  total tokens:           {total_tokens}")
    click.echo(f"  total cost:             {'$' + f'{total_cost:.4f}' if total_cost is not None else '-'}")
    click.echo(f"  average cost per run:   {'$' + f'{avg_cost:.4f}' if avg_cost is not None else '-'}")

    from openshard.history.metrics import compute_profile_stats, compute_skill_stats
    _profile_stats = compute_profile_stats(entries)
    _profiled = sum(s["runs_count"] for s in _profile_stats.values())
    if _profiled > 0:
        click.echo("\n  profiles:")
        for _pname, _ps in _profile_stats.items():
            _n = _ps["runs_count"]
            _pc = f"${_ps['avg_cost']:.4f}" if _ps["avg_cost"] is not None else "-"
            _pd = f"{_ps['avg_duration']:.1f}s" if _ps["avg_duration"] is not None else "-"
            _pp = f"{_ps['verification_pass_rate']:.0%}" if _ps["verification_pass_rate"] is not None else "-"
            _pr = f"{_ps['retry_rate']:.0%}" if _ps["retry_rate"] is not None else "-"
            _run_label = "run" if _n == 1 else "runs"
            click.echo(f"    {_pname:<14}  {_n} {_run_label}  pass: {_pp}  retry: {_pr}  {_pc}  {_pd}")

    _skill_stats = compute_skill_stats(entries)
    if _skill_stats:
        click.echo("\n  skills (top 5):")
        for _slug, _ss in list(_skill_stats.items())[:5]:
            _sn = _ss["runs_count"]
            _sc = f"${_ss['avg_cost']:.4f}" if _ss["avg_cost"] is not None else "-"
            _sd = f"{_ss['avg_duration']:.1f}s" if _ss["avg_duration"] is not None else "-"
            _sp = f"{_ss['verification_pass_rate']:.0%}" if _ss["verification_pass_rate"] is not None else "-"
            _sr = f"{_ss['retry_rate']:.0%}" if _ss["retry_rate"] is not None else "-"
            _run_label = "run" if _sn == 1 else "runs"
            click.echo(f"    {_slug:<24}  {_sn} {_run_label}  pass: {_sp}  retry: {_sr}  {_sc}  {_sd}")

    click.echo("\n  recent runs:")
    for entry in entries[-5:][::-1]:
        ts = entry.get("timestamp", "")
        ts = ts.rstrip("Z").replace("T", " ").split(".")[0]
        task  = entry.get("task", "")[:50]
        model = entry.get("execution_model", "")
        retry = "yes" if entry.get("retry_triggered") else "no"
        vp    = entry.get("verification_passed")
        vstr  = "passed" if vp is True else ("failed" if vp is False else "-")
        click.echo(f"  {ts}  {task}")
        click.echo(f"    model: {model}  retry: {retry}  verify: {vstr}")

def _compute_metrics(entries: list[dict]) -> dict:
    from collections import Counter

    costs = [e["estimated_cost"] for e in entries if e.get("estimated_cost") is not None]
    total_cost = sum(costs) if costs else None
    avg_cost = total_cost / len(costs) if costs else None  # type: ignore[operator]  # total_cost is float when costs is non-empty; guarded by same condition

    durations = [e["duration_seconds"] for e in entries if "duration_seconds" in e]
    avg_duration = sum(durations) / len(durations) if durations else 0.0

    models = Counter(e["execution_model"] for e in entries if e.get("execution_model"))
    categories = Counter(e["routing_category"] for e in entries if e.get("routing_category"))

    v_passed = sum(1 for e in entries if e.get("verification_passed") is True)
    v_failed = sum(1 for e in entries if e.get("verification_passed") is False)
    v_unknown = len(entries) - v_passed - v_failed

    timestamps = [e["timestamp"] for e in entries if e.get("timestamp")]
    most_recent = max(timestamps) if timestamps else None
    if most_recent:
        most_recent = most_recent.rstrip("Z").replace("T", " ").split(".")[0] + " UTC"

    return {
        "total_runs": len(entries),
        "total_cost": total_cost,
        "avg_cost": avg_cost,
        "avg_duration": avg_duration,
        "most_recent": most_recent,
        "models": dict(models.most_common()),
        "categories": dict(categories.most_common()),
        "verification": {"passed": v_passed, "failed": v_failed, "unknown": v_unknown},
    }


@cli.command()
def metrics():
    """Show aggregated metrics from run history."""
    log_path = Path.cwd() / _LOG_PATH

    if not log_path.exists():
        click.echo("No run history found. Run 'openshard run' to get started.")
        return

    entries = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not entries:
        click.echo("No runs recorded yet.")
        return

    m = _compute_metrics(entries)

    def _cost(v: float | None) -> str:
        return f"${v:.4f}" if v is not None else "-"

    click.echo("\n[metrics]")
    click.echo(f"  runs:             {m['total_runs']}")
    click.echo(f"  total cost:       {_cost(m['total_cost'])}")
    click.echo(f"  avg cost/run:     {_cost(m['avg_cost'])}")
    click.echo(f"  avg duration:     {m['avg_duration']:.1f}s")
    click.echo(f"  most recent:      {m['most_recent'] or '-'}")

    if m["models"]:
        click.echo("\n  models")
        for model_id, count in m["models"].items():
            label = _model_label(model_id)
            click.echo(f"    {label:<26} {count}")

    if m["categories"]:
        click.echo("\n  categories")
        for cat, count in m["categories"].items():
            click.echo(f"    {cat:<26} {count}")

    v = m["verification"]
    click.echo("\n  verification")
    click.echo(f"    passed           {v['passed']}")
    click.echo(f"    failed           {v['failed']}")
    click.echo(f"    not attempted    {v['unknown']}")


def _render_log_entry(entry: dict, detail: str, index: int | None = None) -> None:
    """Render a stored run log entry at the requested detail level."""
    ts = entry.get("timestamp", "").rstrip("Z").replace("T", " ").split(".")[0]
    task = entry.get("task", "")
    summary = entry.get("summary", "")
    stage_runs_data: list[dict] = entry.get("stage_runs", [])
    routing_model: str = entry.get("routing_model", "")
    routing_rationale: str = entry.get("routing_rationale", "")
    files_detail: list[dict] = entry.get("files_detail", [])
    notes: list[str] = entry.get("notes", [])

    click.echo(f"\nTask: {task}")
    if ts:
        click.echo(f"At: {ts} UTC")
    _stored_findings = entry.get("findings") or []
    if _stored_findings:
        from openshard.cli.run_output import render_review_tldr_memo
        from openshard.history.shard_contract import ShardFinding
        _sf_list = [
            ShardFinding(severity=f.get("severity", "Note"), message=f.get("message", ""))
            for f in _stored_findings
            if isinstance(f, dict)
        ]
        _review_files = [fd.get("path", "") for fd in files_detail if fd.get("path")]
        click.echo("\nReview complete")
        click.echo(render_review_tldr_memo(_sf_list, _review_files))
    elif entry.get("is_review_task") or (
        entry.get("review_domain") and entry.get("review_domain") not in ("generic_review", "terraform_iac")
    ):
        from openshard.cli.run_output import render_review_fallback_memo
        from openshard.review.domain_files import no_files_message
        _review_files = [fd.get("path", "") for fd in files_detail if fd.get("path")]
        _domain_files_log = entry.get("domain_files") or []
        # Prefer domain-discovered evidence files over the changed-files list.
        # Changed files are always empty for read-only reviews; domain_files
        # contains the files that were actually inspected for the review domain.
        _evidence_files = _domain_files_log or _review_files
        click.echo("\nReview complete")
        if not _evidence_files:
            _no_msg = no_files_message(entry.get("review_domain", ""))
            if _no_msg:
                click.echo(_no_msg)
            else:
                click.echo(render_review_fallback_memo(
                    [],
                    include_diagnostic=(detail in ("more", "full")),
                ))
        else:
            click.echo(render_review_fallback_memo(
                _evidence_files,
                include_diagnostic=(detail in ("more", "full")),
                is_evidence=True,
            ))
    else:
        click.echo("\nDone")
        if summary:
            click.echo(summary)

    # Model line — only in default mode; receipt shows model in --more / --full.
    # Prefer routing_selected_model (scored routing) over routing_model (keyword
    # routing) so the displayed model matches what the receipt shows.
    _is_ro = routing_rationale == "read-only analysis"
    _routing_selected: str = entry.get("routing_selected_model", "")
    if detail == "default":
        if stage_runs_data:
            seen: dict[str, list[str]] = {}
            for sr in stage_runs_data:
                lbl = _model_label(sr["model"])
                stype = "analysis" if (_is_ro and sr["stage_type"] == "implementation") else sr["stage_type"]
                seen.setdefault(lbl, []).append(stype)
            parts = [f"{lbl} ({' + '.join(types)})" for lbl, types in seen.items()]
            prefix = "Model" if len(seen) == 1 else "Models"
            click.echo(f"\n{prefix}: {', '.join(parts)}")
        elif _routing_selected or routing_model:
            _display_model = _routing_selected or routing_model
            from openshard.history.shard_contract import (
                display_model_name as _sc_display_model_name,
            )
            lbl = _sc_display_model_name(_display_model)
            if _routing_selected and _routing_selected != routing_model:
                # Scored routing overrode the keyword pick — mirror receipt format.
                click.echo(f"\nModel: Auto → {lbl}")
            else:
                reason = _RATIONALE_SHORT.get(routing_rationale, "")
                suffix = f" ({reason})" if reason else ""
                click.echo(f"\nModel: {lbl}{suffix}")
        # Always-visible routing truth: state plainly that per-role planner/
        # executor/validator selection was advisory, so the default view can
        # never imply three models ran when one did.
        from openshard.history.routing_truth import (
            build_routing_truth as _brt,
        )
        from openshard.history.routing_truth import (
            render_routing_truth_lines as _rrtl,
        )
        for _rt_line in _rrtl(_brt(entry), "default"):
            click.echo(_rt_line)
        _df_compact = entry.get("developer_feedback")
        if _df_compact:
            click.echo(f"Feedback: {_df_compact.get('outcome', '')}")

    # Shard receipt (--more / --full) — shown near top before diagnostic blocks
    if detail != "default":
        from openshard.history.shard_contract import (
            build_shard_receipt,
            render_compact_shard_receipt,
            render_full_shard_receipt,
        )
        _shard = build_shard_receipt(entry, index)
        click.echo("")
        click.echo(render_compact_shard_receipt(_shard))
        click.echo("")
        click.echo(render_full_shard_receipt(_shard, detail=detail))
        from openshard.history.routing_truth import (
            build_routing_truth as _brt,
        )
        from openshard.history.routing_truth import (
            render_routing_truth_lines as _rrtl,
        )
        _rt_lines = _rrtl(_brt(entry), detail)
        if _rt_lines:
            click.echo("")
            for _rt_line in _rt_lines:
                click.echo(_rt_line)

    # Proof summary (--more only) - compact OSN proof presence check
    if detail == "more":
        from openshard.cli.run_output import _print_proof_summary
        _proof_nm = _native_meta_from_entry(entry)
        if _proof_nm is not None:
            _print_proof_summary(_proof_nm)

    # Stages (--full only)
    if detail == "full" and stage_runs_data:
        click.echo("\nStages")
        for sr in stage_runs_data:
            cost_s = f"${sr['cost']:.4f}" if sr.get("cost") is not None else "-"
            _stage_label = "Analysis" if (_is_ro and sr['stage_type'] == "implementation") else sr['stage_type'].capitalize()
            click.echo(f"  {_stage_label} ({_model_label(sr['model'])}): {sr['duration']:.1f}s, {cost_s}")

    # Task type (--full only, read-only only)
    if detail == "full" and _is_ro:
        click.echo("\nTask type")
        click.echo("  Read-only analysis")
        click.echo("  Reason: The prompt asks a question, so file changes are blocked.")

    # Routing (--full only)
    if detail == "full" and "routing_category" in entry:
        click.echo("\n  Routing")
        click.echo(f"    Category: {entry['routing_category']}")
        if entry.get("routing_used_fallback"):
            click.echo("    Initial candidate: fallback (keyword routing)")
        elif entry.get("routing_selected_model"):
            _prov = entry.get("routing_selected_provider")
            _prov_suffix = f" ({_prov})" if _prov else ""
            click.echo(f"    Initial candidate: {_model_label(entry['routing_selected_model'])}{_prov_suffix}")
        _tdr_check = entry.get("tier_dispatch_receipt")
        if _tdr_check and _tdr_check.get("enabled") and _tdr_check.get("applied"):
            click.echo("    Note: tier dispatch changed the work model shown below.")
        if entry.get("routing_feedback_scoring_used"):
            _fb_adjs = entry.get("routing_feedback_adjustments") or {}
            _fb_rsns = entry.get("routing_feedback_reasons") or {}
            if _fb_adjs:
                click.echo("    Feedback scoring:")
                for _fm, _fa in _fb_adjs.items():
                    _rsn = _fb_rsns.get(_fm, "")
                    _rsn_str = f" ({_rsn})" if _rsn else ""
                    click.echo(f"      {_model_label(_fm)}: {_fa:+.2f}{_rsn_str}")
            else:
                click.echo("    Feedback scoring: enabled (no adjustment)")

    # Execution profile (--full only)
    if detail == "full" and entry.get("execution_profile"):
        _profile = entry["execution_profile"]
        _reason = entry.get("execution_profile_reason", "")
        click.echo("  Execution")
        _is_ro = entry.get("routing_rationale") == "read-only analysis"
        click.echo(f"    Mode: {_profile_display_label(_profile, is_readonly=_is_ro)}")
        if _reason:
            click.echo(f"    Reason: {_reason}")

    # Form factor (--full only)
    if detail == "full" and "form_factor" in entry:
        _ff = entry["form_factor"]
        _ff_pub = _PUBLIC_MODE_LABEL.get(_ff["public_mode"], _ff["public_mode"].title())
        click.echo("\n  Form factor")
        click.echo(f"    Public mode:  {_ff_pub}")
        click.echo(f"    Internal:     {_ff['internal_form_factor']}")
        click.echo(f"    Reason:       {_ff['reason']}")
        click.echo(f"    Confidence:   {_ff['confidence']}")
        click.echo(f"    Risk:         {_ff['risk_level']}")
        if _ff.get("context_quality"):
            click.echo(f"    Context:      {_ff['context_quality']}")
        for _w in _ff.get("warnings", []):
            click.echo(f"    Warning:      {_w}")

    # Verification plan (--full only)
    if detail == "full" and "verification_plan" in entry:
        _vp_raw = entry["verification_plan"]
        # Native runs store verification_plan as {"commands": [...]} (asdict of VerificationPlan).
        # Non-native runs store it as a plain list of command dicts.
        if isinstance(_vp_raw, dict):
            _vp_cmds = _vp_raw.get("commands") or []
        elif isinstance(_vp_raw, list):
            _vp_cmds = _vp_raw
        else:
            _vp_cmds = []
        for _vc in _vp_cmds:
            if not isinstance(_vc, dict):
                continue
            _argv_str = " ".join(_vc.get("argv") or [])
            click.echo("  Verification")
            click.echo(f"    Name:    {_vc.get('name', '')}")
            click.echo(f"    Safety:  {_vc.get('safety', '')}")
            click.echo(f"    Source:  {_vc.get('source', '')}")
            click.echo(f"    Command: {_argv_str}")

    # Files
    fc = entry.get("files_created", 0)
    fu = entry.get("files_updated", 0)
    fd = entry.get("files_deleted", 0)
    if fc or fu or fd:
        counts = ", ".join(p for p in [
            f"{fc} created" if fc else "",
            f"{fu} updated" if fu else "",
            f"{fd} deleted" if fd else "",
        ] if p)
        click.echo(f"\nFiles: {counts}")
        if detail == "full":
            for f in files_detail:
                desc = f" - {f['summary']}" if f.get("summary") else ""
                click.echo(f"  {f['path']}{desc}")

    # Notes (--full only)
    if detail == "full" and notes:
        _notes = [_truncate_note(n) for n in notes if n][:3]
        if _notes:
            click.echo("\nNotes")
            for note in _notes:
                click.echo(f"  {note}")

    # Developer feedback (--more / --full)
    _feedback = entry.get("feedback")
    if detail in ("more", "full") and _feedback:
        click.echo("\nDeveloper feedback")
        _action = _feedback.get("action")
        _reason = _feedback.get("correction_reason")
        _rating = _feedback.get("rating") or ""
        _fb_note = _feedback.get("note", "")
        if _action:
            click.echo(f"  Action: {_action}")
        if _reason:
            click.echo(f"  Reason: {_reason}")
        if _rating:
            click.echo(f"  Rating: {_rating}")
        if _fb_note:
            click.echo(f"  Note: {_fb_note}")

    # Feedback Signals v0 (--more / --full)
    if detail in ("more", "full") and index is not None:
        from openshard.history.feedback import load_feedback_for_shard
        from openshard.history.shard_contract import _make_shard_id
        _shard_id = _make_shard_id(entry.get("timestamp", ""), index)
        _fb_signals = load_feedback_for_shard(_shard_id)
        if _fb_signals:
            _fb = _fb_signals[-1]
            click.echo("\nFEEDBACK")
            click.echo(f"{'Outcome':<12}{_fb.outcome}")
            if _fb.note:
                click.echo(f"{'Note':<12}{_fb.note}")

    # Developer feedback v1 (in-entry, --more / --full)
    _dev_feedback = entry.get("developer_feedback")
    if detail in ("more", "full") and _dev_feedback:
        click.echo("\n  FEEDBACK")
        click.echo(f"  {'Outcome':<12}{_dev_feedback.get('outcome', '')}")
        if _dev_feedback.get("edited"):
            click.echo(f"  {'Edited':<12}yes")
        if _dev_feedback.get("manual_fix_required"):
            click.echo(f"  {'Manual fix':<12}yes")
        if _dev_feedback.get("ci_passed"):
            click.echo(f"  {'CI':<12}passed")
        elif _dev_feedback.get("ci_failed"):
            click.echo(f"  {'CI':<12}failed")
        if _dev_feedback.get("pr_created"):
            click.echo(f"  {'PR':<12}created")
        if _dev_feedback.get("pr_merged"):
            click.echo(f"  {'PR':<12}merged")
        if _dev_feedback.get("reason"):
            click.echo(f"  {'Reason':<12}{_dev_feedback['reason']}")

    # User notes (--more / --full)
    _user_notes = [n for n in entry.get("notes", []) if isinstance(n, dict)]
    if detail in ("more", "full") and _user_notes:
        click.echo("\n  NOTES")
        for _un in _user_notes:
            _un_text = _un.get("text", "")
            _un_at = _un.get("recorded_at", "")
            click.echo(f"  {_un_text}")
            if _un_at:
                click.echo(f"  {'recorded':<12}{_un_at}")

    # Token / model detail (--full only)
    if detail == "full":
        full_model = entry.get("execution_model", "")
        if full_model and not stage_runs_data and not routing_model:
            click.echo(f"\nModel: {full_model}")
        pt = entry.get("prompt_tokens", 0)
        ct = entry.get("completion_tokens", 0)
        tt = entry.get("total_tokens", 0)
        if tt:
            click.echo(f"Tokens: {pt} prompt / {ct} completion / {tt} total")
        if entry.get("retry_triggered"):
            click.echo("Retried: yes")
        vp = entry.get("verification_passed")
        if vp is not None:
            click.echo(f"Verification: {'passed' if vp else 'failed'}")
        ws = entry.get("workspace_path")
        if ws:
            click.echo(f"Workspace: {ws}")

    # Native inspection (--full only)
    if detail == "full":
        _render_native_inspection(entry, detail)

    # Tier dispatch for non-native runs (native gets it inside _render_native_inspection)
    if detail == "full" and entry.get("workflow") != "native":
        _tdr = entry.get("tier_dispatch_receipt")
        _vpol = entry.get("validator_policy")
        if _tdr and _tdr.get("enabled"):
            from openshard.cli.run_output import _render_tier_dispatch_block
            _init_model = entry.get("routing_selected_model")
            _vr = entry.get("validator_result")
            _is_direct_ask = _is_ro and not any(
                sr.get("stage_type") == "planning"
                for sr in (entry.get("stage_runs") or [])
            )
            for line in _render_tier_dispatch_block(_tdr, detail, initial_model=_init_model, validator_result=_vr, validator_policy=_vpol, is_ask=_is_direct_ask):
                click.echo(line)
        elif _vpol and not _vpol.get("run"):
            click.echo(f"\nValidator: skipped — {_vpol.get('reason', '')}")

    duration = entry.get("duration_seconds", 0)
    cost = entry.get("estimated_cost")
    cost_str = f"${cost:.4f}" if cost is not None else "-"

    # Compact RECEIPT — default view only, appears before Time/Cost footer
    if detail == "default":
        from openshard.history.shard_contract import (
            build_shard_receipt,
            render_compact_shard_receipt,
        )
        _shard = build_shard_receipt(entry, index)
        click.echo("")
        click.echo(render_compact_shard_receipt(_shard))

        # One compact proof-quality line, derived from the Shard Proof Contract.
        from openshard.history.shard_quality import build_shard_quality_summary
        _quality = build_shard_quality_summary(entry, _shard)
        click.echo(f"Proof: {_quality['status']}")
        if _quality["unsafe_findings_count"] > 0:
            click.echo(f"Unsafe findings: {_quality['unsafe_findings_count']}")

    click.echo(f"\nTime: {duration:.1f}s   Cost: {cost_str}")

    # Baseline comparison and cost section (after Time/Cost footer)
    if detail == "default":
        _nm = _native_meta_from_entry(entry)
        if _nm is not None:
            from openshard.cost.baseline import format_baseline_line
            _pt = entry.get("prompt_tokens") or 0
            _ct = entry.get("completion_tokens") or 0
            _bl = format_baseline_line(_pt, _ct, actual_cost=cost)
            if _bl is not None:
                click.echo(_bl)
    elif detail == "more":
        from openshard.cost.baseline import (
            compute_baseline_comparison,
            format_concise_comparison_lines,
        )
        _pt = entry.get("prompt_tokens") or 0
        _ct = entry.get("completion_tokens") or 0
        _cmp = compute_baseline_comparison(_pt, _ct, actual_cost=cost)
        if _cmp is not None:
            click.echo("\nCOST COMPARISON")
            click.echo(f"  OpenShard selected: {_shard.model_display}")
            click.echo(f"  Run cost: ${_cmp['actual_cost_usd']:.4f}")
            _rows = format_concise_comparison_lines(_pt, _ct, _cmp["actual_cost_usd"])
            if _rows:
                click.echo("")
                click.echo("  Estimated same-token baseline:")
                for _row in _rows:
                    click.echo(_row)
            click.echo("")
            click.echo("  Method: same-token API price estimate. Real single-model cost may differ.")
    elif detail == "full":
        from openshard.cost.baseline import (
            compute_baseline_comparison,
            format_full_comparison_lines,
        )
        _pt = entry.get("prompt_tokens") or 0
        _ct = entry.get("completion_tokens") or 0
        _cmp = compute_baseline_comparison(_pt, _ct, actual_cost=cost)
        if _cmp is not None:
            click.echo("\nCost comparison")
            click.echo(f"  OpenShard: ${_cmp['actual_cost_usd']:.4f}")
            _rows = format_full_comparison_lines(_pt, _ct, _cmp["actual_cost_usd"])
            if _rows:
                click.echo("")
                click.echo("  Compared with")
                for _row in _rows:
                    click.echo(_row)
            click.echo("")
            click.echo("  Method: same-token API price estimate. Real single-model cost may differ.")


def _locate_history():
    """Resolve where this repository's local history lives (see history.locate).

    Walks up from the current directory to the nearest ``.openshard/`` or
    ``.git`` so ``last`` / ``history`` / ``context`` / ``stats`` read the same
    ``runs.jsonl`` from the repository root or any subdirectory of it, and
    never a sibling or parent repository's.
    """
    from openshard.history.locate import locate_history

    return locate_history()


@cli.command()
@click.option("--more", is_flag=True, default=False, help="Show file list, model names, and token breakdown.")
@click.option("--full", is_flag=True, default=False, help="Show all stored details including verification and workspace.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def last(more: bool, full: bool, as_json: bool):
    """Show what just happened: the most recent Shard receipt for this repository.

    Works from the repository root or any subdirectory. Nothing is rerun and
    nothing is guessed: unknown model/cost/tokens render as unknown or not
    recorded, and costs are always labelled as estimates.
    """
    from openshard.cli.visibility import no_history_lines, repo_note_lines

    loc = _locate_history()
    log_path = loc.runs_path

    if as_json:
        from openshard.history.shard_contract import build_shard_receipt

        entries = _load_run_entries(log_path)
        if not entries:
            click.echo(json.dumps(
                _machine_envelope("last", "not_found", run=None, repo=loc.to_dict()), indent=2,
            ))
            return
        entry = entries[-1]
        receipt = build_shard_receipt(entry, index=len(entries) - 1)
        from openshard.history.proof_contract import build_shard_proof_contract
        from openshard.history.shard_quality import build_shard_quality_summary
        from openshard.history.trust_score import evaluate_trust_score

        _ts = evaluate_trust_score(
            entry, receipt,
            interaction_event_types=_interaction_event_types(entry.get("timestamp", "")),
        )
        payload = _machine_envelope(
            "last", "ok", shard_id=receipt.shard_id,
            repo=loc.to_dict(),
            run=_export_run_entry(entry, include_timeline=True, receipt=receipt),
            trust={
                "score": _ts.score,
                "band": _ts.band,
                "penalties": [
                    {"code": p.code, "points": p.points, "reason": p.reason}
                    for p in _ts.penalties
                ],
            },
            proof_contract=build_shard_proof_contract(entry),
            shard_quality=build_shard_quality_summary(entry, receipt),
            **_content_hash_fields(entry),
        )
        click.echo(json.dumps(payload, indent=2))
        return

    detail = "full" if full else ("more" if more else "default")
    if not log_path.exists():
        for line in no_history_lines(loc):
            click.echo(line)
        return
    entries = _load_run_entries(log_path)
    if not entries:
        click.echo("No runs recorded yet.")
        return
    for line in repo_note_lines(loc):
        click.echo(line)
    _render_log_entry(entries[-1], detail, index=len(entries) - 1)


@cli.command("history")
@click.option("--limit", default=10, type=click.IntRange(min=1), show_default=True,
              help="Show the most recent N Shards.")
@click.option("--repo", "repo_filter", default=None,
              help="Only Shards recorded for this repository identity, remote URL, or folder name.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def history_cmd(limit: int, repo_filter: str | None, as_json: bool) -> None:
    """List recent work: a compact, newest-first view of this repository's Shards.

    Each row shows when, which agent, the status OpenShard can truthfully
    claim (a completed Claude Code turn is not a verified result), the check
    summary, the estimated cost, and how many attempts the Shard has had.
    Local history only; works from any subdirectory of the repository.
    """
    from openshard.cli.visibility import history_json_body, no_history_lines, render_history
    from openshard.history.query import recent_shards

    loc = _locate_history()
    page = recent_shards(limit=limit, repo=repo_filter, repo_path=loc.root)

    if as_json:
        status = "ok" if page.items else "not_found"
        click.echo(json.dumps(_machine_envelope("history", status, **history_json_body(page, loc)), indent=2))
        return
    if not page.items:
        if repo_filter and page.total_shards == 0 and loc.runs_path.exists():
            click.echo(f"No Shards recorded for repository filter '{repo_filter}' in {loc.display_name}.")
            return
        for line in no_history_lines(loc):
            click.echo(line)
        return
    for line in render_history(page, loc):
        click.echo(line)


@cli.command("context")
@click.argument("task", nargs=-1)
@click.option("--limit", default=None, type=click.IntRange(min=1),
              help="Maximum matches to show (default: the same 5 the MCP tool returns).")
@click.option("--repo", "repo_filter", default=None,
              help="Only Shards recorded for this repository identity, remote URL, or folder name.")
@click.option("--text", "as_text", is_flag=True, default=False,
              help="Print only the context block an agent would receive, verbatim.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def context_cmd(task: tuple[str, ...], limit: int | None, repo_filter: str | None, as_text: bool, as_json: bool) -> None:
    """Show what OpenShard would surface to an agent for TASK, and why.

    This is the same relevant_context the local MCP server exposes: prior
    Shards ranked by deterministic keyword overlap, each with the signals
    that matched it, its status and verification, retry history, changed
    files, and structured findings. No transcripts, notes, or model calls.
    """
    from openshard.cli.visibility import context_json_body, render_context
    from openshard.history.query import (
        DEFAULT_CONTEXT_LIMIT,
        ranking_explanation,
        recent_shards,
        relevant_context,
    )

    task_text = " ".join(task).strip()
    loc = _locate_history()
    effective_limit = limit if limit is not None else DEFAULT_CONTEXT_LIMIT
    ctx = relevant_context(task_text, limit=effective_limit, repo=repo_filter, repo_path=loc.root)
    # relevant_context already counted every Shard it considered; only a blank
    # task (which loads nothing) needs a separate count for an honest total.
    total = ctx.total_shards if task_text else recent_shards(limit=0, repo=repo_filter, repo_path=loc.root).total_shards
    ranking = ranking_explanation()

    if as_json:
        if not task_text:
            status = "no_task"
        elif ctx.matches:
            status = "ok"
        else:
            status = "not_found" if total == 0 else "no_match"
        click.echo(json.dumps(
            _machine_envelope("context", status, **context_json_body(ctx, loc, total, ranking)), indent=2,
        ))
        return
    if as_text:
        click.echo(ctx.context_text.rstrip("\n"))
        return
    for line in render_context(ctx, loc, total, ranking):
        click.echo(line)


@cli.group("trust")
def trust_group() -> None:
    """Run Trust Score — a heuristic over recorded proof signals."""


@trust_group.command("last")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def trust_last(as_json: bool) -> None:
    """Show the trust score for the most recent run.

    This is a deterministic trust heuristic over recorded proof signals, not a
    safety guarantee or certification.
    """
    from openshard.history.shard_contract import build_shard_receipt
    from openshard.history.trust_score import evaluate_trust_score, format_human, to_payload

    log_path = Path.cwd() / _LOG_PATH
    entries = _load_run_entries(log_path)
    if not entries:
        if as_json:
            click.echo(json.dumps(
                _machine_envelope("trust last", "not_found", score=None, signals={}, penalties=[]),
                indent=2,
            ))
        else:
            click.echo("No run history found. Run a task first with 'openshard run'.")
        return

    entry = entries[-1]
    receipt = build_shard_receipt(entry, index=len(entries) - 1)
    ts = evaluate_trust_score(
        entry, receipt,
        interaction_event_types=_interaction_event_types(entry.get("timestamp", "")),
    )
    if as_json:
        payload = _machine_envelope(
            "trust last", ts.status, shard_id=ts.shard_id, warnings=list(ts.warnings),
            **to_payload(ts),
        )
        click.echo(json.dumps(payload, indent=2))
        return
    for line in format_human(ts):
        click.echo(line)


# Recommendation phrases keyed by the contract overall_status. Kept as a small
# local map rather than re-deriving advice from section counts, so completeness
# logic is never duplicated here.
_PROOF_RECOMMENDATIONS: dict[str, str] = {
    "strong": "This Shard is strong evidence; required and recommended proof are present.",
    "usable": (
        "Use this Shard as evidence, but improve recommended gaps before relying "
        "on it for stricter review."
    ),
    "partial": (
        "Some required proof is missing. Fill the missing required proof before "
        "relying on this Shard."
    ),
    "weak": (
        "Most required proof is missing. This Shard is not yet usable as evidence."
    ),
    "unsafe": (
        "This Shard has unsafe findings. Do not use it as evidence until they are "
        "resolved."
    ),
    "unknown": (
        "The proof record could not be evaluated. Re-run the task to produce a "
        "usable Shard."
    ),
}


def _proof_recommendation(overall_status: str) -> str:
    """Map a contract overall_status to a one-line, human recommendation."""
    return _PROOF_RECOMMENDATIONS.get(
        overall_status, _PROOF_RECOMMENDATIONS["unknown"]
    )


# Plain-English labels for proof sections in human output. Raw technical section
# names (e.g. "timeline", "provenance") stay in JSON only; the human view reads
# in plain language. Unmapped names fall back to a title-cased form.
_PROOF_SECTION_LABELS: dict[str, str] = {
    "task": "Task",
    "executor": "Executor",
    "model": "Model",
    "actions": "Actions",
    "verification": "Verification",
    "result": "Result",
    "repo_state": "Repo state",
    "strategy": "Execution strategy",
    "files": "Files changed",
    "checks": "Checks",
    "timeline": "Step-by-step run events",
    "provenance": "Proof sources for claims",
    "cost": "Cost and duration",
}


def _proof_section_label(name: object) -> str:
    """Return a plain-English label for a proof section name."""
    if not isinstance(name, str):
        return str(name)
    return _PROOF_SECTION_LABELS.get(name, name.replace("_", " ").capitalize())


def _proof_human_lines(contract: dict, errors: list[str], shard_id: str | None) -> list[str]:
    """Render the compact human view of a proof contract.

    Shows the heading, status, summary, the required sections, the recommended
    gaps, and unsafe findings. It does not print all 17 sections; full detail
    belongs in --json.
    """
    overall = contract.get("overall_status", "unknown")
    lines = ["Proof for last run"]
    if shard_id:
        lines.append(f"Shard: {shard_id}")
    lines.append(f"Status: {overall}")
    lines.append(f"Summary: {contract.get('summary', '')}")
    lines.append(
        f"Contract: Shard Proof Contract v{contract.get('contract_version', '')}"
    )

    required = [
        s for s in contract.get("sections", [])
        if isinstance(s, dict) and s.get("level") == "required"
    ]
    lines.append("")
    lines.append("Required proof:")
    if required:
        for s in required:
            lines.append(f"- {_proof_section_label(s.get('name'))}: {s.get('status')}")
    else:
        lines.append("- none recorded")

    lines.append("")
    lines.append("Missing recommended proof:")
    gaps = contract.get("weak_recommended_sections", [])
    if gaps:
        for name in gaps:
            lines.append(f"- {_proof_section_label(name)}")
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Unsafe findings:")
    findings = contract.get("unsafe_findings", [])
    if findings:
        for finding in findings:
            lines.append(f"- {finding}")
    else:
        lines.append("- none")

    if errors:
        lines.append("")
        lines.append("Validation errors:")
        for err in errors:
            lines.append(f"- {err}")

    lines.append("")
    lines.append("Next action:")
    lines.append(f"- {_proof_recommendation(overall)}")
    return lines


def _proof_verify_last(as_json: bool) -> None:
    """Shared implementation for `proof last` and `shard verify last`.

    Inspects the latest run's Shard Proof Contract and reports whether the proof
    record is complete, safe, and usable as evidence. This is an inspection
    surface, not a CI gate.
    """
    from openshard.history.proof_contract import (
        build_shard_proof_contract,
        validate_shard_proof_contract,
    )
    from openshard.history.shard_contract import build_shard_receipt

    log_path = Path.cwd() / _LOG_PATH
    entries = _load_run_entries(log_path)
    if not entries:
        if as_json:
            payload = _machine_envelope(
                "proof last", "error",
                proof_contract=None,
                validation_errors=[],
                recommendation="No run history found. Run a task first with 'openshard run'.",
            )
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo("No run history found. Run a task first with 'openshard run'.")
        sys.exit(1)

    entry = entries[-1]
    receipt = build_shard_receipt(entry, index=len(entries) - 1)
    contract = build_shard_proof_contract(entry)
    errors = validate_shard_proof_contract(contract)
    overall = contract.get("overall_status", "unknown")

    if errors:
        envelope_status = "invalid"
    elif overall == "unsafe":
        envelope_status = "unsafe"
    else:
        envelope_status = "ok"

    exit_code = 1 if (errors or overall == "unsafe") else 0

    if as_json:
        payload = _machine_envelope(
            "proof last", envelope_status, shard_id=receipt.shard_id,
            proof_contract=contract,
            validation_errors=errors,
            recommendation=_proof_recommendation(overall),
            **_content_hash_fields(entry),
        )
        click.echo(json.dumps(payload, indent=2))
    else:
        for line in _proof_human_lines(contract, errors, receipt.shard_id):
            click.echo(line)

    if exit_code:
        sys.exit(exit_code)


@cli.group("proof")
def proof_group() -> None:
    """Show the proof for an AI coding run."""


@proof_group.command("last")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output (valid JSON only).")
def proof_last(as_json: bool) -> None:
    """Show the proof for the most recent run: is it complete, safe, and usable?"""
    _proof_verify_last(as_json)


@cli.group("shard")
def shard_group() -> None:
    """Inspect Shard proof records."""


@shard_group.group("verify")
def shard_verify_group() -> None:
    """Verify a Shard proof record against the Shard Proof Contract."""


@shard_verify_group.command("last")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output (valid JSON only).")
def shard_verify_last(as_json: bool) -> None:
    """Check whether the latest Shard proof record is complete, safe, and usable.

    Lower-level alias for 'openshard proof last'; identical output and exit code.
    """
    _proof_verify_last(as_json)


@shard_group.command("attempts")
@click.argument("shard_id")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output (valid JSON only).")
def shard_attempts(shard_id: str, as_json: bool) -> None:
    """List all Run/Attempts recorded under a Shard, in attempt order."""
    from openshard.history.metrics import load_runs
    from openshard.history.run_attempt import build_run_attempt
    from openshard.history.shard_contract import build_shard

    entries = [e for e in load_runs() if e.get("shard_id") == shard_id]
    if not entries:
        if as_json:
            click.echo(json.dumps(_machine_envelope(
                "shard attempts", "not_found", shard_id=shard_id, attempts=[],
            ), indent=2))
        else:
            click.echo(f"No attempts found for Shard '{shard_id}'.")
        return

    attempts = []
    for entry in entries:
        shard = build_shard(
            entry,
            shard_id=shard_id,
            created_at=entry.get("timestamp") or "",
            task_short=(entry.get("task") or "")[:70],
            task_full=entry.get("task") or "",
        )
        attempts.append(build_run_attempt(entry, shard))
    attempts.sort(key=lambda a: a.attempt_number)

    if as_json:
        from dataclasses import asdict
        payload = _machine_envelope(
            "shard attempts", "ok", shard_id=shard_id,
            attempts=[asdict(a) for a in attempts],
        )
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"Shard: {shard_id}")
    for a in attempts:
        click.echo(
            f"  Attempt {a.attempt_number} - {a.agent} - {a.created_at} "
            f"- {a.origin}/{a.capture_depth}"
            f"{' (retry)' if a.retry_triggered else ''}"
        )


@cli.group("mcp")
def mcp_group() -> None:
    """Model Context Protocol (MCP) server commands."""


@mcp_group.command("serve")
@click.option(
    "--repo-path",
    "repo_path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository whose .openshard/runs.jsonl to read (default: current directory).",
)
def mcp_serve(repo_path: Path | None) -> None:
    """Start the local read-only OpenShard MCP server over stdio.

    Exposes recent_shards, get_shard, get_receipt, search_history, and
    relevant_context to any MCP-compatible client (e.g. Claude Code) so it
    can read this repository's OpenShard run history. Read-only; no config
    is written.
    Speaks MCP over stdio only -- do not print to stdout once this starts.
    """
    try:
        from openshard.mcp.server import serve_stdio
    except ImportError as exc:
        raise click.ClickException(str(exc))

    click.echo("[openshard] starting MCP server (stdio)...", err=True)
    serve_stdio(repo_path=repo_path)


@mcp_group.group("install")
def mcp_install_group() -> None:
    """Configure a coding agent to launch the local OpenShard MCP server."""


@mcp_install_group.command("claude")
@click.option(
    "--repo-path",
    "repo_path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository to bind the MCP server to (default: current directory).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
@click.option(
    "--no-hooks",
    "no_hooks",
    is_flag=True,
    default=False,
    help="Configure the MCP server only; skip the Claude Code auto-capture hooks.",
)
@click.option(
    "--no-statusline",
    "no_statusline",
    is_flag=True,
    default=False,
    help="Skip configuring the Claude Code status line (model/cost/token capture).",
)
def mcp_install_claude(repo_path: Path | None, as_json: bool, no_hooks: bool, no_statusline: bool) -> None:
    """Configure Claude Code for this repository: OpenShard MCP server + auto-capture hooks.

    MCP: uses `claude mcp add` (local scope: private to you, bound to this
    one repository, never committed to git) so Claude Code can call
    OpenShard's read-only history tools.

    Hooks: merges `openshard hooks claude` into this repository's
    `.claude/settings.local.json` (Claude Code's documented, project-local,
    not-shared settings file) so normal Claude Code sessions are recorded
    automatically as Shards/Receipts -- no `openshard import claude` needed.
    Unrelated hooks and settings are preserved. Pass --no-hooks to skip.

    Status line: also configures `statusLine` in the same settings file, the
    only official surface that reports model id, session cost, and token
    counts, so receipts can show them instead of Unknown/Not recorded. Only
    set when the project has no status line of its own yet -- an existing
    one is never replaced. Pass --no-statusline to skip.

    Restart Claude Code afterwards if it is already running. Safe to re-run;
    never creates duplicate entries.
    """
    from openshard.adapters.claude_hooks_install import (
        ClaudeHooksInstallResult,
        install_claude_hooks,
        install_claude_statusline,
    )
    from openshard.adapters.claude_mcp_install import install_claude_mcp

    result = install_claude_mcp(repo_path=repo_path)
    hooks_result: ClaudeHooksInstallResult | None = None
    statusline_result: ClaudeHooksInstallResult | None = None
    if result.status != "error" and not no_hooks and result.repo_root is not None:
        hooks_result = install_claude_hooks(repo_root=result.repo_root)
    if result.status != "error" and not no_hooks and not no_statusline and result.repo_root is not None:
        statusline_result = install_claude_statusline(repo_root=result.repo_root)

    hooks_failed = hooks_result is not None and hooks_result.status == "error"

    if as_json:
        payload = {
            "status": result.status,
            "repo_root": str(result.repo_root) if result.repo_root else None,
            "repo_identity": result.repo_identity,
            "command": result.command,
            "message": result.message,
            "warnings": result.warnings,
            "hooks": (
                {
                    "status": hooks_result.status,
                    "settings_path": str(hooks_result.settings_path) if hooks_result.settings_path else None,
                    "events": hooks_result.events,
                    "message": hooks_result.message,
                    "warnings": hooks_result.warnings,
                }
                if hooks_result is not None
                else {"status": "skipped"}
            ),
            "statusline": (
                {
                    "status": statusline_result.status,
                    "settings_path": str(statusline_result.settings_path) if statusline_result.settings_path else None,
                    "message": statusline_result.message,
                }
                if statusline_result is not None
                else {"status": "skipped"}
            ),
        }
        click.echo(json.dumps(payload, indent=2))
        if result.status == "error" or hooks_failed:
            raise SystemExit(1)
        return

    if result.status == "error":
        raise click.ClickException(result.message)

    from openshard.adapters.claude_mcp_install import MCP_TOOLS

    label = None
    if result.repo_identity:
        parts = result.repo_identity.split("/", 1)
        label = parts[1] if len(parts) == 2 else result.repo_identity
    elif result.repo_root is not None:
        label = result.repo_root.name

    click.echo("OpenShard MCP installed for Claude Code.")
    if result.status == "already_installed":
        click.echo("(already configured; no changes made)")
    elif result.status == "updated":
        click.echo("(updated existing configuration for this repository)")
    click.echo(f"Repository: {label}")
    click.echo(f"Tools: {', '.join(MCP_TOOLS)}")
    for w in result.warnings:
        click.echo(f"  ! {w}")

    if hooks_result is None:
        click.echo("Auto-capture hooks: skipped (--no-hooks).")
    elif hooks_failed:
        click.echo(f"Auto-capture hooks: NOT configured. {hooks_result.message}")
    else:
        from openshard.adapters.claude_hooks_install import HOOK_EVENTS, SETTINGS_RELPATH

        state = {
            "installed": "installed",
            "updated": "updated",
            "already_installed": "already configured",
        }.get(hooks_result.status, hooks_result.status)
        click.echo(f"Auto-capture hooks: {state} ({SETTINGS_RELPATH.as_posix()})")
        click.echo(f"Hook events: {', '.join(HOOK_EVENTS)}")
        click.echo("Claude Code sessions in this repository are now recorded as Shards automatically.")
        for w in hooks_result.warnings:
            click.echo(f"  ! {w}")

    if statusline_result is None:
        click.echo("Status line (model/cost/token capture): skipped.")
    elif statusline_result.status == "error":
        click.echo(f"Status line: NOT configured. {statusline_result.message}")
    elif statusline_result.status == "skipped_existing":
        click.echo(f"Status line: not changed. {statusline_result.message}")
    else:
        state = {"installed": "installed", "already_installed": "already configured"}.get(
            statusline_result.status, statusline_result.status
        )
        click.echo(f"Status line: {state}. Receipts can now show model/cost/token data when available.")

    click.echo("\nRestart Claude Code if it is already running.")
    if hooks_failed:
        raise SystemExit(1)


@mcp_group.group("uninstall")
def mcp_uninstall_group() -> None:
    """Remove OpenShard's Claude Code configuration for a repository."""


@mcp_uninstall_group.command("claude")
@click.option(
    "--repo-path",
    "repo_path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository to remove Claude Code configuration from (default: current directory).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def mcp_uninstall_claude(repo_path: Path | None, as_json: bool) -> None:
    """Remove the OpenShard MCP entry, auto-capture hooks, and status line for this repository.

    Reverses `openshard mcp install claude` / `openshard setup`. Only
    OpenShard's own entries are removed -- identified the same way they were
    installed (exact command match) -- so unrelated Claude Code
    configuration is never touched. A status line that is not OpenShard's
    own is left alone. Local Shard/Receipt history under `.openshard/` is
    never deleted by this command.
    """
    from openshard.adapters.claude_hooks_install import (
        uninstall_claude_hooks,
        uninstall_claude_statusline,
    )
    from openshard.adapters.claude_mcp_install import find_repo_root, uninstall_claude_mcp

    mcp_result = uninstall_claude_mcp(repo_path=repo_path)
    root = mcp_result.repo_root or find_repo_root(repo_path)
    hooks_result = uninstall_claude_hooks(repo_root=root) if root is not None else None
    statusline_result = uninstall_claude_statusline(repo_root=root) if root is not None else None

    any_error = (
        mcp_result.status == "error"
        or (hooks_result is not None and hooks_result.status == "error")
        or (statusline_result is not None and statusline_result.status == "error")
    )
    any_removed = (
        mcp_result.status == "removed"
        or (hooks_result is not None and hooks_result.status == "removed")
        or (statusline_result is not None and statusline_result.status == "removed")
    )

    if as_json:
        payload = {
            "mcp": {"status": mcp_result.status, "message": mcp_result.message},
            "hooks": (
                {"status": hooks_result.status, "events": hooks_result.events, "message": hooks_result.message}
                if hooks_result is not None
                else {"status": "skipped"}
            ),
            "statusline": (
                {"status": statusline_result.status, "message": statusline_result.message}
                if statusline_result is not None
                else {"status": "skipped"}
            ),
        }
        click.echo(json.dumps(payload, indent=2))
        if any_error:
            raise SystemExit(1)
        return

    if any_removed:
        click.echo("OpenShard Claude Code configuration removed.")
    else:
        click.echo("No OpenShard Claude Code configuration was found to remove.")
    click.echo(f"MCP: {mcp_result.status.replace('_', ' ')}. {mcp_result.message}")
    if hooks_result is not None:
        click.echo(f"Auto-capture hooks: {hooks_result.status.replace('_', ' ')}. {hooks_result.message}")
    if statusline_result is not None:
        click.echo(f"Status line: {statusline_result.status.replace('_', ' ')}. {statusline_result.message}")
    click.echo("\nLocal Shard/Receipt history under .openshard/ was not touched.")
    if any_error:
        raise SystemExit(1)


@cli.group("hooks")
def hooks_group() -> None:
    """Non-interactive hook entrypoints for coding agents (installed by `openshard mcp install claude`)."""


@hooks_group.command("claude")
@click.option(
    "--event",
    "event_override",
    default=None,
    help="Hook event name to assume when the payload carries no hook_event_name.",
)
def hooks_claude(event_override: str | None) -> None:
    """Claude Code hook entrypoint: read one hook payload (JSON) from stdin and record it.

    Observational only. Never prompts, never blocks Claude Code, never
    prints to stdout (Claude Code injects hook stdout into the model's
    context for some events); diagnostics go to stderr. Always exits 0.
    Evidence lands in this repository's .openshard/runs.jsonl as normal
    Shard records readable via `openshard last`, `openshard mcp serve`, etc.
    """
    from openshard.adapters.claude_hooks import run_hook_from_stream

    run_hook_from_stream(sys.stdin, env=os.environ, event_override=event_override)


@hooks_group.command("claude-status")
def hooks_claude_status() -> None:
    """Claude Code status-line entrypoint: read status JSON from stdin, print a status line.

    Configured as this repository's `statusLine` command by `openshard mcp
    install claude` (only when none was already configured). Its stdout IS
    the rendered status line, so this always prints something -- as a side
    effect, model id / cumulative session cost / token counts are recorded
    (best-effort, never blocking) so receipts can show them. Always exits 0.
    """
    from openshard.adapters.claude_hooks import run_status_from_stream

    click.echo(run_status_from_stream(sys.stdin, env=os.environ))


@cli.group("reflect")
def reflect_group() -> None:
    """Post-run reflection and advisory review."""


@reflect_group.command("last")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def reflect_last(as_json: bool) -> None:
    """Show a reflection on the most recent run."""
    from openshard.history.shard_contract import build_shard_receipt
    from openshard.reflection.reflector import build_run_reflection, render_run_reflection

    log_path = Path.cwd() / _LOG_PATH
    entries = _load_run_entries(log_path)
    if not entries:
        if as_json:
            click.echo(json.dumps(_machine_envelope("reflect last", "not_found", reflection=None), indent=2))
        else:
            click.echo("No run history found. Run a task first with 'openshard run'.")
        return
    receipt = build_shard_receipt(entries[-1], index=len(entries) - 1)
    reflection = build_run_reflection(receipt)
    if as_json:
        from dataclasses import asdict

        payload = _machine_envelope(
            "reflect last", "ok", shard_id=receipt.shard_id,
            warnings=list(reflection.warnings), reflection=asdict(reflection),
        )
        click.echo(json.dumps(payload, indent=2))
        return
    for line in render_run_reflection(reflection):
        click.echo(line)


@cli.group("pr", invoke_without_command=True)
@click.pass_context
def pr_group(ctx: click.Context) -> None:
    """PR comment and local export utilities."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@pr_group.command("comment")
@click.option("--output", default=None, help="Write output to this path instead of stdout.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
@click.option("--github-step-summary", "github_step_summary", is_flag=True, default=False,
              help="Append the markdown receipt to $GITHUB_STEP_SUMMARY (GitHub Actions).")
@click.option("--github-output", "github_output", is_flag=True, default=False,
              help="Write safe key=value outputs to $GITHUB_OUTPUT (GitHub Actions).")
def pr_comment(output: str | None, as_json: bool, github_step_summary: bool, github_output: bool) -> None:
    """Generate a GitHub-ready PR comment from the latest OpenShard run.

    The --github-step-summary and --github-output flags write to the files
    referenced by $GITHUB_STEP_SUMMARY and $GITHUB_OUTPUT. This is a local,
    file-based Actions layer only: no GitHub API, no gh, no network, no auth.
    """
    from openshard.github.pr_comment import build_pr_comment_summary, render_pr_comment
    from openshard.history.shard_contract import build_shard_receipt

    log_path = Path.cwd() / _LOG_PATH
    entries = _load_run_entries(log_path)

    if not entries:
        ss_available = ss_written = go_available = go_written = False
        if github_output:
            go_available, go_written = _write_github_output(
                {"openshard_available": "false", "openshard_status": "not_found"}
            )
        if github_step_summary:
            ss_available, ss_written = _write_github_step_summary(
                "## OpenShard\n\nNo OpenShard run found. Run an OpenShard task first."
            )
        if as_json:
            body: dict = {"summary": None}
            if github_step_summary:
                body["github_step_summary_available"] = ss_available
                body["github_step_summary_written"] = ss_written
            if github_output:
                body["github_output_available"] = go_available
                body["github_output_written"] = go_written
            click.echo(json.dumps(_machine_envelope("pr comment", "not_found", **body), indent=2))
        else:
            click.echo("No run history found. Run an OpenShard task first.")
            _warn_missing_github_env(github_step_summary, ss_available, github_output, go_available)
        return

    entry = entries[-1]
    receipt = build_shard_receipt(entry, index=len(entries) - 1)
    summary = build_pr_comment_summary(entry, receipt)
    markdown = render_pr_comment(summary)

    if as_json:
        from dataclasses import asdict

        body = {"summary": asdict(summary)}
        if output:
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(
                _machine_envelope("pr comment", "ok", shard_id=receipt.shard_id,
                                  warnings=list(summary.warnings), summary=asdict(summary)),
                indent=2) + "\n", encoding="utf-8")
            body["written"] = True
            body["output_path_display"] = _safe_output_display(output)
        if github_output:
            go_pairs = _github_output_pairs(
                "ok", receipt.shard_id, summary.manual_review_required,
                output_path_key="openshard_output_path" if output else None,
                output_display=_safe_output_display(output) if output else None,
            )
            go_available, go_written = _write_github_output(go_pairs)
            body["github_output_available"] = go_available
            body["github_output_written"] = go_written
        if github_step_summary:
            ss_available, ss_written = _write_github_step_summary(markdown)
            body["github_step_summary_available"] = ss_available
            body["github_step_summary_written"] = ss_written
        payload = _machine_envelope(
            "pr comment", "ok", shard_id=receipt.shard_id,
            warnings=list(summary.warnings), **body,
        )
        click.echo(json.dumps(payload, indent=2))
        return

    comment_path_display: str | None = None
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        if output_path.exists():
            comment_path_display = _safe_output_display(output)
        click.echo(f"PR comment written to {output}")
    else:
        click.echo(markdown)

    ss_available = go_available = True
    if github_output:
        go_pairs = _github_output_pairs(
            "ok", receipt.shard_id, summary.manual_review_required,
            output_path_key="openshard_comment_path" if comment_path_display else None,
            output_display=comment_path_display,
        )
        go_available, _ = _write_github_output(go_pairs)
    if github_step_summary:
        ss_available, _ = _write_github_step_summary(markdown)
    _warn_missing_github_env(github_step_summary, ss_available, github_output, go_available)


@cli.group("ci", invoke_without_command=True)
@click.pass_context
def ci_group(ctx: click.Context) -> None:
    """CI policy checks over the latest OpenShard run receipt."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@ci_group.command("check")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output (valid JSON only).")
@click.option("--strict", is_flag=True, default=False,
              help="Treat warnings as failures (exit 1).")
@click.option("--github-output", "github_output", is_flag=True, default=False,
              help="Write safe key=value outputs to $GITHUB_OUTPUT (GitHub Actions).")
def ci_check(as_json: bool, strict: bool, github_output: bool) -> None:
    """Evaluate the latest Shard receipt and return a CI verdict.

    Local, deterministic, receipt-based: reduces the most recent OpenShard run
    to pass / warn / fail / skip. No GitHub API, no gh, no network, no auth.
    Exit code is 1 only for fail (and for warnings under --strict); otherwise 0.
    """
    from openshard.ci.policy_check import evaluate_ci_check
    from openshard.history.shard_contract import build_shard_receipt

    log_path = Path.cwd() / _LOG_PATH
    entries = _load_run_entries(log_path)

    if not entries:
        reasons = ["No OpenShard run found to evaluate."]
        if as_json:
            body: dict = {
                "exit_code": 0,
                "reasons": reasons,
                "checks": {
                    "verification": "unknown",
                    "manual_review_required": False,
                    "secret_scan_findings": 0,
                },
            }
            if github_output:
                go_available, go_written = _ci_write_github_output("skip", 0, len(reasons))
                body["github_output_available"] = go_available
                body["github_output_written"] = go_written
            click.echo(json.dumps(_machine_envelope("ci check", "skip", **body), indent=2))
        else:
            click.echo("OpenShard CI Check: skip")
            for reason in reasons:
                click.echo(f"  - {reason}")
            if github_output:
                go_available, _ = _ci_write_github_output("skip", 0, len(reasons))
                if not go_available:
                    click.echo(
                        "warning: GITHUB_OUTPUT is not set; outputs not written.", err=True
                    )
        sys.exit(0)

    entry = entries[-1]
    receipt = build_shard_receipt(entry, index=len(entries) - 1)
    result = evaluate_ci_check(entry, receipt, strict=strict)

    if as_json:
        body = {
            "exit_code": result.exit_code,
            "reasons": result.reasons,
            "checks": result.checks,
        }
        if github_output:
            go_available, go_written = _ci_write_github_output(
                result.status, result.exit_code, len(result.reasons)
            )
            body["github_output_available"] = go_available
            body["github_output_written"] = go_written
        payload = _machine_envelope(
            "ci check", result.status, shard_id=result.shard_id,
            warnings=result.warnings, **body,
        )
        click.echo(json.dumps(payload, indent=2))
        sys.exit(result.exit_code)

    click.echo(f"OpenShard CI Check: {result.status}")
    for reason in result.reasons:
        click.echo(f"  - {reason}")
    for warning in result.warnings:
        click.echo(f"  - {warning}")
    if github_output:
        go_available, _ = _ci_write_github_output(
            result.status, result.exit_code, len(result.reasons)
        )
        if not go_available:
            click.echo("warning: GITHUB_OUTPUT is not set; outputs not written.", err=True)
    sys.exit(result.exit_code)


@cli.group("repo", invoke_without_command=True)
@click.pass_context
def repo_group(ctx: click.Context) -> None:
    """Local repo-map cache: build, reuse, and inspect repo metadata."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@repo_group.command("map")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output (valid JSON only).")
@click.option("--refresh", is_flag=True, default=False,
              help="Rebuild the repo map instead of reusing the cache.")
@click.option("--output", default=None,
              help="Also write the repo-map JSON to this path.")
def repo_map_cmd(as_json: bool, refresh: bool, output: str | None) -> None:
    """Build or load the local repo-map cache for the current directory.

    Local, deterministic, metadata-only: no model calls, no network. Caches under
    .openshard/cache/repo-<fingerprint>.json. A clean git tree reuses the cache; a
    dirty tree always rebuilds; --refresh forces a rebuild. Output and cache contain
    no file contents, raw secrets, or absolute paths.
    """
    from openshard.analysis.repo_map_loader import load_or_build_repo_map

    root = Path.cwd()
    loaded = load_or_build_repo_map(root, refresh=refresh)
    repo_map_dict = loaded.repo_map
    cache_hit = loaded.cache_hit
    cache_display = loaded.cache_path_display
    warnings = loaded.warnings

    output_display: str | None = None
    if output:
        output_path = Path(output)
        if output_path.parent != Path("."):
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(repo_map_dict, indent=2) + "\n", encoding="utf-8")
        output_display = _safe_output_display(output)

    if as_json:
        body: dict = {
            "cache_hit": cache_hit,
            "cache_path_display": cache_display,
            "repo_map": repo_map_dict,
        }
        if output_display is not None:
            body["output_path_display"] = output_display
        payload = _machine_envelope("repo map", "ok", warnings=warnings, **body)
        click.echo(json.dumps(payload, indent=2))
        return

    _render_repo_map(repo_map_dict, cache_hit=cache_hit, cache_path_display=cache_display)
    if output_display is not None:
        click.echo(f"  Wrote: {output_display}")


@repo_group.command("plan")
@click.argument("task")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output (valid JSON only).")
@click.option("--refresh", is_flag=True, default=False,
              help="Rebuild the repo map instead of reusing the cache.")
def repo_plan_cmd(task: str, as_json: bool, refresh: bool) -> None:
    """Produce a read-only, repo-aware plan for TASK from repo-map metadata.

    Local, deterministic, metadata-only: no model calls, no network, no file
    contents read, no source files written, and the task is never executed. Uses
    the repo-map cache (a clean git tree reuses it; a dirty tree or --refresh
    rebuilds). Output contains no file contents, raw secrets, or absolute paths;
    the task argument is sanitised before display.
    """
    from openshard.analysis.repo_map_loader import load_or_build_repo_map
    from openshard.planning.repo_plan import build_repo_aware_plan

    root = Path.cwd()
    loaded = load_or_build_repo_map(root, refresh=refresh)
    plan = build_repo_aware_plan(task, loaded.repo_map, cache_hit=loaded.cache_hit)

    if as_json:
        payload = _machine_envelope(
            "repo plan", "ok", warnings=loaded.warnings, **plan.to_dict()
        )
        click.echo(json.dumps(payload, indent=2))
        return

    render_dict = plan.to_dict()
    render_dict["warnings"] = loaded.warnings
    _render_repo_plan(
        render_dict,
        cache_hit=loaded.cache_hit,
        cache_path_display=loaded.cache_path_display,
    )


@cli.group("stats", invoke_without_command=True)
@click.option("--limit", default=None, type=click.IntRange(min=1),
              help="Aggregate only the most recent N Shards (default: all).")
@click.option("--repo", "repo_filter", default=None,
              help="Only Shards recorded for this repository identity, remote URL, or folder name.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
@click.pass_context
def stats_group(ctx: click.Context, limit: int | None, repo_filter: str | None, as_json: bool) -> None:
    """Honest local counts over this repository's recorded Shards.

    Shards, agents, models (with an explicit unknown bucket), verification
    outcomes, estimated cost, provider-reported tokens, observed duration and
    most-changed files -- all derived from existing receipts, nothing scored
    or inferred. Subcommands give receipt-quality (completeness) and
    failure-category views.
    """
    if ctx.invoked_subcommand is not None:
        return
    from openshard.cli.visibility import no_history_lines, render_stats, stats_json_body
    from openshard.history.query import recent_shards
    from openshard.history.stats import compute_history_stats

    loc = _locate_history()
    page = recent_shards(limit=limit, repo=repo_filter, repo_path=loc.root)
    stats = compute_history_stats(
        page.items,
        total_attempts=page.total_attempts if limit is None else None,
    )

    if as_json:
        status = "ok" if page.items else "not_found"
        click.echo(json.dumps(
            _machine_envelope("stats", status, **stats_json_body(stats, loc, limited_to=limit)), indent=2,
        ))
        return
    if not page.items:
        if repo_filter and loc.runs_path.exists():
            click.echo(f"No Shards recorded for repository filter '{repo_filter}' in {loc.display_name}.")
            return
        for line in no_history_lines(loc):
            click.echo(line)
        return
    for line in render_stats(stats, loc, limited_to=limit):
        click.echo(line)


@stats_group.command("completeness")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output (valid JSON only).")
@click.option("--limit", default=20, type=click.IntRange(min=1),
              help="Evaluate only the most recent N receipts (default 20).")
def stats_completeness(as_json: bool, limit: int) -> None:
    """Score recent Shard receipts for completeness.

    Local, deterministic, receipt-based: reports a completeness heuristic plus
    the fields that are consistently present (strong) or missing (weak) across
    recent runs. No network, no model calls; no secrets or absolute paths leak.
    """
    from openshard.history.completeness import evaluate_completeness
    from openshard.history.shard_contract import build_shard_receipt

    log_path = _locate_history().runs_path
    entries = _load_run_entries(log_path)

    if not entries:
        if as_json:
            payload = _machine_envelope(
                "stats completeness", "not_found",
                runs_checked=0,
                average_score_percent=0,
                field_presence={},
                strong_fields=[],
                weak_fields=[],
                recommendations=[],
                receipts=[],
            )
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo("No run history found. Run 'openshard run' to get started.")
        return

    recent = entries[-limit:]
    receipts = [
        build_shard_receipt(entry, index=len(entries) - len(recent) + offset)
        for offset, entry in enumerate(recent)
    ]
    report = evaluate_completeness(receipts)

    if as_json:
        payload = _machine_envelope(
            "stats completeness", "ok",
            runs_checked=report.runs_checked,
            average_score_percent=report.average_score_percent,
            field_presence=report.field_presence,
            strong_fields=report.strong_fields,
            weak_fields=report.weak_fields,
            recommendations=report.recommendations,
            receipts=[
                {
                    "shard_id": rc.shard_id,
                    "score_percent": rc.score_percent,
                    "present_fields": rc.present_fields,
                    "missing_fields": rc.missing_fields,
                }
                for rc in report.receipts
            ],
        )
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo("\nShard receipt completeness")
    click.echo(f"  runs checked:         {report.runs_checked}")
    click.echo(f"  average completeness: {report.average_score_percent}%")

    if report.strong_fields:
        click.echo(f"\n  strong fields:  {', '.join(report.strong_fields)}")
    if report.weak_fields:
        weak_display = ", ".join(
            f"{name} ({report.field_presence[name]['presence_percent']}%)"
            for name in report.weak_fields
        )
        click.echo(f"  weak fields:    {weak_display}")

    if report.recommendations:
        click.echo("\n  recommendations:")
        for rec in report.recommendations:
            click.echo(f"    - {rec}")


@stats_group.command("failures")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output (valid JSON only).")
@click.option("--limit", default=20, type=click.IntRange(min=1),
              help="Classify only the most recent N receipts (default 20).")
def stats_failures(as_json: bool, limit: int) -> None:
    """Classify recent runs into stable failure categories.

    Local, deterministic, receipt-based: reads existing Shard/run metadata and
    classifies each recent run into one failure category (or no failure). No
    network, no model calls; no secrets, raw error messages, or absolute paths
    leak.
    """
    from openshard.history.failures import evaluate_failures
    from openshard.history.shard_contract import build_shard_receipt

    log_path = _locate_history().runs_path
    entries = _load_run_entries(log_path)

    if not entries:
        if as_json:
            payload = _machine_envelope(
                "stats failures", "not_found",
                runs_checked=0,
                category_counts={},
                top_categories=[],
                recommendations=[],
                failures=[],
            )
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo("No run history found. Run 'openshard run' to get started.")
        return

    recent = entries[-limit:]
    pairs = [
        (entry, build_shard_receipt(entry, index=len(entries) - len(recent) + offset))
        for offset, entry in enumerate(recent)
    ]
    report = evaluate_failures(pairs)

    if as_json:
        payload = _machine_envelope(
            "stats failures", "ok",
            runs_checked=report.runs_checked,
            category_counts=report.category_counts,
            top_categories=report.top_categories,
            recommendations=report.recommendations,
            failures=[
                {
                    "shard_id": fc.shard_id,
                    "category": fc.category,
                    "confidence": fc.confidence,
                    "reasons": fc.reasons,
                    "signals": fc.signals,
                }
                for fc in report.failures
            ],
        )
        click.echo(json.dumps(payload, indent=2))
        return

    failure_total = sum(
        count for name, count in report.category_counts.items()
        if name != "no_failure_detected"
    )

    click.echo("\nFailure taxonomy")
    click.echo(f"  runs checked: {report.runs_checked}")
    click.echo(f"  failures:     {failure_total}")

    if report.top_categories:
        click.echo("\n  top failure categories:")
        for tc in report.top_categories:
            click.echo(f"    {tc['category']:<22}{tc['count']}")

    if report.recommendations:
        click.echo("\n  recommendations:")
        for rec in report.recommendations:
            click.echo(f"    - {rec}")

    if report.failures:
        click.echo("\n  recent failures:")
        for fc in report.failures:
            click.echo(f"    {fc.shard_id:<12}{fc.category} ({fc.confidence})")


@cli.command("apply-last")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be applied without copying files.")
@click.option("--file", "include_files", multiple=True, help="Only apply this relative file path. Can be used multiple times.")
@click.option("--exclude", "exclude_files", multiple=True, help="Exclude this relative file path. Can be used multiple times.")
@click.option("--candidate", "candidate_index", default=None, type=click.IntRange(min=1),
              help="Apply files from a specific candidate (1-based index).")
def apply_last(dry_run: bool, include_files: tuple[str, ...], exclude_files: tuple[str, ...], candidate_index: int | None) -> None:
    """Promote files from the most recent sandbox run into the real repo."""
    from openshard.native.sandbox_apply import (
        apply_sandbox_changes,
        extract_candidate_sandbox_path_from_entry,
        extract_sandbox_path_from_entry,
        filter_sandbox_changed_files,
        list_sandbox_changed_files,
    )

    log_path = Path.cwd() / _LOG_PATH
    entries = _load_run_entries(log_path)
    if not entries:
        click.echo("No run history found. Run a task first with 'openshard run'.")
        return

    entry = entries[-1]

    if entry.get("executor") != "native":
        click.echo("Latest run is not a native run.")
        return

    if candidate_index is not None:
        sandbox_path_str = extract_candidate_sandbox_path_from_entry(entry, candidate_index)
        if not sandbox_path_str:
            click.echo(f"Candidate {candidate_index} has no sandbox path to apply.")
            return
    else:
        sandbox_path_str = extract_sandbox_path_from_entry(entry)
        if not sandbox_path_str:
            click.echo("Latest native run has no sandbox path to apply.")
            return

    include = list(include_files) or None
    exclude = list(exclude_files) or None

    sandbox_path = Path(sandbox_path_str)
    all_files = list_sandbox_changed_files(Path.cwd(), sandbox_path)
    files = filter_sandbox_changed_files(all_files, include=include, exclude=exclude)

    if not files:
        if include or exclude:
            click.echo("No sandbox changes matched the apply selection.")
        else:
            click.echo("No sandbox changes to apply.")
        log_sandbox_apply_receipt(SandboxApplyReceipt(
            source_run_id=entry.get("timestamp", ""),
            sandbox_path=str(sandbox_path),
            applied=False,
            files_applied=[],
            files_skipped=[],
            dry_run=False,
            reason="No sandbox changes to apply.",
        ))
        return

    click.echo(f"Sandbox: {sandbox_path_str}")
    click.echo("")
    if dry_run:
        click.echo(f"Would apply {len(files)} file(s):")
        for f in files:
            click.echo(f"  - {f}")
        log_sandbox_apply_receipt(SandboxApplyReceipt(
            source_run_id=entry.get("timestamp", ""),
            sandbox_path=str(sandbox_path),
            applied=False,
            files_applied=[],
            files_skipped=[],
            dry_run=True,
            reason="dry run",
        ))
        return

    click.echo(f"Files to apply ({len(files)}):")
    for f in files:
        click.echo(f"  - {f}")
    click.echo("")

    result = apply_sandbox_changes(Path.cwd(), sandbox_path, include=include, exclude=exclude)

    log_sandbox_apply_receipt(SandboxApplyReceipt(
        source_run_id=entry.get("timestamp", ""),
        sandbox_path=str(sandbox_path),
        applied=result.applied,
        files_applied=list(result.files_applied),
        files_skipped=list(result.files_skipped),
        dry_run=False,
        reason=result.reason,
    ))

    if result.reason and not result.files_applied:
        raise click.ClickException(result.reason)

    click.echo(f"Applied {len(result.files_applied)} file(s) from sandbox.")
    for f in result.files_applied:
        click.echo(f"  - {f}")
    if result.files_skipped:
        click.echo(f"Skipped {len(result.files_skipped)} file(s).")
        for f in result.files_skipped:
            click.echo(f"  [skipped] {f}")


@cli.command("candidates-last")
def candidates_last() -> None:
    """Show the candidate table for the most recent native run."""
    from openshard.native.sandbox_apply import get_candidate_records_from_entry

    log_path = Path.cwd() / _LOG_PATH
    entries = _load_run_entries(log_path)
    if not entries:
        click.echo("No run history found.")
        return

    entry = entries[-1]
    if entry.get("executor") != "native":
        click.echo("Latest run is not a native run.")
        return

    records = get_candidate_records_from_entry(entry)
    if not records:
        click.echo("No candidate summary available.")
        return

    click.echo(f"{'Candidate':<10} {'Status':<10} {'Selected':<10} {'Score':<8} {'Files':<7} Exit")
    for r in records:
        idx = r.get("candidate_index", "?")
        status = r.get("verification_status", "")
        selected = "yes" if r.get("selected") else "no"
        score = r.get("score", 0.0)
        score_s = f"{score:.1f}"
        files = len(r.get("files_written") or [])
        ec = r.get("exit_code")
        exit_s = str(ec) if ec is not None else "-"
        click.echo(f"{idx:<10} {status:<10} {selected:<10} {score_s:<8} {files:<7} {exit_s}")


@cli.command("diff-last")
@click.option("--full", "show_full", is_flag=True, default=False,
              help="Show full sandbox diff, not just stat summary.")
@click.option("--candidate", "candidate_index", default=None, type=click.IntRange(min=1),
              help="Show diff for a specific candidate (1-based index).")
def diff_last(show_full: bool, candidate_index: int | None) -> None:
    """Preview the diff from the most recent native sandbox run."""
    from openshard.native.sandbox_apply import (
        extract_candidate_sandbox_path_from_entry,
        extract_sandbox_path_from_entry,
    )
    from openshard.native.sandbox_diff import get_sandbox_diff

    log_path = Path.cwd() / _LOG_PATH
    if not log_path.exists():
        click.echo("No run history found.")
        return

    entries = _load_run_entries(log_path)
    if not entries:
        click.echo("No runs recorded yet.")
        return

    entry = entries[-1]

    if entry.get("executor") != "native":
        click.echo("Latest run is not a native run.")
        return

    if candidate_index is not None:
        sandbox_path_str = extract_candidate_sandbox_path_from_entry(entry, candidate_index)
        if not sandbox_path_str:
            click.echo(f"Candidate {candidate_index} has no sandbox path to diff.")
            return
    else:
        sandbox_path_str = extract_sandbox_path_from_entry(entry)
        if not sandbox_path_str:
            click.echo("Latest native run has no sandbox path to diff.")
            return

    result = get_sandbox_diff(Path(sandbox_path_str), full=show_full)

    if not result.available:
        click.echo(f"No sandbox diff available: {result.reason}")
        return

    click.echo(f"Sandbox: {sandbox_path_str}")
    click.echo(f"Changed files ({len(result.files_changed)}):")
    for f in result.files_changed:
        click.echo(f"  - {f}")

    if result.stat_text:
        click.echo("")
        click.echo("Diff stat:")
        click.echo(result.stat_text)

    if show_full and result.diff_text:
        click.echo("")
        click.echo("Diff:")
        click.echo(result.diff_text)


@cli.command("apply-receipts")
@click.option("--last", "last_n", default=10, type=click.IntRange(min=1), show_default=True)
def apply_receipts_cmd(last_n: int) -> None:
    """Show recent sandbox apply receipts."""
    receipts = recent_sandbox_apply_receipts(limit=last_n)
    if not receipts:
        click.echo("No sandbox apply receipts recorded yet.")
        return

    header = f"{'Time':<18} {'Applied':<8} {'Skipped':<8} {'Dry Run':<8} Sandbox"
    click.echo(header)
    for r in receipts:
        ts = r.timestamp[:16].replace("T", " ")
        dry = "yes" if r.dry_run else "no"
        click.echo(f"{ts:<18} {r.applied_count:<8} {r.skipped_count:<8} {dry:<8} {r.sandbox_path}")


@cli.command("checkpoints")
@click.option("--last", "last_n", default=10, type=click.IntRange(min=1), show_default=True)
def checkpoints_cmd(last_n: int) -> None:
    """Show recent native run checkpoints."""
    from openshard.history.run_checkpoints import recent_run_checkpoints
    events = recent_run_checkpoints(limit=last_n)
    if not events:
        click.echo("No run checkpoints recorded yet.")
        return
    header = f"{'Time':<18} {'Run':<22} {'Stage':<14} {'Status':<10} {'Verify':<10} Retry"
    click.echo(header)
    for evt in events:
        ts = evt.timestamp[:16].replace("T", " ")
        run_short = evt.run_id[:20] if evt.run_id else "-"
        retry_s = "yes" if evt.retry_used else "no"
        verify_s = evt.verification_status or "-"
        click.echo(f"{ts:<18} {run_short:<22} {evt.stage:<14} {evt.status:<10} {verify_s:<10} {retry_s}")


@cli.command("resume-last")
def resume_last() -> None:
    """Show safe resume guidance for the most recent native run (v0)."""
    from openshard.history.run_checkpoints import run_checkpoints_for_run
    from openshard.native.sandbox_apply import extract_sandbox_path_from_entry

    log_path = Path.cwd() / _LOG_PATH
    entries = _load_run_entries(log_path)
    if not entries:
        click.echo("No run history found.")
        return

    entry = entries[-1]
    if entry.get("executor") != "native":
        click.echo("Latest run is not a native run.")
        return

    run_id = entry.get("timestamp", "")
    checkpoints = run_checkpoints_for_run(run_id)
    if not checkpoints:
        click.echo("No checkpoints found for latest native run.")
        return

    latest = checkpoints[-1]
    click.echo(f"Latest checkpoint: {latest.stage} ({latest.status})")

    sandbox_path_str = extract_sandbox_path_from_entry(entry)
    if sandbox_path_str and Path(sandbox_path_str).exists():
        click.echo("\nResume options:")
        click.echo("  openshard diff-last --full")
        click.echo("  openshard apply-last --dry-run")
        click.echo("  openshard apply-last")
    else:
        click.echo("\nNo live sandbox found. This run can be inspected, but not resumed from workspace.")


def _load_run_entries(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    entries: list[dict] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _content_hash_fields(entry: dict) -> dict:
    """Compact content-hash verification block for --json envelopes.

    Returns ``content_hash`` (stored value or None) and ``content_hash_status``
    ("valid" | "mismatch" | "missing"). ``computed_content_hash`` is included
    only when the stored hash is absent or doesn't match, so the common valid
    case stays compact and free of redundant duplicate hashes.
    """
    from openshard.history.shard_hash import verify_shard_hash

    result = verify_shard_hash(entry)
    fields: dict = {
        "content_hash": result["stored_hash"],
        "content_hash_status": result["status"],
    }
    if result["status"] != "valid":
        fields["computed_content_hash"] = result["computed_hash"]
    return fields


def _interaction_event_types(run_id: str) -> list[str]:
    """Load sanitised developer-interaction event *types* for a run. Never raises.

    Returns types only (e.g. ``"unsafe_command"``) — never raw summaries — so the
    trust evaluator stays leak-free. Missing/corrupt interaction files yield ``[]``.
    """
    if not run_id:
        return []
    try:
        from openshard.history.interactions import interaction_events_for_run

        return [e.event_type for e in interaction_events_for_run(run_id)]
    except Exception:
        return []


# Schema version for the machine-readable (--json) output contract. Bump only on
# breaking envelope changes; the underlying structures keep their own source/version.
_MACHINE_OUTPUT_SCHEMA_VERSION = "1"


def _machine_envelope(
    command: str,
    status: str,
    shard_id: str | None = None,
    warnings: list[str] | None = None,
    **body: object,
) -> dict:
    """Build the stable machine-readable envelope shared by all --json commands."""
    return {
        "schema_version": _MACHINE_OUTPUT_SCHEMA_VERSION,
        "command": command,
        "status": status,
        "shard_id": shard_id,
        "warnings": warnings or [],
        **body,
    }


def _safe_output_display(output: str) -> str:
    """Return a path safe to echo in JSON: relative as-is, absolute -> bare filename."""
    p = output.replace("\\", "/")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        return Path(output).name
    return output


def _write_github_step_summary(markdown: str) -> tuple[bool, bool]:
    """Append markdown to $GITHUB_STEP_SUMMARY. Returns (available, written).

    available is True when the env var is set; written is True when the append
    succeeded. No network, no secrets - markdown is already sanitized upstream.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return (False, False)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown.rstrip("\n") + "\n")
        return (True, True)
    except OSError:
        return (True, False)


def _write_github_output(pairs: dict[str, str]) -> tuple[bool, bool]:
    """Append safe key=value lines to $GITHUB_OUTPUT. Returns (available, written).

    Values are coerced to str and stripped of CR/LF so each output stays on a
    single line (no heredoc / invalid-character cases). Keys are fixed literals.
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return (False, False)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in pairs.items():
                safe_value = str(value).replace("\r", " ").replace("\n", " ")
                fh.write(f"{key}={safe_value}\n")
        return (True, True)
    except OSError:
        return (True, False)


def _github_output_pairs(
    status: str,
    shard_id: str | None,
    manual_review_required: bool | None,
    output_path_key: str | None = None,
    output_display: str | None = None,
) -> dict[str, str]:
    """Build the safe scalar key=value map for $GITHUB_OUTPUT."""
    pairs: dict[str, str] = {
        "openshard_available": "true" if status == "ok" else "false",
        "openshard_status": status,
    }
    if status == "ok":
        if shard_id:
            pairs["openshard_shard_id"] = shard_id
        if manual_review_required is not None:
            pairs["openshard_manual_review_required"] = "true" if manual_review_required else "false"
        if output_path_key and output_display:
            pairs[output_path_key] = output_display
    return pairs


def _ci_write_github_output(
    status: str, exit_code: int, reasons_count: int
) -> tuple[bool, bool]:
    """Write the CI-check scalar outputs to $GITHUB_OUTPUT. Returns (available, written)."""
    return _write_github_output(
        {
            "openshard_ci_status": status,
            "openshard_ci_exit_code": str(exit_code),
            "openshard_ci_reasons_count": str(reasons_count),
        }
    )


def _warn_missing_github_env(
    github_step_summary: bool,
    ss_available: bool,
    github_output: bool,
    go_available: bool,
) -> None:
    """Warn on stderr (human mode only) when a requested GitHub env var is unset."""
    if github_step_summary and not ss_available:
        click.echo("warning: GITHUB_STEP_SUMMARY is not set; step summary not written.", err=True)
    if github_output and not go_available:
        click.echo("warning: GITHUB_OUTPUT is not set; outputs not written.", err=True)


def _record_feedback(
    outcome: str,
    reason: str | None,
    edited: bool,
    manual_fix_required: bool,
    ci_passed: bool,
    ci_failed: bool,
    pr_created: bool,
    pr_merged: bool,
) -> None:
    log_path = Path.cwd() / _LOG_PATH
    if not log_path.exists():
        raise click.ClickException("No run history found. Run a task first with 'openshard run'.")
    entries = _load_run_entries(log_path)
    if not entries:
        raise click.ClickException("No run history found. Run a task first with 'openshard run'.")
    df: dict = {
        "schema_version": 1,
        "outcome": outcome,
        "reason": reason or None,
        "edited": edited,
        "manual_fix_required": manual_fix_required,
        "ci_passed": ci_passed,
        "ci_failed": ci_failed,
        "pr_created": pr_created,
        "pr_merged": pr_merged,
        "recorded_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "source": "cli",
    }
    entries[-1]["developer_feedback"] = df
    write_jsonl(log_path, entries)
    try:
        from openshard.history.interactions import DeveloperInteractionEvent, log_interaction_event
        _event_type_map = {
            "accepted": "feedback_accepted",
            "rejected": "feedback_rejected",
            "needs-retry": "feedback_retried",
            "noted": "feedback_noted",
        }
        _accepted_map: dict[str, bool] = {
            "accepted": True,
            "rejected": False,
            "needs-retry": False,
        }
        _run_id = entries[-1].get("timestamp") or ""
        _evt = DeveloperInteractionEvent(
            run_id=_run_id,
            event_type=_event_type_map.get(outcome, "feedback_noted"),
            summary=f"feedback outcome={outcome}",
            correction_reason=reason,
            accepted=_accepted_map.get(outcome),
            metadata={"edited": edited, "ci_passed": ci_passed, "ci_failed": ci_failed},
        )
        log_interaction_event(_evt)
    except Exception:
        pass
    try:
        from openshard.history.memory import build_memory_entry, log_memory_entry
        _mem = build_memory_entry(entries[-1], outcome, reason)
        log_memory_entry(_mem)
    except Exception:
        pass


@cli.group("feedback")
def feedback_group() -> None:
    """Record developer feedback for the most recent run."""


@feedback_group.command("accept")
@click.option("--edited", is_flag=True, default=False, help="You edited the output manually.")
@click.option("--ci-passed", is_flag=True, default=False, help="CI passed after this run.")
@click.option("--ci-failed", is_flag=True, default=False, help="CI failed after this run.")
@click.option("--pr-created", is_flag=True, default=False, help="A PR was created from this run.")
@click.option("--pr-merged", is_flag=True, default=False, help="The PR was merged.")
def feedback_accept(
    edited: bool,
    ci_passed: bool,
    ci_failed: bool,
    pr_created: bool,
    pr_merged: bool,
) -> None:
    """Mark the most recent run as accepted."""
    _record_feedback(
        outcome="accepted",
        reason=None,
        edited=edited,
        manual_fix_required=False,
        ci_passed=ci_passed,
        ci_failed=ci_failed,
        pr_created=pr_created,
        pr_merged=pr_merged,
    )
    click.echo("Feedback recorded: accepted")


@feedback_group.command("reject")
@click.option("--reason", default=None, help="Why the run was rejected.")
@click.option("--edited", is_flag=True, default=False, help="You edited the output manually.")
@click.option("--manual-fix-required", is_flag=True, default=False, help="A manual fix was required.")
@click.option("--ci-failed", is_flag=True, default=False, help="CI failed after this run.")
def feedback_reject(
    reason: str | None,
    edited: bool,
    manual_fix_required: bool,
    ci_failed: bool,
) -> None:
    """Mark the most recent run as rejected."""
    if not reason:
        click.echo("Tip: add --reason to help improve future routing.")
    _record_feedback(
        outcome="rejected",
        reason=reason,
        edited=edited,
        manual_fix_required=manual_fix_required,
        ci_passed=False,
        ci_failed=ci_failed,
        pr_created=False,
        pr_merged=False,
    )
    click.echo("Feedback recorded: rejected")


@feedback_group.command("retry")
@click.option("--reason", default=None, help="Why a retry was needed.")
@click.option("--manual-fix-required", is_flag=True, default=False, help="A manual fix was required.")
@click.option("--ci-failed", is_flag=True, default=False, help="CI failed after this run.")
def feedback_retry(
    reason: str | None,
    manual_fix_required: bool,
    ci_failed: bool,
) -> None:
    """Mark the most recent run as needing a retry."""
    if not reason:
        click.echo("Tip: add --reason to help improve future routing.")
    _record_feedback(
        outcome="needs-retry",
        reason=reason,
        edited=False,
        manual_fix_required=manual_fix_required,
        ci_passed=False,
        ci_failed=ci_failed,
        pr_created=False,
        pr_merged=False,
    )
    click.echo("Feedback recorded: needs-retry")


@feedback_group.command("note")
@click.argument("text")
def feedback_note(text: str) -> None:
    """Add a free-text note to the most recent run."""
    _record_feedback(
        outcome="noted",
        reason=text,
        edited=False,
        manual_fix_required=False,
        ci_passed=False,
        ci_failed=False,
        pr_created=False,
        pr_merged=False,
    )
    click.echo("Note recorded.")


@cli.group("memory", invoke_without_command=True)
@click.pass_context
def memory_group(ctx: click.Context) -> None:
    """Browse what OpenShard has learned from your feedback."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@memory_group.command("list")
@click.option("--last", "last_n", default=10, show_default=True, type=int, help="Number of entries to show.")
def memory_list(last_n: int) -> None:
    """Show the most recent memory entries."""
    from openshard.history.memory import load_memory_entries

    entries = load_memory_entries()
    if not entries:
        click.echo("No memory entries yet. Give feedback after runs to start building memory.")
        return
    for entry in entries[-last_n:][::-1]:
        click.echo(f"{entry.recorded_at}  {entry.outcome:<12}  {entry.task_short}")


@memory_group.command("stats")
def memory_stats() -> None:
    """Show counts of memory entries by outcome."""
    from collections import Counter

    from openshard.history.memory import load_memory_entries

    entries = load_memory_entries()
    if not entries:
        click.echo("No memory entries yet.")
        return
    counts: Counter[str] = Counter(e.outcome for e in entries)
    click.echo(f"Total entries: {len(entries)}")
    click.echo(f"Accepted:      {counts.get('accepted', 0)}")
    click.echo(f"Rejected:      {counts.get('rejected', 0)}")
    click.echo(f"Needs-retry:   {counts.get('needs-retry', 0)}")
    rejected_reasons = [e.reason for e in entries if e.outcome == "rejected" and e.reason]
    if rejected_reasons:
        most_common_reason = Counter(rejected_reasons).most_common(1)[0][0]
        click.echo(f"Top rejection reason: {most_common_reason}")


@cli.command("note")
@click.argument("text")
def note_cmd(text: str) -> None:
    """Attach a note to the most recent run."""
    from openshard.security.secret_scan import scrub_text_for_secrets

    log_path = Path.cwd() / _LOG_PATH
    if not log_path.exists():
        click.echo("No run history found.")
        raise SystemExit(1)
    entries = _load_run_entries(log_path)
    if not entries:
        click.echo("No run history found.")
        raise SystemExit(1)
    scrubbed, _ = scrub_text_for_secrets(text[:500], source_label="<note>")
    note_item = {
        "text": scrubbed,
        "recorded_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "schema_version": 1,
    }
    existing = entries[-1].get("notes")
    if isinstance(existing, list) and all(isinstance(n, dict) for n in existing):
        existing.append(note_item)
        entries[-1]["notes"] = existing
    else:
        entries[-1]["notes"] = [note_item]
    write_jsonl(log_path, entries)
    click.echo("Note recorded.")


@cli.command("feedback-stats")
def feedback_stats() -> None:
    """Show local developer feedback stats."""
    log_path = Path.cwd() / _LOG_PATH
    entries = _load_run_entries(log_path)

    if not log_path.exists():
        click.echo("No run history found. Run a task first with 'openshard run'.")
        return

    if not entries:
        click.echo("No runs recorded yet.")
        return

    total = len(entries)
    feedback_entries = [e for e in entries if isinstance(e.get("feedback"), dict)]
    feedback_count = len(feedback_entries)

    if feedback_count == 0:
        click.echo("Developer feedback\n")
        click.echo(f"Runs: {total}")
        click.echo("Feedback: 0 recorded")
        click.echo("\nTip: add feedback with 'openshard feedback --rating good'")
        return

    counts = {"good": 0, "mixed": 0, "bad": 0}
    by_model: dict[str, dict[str, int]] = {}
    action_counts: dict[str, int] = {
        "accepted": 0, "rejected": 0, "edited": 0,
        "retried": 0, "partially-accepted": 0, "unknown": 0,
    }
    reason_counts: dict[str, int] = {}
    by_category: dict[str, dict[str, int]] = {}
    for entry in feedback_entries:
        fb = entry["feedback"]
        rating = fb.get("rating", "")
        if rating in counts:
            counts[rating] += 1
        raw_model = entry.get("execution_model") or entry.get("model") or "unknown"
        label = _model_label(raw_model)
        bucket = by_model.setdefault(label, {"good": 0, "mixed": 0, "bad": 0})
        if rating in bucket:
            bucket[rating] += 1
        action = fb.get("action")
        if action in action_counts:
            action_counts[action] += 1
        reason = fb.get("correction_reason")
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        category = entry.get("routing_category")
        if category:
            cat_bucket = by_category.setdefault(category, {"good": 0, "mixed": 0, "bad": 0})
            if rating in cat_bucket:
                cat_bucket[rating] += 1

    percent = round(feedback_count / total * 100)

    click.echo("Developer feedback\n")
    click.echo(f"Runs: {total}")
    click.echo(f"Feedback: {feedback_count} recorded ({percent}%)")
    click.echo(f"Good:  {counts['good']}")
    click.echo(f"Mixed: {counts['mixed']}")
    click.echo(f"Bad:   {counts['bad']}")

    click.echo("\nBy model")
    for lbl, bucket in by_model.items():
        click.echo(f"  {lbl}: good={bucket['good']} mixed={bucket['mixed']} bad={bucket['bad']}")

    shown_actions = [(a, c) for a, c in action_counts.items() if c > 0]
    if shown_actions:
        click.echo("\nBy action")
        for action, count in shown_actions:
            click.echo(f"  {action}: {count}")

    if reason_counts:
        click.echo("\nCorrection reasons")
        for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
            click.echo(f"  {reason}: {count}")

    if by_category:
        click.echo("\nBy category")
        for cat, cat_bucket in by_category.items():
            click.echo(f"  {cat}: good={cat_bucket['good']} mixed={cat_bucket['mixed']} bad={cat_bucket['bad']}")

    notes = [
        (e["feedback"].get("rating", ""), e["feedback"].get("note", ""))
        for e in reversed(feedback_entries)
        if e["feedback"].get("note", "").strip()
    ][:5]

    if notes:
        click.echo("\nRecent notes")
        for rating, note in notes:
            click.echo(f"  {rating} — {note}")


def _baseline_export_fields(
    prompt_tokens: int,
    completion_tokens: int,
    actual_cost: float | None,
) -> dict:
    from openshard.cost.baseline import compute_baseline_comparison
    cmp = compute_baseline_comparison(prompt_tokens, completion_tokens, actual_cost)
    if cmp is None:
        return {
            "frontier_baseline_cost_usd": None,
            "estimated_saving_usd": None,
            "estimated_saving_percent": None,
        }
    return {
        "frontier_baseline_cost_usd": cmp["frontier_baseline_cost_usd"],
        "estimated_saving_usd": cmp["estimated_saving_usd"],
        "estimated_saving_percent": cmp["estimated_saving_percent"],
    }


def _export_verification_block(receipt) -> dict:  # receipt: ShardReceipt | None
    """Compact, safe verification block for machine output.

    Uses the canonical verification signal from the receipt. Contains only
    bounded scalars and a safe summary reason, never raw command output.
    """
    from openshard.history.proof_signals import verification_status_from_receipt

    if receipt is None:
        return {
            "status": None,
            "reason": "",
            "returncode": None,
            "duration_seconds": None,
            "raw_output_stored": False,
        }
    return {
        "status": verification_status_from_receipt(receipt),
        "reason": getattr(receipt, "verification_reason", "") or "",
        "returncode": getattr(receipt, "verification_returncode", None),
        "duration_seconds": getattr(receipt, "verification_duration_seconds", None),
        "raw_output_stored": bool(getattr(receipt, "verification_raw_output_stored", False)),
    }


def _routing_truth_export(entry: dict) -> dict:
    """Honest routing-truth block for JSON export. Recomputed so old records
    (written before routing_truth was persisted) also get it. Never raises."""
    try:
        from openshard.history.routing_truth import (
            build_routing_truth,
            routing_truth_to_dict,
        )
        return routing_truth_to_dict(build_routing_truth(entry))
    except Exception:
        return {}


def _export_run_entry(entry: dict, include_notes: bool = False, include_timeline: bool = False, receipt=None) -> dict:  # receipt: ShardReceipt | None
    stage_runs = entry.get("stage_runs") or []
    is_ro = entry.get("routing_rationale") == "read-only analysis"

    def _stage_key(sr: dict) -> str | None:
        return sr.get("stage_type") or sr.get("stage")

    _ANALYSIS_STAGE_TYPES = {"analysis", "implementation", "execution", "work"}

    planning_model = next(
        (sr.get("model") for sr in stage_runs if _stage_key(sr) == "planning"), None
    )
    analysis_model = next(
        (sr.get("model") for sr in stage_runs if _stage_key(sr) in _ANALYSIS_STAGE_TYPES), None
    )

    feedback = entry.get("feedback") or {}
    tdr = entry.get("tier_dispatch_receipt") or {}

    row: dict = {
        "task":                      entry.get("task"),
        "timestamp":                 entry.get("timestamp"),
        "workflow":                  entry.get("workflow"),
        "execution_model":           entry.get("execution_model"),
        "planning_model":            planning_model,
        "analysis_model":            analysis_model,
        "routing_category":          entry.get("routing_category"),
        "routing_rationale":         entry.get("routing_rationale"),
        "routing_selected_model":    entry.get("routing_selected_model"),
        "routing_selected_provider": entry.get("routing_selected_provider"),
        "execution_profile":         entry.get("execution_profile"),
        "execution_mode_label":      _profile_display_label(entry.get("execution_profile"), is_readonly=is_ro),
        "verification_attempted":    entry.get("verification_attempted"),
        "verification_passed":       entry.get("verification_passed"),
        "verification":              _export_verification_block(receipt),
        "duration_seconds":          entry.get("duration_seconds"),
        "total_cost_usd":            entry.get("estimated_cost"),
        "prompt_tokens":             entry.get("prompt_tokens"),
        "completion_tokens":         entry.get("completion_tokens"),
        "total_tokens":              entry.get("total_tokens"),
        "files_created":             entry.get("files_created"),
        "files_updated":             entry.get("files_updated"),
        "files_deleted":             entry.get("files_deleted"),
        "feedback_rating":           feedback.get("rating"),
        "feedback_note":             feedback.get("note"),
        "feedback_action":           feedback.get("action"),
        "correction_reason":         feedback.get("correction_reason"),
        "tier_dispatch_enabled":     tdr.get("enabled"),
        "tier_dispatch_applied":     tdr.get("applied"),
        "tier_dispatch_work_model":  tdr.get("executor_model"),
        "routing_truth":             _routing_truth_export(entry),
        "summary":                   entry.get("summary"),
        "import_source":             entry.get("import_source"),
        "import_method":             entry.get("import_method"),
        "executor":                  entry.get("executor"),
        **_baseline_export_fields(
            entry.get("prompt_tokens") or 0,
            entry.get("completion_tokens") or 0,
            entry.get("estimated_cost"),
        ),
    }
    if include_notes:
        row["notes"] = entry.get("notes") or []
    if include_timeline:
        from openshard.run.timeline import project_timeline_for_export
        row["timeline"] = project_timeline_for_export(entry.get("run_timeline") or [])
    if receipt is not None:
        from dataclasses import asdict as _asdict
        row["provenance"] = [_asdict(p) for p in receipt.provenance]
        row["events"] = [_asdict(e) for e in receipt.events]
    else:
        row["provenance"] = []
        row["events"] = []
    return row


def _render_export_preview(rows: list[dict]) -> None:
    _TW, _MW, _MDW, _CW, _SW = 21, 10, 11, 10, 10
    click.echo("Export preview")
    click.echo(f"\nRuns: {len(rows)}\n")
    click.echo(
        "Time".ljust(_TW) + "Mode".ljust(_MW) + "Model".ljust(_MDW)
        + "Cost".ljust(_CW) + "Saving".ljust(_SW) + "Feedback"
    )
    for row in rows:
        ts = (row.get("timestamp") or "").rstrip("Z").replace("T", " ")[:16]
        mode = row.get("execution_mode_label") or "-"
        model_raw = row.get("execution_model") or ""
        model = _model_label(model_raw) if model_raw else "-"
        cost = row.get("total_cost_usd")
        cost_s = f"${cost:.4f}" if cost is not None else "-"
        pct = row.get("estimated_saving_percent")
        saving_s = f"{pct}%" if pct is not None else "-"
        feedback = row.get("feedback_rating") or "-"
        click.echo(
            ts.ljust(_TW) + mode.ljust(_MW) + model.ljust(_MDW)
            + cost_s.ljust(_CW) + saving_s.ljust(_SW) + feedback
        )


@cli.command("export-runs")
@click.option("--output", default=None, help="Write JSONL to this path instead of stdout.")
@click.option("--limit", default=None, type=click.IntRange(min=1), help="Export most recent N entries.")
@click.option("--with-notes", is_flag=True, default=False, help="Include run notes in export.")
@click.option("--preview", is_flag=True, default=False, help="Print a human-readable table instead of JSONL.")
def export_runs(output: str | None, limit: int | None, with_notes: bool, preview: bool) -> None:
    """Export run history as clean JSONL for eval analysis and review."""
    if preview and output:
        raise click.UsageError("--preview and --output cannot be used together; preview is terminal-only.")
    log_path = Path.cwd() / _LOG_PATH
    if not log_path.exists():
        click.echo("No run history found. Run a task first with 'openshard run'.")
        return
    entries = _load_run_entries(log_path)
    if not entries:
        click.echo("No runs recorded yet.")
        return
    if limit is not None:
        entries = entries[-limit:]
    rows = [_export_run_entry(e, include_notes=with_notes) for e in entries]
    if preview:
        _render_export_preview(rows)
        return
    lines = "\n".join(json.dumps(r) for r in rows)
    if output:
        output_path = Path(output)
        if output_path.parent != Path("."):
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(lines + "\n", encoding="utf-8")
    else:
        click.echo(lines)


@cli.command("interactions")
@click.option(
    "--last",
    "last_n",
    default=10,
    type=click.IntRange(min=1),
    show_default=True,
    help="Show the most recent N interaction events.",
)
def interactions_cmd(last_n: int) -> None:
    """Show recent developer interaction events."""
    from openshard.history.interactions import load_interaction_events
    events = load_interaction_events()
    if not events:
        click.echo("No interaction events recorded yet.")
        return
    recent = events[-last_n:]
    _TW, _ETW, _SW = 20, 30, 8
    click.echo("Time".ljust(_TW) + "Event Type".ljust(_ETW) + "Accept".ljust(_SW) + "Summary")
    for evt in recent:
        ts = (evt.timestamp or "").rstrip("Z").replace("T", " ")[:16]
        etype = (evt.event_type or "-")[:_ETW - 1]
        accepted = (
            "yes" if evt.accepted is True
            else "no" if evt.accepted is False
            else "-"
        )
        summary = (evt.summary or "")[:60]
        click.echo(ts.ljust(_TW) + etype.ljust(_ETW) + accepted.ljust(_SW) + summary)


@cli.command("export-interactions")
@click.option("--output", default=None, help="Write JSONL to this path instead of stdout.")
@click.option(
    "--redacted",
    is_flag=True,
    default=False,
    help="Replace summary with '[redacted]' and metadata with {}.",
)
def export_interactions(output: str | None, redacted: bool) -> None:
    """Export developer interaction events as JSONL."""
    from openshard.history.interactions import _event_to_dict, load_interaction_events
    events = load_interaction_events()
    if not events:
        click.echo("No interaction events recorded yet.")
        return
    rows: list[dict] = []
    for evt in events:
        d = _event_to_dict(evt)
        if redacted:
            d["summary"] = "[redacted]"
            d["correction_reason"] = None
            d["related_file_paths"] = []
            d["metadata"] = {}
        rows.append(d)
    lines = "\n".join(json.dumps(r) for r in rows)
    if output:
        output_path = Path(output)
        if output_path.parent != Path("."):
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(lines + "\n", encoding="utf-8")
    else:
        click.echo(lines)


@cli.command("failure-memory")
@click.option(
    "--last",
    "last_n",
    default=10,
    type=click.IntRange(min=1),
    show_default=True,
    help="Show the most recent N failure memory events.",
)
def failure_memory_cmd(last_n: int) -> None:
    """Show recent native verification failure events."""
    from openshard.history.failure_memory import recent_failure_memory
    events = recent_failure_memory(limit=last_n)
    if not events:
        click.echo("No failure memory events recorded yet.")
        return
    _TW, _FTW, _MW = 20, 18, 8
    click.echo(
        "Time".ljust(_TW) + "Failure Type".ljust(_FTW)
        + "Exit".ljust(_MW) + "Retry".ljust(_MW) + "Task"
    )
    for evt in events:
        ts = (evt.timestamp or "").rstrip("Z").replace("T", " ")[:16]
        ftype = (evt.failure_type or "-")[:_FTW - 1]
        retry_s = "yes" if evt.retry_attempted else "no"
        task_s = (evt.task_summary or "")[:50]
        click.echo(
            ts.ljust(_TW) + ftype.ljust(_FTW)
            + str(evt.exit_code).ljust(_MW) + retry_s.ljust(_MW) + task_s
        )


@cli.command("export-failure-memory")
@click.option("--output", default=None, help="Write JSONL to this path instead of stdout.")
@click.option(
    "--redacted",
    is_flag=True,
    default=False,
    help="Replace task_summary with '[redacted]' and model with 'redacted'.",
)
def export_failure_memory(output: str | None, redacted: bool) -> None:
    """Export native failure memory events as JSONL."""
    from openshard.history.failure_memory import _event_to_dict, load_failure_memory_events
    events = load_failure_memory_events()
    if not events:
        click.echo("No failure memory events recorded yet.")
        return
    rows: list[dict] = []
    for evt in events:
        d = _event_to_dict(evt)
        if redacted:
            d["task_summary"] = "[redacted]"
            d["model"] = "redacted"
        rows.append(d)
    lines = "\n".join(json.dumps(r) for r in rows)
    if output:
        output_path = Path(output)
        if output_path.parent != Path("."):
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(lines + "\n", encoding="utf-8")
    else:
        click.echo(lines)


def _demo_default() -> None:
    click.echo("OpenShard demo")
    click.echo("")
    click.echo("This walkthrough shows the main steps OpenShard can follow during a task.")
    click.echo("")
    click.echo("1. Understand the task")
    click.echo("   OpenShard reads your task prompt and decides whether it involves")
    click.echo("   reading (questions, explanations) or writing (code changes, new files).")
    click.echo("")
    click.echo("2. Choose a workflow")
    click.echo("   A workflow is selected — direct (single pass), staged (plan then code),")
    click.echo("   or native (agent loop). You can override this with --workflow.")
    click.echo("")
    click.echo("3. Choose models")
    click.echo("   One or more models are chosen based on task complexity and your config.")
    click.echo("   The planning step typically uses a strong reasoning model.")
    click.echo("")
    click.echo("4. Protect files")
    click.echo("   Read-only tasks (questions, explain, summarise) never write files.")
    click.echo("   Write-guarded paths block accidental changes outside the project root.")
    click.echo("")
    click.echo("5. Record the run")
    click.echo("   Each run is appended to .openshard/runs.jsonl with timing, model,")
    click.echo("   cost, and file counts so you can review history later.")
    click.echo("")
    click.echo("6. Capture feedback")
    click.echo("   After a run you can rate it with 'openshard feedback --rating good'.")
    click.echo("   Ratings are stored alongside the run entry.")


def _demo_readonly() -> None:
    click.echo("Scenario: readonly")
    click.echo("")
    click.echo("Read-only tasks are prompts that ask a question or request an explanation")
    click.echo("without asking OpenShard to change anything — for example:")
    click.echo("")
    click.echo('  openshard run "what does openshard/cli/main.py do?"')
    click.echo('  openshard run "explain the pipeline execution flow"')
    click.echo("")
    click.echo("OpenShard detects these tasks automatically and enforces two protections:")
    click.echo("")
    click.echo("  - File writes are blocked. Even if a model returns file changes, they")
    click.echo("    are discarded and a notice is shown.")
    click.echo("  - The model receives an explicit instruction not to return file changes.")
    click.echo("")
    click.echo("File protection behaviour")
    click.echo("  The write guard is enforced regardless of the --write flag.")
    click.echo("  This means a read-only task cannot accidentally write files even if")
    click.echo("  you pass --write on the command line.")


def _demo_tier_dispatch() -> None:
    click.echo("Scenario: tier-dispatch")
    click.echo("")
    click.echo("Tier dispatch is a model selection strategy where the routing stage")
    click.echo("assigns each part of a task to a tier (fast, standard, capable),")
    click.echo("then resolves those tier names to actual model IDs before execution.")
    click.echo("")
    click.echo("Model plan / dispatch")
    click.echo("  Planning usually uses the strongest reasoning model because it decides")
    click.echo("  how the task should be handled.")
    click.echo("  A work model is then dispatched for the main task. For standard tasks")
    click.echo("  this can be a balanced model like GLM-5.1; for harder tasks it can be")
    click.echo("  a stronger model.")
    click.echo("")
    click.echo("  To enable tier dispatch on a run:")
    click.echo('    openshard run "your task" --experimental-tier-dispatch')
    click.echo("")
    click.echo("  The dispatch decision is recorded in the run log and visible at:")
    click.echo("    openshard last --more")


def _demo_feedback() -> None:
    click.echo("Scenario: feedback")
    click.echo("")
    click.echo("After each run you can attach a developer rating to the run entry.")
    click.echo("This lets you track which tasks and models produced good results.")
    click.echo("")
    click.echo("Developer feedback capture")
    click.echo("  Rate the most recent run with one of three values:")
    click.echo("")
    click.echo("    openshard feedback --rating good")
    click.echo("    openshard feedback --rating mixed")
    click.echo("    openshard feedback --rating bad")
    click.echo("")
    click.echo("  Add an optional note:")
    click.echo('    openshard feedback --rating mixed --note "output was close but missed edge case"')
    click.echo("")
    click.echo("  Feedback is stored in .openshard/runs.jsonl alongside the run entry.")
    click.echo("  You can view it with 'openshard last --more'.")


@cli.group("demo", invoke_without_command=True)
@click.option(
    "--scenario",
    type=click.Choice(["readonly", "tier-dispatch", "feedback"], case_sensitive=False),
    default=None,
    help="Show a focused walkthrough for a specific scenario.",
)
@click.pass_context
def demo(ctx: click.Context, scenario: str | None) -> None:
    """Show a walkthrough of OpenShard concepts and common scenarios."""
    if ctx.invoked_subcommand is not None:
        # A subcommand (e.g. `demo shard`) will run; the scenario walkthrough
        # only applies to the bare `openshard demo` invocation.
        return
    if scenario is None:
        _demo_default()
    elif scenario.lower() == "readonly":
        _demo_readonly()
    elif scenario.lower() == "tier-dispatch":
        _demo_tier_dispatch()
    else:
        _demo_feedback()


# A realistic, in-memory example Shard used only by `openshard demo shard`. It is
# never written to history and never triggers a model, provider, or API key. The
# fields are tuned so the run lands in the "good" trust band: a completed,
# verified, single-file fix whose receipt has a few recommended gaps and a dirty
# repo. The model id is a real registry entry; nothing here implies live access.
_DEMO_SHARD_ENTRY: dict = {
    "schema_version": "1.2",
    "timestamp": "2026-01-01T00:00:00Z",
    "task": "Fix a failing test",
    "execution_model": "anthropic/claude-sonnet-4.6",
    "execution_profile": "native_light",
    "routing_category": "standard",
    "routing_rationale": "standard bug fix",
    "routing_selected_model": "anthropic/claude-sonnet-4.6",
    "routing_selected_provider": "anthropic",
    "estimated_cost": 0.0123,
    "prompt_tokens": 1200,
    "completion_tokens": 300,
    "total_tokens": 1500,
    "files_created": 0,
    "files_updated": 1,
    "files_deleted": 0,
    "files_detail": [
        {"path": "src/calculator.py", "change_type": "update",
         "summary": "Corrected off-by-one in range check"},
    ],
    "verification_attempted": True,
    "verification_passed": True,
    "osn_verification_contract": {
        "enabled": True,
        "status": "passed",
        "manual_review_required": False,
        "summary": "All checks passed",
        "returncode": 0,
        "duration_seconds": 4.2,
        "raw_output_stored": False,
    },
    "git_branch": "demo/fix-failing-test",
    "git_dirty": True,
    "run_timeline": [
        {"event_type": "stage", "label": "Planning", "status": "completed"},
        {"event_type": "stage", "label": "Implementation", "status": "completed"},
        {"event_type": "stage", "label": "Verification", "status": "completed"},
    ],
    "duration_seconds": 12.4,
    "summary": "Fixed the failing test in one file; checks passed.",
}

_DEMO_NEXT_COMMANDS: list[str] = [
    'openshard repo plan "review this repo"',
    'openshard run "fix a small bug" --verify',
    "openshard last",
    "openshard proof last",
    "openshard trust last",
]


def _demo_shard_artifacts():
    """Build the in-memory demo Shard and its receipt/proof/quality/trust views.

    Pure and offline: no API key, no model call, no disk read or write. Returns a
    tuple of ``(entry, receipt, contract, quality, trust)`` so the human and JSON
    branches share a single source of truth.
    """
    from openshard.history.proof_contract import build_shard_proof_contract
    from openshard.history.shard_contract import build_shard_receipt
    from openshard.history.shard_quality import build_shard_quality_summary
    from openshard.history.shard_schema import coerce_shard_entry
    from openshard.history.trust_score import evaluate_trust_score

    entry = coerce_shard_entry(dict(_DEMO_SHARD_ENTRY))
    receipt = build_shard_receipt(entry, index=0)
    contract = build_shard_proof_contract(entry)
    quality = build_shard_quality_summary(entry, receipt)
    # interaction_event_types=[] keeps this fully in-memory: the disk-reading
    # interaction loader is never touched for the demo.
    trust = evaluate_trust_score(entry, receipt, interaction_event_types=[])
    return entry, receipt, contract, quality, trust


def _demo_shard_human_lines(receipt, contract, quality, trust) -> list[str]:
    """Render the plain-language demo walkthrough as a list of lines."""
    unsafe_count = int(quality.get("unsafe_findings_count") or 0)
    unsafe_display = "none" if unsafe_count == 0 else str(unsafe_count)
    lines = [
        "OpenShard demo",
        "",
        "This is what OpenShard records after an AI coding run.",
        "",
        "Receipt:",
        f"  Task: {receipt.task_full}",
        "  Status: completed",
        f"  Files changed: {receipt.files_changed}",
        f"  Verification: {receipt.verification_status}",
        "",
        "Proof:",
        f"  Status: {contract.get('overall_status', 'unknown')}",
        f"  Required proof: {quality.get('required_proof', 'unknown')}",
        f"  Unsafe findings: {unsafe_display}",
        "",
        "Trust:",
        f"  Band: {trust.band}",
        f"  Score: {trust.score}/100",
        "",
        "What this means:",
        '  OpenShard does not just say "the agent changed code."',
        "  It records what happened, what was checked, and whether the proof is",
        "  good enough to rely on.",
        "",
        "  Receipt is what happened.",
        "  Proof is whether the saved record is good enough.",
        "  Trust is whether the run is safe to rely on.",
        "  A Shard is the saved proof record for one AI coding run.",
        "",
        "Try next:",
    ]
    lines.extend(f"  {cmd}" for cmd in _DEMO_NEXT_COMMANDS)
    return lines


@demo.command("shard")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output (valid JSON only).")
def demo_shard(as_json: bool) -> None:
    """Show a realistic example Shard and explain the OpenShard loop.

    Needs no API key, makes no model call, reads no real repo, and never writes
    run history. The Receipt, Proof, and Trust values are produced by the same
    builders used by real runs, so the example is genuine rather than faked.
    """
    entry, receipt, contract, quality, trust = _demo_shard_artifacts()

    if as_json:
        compact_receipt = {
            "task": receipt.task_full,
            "status": "completed",
            "files_changed": receipt.files_changed,
            "verification": receipt.verification_status,
        }
        payload = _machine_envelope(
            "demo shard", "ok", shard_id=receipt.shard_id,
            demo=True,
            run=_export_run_entry(entry, include_timeline=True, receipt=receipt),
            receipt=compact_receipt,
            proof_contract=contract,
            shard_quality=quality,
            trust={
                "score": trust.score,
                "band": trust.band,
                "penalties": [
                    {"code": p.code, "points": p.points, "reason": p.reason}
                    for p in trust.penalties
                ],
            },
            next_commands=list(_DEMO_NEXT_COMMANDS),
        )
        click.echo(json.dumps(payload, indent=2))
        return

    for line in _demo_shard_human_lines(receipt, contract, quality, trust):
        click.echo(line)


def _demo_run() -> None:
    click.echo("Task: Add rate limiting to the API gateway")
    click.echo("")
    click.echo("Execution")
    click.echo("  Mode: Run")
    click.echo("")
    click.echo("Routing")
    click.echo("  Category: standard")
    click.echo("  Initial candidate: Sonnet 4.6")
    click.echo("  Candidates: 8")
    click.echo("  Workflow: staged")
    click.echo("  Reason: standard coding task")
    click.echo("")
    click.echo("Model plan")
    click.echo("  Planning: Sonnet 4.6")
    click.echo("  Work: GLM-5.1")
    click.echo("  Validator: Sonnet 4.6 (reserved)")
    click.echo("")
    click.echo("Dispatch")
    click.echo("  Applied: yes")
    click.echo("  Source: demo")
    click.echo("  Work model: GLM-5.1")
    click.echo("  Initial candidate: Sonnet 4.6")
    click.echo("")
    click.echo("Verification")
    click.echo("  Name: tests")
    click.echo("  Safety: safe")
    click.echo("  Source: demo")
    click.echo("  Command: python -m pytest")
    click.echo("")
    click.echo("Time: 9.5s   Cost: $0.0133")
    click.echo("Feedback: openshard feedback --rating good")


@cli.command("demo-run")
def demo_run() -> None:
    """Show a realistic sample run without making provider calls or writing files."""
    _demo_run()


@cli.command()
def tui() -> None:
    """Launch the interactive OpenShard home screen."""
    try:
        from openshard.tui.app import OpenShardTui
    except ImportError:
        raise click.ClickException(
            "The TUI requires the 'textual' package. "
            "Please reinstall OpenShard or install textual."
        )
    OpenShardTui().run()


@cli.group(invoke_without_command=True)
@click.pass_context
def eval(ctx: click.Context):
    """Eval harness commands."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@eval.command("list")
@click.option("--suite", default="basic", show_default=True, help="Eval suite to list.")
def eval_list(suite: str):
    """List available eval tasks."""
    from openshard.evals.registry import load_eval_tasks

    try:
        tasks = load_eval_tasks(suite)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc))

    col_id = 24
    col_title = 40
    header = f"  {'id':<{col_id}}  {'title':<{col_title}}  category"
    click.echo(header)
    click.echo("  " + "-" * (len(header) - 2))
    for task in tasks:
        tid = task.id if len(task.id) <= col_id else task.id[: col_id - 1] + "..."
        ttitle = task.title if len(task.title) <= col_title else task.title[: col_title - 1] + "..."
        click.echo(f"  {tid:<{col_id}}  {ttitle:<{col_title}}  {task.category}")


@eval.command("validate")
@click.option("--suite", default="basic", show_default=True, help="Eval suite to validate.")
def eval_validate(suite: str):
    """Validate that all eval tasks in a suite load correctly."""
    from openshard.evals.registry import load_eval_tasks

    try:
        tasks = load_eval_tasks(suite)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc))

    errors: list[str] = []
    for task in tasks:
        if not task.prompt.strip():
            errors.append(f"{task.id}: prompt.txt is empty")

    if errors:
        for err in errors:
            click.echo(f"FAIL  {err}")
        raise click.ClickException(f"{len(errors)} task(s) failed validation.")

    click.echo(f"OK  {len(tasks)} task(s) passed validation for suite '{suite}'.")


@eval.command("run")
@click.option("--suite", default="basic", show_default=True, help="Eval suite to run.")
@click.option(
    "--model",
    default="anthropic/claude-haiku-4-5-20251001",
    show_default=True,
    help="Model for execution.",
)
def eval_run(suite: str, model: str):
    """Run all eval tasks in a suite and report pass/fail."""
    import tempfile

    from openshard.evals.registry import load_eval_tasks
    from openshard.evals.runner import append_eval_result, run_eval_task
    from openshard.run.pipeline import _copy_cwd_to_workspace

    try:
        tasks = load_eval_tasks(suite)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc))

    log_path = Path(".openshard") / "eval-runs.jsonl"
    passed_count = failed_count = 0

    for task in tasks:
        with tempfile.TemporaryDirectory(prefix=f"openshard-eval-{task.id}-") as tmp:
            workspace = Path(tmp)
            _copy_cwd_to_workspace(workspace)
            result = run_eval_task(task, model=model, suite=suite, workspace_root=workspace)
        append_eval_result(result, log_path)
        status = "PASS" if result.passed else "FAIL"
        extra = f"  ({result.error})" if result.error else ""
        click.echo(f"{status}  {task.id:<28}  {result.duration_seconds:.1f}s{extra}")
        if result.passed:
            passed_count += 1
        else:
            failed_count += 1

    click.echo(f"\n{passed_count} passed, {failed_count} failed  — results in {log_path}")
    if failed_count:
        raise SystemExit(1)


@eval.command("compare")
@click.option("--suite", default="basic", show_default=True, help="Eval suite to run.")
@click.option("--models", required=True, help="Comma-separated list of model slugs.")
def eval_compare(suite: str, models: str):
    """Run an eval suite across multiple models and print a comparison summary."""
    model_list = [m.strip() for m in models.split(",") if m.strip()]
    if not model_list:
        raise click.ClickException("--models must contain at least one model slug.")

    try:
        tasks = load_eval_tasks(suite)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc))

    log_path = Path(".openshard") / "eval-runs.jsonl"
    click.echo(f"\nRunning suite '{suite}' ({len(tasks)} tasks) across {len(model_list)} model(s)...\n")

    results_by_model: dict[str, list] = {}

    for model in model_list:
        click.echo(f"[{model}]")
        model_results = []
        for task in tasks:
            with tempfile.TemporaryDirectory(prefix=f"openshard-eval-{task.id}-") as tmp:
                workspace = Path(tmp)
                _copy_cwd_to_workspace(workspace)
                result = run_eval_task(task, model=model, suite=suite, workspace_root=workspace)
            append_eval_result(result, log_path)
            status = "PASS" if result.passed else "FAIL"
            extra = f"  ({result.error})" if result.error else ""
            click.echo(f"  {status}  {task.id:<28}  {result.duration_seconds:.1f}s{extra}")
            model_results.append(result)
        results_by_model[model] = model_results
        click.echo()

    all_results = [r for rs in results_by_model.values() for r in rs]
    show_tokens = any(r.total_tokens > 0 for r in all_results)

    header = f"{'Model':<44} {'Runs':>4}  {'Pass':>4}  {'Fail':>4}  {'Rate':>7}  {'Avg Dur':>8}"
    if show_tokens:
        header += f"  {'Avg Tok':>8}"
    header += f"  {'Unsafe':>6}"
    click.echo(header)

    any_failures = False
    for model, model_results in results_by_model.items():
        runs = len(model_results)
        passed_count = sum(1 for r in model_results if r.passed)
        failed_count = runs - passed_count
        if failed_count:
            any_failures = True
        rate = passed_count / runs * 100 if runs else 0.0
        avg_dur = sum(r.duration_seconds for r in model_results) / runs if runs else 0.0
        unsafe = sum(len(r.unsafe_files) for r in model_results)
        row = f"{model:<44} {runs:>4}  {passed_count:>4}  {failed_count:>4}  {rate:>6.1f}%  {avg_dur:>7.1f}s"
        if show_tokens:
            avg_tok = sum(r.total_tokens for r in model_results) / runs if runs else 0.0
            row += f"  {avg_tok:>8.0f}"
        row += f"  {unsafe:>6}"
        click.echo(row)

    click.echo(f"\nResults appended to {log_path}")

    if len(model_list) > 1:
        from openshard.evals.stats import rank_models
        ranking = rank_models(results_by_model)
        click.echo("\n[ranking]")
        if all(entry.pass_count == 0 for entry in ranking):
            click.echo("  no passing runs; cost-per-pass ranking unavailable")
        else:
            for entry in ranking:
                pass_pct = f"{entry.pass_rate:.0%}"
                cost_str = f"${entry.cost_per_pass:.4f}" if entry.cost_per_pass is not None else "-"
                tok_str = f"{entry.avg_tokens:,.0f}" if entry.avg_tokens is not None else "-"
                click.echo(
                    f"  {entry.rank}. {entry.model:<44}"
                    f"  pass: {pass_pct:>4}"
                    f"  cost/pass: {cost_str:<10}"
                    f"  avg tokens: {tok_str:>7}"
                    f"  avg dur: {entry.avg_duration:.1f}s"
                    f"  unsafe: {entry.unsafe_count}"
                )

    if any_failures:
        raise SystemExit(1)


@eval.command("report")
@click.option("--suite", default=None, help="Filter by eval suite name.")
@click.option("--model", default=None, help="Filter by model slug.")
def eval_report(suite: str | None, model: str | None):
    """Summarize eval run results from .openshard/eval-runs.jsonl."""
    import collections

    log_path = Path(".openshard") / "eval-runs.jsonl"

    if not log_path.exists():
        click.echo("No eval runs found. Run `openshard eval run` first.")
        return

    records: list[dict] = []
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            records.append(json.loads(raw))
        except json.JSONDecodeError:
            continue

    if suite:
        records = [r for r in records if r.get("suite") == suite]
    if model:
        records = [r for r in records if r.get("model") == model]

    if not records:
        click.echo("No records match the given filters.")
        return

    total = len(records)
    passed = sum(1 for r in records if r.get("passed"))
    failed = total - passed
    pass_rate = passed / total * 100
    avg_dur = sum(r.get("duration_seconds", 0) for r in records) / total

    tokens = [r.get("total_tokens", 0) for r in records]
    show_tokens = any(t > 0 for t in tokens)
    avg_tok = sum(tokens) / total if show_tokens else 0

    unsafe_count = sum(len(r.get("unsafe_files", [])) for r in records)

    parts = []
    if suite:
        parts.append(f"suite={suite}")
    if model:
        parts.append(f"model={model}")
    header_suffix = "  " + "  ".join(parts) if parts else ""

    click.echo(f"Eval Report{header_suffix}")
    click.echo("-" * 42)
    click.echo(f"  Total runs:    {total}")
    click.echo(f"  Passed:        {passed}  ({pass_rate:.1f}%)")
    click.echo(f"  Failed:        {failed}")
    click.echo(f"  Avg duration:  {avg_dur:.1f}s")
    if show_tokens:
        click.echo(f"  Avg tokens:    {avg_tok:.0f}")
    click.echo(f"  Unsafe files:  {unsafe_count}")

    by_task: dict[str, list[dict]] = collections.defaultdict(list)
    for r in records:
        by_task[r.get("task_id", "unknown")].append(r)

    click.echo("\nBy task:")
    for tid in sorted(by_task):
        grp = by_task[tid]
        p = sum(1 for r in grp if r.get("passed"))
        n = len(grp)
        pct = p / n * 100
        click.echo(f"  {tid:<28}  {p}/{n}  ({pct:.1f}%)")

    by_model: dict[str, list[dict]] = collections.defaultdict(list)
    for r in records:
        by_model[r.get("model", "unknown")].append(r)

    click.echo("\nBy model:")
    for mdl in sorted(by_model):
        grp = by_model[mdl]
        p = sum(1 for r in grp if r.get("passed"))
        n = len(grp)
        pct = p / n * 100
        click.echo(f"  {mdl:<44}  {p}/{n}  ({pct:.1f}%)")


@eval.command("stats")
@click.option("--suite", default=None, help="Filter by suite name.")
@click.option("--model", default=None, help="Filter by model slug.")
@click.option("--task", default=None, help="Filter by task_id.")
@click.option("--by-category", is_flag=True, default=False, help="Group results by task category.")
def eval_stats(suite: str | None, model: str | None, task: str | None, by_category: bool):
    """Show grouped pass/fail stats from .openshard/eval-runs.jsonl."""
    from openshard.evals.stats import (
        EVAL_RUNS_PATH,
        compute_category_stats,
        compute_eval_stats,
        load_eval_runs,
    )

    records = load_eval_runs(Path.cwd() / EVAL_RUNS_PATH)

    if by_category:
        from openshard.evals.registry import build_category_map

        suites = {r["suite"] for r in records if r.get("suite")}
        if suite:
            suites = {suite} & suites
        category_maps: dict[str, dict[str, str]] = {}
        for s in suites:
            try:
                category_maps[s] = build_category_map(s)
            except FileNotFoundError:
                category_maps[s] = {}

        rows = compute_category_stats(records, category_maps, suite=suite, model=model, task=task)

        if not rows:
            click.echo("No eval results found.")
            return

        click.echo("\n[eval stats --by-category]")
        parts = []
        if suite:
            parts.append(f"suite: {suite}")
        if model:
            parts.append(f"model: {model}")
        if task:
            parts.append(f"task: {task}")
        if parts:
            click.echo("  " + "  ".join(parts))

        header = (
            f"  {'suite':<10}  {'category':<12}  {'model':<44}"
            f"  {'runs':>5}  {'pass':>5}  {'fail':>5}  {'pass%':>6}"
            f"  {'avg_dur':>8}  {'avg_tokens':>11}  {'cost/pass':>10}  {'unsafe':>7}"
        )
        click.echo(f"\n{header}")
        for s in rows:
            tok = f"{s.avg_total_tokens:,.0f}" if s.avg_total_tokens is not None else "-"
            cpp = f"${s.cost_per_pass:.4f}" if s.cost_per_pass is not None else "-"
            click.echo(
                f"  {s.suite:<10}  {s.category:<12}  {s.model:<44}"
                f"  {s.run_count:>5}  {s.pass_count:>5}  {s.fail_count:>5}  {s.pass_rate:>5.0%}"
                f"  {s.avg_duration:>7.1f}s  {tok:>11}  {cpp:>10}  {s.unsafe_file_count:>7}"
            )

        total_runs = sum(s.run_count for s in rows)
        total_pass = sum(s.pass_count for s in rows)
        total_fail = sum(s.fail_count for s in rows)
        overall_rate = total_pass / total_runs if total_runs else 0.0
        click.echo(f"\n  total: {total_runs} runs  pass: {total_pass}  fail: {total_fail}  pass rate: {overall_rate:.0%}")
        return

    rows = compute_eval_stats(records, suite=suite, model=model, task=task)  # type: ignore[assignment]  # rows is list[EvalStats] here; earlier assignment was list[CategoryStats]

    if not rows:
        click.echo("No eval results found.")
        return

    click.echo("\n[eval stats]")
    parts = []
    if suite:
        parts.append(f"suite: {suite}")
    if model:
        parts.append(f"model: {model}")
    if task:
        parts.append(f"task: {task}")
    if parts:
        click.echo("  " + "  ".join(parts))

    header = (
        f"  {'suite':<10}  {'model':<44}  {'task_id':<24}"
        f"  {'runs':>5}  {'pass':>5}  {'fail':>5}  {'pass%':>6}"
        f"  {'avg_dur':>8}  {'avg_tokens':>11}  {'unsafe':>7}"
    )
    click.echo(f"\n{header}")
    for s in rows:
        tok = f"{s.avg_total_tokens:,.0f}" if s.avg_total_tokens is not None else "-"
        click.echo(
            f"  {s.suite:<10}  {s.model:<44}  {s.task_id:<24}"  # type: ignore[attr-defined]  # s is EvalStats (has task_id); mypy infers CategoryStats from earlier rows assignment
            f"  {s.run_count:>5}  {s.pass_count:>5}  {s.fail_count:>5}  {s.pass_rate:>5.0%}"
            f"  {s.avg_duration:>7.1f}s  {tok:>11}  {s.unsafe_file_count:>7}"
        )

    total_runs = sum(s.run_count for s in rows)
    total_pass = sum(s.pass_count for s in rows)
    total_fail = sum(s.fail_count for s in rows)
    overall_rate = total_pass / total_runs if total_runs else 0.0
    click.echo(f"\n  total: {total_runs} runs  pass: {total_pass}  fail: {total_fail}  pass rate: {overall_rate:.0%}")


# Default location for locally-generated eval cases (sibling of eval-runs.jsonl).
_GENERATED_EVALS_DIR = Path(".openshard") / "evals" / "generated"


def _resolve_eval_output_path(
    eval_id: str, output: str | None
) -> tuple[Path, list[str]]:
    """Resolve the write target for an eval case, falling back when unsafe.

    Returns (path, warnings). A custom ``--output`` is honoured only when it is a
    safe relative path (no absolute/drive prefix, no ``..`` traversal); otherwise
    we fall back to the default generated location and record a warning. The
    eval id is already sanitised by the caller, so the default path never embeds
    unsanitised shard/task data.
    """
    default_path = Path.cwd() / _GENERATED_EVALS_DIR / f"{eval_id}.json"
    if not output:
        return default_path, []

    candidate = Path(output)
    normalized = output.replace("\\", "/")
    is_absolute = (
        candidate.is_absolute()
        or normalized.startswith("/")  # POSIX-absolute, even when run on Windows
        or (len(normalized) > 1 and normalized[1] == ":")  # drive-letter prefix
    )
    has_traversal = ".." in candidate.parts
    if is_absolute or has_traversal:
        return (
            default_path,
            ["Ignored unsafe --output path; wrote to default location instead."],
        )
    return Path.cwd() / candidate, []


def _write_eval_case(path: Path, case: dict, force: bool) -> None:
    """Write the eval case JSON to ``path``. Raises on collision unless ``force``."""
    if path.exists() and not force:
        raise FileExistsError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")


@eval.command("create-from-last")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output (valid JSON only).")
@click.option("--output", "output", default=None,
              help="Write the eval case to this relative path instead of the default.")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite the output file if it already exists.")
def eval_create_from_last(as_json: bool, output: str | None, force: bool) -> None:
    """Convert the latest failed/rejected/partial Shard into a safe eval case.

    Local, deterministic, receipt-based. Reads the most recent run from history,
    classifies it with the failure taxonomy, and — only when it carries a
    failure/correction signal — writes a redacted, versioned eval-case JSON file
    under .openshard/evals/generated/. No network, no model calls; no secrets,
    raw file contents, diffs, transcripts, error messages, or absolute paths leak.
    """
    from openshard.evals.case_builder import build_eval_case, is_eligible
    from openshard.history.failures import classify_failure
    from openshard.history.shard_contract import build_shard_receipt

    command = "eval create-from-last"
    log_path = Path.cwd() / _LOG_PATH
    entries = _load_run_entries(log_path)

    if not entries:
        if as_json:
            payload = _machine_envelope(
                command, "not_eligible",
                eval_id=None, source_shard_id=None, failure_category=None,
                output_path_display=None,
                warnings=["No run history found."],
            )
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo("No eval case created because there is no run history.")
        return

    entry = entries[-1]
    receipt = build_shard_receipt(entry, index=len(entries) - 1)
    classification = classify_failure(entry, receipt)

    if not is_eligible(classification):
        message = (
            "No eval case created because the latest Shard has no "
            "failure/correction signal."
        )
        if as_json:
            payload = _machine_envelope(
                command, "not_eligible",
                shard_id=receipt.shard_id,
                eval_id=None,
                source_shard_id=receipt.shard_id,
                failure_category=classification.category,
                output_path_display=None,
            )
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo(message)
        return

    created_at = datetime.datetime.now(datetime.UTC).isoformat()
    case = build_eval_case(receipt, classification, created_at)
    eval_id = case["eval_id"]
    target, warnings = _resolve_eval_output_path(eval_id, output)

    try:
        _write_eval_case(target, case, force)
    except FileExistsError:
        display = _safe_output_display(str(target))
        if as_json:
            payload = _machine_envelope(
                command, "error",
                shard_id=receipt.shard_id,
                eval_id=eval_id,
                source_shard_id=receipt.shard_id,
                failure_category=classification.category,
                output_path_display=display,
                warnings=warnings + [f"File already exists: {display}. Use --force to overwrite."],
            )
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo(f"Eval case not written: {display} already exists. Use --force to overwrite.")
        raise SystemExit(1)
    except OSError as exc:
        display = _safe_output_display(str(target))
        if as_json:
            payload = _machine_envelope(
                command, "error",
                shard_id=receipt.shard_id,
                eval_id=eval_id,
                source_shard_id=receipt.shard_id,
                failure_category=classification.category,
                output_path_display=display,
                warnings=warnings + [f"Could not write eval case to {display}: {type(exc).__name__}."],
            )
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo(f"Eval case not written: could not write to {display} ({type(exc).__name__}).")
        raise SystemExit(1)

    display = _safe_output_display(str(target))
    if as_json:
        payload = _machine_envelope(
            command, "created",
            shard_id=receipt.shard_id,
            eval_id=eval_id,
            source_shard_id=receipt.shard_id,
            failure_category=classification.category,
            output_path_display=display,
            warnings=warnings,
        )
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo("Created eval case from the latest failed Shard.")
    click.echo(f"  eval id:          {eval_id}")
    click.echo(f"  source shard id:  {receipt.shard_id}")
    click.echo(f"  failure category: {classification.category}")
    click.echo(f"  output:           {display}")
    for warning in warnings:
        click.echo(f"  warning:          {warning}")


@cli.group("packs", invoke_without_command=True)
@click.pass_context
def packs(ctx: click.Context):
    """Workflow pack commands."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@packs.command("list")
def packs_list():
    """List all available workflow packs."""
    from openshard.workflow_packs.packs import load_packs

    packs_ = load_packs()
    col_id = 32
    col_title = 40
    for p in packs_:
        pid = p.id if len(p.id) <= col_id else p.id[: col_id - 1] + "..."
        ptitle = p.title if len(p.title) <= col_title else p.title[: col_title - 1] + "..."
        click.echo(f"  {pid:<{col_id}}  {ptitle:<{col_title}}  {p.category}")


@packs.command("show")
@click.argument("pack_id")
def packs_show(pack_id: str):
    """Show full metadata for a workflow pack."""
    from openshard.workflow_packs.packs import get_pack, load_packs

    try:
        p = get_pack(pack_id)
    except KeyError:
        available = ", ".join(p.id for p in load_packs())
        raise click.ClickException(f"Unknown pack {pack_id!r}. Available: {available}")

    click.echo(f"ID:                    {p.id}")
    click.echo(f"Title:                 {p.title}")
    click.echo(f"Category:              {p.category}")
    click.echo(f"Summary:               {p.summary}")
    click.echo(f"Recommended context:   {p.recommended_context}")
    click.echo(f"Expected receipt value: {p.expected_receipt_value}")
    click.echo(f"Safety notes:          {p.safety_notes}")
    click.echo(f"Tags:                  {', '.join(p.tags)}")


@packs.command("prompt")
@click.argument("pack_id")
def packs_prompt(pack_id: str):
    """Print only the prompt text for a workflow pack (ready to copy or run)."""
    from openshard.workflow_packs.packs import get_pack, load_packs

    try:
        p = get_pack(pack_id)
    except KeyError:
        available = ", ".join(p.id for p in load_packs())
        raise click.ClickException(f"Unknown pack {pack_id!r}. Available: {available}")

    click.echo(p.prompt)
    click.echo("")
    click.echo(
        "This command only prints the prompt. "
        "To run it, cd into the target repo and use `openshard tui` or `openshard run`."
    )


@packs.command("run")
@click.argument("pack_id")
@click.option("--context", "extra_context", default=None, metavar="TEXT", help="Additional context appended to the pack prompt.")
@click.option("--write", is_flag=True, default=False, help="Write generated files to disk.")
@click.option("--verify", is_flag=True, default=False, help="Run verification after writing (requires --write).")
@click.option("--dry-run", "dry_run", is_flag=True, default=False, help="Preview without executing.")
@click.option(
    "--workflow",
    type=click.Choice(["auto", "direct", "staged", "native", "opencode", "claude-code", "codex"], case_sensitive=False),
    default=None,
    help="Override the pack's recommended workflow.",
)
@click.pass_context
def packs_run(ctx: click.Context, pack_id: str, extra_context: str | None, write: bool, verify: bool, dry_run: bool, workflow: str | None) -> None:
    """Run workflow pack PACK_ID."""
    from openshard.workflow_packs.packs import get_pack, load_packs

    try:
        p = get_pack(pack_id)
    except KeyError:
        available = ", ".join(pk.id for pk in load_packs())
        raise click.ClickException(f"Unknown pack {pack_id!r}. Available: {available}")

    task = p.prompt
    if p.execution_prompt_suffix:
        task = task + p.execution_prompt_suffix
    if extra_context:
        task = f"{task}\n\nContext: {extra_context}"

    resolved_workflow: str | None = workflow or (p.workflow if p.workflow else None)

    click.echo(f"\nPack:     {p.title}")
    click.echo(f"Category: {p.category}")
    if p.safety_notes:
        click.echo(f"Safety:   {p.safety_notes}")
    if resolved_workflow:
        click.echo(f"Workflow: {resolved_workflow}")
    click.echo("")

    ctx.invoke(run, task=task, write=write, verify=verify, dry_run=dry_run, workflow=resolved_workflow)


@cli.group()
def adapters() -> None:
    """External adapter utilities."""


@adapters.command("doctor")
def adapters_doctor() -> None:
    """Check external adapter availability and show setup guidance."""
    from openshard.execution.opencode_adapter import detect_opencode

    click.echo("\nOpenShard Adapter Doctor\n")
    click.echo("OpenCode")
    avail = detect_opencode()
    if avail.available:
        click.echo("  Status:  detected")
        click.echo(f"  Path:    {avail.path}")
    else:
        click.echo("  Status:  not installed")
        click.echo(f"  Reason:  {avail.reason}")
        click.echo("  Install options:")
        for opt in avail.install_guidance:
            click.echo(f"    {opt}")
        click.echo("  After installing, verify with:")
        click.echo("    opencode --version")
        click.echo("    openshard adapters doctor")
    click.echo("")


@cli.group("import")
def import_group() -> None:
    """Import an external AI coding session as an OpenShard receipt."""


@import_group.command("claude")
@click.option("--task", required=True, help="Task description given to Claude Code.")
@click.option("--model", default=None, help="Model used (e.g. claude-sonnet-4-6). Default: unknown.")
@click.option(
    "--notes", "notes_file", default=None, type=click.Path(),
    help="Optional notes/summary file. First 300 chars stored (scrubbed); raw content not kept.",
)
@click.option(
    "--repo-path", "repo_path", default=None, type=click.Path(),
    help="Repository path (default: current directory).",
)
@click.option("--dry-run", is_flag=True, default=False, help="Print the Shard without writing it.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
@click.option(
    "--shard",
    "shard_id",
    default=None,
    help=(
        "Attach this import as another attempt on an existing Shard. Must "
        "reference a Shard ID from a previous run."
    ),
)
def import_claude(
    task: str,
    model: str | None,
    notes_file: str | None,
    repo_path: str | None,
    dry_run: bool,
    as_json: bool,
    shard_id: str | None,
) -> None:
    """Import a Claude Code session as an OpenShard receipt.

    Creates a Shard from the task description and current git state.
    OpenShard did not control this run: verification, cost, and model
    details are not recorded unless explicitly provided.
    """
    from openshard.adapters.claude_code_import import (
        build_claude_code_import_entry,
        write_import_entry,
    )
    from openshard.history.metrics import load_runs
    from openshard.history.run_attempt import UnknownShardError, resolve_shard_for_attempt

    cwd = Path(repo_path) if repo_path else Path.cwd()
    notes_path = Path(notes_file) if notes_file else None

    if notes_path is not None and not notes_path.is_file():
        raise click.BadParameter(
            f"Notes file not found: {notes_file}",
            param_hint="--notes",
        )

    attempt_number = 1
    if shard_id:
        try:
            shard_id, attempt_number = resolve_shard_for_attempt(
                shard_id, load_runs(cwd), "", None,
            )
        except UnknownShardError as exc:
            raise click.UsageError(str(exc))

    runs_path = cwd / ".openshard" / "runs.jsonl"
    try:
        run_index = sum(1 for _ in runs_path.open(encoding="utf-8")) if runs_path.exists() else 0
    except Exception:
        run_index = None

    entry = build_claude_code_import_entry(
        task,
        model=model,
        notes_file=notes_path,
        repo_path=cwd,
        shard_id=shard_id,
        attempt_number=attempt_number,
        run_index=run_index,
    )

    if dry_run or as_json:
        click.echo(json.dumps(entry, indent=2))
        if dry_run:
            return

    if not dry_run:
        write_import_entry(entry, cwd)

    if not as_json:
        click.echo(f"Imported Claude Code receipt. Shard: {entry.get('shard_id')}")
        click.echo("OpenShard did not control this run.")


@cli.group("wrap")
def wrap_group() -> None:
    """Wrap an external AI coding command and record a Shard receipt automatically."""


@wrap_group.command("claude")
@click.option("--task", required=True, help="Task description given to Claude Code.")
@click.option("--model", default=None, help="Model used (e.g. claude-sonnet-4-6). Default: unknown.")
@click.option(
    "--repo-path", "repo_path", default=None, type=click.Path(),
    help="Repository path (default: current directory).",
)
@click.option("--dry-run", is_flag=True, default=False, help="Print the Shard without writing it. Does NOT run the subprocess.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output after run.")
@click.option(
    "--shard",
    "shard_id",
    default=None,
    help=(
        "Attach this wrap as another attempt on an existing Shard. Must "
        "reference a Shard ID from a previous run."
    ),
)
@click.argument("command", nargs=-1, required=True)
def wrap_claude(
    task: str,
    model: str | None,
    repo_path: str | None,
    dry_run: bool,
    as_json: bool,
    shard_id: str | None,
    command: tuple[str, ...],
) -> None:
    """Wrap a Claude Code command and record an OpenShard receipt automatically.

    Captures git state before, runs the command with full passthrough, diffs
    git state after, and creates the receipt automatically.
    OpenShard did not control this run: verification, cost, and model
    details are not recorded unless explicitly provided.

    Example:

      openshard wrap claude --task "Fix the auth service" -- claude "fix auth"
    """
    from openshard.adapters.wrap_exec import (
        build_wrap_entry,
        capture_pre_run_state,
        run_wrapped_command,
        write_wrap_entry,
    )
    from openshard.history.metrics import load_runs
    from openshard.history.run_attempt import UnknownShardError, resolve_shard_for_attempt

    cwd = Path(repo_path) if repo_path else Path.cwd()
    cmd = list(command)

    attempt_number = 1
    if shard_id:
        try:
            shard_id, attempt_number = resolve_shard_for_attempt(
                shard_id, load_runs(cwd), "", None,
            )
        except UnknownShardError as exc:
            raise click.UsageError(str(exc))

    runs_path = cwd / ".openshard" / "runs.jsonl"
    try:
        run_index = sum(1 for _ in runs_path.open(encoding="utf-8")) if runs_path.exists() else 0
    except Exception:
        run_index = None

    if dry_run:
        # Build a fake pre-state without running the subprocess.
        pre_state = {
            "git_branch": None,
            "git_head_commit_hash": None,
            "git_dirty": False,
            "captured_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        entry = build_wrap_entry(
            task,
            model=model,
            pre_state=pre_state,
            exit_code=0,
            repo_path=cwd,
            shard_id=shard_id,
            attempt_number=attempt_number,
            run_index=run_index,
        )
        click.echo(json.dumps(entry, indent=2))
        return

    # Proactive wrap: capture → run → diff → record.
    pre_state = capture_pre_run_state(cwd)
    cmd_display = " ".join(cmd)
    click.echo(f"Running: {cmd_display}")

    exit_code = run_wrapped_command(cmd)

    entry = build_wrap_entry(
        task,
        model=model,
        pre_state=pre_state,
        exit_code=exit_code,
        repo_path=cwd,
        shard_id=shard_id,
        attempt_number=attempt_number,
        run_index=run_index,
    )

    write_wrap_entry(entry, cwd)

    if as_json:
        click.echo(json.dumps(entry, indent=2))
    else:
        click.echo(f"Wrapped Claude Code receipt. Shard: {entry.get('shard_id')}")
        click.echo("OpenShard did not control this run.")

    if exit_code != 0:
        sys.exit(exit_code)


@cli.group()
def session() -> None:
    """Local session utilities."""


@session.command("infer")
@click.option(
    "--path",
    default=None,
    help="Override base directory (default: current working directory).",
)
def session_infer(path: str | None) -> None:
    """Infer behavioural signals from session events and write to session_signals.jsonl."""
    from openshard.history.session_signals import run_inference

    base_path = Path(path) if path else None
    signals = run_inference(base_path)
    click.echo(f"Inferred {len(signals)} signal(s).")


def _current_state() -> dict:
    """Build the shared onboarding state from the current on-disk config."""
    from openshard.config import onboarding as ob

    config, valid, path = load_config_safe()
    return ob.build_state(
        version=__version__,
        config_found=path is not None,
        config_path=path,
        config_valid=valid,
        onboarding=get_onboarding(config),
    )


def _echo_warnings_next_steps(state: dict) -> None:
    warnings = state.get("warnings") or []
    next_steps = state.get("next_steps") or []
    if warnings:
        click.echo("\nWarnings:")
        for w in warnings:
            click.echo(f"  ! {w}")
    if next_steps:
        click.echo("\nNext steps:")
        for s in next_steps:
            click.echo(f"  - {s}")


@cli.command()
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, default=False,
              help="Non-interactive: apply defaults/flags without prompting.")
@click.option("--mode", "mode", default=None, help="Usage mode (see options).")
@click.option("--provider", "provider", default=None, help="Provider preference (see options).")
@click.option("--model-mode", "model_mode", default=None, help="Model mode (see options).")
@click.option("--output-mode", "output_mode", default=None, help="Output mode (see options).")
@click.option("--force", is_flag=True, default=False, help="Overwrite existing onboarding without prompting.")
def init(as_json: bool, assume_yes: bool, mode: str | None, provider: str | None,
         model_mode: str | None, output_mode: str | None, force: bool) -> None:
    """Set up OpenShard for first use (interactive, or --yes / --json)."""
    from openshard.config import onboarding as ob

    mode_keys = [k for k, _, _ in ob.MODES]
    provider_keys = [k for k, _, _ in ob.PROVIDERS]
    model_mode_keys = [k for k, _, _ in ob.MODEL_MODES]
    output_mode_keys = [k for k, _, _ in ob.OUTPUT_MODES]

    def _validate(name: str, value: str | None, allowed: list[str]) -> None:
        if value is not None and value not in allowed:
            raise click.BadParameter(
                f"'{value}' is not a valid {name}. Choose from: {', '.join(allowed)}.",
            )

    _validate("mode", mode, mode_keys)
    _validate("provider", provider, provider_keys)
    _validate("model-mode", model_mode, model_mode_keys)
    _validate("output-mode", output_mode, output_mode_keys)

    # --json without --yes: read-only discovery. Never writes.
    if as_json and not assume_yes:
        payload = {"options": ob.options_catalog(), "state": _current_state()}
        click.echo(json.dumps(payload, indent=2))
        return

    def _prompt(label: str, items: list[tuple[str, str, str]], default: str) -> str:
        click.echo(f"\n{label}:")
        for key, opt_label, desc in items:
            click.echo(f"  {key:<16} {opt_label} — {desc}")
        keys = [k for k, _, _ in items]
        return click.prompt("  Choice", type=click.Choice(keys), default=default,
                            show_choices=False)

    if assume_yes:
        sel_mode = mode or ("native" if ob.any_api_key_present() else "local_only")
        sel_provider = provider or ob.default_provider()
        sel_model_mode = model_mode or "balanced"
        sel_output_mode = output_mode or "human"
    else:
        sel_mode = mode or _prompt("Usage mode", ob.MODES,
                                   "native" if ob.any_api_key_present() else "local_only")
        sel_provider = provider or _prompt("Provider", ob.PROVIDERS, ob.default_provider())
        sel_model_mode = model_mode or _prompt("Model mode", ob.MODEL_MODES, "balanced")
        sel_output_mode = output_mode or _prompt("Output mode", ob.OUTPUT_MODES, "human")

        click.echo("\nSafety:")
        for note in ob.SAFETY_NOTES:
            click.echo(f"  - {note}")

    existing = find_config_path()
    overwrite_warning = None
    if existing is not None and not force:
        if assume_yes:
            overwrite_warning = (
                "Existing config found; onboarding settings were replaced "
                "(model settings preserved)."
            )
        else:
            if not click.confirm(
                "\nConfig already exists. Overwrite onboarding settings? "
                "(existing model settings are preserved)",
                default=True,
            ):
                click.echo("Aborted; no changes made.")
                return

    onboarding_block = {
        "schema_version": ob.SCHEMA_VERSION,
        "mode": sel_mode,
        "provider": sel_provider,
        "model_mode": sel_model_mode,
        "output_mode": sel_output_mode,
        "completed_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }

    # Merge over the full loaded base so model_tiers and friends are preserved.
    base = load_config_safe()[0]
    base["onboarding"] = onboarding_block
    written_path = save_config(base)

    config, valid, _ = load_config_safe()
    state = ob.build_state(
        version=__version__,
        config_found=True,
        config_path=written_path,
        config_valid=valid,
        onboarding=get_onboarding(config),
    )
    if overwrite_warning:
        state["warnings"].insert(0, overwrite_warning)

    if as_json:
        click.echo(json.dumps(state, indent=2))
        return

    click.echo(f"\nWrote {state['config_path_display']}")
    click.echo(f"  mode:        {sel_mode}")
    click.echo(f"  provider:    {sel_provider}")
    click.echo(f"  model_mode:  {sel_model_mode}")
    click.echo(f"  output_mode: {sel_output_mode}")
    _echo_warnings_next_steps(state)


@cli.command()
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
@click.option(
    "--repo-path",
    "repo_path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository to check Claude Code integration for (default: current directory).",
)
def doctor(as_json: bool, repo_path: Path | None) -> None:
    """Diagnose OpenShard configuration and setup state, including Claude Code integration."""
    from openshard.adapters.claude_mcp_install import find_repo_root
    from openshard.adapters.claude_setup import HISTORY_RELPATH, detect_claude_integration
    from openshard.adapters.claude_setup import history_writable as check_history_writable
    from openshard.config import onboarding as ob

    state = _current_state()
    state["git_repo"] = ob.detect_git_repo()

    root = find_repo_root(repo_path)
    base_dir = root if root is not None else (repo_path or Path.cwd())
    claude_status = detect_claude_integration(root)
    history_writable = check_history_writable(base_dir)
    claude_dict = claude_status.to_dict()
    claude_dict["history_writable"] = history_writable
    state["claude_code"] = claude_dict

    if as_json:
        click.echo(json.dumps(state, indent=2))
        return

    click.echo("\nOpenShard Doctor\n")
    click.echo(f"  version:      {state['openshard_version']}")
    click.echo(f"  config found: {'yes' if state['config_found'] else 'no'}")
    click.echo(f"  config path:  {state['config_path_display'] or '-'}")
    click.echo(f"  config valid: {'yes' if state['config_valid'] else 'no'}")
    click.echo(f"  git repo:     {'yes' if state['git_repo'] else 'no'}")
    click.echo("\n  Onboarding:")
    click.echo(f"    mode:        {state['mode'] or '-'}")
    click.echo(f"    provider:    {state['provider'] or '-'}")
    click.echo(f"    model_mode:  {state['model_mode'] or '-'}")
    click.echo(f"    output_mode: {state['output_mode'] or '-'}")
    click.echo("\n  API keys (environment):")
    for prov, present in ob.api_key_present().items():
        click.echo(f"    {prov:<12} {'yes' if present else 'no'}")
    _echo_warnings_next_steps(state)

    hooks_ok = bool(claude_status.hook_events_installed) and not claude_status.hook_events_missing
    checks: list[tuple[str, bool, str]] = [
        ("Repository", root is not None, "not a git repository"),
        ("Local history", history_writable, f"{HISTORY_RELPATH.as_posix()} is not writable"),
        ("Claude Code", claude_status.claude_cli.available, "CLI not found on PATH"),
        ("MCP", claude_status.mcp_configured, claude_status.mcp_detail),
        ("Auto-capture hooks", hooks_ok, claude_status.hooks_settings_error or "not configured"),
        (
            "Receipt enrichment",
            claude_status.statusline_state == "openshard",
            claude_status.hooks_settings_error or (
                "custom status line present" if claude_status.statusline_state == "custom" else "not configured"
            ),
        ),
    ]
    click.echo("\nClaude Code\n")
    for label, ok, detail in checks:
        mark = "✓" if ok else "✗"
        suffix = "" if ok else f" ({detail})"
        click.echo(f"  {mark} {label}{suffix}")

    core_ready = (
        root is not None and history_writable and claude_status.claude_cli.available
        and claude_status.mcp_configured and hooks_ok
    )
    fully_ready = core_ready and claude_status.statusline_state == "openshard"
    click.echo("")
    if fully_ready:
        click.echo("Ready -- use Claude Code normally.")
    elif core_ready:
        click.echo("Ready, with limited receipts -- use Claude Code normally. Run `openshard setup` for details.")
    else:
        click.echo("Not ready -- run `openshard setup` to configure Claude Code capture.")
    click.echo("")


@cli.group("config")
def config_cmd() -> None:
    """Configuration utilities."""


@config_cmd.command("show")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def config_show(as_json: bool) -> None:
    """Show the active configuration with secrets redacted."""
    import yaml

    from openshard.config import onboarding as ob

    config, valid, _ = load_config_safe()
    safe = ob.redact(config)

    if as_json:
        click.echo(json.dumps(safe, indent=2))
        return

    if not valid:
        click.echo("Warning: config could not be parsed; showing safe defaults.\n")
    click.echo(yaml.safe_dump(safe, sort_keys=False, default_flow_style=False).rstrip())


# ---------------------------------------------------------------------------
# roster command group
# ---------------------------------------------------------------------------

@cli.group("roster")
def roster_cmd() -> None:
    """Inspect and manage the custom model roster."""


def _roster_models_section(config: dict) -> dict:
    """Return a reference to config['models'], ensuring the custom_roster sub-key exists."""
    models = config.setdefault("models", {})
    roster = models.setdefault("custom_roster", {"name": "default", "models": []})
    if not isinstance(roster.get("models"), list):
        roster["models"] = []
    return models


@roster_cmd.command("list")
def roster_list() -> None:
    """Show current roster name, models, mode status, and valid/invalid counts."""
    from openshard.models.registry import is_known_model, lifecycle_for

    config, valid, _ = load_config_safe()
    if not valid:
        raise click.ClickException("Config file is malformed — fix or delete it first.")

    models_cfg = config.get("models", {})
    mode = models_cfg.get("mode", "auto")
    roster_cfg = models_cfg.get("custom_roster", {})
    roster_name = roster_cfg.get("name", "default")
    roster_models: list[str] = roster_cfg.get("models") or []

    active = mode == "custom_roster"
    valid_ids = [m for m in roster_models if is_known_model(m)]
    invalid_ids = [m for m in roster_models if not is_known_model(m)]

    click.echo(f"Roster name : {roster_name}")
    click.echo(f"Mode        : {mode}{'  (active)' if active else ''}")
    click.echo(f"Models      : {len(roster_models)}  ({len(valid_ids)} known to registry, {len(invalid_ids)} unknown)")

    if roster_models:
        click.echo("")
        for mid in roster_models:
            lc = lifecycle_for(mid) if is_known_model(mid) else None
            status = f"[{lc}]" if lc else "[unknown]"
            click.echo(f"  {mid:<55} {status}")
    else:
        click.echo("  (empty)")


@roster_cmd.command("show")
def roster_show() -> None:
    """Print the current models config block in a compact, secret-free format."""
    import yaml

    config, valid, _ = load_config_safe()
    if not valid:
        raise click.ClickException("Config file is malformed — fix or delete it first.")

    models_cfg = config.get("models", {})
    # Drop any accidentally injected secret fields before printing
    safe = {k: v for k, v in models_cfg.items() if not k.endswith("_api_key") and not k.endswith("_key")}
    click.echo(yaml.safe_dump({"models": safe}, sort_keys=False, default_flow_style=False).rstrip())


@roster_cmd.command("add")
@click.argument("model_id")
def roster_add(model_id: str) -> None:
    """Add MODEL_ID to the custom roster after validating it against the registry."""
    from openshard.config.settings import config_search_path
    from openshard.models.registry import is_known_model

    if not is_known_model(model_id):
        raise click.ClickException(
            f"Unknown model ID: {model_id!r}. Run 'openshard models list' to see available models."
        )

    config, valid, path = load_config_safe()
    if not valid:
        raise click.ClickException("Config file is malformed — fix or delete it first.")

    models_cfg = _roster_models_section(config)
    current: list[str] = models_cfg["custom_roster"]["models"]

    if model_id in current:
        click.echo(f"{model_id} is already in the roster — no change.")
        return

    current.append(model_id)
    save_config(config)
    click.echo(f"Added {model_id}. Roster now has {len(current)} model(s).")
    click.echo(f"Saved to {path or config_search_path()}")


@roster_cmd.command("remove")
@click.argument("model_id")
def roster_remove(model_id: str) -> None:
    """Remove MODEL_ID from the custom roster (no error if absent)."""
    from openshard.config.settings import config_search_path

    config, valid, path = load_config_safe()
    if not valid:
        raise click.ClickException("Config file is malformed — fix or delete it first.")

    models_cfg = _roster_models_section(config)
    current: list[str] = models_cfg["custom_roster"]["models"]

    if model_id not in current:
        click.echo(f"{model_id!r} is not in the roster — nothing to remove.")
        return

    current.remove(model_id)
    save_config(config)
    click.echo(f"Removed {model_id}. Roster now has {len(current)} model(s).")
    click.echo(f"Saved to {path or config_search_path()}")


@roster_cmd.command("use")
@click.argument("name")
def roster_use(name: str) -> None:
    """Set custom roster mode and assign NAME as the roster label.

    This sets models.mode to 'custom_roster' and records NAME as the roster
    label.  In v1, there is a single local roster list — NAME is a label,
    not a selector for multiple stored rosters.
    """
    from openshard.config.settings import config_search_path

    config, valid, path = load_config_safe()
    if not valid:
        raise click.ClickException("Config file is malformed — fix or delete it first.")

    models_cfg = _roster_models_section(config)
    models_cfg["mode"] = "custom_roster"
    models_cfg["custom_roster"]["name"] = name
    save_config(config)
    click.echo(f"Mode set to 'custom_roster', roster name set to {name!r}.")
    click.echo("Note: v1 has a single local roster list — the name is a label only.")
    click.echo(f"Saved to {path or config_search_path()}")


@roster_cmd.command("validate")
def roster_validate() -> None:
    """Validate all custom roster model IDs against the registry.

    Exits with code 1 if any IDs are not known to the registry.
    Does not call external APIs — validation is registry-only.
    """
    from openshard.models.registry import is_known_model, lifecycle_for

    config, valid, _ = load_config_safe()
    if not valid:
        raise click.ClickException("Config file is malformed — fix or delete it first.")

    roster_cfg = config.get("models", {}).get("custom_roster", {})
    roster_models: list[str] = roster_cfg.get("models") or []

    if not roster_models:
        click.echo("Roster is empty — nothing to validate.")
        return

    unknown: list[str] = []
    warned: list[tuple[str, str]] = []

    for mid in roster_models:
        if not is_known_model(mid):
            unknown.append(mid)
        else:
            lc = lifecycle_for(mid) or "active_default"
            if lc != "active_default":
                warned.append((mid, lc))

    for mid, lc in warned:
        click.echo(f"[WARN] {mid}  lifecycle={lc} — known to registry but not routing-default")

    for mid in unknown:
        click.echo(f"[INVALID] {mid}  — not known to registry")

    valid_count = len(roster_models) - len(unknown)
    click.echo(f"\n{valid_count}/{len(roster_models)} model(s) known to registry.")

    if unknown:
        raise SystemExit(1)


@roster_cmd.command("reset")
def roster_reset() -> None:
    """Clear the custom roster and return to auto routing mode."""
    from openshard.config.settings import config_search_path

    config, valid, path = load_config_safe()
    if not valid:
        raise click.ClickException("Config file is malformed — fix or delete it first.")

    models_cfg = _roster_models_section(config)
    models_cfg["mode"] = "auto"
    models_cfg["custom_roster"]["models"] = []
    save_config(config)
    click.echo("Roster cleared. Mode reset to 'auto'.")
    click.echo(f"Saved to {path or config_search_path()}")


if __name__ == "__main__":
    cli()
