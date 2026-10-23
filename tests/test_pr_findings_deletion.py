"""PR findings: a change, especially a bug fix, that removes public code, endpoints or tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from openshard.verification import pr_findings as pf

API = '''from fastapi import APIRouter

router = APIRouter()


def parse(x):
    return int(x)


def _helper():
    return 1


class Client:
    def fetch(self):
        return 1

    def _retry(self):
        return 2


@router.get("/users/{uid}")
def get_user(uid):
    return parse(uid)
'''
TESTS = '''def test_parse():
    assert 1


def test_parse_rejects_text():
    assert 1
'''
JS = '''app.get("/health", ok);
describe("x", () => {
  it("works", () => {});
  test("also", () => {});
});
'''


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "api.py").write_text(API, encoding="utf-8")
    (root / "tests" / "test_api.py").write_text(TESTS, encoding="utf-8")
    (root / "server.test.js").write_text(JS, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _commit(root: Path, subject: str, files: dict[str, str] | None = None, remove: tuple[str, ...] = ()) -> str:
    for rel, body in (files or {}).items():
        (root / rel).write_text(body, encoding="utf-8")
    for rel in remove:
        (root / rel).unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", subject)
    return _git(root, "rev-parse", "HEAD")


def _removal(root: Path, base: str, head: str, title: str | None = None) -> pf.Finding | None:
    findings = pf.pr_findings(root, base, head, title=title) or []
    hits = [f for f in findings if f.kind in ("fix_by_deletion", "removed_public_code")]
    return hits[0] if hits else None


def test_a_fix_that_deletes_the_endpoint_and_its_test_is_fix_by_deletion(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, "Fix crash when the user id is text", {
        "api.py": API.split('@router.get')[0],
        "tests/test_api.py": TESTS.split("\n\n\ndef test_parse_rejects_text")[0] + "\n",
    })
    finding = _removal(root, base, head)
    assert finding is not None and finding.kind == "fix_by_deletion"
    d = finding.to_dict()
    assert (d["removed_functions"], d["removed_endpoints"], d["removed_tests"]) == (1, 1, 1)
    assert d["reads_as_fix"] is True and d["fix_basis"] == "Fix crash when the user id is text"
    assert set(d["names"]) == {"GET /users/{uid}", "get_user", "test_parse_rejects_text"}


def test_the_same_removal_in_a_feature_change_is_removed_public_code(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, "Drop the old client", {"api.py": API.replace(
        "class Client:\n    def fetch(self):\n        return 1\n\n    def _retry(self):\n        return 2\n", "")})
    finding = _removal(root, base, head)
    assert finding is not None and finding.kind == "removed_public_code"
    assert set(finding.extra["names"]) == {"Client", "Client.fetch"} and finding.extra["reads_as_fix"] is False


def test_a_pull_request_title_can_mark_it_a_fix(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, "Tidy", {"api.py": API.replace("def parse(x):\n    return int(x)\n", "")})
    assert _removal(root, base, head, title="Hotfix: parsing").kind == "fix_by_deletion"  # type: ignore[union-attr]


def test_removing_private_names_or_moving_a_function_is_not_a_removal(tmp_path):
    root, base = _repo(tmp_path)
    moved = API.replace("def parse(x):\n    return int(x)\n", "").replace("def _helper():\n    return 1\n", "")
    head = _commit(root, "Fix: move parse", {"api.py": moved, "parsing.py": "def parse(x):\n    return int(x)\n"})
    assert _removal(root, base, head) is None


def test_deleting_a_test_file_and_js_tests_and_routes_are_counted(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, "fix flaky suite", {"server.test.js": 'describe("x", () => {\n  it("works", () => {});\n});\n'},
                   remove=("tests/test_api.py",))
    finding = _removal(root, base, head)
    assert finding is not None and finding.kind == "fix_by_deletion"
    d = finding.to_dict()
    assert d["deleted_test_files"] == 1 and d["paths"] == ["tests/test_api.py"]
    assert d["removed_endpoints"] == 1 and "GET /health" in d["names"]
    assert d["removed_tests"] == 3  # two Python tests in the deleted file, one JS test (counted, not named)


def test_an_unparseable_head_is_not_called_a_removal(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, "fix", {"api.py": "def broken(:\n"})
    assert _removal(root, base, head) is None


def test_adding_code_is_not_a_removal(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, "Fix parse", {"api.py": API + "\n\ndef extra():\n    return 3\n"})
    assert _removal(root, base, head) is None
