from __future__ import annotations

import httpx

# Re-export shared data types so existing imports from this module keep working.
from openshard.providers.base import (
    COST_SOURCE_LIST_RATE,
    COST_SOURCE_PROVIDER,
    BaseProvider,
    ChatResponse,
    ModelInfo,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    UsageStats,
    guard_prompt_before_send,
)
from openshard.providers.cache import load_cache

__all__ = [
    "OpenRouterClient",
    "OpenRouterError",
    "AuthError",
    "RateLimitError",
    "ModelInfo",
    "UsageStats",
    "ChatResponse",
    "MODEL_PRICING",
    "compute_cost",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://openrouter.ai/api/v1"
_TIMEOUT = 60.0  # seconds


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OpenRouterError(ProviderError):
    """Base error for all OpenRouter failures."""


class AuthError(ProviderAuthError, OpenRouterError):
    """Raised when the API key is invalid or missing (HTTP 401/403)."""


class RateLimitError(ProviderRateLimitError, OpenRouterError):
    """Raised when the API rate limit is exceeded (HTTP 429)."""


# ---------------------------------------------------------------------------
# Pricing snapshot
# ---------------------------------------------------------------------------

# Dollars per million tokens — (prompt, completion).
# Current-family values below are sourced from the checked-in OpenRouter snapshot dated 2026-09-25.
# Older values marked ~est are compatibility fallbacks.
#
# NOTE: a few legacy IDs below (anthropic/claude-opus-4.6,
# anthropic/claude-haiku-4.5-20251001, openai/gpt-4o, openai/gpt-4o-mini) are
# intentionally retained in this snapshot even though they are not in the model
# registry. This is a deliberate, tracked exception:
# this pricing table is a separate snapshot, and gpt-4o* still back the OpenAI
# provider default. Consolidation onto the registry is deferred to
# feat/registry-metadata-v2. The drift test mirrors this via the allowlist
# LEGACY_PRICING_IDS_ALLOWED_UNTIL_METADATA_V2, so any new untracked drift
# fails the test. Do not add further unregistered IDs here.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    # Verified 2026-10-03 against https://platform.claude.com/docs/en/about-claude/pricing
    # and the 2026-09-25 OpenRouter snapshot (they agree).
    "anthropic/claude-haiku-4.5":           (1.00,   5.00),
    "anthropic/claude-haiku-4.5-20251001":  (1.00,   5.00),
    "anthropic/claude-sonnet-4.6":          (3.00,  15.00),
    "anthropic/claude-opus-4.6":            (5.00,  25.00),
    "anthropic/claude-opus-4.7":            (5.00,  25.00),
    "anthropic/claude-opus-4.8":            (5.00,  25.00),
    "anthropic/claude-opus-5.5":            (4.00,  20.00),
    "anthropic/claude-fable-5.1":           (10.00, 50.00),
    # Main worker
    "z-ai/glm-5.1":                         (0.96,   3.03),   # 2026-09-25 OpenRouter snapshot (was a 0.10/0.10 ~est, 10-30x low)
    # Cheap coding
    "deepseek/deepseek-v4.1-flash":        (0.13,   0.52),   # OpenRouter headline rate 2026-10-07
    "deepseek/deepseek-v4-flash":          (0.10,   0.28),   # ~est (deprecated 0423 snapshot)
    "deepseek/deepseek-v4-pro":            (0.69,   1.39),   # 2026-09-25 OpenRouter snapshot (was a 0.27/1.10 ~est)
    # Visual / multimodal
    "moonshotai/kimi-k2.5":                 (0.45,   2.20),
    # Long-horizon
    "minimax/m2.7":                         (0.30,   1.20),   # minimax/minimax-m2.7 in the 2026-09-25 OpenRouter snapshot (retired id kept for old Receipts)
    # OpenAI
    "openai/gpt-4o":                        (2.50,  10.00),
    "openai/gpt-4o-mini":                   (0.15,   0.60),
    # Current OpenAI families — verified from 2026-09-25 provider snapshot
    "openai/gpt-5.6-luna":                  (0.20,   1.20),
    "openai/gpt-5.6-sol":                   (2.00,  10.00),
    "openai/gpt-5.6-terra":                 (2.00,  12.00),
    "openai/gpt-6-astra":                  (10.00,  50.00),
    "openai/gpt-6-luna":                    (0.10,   0.50),
    "openai/gpt-6-sol":                     (2.00,  10.00),
    # Compatibility pricing retained for historical Receipts/configs
    "openai/gpt-5.5":                       (5.00,  30.00),
    # Tiny helpers
    "openai/gpt-5.4-nano":                  (0.10,   0.40),   # ~est
}


def _cost_from_cache(
    model: str, prompt_tokens: int, completion_tokens: int
) -> float | None:
    """Return cost using per-token pricing from the local OpenRouter model cache."""
    cache = load_cache()
    if not cache:
        return None
    for entry in cache.get("models", {}).get("openrouter", []):
        if entry.get("id") == model:
            pricing = entry.get("pricing") or {}
            try:
                p = float(pricing["prompt"])
                c = float(pricing["completion"])
            except (KeyError, TypeError, ValueError):
                return None
            return prompt_tokens * p + completion_tokens * c
    return None


