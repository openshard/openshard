"""RoutingContext: the facts a routing decision is allowed to depend on.

A context carries only what OpenShard actually knows before execution. Every
field that can be unknown is ``None`` (or empty) when it is unknown; nothing
here is guessed to fill a slot. The deterministic baseline reads these facts;
future policies (historical Receipt performance, learned or agent-as-a-router
routing) read the same object, so a new policy never needs new plumbing
through execution.

The context is deliberately separate from policy *configuration*
(``ModelPolicyConfig``), which constrains the candidate set, and from provider
availability, which the candidate set records.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

ROUTING_CONTEXT_VERSION = 1

# Legacy keyword-classifier categories (routing/engine.py ``route``).
TASK_CATEGORIES = frozenset({"boilerplate", "standard", "security", "visual", "complex"})
RISK_LEVELS = frozenset({"low", "medium", "high"})
LATENCY_PREFERENCES = frozenset({"fast", "normal"})
COST_SENSITIVITIES = frozenset({"low", "normal", "high"})

# Bound what a context may carry into a Receipt.
MAX_LANGUAGES = 5


@dataclass(frozen=True)
class RoutingContext:
    """Pre-execution routing facts. ``None`` means unknown, never "no"."""

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
    # caller ask for a routing class directly.
    explicit_model: str | None = None
    requested_class: str | None = None

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
        }

    @property
    def fingerprint(self) -> str:
        """Stable hash of the facts; equal contexts route identically."""
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _known(value: object, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


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
        harness=harness or None,
        explicit_model=explicit_model or None,
    )
