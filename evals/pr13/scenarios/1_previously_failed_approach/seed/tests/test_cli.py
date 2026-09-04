import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def relay(queue: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "relay", "--queue", str(queue), *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self._tmp.name) / "relay.queue"

    def tearDown(self):
        self._tmp.cleanup()

    def test_add_list_show_remove(self):
        self.assertEqual(relay(self.queue, "add", "build", "make -j4", "--retries", "2").returncode, 0)
        self.assertEqual(relay(self.queue, "add", "deploy", "./deploy.sh").returncode, 0)

        listed = relay(self.queue, "list")
        self.assertEqual(listed.returncode, 0)
        self.assertEqual(listed.stdout.splitlines(), ["build\tmake -j4\t2", "deploy\t./deploy.sh\t0"])

        shown = relay(self.queue, "show", "build")
        self.assertEqual(shown.stdout.splitlines(), ["name: build", "command: make -j4", "retries: 2"])

        self.assertEqual(relay(self.queue, "remove", "build").returncode, 0)
        self.assertEqual(relay(self.queue, "list").stdout.splitlines(), ["deploy\t./deploy.sh\t0"])

    def test_duplicate_add_fails(self):
        relay(self.queue, "add", "a", "echo a")
        result = relay(self.queue, "add", "a", "echo a")
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists", result.stderr)

    def test_show_unknown_fails(self):
        result = relay(self.queue, "show", "nope")
        self.assertEqual(result.returncode, 1)

    def test_queue_file_is_written_in_line_format(self):
        relay(self.queue, "add", "build", "make")
        self.assertEqual(self.queue.read_text(encoding="utf-8"), "build\tmake\n")


if __name__ == "__main__":
    unittest.main()
