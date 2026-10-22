"""Fail-without-fix: do a change's new tests fail on the code before the change?

A test that passes without the fix proves nothing about the fix. This takes
the Python test functions a change adds or rewrites (comparing each test
file at the merge-base and at head), copies head's versions of those test
files onto a clean worktree of the merge-base, and runs just those tests
there with pytest. Then it runs them on a clean worktree of head.

Each new or changed test ends in one of:

* ``proves_fix`` -- failed on base (a real test failure), passed on head;
* ``cannot_load_on_base`` -- on base it could not even load: its file failed
  to import, a fixture or helper it needs is missing (a setup error), or it
  failed with ``ImportError`` / ``ModuleNotFoundError``. It passes on head,
  but this shows only that the code it imports is new, not that it tests
  the fix;
* ``passes_without_fix`` -- passed on base: it does not show the change matters;
* ``fails_on_head`` -- failed on head too;
* ``not_run`` -- no result for it in either report (skipped, deselected, renamed).

Opt-in, and local: it runs the change's own tests (as ``openshard verify``
does), never installs anything, and keeps counts and test names only, never
output. Anything it cannot establish leaves ``status`` ``incomplete`` with a
reason, never a pass.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from openshard.verification.gaming_signals import is_test_path

MAX_TEST_FILES = 20
MAX_NAMES = 5
DEFAULT_TIMEOUT_SECONDS = 600.0
MAX_REPORT_BYTES = 8 * 1024 * 1024
OUTCOMES = ("proves_fix", "cannot_load_on_base", "passes_without_fix", "fails_on_head", "not_run")
_LOAD_FAILURE = re.compile(r"^\s*(?:E\s+)?(?:ImportError|ModuleNotFoundError)\b")
_RANK = {"failed": 3, "cannot_load": 2, "skipped": 1, "passed": 0}


def _git(repo: Path, *args: str, timeout: float = 30) -> str | None:
    try:
        proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def test_functions(text: str | None) -> dict[str, str] | None:
    """``{"test_x": <hash of its source>, "TestK.test_y": ...}``; None when *text* does not parse."""
    if text is None:
        return {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    out: dict[str, str] = {}

    def add(name: str, node: ast.AST) -> None:
        out[name] = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test"):
            add(node.name, node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name.startswith("test"):
                    add(f"{node.name}.{item.name}", item)
    return out


def _is_python_test(path: str) -> bool:
    name = PurePosixPath(path).name
    return path.endswith(".py") and name != "conftest.py" and is_test_path(path)


def changed_tests(repo: Path, fork: str, head: str) -> dict[str, list[str]] | None:
    """``{test file: [new or rewritten test names]}`` between *fork* and *head*; None when git cannot tell."""
    listing = _git(repo, "diff", "--name-only", "--diff-filter=AMR", "-z", f"{fork}..{head}")
    if listing is None:
        return None
    out: dict[str, list[str]] = {}
    for path in [p for p in listing.split("\0") if p and _is_python_test(p)][:MAX_TEST_FILES]:
        before = test_functions(_git(repo, "show", f"{fork}:{path}"))
        after = test_functions(_git(repo, "show", f"{head}:{path}"))
        if before is None or after is None:
            continue
        names = sorted(n for n, digest in after.items() if before.get(n) != digest)
        if names:
            out[path] = names
    return out


def read_outcomes(report: Path) -> dict[tuple[str, str], str] | None:
    """``{(module path, test name): passed|failed|cannot_load|skipped}`` from a pytest JUnit report; None if unreadable.

    ``cannot_load``: an error outside the test body (collection, fixture setup) or a failure raised as
    ``ImportError`` / ``ModuleNotFoundError``. Parametrised cases fold into their function: a real failure
    wins, then ``cannot_load``.
    """
    try:
        if report.stat().st_size > MAX_REPORT_BYTES:
            return None
        raw = report.read_bytes()
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            return None
        root = ET.fromstring(raw)
    except (OSError, ET.ParseError):
        return None
    out: dict[tuple[str, str], str] = {}
    for case in root.iter("testcase"):
        classname, name = case.get("classname") or "", (case.get("name") or "").split("[", 1)[0]
        if not name:
            continue
        failure, error = case.find("failure"), case.find("error")
        if error is not None:
            status = "cannot_load"
        elif failure is not None:
            message = failure.get("message") or ""
            status = "cannot_load" if _LOAD_FAILURE.match(message) else "failed"
        else:
            status = "skipped" if case.find("skipped") is not None else "passed"
        parts = classname.split(".")
        cls = parts[-1] if parts and parts[-1].startswith("Test") else None
        module = ".".join(parts[:-1] if cls else parts)
        key = (module, f"{cls}.{name}" if cls else name)
        if _RANK[status] >= _RANK.get(out.get(key, "passed"), 0):
            out[key] = status
    return out


def _module(path: str) -> str:
    return path[:-3].replace("/", ".")


def _run(tree: Path, files: list[str], report: Path, timeout: float) -> tuple[int | None, dict | None]:
    from openshard.verification.post_session import check_environment

    env = check_environment()
    env["PYTHONPATH"] = os.pathsep.join((str(tree), str(tree / "src")))
    env.pop("PYTEST_ADDOPTS", None)
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--import-mode=prepend",
                               f"--junitxml={report}", "-o", "junit_family=xunit1", *files],
                              cwd=tree, env=env, timeout=timeout, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    return proc.returncode, read_outcomes(report)


def _outcome(module: str, name: str, outcomes: dict | None) -> str | None:
    if outcomes is None:
        return None
    if (module, name) in outcomes:
        return outcomes[(module, name)]
    # A test file that failed to import reports one collection error for the module
    # (empty classname, the module as the name), not one per test.
    return "cannot_load" if outcomes.get(("", module)) == "cannot_load" else None


def run_fail_without_fix(repo: Path, base_ref: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Run the change's new or rewritten tests on its merge-base and on head; see the module docstring."""
    result: dict = {"version": 1, "status": "incomplete", "raw_output_stored": False}
    head = (_git(repo, "rev-parse", "--verify", "HEAD^{commit}") or "").strip()
    fork = (_git(repo, "merge-base", base_ref, "HEAD") or "").strip()
    if not head or not fork:
        return {**result, "reason": "base_commit_unavailable"}
    result.update(base_commit=fork, head_commit=head)
    tests = changed_tests(repo, fork, head)
    if tests is None:
        return {**result, "reason": "git_unavailable"}
    if not tests:
        return {**result, "status": "complete", "counts": dict.fromkeys(("new_or_changed", *OUTCOMES), 0)}
    with tempfile.TemporaryDirectory(prefix="openshard-fwf-") as temp:
        root, trees = Path(temp), []
        try:
            for label, sha in (("base", fork), ("head", head)):
                path = root / label
                if _git(repo, "worktree", "add", "--detach", str(path), sha) is None:
                    return {**result, "reason": "worktree_unavailable"}
                trees.append(path)
            base_tree, head_tree = trees
            for rel in tests:
                text = _git(repo, "show", f"{head}:{rel}")
                if text is None:
                    return {**result, "reason": "test_file_unreadable"}
                target = base_tree / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            files = sorted(tests)
            base_code, on_base = _run(base_tree, files, root / "base.xml", timeout)
            if base_code not in (0, 1, 2):  # 0 passed, 1 failures, 2 collection errors; anything else broke
                on_base = None
            head_code, on_head = _run(head_tree, files, root / "head.xml", timeout)
        finally:
            for path in trees:
                _git(repo, "worktree", "remove", "--force", str(path))
    if on_head is None or base_code is None:
        return {**result, "reason": "tests_not_completed"}
    rows: dict[str, list[str]] = {k: [] for k in OUTCOMES}
    for rel, names in tests.items():
        module = _module(rel)
        for name in names:
            before = _outcome(module, name, on_base)
            after = on_head.get((module, name))
            label = f"{rel}::{name.replace('.', '::')}"
            if after in ("failed", "cannot_load"):
                rows["fails_on_head"].append(label)
            elif after != "passed" or before is None or before == "skipped":
                rows["not_run"].append(label)
            elif before == "failed":
                rows["proves_fix"].append(label)
            elif before == "cannot_load":
                rows["cannot_load_on_base"].append(label)
            else:
                rows["passes_without_fix"].append(label)
    total = sum(len(v) for v in rows.values())
    return {**result, "status": "complete", "head_exit_code": head_code,
            "counts": {"new_or_changed": total, **{k: len(v) for k, v in rows.items()}},
            "passes_without_fix_tests": rows["passes_without_fix"][:MAX_NAMES],
            "cannot_load_on_base_tests": rows["cannot_load_on_base"][:MAX_NAMES],
            "fails_on_head_tests": rows["fails_on_head"][:MAX_NAMES]}


__all__ = ["changed_tests", "read_outcomes", "run_fail_without_fix", "test_functions"]
