"""Benchmark-owned verification for Scenario 7 (never visible to the agent).

Same shape as Scenario 1's hidden tests (a schema field added the correct
way vs. the known-bad way), reused here because Scenario 7 asks the same
underlying question with the burn-in agent swapped from Claude Code to
OpenCode: does OpenShard's captured evidence transfer across agents, not
just across sessions of the same one.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WORKSPACE = Path.cwd()
HERE = Path(__file__).resolve().parent


def relay(queue: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "relay", "--queue", str(queue), *args],
        cwd=WORKSPACE, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )


class SourceOfTruthTests(unittest.TestCase):
    def test_schema_json_declares_tags_as_trailing_optional_str(self):
        spec = json.loads((WORKSPACE / "schema" / "jobs.json").read_text(encoding="utf-8"))
        names = [f["name"] for f in spec["fields"]]
        self.assertEqual(names[:3], ["name", "command", "retries"])
        self.assertIn("tags", names)
        self.assertEqual(names[-1], "tags")
        tags = next(f for f in spec["fields"] if f["name"] == "tags")
        self.assertEqual(tags.get("type"), "str")
        self.assertFalse(tags.get("required", False))
        self.assertEqual(tags.get("default"), "")

    def test_generated_module_is_in_sync_with_its_source(self):
        check = subprocess.run([sys.executable, "scripts/gen_schema.py", "--check"],
                               cwd=WORKSPACE, capture_output=True, text=True, timeout=60)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)


class RecordTests(unittest.TestCase):
    def test_tags_defaults_to_empty_string(self):
        from relay.records import Job, parse_line

        self.assertEqual(parse_line("build\tmake -j4\t2").tags, "")
        self.assertEqual(Job(name="a", command="b").tags, "")

    def test_tags_parses_and_formats(self):
        from relay.records import Job, format_line, parse_line

        self.assertEqual(parse_line("a\tb\t1\tbackend,urgent").tags, "backend,urgent")
        self.assertEqual(format_line(Job(name="a", command="b", retries=1, tags="x,y")), "a\tb\t1\tx,y")

    def test_default_tags_is_omitted_when_writing(self):
        from relay.records import Job, format_line

        self.assertEqual(format_line(Job(name="a", command="b", retries=1)), "a\tb\t1")

    def test_queue_file_from_older_release_loads_with_empty_tags(self):
        from relay.queue import QueueFile

        jobs = QueueFile(HERE / "fixtures" / "queue_v1.txt").load()
        self.assertEqual([job.name for job in jobs], ["build", "test", "deploy"])
        self.assertEqual([job.tags for job in jobs], ["", "", ""])


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self._tmp.name) / "relay.queue"

    def tearDown(self):
        self._tmp.cleanup()

    def test_add_with_tags_and_show(self):
        result = relay(self.queue, "add", "a", "echo a", "--tags", "backend,urgent")
        self.assertEqual(result.returncode, 0, result.stderr)
        shown = relay(self.queue, "show", "a")
        self.assertIn("tags: backend,urgent", shown.stdout)

    def test_queue_file_stays_compatible_when_tags_is_default(self):
        relay(self.queue, "add", "build", "make")
        self.assertEqual(self.queue.read_text(encoding="utf-8").replace("\r\n", "\n"), "build\tmake\n")


if __name__ == "__main__":
    unittest.main()
