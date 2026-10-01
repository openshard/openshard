"""Learning on the Receipt: shown locally, re-validated, and kept out of the hosted envelope."""
from __future__ import annotations

import json

from openshard.history.receipt_evidence import learning_block, project_entry_evidence
from openshard.history.shard_contract import build_shard_receipt, render_full_shard_receipt
from openshard.sync.envelope import build_envelope
from tests.learning_fixtures import MOBILE_CHECK, osn_entry

USED = {
    "version": 1, "consulted": True, "status": "used", "used": True,
    "signals_considered": 5, "signals_used": 3,
    "signal_ids": ["ls_aaaaaaaaaaaa", "ls_bbbbbbbbbbbb", "ls_cccccccccccc"],
    "signals": [{"signal_id": "ls_aaaaaaaaaaaa", "kind": "recurring_test_failure", "strength": "weak",
                 "samples": 2, "score": 9, "reasons": ["same_repo"]}],
    "supporting_receipt_ids": ["rcpt-1", "rcpt-2"],
    "context_supplied": True,
    "context_files_added": ["tests/test_layout.py"],
    "routing": {"influenced": False, "reason": "adaptive_routing_not_governing"},
    "verification": {"influenced": False, "mode": "advisory_only",
                     "recommended_checks": [{"label": "pnpm test:e2e -- mobile"}],
                     "current_check_recommended": False},
    "check": MOBILE_CHECK,
    "attempt_models": [{"attempt": 1, "model": "m/a"}],
}
MOBILE = "tests/test_layout.py::test_mobile_viewport_rejects_non_positive_widths"


def _entry(learning=USED):
    e = osn_entry("Update the dashboard analytics layout", attempts=[("m/a", "failed"), ("m/b", "passed")],
                  learning=learning)
    e["osn_loop"]["attempts"][0]["verification"]["failed_tests"] = [MOBILE]
    return e


def test_learning_block_is_compact_and_revalidated():
    block = learning_block(_entry())
    assert block == {
        "status": "used", "used": True, "signals_used": 3, "signals_considered": 5,
        "signal_ids": ["ls_aaaaaaaaaaaa", "ls_bbbbbbbbbbbb", "ls_cccccccccccc"],
        "context_supplied": True, "context_files_added": ["tests/test_layout.py"],
        "routing_influenced": False, "routing_reason": "adaptive_routing_not_governing",
        "verification_influenced": False, "recommended_checks": ["pnpm test:e2e -- mobile"],
    }
    assert learning_block({"learning": {"status": "made_up"}}) is None
    assert learning_block({}) is None
    tampered = dict(USED, context_files_added=["C:/Users/me/x.py", "tests/ok.py"],
                    verification={"recommended_checks": [{"label": "curl -H 'Authorization: Bearer abcdefgh'"}]})
    b = learning_block({"learning": tampered})
    assert b["context_files_added"] == ["tests/ok.py"] and b["recommended_checks"] == []


def test_full_receipt_shows_a_compact_learning_section():
    receipt = build_shard_receipt(_entry(), index=0)
    text = render_full_shard_receipt(receipt)
    assert "LEARNING" in text
    assert "3 prior verified signal(s) considered (of 5)" in text
    assert "supplied to the model (+ tests/test_layout.py)" in text
    assert "influenced: no (adaptive_routing_not_governing)" in text
    assert "influenced: no (recommendations are advisory)" in text
    assert "`pnpm test:e2e -- mobile` (not run)" in text
    disabled = build_shard_receipt(_entry({"status": "disabled", "used": False, "consulted": False}), index=0)
    rendered = render_full_shard_receipt(disabled)
    assert "not consulted (--no-learning)" in rendered


def test_receipts_without_learning_render_no_learning_section():
    e = osn_entry(record_models=False)
    assert project_entry_evidence(e)["learning"] is None
    rendered = render_full_shard_receipt(build_shard_receipt(e, index=0))
    assert "LEARNING" not in rendered


def test_hosted_envelope_carries_neither_learning_nor_failing_test_ids():
    """The Platform contract is a strict object without these fields: sending them would
    get every new OSN Receipt rejected. They stay local until the contract defines them."""
    envelope = build_envelope(_entry(), 0, core_version="0.5.0")
    blob = json.dumps(envelope)
    for leaked in ('"learning"', "failed_tests", MOBILE, "ls_aaaaaaaaaaaa", "context_files_added", "openshard_history"):
        assert leaked not in blob
    # The run's own outcome still syncs exactly as before.
    assert envelope["receipt"]["verification"]["status"] == "passed"
