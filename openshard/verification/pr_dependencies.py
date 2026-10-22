"""New dependencies: packages a change adds to its manifests.

Every new package is new code from outside the repository, and a freshly
published or hijacked one is a supply-chain risk. This module reads the
manifests a change touched, at base and at head, and names the packages that
appear only at head:

* ``pyproject.toml``: ``[project] dependencies`` and ``optional-dependencies``,
  ``[dependency-groups]``, and Poetry's dependency tables;
* ``requirements*.txt`` / ``requirements*.in`` (and files under a
  ``requirements/`` directory);
* ``package.json``: ``dependencies``, ``devDependencies``,
  ``peerDependencies`` and ``optionalDependencies``.

A version bump of an existing package is not new. Lockfile changes are
listed by path only. A manifest that does not parse on either side is
skipped, never read as "no dependencies". Whether a package was published
recently is a separate lookup.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import Any

MAX_PACKAGES = 10
_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_NPM_FIELDS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
_POETRY_SKIP = frozenset({"python"})

Show = Callable[[str, str], "str | None"]


def normalize_python(name: str) -> str:
    """PEP 503 normalised project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _python_name(spec: object) -> str | None:
    if not isinstance(spec, str):
        return None
    m = _NAME_RE.match(spec)
    return normalize_python(m.group(1)) if m else None


def _pyproject(text: str) -> set[str] | None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    specs: list[object] = []
    raw = data.get("project")
    project: dict[str, Any] = raw if isinstance(raw, dict) else {}
    specs += project.get("dependencies") or []
    for group in (project.get("optional-dependencies") or {}).values():
        specs += group if isinstance(group, list) else []
    for group in (data.get("dependency-groups") or {}).values():
        specs += group if isinstance(group, list) else []
    names = {n for s in specs if (n := _python_name(s))}
    poetry = (data.get("tool") or {}).get("poetry") or {}
    tables = [poetry.get("dependencies"), poetry.get("dev-dependencies")]
    tables += [g.get("dependencies") for g in (poetry.get("group") or {}).values() if isinstance(g, dict)]
    for table in tables:
        if isinstance(table, dict):
            names |= {normalize_python(k) for k in table if k not in _POETRY_SKIP}
    return names


def _requirements(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-") and "://" not in line and (n := _python_name(line)):
            names.add(n)
    return names


def _package_json(text: str) -> set[str] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {k for f in _NPM_FIELDS if isinstance(data.get(f), dict) for k in data[f]}


def manifest_kind(path: str) -> str | None:
    p = PurePosixPath(path)
    if p.name == "pyproject.toml":
        return "pyproject"
    if p.suffix in (".txt", ".in") and (p.name.startswith("requirements") or "requirements" in p.parts[:-1]):
        return "requirements"
    if p.name == "package.json" and "node_modules" not in p.parts:
        return "package_json"
    return None


_READERS: dict[str, Callable[[str], set[str] | None]] = {
    "pyproject": _pyproject, "requirements": _requirements, "package_json": _package_json,
}
_ECOSYSTEM = {"pyproject": "pypi", "requirements": "pypi", "package_json": "npm"}


def new_dependencies(files: Iterable[Any], show: Show, base_rev: str, head_rev: str) -> list[dict[str, str]]:
    """``{"name", "ecosystem", "manifest"}`` for each package a changed manifest adds, sorted, once each."""
    found: dict[tuple[str, str], str] = {}
    for f in files:
        kind = manifest_kind(f.path)
        if kind is None or f.status == "D":
            continue
        read = _READERS[kind]
        old = f.old_path or f.path
        before_text = show(base_rev, old) if f.status != "A" else ""
        after_text = show(head_rev, f.path)
        if before_text is None or after_text is None:
            continue
        before, after = read(before_text), read(after_text)
        if before is None or after is None:
            continue  # unparseable on one side: nothing can be said
        for name in sorted(after - before):
            found.setdefault((_ECOSYSTEM[kind], name), f.path)
    return [{"name": n, "ecosystem": e, "manifest": m} for (e, n), m in sorted(found.items())]


__all__ = ["MAX_PACKAGES", "manifest_kind", "new_dependencies", "normalize_python"]
