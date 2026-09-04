"""Benchmark-owned verification for Scenario 3 (never visible to the agent).

Shared by burn-in (`relay export`) and the evaluation arms (`relay
snapshot`; `export` does not exist there -- the code reset removes every
burn-in code change and keeps only the preserved OpenShard history). Each
TestCase skips itself, rather than failing, when its own command is not
registered, exactly like Scenario 2's hidden tests -- see that scenario's
suite for the full rationale.

The constraint under test is real and platform-observed, not invented:
this project's own queue-file writer (``QueueFile.save``) passes
``newline="\n"`` to avoid the default text-mode translation of ``\n`` to
the platform line separator (CRLF on Windows) on write; a file-writing
command that skips that discipline silently produces CRLF output even
though its in-memory text only ever used ``\n``.
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
        self.out = Path(self._tmp.name) / "out.txt"

    def tearDown(self):
        self._tmp.cleanup()


class ExportTests(_QueueTestCase):
    """Burn-in only: 'relay export' must not introduce CRLF line endings."""

    COMMAND = "export"

    def test_export_writes_lf_only(self):
        relay(self.queue, "add", "build", "make -j4", "--retries", "2")
        relay(self.queue, "add", "deploy", "./deploy.sh")
        result = relay(self.queue, "export", str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.out.read_bytes()
        self.assertNotIn(b"\r\n", data, "export must write LF line endings only, like QueueFile.save() does")

    def test_export_content_is_correct(self):
        relay(self.queue, "add", "build", "make -j4", "--retries", "2")
        relay(self.queue, "export", str(self.out))
        lines = self.out.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["build,make -j4,2"])


class SnapshotTests(_QueueTestCase):
    """Evaluation task: 'relay snapshot' must not introduce CRLF line endings
    either, and must not touch the queue file."""

    COMMAND = "snapshot"

    def test_snapshot_writes_lf_only(self):
        relay(self.queue, "add", "build", "make -j4", "--retries", "2")
        relay(self.queue, "add", "deploy", "./deploy.sh")
        result = relay(self.queue, "snapshot", str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.out.read_bytes()
        self.assertNotIn(b"\r\n", data, "snapshot must write LF line endings only, like QueueFile.save() does")

    def test_snapshot_matches_queue_format(self):
        relay(self.queue, "add", "build", "make -j4", "--retries", "2")
        relay(self.queue, "add", "deploy", "./deploy.sh")
        relay(self.queue, "snapshot", str(self.out))
        self.assertEqual(
            self.out.read_text(encoding="utf-8"), self.queue.read_text(encoding="utf-8"),
            "snapshot's format must match the queue file's own on-disk format",
        )

    def test_snapshot_does_not_modify_the_queue_file(self):
        relay(self.queue, "add", "build", "make -j4")
        before = self.queue.read_bytes()
        relay(self.queue, "snapshot", str(self.out))
        self.assertEqual(self.queue.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
