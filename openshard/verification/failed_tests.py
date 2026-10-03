"""Failing test identifiers from verifier output that OpenShard itself ran.

Only structured identifiers are kept, never output text: a pytest node id
(``tests/test_layout.py::test_mobile``, class-qualified ids included) or a
jest/vitest test file (``src/layout.test.tsx``). Parametrisation values
(``test_x[<value>]``) are dropped because they can carry arbitrary data.
Paths must be repo-relative. Like ``sanitize_path``, the credential-shaped
secret patterns apply but the generic "long opaque run" one does not: it
matches ordinary descriptive names such as
``test_mobile_viewport_rejects_non_positive_widths``. Instead a segment is
rejected when it is long with no underscore, or digit-heavy, which is what an
opaque token looks like. At most ``MAX_FAILED_TESTS`` ids, in first-seen
order. Pure; never raises.
"""
from __future__ import annotations

import re

from openshard.safety.sanitize import SECRET_PATTERNS, is_absolute_path

MAX_FAILED_TESTS = 5
MAX_ID_CHARS = 160

_PYTEST = re.compile(r"^(?:FAILED|ERROR)\s+([\w./\\-]+\.py)((?:::[A-Za-z_]\w*)+)(?:\[[^\]\n]*\])?", re.M)
_JS = re.compile(r"^\s*(?:FAIL|×|✕)\s+([\w./\\-]+\.(?:test|spec)\.[cm]?[jt]sx?)\b", re.M)
# Colour (FORCE_COLOR) output: OSC sequences such as hyperlinks (ESC]...BEL or
# ESC]...ESC\), CSI sequences (ESC[ params final-byte) and other two-byte escapes.
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def _safe_path(path: str) -> str | None:
    norm = path.replace("\\", "/")
    if is_absolute_path(norm) or norm.startswith("~") or ".." in norm.split("/"):
        return None
    return norm


_SAFE_ID = re.compile(r"^[\w./-]+(?:::[A-Za-z_]\w*)*$")


def safe_test_id(ident: object) -> str | None:
    """*ident* if it is a safe, repo-relative test identifier, else None."""
    if not isinstance(ident, str) or not ident or len(ident) > MAX_ID_CHARS or not _SAFE_ID.match(ident):
        return None
    path = _safe_path(ident.split("::", 1)[0])
    if path is None or any(p.search(ident) for p in SECRET_PATTERNS[:-1]):
        return None
    for seg in re.split(r"[/.:]+", ident):
        if (len(seg) >= 32 and "_" not in seg) or sum(ch.isdigit() for ch in seg) >= 8:
            return None
    return ident


def failing_test_ids(output: str | None) -> list[str]:
    """Failing test ids named in *output*, bounded and privacy-safe."""
    if not output:
        return []
    found: list[tuple[int, str]] = []
    try:
        output = _ANSI.sub("", output)
        for m in _PYTEST.finditer(output):
            path = _safe_path(m.group(1))
            if path:
                found.append((m.start(), path + m.group(2)))
        for m in _JS.finditer(output):
            path = _safe_path(m.group(1))
            if path:
                found.append((m.start(), path))
    except Exception:
        return []
    out: list[str] = []
    for _, ident in sorted(found):
        if safe_test_id(ident) is None or ident in out:
            continue
        out.append(ident)
        if len(out) >= MAX_FAILED_TESTS:
            break
    return out


__all__ = ["MAX_FAILED_TESTS", "failing_test_ids", "safe_test_id"]
