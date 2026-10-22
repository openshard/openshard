"""Setup / login replace an out-of-date capture service, and `capture status` shows what is running.

Dogfood defect: after upgrading Core from git main, `openshard setup` and
`openshard login` printed "Capture: running" for the older service (main and
the release both said 0.4.18), so the next receipt still lacked the upgrade's
line counts and signing until `capture stop` + `start`.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openshard import __version__
from openshard.adapters import capture_refresh as refresh
from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_capture_service as svc
from openshard.adapters.claude_setup import SetupResult
from tests.capture_fixtures import _Service, capture_env, service  # noqa: F401  (fixtures)


def _doc(**fields) -> dict:
    return {"service": client.SERVICE_NAME, "version": __version__, **fields}


class TestBuildIdentity:
    """The version string alone cannot tell main from the release it follows."""

    def test_same_version_without_a_build_field_predates_this_code(self):
        assert client.service_version_stale(_doc()) is True

    def test_same_version_and_build_is_current(self, monkeypatch):
        monkeypatch.setattr(client, "installed_build", lambda: "abc123")
        assert client.service_version_stale(_doc(build="abc123")) is False

    def test_same_version_other_build_is_stale(self, monkeypatch):
        monkeypatch.setattr(client, "installed_build", lambda: "abc123")
        assert client.service_version_stale(_doc(build="def456")) is True

    def test_an_unknown_build_on_either_side_is_never_stale(self, monkeypatch):
        monkeypatch.setattr(client, "installed_build", lambda: "abc123")
        assert client.service_version_stale(_doc(build=None)) is False
        monkeypatch.setattr(client, "installed_build", lambda: None)
        assert client.service_version_stale(_doc(build="def456")) is False

    def test_versions_still_decide_first(self):
        assert client.service_version_stale(_doc(version="0.0.1", build=client.installed_build())) is True
        assert client.service_version_stale(_doc(version="unknown")) is False

    def test_the_service_reports_its_build(self, service, capture_env):  # noqa: F811
        doc = client.health(service.port, env=capture_env, credential=True)
        assert "build" in doc and doc["build"] == client.installed_build()
        assert svc.service_status(capture_env)["build"] == client.installed_build()
        assert client.service_version_stale(doc) is False


class TestEnsureCurrentService:
    def test_a_current_service_is_left_running(self, service, capture_env):  # noqa: F811
        result = refresh.ensure_current_service(capture_env)
        assert result == {"state": "running", "port": service.port}
        assert not service.server.shutdown_requested.is_set()

    def test_an_out_of_date_service_is_drained_and_replaced(self, service, capture_env, monkeypatch):  # noqa: F811
        monkeypatch.setattr(svc, "_version", lambda: "0.0.1")
        started: list[_Service] = []

        def _start(env):
            assert client.health(service.port) is None  # the old one left before the new one starts
            monkeypatch.setattr(svc, "_version", lambda: __version__)
            started.append(_Service(env))
            return started[0].port, "started"

        monkeypatch.setattr(client, "ensure_service", _start)
        try:
            result = refresh.ensure_current_service(capture_env)
            assert service.server.shutdown_requested.is_set()
            assert result["state"] == "restarted" and result["previous_version"] == "0.0.1"
            assert result["port"] == started[0].port
            line = refresh.setup_service_line(result)
            assert line == f"restarted (was 0.0.1; 127.0.0.1:{started[0].port})"
        finally:
            for new in started:
                new.stop()

    def test_a_service_that_does_not_exit_is_reported_with_the_command(self, service, capture_env, monkeypatch):  # noqa: F811
        monkeypatch.setattr(svc, "_version", lambda: "0.0.1")
        monkeypatch.setattr(client, "request_shutdown", lambda env, wait_seconds: False)
        monkeypatch.setattr(client, "ensure_service", lambda env: pytest.fail("must not start a second service"))
        result = refresh.ensure_current_service(capture_env)
        assert result["state"] == "restart_failed" and result["previous_version"] == "0.0.1"
        line = refresh.setup_service_line(result)
        assert "could not restart it" in line and refresh.RESTART_COMMAND in line

    def test_another_installations_service_is_never_stopped(self, service, capture_env, tmp_path, monkeypatch):  # noqa: F811
        from openshard.adapters import capture_auth

        monkeypatch.setattr(svc, "_version", lambda: "0.0.1")
        other = {**capture_env, "OPENSHARD_HOME": str(tmp_path / "other-home")}
        Path(other["OPENSHARD_HOME"]).mkdir(parents=True)
        capture_auth.ensure_token(other)
        Path(client.state_path(other)).write_text(json.dumps({"port": service.port}), encoding="utf-8")
        result = refresh.ensure_current_service(other)
        assert result["state"] == "unavailable" and "previous_version" not in result
        assert not service.server.shutdown_requested.is_set()

    def test_setup_and_login_share_it(self):
        from openshard.adapters import claude_setup

        with patch.object(refresh, "ensure_current_service", return_value={"state": "running", "port": 1}) as fn:
            assert claude_setup.ensure_capture_service() == {"state": "running", "port": 1}
        fn.assert_called_once_with()

    def test_an_earlier_build_of_the_same_version_says_so(self):
        line = refresh.setup_service_line({"state": "restarted", "port": 47811, "previous_version": __version__})
        assert line == f"restarted (was an earlier {__version__} build; 127.0.0.1:47811)"


class TestCaptureStatusVersion:
    def test_status_shows_the_running_version(self, service, capture_env):  # noqa: F811
        from openshard.cli.main import cli

        result = CliRunner().invoke(cli, ["capture", "status"], env=dict(capture_env))
        assert result.exit_code == 0, result.output
        assert f"  version:  {refresh._label(__version__, client.installed_build())}\n" in result.output
        assert "MISMATCH" not in result.output

    def test_a_mismatch_is_flagged(self, service, capture_env, monkeypatch):  # noqa: F811
        from openshard.cli.main import cli

        monkeypatch.setattr(svc, "_version", lambda: "0.0.1")
        result = CliRunner().invoke(cli, ["capture", "status"], env=dict(capture_env))
        assert f"  version:  0.0.1 -- MISMATCH: installed Core is {__version__}" in result.output

    def test_a_same_version_build_mismatch_names_both_builds(self, monkeypatch):
        monkeypatch.setattr(client, "installed_build", lambda: "new111")
        status = {"running": True, "credential": "accepted", "stale_version": True,
                  "version": __version__, "build": "old000"}
        assert refresh.status_version_line(status) == (
            f"  version:  {__version__} (build old000) -- MISMATCH: installed Core is {__version__} (build new111)"
        )
        assert f"running OpenShard {__version__} (build old000) while" in refresh.stale_detail(status)
        no_build = {**status, "build": None}
        assert f"running OpenShard an earlier {__version__} build while" in refresh.stale_detail(no_build)


class TestSetupOutput:
    def _render(self, result) -> str:
        from openshard.cli.setup_output import _render_readiness, render_components
        from openshard.onboarding.platform_link import KIND_NONE, PlatformLinkStatus

        link = PlatformLinkStatus(KIND_NONE, "none", "not linked; Receipts stay on this machine")
        with patch("openshard.cli.main._telemetry_status", return_value={}), \
             patch("openshard.cli.main._telemetry_line", return_value="off"), \
             patch("openshard.cli.setup_output.click.echo") as echo:
            render_components(result, link)
            _render_readiness(result, link)
        return "\n".join(str(c.args[0]) for c in echo.call_args_list if c.args)

    def _result(self, **fields) -> SetupResult:
        base: dict = dict(
            repo_root=Path("/r"), is_git=False, claude_cli=SimpleNamespace(available=False, path=None),
            history_path=Path("/r/.openshard/runs.jsonl"), history_writable=True,
            mcp=None, hooks=None, statusline=None, readiness="ready",
        )
        base.update(fields)
        return SetupResult(**base)

    def test_a_restart_is_reported(self):
        out = self._render(self._result(capture_service={"state": "restarted", "port": 47811,
                                                          "previous_version": "0.0.1"}))
        assert "  Capture:       restarted (was 0.0.1; 127.0.0.1:47811)" in out

    def test_the_hermes_hint_is_shown_once(self):
        from openshard.adapters.agent_setup import _OPT_IN_MESSAGES, AGENT_HERMES, AgentSetupResult

        hermes = AgentSetupResult(AGENT_HERMES, True, "/bin/hermes", "skipped_optin", _OPT_IN_MESSAGES[AGENT_HERMES],
                                  next_steps=[_OPT_IN_MESSAGES[AGENT_HERMES]])
        result = self._result(agents={"hermes": hermes},
                              next_steps=[_OPT_IN_MESSAGES[AGENT_HERMES], "Something else to do."])
        out = self._render(result)
        assert out.count("openshard capture install hermes") == 1
        assert "Something else to do." in out
        assert _OPT_IN_MESSAGES[AGENT_HERMES] in result.to_dict()["next_steps"]  # JSON keeps it
