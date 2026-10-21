"""The Openshard-Agent commit trailer: marked only inside an observed agent session, hook coexistence, opt-out.

Real ``git commit`` runs through the installed ``prepare-commit-msg`` hook,
with an ``openshard`` shim on PATH that runs this checkout's CLI.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from openshard.adapters import agent_commit_trailer as act

ROOT = Path(__file__).resolve().parents[1]
SID = "0f9b1c2d-1111-4222-8333-944455556666"
TRAILER = "Openshard-Agent: Claude Code"
# The pattern the cloud-receipts workflow uses to find declared agent metadata.
WORKFLOW_AGENT_RE = re.compile(r"^Openshard-Agent:[ \t]*(.+)$", re.IGNORECASE | re.MULTILINE)


def _git(repo: Path, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=check)


@pytest.fixture
def shim(tmp_path: Path) -> Path:
    """A directory holding an ``openshard`` command that runs this checkout's CLI."""
    d = tmp_path / "bin"
    d.mkdir()
    py = Path(sys.executable).as_posix()
    script = d / "openshard"
    script.write_bytes((
        "#!/bin/sh\n"
        f'exec "{py}" -c "import sys; sys.argv[0] = \'openshard\'; '
        f'from openshard.cli.entrypoint import main; main()" "$@"\n'
    ).encode())
    script.chmod(0o755)
    return d


