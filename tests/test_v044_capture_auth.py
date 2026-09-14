"""v0.4.4 Phase 1/3 -- the local capture channel is authenticated.

"Anything that can connect to localhost is trusted" is no longer the
contract. Every ``POST`` must present the per-user capture token (or the
repository-scoped capability derived from it) in ``X-OpenShard-Capture-Token``;
otherwise nothing is recorded. ``/health`` stays unauthenticated and must
not expose anything that authorises a control action.
"""

# ruff: noqa: F811 -- pytest fixtures are re-exported by import from the service test module
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from openshard.adapters import capture_auth as auth
from openshard.adapters import claude_capture_client as client
from openshard.adapters.claude_hooks import resolve_repo_root
from tests.test_claude_capture_service import (  # noqa: F401 - fixtures re-exported for pytest
    SID,
    _first_line,
    _lines,
    _payload,
    _post,
    _session_dir,
    _wait_for,
    capture_env,
    repo,
    service,
)


def _raw_post(port: int, path: str, body: bytes, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    result = client._request("POST", port, path, body, headers)
    assert result is not None, "service did not answer"
    return result


def _no_evidence(repo: Path) -> bool:
    return not list(_session_dir(repo).glob("*.queue*.jsonl")) and not (repo / ".openshard" / "runs.jsonl").exists()


class TestTokenStore:
    def test_token_is_created_locally_random_and_private(self, capture_env, tmp_path):
        path = Path(auth.token_path(capture_env))
        assert not path.exists()
        token = auth.ensure_token(capture_env)
        assert token and auth.is_token(token)
        assert path.exists() and path.read_text(encoding="utf-8").strip() == token
        assert auth.ensure_token(capture_env) == token  # idempotent
        if sys.platform != "win32":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # Never inside the repository.
        assert Path(capture_env["OPENSHARD_HOME"]) in path.parents

    def test_two_tokens_from_two_homes_differ(self, tmp_path):
        a = auth.ensure_token({"OPENSHARD_HOME": str(tmp_path / "a")})
        b = auth.ensure_token({"OPENSHARD_HOME": str(tmp_path / "b")})
        assert a != b

    def test_rotate_invalidates_the_old_token(self, capture_env):
        old = auth.ensure_token(capture_env)
        new = auth.rotate_token(capture_env)
        assert new != old and auth.load_token(capture_env) == new

    def test_repo_capability_is_scoped_and_deterministic(self, capture_env, tmp_path):
        token = auth.ensure_token(capture_env)
        cap_a = auth.repo_capability(token, tmp_path / "a")
        cap_b = auth.repo_capability(token, tmp_path / "b")
        assert cap_a != cap_b and cap_a == auth.repo_capability(token, tmp_path / "a")
        assert cap_a != token
        assert auth.verify_presented(cap_a, token, tmp_path / "a") == "repo"
        assert auth.verify_presented(cap_a, token, tmp_path / "b") is None
        assert auth.verify_presented(token, token, tmp_path / "b") == "token"
        assert auth.verify_presented("", token, tmp_path / "a") is None
        assert auth.verify_presented(None, token, tmp_path / "a") is None


class TestHookAuthentication:
    def test_unauthenticated_hook_is_rejected_and_records_nothing(self, service, repo):
        status, _ = _raw_post(
            service.port, client.HOOK_PATH,
            _payload("UserPromptSubmit", repo, prompt="forged"),
            {client.PROJECT_DIR_HEADER: str(repo)},
        )
        assert status == 401
        assert service.server.recorder.wait_idle(5)
        assert _no_evidence(repo)
        doc = client.health(service.port)
        assert doc["stats"]["rejected"] == 1
        assert doc["stats"]["queued"] == 0

    def test_malformed_token_is_rejected_and_records_nothing(self, service, repo):
        for bad in ("", "nope", "r1.deadbeef", auth.ensure_token(service.env)[:-1] + "0", "x" * 4000):
            status, _ = _raw_post(
                service.port, client.HOOK_PATH,
                _payload("UserPromptSubmit", repo, prompt="forged"),
                {client.PROJECT_DIR_HEADER: str(repo), auth.TOKEN_HEADER: bad},
            )
            assert status == 401, bad
        assert service.server.recorder.wait_idle(5)
        assert _no_evidence(repo)
        assert client.health(service.port)["stats"]["rejected"] == 5

    def test_authenticated_integration_request_succeeds(self, service, repo):
        # The client helpers present the token automatically.
        assert _post(service.port, _payload("SessionStart", repo, source="startup"), project_dir=str(repo))
        assert _post(service.port, _payload("UserPromptSubmit", repo, prompt="real work"), project_dir=str(repo))
        assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["task"] == "real work")
        assert client.health(service.port)["stats"]["rejected"] == 0

    def test_repo_scoped_capability_authorises_only_its_repository(self, service, repo, tmp_path):
        token = auth.ensure_token(service.env)
        root = resolve_repo_root(
            __import__("openshard.adapters.claude_hooks", fromlist=["HookPayload"]).HookPayload(
                event="SessionStart", session_id=None, cwd=str(repo)), {})
        cap = auth.repo_capability(token, root)
        status, _ = _raw_post(
            service.port, client.HOOK_PATH,
            _payload("UserPromptSubmit", repo, prompt="scoped ok"),
            {client.PROJECT_DIR_HEADER: str(repo), auth.TOKEN_HEADER: cap},
        )
        assert status == 200
        assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["task"] == "scoped ok")
        # The same capability presented for a different repository is refused.
        from tests.test_claude_capture_service import _make_repo

        other = _make_repo(tmp_path / "other")
        status, _ = _raw_post(
            service.port, client.HOOK_PATH,
            _payload("UserPromptSubmit", other, prompt="forged elsewhere"),
            {client.PROJECT_DIR_HEADER: str(other), auth.TOKEN_HEADER: cap},
        )
        assert status == 401
        assert service.server.recorder.wait_idle(5)
        assert _no_evidence(other)

    def test_status_endpoint_requires_the_token(self, service, repo):
        body = json.dumps({"session_id": SID, "cwd": str(repo), "model": {"id": "m", "display_name": "M"},
                           "cost": {"total_cost_usd": 1.0}}).encode("utf-8")
        status, _ = _raw_post(service.port, client.STATUS_PATH, body, {client.PROJECT_DIR_HEADER: str(repo)})
        assert status == 401
        assert client.post_status(service.port, body, project_dir=str(repo))

    def test_browser_originated_requests_are_rejected_even_with_a_token(self, service, repo):
        token = auth.ensure_token(service.env)
        for headers in (
            {"Origin": "http://evil.example", auth.TOKEN_HEADER: token},
            {"Origin": "null", auth.TOKEN_HEADER: token},
            {"Referer": "http://evil.example/page", auth.TOKEN_HEADER: token},
            {"Sec-Fetch-Mode": "cors", auth.TOKEN_HEADER: token},
        ):
            status, _ = _raw_post(
                service.port, client.HOOK_PATH, _payload("UserPromptSubmit", repo, prompt="from a page"),
                {client.PROJECT_DIR_HEADER: str(repo), **headers},
            )
            assert status == 403, headers
        assert service.server.recorder.wait_idle(5)
        assert _no_evidence(repo)


