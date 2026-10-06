"""Receipt ablation: remove one capture dimension at a time and measure the evidence.

Every case starts from one fully captured, OpenShard-verified baseline record and
removes a single capture dimension (model, cost, tokens, verification checks, ...)
or swaps the verification evidence for a weaker source. Each variant is measured
on the four existing evidence surfaces, unchanged:

* completeness (``history/completeness.py``): which scored fields are present / missing
* the Shard Proof Contract (``history/proof_contract.py``): per-section status and overall grade
* the Run Trust Score (``history/trust_score.py``): score, band and named penalties
* verification truth (``history/verification_truth.py``): state, authority, effective status

The invariant: losing evidence can never make a Receipt look stronger on any
surface, and a dimension that was not captured reads as missing or unknown,
never as zero or as a verified outcome.

``python -m tests.test_receipt_ablation`` prints the measured report.
"""

from __future__ import annotations

import copy
from collections.abc import Callable

import pytest

from openshard.history import verification as v
from openshard.history.completeness import score_receipt
from openshard.history.proof_contract import build_shard_proof_contract
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.trust_score import evaluate_trust_score, format_human
from openshard.history.verification_truth import OBSERVED_STATES, interpret_receipt

BASELINE: dict = {
    "schema_version": "1.1",
    "task": "Add a helper function",
    "timestamp": "2026-06-03T00:00:00Z",
    "execution_model": "claude-opus-4.7",
    "duration_seconds": 2.5,
    "estimated_cost": 0.01,
    "prompt_tokens": 1200,
    "completion_tokens": 300,
    "tokens_provenance": "provider_reported",
    "files_updated": 1,
    "verification": v.build_verification(
        source=v.SOURCE_DIRECTLY_OBSERVED,
        observation_mode=v.MODE_OPENSHARD_EXECUTED,
        checks=[{"name": "pytest", "kind": "test", "status": "passed"}],
        exit_code=0,
    ),
    "verification_attempted": True,
    "verification_passed": True,
    "file_context": {"paths": ["src/a.py"]},
    "context_files_injected_count": 3,
    "policy_decisions": [{"decision_id": "d1", "decision": "allow", "action": "write"}],
    "approval_receipt": {"granted": True},
    "execution_spans": [{"span_id": "s1", "name": "planning", "kind": "phase"}],
    "developer_feedback": {"outcome": "accepted"},
    "run_timeline": [
        {"event": "receipt_saved", "label": "Saved Shard receipt", "kind": "receipt", "status": "completed"}
    ],
    "summary": "done",
}

_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "tokens_provenance")
_VERIFICATION_KEYS = ("verification", "verification_attempted", "verification_passed")


def _drop(*keys: str) -> Callable[[dict], None]:
    def mutate(entry: dict) -> None:
        for key in keys:
            entry.pop(key, None)
    return mutate


def _agent_reported_only(entry: dict) -> None:
    entry["verification"] = v.build_verification(
        source=v.SOURCE_AGENT_REPORTED,
        observation_mode=v.MODE_AGENT_CLAIM,
        status="passed",
        checks_attempted=42,
        checks_passed=42,
        checks_failed=0,
        reason="Agent reported: 42 tests passed.",
    )


def _no_checks_ran(entry: dict) -> None:
    entry["verification"] = v.build_verification(
        source=v.SOURCE_DIRECTLY_OBSERVED, observation_mode=v.MODE_OPENSHARD_EXECUTED, checks=[],
    )


def _hook_booleans_only(entry: dict) -> None:
    entry.pop("verification")
    entry["executor"] = "claude_code"
    entry["capture"] = {"source": "claude_code_hooks"}


def _import_booleans_only(entry: dict) -> None:
    entry.pop("verification")
    entry["executor"] = "claude_code_import"


def _agent_tokens_no_cost(entry: dict) -> None:
    entry.pop("estimated_cost")
    entry["tokens_provenance"] = "agent_reported"


