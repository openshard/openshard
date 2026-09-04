import tempfile
import unittest
from pathlib import Path

from relay.queue import QueueError, QueueFile
from relay.records import Job

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class QueueFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = QueueFile(Path(self._tmp.name) / "relay.queue")

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_is_empty(self):
        self.assertEqual(self.queue.load(), [])

    def test_add_and_load_keep_insertion_order(self):
        self.queue.add(Job(name="b", command="echo b"))
        self.queue.add(Job(name="a", command="echo a", retries=3))
        self.assertEqual([job.name for job in self.queue.ordered()], ["b", "a"])
        self.assertEqual(self.queue.find("a").retries, 3)

    def test_duplicate_name_rejected(self):
        self.queue.add(Job(name="a", command="echo a"))
        with self.assertRaises(QueueError):
            self.queue.add(Job(name="a", command="echo again"))

    def test_remove(self):
        self.queue.add(Job(name="a", command="echo a"))
        self.queue.add(Job(name="b", command="echo b"))
        self.queue.remove("a")
        self.assertEqual([job.name for job in self.queue.load()], ["b"])
        with self.assertRaises(QueueError):
            self.queue.remove("a")

    def test_comments_and_blank_lines_are_skipped(self):
        self.queue.path.write_text("# nightly jobs\n\nbuild\tmake\n", encoding="utf-8")
        self.assertEqual([job.name for job in self.queue.load()], ["build"])

    def test_bad_line_reports_line_number(self):
        self.queue.path.write_text("build\tmake\n\nbroken\tx\ty\tz\n", encoding="utf-8")
        with self.assertRaises(QueueError) as ctx:
            self.queue.load()
        self.assertIn(":3:", str(ctx.exception))

    def test_queue_file_from_older_release_loads(self):
        jobs = QueueFile(FIXTURES / "queue_v1.txt").load()
        self.assertEqual([job.name for job in jobs], ["build", "test", "deploy"])
        self.assertEqual([job.retries for job in jobs], [2, 0, 0])


if __name__ == "__main__":
    unittest.main()
