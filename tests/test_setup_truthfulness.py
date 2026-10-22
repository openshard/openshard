"""`openshard setup` and `sync status` say "ready" only when a Receipt from this repository reached the Platform."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from openshard.cli import setup_output
from openshard.onboarding import platform_link as pl
from openshard.remote import spool
from openshard.sync import config, outbox

ENDPOINT = "https://api.example.test"
KEY = "osk_abcdEFGH_0123456789abcdefghijklmnopqrstuvwxyz"
ORG = "0b6c1f7e-2a4d-4c8e-9f10-a1b2c3d4e5f6"
CONNECTED = {"OPENSHARD_CONNECTED_ENDPOINT": ENDPOINT, "OPENSHARD_CONNECTED_ORG_ID": ORG,
             "OPENSHARD_CONNECTED_TOKEN": "proxy-injected"}


@pytest.fixture
def env(tmp_path: Path) -> dict:
    return {"OPENSHARD_HOME": str(tmp_path / "home")}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _synced(root: Path, rid: str = "rcpt_" + "a" * 32, *, endpoint: str = ENDPOINT, org: str = ORG) -> None:
    outbox.put(root, outbox.make_record(rid, outbox.STATE_SYNCED, endpoint=endpoint, organisation_id=org))


def _sessions(monkeypatch, *states: dict) -> None:
    monkeypatch.setattr(spool, "connected_envs", lambda env=None: [{"i": str(i)} for i in range(len(states))])
    monkeypatch.setattr(spool, "read_state", lambda env=None: states[int((env or {}).get("i", 0))])


def test_no_link_means_receipts_stay_on_this_machine(root, env):
    link = pl.platform_link(root, env)
    assert (link.kind, link.delivery) == (pl.KIND_NONE, pl.DELIVERY_NONE)
    assert "stay on this machine" in link.text


def test_an_api_key_link_is_confirmed_only_by_a_receipt_it_delivered(root, env):
    config.save_link(endpoint=ENDPOINT, organisation_id=ORG, api_key=KEY, env=env)
    assert pl.platform_link(root, env).delivery == pl.DELIVERY_NOT_CONFIRMED
    _synced(root, endpoint="https://elsewhere.test")  # synced to another link: not this one's evidence
    assert pl.platform_link(root, env).delivery == pl.DELIVERY_NOT_CONFIRMED
    _synced(root, "rcpt_" + "b" * 32)
    link = pl.platform_link(root, env)
    assert (link.kind, link.delivery) == (pl.KIND_API_KEY, pl.DELIVERY_CONFIRMED)
    assert link.text == "API key link; 1 Receipt delivered"


def test_acknowledged_events_are_said_but_are_not_a_delivered_receipt(root, env, monkeypatch):
    _sessions(monkeypatch, {"acked_seq": 24, "sent": 24})
    link = pl.platform_link(root, {**env, **CONNECTED})
    assert (link.kind, link.delivery) == (pl.KIND_CONNECTED, pl.DELIVERY_NOT_CONFIRMED)
    assert "24 events acknowledged, no Receipt delivered yet" in link.text
    _synced(root)
    link = pl.platform_link(root, {**env, **CONNECTED})
    assert link.delivery == pl.DELIVERY_CONFIRMED and "1 Receipt delivered" in link.text


def test_a_broken_reader_is_unknown_never_confirmed(root, env, monkeypatch):
    config.save_link(endpoint=ENDPOINT, organisation_id=ORG, api_key=KEY, env=env)
    _synced(root)
    monkeypatch.setattr(outbox, "load_outbox", lambda _root: 1 / 0)
    link = pl.platform_link(root, env)
    assert link.delivery == pl.DELIVERY_NOT_CONFIRMED and "could not be checked" in link.text


def _result(hooks: str = "already_installed", readiness: str = "ready") -> SimpleNamespace:
    return SimpleNamespace(readiness=readiness, hooks=SimpleNamespace(status=hooks),
                           configured_agents=lambda: ["claude_code"], next_steps=[], warnings=[])


def _readiness(result, link, monkeypatch) -> str:
    monkeypatch.setattr("openshard.adapters.claude_setup.display_next_steps", lambda _r: [])
    runner = CliRunner()
    with runner.isolation() as (out, _err, *_):
        setup_output._render_readiness(result, link)
    return out.getvalue().decode()


@pytest.mark.parametrize(("link", "ready"), [
    (pl.PlatformLinkStatus(pl.KIND_NONE, pl.DELIVERY_NONE, "x"), False),
    (pl.PlatformLinkStatus(pl.KIND_API_KEY, pl.DELIVERY_NOT_CONFIRMED, "x"), False),
    (pl.PlatformLinkStatus(pl.KIND_CONNECTED, pl.DELIVERY_CONFIRMED, "x"), True),
])
def test_ready_is_said_only_on_a_delivered_receipt(link, ready, monkeypatch):
    out = _readiness(_result(), link, monkeypatch)
    assert ("Openshard is ready." in out) is ready
    assert ("capturing on this machine" in out) is not ready
    assert "OpenShard" not in out
    if link.kind == pl.KIND_NONE:
        assert "openshard login" in out


def test_a_running_session_is_told_to_restart_after_hooks_change(monkeypatch):
    link = pl.PlatformLinkStatus(pl.KIND_NONE, pl.DELIVERY_NONE, "x")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "live")
    assert "start a new session" in _readiness(_result(hooks="installed"), link, monkeypatch)
    assert "start a new session" not in _readiness(_result(hooks="already_installed"), link, monkeypatch)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert "start a new session" not in _readiness(_result(hooks="installed"), link, monkeypatch)


def test_sync_status_without_a_link_points_to_login(root, env, monkeypatch):
    from openshard.cli.main import cli

    monkeypatch.chdir(root)
    out = CliRunner().invoke(cli, ["sync", "status"], env=env)
    assert out.exit_code == 0, out.output
    assert "Receipts stay on this machine" in out.output and "openshard login" in out.output
    as_json = CliRunner().invoke(cli, ["sync", "status", "--json"], env=env)
    assert '"platform_link"' in as_json.output and '"delivery": "none"' in as_json.output
