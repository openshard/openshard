"""How a run's usage is paid for (``capture.billing_basis``), from presence checks only.

Every branch runs against a fake environment and a fake home in a tmp dir
(``HOME`` and ``USERPROFILE`` both point at it, so the same cases hold on
Windows). The suite-wide isolation in ``conftest.py`` keeps the developer's
real ``~/.claude`` / ``~/.codex`` out; these tests put the real home lookup
back and give it their own directory.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import jsonschema
import pytest
from click.testing import CliRunner

from openshard.adapters import billing_basis as bb
from openshard.adapters import claude_capture_client as client
from openshard.adapters import claude_hooks as ch
from openshard.adapters.agent_env import (
    agent_environment,
    claude_agent_env,
    codex_agent_env,
    format_agent_env,
    parse_agent_env,
)
from openshard.adapters.billing_basis import _claude_keychain_item as _real_keychain_item
from openshard.adapters.billing_basis import _home as _real_home
from openshard.adapters.claude_hooks import handle_claude_hook, handle_hook
from openshard.cli.main import cli
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.shard_hash import verify_shard_hash
from openshard.history.views import receipt_to_dict
from openshard.sync.envelope import build_envelope
from tests.capture_fixtures import SID, _lines, _make_repo

FIXTURES = Path(__file__).parent / "fixtures"
RECEIPT_SCHEMA = json.loads((FIXTURES / "platform" / "receipt-sync-envelope.v1.json").read_text(encoding="utf-8"))
SECRET = "sk-ant-api03-SENTINEL-must-never-leave-the-env-0123456789"
OAUTH_SECRET = "sk-ant-oat01-SENTINEL-oauth-token-0123456789"


@pytest.fixture(autouse=True)
def real_home_lookup(monkeypatch):
    monkeypatch.setattr(bb, "_home", _real_home)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home dir"  # a space, as Windows profiles often have
    path.mkdir()
    return path


def _env(home: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(home), "USERPROFILE": str(home), **extra}


def _write(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def _claude_login(config_dir: Path, subscription_type: str | None = "max") -> None:
    oauth: dict = {"accessToken": OAUTH_SECRET, "refreshToken": OAUTH_SECRET, "expiresAt": 1}
    if subscription_type is not None:
        oauth["subscriptionType"] = subscription_type
    _write(config_dir / ".credentials.json", {"claudeAiOauth": oauth})


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


class TestClaudeCode:
    @pytest.mark.parametrize(("extra", "expected"), [
        # Nothing observable: unknown, never a default.
        ({}, "unknown"),
        # Cloud provider or gateway accounts bill on terms the hook cannot see.
        ({"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_API_KEY": SECRET, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, "unknown"),
        ({"CLAUDE_CODE_USE_VERTEX": "true"}, "unknown"),
        ({"CLAUDE_CODE_USE_FOUNDRY": "1"}, "unknown"),
        ({"ANTHROPIC_BASE_URL": "https://gateway.example.com", "ANTHROPIC_API_KEY": SECRET,
          "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, "unknown"),
        ({"ANTHROPIC_BASE_URL": "https://api.anthropic.com.evil.example", "ANTHROPIC_API_KEY": SECRET,
          "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, "unknown"),
        # The Anthropic API named explicitly is still the Anthropic API.
        ({"ANTHROPIC_BASE_URL": "https://api.anthropic.com/", "ANTHROPIC_API_KEY": SECRET,
          "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, "api_billed"),
        # Cloud sessions always use the subscription; keys set there do not override it.
        ({"CLAUDE_CODE_REMOTE": "true", "ANTHROPIC_BASE_URL": "https://api.anthropic.com"}, "subscription"),
        ({"CLAUDE_CODE_REMOTE": "true", "ANTHROPIC_API_KEY": SECRET}, "subscription"),
        ({"CLAUDE_CODE_REMOTE": "true", "CLAUDE_CODE_USE_BEDROCK": "1"}, "unknown"),  # conflict
        ({"CLAUDE_CODE_REMOTE": "false"}, "unknown"),
        # A bearer token is a gateway's credential.
        ({"ANTHROPIC_AUTH_TOKEN": SECRET}, "unknown"),
        ({"ANTHROPIC_AUTH_TOKEN": SECRET, "ANTHROPIC_API_KEY": SECRET, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, "unknown"),
        # An API key is always used non-interactively; interactively the user may have declined it.
        ({"ANTHROPIC_API_KEY": SECRET, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, "api_billed"),
        ({"ANTHROPIC_API_KEY": SECRET, "CLAUDE_CODE_ENTRYPOINT": "sdk-py"}, "api_billed"),
        ({"ANTHROPIC_API_KEY": SECRET, "CLAUDE_CODE_ENTRYPOINT": "sdk-ts"}, "api_billed"),
        ({"ANTHROPIC_API_KEY": SECRET, "CLAUDE_CODE_ENTRYPOINT": "cli"}, "unknown"),
        ({"ANTHROPIC_API_KEY": SECRET, "CLAUDE_CODE_ENTRYPOINT": "claude-vscode"}, "unknown"),
        ({"ANTHROPIC_API_KEY": SECRET}, "unknown"),
        ({"ANTHROPIC_API_KEY": "   ", "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, "unknown"),  # blank is absent
        # A long-lived subscription token.
        ({"CLAUDE_CODE_OAUTH_TOKEN": OAUTH_SECRET}, "subscription"),
        ({"CLAUDE_CODE_OAUTH_TOKEN": OAUTH_SECRET, "ANTHROPIC_API_KEY": SECRET}, "unknown"),
        ({"CLAUDE_CODE_OAUTH_TOKEN": OAUTH_SECRET, "ANTHROPIC_API_KEY": SECRET,
          "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, "api_billed"),  # the key outranks it, always used here
    ])
    def test_environment_branches(self, home, extra, expected):
        assert bb.claude_billing_basis(_env(home, **extra)) == expected

    def test_a_claude_ai_login_with_a_plan_is_subscription(self, home):
        _claude_login(home / ".claude")
        assert bb.claude_billing_basis(_env(home)) == "subscription"

    @pytest.mark.parametrize("content", [
        {"claudeAiOauth": {"accessToken": OAUTH_SECRET}},  # no subscriptionType
        {"claudeAiOauth": {"accessToken": OAUTH_SECRET, "subscriptionType": "free-trial-ish"}},
        {"claudeAiOauth": "not an object"},
        {"somethingElse": {}},
        "{not json",
        "[]",
    ])
    def test_a_login_file_without_a_known_plan_is_unknown(self, home, content):
        _write(home / ".claude" / ".credentials.json", content)
        assert bb.claude_billing_basis(_env(home)) == "unknown"

    def test_claude_config_dir_is_where_the_login_lives(self, home, tmp_path):
        config = tmp_path / "work-config"
        _claude_login(config)
        assert bb.claude_billing_basis(_env(home, CLAUDE_CONFIG_DIR=str(config))) == "subscription"
        _claude_login(home / ".claude")
        assert bb.claude_billing_basis(_env(home, CLAUDE_CONFIG_DIR=str(tmp_path / "empty"))) == "unknown"

    @pytest.mark.parametrize("settings_file", ["user", "project", "project_local", "managed"])
    def test_an_api_key_helper_outranks_the_login(self, home, tmp_path, monkeypatch, settings_file):
        project = tmp_path / "project"
        managed = tmp_path / "managed" / "managed-settings.json"
        monkeypatch.setattr(bb, "_MANAGED_SETTINGS", {"darwin": str(managed), "linux": str(managed),
                                                      "win32": str(managed)})
        target = {
            "user": home / ".claude" / "settings.json",
            "project": project / ".claude" / "settings.json",
            "project_local": project / ".claude" / "settings.local.json",
            "managed": managed,
        }[settings_file]
        _write(target, {"apiKeyHelper": "~/bin/get-key.sh"})
        _claude_login(home / ".claude")
        env = _env(home, CLAUDE_PROJECT_DIR=str(project))
        assert bb.claude_billing_basis(env) == "api_billed"
        assert bb.claude_billing_basis({**env, "CLAUDE_CODE_OAUTH_TOKEN": OAUTH_SECRET}) == "api_billed"

    @pytest.mark.parametrize("settings", [{"forceLoginMethod": "gateway"},
                                          {"forceLoginGatewayUrl": "https://gw.example.com"}])
    def test_a_required_gateway_sign_in_is_unknown(self, home, settings):
        _write(home / ".claude" / "settings.json", {**settings, "apiKeyHelper": "x"})
        _claude_login(home / ".claude")
        assert bb.claude_billing_basis(_env(home)) == "unknown"

    @pytest.mark.parametrize("extra", [
        {"ANTHROPIC_PROFILE": "work"},
        {"ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1", "ANTHROPIC_ORGANIZATION_ID": "org_1"},
    ])
    def test_an_anthropic_profile_or_federation_credential_is_unknown(self, home, extra):
        _claude_login(home / ".claude")
        assert bb.claude_billing_basis(_env(home, **extra)) == "unknown"

    def test_a_stored_anthropic_profile_is_unknown(self, home, monkeypatch, tmp_path):
        appdata = tmp_path / "AppData"
        _write(home / ".config" / "anthropic" / "configs" / "default.json", {"auth_mode": "oidc_federation"})
        _write(appdata / "Anthropic" / "configs" / "default.json", {"auth_mode": "oidc_federation"})
        _claude_login(home / ".claude")
        assert bb.claude_billing_basis(_env(home, APPDATA=str(appdata))) == "unknown"

    def test_one_federation_variable_alone_is_not_a_credential(self, home):
        _claude_login(home / ".claude")
        assert bb.claude_billing_basis(_env(home, ANTHROPIC_PROFILE="  ",
                                            ANTHROPIC_FEDERATION_RULE_ID="fdrl_1")) == "subscription"

    def test_no_home_in_the_environment_reads_no_files(self, home, monkeypatch):
        _claude_login(home / ".claude")
        monkeypatch.setattr(Path, "home", lambda: home)  # even a resolvable real home is not used
        assert bb.claude_billing_basis({}) == "unknown"

    def test_never_raises(self):
        assert bb.claude_billing_basis(None) == "unknown"
        assert bb.claude_billing_basis({"HOME": "\x00bad"}) == "unknown"


class TestClaudeInteractiveKey:
    """An interactive session with ``ANTHROPIC_API_KEY``: billed by the key only when no
    subscription login exists to have been used instead."""

    KEY = {"ANTHROPIC_API_KEY": SECRET, "CLAUDE_CODE_ENTRYPOINT": "cli"}

    def _config(self, root: Path, **fields: object) -> None:
        _write(root / ".claude.json", {"numStartups": 3, "primaryApiKey": SECRET, **fields})

    def test_key_and_no_login_is_api_billed(self, home):
        self._config(home)
        assert bb.claude_billing_basis(_env(home, **self.KEY)) == "api_billed"
        # No entrypoint stated at all is interactive too.
        assert bb.claude_billing_basis(_env(home, ANTHROPIC_API_KEY=SECRET)) == "api_billed"

    @pytest.mark.parametrize("subscription_type", ["max", None])
    def test_key_and_a_credentials_login_is_unknown(self, home, subscription_type):
        self._config(home)
        _claude_login(home / ".claude", subscription_type)  # any claudeAiOauth entry counts
        assert bb.claude_billing_basis(_env(home, **self.KEY)) == "unknown"

    def test_key_and_an_oauth_token_is_unknown(self, home):
        self._config(home)
        assert bb.claude_billing_basis(_env(home, **self.KEY, CLAUDE_CODE_OAUTH_TOKEN=OAUTH_SECRET)) == "unknown"

    def test_key_and_an_oauth_account_in_the_global_config_is_unknown(self, home):
        self._config(home, oauthAccount={"accountUuid": "a", "emailAddress": "x@example.com"})
        assert bb.claude_billing_basis(_env(home, **self.KEY)) == "unknown"

    def test_macos_keychain_login_is_seen_through_the_global_config(self, home):
        # macOS keeps the login token in the Keychain (never read), so there is no
        # .credentials.json; Claude Code still writes oauthAccount to ~/.claude.json.
        self._config(home, oauthAccount={"accountUuid": "a"})
        assert not (home / ".claude" / ".credentials.json").exists()
        assert bb.claude_billing_basis(_env(home, **self.KEY)) == "unknown"

    @pytest.mark.parametrize("content", [None, "{not json", "[]"])
    def test_a_missing_or_unreadable_global_config_is_unknown(self, home, content):
        if content is not None:
            _write(home / ".claude.json", content)
        assert bb.claude_billing_basis(_env(home, **self.KEY)) == "unknown"

    def test_an_unreadable_credentials_file_is_not_absence(self, home):
        self._config(home)
        _write(home / ".claude" / ".credentials.json", "{not json")
        assert bb.claude_billing_basis(_env(home, **self.KEY)) == "unknown"

    def test_claude_config_dir_holds_both_files(self, home, tmp_path):
        config = tmp_path / "work-config"
        self._config(config)
        self._config(home, oauthAccount={"accountUuid": "a"})  # a different account's config
        assert bb.claude_billing_basis(_env(home, **self.KEY, CLAUDE_CONFIG_DIR=str(config))) == "api_billed"
        _claude_login(config)
        assert bb.claude_billing_basis(_env(home, **self.KEY, CLAUDE_CONFIG_DIR=str(config))) == "unknown"

    @pytest.mark.parametrize("extra", [{"CLAUDE_CODE_USE_BEDROCK": "1"}, {"ANTHROPIC_AUTH_TOKEN": SECRET},
                                       {"ANTHROPIC_BASE_URL": "https://gateway.example.com"}])
    def test_higher_priority_rules_still_win(self, home, extra):
        self._config(home)
        assert bb.claude_billing_basis(_env(home, **self.KEY, **extra)) == "unknown"
        assert bb.claude_billing_basis(_env(home, **self.KEY, CLAUDE_CODE_REMOTE="true")) == "subscription"

    def test_end_to_end_without_any_secret(self, repo, home, monkeypatch):
        self._config(home)
        entry = _claude_session(repo, _env(home, **self.KEY))
        history, envelope, text = _outputs(repo, entry, monkeypatch)
        assert (entry["capture"]["billing_basis"], history["billing_basis"],
                envelope["receipt"]["billing_basis"]) == ("api_billed",) * 3
        jsonschema.validate(envelope, RECEIPT_SCHEMA)
        for blob in ((repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8"), text, json.dumps(envelope)):
            assert SECRET not in blob


class FakeSecurity:
    """Stands in for ``subprocess.run`` of ``/usr/bin/security``; records every call."""

    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return subprocess.CompletedProcess(args, self.outcome)


class TestMacosKeychain:
    KEY = {"ANTHROPIC_API_KEY": SECRET, "CLAUDE_CODE_ENTRYPOINT": "cli"}

    @pytest.fixture(autouse=True)
    def macos(self, monkeypatch):
        monkeypatch.setattr(bb, "_claude_keychain_item", _real_keychain_item)
        monkeypatch.setattr(bb, "_is_macos", lambda: True)

    def _security(self, monkeypatch, outcome: object) -> FakeSecurity:
        fake = FakeSecurity(outcome)
        monkeypatch.setattr(bb.subprocess, "run", fake)
        return fake

    def _config(self, root: Path, **fields: object) -> None:
        _write(root / ".claude.json", {"numStartups": 1, **fields})

    @staticmethod
    def _assert_existence_only(fake: FakeSecurity, service: str = "Claude Code-credentials") -> None:
        assert fake.calls
        for args, kwargs in fake.calls:
            assert args == ["/usr/bin/security", "find-generic-password", "-s", service]
            assert "-w" not in args and "-g" not in args
            assert kwargs["stdout"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.DEVNULL
            assert kwargs["timeout"] <= 1 and "shell" not in kwargs

    def test_key_and_no_keychain_login_is_api_billed(self, home, monkeypatch):
        fake = self._security(monkeypatch, 44)
        self._config(home)
        assert bb.claude_billing_basis(_env(home, **self.KEY)) == "api_billed"
        self._assert_existence_only(fake)

    def test_key_and_a_keychain_login_is_unknown(self, home, monkeypatch):
        fake = self._security(monkeypatch, 0)
        self._config(home)
        assert bb.claude_billing_basis(_env(home, **self.KEY)) == "unknown"
        self._assert_existence_only(fake)

    @pytest.mark.parametrize("outcome", [
        subprocess.TimeoutExpired(["security"], 1), FileNotFoundError("security"), PermissionError("x"), 36, 51, 1,
    ])
    def test_anything_but_a_clear_answer_is_unknown(self, home, monkeypatch, outcome):
        fake = self._security(monkeypatch, outcome)
        self._config(home)
        assert bb.claude_billing_basis(_env(home, **self.KEY)) == "unknown"
        self._config(home, oauthAccount={"accountUuid": "a"})
        assert bb.claude_billing_basis(_env(home)) == "unknown"
        self._assert_existence_only(fake)

    def test_the_file_checks_still_apply_when_the_keychain_is_empty(self, home, monkeypatch):
        self._security(monkeypatch, 44)
        self._config(home, oauthAccount={"accountUuid": "a"})
        assert bb.claude_billing_basis(_env(home, **self.KEY)) == "unknown"

    def test_a_keychain_login_beside_a_claude_ai_account_is_subscription(self, home, monkeypatch):
        fake = self._security(monkeypatch, 0)
        self._config(home, oauthAccount={"accountUuid": "a"})
        assert bb.claude_billing_basis(_env(home)) == "subscription"
        self._assert_existence_only(fake)

    @pytest.mark.parametrize("fields", [{}, {"oauthAccount": {"accountUuid": "a"}, "primaryApiKey": SECRET}])
    def test_the_item_alone_is_not_a_subscription(self, home, monkeypatch, fields):
        # The same item holds MCP servers' OAuth tokens; a Console key may be the login.
        self._security(monkeypatch, 0)
        self._config(home, **fields)
        assert bb.claude_billing_basis(_env(home)) == "unknown"

    def test_no_keychain_item_and_no_file_login_is_unknown(self, home, monkeypatch):
        self._security(monkeypatch, 44)
        self._config(home, oauthAccount={"accountUuid": "a"})
        assert bb.claude_billing_basis(_env(home)) == "unknown"

    def test_a_config_dir_suffixes_the_name_and_its_absence_proves_nothing(self, home, monkeypatch, tmp_path):
        config = tmp_path / "work-config"
        self._config(config, oauthAccount={"accountUuid": "a"})
        suffix = hashlib.sha256(str(config).encode("utf-8")).hexdigest()[:8]
        fake = self._security(monkeypatch, 0)
        assert bb.claude_billing_basis(_env(home, CLAUDE_CONFIG_DIR=str(config))) == "subscription"
        self._assert_existence_only(fake, f"Claude Code-credentials-{suffix}")
        self._config(config)
        fake = self._security(monkeypatch, 44)
        assert bb.claude_billing_basis(_env(home, **self.KEY, CLAUDE_CONFIG_DIR=str(config))) == "unknown"
        self._assert_existence_only(fake, f"Claude Code-credentials-{suffix}")

    @pytest.mark.parametrize(("value", "service"), [
        ("", "Claude Code-credentials"),
        ("/Users/me/.claude-work",
         "Claude Code-credentials-" + hashlib.sha256(b"/Users/me/.claude-work").hexdigest()[:8]),
    ])
    def test_the_secure_storage_dir_names_the_item_exactly(self, home, monkeypatch, value, service):
        fake = self._security(monkeypatch, 44)
        self._config(home)
        env = _env(home, **self.KEY, CLAUDE_SECURESTORAGE_CONFIG_DIR=value, CLAUDE_CONFIG_DIR=str(home / "x"))
        _write(home / "x" / ".claude.json", {"numStartups": 1})
        assert bb.claude_billing_basis(env) == "api_billed"
        self._assert_existence_only(fake, service)

    def test_never_asked_off_macos(self, home, monkeypatch):
        fake = self._security(monkeypatch, 0)
        monkeypatch.setattr(bb, "_is_macos", lambda: False)
        self._config(home, oauthAccount={"accountUuid": "a"})
        assert bb.claude_billing_basis(_env(home)) == "unknown"
        self._config(home)
        assert bb.claude_billing_basis(_env(home, **self.KEY)) == "api_billed"
        assert fake.calls == []


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def _codex_auth(codex_home: Path, mode: object) -> None:
    _write(codex_home / "auth.json", {"auth_mode": mode, "OPENAI_API_KEY": SECRET,
                                      "tokens": {"access_token": OAUTH_SECRET}})


class TestCodex:
    @pytest.mark.parametrize(("mode", "extra", "expected"), [
        ("apikey", {}, "api_billed"),
        ("chatgpt", {}, "subscription"),
        ("chatgpt", {"OPENAI_API_KEY": SECRET}, "unknown"),  # conflicting signals
        ("chatgpt", {"CODEX_API_KEY": SECRET}, "unknown"),
        ("apikey", {"OPENAI_API_KEY": SECRET}, "api_billed"),
        ("apikey", {"OPENAI_BASE_URL": "https://my-proxy.example.com/v1"}, "unknown"),
        ("apikey", {"OPENAI_BASE_URL": "https://api.openai.com/v1"}, "api_billed"),
        ("chatgptAuthTokens", {}, "unknown"),  # not a documented mode we map
        (None, {}, "unknown"),
        (7, {}, "unknown"),
    ])
    def test_auth_mode(self, home, mode, extra, expected):
        _codex_auth(home / ".codex", mode)
        assert bb.codex_billing_basis(_env(home, **extra)) == expected

    def test_no_login_file_is_unknown_even_with_a_key_in_the_environment(self, home):
        assert bb.codex_billing_basis(_env(home)) == "unknown"
        assert bb.codex_billing_basis(_env(home, OPENAI_API_KEY=SECRET)) == "unknown"

    def test_codex_home_overrides_the_default(self, home, tmp_path):
        _codex_auth(tmp_path / "codex-home", "chatgpt")
        assert bb.codex_billing_basis(_env(home, CODEX_HOME=str(tmp_path / "codex-home"))) == "subscription"
        assert bb.codex_billing_basis(_env(home)) == "unknown"

    @pytest.mark.parametrize(("config", "expected"), [
        ('model = "gpt-5-codex"\n', "subscription"),
        ('model_provider = "openai"\n', "subscription"),
        ('model_provider = "azure"\n', "unknown"),
        ('[profiles.local]\nmodel_provider = "ollama"\n', "unknown"),
        ("this is = = not toml", "unknown"),
    ])
    def test_a_non_openai_model_provider_is_unknown(self, home, config, expected):
        _codex_auth(home / ".codex", "chatgpt")
        _write(home / ".codex" / "config.toml", config)
        assert bb.codex_billing_basis(_env(home)) == expected

    def test_never_raises(self):
        assert bb.codex_billing_basis(None) == "unknown"
        assert bb.codex_billing_basis({}) == "unknown"


# ---------------------------------------------------------------------------
# Transport: agent env dict, client header, service parse
# ---------------------------------------------------------------------------


class TestTransport:
    def test_unknown_is_absent_from_the_agent_env(self, home):
        assert "billing_basis" not in claude_agent_env(_env(home))
        assert codex_agent_env(_env(home)) == {}
        assert agent_environment("cursor", _env(home)) is None
        assert agent_environment("opencode", _env(home)) is None

    def test_header_round_trip_carries_only_the_label(self, home):
        env = _env(home, ANTHROPIC_API_KEY=SECRET, CLAUDE_CODE_ENTRYPOINT="sdk-cli")
        agent_env = claude_agent_env(env)
        header = format_agent_env(agent_env)
        assert agent_env["billing_basis"] == "api_billed"
        assert header == "provider=anthropic;surface=sdk-cli;billing=api_billed"
        assert parse_agent_env(header)["billing_basis"] == "api_billed"
        assert SECRET not in json.dumps(agent_env) and SECRET not in header

    @pytest.mark.parametrize("value", ["billing=free", "billing=unknown", "billing=api_billed;x" * 60, "billing="])
    def test_malformed_or_unknown_values_are_dropped(self, value):
        assert "billing_basis" not in parse_agent_env(value)

    def test_a_codex_payload_takes_billing_and_nothing_claude_specific(self):
        payload = ch.HookPayload(event="SessionStart", session_id=SID, cwd=None, agent="codex")
        ch.apply_agent_env(payload, parse_agent_env("provider=anthropic;surface=cli;billing=subscription"))
        assert (payload.agent_billing_basis, payload.agent_provider, payload.agent_surface) == (
            "subscription", None, None)

    def test_other_agents_take_nothing(self):
        payload = ch.HookPayload(event="SessionStart", session_id=SID, cwd=None, agent="cursor")
        ch.apply_agent_env(payload, {"billing_basis": "api_billed"})
        assert payload.agent_billing_basis is None

    def test_queue_line_round_trip(self, tmp_path):
        payload = ch.HookPayload(event="SessionStart", session_id=SID, cwd=None, agent="claude_code", source="startup")
        payload.agent_billing_basis = "subscription"
        reduced = ch.reduce_hook_payload(payload, tmp_path)
        assert reduced is not None
        line = reduced.to_dict()
        assert line["agent_billing_basis"] == "subscription"
        again = ch.ReducedHookPayload.from_dict(line)
        assert again is not None and again.agent_billing_basis == "subscription"
        assert ch.ReducedHookPayload.from_dict({**line, "agent_billing_basis": "free"}).agent_billing_basis is None
        payload.agent_billing_basis = None
        assert "agent_billing_basis" not in ch.reduce_hook_payload(payload, tmp_path).to_dict()


# ---------------------------------------------------------------------------
# End to end: capture -> runs.jsonl -> history --json -> sync envelope
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "my repo")


def _claude_session(repo: Path, env: dict[str, str]) -> dict:
    base = {"session_id": SID, "cwd": str(repo)}
    for doc in ({**base, "hook_event_name": "SessionStart", "source": "startup"},
                {**base, "hook_event_name": "UserPromptSubmit", "prompt": "Fix the login button"},
                {**base, "hook_event_name": "Stop"},
                {**base, "hook_event_name": "SessionEnd", "reason": "other"}):
        handle_claude_hook(doc, env={"CLAUDE_PROJECT_DIR": str(repo), **env})
    (entry,) = _lines(repo)
    return entry


def _outputs(repo: Path, entry: dict, monkeypatch) -> tuple[dict, dict, str]:
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(cli, ["history", "--json"], catch_exceptions=False)
    history = json.loads(result.output)["shards"][0]
    envelope = build_envelope(entry, 0, core_version="0.0.0-test")
    return history, envelope, result.output


class TestEndToEnd:
    @pytest.mark.parametrize(("extra", "expected"), [
        ({"ANTHROPIC_API_KEY": SECRET, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, "api_billed"),
        ({"CLAUDE_CODE_OAUTH_TOKEN": OAUTH_SECRET}, "subscription"),
    ])
    def test_recorded_projected_and_synced_without_any_secret(self, repo, home, monkeypatch, extra, expected):
        entry = _claude_session(repo, _env(home, **extra))
        assert entry["capture"]["billing_basis"] == expected
        history, envelope, text = _outputs(repo, entry, monkeypatch)
        assert history["billing_basis"] == expected
        assert envelope["receipt"]["billing_basis"] == expected
        jsonschema.validate(envelope, RECEIPT_SCHEMA)
        assert verify_shard_hash(entry)["status"] == "valid"  # sealed with the record's content hash
        stored = (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")
        for blob in (stored, text, json.dumps(envelope)):
            assert SECRET not in blob and OAUTH_SECRET not in blob

    def test_the_login_file_decides_and_its_tokens_stay_in_it(self, repo, home, monkeypatch):
        _claude_login(home / ".claude")
        entry = _claude_session(repo, _env(home))
        history, envelope, text = _outputs(repo, entry, monkeypatch)
        assert (entry["capture"]["billing_basis"], history["billing_basis"]) == ("subscription", "subscription")
        jsonschema.validate(envelope, RECEIPT_SCHEMA)
        for blob in ((repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8"), text, json.dumps(envelope)):
            assert OAUTH_SECRET not in blob

    def test_unknown_is_absent_everywhere(self, repo, home, monkeypatch):
        entry = _claude_session(repo, _env(home, ANTHROPIC_API_KEY=SECRET, CLAUDE_CODE_ENTRYPOINT="cli"))
        assert "billing_basis" not in entry["capture"]
        history, envelope, _ = _outputs(repo, entry, monkeypatch)
        assert "billing_basis" not in history and "billing_basis" not in envelope["receipt"]
        jsonschema.validate(envelope, RECEIPT_SCHEMA)

    def test_an_older_receipt_keeps_its_exact_shape_and_still_verifies(self, repo, home, monkeypatch):
        entry = _claude_session(repo, _env(home))
        assert "billing_basis" not in entry["capture"]
        projected = receipt_to_dict(build_shard_receipt(entry), extended=True)
        assert "billing_basis" not in projected
        assert verify_shard_hash(entry)["status"] == "valid"

    def test_a_tampered_label_is_dropped_and_breaks_the_hash(self, repo, home):
        entry = _claude_session(repo, _env(home, CLAUDE_CODE_OAUTH_TOKEN=OAUTH_SECRET))
        entry["capture"]["billing_basis"] = "free"
        assert "billing_basis" not in receipt_to_dict(build_shard_receipt(entry), extended=True)
        assert verify_shard_hash(entry)["status"] == "mismatch"

    def test_the_next_turn_of_a_cloud_session_keeps_it(self, repo, home):
        cloud = {"CLAUDE_CODE_REMOTE": "true", "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01HSAXkLPtdu65CqEWgJgAUF"}
        base = {"session_id": SID, "cwd": str(repo)}
        env = {"CLAUDE_PROJECT_DIR": str(repo), **_env(home, **cloud)}
        handle_claude_hook({**base, "hook_event_name": "SessionStart", "source": "startup"}, env=env)
        handle_claude_hook({**base, "hook_event_name": "UserPromptSubmit", "prompt": "First"}, env=env)
        handle_claude_hook({**base, "hook_event_name": "SessionEnd", "reason": "other"}, env=env)
        # Later turns arrive through HTTP hooks, which carry no environment.
        bare = {"CLAUDE_PROJECT_DIR": str(repo)}
        handle_claude_hook({**base, "hook_event_name": "UserPromptSubmit", "prompt": "Second"}, env=bare)
        handle_claude_hook({**base, "hook_event_name": "SessionEnd", "reason": "other"}, env=bare)
        first, second = _lines(repo)
        assert first["capture"]["billing_basis"] == second["capture"]["billing_basis"] == "subscription"

    def test_codex_through_the_hook_client(self, repo, home, monkeypatch):
        _codex_auth(home / ".codex", "apikey")
        env = {**_env(home), "OPENSHARD_CAPTURE_DISABLE": "1"}
        base = {"session_id": SID, "cwd": str(repo), "model": "gpt-5-codex", "transcript_path": None}
        for doc in ({**base, "hook_event_name": "SessionStart", "source": "startup"},
                    {**base, "hook_event_name": "UserPromptSubmit", "prompt": "make f return 2"},
                    {**base, "hook_event_name": "Stop"}):
            assert client._run_hook_raw(json.dumps(doc).encode(), env, event_override=None, agent="codex",
                                        spawn=False) != "error"
        (entry,) = _lines(repo)
        assert entry["capture"]["billing_basis"] == "api_billed"
        history, envelope, text = _outputs(repo, entry, monkeypatch)
        assert envelope["receipt"]["billing_basis"] == "api_billed"
        jsonschema.validate(envelope, RECEIPT_SCHEMA)
        for blob in ((repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8"), text, json.dumps(envelope)):
            assert SECRET not in blob and OAUTH_SECRET not in blob

    def test_cursor_records_nothing(self, repo, home):
        text = (FIXTURES / "agents" / "cursor-session.jsonl").read_text(encoding="utf-8")
        repo_json = json.dumps(str(repo))[1:-1]
        docs = [json.loads(ln.replace("@REPO@", repo_json).replace("@SID@", SID)) for ln in text.splitlines()]
        env = _env(home, ANTHROPIC_API_KEY=SECRET, CLAUDE_CODE_ENTRYPOINT="sdk-cli", OPENAI_API_KEY=SECRET)
        for doc in docs:
            if "_step" not in doc:
                handle_hook(doc, env=env, agent="cursor", agent_env=agent_environment("cursor", env))
        entries = _lines(repo)
        assert entries and all("billing_basis" not in entry["capture"] for entry in entries)