def _everything(entry: dict) -> None:
    for key in ("execution_model", "estimated_cost", "duration_seconds", "run_timeline", *_TOKEN_KEYS,
                *_VERIFICATION_KEYS):
        entry.pop(key, None)


# (case id, what was removed or weakened, mutation)
CASES: tuple[tuple[str, str, Callable[[dict], None]], ...] = (
    ("no_model", "execution model", _drop("execution_model")),
    ("no_cost", "dollar cost (tokens kept)", _drop("estimated_cost")),
    ("no_tokens", "token usage (cost kept)", _drop(*_TOKEN_KEYS)),
    ("no_cost_no_tokens", "dollar cost and token usage", _drop("estimated_cost", *_TOKEN_KEYS)),
    ("agent_tokens_no_cost", "dollar cost; tokens agent-reported", _agent_tokens_no_cost),
    ("no_duration", "duration", _drop("duration_seconds")),
    ("no_timeline", "run timeline", _drop("run_timeline")),
    ("no_verification", "all verification evidence", _drop(*_VERIFICATION_KEYS)),
    ("no_checks_ran", "verification checks (OpenShard ran none)", _no_checks_ran),
    ("agent_reported_only", "observed verification -> agent's own claim", _agent_reported_only),
    ("legacy_booleans_only", "verification block; unmarked record keeps booleans", _drop("verification")),
    ("hook_booleans_only", "verification block; hook record keeps booleans", _hook_booleans_only),
    ("import_booleans_only", "verification block; imported record keeps booleans", _import_booleans_only),
    ("everything", "model, cost, tokens, duration, timeline, verification", _everything),
)

# Higher is stronger. Unknown/unsafe grades rank lowest: they never outrank evidence.
PROOF_RANK = {"unknown": 0, "unsafe": 0, "weak": 1, "partial": 2, "usable": 3, "strong": 4}
AUTHORITY_RANK = {
    "none": 0, "agent_reported": 1,
    "git_verified": 2, "independently_verified": 2, "directly_observed": 2,
}


def measure(entry: dict) -> dict:
    receipt = build_shard_receipt(entry, index=0)
    completeness = score_receipt(receipt)
    trust = evaluate_trust_score(entry, receipt)
    proof = build_shard_proof_contract(entry)
    truth = interpret_receipt(receipt)
    return {
        "completeness_percent": completeness.score_percent,
        "missing_fields": list(completeness.missing_fields),
        "trust_score": trust.score,
        "trust_band": trust.band,
        "trust_penalties": [p.code for p in trust.penalties],
        "trust_lines": format_human(trust),
        "proof_overall": proof["overall_status"],
        "proof_sections": {s["name"]: s["status"] for s in proof["sections"]},
        "proof_details": {s["name"]: s["detail"] for s in proof["sections"]},
        "verification_state": truth.state,
        "verification_authority": truth.authority,
        "verification_effective": truth.effective_status,
        "verification_derived": bool((receipt.verification or {}).get("derived")),
        "model_display": receipt.model_display,
        "cost_raw": receipt.cost_raw,
        "cost_display": receipt.cost_display,
        "cost_provenance": receipt.cost_provenance,
        "tokens_input": receipt.tokens_input,
        "tokens_output": receipt.tokens_output,
        "tokens_provenance": receipt.tokens_provenance,
    }


def ablate(mutate: Callable[[dict], None]) -> dict:
    entry = copy.deepcopy(BASELINE)
    mutate(entry)
    return measure(entry)


