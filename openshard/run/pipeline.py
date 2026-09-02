import datetime
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import click

from openshard.analysis.repo import RepoFacts, analyze_repo
from openshard.cli.run_output import (
    _build_routing_line,
    _exec_message,
    _extract_findings_from_model_answer,
    _extract_findings_from_review_files,
    _extract_structured_findings,
    _model_label,
    _print_dry_run,
    _print_shrunk,
    _print_summary,
    _profile_display_label,
    _should_shrink,
    _Spinner,
    render_post_run,
)
from openshard.config.settings import (
    detect_provider,
    get_anthropic_api_key,
    get_openai_api_key,
)
from openshard.execution.gates import VALID_APPROVAL_MODES, GateEvaluator, resolve_gate_decisions
from openshard.execution.generator import (
    ExecutionGenerator,
    ExecutionResult,
    check_stack_mismatch,
)
from openshard.execution.opencode_executor import OpenCodeExecutor
from openshard.execution.stages import (
    Stage,
    StageRun,
    route_stage,
    run_planning_stage,
    run_validator_stage,
    split_task,
)
from openshard.history.adjustments import (
    compute_history_adjustment_reasons,
    compute_history_adjustments,
)
from openshard.history.failure_memory import (
    NativeFailureMemoryEvent,
    log_failure_memory_event,
    parse_failure_summary,
)
from openshard.history.feedback_scoring import (
    compute_feedback_adjustment_reasons,
    compute_feedback_adjustments,
)
from openshard.history.metrics import load_runs
from openshard.history.run_checkpoints import (
    NativeRunCheckpointEvent,
)
from openshard.history.run_checkpoints import (
    log_run_checkpoint_event as _log_run_checkpoint,
)
from openshard.native.context import (
    NativeCandidateSummary,
    NativeEditLoopSummary,
    NativeVerificationLoop,
    RetryMetadata,
    build_failure_summary,
    record_native_candidate_attempt,
    record_native_edit_loop_attempt,
    render_verification_failure_context,
    select_native_candidate,
)
from openshard.native.executor import NativeAgentExecutor
from openshard.providers.base import ProviderAuthError, ProviderError, ProviderRateLimitError
from openshard.providers.manager import ProviderManager
from openshard.routing.engine import (
    ESCALATION_CHAIN,
    MODEL_STRONG,
    RoutingDecision,
    classify_review_domain,
    has_inline_readonly_instruction,
    is_readonly_task,
    looks_like_review_task,
    route,
)
from openshard.routing.form_factor_policy import ExecutionFormFactorDecision, select_form_factor
from openshard.routing.model_policy import (
    model_policy_from_config,
)
from openshard.routing.model_policy import (
    policy_summary as _policy_summary,
)
from openshard.routing.model_resolver import (
    ProviderAwareResolution,
    resolve_routing_model_for_context,
)
from openshard.routing.profiles import (
    ProfileDecision,
    ProfileHistorySummary,
    build_profile_history_summary,
    select_profile,
)
from openshard.routing.provider_availability import (
    build_routable_pool,
    detect_provider_availability,
)
from openshard.routing.workflow_selector import (
    WorkflowHistorySummary,
    build_workflow_history_summary,
    select_workflow,
)
from openshard.run._pipeline_helpers import (
    _LOG_PATH,
    _build_osn_verification_contract_with_loop,
    _build_retry_prompt,
    _copy_cwd_to_workspace,
    _detect_command,  # noqa: F401
    _log_run,
    _parse_cost_hint,
    _populate_context_usage_metadata,
    _populate_execution_span_metadata,
    _pre_run_cost_hint,
    _promote_sandbox_git_metadata,
    _run_verification,  # noqa: F401
    _safe_git_info,  # noqa: F401
    _serialize_verification_plan,  # noqa: F401
    _suggest_executor,
    _write_files,
)
from openshard.run.timeline import RunTimelineEvent, make_timeline_event
from openshard.run.validator_policy import ValidatorPolicyDecision, should_run_validator
from openshard.scoring.requirements import requirements_from_category
from openshard.scoring.scorer import ScoredRoutingResult, select_with_info
from openshard.security.paths import UnsafePathError, resolve_safe_repo_path
from openshard.skills.context import build_skills_context
from openshard.skills.discovery import discover_skills
from openshard.skills.matcher import MatchedSkill, match_skills
from openshard.verification.executor import confirm_or_abort
from openshard.verification.executor import (  # noqa: F401
    run_verification_plan as _run_verification_plan,
)
from openshard.verification.plan import (
    CommandSafety,
    build_verification_plan,
    safe_check_label,
)


def _build_explicit_file_context(task: str, *, root: Path | None = None) -> str:
    """Return a rendered evidence block for repo-relative file paths named in *task*.

    Used by the direct/staged execution path where NativeAgentExecutor is not active.
    Returns "" when no safe, readable, named files are found.
    """
    from openshard.native.context import NativeEvidence, NativeFileSnippet, render_native_evidence
    from openshard.native.executor import (
        _MAX_EXPLICIT_SNIPPET_FILES,
        _build_explicit_file_outline,
        _extract_explicit_file_paths,
    )

    paths = _extract_explicit_file_paths(task)
    if not paths:
        return ""

    repo_root = root or Path.cwd()
    snippets: list[NativeFileSnippet] = []
    for path_str in paths[:_MAX_EXPLICIT_SNIPPET_FILES]:
        try:
            resolved = resolve_safe_repo_path(repo_root, path_str)
        except UnsafePathError:
            continue
        if not resolved.is_file():
            continue
        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
            outline = _build_explicit_file_outline(content, path_str)
            if not outline:
                continue
            snippets.append(NativeFileSnippet(path=path_str, lines=outline))
        except Exception:
            continue

    if not snippets:
        return ""
    return render_native_evidence(
        NativeEvidence(file_snippets=snippets),
        limit=3000,
        max_lines_per_snippet=25,
    )


def _emit_review_debug(
    *,
    effective_executor: str,
    selected_workflow: str,
    opencode_explicit: bool,
    task: str,
    exec_result_summary: str,
    exec_result_files_count: int,
    exec_result_notes: list,
    usage_info: Any,
    extraction_source: "str | None",
    findings_count: int,
) -> None:
    import os as _os
    if _os.environ.get("OPENSHARD_DEBUG_REVIEW", "").strip() != "1":
        return
    _public_label = "OpenCode" if effective_executor == "opencode" else "OpenShard Native"
    lines = [
        "=== OPENSHARD_DEBUG_REVIEW ===",
        f"executor_path:     {effective_executor}",
        f"public_label:      {_public_label}",
        f"selected_workflow: {selected_workflow}",
        f"opencode_explicit: {opencode_explicit}",
        f"is_review_task:    {'STRUCTURED_FINDINGS:' in task}",
        f"task_has_suffix:   {'After completing your analysis' in task}",
        f"task_preview:      {task[:500]!r}",
        "---",
        f"summary_len:       {len(exec_result_summary)}",
        f"summary_preview:   {exec_result_summary[:3000]!r}",
        "---",
        f"files_count:       {exec_result_files_count}",
        f"notes:             {exec_result_notes!r}",
        f"usage:             {usage_info!r}",
        "---",
        f"extraction_source: {extraction_source or 'none (fallback)'}",
        f"findings_count:    {findings_count}",
        "=== END DEBUG ===",
    ]
    block = "\n".join(lines) + "\n"
    sys.stderr.write(block)
    sys.stderr.flush()
    _dot_openshard = Path.cwd() / ".openshard"
    if _dot_openshard.is_dir():
        _log_dir = _dot_openshard / "debug"
        _log_dir.mkdir(parents=True, exist_ok=True)
        _log_file = _log_dir / "review-debug.log"
    else:
        import tempfile as _tmp
        _log_file = Path(_tmp.gettempdir()) / "openshard-review-debug.log"
    try:
        with _log_file.open("a", encoding="utf-8") as _f:
            _f.write(block)
    except OSError:
        pass


@dataclass
class RunResult:
    """Structured return value from RunPipeline.run()."""
    exit_code: int = 0
    start: float = 0.0
    generator: Any = None
    retry_triggered: bool = False
    final_files: list = field(default_factory=list)
    usage: Any = None
    retry_usage: Any = None
    routed_model: str | None = None
    stage_runs: list = field(default_factory=list)
    routing_decision: Any = None
    scored: Any = None
    repo_facts: Any = None
    matched_skills: list = field(default_factory=list)
    profile_decision: Any = None
    verification_plan: Any = None
    verification_attempted: bool = False
    verification_passed: bool | None = None
    workspace: Any = None
    result_summary: str = ""
    result_notes: list = field(default_factory=list)
    native_meta: Any = None
    form_factor_decision: Any = None


