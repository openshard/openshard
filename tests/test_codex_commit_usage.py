"""Codex commits: the thread's own usage as commit trailers, claimed by one commit only."""
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

from openshard.adapters import agent_commit_trailer as act
from openshard.adapters import codex_commit_usage
from openshard.adapters.commit_usage_trailers import UsageUnavailable
from openshard.models.pricing import estimate_usage_cost

THREAD = "019a2b3c-4d5e-7f00-8111-222233334444"
MODEL = "gpt-5.5"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo: Path) -> None:
    (repo / "f.txt").write_text((repo / "f.txt").read_text() + "x\n" if (repo / "f.txt").exists() else "x\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "c")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    for key, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.com"),
                       ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.com")):
        monkeypatch.setenv(key, value)
    _git(tmp_path, "init", "-q", str(root))
    _git(root, "config", "commit.gpgsign", "false")
    _commit(root)
    monkeypatch.chdir(root)
    monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv(act.ENV_OPT_OUT, raising=False)
    return root


def _rollout(tmp_path: Path, totals: list[tuple[int, int, int]], *, models=(MODEL,), thread: str = THREAD) -> Path:
    """A Codex rollout: session_meta, a turn_context per model, then cumulative token_count records."""
    path = tmp_path / "codex" / "sessions" / "2026" / "10" / "24" / f"rollout-2026-10-24T10-00-00-{thread}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [{"type": "session_meta", "payload": {"id": thread, "model_provider": "openai"}}]
    records += [{"type": "turn_context", "payload": {"model": m}} for m in models]
    records += [{"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": i, "cached_input_tokens": c, "output_tokens": o}}}} for i, c, o in totals]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _hook_commit(repo: Path, **env: str) -> str:
    msg = repo / ".git" / "COMMIT_EDITMSG_TEST"
    msg.write_text("Do the work\n", encoding="utf-8")
    act.run_git_hook([str(msg)], env={**os.environ, "CODEX_THREAD_ID": THREAD, **env})
    return msg.read_text(encoding="utf-8")


def test_a_codex_commit_gets_its_threads_model_and_tokens(repo, tmp_path):
    _rollout(tmp_path, [(1000, 400, 50), (5000, 3000, 700)])
    text = _hook_commit(repo)
    # input excludes cached input; no cache-write trailer (Codex does not report one)
    for line in ("Openshard-Model: gpt-5.5", "Openshard-Usage-Scope: agent", "Openshard-Tokens-Input: 2000",
                 "Openshard-Tokens-Output: 700", "Openshard-Tokens-Cache-Read: 3000"):
        assert line + "\n" in text
    assert "Cache-Creation" not in text


def test_the_next_commit_gets_only_the_new_usage_and_never_twice(repo, tmp_path):
    _rollout(tmp_path, [(5000, 3000, 700)])
    _hook_commit(repo)
    _commit(repo)  # the claim landed
    assert "Openshard-Tokens" not in _hook_commit(repo)  # nothing new
    _rollout(tmp_path, [(5000, 3000, 700), (9000, 6000, 900)])
    text = _hook_commit(repo)
    assert "Openshard-Tokens-Input: 1000\n" in text and "Openshard-Tokens-Output: 200\n" in text
    assert "Openshard-Tokens-Cache-Read: 3000\n" in text


def test_a_commit_that_never_landed_releases_its_claim(repo, tmp_path):
    _rollout(tmp_path, [(5000, 3000, 700)])
    _hook_commit(repo)  # HEAD does not move: e.g. the commit was aborted
    assert "Openshard-Tokens-Output: 700\n" in _hook_commit(repo)


@pytest.mark.parametrize("setup,reason", [
    (lambda t: None, "No rollout"),
    (lambda t: _rollout(t, [(100, 0, 10)], models=("gpt-5.5", "gpt-5.4")), "one model"),
    (lambda t: _rollout(t, [(100, 0, 10), (100, 200, 10)]), "malformed"),  # cached > input
    (lambda t: _rollout(t, [(100, 0, 10)], thread="019a0000-0000-7000-8000-000000000000").rename(
        t / "codex" / "sessions" / "2026" / "10" / "24" / f"rollout-x-{THREAD}.jsonl"), "no readable"),
])
def test_no_true_number_adds_nothing(repo, tmp_path, setup, reason):
    setup(tmp_path)
    with pytest.raises(UsageUnavailable, match=reason):
        codex_usage_trailers_for(repo)
    assert _hook_commit(repo) == "Do the work\n"


