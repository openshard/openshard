"""Which guards a change calls for: path-pattern matching against changed files.

Patterns are repository-relative posix globs:

* ``*`` matches within one path segment, ``?`` one character;
* ``**`` matches any number of whole segments (including none);
* a pattern ending in ``/`` matches everything under that folder.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import PurePosixPath

from openshard.guards.model import Guard


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    pat = pattern.strip()
    if pat.endswith("/"):
        pat += "**"
    parts = pat.split("/")
    out: list[str] = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**":
            # any number of whole segments; as the last part, anything below
            out.append(".*" if last else "(?:[^/]+/)*")
            continue
        seg = ""
        for ch in part:
            if ch == "*":
                seg += "[^/]*"
            elif ch == "?":
                seg += "[^/]"
            else:
                seg += re.escape(ch)
        out.append(seg if last else seg + "/")
    return re.compile("^" + "".join(out) + "$")


def path_matches(pattern: str, path: str | PurePosixPath) -> bool:
    """Whether repository-relative *path* falls under *pattern*."""
    text = path.as_posix() if isinstance(path, PurePosixPath) else str(path)
    return bool(_compile(pattern).match(text))


def guards_for_change(
    guards: Iterable[Guard], changed: Iterable[str | PurePosixPath],
) -> list[tuple[Guard, str]]:
    """Runnable guards whose scope covers a changed path, with the first path that matched.

    Order follows *guards*. Notes, proposed and retired guards never match.
    """
    paths = [p.as_posix() if isinstance(p, PurePosixPath) else str(p) for p in changed]
    out: list[tuple[Guard, str]] = []
    for guard in guards:
        if not guard.runnable:
            continue
        hit = next((p for p in paths if any(path_matches(pat, p) for pat in guard.paths)), None)
        if hit is not None:
            out.append((guard, hit))
    return out
