"""Change-aware check selection: which checks a commit's changes call for.

``openshard verify`` used to pick one command for a whole repository (its
configured contract, else one detected test command). That is too much for a
large suite and too little for a TypeScript change (no typecheck). This module
selects checks from what the change touched:

* **JavaScript / TypeScript** -- for each changed file, every ``package.json``
  between the file and the repository root contributes its ``typecheck``,
  ``lint`` and ``test`` scripts (nearest first). The package manager is the
  one the repository declares (``packageManager``, else its lockfile).
* **Python** -- for changed ``.py`` files: the test files that name a changed
  module (``tests/**/test_<module>*.py``) plus changed test files, run with
  pytest; and ``ruff check`` on the changed files when the project configures
  ruff. A Python change with no related test falls back to the project's
  whole test command so a change is never "verified" by nothing.

Every planned check records *why* it was selected. Selection never runs
anything and never reads file contents beyond manifests.

Unattended runs (``trusted_definitions=False``) do not execute a check whose
own definition the change rewrote: a ``package.json`` script whose text differs
between the base and the verified commit is planned ``needs_approval``, so an
agent cannot change ``"test"`` to something else and have OpenShard run it
unasked. A person can still run it with ``openshard verify --approve``.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from openshard.verification.plan import CommandSafety

CHECK_SCRIPTS: tuple[str, ...] = ("typecheck", "lint", "test")
_SCRIPT_KIND = {"typecheck": "typecheck", "lint": "lint", "test": "test"}
MAX_TEST_FILES = 40
MAX_LINT_FILES = 60
_PY_PROJECT_FILES = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
_SKIP_PARTS = frozenset({"node_modules", ".git", ".openshard", "dist", "build", ".venv", "venv"})

GitShow = Callable[[str, str], "str | None"]  # (rev, posix path) -> file text at rev, or None


@dataclass(frozen=True)
class SelectedCheck:
    """One check the change calls for. ``cwd`` is repo-relative (posix, "" = root)."""

    argv: tuple[str, ...]
    cwd: str
    kind: str
    label: str
    why: str
    safety_override: str | None = None  # needs_approval when the definition changed
    override_reason: str = ""


# ---------------------------------------------------------------------------
# JavaScript / TypeScript
# ---------------------------------------------------------------------------


def _package_manager(manifest: dict | None, root: Path) -> list[str]:
    declared = (manifest or {}).get("packageManager")
    name = declared.split("@", 1)[0] if isinstance(declared, str) else ""
    if not name:
        if (root / "pnpm-lock.yaml").is_file():
            name = "pnpm"
        elif (root / "yarn.lock").is_file():
            name = "yarn"
        else:
            name = "npm"
    return [name] if name in ("pnpm", "yarn", "npm", "bun") else ["npm"]


def _read_manifest_text(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _scripts(manifest: dict | None) -> dict[str, str]:
    raw = (manifest or {}).get("scripts")
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(raw, dict) else {}


def _manifest_dirs(path: PurePosixPath) -> list[PurePosixPath]:
    """The directories from *path*'s parent up to the root, nearest first."""
    out: list[PurePosixPath] = []
    current = path.parent
    while True:
        out.append(current)
        if current == PurePosixPath("."):
            return out
        current = current.parent


def _js_checks(
    root: Path, changed: list[PurePosixPath], git_show: GitShow, base: str | None, commit: str,
) -> list[SelectedCheck]:
    js = [p for p in changed if not (set(p.parts) & _SKIP_PARTS)]
    if not js:
        return []
    manifests: dict[str, dict | None] = {}

    def manifest_at(rel: PurePosixPath) -> dict | None:
        key = rel.as_posix()
        if key not in manifests:
            manifests[key] = _read_manifest_text(git_show(commit, (rel / "package.json").as_posix() if key != "." else "package.json"))
        return manifests[key]

    root_manifest = manifest_at(PurePosixPath("."))
    if root_manifest is None:
        return []  # not a JavaScript project at this commit
    pm = _package_manager(root_manifest, root)
    out: list[SelectedCheck] = []
    seen: set[tuple[str, str]] = set()
    for path in js:
        for rel in _manifest_dirs(path):
            manifest = manifest_at(rel)
            if manifest is None:
                continue
            scripts = _scripts(manifest)
            for script in CHECK_SCRIPTS:
                if script not in scripts:
                    continue
                cwd = "" if rel == PurePosixPath(".") else rel.as_posix()
                if (cwd, script) in seen:
                    continue
                seen.add((cwd, script))
                where = cwd or "repository root"
                override, why_override = None, ""
                # A consistency guard, not a security boundary (tests run the
                # change's own code anyway): the check as defined before the
                # work, including the pre/post scripts npm and yarn run with it.
                before = _scripts(_read_manifest_text(
                    git_show(base or f"{commit}^", f"{cwd}/package.json" if cwd else "package.json")))
                if any(before.get(name) != scripts.get(name) for name in (script, f"pre{script}", f"post{script}")):
                    override = CommandSafety.needs_approval.value
                    why_override = f"the '{script}' script (or its pre/post script) was changed by this work"
                out.append(SelectedCheck(
                    argv=(*pm, "run", script), cwd=cwd, kind=_SCRIPT_KIND[script],
                    label=f"{pm[0]} run {script}" + (f" ({cwd})" if cwd else ""),
                    why=f"{path.as_posix()} changed; {where} defines '{script}'",
                    safety_override=override, override_reason=why_override,
                ))
    return out


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def _main_checkout(root: Path) -> Path | None:
    """The main checkout of *root*'s repository when *root* is a linked worktree, else None."""
    from openshard.util.git import run_git

    common = run_git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    if not common:
        return None
    main = Path(common.strip()).parent
    try:
        return None if main.resolve() == root.resolve() else main
    except OSError:
        return None


