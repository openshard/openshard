"""Tests for openshard.history.shard_schema — coercion, defaults, safety."""

from __future__ import annotations

import json
from unittest.mock import patch

from openshard.history.shard_contract import build_shard_receipt
from openshard.history.shard_schema import (
    SHARD_BLOCKED_FIELDS,
    SHARD_SCHEMA_VERSION,
    TIMELINE_EVENT_FIELDS,
    coerce_shard_entry,
    shard_changes_made,
    shard_manual_fix_required,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_entry(**kwargs) -> dict:
    """Minimal valid-ish shard entry for fixture use."""
    base: dict = {
        "timestamp": "2025-01-01T00:00:00Z",
        "task": "test task",
        "execution_model": "test-model",
        "retry_triggered": False,
        "duration_seconds": 1.0,
        "files_created": 0,
        "files_updated": 0,
        "files_deleted": 0,
        "verification_attempted": False,
        "verification_passed": None,
        "summary": "ok",
        "files_detail": [],
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Schema version constant
# ---------------------------------------------------------------------------


def test_schema_version_is_string():
    assert isinstance(SHARD_SCHEMA_VERSION, str)
    assert SHARD_SCHEMA_VERSION  # non-empty


def test_schema_version_newer_than_11():
    # Ensures the constant was bumped from the previous hard-coded "1.1".
    assert SHARD_SCHEMA_VERSION != "1.1"


# ---------------------------------------------------------------------------
# Old-record coercion — schema_version handling
# ---------------------------------------------------------------------------


def test_coerce_missing_version_stamps_unknown():
    entry = _minimal_entry()
    result = coerce_shard_entry(entry)
    assert result["schema_version"] == "unknown"


def test_coerce_v10_preserved():
    entry = _minimal_entry(schema_version="1.0")
    result = coerce_shard_entry(entry)
    assert result["schema_version"] == "1.0"


def test_coerce_v11_preserved():
    entry = _minimal_entry(schema_version="1.1")
    result = coerce_shard_entry(entry)
    assert result["schema_version"] == "1.1"


def test_coerce_v12_preserved():
    entry = _minimal_entry(schema_version="1.2")
    result = coerce_shard_entry(entry)
    assert result["schema_version"] == "1.2"


def test_coerce_does_not_fabricate_verification_passed():
    """Missing verification_passed must stay absent — never filled with False."""
    entry = _minimal_entry()
    entry.pop("verification_passed", None)
    result = coerce_shard_entry(entry)
    assert "verification_passed" not in result


def test_coerce_empty_dict_no_crash():
    result = coerce_shard_entry({})
    assert isinstance(result, dict)
    assert result.get("schema_version") == "unknown"


def test_coerce_does_not_mutate_input():
    entry = _minimal_entry()
    original_keys = set(entry.keys())
    coerce_shard_entry(entry)
    assert set(entry.keys()) == original_keys


# ---------------------------------------------------------------------------
# Blocked field dropping
# ---------------------------------------------------------------------------


def test_coerce_drops_blocked_fields():
    for field_name in SHARD_BLOCKED_FIELDS:
        entry = _minimal_entry(**{field_name: "should be dropped"})
        result = coerce_shard_entry(entry)
        assert field_name not in result, f"Blocked field {field_name!r} was not dropped"


def test_coerce_preserves_unknown_safe_fields():
    entry = _minimal_entry(my_future_field="some value", another_novel_key=42)
    result = coerce_shard_entry(entry)
    assert result.get("my_future_field") == "some value"
    assert result.get("another_novel_key") == 42


def test_coerce_preserves_task_id():
    from openshard.history.task_identity import new_task_id

    tid = new_task_id()
    entry = _minimal_entry(task_id=tid)
    result = coerce_shard_entry(entry)
    assert result.get("task_id") == tid


# ---------------------------------------------------------------------------
# Fail-closed guarantee
# ---------------------------------------------------------------------------


def test_blocked_fields_never_present_even_with_bad_metadata():
    """Blocked fields are stripped before metadata coercion runs."""
    entry = _minimal_entry(
        raw_prompt="secret prompt",
        metadata={"nested": {"deep": "value"}},  # triggers sanitize_metadata path
    )
    result = coerce_shard_entry(entry)
    assert "raw_prompt" not in result


def test_coerce_warning_is_static_string_only():
    """If _coerce_warning is set it must be a static token, not exception text."""
    # Craft an entry that triggers the exception path by passing a non-dict
    # as the "metadata" key after it has been processed (hard to trigger via
    # normal inputs — test the guarantee via the constant directly).
    result = coerce_shard_entry({})
    warning = result.get("_coerce_warning")
    if warning is not None:
        assert warning == "coerce_failed"
        assert "Traceback" not in warning
        assert "Error" not in warning


# ---------------------------------------------------------------------------
# Metadata sanitization
# ---------------------------------------------------------------------------


def test_coerce_sanitizes_top_level_metadata_dict():
    entry = _minimal_entry(metadata={"k1": "safe value", "nested": {"deep": "bad"}})
    result = coerce_shard_entry(entry)
    md = result.get("metadata", {})
    assert "k1" in md
    assert "nested" not in md  # nested dicts are dropped by sanitize_metadata


def test_coerce_leaves_non_metadata_subdicts_intact():
    form_factor = {"public_mode": False, "risk_level": "low"}
    entry = _minimal_entry(form_factor=form_factor)
    result = coerce_shard_entry(entry)
    assert result["form_factor"] == form_factor


# ---------------------------------------------------------------------------
# Canonical predicates
# ---------------------------------------------------------------------------


def _receipt_with(**kwargs):
    base = _minimal_entry(schema_version="1.1")
    base.update(kwargs)
    return build_shard_receipt(base, index=0)


def test_shard_changes_made_with_files():
    receipt = _receipt_with(files_detail=[{"path": "x.py", "change_type": "update", "summary": ""}])
    assert shard_changes_made(receipt) is True


def test_shard_changes_made_empty():
    receipt = _receipt_with(files_detail=[], files_updated=0, files_created=0, files_deleted=0)
    assert shard_changes_made(receipt) is False


def test_shard_changes_made_no_crash_on_bad_receipt():
    class _Bad:
        pass
    assert shard_changes_made(_Bad()) is False  # type: ignore[arg-type]


def test_shard_manual_fix_required_true():
    receipt = _receipt_with()
    # Patch directly — developer_feedback is set after build_shard_receipt in some paths
    receipt.developer_feedback = {"manual_fix_required": True}
    assert shard_manual_fix_required(receipt) is True


def test_shard_manual_fix_required_false():
    receipt = _receipt_with()
    receipt.developer_feedback = {"manual_fix_required": False}
    assert shard_manual_fix_required(receipt) is False


def test_shard_manual_fix_required_missing_field():
    receipt = _receipt_with()
    receipt.developer_feedback = None
    assert shard_manual_fix_required(receipt) is False


# ---------------------------------------------------------------------------
# Regression: old record through coerce + build_shard_receipt
# ---------------------------------------------------------------------------


def test_old_record_no_version_loads_through_build_shard_receipt():
    entry = _minimal_entry()  # no schema_version
    coerced = coerce_shard_entry(entry)
    receipt = build_shard_receipt(coerced, index=0)
    assert receipt is not None
    assert receipt.shard_id  # non-empty
    assert isinstance(receipt.status, str) and receipt.status  # non-empty string


def test_v11_record_loads_through_build_shard_receipt():
    entry = _minimal_entry(schema_version="1.1")
    coerced = coerce_shard_entry(entry)
    receipt = build_shard_receipt(coerced, index=0)
    assert receipt is not None


# ---------------------------------------------------------------------------
# JSON output regression
# ---------------------------------------------------------------------------


def test_coerced_entry_is_json_serializable():
    entry = _minimal_entry(schema_version="1.1", notes=["a note"])
    result = coerce_shard_entry(entry)
    serialized = json.dumps(result)
    parsed = json.loads(serialized)
    assert parsed["schema_version"] == "1.1"


def test_coerced_entry_with_blocked_fields_is_json_serializable():
    entry = _minimal_entry(raw_prompt="should be gone", transcript="also gone")
    result = coerce_shard_entry(entry)
    serialized = json.dumps(result)
    parsed = json.loads(serialized)
    assert "raw_prompt" not in parsed
    assert "transcript" not in parsed


# ---------------------------------------------------------------------------
# Timeline event field constants (documentation check)
# ---------------------------------------------------------------------------


def test_timeline_event_fields_contains_spec_canonical():
    for field_name in ("event_id", "shard_id", "run_id", "timestamp", "event_type", "actor"):
        assert field_name in TIMELINE_EVENT_FIELDS


def test_timeline_event_fields_contains_legacy():
    for field_name in ("event", "label", "kind", "status"):
        assert field_name in TIMELINE_EVENT_FIELDS


# ---------------------------------------------------------------------------
# Non-dict input guard (fix 1)
# ---------------------------------------------------------------------------


def test_coerce_none_input_returns_safe_dict():
    result = coerce_shard_entry(None)  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert result.get("schema_version") == "unknown"
    assert result.get("_coerce_warning") == "invalid_entry"


def test_coerce_list_input_returns_safe_dict():
    result = coerce_shard_entry([{"schema_version": "1.1"}])  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert result.get("schema_version") == "unknown"
    assert result.get("_coerce_warning") == "invalid_entry"


def test_coerce_string_input_returns_safe_dict():
    result = coerce_shard_entry('{"schema_version": "1.1"}')  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert result.get("schema_version") == "unknown"
    assert result.get("_coerce_warning") == "invalid_entry"


def test_coerce_non_dict_never_raises():
    for bad in (None, [], 42, 3.14, True, b"bytes"):
        result = coerce_shard_entry(bad)  # type: ignore[arg-type]
        assert isinstance(result, dict), f"Expected dict for input {bad!r}"


# ---------------------------------------------------------------------------
# Fail-closed exception path (fix 2 — monkeypatched)
# ---------------------------------------------------------------------------


def test_fail_closed_blocked_field_stripped_even_on_sanitize_exception():
    """Blocked fields must be absent from the result even when sanitize_metadata raises."""
    entry = _minimal_entry(
        raw_prompt="should be stripped",
        metadata={"k": "v"},  # will trigger sanitize_metadata which we monkeypatch to raise
    )
    with patch("openshard.history.shard_schema.sanitize_metadata", side_effect=RuntimeError("boom")):
        result = coerce_shard_entry(entry)

    # Blocked field was stripped before the exception fired.
    assert "raw_prompt" not in result
    # Warning is the static token, not exception text.
    assert result.get("_coerce_warning") == "coerce_failed"


def test_fail_closed_warning_contains_no_raw_exception_text():
    entry = _minimal_entry(metadata={"k": "v"})
    with patch("openshard.history.shard_schema.sanitize_metadata", side_effect=ValueError("secret boom text")):
        result = coerce_shard_entry(entry)
    serialized = json.dumps(result)
    assert "secret boom text" not in serialized
    assert "Traceback" not in serialized
    assert result.get("_coerce_warning") == "coerce_failed"


# ---------------------------------------------------------------------------
# Recursive blocked-field stripping (fix 3)
# ---------------------------------------------------------------------------


def test_coerce_strips_blocked_field_in_nested_dict():
    entry = _minimal_entry(debug={"raw_prompt": "nested secret", "safe_key": "ok"})
    result = coerce_shard_entry(entry)
    debug = result.get("debug", {})
    assert "raw_prompt" not in debug
    assert debug.get("safe_key") == "ok"


def test_coerce_strips_blocked_field_in_list_of_dicts():
    entry = _minimal_entry(items=[{"raw_prompt": "bad", "label": "good"}])
    result = coerce_shard_entry(entry)
    items = result.get("items", [])
    assert len(items) == 1
    assert "raw_prompt" not in items[0]
    assert items[0].get("label") == "good"


def test_coerce_strip_depth_bounded():
    """Deeply nested structures beyond depth 3 are left as-is (not stripped)."""
    deep = {"l1": {"l2": {"l3": {"l4": {"raw_prompt": "deep"}}}}}
    entry = _minimal_entry(deep_field=deep)
    result = coerce_shard_entry(entry)
    # We don't assert what happens at depth > 3 — just that it doesn't crash.
    assert isinstance(result, dict)
