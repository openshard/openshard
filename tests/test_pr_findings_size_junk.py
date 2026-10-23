"""PR findings over a real git change: size over 400 lines and leftover junk files."""

from __future__ import annotations

import subprocess
from pathlib import Path

from openshard.verification import pr_findings as pf


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "old_name.py").write_text("y = 2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _commit(root: Path, files: dict[str, str | bytes], *, remove: tuple[str, ...] = ()) -> str:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8")
    for rel in remove:
        (root / rel).unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "head")
    return _git(root, "rev-parse", "HEAD")


def _kinds(findings: list[pf.Finding] | None) -> list[str]:
    assert findings is not None
    return [f.kind for f in findings]


def test_401_lines_is_a_large_change_with_a_split_suggestion(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"big.py": "a = 1\n" * 401})
    (finding,) = pf.pr_findings(root, base, head) or []
    assert finding.kind == "large_change" and finding.count == 401
    assert "consider splitting" in finding.detail
    assert finding.to_dict()["paths"] == ["big.py"]
    assert finding.to_dict()["limit"] == 400


def test_400_lines_is_not_flagged(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"big.py": "a = 1\n" * 399}, remove=("app.py",))  # 399 added + 1 removed
    assert pf.pr_findings(root, base, head) == []


def test_lockfile_lines_do_not_count_but_are_reported(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"pnpm-lock.yaml": "dep: 1\n" * 5000, "web/package-lock.json": "{}\n" * 300,
                          "src.py": "b = 2\n" * 10})
    assert pf.pr_findings(root, base, head) == []
    files = pf.changed_files(root, base, head) or []
    finding = pf.large_change(files, limit=5)
    assert finding is not None and finding.count == 10
    assert finding.extra["lines_with_lockfiles"] == 5310


def test_a_rename_counts_its_edits_not_the_whole_file(tmp_path):
    root, base = _repo(tmp_path)
    (root / "old_name.py").rename(root / "new_name.py")
    head = _commit(root, {})
    files = pf.changed_files(root, base, head) or []
    assert [(f.status, f.old_path, f.path, f.added, f.removed) for f in files] == [
        ("R", "old_name.py", "new_name.py", 0, 0)]


def test_added_junk_files_are_named(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"app.py.orig": "x\n", "notes.txt": "fine\n", "pkg/__pycache__/m.cpython-312.pyc": b"\0\1",
                          ".DS_Store": b"\0", "debug.log": "trace\n", "patch.rej": "x\n"})
    (finding,) = pf.pr_findings(root, base, head) or []
    assert finding.kind == "junk_files" and finding.count == 5
    assert set(finding.to_dict()["paths"]) <= {"app.py.orig", "pkg/__pycache__/m.cpython-312.pyc", ".DS_Store",
                                               "debug.log", "patch.rej"}
    assert "notes.txt" not in finding.paths


def test_deleting_or_editing_a_junk_looking_file_is_not_flagged(tmp_path):
    root, base = _repo(tmp_path)
    mid = _commit(root, {"server.log": "a\n"})
    head = _commit(root, {}, remove=("server.log",))
    assert pf.pr_findings(root, mid, head) == []
    assert _kinds(pf.pr_findings(root, base, mid)) == ["junk_files"]


def test_a_clean_change_is_checked_and_empty(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"app.py": "x = 2\n"})
    assert pf.pr_findings(root, base, head) == []


def test_an_unknown_base_is_not_checked(tmp_path):
    root, _base = _repo(tmp_path)
    assert pf.pr_findings(root, "0" * 40, "HEAD") is None


def test_the_pr_range_starts_at_the_merge_base(tmp_path):
    """Lines that landed on the base branch after the fork are not the pull request's."""
    root, base = _repo(tmp_path)
    _git(root, "checkout", "-q", "-b", "feature")
    head = _commit(root, {"feature.py": "f = 1\n"})
    _git(root, "checkout", "-q", "main")
    main_tip = _commit(root, {"elsewhere.py": "z = 1\n" * 500})
    assert pf.pr_findings(root, main_tip, head) == []
    assert base


def test_findings_keep_no_source_text(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"big.py": "SECRET_MARKER = 1\n" * 500, "x.bak": "SECRET_MARKER\n"})
    findings = pf.pr_findings(root, base, head) or []
    assert len(findings) == 2
    assert "SECRET_MARKER" not in repr([f.to_dict() for f in findings])

