"""Benchmark-owned verification for Scenario 5 (never visible to the agent).

Tests ONLY the evaluation task (`relay rename`). Burn-in's own task
(`relay count`) is deliberately not checked here -- see Scenario 4's
hidden tests for the same rationale: burn-in's own correctness is not
gated for this scenario (require_verification_failed/
require_known_failed_approach are both false).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WORKSPACE = Path.cwd()


def relay(queue: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "relay", "--queue", str(queue), *args],
        cwd=WORKSPACE, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )


class RenameTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self._tmp.name) / "relay.queue"

    def tearDown(self):
        self._tmp.cleanup()

    def test_rename_keeps_command_and_position(self):
        relay(self.queue, "add", "build", "make -j4", "--retries", "2")
        relay(self.queue, "add", "deploy", "./deploy.sh")
        result = relay(self.queue, "rename", "build", "compile")
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = [line.split("\t") for line in relay(self.queue, "list").stdout.splitlines() if line.strip()]
        self.assertEqual([row[0] for row in listed], ["compile", "deploy"])
        self.assertEqual(listed[0], ["compile", "make -j4", "2"])

    def test_rename_missing_source_fails(self):
        relay(self.queue, "add", "deploy", "./deploy.sh")
        result = relay(self.queue, "rename", "nope", "somethingelse")
        self.assertNotEqual(result.returncode, 0)
        names = [line.split("\t")[0] for line in relay(self.queue, "list").stdout.splitlines() if line.strip()]
        self.assertEqual(names, ["deploy"])

    def test_rename_to_existing_name_fails(self):
        relay(self.queue, "add", "build", "make -j4")
        relay(self.queue, "add", "deploy", "./deploy.sh")
        result = relay(self.queue, "rename", "build", "deploy")
        self.assertNotEqual(result.returncode, 0)
        names = [line.split("\t")[0] for line in relay(self.queue, "list").stdout.splitlines() if line.strip()]
        self.assertEqual(names, ["build", "deploy"])


if __name__ == "__main__":
    unittest.main()
