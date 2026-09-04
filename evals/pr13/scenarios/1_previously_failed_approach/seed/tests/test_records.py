import unittest

from relay.records import Job, RecordError, format_line, parse_line


class ParseLineTests(unittest.TestCase):
    def test_full_line_round_trips(self):
        job = parse_line("build\tmake -j4\t2")
        self.assertEqual(job.name, "build")
        self.assertEqual(job.command, "make -j4")
        self.assertEqual(job.retries, 2)
        self.assertEqual(format_line(job), "build\tmake -j4\t2")

    def test_missing_optional_column_takes_default(self):
        job = parse_line("deploy\t./deploy.sh")
        self.assertEqual(job.retries, 0)

    def test_default_optional_column_is_omitted_when_writing(self):
        self.assertEqual(format_line(Job(name="deploy", command="./deploy.sh")), "deploy\t./deploy.sh")

    def test_trailing_newline_is_ignored(self):
        self.assertEqual(parse_line("a\tb\n").command, "b")

    def test_too_many_columns_is_an_error(self):
        with self.assertRaises(RecordError):
            parse_line("a\tb\t1\textra")

    def test_bad_integer_is_an_error(self):
        with self.assertRaises(RecordError):
            parse_line("a\tb\tlots")


class JobTests(unittest.TestCase):
    def test_required_field_missing(self):
        with self.assertRaises(RecordError):
            Job(name="only-a-name")

    def test_unknown_field_rejected(self):
        with self.assertRaises(RecordError):
            Job(name="a", command="b", colour="blue")

    def test_tab_in_value_rejected(self):
        with self.assertRaises(RecordError):
            Job(name="a\tb", command="c")

    def test_equality_and_dict(self):
        self.assertEqual(Job(name="a", command="b"), Job(name="a", command="b", retries=0))
        self.assertEqual(Job(name="a", command="b").to_dict(), {"name": "a", "command": "b", "retries": 0})


if __name__ == "__main__":
    unittest.main()
