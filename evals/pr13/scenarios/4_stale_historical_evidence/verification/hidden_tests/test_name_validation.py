"""Benchmark-owned verification for Scenario 4 (never visible to the agent).

Tests ONLY the evaluation task (empty-name validation). Burn-in's own task
(negative-retries validation) is deliberately not checked here: this
scenario's burn-in is not required to pass verification or match a known
failed approach (see metadata.json's require_verification_failed/
require_known_failed_approach: false) -- its only job is to leave a real,
retrievable Shard about a validation feature that does NOT transfer
directly to this one. A shared "does the burn-in feature still exist"
check would be meaningless here (the code reset removes it) and is not
attempted.
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


class EmptyNameValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self._tmp.name) / "relay.queue"

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_name_is_rejected(self):
        result = relay(self.queue, "add", "", "echo hi")
        self.assertNotEqual(result.returncode, 0, "an empty job name must be rejected")
        self.assertFalse(self.queue.exists() and self.queue.read_text(encoding="utf-8").strip(),
                         "no job should have been added")

    def test_normal_name_still_works(self):
        result = relay(self.queue, "add", "build", "make -j4")
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [line.split("\t")[0] for line in relay(self.queue, "list").stdout.splitlines() if line.strip()]
        self.assertEqual(names, ["build"])

    def test_error_message_is_informative(self):
        result = relay(self.queue, "add", "", "echo hi")
        self.assertTrue(result.stderr.strip(), "the rejection must produce a message, not silent failure")


if __name__ == "__main__":
    unittest.main()
