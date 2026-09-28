"""RoutingContext: the facts a routing decision is allowed to depend on.

A context carries only what OpenShard actually knows at the decision boundary.
Every field that can be unknown is ``None`` (or empty) when it is unknown;
nothing here is guessed to fill a slot. The deterministic policies read these
facts; future policies (a small learned router over the same structured
features) read the same object, so a new policy never needs new plumbing
through execution.

Version 2 adds the trajectory: which step of the run this is, what was tried,
what OpenShard observed of the last attempt's verification, what has been
spent and what may be spent, and whether dogfood candidates may compete. A
version-1 context (no step fields) is still valid and routes as before.

The context is deliberately separate from policy *configuration*
(``ModelPolicyConfig``), which constrains the candidate set, and from provider
availability, which the candidate set records.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from openshard.routing.adaptive.step_types import FAILURE_CLASSES, STEP_TYPES

ROUTING_CONTEXT_VERSION = 2

# Legacy keyword-classifier categories (routing/engine.py ``route``).
TASK_CATEGORIES = frozenset({"boilerplate", "standard", "security", "visual", "complex"})
RISK_LEVELS = frozenset({"low", "medium", "high"})
LATENCY_PREFERENCES = frozenset({"fast", "normal"})
COST_SENSITIVITIES = frozenset({"low", "normal", "high"})

# Bound what a context may carry into a Receipt.
MAX_LANGUAGES = 5
MAX_MODELS_TRIED = 8


@dataclass(frozen=True)
class RoutingContext:
    """Routing facts at one decision boundary. ``None`` means unknown, never "no"."""

    # What the task is. ``task_category`` comes from the keyword classifier
    # (``category_source`` says so) - a heuristic, recorded as one.
    task_category: str | None = None
    category_source: str | None = None
    read_only: bool | None = None
    write_requested: bool | None = None
    # low | medium | high, from the form-factor risk derivation.
    risk: str | None = None

    # Hard capability requirements, as catalog capability tags ("vision",
    # "tools", "long_context", ...). Only set when the caller knows the task
    # needs them; a heuristic category never becomes a hard requirement.
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    # Minimum context window in tokens, when known.
    min_context_tokens: int | None = None

    # Verification: whether checks exist for this repo, and whether the run
    # asked for them. Recovery escalation depends on both.
    verification_available: bool | None = None
    verification_requested: bool | None = None

    # Repository facts, when a scan ran.
    languages: tuple[str, ...] = ()
    framework: str | None = None

    # Preferences. None = no stated preference.
    latency_preference: str | None = None
    cost_sensitivity: str | None = None

    # Harness/agent that will execute (native, direct, staged, opencode, ...).
    # Kept separate from the model: the same model behaves differently under
    # different harnesses, and future routing may choose the pair.
    harness: str | None = None

    # Explicit choices. An explicit model is the user's decision; routing
    # never substitutes a different one for it. ``requested_class`` lets a
    # caller ask for a routing class (legacy or requirement name) directly.
    explicit_model: str | None = None
    requested_class: str | None = None

    # ---- Trajectory (version 2) ------------------------------------------
    # Which step of the run is being routed (step_types.STEP_TYPES).
    step_type: str | None = None
    # 1-based attempt number within the run, when the harness counts attempts.
    attempt: int | None = None
    # Requested model ids already used in this run, oldest first.
    models_tried: tuple[str, ...] = ()
    # What OpenShard knows of the previous attempt's verification: the
    # status token and who vouches for it (history.verification). Unknown
    # stays None and is never treated as success.
    last_verification_status: str | None = None
    last_verification_source: str | None = None
    # Classification of the previous failure (step_types.FAILURE_CLASSES).
    previous_failure_class: str | None = None
    # Estimated spend so far and the cap the run must stay under, in USD.
    # ``accumulated_cost_usd`` is None when any attempt's cost was not reported.
    accumulated_cost_usd: float | None = None
    cost_budget_usd: float | None = None
    # Whether dogfood candidates may compete in this run (the capability gate
    # the caller already checked). False for public runs.
    dogfood_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Bounded, JSON-safe projection. Contains no task text."""
        return {
            "version": ROUTING_CONTEXT_VERSION,
            "task_category": self.task_category,
            "category_source": self.category_source,
            "read_only": self.read_only,
            "write_requested": self.write_requested,
            "risk": self.risk,
            "required_capabilities": sorted(self.required_capabilities),
            "min_context_tokens": self.min_context_tokens,
            "verification_available": self.verification_available,
            "verification_requested": self.verification_requested,
            "languages": list(self.languages[:MAX_LANGUAGES]),
            "framework": self.framework,
            "latency_preference": self.latency_preference,
            "cost_sensitivity": self.cost_sensitivity,
            "harness": self.harness,
            "explicit_model": self.explicit_model,
            "requested_class": self.requested_class,
            "step_type": self.step_type,
            "attempt": self.attempt,
            "models_tried": list(self.models_tried[:MAX_MODELS_TRIED]),
            "last_verification_status": self.last_verification_status,
            "last_verification_source": self.last_verification_source,
            "previous_failure_class": self.previous_failure_class,
            "accumulated_cost_usd": self.accumulated_cost_usd,
            "cost_budget_usd": self.cost_budget_usd,
            "dogfood_enabled": self.dogfood_enabled,
        }

    @property
    def fingerprint(self) -> str:
        """Stable hash of the facts; equal contexts route identically."""
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _known(value: object, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _cost(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if f == f and f not in (float("inf"), float("-inf")) and f >= 0 else None


def routing_context_for_run(
    *,
    task_category: str | None,
    read_only: bool | None = None,
    write_requested: bool | None = None,
    risk: str | None = None,
    repo_facts: object | None = None,
    verification_available: bool | None = None,
    verification_requested: bool | None = None,
    harness: str | None = None,
    required_capabilities: frozenset[str] = frozenset(),
    explicit_model: str | None = None,
    step_type: str | None = None,
    attempt: int | None = None,
    models_tried: tuple[str, ...] = (),
    last_verification_status: str | None = None,
    last_verification_source: str | None = None,
    previous_failure_class: str | None = None,
    accumulated_cost_usd: float | None = None,
    cost_budget_usd: float | None = None,
    cost_sensitivity: str | None = None,
    dogfood_enabled: bool = False,
) -> RoutingContext:
    """Build a context from the signals the run pipeline already computes.

    Unrecognised values are dropped to ``None`` rather than passed through, so
    a context never carries a value the policies do not understand.
    """
    languages: tuple[str, ...] = ()
    framework: str | None = None
    if repo_facts is not None:
        raw_langs = getattr(repo_facts, "languages", None)
        if isinstance(raw_langs, (list, tuple)):
            languages = tuple(str(x) for x in raw_langs if x)[:MAX_LANGUAGES]
        raw_fw = getattr(repo_facts, "framework", None)
        framework = str(raw_fw) if raw_fw else None
    category = _known(task_category, TASK_CATEGORIES)
    return RoutingContext(
        task_category=category,
        category_source="keyword_classifier" if category else None,
        read_only=read_only,
        write_requested=write_requested,
        risk=_known(risk, RISK_LEVELS),
        required_capabilities=frozenset(required_capabilities),
        verification_available=verification_available,
        verification_requested=verification_requested,
        languages=languages,
        framework=framework,
        cost_sensitivity=_known(cost_sensitivity, COST_SENSITIVITIES),
        harness=harness or None,
        explicit_model=explicit_model or None,
        step_type=_known(step_type, STEP_TYPES),
        attempt=int(attempt) if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 1 else None,
        models_tried=tuple(str(m) for m in models_tried if m)[:MAX_MODELS_TRIED],
        last_verification_status=last_verification_status or None,
        last_verification_source=last_verification_source or None,
        previous_failure_class=_known(previous_failure_class, FAILURE_CLASSES),
        accumulated_cost_usd=_cost(accumulated_cost_usd),
        cost_budget_usd=_cost(cost_budget_usd),
        dogfood_enabled=bool(dogfood_enabled),
    )
