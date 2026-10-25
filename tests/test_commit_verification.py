"""Automatic commit verification: checks chosen from the change, run on the exact commit.

The path a Receipt takes to green Verified:

    a captured session creates a commit
    -> capture starts ``openshard verify --commit <sha> --auto`` (on by default)
    -> checks are selected from what the commit changed
    -> they run on exactly that commit (an isolated copy when the checkout is
       not clean at it), never approved, never through a shell
    -> the attestation is bound to the commit (``artifact_sha``)

These tests pin selection, safety (a rewritten check definition is not run
unattended), exact-commit binding even with a dirty checkout, and the trigger.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
from click.testing import CliRunner

from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.cli.main import cli
from openshard.verification import auto
from openshard.verification import change_checks as cc
from openshard.verification import post_session as ps

SID = "55555555-6666-4777-8888-999999999999"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(root: Path, message: str = "change") -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for rel, text in files.items():
        _write(root, rel, text)
    return root, _commit(root, "base")


def _pkg(name: str, scripts: dict[str, str], **extra) -> str:
    return json.dumps({"name": name, "scripts": scripts, **extra})


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


class TestJavaScriptSelection:
    def test_every_manifest_between_the_file_and_the_root_contributes_its_checks(self, tmp_path):
        root, base = _repo(tmp_path, {
            "package.json": _pkg("root", {"typecheck": "tsc", "test": "vitest run", "dev": "vite"},
                                 packageManager="pnpm@10.0.0"),
            "apps/web/package.json": _pkg("web", {"typecheck": "tsc -p .", "build": "vite build"}),
            "apps/web/src/a.ts": "export const a = 1;\n",
            "docs/readme.md": "x\n",
        })
        _write(root, "apps/web/src/a.ts", "export const a = 2;\n")
        head = _commit(root)
        checks = cc.select_checks(root, head, base)
        assert [(c.argv, c.cwd, c.kind) for c in checks] == [
            (("pnpm", "run", "typecheck"), "apps/web", "typecheck"),
            (("pnpm", "run", "typecheck"), "", "typecheck"),
            (("pnpm", "run", "test"), "", "test"),
        ]
        assert all("apps/web/src/a.ts changed" in c.why for c in checks)
        assert all(c.safety_override is None for c in checks)

    def test_a_check_script_rewritten_by_the_change_needs_approval(self, tmp_path):
        root, base = _repo(tmp_path, {
            "package.json": _pkg("root", {"test": "vitest run"}),
            "src/a.ts": "1\n",
        })
        _write(root, "package.json", _pkg("root", {"test": "node -e \"process.exit(0)\""}))
        _write(root, "src/a.ts", "2\n")
        head = _commit(root)
        [check] = cc.select_checks(root, head, base)
        assert check.safety_override == "needs_approval"
        assert "'test' script (or its pre/post script) was changed" in check.override_reason

    def test_a_new_pretest_script_also_needs_approval(self, tmp_path):
        root, base = _repo(tmp_path, {"package.json": _pkg("root", {"test": "vitest run"}), "src/a.ts": "1\n"})
        _write(root, "package.json", _pkg("root", {"test": "vitest run", "pretest": "node evil.js"}))
        head = _commit(root)
        [check] = cc.select_checks(root, head, base)
        assert check.safety_override == "needs_approval"

    def test_without_a_base_the_parent_commit_is_the_reference(self, tmp_path):
        root, _base = _repo(tmp_path, {"package.json": _pkg("root", {"test": "vitest run"}), "src/a.ts": "1\n"})
        _write(root, "src/a.ts", "2\n")
        head = _commit(root)
        [check] = cc.select_checks(root, head, None)
        assert check.safety_override is None

    def test_a_docs_only_change_outside_any_package_selects_nothing_from_javascript(self, tmp_path):
        root, base = _repo(tmp_path, {"README.md": "x\n"})
        _write(root, "README.md", "y\n")
        head = _commit(root)
        assert cc.select_checks(root, head, base) == []


class TestPythonSelection:
    FILES = {
        "pyproject.toml": "[project]\nname='p'\n[tool.ruff]\nline-length=100\n",
        "pkg/__init__.py": "",
        "pkg/widget.py": "def f():\n    return 1\n",
        "pkg/other.py": "def g():\n    return 1\n",
        "tests/test_widget.py": "from pkg.widget import f\n\ndef test_f():\n    assert f() == 1\n",
        "tests/test_widget_extra.py": "def test_x():\n    assert True\n",
        "tests/test_other.py": "def test_g():\n    assert True\n",
    }

    def test_related_tests_and_ruff_on_the_changed_files(self, tmp_path):
        root, base = _repo(tmp_path, self.FILES)
        _write(root, "pkg/widget.py", "def f():\n    return 1  # tweak\n")
        head = _commit(root)
        test, lint = cc.select_checks(root, head, base)[::-1]
        assert test.argv[1:4] == ("-m", "pytest", "-q")
        assert test.argv[4:] == ("tests/test_widget.py", "tests/test_widget_extra.py")
        assert lint.argv[1:] == ("-m", "ruff", "check", "pkg/widget.py")

    def test_no_related_test_falls_back_to_the_whole_suite(self, tmp_path):
        root, base = _repo(tmp_path, {**self.FILES, "pkg/lonely.py": "x = 1\n"})
        _write(root, "pkg/lonely.py", "x = 2\n")
        head = _commit(root)
        checks = cc.select_checks(root, head, base)
        test = next(c for c in checks if c.kind == "test")
        assert test.argv[-3:] == ("-m", "pytest", "-q") and "whole suite" in test.why


class TestPlanCommitChecks:
    def test_the_configured_contract_wins(self, tmp_path):
        root, base = _repo(tmp_path, {"package.json": _pkg("r", {"test": "vitest"}), "a.ts": "1\n"})
        _write(root, "a.ts", "2\n")
        head = _commit(root)
        planned = ps.plan_commit_checks(root, {"verification_commands": ["npm test"]}, None, head, base)
        assert [(p.origin, p.argv) for p in planned] == [("contract", ["npm", "test"])]

    def test_change_checks_are_safe_unless_their_definition_changed(self, tmp_path):
        root, base = _repo(tmp_path, {"package.json": _pkg("r", {"test": "vitest", "lint": "eslint"}), "a.ts": "1\n"})
        _write(root, "package.json", _pkg("r", {"test": "vitest", "lint": "eslint --fix"}))
        head = _commit(root)
        planned = {p.argv[-1]: p for p in ps.plan_commit_checks(root, {}, None, head, base)}
        assert planned["test"].safety == "safe" and planned["test"].origin == ps.ORIGIN_CHANGE
        assert planned["lint"].safety == "needs_approval"


# ---------------------------------------------------------------------------
# Running on the exact commit (CLI)
# ---------------------------------------------------------------------------


def _claude(repo: Path, event: str, **fields) -> None:
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event, **fields}
    handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


@pytest.fixture
def captured(tmp_path, monkeypatch):
    """A captured session in a Python project; the work is then committed."""
    root, _base = _repo(tmp_path, {
        "pyproject.toml": "[project]\nname='p'\n",
        "pkg/__init__.py": "",
        "pkg/widget.py": "def f():\n    return 1\n",
        "tests/__init__.py": "",
        "tests/test_widget.py": "from pkg.widget import f\n\ndef test_f():\n    assert f() == 2\n",
        ".gitignore": ".openshard/\n__pycache__/\n.pytest_cache/\n",
    })
    monkeypatch.chdir(root)
    monkeypatch.setattr(cc, "python_executable", lambda _root: sys.executable)
    _claude(root, "UserPromptSubmit", prompt="make f return 2")
    _write(root, "pkg/widget.py", "def f():\n    return 2\n")
    _claude(root, "PostToolUse", tool_name="Edit", tool_input={"file_path": str(root / "pkg/widget.py")})
    _claude(root, "Stop")
    _claude(root, "SessionEnd", reason="exit")
    head = _commit(root, "make f return 2")
    entry = [json.loads(ln) for ln in (root / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()][-1]
    return root, head, entry


def _verify(*args: str) -> dict:
    result = CliRunner().invoke(cli, ["verify", "--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


class TestVerifyCommit:
    def test_a_clean_checkout_runs_in_place_and_binds_the_commit(self, captured):
        root, head, entry = captured
        out = _verify("--receipt", entry["receipt_id"], "--commit", head)
        v = out["verification"]
        assert (out["commit"], out["checkout"]["mode"]) == (head, "in_place")
        assert (v["status"], v["source"], v["observation_mode"], v["artifact_sha"]) == (
            "passed", "directly_observed", "openshard_executed", head)
        assert [c["origin"] for c in out["checks"]] == ["change"]

    def test_a_dirty_checkout_is_verified_in_an_isolated_copy_of_the_commit(self, captured):
        root, head, entry = captured
        # Someone else's uncommitted edit would fail the test; the commit itself passes.
        _write(root, "pkg/widget.py", "def f():\n    return 3\n")
        out = _verify("--receipt", entry["receipt_id"], "--commit", head)
        assert out["checkout"]["mode"] == "isolated_worktree"
        assert (out["verification"]["status"], out["verification"]["artifact_sha"]) == ("passed", head)
        assert (root / "pkg/widget.py").read_text(encoding="utf-8").endswith("return 3\n")  # untouched
        assert "isolated" not in _git(root, "worktree", "list")  # cleaned up

    def test_a_failing_commit_is_recorded_failed_and_still_bound(self, captured):
        root, _head, entry = captured
        _write(root, "pkg/widget.py", "def f():\n    return 4\n")
        bad = _commit(root, "regress")
        v = _verify("--receipt", entry["receipt_id"], "--commit", bad)["verification"]
        assert (v["status"], v["artifact_sha"], v["checks_failed"]) == ("failed", bad, 1)

    def test_auto_refuses_a_commit_the_session_did_not_create(self, captured):
        root, head, entry = captured  # committed after the session ended: not a session commit
        out = _verify("--receipt", entry["receipt_id"], "--commit", head, "--auto")
        assert out["status"] == "not_session_commit"
        assert not (root / ".openshard" / "verifications.jsonl").exists()

    def test_auto_runs_isolated_and_skips_a_commit_already_verified(self, captured):
        from openshard.verification import commit_run as cr

        root, head, entry = captured
        entry = {**entry, "git_end_head": head, "session_commits": {"source": "git_observed", "shas": [head]}}
        kwargs = dict(config={}, base=None, approve=True, unattended=True, timeout=300, stream=False)
        first = cr.verify_commit(root, root / ".openshard", entry, head, **kwargs)
        v = first.attestation["verification"]
        assert first.attestation["checkout"]["mode"] == "isolated_worktree"
        assert (v["status"], v["artifact_sha"]) == ("passed", head)
        assert cr.verify_commit(root, root / ".openshard", entry, head, **kwargs).status == "already_verified"
    def test_a_missing_test_runner_is_unknown_never_failed(self, captured):
        from openshard.verification import commit_run as cr

        root, _head, _entry = captured
        absent = ps.PlannedCheck(name="absent", argv=[sys.executable, "-m", "openshard_no_such_module_xyz"],
                                 kind="test", origin=ps.ORIGIN_CHANGE, safety="safe", reason="")
        present = ps.PlannedCheck(name="present", argv=[sys.executable, "-c", "pass"],
                                  kind="test", origin=ps.ORIGIN_CHANGE, safety="safe", reason="")
        results = cr._run_in(root, [absent, present], [absent, present], {}, approve=False, timeout=60,
                             stream=False, on_start=None)
        assert [(r.check.name, r.status) for r in results] == [("absent", "unknown"), ("present", "passed")]
        assert "openshard_no_such_module_xyz is not installed" in results[0].note
    def test_unknown_commit_is_refused(self, captured):
        result = CliRunner().invoke(cli, ["verify", "--json", "--commit", "deadbeef"])
        assert result.exit_code == 1 and json.loads(result.output)["status"] == "not_found"


# ---------------------------------------------------------------------------
# The trigger
# ---------------------------------------------------------------------------


def _entry_with_commit(sha: str, rid: str = "rcpt_1") -> dict:
    return {"receipt_id": rid, "git_end_head": sha, "session_commits": {"source": "git_observed", "shas": [sha]}}


class TestTrigger:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.setattr(auto, "_requested", set())

    def test_on_by_default_once_per_receipt_and_commit(self, tmp_path):
        spawned: list[list[str]] = []
        entry = _entry_with_commit("c" * 40)
        spawn = lambda argv, cwd: spawned.append(argv) or True  # noqa: E731
        assert auto.schedule_commit_verify(tmp_path, entry, spawner=spawn, env={}) is True
        assert auto.schedule_commit_verify(tmp_path, entry, spawner=spawn, env={}) is False
        assert spawned[0][-6:] == ["--receipt", "rcpt_1", "--commit", "c" * 40, "--auto", "--json"]
        assert "--approve" not in spawned[0]

    def test_off_by_config_or_environment_and_never_without_a_commit(self, tmp_path):
        spawn = lambda argv, cwd: True  # noqa: E731
        assert auto.schedule_commit_verify(tmp_path, {"receipt_id": "rcpt_1"}, spawner=spawn, env={}) is False
        assert auto.schedule_commit_verify(tmp_path, _entry_with_commit("d" * 40), spawner=spawn,
                                           env={auto.ENV_DISABLE: "1"}) is False
        (tmp_path / ".openshard").mkdir()
        (tmp_path / ".openshard" / "config.yml").write_text("post_session_verify: off\n", encoding="utf-8")
        assert auto.commit_verify_enabled(tmp_path, {}) is False
        assert auto.schedule_commit_verify(tmp_path, _entry_with_commit("e" * 40), spawner=spawn, env={}) is False

    def test_a_commit_already_attested_is_not_requested_again(self, tmp_path):
        sha = "f" * 40
        entry = _entry_with_commit(sha)
        tree = ps.TreeState(head=sha, dirty=False, tracked_dirty=False)
        ps.record_attestation(tmp_path, ps.build_attestation(
            entry, [], before=tree, after=tree, started_at="2026-10-14T00:00:00Z",
            completed_at="2026-10-14T00:00:01Z", commit=sha))
        assert auto.schedule_commit_verify(tmp_path, entry, spawner=lambda a, c: True, env={}) is False

    def test_session_end_with_a_commit_verifies_the_commit_not_the_working_tree(self, tmp_path, monkeypatch):
        from openshard.adapters import claude_hooks as ch

        calls: list[str] = []
        monkeypatch.setattr(auto, "schedule_commit_verify", lambda root, entry: calls.append("commit"))
        monkeypatch.setattr(auto, "schedule_post_session_verify", lambda root, entry: calls.append("tree"))
        ch._schedule_post_session_verify(tmp_path, _entry_with_commit("a" * 40))
        ch._schedule_post_session_verify(tmp_path, {"receipt_id": "rcpt_2"})
        assert calls == ["commit", "tree"]


def test_a_linked_worktree_uses_its_main_checkouts_venv(tmp_path):
    root, _base = _repo(tmp_path, {"a.py": "x = 1\n"})
    venv_python = root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", str(linked), "-b", "side")
    assert Path(cc.python_executable(linked)) == venv_python
    assert Path(cc.python_executable(root)) == venv_python


def test_changed_paths_from_base_and_for_a_root_commit(tmp_path):
    root, base = _repo(tmp_path, {"a.txt": "1\n"})
    _write(root, "b/c.txt", "2\n")
    head = _commit(root)
    assert cc.changed_paths(root, base, head) == [PurePosixPath("b/c.txt")]
    assert cc.changed_paths(root, None, base) == [PurePosixPath("a.txt")]


class TestVerdictCompleteness:
    def _check(self, name: str, kind: str, safety: str = "safe") -> ps.PlannedCheck:
        return ps.PlannedCheck(name=name, argv=[name], kind=kind, origin=ps.ORIGIN_CHANGE, safety=safety, reason="")

    def _attest(self, results, *, truncated=False):
        tree = ps.TreeState(head="a" * 40, dirty=False, tracked_dirty=False)
        return ps.build_attestation({"receipt_id": "rcpt_1"}, results, before=tree, after=tree,
                                    started_at="2026-10-14T00:00:00Z", completed_at="2026-10-14T00:00:01Z",
                                    commit="a" * 40, truncated=truncated)["verification"]

    def test_a_skipped_check_leaves_a_commit_partial_not_passed(self):
        v = self._attest([ps.CheckRun(self._check("lint", "lint"), "passed", exit_code=0),
                          ps.CheckRun(self._check("test", "test", "needs_approval"), "skipped")])
        assert v["status"] == "partial" and "check_not_completed" in v["incomplete_reasons"]

    def test_a_truncated_plan_is_never_a_clean_pass(self):
        v = self._attest([ps.CheckRun(self._check("test", "test"), "passed", exit_code=0)], truncated=True)
        assert v["status"] == "partial" and "checks_truncated" in v["incomplete_reasons"]

    def test_truncation_keeps_tests_before_lint(self, tmp_path):
        files = {"package.json": _pkg("root", {"lint": "eslint", "typecheck": "tsc", "test": "vitest"}, packageManager="pnpm@10")}
        for n in range(4):
            files[f"p{n}/package.json"] = _pkg(f"p{n}", {"lint": "eslint", "typecheck": "tsc", "test": "vitest"})
            files[f"p{n}/a.ts"] = "1\n"
        root, base = _repo(tmp_path, files)
        for n in range(4):
            _write(root, f"p{n}/a.ts", "2\n")
        head = _commit(root)
        planned, truncated = ps.plan_commit(root, {}, None, head, base)
        kinds = [p.kind for p in planned]
        assert truncated and len(planned) == ps.MAX_PLANNED_CHECKS
        assert kinds.count("test") == 5 and "lint" not in kinds

# ---------------------------------------------------------------------------
# The session's own commit when a delegated agent committed on top
# ---------------------------------------------------------------------------


def _iso(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _commit_at(root: Path, message: str, when: int) -> str:
    import os

    env = {**os.environ, "GIT_COMMITTER_DATE": f"{when} +0000", "GIT_AUTHOR_DATE": f"{when} +0000"}
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", message],
                   cwd=root, check=True, env=env)
    return _git(root, "rev-parse", "HEAD")


class TestOwnCommitUnderALaterAgentsCommit:
    """Live E2E shape: the lead committed 24cd66b, then the delegated Codex committed 8042ff9 on top.

    The lead's end HEAD is Codex's commit, so the lead Receipt had no commit and
    no verification although git observed it create its own.
    """

    def _lead_outcome(self, tmp_path):
        import time

        from openshard.adapters import claude_hooks as ch

        root, start = _repo(tmp_path, {"calc/__init__.py": "x = 1\n"})
        now = int(time.time())
        _write(root, "calc/__init__.py", "x = 2\n")
        lead = _commit_at(root, "lead change", now)
        _write(root, "calc/__init__.py", "x = 3\n")
        delegated = _commit_at(root, "delegated change", now + 30)  # after the lead's git call returned
        buf = {"git_head_commit_hash": start, "started_at": _iso(now - 60), "last_activity_at": _iso(now + 40),
               "git_write_at": [_iso(now + 1)]}
        return root, lead, delegated, ch._session_git_outcome(buf, root)

    def test_the_sessions_last_own_commit_is_its_result(self, tmp_path):
        from openshard.adapters import claude_hooks as ch
        from openshard.history.receipt_evidence import commit_value
        from openshard.verification.commit_run import session_commits

        _root, lead, delegated, outcome = self._lead_outcome(tmp_path)
        assert outcome["end_head"] == delegated
        assert outcome["commits"] == [lead]
        assert outcome["result"] == lead
        entry: dict = {"receipt_id": "rcpt_lead"}
        ch._stamp_git_outcome(entry, {"git_outcome": outcome})
        assert commit_value(entry) == lead
        assert delegated not in session_commits(entry)  # never presented as the lead's work
        rebuilt = ch._stored_git_outcome(entry)
        assert rebuilt is not None and rebuilt["result"] == lead

    def test_without_a_confirmed_ancestor_there_is_no_commit(self):
        from openshard.history.receipt_evidence import commit_value

        lead, other = "a" * 40, "b" * 40
        assert commit_value({"git_end_head": other, "session_commits": {"source": "git_observed", "shas": [lead]}}) is None
        # A result that is not one of the session's own commits is never used.
        assert commit_value({"git_end_head": other, "session_commits": {
            "source": "git_observed", "shas": [lead], "result": other}}) is None
        assert commit_value({"git_end_head": other, "session_commits": {
            "source": "git_observed", "shas": [lead], "result": lead}}) == lead


class TestSyncAfterAutoVerify:
    """The automatic per-commit verification sends its Receipt to the Platform at once (merge gate)."""

    def _flushes(self, monkeypatch) -> list[tuple]:
        from openshard.sync import client

        calls: list[tuple] = []
        monkeypatch.setattr(client, "flush", lambda root, **kw: calls.append((Path(root), kw.get("receipt_ids"))))
        return calls

    def test_an_auto_verification_flushes_its_receipt(self, captured, monkeypatch):
        from openshard.verification import commit_run as cr

        root, head, entry = captured
        real = cr.verify_commit
        # The fixture's commit came after the session; let the unattended run proceed anyway.
        monkeypatch.setattr(cr, "verify_commit", lambda *a, **kw: real(*a, **{**kw, "unattended": False}))
        calls = self._flushes(monkeypatch)
        out = _verify("--receipt", entry["receipt_id"], "--commit", head, "--auto")
        assert out["status"] == "ok"
        assert calls == [(root, frozenset({entry["receipt_id"]}))]

    def test_a_flush_failure_never_fails_the_verification(self, captured, monkeypatch):
        from openshard.sync import client
        from openshard.verification import commit_run as cr

        root, head, entry = captured
        real = cr.verify_commit
        monkeypatch.setattr(cr, "verify_commit", lambda *a, **kw: real(*a, **{**kw, "unattended": False}))

        def boom(*_a, **_kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(client, "flush", boom)
        assert _verify("--receipt", entry["receipt_id"], "--commit", head, "--auto")["status"] == "ok"

    def test_a_manual_verification_does_not_flush(self, captured, monkeypatch):
        root, head, entry = captured
        calls = self._flushes(monkeypatch)
        assert _verify("--receipt", entry["receipt_id"], "--commit", head)["status"] == "ok"
        assert calls == []


# ---------------------------------------------------------------------------
# Documentation-only commits: no check applies, nothing is verified
# ---------------------------------------------------------------------------


class TestDocsOnlyClassification:
    @pytest.mark.parametrize("path", [
        "README.md", "CHANGELOG.md", "notes.txt", "LICENSE", "NOTICE", ".gitignore", "guide.rst",
        "docs/verify.md", "docs/img/flow.png", "packages/web/README.md", "packages/web/LICENSE",
    ])
    def test_documentation_paths(self, path):
        assert cc.docs_only([PurePosixPath(path)]) is True

    @pytest.mark.parametrize("path", [
        "pyproject.toml", "setup.cfg", "package.json", "src/a.py", "requirements.txt", "requirements-dev.txt",
        "CMakeLists.txt", "docs/conf.py", "docs/requirements.txt", "docs/Makefile", ".github/workflows/ci.yml",
        ".github/README.md", "tests/fixtures/expected.txt", "tests/README.md", "pkg/prompts/system.md",
        ".gitattributes", ".pre-commit-config.yaml", "config.yml", "Makefile",
    ])
    def test_code_config_test_and_ci_paths_are_never_documentation(self, path):
        assert cc.docs_only([PurePosixPath(path)]) is False

    def test_unknown_or_mixed_changes_are_not_docs_only(self):
        assert cc.docs_only(None) is False
        assert cc.docs_only([]) is False
        assert cc.docs_only([PurePosixPath("README.md"), PurePosixPath("pkg/a.py")]) is False


PY_PROJECT = {
    "pyproject.toml": "[project]\nname='p'\n",
    "pkg/__init__.py": "",
    "pkg/widget.py": "def f():\n    return 1\n",
    "tests/test_widget.py": "def test_f():\n    assert True\n",
    "README.md": "x\n",
}


class TestDocsOnlyPlanning:
    def test_a_docs_only_commit_plans_nothing_not_the_whole_suite(self, tmp_path):
        root, base = _repo(tmp_path, PY_PROJECT)
        _write(root, "README.md", "a one-line note\n")
        head = _commit(root)
        assert ps.plan_commit(root, {}, None, head, base) == ([], False)
        assert ps.docs_only_commit(root, {}, head, base) is True

    def test_a_commit_touching_code_still_uses_change_selection(self, tmp_path):
        root, base = _repo(tmp_path, PY_PROJECT)
        _write(root, "README.md", "y\n")
        _write(root, "pkg/widget.py", "def f():\n    return 2\n")
        head = _commit(root)
        planned, _ = ps.plan_commit(root, {}, None, head, base)
        assert [(p.origin, p.argv[-1]) for p in planned] == [(ps.ORIGIN_CHANGE, "tests/test_widget.py")]
        assert ps.docs_only_commit(root, {}, head, base) is False

    def test_a_config_change_is_not_docs_only_and_falls_back(self, tmp_path):
        root, base = _repo(tmp_path, PY_PROJECT)
        _write(root, "pyproject.toml", "[project]\nname='p2'\n")
        head = _commit(root)
        planned, _ = ps.plan_commit(root, {}, None, head, base)
        assert planned and planned[0].origin == ps.ORIGIN_DETECTED

    def test_the_configured_contract_still_runs_on_a_docs_only_commit(self, tmp_path):
        root, base = _repo(tmp_path, PY_PROJECT)
        _write(root, "README.md", "y\n")
        head = _commit(root)
        config = {"verification_commands": ["python -m pytest -q"]}
        planned, _ = ps.plan_commit(root, config, None, head, base)
        assert [(p.origin, p.argv) for p in planned] == [(ps.ORIGIN_CONTRACT, ["python", "-m", "pytest", "-q"])]
        assert ps.docs_only_commit(root, config, head, base) is False


@pytest.fixture
def captured_docs(tmp_path, monkeypatch):
    """A captured session in a Python project whose whole change is a one-line README note."""
    root, _base = _repo(tmp_path, {**PY_PROJECT, ".gitignore": ".openshard/\n__pycache__/\n.pytest_cache/\n"})
    monkeypatch.chdir(root)
    monkeypatch.setattr(cc, "python_executable", lambda _root: sys.executable)
    _claude(root, "UserPromptSubmit", prompt="add a note to the README")
    _write(root, "README.md", "a one-line note\n")
    _claude(root, "PostToolUse", tool_name="Edit", tool_input={"file_path": str(root / "README.md")})
    _claude(root, "Stop")
    _claude(root, "SessionEnd", reason="exit")
    head = _commit(root, "note")
    entry = [json.loads(ln) for ln in (root / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()][-1]
    return root, head, entry


class TestDocsOnlyRecorded:
    def _docs_commit(self, captured_docs) -> tuple[Path, str, dict]:
        return captured_docs

    def test_recorded_not_run_with_its_own_reason_and_never_verified(self, captured_docs):
        from openshard.history.verification_truth import OBSERVED_STATES, interpret_evidence
        from openshard.verification.verdict import commit_verdict

        root, sha, entry = self._docs_commit(captured_docs)
        out = _verify("--receipt", entry["receipt_id"], "--commit", sha)
        v = out["verification"]
        assert out["no_checks_applicable"] is True and out["checks"] == []
        assert (v["status"], v["artifact_sha"], v["checks_passed"]) == ("not_run", None, None)
        assert ps.REASON_NO_CHECKS_APPLICABLE in v["incomplete_reasons"]
        assert v["reason"] == "No checks needed: documentation-only change; OpenShard ran nothing."
        assert "checkout" not in out or out["checkout"] is None  # nothing was checked out or run

        attestations = ps.load_attestations(root / ".openshard")
        summary = ps.summarize_attestation(attestations[-1])
        assert ps.display_line(summary) == "No checks needed: documentation-only change (nothing run; not verified)"
        assert ps.display_heading(summary) == "Re-check"
        truth = interpret_evidence(None, post_session_verification={"evidence": [summary]})
        assert truth.state not in OBSERVED_STATES and truth.effective_status != "passed"
        assert commit_verdict(sha, attestations)["verdict"] == "unknown"

    def test_the_platform_projection_never_reads_it_as_verified(self, captured_docs):
        from openshard.history.verification_truth import OBSERVED_STATES
        from openshard.sync.evidence import build_evidence_envelope

        root, sha, entry = self._docs_commit(captured_docs)
        _verify("--receipt", entry["receipt_id"], "--commit", sha)
        envelope = build_evidence_envelope(entry, 0, ps.load_attestations(root / ".openshard"), core_version="t")
        assert envelope is not None
        [item] = envelope["evidence"]
        assert item["verification"]["status"] == "not_run"
        assert ps.REASON_NO_CHECKS_APPLICABLE in item["verification"]["incomplete_reasons"]
        assert envelope["state"]["state"] not in OBSERVED_STATES
        assert envelope["state"]["effective_status"] != "passed"

    def test_text_output_says_nothing_ran_and_strict_is_not_a_pass(self, captured_docs):
        root, sha, entry = self._docs_commit(captured_docs)
        result = CliRunner().invoke(cli, ["verify", "--receipt", entry["receipt_id"], "--commit", sha, "--strict"])
        assert result.exit_code == 2, result.output
        assert "No checks needed: documentation-only change. Nothing was run, so this commit is not verified." \
            in result.output
        assert "Re-verified" not in result.output


class TestTimeoutMessage:
    def test_a_timed_out_check_names_the_limit_and_the_flag_and_stays_unknown(self, tmp_path):
        check = ps.PlannedCheck(name="pytest (whole suite)", argv=["pytest"], kind="test",
                                origin=ps.ORIGIN_DETECTED, safety="safe", reason="")

        def runner(argv, **_kw):
            raise subprocess.TimeoutExpired(argv, 600)

        [result] = ps.run_checks([check], tmp_path, timeout=600, stream=False, runner=runner)
        assert result.status == "unknown" and result.note == "timed out after 600s; no exit code"
        assert ps.timeout_hint([result], 600) == (
            "pytest (whole suite) timed out after 600s and was stopped, so the outcome is unknown "
            "(not passed, not failed). To allow longer, re-run with --timeout SECONDS (for example --timeout 1800).")
        tree = ps.TreeState(head="a" * 40, dirty=False, tracked_dirty=False)
        att = ps.build_attestation(None, [result], before=tree, after=tree, started_at="2026-10-25T00:00:00Z",
                                   completed_at="2026-10-25T00:10:00Z", commit="a" * 40)
        assert att["verification"]["status"] == "unknown"
        assert ps.display_heading(ps.summarize_attestation(att)) == "Re-verified"

    def test_no_hint_without_a_timeout(self):
        check = ps.PlannedCheck(name="t", argv=["t"], kind="test", origin=ps.ORIGIN_CHANGE, safety="safe", reason="")
        assert ps.timeout_hint([ps.CheckRun(check, "failed", exit_code=1)], 600) is None


class TestDocsOnlyCli:
    def _contract(self, root: Path, *, sleep: bool = False) -> None:
        if sleep:
            _write(root, "tests/test_slow.py", "import time\n\ndef test_slow():\n    time.sleep(30)\n")
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "--amend", "--no-edit")
        cfg = {"verification_commands": [[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]]}
        _write(root, ".openshard/config.yml", json.dumps(cfg))

    def test_a_configured_contract_still_runs_on_a_docs_only_commit(self, captured_docs):
        root, sha, entry = captured_docs
        self._contract(root)
        out = _verify("--receipt", entry["receipt_id"], "--commit", sha)
        assert out["no_checks_applicable"] is False
        assert [c["origin"] for c in out["checks"]] == ["contract"]
        assert ps.REASON_NO_CHECKS_APPLICABLE not in out["verification"]["incomplete_reasons"]

    def test_a_timeout_is_said_plainly_with_the_flag_and_recorded_unknown(self, captured_docs):
        root, _sha, entry = captured_docs
        self._contract(root, sleep=True)
        sha = _git(root, "rev-parse", "HEAD")
        result = CliRunner().invoke(cli, ["verify", "--receipt", entry["receipt_id"], "--commit", sha,
                                          "--timeout", "2"])
        assert result.exit_code == 0, result.output
        assert "timed out after 2s and was stopped, so the outcome is unknown" in result.output
        assert "re-run with --timeout SECONDS" in result.output
        assert "Re-verified: Outcome unknown" in result.output
        [att] = ps.load_attestations(root / ".openshard")
        assert att["verification"]["status"] == "unknown"
