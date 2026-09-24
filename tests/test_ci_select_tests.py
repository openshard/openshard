"""Tests for scripts/ci/select_tests.py -- the Fast PR Gate's test selection.

Two layers:

* A synthetic mini-repository (``fake_repo``) pins the *rules*: docs never
  run tests, evidence-critical prefixes and CI infrastructure always run
  everything, targeted plans follow real imports (including imports inside
  functions and ``patch("pkg.mod.attr")`` string references), unknown files
  fail open to the full suite, and shards are balanced and deterministic.
* Assertions against the real repository pin the *curated lists*: every
  listed file exists, service files never leak into a parallel shard or
  the Windows slice, and the invariant floor is present in every code plan.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "select_tests.py"


def _load():
    spec = importlib.util.spec_from_file_location("ci_select_tests", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


st = _load()


# ---------------------------------------------------------------------------
# Synthetic repository
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, body: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    files = {
        "openshard/__init__.py": "",
        "openshard/util/__init__.py": "",
        "openshard/util/home.py": "def home(): return 1\n",
        "openshard/history/__init__.py": "from openshard.history.shard import Shard\n",
        "openshard/history/shard.py": "class Shard: ...\n",
        "openshard/leaf/__init__.py": "",
        "openshard/leaf/calc.py": "def add(a, b): return a + b\n",
        "openshard/leaf/other.py": "def noop(): ...\n",
        "openshard/leaf/lazy.py": """
            def go():
                from openshard.leaf.calc import add  # import inside a function
                return add(1, 2)
            """,
        "openshard/leaf/pkgdata.yml": "x: 1\n",
        "openshard/cli/__init__.py": "",
        "openshard/cli/main.py": "from openshard.leaf import lazy\nfrom .helpers import h\n",
        "openshard/cli/helpers.py": "def h(): ...\n",
        "tests/__init__.py": "",
        "tests/conftest.py": "",
        "tests/capture_fixtures.py": "",
        # Direct import of the leaf module.
        "tests/test_calc.py": "from openshard.leaf.calc import add\n",
        # Reaches calc only through cli.main -> lazy -> (function-level) calc.
        "tests/test_cli.py": "from openshard.cli.main import h\n",
        # References calc only via a patch() string literal.
        "tests/test_patched.py": """
            from unittest.mock import patch
            def test_x():
                with patch("openshard.leaf.calc.add"):
                    pass
            """,
        # Imports the package, which re-exports shard.
        "tests/test_history_pkg.py": "from openshard.history import Shard\n",
        # Touches nothing we can track.
        "tests/test_unmapped.py": "import json\n",
        # Unrelated leaf test.
        "tests/test_other.py": "from openshard.leaf.other import noop\n",
        # Stand-ins for the curated lists.
        "tests/test_invariant_a.py": "from openshard.history.shard import Shard\n",
        "tests/test_service_a.py": "from openshard.util.home import home\n",
        "tests/test_win_a.py": "from openshard.util.home import home\n",
        "tests/test_win_b.py": "from openshard.leaf.other import noop\n",
        "docs/ci.md": "# CI\n",
        "README.md": "# readme\n",
        "pyproject.toml": "[project]\nname='x'\n",
        ".github/workflows/ci.yml": "name: x\n",
        "scripts/ci/select_tests.py": "# me\n",
        "config.yml": "a: 1\n",
    }
    for rel, body in files.items():
        _write(root, rel, body)
    # Make the big shard candidates visibly different in size.
    (root / "tests/test_cli.py").write_text(
        "from openshard.cli.main import h\n" + "# pad\n" * 4000, encoding="utf-8"
    )
    monkeypatch.setattr(st, "REPO_ROOT", root)
    monkeypatch.setattr(st, "INVARIANT_TEST_FILES", ("tests/test_invariant_a.py",))
    monkeypatch.setattr(st, "SERVICE_TEST_FILES", ("tests/test_service_a.py",))
    monkeypatch.setattr(st, "WINDOWS_SMOKE_TEST_FILES", ("tests/test_win_a.py", "tests/test_win_b.py"))
    return root


def _plan(root: Path, *changed: str):
    graph = st.ModuleGraph.build(root)
    return st.make_plan(list(changed), graph, root)


class TestClassify:
    @pytest.mark.parametrize(
        "rel, kind",
        [
            ("README.md", "docs"),
            ("docs/architecture/x.md", "docs"),
            ("docs/assets/logo.png", "docs"),
            ("openshard/history/shard.py", "sensitive"),
            ("openshard/adapters/claude_hooks.py", "sensitive"),
            ("openshard/verification/plan.py", "sensitive"),
            ("openshard/security/paths.py", "sensitive"),
            ("openshard/sync/envelope.py", "sensitive"),
            ("openshard/util/git.py", "sensitive"),
            ("openshard/native/sandbox_apply.py", "sensitive"),
            (".github/workflows/ci.yml", "ci"),
            ("pyproject.toml", "ci"),
            ("tests/conftest.py", "ci"),
            ("scripts/ci/select_tests.py", "ci"),
            ("tests/test_cost.py", "test"),
            ("openshard/routing/x.py", "code"),
            ("evals/pr13/thing.py", "code"),
            ("openshard/config/default_config.yml", "other"),
            ("telemetry-server/app.py", "other"),
            ("config.yml", "other"),
        ],
    )
    def test_kinds(self, rel, kind):
        assert st.classify(rel) == kind

    def test_module_names(self):
        assert st.module_name_for("openshard/history/shard.py") == "openshard.history.shard"
        assert st.module_name_for("openshard/history/__init__.py") == "openshard.history"
        assert st.module_name_for("tests/test_x.py") == "tests.test_x"
        assert st.module_name_for("telemetry-server/app.py") is None
        assert st.module_name_for("openshard/tui/styles.tcss") is None


class TestTiers:
    def test_docs_only_runs_nothing(self, fake_repo):
        plan = _plan(fake_repo, "docs/ci.md", "README.md")
        assert plan.tier == "docs"
        assert plan.run_code is False
        assert plan.fast_files == [] and plan.service_files == [] and plan.windows is False
        assert plan.shards() == []

    def test_no_changed_files_fails_open_to_full(self, fake_repo):
        plan = _plan(fake_repo)
        assert plan.tier == "full"

    @pytest.mark.parametrize(
        "rel",
        [
            "openshard/history/shard.py",   # evidence-critical prefix
            "openshard/util/home.py",       # evidence-critical prefix
            ".github/workflows/ci.yml",     # CI infrastructure
            "pyproject.toml",
            "tests/conftest.py",
            "scripts/ci/select_tests.py",
            "openshard/leaf/pkgdata.yml",   # package data: cannot be mapped
            "config.yml",                   # unknown top-level file
            "openshard/leaf/deleted.py",    # not in the tree any more
        ],
    )
    def test_full_triggers(self, fake_repo, rel):
        plan = _plan(fake_repo, rel, "README.md")
        assert plan.tier == "full", plan.reasons
        assert plan.windows is True
        assert plan.service_files == ["tests/test_service_a.py"]
        # Every parallel-safe test file, and never a service file in a shard.
        assert "tests/test_service_a.py" not in plan.fast_files
        assert set(plan.fast_files) == {
            "tests/test_calc.py", "tests/test_cli.py", "tests/test_patched.py",
            "tests/test_history_pkg.py", "tests/test_unmapped.py", "tests/test_other.py",
            "tests/test_invariant_a.py", "tests/test_win_a.py", "tests/test_win_b.py",
        }

    def test_full_reason_names_the_sensitive_file(self, fake_repo):
        plan = _plan(fake_repo, "openshard/history/shard.py")
        assert any("openshard/history/shard.py" in r and "evidence-critical" in r for r in plan.reasons)


class TestTargeted:
    def test_leaf_change_follows_imports_transitively(self, fake_repo):
        plan = _plan(fake_repo, "openshard/leaf/calc.py")
        assert plan.tier == "targeted"
        selected = set(plan.fast_files)
        # direct import, function-level import via cli.main -> lazy, patch() string
        assert {"tests/test_calc.py", "tests/test_cli.py", "tests/test_patched.py"} <= selected
        # the invariant floor and untrackable tests always ride along
        assert {"tests/test_invariant_a.py", "tests/test_unmapped.py"} <= selected
        # unrelated leaf test is not selected
        assert "tests/test_other.py" not in selected
        # nothing reaches the service test or the Windows slice
        assert plan.service_files == []
        assert plan.windows is False

    def test_change_reaching_a_windows_smoke_file_enables_windows(self, fake_repo):
        plan = _plan(fake_repo, "openshard/leaf/other.py")
        assert plan.tier == "targeted"
        assert "tests/test_win_b.py" in plan.fast_files
        assert plan.windows is True

    def test_changed_test_file_runs_itself_plus_floor(self, fake_repo):
        plan = _plan(fake_repo, "tests/test_other.py")
        assert plan.tier == "targeted"
        assert set(plan.fast_files) == {
            "tests/test_other.py", "tests/test_invariant_a.py", "tests/test_unmapped.py"
        }

    def test_changed_service_test_goes_to_the_serial_job(self, fake_repo):
        plan = _plan(fake_repo, "tests/test_service_a.py")
        assert plan.tier == "targeted"
        assert plan.service_files == ["tests/test_service_a.py"]
        assert "tests/test_service_a.py" not in plan.fast_files

    def test_package_init_reexport_is_a_dependency(self, fake_repo):
        # openshard/history is sensitive in the real repo; in the fake one we
        # only care that ``from pkg import Name`` links test -> pkg -> module.
        graph = st.ModuleGraph.build(fake_repo)
        deps = graph.transitive_dependents({"openshard.history.shard"})
        assert "tests.test_history_pkg" in deps

    def test_relative_import_resolves(self, fake_repo):
        graph = st.ModuleGraph.build(fake_repo)
        assert "openshard.cli.helpers" in graph.imports["openshard.cli.main"]

    def test_broad_blast_radius_escalates_to_full(self, fake_repo, monkeypatch):
        monkeypatch.setattr(st, "FULL_ESCALATION_RATIO", 0.1)
        plan = _plan(fake_repo, "openshard/leaf/calc.py")
        assert plan.tier == "full"
        assert any("running everything" in r for r in plan.reasons)


class TestShards:
    def test_single_shard_below_threshold(self, fake_repo):
        files = ["tests/test_calc.py", "tests/test_other.py"]
        assert st.split_shards(files, 3, fake_repo) == [sorted(files)]

    def test_balanced_and_deterministic(self, fake_repo, monkeypatch):
        monkeypatch.setattr(st, "BYTES_PER_SHARD", 1)  # force max shards
        files = sorted(p.relative_to(fake_repo).as_posix() for p in (fake_repo / "tests").glob("test_*.py"))
        a = st.split_shards(files, 3, fake_repo)
        b = st.split_shards(list(reversed(files)), 3, fake_repo)
        assert a == b
        assert len(a) == 3
        assert sorted(f for s in a for f in s) == files  # partition: nothing lost or doubled
        # The padded file dominates, so it sits alone-ish and the others balance.
        sizes = [sum((fake_repo / f).stat().st_size for f in s) for s in a]
        assert max(sizes) - min(sizes) <= (fake_repo / "tests/test_cli.py").stat().st_size

    def test_weights_change_balance(self, fake_repo, monkeypatch):
        monkeypatch.setattr(st, "BYTES_PER_SHARD", 1)
        files = ["tests/test_calc.py", "tests/test_other.py", "tests/test_patched.py", "tests/test_unmapped.py"]
        plain = st.split_shards(files, 2, fake_repo)
        monkeypatch.setattr(st, "SHARD_WEIGHTS", {"tests/test_unmapped.py": 1000.0})
        weighted = st.split_shards(files, 2, fake_repo)
        assert plain != weighted
        # the heavily weighted file gets a shard to itself
        assert ["tests/test_unmapped.py"] in weighted

    def test_service_files_split_across_two_jobs_each_serial(self, fake_repo, monkeypatch):
        monkeypatch.setattr(
            st, "SERVICE_TEST_FILES", ("tests/test_service_a.py", "tests/test_win_a.py", "tests/test_win_b.py")
        )
        monkeypatch.setattr(st, "WINDOWS_SMOKE_TEST_FILES", ())
        plan = _plan(fake_repo, "config.yml")
        shards = plan.service_shards()
        assert len(shards) == 2
        assert sorted(f for s in shards for f in s) == sorted(plan.service_files)
        # a single service file is never split
        plan_one = _plan(fake_repo, "tests/test_service_a.py")
        assert plan_one.service_shards() == [["tests/test_service_a.py"]]
        matrix = plan.to_dict(3, fake_repo)["service_matrix"]
        assert [m["of"] for m in matrix["include"]] == [2, 2]

    def test_matrix_shape(self, fake_repo, monkeypatch):
        monkeypatch.setattr(st, "BYTES_PER_SHARD", 1)
        plan = _plan(fake_repo, "config.yml").to_dict(2, fake_repo)
        assert [s["id"] for s in plan["shard_matrix"]["include"]] == [1, 2]
        assert all(s["of"] == 2 for s in plan["shard_matrix"]["include"])
        assert all(s["files"].split() for s in plan["shard_matrix"]["include"])


class TestCli:
    def test_plan_from_file_writes_github_outputs(self, fake_repo, tmp_path, capsys):
        changed = tmp_path / "changed.txt"
        changed.write_text("openshard/leaf/calc.py\nREADME.md\n")
        out = tmp_path / "gh_output"
        rc = st.main(["plan", "--changed-files", str(changed), "--github-output", str(out), "--json"])
        assert rc == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["tier"] == "targeted"
        kv = dict(line.split("=", 1) for line in out.read_text().splitlines())
        assert kv["tier"] == "targeted"
        assert kv["run_code"] == "true"
        assert kv["run_shards"] == "true"
        assert kv["run_service"] == "false"
        assert json.loads(kv["service_matrix"]) == {"include": []}
        assert kv["windows"] == "false"
        assert kv["windows_files"] == "tests/test_win_a.py tests/test_win_b.py"
        matrix = json.loads(kv["shard_matrix"])
        assert matrix["include"][0]["files"].split() == plan["fast_files"]

    def test_docs_plan_outputs_disable_every_job(self, fake_repo, tmp_path):
        changed = tmp_path / "changed.txt"
        changed.write_text("README.md\n")
        out = tmp_path / "gh_output"
        assert st.main(["plan", "--changed-files", str(changed), "--github-output", str(out)]) == 0
        kv = dict(line.split("=", 1) for line in out.read_text().splitlines())
        assert kv["run_code"] == "false"
        assert kv["run_shards"] == "false"
        assert kv["run_service"] == "false"
        assert kv["windows"] == "false"
        assert json.loads(kv["shard_matrix"]) == {"include": []}

    def test_check_reports_missing_curated_file(self, fake_repo, monkeypatch, capsys):
        monkeypatch.setattr(st, "INVARIANT_TEST_FILES", ("tests/test_missing.py",))
        assert st.main(["check"]) == 1
        assert "test_missing.py" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The real repository
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_graph():
    return st.ModuleGraph.build(REPO_ROOT)


class TestRealRepository:
    def test_curated_lists_resolve(self):
        assert st.check_lists(REPO_ROOT) == []

    def test_full_plan_splits_service_files_over_two_jobs(self, real_graph):
        plan = st.make_plan(["pyproject.toml"], real_graph, REPO_ROOT)
        shards = plan.service_shards()
        assert len(shards) == 2
        assert sorted(f for s in shards for f in s) == sorted(st.SERVICE_TEST_FILES)

    def test_shard_weights_point_at_real_files(self):
        for f in st.SHARD_WEIGHTS:
            assert (REPO_ROOT / f).is_file(), f

    def test_service_files_are_isolated(self, real_graph):
        service = set(st.SERVICE_TEST_FILES)
        assert not service & set(st.WINDOWS_SMOKE_TEST_FILES)
        assert not service & set(st.INVARIANT_TEST_FILES)
        graph = real_graph
        assert not service & set(st.all_fast_files(graph))

    def test_evidence_critical_change_runs_everything(self, real_graph):
        graph = real_graph
        for rel in (
            "openshard/history/shard.py",
            "openshard/adapters/claude_capture_service.py",
            "openshard/adapters/capture_auth.py",
            "openshard/verification/executor.py",
            "openshard/security/secret_scan.py",
            "openshard/sync/envelope.py",
            "openshard/util/home.py",
            "openshard/native/sandbox_apply.py",
        ):
            plan = st.make_plan([rel], graph, REPO_ROOT)
            assert plan.tier == "full", (rel, plan.reasons)
            assert plan.service_files == list(st.SERVICE_TEST_FILES)
            assert plan.windows is True
            assert set(plan.fast_files) == set(st.all_fast_files(graph))

    def test_every_code_plan_includes_the_invariant_floor(self, real_graph):
        graph = real_graph
        for rel in ("tests/test_cost.py", "openshard/tui/app.py", "openshard/routing/model_resolver.py"):
            plan = st.make_plan([rel], graph, REPO_ROOT)
            assert plan.tier in ("targeted", "full")
            assert set(st.INVARIANT_TEST_FILES) <= set(plan.fast_files), rel

    def test_docs_only_is_docs(self, real_graph):
        graph = real_graph
        plan = st.make_plan(["README.md", "docs/ci.md", "CHANGELOG.md"], graph, REPO_ROOT)
        assert plan.tier == "docs"

    def test_script_is_stdlib_only(self):
        """The plan job runs it on the runner's stock python3 before any install."""
        import ast

        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
