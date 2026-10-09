"""Receipt honesty eval: does a Receipt claim only what its evidence supports?

Each scenario produces a Receipt the way Core produces one in use and then
reads it back through every surface a person or the Platform sees:

* the agent side is either the real Cursor hook fold
  (``adapters/claude_hooks.handle_hook``) fed the events an agent session
  emits, or the real OSN bounded loop (``osn/loop.run_bounded_loop``) with a
  scripted model provider -- the replies are scripted, the sandbox, the
  verify command, its exit code and the run entry are real;
* later evidence is a real ``openshard verify`` re-run (the same
  ``plan_checks`` / ``run_checks`` / ``build_attestation`` path the command
  uses, running a real check in the scenario's git repository), or a CI
  verdict built by ``ci_evidence.build_ci_attestation`` from a GitHub
  check-run response fixture (the only faked part is the network fetch);
* a few scenarios are hand-built legacy record shapes, named as such.

The Receipt is then read through ``build_shard_receipt`` /
``verification_truth.interpret_receipt``, the local ``history --json``
projection, the MCP projection, the hosted sync envelope and the hosted
verification-evidence envelope, and checked two ways:

``RULES``
    Honesty boundaries every scenario must respect, judged from the raw
    inputs (the stored record and the attestations), never from the
    interpretation under test: an observed outcome needs observed evidence,
    an agent's claim is never shown as verified, a failure is never hidden,
    missing cost/tokens/model stay missing and a recorded zero stays zero,
    an observed external session is never shown as OpenShard-controlled,
    every surface states the same verification, a model's price never
    changes evidence strength, and an edited record is flagged.
``Scenario.expect``
    The exact facts this scenario's Receipt must state.

The eval measures Receipt honesty, not model capability: a scripted
provider's output says nothing about any real model. Nothing here talks to a
network, a capture service or telemetry; every file is written under the
caller's working directory.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.history.shard_hash import compute_shard_hash, integrity_as_stored
from openshard.history.verification_truth import OBSERVED_STATES

SUITE = "receipt_honesty"

_OBSERVED_SOURCES = frozenset({"directly_observed", "git_verified", "independently_verified"})
_CONCLUSIVE = frozenset({"passed", "failed", "partial"})
_MODEL_FIELDS = ("routing_selected_model", "execution_model")
_COST_PRICES = (0.0, 2.5)  # a free model's run and a paid model's run

RULE_OBSERVED_NEEDS_EVIDENCE = "observed_outcome_needs_observed_evidence"
RULE_CLAIM_IS_NOT_VERIFIED = "agent_claim_is_not_shown_as_verified"
RULE_FAILURE_NOT_HIDDEN = "failure_is_never_hidden"
RULE_CLAIM_KEPT = "agent_claim_is_kept_beside_later_evidence"
RULE_MISSING_NOT_ZERO = "missing_cost_and_tokens_stay_missing"
RULE_ZERO_NOT_MISSING = "recorded_zero_cost_stays_zero"
RULE_MODEL_NOT_INVENTED = "model_is_never_invented"
RULE_OBSERVATION_NOT_CONTROL = "observed_session_is_not_openshard_controlled"
RULE_SURFACES_AGREE = "local_and_hosted_surfaces_agree"
RULE_PRICE_INDEPENDENT = "free_and_paid_runs_get_equal_evidence"
RULE_EDIT_DETECTED = "edited_record_is_flagged"
RULE_EXPECTATION = "scenario_expectation"

RULES: tuple[str, ...] = (
    RULE_OBSERVED_NEEDS_EVIDENCE,
    RULE_CLAIM_IS_NOT_VERIFIED,
    RULE_FAILURE_NOT_HIDDEN,
    RULE_CLAIM_KEPT,
    RULE_MISSING_NOT_ZERO,
    RULE_ZERO_NOT_MISSING,
    RULE_MODEL_NOT_INVENTED,
    RULE_OBSERVATION_NOT_CONTROL,
    RULE_SURFACES_AGREE,
    RULE_PRICE_INDEPENDENT,
    RULE_EDIT_DETECTED,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Built:
    """What a scenario produced: the stored record and every attestation naming it."""

    entry: dict
    attestations: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    build: Callable[[Path], Built]
    expect: Mapping[str, Any]


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str


@dataclass
class ScenarioResult:
    id: str
    title: str
    observed: dict[str, Any]
    violations: list[Violation]
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and not self.violations

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "passed": self.passed,
            "error": self.error,
            "violations": [{"rule": v.rule, "detail": v.detail} for v in self.violations],
            "observed": self.observed,
        }


@dataclass
class HonestyReport:
    results: list[ScenarioResult]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": SUITE,
            "passed": self.passed,
            "scenarios": len(self.results),
            "failed": sum(1 for r in self.results if not r.passed),
            "rules": list(RULES),
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# Reading a Receipt back through every surface
# ---------------------------------------------------------------------------


def observe(entry: dict, attestations: list[dict]) -> dict[str, Any]:
    """What each surface states about *entry*, in one flat dict."""
    from openshard.history.shard_contract import build_shard_receipt
    from openshard.history.verification_truth import interpret_receipt
    from openshard.history.views import receipt_to_dict
    from openshard.sync.envelope import receipt_payload
    from openshard.sync.evidence import build_evidence_envelope
    from openshard.verification.post_session import latest_for_entry

    post = latest_for_entry(entry, attestations)
    receipt = build_shard_receipt(entry, index=0, post_session_verification=post)
    truth = interpret_receipt(receipt).to_dict()
    local = receipt_to_dict(receipt, extended=True)
    mcp = receipt_to_dict(receipt, extended=False)
    hosted = receipt_payload(entry, 0)
    hosted_evidence = build_evidence_envelope(entry, 0, attestations, core_version="eval")
    hosted_state = hosted_evidence.get("state") if isinstance(hosted_evidence, dict) else None
    return {
        "state": truth["state"],
        "authority": truth["authority"],
        "effective_status": truth["effective_status"],
        "basis": truth["basis"],
        "claim_status": truth["claim_status"],
        "label": truth["label"],
        "truth": truth,
        "integrity": receipt.integrity_status,
        "agent": local["agent"],
        "origin": local["origin"],
        "capture_depth": local["capture_depth"],
        "model": local["model"],
        "cost": local["cost"],
        "cost_usd": local["cost_usd"],
        "cost_provenance": local["cost_provenance"],
        "tokens_input": local["tokens_input"],
        "tokens_output": local["tokens_output"],
        "tokens_provenance": local["tokens_provenance"],
        "verification_status": local["verification_status"],
        "verification": local["verification"],
        "mcp_verification_status": mcp["verification_status"],
        "hosted_verification_status": hosted["verification_status"],
        "hosted_verification": hosted["verification"],
        "hosted_origin": hosted["origin"],
        "hosted_cost_usd": hosted["cost_usd"],
        "hosted_tokens_input": hosted["tokens_input"],
        "hosted_has_usage": "usage" in hosted,
        "usage_tokens_status": (receipt.usage or {}).get("tokens", {}).get("status")
        if isinstance(receipt.usage, dict) else None,
        "usage_tokens_total": (receipt.usage or {}).get("tokens", {}).get("total")
        if isinstance(receipt.usage, dict) else None,
        "usage_cost_status": (receipt.usage or {}).get("cost", {}).get("status")
        if isinstance(receipt.usage, dict) else None,
        "usage_cost_usd": (receipt.usage or {}).get("cost", {}).get("usd")
        if isinstance(receipt.usage, dict) else None,
        "hosted_state": (
            {k: hosted_state.get(k) for k in ("state", "authority", "effective_status", "basis")}
            if isinstance(hosted_state, dict) else None
        ),
    }


# ---------------------------------------------------------------------------
# Honesty rules, judged from the raw inputs
# ---------------------------------------------------------------------------


def _names(entry: dict, item: dict) -> bool:
    rid = entry.get("receipt_id")
    if isinstance(rid, str) and rid:
        return item.get("receipt_id") == rid
    run_id = entry.get("run_id")
    return isinstance(run_id, str) and bool(run_id) and item.get("run_id") == run_id


def _later_witnesses(entry: dict, attestations: list[dict]) -> list[str]:
    """Statuses of conclusive OpenShard re-runs and CI verdicts naming *entry*, oldest first."""
    out: list[str] = []
    for item in attestations:
        if not isinstance(item, dict) or not _names(entry, item):
            continue
        raw = item.get("verification")
        block: dict = raw if isinstance(raw, dict) else {}
        status = block.get("status")
        if item.get("kind") == "post_session_verification":
            ok = (block.get("source") == "directly_observed"
                  and block.get("observation_mode") == "openshard_executed" and status in _CONCLUSIVE)
        elif item.get("kind") == "ci_verification":
            ok = (block.get("source") == "independently_verified" and block.get("observation_mode") == "ci_report"
                  and bool(block.get("artifact_sha")) and status in ("passed", "failed"))
        else:
            ok = False
        if ok:
            out.append(str(status))
    return out


def _session_block(entry: dict) -> dict | None:
    block = entry.get("verification")
    return block if isinstance(block, dict) else None


def _openshard_ran_it(entry: dict) -> bool:
    """A positive record that OpenShard itself executed the run (``shard.derive_shard_identity``'s signals)."""
    executor, workflow = entry.get("executor"), entry.get("workflow")
    return (executor in ("native", "osn_loop", "opencode") or workflow in ("native", "osn_loop", "opencode")
            or "retry_triggered" in entry)


def _session_witness(entry: dict) -> str | None:
    """The session's own observed outcome, when its evidence is observed; else None."""
    block = _session_block(entry)
    if block is not None:
        status = block.get("status")
        if block.get("source") in _OBSERVED_SOURCES and status in _CONCLUSIVE:
            return str(status)
        return None
    if not _openshard_ran_it(entry) or "capture" in entry:
        return None
    passed = entry.get("verification_passed")
    if isinstance(passed, bool):
        return "passed" if passed else "failed"
    osn = entry.get("osn_verification_contract")
    if isinstance(osn, dict) and osn.get("status") in ("passed", "failed"):
        return str(osn["status"])
    return None


def _has_recorded_cost(entry: dict) -> bool:
    if entry.get("estimated_cost") is not None:
        return True
    runs = entry.get("stage_runs")
    return isinstance(runs, list) and any(isinstance(s, dict) and s.get("cost") is not None for s in runs)


def _has_model_source(entry: dict) -> bool:
    if any(isinstance(entry.get(k), str) and entry.get(k) for k in _MODEL_FIELDS):
        return True
    runs = entry.get("stage_runs")
    return isinstance(runs, list) and any(isinstance(s, dict) and s.get("model") for s in runs)


def _priced(entry: dict, price: float) -> dict:
    """*entry* as if the same run had cost *price*; re-hashed only when its hash was valid."""
    variant = copy.deepcopy(entry)
    variant["estimated_cost"] = price
    if entry.get("content_hash") == compute_shard_hash(entry):
        variant["content_hash"] = compute_shard_hash(variant)
    return variant


def _evidence_view(obs: dict[str, Any]) -> dict[str, Any]:
    return {"truth": obs["truth"], "origin": obs["origin"], "capture_depth": obs["capture_depth"],
            "verification": obs["verification"], "verification_status": obs["verification_status"]}


def check_rules(
    entry: dict, attestations: list[dict], obs: dict[str, Any],
    *, observer: Callable[[dict, list[dict]], dict[str, Any]] = observe,
) -> list[Violation]:
    """Every honesty boundary *obs* (what the surfaces state about *entry*) crosses."""
    out: list[Violation] = []
    later = _later_witnesses(entry, attestations)
    session = _session_witness(entry)
    witnesses = ([session] if session else []) + later
    block = _session_block(entry) or {}
    claim = block.get("status") if block.get("source") == "agent_reported" else None

    claims_observed = obs["state"] in OBSERVED_STATES or (
        obs["authority"] in _OBSERVED_SOURCES and obs["state"] not in ("not_run", "attempted_unverified")
    )
    if claims_observed and not witnesses:
        out.append(Violation(RULE_OBSERVED_NEEDS_EVIDENCE,
                             f"Receipt states {obs['state']} on {obs['authority']} authority, "
                             "but nothing OpenShard or CI observed supports an outcome"))
    if obs["effective_status"] == "passed" and "passed" not in witnesses:
        out.append(Violation(RULE_OBSERVED_NEEDS_EVIDENCE,
                             "effective status is passed without an observed pass"))
    if str(obs["label"]).startswith("Passed") and obs["state"] not in OBSERVED_STATES:
        out.append(Violation(RULE_CLAIM_IS_NOT_VERIFIED, f"label {obs['label']!r} on an unobserved outcome"))
    if claim == "passed" and not witnesses and obs["effective_status"] == "passed":
        out.append(Violation(RULE_CLAIM_IS_NOT_VERIFIED, "the agent's own pass is shown as a verified pass"))

    if later and later[-1] == "failed" and obs["effective_status"] != "failed":
        out.append(Violation(RULE_FAILURE_NOT_HIDDEN,
                             f"newest observed evidence failed but the Receipt states {obs['effective_status']}"))
    if not later and session is None and claim == "failed" and obs["effective_status"] != "failed":
        out.append(Violation(RULE_FAILURE_NOT_HIDDEN,
                             f"the agent reported a failure but the Receipt states {obs['effective_status']}"))
    if claim in _CONCLUSIVE and obs["claim_status"] != claim:
        out.append(Violation(RULE_CLAIM_KEPT, f"agent claimed {claim}; Receipt keeps {obs['claim_status']!r}"))

    # Without a recorded cost the only honest figure is a labelled estimate
    # priced from recorded tokens (shard_contract's official-rate fallback).
    estimate = bool(obs["cost_provenance"]) and str(obs["cost"]).endswith("est.")
    if not _has_recorded_cost(entry) and not estimate:
        if obs["cost_usd"] is not None or obs["cost"] != "Not recorded":
            out.append(Violation(RULE_MISSING_NOT_ZERO, f"no cost recorded but Receipt states {obs['cost']!r}"))
    if not isinstance(entry.get("tokens_provenance"), str) and (
        obs["tokens_input"] is not None or obs["tokens_output"] is not None
    ):
        out.append(Violation(RULE_MISSING_NOT_ZERO, "no token provenance but Receipt states token counts"))
    if obs.get("hosted_has_usage"):
        out.append(Violation(RULE_SURFACES_AGREE, "hosted receipt-sync payload carries usage; that contract is closed"))
    if (
        not isinstance(entry.get("tokens_provenance"), str)
        and obs.get("usage_tokens_total") == 0
        and obs.get("usage_tokens_status") in ("observed", "reconciled")
    ):
        out.append(Violation(RULE_MISSING_NOT_ZERO, "no token provenance but usage states observed/reconciled 0"))
    cost = entry.get("estimated_cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost == 0 and obs["cost_usd"] != 0.0:
        out.append(Violation(RULE_ZERO_NOT_MISSING, f"recorded $0 cost became {obs['cost_usd']!r}"))
    if not _has_model_source(entry) and obs["model"] != "Not recorded":
        out.append(Violation(RULE_MODEL_NOT_INVENTED, f"no model recorded but Receipt states {obs['model']!r}"))

    if "capture" in entry and (obs["origin"] == "openshard_routed" or obs["capture_depth"] == "full"):
        out.append(Violation(RULE_OBSERVATION_NOT_CONTROL,
                             f"observed agent session shown as {obs['origin']}/{obs['capture_depth']}"))

    agree = {
        "verification_status (MCP)": (obs["mcp_verification_status"], obs["verification_status"]),
        "verification_status (hosted)": (obs["hosted_verification_status"], obs["verification_status"]),
        "verification (hosted)": (obs["hosted_verification"], obs["verification"]),
        "origin (hosted)": (obs["hosted_origin"], obs["origin"]),
        "cost_usd (hosted)": (obs["hosted_cost_usd"], obs["cost_usd"]),
        "tokens_input (hosted)": (obs["hosted_tokens_input"], obs["tokens_input"]),
    }
    if obs["hosted_state"] is not None:
        for key, value in obs["hosted_state"].items():
            agree[f"{key} (hosted evidence)"] = (value, obs[key])
    for name, (theirs, ours) in agree.items():
        if theirs != ours:
            out.append(Violation(RULE_SURFACES_AGREE, f"{name} states {theirs!r}, local Receipt {ours!r}"))

    views = [_evidence_view(observer(_priced(entry, p), attestations)) for p in _COST_PRICES]
    if any(v != views[0] for v in views[1:]):
        out.append(Violation(RULE_PRICE_INDEPENDENT, "the same run's evidence differs between a $0 and a paid cost"))

    stored = entry.get("content_hash")
    # A coerced record carries the loader's verdict over the stored bytes; only
    # a record without one is judged by recomputing over its current content.
    edited = integrity_as_stored(entry) == "mismatch" if integrity_as_stored(entry) is not None else (
        isinstance(stored, str) and stored and stored != compute_shard_hash(entry)
    )
    if edited and obs["integrity"] != "mismatch":
        out.append(Violation(RULE_EDIT_DETECTED, f"record no longer matches its hash; integrity {obs['integrity']!r}"))
    return out


def check_expectations(expect: Mapping[str, Any], obs: dict[str, Any]) -> list[Violation]:
    return [
        Violation(RULE_EXPECTATION, f"{key}: expected {want!r}, Receipt states {obs.get(key)!r}")
        for key, want in expect.items()
        if obs.get(key) != want
    ]


# ---------------------------------------------------------------------------
# Producing Receipts
# ---------------------------------------------------------------------------

# Scenario repositories are throw-away: the user's git config (commit signing,
# fsmonitor, hooks) never applies to them.
_QUIET_ENV = {
    "OPENSHARD_TELEMETRY": "off",
    "OPENSHARD_TELEMETRY_NO_BACKGROUND": "1",
    "OPENSHARD_LEARNING_WORKER": "0",
    "OPENSHARD_PR_LOOKUP": "off",
    "OPENSHARD_CAPTURE_DISABLE": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "openshard-eval", "GIT_AUTHOR_EMAIL": "eval@openshard.invalid",
    "GIT_COMMITTER_NAME": "openshard-eval", "GIT_COMMITTER_EMAIL": "eval@openshard.invalid",
}


@contextlib.contextmanager
def _quiet() -> Iterator[None]:
    """No telemetry, learning worker, capture service, PR lookup or user git config while scenarios run."""
    saved = {k: os.environ.get(k) for k in _QUIET_ENV}
    os.environ.update(_QUIET_ENV)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **_GIT_ENV})
    return proc.stdout.strip()


