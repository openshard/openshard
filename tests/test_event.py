"""Tests for openshard.history.event — Canonical Event v0 (Migration 3)."""

from __future__ import annotations

import json
import re
import unittest
from dataclasses import asdict

from openshard.history.event import (
    EVENT_APPROVAL_DENIED,
    EVENT_APPROVAL_GRANTED,
    EVENT_APPROVAL_REQUESTED,
    EVENT_INTERACTION_ACCEPTED,
    EVENT_POLICY_CHECKED,
    EVENT_RECEIPT_SEALED,
    EVENT_RETRY_STARTED,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_FAILED,
    EVENT_RUN_STARTED,
    EVENT_TOOL_INVOKED,
    EVENT_TYPES,
    EVENT_UNKNOWN,
    EVENT_VERIFICATION_FAILED,
    EVENT_VERIFICATION_PASSED,
    EVIDENCE_AGENT_REPORTED,
    EVIDENCE_DIRECTLY_OBSERVED,
    EVIDENCE_GIT_OBSERVED,
    EVIDENCE_INDEPENDENTLY_VERIFIED,
    EVIDENCE_UNKNOWN,
    SOURCE_CLAUDE_CODE_IMPORT,
    SOURCE_POLICY_DECISIONS,
    SOURCE_REVIEW_CHECKS,
    SOURCE_RUN_TIMELINE,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    STATUS_UNKNOWN,
    VALID_EVENT_STATUSES,
    VALID_EVIDENCE,
    Event,
    _stable_event_id,
    build_event_from_adapter_entry,
    build_events_from_checkpoints,
    build_events_from_interactions,
    build_events_from_native_steps,
    build_events_from_policy_decisions,
    build_events_from_review_checks,
    build_events_from_timeline,
    events_from_entry,
    make_event,
)
from openshard.history.interactions import DeveloperInteractionEvent
from openshard.history.native_steps import NativeStepEvent
from openshard.history.run_checkpoints import NativeRunCheckpointEvent
from openshard.history.shard_schema import SHARD_BLOCKED_FIELDS

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_EVENT_ID_RE = re.compile(r"^evt-[0-9a-f]{12}$")


def _evt(**kwargs) -> Event:
    defaults = dict(
        event_type=EVENT_RUN_COMPLETED,
        source=SOURCE_RUN_TIMELINE,
        action="did a thing",
    )
    defaults.update(kwargs)
    return make_event(**defaults)


def _assert_no_unsafe(text: str) -> None:
    for needle in (
        "C:\\", "C:/", "/Users/", "/home/", "/etc/",
        "sk-", "AKIA", "api_key=", "password=", "secret=",
    ):
        assert needle.lower() not in text.lower(), (
            f"unsafe substring {needle!r} leaked in: {text!r}"
        )


def _timeline_event(
    event: str = "repo_scanned",
    label: str | None = "Repository scanned",
    kind: str = "scan",
    status: str = "completed",
    target: str | None = None,
) -> dict:
    ev: dict = {"event": event, "kind": kind, "status": status}
    if label is not None:
        ev["label"] = label
    if target is not None:
        ev["target"] = target
    return ev


def _check(name: str = "terraform_fmt", status: str = "passed", summary: str = "formatting ok") -> dict:
    return {"name": name, "status": status, "summary": summary}


def _policy_decision(
    decision_id: str | None = "decision-001",
    action: str = "write",
    decision: str = "allow",
    reason: str = "Approved by path policy",
    approval_required: bool = False,
    approval_granted: bool | None = None,
) -> dict:
    d: dict = {"action": action, "decision": decision, "reason": reason}
    if decision_id is not None:
        d["decision_id"] = decision_id
    if approval_required:
        d["approval_required"] = True
        d["approval_granted"] = approval_granted
    return d


def _native_step(**kwargs) -> NativeStepEvent:
    defaults = dict(run_id="run-1", step_name="write patch", stage="generate", status="passed")
    defaults.update(kwargs)
    return NativeStepEvent(**defaults)


