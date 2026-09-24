from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Models — resolved from the registry; hardcoded IDs are fallback only.
# ---------------------------------------------------------------------------
from openshard.routing.model_resolver import (
    ESCALATION_CHAIN,
    MODEL_CHEAP,
    MODEL_COMPLEX,
    MODEL_ESCALATE,
    MODEL_MAIN,
    MODEL_STRONG,
    MODEL_VISUAL,
)

__all__ = [
    "ESCALATION_CHAIN",
    "MODEL_CHEAP",
    "MODEL_COMPLEX",
    "MODEL_ESCALATE",
    "MODEL_MAIN",
    "MODEL_STRONG",
    "MODEL_VISUAL",
]

# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------

@dataclass
class RoutingDecision:
    model: str
    category: str   # boilerplate | standard | security | visual | complex
    rationale: str  # shown in --more output
    # Shadow adaptive-routing decision (routing.adaptive.RoutingDecision) for
    # the same run; recorded in the Receipt, never used to pick the model.
    adaptive: Any = field(default=None, compare=False, repr=False)


# ---------------------------------------------------------------------------
# Keyword sets (substring match on lowercased task)
# ---------------------------------------------------------------------------

_SECURITY_KW = {
    "auth", "login", "logout", "password", "token", "jwt", "session",
    "encrypt", "decrypt", "security", "permission", "role", "rbac",
    "oauth", "api key", "secret", "credential", "privilege",
    "access control", "firewall", "ssl", "tls", "certificate",
}

_VISUAL_KW = {
    "ui", "component", "styling", "css", "layout", "design",
    "visual", "chart", "graph", "dashboard", "react", "vue", "angular",
    "tailwind", "scss", "animation", "image processing", "svg",
}

_COMPLEX_KW = {
    "refactor", "migrate", "migration", "architecture", "restructur",
    "system-wide", "multi-file", "large-scale", "codebase", "throughout",
    "across all", "across the", "every file", "optimization",
    "performance tuning",
}

_BOILERPLATE_KW = {
    "validation", "validate", "format", "formatter", "helper", "utility",
    "util", "boilerplate", "getter", "setter", "sanitize",
    "simple form", "basic", "add simple", "add a simple",
}


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def _matches(lower: str, kw_set: set[str]) -> bool:
    """True if any keyword in *kw_set* appears as a substring of *lower*."""
    return any(kw in lower for kw in kw_set)


def route(task: str) -> RoutingDecision:
    """Classify *task* and return the appropriate model with rationale.

    Priority order (first match wins):
    1. Security-sensitive  → Sonnet (careful reasoning required)
    2. Visual / frontend   → Kimi K2.5 (multimodal specialist)
    3. Complex / multi-file → MiniMax M2.7 (long-horizon model)
    4. Boilerplate         → DeepSeek V4 Flash (cheapest)
    5. Default             → GLM-5.1 (main worker)
    """
    lower = task.lower()
    word_count = len(task.split())

    if _matches(lower, _SECURITY_KW):
        return RoutingDecision(
            model=MODEL_STRONG,
            category="security",
            rationale="security-sensitive code requires careful reasoning",
        )

    if _matches(lower, _VISUAL_KW):
        return RoutingDecision(
            model=MODEL_VISUAL,
            category="visual",
            rationale="UI or visual task routed to multimodal specialist",
        )

    if _matches(lower, _COMPLEX_KW) or word_count > 60:
        return RoutingDecision(
            model=MODEL_COMPLEX,
            category="complex",
            rationale="multi-file or long-horizon task",
        )

    if _matches(lower, _BOILERPLATE_KW):
        return RoutingDecision(
            model=MODEL_CHEAP,
            category="boilerplate",
            rationale="low-risk boilerplate task",
        )

    return RoutingDecision(
        model=MODEL_MAIN,
        category="standard",
        rationale="standard feature implementation",
    )


# ---------------------------------------------------------------------------
# Read-only intent detection
# ---------------------------------------------------------------------------

_READONLY_PREFIXES: tuple[str, ...] = (
    "what does ", "what is ", "what are ", "what's ",
    "explain ", "summarise ", "summarize ",
    "describe ", "walk me through ",
    "where is ", "where are ",
    "show me ", "how does ", "how do ",
    "tell me about ", "list the ", "list all ",
    "find where ", "find what ",
)

