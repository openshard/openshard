"""The iterative OSN agent loop: the model chooses bounded actions turn by turn,
the harness validates and performs each one, verification is observed, and the
Receipt records every action with its decision and effect.

The only fake here is the model (a scripted turn provider). Everything after
its reply is the real loop: path safety, the file-mutation gate, the budget
ledger, real reads and writes in the isolated copy, a real verify command run
by OpenShard, and the real Shard entry / Receipt projections.
"""
from __future__ import annotations

import json
import sys

import pytest

from openshard.history import receipt_evidence as ev
from openshard.history.shard_contract import build_shard_receipt, render_full_shard_receipt
from openshard.history.verification import derive_verification
from openshard.history.views import receipt_to_dict
from openshard.osn.actions import (
    ActionParseError,
    parse_turn,
    summarize_actions,
)
from openshard.osn.agent_loop import TurnState
from openshard.osn.budget import BudgetLedger, BudgetLimits
from openshard.osn.loop import LoopReceipt, run_bounded_loop
from openshard.osn.model_provider import AttemptUsage, IterativeModelProvider, build_turn_prompt
from openshard.osn.run_entry import build_osn_run_entry
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
CHECK = [PY, "-c", "import sys; sys.exit(0 if open('src/app.txt').read()=='ok' else 1)"]
TASK = "make src/app.txt contain ok"


def _turn(*actions, note=""):
    return json.dumps({"actions": list(actions), "note": note})


def _a(kind, **kw):
    return {"kind": kind, "intent": kw.pop("intent", f"{kind} step"), **kw}