def codex_usage_trailers_for(repo: Path) -> list[str]:
    return codex_commit_usage.codex_usage_trailers(repo, THREAD)


@pytest.mark.parametrize("env", [
    {"OPENSHARD_COMMIT_TRAILERS": "off"},
    {"GIT_REFLOG_ACTION": "rebase (pick)"},
    {"CLAUDE_CODE_SESSION_ID": "0f9b1c2d-1111-4222-8333-944455556666"},  # Claude's own path decides
])
def test_opt_out_replays_and_claude_sessions_add_nothing(repo, tmp_path, env):
    _rollout(tmp_path, [(5000, 3000, 700)])
    assert "Openshard-Tokens" not in _hook_commit(repo, **env)


def test_a_thread_openshard_captured_is_left_to_its_receipt(repo, tmp_path):
    _rollout(tmp_path, [(5000, 3000, 700)])
    from openshard.adapters.claude_hooks import buffer_path

    buffer = buffer_path(repo, THREAD, "codex")
    buffer.parent.mkdir(parents=True)
    buffer.write_text(json.dumps({"schema_version": 1, "agent": "codex", "session_id": THREAD,
                                  "events": [], "ended": None}))
    text = _hook_commit(repo)
    assert "Openshard-Tokens" not in text
    assert "Openshard-Agent: Codex\n" in text  # an observed Codex thread is marked like a Claude Code session


def test_hook_script_starts_the_cli_for_codex_threads():
    assert '[ -n "${CODEX_THREAD_ID:-}" ]' in act.hook_script()


BASH = shutil.which("bash")


@pytest.mark.skipif(BASH is None, reason="requires bash")
def test_workflow_prices_the_codex_trailers(repo, tmp_path):
    _rollout(tmp_path, [(50_000, 30_000, 4_000)])
    trailers = "\n".join(codex_usage_trailers_for(repo))
    script = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/openshard-cloud-receipts.yml").read_text())
    run = next(s["run"] for s in script["jobs"]["capture"]["steps"] if s.get("name") == "Verify and create hosted Receipts")
    reads = re.search(r"^ *metadata_value\(\) \{.*?^ *usage_scope=.*?$", run, re.S | re.M)
    estimate = re.search(r"^ *cost_provenance=\"\"\n.*?^ *\[\[ -n \"\$cost_provenance\" \]\].*?$", run, re.S | re.M)
    assert reads is not None and estimate is not None
    command = (f"set -uo pipefail\nsha=abc\npr_body=''\nmessage={shlex.quote('Work' + chr(10) + chr(10) + trailers)}\n"
               f"{reads.group(0)}\n{estimate.group(0)}\n"
               'printf "%s|%s|%s|%s" "$model" "$tokens_cache_creation" "$cost" "$cost_provenance"')
    env = {**os.environ, "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")}
    out = subprocess.run([str(BASH), "-c", command], text=True, capture_output=True, check=True, env=env).stdout
    model, creation, cost, provenance = out.splitlines()[-1].split("|")
    expected = estimate_usage_cost(MODEL, input_tokens=20_000, output_tokens=4_000, cache_read_tokens=30_000,
                                   cache_write_tokens=0, cache_write_1h_tokens=0)
    assert expected is not None
    assert (model, creation, provenance) == (MODEL, "", "official_rate_estimate")  # cache writes: not reported
    assert float(cost) == pytest.approx(expected.usd)


def test_a_real_git_commit_through_the_installed_hook(repo, tmp_path):
    _rollout(tmp_path, [(5000, 3000, 700)])
    assert act.install_commit_hook(repo).state == "installed"
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "openshard").write_text(
        "#!/bin/sh\n"
        f'exec "{Path(sys.executable).as_posix()}" -c "import sys; sys.argv[0] = \'openshard\'; '
        'from openshard.cli.entrypoint import main; main()" "$@"\n')
    (shim / "openshard").chmod(0o755)
    root = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "CODEX_THREAD_ID": THREAD, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}",
           "PYTHONPATH": root + os.pathsep + os.environ.get("PYTHONPATH", "")}
    (repo / "g.txt").write_text("g\n")
    subprocess.run(["git", "add", "g.txt"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Codex change"], cwd=repo, env=env, check=True)
    body = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=repo, text=True, capture_output=True).stdout
    assert "Openshard-Model: gpt-5.5\n" in body and "Openshard-Tokens-Output: 700\n" in body
