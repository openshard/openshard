from __future__ import annotations

import unittest

from click.testing import CliRunner

from openshard.cli.main import cli


def _runner():
    return CliRunner()


class TestRootHelpSections(unittest.TestCase):
    """Root --help groups commands into sections instead of one flat list."""

    def test_sections_appear_in_order(self):
        result = _runner().invoke(cli, ["--help"])
        self.assertEqual(result.exit_code, 0)
        out = result.output
        for section in ("Getting Started:", "Receipts:", "Diagnostics:", "Integrations:", "Advanced:"):
            self.assertIn(section, out)
        self.assertLess(out.index("Getting Started:"), out.index("Receipts:"))
        self.assertLess(out.index("Receipts:"), out.index("Diagnostics:"))
        self.assertLess(out.index("Diagnostics:"), out.index("Integrations:"))
        self.assertLess(out.index("Integrations:"), out.index("Advanced:"))

    def test_setup_is_in_getting_started_not_advanced(self):
        result = _runner().invoke(cli, ["--help"])
        out = result.output
        getting_started = out[out.index("Getting Started:"):out.index("Receipts:")]
        advanced = out[out.index("Advanced:"):]
        self.assertIn("setup", getting_started)
        self.assertNotIn("  setup ", advanced)

    def test_no_command_is_dropped_from_help(self):
        """Every visible top-level command must land in exactly one section."""
        result = _runner().invoke(cli, ["--help"])
        out = result.output
        for name in ("last", "history", "run", "tui", "mcp", "roster", "eval"):
            self.assertIn(f"\n  {name}", out)


class TestHiddenInternalCommands(unittest.TestCase):
    """Automation-only commands are hidden from help but remain callable."""

    def test_hooks_group_hidden_from_root_help(self):
        result = _runner().invoke(cli, ["--help"])
        self.assertNotIn("\n  hooks ", result.output)

    def test_hooks_group_still_invocable(self):
        result = _runner().invoke(cli, ["hooks", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("claude", result.output)

    def test_capture_serve_hidden_from_capture_help(self):
        result = _runner().invoke(cli, ["capture", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn("serve", result.output)

    def test_capture_serve_help_still_works(self):
        result = _runner().invoke(cli, ["capture", "serve", "--help"])
        self.assertEqual(result.exit_code, 0)

    def test_shard_verify_hidden_from_shard_help(self):
        result = _runner().invoke(cli, ["shard", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn("verify", result.output)

    def test_shard_verify_last_still_resolves_as_a_command(self):
        # No history recorded in this isolated filesystem, so the command
        # legitimately fails (see tests/test_cli_proof_last.py) -- what
        # matters here is that hiding it from --help did not also make
        # Click treat it as an unknown command (exit code 2).
        with _runner().isolated_filesystem():
            result = _runner().invoke(cli, ["shard", "verify", "last", "--json"])
            self.assertNotEqual(result.exit_code, 2, result.output)


class TestInitDemotedNotRemoved(unittest.TestCase):
    """`init` keeps working; it is just no longer presented as a peer of `setup`."""

    def test_init_still_listed_somewhere_in_help(self):
        result = _runner().invoke(cli, ["--help"])
        self.assertIn("\n  init", result.output)

    def test_init_help_points_to_setup(self):
        result = _runner().invoke(cli, ["init", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("openshard setup", result.output)


if __name__ == "__main__":
    unittest.main()