class ScriptedTurns:
    """A turn provider that replays scripted replies and records the states it saw."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.states: list[TurnState] = []
        self.usage: list[AttemptUsage] = []

    def turn(self, state: TurnState):
        self.states.append(state)
        if not self.replies:
            raise AssertionError("no scripted reply left")
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(state)
        self.usage.append(AttemptUsage(state.attempt, "fake/m", 10, 5, 0.001, requested_model="fake/m",
                                       turn=state.turn, cost_source="provider_reported", duration_ms=3))
        return parse_turn(reply)

    def pending_model_for(self, attempt):
        return "fake/m"


class FakeModel(BaseProvider):
    def __init__(self, replies, cost=0.002, cost_source="provider_reported"):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.cost = cost
        self.cost_source = cost_source

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        self.prompts.append(prompt)
        content = self.replies.pop(0)
        return ChatResponse(content, model, UsageStats(100, 20, 120, self.cost, cost_source=self.cost_source))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "app.txt").write_text("bad")
    (r / "README.md").write_text("# demo\n")
    return r


class TestActionContract:
    def test_rejects_unknown_kind_missing_fields_and_oversize(self):
        for bad in (
            "not json",
            "[]",
            json.dumps({"actions": []}),
            json.dumps({"actions": [{"kind": "run_command", "cmd": "rm -rf /"}]}),
            json.dumps({"actions": [{"kind": "write_file", "path": "a.py"}]}),
            json.dumps({"actions": [{"kind": "read_file"}]}),
            json.dumps({"actions": [{"kind": "write_file", "path": "a", "content": "x" * 300_000}]}),
            json.dumps({"actions": [{"kind": "read_file", "path": "a"}] * 20}),
        ):
            with pytest.raises(ActionParseError):
                parse_turn(bad)

    def test_actions_after_finish_are_dropped_and_legacy_writes_accepted(self):
        t = parse_turn(_turn(_a("finish"), _a("read_file", path="x")))
        assert [a.kind for a in t.actions] == ["finish"]
        legacy = parse_turn(json.dumps({"writes": [{"path": "a.py", "content": "x"}]}))
        assert [a.kind for a in legacy.actions] == ["write_file", "finish"] and legacy.legacy_writes

    def test_intent_is_bounded_and_single_line(self):
        t = parse_turn(_turn({"kind": "finish", "intent": "a\nb\x00" + "c" * 500}))
        assert "\n" not in t.actions[0].intent and len(t.actions[0].intent) <= 200


class TestIterativeAttempt:
    def test_inspect_read_write_verify_fail_repair_verify_pass(self, repo):
        model = ScriptedTurns([
            _turn(_a("list_files"), _a("search_repo", query="bad"), note="looking around"),
            _turn(_a("read_file", path="src/app.txt"), _a("write_file", path="src/app.txt", content="nope"),
                  _a("run_verification")),
            lambda s: _turn(_a("write_file", path="src/app.txt", content="ok"), _a("run_verification"))
            if s.last_verification and s.last_verification.startswith("failed") else _turn(_a("finish")),
            _turn(_a("get_diff"), _a("finish", intent="verified change")),
        ])
        events: list[tuple[str, dict]] = []
        rec = run_bounded_loop(repo, TASK, model, CHECK, max_attempts=1, progress=lambda e, d: events.append((e, d)))

        assert rec.status == "verified" and rec.stop_reason == "verification_passed"
        assert rec.mode == "turns"
        assert (repo / "src" / "app.txt").read_text() == "bad"  # real repo untouched
        a = rec.attempts[0]
        assert a.turns == 4 and a.verifications_in_turn == 2 and a.turn_stop == "finished"
        kinds = [x["kind"] for x in a.actions]
        assert kinds == ["list_files", "search_repo", "read_file", "write_file", "run_verification",
                         "write_file", "run_verification", "get_diff", "finish"]
        # The model saw the search hit, the file content and the failing verification.
        s2 = model.states[1]
        assert any(o.kind == "search_repo" and "src/app.txt:1:bad" in o.text for o in s2.observations)
        s3 = model.states[2]
        assert s3.last_verification == "failed (exit 1)" and s3.writes_applied == 1
        assert any(o.kind == "read_file" and o.text == "bad" for o in s3.observations)
        # Writes carry before/after evidence, never content.
        w = [x for x in a.actions if x["kind"] == "write_file"]
        assert w[0]["decision"] == "allow" and w[0]["executed"] and w[0]["ok"]
        assert w[0]["result"]["change_type"] == "update" and w[0]["result"]["bytes_after"] == 4
        assert w[1]["result"]["sha256_after"] != w[0]["result"]["sha256_after"]
        blob = json.dumps(rec.to_dict())
        assert "nope" not in blob  # written content is never stored
        assert blob.count('"bad"') == 1  # only the search query, never the file content read
        # The passing in-turn verification is the attempt's verification: not run twice.
        assert a.verification is not None and a.verification.passed
        in_turn = [d for e, d in events if e == "verification_result" and d.get("in_turn")]
        assert [d["status"] for d in in_turn] == ["failed", "passed"]
        assert ("verification_reused", {"attempt": 1}) in events  # the loop did not run it a third time
        summary = summarize_actions([])
        assert set(a.to_dict() if hasattr(a, "to_dict") else {}) == set() or summary["actions"] == 0
        d = rec.to_dict()["attempts"][0]
        assert d["action_summary"]["writes_applied"] == 2 and d["action_summary"]["verifications"] == 2
        assert d["final_note"] == "verified change"

    def test_final_verification_runs_when_files_changed_after_last_check(self, repo):
        model = ScriptedTurns([
            _turn(_a("write_file", path="src/app.txt", content="nope"), _a("run_verification")),
            _turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish", intent="done")),
        ])
        rec = run_bounded_loop(repo, TASK, model, CHECK, max_attempts=1)
        assert rec.status == "verified"
        a = rec.attempts[0]
        assert a.verifications_in_turn == 1 and a.verification.passed  # final check run by the loop

    def test_finish_without_verifying_still_gets_verified_by_the_loop(self, repo):
        model = ScriptedTurns([_turn(_a("write_file", path="src/app.txt", content="still bad"), _a("finish"))])
        rec = run_bounded_loop(repo, TASK, model, CHECK, max_attempts=1)
        assert rec.status == "failed" and rec.verification_state == "failed"
        assert rec.attempts[0].actions[-1]["kind"] == "finish"

    def test_unsafe_and_protected_reads_are_refused_reported_and_the_task_continues(self, repo, tmp_path):
        model = ScriptedTurns([
            _turn(_a("read_file", path="../outside.txt"), _a("read_file", path=".env"), _a("list_files", path="..")),
            _turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish")),
        ])
        (tmp_path / "outside.txt").write_text("s3cr3t-outside-content")
        rec = run_bounded_loop(repo, TASK, model, CHECK, max_attempts=1)
        acts = rec.attempts[0].actions
        assert [x["decision"] for x in acts[:3]] == ["deny", "deny", "deny"]
        assert acts[0]["target"] == "<unsafe-path>" and acts[0]["error_class"] == "unsafe_path"
        assert acts[1]["error_class"] == "protected_path" and not any(x["executed"] for x in acts[:3])
        obs = [o for o in model.states[1].observations if o.turn == 1]
        assert len(obs) == 3 and all(o.status == "refused" for o in obs)
        assert "s3cr3t-outside-content" not in json.dumps([o.text for o in obs])
        assert rec.status == "verified"
        assert [d["decision"] for d in rec.attempts[0].decisions] == ["allow"]  # reads are not write decisions

    def test_a_refused_write_stops_the_attempt_and_the_run_is_blocked(self, repo, tmp_path):
        for bad_path, source in (("../evil.txt", "path_safety"), (".env", "file_mutation_policy")):
            model = ScriptedTurns([
                _turn(_a("write_file", path="src/app.txt", content="ok"),
                      _a("write_file", path=bad_path, content="x"),
                      _a("write_file", path="README.md", content="never written")),
                _turn(_a("finish")),
            ])
            rec = run_bounded_loop(repo, TASK, model, CHECK, max_attempts=2)
            assert rec.status == "blocked" and rec.stop_reason == "policy_or_path_block"
            assert rec.verification_state == "not_run" and len(rec.attempts) == 1  # not retried, not verified
            a = rec.attempts[0]
            assert a.turn_stop == "policy_block" and a.applied == ["src/app.txt"] and a.blocked == [bad_path]
            assert [x["kind"] for x in a.actions] == ["write_file", "write_file"]  # the third never ran
            assert a.actions[1]["decision"] == "deny" and a.actions[1]["decision_source"] == source
            assert [d["decision"] for d in a.decisions] == ["allow", "deny"]
            assert not (tmp_path / "evil.txt").exists() and not (repo / ".env").exists()
            assert (repo / "README.md").read_text() == "# demo\n"
            assert len(model.replies) == 1  # the model was not asked again

    def test_ask_path_without_approver_fails_closed_and_blocks_the_run(self, repo):
        model = ScriptedTurns([_turn(_a("write_file", path="pyproject.toml", content="x"), _a("finish"))])
        rec = run_bounded_loop(repo, TASK, model, CHECK, max_attempts=1)
        assert rec.status == "blocked" and rec.stop_reason == "policy_or_path_block"
        act = rec.attempts[0].actions[0]
        assert act["decision"] == "ask" and act["approval_granted"] is None and not act["executed"]
        assert not (repo / "pyproject.toml").exists()

    def test_ask_path_with_approver_is_written_and_approval_recorded(self, repo):
        model = ScriptedTurns([
            _turn(_a("write_file", path="pyproject.toml", content="[tool.x]\n"),
                  _a("write_file", path="src/app.txt", content="ok"), _a("finish")),
        ])
        asked: list[str] = []

        def approver(rel, decision):
            asked.append(rel)
            return True, "test_reviewer"

        rec = run_bounded_loop(repo, TASK, model, CHECK, max_attempts=1, approver=approver)
        assert rec.status == "verified" and asked == ["pyproject.toml"]
        act = rec.attempts[0].actions[0]
        assert act["decision"] == "ask" and act["approval_granted"] is True and act["executed"]
        assert rec.attempts[0].policy["approval_sources"] == ["test_reviewer"]

    def test_write_budget_stops_before_the_write(self, repo):
        ledger = BudgetLedger(BudgetLimits(max_writes=1))
        model = ScriptedTurns([
            _turn(_a("write_file", path="src/app.txt", content="ok"),
                  _a("write_file", path="README.md", content="changed"), _a("finish")),
        ])
        rec = run_bounded_loop(repo, TASK, model, CHECK, max_attempts=1, budget=ledger)
        assert rec.status == "budget_exhausted" and rec.stop_reason == "budget_max_writes"
        acts = rec.attempts[0].actions
        assert acts[0]["executed"] and not acts[1]["executed"] and acts[1]["error_class"] == "budget_exhausted"
        assert ledger.writes == 1

    def test_command_budget_counts_in_turn_verifications(self, repo):
        ledger = BudgetLedger(BudgetLimits(max_commands=1))
        model = ScriptedTurns([
            _turn(_a("write_file", path="src/app.txt", content="nope"), _a("run_verification")),
            _turn(_a("write_file", path="src/app.txt", content="ok"), _a("run_verification")),
        ])
        rec = run_bounded_loop(repo, TASK, model, CHECK, max_attempts=1, budget=ledger)
        assert rec.status == "budget_exhausted" and rec.stop_reason == "budget_max_commands"
        assert ledger.commands == 1

    def test_verification_cap_per_attempt_is_enforced(self, repo):
        model = ScriptedTurns([
            _turn(_a("run_verification"), _a("run_verification"), _a("run_verification")),
            _turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish")),
        ])
        rec = run_bounded_loop(repo, TASK, model, CHECK, max_attempts=1, max_verifications_per_attempt=2)
        acts = rec.attempts[0].actions
        assert [x["executed"] for x in acts[:3]] == [True, True, False]
        assert acts[2]["decision"] == "invalid" and acts[2]["error_class"] == "cap_reached"
        assert rec.status == "verified"

    def test_max_turns_ends_the_attempt_and_the_loop_still_verifies(self, repo):
        model = ScriptedTurns([
            _turn(_a("write_file", path="src/app.txt", content="ok")),
            _turn(_a("list_files")),
            _turn(_a("list_files")),
        ])
        rec = run_bounded_loop(repo, TASK, model, CHECK, max_attempts=1, max_turns=2)
        assert rec.attempts[0].turns == 2 and rec.attempts[0].turn_stop == "max_turns"
        assert rec.status == "verified" and len(model.replies) == 1

    def test_no_writes_is_no_actions(self, repo):
        model = ScriptedTurns([_turn(_a("read_file", path="README.md"), _a("finish", intent="nothing to do"))])
        rec = run_bounded_loop(repo, TASK, model, CHECK)
        assert rec.status == "no_actions" and rec.verification_state == "not_run"

    def test_second_attempt_sees_previous_failure_and_escalates(self, repo):
        model = ScriptedTurns([
            _turn(_a("write_file", path="src/app.txt", content="nope"), _a("finish")),
            lambda s: _turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish"))
            if s.attempt == 2 and s.previous_failure and "exit code 1" in s.previous_failure else _turn(_a("finish")),
        ])
        rec = run_bounded_loop(repo, TASK, model, CHECK, max_attempts=2)
        assert rec.status == "verified" and [a.n for a in rec.attempts] == [1, 2]
        assert rec.attempts[0].verification.passed is False

    def test_identical_final_state_across_attempts_is_no_progress(self, repo):
        model = ScriptedTurns([
            _turn(_a("write_file", path="src/app.txt", content="nope"), _a("finish")),
            _turn(_a("write_file", path="src/app.txt", content="nope"), _a("finish")),
        ])
        rec = run_bounded_loop(repo, TASK, model, CHECK, max_attempts=3)
        assert rec.stop_reason == "no_progress_identical_actions" and len(rec.attempts) == 2

    def test_provider_error_mid_attempt_is_error_not_pass(self, repo):
        class Boom:
            def turn(self, state):
                raise RuntimeError("provider down at C:/secret/path")

        rec = run_bounded_loop(repo, TASK, Boom(), CHECK)
        assert rec.status == "error" and rec.stop_reason == "provider_error"
        assert rec.attempts[0].error_class == "RuntimeError" and rec.verification_state == "not_run"

    def test_verifier_that_rewrites_files_is_caught_in_turn(self, repo):
        rewriter = [PY, "-c", "open('src/app.txt','w').write('tampered')"]
        model = ScriptedTurns([_turn(_a("write_file", path="src/app.txt", content="ok"), _a("run_verification"))])
        rec = run_bounded_loop(repo, TASK, model, rewriter, max_attempts=1)
        assert rec.status == "failed" and rec.stop_reason == "verifier_modified_files"
        assert rec.attempts[0].actions[-1]["result"]["tainted"] is True

    def test_receipt_never_stores_tool_output_or_task_text(self, repo):
        model = ScriptedTurns([
            _turn(_a("read_file", path="README.md"), _a("search_repo", query="demo"),
                  _a("write_file", path="src/app.txt", content="ok"), _a("run_verification"), _a("finish")),
        ])
        rec = run_bounded_loop(repo, "secret task text", model, CHECK, max_attempts=1)
        blob = json.dumps(rec.to_dict())
        assert "secret task text" not in blob and "# demo" not in blob and str(repo) not in blob
        assert rec.to_dict()["evidence"]["action_results"] == "openshard_observed"


class TestIterativeModelProvider:
    def test_turn_prompt_and_reask_and_usage_provenance(self, repo):
        fake = FakeModel([
            "not json at all",
            _turn(_a("read_file", path="src/app.txt")),
            _turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish")),
        ])
        provider = IterativeModelProvider(fake, ["cheap/m", "strong/m"], repo, context_files=["README.md"])
        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=1)
        assert rec.status == "verified"
        assert [u.turn for u in provider.usage] == [1, 1, 2]  # the re-ask shares its turn
        assert all(u.role == "executor" and u.cost_source == "provider_reported" for u in provider.usage)
        assert all(u.duration_ms is not None for u in provider.usage)
        assert "# demo" in fake.prompts[0] and "<untrusted file=\"README.md\">" in fake.prompts[0]
        assert "Files shown to you on turn 1" in fake.prompts[2]
        assert 'action="read_file"' in fake.prompts[2] and "bad" in fake.prompts[2]
        assert fake.prompts[2].count("<untrusted") >= 1

    def test_prompt_is_bounded_and_frames_repo_content_as_untrusted(self, repo):
        from openshard.osn.agent_loop import Observation

        state = TurnState(TASK, 1, 2, 12, [f"f{i}.py" for i in range(500)],
                          [Observation(1, 0, "read_file", "a.py", "ok", "X" * 50_000)], [], [], None, 2, 0)
        p = build_turn_prompt(state, repo, [])
        assert "... and 300 more files" in p and '<untrusted turn="1" action="read_file"' in p
        assert len(p) <= 160_200

    def test_supervisor_override_applies_to_the_whole_next_attempt(self, repo):
        fake = FakeModel([
            _turn(_a("write_file", path="src/app.txt", content="nope"), _a("finish")),
            _turn(_a("read_file", path="src/app.txt")),
            _turn(_a("write_file", path="src/app.txt", content="ok"), _a("finish")),
        ])
        provider = IterativeModelProvider(fake, ["cheap/m"], repo)
        calls: list[str] = []
        original = fake.execute

        def spy(model, prompt, system=None, max_tokens=None):
            calls.append(model)
            return original(model, prompt, system=system, max_tokens=max_tokens)

        fake.execute = spy  # type: ignore[method-assign]
        provider.set_next_model("strong/m")  # as an applied supervisor would, before attempt 1 here
        run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=2)
        assert calls[0] == "strong/m" and calls[1] == "cheap/m" and calls[2] == "cheap/m"


class TestReceiptForIterativeRuns:
    def _run(self, repo, cost_source="provider_reported"):
        fake = FakeModel([
            _turn(_a("read_file", path="src/app.txt"), _a("write_file", path="src/app.txt", content="nope"),
                  _a("run_verification")),
            _turn(_a("write_file", path="src/app.txt", content="ok"), _a("run_verification"), _a("finish")),
        ], cost_source=cost_source)
        provider = IterativeModelProvider(fake, ["acme/m"], repo)
        rec = run_bounded_loop(repo, TASK, provider, CHECK, max_attempts=1)
        entry = build_osn_run_entry(rec, task=TASK, usage=provider.usage, duration_seconds=1.0, repo_path=repo)
        return rec, entry

    def test_entry_records_actions_model_calls_and_cost_provenance(self, repo):
        rec, entry = self._run(repo)
        assert rec.status == "verified"
        assert entry["cost_provenance"] == "provider_reported"
        assert entry["estimated_cost"] == pytest.approx(0.004)
        calls = entry["osn_loop"]["model_calls"]
        assert [c["turn"] for c in calls] == [1, 2] and all(c["role"] == "executor" for c in calls)
        assert all(c["cost_source"] == "provider_reported" and c["duration_ms"] is not None for c in calls)
        assert entry["osn_loop"]["mode"] == "turns"
        assert entry["osn_loop"]["action_summary"]["writes_applied"] == 2
        assert entry["osn_loop"]["turns_total"] == 2
        assert derive_verification(entry).status == "passed"
        assert entry["policy_decisions"] and all(d["decision"] == "allow" for d in entry["policy_decisions"])

    def test_list_rate_cost_is_an_estimate_never_a_bill(self, repo):
        from openshard.history.usage_evidence import usage_from_record

        _, entry = self._run(repo, cost_source="list_rate_estimate")
        assert entry["cost_provenance"] == "official_rate_estimate"
        usage = usage_from_record(entry)
        assert usage["cost"]["status"] == "estimated" and usage["cost"]["source"] == "openshard_calculated"
        observed = usage_from_record(self._run(repo)[1])
        assert observed["cost"]["status"] == "observed" and observed["cost"]["source"] == "provider_reported"
        assert isinstance(receipt_to_dict(build_shard_receipt(entry, index=0), extended=True), dict)

    def test_unknown_cost_source_is_not_claimed(self, repo):
        _, entry = self._run(repo, cost_source=None)
        assert "cost_provenance" not in entry

    def test_local_projection_has_counts_and_models_but_no_paths(self, repo):
        _, entry = self._run(repo)
        block = ev.agent_loop_block(entry)
        assert block["mode"] == "turns" and block["action_summary"]["reads"] == 1 and block["turns_total"] == 2
        assert block["attempts"][0]["turns"] == 2 and block["attempts"][0]["action_summary"]["verifications"] == 2
        assert block["model_calls"][0]["model"] == "acme/m" and block["model_calls"][0]["role"] == "executor"
        assert block["evidence"]["action_results"] == "openshard_observed"
        blob = json.dumps(block)
        assert "src/app.txt" not in blob and "nope" not in blob and "intent" not in blob
        assert ev.agent_loop_block({"osn_loop": {"status": "verified"}}) is None  # one-shot runs: no block

    def test_hosted_execution_loop_keeps_its_contract_shape(self, repo):
        """The Platform validates execution_loop strictly: the agent-loop keys must not leak into it."""
        _, entry = self._run(repo)
        block = ev.execution_loop_block(entry)
        assert set(block) == {"status", "stop_reason", "verification_state", "attempts", "evidence"}
        assert set(block["attempts"][0]) == {"n", "proposed_count", "applied_count", "blocked_count"}
        assert set(block["evidence"]) == {"actions", "policy_and_file_effects", "verification"}
        hosted = receipt_to_dict(build_shard_receipt(entry, index=0), extended=True)
        assert "agent_loop" not in hosted and "agent_loop" not in json.dumps(hosted.get("execution_loop"))

    def test_full_receipt_shows_the_action_trail(self, repo):
        _, entry = self._run(repo)
        text = render_full_shard_receipt(build_shard_receipt(entry, index=0))
        assert "OSN ACTIONS" in text
        assert "write_file" in text and "src/app.txt" in text and "run_verification" in text
        assert "allow" in text and "nope" not in text


def test_legacy_receipt_dict_shape_is_unchanged_for_write_providers(repo):
    from openshard.osn.loop import FileWriteAction

    rec = run_bounded_loop(repo, TASK, lambda c: [FileWriteAction("src/app.txt", "ok")], CHECK)
    d = rec.to_dict()
    assert d["mode"] == "writes" and "actions" not in d["attempts"][0] and "turns" not in d["attempts"][0]
    assert isinstance(rec, LoopReceipt)