def _checkpoint(**kwargs) -> NativeRunCheckpointEvent:
    defaults = dict(run_id="run-1", stage="verify", status="passed", executor="native")
    defaults.update(kwargs)
    return NativeRunCheckpointEvent(**defaults)


def _interaction(**kwargs) -> DeveloperInteractionEvent:
    defaults = dict(run_id="run-1", event_type="accepted", summary="looks good")
    defaults.update(kwargs)
    return DeveloperInteractionEvent(**defaults)


def _adapter_entry(**kwargs) -> dict:
    defaults = dict(
        executor="claude_code_import",
        import_source="claude_code",
        files_source="git_diff_inferred",
        verification_attempted=False,
        verification_passed=None,
        run_id="run-1",
        shard_id="shard-1",
        summary="imported external run",
    )
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# 1. Event can be created safely
# ---------------------------------------------------------------------------

class TestMakeEventSafety(unittest.TestCase):
    def test_minimal_call_never_raises(self):
        e = make_event(event_type=EVENT_RUN_STARTED, source="x", action="y")
        self.assertIsInstance(e, Event)

    def test_garbage_types_never_raise(self):
        e = make_event(
            event_type=object(),
            source=123,
            action=["not", "a", "string"],
            status=object(),
            evidence=[],
            metadata="not a dict",
            run_id=42,
            shard_id=object(),
            attempt_number="one",
            actor=99,
            target=object(),
        )
        self.assertIsInstance(e, Event)

    def test_raw_content_stored_always_false(self):
        e = _evt()
        self.assertFalse(e.raw_content_stored)

    def test_raw_content_stored_cannot_be_forced_true(self):
        e = Event(
            event_id="evt-test",
            event_type=EVENT_UNKNOWN,
            occurred_at=None,
            run_id=None,
            shard_id=None,
            attempt_number=None,
            actor=None,
            source="x",
            action="y",
            target=None,
            status=STATUS_UNKNOWN,
            evidence=EVIDENCE_UNKNOWN,
            raw_content_stored=True,
        )
        self.assertFalse(e.raw_content_stored)

    def test_json_serializable(self):
        e = _evt()
        json.dumps(asdict(e))  # must not raise


# ---------------------------------------------------------------------------
# 2. Stable event_id
# ---------------------------------------------------------------------------

class TestStableEventId(unittest.TestCase):
    def test_same_input_same_id(self):
        id1 = _stable_event_id("run_timeline", "repo_scanned", 0, run_ref="shard-x")
        id2 = _stable_event_id("run_timeline", "repo_scanned", 0, run_ref="shard-x")
        self.assertEqual(id1, id2)

    def test_different_index_different_id(self):
        id1 = _stable_event_id("run_timeline", "repo_scanned", 0, run_ref="shard-x")
        id2 = _stable_event_id("run_timeline", "repo_scanned", 1, run_ref="shard-x")
        self.assertNotEqual(id1, id2)

    def test_different_run_ref_different_id(self):
        id1 = _stable_event_id("run_timeline", "repo_scanned", 0, run_ref="shard-a")
        id2 = _stable_event_id("run_timeline", "repo_scanned", 0, run_ref="shard-b")
        self.assertNotEqual(id1, id2)

    def test_id_format(self):
        self.assertRegex(_stable_event_id("s", "n", 0), _EVENT_ID_RE)

    def test_not_derived_from_position_alone(self):
        # Same index, different source_name -> different id (position alone
        # does not determine identity).
        id1 = _stable_event_id("run_timeline", "a", 0, run_ref="shard-x")
        id2 = _stable_event_id("run_timeline", "b", 0, run_ref="shard-x")
        self.assertNotEqual(id1, id2)


