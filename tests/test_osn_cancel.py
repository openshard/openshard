"""Ctrl-C during parallel workers or explorers stops them at their next turn instead of waiting for all of them.

A worker's model call cannot be interrupted, but no worker starts another
turn once the run is cancelled, queued workers never start, and the
interrupt reaches the caller at once so the checkpoint is marked
interrupted. The run's progress stream says which worker was cancelled.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from openshard.osn.agent_loop import STOP_CANCELLED, run_attempt_turns
from openshard.osn.decompose import Subtask
from openshard.osn.explore import run_explorers
from openshard.osn.model_provider import IterativeModelProvider
from openshard.osn.workers import WorkerSpec, run_workers
from openshard.policy.file_mutation import FileMutationGate
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats


def _turn(*actions, note="n"):
    return json.dumps({"actions": list(actions), "note": note})


def _write(path, content):
    return {"kind": "write_file", "path": path, "content": content, "intent": "w"}


def _finish():
    return {"kind": "finish", "intent": "done"}


class ScriptedByModel(BaseProvider):
    """Replies per model id; a reply may be an exception to raise, or a callable run before replying."""

    def __init__(self, replies: dict[str, list]):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        with self._lock:
            self.calls.append(model)
            reply = self.replies[model].pop(0)
        if callable(reply):
            reply = reply()
        if isinstance(reply, BaseException):
            raise reply
        return ChatResponse(reply, model, UsageStats(10, 5, 15, 0.001, cost_source="provider_reported"))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.txt").write_text("a")
    (r / "src" / "b.txt").write_text("b")
    return r


def _sandbox(tmp_path, repo) -> Path:
    import shutil

    sb = tmp_path / "sb"
    shutil.copytree(repo, sb)
    return sb


def _ok_verify(sandbox, paths):
    from openshard.osn.loop import VerificationResult

    return VerificationResult(command=["true"], exit_code=0, passed=True, output_sha256="", output_bytes=0), ""


def _spec(i: int, model: str) -> WorkerSpec:
    st = Subtask(id=f"s{i}", objective=f"write {i}", allowed_write_paths=[f"src/{'ab'[i - 1]}.txt"], required=True)
    return WorkerSpec(f"worker-{i}", st, model, provider_name="fake")


def test_a_cancelled_attempt_stops_before_its_next_turn_without_a_model_call(tmp_path, repo):
    fake = ScriptedByModel({"m": [_turn(_write("src/a.txt", "A"))]})
    provider = IterativeModelProvider(fake, ["m"], repo)
    cancel = threading.Event()
    cancel.set()
    events: list = []
    out = run_attempt_turns(
        repo_root=repo, sandbox=_sandbox(tmp_path, repo), task="t", attempt=1, provider=provider,
        gate=FileMutationGate(), verify=_ok_verify, budget=None, previous_failure=None, blocked_seen=[],
        changed_so_far=[], max_turns=3, max_verifications=1, progress=lambda e, d: events.append((e, d)),
        cancel=cancel,
    )
    assert out.stop == STOP_CANCELLED and out.turns == 0 and fake.calls == []
    assert events == [("cancelled", {"attempt": 1, "turn": 1, "role": "executor"})]


def test_a_cancel_set_by_one_worker_stops_the_next_worker_at_its_first_turn(tmp_path, repo):
    cancel = threading.Event()
    fake = ScriptedByModel({
        "fast/m": [lambda: (cancel.set(), _turn(_write("src/a.txt", "A"), _finish()))[1]],
        "deep/m": [_turn(_write("src/b.txt", "B"), _finish())],
    })
    events: list = []
    results, usage = run_workers(
        [_spec(1, "fast/m"), _spec(2, "deep/m")], provider=fake, task="t", plan=None, repo_root=repo,
        base_sandbox=_sandbox(tmp_path, repo), verify=_ok_verify, max_workers=1,
        progress=lambda e, d: events.append((e, d)), cancel=cancel,
    )
    assert [r.status for r in results] == ["changed", "failed"]
    assert results[1].reason == "cancelled" and fake.calls == ["fast/m"]
    assert any(e == "cancelled" and d.get("worker_id") == "worker-2" for e, d in events)


def test_a_keyboard_interrupt_in_a_worker_reaches_the_caller_and_queued_workers_never_start(tmp_path, repo):
    fake = ScriptedByModel({
        "fast/m": [KeyboardInterrupt()],
        "deep/m": [_turn(_write("src/b.txt", "B"), _finish())],
    })
    cancel = threading.Event()
    with pytest.raises(KeyboardInterrupt):
        run_workers(
            [_spec(1, "fast/m"), _spec(2, "deep/m")], provider=fake, task="t", plan=None, repo_root=repo,
            base_sandbox=_sandbox(tmp_path, repo), verify=_ok_verify, max_workers=1, cancel=cancel,
        )
    assert cancel.is_set() and fake.calls == ["fast/m"]


def test_explorers_honour_the_cancel_flag_too(tmp_path, repo):
    cancel = threading.Event()
    fake = ScriptedByModel({
        "m": [lambda: (cancel.set(), json.dumps({"findings": ["x"], "sources": ["src/a.txt"],
                                                 "actions": [{"kind": "finish"}]}))[1]],
    })
    questions = [{"question": "q1", "why": "w"}, {"question": "q2", "why": "w"}]
    results, usage = run_explorers(
        questions, provider=fake, model="m", task="t", repo_root=repo, sandbox=_sandbox(tmp_path, repo),
        repo_files=["src/a.txt", "src/b.txt"], max_workers=1, cancel=cancel,
    )
    assert [r.status for r in results] == ["answered", "failed"]
    assert results[1].reason == "cancelled" and fake.calls == ["m"]
