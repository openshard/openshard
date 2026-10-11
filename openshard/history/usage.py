"""Token usage and cost of one model call, as it is recorded.

Providers fill it in and Receipts store it, so it lives with the record
rather than with the provider clients. :mod:`openshard.providers.base`
re-exports these names for existing imports.
"""

from __future__ import annotations

from dataclasses import dataclass

# Where a ``UsageStats.estimated_cost`` figure came from. ``provider_reported``
# is the provider's own figure for this call (OpenRouter ``usage.cost``);
# ``list_rate_estimate`` is OpenShard's arithmetic from the token counts and a
# published per-token price. ``None`` means the provider did not say.
COST_SOURCE_PROVIDER = "provider_reported"
COST_SOURCE_LIST_RATE = "list_rate_estimate"


@dataclass
class UsageStats:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float | None = None
    # Provenance of ``estimated_cost`` (see COST_SOURCE_*). Optional so every
    # existing constructor call keeps working; a missing value stays unknown.
    cost_source: str | None = None
    # Prompt tokens served from a provider cache, when the provider reports them.
    cache_read_tokens: int | None = None
