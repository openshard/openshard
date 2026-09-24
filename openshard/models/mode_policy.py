from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModeModelPolicy:
    mode: str
    default_model_id: str
    fallback_model_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    advisory_only: bool = True


def _cheap_coding_default() -> str:
    """Ask Mode follows the cheap_coding routing class, not a pinned version."""
    from openshard.routing.model_resolver import MODEL_CHEAP

    return MODEL_CHEAP


_ASK_POLICY = ModeModelPolicy(
    mode="ask",
    default_model_id=_cheap_coding_default(),
    fallback_model_ids=(
        "openai/gpt-5-nano",
        "google/gemini-3.1-flash-lite",
        "ibm-granite/granite-4.1-8b",
    ),
    reasons=(
        "Ask Mode answers product/help/model/context questions.",
        "Low-cost control models are sufficient for these queries.",
        "Expensive frontier models are unnecessary overhead.",
    ),
)

_PLAN_POLICY = ModeModelPolicy(
    mode="plan",
    default_model_id="deepseek/deepseek-v4-pro",
    fallback_model_ids=(
        "moonshotai/kimi-k2.6",
        "openai/gpt-5.4-mini",
        "google/gemini-3.1-flash-lite",
    ),
    reasons=(
        "Plan Mode needs more reasoning than Ask Mode.",
        "Value-tier models provide sufficient planning capability.",
        "Frontier models remain reserved for Run mode high-risk tasks.",
    ),
    advisory_only=False,  # wired: PlanGenerator uses this model
)


def model_policy_for_mode(mode: str) -> ModeModelPolicy | None:
    """Return the advisory model policy for a mode, or None for run/unknown."""
    mode = mode.strip().lower()
    if mode == "ask":
        return _ASK_POLICY
    if mode == "plan":
        return _PLAN_POLICY
    return None
