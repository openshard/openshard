"""A Claude HTTP hook keeps recording after the session leaves its project root.

Claude Code interpolates ``$CLAUDE_PROJECT_DIR`` in HTTP hook headers to an
empty string (the variable exists only in command-hook environments), so the
service only ever sees the payload's ``cwd``. Once a session enters a linked
worktree or another checkout, that ``cwd`` resolves to a different repository
and the project's capability no longer verifies there: before this fix every
such event was refused with 401 and lost, while the async watchdog -- which
resolves the root from ``CLAUDE_PROJECT_DIR`` -- judged the HTTP path healthy
and forwarded nothing. The service now checks the capability against the
project it can belong to (the session's root, a worktree's main checkout,
recently recorded repositories) and records under that project, exactly as
the command path does. A capability for an unrelated repository stays refused.
"""

# ruff: noqa: F811 -- pytest fixtures are re-exported by import from the fixtures module
from __future__ import annotations

import json
from pathlib import Path

from openshard.adapters import capture_auth as auth
from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_capture_service as svc
from openshard.adapters.claude_hooks import HookPayload, resolve_repo_root
from tests.capture_fixtures import (
    SID,
    _git,
    _make_repo,
    _payload,
    _post,
    _Service,
    _session_dir,
    _status_payload,
    _wait_for,
)

_EMPTY_PROJECT_DIR = ""  # what Claude Code sends for "$CLAUDE_PROJECT_DIR" in an HTTP hook header


def _capability(env: dict, root: Path) -> str:
    token = auth.load_token(env) or auth.ensure_token(env)
    assert token is not None
    return auth.repo_capability(token, root, "claude_code")


def _http_hook(port: int, body: bytes, capability: str) -> int:
    """POST exactly what Claude Code's configured HTTP hook sends: capability, empty project dir."""
    result = client._request(
        "POST", port, client.HOOK_PATH, body,
        {auth.TOKEN_HEADER: capability, client.PROJECT_DIR_HEADER: _EMPTY_PROJECT_DIR},
    )
    assert result is not None, "service did not answer"
    return result[0]


def _linked_worktree(repo: Path, tmp_path: Path) -> Path:
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "wt-branch")
    return worktree


def _no_evidence(root: Path) -> bool:
    return not (root / ".openshard").exists()


def _tool_calls(root: Path) -> int | None:
    """Tool calls folded into the session's staging buffer under *root* (runs.jsonl waits for a turn)."""
    path = _session_dir(root) / f"{SID}.json"
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("tool_call_count") or 0)
    except (OSError, ValueError):
        return None


def _open_session(port: int, repo: Path) -> None:
    # SessionStart is a command hook: it arrives with the token and the project directory.
    assert _post(port, _payload("SessionStart", repo, source="startup"), project_dir=str(repo))


