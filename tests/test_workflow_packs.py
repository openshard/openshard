from __future__ import annotations

import unittest

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.workflow_packs.packs import REQUIRED_FIELDS, get_pack, load_packs

FORBIDDEN_STRINGS = ["PRIVATE_COMPANY", "INTERNAL_CUSTOMER", "AKIA", "sk-live", "sk-prod"]


class TestWorkflowPacksLoad(unittest.TestCase):

    def test_packs_load_successfully(self):
        packs = load_packs()
        self.assertGreater(len(packs), 0)

    def test_every_pack_has_required_fields(self):
        for p in load_packs():
            for field in REQUIRED_FIELDS:
                self.assertTrue(
                    hasattr(p, field) and getattr(p, field) not in (None, "", []),
                    f"Pack {p.id!r} has missing or empty field {field!r}",
                )

    def test_pack_ids_are_unique(self):
        ids = [p.id for p in load_packs()]
        self.assertEqual(len(ids), len(set(ids)), "Duplicate pack ids found")

    def test_production_iac_hardening_exists(self):
        p = get_pack("production-iac-hardening")
        self.assertEqual(p.id, "production-iac-hardening")

    def test_repo_explanation_exists(self):
        p = get_pack("repo-explanation")
        self.assertEqual(p.id, "repo-explanation")

    def test_get_pack_unknown_raises_key_error(self):
        with self.assertRaises(KeyError):
            get_pack("this-pack-does-not-exist")


class TestWorkflowPacksCLI(unittest.TestCase):

    def test_packs_list_exits_zero(self):
        result = CliRunner().invoke(cli, ["packs", "list"])
        self.assertEqual(result.exit_code, 0)

    def test_packs_list_shows_known_ids(self):
        result = CliRunner().invoke(cli, ["packs", "list"])
        self.assertIn("production-iac-hardening", result.output)
        self.assertIn("repo-explanation", result.output)

    def test_packs_show_displays_metadata(self):
        result = CliRunner().invoke(cli, ["packs", "show", "production-iac-hardening"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Title:", result.output)
        self.assertIn("Category:", result.output)
        self.assertIn("Safety notes:", result.output)

    def test_packs_prompt_prints_only_prompt_text(self):
        result = CliRunner().invoke(cli, ["packs", "prompt", "production-iac-hardening"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Terraform", result.output)
        self.assertNotIn("Title:", result.output)
        self.assertNotIn("Category:", result.output)
        self.assertNotIn("Tags:", result.output)

    def test_packs_show_unknown_id_exits_nonzero(self):
        result = CliRunner().invoke(cli, ["packs", "show", "this-does-not-exist"])
        self.assertNotEqual(result.exit_code, 0)

    def test_packs_prompt_unknown_id_exits_nonzero(self):
        result = CliRunner().invoke(cli, ["packs", "prompt", "this-does-not-exist"])
        self.assertNotEqual(result.exit_code, 0)

    def test_packs_show_unknown_lists_available_ids(self):
        result = CliRunner().invoke(cli, ["packs", "show", "bad-id"])
        self.assertIn("repo-explanation", result.output)

    def test_packs_prompt_includes_run_note(self):
        result = CliRunner().invoke(cli, ["packs", "prompt", "production-iac-hardening"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("This command only prints the prompt", result.output)


class TestWorkflowPacksSafety(unittest.TestCase):

    def test_prompts_no_forbidden_strings(self):
        for p in load_packs():
            for word in FORBIDDEN_STRINGS:
                self.assertNotIn(
                    word,
                    p.prompt,
                    f"Pack {p.id!r} prompt contains forbidden string {word!r}",
                )

    def test_prompts_no_fake_credentials(self):
        credential_patterns = ["AKIA", "sk-live", "sk-prod", "-----BEGIN"]
        for p in load_packs():
            for pattern in credential_patterns:
                self.assertNotIn(
                    pattern,
                    p.prompt,
                    f"Pack {p.id!r} prompt contains credential pattern {pattern!r}",
                )