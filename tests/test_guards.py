"""Guards: file format, scope matching, planning into commit verification, CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
from click.testing import CliRunner

from openshard.guards.model import GuardError, load_guards, load_guards_at, parse_guard
from openshard.guards.plan import MAX_GUARD_CHECKS, plan_guard_checks
from openshard.guards.select import guards_for_change, path_matches

GUARD = """\
id: billing-rounding
version: 1
status: active
kind: test
title: Prorated seat charges round down
scope:
  paths: ["billing/**"]
check:
  command: "python -m pytest tests/guards/test_billing_rounding.py -q"
"""


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _with_guard(tmp_path: Path) -> tuple[Path, str]:
    repo = _repo(tmp_path)
    _write(repo, "billing/charge.py", "def charge():\n    return 1\n")
    _write(repo, "docs/readme.md", "hello\n")
    _write(repo, "tests/guards/test_billing_rounding.py", "def test_ok():\n    assert True\n")
    _write(repo, ".openshard/guards/billing-rounding.yml", GUARD)
    return repo, _commit(repo, "base with guard")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_valid_guard_parses():
    g = parse_guard(GUARD, ".openshard/guards/billing-rounding.yml")
    assert not isinstance(g, GuardError)
    assert g.runnable and g.label == "guard billing-rounding"
    assert g.command == ("python", "-m", "pytest", "tests/guards/test_billing_rounding.py", "-q")
    assert g.paths == ("billing/**",)


@pytest.mark.parametrize("text,needle", [
    (GUARD.replace("id: billing-rounding", "id: Billing Rounding"), "'id'"),
    (GUARD, "must match the file name"),
    (GUARD.replace("kind: test", "kind: vibes"), "'kind'"),
    (GUARD.replace("status: active", "status: on"), "'status'"),
    (GUARD.replace('paths: ["billing/**"]', 'paths: ["/etc/**"]'), "repository-relative"),
    (GUARD.replace('paths: ["billing/**"]', 'paths: ["../x"]'), "repository-relative"),
    (GUARD.replace("title: Prorated seat charges round down", "title: ''"), "'title'"),
    ("id: [", "not valid YAML"),
    ("- a list", "mapping"),
])
def test_invalid_guards_are_reported_not_loaded(text, needle):
    name = "other.yml" if needle == "must match the file name" else "billing-rounding.yml"
    if "Billing Rounding" in text:
        name = "Billing Rounding.yml"
    result = parse_guard(text, f".openshard/guards/{name}")
    assert isinstance(result, GuardError)
    assert needle in result.message


def test_test_guard_needs_a_command_but_a_note_does_not():
    no_cmd = GUARD.split("check:")[0]
    assert isinstance(parse_guard(no_cmd, ".openshard/guards/billing-rounding.yml"), GuardError)
    note = parse_guard(no_cmd.replace("kind: test", "kind: note"), ".openshard/guards/billing-rounding.yml")
    assert not isinstance(note, GuardError)
    assert not note.runnable and note.command is None


def test_load_guards_reports_bad_files_and_duplicates(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, ".openshard/guards/billing-rounding.yml", GUARD)
    _write(repo, ".openshard/guards/broken.yml", "id: [")
    _write(repo, ".openshard/guards/notes.txt", "ignored, not yaml")
    found = load_guards(repo)
    assert [g.id for g in found.guards] == ["billing-rounding"]
    assert [e.source_path for e in found.errors] == [".openshard/guards/broken.yml"]


def test_load_guards_at_reads_the_commit_not_the_working_tree(tmp_path):
    repo, base = _with_guard(tmp_path)
    (repo / ".openshard/guards/billing-rounding.yml").unlink()
    assert load_guards(repo).guards == []
    assert [g.id for g in load_guards_at(repo, base).guards] == ["billing-rounding"]


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern,path,expected", [
    ("billing/**", "billing/charge.py", True),
    ("billing/**", "billing/a/b/c.py", True),
    ("billing/**", "billingx/charge.py", False),
    ("billing/", "billing/a/charge.py", True),
    ("**/*.py", "charge.py", True),
    ("**/*.py", "a/b/charge.py", True),
    ("src/*.ts", "src/a.ts", True),
    ("src/*.ts", "src/x/a.ts", False),
    ("migrations/**/*.sql", "migrations/2026/x.sql", True),
    ("a?.py", "ab.py", True),
    ("a.py", "a_py", False),
])
def test_path_matches(pattern, path, expected):
    assert path_matches(pattern, path) is expected


def test_only_active_test_guards_match():
    base = parse_guard(GUARD, ".openshard/guards/billing-rounding.yml")
    proposed = parse_guard(GUARD.replace("status: active", "status: proposed"),
                           ".openshard/guards/billing-rounding.yml")
    assert [g.id for g, _ in guards_for_change([base], ["billing/x.py"])] == ["billing-rounding"]
    assert guards_for_change([proposed], ["billing/x.py"]) == []
    assert guards_for_change([base], ["docs/readme.md"]) == []


# ---------------------------------------------------------------------------
# Planning into commit verification
# ---------------------------------------------------------------------------


def _changed(repo: Path, base: str, head: str) -> list[PurePosixPath]:
    return [PurePosixPath(p) for p in _git(repo, "diff", "--name-only", base, head).splitlines() if p]


def test_change_in_scope_plans_the_guard_as_safe(tmp_path):
    repo, base = _with_guard(tmp_path)
    _write(repo, "billing/charge.py", "def charge():\n    return 2\n")
    head = _commit(repo, "agent change")
    plan = plan_guard_checks(repo, head, base, _changed(repo, base, head))
    assert [(c.name, c.safety, c.origin, c.kind) for c in plan.checks] == [
        ("guard billing-rounding", "safe", "guard", "test")]
    assert "billing/charge.py" in plan.checks[0].why
    assert not plan.truncated and not plan.failed


def test_change_outside_scope_plans_nothing(tmp_path):
    repo, base = _with_guard(tmp_path)
    _write(repo, "docs/readme.md", "changed\n")
    head = _commit(repo, "docs")
    assert plan_guard_checks(repo, head, base, _changed(repo, base, head)).checks == []


def test_editing_the_guard_needs_approval_and_the_base_version_is_planned(tmp_path):
    repo, base = _with_guard(tmp_path)
    _write(repo, "billing/charge.py", "def charge():\n    return 2\n")
    _write(repo, ".openshard/guards/billing-rounding.yml",
           GUARD.replace("test_billing_rounding.py -q", "test_billing_rounding.py -q -k nothing"))
    head = _commit(repo, "weaken guard")
    [check] = plan_guard_checks(repo, head, base, _changed(repo, base, head)).checks
    assert check.safety == "needs_approval"
    assert "edited guard file" in check.reason
    assert "-k" not in check.argv  # the approved definition, not the edited one


def test_deleting_a_guard_is_never_silent_even_outside_its_scope(tmp_path):
    repo, base = _with_guard(tmp_path)
    (repo / ".openshard/guards/billing-rounding.yml").unlink()
    head = _commit(repo, "remove guard")
    [check] = plan_guard_checks(repo, head, base, _changed(repo, base, head)).checks
    assert check.name == "guard billing-rounding"
    assert check.safety == "needs_approval"
    assert "removed" in check.reason


def test_editing_the_test_a_guard_runs_needs_approval(tmp_path):
    repo, base = _with_guard(tmp_path)
    _write(repo, "billing/charge.py", "def charge():\n    return 2\n")
    _write(repo, "tests/guards/test_billing_rounding.py", "def test_ok():\n    pass\n")
    head = _commit(repo, "weaken test")
    [check] = plan_guard_checks(repo, head, base, _changed(repo, base, head)).checks
    assert check.safety == "needs_approval"
    assert "tests/guards/test_billing_rounding.py" in check.reason


def test_a_guard_added_by_the_change_itself_does_not_run_yet(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "billing/charge.py", "x = 1\n")
    base = _commit(repo, "base")
    _write(repo, ".openshard/guards/billing-rounding.yml", GUARD)
    _write(repo, "billing/charge.py", "x = 2\n")
    head = _commit(repo, "add guard and change")
    assert plan_guard_checks(repo, head, base, _changed(repo, base, head)).checks == []


def test_first_commit_has_no_guards(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, ".openshard/guards/billing-rounding.yml", GUARD)
    _write(repo, "billing/charge.py", "x = 1\n")
    head = _commit(repo, "root")
    plan = plan_guard_checks(repo, head, None, [PurePosixPath("billing/charge.py")])
    assert plan.checks == [] and not plan.failed


def test_unlisted_change_with_guards_is_incomplete_not_covered(tmp_path):
    repo, base = _with_guard(tmp_path)
    _write(repo, "billing/charge.py", "x = 2\n")
    head = _commit(repo, "change")
    plan = plan_guard_checks(repo, head, base, None)
    assert plan.checks == [] and plan.failed


def test_more_guards_than_the_cap_marks_the_plan_truncated(tmp_path):
    repo = _repo(tmp_path)
    for i in range(MAX_GUARD_CHECKS + 2):
        _write(repo, f".openshard/guards/g{i}.yml", GUARD.replace("billing-rounding", f"g{i}", 1))
    _write(repo, "billing/charge.py", "x = 1\n")
    base = _commit(repo, "guards")
    _write(repo, "billing/charge.py", "x = 2\n")
    head = _commit(repo, "change")
    plan = plan_guard_checks(repo, head, base, _changed(repo, base, head))
    assert len(plan.checks) == MAX_GUARD_CHECKS and plan.truncated


def test_plan_commit_adds_guards_next_to_the_contract(tmp_path):
    from openshard.verification.post_session import plan_commit

    repo, base = _with_guard(tmp_path)
    _write(repo, "billing/charge.py", "x = 2\n")
    head = _commit(repo, "change")
    config = {"verification_commands": ["python -m pytest -q"]}
    planned, incomplete = plan_commit(repo, config, None, head, base)
    assert [c.origin for c in planned] == ["contract", "guard"]
    assert not incomplete


def test_plan_commit_runs_guards_on_a_docs_only_change_they_cover(tmp_path):
    from openshard.verification.post_session import plan_commit

    repo = _repo(tmp_path)
    _write(repo, "docs/api.md", "v1\n")
    _write(repo, ".openshard/guards/docs-links.yml", GUARD.replace("billing-rounding", "docs-links", 1)
           .replace('"billing/**"', '"docs/**"'))
    base = _commit(repo, "base")
    _write(repo, "docs/api.md", "v2\n")
    head = _commit(repo, "docs change")
    planned, _ = plan_commit(repo, {}, None, head, base)
    assert [c.name for c in planned] == ["guard docs-links"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(repo: Path, *args: str, monkeypatch) -> tuple[int, str]:
    from openshard.cli.main import cli

    monkeypatch.chdir(repo)
    result = CliRunner().invoke(cli, ["guard", *args])
    return result.exit_code, result.output


def test_guard_list_shows_guards_errors_and_ignore_state(tmp_path, monkeypatch):
    repo, _ = _with_guard(tmp_path)
    _write(repo, ".openshard/guards/broken.yml", "id: [")
    code, out = _cli(repo, "list", "--json", monkeypatch=monkeypatch)
    doc = json.loads(out)
    assert code == 0
    assert [g["id"] for g in doc["guards"]] == ["billing-rounding"]
    assert doc["errors"][0]["source_path"] == ".openshard/guards/broken.yml"
    assert doc["git_ignored"] is False
    (repo / ".gitignore").write_text(".openshard/\n")
    _, text = _cli(repo, "list", monkeypatch=monkeypatch)
    assert "Git ignores .openshard/guards/" in text


@pytest.mark.skipif(sys.platform == "win32", reason="runs a real pytest subprocess")
def test_guard_run_passes_then_catches_the_repeat(tmp_path, monkeypatch):
    repo, _ = _with_guard(tmp_path)
    _write(repo, "billing/charge.py", "def charge():\n    return 2\n")
    code, out = _cli(repo, "run", "--json", monkeypatch=monkeypatch)
    assert code == 0, out
    assert json.loads(out)["results"][0]["status"] == "passed"
    _write(repo, "tests/guards/test_billing_rounding.py", "def test_ok():\n    assert False\n")
    code, out = _cli(repo, "run", "--json", monkeypatch=monkeypatch)
    assert code == 1, out
    assert json.loads(out)["results"][0]["status"] == "failed"


def test_guard_run_with_nothing_in_scope(tmp_path, monkeypatch):
    repo, _ = _with_guard(tmp_path)
    _write(repo, "docs/readme.md", "changed\n")
    code, out = _cli(repo, "run", monkeypatch=monkeypatch)
    assert code == 0 and "No guard applies" in out


# ---------------------------------------------------------------------------
# End to end: a captured agent session, then `verify --commit` runs the guard
# ---------------------------------------------------------------------------

SID = "77777777-6666-4777-8888-999999999999"


def _hook(repo: Path, event: str, **fields) -> None:
    from openshard.adapters.claude_hooks import handle_claude_hook

    handle_claude_hook({"session_id": SID, "cwd": str(repo), "hook_event_name": event, **fields},
                       env={"CLAUDE_PROJECT_DIR": str(repo)})


@pytest.mark.skipif(sys.platform == "win32", reason="runs a real pytest subprocess")
def test_verify_commit_records_a_failed_guard_as_observed_evidence(tmp_path, monkeypatch):
    from openshard.cli.main import cli
    from openshard.verification import change_checks as cc
    from openshard.verification import post_session as ps

    repo = _repo(tmp_path)
    _write(repo, ".gitignore", ".openshard/*\n!.openshard/guards/\n__pycache__/\n.pytest_cache/\n")
    _write(repo, "pyproject.toml", "[project]\nname='p'\n")
    _write(repo, "calc.py", "def add(a, b):\n    return a + b\n")
    _write(repo, "tests/__init__.py", "")
    _write(repo, "tests/guards/__init__.py", "")
    _write(repo, "tests/guards/test_add_is_int.py",
           "from calc import add\n\ndef test_int():\n    assert isinstance(add(2, 3), int)\n")
    _write(repo, ".openshard/guards/add-is-int.yml", GUARD.replace("billing-rounding", "add-is-int", 1)
           .replace('"billing/**"', '"calc.py"')
           .replace("tests/guards/test_billing_rounding.py", "tests/guards/test_add_is_int.py"))
    _commit(repo, "guard added after a correction")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cc, "python_executable", lambda _root: sys.executable)

    _hook(repo, "UserPromptSubmit", prompt="make add precise")
    _write(repo, "calc.py", "def add(a, b):\n    return float(a + b)\n")
    _hook(repo, "PostToolUse", tool_name="Edit", tool_input={"file_path": str(repo / "calc.py")})
    _hook(repo, "Stop")
    _hook(repo, "SessionEnd", reason="exit")
    head = _commit(repo, "agent repeats the mistake")
    entry = [json.loads(ln) for ln in (repo / ".openshard/runs.jsonl").read_text().splitlines()][-1]

    result = CliRunner().invoke(cli, ["verify", "--json", "--receipt", entry["receipt_id"], "--commit", head])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert [c["name"] for c in out["checks"] if c.get("origin") == "guard"] == ["guard add-is-int"]
    statuses = {c["name"]: c["status"] for c in out["verification"]["checks"]}
    assert statuses["guard add-is-int"] == "failed"
    assert out["verification"]["status"] == "failed"
    assert out["verification"]["artifact_sha"] == head  # bound to the exact commit
    attestation = ps.load_attestations(repo / ".openshard")[-1]
    assert [c["origin"] for c in attestation["checks"] if c["name"] == "guard add-is-int"] == ["guard"]


def test_setup_rule_keeps_guards_committable_and_upgrades_an_older_rule(tmp_path):
    from openshard.adapters.claude_hooks_install import history_ignore_state

    repo = _repo(tmp_path)
    exclude = repo / ".git/info/exclude"
    exclude.write_text("# added by openshard setup\n.openshard/*\n!.openshard/config.yml\n")  # an older setup
    assert history_ignore_state(repo)["status"] == "already_ignored"
    assert "!.openshard/guards/" in exclude.read_text()

    def ignored(rel: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", "--no-index", rel], cwd=repo).returncode == 0

    assert ignored(".openshard/runs.jsonl")
    assert not ignored(".openshard/guards/x.yml")
    assert not ignored(".openshard/config.yml")


def test_a_users_own_ignore_rule_is_never_edited(tmp_path):
    from openshard.adapters.claude_hooks_install import history_ignore_state

    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text(".openshard/\n")
    before = (repo / ".git/info/exclude").read_text() if (repo / ".git/info/exclude").exists() else ""
    assert history_ignore_state(repo)["status"] == "already_ignored"
    after = (repo / ".git/info/exclude").read_text() if (repo / ".git/info/exclude").exists() else ""
    assert before == after


def test_a_leading_dot_slash_is_dropped_but_dot_folders_are_kept():
    g = parse_guard(GUARD.replace('paths: ["billing/**"]', 'paths: ["./.github/workflows/**"]'),
                    ".openshard/guards/billing-rounding.yml")
    assert not isinstance(g, GuardError)
    assert g.paths == (".github/workflows/**",)