class TestNativeEventIdsAreGenuinelyUnique(unittest.TestCase):
    def test_two_native_events_without_explicit_id_never_collide(self):
        e1 = _evt()
        e2 = _evt()
        self.assertNotEqual(e1.event_id, e2.event_id)

    def test_native_event_id_is_not_a_deterministic_hash(self):
        # Calling make_event twice with identical semantic inputs but no
        # event_id must NOT reproduce _stable_event_id's output -- that
        # function is for legacy projection only.
        e1 = _evt(event_type=EVENT_RUN_STARTED, source="native", action="same")
        e2 = _evt(event_type=EVENT_RUN_STARTED, source="native", action="same")
        self.assertNotEqual(e1.event_id, e2.event_id)
        stable = _stable_event_id("native", "same", 0)
        self.assertNotEqual(e1.event_id, stable)

    def test_explicit_event_id_is_reused_unchanged(self):
        e = _evt(event_id="evt-explicit-123")
        self.assertEqual(e.event_id, "evt-explicit-123")


# ---------------------------------------------------------------------------
# 3. run_id / shard_id / attempt_number linkage survives serialization
# ---------------------------------------------------------------------------

class TestLinkageSurvivesSerialization(unittest.TestCase):
    def test_round_trip_preserves_linkage(self):
        e = _evt(run_id="run-1", shard_id="shard-1", attempt_number=2)
        restored = Event.from_dict(e.to_dict())
        self.assertEqual(restored.run_id, "run-1")
        self.assertEqual(restored.shard_id, "shard-1")
        self.assertEqual(restored.attempt_number, 2)

    def test_missing_linkage_stays_none(self):
        e = _evt()
        restored = Event.from_dict(e.to_dict())
        self.assertIsNone(restored.run_id)
        self.assertIsNone(restored.shard_id)
        self.assertIsNone(restored.attempt_number)


# ---------------------------------------------------------------------------
# 4. Missing optional fields do not crash
# ---------------------------------------------------------------------------

class TestMissingFieldsDoNotCrash(unittest.TestCase):
    def test_events_from_entry_empty_dict(self):
        self.assertEqual(events_from_entry({}), [])

    def test_events_from_entry_non_dict(self):
        for bad in (None, [], "string", 42, True):
            with self.subTest(input=bad):
                self.assertEqual(events_from_entry(bad), [])

    def test_events_from_entry_none_fields(self):
        self.assertEqual(
            events_from_entry({"run_timeline": None, "review_checks": None, "policy_decisions": None}),
            [],
        )

    def test_all_builders_handle_none_and_non_list(self):
        for builder in (
            build_events_from_timeline,
            build_events_from_review_checks,
            build_events_from_policy_decisions,
            build_events_from_native_steps,
            build_events_from_checkpoints,
            build_events_from_interactions,
        ):
            with self.subTest(builder=builder.__name__):
                self.assertEqual(builder(None), [])
                self.assertEqual(builder("not-a-list"), [])
                self.assertEqual(builder(99), [])


# ---------------------------------------------------------------------------
# 5. External-observed Event does not imply OpenShard execution
# ---------------------------------------------------------------------------

class TestExternalObservedHonesty(unittest.TestCase):
    def test_adapter_entry_actor_is_explicit_import_source_not_openshard(self):
        entry = _adapter_entry()
        events = events_from_entry(entry)
        self.assertGreater(len(events), 0)
        adapter_events = [e for e in events if e.source == SOURCE_CLAUDE_CODE_IMPORT]
        self.assertEqual(len(adapter_events), 1)
        self.assertEqual(adapter_events[0].actor, "claude_code")
        self.assertNotIn("openshard", (adapter_events[0].actor or "").lower())

    def test_adapter_entry_without_import_source_leaves_actor_none(self):
        entry = _adapter_entry(import_source=None)
        events = events_from_entry(entry)
        adapter_events = [e for e in events if e.source == SOURCE_CLAUDE_CODE_IMPORT]
        self.assertIsNone(adapter_events[0].actor)

    def test_unattempted_verification_never_reported_as_passed(self):
        entry = _adapter_entry(verification_attempted=False, verification_passed=None)
        events = events_from_entry(entry)
        adapter_events = [e for e in events if e.source == SOURCE_CLAUDE_CODE_IMPORT]
        self.assertEqual(adapter_events[0].status, STATUS_UNKNOWN)

    def test_git_diff_inferred_files_source_maps_to_git_observed_evidence(self):
        entry = _adapter_entry(files_source="git_diff_inferred")
        events = events_from_entry(entry)
        adapter_events = [e for e in events if e.source == SOURCE_CLAUDE_CODE_IMPORT]
        self.assertEqual(adapter_events[0].evidence, EVIDENCE_GIT_OBSERVED)

    def test_non_adapter_executor_produces_no_adapter_event(self):
        self.assertIsNone(build_event_from_adapter_entry({"executor": "native"}))

    def test_timeline_events_on_external_entry_use_agent_reported_evidence(self):
        entry = {
            "executor": "claude_code_import",
            "run_timeline": [_timeline_event()],
        }
        events = events_from_entry(entry)
        timeline_events = [e for e in events if e.source == SOURCE_RUN_TIMELINE]
        self.assertEqual(len(timeline_events), 1)
        self.assertEqual(timeline_events[0].evidence, EVIDENCE_AGENT_REPORTED)


