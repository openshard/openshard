"""task_id -- explicit engineering-task identity, additive to shard_id/receipt_id.

``task_id`` is ``task_`` + a canonical UUIDv7. Unlike ``receipt_id`` it is
never auto-minted: ``ensure_task_id`` only stamps a caller-supplied value,
and ``new_task_id`` exists solely for an explicit task-creation path. Old
records without a ``task_id`` remain fully valid.
"""

from __future__ import annotations

import uuid

import pytest

from openshard.history.task_identity import (
    TASK_ID_FIELD,
    ensure_task_id,
    is_task_id,
    new_task_id,
    stored_task_id,
)


class TestTaskIdFormat:
    def test_new_ids_are_well_formed_and_distinct(self):
        ids = {new_task_id() for _ in range(2_000)}
        assert len(ids) == 2_000
        assert all(is_task_id(i) for i in ids)

    def test_new_ids_are_canonical_uuid_version_7(self):
        for _ in range(50):
            tid = new_task_id()
            parsed = uuid.UUID(tid[len("task_"):])
            assert parsed.version == 7
            assert parsed.variant == uuid.RFC_4122

    def test_validator_rejects_garbage(self):
        for bad in (
            "",
            None,
            42,
            "task_",
            "task_not-a-uuid",
            "rcpt_" + uuid.uuid4().hex,
            "shard-20260914-0001",
            # well-formed UUID string but wrong version (4, not 7):
            f"task_{uuid.uuid4()}",
            # 36 hex-and-dash chars but not a valid UUID shape at all:
            "task_" + "a" * 36,
        ):
            assert not is_task_id(bad)

    def test_validator_checks_real_version_and_variant_bits_not_just_length(self):
        # Same 36-char dashed-hex shape as a real task_id, but a UUIDv4.
        fake_v4 = "task_" + str(uuid.uuid4())
        assert not is_task_id(fake_v4)
        # Correct version nibble, but variant bits outside RFC 4122 (0xc).
        real = new_task_id()[len("task_"):]
        bad_variant = real[:19] + "c" + real[20:]
        assert not is_task_id(f"task_{bad_variant}")


class TestNeverAutoMints:
    def test_ensure_task_id_with_no_argument_leaves_entry_untouched(self):
        entry: dict = {"task": "something"}
        result = ensure_task_id(entry)
        assert result is None
        assert TASK_ID_FIELD not in entry

    def test_ensure_task_id_stamps_exactly_the_supplied_value(self):
        tid = new_task_id()
        entry: dict = {}
        result = ensure_task_id(entry, tid)
        assert result == tid
        assert entry[TASK_ID_FIELD] == tid

    def test_ensure_task_id_rejects_malformed_id(self):
        with pytest.raises(ValueError):
            ensure_task_id({}, "not-well-formed")

    def test_ensure_task_id_rejects_receipt_id_shaped_value(self):
        with pytest.raises(ValueError):
            ensure_task_id({}, "rcpt_" + uuid.uuid4().hex)


class TestStoredTaskId:
    def test_returns_none_for_absent_or_non_dict(self):
        assert stored_task_id({}) is None
        assert stored_task_id({"task_id": None}) is None
        assert stored_task_id("not-a-dict") is None
        assert stored_task_id(None) is None

    def test_returns_none_for_malformed_stored_value(self):
        assert stored_task_id({"task_id": "garbage"}) is None

    def test_returns_well_formed_stored_value(self):
        tid = new_task_id()
        assert stored_task_id({"task_id": tid}) == tid

    def test_never_mints(self):
        entry = {"task": "x"}
        stored_task_id(entry)
        assert TASK_ID_FIELD not in entry


class TestReceiptToDictCarriesTaskIdForSync:
    """v0.5.0 Platform/Sync constraint: task_id must transport through
    receipt_to_dict without any extra logic, in both default and extended
    form, so the sync layer needs no task_id-specific handling."""

    def _receipt(self, task_id: str | None):
        from openshard.history.shard_contract import build_shard_receipt

        entry: dict = {
            "task": "sync test",
            "timestamp": "2026-04-13T06:24:08Z",
            "workflow": "native",
            "executor": "native",
        }
        if task_id is not None:
            entry["task_id"] = task_id
        return build_shard_receipt(entry, 0)

    def test_task_id_present_in_default_dict(self):
        from openshard.history.views import receipt_to_dict

        tid = new_task_id()
        d = receipt_to_dict(self._receipt(tid))
        assert d["task_id"] == tid

    def test_task_id_present_in_extended_dict(self):
        from openshard.history.views import receipt_to_dict

        tid = new_task_id()
        d = receipt_to_dict(self._receipt(tid), extended=True)
        assert d["task_id"] == tid

    def test_task_id_none_when_absent_in_both_forms(self):
        from openshard.history.views import receipt_to_dict

        receipt = self._receipt(None)
        assert receipt_to_dict(receipt)["task_id"] is None
        assert receipt_to_dict(receipt, extended=True)["task_id"] is None
