"""`openshard usage trailers`: a cloud session's usage as commit trailers, claimed by one commit only."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from openshard.adapters import commit_usage_trailers
from openshard.cli.usage_cmd import usage_group
from openshard.models.pricing import estimate_usage_cost

SESSION = "11111111-2222-3333-4444-555555555555"
OLD = "2026-01-01T00:00:00+00:00"
CLAIMED_AT = datetime(2026, 2, 1, 0, 0, 30, tzinfo=UTC)  # when a claim is made, unless a test moves it


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
    monkeypatch.setattr(commit_usage_trailers, "_now", lambda: CLAIMED_AT)
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
        _msg("m_before", "2025-12-31T00:00:00Z", i=995),  # before HEAD's commit but never claimed: counted
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
        "Openshard-Tokens-Input: 999",
        "Openshard-Tokens-Output: 40",
        "Openshard-Tokens-Cache-Read: 400",
        "Openshard-Tokens-Cache-Creation: 100",
        "Openshard-Tokens-Cache-Creation-1h: 30",
    ]


def test_committing_subagent_gets_only_its_own_transcript(repo, tmp_path):
    _write(_transcript(tmp_path), [_msg("main1", "2026-02-01T00:00:00Z", o=1000)])
    _write(_sub(tmp_path, "other"), [_msg("o1", "2026-02-01T00:00:00Z", o=500)])
    _write(_sub(tmp_path, "me"), [_msg("me1", "2026-02-01T00:00:00Z", o=7), _open_call("cd x && openshard usage trailers")])
    result = _run()
    assert result.exit_code == 0, result.output
    assert "Openshard-Usage-Scope: agent" in result.output and "Openshard-Tokens-Output: 7" in result.output
    # The other agents' usage is still theirs: their own commit gets it, never "me1" again.
    _commit(repo)
    _write(_sub(tmp_path, "me"), [_result()], mode="a")
    _write(_sub(tmp_path, "other"), [_open_call("openshard usage trailers", "toolu_2")], mode="a")
    rest = _run()
    assert "Openshard-Usage-Scope: agent" in rest.output and "Openshard-Tokens-Output: 500" in rest.output


def _rebase_onto_newer_main(repo: Path, monkeypatch) -> None:
    """The agent's branch rebased onto a main commit made long after the agent started."""
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "agent"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-"], check=True)
    for key in ("GIT_COMMITTER_DATE", "GIT_AUTHOR_DATE"):
        monkeypatch.setenv(key, "2026-06-01T00:00:00+00:00")
    _commit(repo)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "agent"], check=True)
    subprocess.run(["git", "-C", str(repo), "rebase", "-q", "-"], check=True, capture_output=True)


def test_rebase_before_the_first_commit_does_not_shrink_the_agents_count(repo, tmp_path, monkeypatch):
    # The bug: with no claim yet the window started at HEAD's commit time, so after a rebase onto a newer
    # main commit the agent's earlier work fell outside it and an undercount was labelled exact.
    _write(_transcript(tmp_path), [_msg("main1", "2026-02-01T00:00:00Z", o=1000)])
    _write(_sub(tmp_path, "me"), [_msg("me1", "2026-02-01T00:00:00Z", o=2000), _msg("me2", "2026-05-01T00:00:00Z", o=453),
                                   _msg("me3", "2026-06-02T00:00:00Z", o=1)])
    _rebase_onto_newer_main(repo, monkeypatch)
    _write(_sub(tmp_path, "me"), [_open_call(f"cd {repo} && git commit -F m")], mode="a")
    result = _run()
    assert result.exit_code == 0, result.output
    assert "Openshard-Usage-Scope: agent" in result.output and "Openshard-Tokens-Output: 2454" in result.output
    # --since is still an explicit override.
    assert "Openshard-Tokens-Output: 1\n" in _run("--since", "HEAD").output


def test_second_commit_counts_only_the_agents_new_messages(repo, tmp_path, monkeypatch):
    _write(_transcript(tmp_path), [])
    me = _sub(tmp_path, "me")
    _write(me, [_msg("me1", "2026-02-01T00:00:00Z", o=2000), _open_call("openshard usage trailers")])
    assert "Openshard-Tokens-Output: 2000" in _run().output
    _commit(repo)
    _write(me, [_result(), _msg("me2", "2026-02-01T00:05:00Z", o=6)], mode="a")
    _rebase_onto_newer_main(repo, monkeypatch)  # a merge or rebase between commits changes nothing either
    _write(me, [_open_call("openshard usage trailers", "toolu_2")], mode="a")
    second = _run()
    assert "Openshard-Usage-Scope: agent" in second.output and "Openshard-Tokens-Output: 6\n" in second.output


def test_main_thread_and_subagent_each_claim_only_their_own_transcript(repo, tmp_path):
    main, sub = _transcript(tmp_path), _sub(tmp_path, "s")
    _write(main, [_msg("main1", "2026-02-01T00:00:00Z", o=11), _open_call("openshard usage trailers")])
    _write(sub, [_msg("s1", "2026-02-01T00:00:00Z", o=22)])
    assert "Openshard-Usage-Scope: agent" in (first := _run()).output and "Openshard-Tokens-Output: 11\n" in first.output
    _commit(repo)
    _write(main, [_result()], mode="a")
    _write(sub, [_open_call("openshard usage trailers", "toolu_2")], mode="a")
    assert "Openshard-Usage-Scope: agent" in (second := _run()).output and "Openshard-Tokens-Output: 22\n" in second.output


