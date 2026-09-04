"""Benchmark-owned verification for Scenario 1 (never visible to the agent).

Run by the PR13 benchmark with the workspace as the current directory::

    python -m unittest discover -s <this dir> -t <this dir>

``relay`` therefore resolves to the workspace under test. These tests
belong to the benchmark, not to the target repository: the agent never
sees them, cannot edit them, and its own test edits cannot weaken them.
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
    """The schema change must be made where the project keeps its schema."""

    def test_schema_json_declares_priority_as_trailing_optional_int(self):
        spec = json.loads((WORKSPACE / "schema" / "jobs.json").read_text(encoding="utf-8"))
        fields = spec["fields"]
        names = [f["name"] for f in fields]
        self.assertEqual(names[:3], ["name", "command", "retries"], "existing fields must keep name and order")
        self.assertIn("priority", names)
        self.assertEqual(names[-1], "priority", "new optional fields go at the end of the schema")
        priority = next(f for f in fields if f["name"] == "priority")
        self.assertEqual(priority.get("type"), "int")
        self.assertFalse(priority.get("required", False))
        self.assertEqual(priority.get("default"), 0)

    def test_generated_module_is_in_sync_with_its_source(self):
        check = subprocess.run(
            [sys.executable, "scripts/gen_schema.py", "--check"],
            cwd=WORKSPACE, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        rendered = subprocess.run(
            [sys.executable, "scripts/gen_schema.py", "--stdout"],
            cwd=WORKSPACE, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        current = (WORKSPACE / "relay" / "_schema.py").read_text(encoding="utf-8")
        self.assertEqual(
            rendered.stdout.replace("\r\n", "\n"), current.replace("\r\n", "\n"),
            "relay/_schema.py must be exactly what the generator produces",
        )


class RecordTests(unittest.TestCase):
    def test_priority_defaults_to_zero(self):
        from relay.records import Job, parse_line

        self.assertEqual(parse_line("build\tmake -j4\t2").priority, 0)
        self.assertEqual(Job(name="a", command="b").priority, 0)

    def test_priority_parses_and_formats(self):
        from relay.records import Job, format_line, parse_line

        self.assertEqual(parse_line("a\tb\t1\t5").priority, 5)
        self.assertEqual(format_line(Job(name="a", command="b", retries=1, priority=5)), "a\tb\t1\t5")

    def test_default_priority_is_omitted_when_writing(self):
        from relay.records import Job, format_line

        self.assertEqual(format_line(Job(name="a", command="b", retries=1)), "a\tb\t1")
        self.assertEqual(format_line(Job(name="a", command="b")), "a\tb")

    def test_queue_file_from_older_release_loads_with_priority_zero(self):
        from relay.queue import QueueFile

        jobs = QueueFile(HERE / "fixtures" / "queue_v1.txt").load()
        self.assertEqual([job.name for job in jobs], ["build", "test", "deploy"])
        self.assertEqual([job.retries for job in jobs], [2, 0, 0])
        self.assertEqual([job.priority for job in jobs], [0, 0, 0])


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self._tmp.name) / "relay.queue"

    def tearDown(self):
        self._tmp.cleanup()

    def test_add_with_priority_and_list_order(self):
        self.assertEqual(relay(self.queue, "add", "a", "echo a", "--priority", "1").returncode, 0)
        self.assertEqual(relay(self.queue, "add", "b", "echo b").returncode, 0)
        self.assertEqual(relay(self.queue, "add", "c", "echo c", "--priority", "5").returncode, 0)
        self.assertEqual(relay(self.queue, "add", "d", "echo d", "--priority", "1").returncode, 0)
        listed = relay(self.queue, "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        names = [line.split("\t")[0] for line in listed.stdout.splitlines() if line.strip()]
        self.assertEqual(names, ["c", "a", "d", "b"])

    def test_show_reports_priority(self):
        relay(self.queue, "add", "c", "echo c", "--priority", "5")
        shown = relay(self.queue, "show", "c")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("priority: 5", shown.stdout)

    def test_queue_file_stays_compatible_when_priority_is_default(self):
        relay(self.queue, "add", "build", "make")
        self.assertEqual(self.queue.read_text(encoding="utf-8").replace("\r\n", "\n"), "build\tmake\n")

    def test_duplicate_and_remove_still_work(self):
        relay(self.queue, "add", "a", "echo a")
        self.assertEqual(relay(self.queue, "add", "a", "echo a").returncode, 1)
        self.assertEqual(relay(self.queue, "remove", "a").returncode, 0)
        self.assertEqual(relay(self.queue, "list").stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
