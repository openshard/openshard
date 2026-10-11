"""Terminal display helpers for the run timeline.

The stored timeline schema lives in :mod:`openshard.history.timeline`; its
names are re-exported here so existing imports keep working.
"""

from __future__ import annotations

import sys

from openshard.history.timeline import (  # noqa: F401  (re-exported)
    _ALLOWED_KINDS,
    _ALLOWED_STATUSES,
    _MAX_DETAIL_CHARS,
    _MAX_LABEL_CHARS,
    _MAX_TARGET_CHARS,
    _MAX_TIMELINE_EVENTS,
    TIMELINE_SCHEMA_VERSION,
    RunTimelineEvent,
    make_timeline_event,
    normalize_timeline,
    project_timeline_for_export,
)


def _stdout_supports_unicode() -> bool:
    try:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        "━—✖⚠✓".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def timeline_symbol(status: str) -> str:
    """Return the display symbol for a run event status. Checks stdout encoding each call."""
    _ok = _stdout_supports_unicode()
    return {
        "completed": "✓" if _ok else "+",
        "failed": "✖" if _ok else "x",
        "skipped": "-",
        "started": "→" if _ok else ">",
    }.get(status, "✓" if _ok else "+")
