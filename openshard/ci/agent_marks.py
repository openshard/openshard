"""Openshard agent marks in commit messages (and a pull request body).

The merge gate asks one question of a pull request that has no Receipt: did
an agent leave its mark on these commits? The marks are the trailers Core
and the cloud-receipts workflow already define:

* ``Openshard-Agent`` (and the other ``Openshard-*`` metadata the workflow
  reads: ``Openshard-Model``, ``Openshard-Provider``, ``Openshard-Surface``,
  ``Openshard-Cost-USD``);
* the usage trailers ``openshard usage trailers`` writes
  (``adapters/commit_usage_trailers.py``: ``Openshard-Tokens-*``,
  ``Openshard-Usage-Scope``);
* ``Claude-Session: https://claude.ai/code/session_...``, which Claude Code
  adds to commits it makes in a cloud session.

A mark is a claim that an agent was involved. Here it only ever makes the
gate stricter (an agent change with no Receipt fails), so a claim is enough:
whether the Platform corroborated the session does not matter for that.
Keys match case-insensitively, as the workflow's ``grep -i`` does. Pure.
"""

from __future__ import annotations

import re

from openshard.adapters.commit_usage_trailers import SCOPE_KEY, TRAILER_KEYS

_METADATA_KEYS = (
    "Openshard-Agent",
    "Openshard-Model",
    "Openshard-Provider",
    "Openshard-Surface",
    "Openshard-Cost-USD",
    SCOPE_KEY,
    *(key for _, key in TRAILER_KEYS),
)
_KEY_RE = re.compile(
    r"^(" + "|".join(re.escape(k) for k in sorted(_METADATA_KEYS, key=len, reverse=True)) + r"):[ \t]*\S",
    re.IGNORECASE | re.MULTILINE,
)
_CLAUDE_SESSION_RE = re.compile(
    r"^Claude-Session:[ \t]*https://claude\.ai/code/session_[A-Za-z0-9]{8,128}[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_CANONICAL = {k.lower(): k for k in _METADATA_KEYS}


def agent_marks(text: object) -> list[str]:
    """The distinct agent-mark trailer names in *text*, in first-seen order. Never raises."""
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    for match in _KEY_RE.finditer(text):
        name = _CANONICAL.get(match.group(1).lower(), match.group(1))
        if name not in found:
            found.append(name)
    if _CLAUDE_SESSION_RE.search(text):
        found.append("Claude-Session")
    return found


__all__ = ["agent_marks"]
