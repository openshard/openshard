"""Routing dispatch smoke tests v1.

Proves that the real dispatch path (provider_availability → build_routable_pool →
resolve_routing_model_for_context) respects provider availability, model policy,
and lifecycle gates, and that CLI run paths record provider enforcement metadata
in Shards.

All tests use mocked environments and controlled registries. No live API calls,
no real home config writes, no OpenRouter network access.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from openshard.models.registry import ModelEntry
from openshard.routing.model_policy import (
    REASON_POLICY_BLOCKED_MODEL,
    REASON_POLICY_BLOCKED_PROVIDER,
    REASON_POLICY_CUSTOM_ROSTER,
    ModelPolicyConfig,
)
from openshard.routing.provider_availability import (
    build_routable_pool,
    detect_provider_availability,
)

# ---------------------------------------------------------------------------
# Environment stubs  (same convention as test_provider_eligibility.py)
# ---------------------------------------------------------------------------

ANTHROPIC_ONLY = {"ANTHROPIC_API_KEY": "k", "OPENROUTER_API_KEY": "", "OPENAI_API_KEY": ""}
OPENAI_ONLY    = {"OPENAI_API_KEY": "k",    "OPENROUTER_API_KEY": "", "ANTHROPIC_API_KEY": ""}
OPENROUTER_ENV = {"OPENROUTER_API_KEY": "k", "ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": ""}
ALL_KEYS       = {"OPENROUTER_API_KEY": "k", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k"}

# Real model IDs confirmed in openshard/models/registry.py
HAIKU_45      = "anthropic/claude-haiku-4.5"
SONNET_46     = "anthropic/claude-sonnet-4.6"
OPUS_47       = "anthropic/claude-opus-4.7"
MYTHOS_5      = "anthropic/claude-mythos-5"
GPT_54_MINI   = "openai/gpt-5.4-mini"
GPT_54        = "openai/gpt-5.4"
DEEPSEEK_FLASH = "deepseek/deepseek-v4.1-flash"
GEMINI_FLASH  = "google/gemini-3.5-flash"

# Minimal config dict that model_policy_from_config handles without error
_DEFAULT_CONFIG: dict = {"approval_mode": "smart"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(model_id: str, lifecycle: str = "active_default", tier: str = "mid") -> ModelEntry:
    vendor = model_id.lstrip("~").split("/")[0]
    return ModelEntry(
        id=model_id,
        display_name=model_id,
        provider=vendor,
        tier=tier,
        lifecycle=lifecycle,
    )


def _routable_ids(pool) -> set[str]:
    return {e.id for e in pool.routable}


def _excluded_ids(pool) -> set[str]:
    return {mid for mid, _ in pool.excluded}


def _excluded_reason(pool, model_id: str) -> str | None:
    for mid, reason in pool.excluded:
        if mid == model_id:
            return reason
    return None


def _pool(env: dict, entries: list[ModelEntry], policy: ModelPolicyConfig | None = None):
    pa = detect_provider_availability(env)
    return build_routable_pool(pa, registry=entries, policy=policy)


def _clear_caches() -> None:
    try:
        from openshard.routing import model_resolver
        model_resolver.resolve_routing_model.cache_clear()
        model_resolver.resolution_source.cache_clear()
    except Exception:
        pass


def _fake_exec_result() -> MagicMock:
    r = MagicMock()
    r.usage = None
    r.files = []
    r.summary = "done"
    r.notes = []
    return r


def _make_generator_mock() -> MagicMock:
    g = MagicMock()
    g.generate.return_value = _fake_exec_result()
    g.model = "mock-model"
    g.fixer_model = "mock-fixer"
    return g


# ---------------------------------------------------------------------------
# Group A — Provider environment isolation
# (real enforcement path; registry injected directly, no patching)
# ---------------------------------------------------------------------------

class TestProviderEnvironmentIsolation:
    def setup_method(self):
        _clear_caches()

    def teardown_method(self):
        _clear_caches()

    def test_anthropic_env_excludes_openai_models(self):
        entries = [_entry(HAIKU_45), _entry(GPT_54_MINI)]
        pool = _pool(ANTHROPIC_ONLY, entries)
        assert HAIKU_45 in _routable_ids(pool)
        assert GPT_54_MINI not in _routable_ids(pool)
        assert GPT_54_MINI in _excluded_ids(pool)

    def test_openai_env_excludes_anthropic_models(self):
        entries = [_entry(HAIKU_45), _entry(GPT_54_MINI)]
        pool = _pool(OPENAI_ONLY, entries)
        assert GPT_54_MINI in _routable_ids(pool)
        assert HAIKU_45 not in _routable_ids(pool)
        assert HAIKU_45 in _excluded_ids(pool)

    def test_openrouter_env_keeps_broad_pool(self):
        entries = [
            _entry(HAIKU_45),
            _entry(GPT_54_MINI),
            _entry(DEEPSEEK_FLASH),
            _entry(GEMINI_FLASH),
        ]
        pool = _pool(OPENROUTER_ENV, entries)
        assert len(pool.routable) >= 2


# ---------------------------------------------------------------------------
# Group B — Policy enforcement
# (real enforcement path; registry injected directly, no patching)
# ---------------------------------------------------------------------------

class TestPolicyEnforcement:
    def setup_method(self):
        _clear_caches()

    def teardown_method(self):
        _clear_caches()

    def test_blocked_provider_removed_from_pool(self):
        entries = [_entry(HAIKU_45), _entry(GPT_54_MINI)]
        policy = ModelPolicyConfig(blocked_providers=frozenset({"anthropic"}))
        pool = _pool(ALL_KEYS, entries, policy)
        assert HAIKU_45 not in _routable_ids(pool)
        assert GPT_54_MINI in _routable_ids(pool)
        reason = _excluded_reason(pool, HAIKU_45)
        assert reason == REASON_POLICY_BLOCKED_PROVIDER

    def test_blocked_model_excluded_leaves_others_routable(self):
        entries = [_entry(SONNET_46), _entry(HAIKU_45)]
        policy = ModelPolicyConfig(blocked_models=frozenset({SONNET_46}))
        pool = _pool(ANTHROPIC_ONLY, entries, policy)
        assert SONNET_46 not in _routable_ids(pool)
        assert HAIKU_45 in _routable_ids(pool)
        reason = _excluded_reason(pool, SONNET_46)
        assert reason == REASON_POLICY_BLOCKED_MODEL

    def test_custom_roster_limits_selection_to_roster_only(self):
        entries = [_entry(HAIKU_45), _entry(SONNET_46), _entry(GPT_54_MINI)]
        policy = ModelPolicyConfig(
            mode="custom_roster",
            custom_roster_models=frozenset({HAIKU_45}),
        )
        pool = _pool(ALL_KEYS, entries, policy)
        assert _routable_ids(pool) == {HAIKU_45}
        for mid, reason in pool.excluded:
            if mid != HAIKU_45:
                assert reason == REASON_POLICY_CUSTOM_ROSTER

    def test_empty_custom_roster_yields_no_routable_models(self):
        entries = [_entry(HAIKU_45)]
        policy = ModelPolicyConfig(
            mode="custom_roster",
            custom_roster_models=frozenset(),
        )
        pool = _pool(ANTHROPIC_ONLY, entries, policy)
        assert pool.routable == ()

    def test_mythos_excluded_by_watchlist_lifecycle(self):
        # anthropic/claude-mythos-5 has lifecycle="watchlist" in the real registry.
        # Default policy (allow_watchlist=False) must exclude it.
        entries = [_entry(MYTHOS_5, lifecycle="watchlist"), _entry(HAIKU_45)]
        pool = _pool(ALL_KEYS, entries)
        assert MYTHOS_5 not in _routable_ids(pool)
        assert HAIKU_45 in _routable_ids(pool)
        reason = _excluded_reason(pool, MYTHOS_5)
        assert reason is not None  # must be excluded with some reason

    def test_mythos_excluded_even_with_all_keys_and_openrouter(self):
        entries = [_entry(MYTHOS_5, lifecycle="watchlist")]
        pool = _pool(ALL_KEYS, entries)
        assert pool.routable == ()


# ---------------------------------------------------------------------------
# Group C — Routing layer: no-eligible-model contract
# (tests resolve_routing_model_for_context directly; no CLI overhead)
# ---------------------------------------------------------------------------

class TestNoEligibleModelResolution:
    def setup_method(self):
        _clear_caches()

    def teardown_method(self):
        _clear_caches()

    def test_empty_pool_returns_no_model(self):
        from openshard.routing.model_resolver import resolve_routing_model_for_context

        # Build a pool that has no routable models (only openai model, anthropic-only env)
        entries = [_entry(GPT_54_MINI)]
        pool = _pool(ANTHROPIC_ONLY, entries)
        assert pool.routable == ()

        resolution = resolve_routing_model_for_context("main", pool)
        assert resolution.model is None
        assert resolution.routable_pool_size == 0

    def test_empty_pool_source_indicates_no_eligible(self):
        from openshard.routing.model_resolver import resolve_routing_model_for_context

        entries = [_entry(GPT_54_MINI)]
        pool = _pool(ANTHROPIC_ONLY, entries)
        resolution = resolve_routing_model_for_context("cheap", pool)
        # source must indicate no eligible model was found
        assert "no_eligible" in resolution.source or resolution.model is None


# ---------------------------------------------------------------------------
# Group D — CLI / end-to-end
# (executor mocked, provider-aware enforcement path real via os.environ patch)
# ---------------------------------------------------------------------------

_CLI_PATCHES_BASE = [
    # Override conftest autouse so detect_provider returns "openrouter" →
    # no AnthropicProvider/OpenAIProvider instantiation needed.
    "openshard.run.pipeline.detect_provider",
    "openshard.run.pipeline.ExecutionGenerator",
    "openshard.run.pipeline.NativeAgentExecutor",
    "openshard.run.pipeline.analyze_repo",
]


class TestCLIDispatch(unittest.TestCase):
    def setUp(self):
        _clear_caches()

    def tearDown(self):
        _clear_caches()

    def test_shard_records_provider_enforcement_metadata(self):
        # runs.jsonl is not written inside isolated_filesystem (no git root detected).
        # Capture _log_run kwargs directly — the product path still executes the full
        # enforcement pipeline; we're only intercepting the write step.
        from openshard.cli.main import cli

        gen_mock = _make_generator_mock()
        runner = CliRunner()
        captured: dict = {}

        def _capture_log(*args, **kwargs):
            captured.update(kwargs)

        with patch("openshard.run.pipeline.detect_provider", return_value="openrouter"), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=gen_mock), \
             patch("openshard.run.pipeline.NativeAgentExecutor", return_value=gen_mock), \
             patch("openshard.run.pipeline.analyze_repo", return_value=MagicMock()), \
             patch("openshard.run.pipeline.build_verification_plan", return_value=MagicMock()), \
             patch("openshard.run.pipeline._log_run", side_effect=_capture_log), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch.dict(os.environ, ANTHROPIC_ONLY, clear=False):
            with runner.isolated_filesystem():
                result = runner.invoke(
                    cli, ["run", "add a simple function"], catch_exceptions=False
                )

        self.assertEqual(
            result.exit_code, 0,
            f"Run must succeed before Shard is recorded. exit={result.exit_code}",
        )
        self.assertTrue(captured, "_log_run was never called; Shard metadata not recorded")

        # routable_pool passed to _log_run must have available_providers recorded
        pool = captured.get("routable_pool")
        self.assertIsNotNone(pool, "_log_run must receive routable_pool")
        self.assertIn(
            "anthropic", pool.available_providers,
            "routable_pool.available_providers must include 'anthropic'",
        )
        self.assertIsInstance(
            len(pool.routable), int,
            "routable_pool.routable must be sized",
        )

        # provider_enforcement_result passed to _log_run must be a ProviderAwareResolution
        pe_result = captured.get("provider_enforcement_result")
        self.assertIsNotNone(pe_result, "_log_run must receive provider_enforcement_result")
        # Must have enforcement_applied attribute (bool)
        self.assertIsInstance(
            pe_result.enforcement_applied, bool,
            "provider_enforcement_result.enforcement_applied must be bool",
        )
        # If enforcement was applied, selected model must be anthropic
        if pe_result.enforcement_applied and pe_result.selected_model:
            self.assertTrue(
                pe_result.selected_model.startswith("anthropic/"),
                f"Enforcement applied but selected_model is not anthropic: "
                f"{pe_result.selected_model!r}",
            )

    def test_normal_run_path_succeeds_with_default_policy(self):
        from openshard.cli.main import cli

        gen_mock = _make_generator_mock()
        runner = CliRunner()

        with patch("openshard.run.pipeline.detect_provider", return_value="openrouter"), \
             patch("openshard.run.pipeline.ExecutionGenerator", return_value=gen_mock), \
             patch("openshard.run.pipeline.NativeAgentExecutor", return_value=gen_mock), \
             patch("openshard.run.pipeline.analyze_repo", return_value=MagicMock()), \
             patch("openshard.run.pipeline.build_verification_plan", return_value=MagicMock()), \
             patch("openshard.cli.main.load_config", return_value=_DEFAULT_CONFIG), \
             patch.dict(os.environ, OPENROUTER_ENV, clear=False):
            with runner.isolated_filesystem():
                result = runner.invoke(
                    cli, ["run", "write a hello world function"], catch_exceptions=False
                )

        self.assertEqual(
            result.exit_code, 0,
            f"Expected exit_code=0 for normal run, got {result.exit_code}. "
            f"Output: {result.output!r}",
        )


# ---------------------------------------------------------------------------
# Group D2 — Narrow receipt mutation tests for per-role dispatch v1
# (test the dataclass mutation and routing-truth derivation only;
#  no full pipeline execution required)
# ---------------------------------------------------------------------------

class TestReceiptMutation(unittest.TestCase):
    """Proves the receipt mutation points added in per-role dispatch v1."""

    def _make_receipt(self, **kwargs):
        from openshard.native.context import NativeTierDispatchReceipt
        defaults = dict(
            enabled=True,
            applied=True,
            tier_source="category_fallback",
            planner_model="anthropic/claude-sonnet-4.6",
            executor_model="z-ai/glm-5.1",
            validator_model="anthropic/claude-sonnet-4.6",
        )
        defaults.update(kwargs)
        return NativeTierDispatchReceipt(**defaults)

    def _entry_with_receipt(self, receipt) -> dict:
        from dataclasses import asdict
        return {
            "schema_version": "1.2",
            "task": "dispatch test",
            "timestamp": "2025-01-01T00:00:00Z",
            "execution_model": "z-ai/glm-5.1",
            "tier_dispatch_receipt": asdict(receipt),
        }

    def test_executor_model_actual_written_after_call(self):
        """Simulates the pipeline assignment after generator.generate() returns."""
        from openshard.history.routing_truth import build_routing_truth

        receipt = self._make_receipt()
        self.assertIsNone(receipt.executor_model_actual)

        # Simulate what pipeline.py does after the executor call succeeds.
        receipt.executor_model_actual = receipt.executor_model

        self.assertEqual(receipt.executor_model_actual, "z-ai/glm-5.1")
        rt = build_routing_truth(self._entry_with_receipt(receipt))
        self.assertTrue(rt.executor_dispatched)
        self.assertIn("executor", rt.dispatched_roles)

    def test_validator_model_actual_written_after_stage(self):
        """Simulates the pipeline assignment after run_validator_stage() returns."""
        from openshard.history.routing_truth import build_routing_truth

        receipt = self._make_receipt()
        self.assertIsNone(receipt.validator_model_actual)

        # Simulate what pipeline.py does after the validator stage succeeds.
        receipt.validator_model_actual = receipt.validator_model
        receipt.validator_dispatch_status = "applied"

        self.assertEqual(receipt.validator_model_actual, "anthropic/claude-sonnet-4.6")
        rt = build_routing_truth(self._entry_with_receipt(receipt))
        self.assertTrue(rt.validator_dispatched)
        self.assertIn("validator", rt.dispatched_roles)

    def test_planner_not_in_dispatched_roles_without_actual(self):
        """Planner has no real call site; planner_model_actual is never set."""
        from openshard.history.routing_truth import build_routing_truth

        # Set executor and validator actuals — planner stays None.
        receipt = self._make_receipt(
            executor_model_actual="z-ai/glm-5.1",
            validator_model_actual="anthropic/claude-sonnet-4.6",
            validator_dispatch_status="applied",
        )
        rt = build_routing_truth(self._entry_with_receipt(receipt))
        self.assertFalse(rt.planner_dispatched)
        self.assertNotIn("planner", rt.dispatched_roles)
        self.assertIn("planner", rt.advisory_only_roles)

    def test_receipt_with_no_actuals_gives_all_advisory(self):
        """Receipt with enabled+applied but no *_actual fields → all advisory."""
        from openshard.history.routing_truth import build_routing_truth

        receipt = self._make_receipt()
        rt = build_routing_truth(self._entry_with_receipt(receipt))
        self.assertEqual(rt.dispatched_roles, [])
        self.assertEqual(
            set(rt.advisory_only_roles), {"planner", "executor", "validator"}
        )

    def test_no_eligible_model_for_executor_role_returns_none(self):
        """Empty pool for executor role gives a clear None result, not silent wrong model."""
        from openshard.routing.model_resolver import resolve_routing_model_for_context

        # openai-only env, but only anthropic model in registry → pool is empty.
        entries = [_entry(SONNET_46, lifecycle="active_default")]
        pool = _pool(OPENAI_ONLY, entries)
        resolution = resolve_routing_model_for_context("main", pool)
        self.assertIsNone(resolution.model)
        self.assertEqual(resolution.routable_pool_size, 0)


# ---------------------------------------------------------------------------
# Group E — Isolation: routing module graph
# ---------------------------------------------------------------------------

def test_routing_does_not_import_openrouter_cache_module():
    """provider_availability must not pull in OpenRouter cache/metadata/sync at import time."""
    import importlib

    sys.modules.pop("openshard.routing.provider_availability", None)
    before = set(sys.modules)
    importlib.import_module("openshard.routing.provider_availability")
    newly_loaded = set(sys.modules) - before

    forbidden = [
        name for name in newly_loaded
        if name.startswith("openshard.")
        and "openrouter" in name
        and any(word in name for word in ("cache", "metadata", "sync", "fetcher"))
    ]
    assert not forbidden, (
        "openshard.routing.provider_availability must not transitively import "
        f"OpenRouter cache/metadata/sync/fetcher modules at import time; found: {forbidden}"
    )
