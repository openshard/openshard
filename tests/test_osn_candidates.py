"""Parallel candidates: distinct models on the whole task, verified in their own copies, ranked deterministically."""
from __future__ import annotations

import json
import shutil
import sys
import threading

import pytest

from openshard.history.receipt_evidence import candidates_block
from openshard.osn.candidates import (
    CANDIDATE_SCOPE,
    EVALUATION_POLICY,
    REASON_NO_WINNER,
    REASON_WINNER,
    candidate_specs,
    candidates_advisory,
    rank_candidates,
    select_candidate,
    verify_candidates,
)
from openshard.osn.loop import run_bounded_loop
from openshard.osn.model_provider import IterativeModelProvider
from openshard.osn.run_entry import build_osn_run_entry
from openshard.osn.synthesis import synthesize
from openshard.osn.topology import TOPOLOGY_PARALLEL_CANDIDATES, TOPOLOGY_SINGLE, decide_topology
from openshard.osn.workers import WorkerResult, run_workers
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
CHECK = [PY, "-c", "import sys; sys.exit(0 if open('src/a.txt').read()=='A' else 1)"]
TASK = "make src/a.txt contain A"


def _turn(*actions, note=""):
    return json.dumps({"actions": list(actions), "note": note})


def _a(kind, **kw):
    return {"kind": kind, "intent": kw.pop("intent", f"{kind} step"), **kw}


class FakeByModel(BaseProvider):
    def __init__(self, replies, cost=None):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.cost = cost or {}
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        with self._lock:
            self.calls.append((model, prompt))
            queue = self.replies.get(model) or []
            if not queue:
                raise RuntimeError(f"no scripted reply left for {model}")
            content = queue.pop(0)
        if isinstance(content, Exception):
            raise content
        c = self.cost.get(model, 0.002)
        return ChatResponse(content, model, UsageStats(40, 10, 50, c, cost_source="provider_reported" if c is not None else None))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.txt").write_bytes(b"old")
    (r / "README.md").write_bytes(b"# demo\n")
    return r


def _verify(sandbox, paths):
    from openshard.osn.loop import _observe_verification

    return _observe_verification(sandbox, paths, CHECK, 60.0, None)


def _result(wid, model, *, files=(), blocked=(), cost=0.01, turns=2, verified=None, status="changed"):
    r = WorkerResult(wid, wid, status, model=model, requested_model=model, changed_files=list(files),
                     blocked=list(blocked), cost_usd=cost, turns=turns, cost_source="provider_reported")
    if verified is not None:
        r.verification = {"status": "passed" if verified else "failed", "scope": "candidate_copy_observed",
                          "failed_tests": [] if verified else ["t"]}
    return r


class TestRanking:
    def test_order_is_verified_then_refusals_then_files_then_cost_then_turns(self):
        results = [
            _result("candidate-1", "a/m", files=("x", "y"), cost=0.001, verified=True),        # 2 files, cheap
            _result("candidate-2", "b/m", files=("x",), cost=0.05, verified=True),              # 1 file: wins
            _result("candidate-3", "c/m", files=("x",), cost=0.0001, verified=False),           # failed
            _result("candidate-4", "d/m", files=("x",), blocked=("secret",), cost=0.0, verified=True),  # refused write
        ]
        ranked = rank_candidates(results)
        assert [e["worker_id"] for e in ranked] == ["candidate-2", "candidate-1", "candidate-4", "candidate-3"]
        assert ranked[0]["selected"] and not ranked[1]["selected"] and ranked[3]["verified"] is False
        winner, record = select_candidate(results)
        assert winner is results[1] and record["winner"] == "candidate-2" and record["reason"] == REASON_WINNER
        assert record["policy"] == EVALUATION_POLICY and record["candidates_cost_usd"] == pytest.approx(0.0511)
        assert record["losers_cost_usd"] == pytest.approx(0.0011)

    def test_unknown_cost_ranks_last_among_equals_and_no_verified_means_no_winner(self):
        results = [
            _result("candidate-1", "a/m", files=("x",), cost=None, verified=True),
            _result("candidate-2", "b/m", files=("x",), cost=0.02, verified=True),
        ]
        assert rank_candidates(results)[0]["worker_id"] == "candidate-2"
        failed = [_result("candidate-1", "a/m", files=("x",), verified=False),
                  _result("candidate-2", "b/m", status="failed", cost=0.003)]
        winner, record = select_candidate(failed)
        assert winner is None and record["reason"] == REASON_NO_WINNER and record["losers_cost_usd"] == pytest.approx(0.013)
        advisory = candidates_advisory(record)
        assert "none verified" in advisory and "candidate-1 (a/m): failed" in advisory and "candidate-2 (b/m): failed" in advisory

    def test_ranking_is_deterministic_for_identical_candidates(self):
        results = [_result("candidate-1", "a/m", files=("x",), verified=True),
                   _result("candidate-2", "b/m", files=("x",), verified=True)]
        assert [e["worker_id"] for e in rank_candidates(results)] == ["candidate-1", "candidate-2"]
        assert [e["worker_id"] for e in rank_candidates(list(reversed(results)))] == ["candidate-2", "candidate-1"]


