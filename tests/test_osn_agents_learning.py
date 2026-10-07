"""The derived agent graph of an OSN run, and the learning signals topology and parallel agents feed."""
from __future__ import annotations

import json

from openshard.history.receipt_evidence import agents_block
from openshard.learning.signals import KIND_AGENT_MODEL, KIND_TOPOLOGY, derive_signals, observe
from openshard.learning.snapshot import build_snapshot
from openshard.osn.agents import build_agent_graph
from tests.learning_fixtures import NOW, REPO, osn_entry


def _loop_with_everything():
    return {
        "status": "verified",
        "attempts": [{"n": 1, "turns": 2, "parallel_stage": True}],
        "plan": {"summary": "two pieces"},
        "roles": {
            "planner": {"role": "planner", "status": "ran", "model": "plan/m", "calls": 2, "cost_usd": 0.01,
                        "explorers": [{"index": 1, "status": "ran", "model": "fast/m", "calls": 1, "cost_usd": 0.001,
                                       "findings_count": 2, "sources": ["src/a.py"]}]},
            "executor": {"role": "executor", "status": "ran", "model": "exec/m", "calls": 3, "cost_usd": 0.02},
            "verifier": {"role": "verifier", "status": "ran", "model": "review/m", "calls": 1, "cost_usd": 0.002,
                         "independent": True},
        },
        "reviews": [{"verdict": "fail", "recovery_requested": True}, {"verdict": "pass"}],
        "topology": {"topology_selected": "parallel_subtasks"},
        "workers": [
            {"worker_id": "worker-1", "status": "changed", "model": "a/m", "calls": 2, "turns": 3, "cost_usd": 0.004,
             "verification": {"status": "failed", "scope": "worker_copy_informational"}, "changed_files": ["src/x.py"]},
            {"worker_id": "worker-2", "status": "failed", "reason": "malformed_reply:ActionParseError", "model": "b/m",
             "calls": 2, "turns": 2, "cost_usd": 0.003},
        ],
        "synthesis": {"applied": ["src/x.py"], "resolution": "executor_turns", "missing_required": [{"worker_id": "worker-2"}]},
    }


class TestAgentGraph:
    def test_graph_names_every_agent_and_what_connected_them(self):
        g = build_agent_graph(_loop_with_everything())
        ids = [n["agent_id"] for n in g["nodes"]]
        assert ids == ["planner", "explorer-1", "executor", "worker-1", "worker-2", "synthesis", "verifier"]
        roles = {n["agent_id"]: n["role"] for n in g["nodes"]}
        assert roles["explorer-1"] == "explorer" and roles["synthesis"] == "harness" and roles["worker-2"] == "worker"
        kinds = {(e["from"], e["to"]): e["kind"] for e in g["edges"]}
        assert kinds[("planner", "explorer-1")] == "asked" and kinds[("explorer-1", "planner")] == "answered"
        assert kinds[("planner", "executor")] == "planned_for" and kinds[("planner", "worker-1")] == "decomposed_into"
        assert kinds[("worker-1", "synthesis")] == "produced_for" and kinds[("synthesis", "executor")] == "resolved_by"
        assert kinds[("executor", "verifier")] == "reviewed_by" and kinds[("verifier", "executor")] == "recovery_requested_from"
        by_id = {n["agent_id"]: n for n in g["nodes"]}
        assert by_id["worker-1"]["outcome"] == "own-copy verification failed" and by_id["worker-2"]["outcome"] is None
        assert by_id["worker-2"]["reason"] == "malformed_reply:ActionParseError"
        assert by_id["verifier"]["outcome"] == "fail, pass" and by_id["synthesis"]["model"] is None
        assert g["models_distinct"] == ["a/m", "b/m", "exec/m", "fast/m", "plan/m", "review/m"]
        assert g["cost_usd"] == round(0.01 + 0.001 + 0.02 + 0.004 + 0.003 + 0.002, 6)
        assert g["evidence"]["graph"] == "derived_from_recorded_roles" and g["topology"] == "parallel_subtasks"

    def test_candidates_mark_the_winner_and_a_single_executor_has_no_graph(self):
        loop = {
            "status": "verified", "attempts": [{"n": 1}],
            "roles": {"executor": {"role": "executor", "status": "skipped", "reason": "workers_synthesised_cleanly"}},
            "topology": {"topology_selected": "parallel_candidates"},
            "candidates": {"winner": "candidate-2"},
            "workers": [{"worker_id": "candidate-1", "status": "changed", "model": "a/m", "cost_usd": 0.001},
                        {"worker_id": "candidate-2", "status": "changed", "model": "b/m", "cost_usd": 0.002}],
            "synthesis": {"applied": ["x"], "resolution": "none_needed"},
        }
        g = build_agent_graph(loop)
        kinds = {(e["from"], e["to"]): e["kind"] for e in g["edges"]}
        assert kinds[("candidate-1", "synthesis")] == "evaluated_by" and kinds[("candidate-2", "synthesis")] == "selected_by"
        assert kinds[("synthesis", "executor")] == "verified_without"
        assert next(n for n in g["nodes"] if n["agent_id"] == "synthesis")["outcome"] == "applied 1 file(s); winner candidate-2"
        assert build_agent_graph({"status": "verified", "attempts": [{"n": 1}]}) is None

    def test_projection_keeps_ids_roles_models_and_edges_but_no_paths(self):
        g = build_agent_graph(_loop_with_everything())
        g["nodes"][0]["prompt"] = "C:/secret/prompt.txt"
        block = agents_block(g)
        assert block["agents"] == 7 and block["nodes"][0]["agent_id"] == "planner" and "prompt" not in block["nodes"][0]
        assert {e["kind"] for e in block["edges"]} >= {"asked", "decomposed_into", "reviewed_by"}
        assert "secret" not in json.dumps(block) and "src/a.py" not in json.dumps(block)
        assert agents_block(None) is None


