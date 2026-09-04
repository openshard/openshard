"""Job records and the tab-separated line format used by queue files.

A queue file holds one job per line, fields in schema order separated by a
tab. Trailing optional fields whose value equals the default are omitted
when writing and filled in when reading, which is what keeps files
readable across releases (see CONTRIBUTING.md, "Compatibility rules").
"""

from __future__ import annotations

from typing import Any

from relay._schema import FIELDS, SCHEMA_VERSION, Field

SEPARATOR = "\t"

__all__ = ["SCHEMA_VERSION", "SEPARATOR", "Job", "RecordError", "format_line", "parse_line"]


class RecordError(ValueError):
    """A job record is missing a required field or holds a bad value."""


def _coerce(field: Field, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and SEPARATOR in value:
        raise RecordError(f"field {field.name!r} must not contain a tab")
    try:
        return field.kind(value)
    except (TypeError, ValueError) as exc:
        raise RecordError(f"field {field.name!r}: {value!r} is not a valid {field.kind.__name__}") from exc


class Job:
    """One job. Attributes are the schema fields, in schema order."""

    __slots__ = tuple(field.name for field in FIELDS)

    def __init__(self, **values: Any) -> None:
        for field in FIELDS:
            if field.name in values:
                value = values.pop(field.name)
            elif field.required:
                raise RecordError(f"missing required field: {field.name}")
            else:
                value = field.default
            if value is None and field.required:
                raise RecordError(f"missing required field: {field.name}")
            setattr(self, field.name, _coerce(field, value))
        if values:
            raise RecordError(f"unknown field(s): {', '.join(sorted(values))}")

    def to_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in FIELDS}

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Job) and other.to_dict() == self.to_dict()

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}={v!r}" for k, v in self.to_dict().items())
        return f"Job({inner})"


def parse_line(line: str) -> Job:
    """Parse one queue line.

    Lines written by an older release may have fewer columns than the
    current schema; the missing trailing fields take their defaults.
    """
    parts = line.rstrip("\r\n").split(SEPARATOR)
    if len(parts) > len(FIELDS):
        raise RecordError(f"expected at most {len(FIELDS)} columns, got {len(parts)}")
    values = {field.name: raw for field, raw in zip(FIELDS, parts)}
    return Job(**values)


def format_line(job: Job) -> str:
    """Serialise *job* as one queue line.

    Trailing optional fields holding their default are omitted so a record
    that does not use newer fields serialises exactly as older releases
    wrote it.
    """
    columns = [str(getattr(job, field.name)) for field in FIELDS]
    while columns:
        field = FIELDS[len(columns) - 1]
        if field.required or getattr(job, field.name) != field.default:
            break
        columns.pop()
    return SEPARATOR.join(columns)
