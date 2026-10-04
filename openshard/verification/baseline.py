"""Opt-in, independently executed pytest baseline diagnostics.

Never changes a verification verdict. Matching test identities establish only
that both revisions failed, not that the cause is environmental. Reports are
bounded and reduced to counts and hashed identities; raw failures stay temporary.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from openshard.verification.post_session import PlannedCheck, tree_state

MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_TESTS = 100_000


def read_report(path: Path, exit_code: int | None) -> dict:
    """Require a complete, unambiguous pytest report before comparing identities."""
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            return {"status": "incomplete", "reason": "report_too_large"}
        raw = path.read_bytes()
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise ValueError("XML declarations")
        root = ET.fromstring(raw)
        cases = list(root.iter("testcase"))
        if not cases or len(cases) > MAX_TESTS or exit_code not in (0, 1):
            raise ValueError("incomplete run")
        outcomes: dict[str, str] = {}
        for case in cases:
            name = case.get("name")
            classname = case.get("classname")
            if not name or not classname:
                raise ValueError("missing identity")
            identity = hashlib.sha256(json.dumps([classname, name]).encode()).hexdigest()
            if identity in outcomes:
                raise ValueError("duplicate identity")
            status = "failed" if case.find("failure") is not None or case.find("error") is not None else (
                "skipped" if case.find("skipped") is not None else "passed")
            outcomes[identity] = status
        if (exit_code == 1) != any(v == "failed" for v in outcomes.values()):
            raise ValueError("inconsistent exit code")
        # Collection/setup errors can create synthetic testcases: counts alone
        # cannot establish that the expected suite executed.
        if any(case.find("error") is not None for case in cases):
            raise ValueError("setup or collection incomplete")
        return {"status": "complete", "outcomes": outcomes}
    except (OSError, ValueError, ET.ParseError):
        return {"status": "incomplete", "reason": "missing_or_incomplete_report"}


def compare_reports(base: dict, head: dict) -> dict:
    if base.get("status") != "complete" or head.get("status") != "complete":
        return {"status": "incomplete", "reason": "both_complete_reports_required"}
    left, right = base["outcomes"], head["outcomes"]
    head_failed = {k for k, v in right.items() if v == "failed"}
    base_failed = {k for k, v in left.items() if v == "failed"}
    return {
        "status": "failed" if head_failed else "passed",
        "passed": sum(v == "passed" for v in right.values()),
        "failed": len(head_failed),
        "skipped": sum(v == "skipped" for v in right.values()),
        "also_failed_on_base": len(head_failed & base_failed),
        "new_failures": len(head_failed - base_failed),
        "base_only_tests": len(left.keys() - right.keys()),
        "head_only_tests": len(right.keys() - left.keys()),
        "environment_incompatible": None,
        "environment_cause": "not_established",
    }


def _pytest_args(argv: tuple[str, ...] | list[str]) -> list[str] | None:
    args = list(argv)
    if args and Path(args[0]).name in ("pytest", "pytest.exe"):
        tail = args[1:]
    elif len(args) >= 3 and Path(args[0]).name in ("python", "python3", "python.exe", Path(sys.executable).name) and args[1:3] == ["-m", "pytest"]:
        tail = args[3:]
    else:
        return None
    # A caller-supplied report path/import override would undermine the evidence.
    if any(a.startswith(("--junit", "--import-mode", "--override-ini", "-o")) for a in tail):
        return None
    return tail


def _dependency_fingerprint() -> str:
    packages = sorted((d.metadata.get("Name", ""), d.version) for d in importlib.metadata.distributions())
    return hashlib.sha256(json.dumps(packages).encode()).hexdigest()


def run_baseline_comparison(repo: Path, base_ref: str, planned: list[PlannedCheck], *,
                            approve: bool, timeout: float) -> dict:
    """Run approved pytest checks on clean detached base/head worktrees.

    Reuses installed dependencies and interpreter; does not install anything.
    This is supplemental evidence, never a way to promote failed checks green.
    """
    state = tree_state(repo)
    result: dict = {"version": 1, "source": "directly_observed", "status": "incomplete",
                    "environment_cause": "not_established", "raw_output_stored": False, "checks": []}
    if not state.head or state.dirty is not False:
        return {**result, "reason": "clean_head_required"}
    try:
        proc = subprocess.run(["git", "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"],
                              cwd=repo, capture_output=True, text=True, timeout=15, check=False)
        base_sha = proc.stdout.strip()
        if proc.returncode or len(base_sha) not in (40, 64) or any(c not in "0123456789abcdef" for c in base_sha):
            return {**result, "reason": "base_commit_unavailable"}
    except (OSError, subprocess.TimeoutExpired):
        return {**result, "reason": "git_unavailable"}
    fingerprint = _dependency_fingerprint()
    result.update(base_commit=base_sha, head_commit=state.head,
                  environment={"system": platform.system(), "python": platform.python_version(),
                               "dependencies": "shared_installed_environment", "source_paths": "worktree_first",
                               "dependency_fingerprint": fingerprint})
    with tempfile.TemporaryDirectory(prefix="openshard-baseline-") as temp:
        root = Path(temp)
        trees: list[Path] = []
        try:
            for label, sha in (("base", base_sha), ("head", state.head)):
                path = root / label
                proc = subprocess.run(["git", "worktree", "add", "--detach", str(path), sha], cwd=repo,
                                      capture_output=True, text=True, timeout=30, check=False)
                if proc.returncode:
                    return {**result, "reason": "worktree_unavailable"}
                trees.append(path)
            for index, check in enumerate(planned):
                args = _pytest_args(check.argv)
                row: dict = {"name": check.name, "status": "incomplete"}
                result["checks"].append(row)
                if args is None or check.safety == "blocked" or (check.safety == "needs_approval" and not approve):
                    row["reason"] = "unsupported_or_unapproved_check"
                    continue
                reports: list[dict] = []
                for path, expected_sha in zip(trees, (base_sha, state.head), strict=True):
                    if tree_state(path).dirty is not False:
                        reports.append({"status": "incomplete", "reason": "tested_tree_changed"})
                        continue
                    report = root / f"{path.name}-{index}.xml"
                    env = dict(os.environ)
                    env["PYTHONPATH"] = os.pathsep.join((str(path), str(path / "src")))
                    env.pop("PYTEST_ADDOPTS", None)
                    try:
                        proc = subprocess.run([sys.executable, "-m", "pytest", *args, "--import-mode=prepend",
                                               f"--junitxml={report}"], cwd=path, env=env, timeout=timeout,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, check=False)
                        observed = read_report(report, proc.returncode)
                        after = tree_state(path)
                        if after.head != expected_sha or after.tracked_dirty is not False or _dependency_fingerprint() != fingerprint:
                            observed = {"status": "incomplete", "reason": "tested_tree_changed"}
                    except (OSError, subprocess.TimeoutExpired):
                        observed = {"status": "incomplete", "reason": "check_not_completed"}
                    reports.append(observed)
                row.update(compare_reports(*reports))
            statuses = [r["status"] for r in result["checks"]]
            result["status"] = "incomplete" if not statuses or "incomplete" in statuses else (
                "failed" if "failed" in statuses else "passed")
            return result
        except (OSError, subprocess.TimeoutExpired):
            return {**result, "status": "incomplete", "reason": "comparison_not_completed"}
        finally:
            for path in trees:
                try:
                    subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=repo,
                                   capture_output=True, text=True, timeout=30, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
