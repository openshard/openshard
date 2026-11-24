"""`openshard setup --everywhere`: one setup for every place a person works with Claude."""

from __future__ import annotations

import zipfile

import pytest
from click.testing import CliRunner

from openshard.adapters import claude_plugin as cp
from openshard.cli import setup_everywhere as se
from openshard.cli.main import cli
from openshard.sync import config as sync_config

ORG = "01ae53cd-d297-4be1-b30a-73f5dce72ff9"


@pytest.fixture
def claude_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    return tmp_path / "claude"


@pytest.fixture
def linked(monkeypatch):
    link = sync_config.PlatformLink(endpoint="https://api.openshard.dev", organisation_id=ORG,
                                    api_key="osk_" + "k" * 32, linked_at=None, source="file")
    monkeypatch.setattr(sync_config, "load_link", lambda *a, **k: link)
    return link


def test_unlinked_it_installs_the_hooks_and_says_what_is_left(claude_dir, monkeypatch):
    monkeypatch.setattr(sync_config, "load_link", lambda *a, **k: None)
    result = CliRunner().invoke(cli, ["setup", "--everywhere"])
    assert result.exit_code == 0, result.output
    assert "hooks claude --scope user" in (claude_dir / "settings.json").read_text(encoding="utf-8")
    assert "Not done: this computer is not linked." in result.output
    assert "Run `openshard login`" in result.output
    assert "Not done: needs the Platform link from step 2." in result.output


def test_linked_with_a_credential_it_writes_a_connected_plugin(claude_dir, linked, tmp_path):
    token = tmp_path / "osc.txt"
    token.write_text("osc_" + "t" * 32 + "\n", encoding="utf-8")
    out = tmp_path / "out" / "openshard.plugin"
    result = CliRunner().invoke(cli, ["setup", "--everywhere", "--token-file", str(token),
                                      "--plugin-path", str(out)])
    assert result.exit_code == 0, result.output
    assert f"Done: linked to organisation {ORG}" in result.output
    assert f"Wrote {out}" in result.output and "Customize -> Plugins" in result.output
    env = zipfile.ZipFile(out).read(cp.CONNECTION_FILE).decode()
    assert f"OPENSHARD_CONNECTED_ORG_ID={ORG}" in env and "osk_" not in env


def test_a_bad_credential_writes_no_plugin(claude_dir, linked, tmp_path):
    token = tmp_path / "osc.txt"
    token.write_text("nope", encoding="utf-8")
    out = tmp_path / "openshard.plugin"
    result = CliRunner().invoke(cli, ["setup", "--everywhere", "--token-file", str(token), "--plugin-path", str(out)])
    assert "Not done: credential is not valid." in result.output
    assert not out.exists()


def test_no_cloud_skips_the_plugin(claude_dir, linked):
    result = CliRunner().invoke(cli, ["setup", "--everywhere", "--no-cloud"])
    assert result.exit_code == 0 and "Skipped (--no-cloud)." in result.output


def test_everywhere_does_not_mix_with_machine_output(claude_dir):
    result = CliRunner().invoke(cli, ["setup", "--everywhere", "--json"])
    assert result.exit_code == 2 and "cannot be combined" in result.output


@pytest.mark.parametrize(("endpoint", "page"), [
    ("https://api.openshard.dev", "https://app.openshard.dev/settings/agents"),
    ("https://api.staging.openshard.dev:8443", "https://app.staging.openshard.dev:8443/settings/agents"),
    ("https://platform.example.com", None),
])
def test_the_agents_page_is_named_only_when_it_can_be_told(endpoint, page):
    assert se.agents_page(endpoint) == page


def test_setup_help_lists_everywhere():
    assert "--everywhere" in CliRunner().invoke(cli, ["setup", "--help"]).output
