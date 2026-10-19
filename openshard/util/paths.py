"""One spelling of a local filesystem path for comparing, hashing and keying.

The same directory reaches OpenShard spelled many ways, and on Windows the
differences are only spelling: ``C:\\Users\\RUNNER~1`` and
``C:\\Users\\runneradmin`` (8.3 short names), ``c:`` and ``C:``, ``\\`` and
``/`` (Git prints ``C:/...``), a trailing separator. Anything that compares
two paths, or keys state by one, uses :func:`path_key` so those spellings
agree. It is a key, never a path to display or open.
"""

from __future__ import annotations

import os

_pathmod = os.path  # this OS's path rules (a test swaps in ``ntpath`` to check Windows spellings)


def path_key(path: object) -> str:
    """*path* resolved (symlinks, short names), normalised and case-folded the way this OS compares paths.

    Accepts ``str``, ``bytes`` or any path-like object. Never raises: a path
    that cannot be resolved is normalised as given. On POSIX the result is the
    real path (case is significant there); on Windows it is also lower-cased
    with ``\\`` separators (``os.path.normcase``).
    """
    if isinstance(path, str):
        text = path
    elif isinstance(path, bytes):
        text = path.decode("utf-8", "replace")
    else:
        fspath = getattr(path, "__fspath__", None)
        text = str(fspath()) if callable(fspath) else str(path)
    try:
        text = _pathmod.realpath(text)
    except (OSError, ValueError):
        pass
    return _pathmod.normcase(_pathmod.normpath(text))


def is_absolute(text: str) -> bool:
    """Whether *text* is an absolute path on this OS (``C:\\x`` or ``C:/x`` on Windows)."""
    return _pathmod.isabs(text)
