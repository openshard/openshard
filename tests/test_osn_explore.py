"""Bounded parallel read-only exploration for the planner.

Workers are real read-only agent turns on the isolated copy (the harness
refuses writes), they run concurrently up to a small cap, their answers reach
the planner as observations, their usage and cost are recorded per worker,
and the planner stays the single reasoning owner. The only fake is the model.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from openshard.history import receipt_evidence as ev
from openshard.osn import explore, roles
from openshard.osn.actions import parse_turn
from openshard.osn.budget import BudgetExhausted, BudgetLedger, BudgetLimits
from openshard.osn.loop import create_isolated_copy
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

TASK = "add a batch helper next to the existing slug helper"


class ThreadSafeFakeModel(BaseProvider):
    """Replies keyed by a marker found in the prompt; records concurrency."""

    def __init__(self, routes, delay=0.05):
        self.routes = routes  # list of (marker, [replies...])
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls: list[tuple[str, str]] = []

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append((model, system or ""))
            reply = None
            for marker, replies in self.routes:
                if marker in prompt and replies:
                    reply = replies.pop(0)
                    break
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        if reply is None:
            raise RuntimeError("no reply routed for this prompt")
        return ChatResponse(reply, model, UsageStats(40, 10, 50, 0.001, cost_source="provider_reported"))


def _answer(*findings, sources=()):
    return json.dumps({"findings": list(findings), "sources": list(sources)})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "slug.py").write_text("def slugify(t): return t\n")
    (r / "tests").mkdir()
    (r / "tests" / "test_slug.py").write_text("from src.slug import slugify\n")
    return r


class TestContract:
    def test_explore_and_findings_are_parsed_and_bounded(self):
        t = parse_turn(json.dumps({
            "actions": [{"kind": "list_files"}],
            "explore": [{"question": "where is slugify?", "paths_hint": ["src/slug.py", "../x", "C:/y"]},
                        "how are tests laid out", {"question": ""}, {"question": "q3"}, {"question": "q4"}],
        }))
        assert [q["question"] for q in t.explore] == ["where is slugify?", "how are tests laid out", "q3"]
        assert t.explore[0]["paths_hint"] == ["src/slug.py"]
        ans = parse_turn(json.dumps({"findings": ["a" * 500] + ["b"] * 20, "sources": ["src/slug.py", "/abs"]}))
        assert [a.kind for a in ans.actions] == ["finish"]  # an answer alone ends the worker's turns
        assert len(ans.findings) == 8 and len(ans.findings[0]) == 240 and ans.sources == ["src/slug.py"]


class TestExplorers:
    def test_workers_run_in_parallel_read_only_and_answer(self, repo):
        model = ThreadSafeFakeModel([
            ("question 1", [json.dumps({"actions": [{"kind": "read_file", "path": "src/slug.py"},
                                                    {"kind": "write_file", "path": "x", "content": "y"}]}),
                            _answer("slugify lives in src/slug.py", sources=["src/slug.py"])]),
            ("question 2", [_answer("tests import from src.slug", sources=["tests/test_slug.py"])]),
            ("question 3", ["not json", "still not json"]),
        ])
        sandbox = create_isolated_copy(repo)
        ledger = BudgetLedger(BudgetLimits(max_spend_usd=1.0))
        results, usage = explore.run_explorers(
            [{"question": f"question {i}", "paths_hint": []} for i in (1, 2, 3)],
            provider=model, model="fast/m", task=TASK, repo_root=repo, sandbox=sandbox,
            repo_files=["src/slug.py", "tests/test_slug.py"], budget=ledger,
        )
        assert [r.status for r in results] == ["answered", "answered", "failed"]
        assert results[0].findings == ["slugify lives in src/slug.py"] and results[0].sources == ["src/slug.py"]
        assert results[0].turns == 2 and results[0].calls == 2
        write = [a for a in results[0].actions if a["kind"] == "write_file"][0]
        assert write["decision"] == "invalid" and not write["executed"]
        assert not (sandbox / "x").exists()
        assert results[2].reason.startswith("malformed_reply")
        assert model.max_active >= 2  # ran concurrently
        assert all(u.role == "explorer" and u.attempt == 0 for u in usage)
        assert ledger.model_calls == len(usage) and ledger.spend_usd == pytest.approx(0.001 * len(usage))
        assert all("read-only exploration worker" in s for _, s in model.calls)

    def test_concurrency_is_capped_and_questions_bounded(self, repo):
        model = ThreadSafeFakeModel([(f"question {i}", [_answer(f"f{i}")]) for i in range(1, 6)], delay=0.1)
        results, _ = explore.run_explorers(
            [{"question": f"question {i}"} for i in range(1, 6)],
            provider=model, model="fast/m", task=TASK, repo_root=repo, sandbox=create_isolated_copy(repo),
            repo_files=[], max_workers=2,
        )
        assert len(results) == 3 and model.max_active <= 2  # MAX_EXPLORE_QUESTIONS and the worker cap

    def test_budget_stops_the_round_before_any_worker_spends(self, repo):
        model = ThreadSafeFakeModel([("question", [_answer("x")])])
        ledger = BudgetLedger(BudgetLimits(max_spend_usd=0.0005))
        ledger.record_model_call(0.001)  # already over the cap
        with pytest.raises(BudgetExhausted):
            explore.run_explorers([{"question": "question"}], provider=model, model="m", task=TASK,
                                  repo_root=repo, sandbox=create_isolated_copy(repo), repo_files=[], budget=ledger)
        assert model.calls == []

    def test_observations_render_answers_and_failures(self):
        results = [
            explore.ExplorerResult(0, "q1", "answered", findings=["a", "b"], sources=["s.py"]),
            explore.ExplorerResult(1, "q2", "no_answer", reason="no_findings_returned"),
        ]
        obs = explore.observations_for(results, turn=1)
        assert obs[0].status == "ok" and "- a" in obs[0].text and "Sources: s.py" in obs[0].text
        assert obs[1].status == "failed" and "no answer" in obs[1].text and obs[1].kind == "explore"


class TestPlannerWithExplorers:
    def test_findings_reach_the_planner_and_are_on_the_role_record(self, repo):
        plan = {"summary": "add slugify_all", "files": ["src/slug.py"], "steps": ["write"], "verification": ["tests"]}
        model = ThreadSafeFakeModel([
            ("Exploration question", [_answer("slugify is in src/slug.py", sources=["src/slug.py"]),
                                      _answer("tests use pytest", sources=["tests/test_slug.py"])]),
            ("Task:", [json.dumps({"actions": [{"kind": "list_files"}],
                                   "explore": [{"question": "where is slugify"}, {"question": "how are tests run"}]}),
                       json.dumps({"plan": plan})]),
        ])
        sandbox = create_isolated_copy(repo)
        events: list = []
        got_plan, role, usage = roles.run_planner_turns(
            model, "plan/m", task=TASK, repo_root=repo, sandbox=sandbox, repo_files=["src/slug.py"],
            explorer_model="fast/m", progress=lambda e, d: events.append((e, d)),
        )
        assert got_plan["summary"] == "add slugify_all" and role.status == "ran"
        assert len(role.explorers) == 2 and all(e["status"] == "answered" for e in role.explorers)
        assert role.explorers[0]["requested_model"] == "fast/m" and role.explorers[0]["findings_count"] == 1
        # The planner's second turn saw both answers.
        second_prompt_calls = [c for c in model.calls if c[0] == "plan/m"]
        assert len(second_prompt_calls) == 2
        assert any(e == "explore_start" and d["questions"] == 2 for e, d in events)
        assert any(e == "explore_end" and d["answered"] == 2 for e, d in events)
        # Usage: planner calls then explorer calls, all attempt 0, roles distinct.
        assert [u.role for u in usage] == ["planner", "planner", "explorer", "explorer"]
        assert "If the repository is large" in model.calls[0][1]  # planner was told it may explore

    def test_without_an_explorer_model_the_planner_is_not_offered_exploration(self, repo):
        model = ThreadSafeFakeModel([("Task:", [json.dumps({"plan": {"summary": "s", "steps": ["x"]}})])])
        _, role, usage = roles.run_planner_turns(model, "plan/m", task=TASK, repo_root=repo,
                                                 sandbox=create_isolated_copy(repo), repo_files=[])
        assert role.explorers == [] and "If the repository is large" not in model.calls[0][1]


class TestReceiptSurfaces:
    def test_local_projection_and_receipt_show_explorers_without_findings_text(self):
        record = {
            "osn_loop": {"mode": "turns", "roles": {"planner": {
                "status": "ran", "model": "plan/m", "calls": 2, "turns": 2, "cost_usd": 0.002,
                "cost_source": "provider_reported",
                "explorers": [{"index": 0, "role": "explorer", "question": "where is secret config", "status": "answered",
                               "model": "fast/m", "turns": 1, "calls": 1, "findings_count": 2,
                               "sources": ["a.py", "b.py"], "cost_usd": 0.001, "cost_source": "provider_reported",
                               "duration_ms": 120, "prompt_tokens": 40, "completion_tokens": 10}]}}},
        }
        block = ev.agent_loop_block(record)
        ex = block["roles"]["planner"]["explorers"][0]
        assert ex["status"] == "answered" and ex["sources_count"] == 2 and ex["findings_count"] == 2
        assert "question" not in ex and "a.py" not in json.dumps(block)
        from openshard.history.shard_contract import build_shard_receipt, render_full_shard_receipt

        entry = {"receipt_id": "rcpt_" + "1" * 32, "timestamp": "2026-10-07T05:00:00Z", "task": "t",
                 "executor": "osn_loop", "schema_version": "1.2", **record}
        text = render_full_shard_receipt(build_shard_receipt(entry, index=0))
        assert "explorer 1" in text and "answered" in text and "2 findings" in text and "$0.0010" in text
