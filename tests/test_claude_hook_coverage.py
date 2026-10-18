"""Capture gaps from silent hooks: transcript work no hook delivered.

Driven through the real hook entry point (``handle_claude_hook``) with a
transcript where Claude Code writes it, so the gap reaches the stored
record, ``openshard last`` and the history JSON exactly as a user sees it.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openshard.adapters import claude_hook_coverage as coverage
from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.adapters.claude_session_fidelity import sweep_stale_buffers
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt
from openshard.history.views import receipt_to_dict
from openshard.remote.spool import wire_event

SID = "0f1e2d3c-4b5a-4697-8877-665544332211"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                       cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return root


def _transcript_path() -> Path:
    directory = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "c--work-repo"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{SID}.jsonl"


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Transcript:
    """Appends Claude Code-shaped records: tool_use from the assistant, tool_result from the user."""

    def __init__(self) -> None:
        self.path = _transcript_path()
        self.n = 0

    def _append(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def tool(self, name: str, at: datetime, *, is_error: bool = False, result: bool = True) -> None:
        self.n += 1
        tid = f"toolu_{self.n:04d}"
        self._append({"type": "assistant", "sessionId": SID, "timestamp": _stamp(at), "message": {
            "id": f"msg_{self.n}", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": {"command": "SECRET INPUT"}}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}}})
        if result:
            self._append({"type": "user", "sessionId": SID, "timestamp": _stamp(at), "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": is_error,
                                             "content": "RAW TOOL OUTPUT"}]}})

    def text(self, at: datetime) -> None:
        self.n += 1
        self._append({"type": "assistant", "sessionId": SID, "timestamp": _stamp(at), "message": {
            "id": f"msg_{self.n}", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "RAW ASSISTANT TEXT"}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}}})


def _run(repo: Path, event: str, **fields):
    payload = {"session_id": SID, "transcript_path": str(_transcript_path()), "cwd": str(repo),
               "permission_mode": "default", "hook_event_name": event, **fields}
    return handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _tool_hook(repo: Path, name: str = "Bash") -> None:
    _run(repo, "PostToolUse", tool_name=name, tool_input={"command": "echo hi"}, tool_response={"stdout": "hi"})


def _entry(repo: Path) -> dict:
    lines = [json.loads(ln) for ln in (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    assert len(lines) == 1
    return lines[0]


def _reasons(entry: dict) -> dict[str, dict]:
    return {r["kind"]: r for r in entry["capture"]["completeness"]["reasons"]}


def _now() -> datetime:
    return datetime.now(UTC)


def _start(repo: Path) -> Transcript:
    transcript = Transcript()
    transcript.text(_now() - timedelta(hours=2))  # before the session was observed: ignored
    _run(repo, "SessionStart", source="startup")
    _run(repo, "UserPromptSubmit", prompt="Tidy the module")
    return transcript


def test_normal_session_has_no_coverage_gap(repo):
    transcript = _start(repo)
    for name in ("Write", "Edit", "Bash"):
        transcript.tool(name, _now())
        _tool_hook(repo, name)
    transcript.tool("Read", _now())  # not matched by the hook: never expected
    transcript.tool("Bash", _now(), is_error=True)  # failed or denied: may reach no PostToolUse
    transcript.tool("Bash", _now(), result=False)  # still running / interrupted: no result yet
    _run(repo, "Stop", stop_hook_active=False)
    _run(repo, "SessionEnd", reason="prompt_input_exit")
    entry = _entry(repo)
    assert entry["capture"]["completeness"] == {"status": "complete", "reasons": []}


def test_tool_calls_that_reached_no_hook_are_a_gap(repo):
    transcript = _start(repo)
    for name in ("Write", "Edit", "Bash", "Bash"):
        transcript.tool(name, _now())
    _tool_hook(repo, "Write")  # only one of four reached the hook
    _run(repo, "Stop", stop_hook_active=False)
    entry = _entry(repo)
    reasons = _reasons(entry)
    assert reasons["tool_calls_without_hook"]["count"] == 3
    assert reasons["tool_calls_without_hook"]["detail"] == "3 tool calls in the transcript reached no hook"
    assert entry["capture"]["completeness"]["status"] == "incomplete"
    # A second settled fold recomputes, it never adds the same gap twice.
    _run(repo, "SessionEnd", reason="prompt_input_exit")
    entry = _entry(repo)
    assert _reasons(entry)["tool_calls_without_hook"]["count"] == 3
    receipt = build_shard_receipt(entry)
    rendered = render_compact_shard_receipt(receipt)
    gaps = [ln for ln in rendered.splitlines() if ln.strip().startswith("Gaps")]
    assert gaps and "3 tool calls in the transcript reached no hook" in gaps[0]
    as_json = receipt_to_dict(receipt)
    assert {"kind": "tool_calls_without_hook", "count": 3,
            "detail": "3 tool calls in the transcript reached no hook"} in as_json["capture_completeness"]["reasons"]
    # Never a claim of control or enforcement.
    assert "block" not in json.dumps(as_json["capture_completeness"]).lower()


def test_gap_reaches_openshard_last_and_history_json(repo, monkeypatch):
    from click.testing import CliRunner

    from openshard.cli.main import cli

    transcript = _start(repo)
    transcript.tool("Bash", _now())
    _run(repo, "Stop", stop_hook_active=False)
    monkeypatch.chdir(repo)
    runner = CliRunner()
    last = runner.invoke(cli, ["last"])
    assert last.exit_code == 0, last.output
    gaps = [ln for ln in last.output.splitlines() if ln.strip().startswith("Gaps")]
    assert gaps and "1 tool call in the transcript reached no hook" in gaps[0]
    history = runner.invoke(cli, ["history", "--json"])
    assert history.exit_code == 0, history.output
    assert "1 tool call in the transcript reached no hook" in history.output


def test_mid_turn_fold_judges_nothing(repo):
    transcript = _start(repo)
    transcript.tool("Bash", _now())
    transcript.tool("Bash", _now())
    _tool_hook(repo, "Bash")
    from openshard.adapters import claude_hooks as ch

    buf = ch._read_buffer(ch.sessions_dir(repo) / f"{SID}.json")
    assert buf is not None
    assert coverage.evaluate(buf, coverage.refresh(buf)) is None  # a turn in flight is not settled


def test_hooks_that_stop_while_the_session_continues(repo):
    transcript = _start(repo)
    transcript.tool("Bash", _now())
    _tool_hook(repo, "Bash")
    later = _now() + timedelta(minutes=29)
    transcript.tool("Edit", later - timedelta(minutes=1))
    transcript.tool("Bash", later - timedelta(minutes=1))
    transcript.text(later)
    swept = sweep_stale_buffers(repo, max_age_seconds=0)
    assert swept == [SID]
    entry = _entry(repo)
    reasons = _reasons(entry)
    detail = reasons["hook_events_stopped"]["detail"]
    last_hook = datetime.strptime(entry["capture"]["last_activity_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert detail.startswith(f"no hook arrived after {last_hook:%H:%M} UTC" if last_hook.date() == later.date()
                             else "no hook arrived after ")
    assert "the session continued until" in detail and detail.endswith("(2 more tool calls)")
    assert "tool_calls_without_hook" not in reasons  # the late calls are the stopped window, not counted twice
    assert "session_idle_sealed" in reasons


def test_idle_seal_without_later_transcript_activity_adds_no_coverage_gap(repo):
    transcript = _start(repo)
    transcript.tool("Bash", _now())
    _tool_hook(repo, "Bash")
    sweep_stale_buffers(repo, max_age_seconds=0)
    reasons = _reasons(_entry(repo))
    assert "hook_events_stopped" not in reasons and "tool_calls_without_hook" not in reasons


def test_no_readable_transcript_means_no_judgement(repo):
    _run(repo, "SessionStart", source="startup")
    _run(repo, "UserPromptSubmit", prompt="Tidy the module")
    _run(repo, "Stop", stop_hook_active=False)
    entry = _entry(repo)
    assert entry["capture"]["completeness"] == {"status": "complete", "reasons": []}


def test_buffer_keeps_no_transcript_content(repo, receipt_scope):
    transcript = _start(repo)
    transcript.tool("Bash", _now())
    _tool_hook(repo, "Bash")
    from openshard.adapters import claude_hooks as ch

    path = ch.sessions_dir(repo) / f"{SID}.json"
    raws = [path.read_text(encoding="utf-8")]
    _run(repo, "Stop", stop_hook_active=False)
    if receipt_scope == "session":  # a sealed turn drops its buffer at the Stop
        raws.append(path.read_text(encoding="utf-8"))
    else:
        assert not path.exists()
    for raw in raws:
        assert "SECRET INPUT" not in raw and "RAW TOOL OUTPUT" not in raw and "RAW ASSISTANT TEXT" not in raw
    assert "hook_coverage" not in json.dumps(_entry(repo))


# ---------------------------------------------------------------------------
# Hook configuration hash on the session
# ---------------------------------------------------------------------------


def test_session_records_the_hook_config_hash(repo):
    from openshard.adapters.claude_hooks_install import build_hook_config

    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text(
        json.dumps({"hooks": build_hook_config(8765, None)}), encoding="utf-8")
    _start(repo)
    _run(repo, "Stop", stop_hook_active=False)
    entry = _entry(repo)
    digest = entry["capture"]["hook_config_hash"]
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 24
    started = [e for e in entry["events"] if e["event_type"] == "session.started"]
    assert started[0]["metadata"]["hook_config_hash"] == digest
    # The remote-capture wire form carries it (the Platform's metadata accepts any scalar key).
    wire = wire_event({**started[0], "seq": 1})
    assert wire is not None and wire["metadata"]["hook_config_hash"] == digest
    # The Receipt projection the Platform syncs does not.
    assert "hook_config_hash" not in json.dumps(receipt_to_dict(build_shard_receipt(entry)))


def test_sync_payload_carries_gaps_only_through_the_existing_completeness_reasons(repo):
    from openshard.sync.envelope import receipt_payload

    transcript = _start(repo)
    transcript.tool("Bash", _now())
    transcript.tool("Bash", _now())
    _run(repo, "Stop", stop_hook_active=False)
    _run(repo, "SessionEnd", reason="prompt_input_exit")
    payload = json.dumps(receipt_payload(_entry(repo), 0))
    assert "2 tool calls in the transcript reached no hook" in payload
    for leaked in ("hook_coverage", "hook_config_hash", "ok_settled", "SECRET INPUT", "RAW TOOL OUTPUT"):
        assert leaked not in payload