def changes(base: dict, row: dict) -> list[str]:
    """Plain-language list of what moved from the baseline. Empty means no surface noticed."""
    out: list[str] = []
    if row["completeness_percent"] != base["completeness_percent"]:
        newly = [f for f in row["missing_fields"] if f not in base["missing_fields"]]
        out.append(f"completeness {base['completeness_percent']}->{row['completeness_percent']}% "
                   f"(missing: {', '.join(newly)})")
    if row["trust_score"] != base["trust_score"]:
        out.append(f"trust {base['trust_score']}->{row['trust_score']} ({', '.join(row['trust_penalties'])})")
    if row["proof_overall"] != base["proof_overall"]:
        out.append(f"proof {base['proof_overall']}->{row['proof_overall']}")
    moved = [
        f"{name}={status}" for name, status in row["proof_sections"].items()
        if status != base["proof_sections"].get(name)
    ]
    if moved:
        out.append(f"sections {', '.join(moved)}")
    if row["verification_state"] != base["verification_state"]:
        out.append(f"verification {row['verification_state']}/{row['verification_authority']}")
    return out


def build_report() -> str:
    base = measure(copy.deepcopy(BASELINE))
    lines = [
        "| case | removed | completeness | trust | proof | verification | what changed |",
        "|---|---|---|---|---|---|---|",
        f"| baseline | nothing | {base['completeness_percent']}% | {base['trust_score']} {base['trust_band']} "
        f"| {base['proof_overall']} | {base['verification_state']} | - |",
    ]
    for case_id, removed, mutate in CASES:
        row = ablate(mutate)
        moved = "; ".join(changes(base, row)) or "**no surface noticed**"
        lines.append(
            f"| {case_id} | {removed} | {row['completeness_percent']}% | {row['trust_score']} {row['trust_band']} "
            f"| {row['proof_overall']} | {row['verification_state']} | {moved} |"
        )
    return "\n".join(lines)


@pytest.fixture(scope="module")
def baseline() -> dict:
    return measure(copy.deepcopy(BASELINE))


def _row(case_id: str) -> dict:
    return ablate(next(m for cid, _, m in CASES if cid == case_id))


# --- the baseline is a real, fully evidenced run ----------------------------

def test_baseline_is_fully_captured_and_observed(baseline):
    assert baseline["completeness_percent"] == 100
    assert baseline["missing_fields"] == []
    assert baseline["trust_score"] == 100 and baseline["trust_band"] == "strong"
    assert baseline["verification_state"] == "verified_passed"
    assert baseline["verification_authority"] == "directly_observed"
    assert baseline["proof_overall"] == "usable"
    assert baseline["proof_sections"]["model"] == "present"
    assert baseline["proof_sections"]["cost"] == "present"
    assert baseline["proof_sections"]["verification"] == "present"


# --- no ablation is ever stronger than the baseline -------------------------

@pytest.mark.parametrize("case_id,removed,mutate", CASES, ids=[c[0] for c in CASES])
def test_ablation_never_upgrades_evidence(baseline, case_id, removed, mutate):
    row = ablate(mutate)
    assert row["completeness_percent"] <= baseline["completeness_percent"]
    assert set(baseline["missing_fields"]) <= set(row["missing_fields"])
    assert row["trust_score"] <= baseline["trust_score"]
    assert PROOF_RANK[row["proof_overall"]] <= PROOF_RANK[baseline["proof_overall"]]
    assert AUTHORITY_RANK[row["verification_authority"]] <= AUTHORITY_RANK[baseline["verification_authority"]]
    for name, status in row["proof_sections"].items():
        if baseline["proof_sections"][name] != "present":
            assert status != "present", f"{name} became present after removing {removed}"


@pytest.mark.parametrize("case_id", ["no_verification", "no_checks_ran", "agent_reported_only",
                                     "hook_booleans_only", "import_booleans_only", "everything"])
def test_weakened_verification_is_never_an_observed_pass(case_id):
    row = _row(case_id)
    assert row["verification_state"] not in OBSERVED_STATES
    assert row["verification_effective"] != "passed"
    assert row["proof_sections"]["verification"] != "present"
    assert row["trust_band"] != "strong"
    assert not any(line.strip().startswith("- Verification passed") for line in row["trust_lines"])


# --- missing stays missing, never zero --------------------------------------

def test_no_model_reads_not_recorded():
    row = _row("no_model")
    assert row["model_display"] == "Not recorded"
    assert "execution_model" in row["missing_fields"]
    assert row["proof_sections"]["model"] == "missing"
    assert row["proof_overall"] == "partial"