def _env(shim: Path, *, session: str | None = None, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE", act.ENV_OPT_OUT, "GIT_DIR", "GIT_WORK_TREE")}
    env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["GIT_EDITOR"] = "true"
    if session:
        env["CLAUDE_CODE_SESSION_ID"] = session
    env.update(extra)
    return env


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "dev@example.com")
    _git(r, "config", "user.name", "Dev")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "a.txt").write_text("a\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "initial")
    return r


def _observe(repo: Path, sid: str = SID, **fields) -> Path:
    """An open Claude Code session buffer, as the capture service writes it."""
    d = repo / ".openshard" / "claude_sessions"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.json"
    path.write_text(json.dumps({"schema_version": 1, "agent": "claude_code", "session_id": sid,
                                "events": [], "ended": None, **fields}))
    return path


def _commit(repo: Path, env: dict, msg: str = "change", *extra: str, name: str = "b.txt") -> None:
    (repo / name).write_text((repo / name).read_text() + "x\n" if (repo / name).exists() else "x\n")
    _git(repo, "add", name, env=env)
    _git(repo, "commit", "-q", "-m", msg, *extra, env=env)


def _msg(repo: Path, rev: str = "HEAD") -> str:
    return _git(repo, "log", "-1", "--format=%B", rev).stdout


def _installed(repo: Path) -> Path:
    status = act.install_commit_hook(repo)
    assert status.state in ("installed", "chained"), status
    return Path(status.hook_path or "")


# -- marking ---------------------------------------------------------------


def test_agent_session_commit_gets_exactly_one_trailer(repo: Path, shim: Path) -> None:
    _installed(repo)
    _observe(repo)
    _commit(repo, _env(shim, session=SID))
    msg = _msg(repo)
    assert msg.count("Openshard-Agent:") == 1
    assert TRAILER in msg.splitlines()
    assert WORKFLOW_AGENT_RE.search(msg).group(1).strip() == "Claude Code"
    # A real git trailer, not just a line in the body.
    parsed = subprocess.run(["git", "interpret-trailers", "--parse"], input=msg, capture_output=True, text=True).stdout
    assert TRAILER in parsed.splitlines()


def test_human_commit_gets_no_trailer(repo: Path, shim: Path) -> None:
    _installed(repo)
    _observe(repo)  # an agent session is open in this repository, but this process is not in it
    _commit(repo, _env(shim))
    assert "Openshard-Agent" not in _msg(repo)


def test_session_id_without_observed_session_adds_nothing(repo: Path, shim: Path) -> None:
    _installed(repo)
    _commit(repo, _env(shim, session=SID))  # no capture state at all
    assert "Openshard-Agent" not in _msg(repo)
    _observe(repo, sid="ffffffff-0000-4000-8000-000000000000")  # a different session
    _commit(repo, _env(shim, session=SID))
    assert "Openshard-Agent" not in _msg(repo)


def test_ended_session_adds_nothing_and_queued_session_counts(repo: Path, shim: Path) -> None:
    _installed(repo)
    _observe(repo, ended={"reason": "exit", "at": "2026-10-21T00:00:00Z"})
    _commit(repo, _env(shim, session=SID))
    assert "Openshard-Agent" not in _msg(repo)
    (repo / ".openshard" / "claude_sessions" / f"{SID}.json").unlink()
    (repo / ".openshard" / "claude_sessions" / f"{SID}.queue.jsonl").write_text("{}\n")
    _commit(repo, _env(shim, session=SID))
    assert TRAILER in _msg(repo)


def test_cloud_session_is_left_to_claude_session_trailer(repo: Path, shim: Path) -> None:
    _installed(repo)
    _observe(repo)
    _commit(repo, _env(shim, session=SID, CLAUDE_CODE_REMOTE="true"))
    assert "Openshard-Agent" not in _msg(repo)


def test_symlinked_capture_state_is_not_evidence(repo: Path, shim: Path, tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("symlinks need privileges on Windows")
    elsewhere = tmp_path / "elsewhere"
    _observe(elsewhere)
    (repo / ".openshard").symlink_to(elsewhere / ".openshard", target_is_directory=True)
    _installed(repo)
    _commit(repo, _env(shim, session=SID))
    assert "Openshard-Agent" not in _msg(repo)


def test_session_observed_in_a_sibling_worktree_counts(repo: Path, shim: Path, tmp_path: Path) -> None:
    _installed(repo)
    _observe(repo)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    _commit(wt, _env(shim, session=SID))
    assert TRAILER in _msg(wt)


# -- amend / merge / rebase --------------------------------------------------


def test_amend_never_duplicates(repo: Path, shim: Path) -> None:
    _installed(repo)
    _observe(repo)
    agent = _env(shim, session=SID)
    _commit(repo, agent)
    _git(repo, "commit", "-q", "--amend", "--no-edit", env=agent)
    assert _msg(repo).count("Openshard-Agent:") == 1
    _git(repo, "commit", "-q", "--amend", "-m", "reworded", env=_env(shim))  # a human rewords it
    assert "Openshard-Agent" not in _msg(repo)  # -m replaces the message; the human commit is unmarked
    _git(repo, "commit", "-q", "--amend", "--no-edit", env=agent)
    assert _msg(repo).count("Openshard-Agent:") == 1


def test_merge_commit_in_session_is_marked_once(repo: Path, shim: Path) -> None:
    _installed(repo)
    _observe(repo)
    agent = _env(shim, session=SID)
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, agent, "feature work", name="f.txt")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, _env(shim), "human work", name="h.txt")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "feature", env=agent)
    assert _msg(repo).count("Openshard-Agent:") == 1
    assert len(_git(repo, "log", "-1", "--format=%P").stdout.split()) == 2


def test_rebase_keeps_marks_without_duplicates_and_never_marks_replayed_human_commits(repo: Path, shim: Path) -> None:
    _installed(repo)
    _observe(repo)
    agent, human = _env(shim, session=SID), _env(shim)
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, human, "human on feature", name="h.txt")
    _commit(repo, agent, "agent on feature", name="f.txt")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, human, "main moves", name="m.txt")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "rebase", "-q", "main", env=agent)  # the agent rebases, replaying a human commit
    assert _msg(repo, "HEAD").count("Openshard-Agent:") == 1
    assert "Openshard-Agent" not in _msg(repo, "HEAD~1")
    _git(repo, "checkout", "-q", "-b", "other", "main")
    _commit(repo, human, "human elsewhere", name="o.txt")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "cherry-pick", "other", env=agent)  # a human commit picked by the agent stays unmarked
    assert "Openshard-Agent" not in _msg(repo)


# -- coexistence -------------------------------------------------------------


