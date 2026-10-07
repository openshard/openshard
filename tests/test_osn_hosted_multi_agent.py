"""The hosted ``multi_agent`` Receipt block: built from the run, sent only behind ``advanced_osn``,
and valid against the Platform's committed receipt-sync JSON schema."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import jsonschema
import pytest

from openshard.history.receipt_evidence import multi_agent_block, project_entry_evidence
from openshard.osn.loop import run_bounded_loop
from openshard.osn.model_provider import IterativeModelProvider
from openshard.osn.run_entry import build_osn_run_entry
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats
from openshard.sync import client, envelope
from openshard.sync.capabilities import CAPABILITY_ADVANCED_OSN, OSN_RUN_CAPABILITIES

SCHEMA_PATH = Path(__file__).parent / "fixtures" / "platform" / "receipt-sync-envelope.v1.json"
PY = sys.executable
CHECK = [PY, "-c",
         "import sys; sys.exit(0 if open('src/a.txt').read()=='A' and open('src/b.txt').read()=='B' else 1)"]
TASK = "make src/a.txt contain A and src/b.txt contain B"


def _turn(*actions, note=""):
    return json.dumps({"actions": list(actions), "note": note})


def _a(kind, **kw):
    return {"kind": kind, "intent": kw.pop("intent", f"{kind} step"), **kw}


def _subtask(sid, path):
    return {"id": sid, "objective": f"write {path}", "allowed_write_paths": [path], "parallel_safe": True, "required": True}


PLAN = {"summary": "two independent files", "files": ["src/a.txt", "src/b.txt"], "steps": ["write both"],
        "verification": ["the check exits 0"], "simple": False,
        "subtasks": [_subtask("a", "src/a.txt"), _subtask("b", "src/b.txt")]}


class FakeByModel(BaseProvider):
    def __init__(self, replies):
        self.replies = {k: list(v) for k, v in replies.items()}
        self._lock = threading.Lock()

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        with self._lock:
            content = self.replies[model].pop(0)
        return ChatResponse(content, model, UsageStats(40, 10, 50, 0.002, cost_source="provider_reported"))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.txt").write_bytes(b"old")
    (r / "src" / "b.txt").write_bytes(b"old")
    return r


def _parallel_entry(repo):
    """A real parallel-workers run through the loop, recorded as a Shard entry."""
    from openshard.osn import roles
    from openshard.osn.decompose import decomposition_from_plan
    from openshard.osn.loop import _observe_verification
    from openshard.osn.synthesis import synthesize
    from openshard.osn.topology import TOPOLOGY_PARALLEL_SUBTASKS, decide_topology
    from openshard.osn.workers import WorkerSpec, run_workers

    fake = FakeByModel({
        "plan/m": [json.dumps({"plan": PLAN})],
        "fast/m": [_turn(_a("write_file", path="src/a.txt", content="A"), _a("finish"))],
        "deep/m": [_turn(_a("write_file", path="src/b.txt", content="B"), _a("finish"))],
    })
    usage: list = []

    def planner(sandbox, files):
        plan, role, u = roles.run_planner_turns(fake, "plan/m", task=TASK, repo_root=repo, sandbox=sandbox,
                                                repo_files=files, provider_name="fake", decompose=True)
        usage.extend(u)
        return plan, role.to_record()

    def verify(sb, paths):
        return _observe_verification(sb, paths, CHECK, 60.0, None)

    def workers(sandbox, plan, files):
        dec = decomposition_from_plan(plan)
        decision = decide_topology("auto", planner_ran=True, verifier_wanted=False, decomposition=dec,
                                   task_complex=True, budget_headroom=None, distinct_models_available=2)
        assert decision.selected == TOPOLOGY_PARALLEL_SUBTASKS
        specs = [WorkerSpec(f"worker-{i + 1}", st, m, provider_name="fake")
                 for i, (st, m) in enumerate(zip(dec.parallel_subtasks, ["fast/m", "deep/m"]))]
        results, u = run_workers(specs, provider=fake, task=TASK, plan=plan, repo_root=repo, base_sandbox=sandbox,
                                 verify=verify)
        usage.extend(u)
        synth = synthesize(results, main_sandbox=sandbox, scopes={s.worker_id: s.subtask.allowed_write_paths for s in specs})
        record = decision.to_record()
        record["actual_extra_cost_usd"] = sum(r.cost_usd for r in results)
        record["distinct_models"] = 2
        return {"topology": record, "ran": True, "workers": [r.to_record() for r in results],
                "synthesis": synth.to_record(), "applied": list(synth.applied), "blocked": [], "decisions": [],
                "advisory": None}

    provider = IterativeModelProvider(fake, ["exec/m"], repo)
    rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=2, planner=planner, workers=workers)
    assert rec.status == "verified", rec.stop_reason
    entry = build_osn_run_entry(rec, task=TASK, usage=usage, duration_seconds=2.0, repo_path=repo)
    entry["receipt_id"] = "rcpt_" + "a1" * 16
    return entry


def test_advanced_osn_is_read_at_sync_time_and_never_snapshotted_on_the_run():
    # The hosted capability_snapshot block is validated strictly by the Platform and lists three keys only.
    assert CAPABILITY_ADVANCED_OSN == "advanced_osn" and CAPABILITY_ADVANCED_OSN not in OSN_RUN_CAPABILITIES


def test_the_block_is_built_from_the_run_with_counts_and_decisions_only(repo):
    entry = _parallel_entry(repo)
    block = multi_agent_block(entry)
    assert block["topology"]["selected"] == "parallel_subtasks" and block["topology"]["worker_count"] == 2
    assert [w["worker_id"] for w in block["workers"]] == ["worker-1", "worker-2"]
    assert block["workers"][0] == {**block["workers"][0], "role": "worker", "status": "changed", "model": "fast/m",
                                   "files_changed": 1, "cost_usd": pytest.approx(0.002),
                                   "cost_source": "provider_reported", "selected": None}
    assert block["synthesis"]["applied_count"] == 2 and block["synthesis"]["resolution"] == "none_needed"
    assert block["economics"]["cost_per_verified_success"] == pytest.approx(0.006)
    assert block["candidates"] is None and block["resumed"] is None
    assert block["agents"]["count"] == 5 and block["agents"]["edges"] >= 3  # planner, executor, 2 workers, synthesis
    assert block["evidence"] == {"verification": "openshard_observed", "costs": "provider_usage_per_call",
                                 "graph": "derived_from_recorded_roles"}
    text = json.dumps(block)
    assert "src/a.txt" not in text and "sandbox" not in text and "prompt" not in text
    assert project_entry_evidence(entry)["multi_agent"] == block
    assert multi_agent_block({"osn_loop": {"status": "verified", "attempts": [{"n": 1}]}}) is None


def test_the_envelope_carries_the_block_only_when_asked_and_validates_against_the_platform_schema(repo):
    entry = _parallel_entry(repo)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    plain = envelope.build_envelope(entry, 0, core_version="0.5.0")
    assert "multi_agent" not in plain["receipt"]
    assert not list(validator.iter_errors(plain)), [e.message for e in validator.iter_errors(plain)][:3]

    full = envelope.build_envelope(entry, 0, core_version="0.5.0", multi_agent=True)
    assert full["receipt"]["multi_agent"]["topology"]["selected"] == "parallel_subtasks"
    errors = [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(full)]
    assert not errors, errors[:5]
    # The block changes the payload hash: a Platform that already holds the plain receipt sees a new version.
    assert envelope.payload_hash(full["receipt"]) != envelope.payload_hash(plain["receipt"])


def test_sync_asks_the_capability_once_and_strips_the_block_when_it_is_off(monkeypatch):
    calls: list[str] = []

    class Caps:
        def __init__(self, *a, **k):
            pass

        def enabled(self, key):
            calls.append(key)
            return False

    monkeypatch.setattr("openshard.sync.capabilities.LazyCapabilities", Caps)
    assert client._advanced_osn_enabled() is False and calls == [CAPABILITY_ADVANCED_OSN]

    class Broken:
        def __init__(self, *a, **k):
            raise RuntimeError("no platform")

    monkeypatch.setattr("openshard.sync.capabilities.LazyCapabilities", Broken)
    assert client._advanced_osn_enabled() is False  # never assumed on