class TestControlEndpoints:
    def test_health_exposes_no_credential(self, service):
        token = auth.ensure_token(service.env)
        doc = client.health(service.port)
        blob = json.dumps(doc)
        assert token not in blob
        assert auth.repo_capability(token, Path.cwd()) not in blob
        assert "token" not in {k.lower() for k in doc}

    def test_shutdown_cannot_be_authorised_from_health_alone(self, service):
        doc = client.health(service.port)
        body = json.dumps({"instance_id": doc["instance_id"]}).encode("utf-8")
        status, _ = _raw_post(service.port, client.SHUTDOWN_PATH, body)
        assert status in (401, 403)
        assert client.health(service.port) is not None  # still alive
        assert not service.server.shutdown_requested.is_set()

    def test_repo_capability_does_not_authorise_shutdown(self, service, tmp_path):
        token = auth.ensure_token(service.env)
        doc = client.health(service.port)
        body = json.dumps({"instance_id": doc["instance_id"]}).encode("utf-8")
        status, _ = _raw_post(service.port, client.SHUTDOWN_PATH, body,
                              {auth.TOKEN_HEADER: auth.repo_capability(token, tmp_path)})
        assert status in (401, 403)
        assert not service.server.shutdown_requested.is_set()

    def test_shutdown_with_token_works(self, service):
        assert client.request_shutdown(service.env, wait_seconds=30)
        assert client.health(service.port) is None


class TestTokenNeverLeaks:
    def test_token_is_not_in_telemetry_or_logs_or_state(self, service, repo, capsys):
        token = auth.ensure_token(service.env)
        from openshard.telemetry import schema

        # The telemetry grammar has no free-text property: a token can be neither
        # a valid enum member nor a valid version token.
        with pytest.raises(Exception):
            schema.token(token)
        _post(service.port, _payload("SessionStart", repo, source="startup"), project_dir=str(repo))
        assert service.server.recorder.wait_idle(5)
        state = json.dumps(client.read_state(service.env) or {})
        assert token not in state
        assert token not in json.dumps(client.health(service.port))
        out, err = capsys.readouterr()
        assert token not in out and token not in err
        log = Path(client.log_path(service.env))
        if log.exists():
            assert token not in log.read_text(encoding="utf-8")

    def test_cli_capture_status_never_prints_the_token(self, service):
        from click.testing import CliRunner

        from openshard.cli.main import cli

        token = auth.ensure_token(service.env)
        env = dict(service.env)
        result = CliRunner().invoke(cli, ["capture", "status"], env=env)
        assert result.exit_code == 0, result.output
        assert token not in result.output
        result = CliRunner().invoke(cli, ["capture", "status", "--json"], env=env)
        assert token not in result.output
        result = CliRunner().invoke(cli, ["doctor"], env=env)
        assert token not in result.output


class TestFailOpen:
    def test_agent_hook_entrypoints_exit_cleanly_when_unauthorised(self, service, repo, monkeypatch):
        """The coding agent keeps working: a rejected hook still returns
        normally (Cursor still gets its decision) and the payload falls back
        to the in-process fold, which needs no token."""
        import io

        # The client presents a wrong token (the service keeps the real one).
        monkeypatch.setattr(
            client, "_auth_headers",
            lambda env, project_dir: {client.PROJECT_DIR_HEADER: project_dir or "", auth.TOKEN_HEADER: "0" * 64},
        )
        label = client.run_hook_via_service(
            io.BytesIO(_payload("UserPromptSubmit", repo, prompt="fallback")),
            env={**service.env, "CLAUDE_PROJECT_DIR": str(repo)}, spawn=False,
        )
        assert label != "forwarded"
        assert _wait_for(lambda: (_e := _first_line(repo)) is not None and _e["task"] == "fallback")
        label, reply = client.run_cursor_hook(
            io.BytesIO(json.dumps({"conversation_id": SID, "hook_event_name": "beforeSubmitPrompt",
                                   "prompt": "x", "workspace_roots": [str(repo)]}).encode("utf-8")),
            env=service.env, spawn=False,
        )
        assert reply == client.CURSOR_ALLOW_RESPONSE
