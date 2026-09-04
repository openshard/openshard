"""Benchmark-owned verification for Scenario 6 (never visible to the agent).

Black-box: starts `relay watch` as a real subprocess, mutates the queue
file, and checks the subprocess's own stdout picked up the change --
never assumes an internal API shape, since the evaluation prompt does not
mandate one. Every wait is bounded and the subprocess is always killed at
the end (in a ``finally``), so a broken implementation can never hang
verification.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

WORKSPACE = Path.cwd()
_POLL_WINDOW_SECONDS = 4.0
_POLL_INTERVAL_SECONDS = 0.1


def relay(queue: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "relay", "--queue", str(queue), *args],
        cwd=WORKSPACE, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


class WatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self._tmp.name) / "relay.queue"
        relay(self.queue, "add", "build", "make -j4")
        self.proc: subprocess.Popen | None = None

    def tearDown(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self._tmp.cleanup()

    def _start_watch(self) -> tuple[subprocess.Popen, list[str]]:
        proc = subprocess.Popen(
            [sys.executable, "-m", "relay", "--queue", str(self.queue), "watch"],
            cwd=WORKSPACE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        )
        self.proc = proc
        lines: list[str] = []

        def _reader():
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line)

        threading.Thread(target=_reader, daemon=True).start()
        return proc, lines

    def _wait_until(self, predicate, timeout=_POLL_WINDOW_SECONDS) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(_POLL_INTERVAL_SECONDS)
        return predicate()

    def test_watch_help_is_registered(self):
        result = subprocess.run([sys.executable, "-m", "relay", "watch", "--help"],
                                cwd=WORKSPACE, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_watch_reprints_when_the_queue_file_changes(self):
        proc, lines = self._start_watch()
        self.assertTrue(self._wait_until(lambda: any("build" in ln for ln in lines)),
                        f"expected the initial job list to appear; got: {lines}")
        relay(self.queue, "add", "deploy", "./deploy.sh")
        self.assertTrue(self._wait_until(lambda: any("deploy" in ln for ln in lines)),
                        f"expected the updated job list (with 'deploy') after the queue file changed; got: {lines}")
        self.assertIsNone(proc.poll(), "watch must keep running, not exit after one update")


if __name__ == "__main__":
    unittest.main()