class TestCandidatesRun:
    def test_candidates_are_verified_in_their_copies_and_the_winner_is_synthesised(self, repo, tmp_path):
        fake = FakeByModel({
            "fast/m": [_turn(_a("write_file", path="src/a.txt", content="wrong"), _a("finish"))],
            "deep/m": [_turn(_a("write_file", path="src/a.txt", content="A"), _a("finish"))],
        }, cost={"fast/m": 0.001, "deep/m": 0.004})
        specs = candidate_specs([("fast/m", "executor"), ("deep/m", "adaptive_routing_v2")], TASK, "fake")
        assert [s.worker_id for s in specs] == ["candidate-1", "candidate-2"]
        assert specs[0].subtask.allowed_write_paths == CANDIDATE_SCOPE and specs[0].max_turns == 10
        main = tmp_path / "main"
        shutil.copytree(repo, main)
        results, usage = run_workers(specs, provider=fake, task=TASK, plan=None, repo_root=repo, base_sandbox=main,
                                     verify=_verify)
        assert [r.status for r in results] == ["changed", "changed"] and all(r.verification is None for r in results)
        verify_candidates(results, _verify)
        assert results[0].verification["status"] == "failed" and results[1].verification["status"] == "passed"
        assert results[0].verification["scope"] == "candidate_copy_observed"
        winner, record = select_candidate(results)
        assert winner is results[1] and record["winner_model"] == "deep/m"
        synth = synthesize([winner], main_sandbox=main, scopes={winner.worker_id: CANDIDATE_SCOPE})
        assert synth.applied == ["src/a.txt"] and (main / "src" / "a.txt").read_bytes() == b"A"
        assert (repo / "src" / "a.txt").read_bytes() == b"old"

    def _hook(self, fake, repo, models):
        def hook(sandbox, plan, files):
            decision = decide_topology("candidates", planner_ran=False, verifier_wanted=False, decomposition=None,
                                       task_complex=True, budget_headroom=None, distinct_models_available=len(models))
            record = decision.to_record()
            if decision.selected != TOPOLOGY_PARALLEL_CANDIDATES:
                return {"topology": record, "ran": False}
            specs = candidate_specs(models, TASK, "fake")
            results, usage = run_workers(specs, provider=fake, task=TASK, plan=plan, repo_root=repo,
                                         base_sandbox=sandbox, verify=_verify)
            hook.usage.extend(usage)
            verify_candidates(results, _verify)
            winner, evaluation = select_candidate(results)
            record["actual_extra_cost_usd"] = evaluation["losers_cost_usd"]
            applied, synth_record = [], None
            if winner is not None:
                synth = synthesize([winner], main_sandbox=sandbox, scopes={winner.worker_id: CANDIDATE_SCOPE})
                applied, synth_record = list(synth.applied), synth.to_record()
            return {"topology": record, "ran": True, "workers": [r.to_record() for r in results],
                    "synthesis": synth_record, "candidates": evaluation, "applied": applied, "blocked": [],
                    "decisions": [d for r in results for d in r.decisions],
                    "advisory": None if winner else candidates_advisory(evaluation)}

        hook.usage = []
        return hook

    def test_loop_verifies_the_winner_again_and_records_every_candidate(self, repo):
        fake = FakeByModel({
            "fast/m": [_turn(_a("write_file", path="src/a.txt", content="wrong"), _a("finish"))],
            "deep/m": [_turn(_a("write_file", path="src/a.txt", content="A"), _a("finish"))],
        }, cost={"fast/m": 0.001, "deep/m": 0.004})
        provider = IterativeModelProvider(fake, ["fast/m"], repo)
        hook = self._hook(fake, repo, [("fast/m", "executor"), ("deep/m", "adaptive_routing_v2")])
        events: list = []
        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=2, workers=hook,
                               progress=lambda k, d: events.append((k, d)))
        assert rec.status == "verified", rec.stop_reason
        assert rec.topology["topology_selected"] == "parallel_candidates" and rec.topology["worker_count"] == 2
        assert rec.candidates["winner"] == "candidate-2" and rec.candidates["evaluated"][0]["model"] == "deep/m"
        assert rec.candidates["evaluated"][1]["verification"] == "failed"
        assert rec.topology["actual_extra_cost_usd"] == pytest.approx(0.001)  # the loser's cost
        assert rec.attempts[0].turns == 0 and rec.attempts[0].parallel_stage and rec.changed_files == ["src/a.txt"]
        assert provider.usage == []  # no executor turn was needed
        end = next(d for k, d in events if k == "stage_end")
        assert end["stage"] == "candidates" and end["winner"] == "candidate-2" and end["winner_model"] == "deep/m"
        entry = build_osn_run_entry(rec, task=TASK, usage=list(hook.usage), duration_seconds=1.0, repo_path=repo)
        loop = entry["osn_loop"]
        assert loop["candidates"]["policy"] == EVALUATION_POLICY
        assert entry["execution_model"] == "deep/m"  # the winner's implementation is the one that landed
        assert "tier_dispatch_receipt" not in entry  # no planner or verifier ran: no role dispatch to report
        assert loop["implementation_models"] == ["fast/m", "deep/m"]  # every model that made implementation calls
        assert loop["roles"]["executor"]["status"] == "skipped" and loop["economics"]["by_worker"] == {
            "candidate-1": pytest.approx(0.001), "candidate-2": pytest.approx(0.004)}
        assert loop["economics"]["cost_per_verified_success"] == pytest.approx(0.005)

    def test_no_verified_candidate_hands_the_task_to_the_executor_with_an_advisory(self, repo):
        fake = FakeByModel({
            "fast/m": [_turn(_a("write_file", path="src/a.txt", content="wrong"), _a("finish")),
                       _turn(_a("write_file", path="src/a.txt", content="A"), _a("finish"))],  # executor turn
            "deep/m": [RuntimeError("provider down")],
        })
        provider = IterativeModelProvider(fake, ["fast/m"], repo)
        hook = self._hook(fake, repo, [("fast/m", "executor"), ("deep/m", "adaptive_routing_v2")])
        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=2, workers=hook)
        assert rec.status == "verified", rec.stop_reason
        assert rec.candidates["winner"] is None and rec.candidates["reason"] == REASON_NO_WINNER
        assert rec.synthesis is None and rec.attempts[0].turns == 1
        executor_prompt = fake.calls[-1][1]
        assert "none verified" in executor_prompt and "candidate-2 (deep/m): failed" in executor_prompt

    def test_fewer_than_two_models_means_no_candidates_and_the_reason_is_recorded(self):
        d = decide_topology("candidates", planner_ran=False, verifier_wanted=False, decomposition=None,
                            task_complex=True, budget_headroom=None, distinct_models_available=1)
        assert d.selected == TOPOLOGY_SINGLE and d.reason == "fewer_than_two_distinct_models"
        d = decide_topology("candidates", planner_ran=False, verifier_wanted=False, decomposition=None,
                            task_complex=True, budget_headroom=False, distinct_models_available=3)
        assert d.reason == "budget_headroom_insufficient"


def test_candidates_block_projects_ranks_without_paths():
    block = candidates_block({
        "policy": EVALUATION_POLICY, "count": 2, "winner": "candidate-2", "winner_model": "deep/m",
        "reason": REASON_WINNER, "candidates_cost_usd": 0.005, "losers_cost_usd": 0.001,
        "evidence": {"verification": "openshard_observed_in_candidate_copy"},
        "evaluated": [{"worker_id": "candidate-2", "model": "deep/m", "rank": 1, "selected": True, "verified": True,
                       "verification": "passed", "files_changed": 1, "cost_usd": 0.004, "turns": 1,
                       "sandbox_path": "C:/secret/path"}],
    })
    assert block["winner"] == "candidate-2" and block["evaluated"][0]["rank"] == 1
    assert "secret" not in json.dumps(block) and block["verification_evidence"] == "openshard_observed_in_candidate_copy"
    assert candidates_block(None) is None