def compute_cost(
    model: str, prompt_tokens: int, completion_tokens: int
) -> float | None:
    """Return estimated cost in USD from token counts, or None if model unknown."""
    pricing = MODEL_PRICING.get(model)
    if pricing is not None:
        p_per_m, c_per_m = pricing
        return (prompt_tokens * p_per_m + completion_tokens * c_per_m) / 1_000_000
    return _cost_from_cache(model, prompt_tokens, completion_tokens)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class OpenRouterClient(BaseProvider):
    """Thin HTTP client for the OpenRouter API."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._client = httpx.Client(
            base_url=_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # BaseProvider interface
    # ------------------------------------------------------------------

    def list_models(self) -> list[ModelInfo]:
        """Return all models available on OpenRouter."""
        data = self._get("/models")
        return [
            ModelInfo(
                id=m.get("id", ""),
                name=m.get("name", m.get("id", "")),
                pricing=m.get("pricing", {}),
                context_window=m.get("context_length"),
                max_output_tokens=(m.get("top_provider") or {}).get("max_completion_tokens"),
                supports_vision="image" in (
                    (m.get("architecture") or {}).get("modality") or ""
                ),
                supports_tools=bool(
                    m.get("supported_parameters")
                    and "tools" in m["supported_parameters"]
                ),
            )
            for m in data.get("data", [])
        ]

    def execute(
        self, model: str, prompt: str, system: str | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """Send *prompt* to *model* and return a structured response."""
        return self.send_request(model, prompt, system, max_tokens=max_tokens)

    def get_model_info(self, model_id: str) -> ModelInfo | None:
        """Return info for *model_id*, or None if not listed."""
        for m in self.list_models():
            if m.id == model_id:
                return m
        return None

    # ------------------------------------------------------------------
    # Legacy method — kept for internal callers; prefer execute()
    # ------------------------------------------------------------------

    def send_request(
        self, model: str, prompt: str, system: str | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """Send *prompt* to *model* and return a structured response.

        *system* is an optional system-role message prepended to the conversation.
        *max_tokens* caps the completion length sent to the API.
        """
        # Pre-send secret scan: redact secret-like values (fail closed) before
        # any request payload is built. This is the single send point for the
        # OpenRouter client (``execute`` delegates here), so guarding here
        # covers every caller.
        prompt, _presend_scan = guard_prompt_before_send(prompt)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict = {
            "model": model,
            "messages": messages,
            # Usage accounting: ask OpenRouter to return its own cost for this
            # call so the Receipt can record a provider-reported figure rather
            # than OpenShard's list-rate estimate whenever the provider says.
            "usage": {"include": True},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        data = self._post("/chat/completions", payload)

        choices = data.get("choices", [])
        if not choices:
            raise OpenRouterError("API returned no choices in response")

        # Reasoning-only exhaustion may return content: null.
        content = (choices[0].get("message") or {}).get("content") or ""
        usage_raw = data.get("usage") or {}
        raw_cost = usage_raw.get("cost")
        resolved_model = data.get("model", model)
        estimated_cost = (
            float(raw_cost) if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) else None
        )
        details = usage_raw.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        usage = UsageStats(
            prompt_tokens=usage_raw.get("prompt_tokens", 0),
            completion_tokens=usage_raw.get("completion_tokens", 0),
            total_tokens=usage_raw.get("total_tokens", 0),
            estimated_cost=estimated_cost,
            cost_source=COST_SOURCE_PROVIDER if estimated_cost is not None else None,
            cache_read_tokens=cached if isinstance(cached, int) and not isinstance(cached, bool) else None,
        )
        # Fallback: compute cost from token counts when provider omits it. This
        # is OpenShard's own list-rate arithmetic and is labelled as such.
        if usage.estimated_cost is None and usage.total_tokens > 0:
            usage.estimated_cost = compute_cost(
                resolved_model, usage.prompt_tokens, usage.completion_tokens
            )
            if usage.estimated_cost is not None:
                usage.cost_source = COST_SOURCE_LIST_RATE
        finish = choices[0].get("finish_reason") or choices[0].get("native_finish_reason")
        return ChatResponse(
            content=content, model=resolved_model, usage=usage,
            presend_secret_scan=_presend_scan,
            finish_reason=finish if isinstance(finish, str) and finish else None,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str) -> dict:
        try:
            response = self._client.get(path)
        except httpx.RequestError as exc:
            raise OpenRouterError(f"Network error: {exc}") from exc
        return self._parse(response)

    def _post(self, path: str, payload: dict) -> dict:
        try:
            response = self._client.post(path, json=payload)
        except httpx.RequestError as exc:
            raise OpenRouterError(f"Network error: {exc}") from exc
        return self._parse(response)

    def _parse(self, response: httpx.Response) -> dict:
        status = response.status_code
        if status in (401, 403):
            raise AuthError(f"Authentication failed (HTTP {status})")
        if status == 429:
            raise RateLimitError("Rate limit exceeded — try again later")
        if status >= 400:
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except Exception:
                detail = response.text
            raise OpenRouterError(f"API error (HTTP {status}): {detail}")
        return response.json()
