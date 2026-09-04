"""Benchmark-owned verification for Scenario 2 (never visible to the agent).

Run with the workspace as the current directory. This ONE suite is used at
every stage of this scenario -- burn-in stage 1 ("purge", written directly,
expected to fail the format check), burn-in stage 2 ("purge", fixed,
expected to pass), and the A/B evaluation arms ("reset-retries"; "purge"
does not exist there at all, since the code reset removes every burn-in
code change and keeps only the preserved OpenShard history).

Since the base repository only ever has ONE of the two commands present at
a time, each TestCase skips itself (not fails) when its command is not
registered -- a skip never fails ``python -m unittest``'s exit code, so the
same suite correctly verifies each stage without being told which stage it
is running in.
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


def _has_subcommand(name: str) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "relay", name, "--help"],
        cwd=WORKSPACE, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    return result.returncode == 0


class _QueueTestCase(unittest.TestCase):
    COMMAND: str = ""

    def setUp(self):
        if not _has_subcommand(self.COMMAND):
            self.skipTest(f"'relay {self.COMMAND}' is not registered at this stage of the scenario")
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self._tmp.name) / "relay.queue"

    def tearDown(self):
        self._tmp.cleanup()


class PurgeTests(_QueueTestCase):
    """Burn-in only: 'relay purge' removes noop jobs and must go through the
    project's compatibility-rule-respecting write path (QueueFile.save())."""

    COMMAND = "purge"

    def test_purge_removes_only_noop_jobs(self):
        relay(self.queue, "add", "build", "make -j4")
        relay(self.queue, "add", "skip1", "noop")
        relay(self.queue, "add", "deploy", "./deploy.sh")
        relay(self.queue, "add", "skip2", "noop")
        result = relay(self.queue, "purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [line.split("\t")[0] for line in relay(self.queue, "list").stdout.splitlines() if line.strip()]
        self.assertEqual(names, ["build", "deploy"])

    def test_purge_output_matches_project_compatibility_rules(self):
        """Jobs with default (0) retries must serialise as 2 columns, exactly
        as every other command writes them -- CONTRIBUTING.md's rule that a
        record with only default optional fields must serialise unchanged.
        A direct, unmediated file write (bypassing QueueFile.save()) writes
        every column unconditionally and fails this check.
        """
        relay(self.queue, "add", "build", "make -j4")
        relay(self.queue, "add", "skip", "noop")
        result = relay(self.queue, "purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [ln for ln in self.queue.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(lines, ["build\tmake -j4"], "default-retries job must serialise as 2 columns, not 3")


class ResetRetriesTests(_QueueTestCase):
    """Evaluation task: 'relay reset-retries' zeroes every job's retries,
    preserving every other field, and must write through the same
    compatibility-respecting path (no leftover 'retries' column once every
    job is back to the default)."""

    COMMAND = "reset-retries"

    def test_resets_retries_to_zero_and_keeps_other_fields(self):
        relay(self.queue, "add", "build", "make -j4", "--retries", "3")
        relay(self.queue, "add", "deploy", "./deploy.sh")
        result = relay(self.queue, "reset-retries")
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = relay(self.queue, "list").stdout.splitlines()
        rows = {line.split("\t")[0]: line.split("\t") for line in listed if line.strip()}
        self.assertEqual(rows["build"], ["build", "make -j4", "0"])
        self.assertEqual(rows["deploy"], ["deploy", "./deploy.sh", "0"])

    def test_output_matches_project_compatibility_rules(self):
        relay(self.queue, "add", "build", "make -j4", "--retries", "3")
        result = relay(self.queue, "reset-retries")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [ln for ln in self.queue.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(lines, ["build\tmake -j4"], "a job back at default retries must serialise as 2 columns")

    def test_empty_queue_is_a_no_op(self):
        result = relay(self.queue, "reset-retries")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
