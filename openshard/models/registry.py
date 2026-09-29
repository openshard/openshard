from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# ModelEntry — curated record for a single model.
# ---------------------------------------------------------------------------
# Field categories (see docs/architecture/routing.md):
#
# * model facts (curated approximations; provider discovery overrides them
#   for display): context_length, modalities, supports_*.
# * policy metadata (authoritative): lifecycle (curation stage), and the
#   access/model policy that lives outside this module.
# * legacy / advisory metadata (NOT routing authority): tier, roles,
#   latency_class, experimental, cost_class. They remain for display, for
#   old Receipts and configs, and as an ordering hint of last resort after
#   facts, promotion state and observed evidence. No routing rule may
#   require a particular tier or role string. See LEGACY_ADVISORY_FIELDS.
#
# Exact token pricing lives in the discovered catalog (OpenRouter) and, as a
# static fallback, in openshard/providers/openrouter.py. Do not hardcode
# volatile prices here.
#
# Metadata v2 adds provenance and forward-looking routing fields (source,
# risk_level, recommended_for, avoid_for) plus a StaticPricing placeholder.
# These are additive with safe defaults. Pricing stays deferred: the pricing
# fields remain empty/unknown in v2 and MODEL_PRICING in
# openshard/providers/openrouter.py is still the authoritative price source.
# ---------------------------------------------------------------------------

# Metadata schema version carried by every ModelEntry.
METADATA_VERSION = "2"

# Curated fields that describe a model's assumed quality or role. They are
# compatibility/advisory data: routing may use them only as a final ordering
# hint, never as a filter, and Receipts may still show them.
LEGACY_ADVISORY_FIELDS: tuple[str, ...] = (
    "tier", "roles", "latency_class", "experimental", "cost_class",
)

# Valid values for the provenance/risk/pricing enum-style fields. Tests use
# these as the single source of truth for accepted tokens.
SOURCE_VALUES = frozenset({"curated_static_registry", "unknown"})
RISK_LEVELS = frozenset({"low", "medium", "high", "unknown"})
PRICING_SOURCES = frozenset(
    {"openrouter_static_snapshot", "provider_static_snapshot", "unknown"}
)

# ---------------------------------------------------------------------------
# Lifecycle tags v1 (additive metadata).
# ---------------------------------------------------------------------------
# Lifecycle separates catalog presence from default routing eligibility. The
# registry knowing a model exists is independent of how trusted/useful it is
# and whether it should be considered for default routing later. Routing is NOT
# wired to these values in this branch; see is_routing_default_eligible().
#
#   active_default     safe for default routing
#   active_specialist  production-grade, specialist tasks only
#   fallback           fallback / alias only
#   open_weight        open-weight / local candidate
#   experimental       experimental / not yet trusted
#   watchlist          tracked, not yet evaluated
#   deprecated         kept for old Shards / history only
LIFECYCLE_VALUES = frozenset(
    {
        "active_default",
        "active_specialist",
        "fallback",
        "open_weight",
        "experimental",
        "watchlist",
        "deprecated",
    }
)

# Lifecycles that may be considered for DEFAULT routing later. Single source of
# truth for is_routing_default_eligible(). routing/engine.py is unchanged: it
# still hardcodes its model-ID constants and does not read the registry.
ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES = frozenset({"active_default"})


@dataclass(frozen=True)
class StaticPricing:
    """Static, snapshot pricing placeholder for a model.

    Values stay None/"unknown" in metadata v2. Authoritative runtime pricing
    remains MODEL_PRICING in openshard/providers/openrouter.py. These fields are
    schema scaffolding for a future branch that does live pricing research. Do
    not populate them from guesses.
    """

    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    # openrouter_static_snapshot | provider_static_snapshot | unknown
    pricing_source: str = "unknown"
    # ISO date string (e.g. "2026-04-01") when last verified, else None.
    last_verified_at: str | None = None


