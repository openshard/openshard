"""v0.5 architecture contracts: boundaries, trivial implementations, receipt mapping."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.contracts import (
    AllowAllPolicy,
    ApprovalOutcome,
    ApprovalRequest,
    ComputeRunSpec,
    ComputeRunStatus,
    ComputeUnavailableError,
    NullVerifier,
    OutcomeReport,
    PolicyContext,
    PolicyVerdict,
    RecordingApprovalProvider,
    RecordingOutcomeReporter,
    RecordingSyncTransport,
    UnavailableComputeProvider,
    VerificationOutcome,
    VerificationRequest,
    VerifierIdentity,
    build_sync_envelope,
)
from openshard.contracts.verification import VerificationCheckResult
from openshard.history.outcomes import load_outcomes, outcome_for_shard, record_outcome
from openshard.history.receipt_contract import (
    STATE_APPROVAL_REQUIRED,
    STATE_BLOCKED,
    STATE_VERIFIED,
    build_receipt_contract,
)
from openshard.policy.decision import make_ask, make_deny

FIXTURES = Path(__file__).parent / "fixtures" / "receipts" / "v2"


def _entry(**kw) -> dict:
    e = {
        "schema_version": "1.2", "timestamp": "2026-02-01T00:00:00Z", "run_id": "2026-02-01T00:00:00Z",
        "shard_id": "shard-20260201-0001", "attempt_number": 1, "task": "Wire the contracts",
        "execution_model": "anthropic/claude-sonnet-4.6", "retry_triggered": False,
        "verification_attempted": False, "verification_passed": None, "summary": "",
        "files_created": 0, "files_updated": 0, "files_deleted": 0,
    }
    e.update(kw)
    return e


class TestVerificationContract(unittest.TestCase):
    def test_null_verifier_is_honest(self):
        out = NullVerifier().verify(VerificationRequest(shard_id="s", run_id=None, repo_path=None))
        self.assertEqual(out.status, "not_run")
        self.assertFalse(out.verifier.independent)
        self.assertEqual(out.to_receipt_block(), [])

    def test_outcome_maps_to_receipt_and_drives_state(self):
        ident = VerifierIdentity(verifier_id="ci.github", verifier_kind="ci", independent=True)
        out = VerificationOutcome(verifier=ident, status="passed", checks=[
            VerificationCheckResult(check="pytest", status="passed", duration_seconds=3.0, cost_usd=0.01),
        ])
        self.assertFalse(out.raw_output_stored)
        block = out.to_receipt_block()
        self.assertEqual(block[0]["verifier_kind"], "ci")
        c = build_receipt_contract(_entry(verifiers=block, verification_attempted=True, verification_passed=True))
        self.assertEqual(c.state, STATE_VERIFIED)
        self.assertTrue(c.verification.independent)
        self.assertEqual(c.cost.verification_usd, 0.01)

    def test_unknown_status_falls_back(self):
        out = VerificationOutcome(verifier=NullVerifier().identity(), status="great")
        self.assertEqual(out.status, "unknown")


class TestPolicyContract(unittest.TestCase):
    def test_allow_all_names_itself(self):
        v = AllowAllPolicy().evaluate(PolicyContext(action="write", resource="src/x.py"))
        self.assertEqual(v.decision, "allow")
        self.assertIn("no policy configured", v.reason)
        block = v.to_receipt_block()
        self.assertEqual(block["policy_id"], "builtin:allow-all")
        c = build_receipt_contract(_entry(policy=block, policy_decisions=v.to_policy_decisions()))
        self.assertEqual(c.policy.policy_id, "builtin:allow-all")
        self.assertEqual(c.policy.decisions_count, 1)

    def test_verdict_resolution_and_state(self):
        decisions = [make_ask("write", reason="risky path", source="path_policy"),
                     make_deny("shell", reason="force push denied", source="path_policy", severity="critical")]
        v = PolicyVerdict.from_decisions(decisions, policy_id="pol-1", policy_version="2", name="protected")
        self.assertTrue(v.blocked)
        self.assertEqual(v.reason, "force push denied")
        c = build_receipt_contract(_entry(policy=v.to_receipt_block(), policy_decisions=v.to_policy_decisions()))
        self.assertEqual(c.state, STATE_BLOCKED)
        self.assertEqual(c.policy.deny_count, 1)
        self.assertEqual(c.policy.ask_count, 1)

    def test_invalid_decision_fails_closed(self):
        v = PolicyVerdict(policy_id="p", policy_version="1", name="n", decision="maybe", reason="?")
        self.assertEqual(v.decision, "deny")


class TestApprovalContract(unittest.TestCase):
    def test_pending_then_granted_by_separate_principal(self):
        provider = RecordingApprovalProvider(mechanism="dashboard")
        req = ApprovalRequest(shard_id="s1", action="write", reason="infra change",
                              requested_by={"kind": "user", "id": "user:dev"})
        out = provider.request(req)
        self.assertEqual(out.status, "pending")
        c = build_receipt_contract(_entry(approval=out.to_receipt_block(action="write")))
        self.assertEqual(c.state, STATE_APPROVAL_REQUIRED)

        out = provider.decide(req.request_id, "granted", {"kind": "user", "id": "user:lead"}, reason="ok")
        c = build_receipt_contract(_entry(approval=out.to_receipt_block(action="write"),
                                          actors={"requested_by": {"kind": "user", "id": "user:dev"}}))
        self.assertEqual(c.approval.status, "granted")
        self.assertEqual(c.approval.mechanism, "dashboard")
        self.assertEqual(c.actors.approved_by.id, "user:lead")
        self.assertEqual(c.actors.requested_by.id, "user:dev")
        self.assertEqual(provider.check(req.request_id).status, "granted")

    def test_scripted_denial_blocks(self):
        provider = RecordingApprovalProvider(decisions={"s2": ("denied", {"kind": "user", "id": "u"})})
        out = provider.request(ApprovalRequest(shard_id="s2", action="write", reason="r"))
        c = build_receipt_contract(_entry(approval=out.to_receipt_block(action="write")))
        self.assertEqual(c.state, STATE_BLOCKED)

    def test_invalid_status_stays_pending(self):
        self.assertEqual(ApprovalOutcome(request_id="x", status="yes").status, "pending")


class TestSyncContract(unittest.TestCase):
    def test_envelope_is_privacy_bounded(self):
        doc = json.loads((FIXTURES / "05_escalation_then_verified.json").read_text(encoding="utf-8"))
        a, b = doc["entries"]
        b = dict(b, raw_prompt="SECRET PROMPT", workspace_path="/home/someone/repo")
        env = build_sync_envelope(b, siblings=[a])
        d = env.to_dict()
        text = json.dumps(d)
        self.assertNotIn("SECRET PROMPT", text)
        self.assertNotIn("/home/someone", text)
        self.assertEqual(d["envelope_version"], "1")
        self.assertEqual(d["receipt_contract"]["state"], "VERIFIED_AFTER_ESCALATION")
        self.assertEqual(d["receipt"]["state"], "VERIFIED_AFTER_ESCALATION")
        self.assertEqual(d["content_hash"], b["content_hash"])
        self.assertEqual(d["shard_id"], b["shard_id"])

    def test_recording_transport(self):
        env = build_sync_envelope(_entry())
        ok = RecordingSyncTransport()
        self.assertTrue(ok.push(env).accepted)
        self.assertEqual(len(ok.envelopes), 1)
        bad = RecordingSyncTransport(fail=True)
        res = bad.push(env)
        self.assertFalse(res.accepted)
        self.assertEqual(res.status, "unreachable")

    def test_envelope_never_raises(self):
        env = build_sync_envelope("garbage")  # type: ignore[arg-type]
        json.dumps(env.to_dict())


class TestComputeContract(unittest.TestCase):
    def test_unavailable_is_explicit(self):
        p = UnavailableComputeProvider()
        spec = ComputeRunSpec(task="t", repo_identity="github.com/o/r", ref="main", agent="openshard_native")
        with self.assertRaises(ComputeUnavailableError):
            p.submit(spec)
        with self.assertRaises(ComputeUnavailableError):
            p.status("r")

    def test_status_vocabulary(self):
        self.assertEqual(ComputeRunStatus(run_id="r", state="flying").state, "unknown")
        self.assertTrue(ComputeRunStatus(run_id="r", state="failed").finished)


class TestOutcomes(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_report_validation_and_reporter(self):
        with self.assertRaises(ValueError):
            OutcomeReport(shard_id="s", status="vanished", source="cli")
        rep = RecordingOutcomeReporter()
        self.assertTrue(rep.report(OutcomeReport(shard_id="s", status="merged", source="github", reference="PR #1")))
        self.assertEqual(rep.reports[0].to_receipt_block()["status"], "merged")

    def test_record_and_load_beside_runs(self):
        with self.runner.isolated_filesystem():
            root = Path.cwd()
            record_outcome(OutcomeReport(shard_id="s1", status="accepted", source="cli"), repo_path=root)
            record_outcome(OutcomeReport(shard_id="s1", status="merged", source="github", reference="PR #2"), repo_path=root)
            record_outcome(OutcomeReport(shard_id="s2", status="rolled_back", source="deploy"), repo_path=root)
            (root / ".openshard" / "outcomes.jsonl").open("a").write("not json\n")
            latest = load_outcomes(root)
        self.assertEqual(latest["s1"]["status"], "merged")
        self.assertEqual(latest["s2"]["status"], "rolled_back")
        self.assertIsNone(outcome_for_shard("s3", root))

    def test_outcome_overlay_keeps_hash_valid(self):
        from openshard.history.shard_hash import compute_shard_hash
        e = _entry()
        e["content_hash"] = compute_shard_hash(e)
        c = build_receipt_contract(e, outcome_record={"status": "deployed", "source": "deploy", "reference": "d-9"})
        self.assertEqual(c.outcome.status, "deployed")
        self.assertEqual(c.integrity.hash_status, "valid")

    def test_cli_outcome_record_shows_in_last(self):
        doc = json.loads((FIXTURES / "01_verified_coding_task.json").read_text(encoding="utf-8"))
        e = dict(doc["entries"][0])
        del e["outcome"]
        from openshard.history.shard_hash import compute_shard_hash
        e["content_hash"] = compute_shard_hash(e)
        with self.runner.isolated_filesystem():
            Path(".openshard").mkdir()
            (Path(".openshard") / "runs.jsonl").write_text(json.dumps(e) + "\n", encoding="utf-8")
            missing = self.runner.invoke(cli, ["outcome", "record", "shard-nope", "merged"])
            self.assertEqual(missing.exit_code, 1)
            res = self.runner.invoke(cli, ["outcome", "record", e["shard_id"], "merged",
                                           "--reference", "PR #7", "--source", "github", "--by", "user:alice", "--json"])
            self.assertEqual(res.exit_code, 0, res.output)
            self.assertEqual(json.loads(res.output)["outcome"]["status"], "merged")
            last = self.runner.invoke(cli, ["last", "--json"])
            self.assertEqual(last.exit_code, 0, last.output)
            data = json.loads(last.output)
            self.assertEqual(data["receipt_contract"]["outcome"]["status"], "merged")
            self.assertEqual(data["receipt_contract"]["outcome"]["reference"], "PR #7")
            self.assertEqual(data["content_hash_status"], "valid")
            full = self.runner.invoke(cli, ["last", "--full"])
            self.assertIn("Outcome                   merged", full.output)


if __name__ == "__main__":
    unittest.main()
