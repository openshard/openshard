from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from openshard.security.secret_scan import SecretScanResult, scrub_text_for_secrets

# ---------------------------------------------------------------------------
# Provider-neutral exceptions
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """Base error for all provider failures."""


class ProviderAuthError(ProviderError):
    """Raised when the API key is invalid or missing."""


class ProviderRateLimitError(ProviderError):
    """Raised when the API rate limit is exceeded."""


class PreSendSecretScanError(ProviderError):
    """Raised when model-bound text cannot be safely scanned before sending.

    Fail-closed signal: the provider call is aborted and no prompt content is
    sent. Messages never contain prompt content.
    """


# ---------------------------------------------------------------------------
# Shared data types
# ---------------------------------------------------------------------------

@dataclass
class ModelInfo:
    id: str
    name: str
    pricing: dict  # {"prompt": str, "completion": str, ...} — varies by provider/model
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_vision: bool = False
    supports_tools: bool = False


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


@dataclass
class ChatResponse:
    content: str
    model: str
    usage: UsageStats
    # Result of the pre-send secret scan applied to the outgoing prompt. None
    # when no secret-like values were found (clean path is byte-identical).
    presend_secret_scan: SecretScanResult | None = None
    # Why the provider stopped generating, as it reported it ("stop", "length",
    # ...); None when the provider did not say. "length" means the reply was
    # cut off at the output limit and may be incomplete.
    finish_reason: str | None = None


# ---------------------------------------------------------------------------
# Pre-send secret guard
# ---------------------------------------------------------------------------

# Far beyond any model context window, so realistic prompts always scan in
# full. Exceeding it is pathological and fails closed (see below). The cap only
# bounds worst-case regex CPU; it never truncates or sends an unscanned tail.
_PRESEND_MAX_CHARS = 5_000_000


def guard_prompt_before_send(
    prompt: str,
    *,
    source_label: str = "<model-prompt>",
    max_chars: int = _PRESEND_MAX_CHARS,
) -> tuple[str, SecretScanResult | None]:
    """Scan and redact a model-bound *prompt* before a provider call.

    Fails closed: if the prompt cannot be scanned in full, the call must be
    aborted rather than sending unscanned content.

    - Clean text -> ``(prompt, None)`` (byte-identical; no metadata side effects).
    - Secret-like values found -> ``(scrubbed_prompt, result)`` with raw values
      replaced in place.
    - Oversized (``result.omitted``) -> raise :class:`PreSendSecretScanError`
      (never send the omission placeholder to the model).
    - Underlying scrub raises (defensive; the primitive normally never raises)
      -> raise :class:`PreSendSecretScanError`.

    Exception messages never contain prompt content.
    """
    try:
        scrubbed, result = scrub_text_for_secrets(
            prompt, source_label=source_label, max_chars=max_chars
        )
    except Exception as exc:  # noqa: BLE001 — fail closed on any scan failure
        raise PreSendSecretScanError(
            "pre-send secret scan failed; provider call aborted"
        ) from exc
    if result.omitted:
        raise PreSendSecretScanError(
            "model-bound prompt exceeds the safe secret-scan limit; "
            "provider call aborted"
        )
    if not result.findings:
        return prompt, None
    return scrubbed, result


# ---------------------------------------------------------------------------
# Abstract base provider
# ---------------------------------------------------------------------------

class BaseProvider(ABC):
    """Minimal interface that every provider must implement."""

    @abstractmethod
    def list_models(self) -> list[ModelInfo]:
        """Return all models available from this provider."""

    @abstractmethod
    def execute(
        self, model: str, prompt: str, system: str | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """Send *prompt* to *model* and return a structured response.

        Implementations MUST call :func:`guard_prompt_before_send` on *prompt*
        before building the request payload, so secret-like values are redacted
        (and the call fails closed if scanning cannot complete). Attach the
        returned scan result to ``ChatResponse.presend_secret_scan``.
        """

    @abstractmethod
    def get_model_info(self, model_id: str) -> ModelInfo | None:
        """Return info for a single model, or None if not found."""