@dataclass(frozen=True)
class ModelEntry:
    id: str
    display_name: str
    provider: str
    # LEGACY / ADVISORY (not routing authority): a hand-assigned quality label
    #   cheap | mid | strong | frontier | experimental | code_specialist |
    #   small_coder | small | tiny | free_experimental | long_horizon |
    #   value_worker | open_weight | fast_reasoning. Kept for display, old
    #   Receipts and configs; routing filters on facts and lifecycle instead.
    tier: str
    # LEGACY / ADVISORY: hand-assigned role hints. An ordering hint of last
    # resort in routing, never a requirement.
    roles: tuple[str, ...] = field(default_factory=tuple)
    experimental: bool = False

    # Capability profile — best-known static values, not live-fetched.
    context_length: int | None = None
    input_modalities: tuple[str, ...] = ("text",)
    output_modalities: tuple[str, ...] = ("text",)

    supports_tools: bool = False
    supports_structured_outputs: bool = False
    supports_reasoning: bool = False
    supports_multimodal: bool = False

    # LEGACY / ADVISORY: fast | normal | slow | unknown. Never measured.
    latency_class: str = "unknown"
    # free | tiny | cheap | mid | expensive | unknown. A curated proxy; the
    # catalog derives the live price band from provider pricing.
    cost_class: str = "unknown"

    notes: str = ""

    # ---- Metadata v2 (additive, safe defaults) ----------------------------
    # Schema version of this entry's metadata.
    metadata_version: str = METADATA_VERSION
    # Provenance of this entry: curated_static_registry | unknown
    source: str = "curated_static_registry"
    # Maturity/trust of the model for routing gates: low | medium | high |
    # unknown. Left unknown until live research; do not invent claims.
    risk_level: str = "unknown"
    # Forward-looking routing hints. Empty until populated by a later branch.
    recommended_for: tuple[str, ...] = field(default_factory=tuple)
    avoid_for: tuple[str, ...] = field(default_factory=tuple)
    # Static pricing placeholder. Stays empty/unknown in v2; see StaticPricing.
    pricing: StaticPricing = field(default_factory=StaticPricing)

    # ---- Lifecycle tags v1 (additive) -------------------------------------
    # Lifecycle/trust stage. Distinguishes catalog presence from default
    # routing eligibility. Routing is NOT wired to this yet; default
    # eligibility is derived via is_routing_default_eligible(). Must be one of
    # LIFECYCLE_VALUES. Set explicitly per entry so each value is auditable.
    lifecycle: str = "active_default"


# ---------------------------------------------------------------------------
# Registry — curated models in a single list.
# ---------------------------------------------------------------------------
# Add new entries here. Do not scatter model IDs across routing files. The
# registry is not the only source of models: provider discovery
# (openshard/models/catalog.py) adds every model the provider lists, with
# lifecycle "discovered". Curation here is what promotes a model into the
# public default pool (active_default / active_specialist); see
# openshard/models/promotion.py for the states in between.
# ---------------------------------------------------------------------------