class TestServiceResolvesTheProject:
    def test_http_hook_from_a_linked_worktree_is_accepted_and_recorded_in_the_worktree(self, service, capture_env, repo, tmp_path):
        worktree = _linked_worktree(repo, tmp_path)
        assert resolve_repo_root(HookPayload(event="PostToolUse", session_id=SID, cwd=str(worktree)), {}) == worktree
        _open_session(service.port, repo)

        status = _http_hook(
            service.port,
            _payload("PostToolUse", worktree, tool_name="Bash", tool_input={"command": "pytest"}, tool_response={}),
            _capability(capture_env, repo),
        )

        # The project's capability authorises the session; the event is recorded where it happened.
        assert status == 200
        assert service.server.recorder.wait_idle(10)
        assert _wait_for(lambda: _tool_calls(worktree) == 1)
        assert _tool_calls(repo) in (None, 0)

    def test_http_hook_from_another_checkout_is_accepted_and_recorded_there(self, service, capture_env, repo, tmp_path):
        other = _make_repo(tmp_path / "other")
        _open_session(service.port, repo)

        status = _http_hook(
            service.port,
            _payload("PostToolUse", other, tool_name="Write", tool_input={"file_path": "x.py"}, tool_response={}),
            _capability(capture_env, repo),
        )

        assert status == 200
        assert service.server.recorder.wait_idle(10)
        assert _wait_for(lambda: _tool_calls(other) == 1)
        assert _tool_calls(repo) in (None, 0)

    def test_a_restarted_service_finds_the_project_among_recent_repositories(self, capture_env, repo, tmp_path):
        # No session was opened on this instance; the project is only known from the state file.
        other = _make_repo(tmp_path / "other")
        token = auth.ensure_token(capture_env)
        capability = auth.repo_capability(token, repo, "claude_code")
        recorder = svc.CaptureRecorder(instance_id="t", state={"recent_repos": [str(repo)]})

        def authorize(root: Path) -> bool:
            return auth.verify_presented(capability, token, root, "claude_code") is not None

        action, _ = recorder.record_hook(
            json.loads(_payload("PostToolUse", other, tool_name="Bash", tool_input={"command": "ls"}, tool_response={})),
            project_dir=None, authorize=authorize,
        )

        # Authorised through the project; recorded in the checkout the event touched.
        assert action == "queued"
        assert list(_session_dir(other).glob("*.queue*.jsonl"))
        assert _no_evidence(repo)

    def test_a_cleanly_stopped_service_still_recognises_its_projects_after_a_restart(self, capture_env, repo, tmp_path):
        # Seen live: an agent restarted the service from a Core worktree while its
        # session belonged to the Platform project. The stop removed the state file
        # and its repository list, so the next HTTP hook from the worktree was
        # refused with 401 and lost.
        other = _make_repo(tmp_path / "other")
        first = _Service(capture_env)
        assert _post(first.port, _payload("SessionStart", repo, source="startup"), project_dir=str(repo))
        assert first.server.recorder.wait_idle(10)
        first.stop()
        assert not Path(client.state_path(capture_env)).exists()

        second = _Service(capture_env)
        try:
            status = _http_hook(
                second.port,
                _payload("PostToolUse", other, tool_name="Bash",
                         tool_input={"command": "ls"}, tool_response={}),
                _capability(capture_env, repo),
            )
            assert status == 200
            assert second.server.recorder.wait_idle(10)
            assert _wait_for(lambda: _tool_calls(other) == 1)
        finally:
            second.stop()

    def test_a_status_ping_teaches_a_restarted_service_the_session_project(self, capture_env, repo, tmp_path):
        """After a restart nothing links the session to its project until a command hook (the status line) says so."""
        other = _make_repo(tmp_path / "other")
        token = auth.ensure_token(capture_env)
        capability = auth.repo_capability(token, repo, "claude_code")
        recorder = svc.CaptureRecorder(instance_id="t2", state={})

        def authorize(root: Path) -> bool:
            return auth.verify_presented(capability, token, root, "claude_code") is not None

        tool_event = json.loads(_payload("PostToolUse", other, tool_name="Bash", tool_input={"command": "ls"}, tool_response={}))
        assert recorder.record_hook(tool_event, project_dir=None, authorize=authorize) == ("rejected", "unauthenticated")
        # The status line arrives with CLAUDE_PROJECT_DIR and the token.
        ping = json.loads(_status_payload(repo))
        assert recorder.record_status(ping, project_dir=str(repo), authorize=lambda root: True)[0] == "queued"
        assert recorder.record_hook(tool_event, project_dir=None, authorize=authorize) == ("queued", "PostToolUse")
        assert list(_session_dir(other).glob("*.queue*.jsonl"))

    def test_a_capability_for_an_unrelated_repository_is_still_refused(self, service, capture_env, repo, tmp_path):
        other = _make_repo(tmp_path / "other")
        # The service has never seen `repo`: nothing ties the credential to a project.
        status = _http_hook(
            service.port,
            _payload("PostToolUse", other, tool_name="Bash", tool_input={"command": "ls"}, tool_response={}),
            _capability(capture_env, repo),
        )

        assert status == 401
        assert service.server.recorder.wait_idle(5)
        assert _no_evidence(other) and _no_evidence(repo)
        assert client.health(service.port)["stats"]["rejected"] == 1

    def test_the_token_never_takes_the_fallback_path(self, service, capture_env, repo, tmp_path):
        other = _make_repo(tmp_path / "other")
        _open_session(service.port, repo)
        # The command path (token) records where cwd says, as before: no re-routing.
        assert _post(service.port, _payload("PostToolUse", other, tool_name="Bash", tool_input={"command": "ls"}, tool_response={}))
        assert service.server.recorder.wait_idle(10)
        assert list(_session_dir(other).glob("*.json*"))

    def test_health_advertises_the_behaviour(self, service):
        assert client.CAPABILITY_ROOT_FALLBACK in client.health(service.port)["features"]
        assert client._service_has_feature({"features": ["x"]}, client.CAPABILITY_ROOT_FALLBACK) is False
        assert client._service_has_feature({"version": "0.4.9"}, client.CAPABILITY_ROOT_FALLBACK) is False
        assert client._service_has_feature(None, client.CAPABILITY_ROOT_FALLBACK) is False


def test_a_state_write_survives_a_briefly_locked_file(tmp_path, monkeypatch):
    # Seen live on Windows: "could not update state file: PermissionError" while
    # another process had the file open, so the project list was not saved.
    target = tmp_path / "claude-capture.json"
    real_replace = client.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("in use")
        real_replace(src, dst)

    monkeypatch.setattr(client.os, "replace", flaky_replace)
    svc._write_state(target, {"recent_repos": ["C:/repo"]})
    assert json.loads(target.read_text(encoding="utf-8")) == {"recent_repos": ["C:/repo"]}

    monkeypatch.setattr(client.os, "replace", lambda src, dst: (_ for _ in ()).throw(PermissionError("in use")))
    try:
        svc._write_state(target, {"recent_repos": []})
    except PermissionError:
        pass
    else:
        raise AssertionError("a file that stays locked must still be reported")
    assert not list(tmp_path.glob("*.tmp"))  # no stray temporary file is left behind