# ---------------------------------------------------------------------------
# 6. Legacy timeline event projects into canonical Event
# ---------------------------------------------------------------------------

class TestTimelineProjection(unittest.TestCase):
    def test_occurred_at_is_none_no_timestamp_in_source(self):
        events = build_events_from_timeline([_timeline_event()])
        self.assertIsNone(events[0].occurred_at)

    def test_completed_maps_to_passed(self):
        events = build_events_from_timeline([_timeline_event(status="completed")])
        self.assertEqual(events[0].status, STATUS_PASSED)

    def test_failed_maps_to_failed(self):
        events = build_events_from_timeline([_timeline_event(status="failed")])
        self.assertEqual(events[0].status, STATUS_FAILED)

    def test_skipped_maps_to_skipped(self):
        events = build_events_from_timeline([_timeline_event(status="skipped")])
        self.assertEqual(events[0].status, STATUS_SKIPPED)

    def test_receipt_saved_maps_to_receipt_sealed(self):
        events = build_events_from_timeline([_timeline_event(event="receipt_saved", kind="receipt")])
        self.assertEqual(events[0].event_type, EVENT_RECEIPT_SEALED)

    def test_run_kind_started_maps_to_run_started(self):
        events = build_events_from_timeline([_timeline_event(event="run_x", kind="run", status="started")])
        self.assertEqual(events[0].event_type, EVENT_RUN_STARTED)

    def test_run_kind_completed_maps_to_run_completed(self):
        events = build_events_from_timeline([_timeline_event(event="run_x", kind="run", status="completed")])
        self.assertEqual(events[0].event_type, EVENT_RUN_COMPLETED)

    def test_run_kind_failed_maps_to_run_failed(self):
        events = build_events_from_timeline([_timeline_event(event="run_x", kind="run", status="failed")])
        self.assertEqual(events[0].event_type, EVENT_RUN_FAILED)

    def test_check_kind_passed_maps_to_verification_passed(self):
        events = build_events_from_timeline([_timeline_event(kind="check", status="completed")])
        self.assertEqual(events[0].event_type, EVENT_VERIFICATION_PASSED)

    def test_unrecognized_kind_falls_back_to_unknown_event(self):
        events = build_events_from_timeline([_timeline_event(event="model_called", kind="model")])
        self.assertEqual(events[0].event_type, EVENT_UNKNOWN)

    def test_non_dict_item_skipped(self):
        events = build_events_from_timeline([None, "bad", 42, _timeline_event()])
        self.assertEqual(len(events), 1)

    def test_ids_stable_across_calls(self):
        tl = [_timeline_event()]
        ids1 = {e.event_id for e in build_events_from_timeline(tl, run_ref="shard-x")}
        ids2 = {e.event_id for e in build_events_from_timeline(tl, run_ref="shard-x")}
        self.assertEqual(ids1, ids2)

    def test_source_is_run_timeline(self):
        events = build_events_from_timeline([_timeline_event()])
        self.assertEqual(events[0].source, SOURCE_RUN_TIMELINE)


