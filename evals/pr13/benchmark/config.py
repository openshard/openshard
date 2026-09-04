"""Scenario configuration: ``metadata.json`` -> validated dataclasses.

A scenario directory holds everything the benchmark needs to reproduce one
experiment: the exact repository source and base commit, the burn-in and
evaluation prompts, the deterministic verification steps, the explicit
definition of the known failed approach, and what evidence the burn-in is
expected to leave behind. Nothing here runs anything; loading is pure
validation and every problem is a ``BenchmarkError("scenario_invalid")``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.pr13.benchmark.errors import BenchmarkError

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_KINDS = ("seed", "git")
CRITERION_KINDS = ("verification_step_failed", "verification_failed", "paths_changed", "paths_unchanged")
PLACEHOLDERS = ("{python}", "{workspace}", "{scenario}")


def _invalid(message: str, **details: Any) -> BenchmarkError:
    return BenchmarkError("scenario_invalid", message, details=details)


@dataclass(frozen=True)
class RepositorySpec:
    """Where the target repository comes from and the exact commit every arm starts at.

    ``seed``: a directory inside the scenario that the benchmark turns into a
    git repository with a fixed author/committer/date, so the resulting
    commit id is reproducible and must equal ``base_commit``. ``git``: a URL
    (or local path) that is cloned; ``base_commit`` must exist in it.
    """

    kind: str
    base_commit: str
    seed_path: Path | None = None
    url: str | None = None
    default_branch: str = "main"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "base_commit": self.base_commit,
            "seed_path": str(self.seed_path) if self.seed_path else None,
            "url": self.url,
            "default_branch": self.default_branch,
        }


@dataclass(frozen=True)
class VerificationStep:
    name: str
    argv: tuple[str, ...]
    cwd: str = "{workspace}"
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 300.0


@dataclass(frozen=True)
class Criterion:
    kind: str
    params: dict[str, Any]


@dataclass(frozen=True)
class KnownFailedApproach:
    id: str
    description: str
    criteria: tuple[Criterion, ...]
    mode: str = "all"  # "all" | "any"


@dataclass(frozen=True)
class ExpectedEvidence:
    description: str
    min_shards: int = 1
    executor: str | None = "claude_code_hooks"
    task_contains_any: tuple[str, ...] = ()
    files_include_any: tuple[str, ...] = ()
    files_exclude_all: tuple[str, ...] = ()


@dataclass(frozen=True)
class WrapStage:
    """One ``openshard wrap claude`` invocation in a ``claude_wrap_chain`` burn-in.

    Each stage is a real, separate Claude Code CLI session, wrapped by
    OpenShard's own ``wrap`` adapter so it can be explicitly linked to the
    previous stage's persisted ``shard_id`` (``openshard wrap claude
    --shard <id>``) -- the only real, non-fabricated way to build a
    genuine multi-attempt Shard from an external coding agent.
    """

    prompt_path: Path
    task: str

    def prompt_text(self) -> str:
        return self.prompt_path.read_text(encoding="utf-8")


# Burn-in capture mechanisms a scenario may select. "claude_hooks" (default)
# is Scenario 1's original single-session, hook-captured burn-in -- every
# field below this point is additive and unused by it, so it is entirely
# unaffected by this. "claude_wrap_chain" links two or more real, separate
# `openshard wrap claude` sessions into one persistent Shard (multi-attempt
# chronology). "opencode_hooks" runs the burn-in through OpenCode instead of
# Claude Code, via OpenCode's own production plugin capture.
BURN_IN_CAPTURE_KINDS = ("claude_hooks", "claude_wrap_chain", "opencode_hooks")


@dataclass(frozen=True)
class StagePolicy:
    prompt_path: Path
    max_turns: int | None
    timeout_seconds: float
    require_verification_failed: bool = False
    require_known_failed_approach: bool = False
    require_expected_evidence: bool = False
    # Burn-in only (ignored for the evaluation stage): see BURN_IN_CAPTURE_KINDS.
    capture: str = "claude_hooks"
    agent: str | None = None  # set only for capture == "opencode_hooks" (currently only "opencode")
    # The burn-in agent's OWN model id, in that agent's own format (OpenCode:
    # "provider/model"). Required for opencode_hooks: the benchmark's --model is
    # a Claude Code model/alias and must never be passed to another agent's CLI
    # (a live Scenario 7 run did exactly that and OpenCode rejected it).
    agent_model: str | None = None
    wrap_stages: tuple[WrapStage, ...] = ()  # set only for capture == "claude_wrap_chain"

    def prompt_text(self) -> str:
        return self.prompt_path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class ScenarioConfig:
    id: str
    title: str
    scenario_dir: Path
    repository: RepositorySpec
    burn_in: StagePolicy
    evaluation: StagePolicy
    verification: tuple[VerificationStep, ...]
    known_failed_approach: KnownFailedApproach
    expected_evidence: ExpectedEvidence
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "scenario_dir": str(self.scenario_dir),
            "repository": self.repository.to_dict(),
            "burn_in": {
                "prompt": self.burn_in.prompt_path.name,
                "max_turns": self.burn_in.max_turns,
                "timeout_seconds": self.burn_in.timeout_seconds,
                "require_verification_failed": self.burn_in.require_verification_failed,
                "require_known_failed_approach": self.burn_in.require_known_failed_approach,
                "require_expected_evidence": self.burn_in.require_expected_evidence,
                "capture": self.burn_in.capture,
                "agent": self.burn_in.agent,
                "agent_model": self.burn_in.agent_model,
                "wrap_stages": [
                    {"prompt": s.prompt_path.name, "task": s.task} for s in self.burn_in.wrap_stages
                ],
            },
            "evaluation": {
                "prompt": self.evaluation.prompt_path.name,
                "max_turns": self.evaluation.max_turns,
                "timeout_seconds": self.evaluation.timeout_seconds,
            },
            "verification": [
                {"name": s.name, "argv": list(s.argv), "cwd": s.cwd, "env": dict(s.env),
                 "timeout_seconds": s.timeout_seconds}
                for s in self.verification
            ],
            "known_failed_approach": {
                "id": self.known_failed_approach.id,
                "description": self.known_failed_approach.description,
                "mode": self.known_failed_approach.mode,
                "criteria": [{"kind": c.kind, **c.params} for c in self.known_failed_approach.criteria],
            },
            "expected_evidence": {
                "description": self.expected_evidence.description,
                "min_shards": self.expected_evidence.min_shards,
                "executor": self.expected_evidence.executor,
                "task_contains_any": list(self.expected_evidence.task_contains_any),
                "files_include_any": list(self.expected_evidence.files_include_any),
                "files_exclude_all": list(self.expected_evidence.files_exclude_all),
            },
            "notes": list(self.notes),
        }


def substitute(value: str, mapping: dict[str, str]) -> str:
    """Replace the documented placeholders (``{python}``, ``{workspace}``, ``{scenario}``)."""
    out = value
    for key, replacement in mapping.items():
        out = out.replace("{" + key + "}", replacement)
    return out


def _require(data: dict[str, Any], key: str, kind: type, where: str) -> Any:
    if key not in data:
        raise _invalid(f"{where}: missing required key {key!r}")
    value = data[key]
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise _invalid(f"{where}: {key!r} must be {kind.__name__}, got {type(value).__name__}")
    return value


def _str_list(data: dict[str, Any], key: str, where: str, default: list[str] | None = None) -> tuple[str, ...]:
    raw = data.get(key, default if default is not None else [])
    if not isinstance(raw, list) or not all(isinstance(x, str) and x for x in raw):
        raise _invalid(f"{where}: {key!r} must be a list of non-empty strings")
    return tuple(raw)


def _load_repository(raw: dict[str, Any], scenario_dir: Path) -> RepositorySpec:
    where = "repository"
    kind = _require(raw, "kind", str, where)
    if kind not in REPOSITORY_KINDS:
        raise _invalid(f"{where}: kind must be one of {REPOSITORY_KINDS}, got {kind!r}")
    base_commit = _require(raw, "base_commit", str, where).strip().lower()
    if not _SHA_RE.match(base_commit):
        raise _invalid(f"{where}: base_commit must be a full 40-hex commit id, got {base_commit!r}")
    default_branch = str(raw.get("default_branch") or "main")
    if kind == "seed":
        rel = _require(raw, "path", str, where)
        seed_path = (scenario_dir / rel).resolve()
        if not seed_path.is_dir():
            raise _invalid(f"{where}: seed path does not exist: {seed_path}")
        return RepositorySpec(kind=kind, base_commit=base_commit, seed_path=seed_path, default_branch=default_branch)
    url = _require(raw, "url", str, where)
    if not url.strip():
        raise _invalid(f"{where}: url must be non-empty")
    return RepositorySpec(kind=kind, base_commit=base_commit, url=url.strip(), default_branch=default_branch)


def _load_stage(raw: dict[str, Any], scenario_dir: Path, where: str, *, burn_in: bool) -> StagePolicy:
    prompt_rel = _require(raw, "prompt", str, where)
    prompt_path = (scenario_dir / prompt_rel).resolve()
    if not prompt_path.is_file():
        raise _invalid(f"{where}: prompt file does not exist: {prompt_path}")
    if not prompt_path.read_text(encoding="utf-8").strip():
        raise _invalid(f"{where}: prompt file is empty: {prompt_path}")
    max_turns = raw.get("max_turns")
    if max_turns is not None and (not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns <= 0):
        raise _invalid(f"{where}: max_turns must be a positive integer or null")
    timeout = _require(raw, "timeout_seconds", float, where)
    if timeout <= 0:
        raise _invalid(f"{where}: timeout_seconds must be positive")
    capture = str(raw.get("capture") or "claude_hooks")
    if capture not in BURN_IN_CAPTURE_KINDS:
        raise _invalid(f"{where}: capture must be one of {BURN_IN_CAPTURE_KINDS}, got {capture!r}")
    if not burn_in and capture != "claude_hooks":
        raise _invalid(f"{where}: 'capture' is only meaningful for burn_in")
    agent = raw.get("agent")
    if agent is not None and (not isinstance(agent, str) or not agent):
        raise _invalid(f"{where}: 'agent' must be a non-empty string when given")
    if capture == "opencode_hooks" and not agent:
        raise _invalid(f"{where}: capture == 'opencode_hooks' requires an explicit 'agent'")
    agent_model = raw.get("agent_model")
    if agent_model is not None and (not isinstance(agent_model, str) or not agent_model.strip()):
        raise _invalid(f"{where}: 'agent_model' must be a non-empty string when given")
    if capture == "opencode_hooks":
        if not agent_model:
            raise _invalid(
                f"{where}: capture == 'opencode_hooks' requires 'agent_model' (OpenCode's own "
                "'provider/model' id); the benchmark's --model is a Claude Code model and is never "
                "passed to OpenCode"
            )
        if "/" not in agent_model:
            raise _invalid(
                f"{where}: 'agent_model' must be in OpenCode's 'provider/model' form, got {agent_model!r}"
            )
    wrap_stages = _load_wrap_stages(raw.get("wrap_stages"), scenario_dir, where) if capture == "claude_wrap_chain" else ()
    if capture == "claude_wrap_chain" and len(wrap_stages) < 2:
        raise _invalid(f"{where}: capture == 'claude_wrap_chain' requires at least 2 wrap_stages")
    return StagePolicy(
        prompt_path=prompt_path,
        max_turns=max_turns,
        timeout_seconds=float(timeout),
        require_verification_failed=bool(raw.get("require_verification_failed", burn_in)),
        require_known_failed_approach=bool(raw.get("require_known_failed_approach", burn_in)),
        require_expected_evidence=bool(raw.get("require_expected_evidence", burn_in)),
        capture=capture,
        agent=agent,
        agent_model=agent_model,
        wrap_stages=wrap_stages,
    )


def _load_wrap_stages(raw: Any, scenario_dir: Path, where: str) -> tuple[WrapStage, ...]:
    if not isinstance(raw, list) or not raw:
        raise _invalid(f"{where}: 'wrap_stages' must be a non-empty list for capture == 'claude_wrap_chain'")
    stages: list[WrapStage] = []
    for i, item in enumerate(raw):
        stage_where = f"{where}.wrap_stages[{i}]"
        if not isinstance(item, dict):
            raise _invalid(f"{stage_where}: must be an object")
        prompt_rel = _require(item, "prompt", str, stage_where)
        prompt_path = (scenario_dir / prompt_rel).resolve()
        if not prompt_path.is_file():
            raise _invalid(f"{stage_where}: prompt file does not exist: {prompt_path}")
        if not prompt_path.read_text(encoding="utf-8").strip():
            raise _invalid(f"{stage_where}: prompt file is empty: {prompt_path}")
        task = _require(item, "task", str, stage_where)
        if not task.strip():
            raise _invalid(f"{stage_where}: 'task' must be non-empty")
        stages.append(WrapStage(prompt_path=prompt_path, task=task))
    return tuple(stages)


def _load_verification(raw: dict[str, Any]) -> tuple[VerificationStep, ...]:
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise _invalid("verification: 'steps' must be a non-empty list")
    steps: list[VerificationStep] = []
    seen: set[str] = set()
    for i, step in enumerate(steps_raw):
        where = f"verification.steps[{i}]"
        if not isinstance(step, dict):
            raise _invalid(f"{where}: must be an object")
        name = _require(step, "name", str, where)
        if name in seen:
            raise _invalid(f"{where}: duplicate step name {name!r}")
        seen.add(name)
        argv = _str_list(step, "argv", where)
        if not argv:
            raise _invalid(f"{where}: argv must be non-empty")
        env_raw = step.get("env", {})
        if not isinstance(env_raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env_raw.items()):
            raise _invalid(f"{where}: env must be a string->string object")
        timeout = float(step.get("timeout_seconds", 300))
        if timeout <= 0:
            raise _invalid(f"{where}: timeout_seconds must be positive")
        steps.append(VerificationStep(
            name=name, argv=argv, cwd=str(step.get("cwd") or "{workspace}"),
            env=dict(env_raw), timeout_seconds=timeout,
        ))
    return tuple(steps)


def _load_known_failed(raw: dict[str, Any], step_names: set[str]) -> KnownFailedApproach:
    where = "known_failed_approach"
    ident = _require(raw, "id", str, where)
    description = _require(raw, "description", str, where)
    mode = str(raw.get("mode") or "all")
    if mode not in ("all", "any"):
        raise _invalid(f"{where}: mode must be 'all' or 'any'")
    criteria_raw = raw.get("criteria")
    if not isinstance(criteria_raw, list) or not criteria_raw:
        raise _invalid(f"{where}: 'criteria' must be a non-empty list")
    criteria: list[Criterion] = []
    for i, c in enumerate(criteria_raw):
        cw = f"{where}.criteria[{i}]"
        if not isinstance(c, dict):
            raise _invalid(f"{cw}: must be an object")
        kind = _require(c, "kind", str, cw)
        if kind not in CRITERION_KINDS:
            raise _invalid(f"{cw}: kind must be one of {CRITERION_KINDS}, got {kind!r}")
        params = {k: v for k, v in c.items() if k != "kind"}
        if kind == "verification_step_failed":
            step = _require(c, "step", str, cw)
            if step not in step_names:
                raise _invalid(f"{cw}: unknown verification step {step!r}")
        elif kind in ("paths_changed", "paths_unchanged"):
            any_of = _str_list(c, "any_of", cw) if "any_of" in c else ()
            all_of = _str_list(c, "all_of", cw) if "all_of" in c else ()
            if not any_of and not all_of:
                raise _invalid(f"{cw}: needs 'any_of' and/or 'all_of'")
        criteria.append(Criterion(kind=kind, params=params))
    return KnownFailedApproach(id=ident, description=description, criteria=tuple(criteria), mode=mode)


def _load_expected_evidence(raw: dict[str, Any]) -> ExpectedEvidence:
    where = "expected_evidence"
    description = _require(raw, "description", str, where)
    min_shards = raw.get("min_shards", 1)
    if not isinstance(min_shards, int) or isinstance(min_shards, bool) or min_shards < 0:
        raise _invalid(f"{where}: min_shards must be a non-negative integer")
    executor = raw.get("executor", "claude_code_hooks")
    if executor is not None and (not isinstance(executor, str) or not executor):
        raise _invalid(f"{where}: executor must be a non-empty string or null")
    return ExpectedEvidence(
        description=description,
        min_shards=min_shards,
        executor=executor,
        task_contains_any=_str_list(raw, "task_contains_any", where),
        files_include_any=_str_list(raw, "files_include_any", where),
        files_exclude_all=_str_list(raw, "files_exclude_all", where),
    )


def load_scenario(scenario_dir: Path) -> ScenarioConfig:
    """Load and validate ``<scenario_dir>/metadata.json``. Raises ``BenchmarkError``."""
    scenario_dir = Path(scenario_dir).resolve()
    meta_path = scenario_dir / "metadata.json"
    if not meta_path.is_file():
        raise _invalid(f"missing metadata.json in {scenario_dir}")
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _invalid(f"could not read {meta_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise _invalid(f"{meta_path}: top level must be an object")

    ident = _require(data, "id", str, "metadata")
    title = _require(data, "title", str, "metadata")
    if ident != scenario_dir.name:
        raise _invalid(f"metadata id {ident!r} does not match directory name {scenario_dir.name!r}")

    repository = _load_repository(_require(data, "repository", dict, "metadata"), scenario_dir)
    burn_in = _load_stage(_require(data, "burn_in", dict, "metadata"), scenario_dir, "burn_in", burn_in=True)
    evaluation = _load_stage(_require(data, "evaluation", dict, "metadata"), scenario_dir, "evaluation", burn_in=False)
    verification = _load_verification(_require(data, "verification", dict, "metadata"))
    known_failed = _load_known_failed(
        _require(data, "known_failed_approach", dict, "metadata"), {s.name for s in verification}
    )
    expected = _load_expected_evidence(_require(data, "expected_evidence", dict, "metadata"))
    notes = _str_list(data, "notes", "metadata")

    for step in verification:
        for token in (*step.argv, step.cwd):
            for m in re.findall(r"\{[a-z_]+\}", token):
                if m not in PLACEHOLDERS:
                    raise _invalid(f"verification step {step.name!r}: unknown placeholder {m}")

    return ScenarioConfig(
        id=ident, title=title, scenario_dir=scenario_dir, repository=repository,
        burn_in=burn_in, evaluation=evaluation, verification=verification,
        known_failed_approach=known_failed, expected_evidence=expected, notes=notes,
    )
