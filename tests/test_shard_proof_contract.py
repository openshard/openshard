"""Tests for openshard.history.proof_contract - Shard Proof Contract v1.

Covers old-record compatibility, malformed input, missing fields, unsafe field
stripping, section statuses, overall status, and JSON serializability.
"""

from __future__ import annotations

import json
import unittest

from openshard.history.proof_contract import (
    LEVEL_REQUIRED,
    OVERALL_STATUSES,
    OVERALL_STRONG,
    OVERALL_UNKNOWN,
    OVERALL_UNSAFE,
    OVERALL_USABLE,
    REQUIRED_SECTIONS,
    SECTION_NAMES,
    SECTION_STATUSES,
    SHARD_PROOF_CONTRACT_VERSION,
    build_shard_proof_contract,
    validate_shard_proof_contract,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _strong_entry(**kwargs) -> dict:
    """An entry that should populate every required and recommended section."""
    entry = {
        "schema_version": "1.2",
        "shard_id": "shard-20260604-0001",
        "timestamp": "2026-06-04T10:00:00Z",
        "task": "implement a feature",
        "workflow": "native",
        "execution_profile": "native_deep",
        "execution_model": "openrouter/some-model",
        "duration_seconds": 12.5,
        "estimated_cost": 0.0123,
        "files_created": 1,
        "files_updated": 1,
        "files_deleted": 0,
        "files_detail": [{"path": "a.py", "change_type": "create", "summary": ""}],
        "verification_attempted": True,
        "verification_passed": True,
        "review_checks": [
            {"name": "ruff", "status": "passed"},
            {"name": "pytest", "status": "passed"},
        ],
        "context_files_injected_count": 4,
        "context_utilisation_ratio": 0.5,
        "policy_decisions": [{"decision_id": "p1", "decision": "allow"}],
        "approval_receipt": {"granted": True, "reason": "ok"},
        "run_timeline": [
            {"event": "run_started", "label": "Run started", "kind": "run", "status": "completed"},
        ],
        "evidence_capsules": [
            {"capsule_id": "c1", "kind": "inspection", "summary": "read a.py"},
        ],
        "git_base_branch": "main",
        "git_head_commit_hash": "abc1234",
        "git_dirty": False,
        "summary": "Implemented the feature and verified it.",
        "developer_feedback": {"rating": "good", "action": "accepted"},
    }
    entry.update(kwargs)
    return entry


def _section(contract: dict, name: str) -> dict:
    return next(s for s in contract["sections"] if s["name"] == name)


def _no_unsafe_substrings(text: str) -> None:
    for needle in (
        "C:\\", "C:/", "/Users/", "/home/", "/etc/",
        "sk-", "AKIA", "api_key=", "password=", "secret=",
        "raw diff body", "BEGIN PRIVATE KEY",
    ):
        assert needle.lower() not in text.lower(), f"unsafe substring {needle!r} leaked"


# ---------------------------------------------------------------------------
# Structural / shape tests
# ---------------------------------------------------------------------------


class TestContractShape(unittest.TestCase):
    def test_version_constant(self) -> None:
        self.assertEqual(SHARD_PROOF_CONTRACT_VERSION, "1.0")

    def test_seventeen_canonical_sections(self) -> None:
        self.assertEqual(len(SECTION_NAMES), 17)
        # trust is deliberately NOT a section (consumer, not section).
        self.assertNotIn("trust", SECTION_NAMES)

    def test_required_floor(self) -> None:
        self.assertEqual(
            set(REQUIRED_SECTIONS),
            {"task", "executor", "model", "actions", "verification", "result"},
        )

    def test_output_has_all_top_level_keys(self) -> None:
        contract = build_shard_proof_contract(_strong_entry())
        for key in (
            "contract_version",
            "sections",
            "overall_status",
            "missing_required_sections",
            "weak_recommended_sections",
            "unsafe_findings",
            "summary",
        ):
            self.assertIn(key, contract)

    def test_one_result_per_section_in_order(self) -> None:
        contract = build_shard_proof_contract(_strong_entry())
        names = [s["name"] for s in contract["sections"]]
        self.assertEqual(names, list(SECTION_NAMES))

    def test_validate_passes_for_well_formed_output(self) -> None:
        for entry in (_strong_entry(), {"task": "x"}, {}, None):
            contract = build_shard_proof_contract(entry)
            self.assertEqual(validate_shard_proof_contract(contract), [])


# ---------------------------------------------------------------------------
# Strong / current record
# ---------------------------------------------------------------------------


class TestStrongRecord(unittest.TestCase):
    def test_strong_overall(self) -> None:
        contract = build_shard_proof_contract(_strong_entry())
        self.assertEqual(contract["overall_status"], OVERALL_STRONG)
        self.assertEqual(contract["missing_required_sections"], [])
        self.assertEqual(contract["weak_recommended_sections"], [])
        self.assertEqual(contract["unsafe_findings"], [])

    def test_required_sections_present(self) -> None:
        contract = build_shard_proof_contract(_strong_entry())
        for name in REQUIRED_SECTIONS:
            self.assertEqual(_section(contract, name)["status"], "present", name)

    def test_verification_detail_is_status_token(self) -> None:
        contract = build_shard_proof_contract(_strong_entry())
        verif = _section(contract, "verification")
        self.assertEqual(verif["status"], "present")
        self.assertEqual(verif["detail"], "passed")

    def test_summary_is_safe_string(self) -> None:
        contract = build_shard_proof_contract(_strong_entry())
        self.assertIn("sections present", contract["summary"])

    def test_partial_required_verification_is_usable_not_strong(self) -> None:
        # Verification recorded as "not run" -> partial required section.
        entry = _strong_entry()
        entry["verification_attempted"] = False
        entry["verification_passed"] = None
        entry.pop("review_checks")
        contract = build_shard_proof_contract(entry)
        self.assertEqual(_section(contract, "verification")["status"], "partial")
        # A partial required section must prevent strong.
        self.assertNotEqual(contract["overall_status"], OVERALL_STRONG)
        self.assertEqual(contract["overall_status"], OVERALL_USABLE)
        # It is partial, not missing/unknown, so it is not a missing required.
        self.assertEqual(contract["missing_required_sections"], [])


# ---------------------------------------------------------------------------
# Old / minimal record compatibility
# ---------------------------------------------------------------------------


class TestOldRecordCompatibility(unittest.TestCase):
    def test_minimal_entry_no_crash(self) -> None:
        contract = build_shard_proof_contract({"task": "do a thing"})
        self.assertIn(contract["overall_status"], OVERALL_STATUSES)
        # task present, but most required missing -> weak / partial, never strong.
        self.assertNotEqual(contract["overall_status"], OVERALL_STRONG)
        self.assertEqual(_section(contract, "task")["status"], "present")

    def test_no_schema_version_still_evaluated(self) -> None:
        entry = {"task": "x", "timestamp": "2020-01-01T00:00:00Z"}
        contract = build_shard_proof_contract(entry)
        # verification has no recorded state -> unknown (distinct from missing).
        self.assertEqual(_section(contract, "verification")["status"], "unknown")
        self.assertIn("verification", contract["missing_required_sections"])

    def test_empty_entry_is_weak_not_unsafe(self) -> None:
        contract = build_shard_proof_contract({})
        self.assertNotEqual(contract["overall_status"], OVERALL_UNSAFE)
        self.assertNotEqual(contract["overall_status"], OVERALL_UNKNOWN)


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


class TestMalformedInput(unittest.TestCase):
    def test_non_dict_inputs_return_unknown(self) -> None:
        for bad in (None, [], "a string", 42, 3.14, ("a", "b")):
            contract = build_shard_proof_contract(bad)
            self.assertEqual(contract["overall_status"], OVERALL_UNKNOWN)
            self.assertEqual(validate_shard_proof_contract(contract), [])

    def test_nested_junk_does_not_crash(self) -> None:
        entry = {
            "task": "x",
            "policy_decisions": "not-a-list",
            "files_detail": {"unexpected": "dict"},
            "run_timeline": 123,
            "evidence_capsules": None,
        }
        contract = build_shard_proof_contract(entry)
        self.assertEqual(validate_shard_proof_contract(contract), [])


# ---------------------------------------------------------------------------
# Conditional / partial sections
# ---------------------------------------------------------------------------


class TestSectionStatuses(unittest.TestCase):
    def test_policy_not_applicable_when_absent(self) -> None:
        entry = _strong_entry()
        entry.pop("policy_decisions")
        contract = build_shard_proof_contract(entry)
        self.assertEqual(_section(contract, "policy")["status"], "not_applicable")

    def test_approval_not_applicable_when_absent(self) -> None:
        entry = _strong_entry()
        entry.pop("approval_receipt")
        contract = build_shard_proof_contract(entry)
        self.assertEqual(_section(contract, "approval")["status"], "not_applicable")

    def test_verification_not_run_is_partial(self) -> None:
        entry = _strong_entry()
        entry["verification_attempted"] = False
        entry["verification_passed"] = None
        # review_checks would force a "Checks:" status; remove so the run
        # reflects a genuine "verification not run" state.
        entry.pop("review_checks")
        contract = build_shard_proof_contract(entry)
        verif = _section(contract, "verification")
        self.assertEqual(verif["status"], "partial")
        self.assertEqual(verif["detail"], "not_run")
        # partial required does not count as missing required.
        self.assertNotIn("verification", contract["missing_required_sections"])

    def test_verification_skipped_is_partial(self) -> None:
        entry = _strong_entry()
        entry.pop("review_checks", None)
        entry["verification_attempted"] = False
        entry["verification_passed"] = None
        entry["osn_verification_contract"] = {
            "enabled": True,
            "status": "skipped",
            "skipped_reason": "needs_approval: medium-risk command",
        }
        contract = build_shard_proof_contract(entry)
        verif = _section(contract, "verification")
        self.assertEqual(verif["status"], "partial")
        self.assertEqual(verif["detail"], "skipped")
        self.assertNotIn("verification", contract["missing_required_sections"])

    def test_verification_manual_review_is_partial_not_unknown(self) -> None:
        entry = _strong_entry()
        entry.pop("review_checks", None)
        entry["verification_attempted"] = False
        entry["verification_passed"] = None
        entry["osn_verification_contract"] = {
            "enabled": True,
            "status": "manual_review",
            "manual_review_required": True,
        }
        contract = build_shard_proof_contract(entry)
        verif = _section(contract, "verification")
        self.assertEqual(verif["status"], "partial")
        self.assertEqual(verif["detail"], "manual_review")

    def test_cost_partial_when_only_duration(self) -> None:
        entry = _strong_entry()
        entry.pop("estimated_cost")
        contract = build_shard_proof_contract(entry)
        self.assertEqual(_section(contract, "cost")["status"], "partial")

    def test_files_partial_when_only_inspected(self) -> None:
        entry = _strong_entry()
        entry.pop("files_detail")
        entry["files_created"] = 0
        entry["files_updated"] = 0
        entry["file_context"] = {"files_read": 2, "paths": ["a.py", "b.py"]}
        contract = build_shard_proof_contract(entry)
        self.assertEqual(_section(contract, "files")["status"], "partial")

    def _unobservable_files_entry(self, **kwargs) -> dict:
        entry = _strong_entry(**kwargs)
        entry.pop("files_detail")
        entry["files_created"] = 0
        entry["files_updated"] = 0
        entry["changes"] = {"agent_reported": 0, "git_observed": 0, "files_observable": False}
        return entry

    def test_unobservable_file_changes_are_not_claimed_as_no_changes(self) -> None:
        contract = build_shard_proof_contract(self._unobservable_files_entry())
        actions = _section(contract, "actions")
        self.assertEqual((actions["status"], actions["detail"]), ("partial", "file_changes_not_observable"))
        files = _section(contract, "files")
        self.assertEqual((files["status"], files["detail"]), ("unknown", "not_observable"))
        self.assertNotEqual(contract["overall_status"], OVERALL_STRONG)
        self.assertIn("files", contract["weak_recommended_sections"])

    def test_unobservable_file_changes_without_result_is_unknown_required(self) -> None:
        contract = build_shard_proof_contract(self._unobservable_files_entry(summary=""))
        actions = _section(contract, "actions")
        self.assertEqual((actions["status"], actions["detail"]), ("unknown", "not_observable"))
        self.assertIn("actions", contract["missing_required_sections"])

    def test_zero_files_on_observable_capture_is_still_no_file_changes(self) -> None:
        entry = self._unobservable_files_entry()
        entry["changes"] = {"agent_reported": 0, "git_observed": 0}
        contract = build_shard_proof_contract(entry)
        actions = _section(contract, "actions")
        self.assertEqual((actions["status"], actions["detail"]), ("present", "no_file_changes"))
        self.assertEqual(_section(contract, "files")["status"], "missing")

    def test_missing_required_section_drives_partial_overall(self) -> None:
        entry = _strong_entry()
        entry["task"] = ""
        contract = build_shard_proof_contract(entry)
        self.assertIn("task", contract["missing_required_sections"])
        self.assertIn(contract["overall_status"], (OVERALL_USABLE, "partial", "weak"))
        self.assertNotEqual(contract["overall_status"], OVERALL_STRONG)

    def test_every_section_status_is_valid_enum(self) -> None:
        contract = build_shard_proof_contract(_strong_entry())
        for section in contract["sections"]:
            self.assertIn(section["status"], SECTION_STATUSES)
            self.assertIn(section["level"], {"required", "recommended", "optional", "conditional"})


# ---------------------------------------------------------------------------
# Unsafe field stripping / findings
# ---------------------------------------------------------------------------


class TestUnsafe(unittest.TestCase):
    def test_blocked_field_flags_unsafe_and_is_stripped(self) -> None:
        entry = _strong_entry(raw_diff="raw diff body that must never appear")
        contract = build_shard_proof_contract(entry)
        self.assertEqual(contract["overall_status"], OVERALL_UNSAFE)
        self.assertIn("blocked_field:raw_diff", contract["unsafe_findings"])
        # The raw value must not leak anywhere in the serialized output.
        _no_unsafe_substrings(json.dumps(contract))

    def test_blocked_field_nested_in_list_flags_unsafe_and_is_stripped(self) -> None:
        # A blocked field buried inside a list of sub-records must still be
        # detected, and its raw value must never leak into the output.
        entry = _strong_entry(
            stage_runs=[
                {"stage_type": "implementation", "model": "x"},
                {"stage_type": "verify", "raw_transcript": "BEGIN PRIVATE KEY secret blob"},
            ]
        )
        contract = build_shard_proof_contract(entry)
        self.assertEqual(contract["overall_status"], OVERALL_UNSAFE)
        self.assertIn("blocked_field:raw_transcript", contract["unsafe_findings"])
        _no_unsafe_substrings(json.dumps(contract))

    def test_secret_scan_capsule_flags_unsafe(self) -> None:
        entry = _strong_entry(
            evidence_capsules=[
                {"capsule_id": "c1", "kind": "secret_scan", "summary": "secret detected"},
            ]
        )
        contract = build_shard_proof_contract(entry)
        self.assertEqual(contract["overall_status"], OVERALL_UNSAFE)
        self.assertTrue(
            any(f.startswith("secret_scan_findings:") for f in contract["unsafe_findings"])
        )
        self.assertEqual(_section(contract, "checks")["status"], "unsafe")

    def test_absolute_path_and_secret_not_echoed_in_details(self) -> None:
        entry = _strong_entry(
            execution_model="C:\\secrets\\sk-ABCDEFGH12345678ABCDEFGH model",
        )
        contract = build_shard_proof_contract(entry)
        _no_unsafe_substrings(json.dumps(contract))


# ---------------------------------------------------------------------------
# JSON serializability
# ---------------------------------------------------------------------------


class TestJsonSerializable(unittest.TestCase):
    def test_all_fixtures_serialize(self) -> None:
        for entry in (
            _strong_entry(),
            {"task": "x"},
            {},
            None,
            {"task": "x", "policy_decisions": "bad"},
            _strong_entry(raw_diff="leak"),
        ):
            contract = build_shard_proof_contract(entry)
            # Round-trips cleanly.
            reparsed = json.loads(json.dumps(contract))
            self.assertEqual(reparsed["contract_version"], "1.0")


# ---------------------------------------------------------------------------
# validate_shard_proof_contract negative cases
# ---------------------------------------------------------------------------


class TestValidate(unittest.TestCase):
    def test_rejects_non_dict(self) -> None:
        self.assertTrue(validate_shard_proof_contract("nope"))

    def test_rejects_bad_overall_status(self) -> None:
        contract = build_shard_proof_contract(_strong_entry())
        contract["overall_status"] = "bogus"
        self.assertIn("invalid overall_status: 'bogus'", validate_shard_proof_contract(contract))

    def test_rejects_unknown_section_name(self) -> None:
        contract = build_shard_proof_contract(_strong_entry())
        contract["sections"][0]["name"] = "not_a_section"
        problems = validate_shard_proof_contract(contract)
        self.assertTrue(any("unknown name" in p for p in problems))

    def test_required_level_constant(self) -> None:
        self.assertEqual(LEVEL_REQUIRED, "required")


if __name__ == "__main__":
    unittest.main()