# ---------------------------------------------------------------------------
# 7. Native/OpenShard events project correctly
# ---------------------------------------------------------------------------

class TestNativeStepProjection(unittest.TestCase):
    def test_event_id_reused_unchanged(self):
        step = _native_step(event_id="native-evt-1")
        events = build_events_from_native_steps([step])
        self.assertEqual(events[0].event_id, "native-evt-1")

    def test_occurred_at_equals_source_timestamp(self):
        step = _native_step(timestamp="2026-01-01T00:00:00Z")
        events = build_events_from_native_steps([step])
        self.assertEqual(events[0].occurred_at, "2026-01-01T00:00:00Z")

    def test_tool_name_maps_to_tool_invoked(self):
        step = _native_step(tool_name="grep", stage="generate")
        events = build_events_from_native_steps([step])
        self.assertEqual(events[0].event_type, EVENT_TOOL_INVOKED)

    def test_evidence_is_directly_observed(self):
        events = build_events_from_native_steps([_native_step()])
        self.assertEqual(events[0].evidence, EVIDENCE_DIRECTLY_OBSERVED)

    def test_non_native_step_item_skipped(self):
        events = build_events_from_native_steps([None, {"not": "a dataclass"}, _native_step()])
        self.assertEqual(len(events), 1)


class TestCheckpointProjection(unittest.TestCase):
    def test_event_id_reused_unchanged(self):
        cp = _checkpoint(event_id="cp-evt-1")
        events = build_events_from_checkpoints([cp])
        self.assertEqual(events[0].event_id, "cp-evt-1")

    def test_occurred_at_equals_source_timestamp(self):
        cp = _checkpoint(timestamp="2026-02-02T00:00:00Z")
        events = build_events_from_checkpoints([cp])
        self.assertEqual(events[0].occurred_at, "2026-02-02T00:00:00Z")

    def test_verify_passed_maps_to_verification_passed(self):
        cp = _checkpoint(stage="verify", status="passed")
        events = build_events_from_checkpoints([cp])
        self.assertEqual(events[0].event_type, EVENT_VERIFICATION_PASSED)

    def test_verify_failed_maps_to_verification_failed(self):
        cp = _checkpoint(stage="verify", status="failed")
        events = build_events_from_checkpoints([cp])
        self.assertEqual(events[0].event_type, EVENT_VERIFICATION_FAILED)

    def test_retry_stage_maps_to_retry_started(self):
        cp = _checkpoint(stage="retry", status="started")
        events = build_events_from_checkpoints([cp])
        self.assertEqual(events[0].event_type, EVENT_RETRY_STARTED)

    def test_actor_is_explicit_executor_field(self):
        cp = _checkpoint(executor="native")
        events = build_events_from_checkpoints([cp])
        self.assertEqual(events[0].actor, "native")

    def test_missing_executor_leaves_actor_none(self):
        cp = _checkpoint(executor="")
        events = build_events_from_checkpoints([cp])
        self.assertIsNone(events[0].actor)


class TestInteractionProjection(unittest.TestCase):
    def test_actor_passed_through_unchanged(self):
        it = _interaction(actor="developer")
        events = build_events_from_interactions([it])
        self.assertEqual(events[0].actor, "developer")

    def test_accepted_maps_to_interaction_accepted(self):
        events = build_events_from_interactions([_interaction(event_type="accepted")])
        self.assertEqual(events[0].event_type, EVENT_INTERACTION_ACCEPTED)

    def test_retried_maps_to_retry_started(self):
        events = build_events_from_interactions([_interaction(event_type="retried")])
        self.assertEqual(events[0].event_type, EVENT_RETRY_STARTED)

    def test_event_id_reused_unchanged(self):
        it = _interaction(event_id="int-evt-1")
        events = build_events_from_interactions([it])
        self.assertEqual(events[0].event_id, "int-evt-1")


