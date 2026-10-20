"""The ``merge_check`` block: how a Receipt's HEAD compared with the default branch.

Core computes it on the machine that ran the agent (``adapters/merge_check.py``)
when a Receipt is sealed, and records facts only: refs, commit shas, author
names, commit subject lines, repository-relative paths and line numbers. Never
diff text or file contents. This module is the shape and privacy boundary: the
writer passes its result through :func:`normalize` before it is stored, and the
projection re-validates the stored block on the way out (``merge_check_block``),
so a hand-edited or malformed record can widen nothing.

Unknown stays unknown: ``behind_by``, ``base_commits`` and ``conflicts`` are
``None`` when git could not say; ``conflicts == []`` means checked and clean.
An absent block means "not checked". Wire shape: the shared timeline contract
(Platform receipt sync, ``merge_check``).
"""

from __future__ import annotations

import re
from typing import Any

from openshard.history.receipt_evidence import unsafe_text

MAX_BASE_COMMITS = 20
MAX_CONFLICT_PATHS = 50
MAX_LINES_PER_PATH = 20
MAX_SUBJECT = 200
MAX_AUTHOR = 200
MAX_PATH = 1024

_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def sha_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA_RE.match(value) else None


def one_line(value: object, limit: int) -> str | None:
    """*value* as one bounded line (newlines and control characters become spaces), else None."""
    if not isinstance(value, str):
        return None
    text = " ".join(_CONTROL_RE.sub(" ", value).split())
    if not text or unsafe_text(text):
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _stamp(value: object) -> str | None:
    return value if isinstance(value, str) and _STAMP_RE.match(value) else None


def safe_path(value: object) -> str | None:
    """A repository-relative path as git prints it; absolute, parent-relative or control-character paths are None."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH or _CONTROL_RE.search(value):
        return None
    if unsafe_text(value) or ".." in value.replace("\\", "/").split("/"):
        return None
    return value


def _commits(raw: object) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for item in raw:
        sha = sha_or_none(item.get("sha")) if isinstance(item, dict) else None
        if sha is None:
            continue
        out.append({"sha": sha, "author": one_line(item.get("author"), MAX_AUTHOR),
                    "subject": one_line(item.get("subject"), MAX_SUBJECT), "committed_at": _stamp(item.get("committed_at"))})
        if len(out) >= MAX_BASE_COMMITS:
            break
    return out


def _conflicts(raw: object) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for item in raw:
        path = safe_path(item.get("path")) if isinstance(item, dict) else None
        if path is None or not isinstance(item, dict):
            continue
        lines = item.get("lines")
        numbers = sorted({n for n in lines if _count(n)} if isinstance(lines, list) else set())
        base_sha = sha_or_none(item.get("base_sha"))
        out.append({"path": path, "lines": numbers[:MAX_LINES_PER_PATH],
                    "base_author": one_line(item.get("base_author"), MAX_AUTHOR) if base_sha else None,
                    "base_sha": base_sha})
        if len(out) >= MAX_CONFLICT_PATHS:
            break
    return out if out or not raw else None  # conflicts none of which can be shown are unknown, never clean


def normalize(raw: object) -> dict[str, Any] | None:
    """The contract shape of *raw*, bounded; None unless the refs and the check time are well formed."""
    if not isinstance(raw, dict):
        return None
    base_ref = raw.get("base_ref")
    base_sha, head_sha, checked_at = sha_or_none(raw.get("base_sha")), sha_or_none(raw.get("head_sha")), _stamp(raw.get("checked_at"))
    if not isinstance(base_ref, str) or not _REF_RE.match(base_ref) or ".." in base_ref:
        return None
    if base_sha is None or head_sha is None or checked_at is None:
        return None
    return {
        "base_ref": base_ref,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "checked_at": checked_at,
        "behind_by": _count(raw.get("behind_by")),
        "base_commits": _commits(raw.get("base_commits")),
        "conflicts": _conflicts(raw.get("conflicts")),
    }


def merge_check_block(entry: dict) -> dict[str, Any] | None:
    """The stored ``merge_check`` of *entry*, re-validated; None when the record has none."""
    return normalize(entry.get("merge_check"))


__all__ = ["MAX_BASE_COMMITS", "MAX_CONFLICT_PATHS", "MAX_LINES_PER_PATH", "MAX_SUBJECT", "merge_check_block",
           "normalize", "one_line", "safe_path", "sha_or_none"]
