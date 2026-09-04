"""The queue file: a list of jobs on disk."""

from __future__ import annotations

from pathlib import Path

from relay.records import Job, format_line, parse_line

__all__ = ["QueueError", "QueueFile"]


class QueueError(Exception):
    """A queue operation could not be carried out."""


class QueueFile:
    """Jobs stored one per line in a text file.

    Blank lines and lines starting with ``#`` are ignored when reading and
    dropped when the file is rewritten.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> list[Job]:
        if not self.path.exists():
            return []
        jobs: list[Job] = []
        with self.path.open(encoding="utf-8") as fh:
            for number, line in enumerate(fh, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                try:
                    jobs.append(parse_line(line))
                except ValueError as exc:
                    raise QueueError(f"{self.path}:{number}: {exc}") from exc
        return jobs

    def save(self, jobs: list[Job]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(format_line(job) + "\n" for job in jobs)
        self.path.write_text(text, encoding="utf-8", newline="\n")

    def find(self, name: str) -> Job | None:
        for job in self.load():
            if job.name == name:
                return job
        return None

    def add(self, job: Job) -> None:
        jobs = self.load()
        if any(existing.name == job.name for existing in jobs):
            raise QueueError(f"a job named {job.name!r} already exists")
        jobs.append(job)
        self.save(jobs)

    def remove(self, name: str) -> None:
        jobs = self.load()
        kept = [job for job in jobs if job.name != name]
        if len(kept) == len(jobs):
            raise QueueError(f"no job named {name!r}")
        self.save(kept)

    def ordered(self) -> list[Job]:
        """Jobs in the order they should run: insertion order."""
        return self.load()