def test_session_window_starts_at_the_sessions_latest_landed_claim(repo, tmp_path, monkeypatch):
    main, sub = _transcript(tmp_path), _sub(tmp_path, "s")
    _write(main, [_msg("m1", "2026-02-01T00:00:00Z", o=1)])
    _write(sub, [_msg("s_old", "2026-01-15T00:00:00Z", o=100)])
    assert "Openshard-Tokens-Output: 101" in _run().output  # first window: the whole unclaimed session
    _commit(repo)
    _rebase_onto_newer_main(repo, monkeypatch)  # HEAD's time is not where the next window starts
    _write(sub, [_msg("s_late", "2026-02-01T00:00:10Z", o=1000), _msg("s_new", "2026-02-01T00:01:00Z", o=5)], mode="a")
    _write(main, [_msg("m2", "2026-02-01T00:02:00Z", o=7)], mode="a")
    window = _run()  # s_late predates the landed claim (00:00:30): outside the window, still s's to claim
    assert "Openshard-Usage-Scope: session-window" in window.output and "Openshard-Tokens-Output: 12\n" in window.output


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
    assert result.stdout == ""
    cost = estimate_usage_cost("claude-opus-5-5", input_tokens=1, output_tokens=10, cache_read_tokens=100,
                               cache_write_tokens=10, cache_write_1h_tokens=30)
    assert cost is not None
    assert result.stderr == f"Added usage trailers: scope session-window, 151 tokens, ~${cost.usd:.4f} list-rate est.\n"
    text = msg.read_text(encoding="utf-8")
    assert text.startswith("Did a thing\n\nOpenshard-Agent: Claude Code\n")
    assert "claude-opus-5.5" not in text and text.count("Openshard-Model:") == 1
    assert "Openshard-Usage-Scope: session-window\n" in text
    assert "Co-Authored-By: Claude" in text and "Openshard-Tokens-Cache-Creation-1h: 30\n" in text
    # git matches trailer keys by prefix; the total cache write must survive its -1h part.
    assert "Openshard-Tokens-Cache-Creation: 40\n" in text
    assert "\n\n" not in text.split("\n\n", 1)[1]  # one trailer block
    result = _run("--append-to", str(msg), "--since", "2026-01-01T00:00:00Z", "--no-record")  # an amend: replaced, not doubled
    again = msg.read_text(encoding="utf-8")
    assert again.count("Openshard-Tokens-Cache-Creation: 40\n") == 1 and again.count("Openshard-Model:") == 1


BASH = shutil.which("bash")


@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") != "true" and BASH is None, reason="requires bash")
@pytest.mark.parametrize("caller_known,provenance_expected", [
    (True, "official_rate_estimate"), (False, "official_rate_estimate_session_window")])
def test_workflow_prices_the_printed_trailers(repo, tmp_path, caller_known, provenance_expected):
    records = [_msg("m1", "2026-02-01T00:00:00Z", i=1000, o=2000, cr=30000, cw=4000, cw1h=1000)]
    _write(_transcript(tmp_path), records + ([_open_call("openshard usage trailers")] if caller_known else []))
    printed = _run()
    assert printed.exit_code == 0
    assert f"Openshard-Usage-Scope: {'agent' if caller_known else 'session-window'}" in printed.output
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
    assert ("may include parallel agents' work (Openshard-Usage-Scope: session-window)" in out) is not caller_known
    model, creation, cost, provenance = out.splitlines()[-1].split("|")
    expected = estimate_usage_cost("claude-opus-5-5", input_tokens=1000, output_tokens=2000, cache_read_tokens=30000,
                                   cache_write_tokens=3000, cache_write_1h_tokens=1000)
    assert expected is not None
    assert (model, creation, provenance) == ("claude-opus-5-5", "4000", provenance_expected)
    assert float(cost) == pytest.approx(expected.usd)


def _hook_commit(repo: Path, env: dict) -> str:
    """A real `git commit` through Openshard's prepare-commit-msg hook logic; the message it landed with."""
    from openshard.adapters.agent_commit_trailer import run_git_hook

    msg = repo / ".git" / "COMMIT_EDITMSG_TEST"
    msg.write_text("Do the work\n", encoding="utf-8")
    run_git_hook([str(msg)], env={**os.environ, **env})
    return msg.read_text(encoding="utf-8")


def test_a_cloud_commit_gets_its_usage_trailers_from_the_hook(repo, tmp_path):
    _write(_transcript(tmp_path), [_msg("m1", "2026-02-01T00:00:00Z", o=42)])
    text = _hook_commit(repo, {"CLAUDE_CODE_REMOTE": "true"})
    assert "Openshard-Model: claude-opus-5-5" in text and "Openshard-Tokens-Output: 42" in text
    _commit(repo)
    again = _hook_commit(repo, {"CLAUDE_CODE_REMOTE": "true"})
    assert "Openshard-Tokens" not in again  # claimed by the first commit: never counted twice


@pytest.mark.parametrize("env", [
    {},  # a laptop session: its hook-captured Receipt already holds the usage
    {"CLAUDE_CODE_REMOTE": "true", "OPENSHARD_COMMIT_TRAILERS": "off"},
    {"CLAUDE_CODE_REMOTE": "true", "GIT_REFLOG_ACTION": "rebase (pick)"},
])
def test_the_hook_adds_no_usage_outside_a_cloud_commit_of_ones_own(repo, tmp_path, env):
    _write(_transcript(tmp_path), [_msg("m1", "2026-02-01T00:00:00Z", o=42)])
    assert "Openshard-" not in _hook_commit(repo, env)


def test_no_true_number_adds_nothing_and_never_blocks(repo, tmp_path):
    assert _hook_commit(repo, {"CLAUDE_CODE_REMOTE": "true"}) == "Do the work\n"  # no transcript at all