_WRITE_IMPERATIVES: tuple[str, ...] = (
    "fix ", "add ", "update ", "implement ", "refactor ",
    "create ", "remove ", "delete ", "migrate ", "rewrite ",
    "change ", "replace ", "set up ", "install ", "write ",
    "make ", "build ", "edit ", "modify ", "rename ",
)

_AND_WRITE_RE = re.compile(
    r"\band\s+(fix|add|update|implement|remove|delete|change|edit|modify)\b"
)


def is_readonly_task(task: str) -> bool:
    """Return True if *task* is a read-only explanation/analysis request.

    Detection is prefix-based and deterministic:
    - Tasks starting with a write-imperative verb are never read-only.
    - Tasks containing "and <write-verb>" (e.g. "find and fix") are never read-only.
    - Tasks starting with an interrogative/explanation prefix are read-only.
    """
    t = task.strip().lower()
    if any(t.startswith(w) for w in _WRITE_IMPERATIVES):
        return False
    if _AND_WRITE_RE.search(t):
        return False
    return any(t.startswith(p) for p in _READONLY_PREFIXES)


# ---------------------------------------------------------------------------
# Inline read-only instruction detection
# ---------------------------------------------------------------------------

_INLINE_READONLY_PHRASES: tuple[str, ...] = (
    "do not apply changes",
    "do not make changes",
    "do not write files",
    "review only",
    "do not modify",
    "without making changes",
    "without modifying files",
    "do not change files",
    "no file changes",
    "read only",
    "read-only",
)

_REVIEW_TASK_TERMS: tuple[str, ...] = (
    "review",
    "audit",
    "assess",
    "investigate",
    "identify risks",
    "production readiness",
    "security",
    "terraform",
    "iac",
    "hardening",
)


def has_inline_readonly_instruction(task: str) -> bool:
    """Return True if the task body contains an explicit do-not-write instruction."""
    t = task.lower()
    return any(phrase in t for phrase in _INLINE_READONLY_PHRASES)


def looks_like_review_task(task: str) -> bool:
    """Return True if the task reads like a structured review/audit/assessment."""
    t = task.lower()
    return any(term in t for term in _REVIEW_TASK_TERMS)


# ---------------------------------------------------------------------------
# Review domain classification
# ---------------------------------------------------------------------------

_REVIEW_DOMAIN_SIGNALS: dict[str, tuple[str, ...]] = {
    "cicd": (
        "ci/cd", "cicd", "github actions", "workflow", "pipeline",
        "deployment checks", "pre-deploy", "pre-deployment",
        "github workflows", "azure-pipelines", "jenkinsfile", "gitlab ci",
    ),
    "auth_security": (
        "auth", "authentication", "authorization", "login", "session",
        "token", "jwt", "oauth",
    ),
    "tests": (
        "test coverage", "missing tests", "pytest", "unit tests",
        "integration tests",
    ),
    "docs_onboarding": (
        "readme", "onboarding", "documentation", "new developer", "docs",
    ),
    "terraform_iac": (
        "terraform", "iac", "infrastructure", "gcp", "aws", "azure",
        "cloud", "production readiness", "hardening",
    ),
}


def classify_review_domain(task: str) -> str:
    """Return the review domain for *task*.

    Non-terraform domains are checked first so a specific match wins over
    the broad terraform_iac set.

    Returns one of:
        cicd | auth_security | tests | docs_onboarding | terraform_iac | generic_review
    """
    t = task.lower()
    for domain in ("cicd", "auth_security", "tests", "docs_onboarding"):
        if any(signal in t for signal in _REVIEW_DOMAIN_SIGNALS[domain]):
            return domain
    if any(signal in t for signal in _REVIEW_DOMAIN_SIGNALS["terraform_iac"]):
        return "terraform_iac"
    return "generic_review"


# ---------------------------------------------------------------------------
# Legacy class (preserved for any existing callers)
# ---------------------------------------------------------------------------

class RoutingEngine:
    """Select the appropriate model for a given task."""

    def select_model(self, task: str) -> str:
        return route(task).model
