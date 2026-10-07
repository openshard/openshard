from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from openshard.cli.main import cli

_NO_KEYS = {
    "OPENROUTER_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "OPENAI_API_KEY": "",
    "OPENSHARD_CONFIG": "",
}
_CONFIG_REL = Path(".openshard") / "config.yml"

# Real model IDs from the registry (active_default lifecycle)
_KNOWN_MODEL = "anthropic/claude-sonnet-4.6"
_KNOWN_MODEL_2 = "deepseek/deepseek-v4.1-flash"
_UNKNOWN_MODEL = "fake/model-does-not-exist-xyz"


def _runner() -> CliRunner:
    return CliRunner()


def _write_config(data: dict) -> None:
    _CONFIG_REL.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_REL.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _read_config() -> dict:
    return yaml.safe_load(_CONFIG_REL.read_text(encoding="utf-8")) or {}


class TestRosterList(unittest.TestCase):
    def test_list_empty_roster_exits_zero(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                result = _runner().invoke(cli, ["roster", "list"])
                self.assertEqual(result.exit_code, 0)

    def test_list_empty_roster_shows_auto_mode(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                result = _runner().invoke(cli, ["roster", "list"])
                self.assertIn("auto", result.output)

    def test_list_shows_models_when_present(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _write_config({
                    "models": {
                        "mode": "custom_roster",
                        "custom_roster": {"name": "myteam", "models": [_KNOWN_MODEL]},
                    }
                })
                result = _runner().invoke(cli, ["roster", "list"])
                self.assertEqual(result.exit_code, 0)
                self.assertIn(_KNOWN_MODEL, result.output)
                self.assertIn("myteam", result.output)

    def test_list_shows_valid_count(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _write_config({
                    "models": {
                        "mode": "custom_roster",
                        "custom_roster": {"name": "default", "models": [_KNOWN_MODEL, _UNKNOWN_MODEL]},
                    }
                })
                result = _runner().invoke(cli, ["roster", "list"])
                self.assertEqual(result.exit_code, 0)
                # 1 known, 1 unknown
                self.assertIn("1 known", result.output)
                self.assertIn("1 unknown", result.output)


class TestRosterShow(unittest.TestCase):
    def test_show_exits_zero(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                result = _runner().invoke(cli, ["roster", "show"])
                self.assertEqual(result.exit_code, 0)

    def test_show_no_secrets(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _write_config({
                    "models": {"mode": "auto"},
                    "anthropic_api_key": "sk-secret-value",
                })
                result = _runner().invoke(cli, ["roster", "show"])
                self.assertNotIn("sk-secret-value", result.output)
                self.assertNotIn("_api_key", result.output)


class TestRosterAdd(unittest.TestCase):
    def test_add_known_model_exits_zero(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                result = _runner().invoke(cli, ["roster", "add", _KNOWN_MODEL])
                self.assertEqual(result.exit_code, 0)

    def test_add_known_model_writes_to_config(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _runner().invoke(cli, ["roster", "add", _KNOWN_MODEL])
                cfg = _read_config()
                self.assertIn(_KNOWN_MODEL, cfg["models"]["custom_roster"]["models"])

    def test_add_unknown_model_exits_nonzero(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                result = _runner().invoke(cli, ["roster", "add", _UNKNOWN_MODEL])
                self.assertNotEqual(result.exit_code, 0)

    def test_add_unknown_model_shows_clear_error(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                result = _runner().invoke(cli, ["roster", "add", _UNKNOWN_MODEL])
                self.assertIn("Unknown model ID", result.output)

    def test_add_duplicate_does_not_duplicate(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _runner().invoke(cli, ["roster", "add", _KNOWN_MODEL])
                result = _runner().invoke(cli, ["roster", "add", _KNOWN_MODEL])
                self.assertEqual(result.exit_code, 0)
                cfg = _read_config()
                roster = cfg["models"]["custom_roster"]["models"]
                self.assertEqual(roster.count(_KNOWN_MODEL), 1)

    def test_add_preserves_unrelated_fields(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _write_config({"onboarding": {"mode": "native"}, "models": {"mode": "auto"}})
                _runner().invoke(cli, ["roster", "add", _KNOWN_MODEL])
                cfg = _read_config()
                self.assertIn("onboarding", cfg)
                self.assertEqual(cfg["onboarding"]["mode"], "native")


class TestRosterRemove(unittest.TestCase):
    def test_remove_present_model_exits_zero(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _write_config({
                    "models": {
                        "mode": "custom_roster",
                        "custom_roster": {"name": "default", "models": [_KNOWN_MODEL]},
                    }
                })
                result = _runner().invoke(cli, ["roster", "remove", _KNOWN_MODEL])
                self.assertEqual(result.exit_code, 0)

    def test_remove_present_model_removes_from_config(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _write_config({
                    "models": {
                        "mode": "custom_roster",
                        "custom_roster": {"name": "default", "models": [_KNOWN_MODEL]},
                    }
                })
                _runner().invoke(cli, ["roster", "remove", _KNOWN_MODEL])
                cfg = _read_config()
                self.assertNotIn(_KNOWN_MODEL, cfg["models"]["custom_roster"]["models"])

    def test_remove_absent_model_exits_zero(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                result = _runner().invoke(cli, ["roster", "remove", "nonexistent/model-id"])
                self.assertEqual(result.exit_code, 0)

    def test_remove_absent_model_gives_clear_message(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                result = _runner().invoke(cli, ["roster", "remove", "nonexistent/model-id"])
                self.assertIn("not in the roster", result.output)


class TestRosterUse(unittest.TestCase):
    def test_use_sets_mode_custom_roster(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _runner().invoke(cli, ["roster", "use", "myteam"])
                cfg = _read_config()
                self.assertEqual(cfg["models"]["mode"], "custom_roster")

    def test_use_sets_roster_name(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _runner().invoke(cli, ["roster", "use", "myteam"])
                cfg = _read_config()
                self.assertEqual(cfg["models"]["custom_roster"]["name"], "myteam")

    def test_use_exits_zero(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                result = _runner().invoke(cli, ["roster", "use", "myteam"])
                self.assertEqual(result.exit_code, 0)

    def test_use_notes_single_local_list(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                result = _runner().invoke(cli, ["roster", "use", "myteam"])
                self.assertIn("label", result.output)


class TestRosterValidate(unittest.TestCase):
    def test_validate_all_valid_exits_zero(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _write_config({
                    "models": {
                        "mode": "custom_roster",
                        "custom_roster": {"name": "default", "models": [_KNOWN_MODEL]},
                    }
                })
                result = _runner().invoke(cli, ["roster", "validate"])
                self.assertEqual(result.exit_code, 0)

    def test_validate_invalid_id_exits_nonzero(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _write_config({
                    "models": {
                        "mode": "custom_roster",
                        "custom_roster": {"name": "default", "models": [_KNOWN_MODEL, _UNKNOWN_MODEL]},
                    }
                })
                result = _runner().invoke(cli, ["roster", "validate"])
                self.assertNotEqual(result.exit_code, 0)

    def test_validate_names_invalid_id(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _write_config({
                    "models": {
                        "mode": "custom_roster",
                        "custom_roster": {"name": "default", "models": [_UNKNOWN_MODEL]},
                    }
                })
                result = _runner().invoke(cli, ["roster", "validate"])
                self.assertIn(_UNKNOWN_MODEL, result.output)

    def test_validate_says_known_to_registry(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _write_config({
                    "models": {
                        "mode": "custom_roster",
                        "custom_roster": {"name": "default", "models": [_KNOWN_MODEL]},
                    }
                })
                result = _runner().invoke(cli, ["roster", "validate"])
                self.assertIn("registry", result.output)

    def test_validate_empty_roster_exits_zero(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                result = _runner().invoke(cli, ["roster", "validate"])
                self.assertEqual(result.exit_code, 0)


class TestRosterReset(unittest.TestCase):
    def test_reset_clears_models(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _write_config({
                    "models": {
                        "mode": "custom_roster",
                        "custom_roster": {"name": "myteam", "models": [_KNOWN_MODEL, _KNOWN_MODEL_2]},
                    }
                })
                _runner().invoke(cli, ["roster", "reset"])
                cfg = _read_config()
                self.assertEqual(cfg["models"]["custom_roster"]["models"], [])

    def test_reset_sets_mode_auto(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _write_config({
                    "models": {
                        "mode": "custom_roster",
                        "custom_roster": {"name": "myteam", "models": [_KNOWN_MODEL]},
                    }
                })
                _runner().invoke(cli, ["roster", "reset"])
                cfg = _read_config()
                self.assertEqual(cfg["models"]["mode"], "auto")

    def test_reset_exits_zero(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                result = _runner().invoke(cli, ["roster", "reset"])
                self.assertEqual(result.exit_code, 0)

    def test_reset_preserves_unrelated_fields(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _write_config({
                    "onboarding": {"mode": "native"},
                    "models": {
                        "mode": "custom_roster",
                        "custom_roster": {"name": "x", "models": [_KNOWN_MODEL]},
                    },
                })
                _runner().invoke(cli, ["roster", "reset"])
                cfg = _read_config()
                self.assertIn("onboarding", cfg)
                self.assertEqual(cfg["onboarding"]["mode"], "native")


class TestRosterMalformedConfig(unittest.TestCase):
    def test_malformed_config_add_fails_clearly(self) -> None:
        with patch.dict(os.environ, _NO_KEYS, clear=False):
            with _runner().isolated_filesystem():
                _CONFIG_REL.parent.mkdir(parents=True, exist_ok=True)
                _CONFIG_REL.write_text(": invalid: yaml: [\n", encoding="utf-8")
                result = _runner().invoke(cli, ["roster", "add", _KNOWN_MODEL])
                self.assertNotEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
