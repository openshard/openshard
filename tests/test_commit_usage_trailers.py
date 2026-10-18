"""`openshard usage trailers`: a cloud session's usage as commit trailers, claimed by one commit only."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from openshard.cli.usage_cmd import usage_group
from openshard.models.pricing import estimate_usage_cost

SESSION = "11111111-2222-3333-4444-555555555555"
OLD = "2026-01-01T00:00:00+00:00"


def _msg(mid: str, at: str, *, model: str = "claude-opus-5-5", i: int = 1, o: int = 10, cr: int = 100,
         cw: int = 20, cw1h: int = 0, **extra) -> dict:
    usage = {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr,
             "cache_creation_input_tokens": cw,
             "cache_creation": {"ephemeral_5m_input_tokens": cw - cw1h, "ephemeral_1h_input_tokens": cw1h}, **extra}
    return {"type": "assistant", "timestamp": at, "message": {"id": mid, "model": model, "usage": usage}}


def _write(path: Path, records: list[dict], mode: str = "w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    for key, value in (("GIT_COMMITTER_DATE", OLD), ("GIT_AUTHOR_DATE", OLD), ("GIT_AUTHOR_NAME", "t"),
                       ("GIT_AUTHOR_EMAIL", "t@example.com"), ("GIT_COMMITTER_NAME", "t"),
                       ("GIT_COMMITTER_EMAIL", "t@example.com")):
        monkeypatch.setenv(key, value)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _commit(root)
    monkeypatch.chdir(root)
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    return root


def _transcript(tmp_path: Path) -> Path:
    return tmp_path / "claude" / "projects" / "-home-claude" / f"{SESSION}.jsonl"


def _run(*args: str):
    return CliRunner().invoke(usage_group, ["trailers", *args])


def _commit(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "work"], check=True)


def _open_call(command: str, call_id: str = "toolu_1", at: str = "2026-02-01T00:09:00Z") -> dict:
    """The Bash call an agent is waiting on, as Claude Code records it before the command runs."""
    return {"type": "assistant", "timestamp": at, "message": {"id": f"msg_{call_id}", "model": "claude-opus-5-5", "content": [
        {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]}}


def _result(call_id: str = "toolu_1") -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": call_id, "content": "ok"}]}}


def _sub(tmp_path: Path, name: str) -> Path:
    return _transcript(tmp_path).with_suffix("") / "subagents" / f"agent-{name}.jsonl"


def test_unidentified_caller_gets_session_window_usage_once_per_message(repo, tmp_path):
    main = _transcript(tmp_path)
    _write(main, [
        _msg("m_before", "2025-12-31T00:00:00Z", i=999),  # before the last commit: not this commit's
        _msg("m1", "2026-02-01T00:00:00Z", o=5),
        _msg("m1", "2026-02-01T00:00:01Z", o=10),  # the same streamed message re-reported: last line wins
        _msg("m2", "2026-02-01T00:01:00Z", cw=40, cw1h=30),
        _msg("syn", "2026-02-01T00:02:00Z", model="<synthetic>", i=0, o=0, cr=0, cw=0),
    ])
    # No transcript has this call open (e.g. a background run), so the caller cannot be told apart.
    _write(_sub(tmp_path, "abc"), [_msg("s1", "2026-02-01T00:03:00Z", i=2), _msg("m2", "2026-02-01T00:01:00Z", cw=40, cw1h=30),
                                    _open_call("git status"), _result()])
    result = _run()
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "Openshard-Model: claude-opus-5-5",
        "Openshard-Usage-Scope: session-window",
        "Openshard-Tokens-Input: 4",
        "Openshard-Tokens-Output: 30",
        "Openshard-Tokens-Cache-Read: 300",
        "Openshard-Tokens-Cache-Creation: 80",
        "Openshard-Tokens-Cache-Creation-1h: 30",
    ]


def test_committing_subagent_gets_only_its_own_transcript(repo, tmp_path):
    _write(_transcript(tmp_path), [_msg("main1", "2026-02-01T00:00:00Z", o=1000)])
    _write(_sub(tmp_path, "other"), [_msg("o1", "2026-02-01T00:00:00Z", o=500)])
    _write(_sub(tmp_path, "me"), [_msg("me1", "2026-02-01T00:00:00Z", o=7), _open_call("cd x && openshard usage trailers")])
    result = _run()
    assert result.exit_code == 0, result.output
    assert "Openshard-Usage-Scope: agent" in result.output and "Openshard-Tokens-Output: 7" in result.output
    # The other agents' usage is still theirs: a later session-window commit gets it, never "me1" again.
    _commit(repo)
    _write(_sub(tmp_path, "me"), [_result()], mode="a")
    rest = _run()
    assert "Openshard-Usage-Scope: session-window" in rest.output and "Openshard-Tokens-Output: 1500" in rest.output


def test_parallel_callers_are_told_apart_by_the_repository_they_name(repo, tmp_path):
    _write(_transcript(tmp_path), [])
    _write(_sub(tmp_path, "a"), [_msg("a1", "2026-02-01T00:00:00Z", o=3), _open_call(f"cd {repo} && git commit -F m")])
    _write(_sub(tmp_path, "b"), [_msg("b1", "2026-02-01T00:00:00Z", o=4), _open_call("cd /elsewhere && git commit -F m")])
    result = _run()
    assert "Openshard-Usage-Scope: agent" in result.output and "Openshard-Tokens-Output: 3" in result.output


def test_another_agents_open_claim_is_not_counted_again(repo, tmp_path):
    _write(_transcript(tmp_path), [])
    _write(_sub(tmp_path, "a"), [_msg("a1", "2026-02-01T00:00:00Z", o=3), _open_call("openshard usage trailers")])
    _write(_sub(tmp_path, "b"), [_msg("b1", "2026-02-01T00:00:00Z", o=4)])
    assert "Openshard-Tokens-Output: 3" in _run().output  # a claims; its commit has not landed yet
    _write(_sub(tmp_path, "a"), [_result()], mode="a")
    window = _run()  # nobody identifiable now: session window, without a's pending claim
    assert "Openshard-Usage-Scope: session-window" in window.output and "Openshard-Tokens-Output: 4" in window.output


def test_landed_commit_keeps_its_usage_and_the_next_one_gets_only_new_usage(repo, tmp_path):
    main = _transcript(tmp_path)
    _write(main, [_msg("m1", "2026-02-01T00:00:00Z")])
    first = _run()
    assert first.exit_code == 0 and "Openshard-Tokens-Output: 10" in first.output
    # A commit that never landed releases its usage: asking again claims the same tokens.
    assert _run().output == first.output
    _commit(repo)
    again = _run()
    assert again.exit_code == 1 and again.stdout == ""
    assert "No new usage" in again.stderr
    _write(main, [_msg("m2", "2026-02-01T00:05:00Z", o=7)], mode="a")
    second = _run()
    assert second.exit_code == 0
    assert "Openshard-Tokens-Output: 7" in second.output and "Openshard-Tokens-Input: 1" in second.output


@pytest.mark.parametrize("records,reason", [
    ([_msg("m1", "2026-02-01T00:00:00Z", o="many")], "incomplete"),  # type: ignore[arg-type]
    ([_msg("m1", "2026-02-01T00:00:00Z"), _msg("m2", "2026-02-01T00:00:01Z", model="claude-haiku-5-5")], "2 models"),
    ([_msg("m1", "2026-02-01T00:00:00Z", speed="fast")], "above list rates"),
    ([], "No new usage"),
])
def test_unknown_usage_prints_no_trailers(repo, tmp_path, records, reason):
    _write(_transcript(tmp_path), records)
    result = _run()
    assert result.exit_code == 1 and result.stdout == ""
    assert reason in result.stderr and "Usage stays unknown" in result.stderr


def test_no_session_prints_no_trailers(repo, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    result = _run()
    assert result.exit_code == 1 and result.stdout == "" and "No Claude Code session" in result.stderr


def test_append_to_replaces_the_declared_model_and_keeps_other_trailers(repo, tmp_path):
    _write(_transcript(tmp_path), [_msg("m1", "2026-02-01T00:00:00Z", cw=40, cw1h=30)])
    msg = tmp_path / "msg.txt"
    msg.write_text("Did a thing\n\nOpenshard-Agent: Claude Code\nOpenshard-Model: claude-opus-5.5\n"
                   "Co-Authored-By: Claude <noreply@anthropic.com>\n", encoding="utf-8")
    result = _run("--append-to", str(msg))
    assert result.exit_code == 0, result.output
    text = msg.read_text(encoding="utf-8")
    assert text.startswith("Did a thing\n\nOpenshard-Agent: Claude Code\n")
    assert "claude-opus-5.5" not in text and text.count("Openshard-Model:") == 1
    assert "Openshard-Usage-Scope: session-window\n" in text
    assert "Co-Authored-By: Claude" in text and "Openshard-Tokens-Cache-Creation-1h: 30\n" in text
    assert "\n\n" not in text.split("\n\n", 1)[1]  # one trailer block


BASH = shutil.which("bash")


@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") != "true" and BASH is None, reason="requires bash")
def test_workflow_prices_the_printed_trailers(repo, tmp_path):
    _write(_transcript(tmp_path), [_msg("m1", "2026-02-01T00:00:00Z", i=1000, o=2000, cr=30000, cw=4000, cw1h=1000)])
    printed = _run()
    assert printed.exit_code == 0
    script = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/openshard-cloud-receipts.yml").read_text())
    run = next(s["run"] for s in script["jobs"]["capture"]["steps"] if s.get("name") == "Verify and create hosted Receipts")
    reads = re.search(r"^ *metadata_value\(\) \{.*?^ *usage_scope=.*?$", run, re.S | re.M)
    estimate = re.search(r"^ *cost_provenance=\"\"\n.*?^ *\[\[ -n \"\$cost_provenance\" \]\].*?$", run, re.S | re.M)
    assert reads is not None and estimate is not None
    command = (f"set -uo pipefail\nsha=abc\npr_body=''\nmessage={shlex.quote('Work' + chr(10) + chr(10) + printed.output)}\n"
               f"{reads.group(0)}\n{estimate.group(0)}\n"
               'printf "%s|%s|%s|%s" "$model" "$tokens_cache_creation" "$cost" "$cost_provenance"')
    env = {**os.environ, "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")}
    out = subprocess.run([str(BASH), "-c", command], text=True, capture_output=True, check=True, env=env).stdout
    assert "may include parallel agents' work (Openshard-Usage-Scope: session-window)" in out
    model, creation, cost, provenance = out.splitlines()[-1].split("|")
    expected = estimate_usage_cost("claude-opus-5-5", input_tokens=1000, output_tokens=2000, cache_read_tokens=30000,
                                   cache_write_tokens=3000, cache_write_1h_tokens=1000)
    assert expected is not None
    assert (model, creation, provenance) == ("claude-opus-5-5", "4000", "official_rate_estimate")
    assert float(cost) == pytest.approx(expected.usd)
