"""A line-numbered outline of a source file: where its definitions start.

For a file too large to show whole, the outline tells the executor which
line ranges to read instead of paging through it. It is derived from the
file's text by simple patterns (top-level and nested ``def`` / ``class`` in
Python; ``function`` / ``class`` / exported bindings in JavaScript and
TypeScript); for other languages there is no outline and the file is left
to ``read_file`` with ranges. Nothing here is model output.
"""
from __future__ import annotations

import re

MAX_OUTLINE_ENTRIES = 120

_PY_DEF = re.compile(r"^(?P<indent>[ \t]*)(?:async\s+)?(?P<kind>def|class)\s+(?P<name>\w+)")
_JS_DEF = re.compile(
    r"^(?P<indent>[ \t]*)(?:export\s+(?:default\s+)?)?"
    r"(?:(?P<kind>function|class)\s+(?P<name>\w+)"
    r"|(?:const|let|var)\s+(?P<binding>\w+)\s*=\s*(?:async\s*)?(?:\(|function\b|\w+\s*=>))"
)

PY_SUFFIXES = (".py", ".pyi")
JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")


def outline_entries(text: str, path: str, *, max_entries: int = MAX_OUTLINE_ENTRIES) -> list[tuple[int, str]]:
    """``(line number, signature)`` for each definition, in file order, bounded. Empty for unknown languages."""
    lower = path.lower()
    if lower.endswith(PY_SUFFIXES):
        pattern, max_depth = _PY_DEF, 1
    elif lower.endswith(JS_SUFFIXES):
        pattern, max_depth = _JS_DEF, 1
    else:
        return []
    out: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        m = pattern.match(line)
        if not m:
            continue
        indent = m.group("indent").expandtabs(4)
        depth = len(indent) // 4
        if depth > max_depth:
            continue
        signature = line.strip()
        if len(signature) > 100:
            signature = signature[:97] + "..."
        out.append((number, ("  " * depth) + signature))
        if len(out) >= max_entries:
            break
    return out


def render_outline(text: str, path: str, *, max_entries: int = MAX_OUTLINE_ENTRIES) -> str:
    """The outline as the model sees it: one ``<line>: <signature>`` per definition, or '' when there is none."""
    entries = outline_entries(text, path, max_entries=max_entries)
    if not entries:
        return ""
    total = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    width = len(str(entries[-1][0]))
    body = "\n".join(f"{n:>{width}}: {sig}" for n, sig in entries)
    more = "" if len(entries) < max_entries else f"\n... (outline capped at {max_entries} definitions)"
    return f"{total} lines; definitions start at:\n{body}{more}"


def top_level_symbols(text: str, path: str, *, max_symbols: int = MAX_OUTLINE_ENTRIES) -> list[str]:
    """The names defined at the top level of a file (``class Receipt``, ``async def fetch``,
    ``function load``, ``const save``), in file order, bounded. Empty for unknown languages."""
    lower = path.lower()
    if lower.endswith(PY_SUFFIXES):
        pattern = _PY_DEF
    elif lower.endswith(JS_SUFFIXES):
        pattern = _JS_DEF
    else:
        return []
    out: list[str] = []
    for line in text.splitlines():
        m = pattern.match(line)
        if not m or m.group("indent"):
            continue
        kind, name = m.group("kind"), m.group("name")
        if pattern is _PY_DEF:
            label = ("async " if line.lstrip().startswith("async") else "") + f"{kind} {name}"
        elif kind:
            label = f"{kind} {name}"
        else:
            label = f"const {m.group('binding')}"
        out.append(label)
        if len(out) >= max_symbols:
            break
    return out


__all__ = ["MAX_OUTLINE_ENTRIES", "outline_entries", "render_outline", "top_level_symbols"]
