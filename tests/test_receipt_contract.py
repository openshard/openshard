"""Receipt Contract v2 (v0.5): state derivation, fixtures, rendering, CLI surfaces."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history import receipt_contract as rc
from openshard.history.receipt_contract import (
    RECEIPT_CONTRACT_VERSION,
    RECEIPT_V2_FIELDS,
    STATE_APPROVAL_REQUIRED,
    STATE_APPROVED,
    STATE_BLOCKED,
    STATE_UNVERIFIED,
    STATE_VERIFICATION_FAILED,
    STATE_VERIFIED,
    STATE_VERIFIED_AFTER_ESCALATION,
    STATE_VERIFIED_AFTER_RETRY,
    VALID_STATES,
    build_receipt_contract,
    derive_receipt_state,
    entry_has_v2_fields,
    receipt_questions,
    render_receipt_state_block,
)
from openshard.history.shard_contract import (
    build_shard_receipt,
    render_compact_shard_receipt,
    render_full_shard_receipt,
)
from openshard.history.shard_hash import compute_shard_hash
from openshard.history.shard_schema import SHARD_BLOCKED_FIELDS
from openshard.history.views import receipt_to_dict

FIXTURES = Path(__file__).parent / "fixtures" / "receipts" / "v2"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _contract_for(name: str):
    doc = _fixture(name)
    entries = doc["entries"]
    return build_receipt_contract(entries[-1], index=len(entries) - 1, siblings=entries[:-1]), doc


def _legacy_entry(**kw) -> dict:
    e = {
        "schema_version": "1.2",
        "timestamp": "2026-01-01T00:00:00Z",
        "run_id": "2026-01-01T00:00:00Z",
        "shard_id": "shard-20260101-0001",
        "attempt_number": 1,
        "task": "Add tests for the auth module",
        "execution_model": "openai/gpt-5.5",
        "retry_triggered": False,
        "duration_seconds": 1.0,
        "files_created": 1,
        "files_updated": 0,
        "files_deleted": 0,
        "verification_attempted": True,
        "verification_passed": True,
        "summary": "done",
        "estimated_cost": 0.01,
    }
    e.update(kw)
    return e


# ---------------------------------------------------------------------------
# State derivation truth table
# ---------------------------------------------------------------------------


class TestDeriveReceiptState(unittest.TestCase):
    def _state(self, **kw) -> str:
        args = dict(verification_status="passed", policy_decision="allow",
                    approval_status="not_required", retries=0, escalation_occurred=False)
        args.update(kw)
        return derive_receipt_state(**args)[0]

    def test_verified(self):
        self.assertEqual(self._state(), STATE_VERIFIED)

    def test_policy_deny_wins_over_everything(self):
        self.assertEqual(self._state(policy_decision="deny", approval_status="granted"), STATE_BLOCKED)
        self.assertEqual(self._state(policy_decision="deny", verification_status="failed"), STATE_BLOCKED)

    def test_denied_approval_is_blocked(self):
        self.assertEqual(self._state(approval_status="denied"), STATE_BLOCKED)

    def test_pending_approval(self):
        self.assertEqual(self._state(approval_status="pending", verification_status="not_run"), STATE_APPROVAL_REQUIRED)
        # Even a passed verification does not release a pending approval.
        self.assertEqual(self._state(approval_status="pending"), STATE_APPROVAL_REQUIRED)

    def test_verification_failed(self):
        self.assertEqual(self._state(verification_status="failed"), STATE_VERIFICATION_FAILED)
        self.assertEqual(self._state(verification_status="failed", approval_status="granted"), STATE_VERIFICATION_FAILED)

    def test_retry_and_escalation(self):
        self.assertEqual(self._state(retries=1), STATE_VERIFIED_AFTER_RETRY)
        self.assertEqual(self._state(retries=2, escalation_occurred=True), STATE_VERIFIED_AFTER_ESCALATION)
        # Escalation without a verified result is not a success state.
        self.assertEqual(self._state(retries=1, escalation_occurred=True, verification_status="failed"),
                         STATE_VERIFICATION_FAILED)

    def test_approved_is_not_verified(self):
        for vs in ("not_run", "skipped", "unknown", "manual_review"):
            self.assertEqual(self._state(verification_status=vs, approval_status="granted"), STATE_APPROVED, vs)

    def test_unverified(self):
        for vs in ("not_run", "skipped", "unknown", "manual_review"):
            state, reason, evidence = derive_receipt_state(
                verification_status=vs, policy_decision="allow", approval_status="not_required",
                retries=None, escalation_occurred=False)
            self.assertEqual(state, STATE_UNVERIFIED, vs)
            self.assertTrue(reason)
            self.assertIn(f"verification={vs}", evidence)

    def test_every_state_reachable_and_valid(self):
        seen = {
            self._state(), self._state(policy_decision="deny"), self._state(approval_status="pending"),
            self._state(verification_status="failed"), self._state(retries=1),
            self._state(retries=1, escalation_occurred=True),
            self._state(verification_status="not_run", approval_status="granted"),
            self._state(verification_status="not_run"),
        }
        self.assertEqual(seen, VALID_STATES)


# ---------------------------------------------------------------------------
# Fixtures: the five representative scenarios
# ---------------------------------------------------------------------------


class TestFixtures(unittest.TestCase):
    def test_all_fixtures_have_valid_hashes_and_expected_state(self):
        for path in sorted(FIXTURES.glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("DEMO DATA", doc["seed_label"])
            for e in doc["entries"]:
                self.assertEqual(e["content_hash"], compute_shard_hash(e), path.name)
                self.assertEqual(e["schema_version"], "1.2", "fixtures are valid v0.4.3 records")
            c = build_receipt_contract(doc["entries"][-1], siblings=doc["entries"][:-1])
            self.assertEqual(c.state, doc["expected"]["state"], path.name)
            self.assertEqual(c.integrity.hash_status, "valid", path.name)
            self.assertEqual(c.contract_version, RECEIPT_CONTRACT_VERSION)

    def test_01_verified(self):
        c, doc = _contract_for("01_verified_coding_task")
        self.assertEqual(c.state, STATE_VERIFIED)
        self.assertTrue(c.is_verified())
        self.assertEqual(c.actors.owner.id, "team:platform")
        self.assertEqual(c.actors.requested_by.display, "Michael")
        self.assertEqual(c.actors.executed_by.kind, "agent")
        self.assertIsNone(c.actors.approved_by)
        self.assertEqual(c.verification.status, "passed")
        self.assertTrue(c.verification.independent)
        self.assertEqual(c.verification.checks[0].verifier_kind, "test_runner")
        self.assertEqual(c.cost.generation_usd, 0.0412)
        self.assertEqual(c.cost.verification_usd, 0.0)
        self.assertEqual(c.cost.cost_per_verified_success_usd, 0.0412)
        self.assertEqual(c.outcome.status, "merged")
        self.assertEqual(c.policy.name, "default-coding")
        self.assertEqual(c.policy.decision, "allow")
        self.assertEqual(c.capture.coverage, "complete")
        self.assertEqual(c.permissions.used, ["read:repo", "write:src/**", "shell:pytest"])

    def test_02_approval_granted_by_named_human(self):
        c, _ = _contract_for("02_high_risk_approval_required")
        self.assertEqual(c.state, STATE_VERIFIED)
        self.assertTrue(c.approval.required)
        self.assertEqual(c.approval.status, "granted")
        self.assertEqual(c.approval.mechanism, "dashboard")
        self.assertEqual(c.approval.approver.id, "user:ops-lead")
        self.assertEqual(c.actors.approved_by.id, "user:ops-lead")
        # Owner, requester and approver are distinct principals.
        self.assertNotEqual(c.actors.owner.id, c.actors.approved_by.id)
        self.assertNotEqual(c.actors.requested_by.id, c.actors.approved_by.id)
        self.assertEqual(c.policy.decision, "ask")
        self.assertIn("human approval", c.policy.reason)

    def test_02b_pending(self):
        c, _ = _contract_for("02b_approval_pending")
        self.assertEqual(c.state, STATE_APPROVAL_REQUIRED)
        self.assertEqual(c.approval.status, "pending")
        self.assertIsNone(c.approval.approver)
        self.assertIsNone(c.cost.cost_per_verified_success_usd)

    def test_03_blocked(self):
        c, _ = _contract_for("03_blocked_by_policy")
        self.assertEqual(c.state, STATE_BLOCKED)
        self.assertEqual(c.policy.decision, "deny")
        self.assertEqual(c.policy.deny_count, 1)
        self.assertIn("force-push", c.policy.reason)
        self.assertEqual(c.permissions.denied, ["shell:git push --force"])
        self.assertEqual(c.permissions.blocked_commands_count, 1)
        self.assertIsNone(c.cost.cost_per_verified_success_usd)

    def test_04_verification_failed(self):
        c, _ = _contract_for("04_verification_failed")
        self.assertEqual(c.state, STATE_VERIFICATION_FAILED)
        self.assertEqual(c.verification.status, "failed")
        self.assertEqual(c.verification.returncode, 1)
        self.assertFalse(c.verification.raw_output_stored)
        self.assertIsNone(c.cost.cost_per_verified_success_usd)
        self.assertEqual(c.cost.total_usd, 0.052)

    def test_05_escalation(self):
        c, doc = _contract_for("05_escalation_then_verified")
        self.assertEqual(c.state, STATE_VERIFIED_AFTER_ESCALATION)
        self.assertEqual(c.attempts.attempts_observed, 2)
        self.assertEqual(c.attempts.retries, 1)
        self.assertTrue(c.attempts.escalation.occurred)
        self.assertEqual(c.attempts.escalation.from_model, "anthropic/claude-haiku-4.5")
        self.assertEqual(c.attempts.escalation.to_model, "anthropic/claude-sonnet-4.6")
        self.assertEqual(len(c.attempts.prior_attempts), 1)
        self.assertEqual(c.attempts.prior_attempts[0].verification_status, "failed")
        # Cost per verified success covers both attempts (0.006 + 0.048).
        self.assertAlmostEqual(c.cost.attempts_total_usd, 0.054)
        self.assertAlmostEqual(c.cost.cost_per_verified_success_usd, 0.054)
        self.assertEqual(c.cost.total_usd, 0.048)

    def test_05_escalation_derived_from_siblings_without_block(self):
        doc = _fixture("05_escalation_then_verified")
        a, b = doc["entries"]
        b = dict(b)
        del b["escalation"]
        c = build_receipt_contract(b, siblings=[a])
        self.assertTrue(c.attempts.escalation.occurred)
        self.assertEqual(c.attempts.escalation.source, "derived:sibling_attempts")
        self.assertEqual(c.state, STATE_VERIFIED_AFTER_ESCALATION)
        # Without siblings and without the block, only the retry is knowable.
        c2 = build_receipt_contract(b)
        self.assertEqual(c2.state, STATE_VERIFIED_AFTER_RETRY)


# ---------------------------------------------------------------------------
# Backwards compatibility with v0.4.3 records
# ---------------------------------------------------------------------------


class TestLegacyEntries(unittest.TestCase):
    def test_legacy_verified_entry(self):
        c = build_receipt_contract(_legacy_entry())
        self.assertEqual(c.state, STATE_VERIFIED)
        self.assertEqual(c.v2_fields_present, [])
        self.assertIsNone(c.actors.owner)
        self.assertIsNone(c.actors.requested_by)
        self.assertEqual(c.actors.executed_by.display, "OpenShard")
        self.assertEqual(c.approval.status, "not_required")
        self.assertEqual(c.execution.model, "openai/gpt-5.5")
        self.assertEqual(c.execution.provider, "openai")
        self.assertEqual(c.cost.generation_usd, 0.01)
        self.assertEqual(c.cost.provenance, "openshard_estimated")
        self.assertEqual(c.outcome.status, "unknown")
        self.assertEqual(c.integrity.hash_status, "missing")

    def test_legacy_pipeline_verification_is_independent(self):
        c = build_receipt_contract(_legacy_entry())
        self.assertEqual(len(c.verification.checks), 1)
        self.assertEqual(c.verification.checks[0].verifier_kind, "openshard_runner")
        self.assertTrue(c.verification.independent)

    def test_external_hooks_entry_is_partial_and_unverified(self):
        e = _legacy_entry(executor="claude_code_hooks", verification_attempted=True, verification_passed=None,
                          capture={"agent": "claude_code", "hook_events_dropped": 0, "session_end_observed": False,
                                   "task_status": "turn_completed", "models_seen": ["claude-sonnet-4-6"]})
        del e["retry_triggered"]
        c = build_receipt_contract(e)
        self.assertEqual(c.state, STATE_UNVERIFIED)
        self.assertEqual(c.capture.coverage, "partial")
        self.assertFalse(c.capture.executed_by_openshard)
        self.assertEqual(c.verification.checks[0].verifier_kind, "agent_reported")
        self.assertFalse(c.verification.independent)
        self.assertIn("did not execute", c.capture.note)

    def test_dropped_hook_events_mark_partial_coverage(self):
        e = _legacy_entry(capture={"hook_events_dropped": 3})
        c = build_receipt_contract(e)
        self.assertEqual(c.capture.coverage, "partial")
        self.assertEqual(c.capture.hook_events_dropped, 3)
        self.assertIn("3 hook event(s) were dropped", c.capture.note)

    def test_legacy_approval_receipt_fields(self):
        e = _legacy_entry(approval_request={"requires_approval": True, "action": "write"},
                          approval_receipt={"granted": False, "reason": "declined by user"})
        c = build_receipt_contract(e)
        self.assertEqual(c.state, STATE_BLOCKED)
        self.assertEqual(c.approval.status, "denied")
        self.assertEqual(c.approval.reason, "declined by user")
        self.assertIsNone(c.approval.approver, "no approver identity is ever invented")

    def test_legacy_retry_counts(self):
        c = build_receipt_contract(_legacy_entry(retry_triggered=True, retry_estimated_cost=0.02))
        self.assertEqual(c.state, STATE_VERIFIED_AFTER_RETRY)
        self.assertEqual(c.attempts.retries, 1)
        self.assertEqual(c.cost.retry_usd, 0.02)
        self.assertAlmostEqual(c.cost.total_usd, 0.03)

    def test_feedback_outcome_surfaces_without_claiming_a_final_outcome(self):
        c = build_receipt_contract(_legacy_entry(developer_feedback={"outcome": "rejected", "manual_fix_required": True}))
        self.assertEqual(c.outcome.status, "unknown")
        self.assertEqual(c.outcome.feedback_outcome, "rejected")
        self.assertTrue(c.outcome.human_intervention)


# ---------------------------------------------------------------------------
# Robustness and safety
# ---------------------------------------------------------------------------


class TestRobustness(unittest.TestCase):
    def test_never_raises_on_garbage(self):
        for bad in (None, 42, "x", [], {}, {"task": None, "actors": "nope", "policy": [1], "verifiers": "x",
                                          "cost_breakdown": {"generation_usd": "abc"}, "escalation": 7}):
            c = build_receipt_contract(bad)
            self.assertIn(c.state, VALID_STATES)
            json.dumps(c.to_dict())

    def test_to_dict_is_json_safe_and_versioned(self):
        c, _ = _contract_for("05_escalation_then_verified")
        d = c.to_dict()
        json.dumps(d)
        self.assertEqual(d["contract_version"], "2.0")
        for key in ("actors", "execution", "permissions", "policy", "approval", "verification",
                    "capture", "attempts", "cost", "outcome", "integrity", "state", "state_reason"):
            self.assertIn(key, d)

    def test_blocked_fields_never_appear(self):
        e = _legacy_entry(raw_prompt="secret prompt", actors={"owner": {"kind": "user", "id": "u1", "transcript": "x"}})
        d = build_receipt_contract(e).to_dict()
        text = json.dumps(d)
        for name in SHARD_BLOCKED_FIELDS:
            self.assertNotIn(f'"{name}"', text)
        self.assertNotIn("secret prompt", text)

    def test_secret_like_and_absolute_values_are_dropped(self):
        e = _legacy_entry(actors={"owner": {"kind": "user", "id": "sk-abcdefghijklmnop1234", "display": "/Users/me/x"}},
                          policy={"policy_id": "pol", "reason": "token=abcd1234efgh"})
        c = build_receipt_contract(e)
        text = json.dumps(c.to_dict())
        self.assertNotIn("sk-abcdefghijklmnop1234", text)
        self.assertNotIn("/Users/me", text)
        self.assertNotIn("abcd1234efgh", text)

    def test_tampered_record_reports_mismatch(self):
        e = _legacy_entry()
        e["content_hash"] = compute_shard_hash(e)
        e["verification_passed"] = False
        c = build_receipt_contract(e)
        self.assertEqual(c.integrity.hash_status, "mismatch")

    def test_unknown_vocabulary_tokens_fall_back(self):
        e = _legacy_entry(approval={"required": True, "status": "granted", "mechanism": "carrier-pigeon",
                                    "approver": {"kind": "wizard", "id": "w"}},
                          outcome={"status": "teleported"},
                          verifiers=[{"check": "x", "status": "maybe", "verifier_kind": "oracle"}])
        c = build_receipt_contract(e)
        self.assertEqual(c.approval.mechanism, "unknown")
        self.assertEqual(c.approval.approver.kind, "unknown")
        self.assertEqual(c.outcome.status, "unknown")
        self.assertEqual(c.verification.checks[0].status, "unknown")
        self.assertEqual(c.verification.checks[0].verifier_kind, "unknown")

    def test_entry_has_v2_fields(self):
        self.assertEqual(entry_has_v2_fields(_legacy_entry()), [])
        self.assertEqual(entry_has_v2_fields(_legacy_entry(actors={}, policy={"policy_id": "p"})), ["policy"])
        self.assertEqual(entry_has_v2_fields("nope"), [])
        self.assertTrue(RECEIPT_V2_FIELDS.isdisjoint(SHARD_BLOCKED_FIELDS))

    def test_questions_cover_every_concept_and_are_short(self):
        c, _ = _contract_for("02_high_risk_approval_required")
        qs = dict(receipt_questions(c))
        for label in ("Owner", "Requested by", "Executed by", "Allowed to", "Policy", "Approval", "Approved by",
                      "Checks", "Independent", "Capture", "State", "Generation cost", "Checks cost", "Retry cost",
                      "Attempts", "Cost per verified success", "Outcome", "Integrity"):
            self.assertIn(label, qs)
            self.assertLess(len(qs[label]), 200)
        self.assertEqual(qs["Approved by"], "Ops lead (dashboard)")
        self.assertEqual(qs["Owner"], "Platform team")

    def test_state_block_renders(self):
        c, _ = _contract_for("05_escalation_then_verified")
        lines = render_receipt_state_block(c)
        text = "\n".join(lines)
        self.assertIn("RECEIPT STATE", text)
        self.assertIn("VERIFIED_AFTER_ESCALATION", text)
        self.assertIn("Escalated", text)
        self.assertIn("Per success", text)


# ---------------------------------------------------------------------------
# ShardReceipt and renderer integration
# ---------------------------------------------------------------------------


class TestReceiptIntegration(unittest.TestCase):
    def test_shard_receipt_carries_state(self):
        r = build_shard_receipt(_legacy_entry())
        self.assertEqual(r.receipt_state, STATE_VERIFIED)
        self.assertEqual(r.receipt_state_reason, "verification passed")
        self.assertEqual(r.receipt_v2_fields, [])

    def test_compact_receipt_unchanged_for_legacy_entries(self):
        out = render_compact_shard_receipt(build_shard_receipt(_legacy_entry()))
        self.assertNotIn("State", out)

    def test_compact_receipt_shows_state_for_v2_entries(self):
        doc = _fixture("03_blocked_by_policy")
        out = render_compact_shard_receipt(build_shard_receipt(doc["entries"][0]))
        self.assertIn("  State       BLOCKED", out)

    def test_full_receipt_has_state_block(self):
        out = render_full_shard_receipt(build_shard_receipt(_legacy_entry(verification_passed=False)))
        self.assertIn("RECEIPT STATE", out)
        self.assertIn("  State       VERIFICATION_FAILED", out)
        self.assertIn("  Because     verification ran and failed", out)

    def test_extended_dict_view_has_state(self):
        r = build_shard_receipt(_legacy_entry())
        self.assertNotIn("state", receipt_to_dict(r), "default MCP key set stays stable")
        ext = receipt_to_dict(r, extended=True)
        self.assertEqual(ext["state"], STATE_VERIFIED)
        self.assertEqual(ext["state_reason"], "verification passed")

    def test_state_for_receipt_never_raises(self):
        state, reason = rc.receipt_state_for_receipt({"policy_decisions": "bad"}, build_shard_receipt({}))
        self.assertIn(state, VALID_STATES)
        self.assertTrue(reason)


class TestCliSurfaces(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def _write(self, entries: list[dict]) -> None:
        Path(".openshard").mkdir(exist_ok=True)
        with (Path(".openshard") / "runs.jsonl").open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    def test_last_json_includes_receipt_contract_with_siblings(self):
        doc = _fixture("05_escalation_then_verified")
        with self.runner.isolated_filesystem():
            self._write(doc["entries"])
            result = self.runner.invoke(cli, ["last", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        contract = data["receipt_contract"]
        self.assertEqual(contract["contract_version"], "2.0")
        self.assertEqual(contract["state"], STATE_VERIFIED_AFTER_ESCALATION)
        self.assertEqual(contract["attempts"]["attempts_observed"], 2)
        self.assertAlmostEqual(contract["cost"]["cost_per_verified_success_usd"], 0.054)
        # Existing envelope keys are untouched.
        for key in ("run", "trust", "proof_contract", "shard_quality", "content_hash"):
            self.assertIn(key, data)

    def test_last_full_prints_receipt_answers(self):
        doc = _fixture("02_high_risk_approval_required")
        with self.runner.isolated_filesystem():
            self._write(doc["entries"])
            result = self.runner.invoke(cli, ["last", "--full"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("RECEIPT ANSWERS", result.output)
        self.assertIn("Ops lead (dashboard)", result.output)
        self.assertIn("  State       VERIFIED", result.output)

    def test_last_default_stays_compact(self):
        doc = _fixture("02_high_risk_approval_required")
        with self.runner.isolated_filesystem():
            self._write(doc["entries"])
            result = self.runner.invoke(cli, ["last"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("RECEIPT ANSWERS", result.output)

    def test_history_json_carries_state(self):
        doc = _fixture("04_verification_failed")
        with self.runner.isolated_filesystem():
            self._write(doc["entries"])
            result = self.runner.invoke(cli, ["history", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(data["shards"][0]["state"], STATE_VERIFICATION_FAILED)


if __name__ == "__main__":
    unittest.main()