def python_executable(root: Path) -> str:
    """The project's own interpreter: a venv in *root*, else in its main checkout (a linked
    worktree rarely has its own), else ``python`` on PATH."""
    rels = (".venv/Scripts/python.exe", ".venv/bin/python", "venv/Scripts/python.exe", "venv/bin/python")
    main = _main_checkout(root)
    for base in (root, *([main] if main else [])):
        for rel in rels:
            candidate = base / rel
            if candidate.is_file():
                return str(candidate)
    return "python" if sys.platform == "win32" else "python3"


def _is_python_project(root: Path, git_show: GitShow, commit: str) -> str | None:
    for name in _PY_PROJECT_FILES:
        text = git_show(commit, name)
        if text is not None:
            return text
    return None


def _related_tests(stems: set[str], test_files: Iterable[str]) -> list[str]:
    out: list[str] = []
    for test in test_files:
        name = PurePosixPath(test).name
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        body = name[len("test_"):-len(".py")]
        if any(body == stem or body.startswith(stem + "_") for stem in stems):
            out.append(test)
    return out


def _python_checks(
    root: Path, changed: list[PurePosixPath], git_show: GitShow, list_files: Callable[[str], list[str]],
    commit: str,
) -> list[SelectedCheck]:
    py = [p for p in changed if p.suffix == ".py" and not (set(p.parts) & _SKIP_PARTS)]
    if not py:
        return []
    project = _is_python_project(root, git_show, commit)
    if project is None:
        return []
    exe = python_executable(root)
    out: list[SelectedCheck] = []
    existing = set(list_files(commit))
    changed_tests = sorted(p.as_posix() for p in py if p.name.startswith("test_") and p.as_posix() in existing)
    stems = {p.stem for p in py if not p.name.startswith("test_") and p.stem not in ("__init__", "__main__", "conftest")}
    tests = sorted(set(changed_tests) | set(_related_tests(stems, (f for f in existing if f.endswith(".py")))))
    if tests:
        shown = tests[:MAX_TEST_FILES]
        out.append(SelectedCheck(
            argv=(exe, "-m", "pytest", "-q", *shown), cwd="", kind="test",
            label=f"pytest ({len(shown)} related test file{'s' if len(shown) != 1 else ''})",
            why="tests named after the changed modules, and changed tests"
                + (f"; first {MAX_TEST_FILES} of {len(tests)}" if len(tests) > MAX_TEST_FILES else ""),
        ))
    else:
        out.append(SelectedCheck(
            argv=(exe, "-m", "pytest", "-q"), cwd="", kind="test", label="pytest (whole suite)",
            why="no test file names a changed module, so the whole suite runs",
        ))
    uses_ruff = "[tool.ruff" in project or git_show(commit, "ruff.toml") is not None or git_show(commit, ".ruff.toml") is not None
    lint_targets = sorted(p.as_posix() for p in py if p.as_posix() in existing)[:MAX_LINT_FILES]
    if uses_ruff and lint_targets:
        out.append(SelectedCheck(
            argv=(exe, "-m", "ruff", "check", *lint_targets), cwd="", kind="lint",
            label=f"ruff check ({len(lint_targets)} changed file{'s' if len(lint_targets) != 1 else ''})",
            why="the project configures ruff; only the changed files are checked",
        ))
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _git_show_factory(root: Path) -> GitShow:
    from openshard.util.git import run_git

    def show(rev: str, path: str) -> str | None:
        return run_git(root, ["show", f"{rev}:{path}"])

    return show


def changed_paths(root: Path, base: str | None, commit: str) -> list[PurePosixPath] | None:
    """Paths changed from *base* to *commit* (or by *commit* alone without a base). None if git cannot tell."""
    from openshard.util.git import run_git

    args = ["diff", "--name-only", "--no-renames", "-z", f"{base}..{commit}"] if base else [
        "diff-tree", "--root", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", commit,
    ]
    out = run_git(root, args)
    if out is None:
        return None
    return [PurePosixPath(p) for p in out.split("\0") if p.strip()]


def select_checks(
    root: Path,
    commit: str,
    base: str | None,
    *,
    changed: list[PurePosixPath] | None = None,
    git_show: GitShow | None = None,
    list_files: Callable[[str], list[str]] | None = None,
) -> list[SelectedCheck]:
    """The checks *commit*'s change calls for, in run order (typecheck, lint, test per area)."""
    if changed is None:
        changed = changed_paths(root, base, commit) or []
    if not changed:
        return []
    show = git_show or _git_show_factory(root)
    if list_files is None:
        from openshard.util.git import run_git

        def list_files(rev: str) -> list[str]:
            out = run_git(root, ["ls-tree", "-r", "--name-only", "-z", rev])
            return [p for p in (out or "").split("\0") if p]

    checks = _js_checks(root, changed, show, base, commit) + _python_checks(root, changed, show, list_files, commit)
    order = {"typecheck": 0, "lint": 1, "build": 2, "test": 3}
    return sorted(checks, key=lambda c: order.get(c.kind, 4))


_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def is_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.match(value))


__all__ = ["SelectedCheck", "changed_paths", "is_sha", "python_executable", "select_checks"]
