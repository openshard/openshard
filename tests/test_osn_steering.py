"""Mid-run steering: operator notes reach the model on its next turn; a stop ends the run before the next call.

Steering is a file next to the run's checkpoint (`openshard osn steer <run id>`),
read at turn boundaries only. The Receipt records that a note was shown
(attempt, turn, size, hash) and that a stop was requested; never the text and
never that the model followed it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.osn import checkpoint as ckpt
from openshard.osn import roles
from openshard.osn.actions import parse_turn
from openshard.osn.agent_loop import STOP_OPERATOR_STOP, TurnState, run_attempt_turns
from openshard.osn.loop import OperatorStopped, run_bounded_loop
from openshard.osn.model_provider import AttemptUsage, IterativeModelProvider, build_turn_prompt
from openshard.osn.steering import (
    KIND_NOTE,
    KIND_STOP,
    MAX_NOTE_CHARS,
    SteeringReader,
    steering_path,
    write_steering,
)
from openshard.policy.file_mutation import FileMutationGate
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
VERIFY = [PY, "-c", "import sys; sys.exit(0 if open('src/app.txt').read()=='ok' else 1)"]


def _turn(*actions, note="n"):
    return json.dumps({"actions": list(actions), "note": note})


def _a(kind, **kw):
    return {"kind": kind, "intent": kw.pop("intent", f"{kind} step"), **kw}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "app.txt").write_text("bad")
    return r


def _checkpointed(repo: Path, run_id: str = "osn-abc123") -> str:
    cp = ckpt.RunCheckpoint(run_id=run_id, task="t", verify_argv=VERIFY, args={}, repo={}, models=["m/a"])
    ckpt.write_checkpoint(repo, cp)
    return run_id


class ScriptedTurns:
    def __init__(self, replies, before_turn=None):
        self.replies = list(replies)
        self.states: list[TurnState] = []
        self.usage: list[AttemptUsage] = []
        self.before_turn = before_turn

    def turn(self, state: TurnState):
        if self.before_turn:
            self.before_turn(state)
        self.states.append(state)
        self.usage.append(AttemptUsage(state.attempt, "m/a", 1, 1, 0.0, requested_model="m/a", turn=state.turn,
                                       cost_source="provider_reported", duration_ms=1))
        return parse_turn(self.replies.pop(0))

    def pending_model_for(self, attempt):
        return "m/a"


class TestSteeringFile:
    def test_notes_and_stops_are_appended_for_a_known_run_only(self, repo):
        with pytest.raises(ValueError):
            write_steering(repo, "osn-missing", KIND_NOTE, "x")
        run_id = _checkpointed(repo)
        with pytest.raises(ValueError):
            write_steering(repo, run_id, KIND_NOTE, "   ")
        with pytest.raises(ValueError):
            write_steering(repo, run_id, "redirect", "x")
        write_steering(repo, run_id, KIND_NOTE, "use \x00the helper\tin util.py " + "z" * 5000)
        write_steering(repo, run_id, KIND_STOP)
        lines = [json.loads(ln) for ln in steering_path(repo, run_id).read_text(encoding="utf-8").splitlines()]
        assert [ln["kind"] for ln in lines] == ["note", "stop"]
        assert "\x00" not in lines[0]["text"] and len(lines[0]["text"]) == MAX_NOTE_CHARS

    def test_reader_hands_over_new_notes_once_and_records_only_counts(self, repo):
        run_id = _checkpointed(repo)
        reader = SteeringReader(repo, run_id)
        assert reader.poll(1, 1) == ([], False) and reader.to_record() is None
        write_steering(repo, run_id, KIND_NOTE, "prefer edit_file")
        assert reader.poll(1, 2) == (["prefer edit_file"], False)
        assert reader.poll(1, 3) == ([], False)  # not shown twice
        write_steering(repo, run_id, KIND_STOP)
        assert reader.poll(1, 4) == ([], True)
        rec = reader.to_record()
        assert rec["notes_shown"] == 1 and rec["stop_requested"] is True
        assert [(e["kind"], e["turn"]) for e in rec["events"]] == [("note", 2), ("stop", 4)]
        assert "prefer edit_file" not in json.dumps(rec) and rec["events"][0]["chars"] == len("prefer edit_file")

    def test_a_half_written_line_waits_and_junk_is_ignored(self, repo):
        run_id = _checkpointed(repo)
        path = steering_path(repo, run_id)
        path.write_text('not json\n{"kind": "note", "text": "ok"}\n{"kind": "note", "text": "partial"}', encoding="utf-8")
        reader = SteeringReader(repo, run_id)
        assert reader.poll(1, 1) == (["ok"], False)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n")
        assert reader.poll(1, 2) == (["partial"], False)


class TestTurnsSeeNotes:
    def test_a_note_written_during_a_turn_is_shown_on_the_next_turn(self, repo):
        run_id = _checkpointed(repo)
        reader = SteeringReader(repo, run_id)

        def during_first_turn(state):
            if state.turn == 1:
                write_steering(repo, run_id, KIND_NOTE, "keep the docstring")

        provider = ScriptedTurns([
            _turn(_a("read_file", path="src/app.txt")),
            _turn(_a("write_file", path="src/app.txt", content="ok")),
            _turn(_a("finish")),
        ], before_turn=during_first_turn)
        out = run_attempt_turns(
            repo_root=repo, sandbox=repo, task="t", attempt=1, provider=provider, gate=FileMutationGate(),
            verify=lambda paths: (None, ""), budget=None, previous_failure=None, blocked_seen=[], changed_so_far=[],
            max_turns=5, max_verifications=0, steer=reader.poll,
        )
        assert out.stop == "finished"
        assert provider.states[0].operator_notes == []
        assert provider.states[1].operator_notes == [(2, "keep the docstring")]
        assert provider.states[2].operator_notes == [(2, "keep the docstring")]  # kept for the whole attempt
        prompt = build_turn_prompt(provider.states[1], repo, [])
        assert "Operator notes" in prompt and "(turn 2) keep the docstring" in prompt
        assert "Operator notes" not in build_turn_prompt(provider.states[0], repo, [])

    def test_a_stop_ends_the_attempt_before_the_next_model_call(self, repo):
        run_id = _checkpointed(repo)
        reader = SteeringReader(repo, run_id)

        def during_first_turn(state):
            if state.turn == 1:
                write_steering(repo, run_id, KIND_STOP)

        provider = ScriptedTurns([_turn(_a("read_file", path="src/app.txt")), _turn(_a("finish"))],
                                 before_turn=during_first_turn)
        events = []
        out = run_attempt_turns(
            repo_root=repo, sandbox=repo, task="t", attempt=1, provider=provider, gate=FileMutationGate(),
            verify=lambda paths: (None, ""), budget=None, previous_failure=None, blocked_seen=[], changed_so_far=[],
            max_turns=5, max_verifications=0, steer=reader.poll, progress=lambda k, d: events.append((k, d)),
        )
        assert out.stop == STOP_OPERATOR_STOP and len(provider.states) == 1 and provider.replies  # no second call
        assert ("operator_stop", {"attempt": 1, "turn": 2, "role": "executor"}) in events


class FakeModel(BaseProvider):
    def __init__(self, replies, on_call=None):
        self.replies = list(replies)
        self.on_call = on_call
        self.prompts: list[str] = []

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        self.prompts.append(prompt)
        if self.on_call:
            self.on_call(len(self.prompts))
        return ChatResponse(self.replies.pop(0), model, UsageStats(10, 5, 15, 0.001, cost_source="provider_reported"))


class TestPlannerSteering:
    def test_a_note_written_during_planning_reaches_the_next_planner_turn(self, repo):
        run_id = _checkpointed(repo)
        reader = SteeringReader(repo, run_id)
        model = FakeModel([
            _turn(_a("read_file", path="src/app.txt")),
            json.dumps({
                "plan": {
                    "summary": "Update the app value",
                    "files": ["src/app.txt"],
                    "steps": ["write ok"],
                    "verification": ["the verifier passes"],
                    "simple": True,
                },
                "actions": [_a("finish")],
            }),
        ], on_call=lambda n: write_steering(repo, run_id, KIND_NOTE, "keep it minimal") if n == 1 else None)

        plan, role, _usage = roles.run_planner_turns(
            model, "m/a", task="t", repo_root=repo, sandbox=repo,
            repo_files=["src/app.txt"], steer=reader.poll,
        )

        assert plan is not None and role.status == roles.STATUS_RAN
        assert "keep it minimal" not in model.prompts[0]
        assert "(turn 2) keep it minimal" in model.prompts[1]
        record = reader.to_record()
        assert record["events"][0]["role"] == roles.ROLE_PLANNER
        assert record["events"][0]["turn"] == 2

    def test_a_stop_during_planning_interrupts_before_executor_work(self, repo):
        run_id = _checkpointed(repo)
        reader = SteeringReader(repo, run_id)
        planner_model = FakeModel(
            [_turn(_a("read_file", path="src/app.txt")), _turn(_a("finish"))],
            on_call=lambda n: write_steering(repo, run_id, KIND_STOP) if n == 1 else None,
        )

        def planner(sandbox, repo_files):
            plan, role, _usage = roles.run_planner_turns(
                planner_model, "plan/m", task="t", repo_root=repo, sandbox=sandbox,
                repo_files=repo_files, steer=reader.poll,
            )
            assert role.reason == STOP_OPERATOR_STOP
            return plan, role.to_record()

        executor_model = FakeModel([])
        executor = IterativeModelProvider(executor_model, ["m/a"], repo)
        with pytest.raises(OperatorStopped) as exc:
            run_bounded_loop(repo, "t", executor, VERIFY, max_attempts=1, planner=planner, steering=reader)

        assert (exc.value.attempt, exc.value.turn) == (0, 2)
        assert len(planner_model.prompts) == 1
        assert executor_model.prompts == []
        record = reader.to_record()
        assert record["stop_requested"] is True
        assert record["events"][-1]["role"] == roles.ROLE_PLANNER


class TestBoundedLoop:
    def test_an_operator_stop_raises_after_the_attempt_is_recorded(self, repo):
        run_id = _checkpointed(repo)
        reader = SteeringReader(repo, run_id)
        model = FakeModel([_turn(_a("read_file", path="src/app.txt")), _turn(_a("finish"))],
                          on_call=lambda n: write_steering(repo, run_id, KIND_STOP) if n == 1 else None)
        provider = IterativeModelProvider(model, ["m/a"], repo)
        with pytest.raises(OperatorStopped) as exc:
            run_bounded_loop(repo, "t", provider, VERIFY, max_attempts=2, steering=reader)
        assert (exc.value.attempt, exc.value.turn) == (1, 2)
        assert len(model.prompts) == 1  # the second turn never reached the model
        assert reader.to_record()["stop_requested"] is True

    def test_notes_reach_the_executor_and_the_record_says_shown_not_followed(self, repo):
        run_id = _checkpointed(repo)
        reader = SteeringReader(repo, run_id)
        model = FakeModel([
            _turn(_a("read_file", path="src/app.txt")),
            _turn(_a("write_file", path="src/app.txt", content="ok")),
            _turn(_a("finish")),
        ], on_call=lambda n: write_steering(repo, run_id, KIND_NOTE, "write ok") if n == 1 else None)
        provider = IterativeModelProvider(model, ["m/a"], repo)
        receipt = run_bounded_loop(repo, "t", provider, VERIFY, max_attempts=1, steering=reader)
        assert receipt.status == "verified"
        assert "write ok" not in model.prompts[0] and "(turn 2) write ok" in model.prompts[1]
        rec = reader.to_record()
        assert rec["notes_shown"] == 1 and rec["stop_requested"] is False and "followed" in rec["evidence"]
        assert "write ok" not in json.dumps(rec)


class TestCli:
    def test_steer_appends_for_a_known_run_and_refuses_unknown_or_finished_runs(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
        r = CliRunner().invoke(cli, ["osn", "steer", "osn-nope", "hello"])
        assert r.exit_code != 0 and "no checkpointed OSN run" in r.output
        run_id = _checkpointed(repo)
        r = CliRunner().invoke(cli, ["osn", "steer", run_id, "use", "the", "helper"])
        assert r.exit_code == 0, r.output
        assert "Note queued" in r.output and "next turn" in r.output
        r = CliRunner().invoke(cli, ["osn", "steer", run_id, "--stop"])
        assert r.exit_code == 0, r.output and "Stop requested" in r.output
        lines = [json.loads(ln) for ln in steering_path(repo, run_id).read_text(encoding="utf-8").splitlines()]
        assert [(ln["kind"], ln["text"]) for ln in lines] == [("note", "use the helper"), ("stop", "")]
        r = CliRunner().invoke(cli, ["osn", "steer", run_id])
        assert r.exit_code != 0 and "note or --stop" in r.output
        cp = ckpt.read_checkpoint(repo, run_id)
        cp.status = ckpt.STATUS_COMPLETED
        ckpt.write_checkpoint(repo, cp)
        r = CliRunner().invoke(cli, ["osn", "steer", run_id, "late"])
        assert r.exit_code != 0 and "not running" in r.output
