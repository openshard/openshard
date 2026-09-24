"""Routing evaluation harness: compare decisions and summarize real outcomes.

Two halves, both pure:

* **Decision comparison.** :func:`evaluate_decisions` runs policies over
  representative scenarios against one candidate set and reports, per
  scenario and policy, the class and model chosen, whether the class is one the
  scenario accepts, and any contract violation. This checks routing *intent*
  offline; it says nothing about quality.
* **Outcome metrics.** :func:`summarize_outcomes` turns observed
  :class:`RoutingOutcome` records into verified success rate, cost per
  verified success, latency and attempts. Every metric is computed only over
  outcomes where its inputs are known, reports that coverage, and is ``None``
  when nothing is known. No number is produced without evidence behind it.

Routing regret (the cost of the chosen route versus the best available one)
needs counterfactual outcomes for the same task - e.g. replaying a scenario on
several classes - so it is not computed here yet; ``expectation_mismatches``
is the offline stand-in and is named as one.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from openshard.routing.adaptive.candidates import CandidateSet
from openshard.routing.adaptive.context import RoutingContext
from openshard.routing.adaptive.decision import MODE_NONE, RoutingDecision
from openshard.routing.adaptive.outcome import RoutingOutcome
from openshard.routing.adaptive.policy import RoutingPolicy, decide_route

VIOLATION_INELIGIBLE = "selected_ineligible_model"
VIOLATION_NOT_PROMOTED = "selected_unpromoted_without_explicit_choice"
VIOLATION_DEPRECATED = "selected_deprecated_model"
VIOLATION_EXPLICIT_SUBSTITUTED = "explicit_model_substituted"
VIOLATION_POLICY_ERROR = "policy_error"


@dataclass(frozen=True)
class RoutingScenario:
    name: str
    context: RoutingContext
    # Classes a reasonable router may pick for this scenario.
    acceptable_classes: frozenset[str]
    description: str = ""


# Representative task classes. Contexts use only facts the pipeline produces.
REPRESENTATIVE_SCENARIOS: tuple[RoutingScenario, ...] = (
    RoutingScenario(
        "boilerplate_low_risk",
        RoutingContext(task_category="boilerplate", risk="low", write_requested=True,
                       verification_available=True, verification_requested=True),
        frozenset({"cheap_coding"}),
        "Small helper or validation function with tests available.",
    ),
    RoutingScenario(
        "boilerplate_risky_paths",
        RoutingContext(task_category="boilerplate", risk="high", write_requested=True,
                       verification_available=True, verification_requested=True),
        frozenset({"balanced_coding"}),
        "Boilerplate that writes to risky paths (migrations, infra, auth).",
    ),
    RoutingScenario(
        "standard_feature",
        RoutingContext(task_category="standard", risk="low", write_requested=True,
                       verification_available=True, verification_requested=True),
        frozenset({"balanced_coding"}),
        "Ordinary feature work.",
    ),
    RoutingScenario(
        "standard_unverifiable",
        RoutingContext(task_category="standard", risk="low", write_requested=True,
                       verification_available=False),
        frozenset({"balanced_coding"}),
        "Feature work in a repo with no detected checks: no escalation possible.",
    ),
    RoutingScenario(
        "security_sensitive",
        RoutingContext(task_category="security", risk="high", write_requested=True,
                       verification_available=True, verification_requested=True),
        frozenset({"frontier_reasoning"}),
        "Auth/token/permission change.",
    ),
    RoutingScenario(
        "complex_refactor",
        RoutingContext(task_category="complex", risk="medium", write_requested=True,
                       verification_available=True, verification_requested=True),
        frozenset({"balanced_coding", "frontier_reasoning"}),
        "Multi-file refactor or migration.",
    ),
    RoutingScenario(
        "visual_ui",
        RoutingContext(task_category="visual", risk="low", write_requested=True),
        frozenset({"vision", "balanced_coding"}),
        "UI/CSS work (keyword-classified; no image input known).",
    ),
    RoutingScenario(
        "image_input_required",
        RoutingContext(task_category="standard", required_capabilities=frozenset({"vision"})),
        frozenset({"vision"}),
        "Task that must read an image.",
    ),
    RoutingScenario(
        "read_only_fast",
        RoutingContext(task_category="standard", read_only=True, latency_preference="fast"),
        frozenset({"fast", "cheap_coding"}),
        "Quick explanation with a latency preference.",
    ),
)


@dataclass(frozen=True)
class ScenarioResult:
    scenario: str
    policy: str
    requested_class: str | None
    resolved_class: str | None
    selected_model: str | None
    selection_mode: str
    acceptable: bool
    violations: tuple[str, ...] = ()
    decision: RoutingDecision | None = field(default=None, compare=False, repr=False)


def policy_violations(decision: RoutingDecision, candidates: CandidateSet) -> list[str]:
    """Contract checks every policy must pass."""
    out: list[str] = []
    ctx = decision.context
    if decision.selected_model is None:
        return out
    cand = candidates.get(decision.selected_model)
    if cand is None:
        return [VIOLATION_INELIGIBLE]
    if not cand.entry.curated and not cand.explicit:
        out.append(VIOLATION_NOT_PROMOTED)
    if cand.entry.status == "deprecated":
        out.append(VIOLATION_DEPRECATED)
    if ctx.explicit_model:
        wanted = candidates.catalog.resolve(ctx.explicit_model) or ctx.explicit_model
        if decision.selected_model != wanted:
            out.append(VIOLATION_EXPLICIT_SUBSTITUTED)
    return out


def evaluate_decisions(
    scenarios: Iterable[RoutingScenario],
    candidates: CandidateSet,
    policies: Sequence[RoutingPolicy],
) -> list[ScenarioResult]:
    """Every (scenario, policy) pair, in input order. A policy that raises or
    selects an ineligible model is reported, not propagated."""
    results: list[ScenarioResult] = []
    for sc in scenarios:
        for pol in policies:
            try:
                d = decide_route(sc.context, candidates, policy=pol)
            except Exception:
                results.append(ScenarioResult(
                    sc.name, f"{pol.name}@{pol.version}", None, None, None, MODE_NONE,
                    False, (VIOLATION_POLICY_ERROR,),
                ))
                continue
            results.append(ScenarioResult(
                scenario=sc.name,
                policy=f"{d.policy_name}@{d.policy_version}",
                requested_class=d.requested_class,
                resolved_class=d.resolved_class,
                selected_model=d.selected_model,
                selection_mode=d.selection_mode,
                acceptable=d.resolved_class in sc.acceptable_classes,
                violations=tuple(policy_violations(d, candidates)),
                decision=d,
            ))
    return results


@dataclass(frozen=True)
class OutcomeSummary:
    outcomes: int
    verification_known: int
    verified_successes: int
    verified_success_rate: float | None
    cost_known: int
    cost_per_verified_success: float | None
    latency_known: int
    mean_latency_seconds: float | None
    attempts_known: int
    mean_attempts: float | None
    human_corrections_known: int
    human_corrections: int
    shadow_comparable: int
    shadow_agreement_rate: float | None
    expectation_mismatches: int | None = None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize_outcomes(
    outcomes: Iterable[RoutingOutcome],
    *,
    scenario_results: Iterable[ScenarioResult] | None = None,
) -> OutcomeSummary:
    """Metrics over observed outcomes only (see module docstring)."""
    items = list(outcomes)
    known = [o for o in items if o.verified_success is not None]
    successes = [o for o in known if o.verified_success]
    # Cost per verified success needs the cost of every verified attempt,
    # failures included: spend on failures is part of the price of success.
    costed = [o for o in known if o.cost_usd is not None]
    cost_per_success = None
    if successes and len(costed) == len(known):
        cost_per_success = sum(o.cost_usd for o in costed if o.cost_usd is not None) / len(successes)
    latencies = [o.latency_seconds for o in items if o.latency_seconds is not None]
    attempts = [float(o.attempts) for o in items if o.attempts is not None]
    corrections = [o for o in items if o.human_correction is not None]
    shadow = [o for o in items if o.shadow_agreed is not None]
    mismatches = None
    if scenario_results is not None:
        mismatches = sum(1 for r in scenario_results if not r.acceptable)
    return OutcomeSummary(
        outcomes=len(items),
        verification_known=len(known),
        verified_successes=len(successes),
        verified_success_rate=(len(successes) / len(known)) if known else None,
        cost_known=len(costed),
        cost_per_verified_success=cost_per_success,
        latency_known=len(latencies),
        mean_latency_seconds=_mean(latencies),
        attempts_known=len(attempts),
        mean_attempts=_mean(attempts),
        human_corrections_known=len(corrections),
        human_corrections=sum(1 for o in corrections if o.human_correction),
        shadow_comparable=len(shadow),
        shadow_agreement_rate=(
            sum(1 for o in shadow if o.shadow_agreed) / len(shadow) if shadow else None
        ),
        expectation_mismatches=mismatches,
    )