class RunPipeline:
    """Owns the run lifecycle: routing, execution, write/verify/retry, and logging."""

    def __init__(
        self,
        config: dict,
        *,
        write: bool,
        verify: bool,
        dry_run: bool,
        no_shrink: bool,
        workflow: str | None,
        profile: str | None,
        executor: str | None,
        plan_flag: bool,
        approval: str | None,
        provider: str | None,
        history_scoring: bool,
        eval_scoring: bool,
        feedback_scoring: bool,
        detail: str,
        native_backend: str | None = None,
        experimental_deepagents_run: bool = False,
        experimental_tier_dispatch: bool = False,
        native_loop: str | None = None,
        model_policy: str | None = None,
        candidates: int = 1,
        shard_id: str | None = None,
    ) -> None:
        self._config = config
        self._shard_id = shard_id
        self._write = write
        self._verify = verify
        self._dry_run = dry_run
        self._no_shrink = no_shrink
        self._workflow = workflow
        self._profile = profile
        self._executor = executor
        self._plan_flag = plan_flag
        self._approval = approval
        self._provider = provider
        self._history_scoring = history_scoring
        self._eval_scoring = eval_scoring
        self._feedback_scoring = feedback_scoring
        self._detail = detail
        self._native_backend = native_backend or "builtin"
        self._experimental_deepagents_run = experimental_deepagents_run
        self._experimental_tier_dispatch = experimental_tier_dispatch
        self._native_loop = native_loop
        self._model_policy = model_policy
        self._candidates = candidates

    def _tier_dispatch_enabled(self) -> bool:
        """True when per-role tier dispatch is on, via CLI flag or config.

        Mirrors how the scoring flags read config: the
        ``--experimental-tier-dispatch`` flag or ``tier_dispatch: true`` in
        config turns it on. Default off.
        """
        return bool(self._experimental_tier_dispatch) or bool(
            self._config.get("tier_dispatch", False)
        )

    def run(self, task: str) -> RunResult:  # noqa: C901
        result_obj = RunResult()
        detail = self._detail
        write = self._write
        verify = self._verify
        dry_run = self._dry_run
        no_shrink = self._no_shrink
        workflow = self._workflow
        profile = self._profile
        executor = self._executor
        plan_flag = self._plan_flag
        approval = self._approval
        provider = self._provider
        history_scoring = self._history_scoring
        eval_scoring = self._eval_scoring
        feedback_scoring = self._feedback_scoring

        start = time.time()
        result_obj.start = start
        _run_id = datetime.datetime.fromtimestamp(
            start, tz=datetime.UTC
        ).isoformat().replace("+00:00", "Z")
        retry_triggered = False
        workspace: Path | None = None
        verification_passed: bool | None = None
        usage = None
        retry_usage = None
        _timeline: list[RunTimelineEvent] = []

        try:
            _timeline.append(make_timeline_event("run_started", "Started run", kind="run"))
            _cfg = self._config
            _cfg_workflow = _cfg.get("workflow", "").strip().lower()
            _cfg_executor_legacy = _cfg.get("executor", "direct").strip().lower()
            _use_history_scoring = history_scoring or bool(_cfg.get("history_scoring", False))
            _use_eval_scoring = eval_scoring or bool(_cfg.get("eval_scoring", False))
            _use_feedback_scoring = feedback_scoring or bool(_cfg.get("feedback_scoring", False))
            # Failure-memory routing signal is active by default (penalty-only and
            # conservative). Set failure_memory_scoring: false in config to disable.
            _use_failure_memory_scoring = bool(_cfg.get("failure_memory_scoring", True))
            _policy_executor, _policy_reason, _executor_advisory_result = _suggest_executor(task)
            _executor_source: str = "heuristic"
            _fra_result: dict | None = None

            # --workflow > --executor (deprecated) > config.workflow > config.executor > auto
            _show_executor_deprecation = False
            _pack_label: str = ""
            if workflow is not None:
                _wf = workflow.lower()
                if _wf in ("claude-code", "codex"):
                    raise click.ClickException(
                        f"--workflow {_wf!r} is not yet available. "
                        "Use: auto, direct, staged, opencode, or native."
                    )
                effective_workflow = _wf
                _policy_reason = ""
                _executor_source = "override"
                _timeline.append(make_timeline_event("workflow_pack_loaded", "Loaded workflow pack", kind="workflow", target=_wf))
                try:
                    from openshard.workflow_packs.packs import load_packs as _load_packs_fn
                    _pack_label = next(
                        (p.title for p in _load_packs_fn() if p.prompt and task.startswith(p.prompt)),
                        "",
                    )
                except Exception:
                    _pack_label = ""
            elif executor is not None:
                _show_executor_deprecation = True
                effective_workflow = executor.lower()
                _policy_reason = ""
                _executor_source = "override"
            elif _cfg_workflow:
                if _cfg_workflow in ("claude-code", "codex"):
                    raise click.ClickException(
                        f"config.workflow: {_cfg_workflow!r} is not yet available. "
                        "Use: auto, direct, staged, opencode, or native."
                    )
                effective_workflow = _cfg_workflow
                _policy_reason = ""
            elif _cfg_executor_legacy != "direct":
                effective_workflow = _cfg_executor_legacy
                _policy_reason = ""
            else:
                effective_workflow = "auto"

            if _show_executor_deprecation:
                click.echo(
                    "  [deprecation] --executor is deprecated; use --workflow instead.",
                    err=True,
                )

            # Map workflow to effective_executor and stage-forcing behaviour.
            # _force_stages=True → always stage; False → never stage; None → auto (routing-driven).
            _force_stages: bool | None = None
            if effective_workflow == "auto":
                import shutil as _shutil_exec
                _oc_avail = bool(_shutil_exec.which("opencode"))
                _oc_pref = False  # opencode preference requires explicit --workflow opencode
                # Run advisory with _for_dispatch=True so advisory_only=False on
                # the result (records that advisory was consulted for dispatch).
                # Effective executor: advisory recommendation is applied only when
                # the heuristic also selects "native" (complex/multi-file tasks).
                # For simple tasks the heuristic "direct" is preserved so existing
                # direct-path pipeline behaviour is unchanged.
                _adv_executor, _adv_reason, _adv_result = _suggest_executor(
                    task,
                    category="standard",  # routing_decision not yet computed at this point
                    read_only=is_readonly_task(task),
                    opencode_available=_oc_avail,
                    opencode_preference=_oc_pref,
                    _for_dispatch=True,
                )
                # _policy_executor comes from the simple heuristic (no _for_dispatch).
                # Use advisory recommendation when both heuristic and advisory agree
                # on "native", or when advisory has a non-default reason.
                if _policy_executor == "native" or _adv_executor == "native" and (
                    len(task.split()) > 60 or any(
                        kw in task.lower() for kw in (
                            "all files", "every file", "multiple files", "across the",
                            "throughout the", "codebase",
                        )
                    )
                ):
                    effective_executor = _adv_executor
                    _policy_reason = _adv_reason
                    _executor_source = "advisory"
                    # Native executor manages its own multi-step workflow.
                    if effective_executor == "native":
                        _force_stages = False
                else:
                    effective_executor = _policy_executor
                    _executor_source = "heuristic"
                _executor_advisory_result = _adv_result
            elif effective_workflow == "direct":
                effective_executor = "direct"
                _force_stages = False
                _executor_source = "override"
            elif effective_workflow == "staged":
                effective_executor = "direct"
                _force_stages = True
                _policy_reason = ""
                _executor_source = "override"
            elif effective_workflow == "opencode":
                effective_executor = "opencode"
                _force_stages = False
                _policy_reason = ""
                _executor_source = "override"
            elif effective_workflow == "native":
                effective_executor = "native"
                _force_stages = False
                _policy_reason = ""
                _executor_source = "override"
            else:
                # Fallback (shouldn't reach here after earlier validation)
                effective_executor = "direct"
                _executor_source = "heuristic"

            # Resolve provider (only applies to direct executor).
            # detect_provider() raises ValueError (caught below) when no key is set,
            # giving a clean error message instead of a traceback later.
            # For openrouter the client is still created lazily inside ExecutionGenerator;
            # the key existence is already confirmed here so the lazy path will succeed.
            _provider_instance = None
            _provider_name = (provider or detect_provider()).lower()
            if effective_executor != "opencode":
                if _provider_name == "anthropic":
                    from openshard.providers.anthropic import AnthropicProvider
                    _provider_instance = AnthropicProvider(get_anthropic_api_key())
                elif _provider_name == "openai":
                    from openshard.providers.openai import OpenAIProvider
                    _provider_instance = OpenAIProvider(get_openai_api_key())

            if effective_executor == "opencode":
                generator: ExecutionGenerator | OpenCodeExecutor | NativeAgentExecutor = OpenCodeExecutor()
            elif effective_executor == "native":
                generator = NativeAgentExecutor(
                    provider=_provider_instance,
                    repo_root=Path.cwd(),
                    backend_name=self._native_backend,
                    experimental_deepagents_run=self._experimental_deepagents_run,
                    native_loop=self._native_loop,
                    model_policy=self._model_policy,
                )
                if hasattr(generator, "_run_id"):
                    generator._run_id = _run_id
            else:
                generator = ExecutionGenerator(provider=_provider_instance)
        except (ValueError, RuntimeError) as exc:
            raise click.ClickException(str(exc))

        # Resolve Shard/Attempt identity before any generation happens, so an
        # unknown --shard fails fast instead of after spending a model call.
        _receipt_index: int | None = None
        try:
            _log_p = Path.cwd() / _LOG_PATH
            if _log_p.exists():
                with _log_p.open("r", encoding="utf-8") as _lf:
                    _receipt_index = sum(1 for _ in _lf)
        except Exception:
            pass
        from openshard.history.run_attempt import resolve_shard_for_attempt
        _resolved_shard_id, _resolved_attempt_number = resolve_shard_for_attempt(
            self._shard_id, load_runs(), _run_id, _receipt_index,
        )

        opencode_mode = (effective_executor == "opencode")
        routing_decision: RoutingDecision | None = route(task) if not opencode_mode else None
        if routing_decision is not None and (is_readonly_task(task) or has_inline_readonly_instruction(task)):
            routing_decision.rationale = "read-only analysis"

        # Build the routable pool once; used for both enforcement and Shard logging.
        # Skipped silently if provider detection fails (degraded gracefully).
        _routable_pool_cache = None
        _provider_enforcement_result: ProviderAwareResolution | None = None
        _pa_detected: tuple = ()
        _model_policy = None
        _model_policy_summary: dict | None = None
        try:
            _model_policy = model_policy_from_config(self._config)
            _model_policy_summary = _policy_summary(_model_policy)
        except Exception:
            pass
        try:
            _pa = detect_provider_availability()
            _pa_detected = _pa.detected
            _routable_pool_cache = build_routable_pool(
                _pa, executor=effective_executor, policy=_model_policy
            )
        except Exception:
            pass

        # Enforce provider-aware routing: replace the keyword-routed model with
        # the best model the user can actually call given their API keys and executor.
        # Enforcement only applies when at least one provider key is present; when no
        # keys are detected the pipeline will already have raised at detect_provider().
        _CATEGORY_TO_ROLE: dict[str, str] = {
            "boilerplate": "cheap",
            "standard":    "main",
            "security":    "strong",
            "visual":      "visual",
            "complex":     "complex",
        }
        if (
            routing_decision is not None
            and _routable_pool_cache is not None
            and _pa_detected
        ):
            # Fail clearly if model policy has removed all routable models
            # before we attempt enforcement.
            if not _routable_pool_cache.routable:
                _providers_str = ", ".join(_pa_detected)
                _excluded_sample = list(_routable_pool_cache.excluded[:5])
                raise click.ClickException(
                    "Model policy filtered out all routable models. "
                    "Check your [models] config section. "
                    f"Available providers: {_providers_str}. "
                    f"Excluded (sample): {_excluded_sample}"
                )
            _role = _CATEGORY_TO_ROLE.get(routing_decision.category, "main")
            _provider_enforcement_result = resolve_routing_model_for_context(
                _role, _routable_pool_cache
            )
            if _provider_enforcement_result.model is None:
                _providers_str = ", ".join(_pa_detected)
                raise click.ClickException(
                    f"No routable model for role '{_role}' — "
                    f"available providers: {_providers_str}. "
                    "Check your API keys or executor configuration."
                )
            routing_decision = RoutingDecision(
                model=_provider_enforcement_result.model,
                category=routing_decision.category,
                rationale=routing_decision.rationale,
            )
        if _force_stages is True:
            _use_stages = not opencode_mode
        elif _force_stages is False:
            _use_stages = False
        else:
            _use_stages = None  # resolved below after history + repo_facts
        stage_runs: list[StageRun] = []

        def _safe_checkpoint(stage: str, status: str, **kwargs) -> None:
            try:
                _wp = str(workspace) if workspace is not None else ""
                _log_run_checkpoint(NativeRunCheckpointEvent(
                    run_id=_run_id,
                    workflow=effective_workflow,
                    executor=effective_executor,
                    stage=stage,
                    status=status,
                    workspace_path=_wp,
                    sandbox_path=_wp,
                    files=list(kwargs.get("files", [])),
                    verification_status=kwargs.get("verification_status", ""),
                    retry_used=bool(kwargs.get("retry_used", False)),
                    reason=kwargs.get("reason", ""),
                ))
            except Exception:
                pass

        if opencode_mode:
            workspace = Path(tempfile.mkdtemp())
            _copy_cwd_to_workspace(workspace)
            if detail == "full":
                click.echo(f"\n  [workspace] {workspace}")

        spinner = _Spinner()
        click.echo("")
        if detail != "default" and _policy_reason:
            click.echo(f"  [executor] {effective_executor} - {_policy_reason}")
        _routed_model = routing_decision.model if routing_decision else None
        _scored: ScoredRoutingResult | None = None
        _runs: list[dict] = []
        _feedback_receipt: dict | None = None
        _failure_receipt: dict | None = None

        # Attempt scored model selection; fall back silently to keyword routing.
        if not opencode_mode and routing_decision is not None:
            try:
                _mgr = ProviderManager()
                _inv = _mgr.get_inventory()
                _reqs = requirements_from_category(routing_decision.category)
                _entries = [e for e in _inv.models if e.provider == _provider_name]
                _hist_adjustments: dict[str, float] | None = None
                _hist_reasons: dict[str, str] = {}
                if _use_history_scoring:
                    try:
                        _runs = load_runs()
                        _hist_adjustments = compute_history_adjustments(_runs)
                        _hist_reasons = compute_history_adjustment_reasons(_runs)
                    except Exception:
                        _runs = []
                _eval_adjustments: dict[str, float] = {}
                _eval_reasons: dict[str, str] = {}
                _cat_adjustments: dict[str, float] = {}
                _cat_reasons: dict[str, str] = {}
                _eval_records: list[dict] = []
                if _use_eval_scoring:
                    try:
                        from openshard.evals.adjustments import (
                            compute_eval_adjustment_reasons,
                            compute_eval_adjustments,
                        )
                        from openshard.evals.stats import (
                            EVAL_RUNS_PATH,
                            compute_eval_stats,
                            load_eval_runs,
                        )
                        _eval_records = load_eval_runs(Path.cwd() / EVAL_RUNS_PATH)
                        _eval_stats_data = compute_eval_stats(_eval_records)
                        _eval_adjustments = compute_eval_adjustments(_eval_stats_data)
                        _eval_reasons = compute_eval_adjustment_reasons(_eval_stats_data)
                    except Exception:
                        pass
                    if routing_decision and routing_decision.category and _eval_records:
                        try:
                            from openshard.evals.adjustments import (
                                compute_category_eval_adjustment_reasons,
                                compute_category_eval_adjustments,
                            )
                            from openshard.evals.registry import build_category_map
                            from openshard.evals.stats import compute_category_stats
                            _suites = {r.get("suite") for r in _eval_records if r.get("suite")}
                            _cat_maps: dict[str, dict[str, str]] = {}
                            for _s in _suites:
                                try:
                                    _cat_maps[str(_s)] = build_category_map(str(_s))
                                except Exception:
                                    pass
                            if _cat_maps:
                                _cat_stats = compute_category_stats(_eval_records, _cat_maps)
                                _cat_adjustments = compute_category_eval_adjustments(
                                    _cat_stats, routing_decision.category
                                )
                                _cat_reasons = compute_category_eval_adjustment_reasons(
                                    _cat_stats, routing_decision.category
                                )
                        except Exception:
                            pass
                _feedback_adjustments: dict[str, float] = {}
                _feedback_reasons: dict[str, str] = {}
                if _use_feedback_scoring:
                    try:
                        from openshard.history.interactions import load_interaction_events
                        _fb_runs = load_runs()
                        _fb_events = load_interaction_events()
                        _feedback_adjustments = compute_feedback_adjustments(
                            _fb_runs, interaction_events=_fb_events
                        )
                        _feedback_reasons = compute_feedback_adjustment_reasons(
                            _fb_runs, interaction_events=_fb_events
                        )
                        _feedback_receipt = {
                            "routing_feedback_scoring_used": True,
                            "routing_feedback_adjustments": {
                                m: round(v, 3) for m, v in _feedback_adjustments.items()
                            },
                            "routing_feedback_reasons": dict(_feedback_reasons),
                        }
                    except Exception:
                        pass
                # Failure-memory routing signal — penalty for models that
                # repeatedly fail real runs. Active by default; never raises.
                _failure_adjustments: dict[str, float] = {}
                _failure_reasons: dict[str, str] = {}
                if _use_failure_memory_scoring:
                    try:
                        from openshard.history.failure_memory import (
                            compute_failure_memory_adjustment_reasons,
                            compute_failure_memory_adjustments,
                            load_failure_memory_events,
                        )
                        _fm_events = load_failure_memory_events()
                        _failure_adjustments = compute_failure_memory_adjustments(_fm_events)
                        _failure_reasons = compute_failure_memory_adjustment_reasons(_fm_events)
                        if _failure_adjustments:
                            _failure_receipt = {
                                "routing_failure_memory_scoring_used": True,
                                "routing_failure_memory_adjustments": {
                                    m: round(v, 3) for m, v in _failure_adjustments.items()
                                },
                                "routing_failure_memory_reasons": dict(_failure_reasons),
                            }
                    except Exception:
                        pass
                # Feedback Routing Advisory (FRA) — session signal-based
                # penalties applied before scoring. Soft signal only; never
                # raises. Stored for Shard recording via _log_run.
                _fra_result: dict | None = None
                _fra_adjustments: dict[str, float] = {}
                try:
                    from openshard.models.feedback_advisory import (
                        _load_recent_session_signals as _fra_load_sigs,
                    )
                    from openshard.models.feedback_advisory import (
                        build_feedback_routing_advisory as _build_fra,
                    )
                    from openshard.models.registry import get_model as _fra_get_model
                    _fra_sig_path = Path(".openshard") / "session_signals.jsonl"
                    _fra_sigs = _fra_load_sigs(_fra_sig_path)
                    _fra_result = _build_fra(_fra_sigs)
                    if _fra_result is not None and _fra_result.get("recommendation") == "consider_stronger_review":
                        _fra_confidence = _fra_result.get("confidence", "low")
                        _fra_penalty = -1.5 if _fra_confidence == "medium" else -0.5
                        for _fe in _entries:
                            _freg = _fra_get_model(_fe.model.id)
                            if _freg is not None and _freg.cost_class == "cheap":
                                _fra_adjustments[_fe.model.id] = _fra_penalty
                except Exception:
                    pass

                _merged_adjustments: dict[str, float] | None = None
                if _hist_adjustments is not None or _eval_adjustments or _cat_adjustments or _feedback_adjustments or _fra_adjustments or _failure_adjustments:
                    _merged_adjustments = dict(_hist_adjustments or {})
                    _all_eval_models = set(_eval_adjustments) | set(_cat_adjustments)
                    for _em in _all_eval_models:
                        _combined = _eval_adjustments.get(_em, 0.0) + _cat_adjustments.get(_em, 0.0)
                        from openshard.evals.adjustments import _ADJ_MAX, _ADJ_MIN
                        _combined = max(_ADJ_MIN, min(_ADJ_MAX, _combined))
                        if _combined != 0.0:
                            _merged_adjustments[_em] = _merged_adjustments.get(_em, 0.0) + _combined
                    _merged_clamp_min, _merged_clamp_max = -2.0, 1.0
                    for _fm, _fval in _feedback_adjustments.items():
                        _merged_adjustments[_fm] = max(
                            _merged_clamp_min, min(_merged_clamp_max, _merged_adjustments.get(_fm, 0.0) + _fval)
                        )
                    for _frm, _frval in _fra_adjustments.items():
                        _merged_adjustments[_frm] = max(
                            _merged_clamp_min, min(_merged_clamp_max, _merged_adjustments.get(_frm, 0.0) + _frval)
                        )
                    for _flm, _flval in _failure_adjustments.items():
                        _merged_adjustments[_flm] = max(
                            _merged_clamp_min, min(_merged_clamp_max, _merged_adjustments.get(_flm, 0.0) + _flval)
                        )
                _scored = select_with_info(_entries, _reqs, routing_decision.category, history_adjustments=_merged_adjustments)
                if (
                    _scored.selected_model is not None
                    and _mgr.providers.get(_scored.selected_provider) is not None
                ):
                    _routed_model = _scored.selected_model
                else:
                    _scored = ScoredRoutingResult(
                        category=_scored.category,
                        requirements=_scored.requirements,
                        candidate_count=_scored.candidate_count,
                        selected_model=None,
                        selected_provider=None,
                        used_fallback=True,
                    )
            except Exception:
                pass

        _repo_facts: RepoFacts | None = None
        try:
            _repo_facts = analyze_repo(Path.cwd())
            _timeline.append(make_timeline_event("repo_scanned", "Scanned repo", kind="scan"))
        except Exception:
            pass

        if _routed_model:
            _timeline.append(make_timeline_event(
                "model_selected",
                f"Routed to {_model_label(_routed_model)}",
                kind="route",
                target=_routed_model,
            ))

        _readonly_task = is_readonly_task(task) or has_inline_readonly_instruction(task)
        _is_sf_task = "STRUCTURED_FINDINGS:" in task
        _is_readonly_review_task = _readonly_task and looks_like_review_task(task)
        _generation_max_tokens: int = 8192 if _is_sf_task else 16384

        _verification_plan = build_verification_plan(_cfg, _repo_facts)

        if (
            effective_executor == "native"
            and _verification_plan.has_commands
            and hasattr(generator, "build_command_policy_preview")
        ):
            generator.build_command_policy_preview(_verification_plan)

        if _use_stages is None:
            _category = routing_decision.category if routing_decision else "standard"
            _workflow_history_summary: WorkflowHistorySummary | None = None
            if _use_history_scoring and _runs:
                _workflow_history_summary = build_workflow_history_summary(_runs, _category)
            _wf_decision = select_workflow(
                category=_category,
                repo_facts=_repo_facts,
                history_summary=_workflow_history_summary,
                verify_enabled=verify,
                readonly=_readonly_task,
            )
            _wf_choice = _wf_decision.workflow
            _wf_reason = _wf_decision.reason
            _use_stages = not opencode_mode and (_wf_choice == "staged")

        _profile_history_summary: dict[str, ProfileHistorySummary] | None = None
        if _use_history_scoring and _runs:
            _profile_history_summary = build_profile_history_summary(_runs)
        _profile_decision: ProfileDecision = select_profile(
            category=routing_decision.category if routing_decision else "standard",
            repo_facts=_repo_facts,
            task=task,
            override=profile,
            history_summary=_profile_history_summary,
        )

        _form_factor_decision: ExecutionFormFactorDecision = select_form_factor(
            category=routing_decision.category if routing_decision else "standard",
            readonly=_readonly_task,
            workflow="staged" if _use_stages else "direct",
            profile_name=_profile_decision.profile,
            repo_facts=_repo_facts,
            write_requested=write,
            verification_available=bool(
                _verification_plan is not None and _verification_plan.has_commands
            ),
            native_loop=self._native_loop,
            experimental_deepagents_run=self._experimental_deepagents_run,
        )

        _cfg_approval = _cfg.get("approval_mode", "smart").strip().lower()
        if _cfg_approval not in VALID_APPROVAL_MODES:
            raise click.ClickException(
                f"Invalid approval_mode {_cfg_approval!r} in config. "
                f"Valid values: {', '.join(sorted(VALID_APPROVAL_MODES))}"
            )
        _approval_mode = approval.lower() if approval else _cfg_approval
        _cost_threshold = float(_cfg.get("cost_gate_threshold", 0.10))
        gate = GateEvaluator(
            approval_mode=_approval_mode,
            risky_paths=_repo_facts.risky_paths if _repo_facts is not None else [],
            cost_threshold=_cost_threshold,
        )

        # Routing section lines are collected here and printed together after workflow is resolved.
        _routing_lines: list[str] = []
        if routing_decision is not None:
            if detail != "default":
                _routing_lines.append(f"  Task type: {routing_decision.rationale}")
            hint = _pre_run_cost_hint(routing_decision.model, task)
            if hint:
                click.echo(f"  Cost estimate: {hint}")
                _cost_val = _parse_cost_hint(hint)
                if _cost_val is not None:
                    _cost_dec = gate.check_high_cost(_cost_val)
                    if _cost_dec.required:
                        confirm_or_abort(_cost_dec.reason)

        if detail != "default" and _scored is not None:
            _req = _scored.requirements
            _req_parts: list[str] = []
            if _req.security_sensitive:
                _req_parts.append("security_sensitive")
            if _req.needs_vision:
                _req_parts.append("needs_vision")
            if _req.needs_tools:
                _req_parts.append("needs_tools")
            if _req.complexity != "standard":
                _req_parts.append(_req.complexity)
            if _req.min_context_window:
                _req_parts.append(f"ctx>={_req.min_context_window // 1_000}k")
            _req_str = ", ".join(_req_parts) if _req_parts else "none"
            _routing_lines.append(f"  Category: {_scored.category}")
            if _req_str != "none":
                _routing_lines.append(f"  Requirements: {_req_str}")
            if _scored.used_fallback:
                _routing_lines.append(f"  Candidates: {_scored.candidate_count} (fallback keyword routing)")
            else:
                _cost_str = f"cost: ${_scored.selected_cost_per_m:.2f}/M" if _scored.selected_cost_per_m is not None else "cost: unknown"
                _routing_lines.append(f"  Initial candidate: {_model_label(_scored.selected_model)} ({_cost_str})")
                _routing_lines.append(f"  Candidates: {_scored.candidate_count}")
            if _use_history_scoring:
                _routing_lines.append("  History scoring: enabled")
                _nonzero = [
                    (m, adj)
                    for m, adj in _scored.history_adjustments.items()
                    if m in set(_scored.candidates) and adj != 0.0
                ]
                for _hm, _hadj in _nonzero:
                    _rsn = _hist_reasons.get(_hm, "")
                    _rsn_str = f" ({_rsn})" if _rsn else ""
                    _marker = " <- selected" if _hm == _scored.selected_model else ""
                    _routing_lines.append(f"    {_model_label(_hm)}: {_hadj:+.1f}{_rsn_str}{_marker}")
            if _use_eval_scoring:
                _routing_lines.append("  eval scoring: enabled")
                _eval_nonzero = [
                    (m, adj)
                    for m, adj in _eval_adjustments.items()
                    if m in set(_scored.candidates) and adj != 0.0
                ]
                for _em, _ea in _eval_nonzero:
                    _rsn = _eval_reasons.get(_em, "")
                    _rsn_str = f" ({_rsn})" if _rsn else ""
                    _marker = " <- selected" if _em == _scored.selected_model else ""
                    _routing_lines.append(f"    {_model_label(_em)}: {_ea:+.1f}{_rsn_str}{_marker}")
                if not _eval_nonzero:
                    _routing_lines.append("    No relevant stats (no adjustment)")
                _cat_label = routing_decision.category if routing_decision else "?"
                _cat_nonzero = [
                    (m, adj)
                    for m, adj in _cat_adjustments.items()
                    if m in set(_scored.candidates) and adj != 0.0
                ]
                if _cat_nonzero:
                    for _cm, _ca in _cat_nonzero:
                        _rsn = _cat_reasons.get(_cm, "")
                        _rsn_str = f" ({_rsn})" if _rsn else ""
                        _marker = " <- selected" if _cm == _scored.selected_model else ""
                        _routing_lines.append(f"    [{_cat_label}] {_model_label(_cm)}: {_ca:+.1f}{_rsn_str}{_marker}")
                else:
                    _routing_lines.append(f"    [{_cat_label}] No category evidence (global only)")
            if _use_feedback_scoring:
                _routing_lines.append("  Feedback scoring: enabled")
                _fb_nonzero = [
                    (m, adj)
                    for m, adj in _feedback_adjustments.items()
                    if m in set(_scored.candidates) and adj != 0.0
                ]
                for _fm, _fa in _fb_nonzero:
                    _rsn = _feedback_reasons.get(_fm, "")
                    _rsn_str = f" ({_rsn})" if _rsn else ""
                    _marker = " <- selected" if _fm == _scored.selected_model else ""
                    _routing_lines.append(f"    {_model_label(_fm)}: {_fa:+.1f}{_rsn_str}{_marker}")
                if not _fb_nonzero:
                    _routing_lines.append("    No relevant feedback (no adjustment)")

        _matched_skills: list[MatchedSkill] = []
        if _repo_facts is not None and routing_decision is not None:
            try:
                _local_skills = discover_skills(Path.cwd())
                if _local_skills:
                    _matched_skills = match_skills(
                        _local_skills, task, routing_decision.category, _repo_facts
                    )
            except Exception:
                pass
        _skills_ctx = build_skills_context(_matched_skills)

        if effective_executor != "native":
            _explicit_ctx = _build_explicit_file_context(task)
            if _explicit_ctx:
                _skills_ctx = f"{_skills_ctx}\n\n{_explicit_ctx}" if _skills_ctx else _explicit_ctx

        if (_readonly_task or _is_sf_task) and not dry_run:
            if "STRUCTURED_FINDINGS:" in task:
                # Review/security pack: model must put COMPLETE multi-line analysis in summary field.
                # The generic "one sentence, third-person" instruction conflicts with this requirement.
                _rv_instruction = (
                    "\n\n[IMPORTANT] This is a read-only security/configuration review task. "
                    "Do not propose file changes. The `files` array must be empty ([]). "
                    "Your `summary` field must contain your COMPLETE multi-line analysis — "
                    "severity sections and the STRUCTURED_FINDINGS line must be inside the summary. "
                    "Do NOT truncate the summary to one sentence for this task."
                )
                _skills_ctx = f"{_skills_ctx}{_rv_instruction}" if _skills_ctx else _rv_instruction
            else:
                _ro_instruction = (
                    "\n\n[IMPORTANT] This is a read-only analysis/explanation task. "
                    "Do not propose file changes. Do not create, update, delete, or rewrite files. "
                    "The `files` field in your response must be empty ([]). "
                    "Write your response as a third-person explanation of what the code does "
                    "(e.g. 'This file handles…', 'The function does…'). "
                    "Do NOT use phrases like 'Implemented', 'Updated', 'Created', or 'Added' "
                    "in your summary — those imply you made changes."
                )
                _skills_ctx = f"{_skills_ctx}{_ro_instruction}" if _skills_ctx else _ro_instruction

        if detail != "default":
            if _force_stages is None:
                _wf_display = _wf_choice
                _wf_display_reason = _wf_reason
            elif _force_stages:
                _wf_display = "staged"
                _wf_display_reason = "forced by workflow setting"
            else:
                _wf_display = "direct"
                _wf_display_reason = "forced by workflow setting"
            _routing_lines.append(f"  Workflow: {_wf_display}")
            _routing_lines.append(f"  Reason: {_wf_display_reason}")
            if _routing_lines:
                click.echo("Routing")
                for _rl in _routing_lines:
                    click.echo(_rl)
            click.echo("")
            click.echo("Execution")
            click.echo(f"  Mode: {_profile_display_label(_profile_decision.profile, is_readonly=_readonly_task)}")
            _exec_reason = "read-only task — direct analysis" if _readonly_task else _profile_decision.reason
            click.echo(f"  Reason: {_exec_reason}")
            click.echo("")
            click.echo("Verification")
            if _verification_plan.has_commands:
                _first_vcmd = True
                for _vcmd in _verification_plan.commands:
                    if not _first_vcmd:
                        click.echo("")
                    _first_vcmd = False
                    _argv_str = " ".join(_vcmd.argv)
                    click.echo(f"  Name: {_vcmd.name}")
                    click.echo(f"  Safety: {_vcmd.safety.value}")
                    click.echo(f"  Source: {_vcmd.source.value}")
                    click.echo(f"  Command: {_argv_str}")
            else:
                click.echo("  No verification command detected")

        if detail != "default" and _matched_skills:
            click.echo("\nSkills")
            for _ms in _matched_skills:
                _rsn = ", ".join(_ms.reasons)
                click.echo(f"  {_ms.skill.slug}: {_ms.skill.name} ({_rsn})")

        if detail == "default":
            _routing_msg = _build_routing_line(routing_decision, _use_stages, actual_model=_routed_model)
            if _routing_msg:
                click.echo(_routing_msg)

        # --- Experimental tier dispatch (non-native, non-opencode only) ----------
        _tier_dispatch_receipt = None
        _dispatch_planner_model: str | None = None
        _dispatch_executor_model: str | None = None
        _effective_model: str | None = None
        _validator_result: dict | None = None
        _validator_policy: ValidatorPolicyDecision | None = None

        _can_dispatch = (
            self._tier_dispatch_enabled()
            and not opencode_mode
            and effective_executor != "native"
        )
        if _can_dispatch:
            from openshard.native.context import NativeTierDispatchReceipt
            from openshard.native.dispatch import resolve_tier, resolve_tier_for_category

            _cat = routing_decision.category if routing_decision else None
            # Per-role dispatch must only select models that are actually
            # routable in this environment. When the routable pool is known and
            # non-empty, block everything excluded from it so resolve_tier
            # substitutes a reachable fallback; if a role then has no routable
            # model at all it resolves to None and we fall back to single-model
            # dispatch below, rather than dispatching to a model whose provider
            # is not configured (which would fail at call time). An empty pool
            # means no provider info is available here (e.g. no API keys), not
            # that every model is unroutable, so we do not over-restrict then.
            _dispatch_blocked: set[str] = set()
            if _routable_pool_cache is not None and _routable_pool_cache.routable:
                _dispatch_blocked = {_mid for _mid, _ in _routable_pool_cache.excluded}
            _p_tier = "frontier-reasoning-model"
            _p_model, _p_fb, _p_reason = resolve_tier(_p_tier, _dispatch_blocked)
            _e_model, _e_tier, _e_fb, _e_reason = resolve_tier_for_category(_cat, _dispatch_blocked)
            _v_tier = "independent-validator-model"
            _v_model, _v_fb, _v_reason = resolve_tier(_v_tier, _dispatch_blocked)

            _unresolved_roles = [
                _role for _role, _m in (
                    ("planner", _p_model),
                    ("executor", _e_model),
                    ("validator", _v_model),
                ) if _m is None
            ]
            _all_roles_resolved = not _unresolved_roles
            _any_fb = _p_fb or _e_fb or _v_fb
            _warns = [r for r in [
                (f"planner fallback: {_p_reason}" if _p_fb and _p_reason else ""),
                (f"executor fallback: {_e_reason}" if _e_fb and _e_reason else ""),
                (f"validator fallback: {_v_reason}" if _v_fb and _v_reason else ""),
            ] if r]
            if not _all_roles_resolved:
                _warns.append(
                    "per-role dispatch unavailable: no routable model for "
                    + ", ".join(_unresolved_roles)
                    + "; using single model"
                )
            _first_reason = next((r for r in (_p_reason, _e_reason, _v_reason) if r), "")

            if dry_run:
                _not_applied_reason = "dry-run"
            elif not _all_roles_resolved:
                _not_applied_reason = "no routable model for " + ", ".join(_unresolved_roles)
            else:
                _not_applied_reason = ""
            _applied = (not dry_run) and _all_roles_resolved

            _tier_dispatch_receipt = NativeTierDispatchReceipt(
                enabled=True,
                applied=_applied,
                tier_source="category_fallback",
                planner_tier=_p_tier,
                planner_model=_p_model,
                executor_tier=_e_tier or "",
                executor_model=_e_model,
                validator_tier=_v_tier,
                validator_model=_v_model,
                fallback_used=_any_fb or not _all_roles_resolved,
                fallback_reason=_not_applied_reason or _first_reason,
                warnings=_warns,
            )
            if _applied:
                _dispatch_planner_model = _p_model
                _dispatch_executor_model = _e_model

        # --- --plan gate: show plan and prompt for approval before executing --------
        _impl_task = task          # may be augmented with a plan
        _plan_already_done = False

        if plan_flag:
            if effective_workflow == "opencode":
                _shape_desc = "opencode (delegated to OpenCode CLI)"
            elif _use_stages:
                _shape_desc = "staged (planning -> implementation)"
            else:
                _shape_desc = "direct single-pass"
            click.echo(f"\n  Workflow:  {_shape_desc}")
            click.echo(f"  Model:     {_model_label(_routed_model or 'unknown')}")
            _hint = _pre_run_cost_hint(routing_decision.model if routing_decision else "", task)
            if _hint:
                click.echo(f"  Est. cost: {_hint}")
            if not opencode_mode:
                spinner.start("Planning - generating implementation plan")
                try:
                    _plan_text, _ = run_planning_stage(
                        generator.client, task,
                        skills_context=_skills_ctx,
                        model=_dispatch_planner_model or MODEL_STRONG,
                    )
                    _impl_task = (
                        f"Task: {task}\n\nImplementation plan:\n{_plan_text}"
                        "\n\nExecute the task following the plan above."
                    )
                    _plan_already_done = True
                    spinner.stop()
                    click.echo(f"\n  Plan:\n{_plan_text}")
                except Exception:
                    spinner.stop()
                    click.echo("  [plan] planning call failed; will proceed without pre-run plan")
            click.echo("")
            if not click.confirm("Proceed?", default=False):
                raise click.Abort()

        # --- Stage-based execution (direct mode, security/complex tasks) ----------
        exec_result = None

        if dry_run:
            exec_result = ExecutionResult(summary="(dry run — no provider call)", files=[], notes=[], usage=None)

        if _use_stages:
            stages = split_task(task)
            for _stage in stages:
                _stage_t0 = time.time()

                if _stage.stage_type == "planning":
                    if _plan_already_done or dry_run:
                        continue
                    spinner.start("Planning - mapping out implementation approach")
                    try:
                        _plan_model = _dispatch_planner_model or MODEL_STRONG
                        _plan_text, _plan_usage = run_planning_stage(
                            generator.client, task,
                            skills_context=_skills_ctx,
                            model=_plan_model,
                        )
                        _impl_task = (
                            f"Task: {task}\n\nImplementation plan:\n{_plan_text}"
                            "\n\nExecute the task following the plan above."
                        )
                        stage_runs.append(StageRun(
                            stage=_stage,
                            model=_plan_model,
                            duration=time.time() - _stage_t0,
                            cost=_plan_usage.estimated_cost if _plan_usage else None,
                            summary="Implementation plan produced",
                        ))
                    except ProviderError:
                        _impl_task = task   # planning failed — fall back to plain task
                        if detail == "full":
                            click.echo("  [stages] planning call failed, continuing without plan")
                    finally:
                        spinner.stop()

                elif _stage.stage_type == "implementation":
                    if dry_run:
                        continue
                    _stage_model = _dispatch_executor_model or _routed_model or route_stage(_stage)
                    spinner.start(_exec_message(
                        _stage_model,
                        routing_decision.rationale if routing_decision else "",
                    ))
                    _stage_ok = False
                    try:
                        exec_result = generator.generate(_impl_task, model=_stage_model, repo_facts=_repo_facts, skills_context=_skills_ctx, max_tokens=_generation_max_tokens, is_review_task=_is_sf_task)
                        _stage_ok = True
                    except RuntimeError as exc:
                        raise click.ClickException(str(exc))
                    except ProviderAuthError:
                        raise click.ClickException(
                            "Authentication failed. Check that your provider API key is valid."
                        )
                    except ProviderRateLimitError:
                        raise click.ClickException("Rate limit exceeded. Wait a moment then try again.")
                    except ProviderError as exc:
                        raise click.ClickException(f"API error: {exc}")
                    finally:
                        spinner.stop()
                        if not _stage_ok:
                            _timeline.append(make_timeline_event("model_response_received", "Model request failed", kind="model", status="failed"))
                    _timeline.append(make_timeline_event("model_response_received", "Model responded", kind="model", target=_stage_model))
                    stage_runs.append(StageRun(
                        stage=_stage,
                        model=_stage_model,
                        duration=time.time() - _stage_t0,
                        cost=exec_result.usage.estimated_cost if exec_result.usage else None,
                        summary=exec_result.summary,
                    ))

        # --- Single-stage execution (simple tasks, opencode, stages not triggered) -
        if exec_result is None:
            _effective_model = _dispatch_executor_model or _routed_model
            _single_msg = (
                _exec_message(_effective_model, routing_decision.rationale)
                if routing_decision is not None
                else ("Executing - running with OpenCode" if opencode_mode else "Executing - running task")
            )
            if effective_executor == "native":
                from openshard.native.context import (
                    build_native_plan_ledger,
                    update_native_plan_ledger_status,
                )
                generator.native_meta.plan_ledger = build_native_plan_ledger(task)
                generator.native_meta.plan_ledger = update_native_plan_ledger_status(
                    generator.native_meta.plan_ledger, "Understand task", "passed", evidence="context prepared"
                )
                _safe_checkpoint("plan", "passed")
            spinner.start(_single_msg)
            _single_ok = False
            try:
                if opencode_mode:
                    exec_result = generator.generate(task, workspace=workspace)
                else:
                    exec_result = generator.generate(task, model=_effective_model, repo_facts=_repo_facts, skills_context=_skills_ctx, max_tokens=_generation_max_tokens, is_review_task=_is_sf_task)
                _single_ok = True
            except RuntimeError as exc:
                raise click.ClickException(str(exc))
            except ProviderAuthError:
                raise click.ClickException(
                    "Authentication failed. Check that your provider API key is valid."
                )
            except ProviderRateLimitError:
                raise click.ClickException("Rate limit exceeded. Wait a moment then try again.")
            except ProviderError as exc:
                raise click.ClickException(f"API error: {exc}")
            finally:
                spinner.stop()
                if not _single_ok:
                    _timeline.append(make_timeline_event("model_response_received", "Model request failed", kind="model", status="failed"))
            _timeline.append(make_timeline_event("model_response_received", "Model responded", kind="model", target=_effective_model))
            if effective_executor == "native" and generator.native_meta.plan_ledger is not None:
                from openshard.native.context import update_native_plan_ledger_status
                generator.native_meta.plan_ledger = update_native_plan_ledger_status(
                    generator.native_meta.plan_ledger, "Generate patch", "passed",
                    evidence=f"files={len(exec_result.files)}",
                )
                _safe_checkpoint("generate", "passed", files=[f.path for f in exec_result.files])

        # Record executor dispatch after the real call succeeded (non-native tier
        # dispatch path only; native path sets this via build_native_tier_dispatch_receipt).
        if (
            _dispatch_executor_model is not None
            and _tier_dispatch_receipt is not None
            and exec_result is not None
            and effective_executor != "native"
        ):
            _tier_dispatch_receipt.executor_model_actual = _dispatch_executor_model

        # --- Multi-candidate path (candidates > 1, native + write only) ---
        _multi_candidate_done = False
        if self._candidates > 1 and effective_executor == "native" and write and not dry_run:
            from openshard.native.sandbox import create_run_sandbox as _create_mc_sandbox
            _candidate_summary = NativeCandidateSummary(
                enabled=True,
                requested_count=self._candidates,
            )
            _candidate_workspaces: list[Path] = []
            _candidate_results: list = []
            _candidate_sb_metas: list = []
            _all_safe = (
                _verification_plan.has_commands
                and all(cmd.safety == CommandSafety.safe for cmd in _verification_plan.commands)
            )
            for _ci in range(self._candidates):
                if _ci == 0:
                    _cand_result = exec_result
                else:
                    spinner.start(f"Candidate {_ci + 1}/{self._candidates} - generating")
                    try:
                        _cand_result = generator.generate(
                            task,
                            model=_effective_model,
                            repo_facts=_repo_facts,
                            skills_context=_skills_ctx,
                        )
                    except RuntimeError as exc:
                        raise click.ClickException(str(exc))
                    except ProviderAuthError:
                        raise click.ClickException(
                            "Authentication failed. Check that your provider API key is valid."
                        )
                    except ProviderRateLimitError:
                        raise click.ClickException("Rate limit exceeded. Wait a moment then try again.")
                    except ProviderError as exc:
                        raise click.ClickException(f"API error: {exc}")
                    finally:
                        spinner.stop()
                _cand_workspace, _cand_sb_meta = _create_mc_sandbox(
                    Path.cwd(), f"{_run_id}-candidate-{_ci + 1}"
                )
                _write_files(_cand_result.files, _cand_workspace)
                if _all_safe:
                    _cand_exit, _cand_out = _run_verification_plan(
                        _verification_plan, _cand_workspace, capture=True
                    )
                    _cand_vstatus = "passed" if _cand_exit == 0 else "failed"
                    _cand_exit_code: int | None = _cand_exit
                    _cand_output_chars = len(_cand_out or "")
                else:
                    _cand_vstatus = "skipped"
                    _cand_exit_code = None
                    _cand_output_chars = 0
                record_native_candidate_attempt(
                    _candidate_summary,
                    candidate_index=_ci + 1,
                    model=_effective_model or generator.model or "",
                    sandbox_path=str(_cand_workspace),
                    files_written=[f.path for f in _cand_result.files],
                    verification_status=_cand_vstatus,
                    exit_code=_cand_exit_code,
                    output_chars=_cand_output_chars,
                )
                _candidate_workspaces.append(_cand_workspace)
                _candidate_results.append(_cand_result)
                _candidate_sb_metas.append(_cand_sb_meta)
            select_native_candidate(_candidate_summary)
            if _candidate_summary.selected_index is None:
                raise click.ClickException("No candidate result was selected.")
            generator.native_meta.candidate_summary = _candidate_summary
            _selected_list_i = _candidate_summary.selected_index - 1
            exec_result = _candidate_results[_selected_list_i]
            workspace = _candidate_workspaces[_selected_list_i]
            generator.native_meta.sandbox = _candidate_sb_metas[_selected_list_i]
            _multi_candidate_done = True

        usage = exec_result.usage
        # When stages ran, fold the planning cost into the reported total
        if stage_runs and usage is not None:
            total_stage_cost = sum(sr.cost for sr in stage_runs if sr.cost is not None)
            if usage.estimated_cost is not None:
                usage.estimated_cost = total_stage_cost
            else:
                usage.estimated_cost = total_stage_cost or None

        # --- Validator policy + stage (experimental tier dispatch only) --------------
        if _can_dispatch and _tier_dispatch_receipt is not None:
            _validator_policy = should_run_validator(
                has_validator_model=_tier_dispatch_receipt.validator_model is not None,
                dry_run=dry_run,
                can_dispatch=_can_dispatch,
                tier_dispatch_applied=_tier_dispatch_receipt.applied,
                readonly_task=_readonly_task,
                routing_category=routing_decision.category if routing_decision else "standard",
                execution_profile=_profile_decision.profile if _profile_decision else "native_light",
                workflow="staged" if _use_stages else "direct",
                risky_paths_count=len(_repo_facts.risky_paths) if _repo_facts else 0,
                verification_attempted=bool(write and verify),
            )

        if (
            _validator_policy is not None
            and _validator_policy.run
            and exec_result is not None
        ):
            _val_t0 = time.time()
            spinner.start("Validating - reviewing implementation result")
            try:
                _val_dict, _val_usage = run_validator_stage(
                    generator.client,
                    task=task,
                    result_summary=exec_result.summary,
                    notes=exec_result.notes or [],
                    model=_tier_dispatch_receipt.validator_model,
                )
                _validator_result = _val_dict
                _tier_dispatch_receipt.validator_model_actual = _tier_dispatch_receipt.validator_model
                stage_runs.append(StageRun(
                    stage=Stage(stage_type="validation", description="Review implementation result"),
                    model=_tier_dispatch_receipt.validator_model,
                    duration=time.time() - _val_t0,
                    cost=_val_usage.estimated_cost if _val_usage else None,
                    summary=f"verdict: {_val_dict.get('verdict', '?')}",
                ))
            except Exception as _val_exc:
                _validator_result = {
                    "verdict": "error",
                    "summary": str(_val_exc)[:200],
                    "model": _tier_dispatch_receipt.validator_model,
                }
                if detail == "full":
                    click.echo(f"  [validator] warning: {_val_exc}")
            finally:
                spinner.stop()

        # Record validator dispatch outcome on the non-native tier dispatch receipt.
        # (_can_dispatch already excludes native, so _tier_dispatch_receipt here is
        # always the non-native receipt built at lines 742-755.)
        if _tier_dispatch_receipt is not None:
            if _validator_result is not None:
                _tier_dispatch_receipt.validator_dispatch_status = "applied"
            elif _validator_policy is not None and not _validator_policy.run:
                _tier_dispatch_receipt.validator_dispatch_status = "skipped"

        # Safety net: discard generated file changes for read-only tasks even if the
        # model ignored the read-only instruction injected into the context.
        # For dry-run, exec_result.files is already [] so this block never fires.
        if (_readonly_task or _is_sf_task) and exec_result.files:
            click.echo(f"\nRead-only task — {len(exec_result.files)} generated change(s) discarded.")
            exec_result = ExecutionResult(
                summary=exec_result.summary,
                files=[],
                notes=exec_result.notes,
                usage=exec_result.usage,
                presend_secret_scan=exec_result.presend_secret_scan,
            )

        final_files = exec_result.files

        if not opencode_mode and _repo_facts is not None:
            _mismatches = check_stack_mismatch(final_files, _repo_facts)
            _sm_dec = gate.check_stack_mismatch(_mismatches)
            if _sm_dec.required:
                _lang_str = ", ".join(_repo_facts.languages) if _repo_facts.languages else "unknown"
                confirm_or_abort(f"{_sm_dec.reason} (repo stack: {_lang_str})")

        _is_review_task = "STRUCTURED_FINDINGS:" in task
        _review_domain = classify_review_domain(task)
        _is_iac_review = (_is_review_task or _is_readonly_review_task) and _review_domain == "terraform_iac"
        # _has_domain_review: true for any read-only/review task whose domain is
        # specific enough to warrant domain file discovery and review-mode output.
        # Includes tasks that don't match looks_like_review_task() (e.g. test-
        # coverage prompts with only an inline readonly instruction).
        _specific_domain = _review_domain not in ("generic_review", "terraform_iac")
        _has_domain_review = (
            _is_review_task or _is_readonly_review_task
            or (_readonly_task and _specific_domain)
        )
        _TASK_SUFFIX_STRIP = "\n\nAfter completing your analysis"
        _task_display = task[:task.index(_TASK_SUFFIX_STRIP)].rstrip() if _TASK_SUFFIX_STRIP in task else task
        _clean_summary, _extracted_findings = _extract_structured_findings(exec_result.summary)
        _extraction_source: str | None = None
        if _extracted_findings:
            _extraction_source = "STRUCTURED_FINDINGS"
        elif _is_review_task or _is_readonly_review_task:
            _extracted_findings = _extract_findings_from_model_answer(_clean_summary)
            if _extracted_findings:
                _extraction_source = "plain-text"
            elif final_files:
                _extracted_findings = _extract_findings_from_review_files(final_files)
                if _extracted_findings:
                    _extraction_source = "markdown-files"

        # Deterministic static analysis — only for Terraform/IaC review domain.
        # Gated so CI/CD, auth, docs, tests, and generic review prompts do not
        # receive IaC findings that are unrelated to the prompt intent.
        if _is_iac_review:
            try:
                from openshard.review.terraform_checker import scan_terraform
                _static_findings = scan_terraform(Path.cwd())
                if _static_findings:
                    # Merge: keep model findings first, then append static ones that
                    # are not exact message duplicates.
                    _existing_msgs = {f.message for f in _extracted_findings}
                    _new_static = [
                        f for f in _static_findings if f.message not in _existing_msgs
                    ]
                    _extracted_findings = list(_extracted_findings) + _new_static
                    if _new_static:
                        _extraction_source = _extraction_source or "static-review"
            except Exception:
                pass
            _timeline.append(make_timeline_event(
                "static_findings_detected",
                f"Found {len(_extracted_findings)} raw findings",
                kind="scan",
                count=len(_extracted_findings),
            ))

        _review_checks: list[dict] = []
        if _is_iac_review:
            try:
                from openshard.review.checks import run_review_checks
                _check_root = workspace if workspace is not None else Path.cwd()
                _review_checks = run_review_checks(_check_root)
            except Exception:
                pass
            if _review_checks:
                _timeline.append(make_timeline_event(
                    "review_checks_recorded",
                    "Recorded review checks",
                    kind="check",
                    count=len(_review_checks),
                ))

        # Domain-specific file discovery for non-IaC review domains.
        # Runs for any read-only/review task with a specific domain so that
        # test-coverage and similar prompts also get honest evidence output.
        _domain_files: list[str] = []
        if _has_domain_review and not _is_iac_review and _specific_domain:
            try:
                from openshard.review.domain_files import find_review_domain_files
                _scan_root = workspace if workspace is not None else Path.cwd()
                _domain_files = find_review_domain_files(_scan_root, _review_domain)
            except Exception:
                pass

        if _is_review_task or _is_readonly_review_task:
            _emit_review_debug(
                effective_executor=effective_executor,
                selected_workflow=effective_workflow,
                opencode_explicit=(effective_executor == "opencode"),
                task=task,
                exec_result_summary=exec_result.summary,
                exec_result_files_count=len(exec_result.files),
                exec_result_notes=exec_result.notes or [],
                usage_info=exec_result.usage,
                extraction_source=_extraction_source,
                findings_count=len(_extracted_findings),
            )
        _findings_extra: dict = {}
        if _is_review_task or _is_readonly_review_task:
            _findings_extra["is_review_task"] = True
        if _has_domain_review and _specific_domain:
            _findings_extra["review_domain"] = _review_domain
        if _review_checks:
            _findings_extra["review_checks"] = _review_checks
        if _domain_files:
            _findings_extra["domain_files"] = _domain_files
        if _extracted_findings:
            _findings_extra["findings"] = [
                {
                    "severity": f.severity,
                    "message": f.message,
                    **({"path": f.path} if f.path else {}),
                    **({"line": f.line} if f.line is not None else {}),
                    "source": "static-review" if f.path else "model",
                }
                for f in _extracted_findings
            ]
            click.echo("\nReview complete")
        elif _has_domain_review:
            click.echo("\nReview complete")
        else:
            click.echo("\nDone")
            click.echo(_clean_summary)
        _tier_validated = _validator_result is not None
        _tier_passed = (
            _validator_result.get("verdict", "fail") in ("pass", "warn")
            if _tier_validated else None
        )
        _mode_label = _profile_display_label(
            _profile_decision.profile if _profile_decision is not None else None,
            is_readonly=_readonly_task,
        )
        _receipt_risk = (
            _form_factor_decision.risk_level.capitalize()
            if _form_factor_decision and _form_factor_decision.risk_level
            else "Not recorded"
        )
        # Review tasks are always at least High risk regardless of routing category
        if (_is_review_task or _is_readonly_review_task) and _receipt_risk in ("Not recorded", "Low"):
            _receipt_risk = "High"
        _receipt_sandbox = "Off" if (_readonly_task or _is_review_task) else "Not recorded"
        _receipt_approval = "Not required" if _readonly_task else "Not recorded"
        # _receipt_index / _resolved_shard_id / _resolved_attempt_number were
        # already resolved before generation started (see above) — nothing
        # appends to runs.jsonl between there and here.
        if _is_review_task:
            _timeline.append(make_timeline_event(
                "review_memo_rendered", "Generated review memo",
                kind="review", count=len(_extracted_findings),
            ))
        _timeline.append(make_timeline_event("run_completed", "Run completed", kind="run"))
        render_post_run(
            stage_runs=stage_runs,
            routing_decision=routing_decision,
            verification_attempted=_tier_validated,
            verification_passed=_tier_passed,
            readonly_task=_readonly_task,
            validator_policy=_validator_policy,
            validator_result=_validator_result,
            final_files=exec_result.files,
            detail=detail,
            notes=exec_result.notes or [],
            repo_facts=_repo_facts,
            mode_label=_mode_label,
            task=_task_display,
            usage=usage,
            run_id=_run_id,
            run_index=_receipt_index,
            risk=_receipt_risk,
            sandbox=_receipt_sandbox,
            approval=_receipt_approval,
            is_native=(effective_executor == "native"),
            is_opencode=(effective_executor == "opencode"),
            exec_result_summary=_clean_summary,
            findings=_extracted_findings if _extracted_findings else None,
            is_review_task=_is_review_task or _is_readonly_review_task,
            generator_model=getattr(generator, "model", None),
            run_timeline=[e.to_dict() for e in _timeline],
            run_label=_pack_label,
            review_checks=_review_checks if _review_checks else None,
            review_domain=_review_domain,
            domain_files=_domain_files if _domain_files else None,
            routing_selected_model=_scored.selected_model if _scored and _scored.selected_model else None,
        )

        if dry_run:
            if _should_shrink(exec_result.files, no_shrink):
                _print_shrunk(exec_result.files, exec_result.summary)
            else:
                _print_dry_run(exec_result.files)
            _print_summary(start, generator, retry_triggered, final_files, usage=usage, detail=detail, model=_routed_model, stage_runs=stage_runs)
            _dr_extra: dict | None = None
            if _tier_dispatch_receipt is not None:
                _dr_extra = {"tier_dispatch_receipt": asdict(_tier_dispatch_receipt)}
            if _validator_result is not None:
                if _dr_extra is None:
                    _dr_extra = {}
                _dr_extra["validator_result"] = _validator_result
            if _validator_policy is not None:
                if _dr_extra is None:
                    _dr_extra = {}
                _dr_extra["validator_policy"] = {"run": _validator_policy.run, "reason": _validator_policy.reason}
            if _feedback_receipt is not None:
                if _dr_extra is None:
                    _dr_extra = {}
                _dr_extra.update(_feedback_receipt)
            if _failure_receipt is not None:
                if _dr_extra is None:
                    _dr_extra = {}
                _dr_extra.update(_failure_receipt)
            if _findings_extra:
                if _dr_extra is None:
                    _dr_extra = {}
                _dr_extra.update(_findings_extra)
            if effective_executor == "native" and hasattr(generator, "native_meta"):
                if _dr_extra is None:
                    _dr_extra = {}
                _dr_extra["executor"] = generator.native_meta.executor
                _dr_extra["tool_trace"] = generator.native_meta.tool_trace
            try:
                _log_run(start, _task_display, generator, retry_triggered, final_files,
                         verification_attempted=False, verification_passed=None,
                         workspace=None, usage=usage, model=_routed_model,
                         summary=_clean_summary, notes=exec_result.notes,
                         stage_runs=stage_runs, routing_decision=routing_decision,
                         _scored=_scored, repo_facts=_repo_facts,
                         matched_skills=_matched_skills,
                         profile_decision=_profile_decision,
                         verification_plan=_verification_plan,
                         form_factor_decision=_form_factor_decision,
                         extra_metadata=_dr_extra,
                         run_index=_receipt_index,
                         run_timeline=[e.to_dict() for e in _timeline],
                         effective_executor=effective_executor,
                         provider_enforcement_result=_provider_enforcement_result,
                         routable_pool=_routable_pool_cache,
                         model_policy_summary=_model_policy_summary,
                         shard_id=_resolved_shard_id,
                         attempt_number=_resolved_attempt_number)
            except Exception as exc:
                click.echo(f"  [log] warning: {exc}")
            result_obj.exit_code = 0
            result_obj.generator = generator
            result_obj.final_files = final_files
            result_obj.usage = usage
            result_obj.routed_model = _routed_model
            result_obj.stage_runs = stage_runs
            result_obj.routing_decision = routing_decision
            result_obj.scored = _scored
            result_obj.repo_facts = _repo_facts
            result_obj.matched_skills = _matched_skills
            result_obj.profile_decision = _profile_decision
            result_obj.verification_plan = _verification_plan
            result_obj.form_factor_decision = _form_factor_decision
            result_obj.result_summary = exec_result.summary
            result_obj.result_notes = exec_result.notes or []
            return result_obj

        if verify and not write:
            raise click.ClickException("--verify requires --write.")

        if write:
            if not opencode_mode:
                if effective_executor == "native" and hasattr(generator, "native_meta") and not _multi_candidate_done:
                    from openshard.native.sandbox import create_run_sandbox as _create_sandbox
                    workspace, _sb_meta = _create_sandbox(Path.cwd(), _run_id)
                    generator.native_meta.sandbox = _sb_meta
                else:
                    workspace = Path(tempfile.mkdtemp())
                if detail == "full":
                    click.echo(f"\n  [workspace] {workspace}")
                _file_paths = [f.path for f in exec_result.files if f.path]
                _fw_dec = gate.check_file_write(_file_paths)
                _rp_dec = gate.check_risky_paths(_file_paths)
                # Priority order: file-write before risky-paths. Route both
                # through the canonical deny > ask > allow resolver.
                _combined_dec = resolve_gate_decisions([_fw_dec, _rp_dec])
                if _combined_dec.required:
                    confirm_or_abort(_combined_dec.reason)
                if effective_executor == "native" and hasattr(generator, "build_patch_proposal"):
                    generator.build_patch_proposal(exec_result.files)
                if effective_executor == "native" and hasattr(generator, "build_change_budget_preview"):
                    generator.build_change_budget_preview()
                if effective_executor == "native" and hasattr(generator, "build_change_budget_soft_gate"):
                    _soft_gate = generator.build_change_budget_soft_gate()
                    _approval_request = None
                    if effective_executor == "native" and hasattr(generator, "build_budget_gate_approval_request"):
                        _approval_request = generator.build_budget_gate_approval_request()
                    if _soft_gate.requires_approval:
                        approval_prompt = (
                            _approval_request.prompt
                            if _approval_request is not None and _approval_request.prompt
                            else _soft_gate.reason
                        )
                        confirm_or_abort(approval_prompt)
                        if effective_executor == "native" and hasattr(generator, "build_approval_receipt"):
                            generator.build_approval_receipt(granted=True)
                if not _multi_candidate_done:
                    _write_files(exec_result.files, workspace)
                if effective_executor == "native":
                    if hasattr(generator, "record_loop_step"):
                        generator.record_loop_step("write")
                    if hasattr(generator, "record_osn_loop_step"):
                        generator.record_osn_loop_step("safe_write", "passed")
                    generator.review_diff()
                    if generator.native_meta.plan_ledger is not None:
                        from openshard.native.context import update_native_plan_ledger_status
                        generator.native_meta.plan_ledger = update_native_plan_ledger_status(
                            generator.native_meta.plan_ledger, "Write patch", "passed",
                            evidence="sandbox write complete",
                        )
                    _safe_checkpoint("sandbox_write", "passed", files=[f.path for f in exec_result.files])
            # OpenCode: workspace already created and populated before generate().

        # Native controlled verification loop — one retry, safe commands only, no --verify flag required
        if effective_executor == "native" and write and not dry_run and not verify and not _multi_candidate_done:
            generator.native_meta.edit_loop_summary = NativeEditLoopSummary(max_attempts=2)
            _edit_loop = generator.native_meta.edit_loop_summary
            _loop_meta = NativeVerificationLoop()
            generator.native_meta.verification_loop = _loop_meta
            if (
                _verification_plan.has_commands
                and all(cmd.safety == CommandSafety.safe for cmd in _verification_plan.commands)
            ):
                _loop_meta.attempted = True
                if hasattr(generator, "record_loop_step"):
                    generator.record_loop_step("verification")
                _loop_t0 = time.monotonic()
                _loop_code, _loop_output = _run_verification_plan(
                    _verification_plan, workspace, capture=True
                )
                _loop_meta.duration_seconds = round(time.monotonic() - _loop_t0, 2)
                _loop_meta.exit_code = _loop_code
                _loop_meta.output_chars = len(_loop_output)
                _loop_meta.truncated = len(_loop_output) > 1200
                _loop_meta.passed = _loop_code == 0
                # Record per-check proof — safe labels only, no raw paths
                _check_label = safe_check_label(_verification_plan.commands[0])
                _loop_meta.check_attempted = [_check_label]
                if _loop_code == 0:
                    _loop_meta.check_passed = [_check_label]
                else:
                    _loop_meta.check_failed = [_check_label]
                _vst_first = "passed" if _loop_meta.passed else "failed"
                if hasattr(generator, "record_osn_loop_step"):
                    generator.record_osn_loop_step("verify", _vst_first, verification_status=_vst_first)
                _safe_checkpoint("verify", _vst_first, verification_status=_vst_first)
                record_native_edit_loop_attempt(
                    _edit_loop,
                    attempt_index=1,
                    purpose="initial",
                    files_written=[f.path for f in exec_result.files],
                    verification_status=_vst_first,
                    exit_code=_loop_code,
                    output_chars=_loop_meta.output_chars,
                )
                if _loop_code != 0:
                    # Build structured failure metadata — no raw content stored
                    _fsummary = build_failure_summary(_loop_output, exit_code=_loop_code)
                    _retry_meta = RetryMetadata(
                        retry_attempted=True,
                        retry_reason="verification_failed",
                        failure_summary=_fsummary,
                    )
                    _loop_meta.retry_metadata = _retry_meta
                    if hasattr(generator, "record_osn_loop_step"):
                        generator.record_osn_loop_step(
                            "retry_diagnosis", "passed",
                            result_summary="verification failed — diagnosis recorded",
                            metadata={"failure_summary": _fsummary, "raw_content_stored": False},
                        )
                    # Raw failure context injected into retry prompt only — never stored
                    _failure_ctx = render_verification_failure_context(
                        _loop_output, exit_code=_loop_code
                    )
                    _retry_skills_ctx = "\n\n".join(
                        p for p in [_skills_ctx, _failure_ctx] if p
                    )
                    spinner.start("Retrying - native verification failed, regenerating")
                    try:
                        _retry_result = generator.generate(
                            task,
                            model=_routed_model,
                            repo_facts=_repo_facts,
                            skills_context=_retry_skills_ctx,
                        )
                    except RuntimeError as exc:
                        raise click.ClickException(str(exc))
                    except ProviderAuthError:
                        raise click.ClickException(
                            "Authentication failed. Check that your provider API key is valid."
                        )
                    except ProviderRateLimitError:
                        raise click.ClickException("Rate limit exceeded. Wait a moment then try again.")
                    except ProviderError as exc:
                        raise click.ClickException(f"API error: {exc}")
                    finally:
                        spinner.stop()
                    _loop_meta.retried = True
                    exec_result = _retry_result
                    final_files = _retry_result.files
                    _write_files(_retry_result.files, workspace)  # workspace is sandbox/worktree-backed
                    _retry_patch_files = [f.path for f in _retry_result.files]
                    _retry_meta.retry_patch_files = _retry_patch_files
                    if hasattr(generator, "record_osn_loop_step"):
                        generator.record_osn_loop_step(
                            "retry_patch", "passed",
                            result_summary=f"retry patch written: {len(_retry_patch_files)} file(s)",
                            metadata={
                                "files": _retry_patch_files,
                                "file_count": len(_retry_patch_files),
                                "raw_content_stored": False,
                            },
                        )
                    # Second verification run
                    _loop_t1 = time.monotonic()
                    _loop_code2, _loop_output2 = _run_verification_plan(
                        _verification_plan, workspace, capture=True
                    )
                    _loop_meta.duration_seconds = round(time.monotonic() - _loop_t1, 2)
                    _loop_meta.exit_code = _loop_code2
                    _loop_meta.output_chars = len(_loop_output2)
                    _loop_meta.truncated = len(_loop_output2) > 1200
                    _loop_meta.passed = _loop_code2 == 0
                    _retry_vst = "passed" if _loop_meta.passed else "failed"
                    _retry_meta.retry_verification_status = _retry_vst
                    record_native_edit_loop_attempt(
                        _edit_loop,
                        attempt_index=2,
                        purpose="repair",
                        files_written=[f.path for f in _retry_result.files],
                        verification_status=_retry_vst,
                        exit_code=_loop_code2,
                        output_chars=len(_loop_output2),
                    )
                    if hasattr(generator, "record_osn_loop_step"):
                        generator.record_osn_loop_step(
                            "retry_once", _retry_vst, verification_status=_retry_vst,
                        )
                    _safe_checkpoint("retry", _retry_vst, retry_used=True, verification_status=_retry_vst)
                    # Failure Memory v1: one structured event per retry, best-effort
                    try:
                        _fm_parsed = parse_failure_summary(_fsummary)
                        log_failure_memory_event(NativeFailureMemoryEvent(
                            run_id=_run_id,
                            task_summary=task[:120],
                            failure_type=_fm_parsed.get("failure_type", "test_failure"),
                            exit_code=int(_fm_parsed.get("exit_code", "1")),
                            output_chars=int(_fm_parsed.get("output_chars", "0")),
                            verification_status="failed",
                            retry_attempted=True,
                            retry_succeeded=(_retry_vst == "passed"),
                            retry_patch_files=list(_retry_patch_files),
                            related_file_paths=list(_retry_patch_files),
                            model=_routed_model or generator.model,
                            workflow=effective_workflow,
                        ))
                    except Exception:
                        pass
            if generator.native_meta.plan_ledger is not None:
                from openshard.native.context import update_native_plan_ledger_status
                _vplan_status = (
                    ("passed" if _loop_meta.passed else "failed")
                    if _loop_meta.attempted
                    else "skipped"
                )
                generator.native_meta.plan_ledger = update_native_plan_ledger_status(
                    generator.native_meta.plan_ledger, "Run verification", _vplan_status
                )
            if not _loop_meta.attempted:
                # Record skipped check labels for commands that were blocked or needed approval
                for _cmd in _verification_plan.commands:
                    if _cmd.safety != CommandSafety.safe:
                        _loop_meta.check_skipped.append(safe_check_label(_cmd))
                        _loop_meta.check_skipped_reasons.append(
                            f"{_cmd.safety.value}: {_cmd.reason}"[:80]
                        )
                record_native_edit_loop_attempt(
                    _edit_loop,
                    attempt_index=1,
                    purpose="initial",
                    files_written=[f.path for f in exec_result.files],
                    verification_status="skipped",
                    exit_code=None,
                    output_chars=0,
                )
                _safe_checkpoint("verify", "skipped", reason="verification not configured")

        if write and verify:
            click.echo("")
            code = _run_verification_plan(_verification_plan, workspace, gate=gate, detail=detail)
            # Escalation loop: try each model in chain until verification passes.
            # Direct mode uses ESCALATION_CHAIN (sonnet → opus).
            # OpenCode mode uses a single fixer-model retry (no chain).
            _escalation = ESCALATION_CHAIN if not opencode_mode else [generator.fixer_model]
            _last_attempt = exec_result
            _can_escalate = (
                _verification_plan.has_commands
                and _verification_plan.commands[0].safety != CommandSafety.blocked
            )
            if _can_escalate:
                for _esc_model in _escalation:
                    if code == 0:
                        break
                    retry_triggered = True
                    _, verify_output = _run_verification_plan(
                        _verification_plan, workspace, gate=None, capture=True
                    )
                    retry_prompt = _build_retry_prompt(task, _last_attempt, verify_output)
                    if detail == "full":
                        snippet = retry_prompt[:300] + ("..." if len(retry_prompt) > 300 else "")
                        click.echo(f"\n  [retry prompt] {snippet}")
                        if verify_output.strip():
                            click.echo(f"\n  [verify output] {verify_output.strip()[:300]}")
                    _esc_label = _esc_model.split("/")[-1]
                    spinner.start(f"Retrying - escalating to {_model_label(_esc_model)}")
                    try:
                        if opencode_mode:
                            _last_attempt = generator.generate(
                                retry_prompt, model=_esc_model, workspace=workspace
                            )
                        else:
                            _last_attempt = generator.generate(retry_prompt, model=_esc_model)
                    except RuntimeError as exc:
                        raise click.ClickException(str(exc))
                    except ProviderAuthError:
                        raise click.ClickException(
                            "Authentication failed. Check that your provider API key is valid."
                        )
                    except ProviderRateLimitError:
                        raise click.ClickException("Rate limit exceeded. Wait a moment then try again.")
                    except ProviderError as exc:
                        raise click.ClickException(f"API error: {exc}")
                    finally:
                        spinner.stop()
                    retry_usage = _last_attempt.usage
                    final_files = _last_attempt.files
                    if not opencode_mode:
                        _write_files(_last_attempt.files, workspace)
                        if effective_executor == "native":
                            generator.review_diff()
                    code = _run_verification_plan(
                        _verification_plan, workspace, gate=None,
                        label=f"[retry/{_esc_label}]", detail=detail,
                    )
            verification_passed = code == 0
            if code != 0:
                _print_summary(start, generator, retry_triggered, final_files,
                               usage=usage, retry_usage=retry_usage, detail=detail, model=_routed_model, stage_runs=stage_runs)
                try:
                    _vf_extra: dict | None = None
                    if _tier_dispatch_receipt is not None:
                        from dataclasses import asdict as _asdict
                        _vf_extra = {"tier_dispatch_receipt": _asdict(_tier_dispatch_receipt)}
                    if _validator_result is not None:
                        if _vf_extra is None:
                            _vf_extra = {}
                        _vf_extra["validator_result"] = _validator_result
                    if _validator_policy is not None:
                        if _vf_extra is None:
                            _vf_extra = {}
                        _vf_extra["validator_policy"] = {"run": _validator_policy.run, "reason": _validator_policy.reason}
                    if _feedback_receipt is not None:
                        if _vf_extra is None:
                            _vf_extra = {}
                        _vf_extra.update(_feedback_receipt)
                    if _failure_receipt is not None:
                        if _vf_extra is None:
                            _vf_extra = {}
                        _vf_extra.update(_failure_receipt)
                    if _findings_extra:
                        if _vf_extra is None:
                            _vf_extra = {}
                        _vf_extra.update(_findings_extra)
                    if effective_executor == "native" and hasattr(generator, "native_meta"):
                        if _vf_extra is None:
                            _vf_extra = {}
                        _vf_extra["executor"] = generator.native_meta.executor
                        _vf_extra["tool_trace"] = generator.native_meta.tool_trace
                    _log_run(start, _task_display, generator, retry_triggered, final_files,
                             verification_attempted=True, verification_passed=False,
                             workspace=workspace, usage=usage, retry_usage=retry_usage, model=_routed_model,
                             summary=_clean_summary, notes=exec_result.notes,
                             stage_runs=stage_runs, routing_decision=routing_decision,
                             _scored=_scored, repo_facts=_repo_facts,
                             matched_skills=_matched_skills,
                             profile_decision=_profile_decision,
                             verification_plan=_verification_plan,
                             form_factor_decision=_form_factor_decision,
                             extra_metadata=_vf_extra,
                             run_index=_receipt_index,
                             run_timeline=[e.to_dict() for e in _timeline],
                             effective_executor=effective_executor,
                             provider_enforcement_result=_provider_enforcement_result,
                             routable_pool=_routable_pool_cache,
                             model_policy_summary=_model_policy_summary,
                             shard_id=_resolved_shard_id,
                             attempt_number=_resolved_attempt_number)
                except Exception as exc:
                    click.echo(f"  [log] warning: {exc}")
                result_obj.exit_code = code
                result_obj.generator = generator
                result_obj.retry_triggered = retry_triggered
                result_obj.final_files = final_files
                result_obj.usage = usage
                result_obj.retry_usage = retry_usage
                result_obj.routed_model = _routed_model
                result_obj.stage_runs = stage_runs
                result_obj.routing_decision = routing_decision
                result_obj.scored = _scored
                result_obj.repo_facts = _repo_facts
                result_obj.matched_skills = _matched_skills
                result_obj.profile_decision = _profile_decision
                result_obj.verification_plan = _verification_plan
                result_obj.form_factor_decision = _form_factor_decision
                result_obj.verification_attempted = True
                result_obj.verification_passed = False
                result_obj.workspace = workspace
                result_obj.result_summary = exec_result.summary
                result_obj.result_notes = exec_result.notes or []
                return result_obj

        _print_summary(start, generator, retry_triggered, final_files,
                       usage=usage, retry_usage=retry_usage, detail=detail, model=_routed_model, stage_runs=stage_runs)
        if (
            effective_executor == "native"
            and hasattr(generator, "build_verification_command_summary")
            and generator.native_meta.verification_loop is not None
            and generator.native_meta.verification_loop.attempted
        ):
            generator.build_verification_command_summary(_verification_plan)
        if effective_executor == "native" and hasattr(generator, "build_final_report"):
            generator.build_final_report()
        _native_meta = generator.native_meta if effective_executor == "native" else None
        if _native_meta is not None:
            from openshard.native.context import build_native_failure_memory
            _native_meta.failure_memory = build_native_failure_memory(
                context_quality_score=_native_meta.context_quality_score,
                clarification_request=_native_meta.clarification_request,
                verification_loop=_native_meta.verification_loop,
                command_policy_preview=_native_meta.command_policy_preview,
                approval_request=_native_meta.approval_request,
                approval_receipt=_native_meta.approval_receipt,
                change_budget_preview=_native_meta.change_budget_preview,
                verification_plan=_native_meta.verification_plan,
                context_usage_summary=_native_meta.context_usage_summary,
            )
            from openshard.native.context import build_native_run_trust_score
            _native_meta.run_trust_score = build_native_run_trust_score(
                context_quality_score=_native_meta.context_quality_score,
                validation_contract=_native_meta.validation_contract,
                context_provenance=_native_meta.context_provenance,
                verification_loop=_native_meta.verification_loop,
                command_policy_preview=_native_meta.command_policy_preview,
                change_budget_preview=_native_meta.change_budget_preview,
                change_budget_soft_gate=_native_meta.change_budget_soft_gate,
                approval_request=_native_meta.approval_request,
                approval_receipt=_native_meta.approval_receipt,
                failure_memory=_native_meta.failure_memory,
                context_usage_summary=_native_meta.context_usage_summary,
                final_report=_native_meta.final_report,
            )
            from openshard.native.context import build_native_verification_contract_result
            _native_meta.verification_contract_result = build_native_verification_contract_result(
                validation_contract=_native_meta.validation_contract,
                verification_loop=_native_meta.verification_loop,
            )
            from openshard.native.context import build_native_model_selection_decision
            _native_meta.model_selection_decision = build_native_model_selection_decision(
                verification_plan=_native_meta.verification_plan,
                validation_contract=_native_meta.validation_contract,
                context_quality_score=_native_meta.context_quality_score,
                context_provenance=_native_meta.context_provenance,
                run_trust_score=_native_meta.run_trust_score,
                change_budget=_native_meta.change_budget,
                failure_memory=_native_meta.failure_memory,
            )
            from openshard.native.context import build_native_model_candidate_scoring
            _native_meta.model_candidate_scoring = build_native_model_candidate_scoring(
                model_selection_decision=_native_meta.model_selection_decision,
                verification_plan=_native_meta.verification_plan,
                validation_contract=_native_meta.validation_contract,
                context_quality_score=_native_meta.context_quality_score,
                context_provenance=_native_meta.context_provenance,
                run_trust_score=_native_meta.run_trust_score,
                context_usage_summary=_native_meta.context_usage_summary,
                failure_memory=_native_meta.failure_memory,
                model_policy=_native_meta.model_policy,
            )
            import copy
            _before_sync = copy.deepcopy(_native_meta.model_selection_decision)
            from openshard.native.context import (
                sync_native_model_selection_decision_with_candidate_scoring,
            )
            _native_meta.model_selection_decision = sync_native_model_selection_decision_with_candidate_scoring(
                model_selection_decision=_native_meta.model_selection_decision,
                model_candidate_scoring=_native_meta.model_candidate_scoring,
                model_policy=_native_meta.model_policy,
            )
            from openshard.native.context import build_native_model_policy_receipt
            _native_meta.model_policy_receipt = build_native_model_policy_receipt(
                model_policy=_native_meta.model_policy,
                model_selection_decision_before=_before_sync,
                model_selection_decision_after=_native_meta.model_selection_decision,
                model_candidate_scoring=_native_meta.model_candidate_scoring,
            )
            from openshard.native.context import build_native_routing_preview
            _native_meta.routing_preview = build_native_routing_preview(
                model_candidate_scoring=_native_meta.model_candidate_scoring,
                model_selection_decision=_native_meta.model_selection_decision,
                model_policy_receipt=_native_meta.model_policy_receipt,
                run_trust_score=_native_meta.run_trust_score,
            )
            from openshard.native.context import build_native_routing_receipt
            _native_meta.routing_receipt = build_native_routing_receipt(
                routing_preview=_native_meta.routing_preview,
                model_policy_receipt=_native_meta.model_policy_receipt,
                run_trust_score=_native_meta.run_trust_score,
            )
            if self._tier_dispatch_enabled():
                from openshard.native.context import build_native_tier_dispatch_receipt
                _native_meta.tier_dispatch_receipt = build_native_tier_dispatch_receipt(
                    routing_receipt=_native_meta.routing_receipt,
                    model_candidate_scoring=_native_meta.model_candidate_scoring,
                    routing_category=routing_decision.category if routing_decision else None,
                    experimental_tier_dispatch=True,
                    applied=False,
                    not_applied_reason="routing decisions resolved post-execution",
                    executor_model_actual=_effective_model,
                    validator_dispatch_status="reserved",
                )
                _tier_dispatch_receipt = _native_meta.tier_dispatch_receipt
        if _native_meta is not None:
            from openshard.native.context import build_native_failure_memory_routing_advisory
            _native_meta.failure_memory_routing_advisory = (
                build_native_failure_memory_routing_advisory()
            )
        if _native_meta is not None and detail != "default":
            from openshard.cli.run_output import _print_native_demo_block, _print_native_summary
            _print_native_demo_block(_native_meta, detail=detail)
            _print_native_summary(_native_meta, detail=detail)
        if _native_meta is None and _tier_dispatch_receipt is not None and detail != "default":
            from openshard.cli.run_output import _print_tier_dispatch_block
            _is_direct_ask = _readonly_task and not _use_stages
            _print_tier_dispatch_block(_tier_dispatch_receipt, detail, validator_result=_validator_result, validator_policy=_validator_policy, is_ask=_is_direct_ask)
        if _native_meta is not None and _native_meta.plan_ledger is not None:
            from openshard.native.context import update_native_plan_ledger_status
            _native_meta.plan_ledger = update_native_plan_ledger_status(
                _native_meta.plan_ledger, "Record receipt", "passed"
            )
        if effective_executor == "native":
            _safe_checkpoint("receipt", "passed")
        _extra_metadata: dict | None = None
        if _native_meta is not None:
            _extra_metadata = {
                "workflow": _native_meta.workflow,
                "executor": _native_meta.executor,
                "execution_depth": _native_meta.execution_depth,
                "selected_skills": _native_meta.selected_skills,
                "context_budget": asdict(_native_meta.context_budget) if _native_meta.context_budget is not None else None,
                "context_state": asdict(_native_meta.context_state) if _native_meta.context_state is not None else None,
                "context_warnings": _native_meta.context_warnings,
                "tool_trace": _native_meta.tool_trace,
                "tool_search_events": [
                    asdict(e) if not isinstance(e, dict) else e
                    for e in _native_meta.tool_search_events
                ],
                "repo_context_summary": asdict(_native_meta.repo_context_summary) if _native_meta.repo_context_summary is not None else None,
                "observation": asdict(_native_meta.observation) if _native_meta.observation is not None else None,
                "evidence": asdict(_native_meta.evidence) if _native_meta.evidence is not None else None,
                "plan": asdict(_native_meta.plan) if _native_meta.plan is not None else None,
                "diff_review": asdict(_native_meta.diff_review) if _native_meta.diff_review is not None else None,
                "write_path": _native_meta.write_path,
                "verification_loop": (
                    asdict(_native_meta.verification_loop)
                    if _native_meta.verification_loop is not None
                    else None
                ),
                "verification_command_summary": (
                    asdict(_native_meta.verification_command_summary)
                    if _native_meta is not None
                    and _native_meta.verification_command_summary is not None
                    else None
                ),
                "final_report": (
                    asdict(_native_meta.final_report)
                    if _native_meta.final_report is not None
                    else None
                ),
                "native_loop_steps": list(_native_meta.native_loop_steps),
                "native_loop_trace": (
                    [
                        {
                            "phase": event.phase,
                            "status": event.status,
                            "summary": event.summary,
                            "metadata": event.metadata,
                        }
                        for event in getattr(_native_meta.native_loop_trace, "events", [])
                    ]
                    if _native_meta is not None
                    else []
                ),
                "native_backend": getattr(_native_meta, "native_backend", "builtin"),
                "native_backend_available": getattr(_native_meta, "native_backend_available", True),
                "native_backend_notes": list(getattr(_native_meta, "native_backend_notes", [])),
                "native_backend_proof": getattr(_native_meta, "native_backend_proof", None),
                "read_search_findings": list(getattr(_native_meta, "read_search_findings", [])),
                "context_packet": (
                    asdict(_native_meta.context_packet)
                    if _native_meta is not None and _native_meta.context_packet is not None
                    else None
                ),
                "patch_proposal": (
                    asdict(_native_meta.patch_proposal)
                    if _native_meta.patch_proposal is not None
                    else None
                ),
                "command_policy_preview": (
                    asdict(_native_meta.command_policy_preview)
                    if _native_meta is not None
                    and _native_meta.command_policy_preview is not None
                    else None
                ),
                "file_context": (
                    asdict(_native_meta.file_context)
                    if _native_meta is not None and _native_meta.file_context is not None
                    else None
                ),
                "context_quality_score": (
                    asdict(_native_meta.context_quality_score)
                    if _native_meta is not None and _native_meta.context_quality_score is not None
                    else None
                ),
                "context_quality_advisory": (
                    asdict(_native_meta.context_quality_advisory)
                    if _native_meta is not None
                    and _native_meta.context_quality_advisory is not None
                    else None
                ),
                "change_budget": (
                    asdict(_native_meta.change_budget)
                    if _native_meta is not None and _native_meta.change_budget is not None
                    else None
                ),
                "change_budget_preview": (
                    asdict(_native_meta.change_budget_preview)
                    if _native_meta is not None and _native_meta.change_budget_preview is not None
                    else None
                ),
                "change_budget_soft_gate": (
                    asdict(_native_meta.change_budget_soft_gate)
                    if _native_meta is not None and _native_meta.change_budget_soft_gate is not None
                    else None
                ),
                "approval_request": (
                    asdict(_native_meta.approval_request)
                    if _native_meta is not None and _native_meta.approval_request is not None
                    else None
                ),
                "approval_receipt": (
                    asdict(_native_meta.approval_receipt)
                    if _native_meta is not None and _native_meta.approval_receipt is not None
                    else None
                ),
                "verification_plan": (
                    asdict(_native_meta.verification_plan)
                    if _native_meta is not None and _native_meta.verification_plan is not None
                    else None
                ),
                "clarification_request": (
                    asdict(_native_meta.clarification_request)
                    if _native_meta is not None and _native_meta.clarification_request is not None
                    else None
                ),
                "context_usage_summary": (
                    asdict(_native_meta.context_usage_summary)
                    if _native_meta is not None and _native_meta.context_usage_summary is not None
                    else None
                ),
                "failure_memory": (
                    asdict(_native_meta.failure_memory)
                    if _native_meta is not None and _native_meta.failure_memory is not None
                    else None
                ),
                "osn_loop": (
                    asdict(_native_meta.osn_loop)
                    if _native_meta is not None and _native_meta.osn_loop is not None
                    else None
                ),
                "osn_loop_summary": (
                    asdict(_native_meta.osn_loop_summary)
                    if _native_meta is not None and _native_meta.osn_loop_summary is not None
                    else None
                ),
                "deepagents_adapter": (
                    asdict(_native_meta.deepagents_adapter)
                    if _native_meta is not None and _native_meta.deepagents_adapter is not None
                    else None
                ),
                "validation_contract": (
                    asdict(_native_meta.validation_contract)
                    if _native_meta is not None and _native_meta.validation_contract is not None
                    else None
                ),
                "verification_contract_result": (
                    asdict(_native_meta.verification_contract_result)
                    if _native_meta is not None and _native_meta.verification_contract_result is not None
                    else None
                ),
                "context_provenance": (
                    asdict(_native_meta.context_provenance)
                    if _native_meta is not None and _native_meta.context_provenance is not None
                    else None
                ),
                "run_trust_score": (
                    asdict(_native_meta.run_trust_score)
                    if _native_meta is not None and _native_meta.run_trust_score is not None
                    else None
                ),
                "model_selection_decision": (
                    asdict(_native_meta.model_selection_decision)
                    if _native_meta is not None and _native_meta.model_selection_decision is not None
                    else None
                ),
                "model_candidate_scoring": (
                    asdict(_native_meta.model_candidate_scoring)
                    if _native_meta is not None and _native_meta.model_candidate_scoring is not None
                    else None
                ),
                "model_policy": (
                    asdict(_native_meta.model_policy)
                    if _native_meta is not None and _native_meta.model_policy is not None
                    else None
                ),
                "model_policy_receipt": (
                    asdict(_native_meta.model_policy_receipt)
                    if _native_meta is not None and _native_meta.model_policy_receipt is not None
                    else None
                ),
                "routing_preview": (
                    asdict(_native_meta.routing_preview)
                    if _native_meta is not None and _native_meta.routing_preview is not None
                    else None
                ),
                "routing_receipt": (
                    asdict(_native_meta.routing_receipt)
                    if _native_meta is not None and _native_meta.routing_receipt is not None
                    else None
                ),
                "tier_dispatch_receipt": (
                    asdict(_native_meta.tier_dispatch_receipt)
                    if _native_meta is not None and _native_meta.tier_dispatch_receipt is not None
                    else None
                ),
                "sandbox": (
                    asdict(_native_meta.sandbox)
                    if _native_meta is not None and _native_meta.sandbox is not None
                    else None
                ),
                "failure_memory_routing_advisory": (
                    asdict(_native_meta.failure_memory_routing_advisory)
                    if _native_meta is not None
                    and _native_meta.failure_memory_routing_advisory is not None
                    else None
                ),
                "plan_ledger": (
                    asdict(_native_meta.plan_ledger)
                    if _native_meta.plan_ledger is not None
                    else None
                ),
                "edit_loop_summary": (
                    asdict(_native_meta.edit_loop_summary)
                    if _native_meta.edit_loop_summary is not None
                    else None
                ),
                "candidate_summary": (
                    asdict(_native_meta.candidate_summary)
                    if _native_meta.candidate_summary is not None
                    else None
                ),
            }
        _promote_sandbox_git_metadata(_extra_metadata)
        # For non-native staged/direct runs, save tier_dispatch_receipt at top level
        if _native_meta is None and _tier_dispatch_receipt is not None:
            from dataclasses import asdict as _asdict
            _extra_metadata = {"tier_dispatch_receipt": _asdict(_tier_dispatch_receipt)}
        if _native_meta is None and _validator_result is not None:
            if _extra_metadata is None:
                _extra_metadata = {}
            _extra_metadata["validator_result"] = _validator_result
        if _native_meta is None and _validator_policy is not None:
            if _extra_metadata is None:
                _extra_metadata = {}
            _extra_metadata["validator_policy"] = {"run": _validator_policy.run, "reason": _validator_policy.reason}
        if _feedback_receipt is not None:
            if _extra_metadata is None:
                _extra_metadata = {}
            _extra_metadata.update(_feedback_receipt)
        if _failure_receipt is not None:
            if _extra_metadata is None:
                _extra_metadata = {}
            _extra_metadata.update(_failure_receipt)
        # Capture adapter metadata for explicit OpenCode runs
        if opencode_mode and exec_result is not None and exec_result.adapter_meta:
            if _extra_metadata is None:
                _extra_metadata = {}
            _extra_metadata.update(exec_result.adapter_meta)
        # Finalise OSN loop summary before serialising to run history
        if effective_executor == "native" and hasattr(generator, "complete_osn_loop"):
            _vloop = getattr(generator.native_meta, "verification_loop", None)
            _approval_rcpt = getattr(generator.native_meta, "approval_receipt", None)
            generator.record_osn_loop_step("final_receipt", "passed")
            generator.complete_osn_loop(
                stopped_reason="completed",
                verification_status=(
                    "passed" if (_vloop and _vloop.passed)
                    else ("failed" if (_vloop and _vloop.attempted) else "")
                ),
                retry_used=bool(_vloop and _vloop.retried),
                approval_granted=bool(_approval_rcpt and _approval_rcpt.granted),
            )
            # Refresh snapshot: _extra_metadata was built before complete_osn_loop ran
            if _extra_metadata is not None and _native_meta is not None:
                _extra_metadata["osn_loop_summary"] = (
                    asdict(_native_meta.osn_loop_summary)
                    if _native_meta.osn_loop_summary is not None
                    else None
                )
                # Persist osn_observation (was previously in-memory only)
                _extra_metadata["osn_observation"] = (
                    asdict(_native_meta.osn_observation)
                    if _native_meta.osn_observation is not None
                    else None
                )
                # Build and persist OSN verification contract
                if _native_meta.osn_loop_summary is not None:
                    _native_meta.osn_verification_contract = (
                        _build_osn_verification_contract_with_loop(
                            _native_meta, is_write_task=not _readonly_task
                        )
                    )
                    _extra_metadata["osn_verification_contract"] = asdict(
                        _native_meta.osn_verification_contract
                    )
                # Build and persist OSN retry diagnosis
                if _native_meta.osn_loop_summary is not None:
                    from openshard.native.retry_diagnosis import (
                        build_osn_retry_diagnosis as _build_rd,
                    )
                    _native_meta.osn_retry_diagnosis = _build_rd(
                        osn_loop_summary=_native_meta.osn_loop_summary,
                        osn_verification_contract=_native_meta.osn_verification_contract,
                        approval_required=bool(
                            _native_meta.osn_loop_summary.approval_required
                        ),
                        approval_granted=bool(
                            _native_meta.osn_loop_summary.approval_granted
                        ),
                    )
                    _extra_metadata["osn_retry_diagnosis"] = asdict(
                        _native_meta.osn_retry_diagnosis
                    )
                # Build and persist OSN progress memory from finalized signals
                from openshard.native.progress_memory import (
                    build_osn_progress_memory as _build_pm,
                )
                _native_meta.osn_progress_memory = _build_pm(
                    osn_observation=_native_meta.osn_observation,
                    osn_loop_summary=_native_meta.osn_loop_summary,
                    osn_verification_contract=_native_meta.osn_verification_contract,
                    osn_retry_diagnosis=_native_meta.osn_retry_diagnosis,
                )
                _extra_metadata["osn_progress_memory"] = asdict(
                    _native_meta.osn_progress_memory
                )
        if _findings_extra:
            if _extra_metadata is None:
                _extra_metadata = {}
            _extra_metadata.update(_findings_extra)
        # Secret scan recording — scans files already read as context, before Shard log.
        # NOTE: runs post-run (after model use). Records findings for receipt visibility.
        # Does not prevent secrets from reaching the model in v1.
        try:
            from dataclasses import asdict as _scan_asdict

            from openshard.security.secret_scan import (
                SecretScanResult,
            )
            from openshard.security.secret_scan import (
                scan_paths_for_secrets as _scan_secrets,
            )
            _scan_file_paths: list[Path] = []
            if _native_meta is not None and _native_meta.file_context is not None:
                _scan_file_paths = [
                    Path(p) for p in (_native_meta.file_context.paths or []) if p
                ]
            # Post-run path scan (existing behaviour).
            _post_findings: list = []
            _post_scanned = 0
            if _scan_file_paths:
                _scan_result = _scan_secrets(_scan_file_paths, root=Path.cwd())
                _post_findings = list(_scan_result.findings)
                _post_scanned = _scan_result.scanned_files_count
            # Merge pre-context scrub findings (secrets caught before model injection).
            _pre_scan = getattr(_native_meta, "pre_context_secret_scan", None)
            _pre_findings = list(_pre_scan.findings) if _pre_scan is not None else []
            # Merge pre-send guard findings (secrets redacted at the provider
            # boundary — covers task/repo_facts and the direct execution path).
            _presend_scan = getattr(exec_result, "presend_secret_scan", None)
            _presend_findings = (
                list(_presend_scan.findings) if _presend_scan is not None else []
            )
            # Union by fingerprint — a secret found in more than one place collapses to one.
            _merged_findings: list = []
            _seen_fp: set[str] = set()
            for _f in _post_findings + _pre_findings + _presend_findings:
                _fp = getattr(_f, "fingerprint", None)
                if _fp and _fp in _seen_fp:
                    continue
                if _fp:
                    _seen_fp.add(_fp)
                _merged_findings.append(_f)
            if _merged_findings:
                _count = len(_merged_findings)
                _merged = SecretScanResult(
                    scanned_files_count=_post_scanned,
                    findings=_merged_findings,
                    blocked=False,
                    summary=(
                        f"{_count} potential secret{'s' if _count != 1 else ''} "
                        f"detected (pre-send guard + pre-context scrub + post-run scan)"
                    ),
                )
                if _extra_metadata is None:
                    _extra_metadata = {}
                _extra_metadata["secret_scan_result"] = _scan_asdict(_merged)
        except Exception as _scan_exc:
            # Never break the run. Record class only — no message that could contain file content.
            if _extra_metadata is None:
                _extra_metadata = {}
            _extra_metadata["secret_scan_error_class"] = type(_scan_exc).__name__
        # Record runtime policy decisions — recording only, no behaviour change.
        try:
            from openshard.policy.runtime import (
                _dedup_decisions as _pd_dedup,
            )
            from openshard.policy.runtime import (
                build_runtime_policy_decisions as _build_pdecisions,
            )
            _runtime_pds = _build_pdecisions(
                approval_request=(_extra_metadata or {}).get("approval_request"),
                approval_receipt=(_extra_metadata or {}).get("approval_receipt"),
                secret_scan_result=(_extra_metadata or {}).get("secret_scan_result"),
                validator_policy=(_extra_metadata or {}).get("validator_policy"),
                readonly=_readonly_task,
            )
            if _runtime_pds:
                if _extra_metadata is None:
                    _extra_metadata = {}
                _existing_pds = _extra_metadata.get("policy_decisions") or []
                _appended = (
                    list(_existing_pds) + _pd_dedup(list(_existing_pds), _runtime_pds)
                    if isinstance(_existing_pds, list)
                    else _runtime_pds
                )
                _extra_metadata["policy_decisions"] = _appended
        except Exception:
            pass  # Never break the run
        # Promote context utilisation and execution spans into top-level metadata.
        _populate_context_usage_metadata(_extra_metadata)
        _populate_execution_span_metadata(_extra_metadata)
        try:
            _log_run(start, _task_display, generator, retry_triggered, final_files,
                     verification_attempted=(write and verify),
                     verification_passed=verification_passed,
                     workspace=workspace, usage=usage, retry_usage=retry_usage, model=_routed_model,
                     summary=_clean_summary, notes=exec_result.notes,
                     stage_runs=stage_runs, routing_decision=routing_decision,
                     _scored=_scored, repo_facts=_repo_facts,
                     matched_skills=_matched_skills,
                     profile_decision=_profile_decision,
                     verification_plan=_verification_plan,
                     form_factor_decision=_form_factor_decision,
                     extra_metadata=_extra_metadata,
                     run_id=_run_id,
                     run_index=_receipt_index,
                     run_timeline=[e.to_dict() for e in _timeline],
                     executor_advisory_result=_executor_advisory_result,
                     executor_source=_executor_source,
                     fra_result=_fra_result,
                     effective_executor=effective_executor,
                     provider_enforcement_result=_provider_enforcement_result,
                     routable_pool=_routable_pool_cache,
                     model_policy_summary=_model_policy_summary,
                     shard_id=_resolved_shard_id,
                     attempt_number=_resolved_attempt_number)
        except Exception as exc:
            click.echo(f"  [log] warning: {exc}")

        if _native_meta is not None and detail == "default":
            from openshard.cost.baseline import format_baseline_line
            _pt = getattr(usage, "prompt_tokens", 0) or 0
            _ct = getattr(usage, "completion_tokens", 0) or 0
            _actual = getattr(usage, "estimated_cost", None)
            _bl = format_baseline_line(_pt, _ct, actual_cost=_actual)
            if _bl is not None:
                click.echo(_bl)
            from openshard.cli.run_output import _print_native_receipt
            _print_native_receipt(_native_meta)

        result_obj.exit_code = 0
        result_obj.generator = generator
        result_obj.retry_triggered = retry_triggered
        result_obj.final_files = final_files
        result_obj.usage = usage
        result_obj.retry_usage = retry_usage
        result_obj.routed_model = _routed_model
        result_obj.stage_runs = stage_runs
        result_obj.routing_decision = routing_decision
        result_obj.scored = _scored
        result_obj.repo_facts = _repo_facts
        result_obj.matched_skills = _matched_skills
        result_obj.profile_decision = _profile_decision
        result_obj.verification_plan = _verification_plan
        result_obj.form_factor_decision = _form_factor_decision
        result_obj.verification_attempted = (write and verify)
        result_obj.verification_passed = verification_passed
        result_obj.workspace = workspace
        result_obj.result_summary = exec_result.summary
        result_obj.result_notes = exec_result.notes or []
        result_obj.native_meta = _native_meta
        return result_obj
