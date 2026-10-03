"""Official-list-rate cost estimation from provider-reported token usage.

The Receipt should not omit cost merely because an agent surface reports usage
but not dollars. This module turns trustworthy token counts into a deterministic
estimate using a dated snapshot of public provider list prices.

Provider-reported dollar totals still win. Token counts must have explicit
provenance before they are priced. Unknown models stay unknown. Cache reads and
writes are priced separately when the provider exposes them. These are list-rate
estimates, not billing statements: plan credits, batch, regional processing,
fast mode, long-context uplifts and negotiated pricing can differ.

Sources checked 2026-10-03:
OpenAI: https://developers.openai.com/api/docs/pricing
Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
Google: https://ai.google.dev/gemini-api/docs/pricing
MiniMax: https://platform.minimax.io/docs/guides/pricing-paygo
Moonshot: https://platform.kimi.ai/docs/pricing/chat
Z.ai: https://docs.z.ai/guides/overview/pricing

Rates that depend on prompt length or time of day (xAI Grok above 200k input,
MiniMax-M3 above 512k, DeepSeek peak/off-peak, Gemini Pro above 200k) have no
single list rate and are deliberately absent: an estimate there would be wrong
for part of the traffic. Anthropic ``cache_write_per_mtok`` is the 5-minute
cache-write rate; the 1-hour rate (``cache_write_1h_per_mtok``) is the explicit
"1h cache writes" column of the same Anthropic table, used only when the usage
says which tokens went to the 1-hour cache.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

PRICING_SNAPSHOT_DATE = "2026-10-03"
COST_PROVENANCE_OFFICIAL_RATE = "official_rate_estimate"


@dataclass(frozen=True)
class OfficialRate:
    provider: str
    model_id: str
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    source: str = ""
    as_of: str = PRICING_SNAPSHOT_DATE
    aliases: tuple[str, ...] = ()
    cache_write_1h_per_mtok: float | None = None


@dataclass(frozen=True)
class CostEstimate:
    usd: float
    provider: str
    model_id: str
    source: str
    as_of: str
    input_usd: float
    output_usd: float
    cache_read_usd: float
    cache_write_usd: float


_OPENAI = "https://developers.openai.com/api/docs/pricing"
_ANTHROPIC = "https://platform.claude.com/docs/en/about-claude/pricing"
_GOOGLE = "https://ai.google.dev/gemini-api/docs/pricing"
_MINIMAX = "https://platform.minimax.io/docs/guides/pricing-paygo"
_MOONSHOT = "https://platform.kimi.ai/docs/pricing/chat"
_ZAI = "https://docs.z.ai/guides/overview/pricing"

_RATES: tuple[OfficialRate, ...] = (
    OfficialRate("openai", "gpt-6-astra", 10.0, 50.0, 1.0, 12.50, _OPENAI, aliases=("openai/gpt-6-astra",)),
    OfficialRate("openai", "gpt-6.1-sol", 2.0, 10.0, 0.10, 2.50, _OPENAI, aliases=("openai/gpt-6.1-sol",)),
    OfficialRate("openai", "gpt-6-sol", 2.0, 10.0, 0.20, 2.50, _OPENAI, aliases=("openai/gpt-6-sol",)),
    OfficialRate("openai", "gpt-6-luna", 0.10, 0.50, 0.01, 0.125, _OPENAI, aliases=("openai/gpt-6-luna",)),
    OfficialRate("openai", "gpt-5.6-sol", 4.0, 20.0, 0.40, 5.0, _OPENAI, aliases=("openai/gpt-5.6-sol", "GPT-5.6 Sol")),
    OfficialRate("openai", "gpt-5.6-terra", 2.0, 12.0, 0.20, 2.50, _OPENAI, aliases=("openai/gpt-5.6-terra",)),
    OfficialRate("openai", "gpt-5.6-luna", 0.20, 1.20, 0.02, 0.25, _OPENAI, aliases=("openai/gpt-5.6-luna",)),
    OfficialRate("openai", "gpt-5.5", 5.0, 30.0, 0.50, None, _OPENAI, aliases=("openai/gpt-5.5",)),
    OfficialRate("openai", "gpt-5.5-pro", 30.0, 180.0, None, None, _OPENAI, aliases=("openai/gpt-5.5-pro",)),
    OfficialRate("openai", "gpt-5.4", 2.50, 15.0, 0.25, None, _OPENAI, aliases=("openai/gpt-5.4",)),
    OfficialRate("openai", "gpt-5.4-pro", 30.0, 180.0, None, None, _OPENAI, aliases=("openai/gpt-5.4-pro",)),
    OfficialRate("openai", "gpt-5.4-mini", 0.75, 4.50, 0.075, None, _OPENAI, aliases=("openai/gpt-5.4-mini",)),
    OfficialRate("openai", "gpt-5.4-nano", 0.20, 1.25, 0.02, None, _OPENAI, aliases=("openai/gpt-5.4-nano",)),
    OfficialRate("anthropic", "claude-fable-5-1", 10.0, 50.0, 0.25, 12.50, _ANTHROPIC, aliases=("anthropic/claude-fable-5-1", "anthropic/claude-fable-5.1", "claude-fable-5.1"), cache_write_1h_per_mtok=20.0),
    OfficialRate("anthropic", "claude-fable-5", 10.0, 50.0, 1.0, 12.50, _ANTHROPIC, aliases=("anthropic/claude-fable-5",), cache_write_1h_per_mtok=20.0),
    OfficialRate("anthropic", "claude-opus-5-5", 4.0, 20.0, 0.20, 5.0, _ANTHROPIC, aliases=("anthropic/claude-opus-5-5", "anthropic/claude-opus-5.5", "claude-opus-5.5"), cache_write_1h_per_mtok=8.0),
    OfficialRate("anthropic", "claude-opus-5", 5.0, 25.0, 0.50, 6.25, _ANTHROPIC, aliases=("anthropic/claude-opus-5",), cache_write_1h_per_mtok=10.0),
    OfficialRate("anthropic", "claude-opus-4-8", 5.0, 25.0, 0.50, 6.25, _ANTHROPIC, aliases=("anthropic/claude-opus-4-8", "anthropic/claude-opus-4.8", "claude-opus-4.8"), cache_write_1h_per_mtok=10.0),
    OfficialRate("anthropic", "claude-opus-4-7", 5.0, 25.0, 0.50, 6.25, _ANTHROPIC, aliases=("anthropic/claude-opus-4-7", "anthropic/claude-opus-4.7", "claude-opus-4.7"), cache_write_1h_per_mtok=10.0),
    OfficialRate("anthropic", "claude-opus-4-6", 5.0, 25.0, 0.50, 6.25, _ANTHROPIC, aliases=("anthropic/claude-opus-4-6", "anthropic/claude-opus-4.6", "claude-opus-4.6"), cache_write_1h_per_mtok=10.0),
    OfficialRate("anthropic", "claude-sonnet-5-5", 2.0, 10.0, 0.20, 2.50, _ANTHROPIC, aliases=("anthropic/claude-sonnet-5-5", "anthropic/claude-sonnet-5.5", "claude-sonnet-5.5"), cache_write_1h_per_mtok=4.0),
    OfficialRate("anthropic", "claude-sonnet-5", 2.0, 10.0, 0.20, 2.50, _ANTHROPIC, aliases=("anthropic/claude-sonnet-5",), cache_write_1h_per_mtok=4.0),
    OfficialRate("anthropic", "claude-sonnet-4-6", 3.0, 15.0, 0.30, 3.75, _ANTHROPIC, aliases=("anthropic/claude-sonnet-4-6", "anthropic/claude-sonnet-4.6", "claude-sonnet-4.6"), cache_write_1h_per_mtok=6.0),
    OfficialRate("anthropic", "claude-haiku-4-5", 1.0, 5.0, 0.10, 1.25, _ANTHROPIC, aliases=("anthropic/claude-haiku-4-5", "anthropic/claude-haiku-4.5", "claude-haiku-4.5", "claude-haiku-4-5-20251001"), cache_write_1h_per_mtok=2.0),
    # Gemini 3.6-3.8 Flash list rates rise on 2027-01-01 per the pricing page;
    # these are the rates in force on PRICING_SNAPSHOT_DATE.
    OfficialRate("google", "gemini-3.8-flash", 0.75, 3.75, 0.075, None, _GOOGLE, aliases=("google/gemini-3.8-flash",)),
    OfficialRate("google", "gemini-3.7-flash", 0.75, 3.75, 0.075, None, _GOOGLE, aliases=("google/gemini-3.7-flash",)),
    OfficialRate("google", "gemini-3.6-flash", 0.75, 3.75, 0.075, None, _GOOGLE, aliases=("google/gemini-3.6-flash",)),
    OfficialRate("google", "gemini-3.5-flash", 1.50, 9.0, 0.15, None, _GOOGLE, aliases=("google/gemini-3.5-flash",)),
    OfficialRate("google", "gemini-3.5-flash-lite", 0.30, 2.50, 0.03, None, _GOOGLE, aliases=("google/gemini-3.5-flash-lite",)),
    OfficialRate("google", "gemini-3.1-flash-lite", 0.25, 1.50, 0.025, None, _GOOGLE, aliases=("google/gemini-3.1-flash-lite",)),
    OfficialRate("minimax", "MiniMax-M2.7", 0.30, 1.20, 0.06, 0.375, _MINIMAX, aliases=("minimax/minimax-m2.7",)),
    OfficialRate("moonshot", "kimi-k3", 3.0, 15.0, 0.30, None, _MOONSHOT, aliases=("moonshotai/kimi-k3",)),
    OfficialRate("moonshot", "kimi-k2.6", 0.95, 4.0, 0.16, None, _MOONSHOT, aliases=("moonshotai/kimi-k2.6",)),
    OfficialRate("zai", "glm-5.1", 1.40, 4.40, 0.26, None, _ZAI, aliases=("z-ai/glm-5.1",)),
)

_INDEX: dict[str, OfficialRate] = {}
for _rate in _RATES:
    for _name in (_rate.model_id, *_rate.aliases):
        _INDEX[_name.strip().lower()] = _rate


def official_rate(model_id: str | None) -> OfficialRate | None:
    """Return an exact or declared-alias official rate. Never fuzzy-match."""
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    return _INDEX.get(model_id.strip().lower())


def _tokens(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def estimate_usage_cost(
    model_id: str | None,
    *,
    input_tokens: object = 0,
    output_tokens: object = 0,
    cache_read_tokens: object = 0,
    cache_write_tokens: object = 0,
    cache_write_1h_tokens: object = 0,
) -> CostEstimate | None:
    """Estimate list-rate USD from normalized token counters.

    input_tokens means uncached/base input when cache counters are supplied.
    Adapters that receive a provider total containing cached input must
    normalize it before storing the Receipt counters.

    ``cache_write_tokens`` are priced at the 5-minute cache-write rate and
    ``cache_write_1h_tokens`` at the 1-hour rate. If a positive cache counter
    has no published token rate, return None instead of silently
    understating cost.
    """
    rate = official_rate(model_id)
    if rate is None:
        return None

    i = _tokens(input_tokens)
    o = _tokens(output_tokens)
    cr = _tokens(cache_read_tokens)
    cw = _tokens(cache_write_tokens)
    cw1h = _tokens(cache_write_1h_tokens)
    if i == o == cr == cw == cw1h == 0:
        return None
    if cr and rate.cached_input_per_mtok is None:
        return None
    if cw and rate.cache_write_per_mtok is None:
        return None
    if cw1h and rate.cache_write_1h_per_mtok is None:
        return None

    input_usd = i * rate.input_per_mtok / 1_000_000
    output_usd = o * rate.output_per_mtok / 1_000_000
    cache_read_usd = cr * (rate.cached_input_per_mtok or 0.0) / 1_000_000
    cache_write_usd = (
        cw * (rate.cache_write_per_mtok or 0.0) + cw1h * (rate.cache_write_1h_per_mtok or 0.0)
    ) / 1_000_000
    total = input_usd + output_usd + cache_read_usd + cache_write_usd
    if not math.isfinite(total) or total < 0:
        return None
    return CostEstimate(
        usd=total,
        provider=rate.provider,
        model_id=rate.model_id,
        source=rate.source,
        as_of=rate.as_of,
        input_usd=input_usd,
        output_usd=output_usd,
        cache_read_usd=cache_read_usd,
        cache_write_usd=cache_write_usd,
    )


def single_pricing_model(entry: dict) -> str | None:
    """Return one unambiguous raw model id from a stored run.

    A multi-model run without per-model token usage cannot be priced honestly
    from one aggregate token counter.
    """
    stage_models = [
        s.get("model")
        for s in (entry.get("stage_runs") or [])
        if isinstance(s, dict) and isinstance(s.get("model"), str) and s.get("model")
    ]
    if stage_models:
        unique = list(dict.fromkeys(stage_models))
        return unique[0] if len(unique) == 1 else None

    capture_raw = entry.get("capture")
    capture: dict = capture_raw if isinstance(capture_raw, dict) else {}
    seen = [m for m in (capture.get("models_seen") or []) if isinstance(m, str) and m]
    if seen:
        unique = list(dict.fromkeys(seen))
        return unique[0] if len(unique) == 1 else None

    for key in ("routing_selected_model", "execution_model"):
        value = entry.get(key)
        if isinstance(value, str) and value and value.lower() not in {"unknown", "not recorded"}:
            return value
    return None
