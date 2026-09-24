from __future__ import annotations

# Score added when a candidate's ID exactly matches a category-preferred model.
EXACT_BONUS: float = 3.0

# Score added when a candidate's model slug (no provider prefix) contains a
# preferred family substring — used as fallback when exact IDs are absent.
FAMILY_BONUS: float = 1.0

# Preferred model IDs per task category, in priority order.
# These are the intended routing-spine models; exact matches earn EXACT_BONUS.
CATEGORY_PREFERRED: dict[str, list[str]] = {
    "standard": [
        "anthropic/claude-sonnet-4.6",
        "deepseek/deepseek-v4-pro",
        "openai/gpt-5.4",
        "z-ai/glm-5.1",
    ],
    "complex": [
        "openai/gpt-5.5",
        "openai/gpt-5.5-pro",
        "anthropic/claude-opus-4.7",
        "openai/gpt-5.4-pro",
        "openai/gpt-5.4",
        "anthropic/claude-sonnet-4.6",
    ],
    "security": [
        "openai/gpt-5.5",
        "openai/gpt-5.5-pro",
        "anthropic/claude-opus-4.7",
        "openai/gpt-5.4-pro",
        "anthropic/claude-sonnet-4.6",
    ],
    "boilerplate": [
        "deepseek/deepseek-v4-flash",
        "openai/gpt-5.4-mini",
        "openai/gpt-5.4-nano",
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.1",
        "minimax/minimax-m2.7",
    ],
    "visual": [
        "google/gemini-3.1-pro-preview",
        "google/gemini-3.1-pro-preview-customtools",
        "qwen/qwen3.6-plus",
        "google/gemini-3.1-flash-lite-preview",
    ],
}

# Broad family substrings derived from the spine above, matched against
# a candidate's slug (model ID with provider prefix stripped) when no
# exact preferred ID is present in the inventory.
CATEGORY_PREFERRED_FAMILIES: dict[str, list[str]] = {
    "standard":    ["claude-sonnet", "deepseek", "gpt-5.4", "glm"],
    "complex":     ["gpt-5.5", "claude-opus", "gpt-5.4", "claude-sonnet"],
    "security":    ["gpt-5.5", "claude-opus", "gpt-5.4", "claude-sonnet"],
    "boilerplate": ["deepseek", "gpt-5.4", "glm", "minimax"],
    "visual":      ["gemini-3.1", "qwen3.6", "kimi-k2"],
}


def policy_bonus(model_id: str, category: str) -> float:
    """Return a policy-preference score bonus for *model_id* in *category*.

    Tier 1 — exact ID match:    EXACT_BONUS  (3.0)
    Tier 2 — family slug match: FAMILY_BONUS (1.0)
    No match:                   0.0

    Hard filters are applied before scoring, so this bonus only shifts
    relative rank among candidates that already pass all requirements.
    """
    # Tier 1: exact preferred ID
    if model_id in CATEGORY_PREFERRED.get(category, []):
        return EXACT_BONUS

    # Tier 2: family match against the slug (provider prefix stripped)
    slug = model_id.lower().split("/", 1)[-1]
    for family in CATEGORY_PREFERRED_FAMILIES.get(category, []):
        if family in slug:
            return FAMILY_BONUS

    return 0.0
