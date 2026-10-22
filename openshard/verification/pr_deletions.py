"""Fix by deletion: public code, endpoints and tests that a change removes.

A bug fix that makes a failure go away by deleting the code, route or test
that showed it is a known agent shortcut. This module compares the base and
head versions of the files a change touched and reports what disappeared:

* public Python functions and classes (top level, and public methods of
  public classes) that no changed file defines at head. A definition moved
  to another changed file is not a removal;
* web routes: Python route decorators (``@app.get("/x")``, ``@router.post``,
  ``@bp.route``) and Express-style calls (``app.get("/x", ...)``);
* tests: Python ``test_*`` functions, JavaScript and TypeScript ``it(...)`` /
  ``test(...)`` calls (counted, never named), and deleted test files.

Whether the change reads as a bug fix is decided from its commit subjects
(and any title the caller passes), and the basis is recorded. Names are kept
(bounded); source text never is. A file that does not parse is skipped,
never counted as a removal.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

MAX_NAMES = 5
_NAME_CAP = 80
_FIX_RE = re.compile(r"\b(fix(es|ed|ing)?|bug(fix)?|hotfix|regression|broken|crash(es)?)\b", re.IGNORECASE)
_ROUTE_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "route", "api_route",
                            "websocket", "all"})
_JS_ROUTE_RE = re.compile(r"\b(?:app|router|server|api)\.(get|post|put|patch|delete|all)\(\s*(['\"`])([^'\"`]{1,200})\2")
_JS_TEST_RE = re.compile(r"(?:^|[^.\w])(?:it|test)(?:\.only|\.each\([^)]*\))?\(\s*(['\"`])")
_JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")

Show = Callable[[str, str], "str | None"]


@dataclass
class Surface:
    """What one side of the change exposes: public names, routes, test names and an unnamed JS test count."""

    public: set[str] = field(default_factory=set)
    routes: set[str] = field(default_factory=set)
    tests: set[str] = field(default_factory=set)
    js_tests: int = 0


def _public(name: str) -> bool:
    return not name.startswith("_")


def _route_of(decorator: ast.expr) -> str | None:
    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
        return None
    method = decorator.func.attr.lower()
    if method not in _ROUTE_METHODS or not decorator.args:
        return None
    first = decorator.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return None
    return f"{method.upper()} {first.value[:_NAME_CAP]}"


def _python_surface(text: str, surface: Surface, *, is_test: bool) -> bool:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if is_test and node.name.startswith("test"):
                surface.tests.add(node.name)
            elif not is_test and _public(node.name):
                surface.public.add(node.name)
            surface.routes.update(r for d in node.decorator_list if (r := _route_of(d)))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if not isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if is_test and item.name.startswith("test"):
                    surface.tests.add(f"{node.name}.{item.name}")
                elif not is_test and _public(node.name) and _public(item.name):
                    surface.public.add(f"{node.name}.{item.name}")
                surface.routes.update(r for d in item.decorator_list if (r := _route_of(d)))
            if not is_test and _public(node.name):
                surface.public.add(node.name)
    return True


def _js_surface(text: str, surface: Surface, *, is_test: bool) -> bool:
    surface.routes.update(f"{m.group(1).upper()} {m.group(3)[:_NAME_CAP]}" for m in _JS_ROUTE_RE.finditer(text))
    if is_test:
        surface.js_tests += len(_JS_TEST_RE.findall(text))
    return True


def surface_of(path: str, text: str | None) -> Surface | None:
    """What *path*'s *text* exposes; None when it could not be read or parsed (never "nothing")."""
    from openshard.verification.gaming_signals import is_test_path

    if text is None:
        return None
    surface, test = Surface(), is_test_path(path)
    if path.endswith(".py"):
        return surface if _python_surface(text, surface, is_test=test) else None
    if path.endswith(_JS_SUFFIXES):
        return surface if _js_surface(text, surface, is_test=test) else None
    return surface


def _merge(into: Surface, part: Surface) -> None:
    into.public |= part.public
    into.routes |= part.routes
    into.tests |= part.tests
    into.js_tests += part.js_tests


def reads_as_fix(texts: Iterable[str]) -> str | None:
    """The first commit subject or title that reads as a bug fix, or None."""
    for text in texts:
        first = text.strip().splitlines()[0] if text.strip() else ""
        if _FIX_RE.search(first):
            return first[:_NAME_CAP]
    return None


def removals(files: Iterable[Any], show: Show, base_rev: str, head_rev: str) -> dict[str, Any] | None:
    """What the change removed, or None when it removed nothing.

    *files* are ``pr_findings.ChangedFile``s. Both sides are read for every
    changed file, so a definition moved between changed files cancels out.
    """
    from openshard.verification.gaming_signals import is_test_path

    before, after = Surface(), Surface()
    deleted_tests: list[str] = []
    for f in files:
        old = f.old_path or f.path
        was = surface_of(old, show(base_rev, old)) if f.status != "A" else Surface()
        now = surface_of(f.path, show(head_rev, f.path)) if f.status != "D" else Surface()
        if was is None or now is None:
            continue  # one side unreadable: nothing can be said about this file
        _merge(before, was)
        _merge(after, now)
        if f.status == "D" and is_test_path(old):
            deleted_tests.append(old)
    gone_public = sorted(before.public - after.public)
    gone_routes = sorted(before.routes - after.routes)
    gone_tests = sorted(before.tests - after.tests)
    gone_js_tests = max(0, before.js_tests - after.js_tests)
    if not (gone_public or gone_routes or gone_tests or gone_js_tests or deleted_tests):
        return None
    return {
        "removed_functions": len(gone_public),
        "removed_endpoints": len(gone_routes),
        "removed_tests": len(gone_tests) + gone_js_tests,
        "deleted_test_files": len(deleted_tests),
        "names": (gone_routes + gone_public + gone_tests)[:MAX_NAMES],
        "paths": deleted_tests[:MAX_NAMES],
    }


def describe(removed: dict[str, Any]) -> str:
    parts = []
    for key, label in (("removed_functions", "public name"), ("removed_endpoints", "endpoint"),
                       ("removed_tests", "test"), ("deleted_test_files", "test file")):
        n = removed[key]
        if n:
            parts.append(f"{n} {label}{'' if n == 1 else 's'}")
    return ", ".join(parts)


__all__ = ["Surface", "describe", "reads_as_fix", "removals", "surface_of"]
