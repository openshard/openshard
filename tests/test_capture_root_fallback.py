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
    _session_dir,
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
    def test_http_hook_from_a_linked_worktree_is_recorded_under_the_project(self, service, capture_env, repo, tmp_path):
        worktree = _linked_worktree(repo, tmp_path)
        assert resolve_repo_root(HookPayload(event="PostToolUse", session_id=SID, cwd=str(worktree)), {}) == worktree
        _open_session(service.port, repo)

        status = _http_hook(
            service.port,
            _payload("PostToolUse", worktree, tool_name="Bash", tool_input={"command": "pytest"}, tool_response={}),
            _capability(capture_env, repo),
        )

        assert status == 200
        assert service.server.recorder.wait_idle(10)
        assert _wait_for(lambda: _tool_calls(repo) == 1)
        assert _no_evidence(worktree)

    def test_http_hook_from_another_checkout_follows_the_session_root(self, service, capture_env, repo, tmp_path):
        other = _make_repo(tmp_path / "other")
        _open_session(service.port, repo)

        status = _http_hook(
            service.port,
            _payload("PostToolUse", other, tool_name="Write", tool_input={"file_path": "x.py"}, tool_response={}),
            _capability(capture_env, repo),
        )

        assert status == 200
        assert service.server.recorder.wait_idle(10)
        assert _wait_for(lambda: _tool_calls(repo) == 1)
        assert _no_evidence(other)

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

        assert action == "queued"
        assert list(_session_dir(repo).glob("*.queue*.jsonl"))
        assert _no_evidence(other)

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
