"""Capture Verification v2: the strongest truthful check outcome per integration.

* hook translators attach a per-command outcome only where the agent's own
  documentation defines one (Claude Code, Cursor, Grok Build); it is always
  ``agent_reported``;
* an interrupted / timed-out / denied / background command has no result:
  ``unknown``, never a failed or passed check;
* ``openshard verify`` re-runs approved checks itself: ``directly_observed`` /
  ``openshard_executed``, bound to a commit only on a clean tree, stored as a
  sidecar attestation that never rewrites a receipt;
* v0.4.7 records, blocks and queue lines keep reading exactly as before.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters import antigravity_hooks as ag
from openshard.adapters import cursor_hooks as cu
from openshard.adapters import grok_build_hooks as gb
from openshard.adapters import hermes_hooks as he
from openshard.adapters.claude_hooks import (
    ReducedHookPayload,
    extract_agent_payload,
    extract_hook_payload,
    handle_claude_hook,
    handle_hook,
)
from openshard.cli.main import cli
from openshard.history import verification as v
from openshard.history.shard_contract import (
    build_shard_receipt,
    checks_label,
    render_compact_shard_receipt,
)
from openshard.history.shard_hash import verify_shard_hash
from openshard.verification import post_session as ps
from tests.capture_fixtures import _make_repo

SID = "22222222-3333-4444-8555-666666666666"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "v2 repo")


def _runs(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _claude(repo: Path, event: str, **fields) -> None:
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, **fields}
    handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _claude_block(repo: Path, *calls: tuple[str, dict]) -> dict:
    _claude(repo, "UserPromptSubmit", prompt="fix and test")
    for event, fields in calls:
        _claude(repo, event, **fields)
    _claude(repo, "Stop")
    return _runs(repo)[-1]["verification"]


def _bash(command: str, **extra) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command, **extra.pop("tool_input", {})}, **extra}


# ---------------------------------------------------------------------------
# Known outcomes, reported by the agent's hooks
# ---------------------------------------------------------------------------


class TestKnownSuccessfulOutcome:
    def test_claude_foreground_post_tool_use_is_a_reported_pass(self, repo):
        block = _claude_block(repo, ("PostToolUse", _bash("python -m pytest -q",
                                                          tool_response={"stdout": "3 passed", "interrupted": False})))
        assert block["status"] == "passed"
        assert block["source"] == "agent_reported"  # Claude Code said so; OpenShard ran nothing
        assert block["observation_mode"] == "hook_tool_event"
        assert block["checks_passed"] == 1 and block["checks_attempted"] == 1
        assert block["incomplete_reasons"] == []
        assert "3 passed" not in json.dumps(_runs(repo))  # output never read

    def test_cursor_shell_exit_code_zero(self):
        p = cu.extract_cursor_payload({
            "hook_event_name": "postToolUse", "conversation_id": SID, "tool_name": "Shell",
            "tool_input": {"command": "npm test"}, "tool_output": json.dumps({"exitCode": 0, "stdout": "ok"}),
        })
        assert p is not None and p.command_exit_code == 0 and p.command_outcome is None

    def test_grok_build_exit_code_zero(self):
        p = gb.extract_grok_build_payload({
            "hook_event_name": "PostToolUse", "sessionId": SID, "toolName": "run_terminal_command",
            "toolInput": {"command": "pytest"}, "toolResult": {"type": "Bash", "exit_code": 0,
                                                               "output_for_prompt": "SECRET OUTPUT"},
        })
        assert p is not None and p.command_exit_code == 0


class TestKnownFailedOutcome:
    def test_claude_failure_exit_line_gives_the_exit_code(self, repo):
        block = _claude_block(repo, ("PostToolUseFailure", _bash("npm test",
                                                                 error="Exit code 1\nError: RAW TEXT")))
        assert block["status"] == "failed" and block["source"] == "agent_reported"
        assert block["checks"][0]["exit_code"] == 1 and block["exit_code"] == 1
        assert "RAW TEXT" not in json.dumps(_runs(repo))

    def test_claude_failure_without_exit_line_is_still_a_reported_failure(self, repo):
        block = _claude_block(repo, ("PostToolUseFailure", _bash("npm test", error="spawn failed")))
        assert block["status"] == "failed" and block["checks"][0]["exit_code"] is None

    def test_cursor_shell_exit_code_nonzero_is_failed(self, repo):
        docs = [
            {"hook_event_name": "beforeSubmitPrompt", "prompt": "test it"},
            {"hook_event_name": "postToolUse", "tool_name": "Shell", "tool_input": {"command": "npm test"},
             "tool_output": json.dumps({"exitCode": 2, "stdout": "FAIL"})},
            {"hook_event_name": "stop", "status": "completed"},
        ]
        for doc in docs:
            handle_hook({"conversation_id": SID, "workspace_roots": [str(repo)], "model": "m", **doc},
                        env={}, agent="cursor")
        block = _runs(repo)[-1]["verification"]
        assert block["status"] == "failed" and block["source"] == "agent_reported"
        assert block["checks"][0]["exit_code"] == 2

    def test_a_failure_is_never_hidden_by_a_pass(self, repo):
        block = _claude_block(
            repo,
            ("PostToolUse", _bash("pytest -q")),
            ("PostToolUseFailure", _bash("ruff check .", error="Exit code 1")),
        )
        assert block["status"] == "failed" and block["checks_passed"] == 1 and block["checks_failed"] == 1


# ---------------------------------------------------------------------------
# Invocation seen, outcome unavailable
# ---------------------------------------------------------------------------


class TestInvocationWithoutOutcome:
    @pytest.mark.parametrize("fields", [
        _bash("pytest", tool_input={"run_in_background": True}),
        _bash("pytest", tool_response={"stdout": "", "backgroundTaskId": "b1"}),
        _bash("pytest", tool_response={"stdout": "", "interrupted": True}),
    ])
    def test_claude_background_or_interrupted_call_has_no_outcome(self, repo, fields):
        block = _claude_block(repo, ("PostToolUse", fields))
        assert block["status"] == "unknown"
        assert block["source"] == "directly_observed"  # the invocation itself was observed
        assert "outcome_not_observed" in block["incomplete_reasons"]
        assert block["checks_passed"] == 0 and block["checks_failed"] == 0

    @pytest.mark.parametrize("fields", [
        _bash("pytest", is_interrupt=True, error="Exit code 130"),
        _bash("pytest", error="Command timed out after 2m 0s"),
    ])
    def test_claude_interrupt_or_timeout_is_not_a_failed_check(self, repo, fields):
        _claude(repo, "UserPromptSubmit", prompt="t")
        _claude(repo, "PostToolUseFailure", **fields)
        _claude(repo, "Stop")
        entry = _runs(repo)[-1]
        assert entry["verification"]["status"] == "unknown"
        tool = next(e for e in entry["events"] if e["event_type"] == "tool.invoked")
        assert tool["status"] == "failed" and tool["metadata"]["not_completed"] is True  # the tool call failed

    def test_cursor_without_exit_code_or_with_timeout_has_no_outcome(self):
        base = {"conversation_id": SID, "tool_name": "Shell", "tool_input": {"command": "npm test"}}
        ok = cu.extract_cursor_payload({**base, "hook_event_name": "postToolUse", "tool_output": "not json"})
        assert ok is not None and ok.command_exit_code is None and ok.command_outcome is None
        for extra in ({"failure_type": "timeout"}, {"failure_type": "permission_denied"}, {"is_interrupt": True}):
            p = cu.extract_cursor_payload({**base, "hook_event_name": "postToolUseFailure", **extra})
            assert p is not None and p.command_outcome == "not_completed", extra
        err = cu.extract_cursor_payload({**base, "hook_event_name": "postToolUseFailure", "failure_type": "error"})
        assert err is not None and err.command_outcome is None and err.event == "PostToolUseFailure"

    def test_codex_reports_no_outcome(self, repo):
        p = extract_agent_payload({
            "hook_event_name": "PostToolUse", "session_id": SID, "cwd": str(repo), "model": "gpt-5.5",
            "tool_name": "Bash", "tool_input": {"command": "pytest"}, "tool_response": "1 failed",
        }, agent="codex")
        assert p is not None and getattr(p, "command_outcome", None) is None
        assert getattr(p, "command_exit_code", None) is None

    def test_hermes_ok_is_not_exit_zero_and_cancelled_has_no_result(self, repo):
        def doc(status):
            return {"hook_event_name": "post_tool_call", "session_id": SID, "cwd": str(repo),
                    "tool_name": "terminal", "tool_input": {"command": "pytest"},
                    "extra": {"status": status, "result": json.dumps({"exit_code": 0})}}
        ok = he.extract_hermes_payload(doc("ok"))
        assert ok is not None and ok.command_outcome is None and ok.command_exit_code is None
        for status in ("cancelled", "blocked"):
            p = he.extract_hermes_payload(doc(status))
            assert p is not None and p.event == "PostToolUseFailure" and p.command_outcome == "not_completed"
        err = he.extract_hermes_payload(doc("error"))
        assert err is not None and err.event == "PostToolUseFailure" and err.command_outcome is None

    def test_grok_truncated_result_has_no_exit_code(self):
        p = gb.extract_grok_build_payload({
            "hook_event_name": "PostToolUse", "sessionId": SID, "toolName": "run_terminal_command",
            "toolInput": {"command": "pytest"}, "toolResultTruncated": True, "toolResult": "{...",
        })
        assert p is not None and p.command_exit_code is None


class TestNoChecks:
    def test_session_without_a_check_command_is_not_run(self, repo):
        block = _claude_block(repo, ("PostToolUse", _bash("ls -la")))
        assert block["status"] == "not_run" and block["checks_attempted"] == 0
        assert block["source"] == "directly_observed"


# ---------------------------------------------------------------------------
# Evidence-source correctness
# ---------------------------------------------------------------------------


class TestEvidenceSource:
    def test_hook_outcomes_are_agent_reported_invocations_directly_observed(self):
        assert v.hook_verification_source("unknown") == "directly_observed"
        assert v.hook_verification_source("not_run") == "directly_observed"
        for status in ("passed", "failed", "partial"):
            assert v.hook_verification_source(status) == "agent_reported"
        assert v.hook_verification_source("unknown", outcome_reported=True) == "agent_reported"

    def test_partial_when_some_outcomes_reported(self, repo):
        block = _claude_block(
            repo,
            ("PostToolUse", _bash("pytest -q")),
            ("PostToolUse", _bash("mypy .", tool_input={"run_in_background": True})),
        )
        assert block["status"] == "partial" and block["source"] == "agent_reported"
        assert "outcome_not_observed" in block["incomplete_reasons"]

    def test_receipt_labels_reported_outcome_but_synced_string_is_unchanged(self, repo):
        _claude_block(repo, ("PostToolUse", _bash("pytest -q")))
        receipt = build_shard_receipt(_runs(repo)[-1])
        assert receipt.checks_display == "1/1 passed"  # sync projection stays source-free
        assert checks_label(receipt) == "1/1 passed (agent-reported)"
        assert "1/1 passed (agent-reported)" in render_compact_shard_receipt(receipt)

    def test_conflicting_exit_code_and_outcome_is_unknown(self):
        from openshard.adapters.claude_hooks import _command_status

        reduced = ReducedHookPayload(event="PostToolUse", session_id=SID, command_outcome="passed",
                                     command_exit_code=1)
        assert _command_status(reduced, False) == "unknown"
        assert _command_status(ReducedHookPayload(event="PostToolUse", session_id=SID), False) == "unknown"


# ---------------------------------------------------------------------------
# Antigravity 2.0 payload compatibility (antigravity.google/docs/hooks)
# ---------------------------------------------------------------------------


def _ag2(repo: Path, **fields) -> dict:
    """A document shaped exactly like the Antigravity 2.0 reference: no event-name field."""
    return {
        "conversationId": SID,
        "workspacePaths": [str(repo)],
        "transcriptPath": str(repo / ".." / "brain" / SID / ".system_generated" / "logs" / "transcript.jsonl"),
        "artifactDirectoryPath": str(repo / ".." / "brain" / SID),
        "modelName": "gemini-3.6-flash-medium",
        **fields,
    }


def _ag2_run(repo: Path, error: str | None) -> dict:
    fields: dict = {"toolCall": {"name": "run_command", "args": {
        "CommandLine": "npm test", "Cwd": str(repo), "WaitMsBeforeAsync": 5000}}, "stepIdx": 3}
    if error is not None:
        fields["error"] = error
    return _ag2(repo, **fields)


class TestAntigravity2:
    def test_documented_failed_run_command_is_a_reported_failure(self, repo):
        for event, doc in (("PreInvocation", _ag2(repo, invocationNum=0, initialNumSteps=0)),
                           ("PostToolUse", _ag2_run(repo, "exit status 1")),
                           ("Stop", _ag2(repo, executionNum=0, terminationReason="model_stop", error="",
                                         fullyIdle=True))):
            handle_hook(doc, env={}, agent="antigravity", event_override=event)
        entry = _runs(repo)[-1]
        block = entry["verification"]
        assert block["status"] == "failed" and block["source"] == "agent_reported"
        assert block["checks"][0]["exit_code"] is None  # "exit status 1" is display text, never parsed
        assert entry["execution_model"] == "gemini-3.6-flash-medium"  # verbatim, no provider guessed
        assert "transcript.jsonl" not in json.dumps(entry) and "brain" not in json.dumps(entry)

    @pytest.mark.parametrize("error", ["", None])
    def test_empty_or_absent_error_is_never_a_command_pass(self, repo, error):
        # run_command backgrounds a command still running after WaitMsBeforeAsync, so the
        # tool call can complete before the command exits.
        p = ag.extract_antigravity_payload(_ag2_run(repo, error), event_override="PostToolUse")
        assert p is not None and p.event == "PostToolUse"
        assert p.command_outcome is None and p.command_exit_code is None and p.tool_success is None

    def test_file_tool_empty_error_is_still_the_success_signal(self, repo):
        doc = _ag2(repo, toolCall={"name": "write_to_file", "args": {"TargetFile": str(repo / "a.py")}},
                   stepIdx=1, error="")
        p = ag.extract_antigravity_payload(doc, event_override="PostToolUse")
        assert p is not None and p.tool_success is True

    def test_non_tool_post_tool_use_is_ignored(self, repo):
        assert ag.extract_antigravity_payload(_ag2(repo, stepIdx=0, error=""), event_override="PostToolUse") is None


# ---------------------------------------------------------------------------
# Backward compatibility with v0.4.7 records
# ---------------------------------------------------------------------------


V047_HOOK_RECORD = {
    "schema_version": "1.2", "timestamp": "2026-09-01T09:00:00Z", "task": "t", "run_id": "r-047",
    "executor": "claude_code_hooks", "verification_attempted": True, "verification_passed": None,
    "capture": {"source": "claude_code_hooks", "hook_events_dropped": 0},
    "verification": {
        "version": 1, "status": "unknown", "source": "directly_observed", "observation_mode": "hook_tool_event",
        "checks_attempted": 1, "checks_passed": 0, "checks_failed": 0, "checks_skipped": 0,
        "checks": [{"name": "Bash: pytest -q", "kind": "test", "status": "unknown", "exit_code": None}],
        "started_at": "2026-09-01T09:00:05Z", "completed_at": None, "duration_seconds": None,
        "exit_code": None, "artifact_sha": None,
        "reason": "Check command(s) observed through agent hooks; outcome not observed.",
        "complete": False, "incomplete_reasons": ["outcome_not_observed"], "derived": False,
    },
}


class TestBackwardCompatibility:
    def test_v047_block_reads_exactly_as_before(self):
        receipt = build_shard_receipt(json.loads(json.dumps(V047_HOOK_RECORD)))
        assert receipt.verification_status == "unknown"
        assert receipt.checks_display == "Attempted (unverified)"
        assert checks_label(receipt) == "Attempted (unverified)"
        assert receipt.verification["source"] == "directly_observed"

    def test_v047_failed_block_keeps_its_display(self):
        entry = json.loads(json.dumps(V047_HOOK_RECORD))
        entry["verification"].update(status="failed", source="agent_reported", checks_failed=1,
                                     incomplete_reasons=[])
        entry["verification"]["checks"][0]["status"] = "failed"
        receipt = build_shard_receipt(entry)
        assert receipt.checks_display == "0/1 passed" and receipt.status == "Failed"

    def test_pre_v2_queue_line_round_trips_without_outcome(self):
        line = {"event": "PostToolUse", "session_id": SID, "tool_name": "Bash", "command_action": "Bash: pytest",
                "command_kind": "test", "agent": "codex", "tool_kind": "command"}
        reduced = ReducedHookPayload.from_dict(line)
        assert reduced is not None and reduced.command_outcome is None and reduced.command_exit_code is None
        assert "command_outcome" not in reduced.to_dict() and "command_exit_code" not in reduced.to_dict()

    def test_v2_queue_line_round_trips_and_rejects_garbage(self):
        base = {"event": "PostToolUse", "session_id": SID}
        good = ReducedHookPayload.from_dict({**base, "command_outcome": "failed", "command_exit_code": 3})
        assert good is not None and (good.command_outcome, good.command_exit_code) == ("failed", 3)
        assert ReducedHookPayload.from_dict(good.to_dict()) == good
        bad = ReducedHookPayload.from_dict({**base, "command_outcome": "maybe", "command_exit_code": True})
        assert bad is not None and bad.command_outcome is None and bad.command_exit_code is None

    def test_v047_record_rebuilds_a_buffer_and_keeps_its_check(self, repo):
        path = repo / ".openshard" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _claude(repo, "UserPromptSubmit", prompt="t")
        _claude(repo, "PostToolUse", **_bash("pytest", tool_input={"run_in_background": True}))
        _claude(repo, "SessionEnd", reason="clear")
        _claude(repo, "UserPromptSubmit", prompt="again")
        _claude(repo, "PostToolUse", **_bash("pytest -q"))
        _claude(repo, "Stop")
        block = _runs(repo)[-1]["verification"]
        assert [c["status"] for c in block["checks"]] == ["unknown", "passed"]
        assert block["status"] == "partial"

    def test_extract_hook_payload_ignores_outcome_markers_for_non_shell_tools(self):
        p = extract_hook_payload({"hook_event_name": "PostToolUseFailure", "session_id": SID,
                                  "tool_name": "Edit", "error": "Exit code 1"})
        assert p is not None and p.command_outcome is None and p.command_exit_code is None


# ---------------------------------------------------------------------------
# Post-session verification (openshard verify)
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self, code):
        self.returncode = code


def _fake_runner(codes: dict[str, object]):
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        outcome = codes[argv[0]]
        if isinstance(outcome, BaseException):
            raise outcome
        return _Proc(outcome)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _check(name: str, safety: str = "safe") -> ps.PlannedCheck:
    return ps.PlannedCheck(name=name, argv=[name], kind="test", origin="contract", safety=safety, reason="")


CLEAN = ps.TreeState(head="a" * 40, dirty=False)


class TestPostSessionPlanning:
    def test_contract_first_and_safety_classes(self, repo):
        config = {"verification_commands": ["pytest -q", "ruff check .", "ruff check --fix .", "rm -rf build",
                                            "python scripts/check.py"]}
        planned = ps.plan_checks(repo, config, None)
        by = {tuple(c.argv): c for c in planned}
        assert by[("pytest", "-q")].safety == "safe"
        assert by[("ruff", "check", ".")].safety == "safe"  # read-only checker
        assert by[("ruff", "check", "--fix", ".")].safety == "needs_approval"  # would rewrite files
        assert by[("rm", "-rf", "build")].safety == "blocked"
        assert by[("python", "scripts/check.py")].safety == "needs_approval"
        assert all(c.origin == "contract" for c in planned)

    def test_detected_test_command_when_no_contract(self, repo):
        (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
        planned = ps.plan_checks(repo, {}, None)
        assert planned and planned[0].origin == "detected"

    def test_observed_commands_only_opt_in_and_never_truncated_or_redacted(self, repo):
        entry = {"verification": v.build_verification(
            source="agent_reported", observation_mode="hook_tool_event", checks=[
                {"name": "Bash: pytest -q tests/test_a.py", "kind": "test", "status": "passed"},
                {"name": "Bash command (redacted)", "kind": "test", "status": "unknown"},
                {"name": "Bash: " + "x" * 100, "kind": "lint", "status": "unknown"},
                {"name": "Bash: cd a && pytest", "kind": "test", "status": "unknown"},
            ])}
        assert ps.observed_check_commands(entry) == ["pytest -q tests/test_a.py", "cd a && pytest"]
        assert not [c for c in ps.plan_checks(repo, {"verification_commands": []}, entry) if c.origin == "observed"]
        observed = [c for c in ps.plan_checks(repo, {"verification_commands": ["mypy ."]}, entry,
                                              include_observed=True) if c.origin == "observed"]
        assert [c.safety for c in observed] == ["safe", "blocked"]  # shell chaining is never run


class TestPostSessionExecution:
    def test_exit_codes_map_to_outcomes_and_nothing_is_manufactured(self, repo):
        runner = _fake_runner({"ok": 0, "bad": 1, "slow": subprocess.TimeoutExpired("slow", 1),
                               "gone": FileNotFoundError()})
        results = ps.run_checks([_check("ok"), _check("bad"), _check("slow"), _check("gone"),
                                 _check("gated", "needs_approval"), _check("nope", "blocked")],
                                repo, runner=runner, stream=False)
        assert [(r.check.name, r.status, r.exit_code) for r in results] == [
            ("ok", "passed", 0), ("bad", "failed", 1), ("slow", "unknown", None), ("gone", "unknown", None),
            ("gated", "skipped", None), ("nope", "skipped", None),
        ]
        assert runner.calls == [["ok"], ["bad"], ["slow"], ["gone"]]  # gated/blocked never executed

    def test_approve_runs_needs_approval_but_never_blocked(self, repo):
        runner = _fake_runner({"gated": 0, "nope": 0})
        results = ps.run_checks([_check("gated", "needs_approval"), _check("nope", "blocked")], repo,
                                approve=True, runner=runner, stream=False)
        assert [r.status for r in results] == ["passed", "skipped"] and runner.calls == [["gated"]]

    def test_passed_attestation_is_directly_observed_and_bound_on_a_clean_tree(self, repo):
        results = ps.run_checks([_check("ok")], repo, runner=_fake_runner({"ok": 0}), stream=False)
        att = ps.build_attestation({"receipt_id": "rcp_1", "run_id": "r1"}, results, before=CLEAN, after=CLEAN,
                                   started_at="2026-09-24T10:00:00Z", completed_at="2026-09-24T10:00:05Z")
        block = att["verification"]
        assert block["status"] == "passed" and block["source"] == "directly_observed"
        assert block["observation_mode"] == "openshard_executed" and block["artifact_sha"] == "a" * 40
        assert block["exit_code"] == 0 and block["complete"] is True
        assert att["receipt_id"] == "rcp_1" and att["raw_output_stored"] is False

    def test_dirty_or_moved_tree_is_not_bound(self):
        passed = [ps.CheckRun(_check("ok"), "passed", exit_code=0)]
        for before, after in ((ps.TreeState("a" * 40, True), ps.TreeState("a" * 40, True)),
                              (CLEAN, ps.TreeState("b" * 40, False)),
                              (CLEAN, ps.TreeState("a" * 40, True)),
                              (ps.TreeState(None, None), ps.TreeState(None, None))):
            block = ps.build_attestation({}, passed, before=before, after=after,
                                         started_at="2026-09-24T10:00:00Z",
                                         completed_at="2026-09-24T10:00:01Z")["verification"]
            assert block["artifact_sha"] is None and "artifact_not_bound" in block["incomplete_reasons"]
            assert block["status"] == "passed"  # the outcome is real; only the binding is missing

    def test_untracked_files_a_check_writes_keep_the_binding(self):
        passed = [ps.CheckRun(_check("ok"), "passed", exit_code=0)]
        after = ps.TreeState("a" * 40, dirty=True, tracked_dirty=False)  # e.g. __pycache__ written by the run
        block = ps.build_attestation({}, passed, before=CLEAN, after=after, started_at="2026-09-24T10:00:00Z",
                                     completed_at="2026-09-24T10:00:01Z")["verification"]
        assert block["artifact_sha"] == "a" * 40
        # ...but an untracked file present *before* the run could be under test.
        untracked_before = ps.TreeState("a" * 40, dirty=True, tracked_dirty=False)
        block = ps.build_attestation({}, passed, before=untracked_before, after=after,
                                     started_at="2026-09-24T10:00:00Z",
                                     completed_at="2026-09-24T10:00:01Z")["verification"]
        assert block["artifact_sha"] is None

    def test_tree_state_reads_git(self, repo):
        state = ps.tree_state(repo)
        assert state.head and len(state.head) == 40 and state.dirty is False
        (repo / "scratch.txt").write_text("x", encoding="utf-8")
        state = ps.tree_state(repo)
        assert state.dirty is True and state.tracked_dirty is False

    def test_failed_and_timeout_attestations(self):
        runs = [ps.CheckRun(_check("a"), "failed", exit_code=2), ps.CheckRun(_check("b"), "unknown")]
        block = ps.build_attestation({}, runs, before=CLEAN, after=CLEAN, started_at="2026-09-24T10:00:00Z",
                                     completed_at="2026-09-24T10:00:01Z")["verification"]
        assert block["status"] == "failed" and "check_not_completed" in block["incomplete_reasons"]
        only_timeout = ps.build_attestation({}, [ps.CheckRun(_check("b"), "unknown")], before=CLEAN, after=CLEAN,
                                            started_at="2026-09-24T10:00:00Z",
                                            completed_at="2026-09-24T10:00:01Z")["verification"]
        assert only_timeout["status"] == "unknown"  # never a pass, never a fail

    def test_nothing_run_is_not_run(self):
        for runs in ([], [ps.CheckRun(_check("g", "needs_approval"), "skipped")]):
            block = ps.build_attestation({}, runs, before=CLEAN, after=CLEAN, started_at="2026-09-24T10:00:00Z",
                                         completed_at="2026-09-24T10:00:01Z")["verification"]
            assert block["status"] == "not_run" and block["checks_passed"] in (0, None)


class TestAttestationStore:
    def test_latest_attestation_joins_by_receipt_and_is_revalidated(self, repo):
        def att(status_code, rid="rcp_1"):
            results = [ps.CheckRun(_check("t"), "passed" if status_code == 0 else "failed", exit_code=status_code)]
            return ps.build_attestation({"receipt_id": rid}, results, before=CLEAN, after=CLEAN,
                                        started_at="2026-09-24T10:00:00Z", completed_at="2026-09-24T10:00:01Z")
        ps.record_attestation(repo, att(1))
        ps.record_attestation(repo, att(0))
        ps.record_attestation(repo, att(1, rid="rcp_other"))
        with (repo / ".openshard" / ps.ATTESTATIONS_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write("not json\n")
        found = ps.latest_for_entry({"receipt_id": "rcp_1"}, ps.load_attestations(repo / ".openshard"))
        assert found is not None and found["verification"]["status"] == "passed"
        assert ps.display_line(found) == f"1/1 passed @ {'a' * 12} (OpenShard re-run)"
        assert ps.latest_for_entry({"receipt_id": "rcp_none"}, ps.load_attestations(repo / ".openshard")) is None

    def test_a_forged_non_executed_block_is_never_surfaced_as_a_rerun(self):
        forged = {"kind": "post_session_verification", "receipt_id": "r", "verification": v.build_verification(
            source="agent_reported", observation_mode="agent_claim", status="passed", checks_attempted=1,
            checks_passed=1)}
        summary = ps.latest_for_entry({"receipt_id": "r"}, [forged])
        assert summary is not None and summary["verification"]["status"] == "unknown"


class TestVerifyCommand:
    @pytest.fixture
    def captured(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        _claude(repo, "UserPromptSubmit", prompt="write tests")
        _claude(repo, "PostToolUse", **_bash("pytest -q", tool_input={"run_in_background": True}))
        _claude(repo, "Stop")
        (repo / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        (repo / "test_bad.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "tests"],
            cwd=repo, check=True, capture_output=True,
        )
        return repo

    def _config(self, repo: Path, *commands: list[str]) -> None:
        import yaml

        (repo / ".openshard" / "config.yml").write_text(
            yaml.safe_dump({"verification_commands": [list(c) for c in commands]}), encoding="utf-8")

    def test_verify_records_a_passing_rerun_without_touching_the_receipt(self, captured):
        repo = captured
        before = (repo / ".openshard" / "runs.jsonl").read_bytes()
        self._config(repo, [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_ok.py"])
        result = CliRunner().invoke(cli, ["verify", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        block = payload["verification"]
        assert block["status"] == "passed" and block["source"] == "directly_observed"
        assert block["observation_mode"] == "openshard_executed"
        assert block["artifact_sha"] and len(block["artifact_sha"]) == 40  # clean committed tree
        assert (repo / ".openshard" / "runs.jsonl").read_bytes() == before  # receipt never rewritten
        entry = _runs(repo)[-1]
        assert verify_shard_hash(entry)["status"] in ("valid", "missing")
        # The receipt's own session evidence is unchanged: still invocation-only.
        assert entry["verification"]["status"] == "unknown"
        last = json.loads(CliRunner().invoke(cli, ["last", "--json"]).output)
        assert last["post_session_verification"]["verification"]["status"] == "passed"

    def test_verify_records_a_failing_rerun_and_still_exits_zero(self, captured):
        self._config(captured, [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_bad.py"])
        result = CliRunner().invoke(cli, ["verify", "--json"])
        assert result.exit_code == 0, result.output  # evidence, not policy
        block = json.loads(result.output)["verification"]
        assert block["status"] == "failed" and block["checks"][0]["exit_code"] == 1

    def test_dry_run_runs_nothing(self, captured):
        self._config(captured, [sys.executable, "-m", "pytest", "-q", "test_ok.py"])
        result = CliRunner().invoke(cli, ["verify", "--dry-run", "--json"])
        assert result.exit_code == 0 and json.loads(result.output)["status"] == "dry_run"
        assert not (captured / ".openshard" / ps.ATTESTATIONS_FILENAME).exists()

    def test_unknown_receipt_is_an_error(self, captured):
        result = CliRunner().invoke(cli, ["verify", "--receipt", "rcp_missing", "--json"])
        assert result.exit_code == 1 and json.loads(result.output)["status"] == "not_found"

    def test_human_output_shows_the_rerun_on_last(self, captured):
        self._config(captured, [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_ok.py"])
        assert CliRunner().invoke(cli, ["verify"]).exit_code == 0
        out = CliRunner().invoke(cli, ["last"]).output
        assert "Re-verified: 1/1 passed @" in out and "(OpenShard re-run)" in out