def test_existing_hook_is_chained_still_runs_and_keeps_its_exit_status(repo: Path, shim: Path) -> None:
    hooks = repo / ".git" / "hooks"
    user_hook = hooks / "prepare-commit-msg"
    user_hook.write_bytes(b'#!/bin/sh\necho "user-hook-ran $2" >> "$(git rev-parse --git-dir)/user-hook.log"\n'
                          b'echo "User-Line: kept" >> "$1"\n')
    user_hook.chmod(0o755)
    status = act.install_commit_hook(repo)
    assert status.state == "chained"
    assert (hooks / "prepare-commit-msg.pre-openshard").read_text().startswith("#!/bin/sh\necho \"user-hook-ran")
    _observe(repo)
    _commit(repo, _env(shim, session=SID))
    msg = _msg(repo)
    assert "User-Line: kept" in msg and TRAILER in msg
    assert "user-hook-ran" in (repo / ".git" / "user-hook.log").read_text()
    assert act.install_commit_hook(repo).state == "already_installed"
    # The user's hook still decides: if it fails, the commit fails as before.
    (hooks / "prepare-commit-msg.pre-openshard").write_bytes(b"#!/bin/sh\nexit 3\n")
    (repo / "c.txt").write_text("c\n")
    _git(repo, "add", "c.txt")
    assert _git(repo, "commit", "-q", "-m", "blocked by user hook", env=_env(shim), check=False).returncode != 0
    # Uninstall puts the user's hook back exactly where it was.
    assert act.uninstall_commit_hook(repo).state == "uninstalled"
    assert user_hook.read_bytes() == b"#!/bin/sh\nexit 3\n"
    assert not (hooks / "prepare-commit-msg.pre-openshard").exists()


def test_hooks_path_and_husky_are_detected_and_never_written(repo: Path) -> None:
    (repo / ".husky" / "_").mkdir(parents=True)
    _git(repo, "config", "core.hooksPath", ".husky/_")
    status = act.install_commit_hook(repo)
    assert status.state == "manual" and "husky" in status.detail
    assert status.instruction == "echo 'openshard hooks git-commit-msg \"$1\" \"$2\" \"$3\" || true' >> .husky/prepare-commit-msg"
    assert not any((repo / ".husky").rglob("prepare-commit-msg"))
    assert act.commit_hook_status(repo).state == "manual"
    _git(repo, "config", "core.hooksPath", "/srv/shared-hooks")
    status = act.install_commit_hook(repo)
    assert status.state == "manual" and "core.hooksPath" in status.detail
    assert status.instruction and status.instruction.endswith('|| true')
    assert not (repo / ".git" / "hooks" / "prepare-commit-msg").exists()


@pytest.mark.parametrize("body, owner", [
    (b"#!/bin/sh\n# lefthook generated\ncall_lefthook run prepare-commit-msg \"$@\"\n", "lefthook"),
    (b"#!/usr/bin/env bash\n# File generated by pre-commit: https://pre-commit.com\nexec pre-commit\n", "pre-commit"),
])
def test_hook_managers_owning_the_hook_get_instructions(repo: Path, body: bytes, owner: str) -> None:
    hook = repo / ".git" / "hooks" / "prepare-commit-msg"
    hook.write_bytes(body)
    status = act.install_commit_hook(repo)
    assert status.state == "manual" and owner in status.detail
    assert status.instruction and "openshard hooks git-commit-msg" in status.instruction
    assert hook.read_bytes() == body


def test_openshard_failure_never_blocks_a_commit(repo: Path, shim: Path) -> None:
    _installed(repo)
    _observe(repo)
    (shim / "openshard").write_bytes(b"#!/bin/sh\necho boom >&2\nexit 7\n")
    _commit(repo, _env(shim, session=SID))
    assert "Openshard-Agent" not in _msg(repo)
    # Not on PATH at all.
    (shim / "openshard").unlink()
    env = _env(shim, session=SID)
    env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if not (Path(p) / "openshard").exists())
    _commit(repo, env)
    # And the Python side swallows everything.
    assert act.mark_commit_message(repo / "missing-file", repo, {"CLAUDE_CODE_SESSION_ID": SID}) is None


# -- opt-out -----------------------------------------------------------------


