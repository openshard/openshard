from __future__ import annotations

from openshard.tui.ask_mode import _ASK_FALLBACK, answer_ask_mode


def test_model_roster_includes_google_provider():
    result = answer_ask_mode("what models do you have")
    assert "Google" in result


def test_model_roster_count_is_positive():
    result = answer_ask_mode("model roster")
    first_line = result.splitlines()[0]
    # "OpenShard currently knows N registered models."
    count = int(first_line.split()[3])
    assert count > 0


def test_cheap_control_includes_gemini_flash_lite():
    result = answer_ask_mode("what cheap control models do you have")
    assert "Gemini 3.1 Flash Lite" in result


def test_cheap_control_includes_cost_and_latency():
    result = answer_ask_mode("cheap model list")
    assert " / " in result


def test_commands_includes_help_and_last():
    result = answer_ask_mode("what commands are available")
    assert "/help" in result
    assert "/last" in result


def test_commands_includes_ask():
    result = answer_ask_mode("what commands can I use")
    assert "/ask" in result


def test_commands_includes_plan():
    result = answer_ask_mode("what commands can I use")
    assert "/plan" in result


def test_openshard_description_contains_openshard():
    result = answer_ask_mode("what is openshard")
    assert "openshard" in result.lower()


def test_openshard_description_for_what_does_openshard():
    result = answer_ask_mode("what does openshard do")
    assert "openshard" in result.lower()
    assert result != _ASK_FALLBACK


def test_receipt_explanation_contains_receipt_or_shard():
    result = answer_ask_mode("what is a shard receipt")
    assert "receipt" in result.lower() or "shard" in result.lower()


def test_unsupported_question_returns_fallback():
    result = answer_ask_mode("deploy my app to production")
    assert result == _ASK_FALLBACK


def test_empty_question_returns_fallback():
    result = answer_ask_mode("")
    assert result == _ASK_FALLBACK


def test_reasoning_models_not_fallback():
    result = answer_ask_mode("reasoning capable models")
    assert result != _ASK_FALLBACK
    assert len(result) > 0


def test_experimental_models_not_fallback():
    result = answer_ask_mode("show me experimental models")
    assert result != _ASK_FALLBACK
    assert len(result) > 0


def test_model_roster_no_cheap_control_role_name():
    result = answer_ask_mode("what models do you have")
    assert "cheap_control models:" not in result


def test_model_roster_no_duplicate_provider_bracket():
    result = answer_ask_mode("what models do you have")
    assert "Google: Gemini 3.1 Flash Lite (Google)" not in result


def test_model_roster_mentions_grouping():
    result = answer_ask_mode("what models do you support?")
    assert "groups models" in result or "useful for" in result


def test_low_cost_routes_to_cheap_control():
    result = answer_ask_mode("low-cost models")
    assert result != _ASK_FALLBACK
    assert "Low-cost" in result


def test_lightweight_routes_to_cheap_control():
    result = answer_ask_mode("lightweight models")
    assert result != _ASK_FALLBACK
    assert "Low-cost" in result


def test_model_roster_includes_gpt_5_6_sol():
    result = answer_ask_mode("what models do you have")
    assert "GPT-5.6 Sol" in result


def test_model_roster_includes_kimi_k2_6():
    result = answer_ask_mode("model roster")
    assert "Kimi K2.6" in result