_CHECK = """\
from calc import add
assert add(2, 3) == 5, "add(2, 3) != 5"
"""


def _make_repo(root: Path) -> Path:
    """A git repository whose real check (``check.py``) passes only when ``calc.add`` is correct."""
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "check.py").write_text(_CHECK, encoding="utf-8")
    (root / "calc.py").write_text("def add(a, b):\n    raise NotImplementedError\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _last_entry(repo: Path) -> dict:
    lines = (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    return json.loads([ln for ln in lines if ln.strip()][-1])


def _cursor_session(repo: Path, *, code: str, reported_exit: int, model: str = "claude-4-sonnet") -> dict:
    """One Cursor agent session through the real hook fold: it edits ``calc.py`` and reports a test run."""
    from openshard.adapters.claude_hooks import handle_hook

    sid = str(uuid.uuid4())

    def send(event: str, **fields: Any) -> None:
        doc = {"conversation_id": sid, "generation_id": "g1", "hook_event_name": event, "model": model,
               "cursor_version": "1.7.0", "workspace_roots": [str(repo)], **fields}
        handle_hook(doc, env={}, agent="cursor")

    send("sessionStart", session_id=sid, is_background_agent=False)
    send("beforeSubmitPrompt", prompt="Implement calc.add and make the tests pass")
    (repo / "calc.py").write_text(code, encoding="utf-8")
    send("postToolUse", tool_name="Write", tool_use_id="w1", cwd=str(repo),
         tool_input={"path": str(repo / "calc.py")}, tool_output=json.dumps({"ok": True}))
    send("afterFileEdit", file_path=str(repo / "calc.py"), edits=[])
    send("postToolUse", tool_name="Shell", tool_use_id="t1", cwd=str(repo),
         tool_input={"command": "python -m pytest -q"},
         tool_output=json.dumps({"exitCode": reported_exit, "stdout": "1 passed" if reported_exit == 0 else "1 failed"}))
    send("stop", status="completed", loop_count=0)
    send("sessionEnd", session_id=sid, reason="completed", duration_ms=1000, is_background_agent=False,
         final_status="completed")
    return _last_entry(repo)


def _openshard_verify(repo: Path, entry: dict) -> list[dict]:
    """``openshard verify --approve`` on *repo*: run the contract check, record the attestation."""
    from openshard.verification.post_session import (
        build_attestation,
        load_attestations,
        plan_checks,
        record_attestation,
        run_checks,
        tree_state,
    )

    def stamp() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ``python check.py`` is not on the read-only allowlist; approving the
    # eval's own fixture check is the operator's ``--approve``.
    planned = plan_checks(repo, {"verification_commands": [[sys.executable, "check.py"]]}, entry)
    started = stamp()
    before = tree_state(repo)
    results = run_checks(planned, repo, approve=True, timeout=60, stream=False)
    after = tree_state(repo)
    record_attestation(repo, build_attestation(entry, results, before=before, after=after,
                                               started_at=started, completed_at=stamp()))
    return load_attestations(repo / ".openshard")


def _ci_verdict(repo: Path, entry: dict, conclusion: str) -> list[dict]:
    """``openshard verify --ci`` with GitHub's check-run response for HEAD replaced by a fixture."""
    from openshard.verification.ci_evidence import (
        build_ci_attestation,
        classify_check_runs,
        git_is_ancestor,
        resolve_ci_target,
    )
    from openshard.verification.post_session import (
        evidence_for_entry,
        load_attestations,
        record_attestation,
        tree_state,
    )

    existing = load_attestations(repo / ".openshard")
    target = resolve_ci_target(entry, evidence_for_entry(entry, existing), tree_state(repo),
                               is_ancestor=git_is_ancestor(repo))
    if target.sha is None:
        raise RuntimeError(f"no CI target: {target.refusal}")
    runs = [{"name": "tests", "head_sha": target.sha, "status": "completed", "conclusion": conclusion,
             "started_at": "2026-01-01T00:00:00Z"}]
    attestation = build_ci_attestation(entry, classify_check_runs(runs, target.sha), target,
                                       created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if attestation is None:
        raise RuntimeError("CI verdict recorded nothing")
    record_attestation(repo, attestation)
    return load_attestations(repo / ".openshard")


_GOOD = "def add(a, b):\n    return a + b\n"
_BAD = "def add(a, b):\n    return a - b\n"


def _scripted_osn_run(workdir: Path, *, model: str, cost: float | None, code: str) -> dict:
    """The real OSN bounded loop and run entry, with a scripted model provider."""
    from openshard.osn.loop import run_bounded_loop
    from openshard.osn.model_provider import ModelActionProvider
    from openshard.osn.run_entry import build_osn_run_entry
    from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

    class ScriptedProvider(BaseProvider):
        def list_models(self) -> list:
            return []

        def get_model_info(self, model_id: str) -> None:
            return None

        def execute(self, model: str, prompt: str, system: str | None = None,
                    max_tokens: int | None = None) -> ChatResponse:
            reply = json.dumps({"writes": [{"path": "calc.py", "content": code}]})
            return ChatResponse(reply, model, UsageStats(120, 40, 160, cost))

    repo = _make_repo(workdir / "repo")
    provider = ModelActionProvider(ScriptedProvider(), [model], repo)
    receipt = run_bounded_loop(repo, "Implement calc.add", provider, [sys.executable, "check.py"], max_attempts=1)
    entry = build_osn_run_entry(receipt, task="Implement calc.add", usage=provider.usage,
                                duration_seconds=1.0, repo_path=repo)
    entry["content_hash"] = compute_shard_hash(entry)
    return entry


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def _forged_pass(workdir: Path) -> Built:
    repo = _make_repo(workdir / "repo")
    return Built(_cursor_session(repo, code=_BAD, reported_exit=0))


def _forged_pass_rerun_fails(workdir: Path) -> Built:
    repo = _make_repo(workdir / "repo")
    entry = _cursor_session(repo, code=_BAD, reported_exit=0)
    return Built(entry, _openshard_verify(repo, entry))


def _claimed_fail_rerun_passes(workdir: Path) -> Built:
    repo = _make_repo(workdir / "repo")
    entry = _cursor_session(repo, code=_GOOD, reported_exit=1)
    _git(repo, "commit", "-q", "-am", "agent work")
    return Built(entry, _openshard_verify(repo, entry))


def _rerun_pass_then_ci_fails(workdir: Path) -> Built:
    repo = _make_repo(workdir / "repo")
    entry = _cursor_session(repo, code=_GOOD, reported_exit=0)
    _git(repo, "commit", "-q", "-am", "agent work")
    _openshard_verify(repo, entry)
    return Built(entry, _ci_verdict(repo, entry, "failure"))


def _tampered_upgrade(workdir: Path) -> Built:
    repo = _make_repo(workdir / "repo")
    entry = _cursor_session(repo, code=_BAD, reported_exit=0)
    entry["verification"]["source"] = "directly_observed"
    entry["verification"]["observation_mode"] = "openshard_executed"
    return Built(entry)


def _osn(model: str, cost: float | None, code: str) -> Callable[[Path], Built]:
    return lambda workdir: Built(_scripted_osn_run(workdir, model=model, cost=cost, code=code))


def _legacy(entry: dict) -> Callable[[Path], Built]:
    return lambda _workdir: Built(copy.deepcopy(entry))


_NOT_VERIFIED_PASS = {"state": "agent_reported_passed", "authority": "agent_reported", "effective_status": "unknown"}

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "agent_claims_pass_unverified",
        "Cursor agent writes a broken add() and reports its pytest run passed; nothing re-ran it",
        _forged_pass,
        {**_NOT_VERIFIED_PASS, "claim_status": "passed", "origin": "external_observed",
         "agent": "Cursor (external)", "model": "Claude 4 Sonnet", "cost": "Not recorded", "cost_usd": None,
         "tokens_input": None, "integrity": "valid"},
    ),
    Scenario(
        "agent_claims_pass_openshard_rerun_fails",
        "Same broken add(); openshard verify runs the repository check itself and it fails",
        _forged_pass_rerun_fails,
        {"state": "verified_failed", "authority": "directly_observed", "effective_status": "failed",
         "basis": "post_session", "claim_status": "passed", "origin": "external_observed"},
    ),
    Scenario(
        "agent_claims_fail_openshard_rerun_passes",
        "Agent writes a correct add() but reports a failing run; the committed work passes OpenShard's re-run",
        _claimed_fail_rerun_passes,
        {"state": "verified_passed", "authority": "directly_observed", "effective_status": "passed",
         "basis": "post_session", "claim_status": "failed"},
    ),
    Scenario(
        "rerun_passes_then_ci_fails_same_commit",
        "OpenShard's re-run passes on the commit, then CI fails on that same commit",
        _rerun_pass_then_ci_fails,
        {"state": "verified_failed", "authority": "independently_verified", "effective_status": "failed",
         "basis": "ci", "claim_status": "passed"},
    ),
    Scenario(
        "stored_claim_upgraded_by_edit",
        "The agent's reported pass is edited in runs.jsonl to look OpenShard-executed",
        _tampered_upgrade,
        {"integrity": "mismatch"},
    ),
    Scenario(
        "osn_free_model_verified",
        "OSN loop, free model reporting $0, correct add(); OpenShard runs the check",
        _osn("meta-llama/llama-3.3-70b-instruct:free", 0.0, _GOOD),
        {"state": "verified_passed", "authority": "directly_observed", "effective_status": "passed",
         "origin": "openshard_routed", "cost_usd": 0.0, "tokens_input": 120, "tokens_output": 40,
         "tokens_provenance": "provider_reported", "integrity": "valid"},
    ),
    Scenario(
        "osn_paid_model_verified",
        "The same run on a paid model reporting its cost",
        _osn("anthropic/claude-sonnet-4", 0.0123, _GOOD),
        {"state": "verified_passed", "authority": "directly_observed", "effective_status": "passed",
         "origin": "openshard_routed", "cost_usd": 0.0123, "tokens_input": 120, "tokens_output": 40,
         "tokens_provenance": "provider_reported", "integrity": "valid"},
    ),
    Scenario(
        "osn_unknown_cost_verified_failure",
        "OSN loop, provider reports no cost, broken add(); OpenShard's check fails",
        _osn("vendor/unpriced-model", None, _BAD),
        {"state": "verified_failed", "authority": "directly_observed", "effective_status": "failed",
         "cost": "Not recorded", "cost_usd": None, "tokens_input": 120},
    ),
    Scenario(
        "legacy_external_record_without_capture",
        "Legacy record shape: a Codex hook record whose capture block is gone, booleans say passed",
        _legacy({"timestamp": "2026-01-01T00:00:00Z", "task": "t", "executor": "codex_hooks",
                 "verification_attempted": True, "verification_passed": True}),
        {**_NOT_VERIFIED_PASS, "origin": "external_observed", "model": "Not recorded"},
    ),
    Scenario(
        "legacy_native_record",
        "Legacy record shape: an OpenShard pipeline run (retry_triggered stamped) whose check passed",
        _legacy({"timestamp": "2026-01-01T00:00:00Z", "task": "t", "execution_model": "openai/gpt-4o",
                 "retry_triggered": False, "verification_attempted": True, "verification_passed": True,
                 "estimated_cost": 0.0}),
        {"state": "verified_passed", "authority": "directly_observed", "origin": "openshard_routed",
         "cost_usd": 0.0},
    ),
)

# Boundaries Core is known to cross today. Each states what an honest Receipt
# would say; the suite does not run them by default (``--known-gaps`` does),
# and the tests require each to still fail so a fix promotes it to SCENARIOS.
KNOWN_GAPS: tuple[Scenario, ...] = (
    Scenario(
        "legacy_unknown_origin_record",
        "Legacy record shape: no executor, no OpenShard run marker, booleans say passed. "
        "verification._derive_legacy still reads it as OpenShard-executed.",
        _legacy({"timestamp": "2026-01-01T00:00:00Z", "task": "t",
                 "verification_attempted": True, "verification_passed": True}),
        {**_NOT_VERIFIED_PASS, "origin": "unknown", "model": "Not recorded"},
    ),
)


# ---------------------------------------------------------------------------
# Running the suite
# ---------------------------------------------------------------------------


def run_scenario(
    scenario: Scenario, workdir: Path, *, observer: Callable[[dict, list[dict]], dict[str, Any]] = observe,
) -> ScenarioResult:
    """Build *scenario* under *workdir*, read its Receipt back, and judge it. Never raises."""
    try:
        with _quiet():
            built = scenario.build(workdir)
            obs = observer(built.entry, built.attestations)
            violations = check_rules(built.entry, built.attestations, obs, observer=observer)
        violations += check_expectations(scenario.expect, obs)
        shown = {k: v for k, v in obs.items() if k not in ("truth", "verification", "hosted_verification")}
        return ScenarioResult(scenario.id, scenario.title, shown, violations)
    except Exception as exc:  # noqa: BLE001 -- one broken scenario must not hide the others
        return ScenarioResult(scenario.id, scenario.title, {}, [], error=f"{type(exc).__name__}: {exc}")


def run_suite(
    scenarios: tuple[Scenario, ...] = SCENARIOS, *, workdir: Path | None = None,
    observer: Callable[[dict, list[dict]], dict[str, Any]] = observe,
) -> HonestyReport:
    with contextlib.ExitStack() as stack:
        root = workdir or Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="openshard-honesty-")))
        return HonestyReport([run_scenario(s, root / s.id, observer=observer) for s in scenarios])


def render(report: HonestyReport) -> str:
    lines = [f"Receipt honesty eval: {len(report.results)} scenarios, "
             f"{sum(1 for r in report.results if not r.passed)} failed"]
    for r in report.results:
        lines.append(f"  {'PASS' if r.passed else 'FAIL'}  {r.id}")
        if r.error:
            lines.append(f"        error: {r.error}")
        for v in r.violations:
            lines.append(f"        {v.rule}: {v.detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    report = run_suite(SCENARIOS + KNOWN_GAPS if "--known-gaps" in args else SCENARIOS)
    print(json.dumps(report.to_dict(), indent=2, default=str) if "--json" in args else render(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