# ---------------------------------------------------------------------------
# 8. Blocked/private fields cannot leak through metadata
# ---------------------------------------------------------------------------

class TestBlockedFieldsCannotLeak(unittest.TestCase):
    def test_blocked_field_keys_dropped_from_metadata(self):
        for key in SHARD_BLOCKED_FIELDS:
            with self.subTest(key=key):
                e = _evt(metadata={key: "some secret value", "note": "ok"})
                self.assertNotIn(key, e.metadata)

    def test_safe_key_survives(self):
        e = _evt(metadata={"note": "ok"})
        self.assertIn("note", e.metadata)

    def test_secret_shaped_value_dropped_even_under_safe_key(self):
        e = _evt(metadata={"detail": "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234"})
        self.assertNotIn("detail", e.metadata)


# ---------------------------------------------------------------------------
# 9. Arbitrary metadata cannot bypass sanitisation
# ---------------------------------------------------------------------------

class TestMetadataSanitisation(unittest.TestCase):
    def test_more_than_ten_keys_capped(self):
        metadata = {f"k{i}": i for i in range(20)}
        e = _evt(metadata=metadata)
        self.assertLessEqual(len(e.metadata), 10)

    def test_nested_dict_dropped(self):
        e = _evt(metadata={"nested": {"a": 1}})
        self.assertNotIn("nested", e.metadata)

    def test_nested_list_dropped(self):
        e = _evt(metadata={"nested": [1, 2, 3]})
        self.assertNotIn("nested", e.metadata)

    def test_absolute_path_dropped(self):
        e = _evt(metadata={"path": "C:\\Users\\admin\\secret.py"})
        self.assertNotIn("path", e.metadata)

    def test_secret_like_token_dropped(self):
        for val in ("sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234", "AKIAABCDEFGHIJKL1234", "api_key=xyz123"):
            with self.subTest(val=val):
                e = _evt(metadata={"k": val})
                self.assertNotIn("k", e.metadata)

    def test_action_and_target_sanitized(self):
        e = _evt(action="C:\\Users\\admin\\file.py", target="sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234")
        _assert_no_unsafe(e.action)
        self.assertIsNone(e.target)


# ---------------------------------------------------------------------------
# 10. Old records continue to read/render unchanged
# ---------------------------------------------------------------------------

class TestOldRecordsUnaffected(unittest.TestCase):
    def test_pre_schema_entry_returns_empty_list(self):
        self.assertEqual(events_from_entry({"task": "x"}), [])

    def test_entry_missing_schema_version_still_works(self):
        entry = {"review_checks": [_check()]}
        events = events_from_entry(entry)
        self.assertGreater(len(events), 0)

    def test_legacy_timeline_missing_status_and_kind_does_not_crash(self):
        ev = {"event": "repo_scanned", "label": "Repository scanned"}
        events = build_events_from_timeline([ev])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, STATUS_PASSED)  # default status "completed" -> passed


# ---------------------------------------------------------------------------
# 11. Round-trip serialize/deserialize
# ---------------------------------------------------------------------------

class TestRoundTrip(unittest.TestCase):
    def _assert_round_trips(self, e: Event) -> None:
        restored = Event.from_dict(e.to_dict())
        self.assertEqual(restored, e)

    def test_timeline_event_round_trips(self):
        self._assert_round_trips(build_events_from_timeline([_timeline_event()])[0])

    def test_review_check_event_round_trips(self):
        self._assert_round_trips(build_events_from_review_checks([_check()])[0])

    def test_policy_decision_event_round_trips(self):
        self._assert_round_trips(build_events_from_policy_decisions([_policy_decision()])[0])

    def test_native_step_event_round_trips(self):
        self._assert_round_trips(build_events_from_native_steps([_native_step()])[0])

    def test_checkpoint_event_round_trips(self):
        self._assert_round_trips(build_events_from_checkpoints([_checkpoint()])[0])

    def test_interaction_event_round_trips(self):
        self._assert_round_trips(build_events_from_interactions([_interaction()])[0])

    def test_from_dict_handles_non_dict_input(self):
        for bad in (None, [], "string", 42):
            with self.subTest(input=bad):
                e = Event.from_dict(bad)
                self.assertIsInstance(e, Event)