_REGISTRY: list[ModelEntry] = [
    # ------------------------------------------------------------------
    # Existing routing models — registered for metadata completeness.
    # Routing defaults in engine.py are unchanged.
    # ------------------------------------------------------------------
    ModelEntry(
        id="deepseek/deepseek-v4-flash",
        lifecycle="active_default",
        display_name="DeepSeek: V4 Flash",
        provider="DeepSeek",
        tier="cheap",
        roles=("cheap_control", "boilerplate"),
        experimental=False,
        context_length=128_000,
        supports_tools=True,
        supports_structured_outputs=True,
        latency_class="fast",
        cost_class="cheap",
    ),
    ModelEntry(
        id="z-ai/glm-5.1",
        lifecycle="active_default",
        display_name="Z-AI: GLM 5.1",
        provider="Z-AI",
        tier="mid",
        roles=("routine_engineering", "standard_coding"),
        experimental=False,
        context_length=128_000,
        supports_tools=True,
        supports_structured_outputs=True,
        latency_class="normal",
        cost_class="mid",
    ),
    ModelEntry(
        id="anthropic/claude-sonnet-4.6",
        lifecycle="active_default",
        display_name="Anthropic: Claude Sonnet 4.6",
        provider="Anthropic",
        tier="strong",
        roles=("planner", "reviewer", "frontier_alternative"),
        experimental=False,
        context_length=200_000,
        input_modalities=("text", "image"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="normal",
        cost_class="expensive",
    ),
    ModelEntry(
        id="anthropic/claude-opus-4.7",
        lifecycle="active_specialist",
        display_name="Anthropic: Claude Opus 4.7",
        provider="Anthropic",
        tier="frontier",
        roles=("escalation", "frontier_alternative"),
        experimental=False,
        context_length=200_000,
        input_modalities=("text", "image"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="slow",
        cost_class="expensive",
    ),
    ModelEntry(
        id="anthropic/claude-haiku-4.5",
        lifecycle="active_default",
        display_name="Anthropic: Claude Haiku 4.5",
        provider="Anthropic",
        tier="cheap",
        roles=("cheap_control", "summariser", "light_review", "claude_family_fast"),
        experimental=False,
        context_length=200_000,
        input_modalities=("text", "image"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=False,
        supports_multimodal=True,
        latency_class="fast",
        cost_class="cheap",
    ),
    ModelEntry(
        id="moonshotai/kimi-k2.5",
        lifecycle="active_specialist",
        display_name="Moonshot AI: Kimi K2.5",
        provider="Moonshot AI",
        tier="mid",
        roles=("visual", "multimodal"),
        experimental=False,
        context_length=131_072,
        input_modalities=("text", "image"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_multimodal=True,
        latency_class="normal",
        cost_class="mid",
    ),
    ModelEntry(
        id="minimax/m2.7",
        lifecycle="deprecated",
        display_name="MiniMax: M2.7",
        provider="MiniMax",
        tier="mid",
        roles=("complex", "long_context"),
        experimental=False,
        context_length=1_000_000,
        supports_tools=True,
        supports_structured_outputs=True,
        latency_class="normal",
        cost_class="mid",
        notes=(
            "Retired 2026-09-28: this id was never listed by OpenRouter (the live id is "
            "minimax/minimax-m2.7, curated as watchlist). Kept so old Receipts stay readable; "
            "never a fresh-run default."
        ),
    ),

    # ------------------------------------------------------------------
    # Core new models — non-experimental.
    # ------------------------------------------------------------------
    ModelEntry(
        id="google/gemini-3.1-flash-lite",
        lifecycle="active_default",
        display_name="Google: Gemini 3.1 Flash Lite",
        provider="Google",
        tier="cheap",
        roles=("cheap_control", "summariser", "session_inference", "feedback_inference", "light_review"),
        experimental=False,
        context_length=1_048_576,
        supports_tools=True,
        supports_structured_outputs=True,
        supports_multimodal=False,
        latency_class="fast",
        cost_class="cheap",
    ),
    ModelEntry(
        id="google/gemini-3.5-flash",
        lifecycle="active_default",
        display_name="Google: Gemini 3.5 Flash",
        provider="Google",
        tier="mid",
        roles=("routine_engineering", "planner", "reviewer", "agentic_mid"),
        experimental=False,
        context_length=1_048_576,
        input_modalities=("text", "image"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_multimodal=True,
        latency_class="fast",
        cost_class="cheap",
    ),
    ModelEntry(
        id="qwen/qwen3.7-max",
        lifecycle="active_default",
        display_name="Qwen: Qwen3.7 Max",
        provider="Qwen",
        tier="strong",
        roles=("planner", "reviewer", "routine_engineering", "coding", "productivity"),
        experimental=False,
        context_length=131_072,
        supports_tools=True,
        supports_structured_outputs=True,
        latency_class="normal",
        cost_class="mid",
    ),
    ModelEntry(
        id="x-ai/grok-4.3",
        lifecycle="active_default",
        display_name="xAI: Grok 4.3",
        provider="xAI",
        tier="strong",
        roles=("planner", "reviewer", "reasoning", "frontier_alternative"),
        experimental=False,
        context_length=131_072,
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        latency_class="normal",
        cost_class="expensive",
    ),
    ModelEntry(
        id="~anthropic/claude-haiku-latest",
        lifecycle="fallback",
        display_name="Anthropic Claude Haiku Latest",
        provider="Anthropic",
        tier="cheap",
        roles=("cheap_control", "summariser", "light_review", "claude_family_fast"),
        experimental=False,
        context_length=200_000,
        input_modalities=("text", "image"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_multimodal=True,
        latency_class="fast",
        cost_class="cheap",
    ),

    # ------------------------------------------------------------------
    # Curated roster update v1 - non-experimental.
    # Factual fields from OpenRouter metadata. Pricing stays deferred;
    # risk_level stays unknown until live research.
    #
    # input_modalities is intentionally limited to ("text", "image") for
    # consistency with the rest of the registry. OpenRouter reports
    # additional input types not captured here yet: Opus 4.8 and Opus 4.8
    # Fast also accept file input, and MiniMax M3 also accepts video input.
    # The modality field does not fully capture every OpenRouter input type;
    # file/video tokens are deferred to a future modality-schema cleanup
    # branch. supports_multimodal=True still records the broad capability.
    # ------------------------------------------------------------------
    ModelEntry(
        id="anthropic/claude-opus-4.8",
        lifecycle="active_specialist",
        display_name="Anthropic: Claude Opus 4.8",
        provider="Anthropic",
        tier="frontier",
        roles=("planner", "reviewer", "escalation", "frontier_alternative"),
        experimental=False,
        context_length=1_000_000,
        input_modalities=("text", "image"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="slow",
        cost_class="expensive",
    ),
    ModelEntry(
        id="anthropic/claude-opus-4.8-fast",
        lifecycle="watchlist",
        display_name="Anthropic: Claude Opus 4.8 Fast",
        provider="Anthropic",
        tier="frontier",
        roles=("planner", "reviewer", "escalation", "frontier_alternative", "claude_family_fast"),
        experimental=False,
        context_length=1_000_000,
        input_modalities=("text", "image"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="normal",
        cost_class="expensive",
        notes=(
            "Moved to watchlist 2026-09-28: not listed by OpenRouter as of 2026-09-25, so the "
            "id cannot be verified against a provider; not a fresh-run default until it is."
        ),
    ),
    # ------------------------------------------------------------------
    # Claude Fable 5 — generally available (2026-06).
    # active_specialist: production-grade but not default-routable.
    # roles intentionally excludes "escalation" so the resolver's
    # escalate path continues to select Opus 4.8 (hint-score wins over
    # alphabetical tie-break; "fable" < "opus" would otherwise flip it).
    # ------------------------------------------------------------------
    ModelEntry(
        id="anthropic/claude-fable-5",
        lifecycle="active_specialist",
        display_name="Anthropic: Claude Fable 5",
        provider="Anthropic",
        tier="frontier",
        roles=("planner", "reviewer", "frontier_alternative"),
        experimental=False,
        context_length=1_000_000,
        input_modalities=("text", "image"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=False,
        supports_multimodal=True,
        latency_class="slow",
        cost_class="expensive",
        notes=(
            "Adaptive thinking always on; does not expose extended thinking as a "
            "user-invokable feature. Max output 128k tokens. Safeguards may fall "
            "back to Opus 4.8."
        ),
    ),
    # ------------------------------------------------------------------
    # Claude Mythos 5 — limited availability / Project Glasswing (2026-06).
    # watchlist: access-restricted; never queried by any resolver path.
    # ------------------------------------------------------------------
    ModelEntry(
        id="anthropic/claude-mythos-5",
        lifecycle="watchlist",
        display_name="Anthropic: Claude Mythos 5",
        provider="Anthropic",
        tier="frontier",
        roles=(),
        experimental=False,
        context_length=1_000_000,
        input_modalities=("text", "image"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=False,
        supports_multimodal=True,
        latency_class="unknown",
        cost_class="expensive",
        notes=(
            "Limited availability — Project Glasswing, approved customers only. "
            "Max output 128k tokens. Mythos-class traffic has 30-day retention. "
            "Not evaluated for routing."
        ),
    ),
    ModelEntry(
        id="minimax/minimax-m3",
        lifecycle="active_default",
        display_name="MiniMax: M3",
        provider="MiniMax",
        tier="strong",
        roles=("routine_engineering", "coding", "long_context", "value_worker", "multimodal"),
        experimental=False,
        context_length=1_048_576,
        input_modalities=("text", "image"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="normal",
        cost_class="cheap",
    ),
    ModelEntry(
        id="qwen/qwen3.7-plus",
        lifecycle="active_default",
        display_name="Qwen: Qwen3.7 Plus",
        provider="Qwen",
        tier="mid",
        roles=("routine_engineering", "planner", "coding", "value_worker"),
        experimental=False,
        context_length=1_000_000,
        input_modalities=("text", "image"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="normal",
        cost_class="cheap",
    ),
    ModelEntry(
        id="x-ai/grok-build-0.1",
        lifecycle="experimental",
        display_name="xAI: Grok Build 0.1",
        provider="xAI",
        tier="experimental",
        roles=("coding_agent", "agentic_engineering", "experimental_coding"),
        experimental=True,
        context_length=131_072,
        supports_tools=True,
        supports_structured_outputs=True,
        latency_class="normal",
        cost_class="mid",
    ),

    # ------------------------------------------------------------------
    # Experimental / specialist models.
    # ------------------------------------------------------------------
    ModelEntry(
        id="qwen/qwen3.6-flash",
        lifecycle="experimental",
        display_name="Qwen: Qwen3.6 Flash",
        provider="Qwen",
        tier="cheap",
        roles=("cheap_control", "summariser", "light_review", "routine_engineering"),
        experimental=True,
        context_length=131_072,
        supports_tools=True,
        supports_structured_outputs=True,
        latency_class="fast",
        cost_class="cheap",
    ),
    ModelEntry(
        id="qwen/qwen3-coder-30b-a3b-instruct",
        lifecycle="experimental",
        display_name="Qwen: Qwen3 Coder 30B A3B Instruct",
        provider="Qwen",
        tier="small_coder",
        roles=("code_generation", "code_review", "repo_understanding", "coding_agent"),
        experimental=True,
        context_length=131_072,
        supports_tools=True,
        supports_structured_outputs=True,
        latency_class="normal",
        cost_class="cheap",
    ),
    ModelEntry(
        id="mistralai/codestral-2508",
        lifecycle="active_specialist",
        display_name="Mistral: Codestral 2508",
        provider="Mistral",
        tier="code_specialist",
        roles=("code_generation", "code_correction", "test_generation", "small_coding_tasks"),
        experimental=True,
        context_length=32_768,
        supports_tools=True,
        supports_structured_outputs=True,
        latency_class="fast",
        cost_class="cheap",
    ),
    ModelEntry(
        id="google/gemma-4-26b-a4b-it",
        lifecycle="open_weight",
        display_name="Google: Gemma 4 26B A4B",
        provider="Google",
        tier="small",
        roles=("cheap_control", "summariser", "structured_metadata", "light_review"),
        experimental=True,
        context_length=131_072,
        supports_tools=True,
        supports_structured_outputs=True,
        latency_class="normal",
        cost_class="cheap",
    ),
    ModelEntry(
        id="google/gemma-4-31b-it",
        lifecycle="open_weight",
        display_name="Google: Gemma 4 31B",
        provider="Google",
        tier="small",
        roles=("cheap_control", "local_candidate", "summariser", "baseline_small_model"),
        experimental=True,
        context_length=131_072,
        supports_tools=True,
        supports_structured_outputs=True,
        latency_class="normal",
        cost_class="cheap",
    ),
    ModelEntry(
        id="ibm-granite/granite-4.1-8b",
        lifecycle="watchlist",
        display_name="IBM: Granite 4.1 8B",
        provider="IBM",
        tier="tiny",
        roles=("metadata_extraction", "structured_output", "labels", "cheap_control"),
        experimental=True,
        context_length=8_192,
        supports_tools=True,
        supports_structured_outputs=True,
        latency_class="fast",
        cost_class="tiny",
    ),
    ModelEntry(
        id="stepfun/step-3.5-flash",
        lifecycle="experimental",
        display_name="StepFun: Step 3.5 Flash",
        provider="StepFun",
        tier="experimental",
        roles=("reasoning", "coding", "agentic_mid", "cheap_reasoning"),
        experimental=True,
        context_length=32_768,
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        latency_class="fast",
        cost_class="cheap",
    ),
    ModelEntry(
        id="poolside/laguna-xs.2:free",
        lifecycle="experimental",
        display_name="Poolside: Laguna XS.2 (free)",
        provider="Poolside",
        tier="free_experimental",
        roles=("coding_agent", "benchmark_only", "experimental_coding"),
        experimental=True,
        context_length=None,
        supports_tools=False,
        supports_structured_outputs=False,
        latency_class="unknown",
        cost_class="free",
    ),
    ModelEntry(
        id="poolside/laguna-m.1:free",
        lifecycle="experimental",
        display_name="Poolside: Laguna M.1 (free)",
        provider="Poolside",
        tier="free_experimental",
        roles=("coding_agent", "benchmark_only", "experimental_coding"),
        experimental=True,
        context_length=None,
        supports_tools=False,
        supports_structured_outputs=False,
        latency_class="unknown",
        cost_class="free",
    ),

    # ------------------------------------------------------------------
    # OpenAI current family — metadata verified against the checked-in
    # OpenRouter snapshot dated 2026-09-25.
    #
    # GPT-5.6 Sol is the validated frontier OpenAI lane. Luna is a stable,
    # low-cost/fast lane. GPT-5.6 Terra and the GPT-6 family remain provider-
    # discovered catalog entries rather than curated registry entries until
    # evaluation promotes them. Catalog presence alone must never make a fresh
    # model a production default.
    # ------------------------------------------------------------------
    ModelEntry(
        id="openai/gpt-5.6-luna",
        lifecycle="active_default",
        display_name="OpenAI: GPT-5.6 Luna",
        provider="OpenAI",
        tier="cheap",
        roles=("cheap_control", "fast_chat", "summariser", "routine_engineering"),
        experimental=False,
        context_length=1_050_000,
        input_modalities=("text", "image", "file"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="fast",
        cost_class="cheap",
        notes="Current low-cost OpenAI lane; listed by OpenRouter in the 2026-09-25 snapshot.",
    ),
    ModelEntry(
        id="openai/gpt-5.6-sol",
        lifecycle="active_specialist",
        display_name="OpenAI: GPT-5.6 Sol",
        provider="OpenAI",
        tier="frontier",
        roles=("escalation", "planner", "reviewer", "reasoning", "high_risk", "coding"),
        experimental=False,
        context_length=1_050_000,
        input_modalities=("text", "image", "file"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="normal",
        cost_class="expensive",
        notes="Current validated OpenAI frontier lane; listed by OpenRouter in the 2026-09-25 snapshot.",
    ),
    # GPT-5.5 remains readable and explicitly selectable for compatibility,
    # but it no longer participates in fresh default/specialist routing.
    ModelEntry(
        id="openai/gpt-5.5",
        lifecycle="fallback",
        display_name="OpenAI: GPT-5.5",
        provider="OpenAI",
        tier="frontier",
        roles=("planner", "reviewer", "reasoning", "coding"),
        experimental=False,
        context_length=1_050_000,
        input_modalities=("text", "image", "file"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="normal",
        cost_class="expensive",
        notes="Compatibility model retained for old Receipts/configs; superseded by GPT-5.6.",
    ),
    ModelEntry(
        id="openai/gpt-5.5-pro",
        lifecycle="fallback",
        display_name="OpenAI: GPT-5.5 Pro",
        provider="OpenAI",
        tier="frontier",
        roles=("deep_review", "high_risk", "reasoning", "final_review"),
        experimental=False,
        context_length=1_050_000,
        input_modalities=("text", "image", "file"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="slow",
        cost_class="expensive",
        notes="Compatibility model retained for old Receipts/configs; superseded by GPT-5.6.",
    ),
    ModelEntry(
        id="openai/gpt-5.4",
        lifecycle="active_specialist",
        display_name="OpenAI: GPT-5.4",
        provider="OpenAI",
        tier="strong",
        roles=("planner", "reviewer", "coding", "high_context", "routine_engineering"),
        experimental=False,
        context_length=1_050_000,
        input_modalities=("text", "image", "file"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="normal",
        cost_class="mid",
        notes="Strong high-context model for large repo/spec digestion and planning.",
    ),
    ModelEntry(
        id="openai/gpt-5.4-pro",
        lifecycle="active_specialist",
        display_name="OpenAI: GPT-5.4 Pro",
        provider="OpenAI",
        tier="frontier",
        roles=("escalation", "deep_review", "high_risk", "reasoning", "final_review"),
        experimental=False,
        context_length=1_050_000,
        input_modalities=("text", "image", "file"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="slow",
        cost_class="expensive",
        notes="Pro OpenAI lane for high-stakes reasoning and review.",
    ),

    # ------------------------------------------------------------------
    # OpenAI Efficient / Small — non-experimental.
    # ------------------------------------------------------------------
    ModelEntry(
        id="openai/gpt-5.4-mini",
        lifecycle="active_default",
        display_name="OpenAI: GPT-5.4 Mini",
        provider="OpenAI",
        tier="mid",
        roles=("value_worker", "routine_engineering", "coding", "test_generation", "docs", "lightweight_review"),
        experimental=False,
        context_length=400_000,
        input_modalities=("text", "image", "file"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="fast",
        cost_class="mid",
        notes="Efficient GPT-5.4 family model for routine engineering and high-throughput workloads.",
    ),
    ModelEntry(
        id="openai/gpt-5.4-nano",
        lifecycle="active_default",
        display_name="OpenAI: GPT-5.4 Nano",
        provider="OpenAI",
        tier="small",
        roles=("low_cost_control", "cheap_control", "lightweight_review", "summariser", "metadata_extraction", "fast_chat"),
        experimental=False,
        context_length=400_000,
        input_modalities=("text", "image", "file"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="fast",
        cost_class="cheap",
        notes="Lightweight GPT-5.4 family model for fast, low-cost control and metadata work.",
    ),
    ModelEntry(
        id="openai/gpt-5-mini",
        lifecycle="active_default",
        display_name="OpenAI: GPT-5 Mini",
        provider="OpenAI",
        tier="small",
        roles=("low_cost_control", "cheap_control", "lightweight_review", "summariser", "docs", "test_generation"),
        experimental=False,
        context_length=400_000,
        input_modalities=("text", "image", "file"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="fast",
        cost_class="cheap",
        notes="Compact GPT-5 model for lighter-weight reasoning and lower-cost workflow stages.",
    ),
    ModelEntry(
        id="openai/gpt-5-nano",
        lifecycle="active_default",
        display_name="OpenAI: GPT-5 Nano",
        provider="OpenAI",
        tier="tiny",
        roles=("low_cost_control", "cheap_control", "metadata_extraction", "labels", "fast_chat", "summariser"),
        experimental=False,
        context_length=400_000,
        input_modalities=("text", "image", "file"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="fast",
        cost_class="tiny",
        notes="Very fast, very cheap GPT model for small control-plane tasks.",
    ),

    # ------------------------------------------------------------------
    # Long-horizon / Agentic / Value Workers — non-experimental.
    # ------------------------------------------------------------------
    ModelEntry(
        id="moonshotai/kimi-k2.6",
        lifecycle="active_specialist",
        display_name="MoonshotAI: Kimi K2.6",
        provider="MoonshotAI",
        tier="long_horizon",
        roles=("swarm_worker", "long_horizon", "coding", "ui_generation", "multi_agent", "routine_engineering"),
        experimental=False,
        context_length=262_144,
        input_modalities=("text", "image"),
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=True,
        latency_class="normal",
        cost_class="cheap",
        notes="Long-horizon coding and multi-agent orchestration candidate.",
    ),
    ModelEntry(
        id="deepseek/deepseek-v4-pro",
        lifecycle="active_default",
        display_name="DeepSeek: DeepSeek V4 Pro",
        provider="DeepSeek",
        tier="value_worker",
        roles=("value_worker", "coding", "high_context", "routine_engineering", "repo_understanding"),
        experimental=False,
        context_length=1_048_576,
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=False,
        latency_class="normal",
        cost_class="cheap",
        notes="Price-sensitive large-context execution and repo-understanding candidate.",
    ),

    # ------------------------------------------------------------------
    # OpenAI Open-weight OSS — experimental.
    # ------------------------------------------------------------------
    ModelEntry(
        id="openai/gpt-oss-20b",
        lifecycle="open_weight",
        display_name="OpenAI: GPT-OSS 20B",
        provider="OpenAI",
        tier="small",
        roles=("open_weight", "local_candidate", "low_cost_control", "cheap_control", "summariser", "metadata_extraction"),
        experimental=True,
        context_length=131_072,
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=False,
        latency_class="fast",
        cost_class="tiny",
        notes="Open-weight small model candidate. Benchmark before trusting for routing.",
    ),
    ModelEntry(
        id="openai/gpt-oss-120b",
        lifecycle="open_weight",
        display_name="OpenAI: GPT-OSS 120B",
        provider="OpenAI",
        tier="open_weight",
        roles=("open_weight", "reasoning", "coding", "reviewer", "local_candidate"),
        experimental=True,
        context_length=131_072,
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=False,
        latency_class="normal",
        cost_class="tiny",
        notes="Larger open-weight reasoning/coding candidate. Benchmark before routing.",
    ),

    # ------------------------------------------------------------------
    # Experimental specialist / value models.
    # ------------------------------------------------------------------
    ModelEntry(
        id="inclusionai/ring-2.6-1t",
        lifecycle="watchlist",
        display_name="inclusionAI: Ring-2.6 1T",
        provider="inclusionAI",
        tier="strong",
        roles=("reasoning", "coding", "agentic_mid", "value_worker", "reviewer"),
        experimental=True,
        context_length=262_144,
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=False,
        latency_class="normal",
        cost_class="cheap",
        notes="Very cheap agentic/reasoning candidate. Keep experimental until benchmarked.",
    ),
    ModelEntry(
        id="minimax/minimax-m2.7",
        lifecycle="watchlist",
        display_name="MiniMax: MiniMax M2.7",
        provider="MiniMax",
        tier="value_worker",
        roles=("value_worker", "coding", "docs", "product_engineering", "routine_engineering"),
        experimental=True,
        context_length=204_800,
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=False,
        latency_class="normal",
        cost_class="cheap",
        notes="Budget mixed worker for product engineering, docs, and internal tools.",
    ),
    ModelEntry(
        id="inception/mercury-2",
        lifecycle="active_specialist",
        display_name="Inception: Mercury 2",
        provider="Inception",
        tier="fast_reasoning",
        roles=("cheap_reasoning", "verifier", "lightweight_review", "fast_chat", "metadata_extraction"),
        experimental=True,
        context_length=128_000,
        supports_tools=True,
        supports_structured_outputs=True,
        supports_reasoning=True,
        supports_multimodal=False,
        latency_class="fast",
        cost_class="cheap",
        notes="Extremely fast reasoning candidate. Benchmark for verifier/control tasks.",
    ),
]

# ---------------------------------------------------------------------------
# Role groups — canonical lists of model IDs per named role group.
# ---------------------------------------------------------------------------

ROLE_GROUPS: dict[str, list[str]] = {
    "cheap_control": [
        "google/gemini-3.1-flash-lite",
        "qwen/qwen3.6-flash",
        "~anthropic/claude-haiku-latest",
        "ibm-granite/granite-4.1-8b",
        "google/gemma-4-26b-a4b-it",
    ],
    "routine_engineering": [
        "google/gemini-3.5-flash",
        "qwen/qwen3.7-max",
        "mistralai/codestral-2508",
        "qwen/qwen3-coder-30b-a3b-instruct",
    ],
    "planner_reviewer": [
        "x-ai/grok-4.3",
        "qwen/qwen3.7-max",
        "google/gemini-3.5-flash",
    ],
    "experimental_coding_agent": [
        "x-ai/grok-build-0.1",
        "poolside/laguna-xs.2:free",
        "poolside/laguna-m.1:free",
        "stepfun/step-3.5-flash",
    ],
}

# ---------------------------------------------------------------------------
# Capability names accepted by models_by_capability() and supports().
# ---------------------------------------------------------------------------

_CAPABILITY_ATTRS: dict[str, str] = {
    "tools": "supports_tools",
    "structured_outputs": "supports_structured_outputs",
    "reasoning": "supports_reasoning",
    "multimodal": "supports_multimodal",
}

CAPABILITY_NAMES: tuple[str, ...] = tuple(_CAPABILITY_ATTRS)

# ---------------------------------------------------------------------------
# Index — built once at import time.
# ---------------------------------------------------------------------------

_INDEX: dict[str, ModelEntry] = {entry.id: entry for entry in _REGISTRY}


# ---------------------------------------------------------------------------
# Public helpers.
# ---------------------------------------------------------------------------


def get_model(model_id: str) -> ModelEntry | None:
    """Return the ModelEntry for *model_id*, or None if not registered."""
    return _INDEX.get(model_id)


def models_by_role(role: str) -> list[ModelEntry]:
    """Return all registered models whose *roles* tuple includes *role*."""
    return [e for e in _REGISTRY if role in e.roles]


def models_by_capability(capability: str) -> list[ModelEntry]:
    """Return all registered models that support *capability*.

    Accepted capability names: "tools", "structured_outputs", "reasoning",
    "multimodal". Returns an empty list for unrecognised capability strings.
    """
    attr = _CAPABILITY_ATTRS.get(capability)
    if attr is None:
        return []
    return [e for e in _REGISTRY if getattr(e, attr)]


def display_name_for(model_id: str, fallback: str | None = None) -> str:
    """Return the display name for *model_id*.

    If the model is not in the registry, returns *fallback* when provided,
    otherwise returns *model_id* unchanged.
    """
    entry = _INDEX.get(model_id)
    if entry is not None:
        return entry.display_name
    return fallback if fallback is not None else model_id


def is_experimental(model_id: str) -> bool:
    """Return True if *model_id* is registered and marked experimental."""
    entry = _INDEX.get(model_id)
    return entry.experimental if entry is not None else False


def lifecycle_for(model_id: str) -> str | None:
    """Return the lifecycle stage for *model_id*, or None if not registered."""
    entry = _INDEX.get(model_id)
    return entry.lifecycle if entry is not None else None


def is_routing_default_eligible(model_id: str) -> bool:
    """Return True if *model_id* is eligible for default routing later.

    Eligibility is DERIVED from lifecycle: a model qualifies only when its
    lifecycle is in ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES. This branch does not
    wire routing to this value; routing/engine.py is unchanged. Returns False
    for unknown model IDs.
    """
    entry = _INDEX.get(model_id)
    if entry is None:
        return False
    return entry.lifecycle in ROUTING_DEFAULT_ELIGIBLE_LIFECYCLES


def models_by_lifecycle(lifecycle: str) -> list[ModelEntry]:
    """Return all registered models whose lifecycle equals *lifecycle*."""
    return [e for e in _REGISTRY if e.lifecycle == lifecycle]


def supports(model_id: str, capability: str) -> bool:
    """Return True if *model_id* is registered and supports *capability*.

    Returns False for unknown model IDs or unrecognised capability strings.
    """
    entry = _INDEX.get(model_id)
    if entry is None:
        return False
    attr = _CAPABILITY_ATTRS.get(capability)
    if attr is None:
        return False
    return bool(getattr(entry, attr))


def all_models() -> list[ModelEntry]:
    """Return all registered models as a list."""
    return list(_REGISTRY)


def is_known_model(model_id: str) -> bool:
    """Return True if *model_id* is a registered model.

    This is the single source of truth for model existence. Prefer it over
    reaching into the private registry or comparing against hand-maintained
    lists elsewhere in the codebase.
    """
    return model_id in _INDEX


def require_model(model_id: str) -> str:
    """Return *model_id* if it is registered, else raise ValueError.

    Useful for asserting at import or startup that a hardcoded constant still
    points at a real registry entry.
    """
    if model_id not in _INDEX:
        raise ValueError(f"Unknown model id (not in registry): {model_id}")
    return model_id


def registry_ids() -> frozenset[str]:
    """Return the set of all registered model IDs.

    Lets callers and tests check membership without reaching into the private
    registry structures.
    """
    return frozenset(_INDEX)
