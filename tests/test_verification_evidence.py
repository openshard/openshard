"""Structured verification evidence (``openshard.history.verification``).

Covers the evidence states end to end: record -> ShardReceipt ->
``history --json`` projection -> sync envelope, plus old records that
predate the ``verification`` block.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.history import verification as v
from openshard.history.proof_signals import verification_status_from_receipt
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.shard_hash import SHARD_HASH_FIELD, compute_shard_hash
from openshard.history.views import receipt_to_dict
from openshard.sync import envelope
from tests.capture_fixtures import _make_repo

SID = "11111111-2222-4333-8444-555555555555"


def _receipt_json(entry: dict) -> dict:
    return receipt_to_dict(build_shard_receipt(entry), extended=True)


def _hook(repo: Path, event: str, **fields) -> None:
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, **fields}
    handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _runs(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "widget")


# ---------------------------------------------------------------------------
# Evidence states
# ---------------------------------------------------------------------------


class TestEvidenceStates:
    def test_passing_directly_observed_check(self):
        entry = {
            "timestamp": "2026-09-20T10:00:00Z", "task": "t",
            "verification_attempted": True, "verification_passed": True,
            "verification_plan": [{"name": "pytest", "argv": ["pytest", "-q"], "kind": "test"}],
        }
        d = _receipt_json(entry)["verification"]
        assert d["status"] == "passed"
        assert d["source"] == "directly_observed"
        assert d["observation_mode"] == "openshard_executed"
        assert (d["checks_attempted"], d["checks_passed"], d["checks_failed"]) == (1, 1, 0)
        assert d["checks"] == [{"name": "pytest", "kind": "test", "status": "passed", "exit_code": None}]
        assert d["derived"] is True  # computed from an older-shape record, never written back

    def test_passing_osn_contract_keeps_exit_code_and_duration(self):
        entry = {
            "timestamp": "2026-09-20T10:00:00Z", "task": "t",
            "osn_verification_contract": {"enabled": True, "status": "passed", "returncode": 0,
                                          "duration_seconds": 3.5, "summary": "pytest exited 0"},
        }
        r = build_shard_receipt(entry)
        assert r.verification_status == "passed"  # OSN token kept as before
        assert r.verification["exit_code"] == 0 and r.verification["duration_seconds"] == 3.5
        assert r.verification["source"] == "directly_observed"

    def test_failing_directly_observed_check(self):
        entry = {
            "timestamp": "2026-09-20T10:00:00Z", "task": "t",
            "verification_attempted": True, "verification_passed": False,
            "osn_verification_contract": {"enabled": True, "status": "failed", "returncode": 1},
        }
        r = build_shard_receipt(entry)
        assert r.verification_status == "failed"
        assert r.verification["status"] == "failed" and r.verification["exit_code"] == 1
        assert r.verification["source"] == "directly_observed"
        assert verification_status_from_receipt(r) == "failed"

    def test_agent_reported_pass_is_labelled_agent_reported(self):
        block = v.build_verification(
            source=v.SOURCE_AGENT_REPORTED, observation_mode=v.MODE_AGENT_CLAIM,
            status="passed", checks_attempted=42, checks_passed=42, checks_failed=0,
            reason="Agent reported: 42 tests passed.",
        )
        entry = {"timestamp": "2026-09-20T10:00:00Z", "task": "t", "verification": block}
        r = build_shard_receipt(entry)
        assert r.verification_status == "passed"
        assert r.verification["source"] == "agent_reported"
        assert r.verification["observation_mode"] == "agent_claim"
        assert r.verification["derived"] is False
        # The flat reason carries the (weaker) source across the sync boundary.
        assert r.verification_reason.endswith("[agent_reported]")
        assert r.status == "Passed" and r.checks_display == "42/42 passed"

    def test_ci_pass_is_independently_verified_against_an_exact_sha(self):
        block = v.build_verification(
            source=v.SOURCE_INDEPENDENTLY_VERIFIED, observation_mode=v.MODE_CI_REPORT,
            checks=[{"name": "ci/test", "kind": "test", "status": "passed"}],
            checks_attempted=42, checks_passed=42, checks_failed=0, checks_skipped=0,
            artifact_sha="ABC1234def",
            started_at="2026-09-20T10:00:00Z", completed_at="2026-09-20T10:04:00Z",
        )
        assert block["status"] == "passed"
        assert block["artifact_sha"] == "abc1234def"
        r = build_shard_receipt({"timestamp": "2026-09-20T10:00:00Z", "task": "t", "verification": block})
        assert r.verification["source"] == "independently_verified"
        assert "@ abc1234def" in r.verification_reason

    def test_no_checks_run(self):
        entry = {"timestamp": "2026-09-20T10:00:00Z", "task": "t", "verification_attempted": False}
        r = build_shard_receipt(entry)
        assert r.verification["status"] == "not_run"
        assert r.verification["checks_attempted"] == 0
        assert r.verification_status == "not_run"
        assert r.status == "No checks run"

    def test_attempted_without_outcome_is_unknown_not_not_run(self):
        entry = {"timestamp": "2026-09-20T10:00:00Z", "task": "t",
                 "verification_attempted": True, "verification_passed": None}
        r = build_shard_receipt(entry)
        assert r.verification["status"] == "unknown"
        assert "outcome_not_observed" in r.verification["incomplete_reasons"]
        assert r.verification["complete"] is False
        assert r.verification_status == "unknown"  # never null, never not_run
        assert r.status == "Checks attempted, result not verified"

    def test_partial_checks(self):
        block = v.build_verification(
            source=v.SOURCE_DIRECTLY_OBSERVED, observation_mode=v.MODE_OPENSHARD_EXECUTED,
            checks=[
                {"name": "pytest", "kind": "test", "status": "passed", "exit_code": 0},
                {"name": "mypy", "kind": "typecheck", "status": "unknown"},
                {"name": "ruff", "kind": "lint", "status": "skipped"},
            ],
        )
        assert block["status"] == "partial"
        assert (block["checks_attempted"], block["checks_passed"], block["checks_skipped"]) == (2, 1, 1)
        r = build_shard_receipt({"timestamp": "2026-09-20T10:00:00Z", "task": "t", "verification": block})
        assert r.verification_status == "partial"
        assert r.status == "Partial"
        assert verification_status_from_receipt(r) == "partial"

    def test_a_failure_is_never_hidden_by_other_outcomes(self):
        checks = [v.VerificationCheck("a", "passed"), v.VerificationCheck("b", "unknown"),
                  v.VerificationCheck("c", "failed")]
        assert v.aggregate_status(checks) == "failed"
        assert v.aggregate_status([]) == "not_run"
        assert v.aggregate_status([v.VerificationCheck("a", "skipped")]) == "not_run"
        assert v.aggregate_status([v.VerificationCheck("a", "unknown")]) == "unknown"


# ---------------------------------------------------------------------------
# Malformed / incomplete evidence
# ---------------------------------------------------------------------------


class TestMalformedEvidence:
    @pytest.mark.parametrize("raw", ["passed", 42, ["passed"], True])
    def test_non_dict_block_becomes_unknown_and_incomplete(self, raw):
        r = build_shard_receipt({"timestamp": "2026-09-20T10:00:00Z", "task": "t", "verification": raw})
        assert r.verification["status"] == "unknown"
        assert r.verification["complete"] is False
        assert "malformed_verification_block" in r.verification["incomplete_reasons"]
        assert r.verification_status == "unknown"  # evidence existed: never "nothing recorded"

    def test_invalid_status_and_bad_checks_are_flagged_not_dropped_silently(self):
        ev = v.parse_verification_block({
            "status": "great", "source": "vibes", "observation_mode": "hook_tool_event",
            "checks": [{"name": "pytest", "status": "passed"}, {"status": "passed"}, "junk"],
            "checks_attempted": -3, "artifact_sha": "not-a-sha", "started_at": "yesterday",
        })
        assert ev.status == "unknown"
        assert ev.source is None
        assert ev.checks_attempted == 1  # recomputed from the one valid check, never -3
        assert ev.started_at is None and ev.artifact_sha is None
        assert {"invalid_status", "malformed_check_dropped", "malformed_verification_block"} <= set(
            ev.incomplete_reasons
        )

    def test_counts_that_contradict_status_become_unknown(self):
        ev = v.parse_verification_block({
            "status": "passed", "source": "agent_reported", "observation_mode": "agent_claim",
            "checks_attempted": 3, "checks_passed": 2, "checks_failed": 1,
        })
        assert ev.status == "unknown"
        assert "status_inconsistent_with_counts" in ev.incomplete_reasons

    def test_secrets_and_paths_never_survive_in_names_or_reasons(self):
        ev = v.parse_verification_block({
            "status": "failed", "source": "directly_observed", "observation_mode": "openshard_executed",
            "checks": [{"name": "pytest --token=ghp_" + "a" * 36, "status": "failed"}],
            "reason": "failed with AKIA" + "B" * 16,
        })
        blob = json.dumps(ev.to_dict())
        assert "ghp_" + "a" * 36 not in blob and "AKIA" + "B" * 16 not in blob

    def test_hook_record_with_lost_events_and_no_check_is_unknown_not_not_run(self):
        entry = {
            "timestamp": "2026-09-20T10:00:00Z", "task": "t", "executor": "claude_code_hooks",
            "verification_attempted": False,
            "capture": {"source": "claude_code_hooks", "hook_events_dropped": 4},
        }
        r = build_shard_receipt(entry)
        assert r.verification["status"] == "unknown"
        assert "capture_events_lost" in r.verification["incomplete_reasons"]
        assert r.status == "Not recorded"


# ---------------------------------------------------------------------------
# Capture paths
# ---------------------------------------------------------------------------


class TestCapturePaths:
    def test_hook_observed_check_invocation_is_directly_observed_unknown(self, repo):
        _hook(repo, "SessionStart", source="startup")
        _hook(repo, "UserPromptSubmit", prompt="fix the bug")
        _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "python -m pytest -q"})
        _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "ruff check ."})
        _hook(repo, "Stop")
        entry = _runs(repo)[-1]
        block = entry["verification"]
        assert block["status"] == "unknown"
        # OpenShard saw the invocation itself; only the outcome is missing.
        assert block["source"] == "directly_observed" and block["observation_mode"] == "hook_tool_event"
        assert "outcome_not_observed" in block["incomplete_reasons"]
        assert "outcome not observed" in block["reason"]
        assert block["checks_attempted"] == 2 and block["checks_passed"] == 0
        assert [c["kind"] for c in block["checks"]] == ["test", "lint"]
        assert block["derived"] is False
        payload = envelope.receipt_payload(entry, 0)
        assert payload["verification_status"] == "unknown"  # was null -> "No verification recorded"
        assert "[directly_observed]" in payload["verification_reason"]
        assert "agent_reported" not in payload["verification_reason"]

    def test_hook_reported_tool_failure_is_an_agent_reported_failure(self, repo):
        _hook(repo, "UserPromptSubmit", prompt="task")
        _hook(repo, "PostToolUseFailure", tool_name="Bash", tool_input={"command": "npm test"},
              error="RAW ERROR TEXT")
        _hook(repo, "Stop")
        entry = _runs(repo)[-1]
        assert entry["verification"]["status"] == "failed"
        assert entry["verification"]["source"] == "agent_reported"
        assert "RAW ERROR TEXT" not in json.dumps(entry)

    def test_hook_session_without_checks_is_not_run(self, repo):
        _hook(repo, "UserPromptSubmit", prompt="explain the code")
        _hook(repo, "Stop")
        block = _runs(repo)[-1]["verification"]
        assert block["status"] == "not_run" and block["checks_attempted"] == 0
        assert block["source"] == "directly_observed"

    def test_check_evidence_survives_a_buffer_rebuild(self, repo):
        _hook(repo, "UserPromptSubmit", prompt="task")
        _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "pytest"})
        _hook(repo, "Stop")
        _hook(repo, "SessionEnd", reason="clear")  # buffer deleted
        _hook(repo, "UserPromptSubmit", prompt="resume")  # rebuilt from runs.jsonl
        _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "go test ./..."})
        _hook(repo, "Stop")
        block = _runs(repo)[-1]["verification"]
        assert block["checks_attempted"] == 2
        assert block["status"] == "unknown"

    def test_import_is_not_observable_not_no_checks_run(self, repo):
        from openshard.adapters.claude_code_import import build_claude_code_import_entry

        entry = build_claude_code_import_entry("task", repo_path=repo)
        assert entry["verification"]["observation_mode"] == "not_observable"
        r = build_shard_receipt(entry)
        assert r.verification_status == "unknown"
        assert r.status == "Not recorded" and r.checks_display == "Not recorded"


# ---------------------------------------------------------------------------
# Old receipts (no verification block)
# ---------------------------------------------------------------------------


class TestOldReceipts:
    OLD_HOOK = {
        "schema_version": "1.2", "timestamp": "2026-08-01T09:00:00Z", "task": "t",
        "executor": "claude_code_hooks", "verification_attempted": True, "verification_passed": None,
        "capture": {"source": "claude_code_hooks", "hook_events_dropped": 0,
                    "completeness": {"depth": "partial", "status": "complete", "reasons": []}},
        "events": [{"event_type": "tool.invoked", "action": "Bash: pytest -q", "status": "unknown",
                    "occurred_at": "2026-08-01T09:01:00Z", "metadata": {"command_kind": "test"}}],
    }
    OLD_IMPORT = {"schema_version": "1.1", "timestamp": "2026-07-01T09:00:00Z", "task": "t",
                  "executor": "claude_code_import", "verification_attempted": False,
                  "verification_passed": None}
    PRE_STAMPING = {"timestamp": "2025-12-01T09:00:00Z", "task": "t"}

    def test_old_hook_record_derives_unknown_from_its_events(self):
        r = build_shard_receipt(copy.deepcopy(self.OLD_HOOK))
        d = r.verification
        assert d["derived"] is True
        assert d["status"] == "unknown" and d["source"] == "directly_observed"
        assert "outcome_not_observed" in d["incomplete_reasons"]
        assert d["checks_attempted"] == 1 and d["started_at"] == "2026-08-01T09:01:00Z"
        assert r.verification_status == "unknown"
        assert r.status == "Checks attempted, result not verified"  # unchanged display

    def test_old_import_no_longer_claims_no_checks_run(self):
        r = build_shard_receipt(copy.deepcopy(self.OLD_IMPORT))
        assert r.verification["observation_mode"] == "not_observable"
        assert r.verification_status == "unknown"
        assert r.status == "Not recorded"

    def test_record_with_nothing_recorded_stays_not_recorded(self):
        r = build_shard_receipt(copy.deepcopy(self.PRE_STAMPING))
        assert r.verification["observation_mode"] == "none"
        assert r.verification_status == ""  # honestly nothing: null over the wire
        assert _receipt_json(self.PRE_STAMPING)["verification_status"] is None

    @pytest.mark.parametrize("name", ["OLD_HOOK", "OLD_IMPORT", "PRE_STAMPING"])
    def test_reading_never_rewrites_the_stored_record(self, name):
        entry = copy.deepcopy(getattr(self, name))
        entry[SHARD_HASH_FIELD] = compute_shard_hash(entry)
        before = json.dumps(entry, sort_keys=True)
        r = build_shard_receipt(entry)
        envelope.receipt_payload(entry, 0)
        assert json.dumps(entry, sort_keys=True) == before
        assert "verification" not in entry
        assert r.integrity.startswith("Matches")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    @pytest.mark.parametrize("block", [
        v.build_verification(source="directly_observed", observation_mode="openshard_executed",
                             checks=[{"name": "pytest", "kind": "test", "status": "passed", "exit_code": 0}],
                             exit_code=0, duration_seconds=1.25, started_at="2026-09-20T10:00:00Z",
                             completed_at="2026-09-20T10:00:02Z"),
        v.build_verification(source="directly_observed", observation_mode="hook_tool_event", checks_attempted=0),
        v.build_verification(source="directly_observed", observation_mode="hook_tool_event", status="unknown",
                             checks=[{"name": "Bash: pytest", "kind": "test", "status": "unknown"}],
                             incomplete_reasons=["outcome_not_observed"]),
        v.not_observable_verification(),
    ])
    def test_round_trip_through_json_record_receipt_and_projection(self, block):
        again = v.parse_verification_block(json.loads(json.dumps(block))).to_dict()
        assert again == block
        entry = {"timestamp": "2026-09-20T10:00:00Z", "task": "t", "verification": block}
        stored = json.loads(json.dumps(entry))  # runs.jsonl boundary
        projected = json.loads(json.dumps(_receipt_json(stored)))  # history --json boundary
        assert projected["verification"] == block

    def test_sync_payload_stays_within_the_hosted_contract(self):
        block = v.build_verification(source="independently_verified", observation_mode="ci_report",
                                     status="passed", checks_attempted=1, checks_passed=1, checks_failed=0,
                                     artifact_sha="abc123f")
        entry = {"receipt_id": "rcpt_" + "a" * 32, "timestamp": "2026-09-20T10:00:00Z", "task": "t",
                 "verification": block}
        doc = envelope.build_envelope(entry, 0, core_version="x")
        receipt = doc["receipt"]
        assert "verification" not in receipt  # withheld until the Platform contract defines it
        assert envelope.WITHHELD_RECEIPT_KEYS == {"verification", "task_title"}
        assert receipt["verification_status"] == "passed"
        assert receipt["verification_reason"].endswith("[independently_verified @ abc123f]")
        assert len(receipt["verification_reason"]) <= 300
        assert len(receipt["verification_status"]) <= 32
