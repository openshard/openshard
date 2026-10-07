from __future__ import annotations

from openshard.providers.base import (
    COST_SOURCE_LIST_RATE,
    BaseProvider,
    ChatResponse,
    ModelInfo,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    UsageStats,
    guard_prompt_before_send,
)
from openshard.providers.openrouter import compute_cost

try:
    import anthropic as _sdk
except ImportError:
    _sdk = None  # type: ignore[assignment]

_DEFAULT_MAX_TOKENS = 4096


def _normalize_model_id(model: str) -> str:
    """Convert OpenRouter-style ID to native Anthropic model ID.

    anthropic/claude-sonnet-4.6  ->  claude-sonnet-4-6
    claude-sonnet-4.6            ->  claude-sonnet-4-6  (already bare, still normalise dots)
    """
    if model.startswith("anthropic/"):
        model = model[len("anthropic/"):]
    return model.replace(".", "-")


class AnthropicProvider(BaseProvider):
    """Direct Anthropic API provider using the official SDK."""

    def __init__(self, api_key: str) -> None:
        if _sdk is None:
            raise ProviderError(
                "The 'anthropic' package is required to use the Anthropic provider.\n"
                "Install it with:  pip install anthropic"
            )
        if not api_key:
            raise ProviderAuthError(
                "ANTHROPIC_API_KEY is not set.\n"
                "Export it before running:\n\n"
                "  export ANTHROPIC_API_KEY=your_key_here\n\n"
                "Obtain a key from https://console.anthropic.com/settings/keys"
            )
        self._client = _sdk.Anthropic(api_key=api_key)

    # ------------------------------------------------------------------
    # BaseProvider interface
    # ------------------------------------------------------------------

    def list_models(self) -> list[ModelInfo]:
        """Return all models available from the Anthropic API."""
        try:
            page = self._client.models.list()
            return [
                ModelInfo(
                    id=m.id,
                    name=getattr(m, "display_name", m.id),
                    pricing={},
                )
                for m in page.data
            ]
        except _sdk.AuthenticationError as exc:
            raise ProviderAuthError(str(exc)) from exc
        except _sdk.RateLimitError as exc:
            raise ProviderRateLimitError(str(exc)) from exc
        except _sdk.APIError as exc:
            raise ProviderError(str(exc)) from exc

    def execute(
        self, model: str, prompt: str, system: str | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """Send *prompt* to *model* via the Anthropic messages API."""
        # Pre-send secret scan: redact secret-like values (fail closed) before
        # any request payload is built.
        prompt, _presend_scan = guard_prompt_before_send(prompt)
        native_model = _normalize_model_id(model)
        kwargs: dict = {
            "model": native_model,
            "max_tokens": max_tokens if max_tokens is not None else _DEFAULT_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        try:
            response = self._client.messages.create(**kwargs)
        except _sdk.AuthenticationError as exc:
            raise ProviderAuthError(str(exc)) from exc
        except _sdk.RateLimitError as exc:
            raise ProviderRateLimitError(str(exc)) from exc
        except _sdk.APIError as exc:
            raise ProviderError(str(exc)) from exc

        content = response.content[0].text if response.content else ""
        resolved_model = response.model or native_model
        usage = UsageStats(
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
        )
        # OpenRouter-style model ID for cost lookup (strip date suffix if present)
        or_model = f"anthropic/{model}" if not model.startswith("anthropic/") else model
        usage.estimated_cost = compute_cost(
            or_model, usage.prompt_tokens, usage.completion_tokens
        )
        if usage.estimated_cost is not None:
            usage.cost_source = COST_SOURCE_LIST_RATE  # OpenShard's arithmetic, not a bill
        return ChatResponse(
            content=content, model=resolved_model, usage=usage,
            presend_secret_scan=_presend_scan,
        )

    def get_model_info(self, model_id: str) -> ModelInfo | None:
        """Return info for *model_id*, or None if not found."""
        native = _normalize_model_id(model_id)
        for m in self.list_models():
            if m.id == native or m.id == model_id:
                return m
        return None