# ---------------------------------------------------------------------------
# 12. Malformed input degrades to unknown, not fabrication
# ---------------------------------------------------------------------------

class TestMalformedInputDegradesSafely(unittest.TestCase):
    def test_unrecognized_event_type_falls_back_to_unknown(self):
        e = make_event(event_type="totally_made_up.thing", source="x", action="y")
        self.assertEqual(e.event_type, EVENT_UNKNOWN)

    def test_unrecognized_status_falls_back_to_unknown_not_passed(self):
        e = _evt(status="ok")
        self.assertEqual(e.status, STATUS_UNKNOWN)
        self.assertNotEqual(e.status, STATUS_PASSED)

    def test_unrecognized_evidence_falls_back_to_unknown(self):
        e = _evt(evidence="extremely_confident")
        self.assertEqual(e.evidence, EVIDENCE_UNKNOWN)

    def test_all_valid_statuses_preserved(self):
        for s in VALID_EVENT_STATUSES:
            with self.subTest(status=s):
                self.assertEqual(_evt(status=s).status, s)

    def test_all_valid_evidence_preserved(self):
        for ev in VALID_EVIDENCE:
            with self.subTest(evidence=ev):
                self.assertEqual(_evt(evidence=ev).evidence, ev)

    def test_all_event_types_are_frozenset_members(self):
        # No stray constant left out of the allow-list.
        self.assertIn(EVENT_RUN_STARTED, EVENT_TYPES)
        self.assertIn(EVENT_UNKNOWN, EVENT_TYPES)

    def test_non_dict_items_in_lists_skipped_not_guessed(self):
        events = build_events_from_review_checks([1, "x", None])
        self.assertEqual(events, [])

    def test_empty_dict_item_degrades_to_unknown_not_skipped(self):
        # {} is a dict (not a malformed item) -- it degrades to safe unknown
        # defaults rather than being silently dropped or guessed at.
        events = build_events_from_review_checks([{}])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, STATUS_UNKNOWN)
        self.assertEqual(events[0].event_type, EVENT_UNKNOWN)

    def test_policy_decision_missing_action_skipped(self):
        d = {"decision": "allow"}
        self.assertEqual(build_events_from_policy_decisions([d]), [])

    def test_policy_decision_missing_decision_skipped(self):
        d = {"action": "write"}
        self.assertEqual(build_events_from_policy_decisions([d]), [])

    def test_from_dict_rejects_unknown_event_type(self):
        d = _evt().to_dict()
        d["event_type"] = "made_up.type"
        restored = Event.from_dict(d)
        self.assertEqual(restored.event_type, EVENT_UNKNOWN)


# ---------------------------------------------------------------------------
# Policy decisions — approval flow mapping
# ---------------------------------------------------------------------------

class TestPolicyDecisionApprovalMapping(unittest.TestCase):
    def test_approval_granted(self):
        d = _policy_decision(approval_required=True, approval_granted=True)
        events = build_events_from_policy_decisions([d])
        self.assertEqual(events[0].event_type, EVENT_APPROVAL_GRANTED)
        self.assertEqual(events[0].status, STATUS_PASSED)

    def test_approval_denied(self):
        d = _policy_decision(approval_required=True, approval_granted=False)
        events = build_events_from_policy_decisions([d])
        self.assertEqual(events[0].event_type, EVENT_APPROVAL_DENIED)
        self.assertEqual(events[0].status, STATUS_FAILED)

    def test_approval_requested_when_pending(self):
        d = _policy_decision(approval_required=True, approval_granted=None)
        events = build_events_from_policy_decisions([d])
        self.assertEqual(events[0].event_type, EVENT_APPROVAL_REQUESTED)

    def test_non_approval_decision_maps_to_policy_checked(self):
        d = _policy_decision(approval_required=False)
        events = build_events_from_policy_decisions([d])
        self.assertEqual(events[0].event_type, EVENT_POLICY_CHECKED)

    def test_actor_never_set_from_decision_source(self):
        d = {**_policy_decision(), "source": "path_policy"}
        events = build_events_from_policy_decisions([d])
        self.assertIsNone(events[0].actor)

    def test_resource_not_in_metadata(self):
        d = {**_policy_decision(), "resource": "/home/user/secret.tf"}
        events = build_events_from_policy_decisions([d])
        self.assertNotIn("resource", events[0].metadata)


