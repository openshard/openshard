"""``Parser`` protocol and the shared defensive JSONL reader.

Parsers are pure: no IO beyond the stream they are handed, no storage, no
git. They tolerate anything: a malformed, truncated or oversized line, or a
record of an unknown type, is counted in ``ParsedSession.losses`` and the
parse continues. Only a source that is not this format at all raises
:class:`ParseError`, which the job runner quarantines (no content kept).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from typing import Any, BinaryIO, Protocol, runtime_checkable

from openshard.ingest.model import ParsedSession, SourceObject

MAX_LINE_BYTES = 8 * 1024 * 1024
HEAD_BYTES = 256 * 1024
CHECKPOINT_EVERY = 500

LOSS_MALFORMED_LINE = "malformed_line"
LOSS_OVERSIZED_LINE = "oversized_line"
LOSS_NON_OBJECT = "non_object_record"

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# ``git commit`` prints ``[branch 1a2b3c4] subject`` / ``[main (root-commit) 1a2b3c4] subject``.
_GIT_COMMIT_OUTPUT_RE = re.compile(r"^\[[^\]\s]+(?: \([a-z -]+\))? ([0-9a-f]{7,40})\] ", re.MULTILINE)
# Agent prose such as "committed 1a2b3c4" / "commit `1a2b3c4d`" -- a claim only.
_CLAIMED_COMMIT_RE = re.compile(r"\bcommit(?:ted)?(?: as)?\s+`?([0-9a-f]{7,40})\b`?", re.IGNORECASE)


class ParseError(ValueError):
    """The source is not in this parser's format (quarantined, never retried)."""


@runtime_checkable
class Parser(Protocol):
    name: str
    version: int

    def sniff(self, head: bytes, obj: SourceObject | None = None) -> float: ...

    def peek(self, head: bytes) -> dict: ...

    def parse(
        self, stream: BinaryIO, obj: SourceObject, *, checkpoint: Callable[[], None] | None = None
    ) -> Iterator[ParsedSession]: ...


def parser_id(parser: Parser) -> str:
    return f"{parser.name}@{parser.version}"


def iter_json_lines(
    stream: BinaryIO,
    session: ParsedSession,
    *,
    checkpoint: Callable[[], None] | None = None,
    max_line_bytes: int = MAX_LINE_BYTES,
) -> Iterator[tuple[int, dict]]:
    """Yield ``(line_number, record)`` for every well-formed JSON object line.

    Oversized lines are skipped without being held in memory in full; every
    skipped line is counted as a loss on *session*. ``checkpoint`` is called
    every :data:`CHECKPOINT_EVERY` lines so a long stream stays cancellable.
    """
    lineno = 0
    while True:
        raw = stream.readline(max_line_bytes + 1)
        if not raw:
            return
        lineno += 1
        if checkpoint is not None and lineno % CHECKPOINT_EVERY == 0:
            checkpoint()
        if len(raw) > max_line_bytes and not raw.endswith(b"\n"):
            # Drain the rest of this line in bounded chunks.
            while True:
                more = stream.readline(max_line_bytes)
                if not more or more.endswith(b"\n"):
                    break
            session.add_loss(LOSS_OVERSIZED_LINE)
            continue
        text = raw.strip()
        if not text:
            continue
        try:
            record = json.loads(text.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            session.add_loss(LOSS_MALFORMED_LINE)
            continue
        if not isinstance(record, dict):
            session.add_loss(LOSS_NON_OBJECT)
            continue
        session.record_count += 1
        yield lineno, record


def head_records(head: bytes, limit: int = 40) -> list[dict]:
    """Best-effort JSON objects from the first lines of *head* (for sniff/peek)."""
    out: list[dict] = []
    for line in head.splitlines()[:limit]:
        try:
            rec = json.loads(line.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def as_dict(value: object) -> dict[str, Any]:
    """*value* when it is a JSON object, else an empty dict (never guesses)."""
    return value if isinstance(value, dict) else {}


def as_str(value: object) -> str | None:
    """*value* when it is a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None


def text_str(value: object, cap: int = 200_000) -> str | None:
    return value[:cap] if isinstance(value, str) and value else None


def git_commit_shas(output: object) -> list[str]:
    """SHAs that ``git commit`` printed in a tool's output (transient text)."""
    if not isinstance(output, str) or "]" not in output:
        return []
    return list(dict.fromkeys(m.group(1).lower() for m in _GIT_COMMIT_OUTPUT_RE.finditer(output[:200_000])))


def claimed_commit_shas(text: object) -> list[str]:
    """SHAs an agent *claims* in prose ("committed 1a2b3c4") -- agent_reported only."""
    if not isinstance(text, str):
        return []
    return list(dict.fromkeys(m.group(1).lower() for m in _CLAIMED_COMMIT_RE.finditer(text[:200_000])))


def is_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.match(value.lower()))


def ref(lineno: int | None) -> str | None:
    return f"L{lineno}" if isinstance(lineno, int) else None


def span(first: int | None, last: int | None) -> str | None:
    if first is None:
        return None
    return f"L{first}" if last in (None, first) else f"L{first}-L{last}"
