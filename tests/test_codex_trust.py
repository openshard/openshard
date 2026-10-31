"""Codex hook-trust detection (hooks approved via /hooks in $CODEX_HOME/config.toml)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard.adapters.agent_setup import detect_codex_integration, install_agent
from openshard.adapters.codex_hooks_install import HOOK_EVENTS, HOOKS_RELPATH, install_codex_hooks
from openshard.adapters.codex_trust import (
    HOOKS_REVIEW_NOTE,
    HOOKS_TRUST_NEXT_STEP,
    codex_hook_trust,
)
from openshard.cli.main import cli
from tests.capture_fixtures import _make_repo

SNAKE = {
    "SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit",
    "PostToolUse": "post_tool_use", "Stop": "stop", "SessionEnd": "session_end", "Interrupt": "interrupt",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = _make_repo(tmp_path / "codex repo")
    install_codex_hooks(repo_root=r)
    return r


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "chome"
    h.mkdir()
    return h


def _write_config(home: Path, repo: Path, events, *, path_text: str | None = None) -> None:
    hooks_path = path_text or str((repo / HOOKS_RELPATH).resolve())
    lines = []
    for e in events:
        lines.append(f"[hooks.state.'{hooks_path}:{SNAKE[e]}:0:0']")
        lines.append('trusted_hash = "sha256:abc"')
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _env(home: Path) -> dict:
    return {"CODEX_HOME": str(home)}


def test_trusted(repo, home):
    _write_config(home, repo, HOOK_EVENTS)
    assert codex_hook_trust(repo, env=_env(home)) == "trusted"


def test_untrusted_when_no_entries_for_this_repo(repo, home, tmp_path):
    _write_config(home, repo, HOOK_EVENTS, path_text=str(tmp_path / "other" / ".codex" / "hooks.json"))
    assert codex_hook_trust(repo, env=_env(home)) == "untrusted"


def test_partial(repo, home):
    _write_config(home, repo, ["SessionStart", "Stop"])
    assert codex_hook_trust(repo, env=_env(home)) == "partial"


def test_unknown_missing_file_bad_toml_and_no_hooks(repo, home, tmp_path):
    assert codex_hook_trust(repo, env=_env(home)) == "unknown"
    (home / "config.toml").write_text("[hooks.state\nnot toml", encoding="utf-8")
    assert codex_hook_trust(repo, env=_env(home)) == "unknown"
    _write_config(home, repo, HOOK_EVENTS)
    assert codex_hook_trust(tmp_path / "no hooks here", env=_env(home)) == "unknown"


def test_codex_home_is_honoured(repo, home, tmp_path):
    _write_config(home, repo, HOOK_EVENTS)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert codex_hook_trust(repo, env=_env(home)) == "trusted"
    assert codex_hook_trust(repo, env=_env(elsewhere)) == "unknown"


def test_only_events_present_in_hooks_json_are_required(repo, home):
    path = repo / HOOKS_RELPATH
    data = json.loads(path.read_text(encoding="utf-8"))
    for event in ("SessionEnd", "Interrupt"):
        data["hooks"][event] = []
    path.write_text(json.dumps(data), encoding="utf-8")
    _write_config(home, repo, ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"])
    assert codex_hook_trust(repo, env=_env(home)) == "trusted"


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive paths are a Windows behaviour")
def test_windows_case_and_separators(repo, home):
    original = str((repo / HOOKS_RELPATH).resolve())
    _write_config(home, repo, HOOK_EVENTS, path_text=original.swapcase().replace("\\", "/"))
    assert codex_hook_trust(repo, env=_env(home)) == "trusted"


def test_status_field_and_json(repo, home, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert detect_codex_integration(repo).to_dict()["hooks_trust"] == "unknown"
    _write_config(home, repo, ["Stop"])
    assert detect_codex_integration(repo).hooks_trust == "partial"
    _write_config(home, repo, HOOK_EVENTS)
    assert detect_codex_integration(repo).to_dict()["hooks_trust"] == "trusted"


def _which(name: str):
    return {"codex": "/usr/local/bin/codex", "openshard": "/usr/local/bin/openshard"}.get(name)


def test_doctor_text_and_json(repo, home, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(home))
    runner = CliRunner()
    with patch("shutil.which", side_effect=_which):
        unknown = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
        _write_config(home, repo, [])
        (home / "config.toml").write_text("[projects.'x']\ntrust_level = 'trusted'\n", encoding="utf-8")
        warned = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
        as_json = runner.invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
        _write_config(home, repo, HOOK_EVENTS)
        ok = runner.invoke(cli, ["doctor", "--repo-path", str(repo)])
    assert HOOKS_TRUST_NEXT_STEP not in unknown.output
    assert HOOKS_TRUST_NEXT_STEP in warned.output
    assert json.loads(as_json.output)["codex"]["hooks_trust"] == "untrusted"
    assert HOOKS_TRUST_NEXT_STEP not in ok.output


def test_install_hint_is_conditional(tmp_path, home, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(home))
    r = _make_repo(tmp_path / "fresh")
    (home / "config.toml").write_text("[projects.'x']\ntrust_level = 'trusted'\n", encoding="utf-8")
    first = install_agent("codex", repo_root=r)
    assert HOOKS_TRUST_NEXT_STEP in first.next_steps
    _write_config(home, r, HOOK_EVENTS)
    again = install_agent("codex", repo_root=r)
    assert HOOKS_TRUST_NEXT_STEP not in again.next_steps
    # Codex config unreadable: no claim either way.
    (home / "config.toml").unlink()
    assert HOOKS_TRUST_NEXT_STEP not in install_agent("codex", repo_root=r).next_steps


def _trust_project(home: Path, path_text: str) -> None:
    (home / "config.toml").write_text(f"[projects.'{path_text}']\ntrust_level = \"trusted\"\n", encoding="utf-8")


def test_trusted_project_without_hook_state_is_trusted(repo, home):
    from openshard.adapters.codex_trust import codex_hooks_reviewed

    _trust_project(home, str(repo.resolve()))
    assert codex_hook_trust(repo, env=_env(home)) == "trusted"
    assert codex_hooks_reviewed(repo, env=_env(home)) == "unreviewed"
    _write_config(home, repo, HOOK_EVENTS)
    assert codex_hooks_reviewed(repo, env=_env(home)) == "reviewed"


def test_project_key_matches_case_separators_and_trailing_slash(repo, home):
    text = str(repo.resolve())
    if os.name == "nt":
        _trust_project(home, text.swapcase().replace("\\", "/") + "/")
        assert codex_hook_trust(repo, env=_env(home)) == "trusted"
    else:
        _trust_project(home, text + "/")
        assert codex_hook_trust(repo, env=_env(home)) == "trusted"


def test_untrusted_project_level_trust_other_path(repo, home, tmp_path):
    _trust_project(home, str(tmp_path / "elsewhere"))
    assert codex_hook_trust(repo, env=_env(home)) == "untrusted"


def test_trusted_but_unreviewed_is_not_a_blocker(repo, home, monkeypatch):
    # Seen live: a trusted project captured through `codex exec` with no /hooks review recorded,
    # so doctor raises nothing; the review state stays available in --json.
    monkeypatch.setenv("CODEX_HOME", str(home))
    _trust_project(home, str(repo.resolve()))
    with patch("shutil.which", side_effect=_which):
        out = CliRunner().invoke(cli, ["doctor", "--repo-path", str(repo)])
        data = CliRunner().invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
    assert HOOKS_TRUST_NEXT_STEP not in out.output
    assert '"hooks_review": "unreviewed"' in data.output


# --- "Capture verified" needs a hook capture newer than the hooks file -----------------------


def _write_runs(repo: Path, *entries: dict) -> None:
    (repo / ".openshard").mkdir(exist_ok=True)
    (repo / ".openshard" / "runs.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
    )


def _hook_record(timestamp: str) -> dict:
    return {"timestamp": timestamp, "run_id": "r1", "task": "t",
            "executor": "codex_hooks", "import_source": "codex"}


def _doctor(repo: Path, home: Path, monkeypatch) -> tuple[str, dict]:
    monkeypatch.setenv("CODEX_HOME", str(home))
    with patch("shutil.which", side_effect=_which):
        out = CliRunner().invoke(cli, ["doctor", "--repo-path", str(repo)])
        data = CliRunner().invoke(cli, ["doctor", "--json", "--repo-path", str(repo)])
    assert out.exit_code == 0, out.output
    return out.output, json.loads(data.output)["codex"]


def test_record_older_than_hooks_file_is_unverified(repo, home, monkeypatch):
    _trust_project(home, str(repo.resolve()))
    _write_config(home, repo, HOOK_EVENTS)
    _write_runs(repo, _hook_record("2026-01-02T03:04:05Z"))
    out, codex = _doctor(repo, home, monkeypatch)
    assert "✓ Capture verified" not in out
    assert ("✗ Capture verified (last captured 2026-01-02 03:04 UTC, before the Codex hooks were "
            "last changed; run one Codex task to re-verify)") in out
    assert codex["capture_observed"] is False and codex["capture_verified"] is False
    assert "Configured but unverified: Codex" in out


def test_imported_history_only_is_unverified(repo, home, monkeypatch):
    _trust_project(home, str(repo.resolve()))
    _write_config(home, repo, HOOK_EVENTS)
    now = datetime.now(UTC).isoformat()
    _write_runs(repo, {"timestamp": now, "run_id": "hist-1", "task": "t", "executor": "codex_history_import",
                       "origin": "historical_import", "import_source": "codex"})
    out, codex = _doctor(repo, home, monkeypatch)
    assert "✗ Capture verified (only imported Codex history found; run one Codex task to verify)" in out
    assert codex["capture_verified"] is False


def test_fresh_hook_record_is_verified(repo, home, monkeypatch):
    _trust_project(home, str(repo.resolve()))
    _write_config(home, repo, HOOK_EVENTS)
    stale = _hook_record("2026-01-02T03:04:05Z")
    _write_runs(repo, stale, _hook_record(datetime.now(UTC).isoformat()))
    out, codex = _doctor(repo, home, monkeypatch)
    assert "✓ Capture verified" in out and "use Codex normally" in out
    assert codex["capture_verified"] is True and codex["capture_unverified_reason"] is None
    assert HOOKS_REVIEW_NOTE not in out  # reviewed: no warning


def test_unreviewed_hooks_warn_to_run_hooks_review(repo, home, monkeypatch):
    _trust_project(home, str(repo.resolve()))
    out, codex = _doctor(repo, home, monkeypatch)
    assert codex["hooks_review"] == "unreviewed"
    assert f"  ! {HOOKS_REVIEW_NOTE}" in out
    assert "run `/hooks` in Codex and approve them" in out