# ---------------------------------------------------------------------------
# Review checks — independent verification evidence
# ---------------------------------------------------------------------------

class TestReviewCheckEvidence(unittest.TestCase):
    def test_passed_check_is_independently_verified(self):
        events = build_events_from_review_checks([_check(status="passed")])
        self.assertEqual(events[0].evidence, EVIDENCE_INDEPENDENTLY_VERIFIED)

    def test_failed_check_is_independently_verified(self):
        events = build_events_from_review_checks([_check(status="failed")])
        self.assertEqual(events[0].evidence, EVIDENCE_INDEPENDENTLY_VERIFIED)

    def test_skipped_check_is_not_independently_verified(self):
        events = build_events_from_review_checks([_check(status="skipped")])
        self.assertNotEqual(events[0].evidence, EVIDENCE_INDEPENDENTLY_VERIFIED)

    def test_source_is_review_checks(self):
        events = build_events_from_review_checks([_check()])
        self.assertEqual(events[0].source, SOURCE_REVIEW_CHECKS)


# ---------------------------------------------------------------------------
# events_from_entry — aggregation across embedded sources
# ---------------------------------------------------------------------------

class TestEventsFromEntry(unittest.TestCase):
    def test_entry_with_all_embedded_sources_produces_all(self):
        entry = {
            "shard_id": "shard-20260601-0001",
            "run_id": "run-1",
            "attempt_number": 1,
            "run_timeline": [_timeline_event()],
            "review_checks": [_check()],
            "policy_decisions": [_policy_decision()],
        }
        events = events_from_entry(entry)
        sources = {e.source for e in events}
        self.assertEqual(sources, {SOURCE_RUN_TIMELINE, SOURCE_REVIEW_CHECKS, SOURCE_POLICY_DECISIONS})

    def test_linkage_threaded_into_every_event(self):
        entry = {
            "shard_id": "shard-1",
            "run_id": "run-1",
            "attempt_number": 3,
            "review_checks": [_check()],
        }
        events = events_from_entry(entry)
        for e in events:
            self.assertEqual(e.run_id, "run-1")
            self.assertEqual(e.shard_id, "shard-1")
            self.assertEqual(e.attempt_number, 3)

    def test_never_raises_on_garbage_input(self):
        bad_entries = [
            {"run_timeline": "not-a-list", "review_checks": 99},
            {"run_timeline": [None, None]},
            {"policy_decisions": [None, "bad"]},
            {"shard_id": 12345, "review_checks": [{"no_status": True}]},
        ]
        for entry in bad_entries:
            with self.subTest(entry=entry):
                result = events_from_entry(entry)
                self.assertIsInstance(result, list)

    def test_all_events_json_serializable(self):
        entry = {
            "shard_id": "shard-1",
            "run_timeline": [_timeline_event()],
            "review_checks": [_check()],
            "policy_decisions": [_policy_decision()],
        }
        events = events_from_entry(entry)
        json.dumps([e.to_dict() for e in events])  # must not raise

    def test_no_unsafe_values_in_output(self):
        entry = {
            "shard_id": "shard-safe",
            "run_timeline": [_timeline_event(target="C:\\Users\\admin\\secret.py")],
            "review_checks": [_check()],
        }
        events = events_from_entry(entry)
        _assert_no_unsafe(json.dumps([e.to_dict() for e in events]))