def test_no_tokens_is_unknown_not_zero():
    row = _row("no_tokens")
    assert row["tokens_input"] is None and row["tokens_output"] is None
    assert row["tokens_provenance"] is None


def test_no_cost_and_no_tokens_is_not_recorded_not_zero():
    row = _row("no_cost_no_tokens")
    assert row["cost_raw"] is None
    assert row["cost_display"] == "Not recorded"
    assert "cost_estimate" in row["missing_fields"]
    assert row["proof_sections"]["cost"] == "partial"  # duration only


def test_no_cost_and_no_duration_is_missing():
    row = ablate(_drop("estimated_cost", "duration_seconds", *_TOKEN_KEYS))
    assert row["cost_raw"] is None
    assert row["proof_sections"]["cost"] == "missing"


@pytest.mark.parametrize("case_id", ["no_cost", "agent_tokens_no_cost"])
def test_cost_rebuilt_from_tokens_is_labelled_an_estimate(case_id):
    row = _row(case_id)
    assert row["cost_raw"] is not None and row["cost_raw"] > 0
    assert row["cost_provenance"] == "official_rate_estimate"
    assert row["cost_display"].endswith(" est.")


def test_agent_reported_tokens_keep_their_source():
    assert _row("agent_tokens_no_cost")["tokens_provenance"] == "agent_reported"


# --- each verification ablation states exactly what it lost -----------------

def test_agent_reported_only_is_labelled_a_claim():
    row = _row("agent_reported_only")
    assert row["verification_state"] == "agent_reported_passed"
    assert row["verification_authority"] == "agent_reported"
    assert row["verification_effective"] == "unknown"
    assert row["proof_sections"]["verification"] == "partial"
    assert row["proof_details"]["verification"] == "agent_reported_passed"
    assert "verification_unverified" in row["trust_penalties"]
    assert "verification" in row["missing_fields"]
    assert any("not independently observed" in line for line in row["trust_lines"])


def test_no_verification_is_not_observed():
    row = _row("no_verification")
    assert row["verification_state"] == "not_observed"
    assert row["verification_authority"] == "none"
    assert row["proof_sections"]["verification"] == "unknown"
    assert "verification_not_run" in row["trust_penalties"]
    assert {"status", "verification"} <= set(row["missing_fields"])


def test_no_checks_ran_is_not_run_not_passed():
    row = _row("no_checks_ran")
    assert row["verification_state"] == "not_run"
    assert row["verification_effective"] == "not_run"
    assert "verification_not_run" in row["trust_penalties"]


def test_legacy_booleans_without_origin_are_marked_derived():
    # Pre-v1 records with no capture/executor marker are read as native OpenShard
    # runs; the derived flag is the only signal that no verification block existed.
    row = _row("legacy_booleans_only")
    assert row["verification_derived"] is True
    assert _row("agent_reported_only")["verification_derived"] is False


def test_hook_booleans_are_attempted_not_verified():
    row = _row("hook_booleans_only")
    assert row["verification_state"] == "attempted_unverified"
    assert "verification_unverified" in row["trust_penalties"]


def test_import_booleans_are_not_observed():
    row = _row("import_booleans_only")
    assert row["verification_state"] == "not_observed"
    assert row["verification_authority"] == "none"


def test_everything_removed_is_the_floor():
    rows = {cid: ablate(m) for cid, _, m in CASES}
    floor = rows["everything"]
    assert floor["trust_score"] == min(r["trust_score"] for r in rows.values())
    assert floor["completeness_percent"] == min(r["completeness_percent"] for r in rows.values())
    assert PROOF_RANK[floor["proof_overall"]] == min(PROOF_RANK[r["proof_overall"]] for r in rows.values())


def test_report_lists_every_case():
    report = build_report()
    for case_id, _, _ in CASES:
        assert f"| {case_id} |" in report


if __name__ == "__main__":
    print(build_report())
