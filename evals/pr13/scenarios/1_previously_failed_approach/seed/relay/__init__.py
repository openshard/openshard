"""relay: a tiny local job queue stored in a tab-separated text file."""

from relay.records import Job, RecordError, format_line, parse_line

__all__ = ["Job", "RecordError", "format_line", "parse_line"]
__version__ = "0.4.2"
