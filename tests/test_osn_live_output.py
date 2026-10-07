"""Live OSN output: what a user watching `openshard osn run` sees, and the events behind it.

The engine emits structured progress events; the CLI renderer turns them
into lines. These tests pin the contract between the two: the plan the
executor was given is shown, a failed verification says why, concurrent
workers' lines are labelled, and the run ends with the Receipt id.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from openshard.cli.osn_cmd import _OsnProgressRenderer, _plan_lines, _verification_result_lines
from openshard.osn.agent_loop import verification_progress_fields


@dataclass
class _Result:
    passed: bool = False
    exit_code: int | None = 1
    ran: bool = True
    timed_out: bool = False
    tainted: bool = False
    setup_failure: str | None = None
    failed_tests: list[str] = field(default_factory=list)


class TestVerificationProgressFields:
    def test_a_failure_carries_the_last_lines_of_output_and_the_failing_tests(self):
        out = "\n".join(f"line {i}" for i in range(40)) + "\nFAILED tests/test_x.py::test_a - AssertionError\n1 failed\n"
        fields = verification_progress_fields(_Result(failed_tests=["tests/test_x.py::test_a"]), out)
        assert fields["status"] == "failed" and fields["exit_code"] == 1 and fields["ran"] is True
        assert fields["failed_tests"] == ["tests/test_x.py::test_a"]
        tail = fields["output_tail"].splitlines()
        assert len(tail) == 12 and tail[-1] == "1 failed" and tail[0] == "line 30"

    def test_a_pass_carries_no_output(self):
        fields = verification_progress_fields(_Result(passed=True, exit_code=0), "90 passed\n")
        assert fields["status"] == "passed" and "output_tail" not in fields

    def test_timeout_setup_failure_and_taint_are_distinct_states(self):
        assert verification_progress_fields(_Result(timed_out=True, exit_code=None), "")["status"] == "unknown"
        setup = verification_progress_fields(_Result(ran=False, setup_failure="missing_module"), "No module named pytest")
        assert setup["status"] == "not_run" and setup["setup_failure"] == "missing_module"
        assert setup["output_tail"] == "No module named pytest"
        assert verification_progress_fields(_Result(tainted=True), "x")["tainted"] is True

    def test_credential_looking_lines_and_control_characters_never_reach_the_terminal(self):
        out = "ok line\nAPI_KEY=sk-live-abcdefghijklmnopqrstuvwxyz0123456789\nbad\x1b[31m line\n"
        fields = verification_progress_fields(_Result(), out)
        tail = fields["output_tail"]
        assert "sk-live" not in tail and "\x1b" not in tail
        assert tail.splitlines() == ["ok line", "bad[31m line"]


class TestRenderedLines:
    def test_plan_lines_show_steps_files_and_subtasks_bounded(self):
        data = {
            "plan_summary": "Add the helper and its tests",
            "plan_steps": [f"step {i}" for i in range(1, 11)],
            "plan_files": [f"f{i}.py" for i in range(8)],
            "plan_subtasks": ["api", "tests"],
        }
        lines = _plan_lines(data)
        assert lines[0] == "    Add the helper and its tests"
        assert lines[1] == "    1. step 1" and lines[8] == "    8. step 8"
        assert lines[9] == "    … 2 more step(s)"
        assert lines[10].startswith("    Files likely to change: f0.py, f1.py") and lines[10].endswith("… +2")
        assert lines[11] == "    Independent subtasks proposed: api, tests"
        assert _plan_lines({}) == []

    def test_verification_lines_explain_each_non_pass_state(self):
        assert _verification_result_lines({"status": "passed"}) == ["  ✓ PASSED"]
        failed = _verification_result_lines({
            "status": "failed", "exit_code": 1, "failed_tests": ["t::a", "t::b"],
            "output_tail": "E  assert 1 == 2\n1 failed",
        })
        assert failed == [
            "  ✗ FAILED · exit 1",
            "    Failing: t::a, t::b",
            "    Output (last lines):",
            "      E  assert 1 == 2",
            "      1 failed",
        ]
        assert _verification_result_lines({"status": "unknown"})[0].startswith("  ? UNKNOWN")
        assert "could not run (missing_module)" in _verification_result_lines(
            {"status": "not_run", "setup_failure": "missing_module"})[0]
        assert _verification_result_lines({"status": "passed", "tainted": True}) == ["  ✓ PASSED"]
        assert _verification_result_lines({"status": "failed", "tainted": True})[0].startswith("  ✗ INVALID")


class TestRenderer:
    @pytest.fixture
    def rendered(self, capsys):
        renderer = _OsnProgressRenderer()

        def run(events):
            for event, data in events:
                renderer(event, data)
            renderer.close()
            return capsys.readouterr().out

        return run

    def test_plan_steps_are_shown_when_the_planner_finishes(self, rendered):
        out = rendered([
            ("role_start", {"role": "planner"}),
            ("role_end", {"role": "planner", "status": "ran", "model": "openai/gpt-5.6-sol", "has_plan": True,
                          "plan_summary": "Mirror deslug.py", "plan_steps": ["read deslug.py", "write reverse.py"],
                          "plan_files": ["reverse.py"], "plan_subtasks": []}),
        ])
        assert "Planning (read-only)" in out
        assert "✓ Plan ready" in out and "1. read deslug.py" in out and "2. write reverse.py" in out
        assert "Files likely to change: reverse.py" in out

    def test_a_planner_without_a_plan_says_so(self, rendered):
        out = rendered([("role_end", {"role": "planner", "status": "failed", "reason": "no_plan_returned",
                                      "has_plan": False})])
        assert "? Planner failed · no_plan_returned · continuing without a plan" in out

    def test_failed_verification_shows_why(self, rendered):
        out = rendered([
            ("verification_start", {"attempt": 1}),
            ("verification_result", {"attempt": 1, "status": "failed", "exit_code": 1,
                                     "failed_tests": ["tests/test_reverse.py::test_empty"],
                                     "output_tail": "E   AssertionError\n1 failed, 89 passed"}),
        ])
        assert "✗ FAILED · exit 1" in out
        assert "Failing: tests/test_reverse.py::test_empty" in out
        assert "      1 failed, 89 passed" in out

    def test_worker_lines_carry_the_worker_id(self, rendered):
        out = rendered([
            ("turn_start", {"attempt": 1, "turn": 1, "max_turns": 6, "model": "vendor/fast-1", "role": "worker",
                            "worker_id": "worker-1", "subtask_id": "api"}),
            ("action", {"kind": "write_file", "target": "src/a.txt", "status": "ok", "summary": "create",
                        "worker_id": "worker-1"}),
            ("turn_start", {"attempt": 1, "turn": 1, "max_turns": 6, "model": "vendor/deep-1", "role": "worker",
                            "worker_id": "worker-2", "subtask_id": "tests"}),
            ("turn_start", {"attempt": 1, "turn": 1, "max_turns": 12, "model": "vendor/exec-1", "role": "executor"}),
        ])
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert lines[0].startswith("  [worker-1] Worker turn 1/6 · ")
        assert lines[1] == "    [worker-1] → write_file src/a.txt · create"
        assert lines[2].startswith("  [worker-2] Worker turn 1/6 · ")
        assert lines[3].startswith("  Turn 1/12 · ") and "[worker" not in lines[3]

    def test_events_the_user_must_not_miss_are_rendered(self, rendered):
        out = rendered([
            ("checkpoint_failed", {"phase": "planned"}),
            ("malformed_attempt", {"attempt": 1, "next_attempt": 2}),
        ])
        assert "! Checkpoint not written (planned) · this run cannot be resumed if interrupted" in out
        assert "✗ Attempt 1 wrote nothing usable · moving to attempt 2 with the next model" in out

    def test_renderer_is_safe_to_call_from_threads(self):
        import threading

        renderer = _OsnProgressRenderer()
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                for t in range(20):
                    renderer("turn_start", {"turn": t, "max_turns": 20, "model": "m", "role": "worker",
                                            "worker_id": f"worker-{i}"})
            except BaseException as exc:  # pragma: no cover - the assertion below reports it
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        renderer.close()
        assert not errors



class TestCliRun:
    """`openshard osn run` in text mode: what the terminal shows end to end with a fake provider."""

    @staticmethod
    def _fake(replies):
        from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

        class Fake(BaseProvider):
            def __init__(self):
                self.replies = list(replies)

            def list_models(self):
                return []

            def get_model_info(self, model_id):
                return None

            def execute(self, model, prompt, system=None, max_tokens=None):
                return ChatResponse(self.replies.pop(0), model, UsageStats(10, 5, 15, 0.001))

        return Fake()

    @staticmethod
    def _repo(tmp_path):
        import subprocess

        r = tmp_path / "proj"
        r.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=r, check=True)
        (r / "out.txt").write_text("bad")
        return r

    def test_text_mode_ends_with_the_receipt_id_and_a_failed_check_shows_its_output(self, tmp_path, monkeypatch):
        import json
        import sys

        from click.testing import CliRunner

        from openshard.cli.main import cli

        repo = self._repo(tmp_path)
        monkeypatch.chdir(repo)
        writes = json.dumps({"actions": [{"kind": "write_file", "path": "out.txt", "content": "nope"},
                                         {"kind": "finish"}]})
        fake = self._fake([writes])
        monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fake))
        monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
        check = (f'"{sys.executable}" -c "import sys; t=open(\'out.txt\').read(); '
                 "print(\'got\', t); sys.exit(0 if t==\'ok\' else 3)\"")
        r = CliRunner().invoke(cli, ["osn", "run", "make out ok", "--model", "fake/m", "--roles", "executor",
                                     "--max-attempts", "1", "--verify-cmd", check])
        assert r.exit_code == 0, r.output
        out = r.output
        assert "✗ FAILED · exit 3" in out and "Output (last lines):" in out and "got nope" in out
        runs = [json.loads(x) for x in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()]
        receipt_id = runs[-1]["receipt_id"]
        assert f"Receipt  {receipt_id} · `openshard last` shows it · run osn-" in out
