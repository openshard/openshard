"""Claude Code ``StopFailure``: a turn that ended on an API error says why it is partial."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest
from click.testing import CliRunner

from openshard.adapters import claude_hooks as ch
from openshard.adapters.claude_hooks_install import HOOK_SPECS, TRANSPORT_HTTP
from openshard.adapters.claude_session_fidelity import sweep_stale_buffers
from openshard.cli.main import cli
from openshard.sync.envelope import build_envelope
from tests.capture_fixtures import _lines, _make_repo

SID = "61616161-6161-4161-8161-616161616161"
SCHEMA = json.loads(
    (Path(__file__).parent / "fixtures" / "platform" / "receipt-sync-envelope.v1.json").read_text(encoding="utf-8")
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "factory")


def _hook(repo: Path, event: str, **fields) -> None:
    doc = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, **fields}
    ch.handle_claude_hook(doc, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _record(repo: Path) -> dict:
    [entry] = [e for e in _lines(repo) if (e.get("capture") or {}).get("session_id") == SID]
    return entry


def _reasons(entry: dict) -> dict[str, dict]:
    return {r["kind"]: r for r in entry["capture"]["completeness"]["reasons"]}


def _failed_turn(repo: Path, error: object = "rate_limit") -> dict:
    _hook(repo, "SessionStart", source="startup")
    _hook(repo, "UserPromptSubmit", prompt="add a feature")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _hook(repo, "StopFailure", error=error)
    return _record(repo)


class TestStopFailure:
    def test_an_api_error_marks_the_turn_partial_and_names_the_error(self, repo):
        entry = _failed_turn(repo, "rate_limit")
        assert entry["capture"]["completeness"]["status"] == "incomplete"
        reason = _reasons(entry)["stopped_on_error"]
        assert reason["detail"] == "Claude Code reported the turn stopped: rate_limit"
        # Not a completed turn: nothing about the turn is upgraded.
        assert entry["capture"].get("turn_count", 0) == 0
        assert entry["capture"].get("status") != "turn_completed"

    def test_the_output_limit_is_a_cap(self, repo):
        assert "stopped_at_cap" in _reasons(_failed_turn(repo, "max_output_tokens"))

    @pytest.mark.parametrize("error", ["something_new", None, 42])
    def test_an_undocumented_error_is_unknown_never_guessed(self, repo, error):
        reason = _reasons(_failed_turn(repo, error))["stopped_on_error"]
        assert reason["detail"].endswith(": unknown")

    def test_repeated_stops_are_counted(self, repo):
        _failed_turn(repo)
        _hook(repo, "UserPromptSubmit", prompt="try again")
        _hook(repo, "StopFailure", error="overloaded")
        assert _reasons(_record(repo))["stopped_on_error"]["count"] == 2

    def test_a_normal_turn_gets_no_termination_reason(self, repo):
        _hook(repo, "SessionStart", source="startup")
        _hook(repo, "UserPromptSubmit", prompt="add a feature")
        _hook(repo, "Stop")
        reasons = _reasons(_record(repo)) if _record(repo)["capture"].get("completeness") else {}
        assert not {"stopped_on_error", "stopped_at_cap"} & set(reasons)

    def test_the_idle_sweep_keeps_the_cause_beside_its_own_timeout_reasons(self, repo):
        _failed_turn(repo)
        sweep_stale_buffers(repo, max_age_seconds=60, now=datetime.now(UTC) + timedelta(hours=2))
        reasons = _reasons(_record(repo))
        assert {"stopped_on_error", "session_end_not_observed", "session_idle_sealed"} <= set(reasons)

    def test_the_reason_syncs_within_the_platform_schema(self, repo):
        entry = _failed_turn(repo)
        doc = build_envelope(entry, 0, core_version="9.9.9")
        kinds = {r["kind"] for r in doc["receipt"]["capture_completeness"]["reasons"]}
        assert "stopped_on_error" in kinds
        errors = list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(doc))
        assert not errors, [e.message for e in errors[:3]]

    def test_last_shows_why_the_capture_is_partial(self, repo, monkeypatch):
        _failed_turn(repo)
        monkeypatch.chdir(repo)
        out = CliRunner().invoke(cli, ["last"], catch_exceptions=False).output
        assert "Claude Code reported the turn stopped: rate_limit" in out

    def test_claude_installs_the_stop_failure_hook_over_http(self):
        assert {s.event: s for s in HOOK_SPECS}["StopFailure"].transport == TRANSPORT_HTTP
