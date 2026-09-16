from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openshard.analysis import repo_map as rm
from openshard.analysis.repo_map import (
    GitInfo,
    RepoMap,
    _porcelain_is_dirty,
    _sanitize_meta,
    build_repo_map,
    compute_repo_fingerprint,
)
from openshard.analysis.repo_map_cache import (
    cache_path_for,
    load_repo_map_cache,
    save_repo_map_cache,
)

_CLEAN = GitInfo(branch="main", head_commit="a" * 40, dirty=False, is_git=True)


def _git(branch="main", head="a" * 40, dirty=False, is_git=True) -> GitInfo:
    return GitInfo(branch=branch, head_commit=head, dirty=dirty, is_git=is_git)


def _build(d: str, git: GitInfo = _CLEAN) -> RepoMap:
    with patch.object(rm, "collect_git_info", return_value=git):
        return build_repo_map(Path(d), now_iso="2026-06-03T00:00:00Z")


class TestStackDetection(unittest.TestCase):

    def test_python_repo(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "main.py").write_text("print(1)\n", encoding="utf-8")
            Path(d, "pyproject.toml").write_text("[tool.pytest]\n", encoding="utf-8")
            m = _build(d).to_dict()
        self.assertIn("python", m["summary"]["languages"])
        self.assertIn("pip", m["summary"]["package_managers"])
        self.assertEqual(m["summary"]["test_commands"], ["python -m pytest"])

    def test_node_repo(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "index.ts").write_text("export const x = 1\n", encoding="utf-8")
            Path(d, "package.json").write_text(
                json.dumps({"dependencies": {"react": "^18"}, "scripts": {"test": "jest"}}),
                encoding="utf-8",
            )
            m = _build(d).to_dict()
        self.assertIn("typescript", m["summary"]["languages"])
        self.assertIn("npm", m["summary"]["package_managers"])
        self.assertEqual(m["summary"]["frameworks"], ["react"])
        self.assertEqual(m["summary"]["test_commands"], ["npm test"])

    def test_terraform_repo(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "main.tf").write_text('resource "x" "y" {}\n', encoding="utf-8")
            m = _build(d).to_dict()
        self.assertIn("main.tf", m["important_files"])

    def test_github_actions_repo(self):
        with tempfile.TemporaryDirectory() as d:
            wf = Path(d, ".github", "workflows")
            wf.mkdir(parents=True)
            Path(wf, "ci.yml").write_text("on: push\n", encoding="utf-8")
            m = _build(d).to_dict()
        self.assertIn(".github/workflows/ci.yml", m["important_files"])

    def test_docker_repo(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            m = _build(d).to_dict()
        self.assertIn("Dockerfile", m["important_files"])

    def test_no_package_files(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "notes.txt").write_text("hi\n", encoding="utf-8")
            m = _build(d).to_dict()
        self.assertEqual(m["summary"]["package_managers"], [])
        self.assertEqual(m["summary"]["test_commands"], [])
        self.assertEqual(m["summary"]["frameworks"], [])


class TestCounts(unittest.TestCase):

    def test_file_and_directory_counts(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").touch()
            sub = Path(d, "pkg")
            sub.mkdir()
            Path(sub, "b.py").touch()
            m = _build(d).to_dict()
        self.assertEqual(m["summary"]["file_count"], 2)
        self.assertGreaterEqual(m["summary"]["directory_count"], 1)

    def test_ignored_directories_only_present_ones(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "node_modules").mkdir()
            Path(d, "node_modules", "junk.js").write_text("x\n", encoding="utf-8")
            Path(d, "main.py").touch()
            m = _build(d).to_dict()
        self.assertIn("node_modules", m["ignored_directories"])
        # the whole skip list is not dumped - only dirs that actually exist
        self.assertNotIn(".venv", m["ignored_directories"])
        # ignored dir contents are not counted/scanned
        self.assertEqual(m["summary"]["file_count"], 1)
        self.assertNotIn("javascript", m["summary"]["languages"])

    def test_tmp_dir_is_skipped(self):
        # .tmp holds local build/package smoke artefacts - must be ignored like
        # node_modules so plans never suggest files from it.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d, ".tmp", "pkg-smoke")
            tmp.mkdir(parents=True)
            Path(tmp, "auth.py").write_text("x = 1\n", encoding="utf-8")
            Path(tmp, "package.json").write_text("{}\n", encoding="utf-8")
            Path(d, "main.py").touch()
            m = _build(d).to_dict()
        self.assertIn(".tmp", m["ignored_directories"])
        self.assertEqual(m["summary"]["file_count"], 1)
        blob = json.dumps(m)
        self.assertNotIn(".tmp/", blob)
        self.assertNotIn("javascript", m["summary"]["languages"])
        self.assertNotIn("pkg-smoke/auth.py", m["risky_areas"])

    def test_walk_cap_sets_warning(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(6):
                Path(d, f"f{i}.py").touch()
            with patch.object(rm, "_WALK_FILE_CAP", 3):
                m = _build(d).to_dict()
        self.assertEqual(m["summary"]["file_count"], 3)
        self.assertTrue(any("capped" in w for w in m["warnings"]))


class TestSafety(unittest.TestCase):

    def test_no_absolute_paths_or_contents(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".env").write_text("API_KEY=supersecretvalue123\n", encoding="utf-8")
            Path(d, "app.py").write_text("x = 1\n", encoding="utf-8")
            m = _build(d)
        blob = json.dumps(m.to_dict())
        # secret file contents never stored
        self.assertNotIn("supersecretvalue123", blob)
        # absolute tempdir path never stored
        self.assertNotIn(str(Path(d)), blob)
        self.assertNotIn(Path(d).as_posix(), blob)
        # .env appears only as risky-area path metadata
        self.assertIn(".env", m.to_dict()["risky_areas"])

    def test_paths_are_forward_slash_relative(self):
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d, "auth")
            sub.mkdir()
            Path(sub, "login.py").touch()
            m = _build(d).to_dict()
        for path in m["risky_areas"] + m["important_files"]:
            self.assertNotIn("\\", path)
            self.assertFalse(path.startswith("/"))


class TestFingerprint(unittest.TestCase):

    def test_same_state_same_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(rm, "collect_git_info", return_value=_git()):
                fp1, _, _ = compute_repo_fingerprint(Path(d))
                fp2, _, _ = compute_repo_fingerprint(Path(d))
        self.assertEqual(fp1, fp2)

    def test_head_change_changes_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(rm, "collect_git_info", return_value=_git(head="a" * 40)):
                fp1, _, _ = compute_repo_fingerprint(Path(d))
            with patch.object(rm, "collect_git_info", return_value=_git(head="b" * 40)):
                fp2, _, _ = compute_repo_fingerprint(Path(d))
        self.assertNotEqual(fp1, fp2)

    def test_dirty_changes_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(rm, "collect_git_info", return_value=_git(dirty=False)):
                fp_clean, _, _ = compute_repo_fingerprint(Path(d))
            with patch.object(rm, "collect_git_info", return_value=_git(dirty=True)):
                fp_dirty, _, _ = compute_repo_fingerprint(Path(d))
        self.assertNotEqual(fp_clean, fp_dirty)

    def test_non_git_layout_fingerprint_and_warning(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").touch()
            with patch.object(rm, "collect_git_info", return_value=_git(is_git=False)):
                fp, git, warnings = compute_repo_fingerprint(Path(d))
        self.assertTrue(fp)
        self.assertFalse(git.is_git)
        self.assertTrue(any("not a git repository" in w for w in warnings))


class TestPorcelainDirty(unittest.TestCase):

    def test_empty_is_clean(self):
        self.assertFalse(_porcelain_is_dirty(""))
        self.assertFalse(_porcelain_is_dirty(None))

    def test_real_change_is_dirty(self):
        self.assertTrue(_porcelain_is_dirty(" M src/main.py"))

    def test_only_openshard_changes_are_clean(self):
        # writing the cache must not dirty the tree
        self.assertFalse(_porcelain_is_dirty("?? .openshard/cache/repo-abc.json"))
        self.assertFalse(_porcelain_is_dirty(" M .openshard/runs.jsonl"))

    def test_mixed_changes_are_dirty(self):
        status = "?? .openshard/cache/repo-abc.json\n M src/main.py"
        self.assertTrue(_porcelain_is_dirty(status))

    def test_rename_uses_destination_path(self):
        self.assertFalse(_porcelain_is_dirty('R  old.txt -> .openshard/x.json'))


class TestSanitizeMeta(unittest.TestCase):

    def test_strips_crlf(self):
        out = _sanitize_meta("feat\r\ninjected")
        self.assertNotIn("\r", out)
        self.assertNotIn("\n", out)
        self.assertIn("feat", out)
        self.assertIn("injected", out)

    def test_caps_length(self):
        self.assertEqual(len(_sanitize_meta("x" * 500, cap=50)), 50)

    def test_absolute_path_reduced_to_name(self):
        self.assertEqual(_sanitize_meta("/home/me/secret/branch"), "branch")
        self.assertEqual(_sanitize_meta("C:\\Users\\me\\branch"), "branch")

    def test_none_passthrough(self):
        self.assertIsNone(_sanitize_meta(None))

    def test_strips_terminal_escapes_and_other_control_chars(self):
        out = _sanitize_meta("feat\x1b[31mred\x00\x07tail")
        self.assertEqual(out, "feat [31mred  tail")


class TestSerializationRoundTrip(unittest.TestCase):

    def test_from_dict_to_dict_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "main.py").touch()
            Path(d, "pyproject.toml").write_text("pytest\n", encoding="utf-8")
            original = _build(d).to_dict()
        restored = RepoMap.from_dict(original).to_dict()
        self.assertEqual(original, restored)


class TestCacheModule(unittest.TestCase):

    def test_cache_path_is_relative_forward_slash(self):
        with tempfile.TemporaryDirectory() as d:
            abs_path, display = cache_path_for("abc123", base=Path(d))
        self.assertEqual(display, ".openshard/cache/repo-abc123.json")
        self.assertTrue(abs_path.is_absolute())

    def test_save_then_load(self):
        with tempfile.TemporaryDirectory() as d:
            abs_path, _ = cache_path_for("abc123", base=Path(d))
            save_repo_map_cache(abs_path, {"hello": "world"})
            self.assertEqual(load_repo_map_cache(abs_path), {"hello": "world"})

    def test_load_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            abs_path, _ = cache_path_for("missing", base=Path(d))
            self.assertIsNone(load_repo_map_cache(abs_path))

    def test_load_corrupt_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            abs_path, _ = cache_path_for("corrupt", base=Path(d))
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_repo_map_cache(abs_path))


if __name__ == "__main__":
    unittest.main()