def _parallel_entry(*, topology, workers, cost=0.02, extra=0.004, verified=True, candidates=None, days_ago=1):
    e = osn_entry(attempts=(("exec/m", "passed" if verified else "failed"),), category="complex", cost=cost,
                  days_ago=days_ago)
    e["osn_loop"]["topology"] = {"topology_selected": topology, "actual_extra_cost_usd": extra}
    e["osn_loop"]["workers"] = workers
    if candidates:
        e["osn_loop"]["candidates"] = candidates
    return e


def _worker(wid, model, status="changed", own=None, reason=None, scope="worker_copy_informational"):
    w = {"worker_id": wid, "model": model, "status": status, "reason": reason}
    if own:
        w["verification"] = {"status": own, "scope": scope}
    return w


class TestLearningSignals:
    def test_topology_outcomes_count_verified_runs_and_extra_cost(self):
        entries = [
            _parallel_entry(topology="parallel_subtasks", workers=[_worker("worker-1", "a/m")], days_ago=d)
            for d in (1, 2, 3)
        ] + [_parallel_entry(topology="parallel_subtasks", workers=[], verified=False, extra=0.01, days_ago=4),
             _parallel_entry(topology="planner_executor", workers=[], extra=None, days_ago=5)]
        index = derive_signals(entries, repo=REPO, now=NOW)
        topo = {s.subject["topology"]: s for s in index.signals if s.kind == KIND_TOPOLOGY}
        par = topo["parallel_subtasks"]
        assert par.samples == 4 and par.stats["runs_verified"] == 3
        assert par.stats["cost_per_verified_success_usd"] == 0.02 and par.stats["median_extra_cost_usd"] == 0.004
        assert "parallel_subtasks reached OpenShard-observed verification in 3 of 4 recorded runs" in par.summary
        assert topo["planner_executor"].stats["median_extra_cost_usd"] is None

    def test_agent_model_outcomes_count_contract_failures_and_own_copy_verification(self):
        entries = [
            _parallel_entry(topology="parallel_subtasks",
                            workers=[_worker("worker-1", "mini/m", "failed", reason="malformed_reply:ActionParseError"),
                                     _worker("worker-2", "qwen/m", own="passed")], days_ago=1),
            _parallel_entry(topology="parallel_candidates",
                            workers=[_worker("candidate-1", "mini/m", own="failed", scope="candidate_copy_observed"),
                                     _worker("candidate-2", "deep/m", own="passed", scope="candidate_copy_observed")],
                            candidates={"winner": "candidate-2"}, days_ago=2),
        ]
        index = derive_signals(entries, repo=REPO, now=NOW)
        agents = {(s.subject["model"], s.subject["role"]): s for s in index.signals if s.kind == KIND_AGENT_MODEL}
        mini_w = agents[("mini/m", "worker")]
        assert mini_w.stats["action_contract_failures"] == 1 and mini_w.stats["usable_results"] == 0
        assert "replies that were not valid actions" in mini_w.summary
        mini_c = agents[("mini/m", "candidate")]
        assert mini_c.stats["own_copy_failed"] == 1 and mini_c.stats["selected_as_winner"] == 0
        deep = agents[("deep/m", "candidate")]
        assert deep.stats["own_copy_verified"] == 1 and deep.stats["selected_as_winner"] == 1
        assert "chosen as the winner 1 time(s)" in deep.summary
        obs = observe(entries[1])
        assert obs.topology == "parallel_candidates" and obs.agents[1].selected and obs.extra_cost_usd == 0.004

    def test_new_kinds_survive_a_snapshot_round_trip(self):
        entries = [_parallel_entry(topology="parallel_subtasks", workers=[_worker("worker-1", "a/m", own="passed")],
                                   days_ago=d) for d in (1, 2, 3)]
        lines = [json.dumps(e) for e in entries]
        snapshot = build_snapshot(lines, repo=REPO, harness="osn_loop", now=NOW)
        kinds = {s["kind"] for s in snapshot["signals"]}
        assert KIND_TOPOLOGY in kinds and KIND_AGENT_MODEL in kinds