def test_opt_out_by_git_config_and_environment(repo: Path, shim: Path) -> None:
    _installed(repo)
    _observe(repo)
    _commit(repo, _env(shim, session=SID, **{act.ENV_OPT_OUT: "off"}))
    assert "Openshard-Agent" not in _msg(repo)
    assert act.set_opt_out(repo)
    _commit(repo, _env(shim, session=SID))
    assert "Openshard-Agent" not in _msg(repo)
    assert act.install_commit_hook(repo).state == "opted_out"
    assert act.commit_hook_status(repo).state == "opted_out"


def test_setup_installs_by_default_and_opt_out_removes_hook_and_persists(repo: Path) -> None:
    from types import SimpleNamespace

    result = SimpleNamespace(repo_root=repo, next_steps=[], commit_trailers=None)
    act.apply_to_setup(result)
    assert result.commit_trailers["state"] == "installed" and result.next_steps == []
    lines: list[str] = []
    act.echo_setup_line(result, lines.append)
    assert lines == ["  Commit trailers: installed"]
    act.opt_out_from_setup(result, True)
    act.apply_to_setup(result)
    assert result.commit_trailers["state"] == "opted_out"
    assert not (repo / ".git" / "hooks" / "prepare-commit-msg").exists()
    assert _git(repo, "config", "--get", act.CONFIG_KEY).stdout.strip() == "false"
    act.apply_to_setup(result)  # a later plain `openshard setup` keeps the opt-out
    assert result.commit_trailers["state"] == "opted_out"
    assert act.doctor_rows(act.commit_hook_status(repo)) == [
        ("Agent commit trailers (off: opted out)", True, act.commit_hook_status(repo).detail)]


def test_setup_under_a_hook_manager_adds_the_exact_line_to_next_steps(repo: Path) -> None:
    from types import SimpleNamespace

    _git(repo, "config", "core.hooksPath", ".husky/_")
    result = SimpleNamespace(repo_root=repo, next_steps=[], commit_trailers=None)
    act.apply_to_setup(result)
    assert result.commit_trailers["state"] == "manual"
    assert len(result.next_steps) == 1 and act.MANUAL_LINE in result.next_steps[0]
    label, ok, detail = act.doctor_rows(act.commit_hook_status(repo))[0]
    assert label == "Agent commit trailers" and not ok and act.MANUAL_LINE in detail
    assert act.apply_to_setup(SimpleNamespace(repo_root=None)) is None
    assert act.doctor_rows(act.commit_hook_status(None)) == []


# -- status / script ---------------------------------------------------------


def test_status_reports_missing_with_how_to_enable(repo: Path) -> None:
    status = act.commit_hook_status(repo)
    assert status.state == "missing"
    assert "openshard setup" in status.detail and "--no-commit-trailers" in status.detail
    assert act.commit_hook_status(None).state == "not_git"


def test_hook_script_is_posix_sh_with_lf_and_cheap_for_humans() -> None:
    script = act.hook_script()
    assert script.startswith("#!/bin/sh\n") and "\r" not in script
    assert act.HOOK_MARKER in script
    # The CLI only starts when a known agent session variable is set.
    assert '[ -n "${CLAUDE_CODE_SESSION_ID:-}" ]' in script
    assert script.rstrip().endswith("exit 0")


def test_install_is_idempotent_and_updates_an_old_openshard_hook(repo: Path) -> None:
    path = _installed(repo)
    assert act.install_commit_hook(repo).state == "already_installed"
    path.write_text(f"#!/bin/sh\n{act.HOOK_MARKER}\nexit 0\n")
    assert act.install_commit_hook(repo).state == "updated"
    assert path.read_text() == act.hook_script()


def test_entrypoint_fast_path_marks_the_message(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openshard.cli import entrypoint

    _observe(repo)
    msg = repo / "MSG"
    msg.write_text("subject\n")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv(act.ENV_OPT_OUT, raising=False)
    assert entrypoint._try_git_hook(["hooks", "git-commit-msg", str(msg), "message"]) is True
    assert msg.read_text().count(TRAILER) == 1
    assert entrypoint._try_git_hook(["hooks", "git-commit-msg", str(msg)]) is True
    assert msg.read_text().count(TRAILER) == 1
